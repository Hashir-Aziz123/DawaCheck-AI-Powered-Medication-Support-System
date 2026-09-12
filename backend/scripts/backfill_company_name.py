"""
scripts/backfill_company_name.py
=================================
Reads every ``drugs`` row and populates ``company_name`` from the DRAP detail
cache (``scripts/.cache/drap.json``, keys ``"detail::<reg_no>"``).

If a reg_no is not cached, the script fetches the detail page live from DRAP
and updates the cache file with the result so future reruns stay cache-only.

Rows that already have a non-null ``company_name`` are skipped.
Rows for which DRAP has no "Company Name" field are logged but left as NULL.

Usage::

    cd backend
    python scripts/backfill_company_name.py
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
from core.clients.drap_client import get_drug_detail  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CACHE_FILE = SCRIPT_DIR / ".cache" / "drap.json"


def _sync_url(url: str) -> str:
    """Convert postgresql+asyncpg:// -> postgresql:// for psycopg2."""
    return re.sub(r"^postgresql\+asyncpg://", "postgresql://", url)


def load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def run() -> None:
    cache = load_cache()
    cache_dirty = False

    conn = psycopg2.connect(_sync_url(DATABASE_URL))
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, brand_name, drap_reg_no, company_name 
                FROM drugs 
                WHERE company_name IS NULL 
                   OR company_name = '' 
                   OR company_name = 'test3'
                ORDER BY id
                """
            )
            drugs = cur.fetchall()
            logger.info("Processing %d drug(s) requiring company_name fix ...", len(drugs))

            updated = 0
            cleared_test3 = 0
            skipped_already = 0
            no_data = 0
            live_fetched = 0

            for drug_id, brand_name, drap_reg_no, company_name in drugs:
                if not drap_reg_no:
                    logger.warning("drug_id=%s (%s) has no drap_reg_no — skipping.", drug_id, brand_name)
                    no_data += 1
                    continue


                cache_key = f"detail::{drap_reg_no}"
                detail = cache.get(cache_key)

                if detail is None:
                    logger.info(
                        "Cache miss for %s (drug_id=%s %s) — fetching live ...",
                        cache_key, drug_id, brand_name,
                    )
                    detail, err = get_drug_detail(drap_reg_no)
                    if err or not detail:
                        logger.warning(
                            "Could not fetch DRAP detail for reg_no %s: %s", drap_reg_no, err
                        )
                        no_data += 1
                        continue
                    # Normalize: Composition is already parsed by get_drug_detail
                    cache[cache_key] = detail
                    cache_dirty = True
                    live_fetched += 1

                co_name = detail.get("Company Name") or detail.get("company_name")
                
                # Check for DRAP's dummy data or empty strings
                if not co_name or co_name.strip().lower() in ("test", "test3"):
                    logger.warning(
                        "DRAP returned empty or dummy data (%r) for reg_no %s (%s) — treating as genuinely empty.",
                        co_name, drap_reg_no, brand_name,
                    )
                    no_data += 1
                    
                    # If it was previously 'test3', we must clear it to NULL
                    if company_name == "test3":
                        cur.execute("UPDATE drugs SET company_name = NULL WHERE id = %s", (drug_id,))
                        cleared_test3 += 1
                        logger.info("Cleared placeholder 'test3' to NULL for drug_id=%s", drug_id)
                        
                    continue

                cur.execute(
                    "UPDATE drugs SET company_name = %s WHERE id = %s",
                    (co_name.strip(), drug_id),
                )
                logger.info(
                    "drug_id=%-3s  %-45s  -> %s", drug_id, brand_name, co_name.strip()
                )
                updated += 1

        conn.commit()

        logger.info("")
        logger.info("Summary:")
        logger.info("  Valid names updated: %d", updated)
        logger.info("  'test3' cleared:     %d", cleared_test3)
        logger.info("  Empty/Dummy in DRAP: %d", no_data)
        logger.info("  Live DRAP fetches:   %d", live_fetched)

        if cache_dirty:
            save_cache(cache)
            logger.info("DRAP cache updated with %d new live fetch(es).", live_fetched)

    except Exception:
        conn.rollback()
        logger.exception("Error — rolled back.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run()
