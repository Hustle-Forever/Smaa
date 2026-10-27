// Rebrand studio: the icons on the scene's timeline, a lane inside
// SceneTimeline under the patterns' (one ruler, one playhead, one time
// scale: the scene's seconds, the same as the patterns').
//
//   speech   one row: how loud they speak (the envelope), grey where no
//            icon may go (the opening, the pattern's entrance, the end; why
//            on hover), and what they say, at the moment they say it
//   icons    one block per icon (SpanBlock), from where it starts coming in
//            to where it is gone, a line where it has settled (the one time
//            the list, the Moments chips and the notes say: iconTime), each
//            drawn in its own look: dashed = the AI's pick, solid = yours or
//            pinned, amber = timed by hand, off its word. One row per
//            track, as the planner put them (the item's `lane`): the first
//            is the AI's, one at a time; one of yours that shows together
//            with another is on a track below.
//
// Drag a block to move it, its left edge to change when it starts (where
// it ends stays), its right edge to change how long it lasts (0.2 to 30 s
// fully on); drag it up or down to put it on another track (a dashed row
// under the last is a new one). "Snap to words" (on unless switched off,
// per viewer, on the timeline's status line, and held off with Alt): a
// block let go near a mention of its OWN idea lands on it; anywhere else it
// stays exactly where it was put, off its word (the status line says so,
// with "Back on its word"). No question is asked.
// Keyboard: ←/→ the next mention (Snap to words on) or 0.1 s (off),
// Shift+←/→ shorter or longer, Alt+←/→ the start, Alt+↑/↓ another track,
// Enter the exact frame, Delete removes it. A click (or a word) goes there
// and shows the exact frame.
//
// The lane is always there while the server draws icons: with no blocks to
// show it is one thin row saying why (IconLaneNote: off, placing, an error,
// none added). A version's own icons, as it froze them, are drawn read-only
// (IconLaneStatic), each on the track its draft had it on.

import { useState, type ReactNode } from "react";

import type { HarnessIconItem, HarnessIconRules, HarnessIconTimeline } from "../../../lib/rebrandHarnessApi";
import { fmtSec, glyphId, iconTime, LANE_MAX, momentOf, settleMark, snapPoints } from "../../../lib/rebrandIcons";
import { laneRows, type LaneSpan, type Span, type SpanDrag, type SpanLimits } from "../../../lib/rebrandSpan";
import { ICON_LANE_KEY, iconBlocksArea, TRACK_PX, type FrozenIconBlock } from "../../../lib/rebrandTracks";
import { IconGlyph } from "./IconGlyph";
import { SpanBlock } from "./SpanBlock";
import { COPY } from "./studioCopy";

const NUDGE = 0.1;
/** A block's top on its row. */
const BLOCK_TOP = (TRACK_PX.iconPad + TRACK_PX.iconRow - TRACK_PX.iconBar) / 2;

/** The rows the blocks sit on, and how many there are (at least one): each
 *  on its track as the planner put it (`lane`; a version's frozen icons on
 *  the ones its draft had: frozenIconBlocks), else stacked as they overlap
 *  (tracks not known). */
export function iconRowsOf(spans: readonly LaneSpan[]): { rows: number[]; count: number } {
  return laneRows(spans);
}

