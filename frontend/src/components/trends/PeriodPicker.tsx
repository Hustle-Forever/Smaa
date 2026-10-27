/**
 * Which week. Which half. Which month. Nothing in between.
 *
 * WHAT THIS REPLACED. A calendar where both ends were dragged out by hand, so
 * a report could cover 11 days, or Tuesday to Tuesday, or half of one week and
 * half of the next. That is an unbounded set of documents, no two of which can
 * be laid beside each other ("posts rose 20%" over what?), and every one of
 * them costs two model passes. The unit of selection is now the period, not
 * the day: clicking any day in a week selects that whole week.
 *
 * AND ONLY THE FINISHED ONES. A period that is still collecting would produce a
 * report about part of itself with nothing on the page to say so. So the
 * current week is greyed out until Sunday is over, and today is never
 * selectable. The same rule makes every report final: a finished period cannot
 * gain another day, which is why one that already exists is opened rather than
 * rebuilt.
 *
 * THE MENU COMES FROM THE SERVER (`/api/reports/periods`), which is the same
 * module the generate route validates against (`services/report_periods`). A
 * picker that worked out the periods for itself would eventually disagree with
 * the route about what a week is, and the reader would see a refusal with no
 * explanation. This file draws what it is given and computes no dates of its
 * own.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { CalendarDays, Check, ChevronLeft, ChevronRight } from "lucide-react";

import type { ReportPeriod } from "../../lib/types";

const MS_DAY = 86_400_000;

const MONTHS_LONG = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/** Monday first, because a week here runs Monday to Sunday. */
const WEEKDAYS = ["M", "T", "W", "T", "F", "S", "S"];

function parseDay(iso: string): Date {
  return new Date(`${iso}T00:00:00Z`);
}

function isoOf(d: Date): string {
  return d.toISOString().slice(0, 10);
}

export function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

/** The period a day falls in, out of the ones on offer. Undefined means this
 *  day cannot be asked about: outside our coverage, or in a period that has
 *  not finished. */
function periodOn(periods: ReportPeriod[], day: string): ReportPeriod | undefined {
  return periods.find((p) => day >= p.from && day <= p.to);
}

type Props = {
  periods: ReportPeriod[];
  selected: ReportPeriod | null;
  loading?: boolean;
  disabled?: boolean;
  onChange: (period: ReportPeriod) => void;
};

