// Rebrand studio: the scene's timeline under the stage (SceneTimeline): one
// ruler, one playhead and one clock (stageT) over every lane.
//
//   ruler     the scene's seconds, stuck at the top of the lanes' box. Click
//             or drag it to go to that moment: the picture shows the
//             playhead's moment. The footage's own jump cuts are marked
//             (amber), and so are the parts' starts when a version was made
//             in parts (Omni's ≤10 s at a time): a click inside a part picks
//             it for the stills too.
//   lanes     `children(width)`: every track of the scene, in the order
//             lib/rebrandTracks gives (the patterns' lanes, PatternLanes, then
//             the icons', IconLane), drawn in the ruler's own seconds. The box
//             is as tall as they are, up to the room the stage gives it; past
//             that they scroll under the ruler. `focus`: the lane a selection
//             brings into view (by its `data-lane` key), with the box's own
//             scroll, never the page's.
//   status    one line under the box, always the same height (nothing jumps):
//             what the lanes have to say (a drop put back, an icon off its
//             word) and, at its end, `aside` (Snap to words).
//
// The playhead, the footage's cuts, the end's line and the hatching after it
// (and a start's, before it) run through the ruler and every lane, the
// icons' too.
//
//   end       `grip` (every view of a scene, once the server has it): where
//             the scene ends: a dark grip on the ruler with a line through
//             the lanes, grey hatching after it (cut). Drag it (it snaps to
//             the automatic cut, a part's start or a pause between words
//             within 8 px; "7.50 s" while it moves), double-click it for
//             automatic; ←/→ one frame (Shift 10), Home / End the limits, 0
//             or Backspace automatic. The ruler then runs to the clip's real
//             end: an amber tick marks the automatic cut, amber hatching the
//             frames after it that jump to another shot (draggable into),
//             and the lanes are drawn inside the scene's end.
//   start     `startGrip` (a main scene's, with the end grip): where the
//             scene starts in the reel — the same grip on the ruler's left,
//             grey hatching before it (cut). Drag it (it snaps to the clip's
//             first frame, a part's start, a word's onset or where speech
//             starts, within 8 px), double-click it for no trim; ←/→ one
//             frame (Shift 10), Home, 0 or Backspace no trim, End the
//             latest. The two grips keep a second between them. The ruler
//             and the lanes stay in the clip's own seconds (only the reel
//             skips the start).
//
// Keyboard on the ruler: ←/→ 0.1 s (Shift: 1 s), Home / End.
//
// It replaces the part buttons (SceneStrip): time is the one axis now.

import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";

import {
  clampEnd,
  clampStart,
  dropEnd,
  dropStart,
  endBubble,
  endRange,
  endReset,
  FPS,
  keyEnd,
  keyStart,
  snapEnd,
  startRange,
} from "../../../lib/rebrandEnd";
import { scrollToShow, TRACK_PX } from "../../../lib/rebrandTracks";
import { clock, COPY } from "./studioCopy";

const STEP = 0.1;
/** How long the bubble stays after a key moves the grip. */
const BUBBLE_MS = 900;
/** A press that moves less than this (px) is a click, not a drag. */
const DRAG_PX = 3;
/** Two clicks this close (ms) are a double-click: automatic. */
const DOUBLE_MS = 400;

/** The scene's end on the timeline (30 fps frames). */
export interface EndGrip {
  /** Where the scene ends now: the user's end, else automatic. */
  frame: number;
  /** Set by hand (the grip is automatic otherwise). */
  user: number | null;
  /** The automatic cut (a Veo tail flash), or null. */
  auto: number | null;
  /** How far the clip really runs: the ruler's reach. */
  extent: number;
  /** Where a drag snaps. */
  snaps: readonly number[];
  /** The frames past the automatic cut are being got ready. */
  busy?: boolean;
  /** A new end (null: automatic); `nudge`: a key step (steps join). */
  onEnd: (frame: number | null, nudge: boolean) => void;
  /** While it is dragged, where (null: let go). */
  onDrag?: (frame: number | null) => void;
}

/** A main scene's start on the timeline (30 fps frames from the clip's
 *  first): the reel skips what is before it. */
export interface StartGrip {
  /** Where the scene starts now (0: the clip's first frame). */
  frame: number;
  /** Set by hand (null: no trim). */
  user: number | null;
  /** Where a drag snaps. */
  snaps: readonly number[];
  /** A new start (null: no trim); `nudge`: a key step (steps join). */
  onStart: (frame: number | null, nudge: boolean) => void;
}

