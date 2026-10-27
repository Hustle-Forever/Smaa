// Rebrand studio — the Patterns tab: the scene's patterns, one row each, in
// draw order (the first is furthest back), and "Add patterns".
//
// Each edit is a pure recipe change (lib/rebrandRecipe) handed to the shell,
// which records it (undo) and switches the stage to Next. What the rows SAY
// comes from the draft, what they SHOW about the drawn result (where it
// went, whether it moved) from the preview on screen.
//
// Added on top of a version ("Add to it"), the draft's rows are the NEW
// layers, and the version's own are listed under them, read-only ("In v2
// (kept)") — they count towards the cap and "full goes alone", and the way
// to remove one is Change it. Carrying on from a version as its whole recipe
// (made in house, or Change it), each row still the version's own is
// tagged "from v3" until it is changed.
//
// Made in house, a pattern can be moved, sized and turned by hand — on the
// picture (the stage's handles) or with its row's Size / Turn — and its row
// says "By hand" with Back to automatic; each such edit pins the others
// first (rebrandRecipe.setHand), so nothing else moves. Its Time is when it
// shows and how it comes in and goes out (the timeline under the stage, as
// numbers): a time that would put a full-screen pattern with another is put
// back, with the reason on the row, as the timeline puts back such a drop.

import { useEffect, useRef, useState } from "react";
import { Lock, Plus } from "lucide-react";

import {
  recipeKey,
  type HarnessCatalogue,
  type HarnessElement,
  type HarnessRecipe,
} from "../../../lib/rebrandHarnessApi";
import {
  anotherSpot,
  clashOf,
  colourOf,
  DEFAULT_SWATCHES,
  enabledPatterns,
  entryOf,
  fullEverywhere,
  goesFull,
  hasManyPlaces,
  isAutoPlaced,
  paletteColour,
  isHouse,
  patternLimit,
  patternsOf,
  previewPatternOf,
  removeElement,
  setAnchor,
  setEnabled,
  setEntrance,
  setPatternColour,
  setRamps,
  setSpan,
  showBlocker,
  slugOf,
  withKept,
  type Pins,
} from "../../../lib/rebrandRecipe";
import { clampHand, handOf, isHandPlaced } from "../../../lib/rebrandHand";
import { EXIT_SEC, isWholeScene, spanOn, timing, type SecSpan } from "../../../lib/rebrandTiming";
import { backToAuto, setFollow, setHand, setLayer } from "../../../lib/rebrandRecipe";
import { Notice } from "../../ui/controls";
import { previewSrc } from "../PatternLibraryGrid";
import { PatternRow, rowDomId, type RowTime } from "./PatternRow";
import { anchorLabel, COPY, layoutLabel } from "./studioCopy";
import type { KeptRow } from "./studioModel";
import type { StudioPreviewState } from "./useStudioPreview";

/** The version the draft is added on top of, as this tab needs it. */
export interface KeptLayers {
  /** v2 → 2. */
  no: number;
  rows: KeptRow[];
  /** Its layers as recipe elements, for the rules that count them. */
  elements: HarnessElement[];
  onChangeIt: () => void;
}

/** The share of a pattern's visible ink on the others above which its row
 *  warns — the server's pattern_planner.OVERLAP_MAX, so the row and the
 *  stage's warning agree. */
const OVERLAP_WARN = 0.05;
const ADD_BUTTON_ID = "rebrand-add-patterns";

