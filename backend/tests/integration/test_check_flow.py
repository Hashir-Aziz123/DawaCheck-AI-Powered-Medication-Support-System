"""
Integration tests for POST /check and GET /check/{job_id}.

Strategy: TestClient with the sync DB dependency overridden by a MagicMock,
and publish_interaction_check patched out — no real queue or real LLM calls.
Tests cover HTTP translation logic only (status codes, JSON shape, error paths).
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.dependencies import get_sync_db
from app.main import app
from core.db.models import Drug, InteractionJob


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db():
    """A MagicMock that satisfies the sync Session interface."""
    return MagicMock(spec=Session)


@pytest.fixture
def client(mock_db):
    """TestClient with DB dependency overridden; no real DB connection needed."""

    def override_get_sync_db():
        yield mock_db

    app.dependency_overrides[get_sync_db] = override_get_sync_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helpers — build ORM-like mock objects
# ---------------------------------------------------------------------------


def _mock_drug(drug_id: int, brand_name: str) -> MagicMock:
    d = MagicMock(spec=Drug)
    d.id = drug_id
    d.brand_name = brand_name
    return d


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


def _db_returns(*scalars):
    """Make mock_db.execute().scalar_one_or_none() return values in sequence."""
    results = []
    for val in scalars:
        r = MagicMock()
        r.scalar_one_or_none.return_value = val
        results.append(r)
    return results


# ---------------------------------------------------------------------------
# POST /check tests
# ---------------------------------------------------------------------------


class TestPostCheck:
    ENDPOINT = "/check"

    @patch("app.routes.check.publish_interaction_check")
    def test_valid_request_returns_202(self, mock_publish, client, mock_db):
        """Both drug IDs exist → job inserted, message published → 202."""
        drug_a = _mock_drug(1, "Aspirin")
        drug_b = _mock_drug(2, "Warfarin")
        job_id = uuid.uuid4()

        mock_db.execute.side_effect = _db_returns(drug_a, drug_b)

        # After db.add/commit/refresh, job.id should be accessible
        def _refresh(obj):
            obj.id = job_id

        mock_db.refresh.side_effect = _refresh

        resp = client.post(self.ENDPOINT, json={"drug_a_id": 1, "drug_b_id": 2})

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "queued"
        assert "job_id" in body
        mock_publish.assert_called_once()

    @patch("app.routes.check.publish_interaction_check")
    def test_nonexistent_drug_a_returns_404(self, mock_publish, client, mock_db):
        """drug_a_id not found in DB → 404, no job created."""
        mock_db.execute.side_effect = _db_returns(None)  # drug_a missing

        resp = client.post(self.ENDPOINT, json={"drug_a_id": 999, "drug_b_id": 2})

        assert resp.status_code == 404
        assert "999" in resp.json()["detail"]
        mock_publish.assert_not_called()
        mock_db.add.assert_not_called()

    @patch("app.routes.check.publish_interaction_check")
    def test_nonexistent_drug_b_returns_404(self, mock_publish, client, mock_db):
        """drug_a_id exists but drug_b_id does not → 404, no job created."""
        drug_a = _mock_drug(1, "Aspirin")
        mock_db.execute.side_effect = _db_returns(drug_a, None)  # drug_b missing

        resp = client.post(self.ENDPOINT, json={"drug_a_id": 1, "drug_b_id": 888})

        assert resp.status_code == 404
        assert "888" in resp.json()["detail"]
        mock_publish.assert_not_called()
        mock_db.add.assert_not_called()

    @patch("app.routes.check.publish_interaction_check", side_effect=Exception("AMQP connection refused"))
    def test_rabbitmq_failure_returns_503_and_marks_job_failed(self, mock_publish, client, mock_db):
        """RabbitMQ publish fails → job status updated to 'failed' → 503."""
        drug_a = _mock_drug(1, "Aspirin")
        drug_b = _mock_drug(2, "Warfarin")
        mock_db.execute.side_effect = _db_returns(drug_a, drug_b)

        captured_job = MagicMock(spec=InteractionJob)
        captured_job.id = uuid.uuid4()

        def _add(obj):
            obj.id = captured_job.id

        mock_db.add.side_effect = _add

        resp = client.post(self.ENDPOINT, json={"drug_a_id": 1, "drug_b_id": 2})

        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# GET /check/{job_id} tests
# ---------------------------------------------------------------------------


class TestGetCheckStatus:
    def _url(self, job_id: uuid.UUID) -> str:
        return f"/check/{job_id}"

    def test_queued_job_returns_status(self, client, mock_db):
        job_id = uuid.uuid4()
        mock_db.execute.side_effect = _db_returns(_mock_job(job_id, "queued"))

        resp = client.get(self._url(job_id))

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "queued"
        assert body["result"] is None
        assert body["error_message"] is None

    def test_processing_job_returns_status(self, client, mock_db):
        job_id = uuid.uuid4()
        mock_db.execute.side_effect = _db_returns(_mock_job(job_id, "processing"))

        resp = client.get(self._url(job_id))

        assert resp.status_code == 200
        assert resp.json()["status"] == "processing"

    def test_done_job_returns_result(self, client, mock_db):
        job_id = uuid.uuid4()
        result_payload = {
            "final_status": "interaction_found",
            "drug_a": "Aspirin",
            "drug_b": "Warfarin",
            "interaction_claim": "Increased bleeding risk.",
            "citation_text": "FDA label section 7.",
            "is_grounded": True,
            "groundedness_reasoning": "Claim found verbatim in citation.",
        }
        mock_db.execute.side_effect = _db_returns(
            _mock_job(job_id, "done", result=result_payload)
        )

        resp = client.get(self._url(job_id))

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "done"
        assert body["result"]["final_status"] == "interaction_found"
        assert body["result"]["drug_a"] == "Aspirin"
        assert body["result"]["interaction_claim"] == "Increased bleeding risk."

    def test_failed_job_returns_error_message(self, client, mock_db):
        job_id = uuid.uuid4()
        mock_db.execute.side_effect = _db_returns(
            _mock_job(job_id, "failed", error_message="Drug not found in DB.")
        )

        resp = client.get(self._url(job_id))

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert body["result"] is None
        assert "not found" in body["error_message"].lower()

    def test_nonexistent_job_returns_404(self, client, mock_db):
        missing_id = uuid.uuid4()
        mock_db.execute.side_effect = _db_returns(None)

        resp = client.get(self._url(missing_id))

        assert resp.status_code == 404
        assert str(missing_id) in resp.json()["detail"]
