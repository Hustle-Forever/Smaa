/**
 * Video Editor · keyframe editor (Inspector section)
 *
 * One row per animatable property. The slider sets the value AT the playhead's
 * clip-local frame — a single keyframe = constant, two+ = animation. The ◆
 * button adds/removes a keyframe at the playhead; a mini lane (when keyframes
 * exist) shows draggable diamonds.
 *
 * On screen a keyframe is called a MARK, never a keyframe: it is the one term
 * here that no non-editor would know, and the ◆ already looks like a mark.
 *
 *   - video clips: opacity · scale · X · Y · rotate  (Ken Burns = scale + X/Y)
 *   - audio clips: volume
 */
import { sampleKeyframes } from "video-edit-schema";

import { useVideoEditorStore, type KeyframeProp } from "../../../lib/store";
import type { VideoEditClip } from "../../../lib/types";
import { KeyframeLane } from "./KeyframeLane";

type PropDef = {
  key: KeyframeProp;
  label: string;
  min: number;
  max: number;
  step: number;
  def: number;
  fmt: (v: number) => string;
};

export function KeyframeEditor({ clip, isAudio }: { clip: VideoEditClip; isAudio: boolean }) {
  const playhead = useVideoEditorStore((s) => s.playheadFrame);
  const project = useVideoEditorStore((s) => s.project);
  const setKeyframe = useVideoEditorStore((s) => s.setKeyframe);
  const removeKeyframe = useVideoEditorStore((s) => s.removeKeyframe);
  const animateClip = useVideoEditorStore((s) => s.animateClip);
  const runAi = useVideoEditorStore((s) => s.runAi);
  const aiBusy = useVideoEditorStore((s) => s.aiBusy);

  // One-click animate is for overlay layers only: text + sticker (image).
  const canAnimate = clip.kind === "text" || clip.kind === "image";

  const dur = Math.max(1, clip.durationInFrames);
  const overClip = playhead >= clip.from && playhead < clip.from + dur;
  const localFrame = Math.max(0, Math.min(playhead - clip.from, dur - 1));
  const halfW = Math.round((project?.width || 1080) / 2);
  const halfH = Math.round((project?.height || 1920) / 2);

  const props: PropDef[] = isAudio
    ? [{ key: "volume", label: "Volume", min: 0, max: 2, step: 0.05, def: 1, fmt: (v) => `${Math.round(v * 100)}%` }]
    : [
        { key: "opacity", label: "Fade", min: 0, max: 1, step: 0.05, def: 1, fmt: (v) => `${Math.round(v * 100)}%` },
        { key: "scale", label: "Size", min: 0.2, max: 3, step: 0.05, def: 1, fmt: (v) => `${v.toFixed(2)}×` },
        { key: "x", label: "Left/right", min: -halfW, max: halfW, step: 5, def: 0, fmt: (v) => `${Math.round(v)}` },
        { key: "y", label: "Up/down", min: -halfH, max: halfH, step: 5, def: 0, fmt: (v) => `${Math.round(v)}` },
        { key: "rotation", label: "Turn", min: -180, max: 180, step: 1, def: 0, fmt: (v) => `${Math.round(v)}°` },
      ];

  return (
    <div className="space-y-2.5 pt-1 border-t border-border">
      <div className="flex items-center justify-between">
        <span className="eyebrow">{isAudio ? "Volume" : "Size and position"}</span>
        <div className="flex items-center gap-1.5">
          {!overClip && <span className="text-[9px] text-muted">not on this clip</span>}
          {canAnimate && (
            <button
              onClick={() => animateClip(clip.id)}
              title="Give it a nice way to appear and disappear (different every time)"
              className="text-3xs px-1.5 py-0.5 rounded border border-border text-muted hover:bg-bg-muted transition-colors"
            >
              ✦ Animate
            </button>
          )}
          {canAnimate && (
            <button
              onClick={() => void runAi("sfx", { clip_id: clip.id })}
              disabled={aiBusy}
              title="Make a sound effect for this clip (different every time)"
              className="text-3xs px-1.5 py-0.5 rounded border border-border text-muted hover:bg-bg-muted transition-colors disabled:opacity-50"
            >
              🔊 Sound effect
            </button>
          )}
        </div>
      </div>
      {props.map((p) => {
        const kfs = clip.keyframes?.[p.key] || [];
        const value = sampleKeyframes(kfs, localFrame, p.def);
        const hasKf = kfs.some((k) => k.f === localFrame);
        return (
          <div key={p.key} className="space-y-1">
            <div className="flex items-center gap-1.5">
              <button
                onClick={() =>
                  hasKf ? removeKeyframe(clip.id, p.key, localFrame) : setKeyframe(clip.id, p.key, localFrame, value)
                }
                title={hasKf ? "Remove the mark here" : "Mark this moment, so the setting can change as the clip plays"}
                className={["text-2xs leading-none w-4 shrink-0", hasKf ? "text-cyan-500" : "text-muted hover:text-fg"].join(" ")}
              >
                ◆
              </button>
              <span className="text-[10.5px] w-16 shrink-0">{p.label}</span>
              <input
                type="range"
                min={p.min}
                max={p.max}
                step={p.step}
                value={value}
                onChange={(e) => setKeyframe(clip.id, p.key, localFrame, Number(e.target.value))}
                className="flex-1 min-w-0"
              />
              <span className="text-3xs font-mono w-9 text-right text-muted">{p.fmt(value)}</span>
            </div>
            {kfs.length > 0 && (
              <div className="pl-[26px]">
                <KeyframeLane
                  clipId={clip.id}
                  prop={p.key}
                  kfs={kfs}
                  dur={dur}
                  localFrame={overClip ? localFrame : null}
                  clipFrom={clip.from}
                />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
