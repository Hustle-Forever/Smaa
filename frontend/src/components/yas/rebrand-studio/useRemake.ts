// Rebrand studio — "Make it again": a version made in house whose video is
// gone from the server, made again from its recipe and the original (the
// same frames, $0). POST /versions/{id}/remake starts it in the background;
// its state is polled each second until it is ready (then `onDone` reads
// the version's row again) or has failed (said beside the button, which
// stays to try once more).

import { useCallback, useEffect, useRef, useState } from "react";

import { harnessApi, HarnessHttpError } from "../../../lib/rebrandHarnessApi";
import { COPY } from "./studioCopy";

const POLL_MS = 1000;

export interface RemakeState {
  running: boolean;
  progress: number | null;
  error: string | null;
}

function messageOf(e: unknown): string {
  return e instanceof HarnessHttpError ? e.detail : e instanceof Error ? e.message : String(e);
}

export function useRemake(onDone: (versionId: number) => void) {
  const [state, setState] = useState<Record<number, RemakeState>>({});
  const timers = useRef(new Map<number, number>());
  const done = useRef(onDone);
  done.current = onDone;

  useEffect(
    () => () => {
      for (const t of timers.current.values()) window.clearTimeout(t);
      timers.current.clear();
    },
    [],
  );

  const set = (id: number, s: RemakeState) => setState((prev) => ({ ...prev, [id]: s }));

  const start = useCallback(async (id: number) => {
    set(id, { running: true, progress: null, error: null });
    const poll = async () => {
      try {
        const r = await harnessApi.remakeState(id);
        if (r.state === "running") {
          set(id, { running: true, progress: r.progress, error: null });
          timers.current.set(id, window.setTimeout(() => void poll(), POLL_MS));
          return;
        }
        timers.current.delete(id);
        if (r.state === "ready") {
          set(id, { running: false, progress: 1, error: null });
          done.current(id);
        } else {
          set(id, { running: false, progress: null, error: r.error ?? COPY.remakeUnfinished });
        }
      } catch (e) {
        timers.current.delete(id);
        set(id, { running: false, progress: null, error: messageOf(e) });
      }
    };
    try {
      const r = await harnessApi.remake(id);
      if (r.state === "ready") {
        set(id, { running: false, progress: 1, error: null });
        done.current(id);
        return;
      }
      if (r.state === "failed") {
        set(id, { running: false, progress: null, error: r.error ?? COPY.remakeUnfinished });
        return;
      }
      timers.current.set(id, window.setTimeout(() => void poll(), POLL_MS));
    } catch (e) {
      set(id, { running: false, progress: null, error: messageOf(e) });
    }
  }, []);

  return { state, start };
}
