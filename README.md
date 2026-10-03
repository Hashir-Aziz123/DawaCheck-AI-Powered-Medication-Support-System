# DawaCheck — AI-Powered Drug Interaction Checker

DawaCheck resolves Pakistani drug names (brand and generic) to canonical medical concepts and checks pairwise drug interactions using FDA label data, verified by an LLM pipeline.

> Pakistani brand names aren't in global drug databases. DawaCheck bridges that gap by routing through DRAP (Pakistan's drug authority) → RxNorm → openFDA → RxClass before any AI reasoning happens.

---

## How It Works

**1. Drug Resolution**

Type a drug name (brand or generic, typos included). The system runs a 6-step fallback chain:

```
DB brand exact → DB generic exact → DB brand fuzzy → DB generic fuzzy
→ Live DRAP brand search → Live DRAP generic search
```

On a live hit, DRAP resolves the brand to its generic composition, RxNorm assigns canonical RxCUI identifiers, openFDA fetches interaction/warning text, and RxClass fetches pharmacological class data. Results are persisted to Postgres so future lookups are instant.

**2. Interaction Check**

Pick two resolved drugs → submit a check. The LangGraph pipeline runs:

```
fetch_context → check_interaction (LLM 1) → verify_groundedness (LLM 2)
                                                    │
                                    ┌───────────────┴────────────────┐
                               grounded=true                  grounded=false
                                    │                                │
                          interaction_found                unverifiable / none_found
```

- **LLM 1** searches the FDA label text for direct or class-level interaction mentions.
- **LLM 2** verifies the citation actually exists in the source text. If it doesn't, the result is `unverifiable` — the system never silently presents an unconfirmed claim.

Results are pushed to the frontend in real time via WebSocket (Postgres LISTEN/NOTIFY, no polling).

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Next.js, TypeScript, Tailwind |
| API | FastAPI, SQLAlchemy, asyncpg |
| AI Pipeline | LangGraph, Groq (GPT-OSS-120B), LangSmith |
| Queue | RabbitMQ (pika) |
| Database | PostgreSQL 16 (pg_trgm for fuzzy search) |
| External APIs | DRAP, RxNorm (NLM), openFDA, RxClass (NLM) |
| Infrastructure | Docker Compose (local), Railway + CloudAMQP (production) |

---

## Project Structure

```
mediAid_langraph/
├── backend/
│   ├── app/                    # FastAPI — routes, schemas, services
│   │   ├── routes/             # /resolve, /check, /ws, /drugs
│   │   └── services/           # drug_resolution.py, queue_publisher.py
│   ├── core/                   # Shared logic — DB models, API clients, types
│   │   ├── clients/            # DRAP, RxNorm, openFDA, RxClass
│   │   └── db/                 # SQLAlchemy models, session, LISTEN listener
│   └── worker/
│       ├── consumer.py         # RabbitMQ consumer
│       └── langgraph_pipeline/ # graph.py, nodes.py, prompts.py, state.py
├── frontend/                   # Next.js app
├── infra/                      # docker-compose.yml, .env.example
└── docs/                       # Architecture and pipeline documentation
```

---

## Local Development

### Prerequisites
- Docker Desktop
- Python 3.12+
- Node.js 20+
- A [Groq API key](https://console.groq.com) (free tier is sufficient)

### 1. Start infrastructure

```bash
cd infra
cp .env.example .env   # fill in your keys
docker compose up -d
```

This starts PostgreSQL on port 5432 and RabbitMQ on port 5672 (management UI at port 15672).

### 2. Create DB tables

```bash
cd backend
python -c "
import asyncio
from core.db.session import engine
from core.db.models import Base
async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
asyncio.run(main())
"
```

### 3. Start the backend API

```bash
cd backend
uvicorn app.main:app --reload --port 8000
```

### 4. Start the worker

```bash
cd backend
python -m worker.consumer
```

### 5. Start the frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend runs at `http://localhost:3000`. API docs at `http://localhost:8000/docs`.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/resolve` | Resolve a drug name |
| `POST` | `/check` | Submit an interaction check job (async) |
| `GET` | `/check/{job_id}` | Poll job status |
| `WS` | `/ws/jobs/{job_id}` | Real-time job completion push |
| `GET` | `/drugs` | List all drugs in the dataset |

Full API reference: [`docs/api.md`](docs/api.md)

---

## Running Tests

```bash
cd backend

# Unit tests (no external services needed)
pytest tests/unit/

# Integration tests (requires Docker containers running)
pytest tests/integration/
```

---

## Environment Variables

Copy `infra/.env.example` to `infra/.env` and fill in:

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL connection string (`postgresql+asyncpg://...`) |
| `RABBITMQ_URL` | RabbitMQ connection string (`amqp://...`) |
| `GROQ_API_KEY` | Groq API key for LLM calls |
| `LANGCHAIN_TRACING_V2` | `true` to enable LangSmith tracing (optional) |
| `LANGCHAIN_API_KEY` | LangSmith API key (optional) |

---

## Documentation

| Doc | Contents |
|-----|----------|
| [`docs/overview.md`](docs/overview.md) | System architecture and request lifecycle |
| [`docs/pipeline.md`](docs/pipeline.md) | Drug resolution pipeline (DRAP → RxNorm → openFDA → RxClass) |
| [`docs/langgraph.md`](docs/langgraph.md) | LangGraph pipeline — nodes, state, prompts, verification |
| [`docs/api.md`](docs/api.md) | API reference and WebSocket protocol |
| [`docs/infrastructure.md`](docs/infrastructure.md) | Database schema, Docker setup, message queue |
| [`docs/testing.md`](docs/testing.md) | Test suite and mocking patterns |
| [`docs/cicd.md`](docs/cicd.md) | CI/CD pipeline (GitHub Actions + Railway + Vercel) |
