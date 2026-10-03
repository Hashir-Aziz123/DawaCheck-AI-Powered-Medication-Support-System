# CI/CD Pipeline

## Overview

Two GitHub Actions workflows:

| Workflow | File | Trigger |
|----------|------|---------|
| CI | `.github/workflows/ci.yml` | Every push and every pull request |
| CD | `.github/workflows/deploy.yml` | Merges to `main` only (after CI passes) |

---

## CI Pipeline

Both backend and frontend stages run in parallel. All must pass before a PR can merge.

### Backend

**Stage 1 — Lint & Type Check** (`ruff` + `pyright`)
Runs in ~5–10 seconds. Catches unused imports, style violations, and type errors before wasting time on tests.

**Stage 2 — Unit Tests** (`pytest tests/unit/`)
No external services. All HTTP calls are mocked. Should complete under 30 seconds.

**Stage 3 — Integration Tests** (`pytest tests/integration/`)
Spins up real PostgreSQL 16 and RabbitMQ 3 as GitHub Actions service containers. `infra/init.sql` is applied before tests run. Requires the secrets listed below.

**Stage 4 — Docker Build Smoke Test** (`docker build`)
Builds `backend/Dockerfile` to verify the image compiles. Not pushed — that's the CD pipeline's job.

### Frontend

**Stage 5 — Lint & Type Check** (`next lint` + `tsc --noEmit`)
Runs from `frontend/`. Catches JSX type errors, React hook violations, and ESLint rules.

**Stage 6 — Production Build Smoke Test** (`npm run build`)
Full Next.js production build. Catches page-level errors, missing build-time env vars, and broken dynamic imports. Output not deployed here.

---

## CD Pipeline

Runs only after a successful CI run on `main`.

### Backend

**Stage 1 — Build & Push Image**
Builds the backend Docker image and pushes to GitHub Container Registry (`ghcr.io`). Tagged with the Git commit SHA for full traceability.

**Stage 2 — Deploy to Server**
SSHs into the production VM and runs:
```bash
docker compose -f infra/docker-compose.prod.yml pull
docker compose -f infra/docker-compose.prod.yml up -d
```
Only affected containers restart; Postgres and RabbitMQ data volumes are untouched.

**Stage 3 — Health Check**
Hits the backend `/health` endpoint after a brief startup delay. Failure marks the workflow failed and sends a notification.

### Frontend

**Stage 4 — Deploy to Vercel** (`npx vercel --prod`)
Deploys `frontend/` to Vercel production. Blocks until Vercel confirms the deployment is live so Stage 5 has a real URL.

**Stage 5 — Smoke Test**
`curl` the deployed root URL. A `200 OK` confirms the Next.js app rendered. If it fails, the previous Vercel deployment stays live.

> Preview deployments for branches are handled automatically by Vercel's GitHub integration — no workflow config needed.

---

## Hosting

**Backend:** Oracle Cloud Always Free ARM VM (Ampere A1)

| Resource | Allocation |
|----------|------------|
| vCPUs | 4 (ARM) |
| RAM | 24 GB |
| Storage | 50 GB |
| Cost | $0 permanently |

The full backend stack (`backend`, `worker`, `postgres`, `rabbitmq`) runs on this VM via `infra/docker-compose.prod.yml`.

**Frontend:** Vercel Hobby tier (free for personal/open-source projects). Deployments are instant and independent of server state.

---

## Secrets

Configure in GitHub → Settings → Secrets and variables → Actions:

| Secret | Used by | Description |
|--------|---------|-------------|
| `POSTGRES_USER` | CI, CD | DB username |
| `POSTGRES_PASSWORD` | CI, CD | DB password |
| `POSTGRES_DB` | CI, CD | DB name |
| `GROQ_API_KEY` | CI (integration) | LLM API key |
| `LANGSMITH_API_KEY` | CI (integration) | LangSmith tracing key |
| `GHCR_TOKEN` | CD (backend) | GitHub PAT with `write:packages` scope |
| `DEPLOY_SSH_KEY` | CD (backend) | Private SSH key for Oracle VM |
| `DEPLOY_HOST` | CD (backend) | Oracle VM public IP |
| `DEPLOY_USER` | CD (backend) | SSH username (e.g. `ubuntu`) |
| `VERCEL_TOKEN` | CD (frontend) | Vercel personal access token |
| `VERCEL_ORG_ID` | CD (frontend) | From `.vercel/project.json` after `vercel link` |
| `VERCEL_PROJECT_ID` | CD (frontend) | From `.vercel/project.json` after `vercel link` |
| `NEXT_PUBLIC_API_URL` | CD (frontend) | Backend API base URL (injected at build time) |

---

## Branch Strategy

- **`main`** is the single deployable branch. CI must pass before any PR merges.
- Feature and bugfix work uses short-lived branches (`feature/xyz`, `fix/abc`).
- Direct pushes to `main` should be blocked via GitHub branch protection rules.

---

## Evals

The `eval/` pipeline (LLM evaluation scripts) is **not** part of automated CI/CD. Evals are run manually by developers as needed.
