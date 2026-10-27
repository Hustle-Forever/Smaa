/**
 * Yas Producer · Create Animation (intros / outros).
 *
 * Freeform (any-style) flow, local-only (yas-output/):
 *   1. Describe the intro/outro + pick aspect (9:16 / 16:9) → gpt-4o expands →
 *      Gemini renders 4 static hero-frame VARIANTS with the 42 logo baked in,
 *      shown as a 2×2 grid (Midjourney-style, exactly like Create Poster).
 *   2. On any variant: "Explore" 4 close riffs of it, or pick a duration (4/6/8s)
 *      + audio and Animate → the frame goes to Veo image-to-video IN PLACE (the
 *      card becomes the clip). The backend poller finishes it; we poll get().
 *
 * Mirrors PosterCreator conventions: sticky left composer, 2×2 result grid with
 * per-card hover actions + breadcrumb, toast, re-entrance locks, history strip,
 * 24h rehydrate. The 4-up grouping lives in frontend `batchStack` (ids) — no DB
 * schema change.
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../../lib/api";
import { fmtRel } from "../../lib/format";
import type { ProducerBrief } from "../../lib/producerBrief";
import { useProducerAssetsStore } from "../../lib/store";
import { usePersistentState } from "../../lib/usePersistentState";
import { useRevealPanel } from "../../lib/useRevealPanel";
import { useConfirm } from "../ConfirmProvider";
import { AssetPickerModal } from "./AssetPickerModal";
import { useToast } from "../ToastProvider";
import { EnhanceButton } from "./EnhanceButton";
import { SegmentedControl } from "./SegmentedControl";
import type {
  YasAnimateDuration,
  YasAnimationAspect,
  YasAnimationGen,
  YasAnimationStyle,
} from "../../lib/types";
import type { ContinueToPreview } from "./roadmap/types";

const ANIM_VARIANT_COUNT = 4;

type IconProps = { className?: string };

function ChevronLeftIcon({ className = "w-4 h-4" }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <path d="M15 18l-6-6 6-6"/>
    </svg>
  );
}
function FilmIcon({ className = "w-8 h-8" }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <rect x="3" y="4" width="18" height="16" rx="2"/>
      <path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>
    </svg>
  );
}

const DURATIONS: YasAnimateDuration[] = [4, 6, 8];

// What the little corner badge says. "asis" is a frame the user brought in
// rather than one we drew, so it gets words rather than its key.
const STYLE_BADGE: Record<string, string> = {
  voxel: "voxel",
  popup: "pop-up",
  pattern: "pattern",
  asis: "your picture",
};
const ASPECTS: { key: YasAnimationAspect; label: string }[] = [
  { key: "9:16", label: "9:16 vertical" },
  { key: "16:9", label: "16:9 wide" },
];
// Static-frame look. Voxel is the default; the rest are brand-grounded presets.
// Keep in sync with the backend registry (yas_animation_styles.py).
const STYLES: { key: YasAnimationStyle; label: string; hint: string }[] = [
  { key: "voxel", label: "Voxel", hint: "Retro-voxel toy diorama." },
  { key: "popup", label: "Pop-up", hint: "Code-centric OS-window cards (●●● title bar, <tag/> titles)." },
  { key: "pattern", label: "Pattern", hint: "Bold full-bleed brand pattern (Ghutra / Map) on a two-color split." },
];

/** CSS aspect-ratio for a variant card, matching the frame's shape. */
function aspectStyle(aspect: string | undefined) {
  return { aspectRatio: aspect === "16:9" ? "16 / 9" : "9 / 16" } as const;
}

/** One variant card in the 2×2 grid. Shows the frame, animates IN PLACE (frame →
 *  spinner → video), and offers per-card Animate / Explore / Remove-logo. */
