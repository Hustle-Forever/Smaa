/**
 * Advanced: the brand profile synthesizer's own controls.
 *
 * WHOSE THIS IS. The mode switch, the eight weights, the four include flags and
 * the model settings are Agegnew's feature, end to end: `WeightSlider`,
 * `lib/hints.ts`, `SettingsPatch` and `settings_service.build_hints` are all his
 * and are all restored exactly as he wrote them. They were removed from this app
 * and are back at the owner's request, unmodified and unpatched, so that the
 * person who built them is the one who decides what happens to them next.
 *
 * NOTHING IN HERE IS FIXED, INCLUDING THINGS THAT LOOK LIKE BUGS. That is the
 * point of restoring it rather than rewriting it: a half-repaired feature is
 * harder to review than an intact one. Known behaviour worth raising as issues
 * rather than patching quietly is listed at the bottom of this file.
 *
 * WHAT IS NEW IS ONLY THE FRAME. The controls used to be the whole Settings
 * page; now they are one tab of it, they use this app's `SaveBar` like every
 * other section, and the owner's sentence is not duplicated here because "Your
 * brand" already owns that field. The controls themselves, their labels, their
 * ranges and what they do are untouched.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "../../lib/api";
import { buildHints } from "../../lib/hints";
import type { Settings as TSettings } from "../../lib/types";
import { Skeleton } from "../Skeleton";
import { Toggle } from "../Toggle";
import { useToast } from "../ToastProvider";
import { WeightSlider } from "../WeightSlider";
import { Notice, SaveBar } from "../ui/controls";
import { ErrorCard, SectionHeader } from "../ui/primitives";

export function AdvancedSection() {
  const toast = useToast();
  const [original, setOriginal] = useState<TSettings | null>(null);
  const [draft, setDraft] = useState<TSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const s = await api.settings();
        if (cancelled) return;
        setOriginal(s);
        setDraft(s);
      } catch (e: any) {
        if (!cancelled) setError(String(e?.message || e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const dirty = useMemo(() => {
    if (!original || !draft) return false;
    return JSON.stringify(original) !== JSON.stringify(draft);
  }, [original, draft]);

  const hints = useMemo(() => (draft ? buildHints(draft) : null), [draft]);

  const update = useCallback(
    <K extends keyof TSettings>(key: K, value: TSettings[K]) => {
      setDraft((d) => (d ? { ...d, [key]: value } : d));
    },
    [],
  );

  const save = useCallback(async () => {
    if (!draft || !dirty) return;
    setError(null);
    setSaving(true);
    try {
      // Send the whole sanitised draft; the backend whitelists the fields.
      const patch = { ...draft };
      delete (patch as any).id;
      delete (patch as any).created_at;
      delete (patch as any).updated_at;
      const updated = await api.updateSettings(patch);
      setOriginal(updated);
      setDraft(updated);
      toast.success("Settings saved");
    } catch (e: any) {
      const msg = String(e?.message || e);
      setError(msg);
      toast.error("Couldn't save", msg);
    } finally {
      setSaving(false);
    }
  }, [draft, dirty, toast]);

  if (loading || !draft) {
    return (
      <div className="space-y-3">
        <Skeleton h="h-6" className="w-64" />
        <Skeleton h="h-40" className="w-full" />
        {error && <ErrorCard message={error} />}
      </div>
    );
  }

  const isManual = draft.mode === "manual";

  return (
    <section aria-labelledby="set-advanced" className="space-y-4">
      <SectionHeader id="set-advanced" title="Advanced">
        These change how your brand profile is put together. If you are not sure
        what a number does, leave the mode on Auto and nothing here applies.
      </SectionHeader>

      {error && <ErrorCard message={error} />}

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_320px] gap-6 items-start">
        <div className="space-y-4">
          {/* Mode */}
          <section className="card p-5">
            <p className="eyebrow mb-3">Mode</p>
            <div className="inline-flex rounded border border-border overflow-hidden">
              {(["auto", "manual"] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => update("mode", m)}
                  className={[
                    "px-4 py-1.5 text-sm font-medium transition-colors",
                    draft.mode === m
                      ? "bg-fg text-bg"
                      : "bg-bg-subtle text-fg/80 hover:bg-bg-muted",
                  ].join(" ")}
                >
                  {m === "auto" ? "Auto (defaults)" : "Manual"}
                </button>
              ))}
            </div>
            <p className="font-mono text-2xs text-muted mt-3">
              {isManual
                ? "Weights apply. Phase budgets scale with the visual / text weights."
                : "Auto uses historical defaults. Weight sliders are disabled."}
            </p>
          </section>

          {/* Source mix */}
          <section className="card p-5">
            <p className="eyebrow mb-3">Source mix</p>
            <WeightSlider
              label="Website weight"
              value={draft.weight_website}
              onChange={(v) => update("weight_website", v)}
              disabled={!isManual}
            />
            <WeightSlider
              label="Owner uploads weight"
              value={draft.weight_uploads}
              onChange={(v) => update("weight_uploads", v)}
              disabled={!isManual}
            />
            <div className="mt-3 flex flex-wrap gap-x-6 gap-y-2">
              <Toggle
                label="Include website"
                checked={draft.include_website}
                onChange={(v) => update("include_website", v)}
              />
              <Toggle
                label="Include uploads"
                checked={draft.include_uploads}
                onChange={(v) => update("include_uploads", v)}
              />
            </div>
          </section>

          {/* Modality */}
          <section className="card p-5">
            <p className="eyebrow mb-3">Modality emphasis (website portion)</p>
            <WeightSlider
              label="Text"
              value={draft.weight_text}
              onChange={(v) => update("weight_text", v)}
              disabled={!isManual}
            />
            <WeightSlider
              label="Visual"
              value={draft.weight_visual}
              onChange={(v) => update("weight_visual", v)}
              disabled={!isManual}
              hint={
                draft.weight_visual >= 60
                  ? "Vision-describe prompts will use the visual-emphasis preamble"
                  : undefined
              }
            />
            <WeightSlider
              label="Audio (videos)"
              value={draft.weight_audio}
              onChange={(v) => update("weight_audio", v)}
              disabled={!isManual}
            />
            <div className="mt-3 flex flex-wrap gap-x-6 gap-y-2">
              <Toggle
                label="Include images"
                checked={draft.include_images}
                onChange={(v) => update("include_images", v)}
              />
              <Toggle
                label="Include videos"
                checked={draft.include_videos}
                onChange={(v) => update("include_videos", v)}
              />
            </div>
          </section>

          {/* Output emphasis */}
          <section className="card p-5">
            <p className="eyebrow mb-3">Output emphasis</p>
            <WeightSlider
              label="Voice"
              value={draft.weight_voice}
              onChange={(v) => update("weight_voice", v)}
              disabled={!isManual}
            />
            <WeightSlider
              label="Visual identity"
              value={draft.weight_visual_identity}
              onChange={(v) => update("weight_visual_identity", v)}
              disabled={!isManual}
              hint={
                draft.weight_visual_identity >= 50
                  ? "Vision-describe prompts will use the visual-emphasis preamble"
                  : undefined
              }
            />
            <WeightSlider
              label="Themes"
              value={draft.weight_themes}
              onChange={(v) => update("weight_themes", v)}
              disabled={!isManual}
            />
          </section>

          {/* Tuning */}
          <section className="card p-5">
            <p className="eyebrow mb-3">Synthesis model tuning</p>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <label className="block text-sm">
                <span className="font-medium">Model</span>
                <select
                  value={draft.model}
                  onChange={(e) => update("model", e.currentTarget.value as any)}
                  className="mt-1 w-full px-2 py-1.5 rounded border border-border bg-bg-subtle text-sm"
                >
                  <option value="gpt-4o">gpt-4o</option>
                  <option value="gpt-4o-mini">gpt-4o-mini</option>
                  <option value="gpt-4-turbo">gpt-4-turbo</option>
                </select>
              </label>
              <label className="block text-sm">
                <span className="font-medium">Temperature</span>
                <input
                  type="number"
                  step="0.05"
                  min={0}
                  max={2}
                  value={draft.temperature}
                  onChange={(e) =>
                    update("temperature", Number(e.currentTarget.value))
                  }
                  className="mt-1 w-full px-2 py-1.5 rounded border border-border bg-bg-subtle text-sm font-mono tabular-nums"
                />
              </label>
              <label className="block text-sm">
                <span className="font-medium">Max tokens</span>
                <input
                  type="number"
                  step={100}
                  min={500}
                  max={16000}
                  value={draft.max_tokens}
                  onChange={(e) =>
                    update("max_tokens", Number(e.currentTarget.value))
                  }
                  className="mt-1 w-full px-2 py-1.5 rounded border border-border bg-bg-subtle text-sm font-mono tabular-nums"
                />
              </label>
            </div>
          </section>

          <Notice tone="info">
            Your brand note lives on the{" "}
            <a
              href="#settings"
              className="text-accent underline underline-offset-2"
            >
              Your brand
            </a>{" "}
            tab. Saving here does not start a rebuild: you do that from{" "}
            <a
              href="#brand/synthesizer"
              className="text-accent underline underline-offset-2"
            >
              Brand
            </a>
            .
          </Notice>
        </div>

        {/* What this means: the only thing on screen that says what the numbers
            above actually do, so it travels with them. `lib/hints.ts` is a
            hand-maintained mirror of `settings_service.build_hints`. */}
        <aside className="lg:sticky lg:top-20 self-start">
          <div className="card relative overflow-hidden p-5">
            <span
              className="absolute top-0 left-0 right-0 h-[3px] bg-gradient-to-r from-accent via-accent to-accent/30"
              aria-hidden
            />
            <p className="eyebrow mb-3">What this means</p>
            {hints && (
              <ul className="text-sm space-y-2.5">
                <li>
                  <span className="font-mono text-2xs text-muted block">mode</span>
                  <span className="font-medium">{hints.mode}</span>
                </li>
                <li>
                  <span className="font-mono text-2xs text-muted block">
                    synthesis source limits
                  </span>
                  <span className="font-mono text-xs tabular-nums">
                    pages {hints.limits.pageSummaries}
                    {"  ·  "}
                    images {hints.limits.imageDescriptions}
                    {"  ·  "}
                    videos {hints.limits.videoSummaries}
                    {"  ·  "}
                    uploads {hints.limits.ownerDocs}
                  </span>
                </li>
                <li>
                  <span className="font-mono text-2xs text-muted block">
                    output emphasis
                  </span>
                  <span className="font-mono text-xs tabular-nums">
                    voice {Math.round(hints.emphasis.voice * 100)}%
                    {"  ·  "}
                    visual id {Math.round(hints.emphasis.visual_identity * 100)}%
                    {"  ·  "}
                    themes {Math.round(hints.emphasis.themes * 100)}%
                  </span>
                </li>
                <li>
                  <span className="font-mono text-2xs text-muted block">
                    per-phase caps
                  </span>
                  <span className="font-mono text-xs tabular-nums">
                    page processing: {hints.pageProcessingCap}
                    <br />
                    image processing: {hints.imageProcessingCap}
                  </span>
                </li>
                <li>
                  <span className="font-mono text-2xs text-muted block">
                    phases that will run
                  </span>
                  <span className="font-mono text-xs">
                    text path ·{" "}
                    {hints.skipImages ? (
                      <span className="text-muted">no images</span>
                    ) : (
                      "images"
                    )}{" "}
                    ·{" "}
                    {hints.skipVideos ? (
                      <span className="text-muted">no videos</span>
                    ) : (
                      "videos"
                    )}{" "}
                    · synth
                  </span>
                </li>
                {hints.imagePromptPreamble && (
                  <li className="border-t border-border pt-2.5">
                    <span className="inline-flex items-center gap-2">
                      <span className="relative inline-flex w-2 h-2">
                        <span className="relative inline-block w-2 h-2 rounded-full bg-accent" />
                        <span
                          className="absolute inset-0 inline-block rounded-full bg-accent animate-dotPing"
                          aria-hidden
                        />
                      </span>
                      <span className="pill pill-running">visual emphasis ON</span>
                    </span>
                    <p className="text-2xs text-muted mt-1.5 leading-relaxed">
                      Vision describe prompts will steer toward composition,
                      colour palette, lighting, and stylistic motifs.
                    </p>
                  </li>
                )}
                {hints.emphasisText && (
                  <li className="border-t border-border pt-2.5">
                    <span className="font-mono text-2xs text-muted block mb-1">
                      synthesis prompt addition
                    </span>
                    <p className="text-xs text-fg/80 leading-relaxed italic">
                      {hints.emphasisText}
                    </p>
                  </li>
                )}
              </ul>
            )}
          </div>
        </aside>
      </div>

      <SaveBar dirty={dirty} saving={saving} onSave={() => void save()} />
    </section>
  );
}

