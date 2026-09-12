"""
Unit tests for core.clients.drap_client.

All HTTP calls are mocked via unittest.mock.patch so no network access is
required.  Tests cover:

  - parse_composition()         — two recognised separator formats
  - preprocess_ingredient_name() — salt/qualifier stripping
  - _spelling_similarity()      — calibrated known values (Amryl/Amaryl, Amryl/Pamaryl)
  - search_drap()               — happy path + network error
  - resolve_brand()             — NOT_FOUND path + SPELLING_RETRY_FOUND path
"""

from unittest.mock import MagicMock, patch

import pytest

from core.clients.drap_client import (
    _spelling_similarity,
    parse_composition,
    preprocess_ingredient_name,
    resolve_brand,
    search_drap,
)
from core.status import Status


# ---------------------------------------------------------------------------
# parse_composition
# ---------------------------------------------------------------------------

class TestParseComposition:
    def test_dotdot_separator(self):
        raw = "Amoxicillin..........500 mg\nClavulanic Acid..125 mg"
        result = parse_composition(raw)
        assert len(result) == 2
        assert result[0] == {"name": "Amoxicillin", "amount": "500 mg"}
        assert result[1] == {"name": "Clavulanic Acid", "amount": "125 mg"}

    def test_space_dose_separator(self):
        raw = "Metformin 500 mg"
        result = parse_composition(raw)
        assert len(result) == 1
        assert result[0]["name"] == "Metformin"
        assert result[0]["amount"] == "500 mg"

    def test_empty_string_returns_empty_list(self):
        assert parse_composition("") == []

    def test_blank_lines_are_skipped(self):
        raw = "Paracetamol 500 mg\n\n\nCaffeine 65 mg"
        result = parse_composition(raw)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# preprocess_ingredient_name
# ---------------------------------------------------------------------------

class TestPreprocessIngredientName:
    def test_strips_eq_to_suffix(self):
        cleaned, was_stripped = preprocess_ingredient_name(
            "AMOXICILLIN TRIHYDRATE EQ TO AMOXICILLIN"
        )
        assert was_stripped is True
        # After EQ TO stripping: "AMOXICILLIN TRIHYDRATE", then TRIHYDRATE stripped
        assert "EQ TO" not in cleaned

    def test_strips_trihydrate(self):
        cleaned, was_stripped = preprocess_ingredient_name("AMOXICILLIN TRIHYDRATE")
        assert was_stripped is True
        assert cleaned.upper() == "AMOXICILLIN"

    def test_strips_hydrochloride(self):
        cleaned, was_stripped = preprocess_ingredient_name("METFORMIN HYDROCHLORIDE")
        assert was_stripped is True
        assert cleaned.upper() == "METFORMIN"

    def test_no_stripping_needed(self):
        cleaned, was_stripped = preprocess_ingredient_name("PARACETAMOL")
        assert was_stripped is False
        assert cleaned.upper() == "PARACETAMOL"

    def test_case_insensitive(self):
        cleaned, was_stripped = preprocess_ingredient_name("Amoxicillin trihydrate")
        assert was_stripped is True


# ---------------------------------------------------------------------------
# _spelling_similarity — calibrated known values
# ---------------------------------------------------------------------------

