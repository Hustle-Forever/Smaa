// Checks the Talking Head v2 intro/outro card rules (src/lib/thBookends.ts):
// a pick is kept as-is by default, a card counts as made only when it is a
// real card from the image picked now, the videos wait for every wanted card,
// "editing an existing run" needs generate-previews to have run (run 827's
// trap), and what a card tile says.
//
//   node --experimental-strip-types --no-warnings scripts/check-th-bookends.mjs
//
// (node 22.6+). Prints one line per group and exits 1 on any difference.

import {
  CARD_FIRST_NOTE,
  DEFAULT_REF_MODE,
  MOTION_DECIDED_LATER,
  REF_MODE_CHOICES,
  bookendClipWaits,
  bookendWanted,
  cardReady,
  cardRetryButton,
  cardTileNote,
  cardsPhrase,
  clipMotionLine,
  editingExisting,
  missingCards,
  normRefMode,
  previewsReady,
  refModeHint,
} from "../src/lib/thBookends.ts";

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

const card = (over) => ({ id: 81, status: "ready", card_type: "intro", url: "/c/81", ...over });
const pick = (over) => ({ id: null, status: "ready", card_type: "intro", url: "/a/243", source: "asset", card_missing: true, ...over });
const run = (over) => ({
  main: { status: "ready", url: "/m", width: 720, height: 1280, error: null },
  scenes: [{ index: 0, keyframe_url: "/m" }],
  intro: card({}),
  outro: card({ id: 82, card_type: "outro", url: "/c/82" }),
  intro_excluded: false,
  outro_excluded: false,
  refs: {},
  ...over,
});

group("a pick is kept as-is unless asked otherwise", [
  { m: undefined, want: "as_is" },
  { m: null, want: "as_is" },
  { m: "", want: "as_is" },
  { m: "junk", want: "as_is" },
  { m: "as_is", want: "as_is" },
  { m: "new_vibe", want: "new_vibe" },
  { m: "text_only", want: "text_only" },
], (c) => {
  const got = normRefMode(c.m);
  return got === c.want ? null : `${c.m}: got ${got}`;
});

group("the mode control", [
  () => (DEFAULT_REF_MODE === "as_is" ? null : DEFAULT_REF_MODE),
  () => (REF_MODE_CHOICES.map((c) => c[0]).join() === "as_is,new_vibe" ? null : "choices"),
  () => (REF_MODE_CHOICES[0][1] === "Keep as-is" ? null : REF_MODE_CHOICES[0][1]),
  () => (REF_MODE_CHOICES[1][1] === "New design in this style" ? null : REF_MODE_CHOICES[1][1]),
  () => (/pixel for pixel/.test(refModeHint("as_is")) ? null : "as_is hint"),
  () => (/new card/.test(refModeHint("new_vibe")) ? null : "new_vibe hint"),
  () => (/Made before Keep as-is/.test(refModeHint("text_only")) ? null : "text_only hint"),
  () => ([...REF_MODE_CHOICES.flat(), refModeHint("as_is"), refModeHint("new_vibe"), refModeHint("text_only")]
    .join(" ").includes(String.fromCharCode(0x2014)) ? "em dash" : null),
], (f) => f());

group("a card counts as made only when it is a real card from the pick", [
  { name: "a card", c: card({}), want: true },
  { name: "no card", c: null, want: false },
  { name: "the pick shown while no card exists", c: pick({}), want: false },
  { name: "a card from another image (run 824)", c: card({ matches_refs: false }), want: false },
  { name: "a card from this image", c: card({ matches_refs: true }), want: true },
  { name: "a card being made (no url)", c: card({ url: null }), want: false },
  { name: "an older backend's card (no flags)", c: { id: 9, status: "ready", card_type: "intro", url: "/c/9" }, want: true },
], (c) => {
  const got = cardReady(c.c);
  return got === c.want ? null : `${c.name}: got ${got}`;
});

group("which bookends want a card", [
  { name: "both", r: run({}), want: ["intro", "outro"] },
  { name: "intro skipped", r: run({ intro_excluded: true }), want: ["outro"] },
  { name: "outro is an uploaded video", r: run({ refs: { outro_video_upload_id: 7 } }), want: ["intro"] },
  { name: "no run", r: null, want: [] },
], (c) => {
  const got = ["intro", "outro"].filter((role) => bookendWanted(c.r, role));
  return got.join() === c.want.join() ? null : `${c.name}: got ${got}`;
});

