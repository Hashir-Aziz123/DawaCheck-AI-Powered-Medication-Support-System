import pytest
from sqlalchemy import text
from core.db.session import engine


@pytest.mark.asyncio
async def test_tables_exist():
    """Confirms create_tables.py successfully created all four expected tables."""
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        ))
        tables = {row[0] for row in result}
        assert {"drugs", "drug_ingredients", "fda_labels", "interaction_jobs"} <= tables