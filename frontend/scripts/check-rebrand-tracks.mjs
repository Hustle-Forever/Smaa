// Checks src/lib/rebrandTracks.ts: the lanes the scene's timeline shows.
//   * the same lanes for every rail tab and every selection (the list takes
//     neither: clicking a pattern never hides the icons' lane);
//   * every pattern of the draft (a hidden one too) and every kept pattern
//     has exactly one lane, every key is unique even when a kept layer and a
//     draft pattern share an element id, kept lanes are locked by a flag (not
//     an id prefix), and a kept icon element never becomes a pattern lane;
//   * the icons' lane is there whenever the server draws icons (on, off,
//     Omni's own, placing, an error, none added), and always last;
//   * no pattern of the draft's own: an "Add a pattern" row; a version or
//     the original: the same lanes, all locked, "empty" where it has none,
//     and a version's icons read from the rows it froze;
//   * the heights: the report's sums (3 patterns + icons, 5 lanes + 2 icon
//     rows), and at 1366x768 and 1920x1080 the timeline stays inside its cap
//     (or says it scrolls) and leaves the picture its room;
//   * scrolling a lane into view under the sticky ruler.
//
//   node --experimental-strip-types scripts/check-rebrand-tracks.mjs
//
// (node 22.6+.) Prints a line per group and exits 1 on any failure.

import { readFileSync } from "node:fs";

import {
  fitTracks,
  frozenIconBlocks,
  ICON_LANE_KEY,
  iconBlocksArea,
  iconLanePx,
  keptLaneKey,
  laneCap,
  patternLaneKey,
  patternPick,
  scrollToShow,
  STAGE_PX,
  stageColumnPx,
  timelineTracks,
  TRACK_PX,
} from "../src/lib/rebrandTracks.ts";

