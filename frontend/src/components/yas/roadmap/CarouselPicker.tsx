import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

import { api } from "../../../lib/api";
import { Skeleton } from "../../Skeleton";
import type { CarouselSlide, RoadmapTool } from "./types";

const TOOL_LABEL: Record<RoadmapTool, string> = {
  poster: "Poster",
  animation: "Animation",
  poster_human: "Poster + Human",
  talking_head_v2: "Human Element Video",
  video_editor: "Video Editor",
  // Human characters are saved as Assets, not carousel-eligible posts; this entry
  // only satisfies the Record<RoadmapTool,…> completeness check.
  human_character: "AI-generated character",
  general_video: "General AI Video",
  broll: "B-roll",
};

function isReady(it: any): boolean {
  const s = String(it?.status ?? "");
  return s === "ready" || s === "saved_to_assets";
}

/** Pull finished posts from every roadmap tool's recent history and map them to
 *  carousel slides. Heterogeneous item shapes → loose `any` mapping. */
async function loadCandidates(): Promise<CarouselSlide[]> {
  const yp = api.yasProducer;
  const out: CarouselSlide[] = [];
  const [poster, animation, posterHuman, th] = await Promise.allSettled([
    yp.posterOnly.recent(),
    yp.animation.recent(),
    yp.posterHuman.recent(),
    yp.talkingHeadV2.recent(),
  ]);
  if (poster.status === "fulfilled")
    for (const it of poster.value.items as any[])
      if (isReady(it))
        out.push({ tool: "poster", refId: it.id, mediaType: "image", mediaUrl: yp.posterOnly.fileUrl(it.id, it.created_at), topic: it.topic ?? "" });
  if (animation.status === "fulfilled")
    for (const it of animation.value.items as any[])
      if (isReady(it))
        out.push({ tool: "animation", refId: it.id, mediaType: "video", mediaUrl: yp.animation.fileUrl(it.id, it.created_at), topic: it.topic ?? "" });
  if (posterHuman.status === "fulfilled")
    for (const it of posterHuman.value.items as any[])
      if (isReady(it))
        out.push({ tool: "poster_human", refId: it.id, mediaType: "image", mediaUrl: yp.posterHuman.fileUrl(it.id), topic: it.topic ?? "" });
  if (th.status === "fulfilled")
    for (const it of th.value.items as any[])
      if (String(it?.status ?? "") === "ready")
        out.push({ tool: "talking_head_v2", refId: it.id, mediaType: "video", mediaUrl: yp.talkingHeadV2.compositionFileUrl(it.id, it.composition_good_seq), topic: it.topic ?? "" });
  return out;
}

export function CarouselPicker({
  existing,
  onAdd,
  onClose,
}: {
  existing: CarouselSlide[];
  onAdd: (slide: CarouselSlide) => void;
  onClose: () => void;
}) {
  const [cands, setCands] = useState<CarouselSlide[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadCandidates()
      .then((c) => !cancelled && setCands(c))
      .catch(() => !cancelled && setCands([]));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prev;
      window.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const taken = (s: CarouselSlide) => existing.some((e) => e.tool === s.tool && e.refId === s.refId);
  const available = (cands ?? []).filter((c) => !taken(c));

  return createPortal(
    <div
      style={{
        position: "fixed", inset: 0, zIndex: 100, background: "rgba(0,0,0,0.7)",
        backdropFilter: "blur(2px)", display: "flex", alignItems: "center",
        justifyContent: "center", padding: 16,
      }}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div onClick={(e) => e.stopPropagation()} className="card w-full max-w-3xl max-h-[85vh] flex flex-col overflow-hidden">
        <div className="flex items-center justify-between p-5 border-b border-border">
          <div>
            <p className="eyebrow mb-1">Add a slide</p>
            <h2 className="text-lg font-semibold text-fg">Pick a finished post</h2>
          </div>
          <button onClick={onClose} className="btn btn-ghost px-2" aria-label="Close">
            <X className="w-4 h-4" strokeWidth={1.8} />
          </button>
        </div>
        <div className="p-5 overflow-y-auto">
          {cands == null ? (
            <div className="grid grid-cols-3 sm:grid-cols-4 gap-3">
              {Array.from({ length: 8 }).map((_, i) => (
                <Skeleton key={i} h="h-28" rounded="rounded-lg" />
              ))}
            </div>
          ) : available.length === 0 ? (
            <p className="text-sm text-muted">
              No other finished posts to add yet. Create more posts, then come back.
            </p>
          ) : (
            <div className="grid grid-cols-3 sm:grid-cols-4 gap-3">
              {available.map((c) => (
                <button
                  key={`${c.tool}:${c.refId}`}
                  onClick={() => {
                    onAdd(c);
                    onClose();
                  }}
                  className="card card-hover p-0 overflow-hidden text-left group"
                  title={c.topic || `#${c.refId}`}
                >
                  <div className="aspect-square bg-bg-muted relative overflow-hidden">
                    {c.mediaType === "video" ? (
                      <video src={c.mediaUrl} muted playsInline preload="metadata" className="w-full h-full object-cover" />
                    ) : (
                      <img src={c.mediaUrl} alt="" loading="lazy" className="w-full h-full object-cover" />
                    )}
                    <span className="absolute top-1 left-1 font-mono text-[9px] px-1 py-0.5 rounded bg-black/60 text-white">
                      {TOOL_LABEL[c.tool]}
                    </span>
                  </div>
                  <p className="p-1.5 text-[10.5px] text-fg/80 line-clamp-1">{c.topic || `#${c.refId}`}</p>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
