"use client";

import { useState, useCallback, useRef, useEffect } from "react";
import {
  resolveDrug,
  createCheckJob,
  subscribeJobWs,
  getCheckStatus,
  ApiError,
  type ResolvedDrugInfo,
  type ResolveResponse,
  type CheckStatusResponse,
  type InteractionResult,
  type FinalStatus,
  type JobStatus,
} from "@/lib/api/client";

// ─────────────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────────────

type DrugSlot = "a" | "b";

interface DrugState {
  query: string;
  status: "idle" | "loading" | "resolved" | "ambiguous" | "not_found" | "error";
  resolved?: ResolvedDrugInfo;
  candidates?: ResolvedDrugInfo[];
  errorMsg?: string;
}

const EMPTY_DRUG: DrugState = { query: "", status: "idle" };

type CheckPhase =
  | { type: "idle" }
  | { type: "submitting" }
  | { type: "waiting"; jobId: string; jobStatus: JobStatus }
  | { type: "done"; result: InteractionResult }
  | { type: "error"; message: string };

// ─────────────────────────────────────────────────────────────────────────────
// Debounce
// ─────────────────────────────────────────────────────────────────────────────

function useDebounce<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}

// ─────────────────────────────────────────────────────────────────────────────
// DrugInput
// ─────────────────────────────────────────────────────────────────────────────

