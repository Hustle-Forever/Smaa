/**
 * Presentational atoms shared across the whole app.
 *
 * These were written for the Trends tabs and are the best-engineered UI code in
 * the repo -- documented, accessibility-aware, and disciplined about Tailwind.
 * They were also unreachable from anywhere else: nothing outside
 * components/trends/ imported them, so yas/ went and hand-rolled its own empty
 * states, its own error text and its own inputs. Moving them here is promotion,
 * not cleanup.
 *
 * The old paths stay as re-export shims, so every existing import keeps working
 * byte-for-byte. That is a hard requirement, not politeness: the flag-guarded
 * trends files import from ./shared, and featureFlags.ts promises that flipping
 * one constant is the entire revert.
 *
 * Every colour band is a COMPLETE class string in a map -- Tailwind's content
 * scanner only sees literals, so `"bg-" + x` would compile to nothing.
 */
import type { ReactNode } from "react";

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

const COMPACT = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
});

/** 12400 -> "12.4K". Renders an em dash for null so a metric never reads "0". */
export function fmtCompact(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "-";
  if (Math.abs(n) < 1000) return String(Math.round(n));
  return COMPACT.format(n);
}

/** 4.72 -> "4.7x". The hero metric's own formatter. */
export function fmtMultiple(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "-";
  return `${n >= 10 ? Math.round(n) : n.toFixed(1)}x`;
}

export function fmtScore(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "-";
  return String(Math.round(n));
}
// ---------------------------------------------------------------------------
// Layout helpers
// ---------------------------------------------------------------------------

export const SELECT_CLS =
  "rounded border border-border bg-bg-subtle text-sm px-2.5 py-1.5 text-fg " +
  "focus:outline-none focus:border-accent transition-colors";

export const INPUT_CLS =
  "rounded border border-border bg-bg-subtle text-sm px-3 py-1.5 text-fg " +
  "placeholder:text-muted/70 focus:outline-none focus:border-accent transition-colors";

/** Label-above-control wrapper used across the filter bar. */
export function Field({
  label,
  children,
  className = "",
}: {
  label: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={`flex flex-col gap-1 ${className}`}>
      <span className="eyebrow">{label}</span>
      {children}
    </label>
  );
}

/**
 * One section of a page: its name, an optional action beside it, and a line
 * saying what the section is for.
 *
 * Extracted because four different idioms for this existed across the screens
 * the Settings page now gathers: an `.eyebrow` paragraph, a local unexported
 * copy of this component, a bare `h2` beside a result count, and a `<p>` with
 * hand-written classes. On four separate screens nobody notices. On one page
 * they are four different-looking headings for the same job.
 *
 * `id` is required and not optional: a section heading with no id cannot be
 * pointed at by `aria-labelledby`, and a page made of unlabelled regions is a
 * page a screen reader reads as one undifferentiated block.
 */
export function SectionHeader({
  id,
  title,
  action,
  children,
}: {
  id: string;
  title: string;
  /** Sits to the right of the title. A button, usually. */
  action?: ReactNode;
  /** One line on what this section is for. Plain words, no jargon. */
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start gap-3">
      <div className="min-w-0 flex-1">
        <h2 id={id} className="text-lg font-semibold tracking-tight text-fg">
          {title}
        </h2>
        {children && (
          <p className="mt-1.5 max-w-2xl text-sm leading-relaxed text-muted">
            {children}
          </p>
        )}
      </div>
      {action}
    </div>
  );
}

/**
 * Empty states here TEACH: every one of them names the next action, because
 * an empty trend feed is almost always a setup step that hasn't happened yet.
 */
export function EmptyState({
  title,
  body,
  action,
  variant = "card",
}: {
  title: string;
  body: ReactNode;
  action?: ReactNode;
  /**
   * "card" draws its own bordered surface -- the original behaviour, and the
   * default so existing call sites render identically.
   *
   * "inset" drops the border and background for the many places that are
   * ALREADY inside a .card (the Dashboard panels, for one). Without this the
   * sweep replacing ad-hoc "No items yet" strings would have produced a card
   * inside a card, with a double border.
   */
  variant?: "card" | "inset";
}) {
  return (
    <div
      className={
        variant === "card" ? "card p-10 text-center" : "py-8 text-center"
      }
    >
      <p className="text-sm font-semibold text-fg">{title}</p>
      <div className="lead mt-2 mx-auto max-w-md text-sm">{body}</div>
      {action && <div className="mt-5 flex justify-center gap-2">{action}</div>}
    </div>
  );
}

/**
 * A failure the reader needs to see.
 *
 * `role="alert"` because a fetch that failed after the page was already drawn
 * changes nothing visually except this box, and a screen reader would otherwise
 * be looking at a list that silently stopped updating.
 */
export function ErrorCard({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="card border-danger/30 bg-danger/5 p-4 text-sm text-danger"
    >
      {message}
    </div>
  );
}

/** Circular handle avatar with a letter fallback: IG avatar URLs expire too. */
export function Avatar({
  src,
  name,
  size = 28,
}: {
  src: string | null | undefined;
  name: string;
  size?: number;
}) {
  const initial = (name || "?").replace(/^@/, "").slice(0, 1).toUpperCase();
  return (
    <span
      className="relative inline-flex shrink-0 items-center justify-center overflow-hidden rounded-full bg-bg-muted text-3xs font-semibold text-muted"
      style={{ width: size, height: size }}
    >
      {initial}
      {src && (
        <img
          src={src}
          alt=""
          loading="lazy"
          decoding="async"
          className="absolute inset-0 h-full w-full object-cover"
          onError={(e) => {
            // Drop the broken node so the letter underneath shows through.
            e.currentTarget.style.display = "none";
          }}
        />
      )}
    </span>
  );
}

