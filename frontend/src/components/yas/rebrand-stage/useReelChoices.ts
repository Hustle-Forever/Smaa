// The Rebrand stage's hold on what goes in the reel, scene by scene.
//
//   pick(target, id|null)   the clicked version (or the original) goes in:
//                           the tile and the ✓ move at once, and a PUT goes
//                           out at once — one at a time per scene, the last
//                           one wins. A PUT refused puts back what the server
//                           has (GET …/reel) and says why.
//   flush()                 waits for every PUT: Finalize and a tile switch
//                           go only after, so a pick made just before always
//                           ships, with the inputs hash the server returned.
//   noteVersions(t, list)   the studio's versions changed: a version on its
//                           way shows as being made at once (before the
//                           server's reel says so), and the reel is read
//                           again; one that just finished moves the ✓ now.
//
// The reel is read again when the stage mounts, and whenever the stage
// asks (a scene got ready: preparing it may have fetched a clip, which can
// change what goes in, and so the inputs hash Finalize sends).
//
// While a version is being made (the server's reel says so, or one just
// started here) the reel is read every 4 s. It all lives in the stage,
// which stays mounted across tile switches; a PUT still in flight when the
// stage goes lands on the run all the same, and polling stops with it.
//
// End changes (the timeline's grip) take the same queue: `end(t, frame)`
// moves the tile and the grip at once, and its PUT goes out at once too —
// keyboard nudges are joined for 300 ms first (the last one wins). A main
// scene's start (the left grip) is the same: `start(t, frame)`. A move of
// one sends the other's nudge still waiting first, so the server always
// gets them in the order they were made (it keeps a second between them).

import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../../../lib/api";
import { withReel } from "../../../lib/rebrandStage";
import { versionEndOf, type HarnessVersion } from "../../../lib/rebrandHarnessApi";
import type { ReelBlock, ReelChoicePatch, ReelTarget, TalkingHeadRun } from "../../../lib/types";
import { isActive } from "../rebrand-studio/useStudioVersions";

const POLL_MS = 4000;
const NUDGE_MS = 300;

type Overlay = Partial<Pick<ReelTarget, "ships" | "pick" | "making" | "end">>;

/** Each scene's version numbers (v1 … vN, oldest first), as the studio last
 *  listed them: the tiles' titles say "v2" (kept for the page session). */
const NUMBERS = new Map<string, Map<number, number>>();
/** … and the end each was made for (null: automatic): a tile whose version
 *  was made for another end than the scene's says so. */
const ENDS = new Map<string, Map<number, number | null>>();

function laid(block: ReelBlock, overlays: Map<string, Overlay>): ReelBlock {
  if (!overlays.size) return block;
  return { ...block, targets: block.targets.map((t) => (overlays.has(t.target) ? { ...t, ...overlays.get(t.target) } : t)) };
}

