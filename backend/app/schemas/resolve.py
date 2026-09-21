from typing import Literal

from pydantic import BaseModel, field_validator


# ---------------------------------------------------------------------------
# Service-layer models
# Returned by resolve_drug() — used internally between service and route.
# ---------------------------------------------------------------------------

class IngredientResponse(BaseModel):
    generic_name: str
    dose: str | None = None
    rxcui: str | None = None
    drug_interactions: str | None = None
    warnings: str | None = None
    boxed_warning: str | None = None


class ResolvedDrugInfo(BaseModel):
    """A single drug entry returned to API consumers.

    Intentionally minimal — no internal identifiers (no drug_id, no drap_reg_no).
    ``display_name`` is constructed by the route layer as
    "{brand_name} {dosage_form}".strip() when dosage_form is present,
    or just brand_name otherwise.
    """
    display_name: str
    dosage_form: str | None = None


class DrugResolutionResult(BaseModel):
    status: Literal["found", "not_found", "ambiguous", "error"]
    source: Literal["database", "live"] | None = None
    matched_as: Literal["brand", "generic"] | None = None
    brand_name: str
    dosage_form: str | None = None
    ingredients: list[IngredientResponse] = []
    # Populated only when status == "ambiguous"; each entry is a candidate drug.
    candidates: list[ResolvedDrugInfo] = []


# ---------------------------------------------------------------------------
# HTTP-layer request / response models
# These are what the API consumer sends and receives.
# ---------------------------------------------------------------------------

class ResolveRequest(BaseModel):
    query: str

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must not be blank or whitespace-only")
        return v.strip()


class ResolveResponse(BaseModel):
    status: Literal["resolved", "ambiguous", "not_found"]
    drug: ResolvedDrugInfo | None = None
    candidates: list[ResolvedDrugInfo] | None = None
