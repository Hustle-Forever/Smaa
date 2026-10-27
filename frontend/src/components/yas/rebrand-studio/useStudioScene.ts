// Rebrand studio — the catalogue (what the studio offers) and the scene
// (prepared once: its parts, their first frames, the 42 default, the
// versions). Each fails on its own and has its own Retry.
//
// One scene opened is one prepare: a second ask for the same scene while
// the first is on its way (React's development double mount, a remount on
// the stage) shares it instead of sending another.
//
// The scene's end set by hand (`view`) goes with the prepare that opens it
// (the scene's seconds come back cut to it); a later move of the end
// prepares nothing again — the studio works the seconds out itself
// (lib/rebrandEnd), and `preparedView` says which end the answer was for.
//
// The stage may prepare a scene ahead (`prepareAhead`: the whole clip, in
// the background, while the end grip is dragged past the automatic cut):
// the studio that then opens on it takes that answer instead of asking
// again — once.

import { useCallback, useEffect, useRef, useState } from "react";

import { rebrandErrorDetail } from "../../../lib/rebrandApi";
import {
  harnessApi,
  viewKey,
  type HarnessCatalogue,
  type HarnessPrepared,
  type SceneView,
} from "../../../lib/rebrandHarnessApi";

const preparing = new Map<string, Promise<HarnessPrepared>>();
/** Scenes prepared ahead, by run, target and variant, with the view they
 *  were prepared with: taken by the next studio to open on one. */
const ahead = new Map<string, { view: SceneView | null; prepared: HarnessPrepared }>();

function aheadKey(runId: number, target: string, view: SceneView | null): string {
  return `${runId}:${target}:${view?.variant ?? "base"}`;
}

function prepareOnce(runId: number, target: string, view: SceneView | null): Promise<HarnessPrepared> {
  const key = `${runId}:${target}${viewKey(view)}`;
  let p = preparing.get(key);
  if (!p) {
    p = harnessApi.prepare(runId, target, view);
    preparing.set(key, p);
    const done = () => {
      if (preparing.get(key) === p) preparing.delete(key);
    };
    p.then(done, done);
  }
  return p;
}

/** Prepare a scene ahead of the studio that will open on it (free: the
 *  server's CPU, behind its house gate). */
export async function prepareAhead(runId: number, target: string, view: SceneView | null): Promise<HarnessPrepared> {
  const p = await prepareOnce(runId, target, view);
  ahead.set(aheadKey(runId, target, view), { view, prepared: p });
  return p;
}

export function useStudioScene(runId: number, target: string, view: SceneView | null = null) {
  const [catalogue, setCatalogue] = useState<HarnessCatalogue | null>(null);
  const [catalogueError, setCatalogueError] = useState<string | null>(null);
  const [catalogueKey, setCatalogueKey] = useState(0);
  const [prepared, setPrepared] = useState<HarnessPrepared | null>(null);
  const [preparedView, setPreparedView] = useState<SceneView | null>(null);
  const [prepareError, setPrepareError] = useState<string | null>(null);
  const viewRef = useRef(view);
  viewRef.current = view;
  const [prepareKey, setPrepareKey] = useState(0);
  // When the current prepare started, for the "0:07" on the waiting stage.
  const [startedAt, setStartedAt] = useState(() => Date.now());

  useEffect(() => {
    let cancelled = false;
    setCatalogueError(null);
    harnessApi
      .catalogue()
      .then((c) => !cancelled && setCatalogue(c))
      .catch((e) => !cancelled && setCatalogueError(rebrandErrorDetail(e)));
    return () => {
      cancelled = true;
    };
  }, [catalogueKey]);

  useEffect(() => {
    let cancelled = false;
    setPrepareError(null);
    setStartedAt(Date.now());
    const asked = viewRef.current;
    // Prepared ahead (the stage did, in the background): that answer, once —
    // gone after this mount has settled (React's development double mount
    // runs this twice).
    const k = aheadKey(runId, target, asked);
    const early = ahead.get(k);
    if (early) {
      setPreparedView(early.view);
      setPrepared(early.prepared);
      window.setTimeout(() => {
        if (ahead.get(k) === early) ahead.delete(k);
      }, 0);
      return;
    }
    prepareOnce(runId, target, asked)
      .then((p) => {
        if (cancelled) return;
        setPreparedView(asked);
        setPrepared(p);
      })
      .catch((e) => !cancelled && setPrepareError(rebrandErrorDetail(e)));
    return () => {
      cancelled = true;
    };
  }, [runId, target, prepareKey]);

  const retryCatalogue = useCallback(() => setCatalogueKey((k) => k + 1), []);
  const retryPrepare = useCallback(() => setPrepareKey((k) => k + 1), []);

  return {
    catalogue,
    setCatalogue,
    catalogueError,
    retryCatalogue,
    prepared,
    preparedView,
    prepareError,
    retryPrepare,
    startedAt,
  };
}
