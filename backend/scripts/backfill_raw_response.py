"""
scripts/backfill_raw_response.py
=================================
Idempotent script that fills in ``fda_labels.raw_response`` for any rows where
it is currently ``NULL``.

Strategy:
* For each ``fda_labels`` row with ``raw_response IS NULL``, call
  ``get_fda_label(rxnorm_name)`` using the existing openFDA client (which
  handles rate limiting internally).
* On ``Status.OK``: update ``raw_response`` with the full label JSON payload.
  Also re-populate ``drug_interactions``, ``warnings``, and ``boxed_warning``
  from the fresh response, since the existing text fields were populated from
  the NEML pipeline's inline cache (same source) and should be consistent.
* On ``Status.NOT_FOUND`` or ``Status.API_ERROR``: log and continue — do not
  fail the run.

Rows with a non-null ``raw_response`` are skipped.

Usage::

    cd backend
    python scripts/backfill_raw_response.py
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

from core.db.session import DATABASE_URL  # noqa: E402
from core.clients.openfda_client import get_fda_label, extract_interaction_text  # noqa: E402
from core.status import Status  # noqa: E402

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
            cur.execute(
                """
                SELECT id, rxcui, rxnorm_name
                FROM fda_labels
                WHERE raw_response IS NULL
                ORDER BY id
                """
            )
            rows = cur.fetchall()
            logger.info("Found %d fda_labels row(s) with raw_response = NULL.", len(rows))

            ok_count = 0
            not_found_count = 0
            error_count = 0

            for label_id, rxcui, rxnorm_name in rows:
                logger.info(
                    "Fetching openFDA for id=%-4s rxcui=%-8s rxnorm_name=%r ...",
                    label_id, rxcui, rxnorm_name,
                )

                label_dict, status = get_fda_label(rxnorm_name)

                if status == Status.NOT_FOUND:
                    logger.warning(
                        "NOT_FOUND for rxnorm_name=%r (fda_label id=%s) — leaving NULL.",
                        rxnorm_name, label_id,
                    )
                    not_found_count += 1
                    continue

                if status != Status.OK or label_dict is None:
                    logger.warning(
                        "API_ERROR for rxnorm_name=%r (fda_label id=%s) — leaving NULL.",
                        rxnorm_name, label_id,
                    )
                    error_count += 1
                    continue

                # Extract structured text while we have the fresh payload
                drug_interactions, warnings, boxed_warning = extract_interaction_text(label_dict)

                cur.execute(
                    """
                    UPDATE fda_labels
                    SET raw_response     = %s,
                        drug_interactions = %s,
                        warnings          = %s,
                        boxed_warning     = %s
                    WHERE id = %s
                    """,
                    (
                        json.dumps(label_dict),
                        drug_interactions,
                        warnings,
                        boxed_warning,
                        label_id,
                    ),
                )
                logger.info(
                    "Updated fda_label id=%-4s  raw_response=%d bytes",
                    label_id, len(json.dumps(label_dict)),
                )
                ok_count += 1

        conn.commit()

        logger.info("")
        logger.info("Summary:")
        logger.info("  Updated (raw_response set): %d", ok_count)
        logger.info("  Not found in openFDA:       %d", not_found_count)
        logger.info("  API errors:                 %d", error_count)
        logger.info("  Total NULL rows processed:  %d", len(rows))

    except Exception:
        conn.rollback()
        logger.exception("Error — rolled back.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run()
