"""
scripts/fix_duplicates.py
=========================
One-shot remediation script:

1. Identifies and removes duplicate rows from ``drug_ingredients`` keeping the
   lowest ``id`` for each (drug_id, generic_name, dose) triple.
2. Applies (or verifies) the DB-level UNIQUE constraint that guards against
   future duplicates.

Safe to re-run: the deduplication is idempotent and the constraint creation
uses ``IF NOT EXISTS``.

Usage::

    cd backend
    python scripts/fix_duplicates.py
"""

import logging
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

import psycopg2

from core.db.session import DATABASE_URL  # noqa: E402

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
            # ----------------------------------------------------------------
            # 1. Report current state
            # ----------------------------------------------------------------
            cur.execute(
                """
                SELECT drug_id, generic_name, dose, COUNT(*) AS n
                FROM drug_ingredients
                GROUP BY drug_id, generic_name, dose
                HAVING COUNT(*) > 1
                ORDER BY drug_id, generic_name
                """
            )
            dupes = cur.fetchall()
            if dupes:
                logger.info("Found %d duplicate (drug_id, generic_name, dose) groups:", len(dupes))
                for row in dupes:
                    logger.info("  drug_id=%-4s  %-35s  dose=%-12s  count=%d", *row)
            else:
                logger.info("No duplicate rows found — database already clean.")

            # ----------------------------------------------------------------
            # 2. Delete duplicates — keep only MIN(id) per group
            # ----------------------------------------------------------------
            cur.execute(
                """
                DELETE FROM drug_ingredients
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM drug_ingredients
                    GROUP BY drug_id, generic_name, dose
                )
                RETURNING id, drug_id, generic_name, dose
                """
            )
            deleted = cur.fetchall()
            if deleted:
                logger.info("Deleted %d duplicate row(s):", len(deleted))
                for row in deleted:
                    logger.info(
                        "  deleted id=%-5s  drug_id=%-4s  %-35s  dose=%s",
                        *row,
                    )
            else:
                logger.info("No rows were deleted (none to remove).")

            # ----------------------------------------------------------------
            # 3. Add UNIQUE constraint (idempotent — skipped if already exists)
            # ----------------------------------------------------------------
            cur.execute(
                """
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'uq_drug_ingredient'
                  AND conrelid = 'drug_ingredients'::regclass
                """
            )
            already_exists = cur.fetchone() is not None

            if already_exists:
                logger.info(
                    "Constraint 'uq_drug_ingredient' already exists — skipping ALTER TABLE."
                )
            else:
                logger.info("Applying UNIQUE constraint on (drug_id, generic_name, dose) ...")
                cur.execute(
                    """
                    ALTER TABLE drug_ingredients
                    ADD CONSTRAINT uq_drug_ingredient
                    UNIQUE (drug_id, generic_name, dose)
                    """
                )
                logger.info("Constraint applied successfully.")

        conn.commit()
        logger.info("Done — all changes committed.")

    except Exception:
        conn.rollback()
        logger.exception("Error — rolled back transaction.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run()
