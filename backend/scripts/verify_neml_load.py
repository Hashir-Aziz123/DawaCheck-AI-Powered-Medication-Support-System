"""
verify_neml_load.py
===================
Post-load verification: print row counts from each table and spot-check
known entries (paracetamol/acetaminophen and warfarin).

Run from backend/ directory:
    python scripts/verify_neml_load.py
"""

import re
import sys
from pathlib import Path

SCRIPT_DIR  = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from core.db.session import DATABASE_URL  # noqa: E402


def _sync_url(u: str) -> str:
    return re.sub(r"^postgresql\+asyncpg://", "postgresql://", u)


def main() -> None:
    try:
        import psycopg2
    except ImportError:
        print("psycopg2 not installed.  pip install psycopg2-binary")
        sys.exit(1)

    conn = psycopg2.connect(_sync_url(DATABASE_URL))

    with conn.cursor() as cur:
        # Row counts
        for table in ("drugs", "drug_ingredients", "fda_labels", "interaction_jobs"):
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            count = cur.fetchone()[0]
            print(f"  {table:<25} {count:>6} rows")

        print()

        # Spot-check 1: paracetamol / acetaminophen
        cur.execute("""
            SELECT d.id, d.brand_name, d.drap_reg_no, d.dosage_form,
                   di.generic_name, di.dose, di.rxcui, di.rxnorm_name
            FROM drugs d
            JOIN drug_ingredients di ON di.drug_id = d.id
            WHERE LOWER(d.brand_name) LIKE '%paracetamol%'
               OR LOWER(di.generic_name) LIKE '%paracetamol%'
               OR LOWER(di.rxnorm_name) LIKE '%acetaminophen%'
            LIMIT 5
        """)
        rows = cur.fetchall()
        print("Spot-check — paracetamol/acetaminophen:")
        if rows:
            for r in rows:
                print(f"  drug_id={r[0]}  brand={r[1]!r}  reg={r[2]}  form={r[3]}")
                print(f"    ingredient={r[4]!r}  dose={r[5]}  rxcui={r[6]}  rxnorm={r[7]!r}")
        else:
            print("  *** NOT FOUND ***")

        print()

        # Spot-check 2: warfarin
        cur.execute("""
            SELECT d.id, d.brand_name, d.drap_reg_no, d.dosage_form,
                   di.generic_name, di.dose, di.rxcui, di.rxnorm_name
            FROM drugs d
            JOIN drug_ingredients di ON di.drug_id = d.id
            WHERE LOWER(di.generic_name) LIKE '%warfarin%'
               OR LOWER(di.rxnorm_name) LIKE '%warfarin%'
            LIMIT 5
        """)
        rows = cur.fetchall()
        print("Spot-check — warfarin:")
        if rows:
            for r in rows:
                print(f"  drug_id={r[0]}  brand={r[1]!r}  reg={r[2]}  form={r[3]}")
                print(f"    ingredient={r[4]!r}  dose={r[5]}  rxcui={r[6]}  rxnorm={r[7]!r}")
        else:
            print("  *** NOT FOUND ***")

        print()

        # Spot-check 3: fda_labels with raw_response
        cur.execute("""
            SELECT rxcui, rxnorm_name,
                   (raw_response IS NOT NULL) AS has_raw,
                   (drug_interactions IS NOT NULL) AS has_interactions
            FROM fda_labels
            ORDER BY rxcui
            LIMIT 8
        """)
        rows = cur.fetchall()
        print("Sample fda_labels rows:")
        for r in rows:
            print(f"  rxcui={r[0]}  name={r[1]!r}  has_raw={r[2]}  has_interactions={r[3]}")

    conn.close()


if __name__ == "__main__":
    main()
