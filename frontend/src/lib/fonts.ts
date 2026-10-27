/**
 * Video Editor — curated font list for the Text picker (frontend mirror).
 *
 * These family strings MUST match the `fontFamily` constants the composition's
 * `services/remotion/src/compositions/fonts.ts` registers via
 * `@remotion/google-fonts/<Name>` — the composition (bundled into the <Player>
 * via the Vite alias) actually loads the faces; this list only drives the UI.
 * Brand fonts are fetched separately from GET /video-editor/assets/fonts.
 */
export interface CuratedFont {
  family: string;
  label: string;
  arabic?: boolean;
}

export const CURATED_FONTS: CuratedFont[] = [
  { family: "Inter", label: "Inter" },
  { family: "Roboto", label: "Roboto" },
  { family: "Montserrat", label: "Montserrat" },
  { family: "Poppins", label: "Poppins" },
  { family: "Oswald", label: "Oswald" },
  { family: "Bebas Neue", label: "Bebas Neue" },
  { family: "Playfair Display", label: "Playfair Display" },
  { family: "Anton", label: "Anton" },
  { family: "Cairo", label: "Cairo (Arabic)", arabic: true },
  { family: "Tajawal", label: "Tajawal (Arabic)", arabic: true },
];

export const DEFAULT_TEXT_FONT = "Inter";
