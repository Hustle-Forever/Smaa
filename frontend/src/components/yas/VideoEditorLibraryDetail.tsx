/**
 * Yas Producer · Library · Video Editor detail
 *
 * Read-only detail for a single Video Editor project. Hydrates the project via
 * api.yasProducer.videoEditor.fetch(projectId) and shows the exported MP4 (when
 * the render is ready) or a draft/rendering notice, plus a "Continue editing"
 * button that reopens the project in the editor.
 *
 * Portaled to document.body to escape the page-transition transform that traps
 * `fixed` descendants (see CLAUDE.md modal-pattern note).
 */
import { useEffect, useRef, useState } from "react";

import { useModalChrome } from "../../lib/useModalChrome";
import { createPortal } from "react-dom";

import { api } from "../../lib/api";
import { fmtRel } from "../../lib/format";
import type { VideoEditorProject, VideoEditRenderStatus } from "../../lib/types";
import { useConfirm } from "../ConfirmProvider";
import { useToast } from "../ToastProvider";

function renderLabel(status: VideoEditRenderStatus | string | undefined): string {
  switch (status) {
    case "rendering":
      return "Exporting…";
    case "ready":
      return "Exported";
    case "failed":
      return "Export failed";
    default:
      return "Draft — not exported yet";
  }
}

export function VideoEditorLibraryDetail({
  projectId,
  title,
  onClose,
  onContinue,
  onDelete,
}: {
  projectId: number;
  title: string | null;
  onClose: () => void;
  /** Reopen this project in the editor to keep working on it. */
  onContinue?: () => void;
  /** Permanently delete this project (server-side + cached grid). */
  onDelete?: () => void | Promise<void>;
}) {
  const [project, setProject] = useState<VideoEditorProject | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const toast = useToast();
  const confirm = useConfirm();

  // Escape, scroll lock, focus trap and focus restore in one place. The first
  // two were already here; the trap is the new part -- without it Tab walked
  // straight out of the dialog and into the page behind the overlay, which
  // makes a dialog modal only to a mouse.
  const panelRef = useRef<HTMLDivElement>(null);
  const { trapTab } = useModalChrome({ onClose, panelRef });

  useEffect(() => {
    let cancelled = false;
    api.yasProducer.videoEditor
      .fetch(projectId)
      .then((p) => {
        if (!cancelled) setProject(p);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load");
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const status = project?.render?.composition_status;
  const exported = status === "ready";
  const videoUrl = exported ? api.yasProducer.videoEditor.compositionFileUrl(projectId) : null;
  const clipCount = project?.clips?.length ?? 0;
  const trackCount = project?.tracks?.length ?? 0;

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
        style={{ width: "min(620px, 100%)", maxHeight: "min(940px, 94vh)" }}
      >
        <header className="shrink-0 px-4 py-3 border-b border-border flex items-center gap-2.5 bg-bg">
          <span className="font-mono text-3xs px-1.5 py-0.5 rounded bg-cyan-500/15 text-cyan-600">
            Video Editor
          </span>
          <h2 className="text-sm font-semibold text-fg flex-1 truncate">
            {project?.title || title || "(untitled)"}
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
          {!project && !error && (
            <p className="text-sm text-muted text-center py-16">Loading…</p>
          )}
          {error && (
            <p className="text-sm text-danger text-center py-16 px-4">
              Couldn’t load this project: {error}
            </p>
          )}

          {project && (
            <>
              <div className="bg-black flex items-center justify-center aspect-video max-h-[55vh]">
                {exported && videoUrl ? (
                  <video
                    src={videoUrl}
                    controls
                    autoPlay
                    playsInline
                    className="w-full h-full object-contain bg-black"
                  />
                ) : (
                  <div className="text-center px-6 py-10 space-y-2">
                    <div className="text-2xl">{status === "rendering" ? "⏳" : "🎬"}</div>
                    <p className="text-[13px] font-medium text-white/90">
                      {renderLabel(status)}
                    </p>
                    {project.render?.composition_error && (
                      <p className="text-2xs text-danger/80">
                        {project.render.composition_error}
                      </p>
                    )}
                  </div>
                )}
              </div>

              <dl className="px-4 py-3 grid grid-cols-2 gap-x-4 gap-y-2 text-[12px]">
                <Meta label="Aspect" value={project.project?.aspect || "—"} />
                <Meta
                  label="Canvas"
                  value={
                    project.project
                      ? `${project.project.width}×${project.project.height}`
                      : "—"
                  }
                />
                <Meta label="Clips" value={String(clipCount)} />
                <Meta label="Tracks" value={String(trackCount)} />
                <Meta label="Edits" value={`rev ${project.history?.rev ?? 0}`} />
                <Meta label="Updated" value={fmtRel(project.updated_at || project.created_at || "")} />
              </dl>
            </>
          )}
        </div>

        <footer className="shrink-0 px-4 py-3 border-t border-border bg-bg-muted/50 flex items-center gap-3">
          <p className="text-2xs font-mono text-muted flex-1">#{projectId}</p>
          <button onClick={onClose} className="btn btn-ghost text-xs">
            Close
          </button>
          {onContinue && (
            <button onClick={onContinue} className="btn btn-primary text-xs">
              Continue editing
            </button>
          )}
          {onDelete && (
            <button
              onClick={async () => {
                if (deleting) return;
                if (
                  !(await confirm({
                    title: "Delete project permanently?",
                    message:
                      "Its final video will be removed from the library. Your original clips will stay. This can’t be undone.",
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
          {exported && videoUrl && (
            <a
              href={videoUrl + (videoUrl.includes("?") ? "&" : "?") + "download=1"}
              download
              className="btn btn-ghost text-xs"
            >
              Download video
            </a>
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
