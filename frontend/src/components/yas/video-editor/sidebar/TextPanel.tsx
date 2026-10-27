/**
 * Video Editor · Text tab (sidebar)
 *
 * "Add text" + a few style presets. Each drops a centered text overlay clip at
 * the playhead on a dedicated overlay track (above the video). Style + content
 * are then tuned in the Text inspector on the right.
 */
import { useRef, useState } from "react";

import { api } from "../../../../lib/api";
import { useVideoEditorStore } from "../../../../lib/store";
import { DEFAULT_TEXT_FONT } from "../../../../lib/fonts";
import type { VideoEditClipText } from "../../../../lib/types";
import { refreshBrandFonts, useBrandFonts } from "../useBrandFonts";
import { attachmentDragProps } from "./attachmentDnd";

const PRESETS: { label: string; content: string; sample: string; preset: Partial<VideoEditClipText> }[] = [
  {
    label: "Heading",
    content: "Heading",
    sample: "font-bold text-[18px]",
    preset: { fontFamily: "Anton", fontSize: 140, fontWeight: 400 },
  },
  {
    label: "Subtitle",
    content: "Subtitle goes here",
    sample: "text-[13px]",
    preset: { fontFamily: DEFAULT_TEXT_FONT, fontSize: 72, fontWeight: 600 },
  },
  {
    label: "Caption pill",
    content: "Caption",
    sample: "text-[12px]",
    preset: { fontFamily: DEFAULT_TEXT_FONT, fontSize: 56, fontWeight: 700, background: "#0a84ff" },
  },
  {
    label: "Body",
    content: "Body text — a short line of supporting copy.",
    sample: "text-2xs font-normal",
    preset: { fontFamily: DEFAULT_TEXT_FONT, fontSize: 44, fontWeight: 400, shadow: null },
  },
];

export function TextPanel() {
  const addTextClip = useVideoEditorStore((s) => s.addTextClip);
  const brandFonts = useBrandFonts();
  const [uploadingFont, setUploadingFont] = useState(false);
  const fontFileRef = useRef<HTMLInputElement>(null);

  async function onUploadFont(file: File) {
    setUploadingFont(true);
    try {
      await api.producerAssets.upload({ file, asset_kind: "font", display_name: file.name });
      await refreshBrandFonts();
    } catch {
      /* ignore */
    } finally {
      setUploadingFont(false);
    }
  }

  return (
    <div className="h-full flex flex-col">
      <div className="shrink-0 p-2.5 border-b border-border flex items-center justify-between">
        <span className="eyebrow">Text</span>
        <button
          onClick={() => addTextClip()}
          className="btn btn-primary text-2xs px-2.5 py-0.5"
        >
          + Add text
        </button>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-2 space-y-1.5">
        <p className="text-[10.5px] text-muted px-1 pb-1">Ready-made styles</p>
        {PRESETS.map((p) => (
          <button
            key={p.label}
            onClick={() => addTextClip({ content: p.content, preset: p.preset })}
            title="Click to add it, or drag it into the chat"
            {...attachmentDragProps({
              kind: "text",
              name: `${p.label} text`,
              preset: { ...p.preset, content: p.content } as Record<string, unknown>,
            })}
            className="w-full flex items-center gap-2.5 p-2 rounded-lg border border-border bg-bg hover:bg-bg-muted hover:border-accent/50 transition-colors text-left cursor-grab active:cursor-grabbing"
          >
            <span className="w-9 h-9 shrink-0 rounded-md bg-black/80 text-white flex items-center justify-center text-[15px] font-bold">
              T
            </span>
            <span className="min-w-0 flex-1">
              <span className="block text-[11.5px] text-fg/85 font-medium">{p.label}</span>
              <span className={["block truncate text-fg/60", p.sample].join(" ")}>{p.content}</span>
            </span>
          </button>
        ))}
      </div>

      {/* Brand fonts — upload a .ttf/.otf to the Assets Library; it then appears
          in the Text inspector's font picker. */}
      <div className="shrink-0 p-2.5 border-t border-border space-y-1.5">
        <div className="flex items-center justify-between">
          <span className="eyebrow">Brand fonts ({brandFonts.length})</span>
          <button
            onClick={() => fontFileRef.current?.click()}
            disabled={uploadingFont}
            className="btn btn-ghost text-2xs px-2 py-0.5"
          >
            {uploadingFont ? "Adding…" : "↑ Add a font"}
          </button>
        </div>
        <p className="text-[9.5px] text-muted">Saved with your assets. Pick it for any text on the right.</p>
        <input
          ref={fontFileRef}
          type="file"
          accept=".ttf,.otf,.woff,.woff2,font/*"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) onUploadFont(f);
            e.target.value = "";
          }}
        />
      </div>
    </div>
  );
}
