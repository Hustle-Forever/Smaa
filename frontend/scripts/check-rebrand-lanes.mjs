// Checks the icons' tracks and adds in src/lib/rebrandIcons.ts, as the
// planner reads them (backend elements/icon.py: TRACKS, recipe.py IconProps):
//   * an add carries the moment it was added at (IconAdd.at: the playhead,
//     to the hundredth, 0 to 600 s), and only then;
//   * setLane writes a track of 1 and up, deletes the key for track 0, keeps
//     at most 8 (past that the recipe is returned as it was: nothing pushed
//     out), and the planner's track caps it;
//   * removing, resetting and swapping an icon carry its track as its pins;
//   * a pin never pushes another out: 8 icons may carry one (the user's five
//     and the AI's three); a ninth is not made, a removed icon's stale pin
//     goes first;
//   * "show this one first" is track 0's order (one on a track of its own is
//     moved on the timeline).
//
//   node --experimental-strip-types scripts/check-rebrand-lanes.mjs
//
// (node 22.6+.) rebrandIcons.ts imports its neighbours without an extension
// (as Vite does): a resolve hook adds ".ts". Prints a line per group and
// exits 1 on any failure.

import { register } from "node:module";

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

const draft = (props = {}) => ({
  engine: "house",
  target: "scene_0",
  preset: null,
  elements: [{ id: "icons", kind: "icon", enabled: true, placement: { anchor: "auto" }, animation: { entrance: "auto" }, props }],
});
const propsOf = (r) => I.iconProps(I.iconsOf(r));
const lib = (id) => ({ source: "library", id });
const item = (id, lane, settle) => ({
  glyph: lib(id),
  label: id,
  source: "user",
  pinned: false,
  off_word: false,
  from: settle - 0.27,
  settle,
  to: settle + 1.2,
  box: { x: 0, y: 0, w: 0.1, h: 0.1 },
  word: null,
  w: null,
  purpose: "",
  priority: 0,
  mentions: [],
  moment_t: settle + 0.4,
  lane,
});

