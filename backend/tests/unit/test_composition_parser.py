"""
Unit tests for composition parsing and ingredient preprocessing.

These are pure logic tests — no HTTP calls, no mocking required.  They
exercise ``parse_composition()`` and ``preprocess_ingredient_name()`` from
``core.clients.drap_client``.
"""

import pytest

from core.clients.drap_client import parse_composition, preprocess_ingredient_name


# ===========================================================================
# parse_composition — existing passing tests (must not regress)
# ===========================================================================

class TestParseCompositionDotSeparator:
    """Composition strings using the ``..`` (two-or-more dot) separator."""

    def test_single_ingredient_dotdot(self):
        raw = "Paracetamol..........500 mg"
        result = parse_composition(raw)
        assert result == [{"name": "Paracetamol", "amount": "500 mg"}]

    def test_two_ingredients_dotdot(self):
        raw = "Amoxicillin..........500 mg\nClavulanic Acid..125 mg"
        result = parse_composition(raw)
        assert len(result) == 2
        assert result[0] == {"name": "Amoxicillin", "amount": "500 mg"}
        assert result[1] == {"name": "Clavulanic Acid", "amount": "125 mg"}

    def test_amount_with_mcg(self):
        raw = "Levothyroxine Sodium..50 mcg"
        result = parse_composition(raw)
        assert result[0]["amount"] == "50 mcg"

    def test_amount_with_percent(self):
        raw = "Hydrocortisone..1%"
        result = parse_composition(raw)
        assert result[0]["amount"] == "1%"

    def test_amount_with_iu(self):
        raw = "Vitamin D3..400 IU"
        result = parse_composition(raw)
        assert result[0]["amount"] == "400 IU"


class TestParseCompositionSpaceSeparator:
    """Composition strings using the ``name <dose>`` space-separated format."""

    def test_mg_dose(self):
        result = parse_composition("Metformin 500 mg")
        assert result == [{"name": "Metformin", "amount": "500 mg"}]

    def test_g_dose(self):
        result = parse_composition("Amoxicillin 1 g")
        assert result == [{"name": "Amoxicillin", "amount": "1 g"}]

    def test_decimal_dose(self):
        result = parse_composition("Amlodipine 2.5 mg")
        assert result == [{"name": "Amlodipine", "amount": "2.5 mg"}]

    def test_no_dose_falls_through(self):
        """A line with no recognisable dose is kept as name with empty amount."""
        result = parse_composition("SomeDrug")
        assert result == [{"name": "SomeDrug", "amount": ""}]


class TestParseCompositionEdgeCases:
    def test_empty_string(self):
        assert parse_composition("") == []

    def test_whitespace_only(self):
        assert parse_composition("   \n  \n  ") == []

    def test_blank_lines_skipped(self):
        raw = "Paracetamol 500 mg\n\n\nCaffeine 65 mg"
        result = parse_composition(raw)
        assert len(result) == 2

    def test_multiline_mixed_formats(self):
        raw = "Amoxicillin..........500 mg\nClavulanic Acid 125 mg"
        result = parse_composition(raw)
        assert len(result) == 2
        assert result[0]["name"] == "Amoxicillin"
        assert result[1]["name"] == "Clavulanic Acid"


# ===========================================================================
# F1 — Trailing punctuation in composition lines (Bug 1)
# ===========================================================================

