"""
Pakistani drug brand -> generic ingredient -> interaction data pipeline.

Chain: DRAP (brand -> composition) -> RxNorm (generic name normalization)
       -> openFDA (interaction/warning label text).

Design notes:
- Every external call is cached to disk (.cache/) so re-running this script
  after a partial failure doesn't re-hit already-resolved drugs.
  Cache granularity: one entry per brand name (the full DrugResolution result).
  Cache management is the CALLER's responsibility (this script); the library
  functions in core/clients/ and core/resolution.py always perform fresh HTTP
  calls and are cache-unaware.
- Every external call is rate-limited per-service and retried automatically
  on transient failures (connection errors, 429/5xx) via a shared session
  (managed in core/http_session.py).
- Every stage records an explicit Status rather than silently succeeding
  or failing, so batch output distinguishes "not found" from "API error"
  from "found but empty" from "normalization fell back to raw name".

For the full normalization-robustness design rationale (salt stripping,
approximate matching, spelling-variant retry) see the original inline
comments — the logic now lives in:
    core/clients/drap_client.py   — DRAP search, parse, resolve_brand()
    core/clients/rxnorm_client.py — get_rxnorm_name()
    core/clients/openfda_client.py — get_fda_label()
    core/resolution.py            — build_drug_record() orchestration
"""

import csv
import logging
from pathlib import Path

# ---------- Core library imports ----------
from core.cache import JsonCache
from core.resolution import build_drug_record
from core.status import Status
from core.types import DrugResolution, Ingredient  # noqa: F401 — Ingredient used in type hints

# ---------- Logging (pipeline-specific handler) ----------

CACHE_DIR = Path(__file__).resolve().parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("pipeline_run.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("drug_pipeline")


# ---------- Brand-level cache helpers ----------
# The batch script caches at the DrugResolution (brand) level: one JSON entry
# per brand, keyed by brand name.  This is coarser than the original per-API-call
# granularity but is equivalent for reruns: if a brand resolved successfully,
# re-running skips all three HTTP stages.  The cache files from the library's
# internal caches (drap/rxnorm/openfda) are no longer used by this script.

def _resolution_to_dict(r: DrugResolution) -> dict:
    """Serialise a ``DrugResolution`` to a JSON-compatible dict."""
    return {
        "brand_query": r.brand_query,
        "status": r.status.value,
        "matched_name": r.matched_name,
        "drap_reg_no": r.drap_reg_no,
        "candidate_count": r.candidate_count,
        "spelling_variant_used": r.spelling_variant_used,
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


def _resolution_from_dict(d: dict) -> DrugResolution:
    """Reconstruct a ``DrugResolution`` from a previously serialised dict."""
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
    return DrugResolution(
        brand_query=d["brand_query"],
        status=Status(d["status"]),
        matched_name=d.get("matched_name"),
        drap_reg_no=d.get("drap_reg_no"),
        candidate_count=d.get("candidate_count", 0),
        spelling_variant_used=d.get("spelling_variant_used"),
        error=d.get("error"),
        ingredients=ingredients,
    )


# ---------- Batch runner -> CSV ----------

CSV_FIELDS = [
    "brand_query", "resolution_status", "matched_name", "drap_reg_no", "candidate_count",
    "spelling_variant_used",
    "ingredient_name", "drap_raw_name", "name_was_stripped", "ingredient_amount",
    "rxcui", "rxnorm_name", "normalization_status",
    "fda_status", "drug_interactions", "warnings", "boxed_warning", "error",
]


def run_batch(brand_names: list[str], output_csv: str) -> None:
    """
    Resolve each brand in *brand_names* and write one CSV row per ingredient to *output_csv*.

    A brand-level disk cache (``CACHE_DIR/resolutions.json``) is checked before
    calling ``build_drug_record()``.  On a cache miss the result is written back
    so subsequent reruns skip already-resolved brands.
    """
    resolution_cache = JsonCache("resolutions", CACHE_DIR)

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()

        for brand in brand_names:
            logger.info("Processing '%s'...", brand)

            # --- Cache check ---
            cached = resolution_cache.get(brand)
            if cached is not None:
                logger.info("[Cache] hit for '%s'", brand)
                record = _resolution_from_dict(cached)
            else:
                record = build_drug_record(brand)
                resolution_cache.set(brand, _resolution_to_dict(record))

            # --- Write CSV rows ---
            base_row = {
                "brand_query": record.brand_query,
                "resolution_status": record.status.value,
                "matched_name": record.matched_name,
                "drap_reg_no": record.drap_reg_no,
                "candidate_count": record.candidate_count,
                "spelling_variant_used": record.spelling_variant_used,
                "error": record.error,
            }

            if not record.ingredients:
                writer.writerow(base_row)
                continue

            for ing in record.ingredients:
                writer.writerow({
                    **base_row,
                    "ingredient_name": ing.name,
                    "drap_raw_name": ing.drap_raw_name,
                    "name_was_stripped": ing.name_was_stripped,
                    "ingredient_amount": ing.amount,
                    "rxcui": ing.rxcui,
                    "rxnorm_name": ing.rxnorm_name,
                    "normalization_status": ing.normalization_status.value,
                    "fda_status": ing.fda_status.value,
                    "drug_interactions": ing.drug_interactions,
                    "warnings": ing.warnings,
                    "boxed_warning": ing.boxed_warning,
                })

    logger.info("Batch complete — results written to %s", output_csv)


if __name__ == "__main__":
    TEST_DRUGS = [
        "Panadol", "Disprin", "Brufen", "Augmentin", "Ponstan",
        "Glucophage", "Concor", "Lipitor", "Amryl", "Voltral",
    ]
    run_batch(TEST_DRUGS, "drap_pipeline_results.csv")