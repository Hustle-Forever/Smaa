/**
 * Fonts for the 42 overlay, loaded on demand (not at module top) so the other
 * compositions in this bundle never wait on, or fail because of, a brand font.
 *
 * Both faces ship in public/brand42/fonts/ and load through @remotion/fonts,
 * which holds the render with its own delayRender and FAILS it if a file
 * cannot load (a silent fallback face would change every text measurement):
 *  - VG5000 Regular (SIL OFL), the headings;
 *  - Jost 400 (SIL OFL, stand-in for Futura PT Book), the sub-line. It used to
 *    come from @remotion/google-fonts, which made a deterministic, offline arm
 *    depend on fonts.googleapis.com at render time.
 */
import { loadFont } from "@remotion/fonts";
import { staticFile } from "remotion";

import { ASSETS, JOST_FAMILY, VG5000_FAMILY } from "./tokens";

export { JOST_FAMILY };

let loading: Promise<void> | null = null;

export function loadBrand42Fonts(): Promise<void> {
  if (!loading) {
    loading = Promise.all([
      loadFont({ family: VG5000_FAMILY, url: staticFile(ASSETS.vg5000), weight: "400", format: "woff2" }),
      loadFont({ family: JOST_FAMILY, url: staticFile(ASSETS.jost), weight: "400", format: "woff2" }),
    ]).then(() => undefined);
  }
  return loading;
}
