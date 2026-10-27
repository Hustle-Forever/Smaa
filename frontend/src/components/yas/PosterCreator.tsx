/**
 * Yas Producer · Poster-only mode (Yasin's path).
 *
 * Two-stage voxel pipeline (single-path, one-shot):
 *   1. gpt-4o expands the topic into a retro-voxel scene description.
 *   2. gemini-3-pro-image renders it — the real 42 logo + optional references
 *      are sent to the model as image parts (the logo is baked in, not pasted).
 *
 *   - No engine toggle, no template-fill, no animate, no retry chain.
 *   - Server-side rate-limited (20/hour).
 *
 * UX:
 *   topic textarea + Enhance button
 *   reference picker (1-3 from saved img_poster assets, optional)
 *     + Style/Background toggle when references are attached
 *   Generate → shows the result
 *   Save to Assets
 *   history strip across the bottom
 *   24h rehydrate via /poster/recent
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { JobProgress } from "../ui/JobProgress";

import { api } from "../../lib/api";
import { POSTER_FORMATS, aspectClass, aspectFromDims } from "../../lib/aspect";
import { FormatPicker } from "./FormatPicker";
import { fmtRel, humanError } from "../../lib/format";
import type { ProducerBrief } from "../../lib/producerBrief";
import { useProducerAssetsStore, useYasLibraryStore } from "../../lib/store";
import { usePersistentState } from "../../lib/usePersistentState";
import { useRevealPanel } from "../../lib/useRevealPanel";
import { useConfirm } from "../ConfirmProvider";
import { useToast } from "../ToastProvider";
import type {
  ProducerAssetRow,
  YasImageUsage,
  YasPosterAspect,
  YasPosterOnlyGeneration,
  YasPosterStyle,
} from "../../lib/types";
import { AssetPickerModal } from "./AssetPickerModal";
import { EnhanceButton } from "./EnhanceButton";
import type { ContinueToPreview } from "./roadmap/types";

const CONTENT_CATEGORY = "img_poster"; // references come from the static-poster bucket
const MAX_REFS = 3;

// Poster look, and the one toggle that matters here: VOXEL and VOXEL CLASSIC are
// the same drawing style fed by two different prompts. Classic still carries the
// v1 scene recipe (a named universe of retro-gaming props, staged on a base);
// voxel dropped it so Stage 1 invents the scene. Both stay pickable so they can
// be judged on the same topic, and each poster records which one drew it.
// Pattern + popup are separate, flat graphic styles.
// Keep in sync with the backend registry (poster_styles.py).
const STYLES: { key: YasPosterStyle; label: string; short: string; hint: string }[] = [
  { key: "voxel", label: "Voxel", short: "voxel", hint: "3D blocks. What is in the picture is left to the writing step, so two topics rarely look alike." },
  { key: "voxel_classic", label: "Voxel classic", short: "classic", hint: "The original recipe: retro gaming props (arcade machines, keyboards, monitors) staged like a toy set. Same style, more consistent, more repetitive." },
  { key: "pattern", label: "Pattern", short: "pattern", hint: "Bold full-bleed brand pattern (Ghutra / Map) on a two-color split." },
  { key: "popup", label: "Pop-up", short: "pop-up", hint: "Code-centric OS-window event card (\u25cf\u25cf\u25cf bar, <tag/> titles), carrying the headline, date, details and a CTA button." },
];

// Which prompt drew a given poster, for the history strip. Two posters of the
// same topic only tell you something if you can see which brain made each one.
const STYLE_BY_KEY = new Map(STYLES.map((s) => [s.key, s]));

// Output shape. 1:1 is the default and what every poster before this was.
// These are REAL shapes, not requests: the backend sends the ratio as a Gemini
// API parameter (ImageConfig), which is the only thing that actually forces it.
// The list, the picker and the pixel reader are shared with Create Poster +
// Human and Human Character; see lib/aspect.ts and FormatPicker.
const aspectOf = (gen: { width?: number | null; height?: number | null }) =>
  aspectFromDims(gen, POSTER_FORMATS, "1:1");

// ---------------------------------------------------------------------------
// Minimal inline icons (same style as PosterHumanCreator — 1.75-2px stroke,
// rounded caps, currentColor). Keeping inline so we don't add a dependency.
// ---------------------------------------------------------------------------

type IconProps = { className?: string };

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
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <rect x="3" y="3" width="18" height="18" rx="2.5"/>
      <path d="M3 16l5-5 4 4 3-3 6 6"/>
      <circle cx="8.5" cy="8.5" r="1.5"/>
    </svg>
  );
}
// "No logo" — a ban/no-symbol glyph for the Remove-logo action.

type RefItem =
  | { kind: "asset"; assetId: number; thumbnailUrl: string; displayName?: string }
  | { kind: "upload"; uploadId: number; thumbnailUrl: string; displayName?: string };

// ---------------------------------------------------------------------------
// One cell in the 2×2 variant grid. Hover reveals Explore (→ 4 more from this
// one) and Save (→ Assets library). Failed renders fall back to a red panel.
// ---------------------------------------------------------------------------

function VariantCard({
  gen,
  saving,
  removingLogo,
  refining,
  disabled,
  canExplore,
  onExplore,
  onSave,
  onRemoveLogo,
  onRefine,
  animating,
  onAnimate,
  onUse,
  pendingAspect,
}: {
  gen: YasPosterOnlyGeneration;
  saving: boolean;
  removingLogo: boolean;
  /** True while THIS card is the one being refined (shows a spinner label). */
  refining: boolean;
  disabled: boolean;
  /** Explore is one level only — hidden on explored (depth ≥ 1) variants. */
  canExplore: boolean;
  onExplore: () => void;
  onSave: () => void;
  /** Re-render this poster with the 42 logo removed (kept as a new card). */
  onRemoveLogo: () => void;
  /** Select THIS poster for a guided refine (opens the prompt panel below). */
  onRefine: () => void;
  /** True while THIS poster is being handed to Create Animation. */
  animating: boolean;
  /** Send THIS poster to Create Animation. Absent when standalone (outside the
   *  roadmap), where there is no animation step to continue into. */
  onAnimate?: () => void;
  /** Roadmap: pick THIS variant and continue to Preview. Absent when standalone. */
  onUse?: () => void;
  /** The shape to hold BEFORE the pixels exist (generating / failed), which is
   *  whatever was asked for. A ready card measures itself instead. */
  pendingAspect: YasPosterAspect;
}) {
  const isReady = gen.status === "ready" || gen.status === "saved_to_assets";
  const isSaved = gen.status === "saved_to_assets";
  // A hardcoded aspect-square here would letterbox every 9:16 poster into a
  // square box and crop it (object-cover), which is the exact bug lib/aspect.ts
  // was written to end.
  const shape = aspectClass(
    gen.width && gen.height ? aspectOf(gen) : pendingAspect,
    "1:1",
  );

  if (gen.status === "failed") {
    return (
      <div className={`${shape} rounded-lg bg-danger/10 border border-danger/30 flex flex-col items-center justify-center p-3 text-center`}>
        <span className="font-mono text-3xs px-1.5 py-0.5 rounded bg-danger/15 text-danger uppercase tracking-wider mb-1">
          failed
        </span>
        <p className="text-2xs text-danger leading-snug line-clamp-3">
          {gen.error || "Generation failed."}
        </p>
      </div>
    );
  }

  if (!isReady) {
    return (
      <div className={`${shape} rounded-lg bg-bg-muted flex items-center justify-center`}>
        <span className="inline-block w-6 h-6 border-2 border-accent border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className={`group relative ${shape} rounded-lg overflow-hidden bg-black`}>
      <img
        src={api.yasProducer.posterOnly.fileUrl(gen.id, gen.created_at)}
        alt={gen.topic || `Variant ${gen.id}`}
        className="w-full h-full object-contain"
      />
      {isSaved && (
        <span className="absolute top-1.5 right-1.5 w-6 h-6 rounded-full bg-success text-white flex items-center justify-center shadow text-xs z-10">
          ✓
        </span>
      )}
      {/* Hover action bar */}
      <div className="absolute inset-x-0 bottom-0 p-2 flex flex-wrap items-center gap-1.5 bg-gradient-to-t from-black/80 to-transparent opacity-0 group-hover:opacity-100 transition-opacity">
        {canExplore && (
          <button
            onClick={onExplore}
            disabled={disabled}
            title="Variations of this one — 4 close riffs on the chosen poster"
            className="flex-1 rounded-md bg-white/95 text-black text-xs font-semibold px-2 py-1.5 hover:bg-white disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            Explore →
          </button>
        )}
        <button
          onClick={onSave}
          disabled={disabled || isSaved}
          title={isSaved ? "Already saved" : "Save to Assets"}
          className={`${canExplore ? "" : "flex-1 "}rounded-md bg-black/60 text-white text-xs font-medium px-2.5 py-1.5 hover:bg-black/80 disabled:opacity-50 disabled:cursor-not-allowed transition-colors`}
        >
          {saving ? "Saving…" : isSaved ? "Saved" : "Save"}
        </button>
        <button
          onClick={onRefine}
          disabled={disabled}
          title="Refine this poster — describe a change and re-render it (kept as a new version)"
          className="rounded-md bg-black/60 text-white text-xs font-medium px-2.5 py-1.5 hover:bg-black/80 disabled:opacity-50 disabled:cursor-not-allowed transition-colors inline-flex items-center gap-1.5"
        >
          {refining ? (
            <><span className="inline-block w-3.5 h-3.5 border-2 border-current border-t-transparent rounded-full animate-spin" />Refining…</>
          ) : (
            "Refine"
          )}
        </button>
        {onAnimate && (
          <button
            onClick={onAnimate}
            disabled={disabled}
            title="Animate this poster: takes it into Create Animation, where you pick a length and carry on to the caption"
            className="rounded-md bg-black/60 text-white text-xs font-medium px-2.5 py-1.5 hover:bg-black/80 disabled:opacity-50 disabled:cursor-not-allowed transition-colors inline-flex items-center gap-1.5"
          >
            {animating ? (
              <><span className="inline-block w-3.5 h-3.5 border-2 border-current border-t-transparent rounded-full animate-spin" />Opening…</>
            ) : (
              "Animate"
            )}
          </button>
        )}
        <button
          onClick={onRemoveLogo}
          disabled={disabled}
          title="Re-render this poster without the 42 logo (kept as a new version)"
          className="rounded-md bg-black/60 text-white text-xs font-medium px-2.5 py-1.5 hover:bg-black/80 disabled:opacity-50 disabled:cursor-not-allowed transition-colors inline-flex items-center gap-1.5"
        >
          {removingLogo ? (
            <><span className="inline-block w-3.5 h-3.5 border-2 border-current border-t-transparent rounded-full animate-spin" />Removing…</>
          ) : (
            "Remove logo"
          )}
        </button>
        {onUse && (
          <button
            onClick={onUse}
            disabled={disabled}
            title="Use this poster — continue to Preview"
            className="rounded-md bg-accent text-white text-xs font-semibold px-2.5 py-1.5 hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            Use →
          </button>
        )}
      </div>
    </div>
  );
}

interface Props {
  onBack: () => void;
  /** Roadmap: hand the chosen poster to the Preview step. Absent when standalone. */
  onContinue?: ContinueToPreview;
  /** Phase C: reopen a specific past poster (by id) from the Library, pre-loaded
   *  into the ready/refine view. Wins over the most-recent rehydrate. */
  initialRunId?: number | null;
  /** Deep-link: report the on-screen 2×2 batch's generation ids up to the
   *  ProducerRoadmap so it can encode a shareable full-grid URL. */
  onBatch?: (ids: number[]) => void;
  /** Deep-link: restore a full 2×2 grid on resume (superset of initialRunId).
   *  When >1 id, all ids are fetched in parallel and re-hung as one batch. */
  initialRunIds?: number[] | null;
  /** Trend Finder "Make this" seed. One-shot, applied at mount only — the
   *  roadmap bumps projectNonce for every hand-off, so this component is always
   *  freshly mounted when a brief arrives. Never set together with
   *  initialRunId (a resume wins; the roadmap clears resumeRunId first). */
  initialBrief?: ProducerBrief | null;
  /** Roadmap: "Animate" on a finished poster. The poster is imported as an
   *  animation frame here, and the roadmap opens Create Animation on it, so the
   *  clip carries on through Finalization → Scheduling → Approval like any
   *  other animation. Absent when standalone → the button is hidden. */
  onAnimate?: (animId: number) => void;
}

export function PosterCreator({
  onBack,
  onContinue,
  initialRunId = null,
  onBatch,
  initialRunIds = null,
  initialBrief = null,
  onAnimate,
}: Props) {
  // Draft inputs persist to localStorage so a full refresh (F5) keeps them.
  const [topic, setTopic] = usePersistentState("yas.poster.topic", "");
  const [refs, setRefs] = usePersistentState<RefItem[]>("yas.poster.refs", []);
  const [imageUsage, setImageUsage] = usePersistentState<YasImageUsage>("yas.poster.imageUsage", "style");
  const [style, setStyle] = usePersistentState<YasPosterStyle>("yas.poster.style", "voxel");
  const [aspect, setAspect] = usePersistentState<YasPosterAspect>("yas.poster.aspect", "1:1");
  const [attempts, setAttempts] = useState<YasPosterOnlyGeneration[]>([]);
  // Midjourney-style navigation: each entry is one 2×2 batch of 4 variants.
  // The last entry is the batch currently on screen; earlier entries are the
  // ancestors you drilled through (breadcrumb / Back walk this stack).
  //
  // Persisted (not plain useState) so the on-canvas result survives leaving to
  // another tab AND a full refresh — same lifetime as the typed draft above.
  // Without this, navigating away dropped the batch you generated, and the
  // mount-time recent() rehydrate would re-attach an UNRELATED most-recent
  // render under your freshly-typed topic (looked like "it remembered my old
  // topic"). Pairs with the per-batch "Results for" label below.
  const [batchStack, setBatchStack] = usePersistentState<YasPosterOnlyGeneration[][]>(
    "yas.poster.batchStack",
    [],
  );
  // True while a Library resume is still loading its poster. Until then the
  // batch restored above belongs to an EARLIER session, so it is neither shown
  // nor reported: reporting it mirrored its ids into the URL, and App's
  // hashchange (which fires after that mirror) re-routed the resume onto the
  // old poster — "Continue editing" opened the last poster instead of this one.
  const [resuming, setResuming] = useState(initialRunId != null);
  const [phase, setPhase] = useState<"idle" | "generating">("idle");

  // When the run started, so JobProgress can show elapsed time. Derived from the
  // phase flip rather than threaded through every call site that starts a job.
  const [startedAt, setStartedAt] = useState(0);
  useEffect(() => {
    if (phase === "generating") setStartedAt(Date.now());
  }, [phase]);
  const [savingId, setSavingId] = useState<number | null>(null);
  const [removingLogoId, setRemovingLogoId] = useState<number | null>(null);
  // Refine: which card is selected for a guided edit, the change text, and the id
  // currently rendering. Session-only (NOT persisted) — a refine draft shouldn't
  // survive a refresh, so ProducerRoadmap's draft-key wipe needs no update.
  const [refineTargetId, setRefineTargetId] = useState<number | null>(null);
  const [refineText, setRefineText] = useState("");
  const [refiningId, setRefiningId] = useState<number | null>(null);
  // The panel sits under the 2x2 grid, which on most screens is off the bottom
  // of the window. Opening it therefore also has to take the user to it.
  const refinePanel = useRevealPanel<HTMLDivElement, HTMLTextAreaElement>();
  // The poster being handed to Create Animation, for its button spinner.
  const [animatingId, setAnimatingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [enhancingTopic, setEnhancingTopic] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [addMenuOpen, setAddMenuOpen] = useState(false);

  // Re-entrance lock for the Generate button — closes the synchronous
  // double-click hole between onClick and setPhase committing to state.
  const generateLockRef = useRef(false);
  // A batch now answers with its four `generating` cards as soon as the
  // concepts exist; the grey placeholders only cover the wait for that answer.
  const [awaitingBatch, setAwaitingBatch] = useState(false);
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const toast = useToast();
  const confirm = useConfirm();
  const assetsStore = useProducerAssetsStore();

  // Trend Finder "Make this" — seed the composer with the AI-written brief.
  // Mount-only: the roadmap remounts this creator (projectNonce) for every
  // hand-off. Writing through the persisted topic also saves it as the draft,
  // which is correct — the seed IS the draft.
  useEffect(() => {
    if (!initialBrief || initialRunId != null) return;
    setTopic(initialBrief.topic);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Rehydrate on mount. Skipped when resuming a specific id (the effect below
  // owns the view) so it can't clobber the targeted poster. A Trend Finder
  // brief does NOT skip the whole effect — this is also the only unconditional
  // loader of the 24h history strip, so bailing here left the strip empty for
  // the rest of the session. Only the adopt-a-past-topic branch below is
  // skipped (it is async, so the skip is explicit rather than relying on the
  // `cur.trim()` guard).
  useEffect(() => {
    if (initialRunId != null) return;
    let cancelled = false;
    // Captured at mount from the synchronously-restored persisted batch: was the
    // user already mid-session (warm start) or starting clean (cold start)?
    const hadPersistedBatch = batchStack.length > 0;
    api.yasProducer.posterOnly
      .recent()
      .then((r) => {
        if (cancelled) return;
        const list = r.items || [];
        // History strip always reflects the backend's last 24h.
        setAttempts(list);
        // A Trend Finder brief owns the composer — the strip above still loads,
        // but nothing below may overwrite the seeded topic.
        if (initialBrief) return;
        // Warm start: the canvas (and its matching topic) were already restored
        // from localStorage — leave them alone. Seeding an unrelated "most
        // recent" render here is exactly what made an old poster appear under a
        // newly-typed topic.
        if (hadPersistedBatch) return;
        // Cold start: show the most-recent ready poster AND adopt its own topic,
        // so the canvas and composer always describe the same thing.
        const resume = list.find((g) => g.status === "ready") || null;
        if (!resume) return;
        setBatchStack([[resume]]);
        setTopic((cur) => (cur.trim() ? cur : resume.topic || ""));
      })
      .catch(() => {
        /* non-fatal */
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Phase C — reopen a specific past poster (by id) from the Library, pre-loaded
  // into the ready/refine view (single-card batch). Takes precedence over the
  // most-recent rehydrate above.
  //
  // Deep-link full-grid restore: when initialRunIds carries more than one id
  // (a shared 2×2 URL), fetch them ALL in parallel via the single-generation GET
  // (no age filter), drop any that 404, and re-hang them as ONE batch preserving
  // id order. A single id (Library "Continue" / legacy) keeps the single-card path.
  useEffect(() => {
    if (initialRunId == null) {
      setResuming(false);
      return;
    }
    let cancelled = false;
    const ids =
      initialRunIds && initialRunIds.length > 1 ? initialRunIds : [initialRunId];
    if (ids.length > 1) {
      Promise.all(
        ids.map((id) =>
          api.yasProducer.posterOnly.fetch(id).catch(() => null),
        ),
      )
        .then((results) => {
          if (cancelled) return;
          const gens = results.filter(
            (g): g is YasPosterOnlyGeneration => g != null,
          );
          if (!gens.length) return;
          setBatchStack([[...gens]]); // full grid → lands in the ready/refine view
          setAttempts((prev) => {
            const seen = new Set(prev.map((x) => x.id));
            const fresh = gens.filter((g) => !seen.has(g.id));
            return fresh.length ? [...fresh, ...prev] : prev;
          });
          const head = gens[0];
          setTopic(head.topic || "");
          if (head.style) setStyle(head.style);
          if (head.image_usage) setImageUsage(head.image_usage);
          // The reopened poster's own pixels decide the picker, so a follow-up
          // Generate keeps the shape the person was working in.
          setAspect(aspectOf(head));
        })
        .catch(() => {
          /* non-fatal — stays on the composer */
        })
        .finally(() => {
          if (!cancelled) setResuming(false);
        });
    } else {
      api.yasProducer.posterOnly
        .fetch(initialRunId)
        .then((g) => {
          if (cancelled || !g) return;
          setBatchStack([[g]]); // single-card batch → lands in the ready/refine grid
          setAttempts((prev) =>
            prev.some((x) => x.id === g.id) ? prev : [g, ...prev],
          );
          setTopic(g.topic || "");
          if (g.style) setStyle(g.style);
          if (g.image_usage) setImageUsage(g.image_usage);
          setAspect(aspectOf(g));
        })
        .catch(() => {
          /* non-fatal — stays on the composer */
        })
        .finally(() => {
          if (!cancelled) setResuming(false);
        });
    }
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialRunId, initialRunIds]);

  useEffect(() => {
    if (!addMenuOpen) return;
    function onDocClick() {
      setAddMenuOpen(false);
    }
    const t = setTimeout(() => document.addEventListener("click", onDocClick), 0);
    return () => {
      clearTimeout(t);
      document.removeEventListener("click", onDocClick);
    };
  }, [addMenuOpen]);

  // The batch currently on screen (last entry of the stack).
  const currentBatch = useMemo(
    () => (!resuming && batchStack.length ? batchStack[batchStack.length - 1] : []),
    [batchStack, resuming],
  );
  const hasResult = currentBatch.length > 0;

  // Deep-link: whenever the visible top batch changes, report its generation ids
  // up to ProducerRoadmap so it can encode a shareable full-grid URL. Guarded on
  // batchStack (our OWN state) exactly like TalkingHeadV2Creator's onRunId effect;
  // skip while the grid is empty so we never emit an empty batch.
  useEffect(() => {
    if (resuming) return;
    const top = batchStack.length ? batchStack[batchStack.length - 1] : [];
    if (!top.length) return;
    onBatch?.(top.map((g) => g.id));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchStack, resuming]);

  // The card selected for refine, resolved against the on-screen batch. If the
  // selection isn't in the current batch (e.g. you navigated the breadcrumb away),
  // the panel hides automatically.
  const refineTarget =
    refineTargetId == null
      ? null
      : currentBatch.find((c) => c.id === refineTargetId) ?? null;

  // What's on the canvas is pinned to the batch's OWN topic (each record carries
  // it), NOT the editable composer field — so a leftover render can never look
  // like it belongs to a topic you've since retyped. While rendering, show the
  // topic actually being generated.
  const resultTopic =
    phase === "generating" ? topic.trim() : currentBatch[0]?.topic || "";

  const canGenerate = phase === "idle" && topic.trim().length > 0;

  // Patch a single generation everywhere it appears (current view + history).
  function patchGeneration(id: number, patch: Partial<YasPosterOnlyGeneration>) {
    setBatchStack((stack) =>
      stack.map((batch) =>
        batch.map((g) => (g.id === id ? { ...g, ...patch } : g)),
      ),
    );
    setAttempts((prev) => prev.map((g) => (g.id === id ? { ...g, ...patch } : g)));
  }

  // After a reload in the middle of a batch the restored cards are still
  // `generating`: keep watching them (and keep Generate locked meanwhile).
  useEffect(() => {
    const top = batchStack.length ? batchStack[batchStack.length - 1] : [];
    const ids = top.filter((g) => g.status === "generating").map((g) => g.id);
    if (!ids.length || generateLockRef.current) return;
    generateLockRef.current = true;
    setPhase("generating");
    void watchBatch(ids).finally(() => {
      generateLockRef.current = false;
      if (aliveRef.current) setPhase("idle");
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Watch a background batch: every few seconds re-read the cards still
  // `generating`, and drop each one in as it turns ready (or failed). Ends when
  // all of them have ended, or after six minutes (a card still going then is
  // marked so nobody waits on it forever). Resolves with the final rows.
  async function watchBatch(ids: number[]): Promise<YasPosterOnlyGeneration[]> {
    const POLL_MS = 3000;
    const GIVE_UP_MS = 6 * 60_000;
    const started = Date.now();
    const latest = new Map<number, YasPosterOnlyGeneration>();
    let pending = new Set(ids);
    while (pending.size && aliveRef.current) {
      await new Promise((r) => setTimeout(r, POLL_MS));
      const rows = await Promise.all(
        [...pending].map((id) => api.yasProducer.posterOnly.fetch(id).catch(() => null)),
      );
      for (const g of rows) {
        if (!g) continue;
        if (g.status === "generating") {
          // A card older than the give-up window is one a restart orphaned
          // (a reload can bring those back): end it now, not six minutes on.
          const age = Date.now() - new Date(g.created_at).getTime();
          if (!(age > GIVE_UP_MS)) continue;
          patchGeneration(g.id, { status: "failed", error: "This poster stopped before it finished. Try again." });
          pending.delete(g.id);
          continue;
        }
        pending.delete(g.id);
        latest.set(g.id, g);
        patchGeneration(g.id, g);
      }
      if (pending.size && Date.now() - started > GIVE_UP_MS) {
        for (const id of pending) {
          patchGeneration(id, { status: "failed", error: "This poster took too long. Try again." });
        }
        pending = new Set();
      }
    }
    // The batch's cost and set id are stamped just after its last picture:
    // read the rows once more so the cards and the toast carry them.
    if (aliveRef.current) {
      await new Promise((r) => setTimeout(r, 1500));
      const rows = await Promise.all(
        ids.map((id) => api.yasProducer.posterOnly.fetch(id).catch(() => null)),
      );
      for (const g of rows) {
        if (!g) continue;
        latest.set(g.id, g);
        patchGeneration(g.id, g);
      }
    }
    return ids.map((id) => latest.get(id)).filter((g): g is YasPosterOnlyGeneration => !!g);
  }

  // When a poster is deleted from the Library while this creator is still mounted
  // (co-mounted across tab switches), drop it from our OWN local history strip
  // and the on-canvas grid — otherwise a deleted tile lingers and clicking it
  // 404s on resume. The signal is a fresh object per delete, so this re-fires.
  const lastRemoved = useYasLibraryStore((s) => s.lastRemoved);
  useEffect(() => {
    if (!lastRemoved || lastRemoved.type !== "poster") return;
    const delId = lastRemoved.id;
    setAttempts((prev) => prev.filter((g) => g.id !== delId));
    setBatchStack((stack) => {
      const next = stack.map((batch) => batch.filter((g) => g.id !== delId));
      // Trim only TRAILING-empty levels. NEVER drop an interior (e.g. depth-0)
      // empty batch — collapsing the stack length would shorten the explore
      // depth and wrongly re-enable the one-level Explore cap on already-explored
      // riffs (deleting a parent must not let its riffs be explored). See
      // test_explore_once.py.
      while (next.length > 1 && next[next.length - 1].length === 0) next.pop();
      return next.some((b) => b.length > 0) ? next : [];
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastRemoved]);

  async function handleAddAsset() {
    setAddMenuOpen(false);
    setPickerOpen(true);
  }
  async function handleAddUpload() {
    setAddMenuOpen(false);
    fileInputRef.current?.click();
  }
  async function handleFileChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
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
    setPickerOpen(false);
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

  async function enhanceTopic() {
    const text = topic.trim();
    if (!text || enhancingTopic) return;
    setEnhancingTopic(true);
    try {
      const r = await api.yasProducer.enhance(text, "topic");
      if (r.enhanced) setTopic(r.enhanced);
    } catch (err: any) {
      toast.error("Couldn't enhance", humanError(err));
    } finally {
      setEnhancingTopic(false);
    }
  }

  async function runGenerate() {
    if (!canGenerate) return;
    // Re-entrance lock — guards against double-clicks before React commits
    // the phase state.
    if (generateLockRef.current) return;

    generateLockRef.current = true;
    setError(null);
    setPhase("generating");
    setAwaitingBatch(true);
    try {
      const batch = await api.yasProducer.posterOnly.generate({
        topic: topic.trim(),
        reference_asset_ids: refs
          .filter((r) => r.kind === "asset")
          .map((r: any) => r.assetId),
        reference_upload_ids: refs
          .filter((r) => r.kind === "upload")
          .map((r: any) => r.uploadId),
        image_usage: refs.length > 0 ? imageUsage : undefined,
        image_model: "gemini_pro",
        style,
        aspect,
        progressive: true,
      });
      // Fresh root batch — reset the navigation stack.
      setBatchStack([batch.items]);
      setAttempts((prev) => [...batch.items, ...prev]);
      setAwaitingBatch(false);
      const done = batch.progressive ? await watchBatch(batch.items.map((g) => g.id)) : batch.items;
      if (!aliveRef.current) return;
      const total = done.reduce((s, g) => s + Number(g.cost_usd || 0), 0);
      const ok = done.filter((g) => g.status === "ready").length;
      toast.info(`${ok} variant${ok === 1 ? "" : "s"} generated`, `~$${total.toFixed(2)}`);
    } catch (err: any) {
      const msg = err?.message || String(err);
      setError(msg);
      if (msg.includes("429") || msg.toLowerCase().includes("hourly cap")) {
        toast.error("Rate limited", "Hourly cap reached — try again later");
      } else if (
        msg.includes("503") ||
        msg.toLowerCase().includes("permission") ||
        msg.toLowerCase().includes("suspended")
      ) {
        toast.error("Gemini access denied", "Check the GCP project — generation halted");
      } else {
        toast.error("Generation failed", msg);
      }
    } finally {
      generateLockRef.current = false;
      setAwaitingBatch(false);
      setPhase("idle");
    }
  }

  // "Explore" a variant → push a new 4-up batch generated from it.
  async function handleExplore(gen: YasPosterOnlyGeneration) {
    if (phase !== "idle" || gen.status !== "ready") return;
    if (generateLockRef.current) return;
    // Explore is one level only — never explore an explored variant (depth ≥ 1).
    if (batchStack.length > 1) return;
    generateLockRef.current = true;
    setError(null);
    setPhase("generating");
    setAwaitingBatch(true);
    try {
      const batch = await api.yasProducer.posterOnly.variants(gen.id, "gemini_pro", true);
      setBatchStack((prev) => [...prev, batch.items]);
      setAttempts((prev) => [...batch.items, ...prev]);
      setAwaitingBatch(false);
      const done = batch.progressive ? await watchBatch(batch.items.map((g) => g.id)) : batch.items;
      if (!aliveRef.current) return;
      const total = done.reduce((s, g) => s + Number(g.cost_usd || 0), 0);
      const ok = done.filter((g) => g.status === "ready").length;
      toast.info(`${ok} variant${ok === 1 ? "" : "s"} from #${gen.id}`, `~$${total.toFixed(2)}`);
    } catch (err: any) {
      const msg = err?.message || String(err);
      setError(msg);
      if (msg.includes("429") || msg.toLowerCase().includes("hourly cap")) {
        toast.error("Rate limited", "Hourly cap reached — try again later");
      } else {
        toast.error("Couldn't explore", msg);
      }
    } finally {
      generateLockRef.current = false;
      setAwaitingBatch(false);
      setPhase("idle");
    }
  }

  // Save a single variant card into the Assets library.
  async function handleSaveCard(gen: YasPosterOnlyGeneration) {
    if (savingId != null || gen.status !== "ready") return;
    setSavingId(gen.id);
    try {
      const asset = await api.yasProducer.posterOnly.saveToAssets(gen.id, {
        display_name: topic.trim() || gen.topic || `Yas poster #${gen.id}`,
      });
      patchGeneration(gen.id, { status: "saved_to_assets", saved_asset_id: asset.id });
      assetsStore.invalidate();
      assetsStore.fetch({ limit: 500 });
      toast.info("Saved to Assets", asset.display_name || "");
    } catch (err: any) {
      const msg = err?.message || String(err);
      if (msg.includes("already_saved") || msg.includes("409")) {
        toast.info("Already saved", "View it in Assets");
        patchGeneration(gen.id, { status: "saved_to_assets" });
      } else {
        toast.error("Save failed", msg);
      }
    } finally {
      setSavingId(null);
    }
  }

  // "Remove logo" → re-render this poster without the 42 logo, kept as a NEW
  // card appended next to the original (both stay visible; the user can compare
  // or re-roll). The new record carries its own created_at so its cache-busted
  // image renders cleanly.
  async function handleRemoveLogo(gen: YasPosterOnlyGeneration) {
    if (removingLogoId != null) return;
    if (!(gen.status === "ready" || gen.status === "saved_to_assets")) return;
    setRemovingLogoId(gen.id);
    try {
      const rec = await api.yasProducer.posterOnly.removeLogo(gen.id);
      setBatchStack((stack) => {
        if (!stack.length) return [[rec]];
        const next = stack.slice();
        next[next.length - 1] = [...next[next.length - 1], rec];
        return next;
      });
      setAttempts((prev) => [rec, ...prev]);
      toast.info("Logo removed", `New version #${rec.id} added`);
    } catch (err: any) {
      const msg = err?.message || String(err);
      if (msg.includes("429") || msg.toLowerCase().includes("rate-limit") || msg.toLowerCase().includes("hourly cap")) {
        toast.error("Rate limited", "Image quota reached — wait a minute and try again");
      } else if (
        msg.includes("503") ||
        msg.toLowerCase().includes("permission") ||
        msg.toLowerCase().includes("suspended")
      ) {
        toast.error("Gemini access denied", "Check the GCP project");
      } else {
        toast.error("Couldn't remove logo", msg);
      }
    } finally {
      setRemovingLogoId(null);
    }
  }

  // A card's "Refine" button: point the panel at that card AND take the user
  // there. Setting the target alone reads as a dead click, because the panel it
  // opens is below the fold. reveal() scrolls to it, focuses the box and pulses it.
  function openRefine(id: number) {
    setRefineTargetId(id);
    refinePanel.reveal();
  }

  // "Animate" → import this poster as an animation frame, then hand it to the
  // roadmap, which opens Create Animation on it. Importing costs nothing and
  // calls no model; the Veo step, its cost and its hourly cap all stay where
  // they already were, on the animation tool. This deliberately does NOT
  // animate anything here: the point is to join the flow that already runs on
  // to the caption, the schedule and publishing.
  async function handleAnimate(g: YasPosterOnlyGeneration) {
    if (!onAnimate || animatingId != null) return;
    if (phase !== "idle" || savingId != null || removingLogoId != null || refiningId != null) return;
    setAnimatingId(g.id);
    try {
      const frame = await api.yasProducer.animation.fromImage({ poster_gen_id: g.id });
      onAnimate(frame.id);
    } catch (err: any) {
      toast.error("Couldn't open this in Create Animation", err?.message || String(err));
    } finally {
      setAnimatingId(null);
    }
  }

  // "Refine" → re-render the SELECTED poster applying only the typed change, kept
  // as a NEW card appended next to the original (same non-destructive insert as
  // Remove logo). On success it clears the text and retargets the panel to the new
  // card, so the user can keep iterating ("make it blue" → "now bigger title").
  async function handleRefine() {
    const targetId = refineTargetId;
    const text = refineText.trim();
    if (targetId == null || !text) return;
    if (phase !== "idle" || generateLockRef.current || refiningId != null) return;
    if (savingId != null || removingLogoId != null) return;
    generateLockRef.current = true;
    setRefiningId(targetId);
    try {
      const rec = await api.yasProducer.posterOnly.refine(targetId, text);
      setBatchStack((stack) => {
        if (!stack.length) return [[rec]];
        const next = stack.slice();
        next[next.length - 1] = [...next[next.length - 1], rec];
        return next;
      });
      setAttempts((prev) => [rec, ...prev]);
      toast.info("Changes applied", `New version #${rec.id} added`);
      setRefineText("");
      setRefineTargetId(rec.id);
    } catch (err: any) {
      const msg = err?.message || String(err);
      if (msg.includes("429") || msg.toLowerCase().includes("rate-limit") || msg.toLowerCase().includes("hourly cap")) {
        toast.error("Rate limited", "Image quota reached — wait a minute and try again");
      } else if (
        msg.includes("503") ||
        msg.toLowerCase().includes("permission") ||
        msg.toLowerCase().includes("suspended")
      ) {
        toast.error("Gemini access denied", "Check the GCP project");
      } else {
        toast.error("Couldn't refine", msg);
      }
    } finally {
      generateLockRef.current = false;
      setRefiningId(null);
    }
  }

  // Breadcrumb / Back: truncate the stack back to a given depth.
  function goToDepth(depth: number) {
    setBatchStack((prev) => prev.slice(0, depth + 1));
  }

  // History thumb → show that single poster as a fresh single-card view.
  function viewSingle(gen: YasPosterOnlyGeneration) {
    setBatchStack([[gen]]);
  }

  async function startOver() {
    if (attempts.length > 0) {
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
    setImageUsage("style");
    setAttempts([]);
    setBatchStack([]);
    setError(null);
  }

  return (
    <div className="space-y-5">
      <header className="flex items-center gap-3">
        <button onClick={onBack} className="btn btn-ghost text-sm inline-flex items-center gap-1">
          <ChevronLeftIcon /> Back
        </button>
        <h1 className="h1 flex-1">Create Poster</h1>
        {(attempts.length > 0 || topic) && (
          <button onClick={startOver} className="btn btn-ghost text-xs">
            Start over
          </button>
        )}
      </header>

      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)] gap-5 items-start">
        {/* LEFT: Composer */}
        <section className="card p-4 space-y-4 lg:sticky lg:top-4">
          <label className="block">
            <span className="eyebrow mb-1.5 block">What's the post about?</span>
            <div className="relative">
              <textarea
                value={topic}
                onChange={(e) => setTopic(e.target.value)}
                placeholder="e.g. Piscine applications now open. Focused on ambitious students who haven't coded before."
                rows={3}
                disabled={enhancingTopic}
                className="w-full rounded-lg border border-border bg-bg-subtle px-3 py-2 pr-24 text-sm focus:outline-none focus:border-accent resize-none disabled:opacity-60"
              />
              <EnhanceButton
                onClick={enhanceTopic}
                busy={enhancingTopic}
                disabled={topic.trim().length === 0}
                busyLabel="Polishing…"
                title="Rewrite into a brand-on-tone headline"
              />
            </div>
          </label>

          <div>
            <p className="eyebrow mb-1.5">Style</p>
            <div className="flex items-center gap-1 p-1 rounded-lg bg-bg-muted w-fit flex-wrap">
              {STYLES.map((s) => (
                <button
                  key={s.key}
                  onClick={() => setStyle(s.key)}
                  title={s.hint}
                  className={[
                    "px-3 py-1 rounded-md text-xs font-medium transition-all",
                    style === s.key ? "bg-bg text-fg shadow-sm" : "text-muted hover:text-fg",
                  ].join(" ")}
                >
                  {s.label}
                </button>
              ))}
            </div>
            <p className="text-3xs text-muted mt-1.5 leading-snug">
              {STYLES.find((s) => s.key === style)?.hint}
            </p>
          </div>

          <FormatPicker options={POSTER_FORMATS} value={aspect} onChange={setAspect} />

          <div>
            <p className="eyebrow mb-1.5">References <span className="text-muted normal-case font-normal">(up to {MAX_REFS}, optional)</span></p>
            <div className="flex items-center gap-2 flex-wrap">
              {refs.map((ref, idx) => (
                <div
                  key={idx}
                  className="relative w-16 h-16 rounded-lg overflow-hidden border border-border bg-black"
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
                        className="flex items-center gap-2 w-full text-left px-3 py-2 text-sm hover:bg-bg-muted"
                      >
                        <UploadIcon className="w-4 h-4 text-muted" />
                        Upload from file
                      </button>
                      <button
                        onClick={handleAddAsset}
                        className="flex items-center gap-2 w-full text-left px-3 py-2 text-sm hover:bg-bg-muted"
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

            {refs.length > 0 && (
              <div className="mt-2.5">
                <div className="flex items-center gap-1 p-1 rounded-lg bg-bg-muted w-fit">
                  {(["style", "background"] as const).map((usage) => (
                    <button
                      key={usage}
                      onClick={() => setImageUsage(usage)}
                      className={[
                        "px-3 py-1 rounded-md text-xs font-medium transition-all capitalize",
                        imageUsage === usage
                          ? "bg-bg text-fg shadow-sm"
                          : "text-muted hover:text-fg",
                      ].join(" ")}
                    >
                      {usage}
                    </button>
                  ))}
                </div>
                <p className="text-3xs text-muted mt-1.5 leading-snug">
                  {imageUsage === "style"
                    ? "The model copies the reference's look (textures, lighting) — it isn't placed in the poster."
                    : "The reference becomes the literal background; the scene, text, and logo render on top of it."}
                </p>
              </div>
            )}
          </div>

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
                  Generating 4 variants…
                </span>
              ) : (
                <>{hasResult ? "Generate a new set" : "Generate 4 posters"}</>
              )}
            </button>
            {phase === "idle" && (
              <p className="text-2xs text-muted text-center mt-1.5">
                gpt-4o + Nano Banana Pro · 4 variants · ~$0.55 · each shows as it's ready (~20-30 s apart) · 42 logo baked in
              </p>
            )}
            {phase === "generating" && (
              <JobProgress className="mt-2" startedAt={startedAt} kind="poster" />
            )}
          </div>
          {error && (
            <p className="text-xs text-danger" role="alert">{error}</p>
          )}
        </section>

        {/* RIGHT: Result grid */}
        <div className="space-y-4 min-w-0">
          {!hasResult && phase !== "generating" && (
            <section className="card p-10 text-center border-dashed flex flex-col items-center justify-center min-h-[400px]">
              <div className="text-muted mb-3"><CanvasIcon className="w-12 h-12" /></div>
              <p className="text-sm text-fg font-medium">Your 4 variants will appear here</p>
              <p className="text-xs text-muted mt-1 max-w-sm mx-auto leading-relaxed">
                Add a topic{refs.length === 0 ? " (references optional)" : ""} and hit Generate. Click any
                variant to explore 4 more from it.
              </p>
            </section>
          )}

          {(hasResult || phase === "generating") && (
            <section className="card p-4 space-y-3">
              {/* Which topic produced what's on screen — pinned to the batch's
                  own topic so a stale render can't masquerade as the current
                  composer text. */}
              {resultTopic && (
                <div className="flex items-baseline gap-2 min-w-0">
                  <span className="eyebrow shrink-0">
                    {phase === "generating" ? "Generating" : "Results for"}
                  </span>
                  <span className="text-sm font-medium text-fg truncate" title={resultTopic}>
                    {resultTopic}
                  </span>
                </div>
              )}
              {/* Breadcrumb */}
              <div className="flex items-center gap-2 flex-wrap min-h-[20px]">
                {batchStack.length > 1 && (
                  <button
                    onClick={() => goToDepth(batchStack.length - 2)}
                    disabled={phase === "generating"}
                    className="btn btn-ghost text-xs inline-flex items-center gap-1 disabled:opacity-50"
                  >
                    <ChevronLeftIcon className="w-3.5 h-3.5" /> Back
                  </button>
                )}
                <div className="flex items-center gap-1 text-xs text-muted flex-wrap">
                  {batchStack.map((_, depth) => (
                    <span key={depth} className="inline-flex items-center gap-1">
                      {depth > 0 && <span className="opacity-50">→</span>}
                      <button
                        onClick={() => goToDepth(depth)}
                        disabled={phase === "generating"}
                        className={[
                          "px-1.5 py-0.5 rounded transition-colors disabled:opacity-50",
                          depth === batchStack.length - 1
                            ? "text-fg font-medium bg-bg-muted"
                            : "hover:text-fg hover:bg-bg-muted/60",
                        ].join(" ")}
                      >
                        {depth === 0 ? "Original" : `Variant lvl ${depth}`}
                      </button>
                    </span>
                  ))}
                </div>
              </div>

              {/* 2×2 grid */}
              <div className="grid grid-cols-2 gap-3">
                {awaitingBatch
                  ? Array.from({ length: 4 }).map((_, i) => (
                      <div
                        key={i}
                        className={`${aspectClass(aspect, "1:1")} rounded-lg bg-bg-muted animate-pulse flex items-center justify-center`}
                      >
                        <span className="inline-block w-6 h-6 border-2 border-accent border-t-transparent rounded-full animate-spin" />
                      </div>
                    ))
                  : currentBatch.map((g) => (
                      <VariantCard
                        key={g.id}
                        gen={g}
                        pendingAspect={aspect}
                        saving={savingId === g.id}
                        removingLogo={removingLogoId === g.id}
                        refining={refiningId === g.id}
                        disabled={phase !== "idle" || savingId != null || removingLogoId != null || refiningId != null || animatingId != null}
                        canExplore={batchStack.length <= 1}
                        onExplore={() => handleExplore(g)}
                        onSave={() => handleSaveCard(g)}
                        onRemoveLogo={() => handleRemoveLogo(g)}
                        onRefine={() => openRefine(g.id)}
                        animating={animatingId === g.id}
                        onAnimate={onAnimate ? () => void handleAnimate(g) : undefined}
                        onUse={
                          onContinue && (g.status === "ready" || g.status === "saved_to_assets")
                            ? () =>
                                onContinue({
                                  refId: g.id,
                                  mediaType: "image",
                                  mediaUrl: api.yasProducer.posterOnly.fileUrl(g.id, g.created_at),
                                  topic: g.topic || topic.trim(),
                                })
                            : undefined
                        }
                      />
                    ))}
              </div>
              {phase === "generating" && (
                <p className="text-2xs text-muted text-center">
                  Each poster appears as soon as it's ready
                </p>
              )}

              {/* Refine panel — pick a card's "Refine" button → describe a change
                  here → re-render it as a NEW card. Shown only when a card in the
                  current batch is selected. */}
              {refineTarget && (
                <div
                  ref={refinePanel.panelRef}
                  className="rounded-lg border border-accent/50 bg-accent-fade p-3 flex gap-3"
                >
                  <img
                    src={api.yasProducer.posterOnly.fileUrl(refineTarget.id, refineTarget.created_at)}
                    alt={`Poster #${refineTarget.id}`}
                    className="w-16 h-16 rounded-md object-cover shrink-0 bg-black"
                  />
                  <div className="flex-1 min-w-0">
                    <p className="text-xs font-medium text-fg mb-1.5">
                      Refine poster #{refineTarget.id}
                    </p>
                    <textarea
                      ref={refinePanel.focusRef}
                      value={refineText}
                      onChange={(e) => setRefineText(e.target.value)}
                      onKeyDown={(e) => {
                        if ((e.metaKey || e.ctrlKey) && e.key === "Enter") handleRefine();
                      }}
                      placeholder="Describe what to change — e.g. 'make the title bigger', 'change the background to blue'"
                      rows={2}
                      disabled={refiningId != null}
                      className="w-full resize-none rounded-md border border-border bg-bg px-2.5 py-1.5 text-xs text-fg placeholder:text-muted focus:outline-none focus:ring-1 focus:ring-accent disabled:opacity-50"
                    />
                    <div className="flex items-center gap-2 mt-1.5">
                      <button
                        onClick={handleRefine}
                        disabled={!refineText.trim() || phase !== "idle" || savingId != null || removingLogoId != null || refiningId != null}
                        className="rounded-md bg-accent text-white text-xs font-semibold px-3 py-1.5 hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors inline-flex items-center gap-1.5"
                      >
                        {refiningId != null ? (
                          <><span className="inline-block w-3.5 h-3.5 border-2 border-current border-t-transparent rounded-full animate-spin" />Applying…</>
                        ) : (
                          "Apply changes"
                        )}
                      </button>
                      <button
                        onClick={() => {
                          setRefineTargetId(null);
                          setRefineText("");
                        }}
                        disabled={refiningId != null}
                        className="rounded-md text-muted text-xs font-medium px-2.5 py-1.5 hover:text-fg hover:bg-bg-muted disabled:opacity-50 transition-colors"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </section>
          )}

          {attempts.length > 1 && (
            <section>
              <p className="eyebrow mb-2">History · {attempts.length} generations</p>
              <div className="flex gap-2.5 overflow-x-auto pb-2 -mx-1 px-1">
                {attempts.map((g) => {
                  const isCurrent = currentBatch.some((c) => c.id === g.id);
                  const isSaved = g.status === "saved_to_assets";
                  const drewIt = g.style ? STYLE_BY_KEY.get(g.style) : undefined;
                  return (
                    <button
                      key={g.id}
                      onClick={() => viewSingle(g)}
                      title={`#${g.id} · ${fmtRel(g.created_at)}${drewIt ? ` · ${drewIt.label}` : ""}`}
                      className={[
                        "shrink-0 w-20 h-20 rounded-lg overflow-hidden border-2 relative",
                        "transition-all duration-150 hover:scale-105 focus:outline-none focus:ring-2 focus:ring-accent/40",
                        isCurrent
                          ? "border-accent shadow-md scale-105"
                          : "border-transparent opacity-60 hover:opacity-100",
                      ].join(" ")}
                    >
                      {g.status === "ready" || g.status === "saved_to_assets" ? (
                        <img
                          src={api.yasProducer.posterOnly.fileUrl(g.id, g.created_at)}
                          alt={`Attempt ${g.id}`}
                          className="w-full h-full object-cover"
                          // Visited creators stay MOUNTED under `hidden`, and
                          // display:none does NOT suppress an eager <img> fetch —
                          // but loading="lazy" does. Without this the history
                          // strip keeps competing for the browser's ~6
                          // connections while the user is on Dashboard/Library.
                          loading="lazy"
                          decoding="async"
                        />
                      ) : (
                        <div className="w-full h-full bg-bg-muted text-3xs text-muted flex items-center justify-center">
                          {g.status === "failed" ? "fail" : "…"}
                        </div>
                      )}
                      {isSaved && (
                        <span className="absolute top-1 right-1 w-5 h-5 rounded-full bg-success text-white flex items-center justify-center shadow text-3xs">
                          ✓
                        </span>
                      )}
                      {drewIt && (
                        <span className="absolute inset-x-0 bottom-0 bg-black/60 py-0.5 text-center text-3xs leading-none text-white">
                          {drewIt.short}
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
            </section>
          )}
        </div>
      </div>

      {pickerOpen && (
        <AssetPickerModal
          contentCategory={CONTENT_CATEGORY}
          maxSelection={MAX_REFS - refs.length}
          alreadySelectedAssetIds={refs.filter((r) => r.kind === "asset").map((r: any) => r.assetId)}
          onPick={handleAssetsPicked}
          onSkip={() => setPickerOpen(false)}
          onClose={() => setPickerOpen(false)}
        />
      )}
    </div>
  );
}
