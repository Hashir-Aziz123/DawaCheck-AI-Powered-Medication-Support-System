# DawaCheck Frontend

Next.js 15 (App Router) frontend for the DawaCheck drug interaction decision-support system.

## Stack

- **Framework**: Next.js 15 with App Router and TypeScript
- **Styling**: Tailwind CSS with a centralized design token system
- **Font**: Inter via `next/font/google`
- **API**: Typed client in `lib/api/client.ts` — wraps all FastAPI backend endpoints

## Pages

| Route | Description |
|---|---|
| `/` | Landing page — what it is, who it's for, disclaimer |
| `/check` | Drug interaction checker — the core interactive page |
| `/browse` | Searchable list of all drugs in the dataset |
| `/how-it-works` | Plain-language explanation of the resolution pipeline |

## Setup

### Prerequisites

- Node.js 18+ and npm
- The FastAPI backend running on `http://localhost:8000`

### Install and run

```bash
cd frontend
npm install
npm run dev
```

The dev server starts at `http://localhost:3000`.

### Environment variables

The backend URL is configured via `.env.local`:

```
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

This file is already created. Change the URL if your backend runs on a different port.

## Design system

The design token set lives in two places that stay in sync:

- **`tailwind.config.ts`** — Tailwind palette, typography, shadows, border radius
- **`lib/tokens.ts`** — TypeScript-typed constants for programmatic use
- **`app/globals.css`** — CSS custom properties (CSS vars) and shared component classes

Accent color: `teal-500` (#14b8a6). Neutral base: slate-50 background, slate-800 text.
Status colors: amber (interaction found), slate (none found / unverifiable).

## Backend changes made in this mission

Two additions to the backend were needed to support the frontend:

1. **`GET /drugs`** — new read-only endpoint returning all drugs (brand name, generic names, dosage form) for the Browse page. See `backend/app/routes/drugs.py`.

2. **`drug_id` added to `/resolve` response** — the Check page needs a numeric drug ID to call `POST /check`. The resolve response now includes `drug_id` (the database primary key) in both the `drug` object and as a top-level field. This is an opaque integer — not a DRAP registration number. See `backend/app/schemas/resolve.py` and `backend/app/routes/resolve.py`.

3. **CORS middleware** — added to `backend/app/main.py` to allow `localhost:3000` requests during development.

## API client

All backend communication is centralized in `lib/api/client.ts`:

- `resolveDrug(query)` — POST /resolve
- `createCheckJob(drugAId, drugBId)` — POST /check
- `getCheckStatus(jobId)` — GET /check/{job_id}
- `subscribeJobWs(jobId, onMessage, onError)` — WS /ws/jobs/{job_id}
- `listDrugs()` — GET /drugs

The WebSocket helper returns a cleanup function. The Check page uses it with an HTTP polling fallback.
