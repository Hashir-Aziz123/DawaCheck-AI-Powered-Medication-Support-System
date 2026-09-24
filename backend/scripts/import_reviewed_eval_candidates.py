"""
import_reviewed_eval_candidates.py
===================================
Part D of the LLM-assisted eval dataset pipeline.

Reads eval_candidates_raw.csv and imports accepted rows into the LangSmith
dataset interaction-check-eval-v1.

Two operating modes
-------------------
  Default (human-review mode):
    your_decision column is required.  Rows with blank your_decision are
    skipped as unreviewed.
      "approve" — use proposed_* fields as-is
      "edit"    — use corrected_* fields; fall back to proposed_* for any blank
                  corrected_* field (never imports an empty value)
      "reject"  — skip entirely
      blank     — skip (treat as unreviewed)

  --auto-approve (AI-decision mode):
    Skips the review step entirely.  Every row is accepted using its
    proposed_* fields, EXCEPT:
      - Rows whose proposed_final_status is not a valid pipeline status
        (e.g. the LLM returned "error" or left the field blank).
      - Rows with an explicit your_decision = "reject" (honouring any
        partial manual overrides already written into the file).
    Rows with explicit your_decision = "approve" or "edit" are still
    handled by their normal logic so the two modes are compatible.

The script EXTENDS the existing dataset by default — the 4 original seed
examples are never touched.  Use --fresh-start --confirm to wipe and rebuild
from scratch (this is a destructive, confirmed action).

Usage (from backend/)
---------------------
    # Human-review mode (your_decision column must be filled)
    python scripts/import_reviewed_eval_candidates.py

    # AI-decision mode — accept all LLM proposed_* values without review
    python scripts/import_reviewed_eval_candidates.py --auto-approve

    # Other options
    python scripts/import_reviewed_eval_candidates.py --csv path/to/other.csv
    python scripts/import_reviewed_eval_candidates.py --fresh-start --confirm

Environment
-----------
    DATABASE_URL      — via core/db/session.py (loads infra/.env)
    LANGCHAIN_API_KEY — via infra/.env
    LANGCHAIN_ENDPOINT — via infra/.env (optional, defaults to api.smith.langchain.com)
"""

import argparse
import csv
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path / env setup — mirrors build_eval_dataset.py exactly
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
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATASET_NAME = "interaction-check-eval-v1"
DEFAULT_CSV  = _BACKEND_DIR / "pipeline_outputs" / "eval_candidates_raw.csv"

# Status values the pipeline and evaluators understand
VALID_FINAL_STATUSES = {"interaction_found", "none_found", "unverifiable"}


# ---------------------------------------------------------------------------
# Row processing helpers
# ---------------------------------------------------------------------------

def _resolve_fields(row: dict, auto_approve: bool = False) -> dict | None:
    """
    Resolve the effective claim/citation/status for a row.

    Normal mode (auto_approve=False):
      - "approve" → proposed_* fields
      - "edit"    → corrected_* fields, falling back to proposed_* when blank
      - anything else (blank, reject, unknown) → None (skip)

    Auto-approve mode (auto_approve=True):
      - blank your_decision → treat as "approve" (use proposed_* fields)
      - explicit "approve"  → same as normal approve
      - explicit "edit"     → same as normal edit
      - explicit "reject"   → None (skip, honouring manual overrides)
      - proposed_final_status not in VALID_FINAL_STATUSES → None (skip;
        this filters out LLM errors and blank-status rows automatically)

    Returns a dict with keys: final_status, claim, citation, pair_id,
    drug_a_brand, drug_b_brand, notes, your_decision.
    Returns None if the row should be skipped.
    """
    decision = row.get("your_decision", "").strip().lower()

    # In auto-approve mode, treat blank as approve
    if auto_approve and not decision:
        decision = "approve"

    if decision not in ("approve", "edit"):
        return None

    if decision == "approve":
        final_status = row.get("proposed_final_status", "").strip()
        claim = row.get("proposed_claim", "").strip()
        citation = row.get("proposed_citation", "").strip()
    else:  # edit
        # Use corrected_* fields; fall back to proposed_* when blank
        final_status = (
            row.get("corrected_status", "").strip()
            or row.get("proposed_final_status", "").strip()
        )
        claim = (
            row.get("corrected_claim", "").strip()
            or row.get("proposed_claim", "").strip()
        )
        citation = (
            row.get("corrected_citation", "").strip()
            or row.get("proposed_citation", "").strip()
        )

    if not final_status:
        logger.warning(
            "Row %s has decision='%s' but final_status is empty — skipping.",
            row.get("pair_id"), decision,
        )
        return None

    if final_status not in VALID_FINAL_STATUSES:
        logger.warning(
            "Row %s has proposed_final_status='%s' (not a valid pipeline status) "
            "— skipping.  This row likely had an LLM error during generation.",
            row.get("pair_id"), final_status,
        )
        return None

    return {
        "pair_id": row.get("pair_id", ""),
        "drug_a_brand": row.get("drug_a_brand", ""),
        "drug_b_brand": row.get("drug_b_brand", ""),
        "final_status": final_status,
        "claim": claim,
        "citation": citation,
        "notes": row.get("notes", "").strip(),
        "your_decision": decision,
    }


