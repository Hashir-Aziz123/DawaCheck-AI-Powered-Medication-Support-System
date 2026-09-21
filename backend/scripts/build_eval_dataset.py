"""
Build (or refresh) the LangSmith evaluation dataset: interaction-check-eval-v1.

Reads eval/datasets/interaction_pairs.json (version-controlled seed file) and
creates the dataset + examples in LangSmith.  Idempotent: if the dataset already
exists and has the same number of examples, it skips creation.  Use --force to
delete and recreate from scratch.

Usage (from e:\\mediAid_langraph\\backend\\):
    python scripts/build_eval_dataset.py
    python scripts/build_eval_dataset.py --force
"""

import argparse
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — ensures infra/.env is found and langsmith can be imported
# regardless of invocation directory.
# ---------------------------------------------------------------------------
_BACKEND_DIR = Path(__file__).resolve().parent.parent   # .../backend/
_ROOT_DIR = _BACKEND_DIR.parent                          # .../mediAid_langraph/

if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from dotenv import load_dotenv
load_dotenv(dotenv_path=_ROOT_DIR / "infra" / ".env")

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATASET_NAME = "interaction-check-eval-v1"
DATASET_DESCRIPTION = (
    "Drug interaction pipeline evaluation dataset — 4 labeled pairs from manual "
    "testing.  Each example carries structured expected outputs so evaluators can "
    "check status, groundedness, and severity overstatement programmatically."
)
SEED_FILE = _ROOT_DIR / "eval" / "datasets" / "interaction_pairs.json"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(force: bool = False) -> None:
    try:
        from langsmith import Client
    except ImportError:
        logger.error(
            "langsmith is not installed.  Run: pip install langsmith"
        )
        sys.exit(1)

    # Read the key explicitly so we can diagnose loading issues.
    api_key = os.environ.get("LANGCHAIN_API_KEY", "")
    if not api_key or api_key == "your_langsmith_api_key_here":
        logger.error(
            "LANGCHAIN_API_KEY is not set or still a placeholder.\n"
            "  Add your real key to infra/.env (get it from "
            "https://smith.langchain.com/settings)."
        )
        sys.exit(1)

    logger.info(
        "Using LangSmith key: %s...%s (len=%d)",
        api_key[:12], api_key[-6:], len(api_key),
    )

    # Pass api_key and api_url explicitly so dotenv-loaded values are used even
    # if the shell's environment has stale/different values.
    # LANGCHAIN_ENDPOINT must point to the correct regional server (e.g. APAC).
    api_url = os.environ.get("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com")
    logger.info("Using LangSmith endpoint: %s", api_url)
    client = Client(api_key=api_key, api_url=api_url)

    # --- Load seed data ---
    if not SEED_FILE.exists():
        logger.error("Seed file not found: %s", SEED_FILE)
        sys.exit(1)

    pairs: list[dict] = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    logger.info("Loaded %d examples from %s", len(pairs), SEED_FILE.name)

    # --- Dataset management ---
    try:
        existing_datasets = list(client.list_datasets(dataset_name=DATASET_NAME))
    except Exception as exc:
        err_str = str(exc)
        if "403" in err_str:
            logger.error(
                "403 Forbidden from LangSmith API.\n"
                "  Common causes:\n"
                "  1. The API key is invalid or revoked — regenerate at "
                "https://smith.langchain.com/settings\n"
                "  2. The key belongs to a different workspace/org\n"
                "  3. The key was truncated when pasting into infra/.env\n"
                "  Key used: %s...%s (len=%d)",
                api_key[:12], api_key[-6:], len(api_key),
            )
        else:
            logger.error("LangSmith API error: %s", exc)
        sys.exit(1)

    if existing_datasets and force:
        logger.warning("--force: deleting existing dataset '%s'", DATASET_NAME)
        client.delete_dataset(dataset_id=existing_datasets[0].id)
        existing_datasets = []

    if existing_datasets:
        dataset = existing_datasets[0]
        existing_examples = list(client.list_examples(dataset_id=dataset.id))
        if len(existing_examples) == len(pairs):
            logger.info(
                "Dataset '%s' already exists with %d examples (matches seed). "
                "Nothing to do.  Pass --force to recreate.",
                DATASET_NAME, len(existing_examples),
            )
            print(f"\nDataset URL: https://smith.langchain.com/datasets/{dataset.id}")
            return
        else:
            logger.info(
                "Dataset '%s' exists but has %d examples (seed has %d). "
                "Adding/updating examples.",
                DATASET_NAME, len(existing_examples), len(pairs),
            )
    else:
        logger.info("Creating dataset '%s'...", DATASET_NAME)
        dataset = client.create_dataset(
            dataset_name=DATASET_NAME,
            description=DATASET_DESCRIPTION,
        )
        logger.info("Dataset created: %s", dataset.id)

    # --- Add examples ---
    for pair in pairs:
        inputs = {
            "drug_a_name": pair["drug_a_name"],
            "drug_b_name": pair["drug_b_name"],
        }
        outputs = pair["expected_output"]
        metadata = {"notes": pair.get("notes", "")}

        client.create_example(
            inputs=inputs,
            outputs=outputs,
            metadata=metadata,
            dataset_id=dataset.id,
        )
        logger.info(
            "  Added example: %s ↔ %s  [expected: %s]",
            pair["drug_a_name"],
            pair["drug_b_name"],
            outputs.get("final_status"),
        )

    logger.info(
        "Done — %d examples added to dataset '%s'.", len(pairs), DATASET_NAME
    )
    print(f"\nDataset URL: https://smith.langchain.com/datasets/{dataset.id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the LangSmith eval dataset.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete the existing dataset and recreate from scratch.",
    )
    args = parser.parse_args()
    main(force=args.force)
