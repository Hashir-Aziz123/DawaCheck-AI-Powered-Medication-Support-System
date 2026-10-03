# Infrastructure & Data Models

## Docker Setup

All local services are managed via Docker Compose in `infra/`.

```bash
cd infra
docker compose up -d
```

This starts:
- **PostgreSQL 16** (`drug_assistant_db`) on port 5432
- **RabbitMQ 3** (`drug_assistant_mq`) on ports 5672 (AMQP) and 15672 (management UI)

`infra/init.sql` runs once on first container creation to enable the `pg_trgm` extension (required for fuzzy brand-name search).

Environment variables are loaded from `infra/.env` (see `infra/.env.example` for the required keys).

---

## Database Schema

Defined as SQLAlchemy ORM models in `backend/core/db/models.py`.

### `drugs`
One row per resolved drug (brand-level).

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `brand_name` | text | e.g. "Augmentin" |
| `search_name` | text (indexed) | Cleaned fuzzy-search key — no dosage form, no dose (e.g. "augmentin") |
| `drap_reg_no` | text | DRAP registration number — internal only, never returned by API |
| `dosage_form` | text | e.g. "Tablet" |
| `company_name` | text | |
| `resolved_at` | timestamp | |

### `drug_ingredients`
One row per generic ingredient per drug.

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `drug_id` | int FK → drugs | Cascade delete |
| `generic_name` | text | As parsed from DRAP (e.g. "Guaifenesin") |
| `dose` | text | e.g. "100 mg" |
| `rxcui` | text (indexed) | RxNorm concept ID — canonical join key |
| `rxnorm_name` | text | Human-readable RxNorm name |

Unique constraint: `(drug_id, generic_name, dose)`.

### `fda_labels`
One row per RxCUI. Shared across all brands containing the same generic ingredient.

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `rxcui` | text (unique) | Join key |
| `rxnorm_name` | text | |
| `drug_interactions` | text | Extracted from FDA label |
| `warnings` | text | Extracted from FDA label |
| `boxed_warning` | text | Extracted from FDA label |
| `raw_response` | JSONB | Full openFDA payload for future use |
| `fetched_at` | timestamp | |

### `drug_classes`
One row per RxCUI. Parallel to `fda_labels`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `rxcui` | text (unique) | |
| `class_names` | JSONB (list) | e.g. `["Selective Serotonin Reuptake Inhibitors"]` |
| `class_sources` | JSONB (list) | e.g. `["ATC", "MEDRT-MOA"]` — parallel to class_names |
| `fetched_at` | timestamp | |

### `interaction_jobs`
One row per drug-interaction check request.

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `drug_a_id` | int FK → drugs | |
| `drug_b_id` | int FK → drugs | |
| `status` | text | `queued` \| `processing` \| `done` \| `failed` |
| `result` | JSONB | Final answer dict from LangGraph (populated on `done`) |
| `error_message` | text | Populated on `failed` |
| `created_at` | timestamp | |
| `completed_at` | timestamp | |

---

## Message Queue

RabbitMQ is used for async job dispatch. The API publishes; the worker consumes.

**Queue name:** `interaction_checks` (durable)

**Message shape:**
```json
{ "job_id": "uuid", "drug_a_id": 7, "drug_b_id": 42 }
```

The worker sets `prefetch_count=1` so it processes one job at a time.

### Worker Event Loop

The worker uses a persistent background event loop (one `asyncio` loop on a dedicated thread) to avoid the Windows asyncpg crash caused by creating and destroying a new event loop per message. The SQLAlchemy async engine and session factory are created inside that loop thread and bound to it for their entire lifetime.

Pika (the RabbitMQ client) runs synchronously on the main thread. When a message arrives, it calls `asyncio.run_coroutine_threadsafe()` to submit the async job handler to the persistent loop and blocks until it completes before acknowledging the message.

---

## Caching

**DB-as-cache:** Resolved drugs are persisted to Postgres on first lookup. Subsequent `/resolve` requests hit the DB without calling external APIs.

**`JsonCache` (disk-backed):** `backend/core/cache.py` provides a simple `.cache/` directory cache used by batch scripts (e.g. `create_dataset.py`) to avoid re-hitting APIs during iterative development. Not used by the live API.

Write strategy for DB: `INSERT ... ON CONFLICT DO NOTHING` (for `fda_labels` and `drug_classes`), so stale entries are preserved rather than silently overwritten. A future refresh job would handle updates.

---

## Environment Variables

All environment variables are loaded from `infra/.env`. Key variables:

| Variable | Used by |
|----------|---------|
| `DATABASE_URL` | Backend (SQLAlchemy async URL: `postgresql+asyncpg://...`) |
| `RABBITMQ_URL` | Backend + Worker |
| `GROQ_API_KEY` | Worker (LangGraph LLM calls) |
| `LANGCHAIN_TRACING_V2` | Worker (LangSmith tracing, optional) |
| `LANGCHAIN_API_KEY` | Worker (LangSmith, optional) |
