/**
 * Progress indicator for the Human Element Video studio. Shows the user where
 * they are in the five-stage flow (Topic → Look → Stills → Videos → Final)
 * with checkmarks for completed stages and a filled dot for the active one.
 *
 * Pure presentational. Doesn't drive navigation — clicking a past stage
 * scrolls the relevant section into view (the parent handles that).
 */
import type { TalkingHeadRun } from "../../lib/types";

export type StudioStage = "topic" | "look" | "stills" | "videos" | "rebrand" | "final";

const ORDER: StudioStage[] = ["topic", "look", "stills", "videos", "rebrand", "final"];

const LABELS: Record<StudioStage, string> = {
  topic: "Topic",
  look: "Look",
  stills: "Stills",
  videos: "Videos",
  rebrand: "Rebrand",
  final: "Final",
};

/** Derive the active stage from the phase + run snapshot. */
export function deriveStage(
  phase: string,
  run: TalkingHeadRun | null,
): StudioStage {
  if (phase === "ready" || phase === "saving" || phase === "saved") return "final";
  if (phase === "stitching") return "final";
  if (
    phase === "videos_ready" ||
    phase === "polling_videos" ||
    phase === "finalizing" ||
    phase === "retrying_video_main" ||
    phase === "retrying_video_intro" ||
    phase === "retrying_video_outro"
  ) {
    return "videos";
  }
  if (run) return "stills";
  return "topic";
}

interface Props {
  stage: StudioStage;
  /** When the user has set at least one reference, mark Look as visited. */
  hasReferences: boolean;
  /** Called when a navigable stage is clicked. Optional. */
  onJump?: (target: StudioStage) => void;
  /** v2 editor — allow clicking the CURRENT stage too (to re-open its inputs). */
  allowCurrent?: boolean;
}

export function TalkingHeadV2StepChips({ stage, hasReferences, onJump, allowCurrent }: Props) {
  const activeIdx = ORDER.indexOf(stage);
  const visitedLook = hasReferences || activeIdx > ORDER.indexOf("look");
  return (
    <ol className="flex items-center gap-1.5 text-2xs font-medium select-none">
      {ORDER.map((s, idx) => {
        const isActive = s === stage;
        const isPast =
          idx < activeIdx ||
          (s === "topic" && activeIdx > 0) ||
          (s === "look" && visitedLook && activeIdx > 1);
        const isFuture = idx > activeIdx && !(s === "look" && visitedLook);
        const clickable = !!onJump && (isPast || (allowCurrent && isActive));
        const cls = isActive
          ? "text-accent"
          : isPast
            ? "text-fg"
            : "text-muted/60";
        return (
          <li key={s} className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={clickable ? () => onJump!(s) : undefined}
              disabled={!clickable}
              className={[
                "inline-flex items-center gap-1 px-2 py-0.5 rounded-full border transition-colors",
                isActive
                  ? "border-accent bg-accent/8"
                  : isPast
                    ? "border-border bg-bg-subtle hover:bg-bg-muted"
                    : "border-border/60 bg-transparent cursor-default",
                cls,
              ].join(" ")}
              aria-current={isActive ? "step" : undefined}
            >
              <span
                className={[
                  "inline-flex items-center justify-center w-3.5 h-3.5 rounded-full text-[9px]",
                  isActive
                    ? "bg-accent text-white"
                    : isPast
                      ? "bg-fg text-bg"
                      : "border border-border/60",
                ].join(" ")}
                aria-hidden
              >
                {isPast && !isActive ? "✓" : isActive ? "●" : ""}
              </span>
              <span>{LABELS[s]}</span>
            </button>
            {idx < ORDER.length - 1 && (
              <span
                className={isFuture ? "text-muted/30" : "text-muted/60"}
                aria-hidden
              >
                ·
              </span>
            )}
          </li>
        );
      })}
    </ol>
  );
}
