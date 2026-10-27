import { useEffect, useMemo, useRef, useState } from "react";

import { useModalChrome } from "../../lib/useModalChrome";
import { createPortal } from "react-dom";

import { Skeleton } from "../Skeleton";
import { useToast } from "../ToastProvider";
import { api } from "../../lib/api";
import { useProducerAssetsStore } from "../../lib/store";
import { fmtNumber, fmtRel } from "../../lib/format";
import type {
  FrameExtractionMode,
  InstagramPostDetail,
  InstagramScene,
  InstagramVideoAnalysis,
} from "../../lib/types";
import { FrameExtractionDialog } from "./FrameExtractionDialog";

interface Props {
  postId: number;
  onClose: () => void;
}

export function PostDetailDrawer({ postId, onClose }: Props) {
  const [data, setData] = useState<InstagramPostDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeIdx, setActiveIdx] = useState(0);
  const [movingCategory, setMovingCategory] = useState(false);
  const [addingAsset, setAddingAsset] = useState(false);
  const [addedAssetIds, setAddedAssetIds] = useState<Set<number>>(new Set());
  // For video-category posts we open a frame-extraction modal instead of
  // importing the MP4 directly. State carries the (assetId, mode) of the
  // currently-targeted carousel child.
  const [frameDialog, setFrameDialog] = useState<{
    assetId: number;
    mode: FrameExtractionMode;
  } | null>(null);
  const toast = useToast();
  const assetsStore = useProducerAssetsStore();

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setActiveIdx(0);
    api.instagramIngest
      .getPost(postId)
      .then((r) => !cancelled && setData(r))
      .catch((e) => !cancelled && setError(String(e?.message || e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [postId]);

  // ESC closes + body scroll lock
  // Escape, scroll lock, focus trap and focus restore in one place. The first
  // two were already here; the trap is the new part -- without it Tab walked
  // straight out of the dialog and into the page behind the overlay, which
  // makes a dialog modal only to a mouse.
  const panelRef = useRef<HTMLDivElement>(null);
  const { trapTab } = useModalChrome({ onClose, panelRef });

  const isCarousel = (data?.assets.length ?? 0) > 1;
  const activeAsset = data?.assets[activeIdx] ?? data?.assets[0];

  const typeBadge = useMemo(() => {
    if (!data) return "";
    if (data.media_type === "VIDEO") return "REEL";
    if (data.media_type === "CAROUSEL_ALBUM")
      return `CAROUSEL · ${data.children_count || data.assets.length}`;
    return "IMAGE";
  }, [data]);

  // Portal: mount on document.body so `position: fixed` is relative to the
  // viewport, not the page-transition wrapper (which sets `transform` and
  // creates a containing block that traps fixed children).
  return createPortal(
    <div
      // Inline styles for the outer wrapper so nothing in the project's
      // Tailwind config (or a parent with a transform) can override the
      // viewport positioning. This is a popup; treat it as a top-level layer.
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
        style={{
          width: "min(560px, 100%)",
          maxHeight: "min(860px, 92vh)",
        }}
      >
        {/* Header */}
        <header className="shrink-0 px-4 py-3 border-b border-border flex items-center gap-2.5 flex-wrap bg-bg">
          <span className="font-mono text-3xs px-1.5 py-0.5 rounded bg-fg text-bg uppercase tracking-wider2">
            {typeBadge || "POST"}
          </span>
          <span className="text-sm font-semibold text-fg truncate">
            @{data?.account_username || "—"}
          </span>
          {data?.timestamp_posted && (
            <span className="text-xs text-muted">{fmtRel(data.timestamp_posted)}</span>
          )}
          {data?.permalink && (
            <a
              href={data.permalink}
              target="_blank"
              rel="noreferrer"
              className="ml-auto text-xs text-muted hover:text-accent"
            >
              open on instagram ↗
            </a>
          )}
          <button
            onClick={onClose}
            aria-label="Close"
            className="w-7 h-7 rounded-full bg-bg-muted hover:bg-border text-fg flex items-center justify-center text-base leading-none transition-colors"
          >
            ×
          </button>
        </header>

        {/* Body — single column, scrollable */}
        <div className="flex-1 min-h-0 overflow-y-auto">
          {loading && (
            <div className="p-4 space-y-3">
              <Skeleton h="h-72" rounded="rounded-lg" />
              <Skeleton h="h-20" rounded="rounded-lg" />
              <Skeleton h="h-32" rounded="rounded-lg" />
            </div>
          )}
          {error && (
            <p className="p-4 text-sm text-danger" role="alert">
              {error}
            </p>
          )}
          {data && (
            <>
              {/* Media — height-capped at 50vh so it never pushes
                  the caption / metadata off-screen. */}
              {activeAsset ? (
                <div className="bg-black flex items-center justify-center" style={{ maxHeight: "50vh" }}>
                  <MediaTile postId={data.id} asset={activeAsset} />
                </div>
              ) : (
                <div className="p-4 text-sm text-muted italic">
                  No media downloaded for this post yet.
                </div>
              )}

              {/* Carousel strip */}
              {isCarousel && (
                <div className="flex gap-2 overflow-x-auto px-3 py-2 bg-bg-muted border-b border-border">
                  {data.assets.map((a, i) => (
                    <button
                      key={a.id}
                      onClick={() => setActiveIdx(i)}
                      className={[
                        "shrink-0 w-12 h-12 rounded overflow-hidden border-2 relative transition",
                        i === activeIdx
                          ? "border-accent"
                          : "border-transparent opacity-60 hover:opacity-100",
                      ].join(" ")}
                      aria-label={`Show item ${i + 1}`}
                    >
                      <Thumb postId={data.id} asset={a} />
                      {a.asset_type === "instagram" && (
                        <span className="absolute inset-0 flex items-center justify-center bg-black/30 text-white text-3xs">
                          ▶
                        </span>
                      )}
                    </button>
                  ))}
                </div>
              )}

              {/* Info */}
              <div className="p-4 space-y-4">
                {/* Engagement */}
                <dl className="grid grid-cols-3 gap-2 text-sm">
                  <Stat icon="♥" label="Likes" value={fmtNumber(data.like_count ?? 0)} />
                  <Stat icon="💬" label="Comments" value={fmtNumber(data.comments_count ?? 0)} />
                  <Stat
                    icon="▶"
                    label="Views"
                    value={data.view_count != null ? fmtNumber(data.view_count) : "—"}
                  />
                </dl>

                {/* Actions */}
                <div className="flex items-center gap-2 flex-wrap">
                  {/* Move category */}
                  <div className="flex items-center gap-1.5">
                    <label className="text-3xs font-mono text-muted uppercase tracking-wider2">Category</label>
                    <select
                      value={data.content_category || ""}
                      disabled={movingCategory}
                      onChange={async (e) => {
                        const newCat = e.target.value;
                        if (!newCat) return;
                        setMovingCategory(true);
                        try {
                          await api.instagramIngest.updateCategory(data.id, newCat);
                          setData({ ...data, content_category: newCat as any });
                          toast.info(`Moved to ${newCat}`, "Category updated");
                        } catch (err: any) {
                          toast.error("Failed to update", err?.message || String(err));
                        } finally {
                          setMovingCategory(false);
                        }
                      }}
                      className="rounded border border-border bg-bg-subtle text-xs px-2 py-1"
                    >
                      <option value="">Unclassified</option>
                      <option value="img_poster">Poster</option>
                      <option value="img_poster_human">Poster + Human</option>
                      <option value="img_poster_animated">Animated</option>
                      <option value="video_interview">Interview</option>
                      <option value="other">Other</option>
                    </select>
                  </div>

                  {/* Add to Asset — adds the currently visible carousel item */}
                  {activeAsset && (() => {
                    const thisAssetId = activeAsset.asset_id;
                    const cat = data.content_category;
                    // Video categories route to the frame-extraction modal;
                    // static categories use the direct-import path.
                    const isVideoCategory =
                      cat === "video_interview" || cat === "img_poster_animated";
                    // For VIDEO posts we don't lock the button — we count how
                    // many frames have already been pulled from this video and
                    // show "+ Extract (N added)" so the user can keep adding.
                    // `derived_from_video_asset_id` is set in the from-frame
                    // payload (producer_assets.py::create_from_frame).
                    const extractedCount = isVideoCategory
                      ? assetsStore.items.filter(
                          (a) =>
                            (a.metadata as any)?.derived_from_video_asset_id ===
                            thisAssetId,
                        ).length
                      : 0;
                    // Static-image lock — unchanged.
                    const isAddedStatic =
                      !isVideoCategory && addedAssetIds.has(thisAssetId);
                    const buttonLabel = (() => {
                      if (isAddedStatic) return "✓ Added";
                      if (addingAsset) return "Saving…";
                      if (isVideoCategory) {
                        const base =
                          cat === "video_interview"
                            ? "+ Extract characters"
                            : "+ Extract poster frame";
                        return extractedCount > 0
                          ? `${base} (${extractedCount} added)`
                          : base;
                      }
                      return isCarousel ? `+ Add item ${activeIdx + 1}` : "+ Add to Asset";
                    })();
                    return (
                      <button
                        className={`btn text-xs ml-auto ${isAddedStatic ? "btn-ghost text-success" : "btn-primary"}`}
                        disabled={addingAsset || isAddedStatic || !cat}
                        title={!cat ? "Classify this post first" : isCarousel ? `Add item ${activeIdx + 1} to assets` : undefined}
                        onClick={async () => {
                          // Video categories: open the extraction modal
                          if (isVideoCategory) {
                            const mode: FrameExtractionMode =
                              cat === "video_interview" ? "talking_head" : "animated_poster";
                            setFrameDialog({ assetId: thisAssetId, mode });
                            return;
                          }
                          // Static categories: direct import (existing path)
                          setAddingAsset(true);
                          try {
                            const newAsset = await api.producerAssets.createFromPost(data.id, thisAssetId);
                            assetsStore.addItem(newAsset as any);
                            setAddedAssetIds((s) => new Set(s).add(thisAssetId));
                            toast.info(
                              isCarousel ? `Item ${activeIdx + 1} saved to assets` : "Saved to assets library",
                              "Asset added",
                            );
                          } catch (err: any) {
                            const msg = err?.message || String(err);
                            if (msg.includes("409") || msg.toLowerCase().includes("already")) {
                              setAddedAssetIds((s) => new Set(s).add(thisAssetId));
                              toast.info("Already in assets", "Asset");
                            } else {
                              toast.error("Failed to add", msg);
                            }
                          } finally {
                            setAddingAsset(false);
                          }
                        }}
                      >
                        {buttonLabel}
                      </button>
                    );
                  })()}
                </div>

                {/* Caption */}
                <section>
                  <p className="eyebrow mb-1.5">Caption</p>
                  {data.caption ? (
                    <p className="text-sm text-fg whitespace-pre-wrap leading-relaxed">
                      {data.caption}
                    </p>
                  ) : (
                    <p className="text-sm text-muted italic">(no caption)</p>
                  )}
                </section>

                {/* v2 — Structural analysis (only present for deepened videos) */}
                {data.video_analysis && <StructuralAnalysis a={data.video_analysis} />}

                {/* v2 — Scene-by-scene breakdown */}
                {data.scenes && data.scenes.length > 0 && (
                  <SceneBreakdown postId={data.id} scenes={data.scenes} />
                )}

                {/* Per-active-item AI description (only when no deepened analysis) */}
                {!data.video_analysis &&
                  activeAsset && (activeAsset.ai_description || activeAsset.video_summary) && (
                  <section>
                    <p className="eyebrow mb-1.5">
                      {activeAsset.asset_type === "instagram"
                        ? `Video understanding${isCarousel ? ` · item ${activeIdx + 1}` : ""}`
                        : `Image understanding${isCarousel ? ` · item ${activeIdx + 1}` : ""}`}
                    </p>
                    <p className="text-sm text-fg/90 leading-relaxed">
                      {activeAsset.video_summary || activeAsset.ai_description}
                    </p>
                  </section>
                )}

                {/* Transcript */}
                {data.transcript.length > 0 && (
                  <section>
                    <p className="eyebrow mb-1.5">Transcript</p>
                    <ul className="space-y-1 font-mono text-xs bg-bg-muted rounded-lg p-3 max-h-48 overflow-y-auto">
                      {data.transcript.map((seg, i) => (
                        <li key={i} className="flex gap-3">
                          <span className="text-muted tabular-nums w-12 shrink-0">
                            {fmtT(seg.start_sec)}
                          </span>
                          <span className="text-fg/90">{seg.text}</span>
                        </li>
                      ))}
                    </ul>
                  </section>
                )}

                {/* Composed retrieval text (collapsed). For deepened posts this
                    is the long-form structural summary used by the Producer's
                    embedding retrieval. */}
                {data.ai_summary && (
                  <details className="text-xs">
                    <summary className="cursor-pointer text-muted hover:text-fg font-mono uppercase tracking-wider2 text-3xs">
                      Composed retrieval text {data.video_deepened_at ? "(deepened)" : "(basic)"}
                    </summary>
                    <pre className="mt-2 text-xs text-fg/80 whitespace-pre-wrap bg-bg-muted rounded p-3 font-mono leading-relaxed">
                      {data.ai_summary}
                    </pre>
                  </details>
                )}

                {/* Metrics history */}
                {data.metrics_history.length > 1 && (
                  <section>
                    <p className="eyebrow mb-1.5">Engagement history</p>
                    <div className="overflow-x-auto">
                    <table className="w-full min-w-[420px] text-xs font-mono">
                      <thead>
                        <tr className="text-left text-muted border-b border-border">
                          <th className="py-1.5 font-medium">Captured</th>
                          <th className="py-1.5 font-medium text-right">♥</th>
                          <th className="py-1.5 font-medium text-right">💬</th>
                          <th className="py-1.5 font-medium text-right">▶</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.metrics_history.map((s, i) => (
                          <tr key={i} className="border-t border-border/40">
                            <td className="py-1 text-muted">{fmtRel(s.captured_at)}</td>
                            <td className="py-1 text-right tabular-nums">
                              {fmtNumber(s.like_count ?? 0)}
                            </td>
                            <td className="py-1 text-right tabular-nums">
                              {fmtNumber(s.comments_count ?? 0)}
                            </td>
                            <td className="py-1 text-right tabular-nums">
                              {s.view_count != null ? fmtNumber(s.view_count) : "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    </div>
                  </section>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {/* Frame extraction modal — only mounts when user clicks "+ Extract …"
          on a video-category post. Itself portaled to body. */}
      {frameDialog && data && (
        <FrameExtractionDialog
          postId={data.id}
          assetId={frameDialog.assetId}
          initialMode={frameDialog.mode}
          onClose={() => setFrameDialog(null)}
          onImported={(_count) => {
            // Refresh the assets store so the Assets page reflects the new
            // rows AND the per-video extracted counter on the Extract button.
            assetsStore.invalidate();
            assetsStore.fetch({ limit: 500 });
            // Note: video-category buttons no longer lock — the counter badge
            // ("+ Extract (N added)") signals progress while leaving the
            // button clickable so the user can keep saving more references.
          }}
        />
      )}
    </div>,
    document.body,
  );
}

function MediaTile({
  postId,
  asset,
}: {
  postId: number;
  asset: InstagramPostDetail["assets"][number];
}) {
  const url = api.instagramIngest.assetFileUrl(postId, asset.asset_id);
  if (asset.asset_type === "image") {
    return (
      <img
        src={url}
        alt=""
        style={{ maxWidth: "100%", maxHeight: "50vh", display: "block" }}
        loading="lazy"
      />
    );
  }
  return (
    <video
      src={url}
      controls
      autoPlay
      muted
      playsInline
      preload="metadata"
      style={{ maxWidth: "100%", maxHeight: "50vh", display: "block" }}
    />
  );
}

function Thumb({
  postId,
  asset,
}: {
  postId: number;
  asset: InstagramPostDetail["assets"][number];
}) {
  const url = api.instagramIngest.assetFileUrl(postId, asset.asset_id);
  if (asset.asset_type === "image") {
    return <img src={url} alt="" className="w-full h-full object-cover" />;
  }
  return (
    <video
      src={url}
      className="w-full h-full object-cover"
      preload="metadata"
      muted
      playsInline
      onLoadedMetadata={(e) => {
        const v = e.currentTarget;
        if (v.duration > 0.1) v.currentTime = 0.1;
      }}
    />
  );
}

function StructuralAnalysis({ a }: { a: InstagramVideoAnalysis }) {
  const audio = a.audio_profile || {};
  const brand = a.brand_presence || {};
  const audioBits: string[] = [];
  if (audio.speech_language) audioBits.push(`speech=${audio.speech_language}`);
  if (audio.music_present) audioBits.push("music");
  if (audio.energy) audioBits.push(`energy=${audio.energy}`);

  return (
    <section className="space-y-2 rounded-xl border border-accent/30 bg-accent/5 p-3.5">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <p className="eyebrow text-accent">Structural analysis</p>
        {a.pacing && (
          <span className="font-mono text-3xs px-1.5 py-0.5 rounded bg-accent/15 text-accent uppercase tracking-wider2">
            {a.pacing}
          </span>
        )}
      </div>

      <Row label="Hook" value={a.hook_description} pill={a.hook_type} />
      <Row label="Narrative" value={a.narrative_arc} />
      <Row label="CTA" value={a.cta} />
      {audioBits.length > 0 && <Row label="Audio" value={audioBits.join(" · ")} />}

      {(a.on_screen_text || []).length > 0 && (
        <div>
          <p className="font-mono text-3xs text-muted uppercase tracking-wider2 mb-1">
            On-screen text
          </p>
          <ul className="flex flex-wrap gap-1.5">
            {a.on_screen_text!.slice(0, 8).map((t, i) => (
              <li
                key={i}
                className="font-mono text-2xs px-1.5 py-0.5 rounded bg-bg-muted text-fg/80"
              >
                {t}
              </li>
            ))}
          </ul>
        </div>
      )}

      {(brand.logo_visible ||
        (brand.palette_called_out && brand.palette_called_out.length > 0) ||
        (brand.locations && brand.locations.length > 0) ||
        (brand.people_described && brand.people_described.length > 0)) && (
        <div className="space-y-1">
          <p className="font-mono text-3xs text-muted uppercase tracking-wider2">
            Brand presence
          </p>
          {brand.logo_visible && (
            <p className="text-xs text-fg/85">
              Logo: <span className="text-fg">{brand.logo_treatment || "visible"}</span>
            </p>
          )}
          {brand.palette_called_out && brand.palette_called_out.length > 0 && (
            <p className="text-xs text-fg/85">
              Palette:{" "}
              <span className="text-fg">{brand.palette_called_out.join(", ")}</span>
            </p>
          )}
          {brand.locations && brand.locations.length > 0 && (
            <p className="text-xs text-fg/85">
              Locations: <span className="text-fg">{brand.locations.join(", ")}</span>
            </p>
          )}
          {brand.people_described && brand.people_described.length > 0 && (
            <p className="text-xs text-fg/85">
              People: <span className="text-fg">{brand.people_described.join(" · ")}</span>
            </p>
          )}
        </div>
      )}

      {a.why_this_likely_worked && (
        <div className="pt-1 border-t border-accent/20">
          <p className="font-mono text-3xs text-muted uppercase tracking-wider2 mb-1">
            Why this likely worked
          </p>
          <p className="text-xs text-fg/85 leading-relaxed">{a.why_this_likely_worked}</p>
        </div>
      )}

      {a.transferable_pattern && (
        <div>
          <p className="font-mono text-3xs text-accent uppercase tracking-wider2 mb-1">
            Transferable pattern (producer signal)
          </p>
          <p className="text-xs text-fg leading-relaxed font-medium">
            {a.transferable_pattern}
          </p>
        </div>
      )}
    </section>
  );
}

function Row({
  label,
  value,
  pill,
}: {
  label: string;
  value?: string | null;
  pill?: string | null;
}) {
  if (!value) return null;
  return (
    <div className="flex gap-2 text-xs">
      <span className="font-mono text-3xs text-muted uppercase tracking-wider2 w-20 shrink-0 pt-0.5">
        {label}
      </span>
      <span className="flex-1 text-fg/85 leading-relaxed">
        {pill && (
          <span className="font-mono text-3xs px-1.5 py-0.5 rounded bg-bg-muted text-muted mr-1.5">
            {pill}
          </span>
        )}
        {value}
      </span>
    </div>
  );
}

function SceneBreakdown({
  postId,
  scenes,
}: {
  postId: number;
  scenes: InstagramScene[];
}) {
  return (
    <section>
      <p className="eyebrow mb-1.5">Scene-by-scene</p>
      <ol className="space-y-2">
        {scenes.map((sc) => (
          <li
            key={sc.id}
            className="flex gap-3 rounded-lg border border-border bg-bg-muted/50 p-2"
          >
            <div className="shrink-0 w-20 h-20 rounded overflow-hidden bg-black flex items-center justify-center">
              {sc.representative_frame_id != null ? (
                <img
                  src={api.instagramIngest.frameFileUrl(postId, sc.representative_frame_id)}
                  alt=""
                  className="w-full h-full object-cover"
                  loading="lazy"
                />
              ) : (
                <span className="text-white/40 text-xs">—</span>
              )}
            </div>
            <div className="flex-1 min-w-0 space-y-1">
              <p className="font-mono text-3xs text-muted tabular-nums">
                {fmtT(sc.start_sec)}–{fmtT(sc.end_sec)} · scene {sc.scene_index + 1}
              </p>
              <p className="text-xs text-fg/90 leading-relaxed">{sc.description}</p>
              {sc.transcript_excerpt && (
                <p className="text-2xs text-muted italic leading-relaxed">
                  “{sc.transcript_excerpt}”
                </p>
              )}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

function Stat({ icon, label, value }: { icon: string; label: string; value: string }) {
  return (
    <div className="rounded-lg bg-bg-muted px-3 py-2">
      <dt className="font-mono text-3xs uppercase tracking-wider2 text-muted flex items-center gap-1">
        <span aria-hidden>{icon}</span>
        {label}
      </dt>
      <dd className="font-semibold text-fg tabular-nums text-base mt-0.5">{value}</dd>
    </div>
  );
}

function fmtT(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, "0")}`;
}
