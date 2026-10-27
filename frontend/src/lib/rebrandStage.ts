// The Rebrand stage's own rules, as pure functions (checked by
// scripts/check-rebrand-stage.mjs): the deep-link step, where a reopened run
// lands, which scene the strip opens, and what each tile and the Finalize
// button say.
//
// The reel block (run.reel) comes from a backend that has the stage; an
// older one sends no reel_ready at all, and every rule here then gives
// today's answer.

import type { StudioStage } from "../components/yas/TalkingHeadV2StepChips";
import type { ReelBlock, ReelTarget } from "./types";

// ---------------------------------------------------------------------------
// The deep-link step: "rebrand" (the first included main scene), or
// "rebrand_scene_1" / "rebrand_intro" / "rebrand_outro" — one token, so it
// rides the same colon segment as every other step (producerHash).
// ---------------------------------------------------------------------------

const STEP = /^rebrand(?:_(intro|outro|scene_\d{1,2}))?$/;

/** The Rebrand stage's step → its scene (null: the first included main
 *  scene); null for any other step. */
export function parseRebrandStep(step: string | null | undefined): { target: string | null } | null {
  const m = typeof step === "string" ? step.match(STEP) : null;
  return m ? { target: m[1] ?? null } : null;
}

/** The step the URL carries for the Rebrand stage on `target`. */
export function rebrandStep(target: string | null | undefined): string {
  return target ? `rebrand_${target}` : "rebrand";
}

const PLAIN: readonly StudioStage[] = ["topic", "look", "stills", "videos", "final"];

/** A deep-link step as a stage, or null when it names none (a bogus or
 *  absent step falls back to where the run lands). The Rebrand stage only
 *  when the backend has it (`hasRebrand`). */
export function stageOfStep(step: string | null | undefined, hasRebrand: boolean): StudioStage | null {
  if (!step) return null;
  if ((PLAIN as readonly string[]).includes(step)) return step as StudioStage;
  return hasRebrand && parseRebrandStep(step) ? "rebrand" : null;
}

// ---------------------------------------------------------------------------
// Where a reopened run lands (Library "Continue editing" carries no step)
// ---------------------------------------------------------------------------

export interface LandingRun {
  composition_status: string | null | undefined;
  /** Every included clip made and none out of date. Absent: an older
   *  backend (no Rebrand stage). */
  reel_ready?: boolean;
  /** A still exists (a run closed on Look before any was made lands there). */
  hasStills: boolean;
}

/** The furthest stage a run has reached. A reel that can be finalized opens
 *  on Rebrand; a finished reel whose clips went out of date opens on Final,
 *  never on Videos (entering Videos makes Veo clips again, which costs). */
export function landingStage(r: LandingRun): StudioStage {
  const cs = r.composition_status;
  if (r.reel_ready === undefined) {
    if (cs === "ready" || cs === "rendering") return "final";
    if (cs === "queued" || cs === "videos_ready" || cs === "failed") return "videos";
  } else {
    if (cs === "rendering") return "final";
    if (cs === "ready") return r.reel_ready ? "rebrand" : "final";
    if (cs === "videos_ready" || cs === "failed") return r.reel_ready ? "rebrand" : "videos";
    if (cs === "queued") return "videos";
  }
  return r.hasStills ? "stills" : "look";
}

// ---------------------------------------------------------------------------
// The strip: which scenes it shows, and which one opens
// ---------------------------------------------------------------------------

/** At most this many tiles (intro, five scenes, outro). */
export const MAX_TILES = 7;

/** Whether the studio can open this segment: a main scene always; the intro
 *  and outro when the server's studio opens them (`bookends`: the reel block
 *  says so — an older server refuses them) and a clip is found for them. */
export function openable(t: ReelTarget, bookends = false): boolean {
  return t.role === "main" || (bookends && !!t.src);
}

/** The strip's tiles, in reel order. */
export function stripTargets(reel: ReelBlock | null | undefined, bookends = false): ReelTarget[] {
  return (reel?.targets ?? []).filter((t) => openable(t, bookends)).slice(0, MAX_TILES);
}

/** The scene the stage opens: the one asked for while it is still in the
 *  reel (a refresh, a tile click), else the first included main scene —
 *  never blindly scene_0, which may be excluded. */
export function resolveTarget(reel: ReelBlock | null | undefined, wanted: string | null | undefined,
  bookends = false): string | null {
  const tiles = stripTargets(reel, bookends);
  if (wanted && tiles.some((t) => t.target === wanted)) return wanted;
  return (tiles.find((t) => t.role === "main") ?? tiles[0])?.target ?? null;
}

// ---------------------------------------------------------------------------
// A tile's state (one amber "!" for every problem)
// ---------------------------------------------------------------------------

export const FPS = 30;

/** Frames → "7.5" (one decimal, no trailing ".0"). */
export function endSeconds(frames: number): string {
  const s = Math.round((frames / FPS) * 10) / 10;
  return Number.isInteger(s) ? String(s) : s.toFixed(1);
}

