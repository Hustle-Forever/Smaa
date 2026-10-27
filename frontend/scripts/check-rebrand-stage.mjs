// Checks src/lib/rebrandStage.ts — the Rebrand stage's own rules:
//   * the deep-link step: parse ∘ format is the identity for every scene a
//     reel can have, and anything else is not the Rebrand stage;
//   * where a reopened run lands: the plan's full table (composition status ×
//     reel_ready), and today's answer on a backend without the stage;
//   * which scene the strip opens (a refresh's, else the first included main);
//   * what each tile says (the badge, making, the one amber "!");
//   * the Finalize button's four states, and what "unchanged" means;
//   * a reel block from a PUT laid on the run (withReel: stale or not).
//
//   node --experimental-strip-types scripts/check-rebrand-stage.mjs
//
// (node 22.6+.) Prints a line per group and exits 1 on any failure.

import {
  finalizeState,
  landingStage,
  parseRebrandStep,
  rebrandStep,
  reelUnchanged,
  resolveTarget,
  stageOfStep,
  stripTargets,
  tileState,
  withReel,
} from "../src/lib/rebrandStage.ts";

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

// --- the step ----------------------------------------------------------------------
start("step: parse(format(t)) is t for every scene");
for (const t of [null, "intro", "outro", ...Array.from({ length: 100 }, (_, i) => `scene_${i}`)]) {
  const step = rebrandStep(t);
  check(/^[a-z0-9_]+$/.test(step), `${step} fits the hash's @([a-z0-9_]+)`);
  check(eq(parseRebrandStep(step), { target: t }), `${t} → ${step} → ${JSON.stringify(parseRebrandStep(step))}`);
  check(stageOfStep(step, true) === "rebrand", `${step} is the Rebrand stage`);
  check(stageOfStep(step, false) === null, `${step} on an older backend falls back to landing`);
}
start("step: anything else is not the Rebrand stage");
for (const s of ["", "rebrand_", "rebrand_scene_", "rebrand_scene_100", "rebrand_scene_x", "rebrand_main", "rebrandscene_0",
  "Rebrand", "rebrand_intro_", "xrebrand", "rebrand scene_0", null, undefined]) {
  check(parseRebrandStep(s) === null, JSON.stringify(s));
}
for (const s of ["topic", "look", "stills", "videos", "final"]) {
  check(stageOfStep(s, true) === s && stageOfStep(s, false) === s, s);
  check(parseRebrandStep(s) === null, s);
}
check(stageOfStep("bogus", true) === null && stageOfStep(null, true) === null, "bogus / none → landing");

// --- landing -------------------------------------------------------------------------
start("landing: the plan's table (S4.2)");
const TABLE = [
  // status, reel_ready, lands
  ["rendering", true, "final"],
  ["rendering", false, "final"],
  ["ready", true, "rebrand"],
  ["ready", false, "final"],          // never Videos: its auto-fire would spend Veo
  ["videos_ready", true, "rebrand"],
  ["videos_ready", false, "videos"],
  ["failed", true, "rebrand"],
  ["failed", false, "videos"],
  ["queued", true, "videos"],
  ["queued", false, "videos"],
];
for (const [cs, rr, want] of TABLE) {
  for (const hasStills of [true, false]) {
    const got = landingStage({ composition_status: cs, reel_ready: rr, hasStills });
    check(got === want, `${cs} × reel_ready=${rr} → ${got}, want ${want}`);
  }
}
for (const rr of [true, false]) {
  check(landingStage({ composition_status: null, reel_ready: rr, hasStills: true }) === "stills", "no reel yet, stills → stills");
  check(landingStage({ composition_status: null, reel_ready: rr, hasStills: false }) === "look", "no stills → look");
}
start("landing: an older backend (no reel_ready) is today's rule");
for (const [cs, want] of [["ready", "final"], ["rendering", "final"], ["queued", "videos"], ["videos_ready", "videos"], ["failed", "videos"]]) {
  check(landingStage({ composition_status: cs, hasStills: true }) === want, `${cs} → ${want}`);
}
check(landingStage({ composition_status: null, hasStills: false }) === "look", "no stills → look");
check(landingStage({ composition_status: undefined, hasStills: true }) === "stills", "stills → stills");

