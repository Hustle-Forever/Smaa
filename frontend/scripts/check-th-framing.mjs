// Checks the Talking Head v2 Framing rules (src/lib/thFraming.ts): the Look
// step's starting framing (Above the hip for a new reel, Close-up for a run
// made before framing existed), a still tile's framing buttons (what is
// highlighted, the character photo kept as it is), the edit a tile sends, and
// how many stills a new run framing re-shoots.
//
//   node --experimental-strip-types --no-warnings scripts/check-th-framing.mjs
//
// (node 22.6+). Prints one line per group and exits 1 on any difference.

import {
  DEFAULT_FRAMING,
  FRAMING_AS_PHOTO_HINT,
  FRAMING_CHOICES,
  framingButtonTitle,
  framingChangeToast,
  framingFromRun,
  framingHint,
  framingLabel,
  framingMove,
  framingReshootCount,
  normFraming,
  sceneFramingPatch,
  stillFraming,
} from "../src/lib/thFraming.ts";

let failed = 0;
let total = 0;
function group(name, cases, check) {
  let bad = 0;
  for (const c of cases) {
    total += 1;
    const why = check(c);
    if (why) {
      bad += 1;
      if (bad <= 5) console.log(`  ${name}: ${why}`);
    }
  }
  failed += bad;
  console.log(`${bad ? "FAIL" : "ok  "} ${name} (${cases.length})`);
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const scene = (over) => ({
  index: 0, keyframe_url: "/k", as_is: false,
  framing: null, framing_effective: "above_hip", framing_used: "above_hip",
  ...over,
});
const run = (over) => ({ id: 5, scene_count: 1, framing: "above_hip", scenes: [scene({})], ...over });

group("the three choices", [
  () => (same(FRAMING_CHOICES.map((c) => c.value), ["close", "above_hip", "full_body"]) ? null : "order"),
  () => (same(FRAMING_CHOICES.map((c) => c.label), ["Close-up", "Above the hip", "Full body"]) ? null : "labels"),
  () => (DEFAULT_FRAMING === "above_hip" ? null : `default ${DEFAULT_FRAMING}`),
  () => (framingLabel("full_body") === "Full body" && framingLabel("x") === "" ? null : "framingLabel"),
  () => (normFraming("wide") === null && normFraming("close") === "close" ? null : "normFraming"),
  () => (FRAMING_CHOICES.some((c) => (c.label + c.short + c.hint).includes(String.fromCharCode(0x2014))) ? "em dash" : null),
  () => (FRAMING_AS_PHOTO_HINT === "Framing as in your photo" ? null : FRAMING_AS_PHOTO_HINT),
], (f) => f());

group("the Look step starts at", [
  { name: "a new reel", run: null, want: "above_hip" },
  { name: "the run's framing", run: run({ framing: "full_body" }), want: "full_body" },
  { name: "an older backend's run (made close)", run: run({ framing: undefined }), want: "close" },
  { name: "garbage reads close", run: run({ framing: "wide" }), want: "close" },
], (c) => {
  const got = framingFromRun(c.run);
  return got === c.want ? null : `${c.name}: got ${got}`;
});

group("a still tile's framing buttons", [
  { name: "highlights the still's framing", s: scene({ framing_used: "full_body" }),
    want: { supported: true, current: "full_body", disabled: false, hint: null } },
  { name: "a still made before framing existed", s: scene({ framing_used: "close", framing_effective: "close" }),
    want: { supported: true, current: "close", disabled: false, hint: null } },
  { name: "the character photo itself: off", s: scene({ as_is: true, framing_used: "as_photo" }),
    want: { supported: true, current: null, disabled: true, hint: FRAMING_AS_PHOTO_HINT } },
  { name: "made on the photo: none lit, any re-shoots", s: scene({ framing_used: "as_photo" }),
    want: { supported: true, current: null, disabled: false, hint: FRAMING_AS_PHOTO_HINT } },
  { name: "no still yet: the framing it will get", s: scene({ keyframe_url: null, framing_used: null, framing_effective: "close" }),
    want: { supported: true, current: "close", disabled: false, hint: null } },
  { name: "an older backend: no buttons", s: scene({ framing_effective: undefined, framing_used: undefined }),
    want: { supported: false, current: null, disabled: true, hint: null } },
  { name: "no scene", s: null, want: { supported: false, current: null, disabled: true, hint: null } },
], (c) => {
  const got = stillFraming(c.s);
  return same(got, c.want) ? null : `${c.name}: got ${JSON.stringify(got)}`;
});

group("the edit a tile sends", [
  { r: run({}), i: 0, v: "full_body", want: { scene_framings: ["full_body"] } },
  { r: run({ scene_count: 3 }), i: 1, v: "close", want: { scene_framings: [null, "close", null] } },
  { r: run({ scene_count: 3 }), i: 2, v: "above_hip", want: { scene_framings: [null, null, "above_hip"] } },
], (c) => {
  const got = sceneFramingPatch(c.r, c.i, c.v);
  return same(got, c.want) ? null : `scene ${c.i}: got ${JSON.stringify(got)}`;
});

const multi = run({
  scene_count: 4, framing: "above_hip",
  scenes: [
    scene({ index: 0 }),
    scene({ index: 1, framing: "full_body", framing_effective: "full_body", framing_used: "full_body" }),
    scene({ index: 2, as_is: true, framing_used: "as_photo" }),
    scene({ index: 3, framing_used: "as_photo" }),
  ],
});
group("stills a new run framing re-shoots", [
  { name: "the run's own framing", r: multi, f: "above_hip", want: 0 },
  { name: "following scenes only, never the photo itself", r: multi, f: "close", want: 2 },
  { name: "a scene with its own framing is left", r: multi, f: "full_body", want: 2 },
  { name: "no stills yet", r: run({ framing: "close", scenes: [scene({ keyframe_url: null, framing_used: null })] }), f: "full_body", want: 0 },
  { name: "an older backend", r: run({ framing: undefined }), f: "full_body", want: 0 },
  { name: "no run", r: null, f: "close", want: 0 },
], (c) => {
  const got = framingReshootCount(c.r, c.f);
  return got === c.want ? null : `${c.name}: got ${got}`;
});

// A closer framing is a free crop; a wider one (or a still in the photo's
// framing) a paid re-shoot. Never "re-shoot" for a crop (round 3, U4).
group("the hint under the Look control", [
  () => (framingHint("close", null) === FRAMING_CHOICES[0].hint ? null : framingHint("close", null)),
  // Scene 0 (above the hip) is cropped closer; scene 3 (the photo's framing) re-shot.
  () => (framingHint("close", multi).endsWith("Apply look crops 1 still closer (free) and re-shoots 1 (~$0.04 each).")
    ? null : framingHint("close", multi)),
  () => (framingHint("above_hip", multi) === FRAMING_CHOICES[1].hint ? null : "no re-shoot note for the run's own framing"),
  () => (framingHint("full_body", run({})).endsWith("Apply look re-shoots 1 still at this framing (~$0.04 each).")
    ? null : framingHint("full_body", run({}))),
  () => (framingHint("close", run({})).endsWith("Apply look crops 1 still closer (free).") ? null : framingHint("close", run({}))),
  () => (framingHint("close", run({ scenes: [scene({}), scene({ index: 1 })], scene_count: 2 }))
    .endsWith("Apply look crops 2 stills closer (free).") ? null : "two crops"),
  () => (framingHint("close", multi).includes(String.fromCharCode(0x2014)) ? "em dash" : null),
  () => (framingHint("full_body", run({}), "~$0.13").endsWith("Apply look re-shoots 1 still at this framing (~$0.13 each).")
    ? null : framingHint("full_body", run({}), "~$0.13")),
  // A full-body still to Close-up is a paid re-shoot, never "free".
  () => (framingHint("close", run({ framing: "full_body", scenes: [scene({ framing_used: "full_body" })] }))
    .endsWith("Apply look re-shoots 1 still at this framing (~$0.04 each).")
    ? null : framingHint("close", run({ framing: "full_body", scenes: [scene({ framing_used: "full_body" })] }))),
], (f) => f());

group("a closer framing is a crop, a wider one a re-shoot", [
  { from: "above_hip", to: "close", want: "crop" },
  { from: "full_body", to: "above_hip", want: "crop" },
  // A full-body face would be enlarged about 5x (a crop stops at 3x): re-shot.
  { from: "full_body", to: "close", want: "reshoot" },
  { from: "close", to: "above_hip", want: "reshoot" },
  { from: "above_hip", to: "full_body", want: "reshoot" },
  { from: "as_photo", to: "close", want: "reshoot" },
  { from: null, to: "close", want: "reshoot" },
], (c) => {
  const got = framingMove(c.from, c.to);
  return got === c.want ? null : `${c.from} to ${c.to}: got ${got}`;
});

group("a framing button's toast", [
  { w: "crop", f: "close", want: ["Cropping to Close-up", "A closer crop of this still: free, the same person to the pixel."] },
  { w: "reshoot", f: "full_body", want: ["Re-shooting at Full body",
    "The image model re-shoots it (~$0.04): same person, outfit and room; only the camera moves."] },
  { w: "regen", f: "close", want: ["Making the still at Close-up", "A new still at this framing (~$0.04)."] },
  { w: "reshoot", f: "full_body", cost: "~$0.13", want: ["Re-shooting at Full body",
    "The image model re-shoots it (~$0.13): same person, outfit and room; only the camera moves."] },
], (c) => {
  const got = framingChangeToast(c.w, c.f, c.cost);
  if (got.join(" ").includes(String.fromCharCode(0x2014))) return `${c.w}: em dash`;
  return same(got, c.want) ? null : `${c.w}: got ${JSON.stringify(got)}`;
});

const hip = { supported: true, current: "above_hip", disabled: false, hint: null };
group("a still tile's framing button says what it does and what it costs", [
  { name: "the still's own framing", c: "above_hip", s: hip, still: true, want: ["Above the hip: this still's framing"] },
  { name: "closer: a free crop, never a re-shoot",
    c: "close", s: hip, still: true, want: ["Close-up (crop, free)", "a closer crop of this still", "re-shot instead (~$0.04)"],
    not: ["(re-shoot", "re-shoot this", "Regenerat"] },
  { name: "wider: a paid re-shoot", c: "full_body", s: hip, still: true,
    want: ["Full body (re-shoot, ~$0.04)", "re-shoots this still"], not: ["free", "crop"] },
  { name: "full body to close-up: a paid re-shoot (more than a crop may enlarge)", c: "close",
    s: { ...hip, current: "full_body" }, still: true,
    want: ["Close-up (re-shoot, ~$0.04)", "re-shoots this still"], not: ["free", "crop"] },
  { name: "the photo's framing: a re-shoot", c: "close",
    s: { supported: true, current: null, disabled: false, hint: FRAMING_AS_PHOTO_HINT }, still: true,
    want: ["Close-up (re-shoot, ~$0.04)"] },
  { name: "the photo itself", c: "close", s: { supported: true, current: null, disabled: true, hint: FRAMING_AS_PHOTO_HINT },
    still: true, want: [FRAMING_AS_PHOTO_HINT] },
  { name: "no still yet: made at it", c: "close", s: hip, still: false, want: ["Close-up: make this still at it (~$0.04)"] },
  { name: "the backend's price", c: "full_body", s: hip, still: true, cost: "~$0.13",
    want: ["Full body (re-shoot, ~$0.13)"], not: ["~$0.04"] },
  { name: "the backend's price, a crop's fallback", c: "close", s: hip, still: true, cost: "~$0.13",
    want: ["Close-up (crop, free)", "re-shot instead (~$0.13)"], not: ["~$0.04"] },
], (c) => {
  const choice = FRAMING_CHOICES.find((x) => x.value === c.c);
  const got = framingButtonTitle(choice, c.s, c.still, c.cost);
  if (got.includes(String.fromCharCode(0x2014))) return `${c.name}: em dash`;
  const missing = c.want.filter((w) => !got.includes(w));
  const extra = (c.not ?? []).filter((w) => got.includes(w));
  return missing.length || extra.length ? `${c.name}: got ${got}` : null;
});

console.log(failed ? `${failed} of ${total} differ` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
