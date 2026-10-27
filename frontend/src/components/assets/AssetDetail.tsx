import { useEffect, useRef, useState } from "react";

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
        className="relative bg-bg rounded-2xl shadow-2xl border border-border overflow-hidden flex flex-col animate-scaleIn"
        style={{ width: "min(820px, 100%)", maxHeight: "92vh" }}
      >
        {/* Header */}
        <header className="shrink-0 px-5 py-3.5 border-b border-border flex items-center gap-3 flex-wrap bg-bg">
          <div className="flex items-center gap-2 flex-wrap min-w-0 flex-1">
            <span
              className={[
                "inline-flex items-center gap-1.5 font-mono text-3xs uppercase tracking-wider2 px-2 py-1 rounded-full",
                data?.enabled
                  ? "bg-success/10 text-success border border-success/20"
                  : "bg-bg-muted text-muted border border-border",
              ].join(" ")}
            >
              <span
                aria-hidden
                className={[
                  "w-1.5 h-1.5 rounded-full",
                  data?.enabled ? "bg-success" : "bg-muted/60",
                ].join(" ")}
              />
              {data?.enabled ? "In rotation" : "Muted"}
            </span>
            <span className="pill pill-uploaded">{data?.asset_kind ?? "?"}</span>
            {data?.archetype && (
              <span className="font-mono text-3xs uppercase tracking-wider2 px-2 py-1 rounded-full bg-accent/10 text-accent border border-accent/20">
                {data.archetype.replace("_", " ")}
              </span>
            )}
            <h2 className="text-sm font-semibold text-fg truncate min-w-0">
              {data?.display_name || data?.slug || "asset"}
            </h2>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="w-8 h-8 rounded-full bg-bg-muted hover:bg-border text-fg flex items-center justify-center text-base shrink-0"
          >
            ×
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
                  data.asset_kind === "font" ? "bg-white" : "bg-black",
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
                    <span className="text-5xl text-emerald-300 leading-none">♪</span>
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
                      <button onClick={() => setEditing(true)} className="btn btn-ghost text-xs">
                        Edit
                      </button>
                      <button
                        onClick={toggleEnabled}
                        className={[
                          "btn text-xs",
                          data.enabled ? "btn-ghost" : "btn-accent",
                        ].join(" ")}
                      >
                        {data.enabled ? "Mute" : "Enable"}
                      </button>
                      <button onClick={deleteAsset} className="btn btn-ghost text-xs text-danger">
                        Delete
                      </button>
                      <span className="ml-auto font-mono text-3xs text-muted">
                        #{data.id} · {data.slug}
                      </span>
                    </>
                  ) : (
                    <>
                      <button onClick={saveEdits} className="btn btn-primary text-xs">
                        Save changes
                      </button>
                      <button
                        onClick={() => {
                          setEditing(false);
                          setDraftName(data.display_name || "");
                          setDraftDesc(data.ai_description);
                        }}
                        className="btn btn-ghost text-xs"
                      >
                        Cancel
                      </button>
                    </>
                  )}
                </div>

                {/* Display name */}
                {editing && (
                  <label className="text-sm block space-y-1">
                    <span className="eyebrow">Display name</span>
                    <input
                      type="text"
                      value={draftName}
                      onChange={(e) => setDraftName(e.target.value)}
                      className="w-full rounded-lg border border-border bg-bg-subtle px-3 py-2 text-sm focus:outline-none focus:border-accent focus:shadow-glow transition-shadow"
                    />
                  </label>
                )}

                {/* Stat strip — usage signals */}
                <div className="grid grid-cols-3 gap-2">
                  <MiniStat
                    label="Used"
                    value={`${fmtNumber(data.usage_count)}×`}
                    hint="In scripts"
                  />
                  <MiniStat
                    label="Last used"
                    value={data.last_used_at ? fmtRel(data.last_used_at) : "never"}
                    hint={data.last_used_at ? "" : "first in rotation"}
                  />
                  <MiniStat label="Added" value={fmtRel(data.created_at)} hint="" />
                </div>

                {/* Instagram-sourced: Caption + Image Understanding + Composed text */}
                {(data.metadata as any)?.source === "instagram" ? (
                  <>
                    {(data.metadata as any)?.caption && (
                      <section>
                        <p className="eyebrow mb-2">Caption</p>
                        <p className="text-sm text-fg whitespace-pre-wrap leading-relaxed">
                          {(data.metadata as any).caption}
                        </p>
                      </section>
                    )}
                    {(data.metadata as any)?.image_understanding && (
                      <section>
                        <p className="eyebrow mb-2">Image understanding</p>
                        <p className="text-sm text-fg/90 whitespace-pre-wrap leading-relaxed">
                          {(data.metadata as any).image_understanding}
                        </p>
                      </section>
                    )}
                    {(data.metadata as any)?.composed_retrieval_text && (
                      <details className="text-xs">
                        <summary className="cursor-pointer text-muted hover:text-fg font-mono uppercase tracking-wider2 text-3xs">
                          Composed retrieval text
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
                    <p className="eyebrow mb-2">Description</p>
                    {editing ? (
                      <textarea
                        value={draftDesc}
                        onChange={(e) => setDraftDesc(e.target.value)}
                        rows={10}
                        className="w-full rounded-lg border border-border bg-bg-subtle px-3 py-2.5 text-sm leading-relaxed focus:outline-none focus:border-accent focus:shadow-glow transition-shadow"
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
                      <p className="eyebrow">Attributes</p>
                      <button
                        type="button"
                        onClick={() => setShowRaw((v) => !v)}
                        className="font-mono text-3xs uppercase tracking-wider2 text-muted hover:text-fg"
                      >
                        {showRaw ? "Pretty view" : "Raw JSON"}
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
                  <p className="eyebrow mb-2">File</p>
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
                    <p className="eyebrow mb-2">Provenance</p>
                    <div className="space-y-1.5 text-xs">
                      {(data.metadata as any)?.source_url && (
                        <p className="text-fg/90 break-all">
                          <span className="text-muted">Source URL: </span>
                          <a
                            href={(data.metadata as any).source_url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-accent hover:underline"
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
    <div className="card p-3">
      <p className="eyebrow">{label}</p>
      <p className="mt-1 text-base font-semibold text-fg tabular-nums">{value}</p>
      {hint && <p className="font-mono text-3xs text-muted">{hint}</p>}
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-bg-muted px-3 py-2">
      <dt className="font-mono text-3xs uppercase tracking-wider2 text-muted">{label}</dt>
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
        <span className="font-mono text-3xs uppercase tracking-wider2 text-muted w-28 shrink-0">
          {label}
        </span>
        <span className="text-fg/90">{String(value)}</span>
      </div>
    );
  }
  if (Array.isArray(value)) {
    return (
      <div className="text-sm border-b border-border/60 pb-1.5 last:border-0">
        <p className="font-mono text-3xs uppercase tracking-wider2 text-muted">{label}</p>
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
      <div className="rounded-lg bg-bg-muted/60 border border-border p-3">
        <p className="font-mono text-3xs uppercase tracking-wider2 text-fg mb-2">{label}</p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
          {keys.map((k) => (
            <div key={k} className="flex items-baseline gap-2">
              <span className="font-mono text-3xs uppercase tracking-wider2 text-muted shrink-0">
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
