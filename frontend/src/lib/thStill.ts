// Talking Head v2 stills: Refine makes ONE change on the current still (a
// true image edit, so its framing, room and light stay unless the change
// names them), Regenerate makes it again the way first generation did (no
// text; also for a failed still), and Undo puts back the version from
// before the last change. Pure helpers for the Stills tiles; checked by
// scripts/check-th-still.mjs.

export type StillAction = "refine" | "regenerate" | "undo";

export const STILL_REFINE_PLACEHOLDER = "One change, e.g. warmer light, zoom out to above the hip";

export interface StillTileState {
  /** The tile shows a still (there is something to refine). */
  hasStill: boolean;
  /** This still is being made or changed right now. */
  busy: boolean;
  /** Something else on the page is running (another tile's change, a
   *  re-shoot or the videos being polled): every button waits, so nothing
   *  stops that polling or runs twice. */
  pageBusy?: boolean;
  /** The tile's change text. */
  change: string;
  /** The backend has an earlier version to go back to (scene.can_undo). */
  canUndo: boolean;
  /** The still is the character photo itself (used as-is): Regenerate would
   *  only give back the same photo, and its clip would be made again. */
  asIsPhoto?: boolean;
  /** What one image costs now (stillImageCost); STILL_IMAGE_COST when unsaid. */
  cost?: string;
}

/** What one image from the image model costs (the backend meter's rate for
 *  gemini-2.5-flash-image, $0.04): a Refine, a Regenerate, a re-shoot. The
 *  price said when the backend does not send it (run.still_image_usd). */
export const STILL_IMAGE_COST = "~$0.04";

/** The price of one still image as the backend meters it now
 *  (run.still_image_usd: $0.13 once the Pro image model is on, $0.04 for
 *  the standard one), else STILL_IMAGE_COST. */
export function stillImageCost(usd: number | null | undefined): string {
  return typeof usd === "number" && Number.isFinite(usd) && usd > 0 ? `~$${usd.toFixed(2)}` : STILL_IMAGE_COST;
}

export const regenerateTitle = (cost: string = STILL_IMAGE_COST) =>
  `Make this still again from its references, the way it was first made (${cost})`;
export const refineTitle = (cost: string = STILL_IMAGE_COST) => `Make this one change on the still (${cost})`;
export const REGENERATE_TITLE = regenerateTitle();
export const REFINE_TITLE = refineTitle();
export const UNDO_TITLE = "Put back the version from before the last change (free)";
export const AS_IS_REGENERATE_HINT = "This is your photo: turn on New bg on the Look step for a new still";

export interface StillTileButtons {
  /** The change box and the Refine button: only on a tile with a still. */
  showRefine: boolean;
  refineEnabled: boolean;
  refineTitle: string;
  /** Regenerate is on every still tile, a failed or empty one too; off
   *  for a still that is the character photo (regenerateHint says why). */
  regenerateEnabled: boolean;
  regenerateTitle: string;
  regenerateHint: string | null;
  showUndo: boolean;
  undoEnabled: boolean;
}

export function stillTileButtons(s: StillTileState): StillTileButtons {
  const hasText = s.change.trim().length > 0;
  const wait = s.busy || !!s.pageBusy;
  const photo = s.hasStill && !!s.asIsPhoto;
  return {
    showRefine: s.hasStill,
    refineEnabled: s.hasStill && !wait && hasText,
    refineTitle: hasText ? refineTitle(s.cost) : "Type one change above first",
    regenerateEnabled: !wait && !photo,
    regenerateTitle: photo ? AS_IS_REGENERATE_HINT : regenerateTitle(s.cost),
    regenerateHint: photo ? AS_IS_REGENERATE_HINT : null,
    showUndo: s.canUndo,
    undoEnabled: s.canUndo && !wait,
  };
}

/** The toast when an edit's background re-shoot of a still fails: it names
 *  a button the still tiles have. */
export const STILL_REGEN_FAILED_HINT = "Use Regenerate on the tile, or re-pick the image.";

/** What a Stills tile shows: its picture (ready), an error (failed), nothing
 *  asked for (empty), a pick with no card made from it yet (not_made: waiting
 *  for "Make card", not being made), or a spinner (generating). */
export type PreviewTileStatus = "ready" | "generating" | "failed" | "empty" | "not_made";

export function previewTileStatus(
  card: { url?: string | null; card_missing?: boolean } | null | undefined,
  err: string | null | undefined,
): PreviewTileStatus {
  if (card?.url) return "ready";
  if (err) return "failed";
  if (card == null) return "empty";
  return card.card_missing ? "not_made" : "generating";
}

/** The line on a still tile whose last change failed while the previous
 *  still stays on show (an edit's background re-shoot): the error is in
 *  its title. */
export const STILL_KEPT_AFTER_ERROR = "The last change failed: this is the previous still";

