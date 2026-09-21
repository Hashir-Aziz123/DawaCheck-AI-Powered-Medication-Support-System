"""
POST /resolve — synchronous drug resolution endpoint.

The route's sole responsibility is HTTP ↔ service translation:
  - validate & unwrap the request
  - call resolve_drug()
  - map the service result to the appropriate ResolveResponse
  - handle and log unexpected exceptions so raw tracebacks never reach the wire
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.dependencies import get_sync_db
from app.schemas.resolve import (
    ResolveRequest,
    ResolveResponse,
    ResolvedDrugInfo,
)
from app.services.drug_resolution import resolve_drug

logger = logging.getLogger(__name__)

router = APIRouter()


def _build_drug_info(brand_name: str, dosage_form: str | None) -> ResolvedDrugInfo:
    """Build a consumer-safe drug info object.

    Appends dosage_form only when it isn't already present in brand_name,
    preventing duplicates like "Paracetamol Syrup syrup".
    """
    if dosage_form and dosage_form.lower() not in brand_name.lower():
        display_name = f"{brand_name} {dosage_form}".strip()
    else:
        display_name = brand_name
    return ResolvedDrugInfo(display_name=display_name, dosage_form=dosage_form)


@router.post("/resolve", response_model=ResolveResponse)
def resolve(
    body: ResolveRequest,
    db: Annotated[Session, Depends(get_sync_db)],
) -> ResolveResponse:
    """
    Resolve a drug query to its canonical form.

    Returns 200 with one of three statuses:
      - ``resolved``  — single unambiguous match found
      - ``ambiguous`` — multiple candidates; client should prompt user to pick one
      - ``not_found`` — nothing matched across all resolution paths

    A 422 is returned automatically by FastAPI/Pydantic when the request body
    is invalid (e.g. blank query).

    A 503 is returned when the upstream resolution APIs are unreachable.
    """
    query = body.query  # already stripped by the Pydantic validator

    try:
        result = resolve_drug(query, db)
    except Exception:
        logger.exception("Unexpected error while resolving query '%s'", query)
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while resolving the drug. Please try again.",
        )

    if result.status == "found":
        return ResolveResponse(
            status="resolved",
            drug=_build_drug_info(result.brand_name, result.dosage_form),
        )

    if result.status == "ambiguous":
        # Candidates were populated by the service; pass them through directly.
        return ResolveResponse(
            status="ambiguous",
            candidates=result.candidates if result.candidates else None,
        )

    if result.status == "not_found":
        return ResolveResponse(status="not_found")

    if result.status == "error":
        # Upstream API failure — not a bug in our code, signal as 503.
        logger.error(
            "Upstream API error while resolving '%s' — service returned status='error'",
            query,
        )
        raise HTTPException(
            status_code=503,
            detail="Drug resolution service is temporarily unavailable. Please try again later.",
        )

    # Defensive fallback — should never be reached if the service contract holds.
    logger.error("resolve_drug returned unexpected status '%s' for query '%s'", result.status, query)
    raise HTTPException(status_code=500, detail="Unexpected resolution status.")
