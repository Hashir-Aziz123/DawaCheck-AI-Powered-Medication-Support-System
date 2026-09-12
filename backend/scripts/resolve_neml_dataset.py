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

**Design notes:**
- Combination generics ("amoxicillin + clavulanic acid") are detected via the
  "+" separator.  The DRAP search uses the *lead* component (first part before
  "+").  Composition verification then requires *both* named components to be
  present in the resolved product.
- Parenthetical aliases ("paracetamol (acetaminophen)") are stripped from the
  query before DRAP search; the original name is preserved in ``searched_generic``.
- A stricter composition-similarity threshold (0.80) is used vs the brand
  spelling-retry threshold (0.65), since this comparison needs only to absorb
  salt-form differences, not genuine typos.
- Failed composition verifications are kept in the CSV (``composition_verified=False``);
  they are never silently dropped — flag, don't discard.
- No Postgres writes in this script.  Output is CSV only.

Pipeline reuse:
  core/clients/drap_client.py   — search_drap (extended), get_drug_detail,
                                   parse_composition, preprocess_ingredient_name
  core/clients/rxnorm_client.py — get_rxnorm_name (unchanged)
  core/clients/openfda_client.py — get_fda_label, extract_interaction_text (unchanged)
  core/cache.py                  — JsonCache (same pattern as create_dataset.py)
