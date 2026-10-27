// Checks the Talking Head v2 Stills tile actions (src/lib/thStill.ts): when
// Refine, Regenerate and Undo are offered and enabled, and what the tile and
// the toasts say while and after they run.
//
//   node --experimental-strip-types --no-warnings scripts/check-th-still.mjs
//
// (node 22.6+). Prints one line per group and exits 1 on any difference.

import {
  AS_IS_REGENERATE_HINT,
  REFINE_TITLE,
  REGENERATE_TITLE,
  STILL_IMAGE_COST,
  STILL_KEPT_AFTER_ERROR,
  STILL_REFINE_LIMIT_HINT,
  STILL_REFINE_PLACEHOLDER,
  STILL_REGEN_FAILED_HINT,
  UNDO_TITLE,
  previewTileStatus,
  stillBusyLabel,
  stillDoneToast,
  stillFailTitle,
  stillImageCost,
  stillLimitHint,
  stillStatusLine,
  stillTileButtons,
  stillWorkStatusLine,
  stillWorkToast,
} from "../src/lib/thStill.ts";

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

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

const tile = (over) => ({ hasStill: true, busy: false, change: "", canUndo: false, ...over });

group("which buttons a still tile offers", [
  { name: "a still, no text", s: tile({}), want: { showRefine: true, refineEnabled: false, regenerateEnabled: true, showUndo: false, undoEnabled: false } },
  { name: "a still and a change", s: tile({ change: "warmer light" }), want: { showRefine: true, refineEnabled: true, regenerateEnabled: true } },
  { name: "only spaces is no change", s: tile({ change: "   " }), want: { refineEnabled: false } },
  { name: "busy: nothing runs twice", s: tile({ change: "x", canUndo: true, busy: true }), want: { refineEnabled: false, regenerateEnabled: false, showUndo: true, undoEnabled: false } },
  { name: "a failed tile: Regenerate only", s: tile({ hasStill: false, change: "x" }), want: { showRefine: false, refineEnabled: false, regenerateEnabled: true } },
  { name: "an earlier version: Undo", s: tile({ canUndo: true }), want: { showUndo: true, undoEnabled: true } },
  { name: "the page is busy: every button waits", s: tile({ change: "x", canUndo: true, pageBusy: true }),
    want: { showRefine: true, refineEnabled: false, regenerateEnabled: false, showUndo: true, undoEnabled: false } },
  { name: "the photo itself: no Regenerate, Refine and Undo stay",
    s: tile({ change: "x", canUndo: true, asIsPhoto: true }),
    want: { refineEnabled: true, regenerateEnabled: false, regenerateTitle: AS_IS_REGENERATE_HINT,
            regenerateHint: AS_IS_REGENERATE_HINT, undoEnabled: true } },
  { name: "a failed photo tile can be made again", s: tile({ hasStill: false, asIsPhoto: true }),
    want: { regenerateEnabled: true, regenerateTitle: REGENERATE_TITLE, regenerateHint: null } },
], (c) => {
  const got = stillTileButtons(c.s);
  for (const [k, v] of Object.entries(c.want)) {
    if (got[k] !== v) return `${c.name}: ${k} = ${got[k]}`;
  }
  return null;
});

group("what a tile shows", [
  { name: "a still", card: { url: "/m" }, err: null, want: "ready" },
  { name: "the previous still after a failed change", card: { url: "/m" }, err: "quota", want: "ready" },
  { name: "failed, no still", card: { url: null }, err: "quota", want: "failed" },
  { name: "nothing asked for", card: null, err: null, want: "empty" },
  { name: "being made", card: { url: null }, err: null, want: "generating" },
  { name: "an uploaded pick with no card yet: not made, no spinner", card: { url: null, card_missing: true }, err: null,
    want: "not_made" },
  { name: "a pick shown while no card exists", card: { url: "/a/243", card_missing: true }, err: null, want: "ready" },
], (c) => {
  const got = previewTileStatus(c.card, c.err);
  return got === c.want ? null : `${c.name}: ${got}`;
});

