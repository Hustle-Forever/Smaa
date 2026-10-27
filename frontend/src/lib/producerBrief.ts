/**
 * Trend Finder → Producer creator hand-off ("Make this").
 *
 * The hash grammar has exactly two segments (App.routeParamFromHash reads only
 * hash.split("/")[1], and parseProducerFreshTool demands an exact tool key), so
 * the AI-generated brief rides a module-level slot, not the URL — the same
 * mechanism components/trends/bus.ts uses for Themes → Feed.
 *
 * Delivery has two paths, and exactly ONE of them fires per hand-off:
 *
 *   1. REMOUNT (the normal case). `sendProducerBrief` sets the slot, then sets
 *      the hash to "#producer/<tool>". App re-renders, YasProducer's
 *      key={initialFreshTool} changes, ProducerRoadmap REMOUNTS with
 *      initialTool === brief.tool and drains the slot in its useState
 *      initializer — i.e. before its creator children mount.
 *   2. EVENT (already on that exact tool). The hash doesn't change, so nothing
 *      remounts; the mounted ProducerRoadmap picks the brief up from the window
 *      event and bumps projectNonce itself to remount the creator.
 *
 * The listener rule that keeps these from colliding: a ProducerRoadmap instance
 * handles the event ONLY when its own `initialTool === brief.tool`. When a
 * hand-off is about to remount the page (case 1) the currently-mounted instance
 * has a DIFFERENT initialTool (usually null, because App only computes
 * initialFreshTool while tab === "producer"), so it ignores the event and never
 * steals the slot from the instance that is about to mount.
 */
import { producerToolHash } from "./producerHash";
import type { RoadmapTool } from "../components/yas/roadmap/types";

export const PRODUCER_BRIEF_EVENT = "producer:brief";

/** A brief older than this is stale (the user navigated away and came back) and
 *  is ignored rather than re-seeding a creator out of nowhere. */
const BRIEF_TTL_MS = 120_000;

/**
 * One AI-generated prefill, already shaped for the destination tool. Every field
 * is ALWAYS present — "" / [] when the tool doesn't use it — so consumers never
 * branch on undefined. Mirrors the backend `prefill` envelope 1:1 (camelCased
 * for the two-word keys).
 */
export interface ProducerBrief {
  /** Monotonic per page-load. A consumer applies each id at most once. */
  id: number;
  /** Wall-clock stamp, for the TTL. */
  at: number;
  /** Destination creator. Also decides which "#producer/<tool>" hash is set. */
  tool: RoadmapTool;
  /** Short name for the idea (<=80 chars). The ONLY field video_editor can use
   *  (it becomes the CreateProjectGate project name). */
  title: string;
  /** The main brief for the tool's primary text box. Always non-empty. */
  topic: string;
  /** talking_head_v2 ONLY: one spoken line per scene, in order, 1..3 entries.
   *  [] for every other tool. */
  dialogue: string[];
  /** broll ONLY: the animated intro / outro card lines. "" otherwise. */
  introTopic: string;
  outroTopic: string;
  /** human_character ONLY: a CASTING brief describing one person (not a post
   *  topic — that box expects a person). "" otherwise. */
  character: string;
  /** One short sentence on why this idea works. May be "". Toast copy only. */
  notes: string;
  /** Where it came from, for the toast / provenance. */
  postId: number;
  permalink: string | null;
}

let pending: ProducerBrief | null = null;
let nextId = 1;

/** Stash a brief and navigate to its creator. Returns the stamped brief. */
export function sendProducerBrief(
  brief: Omit<ProducerBrief, "id" | "at">,
): ProducerBrief {
  const stamped: ProducerBrief = { ...brief, id: nextId++, at: Date.now() };
  pending = stamped;
  // Navigate FIRST: when the Producer page is not already on this tool, the
  // hash change is what remounts ProducerRoadmap, and its mount-drain is the
  // delivery. The event below only matters when the hash is already correct.
  const next = producerToolHash(stamped.tool);
  if (window.location.hash !== next) window.location.hash = next;
  window.dispatchEvent(
    new CustomEvent<ProducerBrief>(PRODUCER_BRIEF_EVENT, { detail: stamped }),
  );
  return stamped;
}

/** Read WITHOUT clearing. Returns null once the brief is past its TTL. */
export function peekProducerBrief(): ProducerBrief | null {
  if (pending && Date.now() - pending.at > BRIEF_TTL_MS) pending = null;
  return pending;
}

/** Clear the slot, but only if it still holds THAT brief. Called by the
 *  consumer once it has actually applied it. */
export function clearProducerBrief(id: number): void {
  if (pending && pending.id === id) pending = null;
}
