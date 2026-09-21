"""
WS /ws/jobs/{job_id} — real-time job-completion push via WebSocket.

Flow:
  1. Accept the WebSocket connection.
  2. Immediately check if the job is already done/failed (fast-path).
  3. If still in-flight: register a waiter with the LISTEN/NOTIFY registry,
     then await the Event (with a timeout).
  4. On signal (or timeout): re-read the job row from the DB and send the
     CheckStatusResponse JSON, then close.

The response shape is identical to GET /check/{job_id} so the client can
use the same handler for both polling and push.
"""

import asyncio
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.dependencies import get_sync_db
from app.schemas.check import CheckStatusResponse, InteractionResult
from core.db.listeners import register_waiter, unregister_waiter
from core.db.models import InteractionJob
from core.db.session import async_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ws", tags=["websocket"])

# How long to wait for a notification before giving up (seconds).
WS_TIMEOUT_SECONDS = 180


def _build_response(job: InteractionJob) -> dict:
    """Serialise an InteractionJob ORM row to the CheckStatusResponse shape."""
    result = None
    if job.status == "done" and job.result:
        result = InteractionResult(**job.result)

    return CheckStatusResponse(
        job_id=job.id,
        status=job.status,
        result=result,
        error_message=job.error_message,
    ).model_dump(mode="json")


async def _fetch_job(job_id: uuid.UUID) -> InteractionJob | None:
    """Read the current job row using the async session pool."""
    async with async_session() as session:
        row = await session.execute(
            select(InteractionJob).where(InteractionJob.id == job_id)
        )
        return row.scalar_one_or_none()


@router.websocket("/jobs/{job_id}")
async def ws_job_status(websocket: WebSocket, job_id: uuid.UUID) -> None:
    """Push job result to the client the moment the worker finishes.

    The client connects once and waits; the server sends exactly one message
    (the final CheckStatusResponse JSON) and closes the connection.
    """
    await websocket.accept()
    job_id_str = str(job_id)
    logger.info("WS /ws/jobs/%s | client connected", job_id_str)

    try:
        # ------------------------------------------------------------------
        # Fast-path: job already finished before the client connected
        # ------------------------------------------------------------------
        job = await _fetch_job(job_id)

        if job is None:
            logger.warning("WS /ws/jobs/%s | job not found", job_id_str)
            await websocket.send_json({"error": f"Job {job_id_str} not found."})
            await websocket.close(code=1008)  # Policy violation
            return

        if job.status in ("done", "failed"):
            logger.info(
                "WS /ws/jobs/%s | already %s — sending immediately", job_id_str, job.status
            )
            await websocket.send_json(_build_response(job))
            await websocket.close()
            return

        # ------------------------------------------------------------------
        # Slow-path: job is still queued/processing — wait for NOTIFY
        # ------------------------------------------------------------------
        event = register_waiter(job_id_str)
        logger.info(
            "WS /ws/jobs/%s | status=%s — waiting for NOTIFY (timeout=%ds)",
            job_id_str,
            job.status,
            WS_TIMEOUT_SECONDS,
        )

        try:
            await asyncio.wait_for(event.wait(), timeout=WS_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "WS /ws/jobs/%s | timed out after %ds — sending timeout message",
                job_id_str,
                WS_TIMEOUT_SECONDS,
            )
            await websocket.send_json(
                {
                    "error": "timeout",
                    "message": f"Job did not complete within {WS_TIMEOUT_SECONDS}s.",
                    "job_id": job_id_str,
                }
            )
            await websocket.close()
            return
        finally:
            unregister_waiter(job_id_str)

        # Re-read the row — never trust the notification payload as source of truth
        job = await _fetch_job(job_id)
        if job is None:
            await websocket.send_json({"error": f"Job {job_id_str} vanished after notification."})
            await websocket.close(code=1011)
            return

        logger.info(
            "WS /ws/jobs/%s | NOTIFY delivered | status=%s — sending result",
            job_id_str,
            job.status,
        )
        await websocket.send_json(_build_response(job))
        await websocket.close()

    except WebSocketDisconnect:
        logger.info("WS /ws/jobs/%s | client disconnected early", job_id_str)
        unregister_waiter(job_id_str)
    except Exception as exc:
        logger.exception("WS /ws/jobs/%s | unexpected error: %s", job_id_str, exc)
        unregister_waiter(job_id_str)
        try:
            await websocket.send_json({"error": "internal_error", "detail": str(exc)})
            await websocket.close(code=1011)
        except Exception:
            pass
