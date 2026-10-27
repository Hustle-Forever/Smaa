/**
 * Which 42 brand theme the studio uses: "dark" (charcoal, like 42abudhabi.ae)
 * or "light" (light content inside a charcoal frame).
 *
 * Stamped on <html data-k="…">; styles.css maps it to the --k-* colours.
 * Pick it with ?theme=dark or ?theme=light in the address bar; the choice is
 * remembered in this browser. Default: dark.
 */
export type StudioTheme = "dark" | "light";

const KEY = "studio.theme";

export function initTheme(): StudioTheme {
  let theme: StudioTheme = "dark";
  try {
    const fromUrl = new URLSearchParams(window.location.search).get("theme");
    if (fromUrl === "dark" || fromUrl === "light") localStorage.setItem(KEY, fromUrl);
    const saved = localStorage.getItem(KEY);
    if (saved === "dark" || saved === "light") theme = saved;
  } catch {
    /* storage blocked: keep the default */
  }
  document.documentElement.dataset.k = theme;
  return theme;
}

/** Switch theme now and remember it in this browser. */
export function setTheme(theme: StudioTheme): void {
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    /* storage blocked: still switch for this visit */
  }
  document.documentElement.dataset.k = theme;
}

export function currentTheme(): StudioTheme {
  return document.documentElement.dataset.k === "light" ? "light" : "dark";
}
