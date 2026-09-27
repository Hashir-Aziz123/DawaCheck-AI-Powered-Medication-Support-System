"""
Manual verification script for the LangGraph interaction-checking pipeline.

v3: Extended with Sertraline + Tramadol (Zoloft + Vexnil) pair to verify
    class-level interaction detection (expected: interaction_found, match_type=class_level).

Test pairs:
  Pair 1: Warfarin + Aspirin       -- RERUN after citation-quality fix.
                                     Original run accepted a bare name list as grounded.
                                     Expected after fix: either a proper effect-stating
                                     sentence is cited, OR system returns none_found /
                                     unverifiable. Bare list accepted as grounded = FAIL.

  Pair 2: Panadol + Omeprazole     -- RERUN. Should remain none_found (control -- unchanged).

  Pair 3: Mefenamic Acid + Warfarin -- NSAIDs + anticoagulant. FDA prose sections for
                                       NSAIDs typically contain explicit effect sentences
                                       (e.g., "may potentiate the anticoagulant effect").
                                       Expected: interaction_found with a proper sentence
                                       citation, or none_found if no prose in this DB drug's
                                       FDA text. Should NOT produce a bare-list citation.

  Pair 4: Aspirin + Ibuprofen      -- Two NSAIDs. Likely to appear in each other's
                                       drug-class lists without explicit interaction prose.
                                       Expected: none_found or unverifiable (not a grounded
                                       interaction_found from a bare list).

  Pair 5: Zoloft + Vexnil          -- Sertraline (SSRI) + Tramadol (opioid + serotonergic).
                                       Tramadol's label warns about "serotonergic agents";
                                       sertraline is classified as an SSRI / Serotonin
                                       Uptake Inhibitor. Expected AFTER class-level fix:
                                         final_status: interaction_found
                                         match_type:   class_level
                                       If still none_found: class-level detection is not working.

Usage (from e:\\mediAid_langraph\\backend\\):
    python scripts/test_interaction_check.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.db.session import DATABASE_URL
from app.services.drug_resolution import resolve_drug
from app.schemas.resolve import DrugResolutionResult
from worker.langgraph_pipeline.graph import run_interaction_check


def drug_result_to_dict(result: DrugResolutionResult) -> dict:
    ingredients = []
    for ing in result.ingredients:
        d = ing.model_dump()
        # drug_classes may not be present on older schema rows -- default to empty
        if "drug_classes" not in d:
            d["drug_classes"] = []
        ingredients.append(d)
    return {
        "brand_name": result.brand_name,
        "dosage_form": result.dosage_form,
        "ingredients": ingredients,
    }


def resolve_to_dict(query: str, session: Session) -> dict | None:
    result = resolve_drug(query, session)
    if result.status not in ("found",):
        print(f"  WARNING: Could not resolve '{query}': status={result.status}")
        return None
    has_fda = any(
        any([ing.drug_interactions, ing.warnings, ing.boxed_warning])
        for ing in result.ingredients
    )
    has_classes = any(
        getattr(ing, "drug_classes", None)
        for ing in result.ingredients
    )
    if not has_fda:
        print(
            f"  WARNING: '{query}' resolved as '{result.brand_name}' but has NO FDA label data "
            f"-- interaction check will be uninformative (expected: none_found)."
        )
    else:
        class_info = f" | classes: {[c for ing in result.ingredients for c in getattr(ing, 'drug_classes', [])]}" if has_classes else ""
        print(f"  OK: '{query}' -> '{result.brand_name}' (has FDA data){class_info}")
    return drug_result_to_dict(result)


def print_result(label: str, result: dict) -> None:
    sep = "=" * 68
    print(f"\n{sep}")
    print(f"  {label}")
    print(f"  {result['drug_a']}  <->  {result['drug_b']}")
    print(sep)
    print(f"  Final Status         : {result['final_status']}")
    print(f"  Match Type           : {result.get('match_type') or '(n/a)'}")
    print(f"  Is Grounded          : {result.get('is_grounded')}")
    print()
    print(f"  Claim:\n    {result.get('interaction_claim') or '(none)'}")
    print()
    print(f"  Citation:\n    {result.get('citation_text') or '(none)'}")
    print()
    print(f"  Groundedness Reasoning:\n    {result.get('groundedness_reasoning') or '(none)'}")
    print()


def run_pair(label: str, drug_a_name: str, drug_b_name: str, session: Session) -> None:
    print(f"\n{'---' * 23}")
    print(f"Resolving '{drug_a_name}' and '{drug_b_name}'...")
    drug_a = resolve_to_dict(drug_a_name, session)
    drug_b = resolve_to_dict(drug_b_name, session)
    if not drug_a or not drug_b:
        print(f"  SKIPPED -- resolution failed.\n")
        return
    print(f"  Running interaction check...")
    result = run_interaction_check(drug_a, drug_b)
    print_result(label, result)


def main():
    sync_url = re.sub(r"^postgresql\+asyncpg://", "postgresql://", DATABASE_URL)
    engine = create_engine(sync_url)

    pairs = [
        (
            "PAIR 1 -- RERUN: Warfarin + Aspirin  [expect: proper sentence OR none_found -- bare list must NOT pass]",
            "Warfarin",
            "Aspirin",
        ),
        (
            "PAIR 2 -- CONTROL: Panadol + Omeprazole  [expect: none_found, unchanged]",
            "Panadol",
            "Omeprazole",
        ),
        (
            "PAIR 3 -- PROSE SENTENCE: Mefenamic Acid + Warfarin  [expect: grounded with full sentence, or none_found if no prose in DB]",
            "Mefenamic Acid",
            "Warfarin",
        ),
        (
            "PAIR 4 -- TABLE-ONLY: Aspirin + Ibuprofen  [expect: none_found or unverifiable -- bare list must NOT pass]",
            "Aspirin",
            "Ibuprofen",
        ),
        (
            "PAIR 5 -- CLASS-LEVEL: Zoloft + Vexnil (Sertraline + Tramadol)  [expect: interaction_found, match_type=class_level]",
            "Zoloft",
            "Vexnil",
        ),
    ]

    with Session(engine) as session:
        for label, drug_a_name, drug_b_name in pairs:
            run_pair(label, drug_a_name, drug_b_name, session)


if __name__ == "__main__":
    main()