group("the videos wait for every wanted card", [
  { name: "all made", r: run({}), missing: [], ready: true },
  { name: "intro pick, no card (run 827)", r: run({ intro: pick({}) }), missing: ["intro"], ready: false },
  { name: "outro card from another image (run 824)", r: run({ outro: card({ id: 82, matches_refs: false }) }),
    missing: ["outro"], ready: false },
  { name: "intro failed, nothing shown", r: run({ intro: null }), missing: ["intro"], ready: false },
  { name: "a skipped intro needs no card", r: run({ intro: null, intro_excluded: true }), missing: [], ready: true },
  { name: "an uploaded outro video needs no card", r: run({ outro: null, refs: { outro_video_upload_id: 3 } }),
    missing: [], ready: true },
  { name: "both missing", r: run({ intro: null, outro: pick({ card_type: "outro" }) }),
    missing: ["intro", "outro"], ready: false },
  { name: "no main still", r: run({ main: { status: "ready", url: null } }), missing: [], ready: false },
], (c) => {
  const m = missingCards(c.r);
  const r = previewsReady(c.r);
  return m.join() === c.missing.join() && r === c.ready ? null : `${c.name}: missing ${m}, ready ${r}`;
});

group("editing an existing run means generate-previews ran", [
  { name: "script only", r: run({ main: { url: null }, intro: null, outro: null, scenes: [] }), want: false },
  { name: "run 827: a later scene's keyframe only", r: run({ main: { url: null }, intro: pick({}), outro: null,
    scenes: [{ index: 0, keyframe_url: null }, { index: 1, keyframe_url: "/s1" }] }), want: false },
  { name: "scene 0's still", r: run({ intro: null, outro: null }), want: true },
  { name: "scene 0 failed, a card made", r: run({ main: { url: null } }), want: true },
  { name: "scene 0 failed, no card row (later scenes made)",
    r: run({ main: { url: null, error: "main_keyframe_failed: 429" }, intro: null, outro: null,
             scenes: [{ index: 0, keyframe_url: null }, { index: 1, keyframe_url: "/s1" }] }), want: true },
  { name: "scene 0's error kept in the metadata only",
    r: run({ main: { url: null }, main_error: "quota", intro: null, outro: null }), want: true },
  { name: "no run", r: null, want: false },
], (c) => {
  const got = editingExisting(c.r);
  return got === c.want ? null : `${c.name}: got ${got}`;
});

group("what a card tile says", [
  // No clip yet: the plan, said as a plan.
  { name: "kept, text read off it, a slow zoom planned",
    c: card({ as_is: true, card_text: "42\nABU DHABI", card_text_source: "detected", motion: "zoom" }),
    want: { kept: true, needsMaking: false, textLine: "Text on the card: 42 / ABU DHABI", motionLine: "Motion planned: slow zoom" } },
  { name: "drawn, text asked for, icons planned",
    c: card({ card_text: "Apply Today\n42abudhabi.ae", card_text_source: "planned", motion: "veo" }),
    want: { kept: false, needsMaking: false, textLine: "Text asked for: Apply Today / 42abudhabi.ae",
            motionLine: "Motion planned: icons move (Veo)" } },
  // A clip made: what it turned out to be, not the plan (run 830's intro
  // said "icons move" while its clip was the slow zoom).
  { name: "Veo changed the card: the slow zoom replaced it",
    c: card({ as_is: true, motion: "veo" }), clip: { motion: "veo_replaced" },
    want: { kept: true, needsMaking: false, textLine: null,
            motionLine: "Motion: slow zoom (Veo changed the card, so it was replaced)" } },
  { name: "Veo moved its icons", c: card({ motion: "veo" }), clip: { motion: "veo" },
    want: { kept: false, needsMaking: false, textLine: null, motionLine: "Motion: icons move (Veo)" } },
  { name: "the slow zoom", c: card({ motion: "zoom" }), clip: { motion: "zoom" },
    want: { kept: false, needsMaking: false, textLine: null, motionLine: "Motion: slow zoom" } },
  { name: "still being checked", c: card({ motion: "veo" }), clip: { motion: "veo_checking" },
    want: { kept: false, needsMaking: false, textLine: null, motionLine: "Motion: icons move (Veo, being checked)" } },
  { name: "a clip of an older card (out of date): the plan for the next",
    c: card({ motion: "zoom" }), clip: { motion: "veo" }, stale: true,
    want: { kept: false, needsMaking: false, textLine: null, motionLine: "Motion planned: slow zoom" } },
  { name: "an older clip with no record: the plan",
    c: card({ motion: "veo" }), clip: { motion: null },
    want: { kept: false, needsMaking: false, textLine: null, motionLine: "Motion planned: icons move (Veo)" } },
  { name: "kept, not read yet", c: card({ as_is: true, card_text: null }),
    want: { kept: true, needsMaking: false, textLine: null, motionLine: null } },
  // Run 830's outro: its card's plan was made under older rules ("animate
  // icons"), so the backend sends none: never shown as the plan.
  { name: "no plan under the current rules, no clip", c: card({ as_is: true, motion: null }),
    want: { kept: true, needsMaking: false, textLine: null, motionLine: MOTION_DECIDED_LATER } },
  { name: "no plan, its clip out of date", c: card({ motion: null }), clip: { motion: "veo" }, stale: true,
    want: { kept: false, needsMaking: false, textLine: null, motionLine: MOTION_DECIDED_LATER } },
  { name: "no plan, a clip of it: what the clip became", c: card({ motion: null }), clip: { motion: "veo" },
    want: { kept: false, needsMaking: false, textLine: null, motionLine: "Motion: icons move (Veo)" } },
  { name: "no plan, an older clip with no record", c: card({ motion: null }), clip: { motion: null },
    want: { kept: false, needsMaking: false, textLine: null, motionLine: MOTION_DECIDED_LATER } },
  { name: "the pick, no card: nothing about motion", c: pick({ motion: null }),
    want: { kept: false, needsMaking: true, textLine: null, motionLine: null } },
  { name: "the pick, no card", c: pick({}), want: { kept: false, needsMaking: true, textLine: null, motionLine: null } },
  { name: "from another image", c: card({ as_is: true, matches_refs: false, card_text: "x", motion: "zoom" }),
    want: { kept: false, needsMaking: true, textLine: null, motionLine: null } },
  { name: "no card", c: null, want: { kept: false, needsMaking: false, textLine: null, motionLine: null } },
], (c) => {
  const got = cardTileNote(c.c, c.clip, !!c.stale);
  if (got.motionLine && got.motionLine.includes(String.fromCharCode(0x2014))) return `${c.name}: em dash`;
  return JSON.stringify(got) === JSON.stringify(c.want) ? null : `${c.name}: got ${JSON.stringify(got)}`;
});

