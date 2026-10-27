// Rebrand studio — a pattern being moved, sized or turned by hand, on the
// stage before the server's frame (modelled on RecolourCanvas).
//
// One <canvas> in the stage's own box, fitted like the <img> it stands in
// for (object-contain): the part's frame, the other patterns' visible alphas
// in the draft's colours, and the grabbed pattern's own art — unmoved, past
// the frame's edges (POST /pattern/art) — drawn under the hand's matrix
// (lib/rebrandHand: the renderer's own), filled with its colour and cut by
// the still's speaker cut (keep_url). Canvas compositing only: it repaints
// on every move of the hand.
//
// It is a stand-in and says so (the stage labels it "Placing Structure…"):
// the other patterns are the preview's own pixels, the moved one differs
// from the engine's only by resampling. It is never kept, never sent, and
// the stage drops it for the server's exact frame once that has decoded.
// Until the art is read it reports not ready, and the stage keeps its old
// picture (dimmed) with only the handles moving — never a picture with a
// hole.
//
// pickPatternAt reads the same alphas to say which pattern a click on the
// picture landed on (the topmost with ink there).

import { useEffect, useLayoutEffect, useRef, useState } from "react";

import type { HarnessBox, HarnessPreviewChunk } from "../../../lib/rebrandHarnessApi";
import { handMatrix, type Hand } from "../../../lib/rebrandHand";

export interface HandLayer {
  /** A pattern's visible alpha, as the preview laid it in. */
  url: string;
  colour: string;
  elementId: string | null;
}

export interface HandSpec {
  /** The part's frame. */
  base: string;
  /** The preview's layers, back to front (the grabbed one's is skipped). */
  layers: HandLayer[];
  grabbed: {
    elementId: string;
    /** Its art unmoved (a white PNG) and where it lies, frame fractions. */
    artUrl: string;
    window: HarnessBox;
    colour: string;
    hand: Hand;
    /** Where the speaker leaves it seen; null: nothing cut. */
    keepUrl: string | null;
  };
}

// Decoded images, most recently used last (a frame, a few alphas, an art).
const KEEP = 16;
const images = new Map<string, Promise<HTMLImageElement | null>>();

function image(url: string): Promise<HTMLImageElement | null> {
  const hit = images.get(url);
  if (hit) {
    images.delete(url);
    images.set(url, hit);
    return hit;
  }
  const p = new Promise<HTMLImageElement | null>((resolve) => {
    const img = new Image();
    img.decoding = "async";
    img.onload = () => resolve(img);
    img.onerror = () => resolve(null);
    img.src = url;
  }).then((v) => {
    if (v === null) images.delete(url);
    return v;
  });
  images.set(url, p);
  while (images.size > KEEP) {
    const oldest = images.keys().next().value;
    if (oldest === undefined) break;
    images.delete(oldest);
  }
  return p;
}

/** Read a pattern's art (and the keep) ahead, so its first move paints at once. */
export function preloadHand(urls: (string | null | undefined)[]): void {
  for (const u of urls) if (u) void image(u);
}

/** `alpha` (a white image whose opacity is the pattern) laid on `ctx` in
 *  `colour`, through a scratch canvas of the frame's size. */
function tinted(scratch: HTMLCanvasElement, W: number, H: number, colour: string, draw: (c: CanvasRenderingContext2D) => void): HTMLCanvasElement | null {
  if (scratch.width !== W) scratch.width = W;
  if (scratch.height !== H) scratch.height = H;
  const c = scratch.getContext("2d");
  if (!c) return null;
  c.setTransform(1, 0, 0, 1, 0, 0);
  c.globalCompositeOperation = "source-over";
  c.clearRect(0, 0, W, H);
  draw(c);
  c.setTransform(1, 0, 0, 1, 0, 0);
  c.globalCompositeOperation = "source-in";
  c.fillStyle = colour;
  c.fillRect(0, 0, W, H);
  c.globalCompositeOperation = "source-over";
  return scratch;
}

