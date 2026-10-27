// Rebrand studio — the version a draft is added on top of, made ready:
// each part's frame as Omni will get it (the speaker from frame 0, the
// version's graphics settled) and every layer it already shows.
//
// Asked for only in "Add to it" (a changed version is redone from the
// original, so its own frames don't matter), once per version per session.
// The first time takes ~5-10 s: the server cuts the version's parts and
// settles their frames. A 409 (not ready, or on its way) or a 410 (its video
// is gone) comes back in the server's own words and is not worth a Try
// again: Change it still works.

import { useCallback, useEffect, useRef, useState } from "react";

import { rebrandErrorDetail } from "../../../lib/rebrandApi";
import {
  harnessApi,
  HarnessHttpError,
  type HarnessBaseInfo,
  type HarnessBaseRef,
  type SceneView,
} from "../../../lib/rebrandHarnessApi";
import { COPY } from "./studioCopy";

export interface StudioBaseState {
  info: HarnessBaseInfo | null;
  loading: boolean;
  error: { message: string; status: number | null; retryable: boolean } | null;
  retry: () => void;
}

export function useStudioBase(
  runId: number,
  target: string,
  base: HarnessBaseRef | null,
  enabled: boolean,
  view: SceneView | null = null,
): StudioBaseState {
  const id = base?.mode === "stack" && enabled ? base.version_id : null;
  // On the whole clip past the automatic cut (the studio is mounted per
  // clip, so this never changes under it).
  const variant = view?.variant === "full" ? "full" : null;
  const cache = useRef(new Map<number, HarnessBaseInfo>());
  const [info, setInfo] = useState<HarnessBaseInfo | null>(null);
  const [error, setError] = useState<StudioBaseState["error"]>(null);
  const [loading, setLoading] = useState(false);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    setError(null);
    if (id === null) {
      setLoading(false);
      return;
    }
    const known = cache.current.get(id);
    if (known) {
      setInfo(known);
      setLoading(false);
      return;
    }
    // Another version's frames never stand in for this one's.
    setInfo(null);
    setLoading(true);
    const ctl = new AbortController();
    harnessApi
      .base(runId, target, id, ctl.signal, variant ? { variant } : null)
      .then((b) => {
        if (ctl.signal.aborted) return;
        const got = { ...b, version_id: id };
        cache.current.set(id, got);
        setInfo(got);
        setLoading(false);
      })
      .catch((e) => {
        if (ctl.signal.aborted) return;
        const status = e instanceof HarnessHttpError ? e.status : null;
        const theirs = status === 409 || status === 410 || status === 404;
        setError({
          message: theirs && e instanceof HarnessHttpError ? e.detail : e instanceof TypeError ? COPY.offline : rebrandErrorDetail(e),
          status,
          retryable: !theirs,
        });
        setLoading(false);
      });
    return () => ctl.abort();
  }, [runId, target, id, nonce, variant]);

  const retry = useCallback(() => setNonce((n) => n + 1), []);
  return {
    info: info && info.version_id === id ? info : null,
    loading: id !== null && loading,
    error: id !== null ? error : null,
    retry,
  };
}
