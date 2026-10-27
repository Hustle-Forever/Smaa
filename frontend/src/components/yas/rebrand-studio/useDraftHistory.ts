// Rebrand studio — the draft recipe, with undo and redo.
//
// The draft itself is kept per scene in this browser (the same key the first
// studio used), so closing the window loses nothing. The history is for this
// session only: after a reload the draft is there, its undo steps are not.
//
// The version the draft is built on (`base`: Add to it / Change it, or null)
// travels with it: it is kept per scene beside the draft, and every step
// holds both, so an undo of "Change it" gives back the draft AND the mode it
// was made in. Storage can come back empty or with an older shape: a base
// that isn't one reads as null (a fresh draft), never as an error.
//
// Typing is one step, not one per key: an edit tagged `coalesce` within 1.5 s
// of the last edit with the same tag replaces the step instead of adding one.
//
// The stacks live beside the draft for the page session, not in the
// component: the Rebrand stage mounts the studio afresh for every scene it
// opens, and switching scenes and back keeps each scene's undo. The same
// place remembers which drafts were edited during this visit (the stage's
// "edits not generated" line counts only those).
//
// The scene's end (the end grip; `end`, when the studio has one) is on the
// same stack: every step holds the end as it was, a move of the end is a
// step of its own (keyboard steps join, as typing does), and undo or redo
// puts the end back through `end.set` when a step's end is not the one
// now — so Ctrl+Z right after a trim undoes the trim. Moving the end is not
// an edit of the draft (it never counts as "edits not generated").
//
// And the icons seen in Add icon → Suggested while a draft was made (glyph
// ids): its Generate says so (seen_ideas), and they start again after it.

import { useCallback, useMemo, useRef, useState } from "react";

import { asBaseRef, recipeKey, sameBase, type HarnessBaseRef, type HarnessRecipe } from "../../../lib/rebrandHarnessApi";
import { usePersistentState } from "../../../lib/usePersistentState";

const STEPS = 50;
const COALESCE_MS = 1500;

interface Step {
  draft: HarnessRecipe;
  base: HarnessBaseRef | null;
  /** The scene's end then (30 fps frames; null: automatic). Absent: the
   *  studio had no end grip. */
  end?: number | null;
}

/** The scene's end, as the studio holds it (the stage's queue sends it). */
export interface EndIo {
  now: () => number | null;
  set: (frame: number | null, nudge: boolean) => void;
}

/** What an undo or redo moved: the draft (or its base), or only the end. */
export type Undone = "draft" | "end";

interface Stacks {
  past: Step[];
  future: Step[];
  last: { tag: string | null; at: number };
}

/** Each draft's undo and redo, by its storage key, for the page session. */
const STACKS = new Map<string, Stacks>();
/** The drafts edited during this visit (a reload starts none). */
const EDITED = new Set<string>();
/** The ideas seen in Suggested while each draft was made (glyph ids). */
const SEEN = new Map<string, Set<string>>();
/** The most a Generate says it saw (the server keeps 60). */
const SEEN_MAX = 60;

function stacksOf(key: string): Stacks {
  let s = STACKS.get(key);
  if (!s) {
    s = { past: [], future: [], last: { tag: null, at: 0 } };
    STACKS.set(key, s);
  }
  return s;
}

/** Where a studio scene's draft is kept (and what the edited-this-visit
 *  marks name). */
export function studioDraftKey(runId: number, target: string): string {
  return `rebrand.studio.draft.${runId}.${target}`;
}

/** Whether the draft kept under `storageKey` was edited during this visit. */
export function editedThisVisit(storageKey: string): boolean {
  return EDITED.has(storageKey);
}

/** Forget that these drafts were edited (the stage's "Finalize anyway"). */
export function clearEdited(storageKeys: Iterable<string>): void {
  for (const k of storageKeys) EDITED.delete(k);
}

/** Ideas seen in Suggested while the draft kept under `storageKey` was made. */
export function noteSeenIdeas(storageKey: string, glyphIds: Iterable<string>): void {
  const seen = SEEN.get(storageKey) ?? new Set<string>();
  for (const id of glyphIds) if (id && seen.size < SEEN_MAX) seen.add(id);
  SEEN.set(storageKey, seen);
}

/** … as its Generate sends them (none: an empty list). */
export function seenIdeasOf(storageKey: string): string[] {
  return [...(SEEN.get(storageKey) ?? [])];
}

/** A Generate went: the next draft's ideas start afresh. */
export function clearSeenIdeas(storageKey: string): void {
  SEEN.delete(storageKey);
}

