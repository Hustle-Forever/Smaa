// Checks the times and labels the studio says for each icon against the
// real planner's plans (backend/tests/fixtures/rebrand_icon_times.json,
// written by tests/test_icons_lanes.py from elements/icon.plan):
//
//   * one time per icon (the round-2 QA, R5: the list said "Moved to 1.5 s",
//     the Moments chip 1.9 s and the block started at 1.23 s for Team): the
//     list, the chips and the notes say its settle (rebrandIcons.iconTime),
//     its block marks that settle (settleMark: the block itself starts at
//     the entrance), and a click rests the stage on it (momentOf);
//   * "Show at Y s anyway" offers the moment it was added at (R4: 0.2 s for
//     Team, not 0.0 s), else its word (anywayOf);
//   * who each one is (iconTag): a library add the AI picked too is "yours"
//     (R6), Keep on the AI's pick is "kept";
//   * two added at one moment are on two tracks (R3, laneRows);
//   * a version's read-only timeline has its icons on the tracks its draft
//     had (round 3, V1: v2 showed Brain on the first and Idea on the second,
//     the reverse of the draft): the tracks kept at Generate
//     (options.icon_lanes), else worked out from its recipe as the planner
//     puts them (frozenLanesOf), and a "Keep v3's icons" draft has v3's;
//   * an idea in Suggested says the time its icon shows at, as the list does
//     (round 3, V2: 6.0 s there, 5.9 s in the list), and one not placed when
//     its word is said ("said at"): ideaTime;
//   * the components use these (their source says so).
//
//   node --experimental-strip-types scripts/check-rebrand-icon-times.mjs
//
// (node 22.6+; the backend test runs it too when node is there.)
// rebrandIcons.ts imports its neighbours without an extension (as Vite
// does): a resolve hook adds ".ts". Prints a line per group and exits 1 on
// any failure.

import { readFileSync } from "node:fs";
import { register } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

register(
  "data:text/javascript," +
    encodeURIComponent(
      `export async function resolve(s, c, n) {
        if ((s.startsWith("./") || s.startsWith("../")) && !/[.][a-z]+$/i.test(s)) {
          try { return await n(s + ".ts", c); } catch {}
        }
        return n(s, c);
      }`,
    ),
);

const I = await import(new URL("../src/lib/rebrandIcons.ts", import.meta.url).href);
const { laneRows } = await import(new URL("../src/lib/rebrandSpan.ts", import.meta.url).href);
const { frozenIconBlocks } = await import(new URL("../src/lib/rebrandTracks.ts", import.meta.url).href);
const { frozenLanesOf, keptItemLanes } = await import(new URL("../src/lib/rebrandBuildOn.ts", import.meta.url).href);
const { COPY } = await import(new URL("../src/components/yas/rebrand-studio/studioCopy.ts", import.meta.url).href);

const here = dirname(fileURLToPath(import.meta.url));
const fx = JSON.parse(readFileSync(resolve(here, "../../backend/tests/fixtures/rebrand_icon_times.json"), "utf-8"));
const src = (p) => readFileSync(resolve(here, "../src/components/yas/rebrand-studio", p), "utf-8");

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
const plan = (prefix) => {
  const p = fx.plans.find((x) => x.name.startsWith(prefix));
  if (!p) throw new Error(`no plan "${prefix}" in the fixture`);
  return p;
};
const itemOf = (p, label) => p.items.find((it) => it.label === label);
const anywayLabel = (it) => COPY.icons.showAnyway(I.fmtSec(I.anywayOf(it.shifted).label));