/** The speech and the words, one row, on the lane's time scale. */
function SpeechRow({
  timeline,
  total,
  width,
  onSeek,
  anchors = new Set<number>(),
  mentions = new Set<number>(),
}: {
  timeline: HarnessIconTimeline;
  total: number;
  width: number;
  onSeek: (t: number) => void;
  /** Words an icon lands on (always labelled, in bold). */
  anchors?: ReadonlySet<number>;
  /** The picked icon's other mentions (marked). */
  mentions?: ReadonlySet<number>;
}) {
  const pct = (s: number) => `${(Math.min(total, Math.max(0, s)) / total) * 100}%`;
  // The speech envelope as one filled path (0..100 x 0..24).
  const env = timeline.envelope ?? [];
  const envPath = env.length
    ? `M0,24 ${env.map((v, i) => `L${((i + 0.5) / env.length) * 100},${24 - Math.max(0.5, Math.min(1, v) * 22)}`).join(" ")} L100,24 Z`
    : "";
  const words = (timeline.words ?? []).filter(
    (w): w is typeof w & { s: number } => w.said && typeof w.s === "number",
  );
  // Word labels that fit (an anchor word always does).
  const labelled = new Set<number>();
  if (width > 0) {
    const order = [...words].sort((a, b) => Number(anchors.has(b.w)) - Number(anchors.has(a.w)) || a.s - b.s);
    const taken: [number, number][] = [];
    for (const w of order) {
      const x = (w.s / total) * width;
      const wd = w.text.length * 5.6 + 6;
      if (taken.some(([a, b]) => x < b && x + wd > a) || x + wd > width + 4) continue;
      taken.push([x, x + wd]);
      labelled.add(w.w);
    }
  }
  return (
    <div
      className="relative overflow-hidden border-t border-border/70 text-[9px] text-muted"
      style={{ height: TRACK_PX.speech }}
      onClick={(e) => {
        // A word goes to its own moment (its button); anywhere else, there.
        if ((e.target as HTMLElement).closest("button")) return;
        const r = e.currentTarget.getBoundingClientRect();
        if (r.width > 0) onSeek(Math.min(total, Math.max(0, ((e.clientX - r.left) / r.width) * total)));
      }}
    >
      {envPath && (
        <svg aria-hidden viewBox="0 0 100 24" preserveAspectRatio="none" className="pointer-events-none absolute inset-0 h-full w-full">
          <path d={envPath} className="fill-accent/20" />
        </svg>
      )}
      {(timeline.zones ?? []).map((z, i) => (
        <span
          key={i}
          title={COPY.icons.zoneTitle(z.why)}
          className="absolute inset-y-0 bg-[repeating-linear-gradient(135deg,rgba(113,113,122,0.28)_0_4px,rgba(113,113,122,0.12)_4px_8px)]"
          style={{ left: pct(z.from), width: `calc(${pct(z.to)} - ${pct(z.from)})` }}
        />
      ))}
      {(timeline.chunks ?? []).slice(1).map((c, i) => (
        <span key={i} aria-hidden className="pointer-events-none absolute inset-y-0 w-px bg-border-strong" style={{ left: pct(c.start) }} />
      ))}
      {words.map((w) => (
        <button
          key={w.w}
          type="button"
          tabIndex={-1}
          onClick={() => onSeek(w.s)}
          title={`${w.text} · ${fmtSec(w.s)}`}
          className={`absolute top-0 h-full -translate-x-px whitespace-nowrap leading-[18px] hover:text-fg ${
            anchors.has(w.w) ? "font-semibold text-fg" : ""
          } ${mentions.has(w.w) ? "text-accent-hover" : ""}`}
          style={{ left: pct(w.s) }}
        >
          <span aria-hidden className="absolute left-0 top-0 h-1 w-px bg-border-strong" />
          {labelled.has(w.w) ? <span className="pl-0.5">{w.text}</span> : <span className="inline-block w-1" />}
          {mentions.has(w.w) && (
            <span aria-hidden className="absolute bottom-0 left-0 h-1.5 w-1.5 -translate-x-1/2 rounded-full bg-accent" />
          )}
        </button>
      ))}
    </div>
  );
}

