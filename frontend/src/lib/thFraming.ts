/**
 * Talking Head v2 "Framing" (#56): how close the camera is.
 *
 * Three choices: Close-up (head and shoulders), Above the hip (the default
 * for a new reel) and Full body. The Look step sets the run's framing; each
 * still's tile can re-frame that one still: a closer framing is a free crop
 * of the still around the face, a wider one is re-shot by the image model
 * (it edits the still itself, so the same person, outfit and room come back).
 * A run made before framing existed reads as Close-up, the framing its
 * stills were made at. A still that IS the character photo keeps the
 * photo's framing ("Framing as in your photo"), so its buttons are off; a
 * still made on the photo (keep the character, new background) shows the
 * photo's framing too, and any choice re-shoots it.
 *
 * Pure functions (checked by scripts/check-th-framing.mjs).
 */
import type {
  TalkingHeadFraming,
  TalkingHeadRun,
  TalkingHeadScene,
  TalkingHeadV2EditRequest,
} from "./types";

export const DEFAULT_FRAMING: TalkingHeadFraming = "above_hip";
export const LEGACY_FRAMING: TalkingHeadFraming = "close";
export const FRAMING_AS_PHOTO = "as_photo";
export const FRAMING_AS_PHOTO_HINT = "Framing as in your photo";

export interface FramingChoice {
  value: TalkingHeadFraming;
  /** The Look step's button. */
  label: string;
  /** A still tile's (narrow) button; `label` is its tooltip. */
  short: string;
  hint: string;
}

export const FRAMING_CHOICES: FramingChoice[] = [
  {
    value: "close",
    label: "Close-up",
    short: "Close",
    hint: "Head and shoulders: the best lip-sync.",
  },
  {
    value: "above_hip",
    label: "Above the hip",
    short: "Hip",
    hint: "Waist up, hands in view, more room for brand graphics.",
  },
  {
    value: "full_body",
    label: "Full body",
    short: "Full",
    hint: "Head to toe: the face is small, so lip-sync is less readable.",
  },
];

/** `v` when it is one of the three framings, else null. */
export function normFraming(v: unknown): TalkingHeadFraming | null {
  return v === "close" || v === "above_hip" || v === "full_body" ? v : null;
}

/** The Look step's starting framing: the run's (a run without one reads as
 *  Close-up, its stills were made so), or the default for a new reel. */
export function framingFromRun(run: TalkingHeadRun | null | undefined): TalkingHeadFraming {
  if (!run) return DEFAULT_FRAMING;
  return normFraming(run.framing) ?? LEGACY_FRAMING;
}

export function framingLabel(v: unknown): string {
  const f = normFraming(v);
  return FRAMING_CHOICES.find((c) => c.value === f)?.label ?? "";
}

export function framingChoice(v: unknown): FramingChoice | undefined {
  const f = normFraming(v);
  return FRAMING_CHOICES.find((c) => c.value === f);
}

/** A still tile's framing buttons. `supported` false: an older backend that
 *  knows no framing (no buttons). `current` is the framing the still shows
 *  (highlighted; null when it shows a photo's own). `disabled`: the still is
 *  the character photo itself. */
export interface StillFramingState {
  supported: boolean;
  current: TalkingHeadFraming | null;
  disabled: boolean;
  hint: string | null;
}

export function stillFraming(scene: TalkingHeadScene | null | undefined): StillFramingState {
  if (!scene || scene.framing_effective === undefined) {
    return { supported: false, current: null, disabled: true, hint: null };
  }
  if (scene.keyframe_url && scene.as_is) {
    return { supported: true, current: null, disabled: true, hint: FRAMING_AS_PHOTO_HINT };
  }
  if (!scene.keyframe_url) {
    // No still yet (being made, or failed): the framing it will be made at.
    return { supported: true, current: normFraming(scene.framing_effective), disabled: false, hint: null };
  }
  if (scene.framing_used === FRAMING_AS_PHOTO) {
    return { supported: true, current: null, disabled: false, hint: FRAMING_AS_PHOTO_HINT };
  }
  return {
    supported: true,
    current: normFraming(scene.framing_used) ?? normFraming(scene.framing_effective) ?? LEGACY_FRAMING,
    disabled: false,
    hint: null,
  };
}

/** The edit that re-shoots scene `sceneIdx`'s still at `value` (a single
 *  scene's framing is the run's; the backend handles both). */
export function sceneFramingPatch(
  run: TalkingHeadRun,
  sceneIdx: number,
  value: TalkingHeadFraming,
): Pick<TalkingHeadV2EditRequest, "scene_framings"> {
  const n = Math.max(1, run.scene_count ?? 1, sceneIdx + 1);
  const scene_framings: (TalkingHeadFraming | null)[] = Array.from({ length: n }, () => null);
  scene_framings[sceneIdx] = value;
  return { scene_framings };
}