// --- one time per icon ----------------------------------------------------------------
start("one time per icon: the list, the chips, the notes, the block");
for (const p of fx.plans) {
  for (const it of p.items) {
    const said = I.fmtSec(I.iconTime(it));
    check(said === I.fmtSec(it.settle), `${it.label}: ${said} is not its settle`);
    // The block starts at the entrance; its mark is where the time said is.
    const mark = I.settleMark(it);
    check(Math.abs(it.from + mark - it.settle) < 1e-9, `${it.label}: the mark is at ${it.from + mark}, not ${it.settle}`);
    check(mark > 0 && mark < it.to - it.from, `${it.label}: the mark ${mark} is not inside its block`);
    // A click rests the stage on it, shown.
    const rest = I.momentOf(it);
    check(rest >= it.settle && rest < it.to, `${it.label}: the stage rests at ${rest}, outside ${it.settle}..${it.to}`);
    if (it.shifted) {
      // The planner's note and the studio's say the same time as the list.
      check(it.notes.some((n) => n.startsWith(`'${it.label}' moved to ${said}, `)), `${it.label}: the note says ${it.notes}`);
      check(COPY.icons.shifted(said, it.shifted.why).startsWith(`Moved to ${said}, `), `${it.label}: the studio's note`);
    }
  }
}

// A version's own icons (read only) say the same time: the one they froze.
start("a version's icons, as it froze them");
{
  const lib = (id) => ({ source: "library", id });
  const [a, b, c] = frozenIconBlocks([
    { glyph: lib("users-three"), label: "Team", from_sec: 1.2333, settle_sec: 1.5, to_sec: 3.3 },
    { glyph: lib("code"), label: "Code", from_sec: 2.0, to_sec: 3.0 },
    { glyph: lib("brain"), label: "Brain", from_sec: 4.0, settle_sec: 3.5, to_sec: 5.0 },
  ]);
  check(a.settle === 1.5 && I.fmtSec(a.settle) === "1.5 s", `settle ${a.settle}`);
  check(b.settle === null, "a row with no settle: none");
  check(c.settle === null, "a settle outside its block: none");
}

// --- the round-2 QA's scene -------------------------------------------------------------
start("run 830 scene_0 (the round-2 QA)");
{
  const p = plan("run 830 scene_0");
  const team = itemOf(p, "Team");
  check(I.fmtSec(I.iconTime(team)) === "1.5 s", `Team's time ${I.fmtSec(I.iconTime(team))}`);
  check(I.fmtSec(team.moment_t) === "1.9 s", "the plan's exact still is still a moment later");
  check(I.fmtSec(I.iconTime(team)) !== I.fmtSec(team.moment_t), "the chip says the settle, not the still (1.9 s)");
  check(team.from < 1.3, "its block starts at its entrance");
  check(anywayLabel(team) === "Show at 0.2 s anyway", `R4: ${anywayLabel(team)}`);
  check(I.anywayOf(team.shifted).sec === 0.2, `R4: pinned at ${I.anywayOf(team.shifted).sec}`);
  check(COPY.icons[I.iconTag(team)] === COPY.icons.yours, `R6: Team is ${I.iconTag(team)}`);
  const idea = itemOf(p, "Idea");
  const brain = itemOf(p, "Brain");
  check(I.fmtSec(I.iconTime(idea)) === "4.0 s" && I.fmtSec(I.iconTime(brain)) === "4.0 s", "R3: both at 4.0 s");
  const spans = p.items.map((it) => ({ from: it.from, to: it.to, lane: it.lane }));
  const { rows, count } = laneRows(spans);
  const at = (label) => rows[p.items.findIndex((it) => it.label === label)];
  check(at("Idea") !== at("Brain") && count === 2, `R3: Idea and Brain on rows ${at("Idea")} and ${at("Brain")} of ${count}`);
  check(!brain.shifted && brain.notes.length === 0, "R3: Brain is where it was added: no note");
  check([idea, brain].every((it) => I.iconTag(it) === "yours"), "both yours");
  const projects = p.ideas.find((i) => i.label === "Projects");
  check(projects && projects.reason === "Gives way to your 'Brain'", `the AI's Projects: ${projects && projects.reason}`);
}

