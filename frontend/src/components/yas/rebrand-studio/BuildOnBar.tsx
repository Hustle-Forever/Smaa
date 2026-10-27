// Rebrand studio — the bar at the top of the rail while the draft carries on
// from a finished version. Kept short (on a phone it sits above the rows):
//
//   [≡] Adding to v3 · everything you change here makes v4           [x]
//       [ Add to it | Change it ]      (a version Omni made only)
//   one line: what that means
//   (Add)      "Getting v3 ready…" the first time, or why it can't be used
//   "Carrying on from v3. Undo" right after Generate made it (or "Loaded
//   v3's settings. Undo" after a Change it by hand)
//   "The icons came out differently from v3's this time. Keep v3's icons"
//   Start over from the original
//
// A version made in house from the original is carried on from as its whole
// recipe (the same picture until something changes: Add and Change would
// draw the same pixels), so it has no chips and no [x] — "Start over from
// the original" is the way out. A version with Omni's video under it (made
// by Omni, or in house on top of one) keeps both chips and the [x]: Add
// keeps that video and draws on it, Change redoes it flat from the original
// (the words say so); [x] keeps the new layers on a fresh draft (drawn on
// the original), or the loaded settings. Every switch is one undo step.

import { Layers, RotateCcw, X } from "lucide-react";

import { ChoiceChips } from "../../ui/ChoiceChips";
import { Notice } from "../../ui/controls";
import { Spinner } from "../../ui/Spinner";
import { COPY } from "./studioCopy";
import type { StudioBaseState } from "./useStudioBase";

const LINK =
  "rounded font-medium text-accent-hover underline-offset-2 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40";

export function BuildOnBar({
  no,
  nextNo,
  mode,
  onMode,
  onLeave,
  addBlocked,
  base,
  loaded,
  continued = false,
  onUndoLoaded,
  onDismissLoaded,
  addText,
  changeText,
  house = false,
  onStartOver,
  startOverTitle = COPY.startOverTitle,
  iconsMoved = null,
  iconsKept = false,
}: {
  /** The version carried on from (v3 → 3). */
  no: number;
  /** The version Generate makes next (v4 → 4). */
  nextNo: number;
  mode: "stack" | "rebuild";
  onMode: (mode: "stack" | "rebuild") => void;
  onLeave: () => void;
  /** Why "Add to it" is off (null: it isn't). */
  addBlocked: string | null;
  /** The version made ready to add on top of (Add only). */
  base: StudioBaseState;
  /** Its settings were just loaded (Change, or carried on from after
   *  Generate): offer to undo that. */
  loaded: boolean;
  /** …by the studio itself, after Generate made it ("Carrying on from v3"). */
  continued?: boolean;
  onUndoLoaded: () => void;
  onDismissLoaded: () => void;
  /** What Add to it / Change it mean for this version (made in house, or by
   *  Omni) — the plain words when absent. */
  addText?: string;
  changeText?: string;
  /** Made in house from the original: no chips, no [x] (see the file's
   *  note). */
  house?: boolean;
  /** Back to a fresh scene (the 42 default + automatic icons), one undo step. */
  onStartOver: () => void;
  /** What Start over gives, said on it (an intro or outro: an empty draft). */
  startOverTitle?: string;
  /** Its icons, planned again, came out otherwise than it froze them. */
  iconsMoved?: { onKeep: () => void } | null;
  /** The draft keeps its icons exactly ("Keep v3's icons"). */
  iconsKept?: boolean;
}) {
  const baseError = mode === "stack" ? base.error : null;
  const addWords = addText ?? COPY.addExplain(no);
  const changeWords = changeText ?? COPY.changeExplain;
  return (
    <section
      aria-label={COPY.buildingOn(no)}
      className="shrink-0 space-y-1.5 border-b border-border bg-accent-fade/40 px-3 pb-2 pt-2"
    >
      <div className="flex items-center gap-2">
        <Layers className="h-3.5 w-3.5 shrink-0 text-accent-hover" aria-hidden />
        <h3 className="min-w-0 flex-1 truncate text-xs text-fg">
          <span className="font-semibold">{COPY.buildingOn(no)}</span>
          <span className="text-muted"> · {COPY.makesNext(nextNo)}</span>
        </h3>
        {!house && (
          <ChoiceChips
            label={COPY.buildMode}
            className="shrink-0 flex-nowrap"
            options={[
              {
                value: "stack" as const,
                label: COPY.addToIt,
                title: addBlocked ?? addWords,
                disabled: !!addBlocked && mode !== "stack",
              },
              { value: "rebuild" as const, label: COPY.changeIt, title: changeWords },
            ]}
            value={mode}
            onChange={onMode}
          />
        )}
        {!house && (
          <button
            type="button"
            onClick={onLeave}
            aria-label={COPY.leaveBuild(no)}
            title={COPY.leaveBuild(no)}
            className="-mr-1 h-7 w-7 shrink-0 rounded-lg flex items-center justify-center text-muted hover:text-fg hover:bg-bg-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
          >
            <X className="h-3.5 w-3.5" aria-hidden />
          </button>
        )}
      </div>
      <p className="text-2xs leading-snug text-muted">{mode === "stack" ? addWords : changeWords}</p>

      {mode === "stack" && base.loading && (
        <p role="status" className="flex items-center gap-1.5 text-2xs text-muted">
          <Spinner size="xs" tone="accent" />
          {COPY.gettingBase(no)}
        </p>
      )}
      {baseError && (
        <Notice
          tone="warning"
          action={
            baseError.retryable ? (
              <button type="button" className="btn btn-xs btn-ghost" onClick={base.retry}>
                <RotateCcw className="h-3 w-3" aria-hidden /> {COPY.retry}
              </button>
            ) : (
              <button type="button" className="btn btn-xs btn-ghost" onClick={() => onMode("rebuild")}>
                {COPY.changeInstead}
              </button>
            )
          }
        >
          {baseError.message}
        </Notice>
      )}
      {loaded && (
        <p role="status" className="flex items-center gap-2 text-2xs text-fg">
          <span className="min-w-0 flex-1">
            {continued ? COPY.continuedFrom(no) : COPY.loadedFrom(no)}{" "}
            <button type="button" onClick={onUndoLoaded} className={LINK}>
              {COPY.undo}
            </button>
          </span>
          <button
            type="button"
            onClick={onDismissLoaded}
            aria-label="Dismiss"
            className="h-6 w-6 shrink-0 rounded flex items-center justify-center text-muted hover:text-fg hover:bg-bg-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
          >
            <X className="h-3 w-3" aria-hidden />
          </button>
        </p>
      )}
      {iconsMoved ? (
        <p role="status" className="text-2xs leading-snug text-fg">
          {COPY.iconsMoved(no)}{" "}
          <button type="button" onClick={iconsMoved.onKeep} title={COPY.keepIconsTitle} className={LINK}>
            {COPY.keepIcons(no)}
          </button>
        </p>
      ) : iconsKept ? (
        <p className="text-2xs leading-snug text-muted">{COPY.iconsKept(no)}</p>
      ) : null}
      <button type="button" onClick={onStartOver} title={startOverTitle} className={`text-2xs ${LINK}`}>
        {COPY.startOver}
      </button>
    </section>
  );
}
