// Checks the Talking Head v2 intro/outro "Previous videos" and kept-card
// words rules (src/lib/thBookends.ts, Part B): how a previous video's card
// is held by a slot and shown in the picker (its shape against the video's,
// its reusable clip), what a kept card's tile says about its words (swapped,
// kept, a failed swap) and its reused clip, and when the script's bookend
// words are the kept card's own (read-only).
//
//   node --experimental-strip-types --no-warnings scripts/check-th-previous-cards.mjs
//
// (node 22.6+). Prints one line per group and exits 1 on any difference.

import {
  cardImageUrl,
  cardTextLocked,
  cardWordsNote,
  keepTextHint,
  pickedCardFrom,
  pickedCardFromRefs,
  pickedCardLabel,
  previousCardNote,
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
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const prev = (over) => ({
  card_id: 825, role: "intro", run_id: 824, topic: "Decode your tech future",
  url: "/api/yas-producer/talking-head-v2/cards/825/file", card_text: "Decode Your Tech Future\n42 Abu Dhabi",
  aspect: "9:16", width: 720, height: 1280, as_is: false, clip_animation_id: null, clip_style: null,
  created_at: null, ...over,
});

group("a previous video's card in a slot", [
  { name: "from the list", got: () => pickedCardFrom(prev({})),
    want: { card_id: 825, url: "/api/yas-producer/talking-head-v2/cards/825/file",
            topic: "Decode your tech future", card_text: "Decode Your Tech Future\n42 Abu Dhabi" } },
  { name: "no url in the list", got: () => pickedCardFrom(prev({ url: "", topic: "" })).url,
    want: "/api/yas-producer/talking-head-v2/cards/825/file" },
  { name: "on resume", got: () => pickedCardFromRefs(826),
    want: { card_id: 826, url: cardImageUrl(826), topic: null, card_text: null } },
  { name: "no card on resume", got: () => pickedCardFromRefs(null), want: null },
  { name: "undefined on resume", got: () => pickedCardFromRefs(undefined), want: null },
  { name: "label with a topic", got: () => pickedCardLabel(pickedCardFrom(prev({}))),
    want: 'From "Decode your tech future"' },
  { name: "label on resume", got: () => pickedCardLabel(pickedCardFromRefs(826)),
    want: "Card #826 from an earlier video" },
], (c) => (same(c.got(), c.want) ? null : `${c.name}: got ${JSON.stringify(c.got())}`));

group("what a Previous videos tile says", [
  { name: "same shape, no clip", c: prev({}), v: "9:16",
    want: { title: 'From "Decode your tech future"', text: "Decode Your Tech Future / 42 Abu Dhabi",
            shape: "9:16", clip: null } },
  { name: "same shape, a zoom clip", c: prev({ clip_animation_id: 400, clip_style: "slow_zoom" }), v: "9:16",
    want: { title: 'From "Decode your tech future"', text: "Decode Your Tech Future / 42 Abu Dhabi",
            shape: "9:16", clip: "Clip ready (slow zoom): reused if its text stays" } },
  { name: "an icons clip", c: prev({ clip_animation_id: 401, clip_style: "animate_icons", card_text: null }), v: "9:16",
    want: { title: 'From "Decode your tech future"', text: null, shape: "9:16",
            clip: "Clip ready (its icons move): reused if its text stays" } },
  { name: "another video size: fitted, no clip", c: prev({ clip_animation_id: 400, clip_style: "slow_zoom" }),
    v: "16:9", want: { title: 'From "Decode your tech future"', text: "Decode Your Tech Future / 42 Abu Dhabi",
                       shape: "9:16: fitted to 16:9", clip: null } },
  { name: "an older square card", c: prev({ aspect: null, width: 1024, height: 1024, topic: "" }), v: "9:16",
    want: { title: "Card #825", text: "Decode Your Tech Future / 42 Abu Dhabi",
            shape: "1024x1024: fitted to 9:16", clip: null } },
  { name: "no size known", c: prev({ aspect: null, width: null, height: null, card_text: "  \n " }), v: "9:16",
    want: { title: 'From "Decode your tech future"', text: null, shape: "another size: fitted to 9:16", clip: null } },
], (c) => {
  const got = previousCardNote(c.c, c.v);
  return same(got, c.want) ? null : `${c.name}: got ${JSON.stringify(got)}`;
});

group("the Keep card text hint", [
  { on: true, has: "keeps its own words" },
  { on: false, has: "swapped for your script's words" },
  { on: false, has: "calls to action never change" },
], (c) => (keepTextHint(c.on).includes(c.has) ? null : `${c.on}: ${keepTextHint(c.on)}`));

const card = (over) => ({ id: 901, status: "ready", card_type: "intro", url: "/c/901", as_is: true,
                          matches_refs: true, ...over });
const swap = (status, extra = {}) => ({ status, warning: null, swapped: [], ...extra });
const none = { swapLine: null, warning: null, clipLine: null };

group("what a kept card's tile says about its words", [
  { name: "swapped", c: card({ text_swap: swap("swapped", { swapped: [
      { kind: "headline", old: "Decode Your Tech Future", new: "Learn from your peers" }] }) }),
    want: { swapLine: 'Text swapped: "Decode Your Tech Future" for "Learn from your peers"', warning: null, clipLine: null } },
  { name: "two lines swapped", c: card({ text_swap: swap("swapped", { swapped: [
      { kind: "headline", old: "A", new: "B" }, { kind: "subtitle", old: "c", new: "d" }] }) }),
    want: { swapLine: 'Text swapped: "A" for "B"; "c" for "d"', warning: null, clipLine: null } },
  // Run 830's card 835: the headline swapped, the subtitle kept its words
  // (the swap is judged line by line): both are said.
  { name: "a line kept its own words", c: card({ text_swap: swap("swapped", {
      swapped: [{ kind: "headline", old: "DECODE YOUR TECH FUTURE", new: "Why Peer Learning Works" }],
      kept: [{ kind: "subtitle", old: "Start your journey in tech today", new: "No teachers, just collaboration",
               why: "ran outside" }],
      warning: "The intro card's subtitle couldn't be swapped cleanly" }) }),
    want: { swapLine: 'Text swapped: "DECODE YOUR TECH FUTURE" for "Why Peer Learning Works"',
            warning: "The intro card's subtitle couldn't be swapped cleanly", clipLine: null } },
  { name: "kept", c: card({ keep_text: true, text_swap: swap("kept") }),
    want: { swapLine: "Keeps its own words", warning: null, clipLine: null } },
  { name: "a failed swap", c: card({ text_swap: swap("failed", { warning: "couldn't be swapped cleanly" }) }),
    want: { swapLine: null, warning: "couldn't be swapped cleanly", clipLine: null } },
  { name: "unread", c: card({ text_swap: swap("unread", { warning: "couldn't be read" }) }),
    want: { swapLine: null, warning: "couldn't be read", clipLine: null } },
  { name: "nothing to swap", c: card({ text_swap: swap("unchanged") }), want: none },
  { name: "a reused clip", c: card({ text_swap: swap("unchanged"), reused_clip: true }),
    want: { swapLine: null, warning: null, clipLine: "Clip reused from the earlier video (no new Veo)" } },
  { name: "a drawn card", c: card({ as_is: false, text_swap: swap("swapped", { swapped: [{ kind: "headline", old: "a", new: "b" }] }) }),
    want: none },
  { name: "the pick only", c: card({ id: null, card_missing: true, text_swap: swap("failed", { warning: "x" }) }),
    want: none },
  { name: "made from another image", c: card({ matches_refs: false, reused_clip: true }), want: none },
  { name: "an older card", c: card({}), want: none },
  { name: "no card", c: null, want: none },
], (c) => {
  const got = cardWordsNote(c.c);
  return same(got, c.want) ? null : `${c.name}: got ${JSON.stringify(got)}`;
});

const run = (over) => ({
  main: { status: "ready", url: "/m", width: 720, height: 1280, error: null },
  intro: card({}), outro: card({ id: 902, card_type: "outro", url: "/c/902" }),
  intro_excluded: false, outro_excluded: false, refs: {}, ...over,
});

group("the script's bookend words are the kept card's", [
  { name: "keep on, a kept card", r: run({ refs: { intro_keep_text: true } }), role: "intro", want: true },
  { name: "the other bookend", r: run({ refs: { intro_keep_text: true } }), role: "outro", want: false },
  { name: "keep off", r: run({ refs: { intro_keep_text: false } }), role: "intro", want: false },
  { name: "outro keep on", r: run({ refs: { outro_keep_text: true } }), role: "outro", want: true },
  { name: "a drawn card", r: run({ refs: { intro_keep_text: true }, intro: card({ as_is: false }) }), role: "intro", want: false },
  { name: "no card made yet", r: run({ refs: { intro_keep_text: true }, intro: card({ id: null, card_missing: true }) }),
    role: "intro", want: false },
  { name: "a card from another image", r: run({ refs: { intro_keep_text: true }, intro: card({ matches_refs: false }) }),
    role: "intro", want: false },
  { name: "no run", r: null, role: "intro", want: false },
], (c) => {
  const got = cardTextLocked(c.r, c.role);
  return got === c.want ? null : `${c.name}: got ${got}`;
});

console.log(failed ? `${failed} of ${total} differ` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
