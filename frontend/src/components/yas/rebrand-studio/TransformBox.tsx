// Rebrand studio — the stage handles (TransformBox): one box on the picture
// a hand can move, size and — a pattern — turn. An icon and a pattern moved
// by hand share it (C7).
//
//   the body      drag it to move it: its edges and centre snap (8 px) to
//                 `snaps` (the frame's edges, the safe margin, the middle,
//                 the icon spots; a pattern's own automatic spot), with a
//                 guide line while they do
//   a corner      drag it to size it, always uniformly (nothing stretches),
//                 about the opposite corner (Alt: about the centre), inside
//                 `scaleRange` × the box it started as — a turned box too
//   the turn      (`rotate`) a round purple button just outside the turned
//   button        box (rebrandTransform.turnButtonSpot: below its middle,
//                 else above, right, left — always wholly on the canvas,
//                 clear of the stage's chips and buttons, `TurnAvoid`, and
//                 44 px or more from the pivot). It is NOT inside the turned
//                 box, so it never turns with it or leaves the canvas. Drag
//                 it to turn the box about its centre: it clicks onto
//                 multiples of 45° within 4°, Shift gives 15° steps, and an
//                 angle pill beside it says where it is (purple on a stop).
//                 It stays where it was grabbed until let go. ← / → on it
//                 turn 1° (Shift 15°)
//   "pan"         a full-screen pattern has no box: a drag anywhere on the
//                 picture pans it, the pill's − / + zoom it (never below
//                 `scaleRange`, the least that still fills the frame), the
//                 turn button, bottom-right, turns it
//   the keyboard  arrows move it 1 % (Shift 5 %), + / − size it 5 %, [ / ]
//                 turn it exactly 1° (Shift 15°; no pull onto 45°), 0 back
//                 to automatic (`onReset`), Esc drops a drag in progress
//
// Every gesture gives ONE Transform against the box it started from —
// {dx, dy (frame fractions), scale (× about its centre), rotate (degrees
// added)} — live while it moves (`onChange(t, {final: false})`) and once
// when it is let go or a key is pressed (`final: true`). An icon is kept
// inside the frame (`keep` "inside"); a pattern only keeps part of itself
// on it ("partly"). The outline says what the speaker does to it where it
// is now (`toneOf`): green clear, amber partly behind the speaker, red over
// the face.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { Minus, Plus, RotateCw } from "lucide-react";

import {
  angleAbout,
  between,
  insideFrame,
  maxScaleInside,
  onTurnStop,
  partlyInside,
  scaledAbout,
  sizedByCorner,
  snapAxis,
  snapTurn,
  turnButtonSpot,
  TURN_PILL,
  type Box,
  type Rect,
  type Transform,
  type TurnSpot,
} from "../../../lib/rebrandTransform";
import { COPY } from "./studioCopy";

export type BoxTone = "clear" | "near" | "over" | "plain";

/** What the turn button keeps clear of on the canvas (canvas px): the
 *  stage's chips and buttons, as StudioStage measures them. */
export const TurnAvoid = createContext<readonly Rect[]>([]);

/** How long the angle pill stays after a key turns it (ms). */
const KEY_PILL_MS = 1200;

const SNAP_PX = 8;
const NUDGE = 0.01;
const NUDGE_BIG = 0.05;
const SIZE_STEP = 1.05;
const TURN_STEP = 1;
const TURN_BIG = 15;
const MOVED_PX = 2;

const TONE_CLASS: Record<BoxTone, string> = {
  clear: "border-emerald-400",
  near: "border-amber-400",
  over: "border-red-500",
  plain: "border-white",
};

type Gesture = {
  kind: "move" | "size" | "turn";
  corner: number;
  x0: number;
  y0: number;
  rect: DOMRect;
  alt: boolean;
  moved: boolean;
  /** The pointer's angle about the centre when the turn button was
   *  grabbed (turn). */
  angle0: number;
};

