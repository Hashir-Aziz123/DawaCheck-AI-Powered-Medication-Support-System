import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Browse Drugs",
  description:
    "Browse the full list of drugs currently in the DawaCheck dataset, including brand name, generic ingredients, and dosage form.",
};

export default function BrowseLayout({ children }: { children: React.ReactNode }) {
  return children;
}
