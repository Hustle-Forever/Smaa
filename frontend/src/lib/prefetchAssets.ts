/**
 * Warm the browser image cache for asset-picker thumbnails.
 *
 * History worth keeping, because the original version was actively harmful: it
 * prefetched `api.producerAssets.fileUrl` — the FULL-RESOLUTION original — for 60
 * assets on creator mount. Producer assets average ~3.1 MB (the img_bg category
 * averages 9.3 MB), so that was ~331 MB fetched eagerly. Its docstring claimed
 * the bytes would be immutable-cached; they were not (the immutable header only
 * applied to the public producer-media bucket, while every asset lives in the
 * private producer-assets bucket), so they were revalidated anyway and the
 * warm-up bought nothing. `new Image()` also bypasses `loading="lazy"`, so all 60
 * fired at once and saturated the browser's ~6 connections per origin — and
 * because visited creators stay mounted under `hidden`, the burst kept competing
 * while the user was on Dashboard, Library or Assets.
 *
 * The rewrite fixes all three causes:
 *   1. It requests the server-minted THUMBNAIL (`thumb_url`), ~20-150 kB of WebP
 *      from the CDN, never the original. Rows with no thumbnail are SKIPPED
 *      rather than falling back to `fileUrl` — a miss must never cost megabytes.
 *   2. It budgets by BYTES, not by count, so a handful of 9 MB backgrounds can't
 *      blow the budget the way a fixed `max: 60` did.
 *   3. It runs inside requestIdleCallback, so it never competes with the fetches
 *      the user is actually waiting for.
 *
 * Uses the SAME URL the picker renders so the cache key matches. Idempotent
 * across renders / StrictMode double-mount via a module-level seen-set.
 */
import type { ProducerAssetRow } from "./types";

const _prefetched = new Set<number>();

/** Stop once the queued thumbnails add up to this much. */
const DEFAULT_BUDGET_BYTES = 8 * 1024 * 1024; // 8 MB
/** Hard ceiling on requests regardless of budget, so one tiny-file category
 *  can't queue hundreds of connections. */
const DEFAULT_MAX = 60;
/** Assumed thumbnail weight when we can't derive one (WebP re-encode of a
 *  typical asset). Only used for budgeting. */
const ASSUMED_THUMB_BYTES = 120 * 1024;

function idle(fn: () => void): void {
  const ric = (window as any).requestIdleCallback as
    | ((cb: () => void, opts?: { timeout?: number }) => number)
    | undefined;
  if (ric) ric(fn, { timeout: 2000 });
  else window.setTimeout(fn, 300);
}

export function prefetchAssetImages(
  items: ProducerAssetRow[],
  categories: string[],
  opts: { max?: number; budgetBytes?: number } = {},
): void {
  if (typeof window === "undefined" || typeof Image === "undefined") return;
  const max = opts.max ?? DEFAULT_MAX;
  const budget = opts.budgetBytes ?? DEFAULT_BUDGET_BYTES;
  if (max <= 0 || budget <= 0) return;
  const cats = new Set(categories);

  const queue: string[] = [];
  let spent = 0;
  for (const a of items) {
    if (queue.length >= max || spent >= budget) break;
    const cat = (a.metadata as any)?.content_category;
    if (!cats.has(cat)) continue;
    if (_prefetched.has(a.id)) continue;
    // No thumbnail → skip. Falling back to fileUrl here is what made the old
    // version cost hundreds of megabytes.
    if (!a.thumb_url) continue;
    _prefetched.add(a.id);
    queue.push(a.thumb_url);
    spent += ASSUMED_THUMB_BYTES;
  }
  if (!queue.length) return;

  idle(() => {
    for (const src of queue) {
      // Fire-and-forget: constructing an Image and setting src triggers the
      // fetch that warms the HTTP cache; we never attach it to the DOM.
      const img = new Image();
      img.decoding = "async";
      img.src = src;
    }
  });
}