class TestSpellingSimilarity:
    def test_amryl_vs_amaryl_accepted(self):
        """Empirical value 0.767 — must be above SPELLING_SIM_THRESHOLD (0.65)."""
        score = _spelling_similarity("Amryl", "Amaryl")
        assert score >= 0.65, f"Expected >= 0.65, got {score:.3f}"

    def test_amryl_vs_amaryl_above_high_confidence(self):
        """Empirical value 0.767 — must be above SPELLING_LOW_CONFIDENCE_THRESHOLD (0.75)."""
        score = _spelling_similarity("Amryl", "Amaryl")
        assert score >= 0.75, f"Expected >= 0.75, got {score:.3f}"

    def test_amryl_vs_pamaryl_rejected(self):
        """Empirical value 0.257 — must be below SPELLING_SIM_THRESHOLD (0.65)."""
        score = _spelling_similarity("Amryl", "Pamaryl Plus Tablet")
        assert score < 0.65, f"Expected < 0.65, got {score:.3f}"

    def test_bicoprolol_vs_bisoprolol_accepted(self):
        """Empirical value 0.747 — must be above SPELLING_SIM_THRESHOLD (0.65)."""
        score = _spelling_similarity("Bicoprolol", "Bisoprolol")
        assert score >= 0.65, f"Expected >= 0.65, got {score:.3f}"

    def test_identical_strings_score_one(self):
        score = _spelling_similarity("Panadol", "Panadol")
        assert score == pytest.approx(1.0)

    def test_completely_different_strings_score_low(self):
        score = _spelling_similarity("Zyrtec", "Amoxicillin")
        assert score < 0.3


# ---------------------------------------------------------------------------
# search_drap — mocked HTTP
# ---------------------------------------------------------------------------

class TestSearchDrap:
    def test_returns_results_on_success(self):
        mock_resp = MagicMock()
        mock_resp.content = b'{"results": [{"id": "REG001", "text": "Panadol 500mg Tablet"}]}'
        mock_resp.raise_for_status = MagicMock()

        with patch("core.clients.drap_client.SESSION") as mock_session:
            mock_session.get.return_value = mock_resp
            results, err = search_drap("Panadol")

        assert err is None
        assert len(results) == 1
        assert results[0]["id"] == "REG001"

    def test_returns_empty_list_on_network_error(self):
        import requests as _requests

        with patch("core.clients.drap_client.SESSION") as mock_session:
            mock_session.get.side_effect = _requests.RequestException("timeout")
            results, err = search_drap("Panadol")

        assert results == []
        assert err is not None

    def test_returns_empty_list_on_json_error(self):
        mock_resp = MagicMock()
        mock_resp.content = b"not json"
        mock_resp.raise_for_status = MagicMock()

        with patch("core.clients.drap_client.SESSION") as mock_session:
            mock_session.get.return_value = mock_resp
            results, err = search_drap("Panadol")

        assert results == []
        assert err is not None


# ---------------------------------------------------------------------------
# resolve_brand — mocked HTTP, high-level behaviour
# ---------------------------------------------------------------------------

class TestResolveBrand:
    def test_not_found_when_drap_returns_empty(self):
        mock_resp = MagicMock()
        mock_resp.content = b'{"results": []}'
        mock_resp.raise_for_status = MagicMock()

        with patch("core.clients.drap_client.SESSION") as mock_session:
            # All variant searches also return empty
            mock_session.get.return_value = mock_resp
            result = resolve_brand("Zzzzzzz")

        assert result.status == Status.NOT_FOUND

    def test_api_error_propagated(self):
        import requests as _requests

        with patch("core.clients.drap_client.SESSION") as mock_session:
            mock_session.get.side_effect = _requests.RequestException("timeout")
            result = resolve_brand("Panadol")

        assert result.status == Status.API_ERROR

    def test_ok_status_on_successful_resolution(self):
        # Build mock responses: search -> detail POST
        search_resp = MagicMock()
        search_resp.content = b'{"results": [{"id": "REG001", "text": "Panadol 500mg Tablet"}]}'
        search_resp.raise_for_status = MagicMock()

        detail_resp = MagicMock()
        # Minimal HTML that parse_detail_html can work with (no Composition)
        detail_resp.text = "<html><body></body></html>"
        detail_resp.raise_for_status = MagicMock()

        with patch("core.clients.drap_client.SESSION") as mock_session:
            mock_session.get.return_value = search_resp
            mock_session.post.return_value = detail_resp
            result = resolve_brand("Panadol")

        # Empty composition -> EMPTY_DATA
        assert result.status == Status.EMPTY_DATA
        assert result.matched_name == "Panadol 500mg Tablet"
