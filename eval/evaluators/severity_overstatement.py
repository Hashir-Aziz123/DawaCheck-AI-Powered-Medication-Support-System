"""
severity_overstatement evaluator — guards against LLMs inflating severity in claims.

Checks whether any term from the dataset's `forbidden_severity_terms` list appears
in the pipeline's `interaction_claim` but is ABSENT from the `citation_text`.

Rationale: if the claim says "serious bleeding risk" but the cited FDA sentence only
says "increased bleeding risk", the model has overstated severity beyond what the
source supports.  The citation is the direct grounding source — if the severity word
isn't in the citation, the claim has added it without evidence.

This evaluator directly encodes the known LLM behaviour observed for the
Mefenamic Acid + Warfarin pair and ensures it can never reappear silently.

Evaluator signature: (outputs, reference_outputs) -> dict
Compatible with langsmith.evaluate() evaluators list.
"""


def severity_overstatement_evaluator(outputs: dict, reference_outputs: dict) -> dict:
    """Fail if the claim contains forbidden severity terms absent from the citation.

    Args:
        outputs: The pipeline's run outputs (final_answer dict from run_interaction_check).
        reference_outputs: The dataset example's expected_output dict, which may
                           contain `forbidden_severity_terms: list[str]`.

    Returns:
        dict with keys: key, score (1=pass / 0=fail), comment (reasoning string)
    """
    outputs = outputs or {}
    reference_outputs = reference_outputs or {}

    forbidden_terms: list[str] = reference_outputs.get("forbidden_severity_terms", [])

    # Auto-pass for examples with no forbidden terms defined.
    if not forbidden_terms:
        return {
            "key": "severity_overstatement",
            "score": 1,
            "comment": "No forbidden severity terms specified for this example — check skipped.",
        }

    # If no interaction was claimed, there's no claim to overstate.
    claim_raw: str = outputs.get("interaction_claim") or ""
    citation_raw: str = outputs.get("citation_text") or ""

    if not claim_raw:
        return {
            "key": "severity_overstatement",
            "score": 1,
            "comment": (
                "No interaction_claim produced by the pipeline — "
                "no severity terms to evaluate."
            ),
        }

    claim_lower = claim_raw.lower()
    citation_lower = citation_raw.lower()

    # A term is "overstated" if it appears in the claim but NOT in the citation.
    # The citation is the direct grounding anchor — severity language not in the
    # citation has been introduced by the model without evidence.
    overstated = [
        term for term in forbidden_terms
        if term.lower() in claim_lower and term.lower() not in citation_lower
    ]

    if overstated:
        return {
            "key": "severity_overstatement",
            "score": 0,
            "comment": (
                f"❌ Claim contains severity term(s) {overstated} not present in citation. "
                f"This indicates severity overstatement beyond what the FDA source supports. "
                f"Claim: '{claim_raw}' | "
                f"Citation: '{citation_raw or '(none)'}'"
            ),
        }

    # All forbidden terms either absent from the claim or also present in the citation.
    terms_in_claim = [t for t in forbidden_terms if t.lower() in claim_lower]
    if terms_in_claim:
        comment = (
            f"✅ Term(s) {terms_in_claim} found in claim are also present in citation — "
            f"severity language is grounded in the source. "
            f"Checked: {forbidden_terms}."
        )
    else:
        comment = (
            f"✅ None of the forbidden severity terms {forbidden_terms} appear in the claim."
        )

    return {
        "key": "severity_overstatement",
        "score": 1,
        "comment": comment,
    }
