# Core Drug Resolution Pipelines

The core value of this repository is its ability to accurately identify drugs despite spelling variations, local brand conventions, and complex compositions. This logic is handled in `backend/core/`.

## 1. The Resolution Flow
The entry point for resolving any drug brand is `build_drug_record()` in `backend/core/resolution.py`. This orchestrator chains three distinct clients:

### A. DRAP Client (`core/clients/drap_client.py`)
- **Purpose**: Converts a Pakistani brand name into its generic composition.
- **Process**:
  - Searches the DRAP registry.
  - Applies a fuzzy spelling-retry mechanism (Levenshtein distance) if exact matches fail, allowing recovery from typos while maintaining strict confidence thresholds.
  - Prioritizes "active" and oral dosage forms over niche forms (like injections) when multiple candidates exist.
  - Parses the raw composition string into distinct ingredients and amounts.

### B. RxNorm Client (`core/clients/rxnorm_client.py`)
- **Purpose**: Normalizes generic names into universally recognized RxNorm Concept IDs (RxCUI).
- **Process**:
  - Queries the NLM RxNorm API.
  - Cleans the DRAP generic string, stripping unnecessary salt forms (e.g., "Hydrochloride") if initial queries fail, increasing match rates for complex active ingredients.

### C. openFDA Client (`core/clients/openfda_client.py`)
- **Purpose**: Retrieves critical interaction and safety data.
- **Process**:
  - Queries openFDA using the normalized RxNorm name.
  - Extracts `drug_interactions`, `warnings`, and `boxed_warning` texts from the FDA label data.

## 2. NEML Batch Resolution (`scripts/resolve_neml_dataset.py`)
The National Essential Medicines List (NEML) pipeline works in the **reverse** direction.
- **Input**: A generic name (e.g., "amoxicillin + clavulanic acid").
- **Process**:
  1. Searches DRAP by the *generic* name.
  2. Selects a representative brand.
  3. **Verification Step**: Ensures the chosen brand actually contains the requested generic components using full-string composition similarity (threshold `0.80`).
  4. Runs the chosen brand through the RxNorm and openFDA steps.
- **Output**: Generates a CSV for manual review, ensuring no verification failures are silently dropped.

## 3. Resiliency Features
- **Rate Limiting**: `core/rate_limit.py` protects against hitting API limits on DRAP, RxNorm, and openFDA.
- **Session Management**: `core/http_session.py` provides shared connections with automatic retries for transient HTTP errors (429s, 5xx).
