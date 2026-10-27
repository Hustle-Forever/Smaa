/**
 * Frame extraction dialog — opens when "+ Add to Asset" is clicked on an IG
 * post classified as a VIDEO category (video_interview or img_poster_animated).
 *
 * Flow:
 *   loading    — fires extractFrames(), shows spinner ("Analyzing N frames…")
 *   degraded   — talking_head returned has_visible_speakers=false; user can
 *                retry as animated_poster, cancel, or just see what happened
 *   selecting  — main UI:
 *                  talking_head    → per-person cards, checkbox + archetype + name
 *                  animated_poster → ranked candidates, single-select
 *   importing  — fires fromFrame() N times in parallel, shows progress
 *   done       — toast + close
 *
 * Portaled to document.body (per CLAUDE.md modal pattern — the page-transition
 * wrapper sets `transform`, which creates a containing block that traps `fixed`
 * descendants).
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { useModalChrome } from "../../lib/useModalChrome";
import { createPortal } from "react-dom";

import { api } from "../../lib/api";
import { Skeleton } from "../Skeleton";
import { useToast } from "../ToastProvider";
import type {
  AnimatedPosterExtractResult,
  ExtractFramesResult,
  FrameAssetContentCategory,
  FrameExtractionMode,
  IntroOutroExtractResult,
  IntroOutroFrameCandidate,
  PersonCandidate,
  ProducerCharacterArchetype,
  TalkingHeadExtractResult,
} from "../../lib/types";

interface Props {
  postId: number;
  assetId: number;
  initialMode: FrameExtractionMode;
  onClose: () => void;
  onImported: (count: number) => void;
}

type Phase = "loading" | "degraded" | "selecting" | "importing";

// Per-person selection state for talking_head mode
interface PersonSelection {
  selected: boolean;
  chosenFrameId: number; // best_frame.frame_id by default; alternates can swap
  archetype: ProducerCharacterArchetype | "" | "none"; // "" = AI suggestion, "none" = not a character
  displayName: string;
  description: string;
}

const ARCHETYPE_LABELS: Record<ProducerCharacterArchetype, string> = {
  emirati_male: "Emirati male",
  emirati_female: "Emirati female",
  casual_male: "Casual male",
  casual_female: "Casual female",
};

export function FrameExtractionDialog({
  postId,
  assetId,
  initialMode,
  onClose,
  onImported,
}: Props) {
  const [mode, setMode] = useState<FrameExtractionMode>(initialMode);
  const [phase, setPhase] = useState<Phase>("loading");
  const [error, setError] = useState<string | null>(null);

  // Per-mode extraction-result cache. Switching tabs reads from here when
  // populated (instant) and only re-fetches the first time a tab is opened.
  const [resultsByMode, setResultsByMode] = useState<
    Partial<Record<FrameExtractionMode, ExtractFramesResult>>
  >({});

  // Talking-head per-person UI state, keyed by person_label
  const [personState, setPersonState] = useState<
    Record<string, PersonSelection>
  >({});

  // Animated-poster: which candidate frame the user picked (by frame_id)
  const [chosenPosterFrameId, setChosenPosterFrameId] = useState<number | null>(
    null,
  );

  // Intro / outro: gallery multi-select. Sets of frame_ids per tab.
  const [introSelections, setIntroSelections] = useState<Set<number>>(
    () => new Set(),
  );
  const [outroSelections, setOutroSelections] = useState<Set<number>>(
    () => new Set(),
  );

  // Import progress
  const [importDone, setImportDone] = useState(0);
  const [importTotal, setImportTotal] = useState(0);
  const [importErrors, setImportErrors] = useState<
    Array<{ label: string; reason: string }>
  >([]);

  const toast = useToast();

  // Tabs visible iff the dialog was launched from the talking-head entry
  // point and the user hasn't flipped to the animated-poster fallback.
  const showTabs =
    initialMode === "talking_head" && mode !== "animated_poster";

  const result = resultsByMode[mode] ?? null;

  // Escape, scroll lock, focus trap and focus restore. The Escape guard is
  // preserved deliberately: closing mid-import would abandon an in-flight
  // import, so Escape does nothing while phase === "importing". The hook reads
  // onClose through a ref that is refreshed every render, so this closure
  // always sees the current phase.
  const panelRef = useRef<HTMLDivElement>(null);
  const { trapTab } = useModalChrome({
    onClose: () => {
      if (phase !== "importing") onClose();
    },
    panelRef,
  });

  // Fire extraction when mode changes, or rehydrate from per-mode cache.
  useEffect(() => {
    let cancelled = false;
    setError(null);

    const cached = resultsByMode[mode];
    if (cached) {
      // Already fetched this tab once — show it without re-firing.
      if (
        cached.mode === "talking_head" &&
        cached.degrade_reason === "no_talking_head"
      ) {
        setPhase("degraded");
      } else {
        setPhase("selecting");
      }
      return;
    }

    setPhase("loading");
    api.producerAssets
      .extractFrames(postId, assetId, mode)
      .then((r) => {
        if (cancelled) return;
        setResultsByMode((prev) => ({ ...prev, [mode]: r }));
        if (
          r.mode === "talking_head" &&
          r.degrade_reason === "no_talking_head"
        ) {
          setPhase("degraded");
          return;
        }
        if (r.candidates.length === 0) {
          if (r.mode === "talking_head") {
            setError(
              `No reference frames could be extracted${
                r.degrade_reason ? ` (${r.degrade_reason})` : ""
              }.`,
            );
          } else if (r.mode === "animated_poster") {
            setError(
              "No usable poster frames could be extracted from this video.",
            );
          }
          // intro/outro: empty candidates → fall through; IntroOutroBody
          // renders its own soft empty state.
          setPhase("selecting");
          return;
        }
        if (r.mode === "talking_head") {
          // Seed selection state: nothing selected by default — user picks
          const seed: Record<string, PersonSelection> = {};
          for (const p of r.candidates) {
            seed[p.person_label] = {
              selected: false,
              chosenFrameId: p.best_frame.frame_id,
              archetype: "", // empty string = use AI's suggested_archetype
              displayName: "",
              description: p.description,
            };
          }
          setPersonState(seed);
        } else if (r.mode === "animated_poster") {
          // Animated poster: preselect rank 1 only on high confidence
          if (r.confidence === "high") {
            setChosenPosterFrameId(r.candidates[0].frame.frame_id);
          }
        }
        // intro/outro: no auto-seed; user explicitly checks the frames they want
        setPhase("selecting");
      })
      .catch((e: any) => {
        if (cancelled) return;
        setError(String(e?.message || e));
        setPhase("selecting");
      });
    return () => {
      cancelled = true;
    };
    // resultsByMode intentionally not in deps — re-running the effect on
    // its own writes would cause an infinite loop. We only want to re-fire
    // when the underlying (postId, assetId, mode) changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [postId, assetId, mode]);

  const talkingHead =
    result?.mode === "talking_head"
      ? (result as TalkingHeadExtractResult)
      : null;
  const animatedPoster =
    result?.mode === "animated_poster"
      ? (result as AnimatedPosterExtractResult)
      : null;
  const introOutro =
    result?.mode === "intro" || result?.mode === "outro"
      ? (result as IntroOutroExtractResult)
      : null;

  const currentIntroOutroSelections =
    mode === "intro"
      ? introSelections
      : mode === "outro"
        ? outroSelections
        : null;
  const setCurrentIntroOutroSelections =
    mode === "intro"
      ? setIntroSelections
      : mode === "outro"
        ? setOutroSelections
        : null;

  // Cross-tab selection counts — these come from independent per-tab state
  // slots, so they're always meaningful regardless of which tab is active.
  const characterCount = useMemo(
    () => Object.values(personState).filter((s) => s.selected).length,
    [personState],
  );
  const introCount = introSelections.size;
  const outroCount = outroSelections.size;
  const posterCount = chosenPosterFrameId != null ? 1 : 0;

  const selectedCount = useMemo(() => {
    if (showTabs) {
      return characterCount + introCount + outroCount;
    }
    // Single-mode entry (animated_poster from the post-detail entry point)
    if (animatedPoster) return posterCount;
    return characterCount;
  }, [
    showTabs,
    animatedPoster,
    characterCount,
    introCount,
    outroCount,
    posterCount,
  ]);

  async function runImport() {
    setPhase("importing");
    setImportDone(0);
    setImportErrors([]);

    const jobs: Array<() => Promise<{ label: string; ok: boolean; reason?: string }>> = [];

    // Characters — read from the talking_head cache (not the active result).
    const characterResult = resultsByMode["talking_head"];
    if (
      characterResult &&
      characterResult.mode === "talking_head" &&
      characterCount > 0
    ) {
      for (const p of characterResult.candidates) {
        const sel = personState[p.person_label];
        if (!sel?.selected) continue;
        const archetype =
          sel.archetype === ""
            ? p.suggested_archetype || undefined
            : sel.archetype === "none"
              ? undefined
              : (sel.archetype as ProducerCharacterArchetype);
        const displayName =
          sel.displayName.trim() || p.person_label.replace(/_/g, " ");
        jobs.push(async () => {
          try {
            await api.producerAssets.fromFrame({
              post_id: postId,
              asset_id: assetId,
              frame_id: sel.chosenFrameId,
              content_category: "video_interview",
              archetype: archetype ?? null,
              display_name: displayName,
              person_label: p.person_label,
              person_description: p.description,
              analyze_attributes: !!archetype,
            });
            return { label: displayName, ok: true };
          } catch (err: any) {
            const msg = String(err?.message || err);
            return { label: displayName, ok: false, reason: msg };
          }
        });
      }
    }

    // Intro / Outro — each tab has its own cached result + Set<frame_id>.
    const introOutroJobs = (
      pos: "intro" | "outro",
      cache: ExtractFramesResult | undefined,
      sel: Set<number>,
    ) => {
      if (!cache || (cache.mode !== "intro" && cache.mode !== "outro")) return;
      if (sel.size === 0) return;
      const io = cache as IntroOutroExtractResult;
      const byFrameId = new Map<number, IntroOutroFrameCandidate>();
      for (const c of io.candidates) byFrameId.set(c.frame.frame_id, c);
      const positionLabel = pos === "intro" ? "Intro" : "Outro";
      const contentCategory: FrameAssetContentCategory =
        pos === "intro" ? "img_intro" : "img_outro";
      for (const frameId of Array.from(sel)) {
        const cand = byFrameId.get(frameId);
        const ts = cand?.frame.timestamp_sec ?? 0;
        const noteFragment =
          cand?.note && cand.note.length > 0 ? ` · ${cand.note.slice(0, 40)}` : "";
        const displayName = `${positionLabel} · ${ts.toFixed(1)}s · post #${postId}${noteFragment}`;
        jobs.push(async () => {
          try {
            await api.producerAssets.fromFrame({
              post_id: postId,
              asset_id: assetId,
              frame_id: frameId,
              content_category: contentCategory,
              display_name: displayName,
              analyze_attributes: false,
            });
            return { label: displayName, ok: true };
          } catch (err: any) {
            const msg = String(err?.message || err);
            return { label: displayName, ok: false, reason: msg };
          }
        });
      }
    };
    introOutroJobs("intro", resultsByMode["intro"], introSelections);
    introOutroJobs("outro", resultsByMode["outro"], outroSelections);

    // Animated poster — single-mode (no tabs) path only.
    if (!showTabs && animatedPoster && chosenPosterFrameId != null) {
      jobs.push(async () => {
        try {
          await api.producerAssets.fromFrame({
            post_id: postId,
            asset_id: assetId,
            frame_id: chosenPosterFrameId,
            content_category: "img_poster_animated",
            display_name: `Animated poster · post #${postId}`,
            analyze_attributes: false,
          });
          return { label: "animated poster frame", ok: true };
        } catch (err: any) {
          const msg = String(err?.message || err);
          return { label: "animated poster frame", ok: false, reason: msg };
        }
      });
    }

    setImportTotal(jobs.length);
    if (jobs.length === 0) {
      setPhase("selecting");
      return;
    }

    let completed = 0;
    let succeeded = 0;
    const errors: Array<{ label: string; reason: string }> = [];

    // Fire in parallel — the API handles concurrency fine and N is small (1-8)
    await Promise.all(
      jobs.map(async (job) => {
        const r = await job();
        completed += 1;
        setImportDone(completed);
        if (r.ok) succeeded += 1;
        else errors.push({ label: r.label, reason: r.reason || "unknown" });
      }),
    );

    // Build a "3 characters, 2 intro, 1 outro" breakdown so the user sees
    // exactly what got committed across the tab.
    const breakdownParts: string[] = [];
    if (characterCount > 0)
      breakdownParts.push(
        `${characterCount} character${characterCount === 1 ? "" : "s"}`,
      );
    if (introCount > 0) breakdownParts.push(`${introCount} intro`);
    if (outroCount > 0) breakdownParts.push(`${outroCount} outro`);
    const breakdownSuffix = breakdownParts.length
      ? ` (${breakdownParts.join(", ")})`
      : "";

    if (errors.length === 0) {
      toast.info(
        `Saved ${succeeded} to assets${breakdownSuffix}`,
        "Frames imported",
      );
      onImported(succeeded);
      onClose();
    } else {
      setImportErrors(errors);
      // Partial success — keep modal open so user sees what failed
      if (succeeded > 0) {
        toast.info(
          `${succeeded} saved, ${errors.length} failed${breakdownSuffix}`,
          "Partial import",
        );
        onImported(succeeded);
      } else {
        toast.error("Import failed", errors[0].reason);
      }
      setPhase("selecting");
    }
  }

  function frameUrl(frameId: number): string {
    return api.instagramIngest.frameFileUrl(postId, frameId);
  }

  const title =
    mode === "animated_poster"
      ? "Add a poster frame from this video"
      : showTabs
        ? "Add references from this video"
        : "Add character references from this video";

  const modeBadge =
    mode === "talking_head"
      ? "TALKING HEAD"
      : mode === "animated_poster"
        ? "ANIMATED POSTER"
        : mode === "intro"
          ? "INTRO"
          : "OUTRO";

  // Tabbed flow commits across all three tabs in one click — make the label
  // reflect the aggregate, not just the active tab.
  const importLabel = showTabs
    ? `Save${selectedCount > 0 ? ` ${selectedCount}` : ""} to assets`
    : `Import${selectedCount > 0 ? ` ${selectedCount}` : ""}`;

  function toggleIntroOutroFrame(frameId: number) {
    if (!setCurrentIntroOutroSelections) return;
    setCurrentIntroOutroSelections((prev) => {
      const next = new Set(prev);
      if (next.has(frameId)) next.delete(frameId);
      else next.add(frameId);
      return next;
    });
  }

  // -- Render --
  return createPortal(
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 110,
        background: "rgba(0,0,0,0.75)",
        backdropFilter: "blur(2px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "16px",
      }}
      onClick={() => phase !== "importing" && onClose()}
      role="dialog"
      aria-modal="true"
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        onKeyDown={trapTab}
        onClick={(e) => e.stopPropagation()}
        className="relative bg-bg rounded-2xl shadow-2xl border border-border overflow-hidden flex flex-col"
        style={{ width: "min(760px, 100%)", maxHeight: "min(880px, 92vh)" }}
      >
        {/* Header */}
        <header className="shrink-0 px-4 py-3 border-b border-border flex items-center gap-2.5 bg-bg">
          <span className="font-mono text-3xs px-1.5 py-0.5 rounded bg-fg text-bg uppercase tracking-wider2">
            {modeBadge}
          </span>
          <h2 className="text-sm font-semibold text-fg flex-1 truncate">{title}</h2>
          <button
            onClick={onClose}
            disabled={phase === "importing"}
            aria-label="Close"
            className="w-7 h-7 rounded-full bg-bg-muted hover:bg-border disabled:opacity-50 text-fg flex items-center justify-center text-base leading-none transition-colors"
          >
            ×
          </button>
        </header>

        {/* Tab strip — only when launched in talking_head and not flipped to animated_poster */}
        {showTabs && (
          <TabStrip
            mode={mode}
            disabled={phase === "importing"}
            setMode={setMode}
            characterSelections={
              Object.values(personState).filter((s) => s.selected).length
            }
            introSelections={introSelections.size}
            outroSelections={outroSelections.size}
          />
        )}

        {/* Body */}
        <div className="flex-1 min-h-0 overflow-y-auto">
          {phase === "loading" && (
            <LoadingBody mode={mode} />
          )}

          {phase === "degraded" && talkingHead && (
            <DegradedBody
              dominantContent={talkingHead.dominant_content}
              suggestAnimated={talkingHead.suggest_animated_poster}
              onTryAnimated={() => setMode("animated_poster")}
              onCancel={onClose}
            />
          )}

          {phase === "selecting" && !result && error && (
            <div className="p-4">
              <p className="text-sm text-danger" role="alert">
                {error}
              </p>
            </div>
          )}

          {phase === "selecting" && talkingHead && talkingHead.candidates.length > 0 && (
            <TalkingHeadBody
              candidates={talkingHead.candidates}
              state={personState}
              setState={setPersonState}
              frameUrl={frameUrl}
            />
          )}

          {phase === "selecting" && animatedPoster && animatedPoster.candidates.length > 0 && (
            <AnimatedPosterBody
              result={animatedPoster}
              chosenFrameId={chosenPosterFrameId}
              setChosenFrameId={setChosenPosterFrameId}
              frameUrl={frameUrl}
            />
          )}

          {phase === "selecting" && introOutro && currentIntroOutroSelections && (
            <IntroOutroBody
              result={introOutro}
              selections={currentIntroOutroSelections}
              onToggle={toggleIntroOutroFrame}
              frameUrl={frameUrl}
            />
          )}

          {phase === "selecting" && error && result && (
            <p className="px-4 pb-3 text-xs text-danger" role="alert">
              {error}
            </p>
          )}

          {phase === "importing" && (
            <ImportingBody
              done={importDone}
              total={importTotal}
              errors={importErrors}
            />
          )}
        </div>

        {/* Footer — visible whenever the current tab has candidates OR
            another tab already has selections waiting to commit. */}
        {phase === "selecting" &&
          (((result?.candidates.length ?? 0) > 0) || selectedCount > 0) && (
          <footer className="shrink-0 px-4 py-3 border-t border-border bg-bg-muted/50 flex items-center gap-3">
            <p className="text-xs text-muted flex-1">
              {selectedCount === 0 ? (
                "Select what to import."
              ) : showTabs ? (
                <>
                  <span className="text-fg font-medium">{selectedCount} selected</span>
                  {(() => {
                    const parts: string[] = [];
                    if (characterCount > 0)
                      parts.push(
                        `${characterCount} char${characterCount === 1 ? "" : "s"}`,
                      );
                    if (introCount > 0) parts.push(`${introCount} intro`);
                    if (outroCount > 0) parts.push(`${outroCount} outro`);
                    return parts.length > 1 ? ` · ${parts.join(" · ")}` : "";
                  })()}
                </>
              ) : (
                `${selectedCount} selected.`
              )}
            </p>
            <button
              onClick={onClose}
              className="btn btn-ghost text-xs"
            >
              Cancel
            </button>
            <button
              onClick={runImport}
              disabled={selectedCount === 0}
              className="btn btn-primary text-xs"
            >
              {importLabel}
            </button>
          </footer>
        )}
      </div>
    </div>,
    document.body,
  );
}

