// Checks that Talking Head v2 makes clips (paid Veo) ONLY from an explicit
// press of the Videos button, and what that button says it costs
// (src/lib/thVideos.ts). Run 830 got two rounds of Veo nobody asked for: a
// page opened on the Videos step and a click on the out-of-date Videos step
// both sent /finalize from an effect.
//
// Besides the pure helpers, this reads the TH components' source: no React
// effect may start paid work, /finalize is called in one place (makeVideos,
// with confirm) and makeVideos only from a button's onClick, Cmd/Ctrl+Enter
// never fires a primary that costs money, and the Video size buttons say
// which one is on (aria-pressed).
//
//   node --experimental-strip-types --no-warnings scripts/check-th-videos.mjs
//
// (node 22.6+). Prints one line per group and exits 1 on any difference.

import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  clipLine,
  clipTargetLabel,
  costsMoney,
  elapsedLabel,
  shortcutFires,
  usd,
  videosAction,
  videosCost,
  videosStaleNote,
} from "../src/lib/thVideos.ts";

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

const EM_DASH = String.fromCharCode(0x2014);
const FAST = "veo-3.1-fast-generate-001";
const clip = (target, over = {}) => ({
  target, why: "missing", engine: "veo", model: FAST, seconds: target.startsWith("scene") || target === "main" ? 8 : 4,
  audio: target.startsWith("scene") || target === "main", usd: target.startsWith("scene") || target === "main" ? 1.4 : 0.4,
  usd_max: target.startsWith("scene") || target === "main" ? 1.4 : 0.4, ...over,
});
const plan = (clips, any_clip = false) => ({
  clips, count: clips.length, any_clip,
  usd: Math.round(clips.reduce((a, c) => a + c.usd, 0) * 100) / 100,
  usd_max: Math.round(clips.reduce((a, c) => a + c.usd_max, 0) * 100) / 100,
});
const ready = { id: 1, status: "ready" };
const run = (over = {}) => ({
  scene_count: 2,
  scenes: [{ index: 0, keyframe_url: "/s0", keyframe_status: "ready", animation: null },
           { index: 1, keyframe_url: "/s1", keyframe_status: "ready", animation: null }],
  animations: { intro: null, main: null, outro: null },
  intro_excluded: false, outro_excluded: false,
  ...over,
});
const go = { busy: false, previewsReady: true };

// ---------------------------------------------------------------------------
// The button: Make videos / Update N clips, with what it costs
// ---------------------------------------------------------------------------

const QA = plan([clip("scene_0"), clip("scene_1"), clip("intro"), clip("outro")]);
const allFresh = {
  scenes: [{ index: 0, keyframe_url: "/s0", animation: ready }, { index: 1, keyframe_url: "/s1", animation: ready }],
  animations: { intro: ready, main: null, outro: ready },
};

