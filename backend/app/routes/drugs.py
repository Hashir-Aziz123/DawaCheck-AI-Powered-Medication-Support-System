"""
GET /drugs — return a read-only list of all drugs in the dataset.

This is a lightweight endpoint used by the Browse page of the frontend.
It returns only consumer-safe fields: brand_name, generic_name(s), dosage_form.
No registration numbers or internal identifiers are exposed.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.dependencies import get_sync_db
from core.db.models import Drug

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/drugs", tags=["drugs"])


class DrugListEntry(BaseModel):
    """A single drug entry for the browse/list view.

    Intentionally minimal — no DRAP registration numbers or internal IDs.
    generic_names aggregates all ingredient generic_names for this drug.
    """

    brand_name: str
    generic_names: list[str]
    dosage_form: str | None = None


class DrugListResponse(BaseModel):
    total: int
    drugs: list[DrugListEntry]


@router.get("", response_model=DrugListResponse)
def list_drugs(
    db: Annotated[Session, Depends(get_sync_db)],
) -> DrugListResponse:
    """Return all drugs in the dataset.

    Results are sorted alphabetically by brand_name.
    Includes brand name, aggregated generic ingredient names, and dosage form.
    No pagination — the dataset is small enough (65-90 entries) to return in full.
    """
    rows = db.execute(
        select(Drug).options(selectinload(Drug.ingredients)).order_by(Drug.brand_name)
    ).scalars().all()

    entries = [
        DrugListEntry(
            brand_name=drug.brand_name,
            generic_names=sorted(
                {ing.generic_name for ing in drug.ingredients if ing.generic_name}
            ),
            dosage_form=drug.dosage_form,
        )
        for drug in rows
    ]

    logger.info("GET /drugs | returning %d entries", len(entries))
    return DrugListResponse(total=len(entries), drugs=entries)
