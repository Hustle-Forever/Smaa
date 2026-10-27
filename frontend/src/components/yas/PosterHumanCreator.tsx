/**
 * Yas Producer · Poster + Human creator view.
 *
 * UX: topic input + reference picker (0-2 refs, upload OR pick from assets) +
 * Generate. After result: Remove logo, Save to Assets, or Continue to Preview.
 * History strip at bottom shows all attempts in the current session; clicking
 * one makes it the current.
 *
 * Rehydrates the last 24h of unsaved generations on mount so a refresh
 * doesn't evaporate work.
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { JobProgress } from "../ui/JobProgress";

import { humanError } from "../../lib/format";
import { api } from "../../lib/api";
import type { ProducerBrief } from "../../lib/producerBrief";
import { useProducerAssetsStore } from "../../lib/store";
import { useConfirm } from "../ConfirmProvider";
import { useToast } from "../ToastProvider";
import type { ProducerAssetRow, YasGeneration, YasGenerationBatchResponse, YasMode, YasImageModel } from "../../lib/types";
import { POSTER_FORMATS, aspectClass, aspectFromDims } from "../../lib/aspect";
import type { YasPosterAspect } from "../../lib/types";
import { AssetPickerModal } from "./AssetPickerModal";
import { FormatPicker } from "./FormatPicker";
import type { ContinueToPreview } from "./roadmap/types";

const CONTENT_CATEGORY = "img_poster_human";
const MAX_REFS = 2;

// ---------------------------------------------------------------------------
// Inline SVG icons — Phosphor / Lucide style: 1.75-2px stroke, rounded caps,
// 24x24 viewBox. They inherit color from the parent text color, so they sit
// in the theme naturally (text-fg, text-muted, text-accent etc.). Avoid
// pulling in an icon library — we only need a handful here.
// ---------------------------------------------------------------------------

type IconProps = { className?: string };

function SparkleIcon({ className = "w-4 h-4" }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3z"/>
      <path d="M19 14l.8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8L19 14z"/>
    </svg>
  );
}

function ImageFrameIcon({ className = "w-4 h-4" }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <rect x="3" y="4" width="18" height="16" rx="2"/>
      <circle cx="9" cy="10" r="1.5"/>
      <path d="M21 17l-5-5-9 9"/>
    </svg>
  );
}

function UploadIcon({ className = "w-5 h-5" }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <path d="M21 14v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
      <path d="M17 8l-5-5-5 5"/>
      <path d="M12 3v13"/>
    </svg>
  );
}

function GridFolderIcon({ className = "w-5 h-5" }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <rect x="3" y="3" width="7" height="7" rx="1.5"/>
      <rect x="14" y="3" width="7" height="7" rx="1.5"/>
      <rect x="3" y="14" width="7" height="7" rx="1.5"/>
      <rect x="14" y="14" width="7" height="7" rx="1.5"/>
    </svg>
  );
}

function CheckIcon({ className = "w-3.5 h-3.5" }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <path d="M20 6L9 17l-5-5"/>
    </svg>
  );
}

function XIcon({ className = "w-3.5 h-3.5" }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <path d="M18 6L6 18M6 6l12 12"/>
    </svg>
  );
}

function ChevronLeftIcon({ className = "w-4 h-4" }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <path d="M15 18l-6-6 6-6"/>
    </svg>
  );
}

function CanvasIcon({ className = "w-8 h-8" }: IconProps) {
  /* Empty-state placeholder — a stylised picture frame with a small spark.
     Stroke-only so it sits gracefully on the muted background. */
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <rect x="3" y="3" width="18" height="18" rx="2.5"/>
      <path d="M3 16l5-5 4 4 3-3 6 6"/>
      <circle cx="8.5" cy="8.5" r="1.5"/>
    </svg>
  );
}

// Local model of a selected reference. Asset refs carry the producer_assets
// id; upload refs carry the yas_uploads id + thumbnail URL.
type RefItem =
  | { kind: "asset"; assetId: number; thumbnailUrl: string; displayName?: string }
  | { kind: "upload"; uploadId: number; thumbnailUrl: string; displayName?: string };

interface Props {
  onBack: () => void;
  /** Roadmap: hand the chosen poster to the Preview step. Absent when standalone. */
  onContinue?: ContinueToPreview;
  /** Phase C: reopen a specific past generation (by id) from the Library,
   *  pre-loaded into the ready view. Wins over the most-recent rehydrate. */
  initialRunId?: number | null;
  /** Roadmap deep-link: report the visible 2x2 grid's generation ids so the
   *  wrapper can encode a full, shareable batch URL. */
  onBatch?: (ids: number[]) => void;
  /** Roadmap deep-link: seed the grid on resume (superset of initialRunId).
   *  >1 id restores the full grid; single id → single-card behavior. */
  initialRunIds?: number[] | null;
  /** Trend Finder "Make this" seed. One-shot, applied at mount only — the
   *  roadmap bumps projectNonce for every hand-off, so this component is always
   *  freshly mounted when a brief arrives. Never set together with
   *  initialRunId (a resume wins; the roadmap clears resumeRunId first). */
  initialBrief?: ProducerBrief | null;
}