// --- adds at the playhead ------------------------------------------------------------
start("an add carries its moment");
{
  const r = I.addIcon(draft(), lib("rocket-launch"), "Future", null, 1.234);
  check(eq(propsOf(r).added, [{ glyph: lib("rocket-launch"), label: "Future", at: 1.23 }]), JSON.stringify(propsOf(r).added));
  const none = I.addIcon(draft(), lib("rocket-launch"), "Future");
  check(!("at" in propsOf(none).added[0]), "no moment: no `at` (the key of every other add holds)");
  for (const bad of [null, undefined, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
    const got = I.addIcon(draft(), lib("x"), "X", null, bad);
    check(!("at" in propsOf(got).added[0]), `a moment of ${bad} is none`);
  }
  check(propsOf(I.addIcon(draft(), lib("x"), "X", null, 900)).added[0].at === 600, "at most 600 s (the server's limit)");
  // Added again: unchanged (one per glyph), whatever the playhead.
  const twice = I.addIcon(r, lib("rocket-launch"), "Future", null, 5);
  check(twice === r, "the same icon twice is one add");
  // The sixth: none (five a scene).
  let full = draft();
  for (let k = 0; k < 6; k++) full = I.addIcon(full, lib(`i${k}`), `I${k}`, null, k);
  check(propsOf(full).added.length === I.ADDED_MAX, `five at most: ${propsOf(full).added.length}`);
  // Change icon keeps the moment (a never-said Idea at 6.6 s became Brain
  // at the earliest clear time, 2.6 s earlier, with no note).
  const idea = I.addIcon(draft(), lib("lightbulb"), "Idea", null, 6.6);
  const brain = I.swapIcon(idea, item("lightbulb", 0, 6.467), lib("brain"), "Brain", null);
  check(eq(propsOf(brain).added, [{ glyph: lib("brain"), label: "Brain", at: 6.6 }]),
    `a swap keeps the moment ${JSON.stringify(propsOf(brain).added)}`);
  const plain = I.swapIcon(none, item("rocket-launch", 0, 2), lib("brain"), "Brain", null);
  check(!("at" in propsOf(plain).added[0]), "a swap of an add with no moment makes none");
  const ai = I.swapIcon(draft(), item("users-three", 0, 1.5), lib("brain"), "Brain", null);
  check(!("at" in propsOf(ai).added[0]), "a swap of the AI's pick (not an add) makes none");
}

// --- tracks -----------------------------------------------------------------------------
start("a track the user picked");
{
  const r = I.setLane(draft(), "code", 2);
  check(eq(propsOf(r).lanes, { code: 2 }), JSON.stringify(propsOf(r).lanes));
  check(I.laneSetOf(r, "code") === 2 && I.laneSetOf(r, "free") === null, "laneSetOf");
  const back = I.setLane(r, "code", 0);
  check(!("lanes" in I.iconsOf(back).props), "track 0 is no pin: the key goes (and the empty map)");
  check(I.setLane(draft(), "code", 99) && propsOf(I.setLane(draft(), "code", 99)).lanes.code === I.LANE_MAX, "capped");
  check(propsOf(I.setLane(draft(), "code", -3)).lanes.code === undefined, "below 0 is track 0");
  check(propsOf(I.setLane(draft(), "code", 1.6)).lanes.code === 2, "a whole track");
  // At most 8 icons carry one: a ninth is not made, none pushed out.
  let many = draft();
  for (let k = 0; k < 8; k++) many = I.setLane(many, `g${k}`, 1);
  const ninth = I.setLane(many, "g8", 1);
  check(ninth === many, "a ninth track pin is not made");
  check(Object.keys(propsOf(I.setLane(many, "g3", 2)).lanes).length === 8, "an existing one moves");
  check(I.hasTimingPins(r) && I.hasIconEdits(r), "a track is a pin (Auto and Reset all have work)");
  check(!("lanes" in I.iconsOf(I.resetOrder(r)).props), "Auto: the tracks go");
  check(!("lanes" in I.iconsOf(I.resetAllIcons(r)).props), "Reset all: the tracks go");
  check(!("lanes" in I.iconsOf(I.resetIcon(r, "code")).props), "Reset this icon: its track goes");
  const removed = I.removeIcon(I.setTweak(r, "code", { hold_sec: 2 }), lib("code"));
  check(!("lanes" in I.iconsOf(removed).props) && !("tweaks" in I.iconsOf(removed).props), "removed: its pins and track go");
  const swapped = I.swapIcon(I.addIcon(r, lib("code"), "Code"), item("code", 2, 3), lib("terminal"), "Terminal", null);
  check(eq(propsOf(swapped).lanes, { terminal: 2 }), `a swap keeps the track ${JSON.stringify(propsOf(swapped).lanes)}`);
}

start("an item's track");
{
  check(I.laneOf(item("a", 2, 1)) === 2 && I.laneOf(item("a", 0, 1)) === 0, "its lane");
  check(I.laneOf(item("a", null, 1)) === 0 && I.laneOf({}) === 0 && I.laneOf(null) === 0, "none: track 0");
  const items = [item("c", 0, 5), item("b", 1, 3), item("a", 0, 2)];
  check(eq(I.trackOrder(items).map((x) => x.glyph.id), ["a", "c"]), "track 0, in speech order");
  check(eq(I.speechOrder(items).map((x) => x.glyph.id), ["a", "b", "c"]), "every one in speech order");
}

// --- the pin cap -------------------------------------------------------------------------
start("a pin never pushes another out");
{
  let r = draft();
  for (let k = 0; k < 8; k++) r = I.setTweak(r, `g${k}`, { hold_sec: 1 + k / 10 });
  check(Object.keys(propsOf(r).tweaks).length === 8, "eight icons carry a pin");
  const ninth = I.setTweak(r, "g8", { hold_sec: 2 });
  check(ninth === r, "a ninth is not made (the oldest is kept)");
  const again = I.setTweak(r, "g0", { hold_sec: 3 });
  check(propsOf(again).tweaks.g0.hold_sec === 3 && Object.keys(propsOf(again).tweaks).length === 8, "an existing pin changes");
  // A removed icon's stale pin goes first.
  const stale = { ...r, elements: [{ ...r.elements[0], props: { ...r.elements[0].props, dismissed: ["g2"] } }] };
  const made = I.setTweak(stale, "g8", { hold_sec: 2 });
  check(made !== stale && !("g2" in propsOf(made).tweaks) && "g8" in propsOf(made).tweaks, "the stale one made room");
  check(Object.keys(propsOf(made).tweaks).length === 8, "still eight");
}

// --- reorder: track 0's order ----------------------------------------------------------------
start("show this one first: track 0");
{
  const plan = {
    items: [item("a", 0, 3), item("own", 1, 3.5), item("b", 0, 6)],
    timeline: { seconds: 10, zones: [], words: [], voiced: null, envelope: [], chunks: [] },
  };
  plan.items[0].mentions = [{ w: 1, at: 3.07 }];
  plan.items[2].mentions = [{ w: 2, at: 1.0 }, { w: 9, at: 6.07 }];
  plan.items[2].w = 9;
  const r = draft({ added: plan.items.map((x) => ({ glyph: x.glyph, label: x.label })) });
  // b to the front of track 0 (places 1 -> 0): its earlier mention.
  const got = I.reorder(r, plan, 1, 0);
  check(got.kind === "moved" && propsOf(got.recipe).tweaks.b?.w === 2, `b first on track 0: ${got.kind}`);
  check(I.reorder(r, plan, 0, 2).kind === "none", "track 0 has two places: no third");
}

// --- the Icons list: a row of track 0 is never stuck behind one on its own track --------------
start("the Icons list steps and drops onto track 0");
{
  // A (track 0), B (track 1, yours), C (track 0), D (track 2), E (track 0).
  const rows = [{ lane: 0 }, { lane: 1 }, { lane: 0 }, { lane: 2 }, { lane: 0 }];
  check(I.listStep(rows, 2, -1) === 0, "Alt+Up on C goes past B to A");
  check(I.listStep(rows, 2, 1) === 4, "Alt+Down on C goes past D to E");
  check(I.listStep(rows, 0, -1) === null, "nothing above A");
  check(I.listStep(rows, 4, 1) === null, "nothing below E");
  check(I.listStep(rows, 1, -1) === null, "B is on a track of its own");
  check(I.listDrop(rows, 2, 1) === 0, "C let go over B: nothing of track 0 between, so A (it moves up)");
  check(I.listDrop(rows, 2, 3) === 4, "C let go over D: E (it moves down)");
  check(I.listDrop(rows, 0, 3) === 2, "A let go over D (between C and E): after C, not past E");
  check(I.listDrop(rows, 4, 1) === 2, "E let go over B (between A and C): before C, not past A");
  check(I.listDrop(rows, 4, 0) === 0, "a row of track 0 is taken as it is");
  check(I.listDrop(rows, 1, 0) === null, "B is moved on the timeline");
  check(I.listDrop([{ lane: 0 }, { lane: 1 }], 0, 1) === null, "no other row of track 0");
  // Every step lands on track 0, or nowhere.
  for (let i = 0; i < rows.length; i++) {
    for (const d of [-1, 1]) {
      const to = I.listStep(rows, i, d);
      check(to === null || (I.laneOf(rows[to]) === 0 && to !== i), `step ${i} ${d}: ${to}`);
    }
  }
}

start("the picker says why an add is not shown");
{
  const plan = {
    items: [item("on", 0, 3)],
    ideas: [
      { glyph: lib("on"), label: "On", state: "on", reason: null, added: true },
      { glyph: lib("cap"), label: "Cap", state: "limit", reason: "A scene shows 5 icons at most", added: true },
      { glyph: lib("old"), label: "Old", state: "gone", reason: "Not in the icon library any more", added: true },
    ],
  };
  check(eq(I.pickedState("on", plan, true), { kind: "on" }), "on screen");
  check(eq(I.pickedState("cap", plan, true), { kind: "out", state: "limit", reason: "A scene shows 5 icons at most" }),
    "left out by the plan as it is now: the limit");
  check(I.pickedState("old", plan, true).kind === "out", "gone from the library");
  check(eq(I.pickedState("cap", plan, false), { kind: "placing" }), "the plan is not the draft's yet: being placed");
  check(eq(I.pickedState("new", plan, true), { kind: "placing" }), "not in the plan at all yet: being placed");
  check(eq(I.pickedState("cap", null, true), { kind: "placing" }), "no plan yet");
}

for (const g of groups) console.log(`${g.bad ? "FAIL" : "ok  "} ${g.name} (${g.n})`);
console.log(failed ? `${failed} of ${total} failed` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
