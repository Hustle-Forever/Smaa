// Rebrand studio — this scene's versions (v1, v2, … oldest first), polled
// while one is on its way, plus Generate and Stop.
//
// Polling asks the harness list, not each experiment: one request, and the
// list is what carries each version's recipe, its lineage (the version it was
// built on, every layer it shows) and, once it is ready, its per-part stills.
// An older server without that list falls back to polling the experiments
// one by one.

import { useCallback, useEffect, useState } from "react";

import { rebrandApi, rebrandErrorDetail, REBRAND_ACTIVE } from "../../../lib/rebrandApi";
import {
  harnessApi,
  iconConflictOf,
  recipeOfVersion,
  type HarnessBaseRef,
  type HarnessRecipe,
  type HarnessVersion,
  type SceneView,
} from "../../../lib/rebrandHarnessApi";

const POLL_MS = 4000;

export function isActive(v: HarnessVersion): boolean {
  return REBRAND_ACTIVE.includes(v.status);
}

/** What only the harness list knows about a version (a bare experiment,
 *  polled one by one, doesn't carry it). */
function known(old: HarnessVersion) {
  return { recipe: recipeOfVersion(old), stills: old.stills, base: old.base, layers: old.layers, depth: old.depth };
}

/** Fresh rows win, but never lose what only the older row knew. */
function merge(prev: HarnessVersion[], fresh: HarnessVersion[]): HarnessVersion[] {
  const byId = new Map(prev.map((v) => [v.id, v]));
  for (const f of fresh) {
    const old = byId.get(f.id);
    byId.set(
      f.id,
      old
        ? {
            ...old,
            ...f,
            recipe: recipeOfVersion(f) ?? recipeOfVersion(old),
            stills: f.stills ?? old.stills,
            base: f.base !== undefined ? f.base : old.base,
            layers: f.layers ?? old.layers,
            depth: f.depth ?? old.depth,
          }
        : f,
    );
  }
  return [...byId.values()].sort((a, b) => a.id - b.id);
}

export function useStudioVersions(runId: number, target: string, initial: HarnessVersion[] | null | undefined) {
  const [versions, setVersions] = useState<HarnessVersion[]>([]);
  const [posting, setPosting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (initial) setVersions((prev) => merge(prev, initial));
  }, [initial]);

  const activeKey = versions
    .filter(isActive)
    .map((v) => v.id)
    .join(",");
  useEffect(() => {
    if (!activeKey) return;
    let stopped = false;
    const tick = async () => {
      try {
        const { versions: fresh } = await harnessApi.versions(runId, target);
        if (!stopped) setVersions((prev) => merge(prev, fresh));
      } catch {
        const ids = activeKey.split(",").map(Number);
        const fresh = await Promise.all(ids.map((id) => rebrandApi.get(id).catch(() => null)));
        if (stopped) return;
        setVersions((prev) =>
          prev.map((v) => {
            const f = fresh.find((x) => x?.id === v.id);
            return f ? { ...v, ...f, ...known(v) } : v;
          }),
        );
      }
    };
    const id = window.setInterval(() => void tick(), POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(id);
    };
  }, [activeKey, runId, target]);

  /** A new version from `recipe` — built on `base` when given (`fresh`: a
   *  new take, nothing reused; `iconsPlanKey`: the icon plan the studio
   *  showed; `view`: made to the scene's end set by hand); null when the
   *  server refused (error set) — or when the icons moved on since they were
   *  shown: then `onIconConflict` gets the fresh plan instead of an error. */
  const generate = useCallback(
    async (
      recipe: HarnessRecipe,
      opts: {
        base?: HarnessBaseRef | null;
        fresh?: boolean;
        iconsPlanKey?: string | null;
        view?: SceneView | null;
        seenIdeas?: readonly string[] | null;
        onIconConflict?: (c: NonNullable<ReturnType<typeof iconConflictOf>>) => void;
      } = {},
    ): Promise<HarnessVersion | null> => {
      setPosting(true);
      setError(null);
      try {
        const v = await harnessApi.generate(runId, target, recipe, opts);
        const row = { ...v, recipe: recipeOfVersion(v) ?? recipe, base: v.base !== undefined ? v.base : opts.base ?? null };
        setVersions((prev) => merge(prev, [row]));
        return row;
      } catch (e) {
        const conflict = iconConflictOf(e);
        if (conflict && opts.onIconConflict) opts.onIconConflict(conflict);
        else setError(rebrandErrorDetail(e));
        return null;
      } finally {
        setPosting(false);
      }
    },
    [runId, target],
  );

  const cancel = useCallback(async (v: HarnessVersion) => {
    try {
      const f = await rebrandApi.cancel(v.id);
      setVersions((prev) => prev.map((x) => (x.id === v.id ? { ...x, ...f, ...known(x) } : x)));
    } catch (e) {
      setError(rebrandErrorDetail(e));
    }
  }, []);

  const clearError = useCallback(() => setError(null), []);

  /** A version the list doesn't reach (it holds this scene's latest 20),
   *  fetched by id — the one a draft is built on — added to it. */
  const adopt = useCallback((v: HarnessVersion) => setVersions((prev) => merge(prev, [v])), []);

  return {
    versions,
    active: versions.find(isActive) ?? null,
    posting,
    error,
    clearError,
    generate,
    cancel,
    adopt,
  };
}
