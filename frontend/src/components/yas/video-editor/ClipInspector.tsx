/**
 * Video Editor · Inspector
 *
 * Properties for the selected clip:
 *   - in/out trim (frame-accurate, shared with the timeline edges)
 *   - video: opacity (for overlay blending) · mute · detach audio
 *   - audio: volume
 *   - remove
 * Opacity/volume are written as a single static keyframe — the Phase F keyframe
 * editor will animate them over time.
 */
import { useVideoEditorStore } from "../../../lib/store";
import type { TransitionPreset } from "../../../lib/types";
import { ImageInspector } from "./ImageInspector";
import { KeyframeEditor } from "./KeyframeEditor";
import { ShapeInspector } from "./ShapeInspector";
import { TextInspector } from "./TextInspector";

const TRANSITION_OPTIONS: { value: TransitionPreset; label: string }[] = [
  { value: "none", label: "None" },
  { value: "fade", label: "Fade" },
  { value: "zoom", label: "Zoom in" },
  { value: "blur", label: "Blur" },
  { value: "slide", label: "Slide" },
  { value: "slideLeft", label: "Slide left" },
  { value: "slideRight", label: "Slide right" },
  { value: "slideUp", label: "Slide up" },
  { value: "slideDown", label: "Slide down" },
  { value: "wipe", label: "Wipe" },
  { value: "flip", label: "Flip" },
  { value: "clockWipe", label: "Clock wipe" },
  { value: "iris", label: "Iris" },
];

function fmt(frames: number, fps = 30): string {
  return `${(frames / fps).toFixed(2)}s`;
}

// Raw source kinds ("upload", "animation") used to render straight into the
// panel. They are our words for our plumbing, not the user's.
const SOURCE_LABEL: Record<string, string> = {
  upload: "Your upload",
  animation: "Animation",
  composition: "Video",
  poster: "Poster",
};

