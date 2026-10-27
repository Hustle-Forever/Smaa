// Rebrand studio — the stage's clock (stageT), and its exact stills.
//
// useIconPlayer   ONE clock for the stage's one video (StagePlayer: the
//                 draft's exact video, or the scene under the quick
//                 stand-in), the lane's playhead and the Moments chips. The
//                 video plays WITH its sound (timing is judged by ear). The
//                 playhead updates ~10 times a second; the overlay draws
//                 every frame on its own. A "stop" is the user coming to rest
//                 on a time: a pause, a click on the lane, a Moments chip.
//                 Playing to the end is not one: the clock goes back to
//                 0:00, paused, and `ended` counts it (the stage goes back
//                 to its still). The scene's end set by hand (`end`, s) is
//                 its end: play stops there as at the video's own, and no
//                 seek goes past it. A main scene's start (`start`, s: the
//                 reel skips the clip before it) is its start the same
//                 way: play begins there, the end goes back there, and no
//                 seek goes before it. The clock stays in the clip's own
//                 seconds either way.
// useIconMoment   on a stop, the exact frame at that time (POST
//                 /icons/moment, or /house/moment for a draft made in house:
//                 built the way the run builds it), swapped in for the video
//                 once it has decoded. It is only ever the frame of the plan
//                 (the draft) on screen at the time it was asked for: a new
//                 one, or pressing Play, takes it away.

import { useCallback, useEffect, useRef, useState } from "react";

import { harnessApi, viewKey, type HarnessBaseRef, type HarnessRecipe, type SceneView } from "../../../lib/rebrandHarnessApi";
import { decodeImage } from "./imageDecode";

const PUSH_MS = 90;
const MOMENT_AFTER_MS = 300;
const FPS = 30;

export interface IconPlayer {
  /** The playhead (s), ~10 Hz. */
  t: number;
  tRef: React.MutableRefObject<number>;
  playing: boolean;
  muted: boolean;
  duration: number;
  /** The last time the user came to rest on (asks for its exact frame). */
  stop: { t: number; n: number } | null;
  /** How many times a video played to its end (back to 0:00, paused). */
  ended: number;
  videoRef: React.MutableRefObject<HTMLVideoElement | null>;
  /** The <video>'s ref: it takes up where the clock is when it mounts. */
  attach: (v: HTMLVideoElement | null) => void;
  play: () => void;
  pause: () => void;
  toggle: () => void;
  /** Go to t: playing on (`play`), or resting there (its exact frame). */
  seek: (t: number, opts?: { play?: boolean }) => void;
  setMuted: (m: boolean) => void;
  /** The overlay's frame clock telling the playhead where it is. */
  tick: (t: number) => void;
}

