"""
openFDA drug label HTTP client.

Fetches drug-label data (drug interactions, warnings, boxed warnings) from
the FDA's public openFDA API.

**Caching note:** No function here reads from or writes to any cache.
They always perform fresh HTTP calls.  Cache management is the caller's
responsibility.

Public API::

    get_fda_label(query_name)        → (label_dict | None, Status)
    extract_interaction_text(label)  → (interactions, warnings, boxed_warning)
"""

import logging
from typing import Optional

import requests

from core.http_session import SESSION
from core.rate_limit import rate_limit
from core.status import Status

logger = logging.getLogger(__name__)

# ---------- Configuration ----------

OPENFDA_URL = "https://api.fda.gov/drug/label.json"
REQUEST_TIMEOUT = 10  # seconds


# ---------- Public API ----------

def get_fda_label(query_name: str) -> tuple[Optional[dict], Status]:
    """
    Fetch a drug label from openFDA for *query_name* (searched as ``openfda.generic_name``).

    Returns ``(label_dict, status)`` where *label_dict* is the first result
    from the openFDA ``results`` array, or ``None`` on any non-OK outcome.

    Status values:

    * ``Status.OK``        — label found and returned.
    * ``Status.NOT_FOUND`` — openFDA returned HTTP 404 or an empty results list.
    * ``Status.API_ERROR`` — network / HTTP error.

    **No cache is read or written here.**
    """
    rate_limit("openfda")
    try:
        resp = SESSION.get(
            OPENFDA_URL,
            params={"search": f'openfda.generic_name:"{query_name}"', "limit": 1},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        logger.error("[openFDA] request failed for '%s': %s", query_name, e)
        return None, Status.API_ERROR

    if resp.status_code == 404:  # openFDA's documented "no matches" response
        return None, Status.NOT_FOUND

    try:
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("[openFDA] bad response for '%s': %s", query_name, e)
        return None, Status.API_ERROR

    results = resp.json().get("results", [])
    if not results:
        return None, Status.NOT_FOUND

    return results[0], Status.OK


def extract_interaction_text(
    label: dict,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Extract the three interaction/warning text fields from an openFDA label dict.

    Returns ``(drug_interactions, warnings, boxed_warning)``; any field that is
    absent or empty in the label is returned as ``None``.

    This is a pure data-extraction helper — no HTTP calls, no cache.
    """
    drug_interactions = "; ".join(label.get("drug_interactions", [])) or None
    warnings = "; ".join(label.get("warnings", [])) or None
    boxed_warning = "; ".join(label.get("boxed_warning", [])) or None
    return drug_interactions, warnings, boxed_warning
