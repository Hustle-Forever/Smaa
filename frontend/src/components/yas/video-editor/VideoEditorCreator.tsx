/**
 * Video Editor · top-level creator
 *
 * Mounted by YasProducer when the "Video Editor" category is chosen or a
 * video-editor project is resumed from the Library. Mirrors the Human Element Video
 * creator contract: `{ onBack, initialRunId }`, hydrate-from-run when resuming.
 *
 *   - No initialRunId  → CreateProjectGate (name + aspect) → editor.
 *   - initialRunId set  → fetch the project, load the store, open the editor.
 *
 * The live document lives in `useVideoEditorStore`; edits autosave (debounced),
 * which is the leave-&-resume mechanism. We flush on Back so the resume is current.
 */
import { useEffect, useState } from "react";

import { api } from "../../../lib/api";
import type { ProducerBrief } from "../../../lib/producerBrief";
import { PRODUCER_HUB_HASH, producerResumeHash, replaceHash } from "../../../lib/producerHash";
import { useVideoEditorStore, useYasLibraryStore } from "../../../lib/store";
import type { VideoEditorProject } from "../../../lib/types";
import type { ContinueToPreview } from "../roadmap/types";
import { CreateProjectGate } from "./CreateProjectGate";
import { EditorShell } from "./EditorShell";

interface Props {
  onBack: () => void;
  initialRunId?: number | null;
  /** When mounted inside the Producer roadmap's Editing stage: the editor's
   *  Export becomes "Finish & continue →" (advances to Finalization via onContinue). */
  inRoadmap?: boolean;
  onContinue?: ContinueToPreview;
  /** Trend Finder "Make this" seed. One-shot, applied at mount only — the
   *  roadmap bumps projectNonce for every hand-off, so this component is always
   *  freshly mounted when a brief arrives. Never set together with
   *  initialRunId (a resume wins; the roadmap clears resumeRunId first). */
  initialBrief?: ProducerBrief | null;
}

export function VideoEditorCreator({ onBack, initialRunId = null, inRoadmap = false, onContinue, initialBrief = null }: Props) {
  const loaded = useVideoEditorStore((s) => s.loaded);
  const load = useVideoEditorStore((s) => s.load);
  const reset = useVideoEditorStore((s) => s.reset);
  const flush = useVideoEditorStore((s) => s.flush);

  const [hydrating, setHydrating] = useState<boolean>(!!initialRunId);
  const [error, setError] = useState<string | null>(null);

  const onCreated = (p: VideoEditorProject) => {
    load(p);
    // Write the deep link so a browser reload restores this project.
    replaceHash(producerResumeHash("video_editor", p.id));
  };

  async function hydrate(id: number) {
    setHydrating(true);
    setError(null);
    try {
      const p = await api.yasProducer.videoEditor.fetch(id);
      load(p);
      // Keep the deep link current so a browser reload restores this project.
      replaceHash(producerResumeHash("video_editor", p.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load project");
    } finally {
      setHydrating(false);
    }
  }

  useEffect(() => {
    if (initialRunId == null) {
      // Fresh entry from the chooser — clean slate so a stale doc never leaks in.
      reset();
      setHydrating(false);
      return;
    }
    void hydrate(initialRunId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialRunId]);

  // Flush pending autosave if the tab is closed/reloaded mid-edit.
  useEffect(() => {
    const onBeforeUnload = () => useVideoEditorStore.getState().flushBeacon();
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, []);

  async function handleBack() {
    // Persist any pending edit before leaving so the Library resume is current.
    try {
      await flush();
    } catch {
      /* best-effort */
    }
    // Refresh the Library so this project (and any new export) shows immediately.
    useYasLibraryStore.getState().invalidate();
    // Clear the project deep link so a later reload doesn't reopen this project.
    replaceHash(PRODUCER_HUB_HASH);
    reset();
    onBack();
  }

  if (hydrating) {
    return (
      <div className="flex flex-col h-[calc(100vh-80px)]">
        <header className="shrink-0 flex items-center gap-3 pb-4">
          <button onClick={onBack} className="btn btn-ghost text-sm">
            ← Back
          </button>
          <h2 className="text-lg font-semibold text-fg">Video Editor</h2>
        </header>
        <div className="flex-1 flex items-center justify-center">
          <p className="text-sm text-muted">Loading…</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col h-[calc(100vh-80px)]">
        <header className="shrink-0 flex items-center gap-3 pb-4">
          <button onClick={onBack} className="btn btn-ghost text-sm">
            ← Back
          </button>
          <h2 className="text-lg font-semibold text-fg">Video Editor</h2>
        </header>
        <div className="flex-1 flex items-center justify-center">
          <p className="text-sm text-danger">Couldn’t open project: {error}</p>
        </div>
      </div>
    );
  }

  if (!loaded) {
    return (
      // A Trend Finder brief can only reach the project NAME here: this tool has
      // no brief/topic box anywhere on its entry path (the document lives in the
      // store, and the AI chat is a per-message command against an already-loaded
      // timeline). The edit plan reaches the user via the clipboard instead.
      <CreateProjectGate
        onBack={onBack}
        onCreated={onCreated}
        onResume={(id) => void hydrate(id)}
        initialTitle={initialBrief?.title ?? ""}
      />
    );
  }

  return <EditorShell onBack={handleBack} inRoadmap={inRoadmap} onContinue={onContinue} />;
}
