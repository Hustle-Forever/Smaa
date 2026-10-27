// Rebrand studio — one draggable bar on the scene's timeline (SpanBlock):
// an icon's block on the icon lane, a pattern's bar on its own lane.
//
//   the body       drag it to move it at the same length
//   either edge    drag it to trim (the other end stays): the start, or how
//                  long it lasts (`trims`)
//   snapping       a move or a start trim lands its anchor (an icon's
//                  settle) on the nearest snap point within reach when
//                  `snapOn` ("Snap to words"); Alt holds it off. A thin mark
//                  shows where it will land.
//
// The numbers are lib/rebrandSpan.ts (dragSpan, or the lane's own `drag` —
// a pattern's bar: dragPatternSpan): within its limits, never shorter than
// `limits.min`. While it is dragged the lane is told where it would land
// (`onLive`: a pattern's lane marks a clash with a full-screen one), and
// `times` shows it ("0:01.5 – 0:04.0"). A press that does not move is a
// click. The keyboard is the lane's own (`onKeyDown`), as each lane means
// its keys its own way.
//
// An icon's block marks when it has settled (`mark`, s after the block's
// start): the part before it (its entrance) is washed out and a line marks
// the time the studio says it shows at; the mark moves with the block.
//
// On a lane of several rows (`rows`: the icons' tracks) the body may also
// be dragged up or down: it follows the pointer row by row, one past the
// last being a new track (rebrandSpan.rowAt), and the row it is let go on
// goes with the commit (`onCommit`'s `row`, or `onRow` when only the row
// changed).

