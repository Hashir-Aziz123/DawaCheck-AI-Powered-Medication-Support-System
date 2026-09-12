"""
Shared resolution status codes used across all pipeline stages.

Every stage of the brand-resolution pipeline (DRAP, RxNorm, openFDA)
records an explicit Status rather than silently succeeding or failing,
so batch output and live-API responses can distinguish "not found" from
"API error" from "found but empty" from "normalization fell back to raw name".

Status is a ``str`` subclass so its values serialise transparently to/from
JSON without a custom encoder.
"""

from enum import Enum


class Status(str, Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    API_ERROR = "api_error"
    EMPTY_DATA = "empty_data"
    FALLBACK = "fallback"                                   # normalization fell back to the raw/un-normalized name
    STRIPPED_AND_MATCHED = "stripped_and_matched"           # Problem 1: salt/qualifier stripped before RxNorm hit
    APPROXIMATE_MATCH = "approximate_match"                 # Problem 2: typo corrected via approximateTerm
    SPELLING_RETRY_FOUND = "spelling_retry_found"           # Problem 3: variant succeeded, high-confidence match
    SPELLING_RETRY_LOW_CONFIDENCE = "spelling_retry_low_confidence"  # Problem 3: borderline; review recommended
    COMPOSITION_MISMATCH = "composition_mismatch"                    # NEML: resolved product doesn't contain the searched generic
    COMBINATION_COMPONENT_MISMATCH = "combination_component_mismatch"  # NEML: combination generic — one or both components absent