export function PatternsTab({
  recipe,
  catalogue,
  catalogueError,
  onRetryCatalogue,
  preview,
  introColour,
  openId,
  onOpen,
  onEdit,
  onLibrary,
  focusId,
  onFocused,
  kept = null,
  fromNo = null,
  duration = null,
  layers = true,
}: {
  recipe: HarnessRecipe;
  catalogue: HarnessCatalogue | null;
  catalogueError: string | null;
  onRetryCatalogue: () => void;
  preview: StudioPreviewState;
  introColour: string | null;
  openId: string | null;
  onOpen: (id: string | null) => void;
  onEdit: (next: HarnessRecipe, opts?: { coalesce?: string }) => void;
  /** Open the pattern library: to add, or to swap element `id`'s pattern. */
  onLibrary: (mode: "add" | "swap", id: string | null, trigger: HTMLElement | null) => void;
  /** A row to focus once it exists (the first of a batch just added). */
  focusId: string | null;
  onFocused: () => void;
  /** Added on top of a version: its own layers, kept as they are. */
  kept?: KeptLayers | null;
  /** Carried on from a version: the rows still its own, unchanged — the
   *  version number each is tagged with ("from v3"), by element id. */
  fromNo?: Record<string, number> | null;
  /** The scene's length (s): a pattern's Time is shown on it (made in
   *  house). null while it isn't known. */
  duration?: number | null;
  /** Behind / in front of the character is offered (not on an intro or
   *  outro with no one in it). */
  layers?: boolean;
}) {
  const patterns = patternsOf(recipe);
  // Every layer the result will show, for the cap and "full goes alone".
  const all = kept ? withKept(recipe, kept.elements) : recipe;
  const count = patternsOf(all).length;
  const limit = patternLimit(catalogue);
  const shown = preview.shown;
  const ctx = { catalogue, introColour };
  const paletteHex = paletteColour(recipe, ctx);
  const swatches = catalogue?.swatches?.length ? catalogue.swatches : DEFAULT_SWATCHES;
  const busy = preview.status === "waiting" || preview.status === "loading";
  const busyIds = new Set(busy ? preview.change?.ids ?? [] : []);
  const refocus = useRef<string | null>(null);
  // A time put back (it would show a full-screen pattern with another).
  const [refused, setRefused] = useState<{ id: string; text: string } | null>(null);
  useEffect(() => {
    if (!refused) return;
    const h = window.setTimeout(() => setRefused(null), 6000);
    return () => window.clearTimeout(h);
  }, [refused]);
  // Where the preview on screen drew each pattern, while it is the draft's.
  const pins: Pins = preview.current && shown ? { preview: shown.preview, recipe: shown.recipe, catalogue } : null;

  useEffect(() => {
    if (!focusId) return;
    const el = document.getElementById(rowDomId(focusId));
    if (el) {
      focusInRail(el);
      onFocused();
    }
  }, [focusId, patterns.length, onFocused]);

  // After a Remove: the next row, or Add when none is left.
  useEffect(() => {
    const target = refocus.current;
    if (!target) return;
    refocus.current = null;
    const el = document.getElementById(target);
    if (el) focusInRail(el);
  });

  // Full-screen patterns the whole time: nowhere left to add one.
  const fullOn = fullEverywhere(all, catalogue, duration);
  const atCap = count >= limit;
  const single = limit <= 1;
  const addTitle = fullOn ? COPY.blockedTitle["full-present"] : atCap ? COPY.blockedTitle.cap(limit) : undefined;

  const rows = patterns.map((el, i) => {
    const entry = entryOf(catalogue, slugOf(el));
    const pp = shown ? previewPatternOf(shown.preview, shown.recipe, el.id) : null;
    // The drawn place still describes this element only while its slug,
    // anchor, pin and shuffle are the ones that preview was made with.
    // A pin equal to where it was drawn (set by a colour change) is the same place.
    const was = shown?.recipe.elements.find((e) => e.id === el.id);
    const pinOf = (layout: unknown) => (layout && pp?.layout && recipeKey(layout) === recipeKey(pp.layout) ? null : layout ?? null);
    const placed =
      !!pp &&
      !!was &&
      was.enabled === el.enabled &&
      recipeKey([was.props.slug, was.placement.anchor, pinOf(was.props.layout), was.props.shuffle ?? null]) ===
        recipeKey([el.props.slug, el.placement.anchor, pinOf(el.props.layout), el.props.shuffle ?? null]);
    const rowBusy = busyIds.has(el.id);
    const placeText = placed
      ? layoutLabel(pp?.layout) ?? anchorLabel(String(el.placement.anchor))
      : rowBusy
        ? COPY.placing
        : anchorLabel(String(el.placement.anchor));
    let note: string | null = null;
    if (refused?.id === el.id) note = refused.text;
    else if (placed && preview.current && pp) {
      if (pp.error) note = pp.error;
      else if (pp.warning) note = pp.warning;
      else if (pp.moved) note = typeof pp.moved === "string" ? pp.moved : COPY.moved;
      else if ((pp.overlap ?? 0) > OVERLAP_WARN) note = COPY.overlaps;
    }
    const pin = preview.current && pp && isAutoPlaced(pp) ? pp.layout : null;
    const others = enabledPatterns(all).filter((e) => e.id !== el.id).length;
    // Showing it again follows the same rules as adding it.
    const blocked = showBlocker(all, catalogue, el.id);
    // By hand (made in house): the move as the recipe has it (the drawn one
    // once the preview is this draft's: a full-screen size is raised), and
    // where the preview drew it — a first move pins it there.
    const house = isHouse(recipe);
    const own = handOf(el);
    const drawnHand = preview.current && pp?.hand ? pp.hand : null;
    const drawn = pp?.layout && pp.base_box ? { layout: pp.layout, box: pp.base_box } : null;
    // Its time (made in house): the recipe's span on this scene, drawn with
    // the entrance the preview resolved (its own, aimed) — the server's
    // timing() rule for rule (lib/rebrandTiming).
    const dur = duration && duration > 0 ? duration : null;
    const entrance =
      pp?.animation ?? (el.animation.entrance !== "auto" ? el.animation.entrance : entry?.default_animation ?? "fade");
    const span = dur ? spanOn(el, dur) : null;
    const time: RowTime | null =
      house && dur
        ? {
            duration: dur,
            span,
            whole: !!span && isWholeScene(span, dur),
            timing: timing(el, { duration: dur, entrance }),
            entranceSec: el.animation.duration_sec ?? 1,
            exit: el.animation.exit ?? "auto",
            exitSec: el.animation.exit_sec ?? EXIT_SEC,
            exits: catalogue?.exits ?? [],
          }
        : null;
    const retime = (next: SecSpan) => {
      if (!dur) return;
      const other = clashOf(all, catalogue, el.id, [next.from, isWholeScene(next, dur) ? null : next.to]);
      if (other) {
        const name = entry?.name ?? slugOf(el);
        setRefused({
          id: el.id,
          text: goesFull(el, catalogue) ? COPY.time.refused(name) : COPY.time.refusedOther(other.name),
        });
        return;
      }
      setRefused(null);
      onEdit(setSpan(recipe, el.id, next, dur, pins), { coalesce: `span:${el.id}` });
    };
    const hand = house
      ? {
          placed: isHandPlaced(el),
          scale: drawnHand?.scale ?? own.scale,
          rotate: own.rotate,
          full: !!pp?.full,
          canEdit: isHandPlaced(el) || (!!drawn && preview.current),
          follow: (el.props.follow === "room" || el.props.follow === "screen" ? el.props.follow : "auto") as
            | "auto"
            | "room"
            | "screen",
        }
      : null;

    return (
      <PatternRow
        key={el.id}
        el={el}
        entry={entry}
        catalogue={catalogue}
        swatches={swatches}
        open={openId === el.id}
        onToggle={() => onOpen(openId === el.id ? null : el.id)}
        busy={rowBusy}
        colour={colourOf(el, recipe, ctx)}
        paletteHex={paletteHex}
        placeText={placeText}
        note={note}
        canAnotherSpot={(String(el.placement.anchor) === "auto" && hasManyPlaces(entry)) || isAutoPlaced(pp)}
        fullBlocked={!!clashOf(all, catalogue, el.id, undefined, true)}
        showBlocked={blocked ? COPY.showBlockedTitle[blocked] : null}
        canSwap={!!catalogue}
        onEnabled={(v) => onEdit(setEnabled(recipe, el.id, v, pins))}
        onAnchor={(a) => onEdit(setAnchor(recipe, el.id, a))}
        onColour={(c) => onEdit(setPatternColour(recipe, el.id, c, pin))}
        onEntrance={(a) => onEdit(setEntrance(recipe, el.id, a))}
        onAnotherSpot={() => onEdit(anotherSpot(recipe, el.id))}
        onSwap={() => onLibrary("swap", el.id, document.getElementById(rowDomId(el.id)))}
        fromNo={fromNo?.[el.id] ?? null}
        hand={hand}
        onHand={(patch) =>
          onEdit(setHand(recipe, el.id, clampHand({ ...own, ...patch }), drawn, pins), { coalesce: `hand:${el.id}` })
        }
        onBackToAuto={() => onEdit(backToAuto(recipe, el.id))}
        onFollow={(f) => onEdit(setFollow(recipe, el.id, f))}
        onLayer={house && layers ? (z) => onEdit(setLayer(recipe, el.id, z)) : undefined}
        time={time}
        onSpan={retime}
        onWhole={() => dur && retime({ from: 0, to: dur })}
        onRamps={(r) => onEdit(setRamps(recipe, el.id, r), { coalesce: `ramps:${el.id}` })}
        onRemove={() => {
          const after = patterns[i + 1] ?? patterns[i - 1];
          refocus.current = after ? rowDomId(after.id) : ADD_BUTTON_ID;
          if (openId === el.id) onOpen(null);
          onEdit(removeElement(recipe, el.id, pins));
        }}
      />
    );
  });

  // With one pattern at most (an older server), Add becomes "Change pattern".
  const swapOnly = single && patterns.length > 0;
  const addBlocked = !swapOnly && (fullOn || atCap || !catalogue);

  return (
    <div className="space-y-3">
      {catalogueError && (
        <Notice
          tone="warning"
          action={
            <button type="button" className="btn btn-xs btn-ghost" onClick={onRetryCatalogue}>
              {COPY.retry}
            </button>
          }
        >
          {COPY.catalogueFailed}
        </Notice>
      )}

      {kept && <h4 className="eyebrow">{COPY.newLayers}</h4>}
      {patterns.length === 0 ? (
        <p className="rounded-xl border border-dashed border-border px-3 py-4 text-center text-xs text-muted">
          {kept ? COPY.nothingNew(kept.no) : isHouse(recipe) ? COPY.noPatternsHouse : COPY.noPatterns}
        </p>
      ) : (
        <ul className="space-y-2" aria-label={kept ? COPY.newLayers : COPY.tabs.patterns}>
          {rows}
        </ul>
      )}

      <div className="flex items-center gap-2">
        <button
          id={ADD_BUTTON_ID}
          type="button"
          disabled={addBlocked}
          title={addTitle}
          onClick={(e) =>
            swapOnly ? onLibrary("swap", patterns[0].id, e.currentTarget) : onLibrary("add", null, e.currentTarget)
          }
          className="btn btn-sm btn-ghost gap-1.5"
        >
          {!swapOnly && <Plus className="h-3.5 w-3.5" aria-hidden />}
          {swapOnly ? COPY.changePattern : COPY.addPatterns}
        </button>
        {!single && count > 0 && (
          <span className="ml-auto text-2xs text-muted tabular-nums" title={atCap ? COPY.blockedTitle.cap(limit) : undefined}>
            {count} of {limit}
          </span>
        )}
      </div>

      {kept && kept.rows.length > 0 && (
        <section className="space-y-2 border-t border-border pt-3" aria-label={COPY.keptIn(kept.no)}>
          <h4 className="eyebrow">{COPY.keptIn(kept.no)}</h4>
          <ul className="space-y-1.5">
            {kept.rows.map((row) => (
              <KeptLayerRow key={row.key} row={row} />
            ))}
          </ul>
          <p className="text-2xs text-muted">
            {COPY.toRemoveIt}{" "}
            <button
              type="button"
              onClick={kept.onChangeIt}
              className="font-medium text-accent-hover underline-offset-2 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 rounded"
            >
              {COPY.changeIt}
            </button>
          </p>
        </section>
      )}
    </div>
  );
}

