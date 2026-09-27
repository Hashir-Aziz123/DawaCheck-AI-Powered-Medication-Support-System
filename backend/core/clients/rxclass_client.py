"""
RxClass HTTP client.

Fetches pharmacological class data for a drug ingredient using NLM's RxClass
REST API (https://rxnav.nlm.nih.gov/RxClassAPIs.html).  No API key required.

**Source selection rationale** (confirmed against live API responses):

  ATC    — Returns a single, clean anatomical/therapeutic class string per rxcui
            (e.g. "Selective serotonin reuptake inhibitors" for fluoxetine/sertraline).
            Excellent signal-to-noise ratio; every returned class is meaningful.
            One structural limitation: many drugs that are clinically well-classified
            (e.g. tramadol, codeine) return empty from ATC.

  MEDRT  — VA/NLM terminology.  Returns many relation types.  We filter to:
            - has_moa  (Mechanism of Action)  — e.g. "Serotonin Uptake Inhibitors"
            - has_pe   (Pharmacological Effect) — e.g. "Increased CNS Serotonin Activity"
            These two relation types complement ATC for drugs it misses, and the
            class names are written in the same language FDA labels use when they
            warn about drug classes.

  EPC    — FDA's Established Pharmacologic Class.  Returns HTTP 400 as a filter
            parameter for byRxcui (confirmed via live probe); omitted.

  FMTSME, VA_CLASS, MESHPA — return noisy or disease/indication data; excluded.

**Caching note:** No function here reads from or writes to any cache.
They always perform fresh HTTP calls.  Cache management (the drug_classes table)
is the caller's responsibility.

Public API::

    get_drug_classes(rxcui)  → list[{"class_name": str, "class_source": str}]
"""

import logging
from typing import Optional

import requests

from core.http_session import SESSION
from core.rate_limit import rate_limit

logger = logging.getLogger(__name__)

# ---------- Configuration ----------

RXCLASS_BASE = "https://rxnav.nlm.nih.gov/REST/rxclass"
REQUEST_TIMEOUT = 10  # seconds

# Sources to query and, for MEDRT, which relation types to keep.
# Each entry is (relaSource, accepted_rela_set | None).
# None means "accept all relations returned by this source".
_SOURCES: list[tuple[str, Optional[set[str]]]] = [
    ("ATC",   None),                         # all ATC relations are useful
    ("MEDRT", {"has_moa", "has_pe"}),        # mechanism-of-action + pharmacological effect only
]

# Short labels stored in class_sources so downstream consumers know provenance.
_SOURCE_LABEL = {
    "ATC":   "ATC",
    "MEDRT": "MEDRT-MOA",   # branded as MOA since we only take has_moa/has_pe
}

# Deduplicate noisy/generic class names that carry no interaction reasoning value.
_EXCLUDED_CLASS_NAMES = {
    # MED-RT pharmacological effects that are too abstract
    "Hepatic Metabolism",
    "Renal Excretion",
}


# ---------- Internal helpers ----------

def _fetch_by_source(rxcui: str, rela_source: str) -> list[dict]:
    """Call byRxcui for one relaSource; return raw rxclassDrugInfo list or []."""
    rate_limit("rxnorm")   # RxClass lives on the same NLM host as RxNorm
    try:
        resp = SESSION.get(
            f"{RXCLASS_BASE}/class/byRxcui.json",
            params={"rxcui": rxcui, "relaSource": rela_source},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("[RxClass] request failed for rxcui=%s source=%s: %s", rxcui, rela_source, e)
        return []

    return resp.json().get("rxclassDrugInfoList", {}).get("rxclassDrugInfo", [])


# ---------- Public API ----------

def get_drug_classes(rxcui: str) -> list[dict]:
    """
    Fetch pharmacological class data for *rxcui* from RxClass.

    Queries ATC and MEDRT (mechanism-of-action relations only), deduplicates
    by class name, and returns a clean list of::

        {"class_name": str, "class_source": str}

    Returns an empty list when no classes are found — this is expected for
    some drugs and is not treated as an error by the caller.

    Parameters
    ----------
    rxcui:
        The RxNorm CUI for the ingredient (e.g. ``"4493"`` for fluoxetine).

    **No cache is read or written here.**
    """
    seen_names: set[str] = set()
    results: list[dict] = []

    for rela_source, accepted_relas in _SOURCES:
        entries = _fetch_by_source(rxcui, rela_source)
        source_label = _SOURCE_LABEL.get(rela_source, rela_source)

        for entry in entries:
            concept = entry.get("rxclassMinConceptItem", {})
            class_name: str = concept.get("className", "").strip()
            rela: str = entry.get("rela", "")

            # Apply relation filter if one is defined for this source
            if accepted_relas is not None and rela not in accepted_relas:
                continue

            # Skip noisy names
            if class_name in _EXCLUDED_CLASS_NAMES:
                continue

            # Deduplicate across sources
            if not class_name or class_name in seen_names:
                continue

            seen_names.add(class_name)
            results.append({"class_name": class_name, "class_source": source_label})

    if results:
        logger.info(
            "[RxClass] rxcui=%s: found %d class(es): %s",
            rxcui,
            len(results),
            [r["class_name"] for r in results],
        )
    else:
        logger.info("[RxClass] rxcui=%s: no classes found.", rxcui)

    return results
