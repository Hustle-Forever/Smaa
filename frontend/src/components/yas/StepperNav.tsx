/**
 * Human Element Video — stage navigation.
 *
 * Unlike the old StepChips (which derived the view from `phase` and was only
 * clickable in a special "edit mode"), this is TRUE navigation: every reachable
 * stage is always clickable and jumps the workspace to that stage. Each chip
 * shows live status — done ✓ / active ● / generating (spinner) / out-of-date
 * (amber dot) / locked (greyed, with a tooltip).
 */
import type { StudioStage } from "./TalkingHeadV2StepChips";

export interface StageStatus {
  reachable: boolean;
  done: boolean;
  generating: boolean;
  stale: boolean;
  /** Not a stage of this run's flow (Rebrand, on a backend without it). */
  hidden?: boolean;
}

const ORDER: StudioStage[] = ["topic", "look", "stills", "videos", "rebrand", "final"];
const LABELS: Record<StudioStage, string> = {
  topic: "Topic",
  look: "Look",
  stills: "Stills",
  videos: "Videos",
  rebrand: "Rebrand",
  final: "Final",
};
const LOCK_HINT: Record<StudioStage, string> = {
  topic: "",
  look: "",
  stills: "Generate the script first",
  videos: "Generate the still previews first",
  rebrand: "Finish the videos first",
  final: "Generate the videos first",
};

export function StepperNav({
  activeStage,
  status,
  onNavigate,
}: {
  activeStage: StudioStage;
  status: Record<StudioStage, StageStatus>;
  onNavigate: (s: StudioStage) => void;
}) {
  const order = ORDER.filter((s) => !status[s].hidden);
  return (
    <ol className="flex items-center gap-1 sm:gap-1.5 flex-wrap select-none">
      {order.map((s, idx) => {
        const st = status[s];
        const active = s === activeStage;
        const clickable = st.reachable;
        return (
          <li key={s} className="flex items-center gap-1 sm:gap-1.5">
            <button
              type="button"
              disabled={!clickable}
              onClick={() => clickable && onNavigate(s)}
              aria-current={active ? "step" : undefined}
              title={!clickable ? LOCK_HINT[s] : `Go to ${LABELS[s]}`}
              className={[
                "inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-[12px] font-medium transition-colors",
                active
                  ? "border-accent bg-accent/10 text-fg"
                  : clickable
                    ? "border-border bg-bg-subtle text-fg hover:bg-bg-muted cursor-pointer"
                    : "border-border/50 bg-transparent text-muted/50 cursor-not-allowed",
              ].join(" ")}
            >
              <span
                className={[
                  "inline-flex items-center justify-center w-4 h-4 rounded-full text-[9px] font-mono",
                  active
                    ? "bg-accent text-white"
                    : st.done
                      ? "bg-fg text-bg"
                      : "border border-current text-muted/70",
                ].join(" ")}
                aria-hidden
              >
                {st.generating ? (
                  <span className="w-2.5 h-2.5 rounded-full border-2 border-current border-t-transparent animate-spin" />
                ) : st.done && !active ? (
                  "✓"
                ) : (
                  idx + 1
                )}
              </span>
              <span>{LABELS[s]}</span>
              {st.stale && (
                <span
                  title="Out of date — regenerate"
                  className="w-1.5 h-1.5 rounded-full bg-warning"
                  aria-label="out of date"
                />
              )}
            </button>
            {idx < order.length - 1 && (
              <span className="text-muted/40 text-2xs" aria-hidden>
                →
              </span>
            )}
          </li>
        );
      })}
    </ol>
  );
}
