"""
NEML 2025 generic-name-first batch resolution script.

Reverse direction from ``scripts/create_dataset.py``:

  Generic name (NEML 2025)
      → DRAP search by generic name
      → Representative brand selection
      → Composition verification (does this brand actually contain the generic?)
      → RxNorm normalization  (unchanged library function)
      → openFDA interaction data  (unchanged library function)
      → CSV for manual review

Pipeline reuse:
  core/generic_resolution.py     — build_generic_record
  core/cache.py                  — JsonCache (same pattern as create_dataset.py)
"""

import csv
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Need to add backend_dir to path so imports work correctly when run as script
import sys
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from core.cache import JsonCache
from core.status import Status
from core.types import Ingredient
from core.generic_resolution import build_generic_record, detect_combination

# ---------- Paths ----------

CACHE_DIR = SCRIPT_DIR / ".cache"
CACHE_DIR.mkdir(exist_ok=True)

NEML_GENERICS_JSON = SCRIPT_DIR.parent.parent / "data" / "neml_generics.json"

# ---------- Logging ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("neml_pipeline_run.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("neml_pipeline")

# ---------- Result type ----------

@dataclass
class NemlResolution:
    """Result of resolving one NEML generic name through the full pipeline."""

    searched_generic: str
    is_combination: bool
    drap_candidate_count: int
    overall_status: str          # "ok", "no_drap_match", "composition_mismatch", "api_error", …
    selected_brand_name: Optional[str] = None
    drap_reg_no: Optional[str] = None
    selection_reason: Optional[str] = None
    composition_verified: Optional[bool] = None
    composition_similarity_score: Optional[float] = None
    error: Optional[str] = None
    ingredients: list[Ingredient] = field(default_factory=list)


# ---------- Pipeline wrapper ----------

def resolve_neml_generic(generic_name: str) -> NemlResolution:
    """
    Resolve a single NEML generic name through the full pipeline.

    Returns a ``NemlResolution`` regardless of outcome — failed stages set
    ``overall_status`` and ``error`` but never raise exceptions.  The result
    is always safe to write to CSV.
    """
    is_combination, _ = detect_combination(generic_name)
    record = build_generic_record(generic_name)
    
    status_map = {
        Status.OK: "ok",
        Status.NOT_FOUND: "no_drap_match",
        Status.API_ERROR: "api_error",
        Status.EMPTY_DATA: "empty_data",
        Status.COMPOSITION_MISMATCH: "composition_mismatch",
        Status.COMBINATION_COMPONENT_MISMATCH: "combination_component_mismatch",
    }
    overall_status = status_map.get(record.status, record.status.value)
    
    return NemlResolution(
        searched_generic=generic_name,
        is_combination=is_combination,
        drap_candidate_count=record.candidate_count,
        overall_status=overall_status,
        selected_brand_name=record.matched_name,
        drap_reg_no=record.drap_reg_no,
        selection_reason=record.selection_reason,
        composition_verified=record.composition_verified,
        composition_similarity_score=record.composition_similarity_score,
        error=record.error,
        ingredients=record.ingredients,
    )


# ---------- Cache serialisation ----------

def _resolution_to_dict(r: NemlResolution) -> dict:
    """Serialise ``NemlResolution`` to a JSON-compatible dict."""
    return {
        "searched_generic": r.searched_generic,
        "is_combination": r.is_combination,
        "drap_candidate_count": r.drap_candidate_count,
        "overall_status": r.overall_status,
        "selected_brand_name": r.selected_brand_name,
        "drap_reg_no": r.drap_reg_no,
        "selection_reason": r.selection_reason,
        "composition_verified": r.composition_verified,
        "composition_similarity_score": r.composition_similarity_score,
        "error": r.error,
        "ingredients": [
            {
                "name": ing.name,
                "amount": ing.amount,
                "drap_raw_name": ing.drap_raw_name,
                "name_was_stripped": ing.name_was_stripped,
                "rxcui": ing.rxcui,
                "rxnorm_name": ing.rxnorm_name,
                "normalization_status": ing.normalization_status.value,
                "fda_status": ing.fda_status.value,
                "drug_interactions": ing.drug_interactions,
                "warnings": ing.warnings,
                "boxed_warning": ing.boxed_warning,
            }
            for ing in r.ingredients
        ],
    }


