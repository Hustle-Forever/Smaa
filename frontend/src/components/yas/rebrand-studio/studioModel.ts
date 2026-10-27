// Rebrand studio — the shell's own rules, as pure functions.

import type { RebrandArm, RebrandPattern } from "../../../lib/rebrandApi";
import { recordedOf } from "../../../lib/rebrandBuildOn";
import {
  recipeOfVersion,
  type HarnessCatalogue,
  type HarnessKind,
  type HarnessLayer,
  type HarnessPatternLayout,
  type HarnessRecipe,
  type HarnessScene,
  type HarnessVersion,
} from "../../../lib/rebrandHarnessApi";
import { isHandPlaced } from "../../../lib/rebrandHand";
import { colourOf, enabledPatterns, entryOf, slugOf } from "../../../lib/rebrandRecipe";
import { anchorLabel, COPY, layoutLabel } from "./studioCopy";

/** What the one canvas shows. There is never a second one. */
export type StageSel = { kind: "original" } | { kind: "version"; id: number } | { kind: "next" };

export function sameSel(a: StageSel, b: StageSel): boolean {
  return a.kind === b.kind && (a.kind !== "version" || (b.kind === "version" && a.id === b.id));
}

export type VersionPhase = "queued" | "running" | "ready" | "failed" | "stopped";

export function versionPhase(v: HarnessVersion): VersionPhase {
  switch (v.status) {
    case "queued":
      return "queued";
    case "briefing":
    case "running":
      return "running";
    case "ready":
      return "ready";
    case "failed":
      return "failed";
    default:
      return "stopped"; // cancelled, interrupted
  }
}

export function omniArm(v: HarnessVersion): RebrandArm | undefined {
  return v.arms?.omni;
}

/** The version was made in house (our own graphics over the untouched
 *  footage, $0) — not by Omni. Its arm keeps the key "omni". */
export function madeInHouse(v: HarnessVersion | null | undefined): boolean {
  if (!v) return false;
  const opts = (v.options ?? {}) as Record<string, unknown>;
  return recipeOfVersion(v)?.engine === "house" || omniArm(v)?.engine === "house" || opts.engine === "house";
}

/** When the version was asked for, never in the future (clock skew). */
export function startedAtOf(v: HarnessVersion): number {
  const t = v.created_at ? Date.parse(v.created_at) : NaN;
  return Number.isFinite(t) ? Math.min(t, Date.now()) : Date.now();
}

/** The version's still for a part, when the server has made it. */
export function stillOf(v: HarnessVersion, part: number): string | null {
  return v.stills?.[part] ?? null;
}

/** "on v2" / "from v2": what a version was built on, and how. */
export function lineageOf(v: HarnessVersion, versions: HarnessVersion[]): string | null {
  if (!v.base) return null;
  const at = versions.findIndex((x) => x.id === v.base!.version_id);
  return COPY.lineage(v.base.mode, at >= 0 ? at + 1 : null);
}

/** One layer of the version a draft is added on top of, as the rail lists
 *  it: read-only, where its run put it, in the colour it was drawn. */
export interface KeptRow {
  key: string;
  name: string;
  entry: RebrandPattern | undefined;
  where: string;
  colour: string | null;
  /** The version that drew it, when that is not the one built on (it was
   *  added on top of another). */
  fromNo: number | null;
}

export function keptRows(
  layers: HarnessLayer[],
  versions: HarnessVersion[],
  baseId: number,
  ctx: { catalogue: HarnessCatalogue | null; introColour: string | null },
): KeptRow[] {
  return layers
    .filter((l) => l.enabled && l.kind === "pattern")
    .map((l, i) => {
      const entry = entryOf(ctx.catalogue, slugOf(l));
      const rec = recordedOf(l, versions);
      const at = versions.findIndex((x) => x.id === l.from_version);
      const recipe = at >= 0 ? recipeOfVersion(versions[at]) : null;
      return {
        key: `${l.from_version}:${l.id}:${i}`,
        name: entry?.name ?? rec?.name ?? slugOf(l),
        entry,
        where:
          (layoutLabel(rec?.layout ?? (l.props.layout as HarnessPatternLayout | null)) ??
            anchorLabel(String(l.placement.anchor))) + (isHandPlaced(l) ? ` · ${COPY.hand.byHandShort}` : ""),
        colour: rec?.colour ?? (recipe ? colourOf(l, recipe, ctx) : null),
        fromNo: l.from_version !== baseId && at >= 0 ? at + 1 : null,
      };
    });
}

/** What Generate costs: Omni's ~$0.10 a second over the parts it would
 *  redo (the server's `omni_parts`; every part when it doesn't say), to the
 *  nearest 10 cents, and its usual ~3 minutes. */
export function costOf(scene: HarnessScene, omniParts?: number[] | null): { minutes: number; dollars: number } {
  const parts = omniParts ?? scene.chunks.map((c) => c.index);
  const seconds = parts.reduce((sum, j) => sum + (scene.chunks.find((c) => c.index === j)?.seconds ?? 0), 0);
  return { minutes: 3, dollars: Math.round(seconds) / 10 };
}

// --- the rail's tabs -----------------------------------------------------------

export type RailTabId = "patterns" | "icons" | "style";

export interface RailTabDef {
  id: RailTabId;
  /** The tab shows only when the server can draw every one of these. */
  kinds: HarnessKind[];
  count?: (r: HarnessRecipe) => number;
}

/** In order. Icons joins when the server says icons are ready AND the
 *  studio has an Icons tab to show (`built`). */
export const RAIL_TABS: RailTabDef[] = [
  { id: "patterns", kinds: ["pattern"], count: (r) => enabledPatterns(r).length },
  { id: "icons", kinds: ["icon"], count: (r) => r.elements.filter((e) => e.kind === "icon" && e.enabled).length },
  { id: "style", kinds: [] },
];

export function visibleTabs(catalogue: HarnessCatalogue | null, built: ReadonlySet<RailTabId>): RailTabDef[] {
  return RAIL_TABS.filter(
    (t) =>
      built.has(t.id) &&
      // Without a catalogue, patterns are still editable (they always were).
      t.kinds.every((k) => catalogue?.kinds.find((x) => x.kind === k)?.ready ?? k === "pattern"),
  );
}

/** Typing in the note is one undo step, not one per key. */
export const COALESCE_NOTES = "notes";
