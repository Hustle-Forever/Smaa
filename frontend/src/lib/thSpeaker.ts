/**
 * Talking Head v2 "Speaker": who is on camera when no character is picked.
 *
 * The backend writes the speaker with the script: an Emirati speaker by
 * default (alternating man and woman across reels), or the one the topic
 * fixes. It comes back as run.speaker. While no character is picked the Look
 * step shows it in an editable Speaker field; the chips "Emirati man" and
 * "Emirati woman" ask the backend for a default speaker of that gender. The
 * field is saved with the Look step's main action (Generate stills, Apply
 * look) as edit's speaker_subject; once stills exist the backend re-shoots
 * the scenes showing the speaker.
 *
 * lock_character is a picked character's lock, so it is sent only when a
 * character is set: a run with none never claims one.
 *
 * Pure functions (checked by scripts/check-th-speaker.mjs).
 */
import type { TalkingHeadRun, TalkingHeadSpeaker } from "./types";

export const SPEAKER_CHIPS = ["Emirati man", "Emirati woman"] as const;
export type SpeakerChip = (typeof SPEAKER_CHIPS)[number];

export const SPEAKER_HINT =
  "Who is on camera when no character is picked. Saved with Generate stills or Apply look.";

const SOURCE_LABELS: Record<TalkingHeadSpeaker["source"], string> = {
  default_emirati: "Default",
  topic: "From the topic",
  user: "Yours",
};

function norm(s: string | null | undefined): string {
  return (s ?? "").trim().replace(/\s+/g, " ").toLowerCase();
}

/** Whether the Speaker field applies: no character is picked for the run
 *  and, on a multi-scene reel, none for scene 0 (every scene without a pick
 *  shows scene 0's person). */
export function speakerApplies(opts: {
  sceneCount: number;
  globalCharacter: boolean;
  sceneCharacters: boolean[];
}): boolean {
  if (opts.globalCharacter) return false;
  if (opts.sceneCount <= 1) return true;
  return !opts.sceneCharacters[0];
}

/** Whether any character is picked: the run's, or one of the first
 *  `sceneCount` scenes' (multi-scene only). */
export function anyCharacterSet(opts: {
  sceneCount: number;
  globalCharacter: boolean;
  sceneCharacters: boolean[];
}): boolean {
  if (opts.globalCharacter) return true;
  return opts.sceneCount > 1 && opts.sceneCharacters.slice(0, opts.sceneCount).some(Boolean);
}

/** The lock_character field of a request body: present only when a
 *  character is set. */
export function lockCharacterField(
  characterSet: boolean,
  lock: boolean,
): { lock_character?: boolean } {
  return characterSet ? { lock_character: lock } : {};
}

/** The text the Speaker field starts from: the run's speaker. */
export function speakerDraftFromRun(run: TalkingHeadRun | null | undefined): string {
  return run?.speaker?.subject ?? "";
}

/** The speaker_subject field of an edit body: the draft when the field
 *  applies, is not empty and differs from the run's speaker; else nothing. */
export function speakerPatch(
  draft: string | null | undefined,
  run: TalkingHeadRun | null | undefined,
  applies: boolean,
): { speaker_subject?: string } {
  const d = (draft ?? "").trim();
  if (!applies || !d || norm(d) === norm(run?.speaker?.subject)) return {};
  return { speaker_subject: d };
}

/** The chip to show as chosen: the one the draft names ("Emirati man", with
 *  or without "an"), or, while the draft is the run's own Emirati speaker,
 *  the chip of its gender. */
export function activeChip(
  draft: string | null | undefined,
  run: TalkingHeadRun | null | undefined,
): SpeakerChip | null {
  const d = norm(draft);
  for (const c of SPEAKER_CHIPS) {
    if (d === norm(c) || d === norm(`an ${c}`)) return c;
  }
  const sp = run?.speaker;
  if (sp && d && d === norm(sp.subject) && /\bemirati\b/i.test(sp.subject) && sp.gender) {
    return sp.gender === "man" ? "Emirati man" : "Emirati woman";
  }
  return null;
}

/** What a click on chip `chip` makes the draft: the chip, unless it is the
 *  chip already chosen (then null: nothing changes, so the run's own
 *  speaker never comes back as "Unsaved"). */
export function chipClick(
  chip: SpeakerChip,
  draft: string | null | undefined,
  run: TalkingHeadRun | null | undefined,
): string | null {
  return activeChip(draft, run) === chip ? null : chip;
}

/** A short label for where the run's speaker came from. */
export function speakerSourceLabel(speaker: TalkingHeadSpeaker | null | undefined): string | null {
  return speaker ? SOURCE_LABELS[speaker.source] ?? null : null;
}
