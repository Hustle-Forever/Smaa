// Rebrand studio — the free preview, as a state machine the stage can trust.
//
//   shown     the preview whose pictures are DECODED and on screen, with the
//             recipe it was made from. It is "current" only while its key is
//             the draft's key: a reply for an older draft is kept (and cached)
//             but can never pass as the answer to the latest edit.
//   status    idle      shown is current
//             waiting   an edit is waiting out the 400 ms debounce
//             loading   the request (or the pictures' decode) is under way
//             error     the latest edit failed; 422 = the server refuses the
//                       recipe (fix it, retrying won't help), anything else
//                       (5xx, network, 90 s timeout — longer for the first
//                       preview while /warmup says the scene is still
//                       warming) offers Try again
//   change    what the pending edit changes about `shown` (describeChange),
//             so the stage can say what it is doing
//   pulse     true for 1.5 s after a commit that changed the pictures
//   warmup    what the FIRST preview of the scene is waiting on, polled from
//             the server every second once it has taken over 1.5 s
//
// "waiting" starts on the very render the edit lands in, not after the
// debounce: the stage dims on the click. A commit happens only once every
// part's picture has decoded, so the veil lifts on the new frame, never on a
// half-loaded one. Going back to a recipe seen this session is instant (an
// LRU of 40 previews) — except to one the server did not keep (kept: false,
// e.g. drawn without the speaker's mask), which is asked for again. The
// first preview and Try again skip the debounce.
//
// Built on a version (`base`), the same recipe is another picture (added on
// top of v2 it is drawn on v2's own frames): the base is part of the key —
// and so is the scene's end set by hand (`view`), only when there is one.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { rebrandErrorDetail } from "../../../lib/rebrandApi";
import {
  harnessApi,
  HarnessHttpError,
  isWarmup,
  recipeKey,
  viewKey,
  type HarnessBaseRef,
  type HarnessPreview,
  type HarnessRecipe,
  type HarnessWarmup,
  type SceneView,
} from "../../../lib/rebrandHarnessApi";
import { describeChange, type ChangeInfo } from "../../../lib/rebrandRecipe";
import { decodeAll } from "./imageDecode";
import { COPY } from "./studioCopy";

const DEBOUNCE_MS = 400;
const TIMEOUT_MS = 90_000;
/** The first preview of a cold scene waits on its one-off warm-up (the
 *  intro's colours alone took 60-100 s on a busy machine): it keeps waiting
 *  while /warmup says that is still going, up to this. */
const FIRST_TIMEOUT_MS = 300_000;
const FIRST_RECHECK_MS = 15_000;
const CACHE_SIZE = 40;
const PULSE_MS = 1500;
const WARMUP_AFTER_MS = 1500;
const WARMUP_EVERY_MS = 1000;

export interface ShownPreview {
  key: string;
  recipe: HarnessRecipe;
  /** The version it was drawn on (null: a fresh draft). */
  base: HarnessBaseRef | null;
  preview: HarnessPreview;
}

export type PreviewStatus = "idle" | "waiting" | "loading" | "error";

export interface PreviewError {
  key: string;
  message: string;
  /** Worth pressing Try again (not a recipe the server refuses). */
  retryable: boolean;
  /** The server's status, when it answered (404/409/410: the version built
   *  on can't be used right now). */
  status?: number;
}

export interface StudioPreviewState {
  shown: ShownPreview | null;
  current: boolean;
  status: PreviewStatus;
  /** When the pending edit (or burst of edits) started. */
  since: number | null;
  change: ChangeInfo | null;
  error: PreviewError | null;
  pulse: boolean;
  warmup: HarnessWarmup | null;
  retry: () => void;
}

/** Every picture a preview puts on the stage, so the commit waits for all. */
function picturesOf(p: HarnessPreview): string[] {
  return p.chunks.map((c) => c.target_url ?? c.frame_url);
}

function classify(key: string, e: unknown, timedOut: boolean, onBase: boolean): PreviewError {
  if (timedOut) return { key, message: COPY.tooLong, retryable: true };
  if (e instanceof HarnessHttpError && (e.status === 422 || e.status === 400)) {
    const listed = e.errors.map((x) => x.message).filter(Boolean);
    return { key, message: listed.length ? listed.join("; ") : e.detail, retryable: false, status: e.status };
  }
  // The version it is built on is on its way, gone, or its video is: the
  // server's own words, and no Try again (the way out is Change it or leave).
  if (e instanceof HarnessHttpError && (e.status === 409 || e.status === 410 || (onBase && e.status === 404))) {
    return { key, message: e.detail, retryable: false, status: e.status };
  }
  // A crash answers with its bare status text ("Internal Server Error"), a
  // dropped connection with the browser's ("Failed to fetch"): say it plainly.
  if (e instanceof HarnessHttpError) {
    const own = e.detail && !/^(internal server error|bad gateway|service unavailable|gateway timeout)$/i.test(e.detail);
    return { key, message: own ? e.detail : COPY.serverError, retryable: true, status: e.status };
  }
  if (e instanceof TypeError) return { key, message: COPY.offline, retryable: true };
  return { key, message: rebrandErrorDetail(e), retryable: true };
}

