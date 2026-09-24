"""
export_fda_reference.py
=======================
Part B of the LLM-assisted eval dataset pipeline.

Exports the COMPLETE, UNTRUNCATED FDA label text for every drug in the
database into two companion files:

  pipeline_outputs/eval_fda_reference.csv
      Columns: drug_id, brand_name, generic_names, drug_interactions,
               warnings, boxed_warning
      FDA text is concatenated from all ingredients (labelled by ingredient
      name and field), full length — no truncation.
      Sorted by drug_id.
      Purpose: programmatic access / diff tooling.

  pipeline_outputs/eval_fda_reference.md
      One section per drug with a clear heading:
          ## Drug ID 12 — Warfarin Tablets
      Each sub-section shows the full text for drug_interactions, warnings,
      and boxed_warning per ingredient.
      Purpose: the file reviewers will actually read while working through
      eval_candidates_raw.csv.

Unlike fda_labels_export.csv (which intentionally truncates for a quick
structural scan), both files here are the COMPLETE reference — no char caps.

Usage (from backend/)
---------------------
    python scripts/export_fda_reference.py

Environment
-----------
    DATABASE_URL — via core/db/session.py (loads infra/.env)

Read-only — does not modify any table or any existing output file.
"""

import csv
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path / env setup
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parent.parent   # .../backend/
_ROOT_DIR = _BACKEND_DIR.parent                          # .../mediAid_langraph/

if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from dotenv import load_dotenv
load_dotenv(dotenv_path=_ROOT_DIR / "infra" / ".env")

# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------

OUT_DIR = _BACKEND_DIR / "pipeline_outputs"
OUT_DIR.mkdir(exist_ok=True)
OUT_CSV = OUT_DIR / "eval_fda_reference.csv"
OUT_MD  = OUT_DIR / "eval_fda_reference.md"

# ---------------------------------------------------------------------------
# CSV columns
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "drug_id",
    "brand_name",
    "generic_names",
    "drug_interactions",
    "warnings",
    "boxed_warning",
]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _sync_url(url: str) -> str:
    """Convert postgresql+asyncpg:// → postgresql:// for psycopg2."""
    return re.sub(r"^postgresql\+asyncpg://", "postgresql://", url)


def load_drugs() -> list[dict]:
    """
    Load all drugs with their per-ingredient FDA text.

    Returns a list sorted by drug_id, each entry shaped as::

        {
            "id": int,
            "brand_name": str,
            "dosage_form": str | None,
            "generics": [str, ...],
            "ingredients": [
                {
                    "generic_name": str,
                    "drug_interactions": str,
                    "warnings": str,
                    "boxed_warning": str,
                }
            ]
        }
    """
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        print(
            "ERROR: psycopg2 not installed. Run: pip install psycopg2-binary",
            file=sys.stderr,
        )
        sys.exit(1)

    from core.db.session import DATABASE_URL

    conn = psycopg2.connect(_sync_url(DATABASE_URL))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT
            d.id           AS drug_id,
            d.brand_name,
            d.dosage_form,
            di.generic_name,
            fl.drug_interactions,
            fl.warnings,
            fl.boxed_warning
        FROM drugs d
        JOIN drug_ingredients di ON di.drug_id = d.id
        LEFT JOIN fda_labels fl   ON fl.rxcui   = di.rxcui
        ORDER BY d.id, di.id
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()

    # Group by drug_id preserving id-order
    drugs: dict[int, dict] = {}
    for r in rows:
        did = r["drug_id"]
        if did not in drugs:
            drugs[did] = {
                "id": did,
                "brand_name": r["brand_name"],
                "dosage_form": r["dosage_form"],
                "generics": [],
                "ingredients": [],
            }
        ing_name = r["generic_name"] or ""
        if ing_name and ing_name not in drugs[did]["generics"]:
            drugs[did]["generics"].append(ing_name)

        drugs[did]["ingredients"].append({
            "generic_name": ing_name,
            "drug_interactions": r["drug_interactions"] or "",
            "warnings": r["warnings"] or "",
            "boxed_warning": r["boxed_warning"] or "",
        })

    return list(drugs.values())


# ---------------------------------------------------------------------------
# Text builders
# ---------------------------------------------------------------------------

