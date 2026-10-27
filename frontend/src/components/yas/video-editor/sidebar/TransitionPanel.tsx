/**
 * Video Editor · Transitions tab — preset palette. Click a preset to apply it
 * to the selected clip's in-boundary (needs a preceding adjacent clip).
 */
import { useVideoEditorStore } from "../../../../lib/store";
import type { TransitionPreset } from "../../../../lib/types";

const PRESETS: { key: TransitionPreset; label: string }[] = [
  { key: "fade", label: "Fade" },
  { key: "slide", label: "Slide" },
  { key: "wipe", label: "Wipe" },
  { key: "flip", label: "Flip" },
  { key: "clockWipe", label: "Clock" },
  { key: "iris", label: "Iris" },
];

export function TransitionPanel() {
  const selection = useVideoEditorStore((s) => s.selection);
  const clips = useVideoEditorStore((s) => s.clips);
  const setTransition = useVideoEditorStore((s) => s.setTransition);

  const clipId = selection && "clipId" in selection ? selection.clipId : null;
  const clip = clipId ? clips.find((c) => c.id === clipId) : null;
  const prev = clip
    ? clips.filter((c) => c.trackId === clip.trackId && c.id !== clip.id && c.from < clip.from).sort((a, b) => b.from - a.from)[0]
    : null;
  const eligible = !!clip && !!prev && clip.from <= prev.from + prev.durationInFrames;
  const current = clip?.transitionIn?.preset;

  return (
    <div className="h-full flex flex-col">
      <div className="shrink-0 p-2.5 border-b border-border">
        <span className="eyebrow">Transitions</span>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-2.5 space-y-2">
        {!clip && <p className="text-2xs text-muted text-center py-6">Pick a clip first.</p>}
        {clip && !eligible && (
          <p className="text-2xs text-muted text-center py-6">
            Nothing comes right before this clip, so there is nothing to blend from.
          </p>
        )}
        {clip && eligible && (
          <>
            <div className="grid grid-cols-2 gap-2">
              <PresetCard label="None" active={!current || current === "none"} onClick={() => setTransition(clip.id, "none", 15)} />
              {PRESETS.map((p) => (
                <PresetCard key={p.key} label={p.label} active={current === p.key} onClick={() => setTransition(clip.id, p.key, clip.transitionIn?.durationInFrames || 15, clip.transitionIn?.timing || "linear")} />
              ))}
            </div>
            <p className="text-[9.5px] text-muted pt-1">Set how long it takes on the right.</p>
          </>
        )}
      </div>
    </div>
  );
}

function PresetCard({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={[
        "rounded-lg border h-14 flex items-center justify-center text-[12px] font-medium transition-colors",
        active ? "border-accent bg-accent/10 text-fg" : "border-border bg-bg-subtle text-muted hover:bg-bg-muted hover:text-fg",
      ].join(" ")}
    >
      <span className="flex flex-col items-center gap-1">
        <span className="text-[15px]">⇄</span>
        {label}
      </span>
    </button>
  );
}
