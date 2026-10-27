// Rebrand studio — the stage: ONE canvas, showing one thing at a time.
//
//   Next       your change: the free preview. Made in house it is the real
//              frame — the design still, the very plate the run lays over
//              frame 0 (the patterns showing at its moment, fully in behind
//              the speaker), or, once the timeline's playhead is put
//              somewhere, the exact frame at that moment (`nextStill`, with
//              `nextExtra` above it saying so) — and Play plays it in motion
//              (the StagePlayer: the exact video once it is made, a quick
//              stand-in until then), back to the still when it ends. For
//              Omni, the exact frame Omni will be shown.
//              A new colour shows at once, painted from that preview's own
//              layers (RecolourCanvas), until the server's frame lands.
//              Made in house, the open pattern has handles on the picture
//              (`patternHandles`: move, size, turn — TransformBox), and a
//              pattern being moved is painted at once from its own art under
//              the hand (HandCanvas), until the server's frame lands; a click
//              on a pattern's lines picks it (`onPickPattern`). The pattern's
//              turn button keeps clear of the chips and buttons on the
//              picture (`TurnAvoid`: each measured where it shows, and kept
//              while the stage shows the same thing, so the button doesn't
//              hop when a chip blinks out while a new frame is made).
//              Added on top of a version, it is drawn on THAT version's own
//              frames (its speaker, its graphics, the new layers) — never on
//              the original's, which would be a second picture of the speaker
//              Carrying on from a version, while the draft IS that version
//              (nothing changed yet), Next is that version's own picture
//              (`nextStill`): flipping vN ↔ Next shows the same frame.
//   vN         a version: its settled first frame per part (the speaker from
//              frame 0, the graphics settled), so flipping vN ↔ Next keeps the
//              speaker's pose; the video plays only when asked. One made in
//              house whose video is gone rests on its still, with a note
//              (VersionActions offers Make it again). One made for another
//              end of the scene says so at its top right ("Ends at 7.8 s ·
//              Remake", `endChip`: made again to this end, free)
//   Original   the part's first frame, and the scene itself on Play — up to
//              the scene's end when one is set by hand (`originalEnd`), and
//              from a main scene's start when it has one (`originalStart`)
//   Next, on the Icons tab (or played)   Next in motion (StagePlayer): the
//              draft's exact video, or the scene (or the version added to)
//              playing with a canvas of the patterns and the plan's icons
//              over it, and the exact frame when it rests — in place of the
//              still, never beside it
//
// There is never a second picture of the speaker on screen and never a
// cross-fade: a new picture replaces the old in the same element, hard, and
// only once it has decoded (a blend of two frames of a person is exactly the
// "two faces" look). While the next picture is being made, the old one is
// dimmed and the stage says what it is doing; when it lands, "Updated".
//
// A video that plays to its end goes back to its still, with "Play again":
// the stage never rests on a clip's last frame, the least trustworthy
// picture there is (a Veo clip can end on a flash of another shot, and a
// version made from one on two people).
//
// "Show camera tracking" (the ⋯ menu) draws how the camera moves over the
// scene on top (CameraOverlay): frame 0's middle box, carried by the camera
// to the moment on screen — play the original to see it grow with a push-in.

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { AlertTriangle, Image as ImageIcon, Play, RotateCcw, Undo2 } from "lucide-react";

import type { HarnessBox, HarnessCamera, HarnessScene, HarnessVersion } from "../../../lib/rebrandHarnessApi";
import type { RecolourSpec } from "../../../lib/rebrandRecolour";
import type { Rect } from "../../../lib/rebrandTransform";
import { BusyVeil } from "../../ui/BusyVeil";
import { JobProgress } from "../../ui/JobProgress";
import { Spinner } from "../../ui/Spinner";
import { CameraOverlay } from "./CameraOverlay";
import { HandCanvas, pickPatternAt, type HandSpec } from "./HandCanvas";
import { decodeImage, isDecoded } from "./imageDecode";
import { RecolourCanvas } from "./RecolourCanvas";
import { COPY, mmss } from "./studioCopy";
import { TurnAvoid } from "./TransformBox";
import { madeInHouse, omniArm, startedAtOf, stillOf, versionPhase, type StageSel } from "./studioModel";
import type { StudioPreviewState } from "./useStudioPreview";

/** The picture on screen changes only once the next one has decoded. One
 *  that already has is shown on the very render it is asked for (the live
 *  colour hands over to the server's frame with no frame of the old one). */