export function useIconPlayer(end: number | null = null, start: number | null = null): IconPlayer {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  // The scene's start (s; 0: the clip's first frame): where the clock rests
  // before anything moves it.
  const from = start != null && start > 0 ? start : 0;
  const tRef = useRef(from);
  // The scene's end set by hand (s): read by the clock as it runs.
  const endRef = useRef(end);
  endRef.current = end;
  const startRef = useRef(from);
  startRef.current = from;
  const [t, setT] = useState(from);
  const [playing, setPlaying] = useState(false);
  const wantPlay = useRef(false);
  const [muted, setMuted] = useState(false);
  const [duration, setDuration] = useState(0);
  const [stop, setStop] = useState<{ t: number; n: number } | null>(null);
  const [ended, setEnded] = useState(0);
  const lastPush = useRef(0);
  const n = useRef(0);

  /** Played to its end: back to the start (the scene's, when it has one),
   *  paused (the clip's last frame is never left on screen — a Veo clip can
   *  end on a flash of another shot), and `ended` counts it. No exact frame
   *  is asked for: the user did not stop there. */
  const finish = useCallback((v: HTMLVideoElement | null) => {
    const at = startRef.current;
    wantPlay.current = false;
    setPlaying(false);
    if (v) {
      v.pause();
      v.currentTime = at;
    }
    tRef.current = at;
    lastPush.current = performance.now();
    setT(at);
    setEnded((k) => k + 1);
  }, []);

  const publish = useCallback((now: number, force = false) => {
    // Past the scene's end while it plays: that is its end.
    const cut = endRef.current;
    if (cut != null && wantPlay.current && now >= cut - 0.5 / FPS) {
      finish(videoRef.current);
      return;
    }
    tRef.current = now;
    const ms = performance.now();
    if (force || ms - lastPush.current > PUSH_MS) {
      lastPush.current = ms;
      setT(now);
    }
  }, [finish]);

  const rest = useCallback((at: number) => setStop({ t: at, n: ++n.current }), []);

  // The start moved past the clock (its grip dragged right): the clock goes
  // there, at rest. No exact frame is asked for: the user did not stop there.
  useEffect(() => {
    if (wantPlay.current || tRef.current >= from) return;
    tRef.current = from;
    setT(from);
    const v = videoRef.current;
    if (v) v.currentTime = from;
  }, [from]);

  const attach = useCallback(
    (v: HTMLVideoElement | null) => {
      videoRef.current = v;
      if (!v) return;
      const start = () => {
        if (Number.isFinite(v.duration)) setDuration(v.duration);
        if (Math.abs(v.currentTime - tRef.current) > 0.02) v.currentTime = tRef.current;
        if (wantPlay.current) {
          void v.play().catch(() => {
            wantPlay.current = false;
            setPlaying(false);
          });
        }
      };
      if (v.readyState >= 1) start();
      else v.addEventListener("loadedmetadata", start, { once: true });
      // A video the stage has swapped out (the exact video replacing the
      // stand-in's, at the same time) says nothing more: its removal pauses
      // it, which is not the user stopping.
      const mine = () => videoRef.current === v;
      v.addEventListener("timeupdate", () => mine() && publish(v.currentTime));
      v.addEventListener("seeked", () => mine() && publish(v.currentTime, true));
      v.addEventListener("ended", () => {
        if (mine()) finish(v);
      });
      v.addEventListener("pause", () => {
        // A pause from the video's own controls is a stop too.
        if (mine() && wantPlay.current && !v.ended) {
          wantPlay.current = false;
          setPlaying(false);
          publish(v.currentTime, true);
          rest(v.currentTime);
        }
      });
      v.addEventListener("play", () => {
        if (!mine()) return;
        wantPlay.current = true;
        setPlaying(true);
      });
    },
    [publish, rest, finish],
  );

  const play = useCallback(() => {
    wantPlay.current = true;
    setPlaying(true);
    setStop(null);
    const v = videoRef.current;
    if (!v) return;
    const last = Math.min(duration || Infinity, endRef.current ?? Infinity);
    // At its end, or before its start: from the start.
    if (v.ended || (Number.isFinite(last) && tRef.current >= last - 0.05) || tRef.current < startRef.current) {
      v.currentTime = tRef.current = startRef.current;
    }
    void v.play().catch(() => {
      wantPlay.current = false;
      setPlaying(false);
    });
  }, [duration]);

  const pause = useCallback(() => {
    wantPlay.current = false;
    setPlaying(false);
    const v = videoRef.current;
    if (v) {
      v.pause();
      publish(v.currentTime, true);
    }
    rest(tRef.current);
  }, [publish, rest]);

  const seek = useCallback(
    (to: number, opts: { play?: boolean } = {}) => {
      const last = Math.min(duration || Infinity, endRef.current ?? Infinity);
      const at = Math.max(startRef.current, Number.isFinite(last) ? Math.min(to, last - 1 / FPS) : to);
      publish(at, true);
      const v = videoRef.current;
      if (v) v.currentTime = at;
      if (opts.play) {
        wantPlay.current = true;
        setPlaying(true);
        setStop(null);
        if (v) void v.play().catch(() => setPlaying(false));
      } else {
        wantPlay.current = false;
        setPlaying(false);
        v?.pause();
        rest(at);
      }
    },
    [duration, publish, rest],
  );

  const toggle = useCallback(() => (wantPlay.current ? pause() : play()), [pause, play]);

  return {
    t,
    tRef,
    playing,
    muted,
    duration,
    stop,
    ended,
    videoRef,
    attach,
    play,
    pause,
    toggle,
    seek,
    setMuted,
    tick: publish,
  };
}

