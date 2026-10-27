// Rebrand — which 42 pattern the Omni arm puts behind the speaker.
//
// Every pattern the server knows (built-in + uploaded) as a grid of preview
// cards. The picked one shows its description, where it goes in the frame and
// an animation select (the pattern's own default unless the user picks another;
// null is sent for "default", so the server stays the source of truth for it).
//
// Uploads are analysed by a model on the way in (~10-40 s): it writes the
// description, the places the pattern may take and the default animation, so
// a new pattern is usable the moment it lands in the grid. A pattern with
// several places ("auto", like the bead row) is placed per clip by the server;
// the card lists the places it can take. Only uploads can be deleted.
//
// Self-loading on purpose: RebrandPanel only keeps the three fields it sends
// (pattern_on, pattern, pattern_animation) and renders this.

import { useEffect, useId, useRef, useState, type ChangeEvent } from "react";

import {
  rebrandApi,
  rebrandErrorDetail,
  type RebrandPattern,
  type RebrandPatternAnimation,
} from "../../lib/rebrandApi";
import { useConfirm } from "../ConfirmProvider";
import { useToast } from "../ToastProvider";
import { Spinner } from "../ui/Spinner";
import { PatternCard, UploadingCard } from "./PatternLibraryGrid";

export interface RebrandPatternValue {
  pattern_on: boolean;
  /** Pattern slug. */
  pattern: string;
  /** null = the pattern's own default animation. */
  pattern_animation: string | null;
}

/** The owner's approved setup and the server's own default — used until the
 *  list loads (and if it never does, the server fills in the same). */
export const DEFAULT_REBRAND_PATTERN = "structure";

/** Same field look as RebrandPanel's INPUT_CLS. */
const INPUT_CLS =
  "w-full rounded-lg border border-border bg-bg px-2.5 py-1.5 text-xs focus:outline-none focus:border-accent disabled:opacity-50 disabled:bg-bg-muted";

/** Display name cap for an upload (the slug is made from it server side). */
const NAME_MAX = 40;

const PLACEMENT_LABEL: Record<RebrandPattern["placement"], string> = {
  left: "Left side",
  right: "Right side",
  top: "Top",
  bottom: "Bottom",
  full: "Full frame",
  auto: "Auto-placed",
};

