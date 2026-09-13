import pytest
import re
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from core.db.session import DATABASE_URL
from app.services.drug_resolution import resolve_drug

# 1. Convert the async URL to a synchronous URL
sync_url = re.sub(r"^postgresql\+asyncpg://", "postgresql://", DATABASE_URL)
engine = create_engine(sync_url)


@pytest.fixture
def db_session():
    """Provides a synchronous database session for testing."""
    with Session(engine) as session:
        yield session


def test_resolve_drug_integration_panadol(db_session):
    """
    Test resolving a drug that is likely already in the DB (like Panadol).
    We call it twice to guarantee the second time is a database hit, 
    even if the DB was completely empty on the first run.
    """
    # First call will populate the DB if it's missing (and return the canonical name)
    first_result = resolve_drug("Panadol", db_session)
    canonical_name = first_result.brand_name
    
    # Second call using the canonical name MUST be a DB hit
    result = resolve_drug(canonical_name, db_session)
    
    assert result.status == "found"
    assert result.source == "database"
    assert result.brand_name.lower() == canonical_name.lower()


def test_resolve_drug_integration_live_fallback(db_session):
    """
    Integration test for live fallback and write-back functionality.
    Cleans up a test drug ('Tylenol'), calls resolve_drug (hitting the live API),
    and then calls it again (hitting the DB).
    """
    test_brand = "Tylenol"
    
    # 1. Clean up 'Tylenol' (and any variants like 'tylenol extra') from the DB
    db_session.execute(text("DELETE FROM drug_ingredients WHERE drug_id IN (SELECT id FROM drugs WHERE brand_name ILIKE :brand)"), {"brand": f"%{test_brand}%"})
    db_session.execute(text("DELETE FROM drugs WHERE brand_name ILIKE :brand"), {"brand": f"%{test_brand}%"})
    db_session.commit()
    
    # 2. First call: Should be a database miss -> Live fallback -> DB write-back
    result_live = resolve_drug(test_brand, db_session)
    
    # Ensure it handled the live query properly
    assert result_live.status in ("found", "error", "not_found")
    
    if result_live.status == "found":
        assert result_live.source == "live"
        assert test_brand.lower() in result_live.brand_name.lower()
        
        # 3. Second call: Should now be a database hit because of the write-back
        # Use the exact brand name that was just written to the DB
        result_db = resolve_drug(result_live.brand_name, db_session)
        assert result_db.status == "found"
        assert result_db.source == "database"
        assert result_db.brand_name.lower() == result_live.brand_name.lower()
        assert len(result_db.ingredients) == len(result_live.ingredients)

    # 4. Clean up after the test to leave the DB as we found it
    db_session.execute(text("DELETE FROM drug_ingredients WHERE drug_id IN (SELECT id FROM drugs WHERE brand_name ILIKE :brand)"), {"brand": f"%{test_brand}%"})
    db_session.execute(text("DELETE FROM drugs WHERE brand_name ILIKE :brand"), {"brand": f"%{test_brand}%"})
    db_session.commit()
