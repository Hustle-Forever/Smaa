// Rebrand studio — an icon's art in the browser, and how the moving preview
// draws it.
//
// At rest an icon is drawn from the SERVER'S sprite: the PNG the plan hands
// over for each shown icon is the run's own Stamper output, so the stage
// shows the very pixels Generate stamps (drawItem, when the plan's look is
// the draft's). While a plan for a changed look is on its way (a click
// behind), the canvas draws it itself: the same Phosphor path (the bold
// weight for the circle and the chip) and the same geometry — the circle a
// disc with its glyph at 0.53 L; the chip a pill with the glyph and its words
// in the same TTF (FontFace "42 Label"); the old badge (a square with its
// top-right corner cut at 45°, legs 0.24 L) and "Just the icon" — with a
// canvas shadow standing in for the server's Gaussian one.
//
// Motion: fade, pop and slide move frame for frame as the run moves them
// (rebrandIconFx.ts, a port of the server's fx_progress checked against a
// shared fixture); the glitch's torn slices and a chip's grow (its circle
// pops, then the pill unfolds to its words: drawChipGrown) are the canvas's
// own drawing of the same numbers. Library paths arrive with the catalogue, a search, a
// match or a plan item, and are kept here by id and weight; one the studio
// hasn't seen is read from the glyph route. An icon the AI drew is a PNG
// (black on clear), tinted here.

import { iconGlyphUrl, type HarnessIconFonts, type HarnessIconGlyph, type HarnessIconLibraryItem, type HarnessIconRules, type HarnessIconStyle } from "../../../lib/rebrandHarnessApi";
import { framesOf, fxProgress, nearestEdge, slideOffset } from "../../../lib/rebrandIconFx";

export type GlyphWeight = "fill" | "bold";

const paths = new Map<string, string>();
const pathObjs = new Map<string, Path2D>();
const images = new Map<string, HTMLImageElement>();
const loading = new Map<string, Promise<void>>();
const listeners = new Set<() => void>();

/** Called when art that wasn't here arrives (a glyph, a sprite, the label
 *  font) — to redraw. */
