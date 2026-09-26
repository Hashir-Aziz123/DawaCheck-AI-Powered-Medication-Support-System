import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "Drug Interaction Decision Support",
  description:
    "Pharmacist-facing tool for checking drug interactions against cited openFDA evidence.",
};

export default function HomePage() {
  return (
    <div className="page-container">
      {/* Hero */}
      <section className="py-20 text-center" aria-labelledby="hero-heading">
        <div className="mx-auto max-w-3xl">
          <h1 id="hero-heading" className="mb-4 text-5xl font-bold tracking-tight text-slate-900">
            Drug interaction checks,{" "}
            <span className="text-teal-600">grounded in evidence</span>
          </h1>
          <p className="mb-10 text-xl text-slate-500">
            For pharmacists. Backed by cited openFDA label text. Honest when it cannot find an answer.
          </p>
          <Link href="/check" className="btn-primary px-8 py-3 text-base" id="cta-check">
            Check an interaction
          </Link>
        </div>
      </section>

      {/* Features */}
      <section className="mt-4 grid gap-4 sm:grid-cols-3" aria-label="Features">
        <FeatureCard title="Cited sources" body="Every result includes the exact openFDA label passage the claim was derived from." />
        <FeatureCard title="Independent verification" body="Each claim is checked against the source passage before it is returned to you." />
        <FeatureCard title="Refuses to guess" body={'Returns "unverifiable" instead of a confident answer when the evidence is missing or contradictory.'} />
      </section>

      {/* Disclaimer */}
      <section className="mt-16 rounded-xl border border-amber-200 bg-amber-50 px-6 py-5" aria-label="Disclaimer">
        <p className="text-sm font-semibold text-amber-700 mb-1">Before you use this</p>
        <p className="text-sm text-amber-800">
          Decision support only. Not a substitute for clinical judgment.
          Designed for clinicians, not patients. Always verify results against primary references before acting.
        </p>
      </section>

      {/* Secondary links */}
      <div className="mt-8 mb-4 flex gap-3">
        <Link href="/browse" className="btn-secondary" id="link-browse">Browse drugs</Link>
        <Link href="/how-it-works" className="btn-secondary" id="link-how">How it works</Link>
      </div>
    </div>
  );
}

function FeatureCard({ title, body }: { title: string; body: string }) {
  return (
    <div className="card px-6 py-5">
      <p className="font-semibold text-slate-900 mb-1">{title}</p>
      <p className="text-sm text-slate-500">{body}</p>
    </div>
  );
}
