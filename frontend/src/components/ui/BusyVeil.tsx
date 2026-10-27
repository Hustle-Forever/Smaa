import { useEffect, useState, type ReactNode } from "react";
import { Check } from "lucide-react";

import { Spinner } from "./Spinner";

/**
 * "Something is happening to this picture."
 *
 * Wraps a media box. While `active`, the picture dims (and blurs a touch, for
 * anyone who has not asked for less motion) and a pill says what is being
 * done, with the time it has taken once that is worth saying: after 3 s the
 * pill adds m:ss, after `slowAfterMs` the `slowHint` line. When the work
 * lands, `done` flashes a small "Updated" chip.
 *
 * The dim is the point, not the pill. A picture that stays at full strength
 * while it is being replaced reads as current, and the reader edits against
 * a frame that is already out of date. The swap itself is the caller's: the
 * new picture should be decoded before it goes on screen, so the veil lifts
 * on the new frame, never on a half-loaded one.
 *
 * role="status" + aria-live="polite": the label is announced once when it
 * starts ("Painting the new colour…"), the slow hint once when it appears,
 * and "Updated" once when it lands. The m:ss counter is for the eye only
 * (aria-hidden): inside the live region it would be read out every second.
 */
function mmss(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = Math.floor(totalSeconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function BusyVeil({
  active,
  since,
  label,
  slowHint,
  slowAfterMs = 8000,
  dim,
  done = false,
  doneLabel = "Updated",
  placement = "lower",
  className = "",
  children,
}: {
  active: boolean;
  /** Date.now() when the wait started; elapsed is derived, never stored. */
  since?: number | null;
  label: ReactNode;
  slowHint?: ReactNode;
  slowAfterMs?: number;
  /** Dim without the pill (an error card is on top). Defaults to `active`. */
  dim?: boolean;
  done?: boolean;
  doneLabel?: string;
  /** "lower": the lower third, so a face in the middle stays visible. */
  placement?: "center" | "lower";
  className?: string;
  children: ReactNode;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [active]);

  const dimmed = dim ?? active;
  const elapsed = active && since ? Math.max(0, now - since) : 0;

  return (
    <div className={`relative ${className}`}>
      <div
        className={`h-full w-full transition-[opacity,filter] duration-150 ${
          dimmed ? "opacity-50 motion-safe:blur-[2px] saturate-50" : ""
        }`}
      >
        {children}
      </div>
      <div
        role="status"
        aria-live="polite"
        className={`pointer-events-none absolute inset-x-0 flex justify-center px-3 ${
          placement === "center" ? "top-1/2 -translate-y-1/2" : "bottom-[18%]"
        }`}
      >
        {active ? (
          <span className="max-w-full inline-flex flex-col items-center gap-0.5 rounded-2xl bg-black/70 text-white px-3.5 py-2 text-xs shadow-lift backdrop-blur-sm text-center">
            <span className="inline-flex items-center gap-2">
              <Spinner size="sm" />
              <span>{label}</span>
              {elapsed >= 3000 && (
                <span aria-hidden className="font-mono tabular-nums text-white/70">
                  {mmss(elapsed / 1000)}
                </span>
              )}
            </span>
            {slowHint && elapsed >= slowAfterMs && <span className="text-2xs text-white/70">{slowHint}</span>}
          </span>
        ) : done ? (
          <span className="inline-flex items-center gap-1.5 rounded-full bg-black/70 text-white px-3 py-1.5 text-xs shadow-lift motion-safe:animate-fadeIn">
            <Check className="h-3.5 w-3.5 text-accent" strokeWidth={3} aria-hidden />
            {doneLabel}
          </span>
        ) : null}
      </div>
    </div>
  );
}