/** The icons' lane, its blocks editable (the draft's plan). */
export function IconLane({
  width,
  duration,
  timeline,
  items,
  fill,
  ink,
  selected,
  onSelect,
  onSeek,
  rules,
  snapOn,
  onSpan,
  onLane,
  onStep,
  onRemove,
  ghost = null,
  busy = false,
}: {
  /** The lanes' width (px) and seconds: SceneTimeline's. */
  width: number;
  duration: number;
  /** The plan's words (null: none yet). */
  timeline: HarnessIconTimeline | null;
  /** In speech order. */
  items: HarnessIconItem[];
  fill: string;
  ink: string;
  selected: string | null;
  /** Picked: grabbed, focused or chosen. */
  onSelect: (id: string | null) => void;
  /** Go there and rest (its exact frame). */
  onSeek: (t: number) => void;
  rules: HarnessIconRules;
  /** "Snap to words". */
  snapOn: boolean;
  /** A block let go: moved, or an edge trimmed. The span it lands on, the
   *  word it snapped onto (null: off its word, exactly there), and the
   *  track it was let go on when that is another (null: its own). */
  onSpan: (item: HarnessIconItem, span: Span, kind: SpanDrag, snappedW: number | null, lane: number | null) => void;
  /** A block dragged up or down at the same time: the track it goes on. */
  onLane: (item: HarnessIconItem, lane: number) => void;
  /** ←/→ with Snap to words: its previous / next mention. */
  onStep: (item: HarnessIconItem, dir: -1 | 1) => void;
  onRemove: (item: HarnessIconItem) => void;
  /** Where an icon would go if the user says yes to the list's question
   *  ("Show Code first anyway?"): amber, dashed. */
  ghost?: { id: string; sec: number } | null;
  busy?: boolean;
}) {
  const total = Math.max(0.1, duration);
  const inSec = rules.in_f / rules.fps;
  const outSec = rules.out_f / rules.fps;
  const holdMin = rules.user_hold_min ?? 0.2;
  const holdMax = rules.user_hold_max ?? 30;
  const pct = (s: number) => `${(Math.min(total, Math.max(0, s)) / total) * 100}%`;

  const anchors = new Set(items.map((it) => it.w).filter((w): w is number => w != null));
  const sel = items.find((it) => glyphId(it.glyph) === selected) ?? null;
  const mentionWs = new Set(sel?.mentions.map((m) => m.w) ?? []);

  // One row per track (the planner's).
  const { rows, count } = iconRowsOf(items.map((it) => ({ from: it.from, to: it.to, lane: it.lane })));
  // While a block is dragged down past the last track: a dashed new one,
  // drawn inside the lane (its height never changes mid-drag).
  const [liveRow, setLiveRow] = useState<number | null>(null);
  const area = iconBlocksArea(count, liveRow, LANE_MAX);
  const toLane = (it: HarnessIconItem, row: number, d: -1 | 1) => {
    const lane = Math.min(Math.min(count, LANE_MAX), Math.max(0, row + d));
    if (lane !== row) onLane(it, lane);
  };

  /** Where a block may go: inside the scene; one on its word keeps out of
   *  the end (the planner's zone), one timed by hand may run to the end. */
  const limitsOf = (it: HarnessIconItem): SpanLimits => ({
    min: inSec + holdMin + outSec,
    max: inSec + holdMax + outSec,
    lo: 0,
    hi: it.off_word ? total : Math.max(it.to, total - rules.tail_clear),
  });

  const nudge = (it: HarnessIconItem, kind: SpanDrag, d: number) => {
    const lim = limitsOf(it);
    const span = { from: it.from, to: it.to };
    if (kind === "move") {
      const from = Math.min(Math.max(span.from + d, 0), total - (span.to - span.from));
      onSpan(it, { from, to: from + (span.to - span.from) }, "move", null, null);
    } else if (kind === "start") {
      onSpan(it, { from: Math.min(Math.max(span.from + d, 0), span.to - lim.min), to: span.to }, "start", null, null);
    } else {
      onSpan(it, { from: span.from, to: Math.max(span.from + lim.min, Math.min(span.to + d, lim.hi)) }, "end", null, null);
    }
  };

  const seekTo = (s: number) => onSeek(Math.min(total, Math.max(0, s)));

  return (
    <div data-lane={ICON_LANE_KEY} className="relative" aria-busy={busy || undefined}>
      {timeline && (
        <SpeechRow timeline={timeline} total={total} width={width} onSeek={seekTo} anchors={anchors} mentions={mentionWs} />
      )}
      {/* the icons */}
      <div
        role="group"
        aria-label={COPY.icons.laneLabel}
        className={`relative border-t ${timeline ? "border-border/40" : "border-border/70"}`}
        style={{ height: area.heightPx }}
        onClick={(e) => {
          if (e.target !== e.currentTarget) return;
          const r = e.currentTarget.getBoundingClientRect();
          if (r.width > 0) seekTo(((e.clientX - r.left) / r.width) * total);
        }}
      >
        {/* the tracks under the first: a hairline each; and, while a block
            is dragged down past the last, the dashed new one, inside the
            lane (made when it is let go) */}
        {Array.from({ length: count - 1 }, (_, i) => i + 1).map((row) => (
          <span
            key={row}
            aria-hidden
            title={COPY.icons.track(row)}
            className="pointer-events-none absolute inset-x-0 border-t border-border/30"
            style={{ top: TRACK_PX.iconPad / 2 + row * TRACK_PX.iconRow, height: 0 }}
          />
        ))}
        {area.newTrack && (
          <span
            aria-hidden
            title={COPY.icons.newTrack}
            className="pointer-events-none absolute inset-x-0 rounded-md border-2 border-dashed border-accent/50 bg-accent-fade/40"
            style={{ top: area.newTop, height: TRACK_PX.iconBar }}
          />
        )}
        {ghost && (
          <span
            aria-hidden
            className="absolute rounded-md border-2 border-dashed border-amber-500/80 bg-amber-100/50"
            style={{
              top: BLOCK_TOP,
              height: TRACK_PX.iconBar,
              left: pct(ghost.sec - inSec),
              width: `calc(${pct(ghost.sec + rules.hold_min + outSec)} - ${pct(ghost.sec - inSec)})`,
            }}
          />
        )}
        {items.map((it, i) => {
          const id = glyphId(it.glyph);
          const mine = it.source === "user" || it.pinned;
          const at = fmtSec(iconTime(it));
          const points = snapPoints(it, timeline?.words, rules);
          const byAt = new Map(points.map((p) => [p.at, p.w]));
          return (
            <SpanBlock
              key={id}
              span={{ from: it.from, to: it.to }}
              seconds={total}
              width={width}
              limits={limitsOf(it)}
              snap={{ points: points.map((p) => p.at), anchor: inSec, within: rules.magnet }}
              snapOn={snapOn}
              top={BLOCK_TOP + rows[i] * TRACK_PX.iconRow}
              height={TRACK_PX.iconBar}
              selected={selected === id}
              rows={{
                row: rows[i],
                pitch: TRACK_PX.iconRow,
                count: Math.min(count, LANE_MAX),
                onLive: setLiveRow,
                newTop: count <= LANE_MAX ? area.newTop : null,
              }}
              onSelect={() => onSelect(id)}
              onFocus={() => onSelect(id)}
              onClick={() => seekTo(momentOf(it))}
              mark={settleMark(it)}
              onCommit={(span, kind, snapped, row) =>
                onSpan(it, span, kind, snapped != null ? byAt.get(snapped) ?? null : null, row)
              }
              onRow={(row) => onLane(it, row)}
              onKeyDown={(e) => {
                if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
                  e.preventDefault();
                  toLane(it, rows[i], e.key === "ArrowUp" ? -1 : 1);
                } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
                  e.preventDefault();
                  const dir = e.key === "ArrowLeft" ? -1 : 1;
                  if (e.shiftKey) nudge(it, "end", dir * NUDGE);
                  else if (e.altKey) nudge(it, "start", dir * NUDGE);
                  else if (snapOn) onStep(it, dir);
                  else nudge(it, "move", dir * NUDGE);
                } else if (e.key === "Enter") {
                  e.preventDefault();
                  seekTo(momentOf(it));
                } else if (e.key === "Delete" || e.key === "Backspace") {
                  e.preventDefault();
                  onRemove(it);
                }
              }}
              aria={{
                label: COPY.icons.blockAria(it.label, at, it.word),
                valuetext: COPY.icons.rowAria(it.label, at, it.off_word ? null : it.word),
                now: it.settle,
              }}
              titles={{ start: COPY.icons.startHandle(it.label), end: COPY.icons.lengthHandle(it.label) }}
              className={`${
                it.off_word ? "border-amber-500 bg-amber-50" : mine ? "border-fg/70 bg-bg" : "border-dashed border-fg/45 bg-bg"
              } ${selected === id ? "ring-2 ring-accent/60" : ""}`}
            >
              <IconGlyph
                glyph={it.glyph}
                d={it.d}
                size={16}
                look={{ style: it.style ?? "badge", fill: it.fill ?? fill, ink: it.ink ?? ink, weight: it.weight }}
              />
              <span className="min-w-0 truncate">{it.label}</span>
            </SpanBlock>
          );
        })}
      </div>
    </div>
  );
}

