/**
 * Talking Head v2 intro / outro cards: how a picked image is used, when a
 * card counts as made, and what its Stills tile says.
 *
 * A picked or uploaded intro/outro image is kept AS IT IS by default (the
 * card is that image, padded to the video's shape when it is another shape);
 * "New design in this style" draws a new card from it. A card counts as made
 * only when it is a real card row made from the image picked now: the pick
 * alone (shown while no card exists) or a card made from another image (run
 * 824) is not, so the reel waits for it instead of animating the wrong
 * picture. "Editing an existing run" means generate-previews ran (scene 0's
 * still or a card exists), not that any keyframe exists (run 827).
 *
 * Pure functions (checked by scripts/check-th-bookends.mjs).
 */
import type {
  TalkingHeadAnimation,
  TalkingHeadAspect,
  TalkingHeadCard,
  TalkingHeadPreviousCard,
  TalkingHeadRefMode,
  TalkingHeadRun,
} from "./types";

export type BookendRole = "intro" | "outro";

export const DEFAULT_REF_MODE: TalkingHeadRefMode = "as_is";

/** The two modes the Look step offers (older runs may carry "text_only"). */
export const REF_MODE_CHOICES: ReadonlyArray<readonly [TalkingHeadRefMode, string, string]> = [
  ["as_is", "Keep as-is", "Use this image exactly as it is as the card"],
  ["new_vibe", "New design in this style", "Draw a new card in this image's colours and style"],
];

/** A stored or picked mode as one the UI knows ("as_is" when unknown). */
export function normRefMode(mode: string | null | undefined): TalkingHeadRefMode {
  return mode === "new_vibe" || mode === "text_only" || mode === "as_is" ? mode : DEFAULT_REF_MODE;
}

/** The line under the mode control. */
export function refModeHint(mode: string | null | undefined): string {
  const m = normRefMode(mode);
  if (m === "new_vibe") return "Draws a new card in this image's colours and style, with your script's words.";
  if (m === "text_only") {
    return "Made before Keep as-is existed: the image is redrawn with your script's words. Pick a mode to change it.";
  }
  return "Your image is the card, pixel for pixel (fitted to the video size, never redrawn).";
}

/** Whether the reel wants a card for this bookend: not skipped, and no
 *  finished video uploaded for it (that video is used as it is). */
export function bookendWanted(run: TalkingHeadRun | null | undefined, role: BookendRole): boolean {
  if (!run) return false;
  const excluded = role === "intro" ? run.intro_excluded : run.outro_excluded;
  const video = role === "intro" ? run.refs?.intro_video_upload_id : run.refs?.outro_video_upload_id;
  return !excluded && video == null;
}

/** A real card made from the image picked now (not the pick shown while no
 *  card exists, not a card made from another image). */
export function cardReady(card: TalkingHeadCard | null | undefined): boolean {
  return !!card && card.id != null && !!card.url && !card.card_missing && card.matches_refs !== false;
}

/** The bookends the reel wants whose card is not made. */
export function missingCards(run: TalkingHeadRun | null | undefined): BookendRole[] {
  if (!run) return [];
  return (["intro", "outro"] as const).filter(
    (role) => bookendWanted(run, role) && !cardReady(role === "intro" ? run.intro : run.outro),
  );
}

/** Every still and every wanted card is made: the videos can be made. */
export function previewsReady(run: TalkingHeadRun | null | undefined): boolean {
  return !!run?.main?.url && missingCards(run).length === 0;
}

/** generate-previews ran (it makes scene 0's still and the cards together),
 *  even when scene 0's still failed (its error is recorded: the later scenes'
 *  stills were made all the same, and generating again would pay for them
 *  twice). A keyframe of a later scene alone does not count: the create-flow
 *  trap of run 827, where a scene count change made one before any card
 *  existed and "Generate stills" became "Apply look". */
export function editingExisting(run: TalkingHeadRun | null | undefined): boolean {
  if (!run) return false;
  return !!run.main?.url || !!run.main?.error || !!run.main_error ||
    run.intro?.id != null || run.outro?.id != null;
}

/** The Videos tile of an intro/outro whose card is missing: its clip waits
 *  for the card (a retry would animate the bare pick). */
export const CARD_FIRST_NOTE = "No card from this pick yet: make it first, then its clip";

/** What the Videos tile of bookend `role` says instead of offering a retry
 *  (CARD_FIRST_NOTE while its wanted card is missing), else null. */
export function bookendClipWaits(run: TalkingHeadRun | null | undefined, role: BookendRole): string | null {
  return missingCards(run).includes(role) ? CARD_FIRST_NOTE : null;
}