/** One grip on the ruler, as useGrip handles it (the end's, the start's). */
interface GripIo {
  /** Where it is now (frames). */
  frame: number;
  /** Set by hand (null: its default). */
  user: number | null;
  snaps: readonly number[];
  /** A dragged frame, in range. */
  clamp: (frame: number) => number;
  /** What a drop stores. */
  drop: (frame: number) => number | null;
  /** What a double-click stores (the grip's default). */
  reset: number | null;
  /** Its keys: what one stores, or undefined (not one of its keys). */
  key: (key: string, shift: boolean) => { to: number | null; nudge: boolean } | undefined;
  set: (frame: number | null, nudge: boolean) => void;
  onDrag?: (frame: number | null) => void;
}

/** A grip's pointer and keys: a drag (snapping within 8 px; `live` while it
 *  moves, for the bubble), a click (a second one soon after is a
 *  double-click: `reset`) and its keys (the bubble shows a moment). */
function useGrip(io: GripIo | null, pxPerFrame: number) {
  // `live`: where a drag has it (the bubble shows it); `bubble`: a key just
  // moved it (the bubble shows a moment).
  const [live, setLive] = useState<number | null>(null);
  const [bubble, setBubble] = useState(false);
  const bubbleTimer = useRef<number | undefined>();
  useEffect(() => () => window.clearTimeout(bubbleTimer.current), []);
  const drag = useRef<{ x: number; from: number; moved: boolean; id: number } | null>(null);
  // The last click on the grip (a press let go where it was): a second one
  // soon after is a double-click. Counted here, not from the browser's own
  // dblclick, which a captured pointer doesn't always get.
  const lastClick = useRef(0);
  const down = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!io || e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, from: io.frame, moved: false, id: e.pointerId };
    e.currentTarget.focus({ preventScroll: true });
  };
  const move = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!io || !d || d.id !== e.pointerId) return;
    if (!d.moved && Math.abs(e.clientX - d.x) < DRAG_PX) return;
    d.moved = true;
    const raw = io.clamp(d.from + (e.clientX - d.x) / Math.max(1e-6, pxPerFrame));
    const at = snapEnd(raw, io.snaps, pxPerFrame);
    setLive(at);
    io.onDrag?.(at);
  };
  const up = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!io || !d || d.id !== e.pointerId) return;
    drag.current = null;
    const at = live;
    setLive(null);
    io.onDrag?.(null);
    // A click is not a drag: nothing moves, but a double-click is the
    // grip's default.
    if (!d.moved || at == null) {
      const now = Date.now();
      if (now - lastClick.current < DOUBLE_MS) {
        lastClick.current = 0;
        if (io.user !== io.reset) io.set(io.reset, false);
      } else {
        lastClick.current = now;
      }
      return;
    }
    lastClick.current = 0;
    const to = io.drop(at);
    if (to !== io.user) io.set(to, false);
  };
  const cancel = () => {
    if (!drag.current) return;
    drag.current = null;
    setLive(null);
    io?.onDrag?.(null);
  };
  const keyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!io) return;
    const k = io.key(e.key, e.shiftKey);
    if (!k) return;
    e.preventDefault();
    e.stopPropagation();
    if (k.to !== io.user) io.set(k.to, k.nudge);
    setBubble(true);
    window.clearTimeout(bubbleTimer.current);
    bubbleTimer.current = window.setTimeout(() => setBubble(false), BUBBLE_MS);
  };
  return {
    /** Where it shows: the drag's, else where it is. */
    at: live ?? io?.frame ?? 0,
    bubble: live != null || bubble,
    handlers: {
      onPointerDown: down,
      onPointerMove: move,
      onPointerUp: up,
      onPointerCancel: cancel,
      onLostPointerCapture: cancel,
      onKeyDown: keyDown,
    },
  };
}