// --- the strip -----------------------------------------------------------------------
const T = (target, role, extra = {}) => ({
  target, role, label: target, src: `anim:${target.length}`, thumb_url: null, pick: null, ships: null, making: null,
  end: { frame: 240, auto: null, user: null, full: 240 }, ...extra,
});
const reel = (targets) => ({ ready: true, inputs_sha: "abc", targets });
start("strip: main scenes only (bookends when the studio can open them)");
{
  const r = reel([T("intro", "intro"), T("scene_1", "main"), T("scene_2", "main"), T("outro", "outro")]);
  check(eq(stripTargets(r).map((t) => t.target), ["scene_1", "scene_2"]), "mains only");
  check(eq(stripTargets(r, true).map((t) => t.target), ["intro", "scene_1", "scene_2", "outro"]), "with bookends, reel order");
  check(eq(stripTargets(reel([T("intro", "intro", { src: null }), T("scene_0", "main")]), true).map((t) => t.target), ["scene_0"]),
    "a bookend with nothing found is not a tile");
  const many = reel([T("intro", "intro"), ...Array.from({ length: 9 }, (_, i) => T(`scene_${i}`, "main")), T("outro", "outro")]);
  check(stripTargets(many, true).length === 7, "at most 7 tiles");
  check(stripTargets(null).length === 0 && stripTargets(undefined).length === 0, "no reel, no tiles");
}
start("strip: which scene opens");
{
  const r = reel([T("intro", "intro"), T("scene_1", "main"), T("scene_2", "main"), T("outro", "outro")]);
  check(resolveTarget(r, "scene_2") === "scene_2", "the one asked for");
  check(resolveTarget(r, null) === "scene_1", "none asked: the first included main (scene_0 excluded)");
  check(resolveTarget(r, "scene_0") === "scene_1", "gone from the reel: the first included main, not scene_0");
  check(resolveTarget(r, "intro") === "scene_1", "a bookend before the studio can open it: the first main");
  check(resolveTarget(r, "intro", true) === "intro", "…and itself once it can");
  check(resolveTarget(null, "scene_0") === null, "no reel: none");
}

// --- the tile ------------------------------------------------------------------------
start("tile: the plan's state table (§4.2)");
{
  const no = (id) => ({ 11: 1, 12: 2, 13: 3 })[id];
  const untouched = tileState(T("scene_0", "main"));
  check(eq(untouched, { badge: null, badgeTitle: null, making: false, makingTitle: null, problem: null }), "untouched: nothing");
  const reb = tileState(T("scene_0", "main", { ships: 12, pick: { version_id: 12, state: "ready" } }), { no });
  check(reb.badge === "rebranded" && reb.badgeTitle === "v2 is in the reel" && !reb.problem && !reb.making, "Rebranded, title v2 is in the reel");
  const trim = tileState(T("scene_0", "main", { end: { frame: 225, auto: 235, user: 225, full: 240 } }));
  check(trim.badge === "trimmed" && trim.badgeTitle === "Ends at 7.5 s", `Trimmed: ${trim.badgeTitle}`);
  const both = tileState(T("scene_0", "main", { ships: 11, end: { frame: 200, auto: null, user: 200, full: 240 } }), { no });
  check(both.badge === "rebranded", "a version AND an end: Rebranded");
  const making = tileState(T("scene_0", "main", { ships: 11, making: 13, pick: { version_id: 13, state: "running" } }), { no });
  check(making.making && making.makingTitle === "Making v3…" && making.badge === "rebranded" && !making.problem,
    "Making v3… (the older pick keeps its badge)");
  const opt = tileState(T("scene_0", "main"), { making: 13, no });
  check(opt.making && opt.makingTitle === "Making v3…", "making, before the server says so (optimistic)");
  const failedV = tileState(T("scene_0", "main", { ships: 11, pick: { version_id: 12, state: "failed" } }), { no });
  check(failedV.problem === "v2 failed" && failedV.badge === "rebranded", "a failed newer version: ! v2 failed (v1 still ships)");
  const lost = tileState(T("scene_0", "main", { ships: 12, pick: { version_id: 12, state: "lost" } }), { no });
  check(lost.problem === "v2 isn't on this computer: open it and press Remake", `lost: ${lost.problem}`);
  const older = tileState(T("scene_0", "main", { pick: { version_id: 12, state: "other_clip" } }), { no });
  check(older.problem === "v2 was made on an older clip" && older.badge === null, "made on an older clip: ! and no badge");
  const unknown = tileState(T("scene_0", "main", { ships: 99, pick: { version_id: 99, state: "ready" } }), { no });
  check(unknown.badgeTitle === "Its version is in the reel", "a version the page can't number yet");
  check(tileState(T("scene_0", "main", { pick: { version_id: 12, state: "running" }, making: 12 }), { no }).problem === null,
    "running is making, not a problem");
}
start("tile: the version that goes in, made for another end");
{
  const no = (id) => ({ 11: 1, 12: 2 })[id];
  const ends = { 11: null, 12: 200 };
  const endOf = (id) => (id in ends ? ends[id] : undefined);
  const main = (extra) => T("scene_0", "main", { ships: 12, pick: { version_id: 12, state: "ready" }, ...extra });
  const auto = { frame: 235, auto: 235, user: null, full: 240 };
  const other = tileState(main({ end: auto }), { no, endOf });
  check(other.problem === "v2 ends at 6.7 s" && other.badge === "rebranded", `v2 made to 200, the scene automatic: ${other.problem}`);
  const same = tileState(main({ end: { ...auto, frame: 200, user: 200 } }), { no, endOf });
  check(same.problem === null, "made to the scene's end: nothing");
  const longer = tileState(T("scene_0", "main", { ships: 11, pick: { version_id: 11, state: "ready" }, end: { ...auto, frame: 200, user: 200 } }), { no, endOf });
  check(longer.problem === "v1 ends at 7.8 s", `v1 automatic (235), the scene cut to 200: ${longer.problem}`);
  const bookend = tileState(T("intro", "intro", { ships: 11, pick: { version_id: 11, state: "ready" }, end: { frame: 90, auto: null, user: 90, full: 120 } }), { no, endOf });
  check(bookend.problem === "v1 ends at 4 s", `an intro cut to 3 s, v1 whole: ${bookend.problem}`);
  check(tileState(main({ end: auto }), { no }).problem === null, "not known yet (no endOf): nothing");
  check(tileState(main({ end: auto }), { no, endOf: () => undefined }).problem === null, "a version the page hasn't listed: nothing");
  check(tileState(T("scene_0", "main", { end: { ...auto, user: 200, frame: 200 } }), { no, endOf }).problem === null, "the original: never");
  const failedToo = tileState(main({ end: auto, pick: { version_id: 12, state: "lost" } }), { no, endOf });
  check(failedToo.problem === "v2 isn't on this computer: open it and press Remake", "one problem at a time: the version's own first");
}

