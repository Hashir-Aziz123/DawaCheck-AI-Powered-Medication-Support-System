"""
Unit tests for the LangGraph interaction-checking pipeline.

Strategy:
  - Node tests: patch _build_interaction_chain / _build_groundedness_chain so the
    LLM is never called. Each test owns its mock result completely.
  - End-to-end graph routing tests: patch both chain builders at once and run
    the full compiled graph, asserting on routing and final_answer shape.
  - No real API calls, no real database access in any test.
"""

import pytest
from unittest.mock import MagicMock, patch

from worker.langgraph_pipeline.nodes import (
    InteractionCheckResult,
    GroundednessCheckResult,
    _collect_fda_text,
    fetch_context,
    check_interaction,
    verify_groundedness,
    format_grounded_answer,
    format_unverifiable_answer,
)
from worker.langgraph_pipeline.graph import run_interaction_check


# ---------------------------------------------------------------------------
# Test fixtures — reusable drug dicts with realistic FDA text
# ---------------------------------------------------------------------------

WARFARIN_DICT = {
    "brand_name": "Coumadin",
    "dosage_form": "Tablet",
    "ingredients": [
        {
            "generic_name": "Warfarin",
            "dose": "5mg",
            "rxcui": "11289",
            "drug_interactions": (
                "Aspirin and other antiplatelet agents may increase the anticoagulant "
                "effect of warfarin, increasing the risk of bleeding. Concurrent use "
                "should be avoided unless the clinical benefit outweighs the risk."
            ),
            "warnings": "Bleeding risk. Monitor INR regularly.",
            "boxed_warning": "Warfarin can cause major or fatal bleeding.",
        }
    ],
}

ASPIRIN_DICT = {
    "brand_name": "Aspirin",
    "dosage_form": "Tablet",
    "ingredients": [
        {
            "generic_name": "Aspirin",
            "dose": "81mg",
            "rxcui": "1191",
            "drug_interactions": (
                "Anticoagulants: Concurrent use with warfarin or other anticoagulants "
                "increases the risk of bleeding. Use caution."
            ),
            "warnings": "GI bleeding risk.",
            "boxed_warning": None,
        }
    ],
}

PANADOL_DICT = {
    "brand_name": "Panadol",
    "dosage_form": "Tablet",
    "ingredients": [
        {
            "generic_name": "Paracetamol",
            "dose": "500mg",
            "rxcui": "161",
            "drug_interactions": "Avoid alcohol. No significant interactions at standard doses.",
            "warnings": "Liver damage at high doses.",
            "boxed_warning": None,
        }
    ],
}

OMEPRAZOLE_DICT = {
    "brand_name": "Prilosec",
    "dosage_form": "Capsule",
    "ingredients": [
        {
            "generic_name": "Omeprazole",
            "dose": "20mg",
            "rxcui": "7646",
            "drug_interactions": "May reduce absorption of drugs requiring acidic pH.",
            "warnings": "Hypomagnesemia with prolonged use.",
            "boxed_warning": None,
        }
    ],
}


