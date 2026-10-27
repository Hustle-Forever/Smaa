/**
 * B-roll creator (Ideation → General video → B-roll).
 *
 * A dedicated, non-technical tool (NO timeline UI). Two columns: LEFT = the setup
 * (drop clips, format, intro/outro, music, "Create & auto-edit"); RIGHT = the live
 * preview (empty → progress → the AI-ordered clip strip you can drag to reorder,
 * each downloadable). Under the hood it drives a headless Video Editor project
 * (preset "b_roll") through the existing B-roll engine + Remotion export. Finalise
 * stitches the result into one video (advances the roadmap) or opens it in the
 * editor.
 */
import { useEffect, useRef, useState } from "react";

import { api } from "../../lib/api";
import type { ProducerBrief } from "../../lib/producerBrief";
import {
  aiStatusLabel,
  useVideoEditorStore,
  useVideoSourcesStore,
} from "../../lib/store";
import type { VideoEditAspect, VideoEditClip, VideoSource } from "../../lib/types";
import type { ContinueToPreview } from "./roadmap/types";
import { SegmentedControl } from "./SegmentedControl";

interface Props {
  onBack: () => void;
  onContinue?: ContinueToPreview;
  onOpenInEditor?: (projectId: number) => void;
  initialRunId?: number | null;
  onStep?: (step: string) => void;
  initialStep?: string | null;
  /** Trend Finder "Make this" seed. One-shot, applied at mount only — the
   *  roadmap bumps projectNonce for every hand-off, so this component is always
   *  freshly mounted when a brief arrives. Never set together with
   *  initialRunId (a resume wins; the roadmap clears resumeRunId first). */
  initialBrief?: ProducerBrief | null;
}

const stripStatus = (e: unknown) =>
  (e instanceof Error ? e.message : String(e)).replace(/^\d{3}\s+/, "");

const UPLOAD_CONCURRENCY = 4;
type IntroOutroMode = "none" | "generate" | "upload";
type Stage = "setup" | "running" | "result";

// Plain words up front, the ratio only as a hover hint: people picking a shape
// are thinking "for Reels" or "for YouTube", not in numbers.
const ASPECTS: { key: VideoEditAspect; label: string; hint: string }[] = [
  { key: "9:16", label: "Tall", hint: "9:16 · Reels, TikTok, Stories" },
  { key: "16:9", label: "Wide", hint: "16:9 · YouTube, a screen" },
  { key: "1:1", label: "Square", hint: "1:1 · a feed post" },
];

function sourceKindId(clip: VideoEditClip): { kind: string; id: number } | null {
  const s = clip.source;
  if (!s) return null;
  const id = s.upload_id ?? s.animation_id ?? s.composition_id ?? null;
  if (id == null) return null;
  return { kind: s.kind, id };
}

