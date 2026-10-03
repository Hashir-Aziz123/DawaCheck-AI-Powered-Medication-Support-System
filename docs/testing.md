# Testing

## Running Tests

```bash
cd backend

# All tests
pytest

# Unit tests only (no external services needed)
pytest tests/unit/

# Integration tests (requires Docker Postgres + RabbitMQ running)
pytest tests/integration/
```

---

## Unit Tests (`backend/tests/unit/`)

All external HTTP calls are mocked with `unittest.mock.patch`. Tests run in under 30 seconds with no network access.

### `test_composition_parser.py`
Tests `parse_composition()` and `preprocess_ingredient_name()` from the DRAP client.
- Dot-separator and space-separator parsing.
- Edge cases: empty strings, blank lines, trailing punctuation, stray commas.

### `test_drap_client.py`
Tests DRAP client logic — fuzzy matching and data cleaning.
- Salt stripping: "EQ TO", "TRIHYDRATE", "HYDROCHLORIDE" removed correctly.
- Levenshtein spelling retry: "Amryl" matches "Amaryl" above the 0.65 threshold.
- Network paths: happy path, `SPELLING_RETRY_FOUND`, API error handling.

### `test_rxnorm_client.py`
Tests the RxNorm normalization cascade.
- Exact hit → stripped name hit → approximate match fallback.
- Correct `Status` enum assigned per pathway.

### `test_openfda_client.py`
Tests FDA label retrieval and text extraction.
- 404, empty results, 500 server error scenarios.
- Correct extraction of `drug_interactions`, `warnings`, `boxed_warning` from nested JSON.

### `test_langgraph_nodes.py`
Tests LangGraph nodes with the LLM mocked.
- `fetch_context`: FDA text collection, truncation, class-level evidence extraction.
- `check_interaction`: structured output parsing, direct vs. class_level match_type.
- `verify_groundedness`: grounding conditions (presence, support, class attribution).
- `format_grounded_answer` / `format_unverifiable_answer`: output shapes and `none_found` vs. `unverifiable` distinction.

---

## Integration Tests (`backend/tests/integration/`)

Require the Docker containers from `infra/docker-compose.yml` to be running.

### `test_db_connection.py`
- Runs `SELECT 1` to verify the SQLAlchemy async engine can connect.
- Verifies `pg_trgm` extension is enabled (confirms `init.sql` ran).

### `test_models.py`
- Verifies all five expected tables exist: `drugs`, `drug_ingredients`, `fda_labels`, `drug_classes`, `interaction_jobs`.

### `test_resolve_endpoint.py`
- End-to-end tests against the `/resolve` route using a real DB.
- Tests DB hit paths (brand exact, generic exact, fuzzy) and `not_found` case.

### `test_check_flow.py`
- Tests the `/check` → RabbitMQ → worker → DB flow end-to-end.
- Requires RabbitMQ and a running worker process (or a mock consumer).

### `test_websocket.py`
- Tests the WebSocket endpoint: fast-path (job already done), slow-path (wait for NOTIFY), and timeout behavior.

---

## Mocking Patterns

The LangGraph nodes expose two factory functions (`_get_llm`, `_build_interaction_chain`, `_build_groundedness_chain`) specifically so they can be patched cleanly:

```python
# Mock the chain, not the LLM directly
with patch("worker.langgraph_pipeline.nodes._build_interaction_chain") as mock_chain:
    mock_chain.return_value.invoke.return_value = InteractionCheckResult(
        interaction_found=True,
        match_type="direct",
        claim="...",
        citation="..."
    )
    result = check_interaction(state)
```
