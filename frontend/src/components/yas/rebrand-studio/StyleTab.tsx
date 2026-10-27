// Rebrand studio — the Style tab: the clip-wide settings.
//
//   Colours   the palette every "Auto" colour uses (the intro's own, 42
//             violet, or any colour). Swatches only; a hex code is only ever
//             a tooltip. A new palette recolours; it never moves a pattern
//             the server placed itself (each is pinned where it was drawn).
//   Feel      how strongly the brief leans in (subtle / balanced / bold).
//   Note      a line to the brief, as the owner would say it.
//   Advanced  the white-flash guard, the stray-drawing clean-up, and what
//             Omni is told (read-only).
//
// Made in house (every new draft) nothing is painted over the footage and
// nothing is told anything: Colours only, and one line saying why.

import { useId, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import type { RebrandIntensity } from "../../../lib/rebrandApi";
import type { HarnessCatalogue, HarnessColourSource, HarnessRecipe } from "../../../lib/rebrandHarnessApi";
import {
  BRAND_COLOUR,
  isHouse,
  setPalette as withPalette,
  setStrayClean,
  strayCleanApplies,
  strayCleanOn,
  type Pins,
} from "../../../lib/rebrandRecipe";
import { ChoiceChips } from "../../ui/ChoiceChips";
import { PickColour } from "./PickColour";
import { COPY } from "./studioCopy";
import { COALESCE_NOTES } from "./studioModel";

const INTENSITIES: { value: RebrandIntensity; label: string }[] = [
  { value: "subtle", label: "Subtle" },
  { value: "balanced", label: "Balanced" },
  { value: "bold", label: "Bold" },
];

export function StyleTab({
  recipe,
  catalogue,
  introColour,
  pins,
  onTop = null,
  onEdit,
  onSeePrompt,
}: {
  recipe: HarnessRecipe;
  catalogue: HarnessCatalogue | null;
  introColour: string | null;
  /** Where the preview on screen drew each pattern (null unless it is the
   *  preview of this recipe). */
  pins: Pins;
  /** Added on top of this version (vN): its strays are its own, kept by the hold. */
  onTop?: number | null;
  onEdit: (next: HarnessRecipe, opts?: { coalesce?: string }) => void;
  onSeePrompt: (trigger: HTMLElement) => void;
}) {
  // Open when the clean-up is on (as "Clean it up" leaves it), so it shows.
  const [advanced, setAdvanced] = useState(() => strayCleanOn(recipe));
  const brand = catalogue?.brand_colour ?? BRAND_COLOUR;
  const notesMax = catalogue?.limits.notes ?? 600;
  const source = recipe.colours.source;

  const setPalette = (next: HarnessColourSource, custom?: string) =>
    onEdit(
      withPalette(
        recipe,
        // A custom colour stays remembered when switching away, as before.
        { source: next, custom: custom ?? recipe.colours.custom },
        pins,
      ),
    );

  // The clean-up: only where a run makes one (strayCleanApplies), and never
  // added on top of a version, which the hold keeps as it is. Once on, it
  // can always be turned off.
  const cleanOn = strayCleanOn(recipe);
  const cleanNote = onTop ? COPY.strayCleanOnTop(onTop) : strayCleanApplies(recipe) ? null : COPY.strayCleanNeedsIcons;
  const cleanLocked = !!cleanNote && !cleanOn;

  const house = isHouse(recipe);
  const intensities = catalogue?.intensities?.length
    ? catalogue.intensities.map((o) => ({ value: o.value as RebrandIntensity, label: o.label }))
    : INTENSITIES;

  return (
    <div className="space-y-5">
      <section className="space-y-1.5">
        <h4 className="eyebrow">{COPY.palette}</h4>
        <p className="text-2xs text-muted">{COPY.paletteHint}</p>
        <div className="flex flex-wrap items-center gap-2">
          <ChoiceChips
            label={COPY.palette}
            options={[
              {
                value: "intro",
                label: COPY.paletteIntro,
                title: introColour ?? undefined,
                icon: <span className="h-3.5 w-3.5 rounded-full border border-black/10" style={{ background: introColour ?? "#d4d4d8" }} aria-hidden />,
              },
              {
                value: "brand",
                label: COPY.paletteBrand,
                title: brand,
                icon: <span className="h-3.5 w-3.5 rounded-full border border-black/10" style={{ background: brand }} aria-hidden />,
              },
            ]}
            value={source === "custom" ? null : source}
            onChange={(v) => setPalette(v as HarnessColourSource)}
          />
          <PickColour
            value={recipe.colours.custom}
            checked={source === "custom"}
            onCommit={(hex) => setPalette("custom", hex)}
            label={COPY.paletteCustom}
            title={source === "custom" && recipe.colours.custom ? recipe.colours.custom : COPY.pickColourTitle}
          />
        </div>
      </section>

      {house ? (
        <p className="text-2xs leading-snug text-muted">{COPY.houseStyle}</p>
      ) : (
        <OmniSettings
          recipe={recipe}
          intensities={intensities}
          notesMax={notesMax}
          advanced={advanced}
          setAdvanced={setAdvanced}
          cleanOn={cleanOn}
          cleanLocked={cleanLocked}
          cleanNote={cleanNote}
          onEdit={onEdit}
          onSeePrompt={onSeePrompt}
        />
      )}
    </div>
  );
}

/** What only Omni reads: the feel, the note, and the Advanced guards. */
function OmniSettings({
  recipe,
  intensities,
  notesMax,
  advanced,
  setAdvanced,
  cleanOn,
  cleanLocked,
  cleanNote,
  onEdit,
  onSeePrompt,
}: {
  recipe: HarnessRecipe;
  intensities: { value: RebrandIntensity; label: string }[];
  notesMax: number;
  advanced: boolean;
  setAdvanced: (f: (v: boolean) => boolean) => void;
  cleanOn: boolean;
  cleanLocked: boolean;
  cleanNote: string | null;
  onEdit: (next: HarnessRecipe, opts?: { coalesce?: string }) => void;
  onSeePrompt: (trigger: HTMLElement) => void;
}) {
  const noteId = useId();
  return (
    <>
      <section className="space-y-1.5">
        <h4 className="eyebrow">{COPY.feel}</h4>
        <ChoiceChips
          label={COPY.feel}
          options={intensities}
          value={recipe.director.intensity}
          onChange={(v) => onEdit({ ...recipe, preset: null, director: { ...recipe.director, intensity: v } })}
        />
      </section>

      <section className="space-y-1.5">
        <div className="flex items-baseline gap-2">
          <label htmlFor={noteId} className="eyebrow flex-1">
            {COPY.note}
          </label>
          <span className="text-3xs text-muted tabular-nums">
            {recipe.director.notes.length}/{notesMax}
          </span>
        </div>
        <textarea
          id={noteId}
          rows={3}
          maxLength={notesMax}
          value={recipe.director.notes}
          placeholder={COPY.notePlaceholder}
          onChange={(e) =>
            onEdit(
              { ...recipe, preset: null, director: { ...recipe.director, notes: e.target.value.slice(0, notesMax) } },
              { coalesce: COALESCE_NOTES },
            )
          }
          className="w-full resize-y rounded-lg border border-border bg-bg px-2.5 py-2 text-xs text-fg placeholder:text-muted/70 focus:outline-none focus:border-accent"
        />
      </section>

      <section className="space-y-2">
        <button
          type="button"
          aria-expanded={advanced}
          onClick={() => setAdvanced((v) => !v)}
          className="inline-flex items-center gap-1 text-2xs font-medium text-muted hover:text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 rounded"
        >
          {advanced ? <ChevronDown className="h-3.5 w-3.5" aria-hidden /> : <ChevronRight className="h-3.5 w-3.5" aria-hidden />}
          {COPY.advanced}
        </button>
        {advanced && (
          <div className="space-y-3 rounded-xl border border-border p-3">
            <label className="flex cursor-pointer items-start gap-2 text-xs">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={recipe.guards.veil === "entrance"}
                onChange={(e) =>
                  onEdit({ ...recipe, preset: null, guards: { ...recipe.guards, veil: e.target.checked ? "entrance" : "off" } })
                }
              />
              <span>
                <span className="font-medium">{COPY.whiteFlash}</span>
                <span className="block text-2xs text-muted leading-snug">{COPY.whiteFlashHint}</span>
              </span>
            </label>
            <label
              className={`flex items-start gap-2 text-xs ${cleanLocked ? "cursor-not-allowed opacity-60" : "cursor-pointer"}`}
            >
              <input
                type="checkbox"
                className="mt-0.5"
                checked={cleanOn}
                disabled={cleanLocked}
                onChange={(e) => onEdit(setStrayClean(recipe, e.target.checked))}
              />
              <span>
                <span className="font-medium">{COPY.strayClean}</span>
                <span className="block text-2xs text-muted leading-snug">{cleanNote ?? COPY.strayCleanHint}</span>
              </span>
            </label>
            <button type="button" className="btn btn-xs btn-ghost" onClick={(e) => onSeePrompt(e.currentTarget)}>
              {COPY.seePrompt}
            </button>
          </div>
        )}
      </section>
    </>
  );
}

