"""
LangGraph StateGraph wiring for the drug interaction-checking pipeline.

Graph flow:
    fetch_context → check_interaction → verify_groundedness
                                              │
                              ┌───────────────┴────────────────┐
                        is_grounded=True              is_grounded=False
                              │                                 │
                 format_grounded_answer        format_unverifiable_answer
                              │                                 │
                             END                               END

No retry loop — a failed grounding check produces "unverifiable" and terminates.
Silently retrying until grounding passes would undermine the purpose of verification.

Public entry point:
    run_interaction_check(drug_a: dict, drug_b: dict) -> dict
"""

import logging

from langgraph.graph import END, StateGraph

from worker.langgraph_pipeline.nodes import (
    check_interaction,
    fetch_context,
    format_grounded_answer,
    format_unverifiable_answer,
    verify_groundedness,
)
from worker.langgraph_pipeline.state import InteractionState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conditional routing
# ---------------------------------------------------------------------------

def _route_after_verification(state: InteractionState) -> str:
    """Route to the appropriate formatting node based on groundedness verdict."""
    if state.get("is_grounded"):
        return "format_grounded_answer"
    return "format_unverifiable_answer"


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

_builder = StateGraph(InteractionState)

_builder.add_node("fetch_context", fetch_context)
_builder.add_node("check_interaction", check_interaction)
_builder.add_node("verify_groundedness", verify_groundedness)
_builder.add_node("format_grounded_answer", format_grounded_answer)
_builder.add_node("format_unverifiable_answer", format_unverifiable_answer)

_builder.set_entry_point("fetch_context")
_builder.add_edge("fetch_context", "check_interaction")
_builder.add_edge("check_interaction", "verify_groundedness")
_builder.add_conditional_edges(
    "verify_groundedness",
    _route_after_verification,
    {
        "format_grounded_answer": "format_grounded_answer",
        "format_unverifiable_answer": "format_unverifiable_answer",
    },
)
_builder.add_edge("format_grounded_answer", END)
_builder.add_edge("format_unverifiable_answer", END)

_pipeline = _builder.compile()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_interaction_check(drug_a: dict, drug_b: dict) -> dict:
    """Check for interactions between two resolved drugs.

    Args:
        drug_a: Drug dict from resolve_drug(), serialised as a plain dict.
                Shape: {brand_name, dosage_form, ingredients: [{generic_name, dose,
                rxcui, drug_interactions, warnings, boxed_warning}]}
        drug_b: Same shape as drug_a.

    Returns:
        final_answer dict with keys:
            final_status  -- "interaction_found" | "none_found" | "unverifiable"
            drug_a        -- brand name string
            drug_b        -- brand name string
            interaction_claim  -- str | None
            citation_text      -- str | None
            match_type         -- "direct" | "class_level" | None
            is_grounded        -- bool | None
            groundedness_reasoning -- str | None
    """
    a_name = drug_a.get("brand_name", "Drug A")
    b_name = drug_b.get("brand_name", "Drug B")
    logger.info("run_interaction_check | Starting: %s ↔ %s", a_name, b_name)

    initial_state: InteractionState = {
        "drug_a": drug_a,
        "drug_b": drug_b,
        "drug_a_fda_text": None,
        "drug_b_fda_text": None,
        "drug_a_classes": [],
        "drug_b_classes": [],
        "interaction_claim": None,
        "citation_text": None,
        "match_type": None,
        "is_grounded": None,
        "groundedness_reasoning": None,
        "final_status": "",
        "final_answer": None,
    }

    # Pass run_name + metadata as a LangChain RunnableConfig so LangSmith
    # groups all node spans under one named trace and shows the drug pair in
    # the UI without opening individual spans.  This is the only change needed
    # for tracing — langchain-groq LLM calls are auto-instrumented once
    # LANGCHAIN_TRACING_V2=true is set in the environment.
    run_config = {
        "run_name": f"interaction_check | {a_name} \u2194 {b_name}",
        "metadata": {
            "drug_a": a_name,
            "drug_b": b_name,
        },
        "tags": ["interaction_check"],
    }
    final_state = _pipeline.invoke(initial_state, run_config)

    logger.info(
        "run_interaction_check | Done: %s \u2194 %s | status=%s",
        a_name, b_name, final_state.get("final_status"),
    )

    return final_state["final_answer"]
