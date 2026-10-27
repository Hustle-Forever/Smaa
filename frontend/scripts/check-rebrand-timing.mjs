// Checks src/lib/rebrandTiming.ts against the shared fixture the server's
// timing() is tested with (backend/tests/fixtures/rebrand_timing.json), so
// the timeline the studio draws and the video the server makes cannot drift.
//
//   node --experimental-strip-types scripts/check-rebrand-timing.mjs
//
// (node 22.6+; the backend test runs it too when node is there). Prints one
// line per case and exits 1 on the first difference.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { phase, timing } from "../src/lib/rebrandTiming.ts";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(
  readFileSync(resolve(here, "../../backend/tests/fixtures/rebrand_timing.json"), "utf-8"),
);
const EPS = 1e-9;
const KEYS = ["t_in", "t_full", "t_hold_end", "t_out"];

let failed = 0;
for (const c of fixture.cases) {
  const got = timing({ animation: { entrance: "auto", ...c.animation } }, { duration: c.duration, entrance: c.entrance });
  const want = c.expect;
  let ok = true;
  if (want === null || got === null) {
    ok = want === got;
  } else {
    ok =
      KEYS.every((k) => Math.abs(got[k] - want[k]) < 1e-4) &&
      got.entrance === want.entrance &&
      got.exit === want.exit &&
      got.runs_to_end === want.runs_to_end;
    for (const [t, name, q] of c.phases ?? []) {
      const [pn, pq] = phase(got, t);
      if (pn !== name || Math.abs(pq - q) > 1e-6 + EPS) {
        ok = false;
        console.log(`  at ${t}: got ${pn} ${pq}, want ${name} ${q}`);
      }
    }
  }
  console.log(`${ok ? "ok  " : "FAIL"} ${c.name}`);
  if (!ok) {
    failed += 1;
    console.log(`  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`);
  }
}
console.log(failed ? `${failed} of ${fixture.cases.length} differ` : `all ${fixture.cases.length} cases agree`);
process.exit(failed ? 1 : 0);