group("the Videos button", [
  { name: "run 830's first round: no clip yet", r: run({ videos_plan: QA }), st: go,
    want: { kind: "make", label: "Make videos", count: 4, cost: "~$3.60", disabled: false, waiting: null } },
  { name: "after an Undo that put a different still back: one clip to update",
    r: run({ ...allFresh, videos_plan: plan([clip("scene_0", { why: "stale" })], true) }), st: go,
    want: { kind: "update", label: "Update 1 clip", count: 1, cost: "~$1.40", disabled: false } },
  { name: "after an Undo back to the clip's own still: nothing to make",
    r: run({ ...allFresh, videos_plan: plan([], true) }), st: go, want: { kind: "none", label: "", disabled: true } },
  { name: "three to update", r: run({ ...allFresh, videos_plan: plan([clip("scene_0"), clip("scene_1"), clip("intro")], true) }),
    st: go, want: { kind: "update", label: "Update 3 clips", cost: "~$3.20" } },
  { name: "a card not planned yet is a range",
    r: run({ videos_plan: plan([clip("scene_0"), clip("outro", { engine: "veo_or_zoom", usd: 0, usd_max: 0.4 })]) }),
    st: go, want: { cost: "~$1.40 to $1.80" } },
  { name: "only slow zooms: free",
    r: run({ ...allFresh, videos_plan: plan([clip("intro", { engine: "zoom", model: null, usd: 0, usd_max: 0 })], true) }),
    st: go, want: { kind: "update", cost: "free" } },
  { name: "a still is being made: wait (the clip would animate the old one)",
    r: run({ videos_plan: QA, scenes: [{ index: 0, keyframe_url: "/s0", keyframe_status: "generating" }, { index: 1 }] }),
    st: go, want: { disabled: true, waiting: "Wait for the still being made." } },
  { name: "clips are being made: wait",
    r: run({ ...allFresh, animations: { intro: { id: 2, status: "generating" }, outro: ready },
             videos_plan: plan([clip("scene_0", { why: "stale" })], true) }),
    st: go, want: { disabled: true, waiting: "Clips are being made." } },
  { name: "the stills or cards are not ready", r: run({ videos_plan: QA }), st: { busy: false, previewsReady: false },
    want: { disabled: true, waiting: "Make the stills and cards first." } },
  { name: "something else runs", r: run({ videos_plan: QA }), st: { busy: true, previewsReady: true },
    want: { disabled: true, waiting: null } },
  { name: "an older backend (no plan): counted from the clips, no price",
    r: run({ scenes: [{ index: 0, keyframe_url: "/s0", animation: ready, video_stale: true },
                      { index: 1, keyframe_url: "/s1", animation: ready }],
             animations: { intro: ready, outro: { id: 3, status: "failed" } } }), st: go,
    want: { kind: "update", label: "Update 2 clips", cost: null } },
  { name: "no run", r: null, st: go, want: { kind: "none", disabled: true } },
], (c) => {
  const got = videosAction(c.r, c.st);
  for (const [k, v] of Object.entries(c.want)) {
    if (got[k] !== v) return `${c.name}: ${k} = ${JSON.stringify(got[k])}`;
  }
  if (got.hint.includes(EM_DASH)) return `${c.name}: em dash`;
  return null;
});

group("what the button says it makes", [
  { c: clip("scene_0"), want: "Scene 1: 8 s, Veo Fast with audio, $1.40" },
  { c: clip("main", { model: "veo-3.1-generate-001", usd: 6, usd_max: 6 }), want: "Main: 8 s, Veo Quality with audio, $6.00" },
  { c: clip("intro"), want: "Intro: 4 s, Veo Fast, $0.40" },
  { c: clip("outro", { engine: "zoom", model: null, usd: 0, usd_max: 0 }), want: "Outro: slow zoom, free" },
  { c: clip("outro", { engine: "veo_or_zoom", usd: 0 }), want: "Outro: 4 s, Veo Fast or the slow zoom, up to $0.40" },
], (c) => {
  const got = clipLine(c.c);
  return got === c.want ? null : `got ${got}`;
});

group("prices and labels", [
  { got: () => usd(1.4), want: "$1.40" },
  { got: () => usd(3.6000000001), want: "$3.60" },
  { got: () => videosCost(QA), want: "~$3.60" },
  { got: () => videosCost(null), want: null },
  { got: () => videosCost(plan([])), want: "free" },
  { got: () => clipTargetLabel("scene_4"), want: "Scene 5" },
  { got: () => clipTargetLabel("intro"), want: "Intro" },
  { got: () => elapsedLabel(0), want: "0:00" },
  { got: () => elapsedLabel(45.9), want: "0:45" },
  { got: () => elapsedLabel(125), want: "2:05" },
  { got: () => videosStaleNote(videosAction(run({ ...allFresh, videos_plan: plan([clip("scene_0")], true) }), go)),
    want: '1 clip is missing or out of date. "Update 1 clip" (~$1.40) makes it again; nothing is made until you press it.' },
  { got: () => videosStaleNote(videosAction(run({ videos_plan: QA }), go)), want: null },
], (c) => {
  const got = c.got();
  return got === c.want ? null : `got ${JSON.stringify(got)}, want ${JSON.stringify(c.want)}`;
});

group("Cmd/Ctrl+Enter never spends", [
  { cost: "~$3.60", want: false },
  { cost: "~$1.40 to $1.80", want: false },
  { cost: "~$0.05", want: false },
  { cost: "free", want: true },
  { cost: null, want: true },
  { cost: "", want: true },
  { cost: null, shortcut: false, want: false },
], (c) => {
  const got = shortcutFires(c.cost, c.shortcut);
  if (got !== c.want) return `${JSON.stringify(c.cost)}: got ${got}`;
  return costsMoney(c.cost) === (c.cost != null && c.cost !== "" && c.cost !== "free") ? null : "costsMoney";
});