"""

import csv
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.cache import JsonCache
from core.clients.drap_client import (
    get_drug_detail,
    parse_composition,
    preprocess_ingredient_name,
    search_drap,
)
from core.clients.openfda_client import extract_interaction_text, get_fda_label
from core.clients.rxnorm_client import get_rxnorm_name
from core.status import Status
from core.types import Ingredient

# ---------- Paths ----------

SCRIPT_DIR = Path(__file__).resolve().parent
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

# ---------- Levenshtein (rapidfuzz preferred; pure-Python fallback) ----------
# Re-implemented locally to avoid importing private symbols from drap_client.
# Identical logic to the version already used there.
try:
    from rapidfuzz.distance import Levenshtein as _RFLev

    def _lev_dist(a: str, b: str) -> int:
        """Edit distance via rapidfuzz."""
        return _RFLev.distance(a, b)

except ImportError:  # pragma: no cover
    def _lev_dist(a: str, b: str) -> int:  # type: ignore[misc]
        """Pure-Python Wagner-Fischer fallback."""
        m, n = len(a), len(b)
        dp = list(range(n + 1))
        for i in range(1, m + 1):
            prev, dp[0] = dp[0], i
            for j in range(1, n + 1):
                prev, dp[j] = dp[j], min(
                    dp[j] + 1,
                    dp[j - 1] + 1,
                    prev + (a[i - 1] != b[j - 1]),
                )
        return dp[n]


# ---------- Composition similarity (full-string, no first-token truncation) ----------
# Distinct from _spelling_similarity() in drap_client, which uses first-token logic
# suited for brand-name comparisons.  Here we compare full cleaned generic names
# against full cleaned ingredient names, so full-string ratio is correct.

def _composition_similarity(a: str, b: str) -> float:
    """
    Full-string Levenshtein ratio between *a* and *b* (both lowercased/stripped).

    Returns a float in [0.0, 1.0].  1.0 means identical.
    """
    a = a.lower().strip()
    b = b.lower().strip()
    dist = _lev_dist(a, b)
    return 1.0 - dist / max(len(a), len(b), 1)


# ---------- Thresholds ----------

#: Minimum full-string similarity for composition verification.
#: Stricter than the brand spelling-retry threshold (0.65) — this comparison
#: should only need to absorb salt-form / formatting differences, not typos.
COMPOSITION_SIM_THRESHOLD: float = 0.80

# ---------- Brand selection preferences ----------

#: DRAP candidate status values treated as "active" for selection priority.
_PREFERRED_STATUSES: frozenset[str] = frozenset({"active", "provisionally active"})

#: Keywords in candidate text that indicate an oral dosage form (preferred).
_PREFERRED_FORM_KW: tuple[str, ...] = ("tablet", "capsule", "syrup", "oral", "solution")

#: Keywords indicating non-oral/niche forms (deprioritised).
_DEPRIORITISED_FORM_KW: tuple[str, ...] = (
    "injection", "infusion", "eye drop", "ophthalmic", "ointment",
    "cream", "inhaler", "suppository", "drop",
)

#: Manual override: map generic name (lowercase) → list of DRAP reg_no values to force-select.
#: Populate to test that multiple brands of the same generic normalise identically.
#: Example: ``{"metformin": ["DRAP-REG-001", "DRAP-REG-002"]}``
MULTI_BRAND_OVERRIDES: dict[str, list[str]] = {}

#: Secondary search aliases for generics that DRAP lists under a different
#: naming convention than the NEML name.  Key is the NEML generic name or
#: its lead component (lowercase); values are tried in order if the primary
#: search returns no candidates.
#:
#: Example: NEML says "sulfamethoxazole + trimethoprim"; DRAP uses "cotrimoxazole".
GENERIC_ALIASES: dict[str, list[str]] = {
    "sulfamethoxazole": ["cotrimoxazole", "co-trimoxazole", "trimethoprim"],
    "glyceryl trinitrate": ["nitroglycerin", "nitroglycerine", "gtnt"],
    "isosorbide dinitrate": ["isordil"],
    "acetylsalicylic acid": ["aspirin"],
    "colecalciferol": ["cholecalciferol", "vitamin d3"],
    "hydroxocobalamin": ["cyanocobalamin", "vitamin b12"],
}

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


# ---------- Regex helpers ----------

_PAREN_RE = re.compile(r"\s*\([^)]*\)")


def _strip_parens(name: str) -> str:
    """Remove parenthetical aliases: ``"paracetamol (acetaminophen)"`` → ``"paracetamol"``."""
    return _PAREN_RE.sub("", name).strip()


# ---------- Pipeline stages ----------

def detect_combination(name: str) -> tuple[bool, list[str]]:
    """
    Detect whether *name* is a combination generic (contains ``"+"``).

    Returns ``(is_combination, [cleaned_components])``.  Each component has
    parenthetical aliases stripped and extra whitespace removed.

    Examples::

        detect_combination("amoxicillin + clavulanic acid")
            → (True, ["amoxicillin", "clavulanic acid"])
        detect_combination("paracetamol (acetaminophen)")
            → (False, ["paracetamol"])
    """
    if "+" in name:
        components = [_strip_parens(c.strip()) for c in name.split("+")]
        components = [c for c in components if c]
        return True, components
    return False, [_strip_parens(name)]


def select_representative_brand(
    candidates: list[dict],
    generic_name: str,
    *,
    is_combination: bool = False,
    components: Optional[list[str]] = None,
) -> tuple[dict, str]:
    """
    Choose the single best DRAP candidate for *generic_name* from *candidates*.

    Selection priority (highest → lowest):

    1. **Composition completeness** (combination generics only) — candidates
       whose ``text`` field contains ALL named components score +1 over those
       that only partially match.  This ensures a combined product
       (e.g. amoxicillin + clavulanic acid) is preferred over a single-
       ingredient product when both appear in the same search result set.
    2. ``status`` field (if present in candidate dict) — prefer "Active" /
       "Provisionally Active" over other values.
    3. Dosage form — oral forms (Tablet, Capsule, Syrup …) preferred over
       injectable / ophthalmic / topical forms.
    4. Full-string Levenshtein similarity of candidate text to *generic_name* —
       tie-breaker ensuring the most name-similar product wins.
    5. ``MULTI_BRAND_OVERRIDES`` — if *generic_name* is in the override dict,
       the listed reg_no(s) are forced regardless of the above.

    Returns ``(chosen_candidate_dict, reason_string)``.
    """
    if not candidates:
        raise ValueError("select_representative_brand called with empty candidates list")

    if len(candidates) == 1:
        return candidates[0], "only match"

    # Manual override check
    key = generic_name.lower()
    if key in MULTI_BRAND_OVERRIDES:
        override_ids = set(MULTI_BRAND_OVERRIDES[key])
        overridden = [c for c in candidates if c.get("id") in override_ids]
        if overridden:
            logger.info(
                "[BrandSelect] Manual override for '%s': %d candidate(s) selected",
                generic_name, len(overridden),
            )
            return overridden[0], "multi-brand override"

    # Normalised component names for composition-completeness scoring
    _comps_lower = [c.lower() for c in (components or [])] if is_combination else []

    def _score(c: dict) -> tuple:
        text = c.get("text", "").lower()
        status = c.get("status", "").lower()

        # Tier 0 (combination only): composition completeness
        # +1 if the candidate text contains every named component, 0 otherwise.
        # This is a coarse text-presence check — a full composition fetch would
        # be more accurate but would require N extra HTTP requests per call.
        # For the common case (Augmentin vs plain Amoxicillin), the combination
        # product name includes both "amoxicillin" and "clavulan" in its text.
        if _comps_lower:
            combo_score = int(all(comp in text for comp in _comps_lower))
        else:
            combo_score = 0

        # Tier 1: preferred status (2 = preferred, 1 = unknown/missing, 0 = other)
        if status in _PREFERRED_STATUSES:
            status_score = 2
        elif not status:
            status_score = 1
        else:
            status_score = 0

        # Tier 2: dosage form (2 = preferred oral, 1 = unknown, 0 = deprioritised)
        if any(kw in text for kw in _PREFERRED_FORM_KW):
            form_score = 2
        elif any(kw in text for kw in _DEPRIORITISED_FORM_KW):
            form_score = 0
        else:
            form_score = 1

        # Tier 3: similarity to generic name (float in [0, 1])
        sim = _composition_similarity(generic_name, c.get("text", ""))

        return (combo_score, status_score, form_score, sim)

    best = max(candidates, key=_score)
    s = _score(best)

    # Build human-readable reason
    reason_parts: list[str] = []
    best_text = best.get("text", "").lower()
    best_status = best.get("status", "").lower()

    if is_combination and s[0]:
        reason_parts.append("composition-complete")
    if best_status in _PREFERRED_STATUSES:
        reason_parts.append(best_status)
    matched_form = next((kw for kw in _PREFERRED_FORM_KW if kw in best_text), None)
    if matched_form:
        reason_parts.append(matched_form)

    n = len(candidates)
    suffix = f" (of {n})" if n > 1 else ""
    reason = (" + ".join(reason_parts) + suffix) if reason_parts else f"first of {n}"

    logger.info(
        "[BrandSelect] '%s' → '%s' | reason: %s | scores: combo=%d status=%d form=%d sim=%.3f",
        generic_name, best.get("text", "?"), reason, s[0], s[1], s[2], s[3],
    )
    return best, reason


def verify_composition(
    searched_generic: str,
    is_combination: bool,
    components: list[str],
    parsed_ings: list[dict],
) -> tuple[bool, float, str]:
    """
    Check that the resolved product's composition actually contains the expected ingredient(s).

    Algorithm:

    1. Both the searched generic component(s) and each parsed ingredient name are
       pre-processed through ``preprocess_ingredient_name()`` (strips salt qualifiers /
       "EQ TO" suffixes) so "AMOXICILLIN TRIHYDRATE" matches "amoxicillin".
    2. Full-string Levenshtein similarity (``_composition_similarity``) is computed
       between each cleaned component and each cleaned ingredient.
    3. Single generics: accepted if any ingredient scores ≥ ``COMPOSITION_SIM_THRESHOLD``.
    4. Combinations: *each* named component must individually match a *different*
       ingredient ≥ threshold.  If any component is missing → ``combination_component_mismatch``.

    Returns ``(verified: bool, best_score: float, status_str: str)``.

    **Failed verifications are not silently dropped** — the caller always writes the
    row to CSV with ``composition_verified=False`` for manual review.
    """

    def _clean(raw: str) -> str:
        cleaned, _ = preprocess_ingredient_name(raw.upper())
        return cleaned.lower()

    if not parsed_ings:
        return False, 0.0, Status.COMPOSITION_MISMATCH.value

    cleaned_ings = [_clean(ing["name"]) for ing in parsed_ings if ing.get("name")]

    if not is_combination:
        # Single generic — one component
        search_clean = _clean(components[0])
        scores = [_composition_similarity(search_clean, ing) for ing in cleaned_ings]
        best_score = max(scores) if scores else 0.0

        if best_score >= COMPOSITION_SIM_THRESHOLD:
            logger.info(
                "[Verify] OK: '%s' → best_score=%.3f (threshold=%.2f)",
                searched_generic, best_score, COMPOSITION_SIM_THRESHOLD,
            )
            return True, best_score, Status.OK.value
        else:
            logger.warning(
                "[Verify] MISMATCH: searched='%s' (cleaned='%s'), best_score=%.3f "
                "(threshold=%.2f). Ingredients: %s",
                searched_generic, search_clean, best_score, COMPOSITION_SIM_THRESHOLD,
                [ing["name"] for ing in parsed_ings],
            )
            return False, best_score, Status.COMPOSITION_MISMATCH.value

    else:
        # Combination — require each component to match a distinct ingredient
        matched_idxs: set[int] = set()
        component_results: list[tuple[str, bool, float]] = []

        for component in components:
            comp_clean = _clean(component)
            # Find the best unmatched ingredient for this component
            best_idx, best_score = max(
                ((i, _composition_similarity(comp_clean, ing))
                 for i, ing in enumerate(cleaned_ings)
                 if i not in matched_idxs),
                key=lambda x: x[1],
                default=(None, 0.0),
            )
            if best_idx is not None and best_score >= COMPOSITION_SIM_THRESHOLD:
                matched_idxs.add(best_idx)
                component_results.append((component, True, best_score))
            else:
                component_results.append((component, False, best_score if best_idx is not None else 0.0))

        all_matched = all(ok for _, ok, _ in component_results)
        overall_best = max((s for _, _, s in component_results), default=0.0)

        if all_matched:
            logger.info(
                "[Verify] COMBINATION OK: '%s' — all %d component(s) matched. scores=%s",
                searched_generic, len(components),
                [(c, f"{s:.3f}") for c, _, s in component_results],
            )
            return True, overall_best, Status.OK.value
        else:
            unmatched = [c for c, ok, _ in component_results if not ok]
            logger.warning(
                "[Verify] COMBINATION MISMATCH: '%s' — unmatched: %s. scores=%s. "
                "Ingredients: %s",
                searched_generic, unmatched,
                [(c, f"{s:.3f}") for c, _, s in component_results],
                [ing["name"] for ing in parsed_ings],
            )
            return False, overall_best, Status.COMBINATION_COMPONENT_MISMATCH.value


def resolve_neml_generic(generic_name: str) -> NemlResolution:
    """
    Resolve a single NEML generic name through the full pipeline.

    Returns a ``NemlResolution`` regardless of outcome — failed stages set
    ``overall_status`` and ``error`` but never raise exceptions.  The result
    is always safe to write to CSV.
    """
    is_combination, components = detect_combination(generic_name)
    lead_query = components[0]  # search DRAP with first (or only) component

    logger.info(
        "[NEML] Resolving '%s' | is_combination=%s | lead_query='%s'",
        generic_name, is_combination, lead_query,
    )

    # ── Stage 1: DRAP search by generic name ────────────────────────────────
    candidates, err = search_drap(lead_query, search_type="generic name")
    if err:
        logger.error("[NEML] DRAP search API error for '%s': %s", generic_name, err)
        return NemlResolution(
            searched_generic=generic_name,
            is_combination=is_combination,
            drap_candidate_count=0,
            overall_status=Status.API_ERROR.value,
            error=err,
        )

    if not candidates:
        # ── F5: Alias / component fallback before giving up ──────────────────
        # Try components[1] first (e.g. for "sulfamethoxazole + trimethoprim",
        # search "trimethoprim" if "sulfamethoxazole" found nothing).
        fallback_queries: list[str] = []
        if is_combination and len(components) > 1:
            fallback_queries.append(components[1])
        # Then try any registered aliases for the lead component.
        for alias in GENERIC_ALIASES.get(lead_query.lower(), []):
            fallback_queries.append(alias)

        for fb_query in fallback_queries:
            logger.info(
                "[NEML] No results for '%s' — trying fallback query '%s'",
                generic_name, fb_query,
            )
            candidates, err = search_drap(fb_query, search_type="generic name")
            if err:
                logger.error(
                    "[NEML] DRAP search API error on fallback '%s': %s", fb_query, err
                )
                continue
            if candidates:
                logger.info(
                    "[NEML] Fallback '%s' → %d DRAP candidate(s): %s",
                    fb_query, len(candidates),
                    [c.get("text", "?") for c in candidates[:5]],
                )
                lead_query = fb_query  # use the successful query for brand-select
                break

    if not candidates:
        logger.info("[NEML] No DRAP results for generic '%s' (all fallbacks exhausted)", generic_name)
        return NemlResolution(
            searched_generic=generic_name,
            is_combination=is_combination,
            drap_candidate_count=0,
            overall_status="no_drap_match",
            error="No DRAP results found for this generic name",
        )

    logger.info(
        "[NEML] '%s' → %d DRAP candidate(s): %s",
        generic_name, len(candidates), [c.get("text", "?") for c in candidates[:5]],
    )

    # ── Stage 2: Brand selection ─────────────────────────────────────────────
    chosen, selection_reason = select_representative_brand(
        candidates, lead_query,
        is_combination=is_combination,
        components=components,
    )

    # ── Stage 3: Detail + composition ───────────────────────────────────────
    detail, err = get_drug_detail(chosen["id"])
    if err:
        return NemlResolution(
            searched_generic=generic_name,
            is_combination=is_combination,
            drap_candidate_count=len(candidates),
            selected_brand_name=chosen.get("text"),
            drap_reg_no=chosen.get("id"),
            selection_reason=selection_reason,
            overall_status=Status.API_ERROR.value,
            error=err,
        )

    parsed_comp = detail.get("Composition", [])
    if not parsed_comp:
        return NemlResolution(
            searched_generic=generic_name,
            is_combination=is_combination,
            drap_candidate_count=len(candidates),
            selected_brand_name=chosen.get("text"),
            drap_reg_no=chosen.get("id"),
            selection_reason=selection_reason,
            overall_status=Status.EMPTY_DATA.value,
            error="DRAP record found but composition was empty",
        )

    # ── Stage 4: Composition verification ───────────────────────────────────
    comp_verified, comp_score, verify_status = verify_composition(
        generic_name, is_combination, components, parsed_comp,
    )

    # ── Stages 5 & 6: RxNorm + openFDA per ingredient ───────────────────────
    ingredients: list[Ingredient] = []
    for c in parsed_comp:
        if not c.get("name"):
            continue
        raw_name = c["name"].upper()
        cleaned_name, was_stripped = preprocess_ingredient_name(raw_name)

        rxcui, rxnorm_name, norm_status = get_rxnorm_name(
            cleaned_name, was_stripped=was_stripped
        )

        final_norm_status = norm_status if rxnorm_name else Status.FALLBACK
        query_name = rxnorm_name or cleaned_name
        label, fda_status = get_fda_label(query_name)

        drug_interactions = warnings = boxed_warning = None
        if label:
            drug_interactions, warnings, boxed_warning = extract_interaction_text(label)

        ingredients.append(
            Ingredient(
                name=cleaned_name,
                amount=c.get("amount", ""),
                drap_raw_name=raw_name,
                name_was_stripped=was_stripped,
                rxcui=rxcui,
                rxnorm_name=rxnorm_name,
                normalization_status=final_norm_status,
                fda_status=fda_status,
                drug_interactions=drug_interactions,
                warnings=warnings,
                boxed_warning=boxed_warning,
            )
        )

    return NemlResolution(
        searched_generic=generic_name,
        is_combination=is_combination,
        drap_candidate_count=len(candidates),
        selected_brand_name=chosen.get("text"),
        drap_reg_no=chosen.get("id"),
        selection_reason=selection_reason,
        composition_verified=comp_verified,
        composition_similarity_score=round(comp_score, 4),
        overall_status=verify_status,
        ingredients=ingredients,
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
