# Drug Resolution Pipeline

The resolution pipeline converts a raw drug name (brand or generic) into a structured record with ingredients, RxCUI identifiers, and FDA safety text. It runs on every `/resolve` request when the DB has no cached result.

## Entry Points

| Function | File | Use case |
|----------|------|----------|
| `build_drug_record(brand_name)` | `core/resolution.py` | Brand-name-first resolution |
| `build_generic_record(generic_name)` | `core/generic_resolution.py` | Generic-name-first resolution |
| `resolve_drug(query, db)` | `app/services/drug_resolution.py` | Full resolve with DB cache check (used by the API) |

---

## `resolve_drug` — The API-Facing Entry Point

`resolve_drug` is the function called by the `/resolve` route. It tries six resolution paths in order, returning as soon as one succeeds:

```
1. DB Brand Search       — exact case-insensitive match on drugs.brand_name
2. DB Generic Search     — exact match on drug_ingredients.generic_name
3. DB Brand Fuzzy        — pg_trgm similarity > 0.40 on drugs.search_name
4. DB Generic Fuzzy      — pg_trgm similarity > 0.40 on drug_ingredients.generic_name
5. Live Brand Fallback   — build_drug_record() via external APIs
6. Live Generic Fallback — build_generic_record() via external APIs
```

If a live result is found, it is persisted to the DB so subsequent queries hit the cache.

**Return statuses:** `found` | `ambiguous` | `not_found` | `error`

---

## Brand Resolution — `build_drug_record`

```
brand_name
    │
    ▼
DRAP client — resolve_brand()
    │  returns: matched drug name, DRAP reg no, parsed ingredients
    │
    ▼ for each ingredient:
RxNorm client — get_rxnorm_name()
    │  returns: rxcui, rxnorm_name
    │
    ▼
openFDA client — get_fda_label()
    │  returns: drug_interactions, warnings, boxed_warning
    │
    ▼
RxClass client — get_drug_classes()
    │  returns: list of pharmacological class names + sources
    │
    ▼
DrugResolution object (fully populated)
```

---

## Generic Resolution — `build_generic_record`

Same pipeline, but starts from a generic name instead of a brand name.

```
generic_name (e.g. "amoxicillin + clavulanic acid")
    │
    ▼
detect_combination() — splits on "+" to get components
    │
    ▼
DRAP search by generic name — search_drap(search_type="generic name")
    │  If no results: try alias fallbacks from GENERIC_ALIASES dict
    │
    ▼
select_representative_brand()
    │  Scores candidates on: composition completeness, registration status,
    │  preferred dosage forms (tablet > injection), name similarity
    │
    ▼
get_drug_detail() — fetch full DRAP record for chosen brand
    │
    ▼
verify_composition()
    │  Levenshtein similarity ≥ 0.80 between searched generic and parsed ingredients
    │  Fails → Status.COMPOSITION_MISMATCH (result still returned, flagged)
    │
    ▼
RxNorm → openFDA → (no RxClass in this path)
    │
    ▼
DrugResolution object
```

---

## External Clients

### DRAP Client (`core/clients/drap_client.py`)
- Queries the Drug Regulatory Authority of Pakistan registry.
- **Spelling retry**: If no exact match, computes Levenshtein distance against candidates. Accepts a match if similarity ≥ 0.65 (flagged as `SPELLING_RETRY_FOUND`) or ≥ 0.50 (flagged as `SPELLING_RETRY_LOW_CONFIDENCE`).
- Strips salt forms (e.g. "HYDROCHLORIDE", "EQ TO") from ingredient names before downstream processing.
- Parses composition strings (dot-separated or space-separated) into `{name, amount}` dicts.

### RxNorm Client (`core/clients/rxnorm_client.py`)
- Queries the NLM RxNorm API to get a canonical RxCUI for a generic name.
- Cascade: exact name → stripped salt form → approximate match.
- Returns `(rxcui, rxnorm_name, Status)`.

### openFDA Client (`core/clients/openfda_client.py`)
- Queries the openFDA drug label API using the RxNorm name.
- Extracts three text fields: `drug_interactions`, `warnings`, `boxed_warning`.
- Returns raw label JSON alongside extracted text (raw stored in `FdaLabel.raw_response` for future use).

### RxClass Client (`core/clients/rxclass_client.py`)
- Queries the NLM RxClass API using the RxCUI.
- Returns pharmacological class names and their sources (ATC, MEDRT-MOA, etc.).
- Stored in the `drug_classes` table as parallel JSON arrays.

---

## Resiliency

- **Rate limiting**: `core/rate_limit.py` — per-domain token buckets protect DRAP, RxNorm, and openFDA limits.
- **HTTP retries**: `core/http_session.py` — shared session with automatic retries on 429 and 5xx responses.
- **Persistence**: Successfully resolved live results are written back to Postgres, so future lookups are DB-only.

---

## `generate_search_name`

Used to produce a clean fuzzy-search key when persisting a drug to the DB. Strips dosage forms (tablet, capsule, syrup…), dose patterns (500mg, 10ml…), and trailing punctuation from the brand name.

Example: `"Panadol Tablet 500mg."` → `"panadol"`
