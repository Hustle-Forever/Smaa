/**
 * Video Editor · create / resume gate
 *
 * Two tabs: "Recent projects" (resume a previous video-editor project) and
 * "New project" (name + aspect → create). Defaults to Recent when any exist.
 * Creating a project up-front inserts a draft row (the resume anchor).
 */
import { useEffect, useMemo, useState } from "react";
import { Clapperboard } from "lucide-react";

import { api } from "../../../lib/api";
import { fmtRel } from "../../../lib/format";
import { useYasLibraryStore } from "../../../lib/store";
import type { VideoEditAspect, VideoEditorProject, YasLibraryItem } from "../../../lib/types";

const ASPECTS: {
  key: VideoEditAspect;
  label: string;
  dims: string;
  box: { w: number; h: number };
  hint: string;
}[] = [
  { key: "9:16", label: "Tall", dims: "9:16 · 1080 × 1920", box: { w: 30, h: 54 },
    hint: "Reels, TikTok, Stories" },
  { key: "16:9", label: "Wide", dims: "16:9 · 1920 × 1080", box: { w: 54, h: 30 },
    hint: "YouTube, a screen" },
  { key: "1:1", label: "Square", dims: "1:1 · 1080 × 1080", box: { w: 42, h: 42 },
    hint: "A feed post" },
];

