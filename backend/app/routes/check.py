"""
POST /check  — enqueue a drug-interaction check job.
GET  /check/{job_id} — poll the status (and result) of a job.

Route responsibilities:
  - Validate inputs & drug existence (DB)
  - Insert the interaction_jobs row
  - Publish to RabbitMQ via queue_publisher
  - Handle publish failures gracefully (mark job failed → 503)
  - Return job status on GET
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dependencies import get_sync_db
from app.schemas.check import (
    CheckAcceptedResponse,
    CheckRequest,
    CheckStatusResponse,
    InteractionResult,
)
from app.services.queue_publisher import publish_interaction_check
from core.db.models import Drug, InteractionJob

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/check", tags=["check"])


# ---------------------------------------------------------------------------
# POST /check — create and enqueue a job
# ---------------------------------------------------------------------------


@router.post("", status_code=202, response_model=CheckAcceptedResponse)
def create_check(
    body: CheckRequest,
    db: Annotated[Session, Depends(get_sync_db)],
) -> CheckAcceptedResponse:
    """Submit a drug-interaction check job.

    Returns 202 immediately; the actual LLM pipeline runs asynchronously in
    the worker process. Poll GET /check/{job_id} for the result.

    Errors:
        404 — either drug_a_id or drug_b_id does not exist in the drugs table.
        503 — RabbitMQ is unreachable; the job is marked failed and not retried.
    """
    drug_a_id = body.drug_a_id
    drug_b_id = body.drug_b_id

    # ------------------------------------------------------------------
    # 1. Validate both drug IDs exist
    # ------------------------------------------------------------------
    drug_a = db.execute(select(Drug).where(Drug.id == drug_a_id)).scalar_one_or_none()
    if drug_a is None:
        raise HTTPException(status_code=404, detail=f"Drug with id={drug_a_id} not found.")

    drug_b = db.execute(select(Drug).where(Drug.id == drug_b_id)).scalar_one_or_none()
    if drug_b is None:
        raise HTTPException(status_code=404, detail=f"Drug with id={drug_b_id} not found.")

    # ------------------------------------------------------------------
    # 2. Insert the interaction_jobs row (status = "queued")
    # ------------------------------------------------------------------
    job = InteractionJob(
        id=uuid.uuid4(),
        drug_a_id=drug_a_id,
        drug_b_id=drug_b_id,
        status="queued",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    logger.info(
        "POST /check | job %s created | drug_a=%s (%s) drug_b=%s (%s)",
        job.id,
        drug_a_id,
        drug_a.brand_name,
        drug_b_id,
        drug_b.brand_name,
    )

    # ------------------------------------------------------------------
    # 3. Publish to RabbitMQ; on failure, mark the job failed and 503
    # ------------------------------------------------------------------
    try:
        publish_interaction_check(job.id, drug_a_id, drug_b_id)
    except Exception as exc:
        logger.exception(
            "POST /check | job %s — publish to RabbitMQ failed: %s", job.id, exc
        )
        job.status = "failed"
        job.error_message = f"Failed to enqueue job: {exc}"
        db.commit()
        raise HTTPException(
            status_code=503,
            detail="Could not enqueue the interaction check job. RabbitMQ may be unavailable.",
        )

    logger.info("POST /check | job %s published successfully", job.id)
    return CheckAcceptedResponse(job_id=job.id, status="queued")


# ---------------------------------------------------------------------------
# GET /check/{job_id} — poll job status
# ---------------------------------------------------------------------------


@router.get("/{job_id}", response_model=CheckStatusResponse)
def get_check_status(
    job_id: uuid.UUID,
    db: Annotated[Session, Depends(get_sync_db)],
) -> CheckStatusResponse:
    """Return the current status (and result, if complete) for a check job.

    This is a plain, single-read endpoint — no side effects, no queue
    interaction. Poll as often as needed until status is 'done' or 'failed'.

    Errors:
        404 — no job with this ID exists.
    """
    job = db.execute(
        select(InteractionJob).where(InteractionJob.id == job_id)
    ).scalar_one_or_none()

    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")

    logger.info("GET /check/%s | status=%s", job_id, job.status)

    result: InteractionResult | None = None
    if job.status == "done" and job.result:
        result = InteractionResult(**job.result)

    return CheckStatusResponse(
        job_id=job.id,
        status=job.status,
        result=result,
        error_message=job.error_message,
    )