function AnimVariantCard({
  g,
  duration,
  animating,
  removingLogo,
  refining,
  saving,
  disabled,
  canExplore,
  onAnimate,
  onExplore,
  onRemoveLogo,
  onRefine,
  onSave,
  onUse,
}: {
  g: YasAnimationGen;
  duration: YasAnimateDuration;
  animating: boolean;
  removingLogo: boolean;
  /** True while THIS frame is the one being refined (shows a spinner label). */
  refining: boolean;
  /** True while THIS frame is being saved to Assets. */
  saving: boolean;
  disabled: boolean;
  /** Explore is one level only — hidden on explored (depth ≥ 1) variants. */
  canExplore: boolean;
  onAnimate: () => void;
  onExplore: () => void;
  onRemoveLogo: () => void;
  /** Select THIS frame for a guided refine (opens the prompt panel below). */
  onRefine: () => void;
  /** Save the static frame (a branded poster) to the Assets library. */
  onSave: () => void;
  onUse?: () => void;
}) {
  const style = aspectStyle(g.aspect_ratio);
  const isSaved = g.saved_asset_id != null;

  // Frame render failed → no image to act on.
  if (g.status === "failed" && !g.frame_url) {
    return (
      <div style={style} className="rounded-lg bg-danger/10 border border-danger/30 flex flex-col items-center justify-center p-3 text-center">
        <span className="font-mono text-3xs px-1.5 py-0.5 rounded bg-danger/15 text-danger uppercase tracking-wider mb-1">failed</span>
        <p className="text-2xs text-danger leading-snug line-clamp-3">{g.error || "Generation failed."}</p>
      </div>
    );
  }
  // Still rendering the frame (shouldn't linger — the batch awaits all renders).
  if (!g.frame_url && g.status !== "ready") {
    return (
      <div style={style} className="rounded-lg bg-bg-muted animate-pulse flex items-center justify-center">
        <span className="inline-block w-6 h-6 border-2 border-accent border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  const isAnimating = g.status === "animating";
  const isReady = g.status === "ready" && !!g.file_url;

  return (
    <div className="group relative rounded-lg overflow-hidden bg-black" style={style}>
      {isReady ? (
        <video
          src={api.yasProducer.animation.fileUrl(g.id, g.created_at)}
          controls
          loop
          playsInline
          className="w-full h-full object-contain bg-black"
        />
      ) : (
        <img
          src={api.yasProducer.animation.frameUrl(g.id, g.created_at)}
          alt={g.prompt}
          className="w-full h-full object-cover"
        />
      )}

      {isAnimating && (
        <div className="absolute inset-0 bg-black/55 flex flex-col items-center justify-center text-white pointer-events-none">
          <span className="inline-block w-8 h-8 border-2 border-white border-t-transparent rounded-full animate-spin" />
          <p className="text-xs font-medium mt-2">Animating with Veo…</p>
          <p className="text-3xs opacity-80 mt-0.5">~1–2 minutes</p>
        </div>
      )}

      <span className="absolute top-1.5 left-1.5 font-mono text-[9px] leading-none px-1 py-0.5 rounded bg-black/65 text-white uppercase tracking-wide z-10">
        {STYLE_BADGE[g.style || "voxel"] || g.style}
      </span>

      {/* Frame actions — bottom bar (hidden once it's a video or while animating). */}
      {!isReady && !isAnimating && (
        <div className="absolute inset-x-0 bottom-0 p-2 flex flex-wrap items-center gap-1.5 bg-gradient-to-t from-black/85 to-transparent opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            onClick={onAnimate}
            disabled={disabled}
            className="flex-1 min-w-[88px] rounded-md bg-white/95 text-black text-xs font-semibold px-2 py-1.5 hover:bg-white disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {animating ? "Animating…" : g.status === "failed" ? `Retry · ${duration}s` : `Animate · ${duration}s`}
          </button>
          {canExplore && (
            <button
              onClick={onExplore}
              disabled={disabled}
              title="Explore — 4 close variations of this frame"
              className="rounded-md bg-black/60 text-white text-xs font-medium px-2 py-1.5 hover:bg-black/80 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Explore →
            </button>
          )}
          <button
            onClick={onSave}
            disabled={disabled || isSaved}
            title={isSaved ? "Already saved to Assets" : "Save this frame to Assets (as a poster)"}
            className="rounded-md bg-black/60 text-white text-xs font-medium px-2.5 py-1.5 hover:bg-black/80 disabled:opacity-50 disabled:cursor-not-allowed transition-colors inline-flex items-center gap-1.5"
          >
            {saving ? (
              <><span className="inline-block w-3.5 h-3.5 border-2 border-current border-t-transparent rounded-full animate-spin" />Saving…</>
            ) : isSaved ? (
              "Saved"
            ) : (
              "Save"
            )}
          </button>
          <button
            onClick={onRefine}
            disabled={disabled}
            title="Refine this frame — describe a change and re-render it (kept as a new variant)"
            className="rounded-md bg-black/60 text-white text-xs font-medium px-2.5 py-1.5 hover:bg-black/80 disabled:opacity-50 disabled:cursor-not-allowed transition-colors inline-flex items-center gap-1.5"
          >
            {refining ? (
              <><span className="inline-block w-3.5 h-3.5 border-2 border-current border-t-transparent rounded-full animate-spin" />Refining…</>
            ) : (
              "Refine"
            )}
          </button>
          <button
            onClick={onRemoveLogo}
            disabled={disabled}
            title="Re-render this frame without the 42 logo (kept as a new variant)"
            className="rounded-md bg-black/60 text-white text-xs font-medium px-2.5 py-1.5 hover:bg-black/80 disabled:opacity-50 disabled:cursor-not-allowed transition-colors inline-flex items-center gap-1.5"
          >
            {removingLogo ? (
              <><span className="inline-block w-3.5 h-3.5 border-2 border-current border-t-transparent rounded-full animate-spin" />Removing…</>
            ) : (
              "Remove logo"
            )}
          </button>
        </div>
      )}

      {/* Ready-video actions — top bar (keeps clear of the video's own controls). */}
      {isReady && (
        <div className="absolute inset-x-0 top-0 p-2 flex items-center gap-1.5 bg-gradient-to-b from-black/75 to-transparent opacity-0 group-hover:opacity-100 transition-opacity">
          {canExplore && (
            <button
              onClick={onExplore}
              disabled={disabled}
              title="Explore — 4 close variations of this frame"
              className="rounded-md bg-white/90 text-black text-xs font-semibold px-2 py-1 hover:bg-white disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Explore →
            </button>
          )}
          <button
            onClick={onSave}
            disabled={disabled || isSaved}
            title={isSaved ? "Already saved to Assets" : "Save the static frame to Assets (as a poster)"}
            className="rounded-md bg-white/90 text-black text-xs font-semibold px-2 py-1 hover:bg-white disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {saving ? "Saving…" : isSaved ? "Saved" : "Save"}
          </button>
          {onUse && (
            <button
              onClick={onUse}
              disabled={disabled}
              title="Use this animation — continue to Preview"
              className="ml-auto rounded-md bg-accent text-white text-xs font-semibold px-2 py-1 hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Use →
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/** Small history-strip thumbnail (the all-time 24h list, below the grid). */
function HistoryThumb({
  g,
  active,
  disabled,
  onClick,
}: {
  g: YasAnimationGen;
  active: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={`#${g.id} · ${g.style || "voxel"} · ${fmtRel(g.created_at)}`}
      className={[
        "shrink-0 w-16 h-16 rounded-lg overflow-hidden border-2 relative transition-all duration-150 hover:scale-105 focus:outline-none focus:ring-2 focus:ring-accent/40 disabled:cursor-not-allowed disabled:hover:scale-100",
        active ? "border-accent shadow-md scale-105" : "border-transparent opacity-60 hover:opacity-100",
      ].join(" ")}
    >
      {g.frame_url ? (
        <img
          src={api.yasProducer.animation.frameUrl(g.id, g.created_at)}
          alt={`Animation ${g.id}`}
          className="w-full h-full object-cover"
          // Hidden-but-mounted creators still fetch eagerly;
          // display:none doesn't stop it, loading="lazy" does.
          loading="lazy"
          decoding="async"
        />
      ) : (
        <div className="w-full h-full bg-bg-muted text-3xs text-muted flex items-center justify-center">
          {g.status === "failed" ? "fail" : "…"}
        </div>
      )}
      {g.status === "ready" && (
        <span className="absolute bottom-0.5 right-0.5 w-4 h-4 rounded-full bg-black/70 text-white flex items-center justify-center shadow text-[8px]">▶</span>
      )}
      {g.status === "animating" && (
        <span className="absolute inset-0 bg-black/40 flex items-center justify-center">
          <span className="inline-block w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full animate-spin" />
        </span>
      )}
    </button>
  );
}

interface Props {
  onBack: () => void;
  /** Roadmap: hand the finished animation to the Preview step. Absent standalone. */
  onContinue?: ContinueToPreview;
  /** Phase C: reopen a specific past animation (by id) from the Library,
   *  pre-loaded into the ready/refine view. Wins over the most-recent rehydrate. */
  initialRunId?: number | null;
  /** Roadmap: report the on-screen 2×2 grid's ids (the top batch) so the URL hash
   *  can encode a shareable full-grid resume. Fired from an effect on batchStack. */
  onBatch?: (ids: number[]) => void;
  /** Roadmap: seed the grid from a shared/refreshed URL — a superset of
   *  initialRunId. When >1 id, the whole batch is rehydrated in order. */
  initialRunIds?: number[] | null;
  /** Trend Finder "Make this" seed. One-shot, applied at mount only — the
   *  roadmap bumps projectNonce for every hand-off, so this component is always
   *  freshly mounted when a brief arrives. Never set together with
   *  initialRunId (a resume wins; the roadmap clears resumeRunId first). */
  initialBrief?: ProducerBrief | null;
}

export function AnimationCreator({
  onBack,
  onContinue,
  initialRunId = null,
  onBatch,
  initialRunIds = null,
  initialBrief = null,
}: Props) {
  // Draft inputs persist to localStorage so a full refresh (F5) keeps them.
  const [prompt, setPrompt] = usePersistentState("yas.animation.prompt", "");
  const [aspect, setAspect] = usePersistentState<YasAnimationAspect>("yas.animation.aspect", "9:16");
  const [style, setStyle] = usePersistentState<YasAnimationStyle>("yas.animation.style", "voxel");
  const [duration, setDuration] = usePersistentState<YasAnimateDuration>("yas.animation.duration", 6);
  const [withAudio, setWithAudio] = usePersistentState("yas.animation.withAudio", true);

  // `items` is the flat record store + all-time (24h) history, rehydrated on
  // mount. The 4-up batch grouping lives separately in `batchStack` (ids only),
  // so polling/patch touch a single source of truth.
  const [items, setItems] = useState<YasAnimationGen[]>([]);
  // Each entry is one batch of variant ids (Generate = root batch; Explore pushes
  // a child batch → breadcrumbs). The LAST entry is the on-screen 2×2 grid.
  const [batchStack, setBatchStack] = usePersistentState<number[][]>(
    "yas.animation.batchStack",
    [],
  );
  const [phase, setPhase] = useState<"idle" | "framing" | "exploring">("idle");
  const [animatingId, setAnimatingId] = useState<number | null>(null);
  const [removingLogoId, setRemovingLogoId] = useState<number | null>(null);
  // Refine: selected frame, change text, and the id currently rendering. Session-only
  // (NOT persisted) — a refine draft shouldn't survive a refresh.
  const [refineTargetId, setRefineTargetId] = useState<number | null>(null);
  const [refineText, setRefineText] = useState("");
  const [refiningId, setRefiningId] = useState<number | null>(null);
  // The panel sits under the 2x2 grid, which on most screens is off the bottom
  // of the window. Opening it therefore also has to take the user to it.
  const refinePanel = useRevealPanel<HTMLDivElement, HTMLTextAreaElement>();
  // "Use a picture I already have": the same grid, seeded from something the
  // user brought in instead of something we rendered. It lands as an ordinary
  // frame_ready card, so Animate / Explore / Refine / Save all apply to it.
  const [importing, setImporting] = useState(false);
  const [importPickerOpen, setImportPickerOpen] = useState(false);
  const importFileRef = useRef<HTMLInputElement | null>(null);
  // Re-entrancy lock, same idiom as frameLockRef/animLockRef above. A ref, not
  // the `importing` flag: `importing` is read from the render closure, so an
  // upload-then-import chain inside ONE callback would still see its old value.
  const importLockRef = useRef(false);
  const [savingId, setSavingId] = useState<number | null>(null);
  const [enhancing, setEnhancing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const frameLockRef = useRef(false);
  const exploreLockRef = useRef(false);
  const animLockRef = useRef(false);
  const toast = useToast();
  const confirm = useConfirm();
  const assetsStore = useProducerAssetsStore();

  // Trend Finder "Make this" — seed the prompt with the AI-written motion brief.
  // Mount-only: the roadmap remounts this creator (projectNonce) for every
  // hand-off. Writing through the persisted prompt also saves it as the draft.
  useEffect(() => {
    if (!initialBrief || initialRunId != null) return;
    setPrompt(initialBrief.topic);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Rehydrate on mount. Skipped when resuming a specific id (the effect below
  // owns the view) so it can't clobber the targeted animation. A Trend Finder
  // brief does NOT skip the whole effect — this is also the only unconditional
  // loader of the 24h history list, and `batch` is resolved as batchStack ∩
  // items, so bailing here left the grid empty while a persisted batchStack
  // still drove the breadcrumb. Only the cold-start branch is skipped.
  useEffect(() => {
    if (initialRunId != null) return;
    let cancelled = false;
    const lastBatch = batchStack.length ? batchStack[batchStack.length - 1] : [];
    const hadPersistedBatch = lastBatch.length > 0;
    api.yasProducer.animation
      .recent()
      .then((r) => {
        if (cancelled) return;
        const list = r.items || [];
        // History strip always reflects the backend's last 24h; the grid then
        // resolves the persisted batch out of it (the poll re-attaches to any
        // still-rendering item, since Veo keeps running server-side).
        setItems(list);
        // A Trend Finder brief owns the prompt — the list above still loads (so
        // the grid can resolve), but nothing below may overwrite the seed.
        if (initialBrief) return;
        // Warm start: the batch (+ prompt) were already restored from
        // localStorage — leave them, AS LONG AS the batch is still retrievable
        // (it could have aged out of the 24h window, or this is a fresh device).
        if (hadPersistedBatch && lastBatch.some((id) => list.some((g) => g.id === id))) return;
        // Cold start (or the persisted batch is gone): prefer a still-rendering
        // item so its poll re-attaches; else the latest ready / frame_ready /
        // most-recent — show it as a single-card batch and adopt ITS prompt.
        const resume = list.find((g) => g.status === "animating")
          || list.find((g) => g.status === "ready")
          || list.find((g) => g.status === "frame_ready")
          || list[0];
        if (!resume) return;
        setBatchStack([[resume.id]]);
        setPrompt((cur) => (cur.trim() ? cur : resume.prompt || ""));
        setAspect(resume.aspect_ratio || "9:16");
        setStyle(resume.style || "voxel");
      })
      .catch(() => {/* non-fatal */});
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Phase C — reopen a specific past animation (by id) from the Library,
  // pre-loaded into its ready/refine view. Takes precedence over the rehydrate.
  // A shared/refreshed URL may carry a WHOLE batch (initialRunIds, >1) — restore
  // the full 2×2 grid by fetching each id in parallel and preserving order.
  useEffect(() => {
    if (initialRunId == null) return;
    // The batch to restore: prefer the multi-id superset when it has >1 member;
    // otherwise fall back to the single resumed id (unchanged behavior).
    const ids =
      initialRunIds && initialRunIds.length > 1 ? initialRunIds : [initialRunId];
    let cancelled = false;
    if (ids.length > 1) {
      // Full-grid resume — fetch every id in parallel, merge the ones that
      // resolve into `items`, and rebuild the batch in the requested order.
      Promise.all(
        ids.map((id) => api.yasProducer.animation.get(id).catch(() => null)),
      )
        .then((results) => {
          if (cancelled) return;
          const fetched = results.filter(
            (g): g is YasAnimationGen => Boolean(g),
          );
          if (!fetched.length) return;
          mergeItems(fetched);
          // Preserve the URL's order, dropping any id that failed to resolve.
          const ordered = ids.filter((id) => fetched.some((g) => g.id === id));
          setBatchStack([ordered]);
          const head = fetched.find((g) => g.id === ids[0]) || fetched[0];
          setPrompt(head.prompt || "");
          setAspect(head.aspect_ratio || "9:16");
          setStyle(head.style || "voxel");
          if (head.duration_sec) setDuration(head.duration_sec as YasAnimateDuration);
          if (head.with_audio != null) setWithAudio(head.with_audio);
        })
        .catch(() => {/* non-fatal */});
      return () => { cancelled = true; };
    }
    api.yasProducer.animation
      .get(initialRunId)
      .then((g) => {
        if (cancelled || !g) return;
        setItems((prev) => [g, ...prev.filter((x) => x.id !== g.id)]);
        setBatchStack([[g.id]]);
        setPrompt(g.prompt || "");
        setAspect(g.aspect_ratio || "9:16");
        setStyle(g.style || "voxel");
        if (g.duration_sec) setDuration(g.duration_sec as YasAnimateDuration);
        if (g.with_audio != null) setWithAudio(g.with_audio);
      })
      .catch(() => {/* non-fatal */});
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialRunId]);

  // Roadmap: report the on-screen 2×2 grid's ids (the top batch) so the wrapper
  // can encode a shareable full-grid resume in the URL hash. Keyed on batchStack
  // (our own state), guarded so an empty stack never emits.
  useEffect(() => {
    if (!batchStack.length) return;
    const topBatchIds = batchStack[batchStack.length - 1];
    if (!topBatchIds.length) return;
    onBatch?.(topBatchIds);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchStack]);

  // The on-screen 2×2 grid = the last batch's ids resolved against `items`
  // (filtering any that aged out of the 24h window on a cold device).
  const currentBatch = useMemo(() => {
    if (!batchStack.length) return [] as YasAnimationGen[];
    const ids = batchStack[batchStack.length - 1];
    return ids
      .map((id) => items.find((g) => g.id === id))
      .filter((g): g is YasAnimationGen => Boolean(g));
  }, [batchStack, items]);

  // A mutating op is in flight (generate / explore / animate-kickoff / logo
  // removal / refine). Every state-mutating control gates on this so overlapping
  // actions can't corrupt the batch or hijack focus.
  const busy = phase !== "idle" || animatingId != null || removingLogoId != null || refiningId != null;
  // The frame selected for refine, resolved against the on-screen batch. Hides the
  // panel automatically if the selection isn't in the current batch (e.g. breadcrumb).
  const refineTarget =
    refineTargetId == null
      ? null
      : currentBatch.find((g) => g.id === refineTargetId) ?? null;
  const canFrame = !busy && prompt.trim().length > 0;
  const generating = phase === "framing" || phase === "exploring";

  function patch(id: number, p: Partial<YasAnimationGen>) {
    setItems((prev) => prev.map((g) => (g.id === id ? { ...g, ...p } : g)));
  }

  // Save the STATIC FRAME (a branded poster) to the Assets library. Saved-ness
  // rides `saved_asset_id` — `status` is untouched so the frame stays animatable.
  async function handleSaveCard(g: YasAnimationGen) {
    if (savingId != null || g.saved_asset_id != null) return;
    if (!(g.status === "frame_ready" || g.status === "ready")) return;
    setSavingId(g.id);
    try {
      const asset = await api.yasProducer.animation.saveToAssets(g.id, {
        display_name: prompt.trim() || g.prompt || `Animation frame #${g.id}`,
      });
      patch(g.id, { saved_asset_id: asset.id });
      assetsStore.invalidate();
      assetsStore.fetch({ limit: 500 });
      toast.info("Saved to Assets", asset.display_name || "");
    } catch (err: any) {
      const msg = err?.message || String(err);
      if (msg.includes("already_saved") || msg.includes("409")) {
        toast.info("Already saved", "View it in Assets");
        api.yasProducer.animation
          .get(g.id)
          .then((fresh) => patch(g.id, fresh))
          .catch(() => {/* best-effort */});
      } else {
        toast.error("Save failed", msg);
      }
    } finally {
      setSavingId(null);
    }
  }

  // Prepend a batch of fresh records to `items`, de-duping by id.
  function mergeItems(fresh: YasAnimationGen[]) {
    setItems((prev) => [...fresh, ...prev.filter((p) => !fresh.some((f) => f.id === p.id))]);
  }

  // Poll EVERY animating variant (not just one) so animating several cards all
  // complete. Re-subscribes when the set of animating ids changes.
  const animatingKey = useMemo(
    () => items.filter((g) => g.status === "animating").map((g) => g.id).sort((a, b) => a - b).join(","),
    [items],
  );
  useEffect(() => {
    const ids = animatingKey ? animatingKey.split(",").map(Number) : [];
    if (!ids.length) return;
    let cancelled = false;
    const tick = async () => {
      await Promise.all(
        ids.map(async (animId) => {
          try {
            const fresh = await api.yasProducer.animation.get(animId);
            if (cancelled) return;
            patch(animId, fresh);
            if (fresh.status === "ready") toast.info("Animation ready", `#${fresh.id} — press play`);
            else if (fresh.status === "failed") toast.error("Animation failed", fresh.error || "Veo error");
          } catch {/* transient — keep polling */}
        }),
      );
    };
    const h = window.setInterval(tick, 5000);
    return () => { cancelled = true; window.clearInterval(h); };
  }, [animatingKey]); // eslint-disable-line react-hooks/exhaustive-deps

  async function enhancePrompt() {
    const text = prompt.trim();
    if (!text || enhancing) return;
    setEnhancing(true);
    try {
      const r = await api.yasProducer.enhance(text, "topic");
      if (r.enhanced) setPrompt(r.enhanced);
    } catch (err: any) {
      toast.error("Couldn't enhance", err?.message || String(err));
    } finally {
      setEnhancing(false);
    }
  }

  async function runGenerateFrame() {
    if (!canFrame || frameLockRef.current) return;
    frameLockRef.current = true;
    setError(null);
    setPhase("framing");
    try {
      const res = await api.yasProducer.animation.generateFrame({
        prompt: prompt.trim(),
        aspect_ratio: aspect,
        style,
      });
      const fresh = res.items || [];
      if (!fresh.length) {
        toast.error("Frame generation failed", "No frames returned");
        return;
      }
      mergeItems(fresh);
      setBatchStack([fresh.map((f) => f.id)]); // fresh root batch — reset the stack
      const ok = fresh.filter((g) => g.status === "frame_ready").length;
      toast.info(`${ok} frame${ok === 1 ? "" : "s"} ready`, "Pick one, then Animate");
    } catch (err: any) {
      const msg = err?.message || String(err);
      setError(msg);
      if (msg.toLowerCase().includes("permission") || msg.includes("503")) {
        toast.error("Gemini access denied", "Check the GCP project");
      } else {
        toast.error("Frame generation failed", msg);
      }
    } finally {
      frameLockRef.current = false;
      setPhase("idle");
    }
  }

  // Import a picture the user already has as a frame and append it to the grid
  // (the same non-destructive insert Refine uses, so nothing on screen is lost).
  // No model runs and nothing is charged: the backend copies the bytes into a
  // frame_ready record, and from there it is an ordinary card.
  async function importPicture(body: { asset_id?: number; upload_token?: string }) {
    if (busy || importLockRef.current) return;
    importLockRef.current = true;
    setImporting(true);
    setError(null);
    try {
      const gen = await api.yasProducer.animation.fromImage({ ...body, aspect_ratio: aspect });
      mergeItems([gen]);
      setBatchStack((prev) => {
        if (!prev.length) return [[gen.id]];
        const copy = prev.map((b) => b.slice());
        copy[copy.length - 1] = [...copy[copy.length - 1], gen.id];
        return copy;
      });
      toast.info("Picture added", "Pick a length, then Animate it");
    } catch (err: any) {
      const msg = err?.message || String(err);
      setError(msg);
      toast.error("Couldn't use that picture", msg);
    } finally {
      importLockRef.current = false;
      setImporting(false);
    }
  }

  // Upload the file, then import the token it comes back with. The lock is
  // released before handing off so importPicture can take it in turn.
  async function importFromFile(file: File | null | undefined) {
    if (!file || busy || importLockRef.current) return;
    importLockRef.current = true;
    setImporting(true);
    let token: string | null = null;
    try {
      token = (await api.yasProducer.posterHuman.uploadRef(file)).token;
    } catch {
      toast.error("Couldn't read that file", "Use a JPEG, PNG or WebP picture");
    } finally {
      importLockRef.current = false;
      setImporting(false);
      if (importFileRef.current) importFileRef.current.value = ""; // re-picking the same file must fire
    }
    if (token) await importPicture({ upload_token: token });
  }

  // "Explore" → 4 CLOSE variations of a frame (anchored on it). Pushes a child
  // batch (breadcrumb) and shows it in the grid. Mirrors poster Explore.
  async function handleExplore(g: YasAnimationGen) {
    if (busy || exploreLockRef.current) return;
    if (!g.frame_url || g.status === "animating") return;
    // Explore is one level only — never explore an explored variant (depth ≥ 1).
    if (batchStack.length > 1) return;
    exploreLockRef.current = true;
    setError(null);
    setPhase("exploring");
    try {
      const res = await api.yasProducer.animation.variants(g.id);
      const fresh = res.items || [];
      if (!fresh.length) {
        toast.error("Explore failed", "No variants returned");
        return;
      }
      mergeItems(fresh);
      setBatchStack((prev) => [...prev, fresh.map((f) => f.id)]);
      const ok = fresh.filter((x) => x.status === "frame_ready").length;
      toast.info(`${ok} variant${ok === 1 ? "" : "s"} from #${g.id}`, "Pick one, then Animate");
    } catch (err: any) {
      const msg = err?.message || String(err);
      setError(msg);
      if (msg.toLowerCase().includes("permission") || msg.includes("503")) {
        toast.error("Gemini access denied", "Check the GCP project");
      } else {
        toast.error("Explore failed", msg);
      }
    } finally {
      exploreLockRef.current = false;
      setPhase("idle");
    }
  }

  async function runAnimate(g: YasAnimationGen) {
    if (busy || animLockRef.current) return;
    if (g.status === "animating") return;
    if (!["frame_ready", "ready", "failed"].includes(g.status)) return;
    animLockRef.current = true;
    setAnimatingId(g.id);
    setError(null);
    try {
      const anim = await api.yasProducer.animation.animate(g.id, {
        duration_sec: duration,
        with_audio: withAudio,
      });
      patch(g.id, anim);
      toast.info("Animating", `Veo · ${duration}s · ~1–2 min`);
    } catch (err: any) {
      const msg = err?.message || String(err);
      setError(msg);
      if (msg.includes("animation_in_flight")) {
        toast.info("Already animating", "Wait for it to finish");
      } else if (msg.includes("429")) {
        toast.error("Veo is busy", "Google's quota was hit, try again in a minute");
      } else if (msg.toLowerCase().includes("permission") || msg.includes("503")) {
        toast.error("Veo access denied", "Check the GCP project");
      } else {
        toast.error("Animate failed", msg);
      }
    } finally {
      animLockRef.current = false;
      setAnimatingId(null);
    }
  }

  // "Remove logo" → re-render a FRAME without the 42 logo (do it before animating
  // — once it's a Veo video the logo is locked in). Kept as a NEW frame_ready
  // record, appended to the current batch as an extra variant.
  async function handleRemoveLogo(g: YasAnimationGen) {
    if (busy) return;
    if (!g.frame_url || g.status === "animating") return;
    setRemovingLogoId(g.id);
    setError(null);
    try {
      const gen = await api.yasProducer.animation.removeLogo(g.id);
      mergeItems([gen]);
      setBatchStack((prev) => {
        if (!prev.length) return [[gen.id]];
        const copy = prev.map((b) => b.slice());
        copy[copy.length - 1] = [...copy[copy.length - 1], gen.id];
        return copy;
      });
      toast.info("Logo removed", `New frame #${gen.id} — animate when ready`);
    } catch (err: any) {
      const msg = err?.message || String(err);
      setError(msg);
      if (msg.includes("429") || msg.toLowerCase().includes("rate-limit")) {
        toast.error("Rate limited", "Image quota reached — wait a minute and try again");
      } else if (msg.toLowerCase().includes("permission") || msg.includes("503")) {
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

  // "Refine" → re-render the SELECTED FRAME applying only the typed change (do it
  // before animating — once it's a Veo video the change is locked in). Kept as a
  // NEW frame_ready record appended to the current batch. Clears the text and
  // retargets to the new frame so the user can keep iterating.
  async function handleRefine() {
    const target = currentBatch.find((g) => g.id === refineTargetId);
    const text = refineText.trim();
    if (!target || !text || busy) return;
    if (!target.frame_url || target.status === "animating") return;
    setRefiningId(target.id);
    setError(null);
    try {
      const gen = await api.yasProducer.animation.refine(target.id, text);
      mergeItems([gen]);
      setBatchStack((prev) => {
        if (!prev.length) return [[gen.id]];
        const copy = prev.map((b) => b.slice());
        copy[copy.length - 1] = [...copy[copy.length - 1], gen.id];
        return copy;
      });
      toast.info("Changes applied", `New frame #${gen.id} — animate when ready`);
      setRefineText("");
      setRefineTargetId(gen.id);
    } catch (err: any) {
      const msg = err?.message || String(err);
      setError(msg);
      if (msg.includes("429") || msg.toLowerCase().includes("rate-limit")) {
        toast.error("Rate limited", "Image quota reached — wait a minute and try again");
      } else if (msg.toLowerCase().includes("permission") || msg.includes("503")) {
        toast.error("Gemini access denied", "Check the GCP project");
      } else {
        toast.error("Couldn't refine", msg);
      }
    } finally {
      setRefiningId(null);
    }
  }

  // History click → show that item as the grid (a single-card batch) + restore
  // its composer fields.
  function viewSingle(g: YasAnimationGen) {
    setBatchStack([[g.id]]);
    setPrompt(g.prompt || "");
    setAspect(g.aspect_ratio || "9:16");
    setStyle(g.style || "voxel");
  }

  // Breadcrumb: jump back to a shallower Explore depth.
  function goToDepth(d: number) {
    setBatchStack((prev) => prev.slice(0, d + 1));
  }

  async function startOver() {
    if (
      items.length > 0 &&
      !(await confirm({
        title: "Clear everything?",
        message: "This clears the prompt and the history on screen.",
        confirmLabel: "Clear",
      }))
    ) {
      return;
    }
    setPrompt("");
    setItems([]);
    setBatchStack([]);
    setError(null);
  }

  const showGrid = currentBatch.length > 0 || generating;

  return (
    <div className="space-y-5">
      <header className="flex items-center gap-3">
        <button onClick={onBack} className="btn btn-ghost text-sm inline-flex items-center gap-1">
          <ChevronLeftIcon /> Back
        </button>
        <h1 className="h1 flex-1">Create Animation</h1>
        {(items.length > 0 || prompt) && (
          <button onClick={startOver} className="btn btn-ghost text-xs">Start over</button>
        )}
      </header>

      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)] gap-5 items-start">
        {/* LEFT: Composer */}
        <section className="card p-4 space-y-4 lg:sticky lg:top-4">
          <label className="block">
            <span className="eyebrow mb-1.5 block">Describe the intro / outro</span>
            <div className="relative">
              <textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder="e.g. A cinematic logo reveal over a dark studio with electric-cyan light streaks and the words WELCOME TO 42."
                rows={4}
                disabled={enhancing}
                className="w-full rounded-lg border border-border bg-bg-subtle px-3 py-2 pr-24 text-sm focus:outline-none focus:border-accent resize-none disabled:opacity-60"
              />
              <EnhanceButton
                onClick={enhancePrompt}
                busy={enhancing}
                disabled={prompt.trim().length === 0}
                busyLabel="Polishing…"
                title="Polish into a punchier brief"
              />
            </div>
          </label>

          <div>
            <p className="eyebrow mb-1.5">Format</p>
            <SegmentedControl
              variant="tab"
              options={ASPECTS.map((a) => ({ value: a.key, label: a.label }))}
              value={aspect}
              onChange={setAspect}
            />
            <p className="text-3xs text-muted mt-1.5">Set the shape before generating — the video matches it.</p>
          </div>

          <div>
            <p className="eyebrow mb-1.5">Style</p>
            <SegmentedControl
              variant="tab"
              className="flex-wrap"
              options={STYLES.map((s) => ({ value: s.key, label: s.label }))}
              value={style}
              onChange={setStyle}
              disabled={busy}
            />
            <p className="text-3xs text-muted mt-1.5 leading-snug">
              {STYLES.find((s) => s.key === style)?.hint}
            </p>
          </div>

          {/* Animate settings — shared; applied to whichever variant you Animate. */}
          <div>
            <p className="eyebrow mb-1.5">Animate settings</p>
            <div className="flex items-center gap-4 flex-wrap">
              <SegmentedControl
                variant="tab"
                options={DURATIONS.map((d) => ({ value: d, label: `${d}s` }))}
                value={duration}
                onChange={setDuration}
              />
              <label className="flex items-center gap-2 text-sm text-fg cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={withAudio}
                  onChange={(e) => setWithAudio(e.target.checked)}
                  className="rounded border-border"
                />
                Music / audio
              </label>
            </div>
          </div>

          <div className="pt-1">
            <button
              onClick={runGenerateFrame}
              disabled={!canFrame}
              className={[
                "w-full rounded-xl px-5 py-4 text-base font-semibold transition-all shadow-sm hover:shadow-md",
                canFrame ? "bg-fg text-bg hover:bg-fg/90" : "bg-bg-muted text-muted cursor-not-allowed",
              ].join(" ")}
            >
              {phase === "framing" ? (
                <span className="inline-flex items-center justify-center gap-2">
                  <span className="inline-block w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin" />
                  Generating frames…
                </span>
              ) : (
                <>{currentBatch.length > 0 ? "Generate new frames" : "Generate frames"}</>
              )}
            </button>
            {phase === "idle" && (
              <p className="text-2xs text-muted text-center mt-1.5">
                gpt-4o + Nano Banana Pro · 4 variants · logo baked in · ~30–60s
              </p>
            )}
          </div>

          {/* Or skip generating entirely and animate a picture that already
              exists: a saved asset, or a file on the user's own computer. */}
          <div className="pt-1">
            <p className="eyebrow mb-1.5">Or animate a picture you already have</p>
            <div className="grid grid-cols-2 gap-2">
              <button
                onClick={() => setImportPickerOpen(true)}
                disabled={busy || importing}
                className="rounded-lg border border-border bg-bg-muted/40 px-3 py-2.5 text-xs font-medium text-fg hover:border-accent hover:bg-accent-fade disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                From Assets
              </button>
              <button
                onClick={() => importFileRef.current?.click()}
                disabled={busy || importing}
                className="rounded-lg border border-border bg-bg-muted/40 px-3 py-2.5 text-xs font-medium text-fg hover:border-accent hover:bg-accent-fade disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {importing ? "Adding…" : "From your computer"}
              </button>
            </div>
            <input
              ref={importFileRef}
              type="file"
              accept="image/jpeg,image/png,image/webp"
              className="hidden"
              onChange={(e) => importFromFile(e.target.files?.[0])}
            />
            <p className="text-2xs text-muted mt-1.5 leading-snug">
              It joins the grid as a card. Nothing is redrawn, so the picture stays
              exactly as it is.
            </p>
          </div>
          {error && <p className="text-xs text-danger" role="alert">{error}</p>}
        </section>

        {/* RIGHT: Result grid */}
        <div className="space-y-4 min-w-0">
          {!showGrid && (
            <section className="card p-10 text-center border-dashed flex flex-col items-center justify-center min-h-[400px]">
              <div className="text-muted mb-3"><FilmIcon className="w-12 h-12" /></div>
              <p className="text-sm text-fg font-medium">Your intro/outro will appear here</p>
              <p className="text-xs text-muted mt-1 max-w-sm mx-auto leading-relaxed">
                Describe it, pick a format, generate 4 frame variants — then animate one into a clip.
              </p>
            </section>
          )}

          {showGrid && (
            <section className="card p-4 space-y-3">
              {/* Breadcrumb (Explore depth) */}
              <div className="flex items-center gap-2 flex-wrap min-h-[20px]">
                {batchStack.length > 1 && (
                  <button
                    onClick={() => goToDepth(batchStack.length - 2)}
                    disabled={busy}
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
                        disabled={busy}
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
                {generating
                  ? Array.from({ length: ANIM_VARIANT_COUNT }).map((_, i) => (
                      <div
                        key={i}
                        style={aspectStyle(aspect)}
                        className="rounded-lg bg-bg-muted animate-pulse flex items-center justify-center"
                      >
                        <span className="inline-block w-6 h-6 border-2 border-accent border-t-transparent rounded-full animate-spin" />
                      </div>
                    ))
                  : currentBatch.map((g) => (
                      <AnimVariantCard
                        key={g.id}
                        g={g}
                        duration={duration}
                        animating={animatingId === g.id}
                        removingLogo={removingLogoId === g.id}
                        refining={refiningId === g.id}
                        saving={savingId === g.id}
                        disabled={busy}
                        canExplore={batchStack.length <= 1}
                        onAnimate={() => runAnimate(g)}
                        onExplore={() => handleExplore(g)}
                        onRemoveLogo={() => handleRemoveLogo(g)}
                        onRefine={() => openRefine(g.id)}
                        onSave={() => handleSaveCard(g)}
                        onUse={
                          onContinue && g.status === "ready"
                            ? () =>
                                onContinue({
                                  refId: g.id,
                                  mediaType: "video",
                                  mediaUrl: api.yasProducer.animation.fileUrl(g.id, g.created_at),
                                  topic: g.prompt || "",
                                })
                            : undefined
                        }
                      />
                    ))}
              </div>
              <p className="text-2xs text-muted text-center">
                {generating
                  ? "Rendering 4 variants · ~30–60 seconds"
                  : "Hover a variant → Animate · Explore · Refine · Remove logo"}
              </p>

              {/* Refine panel — pick a frame's "Refine" button → describe a change
                  here → re-render it as a NEW frame. Shown only when a frame in the
                  current batch is selected. */}
              {refineTarget && (
                <div
                  ref={refinePanel.panelRef}
                  className="rounded-lg border border-accent/50 bg-accent-fade p-3 flex gap-3"
                >
                  <img
                    src={api.yasProducer.animation.frameUrl(refineTarget.id, refineTarget.created_at)}
                    alt={`Frame #${refineTarget.id}`}
                    className="w-16 h-16 rounded-md object-cover shrink-0 bg-black"
                  />
                  <div className="flex-1 min-w-0">
                    <p className="text-xs font-medium text-fg mb-1.5">
                      Refine frame #{refineTarget.id}
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
                        disabled={busy || !refineText.trim()}
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

          {items.length > 1 && (
            <section>
              <p className="eyebrow mb-2">History · {items.length} animations</p>
              <div className="flex gap-2.5 overflow-x-auto pb-2 -mx-1 px-1">
                {items.map((g) => (
                  <HistoryThumb
                    key={g.id}
                    g={g}
                    active={batchStack.length > 0 && batchStack[batchStack.length - 1].includes(g.id)}
                    disabled={busy}
                    onClick={() => viewSingle(g)}
                  />
                ))}
              </div>
            </section>
          )}
        </div>
      </div>

      {importPickerOpen && (
        <AssetPickerModal
          maxSelection={1}
          singleSelectMode="radio"
          alreadySelectedAssetIds={[]}
          showCategoryChips
          imageOnly
          confirmLabel="Use this picture"
          onPick={(assets) => {
            setImportPickerOpen(false);
            if (assets[0]) void importPicture({ asset_id: assets[0].id });
          }}
          onClose={() => setImportPickerOpen(false)}
        />
      )}
    </div>
  );
}
