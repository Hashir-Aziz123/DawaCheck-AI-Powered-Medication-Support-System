"""
Drug-resolution orchestration — the single shared entry point.

``build_drug_record(brand_name)`` is the function that both the batch
script and the live API should call.  It chains the four external services
in the canonical order:

    DRAP (brand -> composition)
        -> RxNorm (generic-name normalization)
        -> openFDA (interaction / warning label text)
        -> RxClass (pharmacological class data)

**Caching note:** This function (and the client functions it delegates to)
performs **no caching of any kind**.  Callers are responsible for their own
cache strategy:

* ``scripts/create_dataset.py`` -- wraps this call with a ``JsonCache``
  (disk-backed) keyed by brand name.
* A future live-API caller -- would check Postgres first (``drugs`` /
  ``drug_ingredients`` / ``fda_labels`` / ``drug_classes`` tables), falling
  back to ``build_drug_record()`` only on a miss.

Re-exported for callers' convenience::

    from core.resolution import build_drug_record, DrugResolution, Ingredient
    from core.status import Status
"""

import logging
from typing import Optional  # noqa: F401 -- kept for re-export convenience

from core.clients.drap_client import resolve_brand
from core.clients.openfda_client import extract_interaction_text, get_fda_label
from core.clients.rxclass_client import get_drug_classes
from core.clients.rxnorm_client import get_rxnorm_name
from core.status import Status
from core.types import DrugResolution, Ingredient  # noqa: F401 -- re-exported

logger = logging.getLogger(__name__)


def build_drug_record(brand_name: str) -> DrugResolution:
    """
    Fully resolve *brand_name* through the DRAP -> RxNorm -> openFDA -> RxClass pipeline.

    Parameters
    ----------
    brand_name:
        The brand name to resolve (e.g. ``"Augmentin"``).

    Returns
    -------
    DrugResolution
        A fully-populated ``DrugResolution`` instance.  If any early stage
        fails (e.g. ``Status.NOT_FOUND`` from DRAP), the record is returned
        immediately without attempting later stages.

    This function performs **no caching**.  Wrap it in your own cache layer
    as appropriate for the calling context.
    """
    record = resolve_brand(brand_name)
    if record.status not in (
        Status.OK,
        Status.SPELLING_RETRY_FOUND,
        Status.SPELLING_RETRY_LOW_CONFIDENCE,
    ):
        return record

    for ing in record.ingredients:
        # Forward was_stripped so get_rxnorm_name can assign the right Status.
        rxcui, rxnorm_name, norm_status = get_rxnorm_name(
            ing.name, was_stripped=ing.name_was_stripped
        )
        ing.rxcui = rxcui
        ing.rxnorm_name = rxnorm_name
        # If we got back a result but there is no rxnorm_name, still FALLBACK.
        ing.normalization_status = norm_status if rxnorm_name else Status.FALLBACK

        query_name = rxnorm_name or ing.name
        label, fda_status = get_fda_label(query_name)
        ing.fda_status = fda_status
        if label:
            ing.raw_fda_response = label
            (
                ing.drug_interactions,
                ing.warnings,
                ing.boxed_warning,
            ) = extract_interaction_text(label)

        # RxClass: fetch pharmacological class data when we have an rxcui.
        # Empty result is expected for some drugs — not an error.
        if rxcui:
            ing.drug_classes = get_drug_classes(rxcui)

    return record