/** "the intro card", "the outro card", "the intro and outro cards". */
export function cardsPhrase(roles: BookendRole[]): string {
  if (roles.length === 0) return "the cards";
  if (roles.length === 2) return "the intro and outro cards";
  return `the ${roles[0]} card`;
}

export interface CardTileNote {
  /** The card is the picked image itself. */
  kept: boolean;
  /** No card made from the image picked now. */
  needsMaking: boolean;
  /** "Text on the card: ..." / "Text asked for: ...", or null. */
  textLine: string | null;
  /** How its clip moves: what its clip turned out to be when it has one
   *  (clipMotionLine), else the plan for the next one; null when neither. */
  motionLine: string | null;
}

/** What an intro/outro clip turned out to be, from its own record (not the
 *  plan): Veo moved its icons, the in-house slow zoom, or a Veo clip the
 *  check caught changing the card, replaced by the slow zoom (run 830's
 *  intro said "icons move" while its clip was the zoom). null for a clip
 *  with no such record (a scene, an older clip). */
export function clipMotionLine(clip: Pick<TalkingHeadAnimation, "motion"> | null | undefined): string | null {
  switch (clip?.motion) {
    case "veo":
      return "Motion: icons move (Veo)";
    case "zoom":
      return "Motion: slow zoom";
    case "veo_replaced":
      return "Motion: slow zoom (Veo changed the card, so it was replaced)";
    case "veo_checking":
      return "Motion: icons move (Veo, being checked)";
    case "veo_flagged":
      return "Motion: Veo, which changed the card (the slow zoom could not be made)";
    case "veo_unchecked":
      return "Motion: icons move (Veo, not checked)";
    default:
      return null;
  }
}

/** A card with no motion plan under the current rules (made before them,
 *  like run 830's outro, or its look failed): never an older plan as if it
 *  were current. The backend plans it once in the background when the run
 *  is opened; until then, and if that fails, the clip decides it. */
export const MOTION_DECIDED_LATER = "Motion: decided when the clip is made";

/** What a card's Stills tile (and its Videos tile) says under the picture.
 *  `clip`: its clip, when one was made from this card (`clipStale` false):
 *  the motion line is what that clip became; otherwise the plan for the
 *  next clip ("planned"), or MOTION_DECIDED_LATER for a card with no plan
 *  yet (`motion` null). */
export function cardTileNote(
  card: TalkingHeadCard | null | undefined,
  clip?: Pick<TalkingHeadAnimation, "motion"> | null,
  clipStale = false,
): CardTileNote {
  const needsMaking = !!card && (!!card.card_missing || card.matches_refs === false);
  const text = (card?.card_text ?? "").split("\n").map((t) => t.trim()).filter(Boolean).join(" / ");
  let textLine: string | null = null;
  if (!needsMaking && text) {
    textLine = card?.card_text_source === "detected" ? `Text on the card: ${text}` : `Text asked for: ${text}`;
  }
  let motionLine: string | null = null;
  if (!needsMaking) {
    motionLine = clipStale ? null : clipMotionLine(clip);
    if (!motionLine && card?.motion === "veo") motionLine = "Motion planned: icons move (Veo)";
    if (!motionLine && card?.motion === "zoom") motionLine = "Motion planned: slow zoom";
    if (!motionLine && card && card.motion === null) motionLine = MOTION_DECIDED_LATER;
  }
  return { kept: !needsMaking && !!card?.as_is, needsMaking, textLine, motionLine };
}

/** The card tile's button: its label and title. Text is optional: without
 *  it the card is made again from the picked image and mode. */
export function cardRetryButton(feedback: string, needsMaking: boolean): { label: string; title: string } {
  if (needsMaking) return { label: "Make card", title: "Make this card from the picked image and mode" };
  if (feedback.trim()) return { label: "Retry", title: "Make this card again with your note" };
  return { label: "Make again", title: "Make this card again from the picked image and mode" };
}

// ---------------------------------------------------------------------------
// Previous reels' cards, and a kept card's words (Part B)
// ---------------------------------------------------------------------------
//
// The intro/outro picker also lists the cards of earlier videos ("Previous
// videos"). A picked one is kept as it is, like any pick; when it is used at
// its own size with its words unchanged, its finished clip comes with it (no
// new Veo). A kept card's headline or subtitle that differs from the script
// is swapped for the script's words, unless "Keep card text" is on (then the
// script says the card's words). Logos, web addresses and calls to action are
// never changed.