let failed = 0;
let total = 0;
const groups = [];
let group = null;
const start = (name) => {
  group = { name, n: 0, bad: 0 };
  groups.push(group);
};
const check = (cond, msg) => {
  total += 1;
  group.n += 1;
  if (!cond) {
    failed += 1;
    group.bad += 1;
    if (failed <= 30) console.log(`FAIL ${group.name}: ${msg}`);
  }
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const pat = (id, enabled = true) => ({ id, kind: "pattern", enabled });
const icon = (id = "icons") => ({ id, kind: "icon", enabled: true });
const ICON_STATES = [
  { kind: "off" },
  { kind: "omni" },
  { kind: "empty" },
  { kind: "placing", words: false },
  { kind: "placing", words: true },
  { kind: "error", words: false, retryable: true },
  { kind: "error", words: true, retryable: false },
  { kind: "ready", words: true, rows: 1 },
  { kind: "ready", words: true, rows: 2 },
  { kind: "ready", words: false, rows: 3 },
];

// A small seeded generator: the same cases every run.
let seed = 42;
const rnd = () => {
  seed = (seed * 1103515245 + 12345) % 2147483648;
  return seed / 2147483648;
};
const pick = (xs) => xs[Math.floor(rnd() * xs.length)];
const randomInput = () => {
  const nKept = Math.floor(rnd() * 4);
  const nOwn = Math.floor(rnd() * 6);
  // Every pass numbers its own from "pattern": the ids collide on purpose.
  const ids = ["pattern", "pattern-2", "pattern-3", "pattern-4", "pattern-5", "pattern-6"];
  const kept = Array.from({ length: nKept }, (_, i) => pat(ids[i], rnd() > 0.2));
  if (rnd() > 0.6) kept.push(icon());
  const patterns = Array.from({ length: nOwn }, (_, i) => pat(ids[i], rnd() > 0.3));
  if (rnd() > 0.4) patterns.push(icon());
  const icons = rnd() > 0.15 ? pick(ICON_STATES) : { kind: "none" };
  return { kept, patterns, icons, readOnly: rnd() > 0.8 };
};
const CASES = Array.from({ length: 400 }, randomInput);
// The owner's own: added to v2 (its layer "pattern") with a new "pattern",
// and the icon element of v2 in the kept list (bug 1 and bug 3).
CASES.unshift(
  { kept: [pat("pattern"), icon()], patterns: [pat("pattern"), icon()], icons: { kind: "ready", words: true, rows: 1 } },
  { kept: [], patterns: [], icons: { kind: "placing", words: false } },
  { kept: [], patterns: [pat("pattern", false)], icons: { kind: "off" } },
);

// --- the same lanes whatever the tab or the selection -----------------------------
start("tab x selection never changes the lanes");
const TABS = ["patterns", "icons", "style", undefined];
const SELS = [null, { kind: "pattern", id: "pattern" }, { kind: "icon", id: "code" }, { kind: "kept", id: "pattern" }];
for (const c of CASES.slice(0, 60)) {
  const base = timelineTracks(c);
  for (const tab of TABS) {
    for (const selected of SELS) {
      for (const focused of [null, "p:pattern", "icons"]) {
        const got = timelineTracks({ ...c, tab, selected, focused, openRow: selected?.id ?? null, selIcon: "code" });
        check(eq(got, base), `${tab}/${JSON.stringify(selected)}/${focused}: ${JSON.stringify(got.lanes.map((l) => l.key))}`);
      }
    }
  }
}

// --- one lane per pattern, unique keys ------------------------------------------------
start("every pattern (hidden too) and every kept pattern: exactly one lane");
for (const c of CASES) {
  const { lanes } = timelineTracks(c);
  const keys = lanes.map((l) => l.key);
  check(new Set(keys).size === keys.length, `duplicate keys ${JSON.stringify(keys)}`);
  const keptPats = c.kept.map((e, i) => ({ e, i })).filter(({ e }) => e.kind === "pattern");
  const ownPats = c.patterns.map((e, i) => ({ e, i })).filter(({ e }) => e.kind === "pattern");
  const locked = lanes.filter((l) => l.kind === "pattern" && l.locked);
  const own = lanes.filter((l) => l.kind === "pattern" && !l.locked);
  if (c.readOnly) {
    // Read-only, every lane is locked: kept and own alike, one each.
    check(locked.length === keptPats.length + ownPats.length && own.length === 0, `read-only lanes ${locked.length}`);
  } else {
    check(locked.length === keptPats.length, `kept lanes ${locked.length} want ${keptPats.length}`);
    check(own.length === ownPats.length, `own lanes ${own.length} want ${ownPats.length}`);
    for (const { e, i } of keptPats) {
      const hits = lanes.filter((l) => l.locked && l.index === i && l.id === e.id);
      check(hits.length === 1 && hits[0].hidden === !e.enabled, `kept ${i}:${e.id} → ${hits.length}`);
    }
    for (const { e, i } of ownPats) {
      const hits = lanes.filter((l) => !l.locked && l.kind === "pattern" && l.index === i);
      check(hits.length === 1 && hits[0].id === e.id && hits[0].hidden === !e.enabled, `own ${i}:${e.id} → ${hits.length}`);
    }
  }
  check(lanes.every((l) => l.kind !== "pattern" || l.id !== "icons"), "an icon element became a pattern lane");
  check(lanes.filter((l) => l.kind === "pattern").every((l) => l.height === TRACK_PX.pattern), "pattern lane height");
}

start("kept lanes go by a flag and their own key, never the draft's");
{
  const { lanes } = timelineTracks(CASES[0]);
  const keptLane = lanes.find((l) => l.locked);
  const ownLane = lanes.find((l) => l.kind === "pattern" && !l.locked);
  check(!!keptLane && keptLane.key === keptLaneKey(0, "pattern"), `kept key ${keptLane?.key}`);
  check(!!ownLane && ownLane.key === patternLaneKey("pattern"), `own key ${ownLane?.key}`);
  check(keptLane?.id === ownLane?.id && keptLane?.key !== ownLane?.key, "same id, two keys");
  check(!keptLane?.key.startsWith("kept-"), "no id prefix");
  check(eq(lanes.map((l) => l.kind), ["pattern", "pattern", "icons"]), `kinds ${JSON.stringify(lanes.map((l) => l.kind))}`);
}

// --- the icons' lane ------------------------------------------------------------------
start("the icons' lane: there whenever icons are served, and last");
for (const c of CASES) {
  const { lanes } = timelineTracks(c);
  const icons = lanes.filter((l) => l.kind === "icons");
  if (c.icons.kind === "none") {
    check(icons.length === 0, "a lane for icons the server doesn't draw");
  } else {
    check(icons.length === 1 && icons[0].key === ICON_LANE_KEY, `icon lanes ${icons.length}`);
    check(lanes[lanes.length - 1].kind === "icons", "the icons' lane is not last (it went above the ruler once)");
    check(icons[0].height === iconLanePx(c.icons), "icon lane height");
  }
}
for (const s of ICON_STATES) {
  const { lanes } = timelineTracks({ kept: [], patterns: [pat("pattern")], icons: s });
  check(lanes.some((l) => l.kind === "icons"), `${JSON.stringify(s)} has no lane`);
}
start("the icons' lane heights");
check(iconLanePx({ kind: "none" }) === 0, "none");
check(iconLanePx({ kind: "off" }) === 20, "off: one thin row");
check(iconLanePx({ kind: "placing", words: true }) === 38, "placing with words: speech + a thin row");
check(iconLanePx({ kind: "ready", words: true, rows: 1 }) === 46, "ready, one row: 18 + 28");
check(iconLanePx({ kind: "ready", words: true, rows: 2 }) === 70, "ready, two rows: 18 + 52");
check(iconLanePx({ kind: "ready", words: false, rows: 0 }) === 28, "ready keeps a row");

// --- no pattern: an add row; read-only: locked ------------------------------------------
start("no pattern of its own: Add a pattern; read-only: the same lanes, locked");
for (const c of CASES) {
  const { lanes } = timelineTracks(c);
  const own = c.patterns.filter((e) => e.kind === "pattern").length;
  const adds = lanes.filter((l) => l.kind === "add");
  const empties = lanes.filter((l) => l.kind === "empty");
  if (c.readOnly) {
    check(adds.length === 0, "read-only offers Add a pattern");
    check(lanes.every((l) => l.locked), "read-only lane not locked");
    check(empties.length === (own ? 0 : lanes.some((l) => l.kind === "pattern") ? 0 : 1), `read-only empties ${empties.length}`);
  } else {
    check(adds.length === (own ? 0 : 1), `add rows ${adds.length} with ${own} own`);
    check(empties.length === 0, "an empty row on Next");
    if (adds.length) {
      const i = lanes.indexOf(adds[0]);
      check(lanes.slice(0, i).every((l) => l.kind === "pattern" && l.locked), "the add row sits after the kept lanes");
    }
  }
}
{
  const original = timelineTracks({ kept: [], patterns: [], icons: { kind: "empty" }, readOnly: true });
  check(eq(original.lanes.map((l) => l.kind), ["empty", "icons"]), `original ${JSON.stringify(original.lanes.map((l) => l.kind))}`);
  const v = timelineTracks({ kept: [], patterns: [pat("pattern"), pat("pattern-2"), icon()], icons: { kind: "ready", words: false, rows: 1 }, readOnly: true });
  check(eq(v.lanes.map((l) => [l.kind, l.locked]), [["pattern", true], ["pattern", true], ["icons", true]]), "a version's lanes");
}

// --- the heights and the budget -----------------------------------------------------------
start("the report's sums");
{
  const three = timelineTracks({ kept: [], patterns: [pat("a"), pat("b"), pat("c")], icons: { kind: "ready", words: true, rows: 1 } });
  // ruler 20 + 3 x 28 + speech 18 + blocks 28 + status 18 (+ border and gap)
  check(three.lanesPx === 84 + 46, `3 patterns + icons lanes ${three.lanesPx}`);
  check(three.totalPx === 2 + 20 + 130 + 4 + 18 && three.totalPx <= 176, `3 patterns + icons total ${three.totalPx} (was 242)`);
  const five = timelineTracks({ kept: [pat("k1"), pat("k2")], patterns: [pat("a"), pat("b"), pat("c")], icons: { kind: "ready", words: true, rows: 2 } });
  check(five.totalPx === 2 + 20 + 140 + 70 + 4 + 18 && five.totalPx <= 256, `5 lanes + 2 icon rows total ${five.totalPx} (was 346)`);
  check(five.boxPx === 2 + 20 + 140 + 70, `box ${five.boxPx}`);
}

start("1366x768 and 1920x1080: inside the cap, the picture keeps its room");
// The windows' inner heights: the screen less the browser's bars and the
// taskbar (about 111 px at 768, 130 at 1080), the stepper row 50 px above
// the card.
const SCREENS = [
  { name: "1366x768", inner: 657, offset: 50, minPicture: 180 },
  { name: "1920x1080", inner: 950, offset: 50, minPicture: 380 },
];
for (const s of SCREENS) {
  const col = stageColumnPx(s.inner, s.offset);
  if (s.name === "1366x768") {
    check(col === STAGE_PX.cardMin - STAGE_PX.cardBorder - STAGE_PX.sceneRow - STAGE_PX.footer, `${s.name}: the card clamps to 600 (column ${col})`);
    // What a browser measured there (the real StudioStage and SceneTimeline
    // in the studio's grid): column 497, the timeline capped at 200 (42% of
    // the column's content box), the picture 191 with 5 lanes and 2 icon rows.
    const measured = fitTracks(
      timelineTracks({ kept: [pat("k1"), pat("k2")], patterns: [pat("a"), pat("b"), pat("c")], icons: { kind: "ready", words: true, rows: 2 } }),
      col,
    );
    check(col === 497 && measured.capPx === 200 && measured.needsScroll && measured.picturePx === 191, `${s.name}: measured ${JSON.stringify(measured)}`);
  }
  const cap = laneCap(col);
  check(cap > 0 && cap < col / 2, `${s.name}: cap ${cap} of ${col}`);
  for (const c of CASES) {
    const tr = timelineTracks(c);
    const fit = fitTracks(tr, col);
    check(fit.shownPx <= cap, `${s.name}: shown ${fit.shownPx} > cap ${cap}`);
    check(fit.needsScroll === tr.totalPx > cap, `${s.name}: scroll flag ${fit.needsScroll} for ${tr.totalPx}`);
    check(fit.needsScroll || fit.shownPx === tr.totalPx, `${s.name}: not scrolling but drawn ${fit.shownPx} of ${tr.totalPx}`);
    check(fit.picturePx >= col - STAGE_PX.fixed - cap, `${s.name}: picture ${fit.picturePx}`);
  }
  // The usual draft: three patterns and the icons fit without a scroll and
  // leave the picture well over the 145 px it had at 1366x768.
  const usual = fitTracks(timelineTracks({ kept: [], patterns: [pat("a"), pat("b"), pat("c")], icons: { kind: "ready", words: true, rows: 1 } }), col);
  check(!usual.needsScroll, `${s.name}: 3 patterns + icons scroll (${usual.shownPx} of cap ${usual.capPx})`);
  check(usual.picturePx >= s.minPicture, `${s.name}: picture ${usual.picturePx} < ${s.minPicture}`);
  const busy = fitTracks(timelineTracks({ kept: [pat("k1"), pat("k2")], patterns: [pat("a"), pat("b"), pat("c")], icons: { kind: "ready", words: true, rows: 2 } }), col);
  check(busy.picturePx >= col - STAGE_PX.fixed - cap, `${s.name}: a busy scene leaves ${busy.picturePx}`);
  console.log(`     ${s.name}: column ${col}, cap ${cap}, picture ${usual.picturePx} (3 + icons), ${busy.picturePx} (5 + 2 rows${busy.needsScroll ? ", scrolls" : ""})`);
}

// --- a version's icons, as it froze them ------------------------------------------------------
start("a version's frozen icons on its read-only lane");
{
  const rows = [
    // as the planner freezes one (IconResolved, icon_parity.resolved_item)
    { glyph: { source: "library", id: "code" }, label: "Developer", from_sec: 0.4, settle_sec: 0.7, to_sec: 2.1, fill: "#7f2bf2", ink: "#ffffff", style: "circle", weight: "bold" },
    // an AI-drawn one, an older row with no look
    { glyph: { source: "custom", id: "c-0123456789ab" }, label: "Piscine", from_sec: 1.5, settle_sec: 1.8, to_sec: 3.0 },
    // can't be drawn: no glyph, no time, gone before it starts, not a row
    { label: "nothing", from_sec: 0, to_sec: 1 },
    { glyph: { source: "library", id: "clock" }, label: "Deadline", settle_sec: 1 },
    { glyph: { source: "library", id: "users-three" }, label: "Peer", from_sec: 2, to_sec: 2 },
    null,
    "code",
    // odd values fall back to the 42 badge's look
    { glyph: { source: "library", id: "piggy-bank" }, label: "", from_sec: -0.2, to_sec: 1.2, fill: "violet", ink: 3, style: "sticker", weight: "thin" },
  ];
  const got = frozenIconBlocks(rows);
  check(got.length === 3, `kept ${got.length} of ${rows.length}`);
  check(eq(got.map((b) => b.glyph.id), ["code", "c-0123456789ab", "piggy-bank"]), `order ${JSON.stringify(got.map((b) => b.glyph.id))}`);
  check(new Set(got.map((b) => b.key)).size === got.length, "keys unique");
  const [a, b, c] = got;
  check(a.from === 0.4 && a.to === 2.1 && a.style === "circle" && a.weight === "bold" && a.label === "Developer", `row 1 ${JSON.stringify(a)}`);
  check(b.glyph.source === "custom" && b.style === "badge" && b.fill === "#7f2bf2" && b.ink === "#ffffff" && b.weight === null, `row 2 ${JSON.stringify(b)}`);
  check(c.from === 0 && c.label === "piggy-bank" && c.style === "badge" && c.fill === "#7f2bf2" && c.ink === "#ffffff" && c.weight === null, `row 3 ${JSON.stringify(c)}`);
  check(eq(frozenIconBlocks(null), []) && eq(frozenIconBlocks(undefined), []) && eq(frozenIconBlocks([]), []), "none");
  // Their tracks: none given (or not one per row), not known; given, each
  // row's, closed up over the rows left out (round 3, V1).
  check(got.every((x) => x.lane === null), "no tracks: not known");
  check(frozenIconBlocks(rows, [0, 1]).every((x) => x.lane === null), "not one per row: not known");
  const lanes = [0, 2, 0, 0, 0, 0, 0, 3];
  check(eq(frozenIconBlocks(rows, lanes).map((x) => x.lane), [0, 1, 2]), `closed up ${JSON.stringify(frozenIconBlocks(rows, lanes).map((x) => x.lane))}`);
  // A version's lanes with them: read-only, the icons' lane last.
  const v = timelineTracks({ kept: [], patterns: [pat("pattern")], icons: { kind: "ready", words: false, rows: 1 }, readOnly: true });
  check(eq(v.lanes.map((l) => [l.kind, l.locked]), [["pattern", true], ["icons", true]]), "a version with icons");
}

// --- scrolling a lane into view ---------------------------------------------------------------
start("a lane scrolled into view under the sticky ruler");
{
  const R = TRACK_PX.ruler;
  // A box 120 px tall, scrolled to 0: a lane at 40..68 is in view.
  check(scrollToShow(40, 28, 0, 120, R) === 0, "in view stays");
  // Below the fold: its bottom comes to the box's bottom.
  check(scrollToShow(150, 28, 0, 120, R) === 58, `below ${scrollToShow(150, 28, 0, 120, R)}`);
  // Under the sticky ruler: its top comes just under it.
  check(scrollToShow(48, 28, 40, 120, R) === 28, `under the ruler ${scrollToShow(48, 28, 40, 120, R)}`);
  // Taller than the box: its top.
  check(scrollToShow(100, 200, 0, 120, R) === 80, "tall lane shows its top");
  // Never above the start.
  check(scrollToShow(10, 28, 50, 120, R) === 0, "never negative");
  for (let i = 0; i < 300; i++) {
    const view = 60 + Math.floor(rnd() * 200);
    const h = 18 + Math.floor(rnd() * 60);
    const top = R + Math.floor(rnd() * 400);
    const scroll = Math.floor(rnd() * 400);
    const got = scrollToShow(top, h, scroll, view, R);
    if (h <= view - R) check(top >= got + R - 1e-9 && top + h <= got + view + 1e-9, `lane ${top}+${h} not shown at ${got} in ${view}`);
    check(got >= 0, "negative scroll");
  }
}

// --- a block dragged to a new track: the timeline keeps its height ---------------------------
start("a drag to a new track never grows the timeline mid-drag");
{
  for (const count of [1, 2, 3, 4]) {
    const still = iconBlocksArea(count, null, 4);
    // The lane's model height is the blocks area's (the speech row aside).
    check(still.heightPx === iconLanePx({ kind: "ready", words: false, rows: count }), `rows ${count} height`);
    for (const live of [0, count - 1, count, count + 1]) {
      const a = iconBlocksArea(count, live, 4);
      check(a.heightPx === still.heightPx, `rows ${count}, dragged to ${live}: ${a.heightPx} != ${still.heightPx}`);
      check(a.newTrack === (live >= count && count <= 4), `rows ${count}, dragged to ${live}: new track ${a.newTrack}`);
      check(a.newTop >= 0 && a.newTop + TRACK_PX.iconBar <= a.heightPx, `the new track's band inside the lane (${count})`);
    }
  }
  check(!iconBlocksArea(5, 5, 4).newTrack, "no new track past the most tracks");
}

// --- a pattern's bar picked on the timeline -----------------------------------------------------
start("a click on a pattern from the Icons tab rests on its moment");
{
  check(eq(patternPick("click", true), { openRow: true, restOnMoment: true }), "click while the icons play");
  check(eq(patternPick("click", false), { openRow: true, restOnMoment: false }), "click on the still");
  check(eq(patternPick("focus", true), { openRow: false, restOnMoment: false }), "a focus only picks it");
  check(eq(patternPick("focus", false), { openRow: false, restOnMoment: false }), "a focus on the still");
  // The studio uses it (and the stage rests on the moment when it says so).
  const studio = readFileSync(new URL("../src/components/yas/rebrand-studio/RebrandStudio.tsx", import.meta.url), "utf-8");
  check(/patternPick\(how, iconsMode/.test(studio) && /restOnMoment\) setAtMoment\(true\)/.test(studio),
    "RebrandStudio picks a bar through patternPick");
  // Next's lanes and a version's read-only ones are two instances (keys):
  // leaving Next unmounts its lanes, which clears their status-line note.
  const keys = [...studio.matchAll(/<PatternLanes\b[^>]*?\bkey="([^"]+)"/gs)].map((m) => m[1]);
  check(keys.length === 2 && keys[0] !== keys[1], `PatternLanes keys ${JSON.stringify(keys)}`);
}

for (const g of groups) console.log(`${g.bad ? "FAIL" : "ok  "} ${g.name} (${g.n})`);
console.log(failed ? `${failed} of ${total} failed` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