interface Live {
  box: Box;
  rot: number;
  /** A turn that clicked onto a stop (a multiple of 45°, or Shift's 15°). */
  snap?: boolean;
}

export function TransformBox({
  box,
  frame,
  scaleRange = [0.5, 3],
  snaps = null,
  tone = "clear",
  toneOf,
  label,
  hint,
  name,
  mode = "box",
  rotate = false,
  rotation = 0,
  keep = "inside",
  onReset,
  zoomLabels,
  zoom: zoomNow = 1,
  onChange,
  onCancel,
}: {
  /** Where it starts (frame fractions; unturned). */
  box: Box;
  /** The frame (px): a size is uniform, and a turn is made, in its pixels. */
  frame: { w: number; h: number };
  /** × the box it started as. */
  scaleRange?: readonly [number, number];
  /** Where its edges and centre snap to (frame fractions). */
  snaps?: { x: readonly number[]; y: readonly number[] } | null;
  /** The outline at rest. */
  tone?: BoxTone;
  /** The outline for where a gesture has it now. */
  toneOf?: (box: Box) => BoxTone;
  /** What it is ("Coding"), and how to move it, for a screen reader. */
  label: string;
  hint?: string;
  /** Its name, for the turn button ("Turn Structure"). */
  name?: string;
  /** "box" (move and size); "pan": a full-screen pattern (drag, zoom). */
  mode?: "box" | "pan";
  /** It turns (a pattern): the turn button. An icon never turns. */
  rotate?: boolean;
  /** Its turn now (degrees, clockwise): the box is drawn turned. */
  rotation?: number;
  /** "inside": always wholly in the frame (an icon); "partly": at least a
   *  part of it on the frame (a pattern). */
  keep?: "inside" | "partly";
  /** "0": back to automatic. */
  onReset?: () => void;
  /** The pan pill's buttons, for a screen reader. */
  zoomLabels?: { in: string; out: string };
  /** Its size where the box starts (× as placed): what the pan pill says. */
  zoom?: number;
  /** `key`: a key (or the zoom pill) gave it — edits of that kind join. */
  onChange: (t: Transform, opts: { final: boolean; key?: boolean }) => void;
  onCancel?: () => void;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [live, setLive] = useState<Live | null>(null);
  const [guides, setGuides] = useState<{ x: number | null; y: number | null }>({ x: null, y: null });
  const gesture = useRef<Gesture | null>(null);
  const liveRef = useRef<Live | null>(null);
  const shown = live?.box ?? box;
  const shownRot = live?.rot ?? rotation;
  const shownTone = live && toneOf ? toneOf(live.box) : tone;
  const pan = mode === "pan";

  // A new starting box (the plan caught up, or another icon): nothing live.
  const boxKey = `${box.x}|${box.y}|${box.w}|${box.h}|${rotation}`;
  useEffect(() => {
    if (!gesture.current) {
      setLive(null);
      liveRef.current = null;
    }
  }, [boxKey]);

  // --- the turn button: where it goes on the canvas ---------------------------
  // The canvas' size (this layer covers it), and the pan pill's place: the
  // turn button keeps clear of it as of the stage's chips.
  const [canvas, setCanvas] = useState<{ w: number; h: number } | null>(null);
  const panPillRef = useRef<HTMLSpanElement | null>(null);
  const [panPill, setPanPill] = useState<Rect | null>(null);
  useLayoutEffect(() => {
    const el = rootRef.current;
    if (!el || !rotate) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      setCanvas((c) => (c && c.w === r.width && c.h === r.height ? c : { w: r.width, h: r.height }));
      const p = panPillRef.current?.getBoundingClientRect();
      const next = p && p.width > 0 ? { x: p.left - r.left, y: p.top - r.top, w: p.width, h: p.height } : null;
      setPanPill((was) =>
        was && next && was.x === next.x && was.y === next.y && was.w === next.w && was.h === next.h ? was : next,
      );
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [rotate, pan]);
  const stageAvoid = useContext(TurnAvoid);
  const avoid = useMemo(() => (pan && panPill ? [...stageAvoid, panPill] : stageAvoid), [stageAvoid, pan, panPill]);
  const liveSpot = useMemo(
    () => (rotate && canvas && canvas.w > 0 && canvas.h > 0 ? turnButtonSpot(shown, shownRot, frame, canvas, avoid, { pan }) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rotate, canvas, avoid, pan, shown.x, shown.y, shown.w, shown.h, shownRot, frame.w, frame.h],
  );
  // Turning, it stays where it was grabbed (the box's outline turns under it).
  const [turning, setTurning] = useState<TurnSpot | null>(null);
  const spot = turning ?? liveSpot;
  // After ← / → on the button, the angle pill shows for a moment.
  const [keyPill, setKeyPill] = useState(false);
  const keyPillTimer = useRef<number | undefined>();
  useEffect(() => () => window.clearTimeout(keyPillTimer.current), []);

  const [lo, hi] = scaleRange;
  // A turned pattern is held on the frame by its turned outline (`rot`).
  const kept = useCallback(
    (b: Box, rot: number) => (keep === "inside" ? insideFrame(b) : pan ? b : partlyInside(b, 0.15, { rotate: rot, frame })),
    [keep, pan, frame],
  );

  const moveTo = useCallback(
    (dxFrac: number, dyFrac: number, rect: DOMRect, snapOn: boolean): Box => {
      let b: Box = { ...box, x: box.x + dxFrac, y: box.y + dyFrac };
      let gx: number | null = null;
      let gy: number | null = null;
      if (snapOn && snaps && !pan) {
        const sx = snapAxis([b.x, b.x + b.w / 2, b.x + b.w], snaps.x, SNAP_PX / Math.max(1, rect.width));
        const sy = snapAxis([b.y, b.y + b.h / 2, b.y + b.h], snaps.y, SNAP_PX / Math.max(1, rect.height));
        if (sx) {
          b = { ...b, x: b.x + sx.shift };
          gx = sx.target;
        }
        if (sy) {
          b = { ...b, y: b.y + sy.shift };
          gy = sy.target;
        }
      }
      const out = kept(b, rotation);
      setGuides({ x: out.x === b.x ? gx : null, y: out.y === b.y ? gy : null });
      return out;
    },
    [box, rotation, snaps, pan, kept],
  );

  const sizeTo = useCallback(
    (px: number, py: number, corner: number, alt: boolean): Box => {
      // An icon stays inside the frame: its scale about the corner it is
      // sized about (the opposite one, or its centre) is limited.
      const ax = alt ? box.x + box.w / 2 : [box.x + box.w, box.x, box.x, box.x + box.w][corner];
      const ay = alt ? box.y + box.h / 2 : [box.y + box.h, box.y + box.h, box.y, box.y][corner];
      const inside = keep === "inside" ? maxScaleInside(box, ax, ay) : Infinity;
      setGuides({ x: null, y: null });
      return sizedByCorner(box, rotation, corner, px, py, frame, alt, lo, Math.min(hi, inside)).box;
    },
    [box, rotation, frame, lo, hi, keep],
  );

  const at = (e: { clientX: number; clientY: number }, rect: DOMRect) => ({
    x: (e.clientX - rect.left) / Math.max(1, rect.width),
    y: (e.clientY - rect.top) / Math.max(1, rect.height),
  });

  /** A gesture begins (true), from the body, a corner or the turn button. */
  const start = (e: ReactPointerEvent, kind: Gesture["kind"], corner = 0): boolean => {
    if (e.button !== 0) return false;
    const rect = rootRef.current?.getBoundingClientRect();
    if (!rect) return false;
    e.stopPropagation();
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    const p = at(e, rect);
    gesture.current = {
      kind,
      corner,
      x0: e.clientX,
      y0: e.clientY,
      rect,
      alt: e.altKey,
      moved: false,
      angle0: angleAbout(box, p.x, p.y, frame),
    };
    return true;
  };

  const move = (e: ReactPointerEvent) => {
    const g = gesture.current;
    if (!g) return;
    if (!g.moved && Math.hypot(e.clientX - g.x0, e.clientY - g.y0) < MOVED_PX) return;
    g.moved = true;
    const p = at(e, g.rect);
    let next: Live;
    if (g.kind === "move") {
      next = {
        box: moveTo((e.clientX - g.x0) / Math.max(1, g.rect.width), (e.clientY - g.y0) / Math.max(1, g.rect.height), g.rect, !e.altKey),
        rot: rotation,
      };
    } else if (g.kind === "size") {
      next = { box: sizeTo(p.x, p.y, g.corner, e.altKey), rot: rotation };
    } else {
      // A drag clicks onto 0, 45, 90… within 4°; Shift turns in 15° steps.
      const rot = snapTurn(rotation + angleAbout(box, p.x, p.y, frame) - g.angle0, { step: e.shiftKey, magnet: !e.shiftKey });
      // A pattern sitting mostly off the frame could turn right off it: it
      // is held on by its turned outline (on the frame, it doesn't move).
      next = { box: kept(box, rot), rot, snap: e.shiftKey || onTurnStop(rot) };
    }
    liveRef.current = next;
    setLive(next);
    onChange(between(box, next.box, next.rot - rotation), { final: false });
  };

  const end = (e: ReactPointerEvent) => {
    const g = gesture.current;
    if (!g) return;
    gesture.current = null;
    e.stopPropagation();
    setGuides({ x: null, y: null });
    setTurning(null);
    const b = liveRef.current;
    if (g.moved && b) onChange(between(box, b.box, b.rot - rotation), { final: true });
    else setLive(null);
  };

  const cancel = () => {
    if (!gesture.current) return;
    gesture.current = null;
    liveRef.current = null;
    setLive(null);
    setGuides({ x: null, y: null });
    setTurning(null);
    onCancel?.();
  };

  /** A key's (or the pill's) change, given at once. */
  const commit = (b: Box, rot: number) => {
    const out = kept(b, rot);
    const next = { box: out, rot };
    liveRef.current = next;
    setLive(next);
    onChange(between(box, out, rot - rotation), { final: true, key: true });
  };

  const zoom = (grow: boolean) => {
    const cx = box.x + box.w / 2;
    const cy = box.y + box.h / 2;
    const limit = keep === "inside" ? maxScaleInside(box, cx, cy) : Infinity;
    const s = Math.min(Math.max(grow ? SIZE_STEP : 1 / SIZE_STEP, lo), hi, limit);
    commit(scaledAbout(box, s, cx, cy), rotation);
  };

  const key = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      if (gesture.current) {
        e.preventDefault();
        cancel();
      }
      return;
    }
    if (e.key === "0" && onReset) {
      e.preventDefault();
      onReset();
      return;
    }
    const step = e.shiftKey ? NUDGE_BIG : NUDGE;
    if (e.key === "+" || e.key === "=" || e.key === "-" || e.key === "_") {
      e.preventDefault();
      zoom(e.key === "+" || e.key === "=");
      return;
    }
    if (rotate && (e.key === "[" || e.key === "]" || e.key === "{" || e.key === "}")) {
      e.preventDefault();
      const by = (e.shiftKey ? TURN_BIG : TURN_STEP) * (e.key === "[" || e.key === "{" ? -1 : 1);
      // Exactly the degree asked: no pull onto 45° (1° at 0° stays 1°).
      commit(box, snapTurn(rotation + by));
      return;
    }
    let b: Box | null = null;
    if (e.key === "ArrowLeft") b = { ...box, x: box.x - step };
    else if (e.key === "ArrowRight") b = { ...box, x: box.x + step };
    else if (e.key === "ArrowUp") b = { ...box, y: box.y - step };
    else if (e.key === "ArrowDown") b = { ...box, y: box.y + step };
    if (!b) return;
    e.preventDefault();
    commit(b, rotation);
  };

  // The turn button: grabbed, it stays where it is until let go.
  const startTurn = (e: ReactPointerEvent) => {
    if (start(e, "turn")) setTurning(spot);
  };
  // ← / → on it turn 1° (Shift 15°), exactly; Esc drops a turn in progress.
  const turnKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      if (gesture.current) {
        e.preventDefault();
        cancel();
      }
      return;
    }
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    e.stopPropagation();
    const by = (e.shiftKey ? TURN_BIG : TURN_STEP) * (e.key === "ArrowLeft" ? -1 : 1);
    const rot = snapTurn(rotation + by);
    commit(box, rot);
    setKeyPill(true);
    window.clearTimeout(keyPillTimer.current);
    keyPillTimer.current = window.setTimeout(() => setKeyPill(false), KEY_PILL_MS);
  };

  const pct = (v: number) => `${v * 100}%`;
  const corners = [
    { left: 0, top: 0, cursor: "nwse-resize" },
    { left: 1, top: 0, cursor: "nesw-resize" },
    { left: 1, top: 1, cursor: "nwse-resize" },
    { left: 0, top: 1, cursor: "nesw-resize" },
  ];
  const sizePx = Math.round(Math.min(shown.w * frame.w, shown.h * frame.h));
  const handlers = { onPointerMove: move, onPointerUp: end, onPointerCancel: cancel };
  // The turn button, in the canvas' own pixels: a sibling of the turned box,
  // so it never turns with it or leaves the canvas (the canvas clips). Its
  // 44 px hit area holds a 32 px purple disc; its angle pill shows while it
  // turns, purple on a stop.
  const snapped = live?.snap ?? onTurnStop(shownRot);
  const turnButton =
    rotate && spot ? (
      <>
        <button
          type="button"
          data-turn-knob
          data-side={spot.side}
          aria-label={COPY.hand.turnButton(name ?? "")}
          title={COPY.hand.turnButtonTitle}
          onPointerDown={startTurn}
          {...handlers}
          onKeyDown={turnKey}
          onClick={(e) => e.stopPropagation()}
          className={`group pointer-events-auto absolute z-20 flex h-11 w-11 -translate-x-1/2 -translate-y-1/2 touch-none select-none items-center justify-center rounded-full focus:outline-none ${
            turning ? "cursor-grabbing" : "cursor-grab"
          }`}
          style={{ left: spot.x, top: spot.y }}
        >
          <span
            aria-hidden
            className="flex h-8 w-8 items-center justify-center rounded-full bg-[#7F2BF2] text-white shadow-[0_1px_3px_rgba(0,0,0,0.35)] ring-2 ring-white transition-transform duration-100 group-hover:scale-[1.06] group-focus-visible:outline group-focus-visible:outline-2 group-focus-visible:outline-offset-2 group-focus-visible:outline-accent"
          >
            <RotateCw className="h-4 w-4" strokeWidth={2.5} />
          </span>
        </button>
        {(turning || keyPill) && (
          <span
            aria-hidden
            data-turn-pill
            data-snapped={snapped ? "" : undefined}
            className={`pointer-events-none absolute z-20 flex -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full font-mono text-[11px] tabular-nums text-white shadow-[0_1px_3px_rgba(0,0,0,0.35)] ${
              snapped ? "bg-[#7F2BF2]" : "bg-black/75"
            }`}
            style={{ left: spot.pill.x, top: spot.pill.y, width: TURN_PILL.w, height: TURN_PILL.h }}
          >
            {Math.round(shownRot)}°
          </span>
        )}
      </>
    ) : null;

  if (pan) {
    // A full-screen pattern: the whole picture is its handle.
    const scalePct = Math.round(zoomNow * (shown.w / Math.max(1e-6, box.w)) * 100);
    return (
      <div ref={rootRef} className="pointer-events-none absolute inset-0" data-transform-box data-mode="pan">
        <div
          role="group"
          tabIndex={0}
          aria-label={label}
          title={hint}
          aria-roledescription="movable"
          data-tone={shownTone}
          data-rotate={Math.round(shownRot * 10) / 10}
          onPointerDown={(e) => start(e, "move")}
          {...handlers}
          onKeyDown={key}
          onClick={(e) => e.stopPropagation()}
          className="pointer-events-auto absolute inset-2 cursor-move touch-none select-none rounded-lg border-2 border-dashed border-white/80 shadow-[0_0_0_1px_rgba(0,0,0,0.35)] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <span
            ref={panPillRef}
            className="absolute right-2 top-1/2 inline-flex -translate-y-1/2 flex-col-reverse items-center gap-0.5 rounded-full bg-black/65 p-0.5 text-2xs text-white shadow-lift"
            onPointerDown={(e) => e.stopPropagation()}
          >
            <button
              type="button"
              aria-label={zoomLabels?.out ?? COPY.hand.zoomOut}
              onClick={(e) => {
                e.stopPropagation();
                zoom(false);
              }}
              className="flex h-6 w-6 items-center justify-center rounded-full hover:bg-white/15 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              <Minus className="h-3 w-3" aria-hidden />
            </button>
            <span className="px-0.5 text-center font-mono text-3xs tabular-nums">{scalePct}%</span>
            <button
              type="button"
              aria-label={zoomLabels?.in ?? COPY.hand.zoomIn}
              onClick={(e) => {
                e.stopPropagation();
                zoom(true);
              }}
              className="flex h-6 w-6 items-center justify-center rounded-full hover:bg-white/15 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              <Plus className="h-3 w-3" aria-hidden />
            </button>
          </span>
        </div>
        {turnButton}
      </div>
    );
  }

  return (
    <div ref={rootRef} className="pointer-events-none absolute inset-0" data-transform-box>
      {guides.x !== null && (
        <span aria-hidden className="absolute inset-y-0 w-px bg-sky-300/90" style={{ left: pct(guides.x) }} />
      )}
      {guides.y !== null && (
        <span aria-hidden className="absolute inset-x-0 h-px bg-sky-300/90" style={{ top: pct(guides.y) }} />
      )}
      <div
        role="group"
        tabIndex={0}
        aria-label={label}
        title={hint}
        aria-roledescription="movable"
        data-tone={shownTone}
        data-size-px={sizePx}
        data-rotate={Math.round(shownRot * 10) / 10}
        onPointerDown={(e) => start(e, "move")}
        {...handlers}
        onKeyDown={key}
        onClick={(e) => e.stopPropagation()}
        className={`pointer-events-auto absolute cursor-move touch-none select-none rounded-sm border-2 shadow-[0_0_0_1px_rgba(0,0,0,0.45)] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent ${TONE_CLASS[shownTone]}`}
        style={{
          left: pct(shown.x),
          top: pct(shown.y),
          width: pct(shown.w),
          height: pct(shown.h),
          transform: shownRot ? `rotate(${shownRot}deg)` : undefined,
        }}
      >
        {corners.map((c, i) => (
          <span
            key={i}
            role="presentation"
            onPointerDown={(e) => start(e, "size", i)}
            {...handlers}
            className="absolute flex h-6 w-6 -translate-x-1/2 -translate-y-1/2 items-center justify-center"
            style={{ left: `${c.left * 100}%`, top: `${c.top * 100}%`, cursor: c.cursor }}
          >
            <span aria-hidden className="h-2.5 w-2.5 rounded-[2px] border border-black/50 bg-white shadow" />
          </span>
        ))}
      </div>
      {turnButton}
    </div>
  );
}
