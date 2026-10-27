/**
 * Single source of truth for the Producer / Library URL-hash grammar.
 *
 * The builders (producer*Hash / libraryItemHash) and the parsers
 * (parseProducer* / libraryOpenTarget) live together here so the two can never
 * drift — the class of bug that has broken deep-links before. The hash *router*
 * itself (tab parsing, navigate()) stays in App.tsx; this module owns only the
 * `#producer/…` and `#library/…` param grammar.
 */
import { ROADMAP_TOOL_LABELS, type RoadmapTool } from "../components/yas/roadmap/types";
import type { YasLibraryType } from "./types";

/** Library tool types a resume/open deep-link may name. The `<type>` group in a
 *  hash is validated against this set so a hand-crafted/bogus hash can't open
 *  the wrong creator. Poster / animation / poster_human ids live in separate
 *  id-spaces, so the type is what disambiguates them. */
export const KNOWN_LIBRARY_TYPES: readonly string[] = [
  "talking_head",
  "talking_head_v2",
  "poster",
  "poster_human",
  "animation",
  "video_editor",
  "general_video",
  "broll",
];

/** The closed set of Producer tool keys, derived from the labels map so a new
 *  tool automatically becomes deep-linkable. */
export const ROADMAP_TOOLS = Object.keys(ROADMAP_TOOL_LABELS) as RoadmapTool[];

// ---------------------------------------------------------------------------
// Builders — the only place a #producer / #library hash string is constructed.
// ---------------------------------------------------------------------------

/** The bare Producer hub. */
export const PRODUCER_HUB_HASH = "#producer";
/** "#producer/<tool>" — open a creator fresh (no run id). */
export const producerToolHash = (tool: RoadmapTool) => `#producer/${tool}`;
/** "#producer/<type>:<id>" — resume a run in its creator. Two additive forms
 *  ride inside the SAME colon param (App.tsx reads only hash.split("/")[1], so a
 *  step/id-list may never be a 3rd "/" segment):
 *    - a WIZARD sub-step:  pass `step` → "#producer/<type>:<id>@<step>"
 *    - a BATCH grid:       pass an id array → "#producer/<type>:<id1>+<id2>+…"
 *  Single number + no step keeps the exact legacy string (back-compat). */
export const producerResumeHash = (
  type: string,
  idOrIds: number | number[],
  step?: string,
) => {
  const ids = Array.isArray(idOrIds) ? idOrIds.join("+") : String(idOrIds);
  const suffix = step ? `@${step}` : "";
  return `#producer/${type}:${ids}${suffix}`;
};
/** "#library/<type>:<id>" — open that item's detail modal. */
export const libraryItemHash = (type: string, id: number) =>
  `#library/${type}:${id}`;

/** Replace the current URL hash WITHOUT a history push or a hashchange event —
 *  used to mirror in-app state to the URL (F5 / copy-URL safe). Best-effort:
 *  swallows the rare replaceState SecurityError so callers stay simple. */
export function replaceHash(hash: string) {
  try {
    history.replaceState(null, "", hash);
  } catch {
    /* ignore */
  }
}

// ---------------------------------------------------------------------------
// Parsers — kept beside the builders so the grammar has one owner.
// ---------------------------------------------------------------------------

/** Parse a Producer resume target from a hash route segment. The form
 *  "<type>:<ids>[@<step>]" carries the Library type (unambiguous across
 *  id-spaces); legacy "<id>" resolves the type via cache. The `<ids>` group is
 *  one id or a "+"-joined batch (Poster / Animation / Poster+Human grids); the
 *  optional "@<step>" pins a wizard sub-step (Human Element Video, B-roll). The type
 *  group is validated against the known set so a bogus hash can't open the wrong
 *  creator. Returns `id` (== ids[0]) + `type` — the keys every existing caller
 *  reads — plus the additive `ids` + `step`. */
export function parseProducerResume(
  param: string | null,
): { id: number; ids: number[]; type: YasLibraryType | null; step: string | null } | null {
  if (!param) return null;
  const m = param.match(/^([a-z0-9_]+):(\d+(?:\+\d+)*)(?:@([a-z0-9_]+))?$/);
  if (m) {
    if (!KNOWN_LIBRARY_TYPES.includes(m[1])) return null;
    const ids = m[2].split("+").map(Number);
    return { type: m[1] as YasLibraryType, id: ids[0], ids, step: m[3] ?? null };
  }
  if (/^\d+$/.test(param)) {
    const id = Number(param);
    return { id, ids: [id], type: null, step: null };
  }
  return null;
}

/** Parse a "fresh tool" Producer deep-link. A bare tool key ("#producer/poster")
 *  opens that creator BLANK — distinct from the resume form "<type>:<id>": the
 *  key ALONE means "start fresh", the same key with ":<id>" means "resume run
 *  <id>" (so general_video / broll — both a tool AND a Library type —
 *  disambiguate by the colon). Validated against the closed RoadmapTool set →
 *  an unknown token falls through to the hub grid. */
export function parseProducerFreshTool(param: string | null): RoadmapTool | null {
  if (!param) return null;
  return (ROADMAP_TOOLS as string[]).includes(param)
    ? (param as RoadmapTool)
    : null;
}

/** Library deep-link target — "<type>:<id>" opens that item's detail modal
 *  directly (e.g. the Dashboard "Recent work" tiles). Requires an explicit type
 *  so it never collides with the "scheduled" sub-tab alias. */
export function libraryOpenTarget(
  param: string | null,
): { id: number; type: YasLibraryType } | null {
  const t = parseProducerResume(param);
  return t && t.type ? { id: t.id, type: t.type } : null;
}
