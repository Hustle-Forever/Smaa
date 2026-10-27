/**
 * Video Editor · multi-select media picker
 *
 * Opens from the Media bin "Add" button. Grids the user's generated clips,
 * stitched finals, keyframe/poster stills and uploads (from
 * useVideoSourcesStore) with hover-to-play preview, search, and a CATEGORY
 * chip filter; "Add to bin" pushes the picks into the project's media bin.
 * Portaled to document.body (CLAUDE.md modal trap).
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { useModalChrome } from "../../../lib/useModalChrome";
import { createPortal } from "react-dom";

import { api } from "../../../lib/api";
import { useVideoEditorStore, useVideoSourcesStore } from "../../../lib/store";
import type { MediaBinEntry, VideoSource, VideoSourceCategory } from "../../../lib/types";

function srcKey(s: { kind: string; id: number }) {
  return `${s.kind}-${s.id}`;
}

/** Canonical chip order + non-technical labels. */
const CATEGORY_LABELS: [VideoSourceCategory, string][] = [
  ["talking_head_v2", "Human Element Video"],
  ["poster_animation", "Poster animations"],
  ["video_edit", "Video edits"],
  ["final", "Finals"],
  ["still", "Stills"],
  ["poster", "Posters"],
  ["upload", "Uploads"],
  ["bookend_intro", "Intros"],
  ["bookend_outro", "Outros"],
];

/** "intro" / "outro" when the source is a bookend card, else null. */
function bookendRoleOf(s: VideoSource): "intro" | "outro" | null {
  if (s.category === "bookend_intro") return "intro";
  if (s.category === "bookend_outro") return "outro";
  return null;
}

/** Category of a source, with a kind-based fallback for stale cached items. */
function categoryOf(s: VideoSource): VideoSourceCategory {
  if (s.category) return s.category;
  if (s.kind === "upload") return "upload";
  if (s.kind === "composition") return "final";
  if (s.kind === "still") return "still";
  return "poster_animation";
}

/** Definitive image test: stills (plain keyframes AND posters) are the only
 * image kind. Uploads are video-only; animations/compositions are video. */
function isImageSource(s: VideoSource): boolean {
  return s.kind === "still";
}

