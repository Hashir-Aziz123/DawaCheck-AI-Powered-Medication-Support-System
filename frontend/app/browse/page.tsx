"use client";

import { useState, useEffect, useMemo } from "react";
import { listDrugs, ApiError, type DrugListEntry } from "@/lib/api/client";
import Link from "next/link";

function DrugCard({ drug }: { drug: DrugListEntry }) {
  return (
    <div className="card px-4 py-4 hover:shadow-card-md transition-shadow">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="font-semibold text-slate-900 truncate text-sm">{drug.brand_name}</p>
          {drug.generic_names.length > 0 && (
            <p className="mt-0.5 text-xs text-slate-400 leading-snug truncate">
              {drug.generic_names.join(", ")}
            </p>
          )}
        </div>
        {drug.dosage_form && (
          <span className="shrink-0 rounded-full border border-slate-200 bg-slate-100 px-2 py-0.5 text-xs text-slate-500">
            {drug.dosage_form}
          </span>
        )}
      </div>
    </div>
  );
}

export default function BrowsePage() {
  const [drugs, setDrugs] = useState<DrugListEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  useEffect(() => {
    let cancelled = false;
    listDrugs().then(d => {
      if (cancelled) return;
      setDrugs(d.drugs); setTotal(d.total); setLoading(false);
    }).catch(e => {
      if (cancelled) return;
      setError(e instanceof ApiError ? e.message : "Could not load drug list."); setLoading(false);
    });
    return () => { cancelled = true; };
  }, []);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return drugs;
    return drugs.filter(d =>
      d.brand_name.toLowerCase().includes(q) ||
      d.generic_names.some(g => g.toLowerCase().includes(q))
    );
  }, [drugs, search]);

  return (
    <div className="page-container">
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold text-slate-900">Drug dataset</h1>
          <p className="mt-1 text-slate-500 text-sm">
            {loading ? "Loading…" : error ? "Failed to load" : `${total} drugs`}
          </p>
        </div>
        {!loading && !error && (
          <div className="flex items-center gap-3">
            <label htmlFor="drug-search" className="sr-only">Search</label>
            <input id="drug-search" type="search" className="input w-64"
              placeholder="Search by name…" value={search}
              onChange={e => setSearch(e.target.value)} />
            {search && (
              <span className="text-sm text-slate-400">{filtered.length} result{filtered.length !== 1 ? "s" : ""}</span>
            )}
          </div>
        )}
      </div>

      {loading && (
        <div className="flex items-center gap-3 py-20 justify-center text-slate-400">
          <span className="spinner" /> Loading…
        </div>
      )}

      {error && (
        <div className="status-banner status-banner--error">{error}</div>
      )}

      {!loading && !error && (
        <>
          {filtered.length === 0 ? (
            <p className="py-12 text-center text-slate-400">No drugs match &ldquo;{search}&rdquo;.</p>
          ) : (
            <div className="grid gap-2.5 sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-4">
              {filtered.map((d, i) => <DrugCard key={`${d.brand_name}-${i}`} drug={d} />)}
            </div>
          )}

          <div className="mt-10 rounded-xl border border-teal-200 bg-teal-50 px-5 py-4 flex items-center justify-between gap-4">
            <p className="text-sm text-teal-800">Ready to check an interaction?</p>
            <Link href="/check" className="btn-primary shrink-0" id="browse-cta">Check now</Link>
          </div>
        </>
      )}
    </div>
  );
}