export function HandCanvas({
  spec,
  onReady,
  className = "",
}: {
  spec: HandSpec;
  /** true once the canvas holds this spec's picture; false while it can't. */
  onReady: (ready: boolean) => void;
  className?: string;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  const scratch = useRef<HTMLCanvasElement | null>(null);
  const onReadyRef = useRef(onReady);
  onReadyRef.current = onReady;
  const urls = [spec.base, spec.grabbed.artUrl, spec.grabbed.keepUrl ?? "", ...spec.layers.map((l) => l.url)].join("|");
  const [loaded, setLoaded] = useState<{ urls: string; imgs: Map<string, HTMLImageElement> } | null>(null);

  useLayoutEffect(() => {
    onReadyRef.current(false);
  }, [urls]);

  useEffect(() => {
    let cancelled = false;
    const want = urls.split("|").filter(Boolean);
    void Promise.all(want.map((u) => image(u).then((img) => [u, img] as const))).then((got) => {
      if (cancelled) return;
      const imgs = new Map<string, HTMLImageElement>();
      for (const [u, img] of got) if (img) imgs.set(u, img);
      setLoaded(got.every(([, img]) => !!img) ? { urls, imgs } : null);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urls]);

  useLayoutEffect(() => () => onReadyRef.current(false), []);

  const h = spec.grabbed.hand;
  const handKey = `${h.dx}|${h.dy}|${h.scale}|${h.rotate}|${h.pivot.join(",")}`;
  const colours = [spec.grabbed.colour, ...spec.layers.map((l) => l.colour)].join("|");

  useLayoutEffect(() => {
    const c = ref.current;
    if (!c || !loaded || loaded.urls !== urls) return;
    const base = loaded.imgs.get(spec.base);
    const art = loaded.imgs.get(spec.grabbed.artUrl);
    const ctx = c.getContext("2d");
    if (!base || !art || !ctx) {
      onReadyRef.current(false);
      return;
    }
    const W = base.naturalWidth;
    const H = base.naturalHeight;
    if (c.width !== W) c.width = W;
    if (c.height !== H) c.height = H;
    scratch.current = scratch.current ?? document.createElement("canvas");
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalCompositeOperation = "source-over";
    ctx.clearRect(0, 0, W, H);
    ctx.drawImage(base, 0, 0, W, H);
    const g = spec.grabbed;
    const drawGrabbed = () => {
      const keep = g.keepUrl ? loaded.imgs.get(g.keepUrl) : null;
      const m = handMatrix(g.hand, W, H);
      const t = tinted(scratch.current!, W, H, g.colour, (s) => {
        if (m) s.setTransform(m[0][0], m[1][0], m[0][1], m[1][1], m[0][2], m[1][2]);
        s.imageSmoothingEnabled = true;
        s.drawImage(art, g.window.x * W, g.window.y * H, g.window.w * W, g.window.h * H);
      });
      if (!t) return;
      if (keep) {
        const sc = t.getContext("2d")!;
        sc.globalCompositeOperation = "destination-in";
        sc.drawImage(keep, 0, 0, W, H);
        sc.globalCompositeOperation = "source-over";
      }
      ctx.drawImage(t, 0, 0);
    };
    let drew = false;
    for (const l of spec.layers) {
      if (l.elementId === g.elementId) {
        drawGrabbed();
        drew = true;
        continue;
      }
      const alpha = loaded.imgs.get(l.url);
      if (!alpha) continue;
      const t = tinted(scratch.current, W, H, l.colour, (s) => s.drawImage(alpha, 0, 0, W, H));
      if (t) ctx.drawImage(t, 0, 0);
    }
    if (!drew) drawGrabbed();
    onReadyRef.current(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded, urls, handKey, colours, spec.grabbed.window]);

  return <canvas ref={ref} aria-hidden data-hand-standin="" className={className} />;
}

// --- which pattern a click landed on -------------------------------------------

const planes = new Map<string, Promise<{ w: number; h: number; a: Uint8Array } | null>>();

function plane(url: string): Promise<{ w: number; h: number; a: Uint8Array } | null> {
  const hit = planes.get(url);
  if (hit) return hit;
  const p = image(url).then((img) => {
    if (!img) return null;
    try {
      const c = document.createElement("canvas");
      c.width = img.naturalWidth;
      c.height = img.naturalHeight;
      const ctx = c.getContext("2d", { willReadFrequently: true });
      if (!ctx || !c.width || !c.height) return null;
      ctx.drawImage(img, 0, 0);
      const d = ctx.getImageData(0, 0, c.width, c.height).data;
      const a = new Uint8Array(c.width * c.height);
      for (let i = 0, j = 3; i < a.length; i++, j += 4) a[i] = d[j];
      return { w: c.width, h: c.height, a };
    } catch {
      return null;
    }
  });
  planes.set(url, p);
  while (planes.size > 8) {
    const oldest = planes.keys().next().value;
    if (oldest === undefined) break;
    planes.delete(oldest);
  }
  return p;
}

/** The pattern whose ink is under (x, y) (frame fractions) on a preview's
 *  part — the topmost with an alpha above 32 — or null for none. */
export async function pickPatternAt(chunk: HarnessPreviewChunk | null | undefined, x: number, y: number): Promise<string | null> {
  const layers = chunk?.layers ?? [];
  for (let i = layers.length - 1; i >= 0; i--) {
    const l = layers[i];
    if (!l.alpha_url || !l.element_id) continue;
    const p = await plane(l.alpha_url);
    if (!p) continue;
    const px = Math.min(p.w - 1, Math.max(0, Math.floor(x * p.w)));
    const py = Math.min(p.h - 1, Math.max(0, Math.floor(y * p.h)));
    // A few pixels around: a hairline is hard to hit exactly.
    for (let dy = -3; dy <= 3; dy++) {
      for (let dx = -3; dx <= 3; dx++) {
        const qx = px + dx;
        const qy = py + dy;
        if (qx >= 0 && qy >= 0 && qx < p.w && qy < p.h && p.a[qy * p.w + qx] > 32) return l.element_id;
      }
    }
  }
  return null;
}
