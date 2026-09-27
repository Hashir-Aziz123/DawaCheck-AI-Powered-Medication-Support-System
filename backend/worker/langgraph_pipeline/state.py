"""
Shared state object that flows through every node in the interaction-checking pipeline.

Drug dict shape (produced by resolve_drug() and serialised by the caller):
    {
        "brand_name": str,
        "dosage_form": str | None,
        "ingredients": [
            {
                "generic_name": str,
                "dose": str | None,
                "rxcui": str | None,
                "drug_interactions": str | None,
                "warnings": str | None,
                "boxed_warning": str | None,
                "drug_classes": [{"class_name": str, "class_source": str}]  # may be absent
            }
        ]
    }
"""

from typing import TypedDict


class InteractionState(TypedDict):
    # -----------------------------------------------------------------------
    # Inputs -- provided by the caller before the graph starts
    # -----------------------------------------------------------------------
    drug_a: dict   # shape documented above
    drug_b: dict   # same shape

    # -----------------------------------------------------------------------
    # Set by fetch_context -- pre-computed for trace visibility and reuse
    # Concatenated FDA text from all ingredients of each drug.
    # -----------------------------------------------------------------------
    drug_a_fda_text: str | None
    drug_b_fda_text: str | None

    # Flat deduplicated list of pharmacological class names for each drug.
    # Fetched from the drug_classes DB table (or the ingredient dict if present).
    # Used by check_interaction to surface class-level warnings.
    drug_a_classes: list[str]   # e.g. ["Selective Serotonin Reuptake Inhibitors"]
    drug_b_classes: list[str]

    # -----------------------------------------------------------------------
    # Set by check_interaction
    # -----------------------------------------------------------------------
    interaction_claim: str | None   # plain-language description of the interaction
    citation_text: str | None       # exact quoted sentence(s) from the FDA source text
    match_type: str | None          # "direct" | "class_level" | None

    # -----------------------------------------------------------------------
    # Set by verify_groundedness
    # -----------------------------------------------------------------------
    is_grounded: bool | None
    groundedness_reasoning: str | None   # brief explanation -- useful for debugging/logging

    # -----------------------------------------------------------------------
    # Set by format_grounded_answer / format_unverifiable_answer
    # -----------------------------------------------------------------------
    # "interaction_found" -- claim made and grounded in FDA text
    # "none_found"        -- check_interaction found nothing in the text
    # "unverifiable"      -- claim was made but citation not found in source text
    final_status: str
    final_answer: dict | None