/** The icons' lane with no blocks to show: one thin row saying why (off,
 *  placing, an error, none added), under the speech once the words are in. */
export function IconLaneNote({
  width,
  duration,
  timeline = null,
  onSeek,
  tone = "muted",
  children,
}: {
  width: number;
  duration: number;
  timeline?: HarnessIconTimeline | null;
  onSeek: (t: number) => void;
  tone?: "muted" | "danger";
  children: ReactNode;
}) {
  const total = Math.max(0.1, duration);
  return (
    <div data-lane={ICON_LANE_KEY} className="relative">
      {timeline && <SpeechRow timeline={timeline} total={total} width={width} onSeek={onSeek} />}
      <div
        role="group"
        aria-label={COPY.icons.laneLabel}
        className={`flex items-center gap-2 overflow-hidden border-t px-2 text-2xs ${tone === "danger" ? "text-red-700" : "text-muted"} ${
          timeline ? "border-border/40" : "border-border/70"
        }`}
        style={{ height: TRACK_PX.iconNote }}
      >
        {children}
      </div>
    </div>
  );
}

/** A version's own icons, as it froze them, on its read-only timeline. */
export function IconLaneStatic({ duration, blocks }: { duration: number; blocks: FrozenIconBlock[] }) {
  const total = Math.max(0.1, duration);
  const pct = (s: number) => `${(Math.min(total, Math.max(0, s)) / total) * 100}%`;
  const { rows, count } = iconRowsOf(blocks);
  return (
    <div data-lane={ICON_LANE_KEY} className="relative">
      <div
        role="group"
        aria-label={COPY.icons.laneLabel}
        className="relative border-t border-border/70"
        style={{ height: TRACK_PX.iconPad + TRACK_PX.iconRow * count }}
      >
        {blocks.map((b, i) => (
          <div
            key={b.key}
            // Its one time (when it settled), as the draft's blocks say it.
            title={b.settle != null ? `${b.label} · ${fmtSec(b.settle)}` : `${b.label} · ${fmtSec(b.from)} to ${fmtSec(b.to)}`}
            className="absolute flex items-center gap-1 overflow-hidden rounded-md border-2 border-dashed border-fg/45 bg-bg pl-0.5 pr-2 text-2xs text-muted"
            style={{
              left: pct(b.from),
              width: `max(1.75rem, calc(${pct(b.to)} - ${pct(b.from)}))`,
              top: BLOCK_TOP + rows[i] * TRACK_PX.iconRow,
              height: TRACK_PX.iconBar,
            }}
          >
            {b.settle != null && (
              <span
                aria-hidden
                className="pointer-events-none absolute inset-y-0 left-0 border-r-2 border-current bg-fg/10 opacity-60"
                style={{ width: `${((b.settle - b.from) / Math.max(1e-6, b.to - b.from)) * 100}%` }}
              />
            )}
            <IconGlyph glyph={b.glyph} size={16} look={{ style: b.style, fill: b.fill, ink: b.ink, weight: b.weight ?? undefined }} />
            <span className="min-w-0 truncate">{b.label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