import { useRef, useState, type KeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";

import {
  dragSpan,
  rowAt,
  shownRowTop,
  type Span,
  type SpanDrag,
  type SpanLimits,
  type SpanSnap,
} from "../../../lib/rebrandSpan";

type Live = { kind: SpanDrag; x0: number; dx: number; y0: number; dy: number; alt: boolean };

/** The rows a block may be dragged between: its own, their pitch (px) and
 *  how many are drawn (it may go one past the last: a new one). */
export interface SpanRows {
  row: number;
  pitch: number;
  count: number;
  /** While dragged: the row it would land on (null once let go). */
  onLive?: (row: number | null) => void;
  /** Where it is drawn while it would land past the last row (a new one):
   *  inside the lane, which keeps its height mid-drag (null: a row below). */
  newTop?: number | null;
}

/** One drag's result: where the span lands, the snap point it took. */
export type SpanDragFn = (
  start: Span,
  kind: SpanDrag,
  dt: number,
  limits: SpanLimits,
  alt: boolean,
) => { span: Span; snapped: number | null };

const MOVED_PX = 3;

export function SpanBlock({
  span,
  seconds,
  width,
  limits,
  snap = null,
  snapOn = true,
  trims = ["start", "end"],
  top,
  height = 28,
  className = "",
  selected = false,
  rows = null,
  onCommit,
  onRow,
  onClick,
  onSelect,
  onKeyDown,
  onFocus,
  onDoubleClick,
  drag,
  onLive,
  times,
  tint,
  aria,
  titles,
  mark = null,
  children,
}: {
  span: Span;
  /** The lane's length (s) and width (px). */
  seconds: number;
  width: number;
  limits: SpanLimits;
  snap?: SpanSnap | null;
  snapOn?: boolean;
  trims?: readonly ("start" | "end")[];
  /** Its row's top (px) in the lane, and its height (px). */
  top: number;
  height?: number;
  className?: string;
  /** Picked: its trim handles show. */
  selected?: boolean;
  /** The rows it may be dragged between (null: it stays on its own). */
  rows?: SpanRows | null;
  /** Let go after a drag: the span it lands on, the snap point it took, and
   *  the row it was let go on when that is another (null: its own). */
  onCommit: (span: Span, kind: SpanDrag, snapped: number | null, row: number | null) => void;
  /** Let go on another row at the same time. */
  onRow?: (row: number) => void;
  /** Pressed without a drag. */
  onClick?: () => void;
  /** Grabbed (any part of it). */
  onSelect?: () => void;
  onKeyDown?: (e: KeyboardEvent<HTMLDivElement>) => void;
  onFocus?: () => void;
  onDoubleClick?: () => void;
  /** The lane's own drag (default: dragSpan with `snap`, off with Alt). */
  drag?: SpanDragFn;
  /** While dragged: where it would land (null once let go or dropped). */
  onLive?: (span: Span | null, kind: SpanDrag) => void;
  /** While dragged, a label of where it would land. */
  times?: (span: Span) => string;
  /** #rrggbb: its border, and a wash of it behind (a pattern's colour). */
  tint?: string | null;
  aria: {
    label: string;
    valuetext: string;
    now: number;
  };
  titles?: { start?: string; end?: string };
  /** An icon's settle, s after the block's start (null: no mark). */
  mark?: number | null;
  children: ReactNode;
}) {
  const [live, setLive] = useState<Live | null>(null);
  const liveRef = useRef<Live | null>(null);
  const total = Math.max(0.1, seconds);
  const secPerPx = width > 0 ? total / width : 0;

  const result = (l: Live) =>
    drag
      ? drag(span, l.kind, l.dx * secPerPx, limits, l.alt || !snapOn)
      : dragSpan(span, l.kind, l.dx * secPerPx, limits, snapOn && !l.alt && l.kind !== "end" ? snap : null);
  const shown = live && Math.abs(live.dx) >= MOVED_PX ? result(live) : null;
  const at = shown?.span ?? span;
  const pct = (s: number) => `${(Math.min(total, Math.max(0, s)) / total) * 100}%`;
  /** The row a drag of the body would land on (null: no rows, or a trim). */
  const rowOf = (l: Live | null): number | null =>
    l && rows && l.kind === "move" ? rowAt(rows.row, l.dy, rows.pitch, rows.count) : null;
  const liveRow = rowOf(live);
  // Its top while dragged to another row.
  const shownTop = rows ? shownRowTop(top, rows.row, liveRow, rows.pitch, rows.count, rows.newTop) : top;
  // The settle mark, px from the block's left edge (inside it).
  const markPx = mark != null && mark > 0 && width > 0 ? (Math.min(mark, at.to - at.from) / total) * width : null;

  const start = (e: ReactPointerEvent, kind: SpanDrag) => {
    if (e.button !== 0) return;
    e.stopPropagation();
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    const l = { kind, x0: e.clientX, dx: 0, y0: e.clientY, dy: 0, alt: e.altKey };
    liveRef.current = l;
    setLive(l);
    onSelect?.();
  };
  const move = (e: ReactPointerEvent) => {
    const l = liveRef.current;
    if (!l) return;
    const next = { ...l, dx: e.clientX - l.x0, dy: e.clientY - l.y0, alt: e.altKey };
    liveRef.current = next;
    setLive(next);
    if (onLive && Math.abs(next.dx) >= MOVED_PX) onLive(result(next).span, next.kind);
    if (rows?.onLive && next.kind === "move") rows.onLive(rowOf(next));
  };
  const end = (e: ReactPointerEvent) => {
    const l = liveRef.current;
    if (!l) return;
    liveRef.current = null;
    setLive(null);
    onLive?.(null, l.kind);
    rows?.onLive?.(null);
    e.stopPropagation();
    const row = rowOf(l);
    const newRow = rows && row != null && row !== rows.row ? row : null;
    if (Math.abs(l.dx) < MOVED_PX) {
      if (newRow != null) onRow?.(newRow);
      else if (l.kind === "move") onClick?.();
      return;
    }
    const got = result({ ...l, alt: e.altKey });
    if (Math.abs(got.span.from - span.from) < 1e-6 && Math.abs(got.span.to - span.to) < 1e-6) {
      if (newRow != null) onRow?.(newRow);
      return;
    }
    onCommit(got.span, l.kind, got.snapped, newRow);
  };
  const cancel = () => {
    const l = liveRef.current;
    liveRef.current = null;
    setLive(null);
    if (l) onLive?.(null, l.kind);
    rows?.onLive?.(null);
  };

  return (
    <>
      {shown?.snapped != null && (
        <span
          aria-hidden
          className="pointer-events-none absolute w-0.5 -translate-x-1/2 rounded bg-accent"
          style={{ left: pct(shown.snapped), top: shownTop - 3, height: height + 6 }}
        />
      )}
      <div
        role="slider"
        tabIndex={0}
        aria-label={aria.label}
        aria-valuemin={0}
        aria-valuemax={Math.round(total * 10) / 10}
        aria-valuenow={Math.round(aria.now * 10) / 10}
        aria-valuetext={aria.valuetext}
        data-span={`${at.from.toFixed(3)}-${at.to.toFixed(3)}`}
        onPointerDown={(e) => start(e, "move")}
        onPointerMove={move}
        onPointerUp={end}
        onPointerCancel={cancel}
        onKeyDown={onKeyDown}
        onFocus={onFocus}
        onDoubleClick={onDoubleClick}
        className={`group absolute flex cursor-grab touch-none select-none items-center gap-1 overflow-hidden rounded-md border-2 pl-0.5 pr-2 text-2xs font-medium text-fg shadow-soft focus:outline-none focus-visible:ring-2 focus-visible:ring-accent active:cursor-grabbing ${
          live ? "z-10 opacity-90" : ""
        } ${className}`}
        style={{
          left: pct(at.from),
          width: `max(1.75rem, calc(${pct(at.to)} - ${pct(at.from)}))`,
          top: shownTop,
          height,
          ...(tint ? { borderColor: tint, backgroundColor: `${tint}29` } : {}),
        }}
      >
        {markPx != null && (
          // Its 2 px line centred on the settle (the box starts inside the
          // block's 2 px border).
          <span
            aria-hidden
            data-mark={mark}
            className="pointer-events-none absolute inset-y-0 left-0 border-r-2 border-current bg-fg/10 opacity-60"
            style={{ width: Math.max(0, markPx - 1) }}
          />
        )}
        {markPx != null && <span aria-hidden className="shrink-0" style={{ width: Math.max(0, markPx - 2) }} />}
        {children}
        {trims.includes("start") && (
          <span
            role="presentation"
            title={titles?.start}
            data-edge="start"
            onPointerDown={(e) => start(e, "start")}
            onPointerMove={move}
            onPointerUp={end}
            onPointerCancel={cancel}
            className={`absolute inset-y-0 left-0 w-2 cursor-ew-resize bg-fg/10 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100 ${selected ? "opacity-100" : "opacity-0"}`}
          />
        )}
        {trims.includes("end") && (
          <span
            role="presentation"
            title={titles?.end}
            data-edge="end"
            onPointerDown={(e) => start(e, "end")}
            onPointerMove={move}
            onPointerUp={end}
            onPointerCancel={cancel}
            className={`absolute inset-y-0 right-0 w-2 cursor-ew-resize bg-fg/10 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100 ${selected ? "opacity-100" : "opacity-0"}`}
          />
        )}
      </div>
      {shown && times && (
        <span
          aria-hidden
          className="pointer-events-none absolute z-20 -translate-x-1/2 whitespace-nowrap rounded bg-black/80 px-1.5 py-0.5 font-mono text-[10px] tabular-nums text-white"
          // Above the bar: over the lane above it, or the ruler (it may go
          // past the lane's top; the timeline's box doesn't clip it there).
          style={{ left: pct((at.from + at.to) / 2), top: shownTop - 18 }}
        >
          {times(at)}
        </span>
      )}
    </>
  );
}
