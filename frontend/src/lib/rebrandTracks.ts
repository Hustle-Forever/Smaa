// Rebrand studio: the scene's tracks under the stage, as numbers. Which lanes
// the timeline (SceneTimeline) shows, in which order, and how tall each is.
//
// The list never depends on the rail's tab, on what is selected or focused,
// or on which pattern's row is open: those only highlight a lane (and scroll
// it into view inside the timeline). On Next it always holds, in order:
//
//   kept      the layers of the version the draft is added on top of, one
//             lane each, locked (drawn, never moved); patterns only
//   patterns  one lane per pattern of the draft, a hidden one too (hatched)
//   add       "Add a pattern", when the draft has none of its own
//   icons     the icons' lane whenever the server draws icons: its speech
//             row once the words are in, then its blocks, or a thin row
//             that says why there are none (off, placing, an error)
//
// A version (or the original) shows the same lanes, read-only: its own
// patterns locked, its icons as it froze them, each on the track its draft
// had it on (frozenIconBlocks with the version's tracks); "empty" where it
// has none.
//
// Every lane has a key of its own: a kept layer and a draft pattern may share
// an element id (every pass numbers its own from "pattern"), so the key, not
// the id, is what React, the highlight and the snapping go by.
//
// The heights here are the ones the lanes are drawn at (TRACK_PX), and the
// timeline takes at most LANE_CAP_SHARE of the stage column (the picture
// keeps the rest), scrolling inside it with the ruler stuck at its top.
//
// Plain TypeScript only (no enums, no path aliases, no runtime imports): node
// runs it (scripts/check-rebrand-tracks.mjs, check-rebrand-icon-times.mjs).

import type { HarnessIconProps } from "./rebrandHarnessApi";

/** The drawn sizes (px). */
export const TRACK_PX = {
  /** The ruler (sticky at the top of the lanes). */
  ruler: 20,
  /** A pattern's lane, and its bar. */
  pattern: 28,
  bar: 24,
  /** "Add a pattern", or an empty lane of a version. */
  add: 20,
  /** The icons' speech and words, one row. */
  speech: 18,
  /** One row of icon blocks (the pitch), the block itself, and the room
   *  around the rows. */
  iconRow: 24,
  iconBar: 22,
  iconPad: 4,
  /** A thin row in place of the blocks (off, placing, an error, none). */
  iconNote: 20,
  /** The status line under the lanes: always this tall. */
  status: 18,
  /** Between the lanes' box and the status line. */
  gap: 4,
  /** The lanes' box: its top and bottom border. */
  border: 2,
} as const;

/** The most of the stage column the timeline takes (its lanes scroll past
 *  that): a share of the column's content box, its padding left out (what
 *  a CSS max-height of 42% resolves against). */
export const LANE_CAP_SHARE = 0.42;

/** The icons' lane, as the timeline shows it. */
export type IconLaneState =
  /** The server doesn't draw icons: no lane. */
  | { kind: "none" }
  /** The draft's icons are switched off. */
  | { kind: "off" }
  /** Omni's own icons are kept (ours are off). */
  | { kind: "omni" }
  /** Nothing to show: an intro or outro with none added, a version with
   *  none. */
  | { kind: "empty" }
  /** The plan is on its way (`words`: the speech row is in already). */
  | { kind: "placing"; words: boolean }
  /** The plan couldn't be had. */
  | { kind: "error"; words: boolean; retryable: boolean }
  /** The plan's blocks, on `rows` rows (at least one). */
  | { kind: "ready"; words: boolean; rows: number };

/** What a lane needs to know about a pattern element. */
export interface TrackElement {
  id: string;
  kind: string;
  enabled: boolean;
}

export interface TrackInput {
  /** The layers of the version added to (any kind: only patterns get a
   *  lane), in draw order. */
  kept: readonly TrackElement[];
  /** The draft's own elements (or a version's, read-only), in draw order. */
  patterns: readonly TrackElement[];
  icons: IconLaneState;
  /** A version or the original: every lane locked, no "Add a pattern". */
  readOnly?: boolean;
}

export type LaneKind = "pattern" | "add" | "empty" | "icons";

