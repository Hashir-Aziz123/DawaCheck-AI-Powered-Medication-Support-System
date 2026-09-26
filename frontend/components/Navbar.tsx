"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV_LINKS = [
  { href: "/",             label: "Home" },
  { href: "/check",        label: "Check" },
  { href: "/browse",       label: "Browse" },
  { href: "/how-it-works", label: "How it works" },
] as const;

export default function Navbar() {
  const pathname = usePathname();

  return (
    <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/95 backdrop-blur-sm">
      <nav
        className="mx-auto flex max-w-7xl items-center justify-between px-8 py-4"
        aria-label="Main navigation"
      >
        {/* Logo / wordmark */}
        <Link
          href="/"
          className="flex items-center gap-2 font-semibold text-slate-900 hover:no-underline"
          aria-label="DawaCheck home"
        >
          <span
            className="flex h-7 w-7 items-center justify-center rounded-md bg-teal-500 text-sm font-bold text-white"
            aria-hidden="true"
          >
            D
          </span>
          <span className="text-lg tracking-tight">DawaCheck</span>
        </Link>

        {/* Nav links */}
        <ul className="flex items-center gap-1" role="list">
          {NAV_LINKS.map(({ href, label }) => {
            const isActive =
              href === "/" ? pathname === "/" : pathname.startsWith(href);
            return (
              <li key={href}>
                <Link
                  href={href}
                  className={[
                    "rounded-md px-3 py-1.5 text-sm font-medium transition-colors duration-150 hover:no-underline",
                    isActive
                      ? "bg-teal-50 text-teal-700"
                      : "text-slate-600 hover:bg-slate-100 hover:text-slate-900",
                  ].join(" ")}
                  aria-current={isActive ? "page" : undefined}
                >
                  {label}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
    </header>
  );
}
