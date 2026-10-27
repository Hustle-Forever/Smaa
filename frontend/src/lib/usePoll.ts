import { useEffect, useRef } from "react";

/**
 * The one polling loop.
 *
 * Ten files rolled their own, across five intervals (2s, 3s, 4s, 5s, and a
 * magic number) and three scheduling idioms: setInterval, a self-rescheduling
 * setTimeout, and `await new Promise(r => setTimeout(r, 5000))` inside a while
 * loop. Only the two in trends/ backed off when the server was failing; the
 * rest hammered a sick backend at a fixed rate.
 *
 * setTimeout, not setInterval, and deliberately: setInterval fires on a
 * schedule regardless of whether the previous request came back, so a slow
 * endpoint gets overlapping in-flight polls that resolve out of order. Each
 * tick here is scheduled only after the last one settles.
 *
 * pauseWhenHidden DEFAULTS TO FALSE. This is a video tool: people start a
 * ten-minute render and go and do something else, and a poller that stops when
 * the tab is backgrounded means they come back to a screen still claiming
 * "generating". Tab-pausing is a per-caller decision about a specific surface,
 * never a default applied to ten of them at once -- especially since nothing
 * would catch the regression, as neither typecheck nor build has an opinion
 * about it.
 */
export function usePoll({
  enabled,
  intervalMs,
  onTick,
  onError,
  backoffFactor = 2,
  maxIntervalMs,
  pauseWhenHidden = false,
}: {
  /** Poll only while true. Flipping to false stops and resets the backoff. */
  enabled: boolean;
  intervalMs: number;
  /** Return "stop" to end the loop -- the job reached a terminal state. */
  onTick: () => Promise<"stop" | void> | "stop" | void;
  /** Called when onTick throws. The loop keeps going, slower. */
  onError?: (err: unknown) => void;
  backoffFactor?: number;
  /** Ceiling for the backoff. Defaults to 8x the base interval. */
  maxIntervalMs?: number;
  pauseWhenHidden?: boolean;
}) {
  // Through refs so the effect below can have honest deps. Callers pass inline
  // arrows that change identity every render; depending on them would tear the
  // loop down and rebuild it on every parent re-render, which for a polling
  // parent means constantly.
  const onTickRef = useRef(onTick);
  onTickRef.current = onTick;
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  useEffect(() => {
    if (!enabled) return;

    let cancelled = false;
    let timer: number | undefined;
    let delay = intervalMs;
    const ceiling = maxIntervalMs ?? intervalMs * 8;

    const schedule = () => {
      timer = window.setTimeout(run, delay);
    };

    const run = async () => {
      if (cancelled) return;

      if (pauseWhenHidden && document.hidden) {
        schedule();
        return;
      }

      try {
        const result = await onTickRef.current();
        if (cancelled) return;
        if (result === "stop") return;
        delay = intervalMs; // recovered
      } catch (err) {
        if (cancelled) return;
        onErrorRef.current?.(err);
        delay = Math.min(delay * backoffFactor, ceiling);
      }
      schedule();
    };

    schedule();

    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [enabled, intervalMs, backoffFactor, maxIntervalMs, pauseWhenHidden]);
}