function DrugInput({
  slot, state, onChange, onSelect, onClear, disabled,
}: {
  slot: DrugSlot;
  state: DrugState;
  onChange: (q: string) => void;
  onSelect: (c: ResolvedDrugInfo) => void;
  onClear: () => void;
  disabled?: boolean;
}) {
  const label = slot === "a" ? "Drug A" : "Drug B";
  const id = `drug-${slot}-input`;

  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="section-label">{label}</label>

      {state.status === "resolved" && state.resolved ? (
        <div className="flex items-center justify-between rounded-lg border border-teal-300 bg-teal-50 px-4 py-3">
          <span className="text-sm font-medium text-teal-800">{state.resolved.display_name}</span>
          <button type="button" onClick={onClear} disabled={disabled}
            className="ml-3 text-teal-400 hover:text-teal-600 transition-colors"
            aria-label={`Clear ${label}`}>
            <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z" />
            </svg>
          </button>
        </div>
      ) : (
        <>
          <div className="relative">
            <input id={id} type="text" className="input pr-10"
              placeholder="Type a drug name..."
              value={state.query}
              onChange={(e) => onChange(e.target.value)}
              disabled={disabled}
              autoComplete="off" spellCheck="false"
            />
            {state.status === "loading" && (
              <div className="absolute right-3 top-1/2 -translate-y-1/2">
                <span className="spinner h-4 w-4" />
              </div>
            )}
          </div>
          {state.status === "not_found" && (
            <p className="text-xs text-slate-400">No match found. Try a different spelling.</p>
          )}
          {state.status === "error" && (
            <p className="text-xs text-red-500">{state.errorMsg}</p>
          )}
          {state.status === "ambiguous" && state.candidates && (
            <div className="card mt-1 divide-y divide-slate-100 overflow-hidden">
              <p className="px-4 py-2 text-xs text-slate-400">Multiple matches found. Select one:</p>
              {state.candidates.map((c, i) => (
                <button key={i} type="button" onClick={() => onSelect(c)} disabled={disabled}
                  className="w-full px-4 py-2.5 text-left text-sm text-slate-700 hover:bg-teal-50 hover:text-teal-700 transition-colors">
                  {c.display_name}
                </button>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Status indicator
// ─────────────────────────────────────────────────────────────────────────────

const STATUS_LABELS: Record<JobStatus, string> = {
  queued: "Queued…",
  processing: "Checking interaction…",
  done: "Done",
  failed: "Failed",
};

// ─────────────────────────────────────────────────────────────────────────────
// Result panel
// ─────────────────────────────────────────────────────────────────────────────

const RESULT_CONFIG: Record<FinalStatus, { cls: string; label: string }> = {
  interaction_found: { cls: "status-banner status-banner--interaction", label: "Interaction found" },
  none_found:        { cls: "status-banner status-banner--none",        label: "No interaction found in available sources" },
  unverifiable:      { cls: "status-banner status-banner--unverifiable", label: "Could not verify: source text inconclusive" },
};

function ResultPanel({ result }: { result: InteractionResult }) {
  const [open, setOpen] = useState(false);
  const cfg = RESULT_CONFIG[result.final_status];

  return (
    <div className="animate-fade-in space-y-3">
      <div className={cfg.cls} role="status">{cfg.label}</div>

      {(result.drug_a || result.drug_b) && (
        <p className="text-sm text-slate-500">
          {[result.drug_a, result.drug_b].filter(Boolean).join(" × ")}
        </p>
      )}

      {result.interaction_claim && (
        <div className="card px-5 py-4">
          <p className="section-label mb-1.5">Claim</p>
          <p className="text-sm text-slate-700 leading-relaxed">{result.interaction_claim}</p>
        </div>
      )}

      {result.citation_text && (
        <div className="card overflow-hidden">
          <button type="button" onClick={() => setOpen(o => !o)}
            className="flex w-full items-center justify-between px-5 py-3 hover:bg-slate-50 transition-colors"
            aria-expanded={open} id="citation-toggle">
            <span className="section-label">Source: openFDA</span>
            <svg className={`h-4 w-4 text-slate-400 transition-transform ${open ? "rotate-180" : ""}`}
              viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path fillRule="evenodd" d="M5.22 8.22a.75.75 0 0 1 1.06 0L10 11.94l3.72-3.72a.75.75 0 1 1 1.06 1.06l-4.25 4.25a.75.75 0 0 1-1.06 0L5.22 9.28a.75.75 0 0 1 0-1.06Z" clipRule="evenodd" />
            </svg>
          </button>
          {open && (
            <div className="border-t border-slate-100 bg-slate-50 px-5 py-4">
              <p className="text-xs text-slate-500 leading-relaxed whitespace-pre-wrap font-mono">
                {result.citation_text}
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main page
// ─────────────────────────────────────────────────────────────────────────────

export default function CheckPage() {
  const [drugA, setDrugA] = useState<DrugState>(EMPTY_DRUG);
  const [drugB, setDrugB] = useState<DrugState>(EMPTY_DRUG);
  const [phase, setPhase] = useState<CheckPhase>({ type: "idle" });

  const dqA = useDebounce(drugA.query, 350);
  const dqB = useDebounce(drugB.query, 350);
  const wsCleanup = useRef<(() => void) | null>(null);

  // ── Auto-resolve ──────────────────────────────────────────────────────────

  const resolveSlot = useCallback(async (slot: DrugSlot, query: string) => {
    const set = slot === "a" ? setDrugA : setDrugB;
    if (!query.trim() || query.trim().length < 2) {
      set(p => ({ ...p, status: "idle", candidates: undefined, resolved: undefined }));
      return;
    }
    set(p => ({ ...p, status: "loading" }));
    try {
      const res: ResolveResponse = await resolveDrug(query.trim());
      if (res.status === "resolved" && res.drug) {
        set({ query, status: "resolved", resolved: res.drug });
      } else if (res.status === "ambiguous" && res.candidates?.length) {
        set({ query, status: "ambiguous", candidates: res.candidates });
      } else {
        set({ query, status: "not_found" });
      }
    } catch (e) {
      set({ query, status: "error", errorMsg: e instanceof ApiError ? e.message : "Resolution failed." });
    }
  }, []);

  useEffect(() => { if (drugA.status !== "resolved") resolveSlot("a", dqA); }, [dqA]); // eslint-disable-line
  useEffect(() => { if (drugB.status !== "resolved") resolveSlot("b", dqB); }, [dqB]); // eslint-disable-line

  const handleSelect = useCallback((slot: DrugSlot, c: ResolvedDrugInfo) => {
    (slot === "a" ? setDrugA : setDrugB)({ query: c.display_name, status: "resolved", resolved: c });
  }, []);

  const handleClear = useCallback((slot: DrugSlot) => {
    (slot === "a" ? setDrugA : setDrugB)(EMPTY_DRUG);
  }, []);

  // ── Submit ────────────────────────────────────────────────────────────────

  const canSubmit =
    drugA.status === "resolved" && drugB.status === "resolved" &&
    drugA.resolved?.drug_id != null && drugB.resolved?.drug_id != null &&
    phase.type === "idle";

  const startPolling = useCallback(async (jobId: string) => {
    let polls = 0;
    const poll = async () => {
      if (++polls > 80) { setPhase({ type: "error", message: "Timed out." }); return; }
      try {
        const d = await getCheckStatus(jobId);
        if (d.status === "done" && d.result) { setPhase({ type: "done", result: d.result }); }
        else if (d.status === "failed") { setPhase({ type: "error", message: d.error_message ?? "Job failed." }); }
        else { setPhase(p => p.type === "waiting" ? { ...p, jobStatus: d.status } : p); setTimeout(poll, 2500); }
      } catch { setTimeout(poll, 2500); }
    };
    poll();
  }, []);

  const handleSubmit = useCallback(async () => {
    if (!canSubmit) return;
    wsCleanup.current?.();
    setPhase({ type: "submitting" });

    try {
      const { job_id } = await createCheckJob(drugA.resolved!.drug_id!, drugB.resolved!.drug_id!);
      setPhase({ type: "waiting", jobId: job_id, jobStatus: "queued" });

      let settled = false;
      const cleanup = subscribeJobWs(job_id, (data) => {
        if (settled) return;
        if ("error" in data) { settled = true; startPolling(job_id); return; }
        const s = data as CheckStatusResponse;
        if (s.status === "done" && s.result) { settled = true; setPhase({ type: "done", result: s.result }); }
        else if (s.status === "failed") { settled = true; setPhase({ type: "error", message: s.error_message ?? "Job failed." }); }
        else { setPhase(p => p.type === "waiting" ? { ...p, jobStatus: s.status } : p); }
      }, () => { if (!settled) { settled = true; startPolling(job_id); } });
      wsCleanup.current = () => { settled = true; cleanup(); };
    } catch (e) {
      setPhase({ type: "error", message: e instanceof ApiError ? e.message : "Submission failed." });
    }
  }, [canSubmit, drugA.resolved, drugB.resolved, startPolling]); // eslint-disable-line

  const handleReset = useCallback(() => {
    wsCleanup.current?.();
    wsCleanup.current = null;
    setDrugA(EMPTY_DRUG);
    setDrugB(EMPTY_DRUG);
    setPhase({ type: "idle" });
  }, []);

  const isProcessing = phase.type === "submitting" || phase.type === "waiting";
  const hasResult = phase.type === "done" || phase.type === "error";

  // ─────────────────────────────────────────────────────────────────────────
  // Layout: two columns on wide screens — form left, result right
  // ─────────────────────────────────────────────────────────────────────────

  return (
    <div className="page-container pb-20 min-h-[calc(100vh-64px)] flex flex-col">
      <div className="mb-6">
        <h1 className="text-3xl font-bold text-slate-900">Check an interaction</h1>
        <p className="mt-1 text-slate-500">Enter two drug names and submit.</p>
      </div>

      <div className="flex-1 grid gap-6 lg:grid-cols-2 lg:items-start">
        {/* ── Left: input form ─────────────────────────────────────────── */}
        <div className="card px-6 py-6 space-y-5">
          <DrugInput slot="a" state={drugA}
            onChange={q => setDrugA(p => ({ ...p, query: q, status: "idle", resolved: undefined, candidates: undefined }))}
            onSelect={c => handleSelect("a", c)} onClear={() => handleClear("a")} disabled={isProcessing} />

          <DrugInput slot="b" state={drugB}
            onChange={q => setDrugB(p => ({ ...p, query: q, status: "idle", resolved: undefined, candidates: undefined }))}
            onSelect={c => handleSelect("b", c)} onClear={() => handleClear("b")} disabled={isProcessing} />




          <div className="flex gap-3 pt-1">
            <button type="button" id="submit-check" className="btn-primary"
              onClick={handleSubmit} disabled={!canSubmit || isProcessing}>
              {isProcessing ? (
                <><span className="spinner h-4 w-4" aria-hidden="true" />Checking…</>
              ) : "Check interaction"}
            </button>
            {(hasResult || drugA.query || drugB.query) && (
              <button type="button" id="reset-check" className="btn-secondary"
                onClick={handleReset} disabled={isProcessing}>
                Reset
              </button>
            )}
          </div>
        </div>

        {/* ── Right: status / result ────────────────────────────────────── */}
        <div>
          {phase.type === "idle" && (
            <div className="flex h-full items-center justify-center rounded-xl border border-dashed border-slate-200 p-12 text-center">
              <p className="text-sm text-slate-400">Results will appear here.</p>
            </div>
          )}

          {phase.type === "submitting" && (
            <div className="flex items-center gap-3 rounded-lg border border-slate-200 bg-slate-50 px-5 py-4">
              <span className="spinner" aria-hidden="true" />
              <span className="text-sm text-slate-500">Submitting…</span>
            </div>
          )}

          {phase.type === "waiting" && (
            <div className="flex items-center gap-3 rounded-lg border border-slate-200 bg-slate-50 px-5 py-4">
              <span className="spinner" aria-hidden="true" />
              <span className="text-sm text-slate-500">{STATUS_LABELS[phase.jobStatus]}</span>
            </div>
          )}

          {phase.type === "done" && <ResultPanel result={phase.result} />}

          {phase.type === "error" && (
            <div className="status-banner status-banner--error animate-fade-in" role="alert">
              {phase.message}
            </div>
          )}
        </div>
      </div>

      {/* Permanent disclaimer */}
      <div className="disclaimer-band mt-6" role="complementary">
        Decision support only. Not a substitute for clinical judgment. Verify before acting.
      </div>
    </div>
  );
}
