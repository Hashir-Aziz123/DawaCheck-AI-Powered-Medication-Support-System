"""
groundedness evaluator — verifies that the pipeline's citation is genuinely grounded.

Rather than re-running a separate LLM judge, this evaluator reads the pipeline's own
`is_grounded` field (set by the `verify_groundedness` node) and `groundedness_reasoning`
(the node's own explanation).  This avoids maintaining two divergent definitions of
"grounded" — the node and the evaluator share the same definition because the evaluator
reads the node's verdict directly.

The evaluator only fires when the dataset example has `requires_grounded: true`
(i.e. examples where an interaction_found result is expected and must be supported
by a real citation, not just a bare drug-name list).

Evaluator signature: (outputs, reference_outputs) -> dict
Compatible with langsmith.evaluate() evaluators list.
"""


def groundedness_evaluator(outputs: dict, reference_outputs: dict) -> dict:
    """Pass if the pipeline confirmed the citation as grounded (is_grounded=True).

    Short-circuits with a pass when requires_grounded is False in the dataset example.

    Args:
        outputs: The pipeline's run outputs (final_answer dict from run_interaction_check).
        reference_outputs: The dataset example's expected_output dict.

    Returns:
        dict with keys: key, score (1=pass / 0=fail), comment (reasoning string)
    """
    outputs = outputs or {}
    reference_outputs = reference_outputs or {}

    # Skip grounding check for examples that don't require it (e.g. none_found cases).
    if not reference_outputs.get("requires_grounded", False):
        return {
            "key": "groundedness",
            "score": 1,
            "comment": "Groundedness check not required for this example (requires_grounded=false).",
        }

    final_status = outputs.get("final_status", "")

    # If the pipeline returned none_found or error, grounding can't apply.
    if final_status in ("none_found", "error", ""):
        expected_status = reference_outputs.get("final_status", "interaction_found")
        return {
            "key": "groundedness",
            "score": 0,
            "comment": (
                f"❌ Pipeline returned final_status='{final_status}' — expected "
                f"'{expected_status}' with a grounded citation. "
                f"Cannot evaluate groundedness when no interaction was found."
            ),
        }

    is_grounded: bool | None = outputs.get("is_grounded")
    reasoning: str = outputs.get("groundedness_reasoning") or "(no reasoning returned by pipeline)"

    if is_grounded is True:
        return {
            "key": "groundedness",
            "score": 1,
            "comment": f"✅ Pipeline verified citation as grounded. Reasoning: {reasoning}",
        }

    # is_grounded=False or None means grounding failed or pipeline returned unverifiable.
    citation = outputs.get("citation_text") or "(none)"
    claim = outputs.get("interaction_claim") or "(none)"
    return {
        "key": "groundedness",
        "score": 0,
        "comment": (
            f"❌ Pipeline groundedness check failed (is_grounded={is_grounded}). "
            f"Reasoning: {reasoning} | "
            f"Claim: '{claim}' | Citation: '{citation}'"
        ),
    }