/** What one image from the image model costs (lib/thStill STILL_IMAGE_COST):
 *  said when the caller passes no price (lib/thStill stillImageCost). */
const RESHOOT_COST = "~$0.04";

/** How a still at framing `current` (null: the photo's own) is re-framed to
 *  `target`, as the backend does it (_keyframe_work): the next TIGHTER
 *  framing is a crop of the still around the face (free, the same pixels);
 *  a wider one, or a still in the photo's framing, is re-shot by the image
 *  model (paid). Full body to Close-up is re-shot too: a crop never
 *  enlarges a still more than 3x, and a full-body face (about 6% of the
 *  frame) would need about 5x. A crop that cannot be made (no face found,
 *  or too small) falls back to a re-shoot: the tooltips say so. */
export function framingMove(current: unknown, target: TalkingHeadFraming): "crop" | "reshoot" {
  const rank = (f: unknown) => (f === "full_body" ? 0 : f === "above_hip" ? 1 : f === "close" ? 2 : -1);
  const from = rank(normFraming(current));
  return from >= 0 && rank(target) === from + 1 ? "crop" : "reshoot";
}

/** A still tile's framing button title: what pressing it does and whether
 *  it is free ("Close-up (crop, free)" / "Full body (re-shoot, ~$0.04)";
 *  `cost`: one image's price). `hasStill` false: the scene's still is made
 *  at that framing. */
export function framingButtonTitle(
  choice: FramingChoice,
  state: StillFramingState,
  hasStill: boolean,
  cost: string = RESHOOT_COST,
): string {
  if (state.disabled) return FRAMING_AS_PHOTO_HINT;
  if (state.current === choice.value) return `${choice.label}: this still's framing`;
  if (!hasStill) return `${choice.label}: make this still at it (${cost}). ${choice.hint}`;
  if (framingMove(state.current, choice.value) === "crop") {
    return `${choice.label} (crop, free): a closer crop of this still, the same pixels. If it can't be ` +
      `cropped (no face found, or too small to enlarge), it is re-shot instead (${cost}). ${choice.hint}`;
  }
  return `${choice.label} (re-shoot, ${cost}): the image model re-shoots this still at it, ` +
    `same person, outfit and room. ${choice.hint}`;
}

/** The toast when a still tile's framing button starts its work (`work`:
 *  the scene's keyframe_work from the backend, else framingMove's guess):
 *  [title, body]. A crop is free and says so; a re-shoot says its price. */
export function framingChangeToast(
  work: "crop" | "reshoot" | "regen" | null | undefined,
  target: TalkingHeadFraming,
  cost: string = RESHOOT_COST,
): [string, string] {
  const label = framingLabel(target);
  if (work === "crop") return [`Cropping to ${label}`, "A closer crop of this still: free, the same person to the pixel."];
  if (work === "reshoot") {
    return [`Re-shooting at ${label}`,
      `The image model re-shoots it (${cost}): same person, outfit and room; only the camera moves.`];
  }
  return [`Making the still at ${label}`, `A new still at this framing (${cost}).`];
}

/** The stills a new run framing `framing` re-frames on Apply look: those
 *  that follow the run's framing and show another one (never a still that
 *  is the character photo). */
function framingTargets(run: TalkingHeadRun | null | undefined, framing: TalkingHeadFraming) {
  if (!run || run.framing === undefined || framing === framingFromRun(run)) return [];
  return (run.scenes ?? []).filter(
    (s) => !!s.keyframe_url && !s.as_is && !normFraming(s.framing) && s.framing_used !== framing,
  );
}

/** How many stills a new run framing `framing` re-frames on Apply look
 *  (a crop or a re-shoot). 0 when the framing is the run's, or the backend
 *  knows no framing. */
export function framingReshootCount(
  run: TalkingHeadRun | null | undefined,
  framing: TalkingHeadFraming,
): number {
  return framingTargets(run, framing).length;
}

/** The line under the Look step's Framing control: what Apply look does to
 *  the stills (closer: a free crop; wider: a paid re-shoot at `cost` each). */
export function framingHint(
  framing: TalkingHeadFraming,
  run: TalkingHeadRun | null | undefined,
  cost: string = RESHOOT_COST,
): string {
  const hint = framingChoice(framing)?.hint ?? "";
  const targets = framingTargets(run, framing);
  const crops = targets.filter((s) => framingMove(s.framing_used, framing) === "crop").length;
  const shots = targets.length - crops;
  const stills = (n: number) => `${n} still${n === 1 ? "" : "s"}`;
  if (crops && shots) {
    return `${hint} Apply look crops ${stills(crops)} closer (free) and re-shoots ${shots} (${cost} each).`;
  }
  if (crops) return `${hint} Apply look crops ${stills(crops)} closer (free).`;
  if (shots) return `${hint} Apply look re-shoots ${stills(shots)} at this framing (${cost} each).`;
  return hint;
}
