import { useEffect, useMemo, useRef, useState } from "react";
import type { ComponentType } from "react";
import { createPortal } from "react-dom";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Check,
  Clapperboard,
  CloudUpload,
  Film,
  Flag,
  Image as ImageIcon,
  Mountain,
  Music,
  Play,
  Plus,
  Search,
  Sticker,
  Type,
  UserRound,
  Users,
  Video,
  X,
} from "lucide-react";

import { AssetDetailDrawer } from "../components/assets/AssetDetail";
import { Thumb } from "../components/Thumb";
import { useToast } from "../components/ToastProvider";
import { api } from "../lib/api";
import { fmtNumber, fmtRel } from "../lib/format";
import { useProducerAssetsStore } from "../lib/store";
import { useStudioCanvas } from "../lib/useStudioCanvas";
import type {
  ProducerAssetCategory,
  ProducerAssetKind,
  ProducerAssetRow,
  ProducerAssetsList,
} from "../lib/types";

// The eight content categories. `key` is what the backend stores in
// metadata.content_category and what the upload sends, so keys never change;
// only the words people read do. Descriptions are written for non-technical
// staff: what the thing IS, in everyday words.
const CATEGORIES: ReadonlyArray<{
  key: string;
  label: string;
  desc: string;
  Icon: ComponentType<any>;
}> = [
  { key: "img_poster", label: "Poster", desc: "Designed images with no people in them", Icon: ImageIcon },
  { key: "img_poster_human", label: "Poster + Human", desc: "Designed posters that show a person", Icon: UserRound },
  { key: "img_human_character", label: "Human Characters", desc: "AI-made people you can use as the presenter in a video", Icon: Users },
  { key: "img_poster_animated", label: "Animated poster", desc: "A still frame taken from a moving graphic", Icon: Film },
  { key: "video_interview", label: "Talking head", desc: "People taken from interview videos, for reference", Icon: Video },
  { key: "img_intro", label: "Intro", desc: "Opening title cards from presenter videos", Icon: Clapperboard },
  { key: "img_outro", label: "Outro", desc: "Closing cards with the call to action", Icon: Flag },
  { key: "img_bg", label: "Background", desc: "Places and rooms, studied by AI so it can match the scene", Icon: Mountain },
];

// Categories whose upload runs vision analysis + embedding (slower per file,
// shown with an "Analyzing…" progress label instead of "Uploading…").
const AI_ANALYZE_CATEGORIES = new Set<string>(["img_bg"]);

// "Kind" sections: fonts / stickers / audio. Unlike the content-category tabs
// above (which key on metadata.content_category), these filter by asset_kind /
// category and each carry their own inline upload (the analyzing endpoint).
type KindSection = {
  key: string;
  label: string;
  desc: string;
  Icon: ComponentType<any>;
  match: (a: ProducerAssetRow) => boolean;
  totalKey: keyof ProducerAssetsList["totals"];
  upload: { asset_kind: ProducerAssetKind; category?: ProducerAssetCategory; accept: string };
};

const KIND_SECTIONS: KindSection[] = [
  {
    key: "k_font",
    label: "Fonts",
    desc: "Brand typefaces for text on posters and videos (.ttf, .otf)",
    Icon: Type,
    match: (a) => a.asset_kind === "font",
    totalKey: "fonts",
    upload: { asset_kind: "font", accept: ".ttf,.otf,.woff,.woff2,font/*" },
  },
  {
    key: "k_sticker",
    label: "Stickers",
    desc: "Logos and graphics to place on top of images",
    Icon: Sticker,
    match: (a) => a.category === "sticker",
    totalKey: "stickers",
    upload: { asset_kind: "image", category: "sticker", accept: "image/*" },
  },
  {
    key: "k_audio",
    label: "Audio / Music",
    desc: "Music and sound effects, sorted by AI by mood",
    Icon: Music,
    match: (a) => a.asset_kind === "audio",
    totalKey: "audio",
    upload: { asset_kind: "audio", accept: "audio/*" },
  },
];

/** Does this asset belong to this overview card? Same rules the detail view
 *  filters with, so a card's previews always match what opens. */
