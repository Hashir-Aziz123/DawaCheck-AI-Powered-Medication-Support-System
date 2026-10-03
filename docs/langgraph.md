# LangGraph Interaction-Checking Pipeline

The LangGraph pipeline is the AI core of DawaCheck. Given two resolved drug dicts, it determines whether their FDA labels document an interaction — and verifies any claim it makes is actually supported by the source text.

**Entry point:** `run_interaction_check(drug_a: dict, drug_b: dict) → dict` in `backend/worker/langgraph_pipeline/graph.py`

---

## Graph Flow

```
fetch_context
    │
    ▼
check_interaction  (LLM call 1)
    │
    ▼
verify_groundedness  (LLM call 2)
    │
    ├── is_grounded=True  ──► format_grounded_answer  ──► END
    │
    └── is_grounded=False ──► format_unverifiable_answer ──► END
```

No retry loop. If verification fails, the result is marked `unverifiable` and the pipeline terminates. Silently retrying until grounding passes would undermine the purpose of verification.

---

## State — `InteractionState`

A `TypedDict` that flows through every node. Each node reads what it needs and returns a partial update.

| Field | Set by | Description |
|-------|--------|-------------|
| `drug_a`, `drug_b` | caller | Full resolved drug dicts (brand_name, dosage_form, ingredients[]) |
| `drug_a_fda_text`, `drug_b_fda_text` | `fetch_context` | Concatenated FDA label text (drug_interactions + warnings + boxed_warning), capped at 2500 chars |
| `drug_a_classes`, `drug_b_classes` | `fetch_context` | Deduplicated pharmacological class names from the DB |
| `interaction_claim` | `check_interaction` | Plain-language description of the interaction, or `None` |
| `citation_text` | `check_interaction` | Exact verbatim sentence copied from the FDA label |
| `match_type` | `check_interaction` | `"direct"` or `"class_level"` or `None` |
| `is_grounded` | `verify_groundedness` | Whether the citation was confirmed in the source text |
| `groundedness_reasoning` | `verify_groundedness` | Brief explanation of the grounding verdict |
| `final_status` | format nodes | `"interaction_found"` \| `"none_found"` \| `"unverifiable"` |
| `final_answer` | format nodes | The dict returned to the caller |

---

## Nodes

### 1. `fetch_context`
No LLM call. Prepares data for the LLM nodes:
- Calls `_collect_fda_text()` to concatenate drug_interactions, warnings, and boxed_warning from all ingredients. Truncates at 2500 chars with a notice appended.
- Calls `_collect_class_names()` to get pharmacological classes from the DB for each drug.
- **Class-level evidence extraction**: Searches each drug's *full* (uncapped) FDA text for sentences mentioning terms from the *other* drug's pharmacological classes. Found sentences are appended to the FDA text block under `[Class-Level Evidence from Drug X full label]`. This surfaces class-level warnings buried past the 2500-char truncation point.

### 2. `check_interaction` (LLM call 1)
Uses `CHECK_INTERACTION_PROMPT` → structured output (`InteractionCheckResult`).

The LLM searches the FDA text for two types of matches, in order:
- **Direct match**: Drug A's label names Drug B (or vice versa).
- **Class-level match**: Drug A's label warns about a pharmacological class that Drug B belongs to.

The prompt design is **high recall** — lean toward reporting an interaction; the verification step handles false positives.

Returns: `interaction_claim`, `citation_text`, `match_type`. All three are `None` if no interaction is found.

### 3. `verify_groundedness` (LLM call 2)
Short-circuits immediately (no LLM call) if `interaction_claim` is `None`.

Otherwise, uses `VERIFY_GROUNDEDNESS_PROMPT` → structured output (`GroundednessCheckResult`).

The LLM checks three conditions:
- **Presence (A)**: The citation appears verbatim (or near-verbatim) in the combined FDA source text.
- **Support (B)**: The citation describes an interaction *effect*, not just a list of drug names.
- **Class attribution (C)**: For `class_level` matches only — the pharmacological class attributed in the claim is correct per the class lists in state.

All applicable conditions must pass for `is_grounded=True`.

### 4. `format_grounded_answer`
Assembles `final_answer` with `final_status="interaction_found"` and `is_grounded=True`.

### 5. `format_unverifiable_answer`
Distinguishes two cases:
- `"none_found"`: `check_interaction` returned no claim — FDA text doesn't mention an interaction.
- `"unverifiable"`: `check_interaction` made a claim but `verify_groundedness` couldn't confirm it.

---

## LLM Setup

| Setting | Value |
|---------|-------|
| Model | `openai/gpt-oss-120b` via Groq |
| Temperature | `0.0` for both calls |
| Output parsing | Structured output via `json_mode` (Pydantic models) |

Temperature 0.0 is deliberate — the same drug pair should always produce the same verification verdict. Response diversity is not a goal here.

---

## Prompts

Both prompts are in `backend/worker/langgraph_pipeline/prompts.py` and are versioned independently from the graph code.

**`CHECK_INTERACTION_PROMPT`** — pharmacovigilance assistant role. Receives: drug names, FDA texts, and known pharmacological classes for each drug. Returns JSON matching `InteractionCheckResult`.

**`VERIFY_GROUNDEDNESS_PROMPT`** — citation verification assistant role. Receives: interaction claim, citation text, combined FDA source text, match type, and class lists. Returns JSON matching `GroundednessCheckResult`.

Key design choice: the check prompt explicitly forbids using outside knowledge. All conclusions must trace to the provided FDA text.

---

## Output Shape

```python
{
    "final_status": "interaction_found" | "none_found" | "unverifiable",
    "drug_a": "Brand Name A",
    "drug_b": "Brand Name B",
    "interaction_claim": str | None,   # plain-language description
    "citation_text": str | None,       # verbatim FDA quote
    "match_type": "direct" | "class_level" | None,
    "is_grounded": bool | None,
    "groundedness_reasoning": str | None
}
```

---

## Tracing

When `LANGCHAIN_TRACING_V2=true` is set, every pipeline run is recorded in LangSmith. The run is named `"interaction_check | DrugA ↔ DrugB"` with `drug_a` and `drug_b` metadata, so the drug pair is visible in the LangSmith UI without opening individual spans.

---

## Worker Integration

The pipeline is invoked by `backend/worker/consumer.py`:

```
RabbitMQ message {job_id, drug_a_id, drug_b_id}
    │
    ▼
fetch_drug_data(drug_a_id), fetch_drug_data(drug_b_id)
    │  Loads drug + ingredients + FDA labels + drug_classes from DB
    │
    ▼
asyncio.to_thread(run_interaction_check, drug_a, drug_b)
    │  Runs synchronous LangGraph pipeline off the async event loop
    │
    ▼
update_job_status(job_id, status="done", result=result)
    │  Also issues: SELECT pg_notify('interaction_jobs_channel', job_id)
    │
    ▼
FastAPI LISTEN task receives NOTIFY → fires asyncio.Event
    │
    ▼
WS handler wakes, re-reads job from DB, sends result to client
```
