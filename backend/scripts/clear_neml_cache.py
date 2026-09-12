"""
clear_neml_cache.py
-------------------
Delete the cached neml_resolutions so the fixed code runs fresh.

Usage (from backend/):
    python scripts/clear_neml_cache.py

After running, execute:
    python scripts/resolve_neml_dataset.py
"""

import json
from pathlib import Path

CACHE_FILE = Path(__file__).resolve().parent / ".cache" / "neml_resolutions.json"


def main() -> None:
    if not CACHE_FILE.exists():
        print(f"Cache file not found: {CACHE_FILE}")
        return

    data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    n = len(data)
    CACHE_FILE.write_text("{}", encoding="utf-8")
    print(f"Cleared {n} cached resolution(s) from {CACHE_FILE}")
    print("You can now run:  python scripts/resolve_neml_dataset.py")


if __name__ == "__main__":
    main()