export interface TrackLane {
  /** Unique in the list. */
  key: string;
  kind: LaneKind;
  /** The pattern's element id (null for the other lanes). */
  id: string | null;
  /** Its index in `kept` (locked) or in `patterns` (null for the others). */
  index: number | null;
  /** Drawn, never moved (a kept layer, or any lane of a version). */
  locked: boolean;
  /** A hidden pattern (its lane is hatched). */
  hidden: boolean;
  height: number;
}

export interface TimelineTracks {
  lanes: TrackLane[];
  /** The lanes' own height (px). */
  lanesPx: number;
  /** The lanes' box: its border, the ruler and the lanes (px). */
  boxPx: number;
  /** All of it: the box, the gap and the status line (px). */
  totalPx: number;
}

/** A draft pattern's lane key (what the highlight scrolls to). */
export function patternLaneKey(id: string): string {
  return `p:${id}`;
}

/** A kept layer's lane key: its place among the kept ones and its id. */
export function keptLaneKey(index: number, id: string): string {
  return `k:${index}:${id}`;
}

export const ICON_LANE_KEY = "icons";
export const ADD_LANE_KEY = "add";

/** The icons' lane height for its state (0: no lane). */
/** The icons' blocks area for `count` tracks while a block may be dragged
 *  (`liveRow`: the row it would land on, null when none is): its height is
 *  the tracks' own and never changes mid-drag, so neither the picture nor
 *  any lane moves under the pointer. A drag past the last track while one
 *  more may be made (`laneMax`) shows the "new track" band inside it, at
 *  `newTop`, where the dragged block is drawn; the track is made when it is
 *  let go (the plan's own rows then). */
export function iconBlocksArea(
  count: number,
  liveRow: number | null,
  laneMax: number,
): { heightPx: number; newTrack: boolean; newTop: number } {
  const rows = Math.max(1, Math.floor(count));
  const heightPx = TRACK_PX.iconPad + TRACK_PX.iconRow * rows;
  return {
    heightPx,
    newTrack: liveRow != null && liveRow >= rows && rows <= laneMax,
    newTop: heightPx - TRACK_PX.iconBar,
  };
}

/** What picking a pattern's bar on the timeline does besides selecting it:
 *  a click opens its row in the rail (the Patterns tab), a focus (a tab onto
 *  it, a grab) only picks it. A click made while the stage plays the Icons
 *  tab's motion leaves that motion: the stage must then rest on the moment
 *  the click goes to (the bar's own), never fall back to the design still. */
export function patternPick(
  how: "click" | "focus",
  iconsMotion: boolean,
): { openRow: boolean; restOnMoment: boolean } {
  const click = how === "click";
  return { openRow: click, restOnMoment: click && iconsMotion };
}

export function iconLanePx(s: IconLaneState): number {
  if (s.kind === "none") return 0;
  const words = (s.kind === "placing" || s.kind === "error" || s.kind === "ready") && s.words ? TRACK_PX.speech : 0;
  if (s.kind === "ready") return words + TRACK_PX.iconPad + TRACK_PX.iconRow * Math.max(1, Math.floor(s.rows));
  return words + TRACK_PX.iconNote;
}

/** The lanes, in order, with their heights. Takes nothing about the rail's
 *  tab or the selection: the same draft always gives the same lanes. */