/** " 0:45": how long a change has been running, so a slow image model is
 *  never a silent wait (run 830's refine sat five minutes with no word). */
function elapsedSuffix(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "";
  const s = Math.max(0, Math.floor(seconds));
  return ` ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** What a still's background work is (scene.keyframe_work): a framing crop
 *  (free, the same pixels), a re-shoot by the image model, or the still
 *  made again. */
export type StillWork = "crop" | "reshoot" | "regen";

/** The tile overlay while `action` runs, with the time it has run
 *  ("Refining… 0:45"). An edit's background work has no action: `work`
 *  says what it is, so a free framing crop never reads "Regenerating…"
 *  ("Cropping…", "Re-shooting…"; made again, or unknown: "Regenerating…"). */
export function stillBusyLabel(
  action: StillAction | null | undefined,
  seconds?: number | null,
  work?: StillWork | null,
): string {
  const t = elapsedSuffix(seconds);
  if (action === "refine") return `Refining…${t}`;
  if (action === "undo") return `Going back…${t}`;
  if (action === "regenerate") return `Regenerating…${t}`;
  if (work === "crop") return `Cropping…${t}`;
  if (work === "reshoot") return `Re-shooting…${t}`;
  return `Regenerating…${t}`;
}

/** The Stills status line while the background work `works` (the scenes
 *  being made) runs, with the time it has run: what it is and whether it
 *  is paid (`cost`: one image's price, stillImageCost). */
export function stillWorkStatusLine(
  works: (StillWork | null | undefined)[],
  seconds?: number | null,
  cost: string = STILL_IMAGE_COST,
): string {
  const t = elapsedSuffix(seconds);
  const kind = workKind(works);
  if (kind === "crop") return `Cropping the still closer (free)…${t}`;
  if (kind === "reshoot") return `Re-shooting the still at its new framing (${cost} each)…${t}`;
  if (kind === "reframe") return `Re-framing the stills (a crop is free, a re-shoot ${cost})…${t}`;
  return `Making the new still…${t}`;
}

/** The toast when an applied edit starts background work on the stills
 *  (works: each generating scene's keyframe_work): [title, body]. */
export function stillWorkToast(
  works: (StillWork | null | undefined)[],
  cost: string = STILL_IMAGE_COST,
): [string, string] {
  const kind = workKind(works);
  if (kind === "crop") {
    return ["Cropping the still", "A closer crop of the still: free, the same person to the pixel."];
  }
  if (kind === "reshoot") {
    return ["Re-shooting the still",
      `The image model re-shoots it at the new framing (${cost} each): same person, outfit and room.`];
  }
  if (kind === "reframe") {
    return ["Re-framing the stills",
      `A crop of a still is free; the others are re-shot by the image model (${cost} each).`];
  }
  return ["Regenerating still", "Applying your new image to the scene…"];
}

/** All crops, all re-shoots, a mix of the two (reframe), or anything else
 *  (a still made again, or unknown: regen). */
function workKind(works: (StillWork | null | undefined)[]): "crop" | "reshoot" | "reframe" | "regen" {
  if (!works.length || works.some((w) => w !== "crop" && w !== "reshoot")) return "regen";
  if (works.every((w) => w === "crop")) return "crop";
  if (works.every((w) => w === "reshoot")) return "reshoot";
  return "reframe";
}

/** The Stills status line while `action` runs on `what` ("the main still"),
 *  with the time it has run. */
export function stillStatusLine(action: StillAction | null | undefined, what: string, seconds?: number | null): string {
  const t = elapsedSuffix(seconds);
  if (action === "refine") return `Refining ${what}…${t}`;
  if (action === "undo") return `Putting back the previous version of ${what}…${t}`;
  return `Regenerating ${what}…${t}`;
}

/** A refine stops by itself: the server asks the image model at most twice,
 *  about three minutes in all, then says it took too long. */
export const STILL_REFINE_LIMIT_HINT = "A change takes up to about three minutes; it stops by itself after that.";

/** The limit said beside a running `action`'s status line: only a refine
 *  has it (a Regenerate is not bounded the same way and can take longer,
 *  an Undo is quick); "" for the others. */
export function stillLimitHint(action: StillAction | null | undefined): string {
  return action === "refine" ? STILL_REFINE_LIMIT_HINT : "";
}

/** The toast once `action` is done on the tile titled `label` ("Scene 2"). */
export function stillDoneToast(action: StillAction, label: string): string {
  if (action === "refine") return `${label} refined`;
  if (action === "undo") return `${label}: previous version back`;
  return `${label} regenerated`;
}

/** The toast title when `action` fails (the body is the server's message). */
export function stillFailTitle(action: StillAction): string {
  if (action === "refine") return "Refine failed";
  if (action === "undo") return "Undo failed";
  return "Regenerate failed";
}
