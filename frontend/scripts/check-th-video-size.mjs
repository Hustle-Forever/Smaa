// Checks the Talking Head v2 Video size rules (src/lib/thVideoSize.ts): when
// the size is fixed (the backend's aspect_locked, or what an older backend's
// run shows), and which stills are not exactly the video's shape.
//
//   node --experimental-strip-types --no-warnings scripts/check-th-video-size.mjs
//
// (node 22.6+). Prints one line per group and exits 1 on any difference.

import { stillShapeOff, videoSizeLocked, VIDEO_SIZE_HINT, VIDEO_SIZE_LOCKED_HINT } from "../src/lib/thVideoSize.ts";

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

const run = (over) => ({
  main: { status: "ready", url: null, width: null, height: null, error: null },
  scenes: [{ index: 0, keyframe_url: null }],
  intro: null,
  outro: null,
  ...over,
});

group("the size is fixed once a still or card exists", [
  { name: "no run", run: null, want: false },
  { name: "script only", run: run({}), want: false },
  { name: "backend says locked", run: run({ aspect_locked: true }), want: true },
  { name: "backend says open, whatever the run shows", run: run({ aspect_locked: false, main: { url: "/m" } }), want: false },
  { name: "older backend: scene 0 still", run: run({ main: { url: "/m" } }), want: true },
  { name: "older backend: a scene still", run: run({ scenes: [{ index: 0, keyframe_url: null }, { index: 1, keyframe_url: "/s1" }] }), want: true },
  { name: "older backend: a generated intro card", run: run({ intro: { id: 81, url: "/c/81" } }), want: true },
  { name: "older backend: a picked outro asset is no card", run: run({ outro: { id: null, url: "/a/9", source: "asset" } }), want: false },
], (c) => {
  const got = videoSizeLocked(c.run);
  return got === c.want ? null : `${c.name}: got ${got}`;
});

group("a still not exactly the video's shape", [
  { w: 720, h: 1280, a: "9:16", want: false },
  { w: 756, h: 1344, a: "9:16", want: false },
  { w: 1080, h: 1920, a: "9:16", want: false },
  { w: 768, h: 1344, a: "9:16", want: true },
  { w: 1248, h: 832, a: "9:16", want: true },
  { w: 1024, h: 1024, a: "9:16", want: true },
  { w: 1280, h: 720, a: "16:9", want: false },
  { w: 1344, h: 756, a: "16:9", want: false },
  { w: 1344, h: 768, a: "16:9", want: true },
  { w: 720, h: 1280, a: "16:9", want: true },
  { w: 720, h: 1280, a: null, want: false },
  { w: null, h: 1280, a: "9:16", want: false },
  { w: undefined, h: undefined, a: "9:16", want: false },
], (c) => {
  const got = stillShapeOff(c.w, c.h, c.a);
  return got === c.want ? null : `${c.w}x${c.h} at ${c.a}: got ${got}`;
});

group("the hints", [
  () => (VIDEO_SIZE_LOCKED_HINT.startsWith("Video size is fixed once stills are made") ? null : VIDEO_SIZE_LOCKED_HINT),
  () => (/re-renders everything/.test(VIDEO_SIZE_HINT) ? "the old promise is back" : null),
  () => ((VIDEO_SIZE_HINT + VIDEO_SIZE_LOCKED_HINT).includes(String.fromCharCode(0x2014)) ? "em dash" : null),
], (f) => f());

console.log(failed ? `${failed} of ${total} differ` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