export function BrollCreator({ onBack, onContinue, onOpenInEditor, initialRunId = null, onStep, initialStep = null, initialBrief = null }: Props) {
  const [stage, setStage] = useState<Stage>("setup");
  // When resuming from the Library ("Continue editing"), load the saved b_roll
  // project into the editor store and jump straight to the result view.
  const [hydrating, setHydrating] = useState<boolean>(initialRunId != null);

  const [clips, setClips] = useState<File[]>([]);
  const [aspect, setAspect] = useState<VideoEditAspect>("9:16");
  const [mode, setMode] = useState<IntroOutroMode>("generate");
  const [introTopic, setIntroTopic] = useState("");
  const [outroTopic, setOutroTopic] = useState("");
  const [introFile, setIntroFile] = useState<File | null>(null);
  const [outroFile, setOutroFile] = useState<File | null>(null);
  const [music, setMusic] = useState(true);
  const [dragging, setDragging] = useState(false);
  const clipInputRef = useRef<HTMLInputElement | null>(null);

  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState("");
  const [prog, setProg] = useState<{ done: number; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [reordering, setReordering] = useState(false);
  const [finalising, setFinalising] = useState(false);
  const [finalMsg, setFinalMsg] = useState<string | null>(null);
  const [previewClipId, setPreviewClipId] = useState<string | null>(null);
  const dragIdx = useRef<number | null>(null);

  const aiBusy = useVideoEditorStore((s) => s.aiBusy);
  const aiJob = useVideoEditorStore((s) => s.aiJob);
  const aiError = useVideoEditorStore((s) => s.aiError);
  const storeClips = useVideoEditorStore((s) => s.clips);
  const projectId = useVideoEditorStore((s) => s.projectId);

  // Trend Finder "Make this" — this tool's real input is the user's own footage,
  // so the only text boxes here are the intro/outro card lines; they're what the
  // brief can fill (the AI's shot list rides the clipboard instead). Mount-only:
  // the roadmap remounts this creator (projectNonce) for every hand-off.
  useEffect(() => {
    if (!initialBrief || initialRunId != null) return;
    if (initialBrief.introTopic) setIntroTopic(initialBrief.introTopic);
    if (initialBrief.outroTopic) setOutroTopic(initialBrief.outroTopic);
    // "generate" is already the default, but it's what gates those inputs — set
    // it explicitly so the seeded lines stay visible if that default ever moves.
    if (initialBrief.introTopic || initialBrief.outroTopic) setMode("generate");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (stage !== "running") return;
    if (aiError) {
      setError(stripStatus(aiError));
      setStage("setup");
      setBusy(false);
      return;
    }
    if (!aiBusy && aiJob?.status === "done") {
      setStage("result");
      setBusy(false);
    }
  }, [stage, aiBusy, aiJob, aiError]);

  // Resume from Library: hydrate the saved b_roll project into the editor store
  // and show the result view (clips strip + preview), so "Continue editing" works.
  useEffect(() => {
    if (initialRunId == null) return;
    let cancelled = false;
    (async () => {
      try {
        const p = await api.yasProducer.videoEditor.fetch(initialRunId);
        if (cancelled) return;
        useVideoEditorStore.getState().load(p);
        // Deep-link may pin an exact sub-step; otherwise land on the result view.
        setStage(
          initialStep === "setup" || initialStep === "running" || initialStep === "result"
            ? initialStep
            : "result",
        );
      } catch (e) {
        if (!cancelled) setError(stripStatus(e));
      } finally {
        if (!cancelled) setHydrating(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Report the live sub-step to the roadmap so it can encode it in the URL hash.
  // Only once a project exists (created / hydrated), matching the onRunId pattern.
  useEffect(() => {
    if (projectId != null) onStep?.(stage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, projectId]);

  function addClips(list: FileList | null) {
    if (!list) return;
    const incoming = Array.from(list).filter(
      (f) => f.type.startsWith("video/") || /\.(mp4|mov|m4v|webm|avi|mkv)$/i.test(f.name),
    );
    setClips((prev) => {
      const out = [...prev];
      for (const f of incoming) {
        if (!out.some((x) => x.name === f.name && x.size === f.size)) out.push(f);
      }
      return out;
    });
  }

  async function uploadAndSeed(file: File): Promise<VideoSource> {
    const src = await api.yasProducer.videoEditor.upload(file);
    useVideoSourcesStore.getState().addItem(src);
    useVideoEditorStore.getState().addClipFromSource(src);
    return src;
  }

  async function createAndEdit() {
    if (busy) return;
    if (clips.length === 0) {
      setError("Add at least one clip first.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      setPhase("Getting set up…");
      const p = await api.yasProducer.videoEditor.create({
        title: introTopic.trim() || "B-roll",
        aspect,
        preset: "b_roll",
      });
      useVideoEditorStore.getState().load(p);

      setPhase("Sending your clips over…");
      setProg({ done: 0, total: clips.length });
      let idx = 0;
      let done = 0;
      const failed: string[] = [];
      const worker = async () => {
        while (idx < clips.length) {
          const f = clips[idx++];
          try {
            await uploadAndSeed(f);
          } catch {
            failed.push(f.name);
          } finally {
            done++;
            setProg({ done, total: clips.length });
          }
        }
      };
      await Promise.all(Array.from({ length: Math.min(UPLOAD_CONCURRENCY, clips.length) }, worker));
      if (done - failed.length === 0) throw new Error("every clip failed to upload");

      let introUploadId: number | null = null;
      let outroUploadId: number | null = null;
      if (mode === "upload") {
        setPhase("Sending your opening and ending over…");
        if (introFile) {
          const s = await uploadAndSeed(introFile);
          if (s.kind === "upload") introUploadId = s.id;
        }
        if (outroFile) {
          const s = await uploadAndSeed(outroFile);
          if (s.kind === "upload") outroUploadId = s.id;
        }
      }
      const wantBookends =
        mode === "generate" || (mode === "upload" && (introUploadId != null || outroUploadId != null));

      setPhase("Starting the edit…");
      await useVideoEditorStore.getState().runAi("broll", {
        introOutro: wantBookends,
        bookendMode: mode === "upload" ? "upload" : "generate",
        withMusic: music,
        introTopic: introTopic.trim() || undefined,
        outroTopic: outroTopic.trim() || undefined,
        introUploadId,
        outroUploadId,
      });
      setStage("running");
    } catch (e) {
      setError(stripStatus(e));
      setBusy(false);
      setPhase("");
      setProg(null);
    }
  }

  // ----- result: orderable main-track video clips -----
  const videoClips = storeClips.filter((c) => (c.kind ?? "video") === "video" && c.source);
  const trackCounts = new Map<string, number>();
  for (const c of videoClips) trackCounts.set(c.trackId, (trackCounts.get(c.trackId) ?? 0) + 1);
  let mainTrack: string | null = null;
  let best = -1;
  for (const [t, n] of trackCounts) if (n > best) { best = n; mainTrack = t; }
  const orderedClips = videoClips
    .filter((c) => c.trackId === mainTrack)
    .sort((a, b) => a.from - b.from);

  // The clip shown in the big preview pane (defaults to the first / intro clip).
  const activePreviewId =
    previewClipId && orderedClips.some((c) => c.id === previewClipId)
      ? previewClipId
      : orderedClips[0]?.id ?? null;
  const _activeClip = orderedClips.find((c) => c.id === activePreviewId) || null;
  const _activeSid = _activeClip ? sourceKindId(_activeClip) : null;
  const previewSrc = _activeSid
    ? api.yasProducer.videoEditor.sourceVideoUrl(_activeSid.kind, _activeSid.id)
    : null;

  async function persistOrder(newOrder: VideoEditClip[]) {
    if (!projectId) return;
    setReordering(true);
    setError(null);
    try {
      const doc = await api.yasProducer.videoEditor.brollReorder(projectId, newOrder.map((c) => c.id));
      useVideoEditorStore.getState().applyServerDoc(doc);
    } catch (e) {
      setError(stripStatus(e));
    } finally {
      setReordering(false);
    }
  }

  function onDrop(targetIdx: number) {
    const from = dragIdx.current;
    dragIdx.current = null;
    if (from == null || from === targetIdx) return;
    const next = orderedClips.slice();
    const [moved] = next.splice(from, 1);
    next.splice(targetIdx, 0, moved);
    void persistOrder(next);
  }

  async function finaliseStitch() {
    if (!projectId) return;
    setFinalising(true);
    setError(null);
    setFinalMsg("Making the final video…");
    try {
      await api.yasProducer.videoEditor.export(projectId);
      const mediaUrl = api.yasProducer.videoEditor.compositionFileUrl(projectId);
      const start = Date.now();
      // 'failed' can be a TRANSIENT flag set by another backend on the shared DB;
      // this stack reclaims + self-heals it to 'ready'. Don't bail on the first
      // 'failed' — keep polling and only give up after a grace window.
      let failedSince: number | null = null;
      while (Date.now() - start < 10 * 60 * 1000) {
        await new Promise((r) => setTimeout(r, 5000));
        const proj = await api.yasProducer.videoEditor.fetch(projectId);
        const st = proj.render?.composition_status;
        if (st === "ready") {
          setFinalMsg(null);
          setFinalising(false);
          onContinue?.({
            refId: projectId,
            mediaType: "video",
            mediaUrl,
            topic: introTopic.trim() || "B-roll",
          });
          return;
        }
        if (st === "failed") {
          if (failedSince == null) failedSince = Date.now();
          setFinalMsg("Finishing up…");
          if (Date.now() - failedSince > 120000) {
            throw new Error(proj.render?.composition_error || "Couldn't make the video.");
          }
        } else {
          failedSince = null;
          setFinalMsg("Making the final video…");
        }
      }
      throw new Error("That took too long. Try again.");
    } catch (e) {
      setError(stripStatus(e));
      setFinalMsg(null);
      setFinalising(false);
    }
  }

  function handleBack() {
    void useVideoEditorStore.getState().flush().catch(() => {});
    useVideoEditorStore.getState().reset();
    onBack();
  }

  const canSubmit = clips.length > 0 && !busy && stage === "setup";

  return (
    <div className="flex flex-col h-[calc(100vh-150px)] overflow-y-auto">
      <header className="shrink-0 flex items-center gap-3 pb-4">
        <button onClick={handleBack} className="btn btn-ghost text-sm">← Back</button>
        <h2 className="text-lg font-semibold text-fg">B-roll</h2>
        <span className="text-xs text-muted">Drop in your clips and we put them together for you</span>
      </header>

      {hydrating ? (
        <div className="flex-1 flex flex-col items-center justify-center gap-3 text-center">
          <span className="w-6 h-6 rounded-full border-2 border-accent border-t-transparent animate-spin" />
          <p className="text-sm text-muted">Loading…</p>
        </div>
      ) : (
      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)] gap-5 items-start">
        {/* ---------- LEFT: setup ---------- */}
        <section className="card p-4 space-y-5 lg:sticky lg:top-4">
          <div className="space-y-2">
            <span className="eyebrow">1 · Your clips</span>
            <label
              className={[
                "block rounded-xl border-2 border-dashed p-5 text-center transition-colors",
                busy || stage !== "setup" ? "border-border opacity-60" : "cursor-pointer",
                dragging ? "border-accent bg-accent/8" : "border-border hover:border-accent/60 hover:bg-bg-muted",
              ].join(" ")}
              onDragOver={(e) => { e.preventDefault(); if (stage === "setup" && !busy) setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => { e.preventDefault(); setDragging(false); if (stage === "setup" && !busy) addClips(e.dataTransfer.files); }}
            >
              <input
                ref={clipInputRef}
                type="file"
                multiple
                accept="video/*"
                className="hidden"
                disabled={busy || stage !== "setup"}
                onChange={(e) => { addClips(e.currentTarget.files); e.currentTarget.value = ""; }}
              />
              <p className="text-[12px] font-medium text-fg">Drop videos here, or click to browse</p>
              <p className="text-3xs text-muted pt-0.5">As many as you like. We put them in order for you.</p>
            </label>
            {clips.length > 0 && (
              <div className="flex items-center justify-between">
                <span className="text-2xs text-muted">{clips.length} clip{clips.length === 1 ? "" : "s"} added</span>
                {stage === "setup" && !busy && (
                  <button onClick={() => setClips([])} className="text-2xs text-muted hover:text-danger">Clear</button>
                )}
              </div>
            )}
          </div>

          <div className="space-y-2">
            <span className="eyebrow">2 · Shape</span>
            <SegmentedControl
              variant="pill"
              options={ASPECTS.map((a) => ({ value: a.key, label: a.label, title: a.hint }))}
              value={aspect}
              onChange={(v) => setAspect(v)}
              disabled={busy || stage !== "setup"}
            />
          </div>

          <div className="space-y-2">
            <span className="eyebrow">3 · Start and end</span>
            <div className="grid grid-cols-3 gap-1.5">
              {([
                { key: "none", label: "None" },
                { key: "generate", label: "Make one" },
                { key: "upload", label: "Use my own" },
              ] as const).map((o) => (
                <button
                  key={o.key}
                  disabled={busy || stage !== "setup"}
                  onClick={() => setMode(o.key)}
                  className={[
                    "rounded-lg border px-2 py-1.5 text-[11.5px] font-medium transition-colors disabled:opacity-50",
                    mode === o.key ? "border-accent bg-accent/8 text-fg" : "border-border bg-bg-subtle text-muted hover:bg-bg-muted",
                  ].join(" ")}
                >
                  {o.label}
                </button>
              ))}
            </div>
            {mode === "generate" && (
              <div className="space-y-1.5 pt-1">
                <input
                  value={introTopic}
                  disabled={busy || stage !== "setup"}
                  onChange={(e) => setIntroTopic(e.target.value)}
                  placeholder="What the opening should say, e.g. AI hackathon at 42 Abu Dhabi"
                  className="w-full rounded-lg border border-border bg-bg-subtle text-[12px] px-3 py-2 focus:outline-none focus:border-accent"
                />
                <input
                  value={outroTopic}
                  disabled={busy || stage !== "setup"}
                  onChange={(e) => setOutroTopic(e.target.value)}
                  placeholder="What the ending should say, e.g. Follow for more"
                  className="w-full rounded-lg border border-border bg-bg-subtle text-[12px] px-3 py-2 focus:outline-none focus:border-accent"
                />
                <p className="text-3xs text-muted">Short animated openers. Leave them blank and we write them from your clips.</p>
              </div>
            )}
            {mode === "upload" && (
              <div className="grid grid-cols-2 gap-2 pt-1">
                <BookendSlot label="Opening clip" file={introFile} onPick={setIntroFile} disabled={busy || stage !== "setup"} />
                <BookendSlot label="Closing clip" file={outroFile} onPick={setOutroFile} disabled={busy || stage !== "setup"} />
              </div>
            )}
          </div>

          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" checked={music} disabled={busy || stage !== "setup"} onChange={(e) => setMusic(e.target.checked)} />
            <span className="text-[12px] text-fg/85">Add music (your clips go silent)</span>
          </label>

          {error && <p className="text-[12px] text-danger">{error}</p>}

          {stage === "setup" ? (
            <button onClick={() => void createAndEdit()} disabled={!canSubmit} className="btn btn-primary text-sm w-full disabled:opacity-50">
              ✦ Make my video
            </button>
          ) : (
            <p className="text-2xs text-muted">On it. Watch the preview on the right.</p>
          )}
        </section>

        {/* ---------- RIGHT: preview ---------- */}
        <div className="space-y-4 min-w-0">
          {stage === "setup" && (
            <div className="card p-10 text-center text-sm text-muted">
              Your video will appear here. Add some clips, then press <span className="text-fg">Make my video</span>.
            </div>
          )}

          {stage === "running" && (
            <div className="card p-10 flex flex-col items-center justify-center gap-3 text-center">
              <span className="w-6 h-6 rounded-full border-2 border-accent border-t-transparent animate-spin" />
              <p className="text-sm text-fg">{aiStatusLabel(aiJob)}</p>
              <p className="text-2xs text-muted">{phase || "Putting your clips in order and adding music…"}{prog && prog.total > 0 ? ` · ${prog.done}/${prog.total}` : ""}</p>
            </div>
          )}

          {stage === "result" && (
            <div className="space-y-3">
              {/* Big preview — plays the selected clip. The stitched final (with
                  music + bookends) is produced on Finalise. */}
              <div
                className="rounded-xl overflow-hidden bg-black w-full flex items-center justify-center"
                style={{ height: "min(56vh, 460px)" }}
              >
                {previewSrc ? (
                  <video
                    key={previewSrc}
                    src={previewSrc}
                    controls
                    playsInline
                    className="max-h-full max-w-full object-contain"
                  />
                ) : (
                  <span className="text-white/45 text-sm">Nothing to play yet</span>
                )}
              </div>

              <div className="flex items-center justify-between">
                <h3 className="eyebrow">Your clips · tap to play · drag to reorder</h3>
                <span className="text-2xs text-muted">{reordering ? "Saving the order…" : `${orderedClips.length} clip${orderedClips.length === 1 ? "" : "s"}`}</span>
              </div>

              <div className="grid grid-cols-3 md:grid-cols-4 gap-2.5">
                {orderedClips.map((c, i) => {
                  const sid = sourceKindId(c);
                  const thumb = sid ? api.yasProducer.videoEditor.sourceThumbUrl(sid.kind, sid.id) : null;
                  const dl = sid ? api.yasProducer.videoEditor.sourceVideoUrl(sid.kind, sid.id) : null;
                  const isActive = c.id === activePreviewId;
                  return (
                    <div
                      key={c.id}
                      draggable
                      onDragStart={() => (dragIdx.current = i)}
                      onDragOver={(e) => e.preventDefault()}
                      onDrop={() => onDrop(i)}
                      className={[
                        "card p-1.5 space-y-1.5 cursor-grab active:cursor-grabbing transition-shadow",
                        isActive ? "ring-2 ring-accent" : "",
                      ].join(" ")}
                    >
                      <button
                        type="button"
                        onClick={() => setPreviewClipId(c.id)}
                        title="Play this clip"
                        className="group relative block w-full aspect-video rounded-md overflow-hidden bg-black/80"
                      >
                        {thumb ? (
                          <img src={thumb} alt="" className="w-full h-full object-cover" />
                        ) : (
                          <span className="absolute inset-0 flex items-center justify-center text-2xs text-muted">clip</span>
                        )}
                        <span className="absolute top-1 left-1 w-5 h-5 rounded-full bg-black/70 text-white text-3xs flex items-center justify-center">{i + 1}</span>
                        <span className="absolute inset-0 flex items-center justify-center bg-black/0 group-hover:bg-black/25 transition-colors">
                          <span className="opacity-0 group-hover:opacity-100 text-white text-lg transition-opacity">▶</span>
                        </span>
                      </button>
                      {dl && (
                        <a href={dl} download className="block text-center btn btn-ghost text-2xs py-0.5">↓ Download</a>
                      )}
                    </div>
                  );
                })}
              </div>

              <section className="sticky bottom-0 bg-bg/90 backdrop-blur border-t border-border py-3 flex items-center gap-3">
                <span className="text-[13px] text-muted">{finalMsg ?? "Clips gone silent, music and the opening and ending added"}</span>
                <div className="ml-auto flex items-center gap-2">
                  <button onClick={() => projectId && onOpenInEditor?.(projectId)} disabled={finalising || !projectId} className="btn btn-ghost text-sm">
                    Open in video editor
                  </button>
                  {onContinue && (
                    <button onClick={() => void finaliseStitch()} disabled={finalising} className="btn btn-accent text-sm px-5">
                      {finalising ? "Joining…" : "Join into one video →"}
                    </button>
                  )}
                </div>
              </section>
            </div>
          )}
        </div>
      </div>
      )}
    </div>
  );
}

function BookendSlot({
  label,
  file,
  onPick,
  disabled,
}: {
  label: string;
  file: File | null;
  onPick: (f: File | null) => void;
  disabled: boolean;
}) {
  const ref = useRef<HTMLInputElement | null>(null);
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => ref.current?.click()}
      className="rounded-lg border border-border bg-bg-subtle px-3 py-2.5 text-left hover:bg-bg-muted transition-colors disabled:opacity-50"
    >
      <span className="block text-3xs text-muted">{label}</span>
      <span className="block text-[11.5px] text-fg truncate">{file ? file.name : "Pick a video…"}</span>
      <input
        ref={ref}
        type="file"
        accept="video/*"
        className="hidden"
        disabled={disabled}
        onChange={(e) => { onPick(e.currentTarget.files?.[0] ?? null); e.currentTarget.value = ""; }}
      />
    </button>
  );
}