export function useDraftHistory(storageKey: string, baseKey: string, end: EndIo | null = null) {
  const [draft, setDraft] = usePersistentState<HarnessRecipe | null>(storageKey, null);
  const [storedBase, setStoredBase] = usePersistentState<unknown>(baseKey, null);
  const base = useMemo(() => asBaseRef(storedBase), [storedBase]);
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const baseRef = useRef(base);
  baseRef.current = base;
  const endRef = useRef(end);
  endRef.current = end;
  // This draft's undo and redo: the session's, so a remount (another scene
  // and back) finds them as they were.
  const stacks = stacksOf(storageKey);
  // The stacks change only in event handlers; this re-renders.
  const [, bump] = useState(0);

  const commit = useCallback(
    (next: Step) => {
      draftRef.current = next.draft;
      baseRef.current = next.base;
      setDraft(next.draft);
      setStoredBase(next.base);
      // A step of the end (undo, redo): the end it held goes back.
      const io = endRef.current;
      if (io && next.end !== undefined && next.end !== io.now()) io.set(next.end, false);
      bump((n) => n + 1);
    },
    [setDraft, setStoredBase],
  );

  const now = (): Step | null => {
    if (!draftRef.current) return null;
    const io = endRef.current;
    return { draft: draftRef.current, base: baseRef.current, ...(io ? { end: io.now() } : {}) };
  };
  /** What moves between two steps: the draft (or its base), or the end. */
  const movedOf = (a: Step, b: Step): Undone =>
    recipeKey(a.draft) !== recipeKey(b.draft) || !sameBase(a.base, b.base) ? "draft" : "end";

  /** A new draft (and, when `base` is given, a new base — null leaves the
   *  version), as one undo step. Returns false when nothing changed. */
  const set = useCallback(
    (next: HarnessRecipe, opts?: { coalesce?: string; base?: HarnessBaseRef | null }): boolean => {
      const cur = now();
      const nextBase = opts?.base !== undefined ? opts.base : baseRef.current;
      const sameDraft = !!cur && recipeKey(cur.draft) === recipeKey(next);
      const moved = !sameBase(nextBase, baseRef.current);
      if (sameDraft && !moved) return false;
      const at = Date.now();
      const tag = opts?.coalesce ?? null;
      const joins = !moved && tag !== null && stacks.last.tag === tag && at - stacks.last.at < COALESCE_MS;
      if (cur && !joins) stacks.past = [...stacks.past, cur].slice(-STEPS);
      stacks.future = [];
      stacks.last = { tag: moved ? null : tag, at };
      EDITED.add(storageKey);
      commit({ draft: next, base: nextBase });
      return true;
    },
    [commit, stacks, storageKey],
  );

  /** The scene's end moved (null: automatic), as one undo step — steps
   *  tagged `coalesce` within 1.5 s join. Returns false when it didn't. */
  const setEnd = useCallback(
    (frame: number | null, opts?: { coalesce?: string }): boolean => {
      const io = endRef.current;
      const cur = now();
      if (!io || !cur || io.now() === frame) return false;
      const at = Date.now();
      const tag = opts?.coalesce ?? null;
      const joins = tag !== null && stacks.last.tag === tag && at - stacks.last.at < COALESCE_MS;
      if (!joins) stacks.past = [...stacks.past, cur].slice(-STEPS);
      stacks.future = [];
      stacks.last = { tag, at };
      io.set(frame, tag !== null);
      bump((n) => n + 1);
      return true;
    },
    [stacks],
  );

  /** Replace the draft (and the base: none unless given) with no undo step —
   *  the scene's first load. */
  const reset = useCallback(
    (next: HarnessRecipe, nextBase: HarnessBaseRef | null = null) => {
      stacks.past = [];
      stacks.future = [];
      stacks.last = { tag: null, at: 0 };
      commit({ draft: next, base: nextBase });
    },
    [commit, stacks],
  );

  /** Drop version `versionId` as a base, with no undo step: it is gone (or
   *  can never be built on). The steps built on it lose it too — an undo
   *  back onto it would only be dropped again — but steps built on any
   *  other version keep theirs. */
  const forgetBase = useCallback(
    (versionId: number) => {
      const gone = (b: HarnessBaseRef | null) => !!b && b.version_id === versionId;
      const drop = (s: Step) => (gone(s.base) ? { ...s, base: null } : s);
      stacks.past = stacks.past.map(drop);
      stacks.future = stacks.future.map(drop);
      if (gone(baseRef.current)) {
        baseRef.current = null;
        setStoredBase(null);
      }
      bump((n) => n + 1);
    },
    [setStoredBase, stacks],
  );

  const undo = useCallback((): Undone | null => {
    const prev = stacks.past[stacks.past.length - 1];
    if (!prev) return null;
    stacks.past = stacks.past.slice(0, -1);
    const cur = now();
    if (cur) stacks.future = [cur, ...stacks.future].slice(0, STEPS);
    stacks.last = { tag: null, at: 0 };
    const moved = cur ? movedOf(cur, prev) : "draft";
    if (moved === "draft") EDITED.add(storageKey);
    commit(prev);
    return moved;
  }, [commit, stacks, storageKey]);

  const redo = useCallback((): Undone | null => {
    const next = stacks.future[0];
    if (!next) return null;
    stacks.future = stacks.future.slice(1);
    const cur = now();
    if (cur) stacks.past = [...stacks.past, cur].slice(-STEPS);
    stacks.last = { tag: null, at: 0 };
    const moved = cur ? movedOf(cur, next) : "draft";
    if (moved === "draft") EDITED.add(storageKey);
    commit(next);
    return moved;
  }, [commit, stacks, storageKey]);

  return {
    draft,
    base,
    set,
    setEnd,
    reset,
    forgetBase,
    undo,
    redo,
    canUndo: stacks.past.length > 0,
    canRedo: stacks.future.length > 0,
  };
}
