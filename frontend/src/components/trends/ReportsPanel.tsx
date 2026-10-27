/**
 * The SMAA intelligence Report tab.
 *
 * WHAT IT IS. A report is BUILT when someone asks for one, kept, and then read.
 * Nothing on this page is written here: every number, heading, table and line
 * comes off the envelope the backend assembled. This renderer phrases nothing.
 *
 * THREE THINGS THIS FILE GETS RIGHT THAT THE VERSION BEFORE IT DID NOT:
 *
 * 1. IT DOES NOT BUILD A REPORT WHEN YOU OPEN THE TAB. The old panel fired two
 *    fetches on mount and two more on every window or scope change, and each
 *    full fetch cost two model passes (one contended Vertex call has been
 *    measured at 113 s). So simply looking at this screen spent money and
 *    produced a document nobody had asked for. Now the tab loads an INDEX of
 *    reports that already exist, which costs one cheap query, and builds
 *    nothing until the button is pressed.
 *
 * 2. THE SAME DOCUMENT IS ALREADY THERE. A report is identified by what it is
 *    about, not by when it was asked for: scope, mode, range and the day. Ask
 *    twice on the same day and the second ask is free and returns the first
 *    answer (`cached: true`), so the numbers cannot quietly shift under a
 *    reader between two looks at "the same" report. `Regenerate` is the
 *    explicit override for when the collectors have run since.
 *
 * 3. VIEW AND DOWNLOAD ARE ONE DOCUMENT. They used to be two: the screen drew a
 *    hand-picked half-dozen blocks with bespoke React, while the PDF looped over
 *    EVERY section in the envelope. Two renderers reading two different lists
 *    cannot agree, and they did not. Now `ReportPage` walks the same ordered
 *    `sections` array the PDF walks, dispatching on `kind` exactly as
 *    `social_report_pdf._RENDERERS` does. A section added on the server appears
 *    in both or neither. The download is the stored payload the screen read, so
 *    it is not even a re-render — it is the same bytes.
 *
 * WHY THERE IS SO MUCH LESS TEXT. Every section carries a `basis` string, and
 * both renderers used to print it under the heading: ~250 characters of
 * methodology per section, 31 of them, near 8,000 characters of prose wrapped
 * around the numbers a reader came for. It read as a second, denser section
 * under every single title. The audit trail is not lost — the PDF prints all of
 * them once, at the end, under "Where these numbers come from" — but it is no
 * longer the first thing between a heading and its content. Same decision on
 * both sides, for the same reason.
 *
 * WHY THE BODY SHAPES ARE PARSED RATHER THAN CAST. `ReportSection.body` is
 * `unknown` on purpose: its shape depends on `kind`, and the server is the only
 * thing that guarantees they agree. Every reader below narrows defensively and
 * renders nothing when the shape is wrong, so a newly-shaped section drops its
 * body instead of blanking the tab with a runtime TypeError. A dropped body is
 * never backfilled with a placeholder.
 *
 * WHY THE DOWNLOAD IS AN <a>, NOT A FETCH. The PDF route sets
 * `Content-Disposition: attachment; filename="…"`. Pulling the bytes into a
 * Blob would throw that filename away and save "download.pdf"; letting the
 * browser make the request keeps the server's name.
 *
 * PRINTING. The reader carries `data-print-root` and the page `data-print-page`,
 * which is the whole wiring for the `@media print` block in styles.css: it hides
 * the app, brings the reader back, drops anything marked `.no-print`, forces the
 * figures' fills to print as ink rather than being dropped as decoration, and
 * starts a new sheet per page. So Ctrl+P saves exactly the page on screen, while
 * Download fetches the server's own complete PDF.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  Download,
  ExternalLink,
  Eye,
  FileText,
  Loader2,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";

import { Skeleton } from "../Skeleton";
import { api } from "../../lib/api";
import type {
  ReportGrain,
  ReportPeriod,
  ReportScope,
  ReportSection,
  SavedReport,
  SocialReport,
} from "../../lib/types";
import { useConfirm } from "../ConfirmProvider";
import { VIRAL_SOCIAL_TRENDS_ENABLED } from "./featureFlags";
import { PeriodPicker } from "./PeriodPicker";
import { ErrorCard, SELECT_CLS } from "./shared";
import { humanError } from "./trendsApi";

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------

/**
 * The three shapes a report may cover, and nothing in between.
 *
 * These are not lengths, they are CALENDAR PERIODS: a week runs Monday to
 * Sunday, "15 days" is the 1st to the 15th or the 16th to the end of the
 * month, and "30 days" is the month itself. So a second half is thirteen days
 * in February, and that is correct: the window is the half month, not a count.
 *
 * The names are the ones the team already uses. The backend owns the list and
 * enforces it, so a hand-edited URL gets the same refusal this picker would
 * give.
 */
