# Issue Report: Class-Level Evidence Extraction vs. Token Limits

## The Core Problem
The LangGraph pipeline successfully identifies when two drugs share intersecting pharmacological classes (via the NLM RxClass API) and attempts to verify class-level interactions using their FDA labels. However, it fails on drugs with exceptionally long labels (e.g., Zoloft at ~34,000 characters) because the critical interaction warnings are hidden deep in the text, far past the pipeline's truncation limits.

## The API / Context Window Constraint
Because we are using Groq (which has strict token limits) and standard LLM context windows, we cannot feed 50,000+ characters of raw FDA text into the `check_interaction` prompt without triggering `429 Too Many Requests` or context length errors. 

To prevent API crashes, two limits were established:
1. **Global FDA Text Cap (`_MAX_FDA_TEXT_CHARS = 2500`)**: Cuts off the main label text at 2500 characters.
2. **Class Evidence Extraction Cap (`max_chars = 1000`)**: A lightweight RAG step that searches the *uncapped* text for sentences containing the other drug's class keywords, capping the extracted block at 1000 characters to protect the prompt size.

## The Catch-22 (The "Noisy Keyword" Problem)
When Drug A is Tramadol and Drug B is Zoloft (Sertraline):
- Tramadol's classes trigger a search for `"serotonin"` inside Zoloft's 34,000-character label.
- Zoloft's label mentions `"serotonin"` constantly in completely benign or unrelated contexts (e.g., mechanism of action, washout periods, platelet hemostasis).
- The extraction tool pulls the first 1000 characters of these irrelevant sentences and stops.
- The **actual** warning sentence ("...potentially life-threatening serotonin syndrome has been reported with... tramadol...") appears later in the text and is completely excluded from the LLM prompt.

We cannot simply increase the `1000` character extraction limit without risking API failures, but leaving it as-is starves the LLM of the evidence it needs to trigger a `class_level` match.

## Potential Solutions to Discuss
1. **Smarter, Dual-Keyword Extraction (Recommended)**: Update `_extract_class_evidence` to only append a sentence if it contains a class keyword (e.g., `"serotonin"`) **AND** an interaction risk keyword (e.g., `"syndrome"`, `"risk"`, `"avoid"`, `"interaction"`). This filters out the noisy pharmacological fluff.
2. **Section Targeting**: Modify the database parsing to only extract text from the `[drug_interactions]` and `[boxed_warning]` headers, ignoring the `[warnings]` header if it's too long.
3. **Upgrade the Model / API Tier**: If token limits are no longer a concern, we could remove the `1000` character cap and let the LLM parse the entire chunk of matched sentences.
