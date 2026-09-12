"""
RxNorm HTTP client.

Normalizes generic drug names using the NLM RxNav REST API.

**Lookup strategy (in order):**

1. Exact/normalized search (``search=2``) — the primary path.
   Returns ``Status.OK`` on success, or ``Status.STRIPPED_AND_MATCHED``
   if the ingredient name had salt qualifiers stripped before this call
   succeeded (Problem 1).

2. Approximate/spelling-tolerant search via ``approximateTerm`` (Problem 2).
   Returns ``Status.APPROXIMATE_MATCH`` on success.

3. ``Status.NOT_FOUND`` — caller sets ``Status.FALLBACK``.

**Caching note:** No function here reads from or writes to any cache.
They always perform fresh HTTP calls.  Cache management is the caller's
responsibility.

Public API::

    get_rxnorm_name(generic_name, was_stripped=False)
        → (rxcui | None, rxnorm_name | None, Status)
"""

import logging
from typing import Optional

import requests

from core.http_session import SESSION
from core.rate_limit import rate_limit
from core.status import Status

logger = logging.getLogger(__name__)

# ---------- Configuration ----------

RXNORM_BASE = "https://rxnav.nlm.nih.gov/REST"
REQUEST_TIMEOUT = 10  # seconds

# ---------- Problem 2 threshold ----------
# Observed score for "BICOPROLOL FUMARATE" -> "Bisoprolol Fumarate" = 11.538.
# Scores in RxNorm approximateTerm range from ~0 (junk) to ~14 (near-exact).
# 8.0 sits well below the clear-match zone (11+) while excluding low-quality
# candidates that could silently corrupt a name.  Adjust if real data warrants.
APPROX_SCORE_THRESHOLD = 8.0


# ---------- Internal helpers ----------

def _rxcui_to_name(rxcui: str) -> tuple[Optional[str], Status]:
    """Resolve an *rxcui* to its RxNorm canonical name via the property endpoint."""
    rate_limit("rxnorm")
    try:
        resp = SESSION.get(
            f"{RXNORM_BASE}/rxcui/{rxcui}/property.json",
            params={"propName": "RxNorm Name"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("[RxNorm] name lookup failed for rxcui '%s': %s", rxcui, e)
        return None, Status.API_ERROR

    props = resp.json().get("propConceptGroup", {}).get("propConcept", [])
    if not props:
        return None, Status.EMPTY_DATA
    return props[0].get("propValue"), Status.OK


def _approximate_rxnorm_lookup(
    generic_name: str,
) -> tuple[Optional[str], Optional[str], Status]:
    """
    Problem 2: attempt spelling-tolerant RxNorm resolution via ``approximateTerm``.

    Returns ``(rxcui, rxnorm_name, status)`` where *status* is
    ``Status.APPROXIMATE_MATCH`` on success, or ``Status.NOT_FOUND`` if no
    candidate scores above ``APPROX_SCORE_THRESHOLD``.

    Every accepted match is logged at ``WARNING`` level because silently
    correcting a possible source-data typo is a decision worth auditing.
    """
    rate_limit("rxnorm")
    try:
        resp = SESSION.get(
            f"{RXNORM_BASE}/approximateTerm.json",
            params={"term": generic_name, "maxEntries": 5},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(
            "[RxNorm] approximateTerm request failed for '%s': %s", generic_name, e
        )
        return None, None, Status.API_ERROR

    candidates = resp.json().get("approximateGroup", {}).get("candidate", [])
    if not candidates:
        logger.info(
            "[RxNorm] approximateTerm returned no candidates for '%s'", generic_name
        )
        return None, None, Status.NOT_FOUND

    # Candidates are returned ranked; pick the first one above the threshold.
    best = candidates[0]
    score = float(best.get("score", 0))
    rxcui = best.get("rxcui")
    candidate_name = best.get("name", "<unnamed>")

    if score < APPROX_SCORE_THRESHOLD:
        logger.info(
            "[RxNorm] approximateTerm best candidate for '%s' scored %.2f "
            "(threshold %s) — below threshold, rejecting",
            generic_name, score, APPROX_SCORE_THRESHOLD,
        )
        return None, None, Status.NOT_FOUND

    # Fetch the canonical RxNorm name for this rxcui.
    rxnorm_name, name_status = _rxcui_to_name(rxcui)
    if name_status != Status.OK or not rxnorm_name:
        # Use the candidate name from the approximateTerm response as fallback.
        rxnorm_name = candidate_name if candidate_name != "<unnamed>" else None

    logger.warning(
        "[RxNorm] APPROXIMATE_MATCH accepted: '%s' -> '%s' (rxcui=%s, score=%.2f). "
        "Source data may contain a typo — review this mapping.",
        generic_name, rxnorm_name, rxcui, score,
    )
    return rxcui, rxnorm_name, Status.APPROXIMATE_MATCH


# ---------- Public API ----------

def get_rxnorm_name(
    generic_name: str,
    was_stripped: bool = False,
) -> tuple[Optional[str], Optional[str], Status]:
    """
    Normalise *generic_name* via RxNorm and return ``(rxcui, standardized_name, status)``.

    Parameters
    ----------
    generic_name:
        The ingredient name to look up (already pre-processed by
        ``drap_client.preprocess_ingredient_name`` if applicable).
    was_stripped:
        ``True`` when the caller applied salt-qualifier stripping before this
        call (Problem 1).  Used to assign ``Status.STRIPPED_AND_MATCHED``
        instead of plain ``Status.OK`` when the exact lookup succeeds.

    **No cache is read or written here.**
    """
    # --- Step 1: exact / normalized RxNorm search ---
    rate_limit("rxnorm")
    try:
        resp = SESSION.get(
            f"{RXNORM_BASE}/rxcui.json",
            params={"name": generic_name, "search": 2},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("[RxNorm] rxcui lookup failed for '%s': %s", generic_name, e)
        return None, None, Status.API_ERROR

    rxcuis = resp.json().get("idGroup", {}).get("rxnormId", [])
    if rxcuis:
        rxcui = rxcuis[0]
        rxnorm_name, name_status = _rxcui_to_name(rxcui)
        if name_status == Status.API_ERROR:
            return rxcui, None, Status.API_ERROR
        if not rxnorm_name:
            return rxcui, None, Status.EMPTY_DATA
        # Decide status: STRIPPED_AND_MATCHED if pre-processing contributed, else OK.
        resolved_status = Status.STRIPPED_AND_MATCHED if was_stripped else Status.OK
        if was_stripped:
            logger.info(
                "[RxNorm] STRIPPED_AND_MATCHED: exact lookup succeeded after stripping "
                "-> '%s' -> '%s' (rxcui=%s)",
                generic_name, rxnorm_name, rxcui,
            )
        return rxcui, rxnorm_name, resolved_status

    logger.info(
        "[RxNorm] exact search returned no RxCUI for '%s' — trying approximateTerm",
        generic_name,
    )

    # --- Step 2: approximate / spelling-tolerant match (Problem 2) ---
    rxcui, rxnorm_name, approx_status = _approximate_rxnorm_lookup(generic_name)
    if approx_status == Status.APPROXIMATE_MATCH:
        return rxcui, rxnorm_name, approx_status

    # --- Step 3: fallback — caller will use the raw/stripped name ---
    logger.info(
        "[RxNorm] no RxCUI found for '%s' (exact or approximate) — marking FALLBACK",
        generic_name,
    )
    return None, None, Status.NOT_FOUND
