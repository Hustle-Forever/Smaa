/**
 * Video Editor · shell layout
 *
 * The 3-zone editor frame: [Sources] · [Preview + Timeline] · [Inspector].
 * Phase B wires the Player preview, the Sources media bin, the Inspector (trim),
 * a simple selectable clip strip, and Export (Remotion render + status poll).
 * The full drag/trim/split multi-track timeline arrives in Phase C.
 */
import { lazy, Suspense, useEffect, useRef, useState } from "react";

import { api } from "../../../lib/api";
import { useFullBleed } from "../../LayoutChrome";
import { useVideoEditorStore } from "../../../lib/store";
import type { ContinueToPreview } from "../roadmap/types";
import { EditorTopBar } from "./EditorTopBar";
import { RightSidebar } from "./RightSidebar";
import { SidebarShell } from "./sidebar/SidebarShell";
import { Timeline } from "./Timeline";

/**
 * Lazy, and this one import is worth the ceremony.
 *
 * PreviewPane is the ONLY module that pulls @remotion/player and the shared
 * `video-edit-composition` at runtime -- verified: playerHandle.ts takes
 * PlayerRef with `import type` (erased), and everything else touching
 * video-edit-schema only wants sampleKeyframes. So this boundary is the whole
 * difference between shipping the Remotion player to every visitor of every
 * page and shipping it to the people who open the video editor.
 */
const PreviewPane = lazy(() =>
  import("./PreviewPane").then((m) => ({ default: m.PreviewPane })),
);

