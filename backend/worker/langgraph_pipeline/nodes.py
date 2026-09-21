"""
LangGraph node implementations for the drug interaction-checking pipeline.

Each node is a pure function: InteractionState → InteractionState (partial update).
LLM nodes use structured output (Pydantic models) so parsing is reliable and
type-safe, not brittle string-splitting.

LLM setup:
  Model: llama-3.3-70b-versatile (Groq)
  Temperature: 0.0 for both calls — deliberate choice.
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

# Load infra/.env — same pattern as core/db/session.py so GROQ_API_KEY is
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
    claim: str | None = Field(
        default=None,
        description=(
            "Plain-language description of the interaction (1–2 sentences). "
            "Required when interaction_found=True, null otherwise."
        ),
    )
    citation: str | None = Field(
        default=None,
        description=(
            "A complete, verbatim sentence or clause copied from the FDA label text "
            "that explicitly states the interaction or its effect (e.g., increased "
            "bleeding risk, reduced efficacy). "
            "MUST NOT be a bare list of drug names, a table heading, a section label, "
            "or a cross-reference — those do not qualify regardless of verbatim presence. "
            "Required when interaction_found=True, null otherwise."
        ),
    )


class GroundednessCheckResult(BaseModel):
    """Parsed output from the verify_groundedness LLM call."""
    is_grounded: bool = Field(
        description=(
            "True if the citation is present verbatim or as a faithful near-verbatim "
            "excerpt of the source FDA text."
        )
    )
    reasoning: str = Field(
        description="Brief explanation of the grounding verdict (1–3 sentences)."
    )


# ---------------------------------------------------------------------------
# LLM factory — isolated so it can be patched cleanly in tests
# ---------------------------------------------------------------------------

def _get_llm() -> ChatGroq:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Add it to infra/.env and restart."
        )
    return ChatGroq(
        model="openai/gpt-oss-120b",
        # Temperature 0.0 is deliberate — see module docstring.
        temperature=0.0,
        groq_api_key=api_key,
    )


# ---------------------------------------------------------------------------
# Chain builders — isolated so they can be patched cleanly in tests
# ---------------------------------------------------------------------------

def _build_interaction_chain(llm: ChatGroq):
    # method="json_mode" uses response_format={"type":"json_object"} instead of
    # function/tool calling — required for models that support JSON output but
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
# TPM limit means we must cap this. 2500 chars ≈ 625 tokens per drug — leaves
# enough headroom for system prompt + format instructions within an 8000-token limit.
_MAX_FDA_TEXT_CHARS = 2500


def _collect_fda_text(drug: dict, max_chars: int = _MAX_FDA_TEXT_CHARS) -> str:
    """Concatenate all FDA label fields from every ingredient into one text block.

    Includes drug_interactions, warnings, and boxed_warning.  Each section is
    labelled with the ingredient name and field so the LLM can distinguish sources.
    Returns a fallback string when no FDA data is present so the prompt is never
    silently empty.

    If the combined text exceeds max_chars, it is truncated and a notice appended
    so the LLM knows the text was cut — the most clinically relevant text (drug
    interactions, which comes first) is preserved over the tail of warnings.
    """
    parts = []
    for ing in drug.get("ingredients", []):
        name = ing.get("generic_name", "unknown ingredient")
        for field in ("drug_interactions", "warnings", "boxed_warning"):
            text = ing.get(field)
            if text and text.strip():
                label = field.replace("_", " ").title()
                parts.append(f"[{name} — {label}]\n{text.strip()}")
    if not parts:
        return "No FDA label data available for this drug."
    combined = "\n\n".join(parts)
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n\n[... FDA text truncated for length]"
    return combined

# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def fetch_context(state: InteractionState) -> dict:
    """Normalise and pre-compute FDA text from both drug dicts.

    No LLM call.  Exists for trace visibility — every stage of the pipeline is
    inspectable as a discrete node output, including the raw FDA text blocks
    that will be fed into the LLM.
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

    a_raw_len = len(_collect_fda_text(drug_a, max_chars=999_999))  # measure before truncation
    b_raw_len = len(_collect_fda_text(drug_b, max_chars=999_999))
    a_fda = _collect_fda_text(drug_a)
    b_fda = _collect_fda_text(drug_b)

    logger.info(
        "fetch_context | Drug A FDA: %d chars (cap: %d) | Drug B FDA: %d chars (cap: %d)",
        a_raw_len, _MAX_FDA_TEXT_CHARS, b_raw_len, _MAX_FDA_TEXT_CHARS,
    )
    if a_raw_len > _MAX_FDA_TEXT_CHARS:
        logger.warning("Drug A FDA text truncated: %d → %d chars.", a_raw_len, _MAX_FDA_TEXT_CHARS)
    if b_raw_len > _MAX_FDA_TEXT_CHARS:
        logger.warning("Drug B FDA text truncated: %d → %d chars.", b_raw_len, _MAX_FDA_TEXT_CHARS)

    return {
        "drug_a_fda_text": a_fda,
        "drug_b_fda_text": b_fda,
    }


