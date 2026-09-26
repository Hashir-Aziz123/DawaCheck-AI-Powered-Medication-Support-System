/**
 * Typed API client — all communication with the FastAPI backend lives here.
 *
 * Backend base URL is read from NEXT_PUBLIC_API_BASE_URL (set in .env.local).
 * Never hardcode the URL in page or component files.
 *
 * Endpoints covered:
 *   POST /resolve          — drug name resolution with autocomplete
 *   POST /check            — create an interaction check job
 *   GET  /check/{job_id}   — poll job status (WebSocket fallback)
 *   WS   /ws/jobs/{job_id} — live job status via WebSocket
 *   GET  /drugs            — list all drugs in the dataset (Browse page)
 */

// ─────────────────────────────────────────────────────────────────────────────
// Base URL
// ─────────────────────────────────────────────────────────────────────────────

const BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ??
  "http://localhost:8000";

const WS_BASE_URL = BASE_URL.replace(/^http/, "ws");

// ─────────────────────────────────────────────────────────────────────────────
// Shared types (mirror backend Pydantic schemas)
// ─────────────────────────────────────────────────────────────────────────────

export interface ResolvedDrugInfo {
  display_name: string;
  dosage_form: string | null;
  /** Opaque integer DB primary key — returned by the backend so the frontend
   *  can pass it to POST /check without a separate lookup. Null when the drug
   *  was resolved via a live API call and has not yet been persisted. */
  drug_id: number | null;
}

export type ResolveStatus = "resolved" | "ambiguous" | "not_found";

export interface ResolveResponse {
  status: ResolveStatus;
  /** Populated when status === "resolved" */
  drug?: ResolvedDrugInfo;
  /** Populated when status === "ambiguous" */
  candidates?: ResolvedDrugInfo[];
  /** Convenience top-level alias for drug.drug_id when status === "resolved" */
  drug_id?: number | null;
}


export interface CheckAcceptedResponse {
  job_id: string;
  status: "queued";
}

export type JobStatus = "queued" | "processing" | "done" | "failed";
export type FinalStatus = "interaction_found" | "none_found" | "unverifiable";

export interface InteractionResult {
  final_status: FinalStatus;
  drug_a: string | null;
  drug_b: string | null;
  interaction_claim: string | null;
  citation_text: string | null;
  is_grounded: boolean | null;
  groundedness_reasoning: string | null;
}

export interface CheckStatusResponse {
  job_id: string;
  status: JobStatus;
  result: InteractionResult | null;
  error_message: string | null;
}

export interface DrugListEntry {
  brand_name: string;
  generic_names: string[];
  dosage_form: string | null;
}

export interface DrugListResponse {
  total: number;
  drugs: DrugListEntry[];
}

// ─────────────────────────────────────────────────────────────────────────────
// API Error class
// ─────────────────────────────────────────────────────────────────────────────

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly detail?: string
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function handleResponse<T>(res: Response): Promise<T> {
  if (res.ok) {
    return res.json() as Promise<T>;
  }

  let detail: string | undefined;
  try {
    const body = await res.json();
    detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch {
    detail = undefined;
  }

  throw new ApiError(
    res.status,
    detail ?? `Request failed with status ${res.status}`,
    detail
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// POST /resolve
// ─────────────────────────────────────────────────────────────────────────────

export async function resolveDrug(query: string): Promise<ResolveResponse> {
  const res = await fetch(`${BASE_URL}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  return handleResponse<ResolveResponse>(res);
}

// ─────────────────────────────────────────────────────────────────────────────
// POST /check
// ─────────────────────────────────────────────────────────────────────────────

export async function createCheckJob(
  drugAId: number,
  drugBId: number
): Promise<CheckAcceptedResponse> {
  const res = await fetch(`${BASE_URL}/check`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ drug_a_id: drugAId, drug_b_id: drugBId }),
  });
  return handleResponse<CheckAcceptedResponse>(res);
}

// ─────────────────────────────────────────────────────────────────────────────
// GET /check/{job_id}
// ─────────────────────────────────────────────────────────────────────────────

export async function getCheckStatus(jobId: string): Promise<CheckStatusResponse> {
  const res = await fetch(`${BASE_URL}/check/${jobId}`);
  return handleResponse<CheckStatusResponse>(res);
}

// ─────────────────────────────────────────────────────────────────────────────
// WS /ws/jobs/{job_id}
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Open a WebSocket to /ws/jobs/{job_id} and call `onMessage` with each
 * parsed message. Calls `onError` if the connection fails.
 *
 * Returns a cleanup function — call it to close the socket early.
 */
export function subscribeJobWs(
  jobId: string,
  onMessage: (data: CheckStatusResponse | { error: string; message?: string }) => void,
  onError: (err: Event) => void
): () => void {
  const url = `${WS_BASE_URL}/ws/jobs/${jobId}`;
  const ws = new WebSocket(url);

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data as string);
      onMessage(data);
    } catch {
      // ignore malformed frames
    }
  };

  ws.onerror = onError;

  return () => {
    if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
      ws.close();
    }
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// GET /drugs
// ─────────────────────────────────────────────────────────────────────────────

export async function listDrugs(): Promise<DrugListResponse> {
  const res = await fetch(`${BASE_URL}/drugs`);
  return handleResponse<DrugListResponse>(res);
}


