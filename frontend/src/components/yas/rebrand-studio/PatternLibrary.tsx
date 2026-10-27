// Rebrand studio — the pattern library, a view pushed into the rail (never a
// dialog over the dialog, which would share its Escape).
//
//   add    tap to tick one or more, then "Add 2 patterns". Cards that can't
//          join say why: the cap ("Up to 3"), or a full-screen pattern, which
//          goes alone (ticking one clears the others, and back).
//   swap   tap one to put it in the place of the pattern being swapped.
//
// The last tile uploads your own: the server's model reads it (10-40 s) and
// it lands ticked. Uploaded patterns can be deleted here too.
// Esc and Back return to the tab, and focus to what opened this.

import { useEffect, useRef, useState, type ChangeEvent } from "react";
import { ArrowLeft, Upload } from "lucide-react";

import { isShown } from "../../../lib/isShown";
import { rebrandApi, rebrandErrorDetail, type RebrandPattern } from "../../../lib/rebrandApi";
import type { HarnessCatalogue, HarnessRecipe } from "../../../lib/rebrandHarnessApi";
import {
  addBlocker,
  enabledPatterns,
  entryOf,
  isFullOnly,
  patternLimit,
  slugOf,
  transitionOf,
} from "../../../lib/rebrandRecipe";
import { spanOn, startingAfter } from "../../../lib/rebrandTiming";
import { useConfirm } from "../../ConfirmProvider";
import { useToast } from "../../ToastProvider";
import { PatternCard, UploadingCard } from "../PatternLibraryGrid";
import { COPY } from "./studioCopy";

