import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { FileText, X } from "lucide-react";

import { api } from "../lib/api";
import { fmtRel } from "../lib/format";
import type { BrandDocDetail } from "../lib/types";
import { Skeleton } from "./Skeleton";

interface Props {
  docId: number;
  onClose: () => void;
}

function fmtBytes(n: number | null | undefined): string {
  if (!n) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Read-only view of one ingested brand document — opened from the Recent-uploads
 * list. Shows the AI summary/description plus the full extracted `raw_text` (the
 * actual document content), fetched fresh from GET /documents/{id}.
 */
export function BrandDocDrawer({ docId, onClose }: Props) {
  const [doc, setDoc] = useState<BrandDocDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .document(docId)
      .then((r) => !cancelled && setDoc(r))
      .catch((e) => !cancelled && setError(String(e?.message || e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [docId]);

  // ESC closes + body scroll lock.
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

  const title = doc?.source_filename ?? (doc ? `(${doc.doc_kind})` : "Document");

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
        onClick={(e) => e.stopPropagation()}
        className="card w-full max-w-2xl max-h-[85vh] flex flex-col overflow-hidden"
      >
        {/* Header */}
        <div className="flex items-start justify-between gap-3 p-5 border-b border-border">
          <div className="min-w-0">
            <p className="eyebrow mb-1.5">Document #{docId}</p>
            <h2 className="text-lg font-semibold text-fg truncate flex items-center gap-2">
              <FileText className="w-4 h-4 text-muted shrink-0" strokeWidth={1.8} />
              {title}
            </h2>
            {doc && (
              <p className="font-mono text-2xs text-muted mt-1">
                {doc.doc_kind}
                {doc.mime_type ? ` · ${doc.mime_type}` : ""}
                {doc.bytes ? ` · ${fmtBytes(doc.bytes)}` : ""}
                {doc.created_at ? ` · ${fmtRel(doc.created_at)}` : ""}
              </p>
            )}
          </div>
          <button
            onClick={onClose}
            className="btn btn-ghost shrink-0 px-2"
            aria-label="Close"
          >
            <X className="w-4 h-4" strokeWidth={1.8} />
          </button>
        </div>

        {/* Body */}
        <div className="p-5 overflow-y-auto space-y-5">
          {loading ? (
            <div className="space-y-2">
              {Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} h="h-4" />
              ))}
            </div>
          ) : error ? (
            <p className="text-sm text-danger">Couldn't load document — {error}</p>
          ) : !doc ? (
            <p className="text-sm text-muted">Document not found.</p>
          ) : (
            <>
              {doc.uploader_note && (
                <p className="font-mono text-2xs text-muted italic">{doc.uploader_note}</p>
              )}

              {/* Real preview of the original file (image / PDF). Falls back to
                  the extracted text when the file isn't on this server or the
                  format can't render in-browser. */}
              <section>
                <p className="eyebrow mb-2">Preview</p>
                <DocPreview doc={doc} />
              </section>

              {doc.ai_summary && (
                <section>
                  <p className="eyebrow mb-1.5">AI summary</p>
                  <p className="text-sm text-fg/85 leading-relaxed whitespace-pre-wrap">
                    {doc.ai_summary}
                  </p>
                </section>
              )}
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** Renders the actual uploaded file when possible (image inline, PDF in a
 *  frame). For text-based docs, or when the original file isn't on this server,
 *  it shows the extracted text instead. */
function DocPreview({ doc }: { doc: BrandDocDetail }) {
  const [imgError, setImgError] = useState(false);
  const url = api.documentFileUrl(doc.id);
  const mime = doc.mime_type ?? "";
  const name = doc.source_filename ?? "";
  const isImage = doc.doc_kind === "image" || mime.startsWith("image/");
  const isPdf = mime === "application/pdf" || name.toLowerCase().endsWith(".pdf");

  // Real visual preview — only if the original file is actually on the server.
  if (doc.has_file && isImage && !imgError) {
    return (
      <img
        src={url}
        alt={name || "document preview"}
        onError={() => setImgError(true)}
        className="max-w-full max-h-[60vh] mx-auto rounded-lg border border-border"
      />
    );
  }
  if (doc.has_file && isPdf) {
    return (
      <div className="space-y-2">
        <iframe
          src={url}
          title={name || "document preview"}
          className="w-full h-[60vh] rounded-lg border border-border bg-white"
        />
        <a
          href={url}
          target="_blank"
          rel="noreferrer"
          className="inline-block font-mono text-2xs text-accent hover:underline"
        >
          Open original ↗
        </a>
      </div>
    );
  }

  // Text-based formats (and docx whose text we extracted): show the content.
  if (doc.raw_text && doc.raw_text.trim()) {
    return (
      <>
        {!doc.has_file && (isImage || isPdf) && (
          <p className="text-xs text-muted mb-2">
            Original file isn't on this server — showing the extracted text.
          </p>
        )}
        {doc.has_file && !isImage && !isPdf && (
          <a
            href={url}
            target="_blank"
            rel="noreferrer"
            className="inline-block font-mono text-2xs text-accent hover:underline mb-2"
          >
            Open original file ↗
          </a>
        )}
        <pre className="text-xs text-fg/85 leading-relaxed whitespace-pre-wrap font-sans bg-bg-muted rounded-lg p-4 max-h-[55vh] overflow-y-auto">
          {doc.raw_text}
        </pre>
      </>
    );
  }

  // Nothing renderable (e.g. an image whose file isn't here and no AI text).
  return (
    <p className="text-sm text-muted">
      {doc.has_file ? (
        <a href={url} target="_blank" rel="noreferrer" className="text-accent hover:underline">
          Open original file ↗
        </a>
      ) : (
        "No preview available — the original file isn't on this server."
      )}
    </p>
  );
}