const GRAINS: Array<{ key: ReportGrain; label: string; hint: string }> = [
  { key: "week", label: "1 week", hint: "Monday to Sunday" },
  { key: "half", label: "15 days", hint: "1st to 15th, or 16th to the end" },
  { key: "month", label: "30 days", hint: "a whole calendar month" },
];

/**
 * What the report can be about.
 *
 * Plain words, because this select is the only place the choice is now made.
 * The VALUES are the backend's and cannot drift; only the labels are ours.
 *
 * VIRAL SOCIAL TRENDS STAYS BEHIND ITS FLAG. The backend drops "viral" from its
 * own scope list while that stream is not being collected, so an ungated entry
 * here answers 422. The list has to be the list the server accepts.
 */
const REPORT_SCOPES: Array<{ value: ReportScope; label: string }> = [
  { value: "all", label: "Everything" },
  { value: "competitors", label: "Competitors" },
  // Industry Signal was missing, and the backend has always accepted it: a
  // `scope=news` report is the same document narrowed to that stream, and it
  // is one of the three tabs of the page this reports on. Leaving it off meant
  // the only way to read the news stream on its own was to edit the URL.
  { value: "news", label: "Industry Signal" },
  ...(VIRAL_SOCIAL_TRENDS_ENABLED
    ? [{ value: "viral" as ReportScope, label: "Viral Social Trends" }]
    : []),
];

/** Falls back to the first entry rather than throwing: a scope this list does
 *  not know is a bug worth showing as the whole picture, not a blank control. */
function scopeMeta(scope: ReportScope) {
  return REPORT_SCOPES.find((s) => s.value === scope) ?? REPORT_SCOPES[0];
}

// ---------------------------------------------------------------------------
// Reading the envelope
// ---------------------------------------------------------------------------

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function textOf(v: unknown): string {
  return typeof v === "string" ? v.trim() : "";
}

function stringList(v: unknown): string[] {
  return Array.isArray(v) ? v.map(textOf).filter(Boolean) : [];
}

// ---------------------------------------------------------------------------
// Dates, in the words the masthead uses
// ---------------------------------------------------------------------------

