# Local Drug Interaction & Brand Resolution Assistant

## Project Overview

### Purpose
This project is a **pharmacist-facing decision-support tool** that answers a specific, narrow question: *"Does taking Drug A and Drug B together carry a known interaction risk?"* — where the pharmacist enters drugs using **local Pakistani brand names** rather than generic/chemical names.

It exists to close a documented gap: research into current practice in Pakistan shows drug-drug interaction checking is largely manual/memory-based, and where checks do happen they use general-purpose international tools (drugs.com, Medscape, Micromedex) that have no awareness of local brand names — meaning a pharmacist has to already know the generic name before they can check anything. This tool is the missing translation layer plus a citation-grounded interaction check on top of it.

### Explicit scope and audience
- **Built for licensed pharmacists / clinical staff, not patients.** This distinction is foundational to the project's safety design, not a legal footnote.
- **Decision support, not a diagnostic or prescribing tool.** The system never states a drug combination is "safe." It either cites a specific, sourced interaction warning, or explicitly states no interaction was found in the sources checked.
- **Portfolio/demonstration project.** Not intended for production clinical deployment. Framed honestly throughout as a prototype demonstrating architecture, grounding, and evaluation methodology for an AI engineering portfolio — not a claim of clinical readiness.

### Core design philosophy
1. **Recall over precision on flagging.** The system is tuned to over-flag potential concerns for human review rather than confidently clear a combination. A false negative (missing a real interaction) is the dangerous failure mode; a false positive (over-caution) is merely annoying.
2. **Never infer beyond retrieved sources.** Every interaction claim must be traceable to an exact sentence in a real source document. If nothing relevant is found, the system says so explicitly rather than reasoning from the LLM's general training knowledge.
3. **Refusal is a first-class outcome, not a failure state.** "Unverifiable" / "no interaction found in available sources" are valid, expected answers — not edge cases to be engineered away.

---

## The Core Technical Problem

Local brand names (what a pharmacist actually sees on a box — "Panadol," "Augmentin," "Brufen") don't map cleanly onto any interaction database, all of which are organized by generic/chemical name. Worse, generic naming itself is inconsistent across regions: Pakistan (like most of the world) uses **INN (International Nonproprietary Names)** — e.g. "Paracetamol," "Acetylsalicylic Acid" — while US-sourced data (including openFDA) uses **USAN (United States Adopted Names)** — e.g. "Acetaminophen," "Aspirin." A naive pipeline that skips this normalization step silently fails on some of the most common drugs in the world.

The project's real technical contribution is the three-stage resolution chain that solves this, described below.

---

## Architecture

### High-level data flow

```
Pharmacist enters brand name(s)
        ↓
[Resolution] Brand name → generic ingredient(s)          (DRAP, internal only)
        ↓
[Normalization] Generic name → standardized name          (RxNorm)
        ↓
[Retrieval] Standardized name → clinical label text       (openFDA)
        ↓
[Reasoning] LLM checks both drugs' label text for mutual mentions
        ↓
[Verification] Independent check: is the claim actually grounded in the source text?
        ↓
[Response] Cited finding, or explicit "not found" — never an unverified claim
```

### Layer breakdown

#### 1. Data Layer (PostgreSQL)
A curated dataset of common Pakistani drug brands, pre-resolved through the full chain (DRAP → RxNorm → openFDA) as a one-time batch process, used as the fast path for common queries. Falls back to live resolution for brands not yet in the dataset. See **Database Schema** below for full table definitions.

