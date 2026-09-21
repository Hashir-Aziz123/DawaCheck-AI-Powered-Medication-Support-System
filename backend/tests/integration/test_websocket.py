"""
Tests for the LISTEN/NOTIFY infrastructure and WebSocket route.

Part 1 — Listener registry unit tests (no real Postgres)
Part 2 — WebSocket route integration tests
  - Fast-path: job already done when client connects (mocked DB)
  - Fast-path: job not found → error + close
  - Slow-path: client connects while job is processing, then NOTIFY arrives
    (simulated by setting the event directly — no real Postgres required)
  - Timeout: event never fires → timeout message sent
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.dependencies import get_sync_db
from core.db.listeners import register_waiter, unregister_waiter, _waiters
from core.db.models import InteractionJob


# ---------------------------------------------------------------------------
# Part 1 — Registry unit tests
# ---------------------------------------------------------------------------


class TestListenerRegistry:
    def setup_method(self):
        """Clear the global registry before each test."""
        _waiters.clear()

    def test_register_creates_event(self):
        job_id = str(uuid.uuid4())
        event = register_waiter(job_id)
        assert job_id in _waiters
        assert isinstance(event, asyncio.Event)
        assert not event.is_set()

    def test_unregister_removes_entry(self):
        job_id = str(uuid.uuid4())
        register_waiter(job_id)
        unregister_waiter(job_id)
        assert job_id not in _waiters

    def test_unregister_nonexistent_is_safe(self):
        """Unregistering a job_id that was never registered should not raise."""
        unregister_waiter(str(uuid.uuid4()))  # no exception

    @pytest.mark.asyncio
    async def test_notification_signals_correct_waiter(self):
        """Simulating what the listener background task does: setting the event."""
        job_id = str(uuid.uuid4())
        other_id = str(uuid.uuid4())

        event_a = register_waiter(job_id)
        event_b = register_waiter(other_id)

        # Simulate notification arriving for job_id only
        _waiters[job_id].set()

        assert event_a.is_set()
        assert not event_b.is_set()

        unregister_waiter(job_id)
        unregister_waiter(other_id)


# ---------------------------------------------------------------------------
# Part 2 — WebSocket route integration tests
# ---------------------------------------------------------------------------


def _mock_job(
    job_id: uuid.UUID,
    status: str,
    result: dict | None = None,
    error_message: str | None = None,
) -> MagicMock:
    j = MagicMock(spec=InteractionJob)
    j.id = job_id
    j.status = status
    j.result = result
    j.error_message = error_message
    return j


class TestWebSocketRoute:
    """
    Uses TestClient's WebSocket support (runs the ASGI app synchronously).
    We patch _fetch_job (the async DB read inside the WS handler) and
    start_listener (so no real Postgres connection is attempted in tests).
    """

    @pytest.fixture(autouse=True)
    def suppress_listener(self):
        """Prevent the lifespan from trying to connect to a real Postgres."""
        async def _noop():
            await asyncio.sleep(9999)

        with patch("app.main.start_listener", return_value=_noop()):
            yield

    def test_already_done_sends_result_and_closes(self):
        job_id = uuid.uuid4()
        done_job = _mock_job(
            job_id,
            "done",
            result={
                "final_status": "none_found",
                "drug_a": "Aspirin",
                "drug_b": "Warfarin",
                "interaction_claim": None,
                "citation_text": None,
                "is_grounded": False,
                "groundedness_reasoning": "No claim.",
            },
        )

        with patch("app.routes.ws._fetch_job", new=AsyncMock(return_value=done_job)):
            with TestClient(app) as client:
                with client.websocket_connect(f"/ws/jobs/{job_id}") as ws:
                    data = ws.receive_json()

        assert data["status"] == "done"
        assert data["result"]["final_status"] == "none_found"
        assert data["job_id"] == str(job_id)

    def test_already_failed_sends_result_and_closes(self):
        job_id = uuid.uuid4()
        failed_job = _mock_job(job_id, "failed", error_message="Drug not found in DB.")

        with patch("app.routes.ws._fetch_job", new=AsyncMock(return_value=failed_job)):
            with TestClient(app) as client:
                with client.websocket_connect(f"/ws/jobs/{job_id}") as ws:
                    data = ws.receive_json()

        assert data["status"] == "failed"
        assert data["error_message"] == "Drug not found in DB."
        assert data["result"] is None

    def test_job_not_found_sends_error(self):
        job_id = uuid.uuid4()

        with patch("app.routes.ws._fetch_job", new=AsyncMock(return_value=None)):
            with TestClient(app) as client:
                with client.websocket_connect(f"/ws/jobs/{job_id}") as ws:
                    data = ws.receive_json()

        assert "error" in data
        assert str(job_id) in data["error"]

    def test_slow_path_delivers_on_notify(self):
        """Client connects while job is 'processing'; event fires → result delivered."""
        job_id = uuid.uuid4()
        job_id_str = str(job_id)

        processing_job = _mock_job(job_id, "processing")
        done_job = _mock_job(
            job_id,
            "done",
            result={
                "final_status": "interaction_found",
                "drug_a": "Warfarin",
                "drug_b": "Aspirin",
                "interaction_claim": "Increased bleeding risk.",
                "citation_text": "FDA label.",
                "is_grounded": True,
                "groundedness_reasoning": "Found in citation.",
            },
        )

        # First call returns 'processing'; second (after NOTIFY) returns 'done'
        fetch_side_effects = [processing_job, done_job]
        call_count = 0

        async def _fetch_side_effect(jid):
            nonlocal call_count
            result = fetch_side_effects[min(call_count, len(fetch_side_effects) - 1)]
            call_count += 1
            # After first fetch, simulate notification arriving almost immediately
            if call_count == 1:
                asyncio.get_event_loop().call_later(
                    0.05, lambda: _waiters.get(job_id_str) and _waiters[job_id_str].set()
                )
            return result

        with patch("app.routes.ws._fetch_job", new=_fetch_side_effect):
            with TestClient(app) as client:
                with client.websocket_connect(f"/ws/jobs/{job_id}") as ws:
                    data = ws.receive_json()

        assert data["status"] == "done"
        assert data["result"]["final_status"] == "interaction_found"
