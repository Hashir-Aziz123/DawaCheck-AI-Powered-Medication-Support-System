"""
Integration tests for POST /resolve.

Strategy: mock resolve_drug() at the route layer so tests are fully isolated
from the database and external APIs — the same unit-test pattern already used
in tests/unit/test_drug_resolution_service.py.  We test the HTTP translation
logic (status codes, JSON shape, candidate passthrough) rather than the
resolution logic itself (which is covered by the service unit/integration tests).
"""

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.dependencies import get_sync_db
from app.schemas.resolve import DrugResolutionResult, IngredientResponse, ResolvedDrugInfo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """TestClient with the DB dependency overridden by a no-op mock session."""
    mock_db = MagicMock(spec=Session)

    def override_get_sync_db():
        yield mock_db

    app.dependency_overrides[get_sync_db] = override_get_sync_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helper factories — build DrugResolutionResult fixtures for patching
# ---------------------------------------------------------------------------

def _found_result(brand_name: str, dosage_form: str | None = None) -> DrugResolutionResult:
    return DrugResolutionResult(
        status="found",
        source="database",
        matched_as="brand",
        brand_name=brand_name,
        dosage_form=dosage_form,
        ingredients=[
            IngredientResponse(
                generic_name="Paracetamol",
                dose="500mg",
                rxcui="161",
                drug_interactions="Avoid alcohol",
                warnings="Liver damage risk at high doses",
            )
        ],
    )


def _found_generic_result(brand_name: str) -> DrugResolutionResult:
    return DrugResolutionResult(
        status="found",
        source="database",
        matched_as="generic",
        brand_name=brand_name,
        dosage_form="Tablet",
        ingredients=[
            IngredientResponse(generic_name="Ibuprofen", dose="400mg", rxcui="5640")
        ],
    )


def _ambiguous_result(query: str) -> DrugResolutionResult:
    return DrugResolutionResult(
        status="ambiguous",
        source="database",
        matched_as="generic",
        brand_name=query,
        candidates=[
            ResolvedDrugInfo(display_name="Panadol Tablet", dosage_form="Tablet"),
            ResolvedDrugInfo(display_name="Tylenol", dosage_form=None),
        ],
    )


def _not_found_result(query: str) -> DrugResolutionResult:
    return DrugResolutionResult(
        status="not_found",
        source="live",
        matched_as=None,
        brand_name=query,
    )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestResolveEndpoint:
    """POST /resolve — HTTP translation layer tests."""

    ENDPOINT = "/resolve"

    def test_brand_name_resolves_cleanly(self, client):
        """A brand-name query that resolves to a single match → status=resolved."""
        with patch(
            "app.routes.resolve.resolve_drug",
            return_value=_found_result("Panadol", dosage_form="Tablet 500mg"),
        ):
            resp = client.post(self.ENDPOINT, json={"query": "Panadol"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "resolved"
        assert body["drug"] is not None
        assert body["drug"]["display_name"] == "Panadol Tablet 500mg"
        assert body["drug"]["dosage_form"] == "Tablet 500mg"
        assert body["candidates"] is None

    def test_generic_name_resolves_cleanly(self, client):
        """A generic-name query that resolves to a single drug → status=resolved."""
        with patch(
            "app.routes.resolve.resolve_drug",
            return_value=_found_generic_result("Brufen"),
        ):
            resp = client.post(self.ENDPOINT, json={"query": "Ibuprofen"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "resolved"
        assert body["drug"] is not None
        assert "Brufen" in body["drug"]["display_name"]
        assert body["candidates"] is None

    def test_ambiguous_query_returns_candidates(self, client):
        """A query matching multiple drugs → status=ambiguous with candidate list."""
        with patch(
            "app.routes.resolve.resolve_drug",
            return_value=_ambiguous_result("Paracetamol"),
        ):
            resp = client.post(self.ENDPOINT, json={"query": "Paracetamol"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ambiguous"
        assert body["drug"] is None
        assert isinstance(body["candidates"], list)
        assert len(body["candidates"]) == 2
        display_names = [c["display_name"] for c in body["candidates"]]
        assert "Panadol Tablet" in display_names
        assert "Tylenol" in display_names

    def test_unrecognised_query_returns_not_found(self, client):
        """A query that matches nothing → status=not_found."""
        with patch(
            "app.routes.resolve.resolve_drug",
            return_value=_not_found_result("xyznonexistentdrug123"),
        ):
            resp = client.post(self.ENDPOINT, json={"query": "xyznonexistentdrug123"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "not_found"
        assert body["drug"] is None
        assert body["candidates"] is None

    def test_blank_query_returns_422(self, client):
        """An empty/whitespace query is rejected by Pydantic before the service is called."""
        with patch("app.routes.resolve.resolve_drug") as mock_service:
            resp = client.post(self.ENDPOINT, json={"query": "   "})

        assert resp.status_code == 422
        mock_service.assert_not_called()

    def test_missing_query_field_returns_422(self, client):
        """A request body without a query field is rejected."""
        resp = client.post(self.ENDPOINT, json={})
        assert resp.status_code == 422

    def test_service_exception_returns_500(self, client):
        """An unexpected exception from resolve_drug() → HTTP 500, no traceback leaked."""
        with patch(
            "app.routes.resolve.resolve_drug",
            side_effect=RuntimeError("DB connection lost"),
        ):
            resp = client.post(self.ENDPOINT, json={"query": "Panadol"})

        assert resp.status_code == 500
        body = resp.json()
        # Generic message only — no internal detail
        assert "unexpected error" in body["detail"].lower()

    def test_upstream_api_error_returns_503(self, client):
        """When the service signals status='error' (upstream API down) → HTTP 503."""
        error_result = DrugResolutionResult(
            status="error",
            source="live",
            matched_as=None,
            brand_name="Panadol",
        )
        with patch("app.routes.resolve.resolve_drug", return_value=error_result):
            resp = client.post(self.ENDPOINT, json={"query": "Panadol"})

        assert resp.status_code == 503
        body = resp.json()
        assert "unavailable" in body["detail"].lower()
