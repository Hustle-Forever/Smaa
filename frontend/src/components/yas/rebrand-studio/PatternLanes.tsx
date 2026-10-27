// Rebrand studio: the patterns on the scene's timeline (made in house): a
// lane each, in the rows' order (the first is the back-most), inside
// SceneTimeline's ruler. Which lanes, in which order, is lib/rebrandTracks's:
// each has a key of its own (a kept layer and a draft pattern can share an
// element id), and a kept one is `locked`.
//
//   the bar      when it shows (SpanBlock): drag it to move it, an end to
//                trim it (at least half a second, inside the scene); a
//                double-click shows it for the whole scene. It is tinted in
//                the pattern's colour, and says "Full screen" when it fills
//                the frame. A hidden pattern's lane is hatched.
//   the wedges   how long it takes to come in (at its start) and to go out
//                (at its end): drag a wedge's inner edge to change it (0.2
//                to 3 s). None when it has no entrance, or holds to the cut.
//   snapping     an edge lands on the scene's ends, the playhead, the other
//                bars' edges and ramps (every other lane's, the kept ones
//                too), the parts, the words (once the icons have heard them),
//                else on the 0.1 s grid; Alt holds it off.
//   the rule     a full-screen pattern goes alone WHILE it shows: a drag
//                that would put two together turns red, and let go there it
//                is put back, with the reason on the timeline's status line
//                (`onNote`), as the server would refuse it.
//   locked       a layer of the version added to: drawn, never moved, never
//                picked (no "Show it for the whole scene" either).
//
// Picking: grabbing a bar or tabbing onto it only picks it (`onSelect` with
// "focus": the highlight); a click on it or on its lane picks it with
// "click" (the studio may open its row). The lanes themselves never change.
//
// Keyboard on a bar: ←/→ move it 0.1 s, Shift+←/→ its end, Alt+←/→ its
// start, Enter goes to where it is fully in. Each drag is one undo step; a
// burst of keys joins one (the shell's coalesce).

import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";

import type { HarnessElement } from "../../../lib/rebrandHarnessApi";
import { dragPatternSpan, type EdgeSnap, type Span, type SpanLimits } from "../../../lib/rebrandSpan";
import {
  EDGE_SEC,
  focusTime,
  RAMP_SEC,
  SPAN_MIN_SEC,
  type Ramps,
  type TimedPattern,
  type Timing,
} from "../../../lib/rebrandTiming";
import { TRACK_PX } from "../../../lib/rebrandTracks";
import { SpanBlock, type SpanDragFn } from "./SpanBlock";
import { COPY } from "./studioCopy";

const NUDGE = 0.1;
const SNAP_PX = 8;
const GRID = 0.1;
/** The bar's top in its lane. */
const BAR_TOP = (TRACK_PX.pattern - TRACK_PX.bar) / 2;

/** One pattern as its lane draws it. */
export interface LanePattern {
  /** The lane's own key (lib/rebrandTracks): unique on the timeline. */
  key: string;
  el: HarnessElement;
  name: string;
  /** Its thumbnail (the library's preview). */
  thumb?: string | null;
  /** The colour the draft paints it (#rrggbb; null: not known yet). */
  colour: string | null;
  /** It fills the whole frame. */
  full: boolean;
  /** When it shows on this scene (null: it starts after the scene ends). */
  timing: Timing | null;
  /** A layer of the version added to (or any of a version's own): drawn,
   *  never moved. */
  locked?: boolean;
}