export function timelineTracks(input: TrackInput): TimelineTracks {
  const readOnly = !!input.readOnly;
  const lanes: TrackLane[] = [];
  const keys = new Set<string>();
  const unique = (key: string) => {
    let k = key;
    for (let n = 2; keys.has(k); n++) k = `${key}#${n}`;
    keys.add(k);
    return k;
  };
  input.kept.forEach((el, i) => {
    if (el.kind !== "pattern") return;
    lanes.push({
      key: unique(keptLaneKey(i, el.id)),
      kind: "pattern",
      id: el.id,
      index: i,
      locked: true,
      hidden: !el.enabled,
      height: TRACK_PX.pattern,
    });
  });
  let own = 0;
  input.patterns.forEach((el, i) => {
    if (el.kind !== "pattern") return;
    own += 1;
    lanes.push({
      key: unique(patternLaneKey(el.id)),
      kind: "pattern",
      id: el.id,
      index: i,
      locked: readOnly,
      hidden: !el.enabled,
      height: TRACK_PX.pattern,
    });
  });
  if (!own && !(readOnly && lanes.length)) {
    lanes.push({
      key: unique(ADD_LANE_KEY),
      kind: readOnly ? "empty" : "add",
      id: null,
      index: null,
      locked: readOnly,
      hidden: false,
      height: TRACK_PX.add,
    });
  }
  const iconPx = iconLanePx(input.icons);
  if (iconPx > 0) {
    lanes.push({
      key: unique(ICON_LANE_KEY),
      kind: "icons",
      id: null,
      index: null,
      locked: readOnly,
      hidden: false,
      height: iconPx,
    });
  }
  const lanesPx = lanes.reduce((a, l) => a + l.height, 0);
  const boxPx = TRACK_PX.border + TRACK_PX.ruler + lanesPx;
  return { lanes, lanesPx, boxPx, totalPx: boxPx + TRACK_PX.gap + TRACK_PX.status };
}

/** The timeline's cap in a stage column this tall (px). */
export function laneCap(stagePx: number): number {
  return Math.round(Math.max(0, stagePx - STAGE_PX.pad) * LANE_CAP_SHARE);
}

/** The timeline in a stage column this tall: how tall it is drawn, whether
 *  its lanes scroll, and what is left for the picture (`fixedPx`: the rest of
 *  the column that isn't the picture: its header, the row under the picture,
 *  the gaps and padding). */
export function fitTracks(
  tracks: Pick<TimelineTracks, "totalPx">,
  stagePx: number,
  fixedPx: number = STAGE_PX.fixed,
): { capPx: number; shownPx: number; needsScroll: boolean; picturePx: number } {
  const capPx = laneCap(stagePx);
  const needsScroll = tracks.totalPx > capPx;
  const shownPx = needsScroll ? capPx : tracks.totalPx;
  return { capPx, shownPx, needsScroll, picturePx: Math.max(0, stagePx - fixedPx - shownPx) };
}

/** The Rebrand stage around the studio, from lg up (RebrandStage and
 *  StudioStage): the card is the rest of the window under the stepper row
 *  (at least `cardMin`, its border inside that); the scene row and the
 *  studio's footer take their share, and the stage column keeps `fixed`
 *  for its header (32), the row under the picture (30), its three gaps (24)
 *  and its padding (`pad`, 20). Measured in a browser at 1366x768 (stage
 *  column 497, cap 200, picture 191 with 5 lanes and 2 icon rows). */
export const STAGE_PX = {
  cardMin: 600,
  cardBorder: 2,
  sceneRow: 44,
  footer: 57,
  fixed: 106,
  pad: 20,
  /** The window's margin under the card. */
  margin: 16,
} as const;

/** The stage column for a window this tall (`offset`: the stepper row above
 *  the card), as RebrandStage sizes its card. */
export function stageColumnPx(innerHeight: number, offset: number): number {
  const card = Math.max(STAGE_PX.cardMin, Math.floor(innerHeight - offset - STAGE_PX.margin));
  return card - STAGE_PX.cardBorder - STAGE_PX.sceneRow - STAGE_PX.footer;
}

/** One icon a version froze at Generate (its icon element's
 *  `props.resolved` row), as its read-only lane draws it. */
export interface FrozenIconBlock {
  key: string;
  glyph: { source: "library" | "custom"; id: string };
  label: string;
  from: number;
  to: number;
  /** When it settled (the one time said for it), inside its block; null:
   *  the version didn't say. */
  settle: number | null;
  /** Its track, as the draft it was made from had it (0: the AI's, one at
   *  a time); null: not known (the lane then stacks them as they overlap). */
  lane: number | null;
  style: "circle" | "chip" | "badge" | "glyph";
  fill: string;
  ink: string;
  weight: "fill" | "bold" | null;
}

const FROZEN_STYLES = ["circle", "chip", "badge", "glyph"];
const HEX6 = /^#[0-9a-f]{6}$/i;