/** A previous reel's card held by an intro/outro slot. */
export interface PickedCard {
  card_id: number;
  url: string;
  topic: string | null;
  card_text: string | null;
}

/** The URL of a card's image (same-origin path the backend serves). */
export function cardImageUrl(cardId: number): string {
  return `/api/yas-producer/talking-head-v2/cards/${cardId}/file`;
}

/** A picked "Previous videos" card as a slot holds it. */
export function pickedCardFrom(c: TalkingHeadPreviousCard): PickedCard {
  return { card_id: c.card_id, url: c.url || cardImageUrl(c.card_id), topic: c.topic || null, card_text: c.card_text };
}

/** The slot's card on resume, from refs.{role}_card_id (no topic known). */
export function pickedCardFromRefs(cardId: number | null | undefined): PickedCard | null {
  if (cardId == null) return null;
  return { card_id: cardId, url: cardImageUrl(cardId), topic: null, card_text: null };
}

/** The slot's line for a picked previous card. */
export function pickedCardLabel(c: PickedCard): string {
  return c.topic ? `From "${c.topic}"` : `Card #${c.card_id} from an earlier video`;
}

/** The card's lines on one line, " / " between them. */
function oneLine(text: string | null | undefined): string {
  return (text ?? "").split("\n").map((t) => t.trim()).filter(Boolean).join(" / ");
}

export interface PreviousCardNote {
  title: string;
  /** Its text, one line, or null when not known. */
  text: string | null;
  /** Its shape against the video's ("9:16", or "1:1: fitted to 9:16"). */
  shape: string;
  /** Its clip, when one is reused with it, or null. */
  clip: string | null;
}

/** What a "Previous videos" tile says. */
export function previousCardNote(c: TalkingHeadPreviousCard, videoAspect: TalkingHeadAspect): PreviousCardNote {
  const own = c.aspect ?? (c.width && c.height ? `${c.width}x${c.height}` : "another size");
  const fits = c.aspect === videoAspect;
  let clip: string | null = null;
  if (fits && c.clip_animation_id != null) {
    clip = c.clip_style === "animate_icons"
      ? "Clip ready (its icons move): reused if its text stays"
      : "Clip ready (slow zoom): reused if its text stays";
  }
  return {
    title: c.topic ? `From "${c.topic}"` : `Card #${c.card_id}`,
    text: oneLine(c.card_text) || null,
    shape: fits ? own : `${own}: fitted to ${videoAspect}`,
    clip,
  };
}

/** The line under the "Keep card text" toggle. */
export function keepTextHint(on: boolean): string {
  return on
    ? "The card keeps its own words, and the script says them too."
    : "A headline or subtitle that differs from your script is swapped for your script's words. Logos, web addresses and calls to action never change.";
}

export interface CardWordsNote {
  /** What was swapped, or that the card keeps its words; null otherwise. */
  swapLine: string | null;
  /** The swap failed or the text could not be read (the card keeps its
   *  own words), or some lines were swapped and a line kept its own words
   *  (the warning names it). */
  warning: string | null;
  /** Its clip is a previous video's, reused. */
  clipLine: string | null;
}

/** What a kept card's Stills tile says about its words and its clip. */
export function cardWordsNote(card: TalkingHeadCard | null | undefined): CardWordsNote {
  const none: CardWordsNote = { swapLine: null, warning: null, clipLine: null };
  if (!card || card.card_missing || card.matches_refs === false || !card.as_is) return none;
  const swap = card.text_swap ?? null;
  let swapLine: string | null = null;
  if (swap?.status === "swapped" && swap.swapped.length) {
    swapLine = "Text swapped: " + swap.swapped.map((s) => `"${s.old}" for "${s.new}"`).join("; ");
  } else if (swap?.status === "kept") {
    swapLine = "Keeps its own words";
  }
  const warning =
    swap && (swap.status === "failed" || swap.status === "unread" || swap.status === "swapped")
      ? swap.warning || null
      : null;
  return {
    swapLine,
    warning,
    clipLine: card.reused_clip ? "Clip reused from the earlier video (no new Veo)" : null,
  };
}

/** Whether the script's words for this bookend are the kept card's own
 *  ("Keep card text" on a made, kept card): the script shows them read-only. */
export function cardTextLocked(run: TalkingHeadRun | null | undefined, role: BookendRole): boolean {
  if (!run) return false;
  const card = role === "intro" ? run.intro : run.outro;
  const keep = role === "intro" ? run.refs?.intro_keep_text : run.refs?.outro_keep_text;
  return !!keep && cardReady(card) && !!card?.as_is;
}
