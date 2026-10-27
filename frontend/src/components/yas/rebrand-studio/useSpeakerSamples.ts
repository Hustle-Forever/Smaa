// Rebrand studio — the icon plan's speaker samples, read once per track: the
// grey strip of mattes (sample k in rows [k·h, (k+1)·h)) as numbers, with
// the head boxes, the bars and the samples' timing the plan sends beside it
// (studio._track_masks). What an icon dragged by hand is outlined by
// (lib/rebrandCover.ts: the planner's own rule). Null until it is read, or
// when the plan has no heads to judge by (an older server, no speaker found).

import { useEffect, useState } from "react";

import type { SpeakerSamples } from "../../../lib/rebrandCover";
import type { HarnessIconMasks } from "../../../lib/rebrandHarnessApi";

export function useSpeakerSamples(masks: HarnessIconMasks | null | undefined): SpeakerSamples | null {
  const [got, setGot] = useState<{ key: string; samples: SpeakerSamples } | null>(null);
  const usable = !!masks && Array.isArray(masks.heads) && !!masks.frame && !!masks.sample_frames && !!masks.fps;
  const key = usable ? `${masks!.url}|${masks!.times.length}` : "";

  useEffect(() => {
    if (!usable || !masks) return;
    let dead = false;
    const img = new Image();
    img.onload = () => {
      if (dead) return;
      try {
        const n = Math.min(masks.times.length, Math.floor(img.naturalHeight / masks.height));
        const c = document.createElement("canvas");
        c.width = masks.width;
        c.height = masks.height * n;
        const ctx = c.getContext("2d", { willReadFrequently: true });
        if (!ctx) return;
        ctx.drawImage(img, 0, 0);
        const data = ctx.getImageData(0, 0, c.width, c.height).data;
        const size = masks.width * masks.height;
        const rows: Uint8Array[] = [];
        for (let k = 0; k < n; k++) {
          const m = new Uint8Array(size);
          for (let i = 0; i < size; i++) m[i] = data[(k * size + i) * 4];
          rows.push(m);
        }
        setGot({
          key,
          samples: {
            times: masks.times.slice(0, n),
            masks: rows,
            w: masks.width,
            h: masks.height,
            frame: [masks.frame![0], masks.frame![1]],
            sampleFrames: masks.sample_frames!,
            fps: masks.fps!,
            cuts: masks.cuts ?? [],
            heads: (masks.heads ?? []).slice(0, n),
            bars: masks.bars ?? [],
          },
        });
      } catch {
        // A strip that can't be read: the handles' outline is plain.
      }
    };
    img.src = masks.url;
    return () => {
      dead = true;
    };
    // `key` holds the strip and its length.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return got && got.key === key ? got.samples : null;
}
