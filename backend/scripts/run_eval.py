"""
Run the full evaluation suite against the LangSmith dataset interaction-check-eval-v1.

For each example in the dataset this script:
  1. Resolves the two drug names against the local DB.
  2. Calls run_interaction_check() (the real LangGraph pipeline).
  3. Runs three evaluators: status_match, groundedness, severity_overstatement.
  4. Submits results to LangSmith and prints an aggregate pass-rate table to stdout.

Usage (from e:\\mediAid_langraph\\backend\\):
    python scripts/run_eval.py
    python scripts/run_eval.py --verbose   # also print per-pair detail

Requires:
  - DB reachable (same as test_interaction_check.py)
  - LANGCHAIN_API_KEY set in infra/.env
  - GROQ_API_KEY set in infra/.env
"""

import argparse
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

def _print_results(results, verbose: bool = False) -> None:
    """
    Print evaluation results in a readable format.

    Default: aggregate pass-rate table + list of failing pairs per evaluator.
    --verbose: also print every individual pair's scores (original behaviour).
    """
    sep = "=" * 72

    # Collect stats in one pass
    from collections import defaultdict

    # per_evaluator: {key: {"passed": int, "failed": int, "failures": [(drug_a, drug_b, comment)]}}
    per_evaluator: dict = defaultdict(lambda: {"passed": 0, "failed": 0, "failures": []})
    total_checks = 0
    total_passed = 0
    total_examples = 0
    resolution_errors = 0

    per_pair_lines: list[str] = []

    for result in results:
        total_examples += 1
        inputs = result.get("run") and result["run"].inputs or {}
        example_inputs = result.get("example") and result["example"].inputs or inputs

        drug_a = example_inputs.get("drug_a_name", "?")
        drug_b = example_inputs.get("drug_b_name", "?")
        pair_label = f"{drug_a} ↔ {drug_b}"

        # Detect resolution errors (pipeline returned final_status="error")
        run_outputs = result.get("run") and result["run"].outputs or {}
        if (run_outputs or {}).get("final_status") == "error":
            resolution_errors += 1

        pair_lines = [f"\n  {pair_label}", f"  {'─' * 60}"]
        eval_results = result.get("evaluation_results", {})

        for er in (eval_results.get("results") or []):
            total_checks += 1
            score = er.score if er.score is not None else 0
            passed = score == 1

            if passed:
                total_passed += 1
                per_evaluator[er.key]["passed"] += 1
                icon = "✅"
            else:
                per_evaluator[er.key]["failed"] += 1
                per_evaluator[er.key]["failures"].append(
                    (drug_a, drug_b, er.comment or "")
                )
                icon = "❌"

            pair_lines.append(f"    {icon} {er.key:<28} score={score}")
            if er.comment:
                for line in er.comment.split(" | "):
                    pair_lines.append(f"       {line.strip()}")

        per_pair_lines.extend(pair_lines)

    # --- Aggregate summary ---
    print(f"\n{sep}")
    print(f"  EVALUATION RESULTS — interaction-check-eval-v1")
    print(sep)
    print(f"  Examples run: {total_examples}")
    if resolution_errors:
        print(f"  ⚠️  Drug-resolution errors (pipeline returned 'error'): {resolution_errors}")
    print()

    # Per-evaluator breakdown table
    print(f"  {'Evaluator':<30} {'Pass':>5}  {'Fail':>5}  {'Pass rate':>10}")
    print(f"  {'─'*30}  {'─'*5}  {'─'*5}  {'─'*10}")
    all_evaluator_keys = list(per_evaluator.keys())
    for key in all_evaluator_keys:
        stats = per_evaluator[key]
        total_for_key = stats["passed"] + stats["failed"]
        rate = (stats["passed"] / total_for_key * 100) if total_for_key else 0
        print(f"  {key:<30} {stats['passed']:>5}  {stats['failed']:>5}  {rate:>9.1f}%")

    overall_rate = (total_passed / total_checks * 100) if total_checks else 0
    print(f"  {'─'*30}  {'─'*5}  {'─'*5}  {'─'*10}")
    print(f"  {'TOTAL':<30} {total_passed:>5}  {total_checks - total_passed:>5}  {overall_rate:>9.1f}%")
    print()

    # Failing pairs per evaluator
    any_failures = any(per_evaluator[k]["failures"] for k in all_evaluator_keys)
    if any_failures:
        print(f"  Failures by evaluator:")
        for key in all_evaluator_keys:
            failures = per_evaluator[key]["failures"]
            if not failures:
                continue
            print(f"\n  [{key}] — {len(failures)} failure(s):")
            for drug_a, drug_b, comment in failures:
                print(f"    ❌ {drug_a} ↔ {drug_b}")
                if comment:
                    # Print first 120 chars of comment to keep it readable
                    short = comment[:120] + ("…" if len(comment) > 120 else "")
                    print(f"       {short}")
    else:
        print("  ✅ All checks passed across all examples.")

    print(f"\n{sep}")
    print(f"  Summary: {total_passed}/{total_checks} checks passed ({overall_rate:.1f}%)")
    print(sep)

    # Per-pair detail (verbose mode only)
    if verbose:
        print(f"\n{'─'*72}")
        print("  PER-PAIR DETAIL (--verbose)")
        print(f"{'─'*72}")
        for line in per_pair_lines:
            print(line)
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

DATASET_NAME = "interaction-check-eval-v1"
EVALUATORS = [
    status_match_evaluator,
    groundedness_evaluator,
    severity_overstatement_evaluator,
]


def main(verbose: bool = False) -> None:
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

    _print_results(results, verbose=verbose)

    # LangSmith prints the experiment URL automatically — confirm it here.
    print(
        "\nView the full eval run in LangSmith (URL printed above by langsmith).\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the full evaluation suite against interaction-check-eval-v1."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Print per-pair scores for every example in addition to the "
            "aggregate summary table.  Default is summary + failures only."
        ),
    )
    args = parser.parse_args()
    main(verbose=args.verbose)