// ---------------------------------------------------------------------------
// The source: no effect starts paid work
// ---------------------------------------------------------------------------

const here = dirname(fileURLToPath(import.meta.url));
const COMPONENTS = join(here, "..", "src", "components", "yas");
const files = readdirSync(COMPONENTS).filter((f) => /^TalkingHeadV2.*\.tsx$/.test(f));
const sources = Object.fromEntries(files.map((f) => [f, readFileSync(join(COMPONENTS, f), "utf8")]));
const creator = sources["TalkingHeadV2Creator.tsx"] ?? "";
const apiSrc = readFileSync(join(here, "..", "src", "lib", "api.ts"), "utf8");

/** The text of the call that starts at `open` (an index of "(") up to its
 *  matching ")", skipping strings, template literals and comments. */
function callText(src, open) {
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    const ch = src[i];
    if (ch === "/" && src[i + 1] === "/") { i = src.indexOf("\n", i); if (i < 0) break; continue; }
    if (ch === "/" && src[i + 1] === "*") { i = src.indexOf("*/", i + 2) + 1; continue; }
    if (ch === '"' || ch === "'" || ch === "`") {
      for (i += 1; i < src.length && src[i] !== ch; i++) if (src[i] === "\\") i += 1;
      continue;
    }
    if (ch === "(") depth += 1;
    if (ch === ")") { depth -= 1; if (depth === 0) return src.slice(open, i + 1); }
  }
  return src.slice(open);
}

/** Every useEffect(...) call in `src`. */
function effects(src) {
  const out = [];
  for (let i = src.indexOf("useEffect("); i >= 0; i = src.indexOf("useEffect(", i + 1)) {
    out.push(callText(src, i + "useEffect".length));
  }
  return out;
}

/** The body of `async function name(` ... up to its closing brace. */
function functionBody(src, name) {
  const start = src.indexOf(`async function ${name}(`);
  if (start < 0) return "";
  const brace = src.indexOf("{", src.indexOf(")", start));
  let depth = 0;
  for (let i = brace; i < src.length; i++) {
    if (src[i] === "{") depth += 1;
    if (src[i] === "}") { depth -= 1; if (depth === 0) return src.slice(start, i + 1); }
  }
  return src.slice(start);
}

// Anything that makes a paid image or a Veo clip, called directly or through
// the creator's own handlers (finalize was the old name of makeVideos;
// finalizeReel, the free Remotion stitch, is not one).
const PAID = [
  "makeVideos", "finalize", "retryVideo", "batchRegen", "generatePreviews",
  "retryPreview", "refineStill", "regenerateStill", "makeCards", "generateStills", "stillTileAction",
  "retryTile", "retryVideoTile", "batchRegenerate", "startScript", "generateScript", "setStillFraming",
].map((w) => [w, new RegExp(`\\b${w}\\b`)]);

/** The first paid call in an effect's text, or null. */
function paidIn(body) {
  const hit = PAID.find(([, re]) => re.test(body));
  return hit ? hit[0] : null;
}

// The scan must catch what fired run 830's rounds: the effect as it was at
// 269f9f0 (shortened).
const RUN_830_EFFECT = `useEffect(() => {
    if (activeStage !== "videos") { autoFiredRef.current = false; return; }
    if (autoFiredRef.current || !run || busy) return;
    const needsGen = hasStaleIncluded || !hasAnyClip;
    if (!needsGen) return;
    autoFiredRef.current = true;
    toast.info("Generating videos", "Animating the updated stills with Veo...");
    void finalize();
  }, [activeStage, run, busy]);`;
group("the scan catches run 830's auto-fire", [
  { body: effects(RUN_830_EFFECT)[0], want: "finalize" },
  { body: "(() => { void makeVideos(); }, [])", want: "makeVideos" },
  { body: "(() => { api.yasProducer.talkingHeadV2.retryVideo(1, {}); }, [])", want: "retryVideo" },
  { body: "(() => { void api.yasProducer.talkingHeadV2.finalize(1, { confirm: true }); }, [])", want: "finalize" },
  { body: "(() => { void finalizeReel(); setX(1); }, [])", want: null },
  { body: "(() => { setSpeakerDraft(runSpeakerSubject); }, [x])", want: null },
], (c) => {
  const got = paidIn(c.body);
  return got === c.want ? null : `got ${got}, want ${c.want}`;
});

