/**
 * Global Producer Roadmap stepper: the master 5-step rail shown on top of the
 * Producer surface: Ideation → Editing → Finalization → Scheduling → Approval.
 *
 * While the user works inside a creator, this rail stays frozen on "Editing"
 * (and shows the active tool, e.g. "Editing: Human Element Video") while the
 * creator's OWN local sub-stepper advances beneath it.
 *
 * Reachability: a step is clickable when its index ≤ `maxReachedIndex` (the
 * furthest the user has progressed). You can always step BACK to a completed
 * step; you can't skip ahead.
 */
import { Check } from "lucide-react";

import {
  GLOBAL_STEP_LABELS,
  GLOBAL_STEP_ORDER,
  type GlobalStep,
} from "./types";

// One short line under each step, so a first-time user knows what happens there.
const STEP_HINTS: Record<GlobalStep, string> = {
  producer: "Pick a tool",
  editor: "Make it",
  preview: "Write the caption",
  calendar: "Pick a date",
  approval: "Post it",
};

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
    <ol className="flex items-stretch gap-1 min-w-max select-none">
      {GLOBAL_STEP_ORDER.map((s, idx) => {
        const active = s === activeStep;
        const done = idx < maxReachedIndex && !active;
        const clickable = idx <= maxReachedIndex;
        const label =
          s === "editor" && editorLabel ? editorLabel : GLOBAL_STEP_LABELS[s];
        return (
          <li key={s} className="flex items-center gap-1">
            <button
              type="button"
              disabled={!clickable}
              onClick={() => clickable && onNavigate(s)}
              aria-current={active ? "step" : undefined}
              title={clickable ? `Go to ${GLOBAL_STEP_LABELS[s]}` : "Not reached yet"}
              className={[
                "flex items-center gap-2.5 pl-1.5 pr-3.5 py-1.5 rounded-none text-left transition-all duration-200",
                "focus:outline-none focus-visible:ring-2 focus-visible:ring-fg/30",
                active
                  ? "bg-k-surface shadow-[0_0_0_1.5px_rgb(var(--k-btn)),0_6px_16px_-8px_rgb(var(--k-btn)/0.35)]"
                  : clickable
                    ? "hover:bg-k-surface/80 cursor-pointer"
                    : "opacity-50 cursor-not-allowed",
              ].join(" ")}
            >
              <span
                className={[
                  "inline-flex items-center justify-center w-7 h-7 shrink-0 rounded-none text-[12px] font-semibold tabular-nums",
                  active
                    ? "bg-k-cyan text-k-oncyan"
                    : done
                      ? "bg-k-text text-k-canvas"
                      : "bg-k-surface text-fg/60 ring-1 ring-fg/15",
                ].join(" ")}
                aria-hidden
              >
                {done ? <Check className="w-3.5 h-3.5" strokeWidth={2.6} /> : idx + 1}
              </span>
              <span className="leading-tight">
                <span
                  className={[
                    "block text-[13px] font-semibold whitespace-nowrap",
                    active ? "text-fg" : clickable ? "text-fg/80" : "text-fg/60",
                  ].join(" ")}
                >
                  {label}
                </span>
                <span className="block text-[11.5px] text-fg/50 whitespace-nowrap">{STEP_HINTS[s]}</span>
              </span>
            </button>
            {idx < GLOBAL_STEP_ORDER.length - 1 && (
              <span
                className={["w-5 h-px shrink-0", idx < maxReachedIndex ? "bg-fg/40" : "bg-fg/15"].join(" ")}
                aria-hidden
              />
            )}
          </li>
        );
      })}
    </ol>
  );
}
