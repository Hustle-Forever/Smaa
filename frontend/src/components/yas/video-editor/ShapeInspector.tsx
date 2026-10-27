/**
 * Video Editor · Shape inspector
 *
 * Shown when the selected clip is a shape layer. Edits variant + fill + stroke +
 * size (+ corner radius for rects), duration, and the shared keyframe editor
 * (shapes animate opacity/scale/x/y/rotation).
 */
import { useVideoEditorStore } from "../../../lib/store";
import type { VideoEditClip, VideoEditShapeVariant } from "../../../lib/types";
import { KeyframeEditor } from "./KeyframeEditor";

const FPS = 30;
const VARIANTS: { value: VideoEditShapeVariant; glyph: string; label: string }[] = [
  { value: "rect", glyph: "▭", label: "Rectangle" },
  { value: "ellipse", glyph: "⬭", label: "Ellipse" },
  { value: "line", glyph: "▬", label: "Line" },
  { value: "triangle", glyph: "△", label: "Triangle" },
  { value: "star", glyph: "★", label: "Star" },
];

export function ShapeInspector({ clip }: { clip: VideoEditClip }) {
  const setClipShape = useVideoEditorStore((s) => s.setClipShape);
  const updateClip = useVideoEditorStore((s) => s.updateClip);
  const removeClip = useVideoEditorStore((s) => s.removeClip);
  const sh = clip.shape;
  if (!sh) return null;

  const durSec = (clip.durationInFrames / FPS).toFixed(1);
  const isLine = sh.variant === "line";

  return (
    <div className="p-3 space-y-4">
      <div className="space-y-0.5">
        <span className="eyebrow">Shape</span>
        <p className="text-[12px] text-fg/85 font-mono truncate">{durSec}s · {sh.variant}</p>
      </div>

      {/* Variant */}
      <div className="space-y-1.5">
        <span className="eyebrow">Shape</span>
        <div className="grid grid-cols-5 gap-1">
          {VARIANTS.map((v) => (
            <button
              key={v.value}
              onClick={() => setClipShape(clip.id, { variant: v.value })}
              title={v.label}
              className={[
                "aspect-square flex items-center justify-center rounded border text-[15px] transition-colors",
                sh.variant === v.value ? "border-accent bg-accent/8 text-fg" : "border-border text-muted hover:bg-bg-muted",
              ].join(" ")}
            >
              {v.glyph}
            </button>
          ))}
        </div>
      </div>

      {/* Fill */}
      <div className="grid grid-cols-2 gap-2">
        <div className="space-y-1">
          <span className="eyebrow">Fill</span>
          <input
            type="color"
            value={sh.fill}
            onChange={(e) => setClipShape(clip.id, { fill: e.target.value })}
            className="w-full h-7 rounded-md border border-border bg-bg cursor-pointer"
          />
        </div>
        <div className="space-y-1">
          <div className="flex items-center justify-between">
            <span className="eyebrow">Outline</span>
            <button
              onClick={() => setClipShape(clip.id, sh.stroke ? { stroke: null, strokeWidth: 0 } : { stroke: "#ffffff", strokeWidth: 6 })}
              className={[
                "text-3xs px-1.5 py-0.5 rounded border transition-colors",
                sh.stroke ? "border-accent bg-accent/8 text-fg" : "border-border text-muted hover:bg-bg-muted",
              ].join(" ")}
            >
              {sh.stroke ? "On" : "Off"}
            </button>
          </div>
          <input
            type="color"
            value={sh.stroke || "#ffffff"}
            disabled={!sh.stroke}
            onChange={(e) => setClipShape(clip.id, { stroke: e.target.value })}
            className="w-full h-7 rounded-md border border-border bg-bg cursor-pointer disabled:opacity-40"
          />
        </div>
      </div>

      {sh.stroke && (
        <div className="space-y-1">
          <Row label="Thickness" value={`${sh.strokeWidth ?? 0}px`} />
          <input
            type="range"
            min={1}
            max={48}
            value={sh.strokeWidth ?? 6}
            onChange={(e) => setClipShape(clip.id, { strokeWidth: Number(e.target.value) })}
            className="w-full"
          />
        </div>
      )}

      {/* Size */}
      <div className="space-y-2">
        <div className="space-y-1">
          <Row label={isLine ? "Length" : "Width"} value={`${sh.width}px`} />
          <input
            type="range"
            min={20}
            max={1920}
            value={sh.width}
            onChange={(e) => setClipShape(clip.id, { width: Number(e.target.value) })}
            className="w-full"
          />
        </div>
        {!isLine && (
          <div className="space-y-1">
            <Row label="Height" value={`${sh.height}px`} />
            <input
              type="range"
              min={20}
              max={1920}
              value={sh.height}
              onChange={(e) => setClipShape(clip.id, { height: Number(e.target.value) })}
              className="w-full"
            />
          </div>
        )}
        {sh.variant === "rect" && (
          <div className="space-y-1">
            <Row label="Corner radius" value={`${sh.cornerRadius ?? 0}px`} />
            <input
              type="range"
              min={0}
              max={400}
              value={sh.cornerRadius ?? 0}
              onChange={(e) => setClipShape(clip.id, { cornerRadius: Number(e.target.value) })}
              className="w-full"
            />
          </div>
        )}
      </div>

      {/* Duration */}
      <div className="space-y-1.5 pt-1 border-t border-border">
        <Row label="Duration" value={`${durSec}s`} />
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

      {/* Keyframes */}
      <KeyframeEditor clip={clip} isAudio={false} />

      <button onClick={() => removeClip(clip.id)} className="btn btn-ghost text-xs text-danger w-full">
        Delete this shape
      </button>
    </div>
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