def _build_example(resolved: dict) -> tuple[dict, dict, dict]:
    """
    Build the (inputs, outputs, metadata) triple for a LangSmith example,
    matching the format established in eval/datasets/interaction_pairs.json.

    inputs:
        drug_a_name, drug_b_name

    outputs:
        final_status              — "interaction_found" | "none_found" | "unverifiable"
        requires_grounded         — True when final_status == "interaction_found"
        forbidden_severity_terms  — [] (reviewer edits the notes field if needed;
                                        severity terms are not auto-inferred here)
        citation_must_state_effect — True when final_status == "interaction_found"

    metadata:
        notes, source, pair_id, your_decision
    """
    final_status = resolved["final_status"]
    is_interaction = final_status == "interaction_found"

    inputs = {
        "drug_a_name": resolved["drug_a_brand"],
        "drug_b_name": resolved["drug_b_brand"],
    }
    outputs = {
        "final_status": final_status,
        "requires_grounded": is_interaction,
        "forbidden_severity_terms": [],
        "citation_must_state_effect": is_interaction,
    }
    metadata = {
        "notes": resolved["notes"],
        "source": "generated",
        "pair_id": resolved["pair_id"],
        "your_decision": resolved["your_decision"],
    }
    return inputs, outputs, metadata


# ---------------------------------------------------------------------------
# LangSmith client setup — mirrors build_eval_dataset.py
# ---------------------------------------------------------------------------

