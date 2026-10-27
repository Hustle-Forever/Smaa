/**
 * Global Producer Roadmap stepper — the master 5-step rail shown on top of the
 * Producer surface: Producer → Editor → Preview → Calendar → Approval.
 *
 * Mirrors the look of the Human Element Video local StepperNav, but represents the
 * GLOBAL workflow. While the user works inside a creator, this rail stays frozen
 * on "Editor" (and shows the active tool, e.g. "Editing: Human Element Video") while the
 * creator's OWN local sub-stepper advances beneath it.
 *
 * Reachability: a step is clickable when its index ≤ `maxReachedIndex` (the
 * furthest the user has progressed). You can always step BACK to a completed
 * step; you can't skip ahead.
 */
import {
  GLOBAL_STEP_LABELS,
  GLOBAL_STEP_ORDER,
  type GlobalStep,
} from "./types";

export function GlobalStepper({
  activeStep,
  maxReachedIndex,
  editorLabel,
  onNavigate,
}: {
  activeStep: GlobalStep;
  /** Highest step index (0..4) the user has reached; ≤ this is clickable. */
  maxReachedIndex: number;
  /** Optional dynamic label for the Editor step, e.g. "Editing: Human Element Video". */
  editorLabel?: string;
  onNavigate: (s: GlobalStep) => void;
}) {
  return (
    <ol className="flex items-center gap-1 sm:gap-1.5 flex-wrap select-none">
      {GLOBAL_STEP_ORDER.map((s, idx) => {
        const active = s === activeStep;
        const done = idx < maxReachedIndex && !active;
        const clickable = idx <= maxReachedIndex;
        const label =
          s === "editor" && editorLabel ? editorLabel : GLOBAL_STEP_LABELS[s];
        return (
          <li key={s} className="flex items-center gap-1 sm:gap-1.5">
            <button
              type="button"
              disabled={!clickable}
              onClick={() => clickable && onNavigate(s)}
              aria-current={active ? "step" : undefined}
              title={clickable ? `Go to ${GLOBAL_STEP_LABELS[s]}` : "Not reached yet"}
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
                    : done
                      ? "bg-fg text-bg"
                      : "border border-current text-muted/70",
                ].join(" ")}
                aria-hidden
              >
                {done ? "✓" : idx + 1}
              </span>
              <span>{label}</span>
            </button>
            {idx < GLOBAL_STEP_ORDER.length - 1 && (
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
