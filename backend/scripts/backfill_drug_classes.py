"""
scripts/backfill_drug_classes.py
=================================
Idempotent script that populates ``drug_classes`` for every distinct rxcui
currently present in ``drug_ingredients`` that does not yet have a row.

Strategy
--------
* Collect all distinct non-null rxcuis from ``drug_ingredients``.
* For each, check whether a ``drug_classes`` row already exists; skip if so.
* Call ``get_drug_classes(rxcui)`` and INSERT when data is returned.
* Log clearly: total rxcuis, got data, empty (no classes found), API error.

Idempotent / safely re-runnable — rows with existing data are never modified.

Usage (from backend/)::

    python scripts/backfill_drug_classes.py
"""

import json
import logging
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

import psycopg2

from core.db.session import DATABASE_URL
from core.clients.rxclass_client import get_drug_classes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _sync_url(url: str) -> str:
    """Convert postgresql+asyncpg:// -> postgresql:// for psycopg2."""
    return re.sub(r"^postgresql\+asyncpg://", "postgresql://", url)


def run() -> None:
    conn = psycopg2.connect(_sync_url(DATABASE_URL))
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            # All distinct non-null rxcuis in the dataset
            cur.execute(
                """
                SELECT DISTINCT rxcui
                FROM drug_ingredients
                WHERE rxcui IS NOT NULL
                ORDER BY rxcui
                """
            )
            all_rxcuis: list[str] = [row[0] for row in cur.fetchall()]
            logger.info("Found %d distinct rxcui(s) in drug_ingredients.", len(all_rxcuis))

            # Which already have a drug_classes row?
            cur.execute("SELECT rxcui FROM drug_classes")
            already_done: set[str] = {row[0] for row in cur.fetchall()}
            logger.info("%d rxcui(s) already have drug_classes data — skipping.", len(already_done))

            to_process = [r for r in all_rxcuis if r not in already_done]
            logger.info("%d rxcui(s) to process.", len(to_process))

        ok_count = 0
        empty_count = 0
        error_count = 0

        for rxcui in to_process:
            logger.info("Fetching RxClass for rxcui=%s ...", rxcui)

            try:
                classes = get_drug_classes(rxcui)
            except Exception as e:
                logger.error("Unexpected error for rxcui=%s: %s", rxcui, e)
                error_count += 1
                continue

            if not classes:
                logger.info("  rxcui=%s: no classes returned (expected for some drugs).", rxcui)
                empty_count += 1
                # Still insert an empty row so this rxcui is not re-fetched on
                # future runs (it truly has no class data).
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO drug_classes (rxcui, class_names, class_sources)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (rxcui) DO NOTHING
                        """,
                        (rxcui, json.dumps([]), json.dumps([])),
                    )
                conn.commit()
                continue

            class_names = [c["class_name"] for c in classes]
            class_sources = [c["class_source"] for c in classes]

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO drug_classes (rxcui, class_names, class_sources)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (rxcui) DO NOTHING
                    """,
                    (rxcui, json.dumps(class_names), json.dumps(class_sources)),
                )
            conn.commit()

            logger.info(
                "  rxcui=%s: inserted %d class(es): %s",
                rxcui,
                len(class_names),
                class_names,
            )
            ok_count += 1

        logger.info("")
        logger.info("Summary:")
        logger.info("  Total rxcuis in dataset:          %d", len(all_rxcuis))
        logger.info("  Already had data (skipped):       %d", len(already_done))
        logger.info("  Got class data (inserted):        %d", ok_count)
        logger.info("  Empty / no classes found:         %d", empty_count)
        logger.info("  API errors:                       %d", error_count)

    except Exception:
        conn.rollback()
        logger.exception("Fatal error — rolled back.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run()
