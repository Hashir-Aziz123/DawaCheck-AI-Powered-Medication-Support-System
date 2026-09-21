"""
test_ws_e2e.py — End-to-end WebSocket push test.

Requires: FastAPI server + worker already running.
Run from the project root:
    python backend/scripts/test_ws_e2e.py

What this does:
  1. POST /check with two real drug IDs from the DB — creates a job.
  2. Immediately open WS /ws/jobs/{job_id} — before the job finishes.
  3. Block on ws.recv() — this is a real wait, not polling.
  4. Print the pushed result the moment the worker fires NOTIFY.
  5. Report total time from job submission to result received.
"""

import asyncio
import json
import time
from pathlib import Path

import httpx
import websockets
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / "infra" / ".env")

BASE_HTTP = "http://127.0.0.1:8000"
BASE_WS   = "ws://127.0.0.1:8000"

# These two drug IDs must exist in your 'drugs' table.
# Defaults are the Aspirin (1) and Paracetamol (2) rows that were already there.
DRUG_A_ID = 1
DRUG_B_ID = 2

DIVIDER = "─" * 60


def _print(label: str, value=None):
    if value is None:
        print(f"\n{label}")
    else:
        print(f"  {label}: {value}")


async def run_test():
    print(DIVIDER)
    print("DawaCheck WebSocket Push — End-to-End Test")
    print(DIVIDER)

    # ------------------------------------------------------------------
    # Step 1: POST /check
    # ------------------------------------------------------------------
    _print("STEP 1 — Submitting interaction check job")
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"{BASE_HTTP}/check",
            json={"drug_a_id": DRUG_A_ID, "drug_b_id": DRUG_B_ID},
        )

    if resp.status_code != 202:
        print(f"  [FAIL] POST /check returned {resp.status_code}: {resp.text}")
        return

    job = resp.json()
    job_id = job["job_id"]
    _print("status code", resp.status_code)
    _print("job_id", job_id)
    _print("initial status", job["status"])

    # ------------------------------------------------------------------
    # Step 2: Open WebSocket immediately (before job finishes)
    # ------------------------------------------------------------------
    print(DIVIDER)
    _print("STEP 2 — Connecting WebSocket")
    ws_url = f"{BASE_WS}/ws/jobs/{job_id}"
    _print("url", ws_url)

    t_connect = time.perf_counter()

    async with websockets.connect(ws_url) as ws:
        _print("connection", "OPEN — waiting for push (no polling)...")

        # ------------------------------------------------------------------
        # Step 3: Block on recv() — this proves push, not polling
        # ------------------------------------------------------------------
        t_wait_start = time.perf_counter()
        raw = await ws.recv()
        t_received = time.perf_counter()

    # ------------------------------------------------------------------
    # Step 4: Report result
    # ------------------------------------------------------------------
    print(DIVIDER)
    _print("STEP 3 — Result received via WebSocket push")
    data = json.loads(raw)

    elapsed_connect = t_wait_start - t_connect
    elapsed_total   = t_received - t_connect
    elapsed_wait    = t_received - t_wait_start

    _print("WS connect time",    f"{elapsed_connect*1000:.0f} ms")
    _print("Waiting duration",   f"{elapsed_wait:.2f} s  (single blocking recv, no poll loop)")
    _print("Total time (POST→push)", f"{elapsed_total:.2f} s")

    print(DIVIDER)
    _print("RESULT JSON")
    print(json.dumps(data, indent=2))
    print(DIVIDER)

    # ------------------------------------------------------------------
    # Step 5: Assertions
    # ------------------------------------------------------------------
    errors = []

    if "error" in data:
        errors.append(f"Got error from server: {data}")
    else:
        if data.get("status") not in ("done", "failed"):
            errors.append(f"Expected done/failed, got: {data.get('status')}")
        if data.get("status") == "done" and data.get("result") is None:
            errors.append("Status is 'done' but result is null")
        if str(job_id) not in str(data.get("job_id", "")):
            errors.append(f"job_id mismatch: expected {job_id}, got {data.get('job_id')}")

    print()
    if errors:
        print("  ❌  FAILED")
        for e in errors:
            print(f"     • {e}")
    else:
        print("  ✅  PASSED — result was pushed over WebSocket without polling")
    print(DIVIDER)


if __name__ == "__main__":
    asyncio.run(run_test())