export interface TileState {
  /** The pill on the tile's second line. */
  badge: "rebranded" | "trimmed" | null;
  badgeTitle: string | null;
  /** A version on its way (the server's, or one Generate just started). */
  making: boolean;
  makingTitle: string | null;
  /** Why the tile needs a look (the amber "!"), or null. */
  problem: string | null;
}

/** What one tile shows. `making`: a version this page just started for it
 *  (before the server's reel says so); `no`: a version's number in its
 *  scene (v2), when the page knows it; `endOf`: the end a version was made
 *  for (null: automatic; undefined: not known) — the version that goes in,
 *  made for another end than the scene's, needs a look ("v2 ends at 7.8 s"). */
export function tileState(
  t: ReelTarget,
  opts: {
    making?: number | null;
    no?: (versionId: number) => number | null | undefined;
    endOf?: (versionId: number) => number | null | undefined;
  } = {},
): TileState {
  const name = (id: number) => {
    const n = opts.no?.(id);
    return n ? `v${n}` : "Its version";
  };
  const makingId = t.making ?? opts.making ?? null;
  let badge: TileState["badge"] = null;
  let badgeTitle: string | null = null;
  if (t.ships != null) {
    badge = "rebranded";
    badgeTitle = `${name(t.ships)} is in the reel`;
  } else if (t.end.user != null) {
    badge = "trimmed";
    badgeTitle = `Ends at ${endSeconds(t.end.user)} s`;
  }
  let problem: string | null = null;
  const p = t.pick;
  if (p && makingId !== p.version_id) {
    if (p.state === "failed") problem = `${name(p.version_id)} failed`;
    else if (p.state === "lost") problem = `${name(p.version_id)} isn't on this computer: open it and press Remake`;
    else if (p.state === "other_clip") problem = `${name(p.version_id)} was made on an older clip`;
  }
  if (!problem && t.ships != null && opts.endOf) {
    const made = opts.endOf(t.ships);
    if (made !== undefined && made !== t.end.user) {
      problem = `${name(t.ships)} ends at ${endSeconds(made ?? t.end.auto ?? t.end.full)} s`;
    }
  }
  return {
    badge,
    badgeTitle,
    making: makingId != null,
    makingTitle: makingId != null ? `Making ${name(makingId)}…` : null,
    problem,
  };
}

// ---------------------------------------------------------------------------
// The Finalize button
// ---------------------------------------------------------------------------

export type FinalizeKind = "making" | "rendering" | "finalize" | "see";

export interface FinalizeState {
  kind: FinalizeKind;
  label: string;
  enabled: boolean;
}

/** `making`: the label of a scene with a version really being made (it
 *  would ship the older pick without saying so); `rendering`: the reel is
 *  being made; `unchanged`: finalized and nothing that goes in changed
 *  since (the good reel is served, no error). */
export function finalizeState(o: { making: string | null; rendering: boolean; unchanged: boolean }): FinalizeState {
  if (o.making) return { kind: "making", label: `Making ${o.making}…`, enabled: false };
  if (o.rendering) return { kind: "rendering", label: "Rendering…", enabled: false };
  if (o.unchanged) return { kind: "see", label: "See final →", enabled: true };
  return { kind: "finalize", label: "Finalize →", enabled: true };
}

/** Finalized and unchanged: a reel of this round (a good seq; a legacy reel
 *  is always finalized again), ready with no error, served, not stale. */
export function reelUnchanged(r: {
  composition_status: string | null | undefined;
  composition_good_seq?: number | null;
  composition_error?: string | null;
  composition_url: string | null | undefined;
  composition_stale?: boolean;
}): boolean {
  return (
    r.composition_status === "ready" &&
    r.composition_good_seq != null &&
    !r.composition_error &&
    !!r.composition_url &&
    !r.composition_stale
  );
}

/** A reel block from a PUT (or a poll), laid on the run as the page knows
 *  it. The server says whether the reel is out of date only with a whole
 *  run (composition_stale, for the reel it read); a pick or an end changes
 *  the inputs after that, so the reel is out of date exactly when the new
 *  inputs are not the good reel's. `good` holds the inputs last known to be
 *  the good reel's (updated here from the run before the change). */
export function withReel<R extends {
  reel?: ReelBlock | null;
  composition_stale?: boolean;
  composition_good_seq?: number | null;
}>(prev: R, block: ReelBlock, good: { sha: string | null }): R {
  if (prev.composition_good_seq != null && prev.reel && !prev.composition_stale) good.sha = prev.reel.inputs_sha;
  const stale = prev.composition_good_seq == null
    ? !!prev.composition_stale
    : good.sha
      ? block.inputs_sha !== good.sha
      : prev.reel?.inputs_sha === block.inputs_sha
        ? !!prev.composition_stale
        : true;
  return { ...prev, reel: block, composition_stale: stale };
}
