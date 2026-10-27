// Checks src/lib/rebrandHand.ts — a pattern moved, sized or turned by hand —
// against the shared fixture the renderer is tested with (backend/tests/
// fixtures/rebrand_hand.json): the hand's matrix and the full-screen clamp
// are the renderer's own, number for number. Then the element edits: a move
// pins the pattern and keeps the box it was grabbed at, only non-default
// fields are written, "Back to automatic" takes all of it away, and a
// gesture composes onto the hand about the same pivot.
//
//   node --experimental-strip-types scripts/check-rebrand-hand.mjs
//
// (node 22.6+; the backend test runs it too when node is there). Prints a
// line per group and exits 1 on any difference.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  clampCover,
  composeHand,
  coversFrame,
  handBox,
  handMatrix,
  handOf,
  handSig,
  isHandPlaced,
  roundHand,
  withHand,
  withoutHand,
} from "../src/lib/rebrandHand.ts";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(readFileSync(resolve(here, "../../backend/tests/fixtures/rebrand_hand.json"), "utf-8"));

// As rebrandHarnessApi.recipeKey: keys sorted (the studio compares recipes so).
const canon = (v) =>
  Array.isArray(v)
    ? `[${v.map(canon).join(",")}]`
    : v && typeof v === "object"
      ? `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canon(v[k])}`).join(",")}}`
      : JSON.stringify(v ?? null);

let failed = 0;
let total = 0;
const fail = (msg) => {
  failed += 1;
  console.log(`FAIL ${msg}`);
};

for (const c of fixture.matrix) {
  total += 1;
  const m = handMatrix(c.hand, c.size[0], c.size[1]);
  if ((m === null) !== (c.matrix === null)) fail(`matrix ${JSON.stringify(c.hand)}: null-ness`);
  else if (m) {
    for (let r = 0; r < 2; r++)
      for (let k = 0; k < 3; k++)
        if (Math.abs(m[r][k] - c.matrix[r][k]) > 1e-9) fail(`matrix ${JSON.stringify(c.hand)} [${r}][${k}]: ${m[r][k]} vs ${c.matrix[r][k]}`);
  }
}
for (const c of fixture.clamp) {
  total += 1;
  const [w, h] = c.size;
  if (coversFrame(c.hand, c.rect, w, h) !== c.covers) fail(`covers ${JSON.stringify(c)}`);
  const got = clampCover(c.hand, c.rect, w, h);
  for (const k of ["dx", "dy", "scale", "rotate"]) {
    if (Math.abs(got[k] - c.clamped[k]) > 1e-9) fail(`clamp ${JSON.stringify(c.hand)} ${JSON.stringify(c.rect)} ${k}: ${got[k]} vs ${c.clamped[k]}`);
  }
}

// The element edits.
const plain = {
  id: "pattern",
  kind: "pattern",
  enabled: true,
  z: "behind",
  exact: false,
  placement: { anchor: "auto" },
  animation: { entrance: "auto" },
  props: { slug: "structure", layout: null, colour: "auto" },
};
const drawn = { layout: { placement: "left", scale: 2, reach: 0.5, anchor: "left", rotate: 0 }, box: { x: 0, y: 0.00004, w: 0.49999, h: 1 } };
const moved = withHand(plain, { dx: 0.123456, dy: -0.05, scale: 0.8, rotate: 15.04 }, drawn);
total += 6;
if (JSON.stringify(moved.placement) !== JSON.stringify({ anchor: "auto", box: { x: 0, y: 0, w: 0.5, h: 1 }, offset: [0.1235, -0.05], rotate: 15, size: 0.8 }))
  fail(`withHand placement ${JSON.stringify(moved.placement)}`);
if (JSON.stringify(moved.props.layout) !== JSON.stringify(drawn.layout)) fail("withHand pins where it was drawn");
if (!isHandPlaced(moved) || handOf(moved).pivot.join() !== "0.25,0.5") fail(`handOf ${JSON.stringify(handOf(moved))}`);
// A second move keeps the box it was first grabbed at, and the pin.
const again = withHand(moved, { dx: 0.3, dy: 0, scale: 1, rotate: 0 }, { layout: { placement: "right" }, box: { x: 0.3, y: 0, w: 0.2, h: 1 } });
if (JSON.stringify(again.placement) !== JSON.stringify({ anchor: "auto", box: { x: 0, y: 0, w: 0.5, h: 1 }, offset: [0.3, 0] }) || again.props.layout.placement !== "left")
  fail(`withHand again ${JSON.stringify(again)}`);
// Back to automatic: exactly the element before any move.
if (JSON.stringify(withoutHand(moved)) !== JSON.stringify(plain)) fail(`withoutHand ${JSON.stringify(withoutHand(moved))}`);
// The server's own shape (every placement field dumped at its default)
// comes back byte for byte too.
const served = { ...plain, placement: { anchor: "auto", region: "frame", size: null, margin: 0.04, box: null, rotate: 0, offset: [0, 0] } };
total += 1;
const servedMoved = withHand(served, { dx: 0.2, dy: 0, scale: 1, rotate: 0 }, drawn);
if (canon(servedMoved.placement) !== canon({ anchor: "auto", region: "frame", size: null, margin: 0.04, box: { x: 0, y: 0, w: 0.5, h: 1 }, rotate: 0, offset: [0.2, 0] }))
  fail(`withHand served ${JSON.stringify(servedMoved.placement)}`);
if (canon(withoutHand(servedMoved)) !== canon(served)) fail(`withoutHand served ${JSON.stringify(withoutHand(servedMoved))}`);
if (handSig(plain) !== null || JSON.stringify(handSig(moved)) !== JSON.stringify([[0.1235, -0.05], 0.8, 15, { x: 0, y: 0, w: 0.5, h: 1 }]))
  fail("handSig");
// A gesture composes about the same pivot: the box's centre is the pivot
// plus the move; the matrix of the composed hand maps the pivot there.
total += 3;
const h0 = { ...handOf(moved) };
const h1 = roundHand(composeHand(h0, { dx: 0.05, dy: 0.1, scale: 1.25, rotate: -30 }));
if (Math.abs(h1.scale - 1) > 1e-9 || Math.abs(h1.rotate + 15) > 1e-9 || Math.abs(h1.dx - 0.1735) > 1e-9) fail(`composeHand ${JSON.stringify(h1)}`);
const m1 = handMatrix(h1, 720, 1280);
const px = h1.pivot[0] * 720;
const py = h1.pivot[1] * 1280;
const at = [m1[0][0] * px + m1[0][1] * py + m1[0][2], m1[1][0] * px + m1[1][1] * py + m1[1][2]];
if (Math.abs(at[0] - (px + h1.dx * 720)) > 1e-6 || Math.abs(at[1] - (py + h1.dy * 1280)) > 1e-6) fail(`pivot lands ${at}`);
const b = handBox({ x: 0, y: 0, w: 0.5, h: 1 }, h1);
if (Math.abs(b.x + b.w / 2 - (0.25 + h1.dx)) > 1e-9 || Math.abs(b.w - 0.5 * h1.scale) > 1e-9) fail(`handBox ${JSON.stringify(b)}`);

console.log(failed ? `${failed} of ${total} differ` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
