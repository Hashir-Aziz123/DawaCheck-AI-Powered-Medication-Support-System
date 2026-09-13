"""
Pipeline result dataclasses shared across the core library.

Both ``Ingredient`` and ``DrugResolution`` live here (rather than in
``core/resolution.py``) so that ``core/clients/drap_client.py`` can
return them without creating a circular import with ``core/resolution.py``.

Import path::

    from core.types import Ingredient, DrugResolution
"""

from dataclasses import dataclass, field
from typing import Optional

from core.status import Status


@dataclass
class Ingredient:
    """A single active ingredient as resolved through the DRAP→RxNorm→openFDA pipeline."""

    name: str            # cleaned name used for RxNorm lookup (may have been salt-stripped)
    amount: str
    drap_raw_name: str = ""          # original DRAP composition string — preserved for audit trail
    name_was_stripped: bool = False  # True when salt/qualifier stripping was applied in pre-processing
    rxcui: Optional[str] = None
    rxnorm_name: Optional[str] = None
    normalization_status: Status = Status.NOT_FOUND
    fda_status: Status = Status.NOT_FOUND
    drug_interactions: Optional[str] = None
    warnings: Optional[str] = None
    boxed_warning: Optional[str] = None
    raw_fda_response: Optional[dict] = None


@dataclass
class DrugResolution:
    """Result of fully resolving a single brand name through the pipeline."""

    brand_query: str
    status: Status
    matched_name: Optional[str] = None
    drap_reg_no: Optional[str] = None
    candidate_count: int = 0
    spelling_variant_used: Optional[str] = None   # Problem 3: variant that DRAP matched on
    ingredients: list[Ingredient] = field(default_factory=list)
    error: Optional[str] = None
    
    # Generic-resolution specific fields (originally from batch script)
    selection_reason: Optional[str] = None
    composition_verified: Optional[bool] = None
    composition_similarity_score: Optional[float] = None
