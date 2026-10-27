/**
 * General AI Video creator (Ideation → General video → General AI Video).
 *
 * A clean (unbranded) video generator. Two columns: LEFT = inputs (references
 * picked from Assets or uploaded + a free-form prompt + engine/format/length/sound),
 * RIGHT = preview/results (the session's stills & clips, each with Generate-video
 * / Download / Include). Generate a still first, then animate it to a video; make
 * one or more per session. Finalise stitches the chosen clips into one video
 * (advances the roadmap) or opens them in the editor.
 *
 * References feed the STILL (Gemini 3 Pro Image, up to 14 refs) — anything goes:
 * drop a background + tell the AI to place kids coding in it, or no refs at all.
 *
 * The still is animated by one of two engines, picked in the form: Veo (the
 * default; 9:16 or 16:9, 4/6/8 s, Normal/High quality) or Seedance 2.5 via
 * Higgsfield (six shapes, 4-30 s, 480p-1080p). The shape is chosen before the
 * still is drawn, because Seedance animates the still in whatever shape it is.
 */
import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";

import { aspectClass as aspectClassFor, SEEDANCE_FORMATS } from "../../lib/aspect";

import { api } from "../../lib/api";
import type { ProducerBrief } from "../../lib/producerBrief";
import { useProducerAssetsStore } from "../../lib/store";
import type {
  YasAnimateDuration,
  YasAnimationAspect,
  YasGeneralVideoEngine,
  YasGeneralVideoEngines,
  YasGeneralVideoGen,
  ProducerAssetRow,
  SeedanceAspect,
  SeedanceResolution,
} from "../../lib/types";
import type { ContinueToPreview } from "./roadmap/types";
import { AssetPickerModal } from "./AssetPickerModal";
import { EnhanceButton } from "./EnhanceButton";
import { SegmentedControl } from "./SegmentedControl";
import { Toggle } from "../Toggle";
import { useConfirm } from "../ConfirmProvider";

interface Props {
  onBack: () => void;
  onContinue?: ContinueToPreview;
  onOpenInEditor?: (projectId: number) => void;
  /** Resume — when set, fetch this generation and surface it in the strip
   *  ("Continue" / shared link). */
  initialRunId?: number | null;
  /** Roadmap: mirror the live run id up to the URL so a hard refresh (F5)
   *  reloads THIS run. Absent standalone. */
  onRunId?: (id: number) => void;
  /** Trend Finder "Make this" seed. One-shot, applied at mount only — the
   *  roadmap bumps projectNonce for every hand-off, so this component is always
   *  freshly mounted when a brief arrives. Never set together with
   *  initialRunId (a resume wins; the roadmap clears resumeRunId first). */
  initialBrief?: ProducerBrief | null;
}

interface RefItem {
  kind: "asset" | "upload";
  id: number;
  thumb: string;
  name: string;
}

const MAX_REFS = 6; // Gemini accepts up to 14; keep it sane for quality + UI.
const ASPECTS: { key: YasAnimationAspect; label: string }[] = [
  { key: "9:16", label: "Vertical 9:16" },
  { key: "16:9", label: "Landscape 16:9" },
];
const DURATIONS: YasAnimateDuration[] = [4, 6, 8];
const VEO_ASPECTS = new Set<SeedanceAspect>(ASPECTS.map((a) => a.key));
// Used until GET /general-video/engines answers (and if it never does).
const SEEDANCE_FALLBACK: YasGeneralVideoEngines["seedance"] = {
  connected: false,
  not_connected_message: null,
  aspects: SEEDANCE_FORMATS.map((f) => f.key),
  duration_min: 4,
  duration_max: 30,
  resolutions: ["480p", "720p", "1080p"],
  default_resolution: "720p",
  price_per_sec_usd: { "480p": 0.2056, "720p": 0.4622, "1080p": 1.1372 },
  budget_usd: null,
};
const ENGINE_LABEL: Record<YasGeneralVideoEngine, string> = { veo: "Veo", seedance: "Seedance" };

/** The nearest Veo length to any Seedance length. */
function toVeoDuration(d: number): YasAnimateDuration {
  return d <= 5 ? 4 : d <= 7 ? 6 : 8;
}

