import { useEffect, useRef, useState } from "react";
import { Music, Pause, Pencil, Play, Trash2, X } from "lucide-react";

import { useModalChrome } from "../../lib/useModalChrome";
import { createPortal } from "react-dom";

import { Skeleton } from "../Skeleton";
import { useConfirm } from "../ConfirmProvider";
import { useToast } from "../ToastProvider";
import { api } from "../../lib/api";
import { fmtBytes, fmtNumber, fmtRel } from "../../lib/format";
import type { ProducerAssetDetail } from "../../lib/types";

interface Props {
  assetId: number;
  onClose: () => void;
  onChanged: () => void;
}

export function AssetDetailDrawer({ assetId, onClose, onChanged }: Props) {
  const [data, setData] = useState<ProducerAssetDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draftName, setDraftName] = useState("");
  const [draftDesc, setDraftDesc] = useState("");
  const [showRaw, setShowRaw] = useState(false);
  const toast = useToast();
  const confirm = useConfirm();

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.producerAssets
      .get(assetId)
      .then((r) => {
        if (cancelled) return;
        setData(r);
        setDraftName(r.display_name || "");
        setDraftDesc(r.ai_description || "");
      })
      .catch((e) => !cancelled && setError(String(e?.message || e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [assetId]);

  // Escape, scroll lock, focus trap and focus restore in one place. The first
  // two were already here; the trap is the new part -- without it Tab walked
  // straight out of the dialog and into the page behind the overlay, which
  // makes a dialog modal only to a mouse.
  const panelRef = useRef<HTMLDivElement>(null);
  const { trapTab } = useModalChrome({ onClose, panelRef });

  async function toggleEnabled() {
    if (!data) return;
    try {
      await api.producerAssets.patch(data.id, { enabled: !data.enabled });
      setData({ ...data, enabled: !data.enabled });
      onChanged();
    } catch (e: any) {
      toast.error("Toggle failed", e?.message || String(e));
    }
  }

  async function saveEdits() {
    if (!data) return;
    try {
      await api.producerAssets.patch(data.id, {
        display_name: draftName || undefined,
        ai_description: draftDesc || undefined,
      });
      setData({ ...data, display_name: draftName || null, ai_description: draftDesc });
      setEditing(false);
      onChanged();
      toast.info("Saved", "Asset updated");
    } catch (e: any) {
      toast.error("Save failed", e?.message || String(e));
    }
  }

  async function deleteAsset() {
    if (!data) return;
    if (
      !(await confirm({
        title: "Delete asset?",
        message: `"${data.display_name || data.slug}" will be permanently deleted. This can’t be undone.`,
        confirmLabel: "Delete",
        danger: true,
      }))
    )
      return;
    try {
      await api.producerAssets.delete(data.id);
      toast.info("Deleted", data.display_name || data.slug);
      onChanged();
      onClose();
    } catch (e: any) {
      toast.error("Delete failed", e?.message || String(e));
    }
  }

  const fileUrl = api.producerAssets.fileUrl(assetId);
  // Prefer the server-minted mid-size transform (~150 kB) over the original
  // (9-15 MB). Falls through to the original when there is no transform — fonts,
  // video, SVG and local-disk rows.
  const previewUrl =
    data?.asset_kind === "font"
      ? api.producerAssets.previewUrl(assetId)
      : data?.preview_url || fileUrl;

  return createPortal(
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-[rgba(15,16,17,0.45)] backdrop-blur-[6px] animate-fadeIn"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        onKeyDown={trapTab}
        onClick={(e) => e.stopPropagation()}
        className="relative bg-k-surface rounded-[2px] overflow-hidden flex flex-col animate-scaleIn outline-none shadow-[0_0_0_1px_rgba(15,23,42,0.06),0_30px_80px_-20px_rgba(15,23,42,0.45)]"
        style={{ width: "min(820px, 100%)", maxHeight: "92vh" }}
      >
        {/* Header */}
        <header className="shrink-0 px-5 py-3.5 border-b border-fg/[0.07] flex items-center gap-3 flex-wrap bg-k-surface">
          <div className="flex items-center gap-2 flex-wrap min-w-0 flex-1">
            <span
              className={[
                "inline-flex items-center gap-1.5 text-[12px] font-semibold px-2.5 py-1 rounded-full ring-1",
                data?.enabled
                  ? "bg-gold-soft text-gold-ink ring-gold/30"
                  : "bg-fg/[0.05] text-fg/60 ring-fg/10",
              ].join(" ")}
            >
              <span
                aria-hidden
                className={[
                  "w-1.5 h-1.5 rounded-full",
                  data?.enabled ? "bg-gold" : "bg-fg/30",
                ].join(" ")}
              />
              {/* "Active" = the AI may pick it for new posts; "Paused" = it will not. */}
              {data?.enabled ? "Active" : "Paused"}
            </span>
            <span className="text-[12px] font-medium px-2.5 py-1 rounded-full bg-fg/[0.05] text-fg/65 ring-1 ring-fg/10 capitalize">
              {data?.asset_kind ?? "?"}
            </span>
            {data?.archetype && (
              <span className="text-[12px] font-medium px-2.5 py-1 rounded-full bg-fg/[0.05] text-fg/65 ring-1 ring-fg/10 capitalize">
                {data.archetype.replace("_", " ")}
              </span>
            )}
            <h2 className="text-[15px] font-semibold text-fg truncate min-w-0">
              {data?.display_name || data?.slug || "asset"}
            </h2>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="w-9 h-9 rounded-full text-fg/60 hover:text-fg hover:bg-fg/[0.06] flex items-center justify-center shrink-0 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-fg/30"
          >
            <X className="w-4 h-4" strokeWidth={2} aria-hidden />
          </button>
        </header>

        {/* Body */}
        <div className="flex-1 min-h-0 overflow-y-auto">
          {loading && (
            <div className="p-5 space-y-3">
              <Skeleton h="h-64" rounded="rounded-xl" />
              <Skeleton h="h-20" rounded="rounded-lg" />
            </div>
          )}
          {error && <p className="p-5 text-sm text-danger">{error}</p>}
          {data && (
            <>
              {/* Media preview */}
              <div
                className={[
                  "flex items-center justify-center",
                  data.asset_kind === "font"
                    ? "bg-k-surface"
                    : data.asset_kind === "audio"
                      ? "bg-k-inset"
                      : "bg-black",
                ].join(" ")}
                style={{ maxHeight: "52vh" }}
              >
                {data.asset_kind === "image" || data.asset_kind === "font" ? (
                  <img
                    src={previewUrl}
                    alt=""
                    style={{ maxWidth: "100%", maxHeight: "52vh", display: "block" }}
                  />
                ) : data.asset_kind === "video" ? (
                  <video
                    src={fileUrl}
                    controls
                    autoPlay
                    muted
                    playsInline
                    style={{ maxWidth: "100%", maxHeight: "52vh", display: "block" }}
                  />
                ) : data.asset_kind === "audio" ? (
                  <div className="w-full flex flex-col items-center gap-3 py-10 px-6">
                    <span className="icon-well w-14 h-14 !rounded-none">
                      <Music className="w-6 h-6" strokeWidth={1.5} aria-hidden />
                    </span>
                    <audio src={fileUrl} controls className="w-full max-w-md" />
                  </div>
                ) : (
                  <p className="text-white/60 text-sm p-8">No preview for this file kind.</p>
                )}
              </div>

              <div className="p-5 space-y-6">
                {/* Action row */}
                <div className="flex flex-wrap items-center gap-2">
                  {!editing ? (
                    <>
                      <button onClick={() => setEditing(true)} className="btn btn-ghost btn-sm !rounded-none h-9 px-3.5">
                        <Pencil className="w-3.5 h-3.5" strokeWidth={1.8} aria-hidden />
                        Edit
                      </button>
                      <button
                        onClick={toggleEnabled}
                        title={data.enabled ? "The AI will not pick this for new posts" : "The AI can pick this for new posts again"}
                        className={[
                          "btn btn-sm !rounded-none h-9 px-3.5",
                          data.enabled ? "btn-ghost" : "btn-accent",
                        ].join(" ")}
                      >
                        {data.enabled ? (
                          <Pause className="w-3.5 h-3.5" strokeWidth={1.8} aria-hidden />
                        ) : (
                          <Play className="w-3.5 h-3.5" strokeWidth={1.8} aria-hidden />
                        )}
                        {data.enabled ? "Pause" : "Make active"}
                      </button>
                      <button onClick={deleteAsset} className="btn btn-ghost btn-sm !rounded-none h-9 px-3.5 text-k-danger hover:bg-red-50 hover:border-red-200">
                        <Trash2 className="w-3.5 h-3.5" strokeWidth={1.8} aria-hidden />
                        Delete
                      </button>
                      <span className="ml-auto text-[11.5px] text-fg/40 tabular-nums">
                        #{data.id} · {data.slug}
                      </span>
                    </>
                  ) : (
                    <>
                      <button onClick={saveEdits} className="btn btn-accent btn-sm h-9">
                        Save changes
                      </button>
                      <button
                        onClick={() => {
                          setEditing(false);
                          setDraftName(data.display_name || "");
                          setDraftDesc(data.ai_description);
                        }}
                        className="btn btn-ghost btn-sm !rounded-none h-9 px-4"
                      >
                        Cancel
                      </button>
                    </>
                  )}
                </div>

                {/* Display name */}
                {editing && (
                  <label className="text-sm block space-y-1">
                    <span className="text-[13.5px] font-semibold text-fg">Name</span>
                    <input
                      type="text"
                      value={draftName}
                      onChange={(e) => setDraftName(e.target.value)}
                      className="w-full rounded-lg border border-border bg-bg-subtle px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-fg/20 focus:border-fg/30 transition-shadow"
                    />
                  </label>
                )}

                {/* Stat strip — usage signals */}
                <div className="grid grid-cols-3 gap-2">
                  <MiniStat
                    label="Times used"
                    value={`${fmtNumber(data.usage_count)}`}
                    hint="in scripts"
                  />
                  <MiniStat
                    label="Last used"
                    value={data.last_used_at ? fmtRel(data.last_used_at) : "never"}
                    hint={data.last_used_at ? "" : "not used yet"}
                  />
                  <MiniStat label="Added" value={fmtRel(data.created_at)} hint="" />
                </div>

                {/* Instagram-sourced: Caption + Image Understanding + Composed text */}
                {(data.metadata as any)?.source === "instagram" ? (
                  <>
                    {(data.metadata as any)?.caption && (
                      <section>
                        <h3 className="text-[13.5px] font-semibold text-fg mb-2">Caption</h3>
                        <p className="text-sm text-fg whitespace-pre-wrap leading-relaxed">
                          {(data.metadata as any).caption}
                        </p>
                      </section>
                    )}
                    {(data.metadata as any)?.image_understanding && (
                      <section>
                        <h3 className="text-[13.5px] font-semibold text-fg mb-2">What the AI sees in it</h3>
                        <p className="text-sm text-fg/90 whitespace-pre-wrap leading-relaxed">
                          {(data.metadata as any).image_understanding}
                        </p>
                      </section>
                    )}
                    {(data.metadata as any)?.composed_retrieval_text && (
                      <details className="text-xs">
                        <summary className="cursor-pointer text-fg/55 hover:text-fg text-[12px] font-medium">
                          Search text (technical)
                        </summary>
                        <pre className="mt-2 text-xs text-fg/80 whitespace-pre-wrap bg-bg-muted rounded p-3 font-mono leading-relaxed">
                          {(data.metadata as any).composed_retrieval_text}
                        </pre>
                      </details>
                    )}
                  </>
                ) : (
                  /* Non-Instagram: original description field */
                  <section>
                    <h3 className="text-[13.5px] font-semibold text-fg mb-2">Description</h3>
                    {editing ? (
                      <textarea
                        value={draftDesc}
                        onChange={(e) => setDraftDesc(e.target.value)}
                        rows={10}
                        className="w-full rounded-lg border border-border bg-bg-subtle px-3 py-2.5 text-sm leading-relaxed focus:outline-none focus:ring-2 focus:ring-fg/20 focus:border-fg/30 transition-shadow"
                      />
                    ) : (
                      <p className="text-sm text-fg/90 whitespace-pre-wrap leading-relaxed">
                        {data.ai_description || (
                          <span className="text-muted italic">(no description)</span>
                        )}
                      </p>
                    )}
                  </section>
                )}

                {/* Structured attributes — pretty-printed */}
                {data.attributes && Object.keys(data.attributes).length > 0 && (
                  <section>
                    <div className="flex items-center justify-between mb-2">
                      <h3 className="text-[13.5px] font-semibold text-fg">Details the AI found</h3>
                      <button
                        type="button"
                        onClick={() => setShowRaw((v) => !v)}
                        className="text-[12px] font-medium text-fg/55 hover:text-fg"
                      >
                        {showRaw ? "Simple view" : "Technical view"}
                      </button>
                    </div>
                    {showRaw ? (
                      <pre className="text-xs text-fg/85 bg-bg-muted rounded-lg p-3 font-mono whitespace-pre-wrap leading-relaxed max-h-72 overflow-y-auto">
                        {JSON.stringify(data.attributes, null, 2)}
                      </pre>
                    ) : (
                      <AttributeList attrs={data.attributes} />
                    )}
                  </section>
                )}

                {/* File facts */}
                <section>
                  <h3 className="text-[13.5px] font-semibold text-fg mb-2">File</h3>
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    {data.mime_type && <Fact label="MIME" value={data.mime_type} />}
                    {data.bytes != null && <Fact label="Size" value={fmtBytes(data.bytes)} />}
                    {data.width != null && data.height != null && (
                      <Fact label="Dimensions" value={`${data.width} × ${data.height}`} />
                    )}
                    {data.duration_sec != null && (
                      <Fact label="Duration" value={`${data.duration_sec.toFixed(1)}s`} />
                    )}
                    {data.storage_bucket ? (
                      <Fact label="Storage" value={`bucket: ${data.storage_bucket}`} />
                    ) : data.local_path ? (
                      <Fact label="Storage" value="local disk" />
                    ) : null}
                    <Fact label="Embedded" value={data.embedded ? "yes" : "no"} />
                  </div>
                </section>

                {/* Provenance */}
                {(data.source_post_id ||
                  data.source_asset_id ||
                  data.source_frame_id ||
                  (data.metadata as any)?.source_url) && (
                  <section>
                    <h3 className="text-[13.5px] font-semibold text-fg mb-2">Where it came from</h3>
                    <div className="space-y-1.5 text-xs">
                      {(data.metadata as any)?.source_url && (
                        <p className="text-fg/90 break-all">
                          <span className="text-muted">Source URL: </span>
                          <a
                            href={(data.metadata as any).source_url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-fg underline decoration-fg/30 underline-offset-2 hover:decoration-fg"
                          >
                            {(data.metadata as any).source_url}
                          </a>
                        </p>
                      )}
                      {(data.source_post_id || data.source_asset_id || data.source_frame_id) && (
                        <p className="font-mono text-2xs text-muted">
                          post #{data.source_post_id ?? "—"} · asset #
                          {data.source_asset_id ?? "—"} · frame #
                          {data.source_frame_id ?? "—"}
                        </p>
                      )}
                    </div>
                  </section>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

function MiniStat({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <div className="rounded-xl bg-k-inset ring-1 ring-fg/[0.07] px-3.5 py-3">
      <p className="text-[12px] text-fg/55">{label}</p>
      <p className="mt-1 text-[18px] font-semibold tracking-[-0.02em] text-fg tabular-nums">{value}</p>
      {hint && <p className="text-[11.5px] text-fg/45">{hint}</p>}
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-k-inset ring-1 ring-fg/[0.06] px-3 py-2">
      <dt className="text-[11.5px] text-fg/50">{label}</dt>
      <dd className="text-fg/90 tabular-nums break-all">{value}</dd>
    </div>
  );
}

/**
 * Render structured character attributes as nested rows instead of a JSON blob.
 * Top-level keys (e.g. "kandura", "ghutra") become section labels; their string/
 * primitive children render as label → value pairs. Nested objects recurse.
 */
function AttributeList({ attrs }: { attrs: Record<string, unknown> }) {
  const entries = Object.entries(attrs);
  if (entries.length === 0) return null;
  return (
    <div className="space-y-2">
      {entries.map(([k, v]) => (
        <AttrSection key={k} label={k} value={v} />
      ))}
    </div>
  );
}

function AttrSection({ label, value }: { label: string; value: unknown }) {
  if (value == null) return null;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return (
      <div className="flex items-baseline gap-3 text-sm border-b border-border/60 pb-1.5 last:border-0">
        <span className="text-[12.5px] text-fg/50 w-28 shrink-0 capitalize">
          {label}
        </span>
        <span className="text-fg/90">{String(value)}</span>
      </div>
    );
  }
  if (Array.isArray(value)) {
    return (
      <div className="text-sm border-b border-border/60 pb-1.5 last:border-0">
        <p className="text-[12.5px] text-fg/50 capitalize">{label}</p>
        <p className="text-fg/90 mt-0.5">
          {value.length === 0 ? <span className="text-muted italic">(none)</span> : value.map(String).join(", ")}
        </p>
      </div>
    );
  }
  if (typeof value === "object") {
    const obj = value as Record<string, unknown>;
    const keys = Object.keys(obj);
    return (
      <div className="rounded-xl bg-k-inset ring-1 ring-fg/[0.07] p-3">
        <p className="text-[12.5px] font-semibold text-fg mb-2 capitalize">{label}</p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
          {keys.map((k) => (
            <div key={k} className="flex items-baseline gap-2">
              <span className="text-[12.5px] text-fg/50 shrink-0 capitalize">
                {k}
              </span>
              <span className="text-fg/90 break-words">
                {obj[k] == null
                  ? "—"
                  : typeof obj[k] === "object"
                    ? JSON.stringify(obj[k])
                    : String(obj[k])}
              </span>
            </div>
          ))}
        </div>
      </div>
    );
  }
  return null;
}