export function PatternLibrary({
  mode,
  catalogue,
  recipe,
  swapId,
  onAdd,
  onSwap,
  onBack,
  onPatternsChanged,
  duration = null,
}: {
  mode: "add" | "swap";
  catalogue: HarnessCatalogue;
  recipe: HarnessRecipe;
  /** The element whose pattern is swapped (swap mode). */
  swapId: string | null;
  onAdd: (slugs: string[]) => void;
  onSwap: (slug: string) => void;
  onBack: () => void;
  onPatternsChanged: (patterns: RebrandPattern[]) => void;
  /** The scene's length (s): a transition makes room in it (null: not
   *  known yet). */
  duration?: number | null;
}) {
  const toast = useToast();
  const confirm = useConfirm();
  const [ticked, setTicked] = useState<string[]>([]);
  const [uploading, setUploading] = useState<{ name: string; previewUrl: string; startedAt: number } | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const headingRef = useRef<HTMLHeadingElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const onBackRef = useRef(onBack);
  onBackRef.current = onBack;
  const mounted = useRef(true);
  // "Delete this pattern?" is open: Esc is its Cancel, not Back.
  const confirming = useRef(false);
  const patternsRef = useRef(catalogue.patterns);
  patternsRef.current = catalogue.patterns;

  useEffect(() => {
    mounted.current = true;
    headingRef.current?.focus();
    // Esc goes back here, before the studio (on window) would close. The
    // confirm listens on document too (after this), so while it is open Esc
    // is left to it — still kept from the studio.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || !isShown(headingRef.current)) return;
      e.stopPropagation();
      if (confirming.current) return;
      onBackRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      mounted.current = false;
      document.removeEventListener("keydown", onKey);
    };
  }, []);

  useEffect(() => {
    if (!uploading) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [uploading]);

  const limit = patternLimit(catalogue);
  const swapEl = swapId ? recipe.elements.find((e) => e.id === swapId) ?? null : null;
  const swapSlug = swapEl ? slugOf(swapEl) : null;
  const swapName = (swapSlug && entryOf(catalogue, swapSlug)?.name) || "pattern";
  const othersOn = enabledPatterns(recipe).filter((e) => e.id !== swapId).length;

  // A full-screen pattern that is not a transition goes alone the whole
  // scene: ticking one clears every other tick (and any other clears it).
  // A transition (Blueprint) goes with the others — they start once it is
  // over — but one at a time.
  const alone = (slug: string) => isFullOnly(entryOf(catalogue, slug)) && !transitionOf(catalogue, slug);
  const kept = (p: RebrandPattern, prev: string[]) =>
    alone(p.slug) ? [] : prev.filter((s) => s !== p.slug && !alone(s) && !(transitionOf(catalogue, p.slug) && transitionOf(catalogue, s)));

  /** Why this card can't be ticked (null: it can). The ticks ticking it
   *  would clear don't count. */
  const blocker = (p: RebrandPattern) => {
    if (mode === "swap") {
      if (!isFullOnly(p)) return null;
      const t = transitionOf(catalogue, p.slug);
      if (!t) return othersOn > 0 ? ("full-alone" as const) : null;
      // A transition in its place: the others start once it is over.
      const d = duration ?? 1e6;
      const others = enabledPatterns(recipe).filter((e) => e.id !== swapId);
      const early = others.filter((e) => (spanOn(e, d)?.from ?? d) < t.sec - 1e-9);
      return early.some((e) => isFullOnly(entryOf(catalogue, slugOf(e)))) || startingAfter(early, t.sec, d) === null
        ? ("full-alone" as const)
        : null;
    }
    return addBlocker(recipe, catalogue, p.slug, kept(p, ticked), duration);
  };

  const toggle = (p: RebrandPattern) => {
    if (mode === "swap") {
      if (p.slug !== swapSlug) onSwap(p.slug);
      else onBack();
      return;
    }
    setTicked((prev) => (prev.includes(p.slug) ? prev.filter((s) => s !== p.slug) : [...kept(p, prev), p.slug]));
  };

  const onFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const local = URL.createObjectURL(file);
    setUploadError(null);
    setUploading({ name: file.name.replace(/\.[^.]+$/, ""), previewUrl: local, startedAt: Date.now() });
    try {
      const p = await rebrandApi.uploadPattern(file);
      if (!mounted.current) return;
      onPatternsChanged([...patternsRef.current.filter((x) => x.slug !== p.slug), p]);
      toast.success(COPY.libAdded, p.name);
      if (mode === "swap") onSwap(p.slug);
      else if (!blocker(p)) setTicked((prev) => [...kept(p, prev), p.slug]);
    } catch (err) {
      if (mounted.current) setUploadError(rebrandErrorDetail(err));
    } finally {
      URL.revokeObjectURL(local);
      if (mounted.current) setUploading(null);
    }
  };

  const remove = async (p: RebrandPattern) => {
    confirming.current = true;
    let ok = false;
    try {
      ok = await confirm({
        title: COPY.libDeleteTitle,
        message: COPY.libDeleteMessage(p.name),
        confirmLabel: COPY.libDeleteConfirm,
        danger: true,
      });
    } finally {
      confirming.current = false;
    }
    if (!ok) return;
    setDeleting(p.slug);
    try {
      await rebrandApi.deletePattern(p.slug);
      if (!mounted.current) return;
      onPatternsChanged(patternsRef.current.filter((x) => x.slug !== p.slug));
      setTicked((prev) => prev.filter((s) => s !== p.slug));
      toast.success(COPY.libDeleted, p.name);
    } catch (err) {
      toast.error(COPY.libDeleteFailed, rebrandErrorDetail(err));
    } finally {
      if (mounted.current) setDeleting(null);
    }
  };

  const title = mode === "add" ? COPY.libAdd : COPY.libSwap(swapName);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-1.5 border-b border-border px-2 py-1.5">
        <button
          type="button"
          onClick={onBack}
          aria-label={COPY.back}
          title={COPY.back}
          className="h-8 w-8 rounded-lg flex items-center justify-center text-muted hover:text-fg hover:bg-bg-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden />
        </button>
        <h3 ref={headingRef} tabIndex={-1} className="text-xs font-semibold text-fg focus:outline-none">
          {title}
        </h3>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-3 space-y-2.5">
        <p className="text-2xs text-muted">{mode === "add" ? COPY.libAddHint : COPY.libSwapHint}</p>
        <div
          role={mode === "add" ? "group" : "radiogroup"}
          aria-label={title}
          className="grid grid-cols-3 gap-2"
        >
          {catalogue.patterns.map((p) => {
            const why = blocker(p);
            const on = mode === "add" ? ticked.includes(p.slug) : p.slug === swapSlug;
            const caption = why && !on
              ? why === "cap"
                ? COPY.blocked.cap(limit)
                : COPY.blocked[why]
              : transitionOf(catalogue, p.slug)
                ? COPY.libTransitionCaption(transitionOf(catalogue, p.slug)!.sec)
                : isFullOnly(p)
                  ? COPY.libFullCaption
                  : undefined;
            const titleText = why && !on ? (why === "cap" ? COPY.blockedTitle.cap(limit) : COPY.blockedTitle[why]) : undefined;
            return (
              <PatternCard
                key={p.slug}
                p={p}
                role={mode === "add" ? "checkbox" : "radio"}
                selected={on}
                disabled={(!!why && !on) || !!uploading}
                deleting={deleting === p.slug}
                caption={caption}
                title={titleText}
                onSelect={() => toggle(p)}
                onDelete={() => void remove(p)}
              />
            );
          })}
          {uploading ? (
            <UploadingCard
              name={uploading.name}
              previewUrl={uploading.previewUrl}
              note={COPY.libUploading(Math.max(0, Math.round((now - uploading.startedAt) / 1000)))}
            />
          ) : (
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              className="flex min-w-0 flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-border-strong px-1 py-3 text-2xs text-muted transition-colors hover:border-accent hover:text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
            >
              <Upload className="h-4 w-4" aria-hidden />
              <span className="font-medium">{COPY.libUpload}</span>
              <span className="text-3xs">{COPY.libUploadHint}</span>
            </button>
          )}
        </div>
        <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp" className="hidden" onChange={onFile} />
        {uploadError && (
          <p className="rounded-lg border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-2xs text-danger">
            {COPY.libUploadFailed} {uploadError}
          </p>
        )}
      </div>

      {mode === "add" && (
        <div className="shrink-0 border-t border-border p-3">
          <button
            type="button"
            className="btn btn-primary btn-sm w-full"
            disabled={!ticked.length || !!uploading}
            onClick={() => onAdd(ticked)}
          >
            {COPY.libAddButton(ticked.length)}
          </button>
        </div>
      )}
    </div>
  );
}
