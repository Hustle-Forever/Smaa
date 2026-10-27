// Rebrand studio — the art of the pattern whose row is open, unmoved and
// uncut past the frame's edges (POST /pattern/art): what the stage's
// stand-in draws under the hand while it moves, sizes or turns the pattern.
//
// Asked once per art key (a pattern's art is the same while its place is:
// a colour, a move or a turn keep it), when the row opens on a draft made
// in house; read ahead (preloadHand) so the first move paints at once. A
// failure is only a stand-in less: the handles still move, the exact frame
// still lands.

import { useEffect, useState } from "react";

import {
  harnessApi,
  viewKey,
  type HarnessBaseRef,
  type HarnessPatternArt,
  type HarnessRecipe,
  type SceneView,
} from "../../../lib/rebrandHarnessApi";
import { preloadHand } from "./HandCanvas";

const known = new Map<string, HarnessPatternArt>();

export function usePatternArt({
  runId,
  target,
  draft,
  base,
  elementId,
  artKey,
  keepUrl,
  enabled,
  view = null,
}: {
  runId: number;
  target: string;
  draft: HarnessRecipe | null;
  base: HarnessBaseRef | null;
  /** The scene's end set by hand (sent, and keyed, only when set). */
  view?: SceneView | null;
  elementId: string | null;
  /** The preview's art key for it (null: not drawn yet). */
  artKey: string | null;
  /** The still's speaker cut, read ahead with it. */
  keepUrl: string | null;
  enabled: boolean;
}): HarnessPatternArt | null {
  const key = artKey ? `${runId}:${target}:${artKey}${viewKey(view)}` : null;
  const [got, setGot] = useState<HarnessPatternArt | null>(() => (key ? known.get(key) ?? null : null));

  useEffect(() => {
    if (!key) {
      setGot(null);
      return;
    }
    const hit = known.get(key);
    if (hit) {
      setGot(hit);
      preloadHand([hit.art_url, keepUrl]);
      return;
    }
    setGot(null);
    if (!enabled || !draft || !elementId) return;
    const ctl = new AbortController();
    const t = window.setTimeout(() => {
      harnessApi
        .patternArt(runId, target, draft, elementId, { signal: ctl.signal, base, view })
        .then((art) => {
          known.set(key, art);
          while (known.size > 24) {
            const oldest = known.keys().next().value;
            if (oldest === undefined) break;
            known.delete(oldest);
          }
          preloadHand([art.art_url, keepUrl]);
          setGot(art);
        })
        .catch(() => undefined);
    }, 150);
    return () => {
      window.clearTimeout(t);
      ctl.abort();
    };
    // The draft is read when asked: the same key is the same art.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled, elementId, keepUrl]);

  return got && key && known.get(key) === got ? got : null;
}
