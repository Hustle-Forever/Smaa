// Checks the Talking Head v2 Speaker rules (src/lib/thSpeaker.ts): when the
// Speaker field applies, when lock_character is sent, what an edit sends for
// the speaker, and which chip shows as chosen.
//
//   node --experimental-strip-types --no-warnings scripts/check-th-speaker.mjs
//
// (node 22.6+). Prints one line per group and exits 1 on any difference.

import {
  SPEAKER_CHIPS,
  SPEAKER_HINT,
  activeChip,
  anyCharacterSet,
  chipClick,
  lockCharacterField,
  speakerApplies,
  speakerDraftFromRun,
  speakerPatch,
  speakerSourceLabel,
} from "../src/lib/thSpeaker.ts";

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

const MAN = "an Emirati man in his early 20s with a short, neat beard, wearing a crisp white kandura";
const WOMAN = "an Emirati woman in her late 20s wearing a navy abaya and a matching navy shayla";
const run = (speaker) => ({ id: 1, speaker });

group("the Speaker field shows while no character is picked", [
  { name: "single, nothing picked", o: { sceneCount: 1, globalCharacter: false, sceneCharacters: [] }, want: true },
  { name: "single, a character", o: { sceneCount: 1, globalCharacter: true, sceneCharacters: [] }, want: false },
  { name: "single ignores scene slots", o: { sceneCount: 1, globalCharacter: false, sceneCharacters: [true] }, want: true },
  { name: "multi, nothing picked", o: { sceneCount: 3, globalCharacter: false, sceneCharacters: [false, false, false] }, want: true },
  { name: "multi, scene 2 picked (scene 0 still the speaker)", o: { sceneCount: 3, globalCharacter: false, sceneCharacters: [false, false, true] }, want: true },
  { name: "multi, scene 0 picked (every unpicked scene follows it)", o: { sceneCount: 3, globalCharacter: false, sceneCharacters: [true, false, false] }, want: false },
  { name: "multi, a run-wide character", o: { sceneCount: 2, globalCharacter: true, sceneCharacters: [false, false] }, want: false },
], (c) => {
  const got = speakerApplies(c.o);
  return got === c.want ? null : `${c.name}: got ${got}`;
});

group("a character is set", [
  { o: { sceneCount: 1, globalCharacter: false, sceneCharacters: [true, true] }, want: false },
  { o: { sceneCount: 1, globalCharacter: true, sceneCharacters: [] }, want: true },
  { o: { sceneCount: 2, globalCharacter: false, sceneCharacters: [false, true] }, want: true },
  { o: { sceneCount: 2, globalCharacter: false, sceneCharacters: [false, false, true] }, want: false },
], (c) => {
  const got = anyCharacterSet(c.o);
  return got === c.want ? null : `${JSON.stringify(c.o)}: got ${got}`;
});

group("lock_character only with a character", [
  { set: false, lock: true, want: {} },
  { set: false, lock: false, want: {} },
  { set: true, lock: true, want: { lock_character: true } },
  { set: true, lock: false, want: { lock_character: false } },
], (c) => {
  const got = JSON.stringify(lockCharacterField(c.set, c.lock));
  return got === JSON.stringify(c.want) ? null : `set=${c.set} lock=${c.lock}: got ${got}`;
});

const def = { subject: MAN, gender: "man", source: "default_emirati" };
group("what an edit sends for the speaker", [
  { name: "unchanged", draft: MAN, run: run(def), applies: true, want: {} },
  { name: "unchanged but for spaces and case", draft: `  ${MAN.toUpperCase()} `, run: run(def), applies: true, want: {} },
  { name: "empty", draft: "   ", run: run(def), applies: true, want: {} },
  { name: "a chip", draft: "Emirati woman", run: run(def), applies: true, want: { speaker_subject: "Emirati woman" } },
  { name: "free text, trimmed", draft: "  a Brazilian student in a hoodie ", run: run(def), applies: true, want: { speaker_subject: "a Brazilian student in a hoodie" } },
  { name: "a character is picked", draft: "Emirati woman", run: run(def), applies: false, want: {} },
  { name: "older run without a speaker", draft: "Emirati man", run: run(null), applies: true, want: { speaker_subject: "Emirati man" } },
  { name: "no run", draft: "Emirati man", run: null, applies: true, want: { speaker_subject: "Emirati man" } },
], (c) => {
  const got = JSON.stringify(speakerPatch(c.draft, c.run, c.applies));
  return got === JSON.stringify(c.want) ? null : `${c.name}: got ${got}`;
});

group("the field starts from the run's speaker", [
  { run: run(def), want: MAN },
  { run: run(null), want: "" },
  { run: null, want: "" },
], (c) => {
  const got = speakerDraftFromRun(c.run);
  return got === c.want ? null : `got ${JSON.stringify(got)}`;
});

group("the chip shown as chosen", [
  { draft: "Emirati man", run: run(def), want: "Emirati man" },
  { draft: "an emirati WOMAN", run: run(def), want: "Emirati woman" },
  { draft: MAN, run: run(def), want: "Emirati man" },
  { draft: WOMAN, run: run({ subject: WOMAN, gender: "woman", source: "user" }), want: "Emirati woman" },
  { draft: MAN + " in glasses", run: run(def), want: null },
  { draft: "a Brazilian student", run: run({ subject: "a Brazilian student", gender: null, source: "topic" }), want: null },
  { draft: "", run: run(def), want: null },
], (c) => {
  const got = activeChip(c.draft, c.run);
  return got === c.want ? null : `${JSON.stringify(c.draft)}: got ${got}`;
});

group("a chip, and the field after a save", [
  // The chip already chosen (the run's own default man) changes nothing.
  () => (chipClick("Emirati man", MAN, run({ subject: MAN, gender: "man", source: "default_emirati" })) === null
    ? null : "the chosen chip changed the draft"),
  () => (chipClick("Emirati woman", MAN, run({ subject: MAN, gender: "man", source: "default_emirati" })) === "Emirati woman"
    ? null : "the other chip"),
  () => (chipClick("Emirati man", "", run(null)) === "Emirati man" ? null : "no speaker yet"),
  // Saving the chip of the current cast keeps the same subject: the field
  // set from the saved run is clean (nothing sent again, no "Unsaved").
  () => {
    const saved = run({ subject: MAN, gender: "man", source: "user" });
    const draft = speakerDraftFromRun(saved);
    return draft === MAN && Object.keys(speakerPatch(draft, saved, true)).length === 0 ? null : "still dirty after a save";
  },
], (f) => f());

group("labels and copy", [
  () => (speakerSourceLabel(def) === "Default" ? null : speakerSourceLabel(def)),
  () => (speakerSourceLabel({ ...def, source: "topic" }) === "From the topic" ? null : "topic label"),
  () => (speakerSourceLabel({ ...def, source: "user" }) === "Yours" ? null : "user label"),
  () => (speakerSourceLabel(null) === null ? null : "null speaker"),
  () => (SPEAKER_CHIPS.join("|") === "Emirati man|Emirati woman" ? null : SPEAKER_CHIPS.join("|")),
  () => (SPEAKER_HINT.includes(String.fromCharCode(0x2014)) ? "em dash" : null),
], (f) => f());

console.log(failed ? `${failed} of ${total} differ` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
