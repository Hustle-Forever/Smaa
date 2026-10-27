// Checks src/lib/rebrandPlate.ts — the moving preview's quick stand-in for a
// draft made in house — against the shared fixture the server's plate is
// tested with (backend/tests/fixtures/rebrand_plate.json): the easing of a
// pattern's entrance and exit, and a slide's offset, are the engine's own.
// (Its wipes and its glitch are approximations, said on the stage.)
//
//   node --experimental-strip-types scripts/check-rebrand-plate.mjs
//
// (node 22.6+; the backend test runs it too when node is there). Prints a
// line per group and exits 1 on the first difference.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { layerAt, shownShare, slideOffset } from "../src/lib/rebrandPlate.ts";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(readFileSync(resolve(here, "../../backend/tests/fixtures/rebrand_plate.json"), "utf-8"));
const EPS = 1e-9;

let failed = 0;
for (const c of fixture.shown_share) {
  const got = shownShare(c.direction, c.q);
  if (Math.abs(got - c.expect) > EPS) {
    failed += 1;
    console.log(`FAIL shown_share ${c.direction} ${c.q}: got ${got}, want ${c.expect}`);
  }
}
for (const c of fixture.slide_offset) {
  const [dx, dy] = slideOffset(c.edge, c.box, c.margin, c.size, c.hidden);
  if (Math.abs(dx - c.expect[0]) > 1e-6 || Math.abs(dy - c.expect[1]) > 1e-6) {
    failed += 1;
    console.log(`FAIL slide_offset ${JSON.stringify(c)}: got ${[dx, dy]}`);
  }
}
// A slide at the same moment through layerAt: the offset of its entrance.
const tm = { t_in: 0, t_full: 1, t_hold_end: 8, t_out: 8, entrance: "slide", exit: "none", runs_to_end: true };
const layer = { timing: tm, ink_box: [0, 100, 240, 900], margin: [0, 0], edge: "left", seed: "s" };
const at = layerAt(layer, 0.5, 15, [720, 1280]);
const want = slideOffset("left", layer.ink_box, layer.margin, [720, 1280], 1 - shownShare("in", 0.5));
if (!at || Math.abs(at.dx - want[0]) > 1e-9 || at.dy !== want[1]) {
  failed += 1;
  console.log(`FAIL layerAt slide: got ${JSON.stringify(at)}, want ${want}`);
}
if (layerAt(layer, 8.5, 255, [720, 1280]) !== null || layerAt({ ...layer, timing: { ...tm, t_in: 1 } }, 0.5, 15, [720, 1280]) !== null) {
  failed += 1;
  console.log("FAIL layerAt: a layer shows outside its span");
}
const total = fixture.shown_share.length + fixture.slide_offset.length + 2;
console.log(failed ? `${failed} of ${total} differ` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
