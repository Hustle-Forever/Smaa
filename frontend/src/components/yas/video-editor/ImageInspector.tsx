/**
 * Video Editor · Image / sticker inspector
 *
 * Shown when the selected clip is an image layer (an Assets-Library image). Fit
 * (contain/cover) + duration + the shared keyframe editor (size/position via
 * scale/x/y, plus opacity) + remove.
 */
import { api } from "../../../lib/api";
import { useVideoEditorStore } from "../../../lib/store";
import type { VideoEditClip } from "../../../lib/types";
import { KeyframeEditor } from "./KeyframeEditor";

const FPS = 30;

export function ImageInspector({ clip }: { clip: VideoEditClip }) {
  const updateClip = useVideoEditorStore((s) => s.updateClip);
  const removeClip = useVideoEditorStore((s) => s.removeClip);
  const assetId = clip.source?.asset_id ?? null;
  const durSec = (clip.durationInFrames / FPS).toFixed(1);

  return (
    <div className="p-3 space-y-4">
      <div className="space-y-0.5">
        <span className="eyebrow">Picture</span>
        <p className="text-[12px] text-fg/85 font-mono truncate">{durSec}s · contain</p>
      </div>

      {assetId != null && (
        <div className="rounded-lg border border-border bg-black/40 overflow-hidden">
          <img src={api.producerAssets.previewUrl(assetId)} alt="" className="w-full h-28 object-contain" />
        </div>
      )}

      {/* Fit */}
      <div className="space-y-1.5">
        <span className="eyebrow">Fit</span>
        <div className="flex gap-1">
          {(["contain", "cover"] as const).map((f) => (
            <button
              key={f}
              onClick={() => updateClip(clip.id, { fit: f })}
              className={[
                "flex-1 text-2xs py-0.5 rounded border capitalize transition-colors",
                (clip.fit || "contain") === f ? "border-accent bg-accent/8 text-fg" : "border-border text-muted hover:bg-bg-muted",
              ].join(" ")}
            >
              {f}
            </button>
          ))}
        </div>
        <p className="text-3xs text-muted">Set its size and place below.</p>
      </div>

      {/* Duration */}
      <div className="space-y-1.5 pt-1 border-t border-border">
        <div className="flex items-center justify-between">
          <span className="eyebrow">How long</span>
          <span className="text-2xs font-mono text-muted">{durSec}s</span>
        </div>
        <input
          type="range"
          min={15}
          max={450}
          value={clip.durationInFrames}
          onChange={(e) => {
            const d = Number(e.target.value);
            updateClip(clip.id, { durationInFrames: d, trimAfter: d });
          }}
          className="w-full"
        />
      </div>

      {/* Keyframes — scale/x/y for sizing+placement, opacity for fades */}
      <KeyframeEditor clip={clip} isAudio={false} />

      <button onClick={() => removeClip(clip.id)} className="btn btn-ghost text-xs text-danger w-full">
        Delete this picture
      </button>
    </div>
  );
}
