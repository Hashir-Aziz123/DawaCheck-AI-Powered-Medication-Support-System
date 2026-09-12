"""
export_db_to_csv.py
===================
Exports a single flat CSV — one row per (drug, ingredient) pair — for
cross-verification of loaded data.

Output
------
  backend/pipeline_outputs/drugs_with_ingredients_export.csv

    Columns:
      drug_id, brand_name, drap_reg_no, dosage_form, company_name,
      ingredient_id, generic_name, dose, rxcui, rxnorm_name

    From this one file you can directly verify:
      - Total unique drug_id values  -> drugs row count
      - Total rows                   -> drug_ingredients row count
      - drug_ids appearing once      -> simple (single-ingredient) drugs
      - drug_ids appearing 2+ times  -> combination drugs

Usage (from backend/ directory)
--------------------------------
    python scripts/export_db_to_csv.py

Environment
-----------
    DATABASE_URL is resolved via core/db/session.py (loads infra/.env).
    asyncpg URL is converted to psycopg2 automatically.

Read-only -- does not modify any table or existing file.
"""

import csv
import re
import sys
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

SCRIPT_DIR  = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from core.db.session import DATABASE_URL  # noqa: E402 (loads infra/.env)

# ---------------------------------------------------------------------------
# Output location
# ---------------------------------------------------------------------------

OUT_DIR = BACKEND_DIR / "pipeline_outputs"
OUT_DIR.mkdir(exist_ok=True)
OUT_FILE_DRUGS = OUT_DIR / "drugs_with_ingredients_export.csv"
OUT_FILE_FDA = OUT_DIR / "fda_labels_export.csv"
OUT_FILE_FDA_DETAIL = OUT_DIR / "fda_labels_raw_response_detail.csv"

FIELDS_DRUGS = [
    "drug_id", "brand_name", "drap_reg_no", "dosage_form", "company_name",
    "ingredient_id", "generic_name", "dose", "rxcui", "rxnorm_name",
]

FIELDS_FDA = [
    "id", "rxcui", "rxnorm_name", "drug_interactions", "warnings", "boxed_warning",
    "fetched_at", "has_raw_response"
]

FIELDS_FDA_DETAIL = [
    "id", "rxcui", "rxnorm_name", "has_raw_response",
    "raw_response_size_bytes", "raw_response_top_level_keys"
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sync_url(url: str) -> str:
    """Convert postgresql+asyncpg:// -> postgresql:// for psycopg2."""
    return re.sub(r"^postgresql\+asyncpg://", "postgresql://", url)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def export() -> None:
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary",
              file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(_sync_url(DATABASE_URL))
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # --- 1. Export Drugs with Ingredients ---
    cur.execute("""
        SELECT
            d.id          AS drug_id,
            d.brand_name,
            d.drap_reg_no,
            d.dosage_form,
            d.company_name,
            di.id         AS ingredient_id,
            di.generic_name,
            di.dose,
            di.rxcui,
            di.rxnorm_name
        FROM drugs d
        JOIN drug_ingredients di ON di.drug_id = d.id
        ORDER BY d.id, di.id
    """)
    drug_rows = [dict(r) for r in cur.fetchall()]

    with open(OUT_FILE_DRUGS, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS_DRUGS)
        writer.writeheader()
        writer.writerows(drug_rows)

    # --- 2. Export FDA Labels (Quick Scan) ---
    cur.execute("""
        SELECT
            id,
            rxcui,
            rxnorm_name,
            drug_interactions,
            warnings,
            boxed_warning,
            fetched_at,
            raw_response IS NOT NULL AS has_raw_response
        FROM fda_labels
        ORDER BY id
    """)
    fda_rows = [dict(r) for r in cur.fetchall()]
    
    with open(OUT_FILE_FDA, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS_FDA)
        writer.writeheader()
        writer.writerows(fda_rows)

    # --- 3. Export FDA Labels (Raw Response Detail) ---
    import json
    cur.execute("SELECT id, rxcui, rxnorm_name, raw_response FROM fda_labels ORDER BY id")
    fda_detail_db_rows = cur.fetchall()
    
    fda_detail_rows = []
    top_keys_sets = Counter()

    for r in fda_detail_db_rows:
        has_raw = r["raw_response"] is not None
        size_bytes = 0
        top_keys = ""
        
        if has_raw:
            raw_json = json.dumps(r["raw_response"])
            size_bytes = len(raw_json.encode("utf-8"))
            keys_list = list(r["raw_response"].keys())
            top_keys = ",".join(keys_list)
            top_keys_sets[top_keys] += 1
            
        fda_detail_rows.append({
            "id": r["id"],
            "rxcui": r["rxcui"],
            "rxnorm_name": r["rxnorm_name"],
            "has_raw_response": has_raw,
            "raw_response_size_bytes": size_bytes if has_raw else None,
            "raw_response_top_level_keys": top_keys if has_raw else None,
        })
        
    with open(OUT_FILE_FDA_DETAIL, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS_FDA_DETAIL)
        writer.writeheader()
        writer.writerows(fda_detail_rows)

    cur.close()
    conn.close()

    # --- summary ------------------------------------------------------------
    ingr_per_drug: Counter = Counter(r["drug_id"] for r in drug_rows)
    unique_drugs  = len(ingr_per_drug)
    total_ingr    = len(drug_rows)
    simple        = sum(1 for c in ingr_per_drug.values() if c == 1)
    combo         = sum(1 for c in ingr_per_drug.values() if c >= 2)
    
    total_fda = len(fda_rows)
    fda_with_raw = sum(1 for r in fda_rows if r["has_raw_response"])

    print(f"\n{'='*55}")
    print(f"  Export complete.")
    print(f"  - {OUT_FILE_DRUGS.name}")
    print(f"  - {OUT_FILE_FDA.name}")
    print(f"  - {OUT_FILE_FDA_DETAIL.name}")
    print(f"{'='*55}")
    print(f"  Total rows (drug x ingredient pairs) : {total_ingr:>5,}")
    print(f"  Unique drugs                         : {unique_drugs:>5,}")
    print(f"    single-ingredient drugs            : {simple:>5,}")
    print(f"    combination drugs (2+ ingredients) : {combo:>5,}")
    print(f"\n  Total fda_labels                     : {total_fda:>5,}")
    print(f"    with raw_response                  : {fda_with_raw:>5,}")
    print(f"    without raw_response               : {total_fda - fda_with_raw:>5,}")
    
    print(f"\n  Raw Response Top-Level Keys Distribution:")
    for keys_str, count in top_keys_sets.most_common():
        print(f"    {count:>3} rows -> [{keys_str}]")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    export()
