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
  ├─ Backend
  │   ├─ Stage 1: Lint & type check  (ruff + pyright)
  │   ├─ Stage 2: Unit tests
  │   ├─ Stage 3: Integration tests  (live Postgres + RabbitMQ)
  │   └─ Stage 4: Docker build smoke test
  └─ Frontend
      ├─ Stage 5: ESLint + TypeScript type check
      └─ Stage 6: Next.js production build smoke test

Merge to main (CI must pass first)
        │
        ▼
  CD Pipeline
  ├─ Backend
  │   ├─ Stage 1: Build image → push to ghcr.io
  │   ├─ Stage 2: SSH into server → docker compose up
  │   └─ Stage 3: Health check (/health endpoint)
  └─ Frontend
      ├─ Stage 4: Deploy to Vercel (production)
      └─ Stage 5: Smoke-test the deployed URL
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

## Frontend CI — `.github/workflows/ci.yml` (continued)

The frontend stages run **in parallel** with the backend stages. Both must pass before a PR can be merged.

### Stage 5: ESLint + TypeScript Type Check

**Tools:** `next lint` (wraps ESLint) + `tsc --noEmit`

Runs from the `frontend/` directory. Catches JSX/TSX type errors, React hook violations, import issues, and any lint rules configured in `eslint.config.mjs`. This stage has no external dependencies and completes in under 30 seconds.

### Stage 6: Next.js Production Build Smoke Test

**Tool:** `npm run build` (`next build`)

Runs a full Next.js production build inside the workflow runner. This catches page-level errors, missing environment variables referenced at build time, broken dynamic imports, and invalid Tailwind CSS purge paths that only surface during compilation. The resulting `.next/` output is **not deployed** here — deployment is handled by the CD pipeline.

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

## Frontend CD — `.github/workflows/deploy.yml` (continued)

The frontend is deployed to **Vercel** — the natural fit for Next.js applications. Vercel handles incremental static regeneration, edge caching, and automatic preview URLs for every branch.

### Stage 4: Deploy to Vercel (Production)

**Tool:** Vercel CLI (`npx vercel --prod`)

The workflow uses the official Vercel CLI with the secrets `VERCEL_TOKEN`, `VERCEL_ORG_ID`, and `VERCEL_PROJECT_ID` to deploy the `frontend/` directory directly to the linked Vercel project. The deployment targets the **production** environment, making the new build live at the project's primary domain.

Key CLI flags used:
- `--prod` — promotes the deployment to production immediately.
- `--cwd frontend/` — scopes the CLI to the `frontend/` directory.
- `--no-wait` is **not** used — the step blocks until Vercel confirms the deployment is live so that Stage 5 has a real URL to test.

### Stage 5: Frontend Smoke Test

After the Vercel CLI outputs the canonical deployment URL, the workflow performs a `curl` health check against the root path (`/`). A `200 OK` response confirms the Next.js app rendered successfully. If the check fails, GitHub marks the run as failed and sends a notification, and the previous Vercel production deployment remains live (Vercel does not automatically roll back, but the old deployment is still accessible via its immutable URL).

> **Note on Preview Deployments:** Vercel automatically creates a preview deployment for every branch push via its own GitHub integration. The CD pipeline above handles only the final *production* promotion on merge to `main`. You do not need to configure the preview behaviour in the workflow file.

---

## Hosting — Oracle Cloud Always Free

The production server is an **Oracle Cloud Always Free** ARM VM (Ampere A1). This tier is permanently free (not a trial) and provides more than enough capacity for this project's services.

| Resource | Allocation |
|---|---|
| vCPUs | 4 (ARM Ampere A1) |
| RAM | 24 GB |
| Storage | 50 GB block volume |
| Cost | $0.00, indefinitely |

The full production backend stack (`backend`, `worker`, `postgres`, `rabbitmq`) runs on this single VM via `infra/docker-compose.prod.yml`. The deployment approach is identical to what you would use with any paid VPS — GitHub Actions SSHs in and runs Docker Compose.

The **frontend** is hosted separately on **Vercel's free Hobby tier** — permanently free for personal/open-source projects. This separation of concerns means frontend deployments are instant and independent of any server state.

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
| `GHCR_TOKEN` | CD (backend) | GitHub PAT with `write:packages` scope for pushing images |
| `DEPLOY_SSH_KEY` | CD (backend) | Private SSH key for accessing the Oracle VM |
| `DEPLOY_HOST` | CD (backend) | Oracle VM's public IP address |
| `DEPLOY_USER` | CD (backend) | SSH username on the server (e.g., `ubuntu`) |
| `VERCEL_TOKEN` | CD (frontend) | Vercel personal access token — create at vercel.com/account/tokens |
| `VERCEL_ORG_ID` | CD (frontend) | Vercel team/personal account ID — from `.vercel/project.json` after `vercel link` |
| `VERCEL_PROJECT_ID` | CD (frontend) | Vercel project ID — from `.vercel/project.json` after `vercel link` |
| `NEXT_PUBLIC_API_URL` | CD (frontend) | Base URL of the backend API, injected as a Next.js build-time env var |

---

## Branch Strategy

- **`main`** is the single deployable branch. CI must pass before any PR can be merged.
- All feature and bugfix work is done in short-lived branches (`feature/xyz`, `fix/abc`).
- Direct pushes to `main` should be disabled via GitHub branch protection rules.

---

## Files Created / Modified During Implementation

| File | Status | Notes |
|---|---|---|
| `.github/workflows/ci.yml` | To be written | Currently an empty placeholder — must cover both backend and frontend stages |
| `.github/workflows/deploy.yml` | To be written | Currently an empty placeholder — must cover backend (Docker/SSH) and frontend (Vercel) |
| `backend/Dockerfile` | To be written | Currently an empty placeholder |
| `infra/docker-compose.prod.yml` | To be written | Currently an empty placeholder |
| `backend/app/main.py` | To be modified | Add `/health` endpoint |
| `frontend/Dockerfile` | Optional | Only needed if the frontend is ever containerised for a self-hosted setup; not required for the Vercel deployment path |

---

## Evals

The `eval/` pipeline (LLM evaluation scripts in `backend/scripts/run_eval.py`) is **not part of the automated CI/CD pipeline**. Evals are run manually by the developer as needed. See [pipeline.md](pipeline.md) for more on the data and evaluation pipeline.
