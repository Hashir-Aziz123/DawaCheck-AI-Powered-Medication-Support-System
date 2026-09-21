"""
Run the full evaluation suite against the LangSmith dataset interaction-check-eval-v1.

For each example in the dataset this script:
  1. Resolves the two drug names against the local DB.
  2. Calls run_interaction_check() (the real LangGraph pipeline).
  3. Runs three evaluators: status_match, groundedness, severity_overstatement.
  4. Submits results to LangSmith and prints a pass/fail table to stdout.

Usage (from e:\\mediAid_langraph\\backend\\):
    python scripts/run_eval.py

Requires:
  - DB reachable (same as test_interaction_check.py)
  - LANGCHAIN_API_KEY set in infra/.env
  - GROQ_API_KEY set in infra/.env
"""

import re
import sys
import logging
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — mirrors test_interaction_check.py exactly.
# ---------------------------------------------------------------------------
_BACKEND_DIR = Path(__file__).resolve().parent.parent   # .../backend/
_ROOT_DIR = _BACKEND_DIR.parent                          # .../mediAid_langraph/

if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from dotenv import load_dotenv
load_dotenv(dotenv_path=_ROOT_DIR / "infra" / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.db.session import DATABASE_URL
from app.services.drug_resolution import resolve_drug
from app.schemas.resolve import DrugResolutionResult
from worker.langgraph_pipeline.graph import run_interaction_check

from eval.evaluators import (
    groundedness_evaluator,
    severity_overstatement_evaluator,
    status_match_evaluator,
)

# ---------------------------------------------------------------------------
# Drug resolution helpers — identical to test_interaction_check.py
# ---------------------------------------------------------------------------

def _drug_result_to_dict(result: DrugResolutionResult) -> dict:
    return {
        "brand_name": result.brand_name,
        "dosage_form": result.dosage_form,
        "ingredients": [ing.model_dump() for ing in result.ingredients],
    }


def _resolve_to_dict(query: str, session: Session) -> dict | None:
    result = resolve_drug(query, session)
    if result.status not in ("found",):
        logger.warning("Could not resolve '%s': status=%s", query, result.status)
        return None
    return _drug_result_to_dict(result)


# ---------------------------------------------------------------------------
# Pipeline target — called once per dataset example by langsmith.evaluate()
# ---------------------------------------------------------------------------

def _make_target(session: Session):
    """Return a closure that resolves drugs and runs the pipeline for one example."""

    def target(inputs: dict) -> dict:
        drug_a_name = inputs.get("drug_a_name", "")
        drug_b_name = inputs.get("drug_b_name", "")

        drug_a = _resolve_to_dict(drug_a_name, session)
        drug_b = _resolve_to_dict(drug_b_name, session)

        if not drug_a or not drug_b:
            logger.error(
                "Resolution failed for '%s' or '%s' — returning error sentinel.",
                drug_a_name, drug_b_name,
            )
            return {
                "final_status": "error",
                "drug_a": drug_a_name,
                "drug_b": drug_b_name,
                "interaction_claim": None,
                "citation_text": None,
                "is_grounded": False,
                "groundedness_reasoning": "Drug resolution failed — could not run pipeline.",
            }

        return run_interaction_check(drug_a, drug_b)

    return target


# ---------------------------------------------------------------------------
# Results printer
# ---------------------------------------------------------------------------

def _print_results(results) -> None:
    """Print a human-readable pass/fail table from the evaluate() results."""
    sep = "=" * 72
    print(f"\n{sep}")
    print("  EVALUATION RESULTS — interaction-check-eval-v1")
    print(sep)

    total = 0
    passed = 0

    for result in results:
        inputs = result.get("run") and result["run"].inputs or {}
        example_inputs = result.get("example") and result["example"].inputs or inputs

        drug_a = example_inputs.get("drug_a_name", "?")
        drug_b = example_inputs.get("drug_b_name", "?")
        print(f"\n  {drug_a} ↔ {drug_b}")
        print(f"  {'─' * 60}")

        eval_results = result.get("evaluation_results", {})
        for er in (eval_results.get("results") or []):
            total += 1
            score = er.score if er.score is not None else 0
            if score == 1:
                passed += 1
                icon = "✅"
            else:
                icon = "❌"
            print(f"    {icon} {er.key:<28} score={score}")
            if er.comment:
                # Indent comment lines for readability
                for line in er.comment.split(" | "):
                    print(f"       {line.strip()}")

    print(f"\n{sep}")
    print(f"  Summary: {passed}/{total} checks passed")
    print(sep)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

DATASET_NAME = "interaction-check-eval-v1"
EVALUATORS = [
    status_match_evaluator,
    groundedness_evaluator,
    severity_overstatement_evaluator,
]


def main() -> None:
    try:
        from langsmith import evaluate
    except ImportError:
        logger.error("langsmith is not installed. Run: pip install langsmith")
        sys.exit(1)

    sync_url = re.sub(r"^postgresql\+asyncpg://", "postgresql://", DATABASE_URL)
    engine = create_engine(sync_url)

    with Session(engine) as session:
        target = _make_target(session)

        logger.info("Starting evaluation on dataset '%s'...", DATASET_NAME)

        results = evaluate(
            target,
            data=DATASET_NAME,
            evaluators=EVALUATORS,
            experiment_prefix="interaction-check-eval",
            # Sequential execution: avoids DB connection contention and keeps
            # Groq API calls easy to follow in the console logs.
            max_concurrency=1,
        )

    _print_results(results)

    # LangSmith prints the experiment URL automatically — confirm it here.
    print(
        "\nView the full eval run in LangSmith (URL printed above by langsmith).\n"
    )


if __name__ == "__main__":
    main()