// ---------------------------------------------------------------------------
// Body fragments
// ---------------------------------------------------------------------------

function LoadingBody({ mode }: { mode: FrameExtractionMode }) {
  const msg =
    mode === "talking_head"
      ? "Analyzing frames for visible speakers…"
      : mode === "animated_poster"
        ? "Looking for the cleanest poster frame…"
        : mode === "intro"
          ? "Looking for the title card / opening text…"
          : "Looking for the end card / CTA frame…";
  return (
    <div className="p-6 space-y-3">
      <p className="text-sm text-fg">{msg}</p>
      <p className="text-2xs text-muted leading-relaxed">
        This takes ~15-45 seconds. We're running scene detection, extracting
        candidate frames, and asking Gemini to pick the best ones.
      </p>
      <div className="space-y-2 mt-4">
        <Skeleton h="h-32" rounded="rounded-lg" />
        <Skeleton h="h-32" rounded="rounded-lg" />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab strip — Characters | Intro | Outro (talking-head workflow only)
// ---------------------------------------------------------------------------

function TabStrip({
  mode,
  disabled,
  setMode,
  characterSelections,
  introSelections,
  outroSelections,
}: {
  mode: FrameExtractionMode;
  disabled: boolean;
  setMode: (m: FrameExtractionMode) => void;
  characterSelections: number;
  introSelections: number;
  outroSelections: number;
}) {
  const tabs: Array<{
    key: "talking_head" | "intro" | "outro";
    label: string;
    selections: number;
  }> = [
    { key: "talking_head", label: "Characters", selections: characterSelections },
    { key: "intro", label: "Intro", selections: introSelections },
    { key: "outro", label: "Outro", selections: outroSelections },
  ];
  return (
    <nav
      className="shrink-0 px-4 border-b border-border bg-bg flex gap-1"
      role="tablist"
      aria-label="Reference type"
    >
      {tabs.map((t) => {
        const active = mode === t.key;
        return (
          <button
            key={t.key}
            role="tab"
            aria-selected={active}
            disabled={disabled}
            onClick={() => setMode(t.key)}
            className={[
              "relative px-3 py-2.5 text-xs font-medium transition-colors",
              "border-b-2 -mb-px",
              active
                ? "text-fg border-accent"
                : "text-muted hover:text-fg border-transparent",
              disabled ? "opacity-50 cursor-not-allowed" : "",
            ].join(" ")}
          >
            {t.label}
            {t.selections > 0 && (
              <span className="ml-1.5 font-mono text-3xs px-1.5 py-0.5 rounded-full bg-accent/15 text-accent tabular-nums">
                {t.selections}
              </span>
            )}
          </button>
        );
      })}
    </nav>
  );
}

function DegradedBody({
  dominantContent,
  suggestAnimated,
  onTryAnimated,
  onCancel,
}: {
  dominantContent: string | null;
  suggestAnimated: boolean;
  onTryAnimated: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="p-6 space-y-4">
      <p className="text-sm text-fg">
        No talking head detected in this video.
      </p>
      <p className="text-xs text-muted leading-relaxed">
        The AI didn't see anyone speaking to camera
        {dominantContent ? ` — it looks more like ${dominantContent.replace(/_/g, " ")}.` : "."}{" "}
        {suggestAnimated && (
          <span className="text-fg">
            Want to extract it as an animated poster frame instead?
          </span>
        )}
      </p>
      <div className="flex items-center gap-2 pt-2">
        {suggestAnimated && (
          <button onClick={onTryAnimated} className="btn btn-primary text-xs">
            Try animated-poster mode
          </button>
        )}
        <button onClick={onCancel} className="btn btn-ghost text-xs">
          Cancel
        </button>
      </div>
    </div>
  );
}

function TalkingHeadBody({
  candidates,
  state,
  setState,
  frameUrl,
}: {
  candidates: PersonCandidate[];
  state: Record<string, any>;
  setState: (
    upd: (s: Record<string, any>) => Record<string, any>,
  ) => void;
  frameUrl: (frameId: number) => string;
}) {
  return (
    <div className="p-4 space-y-3">
      <p className="text-xs text-muted leading-relaxed">
        Detected {candidates.length} {candidates.length === 1 ? "person" : "people"}.
        Pick which to import. The AI's best frame per person is shown; expand
        alternates to swap if needed.
      </p>
      {candidates.map((p) => {
        const s = state[p.person_label];
        if (!s) return null;
        const allFrames = [p.best_frame, ...p.alternates];
        return (
          <div
            key={p.person_label}
            className={[
              "rounded-xl border p-3 transition-colors",
              s.selected
                ? "border-accent bg-accent/5"
                : "border-border bg-bg-muted/30",
            ].join(" ")}
          >
            <div className="flex gap-3">
              <label className="shrink-0 cursor-pointer relative">
                <input
                  type="checkbox"
                  checked={s.selected}
                  onChange={(e) =>
                    setState((prev) => ({
                      ...prev,
                      [p.person_label]: { ...prev[p.person_label], selected: e.target.checked },
                    }))
                  }
                  className="sr-only peer"
                />
                <div className="w-28 h-28 rounded-lg overflow-hidden bg-black ring-2 ring-transparent peer-checked:ring-accent transition-all">
                  <img
                    src={frameUrl(s.chosenFrameId)}
                    alt={p.description}
                    className="w-full h-full object-cover"
                    loading="lazy"
                  />
                </div>
                <span className="absolute top-1.5 left-1.5 w-5 h-5 rounded-full border-2 border-white bg-white/30 backdrop-blur-sm peer-checked:bg-accent peer-checked:border-accent flex items-center justify-center transition-colors">
                  {s.selected && <span className="text-white text-xs">✓</span>}
                </span>
              </label>

              <div className="flex-1 min-w-0 space-y-2">
                <p className="text-xs text-fg leading-relaxed">{p.description}</p>
                <p className="font-mono text-3xs text-muted">
                  best frame at {p.best_frame.timestamp_sec.toFixed(1)}s · score {p.score.toFixed(2)}
                </p>

                {s.selected && (
                  <div className="space-y-1.5 pt-1">
                    <div className="flex items-center gap-1.5">
                      <label className="text-3xs font-mono text-muted uppercase tracking-wider2 w-16">
                        Archetype
                      </label>
                      <select
                        value={s.archetype}
                        onChange={(e) =>
                          setState((prev) => ({
                            ...prev,
                            [p.person_label]: {
                              ...prev[p.person_label],
                              archetype: e.target.value,
                            },
                          }))
                        }
                        className="rounded border border-border bg-bg-subtle text-xs px-2 py-1 flex-1"
                      >
                        <option value="">
                          AI suggestion ({p.suggested_archetype || "none"})
                        </option>
                        {Object.entries(ARCHETYPE_LABELS).map(([k, label]) => (
                          <option key={k} value={k}>
                            {label}
                          </option>
                        ))}
                        <option value="none">No archetype (just a reference)</option>
                      </select>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <label className="text-3xs font-mono text-muted uppercase tracking-wider2 w-16">
                        Name
                      </label>
                      <input
                        type="text"
                        value={s.displayName}
                        onChange={(e) =>
                          setState((prev) => ({
                            ...prev,
                            [p.person_label]: {
                              ...prev[p.person_label],
                              displayName: e.target.value,
                            },
                          }))
                        }
                        placeholder={p.person_label.replace(/_/g, " ")}
                        className="rounded border border-border bg-bg-subtle text-xs px-2 py-1 flex-1"
                      />
                    </div>

                    {allFrames.length > 1 && (
                      <details className="text-xs">
                        <summary className="cursor-pointer text-muted hover:text-fg font-mono uppercase tracking-wider2 text-3xs">
                          {allFrames.length} candidate frames · swap
                        </summary>
                        <div className="mt-1.5 flex gap-1.5 overflow-x-auto pb-1">
                          {allFrames.map((f) => (
                            <button
                              key={f.frame_id}
                              onClick={() =>
                                setState((prev) => ({
                                  ...prev,
                                  [p.person_label]: {
                                    ...prev[p.person_label],
                                    chosenFrameId: f.frame_id,
                                  },
                                }))
                              }
                              className={[
                                "w-14 h-14 shrink-0 rounded overflow-hidden border-2 transition",
                                f.frame_id === s.chosenFrameId
                                  ? "border-accent"
                                  : "border-transparent opacity-60 hover:opacity-100",
                              ].join(" ")}
                            >
                              <img
                                src={frameUrl(f.frame_id)}
                                alt=""
                                className="w-full h-full object-cover"
                                loading="lazy"
                              />
                            </button>
                          ))}
                        </div>
                      </details>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function AnimatedPosterBody({
  result,
  chosenFrameId,
  setChosenFrameId,
  frameUrl,
}: {
  result: AnimatedPosterExtractResult;
  chosenFrameId: number | null;
  setChosenFrameId: (id: number) => void;
  frameUrl: (frameId: number) => string;
}) {
  const topCandidate = result.candidates[0];
  const otherCandidates = result.candidates.slice(1);

  const confidenceCopy: Record<string, { color: string; label: string; hint: string }> = {
    high: { color: "text-success", label: "High confidence", hint: "Top candidate preselected — looks like a clear winner." },
    medium: { color: "text-warning", label: "Medium confidence", hint: "A few frames look equally complete — pick whichever reads cleanest." },
    low: { color: "text-danger", label: "Low confidence", hint: "AI couldn't find an obviously complete frame. Pick the least bad one." },
  };
  const conf = confidenceCopy[result.confidence] || confidenceCopy.low;

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center gap-2 text-xs">
        <span className={`font-mono text-3xs uppercase tracking-wider2 ${conf.color}`}>
          {conf.label}
        </span>
        <span className="text-muted">·</span>
        <span className="text-muted leading-relaxed">{conf.hint}</span>
      </div>

      {/* Top candidate big */}
      {topCandidate && (
        <button
          onClick={() => setChosenFrameId(topCandidate.frame.frame_id)}
          className={[
            "block w-full rounded-xl overflow-hidden border-2 transition-colors",
            chosenFrameId === topCandidate.frame.frame_id
              ? "border-accent"
              : "border-border hover:border-fg/30",
          ].join(" ")}
        >
          <div className="aspect-[9/16] bg-black flex items-center justify-center" style={{ maxHeight: "55vh" }}>
            <img
              src={frameUrl(topCandidate.frame.frame_id)}
              alt={`Rank ${topCandidate.rank} candidate`}
              className="max-w-full max-h-full"
              loading="lazy"
            />
          </div>
          <div className="px-3 py-2 flex items-center justify-between bg-bg-muted/50">
            <span className="font-mono text-3xs text-muted uppercase tracking-wider2">
              Rank {topCandidate.rank} · {topCandidate.frame.timestamp_sec.toFixed(1)}s
            </span>
            {chosenFrameId === topCandidate.frame.frame_id && (
              <span className="font-mono text-3xs text-accent uppercase tracking-wider2">
                ✓ selected
              </span>
            )}
          </div>
        </button>
      )}

      {/* Alternates */}
      {otherCandidates.length > 0 && (
        <div>
          <p className="font-mono text-3xs text-muted uppercase tracking-wider2 mb-2">
            Alternates
          </p>
          <div className="grid grid-cols-4 gap-2">
            {otherCandidates.map((c) => (
              <button
                key={c.frame.frame_id}
                onClick={() => setChosenFrameId(c.frame.frame_id)}
                className={[
                  "rounded-lg overflow-hidden border-2 aspect-[9/16] transition relative",
                  chosenFrameId === c.frame.frame_id
                    ? "border-accent"
                    : "border-transparent opacity-70 hover:opacity-100",
                ].join(" ")}
              >
                <img
                  src={frameUrl(c.frame.frame_id)}
                  alt={`Rank ${c.rank}`}
                  className="w-full h-full object-cover"
                  loading="lazy"
                />
                <span className="absolute top-1 left-1 font-mono text-[9px] px-1.5 py-0.5 rounded bg-black/60 text-white tracking-wider2">
                  #{c.rank}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Rejected */}
      {result.rejected.length > 0 && (
        <details>
          <summary className="cursor-pointer text-muted hover:text-fg font-mono uppercase tracking-wider2 text-3xs">
            {result.rejected.length} frames rejected · see reasons
          </summary>
          <ul className="mt-2 space-y-1 text-xs text-muted">
            {result.rejected.map((r, i) => (
              <li key={i}>
                <span className="font-mono tabular-nums">
                  {r.frame.timestamp_sec.toFixed(1)}s
                </span>
                {" — "}
                <span className="italic">{r.reason || "no reason given"}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Intro / Outro body — multi-select gallery of text-card / end-card candidates
// ---------------------------------------------------------------------------

function IntroOutroBody({
  result,
  selections,
  onToggle,
  frameUrl,
}: {
  result: IntroOutroExtractResult;
  selections: Set<number>;
  onToggle: (frameId: number) => void;
  frameUrl: (frameId: number) => string;
}) {
  const position = result.position;
  const positionLabel = position === "intro" ? "intro" : "outro";

  // Soft empty state — backend says no text was found in the window
  if (result.degrade_reason === "no_text_detected") {
    return (
      <div className="p-6 space-y-3">
        <p className="text-sm text-fg">
          No {positionLabel} text composition was detected in this video.
        </p>
        <p className="text-xs text-muted leading-relaxed">
          The {positionLabel === "intro" ? "opening" : "closing"} seconds don't
          show a title card or graphic with readable text — it looks more like
          a speaker shot or unrelated b-roll. Try the Characters tab, or save
          a manual frame later via the Animated poster button.
        </p>
      </div>
    );
  }

  // Empty / fallback states (window unavailable, model failure, etc.)
  if (result.candidates.length === 0) {
    return (
      <div className="p-6 space-y-3">
        <p className="text-sm text-fg">
          Could not extract {positionLabel} frame candidates.
        </p>
        <p className="text-xs text-muted leading-relaxed">
          {result.degrade_reason === "zero_duration"
            ? "Video duration is too short to sample frames from."
            : result.degrade_reason === "window_unavailable"
              ? `No suitable ${positionLabel} window could be derived from this video's scenes.`
              : "Something went wrong. Try again in a moment."}
        </p>
      </div>
    );
  }

  const confidenceCopy: Record<string, { color: string; label: string; hint: string }> = {
    high: {
      color: "text-success",
      label: "Strong text composition",
      hint: `Best ${positionLabel} frames are crisp and fully composed — check the ones you want.`,
    },
    medium: {
      color: "text-warning",
      label: "Some text visible",
      hint: `Pick the frame(s) where the most ${positionLabel} text is settled and readable.`,
    },
    low: {
      color: "text-danger",
      label: "Weak text composition",
      hint: `${positionLabel} is mostly motion / speaker shots — pick the least bad one if you need one.`,
    },
  };
  const conf = confidenceCopy[result.confidence] || confidenceCopy.low;

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center gap-2 text-xs">
        <span className={`font-mono text-3xs uppercase tracking-wider2 ${conf.color}`}>
          {conf.label}
        </span>
        <span className="text-muted">·</span>
        <span className="text-muted leading-relaxed">{conf.hint}</span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        {result.candidates.map((c) => {
          const selected = selections.has(c.frame.frame_id);
          return (
            <button
              key={c.frame.frame_id}
              onClick={() => onToggle(c.frame.frame_id)}
              className={[
                "relative block rounded-lg overflow-hidden border-2 transition text-left",
                selected
                  ? "border-accent shadow-md"
                  : "border-border hover:border-fg/30",
              ].join(" ")}
              aria-pressed={selected}
            >
              <div className="aspect-[9/16] bg-black overflow-hidden relative">
                <img
                  src={frameUrl(c.frame.frame_id)}
                  alt={c.note || `${positionLabel} candidate at ${c.frame.timestamp_sec.toFixed(1)}s`}
                  className="w-full h-full object-cover"
                  loading="lazy"
                />
                <span className="absolute top-1.5 left-1.5 font-mono text-3xs px-1.5 py-0.5 rounded bg-black/70 text-white tabular-nums tracking-wider2">
                  {c.frame.timestamp_sec.toFixed(1)}s
                </span>
                <span
                  className={[
                    "absolute top-1.5 right-1.5 w-6 h-6 rounded-full border-2 flex items-center justify-center transition-colors",
                    selected
                      ? "bg-accent border-accent text-white"
                      : "bg-white/30 border-white backdrop-blur-sm text-transparent",
                  ].join(" ")}
                >
                  {selected && <span className="text-xs leading-none">✓</span>}
                </span>
              </div>
              <div className="px-2.5 py-2 bg-bg-muted/40 space-y-1">
                <div className="flex items-center gap-2">
                  <TextCompletenessBar value={c.text_completeness} />
                  <span className="font-mono text-3xs text-muted tabular-nums">
                    {Math.round(c.text_completeness * 100)}%
                  </span>
                </div>
                {c.note && (
                  <p className="text-2xs text-fg/70 leading-snug line-clamp-2">
                    {c.note}
                  </p>
                )}
              </div>
            </button>
          );
        })}
      </div>

      {result.rejected.length > 0 && (
        <details>
          <summary className="cursor-pointer text-muted hover:text-fg font-mono uppercase tracking-wider2 text-3xs">
            {result.rejected.length} rejected · see reasons
          </summary>
          <ul className="mt-2 space-y-1 text-xs text-muted">
            {result.rejected.map((r, i) => (
              <li key={i}>
                <span className="font-mono tabular-nums">
                  {r.frame.timestamp_sec.toFixed(1)}s
                </span>
                {" — "}
                <span className="italic">{r.reason || "no reason given"}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function TextCompletenessBar({ value }: { value: number }) {
  const clamped = Math.max(0, Math.min(1, value));
  // Strong = green, partial = amber, weak = red — same vocabulary as conf.
  const color =
    clamped >= 0.75
      ? "bg-success"
      : clamped >= 0.4
        ? "bg-warning"
        : "bg-danger";
  return (
    <div
      className="h-1 flex-1 bg-bg-muted rounded-full overflow-hidden"
      title={`text completeness ${Math.round(clamped * 100)}%`}
    >
      <div
        className={`h-full ${color} transition-all`}
        style={{ width: `${clamped * 100}%` }}
      />
    </div>
  );
}

function ImportingBody({
  done,
  total,
  errors,
}: {
  done: number;
  total: number;
  errors: Array<{ label: string; reason: string }>;
}) {
  return (
    <div className="p-6 space-y-4">
      <p className="text-sm text-fg">
        Importing {done} / {total}…
      </p>
      <div className="h-2 bg-bg-muted rounded-full overflow-hidden">
        <div
          className="h-full bg-accent transition-all duration-300"
          style={{ width: total > 0 ? `${(done / total) * 100}%` : "0%" }}
        />
      </div>
      {errors.length > 0 && (
        <div className="space-y-1 pt-2">
          <p className="font-mono text-3xs text-danger uppercase tracking-wider2">
            Failures
          </p>
          {errors.map((e, i) => (
            <p key={i} className="text-xs text-danger">
              {e.label}: {e.reason}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