// --- "Show at Y s anyway" ------------------------------------------------------------------
start("Show at Y s anyway");
{
  const zebra = itemOf(plan("never said"), "Zebra");
  check(zebra.shifted && zebra.shifted.said === null, "never said: no word");
  check(anywayLabel(zebra) === "Show at 0.1 s anyway" && I.anywayOf(zebra.shifted).sec === 0.1, anywayLabel(zebra));
  check(I.fmtSec(I.iconTime(zebra)) === "1.6 s", `moved to ${I.fmtSec(I.iconTime(zebra))}`);
  const future = itemOf(plan("the owner's Future card"), "Future");
  check(anywayLabel(future) === "Show at 0.2 s anyway", `no moment: its word (${anywayLabel(future)})`);
  check(I.anywayOf(future.shifted).sec === future.shifted.at, "no moment: pinned at the settle its word gives");
  // Dropped on a word the user picked: that word is offered, not the moment
  // it was added at (4.0 s would take it off the word, back where it was).
  const dropped = itemOf(plan("never said, added at 4.0 s, then dropped"), "Zebra");
  check(dropped.word === "Peer" && !("asked" in dropped.shifted), JSON.stringify(dropped.shifted));
  check(anywayLabel(dropped) === "Show at 0.0 s anyway" && I.anywayOf(dropped.shifted).sec === 0,
    `a picked word: its word (${anywayLabel(dropped)})`);
  // The type's every shape.
  check(JSON.stringify(I.anywayOf({ at: 0.13, said: 0.2 })) === JSON.stringify({ label: 0.2, sec: 0.13 }), "said, no moment");
  check(JSON.stringify(I.anywayOf({ at: 0, said: 0, asked: 0.2 })) === JSON.stringify({ label: 0.2, sec: 0.2 }), "said, a moment");
  check(JSON.stringify(I.anywayOf({ at: 0.1, said: null, asked: 0.1 })) === JSON.stringify({ label: 0.1, sec: 0.1 }), "never said");
  check(JSON.stringify(I.anywayOf({ at: 0.13, said: 0.2, asked: null })) === JSON.stringify({ label: 0.2, sec: 0.13 }), "asked null");
}

// --- who it is ------------------------------------------------------------------------------
start("who it is");
{
  const peer = itemOf(plan("Keep on the AI's Peer pick"), "Peer");
  check(I.iconTag(peer) === "keptPick", `Keep: ${I.iconTag(peer)}`);
  const base = { off_word: false, source: "ai", pinned: false, why: { by: "ai" } };
  const cases = [
    [{ ...base, off_word: true, source: "user", why: { by: "you" } }, "offWord"],
    [{ ...base, source: "user", why: { by: "kept" } }, "keptPick"],
    [{ ...base, source: "user", why: { by: "team" } }, "teamTag"],
    [{ ...base, source: "user", why: { by: "you" } }, "yours"],
    [{ ...base, source: "kept", why: { by: "version" } }, "kept"],
    [{ ...base, pinned: true }, "pinned"],
    [{ ...base, source: "keyword", why: { by: "keyword" } }, "keyword"],
    [base, "ai"],
    [{ ...base, why: null }, "ai"],
  ];
  for (const [it, want] of cases) check(I.iconTag(it) === want, `${JSON.stringify(it)}: ${I.iconTag(it)}, not ${want}`);
  for (const [, want] of cases) check(typeof COPY.icons[want] === "string", `COPY.icons.${want}`);
}

