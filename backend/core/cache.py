"""
Disk-based JSON cache for batch pipeline idempotency.

``JsonCache`` is a simple key→JSON-value store backed by a single ``.json``
file on disk.  Every ``set()`` call flushes the whole file so that a partial
run can be resumed without re-hitting already-resolved entries.

**Design note — caching is the CALLER's responsibility.**
The library functions in ``core/clients/`` do NOT use ``JsonCache``; they
always perform fresh HTTP resolutions.  Caching is wired in by callers that
need it (e.g. ``scripts/create_dataset.py``).  A live-API caller would check
Postgres instead, falling back to the library functions on a miss.

Usage::

    from pathlib import Path
    from core.cache import JsonCache

    CACHE_DIR = Path(__file__).resolve().parent / ".cache"
    CACHE_DIR.mkdir(exist_ok=True)

    cache = JsonCache("my_cache", CACHE_DIR)
    cache.set("key", {"some": "data"})
    value = cache.get("key")   # → {"some": "data"}
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class JsonCache:
    """
    A lightweight, file-backed key→JSON-value cache.

    Parameters
    ----------
    name:
        Base name for the backing ``.json`` file (no extension).
    cache_dir:
        Directory in which the ``.json`` file is stored.  The directory
        must already exist before constructing the cache.
    """

    def __init__(self, name: str, cache_dir: Path) -> None:
        self.path = cache_dir / f"{name}.json"
        self._data: dict = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning(
                    "Cache file %s was corrupt — starting fresh.", self.path
                )

    def get(self, key: str):
        """Return the cached value for *key*, or ``None`` on a miss."""
        return self._data.get(key)

    def set(self, key: str, value) -> None:
        """
        Store *value* under *key* and flush the entire cache to disk.

        *value* must be JSON-serialisable.
        """
        self._data[key] = value
        self.path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
