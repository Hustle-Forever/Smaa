// Rebrand studio — the EXACT video of a draft made in house: rendered on the
// server in the background (POST /house/render, polled by its key), as the
// very file a Generate of the same draft copies.
//
// It is only ever the video of the draft on screen: the answer is kept with
// the draft's key (the recipe, the version built on, the icon plan shown)
// and handed out while that is still the draft's — an edit asks for a new
// one (the server stops the old), and until it is ready the stage plays its
// quick stand-in. Asked a moment after the draft settles (an edit burst
// renders once), never while the draft's preview or icon plan is still on
// its way. A render that fails or can't be made (the plan shown is gone, a
// pattern the preview couldn't draw) leaves the stand-in playing; nothing
// is retried on its own — but one stopped by something other than this
// draft moving on (a Generate outranks it; another window's draft) is asked
// for once more.

import { useEffect, useState } from "react";

import {
  harnessApi,
  HarnessHttpError,
  recipeKey,
  viewKey,
  type HarnessBaseRef,
  type HarnessHouseRender,
  type HarnessRecipe,
  type SceneView,
} from "../../../lib/rebrandHarnessApi";

const SETTLE_MS = 700;
const POLL_MS = 1000;

export interface HouseRenderState {
  /** The draft's exact video, once made (null: the stand-in plays). */
  url: string | null;
  /** How far it is: the render for this draft (null: not asked yet). */
  render: HarnessHouseRender | null;
  /** It couldn't be made (the server's words). */
  error: string | null;
}

export function useHouseRender({
  runId,
  target,
  draft,
  base,
  planKey,
  enabled,
  view = null,
}: {
  runId: number;
  target: string;
  draft: HarnessRecipe | null;
  base: HarnessBaseRef | null;
  /** The scene's end set by hand (sent, and keyed, only when set). */
  view?: SceneView | null;
  /** The icon plan shown (a draft with icons), else null. */
  planKey: string | null;
  /** A draft made in house, its preview (and icon plan) the draft's own. */
  enabled: boolean;
}): HouseRenderState {
  const want =
    enabled && draft
      ? `${recipeKey(draft)}${base ? `@${base.mode}:${base.version_id}` : ""}#${planKey ?? ""}${viewKey(view)}`
      : "";
  const [got, setGot] = useState<{ key: string; render: HarnessHouseRender | null; error: string | null } | null>(
    null,
  );

  useEffect(() => {
    if (!want || !draft) return;
    let stopped = false;
    let timer: number | undefined;
    const ctl = new AbortController();
    const recipe = draft;
    const on = base;
    const plan = planKey;
    const seen = view;
    const done = (r: HarnessHouseRender) => r.state === "ready" || r.state === "failed" || r.state === "cancelled";
    const fail = (e: unknown) => {
      if (stopped) return;
      const message = e instanceof HarnessHttpError ? e.detail : e instanceof Error ? e.message : String(e);
      setGot({ key: want, render: null, error: message });
    };
    const poll = async (key: string, asked: boolean) => {
      try {
        const r = await harnessApi.houseRenderState(runId, target, key, ctl.signal, seen);
        if (stopped) return;
        // Stopped by something other than this draft moving on (a Generate
        // outranks it, another window's draft): asked for once more.
        if (r.state === "cancelled" && !asked) {
          timer = window.setTimeout(() => void start(true), POLL_MS);
          return;
        }
        setGot({ key: want, render: r, error: r.state === "failed" ? r.error ?? null : null });
        if (!done(r)) timer = window.setTimeout(() => void poll(key, asked), POLL_MS);
      } catch (e) {
        if (stopped) return;
        // Forgotten by the server (a restart): asked for once more.
        if (e instanceof HarnessHttpError && e.status === 404 && !asked) {
          timer = window.setTimeout(() => void start(true), POLL_MS);
          return;
        }
        if (e instanceof HarnessHttpError && e.status >= 400 && e.status < 500) {
          fail(e);
          return;
        }
        timer = window.setTimeout(() => void poll(key, asked), POLL_MS * 2);
      }
    };
    const start = async (asked = false) => {
      try {
        const r = await harnessApi.houseRender(runId, target, recipe, { base: on, iconsPlanKey: plan, signal: ctl.signal, view: seen });
        if (stopped) return;
        // Overtaken on the server by a newer ask (another window's): once more.
        if (r.state === "cancelled" && !asked) {
          timer = window.setTimeout(() => void start(true), POLL_MS);
          return;
        }
        setGot({ key: want, render: r, error: r.state === "failed" ? r.error ?? null : null });
        if (!done(r)) timer = window.setTimeout(() => void poll(r.key, asked), POLL_MS);
      } catch (e) {
        if (!stopped && !(e instanceof DOMException && e.name === "AbortError")) fail(e);
      }
    };
    timer = window.setTimeout(() => void start(), SETTLE_MS);
    return () => {
      stopped = true;
      ctl.abort();
      window.clearTimeout(timer);
    };
    // want holds the draft, the base, the plan and the end.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [want, runId, target]);

  const mine = !!want && got?.key === want ? got : null;
  const render = mine?.render ?? null;
  return {
    url: render?.state === "ready" && render.url ? render.url : null,
    render,
    error: mine?.error ?? null,
  };
}