group("a bookend clip's Videos tile says what it became", [
  { clip: { motion: "veo_replaced" }, want: "Motion: slow zoom (Veo changed the card, so it was replaced)" },
  { clip: { motion: "zoom" }, want: "Motion: slow zoom" },
  { clip: { motion: "veo" }, want: "Motion: icons move (Veo)" },
  // The check failed but no slow zoom could be made: never "icons move".
  { clip: { motion: "veo_flagged" }, want: "Motion: Veo, which changed the card (the slow zoom could not be made)" },
  { clip: { motion: "veo_unchecked" }, want: "Motion: icons move (Veo, not checked)" },
  { clip: { motion: null }, want: null },
  { clip: null, want: null },
], (c) => {
  const got = clipMotionLine(c.clip);
  return got === c.want ? null : `${JSON.stringify(c.clip)}: got ${got}`;
});

group("the card button works without text", [
  { fb: "", needs: false, want: "Make again" },
  { fb: "   ", needs: false, want: "Make again" },
  { fb: "bolder colours", needs: false, want: "Retry" },
  { fb: "", needs: true, want: "Make card" },
  { fb: "bolder", needs: true, want: "Make card" },
], (c) => {
  const got = cardRetryButton(c.fb, c.needs).label;
  return got === c.want ? null : `${JSON.stringify(c.fb)} ${c.needs}: got ${got}`;
});

group("a bookend's clip waits for its card (no retry of the bare pick)", [
  { name: "a kept intro switched to a new design after the clips", r: run({ intro: pick({}) }), role: "intro", want: CARD_FIRST_NOTE },
  { name: "its card made", r: run({}), role: "intro", want: null },
  { name: "a card from another image", r: run({ outro: card({ id: 82, card_type: "outro", matches_refs: false }) }),
    role: "outro", want: CARD_FIRST_NOTE },
  { name: "an uploaded video needs no card", r: run({ intro: null, refs: { intro_video_upload_id: 9 } }), role: "intro", want: null },
  { name: "skipped", r: run({ intro: null, intro_excluded: true }), role: "intro", want: null },
  { name: "no run", r: null, role: "intro", want: null },
], (c) => {
  const got = bookendClipWaits(c.r, c.role);
  if (got !== c.want) return `${c.name}: ${got}`;
  return CARD_FIRST_NOTE.includes(String.fromCharCode(0x2014)) ? "em dash" : null;
});

group("how the cards are named", [
  { r: ["intro"], want: "the intro card" },
  { r: ["outro"], want: "the outro card" },
  { r: ["intro", "outro"], want: "the intro and outro cards" },
  { r: [], want: "the cards" },
], (c) => {
  const got = cardsPhrase(c.r);
  return got === c.want ? null : `${c.r}: got ${got}`;
});

console.log(failed ? `${failed} of ${total} differ` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