group("the failed re-shoot toast names a button the tile has", [
  () => (/\bRegenerate\b/.test(STILL_REGEN_FAILED_HINT) && !/\bRetry\b/.test(STILL_REGEN_FAILED_HINT) ? null : STILL_REGEN_FAILED_HINT),
], (f) => f());

group("the Refine button says why it waits", [
  () => (stillTileButtons(tile({})).refineTitle === "Type one change above first" ? null : "no text"),
  () => (stillTileButtons(tile({ change: "x" })).refineTitle === "Make this one change on the still (~$0.04)" ? null : "with text"),
], (f) => f());

// Round 3 (U4): every button says whether it is paid. One image from the
// image model is ~$0.04 (the meter's rate); Undo is free.
group("free or paid, said on every still button", [
  ["the image price", STILL_IMAGE_COST, "~$0.04"],
  ["refine", REFINE_TITLE, "Make this one change on the still (~$0.04)"],
  ["regenerate", REGENERATE_TITLE, "Make this still again from its references, the way it was first made (~$0.04)"],
  ["undo", UNDO_TITLE, "Put back the version from before the last change (free)"],
], ([name, got, want]) => (got === want ? null : `${name}: ${JSON.stringify(got)}`));

// The price is the backend's (run.still_image_usd: the meter's rate for the
// image model in use, $0.13 once the Pro model is on), ~$0.04 when unsaid.
group("the price of a still image is the backend's", [
  ["Pro", stillImageCost(0.13), "~$0.13"],
  ["the standard model", stillImageCost(0.04), "~$0.04"],
  ["an older backend", stillImageCost(undefined), STILL_IMAGE_COST],
  ["nothing", stillImageCost(null), STILL_IMAGE_COST],
  ["not a price", stillImageCost(Number.NaN), STILL_IMAGE_COST],
  ["refine", stillTileButtons(tile({ change: "x", cost: "~$0.13" })).refineTitle, "Make this one change on the still (~$0.13)"],
  ["regenerate", stillTileButtons(tile({ cost: "~$0.13" })).regenerateTitle,
    "Make this still again from its references, the way it was first made (~$0.13)"],
  ["no price given", stillTileButtons(tile({ change: "x" })).refineTitle, REFINE_TITLE],
  ["status", stillWorkStatusLine(["reshoot"], 4, "~$0.13"), "Re-shooting the still at its new framing (~$0.13 each)… 0:04"],
  ["status: both", stillWorkStatusLine(["crop", "reshoot"], 4, "~$0.13"), "Re-framing the stills (a crop is free, a re-shoot ~$0.13)… 0:04"],
  ["toast", stillWorkToast(["reshoot"], "~$0.13")[1],
    "The image model re-shoots it at the new framing (~$0.13 each): same person, outfit and room."],
  ["toast: both", stillWorkToast(["crop", "reshoot"], "~$0.13")[1],
    "A crop of a still is free; the others are re-shot by the image model (~$0.13 each)."],
], ([name, got, want]) => (got === want ? null : `${name}: ${JSON.stringify(got)}`));

