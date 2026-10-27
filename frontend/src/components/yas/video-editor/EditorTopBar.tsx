/**
 * Video Editor · top bar
 *
 * Back, inline title rename, aspect badge, autosave status, and the Export
 * button. Reads/writes the live editor store. Export is wired in Phase B.
 */
import { api } from "../../../lib/api";
import { useVideoEditorStore } from "../../../lib/store";

// The exact pixels live in the tooltip; the badge says the thing people
// actually chose.
const SHAPE_LABEL: Record<string, string> = {
  "9:16": "Tall",
  "16:9": "Wide",
  "1:1": "Square",
};

function SaveStatus() {
  const saveState = useVideoEditorStore((s) => s.saveState);
  const map: Record<string, { label: string; cls: string }> = {
    idle: { label: "Saved", cls: "text-muted" },
    dirty: { label: "Not saved yet…", cls: "text-amber-600" },
    saving: { label: "Saving…", cls: "text-amber-600" },
    saved: { label: "All saved", cls: "text-emerald-600" },
    error: { label: "Couldn't save, trying again", cls: "text-danger" },
  };
  const s = map[saveState] || map.idle;
  return <span className={`text-2xs font-medium ${s.cls}`}>{s.label}</span>;
}

export function EditorTopBar({
  onBack,
  onExport,
  exportDisabled,
  inRoadmap = false,
  onFinish,
  finishDisabled,
}: {
  onBack: () => void;
  onExport?: () => void;
  exportDisabled?: boolean;
  /** Inside the roadmap, Export is replaced by "Finish & continue →". */
  inRoadmap?: boolean;
  onFinish?: () => void;
  finishDisabled?: boolean;
}) {
  const title = useVideoEditorStore((s) => s.title);
  const project = useVideoEditorStore((s) => s.project);
  const setTitle = useVideoEditorStore((s) => s.setTitle);

  // Re-fetch the current project from the server and reload the store in place
  // (no navigation). Flushes pending edits first so nothing is lost.
  const reloadProject = async () => {
    const st = useVideoEditorStore.getState();
    const pid = st.projectId;
    if (pid == null) return;
    try {
      await st.flush();
      const p = await api.yasProducer.videoEditor.fetch(pid);
      useVideoEditorStore.getState().load(p);
    } catch {
      /* ignore */
    }
  };

  return (
    <header className="shrink-0 flex items-center gap-3 px-1 pb-3 border-b border-border">
      <button onClick={onBack} className="btn btn-ghost text-sm">
        ← Back
      </button>

      <button
        onClick={() => void reloadProject()}
        className="btn btn-ghost text-sm"
        title="Load the latest version of this project"
      >
        ⟳ Reload
      </button>

      <input
        type="text"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Name this video"
        className="min-w-0 flex-1 max-w-sm bg-transparent text-sm font-semibold text-fg px-2 py-1 rounded-md hover:bg-bg-subtle focus:bg-bg-subtle focus:outline-none focus:border-accent border border-transparent"
      />

      {project && (
        <span
          className="text-3xs px-1.5 py-0.5 rounded bg-cyan-500/15 text-cyan-600"
          title={`${project.aspect} · ${project.width}×${project.height}`}
        >
          {SHAPE_LABEL[project.aspect] || project.aspect}
        </span>
      )}

      <div className="flex-1" />

      <SaveStatus />

      {inRoadmap ? (
        <button
          onClick={onFinish}
          disabled={finishDisabled || !onFinish}
          className="btn btn-accent text-sm px-4"
          title="Make the video and move on to the next step"
        >
          Make my video →
        </button>
      ) : (
        <button
          onClick={onExport}
          disabled={exportDisabled || !onExport}
          className="btn btn-primary text-sm px-4"
          title={!onExport ? "Not ready yet" : "Turn this into a video file you can download"}
        >
          Save as a video
        </button>
      )}
    </header>
  );
}
