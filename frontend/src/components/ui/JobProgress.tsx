import { useEffect, useState } from "react";

import { Spinner } from "./Spinner";

/**
 * What a long job shows while it runs.
 *
 * Four creators each drew their own version of this, and all four were the same
 * lie: a 30%-wide div sliding across a track forever, conveying no information
 * about how far along anything was. Each injected a raw <style> tag into its
 * JSX to define the keyframe, under a name renamed per file to dodge a
 * collision -- yasProgressPo, yasProgressHc, yasProgress. The dodge did not
 * work: PosterHumanCreator and TalkingHeadV2Creator both landed on
 * `yasProgress` with DIFFERENT bodies (a 370% track sweep versus a 100% button
 * sheen), @keyframes are global, and both components live under the Producer
 * slot that stays mounted across tabs. Whichever rendered last silently
 * redefined the other.
 *
 * The honest version. The backend does not report a percentage for these jobs,
 * so this does not invent one. What it can truthfully say is:
 *
 *   - how long you have been waiting (counted here, from a real timestamp)
 *   - how long this kind of job usually takes (one table, below)
 *   - which step of how many, WHEN the caller actually knows
 *
 * The elapsed counter is the part users asked for and only one surface in the
 * app had -- the AI panel in the video editor. Knowing you are 40 seconds into
 * a job that usually takes 60 is worth more than a bar that has been sliding
 * since you pressed the button.
 */

/**
 * Typical durations, in one place.
 *
 * These were prose scattered across the creators and they disagreed with each
 * other -- "about 30-60s", "~30-60 seconds" and "(~15-30 s)" for overlapping
 * work. Numbers here, phrasing derived, so they cannot drift apart again.
 */
export const JOB_ETA = {
  poster: [30, 60],
  poster_human: [30, 60],
  human_character: [30, 60],
  animation: [60, 120],
  still: [15, 30],
  video: [60, 180],
  broll: [90, 240],
  /** A Rebrand studio version: Omni edits the scene. */
  rebrand: [120, 240],
  /** A Rebrand studio version made in house (run 815, this PC: 4-5 s when
   *  the studio's exact video is copied, 17 s rendered, ~35 s on a scene
   *  never analysed). */
  rebrand_house: [10, 40],
  /** The studio's first preview of a scene (the intro colour dig: a cold
   *  scene measured 60 s on run 815, a warm one a few seconds). */
  rebrand_preview: [15, 60],
} as const;

export type JobKind = keyof typeof JOB_ETA;

function etaPhrase(kind: JobKind | undefined): string | null {
  if (!kind) return null;
  const [lo, hi] = JOB_ETA[kind];
  // Whole minutes read as minutes ("usually 2\u20134 min"); every older kind has
  // a bound under two minutes, so its phrase is unchanged.
  if (lo >= 120 && lo % 60 === 0 && hi % 60 === 0) return `usually ${lo / 60}\u2013${hi / 60} min`;
  return `usually ${lo}\u2013${hi}s`;
}

function mmss(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = Math.floor(totalSeconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function JobProgress({
  startedAt,
  kind,
  step,
  label = "Working\u2026",
  className = "",
}: {
  /** Date.now() when the job started. Elapsed is derived, never stored. */
  startedAt: number;
  kind?: JobKind;
  /** Pass only when the pipeline genuinely reports it. */
  step?: { done: number; total: number };
  label?: string;
  className?: string;
}) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);

  const elapsed = Math.max(0, (now - startedAt) / 1000);
  const eta = etaPhrase(kind);

  // A real ratio when the caller has one, an indeterminate sweep when it does
  // not. Never a fake percentage.
  const determinate = step && step.total > 0;
  const pct = determinate ? Math.min(100, (step.done / step.total) * 100) : 0;

  return (
    <div className={className} aria-live="polite" aria-busy="true">
      <div className="h-1 rounded-full bg-bg-muted overflow-hidden">
        {determinate ? (
          <div
            className="h-full bg-accent rounded-full transition-all duration-500 ease-out"
            style={{ width: `${pct}%` }}
          />
        ) : (
          <div className="h-full w-[30%] bg-accent rounded-full animate-indeterminate" />
        )}
      </div>

      {/* On a phone the typical time is a line of its own: squeezed beside a
          long label it broke mid-phrase. */}
      <p className="mt-1.5 flex flex-wrap items-center justify-center gap-x-2 gap-y-0.5 text-2xs text-muted">
        <Spinner size="xs" />
        <span>{label}</span>
        {determinate && (
          <span className="font-mono tabular-nums">
            {step.done}/{step.total}
          </span>
        )}
        <span className="font-mono tabular-nums">{mmss(elapsed)}</span>
        {eta && (
          <span className="whitespace-nowrap max-sm:basis-full max-sm:text-center">
            <span className="max-sm:hidden">· </span>
            {eta}
          </span>
        )}
      </p>
    </div>
  );
}
