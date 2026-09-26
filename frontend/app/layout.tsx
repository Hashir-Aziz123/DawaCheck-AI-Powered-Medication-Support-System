import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import Navbar from "@/components/Navbar";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

export const metadata: Metadata = {
  title: {
    template: "%s | DawaCheck",
    default: "DawaCheck: Drug Interaction Decision Support",
  },
  description:
    "Pharmacist-facing decision support tool for checking drug interactions. Grounded in cited sources, honest about uncertainty.",
  keywords: ["drug interactions", "pharmacist", "decision support", "medication safety"],
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={inter.variable} suppressHydrationWarning>
      <body className="min-h-screen bg-slate-50 text-slate-800 antialiased">
        <Navbar />
        <main>{children}</main>
        <footer className="mt-auto border-t border-slate-200 bg-white py-8">
          <div className="mx-auto max-w-7xl px-8 text-center">
            <p className="text-sm text-slate-400">
              DawaCheck. Decision support only. Not a substitute for clinical judgment.
            </p>
          </div>
        </footer>
      </body>
    </html>
  );
}
