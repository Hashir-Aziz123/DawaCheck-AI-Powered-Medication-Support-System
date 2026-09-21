"""
Prompt templates for the interaction-checking pipeline.

Kept separate from node logic so they can be iterated and versioned
independently without touching the graph execution code.

Template variables:
  CHECK_INTERACTION_PROMPT  — {drug_a_name}, {drug_b_name},
                               {drug_a_fda_text}, {drug_b_fda_text}
  VERIFY_GROUNDEDNESS_PROMPT — {drug_a_name}, {drug_b_name},
                                {interaction_claim}, {citation_text},
                                {source_fda_text}

Design philosophy:
  - check_interaction is the RECALL step: lean toward finding interactions.
    It is better to surface a potential interaction that gets filtered by
    verification than to miss a real one.
  - verify_groundedness is the PRECISION step: it filters out hallucinated
    or poorly-supported citations. This is where quality control happens.
"""

from langchain_core.prompts import ChatPromptTemplate


CHECK_INTERACTION_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        """You are a pharmacovigilance assistant. Analyse the FDA label text for two \
drugs and determine whether either label mentions an interaction with the other drug.

RULES:
1. Use ONLY the FDA label text provided below. Do not use outside knowledge.
2. Search Drug A's text for any mention of Drug B (by brand name, generic name, \
active ingredient, or pharmacological class), and vice versa.
3. An "interaction" includes any of the following mentioned in the text:
   - A statement that one drug affects the other's efficacy, metabolism, or safety
   - A warning about combined use (e.g., increased risk, reduced benefit)
   - A recommendation to avoid, monitor, or adjust dosage when used together
   - A statement that one drug has a synergistic, additive, or antagonistic effect \
with the other
4. If you find an interaction:
   - Set interaction_found = true
   - Write a short, plain-language claim describing the interaction. Mirror the \
language of the source text closely — do not add severity words that aren't in \
the original text.
   - Copy an exact, verbatim quote from the FDA text as the citation. The quote \
should be a complete sentence or clause that describes the interaction effect — \
not just a list of drug names.
5. If no interaction is mentioned anywhere in the provided text:
   - Set interaction_found = false
   - Set claim and citation to null

When in doubt, lean toward reporting the interaction. The verification step will \
confirm whether the citation is adequately supported.""",
    ),
    (
        "human",
        """Drug A: {drug_a_name}
Drug B: {drug_b_name}

--- Drug A FDA Label Text ---
{drug_a_fda_text}

--- Drug B FDA Label Text ---
{drug_b_fda_text}

Analyse the text above. Does either drug's label mention an interaction with the \
other drug?

Respond with ONLY a JSON object in this exact format:
{{"interaction_found": true or false, "claim": "description of interaction" or null, "citation": "exact verbatim quote from FDA text" or null}}""",
    ),
])


VERIFY_GROUNDEDNESS_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        """You are a citation verification assistant. Check whether a given citation \
is supported by the source text.

RULES:
1. Check TWO conditions:
   - Condition A (PRESENCE): Does the citation text appear in the source? Minor \
differences are acceptable: small omissions of non-essential words, whitespace \
or punctuation changes, and trivial phrasing differences are all fine. The core \
meaning and key medical terms must match. Only fail this condition if the citation \
is fabricated or substantially paraphrased.
   - Condition B (SUPPORT): Does the citation, read on its own, describe an \
interaction effect? It must say something about what happens when the drugs are \
used together — not merely list drug names.

2. Return is_grounded = true if BOTH conditions are met.

3. Return is_grounded = false if EITHER condition fails. Specifically:
   - Fail Condition B if the citation is only a list of drug names, a table \
heading, a section label, or a cross-reference — even if it appears verbatim.

4. Provide brief reasoning covering both conditions.""",
    ),
    (
        "human",
        """Interaction being checked:
Drug A: {drug_a_name}
Drug B: {drug_b_name}

Claimed interaction: {interaction_claim}

Citation to verify:
"{citation_text}"

--- Source FDA Text to search in ---
{source_fda_text}

Is this citation present in the source text AND does it describe an interaction effect?

Respond with ONLY a JSON object in this exact format:
{{"is_grounded": true or false, "reasoning": "brief explanation"}}""",
    ),
])
