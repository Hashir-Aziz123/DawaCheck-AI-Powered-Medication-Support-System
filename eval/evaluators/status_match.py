"""
status_match evaluator — checks that run final_status matches the dataset label.

Evaluator signature: (outputs, reference_outputs) -> dict
Compatible with langsmith.evaluate() evaluators list.
"""


def status_match_evaluator(outputs: dict, reference_outputs: dict) -> dict:
    """Verify the pipeline's final_status matches the dataset's expected value.

    Returns:
        dict with keys: key, score (1=pass / 0=fail), comment (reasoning string)
    """
    actual = (outputs or {}).get("final_status", "")
    expected = (reference_outputs or {}).get("final_status", "")

    if not expected:
        return {
            "key": "status_match",
            "score": 1,
            "comment": "No expected final_status specified in dataset — skipping.",
        }

    passed = actual == expected
    if passed:
        comment = f"✅ final_status matches: '{actual}'."
    else:
        comment = (
            f"❌ final_status mismatch — expected '{expected}', got '{actual}'. "
            f"The pipeline classified the pair differently from the dataset label."
        )

    return {
        "key": "status_match",
        "score": int(passed),
        "comment": comment,
    }
