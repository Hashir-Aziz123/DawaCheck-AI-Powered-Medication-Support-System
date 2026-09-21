# Testing Documentation

The repository currently features a highly robust and fast test suite (98 passing tests) focusing on the core resolution logic and database integrity. The tests are split into **unit tests** (which use mocking to run instantly without network calls) and **integration tests** (which interact with the live PostgreSQL database).

## Unit Tests (`backend/tests/unit/`)

The unit test suite ensures that the complex string parsing, API fallbacks, and data extraction logic work flawlessly even when edge cases are encountered. All external HTTP requests (to DRAP, RxNorm, and openFDA) are mocked using `unittest.mock.patch`.

### 1. `test_composition_parser.py`
This is a pure-logic test suite that covers the `parse_composition()` and `preprocess_ingredient_name()` functions.
- **Separator Logic**: Verifies that both `..` (dot separators) and space separators correctly split raw composition strings into lists of generic ingredients and dosages.
- **Edge Cases**: Ensures empty strings, blank lines, and trailing punctuation (like stray commas) don't break the regex parser.

### 2. `test_drap_client.py`
Tests the DRAP client logic, focusing on fuzzy matching and data cleaning.
- **Salt Stripping**: Confirms that phrases like "EQ TO" or "TRIHYDRATE" are accurately stripped from generic names.
- **Spelling Similarity**: Verifies the calibrated thresholds for the Levenshtein distance fallback (e.g., confirming "Amryl" successfully matches "Amaryl" above the `0.65` confidence threshold).
- **Network Paths**: Mocks HTTP calls to test happy paths, network errors, and the `SPELLING_RETRY_FOUND` logic when an exact match isn't found.

### 3. `test_rxnorm_client.py`
Validates the canonical normalization pathways.
- Tests the cascading logic: Exact hit -> Stripped Name hit -> Approximate Match fallback.
- Ensures the proper `Status` enum is assigned based on which search pathway succeeded.

### 4. `test_openfda_client.py`
Validates label retrieval and text extraction.
- **HTTP Handling**: Mocks scenarios where openFDA returns a `404 Not Found`, empty JSON results, or `500 Server Error`.
- **Data Extraction**: Ensures `drug_interactions`, `warnings`, and `boxed_warning` are properly extracted from the deeply nested FDA label JSON dictionaries.

---

## Integration Tests (`backend/tests/integration/`)

The integration tests require the Docker PostgreSQL container to be running (`docker-compose up -d`). They ensure the backend can correctly talk to the database.

### 1. `test_db_connection.py`
- Runs a simple `SELECT 1` query to verify the SQLAlchemy async engine can reach the database.
- Specifically verifies that the `pg_trgm` extension is enabled (required for future fuzzy text search on brand names), ensuring `init.sql` ran successfully.

### 2. `test_models.py`
- Verifies that the table creation script (`create_tables.py`) successfully generated the four expected tables: `drugs`, `drug_ingredients`, `fda_labels`, and `interaction_jobs`.

---

## Empty / Pending Tests
You will notice some empty test files (e.g., `test_check_flow.py`, `test_resolve_endpoint.py`, `test_websocket.py`, and `test_langgraph_nodes.py`). As mentioned in the [Pending Work](pending_work.md) documentation, these are placeholders aligned with the unimplemented FastAPI and LangGraph components. They are ready to be filled out as you begin building those layers.