// --- Finalize ----------------------------------------------------------------------------
start("Finalize: its four states (§4.3)");
check(eq(finalizeState({ making: "Main 2", rendering: true, unchanged: true }), { kind: "making", label: "Making Main 2…", enabled: false }), "making wins");
check(eq(finalizeState({ making: null, rendering: true, unchanged: true }), { kind: "rendering", label: "Rendering…", enabled: false }), "rendering");
check(eq(finalizeState({ making: null, rendering: false, unchanged: true }), { kind: "see", label: "See final →", enabled: true }), "see final");
check(eq(finalizeState({ making: null, rendering: false, unchanged: false }), { kind: "finalize", label: "Finalize →", enabled: true }), "finalize");
start("Finalize: unchanged = a good reel of this round, ready, no error, served, not stale");
{
  const good = { composition_status: "ready", composition_good_seq: 2, composition_error: null, composition_url: "/x?v=2", composition_stale: false };
  check(reelUnchanged(good), "unchanged");
  check(!reelUnchanged({ ...good, composition_stale: true }), "a pick or end since");
  check(!reelUnchanged({ ...good, composition_error: "boom" }), "the last render failed");
  check(!reelUnchanged({ ...good, composition_good_seq: null }), "a legacy reel is finalized again");
  check(!reelUnchanged({ ...good, composition_url: null }), "nothing served");
  for (const s of ["rendering", "videos_ready", "failed", "queued", null]) check(!reelUnchanged({ ...good, composition_status: s }), `status ${s}`);
}

// --- withReel ----------------------------------------------------------------------------
start("withReel: a PUT's block laid on the run");
{
  const blk = (sha) => ({ ready: true, inputs_sha: sha, targets: [] });
  const run = (sha, stale, seq = 3) => ({ reel: blk(sha), composition_stale: stale, composition_good_seq: seq });
  const good = { sha: null };
  let r = withReel(run("A", false), blk("B"), good);
  check(r.composition_stale === true && good.sha === "A" && r.reel.inputs_sha === "B", "unchanged A, then a pick: stale");
  r = withReel(r, blk("A"), good);
  check(r.composition_stale === false, "back to A: not stale");
  r = withReel(r, blk("C"), good);
  check(r.composition_stale === true, "C: stale");
  const g2 = { sha: null };
  r = withReel(run("A", true), blk("B"), g2);
  check(r.composition_stale === true && g2.sha === null, "stale from the start, the good inputs unknown: stale");
  r = withReel(run("A", true), blk("A"), { sha: null });
  check(r.composition_stale === true, "the same inputs keep the server's word");
  r = withReel(run("A", false, null), blk("B"), { sha: null });
  check(r.composition_stale === false, "never finalized here: the server's word stands");
}

for (const g of groups) console.log(`${g.bad ? "FAIL" : "ok  "} ${g.name} (${g.n})`);
console.log(failed ? `${failed} of ${total} FAILED` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
