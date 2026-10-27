/**
 * Video Editor · Stickers / Images tab (sidebar)
 *
 * Stickers live in the shared Assets Library (producer_assets, asset_kind=image
 * + category='sticker'). Uploading runs Gemini analysis + a 768d embedding.
 * Clicking one drops it as an image overlay clip at the playhead.
 */
import { useEffect, useRef, useState } from "react";

import { api } from "../../../../lib/api";
import { useVideoEditorStore } from "../../../../lib/store";
import type { ProducerAssetRow } from "../../../../lib/types";
import { attachmentDragProps } from "./attachmentDnd";

export function StickerPanel() {
  const addImageClip = useVideoEditorStore((s) => s.addImageClip);
  const [items, setItems] = useState<ProducerAssetRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  async function refresh() {
    try {
      const r = await api.producerAssets.list({ kind: "image", category: "sticker", limit: 200 });
      setItems(r.items);
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  // Multi-file upload: each file runs the Gemini analysis + 768d embedding via
  // the existing single-file endpoint, looped serially with progress.
  async function onUpload(files: File[]) {
    if (!files.length) return;
    setErr(null);
    setProgress({ done: 0, total: files.length });
    const failed: string[] = [];
    for (let i = 0; i < files.length; i++) {
      try {
        await api.producerAssets.upload({
          file: files[i],
          asset_kind: "image",
          category: "sticker",
          display_name: files[i].name,
        });
      } catch {
        failed.push(files[i].name);
      }
      setProgress({ done: i + 1, total: files.length });
    }
    setProgress(null);
    if (failed.length) setErr(`Failed: ${failed.join(", ")}`);
    await refresh();
  }
  const analyzing = progress != null;

  return (
    <div className="h-full flex flex-col">
      <div className="shrink-0 p-2.5 border-b border-border flex items-center justify-between">
        <span className="eyebrow">Stickers</span>
        <button
          onClick={() => fileRef.current?.click()}
          disabled={analyzing}
          className="btn btn-primary text-2xs px-2.5 py-0.5"
        >
          {progress ? `Analyzing ${progress.done}/${progress.total}…` : "+ Upload"}
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          multiple
          className="hidden"
          onChange={(e) => {
            const fs = Array.from(e.target.files || []);
            if (fs.length) void onUpload(fs);
            e.target.value = "";
          }}
        />
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-2">
        {err && <p className="text-[10.5px] text-danger px-1 pb-1">{err}</p>}
        {loading ? (
          <p className="text-2xs text-muted text-center py-8">Loading…</p>
        ) : items.length === 0 ? (
          <div className="text-center py-8 px-3 space-y-1.5">
            <p className="text-[11.5px] text-muted">No stickers saved yet.</p>
            <button onClick={() => fileRef.current?.click()} className="text-2xs text-accent hover:underline">
              + Upload a sticker / logo
            </button>
          </div>
        ) : (
          <div className="grid grid-cols-3 gap-1.5">
            {items.map((a) => (
              <button
                key={a.id}
                onClick={() => addImageClip({ id: a.id })}
                title={`${a.display_name || a.slug} · click to add it, or drag it into the chat`}
                {...attachmentDragProps({
                  kind: "sticker",
                  name: a.display_name || a.slug,
                  assetId: a.id,
                  thumbUrl: api.producerAssets.previewUrl(a.id),
                })}
                className="aspect-square rounded-md border border-border bg-black/40 overflow-hidden hover:border-accent/60 transition-colors cursor-grab active:cursor-grabbing"
              >
                <img
                  src={api.producerAssets.previewUrl(a.id)}
                  alt=""
                  loading="lazy"
                  className="w-full h-full object-contain"
                />
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