/*
 * KNOWN BEHAVIOUR, DELIBERATELY NOT CHANGED. Raise these as issues against the
 * feature's owner rather than patching them here; every one of them is load
 * bearing for somebody's mental model of how this is supposed to work.
 *
 * 1. Saving from this tab can wipe the brand note. This form PUTs the whole
 *    row, `custom_instructions` included, from whatever it loaded when the tab
 *    was opened. Edit the note on "Your brand" in another tab, then save here,
 *    and the older copy wins.
 *
 * 2. `PUT /api/settings` uses `exclude_none`, so a null is dropped rather than
 *    written. Emptying the brand note therefore cannot clear it: the box shows
 *    empty and the old sentence stays in the prompt. Applies to every nullable
 *    field on this row.
 *
 * 3. The live row is `mode='manual'` with weights already on it (website 50,
 *    uploads 85, text 50, visual 93, audio 30, voice 33, visual identity 92,
 *    themes 75), so those are in force right now, not the Auto defaults.
 *
 * 4. `lib/hints.ts` is a hand-copied mirror of the Python in
 *    `settings_service.build_hints`. Nothing checks the two agree, so the panel
 *    on the right can drift from what a run actually does.
 *
 * 5. Include toggles are writable in Auto mode while the sliders are disabled,
 *    and they do take effect in Auto (`_build_hints_auto` reads
 *    `include_images` and `include_videos`). So a phase can be switched off
 *    from a screen that says weights do not apply.
 */
