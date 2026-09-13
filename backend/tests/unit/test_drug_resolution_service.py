import pytest
from unittest.mock import MagicMock, patch

from core.status import Status
from core.types import DrugResolution, Ingredient
from core.db.models import Drug, DrugIngredient, FdaLabel
from app.services.drug_resolution import resolve_drug


def test_resolve_drug_database_hit():
    mock_session = MagicMock()
    
    mock_drug = Drug(
        brand_name="Panadol",
        dosage_form="tablet"
    )
    mock_ing = DrugIngredient(
        generic_name="Paracetamol",
        dose="500mg",
        rxcui="161"
    )
    mock_drug.ingredients = [mock_ing]
    
    mock_label = FdaLabel(
        rxcui="161",
        rxnorm_name="Acetaminophen",
        drug_interactions="None",
        warnings="Liver damage",
        boxed_warning=None
    )
    
    mock_result_drug = MagicMock()
    mock_result_drug.scalars().all.return_value = [mock_drug]
    
    mock_result_label = MagicMock()
    mock_result_label.scalars().all.return_value = [mock_label]
    
    mock_session.execute.side_effect = [mock_result_drug, mock_result_label]

    result = resolve_drug("Panadol", mock_session)

    assert result.status == "found"
    assert result.source == "database"
    assert result.matched_as == "brand"
    assert result.brand_name == "Panadol"
    assert len(result.ingredients) == 1
    assert result.ingredients[0].generic_name == "Paracetamol"
    assert result.ingredients[0].warnings == "Liver damage"


@patch("app.services.drug_resolution.build_drug_record")
@patch("app.services.drug_resolution.build_generic_record")
def test_resolve_drug_live_fallback_and_writeback(mock_build_generic, mock_build_brand):
    mock_ing = Ingredient(
        name="Aspirin",
        amount="81mg",
        rxcui="1191",
        rxnorm_name="Aspirin",
        drug_interactions="Bleeding risk",
        raw_fda_response={"raw": "data"}
    )
    mock_resolution = DrugResolution(
        brand_query="Aspirin-Not-In-DB",
        status=Status.OK,
        matched_name="Aspirin",
        drap_reg_no="12345",
        ingredients=[mock_ing]
    )
    mock_build_brand.return_value = mock_resolution
    
    mock_session = MagicMock()
    
    mock_result_empty = MagicMock()
    mock_result_empty.scalars().all.return_value = []
    
    mock_result_empty_unique = MagicMock()
    mock_result_empty_unique.scalars().unique().all.return_value = []
    
    mock_result_empty_all = MagicMock()
    mock_result_empty_all.all.return_value = []
    
    mock_result_scalar_empty = MagicMock()
    mock_result_scalar_empty.scalar.return_value = None
    
    # 1. DB Brand Search (empty)
    # 2. DB Generic Search (empty)
    # 3. DB Brand Fuzzy Search (empty)
    # 4. DB Generic Fuzzy Search (empty)
    # 5. Live Brand Fallback succeeds -> _persist_live_record
    #    - select(Drug.id) (empty)
    #    - insert(DrugIngredient)
    #    - insert(FdaLabel)
    mock_session.execute.side_effect = [
        mock_result_empty, 
        mock_result_empty_unique,
        mock_result_empty_all,
        mock_result_empty_all,
        mock_result_scalar_empty, 
        None, 
        None
    ]
    
    result = resolve_drug("Aspirin-Not-In-DB", mock_session)
    
    assert result.status == "found"
    assert result.source == "live"
    assert result.matched_as == "brand"
    assert result.brand_name == "Aspirin"
    
    assert mock_session.add.called
    added_drug = mock_session.add.call_args[0][0]
    assert added_drug.brand_name == "Aspirin"
    assert mock_session.commit.called


def test_resolve_drug_generic_database_hit_ambiguous():
    mock_session = MagicMock()
    
    # DB Brand Search -> Empty
    mock_result_brand_empty = MagicMock()
    mock_result_brand_empty.scalars().all.return_value = []
    
    # DB Generic Search -> 2 Drugs (ambiguous)
    mock_drug1 = Drug(brand_name="Panadol")
    mock_drug2 = Drug(brand_name="Tylenol")
    mock_result_generic_ambiguous = MagicMock()
    mock_result_generic_ambiguous.scalars().unique().all.return_value = [mock_drug1, mock_drug2]
    
    mock_session.execute.side_effect = [
        mock_result_brand_empty, 
        mock_result_generic_ambiguous
    ]

    result = resolve_drug("Paracetamol", mock_session)

    assert result.status == "ambiguous"
    assert result.source == "database"
    assert result.matched_as == "generic"
    assert result.brand_name == "Paracetamol"


