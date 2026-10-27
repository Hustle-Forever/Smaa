import { useCallback, useEffect, useState } from "react";
import { UploadCloud } from "lucide-react";

import { BrandDocDrawer } from "../components/BrandDocDrawer";
import { Skeleton } from "../components/Skeleton";
import { useToast } from "../components/ToastProvider";
import { api } from "../lib/api";
import { fmtBytes, fmtRel } from "../lib/format";
import type { AgentId, BrandDoc } from "../lib/types";

interface Props {
  onStartRun: (agent: AgentId, runId: number, preface?: string) => void;
  /** When rendered inside the Brand workspace, the workspace owns the header. */
  embedded?: boolean;
}

const ACCEPT =
  ".pdf,.docx,.doc,.txt,.md,.markdown,.rtf,.jpg,.jpeg,.png,.webp,.gif,.bmp,.tiff,.tif,.heic,.heif,.svg,.avif" +
  ",.mp4,.mov,.m4v,.webm,.avi,.mkv";

// How many files to upload at once. Bulk video uploads (1000+) ride this pool.
const UPLOAD_CONCURRENCY = 4;

type VideoStatus = {
  uploaded: number;
  processing: number;
  completed: number;
  failed: number;
  total: number;
  pending: number;
};

export function UploadPage({ onStartRun, embedded = false }: Props) {
  const [files, setFiles] = useState<File[]>([]);
  const [freeText, setFreeText] = useState("");
  const [note, setNote] = useState("");
  const [dragging, setDragging] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  // Bulk-upload progress (transport) and background video-analysis progress.
  const [progress, setProgress] = useState<{ done: number; total: number; failed: number } | null>(null);
  const [vidStatus, setVidStatus] = useState<VideoStatus | null>(null);
  const [polling, setPolling] = useState(false);

  const [docs, setDocs] = useState<BrandDoc[]>([]);
  const [docsLoading, setDocsLoading] = useState(true);
  const [openDocId, setOpenDocId] = useState<number | null>(null);

  const refreshDocs = useCallback(async () => {
    try {
      const r = await api.documents(20);
      setDocs(r.items);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setDocsLoading(false);
    }
  }, []);

  useEffect(() => {
    refreshDocs();
    // Resume the analysis indicator if videos are still being processed (e.g.
    // after a page reload mid-run).
    api.ingestStatus()
      .then((s) => {
        if (s.video.total > 0) setVidStatus(s.video);
        if (s.video.pending > 0) setPolling(true);
      })
      .catch(() => {});
  }, [refreshDocs]);

  // Poll the analysis queue while videos are still pending.
  useEffect(() => {
    if (!polling) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const s = await api.ingestStatus();
        if (!alive) return;
        setVidStatus(s.video);
        refreshDocs();
        if (s.video.pending === 0) {
          setPolling(false);
          return;
        }
      } catch {
        /* transient — keep polling */
      }
      if (alive) timer = setTimeout(tick, 4000);
    };
    tick();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [polling, refreshDocs]);

  const addFiles = useCallback((list: FileList | null) => {
    if (!list) return;
    setFiles((prev) => {
      const out = [...prev];
      for (const f of Array.from(list)) {
        if (!out.some((x) => x.name === f.name && x.size === f.size)) {
          out.push(f);
        }
      }
      return out;
    });
  }, []);

  function removeFile(i: number) {
    setFiles((prev) => prev.filter((_, idx) => idx !== i));
  }

  async function submit() {
    if (files.length === 0 && !freeText.trim()) {
      setError("Add at least one file or some free text.");
      return;
    }
    setError(null);
    setSubmitting(true);
    const total = files.length;
    setProgress(total ? { done: 0, total, failed: 0 } : null);

    try {
      let videoCount = 0;
      let docCount = 0;
      const failedNames: string[] = [];

      // 1. Upload files one-at-a-time, bounded concurrency. Each call stores the
      //    file + inserts a pending row; videos are NOT processed inline.
      if (total) {
        let idx = 0;
        let done = 0;
        const worker = async () => {
          while (idx < files.length) {
            const f = files[idx++];
            try {
              const r = await api.uploadFile(f);
              if (r.kind === "video") videoCount++;
              else docCount++;
            } catch {
              failedNames.push(f.name);
            } finally {
              done++;
              setProgress({ done, total, failed: failedNames.length });
            }
          }
        };
        await Promise.all(
          Array.from({ length: Math.min(UPLOAD_CONCURRENCY, total) }, worker),
        );
      }

      // 2. Kick processing for the NON-video side (videos are drained by the
      //    backend poller). Free text goes through the classic /upload (which
      //    inserts the note + runs the doc DAG); otherwise re-run on pending docs.
      let runId: number | null = null;
      if (freeText.trim()) {
        const r = await api.upload({
          files: [],
          free_text: freeText.trim(),
          uploader_note: note.trim() || undefined,
        });
        runId = r.run_id;
      } else if (docCount > 0) {
        const r = await api.runIngest();
        runId = r.run_id;
      }

      // 3. Feedback.
      const parts: string[] = [];
      if (docCount) parts.push(`${docCount} doc${docCount === 1 ? "" : "s"}`);
      if (videoCount) parts.push(`${videoCount} video${videoCount === 1 ? "" : "s"}`);
      if (freeText.trim()) parts.push("1 note");
      toast.success(
        `Uploaded ${parts.join(" · ") || "nothing"}`,
        videoCount > 0
          ? `${videoCount} video${videoCount === 1 ? "" : "s"} queued — analysing in the background`
          : "brand-doc-ingest started",
      );
      if (failedNames.length) {
        setError(`${failedNames.length} file(s) failed to upload (e.g. ${failedNames[0]}).`);
      }
      if (runId != null) {
        const preface =
          videoCount > 0
            ? `${videoCount} video${videoCount === 1 ? "" : "s"} queued for analysis; processing docs.`
            : "Processing uploaded documents.";
        onStartRun("brand-doc-ingest", runId, preface);
      }

      // reset form
      setFiles([]);
      setFreeText("");
      setNote("");
      // start/refresh the background-analysis indicator
      if (videoCount > 0) {
        setPolling(true);
      }
      setTimeout(refreshDocs, 800);
    } catch (e: any) {
      const msg = String(e?.message || e);
      setError(msg);
      toast.error("Upload failed", msg);
    } finally {
      setSubmitting(false);
      setProgress(null);
    }
  }

  async function retryFailed() {
    try {
      const r = await api.retryFailedVideos();
      toast.success(
        `Re-queued ${r.requeued} video${r.requeued === 1 ? "" : "s"}`,
        "Analysing again in the background…",
      );
      if (r.requeued > 0) setPolling(true);
    } catch (e: any) {
      toast.error("Retry failed", String(e?.message || e));
    }
  }

  const analysing = !!vidStatus && vidStatus.pending > 0;
  const vidPct =
    vidStatus && vidStatus.total > 0
      ? Math.round((vidStatus.completed / vidStatus.total) * 100)
      : 0;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[1.2fr_1fr] gap-8">
      {/* LEFT: form */}
      <div>
        {!embedded && (
          <>
            <p className="eyebrow mb-1.5">Upload</p>
            <h1 className="h1 mb-1.5">Add brand material</h1>
            <p className="lead mb-6 max-w-2xl">
              Drop owner-uploaded files (documents, images, or brand videos) or
              paste free text. Each item becomes a row in{" "}
              <code className="font-mono">brand_documents</code> and feeds into
              the brand profile.
            </p>
          </>
        )}

        {/* Drop zone */}
        <label
          className={[
            "block rounded-xl border-2 border-dashed p-8 text-center cursor-pointer",
            "transition-all duration-200",
            dragging
              ? "border-accent bg-accent-fade scale-[1.01] shadow-glow"
              : "border-border bg-bg-deep hover:border-fg/40 hover:bg-bg-muted",
          ].join(" ")}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            addFiles(e.dataTransfer.files);
          }}
        >
          <input
            type="file"
            multiple
            accept={ACCEPT}
            onChange={(e) => addFiles(e.currentTarget.files)}
            className="hidden"
          />
          <UploadCloud
            className={[
              "w-10 h-10 mx-auto mb-2 transition-colors",
              dragging ? "text-accent" : "text-muted",
            ].join(" ")}
            strokeWidth={1.5}
          />
          <p className="text-base font-medium text-fg">
            Drop files here, or click to browse
          </p>
          <p className="font-mono text-2xs text-muted mt-1">
            PDF · DOCX · TXT · MD · images · video (MP4, MOV, WebM, …) — bulk OK
          </p>
        </label>

        {files.length > 0 && (
          <div className="mt-3">
            <div className="flex items-baseline justify-between mb-2">
              <span className="font-mono text-2xs text-muted">
                {files.length} file{files.length === 1 ? "" : "s"} selected
              </span>
              <button
                onClick={() => setFiles([])}
                className="font-mono text-2xs text-muted hover:text-danger"
              >
                Clear all
              </button>
            </div>
            <div className="flex flex-wrap gap-2 max-h-44 overflow-y-auto">
              {files.slice(0, 60).map((f, i) => (
                <span
                  key={`${f.name}-${i}`}
                  className="inline-flex items-center gap-2 pl-3 pr-2 py-1.5 rounded-full bg-bg-muted border border-border text-xs animate-riseFade"
                >
                  <span className="text-sm leading-none">{iconFor(f.name)}</span>
                  <span className="font-mono max-w-[16ch] truncate text-fg">
                    {f.name}
                  </span>
                  <span className="font-mono text-3xs text-muted">
                    {fmtBytes(f.size)}
                  </span>
                  <button
                    onClick={() => removeFile(i)}
                    className="w-4 h-4 rounded-full hover:bg-danger/15 text-muted hover:text-danger flex items-center justify-center transition-colors"
                    aria-label={`Remove ${f.name}`}
                  >
                    ×
                  </button>
                </span>
              ))}
              {files.length > 60 && (
                <span className="inline-flex items-center px-3 py-1.5 rounded-full bg-bg-muted border border-border text-xs font-mono text-muted">
                  +{files.length - 60} more
                </span>
              )}
            </div>
          </div>
        )}

        <div className="mt-6">
          <label className="block">
            <span className="eyebrow block mb-2">Or paste free text</span>
            <textarea
              rows={6}
              placeholder="A brand-voice memo, a copy-deck snippet, anything textual…"
              value={freeText}
              onChange={(e) => setFreeText(e.currentTarget.value)}
              className="textarea"
            />
          </label>
        </div>

        <div className="mt-4">
          <label className="block">
            <span className="eyebrow block mb-2">Uploader note (optional)</span>
            <input
              type="text"
              placeholder="e.g. From Lina (Marketing) · style guide v3"
              value={note}
              onChange={(e) => setNote(e.currentTarget.value)}
              className="input py-2.5"
            />
          </label>
        </div>

        {/* Upload-transport progress */}
        {progress && progress.total > 1 && (
          <div className="mt-5 space-y-1.5">
            <div className="flex justify-between font-mono text-2xs text-muted">
              <span>
                Uploading {progress.done} / {progress.total}
                {progress.failed > 0 ? ` · ${progress.failed} failed` : ""}
              </span>
              <span>{Math.round((progress.done / progress.total) * 100)}%</span>
            </div>
            <div className="h-2 bg-bg-muted rounded-full overflow-hidden">
              <div
                className="h-full bg-accent transition-all duration-300"
                style={{ width: `${(progress.done / progress.total) * 100}%` }}
              />
            </div>
          </div>
        )}

        <div className="mt-6 flex items-center gap-3">
          <button
            onClick={submit}
            disabled={submitting || (files.length === 0 && !freeText.trim())}
            className="btn btn-accent"
          >
            {submitting
              ? progress
                ? `Uploading ${progress.done}/${progress.total}…`
                : "Uploading…"
              : "Upload & ingest"}
          </button>
          {error && (
            <span className="text-sm text-danger truncate" title={error}>
              {error}
            </span>
          )}
        </div>

        {/* Background video-analysis indicator */}
        {vidStatus && vidStatus.total > 0 && (
          <div className="mt-5 card p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm font-medium text-fg">
                {analysing ? "Analysing brand videos…" : "Brand videos analysed"}
              </span>
              <span className="font-mono text-2xs text-muted">
                {vidStatus.completed}/{vidStatus.total} done
                {vidStatus.failed > 0 ? ` · ${vidStatus.failed} failed` : ""}
              </span>
            </div>
            <div className="h-2 bg-bg-muted rounded-full overflow-hidden">
              <div
                className={`h-full bg-accent transition-all duration-500 ${analysing ? "animate-pulse" : ""}`}
                style={{ width: `${vidPct}%` }}
              />
            </div>
            <p className="font-mono text-2xs text-muted mt-2">
              {analysing
                ? "Transcribed, embedded, and folded into the brand profile when done — runs in the background, safe to leave."
                : "The brand profile has been updated with these videos."}
            </p>
            {vidStatus.failed > 0 && !analysing && (
              <button
                onClick={retryFailed}
                className="mt-3 font-mono text-2xs text-accent hover:underline"
              >
                Retry {vidStatus.failed} failed video{vidStatus.failed === 1 ? "" : "s"}
              </button>
            )}
          </div>
        )}
      </div>

      {/* RIGHT: existing docs */}
      <aside>
        <div className="flex items-baseline justify-between mb-3">
          <p className="eyebrow">Recent uploads</p>
          <button
            onClick={refreshDocs}
            className="font-mono text-2xs text-fg/80 hover:text-fg"
          >
            Refresh
          </button>
        </div>
        {docsLoading ? (
          <ul className="space-y-2">
            {Array.from({ length: 4 }).map((_, i) => (
              <li key={i}>
                <Skeleton h="h-16" rounded="rounded-xl" />
              </li>
            ))}
          </ul>
        ) : docs.length === 0 ? (
          <p className="text-sm text-muted">
            No documents yet. The first upload will start a doc-ingest run.
          </p>
        ) : (
          <ul className="space-y-2">
            {docs.map((d) => {
              const cls =
                d.upload_status === "completed"
                  ? "pill-completed"
                  : d.upload_status === "failed"
                  ? "pill-failed"
                  : d.upload_status === "processing"
                  ? "pill-running"
                  : "pill-uploaded";
              return (
                <li key={d.id}>
                  <button
                    type="button"
                    onClick={() => setOpenDocId(d.id)}
                    className="card card-hover p-3 w-full text-left"
                    title="Open document"
                  >
                    <div className="flex items-baseline justify-between gap-2 mb-1">
                      <span className="font-mono text-xs text-muted">#{d.id}</span>
                      <span className="font-mono text-2xs text-muted">
                        {fmtRel(d.created_at)}
                      </span>
                    </div>
                    <div className="flex items-center justify-between gap-2 mb-1">
                      <p className="text-sm font-medium text-fg truncate">
                        {iconFor(d.source_filename ?? d.doc_kind)}{" "}
                        {d.source_filename ?? `(${d.doc_kind})`}
                      </p>
                      <span className={`pill ${cls}`}>{d.upload_status}</span>
                    </div>
                    {d.preview && (
                      <p className="text-xs text-fg/80/80 leading-relaxed line-clamp-2">
                        {d.preview}
                      </p>
                    )}
                    {d.uploader_note && (
                      <p className="font-mono text-2xs text-muted mt-1 italic truncate">
                        {d.uploader_note}
                      </p>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </aside>

      {openDocId != null && (
        <BrandDocDrawer docId={openDocId} onClose={() => setOpenDocId(null)} />
      )}
    </div>
  );
}

function iconFor(name: string): string {
  const ext = name.toLowerCase().split(".").pop();
  if (!ext) return "📄";
  if (["pdf"].includes(ext)) return "📕";
  if (["docx", "doc"].includes(ext)) return "📘";
  if (["md", "markdown"].includes(ext)) return "📝";
  if (["txt", "rtf"].includes(ext)) return "📄";
  if (["mp4", "mov", "m4v", "webm", "avi", "mkv"].includes(ext)) return "🎬";
  if (["jpg", "jpeg", "png", "webp", "gif", "bmp", "tiff", "tif", "heic", "heif", "svg", "avif"].includes(ext))
    return "🖼️";
  return "📄";
}