/** A whole track number (0 and up). */
function isTrack(n: unknown): n is number {
  return typeof n === "number" && Number.isInteger(n) && n >= 0;
}

/** Tracks numbered with no empty one between them, track 0 (the AI's)
 *  staying 0: as the planner numbers them (elements/icon.py _compact). */
function closeUp(lanes: readonly number[]): number[] {
  const used = [...new Set(lanes.filter((n) => n > 0))].sort((a, b) => a - b);
  return lanes.map((n) => (n > 0 ? used.indexOf(n) + 1 : 0));
}

/** A version's icons, from the rows it froze (glyph, label, from_sec,
 *  settle_sec, to_sec, its look): a row that can't be drawn (no glyph, no
 *  time, or gone before it starts) is left out. The look's defaults are the
 *  42 badge in 42 violet and white, as on an older version. `lanes`: the
 *  track of each row, in order (rebrandBuildOn.frozenLanesOf: the ones its
 *  draft had), closed up over the rows left out; none (or not one per row):
 *  each block's `lane` is null, and the lane stacks them as they overlap. */
export function frozenIconBlocks(
  rows: readonly unknown[] | null | undefined,
  lanes?: readonly unknown[] | null,
): FrozenIconBlock[] {
  const out: FrozenIconBlock[] = [];
  const tracked = Array.isArray(lanes) && lanes.length === (rows ?? []).length && lanes.every(isTrack);
  (rows ?? []).forEach((r, i) => {
    if (!r || typeof r !== "object") return;
    const x = r as Record<string, unknown>;
    const g = x.glyph as { source?: unknown; id?: unknown } | null | undefined;
    const id = typeof x.glyph === "string" ? x.glyph : g && typeof g.id === "string" ? g.id : "";
    if (!id) return;
    const from = typeof x.from_sec === "number" ? x.from_sec : NaN;
    const to = typeof x.to_sec === "number" ? x.to_sec : NaN;
    if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from) return;
    const custom = (g && g.source === "custom") || /^c-[0-9a-f]{12}$/.test(id);
    const settle = typeof x.settle_sec === "number" && x.settle_sec > from && x.settle_sec < to ? x.settle_sec : null;
    const style = typeof x.style === "string" && FROZEN_STYLES.includes(x.style) ? (x.style as FrozenIconBlock["style"]) : "badge";
    out.push({
      key: `${id}:${i}`,
      glyph: { source: custom ? "custom" : "library", id },
      label: typeof x.label === "string" && x.label ? x.label : id,
      from: Math.max(0, from),
      to,
      settle,
      lane: tracked ? (lanes[i] as number) : null,
      style,
      fill: typeof x.fill === "string" && HEX6.test(x.fill) ? x.fill : "#7f2bf2",
      ink: typeof x.ink === "string" && HEX6.test(x.ink) ? x.ink : "#ffffff",
      weight: x.weight === "fill" || x.weight === "bold" ? x.weight : null,
    });
  });
  if (!tracked) return out;
  const shut = closeUp(out.map((b) => b.lane as number));
  return out.map((b, k) => ({ ...b, lane: shut[k] }));
}

/** The clear air between two of the user's icons on track 0 (s): the
 *  planner's for two key ideas (GAP_KEY; rules.gap_user). */
export const TRACK_GAP_SEC = 0.3;

/** A version's icons' tracks worked out from its recipe, as the planner
 *  puts them (elements/icon.py: TRACKS), for a version that didn't keep
 *  them (made before Generate kept them: options.icon_lanes). One per row
 *  of `rows` (its props.resolved), in order; `owners`: its icon element's
 *  props (who added, pinned or put each one on a track). The planner's
 *  order: the ones timed by hand first (in time order), each on the track
 *  the user put it on when free there, else the first free one; then the
 *  rest of the user's (pinned to a word first; in each lot the AI's picks
 *  they made theirs, on a track or a word, before their adds, since such a
 *  pick keeps the AI's rank, which comes first in Scheduler.run; then the
 *  adds in the order they were added), each on the track the user put it
 *  on when free there, else track 0 when it keeps `gap` clear of every one
 *  there, else the first free track; the AI's own always on track 0 (they
 *  keep clear of everything). Numbered with no empty track between. A row
 *  that can't be drawn (no time) takes track 0 and is in nobody's way. */
