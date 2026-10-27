// Rebrand studio — one icon's own look, under its row in the Icons tab.
//
//   Style          Circle · Circle + words (the older looks while in use)
//   Colour         as the others · 42 violet · 42 purple · charcoal · white ·
//                  the scene's colours (each pattern's, the intro's) · Pick
//   Icon colour    auto · white · charcoal · violet
//   Words          (Circle + words) its words, or "Use their word"
//   Layer          behind the character or in front of the character
//   Enter · Leave  (Circle + words may also Grow: its circle pops, then it
//                  opens out to its words)
//   Position       automatic (drag it on the picture to place it), or placed
//                  by you — Back to automatic; over the speaker: Bring to
//                  front, Move to a clear spot
//   Reset this icon
//
// Every choice is a plain edit of props.looks[its glyph id] (rebrandIcons
// setLook): a choice set back to the scene's deletes its key, so the recipe
// stays as small as it can. An icon placed by hand is sized in its box: the
// Size slider sizes the box about its centre (setBox), as its handles do. The plan's notes about this icon (too wide, so a
// circle; kept behind, off the face; hard to see) are listed at its foot.

import { useEffect, useState } from "react";

import type { HarnessIconItem, HarnessIconMotion, HarnessIconRules, HarnessIconStyle, HarnessRecipe } from "../../../lib/rebrandHarnessApi";
import {
  CHIP_LABEL_MAX,
  entranceOf,
  glyphId,
  ICON_ENTRANCES,
  iconProps,
  iconsOf,
  lookOf,
  NEW_STYLES,
  OLD_STYLES,
  resetIcon,
  setBox,
  setLook,
  setTweak,
  usesOldStyle,
} from "../../../lib/rebrandIcons";
import { ChoiceChips, type ChoiceOption } from "../../ui/ChoiceChips";
import { PickColour, SwatchDot } from "./PickColour";
import { COPY } from "./studioCopy";

const VIOLET = "#7f2bf2";
const PURPLE = "#8d01fa";
const CHARCOAL = "#303030";
const WHITE = "#ffffff";

export interface SceneColour {
  label: string;
  colour: string;
}

function styleLabel(s: string): string {
  return s === "circle" ? COPY.icons.styleCircle : s === "chip" ? COPY.icons.styleChip : s === "badge" ? COPY.icons.styleBadge : COPY.icons.styleGlyph;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <span className="block text-2xs font-medium text-fg">{label}</span>
      {children}
    </div>
  );
}