export function EditorShell({
  onBack,
  inRoadmap = false,
  onContinue,
}: {
  onBack: () => void;
  /** When inside the Producer roadmap's Editing stage: the Export button becomes
   *  "Finish & continue →" and the shell fills its parent (sits under the stepper). */
  inRoadmap?: boolean;
  /** Advance to Finalization with the finished asset (roadmap only). */
  onContinue?: ContinueToPreview;
}) {
  useFullBleed(); // editor uses the full main-area width (drops the page max-width)
  const projectId = useVideoEditorStore((s) => s.projectId);
  const title = useVideoEditorStore((s) => s.title);
  const clips = useVideoEditorStore((s) => s.clips);
  const render = useVideoEditorStore((s) => s.render);
  const rev = useVideoEditorStore((s) => s.rev);
  const setRender = useVideoEditorStore((s) => s.setRender);
  const flush = useVideoEditorStore((s) => s.flush);

  const [exporting, setExporting] = useState(false);
  const [exportErr, setExportErr] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const status = render?.composition_status;
  // The last export is out of date if the timeline has changed since it ran.
  const staleExport = status === "ready" && rev > (render?.rendered_rev ?? -1);

  async function onExport() {
    if (!projectId || exporting) return;
    setExportErr(null);
    setExporting(true);
    try {
      await flush(); // make sure the latest timeline is persisted first
      const updated = await api.yasProducer.videoEditor.export(projectId);
      setRender(updated.render);
    } catch (e) {
      setExportErr(e instanceof Error ? e.message : "Couldn't make the video.");
    } finally {
      setExporting(false);
    }
  }

  // Roadmap "Finish & continue →": persist, kick the render (unless a fresh one
  // is already ready), then advance to Finalization immediately. Finalization
  // shows a "rendering…" placeholder and swaps in the MP4 when the poller marks
  // it ready. If the export kickoff itself fails, surface it and DON'T advance.
  async function onFinish() {
    if (!projectId || !onContinue || exporting) return;
    setExportErr(null);
    setExporting(true);
    try {
      await flush();
      const alreadyFresh = status === "ready" && !staleExport;
      if (!alreadyFresh) {
        const updated = await api.yasProducer.videoEditor.export(projectId);
        setRender(updated.render);
      }
      onContinue({
        refId: projectId,
        mediaType: "video",
        mediaUrl: api.yasProducer.videoEditor.compositionFileUrl(projectId),
        topic: title || "Untitled project",
      });
    } catch (e) {
      setExportErr(e instanceof Error ? e.message : "Couldn't make the video.");
    } finally {
      setExporting(false);
    }
  }

  // Poll export status while a render is in flight.
  useEffect(() => {
    if (status !== "rendering" || !projectId) {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
      return;
    }
    if (pollRef.current) return;
    pollRef.current = setInterval(async () => {
      try {
        const p = await api.yasProducer.videoEditor.fetch(projectId);
        setRender(p.render);
      } catch {
        /* transient */
      }
    }, 4000);
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [status, projectId, setRender]);

  return (
    <div className={inRoadmap ? "flex flex-col h-full" : "flex flex-col h-[calc(100vh-64px)] lg:h-[calc(100vh-24px)]"}>
      <EditorTopBar
        onBack={onBack}
        onExport={onExport}
        exportDisabled={exporting || status === "rendering" || clips.length === 0}
        inRoadmap={inRoadmap}
        onFinish={inRoadmap ? onFinish : undefined}
        finishDisabled={exporting || clips.length === 0}
      />

      {/* Export status banner */}
      {(status === "rendering" || status === "ready" || status === "failed" || exportErr) && (
        <div className="shrink-0 mt-2">
          {staleExport && !exporting ? (
            <div className="rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-[12px] text-amber-700 flex items-center gap-2">
              ✎ You have changed things since the last video. Save it again to bring it up to date.
            </div>
          ) : (
            <ExportBanner
              status={exportErr ? "failed" : status}
              error={exportErr || render?.composition_error || null}
              href={status === "ready" && projectId ? api.yasProducer.videoEditor.compositionFileUrl(projectId) : null}
            />
          )}
        </div>
      )}

      <div className="flex-1 min-h-0 flex gap-2 pt-2">
        {/* Left group: [sidebar | preview] on top, the FULL-WIDTH timeline below
            (it widens left under the sidebar; the sidebar is now only as tall as
            the preview). The AI chat stays full-height on the right. */}
        <div className="flex-1 min-w-0 flex flex-col gap-2">
          <div className="flex-1 min-h-0 flex gap-2">
            <aside className="shrink-0">
              <SidebarShell />
            </aside>
            <main className="flex-1 min-w-0 min-h-0">
              <Suspense
                fallback={
                  /* The same black stage PreviewPane paints, so the slot does
                     not flash a white box on the way in. This is the only
                     module that pulls the video player, which is why it is
                     loaded separately, but the reader should not be able to
                     tell that from looking at it. */
                  <div
                    className="w-full h-full rounded-xl bg-black/90 flex items-center justify-center"
                    aria-busy="true"
                    aria-live="polite"
                  >
                    <p className="text-2xs text-white/45">Loading preview…</p>
                  </div>
                }
              >
                <PreviewPane />
              </Suspense>
            </main>
          </div>
          <div className="h-56 shrink-0">
            <Timeline />
          </div>
        </div>

        <aside className="w-80 shrink-0">
          <RightSidebar />
        </aside>
      </div>
    </div>
  );
}

function ExportBanner({
  status,
  error,
  href,
}: {
  status: string | null | undefined;
  error: string | null;
  href: string | null;
}) {
  if (status === "rendering") {
    return (
      <div className="rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-[12px] text-amber-700 flex items-center gap-2">
        <span className="animate-pulse">⏳</span> Making your video… you can carry on working while it runs.
      </div>
    );
  }
  if (status === "ready") {
    return (
      <div className="rounded-lg border border-success/30 bg-success/10 px-3 py-2 text-[12px] text-emerald-700 flex items-center gap-2">
        ✅ Your video is ready.
        {href && (
          <a href={href} target="_blank" rel="noreferrer" download className="underline font-medium">
            Open or download
          </a>
        )}
      </div>
    );
  }
  if (status === "failed") {
    return (
      <div className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-[12px] text-danger">
        Couldn't make the video{error ? `: ${error}` : "."}
      </div>
    );
  }
  return null;
}
