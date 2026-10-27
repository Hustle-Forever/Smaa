/**
 * Video Editor — font loading for text layers.
 *
 * Side-effect-imported by VideoEdit.tsx so the SAME font set loads in BOTH the
 * in-browser <Player> preview (frontend, via the Vite alias) and the headless
 * `renderMedia` export — keeping text WYSIWYG.
 *
 *  - Curated Google fonts: loaded eagerly at module top via
 *    `@remotion/google-fonts/<Name>`. Each loader registers its own
 *    delayRender, so the export waits for the faces before painting. Includes
 *    Cairo + Tajawal for Arabic text.
 *  - Brand fonts (Assets Library, asset_kind='font'): loaded on demand via
 *    `@remotion/fonts` `loadFont({family,url})`, wrapped in delayRender /
 *    continueRender. We continue-on-failure so a broken/unreachable font file
 *    never hangs an export.
 *
 * The frontend keeps a parallel `CURATED_FONTS`-shaped list (lib/fonts.ts) for
 * the Text inspector's font picker; the family strings must match the
 * `fontFamily` constants exported here.
 */
import { continueRender, delayRender } from "remotion";
import { loadFont as loadBrandFont } from "@remotion/fonts";

import { fontFamily as F_INTER, loadFont as loadInter } from "@remotion/google-fonts/Inter";
import { fontFamily as F_ROBOTO, loadFont as loadRoboto } from "@remotion/google-fonts/Roboto";
import { fontFamily as F_MONTSERRAT, loadFont as loadMontserrat } from "@remotion/google-fonts/Montserrat";
import { fontFamily as F_POPPINS, loadFont as loadPoppins } from "@remotion/google-fonts/Poppins";
import { fontFamily as F_OSWALD, loadFont as loadOswald } from "@remotion/google-fonts/Oswald";
import { fontFamily as F_BEBAS, loadFont as loadBebas } from "@remotion/google-fonts/BebasNeue";
import { fontFamily as F_PLAYFAIR, loadFont as loadPlayfair } from "@remotion/google-fonts/PlayfairDisplay";
import { fontFamily as F_ANTON, loadFont as loadAnton } from "@remotion/google-fonts/Anton";
import { fontFamily as F_CAIRO, loadFont as loadCairo } from "@remotion/google-fonts/Cairo";
import { fontFamily as F_TAJAWAL, loadFont as loadTajawal } from "@remotion/google-fonts/Tajawal";

// Kick off loading of every curated face. These are idempotent inside
// @remotion/google-fonts, so calling them at module top is safe.
loadInter();
loadRoboto();
loadMontserrat();
loadPoppins();
loadOswald();
loadBebas();
loadPlayfair();
loadAnton();
loadCairo();
loadTajawal();

export interface CuratedFont {
  /** CSS font-family string (matches what google-fonts registers). */
  family: string;
  /** Picker label. */
  label: string;
  /** Supports Arabic glyphs. */
  arabic?: boolean;
}

export const CURATED_FONTS: CuratedFont[] = [
  { family: F_INTER, label: "Inter" },
  { family: F_ROBOTO, label: "Roboto" },
  { family: F_MONTSERRAT, label: "Montserrat" },
  { family: F_POPPINS, label: "Poppins" },
  { family: F_OSWALD, label: "Oswald" },
  { family: F_BEBAS, label: "Bebas Neue" },
  { family: F_PLAYFAIR, label: "Playfair Display" },
  { family: F_ANTON, label: "Anton" },
  { family: F_CAIRO, label: "Cairo (Arabic)", arabic: true },
  { family: F_TAJAWAL, label: "Tajawal (Arabic)", arabic: true },
];

export const DEFAULT_FONT_FAMILY = F_INTER;

const _brandLoaded = new Set<string>();

/**
 * Derive the CSS font `format` from the URL. Brand fonts are served at
 * extension-less API URLs (`/api/agents/producer/assets/<id>/file`), so
 * `@remotion/fonts` can't auto-detect the format and throws — we must pass it
 * explicitly. Default to "opentype" (valid for both .otf and .ttf outlines,
 * which is what the Assets Library accepts); honor an explicit extension when
 * the URL carries one (e.g. a `/data/...woff2` export path).
 */
function brandFontFormat(url: string): "woff2" | "woff" | "truetype" | "opentype" {
  const path = url.toLowerCase().split(/[?#]/)[0];
  if (path.endsWith(".woff2")) return "woff2";
  if (path.endsWith(".woff")) return "woff";
  if (path.endsWith(".ttf")) return "truetype";
  return "opentype"; // .otf or no extension (the API file route)
}

/**
 * Load a brand font (woff2/ttf/otf served at `url`) under the given CSS family
 * name. Idempotent per family. Wrapped in delayRender so the export waits for
 * the face; continue-on-failure so a bad URL never hangs the render.
 */
export function ensureBrandFont(family: string | null | undefined, url: string | null | undefined): void {
  if (!family || !url || _brandLoaded.has(family)) return;
  _brandLoaded.add(family);
  let handle: number;
  try {
    handle = delayRender(`brand-font-${family}`);
  } catch {
    // Outside a render context (shouldn't happen) — load best-effort.
    void loadBrandFont({ family, url, weight: "400", format: brandFontFormat(url) }).catch(() => {});
    return;
  }
  loadBrandFont({ family, url, weight: "400", format: brandFontFormat(url) })
    .then(() => continueRender(handle))
    .catch((e) => {
      // eslint-disable-next-line no-console
      console.warn(`[video-edit] brand font "${family}" failed to load from ${url}`, e);
      continueRender(handle);
    });
}