export function PeriodPicker({
  periods,
  selected,
  loading,
  disabled,
  onChange,
}: Props) {
  const [open, setOpen] = useState(false);
  const [hover, setHover] = useState<ReportPeriod | null>(null);
  const wrap = useRef<HTMLDivElement | null>(null);

  const anchor = selected?.to || periods[0]?.to || todayISO();
  const [view, setView] = useState(() => anchor.slice(0, 7));

  useEffect(() => {
    if (open) {
      setView(anchor.slice(0, 7));
      setHover(null);
    }
  }, [open, anchor]);

  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const oldest = periods.length ? periods[periods.length - 1].from : null;
  const newest = periods.length ? periods[0].to : todayISO();
  const [vYear, vMonth] = view.split("-").map(Number);

  const cells = useMemo(() => {
    const first = new Date(Date.UTC(vYear, vMonth - 1, 1));
    const lead = (first.getUTCDay() + 6) % 7; // Sunday is 0, and goes last
    const length = new Date(Date.UTC(vYear, vMonth, 0)).getUTCDate();
    const out: Array<string | null> = Array(lead).fill(null);
    for (let i = 1; i <= length; i += 1) {
      out.push(isoOf(new Date(Date.UTC(vYear, vMonth - 1, i))));
    }
    return out;
  }, [vYear, vMonth]);

  const canGoBack = !oldest || `${view}-01` > oldest;
  const canGoOn = view < newest.slice(0, 7);
  const painted = hover || selected;

  function step(by: number) {
    const d = new Date(Date.UTC(vYear, vMonth - 1 + by, 1));
    setView(`${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`);
  }

  function pick(period: ReportPeriod) {
    onChange(period);
    setOpen(false);
  }

  return (
    <div ref={wrap} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={disabled || loading || !periods.length}
        aria-expanded={open}
        title="Choose which one"
        className={[
          "flex h-[3.125rem] min-w-[12rem] items-center gap-2 rounded-lg border px-3",
          "text-left transition-colors disabled:opacity-50",
          open ? "border-accent bg-bg" : "border-border bg-bg hover:border-accent/50",
        ].join(" ")}
      >
        <CalendarDays className="h-4 w-4 shrink-0 text-muted" aria-hidden />
        <span className="min-w-0">
          <span className="block truncate text-xs font-medium leading-none text-fg">
            {loading
              ? "Loading…"
              : selected
                ? selected.label
                : "Nothing to report on yet"}
          </span>
          <span className="mt-1 block font-mono text-3xs leading-none text-muted">
            {selected
              ? `${selected.days} days · ${selected.records.toLocaleString()} records`
              : "no finished periods with data"}
          </span>
        </span>
      </button>

      {open && (
        <div className="absolute left-0 top-full z-30 mt-2 w-[19rem] rounded-xl border border-border bg-bg p-3 shadow-lg">
          <div className="mb-2 flex items-center justify-between">
            <button
              type="button"
              onClick={() => step(-1)}
              disabled={!canGoBack}
              aria-label="Previous month"
              className="rounded-md p-1 text-muted transition-colors hover:bg-bg-muted hover:text-fg disabled:opacity-30 disabled:hover:bg-transparent"
            >
              <ChevronLeft className="h-4 w-4" aria-hidden />
            </button>
            <p className="text-xs font-semibold text-fg">
              {MONTHS_LONG[vMonth - 1]} {vYear}
            </p>
            <button
              type="button"
              onClick={() => step(1)}
              disabled={!canGoOn}
              aria-label="Next month"
              className="rounded-md p-1 text-muted transition-colors hover:bg-bg-muted hover:text-fg disabled:opacity-30 disabled:hover:bg-transparent"
            >
              <ChevronRight className="h-4 w-4" aria-hidden />
            </button>
          </div>

          <div
            className="grid grid-cols-7 gap-0.5"
            onMouseLeave={() => setHover(null)}
          >
            {WEEKDAYS.map((d, i) => (
              <div
                key={i}
                className="pb-1 text-center font-mono text-3xs text-muted"
                aria-hidden
              >
                {d}
              </div>
            ))}

            {cells.map((day, i) => {
              if (!day) return <div key={`b${i}`} />;
              const period = periodOn(periods, day);
              const lit = !!painted && day >= painted.from && day <= painted.to;
              const chosen = !!selected && day >= selected.from && day <= selected.to;
              return (
                <button
                  key={day}
                  type="button"
                  onClick={() => period && pick(period)}
                  onMouseEnter={() => setHover(period || null)}
                  disabled={!period}
                  title={
                    period
                      ? `${period.label} · ${period.records.toLocaleString()} records`
                      : "Not a finished period we hold data for"
                  }
                  className={[
                    "relative flex h-8 items-center justify-center rounded-md",
                    "text-2xs tabular-nums transition-colors",
                    !period
                      ? "cursor-not-allowed text-muted/30"
                      : lit
                        ? chosen && !hover
                          ? "bg-accent font-semibold text-white"
                          : "bg-accent/25 font-medium text-fg"
                        : "text-fg hover:bg-bg-muted",
                  ].join(" ")}
                >
                  {parseDay(day).getUTCDate()}
                  {/* Already built: the whole period opens with no wait and no
                      spend, which is worth seeing before clicking. */}
                  {period?.report_id != null && day === period.to && (
                    <span
                      aria-hidden
                      className={`absolute bottom-0.5 h-1 w-1 rounded-full ${
                        lit && chosen && !hover ? "bg-white" : "bg-accent"
                      }`}
                    />
                  )}
                </button>
              );
            })}
          </div>

          <div className="mt-3 border-t border-border pt-2.5">
            {painted ? (
              <>
                <p className="text-2xs font-semibold text-fg">{painted.label}</p>
                <p className="mt-0.5 font-mono text-3xs text-muted">
                  {painted.days} days · {painted.records.toLocaleString()} records
                </p>
                {painted.report_id != null && (
                  <p className="mt-1 flex items-center gap-1 text-3xs text-accent">
                    <Check className="h-3 w-3" aria-hidden />
                    Already built, opens instantly
                  </p>
                )}
              </>
            ) : (
              <p className="text-3xs leading-snug text-muted">
                Pick any day and you get the whole period it belongs to. Greyed
                out means it has not finished yet, or we were not collecting.
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** How far apart two days are, for the one place the panel needs it. */
export function daysBetween(from: string, to: string): number {
  return Math.round((parseDay(to).getTime() - parseDay(from).getTime()) / MS_DAY);
}
