/**
 * Reading the brand colours the profile sends, which are not always a clean
 * "#RRGGBB": a brand guide can give "Cyan #00E5FC (buttons)", "rgb(0,229,252)",
 * "00E5FC" or just "Black". Everything here turns such a line into a colour
 * the page can paint, plus the numbers a poster maker needs (RGB, contrast).
 * Display only: nothing is written back.
 */

export type RGB = [number, number, number];

export interface BrandColour {
  /** The line exactly as the profile has it. */
  raw: string;
  /** "#RRGGBB", or null when no colour could be read from the line. */
  hex: string | null;
  rgb: RGB | null;
  /** Words around the code, e.g. "Cyan" from "Cyan #00E5FC (buttons)". */
  name: string | null;
  /** Words after the name, e.g. "buttons" from the line above. */
  note: string | null;
  /** True when only a colour WORD was found ("Deep purple" -> purple). */
  approx: boolean;
}

const HEX_RE = /#([0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{3,4})(?![0-9a-z])/i;
const BARE_HEX_RE = /(?:^|[\s(:,])([0-9a-f]{6})(?![0-9a-z])/i;
const FN_RE = /(rgba?|hsla?)\([^)]*\)/i;

function hexToRgb(h: string): RGB | null {
  let s = h.replace("#", "");
  if (s.length === 3 || s.length === 4) s = s.slice(0, 3).split("").map((c) => c + c).join("");
  if (s.length === 8) s = s.slice(0, 6);
  if (s.length !== 6) return null;
  const n = parseInt(s, 16);
  if (Number.isNaN(n)) return null;
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

export function rgbToHex([r, g, b]: RGB): string {
  return "#" + [r, g, b].map((v) => Math.round(v).toString(16).padStart(2, "0")).join("").toUpperCase();
}

/** Lets the browser read any CSS colour (names, rgb(), hsl()). */
let ctx: CanvasRenderingContext2D | null | undefined;
function cssToRgb(value: string): RGB | null {
  if (typeof document === "undefined") return null;
  if (ctx === undefined) ctx = document.createElement("canvas").getContext("2d");
  if (!ctx) return null;
  // Two different starting colours: if the value is not a colour, fillStyle
  // keeps the start, so the two reads differ.
  ctx.fillStyle = "#010203";
  ctx.fillStyle = value;
  const a = String(ctx.fillStyle);
  ctx.fillStyle = "#040506";
  ctx.fillStyle = value;
  const b = String(ctx.fillStyle);
  if (a !== b) return null;
  if (a.startsWith("#")) return hexToRgb(a);
  const m = a.match(/rgba?\(([^)]+)\)/);
  if (!m) return null;
  const [r, g, bl, al] = m[1].split(",").map((x) => parseFloat(x));
  if (al === 0) return null; // "transparent"
  return [r, g, bl];
}

function tidy(s: string): string | null {
  const t = s
    .replace(/[()[\]{}]/g, " ")
    .replace(/\s*[-–—:|,;/]+\s*$/g, "")
    .replace(/^\s*[-–—:|,;/]+\s*/g, "")
    .replace(/\s{2,}/g, " ")
    .trim();
  return t.length ? t : null;
}

/** The words before and after the code: "Cyan #00E5FC (buttons)". */
function around(text: string, code: string): { name: string | null; note: string | null } {
  const at = text.indexOf(code);
  const before = tidy(text.slice(0, at));
  const rest = text.slice(at + code.length);
  const after = tidy(rest);
  if (before && after) return { name: before, note: after };
  if (before) return { name: before, note: null };
  // "#FF5E4E - errors" / "#FF5E4E: used for alerts" is a use, not a name;
  // "#00E5FC Cyan" / "#00E5FC (Cyan)" is a name.
  if (after && /^\s*([-–—:|,;]|for\b|used\b|use\b)/i.test(rest)) return { name: null, note: after };
  return { name: after, note: null };
}