// A framing change made by the free crop never says "Regenerating" (run 830's
// Close-up crop did, for the whole poll).
group("an edit's background work says what it is", [
  ["a crop", stillBusyLabel(null, 3, "crop"), "Cropping… 0:03"],
  ["a re-shoot", stillBusyLabel(null, 20, "reshoot"), "Re-shooting… 0:20"],
  ["made again", stillBusyLabel(null, 5, "regen"), "Regenerating… 0:05"],
  ["an older backend: made again", stillBusyLabel(null, null, undefined), "Regenerating…"],
  ["an action wins over the work", stillBusyLabel("refine", 1, "crop"), "Refining… 0:01"],
  ["status: crops", stillWorkStatusLine(["crop"], 3), "Cropping the still closer (free)… 0:03"],
  ["status: re-shoots", stillWorkStatusLine(["reshoot", "reshoot"], 20),
    "Re-shooting the still at its new framing (~$0.04 each)… 0:20"],
  ["status: both", stillWorkStatusLine(["crop", "reshoot"], 9),
    "Re-framing the stills (a crop is free, a re-shoot ~$0.04)… 0:09"],
  ["status: a new still", stillWorkStatusLine(["regen", "crop"], 5), "Making the new still… 0:05"],
  ["status: unknown", stillWorkStatusLine([null], 5), "Making the new still… 0:05"],
  ["status: nothing known", stillWorkStatusLine([], null), "Making the new still…"],
  ["toast: a crop", stillWorkToast(["crop"]).join(" | "),
    "Cropping the still | A closer crop of the still: free, the same person to the pixel."],
  ["toast: a re-shoot", stillWorkToast(["reshoot"]).join(" | "),
    "Re-shooting the still | The image model re-shoots it at the new framing (~$0.04 each): same person, outfit and room."],
  ["toast: both", stillWorkToast(["reshoot", "crop"])[0], "Re-framing the stills"],
  ["toast: a new picture", stillWorkToast(["regen"]).join(" | "), "Regenerating still | Applying your new image to the scene…"],
], ([name, got, want]) => (got === want ? null : `${name}: ${JSON.stringify(got)}`));

group("what the tile and the toasts say", [
  ["busy refine", stillBusyLabel("refine"), "Refining…"],
  ["busy regenerate", stillBusyLabel("regenerate"), "Regenerating…"],
  ["busy undo", stillBusyLabel("undo"), "Going back…"],
  ["an edit's own regen", stillBusyLabel(null), "Regenerating…"],
  ["status refine", stillStatusLine("refine", "scene 2's still"), "Refining scene 2's still…"],
  ["status undo", stillStatusLine("undo", "the main still"), "Putting back the previous version of the main still…"],
  ["status regenerate", stillStatusLine("regenerate", "the main still"), "Regenerating the main still…"],
  ["done refine", stillDoneToast("refine", "Scene 2"), "Scene 2 refined"],
  ["done regenerate", stillDoneToast("regenerate", "Main"), "Main regenerated"],
  ["done undo", stillDoneToast("undo", "Scene 1"), "Scene 1: previous version back"],
  ["fail refine", stillFailTitle("refine"), "Refine failed"],
  ["fail regenerate", stillFailTitle("regenerate"), "Regenerate failed"],
  ["fail undo", stillFailTitle("undo"), "Undo failed"],
  ["placeholder", STILL_REFINE_PLACEHOLDER, "One change, e.g. warmer light, zoom out to above the hip"],
], ([name, got, want]) => (got === want ? null : `${name}: ${JSON.stringify(got)}`));

// Run 830's refine sat five minutes with no word: a change says how long
// it has run, and that it stops by itself.
group("a running change counts its time", [
  ["refine at 45 s", stillBusyLabel("refine", 45.4), "Refining… 0:45"],
  ["regenerate at 2 min", stillBusyLabel("regenerate", 125), "Regenerating… 2:05"],
  ["an edit's re-shoot at 3 s", stillBusyLabel(null, 3), "Regenerating… 0:03"],
  ["not started", stillBusyLabel("refine", null), "Refining…"],
  ["status refine at 1:10", stillStatusLine("refine", "scene 2's still", 70), "Refining scene 2's still… 1:10"],
  ["status undo at 0 s", stillStatusLine("undo", "the main still", 0), "Putting back the previous version of the main still… 0:00"],
  ["the limit", STILL_REFINE_LIMIT_HINT, "A change takes up to about three minutes; it stops by itself after that."],
  // Only a refine is bounded: a Regenerate can run longer, an Undo is quick.
  ["the limit of a refine", stillLimitHint("refine"), STILL_REFINE_LIMIT_HINT],
  ["no limit said for a regenerate", stillLimitHint("regenerate"), ""],
  ["no limit said for an undo", stillLimitHint("undo"), ""],
  ["no limit said for an edit's re-shoot", stillLimitHint(null), ""],
], ([name, got, want]) => (got === want ? null : `${name}: ${JSON.stringify(got)}`));

