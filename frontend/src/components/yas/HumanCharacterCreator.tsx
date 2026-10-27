/**
 * Yas Producer · Human Character (Ideation → Human Character).
 *
 * A photorealistic-person generator that mirrors Create Poster's UX (topic on the
 * left, generated grid on the right) but produces real-looking humans instead of
 * branded graphics:
 *   1. gpt-4o "casts" N distinct candidate people who fit the brief.
 *   2. gemini-3-pro-image renders each as a clean front-facing portrait — NO 42
 *      logo, NO text (the poster logo firewall is skipped server-side).
 *
 *   - No references, no style toggle, no Explore, no Instagram handoff.
 *   - Save a favourite → it lands in Assets as a `character`
 *     (content_category img_human_character) and becomes selectable as a Talking
 *     Head presenter reference.
 *   - Server-side rate-limited (shares the poster 20/hour image-gen cap).
 *   - 24h rehydrate via /human-character/recent.
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { JobProgress } from "../ui/JobProgress";

import {
  HUMAN_CHARACTER_FORMATS,
  aspectClass as aspectClassFor,
} from "../../lib/aspect";
import { FormatPicker } from "./FormatPicker";
import { api } from "../../lib/api";
import { fmtRel } from "../../lib/format";
import type { ProducerBrief } from "../../lib/producerBrief";
import { useProducerAssetsStore } from "../../lib/store";
import { usePersistentState } from "../../lib/usePersistentState";
import { useConfirm } from "../ConfirmProvider";
import { useToast } from "../ToastProvider";
import { EnhanceButton } from "./EnhanceButton";
import type { YasHumanCharacterAspect, YasHumanCharacterGeneration } from "../../lib/types";

// Output formats and the picker that draws them are shared with the two poster
// surfaces now: this screen already said the ratio and let the line underneath
// explain it, and that convention won. See lib/aspect.ts.

/** This map moved to lib/aspect.ts, which the two inline ternaries elsewhere
 *  now share. A thin alias keeps the call sites below reading unchanged. */
const ASPECT_CLASS = (a: YasHumanCharacterAspect) => aspectClassFor(a, "1:1");

// ---------------------------------------------------------------------------
// Minimal inline icons (same set/stroke as PosterCreator).
// ---------------------------------------------------------------------------

type IconProps = { className?: string };

function ChevronLeftIcon({ className = "w-4 h-4" }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <path d="M15 18l-6-6 6-6"/>
    </svg>
  );
}
function PersonIcon({ className = "w-8 h-8" }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <circle cx="12" cy="8" r="4"/>
      <path d="M4 21c0-4 3.6-6.5 8-6.5s8 2.5 8 6.5"/>
    </svg>
  );
}

// ---------------------------------------------------------------------------
// One candidate cell. Hover reveals Save (→ Assets as a character reference).
// ---------------------------------------------------------------------------

