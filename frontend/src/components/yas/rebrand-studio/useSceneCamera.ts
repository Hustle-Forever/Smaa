// Rebrand studio — the scene's camera (GET /house/camera), asked for only
// while "Show camera tracking" is on.
//
// The server works it out once per scene, after the speaker masks (the
// speaker on every frame, then the camera): until then it answers how far it
// is, and this asks again every couple of seconds. Once it is "ready" (or
// "failed") nothing more is asked. A server without the route counts as
// failed, so the stage says so instead of waiting.

import { useEffect, useState } from "react";

import { harnessApi, HarnessHttpError, type HarnessCamera, type SceneView } from "../../../lib/rebrandHarnessApi";

const POLL_MS = 2000;

export function useSceneCamera(
  runId: number,
  target: string,
  enabled: boolean,
  view: SceneView | null = null,
): HarnessCamera | null {
  // The whole clip past the automatic cut is a clip of its own.
  const variant = view?.variant === "full" ? "full" : null;
  const key = `${runId}:${target}${variant ? ":full" : ""}`;
  const [got, setGot] = useState<{ key: string; camera: HarnessCamera } | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let timer: number | undefined;
    const ctl = new AbortController();
    const poll = async () => {
      try {
        const camera = await harnessApi.houseCamera(runId, target, ctl.signal, variant ? { variant } : null);
        if (stopped) return;
        setGot({ key, camera });
        if (camera.state === "ready" || camera.state === "failed") return;
      } catch (e) {
        if (stopped) return;
        if (e instanceof HarnessHttpError && (e.status === 404 || e.status === 405)) {
          setGot({ key, camera: { state: "failed", speaker_frames: { done: 0, total: 0 } } });
          return;
        }
        // Anything else (the server busy, a blip): asked again below.
      }
      timer = window.setTimeout(() => void poll(), POLL_MS);
    };
    void poll();
    return () => {
      stopped = true;
      ctl.abort();
      window.clearTimeout(timer);
    };
  }, [enabled, runId, target, key]);

  return enabled && got?.key === key ? got.camera : null;
}
