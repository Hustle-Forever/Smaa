/**
 * Video Editor · Text inspector
 *
 * Shown in place of the trim/audio inspector when the selected clip is a text
 * layer. Edits content + font (curated Google fonts + brand fonts from the
 * Assets Library) + size/color/weight/align/background, plus duration and the
 * shared keyframe editor (text animates opacity/scale/x/y/rotation).
 */
import { useVideoEditorStore } from "../../../lib/store";
import { CURATED_FONTS } from "../../../lib/fonts";
import type { VideoEditClip } from "../../../lib/types";
import { KeyframeEditor } from "./KeyframeEditor";
import { useBrandFonts } from "./useBrandFonts";

const FPS = 30;
const WEIGHTS = [400, 600, 700, 800, 900];

export function TextInspector({ clip }: { clip: VideoEditClip }) {
  const setClipText = useVideoEditorStore((s) => s.setClipText);
  const updateClip = useVideoEditorStore((s) => s.updateClip);
  const removeClip = useVideoEditorStore((s) => s.removeClip);
  const brandFonts = useBrandFonts();
  const t = clip.text;
  if (!t) return null;

  const durSec = (clip.durationInFrames / FPS).toFixed(1);

  // The picker value is the family; brand fonts resolve to their file URL.
  const onPickFont = (family: string) => {
    const brand = brandFonts.find((b) => b.family === family);
    setClipText(clip.id, { fontFamily: family, fontUrl: brand ? brand.file_url : null });
  };

  return (
    <div className="p-3 space-y-4">
      <div className="space-y-0.5">
        <span className="eyebrow">Text</span>
        <p className="text-[12px] text-fg/85 font-mono truncate">{durSec}s · {t.fontFamily}</p>
      </div>

      {/* Content */}
      <div className="space-y-1.5">
        <span className="eyebrow">Words</span>
        <textarea
          value={t.content}
          onChange={(e) => setClipText(clip.id, { content: e.target.value })}
          rows={3}
          className="w-full rounded-md border border-border bg-bg px-2 py-1.5 text-[12px] resize-y focus:outline-none focus:border-accent"
          placeholder="Type what it should say…"
        />
      </div>

      {/* Font */}
      <div className="space-y-1.5">
        <span className="eyebrow">Font</span>
        <select
          value={t.fontFamily}
          onChange={(e) => onPickFont(e.target.value)}
          className="w-full rounded-md border border-border bg-bg px-2 py-1 text-[12px] focus:outline-none focus:border-accent"
        >
          <optgroup label="Google fonts">
            {CURATED_FONTS.map((f) => (
              <option key={f.family} value={f.family}>{f.label}</option>
            ))}
          </optgroup>
          {brandFonts.length > 0 && (
            <optgroup label="Brand fonts">
              {brandFonts.map((f) => (
                <option key={f.id} value={f.family}>{f.family}</option>
              ))}
            </optgroup>
          )}
        </select>
      </div>

      {/* Size + color */}
      <div className="grid grid-cols-2 gap-2">
        <div className="space-y-1">
          <Row label="Size" value={`${t.fontSize}px`} />
          <input
            type="range"
            min={24}
            max={320}
            value={t.fontSize}
            onChange={(e) => setClipText(clip.id, { fontSize: Number(e.target.value) })}
            className="w-full"
          />
        </div>
        <div className="space-y-1">
          <span className="eyebrow">Color</span>
          <input
            type="color"
            value={t.color}
            onChange={(e) => setClipText(clip.id, { color: e.target.value })}
            className="w-full h-7 rounded-md border border-border bg-bg cursor-pointer"
          />
        </div>
      </div>

      {/* Weight + align */}
      <div className="space-y-2">
        <span className="eyebrow">Thickness</span>
        <div className="flex gap-1">
          {WEIGHTS.map((w) => (
            <button
              key={w}
              onClick={() => setClipText(clip.id, { fontWeight: w })}
              className={[
                "flex-1 text-[10.5px] py-0.5 rounded border transition-colors",
                (t.fontWeight || 700) === w ? "border-accent bg-accent/8 text-fg" : "border-border text-muted hover:bg-bg-muted",
              ].join(" ")}
            >
              {w}
            </button>
          ))}
        </div>
        <span className="eyebrow">Align</span>
        <div className="flex gap-1">
          {(["left", "center", "right"] as const).map((a) => (
            <button
              key={a}
              onClick={() => setClipText(clip.id, { textAlign: a })}
              className={[
                "flex-1 text-2xs py-0.5 rounded border capitalize transition-colors",
                (t.textAlign || "center") === a ? "border-accent bg-accent/8 text-fg" : "border-border text-muted hover:bg-bg-muted",
              ].join(" ")}
            >
              {a}
            </button>
          ))}
        </div>
      </div>

      {/* Background pill */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <span className="eyebrow">Background</span>
          <button
            onClick={() => setClipText(clip.id, { background: t.background ? null : "#000000" })}
            className={[
              "text-2xs px-2 py-0.5 rounded border transition-colors",
              t.background ? "border-accent bg-accent/8 text-fg" : "border-border text-muted hover:bg-bg-muted",
            ].join(" ")}
          >
            {t.background ? "On" : "Off"}
          </button>
        </div>
        {t.background && (
          <input
            type="color"
            value={t.background}
            onChange={(e) => setClipText(clip.id, { background: e.target.value })}
            className="w-full h-7 rounded-md border border-border bg-bg cursor-pointer"
          />
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

      {/* Keyframes — opacity/scale/x/y/rotate */}
      <KeyframeEditor clip={clip} isAudio={false} />

      <button onClick={() => removeClip(clip.id)} className="btn btn-ghost text-xs text-danger w-full">
        Delete this text
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