const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function day(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

/** "6 Aug – 5 Sep 2026". An en dash, which is what a range takes. */
function rangeLabel(from: string | null | undefined, to: string | null | undefined): string {
  const a = day(from);
  const b = day(to);
  if (!a || !b) return "";
  // The year is stated once when both ends share it.
  const ay = a.slice(-4);
  return ay === b.slice(-4) ? `${a.slice(0, -5)} – ${b}` : `${a} – ${b}`;
}

/**
 * Every date on this screen comes off the server. `/api/reports/periods` says
 * which windows exist, how many records are in each and which are already
 * built; `PeriodPicker` draws them. Nothing here works out a period for
 * itself, because a picker that did would eventually disagree with the route
 * about what a week is, and the reader would see a refusal with no
 * explanation.
 */

// ---------------------------------------------------------------------------
// One section, by kind
//
// This dispatch IS the promise that the screen and the PDF show the same
// document: `social_report_pdf._RENDERERS` has exactly these six entries, and
// both sides walk the same ordered array. Adding a kind on the server means
// adding it in two places, and until then BOTH sides say "nothing rendered"
// rather than one silently dropping it.
// ---------------------------------------------------------------------------

function Prose({ body }: { body: unknown }) {
  const paragraphs = isRecord(body) ? stringList(body.paragraphs) : [];
  if (!paragraphs.length) return null;
  return (
    <div className="space-y-2">
      {paragraphs.map((p, i) => (
        <p key={i} className="text-sm leading-relaxed text-fg">{p}</p>
      ))}
    </div>
  );
}

function Bullets({ body }: { body: unknown }) {
  const items = isRecord(body) ? stringList(body.items) : [];
  if (!items.length) return null;
  return (
    <ol className="divide-y divide-border">
      {items.map((item, i) => (
        <li key={i} className="flex gap-3 py-2.5 first:pt-0 last:pb-0">
          <span
            aria-hidden
            className="mt-px shrink-0 font-mono text-2xs font-bold tabular-nums text-accent"
          >
            {String(i + 1).padStart(2, "0")}
          </span>
          {/* A bullet arrives as the action, a newline, then its evidence. Only
              the action is printed large: the evidence is the half that cites
              section ids, and it reads as a second paragraph under every line. */}
          <div className="min-w-0 flex-1">
            <p className="text-sm leading-snug text-fg">{item.split("\n")[0]}</p>
          </div>
        </li>
      ))}
    </ol>
  );
}

/**
 * The counted figures, at headline size.
 *
 * `value` is the server's own formatted string, reprinted, never reformatted —
 * so the screen and the PDF cannot round differently. `delta` is null when
 * there is no comparable history, which is NOT the same as no change, so a
 * missing delta draws no chip rather than a flat one.
 *
 * THE ARROW IS NOT COLOURED GOOD OR BAD, deliberately. More industry news in a
 * month is not a win and fewer competitor posts is not a loss, so a green rise
 * and a red fall would be this page inventing a verdict it has no basis for.
 */
function Kpis({ body }: { body: unknown }) {
  const items = isRecord(body) && Array.isArray(body.items) ? body.items : [];
  const tiles = items.filter(isRecord).filter((it) => textOf(it.value));
  if (!tiles.length) return null;
  return (
    <div className="grid grid-cols-2 gap-x-6 gap-y-5 sm:grid-cols-3">
      {tiles.map((it, i) => {
        const delta = textOf(it.delta);
        return (
          <div key={i}>
            <p className="text-2xl font-bold leading-none tabular-nums tracking-tight text-fg md:text-3xl">
              {textOf(it.value)}
            </p>
            <p className="mt-1.5 text-2xs leading-snug text-muted">{textOf(it.label)}</p>
            {delta && (
              <span className="mt-1.5 inline-block rounded bg-bg-muted px-1.5 py-0.5 font-mono text-3xs tabular-nums text-muted">
                {delta}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** A percentage cell, as a number between 0 and 100. NaN for anything else,
 *  which is how "n/a" and a plain count both opt out of the inline bar. */
function percentOf(cell: string): number {
  if (!/^\s*\d+(\.\d+)?\s*%\s*$/.test(cell)) return NaN;
  const n = Number.parseFloat(cell);
  return Number.isFinite(n) ? Math.min(Math.max(n, 0), 100) : NaN;
}

/**
 * A table, as a table.
 *
 * The one place the renderer adds anything: a cell the server wrote as a
 * percentage gets a bar behind it, scaled to the whole rather than to the
 * biggest value in the column. That is decoration on a measurement, not a new
 * measurement — the text is still exactly the server's string, so nothing here
 * can disagree with the PDF; the PDF simply has no bar.
 */
function Table({ body }: { body: unknown }) {
  if (!isRecord(body)) return null;
  const columns = stringList(body.columns);
  const align = stringList(body.align);
  const rows = Array.isArray(body.rows)
    ? body.rows.filter(Array.isArray).map((r) => (r as unknown[]).map(textOf))
    : [];
  if (!columns.length || !rows.length) return null;

  const cellAlign = (i: number) =>
    align[i] === "right" ? "text-right" : align[i] === "center" ? "text-center" : "text-left";

  return (
    // Its own scroll container: a wide table is the one thing allowed to be
    // wider than the page, and it must not drag the whole sheet sideways.
    <div className="-mx-1 overflow-x-auto px-1">
      <table className="w-full min-w-[32rem] border-collapse text-left">
        <thead>
          <tr className="border-b border-border-strong">
            {columns.map((c, i) => (
              <th
                key={i}
                scope="col"
                className={`${cellAlign(i)} pb-2 pr-3 font-mono text-3xs font-bold uppercase tracking-wider2 text-muted last:pr-0`}
              >
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, r) => (
            <tr key={r} className="border-b border-border last:border-b-0">
              {columns.map((_, c) => {
                const cell = row[c] ?? "";
                const pct = percentOf(cell);
                return (
                  <td
                    key={c}
                    className={`${cellAlign(c)} relative py-2 pr-3 text-2xs tabular-nums text-fg last:pr-0`}
                  >
                    {Number.isFinite(pct) && (
                      // The bar grows from the side the number is aligned to,
                      // so it ENDS at the figure it belongs to. Anchored the
                      // other way it reads as a block floating beside an
                      // unrelated number.
                      <span
                        aria-hidden
                        className={`absolute inset-y-1 rounded-sm bg-accent/15 ${
                          align[c] === "right" ? "right-0" : "left-0"
                        }`}
                        style={{ width: `${pct}%` }}
                      />
                    )}
                    <span className="relative">{cell}</span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * How busy the window was, one column per bucket.
 *
 * THE SERIES ARE ADDED, and that is the only arithmetic on this page. The
 * server buckets each stream separately so a line chart could draw one line per
 * stream; a brief has room for the shape, not for three lines and a legend, and
 * the shape of "how busy was it" is the total.
 *
 * DIVS, NOT AN SVG. The column count is the server's and the width is the
 * reader's, so the only ways to draw this in SVG are a non-uniform
 * `preserveAspectRatio` (which stretches the rounded cap into an oval) or a
 * measured viewBox and a resize observer. A flex row sizes itself and prints as
 * what is on screen.
 */
function Chart({ body }: { body: unknown }) {
  if (!isRecord(body)) return null;
  const labels = Array.isArray(body.labels) ? body.labels.map(textOf) : [];
  const series = Array.isArray(body.series) ? body.series.filter(isRecord) : [];
  if (!labels.length || !series.length) return null;

  const totals = labels.map(() => 0);
  for (const s of series) {
    if (!Array.isArray(s.values)) continue;
    s.values.forEach((v, i) => {
      if (i < totals.length && typeof v === "number" && Number.isFinite(v)) {
        totals[i] += v;
      }
    });
  }
  const peak = totals.reduce((m, v) => Math.max(m, v), 0);
  if (peak <= 0) return null;

  return (
    <figure className="m-0">
      <div
        className="flex h-24 items-end gap-[2px]"
        role="img"
        aria-label={`Items per bucket from ${labels[0]} to ${labels[labels.length - 1]}, peaking at ${peak.toLocaleString()}`}
      >
        {totals.map((v, i) => (
          <div
            key={i}
            title={`${v.toLocaleString()} item(s), ${labels[i]}`}
            className="flex h-full max-w-[24px] flex-1 items-end"
          >
            {/* The floor is 3%, not zero, for any bucket that counted
                something: one item in a 30-day window is a fraction of a pixel
                and would read as "nothing happened" about a day something did.
                A true zero keeps its zero height. */}
            <div
              className="w-full rounded-t bg-accent/75 transition-colors hover:bg-accent"
              style={{ height: v > 0 ? `${Math.max((v / peak) * 100, 3)}%` : "0%" }}
            />
          </div>
        ))}
      </div>
      <figcaption className="mt-2 flex items-baseline justify-between gap-3 border-t border-border pt-2 font-mono text-3xs text-muted">
        <span>{labels[0]}</span>
        <span className="tabular-nums">
          busiest: <span className="font-bold text-fg">{peak.toLocaleString()}</span>
        </span>
        <span>{labels[labels.length - 1]}</span>
      </figcaption>
    </figure>
  );
}

/**
 * The measured signal list: what actually happened, one line each.
 *
 * This is the ONE place a full headline and its real link live together, so the
 * headline is printed whole rather than truncated to a table cell.
 */
function Signals({ body }: { body: unknown }) {
  const groups = isRecord(body) && Array.isArray(body.groups)
    ? body.groups.filter(isRecord)
    : [];
  if (!groups.length) return null;

  return (
    <div className="space-y-5">
      {groups.map((group, gi) => {
        const items = Array.isArray(group.items) ? group.items.filter(isRecord) : [];
        if (!items.length) return null;
        return (
          <div key={gi}>
            <p className="eyebrow mb-2">{textOf(group.group)}</p>
            <ol className="divide-y divide-border">
              {items.map((item, i) => {
                const name = textOf(item.name);
                if (!name) return null;
                const link = textOf(item.link);
                const metrics = Array.isArray(item.metrics)
                  ? item.metrics
                      .filter(isRecord)
                      .map((m) => `${textOf(m.label)} ${textOf(m.value)}`.trim())
                      .filter(Boolean)
                  : [];
                return (
                  <li key={i} className="flex gap-3 py-2.5 first:pt-0 last:pb-0">
                    <span
                      aria-hidden
                      className="mt-px shrink-0 font-mono text-2xs font-bold tabular-nums text-accent"
                    >
                      {String(i + 1).padStart(2, "0")}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-baseline gap-x-2">
                        <h4 className="text-sm font-medium leading-snug text-fg">{name}</h4>
                        {link && (
                          <a
                            href={link}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex shrink-0 items-center gap-1 rounded bg-accent/10 px-1.5 py-0.5 font-mono text-3xs text-accent transition-colors hover:bg-accent hover:text-white"
                          >
                            <ExternalLink className="h-3 w-3" aria-hidden />
                            Open
                          </a>
                        )}
                      </div>
                      {metrics.length > 0 && (
                        <p className="mt-1 font-mono text-3xs text-muted">
                          {metrics.join("  ·  ")}
                        </p>
                      )}
                    </div>
                  </li>
                );
              })}
            </ol>
          </div>
        );
      })}
    </div>
  );
}

const RENDERERS: Record<string, (props: { body: unknown }) => ReactNode> = {
  prose: Prose,
  bullets: Bullets,
  kpis: Kpis,
  table: Table,
  chart: Chart,
  signals: Signals,
};

/**
 * One section: its number, its title, and its body. No basis line.
 *
 * A section that is `available: false` prints its reason in one muted line and
 * stops. The gap is part of the finding — "this section measures a stream this
 * report does not cover" is exactly what a reader needs — but it is one line,
 * not a paragraph.
 */
function Section({ section, number }: { section: ReportSection; number: number }) {
  const title = textOf(section.title) || "(untitled section)";
  const Renderer = RENDERERS[String(section.kind || "").toLowerCase()];

  return (
    <section className="py-6">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span aria-hidden className="font-mono text-2xs font-bold tabular-nums text-accent">
          {String(number).padStart(2, "0")}
        </span>
        <h3 className="text-base font-bold tracking-tight text-fg">{title}</h3>
        {section.estimated && (
          // Three words, not a paragraph: "a model judged this" changes how the
          // content below should be read, and nothing else about it does.
          <span className="rounded bg-warning/10 px-1.5 py-0.5 font-mono text-3xs font-bold uppercase tracking-wider text-warning">
            AI-assessed
          </span>
        )}
      </div>

      {!section.available ? (
        <p className="text-2xs leading-snug text-muted">
          <span className="font-semibold text-fg">Not enough data. </span>
          {textOf(section.unavailable_reason) || "No reason was recorded."}
        </p>
      ) : Renderer ? (
        <Renderer body={section.body} />
      ) : (
        <p className="text-2xs text-muted">
          This section is a kind this screen cannot draw yet.
        </p>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

/**
 * The whole document, in the server's own order.
 *
 * No picking, no reordering, no "this section is interesting and that one is
 * not". The PDF makes none of those choices either, and that agreement is the
 * entire reason the two can be trusted to be the same report.
 */
function ReportPage({ report }: { report: SocialReport }) {
  const window = report.meta?.window;
  const windowLabel = rangeLabel(window?.from, window?.to);
  const scopeLabel = report.meta?.scope_label || scopeMeta(report.meta?.scope ?? "all").label;
  const sections = Array.isArray(report.sections) ? report.sections : [];

  return (
    <article data-print-page className="card p-6 md:p-10">
      <header className="border-b border-border-strong pb-5">
        {/* The one piece of pure decoration on the page, and it earns its
            place: it is what makes the masthead read as the top of a document
            rather than as the first row of a list. */}
        <div className="mb-3 h-1 w-10 rounded-full bg-accent" aria-hidden />
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <h2 className="text-2xl font-bold tracking-tight text-fg md:text-3xl">
            SMAA Social Intelligence
          </h2>
          <p className="eyebrow">42 Abu Dhabi · {scopeLabel}</p>
        </div>
        <p className="mt-2 font-mono text-2xs text-muted">{windowLabel}</p>
      </header>

      {/* `divide-y` rather than a border on each section: the number of sections
          is the server's, and a per-section border either doubles against its
          neighbour or leaves a gap. */}
      <div className="divide-y divide-border">
        {sections.map((section, i) => (
          <Section key={section.id || i} section={section} number={i + 1} />
        ))}
      </div>

      <p className="mt-6 border-t border-border pt-3 font-mono text-3xs uppercase tracking-wider2 text-muted">
        42 Abu Dhabi · {windowLabel}
      </p>
    </article>
  );
}

/**
 * Full-screen reader, with Download beside the close button.
 *
 * PORTALLED TO document.body, and it has to be. The Trends page wrapper carries
 * `animate-pageEnter`, whose keyframes end on a `transform` that `fill-mode:
 * both` leaves applied, and a transformed ancestor becomes the containing block
 * for every `position: fixed` descendant. Rendered in place, `inset-0` sized
 * itself to the page wrapper instead of the viewport, so the dim covered the
 * middle of the screen and missed the edges. `MakeThisFlow` solves the same
 * problem the same way.
 */
function ReportViewer({
  report,
  pdfHref,
  onClose,
}: {
  report: SocialReport;
  pdfHref: string;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, [onClose]);

  const label = rangeLabel(report.meta?.window?.from, report.meta?.window?.to);

  return createPortal(
    <div
      className="fixed inset-0 z-[100] flex items-start justify-center overflow-y-auto bg-black/60 p-4 sm:p-8"
      role="dialog"
      aria-modal="true"
      aria-label={`SMAA Social Intelligence, ${label}`}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        data-print-root
        className="w-full max-w-3xl"
      >
        {/* Chrome, not content. `.no-print` takes it out of the printed sheet. */}
        <div className="no-print sticky top-0 z-10 mb-4 flex items-center justify-between gap-3 rounded-xl border border-border bg-bg/90 px-4 py-2.5 shadow-sm backdrop-blur">
          <p className="min-w-0 truncate text-sm font-semibold text-fg">
            SMAA Social Intelligence
            <span className="ml-2 font-normal text-muted">{label}</span>
          </p>
          <div className="flex shrink-0 items-center gap-2">
            {/* A real anchor, never a fetch: the response carries
                `Content-Disposition: attachment; filename=…` and pulling the
                bytes into a Blob here would throw that name away. */}
            <a
              href={pdfHref}
              target="_blank"
              rel="noopener noreferrer"
              className="btn btn-accent text-xs"
            >
              <Download className="h-3.5 w-3.5" aria-hidden />
              Download
            </a>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close the report"
              className="btn btn-ghost text-xs"
            >
              <X className="h-3.5 w-3.5" aria-hidden />
              Close
            </button>
          </div>
        </div>

        <ReportPage report={report} />
      </div>
    </div>,
    document.body,
  );
}

// ---------------------------------------------------------------------------
// The tab
// ---------------------------------------------------------------------------

/**
 * What a stored row says about itself, in one line.
 *
 * The BACKEND writes this (`report_periods.describe`), so a row cannot be
 * described one way in the list and another in the document. The fallback is
 * for a client talking to an older build, not for a row we expect to see.
 */
function savedLabel(row: SavedReport): string {
  return row.label || rangeLabel(row.window_from, row.window_to) ||
    `${row.days} days`;
}

export function ReportsPanel() {
  // A shape, and which one of that shape. Both come off the server: `periods`
  // is the entire menu, and there is nothing else on it.
  const [grain, setGrain] = useState<ReportGrain>("week");
  const [periods, setPeriods] = useState<ReportPeriod[]>([]);
  const [loadingPeriods, setLoadingPeriods] = useState(true);
  const [picked, setPicked] = useState<string | null>(null);
  const [scope, setScope] = useState<ReportScope>(REPORT_SCOPES[0].value);

  const [saved, setSaved] = useState<SavedReport[]>([]);
  const [listing, setListing] = useState(true);
  const [busy, setBusy] = useState(false);
  const [opening, setOpening] = useState<number | null>(null);
  const [removing, setRemoving] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const [open, setOpen] = useState<{ id: number; report: SocialReport } | null>(null);
  const confirm = useConfirm();

  /** The index, and nothing else. This is the only thing that runs on mount,
   *  and it builds nothing. */
  const refreshIndex = useCallback(async () => {
    setListing(true);
    try {
      const res = await api.reports.saved();
      setSaved(res.items || []);
    } catch (e) {
      setError(humanError(e, "We couldn't load your reports"));
    } finally {
      setListing(false);
    }
  }, []);

  /** The menu for this shape and stream. One cheap query, no builder, and it
   *  carries which periods are already built so the tab never has to guess. */
  const refreshPeriods = useCallback(async () => {
    setLoadingPeriods(true);
    try {
      const res = await api.reports.periods(scope, grain);
      setPeriods(res.items || []);
    } catch (e) {
      setPeriods([]);
      setError(humanError(e, "We couldn't load the reporting periods"));
    } finally {
      setLoadingPeriods(false);
    }
  }, [scope, grain]);

  useEffect(() => {
    void refreshIndex();
  }, [refreshIndex]);

  useEffect(() => {
    void refreshPeriods();
  }, [refreshPeriods]);

  // The most recent finished period is the default, and the pick survives a
  // refresh of the menu because it is held BY KEY rather than by object.
  const selected =
    periods.find((p) => p.key === picked) ?? periods[0] ?? null;
  const built = selected?.report_id ?? null;
  // A period we collected through and nothing happened in. Two model passes to
  // write that up is two model passes wasted, and the sentence underneath says
  // so rather than the button simply refusing.
  const empty = !!selected && selected.records === 0;

  async function show(id: number) {
    setOpening(id);
    setError(null);
    try {
      const row = await api.reports.savedOne(id);
      if (row.payload) setOpen({ id, report: row.payload });
      else setError("That report is stored but its contents are missing.");
    } catch (e) {
      setError(humanError(e, "We couldn't open that report"));
    } finally {
      setOpening(null);
    }
  }

  /**
   * Build this period's report, or open the one that exists.
   *
   * There is no "build it again". A finished period cannot gain another day,
   * so a second build could only reword the same numbers, and the reader would
   * have two documents about one window that disagree about how it went.
   */
  async function generate() {
    if (!selected) return;
    if (built != null) {
      await show(built);
      return;
    }
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const res = await api.reports.generate(selected.days, scope, {
        end: selected.to,
      });
      setNote(
        res.cached
          ? "That report already existed, so nothing was rebuilt."
          : `${selected.label} is ready.`,
      );
      if (res.payload) setOpen({ id: res.id, report: res.payload });
      await Promise.all([refreshIndex(), refreshPeriods()]);
    } catch (e) {
      setError(humanError(e, "We couldn't build that report"));
    } finally {
      setBusy(false);
    }
  }

  async function remove(row: SavedReport) {
    const ok = await confirm({
      title: "Delete this report?",
      message: `${savedLabel(row)} will be removed. You can build it again from its period.`,
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    setRemoving(row.id);
    setError(null);
    try {
      await api.reports.remove(row.id);
      if (open?.id === row.id) setOpen(null);
      await Promise.all([refreshIndex(), refreshPeriods()]);
    } catch (e) {
      setError(humanError(e, "We couldn't delete that report"));
    } finally {
      setRemoving(null);
    }
  }

  const grainMeta = GRAINS.find((g) => g.key === grain) ?? GRAINS[0];

  return (
    <div className="space-y-4">
      {/* The whole control surface: which shape, which one, what about, and
          the one button that spends anything. */}
      <div className="card p-4 md:p-5">
        <div className="flex flex-wrap items-end gap-4">
          <div>
            <p className="eyebrow mb-1.5">Period</p>
            <div className="flex flex-wrap items-stretch gap-2">
              <div className="flex h-[3.125rem] items-stretch gap-1 rounded-lg bg-bg-muted p-1 w-fit">
                {GRAINS.map((g) => (
                  <button
                    key={g.key}
                    type="button"
                    onClick={() => {
                      setGrain(g.key);
                      setPicked(null);
                    }}
                    disabled={busy}
                    title={g.hint}
                    className={[
                      "rounded-md px-3 text-xs font-medium transition-all",
                      "disabled:cursor-not-allowed disabled:opacity-40",
                      grain === g.key
                        ? "bg-bg text-fg shadow-sm"
                        : "text-muted hover:bg-bg/60 hover:text-fg",
                    ].join(" ")}
                  >
                    {g.label}
                  </button>
                ))}
              </div>

              <PeriodPicker
                periods={periods}
                selected={selected}
                loading={loadingPeriods}
                disabled={busy}
                onChange={(p) => setPicked(p.key)}
              />
            </div>
          </div>

          <div className="min-w-[11rem] flex-1">
            <p className="eyebrow mb-1.5">Report on</p>
            <select
              value={scope}
              onChange={(e) => setScope(e.target.value as ReportScope)}
              disabled={busy}
              className={`${SELECT_CLS} h-[3.125rem] w-full`}
            >
              {REPORT_SCOPES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>

          <button
            type="button"
            onClick={() => void generate()}
            disabled={busy || !selected || (empty && built == null)}
            title={
              built != null
                ? "This period is already built: open it"
                : empty
                  ? "Nothing was collected in this period"
                  : "Build this period's report"
            }
            className="btn btn-accent text-sm disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                Building…
              </>
            ) : built != null ? (
              <>
                <Eye className="h-4 w-4" aria-hidden />
                View report
              </>
            ) : (
              <>
                <Sparkles className="h-4 w-4" aria-hidden />
                Generate report
              </>
            )}
          </button>
        </div>

        <p className="mt-3 text-3xs leading-snug text-muted">
          <span className="font-semibold text-fg">
            {selected ? `Covers ${selected.label}.` : "Nothing to report on yet."}
          </span>{" "}
          {busy
            ? "Reading what was collected and writing it up. This takes up to a minute."
            : !selected
              ? `No ${grainMeta.label.toLowerCase()} period has finished inside the dates we hold data for.`
              : built != null
                ? "Built once and kept. A finished period cannot change, so there is nothing to rebuild."
                : empty
                  ? "Nothing was collected in it, so there is nothing to report. Pick another period or another stream."
                  : `${selected.records.toLocaleString()} records were collected in it. Nothing is built until you press Generate.`}
        </p>
      </div>

      {error && <ErrorCard message={error} />}
      {note && !error && <p className="px-1 text-2xs text-muted">{note}</p>}

      {/* The report this tab is pointing at right now.
          Held on BOTH loads, not just the index: the menu is what says whether
          this period is already built, so painting before it lands means
          painting "no report yet" over a report that exists. */}
      {listing || loadingPeriods ? (
        <div className="card space-y-3 p-6">
          <Skeleton h="h-3" className="w-28" />
          <Skeleton h="h-6" className="w-72" />
          <Skeleton h="h-4" className="w-40" />
        </div>
      ) : built != null && selected ? (
        <div className="card p-5 md:p-6">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0 flex-1">
              <p className="eyebrow mb-2">{selected.label}</p>
              <h2 className="text-lg font-bold tracking-tight text-fg">
                SMAA Social Intelligence
              </h2>
              <p className="lead mt-1 text-sm">
                {scopeMeta(scope).label} · {selected.days} days ·{" "}
                {selected.records.toLocaleString()} records
              </p>
            </div>

            <div className="flex shrink-0 flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => void show(built)}
                disabled={opening === built}
                className="btn btn-accent text-sm"
              >
                {opening === built ? (
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                ) : (
                  <Eye className="h-4 w-4" aria-hidden />
                )}
                View report
              </button>
              <a
                href={api.reports.savedPdfUrl(built)}
                target="_blank"
                rel="noopener noreferrer"
                title="This exact report as a PDF"
                className="btn btn-ghost text-sm"
              >
                <Download className="h-4 w-4" aria-hidden />
                Download
              </a>
            </div>
          </div>
        </div>
      ) : (
        <div className="card flex flex-col items-center gap-2 p-10 text-center">
          <FileText className="h-6 w-6 text-muted" aria-hidden />
          <p className="text-sm font-semibold text-fg">
            {selected ? `No report for ${selected.label} yet` : "No report yet"}
          </p>
          <p className="max-w-sm text-2xs leading-snug text-muted">
            Pick a finished week, half month or month and press Generate. It is
            built once and kept, so opening this tab again will not rebuild it.
          </p>
        </div>
      )}

      {/* EVERY report that exists, including the one shown above.
          This list used to drop whichever report the controls were pointing
          at, so opening the tab showed a row for two seconds and then took it
          away again the moment the menu landed and said "that one is already
          built". A list of what exists has to hold what exists. */}
      {saved.length > 0 && (
        <div className="card p-4 md:p-5">
          <p className="eyebrow mb-3">
            All reports <span className="text-muted">· {saved.length}</span>
          </p>
          <ul className="divide-y divide-border">
            {saved.map((row) => (
                <li
                  key={row.id}
                  className={[
                    "flex flex-wrap items-center justify-between gap-3 py-2.5",
                    "first:pt-0 last:pb-0",
                    row.id === built ? "text-fg" : "",
                  ].join(" ")}
                >
                  <div className="min-w-0">
                    <p className="flex items-center gap-2 truncate text-2xs font-semibold text-fg">
                      {/* Which one the controls are pointing at, said rather
                          than shown by absence. */}
                      {row.id === built && (
                        <span
                          aria-label="currently selected"
                          className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent"
                        />
                      )}
                      {savedLabel(row)}
                      {/* Built before the three shapes existed, so its dates
                          match nothing on the menu. Saying so is the whole
                          difference between a stale report and a random one. */}
                      {row.period === false && (
                        <span className="ml-2 rounded bg-bg-muted px-1.5 py-0.5 font-mono text-3xs font-normal text-muted">
                          not a standard period
                        </span>
                      )}
                    </p>
                    <p className="font-mono text-3xs text-muted">
                      {scopeMeta(row.scope).label} · built {day(row.generated_at)}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <button
                      type="button"
                      onClick={() => void show(row.id)}
                      disabled={opening === row.id}
                      className="btn btn-ghost btn-sm"
                    >
                      {opening === row.id ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                      ) : (
                        <Eye className="h-3.5 w-3.5" aria-hidden />
                      )}
                      View
                    </button>
                    <a
                      href={api.reports.savedPdfUrl(row.id)}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="btn btn-ghost btn-sm"
                    >
                      <Download className="h-3.5 w-3.5" aria-hidden />
                      PDF
                    </a>
                    <button
                      type="button"
                      onClick={() => void remove(row)}
                      disabled={removing === row.id}
                      title="Delete this report"
                      aria-label={`Delete ${savedLabel(row)}`}
                      className="btn btn-ghost btn-sm text-muted hover:text-danger"
                    >
                      {removing === row.id ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                      ) : (
                        <Trash2 className="h-3.5 w-3.5" aria-hidden />
                      )}
                    </button>
                  </div>
                </li>
              ))}
          </ul>
        </div>
      )}

      {open && (
        <ReportViewer
          report={open.report}
          pdfHref={api.reports.savedPdfUrl(open.id)}
          onClose={() => setOpen(null)}
        />
      )}
    </div>
  );
}