// One tile in the 2x2 grid — mirrors Create Poster's VariantCard. Per-card
// actions on hover: Explore (4 more like this), Save, Remove logo, Use (Continue).
function VariantCard({
  gen,
  saving,
  removing,
  exploring,
  canExplore,
  onSave,
  onRemoveLogo,
  onExplore,
  onContinue,
}: {
  gen: YasGeneration;
  saving: boolean;
  removing: boolean;
  exploring: boolean;
  canExplore: boolean;
  onSave: () => void;
  onRemoveLogo: () => void;
  onExplore: () => void;
  onContinue?: () => void;
}) {
  const ready = gen.status === "ready" || gen.status === "saved_to_assets";
  const isSaved = gen.status === "saved_to_assets" || gen.saved_asset_id != null;
  // The cell is the shape the poster IS, read off its pixels, not the shape
  // that was asked for: a 9:16 poster in a hardcoded square cell is the bug
  // lib/aspect.ts was written to stop happening a fourth time.
  const shape = aspectClass(aspectFromDims(gen, POSTER_FORMATS, "1:1"), "1:1");
  return (
    <div className={`relative group rounded-lg overflow-hidden border border-border bg-black ${shape}`}>
      {ready ? (
        <img
          src={api.yasProducer.posterHuman.fileUrl(gen.id, gen.created_at)}
          alt={gen.topic}
          className="w-full h-full object-contain"
        />
      ) : gen.status === "failed" ? (
        <div className="w-full h-full flex items-center justify-center p-3 text-center text-xs text-danger">
          {gen.error || "Generation failed."}
        </div>
      ) : (
        <div className="w-full h-full flex items-center justify-center">
          <span className="inline-block w-8 h-8 border-2 border-accent border-t-transparent rounded-full animate-spin" />
        </div>
      )}
      {isSaved && (
        <span className="absolute top-1.5 left-1.5 w-5 h-5 rounded-full bg-success text-white flex items-center justify-center shadow">
          <CheckIcon className="w-3 h-3" />
        </span>
      )}
      {ready && (
        <div className="absolute inset-x-0 bottom-0 p-1.5 flex items-center gap-1 flex-wrap bg-gradient-to-t from-black/85 to-transparent opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
          {canExplore && (
            <button
              onClick={onExplore}
              disabled={exploring}
              className="text-2xs px-2 py-1 rounded bg-white/90 text-black font-medium hover:bg-white disabled:opacity-60"
              title="Generate 4 close riffs anchored on this poster"
            >
              {exploring ? "Exploring…" : "Explore →"}
            </button>
          )}
          {gen.status === "ready" && (
            <button
              onClick={onSave}
              disabled={saving}
              className="text-2xs px-2 py-1 rounded bg-white/90 text-black font-medium hover:bg-white disabled:opacity-60"
            >
              {saving ? "Saving…" : "Save"}
            </button>
          )}
          <button
            onClick={onRemoveLogo}
            disabled={removing}
            className="text-2xs px-2 py-1 rounded bg-black/60 text-white font-medium hover:bg-black/80 disabled:opacity-60"
            title="Re-render without the 42 logo (replaces this tile)"
          >
            {removing ? "…" : "Remove logo"}
          </button>
          {onContinue && (
            <button
              onClick={onContinue}
              className="text-2xs px-2 py-1 rounded bg-accent text-white font-medium hover:bg-accent/90 ml-auto"
              title="Use this poster — continue to Preview"
            >
              Use →
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export function PosterHumanCreator({ onBack, onContinue, initialRunId = null, onBatch, initialRunIds = null, initialBrief = null }: Props) {
  const [mode, setMode] = useState<YasMode>("style_inspired");
  // Image engine is Nano Banana Pro (gemini-3-pro-image-preview) — the OpenAI
  // option was removed here; Poster + Human always renders with Gemini.
  const imageModel: YasImageModel = "gemini_pro";
  const [topic, setTopic] = useState("");
  // Output shape, the same three the poster pipeline accepts and the same
  // control Create Poster and Human Character use.
  const [aspect, setAspect] = useState<YasPosterAspect>("1:1");

  // STYLE_INSPIRED state — up to 2 refs (asset or upload), used as style DNA
  const [refs, setRefs] = useState<RefItem[]>([]);

  // TEMPLATE_FILL state — exactly 1 template (asset) + exactly 1 student (upload)
  const [templateAsset, setTemplateAsset] = useState<{ assetId: number; thumbnailUrl: string; displayName?: string } | null>(null);
  const [studentUpload, setStudentUpload] = useState<{ uploadId: number; thumbnailUrl: string; displayName?: string } | null>(null);
  // The student face can also come from a saved Human Character asset instead of
  // a disk upload (mutually exclusive with studentUpload).
  const [studentAsset, setStudentAsset] = useState<{ assetId: number; thumbnailUrl: string; displayName?: string } | null>(null);
  // Per-slot picker state — knows which slot is being chosen
  const [pickerTarget, setPickerTarget] = useState<"refs" | "template" | "student" | null>(null);
  const studentFileInputRef = useRef<HTMLInputElement>(null);

  // 2x2 grid navigation (Midjourney stack): each entry is one batch of 4.
  // Explore pushes a new level; "Back to grid" pops it. Mirrors PosterCreator.
  const [batchStack, setBatchStack] = useState<YasGeneration[][]>([]);
  const [phase, setPhase] = useState<"idle" | "generating">("idle");

  // When the run started, so JobProgress can show elapsed time. Derived from the
  // phase flip rather than threaded through every call site that starts a job.
  const [startedAt, setStartedAt] = useState(0);
  useEffect(() => {
    if (phase === "generating") setStartedAt(Date.now());
  }, [phase]);
  const [savingId, setSavingId] = useState<number | null>(null);
  const [removingId, setRemovingId] = useState<number | null>(null);
  const [exploringId, setExploringId] = useState<number | null>(null);
  const [addMenuOpen, setAddMenuOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [enhancingTopic, setEnhancingTopic] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const toast = useToast();
  const confirm = useConfirm();
  const assetsStore = useProducerAssetsStore();
  const pickerOpen = pickerTarget !== null;

  // Trend Finder "Make this" — seed the composer with the AI-written brief.
  // Mount-only: the roadmap remounts this creator (projectNonce) for every
  // hand-off. `topic` here is plain state (no localStorage layer), so this prop
  // is the only route into the box.
  useEffect(() => {
    if (!initialBrief || initialRunId != null) return;
    setTopic(initialBrief.topic);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Rehydrate the most recent 2x2 batch on mount — group the flat /recent list
  // by variant_set_id. Skipped when resuming a specific id (effect below owns
  // it), and when a Trend Finder brief seeded the composer — its setTopic is
  // unconditional AND async, so it would otherwise clobber the seed.
  useEffect(() => {
    if (initialRunId != null || initialBrief) return;
    let cancelled = false;
    api.yasProducer.posterHuman
      .recent()
      .then((r) => {
        if (cancelled) return;
        const list = r.items || [];
        if (!list.length) return;
        const newest = list[0];
        const key = newest.variant_set_id ?? newest.id;
        const group = list
          .filter((g) => (g.variant_set_id ?? g.id) === key)
          .sort((a, b) => a.id - b.id);
        setBatchStack([group.length ? group : [newest]]);
        setTopic(newest.topic || "");
        if (newest.mode === "template_fill") setMode("template_fill");
      })
      .catch(() => {
        /* non-fatal */
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Phase C — reopen a past generation from the Library. A single id restores
  // one card; a batch (initialRunIds.length > 1) rebuilds the full 2x2 grid by
  // fetching each id in parallel (404s dropped), preserved in id order.
  useEffect(() => {
    if (initialRunId == null) return;
    let cancelled = false;
    const ids = initialRunIds && initialRunIds.length > 1 ? initialRunIds : [initialRunId];
    Promise.all(
      ids.map((id) =>
        api.yasProducer.posterHuman.fetch(id).catch(() => null),
      ),
    )
      .then((results) => {
        if (cancelled) return;
        const gens = results.filter((g): g is YasGeneration => g != null);
        if (!gens.length) return;
        setBatchStack([gens]);
        const first = gens[0];
        setTopic(first.topic || "");
        if (first.mode === "template_fill") setMode("template_fill");
      })
      .catch(() => {
        /* non-fatal */
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialRunId]);

  // Roadmap deep-link — report the visible 2x2 grid's ids so the wrapper can
  // encode a full, shareable batch URL. Fires whenever the top batch changes.
  useEffect(() => {
    const top = batchStack.length ? batchStack[batchStack.length - 1] : [];
    const topBatchIds = top.map((g) => g.id);
    if (!topBatchIds.length) return;
    onBatch?.(topBatchIds);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchStack]);

  // Close the add-menu when clicking outside
  useEffect(() => {
    if (!addMenuOpen) return;
    function onDocClick() {
      setAddMenuOpen(false);
    }
    // Defer attach so the click that opened doesn't immediately close
    const t = setTimeout(() => document.addEventListener("click", onDocClick), 0);
    return () => {
      clearTimeout(t);
      document.removeEventListener("click", onDocClick);
    };
  }, [addMenuOpen]);

  const currentBatch = useMemo(
    () => (batchStack.length ? batchStack[batchStack.length - 1] : []),
    [batchStack],
  );
  const hasResult = currentBatch.length > 0;
  const exploreDepth = batchStack.length;

  // Update one record across every batch level (Save / Remove-logo patches).
  function patch(id: number, p: Partial<YasGeneration>) {
    setBatchStack((prev) =>
      prev.map((batch) => batch.map((g) => (g.id === id ? { ...g, ...p } : g))),
    );
  }

  // Mode-aware "can generate" check.
  const canGenerate = (() => {
    if (phase !== "idle") return false;
    if (topic.trim().length === 0) return false;
    if (mode === "template_fill") {
      // Fresh template_fill requires BOTH slots
      return templateAsset !== null && (studentUpload !== null || studentAsset !== null);
    }
    return true; // fresh style_inspired needs only topic; refs optional
  })();

  async function handleAddAsset() {
    setAddMenuOpen(false);
    setPickerTarget("refs");
  }

  async function handleAddUpload() {
    setAddMenuOpen(false);
    fileInputRef.current?.click();
  }

  async function handleFileChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-pick same file
    if (!file) return;
    if (refs.length >= MAX_REFS) {
      toast.info(`Maximum ${MAX_REFS} references`, "Cap reached");
      return;
    }
    try {
      const upload = await api.yasProducer.posterHuman.uploadRef(file);
      setRefs((prev) => [
        ...prev,
        {
          kind: "upload",
          uploadId: upload.id,
          thumbnailUrl: api.yasProducer.posterHuman.uploadFileUrl(upload.token),
          displayName: file.name,
        },
      ]);
    } catch (err: any) {
      toast.error("Upload failed", humanError(err));
    }
  }

  function handleAssetsPicked(picked: ProducerAssetRow[]) {
    if (pickerTarget === "template") {
      const first = picked[0];
      if (first) {
        setTemplateAsset({
          assetId: first.id,
          thumbnailUrl: api.producerAssets.thumbUrlFor(first),
          displayName: first.display_name ?? first.slug,
        });
      }
      setPickerTarget(null);
      return;
    }
    if (pickerTarget === "student") {
      const first = picked[0];
      if (first) {
        setStudentUpload(null); // asset + upload are mutually exclusive
        setStudentAsset({
          assetId: first.id,
          thumbnailUrl: api.producerAssets.thumbUrlFor(first),
          displayName: first.display_name ?? first.slug,
        });
      }
      setPickerTarget(null);
      return;
    }
    // refs mode (style_inspired)
    setPickerTarget(null);
    const remaining = MAX_REFS - refs.length;
    const additions = picked.slice(0, remaining).map<RefItem>((a) => ({
      kind: "asset",
      assetId: a.id,
      thumbnailUrl: api.producerAssets.thumbUrlFor(a),
      displayName: a.display_name ?? a.slug,
    }));
    setRefs((prev) => [...prev, ...additions]);
  }

  function removeRef(idx: number) {
    setRefs((prev) => prev.filter((_, i) => i !== idx));
  }

  // template_fill: handle student photo upload
  async function handleStudentFileChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    try {
      const upload = await api.yasProducer.posterHuman.uploadRef(file);
      setStudentAsset(null); // asset + upload are mutually exclusive
      setStudentUpload({
        uploadId: upload.id,
        thumbnailUrl: api.yasProducer.posterHuman.uploadFileUrl(upload.token),
        displayName: file.name,
      });
    } catch (err: any) {
      toast.error("Student photo upload failed", humanError(err));
    }
  }

  // ✨ Enhance the topic — call Gemini Flash to polish rough text
  async function enhanceTopic() {
    const text = topic.trim();
    if (!text || enhancingTopic) return;
    setEnhancingTopic(true);
    try {
      const r = await api.yasProducer.enhance(text, "poster_human");
      if (r.enhanced) setTopic(r.enhanced);
    } catch (err: any) {
      toast.error("Couldn't enhance", humanError(err));
    } finally {
      setEnhancingTopic(false);
    }
  }

  // Toggle mode — clears mode-specific selections to avoid stale state
  function changeMode(next: YasMode) {
    if (next === mode) return;
    setMode(next);
    setRefs([]);
    setTemplateAsset(null);
    setStudentUpload(null);
    setStudentAsset(null);
    setError(null);
  }

  async function runGenerate() {
    if (!canGenerate) return;

    setError(null);
    setPhase("generating");
    try {
      let resp: YasGenerationBatchResponse;
      if (mode === "template_fill") {
        // FRESH template-fill — the student face is an uploaded photo or a
        // picked Human Character asset. Renders a 2x2 batch (no concept stage).
        if (!templateAsset || (!studentUpload && !studentAsset)) {
          throw new Error("Pick a template and a student photo (upload or Human Character) first");
        }
        resp = await api.yasProducer.posterHuman.templateFill({
          topic: topic.trim(),
          template_asset_id: templateAsset.assetId,
          student_upload_id: studentUpload?.uploadId ?? null,
          student_asset_id: studentAsset?.assetId ?? null,
          parent_generation_id: null,
          feedback_text: null,
          image_model: imageModel,
          aspect,
        });
      } else {
        // FRESH style-inspired — Stage-1 casts 4 compositions, Stage-2 renders.
        resp = await api.yasProducer.posterHuman.generate({
          topic: topic.trim(),
          reference_asset_ids: refs.filter((r) => r.kind === "asset").map((r: any) => r.assetId),
          reference_upload_ids: refs.filter((r) => r.kind === "upload").map((r: any) => r.uploadId),
          parent_generation_id: null,
          feedback_text: null,
          retry_kind: null,
          image_model: imageModel,
          aspect,
        });
      }
      const items = resp.items || [];
      setBatchStack([items]); // fresh root batch (resets any Explore depth)
      const ready = items.filter((g) => g.status === "ready").length;
      toast.info(`${ready}/${items.length} posters generated`, "");
    } catch (err: any) {
      // humanError, not err.message: the backend now sends a sentence written
      // for whoever clicked Generate, and prefixing it with "502" undoes that.
      const msg = humanError(err, "Generation failed");
      setError(msg);
      toast.error("Generation failed", msg);
    } finally {
      setPhase("idle");
    }
  }

  async function handleSaveCard(gen: YasGeneration) {
    if (savingId || gen.status !== "ready") return;
    setSavingId(gen.id);
    try {
      const asset = await api.yasProducer.posterHuman.saveToAssets(gen.id, {
        display_name: (gen.topic || topic).trim() || `Yas poster #${gen.id}`,
      });
      patch(gen.id, { status: "saved_to_assets", saved_asset_id: asset.id });
      assetsStore.invalidate();
      assetsStore.fetch({ limit: 500 });
      toast.info("Saved to Assets", asset.display_name || "");
    } catch (err: any) {
      const msg = err?.message || String(err);
      if (msg.includes("already_saved") || msg.includes("409")) {
        patch(gen.id, { status: "saved_to_assets" });
        toast.info("Already saved", "View it in Assets");
      } else {
        toast.error("Save failed", msg);
      }
    } finally {
      setSavingId(null);
    }
  }

  // "Remove logo" → re-render this tile without the 42 logo; the logo-removed
  // child REPLACES the tile in the grid (its own row is kept in the DB).
  async function handleRemoveLogo(gen: YasGeneration) {
    if (removingId) return;
    if (!(gen.status === "ready" || gen.status === "saved_to_assets")) return;
    setRemovingId(gen.id);
    setError(null);
    try {
      const child = await api.yasProducer.posterHuman.removeLogo(gen.id);
      setBatchStack((prev) =>
        prev.map((batch) => batch.map((g) => (g.id === gen.id ? child : g))),
      );
      toast.info("Logo removed", `New version #${child.id}`);
    } catch (err: any) {
      toast.error("Couldn't remove logo", humanError(err));
    } finally {
      setRemovingId(null);
    }
  }

  // Explore → 4 close riffs anchored on the chosen poster (one level deep).
  async function handleExplore(gen: YasGeneration) {
    if (exploringId || batchStack.length > 1) return;
    if (!(gen.status === "ready" || gen.status === "saved_to_assets")) return;
    setExploringId(gen.id);
    try {
      const resp = await api.yasProducer.posterHuman.variants(gen.id);
      setBatchStack((prev) => [...prev, resp.items || []]);
    } catch (err: any) {
      toast.error("Explore failed", humanError(err));
    } finally {
      setExploringId(null);
    }
  }

  async function startOver() {
    if (batchStack.length > 0) {
      if (
        !(await confirm({
          title: "Clear everything?",
          message:
            "This clears the topic, references, and results on screen. Your saved posters stay in the Library.",
          confirmLabel: "Clear",
        }))
      ) {
        return;
      }
    }
    setTopic("");
    setRefs([]);
    setTemplateAsset(null);
    setStudentUpload(null);
    setStudentAsset(null);
    setBatchStack([]);
    setError(null);
  }

  return (
    <div className="space-y-5">
      {/* Header */}
      <header className="flex items-center gap-3">
        <button onClick={onBack} className="btn btn-ghost text-sm inline-flex items-center gap-1">
          <ChevronLeftIcon /> Back
        </button>
        <h1 className="h1 flex-1">Create Poster + Human</h1>
        {(batchStack.length > 0 || topic) && (
          <button onClick={startOver} className="btn btn-ghost text-xs">
            Start over
          </button>
        )}
      </header>

      {/* Two-column layout — composer on the left, result on the right.
          Stacks vertically on narrow screens. Cuts vertical scroll in half. */}
      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)] gap-5 items-start">

      {/* === LEFT COLUMN: Composer === */}
      <section className="card p-4 space-y-4 lg:sticky lg:top-4">
        {/* Mode toggle — tab cards with SVG icons */}
        <div className="grid grid-cols-2 gap-2">
          <button
            onClick={() => changeMode("style_inspired")}
            className={[
              "rounded-xl border-2 p-2.5 text-left transition-all",
              mode === "style_inspired"
                ? "border-accent bg-accent/5 shadow-sm"
                : "border-border bg-bg-muted/30 hover:border-fg/30 hover:bg-bg-muted/60",
            ].join(" ")}
          >
            <div className="flex items-center gap-1.5 mb-0.5">
              <SparkleIcon className={mode === "style_inspired" ? "w-4 h-4 text-accent" : "w-4 h-4 text-muted"} />
              <span className="font-semibold text-xs text-fg">Style inspired</span>
              {mode === "style_inspired" && (
                <span className="ml-auto text-accent"><CheckIcon /></span>
              )}
            </div>
            <p className="text-[10.5px] text-muted leading-snug">
              Refs shape the vibe. Fresh composition.
            </p>
          </button>
          <button
            onClick={() => changeMode("template_fill")}
            className={[
              "rounded-xl border-2 p-2.5 text-left transition-all",
              mode === "template_fill"
                ? "border-accent bg-accent/5 shadow-sm"
                : "border-border bg-bg-muted/30 hover:border-fg/30 hover:bg-bg-muted/60",
            ].join(" ")}
          >
            <div className="flex items-center gap-1.5 mb-0.5">
              <ImageFrameIcon className={mode === "template_fill" ? "w-4 h-4 text-accent" : "w-4 h-4 text-muted"} />
              <span className="font-semibold text-xs text-fg">Template + portrait</span>
              {mode === "template_fill" && (
                <span className="ml-auto text-accent"><CheckIcon /></span>
              )}
            </div>
            <p className="text-[10.5px] text-muted leading-snug">
              Saved poster as design + real face.
            </p>
          </button>
        </div>

        <label className="block">
          <span className="eyebrow mb-1.5 block">What's the post about?</span>
          <div className="relative">
            <textarea
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="e.g. Piscine applications now open. Focused on ambitious Emirati students."
              rows={3}
              disabled={enhancingTopic}
              className="w-full rounded-lg border border-border bg-bg-subtle px-3 py-2 pr-24 text-sm focus:outline-none focus:border-accent resize-none disabled:opacity-60"
            />
            <button
              type="button"
              onClick={enhanceTopic}
              disabled={enhancingTopic || topic.trim().length === 0}
              title="Rewrite into a brand-on-tone headline + subtitle"
              className="absolute bottom-2 right-2 inline-flex items-center gap-1 rounded-md border border-border bg-bg px-2 py-1 text-2xs font-medium text-fg hover:bg-bg-muted disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {enhancingTopic ? (
                <>
                  <span className="inline-block w-3 h-3 border-2 border-current border-t-transparent rounded-full animate-spin" />
                  Polishing…
                </>
              ) : (
                <>
                  <SparkleIcon className="w-3.5 h-3.5" />
                  Enhance
                </>
              )}
            </button>
          </div>
        </label>

        <FormatPicker options={POSTER_FORMATS} value={aspect} onChange={setAspect} />

        {mode === "style_inspired" && (
          /* References — up to 2 (style DNA). Compact horizontal layout. */
          <div>
            <p className="eyebrow mb-1.5">References <span className="text-muted normal-case font-normal">(up to {MAX_REFS}, optional)</span></p>
            <div className="flex items-center gap-2 flex-wrap">
              {refs.map((ref, idx) => (
                <div
                  key={idx}
                  className="relative w-16 h-16 rounded-lg overflow-hidden border border-border bg-black group"
                >
                  <img
                    src={ref.thumbnailUrl}
                    alt={ref.displayName || `Reference ${idx + 1}`}
                    className="w-full h-full object-cover"
                  />
                  <button
                    onClick={() => removeRef(idx)}
                    className="absolute top-0.5 right-0.5 w-5 h-5 rounded-full bg-black/70 text-white flex items-center justify-center opacity-80 hover:opacity-100"
                    aria-label="Remove reference"
                  >
                    <XIcon className="w-3 h-3" />
                  </button>
                </div>
              ))}

              {refs.length < MAX_REFS && (
                <div className="relative">
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      setAddMenuOpen((v) => !v);
                    }}
                    className="w-16 h-16 rounded-lg border-2 border-dashed border-border bg-bg-muted/30 hover:bg-bg-muted/60 hover:border-accent flex items-center justify-center text-muted transition-colors"
                    aria-label="Add reference"
                  >
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"
                      strokeLinecap="round" className="w-6 h-6" aria-hidden>
                      <path d="M12 5v14M5 12h14"/>
                    </svg>
                  </button>
                  {addMenuOpen && (
                    <div
                      onClick={(e) => e.stopPropagation()}
                      className="absolute left-0 top-[68px] z-30 bg-bg border border-border rounded-lg shadow-xl py-1 min-w-[200px]"
                    >
                      <button
                        onClick={handleAddUpload}
                        className="flex items-center gap-2 w-full text-left px-3 py-2 text-sm hover:bg-bg-muted transition-colors"
                      >
                        <UploadIcon className="w-4 h-4 text-muted" />
                        Upload from file
                      </button>
                      <button
                        onClick={handleAddAsset}
                        className="flex items-center gap-2 w-full text-left px-3 py-2 text-sm hover:bg-bg-muted transition-colors"
                      >
                        <GridFolderIcon className="w-4 h-4 text-muted" />
                        Choose from Assets
                      </button>
                    </div>
                  )}
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept="image/jpeg,image/png,image/webp"
                    className="hidden"
                    onChange={handleFileChosen}
                  />
                </div>
              )}
            </div>
            <p className="text-3xs text-muted mt-1.5 leading-snug">
              The AI extracts the vibe (palette, illustration style, mood) — never copies the references.
            </p>
          </div>
        )}

        {mode === "template_fill" && (
          /* Two compact slots — fixed height instead of aspect-square */
          <div className="space-y-2">
            <div className="grid grid-cols-2 gap-2">
              {/* Template slot */}
              <div>
                <p className="eyebrow mb-1">Template</p>
                {templateAsset ? (
                  <div className="relative h-32 rounded-lg overflow-hidden border-2 border-accent bg-black">
                    <img
                      src={templateAsset.thumbnailUrl}
                      alt={templateAsset.displayName || "Template"}
                      className="w-full h-full object-cover"
                    />
                    <button
                      onClick={() => setTemplateAsset(null)}
                      className="absolute top-1 right-1 w-5 h-5 rounded-full bg-black/70 text-white flex items-center justify-center"
                      aria-label="Remove template"
                    >
                      <XIcon className="w-3 h-3" />
                    </button>
                  </div>
                ) : (
                  <button
                    onClick={() => setPickerTarget("template")}
                    className="w-full h-32 rounded-lg border-2 border-dashed border-border bg-bg-muted/30 hover:bg-bg-muted/60 hover:border-accent flex flex-col items-center justify-center text-muted transition-colors gap-1"
                  >
                    <GridFolderIcon className="w-6 h-6" />
                    <span className="text-2xs font-medium">Pick from Assets</span>
                  </button>
                )}
              </div>

              {/* Student photo slot — a disk upload OR a saved Human Character. */}
              <div>
                <p className="eyebrow mb-1">Student photo</p>
                {(studentUpload || studentAsset) ? (
                  <div className="relative h-32 rounded-lg overflow-hidden border-2 border-accent bg-black">
                    <img
                      src={(studentUpload ?? studentAsset)!.thumbnailUrl}
                      alt={(studentUpload ?? studentAsset)!.displayName || "Student"}
                      className="w-full h-full object-cover"
                    />
                    {studentAsset && (
                      <span className="absolute bottom-1 left-1 px-1.5 py-0.5 rounded bg-black/70 text-white text-[9px] font-medium">
                        Human Character
                      </span>
                    )}
                    <button
                      onClick={() => { setStudentUpload(null); setStudentAsset(null); }}
                      className="absolute top-1 right-1 w-5 h-5 rounded-full bg-black/70 text-white flex items-center justify-center"
                      aria-label="Remove student photo"
                    >
                      <XIcon className="w-3 h-3" />
                    </button>
                  </div>
                ) : (
                  <div className="h-32 grid grid-rows-2 gap-1">
                    <button
                      onClick={() => studentFileInputRef.current?.click()}
                      className="w-full rounded-lg border-2 border-dashed border-border bg-bg-muted/30 hover:bg-bg-muted/60 hover:border-accent flex flex-col items-center justify-center text-muted transition-colors gap-0.5"
                    >
                      <UploadIcon className="w-5 h-5" />
                      <span className="text-[10.5px] font-medium">Upload photo</span>
                    </button>
                    <button
                      onClick={() => setPickerTarget("student")}
                      className="w-full rounded-lg border-2 border-dashed border-border bg-bg-muted/30 hover:bg-bg-muted/60 hover:border-accent flex flex-col items-center justify-center text-muted transition-colors gap-0.5"
                    >
                      <GridFolderIcon className="w-5 h-5" />
                      <span className="text-[10.5px] font-medium">Human Character</span>
                    </button>
                  </div>
                )}
                <input
                  ref={studentFileInputRef}
                  type="file"
                  accept="image/jpeg,image/png,image/webp"
                  className="hidden"
                  onChange={handleStudentFileChosen}
                />
              </div>
            </div>
            <p className="text-3xs text-muted leading-snug">
              Template design preserved · student's face preserved · headline updated to match topic.
            </p>
          </div>
        )}

        <div className="pt-1">
          <button
            onClick={runGenerate}
            disabled={!canGenerate}
            className={[
              "w-full rounded-xl px-5 py-4 text-base font-semibold transition-all",
              "shadow-sm hover:shadow-md active:shadow-sm",
              canGenerate
                ? "bg-fg text-bg hover:bg-fg/90"
                : "bg-bg-muted text-muted cursor-not-allowed",
            ].join(" ")}
          >
            {phase === "generating" ? (
              <span className="inline-flex items-center justify-center gap-2">
                <span className="inline-block w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin" />
                Generating poster…
              </span>
            ) : (
              <>{hasResult ? "Generate fresh poster" : "Generate poster"}</>
            )}
          </button>
          {phase === "idle" && (
            <p className="text-2xs text-muted text-center mt-1.5">
              {(() => {
                const cost = "~$0.13";
                const time = mode === "template_fill" ? "about 40s" : "about 20s";
                const note = mode === "template_fill"
                  ? "template design preserved, student face preserved"
                  : "fresh on-brand composition";
                return `${cost} · ${time} · Gemini Pro · ${note}`;
              })()}
            </p>
          )}
          {phase === "generating" && (
            <JobProgress className="mt-2" startedAt={startedAt} kind="poster_human" />
          )}
        </div>
        {error && (
          <p className="text-xs text-danger" role="alert">{error}</p>
        )}
      </section>
      {/* === END LEFT COLUMN === */}

      {/* === RIGHT COLUMN: Result (or empty placeholder) === */}
      <div className="space-y-4 min-w-0">

      {/* Empty state — before any generation this session */}
      {!hasResult && phase !== "generating" && (
        <section className="card p-10 text-center border-dashed flex flex-col items-center justify-center min-h-[400px]">
          <div className="text-muted mb-3"><CanvasIcon className="w-12 h-12" /></div>
          <p className="text-sm text-fg font-medium">Your posters will appear here</p>
          <p className="text-xs text-muted mt-1 max-w-sm mx-auto leading-relaxed">
            Add a topic
            {mode === "template_fill" ? ", pick a template, upload a student photo," : (refs.length === 0 ? " (references are optional)" : "")}
            {" "}and hit Generate — you'll get 4 variants.
          </p>
        </section>
      )}

      {/* 2x2 grid — 4 variants per Generate; hover a tile for per-card actions. */}
      {(hasResult || phase === "generating") && (
        <section className="card p-4 space-y-3">
          <div className="flex items-center gap-2 flex-wrap">
            <p className="eyebrow">Generated posters</p>
            {exploreDepth > 1 && (
              <button
                onClick={() => setBatchStack((prev) => prev.slice(0, -1))}
                className="btn btn-ghost text-xs inline-flex items-center gap-1"
              >
                <ChevronLeftIcon className="w-3.5 h-3.5" /> Back to grid
              </button>
            )}
            <span className="text-xs text-muted ml-auto">
              {phase === "generating"
                ? "Rendering 4 variants…"
                : exploreDepth > 1
                  ? "Explore riffs — hover for actions"
                  : "Hover a tile: Explore · Save · Remove logo · Use"}
            </span>
          </div>
          <div className="grid grid-cols-2 gap-3">
            {phase === "generating"
              ? Array.from({ length: 4 }).map((_, i) => (
                  <div
                    key={i}
                    className={`rounded-lg border border-border bg-bg-muted ${aspectClass(aspect, "1:1")} flex items-center justify-center`}
                  >
                    <span className="inline-block w-8 h-8 border-2 border-accent border-t-transparent rounded-full animate-spin" />
                  </div>
                ))
              : currentBatch.map((g) => (
                  <VariantCard
                    key={g.id}
                    gen={g}
                    saving={savingId === g.id}
                    removing={removingId === g.id}
                    exploring={exploringId === g.id}
                    canExplore={exploreDepth <= 1}
                    onSave={() => handleSaveCard(g)}
                    onRemoveLogo={() => handleRemoveLogo(g)}
                    onExplore={() => handleExplore(g)}
                    onContinue={
                      onContinue
                        ? () =>
                            onContinue({
                              refId: g.id,
                              mediaType: "image",
                              mediaUrl: api.yasProducer.posterHuman.fileUrl(g.id),
                              topic: g.topic || "",
                            })
                        : undefined
                    }
                  />
                ))}
          </div>
        </section>
      )}

      </div>
      {/* === END RIGHT COLUMN === */}
      </div>
      {/* === END TWO-COLUMN GRID === */}

      {/* Picker — mode-aware: refs cap 2 in style_inspired, template cap 1 in template_fill */}
      {pickerOpen && (
        <AssetPickerModal
          contentCategory={pickerTarget === "student" ? "img_human_character" : CONTENT_CATEGORY}
          maxSelection={pickerTarget === "template" || pickerTarget === "student" ? 1 : (MAX_REFS - refs.length)}
          alreadySelectedAssetIds={
            pickerTarget === "template"
              ? (templateAsset ? [templateAsset.assetId] : [])
              : pickerTarget === "student"
                ? (studentAsset ? [studentAsset.assetId] : [])
                : refs.filter((r) => r.kind === "asset").map((r: any) => r.assetId)
          }
          onPick={handleAssetsPicked}
          onSkip={pickerTarget === "refs" ? () => setPickerTarget(null) : undefined}
          onClose={() => setPickerTarget(null)}
        />
      )}
    </div>
  );
}