export function VideoSourcePickerModal({
  onClose,
  onPick,
}: {
  onClose: () => void;
  /** When provided, picked sources go to the caller instead of the media bin. */
  onPick?: (sources: VideoSource[]) => void;
}) {
  const store = useVideoSourcesStore();
  const addToBin = useVideoEditorStore((s) => s.addToBin);
  const addBookendFromSource = useVideoEditorStore((s) => s.addBookendFromSource);
  const [search, setSearch] = useState("");
  const [cat, setCat] = useState<"all" | VideoSourceCategory>("all");
  const [mediaType, setMediaType] = useState<"all" | "video" | "image">("all");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [uploadPct, setUploadPct] = useState<number | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  async function onUpload(file: File) {
    setUploadPct(0);
    try {
      const src = await api.yasProducer.videoEditor.upload(file, (p) => setUploadPct(p));
      useVideoSourcesStore.getState().addItem(src);
      setPicked((p) => new Set(p).add(`${src.kind}-${src.id}`));
    } catch {
      /* ignore */
    } finally {
      setUploadPct(null);
    }
  }

  // The fetch was sharing an effect with the dialog chrome. They have nothing
  // to do with each other, and the chrome half now lives in useModalChrome.
  useEffect(() => {
    store.fetch({ limit: 500 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const panelRef = useRef<HTMLDivElement>(null);
  const { trapTab } = useModalChrome({ onClose, panelRef });

  // Only offer chips for categories that actually have items.
  const categories = useMemo(() => {
    const present = new Set(store.items.map(categoryOf));
    return CATEGORY_LABELS.filter(([c]) => present.has(c));
  }, [store.items]);

  const items = useMemo(() => {
    const q = search.trim().toLowerCase();
    return store.items
      .filter((s) => categoryOf(s) !== "talking_head")
      .filter((it) => {
        if (cat !== "all" && categoryOf(it) !== cat) return false;
        if (mediaType === "video" && isImageSource(it)) return false;
        if (mediaType === "image" && !isImageSource(it)) return false;
        if (q && !(it.title || "").toLowerCase().includes(q)) return false;
        return true;
      });
  }, [store.items, search, cat, mediaType]);

  function toggle(s: VideoSource) {
    setPicked((p) => {
      const n = new Set(p);
      const k = srcKey(s);
      if (n.has(k)) n.delete(k);
      else n.add(k);
      return n;
    });
  }

  function confirm() {
    if (onPick) {
      const chosen = store.items.filter((it) => picked.has(srcKey(it)));
      onPick(chosen);
      onClose();
      return;
    }
    for (const it of store.items) {
      if (picked.has(srcKey(it))) {
        addToBin({ kind: it.kind as MediaBinEntry["kind"], id: it.id, title: it.title, durationInFrames: it.durationInFrames });
      }
    }
    onClose();
  }

  return createPortal(
    <div
      style={{ position: "fixed", inset: 0, zIndex: 110, background: "rgba(0,0,0,0.78)", backdropFilter: "blur(2px)", display: "flex", alignItems: "center", justifyContent: "center", padding: "16px" }}
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
        style={{ width: "min(820px, 100%)", maxHeight: "min(86vh, 760px)" }}
      >
        <header className="shrink-0 px-4 py-3 border-b border-border flex items-center gap-3">
          <h2 className="text-sm font-semibold text-fg">Add a clip</h2>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search your clips…"
            className="flex-1 max-w-xs rounded-md border border-border bg-bg-subtle px-2.5 py-1 text-[12px] focus:outline-none focus:border-accent"
          />
          <button onClick={() => fileRef.current?.click()} disabled={uploadPct != null} className="btn btn-ghost text-2xs px-2 py-0.5">
            {uploadPct != null ? `Uploading ${uploadPct}%` : "↑ Upload"}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="video/*"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) onUpload(f);
              e.target.value = "";
            }}
          />
          <button onClick={onClose} aria-label="Close" className="w-7 h-7 rounded-full bg-bg-muted hover:bg-border text-fg flex items-center justify-center text-base">
            ×
          </button>
        </header>

        <div className="shrink-0 px-4 py-2 border-b border-border flex flex-wrap items-center gap-1">
          {/* Type filter — instantly tells videos from images. */}
          <div className="inline-flex rounded-full border border-border overflow-hidden mr-1.5">
            {(["all", "video", "image"] as const).map((t) => (
              <button
                key={t}
                onClick={() => setMediaType(t)}
                className={["px-2 py-0.5 text-[10.5px] capitalize", mediaType === t ? "bg-accent/12 text-fg" : "bg-bg-subtle text-muted hover:bg-bg-muted"].join(" ")}
              >
                {t === "all" ? "All types" : t === "video" ? "Videos" : "Images"}
              </button>
            ))}
          </div>
          <div className="w-px h-4 bg-border mr-1" />
          <button
            onClick={() => setCat("all")}
            className={["px-2 py-0.5 rounded-full border text-[10.5px]", cat === "all" ? "border-accent bg-accent/8 text-fg" : "border-border bg-bg-subtle text-muted hover:bg-bg-muted"].join(" ")}
          >
            All
          </button>
          {categories.map(([c, label]) => (
            <button
              key={c}
              onClick={() => setCat(c)}
              className={["px-2 py-0.5 rounded-full border text-[10.5px]", cat === c ? "border-accent bg-accent/8 text-fg" : "border-border bg-bg-subtle text-muted hover:bg-bg-muted"].join(" ")}
            >
              {label}
            </button>
          ))}
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto p-3">
          {store.loading && store.items.length === 0 && <p className="text-sm text-muted text-center py-12">Loading…</p>}
          {!store.loading && items.length === 0 && <p className="text-sm text-muted text-center py-12">No clips match.</p>}
          <div className="grid grid-cols-3 sm:grid-cols-4 gap-2.5">
            {items.map((it) => (
              <PickTile
                key={srcKey(it)}
                source={it}
                selected={picked.has(srcKey(it))}
                onToggle={() => toggle(it)}
                onAddBookend={(role) => {
                  addBookendFromSource(it, role);
                  onClose();
                }}
              />
            ))}
          </div>
        </div>

        <footer className="shrink-0 px-4 py-3 border-t border-border bg-bg-muted/50 flex items-center gap-3">
          <p className="text-2xs text-muted flex-1">{picked.size} selected</p>
          <button onClick={onClose} className="btn btn-ghost text-xs">Cancel</button>
          <button onClick={confirm} disabled={picked.size === 0} className="btn btn-primary text-xs px-4">
            Add {picked.size > 0 ? `(${picked.size})` : ""}
          </button>
        </footer>
      </div>
    </div>,
    document.body,
  );
}

function PickTile({
  source,
  selected,
  onToggle,
  onAddBookend,
}: {
  source: VideoSource;
  selected: boolean;
  onToggle: () => void;
  onAddBookend: (role: "intro" | "outro") => void;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [hover, setHover] = useState(false);
  const isImage = source.kind === "still"; // images: no hover-to-play, no duration badge
  const bookendRole = bookendRoleOf(source); // intro/outro card → direct-place action
  return (
    // div (not button): bookend tiles nest an "Add as intro/outro" button.
    <div
      role="button"
      tabIndex={0}
      onClick={onToggle}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onToggle();
        }
      }}
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
      className={["relative rounded-lg overflow-hidden border text-left group cursor-pointer", selected ? "border-accent ring-2 ring-accent" : "border-border hover:border-accent/50"].join(" ")}
    >
      <div className="aspect-video bg-black/80 relative">
        {hover && !isImage ? (
          <video ref={videoRef} src={source.url} muted loop playsInline preload="none" poster={source.thumbnail_url} className="w-full h-full object-cover" />
        ) : (
          <img src={source.thumbnail_url} alt="" loading="lazy" className="w-full h-full object-cover" />
        )}
        <span className="absolute bottom-1 right-1 text-[8.5px] font-mono px-1 rounded bg-black/65 text-white/90">
          {isImage ? "IMAGE" : `VIDEO · ${Math.round(source.durationInFrames / 30)}s`}
        </span>
        {selected && (
          <span className="absolute top-1 right-1 w-4 h-4 rounded-full bg-accent text-white text-3xs flex items-center justify-center">✓</span>
        )}
      </div>
      <p className="text-[10.5px] text-fg/80 truncate px-1.5 py-1">{source.title}</p>
      {bookendRole && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            onAddBookend(bookendRole);
          }}
          title={
            bookendRole === "intro"
              ? "Place at the start of the timeline (shifts everything right)"
              : "Place at the end of the timeline"
          }
          className="w-full text-3xs text-accent hover:bg-accent/8 border-t border-border py-1"
        >
          {bookendRole === "intro" ? "Add as intro" : "Add as outro"}
        </button>
      )}
    </div>
  );
}
