"""
FastAPI dependency providers for the backend app.

The core DB session (core/db/session.py) uses asyncpg for the async worker
pipeline.  resolve_drug() is deliberately synchronous (see architecture
decision), so the route layer needs a plain synchronous Session.  This module
provides that without touching the async engine.
"""

import re
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from core.db.session import DATABASE_URL

# Convert  postgresql+asyncpg://...  →  postgresql://...
# so SQLAlchemy uses the standard psycopg2 driver synchronously.
_SYNC_DATABASE_URL = re.sub(r"^postgresql\+asyncpg://", "postgresql://", DATABASE_URL)

_sync_engine = create_engine(_SYNC_DATABASE_URL, pool_pre_ping=True)
_SyncSessionLocal = sessionmaker(bind=_sync_engine, autocommit=False, autoflush=False)


def get_sync_db() -> Generator[Session, None, None]:
    """Yield a synchronous SQLAlchemy Session; close it when the request ends."""
    db = _SyncSessionLocal()
    try:
        yield db
    finally:
        db.close()