/** A layer the version already has: kept exactly as it is, so nothing here
 *  can be changed — a lock where the row's controls would be. */
/** Focus a row and bring it into view in the rail only: the studio sits on
 *  the page, and a plain focus() would scroll the page too. */
function focusInRail(el: HTMLElement) {
  el.focus({ preventScroll: true });
  let box: HTMLElement | null = el.parentElement;
  while (box && !/(auto|scroll)/.test(getComputedStyle(box).overflowY)) box = box.parentElement;
  if (!box) return;
  const b = box.getBoundingClientRect();
  const r = el.getBoundingClientRect();
  if (r.top < b.top) box.scrollTop -= b.top - r.top;
  else if (r.bottom > b.bottom) box.scrollTop += r.bottom - b.bottom;
}

function KeptLayerRow({ row }: { row: KeptRow }) {
  return (
    <li
      className="flex min-h-[44px] items-center gap-2.5 rounded-xl border border-border bg-bg-muted/50 py-1.5 pl-2.5 pr-3"
      title={COPY.keptTitle}
    >
      <span className="h-8 w-11 shrink-0 overflow-hidden rounded-md bg-black opacity-80">
        {row.entry && <img src={previewSrc(row.entry)} alt="" draggable={false} className="h-full w-full object-contain" />}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-xs font-medium text-fg">{row.name}</span>
        <span className="block truncate text-2xs text-muted">
          {row.where}
          {row.fromNo ? ` · ${COPY.keptFrom(row.fromNo)}` : ""}
        </span>
      </span>
      <span
        className="h-3.5 w-3.5 shrink-0 rounded-full border border-black/10"
        style={{ background: row.colour ?? "#d4d4d8" }}
        title={row.colour ?? undefined}
        aria-hidden
      />
      <Lock className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />
      <span className="sr-only">{COPY.keptTitle}</span>
    </li>
  );
}
