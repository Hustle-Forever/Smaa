/**
 * Yas Producer · Library
 *
 * History grid of past generations — talking-head runs + poster+human
 * images/videos — backed by GET /api/yas-producer/library (cached in
 * useYasLibraryStore). Client-side filter chips (type + videos-only) narrow
 * the cached list; clicking a tile opens a self-contained detail modal that
 * plays the video / shows the image.
 *
 * Purely additive: does not touch the existing creators. The detail modal is
 * portaled to document.body to escape the page-transition transform that traps
 * `fixed` descendants (see CLAUDE.md modal-pattern note).
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { useModalChrome } from "../../lib/useModalChrome";
import { createPortal } from "react-dom";
import { Link2 } from "lucide-react";

import { useYasLibraryStore } from "../../lib/store";
import { fmtRel } from "../../lib/format";
import { api } from "../../lib/api";
import { libraryItemHash } from "../../lib/producerHash";
import { useCopyLink } from "../../lib/useCopyLink";
import { useConfirm } from "../ConfirmProvider";
import { useToast } from "../ToastProvider";
import type {
  YasLibraryItem,
  YasLibraryType,
  YasThCompositionStatus,
} from "../../lib/types";
import { TalkingHeadLibraryDetail } from "./TalkingHeadLibraryDetail";
import { TalkingHeadV2LibraryDetail } from "./TalkingHeadV2LibraryDetail";
import { VideoEditorLibraryDetail } from "./VideoEditorLibraryDetail";
import { ScheduledQueue } from "./roadmap/ScheduledQueue";
import { GLOBAL_STEP_LABELS, type GlobalStep } from "./roadmap/types";

/** Roadmap step → display label for the Library "Continue" pill (safe lookup). */
function roadmapStepLabel(step?: string | null): string | null {
  if (!step) return null;
  return GLOBAL_STEP_LABELS[step as GlobalStep] ?? step;
}

type TypeFilter = "all" | YasLibraryType;

const TYPE_META: Record<
  YasLibraryType,
  { label: string; dot: string; pill: string }
> = {
  talking_head: {
    label: "Human Element Video",
    dot: "bg-violet-500/80",
    pill: "bg-violet-500/15 text-violet-600",
  },
  talking_head_v2: {
    label: "Human Element Video",
    dot: "bg-fuchsia-500/80",
    pill: "bg-fuchsia-500/15 text-fuchsia-600",
  },
  poster: {
    label: "Poster",
    dot: "bg-blue-500/80",
    pill: "bg-blue-500/15 text-blue-600",
  },
  poster_human: {
    label: "Poster + Human",
    dot: "bg-success/80",
    pill: "bg-success/15 text-emerald-600",
  },
  animation: {
    label: "Animation",
    dot: "bg-orange-500/80",
    pill: "bg-orange-500/15 text-orange-600",
  },
  video_editor: {
    label: "Video Editor",
    dot: "bg-cyan-500/80",
    pill: "bg-cyan-500/15 text-cyan-600",
  },
  general_video: {
    label: "General AI Video",
    dot: "bg-teal-500/80",
    pill: "bg-teal-500/15 text-teal-600",
  },
  broll: {
    label: "B-roll",
    dot: "bg-warning/80",
    pill: "bg-warning/15 text-amber-600",
  },
};

type Lifecycle = "pending" | "finished" | "failed";

/** Pending vs finished for the Library status filter. Video categories
 *  (Human Element Video, Video Editor) are "finished" once their final video is
 *  playable (has_video); posters are finished once the image is ready. */
function lifecycleOf(it: YasLibraryItem): Lifecycle {
  if (it.status === "failed" || it.composition_status === "failed") return "failed";
  const isVideoType =
    it.type === "talking_head" ||
    it.type === "talking_head_v2" ||
    it.type === "animation" ||
    it.type === "video_editor";
  if (isVideoType) return it.has_video ? "finished" : "pending";
  return it.status === "ready" || it.status === "saved_to_assets"
    ? "finished"
    : "pending";
}

