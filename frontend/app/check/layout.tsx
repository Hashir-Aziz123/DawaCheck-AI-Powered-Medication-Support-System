import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Check Drug Interaction",
  description:
    "Enter two drug names to check for a documented interaction. Grounded in openFDA label evidence.",
};

export default function CheckLayout({ children }: { children: React.ReactNode }) {
  return children;
}