export function SceneTimeline({
  duration,
  t,
  onSeek,
  parts = [],
  cuts = [],
  children,
  grip = null,
  startGrip = null,
  status = null,
  aside = null,
  focus = null,
}: {
  /** The scene's length (s). */
  duration: number;
  /** The playhead (s): the moment the picture shows. */
  t: number;
  /** Go to that moment. */
  onSeek: (t: number) => void;
  /** Where each part after the first starts (s). */
  parts?: number[];
  /** Where the footage itself jumps to another shot (s). */
  cuts?: number[];
  /** The lanes, drawn at the ruler's width (px). */
  children?: (width: number) => ReactNode;
  /** The scene's end: the ruler runs to the clip's real end, the lanes
   *  inside `duration` (the scene's seconds up to its end). */
  grip?: EndGrip | null;
  /** A main scene's start (shown with `grip` only): the reel skips the
   *  clip before it. */
  startGrip?: StartGrip | null;
  /** The status line's words (it keeps its height when there are none). */
  status?: ReactNode;
  /** At the status line's end (Snap to words). */
  aside?: ReactNode;
  /** The `data-lane` key of the lane to bring into view inside the box. */
  focus?: string | null;
}) {
  // What the ruler and the lanes both measure: the box's content (one width
  // for both; the scrollbar's gutter is kept whether it shows or not).
  const ref = useRef<HTMLDivElement | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(0);
  const dragging = useRef(false);
  // With the grip the ruler reaches the clip's real end; the lanes (and
  // every seek) stay inside the scene's own seconds.
  const lanes = Math.max(0.1, duration);
  const total = grip ? Math.max(lanes, grip.extent / FPS) : lanes;

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setWidth(el.getBoundingClientRect().width);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // A selection's lane, brought into view inside the box (its own scroll:
  // scrollIntoView would scroll the page too).
  useEffect(() => {
    const box = boxRef.current;
    const inner = ref.current;
    if (!focus || !box || !inner) return;
    const lane = [...box.querySelectorAll<HTMLElement>("[data-lane]")].find((el) => el.dataset.lane === focus);
    if (!lane) return;
    const top = lane.getBoundingClientRect().top - inner.getBoundingClientRect().top;
    const next = scrollToShow(top, lane.offsetHeight, box.scrollTop, box.clientHeight, TRACK_PX.ruler);
    if (Math.abs(next - box.scrollTop) >= 1) box.scrollTop = next;
  }, [focus]);

  const pct = (s: number) => `${(Math.min(total, Math.max(0, s)) / total) * 100}%`;
  const timeAt = (clientX: number) => {
    const r = ref.current?.getBoundingClientRect();
    if (!r || r.width <= 0) return 0;
    return Math.min(lanes, Math.max(0, ((clientX - r.left) / r.width) * total));
  };

  // A tick a second (every other one labelled when they crowd).
  const perSec = width / total;
  const every = perSec >= 36 ? 1 : perSec >= 18 ? 2 : 5;
  const ticks: number[] = [];
  for (let s = 0; s <= total + 1e-6; s += 1) ticks.push(s);

  const down = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    dragging.current = true;
    onSeek(timeAt(e.clientX));
  };
  const move = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (dragging.current) onSeek(timeAt(e.clientX));
  };
  const up = () => {
    dragging.current = false;
  };
  const key = (e: KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 1 : STEP;
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      onSeek(Math.min(lanes, Math.max(0, t + (e.key === "ArrowLeft" ? -step : step))));
    } else if (e.key === "Home") {
      e.preventDefault();
      onSeek(0);
    } else if (e.key === "End") {
      e.preventDefault();
      onSeek(Math.max(0, lanes - 1 / 30));
    }
  };

  // --- the end grip, and a main scene's start grip --------------------------------
  // Each keeps a second from the other: the end's range starts a second
  // after the start, the start's ends a second before the end.
  const starts = !!grip && !!startGrip;
  const facts = grip ? { auto: grip.auto, extent: grip.extent, start: startGrip?.frame ?? 0 } : null;
  const pxPerFrame = width / Math.max(1, total * FPS);
  const endGrip = useGrip(
    grip && facts
      ? {
          frame: grip.frame,
          user: grip.user,
          snaps: grip.snaps,
          clamp: (f) => clampEnd(f, facts),
          drop: (f) => dropEnd(f, facts),
          // Automatic (or, when a start leaves it too close, the nearest
          // end that fits).
          reset: endReset(facts),
          key: (key, shift) => {
            const k = keyEnd(key, shift, grip.user, facts);
            return k && { to: k.end, nudge: k.nudge };
          },
          set: grip.onEnd,
          onDrag: grip.onDrag,
        }
      : null,
    pxPerFrame,
  );
  const startG = useGrip(
    grip && startGrip
      ? {
          frame: startGrip.frame,
          user: startGrip.user,
          snaps: startGrip.snaps,
          clamp: (f) => clampStart(f, grip.frame),
          drop: (f) => dropStart(f, grip.frame),
          reset: null,
          key: (key, shift) => {
            const k = keyStart(key, shift, startGrip.user, grip.frame);
            return k && { to: k.start, nudge: k.nudge };
          },
          set: startGrip.onStart,
        }
      : null,
    pxPerFrame,
  );
  const gripAt = endGrip.at;
  const gripPct = pct(gripAt / FPS);
  const range = facts ? endRange(facts) : null;
  const startAt = starts ? startG.at : 0;
  const startPct = pct(startAt / FPS);
  const startMax = grip && startGrip ? startRange(grip.frame).max : 0;
  // The frames past the automatic cut (a Veo tail flash): amber.
  const flash = grip && grip.auto != null && grip.extent > grip.auto ? { from: grip.auto / FPS, to: grip.extent / FPS } : null;
  // The bubble sits beside the grip, on the side with room.
  const bubbleLeft = gripAt / FPS / total > 0.8;
  const startBubbleLeft = startAt / FPS / total > 0.8;
  const laneWidth = grip ? (width * lanes) / total : width;
  const hatch =
    "pointer-events-none absolute inset-y-0 bg-[repeating-linear-gradient(135deg,rgba(113,113,122,0.28)_0_4px,rgba(113,113,122,0.12)_4px_8px)]";
  // After the end (cut): its hatching and its line, once in the ruler and
  // once through the lanes under it — and before a start the same.
  const endMarks =
    grip && range ? (
      <>
        <span aria-hidden className={`${hatch} right-0`} style={{ left: gripPct }} />
        <span aria-hidden className="pointer-events-none absolute inset-y-0 w-0.5 -translate-x-1/2 bg-fg/60" style={{ left: gripPct }} />
        {starts && startAt > 0 && (
          <>
            <span aria-hidden data-start-cut className={`${hatch} left-0`} style={{ width: startPct }} />
            <span aria-hidden className="pointer-events-none absolute inset-y-0 w-0.5 -translate-x-1/2 bg-fg/60" style={{ left: startPct }} />
          </>
        )}
      </>
    ) : null;

  return (
    <div className="flex min-h-0 flex-col gap-1">
      <div
        ref={boxRef}
        data-timeline-box
        className="min-h-0 overflow-y-auto overflow-x-hidden rounded-lg border border-border bg-bg [scrollbar-gutter:stable]"
      >
        <div ref={ref} className="relative select-none">
          {/* the ruler: stuck at the top while the lanes scroll under it */}
          <div className="sticky top-0 z-20 border-b border-border/70 bg-bg" style={{ height: TRACK_PX.ruler }}>
            <div
              role="slider"
              tabIndex={0}
              aria-label={COPY.time.rulerLabel}
              aria-valuemin={0}
              aria-valuemax={Math.round(total * 10) / 10}
              aria-valuenow={Math.round(t * 10) / 10}
              aria-valuetext={clock(t)}
              title={COPY.time.rulerTitle}
              onPointerDown={down}
              onPointerMove={move}
              onPointerUp={up}
              onPointerCancel={up}
              onKeyDown={key}
              className="absolute inset-0 cursor-pointer touch-none focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/50"
            >
              {ticks.map((s) => (
                <span key={s} aria-hidden className="absolute bottom-0 w-px bg-border-strong" style={{ left: pct(s), height: s % every === 0 ? 7 : 4 }}>
                  {s % every === 0 && s > 0 && s < total - 0.2 && (
                    <span className="absolute bottom-1.5 left-0 -translate-x-1/2 font-mono text-[9px] tabular-nums text-muted">
                      {`${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`}
                    </span>
                  )}
                </span>
              ))}
              {parts.map((p, i) => (
                <span
                  key={`p${i}`}
                  title={COPY.time.partMark(i + 2)}
                  className="absolute inset-y-0 w-0.5 -translate-x-1/2 bg-fg/40"
                  style={{ left: pct(p) }}
                />
              ))}
              {cuts.map((c, i) => (
                <span
                  key={`c${i}`}
                  title={COPY.time.cutMark}
                  className="absolute top-0 h-0 w-0 -translate-x-1/2 border-x-[5px] border-t-[7px] border-x-transparent border-t-amber-500"
                  style={{ left: pct(c) }}
                />
              ))}
              {flash && (
                <>
                  <span
                    data-flash-zone
                    title={grip?.busy ? COPY.time.flashBusy : COPY.time.flashMark}
                    className={`absolute inset-y-0 bg-[repeating-linear-gradient(135deg,rgba(245,158,11,0.45)_0_4px,rgba(245,158,11,0.15)_4px_8px)] ${
                      grip?.busy ? "motion-safe:animate-pulse" : ""
                    }`}
                    style={{ left: pct(flash.from), width: `calc(${pct(flash.to)} - ${pct(flash.from)})` }}
                  />
                  <span
                    title={COPY.time.flashMark}
                    className="absolute inset-y-0 w-0.5 -translate-x-1/2 bg-amber-500"
                    style={{ left: pct(flash.from) }}
                  />
                </>
              )}
            </div>
            {endMarks}
            {/* the playhead: the moment the picture shows */}
            <span aria-hidden className="pointer-events-none absolute inset-y-0 w-0.5 -translate-x-1/2 bg-accent" style={{ left: pct(t) }}>
              <span className="absolute -top-px left-1/2 h-2 w-2 -translate-x-1/2 rotate-45 bg-accent" />
            </span>
            {grip && range && starts && (
              <>
                {/* the start: the end's grip on the ruler's left (at the
                    clip's first frame it sits whole inside the ruler) */}
                <div
                  role="slider"
                  tabIndex={0}
                  data-start-grip
                  aria-label={COPY.time.startLabel}
                  aria-valuemin={0}
                  aria-valuemax={startMax / FPS}
                  aria-valuenow={Math.round((startAt / FPS) * 100) / 100}
                  aria-valuetext={endBubble(startAt)}
                  title={COPY.time.startTitle}
                  {...startG.handlers}
                  className="absolute top-px z-10 h-[17px] w-2.5 -translate-x-1/2 cursor-ew-resize touch-none rounded-sm bg-fg shadow-soft focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  style={{ left: `max(${startPct}, 5px)` }}
                />
                {startG.bubble && (
                  <span
                    aria-hidden
                    className={`pointer-events-none absolute top-px z-10 whitespace-nowrap rounded bg-fg px-1.5 py-px font-mono text-[10px] tabular-nums text-bg ${
                      startBubbleLeft ? "-translate-x-[calc(100%+10px)]" : "translate-x-[10px]"
                    }`}
                    style={{ left: startPct }}
                  >
                    {endBubble(startAt)}
                  </span>
                )}
              </>
            )}
            {grip && range && (
              <>
                <div
                  role="slider"
                  tabIndex={0}
                  data-end-grip
                  aria-label={COPY.time.endLabel}
                  aria-valuemin={range.min / FPS}
                  aria-valuemax={range.max / FPS}
                  aria-valuenow={Math.round((gripAt / FPS) * 100) / 100}
                  aria-valuetext={endBubble(gripAt)}
                  title={COPY.time.endTitle}
                  {...endGrip.handlers}
                  className="absolute top-px z-10 h-[17px] w-2.5 -translate-x-1/2 cursor-ew-resize touch-none rounded-sm bg-fg shadow-soft focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  style={{ left: gripPct }}
                />
                {endGrip.bubble && (
                  <span
                    aria-hidden
                    className={`pointer-events-none absolute top-px z-10 whitespace-nowrap rounded bg-fg px-1.5 py-px font-mono text-[10px] tabular-nums text-bg ${
                      bubbleLeft ? "-translate-x-[calc(100%+10px)]" : "translate-x-[10px]"
                    }`}
                    style={{ left: gripPct }}
                  >
                    {endBubble(gripAt)}
                  </span>
                )}
              </>
            )}
          </div>
          <div role="group" aria-label={COPY.time.lanesLabel} style={grip ? { width: pct(lanes) } : undefined}>
            {width > 0 && children?.(laneWidth)}
          </div>
          {/* through every lane, the icons' too: the footage's cuts, the
              end (and a start), the playhead */}
          {cuts.map((c, i) => (
            <span
              key={`cl${i}`}
              aria-hidden
              className="pointer-events-none absolute bottom-0 w-px -translate-x-1/2 bg-amber-500/40"
              style={{ left: pct(c), top: TRACK_PX.ruler }}
            />
          ))}
          {endMarks && (
            <span aria-hidden className="pointer-events-none absolute inset-x-0 bottom-0" style={{ top: TRACK_PX.ruler }}>
              {endMarks}
            </span>
          )}
          <span
            aria-hidden
            className="pointer-events-none absolute bottom-0 w-0.5 -translate-x-1/2 bg-accent"
            style={{ left: pct(t), top: TRACK_PX.ruler }}
          />
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2 overflow-hidden px-0.5 text-2xs" style={{ height: TRACK_PX.status }}>
        <div className="min-w-0 flex-1 truncate">{status}</div>
        {aside}
      </div>
    </div>
  );
}
