/**
 * Video Editor · Media bin (sidebar tab)
 *
 * A per-project pool of videos the user added. "+ Add" opens the multi-select
 * Library picker; bin tiles hover-to-play; click a tile to drop it on the
 * timeline. Persisted in project.media_bin.
 */
import { useRef, useState } from "react";

import { api } from "../../../../lib/api";
import { useVideoEditorStore } from "../../../../lib/store";
import type { MediaBinEntry, VideoSource } from "../../../../lib/types";
import { VideoSourcePickerModal } from "../VideoSourcePickerModal";
import { attachmentDragProps } from "./attachmentDnd";

const EMPTY_BIN: MediaBinEntry[] = [];

export function entryToSource(b: MediaBinEntry): VideoSource {
  // Stills are images served by their own file route (used for both the tile
  // thumbnail and the Player); video kinds go through the video/thumb proxies.
  const isStill = b.kind === "still";
  const stillUrl = `/api/yas-producer/video-editor/sources/still/${b.id}/file`;
  return {
    kind: b.kind,
    id: b.id,
    title: b.title || `${b.kind} #${b.id}`,
    durationInFrames: b.durationInFrames || 1,
    naturalDurationInFrames: b.durationInFrames || 1,
    with_audio: !isStill,
    width: null,
    height: null,
    url: isStill ? stillUrl : api.yasProducer.videoEditor.sourceVideoUrl(b.kind, b.id),
    thumbnail_url: isStill ? stillUrl : api.yasProducer.videoEditor.sourceThumbUrl(b.kind, b.id),
    created_at: null,
  };
}

export function MediaBinPanel() {
  const mediaBin = useVideoEditorStore((s) => s.project?.media_bin) ?? EMPTY_BIN;
  const addClipFromSource = useVideoEditorStore((s) => s.addClipFromSource);
  const addImageClip = useVideoEditorStore((s) => s.addImageClip);
  const removeFromBin = useVideoEditorStore((s) => s.removeFromBin);
  const [pickerOpen, setPickerOpen] = useState(false);

  return (
    <div className="h-full flex flex-col">
      <div className="shrink-0 p-2.5 border-b border-border flex items-center justify-between">
        <span className="eyebrow">Media</span>
        <button onClick={() => setPickerOpen(true)} className="btn btn-primary text-2xs px-2.5 py-0.5">
          + Add
        </button>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-2 space-y-1.5">
        {mediaBin.length === 0 ? (
          <div className="text-center py-8 px-3 space-y-1.5">
            <p className="text-[11.5px] text-muted">Nothing here yet.</p>
            <button onClick={() => setPickerOpen(true)} className="text-2xs text-accent hover:underline">
              + Add clips from the Library
            </button>
          </div>
        ) : (
          mediaBin.map((b) => (
            <BinTile
              key={`${b.kind}-${b.id}`}
              source={entryToSource(b)}
              onAdd={() =>
                b.kind === "still"
                  ? addImageClip({ id: b.id, source: "still" }) // image overlay clip
                  : addClipFromSource(entryToSource(b))
              }
              onRemove={() => removeFromBin({ kind: b.kind, id: b.id })}
            />
          ))
        )}
      </div>
      {pickerOpen && <VideoSourcePickerModal onClose={() => setPickerOpen(false)} />}
    </div>
  );
}

function BinTile({ source, onAdd, onRemove }: { source: VideoSource; onAdd: () => void; onRemove: () => void }) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [hover, setHover] = useState(false);
  const [err, setErr] = useState(false);
  const isImage = source.kind === "still"; // images: no hover-to-play / duration
  return (
    <div
      {...attachmentDragProps({
        kind: "media",
        name: source.title,
        sourceKind: source.kind,
        sourceId: source.id,
      })}
      className="relative flex items-center gap-2 p-1.5 rounded-lg border border-border bg-bg hover:bg-bg-muted transition-colors group cursor-grab active:cursor-grabbing"
      onMouseEnter={() => {
        setHover(true);
        if (!isImage) videoRef.current?.play().catch(() => {});
      }}
      onMouseLeave={() => {
        setHover(false);
        const v = videoRef.current;
        if (v) {
          v.pause();
          v.currentTime = 0;
        }
      }}
    >
      <button onClick={onAdd} title="Add it to the timeline" className="relative w-16 h-10 shrink-0 rounded-md overflow-hidden bg-black/80">
        {hover && !err && !isImage ? (
          <video ref={videoRef} src={source.url} muted loop playsInline preload="none" poster={source.thumbnail_url} className="w-full h-full object-cover" />
        ) : !err ? (
          <img src={source.thumbnail_url} alt="" onError={() => setErr(true)} className="w-full h-full object-cover" />
        ) : (
          <span className="w-full h-full flex items-center justify-center text-[9px] text-white/40">▶</span>
        )}
        <span className="absolute bottom-0.5 right-0.5 text-[8px] font-mono px-1 rounded bg-black/65 text-white/90">
          {isImage ? "IMAGE" : `VIDEO · ${Math.round(source.durationInFrames / 30)}s`}
        </span>
      </button>
      <button onClick={onAdd} className="min-w-0 flex-1 text-left">
        <p className="text-2xs text-fg/85 truncate">{source.title}</p>
        <p className="text-[9px] font-mono text-muted">click to add</p>
      </button>
      <button onClick={onRemove} title="Take it out of this list" className="text-muted hover:text-danger text-[13px] px-1 opacity-0 group-hover:opacity-100">
        ×
      </button>
    </div>
  );
}