export function useStudioPreview({
  runId,
  target,
  draft,
  base = null,
  enabled,
  warming,
  view = null,
}: {
  runId: number;
  target: string;
  draft: HarnessRecipe | null;
  /** The version the draft is built on (null: a fresh draft). */
  base?: HarnessBaseRef | null;
  /** The scene's end set by hand (sent, and keyed, only when set). */
  view?: SceneView | null;
  /** The scene is prepared (previews are meaningless before). */
  enabled: boolean;
  /** /prepare's own word on the warm-up, before the first poll. */
  warming?: HarnessWarmup | null;
}): StudioPreviewState {
  const draftKey = draft ? recipeKey(draft) + (base ? `@${base.mode}:${base.version_id}` : "") + viewKey(view) : "";
  const [shown, setShown] = useState<ShownPreview | null>(null);
  const shownRef = useRef(shown);
  shownRef.current = shown;
  const [error, setError] = useState<PreviewError | null>(null);
  const [inflight, setInflight] = useState<string | null>(null);
  const [since, setSince] = useState<number | null>(null);
  const [pulse, setPulse] = useState(false);
  const pulseTimer = useRef<number | undefined>();
  const [nonce, setNonce] = useState(0);
  const seenNonce = useRef(0);
  const cache = useRef(new Map<string, HarnessPreview>());
  const [warmup, setWarmup] = useState<HarnessWarmup | null>(warming ?? null);
  const warmupRef = useRef(warmup);
  warmupRef.current = warmup;

  useEffect(() => {
    if (warming) setWarmup(warming);
  }, [warming]);

  useEffect(() => () => window.clearTimeout(pulseTimer.current), []);

  const remember = (key: string, p: HarnessPreview) => {
    const c = cache.current;
    c.delete(key);
    c.set(key, p);
    while (c.size > CACHE_SIZE) {
      const oldest = c.keys().next().value;
      if (oldest === undefined) break;
      c.delete(oldest);
    }
  };

  useEffect(() => {
    if (!enabled || !draft) return;
    const key = draftKey;
    if (shownRef.current?.key === key) {
      // Back to what is on screen (an undo): nothing to wait for.
      setSince(null);
      return;
    }
    const retrying = nonce !== seenNonce.current;
    seenNonce.current = nonce;
    const hit = cache.current.get(key);
    const recipe = draft;
    const on = base;
    const seen = view;
    const ctl = new AbortController();
    let cancelled = false;
    let timedOut = false;
    setSince((s) => s ?? Date.now());

    const run = async () => {
      setInflight(key);
      try {
        const p = hit ?? (await harnessApi.preview(runId, target, recipe, { signal: ctl.signal, base: on, view: seen }));
        if (p.kept !== false) remember(key, p);
        await decodeAll(picturesOf(p));
        if (cancelled) return;
        const prev = shownRef.current;
        const changed = !!prev && picturesOf(prev.preview).join("|") !== picturesOf(p).join("|");
        const next = { key, recipe, base: on, preview: p };
        shownRef.current = next;
        setShown(next);
        setError(null);
        setSince(null);
        if (changed) {
          setPulse(true);
          window.clearTimeout(pulseTimer.current);
          pulseTimer.current = window.setTimeout(() => setPulse(false), PULSE_MS);
        }
      } catch (e) {
        if (cancelled) return;
        setError(classify(key, e, timedOut, !!on));
        setSince(null);
      } finally {
        if (!cancelled) setInflight((k) => (k === key ? null : k));
      }
    };

    const delay = hit || !shownRef.current || retrying ? 0 : DEBOUNCE_MS;
    const first = !shownRef.current;
    const began = Date.now();
    const start = window.setTimeout(() => void run(), delay);
    let limit: number | undefined;
    const expire = () => {
      const w = warmupRef.current;
      const warming = first && !!w && (w.colours === "working" || w.masks.done < w.masks.total);
      if (warming && Date.now() - began < FIRST_TIMEOUT_MS) {
        limit = window.setTimeout(expire, FIRST_RECHECK_MS);
        return;
      }
      timedOut = true;
      ctl.abort();
    };
    limit = window.setTimeout(expire, delay + TIMEOUT_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(start);
      window.clearTimeout(limit);
      ctl.abort();
    };
    // draft, base and view are read through draftKey: the same key is the
    // same recipe on the same version, to the same end.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, draftKey, nonce, runId, target]);

  // The first preview's warm-up, polled only while it is the thing waited on.
  const waitingFirst = enabled && !shown && !(error && error.key === draftKey);
  const viewRef = useRef(view);
  viewRef.current = view;
  useEffect(() => {
    if (!waitingFirst) return;
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const w = await harnessApi.warmup(runId, target, undefined, viewRef.current);
        if (stopped) return;
        if (isWarmup(w)) setWarmup(w);
      } catch (e) {
        // A server without /warmup: the stage says less, nothing breaks.
        if (e instanceof HarnessHttpError && (e.status === 404 || e.status === 405)) return;
      }
      if (!stopped) timer = window.setTimeout(() => void poll(), WARMUP_EVERY_MS);
    };
    timer = window.setTimeout(() => void poll(), WARMUP_AFTER_MS);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [waitingFirst, runId, target]);

  const retry = useCallback(() => {
    setError(null);
    setNonce((n) => n + 1);
  }, []);

  const current = !!shown && shown.key === draftKey;
  const err = error && error.key === draftKey ? error : null;
  const status: PreviewStatus =
    !enabled || !draft || current ? "idle" : err ? "error" : inflight === draftKey ? "loading" : "waiting";
  const change = useMemo(
    () => (draft && !current ? describeChange(shown?.recipe ?? null, draft) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [draftKey, shown?.key, current],
  );

  return {
    shown,
    current,
    status,
    since: status === "waiting" || status === "loading" ? since : null,
    change,
    error: err,
    pulse: pulse && current,
    warmup,
    retry,
  };
}
