"""
load_neml_csv_to_db.py
======================
Load reviewed NEML pipeline results from ``neml_pipeline_results.csv``
into the PostgreSQL database (tables: drugs, drug_ingredients, fda_labels).

Filter rules
------------
Only rows whose generic-level ``overall_status`` (read from the
``neml_resolutions.json`` disk cache) is one of the *accepted* statuses
are loaded.  Rejected rows are skipped and tallied.

Accepted statuses
    ok, stripped_and_matched, approximate_match,
    spelling_retry_found, spelling_retry_low_confidence

Rejected statuses (excluded)
    composition_mismatch, no_drap_match, empty_data,
    combination_component_mismatch, api_error, (any other)

Design notes
------------
* Runs synchronously using ``psycopg2`` directly (avoids having to drive
  asyncio from a script) while keeping the SQLAlchemy model definitions as
  the single source of truth for column names.
* All inserts are wrapped in ONE transaction: either everything commits or
  nothing does.
* ``drugs`` deduplication: the schema has no UNIQUE constraint on
  ``drap_reg_no``, so the script checks for an existing row by
  ``drap_reg_no`` before inserting.  Already-present drugs are skipped and
  their ``id`` is reused for ingredient linking.
* ``fda_labels`` deduplication: ``rxcui`` has a UNIQUE constraint, so we
  use ``INSERT … ON CONFLICT (rxcui) DO NOTHING`` and check
  ``rowcount`` to distinguish insert vs skip.
* Raw openFDA JSON response: recovered from ``scripts/.cache/openfda.json``
  keyed by UPPERCASE ``rxnorm_name``; stored in ``fda_labels.raw_response``
  as JSONB.  Rows whose rxcui was not found in the cache get
  ``raw_response = NULL``.
* Re-runnable: running the script twice produces no duplicates.

Usage (from backend/ directory)
--------------------------------
    python scripts/load_neml_csv_to_db.py

Environment
-----------
    DATABASE_URL must resolve (loaded from infra/.env automatically via
    core/db/session.py conventions); the URL must use the *sync* scheme
    ``postgresql://`` (psycopg2) rather than ``postgresql+asyncpg://``.
    The script converts the URL automatically.
"""

import csv
import json
import logging
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — make ``core/`` importable when run as a script
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from core.db.session import DATABASE_URL  # noqa: E402  (side-effect: loads infra/.env)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("load_neml_csv")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CSV_PATH       = BACKEND_DIR / "neml_pipeline_results.csv"
RES_CACHE_PATH = SCRIPT_DIR / ".cache" / "neml_resolutions.json"
FDA_CACHE_PATH = SCRIPT_DIR / ".cache" / "openfda.json"

# ---------------------------------------------------------------------------
# Accepted / rejected overall_status values
# ---------------------------------------------------------------------------

ACCEPTED_STATUSES: frozenset[str] = frozenset({
    "ok",
    "stripped_and_matched",
    "approximate_match",
    "spelling_retry_found",
    "spelling_retry_low_confidence",
})

REJECTED_STATUSES: frozenset[str] = frozenset({
    "composition_mismatch",
    "no_drap_match",
    "empty_data",
    "combination_component_mismatch",
    "api_error",
})

# ---------------------------------------------------------------------------
# Dosage-form extraction from selection_reason
# (e.g. "tablet (of 118)" -> "tablet")
# ---------------------------------------------------------------------------

_FORM_RE = re.compile(
    r"\b(tablet|capsule|syrup|solution|suspension|injection|drop|cream|"
    r"ointment|inhaler|suppository|oral|granule|powder|gel|patch|spray)\b",
    re.IGNORECASE,
)


def _extract_dosage_form(selection_reason: str | None) -> str | None:
    """Return the first dosage-form keyword found in ``selection_reason``."""
    if not selection_reason:
        return None
    m = _FORM_RE.search(selection_reason)
    return m.group(0).lower() if m else None


# ---------------------------------------------------------------------------
# Database URL conversion: asyncpg -> psycopg2
# ---------------------------------------------------------------------------

def _sync_url(async_url: str) -> str:
    """Convert ``postgresql+asyncpg://...`` -> ``postgresql://...`` for psycopg2."""
    return re.sub(r"^postgresql\+asyncpg://", "postgresql://", async_url)


# ---------------------------------------------------------------------------
# Main loader
# ---------------------------------------------------------------------------

