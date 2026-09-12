"""
scripts/reparse_codeine.py
==========================
Re-parses the Codeine Plus composition (drug_id 3, reg_no 032251) using the
fixed ``parse_composition`` function, then:

1. Scans ALL loaded ``drug_ingredients`` rows for the "fused-dose" symptom
   (dose string containing dose unit + alphabetic text) and reports them.
2. Deletes the single malformed ingredient row for drug_id 3.
3. Re-resolves the three ingredients (Codeine, Paracetamol, Caffeine) through
   RxNorm + openFDA using existing client functions.
4. Inserts the three correct ``drug_ingredients`` rows and a new ``fda_labels``
   row for Paracetamol (Caffeine typically has no FDA label).

Usage::

    cd backend
    python scripts/reparse_codeine.py

The DRAP cache (scripts/.cache/drap.json) must contain the raw composition for
reg_no 032251. If not, the script will re-fetch it live.
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
from core.clients.drap_client import parse_composition, get_drug_detail, preprocess_ingredient_name  # noqa: E402
from core.clients.rxnorm_client import get_rxnorm_name  # noqa: E402
from core.clients.openfda_client import get_fda_label  # noqa: E402
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


CACHE_FILE = SCRIPT_DIR / ".cache" / "drap.json"
CODEINE_REG_NO = "032251"
CODEINE_DRUG_ID = 3

# Symptom regex: dose field contains a dose unit immediately followed by an
# alphabetic character — indicates the parser failed to split a fused line.
FUSED_DOSE_RE = re.compile(
    r"\d+\.?\d*\s*(?:mg|mcg|g|ml|IU|%)\s*[A-Za-z]",
    re.IGNORECASE,
)


def load_drap_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {}


def get_cached_composition(cache: dict, reg_no: str) -> str | None:
    """Return the raw composition string from the DRAP detail cache, or None."""
    key = f"detail::{reg_no}"
    detail = cache.get(key)
    if not detail:
        return None
    comp = detail.get("Composition")
    if not comp:
        return None
    if isinstance(comp, list):
        # Already parsed — reconstruct a raw string
        parts = []
        for item in comp:
            parts.append(f"{item.get('name', '')}...{item.get('amount', '')}")
        return "\n".join(parts)
    return comp  # raw string


def scan_for_malformed_rows(cur) -> list[tuple]:
    """Return all drug_ingredients rows whose dose looks like a fused parse."""
    cur.execute("SELECT id, drug_id, generic_name, dose FROM drug_ingredients ORDER BY drug_id, id")
    rows = cur.fetchall()
    malformed = [
        r for r in rows
        if r[3] and FUSED_DOSE_RE.search(str(r[3]))
    ]
    return malformed


def run() -> None:
    cache = load_drap_cache()

    # -------------------------------------------------------------------------
    # 1. Get the raw composition string for Codeine Plus
    # -------------------------------------------------------------------------
    raw_comp = get_cached_composition(cache, CODEINE_REG_NO)
    if raw_comp is None:
        logger.info("Cache miss for detail::%s — fetching live from DRAP ...", CODEINE_REG_NO)
        detail, err = get_drug_detail(CODEINE_REG_NO)
        if err:
            logger.error("Failed to fetch DRAP detail for %s: %s", CODEINE_REG_NO, err)
            sys.exit(1)
        raw_comp = detail.get("Composition", "")
        if isinstance(raw_comp, list):
            # Already parsed by get_drug_detail — reconstruct raw string
            parts = []
            for item in raw_comp:
                parts.append(f"{item.get('name', '')}...{item.get('amount', '')}")
            raw_comp = "\n".join(parts)
    else:
        logger.info("Using cached DRAP composition for reg_no %s.", CODEINE_REG_NO)

    logger.info("Raw composition string:\n  %s", raw_comp)

    # -------------------------------------------------------------------------
    # 2. Parse with the fixed parser
    # -------------------------------------------------------------------------
    parsed = parse_composition(raw_comp)
    logger.info("Parsed %d ingredient(s):", len(parsed))
    for i, p in enumerate(parsed, 1):
        logger.info("  [%d] name=%r  amount=%r", i, p["name"], p["amount"])

    if len(parsed) < 2:
        logger.warning(
            "Parser still returns only %d ingredient(s). "
            "This may mean the raw composition from the cache is already post-parse. "
            "Check that drap.json::detail::032251 has the raw Label Claim or Composition "
            "string rather than a pre-parsed list.",
            len(parsed),
        )
        # If we got only one, the raw string in cache is the pre-parsed version.
        # Fall back to the known real raw string for this record.
        logger.info("Falling back to known raw DRAP composition for 032251 ...")
        raw_comp = "CODEINE PHOSPHATE15mgParacetamol.....................500mgCaffeine"
        parsed = parse_composition(raw_comp)
        logger.info("Re-parsed %d ingredient(s):", len(parsed))
        for i, p in enumerate(parsed, 1):
            logger.info("  [%d] name=%r  amount=%r", i, p["name"], p["amount"])

    # -------------------------------------------------------------------------
    # 3. Resolve each ingredient via RxNorm (reuse existing normalise_ingredient)
    # -------------------------------------------------------------------------
    resolved_ingredients = []
    for item in parsed:
        raw_name = item["name"].upper()
        cleaned_name, was_stripped = preprocess_ingredient_name(raw_name)
        logger.info(
            "Pre-processing: %r -> %r (stripped=%s)", raw_name, cleaned_name, was_stripped
        )

        rxcui, rxnorm_name, norm_status = get_rxnorm_name(
            cleaned_name, was_stripped=was_stripped
        )
        logger.info(
            "RxNorm: %r -> rxcui=%s  rxnorm_name=%r  status=%s",
            cleaned_name, rxcui, rxnorm_name, norm_status.value,
        )

        fda_label_dict = None
        fda_status = "skipped"
        drug_interactions = None
        warnings = None
        boxed_warning = None

        if rxnorm_name:
            from core.clients.openfda_client import extract_interaction_text
            fda_label_dict, fda_status_enum = get_fda_label(rxnorm_name)
            fda_status = fda_status_enum.value
            if fda_label_dict is not None:
                drug_interactions, warnings, boxed_warning = extract_interaction_text(fda_label_dict)
            logger.info("FDA label status for %r: %s", rxnorm_name, fda_status)

        resolved_ingredients.append({
            "generic_name": cleaned_name,
            "dose": item["amount"],
            "rxcui": rxcui,
            "rxnorm_name": rxnorm_name,
            "drug_interactions": drug_interactions,
            "warnings": warnings,
            "boxed_warning": boxed_warning,
            "fda_status": fda_status,
            "fda_label_dict": fda_label_dict,
        })

    # -------------------------------------------------------------------------
    # 4. Update the database
    # -------------------------------------------------------------------------
    conn = psycopg2.connect(_sync_url(DATABASE_URL))
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            # Scan for ALL malformed rows first
            malformed = scan_for_malformed_rows(cur)
            if malformed:
                logger.info(
                    "Found %d malformed dose string(s) across all drug_ingredients rows:",
                    len(malformed),
                )
                for row in malformed:
                    logger.info(
                        "  id=%-5s  drug_id=%-4s  %-30s  dose=%s", *row
                    )
            else:
                logger.info("No other malformed dose strings found in drug_ingredients.")

            # Delete the bad row(s) for drug_id 3
            cur.execute(
                "DELETE FROM drug_ingredients WHERE drug_id = %s RETURNING id, generic_name, dose",
                (CODEINE_DRUG_ID,),
            )
            deleted = cur.fetchall()
            logger.info(
                "Deleted %d old ingredient row(s) for drug_id %s: %s",
                len(deleted), CODEINE_DRUG_ID, deleted,
            )

            # Insert the correctly parsed ingredients
            for ing in resolved_ingredients:
                cur.execute(
                    """
                    INSERT INTO drug_ingredients
                        (drug_id, generic_name, dose, rxcui, rxnorm_name)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT ON CONSTRAINT uq_drug_ingredient DO NOTHING
                    RETURNING id
                    """,
                    (
                        CODEINE_DRUG_ID,
                        ing["generic_name"],
                        ing["dose"],
                        ing["rxcui"],
                        ing["rxnorm_name"],
                    ),
                )
                row = cur.fetchone()
                if row:
                    new_id = row[0]
                    logger.info(
                        "Inserted drug_ingredient id=%s: %r  dose=%r  rxcui=%s",
                        new_id, ing["generic_name"], ing["dose"], ing["rxcui"],
                    )
                else:
                    logger.info(
                        "Skipped (already exists): %r  dose=%r", ing["generic_name"], ing["dose"]
                    )

                # Insert / update fda_labels row if we got a result
                if ing["fda_label_dict"] is not None and ing["fda_status"] == "ok":
                    raw_resp = json.dumps(ing["fda_label_dict"])
                    cur.execute(
                        """
                        INSERT INTO fda_labels
                            (rxcui, rxnorm_name, drug_interactions, warnings,
                             boxed_warning, raw_response)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (rxcui) DO UPDATE
                            SET drug_interactions = EXCLUDED.drug_interactions,
                                warnings          = EXCLUDED.warnings,
                                boxed_warning     = EXCLUDED.boxed_warning,
                                raw_response      = EXCLUDED.raw_response
                        """,
                        (
                            ing["rxcui"],
                            ing["rxnorm_name"],
                            ing["drug_interactions"],
                            ing["warnings"],
                            ing["boxed_warning"],
                            raw_resp,
                        ),
                    )
                    if cur.rowcount >= 1:
                        logger.info(
                            "Upserted fda_labels row for rxcui=%s (%r)",
                            ing["rxcui"], ing["rxnorm_name"],
                        )

        conn.commit()
        logger.info("Done — all changes committed.")

    except Exception:
        conn.rollback()
        logger.exception("Error — rolled back.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run()
