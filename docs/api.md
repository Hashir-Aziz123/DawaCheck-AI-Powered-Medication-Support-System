# API Reference

The FastAPI backend exposes four routers. All responses use JSON. Sync DB access uses `Session` (SQLAlchemy sync); async DB access (WebSocket, listener) uses `async_session`.

Base URL (local dev): `http://localhost:8000`

---

## POST /resolve

Resolve a drug name to its canonical form and fetch FDA data.

**Request**
```json
{ "query": "Augmentin" }
```
`query` is stripped and validated by Pydantic (blank strings rejected with 422).

**Response statuses**

| Status | Meaning |
|--------|---------|
| `resolved` | Single unambiguous match found |
| `ambiguous` | Multiple candidates — client should ask user to pick one |
| `not_found` | Nothing matched across all 6 resolution paths |

**`resolved` response**
```json
{
  "status": "resolved",
  "drug": {
    "display_name": "Augmentin Tablet",
    "dosage_form": "Tablet",
    "drug_id": 42
  },
  "drug_id": 42
}
```

**`ambiguous` response**
```json
{
  "status": "ambiguous",
  "candidates": [
    { "display_name": "Paracetamol Tablet", "dosage_form": "Tablet", "drug_id": 7 },
    { "display_name": "Paracetamol Syrup", "dosage_form": "Syrup", "drug_id": 8 }
  ]
}
```

**Error codes:** 422 (invalid body), 500 (unexpected error), 503 (upstream API unavailable)

---

## POST /check

Enqueue an async drug-interaction check job.

**Request**
```json
{ "drug_a_id": 7, "drug_b_id": 42 }
```

**Response** — 202 Accepted
```json
{ "job_id": "uuid-string", "status": "queued" }
```

Both drug IDs must exist in the `drugs` table. The job is inserted with `status="queued"` and published to RabbitMQ. If RabbitMQ is unreachable, the job is marked `failed` and 503 is returned.

**Error codes:** 404 (drug not found), 503 (RabbitMQ unavailable)

---

## GET /check/{job_id}

Poll the status of a check job.

**Response**
```json
{
  "job_id": "uuid-string",
  "status": "done",
  "result": {
    "final_status": "interaction_found",
    "drug_a": "Augmentin",
    "drug_b": "Warfarin",
    "interaction_claim": "...",
    "citation_text": "...",
    "match_type": "direct",
    "is_grounded": true,
    "groundedness_reasoning": "..."
  },
  "error_message": null
}
```

`status` values: `queued` | `processing` | `done` | `failed`

`result` is populated only when `status="done"`. `error_message` is populated only when `status="failed"`.

`final_status` values inside `result`: `interaction_found` | `none_found` | `unverifiable`

**Error codes:** 404 (job not found)

---

## WS /ws/jobs/{job_id}

WebSocket endpoint for real-time job completion push.

**Protocol**
1. Client connects.
2. If job is already `done` or `failed`, server sends the result immediately and closes.
3. If job is still `queued` or `processing`, server registers a waiter and blocks.
4. When Postgres fires `NOTIFY interaction_jobs_channel`, the waiter is signalled.
5. Server re-reads the job row from DB, sends the `CheckStatusResponse` JSON, and closes.
6. Timeout: 180 seconds. If the job doesn't complete in time, server sends `{"error": "timeout"}` and closes.

The response shape is identical to `GET /check/{job_id}`, so the client can use the same handler for both polling and WebSocket push.

**Error messages**
```json
{ "error": "timeout", "message": "Job did not complete within 180s.", "job_id": "..." }
{ "error": "Job <id> not found." }
{ "error": "internal_error", "detail": "..." }
```

---

## GET /drugs

Return all drugs in the dataset (browse/list view).

**Response**
```json
{
  "total": 87,
  "drugs": [
    {
      "brand_name": "Augmentin",
      "generic_names": ["Amoxicillin", "Clavulanic Acid"],
      "dosage_form": "Tablet"
    }
  ]
}
```

Results sorted alphabetically by `brand_name`. No pagination — the dataset is small (~65–90 entries). No internal IDs or DRAP registration numbers are exposed.

---

## LISTEN/NOTIFY Architecture

The FastAPI process runs a single background task (`core/db/listeners.py:start_listener`) that keeps a dedicated raw `asyncpg` connection in `LISTEN` mode on `interaction_jobs_channel`. This connection is separate from the SQLAlchemy pool and is never used for normal queries.

When the worker finishes a job, it issues:
```sql
SELECT pg_notify('interaction_jobs_channel', '<job_id>');
```
inside the same transaction that commits the result, so the notification is guaranteed to arrive after the DB row is visible.

The listener task calls `asyncio.Event.set()` on the matching waiter, which wakes the WebSocket handler to fetch and send the result.

---

## Startup / Shutdown

`backend/app/main.py` uses a FastAPI `lifespan` context to start `start_listener()` as an `asyncio.Task` on startup and cancel it cleanly on shutdown.

CORS is configured to allow `http://localhost:3000` and `http://127.0.0.1:3000` (the Next.js dev server). Restrict `allow_origins` in production.