def _base_state(drug_a=None, drug_b=None, **overrides) -> dict:
    """Build a minimal valid InteractionState for testing."""
    state = {
        "drug_a": drug_a or WARFARIN_DICT,
        "drug_b": drug_b or ASPIRIN_DICT,
        "drug_a_fda_text": None,
        "drug_b_fda_text": None,
        "interaction_claim": None,
        "citation_text": None,
        "is_grounded": None,
        "groundedness_reasoning": None,
        "final_status": "",
        "final_answer": None,
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# Helper: _collect_fda_text
# ---------------------------------------------------------------------------

class TestCollectFdaText:
    def test_collects_all_fields(self):
        text = _collect_fda_text(WARFARIN_DICT)
        assert "Warfarin" in text
        assert "Drug Interactions" in text
        assert "Warnings" in text
        assert "Boxed Warning" in text

    def test_empty_ingredients(self):
        text = _collect_fda_text({"brand_name": "X", "ingredients": []})
        assert text == "No FDA label data available for this drug."

    def test_skips_none_fields(self):
        drug = {
            "brand_name": "X",
            "ingredients": [
                {"generic_name": "X", "drug_interactions": None, "warnings": None, "boxed_warning": None}
            ],
        }
        text = _collect_fda_text(drug)
        assert text == "No FDA label data available for this drug."

    def test_skips_whitespace_fields(self):
        drug = {
            "brand_name": "X",
            "ingredients": [
                {"generic_name": "X", "drug_interactions": "  ", "warnings": None, "boxed_warning": None}
            ],
        }
        text = _collect_fda_text(drug)
        assert text == "No FDA label data available for this drug."


# ---------------------------------------------------------------------------
# Node: fetch_context
# ---------------------------------------------------------------------------

class TestFetchContext:
    def test_computes_fda_texts(self):
        state = _base_state()
        updates = fetch_context(state)
        assert "drug_a_fda_text" in updates
        assert "drug_b_fda_text" in updates
        assert "Warfarin" in updates["drug_a_fda_text"]
        assert "Aspirin" in updates["drug_b_fda_text"]

    def test_handles_no_fda_data(self):
        empty_drug = {"brand_name": "Ghost", "ingredients": []}
        state = _base_state(drug_a=empty_drug)
        updates = fetch_context(state)
        assert updates["drug_a_fda_text"] == "No FDA label data available for this drug."

    def test_returns_only_fda_text_keys(self):
        state = _base_state()
        updates = fetch_context(state)
        assert set(updates.keys()) == {"drug_a_fda_text", "drug_b_fda_text"}


# ---------------------------------------------------------------------------
# Node: check_interaction
# ---------------------------------------------------------------------------

class TestCheckInteraction:
    def _state_with_fda(self, drug_a=None, drug_b=None):
        state = _base_state(drug_a=drug_a, drug_b=drug_b)
        fda = fetch_context(state)
        state.update(fda)
        return state

    @patch("worker.langgraph_pipeline.nodes._build_interaction_chain")
    def test_interaction_found(self, mock_build):
        mock_chain = MagicMock()
        mock_build.return_value = mock_chain
        mock_chain.invoke.return_value = InteractionCheckResult(
            interaction_found=True,
            claim="Aspirin increases bleeding risk when combined with Warfarin.",
            citation="Aspirin and other antiplatelet agents may increase the anticoagulant effect of warfarin.",
        )

        state = self._state_with_fda()
        updates = check_interaction(state)

        assert updates["interaction_claim"] == "Aspirin increases bleeding risk when combined with Warfarin."
        assert updates["citation_text"] == "Aspirin and other antiplatelet agents may increase the anticoagulant effect of warfarin."

    @patch("worker.langgraph_pipeline.nodes._build_interaction_chain")
    def test_no_interaction_found(self, mock_build):
        mock_chain = MagicMock()
        mock_build.return_value = mock_chain
        mock_chain.invoke.return_value = InteractionCheckResult(
            interaction_found=False,
            claim=None,
            citation=None,
        )

        state = self._state_with_fda(drug_a=PANADOL_DICT, drug_b=OMEPRAZOLE_DICT)
        updates = check_interaction(state)

        assert updates["interaction_claim"] is None
        assert updates["citation_text"] is None

    @patch("worker.langgraph_pipeline.nodes._build_interaction_chain")
    def test_passes_drug_names_to_chain(self, mock_build):
        mock_chain = MagicMock()
        mock_build.return_value = mock_chain
        mock_chain.invoke.return_value = InteractionCheckResult(
            interaction_found=False, claim=None, citation=None
        )

        state = self._state_with_fda()
        check_interaction(state)

        call_args = mock_chain.invoke.call_args[0][0]
        assert call_args["drug_a_name"] == "Coumadin"
        assert call_args["drug_b_name"] == "Aspirin"


# ---------------------------------------------------------------------------
# Node: verify_groundedness
# ---------------------------------------------------------------------------

class TestVerifyGroundedness:
    @patch("worker.langgraph_pipeline.nodes._build_groundedness_chain")
    def test_grounded_true(self, mock_build):
        mock_chain = MagicMock()
        mock_build.return_value = mock_chain
        mock_chain.invoke.return_value = GroundednessCheckResult(
            is_grounded=True,
            reasoning="The citation is present verbatim in the source text.",
        )

        state = _base_state(
            drug_a_fda_text="Aspirin increases bleeding with warfarin.",
            drug_b_fda_text="Concurrent use with warfarin increases risk.",
            interaction_claim="Aspirin increases bleeding risk with Warfarin.",
            citation_text="Aspirin increases bleeding with warfarin.",
        )
        updates = verify_groundedness(state)

        assert updates["is_grounded"] is True
        assert "verbatim" in updates["groundedness_reasoning"]

    @patch("worker.langgraph_pipeline.nodes._build_groundedness_chain")
    def test_grounded_false(self, mock_build):
        mock_chain = MagicMock()
        mock_build.return_value = mock_chain
        mock_chain.invoke.return_value = GroundednessCheckResult(
            is_grounded=False,
            reasoning="The citation could not be found in the source text.",
        )

        state = _base_state(
            drug_a_fda_text="Some unrelated text.",
            drug_b_fda_text="More unrelated text.",
            interaction_claim="Made-up claim.",
            citation_text="Sentence not actually in the text.",
        )
        updates = verify_groundedness(state)

        assert updates["is_grounded"] is False

    def test_short_circuits_when_no_claim(self):
        """When check_interaction found nothing, skip the LLM call entirely."""
        state = _base_state(interaction_claim=None)
        with patch("worker.langgraph_pipeline.nodes._build_groundedness_chain") as mock_build:
            updates = verify_groundedness(state)
            mock_build.assert_not_called()

        assert updates["is_grounded"] is False
        assert "No interaction claim" in updates["groundedness_reasoning"]


# ---------------------------------------------------------------------------
# Node: format_grounded_answer
# ---------------------------------------------------------------------------

class TestFormatGroundedAnswer:
    def test_sets_interaction_found_status(self):
        state = _base_state(
            drug_a_fda_text="...",
            drug_b_fda_text="...",
            interaction_claim="Aspirin increases bleeding with Warfarin.",
            citation_text="Aspirin increases bleeding with warfarin.",
            is_grounded=True,
            groundedness_reasoning="Citation found verbatim.",
        )
        updates = format_grounded_answer(state)

        assert updates["final_status"] == "interaction_found"
        assert updates["final_answer"]["final_status"] == "interaction_found"
        assert updates["final_answer"]["drug_a"] == "Coumadin"
        assert updates["final_answer"]["drug_b"] == "Aspirin"
        assert updates["final_answer"]["is_grounded"] is True
        assert updates["final_answer"]["interaction_claim"] is not None
        assert updates["final_answer"]["citation_text"] is not None


# ---------------------------------------------------------------------------
# Node: format_unverifiable_answer
# ---------------------------------------------------------------------------

class TestFormatUnverifiableAnswer:
    def test_none_found_when_no_claim(self):
        """If check_interaction found nothing → final_status=none_found."""
        state = _base_state(
            interaction_claim=None,
            citation_text=None,
            is_grounded=False,
            groundedness_reasoning="No interaction claim was found.",
        )
        updates = format_unverifiable_answer(state)

        assert updates["final_status"] == "none_found"
        assert updates["final_answer"]["final_status"] == "none_found"

    def test_unverifiable_when_claim_failed_grounding(self):
        """If a claim was made but grounding failed → final_status=unverifiable."""
        state = _base_state(
            interaction_claim="Some made-up claim.",
            citation_text="Sentence not in source.",
            is_grounded=False,
            groundedness_reasoning="Citation not found in source text.",
        )
        updates = format_unverifiable_answer(state)

        assert updates["final_status"] == "unverifiable"
        assert updates["final_answer"]["final_status"] == "unverifiable"
        assert updates["final_answer"]["interaction_claim"] == "Some made-up claim."


# ---------------------------------------------------------------------------
# End-to-end graph routing tests (LLM mocked, graph runs fully)
# ---------------------------------------------------------------------------

class TestGraphRouting:
    """Run the full compiled graph with mocked LLM chains.

    These tests verify that the graph routes correctly based on the groundedness
    verdict — they test the wiring, not just individual nodes.
    """

    @patch("worker.langgraph_pipeline.nodes._build_groundedness_chain")
    @patch("worker.langgraph_pipeline.nodes._build_interaction_chain")
    def test_grounded_path_produces_interaction_found(self, mock_interaction, mock_ground):
        """interaction_found=True + is_grounded=True → final_status='interaction_found'."""
        mock_int_chain = MagicMock()
        mock_interaction.return_value = mock_int_chain
        mock_int_chain.invoke.return_value = InteractionCheckResult(
            interaction_found=True,
            claim="Aspirin increases bleeding risk with Warfarin.",
            citation="Aspirin and other antiplatelet agents may increase the anticoagulant effect of warfarin.",
        )

        mock_grd_chain = MagicMock()
        mock_ground.return_value = mock_grd_chain
        mock_grd_chain.invoke.return_value = GroundednessCheckResult(
            is_grounded=True,
            reasoning="Citation found verbatim in source text.",
        )

        result = run_interaction_check(WARFARIN_DICT, ASPIRIN_DICT)

        assert result["final_status"] == "interaction_found"
        assert result["interaction_claim"] is not None
        assert result["citation_text"] is not None
        assert result["is_grounded"] is True
        assert result["drug_a"] == "Coumadin"
        assert result["drug_b"] == "Aspirin"

    @patch("worker.langgraph_pipeline.nodes._build_groundedness_chain")
    @patch("worker.langgraph_pipeline.nodes._build_interaction_chain")
    def test_unverifiable_path_when_grounding_fails(self, mock_interaction, mock_ground):
        """interaction_found=True + is_grounded=False → final_status='unverifiable'."""
        mock_int_chain = MagicMock()
        mock_interaction.return_value = mock_int_chain
        mock_int_chain.invoke.return_value = InteractionCheckResult(
            interaction_found=True,
            claim="Warfarin interacts with aspirin.",
            citation="A sentence that doesn't actually appear in the text.",
        )

        mock_grd_chain = MagicMock()
        mock_ground.return_value = mock_grd_chain
        mock_grd_chain.invoke.return_value = GroundednessCheckResult(
            is_grounded=False,
            reasoning="The citation could not be found in the source text.",
        )

        result = run_interaction_check(WARFARIN_DICT, ASPIRIN_DICT)

        assert result["final_status"] == "unverifiable"
        assert result["is_grounded"] is False

    @patch("worker.langgraph_pipeline.nodes._build_interaction_chain")
    def test_none_found_path_skips_groundedness_llm(self, mock_interaction):
        """interaction_found=False → final_status='none_found', groundedness LLM not called."""
        mock_int_chain = MagicMock()
        mock_interaction.return_value = mock_int_chain
        mock_int_chain.invoke.return_value = InteractionCheckResult(
            interaction_found=False,
            claim=None,
            citation=None,
        )

        with patch("worker.langgraph_pipeline.nodes._build_groundedness_chain") as mock_ground:
            result = run_interaction_check(PANADOL_DICT, OMEPRAZOLE_DICT)
            mock_ground.assert_not_called()

        assert result["final_status"] == "none_found"
        assert result["interaction_claim"] is None
        assert result["citation_text"] is None