function CharacterCard({
  gen,
  saving,
  disabled,
  aspectClass,
  onSave,
}: {
  gen: YasHumanCharacterGeneration;
  saving: boolean;
  disabled: boolean;
  /** Tailwind aspect class for the cell — matches the selected output format. */
  aspectClass: string;
  onSave: () => void;
}) {
  const isReady = gen.status === "ready" || gen.status === "saved_to_assets";
  const isSaved = gen.status === "saved_to_assets";

  if (gen.status === "failed") {
    return (
      <div className={`${aspectClass} rounded-lg bg-danger/10 border border-danger/30 flex flex-col items-center justify-center p-3 text-center`}>
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
      <div className={`${aspectClass} rounded-lg bg-bg-muted flex items-center justify-center`}>
        <span className="inline-block w-6 h-6 border-2 border-accent border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className={`group relative ${aspectClass} rounded-lg overflow-hidden bg-black`}>
      <img
        src={api.yasProducer.humanCharacter.fileUrl(gen.id, gen.created_at)}
        alt={gen.topic || `Candidate ${gen.id}`}
        className="w-full h-full object-contain"
      />
      {isSaved && (
        <span className="absolute top-1.5 right-1.5 w-6 h-6 rounded-full bg-success text-white flex items-center justify-center shadow text-xs z-10">
          ✓
        </span>
      )}
      <div className="absolute inset-x-0 bottom-0 p-2 flex items-center justify-end gap-2 bg-gradient-to-t from-black/80 to-transparent opacity-0 group-hover:opacity-100 transition-opacity">
        <button
          onClick={onSave}
          disabled={disabled || isSaved}
          title={isSaved ? "Already saved to Assets" : "Save to Assets (Human Characters)"}
          className="rounded-md bg-white/95 text-black text-xs font-semibold px-3 py-1.5 hover:bg-white disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {saving ? "Saving…" : isSaved ? "Saved ✓" : "Save"}
        </button>
      </div>
    </div>
  );
}

interface Props {
  onBack: () => void;
  /** Trend Finder "Make this" seed. One-shot, applied at mount only — the
   *  roadmap bumps projectNonce for every hand-off, so this component is always
   *  freshly mounted when a brief arrives. */
  initialBrief?: ProducerBrief | null;
}

export function HumanCharacterCreator({ onBack, initialBrief = null }: Props) {
  // Draft inputs + on-screen batch persist to localStorage so a refresh keeps them.
  const [topic, setTopic] = usePersistentState("yas.humanchar.topic", "");
  const [aspect, setAspect] = usePersistentState<YasHumanCharacterAspect>("yas.humanchar.aspect", "1:1");
  const [currentBatch, setCurrentBatch] = usePersistentState<YasHumanCharacterGeneration[]>(
    "yas.humanchar.batch",
    [],
  );
  const [attempts, setAttempts] = useState<YasHumanCharacterGeneration[]>([]);
  const [phase, setPhase] = useState<"idle" | "generating">("idle");

  // When the run started, so JobProgress can show elapsed time. Derived from the
  // phase flip rather than threaded through every call site that starts a job.
  const [startedAt, setStartedAt] = useState(0);
  useEffect(() => {
    if (phase === "generating") setStartedAt(Date.now());
  }, [phase]);
  const [savingId, setSavingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [enhancingTopic, setEnhancingTopic] = useState(false);

  const generateLockRef = useRef(false);
  const toast = useToast();
  const confirm = useConfirm();
  const assetsStore = useProducerAssetsStore();

  // Trend Finder "Make this" — seed the casting brief. This box expects a
  // PERSON (its Enhance kind is "character"), which is why the brief carries a
  // dedicated `character` field; `topic` is only the fallback. Mount-only: the
  // roadmap remounts this creator (projectNonce) for every hand-off, and its
  // localStorage key is wiped moments before that, so the prop is the route.
  useEffect(() => {
    if (!initialBrief) return;
    setTopic(initialBrief.character || initialBrief.topic);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Rehydrate the 24h history on mount. Cold start (no persisted batch) also
  // shows the most-recent ready candidate so the canvas isn't empty.
  useEffect(() => {
    let cancelled = false;
    const hadPersistedBatch = currentBatch.length > 0;
    api.yasProducer.humanCharacter
      .recent()
      .then((r) => {
        if (cancelled) return;
        const list = r.items || [];
        setAttempts(list);
        if (hadPersistedBatch) return;
        const resume = list.find((g) => g.status === "ready") || null;
        if (resume) setCurrentBatch([resume]);
      })
      .catch(() => {/* non-fatal */});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const hasResult = currentBatch.length > 0;
  const resultTopic =
    phase === "generating" ? topic.trim() : currentBatch[0]?.topic || "";
  const canGenerate = phase === "idle" && topic.trim().length > 0;

  function patchGeneration(id: number, patch: Partial<YasHumanCharacterGeneration>) {
    setCurrentBatch((batch) => batch.map((g) => (g.id === id ? { ...g, ...patch } : g)));
    setAttempts((prev) => prev.map((g) => (g.id === id ? { ...g, ...patch } : g)));
  }

  async function enhanceTopic() {
    const text = topic.trim();
    if (!text || enhancingTopic) return;
    setEnhancingTopic(true);
    try {
      // "character" kind: casting-brief enhancement (describes the person),
      // not the poster-copy "topic" enhancement.
      const r = await api.yasProducer.enhance(text, "character");
      if (r.enhanced) setTopic(r.enhanced);
    } catch (err: any) {
      toast.error("Couldn't enhance", err?.message || String(err));
    } finally {
      setEnhancingTopic(false);
    }
  }

  async function runGenerate() {
    if (!canGenerate) return;
    if (generateLockRef.current) return;
    generateLockRef.current = true;
    setError(null);
    setPhase("generating");
    try {
      const batch = await api.yasProducer.humanCharacter.generate({ topic: topic.trim(), aspect });
      setCurrentBatch(batch.items);
      setAttempts((prev) => [...batch.items, ...prev]);
      const total = batch.items.reduce((s, g) => s + Number(g.cost_usd || 0), 0);
      const ok = batch.items.filter((g) => g.status === "ready").length;
      toast.info(`${ok} candidate${ok === 1 ? "" : "s"} generated`, `~$${total.toFixed(2)}`);
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
      setPhase("idle");
    }
  }

  // Save a chosen candidate into the Assets library (Human Characters card).
  async function handleSaveCard(gen: YasHumanCharacterGeneration) {
    if (savingId != null || gen.status !== "ready") return;
    setSavingId(gen.id);
    try {
      const asset = await api.yasProducer.humanCharacter.saveToAssets(gen.id, {
        display_name: topic.trim() || gen.topic || `Character #${gen.id}`,
      });
      patchGeneration(gen.id, { status: "saved_to_assets", saved_asset_id: asset.id });
      assetsStore.invalidate();
      assetsStore.fetch({ limit: 500 });
      toast.info("Saved to Human Characters", asset.display_name || "");
    } catch (err: any) {
      const msg = err?.message || String(err);
      if (msg.includes("already_saved") || msg.includes("409")) {
        toast.info("Already saved", "View it in Assets → Human Characters");
        patchGeneration(gen.id, { status: "saved_to_assets" });
      } else {
        toast.error("Save failed", msg);
      }
    } finally {
      setSavingId(null);
    }
  }

  function viewSingle(gen: YasHumanCharacterGeneration) {
    setCurrentBatch([gen]);
  }

  async function startOver() {
    if (attempts.length > 0) {
      if (
        !(await confirm({
          title: "Clear everything?",
          message:
            "This clears the brief and the candidates on screen. Your saved characters stay in Assets.",
          confirmLabel: "Clear",
        }))
      ) {
        return;
      }
    }
    setTopic("");
    setCurrentBatch([]);
    setAttempts([]);
    setError(null);
  }

  const gridCells = useMemo(() => currentBatch, [currentBatch]);
  const cellAspect = ASPECT_CLASS(aspect);

  return (
    <div className="space-y-5">
      <header className="flex items-center gap-3">
        <button onClick={onBack} className="btn btn-ghost text-sm inline-flex items-center gap-1">
          <ChevronLeftIcon /> Back
        </button>
        <h1 className="h1 flex-1">Human Character</h1>
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
            <span className="eyebrow mb-1.5 block">Describe the person</span>
            <div className="relative">
              <textarea
                value={topic}
                onChange={(e) => setTopic(e.target.value)}
                placeholder="e.g. A friendly young Emirati woman, software engineer, smart-casual, warm smile."
                rows={3}
                disabled={enhancingTopic}
                className="w-full rounded-lg border border-border bg-bg-subtle px-3 py-2 pr-24 text-sm focus:outline-none focus:border-accent resize-none disabled:opacity-60"
              />
              <EnhanceButton
                onClick={enhanceTopic}
                disabled={enhancingTopic || topic.trim().length === 0}
                busy={enhancingTopic}
                busyLabel="Polishing…"
                title="Polish into a richer, more specific brief"
                className="disabled:cursor-not-allowed"
              />
            </div>
            <p className="text-3xs text-muted mt-1.5 leading-snug">
              Front-facing photorealistic portraits — no logo, no text. Save your
              favourite to use as a Human Element Video presenter.
            </p>
          </label>

          <FormatPicker
            options={HUMAN_CHARACTER_FORMATS}
            value={aspect}
            onChange={setAspect}
          />

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
                  Generating 4 candidates…
                </span>
              ) : (
                <>{hasResult ? "Generate a new set" : "Generate 4 characters"}</>
              )}
            </button>
            {phase === "idle" && (
              <p className="text-2xs text-muted text-center mt-1.5">
                gpt-4o + Nano Banana Pro · 4 candidates · ~$0.55 · about 30-60s · no logo
              </p>
            )}
            {phase === "generating" && (
              <JobProgress className="mt-2" startedAt={startedAt} kind="human_character" />
            )}
          </div>
          {error && <p className="text-xs text-danger" role="alert">{error}</p>}
        </section>

        {/* RIGHT: Result grid */}
        <div className="space-y-4 min-w-0">
          {!hasResult && phase !== "generating" && (
            <section className="card p-10 text-center border-dashed flex flex-col items-center justify-center min-h-[400px]">
              <div className="text-muted mb-3"><PersonIcon className="w-12 h-12" /></div>
              <p className="text-sm text-fg font-medium">Your character candidates will appear here</p>
              <p className="text-xs text-muted mt-1 max-w-sm mx-auto leading-relaxed">
                Describe the person and hit Generate. You'll get 4 candidates — save
                your favourite to Assets and it becomes a Human Element Video reference.
              </p>
            </section>
          )}

          {(hasResult || phase === "generating") && (
            <section className="card p-4 space-y-3">
              {resultTopic && (
                <div className="flex items-baseline gap-2 min-w-0">
                  <span className="eyebrow shrink-0">
                    {phase === "generating" ? "Generating" : "Candidates for"}
                  </span>
                  <span className="text-sm font-medium text-fg truncate" title={resultTopic}>
                    {resultTopic}
                  </span>
                </div>
              )}
              <div className="grid grid-cols-2 gap-3">
                {phase === "generating"
                  ? Array.from({ length: 4 }).map((_, i) => (
                      <div
                        key={i}
                        className={`${cellAspect} rounded-lg bg-bg-muted animate-pulse flex items-center justify-center`}
                      >
                        <span className="inline-block w-6 h-6 border-2 border-accent border-t-transparent rounded-full animate-spin" />
                      </div>
                    ))
                  : gridCells.map((g) => (
                      <CharacterCard
                        key={g.id}
                        gen={g}
                        saving={savingId === g.id}
                        disabled={phase !== "idle" || savingId != null}
                        aspectClass={cellAspect}
                        onSave={() => handleSaveCard(g)}
                      />
                    ))}
              </div>
              {phase === "generating" && (
                <p className="text-2xs text-muted text-center">
                  Rendering 4 candidates · ~30-60 seconds
                </p>
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
                  return (
                    <button
                      key={g.id}
                      onClick={() => viewSingle(g)}
                      title={`#${g.id} · ${fmtRel(g.created_at)}`}
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
                          src={api.yasProducer.humanCharacter.fileUrl(g.id, g.created_at)}
                          alt={`Candidate ${g.id}`}
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
                      {isSaved && (
                        <span className="absolute top-1 right-1 w-5 h-5 rounded-full bg-success text-white flex items-center justify-center shadow text-3xs">
                          ✓
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
    </div>
  );
}