class TestParseCompositionTrailingPunctuation:
    """Trailing comma/semicolon must be stripped so dose regex can match.

    Root cause (confirmed): 'DIAZEPAM 2MG,' fails the dose regex because the
    anchor ``$`` cannot match with a trailing comma.  Stripping that comma
    before matching fixes the split.
    """

    def test_trailing_comma_splits_correctly(self):
        """'DIAZEPAM 2MG,' → name='DIAZEPAM', amount='2MG'."""
        result = parse_composition("DIAZEPAM 2MG,")
        assert len(result) == 1
        assert result[0]["name"] == "DIAZEPAM"
        assert result[0]["amount"] == "2MG"

    def test_trailing_comma_multiline(self):
        """Each line may have its own trailing comma (DRAP list separator)."""
        raw = "FUROSEMIDE 40MG,\nSPIRONOLACTONE 25MG,"
        result = parse_composition(raw)
        assert result[0]["name"] == "FUROSEMIDE"
        assert result[0]["amount"] == "40MG"
        assert result[1]["name"] == "SPIRONOLACTONE"
        assert result[1]["amount"] == "25MG"

    def test_trailing_semicolon_stripped(self):
        result = parse_composition("METFORMIN 500 mg;")
        assert result[0]["name"] == "METFORMIN"
        assert result[0]["amount"] == "500 mg"

    def test_decimal_dose_trailing_comma(self):
        """'DIGOXIN 0.25MG,' — decimal dose with trailing comma."""
        result = parse_composition("DIGOXIN 0.25MG,")
        assert "," not in result[0]["name"]
        assert result[0]["name"] == "DIGOXIN"

    def test_multiword_name_trailing_comma(self):
        result = parse_composition("LITHIUM CARBONATE 400MG,")
        assert result[0]["name"] == "LITHIUM CARBONATE"
        assert "," not in result[0]["name"]

    def test_salt_and_trailing_comma(self):
        """'AMPICILLIN TRIHYDRATE 125MG,' — salt suffix remains (preprocess strips it)."""
        result = parse_composition("AMPICILLIN TRIHYDRATE 125MG,")
        # After comma strip: "AMPICILLIN TRIHYDRATE 125MG" — comma gone from name
        assert "," not in result[0]["name"]
        assert result[0]["name"] == "AMPICILLIN TRIHYDRATE"

    def test_clean_line_unaffected(self):
        """Lines without trailing punctuation parse identically."""
        result = parse_composition("Paracetamol 500 mg")
        assert result == [{"name": "Paracetamol", "amount": "500 mg"}]


# ===========================================================================
# F3 — Encoding-garbage prefix stripping (Pattern 6)
# ===========================================================================

class TestParseCompositionEncodingGarbage:
    """Leading non-ASCII/mojibake bytes must not pollute the name field."""

    def test_leading_mojibake_stripped(self):
        """Â\\xa0 prefix (Windows-1252 BOM mojibake) must be removed."""
        raw = "Â\xa0  Olanzapine"
        result = parse_composition(raw)
        assert result[0]["name"] == "Olanzapine"

    def test_clean_name_unchanged(self):
        result = parse_composition("Paracetamol 500 mg")
        assert result[0]["name"] == "Paracetamol"


# ===========================================================================
# preprocess_ingredient_name — existing tests (must not regress)
# ===========================================================================

class TestPreprocessIngredientNameExisting:
    """Original test coverage — must all continue to pass after fixes."""

    def test_eq_to_stripped(self):
        cleaned, stripped = preprocess_ingredient_name(
            "AMOXICILLIN TRIHYDRATE EQ TO AMOXICILLIN"
        )
        assert stripped is True
        assert "EQ TO" not in cleaned

    def test_trihydrate_stripped(self):
        cleaned, stripped = preprocess_ingredient_name("AMOXICILLIN TRIHYDRATE")
        assert stripped is True
        assert cleaned.upper() == "AMOXICILLIN"

    def test_hydrochloride_stripped(self):
        cleaned, stripped = preprocess_ingredient_name("METFORMIN HYDROCHLORIDE")
        assert stripped is True
        assert cleaned.upper() == "METFORMIN"

    def test_monohydrate_stripped(self):
        cleaned, stripped = preprocess_ingredient_name("DOXYCYCLINE MONOHYDRATE")
        assert stripped is True
        assert cleaned.upper() == "DOXYCYCLINE"

    def test_fumarate_stripped(self):
        cleaned, stripped = preprocess_ingredient_name("BISOPROLOL FUMARATE")
        assert stripped is True
        assert cleaned.upper() == "BISOPROLOL"

    def test_sodium_stripped(self):
        cleaned, stripped = preprocess_ingredient_name("DICLOFENAC SODIUM")
        assert stripped is True
        assert cleaned.upper() == "DICLOFENAC"

    def test_bare_name_unchanged(self):
        cleaned, stripped = preprocess_ingredient_name("PARACETAMOL")
        assert stripped is False
        assert cleaned.upper() == "PARACETAMOL"

    def test_case_insensitive_stripping(self):
        cleaned, stripped = preprocess_ingredient_name("amoxicillin trihydrate")
        assert stripped is True

    def test_multi_word_name_preserved(self):
        """A multi-word name with no qualifying suffix must be left intact."""
        cleaned, stripped = preprocess_ingredient_name("CLAVULANIC ACID")
        assert stripped is False
        assert cleaned.upper() == "CLAVULANIC ACID"


# ===========================================================================
# F2a — HCL abbreviated hydrochloride (Bug 2)
# ===========================================================================

