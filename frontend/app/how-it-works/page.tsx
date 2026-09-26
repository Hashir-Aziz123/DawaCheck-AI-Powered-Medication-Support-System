import Link from "next/link";

export default function HowItWorksPage() {
  return (
    <div className="page-container">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-slate-900">How it works</h1>
        <p className="mt-1 text-slate-500">What happens between typing a drug name and seeing a result.</p>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* Left column */}
        <div className="space-y-4">
          <Section title="Name resolution">
            <Step n="1" text="Exact brand-name match against the local database." />
            <Step n="2" text={'Generic ingredient match. Typing "ibuprofen" resolves to the same drug record as "Brufen".'} />
            <Step n="3" text="Fuzzy match on both brand and generic names with a similarity threshold to filter false hits." />
            <Step n="4" text="Live DRAP lookup if the database has no match. The result is stored for future queries." />
            <Note>When a query matches multiple drugs you are shown the candidate list and asked to select one. No registration numbers or internal identifiers are ever displayed.</Note>
          </Section>

          <Section title="The pipeline">
            <PipelineRow label="Retrieve" text="Fetches the drug interaction section of the openFDA label for both drugs." color="teal" />
            <PipelineRow label="Generate" text="A language model reads the retrieved label text and writes a candidate interaction claim." color="teal" />
            <PipelineRow label="Verify" text="The claim is checked against the source passage. If it is not directly supported, it is rejected." color="amber" />
            <PipelineRow label="Return" text="One of three outcomes: interaction_found, none_found, or unverifiable, together with the source text." color="slate" />
          </Section>
        </div>

        {/* Right column */}
        <div className="space-y-4">
          <Section title="Why grounding matters">
            <p className="text-sm text-slate-600">
              The model is given the retrieved label text as its input. It does not answer from internal training knowledge.
              A separate verification step rejects any claim that is not directly supported by the retrieved passage.
              The full citation text is included in every result so you can read the original language yourself.
            </p>
          </Section>

          <Section title="Result types">
            <ul className="space-y-2 text-sm text-slate-600">
              <li className="flex gap-2">
                <span className="text-teal-600 font-semibold shrink-0 mt-0.5">interaction_found</span>
                <span>The label text contained documented interaction language and the claim was verified against it.</span>
              </li>
              <li className="flex gap-2">
                <span className="text-teal-600 font-semibold shrink-0 mt-0.5">none_found</span>
                <span>The label was retrieved and examined. No interaction language was present.</span>
              </li>
              <li className="flex gap-2">
                <span className="text-teal-600 font-semibold shrink-0 mt-0.5">unverifiable</span>
                <span>A claim was generated but failed the verification step. This is not the same as no interaction existing.</span>
              </li>
            </ul>
          </Section>

          <Section title="Limitations">
            <Step n="1" text="openFDA label coverage varies. Not every drug has an interaction section in the label database." />
            <Step n="2" text="FDA labels are not always current with the latest literature." />
            <Step n="3" text="Designed for clinicians. Not intended for patient use." />
            <Step n="4" text="Always cross-reference results with primary sources before making a clinical decision." />
          </Section>
        </div>
      </div>

      <div className="mt-10 flex gap-3">
        <Link href="/check" className="btn-primary" id="hiw-cta-check">Check an interaction</Link>
        <Link href="/browse" className="btn-secondary" id="hiw-cta-browse">Browse dataset</Link>
      </div>
    </div>
  );
}

// -----------------------------------------------------------------------------
// Sub-components
// -----------------------------------------------------------------------------

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card px-5 py-5">
      <h2 className="mb-3 text-sm font-semibold text-slate-900 uppercase tracking-wide">{title}</h2>
      <div className="space-y-2">{children}</div>
    </div>
  );
}

function Step({ n, text }: { n: string; text: string }) {
  return (
    <div className="flex items-start gap-3">
      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-teal-100 text-xs font-bold text-teal-700">{n}</span>
      <p className="text-sm text-slate-600">{text}</p>
    </div>
  );
}

function PipelineRow({ label, text, color }: { label: string; text: string; color: "teal" | "amber" | "slate" }) {
  const cls = {
    teal:  "bg-teal-100 text-teal-700",
    amber: "bg-amber-100 text-amber-700",
    slate: "bg-slate-200 text-slate-500",
  }[color];
  return (
    <div className="flex items-start gap-3">
      <span className={`shrink-0 rounded-full px-2.5 py-0.5 text-xs font-bold uppercase tracking-wide ${cls}`}>{label}</span>
      <p className="text-sm text-slate-600">{text}</p>
    </div>
  );
}

function Note({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded-md bg-slate-50 border border-slate-200 px-3 py-2 text-xs text-slate-500">{children}</p>
  );
}