const effectCases = [];
for (const [f, src] of Object.entries(sources)) {
  effects(src).forEach((body, k) => effectCases.push({ f, k, body }));
}
group("no effect starts paid work (every TalkingHeadV2 component)", effectCases, (c) => {
  const hit = paidIn(c.body);
  return hit ? `${c.f} effect #${c.k + 1} calls ${hit}: ${c.body.slice(0, 80).replace(/\s+/g, " ")}` : null;
});

const finalizeCalls = Object.entries(sources).flatMap(([f, src]) =>
  [...src.matchAll(/talkingHeadV2\.finalize\(/g)].map((m) => ({ f, at: m.index })));
const makeVideosBody = functionBody(creator, "makeVideos");
const makeVideosRefs = [...creator.matchAll(/\bmakeVideos\b/g)].map((m) => m.index);
const onClickRefs = [...creator.matchAll(/onClick: makeVideos\b/g)].length;
const keydown = effects(creator).find((b) => b.includes("keydown")) ?? "";

group("clips are made only by the Videos button", [
  { name: "the old enter-Videos auto-fire is gone", ok: !/autoFiredRef|AUTO-fires|void finalize\(\)/.test(creator) },
  { name: "/finalize is called in exactly one place", ok: finalizeCalls.length === 1 },
  { name: "...inside makeVideos, with confirm",
    ok: makeVideosBody.includes("talkingHeadV2.finalize(run.id, { confirm: true })") },
  { name: "makeVideos is used only as a button's onClick (and defined once)",
    ok: onClickRefs >= 1 && makeVideosRefs.length === onClickRefs + 1 },
  { name: "the keyboard shortcut asks shortcutFires (never a paid primary)",
    ok: keydown.includes("shortcutFires(c.cost, c.shortcut)") },
  { name: "the ⌘↵ hint shows only where the shortcut works",
    ok: creator.includes("shortcutFires(primary.cost, primary.shortcut) && (") },
  { name: "the api's finalize needs { confirm: true } and sends it",
    ok: /finalize: \(runId: number, body: \{ confirm: true \}\) =>/.test(apiSrc) &&
        /talking-head-v2\/\$\{runId\}\/finalize`,\s*\{ method: "POST", body: JSON\.stringify\(body\) \}/.test(apiSrc) },
  { name: "the Generate-intro switch makes no card by itself",
    ok: !functionBody(creator, "toggleGenerateBookend").includes("retryPreview") },
  { name: "Apply look makes a paid card only when its label offered it",
    ok: /const cardsPromised = cardsToMake\.length > 0;/.test(functionBody(creator, "applyEdits")) &&
        /activeStage === "look" && cardsPromised && runIsBeingEdited\(fresh\)/.test(functionBody(creator, "applyEdits")) },
  { name: "no fixed ~$1.20 clip estimate left", ok: !creator.includes("~$1.20") && !creator.includes("generate_videos") },
  // An edit of a run with stills may re-shoot them (paid, unpriced): a
  // click only, never Cmd/Ctrl+Enter typed in a field.
  { name: "Update script on a run with stills is not the shortcut's",
    ok: /label: applying \? "Updating…" : "Update script", onClick: applyEdits,[^}]*shortcut: !hasKeyframe/.test(creator) },
  { name: "Apply look on a run with stills is not the shortcut's",
    ok: /label: applying \? "Applying…" : "Apply look", onClick: applyEdits,[^}]*shortcut: !hasKeyframe/.test(creator) },
  // A control character in the source (a "\b" written as a backspace made
  // the finalize 409 test match nothing).
  { name: "no control characters in the TalkingHeadV2 sources",
    ok: Object.values(sources).every((src) => !/[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(src)) },
], (c) => (c.ok ? null : c.name));

// T10: the Video size buttons say which is on, like the Framing ones.
const sizeAt = creator.indexOf('aria-label="Video size"');
const sizeBlock = sizeAt >= 0 ? creator.slice(sizeAt, sizeAt + 1200) : "";
group("the Video size buttons say which one is on", [
  { name: "a labelled group", ok: sizeAt >= 0 && creator.slice(sizeAt - 200, sizeAt).includes('role="group"') },
  { name: "aria-pressed on each", ok: sizeBlock.includes("aria-pressed={videoAspect === val}") },
], (c) => (c.ok ? null : c.name));

console.log(failed ? `${failed} of ${total} differ` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