export function ClipInspector() {
  const selection = useVideoEditorStore((s) => s.selection);
  const clips = useVideoEditorStore((s) => s.clips);
  const tracks = useVideoEditorStore((s) => s.tracks);
  const fps = useVideoEditorStore((s) => s.project?.fps || 30);
  const trimClipEdge = useVideoEditorStore((s) => s.trimClipEdge);
  const removeClip = useVideoEditorStore((s) => s.removeClip);
  const updateClip = useVideoEditorStore((s) => s.updateClip);
  const detachAudio = useVideoEditorStore((s) => s.detachAudio);
  const setTransition = useVideoEditorStore((s) => s.setTransition);
  const applyCanvasTransform = useVideoEditorStore((s) => s.applyCanvasTransform);

  const clipId = selection && "clipId" in selection ? selection.clipId : null;
  const clip = clipId ? clips.find((c) => c.id === clipId) : null;

  if (!clip) {
    return (
      <Panel>
        <p className="text-[12px] text-muted text-center px-4 py-8">
          Pick a clip on the timeline to change it.
        </p>
      </Panel>
    );
  }

  // Text layers get a dedicated inspector (content/font/size/color/keyframes).
  if (clip.kind === "text") {
    return (
      <Panel>
        <TextInspector clip={clip} />
      </Panel>
    );
  }
  // Shape layers get their own inspector (variant/fill/stroke/size/keyframes).
  if (clip.kind === "shape") {
    return (
      <Panel>
        <ShapeInspector clip={clip} />
      </Panel>
    );
  }
  // Image/sticker layers get their own inspector (fit/duration/keyframes).
  if (clip.kind === "image") {
    return (
      <Panel>
        <ImageInspector clip={clip} />
      </Panel>
    );
  }

  const track = tracks.find((t) => t.id === clip.trackId);
  const isAudio = track?.kind === "audio";
  const natural = clip.source?.naturalDurationInFrames || clip.trimAfter || 1;

  // Eligible for a transition-in if there's a preceding adjacent/overlapping clip.
  const prevClip = clips
    .filter((c) => c.trackId === clip.trackId && c.id !== clip.id && c.from < clip.from)
    .sort((a, b) => b.from - a.from)[0];
  const transitionEligible =
    !isAudio && !!prevClip && clip.from <= prevClip.from + prevClip.durationInFrames;
  const tin = clip.transitionIn;

  return (
    <Panel>
      <div className="p-3 space-y-4">
        <div className="space-y-0.5">
          <span className="eyebrow">{isAudio ? "Sound" : "Video"}</span>
          <p className="text-[12px] text-fg/85 truncate">
            {SOURCE_LABEL[clip.source?.kind || ""] || "Clip"} · {fmt(clip.durationInFrames, fps)}
          </p>
        </div>

        <div className="space-y-2">
          <Row label="Start at" value={fmt(clip.trimBefore, fps)} />
          <input
            type="range"
            min={0}
            max={natural - 1}
            value={clip.trimBefore}
            onChange={(e) => trimClipEdge(clip.id, "left", Number(e.target.value) - clip.trimBefore)}
            className="w-full"
          />
          <Row label="End at" value={fmt(clip.trimAfter, fps)} />
          <input
            type="range"
            min={1}
            max={natural}
            value={clip.trimAfter}
            onChange={(e) => trimClipEdge(clip.id, "right", Number(e.target.value) - clip.trimAfter)}
            className="w-full"
          />
        </div>

        {/* Frame & fit (video only) — fix aspect mismatch: fill/fit + background.
            Pan/zoom directly on the canvas (drag / corner-resize). */}
        {!isAudio && (
          <div className="space-y-2 pt-1 border-t border-border">
            <span className="eyebrow">How it fits</span>
            <div className="flex gap-1">
              {([
                { v: "cover", label: "Fill" },
                { v: "contain", label: "Fit" },
              ] as const).map((o) => (
                <button
                  key={o.v}
                  onClick={() => updateClip(clip.id, { fit: o.v })}
                  className={[
                    "flex-1 text-2xs py-0.5 rounded border transition-colors",
                    (clip.fit || "cover") === o.v ? "border-accent bg-accent/8 text-fg" : "border-border text-muted hover:bg-bg-muted",
                  ].join(" ")}
                >
                  {o.label}
                </button>
              ))}
            </div>
            <div className="flex gap-1">
              <button
                onClick={() => {
                  updateClip(clip.id, { fit: "cover" });
                  applyCanvasTransform(clip.id, { x: 0, y: 0, scale: 1 }, 0);
                }}
                className="flex-1 text-[10.5px] py-0.5 rounded border border-border text-muted hover:bg-bg-muted"
                title="Fill the whole frame and undo any move or zoom"
              >
                Fill the frame
              </button>
              <button
                onClick={() => {
                  updateClip(clip.id, { fit: "contain" });
                  applyCanvasTransform(clip.id, { x: 0, y: 0, scale: 1 }, 0);
                }}
                className="flex-1 text-[10.5px] py-0.5 rounded border border-border text-muted hover:bg-bg-muted"
                title="Show all of the video, with bars around it"
              >
                Fit in the frame
              </button>
            </div>
            <div className="flex items-center justify-between gap-2">
              <span className="text-[10.5px] text-muted">Behind the bars</span>
              <select
                value={clip.background === "blur" ? "blur" : clip.background ? "color" : "none"}
                onChange={(e) => {
                  const v = e.target.value;
                  updateClip(clip.id, { background: v === "blur" ? "blur" : v === "color" ? "#000000" : null });
                }}
                className="rounded-md border border-border bg-bg px-2 py-0.5 text-2xs focus:outline-none focus:border-accent"
              >
                <option value="none">None</option>
                <option value="blur">Blurred video</option>
                <option value="color">A colour</option>
              </select>
            </div>
            {clip.background && clip.background !== "blur" && (
              <input
                type="color"
                value={clip.background}
                onChange={(e) => updateClip(clip.id, { background: e.target.value })}
                className="w-full h-7 rounded-md border border-border bg-bg cursor-pointer"
              />
            )}
            <p className="text-3xs text-muted">Drag the video in the preview to move it. Drag a corner to zoom.</p>
          </div>
        )}

        {/* Keyframes — opacity/scale/x/y/rotate (video) or volume (audio) */}
        <KeyframeEditor clip={clip} isAudio={isAudio} />

        {/* Transition in (from the previous clip on this track) */}
        {transitionEligible && (
          <div className="space-y-2 pt-1 border-t border-border">
            <span className="eyebrow">How it starts</span>
            <select
              value={tin?.preset || "none"}
              onChange={(e) =>
                setTransition(clip.id, e.target.value as TransitionPreset, tin?.durationInFrames || 15, tin?.timing || "linear")
              }
              className="w-full rounded-md border border-border bg-bg px-2 py-1 text-[12px] focus:outline-none focus:border-accent"
            >
              {TRANSITION_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            {tin && tin.preset !== "none" && (
              <div className="space-y-1.5">
                <Row label="How long" value={`${(tin.durationInFrames / fps).toFixed(2)}s`} />
                <input
                  type="range"
                  min={3}
                  max={45}
                  value={tin.durationInFrames}
                  onChange={(e) => setTransition(clip.id, tin.preset, Number(e.target.value), tin.timing)}
                  className="w-full"
                />
                <div className="flex gap-1">
                  {([
                    { key: "linear", label: "Even" },
                    { key: "spring", label: "Bouncy" },
                  ] as const).map(({ key: tm, label }) => (
                    <button
                      key={tm}
                      onClick={() => setTransition(clip.id, tin.preset, tin.durationInFrames, tm)}
                      className={[
                        "flex-1 text-[10.5px] py-0.5 rounded border transition-colors",
                        (tin.timing || "linear") === tm
                          ? "border-accent bg-accent/8 text-fg"
                          : "border-border text-muted hover:bg-bg-muted",
                      ].join(" ")}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Audio controls */}
        <div className="space-y-2 pt-1 border-t border-border">
          <div className="flex items-center justify-between">
            <span className="eyebrow">Sound</span>
            <button
              onClick={() => updateClip(clip.id, { muted: !clip.muted })}
              className={[
                "text-2xs px-2 py-0.5 rounded border transition-colors",
                clip.muted
                  ? "border-danger/40 bg-danger/10 text-danger"
                  : "border-border text-muted hover:bg-bg-muted",
              ].join(" ")}
            >
              {clip.muted ? "🔇 Off" : "🔊 On"}
            </button>
          </div>
          {!isAudio && (
            <button
              onClick={() => detachAudio(clip.id)}
              disabled={clip.muted}
              className="btn btn-ghost text-xs w-full"
              title="Put this clip's sound on its own row so you can change it on its own"
            >
              {clip.muted ? "Sound is off" : "↧ Split the sound onto its own row"}
            </button>
          )}
        </div>

        <button onClick={() => removeClip(clip.id)} className="btn btn-ghost text-xs text-danger w-full">
          Delete this clip
        </button>
      </div>
    </Panel>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="eyebrow">{label}</span>
      <span className="text-2xs font-mono text-muted">{value}</span>
    </div>
  );
}

function Panel({ children }: { children: React.ReactNode }) {
  // Chrome (rounded border + bg) is owned by the RightSidebar tab container now.
  return <div className="h-full w-full overflow-y-auto">{children}</div>;
}