export function onArt(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function changed() {
  listeners.forEach((fn) => fn());
}

const keyOf = (id: string, weight: GlyphWeight) => `${id}@${weight}`;

/** Keep the paths of icons the server listed (`d` fill, `b` bold). */
export function rememberPaths(items: Pick<HarnessIconLibraryItem, "id" | "d" | "b">[] | null | undefined): void {
  let hit = false;
  for (const it of items ?? []) {
    if (it?.id && it.d && !paths.has(keyOf(it.id, "fill"))) {
      paths.set(keyOf(it.id, "fill"), it.d);
      hit = true;
    }
    if (it?.id && it.b && !paths.has(keyOf(it.id, "bold"))) {
      paths.set(keyOf(it.id, "bold"), it.b);
      hit = true;
    }
  }
  if (hit) changed();
}

/** Keep one path (a plan item's, in its weight). */
export function rememberPath(id: string, weight: GlyphWeight, d: string | null | undefined): void {
  if (!id || !d || paths.has(keyOf(id, weight))) return;
  paths.set(keyOf(id, weight), d);
  changed();
}

export function pathOf(id: string, weight: GlyphWeight = "fill"): string | null {
  return paths.get(keyOf(id, weight)) ?? null;
}

function isCustom(g: HarnessIconGlyph): boolean {
  return g.source === "custom" || /^c-[0-9a-f]{12}$/.test(g.id);
}

/** Fetch what isn't here yet (once per id and weight; a drawn icon has one
 *  shape, whatever the weight). */
export function loadArt(g: HarnessIconGlyph, weight: GlyphWeight = "fill"): Promise<void> {
  const id = g.id;
  const custom = isCustom(g);
  const key = custom ? id : keyOf(id, weight);
  if (!id || (custom ? images.has(id) : paths.has(key))) return Promise.resolve();
  const known = loading.get(key);
  if (known) return known;
  const p = (async () => {
    try {
      if (custom) {
        const img = new Image();
        img.src = iconGlyphUrl(id);
        await img.decode();
        images.set(id, img);
      } else {
        const res = await fetch(iconGlyphUrl(id, weight));
        if (!res.ok) return;
        const svg = await res.text();
        const ds = [...svg.matchAll(/\sd="([^"]+)"/g)].map((m) => m[1]);
        if (ds.length) paths.set(key, ds.join(" "));
      }
      changed();
    } catch {
      /* the row shows a blank tile; the stage still shows the others */
    } finally {
      loading.delete(key);
    }
  })();
  loading.set(key, p);
  return p;
}

type Art = { kind: "path"; path: Path2D } | { kind: "image"; img: HTMLImageElement };

function artOf(g: HarnessIconGlyph, weight: GlyphWeight = "fill"): Art | null {
  if (isCustom(g)) {
    const img = images.get(g.id);
    if (!img) void loadArt(g);
    return img ? { kind: "image", img } : null;
  }
  const key = keyOf(g.id, weight);
  let p = pathObjs.get(key);
  if (!p) {
    const d = paths.get(key);
    if (!d) {
      void loadArt(g, weight);
      return null;
    }
    p = new Path2D(d);
    pathObjs.set(key, p);
  }
  return { kind: "path", path: p };
}

// --- the server's sprites ------------------------------------------------------------

const sprites = new Map<string, HTMLImageElement | "loading" | "failed">();

/** The sprite PNG at `url` once it has decoded (null until then: the
 *  canvas draws the icon itself meanwhile). */
export function spriteImage(url: string | null | undefined): HTMLImageElement | null {
  if (!url) return null;
  const got = sprites.get(url);
  if (got instanceof HTMLImageElement) return got;
  if (got) return null;
  sprites.set(url, "loading");
  const img = new Image();
  img.src = url;
  img
    .decode()
    .then(() => {
      if (sprites.size > 96) sprites.clear();
      sprites.set(url, img);
      changed();
    })
    .catch(() => sprites.set(url, "failed"));
  return null;
}

// --- the chip's words --------------------------------------------------------------

export const LABEL_FAMILY = "42 Label";
let fontKey: string | null = null;

/** Load the chip label font (the very TTFs the server shapes the words
 *  with) as "42 Label" — Latin, with the Arabic face for Arabic letters. */
export function loadLabelFont(fonts: HarnessIconFonts | null | undefined): void {
  const key = fonts?.default;
  const faces = key ? fonts?.labels?.[key] : null;
  if (!key || !faces || fontKey === key || typeof FontFace === "undefined") return;
  fontKey = key;
  const arabic = "U+0600-06FF, U+0750-077F, U+08A0-08FF, U+FB50-FDFF, U+FE70-FEFF";
  const load = [
    new FontFace(LABEL_FAMILY, `url(${faces.latin})`, { weight: "600" }),
    new FontFace(LABEL_FAMILY, `url(${faces.arabic})`, { weight: "600", unicodeRange: arabic }),
  ];
  Promise.all(
    load.map((f) =>
      f.load().then((face) => {
        document.fonts.add(face);
      }),
    ),
  )
    .then(changed)
    .catch(() => {
      /* the chip's words are drawn in the page's own font meanwhile */
    });
}

const RTL = /[֐-ࣿיִ-﷿ﹰ-ﻼ]/;

/** Words that read right to left (a chip's glyph then sits at the right). */
export function isRtl(text: string | null | undefined): boolean {
  return RTL.test(text ?? "");
}

// --- drawing an icon at rest -------------------------------------------------------

const BADGE_CUT = 0.24;
const BADGE_GLYPH = 0.6;
const GLYPH_ONLY = 0.8;
const HALO = 0.35;

/** The circle / chip geometry (the server's, from its catalogue rules). */
function geometry(rules: Partial<HarnessIconRules> | null | undefined) {
  return {
    circleGlyph: rules?.circle_glyph ?? 0.53,
    chipH: rules?.chip_h ?? 0.74,
    chipGlyph: rules?.chip_glyph ?? 0.7,
    padGlyph: rules?.chip_pad_glyph ?? 0.24,
    gap: rules?.chip_gap ?? 0.15,
    padWords: rules?.chip_pad_words ?? 0.28,
    em: rules?.chip_em ?? 0.37,
    sigma: rules?.shadow_sigma ?? 0.08,
    alpha: rules?.shadow_alpha ?? 0.4,
    dy: rules?.shadow_dy ?? 0.05,
  };
}

/** A PNG glyph in one colour, drawn once per (glyph, colour, size). */
const tinted = new Map<string, HTMLCanvasElement>();
function tint(img: HTMLImageElement, colour: string, size: number): HTMLCanvasElement {
  const key = `${img.src}|${colour}|${size}`;
  const hit = tinted.get(key);
  if (hit) return hit;
  const c = document.createElement("canvas");
  c.width = c.height = Math.max(1, Math.round(size));
  const x = c.getContext("2d");
  if (x) {
    x.drawImage(img, 0, 0, c.width, c.height);
    x.globalCompositeOperation = "source-in";
    x.fillStyle = colour;
    x.fillRect(0, 0, c.width, c.height);
  }
  if (tinted.size > 64) tinted.clear();
  tinted.set(key, c);
  return c;
}

function glyphAt(ctx: CanvasRenderingContext2D, art: Art, x: number, y: number, size: number, colour: string) {
  if (art.kind === "path") {
    ctx.save();
    ctx.translate(x, y);
    ctx.scale(size / 256, size / 256);
    ctx.fillStyle = colour;
    ctx.fill(art.path, "nonzero");
    ctx.restore();
  } else {
    ctx.drawImage(tint(art.img, colour, size), x, y, size, size);
  }
}

function withAlpha(hex: string, a: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${Math.min(1, a)})`;
}

/** The soft shadow of an icon of size L: the canvas blur is twice the
 *  Gaussian's sigma (the canvas spec's own rule). */
function shadow(ctx: CanvasRenderingContext2D, L: number, g: ReturnType<typeof geometry>) {
  ctx.shadowColor = `rgba(0,0,0,${g.alpha})`;
  ctx.shadowBlur = 2 * g.sigma * L;
  ctx.shadowOffsetX = 0;
  ctx.shadowOffsetY = g.dy * L;
}

function pill(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number) {
  const r = h / 2;
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.arc(x + w - r, y + r, r, -Math.PI / 2, Math.PI / 2);
  ctx.lineTo(x + r, y + h);
  ctx.arc(x + r, y + r, r, Math.PI / 2, (3 * Math.PI) / 2);
  ctx.closePath();
}

/** How an icon looks at rest (the canvas's own drawing). */
export interface BodyLook {
  style: HarnessIconStyle;
  fill: string;
  ink: string;
  weight: GlyphWeight;
  /** A chip's words. */
  text?: string | null;
}

/** A chip's width for its height `h` (canvas px): the server's rule with
 *  the words measured in "42 Label" (close to its HarfBuzz width). */
export function chipWidth(ctx: CanvasRenderingContext2D, text: string, h: number, rules?: Partial<HarnessIconRules> | null): number {
  const g = geometry(rules);
  ctx.save();
  ctx.font = `600 ${g.em * h}px "${LABEL_FAMILY}", Jost, sans-serif`;
  const words = ctx.measureText(text).width;
  ctx.restore();
  return Math.max(h, (g.padGlyph + g.chipGlyph + g.gap + g.padWords) * h + words);
}

/** One icon at rest in its body box (canvas px: x, y, w, h — a square's
 *  side is min(w, h), centred; a chip is the box's height tall and w wide).
 *  False while its glyph isn't here yet (its body is drawn meanwhile). */
export function drawBody(
  ctx: CanvasRenderingContext2D,
  glyph: HarnessIconGlyph,
  look: BodyLook,
  box: { x: number; y: number; w: number; h: number },
  rules?: Partial<HarnessIconRules> | null,
): boolean {
  const g = geometry(rules);
  const art = artOf(glyph, look.weight);
  if (look.style === "chip") {
    const h = box.h;
    const w = box.w;
    const L = h / g.chipH;
    ctx.save();
    shadow(ctx, L, g);
    pill(ctx, box.x, box.y, w, h);
    ctx.fillStyle = look.fill;
    ctx.fill();
    ctx.restore();
    const side = g.chipGlyph * h;
    const text = look.text ?? "";
    const rtl = RTL.test(text);
    const gx = rtl ? box.x + w - g.padGlyph * h - side : box.x + g.padGlyph * h;
    if (art) glyphAt(ctx, art, gx, box.y + (h - side) / 2, side, look.ink);
    if (text) {
      ctx.save();
      const em = g.em * h;
      ctx.font = `600 ${em}px "${LABEL_FAMILY}", Jost, sans-serif`;
      ctx.fillStyle = look.ink;
      ctx.textBaseline = "alphabetic";
      ctx.direction = rtl ? "rtl" : "ltr";
      ctx.textAlign = rtl ? "right" : "left";
      const tx = rtl ? box.x + w - (g.padGlyph * h + side + g.gap * h) : box.x + g.padGlyph * h + side + g.gap * h;
      // Jost's cap height is 0.7 em: centred on the pill, as the server does.
      ctx.fillText(text, tx, box.y + h / 2 + 0.35 * em);
      ctx.restore();
    }
    return !!art;
  }
  const L = Math.min(box.w, box.h);
  const x = box.x + (box.w - L) / 2;
  const y = box.y + (box.h - L) / 2;
  if (look.style === "circle") {
    ctx.save();
    shadow(ctx, L, g);
    ctx.beginPath();
    ctx.arc(x + L / 2, y + L / 2, L / 2, 0, 2 * Math.PI);
    ctx.fillStyle = look.fill;
    ctx.fill();
    ctx.restore();
    if (!art) return false;
    const s = g.circleGlyph * L;
    glyphAt(ctx, art, x + (L - s) / 2, y + (L - s) / 2, s, look.ink);
    return true;
  }
  if (look.style === "glyph") {
    if (!art) return false;
    const s = GLYPH_ONLY * L;
    const off = (L - s) / 2;
    ctx.save();
    ctx.shadowColor = withAlpha(look.ink, HALO * 1.6);
    ctx.shadowBlur = Math.max(2, 0.08 * L);
    glyphAt(ctx, art, x + off, y + off, s, look.fill);
    ctx.restore();
    glyphAt(ctx, art, x + off, y + off, s, look.fill);
    return true;
  }
  const cut = BADGE_CUT * L;
  ctx.beginPath();
  ctx.moveTo(x, y);
  ctx.lineTo(x + L - cut, y);
  ctx.lineTo(x + L, y + cut);
  ctx.lineTo(x + L, y + L);
  ctx.lineTo(x, y + L);
  ctx.closePath();
  ctx.fillStyle = look.fill;
  ctx.fill();
  if (!art) return false;
  const s = BADGE_GLYPH * L;
  glyphAt(ctx, art, x + (L - s) / 2, y + (L - s) / 2, s, look.ink);
  return true;
}

/** A chip on its way in or out ("grow"), in its body box (canvas px, the
 *  chip's full size): the pill `unfold` of the way from a disc of its height
 *  at its glyph's end to its full width, the glyph moving from the disc's
 *  middle to its place, the words at `words` strength and never past the
 *  pill — the server's chip_grown, drawn by the canvas. */
export function drawChipGrown(
  ctx: CanvasRenderingContext2D,
  glyph: HarnessIconGlyph,
  look: BodyLook,
  box: { x: number; y: number; w: number; h: number },
  rules: Partial<HarnessIconRules> | null | undefined,
  unfold: number,
  words: number,
): void {
  const g = geometry(rules);
  const art = artOf(glyph, look.weight);
  const { h, w } = box;
  const L = h / g.chipH;
  const text = look.text ?? "";
  const rtl = RTL.test(text);
  const width = h + (w - h) * Math.max(0, Math.min(1, unfold));
  const x0 = rtl ? box.x + w - width : box.x;
  ctx.save();
  shadow(ctx, L, g);
  pill(ctx, x0, box.y, width, h);
  ctx.fillStyle = look.fill;
  ctx.fill();
  ctx.restore();
  const side = g.chipGlyph * h;
  const full = rtl ? box.x + w - g.padGlyph * h - side : box.x + g.padGlyph * h;
  const disc = rtl ? box.x + w - h + (h - side) / 2 : box.x + (h - side) / 2;
  if (art) glyphAt(ctx, art, disc + (full - disc) * unfold, box.y + (h - side) / 2, side, look.ink);
  if (text && words > 0) {
    ctx.save();
    pill(ctx, x0, box.y, width, h);
    ctx.clip();
    const em = g.em * h;
    ctx.globalAlpha *= Math.min(1, words);
    ctx.font = `600 ${em}px "${LABEL_FAMILY}", Jost, sans-serif`;
    ctx.fillStyle = look.ink;
    ctx.textBaseline = "alphabetic";
    ctx.direction = rtl ? "rtl" : "ltr";
    ctx.textAlign = rtl ? "right" : "left";
    const tx = rtl ? box.x + w - (g.padGlyph * h + side + g.gap * h) : box.x + g.padGlyph * h + side + g.gap * h;
    ctx.fillText(text, tx, box.y + h / 2 + 0.35 * em);
    ctx.restore();
  }
}

// --- the icon in motion ---------------------------------------------------------------

/** A small deterministic hash (the glitch's slices stay put for a seed). */
function hash(s: string, n: number): number {
  let h = 2166136261 ^ n;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 10000) / 10000;
}

/** One icon to draw on the moving preview (canvas px). */
export interface ItemDraw {
  /** Scene seconds: it starts coming in, is fully in, is gone. */
  from: number;
  settle: number;
  to: number;
  enter: string;
  /** null: the entrance mirrored. */
  leave: string | null;
  seed: string;
  /** Where its body rests. */
  body: { x: number; y: number; w: number; h: number };
  /** The shadow's margin around the body (0 for the badge and bare glyph). */
  pad: number;
  /** Draws it at rest with its body's top-left at (x, y) (the server's
   *  sprite, or the canvas's own drawing). */
  draw: (x: number, y: number) => void;
  /** A chip's grow: draws it `unfold` open with its words at `words`
   *  (drawChipGrown), its body's top-left at (x, y); `rtl` puts its circle
   *  at the right. Without it a grow is drawn as a pop. */
  grow?: { draw: (x: number, y: number, unfold: number, words: number) => void; rtl: boolean };
}

/** Draws an icon as it is at time t (scene seconds): the run's own motion
 *  (fade, pop, slide exact; the glitch's slices and a chip's grow drawn by
 *  the canvas from the same numbers). */
export function drawItem(
  ctx: CanvasRenderingContext2D,
  it: ItemDraw,
  t: number,
  frame: { w: number; h: number },
  rules: HarnessIconRules,
): void {
  const fps = rules.fps;
  const fr = framesOf(t - it.from, it.to - it.from, it.settle - it.from, rules.out_f / fps, fps);
  const pr = fxProgress(it.enter, fr.f, fr.end, fr.fin, fr.fout, it.leave);
  if (!pr) return;
  const { x, y, w, h } = it.body;
  ctx.save();
  if (pr.phase === "hold" || pr.kind === "none" || (pr.kind === "glitch" && pr.amount <= 0.001 && pr.reveal >= 1)) {
    it.draw(x, y);
  } else if (pr.kind === "fade") {
    ctx.globalAlpha = pr.alpha;
    it.draw(x, y);
  } else if (pr.kind === "pop") {
    ctx.globalAlpha = pr.alpha;
    const cx = x + w / 2;
    const cy = y + h / 2;
    ctx.translate(cx, cy);
    ctx.scale(pr.scale, pr.scale);
    ctx.translate(-cx, -cy);
    it.draw(x, y);
  } else if (pr.kind === "grow") {
    const g = it.grow;
    if (pr.unfold <= 0) {
      // Its circle pops about its own middle (a chip's at its glyph's end).
      const cx = g ? (g.rtl ? x + w - h / 2 : x + h / 2) : x + w / 2;
      const cy = y + h / 2;
      ctx.globalAlpha = pr.alpha;
      ctx.translate(cx, cy);
      ctx.scale(pr.scale, pr.scale);
      ctx.translate(-cx, -cy);
      if (g) g.draw(x, y, 0, 0);
      else it.draw(x, y);
    } else if (g && (pr.unfold < 1 || pr.words < 1)) {
      g.draw(x, y, pr.unfold, pr.words);
    } else {
      it.draw(x, y);
    }
  } else if (pr.kind === "slide") {
    const { edge, travel } = nearestEdge(Math.round(x), Math.round(y), Math.round(w), Math.round(h), frame.w, frame.h);
    const { dx, dy } = slideOffset(pr.slide, travel + it.pad, edge);
    it.draw(x + dx, y + dy);
  } else {
    // The glitch: torn slices that jump every two frames and hand over one
    // by one, a strong one flickering off now and then (approximate).
    const step = Math.floor(fr.f / 2);
    if (pr.amount > 0.5 && hash(it.seed, step * 7 + 3) < 0.16 * pr.amount) {
      ctx.restore();
      return;
    }
    const L = Math.min(w, h);
    const top = y - it.pad;
    const tall = h + 2 * it.pad;
    const slices = 10;
    for (let i = 0; i < slices; i++) {
      if (hash(it.seed, 1000 + i) >= pr.reveal) continue;
      const tear = hash(it.seed, step * 13 + i) < 0.18 ? 2.6 : 1;
      const shift = (hash(it.seed, step * 31 + i) - 0.5) * 2 * 0.22 * L * pr.amount * tear;
      const y0 = top + (tall * i) / slices;
      ctx.save();
      ctx.beginPath();
      ctx.rect(x - it.pad - 3 * L, y0, w + 2 * it.pad + 6 * L, tall / slices + 0.5);
      ctx.clip();
      if (pr.amount > 0.02) {
        ctx.globalAlpha = 0.5;
        ctx.globalCompositeOperation = "lighter";
        ctx.filter = "hue-rotate(160deg)";
        it.draw(x + shift + 0.045 * L * pr.amount, y);
        ctx.filter = "none";
        ctx.globalAlpha = 1;
        ctx.globalCompositeOperation = "source-over";
      }
      it.draw(x + shift, y);
      ctx.restore();
    }
  }
  ctx.restore();
}
