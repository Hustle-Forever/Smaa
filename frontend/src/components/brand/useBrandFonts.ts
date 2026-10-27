import { useEffect, useState } from "react";

/**
 * Shows the brand's typefaces in the real font when we can get it.
 *
 * Free fonts come from Google Fonts (the same place the app's own Jost comes
 * from). A font that isn't free online (Futura PT, Helvetica Neue …) can't be
 * downloaded, so the page says a stand-in is shown instead of pretending.
 */
export type FontState = "loading" | "real" | "standin";

const SAMPLE = "mmmmmmmmmmlli1WQ@#Aa";
let measureCtx: CanvasRenderingContext2D | null | undefined;

/** True when the browser has this font (downloaded or on the computer). */
function hasFont(family: string): boolean {
  if (typeof document === "undefined") return false;
  if (measureCtx === undefined) measureCtx = document.createElement("canvas").getContext("2d");
  const c = measureCtx;
  if (!c) return false;
  for (const base of ["monospace", "serif", "sans-serif"]) {
    c.font = `72px ${base}`;
    const plain = c.measureText(SAMPLE).width;
    c.font = `72px "${family}", ${base}`;
    if (c.measureText(SAMPLE).width !== plain) return true;
  }
  return false;
}

function addStylesheet(href: string): Promise<void> {
  return new Promise((resolve) => {
    if (document.querySelector(`link[data-brand-font="${href}"]`)) return resolve();
    const l = document.createElement("link");
    l.rel = "stylesheet";
    l.href = href;
    l.dataset.brandFont = href;
    const done = () => resolve();
    l.onload = done;
    l.onerror = done; // not on Google Fonts: Google answers 400
    window.setTimeout(done, 5000);
    document.head.appendChild(l);
  });
}

/** Brand-guide names that Google Fonts lists under another name. */
const GOOGLE_NAME: Record<string, string> = {
  "ibm plex arabic": "IBM Plex Sans Arabic",
};

/** What stands in for a font that isn't on Google Fonts: the same stand-ins
 *  the app's own styles use (Jost for Futura, Pixelify Sans for VG5000). */
const STAND_IN: Record<string, string> = {
  "futura pt": "Jost",
  futura: "Jost",
  vg5000: "Pixelify Sans",
};

function webName(family: string): string {
  return GOOGLE_NAME[family.trim().toLowerCase()] ?? family.trim();
}

export function standInFor(family: string): string {
  return STAND_IN[family.trim().toLowerCase()] ?? "Jost";
}

const pending = new Map<string, Promise<boolean>>();

function loadFamily(family: string): Promise<boolean> {
  const name = webName(family);
  const known = pending.get(name);
  if (known) return known;
  const job = (async () => {
    if (hasFont(name)) return true;
    // Only plain family names go into the URL.
    if (!/^[A-Za-z0-9 ]{2,60}$/.test(name)) return false;
    const fam = name.replace(/\s+/g, "+");
    // Regular and Bold separately: asking Google for a weight a family lacks
    // fails the whole request.
    await Promise.all([
      addStylesheet(`https://fonts.googleapis.com/css2?family=${fam}&display=swap`),
      addStylesheet(`https://fonts.googleapis.com/css2?family=${fam}:wght@700&display=swap`),
    ]);
    try {
      await Promise.race([
        Promise.all([document.fonts.load(`400 32px "${name}"`), document.fonts.load(`700 32px "${name}"`)]),
        new Promise((r) => window.setTimeout(r, 4000)),
      ]);
    } catch {
      /* ignore: measured below */
    }
    return hasFont(name);
  })();
  pending.set(name, job);
  return job;
}

export function useBrandFonts(fonts: string[] | undefined): Record<string, FontState> {
  const key = (fonts ?? []).join("|");
  const [state, setState] = useState<Record<string, FontState>>({});
  useEffect(() => {
    let alive = true;
    const list = key ? key.split("|") : [];
    setState(Object.fromEntries(list.map((f) => [f, "loading" as FontState])));
    list.forEach((f) => {
      loadFamily(f).then((ok) => {
        if (alive) setState((s) => ({ ...s, [f]: ok ? "real" : "standin" }));
      });
    });
    return () => {
      alive = false;
    };
  }, [key]);
  return state;
}

/** CSS font stack: the brand font first (under its Google Fonts name too),
 *  then its stand-in, then Jost (the 42 font). */
export function fontStack(family?: string | null): string {
  if (!family) return `"Jost", system-ui, sans-serif`;
  const names = Array.from(new Set([family.trim(), webName(family), standInFor(family), "Jost"]));
  return `${names.map((n) => `"${n}"`).join(", ")}, system-ui, sans-serif`;
}