export function PatternLanes({
  width,
  duration,
  patterns,
  selected,
  onSelect,
  onSpan,
  onRamps,
  onGoTo,
  points = [],
  clash,
  onNote,
  readOnly = false,
}: {
  /** The ruler's width (px). */
  width: number;
  duration: number;
  patterns: LanePattern[];
  /** The draft pattern picked (element id): only its own lane lights up. */
  selected: string | null;
  /** Picked: grabbed or focused ("focus"), or clicked ("click"). */
  onSelect: (id: string, how: "focus" | "click") => void;
  /** A bar let go (moved or trimmed); `key`: from the keyboard. */
  onSpan: (id: string, span: Span, key: boolean) => void;
  onRamps: (id: string, ramps: Ramps) => void;
  /** Show that moment (a pattern fully in). */
  onGoTo: (t: number) => void;
  /** More places an edge lands on (s): the playhead, the parts, the words. */
  points?: number[];
  /** The pattern `id` over `span` would show together with (either filling
   *  the frame), or null. */
  clash: (id: string, span: readonly [number, number | null]) => TimedPattern | null;
  /** What the lanes have to say (a drop put back, a clash while dragging),
   *  for the timeline's status line; null: nothing. */
  onNote?: (note: string | null) => void;
  /** A version's own lanes (Original, a version): each bar is its name
   *  alone, never "kept" (that is a layer of the version added to). */
  readOnly?: boolean;
}) {
  const total = Math.max(0.1, duration);
  const secPerPx = total / Math.max(1, width);
  const pct = (s: number) => `${(Math.min(total, Math.max(0, s)) / total) * 100}%`;
  // While a bar is dragged: the pattern it would show together with.
  const [live, setLive] = useState<{ key: string; with: TimedPattern | null; full: string | null } | null>(null);
  // A drop put back (a clash), said on the status line for a few seconds.
  const [refused, setRefused] = useState<string | null>(null);
  useEffect(() => {
    if (!refused) return;
    const h = window.setTimeout(() => setRefused(null), 6000);
    return () => window.clearTimeout(h);
  }, [refused]);
  // A ramp being dragged: its length so far.
  const [ramp, setRamp] = useState<{ key: string; which: "in" | "out"; sec: number } | null>(null);

  // The status line hears what the lanes have to say (and that it is over).
  const note = refused ?? (live?.with ? COPY.time.clashLive(live.full ?? live.with.name) : null);
  const onNoteRef = useRef(onNote);
  onNoteRef.current = onNote;
  useEffect(() => {
    onNoteRef.current?.(note);
  }, [note]);
  useEffect(() => () => onNoteRef.current?.(null), []);

  const asSpan = (s: Span): readonly [number, number | null] => [s.from, s.to >= total - EDGE_SEC ? null : s.to];
  const limits: SpanLimits = { min: SPAN_MIN_SEC, max: Infinity, lo: 0, hi: total };

  /** A drop, unless it would put a full-screen pattern with another. */
  const commit = (p: LanePattern, span: Span, key: boolean) => {
    if (p.locked) return;
    const other = clash(p.el.id, asSpan(span));
    if (other) {
      setRefused(p.full ? COPY.time.refused(p.name) : COPY.time.refusedOther(other.name));
      return;
    }
    setRefused(null);
    onSpan(p.el.id, span, key);
  };

  return (
    <div>
      {patterns.map((p) => {
        const tm = p.timing;
        const id = p.el.id;
        const lane = p.key;
        const hidden = !p.el.enabled;
        // Every other lane's edges and ramps (the kept ones too): where an
        // edge lands.
        const edges = patterns
          .filter((o) => o.key !== lane && o.timing)
          .flatMap((o) => [o.timing!.t_in, o.timing!.t_full, o.timing!.t_hold_end, o.timing!.t_out]);
        const snap: EdgeSnap = { points: [0, total, ...edges, ...points], within: SNAP_PX * secPerPx, grid: GRID };
        const drag: SpanDragFn = (start, kind, dt, lim, off) => dragPatternSpan(start, kind, dt, lim, off ? null : snap);
        const red = live?.key === lane && !!live.with;
        const inSec = ramp?.key === lane && ramp.which === "in" ? ramp.sec : tm ? tm.t_full - tm.t_in : 0;
        const outSec = ramp?.key === lane && ramp.which === "out" ? ramp.sec : tm ? tm.t_out - tm.t_hold_end : 0;
        const spanSec = tm ? Math.max(1e-6, tm.t_out - tm.t_in) : 1;
        const inWedge = tm && tm.entrance !== "none" && inSec > 0;
        const outWedge = tm && tm.exit !== "none" && outSec > 0;
        const tint = p.colour ?? "#7f2bf2";
        const picked = !p.locked && selected === id;

        /** A wedge's inner edge dragged: its ramp's length. */
        const rampDown = (e: ReactPointerEvent<HTMLSpanElement>, which: "in" | "out") => {
          if (e.button !== 0 || !tm || p.locked) return;
          e.stopPropagation();
          e.preventDefault();
          (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
          const x0 = e.clientX;
          const sec0 = which === "in" ? tm.t_full - tm.t_in : tm.t_out - tm.t_hold_end;
          const at = (clientX: number) => {
            const d = (clientX - x0) * secPerPx * (which === "in" ? 1 : -1);
            return Math.round(Math.min(RAMP_SEC[1], Math.max(RAMP_SEC[0], sec0 + d)) * 10) / 10;
          };
          const el = e.currentTarget as HTMLElement;
          const move = (ev: PointerEvent) => setRamp({ key: lane, which, sec: at(ev.clientX) });
          const up = (ev: PointerEvent) => {
            el.removeEventListener("pointermove", move);
            el.removeEventListener("pointerup", up);
            el.removeEventListener("pointercancel", up);
            setRamp(null);
            const sec = at(ev.clientX);
            if (Math.abs(sec - sec0) >= 0.05) onRamps(id, which === "in" ? { entranceSec: sec } : { exitSec: sec });
          };
          el.addEventListener("pointermove", move);
          el.addEventListener("pointerup", up);
          el.addEventListener("pointercancel", up);
        };

        const span: Span | null = tm ? { from: tm.t_in, to: tm.t_out } : null;
        // A short bar keeps its name: the thumbnail and the mark go first.
        const barPx = span ? ((span.to - span.from) / total) * width : 0;
        return (
          <div
            key={lane}
            data-lane={lane}
            className={`relative border-t border-border/70 ${
              hidden ? "bg-[repeating-linear-gradient(135deg,rgba(113,113,122,0.16)_0_4px,transparent_4px_8px)]" : ""
            }`}
            style={{ height: TRACK_PX.pattern }}
            onClick={(e) => {
              if (!p.locked && e.target === e.currentTarget) onSelect(id, "click");
            }}
          >
            {!span ? (
              p.locked ? (
                <span className="absolute inset-y-0 left-2 right-2 flex items-center truncate text-2xs text-muted">
                  {COPY.time.notShown(p.name)}
                </span>
              ) : (
                <button
                  type="button"
                  onClick={() => commit(p, { from: 0, to: total }, false)}
                  className="absolute left-1 right-1 truncate rounded-md border border-dashed border-amber-500/80 px-2 text-left text-2xs text-amber-800"
                  style={{ top: BAR_TOP, height: TRACK_PX.bar }}
                  title={COPY.time.wholeSceneTitle}
                >
                  {COPY.time.notShown(p.name)}
                </button>
              )
            ) : p.locked ? (
              <div
                className="absolute flex items-center gap-1 overflow-hidden rounded-md border-2 border-dashed px-2 text-2xs text-muted"
                style={{
                  left: pct(span.from),
                  width: `calc(${pct(span.to)} - ${pct(span.from)})`,
                  top: BAR_TOP,
                  height: TRACK_PX.bar,
                  borderColor: tint,
                }}
                title={readOnly ? p.name : `${p.name} · ${COPY.time.keptMark}`}
              >
                <span className="truncate">{readOnly ? p.name : `${p.name} · ${COPY.time.keptMark}`}</span>
              </div>
            ) : (
              <SpanBlock
                span={span}
                seconds={total}
                width={width}
                limits={limits}
                drag={drag}
                top={BAR_TOP}
                height={TRACK_PX.bar}
                tint={red ? "#dc2626" : tint}
                selected={picked}
                onSelect={() => onSelect(id, "focus")}
                onFocus={() => onSelect(id, "focus")}
                onClick={() => {
                  onSelect(id, "click");
                  if (tm) onGoTo(focusTime(tm));
                }}
                onDoubleClick={() => commit(p, { from: 0, to: total }, false)}
                onLive={(s) => setLive(s ? { key: lane, with: clash(id, asSpan(s)), full: p.full ? p.name : null } : null)}
                times={(s) => COPY.time.live(s.from, s.to)}
                onCommit={(s) => commit(p, s, false)}
                onKeyDown={(e) => {
                  if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
                    e.preventDefault();
                    const d = (e.key === "ArrowLeft" ? -1 : 1) * NUDGE;
                    const len = span.to - span.from;
                    let next: Span;
                    if (e.shiftKey) next = { from: span.from, to: Math.min(total, Math.max(span.from + SPAN_MIN_SEC, span.to + d)) };
                    else if (e.altKey) next = { from: Math.max(0, Math.min(span.to - SPAN_MIN_SEC, span.from + d)), to: span.to };
                    else {
                      const from = Math.min(Math.max(0, span.from + d), total - len);
                      next = { from, to: from + len };
                    }
                    commit(p, { from: Math.round(next.from * 100) / 100, to: Math.round(next.to * 100) / 100 }, true);
                  } else if (e.key === "Enter" && tm) {
                    e.preventDefault();
                    onGoTo(focusTime(tm));
                  }
                }}
                aria={{
                  label: COPY.time.barAria(p.name),
                  valuetext: COPY.time.barValue(span.from, span.to),
                  now: span.from,
                }}
                titles={{ start: COPY.time.startHandle(p.name), end: COPY.time.endHandle(p.name) }}
                className={`${hidden ? "border-dashed opacity-60" : ""} ${picked ? "ring-2 ring-accent/60" : ""}`}
              >
                {inWedge && (
                  <span
                    aria-hidden
                    className="pointer-events-none absolute inset-y-0 left-0"
                    style={{
                      width: `${Math.min(100, (inSec / spanSec) * 100)}%`,
                      background: `linear-gradient(to right, transparent, ${tint}66)`,
                      clipPath: "polygon(0 100%, 100% 0, 100% 100%)",
                    }}
                  />
                )}
                {inWedge && (
                  <span
                    role="presentation"
                    title={COPY.time.inHandle(p.name)}
                    data-ramp="in"
                    onPointerDown={(e) => rampDown(e, "in")}
                    className="absolute inset-y-0 z-10 w-2 -translate-x-1/2 cursor-col-resize"
                    style={{ left: `${Math.min(100, (inSec / spanSec) * 100)}%` }}
                  >
                    <span aria-hidden className="absolute inset-y-1 left-1/2 w-px -translate-x-1/2 bg-fg/50" />
                  </span>
                )}
                {p.thumb && barPx >= 90 && (
                  <img src={p.thumb} alt="" draggable={false} className="relative h-4 w-6 shrink-0 rounded-sm bg-fg object-contain" />
                )}
                <span className="relative min-w-0 truncate">{p.name}</span>
                {p.full && barPx >= 150 && (
                  <span className="relative shrink-0 rounded bg-fg/10 px-1 text-[9px] font-semibold uppercase tracking-wide text-fg/70">
                    {COPY.time.fullMark}
                  </span>
                )}
                {outWedge && (
                  <span
                    aria-hidden
                    className="pointer-events-none absolute inset-y-0 right-0"
                    style={{
                      width: `${Math.min(100, (outSec / spanSec) * 100)}%`,
                      background: `linear-gradient(to left, transparent, ${tint}66)`,
                      clipPath: "polygon(0 0, 100% 100%, 0 100%)",
                    }}
                  />
                )}
                {outWedge && (
                  <span
                    role="presentation"
                    title={COPY.time.outHandle(p.name)}
                    data-ramp="out"
                    onPointerDown={(e) => rampDown(e, "out")}
                    className="absolute inset-y-0 z-10 w-2 translate-x-1/2 cursor-col-resize"
                    style={{ right: `${Math.min(100, (outSec / spanSec) * 100)}%` }}
                  >
                    <span aria-hidden className="absolute inset-y-1 left-1/2 w-px -translate-x-1/2 bg-fg/50" />
                  </span>
                )}
              </SpanBlock>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** A thin row in a pattern lane's place: "Add a pattern" on the draft (it
 *  has none of its own), or, read-only, that a version has none. */
export function PatternAddRow({
  laneKey,
  onAdd,
  disabled = false,
}: {
  laneKey: string;
  /** null: read-only (a version, the original). */
  onAdd: ((trigger: HTMLElement) => void) | null;
  disabled?: boolean;
}) {
  return (
    <div data-lane={laneKey} className="relative flex items-center border-t border-border/70 px-2" style={{ height: TRACK_PX.add }}>
      {onAdd ? (
        <button
          type="button"
          disabled={disabled}
          onClick={(e) => onAdd(e.currentTarget)}
          title={COPY.time.addPatternTitle}
          className="rounded text-2xs font-medium text-accent-hover hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:opacity-50 disabled:hover:no-underline"
        >
          + {COPY.time.addPattern}
        </button>
      ) : (
        <span className="text-2xs text-muted">{COPY.time.noPatterns}</span>
      )}
    </div>
  );
}
