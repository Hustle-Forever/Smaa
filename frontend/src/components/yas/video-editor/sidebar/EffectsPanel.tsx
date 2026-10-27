/**
 * Video Editor · Effects/Filters tab — brightness / contrast / saturate / blur
 * for the selected clip. Writes clip.filters (the composition applies them as a
 * CSS filter in both the Player and the export).
 */
import { useVideoEditorStore } from "../../../../lib/store";

const FILTERS: { key: "brightness" | "contrast" | "saturate" | "blur"; label: string; min: number; max: number; step: number; def: number; fmt: (v: number) => string }[] = [
  { key: "brightness", label: "Brightness", min: 0, max: 2, step: 0.05, def: 1, fmt: (v) => `${Math.round(v * 100)}%` },
  { key: "contrast", label: "Contrast", min: 0, max: 2, step: 0.05, def: 1, fmt: (v) => `${Math.round(v * 100)}%` },
  { key: "saturate", label: "Saturation", min: 0, max: 2, step: 0.05, def: 1, fmt: (v) => `${Math.round(v * 100)}%` },
  { key: "blur", label: "Blur", min: 0, max: 40, step: 1, def: 0, fmt: (v) => `${Math.round(v)}px` },
];

export function EffectsPanel() {
  const selection = useVideoEditorStore((s) => s.selection);
  const clips = useVideoEditorStore((s) => s.clips);
  const setClipFilters = useVideoEditorStore((s) => s.setClipFilters);

  const clipId = selection && "clipId" in selection ? selection.clipId : null;
  const clip = clipId ? clips.find((c) => c.id === clipId) : null;

  return (
    <div className="h-full flex flex-col">
      <div className="shrink-0 p-2.5 border-b border-border flex items-center justify-between">
        <span className="eyebrow">Effects</span>
        {clip && (
          <button
            onClick={() => setClipFilters(clip.id, { brightness: 1, contrast: 1, saturate: 1, blur: 0 })}
            className="text-3xs text-muted hover:text-fg"
          >
            Reset
          </button>
        )}
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-4">
        {!clip ? (
          <p className="text-2xs text-muted text-center py-6">Pick a clip first.</p>
        ) : (
          FILTERS.map((f) => {
            const value = clip.filters?.[f.key] ?? f.def;
            return (
              <div key={f.key} className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <span className="text-2xs text-fg/85">{f.label}</span>
                  <span className="text-3xs font-mono text-muted">{f.fmt(value)}</span>
                </div>
                <input
                  type="range"
                  min={f.min}
                  max={f.max}
                  step={f.step}
                  value={value}
                  onChange={(e) => setClipFilters(clip.id, { [f.key]: Number(e.target.value) })}
                  className="w-full"
                />
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
