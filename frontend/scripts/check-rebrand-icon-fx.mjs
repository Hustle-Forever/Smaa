// Checks src/lib/rebrandIconFx.ts against the shared fixture the server's
// icon_render.fx_progress is tested with (backend/tests/fixtures/
// rebrand_icon_fx.json), so an icon moves in the studio's moving preview
// exactly as the run stamps it, frame for frame.
//
//   node --experimental-strip-types scripts/check-rebrand-icon-fx.mjs
//
// (node 22.6+; the backend test runs it too when node is there). Prints one
// line per case and exits 1 on any difference.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { framesOf, fxProgress, nearestEdge } from "../src/lib/rebrandIconFx.ts";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(
  readFileSync(resolve(here, "../../backend/tests/fixtures/rebrand_icon_fx.json"), "utf-8"),
);
const EPS = 1e-9;
const NUMS = ["alpha", "scale", "slide", "amount", "reveal", "unfold", "words"];

let failed = 0;
let total = 0;
for (const c of fixture.cases) {
  total += 1;
  let ok = true;
  c.frames.forEach((want, i) => {
    const f = i - 1;
    const got = fxProgress(c.entrance, f, c.end, c.fin, c.fout, c.exit);
    const same =
      want === null || got === null
        ? want === got
        : got.kind === want.kind && got.phase === want.phase && NUMS.every((k) => Math.abs(got[k] - want[k]) < EPS);
    if (!same) {
      ok = false;
      console.log(`  frame ${f}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
    }
  });
  console.log(`${ok ? "ok  " : "FAIL"} ${c.entrance}${c.exit ? ` > ${c.exit}` : ""} ${c.end}/${c.fin}/${c.fout}`);
  if (!ok) failed += 1;
}
for (const t of fixture.timing) {
  total += 1;
  const got = framesOf(t.t_rel, t.span, t.in_sec, t.out_sec, t.fps);
  const ok = got.f === t.f && got.end === t.end && got.fin === t.fin && got.fout === t.fout;
  console.log(`${ok ? "ok  " : "FAIL"} frames of ${t.t_rel} s at ${t.fps} fps`);
  if (!ok) {
    failed += 1;
    console.log(`  got ${JSON.stringify(got)}`);
  }
}
for (const e of fixture.edges) {
  total += 1;
  const [x, y, w, h] = e.box;
  const got = nearestEdge(x, y, w, h, e.frame[0], e.frame[1]);
  const ok = got.edge === e.edge && got.travel === e.travel;
  console.log(`${ok ? "ok  " : "FAIL"} edge of ${JSON.stringify(e.box)}`);
  if (!ok) {
    failed += 1;
    console.log(`  got ${JSON.stringify(got)}, want ${e.edge} ${e.travel}`);
  }
}
console.log(failed ? `${failed} of ${total} differ` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