export function CreateProjectGate({
  onBack,
  onCreated,
  onResume,
  initialTitle,
}: {
  onBack: () => void;
  onCreated: (p: VideoEditorProject) => void;
  onResume: (projectId: number) => void;
  /** Trend Finder "Make this": name the project after the idea. A plain useState
   *  default is enough — the gate only ever mounts once per fresh project. */
  initialTitle?: string;
}) {
  const lib = useYasLibraryStore();
  const [tab, setTab] = useState<"recent" | "new">("new");
  const [tabInit, setTabInit] = useState(false);

  const [title, setTitle] = useState(initialTitle ?? "");
  const [aspect, setAspect] = useState<VideoEditAspect>("9:16");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    lib.fetch({ limit: 300 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const recents = useMemo(
    () =>
      lib.items
        .filter((it) => it.type === "video_editor")
        .sort((a, b) => (b.updated_at || b.created_at || "").localeCompare(a.updated_at || a.created_at || "")),
    [lib.items],
  );

  // Default to Recent on first load if any exist.
  useEffect(() => {
    if (!tabInit && lib.lastFetchedAt) {
      setTab(recents.length > 0 ? "recent" : "new");
      setTabInit(true);
    }
  }, [tabInit, lib.lastFetchedAt, recents.length]);

  async function create() {
    if (creating) return;
    setCreating(true);
    setError(null);
    try {
      const p = await api.yasProducer.videoEditor.create({
        title: title.trim() || "Untitled project",
        aspect,
      });
      onCreated(p);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create project");
      setCreating(false);
    }
  }

  return (
    <div className="flex flex-col h-[calc(100vh-80px)]">
      <header className="shrink-0 flex items-center gap-3 pb-4">
        <button onClick={onBack} className="btn btn-ghost text-sm">
          ← Back
        </button>
        <h2 className="text-lg font-semibold text-fg">Video Editor</h2>
      </header>

      <div className="flex-1 min-h-0 flex items-start justify-center pt-6">
        <div className="w-full max-w-md space-y-4 card p-6">
          {/* Tabs */}
          <div className="flex gap-1.5">
            {(["recent", "new"] as const).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={[
                  "px-3 py-1 rounded-full border text-[13px] font-medium transition-colors",
                  tab === t
                    ? "border-accent bg-accent/8 text-fg"
                    : "border-border bg-bg-subtle text-muted hover:bg-bg-muted",
                ].join(" ")}
              >
                {t === "recent" ? `Carry on${recents.length ? ` (${recents.length})` : ""}` : "Start new"}
              </button>
            ))}
          </div>

          {tab === "recent" ? (
            <div className="space-y-2">
              {lib.loading && recents.length === 0 && <p className="text-sm text-muted py-6 text-center">Loading…</p>}
              {!lib.loading && recents.length === 0 && (
                <div className="text-center py-8 space-y-2">
                  <p className="text-sm text-muted">Nothing here yet.</p>
                  <button onClick={() => setTab("new")} className="btn btn-primary text-sm">
                    Start a new video
                  </button>
                </div>
              )}
              <div className="max-h-[52vh] overflow-y-auto -mx-1 px-1 space-y-1.5">
                {recents.map((it) => (
                  <RecentRow key={it.id} item={it} onResume={() => onResume(it.id)} />
                ))}
              </div>
            </div>
          ) : (
            <div className="space-y-6">
              <div className="space-y-1.5">
                <h1 className="h1 text-xl">Start a new video</h1>
                <p className="text-sm text-muted">Give it a name and pick a shape.</p>
              </div>

              <div className="space-y-1.5">
                <label className="eyebrow">Name</label>
                <input
                  type="text"
                  value={title}
                  autoFocus
                  onChange={(e) => setTitle(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") create();
                  }}
                  placeholder="Name this video"
                  className="w-full rounded-lg border border-border bg-bg-subtle px-3 py-2.5 text-sm focus:outline-none focus:border-accent"
                />
              </div>

              <div className="space-y-2">
                <label className="eyebrow">Shape</label>
                <div className="grid grid-cols-3 gap-2">
                  {ASPECTS.map((a) => (
                    <button
                      key={a.key}
                      type="button"
                      onClick={() => setAspect(a.key)}
                      title={a.dims}
                      className={[
                        "rounded-lg border p-3 flex flex-col items-center gap-2 transition-colors",
                        aspect === a.key
                          ? "border-accent bg-accent/8 text-fg"
                          : "border-border bg-bg-subtle text-muted hover:bg-bg-muted",
                      ].join(" ")}
                    >
                      <span className="flex items-center justify-center h-14">
                        <span
                          className={["rounded-sm border-2", aspect === a.key ? "border-accent" : "border-border"].join(" ")}
                          style={{ width: a.box.w, height: a.box.h }}
                        />
                      </span>
                      <span className="text-[12px] font-medium">{a.label}</span>
                      <span className="text-3xs text-muted">{a.hint}</span>
                    </button>
                  ))}
                </div>
              </div>

              {error && <p className="text-[12px] text-danger">{error}</p>}

              <div className="flex items-center justify-end gap-2 pt-1">
                <button onClick={onBack} className="btn btn-ghost text-sm">
                  Cancel
                </button>
                <button onClick={create} disabled={creating} className="btn btn-primary text-sm px-5">
                  {creating ? "Setting up…" : "Start editing"}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function RecentRow({ item, onResume }: { item: YasLibraryItem; onResume: () => void }) {
  const [imgErr, setImgErr] = useState(false);
  const finalVideo = item.has_video && item.video_url ? item.video_url : null;
  const previewVideoUrl = finalVideo || item.preview_url || null;
  const showStill = !previewVideoUrl && !!item.thumbnail_url && !imgErr;
  return (
    <button
      onClick={onResume}
      className="w-full flex items-center gap-3 p-2 rounded-lg border border-border bg-bg hover:bg-bg-muted text-left transition-colors group"
    >
      <div className="w-20 h-12 shrink-0 rounded-md overflow-hidden bg-black/80 flex items-center justify-center">
        {previewVideoUrl ? (
          <video src={previewVideoUrl} poster={item.thumbnail_url ?? undefined} muted preload="metadata" className="w-full h-full object-cover" />
        ) : showStill ? (
          <img src={item.thumbnail_url!} alt="" onError={() => setImgErr(true)} className="w-full h-full object-cover" />
        ) : (
          <Clapperboard className="w-4 h-4 text-white/40" strokeWidth={1.5} />
        )}
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-[13px] text-fg/90 truncate">{item.topic || "Untitled"}</p>
        <p className="text-[10.5px] text-muted">
          {fmtRel(item.updated_at || item.created_at)}
          {item.composition_status === "ready" ? " · video ready" : ""}
        </p>
      </div>
      <span className="text-2xs text-muted opacity-0 group-hover:opacity-100 transition-opacity pr-1">Open →</span>
    </button>
  );
}
