"""
scripts/debug_class_level.py
============================
Diagnoses why class-level detection may be failing for Zoloft + Vexnil.

Checks:
  1. What rxcuis were assigned to sertraline and tramadol ingredients
  2. Whether drug_classes rows exist for those rxcuis
  3. The first 2500 chars of each drug's FDA text (what the model actually sees)
  4. Whether "serotonergic" or "SSRI" appears in the visible window

Usage (from backend/):
    .venv\\Scripts\\python scripts\\debug_class_level.py
"""

import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from core.db.session import DATABASE_URL
from core.db.models import Drug, DrugIngredient, FdaLabel, DrugClass


def sync_url(url: str) -> str:
    return re.sub(r"^postgresql\+asyncpg://", "postgresql://", url)


def main():
    engine = create_engine(sync_url(DATABASE_URL))

    with Session(engine) as session:
        # Find Zoloft and Vexnil drugs
        for query in ("Zoloft", "Vexnil"):
            print(f"\n{'='*60}")
            print(f"Drug: {query}")
            print('='*60)

            stmt = select(Drug).where(Drug.brand_name.ilike(f"%{query}%"))
            drugs = session.execute(stmt).scalars().all()

            if not drugs:
                print(f"  NOT FOUND in drugs table. Will be live-resolved.")
                continue

            for drug in drugs:
                print(f"  drug_id={drug.id}  brand_name={drug.brand_name!r}")
                for ing in drug.ingredients:
                    print(f"\n  Ingredient: {ing.generic_name!r}  rxcui={ing.rxcui!r}")

                    # Check drug_classes
                    if ing.rxcui:
                        dc = session.execute(
                            select(DrugClass).where(DrugClass.rxcui == ing.rxcui)
                        ).scalar_one_or_none()
                        if dc:
                            print(f"  DrugClass row: class_names={dc.class_names}")
                        else:
                            print(f"  DrugClass row: NONE (not in table)")

                        # Check FDA label text
                        label = session.execute(
                            select(FdaLabel).where(FdaLabel.rxcui == ing.rxcui)
                        ).scalar_one_or_none()
                        if label:
                            # Build the same text the pipeline sees
                            parts = []
                            for field in ("drug_interactions", "warnings", "boxed_warning"):
                                val = getattr(label, field, None)
                                if val and val.strip():
                                    parts.append(f"[{field}]\n{val.strip()}")
                            combined = "\n\n".join(parts)
                            print(f"\n  FDA text total: {len(combined)} chars")
                            print(f"  FDA text (first 2500 chars visible to model):")
                            print("  ---")
                            print(combined[:2500])
                            print("  ---")
                            # Key search
                            keywords = ["serotonin", "serotonergic", "SSRI", "SNRI",
                                        "opioid", "tramadol", "sertraline", "monoamine"]
                            found_kw = [kw for kw in keywords if kw.lower() in combined[:2500].lower()]
                            print(f"  Keywords in first 2500 chars: {found_kw or 'NONE'}")
                            after_kw = [kw for kw in keywords if kw.lower() in combined[2500:].lower()]
                            if after_kw:
                                print(f"  Keywords ONLY in truncated tail (> 2500): {after_kw}")
                        else:
                            print(f"  FDA label: NONE")


if __name__ == "__main__":
    main()