@patch("app.services.drug_resolution.build_drug_record")
@patch("app.services.drug_resolution.build_generic_record")
def test_resolve_drug_generic_live_fallback(mock_build_generic, mock_build_brand):
    # Brand live search fails
    mock_build_brand.return_value = DrugResolution(
        brand_query="NewGeneric",
        status=Status.NOT_FOUND
    )
    
    # Generic live search succeeds
    mock_ing = Ingredient(
        name="NewGeneric",
        amount="10mg",
        rxcui="9999",
        rxnorm_name="NewGeneric",
        drug_interactions="None",
        raw_fda_response={}
    )
    mock_build_generic.return_value = DrugResolution(
        brand_query="NewGeneric",
        status=Status.OK,
        matched_name="NewGenericBrand",
        drap_reg_no="98765",
        ingredients=[mock_ing]
    )
    
    mock_session = MagicMock()
    
    mock_result_empty = MagicMock()
    mock_result_empty.scalars().all.return_value = []
    
    mock_result_empty_unique = MagicMock()
    mock_result_empty_unique.scalars().unique().all.return_value = []
    
    mock_result_empty_all = MagicMock()
    mock_result_empty_all.all.return_value = []
    
    mock_result_scalar_empty = MagicMock()
    mock_result_scalar_empty.scalar.return_value = None
    
    # 1. DB Brand Search (empty)
    # 2. DB Generic Search (empty)
    # 3. DB Brand Fuzzy Search (empty)
    # 4. DB Generic Fuzzy Search (empty)
    # 5. Live Brand Fallback -> fails
    # 6. Live Generic Fallback -> succeeds -> _persist_live_record
    #    - select(Drug.id) (empty)
    #    - insert(DrugIngredient)
    #    - insert(FdaLabel)
    mock_session.execute.side_effect = [
        mock_result_empty, 
        mock_result_empty_unique, 
        mock_result_empty_all,
        mock_result_empty_all,
        mock_result_scalar_empty, 
        None, 
        None
    ]
    
    result = resolve_drug("NewGeneric", mock_session)
    
    assert result.status == "found"
    assert result.source == "live"
    assert result.matched_as == "generic"
    assert result.brand_name == "NewGenericBrand"
    
    assert mock_session.add.called
    added_drug = mock_session.add.call_args[0][0]
    assert added_drug.brand_name == "NewGenericBrand"
    assert mock_session.commit.called


def test_resolve_drug_brand_fuzzy_hit():
    mock_session = MagicMock()
    
    mock_result_empty = MagicMock()
    mock_result_empty.scalars().all.return_value = []
    
    mock_result_empty_unique = MagicMock()
    mock_result_empty_unique.scalars().unique().all.return_value = []
    
    mock_drug = Drug(brand_name="Panadol", search_name="panadol")
    mock_drug.ingredients = []
    mock_result_fuzzy_brand = MagicMock()
    fuzzy_match = MagicMock()
    fuzzy_match.Drug = mock_drug
    fuzzy_match.sim = 0.8
    mock_result_fuzzy_brand.all.return_value = [fuzzy_match]
    
    # 1. DB Brand Search (empty)
    # 2. DB Generic Search (empty)
    # 3. DB Brand Fuzzy Search (1 hit)
    mock_session.execute.side_effect = [
        mock_result_empty, 
        mock_result_empty_unique,
        mock_result_fuzzy_brand
    ]

    result = resolve_drug("Panidal", mock_session)

    assert result.status == "found"
    assert result.source == "database"
    assert result.matched_as == "brand"
    assert result.brand_name == "Panadol"


def test_resolve_drug_generic_fuzzy_hit():
    mock_session = MagicMock()
    
    mock_result_empty = MagicMock()
    mock_result_empty.scalars().all.return_value = []
    
    mock_result_empty_unique = MagicMock()
    mock_result_empty_unique.scalars().unique().all.return_value = []
    
    mock_result_empty_all = MagicMock()
    mock_result_empty_all.all.return_value = []
    
    mock_drug = Drug(brand_name="Tylenol", search_name="tylenol")
    mock_drug.ingredients = []
    mock_result_fuzzy_generic = MagicMock()
    fuzzy_match = MagicMock()
    fuzzy_match.Drug = mock_drug
    fuzzy_match.sim = 0.75
    mock_result_fuzzy_generic.all.return_value = [fuzzy_match]
    
    # 1. DB Brand Search (empty)
    # 2. DB Generic Search (empty)
    # 3. DB Brand Fuzzy Search (empty)
    # 4. DB Generic Fuzzy Search (1 hit)
    mock_session.execute.side_effect = [
        mock_result_empty, 
        mock_result_empty_unique,
        mock_result_empty_all,
        mock_result_fuzzy_generic
    ]

    result = resolve_drug("Acetminofen", mock_session)

    assert result.status == "found"
    assert result.source == "database"
    assert result.matched_as == "generic"
    assert result.brand_name == "Tylenol"
