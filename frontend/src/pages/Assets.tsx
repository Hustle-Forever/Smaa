import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { AssetDetailDrawer } from "../components/assets/AssetDetail";
import { Thumb } from "../components/Thumb";
import { useToast } from "../components/ToastProvider";
import { api } from "../lib/api";
import { fmtRel } from "../lib/format";
import { useProducerAssetsStore } from "../lib/store";
import type {
  ProducerAssetCategory,
  ProducerAssetKind,
  ProducerAssetRow,
  ProducerAssetsList,
} from "../lib/types";

const CATEGORIES = [
  { key: "img_poster", label: "Poster", color: "bg-blue-500/80", desc: "Static images, no human face" },
  { key: "img_poster_human", label: "Poster + Human", color: "bg-success/80", desc: "Designed posters with human faces" },
  { key: "img_human_character", label: "Human Characters", color: "bg-rose-500/80", desc: "AI-generated people — saved as Human Element Video presenter references" },
  { key: "img_poster_animated", label: "Animated poster", color: "bg-warning/80", desc: "Frame extracted from an animated graphic video" },
  { key: "video_interview", label: "Talking head", color: "bg-violet-500/80", desc: "Character references extracted from interview reels" },
  { key: "img_intro", label: "Intro", color: "bg-cyan-500/80", desc: "Opening title cards / intro text from talking-head reels" },
  { key: "img_outro", label: "Outro", color: "bg-rose-500/80", desc: "End cards / CTA frames from talking-head reels" },
  { key: "img_bg", label: "Background", color: "bg-indigo-500/80", desc: "Locations & environments — vision-analyzed for scene reference" },
] as const;

// Categories whose upload runs vision analysis + embedding (slower per file,
// shown with an "Analyzing…" progress label instead of "Uploading…").
const AI_ANALYZE_CATEGORIES = new Set<string>(["img_bg"]);

// "Kind" sections — fonts / stickers / audio. Unlike the content-category tabs
// above (which key on metadata.content_category), these filter by asset_kind /
// category and each carry their own inline upload (the analyzing endpoint).
type KindSection = {
  key: string;
  label: string;
  color: string;
  desc: string;
  match: (a: ProducerAssetRow) => boolean;
  totalKey: keyof ProducerAssetsList["totals"];
  upload: { asset_kind: ProducerAssetKind; category?: ProducerAssetCategory; accept: string };
};

const KIND_SECTIONS: KindSection[] = [
  {
    key: "k_font",
    label: "Fonts",
    color: "bg-pink-500/80",
    desc: "Brand typefaces (.ttf / .otf) for text overlays",
    match: (a) => a.asset_kind === "font",
    totalKey: "fonts",
    upload: { asset_kind: "font", accept: ".ttf,.otf,.woff,.woff2,font/*" },
  },
  {
    key: "k_sticker",
    label: "Stickers",
    color: "bg-fuchsia-500/80",
    desc: "Logos · graphics · overlays — AI-tagged for retrieval",
    match: (a) => a.category === "sticker",
    totalKey: "stickers",
    upload: { asset_kind: "image", category: "sticker", accept: "image/*" },
  },
  {
    key: "k_audio",
    label: "Audio / Music",
    color: "bg-success/80",
    desc: "Music + sfx — AI-tagged by mood / genre / energy",
    match: (a) => a.asset_kind === "audio",
    totalKey: "audio",
    upload: { asset_kind: "audio", accept: "audio/*" },
  },
];

