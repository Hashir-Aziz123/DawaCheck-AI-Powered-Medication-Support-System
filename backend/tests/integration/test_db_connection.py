import pytest
from sqlalchemy import text
from core.db.session import engine


@pytest.mark.asyncio
async def test_database_connection():
    """Confirms the app can actually reach Postgres and run a query."""
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar() == 1


@pytest.mark.asyncio
async def test_pg_trgm_extension_enabled():
    """Confirms init.sql actually ran — pg_trgm is required for brand_name fuzzy search."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'pg_trgm'")
        )
        assert result.scalar() == "pg_trgm"