"""
Generic-name-first resolution pipeline.

Orchestrates resolving a generic name by searching DRAP, selecting a representative
brand, and processing ingredients through RxNorm and openFDA.
"""

import logging
import re
from typing import Optional

from core.clients.drap_client import (
    get_drug_detail,
    preprocess_ingredient_name,
    search_drap,
)
from core.clients.openfda_client import extract_interaction_text, get_fda_label
from core.clients.rxnorm_client import get_rxnorm_name
from core.status import Status
from core.types import DrugResolution, Ingredient

logger = logging.getLogger(__name__)

# ---------- Levenshtein (rapidfuzz preferred; pure-Python fallback) ----------
try:
    from rapidfuzz.distance import Levenshtein as _RFLev

    def _lev_dist(a: str, b: str) -> int:
        return _RFLev.distance(a, b)

except ImportError:  # pragma: no cover
    def _lev_dist(a: str, b: str) -> int:  # type: ignore[misc]
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


def _composition_similarity(a: str, b: str) -> float:
    a = a.lower().strip()
    b = b.lower().strip()
    dist = _lev_dist(a, b)
    return 1.0 - dist / max(len(a), len(b), 1)


# ---------- Thresholds & Config ----------

COMPOSITION_SIM_THRESHOLD: float = 0.80

_PREFERRED_STATUSES: frozenset[str] = frozenset({"active", "provisionally active"})
_PREFERRED_FORM_KW: tuple[str, ...] = ("tablet", "capsule", "syrup", "oral", "solution")
_DEPRIORITISED_FORM_KW: tuple[str, ...] = (
    "injection", "infusion", "eye drop", "ophthalmic", "ointment",
    "cream", "inhaler", "suppository", "drop",
)

MULTI_BRAND_OVERRIDES: dict[str, list[str]] = {}

GENERIC_ALIASES: dict[str, list[str]] = {
    "sulfamethoxazole": ["cotrimoxazole", "co-trimoxazole", "trimethoprim"],
    "glyceryl trinitrate": ["nitroglycerin", "nitroglycerine", "gtnt"],
    "isosorbide dinitrate": ["isordil"],
    "acetylsalicylic acid": ["aspirin"],
    "colecalciferol": ["cholecalciferol", "vitamin d3"],
    "hydroxocobalamin": ["cyanocobalamin", "vitamin b12"],
}


# ---------- Regex helpers ----------

_PAREN_RE = re.compile(r"\s*\([^)]*\)")

def _strip_parens(name: str) -> str:
    return _PAREN_RE.sub("", name).strip()


# ---------- Pipeline logic ----------

def generate_search_name(raw_name: str) -> str:
    """
    Strips noise words, doses, and dosage forms from a drug name to create a clean search key.
    E.g. "Panadol Tablet 500mg." -> "panadol"
    """
    if not raw_name:
        return ""
        
    name = raw_name.lower().strip()
    
    # 1. Remove anything in parentheses
    name = _strip_parens(name)
    
    # 2. Remove dosage forms
    noise_words = list(_PREFERRED_FORM_KW) + list(_DEPRIORITISED_FORM_KW)
    noise_words.extend(["suspension", "injection", "tab", "cap", "syr", "susp", "sachet"])
    # Sort by length descending to replace longest matches first
    noise_words = sorted(noise_words, key=len, reverse=True)
    
    for word in noise_words:
        # Match whole words only (optionally plural)
        name = re.sub(rf'\b{re.escape(word)}s?\b', '', name)
        
    # 3. Remove common dose patterns (e.g. 500mg, 10ml, 500 mg, 0.5%)
    name = re.sub(r'\b\d+(?:\.\d+)?\s*(?:mg|ml|mcg|g|iu|u|%)\b', '', name)
    
    # 4. Remove standalone numbers (often left behind)
    name = re.sub(r'\b\d+(?:\.\d+)?\b', '', name)
    
    # 5. Clean up extra spaces and punctuation (like trailing dots from "Tablet.")
    name = re.sub(r'[^\w\s]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    
    return name



def detect_combination(name: str) -> tuple[bool, list[str]]:
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
    if not candidates:
        raise ValueError("select_representative_brand called with empty candidates list")

    if len(candidates) == 1:
        return candidates[0], "only match"

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

    _comps_lower = [c.lower() for c in (components or [])] if is_combination else []

    def _score(c: dict) -> tuple:
        text = c.get("text", "").lower()
        status = c.get("status", "").lower()

        if _comps_lower:
            combo_score = int(all(comp in text for comp in _comps_lower))
        else:
            combo_score = 0

        if status in _PREFERRED_STATUSES:
            status_score = 2
        elif not status:
            status_score = 1
        else:
            status_score = 0

        if any(kw in text for kw in _PREFERRED_FORM_KW):
            form_score = 2
        elif any(kw in text for kw in _DEPRIORITISED_FORM_KW):
            form_score = 0
        else:
            form_score = 1

        sim = _composition_similarity(generic_name, c.get("text", ""))
        return (combo_score, status_score, form_score, sim)

    best = max(candidates, key=_score)
    s = _score(best)

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
) -> tuple[bool, float, Status]:
    def _clean(raw: str) -> str:
        cleaned, _ = preprocess_ingredient_name(raw.upper())
        return cleaned.lower()

    if not parsed_ings:
        return False, 0.0, Status.COMPOSITION_MISMATCH

    cleaned_ings = [_clean(ing["name"]) for ing in parsed_ings if ing.get("name")]

    if not is_combination:
        search_clean = _clean(components[0])
        scores = [_composition_similarity(search_clean, ing) for ing in cleaned_ings]
        best_score = max(scores) if scores else 0.0

        if best_score >= COMPOSITION_SIM_THRESHOLD:
            logger.info(
                "[Verify] OK: '%s' → best_score=%.3f (threshold=%.2f)",
                searched_generic, best_score, COMPOSITION_SIM_THRESHOLD,
            )
            return True, best_score, Status.OK
        else:
            logger.warning(
                "[Verify] MISMATCH: searched='%s' (cleaned='%s'), best_score=%.3f "
                "(threshold=%.2f). Ingredients: %s",
                searched_generic, search_clean, best_score, COMPOSITION_SIM_THRESHOLD,
                [ing["name"] for ing in parsed_ings],
            )
            return False, best_score, Status.COMPOSITION_MISMATCH

    else:
        matched_idxs: set[int] = set()
        component_results: list[tuple[str, bool, float]] = []

        for component in components:
            comp_clean = _clean(component)
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
            return True, overall_best, Status.OK
        else:
            unmatched = [c for c, ok, _ in component_results if not ok]
            logger.warning(
                "[Verify] COMBINATION MISMATCH: '%s' — unmatched: %s. scores=%s. "
                "Ingredients: %s",
                searched_generic, unmatched,
                [(c, f"{s:.3f}") for c, _, s in component_results],
                [ing["name"] for ing in parsed_ings],
            )
            return False, overall_best, Status.COMBINATION_COMPONENT_MISMATCH


