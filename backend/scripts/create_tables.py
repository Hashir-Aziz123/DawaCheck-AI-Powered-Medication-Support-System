import asyncio
import sys
from pathlib import Path

# backend/scripts/create_tables.py -> parents[1] is backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db.session import engine
from core.db.models import Base


async def create_all():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Tables created.")


if __name__ == "__main__":
    asyncio.run(create_all())