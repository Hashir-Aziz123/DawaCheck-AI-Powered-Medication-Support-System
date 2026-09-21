# Infrastructure & Data Models

## Docker & PostgreSQL
The project relies on a PostgreSQL 16 database, orchestrated via Docker.
- **Setup**: Configured in `infra/docker-compose.yml`.
- **Initialization**: `infra/init.sql` handles the first-time setup of the database container.
- **Usage**: Run `docker-compose up -d` in the `infra/` folder to start the `drug_assistant_db` container on port 5432.

## Database Schema (`backend/core/db/models.py`)
The SQLAlchemy ORM models define the relational structure of the resolved drug data:

- **`Drug`**: Represents a specific medication (e.g., "Augmentin").
  - Stores `brand_name`, `dosage_form`, `company_name`, and the internal `drap_reg_no`.
- **`DrugIngredient`**: Represents the generic components of a `Drug`.
  - Links to a `Drug` via `drug_id`.
  - Stores the parsed `generic_name` and `dose`.
  - Holds the canonical `rxcui` and `rxnorm_name` if normalization succeeded.
- **`FdaLabel`**: Stores interaction and warning data.
  - Indexed by `rxcui` to prevent duplicate FDA lookups across different brands containing the same generic.
  - Stores extracted `drug_interactions`, `warnings`, `boxed_warning`, and the complete `raw_response` as a JSONB column for future-proofing.
- **`InteractionJob`**: Designed for asynchronous processing.
  - Tracks the status (`queued`, `processing`, `done`, `failed`) of evaluating interactions between two specific drugs (`drug_a_id`, `drug_b_id`).

## Caching Strategy (`backend/core/cache.py`)
To prevent repeatedly hitting external APIs during iterative development and batch processing:
- **`JsonCache`**: A disk-backed caching utility that stores responses in the `.cache/` directory.
- **Implementation**: The caching layer wraps the orchestrator (e.g., in `scripts/create_dataset.py`) rather than the core clients. This keeps the core clients stateless and forces the caller to dictate the caching strategy (Disk vs. Postgres).