export function parseColour(raw: string): BrandColour {
  const text = String(raw ?? "").trim();
  const base: BrandColour = { raw: text, hex: null, rgb: null, name: null, note: null, approx: false };
  if (!text) return base;

  const hex = text.match(HEX_RE);
  if (hex) {
    const rgb = hexToRgb(hex[0]);
    if (rgb) return { ...base, rgb, hex: rgbToHex(rgb), ...around(text, hex[0]) };
  }
  const fn = text.match(FN_RE);
  if (fn) {
    const rgb = cssToRgb(fn[0]);
    if (rgb) return { ...base, rgb, hex: rgbToHex(rgb), ...around(text, fn[0]) };
  }
  const bare = text.match(BARE_HEX_RE);
  if (bare && /\d/.test(bare[1])) {
    const rgb = hexToRgb(bare[1]);
    if (rgb) return { ...base, rgb, hex: rgbToHex(rgb), ...around(text, bare[1]) };
  }
  // Only words. "Light grey" -> "lightgrey" is an exact CSS colour.
  const words = text.toLowerCase().replace(/[^a-z\s]/g, " ").split(/\s+/).filter(Boolean);
  const joined = words.join("");
  const exact = joined ? cssToRgb(joined) : null;
  if (exact) return { ...base, rgb: exact, hex: rgbToHex(exact), name: text };
  // Last resort, flagged as approximate: the last colour word ("deep purple").
  for (let i = words.length - 1; i >= 0; i--) {
    const rgb = cssToRgb(words[i]);
    if (rgb) return { ...base, rgb, hex: rgbToHex(rgb), name: text, approx: true };
  }
  return { ...base, name: text };
}

/* ---------- contrast (WCAG 2) ---------- */

function channel(v: number) {
  const s = v / 255;
  return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
}

export function luminance([r, g, b]: RGB): number {
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

export function contrast(a: RGB, b: RGB): number {
  const la = luminance(a);
  const lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

export const INK: RGB = [26, 26, 26];
export const PAPER: RGB = [255, 255, 255];

/** Black or white, whichever reads better on this colour. */
export function inkOn(bg: RGB): string {
  return contrast(bg, PAPER) >= contrast(bg, INK) ? "#FFFFFF" : "#1A1A1A";
}

export type Readability = "easy" | "good" | "large" | "no";

export function readability(ratio: number): Readability {
  if (ratio >= 7) return "easy";
  if (ratio >= 4.5) return "good";
  if (ratio >= 3) return "large";
  return "no";
}

export const READABILITY_WORDS: Record<Readability, string> = {
  easy: "Easy to read",
  good: "Good for text",
  large: "Big text only",
  no: "Hard to read",
};

/* ---------- a plain name from the code ---------- */

function toHsl([r, g, b]: RGB): [number, number, number] {
  const R = r / 255, G = g / 255, B = b / 255;
  const max = Math.max(R, G, B), min = Math.min(R, G, B);
  const l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h = 0;
  if (max === R) h = (G - B) / d + (G < B ? 6 : 0);
  else if (max === G) h = (B - R) / d + 2;
  else h = (R - G) / d + 4;
  return [h * 60, s, l];
}

/** "Cyan", "Dark grey", "Light purple": for staff who don't read hex. */
export function plainName(rgb: RGB): string {
  const [h, s, l] = toHsl(rgb);
  if (l <= 0.09) return "Black";
  if (l >= 0.96) return "White";
  if (s < 0.14) return l < 0.22 ? "Charcoal" : l < 0.4 ? "Dark grey" : l < 0.62 ? "Grey" : l < 0.86 ? "Light grey" : "Off-white";
  const hue =
    h < 15 ? "Red" : h < 40 ? "Orange" : h < 65 ? "Yellow" : h < 160 ? "Green" : h < 200 ? "Cyan" : h < 250 ? "Blue" : h < 290 ? "Purple" : h < 340 ? "Pink" : "Red";
  if (hue === "Red" && l > 0.58 && l <= 0.78) return "Coral";
  if (l < 0.28) return `Dark ${hue.toLowerCase()}`;
  if (l > 0.78) return `Light ${hue.toLowerCase()}`;
  return hue;
}

/** How colourful it is, 0..1 (to pick an accent among the brand colours). */
export function vividness(rgb: RGB): number {
  const [, s, l] = toHsl(rgb);
  return s * (1 - Math.abs(2 * l - 1));
}