class TestPreprocessHCLAbbreviation:
    """'HCL' is distinct from 'HYDROCHLORIDE' in the old list — must now strip."""

    def test_clindamycin_hcl(self):
        cleaned, stripped = preprocess_ingredient_name("CLINDAMYCIN HCL")
        assert stripped is True
        assert cleaned.upper() == "CLINDAMYCIN"

    def test_amiodarone_hcl(self):
        cleaned, stripped = preprocess_ingredient_name("AMIODARONE HCL")
        assert stripped is True
        assert cleaned.upper() == "AMIODARONE"

    def test_verapamil_hcl(self):
        cleaned, stripped = preprocess_ingredient_name("VERAPAMIL HCL")
        assert stripped is True
        assert cleaned.upper() == "VERAPAMIL"

    def test_pyridoxine_hcl(self):
        cleaned, stripped = preprocess_ingredient_name("PYRIDOXINE HCL")
        assert stripped is True
        assert cleaned.upper() == "PYRIDOXINE"

    def test_metformin_hcl(self):
        cleaned, stripped = preprocess_ingredient_name("METFORMIN HCL")
        assert stripped is True
        assert cleaned.upper() == "METFORMIN"


# ===========================================================================
# F2b — New salt suffixes (Pattern 5 + Bug 2)
# ===========================================================================

class TestPreprocessNewSaltSuffixes:

    def test_stearate_stripped(self):
        cleaned, stripped = preprocess_ingredient_name("ERYTHROMYCIN STEARATE")
        assert stripped is True
        assert cleaned.upper() == "ERYTHROMYCIN"

    def test_benzoate_stripped(self):
        cleaned, stripped = preprocess_ingredient_name("METOCLOPRAMIDE BENZOATE")
        assert stripped is True
        assert cleaned.upper() == "METOCLOPRAMIDE"

    def test_sulphate_british_spelling(self):
        cleaned, stripped = preprocess_ingredient_name("SALBUTAMOL SULPHATE")
        assert stripped is True
        assert cleaned.upper() == "SALBUTAMOL"

    def test_bisulfate_stripped(self):
        cleaned, stripped = preprocess_ingredient_name("CLOPIDOGREL BISULFATE")
        assert stripped is True
        assert cleaned.upper() == "CLOPIDOGREL"

    def test_hyclate_stripped(self):
        cleaned, stripped = preprocess_ingredient_name("DOXYCYCLINE HYCLATE")
        assert stripped is True
        assert cleaned.upper() == "DOXYCYCLINE"

    def test_nitrate_stripped(self):
        cleaned, stripped = preprocess_ingredient_name("GLYCERYL TRINITRATE")
        # TRINITRATE not in list, but NITRATE is — should strip trailing NITRATE
        # Result: "GLYCERYL TRI" is not meaningful — TRINITRATE as a whole should
        # be tested instead; confirm at minimum NITRATE suffix strips
        cleaned2, stripped2 = preprocess_ingredient_name("ISOSORBIDE NITRATE")
        assert stripped2 is True
        assert cleaned2.upper() == "ISOSORBIDE"


# ===========================================================================
# F2b — Pharmacopoeia quality-standard suffixes
# ===========================================================================

class TestPreprocessPharmacopoeiaSuffixes:

    def test_usp_stripped(self):
        """'LOSARTAN POTASSIUM USP' → 'LOSARTAN' (iterative: USP then POTASSIUM)."""
        cleaned, stripped = preprocess_ingredient_name("LOSARTAN POTASSIUM USP")
        assert stripped is True
        assert cleaned.upper() == "LOSARTAN"

    def test_bp_stripped(self):
        """'WARFARIN SOD BP' → 'WARFARIN' (iterative: BP then SOD)."""
        cleaned, stripped = preprocess_ingredient_name("WARFARIN SOD BP")
        assert stripped is True
        assert cleaned.upper() == "WARFARIN"

    def test_sod_abbreviated_sodium(self):
        cleaned, stripped = preprocess_ingredient_name("WARFARIN SOD")
        assert stripped is True
        assert cleaned.upper() == "WARFARIN"

    def test_paracetamol_usp(self):
        cleaned, stripped = preprocess_ingredient_name("PARACETAMOL USP")
        assert stripped is True
        assert cleaned.upper() == "PARACETAMOL"

    def test_paracetamol_bp(self):
        cleaned, stripped = preprocess_ingredient_name("PARACETAMOL BP")
        assert stripped is True
        assert cleaned.upper() == "PARACETAMOL"

    def test_flucloxacillin_sodium_bp(self):
        """'FLUCLOXACILLIN SODIUM BP' → 'FLUCLOXACILLIN' (iterative)."""
        cleaned, stripped = preprocess_ingredient_name("FLUCLOXACILLIN SODIUM BP")
        assert stripped is True
        assert cleaned.upper() == "FLUCLOXACILLIN"