// --- a version's tracks (round 3, V1) ---------------------------------------------------------
// A version as the server lists it: its recipe's icon element (what the user
// did, and the icons Generate froze), and the tracks kept beside them.
const versionOf = (p, { id = 2, stored = true, props = null, base = null } = {}) => ({
  id,
  options: stored && p.icon_lanes && p.icon_lanes.some((n) => n > 0) ? { icon_lanes: p.icon_lanes } : {},
  recipe: { elements: [{ id: "icons", kind: "icon", enabled: true, props: { ...(props ?? p.props), resolved: p.frozen } }] },
  base,
});
// The row each icon is drawn on, by glyph id.
const rowsById = (spans) => {
  const { rows, count } = laneRows(spans);
  return { count, by: Object.fromEntries(spans.map((s, i) => [s.glyph.id, rows[i]])) };
};
const draftRows = (p) => rowsById(p.items.map((it) => ({ glyph: it.glyph, from: it.from, to: it.to, lane: it.lane })));

start("a version's tracks are its draft's (V1)");
{
  // The QA: v2 froze Brain before Idea (the planner's order: their settle,
  // then the id), and the read-only lane stacked them as they come: Brain on
  // the first track, the reverse of the draft.
  const p = plan("run 830 scene_0");
  const draft = draftRows(p);
  check(draft.by.lightbulb === 0 && draft.by.brain === 1, `the draft: Idea ${draft.by.lightbulb}, Brain ${draft.by.brain}`);
  check(p.frozen[1].label === "Brain" && p.frozen[2].label === "Idea", "frozen: Brain before Idea");
  const stacked = rowsById(frozenIconBlocks(p.frozen));
  check(stacked.by.brain === 0 && stacked.by.lightbulb === 1, "with no tracks they stack as they come (what v2 showed)");
  for (const stored of [true, false]) {
    const v = versionOf(p, { stored });
    const got = rowsById(frozenIconBlocks(p.frozen, frozenLanesOf(v, [v])));
    const how = stored ? "kept at Generate" : "worked out (made before they were kept)";
    check(got.by.lightbulb === 0 && got.by.brain === 1 && got.by["users-three"] === 0, `${how}: ${JSON.stringify(got.by)}`);
    check(got.count === draft.count, `${how}: ${got.count} tracks, the draft ${draft.count}`);
  }
  check(JSON.stringify(p.icon_lanes) === JSON.stringify([0, 1, 0]), `kept beside the frozen icons: ${JSON.stringify(p.icon_lanes)}`);
}

start("every plan: the version's tracks are the planner's (V1)");
for (const p of fx.plans) {
  const draft = draftRows(p);
  check(p.frozen.length === p.items.length, `${p.name}: froze ${p.frozen.length} of ${p.items.length}`);
  for (const stored of [true, false]) {
    const v = versionOf(p, { stored });
    const got = rowsById(frozenIconBlocks(p.frozen, frozenLanesOf(v, [v])));
    const how = stored ? "kept" : "worked out";
    check(JSON.stringify(got.by) === JSON.stringify(draft.by) && got.count === draft.count,
      `${p.name} (${how}): ${JSON.stringify(got.by)} of ${got.count}, the draft ${JSON.stringify(draft.by)} of ${draft.count}`);
  }
}