export function IconLookPanel({
  recipe,
  item,
  sceneFill,
  sceneColours,
  rules,
  frame = null,
  onEdit,
  layer = true,
}: {
  recipe: HarnessRecipe;
  item: HarnessIconItem;
  /** The colour the icons that take the scene's have (the plan's). */
  sceneFill: string;
  /** Each pattern's colour and the intro's. */
  sceneColours: SceneColour[];
  rules: HarnessIconRules;
  /** The frame (px): an icon placed by hand is sized in it. */
  frame?: { w: number; h: number } | null;
  onEdit: (next: HarnessRecipe) => void;
  /** Behind / in front of the character is offered (not on an intro or
   *  outro with no one in it). */
  layer?: boolean;
}) {
  const id = glyphId(item.glyph);
  const el = iconsOf(recipe);
  const p = iconProps(el);
  const own = lookOf(recipe, id);
  const tweak = p.tweaks[id];
  const edit = (patch: Parameters<typeof setLook>[2]) => onEdit(setLook(recipe, id, patch));

  // Style: the new looks, and the older ones while the draft has them.
  const style: HarnessIconStyle = own.style ?? p.style;
  const styles: ChoiceOption<HarnessIconStyle>[] = [
    ...NEW_STYLES.map((s) => ({ value: s as HarnessIconStyle, label: styleLabel(s) })),
    ...(usesOldStyle(recipe)
      ? OLD_STYLES.map((s) => ({ value: s as HarnessIconStyle, label: styleLabel(s), title: COPY.icons.styleOldTitle }))
      : []),
  ];

  // Colour: as the other icons (the scene's), the brand's, the scene's own.
  const colours: { value: string; label: string; colour: string; mark?: string }[] = [
    { value: VIOLET, label: COPY.icons.colourChip.violet, colour: VIOLET },
    { value: PURPLE, label: COPY.icons.colourPurple, colour: PURPLE },
    { value: CHARCOAL, label: COPY.icons.colourChip.charcoal, colour: CHARCOAL },
    { value: WHITE, label: COPY.icons.colourChip.white, colour: WHITE },
  ];
  for (const c of sceneColours) {
    const hex = c.colour.toLowerCase();
    if (!colours.some((x) => x.value === hex)) colours.push({ value: hex, label: c.label, colour: hex, mark: c.label.slice(0, 1) });
  }
  const colourOptions: ChoiceOption<string>[] = [
    {
      value: "scene",
      label: COPY.icons.colourScene,
      ariaLabel: COPY.icons.colourScene,
      title: COPY.icons.colourSceneTitle(sceneFill),
      icon: <SwatchDot colour={sceneFill} mark="=" />,
    },
    ...colours.map((c) => ({ value: c.value, label: c.label, ariaLabel: c.label, title: `${c.label} (${c.value})`, icon: <SwatchDot colour={c.colour} mark={c.mark} /> })),
  ];
  const colourValue = own.colour ? (colours.some((c) => c.value === own.colour) ? own.colour : null) : "scene";

  const inks: ChoiceOption<string>[] = [
    { value: "auto", label: COPY.icons.inkAuto, title: COPY.icons.inkAutoTitle },
    { value: WHITE, label: COPY.icons.colourChip.white, ariaLabel: COPY.icons.colourChip.white, icon: <SwatchDot colour={WHITE} /> },
    { value: CHARCOAL, label: COPY.icons.colourChip.charcoal, ariaLabel: COPY.icons.colourChip.charcoal, icon: <SwatchDot colour={CHARCOAL} /> },
    { value: VIOLET, label: COPY.icons.colourChip.violet, ariaLabel: COPY.icons.colourChip.violet, icon: <SwatchDot colour={VIOLET} /> },
  ];

  // Words: typed here, kept on Enter or leaving the box (one undo step).
  const [words, setWords] = useState(own.label ?? "");
  useEffect(() => setWords(own.label ?? ""), [own.label, id]);
  const commitWords = (v: string) => {
    const next = v.trim().slice(0, CHIP_LABEL_MAX);
    if (next !== (own.label ?? "")) edit({ label: next || null });
  };
  const spoken = item.word ? item.word.replace(/^[\s"“'‘(]+|[\s"”'’),.!?:;]+$/g, "") : "";
  const spokenLabel = spoken ? spoken[0].toUpperCase() + spoken.slice(1) : "";

  const sceneEnter = entranceOf(el);
  // Grow is a chip's own (any other style grows as a pop): offered for
  // Circle + words, and kept on show while an icon has it.
  const grows = style === "chip" || own.enter === "grow" || own.leave === "grow";
  const enterOptions: ChoiceOption<string>[] = [
    ...ICON_ENTRANCES.map((e) => ({ value: e as string, label: COPY.icons.enterChip[e] })),
    ...(grows ? [{ value: "grow", label: COPY.icons.enterChip.grow, title: COPY.icons.growTitle }] : []),
  ];
  const leaveOptions: ChoiceOption<string>[] = [{ value: "mirror", label: COPY.icons.leaveMirror }, ...enterOptions];
  const hasOwn = Object.keys(own).length > 0 || !!tweak;

  return (
    <section
      aria-label={COPY.icons.panelLabel(item.label)}
      className="mx-2 mb-2 space-y-3 rounded-lg border border-border bg-bg-muted/40 px-3 py-2.5"
      data-icon-panel={id}
    >
      <Row label={COPY.icons.style}>
        <ChoiceChips
          label={COPY.icons.style}
          options={styles}
          value={style}
          onChange={(s) => edit({ style: s === p.style ? null : s })}
        />
      </Row>

      {layer && (
        <Row label={COPY.icons.layer}>
          <ChoiceChips
            label={COPY.icons.layer}
            options={[
              { value: "behind", label: COPY.icons.behind },
              { value: "front", label: COPY.icons.front, title: COPY.icons.frontHint },
            ]}
            value={own.layer ?? "behind"}
            onChange={(v) => edit({ layer: v === "behind" ? null : "front" })}
          />
        </Row>
      )}

      <Row label={COPY.icons.ownColour}>
        <div className="flex flex-wrap items-center gap-2">
          <ChoiceChips
            variant="swatch"
            label={COPY.icons.ownColour}
            options={colourOptions}
            value={colourValue}
            onChange={(v) => edit({ colour: v === "scene" ? null : v })}
          />
          <PickColour
            value={own.colour ?? sceneFill}
            checked={!!own.colour && colourValue === null}
            onCommit={(hex) => edit({ colour: hex })}
            label={COPY.pickColour}
            title={COPY.pickColourTitle}
          />
        </div>
      </Row>

      <Row label={COPY.icons.glyphColour}>
        <div className="flex flex-wrap items-center gap-2">
          <ChoiceChips
            label={COPY.icons.glyphColour}
            options={inks}
            value={own.ink && !inks.some((o) => o.value === own.ink) ? null : own.ink ?? "auto"}
            onChange={(v) => edit({ ink: v === "auto" ? null : v })}
          />
          <PickColour
            value={own.ink ?? WHITE}
            checked={!!own.ink && !inks.some((o) => o.value === own.ink)}
            onCommit={(hex) => edit({ ink: hex })}
            label={COPY.pickColour}
            title={COPY.pickColourTitle}
          />
        </div>
      </Row>

      {style === "chip" && (
        <Row label={COPY.icons.words}>
          <div className="flex flex-wrap items-center gap-1.5">
            <input
              type="text"
              value={words}
              maxLength={CHIP_LABEL_MAX}
              placeholder={COPY.icons.wordsPlaceholder(item.text ?? item.label)}
              aria-label={COPY.icons.words}
              onChange={(e) => setWords(e.target.value)}
              onBlur={(e) => commitWords(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  commitWords((e.target as HTMLInputElement).value);
                }
              }}
              className="min-w-0 flex-1 rounded border border-border bg-bg px-2 py-1 text-xs text-fg focus:border-accent focus:outline-none"
            />
            {spokenLabel && spokenLabel !== (own.label ?? item.text) && (
              <button type="button" className="btn btn-xs btn-ghost" onClick={() => edit({ label: spokenLabel.slice(0, CHIP_LABEL_MAX) })}>
                {COPY.icons.useTheirWord(spoken)}
              </button>
            )}
          </div>
        </Row>
      )}

      <div className="flex flex-wrap gap-x-5 gap-y-3">
        <Row label={COPY.icons.enter}>
          <ChoiceChips
            label={COPY.icons.enter}
            options={enterOptions}
            value={own.enter ?? sceneEnter}
            onChange={(v) => edit({ enter: v === sceneEnter ? null : (v as HarnessIconMotion) })}
          />
        </Row>
        <Row label={COPY.icons.leave}>
          <ChoiceChips
            label={COPY.icons.leave}
            options={leaveOptions}
            value={own.leave ?? "mirror"}
            onChange={(v) => edit({ leave: v === "mirror" ? null : (v as HarnessIconMotion) })}
          />
        </Row>
      </div>

      {tweak?.box && (
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="btn btn-xs btn-ghost" onClick={() => onEdit(setTweak(recipe, id, { box: null }))}>
            {COPY.icons.positionReset}
          </button>
          {item.covered && item.layer !== "front" && item.clear && (
            <button
              type="button"
              className="btn btn-xs btn-ghost"
              title={COPY.icons.moveClearTitle}
              onClick={() => onEdit(setBox(recipe, id, item.clear!))}
            >
              {COPY.icons.moveClear}
            </button>
          )}
        </div>
      )}

      <button
        type="button"
        className="btn btn-xs btn-ghost"
        disabled={!hasOwn}
        title={COPY.icons.resetIconTitle}
        onClick={() => onEdit(resetIcon(recipe, id))}
      >
        {COPY.icons.resetIcon}
      </button>
    </section>
  );
}
