// Rebrand studio — a colour change on the stage before the server's frame.
//
// One <canvas> in the stage's own box, fitted like the <img> it stands in
// for (object-contain): the part's frame with each layer's alpha laid in, in
// the draft's colours — the server's own sums (rebrandRecolour), so it is
// the frame the server will send, to the pixel. It repaints the moment a
// colour changes (a few ms), with no round trip. The stage shows it only
// once it has painted (onReady), in place of the picture, never beside it,
// and drops it for the server's exact frame once that has decoded.
//
// A frame or an alpha that can't be read paints nothing and reports not
// ready, so the stage keeps its usual dim-and-label: never a block of colour
// (a failed CSS mask-image draws an unmasked rectangle, which is why this is
// a canvas).

import { useEffect, useLayoutEffect, useRef, useState } from "react";

import type { HarnessPreviewChunk } from "../../../lib/rebrandHarnessApi";
import { hexRgb, recolour, type RecolourSpec } from "../../../lib/rebrandRecolour";

interface Frame {
  width: number;
  height: number;
  px: Uint8ClampedArray;
}

// Read pixels, most recently used last: a frame is ~3.7 MB at 720x1280, an
// alpha's one byte a pixel ~0.9 MB.
const KEEP = 12;
const pixels = new Map<string, Promise<unknown>>();

function remember<T>(key: string, make: () => Promise<T | null>): Promise<T | null> {
  const hit = pixels.get(key) as Promise<T | null> | undefined;
  if (hit) {
    pixels.delete(key);
    pixels.set(key, hit);
    return hit;
  }
  const p = make().then((v) => {
    if (v === null) pixels.delete(key); // a failure is tried again next time
    return v;
  });
  pixels.set(key, p);
  while (pixels.size > KEEP) {
    const oldest = pixels.keys().next().value;
    if (oldest === undefined) break;
    pixels.delete(oldest);
  }
  return p;
}

function read(url: string): Promise<ImageData | null> {
  return new Promise((resolve) => {
    const img = new Image();
    img.decoding = "async";
    img.onload = () => {
      try {
        const c = document.createElement("canvas");
        c.width = img.naturalWidth;
        c.height = img.naturalHeight;
        const ctx = c.getContext("2d", { willReadFrequently: true });
        if (!ctx || !c.width || !c.height) return resolve(null);
        ctx.drawImage(img, 0, 0);
        resolve(ctx.getImageData(0, 0, c.width, c.height));
      } catch {
        resolve(null); // another origin: the canvas can't be read
      }
    };
    img.onerror = () => resolve(null);
    img.src = url;
  });
}

function framePixels(url: string): Promise<Frame | null> {
  return remember(`frame:${url}`, () =>
    read(url).then((d) => (d ? { width: d.width, height: d.height, px: d.data } : null)),
  );
}

/** The alpha's opacity, one byte a pixel (its A channel). */
function alphaPlane(url: string): Promise<Uint8Array | null> {
  return remember(`alpha:${url}`, () =>
    read(url).then((d) => {
      if (!d) return null;
      const plane = new Uint8Array(d.width * d.height);
      for (let p = 0, i = 3; p < plane.length; p++, i += 4) plane[p] = d.data[i];
      return plane;
    }),
  );
}

/** Read a preview part's frame and alphas ahead, so the first colour change
 *  on it paints at once. */
export function preloadRecolour(chunk: HarnessPreviewChunk | null | undefined): void {
  if (!chunk?.layers) return;
  void framePixels(chunk.frame_url);
  for (const l of chunk.layers) if (l.alpha_url) void alphaPlane(l.alpha_url);
}

export function RecolourCanvas({
  spec,
  onReady,
  className = "",
}: {
  spec: RecolourSpec;
  /** true once the canvas holds this spec's picture; false while it can't. */
  onReady: (ready: boolean) => void;
  className?: string;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  const onReadyRef = useRef(onReady);
  onReadyRef.current = onReady;
  const urls = [spec.base, ...spec.layers.map((l) => l.url)].join("|");
  const colours = spec.layers.map((l) => l.colour).join("|");
  const [loaded, setLoaded] = useState<{ urls: string; frame: Frame; planes: Uint8Array[] } | null>(null);

  // Other pictures (another part): not ready until they have painted — said
  // before this render reaches the screen, so the stage never shows the
  // canvas still holding the last part's picture, not even for a frame.
  useLayoutEffect(() => {
    onReadyRef.current(false);
  }, [urls]);

  useEffect(() => {
    let cancelled = false;
    void Promise.all([framePixels(spec.base), ...spec.layers.map((l) => alphaPlane(l.url))]).then(
      ([frame, ...planes]) => {
        if (cancelled) return;
        const fits =
          !!frame && planes.every((p): p is Uint8Array => !!p && p.length === frame.width * frame.height);
        setLoaded(fits ? { urls, frame: frame!, planes: planes as Uint8Array[] } : null);
      },
    );
    return () => {
      cancelled = true;
    };
    // The spec is read through `urls`: the same addresses are the same pictures.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urls]);

  useLayoutEffect(() => () => onReadyRef.current(false), []);

  useLayoutEffect(() => {
    const c = ref.current;
    if (!c || !loaded || loaded.urls !== urls) return;
    const rgbs = spec.layers.map((l) => hexRgb(l.colour));
    const ctx = c.getContext("2d");
    if (!ctx || rgbs.some((x) => !x)) {
      onReadyRef.current(false);
      return;
    }
    const { width, height, px } = loaded.frame;
    const out = recolour(
      px,
      loaded.planes.map((plane, i) => ({ plane, rgb: rgbs[i]! })),
    );
    if (c.width !== width) c.width = width;
    if (c.height !== height) c.height = height;
    ctx.putImageData(new ImageData(out, width, height), 0, 0);
    onReadyRef.current(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded, urls, colours]);

  return <canvas ref={ref} aria-hidden data-recolour="" className={className} />;
}