def _resolution_from_dict(d: dict) -> NemlResolution:
    """Reconstruct a ``NemlResolution`` from a cached dict."""
    ingredients = [
        Ingredient(
            name=ing["name"],
            amount=ing["amount"],
            drap_raw_name=ing["drap_raw_name"],
            name_was_stripped=ing["name_was_stripped"],
            rxcui=ing["rxcui"],
            rxnorm_name=ing["rxnorm_name"],
            normalization_status=Status(ing["normalization_status"]),
            fda_status=Status(ing["fda_status"]),
            drug_interactions=ing["drug_interactions"],
            warnings=ing["warnings"],
            boxed_warning=ing["boxed_warning"],
        )
        for ing in d.get("ingredients", [])
    ]
    return NemlResolution(
        searched_generic=d["searched_generic"],
        is_combination=d["is_combination"],
        drap_candidate_count=d["drap_candidate_count"],
        overall_status=d["overall_status"],
        selected_brand_name=d.get("selected_brand_name"),
        drap_reg_no=d.get("drap_reg_no"),
        selection_reason=d.get("selection_reason"),
        composition_verified=d.get("composition_verified"),
        composition_similarity_score=d.get("composition_similarity_score"),
        error=d.get("error"),
        ingredients=ingredients,
    )


# ---------- CSV ----------

CSV_FIELDS = [
    "searched_generic", "is_combination", "drap_candidate_count",
    "selected_brand_name", "drap_reg_no", "selection_reason",
    "composition_verified", "composition_similarity_score",
    "ingredient_name", "ingredient_amount",
    "rxcui", "rxnorm_name", "normalization_status",
    "fda_status", "drug_interactions", "warnings", "boxed_warning",
    "error",
]


def _write_rows(writer: "csv.DictWriter[str]", r: NemlResolution) -> None:
    """Write one CSV row per ingredient (or one error row if no ingredients)."""
    base: dict = {
        "searched_generic": r.searched_generic,
        "is_combination": r.is_combination,
        "drap_candidate_count": r.drap_candidate_count,
        "selected_brand_name": r.selected_brand_name,
        "drap_reg_no": r.drap_reg_no,
        "selection_reason": r.selection_reason,
        "composition_verified": r.composition_verified,
        "composition_similarity_score": (
            f"{r.composition_similarity_score:.4f}"
            if r.composition_similarity_score is not None
            else None
        ),
        "error": r.error,
    }

    if not r.ingredients:
        writer.writerow(base)
        return

    for ing in r.ingredients:
        writer.writerow({
            **base,
            "ingredient_name": ing.name,
            "ingredient_amount": ing.amount,
            "rxcui": ing.rxcui,
            "rxnorm_name": ing.rxnorm_name,
            "normalization_status": ing.normalization_status.value,
            "fda_status": ing.fda_status.value,
            "drug_interactions": ing.drug_interactions,
            "warnings": ing.warnings,
            "boxed_warning": ing.boxed_warning,
        })


# ---------- Batch runner ----------

def load_generics(json_path: Path) -> list[str]:
    """Load the list of generic names from a JSON array file."""
    return json.loads(json_path.read_text(encoding="utf-8"))


def run_neml_batch(generics_json: Path, output_csv: str) -> dict[str, int]:
    """
    Resolve each generic in *generics_json* and write results to *output_csv*.

    A generic-level disk cache (``CACHE_DIR/neml_resolutions.json``) is checked
    first; on a miss the result is resolved and cached for subsequent reruns.

    Returns a ``status → count`` dict so callers can log or assert on it.
    """
    generics = load_generics(generics_json)
    logger.info("Loaded %d generic(s) from %s", len(generics), generics_json)

    cache = JsonCache("neml_resolutions", CACHE_DIR)
    status_counts: dict[str, int] = {}

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()

        for generic in generics:
            logger.info("─── Processing '%s' ───", generic)

            cached = cache.get(generic)
            if cached is not None:
                logger.info("[Cache] hit for '%s'", generic)
                resolution = _resolution_from_dict(cached)
            else:
                resolution = resolve_neml_generic(generic)
                cache.set(generic, _resolution_to_dict(resolution))

            status_counts[resolution.overall_status] = (
                status_counts.get(resolution.overall_status, 0) + 1
            )
            _write_rows(writer, resolution)

    # ── Summary ─────────────────────────────────────────────────────────────
    total = len(generics)
    logger.info("")
    logger.info("═══════════════════════════════════════════════")
    logger.info("  NEML batch complete — %d generics processed", total)
    logger.info("  Output: %s", output_csv)
    logger.info("  Status summary:")
    for status, count in sorted(status_counts.items(), key=lambda x: -x[1]):
        bar = "█" * count
        logger.info("    %-45s %3d  %s", status, count, bar)
    logger.info("═══════════════════════════════════════════════")

    return status_counts


if __name__ == "__main__":
    run_neml_batch(NEML_GENERICS_JSON, "neml_pipeline_results.csv")