def build_generic_record(generic_name: str) -> DrugResolution:
    is_combination, components = detect_combination(generic_name)
    lead_query = components[0]

    logger.info(
        "[Generic] Resolving '%s' | is_combination=%s | lead_query='%s'",
        generic_name, is_combination, lead_query,
    )

    candidates, err = search_drap(lead_query, search_type="generic name")
    if err:
        logger.error("[Generic] DRAP search API error for '%s': %s", generic_name, err)
        return DrugResolution(
            brand_query=generic_name,
            status=Status.API_ERROR,
            error=err,
        )

    if not candidates:
        fallback_queries: list[str] = []
        if is_combination and len(components) > 1:
            fallback_queries.append(components[1])
        for alias in GENERIC_ALIASES.get(lead_query.lower(), []):
            fallback_queries.append(alias)

        for fb_query in fallback_queries:
            logger.info(
                "[Generic] No results for '%s' — trying fallback query '%s'",
                generic_name, fb_query,
            )
            candidates, err = search_drap(fb_query, search_type="generic name")
            if err:
                logger.error(
                    "[Generic] DRAP search API error on fallback '%s': %s", fb_query, err
                )
                continue
            if candidates:
                logger.info(
                    "[Generic] Fallback '%s' → %d DRAP candidate(s): %s",
                    fb_query, len(candidates),
                    [c.get("text", "?") for c in candidates[:5]],
                )
                lead_query = fb_query
                break

    if not candidates:
        logger.info("[Generic] No DRAP results for generic '%s' (all fallbacks exhausted)", generic_name)
        return DrugResolution(
            brand_query=generic_name,
            status=Status.NOT_FOUND,
            error="No DRAP results found for this generic name",
        )

    logger.info(
        "[Generic] '%s' → %d DRAP candidate(s): %s",
        generic_name, len(candidates), [c.get("text", "?") for c in candidates[:5]],
    )

    chosen, selection_reason = select_representative_brand(
        candidates, lead_query,
        is_combination=is_combination,
        components=components,
    )

    detail, err = get_drug_detail(chosen["id"])
    if err:
        return DrugResolution(
            brand_query=generic_name,
            status=Status.API_ERROR,
            matched_name=chosen.get("text"),
            drap_reg_no=chosen.get("id"),
            candidate_count=len(candidates),
            selection_reason=selection_reason,
            error=err,
        )

    parsed_comp = detail.get("Composition", [])
    if not parsed_comp:
        return DrugResolution(
            brand_query=generic_name,
            status=Status.EMPTY_DATA,
            matched_name=chosen.get("text"),
            drap_reg_no=chosen.get("id"),
            candidate_count=len(candidates),
            selection_reason=selection_reason,
            error="DRAP record found but composition was empty",
        )

    comp_verified, comp_score, verify_status = verify_composition(
        generic_name, is_combination, components, parsed_comp,
    )

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
            (
                drug_interactions,
                warnings,
                boxed_warning,
            ) = extract_interaction_text(label)

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
                raw_fda_response=label
            )
        )

    return DrugResolution(
        brand_query=generic_name,
        status=verify_status,
        matched_name=chosen.get("text"),
        drap_reg_no=chosen.get("id"),
        candidate_count=len(candidates),
        selection_reason=selection_reason,
        composition_verified=comp_verified,
        composition_similarity_score=round(comp_score, 4) if comp_score is not None else None,
        ingredients=ingredients,
    )