def _concat_fda_text(ingredients: list[dict], field: str) -> str:
    """
    Concatenate the value of `field` across all ingredients, labelling each
    section with the ingredient name.  Sections separated by blank lines.
    Returns an empty string when no ingredient has data for that field.
    """
    parts = []
    for ing in ingredients:
        text = ing.get(field, "").strip()
        if text:
            parts.append(f"[{ing['generic_name']} — {field.replace('_', ' ').title()}]\n{text}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Export functions
# ---------------------------------------------------------------------------

def export_csv(drugs: list[dict]) -> None:
    """Write eval_fda_reference.csv — full, untruncated FDA text."""
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for drug in drugs:
            writer.writerow({
                "drug_id": drug["id"],
                "brand_name": drug["brand_name"],
                "generic_names": ", ".join(drug["generics"]),
                "drug_interactions": _concat_fda_text(drug["ingredients"], "drug_interactions"),
                "warnings": _concat_fda_text(drug["ingredients"], "warnings"),
                "boxed_warning": _concat_fda_text(drug["ingredients"], "boxed_warning"),
            })
    print(f"  ✓ {OUT_CSV.name}  ({len(drugs)} drugs)")


def export_markdown(drugs: list[dict]) -> None:
    """
    Write eval_fda_reference.md — one section per drug, human-readable.

    Structure::

        # FDA Label Reference — eval_fda_reference.md
        ...preamble...

        ---

        ## Drug ID 1 — Panadol Tablet
        **Generic names:** Paracetamol

        ### Drug Interactions
        [full text or *(none)*]

        ### Warnings
        [full text or *(none)*]

        ### Boxed Warning
        [full text or *(none)*]

        ---

        ## Drug ID 2 — ...
    """
    lines: list[str] = [
        "# FDA Label Reference — eval_fda_reference.md",
        "",
        "Generated from the `fda_labels` table.  Full, untruncated text — "
        "use this file when reviewing `eval_candidates_raw.csv`.",
        "",
        "**How to use:** find a drug by its `drug_id` "
        "(Ctrl+F → `## Drug ID <N> —`) to read its complete FDA label text.",
        "",
        f"Total drugs: {len(drugs)}",
        "",
    ]

    for drug in drugs:
        did = drug["id"]
        brand = drug["brand_name"]
        dosage = f" {drug['dosage_form']}" if drug["dosage_form"] else ""
        generics = ", ".join(drug["generics"]) if drug["generics"] else "*(none)*"

        lines.append("---")
        lines.append("")
        lines.append(f"## Drug ID {did} — {brand}{dosage}")
        lines.append("")
        lines.append(f"**Generic names:** {generics}")
        lines.append("")

        for field, heading in (
            ("drug_interactions", "Drug Interactions"),
            ("warnings", "Warnings"),
            ("boxed_warning", "Boxed Warning"),
        ):
            lines.append(f"### {heading}")
            lines.append("")
            # Collect text across all ingredients for this field
            sections: list[str] = []
            for ing in drug["ingredients"]:
                text = ing.get(field, "").strip()
                if text:
                    if len(drug["ingredients"]) > 1:
                        # Label by ingredient for combination drugs
                        sections.append(
                            f"**{ing['generic_name']}:**\n\n{text}"
                        )
                    else:
                        sections.append(text)

            if sections:
                lines.append("\n\n".join(sections))
            else:
                lines.append("*(none)*")
            lines.append("")

    md_content = "\n".join(lines)
    with open(OUT_MD, "w", encoding="utf-8") as fh:
        fh.write(md_content)
    print(f"  ✓ {OUT_MD.name}  ({len(drugs)} drugs)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"\n{'='*55}")
    print("  export_fda_reference — FDA label reference export")
    print(f"{'='*55}")

    drugs = load_drugs()
    if not drugs:
        print("ERROR: No drugs found in the database.", file=sys.stderr)
        sys.exit(1)

    print(f"  Loaded {len(drugs)} drugs from the database.")
    print(f"  Output directory: {OUT_DIR}")
    print()

    export_csv(drugs)
    export_markdown(drugs)

    # Summary stats
    drugs_with_interactions = sum(
        1 for d in drugs
        if any(ing["drug_interactions"] for ing in d["ingredients"])
    )
    drugs_with_warnings = sum(
        1 for d in drugs
        if any(ing["warnings"] for ing in d["ingredients"])
    )
    drugs_with_boxed = sum(
        1 for d in drugs
        if any(ing["boxed_warning"] for ing in d["ingredients"])
    )

    print()
    print(f"  Coverage summary:")
    print(f"    Drugs with drug_interactions text: {drugs_with_interactions}/{len(drugs)}")
    print(f"    Drugs with warnings text:          {drugs_with_warnings}/{len(drugs)}")
    print(f"    Drugs with boxed_warning text:     {drugs_with_boxed}/{len(drugs)}")
    print(f"{'='*55}")
    print()
    print("Reviewer tip:")
    print("  Open eval_fda_reference.md and use Ctrl+F → '## Drug ID <N>'")
    print("  to jump directly to any drug referenced in eval_candidates_raw.csv.")
    print()


if __name__ == "__main__":
    main()
