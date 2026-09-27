"""
LangGraph node implementations for the drug interaction-checking pipeline.

Each node is a pure function: InteractionState -> InteractionState (partial update).
LLM nodes use structured output (Pydantic models) so parsing is reliable and
type-safe, not brittle string-splitting.

LLM setup:
  Model: openai/gpt-oss-120b (Groq)
  Temperature: 0.0 for both calls -- deliberate choice.
  Reproducibility and factual accuracy matter more than response diversity for
  grounded medical claims; the same drug pair should always produce the same
  verification verdict.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_groq import ChatGroq
from pydantic import BaseModel, Field

from worker.langgraph_pipeline.prompts import (
    CHECK_INTERACTION_PROMPT,
    VERIFY_GROUNDEDNESS_PROMPT,
)
from worker.langgraph_pipeline.state import InteractionState

# Load infra/.env -- same pattern as core/db/session.py so GROQ_API_KEY is
# available whether the pipeline is called from scripts, tests, or the worker.
_env_path = Path(__file__).resolve().parents[3] / "infra" / ".env"
load_dotenv(dotenv_path=_env_path)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured output schemas for LLM responses
# ---------------------------------------------------------------------------

class InteractionCheckResult(BaseModel):
    """Parsed output from the check_interaction LLM call."""
    interaction_found: bool = Field(
        description=(
            "True if the provided FDA label text explicitly mentions an interaction "
            "between Drug A and Drug B (or their active ingredients / drug classes)."
        )
    )
    match_type: str | None = Field(
        default=None,
        description=(
            'How the interaction was found: "direct" if the label names the other drug '
            'explicitly, "class_level" if the label warns about a class that the other '
            "drug belongs to. Null when interaction_found=False."
        ),
    )
    claim: str | None = Field(
        default=None,
        description=(
            "Plain-language description of the interaction (1-2 sentences). "
            "Required when interaction_found=True, null otherwise. "
            "For class_level matches, must explicitly name the class connection."
        ),
    )
    citation: str | None = Field(
        default=None,
        description=(
            "A complete, verbatim sentence or clause copied from the FDA label text "
            "that explicitly states the interaction or its effect (e.g., increased "
            "bleeding risk, reduced efficacy). "
            "MUST NOT be a bare list of drug names, a table heading, a section label, "
            "or a cross-reference -- those do not qualify regardless of verbatim presence. "
            "Required when interaction_found=True, null otherwise."
        ),
    )


class GroundednessCheckResult(BaseModel):
    """Parsed output from the verify_groundedness LLM call."""
    is_grounded: bool = Field(
        description=(
            "True if the citation is present verbatim or as a faithful near-verbatim "
            "excerpt of the source FDA text, AND describes an interaction effect, "
            "AND (for class_level matches) the class attribution is confirmed correct."
        )
    )
    reasoning: str = Field(
        description="Brief explanation of the grounding verdict (1-3 sentences)."
    )


# ---------------------------------------------------------------------------
# LLM factory -- isolated so it can be patched cleanly in tests
# ---------------------------------------------------------------------------

def _get_llm() -> ChatGroq:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Add it to infra/.env and restart."
        )
    return ChatGroq(
        model="openai/gpt-oss-120b",
        # Temperature 0.0 is deliberate -- see module docstring.
        temperature=0.0,
        groq_api_key=api_key,
    )


# ---------------------------------------------------------------------------
# Chain builders -- isolated so they can be patched cleanly in tests
# ---------------------------------------------------------------------------

def _build_interaction_chain(llm: ChatGroq):
    # method="json_mode" uses response_format={"type":"json_object"} instead of
    # function/tool calling -- required for models that support JSON output but
    # not the tool-call protocol (e.g. openai/gpt-oss-120b on Groq).
    return CHECK_INTERACTION_PROMPT | llm.with_structured_output(
        InteractionCheckResult, method="json_mode"
    )


def _build_groundedness_chain(llm: ChatGroq):
    return VERIFY_GROUNDEDNESS_PROMPT | llm.with_structured_output(
        GroundednessCheckResult, method="json_mode"
    )


# ---------------------------------------------------------------------------
# Helper: collect and format all FDA text from a drug dict
# ---------------------------------------------------------------------------

# Maximum characters of FDA text sent per drug. FDA labels for well-studied drugs
# (e.g. Mefenamic Acid, Warfarin) can be many thousands of characters; the model's
# TPM limit means we must cap this. 2500 chars ~= 625 tokens per drug -- leaves
# enough headroom for system prompt + format instructions within an 8000-token limit.
_MAX_FDA_TEXT_CHARS = 2500


def _collect_fda_text(drug: dict, max_chars: int = _MAX_FDA_TEXT_CHARS) -> str:
    """Concatenate all FDA label fields from every ingredient into one text block.

    Includes drug_interactions, warnings, and boxed_warning.  Each section is
    labelled with the ingredient name and field so the LLM can distinguish sources.
    Returns a fallback string when no FDA data is present so the prompt is never
    silently empty.

    If the combined text exceeds max_chars, it is truncated and a notice appended
    so the LLM knows the text was cut -- the most clinically relevant text (drug
    interactions, which comes first) is preserved over the tail of warnings.
    """
    parts = []
    for ing in drug.get("ingredients", []):
        name = ing.get("generic_name", "unknown ingredient")
        for field in ("drug_interactions", "warnings", "boxed_warning"):
            text = ing.get(field)
            if text and text.strip():
                label = field.replace("_", " ").title()
                parts.append(f"[{name} -- {label}]\n{text.strip()}")
    if not parts:
        return "No FDA label data available for this drug."
    combined = "\n\n".join(parts)
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n\n[... FDA text truncated for length]"
    return combined


def _collect_class_names(drug: dict) -> list[str]:
    """Collect deduplicated pharmacological class names from all ingredients.

    Reads the ``drug_classes`` key on each ingredient dict (populated by
    fetch_context from the DB).  Falls back gracefully to an empty list if
    the key is absent (older drug dicts or drugs with no class data).
    """
    seen: set[str] = set()
    names: list[str] = []
    for ing in drug.get("ingredients", []):
        for cls in ing.get("drug_classes", []):
            n = cls.get("class_name", "").strip()
            if n and n not in seen:
                seen.add(n)
                names.append(n)
    return names


# Words that are too generic to drive a useful class-level sentence search.
# Including them causes false-positive sentence matches (e.g. "inhibitors"
# would match almost any drug's label).
_GENERIC_TERMS = {
    "inhibitors", "antagonists", "agonists", "agents", "other", "various",
    "class", "combination", "combinations", "derivatives", "preparations",
    "activity", "action", "decreased", "increased", "local", "with",
}


def _extract_class_evidence(
    full_text: str, class_names: list[str], max_chars: int = 1000
) -> str:
    """Search *full_text* for sentences that mention any significant term from
    *class_names* and return them as a compact block.

    Used by fetch_context to surface class-level interaction warnings that
    are buried past the standard 2500-char FDA text truncation point.  Only
    semantically significant words from each class name drive the search
    (generic words like 'inhibitors' or 'agents' are excluded).

    Returns an empty string when no relevant sentences are found.
    """
    import re

    # Build a set of significant search terms from all class names
    search_terms: set[str] = set()
    stop_words = {"and", "with", "for", "in", "of", "to", "the"}
    
    for cls in class_names:
        words = cls.lower().split()
        
        # 1. Generate acronym for multi-word classes (e.g. "Selective serotonin reuptake inhibitors" -> "ssri")
        if len(words) > 1:
            acronym = "".join(w[0] for w in words if w.strip(",.;():") not in stop_words)
            if len(acronym) >= 3:
                search_terms.add(acronym)
                
        # 2. Add individual significant words with naive stemming
        for word in words:
            cleaned = word.strip(",.;():")
            if len(cleaned) > 4 and cleaned not in _GENERIC_TERMS:
                search_terms.add(cleaned)
                # Map 'serotonin' -> 'seroton' to match 'serotonergic'
                if cleaned.endswith("in"):
                    search_terms.add(cleaned[:-2])
                elif cleaned.endswith("ic"):
                    search_terms.add(cleaned[:-2])

    if not search_terms:
        return ""

    # Compile regex to match any of our search terms as a word prefix.
    # e.g., \b(?:ssri|seroton)\w*\b will match "SSRIs" and "serotonergic"
    pattern = r"\b(?:{})\w*\b".format("|".join(map(re.escape, search_terms)))
    search_regex = re.compile(pattern, re.IGNORECASE)

    # Split into sentences (rough but sufficient for this purpose)
    sentences = re.split(r"(?<=[.!?])\s+", full_text)

    seen: set[str] = set()
    relevant: list[str] = []
    total_chars = 0

    for sent in sentences:
        sent = sent.strip()
        if not sent or sent in seen:
            continue
        if search_regex.search(sent):
            seen.add(sent)
            relevant.append(sent)
            total_chars += len(sent) + 1
            if total_chars >= max_chars:
                break

    return " ".join(relevant) if relevant else ""


def _format_classes(class_names: list[str]) -> str:
    """Format class list for prompt injection."""
    if not class_names:
        return "None available"
    return "; ".join(class_names)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def fetch_context(state: InteractionState) -> dict:
    """Normalise and pre-compute FDA text and drug class lists from both drug dicts.

    No LLM call.  Exists for trace visibility -- every stage of the pipeline is
    inspectable as a discrete node output, including the raw FDA text blocks
    and class lists that will be fed into the LLM.
    """
    drug_a = state["drug_a"]
    drug_b = state["drug_b"]

    a_name = drug_a.get("brand_name", "Drug A")
    b_name = drug_b.get("brand_name", "Drug B")
    a_ingredients = [i.get("generic_name") for i in drug_a.get("ingredients", [])]
    b_ingredients = [i.get("generic_name") for i in drug_b.get("ingredients", [])]

    logger.info(
        "fetch_context | Drug A: %s (%d ingredients) | Drug B: %s (%d ingredients)",
        a_name, len(a_ingredients), b_name, len(b_ingredients),
    )

    a_raw_text = _collect_fda_text(drug_a, max_chars=999_999)
    b_raw_text = _collect_fda_text(drug_b, max_chars=999_999)
    a_raw_len = len(a_raw_text)  # measure before truncation
    b_raw_len = len(b_raw_text)
    
    a_fda = _collect_fda_text(drug_a)
    b_fda = _collect_fda_text(drug_b)

    a_classes = _collect_class_names(drug_a)
    b_classes = _collect_class_names(drug_b)

    # Class-level evidence extraction: search each drug's FULL (uncapped) FDA
    # text for sentences mentioning terms from the OTHER drug's class names.
    # This surfaces serotonergic/class-level warnings that are buried past the
    # 2500-char truncation point, without blowing up the token budget.
    b_class_evidence = _extract_class_evidence(b_raw_text, a_classes)  # Drug B mentions Drug A's classes?
    a_class_evidence = _extract_class_evidence(a_raw_text, b_classes)  # Drug A mentions Drug B's classes?

    if b_class_evidence:
        logger.info(
            "fetch_context | Class-level evidence found in Drug B's label (%d chars)",
            len(b_class_evidence),
        )
        b_fda = b_fda + f"\n\n[Class-Level Evidence from Drug B full label]\n{b_class_evidence}"
    if a_class_evidence:
        logger.info(
            "fetch_context | Class-level evidence found in Drug A's label (%d chars)",
            len(a_class_evidence),
        )
        a_fda = a_fda + f"\n\n[Class-Level Evidence from Drug A full label]\n{a_class_evidence}"

    logger.info(
        "fetch_context | Drug A FDA: %d chars (cap: %d) | Drug B FDA: %d chars (cap: %d)",
        a_raw_len, _MAX_FDA_TEXT_CHARS, b_raw_len, _MAX_FDA_TEXT_CHARS,
    )
    if a_raw_len > _MAX_FDA_TEXT_CHARS:
        logger.warning("Drug A FDA text truncated: %d -> %d chars.", a_raw_len, _MAX_FDA_TEXT_CHARS)
    if b_raw_len > _MAX_FDA_TEXT_CHARS:
        logger.warning("Drug B FDA text truncated: %d -> %d chars.", b_raw_len, _MAX_FDA_TEXT_CHARS)

    logger.info(
        "fetch_context | Drug A classes: %s | Drug B classes: %s",
        a_classes or "(none)", b_classes or "(none)",
    )

    return {
        "drug_a_fda_text": a_fda,
        "drug_b_fda_text": b_fda,
        "drug_a_classes": a_classes,
        "drug_b_classes": b_classes,
    }


def check_interaction(state: InteractionState) -> dict:
    """LLM call 1: search FDA label text for explicit interaction mentions.

    Now also checks class-level interactions: if Drug A's label warns about
    Drug B's pharmacological class (or vice versa), that is detected and
    returned with match_type="class_level".

    Uses structured output (InteractionCheckResult) so parsing is type-safe.
    The prompt explicitly forbids outside-knowledge reasoning.
    """
    drug_a_name = state["drug_a"].get("brand_name", "Drug A")
    drug_b_name = state["drug_b"].get("brand_name", "Drug B")

    logger.info("check_interaction | Checking: %s <-> %s", drug_a_name, drug_b_name)

    llm = _get_llm()
    chain = _build_interaction_chain(llm)

    result: InteractionCheckResult = chain.invoke({
        "drug_a_name": drug_a_name,
        "drug_b_name": drug_b_name,
        "drug_a_fda_text": state["drug_a_fda_text"] or "No FDA label data available.",
        "drug_b_fda_text": state["drug_b_fda_text"] or "No FDA label data available.",
        "drug_a_classes": _format_classes(state.get("drug_a_classes") or []),
        "drug_b_classes": _format_classes(state.get("drug_b_classes") or []),
    })

    logger.info(
        "check_interaction | interaction_found=%s | match_type=%s | claim=%s",
        result.interaction_found,
        result.match_type,
        (result.claim or "")[:80],
    )

    return {
        "interaction_claim": result.claim if result.interaction_found else None,
        "citation_text": result.citation if result.interaction_found else None,
        "match_type": result.match_type if result.interaction_found else None,
    }


def verify_groundedness(state: InteractionState) -> dict:
    """LLM call 2: verify the citation is genuinely present in the source text.

    For "class_level" matches, also verifies that the class attribution in the
    claim is correct by cross-checking against the class lists already in state
    (not just trusting the model's claim about what class a drug belongs to).

    Short-circuits (no LLM call) when check_interaction found no claim --
    there is nothing to verify, so is_grounded is set to False with a clear reason.
    """
    if not state.get("interaction_claim"):
        logger.info("verify_groundedness | No claim to verify -- skipping LLM call.")
        return {
            "is_grounded": False,
            "groundedness_reasoning": (
                "No interaction claim was found by check_interaction -- nothing to verify."
            ),
        }

    drug_a_name = state["drug_a"].get("brand_name", "Drug A")
    drug_b_name = state["drug_b"].get("brand_name", "Drug B")

    # Combine both FDA text blocks as the authoritative source for grounding.
    source_fda_text = "\n\n".join(filter(None, [
        state.get("drug_a_fda_text"),
        state.get("drug_b_fda_text"),
    ])) or "No FDA label data available."

    match_type = state.get("match_type") or "direct"

    logger.info(
        "verify_groundedness | Verifying citation for: %s <-> %s (match_type=%s)",
        drug_a_name, drug_b_name, match_type,
    )

    llm = _get_llm()
    chain = _build_groundedness_chain(llm)

    result: GroundednessCheckResult = chain.invoke({
        "drug_a_name": drug_a_name,
        "drug_b_name": drug_b_name,
        "interaction_claim": state["interaction_claim"],
        "citation_text": state["citation_text"],
        "source_fda_text": source_fda_text,
        "match_type": match_type,
        "drug_a_classes": _format_classes(state.get("drug_a_classes") or []),
        "drug_b_classes": _format_classes(state.get("drug_b_classes") or []),
    })

    logger.info(
        "verify_groundedness | is_grounded=%s | reasoning=%s",
        result.is_grounded,
        result.reasoning[:120],
    )

    return {
        "is_grounded": result.is_grounded,
        "groundedness_reasoning": result.reasoning,
    }


def format_grounded_answer(state: InteractionState) -> dict:
    """Assemble the final answer when a claim is verified and grounded."""
    drug_a_name = state["drug_a"].get("brand_name", "Drug A")
    drug_b_name = state["drug_b"].get("brand_name", "Drug B")
    match_type = state.get("match_type") or "direct"

    logger.info(
        "format_grounded_answer | interaction_found between %s and %s (match_type=%s)",
        drug_a_name, drug_b_name, match_type,
    )

    return {
        "final_status": "interaction_found",
        "final_answer": {
            "final_status": "interaction_found",
            "drug_a": drug_a_name,
            "drug_b": drug_b_name,
            "interaction_claim": state["interaction_claim"],
            "citation_text": state["citation_text"],
            "match_type": match_type,
            "is_grounded": True,
            "groundedness_reasoning": state.get("groundedness_reasoning"),
        },
    }


def format_unverifiable_answer(state: InteractionState) -> dict:
    """Assemble the final answer when no grounded interaction is found.

    Distinguishes two meaningfully different outcomes:
      - "none_found"    -- check_interaction found no mention in the FDA text at all.
      - "unverifiable"  -- check_interaction made a claim, but verify_groundedness
                          could not confirm the citation exists in the source text.
    These are NOT the same: one means the drugs may be safe together per FDA text;
    the other means the LLM produced a claim that cannot be verified.
    """
    drug_a_name = state["drug_a"].get("brand_name", "Drug A")
    drug_b_name = state["drug_b"].get("brand_name", "Drug B")

    # Distinguish the two cases explicitly
    if state.get("interaction_claim") is None:
        final_status = "none_found"
    else:
        final_status = "unverifiable"

    logger.info(
        "format_unverifiable_answer | final_status=%s for %s <-> %s",
        final_status, drug_a_name, drug_b_name,
    )

    return {
        "final_status": final_status,
        "final_answer": {
            "final_status": final_status,
            "drug_a": drug_a_name,
            "drug_b": drug_b_name,
            "interaction_claim": state.get("interaction_claim"),
            "citation_text": state.get("citation_text"),
            "match_type": state.get("match_type"),
            "is_grounded": state.get("is_grounded"),
            "groundedness_reasoning": state.get("groundedness_reasoning"),
        },
    }