export interface IconMomentState {
  /** The exact frame on show (null: the moving preview is). */
  exact: { url: string; t: number } | null;
  loading: boolean;
  failed: boolean;
}

export function useIconMoment({
  runId,
  target,
  recipe,
  planKey,
  versionId,
  stop,
  playing,
  enabled,
  house = null,
  view = null,
}: {
  runId: number;
  target: string;
  /** The recipe of the plan on screen. */
  recipe: HarnessRecipe | null;
  planKey: string | null;
  /** Drawn on this version (Add to it), else on the scene. */
  versionId: number | null;
  stop: { t: number; n: number } | null;
  playing: boolean;
  enabled: boolean;
  /** A draft made in house: its frame from the in-house engine
   *  (/house/moment) — with or without icons (`planKey` then optional), on
   *  the version it is built on (`base`); `key` is the draft's own. */
  house?: { key: string; base: HarnessBaseRef | null } | null;
  /** The scene's end set by hand (sent, and keyed, only when set). */
  view?: SceneView | null;
}): IconMomentState {
  const cache = useRef(new Map<string, string>());
  const [got, setGot] = useState<{ key: string; url: string; t: number } | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const recipeRef = useRef(recipe);
  recipeRef.current = recipe;
  const houseRef = useRef(house);
  houseRef.current = house;
  const viewRef = useRef(view);
  viewRef.current = view;

  const keyOf = (at: number) =>
    (house ? `house|${house.key}|${planKey ?? ""}|${Math.round(at * FPS)}` : `${planKey}|${versionId ?? ""}|${Math.round(at * FPS)}`) +
    viewKey(view);
  const want = enabled && !!stop && (!!planKey || !!house) && !playing ? keyOf(stop.t) : null;

  useEffect(() => {
    setFailed(false);
    if (!want || !stop || (!planKey && !houseRef.current) || !recipeRef.current) {
      setLoading(false);
      return;
    }
    const hit = cache.current.get(want);
    if (hit) {
      setGot({ key: want, url: hit, t: stop.t });
      setLoading(false);
      return;
    }
    const ctl = new AbortController();
    let cancelled = false;
    setLoading(true);
    const timer = window.setTimeout(async () => {
      try {
        const h = houseRef.current;
        const m = h
          ? await harnessApi.houseMoment(runId, target, recipeRef.current!, stop.t, {
              base: h.base,
              iconsPlanKey: planKey,
              signal: ctl.signal,
              view: viewRef.current,
            })
          : await harnessApi.iconMoment(runId, target, recipeRef.current!, planKey!, stop.t, {
              versionId,
              signal: ctl.signal,
              view: viewRef.current,
            });
        await decodeImage(m.url);
        if (cancelled) return;
        cache.current.set(want, m.url);
        if (cache.current.size > 60) cache.current.delete(cache.current.keys().next().value!);
        setGot({ key: want, url: m.url, t: stop.t });
      } catch {
        if (!cancelled) setFailed(true);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, MOMENT_AFTER_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      ctl.abort();
    };
    // want holds the plan, the version and the frame.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [want, stop?.n]);

  return {
    exact: got && want && got.key === want ? { url: got.url, t: got.t } : null,
    loading: !!want && loading,
    failed: !!want && failed,
  };
}
