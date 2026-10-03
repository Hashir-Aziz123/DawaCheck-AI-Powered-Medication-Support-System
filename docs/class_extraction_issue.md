# Known Issue: Class-Level Evidence Extraction vs. Token Limits

## The Problem

The LangGraph pipeline identifies class-level interactions by extracting relevant sentences from a drug's full FDA label and appending them to the truncated FDA text block sent to the LLM. However, drugs with very long labels (e.g., Zoloft at ~34,000 chars) mention class-relevant terms (like "serotonin") hundreds of times in benign contexts. The current extraction pulls the first matching sentences up to a 1000-char cap — and the actual interaction warning often appears later in the text, past the cap.

**Concrete example:** Tramadol ↔ Sertraline (Zoloft)
- "serotonin" triggers extraction from Zoloft's label.
- The first ~1000 chars of matched sentences are mechanism-of-action text (benign).
- The actual warning — *"potentially life-threatening serotonin syndrome has been reported with tramadol"* — appears later and is excluded from the LLM prompt.
- Result: the `check_interaction` node finds no class-level evidence → `none_found`.

## Constraints

| Limit | Value | Reason |
|-------|-------|--------|
| Main FDA text cap | 2500 chars | Groq token limits (~8k TPM) |
| Class evidence cap | 1000 chars | Protects prompt size |

We cannot feed 34,000 chars to the LLM without hitting 429 errors or context length failures.

## Current Implementation

`_extract_class_evidence()` in `nodes.py`:
- Extracts significant words from pharmacological class names (strips generic terms like "inhibitors", "agents").
- Generates acronyms for multi-word class names (e.g., "SSRI").
- Applies naive stemming ("serotonin" → "seroton" to match "serotonergic").
- Returns matched sentences up to 1000 chars.

This handles many cases well but fails when the relevant warning sentence appears after many benign mentions of the same keyword.

## Potential Fixes

1. **Dual-keyword filtering (recommended):** Only include a sentence if it contains a class keyword (e.g., "serotonin") **AND** an interaction-risk keyword (e.g., "syndrome", "risk", "avoid", "do not use", "contraindicated"). This filters out pharmacological-mechanism sentences.

2. **Section targeting:** During FDA label parsing, only extract from `drug_interactions` and `boxed_warning` sections for the class evidence search (skip the `warnings` section, which tends to be long and noisy).

3. **Higher cap + better model:** If token limits improve, increase the 1000-char cap and let the LLM parse a larger extracted chunk.
