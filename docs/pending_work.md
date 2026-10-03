# Pending Work

Most of the originally-planned components are now implemented. This file tracks remaining gaps and future improvements.

## Implemented (no longer pending)

- FastAPI application (`backend/app/`) — all routes live: `/resolve`, `/check`, `/ws/jobs/{id}`, `/drugs`
- LangGraph pipeline (`backend/worker/langgraph_pipeline/`) — graph, nodes, prompts, state all complete
- RabbitMQ consumer (`backend/worker/consumer.py`) — running, processes jobs end-to-end
- Postgres LISTEN/NOTIFY push (`core/db/listeners.py`) — real-time WebSocket job completion
- Drug class resolution (`core/clients/rxclass_client.py`) — pharmacological classes fetched and stored
- Fuzzy DB search — `pg_trgm` similarity search on brand/generic names

---

## Open Items

### 1. `/health` Endpoint
The CI/CD health check (Stage 3 of CD) hits `/health` after deployment. This endpoint has not been added to `backend/app/main.py` yet. Simple implementation:
```python
@app.get("/health")
def health():
    return {"status": "ok"}
```

### 2. CI/CD Workflow Files
`.github/workflows/ci.yml` and `.github/workflows/deploy.yml` are empty placeholders. The CI/CD architecture is documented in [cicd.md](cicd.md) — the workflows need to be written to match it.

### 3. `infra/docker-compose.prod.yml`
Empty placeholder. Needs to define the full production stack: `backend`, `worker`, `postgres`, `rabbitmq` with production-appropriate settings (no exposed management ports, proper restart policies, secrets via env).

### 4. Class-Level Evidence Extraction Quality
The `_extract_class_evidence` function has a known limitation with long FDA labels. See [class_extraction_issue.md](class_extraction_issue.md) for details and proposed fixes.

### 5. Drug Data Refresh
The current write strategy (`ON CONFLICT DO NOTHING`) means FDA labels and drug class data fetched at first resolution are never updated. A background refresh job would re-fetch stale records when the FDA label data changes.

### 6. Pagination on `/drugs`
The `/drugs` endpoint returns all entries in one response. Currently fine (~65–90 drugs), but needs pagination if the dataset grows significantly.
