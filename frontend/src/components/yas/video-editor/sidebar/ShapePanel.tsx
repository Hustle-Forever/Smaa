/**
 * Video Editor · Shapes tab (sidebar)
 *
 * A palette of shapes. Clicking one drops a centered shape clip at the playhead
 * on the overlay track (above the video). Tune fill/stroke/size in the Shape
 * inspector on the right.
 */
import { useVideoEditorStore } from "../../../../lib/store";
import type { VideoEditShapeVariant } from "../../../../lib/types";
import { attachmentDragProps } from "./attachmentDnd";

const SHAPES: { value: VideoEditShapeVariant; glyph: string; label: string }[] = [
  { value: "rect", glyph: "▭", label: "Rectangle" },
  { value: "ellipse", glyph: "⬭", label: "Ellipse" },
  { value: "line", glyph: "▬", label: "Line" },
  { value: "triangle", glyph: "△", label: "Triangle" },
  { value: "star", glyph: "★", label: "Star" },
];

export function ShapePanel() {
  const addShapeClip = useVideoEditorStore((s) => s.addShapeClip);

  return (
    <div className="h-full flex flex-col">
      <div className="shrink-0 p-2.5 border-b border-border">
        <span className="eyebrow">Shapes</span>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-2">
        <p className="text-[10.5px] text-muted px-1 pb-1.5">Click one to add it</p>
        <div className="grid grid-cols-2 gap-1.5">
          {SHAPES.map((s) => (
            <button
              key={s.value}
              onClick={() => addShapeClip(s.value)}
              title="Click to add it, or drag it into the chat"
              {...attachmentDragProps({ kind: "shape", name: s.label, variant: s.value })}
              className="aspect-[4/3] flex flex-col items-center justify-center gap-1 rounded-lg border border-border bg-bg hover:bg-bg-muted hover:border-accent/50 transition-colors cursor-grab active:cursor-grabbing"
            >
              <span className="text-[26px] leading-none text-accent">{s.glyph}</span>
              <span className="text-3xs text-fg/70">{s.label}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
