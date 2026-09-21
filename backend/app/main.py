"""
FastAPI application entry point.

Keep this minimal — only register routes and configure logging.
Middleware, CORS, authentication, etc. are out of scope until needed.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routes.resolve import router as resolve_router
from app.routes.check import router as check_router
from app.routes.ws import router as ws_router
from core.db.listeners import start_listener

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

logger = logging.getLogger(__name__)

_listener_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the Postgres LISTEN background task on startup; cancel on shutdown."""
    global _listener_task
    logger.info("startup | starting Postgres LISTEN background task")
    _listener_task = asyncio.create_task(start_listener())
    try:
        yield
    finally:
        if _listener_task and not _listener_task.done():
            logger.info("shutdown | cancelling Postgres LISTEN task")
            _listener_task.cancel()
            try:
                await _listener_task
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="DawaCheck API",
    description="AI-powered medication support system — drug resolution and interaction checking.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(resolve_router)
app.include_router(check_router)
app.include_router(ws_router)
