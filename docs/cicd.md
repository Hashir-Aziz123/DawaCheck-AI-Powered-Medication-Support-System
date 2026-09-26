# CI/CD Pipeline

This document describes the planned Continuous Integration and Continuous Deployment pipeline for the project. The pipeline is implemented via GitHub Actions using the two workflow files in `.github/workflows/`.

## Overview

The pipeline is split into two distinct workflows that run at different times:

| Workflow | File | Triggered by |
|---|---|---|
| **CI** — "Is the code healthy?" | `.github/workflows/ci.yml` | Every push and every Pull Request |
| **CD** — "Ship it." | `.github/workflows/deploy.yml` | Merges to `main` only (after CI passes) |

```
Push to any branch
        │
        ▼
  CI Pipeline
  ├─ Stage 1: Lint & type check
  ├─ Stage 2: Unit tests
  ├─ Stage 3: Integration tests (live Postgres + RabbitMQ containers)
  └─ Stage 4: Docker build smoke test

Merge to main (CI must pass first)
        │
        ▼
  CD Pipeline
  ├─ Stage 1: Build image → push to ghcr.io
  ├─ Stage 2: SSH into server → docker compose up
  └─ Stage 3: Health check (/health endpoint)
```

---

## CI Pipeline — `.github/workflows/ci.yml`

### Stage 1: Lint & Type Check

**Tools:** `ruff` (linting) + `pyright` (type checking, using the existing `backend/pyrightconfig.json`)

This is the fastest stage (~5–10 seconds). It runs first to fail loudly on obvious mistakes before wasting time running tests on broken code. Catches: unused imports, style violations, type mismatches, and undefined variables.

### Stage 2: Unit Tests

**Tool:** `pytest backend/tests/unit/`

Runs against the full unit test suite without requiring any external services. All network calls are mocked (as documented in [testing.md](testing.md)). This stage should complete in under 30 seconds.

### Stage 3: Integration Tests

**Tool:** `pytest backend/tests/integration/`

This stage spins up real **PostgreSQL 16** and **RabbitMQ 3** containers as GitHub Actions service containers alongside the test runner. The `infra/init.sql` initialization script is applied before the tests run, mirroring the local development setup.

This stage requires secrets (see [Secrets](#secrets) below) to configure the database connection and any external API keys used by integration fixtures.

### Stage 4: Docker Build Smoke Test

**Tool:** `docker build`

Builds the `backend/Dockerfile` to verify the production image compiles cleanly. The image is **not pushed** in this stage — that is the CD pipeline's responsibility. This catches issues like missing files, broken `pip install` steps, or invalid `COPY` directives before code ever reaches `main`.

---

## CD Pipeline — `.github/workflows/deploy.yml`

The CD pipeline only runs after a successful CI run on the `main` branch.

### Stage 1: Build & Push Docker Image

Builds the production backend image and pushes it to **GitHub Container Registry (`ghcr.io`)** — free and directly integrated with this repository. The image is tagged with the Git commit SHA (e.g., `ghcr.io/org/dawacheck-backend:a1b2c3d`) to ensure every deployed version is traceable back to an exact commit.

### Stage 2: Deploy to Server

GitHub Actions SSHs into the production server and runs:

```bash
docker compose -f infra/docker-compose.prod.yml pull
docker compose -f infra/docker-compose.prod.yml up -d
```

This pulls the newly pushed image and restarts only the affected containers, leaving Postgres and RabbitMQ data volumes untouched.

### Stage 3: Health Check

After a brief wait for containers to initialise, the workflow hits the backend's `/health` endpoint. If the endpoint does not return `200 OK`, the workflow is marked as failed and GitHub sends a notification. This endpoint needs to be added to `backend/app/main.py` as part of the implementation.

---

## Hosting — Oracle Cloud Always Free

The production server is an **Oracle Cloud Always Free** ARM VM (Ampere A1). This tier is permanently free (not a trial) and provides more than enough capacity for this project's services.

| Resource | Allocation |
|---|---|
| vCPUs | 4 (ARM Ampere A1) |
| RAM | 24 GB |
| Storage | 50 GB block volume |
| Cost | $0.00, indefinitely |

The full production stack (`backend`, `worker`, `postgres`, `rabbitmq`) runs on this single VM via `infra/docker-compose.prod.yml`. The deployment approach is identical to what you would use with any paid VPS — GitHub Actions SSHs in and runs Docker Compose.

> **Note:** Oracle Cloud requires a credit card at signup for identity verification only. You are never charged while staying within Always Free resource limits, which this project comfortably does.

---

## Secrets

The following secrets must be configured in **GitHub → Settings → Secrets and variables → Actions** before the pipelines will work:

| Secret Name | Used by | What it is |
|---|---|---|
| `POSTGRES_USER` | CI (integration tests), CD | Database username |
| `POSTGRES_PASSWORD` | CI (integration tests), CD | Database password |
| `POSTGRES_DB` | CI (integration tests), CD | Database name |
| `GROQ_API_KEY` | CI (integration tests) | LLM API key |
| `LANGSMITH_API_KEY` | CI (integration tests) | LangSmith tracing key |
| `GHCR_TOKEN` | CD | GitHub PAT with `write:packages` scope for pushing images |
| `DEPLOY_SSH_KEY` | CD | Private SSH key for accessing the Oracle VM |
| `DEPLOY_HOST` | CD | Oracle VM's public IP address |
| `DEPLOY_USER` | CD | SSH username on the server (e.g., `ubuntu`) |

---

## Branch Strategy

- **`main`** is the single deployable branch. CI must pass before any PR can be merged.
- All feature and bugfix work is done in short-lived branches (`feature/xyz`, `fix/abc`).
- Direct pushes to `main` should be disabled via GitHub branch protection rules.

---

## Files Created / Modified During Implementation

| File | Status | Notes |
|---|---|---|
| `.github/workflows/ci.yml` | To be written | Currently an empty placeholder |
| `.github/workflows/deploy.yml` | To be written | Currently an empty placeholder |
| `backend/Dockerfile` | To be written | Currently an empty placeholder |
| `infra/docker-compose.prod.yml` | To be written | Currently an empty placeholder |
| `backend/app/main.py` | To be modified | Add `/health` endpoint |

---

## Evals

The `eval/` pipeline (LLM evaluation scripts in `backend/scripts/run_eval.py`) is **not part of the automated CI/CD pipeline**. Evals are run manually by the developer as needed. See [pipeline.md](pipeline.md) for more on the data and evaluation pipeline.