export function YasLibrary({
  onContinue,
  initialSubtab,
  initialOpen,
}: {
  /** Resume a generation in its creator (Library "Continue editing"). The type
   *  is threaded so the resume URL is unambiguous across the poster / animation /
   *  poster_human id-spaces. */
  onContinue?: (runId: number, type: YasLibraryType) => void;
  /** Deep-link entry — e.g. the reminder email opens the Scheduled tab. */
  initialSubtab?: "history" | "scheduled";
  /** Deep-link entry — open this item's detail modal once the list loads (e.g.
   *  the Dashboard "Recent work" tiles). Fires once. */
  initialOpen?: { id: number; type: YasLibraryType } | null;
} = {}) {
  const store = useYasLibraryStore();
  const [subtab, setSubtab] = useState<"history" | "scheduled">(initialSubtab ?? "history");
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");
  const [statusFilter, setStatusFilter] = useState<"all" | "pending" | "finished">("all");
  const [openItem, setOpenItem] = useState<YasLibraryItem | null>(null);
  const didAutoOpen = useRef(false);
  const copyLink = useCopyLink();

  // Copy a shareable deep link to this item's detail view (#library/<type>:<id>).
  // The URL already resolves via producerHash's libraryOpenTarget → initialOpen.
  function copyItemLink(it: YasLibraryItem) {
    const url = new URL(libraryItemHash(it.type, it.id), window.location.href).href;
    copyLink(url, "Opens this item's detail view.");
  }

  // Hard-delete a Library item server-side then drop its tile from the cached
  // grid. Dispatches to the right endpoint per type — every visible type is
  // deletable (talking_head v1 is filtered out of the grid). Throws on failure
  // so the caller's modal can surface a toast and stay open.
  async function deleteLibraryItem(type: YasLibraryType, id: number) {
    switch (type) {
      case "poster": await api.yasProducer.posterOnly.delete(id); break;
      case "poster_human": await api.yasProducer.posterHuman.delete(id); break;
      case "animation": await api.yasProducer.animation.delete(id); break;
      case "talking_head_v2": await api.yasProducer.talkingHeadV2.delete(id); break;
      case "video_editor": await api.yasProducer.videoEditor.delete(id); break;
      default: return; // not deletable from here
    }
    store.remove(type, id);
  }

  useEffect(() => {
    store.fetch({ limit: 300 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Deep-link: once the list is loaded, open the requested item's detail modal
  // exactly once. If it isn't in the (capped) list, fall back to a minimal stub
  // so the type-routed detail modal still hydrates it by id.
  useEffect(() => {
    if (!initialOpen || didAutoOpen.current || store.loading) return;
    didAutoOpen.current = true;
    const found = store.items.find((it) => it.id === initialOpen.id && it.type === initialOpen.type);
    setOpenItem(found ?? ({ id: initialOpen.id, type: initialOpen.type, topic: null } as unknown as YasLibraryItem));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialOpen, store.loading, store.items]);

  // Deep-link (e.g. reminder email → Scheduled tab) can switch the tab after mount.
  useEffect(() => {
    if (initialSubtab) setSubtab(initialSubtab);
  }, [initialSubtab]);

  const visibleItems = useMemo(
    () => store.items.filter((it) => it.type !== "talking_head"),
    [store.items],
  );

  const counts = useMemo(() => {
    const c = {
      all: 0,
      talking_head: 0,
      talking_head_v2: 0,
      poster: 0,
      poster_human: 0,
      animation: 0,
      video_editor: 0,
      general_video: 0,
      broll: 0,
    };
    for (const it of visibleItems) {
      c.all += 1;
      c[it.type] += 1;
    }
    return c;
  }, [visibleItems]);

  const typeFiltered = useMemo(
    () => (typeFilter === "all" ? visibleItems : visibleItems.filter((it) => it.type === typeFilter)),
    [visibleItems, typeFilter],
  );

  const statusCounts = useMemo(() => {
    const c = { all: 0, pending: 0, finished: 0 };
    for (const it of typeFiltered) {
      c.all += 1;
      const lc = lifecycleOf(it);
      if (lc === "pending") c.pending += 1;
      else if (lc === "finished") c.finished += 1;
    }
    return c;
  }, [typeFiltered]);

  const items = useMemo(
    () => (statusFilter === "all" ? typeFiltered : typeFiltered.filter((it) => lifecycleOf(it) === statusFilter)),
    [typeFiltered, statusFilter],
  );

  // Windowed rendering: only mount the first `visibleCount` tiles and reveal
  // more as the user scrolls. Off-screen tiles aren't in the DOM at all, so
  // their media never hits the network — first paint stays cheap even with a
  // few hundred items. Reset to one page whenever the filtered set changes so
  // a new filter shows from the top.
  const PAGE = 48;
  const [visibleCount, setVisibleCount] = useState(PAGE);
  useEffect(() => {
    setVisibleCount(PAGE);
  }, [typeFilter, statusFilter, subtab]);
  const shown = useMemo(() => items.slice(0, visibleCount), [items, visibleCount]);
  const hasMore = visibleCount < items.length;

  const sentinelRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el || !hasMore) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setVisibleCount((c) => c + PAGE);
        }
      },
      { rootMargin: "600px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [hasMore, items.length]);

  return (
    <div className="space-y-5">
      {/* Library sub-tabs */}
      <div className="flex items-center gap-1.5">
        {(["history", "scheduled"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setSubtab(t)}
            className={[
              "px-3 py-1 rounded-full border text-[13px] font-medium transition-colors",
              subtab === t
                ? "border-accent bg-accent/8 text-fg"
                : "border-border bg-bg-subtle text-muted hover:bg-bg-muted",
            ].join(" ")}
          >
            {t === "history" ? "History" : "Scheduled"}
          </button>
        ))}
      </div>

      {subtab === "scheduled" ? (
        <ScheduledQueue />
      ) : (
      <>
      {/* Filter bar */}
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <Chip
            active={typeFilter === "all"}
            onClick={() => setTypeFilter("all")}
            label="All"
            count={counts.all}
          />
          <Chip
            active={typeFilter === "talking_head_v2"}
            onClick={() => setTypeFilter("talking_head_v2")}
            label="Human Element Video"
            count={counts.talking_head_v2}
            dot={TYPE_META.talking_head_v2.dot}
          />
          <Chip
            active={typeFilter === "poster"}
            onClick={() => setTypeFilter("poster")}
            label="Poster"
            count={counts.poster}
            dot={TYPE_META.poster.dot}
          />
          <Chip
            active={typeFilter === "poster_human"}
            onClick={() => setTypeFilter("poster_human")}
            label="Poster + Human"
            count={counts.poster_human}
            dot={TYPE_META.poster_human.dot}
          />
          <Chip
            active={typeFilter === "animation"}
            onClick={() => setTypeFilter("animation")}
            label="Animation"
            count={counts.animation}
            dot={TYPE_META.animation.dot}
          />
          <Chip
            active={typeFilter === "video_editor"}
            onClick={() => setTypeFilter("video_editor")}
            label="Video Editor"
            count={counts.video_editor}
            dot={TYPE_META.video_editor.dot}
          />
          <Chip
            active={typeFilter === "general_video"}
            onClick={() => setTypeFilter("general_video")}
            label="General AI Video"
            count={counts.general_video}
            dot={TYPE_META.general_video.dot}
          />
          <Chip
            active={typeFilter === "broll"}
            onClick={() => setTypeFilter("broll")}
            label="B-roll"
            count={counts.broll}
            dot={TYPE_META.broll.dot}
          />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Chip
            active={statusFilter === "all"}
            onClick={() => setStatusFilter("all")}
            label="All status"
            count={statusCounts.all}
          />
          <Chip
            active={statusFilter === "pending"}
            onClick={() => setStatusFilter("pending")}
            label="Pending"
            count={statusCounts.pending}
            dot="bg-warning/80"
          />
          <Chip
            active={statusFilter === "finished"}
            onClick={() => setStatusFilter("finished")}
            label="Finished"
            count={statusCounts.finished}
            dot="bg-success/80"
          />
        </div>
      </div>

      {/* States */}
      {store.loading && store.items.length === 0 && (
        <p className="text-sm text-muted text-center py-12">Loading history…</p>
      )}
      {store.error && (
        <p className="text-sm text-danger text-center py-12">
          Couldn’t load history: {store.error}
        </p>
      )}
      {!store.loading && !store.error && store.items.length === 0 && (
        <div className="text-center py-16 space-y-1.5">
          <p className="text-sm text-fg">No past work yet.</p>
          <p className="text-xs text-muted">
            Generated posters and talking-head videos will show up here.
          </p>
        </div>
      )}
      {!store.loading && store.items.length > 0 && items.length === 0 && (
        <p className="text-sm text-muted text-center py-12">
          Nothing matches this filter.
        </p>
      )}

      {/* Grid */}
      {items.length > 0 && (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
          {shown.map((it) => {
            const stepLabel = roadmapStepLabel(it.roadmap_step);
            return (
              <div key={`${it.type}-${it.id}`} className="relative group">
                <LibraryTile item={it} onOpen={() => setOpenItem(it)} />
                {/* Copy a shareable link to this item (appears on hover). */}
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    copyItemLink(it);
                  }}
                  title="Copy a shareable link to this item"
                  aria-label="Copy link to this item"
                  className="absolute top-2 left-2 z-10 p-1 rounded-full bg-black/45 text-white/90 hover:bg-black/70 opacity-0 group-hover:opacity-100 focus:opacity-100 transition"
                >
                  <Link2 className="w-3 h-3" />
                </button>
                {/* Mid-roadmap → a direct "Continue → <Step>" resume pill (the
                    roadmap self-hydrates to this exact step). */}
                {stepLabel && onContinue && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onContinue(it.id, it.type);
                    }}
                    title={`Resume this project at ${stepLabel}`}
                    className="absolute top-2 right-2 z-10 text-3xs font-semibold px-2 py-1 rounded-full bg-accent text-white shadow-md hover:brightness-110 transition"
                  >
                    Continue → {stepLabel}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Infinite-scroll sentinel — reveals the next page as it nears the
          viewport; the button is a no-JS / observer-miss fallback. */}
      {hasMore && (
        <div ref={sentinelRef} className="flex justify-center py-6">
          <button
            onClick={() => setVisibleCount((c) => c + PAGE)}
            className="text-xs font-medium text-muted hover:text-fg px-4 py-2 rounded-lg border border-border hover:border-border-strong transition"
          >
            Load more ({items.length - visibleCount} left)
          </button>
        </div>
      )}

      {openItem &&
        (openItem.type === "talking_head" ? (
          <TalkingHeadLibraryDetail
            runId={openItem.id}
            topic={openItem.topic}
            onClose={() => setOpenItem(null)}
          />
        ) : openItem.type === "talking_head_v2" ? (
          <TalkingHeadV2LibraryDetail
            runId={openItem.id}
            topic={openItem.topic}
            onClose={() => setOpenItem(null)}
            onContinue={
              onContinue
                ? () => {
                    const { id, type } = openItem;
                    setOpenItem(null);
                    onContinue(id, type);
                  }
                : undefined
            }
            onDelete={async () => {
              const { id, type } = openItem;
              await deleteLibraryItem(type, id);
              setOpenItem(null);
            }}
          />
        ) : openItem.type === "video_editor" ? (
          <VideoEditorLibraryDetail
            projectId={openItem.id}
            title={openItem.topic}
            onClose={() => setOpenItem(null)}
            onContinue={
              onContinue
                ? () => {
                    const { id, type } = openItem;
                    setOpenItem(null);
                    onContinue(id, type);
                  }
                : undefined
            }
            onDelete={async () => {
              const { id, type } = openItem;
              await deleteLibraryItem(type, id);
              setOpenItem(null);
            }}
          />
        ) : (
          // poster / poster_human / animation → generic media modal + resume.
          <LibraryDetailModal
            item={openItem}
            onClose={() => setOpenItem(null)}
            onContinue={
              onContinue
                ? () => {
                    const { id, type } = openItem;
                    setOpenItem(null);
                    onContinue(id, type);
                  }
                : undefined
            }
            onDelete={
              // Delete for every type this modal serves (poster / poster_human /
              // animation). Removes the generation server-side, then drops it from
              // the cached grid so the tile disappears immediately.
              async () => {
                const { id, type } = openItem;
                await deleteLibraryItem(type, id);
                setOpenItem(null);
              }
            }
            onRemoveLogo={
              // Remove-logo for poster / poster+human STILLS only (never videos).
              // Re-renders server-side as a NEW child record, then refreshes the
              // grid so the de-logoed version shows alongside the original.
              (openItem.type === "poster" || openItem.type === "poster_human") &&
              !openItem.has_video
                ? async () => {
                    const { id, type } = openItem;
                    if (type === "poster")
                      await api.yasProducer.posterOnly.removeLogo(id);
                    else await api.yasProducer.posterHuman.removeLogo(id);
                    store.invalidate();
                    store.fetch({ limit: 300 });
                    setOpenItem(null);
                  }
                : undefined
            }
            onExport={
              onContinue &&
              openItem.type === "animation" &&
              openItem.has_video &&
              openItem.status === "ready"
                ? async () => {
                    const animId = openItem.id;
                    const { project_id } =
                      await api.yasProducer.animation.openInEditor(animId);
                    setOpenItem(null);
                    onContinue(project_id, "video_editor");
                  }
                : undefined
            }
          />
        ))}
      </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------

function Chip({
  active,
  onClick,
  label,
  count,
  dot,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count: number;
  dot?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-[12px] font-medium transition-colors",
        active
          ? "border-accent bg-accent/8 text-fg"
          : "border-border bg-bg-subtle text-muted hover:bg-bg-muted",
      ].join(" ")}
    >
      {dot && <span className={`w-2 h-2 rounded-full ${dot}`} />}
      <span>{label}</span>
      <span className="text-3xs font-mono text-muted tabular-nums">{count}</span>
    </button>
  );
}

/** Tile pending label for an unstitched Human Element Video, keyed by composition status.
 *  v2 reels go through the Rebrand step before Final; v1 (frozen) still stitches. */
function tilePendingLabel(
  status: YasThCompositionStatus | string | null | undefined,
  type?: string,
): string {
  switch (status) {
    case "queued":
      return "Rendering clips…";
    case "videos_ready":
      return type === "talking_head_v2" ? "Ready to rebrand" : "Ready to stitch";
    case "rendering":
      return "Stitching…";
    case "failed":
      return "Render failed";
    default:
      return "Draft · no video yet";
  }
}

function LibraryTile({
  item,
  onOpen,
}: {
  item: YasLibraryItem;
  onOpen: () => void;
}) {
  const [imgError, setImgError] = useState(false);
  const [videoError, setVideoError] = useState(false);
  // One retry through the backend proxy before we admit defeat. `thumbnail_url`
  // may be a Supabase CDN URL, and the whole Library once went blank because
  // those URLs 403'd in the BROWSER — a failure no server-side log could see
  // (#37). `download_url` is the same object served by us, so a CDN or
  // entitlement problem costs a slow tile instead of an empty one.
  const [triedProxy, setTriedProxy] = useState(false);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const meta = TYPE_META[item.type];

  // Show whatever is ready, best-first: the FINAL video, else a non-final
  // preview clip (e.g. a Human Element Video main scene before stitching), else the
  // still/keyframe image, else a typed placeholder. A video whose URL is dead
  // (e.g. a composition wiped from disk → 410) falls through to the still /
  // placeholder branches instead of rendering a black box.
  const finalVideo = item.has_video && item.video_url ? item.video_url : null;
  const previewVideoUrl = finalVideo || item.preview_url || null;
  const showVideo = !!previewVideoUrl && !videoError;
  // The proxy fallback only helps while it is a DIFFERENT url from the one that
  // just failed, so an item whose thumbnail already is the proxy skips straight
  // to the error state rather than reloading the same dead resource.
  const proxyThumb =
    item.download_url && item.download_url !== item.thumbnail_url ? item.download_url : null;
  const stillSrc = triedProxy ? proxyThumb : item.thumbnail_url;
  const showStill = !showVideo && !!stillSrc && !imgError;
  // Editor-backed types are timeline DOCUMENTS, not images: a project with no
  // visual clip legitimately has nothing to preview, so it gets a neutral
  // "Empty timeline" rather than a warning.
  const isEditorType =
    item.type === "video_editor" || item.type === "general_video" || item.type === "broll";
  const isTH = item.type === "talking_head" || item.type === "talking_head_v2";
  // Human Element Video with nothing renderable yet → pending placeholder (status label).
  const showPending = !showVideo && !showStill && isTH;

  function playPreview() {
    const v = videoRef.current;
    if (v) v.play().catch(() => {});
  }
  function stopPreview() {
    const v = videoRef.current;
    if (v) {
      v.pause();
      v.currentTime = 0;
    }
  }

  return (
    <button
      onClick={onOpen}
      onMouseEnter={showVideo ? playPreview : undefined}
      onMouseLeave={showVideo ? stopPreview : undefined}
      onFocus={showVideo ? playPreview : undefined}
      onBlur={showVideo ? stopPreview : undefined}
      className="card card-hover text-left p-0 overflow-hidden flex flex-col group w-full"
      title={item.topic ?? undefined}
    >
      <div className="aspect-[3/4] bg-bg-muted relative overflow-hidden">
        {showVideo ? (
          <video
            ref={videoRef}
            src={previewVideoUrl!}
            // For a comp-ready video-editor row the backend aliases
            // thumbnail_url = video_url, so passing it as `poster` asks the image
            // loader to decode a whole MP4 (measured 29 MB). Only use it as a
            // poster when it is genuinely a different, still, resource.
            poster={
              item.thumbnail_url && item.thumbnail_url !== previewVideoUrl
                ? item.thumbnail_url
                : undefined
            }
            muted
            loop
            playsInline
            // "none", not "metadata": hover-play (below) fetches on demand, so
            // metadata for every off-screen tile is pure waste.
            preload="none"
            className="w-full h-full object-cover"
            onError={() => setVideoError(true)}
          />
        ) : showStill ? (
          <img
            src={stillSrc!}
            alt=""
            className="w-full h-full object-cover"
            loading="lazy"
            decoding="async"
            onError={() => {
              if (!triedProxy && proxyThumb) setTriedProxy(true);
              else setImgError(true);
            }}
          />
        ) : showPending ? (
          <div className="w-full h-full flex flex-col items-center justify-center gap-1.5 px-3 text-center">
            <span className="text-lg opacity-60 animate-pulse">⏳</span>
            <span className="text-[10.5px] text-muted">
              {tilePendingLabel(item.composition_status, item.type)}
            </span>
          </div>
        ) : (
          // Three DIFFERENT states used to collapse into one grey "no preview"
          // box, which made data states look like outages:
          //   failed            — the generation itself failed
          //   nothing to show   — a real, complete record with no image to show
          //                       (an empty or audio-only editor timeline). Not
          //                       an error; the backend sends thumbnail_url:null
          //                       and preview_kind:"none" for these.
          //   broken            — media was expected but couldn't be fetched
          //                       (should be rare; onError sets imgError)
          <div className="w-full h-full flex flex-col items-center justify-center gap-1 px-3 text-center">
            {item.status === "failed" ? (
              <>
                <span className="text-base opacity-50">⚠</span>
                <span className="text-[10.5px] text-danger">failed</span>
              </>
            ) : imgError || videoError ? (
              <>
                <span className="text-base opacity-50">⚠</span>
                <span className="text-[10.5px] text-muted">Preview unavailable</span>
              </>
            ) : (
              <>
                <span className="text-base opacity-40">▤</span>
                <span className="text-[10.5px] text-muted">
                  {isEditorType ? "Empty timeline" : "Nothing to preview"}
                </span>
              </>
            )}
          </div>
        )}
        <span
          className={`absolute top-2 left-2 font-mono text-[9.5px] px-1.5 py-0.5 rounded ${meta.pill}`}
        >
          {meta.label}
        </span>
        {/* Badge what we're showing when it isn't the finished deliverable. */}
        {!finalVideo && item.preview_kind === "scene" && (
          <span className="absolute top-2 right-2 text-[9px] font-medium px-1.5 py-0.5 rounded bg-warning/85 text-white">
            scene
          </span>
        )}
        {!finalVideo && showStill && isTH && (
          <span className="absolute top-2 right-2 text-[9px] font-medium px-1.5 py-0.5 rounded bg-black/55 text-white/90">
            still
          </span>
        )}
        {showVideo && (
          <span className="absolute bottom-2 right-2 text-[9px] font-medium px-1.5 py-0.5 rounded bg-black/55 text-white/90 opacity-0 group-hover:opacity-100 transition-opacity">
            hover to play
          </span>
        )}
      </div>
      <div className="p-2.5 space-y-1 flex-1 flex flex-col">
        <p className="text-xs text-fg/85 line-clamp-2">
          {item.topic || "(untitled)"}
        </p>
        <p className="mt-auto text-[10.5px] font-mono text-muted">
          {fmtRel(item.created_at)}
        </p>
      </div>
    </button>
  );
}

function LibraryDetailModal({
  item,
  onClose,
  onContinue,
  onExport,
  onDelete,
  onRemoveLogo,
}: {
  item: YasLibraryItem;
  onClose: () => void;
  /** Reopen this item in its creator to refine & re-finalize. */
  onContinue?: () => void;
  /** Animation only: seed a Video Editor project with this MP4 and open it. */
  onExport?: () => void | Promise<void>;
  /** Permanently delete this item (server-side + cached grid). When provided,
   *  a "Delete" button shows next to Download. Wired for posters only. */
  onDelete?: () => void | Promise<void>;
  /** Re-render this still with the 42 logo removed (kept as a new library item).
   *  When provided, a "Remove logo" button shows. Wired for poster stills only. */
  onRemoveLogo?: () => void | Promise<void>;
}) {
  const [mediaError, setMediaError] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [removingLogo, setRemovingLogo] = useState(false);
  const toast = useToast();
  const confirm = useConfirm();
  const meta = TYPE_META[item.type];

  // Escape, scroll lock, focus trap and focus restore in one place. The first
  // two were already here; the trap is the new part -- without it Tab walked
  // straight out of the dialog and into the page behind the overlay, which
  // makes a dialog modal only to a mouse.
  const panelRef = useRef<HTMLDivElement>(null);
  const { trapTab } = useModalChrome({ onClose, panelRef });

  const cost =
    item.cost_usd != null && item.cost_usd !== ""
      ? `$${Number(item.cost_usd).toFixed(2)}`
      : null;
  // Prefer the explicit full-res proxy download_url (forces a filename, serves the
  // original — not a transform thumbnail). Fall back to the display URLs for older
  // item shapes / local rows.
  const downloadUrl = item.download_url || item.video_url || item.thumbnail_url;
  // Only our own /api routes understand `download=1`. The display URLs can be
  // presigned storage URLs, where every query parameter is signed and an extra
  // one turns the download into a 403, so those are linked as they are.
  const downloadHref =
    downloadUrl && downloadUrl.startsWith("/api/")
      ? downloadUrl + (downloadUrl.includes("?") ? "&" : "?") + "download=1"
      : downloadUrl;
  // Only finished work can be reopened to refine & re-finalize.
  const canResume =
    item.status === "ready" || item.status === "saved_to_assets";

  return createPortal(
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 110,
        background: "rgba(0,0,0,0.78)",
        backdropFilter: "blur(2px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "16px",
      }}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        onKeyDown={trapTab}
        onClick={(e) => e.stopPropagation()}
        className="relative bg-bg rounded-2xl shadow-2xl border border-border overflow-hidden flex flex-col"
        style={{ width: "min(560px, 100%)", maxHeight: "min(900px, 94vh)" }}
      >
        <header className="shrink-0 px-4 py-3 border-b border-border flex items-center gap-2.5 bg-bg">
          <span className={`font-mono text-3xs px-1.5 py-0.5 rounded ${meta.pill}`}>
            {meta.label}
          </span>
          <h2 className="text-sm font-semibold text-fg flex-1 truncate">
            {item.topic || "(untitled)"}
          </h2>
          <button
            onClick={onClose}
            aria-label="Close"
            className="w-7 h-7 rounded-full bg-bg-muted hover:bg-border text-fg flex items-center justify-center text-base leading-none transition-colors"
          >
            ×
          </button>
        </header>

        <div className="flex-1 min-h-0 overflow-y-auto">
          <div className="aspect-[3/4] bg-black flex items-center justify-center">
            {mediaError ? (
              <p className="text-[12px] text-danger p-4 text-center">
                Couldn’t load this {item.has_video ? "video" : "image"} (the file
                may be unavailable).
              </p>
            ) : item.has_video && item.video_url ? (
              <video
                src={item.video_url}
                controls
                autoPlay
                playsInline
                onError={() => setMediaError(true)}
                className="w-full h-full object-contain bg-black"
              />
            ) : item.thumbnail_url ? (
              <img
                // Detail view shows the FULL-res original (download_url proxy),
                // not the small grid transform thumbnail, so it isn't soft.
                src={item.download_url || item.thumbnail_url}
                alt=""
                onError={() => setMediaError(true)}
                className="w-full h-full object-contain bg-black"
              />
            ) : (
              <p className="text-[12px] text-muted p-4 text-center">
                No preview available.
              </p>
            )}
          </div>

          <dl className="px-4 py-3 grid grid-cols-2 gap-x-4 gap-y-2 text-[12px]">
            <Meta label="Status" value={item.status} />
            <Meta label="Created" value={fmtRel(item.created_at)} />
            {item.image_model && <Meta label="Engine" value={item.image_model} />}
            {cost && <Meta label="Cost" value={cost} />}
          </dl>
        </div>

        <footer className="shrink-0 px-4 py-3 border-t border-border bg-bg-muted/50 flex items-center gap-3">
          <p className="text-2xs font-mono text-muted flex-1">#{item.id}</p>
          <button onClick={onClose} className="btn btn-ghost text-xs">
            Close
          </button>
          {downloadHref && (
            <a
              href={downloadHref}
              download
              className="btn btn-ghost text-xs"
            >
              {item.has_video ? "Download video" : "Download image"}
            </a>
          )}
          {onRemoveLogo && (
            <button
              onClick={async () => {
                if (removingLogo) return;
                setRemovingLogo(true);
                try {
                  await onRemoveLogo();
                  toast.success("Logo removed", "A new version was added to your library");
                } catch (err: any) {
                  toast.error("Couldn’t remove logo", err?.message || String(err));
                  setRemovingLogo(false);
                }
              }}
              disabled={removingLogo}
              title="Re-render this poster without the 42 logo (kept as a new version)"
              className="btn btn-ghost text-xs disabled:opacity-50"
            >
              {removingLogo ? "Removing logo…" : "Remove logo"}
            </button>
          )}
          {onDelete && (
            <button
              onClick={async () => {
                if (deleting) return;
                if (
                  !(await confirm({
                    title: "Delete permanently?",
                    message:
                      "It will be removed from the library. This can’t be undone.",
                    confirmLabel: "Delete",
                    danger: true,
                  }))
                )
                  return;
                setDeleting(true);
                try {
                  await onDelete();
                  toast.success("Deleted");
                } catch (err: any) {
                  toast.error("Delete failed", err?.message || String(err));
                  setDeleting(false);
                }
              }}
              disabled={deleting}
              className="btn btn-ghost text-xs text-danger hover:bg-danger/10 hover:border-danger/30 disabled:opacity-50"
            >
              {deleting ? "Deleting…" : "Delete"}
            </button>
          )}
          {onExport && (
            <button
              onClick={async () => {
                if (exporting) return;
                setExporting(true);
                try {
                  await onExport();
                } catch (err: any) {
                  toast.error("Export failed", err?.message || String(err));
                } finally {
                  setExporting(false);
                }
              }}
              disabled={exporting}
              className="btn btn-ghost text-xs disabled:opacity-50"
            >
              {exporting ? "Exporting…" : "Export to video editor"}
            </button>
          )}
          {onContinue && canResume && (
            <button onClick={onContinue} className="btn btn-primary text-xs">
              Continue editing
            </button>
          )}
        </footer>
      </div>
    </div>,
    document.body,
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="eyebrow">{label}</dt>
      <dd className="text-fg/85 truncate">{value}</dd>
    </div>
  );
}