def load() -> None:
    # -- 1. Load the resolution cache (provides overall_status per generic) --

    if not RES_CACHE_PATH.exists():
        logger.error("Resolution cache not found: %s", RES_CACHE_PATH)
        sys.exit(1)

    res_cache: dict = json.loads(RES_CACHE_PATH.read_text(encoding="utf-8"))
    logger.info("Loaded resolution cache: %d entries", len(res_cache))

    # -- 2. Load openFDA raw-response cache -----------------------------------

    fda_cache: dict = {}
    if FDA_CACHE_PATH.exists():
        fda_cache = json.loads(FDA_CACHE_PATH.read_text(encoding="utf-8"))
        logger.info("Loaded openFDA cache: %d entries", len(fda_cache))
    else:
        logger.warning(
            "openFDA cache not found (%s) -- raw_response will be NULL for all rows",
            FDA_CACHE_PATH,
        )

    # -- 3. Read CSV and group by drap_reg_no ---------------------------------

    if not CSV_PATH.exists():
        logger.error("CSV not found: %s", CSV_PATH)
        sys.exit(1)

    with open(CSV_PATH, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        all_rows = [r for r in reader if any(v.strip() for v in r.values())]

    logger.info("CSV rows read (non-blank): %d", len(all_rows))

    # Determine per-generic overall_status from resolution cache
    generic_status: dict[str, str] = {
        generic: entry.get("overall_status", "unknown")
        for generic, entry in res_cache.items()
    }

    # Filter rows: keep only those whose searched_generic has an accepted status
    accepted_rows: list[dict] = []
    skipped_generics: dict[str, str] = {}   # generic -> status

    for row in all_rows:
        generic = row["searched_generic"]
        status  = generic_status.get(generic, "unknown")
        if status in ACCEPTED_STATUSES:
            accepted_rows.append(row)
        else:
            skipped_generics[generic] = status

    if skipped_generics:
        logger.info(
            "Skipped generics (%d) with rejected overall_status:", len(skipped_generics)
        )
        for g, s in sorted(skipped_generics.items()):
            logger.info("    %-45s  [%s]", g, s)

    logger.info(
        "Filtering: %d accepted rows, %d generics skipped",
        len(accepted_rows),
        len(skipped_generics),
    )

    # -- 4. Group accepted rows by drap_reg_no --------------------------------
    # Structure: { drap_reg_no: { "drug_info": {...}, "ingredients": [...] } }

    drugs_map: dict[str, dict] = {}   # keyed by drap_reg_no

    for row in accepted_rows:
        reg_no = row.get("drap_reg_no", "").strip() or None
        if reg_no is None:
            logger.warning(
                "Row for '%s' has no drap_reg_no -- skipping", row.get("searched_generic")
            )
            continue

        if reg_no not in drugs_map:
            drugs_map[reg_no] = {
                "brand_name":   (row.get("selected_brand_name") or "").strip(),
                "drap_reg_no":  reg_no,
                "dosage_form":  _extract_dosage_form(row.get("selection_reason")),
                "company_name": None,   # not present in CSV
                "ingredients":  [],
            }

        ing_name = (row.get("ingredient_name") or "").strip()
        if ing_name:
            drugs_map[reg_no]["ingredients"].append({
                "generic_name":       ing_name,
                "dose":               (row.get("ingredient_amount") or "").strip() or None,
                "rxcui":              (row.get("rxcui") or "").strip() or None,
                "rxnorm_name":        (row.get("rxnorm_name") or "").strip() or None,
                "_drug_interactions": (row.get("drug_interactions") or "").strip() or None,
                "_warnings":          (row.get("warnings") or "").strip() or None,
                "_boxed_warning":     (row.get("boxed_warning") or "").strip() or None,
            })

    logger.info(
        "Unique drug registrations to process: %d  (total ingredients: %d)",
        len(drugs_map),
        sum(len(v["ingredients"]) for v in drugs_map.values()),
    )

    # -- 5. Collect unique rxcui entries for fda_labels -----------------------

    fda_labels_map: dict[str, dict] = {}

    for drug_info in drugs_map.values():
        for ing in drug_info["ingredients"]:
            rxcui = ing.get("rxcui")
            if not rxcui or rxcui in fda_labels_map:
                continue
            rxnorm_name = ing.get("rxnorm_name") or ""
            # The openFDA cache keyed by UPPERCASE rxnorm_name (as queried by the pipeline)
            raw_response = fda_cache.get(rxnorm_name.upper()) or fda_cache.get(rxnorm_name)
            fda_labels_map[rxcui] = {
                "rxcui":             rxcui,
                "rxnorm_name":       rxnorm_name,
                "drug_interactions": ing["_drug_interactions"],
                "warnings":          ing["_warnings"],
                "boxed_warning":     ing["_boxed_warning"],
                "raw_response":      raw_response,
            }

    logger.info("Unique rxcui entries for fda_labels: %d", len(fda_labels_map))
    raw_found = sum(1 for v in fda_labels_map.values() if v["raw_response"] is not None)
    logger.info(
        "  -- raw_response available from openFDA cache: %d / %d",
        raw_found, len(fda_labels_map),
    )

    # -- 6. Connect and insert in a single transaction ------------------------

    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        logger.error(
            "psycopg2 is not installed.  Install it with:\n"
            "  pip install psycopg2-binary"
        )
        sys.exit(1)

    sync_url = _sync_url(DATABASE_URL)
    logger.info("Connecting to database ...")
    conn = psycopg2.connect(sync_url)
    conn.autocommit = False

    drugs_inserted = 0
    drugs_skipped  = 0
    ingr_inserted  = 0
    fda_inserted   = 0
    fda_skipped    = 0

    try:
        with conn.cursor() as cur:

            # -- 6a. drugs ----------------------------------------------------
            logger.info("Inserting drugs ...")
            drug_id_map: dict[str, int] = {}

            cur.execute(
                "SELECT id, drap_reg_no FROM drugs WHERE drap_reg_no IS NOT NULL"
            )
            existing_drugs: dict[str, int] = {row[1]: row[0] for row in cur.fetchall()}

            for reg_no, drug_info in drugs_map.items():
                if reg_no in existing_drugs:
                    drug_id_map[reg_no] = existing_drugs[reg_no]
                    drugs_skipped += 1
                    logger.debug(
                        "SKIP  drug '%s'  (drap_reg_no=%s already exists, id=%d)",
                        drug_info["brand_name"], reg_no, existing_drugs[reg_no],
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO drugs (brand_name, drap_reg_no, dosage_form, company_name)
                        VALUES (%s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            drug_info["brand_name"],
                            drug_info["drap_reg_no"],
                            drug_info["dosage_form"],
                            drug_info["company_name"],
                        ),
                    )
                    new_id = cur.fetchone()[0]
                    drug_id_map[reg_no] = new_id
                    drugs_inserted += 1
                    logger.debug(
                        "INSERT drug '%s'  (id=%d)", drug_info["brand_name"], new_id
                    )

            # -- 6b. drug_ingredients -----------------------------------------
            # Insert ingredients only for drugs that were inserted in this run.
            # We track `inserted_this_run` as we go so that a reg_no that
            # appears in two separate CSV groups (e.g. "amiloride" and
            # "hydrochlorothiazide" both resolving to the same product) is only
            # processed once — even within a single invocation where the first
            # group created the drug row mid-transaction (making it invisible to
            # the `existing_drugs` snapshot taken before the loop).
            logger.info("Inserting drug_ingredients ...")
            inserted_this_run: set[str] = set(drugs_map) - set(existing_drugs)

            for reg_no in inserted_this_run:
                drug_info = drugs_map[reg_no]
                drug_id   = drug_id_map[reg_no]
                for ing in drug_info["ingredients"]:
                    cur.execute(
                        """
                        INSERT INTO drug_ingredients
                            (drug_id, generic_name, dose, rxcui, rxnorm_name)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT ON CONSTRAINT uq_drug_ingredient DO NOTHING
                        """,
                        (
                            drug_id,
                            ing["generic_name"],
                            ing["dose"],
                            ing["rxcui"],
                            ing["rxnorm_name"],
                        ),
                    )
                    if cur.rowcount == 1:
                        ingr_inserted += 1

            # -- 6c. fda_labels -----------------------------------------------
            logger.info("Inserting fda_labels ...")

            for rxcui, label in fda_labels_map.items():
                raw = label["raw_response"]
                cur.execute(
                    """
                    INSERT INTO fda_labels
                        (rxcui, rxnorm_name, drug_interactions, warnings,
                         boxed_warning, raw_response)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (rxcui) DO NOTHING
                    """,
                    (
                        label["rxcui"],
                        label["rxnorm_name"],
                        label["drug_interactions"],
                        label["warnings"],
                        label["boxed_warning"],
                        psycopg2.extras.Json(raw) if raw is not None else None,
                    ),
                )
                if cur.rowcount == 1:
                    fda_inserted += 1
                else:
                    fda_skipped += 1

        conn.commit()
        logger.info("Transaction committed successfully.")

    except Exception:
        conn.rollback()
        logger.exception(
            "Error during load -- transaction rolled back.  No rows were written."
        )
        sys.exit(1)
    finally:
        conn.close()

    # -- 7. Summary -----------------------------------------------------------

    logger.info("")
    logger.info("=======================================================")
    logger.info("  NEML CSV -> DB load complete")
    logger.info("  Generics with rejected status (not loaded): %d", len(skipped_generics))
    logger.info("  +---------------------+----------+----------+")
    logger.info("  | Table               | Inserted |  Skipped |")
    logger.info("  +---------------------+----------+----------+")
    logger.info("  | drugs               | %8d | %8d |", drugs_inserted, drugs_skipped)
    logger.info("  | drug_ingredients    | %8d |        - |", ingr_inserted)
    logger.info("  | fda_labels          | %8d | %8d |", fda_inserted, fda_skipped)
    logger.info("  +---------------------+----------+----------+")
    logger.info("=======================================================")


if __name__ == "__main__":
    load()
