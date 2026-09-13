from typing import Literal

from pydantic import BaseModel


class IngredientResponse(BaseModel):
    generic_name: str
    dose: str | None = None
    rxcui: str | None = None
    drug_interactions: str | None = None
    warnings: str | None = None
    boxed_warning: str | None = None


class DrugResolutionResult(BaseModel):
    status: Literal["found", "not_found", "ambiguous", "error"]
    source: Literal["database", "live"] | None = None
    matched_as: Literal["brand", "generic"] | None = None
    brand_name: str
    dosage_form: str | None = None
    ingredients: list[IngredientResponse] = []
