"""
Unit tests for core.clients.rxnorm_client.

All HTTP calls are mocked so no network access is required.  Tests cover:

  - get_rxnorm_name() — exact hit (OK), stripped-and-matched, approximate match,
                        not-found fallback, API error propagation
"""

from unittest.mock import MagicMock, call, patch

import pytest
import requests as _requests

from core.clients.rxnorm_client import APPROX_SCORE_THRESHOLD, get_rxnorm_name
from core.status import Status


def _make_rxcui_resp(rxcui: str | None) -> MagicMock:
    """Return a mock response for the ``/rxcui.json`` endpoint."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    if rxcui:
        resp.json.return_value = {"idGroup": {"rxnormId": [rxcui]}}
    else:
        resp.json.return_value = {"idGroup": {}}
    return resp


def _make_property_resp(name: str | None) -> MagicMock:
    """Return a mock response for the ``/rxcui/{id}/property.json`` endpoint."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    if name:
        resp.json.return_value = {
            "propConceptGroup": {"propConcept": [{"propValue": name}]}
        }
    else:
        resp.json.return_value = {"propConceptGroup": {}}
    return resp


def _make_approx_resp(score: float, rxcui: str, name: str) -> MagicMock:
    """Return a mock response for the ``/approximateTerm.json`` endpoint."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "approximateGroup": {
            "candidate": [{"rxcui": rxcui, "name": name, "score": str(score)}]
        }
    }
    return resp


class TestGetRxnormName:
    def test_exact_hit_returns_ok(self):
        rxcui_resp = _make_rxcui_resp("12345")
        prop_resp = _make_property_resp("Amoxicillin")

        with patch("core.clients.rxnorm_client.SESSION") as mock_session:
            mock_session.get.side_effect = [rxcui_resp, prop_resp]
            rxcui, name, status = get_rxnorm_name("AMOXICILLIN")

        assert status == Status.OK
        assert rxcui == "12345"
        assert name == "Amoxicillin"

    def test_stripped_and_matched_status(self):
        """When was_stripped=True and exact lookup succeeds, status must be STRIPPED_AND_MATCHED."""
        rxcui_resp = _make_rxcui_resp("12345")
        prop_resp = _make_property_resp("Amoxicillin")

        with patch("core.clients.rxnorm_client.SESSION") as mock_session:
            mock_session.get.side_effect = [rxcui_resp, prop_resp]
            rxcui, name, status = get_rxnorm_name("AMOXICILLIN", was_stripped=True)

        assert status == Status.STRIPPED_AND_MATCHED
        assert rxcui == "12345"
        assert name == "Amoxicillin"

    def test_approximate_match_when_exact_misses(self):
        """When exact lookup returns no rxcui, fall back to approximateTerm."""
        rxcui_resp = _make_rxcui_resp(None)  # exact miss
        approx_resp = _make_approx_resp(
            score=APPROX_SCORE_THRESHOLD + 1.0,   # above threshold
            rxcui="99999",
            name="Bisoprolol",
        )
        prop_resp = _make_property_resp("Bisoprolol Fumarate")

        with patch("core.clients.rxnorm_client.SESSION") as mock_session:
            mock_session.get.side_effect = [rxcui_resp, approx_resp, prop_resp]
            rxcui, name, status = get_rxnorm_name("BICOPROLOL")

        assert status == Status.APPROXIMATE_MATCH
        assert rxcui == "99999"

    def test_not_found_when_both_paths_miss(self):
        """When exact search and approximateTerm both return nothing, status is NOT_FOUND."""
        rxcui_resp = _make_rxcui_resp(None)
        approx_resp = MagicMock()
        approx_resp.raise_for_status = MagicMock()
        approx_resp.json.return_value = {"approximateGroup": {"candidate": []}}

        with patch("core.clients.rxnorm_client.SESSION") as mock_session:
            mock_session.get.side_effect = [rxcui_resp, approx_resp]
            rxcui, name, status = get_rxnorm_name("UNKNOWNDRUG99")

        assert status == Status.NOT_FOUND
        assert rxcui is None
        assert name is None

    def test_approx_below_threshold_returns_not_found(self):
        """Approximate candidate whose score is below APPROX_SCORE_THRESHOLD must be rejected."""
        rxcui_resp = _make_rxcui_resp(None)
        approx_resp = _make_approx_resp(
            score=APPROX_SCORE_THRESHOLD - 0.5,  # below threshold
            rxcui="99999",
            name="SomeDrug",
        )

        with patch("core.clients.rxnorm_client.SESSION") as mock_session:
            mock_session.get.side_effect = [rxcui_resp, approx_resp]
            rxcui, name, status = get_rxnorm_name("JUNKNAME")

        assert status == Status.NOT_FOUND

    def test_api_error_on_network_failure(self):
        with patch("core.clients.rxnorm_client.SESSION") as mock_session:
            mock_session.get.side_effect = _requests.RequestException("timeout")
            rxcui, name, status = get_rxnorm_name("PARACETAMOL")

        assert status == Status.API_ERROR
        assert rxcui is None
        assert name is None

    def test_empty_data_when_property_endpoint_returns_nothing(self):
        """rxcui found but property endpoint returns no props -> EMPTY_DATA."""
        rxcui_resp = _make_rxcui_resp("12345")
        prop_resp = _make_property_resp(None)  # no name

        with patch("core.clients.rxnorm_client.SESSION") as mock_session:
            mock_session.get.side_effect = [rxcui_resp, prop_resp]
            rxcui, name, status = get_rxnorm_name("PARACETAMOL")

        assert status == Status.EMPTY_DATA
        assert rxcui == "12345"
        assert name is None
