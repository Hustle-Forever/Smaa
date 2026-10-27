/**
 * Video Editor · Audio / Music tab (sidebar)
 *
 * Music lives in the shared Assets Library (producer_assets, asset_kind=audio).
 * Uploading runs Gemini analysis (mood/genre/energy) + a 768d embedding so the
 * future auto-editor can pick the right track — so upload shows "Analyzing…".
 * Clicking a track drops it onto an audio track via an asset-source clip.
 */
import { useEffect, useRef, useState } from "react";

import { api } from "../../../../lib/api";
import { useVideoEditorStore } from "../../../../lib/store";
import type { ProducerAssetRow } from "../../../../lib/types";
import { attachmentDragProps } from "./attachmentDnd";

function durFrames(a: ProducerAssetRow): number {
  return Math.max(1, Math.round((a.duration_sec || 3) * 30));
}

export function AudioPanel() {
  const addAudioClip = useVideoEditorStore((s) => s.addAudioClip);
  const [items, setItems] = useState<ProducerAssetRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [analyzing, setAnalyzing] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  async function refresh() {
    try {
      const r = await api.producerAssets.list({ kind: "audio", limit: 200 });
      setItems(r.items.filter((a) => a.asset_kind === "audio"));
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  }

  // Music the AI generates is saved to the library as its job finishes, so look
  // again then, not only when the tab opens.
  const aiFinishedAt = useVideoEditorStore((s) => s.aiJob?.finished_at ?? null);
  useEffect(() => {
    void refresh();
  }, [aiFinishedAt]);

  async function onUpload(file: File) {
    setAnalyzing(true);
    setErr(null);
    try {
      await api.producerAssets.upload({ file, asset_kind: "audio", display_name: file.name });
      await refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "upload failed");
    } finally {
      setAnalyzing(false);
    }
  }

  return (
    <div className="h-full flex flex-col">
      <div className="shrink-0 p-2.5 border-b border-border flex items-center justify-between">
        <span className="eyebrow">Music</span>
        <button
          onClick={() => fileRef.current?.click()}
          disabled={analyzing}
          className="btn btn-primary text-2xs px-2.5 py-0.5"
        >
          {analyzing ? "Listening…" : "+ Add music"}
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="audio/*"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) onUpload(f);
            e.target.value = "";
          }}
        />
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-2 space-y-1.5">
        {err && <p className="text-[10.5px] text-danger px-1">{err}</p>}
        {analyzing && (
          <p className="text-[10.5px] text-muted px-1">Having a listen, to work out the mood and the energy…</p>
        )}
        {loading ? (
          <p className="text-2xs text-muted text-center py-8">Loading…</p>
        ) : items.length === 0 ? (
          <div className="text-center py-8 px-3 space-y-1.5">
            <p className="text-[11.5px] text-muted">No music saved yet.</p>
            <button onClick={() => fileRef.current?.click()} className="text-2xs text-accent hover:underline">
              + Add some music
            </button>
          </div>
        ) : (
          items.map((a) => (
            <button
              key={a.id}
              onClick={() => addAudioClip({ id: a.id, durationInFrames: durFrames(a), title: a.display_name || a.slug })}
              title="Click to add it, or drag it into the chat"
              {...attachmentDragProps({
                kind: "audio",
                name: a.display_name || a.slug,
                assetId: a.id,
              })}
              className="w-full flex items-center gap-2.5 p-2 rounded-lg border border-border bg-bg hover:bg-bg-muted hover:border-accent/50 transition-colors text-left cursor-grab active:cursor-grabbing"
            >
              <span className="w-9 h-9 shrink-0 rounded-md bg-emerald-600/30 text-emerald-200 flex items-center justify-center text-[15px]">
                ♪
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-[11.5px] text-fg/85 truncate">{a.display_name || a.slug}</span>
                <span className="block text-[9.5px] font-mono text-muted">
                  {Math.round((a.duration_sec || 0))}s
                  {(a.attributes as any)?.mood ? ` · ${(a.attributes as any).mood}` : ""} · click to add
                </span>
              </span>
            </button>
          ))
        )}
      </div>
    </div>
  );
}
