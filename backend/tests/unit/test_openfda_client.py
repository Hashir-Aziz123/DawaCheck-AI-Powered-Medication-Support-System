"""
Unit tests for core.clients.openfda_client.

All HTTP calls are mocked so no network access is required.  Tests cover:

  - get_fda_label()            — OK, 404 not-found, empty results, API error
  - extract_interaction_text() — field extraction from a label dict
"""

from unittest.mock import MagicMock, patch

import pytest
import requests as _requests

from core.clients.openfda_client import extract_interaction_text, get_fda_label
from core.status import Status


class TestGetFdaLabel:
    def _make_ok_resp(self, label_dict: dict) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"results": [label_dict]}
        return resp

    def test_ok_returns_label_dict(self):
        label = {
            "drug_interactions": ["Do not use with MAOIs."],
            "warnings": ["Risk of anaphylaxis."],
            "boxed_warning": [],
        }
        resp = self._make_ok_resp(label)

        with patch("core.clients.openfda_client.SESSION") as mock_session:
            mock_session.get.return_value = resp
            result_label, status = get_fda_label("Amoxicillin")

        assert status == Status.OK
        assert result_label == label

    def test_404_returns_not_found(self):
        resp = MagicMock()
        resp.status_code = 404

        with patch("core.clients.openfda_client.SESSION") as mock_session:
            mock_session.get.return_value = resp
            label, status = get_fda_label("UnknownDrug")

        assert status == Status.NOT_FOUND
        assert label is None

    def test_empty_results_returns_not_found(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"results": []}

        with patch("core.clients.openfda_client.SESSION") as mock_session:
            mock_session.get.return_value = resp
            label, status = get_fda_label("Obscure")

        assert status == Status.NOT_FOUND
        assert label is None

    def test_network_exception_returns_api_error(self):
        with patch("core.clients.openfda_client.SESSION") as mock_session:
            mock_session.get.side_effect = _requests.RequestException("connection error")
            label, status = get_fda_label("Paracetamol")

        assert status == Status.API_ERROR
        assert label is None

    def test_non_404_http_error_returns_api_error(self):
        resp = MagicMock()
        resp.status_code = 500
        resp.raise_for_status.side_effect = _requests.HTTPError("500 Server Error")

        with patch("core.clients.openfda_client.SESSION") as mock_session:
            mock_session.get.return_value = resp
            label, status = get_fda_label("Paracetamol")

        assert status == Status.API_ERROR
        assert label is None


class TestExtractInteractionText:
    def test_extracts_all_fields(self):
        label = {
            "drug_interactions": ["Do not combine with warfarin.", "Avoid NSAIDs."],
            "warnings": ["Hepatotoxicity risk."],
            "boxed_warning": ["BLACK BOX: Serious hepatic events."],
        }
        interactions, warnings, boxed = extract_interaction_text(label)
        assert interactions == "Do not combine with warfarin.; Avoid NSAIDs."
        assert warnings == "Hepatotoxicity risk."
        assert boxed == "BLACK BOX: Serious hepatic events."

    def test_absent_fields_return_none(self):
        interactions, warnings, boxed = extract_interaction_text({})
        assert interactions is None
        assert warnings is None
        assert boxed is None

    def test_empty_list_fields_return_none(self):
        label = {"drug_interactions": [], "warnings": [], "boxed_warning": []}
        interactions, warnings, boxed = extract_interaction_text(label)
        assert interactions is None
        assert warnings is None
        assert boxed is None
