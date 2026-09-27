"""
Prompt templates for the interaction-checking pipeline.

Kept separate from node logic so they can be iterated and versioned
independently without touching the graph execution code.

Template variables:
  CHECK_INTERACTION_PROMPT   -- {drug_a_name}, {drug_b_name},
                                {drug_a_fda_text}, {drug_b_fda_text},
                                {drug_a_classes}, {drug_b_classes}
  VERIFY_GROUNDEDNESS_PROMPT -- {drug_a_name}, {drug_b_name},
                                {interaction_claim}, {citation_text},
                                {source_fda_text},
                                {match_type}, {drug_a_classes}, {drug_b_classes}

Design philosophy:
  - check_interaction is the RECALL step: lean toward finding interactions.
    It is better to surface a potential interaction that gets filtered by
    verification than to miss a real one.
  - verify_groundedness is the PRECISION step: it filters out hallucinated
    or poorly-supported citations. This is where quality control happens.

Class-level matching (new):
  FDA labels often warn about pharmacological classes rather than naming
  individual drugs. For example, tramadol's label may warn about
  "serotonergic drugs" without naming sertraline. When Drug A's label warns
  about a class that Drug B belongs to (or vice versa), that is a real,
  documentable interaction -- it just needs to be labelled differently
  (match_type: "class_level") and the claim must be worded to make the
  class-level connection explicit, not phrased as a direct named mention.
"""

from langchain_core.prompts import ChatPromptTemplate


CHECK_INTERACTION_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        """You are a pharmacovigilance assistant. Analyse the FDA label text for two \
drugs and determine whether either label mentions an interaction with the other drug \
or with the pharmacological class the other drug belongs to.

RULES:
1. Use ONLY the FDA label text and class lists provided below. Do not use outside knowledge.
2. Check TWO types of matches, in order:

   TYPE A -- DIRECT match:
   Search Drug A's text for any mention of Drug B (by brand name, generic name, or \
active ingredient), and vice versa.

   TYPE B -- CLASS-LEVEL match:
   If no direct match is found, check whether Drug A's label text warns about a \
pharmacological class that Drug B belongs to, or vice versa. The known classes for \
each drug are provided explicitly below -- do not infer classes from outside knowledge.

3. An "interaction" includes any of the following mentioned in the text:
   - A statement that one drug affects the other's efficacy, metabolism, or safety
   - A warning about combined use (e.g., increased risk, reduced benefit)
   - A recommendation to avoid, monitor, or adjust dosage when used together
   - A statement that one drug has a synergistic, additive, or antagonistic effect \
with the other
   - A warning about a drug class that includes the other drug

4. If you find an interaction:
   - Set interaction_found = true
   - Set match_type = "direct" for Type A, or "class_level" for Type B
   - Write a short, plain-language claim:
     * For "direct": mirror the language of the source text. Do not add severity \
words that are not in the original text.
     * For "class_level": make the class connection explicit. State that Drug A's \
label warns about Drug B's class, and name the class. Example: "Tramadol's labeling \
warns about concurrent use with serotonergic agents; Sertraline is classified as a \
Selective Serotonin Reuptake Inhibitor, which is a serotonergic agent."
   - Copy an exact, verbatim quote from the FDA text as the citation. The quote \
must be a complete sentence or clause that describes the interaction or class-level \
warning -- not just a list of drug names.

5. If no interaction of either type is mentioned:
   - Set interaction_found = false
   - Set claim, citation, and match_type to null

When in doubt, lean toward reporting the interaction. The verification step will \
confirm whether the citation is adequately supported.""",
    ),
    (
        "human",
        """Drug A: {drug_a_name}
Drug B: {drug_b_name}

Drug A known pharmacological classes: {drug_a_classes}
Drug B known pharmacological classes: {drug_b_classes}

--- Drug A FDA Label Text ---
{drug_a_fda_text}

--- Drug B FDA Label Text ---
{drug_b_fda_text}

Analyse the text above. Does either drug's label mention an interaction with the \
other drug (directly or via its pharmacological class)?

Respond with ONLY a JSON object in this exact format:
{{"interaction_found": true or false, "match_type": "direct" or "class_level" or null, "claim": "description of interaction" or null, "citation": "exact verbatim quote from FDA text" or null}}""",
    ),
])


VERIFY_GROUNDEDNESS_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        """You are a citation verification assistant. Check whether a given citation \
is supported by the source text, applying different criteria depending on the match type.

RULES:
1. Check TWO conditions:
   - Condition A (PRESENCE): Does the citation text appear in the source? Minor \
differences are acceptable: small omissions of non-essential words, whitespace \
or punctuation changes, and trivial phrasing differences are all fine. The core \
meaning and key medical terms must match. Only fail this condition if the citation \
is fabricated or substantially paraphrased.
   - Condition B (SUPPORT): Does the citation, read on its own, describe an \
interaction effect? It must say something about what happens when the drugs are \
used together (or with the stated drug class) -- not merely list drug names.

2. For "class_level" match type, apply an additional check:
   - Condition C (CLASS ATTRIBUTION): The drug_b_classes / drug_a_classes provided \
confirm whether the class attribution is correct. If the citation warns about a class \
(e.g. "serotonergic agents") and the other drug genuinely belongs to that class \
(confirmed by the class lists below), this condition passes. Fail it only if the \
model's claim attributes the wrong class to a drug.

3. Return is_grounded = true if ALL applicable conditions are met:
   - "direct" match: Conditions A and B must both pass.
   - "class_level" match: Conditions A, B, and C must all pass.

4. Return is_grounded = false if ANY condition fails. Specifically:
   - Fail Condition B if the citation is only a list of drug names, a table \
heading, a section label, or a cross-reference -- even if it appears verbatim.

5. Provide brief reasoning covering all applicable conditions.""",
    ),
    (
        "human",
        """Interaction being checked:
Drug A: {drug_a_name}
Drug B: {drug_b_name}

Match type: {match_type}
Drug A pharmacological classes: {drug_a_classes}
Drug B pharmacological classes: {drug_b_classes}

Claimed interaction: {interaction_claim}

Citation to verify:
"{citation_text}"

--- Source FDA Text to search in ---
{source_fda_text}

Is this citation present in the source text AND does it describe an interaction effect? \
(For class_level: also confirm the class attribution is correct per the class lists above.)

Respond with ONLY a JSON object in this exact format:
{{"is_grounded": true or false, "reasoning": "brief explanation"}}""",
    ),
])
