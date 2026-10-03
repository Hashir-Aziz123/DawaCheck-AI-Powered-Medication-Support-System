# DawaCheck — System Overview

DawaCheck is an AI-powered medication support system for Pakistani drug names. It resolves brand/generic drug queries to canonical medical concepts, fetches FDA safety data, and checks pairwise drug interactions using an LLM-backed pipeline.

## What it does

1. A user searches for a drug by brand or generic name.
2. The system resolves it against DRAP (Pakistan's drug registry), normalizes ingredients via RxNorm, and fetches FDA interaction/warning text.
3. The user picks two resolved drugs and submits an interaction check.
4. A worker runs the LangGraph pipeline — two LLM calls with a groundedness verification step — and pushes the result back to the client in real time via WebSocket.

---

## High-Level Architecture

```
┌─────────────┐    HTTP/WS    ┌──────────────────┐
│  Next.js    │ ◄──────────── │  FastAPI backend  │
│  frontend   │ ────────────► │  (app/)           │
└─────────────┘               └────────┬──────────┘
                                       │ RabbitMQ
                               ┌───────▼───────────┐
                               │  Worker process    │
                               │  (worker/)         │
                               │                    │
                               │  LangGraph pipeline│
                               │  (Groq LLM)        │
                               └───────┬────────────┘
                                       │
                               ┌───────▼────────────┐
                               │  PostgreSQL         │
                               │  (drugs, FDA labels,│
                               │   interaction jobs) │
                               └────────────────────┘
```

### Components

| Component | Location | Purpose |
|-----------|----------|---------|
| Frontend | `frontend/` | Next.js app — drug search, interaction UI |
| API | `backend/app/` | FastAPI — resolve, check, WebSocket endpoints |
| Worker | `backend/worker/` | RabbitMQ consumer + LangGraph interaction pipeline |
| Core | `backend/core/` | Resolution clients (DRAP, RxNorm, openFDA, RxClass), DB models |
| Infra | `infra/` | Docker Compose for Postgres + RabbitMQ |

---

## Request Lifecycle

```
User types drug name
    │
    ▼
POST /resolve  →  DB cache hit?  →  Yes: return stored data
                       │
                       No
                       │
                    DRAP → RxNorm → openFDA → RxClass
                       │
                    Persist to DB, return result
    │
User picks Drug A + Drug B → POST /check
    │
    ▼
Job created in DB (status=queued)
    │
    ▼
Published to RabbitMQ "interaction_checks" queue
    │
    ▼
Worker consumes message → runs LangGraph pipeline
    │
    ▼
Result stored in DB → Postgres NOTIFY fired
    │
    ▼
FastAPI LISTEN task receives NOTIFY → signals WebSocket event
    │
    ▼
WS /ws/jobs/{job_id} sends result to client
```

---

## Documentation Index

| File | Contents |
|------|----------|
| [overview.md](overview.md) | This file — system summary and lifecycle |
| [pipeline.md](pipeline.md) | Drug resolution pipeline (DRAP → RxNorm → openFDA → RxClass) |
| [langgraph.md](langgraph.md) | LangGraph interaction-checking pipeline (nodes, state, prompts) |
| [api.md](api.md) | FastAPI routes, schemas, WebSocket protocol |
| [infrastructure.md](infrastructure.md) | Database schema, Docker setup, message queue |
| [testing.md](testing.md) | Test suite structure and how to run tests |
| [cicd.md](cicd.md) | CI/CD pipeline (GitHub Actions, deployment) |