# ===========================================================================
# F2c — Parenthetical and prose "as X" forms (Bug 2)
# ===========================================================================

class TestPreprocessAsFormStripping:

    def test_as_hydrochloride_parenthetical(self):
        """'Terbinafine (as Hydrochloride)' → 'Terbinafine'."""
        cleaned, stripped = preprocess_ingredient_name("TERBINAFINE (AS HYDROCHLORIDE)")
        assert stripped is True
        assert cleaned.upper() == "TERBINAFINE"

    def test_as_hydrochloride_prose(self):
        """'Fluoxetine as Hydrochloride' → 'Fluoxetine'."""
        cleaned, stripped = preprocess_ingredient_name("FLUOXETINE AS HYDROCHLORIDE")
        assert stripped is True
        assert cleaned.upper() == "FLUOXETINE"

    def test_as_sulphate_parenthetical(self):
        cleaned, stripped = preprocess_ingredient_name("SALBUTAMOL (AS SULPHATE)")
        assert stripped is True
        assert cleaned.upper() == "SALBUTAMOL"

    def test_as_bisulfate_prose(self):
        cleaned, stripped = preprocess_ingredient_name("CLOPIDOGREL AS BISULFATE")
        assert stripped is True
        assert cleaned.upper() == "CLOPIDOGREL"

    def test_as_hcl_parenthetical(self):
        """'Ciprofloxacin (as HCl)' — abbreviated salt inside parens."""
        cleaned, stripped = preprocess_ingredient_name("CIPROFLOXACIN (AS HCL)")
        assert stripped is True
        assert cleaned.upper() == "CIPROFLOXACIN"

    def test_as_trihydrate_parenthetical(self):
        """'Cefixime (as trihydrate)'."""
        cleaned, stripped = preprocess_ingredient_name("CEFIXIME (AS TRIHYDRATE)")
        assert stripped is True
        assert cleaned.upper() == "CEFIXIME"


# ===========================================================================
# F2d — "equivalent to" / "eq. to" prose forms (Bug 2)
# ===========================================================================

class TestPreprocessEquivalentTo:

    def test_equivalent_to_prose(self):
        """'Amlodipine besylate equivalent to amlodipine' — prose "equivalent to"."""
        cleaned, stripped = preprocess_ingredient_name(
            "AMLODIPINE BESYLATE EQUIVALENT TO AMLODIPINE"
        )
        assert stripped is True
        # After stripping "EQUIVALENT TO AMLODIPINE": "AMLODIPINE BESYLATE"
        # Then BESYLATE is stripped: "AMLODIPINE"
        assert cleaned.upper() == "AMLODIPINE"

    def test_eq_dot_to_stripped(self):
        """'Sildenafil Citrate eq. to Sildenafil' — dot variant."""
        cleaned, stripped = preprocess_ingredient_name(
            "SILDENAFIL CITRATE EQ. TO SILDENAFIL"
        )
        assert stripped is True
        assert "EQ" not in cleaned.upper()
        assert "SILDENAFIL" in cleaned.upper()

    def test_eq_to_original_still_works(self):
        """Original bare 'EQ TO' must still match."""
        cleaned, stripped = preprocess_ingredient_name(
            "AMOXICILLIN TRIHYDRATE EQ TO AMOXICILLIN"
        )
        assert stripped is True
        assert "EQ TO" not in cleaned


# ===========================================================================
# F2e — Fused parenthetical misspellings (Bug 2)
# ===========================================================================

class TestPreprocessFusedParenthetical:

    def test_astrihydrate_fused(self):
        """'Amoxicillin(astrihydrate)' — no space between drug and open paren."""
        cleaned, stripped = preprocess_ingredient_name("AMOXICILLIN(ASTRIHYDRATE)")
        assert stripped is True
        assert cleaned.upper() == "AMOXICILLIN"


# ===========================================================================
# (INN) annotation stripping
# ===========================================================================

class TestPreprocessINNAnnotation:

    def test_inn_stripped(self):
        """'LIDOCAINE (INN)' — classification tag must be removed."""
        cleaned, stripped = preprocess_ingredient_name("LIDOCAINE (INN)")
        assert stripped is True
        assert cleaned.upper() == "LIDOCAINE"