start("kept from another version, added on top, odd rows (V1)");
{
  const p = plan("run 830 scene_0");
  const lanesBy = (lanes, rows) => Object.fromEntries(rows.map((r, i) => [r.glyph.id, lanes[i]]));
  const v3 = versionOf(p, { id: 3 });
  // "Keep v3's icons": v4 froze v3's own, nothing kept beside them, and its
  // own recipe says nothing of who added them.
  const v4 = versionOf(p, { id: 4, stored: false, props: { keep: 3 } });
  const want = lanesBy(frozenLanesOf(v3, [v3]), p.frozen);
  check(JSON.stringify(lanesBy(frozenLanesOf(v4, [v3, v4]), p.frozen)) === JSON.stringify(want), "v4 has v3's tracks");
  // v3 made before tracks were kept: v4 still has v3's (worked out from v3).
  const old3 = versionOf(p, { id: 3, stored: false });
  check(JSON.stringify(lanesBy(frozenLanesOf(v4, [old3, v4]), p.frozen)) === JSON.stringify(want), "an older v3 too");
  // A keep that points at itself, or at a version not listed: its own recipe.
  const loop = versionOf(p, { id: 5, stored: false, props: { ...p.props, keep: 5 } });
  check(JSON.stringify(lanesBy(frozenLanesOf(loop, [loop]), p.frozen)) === JSON.stringify(want), "no loop");
  // Added on top of v3 with no icon element of its own: v3's icons, v3's tracks.
  const top = { id: 6, options: {}, recipe: { elements: [] }, base: { version_id: 3, mode: "stack" } };
  check(JSON.stringify(frozenLanesOf(top, [v3, top])) === JSON.stringify(frozenLanesOf(v3, [v3])), "a pass on top");
  check(frozenLanesOf({ id: 9, options: {}, recipe: { elements: [] }, base: null }, []) === null, "no icons: none");
  // The draft that keeps them (the plan's `kept`): v3's tracks on its items.
  const items = p.items.map((it) => ({ ...it, lane: null, source: "kept" }));
  const kept = keptItemLanes(items, 3, [v3]);
  check(kept.every((it) => it.lane === want[it.glyph.id]), `the keep draft: ${JSON.stringify(kept.map((it) => it.lane))}`);
  check(keptItemLanes(p.items, null, [v3]) === p.items, "a plan of its own: as it is");
  // Tracks kept that don't fit the rows are not used (worked out instead).
  for (const bad of [[0, 1], [0, -1, 0], [0, 1.5, 0], ["0", "1", "0"], null, 3]) {
    const v = { ...versionOf(p, { stored: false }), options: { icon_lanes: bad } };
    check(JSON.stringify(lanesBy(frozenLanesOf(v, [v]), p.frozen)) === JSON.stringify(want), `kept ${JSON.stringify(bad)}`);
  }
  // A row that can't be drawn leaves no empty track: Brain (the only one on
  // track 1) gone, the rest on track 0; Yak gone from track 1, Quill closes
  // up from track 2.
  const rows = p.frozen.map((r) => (r.label === "Brain" ? { ...r, to_sec: r.from_sec } : r));
  const drawn = frozenIconBlocks(rows, [0, 1, 0]);
  check(drawn.length === 2 && drawn.every((b) => b.lane === 0), `gone: ${JSON.stringify(drawn.map((b) => [b.label, b.lane]))}`);
  const three = plan("three never said");
  const noYak = three.frozen.map((r) => (r.label === "Yak" ? null : r));
  const left = Object.fromEntries(frozenIconBlocks(noYak, three.icon_lanes).map((b) => [b.label, b.lane]));
  check(JSON.stringify(left) === JSON.stringify({ Team: 0, Quill: 1, Zebra: 0 }), `closed up: ${JSON.stringify(left)}`);
}

