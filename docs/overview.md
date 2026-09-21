# MediAid LangGraph Overview

## Project Goals
MediAid is designed to resolve Pakistani drug names (brands and generics, notably from the National Essential Medicines List - NEML) to canonical medical concepts in order to extract robust drug interaction and warning label data. 

The system relies on external medical databases and standardizes disparate drug naming conventions through a multi-step resolution pipeline.

## High-Level Architecture
The project is split into a back-end pipeline engine and a (future) API/worker layer. 

1. **Resolution Pipeline**: The core engine that orchestrates queries between:
    - **DRAP (Drug Regulatory Authority of Pakistan)**: Resolves local brand names to generic compositions.
    - **RxNorm (NLM)**: Normalizes generic ingredients to a canonical RxCUI (concept ID).
    - **openFDA**: Fetches interaction text, warnings, and boxed warnings using the normalized RxNorm name.

2. **Data & Storage**: 
    - A PostgreSQL database stores the structured `Drug`, `DrugIngredient`, and `FdaLabel` data.
    - Local disk caches (`.cache/`) speed up batch processing and protect against rate limits.

3. **Batch Processing**:
    - Standalone scripts (e.g., `create_dataset.py`, `resolve_neml_dataset.py`) read raw inputs (like CSVs or JSON lists) and push them through the resolution pipeline, outputting validated datasets or storing them in the database.

4. **API & AI Workers (Pending)**:
    - A FastAPI layer will provide endpoints for UI interactions.
    - A LangGraph pipeline will manage asynchronous multi-agent evaluation or complex query resolution.

## Navigating the Documentation
For a deeper dive into the system, refer to the following documents:
- [Pipeline Architecture](pipeline.md): Deep dive into the DRAP -> RxNorm -> openFDA resolution chain.
- [Infrastructure & Data Models](infrastructure.md): Details on the database schema, Docker setup, and caching layer.
- [Pending Work](pending_work.md): A guide to the unimplemented modules and placeholders awaiting development.
