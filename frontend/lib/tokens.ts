/**
 * Design tokens — single source of truth for colors, spacing, and typography.
 * Import these into components instead of hardcoding values.
 *
 * These mirror the Tailwind config and CSS variables defined in globals.css,
 * so TypeScript consumers can reference them without magic strings.
 */

export const colors = {
  /** Primary teal accent — use for CTAs, focus rings, active states */
  teal: {
    primary:   "#14b8a6", // teal-500
    hover:     "#0d9488", // teal-600
    pressed:   "#0f766e", // teal-700
    subtle:    "#ccfbf1", // teal-100
    subtleText:"#0f766e", // teal-700 on teal-100 bg
  },
  /** Neutral base — off-white background, dark slate text */
  neutral: {
    bg:          "#f8fafc", // slate-50
    bgAlt:       "#f1f5f9", // slate-100
    border:      "#e2e8f0", // slate-200
    borderMuted: "#cbd5e1", // slate-300
    muted:       "#64748b", // slate-500
    subtle:      "#94a3b8", // slate-400
    text:        "#1e293b", // slate-800
    textStrong:  "#0f172a", // slate-900
  },
  /** Status colors — used only for result banners */
  status: {
    interactionFound: {
      bg:     "#fef3c7", // amber-100
      border: "#fde68a", // amber-200
      text:   "#92400e", // amber-800
    },
    noneFound: {
      bg:     "#f1f5f9", // slate-100
      border: "#e2e8f0", // slate-200
      text:   "#334155", // slate-700
    },
    unverifiable: {
      bg:     "#f1f5f9", // slate-100
      border: "#e2e8f0", // slate-200
      text:   "#475569", // slate-600
    },
    error: {
      bg:     "#fef2f2",
      border: "#fecaca",
      text:   "#991b1b",
    },
  },
} as const;

export const spacing = {
  pageMaxWidth: "800px",
  pageXPad:     "1.5rem",
} as const;

export const typography = {
  fontFamily: "var(--font-inter), Inter, system-ui, sans-serif",
  baseSize:   "1rem",
  baseLine:   "1.75",
} as const;