function useDecodedSrc(src: string | null) {
  const [shown, setShown] = useState<string | null>(src);
  const [pending, setPending] = useState(false);
  const ready = !src || isDecoded(src);
  useEffect(() => {
    if (src === shown) {
      setPending(false);
      return;
    }
    if (!src || isDecoded(src)) {
      setShown(src);
      setPending(false);
      return;
    }
    let cancelled = false;
    setPending(true);
    void decodeImage(src).then(() => {
      if (cancelled) return;
      setShown(src);
      setPending(false);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src]);
  return { src: ready ? src : shown, pending: !ready && pending && src !== shown };
}

/** The run-wide failure the pipeline writes when no arm finished
 *  (rebrand_pipeline._run): not a reason. */
const PIPELINE_WORDS = /^(No arm produced a video|Every arm failed)\b/;

/** Where a version with no still of its own rests (a settled frame). */
const POSTER_SEC = 2;

/** The largest box of this aspect that fits the element. */
function useFitBox(aspect: number) {
  const ref = useRef<HTMLElement | null>(null);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return;
      const w = Math.min(r.width, r.height * aspect);
      setSize({ w: Math.floor(w), h: Math.floor(w / aspect) });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [aspect]);
  return { ref, size };
}

/** Where a chip or button on the canvas sits, for the turn button to keep
 *  clear of (`data-turn-avoid`; one zone per place). */
type AvoidAt = "top-left" | "bottom-left" | "top-centre" | "bottom-centre" | "top-right";

function Chip({ children, className = "", at }: { children: ReactNode; className?: string; at?: AvoidAt }) {
  return (
    <span
      data-turn-avoid={at}
      className={`pointer-events-none absolute max-w-[calc(100%-1rem)] truncate whitespace-nowrap rounded-full bg-black/65 px-2.5 py-1 text-2xs text-white shadow-soft ${className}`}
    >
      {children}
    </span>
  );
}

function StageCard({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "danger" }) {
  return (
    <div className="absolute inset-x-3 bottom-[12%] flex justify-center">
      <div
        className={`w-full max-w-[20rem] rounded-2xl px-4 py-3 text-xs shadow-lift backdrop-blur-sm ${
          tone === "danger" ? "bg-white/95 text-black" : "bg-black/75 text-white"
        }`}
      >
        {children}
      </div>
    </div>
  );
}

type StepState = "done" | "working" | "waiting" | "failed";

function Step({ state, children }: { state: StepState; children: ReactNode }) {
  return (
    <li className={`flex items-center gap-2 ${state === "waiting" ? "text-white/50" : ""}`}>
      <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center" aria-hidden>
        {state === "done" ? (
          <span className="text-accent">✓</span>
        ) : state === "working" ? (
          <Spinner size="xs" />
        ) : state === "failed" ? (
          <span className="text-warning">!</span>
        ) : (
          <span className="h-1.5 w-1.5 rounded-full bg-white/40" />
        )}
      </span>
      {children}
    </li>
  );
}

export function StudioStage({
  scene,
  preparingSince,
  sel,
  part,
  onPart,
  preview,
  changeText,
  instantHide,
  selectedBox,
  version,
  versionNo,
  sentTargets,
  versionBaseFrames = null,
  actions,
  strip,
  backTo,
  canUndo,
  onUndo,
  onReset,
  compact = false,
  baseFrames = null,
  nextSub,
  baseNote = null,
  versionLineage = null,
  baseError = null,
  shownFits = true,
  iconStage = null,
  headerExtra = null,
  nextExtra = null,
  underIcons = null,
  liveColour = null,
  cameraDebug = false,
  camera = null,
  onPlayNext = null,
  nextStill = null,
  handSpec = null,
  handGesture = false,
  holdLive = false,
  patternHandles = null,
  onPickPattern = null,
  originalEnd = null,
  originalStart = null,
  endChip = null,
  className = "",
}: {
  /** null while the scene is being prepared. */
  scene: HarnessScene | null;
  preparingSince: number;
  sel: StageSel;
  part: number;
  onPart: (i: number) => void;
  preview: StudioPreviewState;
  /** What the pending edit is doing ("Painting the new colour…"). */
  changeText: string;
  /** The draft has no pattern on: the target IS the part's first frame. */
  instantHide: boolean;
  /** The open pattern's place on the Next frame. */
  selectedBox: HarnessBox | null;
  version: HarnessVersion | null;
  versionNo: number | null;
  /** The frames this version was sent with (remembered at Generate). */
  sentTargets: string[] | null;
  /** Added on top of another version: that version's frames, per part —
   *  what stands in while this one has none of its own (on its way, after
   *  a reload; failed, stopped), in place of the original's. */
  versionBaseFrames?: (string | null)[] | null;
  /** Under the canvas for a version (VersionActions). */
  actions: ReactNode;
  /** Under the stage: the scene's timeline (SceneTimeline). It keeps the
   *  picture its room: at most 42% of the stage column from lg up
   *  (lib/rebrandTracks LANE_CAP_SHARE; 45% of the screen below), its lanes
   *  scrolling past that. */
  strip: ReactNode;
  backTo: { label: string; onClick: () => void } | null;
  canUndo: boolean;
  onUndo: () => void;
  /** Back to the 42 default: the way out of a refused recipe with no undo. */
  onReset?: () => void;
  /** A phone with a view pushed over the rail: the canvas gives it room. */
  compact?: boolean;
  /** Added on top of a version: what Next is drawn on, per part (its
   *  frames), in place of the original's. */
  baseFrames?: (string | null)[] | null;
  /** Under "Next": "free preview", "on top of v2", "from v2's settings". */
  nextSub?: string;
  /** A note on Next while the version built on is made ready. */
  baseNote?: string | null;
  /** Under a version's name: what it was built on ("on v2"). */
  versionLineage?: string | null;
  /** A preview the version built on can't give (409/410): what to call it,
   *  and the ways out (Change it instead, leave). */
  baseError?: { title: string; actions: ReactNode } | null;
  /** false: the preview on screen was drawn for another base (the original,
   *  or another version) than the draft's. It never stands in for Next,
   *  not even dimmed: the draft's own frame does until its preview lands,
   *  so Next on a version never shows the original's speaker. */
  shownFits?: boolean;
  /** On the Icons tab, what Next shows: the icons in motion (it holds the
   *  one picture itself — the video, or the exact frame). */
  iconStage?: ReactNode;
  /** Beside the caption while the icons show (Show spots). */
  headerExtra?: ReactNode;
  /** Beside the caption on Next's picture (not in motion): what moment it
   *  shows, and the way back to the still. */
  nextExtra?: ReactNode;
  /** Under the canvas while the icons show (the Moments chips). */
  underIcons?: ReactNode;
  /** This part in the draft's colours, painted from the preview on screen
   *  (rebrandRecolour.recolourOf): while only a colour is changing, it is
   *  shown at once in place of the picture, until the server's frame lands. */
  liveColour?: RecolourSpec | null;
  /** "Show camera tracking" is on: draw `camera` over the picture. */
  cameraDebug?: boolean;
  /** The scene's camera (null while it is asked for). */
  camera?: HarnessCamera | null;
  /** Next can be played (a draft made in house): Play on its still hands
   *  the stage to the StagePlayer (`iconStage`). */
  onPlayNext?: (() => void) | null;
  /** The draft IS the version it carries on from: that version's own still
   *  per part (null: the preview's frame, as ever). */
  nextStill?: (string | null)[] | null;
  /** A pattern moved by hand, painted at once (HandCanvas): shown while a
   *  hand moves it (`handGesture`), and after, until the server's frame of
   *  that move has decoded. */
  handSpec?: HandSpec | null;
  handGesture?: boolean;
  /** Next rests on a moment whose new frame is on its way: a pattern just
   *  moved stays painted until it lands (never the old place in between). */
  holdLive?: boolean;
  /** The open pattern's handles, over Next's picture (in place of its
   *  outline). */
  patternHandles?: ReactNode;
  /** A click on Next's picture: the pattern whose lines it landed on (null:
   *  none — the row closes). */
  onPickPattern?: ((id: string | null) => void) | null;
  /** The scene's end set by hand (s): Original plays up to it. */
  originalEnd?: number | null;
  /** A main scene's start (s; the reel skips the clip before it): Original
   *  plays from it. */
  originalStart?: number | null;
  /** A version made for another end of the scene: "Ends at 7.8 s ·
   *  Remake" (made again to this end). */
  endChip?: { label: string; title: string; disabled: boolean; onRemake: () => void } | null;
  className?: string;
}) {
  const [playing, setPlaying] = useState(false);
  // The video played to its end (and went back to its still): Play again.
  const [ended, setEnded] = useState(false);
  const [holding, setHolding] = useState(false);
  // Stills that failed to load (gone from the server): the video stands in.
  const [broken, setBroken] = useState<ReadonlySet<string>>(() => new Set());
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const aspect = scene ? scene.width / Math.max(1, scene.height) : 9 / 16;
  const { ref: fitRef, size } = useFitBox(aspect);

  const selKey = sel.kind === "version" ? `v${sel.id}` : sel.kind;
  useEffect(() => {
    setPlaying(false);
    setEnded(false);
    setHolding(false);
  }, [selKey, part]);

  // What the turn button keeps clear of: each chip or button on the canvas
  // (`data-turn-avoid`), measured after every render, one zone per place,
  // grown to the widest seen. Kept while the stage shows the same part of
  // the same thing at the same size — a chip that blinks out while a new
  // frame is made keeps its room, so the button doesn't hop.
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const [avoid, setAvoid] = useState<readonly Rect[]>([]);
  const zonesRef = useRef<{ key: string; at: Map<string, Rect> }>({ key: "", at: new Map() });
  useLayoutEffect(() => {
    const el = canvasRef.current;
    if (!el) return;
    const c = el.getBoundingClientRect();
    const key = `${selKey}|${part}|${Math.round(c.width)}x${Math.round(c.height)}`;
    if (zonesRef.current.key !== key) zonesRef.current = { key, at: new Map() };
    const at = zonesRef.current.at;
    el.querySelectorAll<HTMLElement>("[data-turn-avoid]").forEach((n) => {
      const r = n.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return;
      const box = { x: r.left - c.left, y: r.top - c.top, w: r.width, h: r.height };
      const was = at.get(n.dataset.turnAvoid ?? "");
      const x0 = Math.min(box.x, was?.x ?? box.x);
      const y0 = Math.min(box.y, was?.y ?? box.y);
      const x1 = Math.max(box.x + box.w, was ? was.x + was.w : 0);
      const y1 = Math.max(box.y + box.h, was ? was.y + was.h : 0);
      at.set(n.dataset.turnAvoid ?? "", { x: x0, y: y0, w: x1 - x0, h: y1 - y0 });
    });
    const next = [...at.values()];
    setAvoid((prev) =>
      prev.length === next.length && prev.every((p, i) => p.x === next[i].x && p.y === next[i].y && p.w === next[i].w && p.h === next[i].h)
        ? prev
        : next,
    );
  });

  const parts = scene?.chunks.length ?? 1;
  const sceneFrame = scene?.chunks[part]?.frame_url ?? null;
  // What Next is drawn on: the version's own frame when adding on top of it.
  const frame = baseFrames ? baseFrames[part] ?? null : sceneFrame;
  const shownChunk = shownFits ? preview.shown?.preview.chunks[part] ?? null : null;
  const pendingNext = preview.status === "waiting" || preview.status === "loading";
  const kind = preview.change?.kind ?? null;

  // --- what goes on the canvas ------------------------------------------------
  let desired: string | null = null;
  let video: string | null = null;
  let title = "";
  let sub = "";
  let what = "";
  const phase = version ? versionPhase(version) : null;
  // Made in house and its video gone: nothing to play until it is made again.
  const lostVideo = !!version && phase === "ready" && madeInHouse(version) && !!version.lost;
  const omniUrl = version && !lostVideo ? omniArm(version)?.url ?? null : null;
  if (sel.kind === "next") {
    title = COPY.captionNext;
    sub = nextSub ?? COPY.captionNextSub;
    what = "Next, the free preview";
    const target = nextStill?.[part] ?? (shownChunk ? shownChunk.target_url ?? shownChunk.frame_url : frame);
    desired = holding || (instantHide && preview.status !== "error") ? frame : target;
  } else if (sel.kind === "original") {
    title = COPY.captionOriginal;
    sub = COPY.captionOriginalSub;
    what = "the original";
    desired = sceneFrame;
    // From the scene's start and up to its end, when either is set by hand
    // (a media fragment).
    const from = originalStart != null && originalStart > 0 ? originalStart.toFixed(3) : "0";
    video = scene?.video_url
      ? originalEnd != null
        ? `${scene.video_url}#t=${from},${originalEnd.toFixed(3)}`
        : from !== "0"
          ? `${scene.video_url}#t=${from}`
          : scene.video_url
      : null;
  } else if (version && versionNo) {
    title = COPY.version(versionNo);
    what = `version ${versionNo}`;
    const on = versionLineage ? `${versionLineage} · ` : "";
    if (phase === "ready") {
      sub = on + COPY.captionVersionSub(omniArm(version)?.duration_sec ?? scene?.duration_sec ?? null);
      const still = stillOf(version, part);
      desired = still && !broken.has(still) ? still : null;
      video = omniUrl;
    } else if (phase === "queued" || phase === "running") {
      sub = on + COPY.captionRunning;
      desired = sentTargets?.[part] ?? versionBaseFrames?.[part] ?? sceneFrame;
    } else {
      sub = on + (phase === "failed" ? COPY.captionFailed : COPY.captionStopped);
      desired = stillOf(version, part) ?? sentTargets?.[part] ?? versionBaseFrames?.[part] ?? sceneFrame;
    }
  }
  // The icons in motion stand in for Next's picture (and hold it themselves).
  const icons = sel.kind === "next" && !!iconStage && !!scene;
  if (icons) desired = null;
  // A ready version with no still (an older server): its video, paused on a
  // settled frame, is the one picture.
  const videoOnly = sel.kind === "version" && phase === "ready" && !desired && !!video;
  const showVideo = !!video && (playing || videoOnly);
  const decoded = useDecodedSrc(showVideo ? null : desired);

  // --- the states on top of it --------------------------------------------------
  const onNext = sel.kind === "next";
  const first = onNext && !icons && !!scene && !preview.shown && preview.status !== "error";
  const errNext = onNext && preview.status === "error" && !!preview.error && (!icons || !preview.error.retryable);
  const wordsOnly = onNext && !icons && pendingNext && kind === "words";
  const checking = onNext && !icons && pendingNext && instantHide;
  // Only a colour is changing: painted here at once from the preview on
  // screen (its alphas never change with the colour), until the server's
  // exact frame has decoded and replaces it. Shown only once it has painted;
  // until then (or if it can't) the stage dims as for any other edit.
  const liveSpec =
    onNext && !icons && !holding && pendingNext && kind === "colour" && !!shownChunk && !instantHide
      ? liveColour
      : null;
  const [liveReady, setLiveReady] = useState(false);
  // A pattern moved by hand: painted from its own art while the hand moves
  // it, and after, until the server's frame of the move has decoded.
  const handWanted =
    onNext && !icons && !holding && !instantHide && !!handSpec &&
    (handGesture || (kind === "hand" && (pendingNext || decoded.pending || holdLive)));
  const [handReady, setHandReady] = useState(false);
  const liveHand = handWanted && handReady;
  const live = (!!liveSpec && liveReady) || liveHand;
  const busyNext = onNext && !icons && pendingNext && !first && !wordsOnly && !instantHide && !holding && !live;
  const versionWorking = sel.kind === "version" && (phase === "queued" || phase === "running");
  const versionProblem = sel.kind === "version" && (phase === "failed" || phase === "stopped");
  // Why: Omni's own reason first; the pipeline's run-wide words ("No arm
  // produced a video — see each arm's steps") mean nothing here, where
  // there are no arms and no steps.
  const problemText = versionProblem && version
    ? omniArm(version)?.error ||
      (version.error && !PIPELINE_WORDS.test(version.error) ? version.error : null) ||
      (phase === "failed" ? (madeInHouse(version) ? COPY.noVideoHouse : COPY.noVideoBack) : null)
    : null;
  const dim = busyNext || first || errNext || versionWorking || versionProblem || (decoded.pending && !live) || !scene;
  const entering = onNext && preview.current && !holding && shownChunk?.entering;
  const drawn = preview.current ? preview.shown?.preview.patterns ?? null : null;
  const entersText = drawn && drawn.length > 1
    ? COPY.entersMany(drawn.filter((p) => p.animation && p.animation !== "none").length)
    : preview.shown?.preview.pattern
      ? COPY.enters(preview.shown.preview.pattern.animation.replace(/_/g, " "))
      : null;
  const warnings = onNext && preview.current ? preview.shown?.preview.warnings ?? [] : [];

  // The first preview's steps. Finding the speaker: the masks the preview is
  // drawn with, then (a newer server) the speaker on the scene's frames for
  // the in-house engine; following the camera comes after those.
  const warm = preview.warmup;
  const masksDone = !!warm && warm.masks.total > 0 && warm.masks.done >= warm.masks.total;
  const frames = warm?.speaker_frames ?? null;
  const analysed = warm?.camera === "ready" || warm?.camera === "failed";
  const framesDone = !frames || analysed || (frames.total > 0 && frames.done >= frames.total);
  const speakerStep: StepState = masksDone && framesDone ? "done" : "working";
  const speakerCount =
    masksDone && frames && frames.total > 0 && !framesDone
      ? `${frames.done}/${frames.total}`
      : warm && !masksDone && warm.masks.total > 1
        ? `${warm.masks.done}/${warm.masks.total}`
        : null;
  const cameraStep: StepState | null = !warm?.camera
    ? null
    : warm.camera === "ready"
      ? "done"
      : warm.camera === "failed"
        ? "failed"
        : warm.camera === "working" && masksDone && framesDone
          ? "working"
          : "waiting";
  // A finished version's own (icons: a symbol Omni drew itself, one left out).
  const versionWarnings = version && phase === "ready" ? omniArm(version)?.warnings ?? [] : [];

  // Hold: before — pointer, Space or Enter.
  const holdHandlers = {
    onPointerDown: () => setHolding(true),
    onPointerUp: () => setHolding(false),
    onPointerLeave: () => setHolding(false),
    onPointerCancel: () => setHolding(false),
    onBlur: () => setHolding(false),
    onKeyDown: (e: React.KeyboardEvent) => {
      if ((e.key === " " || e.key === "Enter") && !e.repeat) {
        e.preventDefault();
        setHolding(true);
      }
    },
    onKeyUp: (e: React.KeyboardEvent) => {
      if (e.key === " " || e.key === "Enter") {
        e.preventDefault();
        setHolding(false);
      }
    },
  };

  // A click on Next's picture picks the pattern whose lines are there.
  const pickable = !!onPickPattern && onNext && !icons && !holding && preview.current && !!shownChunk?.layers;
  const onCanvasClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!pickable || !onPickPattern) return;
    if ((e.target as HTMLElement).closest("button, [data-transform-box] [role=group]")) return;
    const r = e.currentTarget.getBoundingClientRect();
    const x = (e.clientX - r.left) / Math.max(1, r.width);
    const y = (e.clientY - r.top) / Math.max(1, r.height);
    void pickPatternAt(shownChunk, x, y).then(onPickPattern);
  };

  const canvas = (
    <div
      ref={canvasRef}
      className={`relative overflow-hidden rounded-xl bg-black shadow-soft transition-shadow duration-300 ${
        onNext && preview.pulse ? "ring-2 ring-accent ring-offset-2" : ""
      }`}
      style={size ? { width: size.w, height: size.h } : { width: "100%", aspectRatio: String(aspect) }}
      onClick={pickable ? onCanvasClick : undefined}
      data-pickable={pickable ? "" : undefined}
    >
      {icons ? (
        iconStage
      ) : (
      <BusyVeil
        className="h-full w-full"
        active={busyNext || (live && !handGesture)}
        dim={dim}
        since={preview.since}
        label={changeText}
        slowHint={COPY.slowHint}
        done={onNext && preview.pulse}
        doneLabel={COPY.updated}
      >
        {liveSpec && (
          // Painted before it is shown; while it isn't, the picture below is.
          <RecolourCanvas
            spec={liveSpec}
            onReady={setLiveReady}
            className={`absolute inset-0 h-full w-full select-none object-contain ${live ? "" : "invisible"}`}
          />
        )}
        {handWanted && handSpec && !liveSpec && (
          <HandCanvas
            spec={handSpec}
            onReady={setHandReady}
            className={`absolute inset-0 h-full w-full select-none object-contain ${liveHand ? "" : "invisible"}`}
          />
        )}
        {live ? null : showVideo && video ? (
          <video
            key={video}
            ref={videoRef}
            src={videoOnly && !playing ? `${video}#t=${POSTER_SEC}` : video}
            controls
            playsInline
            autoPlay={playing}
            preload="metadata"
            onEnded={(e) => {
              if (videoOnly) {
                // Its video is the one picture this version has: back to
                // its settled frame, where it rests before it is played.
                e.currentTarget.currentTime = POSTER_SEC;
                return;
              }
              setPlaying(false);
              setEnded(true);
            }}
            // Original stops at the scene's end (its fragment pauses there,
            // with no "ended"): that is its end all the same.
            onPause={(e) => {
              if (sel.kind === "original" && originalEnd != null && e.currentTarget.currentTime >= originalEnd - 0.05) {
                setPlaying(false);
                setEnded(true);
              }
            }}
            className="h-full w-full object-contain"
          />
        ) : decoded.src ? (
          <img
            src={decoded.src}
            alt=""
            draggable={false}
            onError={() => {
              const bad = decoded.src;
              if (bad && sel.kind === "version") setBroken((prev) => new Set(prev).add(bad));
            }}
            className="h-full w-full select-none object-contain"
          />
        ) : null}
      </BusyVeil>
      )}

      {cameraDebug && scene && !icons && (
        <CameraOverlay camera={camera} videoRef={videoRef} video={showVideo} />
      )}

      {/* The open pattern's handles (made in house), else its place,
          outlined on the Next frame. */}
      {onNext && !icons && !holding && patternHandles && <TurnAvoid.Provider value={avoid}>{patternHandles}</TurnAvoid.Provider>}
      {onNext && !icons && !patternHandles && preview.current && selectedBox && !holding && !pendingNext && (
        <span
          aria-hidden
          className="pointer-events-none absolute rounded-md border-2 border-dashed border-accent/90 shadow-[0_0_0_1px_rgba(0,0,0,0.25)]"
          style={{
            left: `${Math.max(0, selectedBox.x) * 100}%`,
            top: `${Math.max(0, selectedBox.y) * 100}%`,
            width: `${Math.min(1, selectedBox.w) * 100}%`,
            height: `${Math.min(1, selectedBox.h) * 100}%`,
          }}
        />
      )}

      {holding && !icons && <Chip className="left-2 top-2" at="top-left">{COPY.showingBefore}</Chip>}
      {entering && !icons && entersText && (
        // Next's Play sits at the foot of the picture: the chip goes up.
        <Chip className={onPlayNext ? "left-2 top-2" : "bottom-2 left-2"} at={onPlayNext ? "top-left" : "bottom-left"}>
          {entersText}
        </Chip>
      )}
      {wordsOnly && (
        <Chip className="left-1/2 top-2 -translate-x-1/2" at="top-centre">
          {COPY.wordsChip}
        </Chip>
      )}
      {onNext && !icons && baseNote && !busyNext && !first && !wordsOnly ? (
        <Chip className="left-1/2 top-2 -translate-x-1/2" at="top-centre">
          {baseNote}
        </Chip>
      ) : (
        checking && (
          <Chip className="left-1/2 top-2 -translate-x-1/2" at="top-centre">
            {COPY.checking}
          </Chip>
        )
      )}
      {errNext && !icons && preview.shown && shownFits && !preview.current && (
        <Chip className="left-2 top-2" at="top-left">
          {COPY.notLatest}
        </Chip>
      )}

      {!scene && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 px-6 text-center text-xs text-white/80" role="status">
          <Spinner size="lg" tone="accent" />
          <span className="font-medium text-white">{COPY.gettingReady}</span>
          <ElapsedLine since={preparingSince} hint={COPY.gettingReadyHint} />
        </div>
      )}

      {first && (
        <StageCard>
          <p className="mb-2 font-medium">{COPY.firstTitle}</p>
          <ul className="space-y-1 text-2xs" role="status" aria-live="polite">
            <Step state={speakerStep}>
              {COPY.stepSpeaker}
              {speakerCount && (
                <span className="ml-auto font-mono tabular-nums text-white/60">{speakerCount}</span>
              )}
            </Step>
            {cameraStep && (
              <Step state={cameraStep}>{cameraStep === "failed" ? COPY.stepCameraFailed : COPY.stepCamera}</Step>
            )}
            <Step
              state={
                !preview.warmup
                  ? "working"
                  : preview.warmup.colours === "ready"
                    ? "done"
                    : preview.warmup.colours === "failed"
                      ? "failed"
                      : "working"
              }
            >
              {preview.warmup?.colours === "failed" ? COPY.stepColoursFailed : COPY.stepColours}
            </Step>
            <Step
              state={
                preview.warmup &&
                preview.warmup.colours !== "working" &&
                preview.warmup.masks.done >= preview.warmup.masks.total
                  ? "working"
                  : "waiting"
              }
            >
              {COPY.stepPattern}
            </Step>
          </ul>
          <JobProgress
            startedAt={preview.since ?? Date.now()}
            kind="rebrand_preview"
            label=""
            className="mt-2.5 [&_p]:text-white/70"
          />
        </StageCard>
      )}

      {errNext && preview.error && (
        <StageCard tone="danger">
          <div role="alert" className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-danger" aria-hidden />
            <div className="min-w-0 space-y-2">
              <p className="font-medium">
                {preview.error.retryable ? COPY.errorTitle : baseError ? baseError.title : COPY.refusedTitle}
              </p>
              <p className="text-2xs text-muted break-words">{preview.error.message}</p>
              {preview.error.retryable ? (
                <button type="button" className="btn btn-xs btn-ghost" onClick={preview.retry}>
                  <RotateCcw className="h-3 w-3" aria-hidden /> {COPY.tryAgain}
                </button>
              ) : baseError ? (
                <div className="flex flex-wrap gap-1.5">{baseError.actions}</div>
              ) : canUndo ? (
                <button type="button" className="btn btn-xs btn-ghost" onClick={onUndo}>
                  <Undo2 className="h-3 w-3" aria-hidden /> {COPY.undo}
                </button>
              ) : (
                onReset && (
                  <button type="button" className="btn btn-xs btn-ghost" onClick={onReset}>
                    <RotateCcw className="h-3 w-3" aria-hidden /> {COPY.reset}
                  </button>
                )
              )}
            </div>
          </div>
        </StageCard>
      )}

      {versionWorking && version && versionNo && (
        <StageCard>
          <p className="mb-2 font-medium">{phase === "queued" ? COPY.queued(versionNo) : COPY.making(versionNo, madeInHouse(version))}</p>
          <JobProgress
            startedAt={startedAtOf(version)}
            kind={madeInHouse(version) ? "rebrand_house" : "rebrand"}
            // The server's stage line, except "queued", which the title says.
            label={phase === "queued" ? "" : version.stage || ""}
            className="[&_p]:text-white/70"
          />
        </StageCard>
      )}

      {versionProblem && version && versionNo && (
        <StageCard tone="danger">
          <div className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-danger" aria-hidden />
            <div className="min-w-0 space-y-1">
              <p className="font-medium">{phase === "failed" ? COPY.failed(versionNo) : COPY.stopped(versionNo)}</p>
              {problemText && <p className="text-2xs text-muted break-words">{problemText}</p>}
            </div>
          </div>
        </StageCard>
      )}

      {/* A version made for another end of the scene: made again to this one. */}
      {sel.kind === "version" && endChip && !versionWorking && !versionProblem && (
        <button
          type="button"
          data-turn-avoid="top-right"
          data-end-chip
          onClick={endChip.onRemake}
          disabled={endChip.disabled}
          title={endChip.title}
          className="absolute right-2 top-2 max-w-[calc(100%-1rem)] truncate rounded-full bg-black/65 px-2.5 py-1 text-2xs font-medium text-amber-200 shadow-soft transition-colors hover:bg-black/80 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          {endChip.label}
        </button>
      )}

      {/* Next made in house, in motion: the StagePlayer takes the stage. */}
      {onNext && !icons && onPlayNext && !holding && !busyNext && !first && !errNext && preview.current && (
        <button
          type="button"
          data-turn-avoid="bottom-centre"
          onClick={onPlayNext}
          aria-label={COPY.playTitle(what)}
          title={COPY.stage.playNextTitle}
          className="absolute bottom-3 left-1/2 inline-flex -translate-x-1/2 items-center gap-1.5 rounded-full bg-black/65 px-3 py-1.5 text-2xs font-medium text-white shadow-lift transition-colors hover:bg-black/80 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <Play className="h-3.5 w-3.5" aria-hidden />
          {COPY.play}
        </button>
      )}

      {/* Play / back to the still: the canvas holds one of them, never both. */}
      {video && !videoOnly && !versionWorking && !versionProblem && (
        <button
          type="button"
          data-turn-avoid={playing ? "top-left" : "bottom-centre"}
          onClick={() => {
            setEnded(false);
            setPlaying((v) => !v);
          }}
          aria-label={playing ? COPY.stillTitle : COPY.playTitle(what)}
          title={playing ? COPY.stillTitle : COPY.playTitle(what)}
          className={`absolute inline-flex items-center gap-1.5 rounded-full bg-black/65 px-3 py-1.5 text-2xs font-medium text-white shadow-lift transition-colors hover:bg-black/80 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
            playing ? "left-2 top-2" : "bottom-3 left-1/2 -translate-x-1/2"
          }`}
        >
          {playing ? <ImageIcon className="h-3.5 w-3.5" aria-hidden /> : <Play className="h-3.5 w-3.5" aria-hidden />}
          {playing ? COPY.still : ended ? COPY.playAgain : COPY.play}
        </button>
      )}
    </div>
  );

  return (
    <section className={`flex min-h-0 min-w-0 flex-col gap-2 px-3 pt-2 pb-2 lg:px-4 lg:pt-3 ${className}`}>
      <div className="flex min-h-[32px] flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-sm font-semibold text-fg">{title || COPY.captionNext}</span>
        {sub && <span className="text-2xs text-muted">{sub}</span>}
        {backTo && (
          <button
            type="button"
            onClick={backTo.onClick}
            className="inline-flex items-center gap-1 rounded-full border border-border bg-bg px-2.5 py-0.5 text-2xs text-muted transition-colors hover:border-border-strong hover:text-fg motion-safe:animate-fadeIn"
          >
            ← {backTo.label}
          </button>
        )}
        <span className="ml-auto" />
        {icons ? headerExtra : onNext ? nextExtra : null}
        {onNext && scene && !icons && (
          <button
            type="button"
            aria-pressed={holding}
            title={COPY.holdBeforeTitle}
            className={`btn btn-xs select-none touch-none ${holding ? "btn-secondary" : "btn-ghost"}`}
            {...holdHandlers}
          >
            {COPY.holdBefore}
          </button>
        )}
      </div>

      <figure ref={fitRef as React.RefObject<HTMLElement>} className={`relative m-0 flex min-h-0 items-center justify-center lg:h-auto lg:flex-1 ${compact ? "h-[20dvh]" : "h-[34dvh]"}`}>
        {canvas}
        <figcaption className="sr-only">{COPY.showing(what || "the stage", part + 1, parts)}</figcaption>
      </figure>

      {/* Always the same height, so the canvas (and the speaker in it) keeps
          its size when the stage flips between a version and Next. */}
      <div className="flex min-h-[30px] items-center justify-center">
        {sel.kind === "version" ? (
          lostVideo && versionNo ? (
            <div className="flex flex-col items-center gap-1">
              <p className="max-w-xl text-center text-2xs text-muted" role="note">
                {COPY.lostNote(versionNo)}
              </p>
              {actions}
            </div>
          ) : versionWarnings.length > 0 ? (
            <div className="flex flex-col items-center gap-1">
              <ul className="max-w-xl space-y-0.5 text-center text-2xs text-amber-700" role="note">
                {versionWarnings.map((w) => (
                  <li key={w}>⚠ {w}</li>
                ))}
              </ul>
              {actions}
            </div>
          ) : (
            actions
          )
        ) : icons ? (
          underIcons
        ) : warnings.length > 0 ? (
          <ul className="max-w-xl space-y-0.5 text-center text-2xs text-amber-700">
            {warnings.map((w) => (
              <li key={w}>⚠ {w}</li>
            ))}
          </ul>
        ) : null}
      </div>
      {strip && <div className="flex max-h-[45dvh] min-h-0 shrink-0 flex-col lg:max-h-[42%]">{strip}</div>}
    </section>
  );
}

function ElapsedLine({ since, hint }: { since: number; hint: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);
  // The counter is for the eye only: it sits in a status region, which would
  // read it out every second.
  return (
    <span className="text-2xs text-white/60">
      <span aria-hidden>
        <span className="font-mono tabular-nums">{mmss((now - since) / 1000)}</span> ·{" "}
      </span>
      {hint}
    </span>
  );
}