export function AssetsPage() {
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const store = useProducerAssetsStore();

  // One effect, explicit `force`. Previously these were TWO effects — fetch
  // declared before invalidate — and React runs effects in declaration order, so
  // after an upload the fetch hit the still-fresh 24h cache and returned early,
  // and only THEN was the cache marked stale. The new asset didn't appear until a
  // reload, which re-paid the whole list download. Order-dependence was the bug,
  // so make the intent explicit rather than reshuffling the effects.
  useEffect(() => {
    store.fetch({ limit: 500 }, { force: refreshKey > 0 });
  }, [refreshKey]);

  const allItems = store.items;
  const kindSection = KIND_SECTIONS.find((s) => s.key === selectedCategory) ?? null;
  const filteredItems = !selectedCategory
    ? allItems
    : kindSection
    ? allItems.filter(kindSection.match)
    : allItems.filter((a) => (a.metadata as any)?.content_category === selectedCategory);
  const data = { items: filteredItems, totals: store.totals };
  const loading = store.loading;
  const error = store.error;

  return (
    <div className="space-y-8">
      <header className="flex items-start gap-6">
        <div className="flex-1 min-w-0">
          <p className="eyebrow mb-1.5">Assets library</p>
          <h1 className="h1">Saved assets</h1>
          <p className="lead mt-2 max-w-2xl">
            Instagram posts saved as reusable references, or manually uploaded images.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setUploadOpen(true)}
          className="btn btn-primary shrink-0"
        >
          <span aria-hidden className="text-base leading-none">+</span>
          Add asset
        </button>
      </header>

      {error && (
        <div className="card border-danger/30 bg-danger/5 p-4 text-sm text-danger">
          {error}
        </div>
      )}

      {selectedCategory === null ? (
        <CategoryOverview
          totals={data?.totals ?? null}
          loading={loading && !data}
          onSelect={setSelectedCategory}
        />
      ) : (
        <CategoryDetail
          category={selectedCategory}
          kindSection={kindSection}
          items={data?.items ?? []}
          loading={loading}
          onBack={() => setSelectedCategory(null)}
          onOpen={(id) => setOpenId(id)}
          onUploaded={() => setRefreshKey((k) => k + 1)}
        />
      )}

      {openId != null && (
        <AssetDetailDrawer
          assetId={openId}
          onClose={() => setOpenId(null)}
          onChanged={() => setRefreshKey((k) => k + 1)}
        />
      )}

      {uploadOpen && (
        <UploadDialog
          onClose={() => setUploadOpen(false)}
          onUploaded={() => {
            setRefreshKey((k) => k + 1);
            setUploadOpen(false);
          }}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Category overview — 2 cards
// ---------------------------------------------------------------------------

function CategoryOverview({
  totals,
  loading,
  onSelect,
}: {
  totals: ProducerAssetsList["totals"] | null;
  loading: boolean;
  onSelect: (cat: string) => void;
}) {
  const card = (
    key: string,
    label: string,
    color: string,
    desc: string,
    count: number,
  ) => (
    <button
      key={key}
      onClick={() => onSelect(key)}
      className="card card-hover text-left p-5 flex flex-col gap-3 group"
    >
      <div className="flex items-center gap-2">
        <span className={`w-3 h-3 rounded-full ${color}`} />
        <span className="font-semibold text-fg text-sm">{label}</span>
      </div>
      <p className="text-xs text-muted leading-relaxed">{desc}</p>
      <p className="text-2xl font-bold text-fg tabular-nums mt-auto">{loading ? "—" : count}</p>
      <p className="text-3xs font-mono text-muted uppercase tracking-wider2">assets</p>
    </button>
  );

  return (
    <div className="space-y-6">
      <div>
        <p className="eyebrow mb-3">Posters · characters · backgrounds</p>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 max-w-5xl">
          {CATEGORIES.map((cat) =>
            card(cat.key, cat.label, cat.color, cat.desc, totals ? (totals as any)[`cat_${cat.key}`] ?? 0 : 0),
          )}
        </div>
      </div>
      <div>
        <p className="eyebrow mb-3">Fonts · stickers · audio</p>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 max-w-5xl">
          {KIND_SECTIONS.map((s) =>
            card(s.key, s.label, s.color, s.desc, totals ? ((totals as any)[s.totalKey] ?? 0) : 0),
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Category detail — grid of assets
// ---------------------------------------------------------------------------

function CategoryDetail({
  category,
  kindSection,
  items,
  loading,
  onBack,
  onOpen,
  onUploaded,
}: {
  category: string;
  kindSection: KindSection | null;
  items: ProducerAssetRow[];
  loading: boolean;
  onBack: () => void;
  onOpen: (id: number) => void;
  onUploaded: () => void;
}) {
  const cat = CATEGORIES.find((c) => c.key === category);
  const label = kindSection?.label ?? cat?.label ?? category;
  const color = kindSection?.color ?? cat?.color ?? "bg-gray-500";
  const [search, setSearch] = useState("");
  const [analyzing, setAnalyzing] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const toast = useToast();

  const filtered = items.filter((a) => {
    if (!search.trim()) return true;
    const hay = [a.display_name ?? "", a.preview ?? "", a.slug].join(" ").toLowerCase();
    return hay.includes(search.trim().toLowerCase());
  });

  async function onUploadKind(files: File[]) {
    if (!kindSection || !files.length) return;
    setAnalyzing(true);
    const failed: string[] = [];
    let ok = 0;
    for (const file of files) {
      try {
        await api.producerAssets.upload({
          file,
          asset_kind: kindSection.upload.asset_kind,
          category: kindSection.upload.category,
          display_name: file.name,
        });
        ok += 1;
      } catch (e: any) {
        failed.push(file.name);
      }
    }
    setAnalyzing(false);
    if (ok) toast.info(`Saved ${ok} to ${label}`, "Assets added");
    if (failed.length) toast.error("Some uploads failed", failed.join(", "));
    onUploaded();
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <button onClick={onBack} className="btn btn-ghost text-sm">
          ← Back
        </button>
        <div className="flex items-center gap-2">
          <span className={`w-3 h-3 rounded-full ${color}`} />
          <h2 className="text-lg font-semibold text-fg">{label}</h2>
        </div>
        <span className="font-mono text-xs text-muted ml-2">
          {filtered.length} asset{filtered.length !== 1 ? "s" : ""}
        </span>
        {kindSection && (
          <>
            <button
              onClick={() => fileRef.current?.click()}
              disabled={analyzing}
              className="btn btn-primary text-sm ml-auto"
            >
              {analyzing ? "Analyzing…" : `+ Upload ${label.toLowerCase()}`}
            </button>
            <input
              ref={fileRef}
              type="file"
              accept={kindSection.upload.accept}
              multiple
              className="hidden"
              onChange={(e) => {
                const fs = Array.from(e.target.files || []);
                if (fs.length) void onUploadKind(fs);
                e.target.value = "";
              }}
            />
          </>
        )}
      </div>

      <input
        type="text"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="Search by name or description…"
        className="w-full max-w-md rounded border border-border bg-bg-subtle text-sm px-3 py-2"
      />

      {loading ? (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="h-48 rounded-xl bg-bg-muted animate-pulse" />
          ))}
        </div>
      ) : filtered.length === 0 ? (
        <p className="text-sm text-muted py-8">
          No assets in this category yet. Go to the Instagram tab, open a post,
          and click "Add to Asset" to save it here.
        </p>
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
          {filtered.map((a) => (
            <AssetCard key={a.id} asset={a} onClick={() => onOpen(a.id)} />
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Asset card
// ---------------------------------------------------------------------------

function AssetCard({ asset, onClick }: { asset: ProducerAssetRow; onClick: () => void }) {
  const cat = CATEGORIES.find(
    (c) => c.key === (asset.metadata as any)?.content_category,
  );
  const isFont = asset.asset_kind === "font";
  const isAudio = asset.asset_kind === "audio";
  const isVideo = asset.asset_kind === "video";
  const mood = (asset.attributes as any)?.mood as string | undefined;

  return (
    <button
      onClick={onClick}
      className="card card-hover text-left p-0 overflow-hidden flex flex-col group"
      title={asset.display_name || asset.slug}
    >
      <div className="aspect-square bg-bg-muted relative overflow-hidden">
        {isAudio ? (
          <div className="w-full h-full flex flex-col items-center justify-center gap-1 bg-emerald-950/40 text-emerald-300">
            <span className="text-3xl leading-none">♪</span>
            {asset.duration_sec ? (
              <span className="text-3xs font-mono text-emerald-300/70">{Math.round(asset.duration_sec)}s</span>
            ) : null}
          </div>
        ) : isVideo ? (
          <div className="w-full h-full relative bg-black">
            {/* `#t=0.1` seeks to the first frame so the browser paints a poster
                from metadata alone — no playback, no backend thumbnail needed. */}
            <video
              src={`${api.producerAssets.fileUrl(asset.id)}#t=0.1`}
              muted
              playsInline
              preload="metadata"
              className="w-full h-full object-cover"
            />
            <span className="absolute inset-0 flex items-center justify-center pointer-events-none">
              <span className="w-9 h-9 rounded-full bg-black/50 text-white flex items-center justify-center text-sm leading-none">
                ▶
              </span>
            </span>
            {asset.duration_sec ? (
              <span className="absolute bottom-1.5 right-1.5 font-mono text-[9px] px-1.5 py-0.5 rounded bg-black/65 text-white/90">
                {Math.round(asset.duration_sec)}s
              </span>
            ) : null}
          </div>
        ) : (
          // thumbUrlFor, not fileUrl: these tiles are ~250px and the originals
          // average 3.1 MB (9.3 MB for backgrounds) — the Background category
          // alone was 563 MB per grid paint. The fallback to the full-res proxy
          // keeps the tile working if a transform ever 400s.
          <Thumb
            src={isFont ? api.producerAssets.previewUrl(asset.id) : api.producerAssets.thumbUrlFor(asset)}
            fallbackSrc={isFont ? null : api.producerAssets.fileUrl(asset.id)}
            className={`w-full h-full ${isFont ? "bg-white p-1" : ""}`}
            objectFit={isFont ? "contain" : "cover"}
          />
        )}
        {cat && (
          <span className={`absolute top-2 left-2 font-mono text-3xs px-1.5 py-0.5 rounded text-white ${cat.color}`}>
            {cat.label}
          </span>
        )}
        {mood && (
          <span className="absolute top-2 right-2 font-mono text-[9px] px-1.5 py-0.5 rounded bg-black/55 text-white/90 capitalize">
            {mood}
          </span>
        )}
      </div>
      <div className="p-3 space-y-1 flex-1 flex flex-col">
        <p className="text-xs text-fg/80 line-clamp-2">
          {asset.display_name || asset.preview || "(no caption)"}
        </p>
        <p className="mt-auto text-2xs font-mono text-muted">
          {fmtRel(asset.created_at)}
        </p>
      </div>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Upload dialog — local file + category picker
// ---------------------------------------------------------------------------

function UploadDialog({
  onClose,
  onUploaded,
}: {
  onClose: () => void;
  onUploaded: () => void;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [category, setCategory] = useState<string>("");
  const [displayName, setDisplayName] = useState("");
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState<
    { done: number; total: number; current: string } | null
  >(null);
  const [errors, setErrors] = useState<
    Array<{ filename: string; reason: string }>
  >([]);
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const toast = useToast();

  // Object URLs for thumbnails — revoke on unmount/change to avoid leaks
  const previewUrls = useMemo(
    () => files.map((f) => URL.createObjectURL(f)),
    [files],
  );
  useEffect(() => {
    return () => {
      for (const u of previewUrls) URL.revokeObjectURL(u);
    };
  }, [previewUrls]);

  const isAiCategory = AI_ANALYZE_CATEGORIES.has(category);
  const canSubmit = files.length > 0 && category !== "" && !uploading;

  function addFiles(picked: FileList | File[] | null | undefined) {
    if (!picked) return;
    const next = Array.from(picked).filter(
      (f) => f.type.startsWith("image/") || f.type.startsWith("video/"),
    );
    if (next.length === 0) return;
    // Deduplicate by name+size — drag-and-drop can fire twice
    setFiles((prev) => {
      const seen = new Set(prev.map((f) => `${f.name}|${f.size}`));
      const merged = [...prev];
      for (const f of next) {
        const key = `${f.name}|${f.size}`;
        if (!seen.has(key)) {
          seen.add(key);
          merged.push(f);
        }
      }
      return merged;
    });
  }

  function removeFile(idx: number) {
    setFiles((prev) => prev.filter((_, i) => i !== idx));
  }

  async function handleUpload() {
    if (files.length === 0 || !category) return;
    setUploading(true);
    setErrors([]);
    const failed: Array<{ filename: string; reason: string }> = [];
    let succeeded = 0;

    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      setProgress({ done: i, total: files.length, current: f.name });
      try {
        const form = new FormData();
        form.append("file", f, f.name);
        form.append("content_category", category);
        // Only honour the single display_name field when there's exactly
        // one file — for multi-uploads each asset auto-derives its name.
        if (files.length === 1 && displayName) {
          form.append("display_name", displayName);
        }
        const resp = await fetch("/api/agents/producer/assets/upload-simple", {
          method: "POST",
          body: form,
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.detail || `Upload failed (${resp.status})`);
        }
        succeeded += 1;
      } catch (e: any) {
        failed.push({ filename: f.name, reason: e?.message || String(e) });
      }
    }

    setProgress({ done: files.length, total: files.length, current: "" });
    setUploading(false);

    const catLabel = CATEGORIES.find((c) => c.key === category)?.label ?? category;
    if (failed.length === 0) {
      toast.info(
        `Saved ${succeeded} to assets · ${catLabel}`,
        succeeded === 1 ? "Asset added" : "Assets added",
      );
      onUploaded();
    } else {
      setErrors(failed);
      if (succeeded > 0) {
        toast.info(
          `${succeeded} saved, ${failed.length} failed · ${catLabel}`,
          "Partial upload",
        );
        // Refresh the assets list (don't auto-close so user can see errors).
        // Drop the successfully-uploaded files from the list so retry only
        // re-attempts the failures.
        const failedSet = new Set(failed.map((e) => e.filename));
        setFiles((prev) => prev.filter((f) => failedSet.has(f.name)));
      } else {
        toast.error("Upload failed", failed[0].reason);
      }
    }
  }

  // Compute the upload button label
  const buttonLabel = (() => {
    if (uploading && progress) {
      const verb = isAiCategory ? "Analyzing" : "Uploading";
      const where =
        progress.total > 1
          ? ` ${progress.done + 1} / ${progress.total}`
          : "";
      const fn = progress.current ? ` — ${progress.current}` : "";
      return `${verb}${where}${fn}…`;
    }
    if (files.length === 0) return "Upload";
    if (files.length === 1) return "Upload";
    return `Upload ${files.length} files`;
  })();

  return createPortal(
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 100,
        background: "rgba(0,0,0,0.7)",
        backdropFilter: "blur(2px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "16px",
      }}
      onClick={() => !uploading && onClose()}
      role="dialog"
      aria-modal="true"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="bg-bg rounded-2xl shadow-2xl border border-border overflow-hidden flex flex-col"
        style={{ width: "min(560px, 100%)", maxHeight: "min(820px, 92vh)" }}
      >
        <header className="px-5 py-4 border-b border-border flex items-center justify-between">
          <h2 className="text-base font-semibold text-fg">Add asset</h2>
          <button
            onClick={onClose}
            disabled={uploading}
            className="w-7 h-7 rounded-full bg-bg-muted hover:bg-border disabled:opacity-50 text-fg flex items-center justify-center text-base"
          >
            ×
          </button>
        </header>

        <div className="p-5 space-y-5 overflow-y-auto">
          {/* Category — required */}
          <div className="space-y-2">
            <p className="eyebrow">Category <span className="text-danger">*</span></p>
            <div className="grid grid-cols-2 gap-2.5">
              {CATEGORIES.map((cat) => (
                <button
                  key={cat.key}
                  type="button"
                  onClick={() => setCategory(cat.key)}
                  disabled={uploading}
                  className={[
                    "rounded-lg border-2 p-3 text-left transition",
                    category === cat.key
                      ? "border-accent bg-accent/5"
                      : "border-border hover:border-border/80",
                    uploading ? "opacity-60 cursor-not-allowed" : "",
                  ].join(" ")}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <span className={`w-2.5 h-2.5 rounded-full ${cat.color}`} />
                    <span className="text-sm font-medium text-fg">{cat.label}</span>
                  </div>
                  <p className="text-2xs text-muted">{cat.desc}</p>
                </button>
              ))}
            </div>
            {!category && (
              <p className="text-xs text-muted">Select a category to continue</p>
            )}
            {isAiCategory && category && (
              <p className="text-2xs text-muted leading-relaxed">
                Each background runs a vision pass (palette, lighting,
                location, mood) and gets embedded for retrieval. Expect ~10-20s
                per file.
              </p>
            )}
          </div>

          {/* Drop zone — multi-file picker + drag-and-drop */}
          <div className="space-y-2">
            <p className="eyebrow">
              Image / video file{files.length > 1 ? "s" : ""}{" "}
              <span className="text-danger">*</span>
            </p>
            <input
              ref={fileRef}
              type="file"
              accept="image/*,video/mp4,video/quicktime,video/webm,video/x-matroska,.mp4,.mov,.webm,.mkv"
              multiple
              onChange={(e) => {
                addFiles(e.target.files);
                // Reset so picking the same files again refires onChange
                if (fileRef.current) fileRef.current.value = "";
              }}
              className="hidden"
            />
            <div
              onClick={() => !uploading && fileRef.current?.click()}
              onDragEnter={(e) => {
                e.preventDefault();
                e.stopPropagation();
                if (!uploading) setDragOver(true);
              }}
              onDragOver={(e) => {
                e.preventDefault();
                e.stopPropagation();
                if (!uploading) setDragOver(true);
              }}
              onDragLeave={(e) => {
                e.preventDefault();
                e.stopPropagation();
                setDragOver(false);
              }}
              onDrop={(e) => {
                e.preventDefault();
                e.stopPropagation();
                setDragOver(false);
                if (!uploading) addFiles(e.dataTransfer.files);
              }}
              className={[
                "w-full rounded-lg border-2 border-dashed p-6 text-center transition cursor-pointer",
                dragOver
                  ? "border-accent bg-accent/5"
                  : "border-border hover:border-accent/50",
                uploading ? "opacity-60 cursor-not-allowed" : "",
              ].join(" ")}
            >
              {files.length === 0 ? (
                <span className="text-sm text-muted">
                  {dragOver
                    ? "Drop files here"
                    : "Click to select images or videos, or drag-and-drop here"}
                </span>
              ) : (
                <span className="text-sm text-fg">
                  {files.length} file{files.length === 1 ? "" : "s"} ready
                  {" · "}
                  <span className="text-muted">click to add more</span>
                </span>
              )}
            </div>
          </div>

          {/* Thumbnails — only when files picked */}
          {files.length > 0 && (
            <div className="space-y-1.5">
              <p className="font-mono text-3xs text-muted uppercase tracking-wider2">
                Selected files
              </p>
              <div className="grid grid-cols-3 gap-2">
                {files.map((f, i) => (
                  <div
                    key={`${f.name}|${f.size}|${i}`}
                    className="relative aspect-square rounded-lg overflow-hidden bg-bg-muted group"
                  >
                    {f.type.startsWith("video/") ? (
                      <video
                        src={previewUrls[i]}
                        muted
                        playsInline
                        preload="metadata"
                        className="w-full h-full object-cover"
                      />
                    ) : (
                      <img
                        src={previewUrls[i]}
                        alt={f.name}
                        className="w-full h-full object-cover"
                      />
                    )}
                    <span className="absolute bottom-0 inset-x-0 px-1.5 py-0.5 text-3xs text-white bg-black/60 truncate font-mono">
                      {f.name}
                    </span>
                    {!uploading && (
                      <button
                        type="button"
                        onClick={() => removeFile(i)}
                        aria-label={`Remove ${f.name}`}
                        className="absolute top-1 right-1 w-5 h-5 rounded-full bg-black/70 text-white text-xs flex items-center justify-center opacity-0 group-hover:opacity-100 hover:bg-danger transition"
                      >
                        ×
                      </button>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Display name — single-file only */}
          {files.length <= 1 && (
            <div className="space-y-2">
              <p className="eyebrow">Display name <span className="text-muted text-3xs">(optional)</span></p>
              <input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                disabled={uploading}
                placeholder="Short description…"
                className="w-full rounded border border-border bg-bg-subtle text-sm px-3 py-2 disabled:opacity-60"
              />
            </div>
          )}

          {/* Progress bar — shown while uploading */}
          {uploading && progress && progress.total > 1 && (
            <div className="space-y-1.5">
              <div className="h-2 bg-bg-muted rounded-full overflow-hidden">
                <div
                  className="h-full bg-accent transition-all duration-300"
                  style={{
                    width: `${(progress.done / progress.total) * 100}%`,
                  }}
                />
              </div>
              <p className="text-2xs text-muted font-mono">
                {progress.done} / {progress.total} complete
              </p>
            </div>
          )}

          {/* Per-file errors on partial failure */}
          {errors.length > 0 && (
            <div className="space-y-1.5">
              <p className="font-mono text-3xs text-danger uppercase tracking-wider2">
                Failed ({errors.length})
              </p>
              <ul className="space-y-1 text-xs text-danger">
                {errors.map((e, i) => (
                  <li key={i} className="font-mono">
                    {e.filename}: {e.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        <footer className="px-5 py-4 border-t border-border flex justify-end gap-2">
          <button
            onClick={onClose}
            disabled={uploading}
            className="btn btn-ghost text-sm"
          >
            Cancel
          </button>
          <button
            onClick={handleUpload}
            disabled={!canSubmit}
            className="btn btn-primary text-sm"
          >
            {buttonLabel}
          </button>
        </footer>
      </div>
    </div>,
    document.body,
  );
}