export function GeneralVideoCreator({ onBack, onContinue, onOpenInEditor, initialRunId = null, onRunId, initialBrief = null }: Props) {
  const confirm = useConfirm();
  // The generation whose id rides in the URL — seeded from a resume link, then
  // kept pointing at the most-recently generated still so a refresh reopens it.
  const [runId, setRunId] = useState<number | null>(initialRunId ?? null);
  const [prompt, setPrompt] = useState("");
  const [engine, setEngine] = useState<YasGeneralVideoEngine>("veo");
  const [seedance, setSeedance] = useState(SEEDANCE_FALLBACK);
  const [aspect, setAspect] = useState<SeedanceAspect>("9:16");
  const [duration, setDuration] = useState<number>(6);
  const [withAudio, setWithAudio] = useState(true);
  const [quality, setQuality] = useState<"normal" | "high">("normal");
  const [resolution, setResolution] = useState<SeedanceResolution>("720p");
  const [enhancing, setEnhancing] = useState(false);

  const [refs, setRefs] = useState<RefItem[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [uploading, setUploading] = useState(false);

  const [items, setItems] = useState<YasGeneralVideoGen[]>([]);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [excluded, setExcluded] = useState<Set<number>>(new Set());
  const [finalising, setFinalising] = useState(false);
  const [finalMsg, setFinalMsg] = useState<string | null>(null);

  const fileRef = useRef<HTMLInputElement>(null);
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  // Stills whose animate request is still open, with the engine it went to. A
  // refresh landing mid-request must not put the Generate button back, or a
  // second click could pay for a second video (the server also refuses it).
  const inFlight = useRef<Map<number, YasGeneralVideoEngine>>(new Map());

  const loadRecent = useCallback(async () => {
    try {
      const { items } = await api.yasProducer.generalVideo.recent();
      setItems(
        items.map((r): YasGeneralVideoGen => {
          const eng = inFlight.current.get(r.id);
          return eng && r.status !== "animating" ? { ...r, status: "animating", engine: eng } : r;
        }),
      );
    } catch {
      /* best-effort */
    }
  }, []);

  // Trend Finder "Make this" — seed the Veo prompt with the AI-written brief.
  // Mount-only: the roadmap remounts this creator (projectNonce) for every
  // hand-off. Nothing else writes `prompt` programmatically (the resume effect
  // below only upserts the generation row), so there is nothing to guard.
  useEffect(() => {
    if (!initialBrief || initialRunId != null) return;
    setPrompt(initialBrief.topic);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    let cancelled = false;
    api.yasProducer.generalVideo
      .engines()
      .then((e) => {
        if (cancelled) return;
        setSeedance(e.seedance);
        setResolution(e.seedance.default_resolution);
      })
      .catch(() => {
        /* keep the fallback; the animate call reports anything real */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Switching engine keeps every choice the new engine can honour and moves
  // the rest to the nearest one it can.
  function chooseEngine(next: YasGeneralVideoEngine) {
    setEngine(next);
    if (next === "veo") {
      if (!VEO_ASPECTS.has(aspect)) setAspect(aspect === "1:1" || aspect === "3:4" ? "9:16" : "16:9");
      setDuration((d) => toVeoDuration(d));
    }
  }

  useEffect(() => {
    void loadRecent();
    return () => {
      if (pollTimer.current) clearInterval(pollTimer.current);
    };
  }, [loadRecent]);

  // Resume — a "Continue" / shared link deep-links to a single generation id.
  // Fetch that one row and upsert it into the strip (dedupes with loadRecent),
  // so the run reopens even if it fell outside the recent window. The `cancelled`
  // flag makes this correct under React StrictMode's mount→unmount→mount.
  useEffect(() => {
    if (!initialRunId) return;
    let cancelled = false;
    (async () => {
      try {
        const rec = await api.yasProducer.generalVideo.get(initialRunId);
        if (cancelled) return;
        upsert(rec);
        setRunId(rec.id);
      } catch {
        /* run gone / unreachable — recent() still populates the strip */
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialRunId]);

  // Mirror the current run id up to the URL (via ProducerRoadmap) so a hard
  // refresh reloads THIS run. Guarded by the parent to the active tool.
  useEffect(() => {
    if (runId != null) onRunId?.(runId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  useEffect(() => {
    const anyAnimating = items.some((it) => it.status === "animating");
    if (anyAnimating && !pollTimer.current) {
      pollTimer.current = setInterval(() => void loadRecent(), 5000);
    } else if (!anyAnimating && pollTimer.current) {
      clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
  }, [items, loadRecent]);

  function upsert(rec: YasGeneralVideoGen) {
    setItems((prev) => {
      const idx = prev.findIndex((p) => p.id === rec.id);
      if (idx === -1) return [rec, ...prev];
      const next = prev.slice();
      next[idx] = rec;
      return next;
    });
  }

  const assetsStore = useProducerAssetsStore();

  // ----- clear session -----
  // Archive this session's clips server-side, then reset the local strip so the
  // user starts clean. Non-destructive (rows are flagged 'archived', not deleted).
  async function clearSession() {
    if (
      !(await confirm({
        title: "Clear this session?",
        message:
          "Generated images and clips on screen will be cleared. Anything you already saved to Assets is kept.",
        confirmLabel: "Clear",
      }))
    )
      return;
    if (pollTimer.current) {
      clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
    try {
      await api.yasProducer.generalVideo.clearSession();
      setItems([]);
      setExcluded(new Set());
      setFinalMsg(null);
      setError(null);
    } catch (e: any) {
      setError(e?.message || String(e));
    }
  }

  // ----- references -----
  const assetIds = refs.filter((r) => r.kind === "asset").map((r) => r.id);
  function addAssets(assets: ProducerAssetRow[]) {
    setRefs((prev) => {
      const additions: RefItem[] = assets.map((a) => ({
        kind: "asset",
        id: a.id,
        thumb: api.producerAssets.thumbUrlFor(a),
        name: a.display_name || a.slug,
      }));
      return [...prev, ...additions].slice(0, MAX_REFS);
    });
    setPickerOpen(false);
  }
  async function onPickFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setError(null);
    try {
      for (const f of Array.from(files).slice(0, MAX_REFS - refs.length)) {
        const up = await api.yasProducer.posterHuman.uploadRef(f);
        const item: RefItem = { kind: "upload", id: up.id, thumb: up.url, name: f.name };
        setRefs((r) => [...r, item].slice(0, MAX_REFS));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }
  function removeRef(idx: number) {
    setRefs((prev) => prev.filter((_, i) => i !== idx));
  }

  // ----- generate -----
  async function generateStill() {
    const p = prompt.trim();
    if (!p) {
      setError("Describe the video you want first.");
      return;
    }
    setGenerating(true);
    setError(null);
    try {
      const rec = await api.yasProducer.generalVideo.generateStill({
        prompt: p,
        aspect_ratio: aspect,
        reference_upload_ids: refs.filter((r) => r.kind === "upload").map((r) => r.id),
        reference_asset_ids: assetIds,
      });
      upsert(rec);
      setRunId(rec.id); // surface the new run in the URL for refresh/resume
    } catch (e) {
      setError(e instanceof Error ? e.message : "Still generation failed");
    } finally {
      setGenerating(false);
    }
  }

  async function enhancePrompt() {
    const p = prompt.trim();
    if (!p || enhancing) return;
    setEnhancing(true);
    setError(null);
    try {
      const r = await api.yasProducer.enhance(p, "video_prompt");
      if (r.enhanced) setPrompt(r.enhanced);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn’t enhance");
    } finally {
      setEnhancing(false);
    }
  }

  async function animate(it: YasGeneralVideoGen) {
    if (inFlight.current.has(it.id)) return;
    inFlight.current.set(it.id, engine);
    setError(null);
    try {
      upsert({ ...it, status: "animating", engine });
      const rec = await api.yasProducer.generalVideo.animate(
        it.id,
        engine === "seedance"
          ? { expected_status: it.status, engine, duration_sec: duration, with_audio: withAudio, resolution }
          : {
              expected_status: it.status,
              engine,
              duration_sec: toVeoDuration(duration),
              with_audio: withAudio,
              quality,
            },
      );
      upsert(rec);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Video generation failed");
      void loadRecent();
    } finally {
      // Runs before the catch's loadRecent answers, so that refresh shows the
      // server's real status for this still.
      inFlight.current.delete(it.id);
    }
  }

  // ----- finalise -----
  const readyClips = items.filter((it) => it.status === "ready");
  const selectedIds = readyClips.map((it) => it.id).filter((id) => !excluded.has(id));
  // The final video takes the shape of the clips in it, not whatever the form
  // shows now (the engine toggle and a reload both reset the form's shape).
  const canvasAspect = readyClips.find((c) => !excluded.has(c.id))?.aspect_ratio ?? null;

  function toggleSelected(id: number) {
    setExcluded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function finaliseStitch() {
    if (selectedIds.length === 0) {
      setError("Select at least one finished clip to finalise.");
      return;
    }
    setFinalising(true);
    setError(null);
    setFinalMsg("Rendering the final video…");
    try {
      const { project_id } = await api.yasProducer.generalVideo.stitch(
        selectedIds,
        canvasAspect,
        prompt.trim() || "General AI Video",
      );
      const mediaUrl = api.yasProducer.videoEditor.compositionFileUrl(project_id);
      const start = Date.now();
      // A 'failed' here is often TRANSIENT — another backend on the shared DB can
      // flag it mid-render, but this stack's poller reclaims + self-heals it to
      // 'ready' once the file lands. So don't bail on the first 'failed'; keep
      // polling and only give up if it stays failed for a grace window.
      let failedSince: number | null = null;
      while (Date.now() - start < 10 * 60 * 1000) {
        await new Promise((r) => setTimeout(r, 5000));
        const proj = await api.yasProducer.videoEditor.fetch(project_id);
        const st = proj.render?.composition_status;
        if (st === "ready") {
          setFinalMsg(null);
          setFinalising(false);
          onContinue?.({
            refId: project_id,
            mediaType: "video",
            mediaUrl,
            topic: prompt.trim() || "General AI Video",
          });
          return;
        }
        if (st === "failed") {
          if (failedSince == null) failedSince = Date.now();
          setFinalMsg("Finishing up…");
          if (Date.now() - failedSince > 120000) {
            throw new Error(proj.render?.composition_error || "render failed");
          }
        } else {
          failedSince = null;
          setFinalMsg("Rendering the final video…");
        }
      }
      throw new Error("render timed out — try again");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Finalise failed");
      setFinalMsg(null);
      setFinalising(false);
    }
  }

  async function openInEditor() {
    if (selectedIds.length === 0) {
      setError("Select at least one finished clip to open in the editor.");
      return;
    }
    setFinalising(true);
    setError(null);
    try {
      const { project_id } = await api.yasProducer.generalVideo.openInEditor(
        selectedIds,
        canvasAspect,
        prompt.trim() || "General AI Video",
      );
      onOpenInEditor?.(project_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn’t open the editor");
    } finally {
      setFinalising(false);
    }
  }

  return (
    <div className="flex flex-col h-[calc(100vh-150px)] overflow-y-auto">
      <header className="shrink-0 flex items-center gap-3 pb-4">
        <button onClick={onBack} className="btn btn-ghost text-sm">← Back</button>
        <h2 className="text-lg font-semibold text-fg">General AI Video</h2>
        <span className="text-xs text-muted">Reference pics + prompt → still → video, with Veo or Seedance</span>
      </header>

      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)] gap-5 items-start">
        {/* ---------- LEFT: inputs ---------- */}
        <section className="card p-4 space-y-4 lg:sticky lg:top-4">
          <div className="space-y-1.5">
            <label className="eyebrow">References (optional · up to {MAX_REFS})</label>
            <div className="flex items-center gap-2 flex-wrap">
              {refs.map((r, i) => (
                <div key={`${r.kind}-${r.id}-${i}`} className="relative w-16 h-16 rounded-lg overflow-hidden border border-border">
                  <img src={r.thumb} alt={r.name} title={r.name} className="w-full h-full object-cover" />
                  <button
                    onClick={() => removeRef(i)}
                    className="absolute top-0 right-0 bg-black/60 text-white text-3xs px-1 rounded-bl"
                    title="Remove"
                  >
                    ✕
                  </button>
                  <span className="absolute bottom-0 inset-x-0 bg-black/55 text-white text-[8px] px-1 truncate">
                    {r.kind === "asset" ? "asset" : "upload"}
                  </span>
                </div>
              ))}
              {refs.length < MAX_REFS && (
                <>
                  <button
                    onClick={() => setPickerOpen(true)}
                    className="w-16 h-16 rounded-lg border border-dashed border-border text-muted hover:bg-bg-muted text-3xs flex items-center justify-center text-center leading-tight"
                    title="Pick from your Assets library"
                  >
                    Pick<br />asset
                  </button>
                  <button
                    onClick={() => fileRef.current?.click()}
                    disabled={uploading}
                    className="w-16 h-16 rounded-lg border border-dashed border-border text-muted hover:bg-bg-muted text-3xs flex items-center justify-center text-center leading-tight"
                    title="Upload an image"
                  >
                    {uploading ? "…" : <>Upload<br />image</>}
                  </button>
                </>
              )}
              <input
                ref={fileRef}
                type="file"
                accept="image/png,image/jpeg,image/webp"
                multiple
                hidden
                onChange={(e) => void onPickFiles(e.target.files)}
              />
            </div>
            <p className="text-[10.5px] text-muted">Pick a background, character, poster… or upload your own. The AI can place/compose them however your prompt says.</p>
          </div>

          <div className="space-y-1.5">
            <label className="eyebrow">Describe the video</label>
            <div className="relative">
              <textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                rows={4}
                placeholder="e.g. Place these students in this courtyard, coding on laptops, warm cinematic light…"
                className="w-full rounded-lg border border-border bg-bg-subtle px-3 pr-24 py-2.5 text-sm focus:outline-none focus:border-accent resize-y"
              />
              <EnhanceButton
                onClick={() => void enhancePrompt()}
                busy={enhancing}
                disabled={enhancing || prompt.trim().length === 0}
                title="Rewrite into a richer, cinematic prompt — same topic, brand voice"
              />
            </div>
          </div>

          <div className="space-y-2">
            <label className="eyebrow">Video engine</label>
            <SegmentedControl<YasGeneralVideoEngine>
              variant="tab"
              options={[
                { value: "veo", label: "Veo", title: "Google Veo: 9:16 or 16:9, up to 8 seconds" },
                {
                  value: "seedance",
                  label: "Seedance",
                  title: "Seedance 2.5: six shapes, 4 to 30 seconds, up to 1080p",
                },
              ]}
              value={engine}
              onChange={chooseEngine}
            />
            <p className="text-[10.5px] text-muted">
              {engine === "veo"
                ? "Up to 8 seconds, vertical or landscape."
                : `${seedance.duration_min} to ${seedance.duration_max} seconds, six shapes, up to 1080p.`}
            </p>
            {engine === "seedance" && !seedance.connected && seedance.not_connected_message && (
              <p className="text-[11px] text-warning">{seedance.not_connected_message}</p>
            )}
          </div>

          <div className="space-y-2">
            <label className="eyebrow">Format</label>
            {engine === "veo" ? (
              <SegmentedControl<SeedanceAspect>
                variant="pill"
                options={ASPECTS.map((a) => ({ value: a.key, label: a.label }))}
                value={aspect}
                onChange={setAspect}
              />
            ) : (
              <SegmentedControl<SeedanceAspect>
                variant="pill"
                options={SEEDANCE_FORMATS.filter((f) => seedance.aspects.includes(f.key)).map((f) => ({
                  value: f.key,
                  label: f.label,
                  title: f.hint,
                }))}
                value={aspect}
                onChange={setAspect}
              />
            )}
          </div>

          <div className="space-y-2">
            <label className="eyebrow">Length</label>
            <div className="flex items-center gap-2">
              {engine === "veo" ? (
                <SegmentedControl<number>
                  variant="pill"
                  options={DURATIONS.map((d) => ({ value: d, label: `${d}s` }))}
                  value={toVeoDuration(duration)}
                  onChange={setDuration}
                />
              ) : (
                <div className="flex items-center gap-2 flex-1 min-w-0">
                  <input
                    type="range"
                    min={seedance.duration_min}
                    max={seedance.duration_max}
                    step={1}
                    value={duration}
                    onChange={(e) => setDuration(Number(e.target.value))}
                    className="slider flex-1 min-w-0"
                    style={
                      {
                        "--pct": `${
                          ((duration - seedance.duration_min) /
                            Math.max(1, seedance.duration_max - seedance.duration_min)) *
                          100
                        }%`,
                      } as CSSProperties
                    }
                    aria-label="Length in seconds"
                  />
                  <span className="text-[13px] tabular-nums w-9 text-right">{duration}s</span>
                </div>
              )}
              <div className="ml-auto">
                <Toggle label="Sound" checked={withAudio} onChange={setWithAudio} />
              </div>
            </div>
          </div>

          {engine === "seedance" && (
            <div className="space-y-2">
              <label className="eyebrow">Resolution</label>
              <SegmentedControl<SeedanceResolution>
                variant="pill"
                options={seedance.resolutions.map((res) => ({ value: res, label: res }))}
                value={resolution}
                onChange={setResolution}
              />
              <p className="text-[10.5px] text-muted">
                About ${(duration * (seedance.price_per_sec_usd[resolution] ?? 0)).toFixed(2)} per clip
                at {resolution}
                {seedance.budget_usd ? ` · $${seedance.budget_usd.toFixed(0)} budget per 30 days` : ""}.
              </p>
            </div>
          )}

          <div className="space-y-2" hidden={engine !== "veo"}>
            <label className="eyebrow">Video quality</label>
            <SegmentedControl<"normal" | "high">
              variant="pill"
              options={([
                { key: "normal", label: "Normal", sub: "fast" },
                { key: "high", label: "High", sub: "slower · premium" },
              ] as const).map((q) => ({
                value: q.key,
                label: (
                  <>
                    {q.label} <span className="text-3xs text-muted">· {q.sub}</span>
                  </>
                ),
                title: q.sub,
              }))}
              value={quality}
              onChange={setQuality}
            />
          </div>

          <button
            onClick={() => void generateStill()}
            disabled={generating}
            className="btn btn-primary text-sm w-full"
          >
            {generating ? "Generating still…" : "Generate still"}
          </button>

          {error && <p className="text-[12px] text-danger">{error}</p>}
        </section>

        {/* ---------- RIGHT: preview / results ---------- */}
        <div className="space-y-4 min-w-0">
          {items.length === 0 ? (
            <div className="card p-10 text-center text-sm text-muted">
              Your generated stills &amp; videos will appear here. Start by generating a still on the left.
            </div>
          ) : (
            <>
              <div className="flex items-center justify-between">
                <h3 className="eyebrow">This session</h3>
                <div className="flex items-center gap-3">
                  <span className="text-2xs text-muted">{readyClips.length} ready clip(s)</span>
                  <button
                    onClick={() => void clearSession()}
                    className="text-2xs text-muted hover:text-danger underline underline-offset-2"
                    title="Archive this session's clips and start clean"
                  >
                    Clear session
                  </button>
                </div>
              </div>
              <div className="grid grid-cols-2 xl:grid-cols-3 gap-3">
                {items.map((it) => (
                  <ClipCard
                    key={it.id}
                    it={it}
                    engine={engine}
                    included={it.status === "ready" && !excluded.has(it.id)}
                    onAnimate={() => void animate(it)}
                    onToggle={() => toggleSelected(it.id)}
                    onError={setError}
                    onSaved={() => assetsStore.invalidate()}
                  />
                ))}
              </div>
            </>
          )}

          {readyClips.length > 0 && (
            <section className="sticky bottom-0 bg-bg/90 backdrop-blur border-t border-border py-3 flex items-center gap-3">
              <span className="text-[13px] text-muted">
                {finalMsg ?? `${selectedIds.length} of ${readyClips.length} clip(s) selected`}
              </span>
              <div className="ml-auto flex items-center gap-2">
                <button onClick={() => void openInEditor()} disabled={finalising || selectedIds.length === 0} className="btn btn-ghost text-sm">
                  Open in video editor
                </button>
                {onContinue && (
                  <button onClick={() => void finaliseStitch()} disabled={finalising || selectedIds.length === 0} className="btn btn-accent text-sm px-5">
                    {finalising ? "Finalising…" : "Finalise + stitch →"}
                  </button>
                )}
              </div>
            </section>
          )}
        </div>
      </div>

      {pickerOpen && (
        <AssetPickerModal
          maxSelection={MAX_REFS - refs.length}
          alreadySelectedAssetIds={assetIds}
          onPick={addAssets}
          onClose={() => setPickerOpen(false)}
          showCategoryChips={true}
          imageOnly={true}
          confirmLabel="Add"
        />
      )}
    </div>
  );
}

function ClipCard({
  it,
  engine,
  included,
  onAnimate,
  onToggle,
  onError,
  onSaved,
}: {
  it: YasGeneralVideoGen;
  /** The engine selected in the form, used by this card's Generate video. */
  engine: YasGeneralVideoEngine;
  included: boolean;
  onAnimate: () => void;
  onToggle: () => void;
  onError: (msg: string) => void;
  onSaved?: () => void;
}) {
  const [mediaError, setMediaError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const ready = it.status === "ready";
  const frame = it.frame_url ? api.yasProducer.generalVideo.frameUrl(it.id, it.updated_at) : null;
  // Match the preview box to the clip's true shape (vertical 9:16 vs landscape
  // 16:9) — a hardcoded aspect-video cropped vertical stills into a wide box.
  // Landscape fallback preserved: this surface produces 16:9 unless told
  // otherwise. (TalkingHeadV2Creator's equivalent ternary falls back the other
  // way, which is exactly why the fallback is now an explicit argument.)
  const aspectClass = aspectClassFor(it.aspect_ratio, "16:9");
  // The engine that made (or is making) this clip; old rows carry none = Veo.
  const madeWith: YasGeneralVideoEngine | null =
    it.status === "animating" || ready ? (it.engine ?? "veo") : null;
  const veoCantShape = engine === "veo" && !VEO_ASPECTS.has(it.aspect_ratio);

  async function saveStill() {
    if (saving || saved) return;
    setSaving(true);
    try {
      await api.yasProducer.generalVideo.saveStillToAssets(it.id, {});
      setSaved(true);
      onSaved?.();
    } catch (e: any) {
      onError(e?.message || String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="card p-2 space-y-2">
      <div className={`relative w-full ${aspectClass} rounded-md overflow-hidden bg-black/80 flex items-center justify-center`}>
        {ready && !mediaError ? (
          <video
            src={api.yasProducer.generalVideo.fileUrl(it.id, it.updated_at)}
            controls
            playsInline
            onError={() => setMediaError(true)}
            className="w-full h-full object-cover"
          />
        ) : frame ? (
          <img src={frame} alt="" className="w-full h-full object-cover opacity-90" />
        ) : (
          <span className="text-[12px] text-muted">…</span>
        )}
        {it.status === "animating" && (
          <span className="absolute inset-0 flex items-center justify-center bg-black/40 text-2xs text-white">
            Generating video with {ENGINE_LABEL[madeWith ?? "veo"]}…
          </span>
        )}
        {(madeWith || it.aspect_ratio) && (
          <span className="absolute top-1 left-1 rounded bg-black/60 px-1.5 py-0.5 text-[9px] text-white">
            {[madeWith && ENGINE_LABEL[madeWith], it.aspect_ratio, ready && it.duration_sec ? `${it.duration_sec}s` : null]
              .filter(Boolean)
              .join(" · ")}
          </span>
        )}
      </div>

      {it.status === "failed" && (
        <p className="text-2xs text-danger px-1">
          {mediaError ? "Couldn’t load this clip." : it.error || "Generation failed"}
        </p>
      )}

      {it.status === "frame_ready" && veoCantShape && (
        <p className="text-2xs text-muted px-1">
          Veo makes only 9:16 and 16:9. Switch the engine to Seedance to animate this {it.aspect_ratio} still.
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2 px-0.5">
        {(it.status === "frame_ready" || (it.status === "failed" && frame)) && (
          <>
            {it.status === "frame_ready" && (
              <button
                onClick={onAnimate}
                disabled={veoCantShape}
                className="btn btn-primary text-[12px] px-3 py-1"
                title={
                  veoCantShape
                    ? `Veo makes only 9:16 and 16:9 videos. Switch the engine to Seedance for this ${it.aspect_ratio} still.`
                    : `Animate this still with ${ENGINE_LABEL[engine]}`
                }
              >
                Generate video · {ENGINE_LABEL[engine]}
              </button>
            )}
            <a
              href={api.yasProducer.generalVideo.downloadFrameUrl(it.id)}
              className="btn btn-ghost text-[12px] px-2 py-1 ml-auto"
              title="Download this still"
            >
              ↓ Still
            </a>
            <button
              onClick={() => void saveStill()}
              disabled={saving || saved}
              className="btn btn-ghost text-[12px] px-2 py-1"
              title={saved ? "Saved to Assets" : "Save this still to Assets"}
            >
              {saving ? "Saving…" : saved ? "Saved ✓" : "Save"}
            </button>
          </>
        )}
        {ready && (
          <>
            <span title="Include in the final video">
              <Toggle label="Include" checked={included} onChange={onToggle} />
            </span>
            <a
              href={api.yasProducer.generalVideo.downloadUrl(it.id)}
              className="btn btn-ghost text-[12px] px-2 py-1 ml-auto"
              title="Download this clip"
            >
              ↓ Download
            </a>
          </>
        )}
      </div>
    </div>
  );
}
