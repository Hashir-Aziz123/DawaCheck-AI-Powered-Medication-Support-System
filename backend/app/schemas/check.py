"""
Pydantic models for the POST /check and GET /check/{job_id} endpoints.

CheckRequest              — what the client sends
CheckAcceptedResponse     — 202 response after POST /check (job enqueued)
InteractionResult         — the final result written by the worker (mirrors
                            run_interaction_check's return shape exactly)
CheckStatusResponse       — response for GET /check/{job_id}
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class CheckRequest(BaseModel):
    drug_a_id: int
    drug_b_id: int


class CheckAcceptedResponse(BaseModel):
    job_id: UUID
    status: Literal["queued"]


class InteractionResult(BaseModel):
    """Mirrors the dict returned by run_interaction_check() exactly.

    Fields verified against graph.py return value:
        final_status, drug_a, drug_b, interaction_claim,
        citation_text, is_grounded, groundedness_reasoning
    """

    final_status: Literal["interaction_found", "none_found", "unverifiable"]
    drug_a: str | None = None
    drug_b: str | None = None
    interaction_claim: str | None = None
    citation_text: str | None = None
    is_grounded: bool | None = None
    groundedness_reasoning: str | None = None


class CheckStatusResponse(BaseModel):
    job_id: UUID
    status: Literal["queued", "processing", "done", "failed"]
    result: InteractionResult | None = None
    error_message: str | None = None