def _get_langsmith_client():
    """
    Build a LangSmith Client using keys from infra/.env.
    Exits with a clear message if the key is missing or a placeholder.
    """
    import os
    try:
        from langsmith import Client
    except ImportError:
        logger.error("langsmith is not installed.  Run: pip install langsmith")
        sys.exit(1)

    api_key = os.environ.get("LANGCHAIN_API_KEY", "")
    if not api_key or api_key == "your_langsmith_api_key_here":
        logger.error(
            "LANGCHAIN_API_KEY is not set or still a placeholder.\n"
            "  Add your real key to infra/.env "
            "(get it from https://smith.langchain.com/settings)."
        )
        sys.exit(1)

    api_url = os.environ.get(
        "LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com"
    )
    logger.info(
        "LangSmith key: %s...%s (len=%d) | endpoint: %s",
        api_key[:12], api_key[-6:], len(api_key), api_url,
    )
    return Client(api_key=api_key, api_url=api_url)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(csv_path: Path, fresh_start: bool, confirmed: bool, auto_approve: bool = False) -> None:
    # --- Guard: fresh-start requires --confirm ---
    if fresh_start and not confirmed:
        print(
            "\n[ERROR] --fresh-start will DELETE all existing examples in "
            f"'{DATASET_NAME}', including the 4 original seed examples.\n"
            "  Re-run with both --fresh-start AND --confirm to proceed.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Read and parse the CSV ---
    if not csv_path.exists():
        logger.error("CSV file not found: %s", csv_path)
        sys.exit(1)

    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        all_rows = list(reader)

    logger.info("Read %d rows from %s.", len(all_rows), csv_path.name)
    if auto_approve:
        logger.info(
            "--auto-approve: blank your_decision rows will be accepted using "
            "proposed_* fields.  Only LLM-error rows and explicit rejects are skipped."
        )

    # --- Tally review decisions ---
    counts = {"approve": 0, "edit": 0, "reject": 0, "blank": 0, "invalid": 0, "llm_error": 0}
    resolved_rows: list[dict] = []

    for row in all_rows:
        decision = row.get("your_decision", "").strip().lower()

        # Explicit reject always skips, regardless of mode
        if decision == "reject":
            counts["reject"] += 1
            continue

        if not decision:
            if not auto_approve:
                # Normal mode: blank = unreviewed, skip
                counts["blank"] += 1
                continue
            # auto_approve: blank → will be treated as approve inside _resolve_fields

        if decision and decision not in ("approve", "edit", ""):
            counts["invalid"] += 1
            logger.warning(
                "Row %s has unrecognised your_decision='%s' — skipping.",
                row.get("pair_id"), decision,
            )
            continue

        resolved = _resolve_fields(row, auto_approve=auto_approve)
        if resolved is None:
            # _resolve_fields logs the reason; count as llm_error when proposed status
            # is invalid (most common case in auto-approve mode)
            proposed_status = row.get("proposed_final_status", "").strip()
            if proposed_status and proposed_status not in VALID_FINAL_STATUSES:
                counts["llm_error"] += 1
            else:
                counts["blank"] += 1
            continue

        effective_decision = resolved["your_decision"]
        if effective_decision in counts:
            counts[effective_decision] += 1
        else:
            counts["approve"] += 1  # auto-approved blank rows land here
        resolved_rows.append(resolved)

    total_to_push = len(resolved_rows)
    logger.info(
        "Decision summary — approve: %d | edit: %d | reject: %d | "
        "blank/unreviewed: %d | llm_error_skipped: %d",
        counts["approve"], counts["edit"], counts["reject"],
        counts["blank"] + counts["invalid"], counts["llm_error"],
    )
    logger.info("Will push %d examples to LangSmith.", total_to_push)

    if total_to_push == 0:
        if auto_approve:
            print("\nNo rows to push — all rows were either rejected or had LLM errors.\n")
        else:
            print("\nNo rows to push — all rows were rejected or left blank.")
            print(
                "Fill in the your_decision column (approve / edit / reject) and re-run,"
                " or use --auto-approve to accept all LLM proposed_* values.\n"
            )
        return

    # --- Connect to LangSmith ---
    client = _get_langsmith_client()

    # --- Get or create the dataset ---
    try:
        existing_datasets = list(client.list_datasets(dataset_name=DATASET_NAME))
    except Exception as exc:
        logger.error("LangSmith API error: %s", exc)
        sys.exit(1)

    if fresh_start and existing_datasets:
        logger.warning(
            "--fresh-start --confirm: deleting existing dataset '%s'.", DATASET_NAME,
        )
        client.delete_dataset(dataset_id=existing_datasets[0].id)
        existing_datasets = []

    if existing_datasets:
        dataset = existing_datasets[0]
        before_count = len(list(client.list_examples(dataset_id=dataset.id)))
        logger.info(
            "Extending existing dataset '%s' (currently %d examples).",
            DATASET_NAME, before_count,
        )
    else:
        logger.info("Creating new dataset '%s'...", DATASET_NAME)
        dataset = client.create_dataset(
            dataset_name=DATASET_NAME,
            description=(
                "Drug interaction pipeline evaluation dataset. "
                "Seeded with 4 hand-labeled examples; extended via "
                "import_reviewed_eval_candidates.py with LLM-drafted, "
                "human-reviewed candidates."
            ),
        )
        before_count = 0
        logger.info("Dataset created: %s", dataset.id)

    # --- Push examples ---
    pushed = 0
    errors = 0
    for resolved in resolved_rows:
        inputs, outputs, metadata = _build_example(resolved)
        try:
            client.create_example(
                inputs=inputs,
                outputs=outputs,
                metadata=metadata,
                dataset_id=dataset.id,
            )
            logger.info(
                "  Pushed [%s]: %s ↔ %s  [%s]",
                resolved["pair_id"],
                resolved["drug_a_brand"],
                resolved["drug_b_brand"],
                outputs["final_status"],
            )
            pushed += 1
        except Exception as exc:
            logger.error(
                "  Failed to push [%s]: %s", resolved["pair_id"], exc,
            )
            errors += 1

    # --- Final count ---
    after_count = len(list(client.list_examples(dataset_id=dataset.id)))

    # --- Summary ---
    mode_label = "AI auto-approve" if auto_approve else "Human review"
    print(f"\n{'='*62}")
    print(f"  import_reviewed_eval_candidates — Summary ({mode_label})")
    print(f"{'='*62}")
    print(f"  CSV rows read:                    {len(all_rows)}")
    print(f"  Accepted — proposed_* (approve):  {counts['approve']}")
    print(f"  Accepted — corrected_* (edit):    {counts['edit']}")
    print(f"  Rejected (explicit):              {counts['reject']}")
    print(f"  Skipped — LLM error / bad status: {counts['llm_error']}")
    print(f"  Skipped — blank/unreviewed:       {counts['blank'] + counts['invalid']}")
    print(f"  ─────────────────────────────────────────────────────")
    print(f"  Examples pushed successfully:     {pushed}")
    if errors:
        print(f"  Push errors:                      {errors}")
    print(f"  ─────────────────────────────────────────────────────")
    print(f"  Dataset examples before:          {before_count}")
    print(f"  Dataset examples after:           {after_count}")
    print(f"  Net new examples:                 {after_count - before_count}")
    print(f"{'='*62}")
    print(f"\n  Dataset: {DATASET_NAME}")
    print(f"  View: https://smith.langchain.com/datasets/{dataset.id}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Import reviewed eval candidates from eval_candidates_raw.csv "
            "into the LangSmith dataset interaction-check-eval-v1."
        )
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV,
        metavar="PATH",
        help=(
            "Path to the reviewed CSV file.  "
            f"Defaults to pipeline_outputs/eval_candidates_raw.csv."
        ),
    )
    parser.add_argument(
        "--fresh-start",
        action="store_true",
        help=(
            "Delete the existing dataset and rebuild from scratch.  "
            "DESTRUCTIVE — must also pass --confirm."
        ),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required confirmation flag for --fresh-start.",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help=(
            "Accept the LLM's proposed_* fields for every row without human review. "
            "Only rows with an invalid proposed_final_status (e.g. LLM errors) or "
            "an explicit your_decision='reject' are skipped.  "
            "Compatible with existing approve/edit/reject values already in the file."
        ),
    )
    args = parser.parse_args()
    main(
        csv_path=args.csv,
        fresh_start=args.fresh_start,
        confirmed=args.confirm,
        auto_approve=args.auto_approve,
    )