group("no em dash in what the user reads", [
  () => {
    const all = [STILL_REFINE_PLACEHOLDER, stillTileButtons(tile({})).refineTitle, REGENERATE_TITLE,
      AS_IS_REGENERATE_HINT, STILL_KEPT_AFTER_ERROR, STILL_REGEN_FAILED_HINT, UNDO_TITLE,
      stillTileButtons(tile({ change: "x" })).refineTitle,
      ...["crop", "reshoot", "regen"].flatMap((w) => [stillBusyLabel(null, 1, w), stillWorkStatusLine([w], 1),
        ...stillWorkToast([w])]),
      stillWorkStatusLine(["crop", "reshoot"], 1), ...stillWorkToast(["crop", "reshoot"]),
      ...["refine", "regenerate", "undo"].flatMap((a) => [stillBusyLabel(a), stillStatusLine(a, "x"),
        stillDoneToast(a, "x"), stillFailTitle(a)])].join(" ");
    return all.includes(String.fromCharCode(0x2014)) ? "em dash" : null;
  },
], (f) => f());

// The Stills status line says the limit only where it holds (stillLimitHint):
// a slow Regenerate was told it would stop after three minutes.
const creator = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "src", "components",
  "yas", "TalkingHeadV2Creator.tsx"), "utf8");
group("the Stills status line says the limit only for a refine", [
  ["no unconditional limit", !creator.includes("STILL_REFINE_LIMIT_HINT")],
  ["both still lines ask stillLimitHint", (creator.match(/stillLimitHint\(stillAction\)/g) ?? []).length === 2],
], ([name, ok]) => (ok ? null : name));

// Each still tile's overlay names its own background work (keyframe_work),
// the polling status line and the toast too; no framing button says
// "re-shoot" for a crop (the titles come from lib/thFraming).
group("the Stills step says what the background work is", [
  ["both tiles pass their scene's work",
    (creator.match(/stillBusyLabel\(null, phase === "polling_stills" \? stillSeconds : null,\s*[\w[\]?.]*keyframe_work\)/g) ?? [])
      .length === 2],
  ["the polling line", creator.includes("stillWorkStatusLine(")],
  ["the apply toast", creator.includes("stillWorkToast(")],
  ["the framing titles", creator.includes("framingButtonTitle(") && !creator.includes(": re-shoot this still at it.")],
  ["Undo says it is free", creator.includes("title={UNDO_TITLE}")],
  // The price said is the run's (the backend's meter rate), everywhere.
  ["the price is the run's", creator.includes("const imageCost = stillImageCost(run?.still_image_usd);")],
  ["both still tiles get it", (creator.match(/imageCost=\{imageCost\}/g) ?? []).length === 2],
  ["the tile's buttons and framing titles",
    creator.includes("cost: imageCost") && /framingButtonTitle\([^)]*, imageCost\)/.test(creator)],
  ["the Framing hint", creator.includes("framingHint(framing, run, imageCost)")],
  ["the framing toast", creator.includes("framingChangeToast(work, value, stillImageCost(fresh.still_image_usd))")],
  ["the apply toast", /stillWorkToast\([^;]*stillImageCost\(fresh\.still_image_usd\)/.test(creator)],
  ["the polling line", /stillWorkStatusLine\([^;]*stillSeconds,\s*imageCost,/.test(creator)],
], ([name, ok]) => (ok ? null : name));

console.log(failed ? `${failed} of ${total} differ` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