**Why a pre-built dataset matters, not just a "nice to have":** live resolution requires three sequential network round-trips (DRAP, RxNorm, openFDA) per drug. For common drugs, doing this on every single query is slow, fragile (dependent on an undocumented third-party endpoint's uptime during a live demo), and unnecessarily repetitive traffic against a government system. The hybrid design — dataset-first, live-fallback — is a deliberate systems tradeoff, not a shortcut.

#### 2. Resolution Tools

**DRAP (Drug Regulatory Authority of Pakistan) resolver** — internal only, never exposed to the end user.
- Public but undocumented endpoints discovered via browser DevTools network inspection:
  - `GET https://eapp.dra.gov.pk/productView.php?search={query}&_type=brand%20name` — autocomplete-style search, returns `{"results": [{"id": reg_no, "text": display_name}, ...]}` (note: response has a UTF-8 BOM that must be stripped before JSON parsing)
  - `POST https://eapp.dra.gov.pk/productView.php` with form body `webRegNo={reg_no}` — returns an HTML fragment with full product details (composition, company, registration status, etc.), parsed with BeautifulSoup
- **Important constraint:** DRAP's own disclaimer states its data is a provisional list not intended as a reference for research, citation, or statistical analysis. Accordingly: DRAP is used *only* to resolve brand → generic name (an entity-resolution task, not a claim about drug safety); DRAP is never cited as a source in any user-facing answer; DRAP is never named anywhere in the UI, API responses, or "how was this determined" explanations shown to users; live DRAP calls are minimized via the dataset-first design and server-side caching (see below), never queried live per-keystroke for autocomplete.
- Composition parsing handles both dotted-leader format (`"Guaifenesin ...... 100 mg"`) and plain trailing-dose format (`"PARACETAMOL 500 mg"`), splitting each into structured `{name, amount}` pairs.

**RxNorm normalizer** — free, no-auth NIH API (`rxnav.nlm.nih.gov`).
- Two-step lookup: `GET /REST/rxcui.json?name={generic_name}&search=2` (name → RxCUI concept ID, normalized/fuzzy matching) then `GET /REST/rxcui/{rxcui}/property.json?propName=RxNorm%20Name` (RxCUI → canonical standardized name).
- Confirmed via testing that RxNorm correctly resolves INN names (e.g. "Paracetamol") to the same underlying concept as their USAN equivalent (e.g. "Acetaminophen" — RxCUI 161), making it a reliable crosswalk without hand-maintaining a name-mapping dictionary.
- This fully replaced an earlier manual `INN_TO_USAN` dictionary approach, which was deliberately dropped once RxNorm was confirmed to handle this generally.

**openFDA label retriever** — free, no-auth FDA API.
- `GET https://api.fda.gov/drug/label.json?search=openfda.generic_name:"{normalized_name}"&limit=1`
- Returns manufacturer label data including `drug_interactions`, `warnings`, and `boxed_warning` fields — free-text prose, not a structured interaction database. This is the reason an LLM reasoning step is necessary rather than a simple database join: determining whether Drug A's label discusses Drug B requires reading and reasoning over unstructured text.
- Not every label has every field populated; absence is handled explicitly (`"(not present in this label)"`) rather than treated as an error.

#### 3. Orchestration Layer (LangGraph)

The interaction-checking logic is modeled as an explicit state graph rather than a freeform agent loop, because the workflow has real structural requirements (safety gates) that should be enforced by graph structure, not left to LLM discretion within a single prompt.

**Shared state:**
```python
class InteractionState(TypedDict):
    drug_a: dict          # {rxcui, generic_name, fda_label_text}
    drug_b: dict
    interaction_claim: str | None
    citation_text: str | None
    is_grounded: bool | None
    final_status: str      # "interaction_found" | "none_found" | "unverifiable"
    final_answer: dict | None
```

**Nodes:**
1. `fetch_context` — loads both drugs' resolved label text into state (data already resolved prior to graph invocation). Kept as an explicit node for trace visibility.
2. `check_interaction` (LLM call) — reads both drugs' `drug_interactions`/`warnings` text, determines if either mentions the other drug or its pharmacological class, drafts a claim with an exact quoted source sentence, or explicitly outputs "no interaction found" rather than reasoning from general knowledge.
3. `verify_groundedness` (LLM call, deliberately independent from node 2) — checks whether the claim and citation from node 2 are actually present in the source text. Run as a separate, narrower-scoped call rather than trusting the same call to self-verify, since a model is not a reliable judge of its own output.
4. **Conditional edge** — routes to `format_grounded_answer` if `is_grounded == True`, else `format_unverifiable_answer`. This is the structural (not merely prompted) enforcement of the refuse-rather-than-guess design principle.
5a. `format_grounded_answer` — assembles the final cited response.
5b. `format_unverifiable_answer` — assembles the explicit "not found / could not verify" response, same schema, no fabricated citation.

**Deliberately not cyclic in v1** — a failed groundedness check routes to "unverifiable" and terminates; it does not loop back to retry `check_interaction` until something passes verification, which would silently defeat the purpose of the check.

#### 4. Evaluation Layer (LangSmith)
- Full tracing across every graph node — every run shows exactly what `check_interaction` claimed and what `verify_groundedness` decided, independently inspectable.
- Eval dataset built from the curated drug list: labeled known-interacting pairs (with expected severity language) and labeled known-non-interacting pairs, enabling measurement of both **recall** (catching real interactions) and **precision** (not hallucinating false ones).
- Custom evaluators:
  - **Groundedness evaluator** — checks whether a returned interaction claim is actually entailed by the cited source text.
  - **Refusal-correctness evaluator** — checks whether the system correctly abstains ("not found") on pairs known to have no documented interaction, and correctly does *not* abstain on pairs known to have one.
- Regression testing — re-run the eval set on every prompt/model change, compare deltas, rather than eyeballing a handful of examples.

#### 5. Async Backend Architecture

**Why async at all:** the interaction check involves multiple sequential LLM calls (reasoning + independent verification), which can take several seconds. Decoupling this from the request-handling web process avoids tying up web workers on slow-running LLM chains and provides natural retry/scaling semantics.

**Message broker: RabbitMQ.**
- Single queue (`interaction_checks`) — deliberately not split into multiple queues/exchanges, since there is currently only one job type. Additional queues would be added only if a genuinely distinct job category emerges.
- Message payload is minimal — just `job_id`, `drug_a_id`, `drug_b_id`. The worker re-fetches full drug context from Postgres using these IDs; the queue is never the source of truth for drug data.
- Published with `delivery_mode=2` (persistent) so queued jobs survive a broker restart.
- Worker uses `prefetch_count=1` so each worker instance processes one job at a time — predictable load distribution across multiple worker replicas, avoids one worker hoarding a burst of messages.
- Failed jobs are acked (not silently requeued forever) and recorded with `status='failed'` in Postgres — prevents a poison message from looping indefinitely against the LLM API. Bounded retry (e.g. a `retry_count` column, requeue up to N times) is a deliberate future addition, not RabbitMQ's default infinite-redelivery behavior.

**No Redis.** PostgreSQL is used for both the dataset and for job results/caching, rather than adding Redis as a second data store.
- **Rationale:** the project already requires Postgres for the dataset; adding Redis purely for job-result storage and caching would be additional operational surface area without a corresponding real need at this project's scale. Postgres provides adequate latency for polling/WebSocket-triggered reads (low single-digit ms, irrelevant at this request volume), better default durability than Redis (survives restarts without extra persistence config), and native `LISTEN`/`NOTIFY` pub/sub support, which covers the one Redis feature (push notification) this project actually needed.
- **Tradeoff acknowledged:** Redis would be faster for very high-throughput caching and has built-in TTL/expiry, neither of which this project's scale requires. This was a deliberate choice to avoid adding infrastructure the project doesn't need, not an oversight.
- Job completion is pushed to the frontend via a FastAPI WebSocket connection, triggered by a Postgres `LISTEN`/`NOTIFY` channel the worker fires into after writing a completed result — no polling loop needed on the happy path.

---

## Database Schema (PostgreSQL)

```sql
-- Curated, pre-resolved drug dataset
CREATE TABLE drugs (
    id              SERIAL PRIMARY KEY,
    brand_name      TEXT NOT NULL,
    drap_reg_no     TEXT,                                   -- internal only, never exposed via API
    dosage_form     TEXT,
    company_name    TEXT,
    resolved_at     TIMESTAMPTZ NOT NULL DEFAULT now(),      -- staleness tracking; cache invalidation policy TBD (e.g. re-resolve if older than 30 days)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_drugs_brand_name ON drugs USING gin (brand_name gin_trgm_ops);  -- fuzzy/prefix autocomplete, requires pg_trgm extension

-- One-to-many: a drug may have multiple active ingredients
CREATE TABLE drug_ingredients (
    id              SERIAL PRIMARY KEY,
    drug_id         INTEGER NOT NULL REFERENCES drugs(id) ON DELETE CASCADE,
    generic_name    TEXT NOT NULL,       -- as parsed from DRAP, e.g. "Guaifenesin"
    dose            TEXT,                -- "100 mg"
    rxcui           TEXT,                -- stable RxNorm concept ID — canonical join key
    rxnorm_name     TEXT                 -- human-readable standardized name, stored alongside rxcui for readability
);
CREATE INDEX idx_drug_ingredients_drug_id ON drug_ingredients(drug_id);
CREATE INDEX idx_drug_ingredients_rxcui ON drug_ingredients(rxcui);

-- Cached openFDA label data, keyed by RxCUI (not name string) for stability
CREATE TABLE fda_labels (
    id                  SERIAL PRIMARY KEY,
    rxcui               TEXT NOT NULL UNIQUE,
    rxnorm_name         TEXT NOT NULL,
    drug_interactions   TEXT,
    warnings            TEXT,
    boxed_warning       TEXT,
    raw_response        JSONB,           -- full openFDA payload, future-proofs against needing a field not originally extracted
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Async job tracking (replaces what a Redis store would hold)
CREATE TABLE interaction_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),  -- generated before queue publish, must be collision-safe without a DB round-trip
    drug_a_id       INTEGER REFERENCES drugs(id),
    drug_b_id       INTEGER REFERENCES drugs(id),
    status          TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'processing', 'done', 'failed')),
    result          JSONB,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);
```

**Key design decisions embedded in this schema:**
- `rxcui` (not name strings) is the canonical join key between ingredients and cached labels — RxCUIs are stable identifiers; name strings are fragile to casing/whitespace/minor revisions.
- `JSONB` for `raw_response` and `result` — Postgres-native semi-structured storage, queryable without a migration if a previously-unextracted field turns out to be needed later.
- `UUID` (not `SERIAL`) for `interaction_jobs.id` — must be generatable client-side (in FastAPI) before the RabbitMQ message is published, without a DB round-trip first.
- `resolved_at` on `drugs` exists specifically because DRAP data can change (new registrations, status updates) and an unmanaged cache-forever table is worse than no cache — staleness policy is an explicit, acknowledged open decision, not an oversight.

---

## API Layer (FastAPI)

### `POST /resolve`
Resolves a single drug name — dataset lookup first, DRAP fallback invisible to the caller, kept **synchronous** (deliberately not queued — DRAP fallback is expected to complete in well under a second, and adding async job machinery here would be over-engineering the wrong layer).

Request: `{ "query": str }`
Response:
```
{
  "status": "resolved" | "ambiguous" | "not_found",
  "drug": { "drug_id": int, "display_name": str, "dosage_form": str | null } | null,
  "candidates": [ ...same shape... ] | null   // populated only when ambiguous
}
```
- `ambiguous` → frontend shows a disambiguation picker using **product display names only** (e.g. "Panadol Tablet", "Panadol Extra") — no DRAP registration numbers or any indication of DRAP as a source are ever surfaced.

### `POST /check`
Enqueues an async interaction check. Requires both drugs already resolved via `/resolve`.

Request: `{ "drug_a_id": int, "drug_b_id": int }`
Response (HTTP 202 Accepted): `{ "job_id": UUID, "status": "queued" }`

### `GET /check/{job_id}`
Polling fallback (primary delivery is via WebSocket) — needed for a client that reconnects after missing the WebSocket push.

Response:
```
{
  "job_id": UUID,
  "status": "queued" | "processing" | "done" | "failed",
  "result": {
    "final_status": "interaction_found" | "none_found" | "unverifiable",
    "summary": str | null,
    "citation_text": str | null,
    "source": str | null        // "openFDA" — never "DRAP"
  } | null,
  "error_message": str | null
}
```

### `WS /ws/jobs/{job_id}`
Opened by the client immediately after receiving a `job_id` from `POST /check`. Server subscribes to that job's Postgres `LISTEN` channel, pushes the completed `CheckStatusResponse` the moment the worker marks the job done/failed, then closes.

### Suggested project structure
```
app/                        # FastAPI web process
  main.py
  routes/
    resolve.py               # POST /resolve
    check.py                 # POST /check, GET /check/{job_id}
    ws.py                    # WS /ws/jobs/{job_id}
  schemas/
    resolve.py
    check.py
  services/
    drug_resolution.py       # dataset lookup + DRAP fallback + RxNorm normalization
    queue_publisher.py       # RabbitMQ publish wrapper
  db/
    models.py                # SQLAlchemy models matching the schema above
    listeners.py              # Postgres LISTEN/NOTIFY wrapper

worker/                     # RabbitMQ consumer process, separate deployable service
  consumer.py
  langgraph/
    graph.py                  # StateGraph definition and wiring
    nodes.py                  # fetch_context, check_interaction, verify_groundedness, format_* nodes

frontend/                   # React app
  ...

scripts/
  build_dataset.py           # one-time/periodic batch job: runs the DRAP -> RxNorm -> openFDA
                              # chain over the curated brand list to populate the drugs/
                              # drug_ingredients/fda_labels tables
```
`app/` and `worker/` are separate top-level directories because they are separate deployable services (separate containers), both depending on a shared `db`/`core` package for models — this mirrors the actual deployment topology rather than pretending it's a single monolith with a queue bolted on.

---

## Technology Stack Summary

| Layer | Choice | Rationale (brief — see relevant section above for full reasoning) |
|---|---|---|
| Backend framework | FastAPI | Async-native, typed request/response schemas via Pydantic |
| Frontend | React | Standard SPA, autocomplete + disambiguation UI + WebSocket client |
| Database | PostgreSQL | Single data store for dataset, cache, and job results — no Redis (see async architecture section) |
| Message broker | RabbitMQ | Genuine async decoupling of LLM-heavy work from the request path |
| Orchestration | LangGraph | Explicit state machine — safety gates enforced structurally, not just via prompting |
| Agent framework | LangChain | Tool wrapping (DRAP/RxNorm/openFDA resolvers) |
| Evaluation/observability | LangSmith | Tracing, custom groundedness/refusal evaluators, regression testing |
| Brand resolution | DRAP (undocumented public endpoints) | Only available source for Pakistani drug registration/composition data; internal-only, never user-facing |
| Name normalization | RxNorm (NIH, free) | INN ↔ USAN crosswalk, confirmed via testing to handle this generally |
| Interaction/clinical data | openFDA (free) | Structured access to real manufacturer label text; free, no licensing friction (DrugBank considered but requires sales-issued API key / licensing agreement, not self-serve) |
| Deployment | CI/CD pipeline (to be detailed) | — |

---

## Known Limitations / Explicitly Deferred Decisions

These are documented deliberately, as honest scope boundaries rather than gaps to hide:

1. **Brand disambiguation at the DRAP layer takes the first search match by default**; full disambiguation is handled at the UI layer (`/resolve` returning `ambiguous` with candidates) rather than the backend guessing — this is implemented, but worth noting the backend resolver function itself has a "naive first match" fallback path that should never be hit once the UI flow is used correctly.
2. **No bounded retry logic yet on failed worker jobs** — currently fail-and-record rather than fail-and-requeue-with-limit. Planned as a `retry_count` column addition.
3. **Cache staleness policy for the `drugs` table is not yet enforced** — `resolved_at` is tracked, but no automatic re-resolution job exists yet.
4. **RxNorm coverage is not guaranteed exhaustive** — the normalizer falls back to using the original (unnormalized) name if RxNorm returns no match, meaning some rare/obscure ingredients may still fail to resolve against openFDA. No comprehensive fallback beyond this exists yet.
5. **Multi-drug (3+) interaction checking is not yet designed** — current architecture is pairwise (Drug A vs Drug B); real prescriptions often involve more.
6. **This is explicitly a prototype/portfolio project**, not a clinically validated or deployed tool. It should never be described, in documentation or in interviews, as production-ready clinical software.

---

## Project Narrative Summary (for reference when writing about this project)

This project demonstrates: (1) real-world messy data acquisition — reverse-engineering an undocumented government API via browser network inspection, handling inconsistent HTML/encoding issues (BOM stripping, non-standard whitespace); (2) a genuine entity-resolution problem — cross-referencing naming conventions (INN vs. USAN) across international data sources with no single authoritative crosswalk table, solved via RxNorm rather than a brittle hand-maintained dictionary; (3) LLM grounding and hallucination control as a structural (graph-level), not just prompted, property of the system, including an independently-run verification step; (4) a properly async backend architecture (RabbitMQ + Postgres, no unnecessary Redis dependency) with clear reasoning for each infrastructure choice rather than defaulting to a "standard" stack; and (5) an evaluation methodology (LangSmith custom evaluators for groundedness and refusal-correctness) built around the specific failure modes that matter in a high-stakes domain, rather than generic accuracy metrics.