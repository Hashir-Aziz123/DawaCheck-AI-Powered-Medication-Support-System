import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "How It Works",
  description:
    "How DawaCheck resolves drug names, retrieves evidence, and decides when to refuse rather than guess.",
};

export default function HowItWorksLayout({ children }: { children: React.ReactNode }) {
  return children;
}