function inSection(a: ProducerAssetRow, key: string): boolean {
  const kind = KIND_SECTIONS.find((s) => s.key === key);
  return kind ? kind.match(a) : (a.metadata as any)?.content_category === key;
}

export function AssetsPage() {
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const store = useProducerAssetsStore();
  useStudioCanvas();

  // One effect, explicit `force`. Previously these were TWO effects (fetch
  // declared before invalidate) and React runs effects in declaration order, so
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
    <div className="space-y-6 animate-pageEnter">
      {selectedCategory === null && (
        <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-4 pt-2 pb-1">
          <div className="min-w-0 space-y-1.5">
            <p className="text-[13px] font-medium text-fg/55">Assets library</p>
            <h1 className="k-display text-[32px] md:text-[42px] text-fg text-balance">
              <span className="k-code" aria-hidden>&gt; </span>
              Saved assets
            </h1>
            <p className="text-[15px] text-fg/60 max-w-xl">
              Photos, videos, fonts and music you can reuse in new posts. Pick a group to see what is inside.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setUploadOpen(true)}
            className="k-btn shrink-0"
          >
            <Plus className="w-4 h-4" strokeWidth={2} aria-hidden />
            Add asset
          </button>
        </header>
      )}

      {error && (
        <div className="glass px-5 py-4 flex items-start gap-3" role="alert">
          <AlertTriangle className="w-5 h-5 text-k-danger shrink-0 mt-0.5" strokeWidth={1.8} aria-hidden />
          <div className="space-y-0.5">
            <p className="text-[14px] font-semibold text-fg">Couldn't load your assets</p>
            <p className="text-[13px] text-fg/60 break-words">{error}</p>
          </div>
        </div>
      )}

      {selectedCategory === null ? (
        <CategoryOverview
          totals={data?.totals ?? null}
          items={allItems}
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
          onAddAsset={() => setUploadOpen(true)}
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
// Category overview: one card per group, with a peek at what is inside
// ---------------------------------------------------------------------------

function CategoryOverview({
  totals,
  items,
  loading,
  onSelect,
}: {
  totals: ProducerAssetsList["totals"] | null;
  items: ProducerAssetRow[];
  loading: boolean;
  onSelect: (cat: string) => void;
}) {
  const t = totals as any;
  const summary: Array<{ label: string; value: number }> = totals
    ? [
        { label: "images", value: t.images ?? 0 },
        { label: "videos", value: t.videos ?? 0 },
        { label: "characters", value: t.characters ?? 0 },
        { label: "fonts", value: t.fonts ?? 0 },
        { label: "stickers", value: t.stickers ?? 0 },
        { label: "music", value: t.audio ?? 0 },
      ]
    : [];

  return (
    <div className="space-y-8">
      {/* One line that says what is in the library. */}
      {totals && (
        <div className="glass px-5 py-3.5 flex flex-wrap items-center gap-x-6 gap-y-2 animate-reveal">
          <p className="flex items-baseline gap-2">
            <span className="text-[26px] font-semibold tracking-[-0.03em] tabular-nums text-fg leading-none">
              {fmtNumber(t.total ?? 0)}
            </span>
            <span className="text-[13.5px] text-fg/60">assets in total</span>
          </p>
          <span className="hidden sm:block w-px h-6 bg-fg/10" aria-hidden />
          <ul className="flex flex-wrap gap-x-5 gap-y-1 text-[13px] text-fg/60">
            {summary.map((s) => (
              <li key={s.label}>
                <b className="font-semibold text-fg tabular-nums">{fmtNumber(s.value)}</b> {s.label}
              </li>
            ))}
          </ul>
        </div>
      )}

      <section className="space-y-3 animate-reveal" style={{ animationDelay: "80ms" }}>
        <GroupHead title="Images and videos" sub="Things the studio can reuse inside a new poster or video" />
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
          {CATEGORIES.map((cat) => (
            <CategoryCard
              key={cat.key}
              label={cat.label}
              desc={cat.desc}
              Icon={cat.Icon}
              count={totals ? t[`cat_${cat.key}`] ?? 0 : 0}
              loading={loading}
              previews={items.filter((a) => inSection(a, cat.key))}
              onClick={() => onSelect(cat.key)}
            />
          ))}
        </div>
      </section>

      <section className="space-y-3 animate-reveal" style={{ animationDelay: "160ms" }}>
        <GroupHead title="Fonts, stickers and music" sub="The finishing touches: type, logos and sound" />
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
          {KIND_SECTIONS.map((s) => (
            <CategoryCard
              key={s.key}
              label={s.label}
              desc={s.desc}
              Icon={s.Icon}
              count={totals ? t[s.totalKey] ?? 0 : 0}
              loading={loading}
              previews={items.filter(s.match)}
              onClick={() => onSelect(s.key)}
            />
          ))}
        </div>
      </section>
    </div>
  );
}

function GroupHead({ title, sub }: { title: string; sub: string }) {
  return (
    <div>
      <h2 className="text-[17px] font-semibold tracking-[-0.01em] text-fg">{title}</h2>
      <p className="text-[13px] text-fg/55">{sub}</p>
    </div>
  );
}

function CategoryCard({
  label,
  desc,
  Icon,
  count,
  loading,
  previews,
  onClick,
}: {
  label: string;
  desc: string;
  Icon: ComponentType<any>;
  count: number;
  loading: boolean;
  previews: ProducerAssetRow[];
  onClick: () => void;
}) {
  // Only things that have a still to show; video and audio fall back to the icon.
  const stills = previews.filter((a) => a.asset_kind === "image" || a.asset_kind === "font").slice(0, 3);

  return (
    <button
      type="button"
      onClick={onClick}
      className="glass glass-hover text-left p-1.5 flex flex-col group focus:outline-none focus-visible:ring-2 focus-visible:ring-fg/30"
    >
      {/* A peek at what is inside, so people recognise the group by its pictures. */}
      <div className="relative aspect-[16/10] rounded-[2px] overflow-hidden bg-fg/[0.035]">
        {stills.length === 0 ? (
          <div className="absolute inset-0 flex items-center justify-center">
            <span className="icon-well w-12 h-12">
              <Icon className="w-[22px] h-[22px]" strokeWidth={1.5} aria-hidden />
            </span>
          </div>
        ) : stills.length < 3 ? (
          <PreviewTile asset={stills[0]} className="absolute inset-0" />
        ) : (
          <div className="absolute inset-0 grid grid-cols-3 grid-rows-2 gap-[3px]">
            <PreviewTile asset={stills[0]} className="col-span-2 row-span-2" />
            <PreviewTile asset={stills[1]} />
            <PreviewTile asset={stills[2]} />
          </div>
        )}
      </div>

      <div className="px-3 pt-3 pb-2.5 flex flex-col gap-1.5 flex-1">
        <div className="flex items-center justify-between gap-3">
          <span className="flex items-center gap-2 min-w-0">
            <Icon className="w-4 h-4 text-fg/45 shrink-0" strokeWidth={1.6} aria-hidden />
            <span className="text-[15px] font-semibold tracking-[-0.01em] text-fg truncate">{label}</span>
          </span>
          <span className="text-[22px] font-semibold tracking-[-0.03em] tabular-nums text-fg leading-none">
            {loading ? "" : fmtNumber(count)}
          </span>
        </div>
        <p className="text-[13px] text-fg/55 leading-snug">{desc}</p>
        <span className="mt-auto pt-1.5 inline-flex items-center gap-1 text-[13px] font-semibold text-fg/65 group-hover:text-fg transition-colors">
          {count === 0 ? "Open and add" : "Open"}
          <ArrowRight className="w-3.5 h-3.5 transition-transform duration-200 group-hover:translate-x-0.5" strokeWidth={2} aria-hidden />
        </span>
      </div>
    </button>
  );
}

function PreviewTile({ asset, className = "" }: { asset: ProducerAssetRow; className?: string }) {
  const isFont = asset.asset_kind === "font";
  return (
    <div className={`relative overflow-hidden ${isFont ? "bg-k-surface" : "bg-fg/[0.04]"} ${className}`}>
      <Thumb
        src={isFont ? api.producerAssets.previewUrl(asset.id) : api.producerAssets.thumbUrlFor(asset)}
        fallbackSrc={isFont ? null : api.producerAssets.fileUrl(asset.id)}
        className={`w-full h-full transition-transform duration-500 group-hover:scale-[1.03] ${isFont ? "p-2" : ""}`}
        objectFit={isFont ? "contain" : "cover"}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Category detail: grid of assets
// ---------------------------------------------------------------------------

function CategoryDetail({
  category,
  kindSection,
  items,
  loading,
  onBack,
  onOpen,
  onUploaded,
  onAddAsset,
}: {
  category: string;
  kindSection: KindSection | null;
  items: ProducerAssetRow[];
  loading: boolean;
  onBack: () => void;
  onOpen: (id: number) => void;
  onUploaded: () => void;
  onAddAsset: () => void;
}) {
  const cat = CATEGORIES.find((c) => c.key === category);
  const label = kindSection?.label ?? cat?.label ?? category;
  const desc = kindSection?.desc ?? cat?.desc ?? "";
  const Icon = kindSection?.Icon ?? cat?.Icon ?? ImageIcon;
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

  // Fonts, stickers and music upload from right here; the image groups use the
  // "Add asset" dialog, which asks for the group first.
  const uploadHere = () => (kindSection ? fileRef.current?.click() : onAddAsset());
  const uploadLabel = kindSection
    ? analyzing
      ? "Analyzing…"
      : `Upload ${label.toLowerCase()}`
    : "Add asset";

  return (
    <div className="space-y-5">
      <button
        type="button"
        onClick={onBack}
        className="btn glass glass-hover !rounded-none h-9 px-3.5 text-[13.5px] text-fg"
      >
        <ArrowLeft className="w-4 h-4 relative z-[2]" strokeWidth={1.8} aria-hidden />
        <span className="relative z-[2]">All assets</span>
      </button>

      <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-4">
        <div className="flex items-start gap-4 min-w-0">
          <span className="icon-well w-12 h-12 shrink-0">
            <Icon className="w-[22px] h-[22px]" strokeWidth={1.5} aria-hidden />
          </span>
          <div className="min-w-0 space-y-1">
            <h1 className="k-display text-[28px] md:text-[36px] text-fg">
              {label}
            </h1>
            <p className="text-[14px] text-fg/60">
              {desc}
              <span className="text-fg/35"> · </span>
              <span className="font-semibold text-fg tabular-nums">{fmtNumber(filtered.length)}</span>{" "}
              {filtered.length === 1 ? "asset" : "assets"}
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={uploadHere}
          disabled={analyzing}
          className="k-btn shrink-0"
        >
          {!analyzing && <Plus className="w-4 h-4" strokeWidth={2} aria-hidden />}
          {uploadLabel}
        </button>
        {kindSection && (
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
        )}
      </header>

      <label className="glass !rounded-none flex items-center gap-2.5 h-11 px-4 max-w-md focus-within:ring-2 focus-within:ring-fg/20">
        <Search className="w-4 h-4 text-fg/45 shrink-0 relative z-[2]" strokeWidth={1.8} aria-hidden />
        <span className="sr-only">Search {label}</span>
        <input
          id="asset-search"
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search by name or description"
          className="relative z-[2] flex-1 min-w-0 bg-transparent text-[14px] text-fg placeholder:text-fg/40 focus:outline-none"
        />
        {search && (
          <button
            type="button"
            onClick={() => setSearch("")}
            aria-label="Clear search"
            className="relative z-[2] w-6 h-6 rounded-full flex items-center justify-center text-fg/50 hover:text-fg hover:bg-fg/5"
          >
            <X className="w-3.5 h-3.5" strokeWidth={2} aria-hidden />
          </button>
        )}
      </label>

      {loading ? (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-3">
          {Array.from({ length: 10 }).map((_, i) => (
            <div key={i} className="aspect-[4/5] rounded-2xl bg-fg/[0.05] animate-pulse" />
          ))}
        </div>
      ) : filtered.length === 0 ? (
        <div className="glass px-6 py-12 flex flex-col items-center text-center gap-3">
          <span className="icon-well w-12 h-12">
            {search ? (
              <Search className="w-5 h-5" strokeWidth={1.5} aria-hidden />
            ) : (
              <Icon className="w-5 h-5" strokeWidth={1.5} aria-hidden />
            )}
          </span>
          {search ? (
            <div className="space-y-1">
              <p className="text-[15px] font-semibold text-fg">Nothing matches "{search.trim()}"</p>
              <p className="text-[13px] text-fg/60">Try a shorter word, or clear the search.</p>
            </div>
          ) : (
            <div className="space-y-1 max-w-md">
              <p className="text-[15px] font-semibold text-fg">Nothing in {label} yet</p>
              <p className="text-[13px] text-fg/60">
                {kindSection
                  ? `Press "${uploadLabel}" to add your first file.`
                  : 'Save a post from the Instagram tab with "Add to Asset", or press Add asset to upload from your computer.'}
              </p>
            </div>
          )}
          {search ? (
            <button type="button" onClick={() => setSearch("")} className="btn btn-ghost btn-sm !rounded-none bg-k-surface/70">
              Clear search
            </button>
          ) : (
            <button
              type="button"
              onClick={uploadHere}
              disabled={analyzing}
              className="k-btn"
            >
              <Plus className="w-3.5 h-3.5" strokeWidth={2} aria-hidden />
              {uploadLabel}
            </button>
          )}
        </div>
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-3">
          {filtered.map((a, i) => (
            <AssetCard key={a.id} asset={a} delay={Math.min(i, 12) * 30} onClick={() => onOpen(a.id)} />
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Asset card
// ---------------------------------------------------------------------------

function AssetCard({
  asset,
  onClick,
  delay = 0,
}: {
  asset: ProducerAssetRow;
  onClick: () => void;
  delay?: number;
}) {
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
      style={{ animationDelay: `${delay}ms` }}
      className="glass glass-hover text-left p-1.5 overflow-hidden flex flex-col group animate-pageEnter focus:outline-none focus-visible:ring-2 focus-visible:ring-fg/30"
      title={asset.display_name || asset.slug}
    >
      <div className="aspect-square rounded-[2px] bg-fg/[0.04] relative overflow-hidden">
        {isAudio ? (
          <div className="w-full h-full flex flex-col items-center justify-center gap-2 bg-gradient-to-b from-white to-stone-100">
            <span className="icon-well w-12 h-12 !rounded-none">
              <Music className="w-5 h-5" strokeWidth={1.5} aria-hidden />
            </span>
            {asset.duration_sec ? (
              <span className="text-[12px] font-medium tabular-nums text-fg/55">{Math.round(asset.duration_sec)}s</span>
            ) : null}
          </div>
        ) : isVideo ? (
          <div className="w-full h-full relative bg-black">
            {/* `#t=0.1` seeks to the first frame so the browser paints a poster
                from metadata alone: no playback, no backend thumbnail needed. */}
            <video
              src={`${api.producerAssets.fileUrl(asset.id)}#t=0.1`}
              muted
              playsInline
              preload="metadata"
              className="w-full h-full object-cover"
            />
            <span className="absolute inset-0 flex items-center justify-center pointer-events-none">
              <span className="w-10 h-10 rounded-full bg-k-surface/90 backdrop-blur-md text-fg flex items-center justify-center shadow-md">
                <Play className="w-4 h-4 translate-x-[1px]" strokeWidth={2} fill="currentColor" aria-hidden />
              </span>
            </span>
            {asset.duration_sec ? (
              <span className="absolute bottom-2 right-2 text-[11px] font-semibold tabular-nums px-1.5 py-0.5 rounded-md bg-black/60 text-white">
                {Math.round(asset.duration_sec)}s
              </span>
            ) : null}
          </div>
        ) : (
          // thumbUrlFor, not fileUrl: these tiles are ~250px and the originals
          // average 3.1 MB (9.3 MB for backgrounds). The Background category
          // alone was 563 MB per grid paint. The fallback to the full-res proxy
          // keeps the tile working if a transform ever 400s.
          <Thumb
            src={isFont ? api.producerAssets.previewUrl(asset.id) : api.producerAssets.thumbUrlFor(asset)}
            fallbackSrc={isFont ? null : api.producerAssets.fileUrl(asset.id)}
            className={`w-full h-full transition-transform duration-500 group-hover:scale-[1.03] ${isFont ? "bg-k-surface p-2" : ""}`}
            objectFit={isFont ? "contain" : "cover"}
          />
        )}
        {cat && (
          <span className="absolute top-2 left-2 max-w-[calc(100%-1rem)] truncate rounded-full bg-k-surface/90 backdrop-blur-md px-2 py-1 text-[11px] font-semibold text-fg shadow-sm ring-1 ring-black/5">
            {cat.label}
          </span>
        )}
        {mood && (
          <span className="absolute top-2 right-2 rounded-full bg-black/55 backdrop-blur-md px-2 py-1 text-[11px] font-medium text-white capitalize">
            {mood}
          </span>
        )}
      </div>
      <div className="px-2 pt-2.5 pb-1.5 flex-1 flex flex-col gap-1.5">
        <p className="text-[13px] font-medium leading-snug text-fg line-clamp-2">
          {asset.display_name || asset.preview || "No name yet"}
        </p>
        <p className="mt-auto text-[12px] text-fg/50">Added {fmtRel(asset.created_at)}</p>
      </div>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Upload dialog: local file + category picker
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

  // Object URLs for thumbnails: revoke on unmount/change to avoid leaks
  const previewUrls = useMemo(
    () => files.map((f) => URL.createObjectURL(f)),
    [files],
  );
  useEffect(() => {
    return () => {
      for (const u of previewUrls) URL.revokeObjectURL(u);
    };
  }, [previewUrls]);

  // Escape closes, as in every other dialog (not while an upload is running).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !uploading) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [uploading, onClose]);

  const isAiCategory = AI_ANALYZE_CATEGORIES.has(category);
  const canSubmit = files.length > 0 && category !== "" && !uploading;

  function addFiles(picked: FileList | File[] | null | undefined) {
    if (!picked) return;
    const next = Array.from(picked).filter(
      (f) => f.type.startsWith("image/") || f.type.startsWith("video/"),
    );
    if (next.length === 0) return;
    // Deduplicate by name+size: drag-and-drop can fire twice
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
        // one file; for multi-uploads each asset auto-derives its name.
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
          ? ` ${progress.done + 1} of ${progress.total}`
          : "";
      return `${verb}${where}…`;
    }
    if (files.length <= 1) return "Upload";
    return `Upload ${files.length} files`;
  })();

  return createPortal(
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-[rgba(15,16,17,0.45)] backdrop-blur-[6px] animate-fadeIn"
      onClick={() => !uploading && onClose()}
      role="dialog"
      aria-modal="true"
      aria-labelledby="upload-title"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="bg-k-surface rounded-[2px] overflow-hidden flex flex-col animate-scaleIn shadow-[0_0_0_1px_rgba(15,23,42,0.06),0_30px_80px_-20px_rgba(15,23,42,0.45)]"
        style={{ width: "min(600px, 100%)", maxHeight: "min(860px, 92vh)" }}
      >
        <header className="px-6 pt-5 pb-4 flex items-start justify-between gap-4 border-b border-fg/[0.07]">
          <div>
            <h2 id="upload-title" className="text-[19px] font-semibold tracking-[-0.015em] text-fg">Add asset</h2>
            <p className="text-[13px] text-fg/55">Three quick steps. Your files stay in this studio's library.</p>
          </div>
          <button
            onClick={onClose}
            disabled={uploading}
            aria-label="Close"
            className="w-9 h-9 shrink-0 rounded-full flex items-center justify-center text-fg/60 hover:text-fg hover:bg-fg/[0.06] disabled:opacity-40 transition-colors"
          >
            <X className="w-4 h-4" strokeWidth={2} aria-hidden />
          </button>
        </header>

        <div className="px-6 py-5 space-y-6 overflow-y-auto">
          {/* Step 1: category (required) */}
          <section className="space-y-3">
            <StepLabel n={1} title="What kind of asset is it?" done={category !== ""} />
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {CATEGORIES.map((cat) => {
                const selected = category === cat.key;
                return (
                  <button
                    key={cat.key}
                    type="button"
                    onClick={() => setCategory(cat.key)}
                    disabled={uploading}
                    aria-pressed={selected}
                    className={[
                      "relative rounded-xl p-3 text-left flex items-start gap-3 transition-all duration-150",
                      "focus:outline-none focus-visible:ring-2 focus-visible:ring-fg/30",
                      selected
                        ? "bg-gold-soft/60 shadow-[0_0_0_1.5px_rgb(var(--k-btn))]"
                        : "bg-k-inset shadow-[0_0_0_1px_rgba(15,23,42,0.07)] hover:shadow-[0_0_0_1px_rgba(15,23,42,0.18)]",
                      uploading ? "opacity-60 cursor-not-allowed" : "",
                    ].join(" ")}
                  >
                    <span className="icon-well w-8 h-8 !rounded-lg">
                      <cat.Icon className="w-4 h-4" strokeWidth={1.6} aria-hidden />
                    </span>
                    <span className="min-w-0 flex-1 pr-5">
                      <span className="block text-[14px] font-semibold text-fg">{cat.label}</span>
                      <span className="block text-[12px] text-fg/55 leading-snug mt-0.5">{cat.desc}</span>
                    </span>
                    {selected && (
                      <span className="absolute top-2.5 right-2.5 w-5 h-5 rounded-full bg-k-cyan text-k-oncyan flex items-center justify-center">
                        <Check className="w-3 h-3" strokeWidth={3} aria-hidden />
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
            {isAiCategory && category && (
              <p className="text-[12.5px] text-fg/60 leading-relaxed rounded-lg bg-k-inset px-3 py-2 ring-1 ring-fg/[0.06]">
                The AI studies each background (colours, light, place, mood) so it can find it later.
                This takes about 10 to 20 seconds per file.
              </p>
            )}
          </section>

          {/* Step 2: files (drop zone: multi-file picker + drag-and-drop) */}
          <section className="space-y-3">
            <StepLabel n={2} title="Choose the files" done={files.length > 0} />
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
              role="button"
              tabIndex={uploading ? -1 : 0}
              onKeyDown={(e) => {
                if ((e.key === "Enter" || e.key === " ") && !uploading) {
                  e.preventDefault();
                  fileRef.current?.click();
                }
              }}
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
                "w-full rounded-2xl border-[1.5px] border-dashed px-6 py-7 flex flex-col items-center gap-2 text-center transition-colors cursor-pointer",
                "focus:outline-none focus-visible:ring-2 focus-visible:ring-fg/30",
                dragOver ? "border-gold bg-gold-soft/50" : "border-fg/15 bg-k-inset hover:border-fg/30",
                uploading ? "opacity-60 cursor-not-allowed" : "",
              ].join(" ")}
            >
              <span className="icon-well w-11 h-11 !rounded-none">
                <CloudUpload className="w-5 h-5" strokeWidth={1.5} aria-hidden />
              </span>
              {files.length === 0 ? (
                <>
                  <p className="text-[14px] font-semibold text-fg">
                    {dragOver ? "Drop the files here" : "Click to choose files, or drag them here"}
                  </p>
                  <p className="text-[12.5px] text-fg/55">Images or videos (MP4, MOV, WebM). You can pick several at once.</p>
                </>
              ) : (
                <>
                  <p className="text-[14px] font-semibold text-fg">
                    {files.length} {files.length === 1 ? "file" : "files"} ready
                  </p>
                  <p className="text-[12.5px] text-fg/55">Click to add more</p>
                </>
              )}
            </div>

            {/* Thumbnails: only when files picked */}
            {files.length > 0 && (
              <div className="grid grid-cols-3 sm:grid-cols-4 gap-2">
                {files.map((f, i) => (
                  <div
                    key={`${f.name}|${f.size}|${i}`}
                    className="relative aspect-square rounded-xl overflow-hidden bg-k-inset ring-1 ring-fg/[0.06] group"
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
                    <span className="absolute bottom-0 inset-x-0 px-2 py-1 text-[10.5px] text-white bg-gradient-to-t from-black/70 to-transparent truncate">
                      {f.name}
                    </span>
                    {!uploading && (
                      <button
                        type="button"
                        onClick={() => removeFile(i)}
                        aria-label={`Remove ${f.name}`}
                        className="absolute top-1.5 right-1.5 w-6 h-6 rounded-full bg-k-surface/90 text-fg shadow flex items-center justify-center opacity-0 group-hover:opacity-100 focus:opacity-100 hover:bg-k-danger hover:text-white transition"
                      >
                        <X className="w-3.5 h-3.5" strokeWidth={2.2} aria-hidden />
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* Step 3: display name (single file only) */}
          {files.length <= 1 && (
            <section className="space-y-3">
              <StepLabel n={3} title="Give it a name" optional done={displayName.trim() !== ""} />
              <input
                id="upload-display-name"
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                disabled={uploading}
                placeholder="For example: Summer campus, main hall"
                className="w-full rounded-xl bg-k-inset ring-1 ring-fg/10 px-3.5 h-11 text-[14px] text-fg placeholder:text-fg/40 focus:outline-none focus:ring-2 focus:ring-fg/25 disabled:opacity-60"
              />
            </section>
          )}

          {/* Progress bar: shown while uploading */}
          {uploading && progress && progress.total > 1 && (
            <div className="space-y-1.5" aria-live="polite">
              <div className="h-2 bg-fg/[0.07] rounded-full overflow-hidden">
                <div
                  className="h-full bg-gold rounded-full transition-all duration-300"
                  style={{
                    width: `${(progress.done / progress.total) * 100}%`,
                  }}
                />
              </div>
              <p className="text-[12.5px] text-fg/60 tabular-nums">
                {progress.done} of {progress.total} done
                {progress.current ? ` · ${progress.current}` : ""}
              </p>
            </div>
          )}

          {/* Per-file errors on partial failure */}
          {errors.length > 0 && (
            <div className="rounded-xl bg-k-danger/10 ring-1 ring-k-danger/30 px-4 py-3 space-y-1.5" role="alert">
              <p className="text-[13px] font-semibold text-k-danger">
                {errors.length === 1 ? "1 file didn't upload" : `${errors.length} files didn't upload`}
              </p>
              <ul className="space-y-1 text-[12.5px] text-k-danger/90">
                {errors.map((e, i) => (
                  <li key={i} className="break-words">
                    <span className="font-medium">{e.filename}</span>: {e.reason}
                  </li>
                ))}
              </ul>
              <p className="text-[12.5px] text-k-danger/80">Press Upload again to retry only these files.</p>
            </div>
          )}
        </div>

        <footer className="px-6 py-4 border-t border-fg/[0.07] flex items-center justify-between gap-3 bg-k-inset">
          <p className="text-[12.5px] text-fg/50 hidden sm:block">
            {!category ? "Pick a kind first" : files.length === 0 ? "Then choose your files" : "Ready to upload"}
          </p>
          <div className="flex items-center gap-2 ml-auto">
            <button
              onClick={onClose}
              disabled={uploading}
              className="k-btn-ghost"
            >
              Cancel
            </button>
            <button
              onClick={handleUpload}
              disabled={!canSubmit}
              className="k-btn"
            >
              {buttonLabel}
            </button>
          </div>
        </footer>
      </div>
    </div>,
    document.body,
  );
}

function StepLabel({
  n,
  title,
  optional = false,
  done = false,
}: {
  n: number;
  title: string;
  optional?: boolean;
  done?: boolean;
}) {
  return (
    <p className="flex items-center gap-2.5">
      <span
        className={[
          "w-6 h-6 rounded-full flex items-center justify-center text-[12px] font-semibold tabular-nums transition-colors",
          done ? "bg-k-cyan text-k-oncyan" : "bg-fg/[0.07] text-fg/70",
        ].join(" ")}
      >
        {done ? <Check className="w-3.5 h-3.5" strokeWidth={3} aria-hidden /> : n}
      </span>
      <span className="text-[14.5px] font-semibold text-fg">{title}</span>
      {optional && <span className="text-[12.5px] text-fg/45">optional</span>}
    </p>
  );
}