export function frozenLanes(
  rows: readonly unknown[] | null | undefined,
  owners: Pick<HarnessIconProps, "added" | "tweaks" | "lanes"> | null | undefined,
  gap: number = TRACK_GAP_SEC,
): number[] {
  const spans = (rows ?? []).map((r, i) => {
    const x = (r && typeof r === "object" ? r : {}) as Record<string, unknown>;
    const g = x.glyph as { id?: unknown } | string | null | undefined;
    const id = typeof g === "string" ? g : g && typeof g.id === "string" ? g.id : "";
    const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : NaN);
    return { i, id, from: num(x.from_sec), to: num(x.to_sec) };
  });
  type Row = (typeof spans)[number];
  const added: unknown[] = (Array.isArray(owners?.added) ? owners.added : []).map((a) => a?.glyph?.id);
  const tweaks = owners?.tweaks && typeof owners.tweaks === "object" ? owners.tweaks : {};
  const pins: Record<string, unknown> = owners?.lanes && typeof owners.lanes === "object" ? owners.lanes : {};
  const hand = (id: string) => !!tweaks[id]?.off_word;
  const onWord = (id: string) => !hand(id) && typeof tweaks[id]?.w === "number";
  const pinned = (id: string) => Object.prototype.hasOwnProperty.call(pins, id);
  // A track of its own the user put it on (0 is the AI's: tried first anyway).
  const own = (id: string): number | null => {
    const n = pins[id];
    return pinned(id) && isTrack(n) && n > 0 ? n : null;
  };
  const mine = (id: string) => !!id && (added.includes(id) || hand(id) || onWord(id) || pinned(id));

  const lane: (number | null)[] = spans.map(() => null);
  const EPS = 1e-3;
  // Apart, with `air` between them (a row with no time: always apart).
  const apart = (a: Row, b: Row, air: number) => !(a.from < b.to + air - EPS && b.from < a.to + air - EPS);
  const free = (s: Row, n: number, air: number) =>
    spans.every((o) => o === s || lane[o.i] !== n || apart(s, o, n === 0 ? air : 0));
  const take = (s: Row, air: number) => {
    const first = own(s.id);
    const tries = [...(first != null ? [first] : []), ...spans.map((_, n) => n), spans.length];
    lane[s.i] = tries.find((n) => free(s, n, air)) ?? spans.length;
  };

  // Timed by hand: kept apart from one another only (they may sit close).
  const byHand = spans.filter((s) => mine(s.id) && hand(s.id));
  byHand.sort((a, b) => a.from - b.from || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  for (const s of byHand) take(s, 0);
  const addAt = (id: string) => {
    const k = added.indexOf(id);
    return k < 0 ? Infinity : k;
  };
  const rest = spans.filter((s) => mine(s.id) && !hand(s.id));
  // An AI pick keeps the AI's rank (no priority on a frozen row: their row
  // order), ahead of every add; the adds go in the order they were added.
  const ai = (id: string) => (addAt(id) === Infinity ? 0 : 1);
  rest.sort((a, b) =>
    Number(!onWord(a.id)) - Number(!onWord(b.id)) || ai(a.id) - ai(b.id) ||
    (ai(a.id) ? addAt(a.id) - addAt(b.id) : 0) || a.i - b.i);
  for (const s of rest) take(s, gap);
  return closeUp(lane.map((n) => n ?? 0));
}

/** The scroll that shows a lane whole (top `top`, `height` tall, in the
 *  lanes' box) under a sticky ruler `sticky` px tall, in a box `view` px
 *  tall scrolled to `scroll`: unchanged when it is in view already. A lane
 *  taller than the view shows its top. */
export function scrollToShow(top: number, height: number, scroll: number, view: number, sticky: number): number {
  const visTop = scroll + sticky;
  const visBottom = scroll + view;
  if (top < visTop || height > view - sticky) return Math.max(0, top - sticky);
  if (top + height > visBottom) return Math.max(0, top + height - view);
  return scroll;
}