def check_interaction(state: InteractionState) -> dict:
    """LLM call 1: search FDA label text for explicit interaction mentions.

    Uses structured output (InteractionCheckResult) so parsing is type-safe.
    The prompt explicitly forbids outside-knowledge reasoning.
    """
    drug_a_name = state["drug_a"].get("brand_name", "Drug A")
    drug_b_name = state["drug_b"].get("brand_name", "Drug B")

    logger.info("check_interaction | Checking: %s ↔ %s", drug_a_name, drug_b_name)

    llm = _get_llm()
    chain = _build_interaction_chain(llm)

    result: InteractionCheckResult = chain.invoke({
        "drug_a_name": drug_a_name,
        "drug_b_name": drug_b_name,
        "drug_a_fda_text": state["drug_a_fda_text"] or "No FDA label data available.",
        "drug_b_fda_text": state["drug_b_fda_text"] or "No FDA label data available.",
    })

    logger.info(
        "check_interaction | interaction_found=%s | claim=%s",
        result.interaction_found,
        (result.claim or "")[:80],
    )

    return {
        "interaction_claim": result.claim if result.interaction_found else None,
        "citation_text": result.citation if result.interaction_found else None,
    }


def verify_groundedness(state: InteractionState) -> dict:
    """LLM call 2: verify the citation is genuinely present in the source text.

    This is a completely separate LLM call from check_interaction — it does not
    ask the model to re-evaluate the medical claim, only to confirm whether the
    quoted sentence exists in the provided source text.

    Short-circuits (no LLM call) when check_interaction found no claim —
    there is nothing to verify, so is_grounded is set to False with a clear reason.
    """
    if not state.get("interaction_claim"):
        logger.info("verify_groundedness | No claim to verify — skipping LLM call.")
        return {
            "is_grounded": False,
            "groundedness_reasoning": (
                "No interaction claim was found by check_interaction — nothing to verify."
            ),
        }

    drug_a_name = state["drug_a"].get("brand_name", "Drug A")
    drug_b_name = state["drug_b"].get("brand_name", "Drug B")

    # Combine both FDA text blocks as the authoritative source for grounding.
    source_fda_text = "\n\n".join(filter(None, [
        state.get("drug_a_fda_text"),
        state.get("drug_b_fda_text"),
    ])) or "No FDA label data available."

    logger.info("verify_groundedness | Verifying citation for: %s ↔ %s", drug_a_name, drug_b_name)

    llm = _get_llm()
    chain = _build_groundedness_chain(llm)

    result: GroundednessCheckResult = chain.invoke({
        "drug_a_name": drug_a_name,
        "drug_b_name": drug_b_name,
        "interaction_claim": state["interaction_claim"],
        "citation_text": state["citation_text"],
        "source_fda_text": source_fda_text,
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

    logger.info(
        "format_grounded_answer | interaction_found between %s and %s",
        drug_a_name, drug_b_name,
    )

    return {
        "final_status": "interaction_found",
        "final_answer": {
            "final_status": "interaction_found",
            "drug_a": drug_a_name,
            "drug_b": drug_b_name,
            "interaction_claim": state["interaction_claim"],
            "citation_text": state["citation_text"],
            "is_grounded": True,
            "groundedness_reasoning": state.get("groundedness_reasoning"),
        },
    }


def format_unverifiable_answer(state: InteractionState) -> dict:
    """Assemble the final answer when no grounded interaction is found.

    Distinguishes two meaningfully different outcomes:
      - "none_found"    — check_interaction found no mention in the FDA text at all.
      - "unverifiable"  — check_interaction made a claim, but verify_groundedness
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
        "format_unverifiable_answer | final_status=%s for %s ↔ %s",
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
            "is_grounded": state.get("is_grounded"),
            "groundedness_reasoning": state.get("groundedness_reasoning"),
        },
    }