export function useReelChoices({
  run,
  setRun,
  onRefused,
}: {
  run: TalkingHeadRun;
  setRun: React.Dispatch<React.SetStateAction<TalkingHeadRun | null>>;
  /** A PUT was refused: the server's words. */
  onRefused: (message: string) => void;
}) {
  const runId = run.id;
  const runRef = useRef(run);
  runRef.current = run;
  // The inputs last known to be the good reel's (withReel keeps it).
  const good = useRef<{ sha: string | null }>({ sha: null });
  const overlays = useRef(new Map<string, Overlay>());
  const queues = useRef(new Map<string, { running: Promise<void> | null; next: ReelChoicePatch | null }>());
  const nudges = useRef(new Map<string, { timer: number; go: () => void }>());
  const refusedRef = useRef(onRefused);
  refusedRef.current = onRefused;
  // Versions this page just saw start, per scene, until the server's reel
  // has been read since (it then says so itself).
  const [started, setStarted] = useState<Record<string, { id: number; at: number }>>({});
  const seen = useRef(new Map<string, Set<number>>());
  const wasActive = useRef(new Map<string, boolean>());
  // Bumped when a scene's version numbers change: the tiles' titles follow.
  const [, setNumbered] = useState(0);

  // The inputs_sha of the last block the server returned.
  const lastSha = useRef<string | null>(null);
  const apply = useCallback(
    (block: ReelBlock) => {
      lastSha.current = block.inputs_sha;
      const shown = laid(block, overlays.current);
      setRun((prev) => (prev && prev.id === runId ? withReel(prev, shown, good.current) : prev));
    },
    [runId, setRun],
  );

  // The read on its way, if any (Finalize waits for it: its hash is the
  // one to send).
  const reading = useRef<Promise<void> | null>(null);
  /** Read the reel again (the server's word, what a PUT's rollback is). */
  const refresh = useCallback((): Promise<void> => {
    const asked = Date.now();
    const read: Promise<void> = api.yasProducer.talkingHeadV2
      .reel(runId)
      .then((block) => {
        apply(block);
        setStarted((s) => {
          const keep = Object.entries(s).filter(([, v]) => v.at >= asked);
          return keep.length === Object.keys(s).length ? s : Object.fromEntries(keep);
        });
      })
      .catch(() => {
        // a failed read keeps the tiles; the next one tries again
      })
      .finally(() => {
        if (reading.current === read) reading.current = null;
      });
    reading.current = read;
    return read;
  }, [runId, apply]);

  // Once on mount: what the page holds may be older than what goes in now
  // (a clip fetched since, a choice made in another tab).
  useEffect(() => {
    void refresh();
  }, [refresh]);

  const drain = useCallback(
    async (target: string) => {
      const q = queues.current.get(target);
      if (!q) return;
      while (q.next) {
        const patch = q.next;
        q.next = null;
        try {
          const block = await api.yasProducer.talkingHeadV2.setReel(runId, target, patch);
          if (!q.next) {
            overlays.current.delete(target);
            apply(block);
          }
        } catch (e: any) {
          refusedRef.current(String(e?.message ?? e ?? "").replace(/^\d{3}\s+/, ""));
          if (!q.next) {
            overlays.current.delete(target);
            await refresh();
          }
        }
      }
    },
    [runId, apply, refresh],
  );

  const send = useCallback(
    (target: string, patch: ReelChoicePatch, overlay: Overlay) => {
      overlays.current.set(target, { ...overlays.current.get(target), ...overlay });
      setRun((prev) => (prev?.reel && prev.id === runId ? { ...prev, reel: laid(prev.reel, overlays.current) } : prev));
      let q = queues.current.get(target);
      if (!q) {
        q = { running: null, next: null };
        queues.current.set(target, q);
      }
      q.next = { ...(q.next ?? {}), ...patch };
      if (!q.running) {
        const done = drain(target);
        q.running = done;
        void done.finally(() => {
          if (q && q.running === done) q.running = null;
        });
      }
    },
    [runId, setRun, drain],
  );

  /** The version that goes in the reel for `target` (null: the original). */
  const pick = useCallback(
    (target: string, versionId: number | null) => {
      send(target, { pick: versionId }, {
        ships: versionId,
        pick: versionId == null ? null : { version_id: versionId, state: "ready" },
        making: null,
      });
      // A pick clears a version on its way (the server's rule).
      setStarted((s) => {
        if (!(target in s)) return s;
        const { [target]: _, ...rest } = s;
        return rest;
      });
    },
    [send],
  );

  /** A move of the end or the start (`patch`), its overlay made from the
   *  scene's end block (`change`) as last laid — so an end and a start
   *  moved one after the other both show — then its PUT: at once, or, a
   *  nudge, joined with the next of its kind for 300 ms. Its nudges wait
   *  under `key`; the other kind's (`other`) still waiting goes first. */
  const trim = useCallback(
    (
      target: string,
      key: string,
      other: string,
      change: (e: ReelTarget["end"]) => ReelTarget["end"],
      patch: ReelChoicePatch,
      nudge: boolean,
    ) => {
      const pending = nudges.current.get(other);
      if (pending) {
        window.clearTimeout(pending.timer);
        pending.go();
      }
      const was = overlays.current.get(target)?.end ?? runRef.current.reel?.targets.find((x) => x.target === target)?.end;
      const overlay: Overlay = was ? { end: change(was) } : {};
      overlays.current.set(target, { ...overlays.current.get(target), ...overlay });
      setRun((prev) => (prev?.reel && prev.id === runId ? { ...prev, reel: laid(prev.reel, overlays.current) } : prev));
      const go = () => {
        nudges.current.delete(key);
        send(target, patch, overlay);
      };
      const waiting = nudges.current.get(key);
      if (waiting) window.clearTimeout(waiting.timer);
      if (!nudge) go();
      else nudges.current.set(key, { timer: window.setTimeout(go, NUDGE_MS), go });
    },
    [runId, setRun, send],
  );

  /** Where `target` ends (30 fps frames; null: automatic). `nudge`: a
   *  keyboard step, joined with the next for 300 ms (the grip and the tile
   *  move at once all the same). */
  const end = useCallback(
    (target: string, frame: number | null, nudge = false) =>
      trim(target, target, `${target}:start`, (e) => ({ ...e, user: frame, frame: frame ?? e.auto ?? e.full }),
        { end_frame: frame }, nudge),
    [trim],
  );

  /** Where a main scene `target` starts (30 fps frames from its clip's
   *  first; null: no trim). `nudge` as the end's. */
  const start = useCallback(
    (target: string, frame: number | null, nudge = false) =>
      trim(target, `${target}:start`, target, (e) => ({ ...e, start: frame }), { start_frame: frame }, nudge),
    [trim],
  );

  /** Every PUT sent (and any nudge waiting) has landed, and so has a read
   *  of the reel on its way. */
  const flush = useCallback(async () => {
    for (const w of [...nudges.current.values()]) {
      window.clearTimeout(w.timer);
      w.go();
    }
    for (let i = 0; i < 20; i++) {
      const running = [...queues.current.values()].map((q) => q.running).filter((p): p is Promise<void> => !!p);
      if (!running.length) break;
      await Promise.allSettled(running);
    }
    if (reading.current) await reading.current;
  }, []);

  /** The studio's list of versions for `target` changed. */
  const noteVersions = useCallback(
    (target: string, versions: HarnessVersion[]) => {
      const key = `${runId}:${target}`;
      const numbers = new Map(versions.map((v, i) => [v.id, i + 1]));
      const ends = new Map(versions.map((v) => [v.id, versionEndOf(v)]));
      const old = NUMBERS.get(key);
      const oldEnds = ENDS.get(key);
      if (
        !old ||
        !oldEnds ||
        old.size !== numbers.size ||
        [...numbers].some(([id, n]) => old.get(id) !== n) ||
        [...ends].some(([id, e]) => oldEnds.get(id) !== e)
      ) {
        NUMBERS.set(key, numbers);
        ENDS.set(key, ends);
        setNumbered((n) => n + 1);
      }
      const active = versions.filter(isActive);
      const known = seen.current.get(target) ?? new Set<number>();
      const fresh = active.find((v) => !known.has(v.id));
      active.forEach((v) => known.add(v.id));
      seen.current.set(target, known);
      const was = wasActive.current.get(target) ?? false;
      wasActive.current.set(target, active.length > 0);
      if (fresh) {
        setStarted((s) => ({ ...s, [target]: { id: fresh.id, at: Date.now() } }));
        void refresh();
      } else if (was && !active.length) {
        void refresh();
      }
    },
    [runId, refresh],
  );

  const no = useCallback(
    (target: string) => (versionId: number) => NUMBERS.get(`${runId}:${target}`)?.get(versionId) ?? null,
    [runId],
  );
  /** The end a version of `target` was made for (undefined: not known yet). */
  const endOf = useCallback(
    (target: string) => (versionId: number) => {
      const m = ENDS.get(`${runId}:${target}`);
      return m && m.has(versionId) ? m.get(versionId) ?? null : undefined;
    },
    [runId],
  );

  // A version being made: read the reel every 4 s until none is.
  const making = (run.reel?.targets ?? []).some((t) => t.making != null) || Object.keys(started).length > 0;
  useEffect(() => {
    if (!making) return;
    const id = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(id);
  }, [making, refresh]);

  // A nudge still waiting when the stage goes is sent then.
  useEffect(
    () => () => {
      for (const w of [...nudges.current.values()]) {
        window.clearTimeout(w.timer);
        w.go();
      }
    },
    [],
  );

  const lastShaOf = useCallback(() => lastSha.current, []);

  return { pick, end, start, flush, refresh, noteVersions, started, no, endOf, lastSha: lastShaOf };
}