/** "top-right band" → "Top-right band". */
function capitalise(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

export function RebrandPatternPicker({
  value,
  onChange,
  disabled = false,
  narrow = false,
}: {
  value: RebrandPatternValue;
  onChange: (v: RebrandPatternValue) => void;
  disabled?: boolean;
  /** Compact (it served the first studio's inspector): no on/off switch of
   *  its own, no frame, three cards a row, one column below. The studio now
   *  has its own library (rebrand-studio/PatternLibrary). */
  narrow?: boolean;
}) {
  const toast = useToast();
  const confirm = useConfirm();
  const animId = useId();
  const [patterns, setPatterns] = useState<RebrandPattern[] | null>(null);
  const [animations, setAnimations] = useState<RebrandPatternAnimation[]>([]);
  const [defaultSlug, setDefaultSlug] = useState(DEFAULT_REBRAND_PATTERN);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadName, setUploadName] = useState("");
  // The file being analysed: its name and a local preview for the grid.
  const [uploading, setUploading] = useState<{ name: string; previewUrl: string } | null>(null);
  // Also said in the upload box: inside the Rebrand modal the toast stack sits
  // under the modal's scrim, so the toast alone is easy to miss.
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [deleting, setDeleting] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  // The load and the upload resolve after renders their closures never saw:
  // they read the latest value / onChange from here.
  const valueRef = useRef(value);
  valueRef.current = value;
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  // The animation picked per pattern this session, so going back to a pattern
  // restores that choice instead of its default.
  const animFor = useRef<Record<string, string | null>>({});
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // One emit per action: the parent replaces all three fields with this.
  const emit = (patch: Partial<RebrandPatternValue>) => onChangeRef.current({ ...valueRef.current, ...patch });

  useEffect(() => {
    let cancelled = false;
    setLoadError(null);
    rebrandApi
      .patterns()
      .then((r) => {
        if (cancelled) return;
        setPatterns(r.patterns);
        setAnimations(r.animations);
        const def = r.default_pattern || DEFAULT_REBRAND_PATTERN;
        setDefaultSlug(def);
        // A slug the library no longer has (deleted since, or the server's
        // default moved) falls back to the server's default; an animation it
        // no longer offers falls back to the pattern's own.
        const v = valueRef.current;
        const patch: Partial<RebrandPatternValue> = {};
        if (!r.patterns.some((p) => p.slug === v.pattern)) {
          const fallback = r.patterns.find((p) => p.slug === def) ?? r.patterns[0];
          if (fallback) {
            patch.pattern = fallback.slug;
            patch.pattern_animation = null;
          }
        }
        if (patch.pattern === undefined && v.pattern_animation && !r.animations.some((a) => a.value === v.pattern_animation)) {
          patch.pattern_animation = null;
        }
        if (Object.keys(patch).length) emit(patch);
      })
      .catch((e) => !cancelled && setLoadError(rebrandErrorDetail(e)));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadKey]);

  // Seconds on the clock while the model reads an upload.
  useEffect(() => {
    if (!uploading) return;
    setElapsed(0);
    const t0 = Date.now();
    const id = window.setInterval(() => setElapsed(Math.round((Date.now() - t0) / 1000)), 1000);
    return () => window.clearInterval(id);
  }, [uploading]);

  const selected = patterns?.find((p) => p.slug === value.pattern) ?? null;
  const anim = value.pattern_animation ?? selected?.default_animation ?? "";
  const animHelp = animations.find((a) => a.value === anim)?.help;
  const busy = disabled || !!uploading;

  const pick = (p: RebrandPattern) => {
    if (p.slug === value.pattern) return;
    emit({ pattern: p.slug, pattern_animation: animFor.current[p.slug] ?? null });
  };

  const pickAnimation = (v: string) => {
    if (!selected) return;
    // The pattern's own default goes back as null ("default").
    const next = v === selected.default_animation ? null : v;
    animFor.current[selected.slug] = next;
    emit({ pattern_animation: next });
  };

  const onFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // so picking the same file again still fires
    if (!file) return;
    const name = uploadName.trim().slice(0, NAME_MAX);
    const local = URL.createObjectURL(file);
    setUploadError(null);
    setUploading({ name: name || file.name.replace(/\.[^.]+$/, ""), previewUrl: local });
    try {
      const p = await rebrandApi.uploadPattern(file, name);
      if (!mounted.current) return;
      setPatterns((prev) => [...(prev ?? []).filter((x) => x.slug !== p.slug), p]);
      delete animFor.current[p.slug];
      emit({ pattern: p.slug, pattern_animation: null });
      setUploadOpen(false);
      setUploadName("");
      toast.success("Pattern added", p.where ? `${p.name} — ${p.where}.` : p.name);
    } catch (err) {
      const detail = rebrandErrorDetail(err);
      toast.error("Couldn't add the pattern", detail);
      if (mounted.current) setUploadError(detail);
    } finally {
      URL.revokeObjectURL(local);
      if (mounted.current) setUploading(null);
    }
  };

  const remove = async (p: RebrandPattern) => {
    const ok = await confirm({
      title: "Delete this pattern?",
      message: `“${p.name}” will no longer be offered for new rebrands.`,
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    setDeleting(p.slug);
    try {
      await rebrandApi.deletePattern(p.slug);
      if (!mounted.current) return;
      setPatterns((prev) => (prev ?? []).filter((x) => x.slug !== p.slug));
      delete animFor.current[p.slug];
      if (valueRef.current.pattern === p.slug) {
        emit({ pattern: defaultSlug, pattern_animation: animFor.current[defaultSlug] ?? null });
      }
      toast.success("Pattern deleted", p.name);
    } catch (err) {
      toast.error("Couldn't delete the pattern", rebrandErrorDetail(err));
    } finally {
      if (mounted.current) setDeleting(null);
    }
  };

  return (
    <div className={narrow ? "space-y-3" : "rounded-lg border border-border bg-bg p-3 space-y-3"}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        {narrow ? (
          <span className="text-2xs text-muted leading-snug min-w-0 flex-1">
            Pick a pattern, or upload your own — the AI works out where it fits.
          </span>
        ) : (
          <label className="flex items-start gap-2 cursor-pointer min-w-0">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={value.pattern_on}
              disabled={disabled}
              onChange={(e) => emit({ pattern_on: e.target.checked })}
            />
            <span className="min-w-0">
              <span className="font-semibold">Brand pattern</span>
              <span className="block text-2xs text-muted leading-snug">
                {value.pattern_on
                  ? "One of 42's patterns, placed in the shot behind the speaker and animated in."
                  : "Off: Omni rebrands without a pattern."}
              </span>
            </span>
          </label>
        )}
        {value.pattern_on && patterns && (
          <button
            type="button"
            className="btn btn-ghost text-xs shrink-0"
            disabled={busy}
            onClick={() => {
              setUploadOpen((v) => !v);
              setUploadError(null);
            }}
            aria-expanded={uploadOpen}
          >
            ⬆ Upload pattern
          </button>
        )}
      </div>

      {/* Always mounted, so the upload box's button can open it. */}
      <input
        ref={fileRef}
        type="file"
        accept="image/png,image/jpeg,image/webp"
        className="hidden"
        onChange={onFile}
      />

      {value.pattern_on && (
        <>
          {!patterns && !loadError && (
            <div className="flex items-center gap-2 text-2xs text-muted">
              <Spinner size="xs" /> Loading patterns…
            </div>
          )}

          {loadError && (
            <p className="text-2xs text-amber-700 bg-warning/10 border border-warning/30 rounded-lg px-2.5 py-2 leading-relaxed">
              Couldn&apos;t load the pattern library ({loadError}), so the run uses the default pattern.{" "}
              <button type="button" className="underline hover:no-underline" onClick={() => setReloadKey((k) => k + 1)}>
                Retry
              </button>
            </p>
          )}

          {uploadOpen && patterns && (
            <div className="rounded-lg border border-dashed border-border-strong p-2.5 space-y-2">
              {uploading ? (
                <div className="flex items-center gap-2 text-xs" role="status">
                  <Spinner size="sm" tone="accent" />
                  <span>
                    Analysing your pattern with AI…{" "}
                    <span className="text-muted tabular-nums">{elapsed}s · usually 10–40 s</span>
                  </span>
                </div>
              ) : (
                <>
                  <div className="flex flex-wrap items-center gap-2">
                    <input
                      type="text"
                      value={uploadName}
                      maxLength={NAME_MAX}
                      disabled={busy}
                      onChange={(e) => setUploadName(e.target.value.slice(0, NAME_MAX))}
                      placeholder="Name (optional)"
                      aria-label="Pattern name (optional)"
                      className={`${INPUT_CLS} sm:max-w-[16rem]`}
                    />
                    <button
                      type="button"
                      className="btn btn-accent text-xs"
                      disabled={busy}
                      onClick={() => fileRef.current?.click()}
                    >
                      Choose image…
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost text-xs"
                      onClick={() => {
                        setUploadOpen(false);
                        setUploadError(null);
                      }}
                    >
                      Cancel
                    </button>
                  </div>
                  {uploadError && (
                    <p className="text-2xs text-danger bg-danger/10 border border-danger/30 rounded-lg px-2.5 py-1.5 leading-snug">
                      Couldn&apos;t add the pattern: {uploadError}
                    </p>
                  )}
                  <p className="text-2xs text-muted leading-snug">
                    PNG, JPG or WebP. The AI reads it and works out where it can go in the frame (one place, or
                    several to fit to each clip) and how it moves in; you can change the animation afterwards.
                  </p>
                </>
              )}
            </div>
          )}

          {patterns && patterns.length === 0 && !uploading && (
            <p className="text-2xs text-muted">No patterns yet — upload one.</p>
          )}

          {patterns && (patterns.length > 0 || uploading) && (
            <div
              className={`grid gap-2 ${narrow ? "grid-cols-3" : "grid-cols-3 sm:grid-cols-4 md:grid-cols-6"}`}
              role="radiogroup"
              aria-label="Brand pattern"
            >
              {patterns.map((p) => (
                <PatternCard
                  key={p.slug}
                  p={p}
                  selected={p.slug === value.pattern}
                  disabled={busy}
                  deleting={deleting === p.slug}
                  onSelect={() => pick(p)}
                  onDelete={() => void remove(p)}
                />
              ))}
              {uploading && <UploadingCard name={uploading.name} previewUrl={uploading.previewUrl} />}
            </div>
          )}

          {selected && (
            <div className={`grid gap-3 ${narrow ? "" : "sm:grid-cols-2"}`}>
              <div className="space-y-1 min-w-0">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="font-semibold">{selected.name}</span>
                  {!narrow && (
                    <span className="text-2xs px-1.5 py-0.5 rounded-full border border-border text-muted">
                      {PLACEMENT_LABEL[selected.placement] ?? selected.placement}
                    </span>
                  )}
                  {selected.source === "upload" && (
                    <span className="text-2xs px-1.5 py-0.5 rounded-full border border-accent/30 bg-accent-fade text-accent-hover">
                      Uploaded
                    </span>
                  )}
                </div>
                {selected.description && (
                  <p className="text-2xs text-muted leading-snug">{selected.description}</p>
                )}
                {selected.where && !narrow && (
                  <p className="text-2xs leading-snug">
                    <span className="text-muted">Where:</span> {selected.where}
                  </p>
                )}
                {!narrow && selected.placement === "auto" && (selected.placements?.length ?? 0) > 1 && (
                  <div className="space-y-1">
                    <ul className="flex flex-wrap gap-1" aria-label="Places it can take">
                      {selected.placements!.map((c, i) => (
                        <li
                          key={`${c.placement}-${c.anchor}-${i}`}
                          className="text-3xs px-1.5 py-0.5 rounded-full border border-border text-muted"
                        >
                          {capitalise(c.label)}
                        </li>
                      ))}
                    </ul>
                    <p className="text-2xs text-muted leading-snug">
                      Each rebrand scores these on the clip&apos;s own first frame (the speaker, their head,
                      the background, any letterbox bars) and picks one of the best fits at random.
                    </p>
                  </div>
                )}
              </div>
              {animations.length > 0 && (
                <div className="space-y-1">
                  <label className="text-muted block" htmlFor={animId}>
                    Animation
                  </label>
                  <select
                    id={animId}
                    value={anim}
                    disabled={busy}
                    onChange={(e) => pickAnimation(e.target.value)}
                    className={INPUT_CLS}
                  >
                    {animations.map((a) => (
                      <option key={a.value} value={a.value}>
                        {a.label}
                        {a.value === selected.default_animation ? " (default)" : ""}
                      </option>
                    ))}
                  </select>
                  {animHelp && <p className="text-2xs text-muted leading-snug">{animHelp}</p>}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
