"""
core/db/listeners.py — Postgres LISTEN/NOTIFY registry for job-completion push.

Architecture:
  - One dedicated, long-lived asyncpg connection listens on
    'interaction_jobs_channel'.
  - WebSocket handlers register an asyncio.Event keyed by job_id.
  - When a NOTIFY arrives, the background task signals the matching Event.
  - The WS handler wakes up, re-reads the job from the DB, and sends the
    result to the client.

This module intentionally uses a raw asyncpg connection — not the SQLAlchemy
pool — because a listening connection parks in LISTEN mode and must never be
checked back into the pool for normal query use.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Dict

import asyncpg
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[3] / "infra" / ".env")

logger = logging.getLogger(__name__)

CHANNEL = "interaction_jobs_channel"

# ---------------------------------------------------------------------------
# Registry — maps job_id (str) → asyncio.Event
# WebSocket handlers write here; the listener loop reads here.
# ---------------------------------------------------------------------------
_waiters: Dict[str, asyncio.Event] = {}


def register_waiter(job_id: str) -> asyncio.Event:
    """Register interest in a job_id.  Returns the Event to await on."""
    event = asyncio.Event()
    _waiters[job_id] = event
    logger.info("listener | registered waiter for job %s", job_id)
    return event


def unregister_waiter(job_id: str) -> None:
    """Clean up after the WebSocket handler is done (success, timeout, error)."""
    _waiters.pop(job_id, None)
    logger.info("listener | unregistered waiter for job %s", job_id)


# ---------------------------------------------------------------------------
# Background listener task
# ---------------------------------------------------------------------------

_listener_conn: asyncpg.Connection | None = None


def _get_raw_dsn() -> str:
    """Convert the asyncpg-style DATABASE_URL to a plain DSN asyncpg accepts."""
    url = os.environ["DATABASE_URL"]
    # Replace 'postgresql+asyncpg://' with 'postgresql://'
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def _notification_handler(
    connection: asyncpg.Connection,
    pid: int,
    channel: str,
    payload: str,
) -> None:
    """Called by asyncpg for every NOTIFY on the channel."""
    job_id = payload.strip()
    logger.info("listener | NOTIFY received | channel=%s job_id=%s", channel, job_id)

    event = _waiters.get(job_id)
    if event:
        event.set()
        logger.info("listener | signalled waiter for job %s", job_id)
    else:
        logger.debug("listener | no waiter registered for job %s (already polled?)", job_id)


async def start_listener() -> None:
    """Open the dedicated LISTEN connection and start the background loop.

    Intended to be called once at FastAPI startup (via lifespan).
    Runs until cancelled; attempts a single reconnect on connection loss.
    """
    global _listener_conn
    dsn = _get_raw_dsn()

    while True:
        try:
            logger.info("listener | connecting to Postgres for LISTEN...")
            _listener_conn = await asyncpg.connect(dsn)
            await _listener_conn.add_listener(CHANNEL, _notification_handler)
            logger.info("listener | LISTEN %s — ready", CHANNEL)

            # Park here until the connection closes or is cancelled
            while not _listener_conn.is_closed():
                await asyncio.sleep(1)

        except asyncio.CancelledError:
            logger.info("listener | shutting down cleanly")
            if _listener_conn and not _listener_conn.is_closed():
                await _listener_conn.remove_listener(CHANNEL, _notification_handler)
                await _listener_conn.close()
            return

        except Exception as exc:
            logger.warning(
                "listener | connection lost (%s) — reconnecting in 5 s", exc
            )
            await asyncio.sleep(5)
            # Loop back and reconnect