// --- Suggested's time (round 3, V2) -------------------------------------------------------------
start("an idea says the time its icon shows at (V2)");
for (const p of fx.plans) {
  for (const idea of p.ideas) {
    const t = I.ideaTime(idea, p.items);
    const it = p.items.find((x) => x.glyph.id === idea.glyph.id);
    if (it) {
      check(t && !t.said && I.fmtSec(t.t) === I.fmtSec(I.iconTime(it)), `${p.name} ${idea.label}: ${JSON.stringify(t)}, the list ${I.fmtSec(I.iconTime(it))}`);
    } else if (idea.at != null) {
      check(t && t.said && t.t === idea.at, `${p.name} ${idea.label} (not placed): ${JSON.stringify(t)}`);
      check(COPY.icons.saidAt(I.fmtSec(idea.at)) === `said at ${I.fmtSec(idea.at)}`, "said at");
    } else {
      check(t === null, `${p.name} ${idea.label}: nothing to say, ${JSON.stringify(t)}`);
    }
  }
}
{
  // As the QA saw it: the AI's Code, said at 4.7 s ("coding"), shows at
  // 4.9 s (after Free's clear air); Suggested said 4.7 s, the list 4.9 s.
  const p = plan("the AI's own");
  const idea = p.ideas.find((i) => i.label === "Code");
  const it = itemOf(p, "Code");
  check(I.fmtSec(idea.at) === "4.7 s" && I.fmtSec(I.iconTime(it)) === "4.9 s", `the word ${I.fmtSec(idea.at)}, the list ${I.fmtSec(I.iconTime(it))}`);
  const said = I.ideaTime(idea, p.items);
  check(said && !said.said && I.fmtSec(said.t) === "4.9 s", `Suggested: ${JSON.stringify(said)}`);
  // Team, said at 0.0 s ("Peer", in the opening), shows at 1.5 s.
  const qa = plan("run 830 scene_0");
  const team = I.ideaTime(qa.ideas.find((i) => i.label === "Team"), qa.items);
  check(team && !team.said && I.fmtSec(team.t) === "1.5 s", `Team: ${JSON.stringify(team)}`);
  // Left out (no room): when it is said, labelled so.
  const out = plan("run 830 scene_0").ideas.find((i) => i.label === "Projects");
  const left = I.ideaTime(out, plan("run 830 scene_0").items);
  check(out.state === "no_room" && left && left.said && left.t === out.at, `left out: ${JSON.stringify(left)}`);
  check(I.ideaTime({ glyph: { source: "library", id: "x" }, at: null }, []) === null, "never said, not shown: no time");
  check(I.ideaTime({ glyph: { source: "library", id: "x" }, at: 2 }, null).said, "no plan yet: said at");
}

// --- the components say them ------------------------------------------------------------------
start("the components use them");
{
  const studio = src("RebrandStudio.tsx");
  const tab = src("IconsTab.tsx");
  const lane = src("IconLane.tsx");
  const chips = studio.slice(studio.indexOf("const momentsRow"), studio.indexOf("const selItem"));
  check(chips.includes("fmtSec(iconTime(it))") && !chips.includes("fmtSec(at)"), "the Moments chips say the settle");
  check(!/shifted[!]?\.said/.test(studio) && !/shifted[!]?\.said/.test(tab), "no anyway reads the word's time itself");
  check(studio.includes("anywayOf(") && tab.includes("anywayOf("), "both anyways use anywayOf");
  check(tab.includes("iconTag(it)"), "the list's chip is iconTag");
  check(tab.includes("fmtSec(iconTime(it))"), "the list says the settle");
  check(lane.includes("settleMark(it)"), "the block marks the settle");
  const ro = lane.slice(lane.indexOf("export function IconLaneStatic"));
  check(ro.includes("fmtSec(b.settle)"), "a version's block says its settle");
  // V1: a version's lane draws its icons on their tracks; a keep draft has
  // the kept version's.
  check(/frozenIconBlocks\(\s*frozenIconsOf\(roVersion, knownVersions\),\s*frozenLanesOf\(roVersion, knownVersions/.test(studio),
    "the read-only lane gets the version's tracks");
  check(/keptItemLanes\(/.test(studio.slice(studio.indexOf("const iconItems"), studio.indexOf("const iconItems") + 400)),
    "a keep draft's items get the kept version's tracks");
  // V2: Suggested and yours-not-on-screen say ideaTime, never the word's
  // time alone.
  const picker = src("IconPicker.tsx");
  check(picker.includes("ideaTime(") && !picker.includes("fmtSec(idea.at)"), "Suggested says ideaTime");
  const idleRow = tab.slice(tab.indexOf("function IdeaRow"));
  check(idleRow.includes("ideaTime(") && !idleRow.includes("fmtSec(idea.at)"), "yours not on screen say ideaTime");
}

for (const g of groups) console.log(`${g.bad ? "FAIL" : "ok  "} ${g.name} (${g.n})`);
console.log(failed ? `${failed} of ${total} failed` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
