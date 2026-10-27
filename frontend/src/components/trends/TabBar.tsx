/**
 * The underline tab bar, extracted.
 *
 * This markup existed twice, byte for byte — pages/TrendFinder.tsx and
 * pages/BrandWorkspace.tsx — and those two were the only files in the frontend
 * carrying `aria-current`. Adding a third copy for the Configure sub-bar is
 * what forced the extraction: three copies is where an accessibility attribute
 * starts going missing from one of them.
 *
 * Two sizes. `page` is the section bar and is pixel-identical to what the two
 * pages rendered before this file existed — same paddings, same text size, same
 * accent underline — so the extraction is invisible on screen. `sub` is for a
 * bar nested UNDER a page bar: smaller type, tighter padding and a muted
 * underline, so the eye reads it as subordinate rather than as a second, equal
 * row of tabs.
 *
 * Semantics are deliberately the ones the original had: plain <button>s with
 * `aria-current="page"` on the active one. NOT role="tablist"/role="tab" —
 * those promise arrow-key roving focus, which this bar has never implemented,
 * and a lying role is worse than no role. Every tab is a real button, so Tab
 * and Enter reach all of them.
 *
 * Which is exactly why the focus ring below matters. These are the first real
 * controls on both pages that use this bar, so a keyboard reader arrives here
 * first, and until now the only thing marking where they had arrived was the
 * browser default outline that `.btn` suppresses everywhere else in the app.
 * The ring is the same accent one every other button in the app draws, and it
 * is drawn INSET so it cannot straddle the bar's own bottom border.
 *
 * Nothing about the props changed to add it. pages/BrandWorkspace.tsx renders
 * this bar too and knows nothing about this file's edits, so every change here
 * is either a class or an attribute: no prop is added, renamed or required.
 */
import type { ComponentType } from "react";

export interface TabBarItem<K extends string> {
  key: K;
  label: string;
  /** Optional — lucide icons throughout this app, rendered at 16px. */
  Icon?: ComponentType<any>;
}

interface Props<K extends string> {
  /** Callers pass their own richer tab descriptors; only these fields are read. */
  items: readonly TabBarItem<K>[];
  active: K;
  onSelect: (key: K) => void;
  /** `page` = section bar (default). `sub` = nested under a page bar. */
  size?: "page" | "sub";
  /**
   * Names the bar for screen readers. Worth passing when two bars are on the
   * screen at once, which is exactly the Configure case.
   */
  label?: string;
}

/** Complete literals — the Tailwind scanner never sees a built-up string. */
const BUTTON_CLS = {
  page: "relative -mb-px flex items-center gap-2 rounded px-4 py-2.5 text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/40",
  sub: "relative -mb-px flex items-center gap-2 rounded px-3 py-2 text-[13px] font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/40",
} as const;

const UNDERLINE_CLS = {
  page: "absolute left-0 right-0 -bottom-px h-0.5 bg-accent rounded-full",
  sub: "absolute left-0 right-0 -bottom-px h-0.5 bg-accent/70 rounded-full",
} as const;

const ICON_CLS = {
  page: "w-4 h-4",
  sub: "w-3.5 h-3.5",
} as const;

export function TabBar<K extends string>({
  items,
  active,
  onSelect,
  size = "page",
  label,
}: Props<K>) {
  return (
    <div
      role={label ? "group" : undefined}
      aria-label={label}
      className="flex flex-wrap items-center gap-1 border-b border-border"
    >
      {items.map((t) => {
        const on = t.key === active;
        return (
          <button
            key={t.key}
            type="button"
            onClick={() => onSelect(t.key)}
            aria-current={on ? "page" : undefined}
            className={[
              BUTTON_CLS[size],
              on ? "text-fg" : "text-muted hover:text-fg",
            ].join(" ")}
          >
            {/* Decorative: the label beside it is the button's whole name, so
                an announced icon would only say it twice. */}
            {t.Icon && (
              <t.Icon
                className={ICON_CLS[size]}
                strokeWidth={1.8}
                aria-hidden
              />
            )}
            {t.label}
            {on && <span className={UNDERLINE_CLS[size]} />}
          </button>
        );
      })}
    </div>
  );
}
