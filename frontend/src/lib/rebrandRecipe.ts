// Rebrand studio — the recipe edits, as pure functions.
//
// Every edit the studio makes to a draft recipe goes through here, so the
// rules live in one place and can be read without the UI around them:
//
//   patterns     up to catalogue.limits.patterns (3), one element each
//                ("pattern", "pattern-2", …). List order is draw order, and the
//                server plans each auto place avoiding the ones before it, so
//                adding a pattern never moves the earlier ones. A full-screen
//                pattern goes alone.
//   pins         props.layout pins a pattern's drawn place. It is set when an
//                auto-placed pattern changes colour — its own, or the palette
//                behind every Auto colour (the planner weighs the colour, so it
//                could move) — and on every other pattern when one is removed
//                or hidden (pinMovable); it is cleared by a new pattern, a new
//                anchor and "Another spot" (which sets that pattern's own
//                props.shuffle instead of rewriting the recipe's seed, which
//                moved them all).
//   defaults     controls set back write the literal default ("auto", enabled
//                true), so a round trip returns the very same recipe — and an
//                untouched studio sends exactly the server's 42 default.
//   by hand      a pattern moved, sized or turned on the picture (setHand) is
//                a move made FROM its pinned place (lib/rebrandHand: the pin
//                and the box it was grabbed at are written with the first
//                move); every other movable pattern is pinned first, so
//                nothing else moves. "Back to automatic" (backToAuto) takes the
//                move, the box and the pin away; a new anchor, pattern or spot
//                takes the move away (it was made from the old place).
//   time         each pattern shows over a span (setSpan: the timeline's bar;
//                every movable pattern is pinned first — which patterns show
//                together decides where the planner puts them) and comes in
//                and goes out over its own lengths (setRamps). A full-screen
//                pattern goes alone WHILE it shows (clashOf, addBlocker,
//                showBlocker); Blueprint, a transition, is added as 1.5 s at
//                the scene's start and the others start once it is over; a
//                pattern added beside a full-screen one shows where it doesn't.
//
// describeChange() says what an edit did, so the stage can say what it is
// doing while the new preview is made ("Painting the new colour…").

import type { RebrandPattern } from "./rebrandApi";
import {
  recipeKey,
  type HarnessCatalogue,
  type HarnessElement,
  type HarnessPatternLayout,
  type HarnessPreview,
  type HarnessPreviewPattern,
  type HarnessRecipe,
  type HarnessSwatch,
} from "./rebrandHarnessApi";
import { clearHand, withHand, withoutHand, type Hand, type HandDrawn } from "./rebrandHand";
import { iconsOf, iconsOn } from "./rebrandIcons";
import {
  asTransition,
  freeSpan,
  fullClash,
  patternSpan,
  spanOn,
  startingAfter,
  withRamps,
  withSpan,
  type Ramps,
  type SecSpan,
  type TimedPattern,
} from "./rebrandTiming";

/** 42 violet — what the server falls back to when the catalogue predates
 *  `brand_colour`. */
export const BRAND_COLOUR = "#7f2bf2";

/** The per-pattern colour choices of a server that predates `swatches`. */
export const DEFAULT_SWATCHES: HarnessSwatch[] = [
  { value: "auto", label: "Auto" },
  { value: "#7f2bf2", label: "42 violet" },
  { value: "#8d01fa", label: "42 purple" },
  { value: "#303030", label: "Charcoal" },
  { value: "#f4f4f8", label: "White" },
];

const HEX = /^#[0-9a-fA-F]{6}$/;

export function isHex(v: unknown): v is string {
  return typeof v === "string" && HEX.test(v);
}

// --- reading -----------------------------------------------------------------

/** Every pattern element, hidden ones too, in draw order. */
export function patternsOf(r: HarnessRecipe): HarnessElement[] {
  return r.elements.filter((e) => e.kind === "pattern");
}

export function enabledPatterns(r: HarnessRecipe): HarnessElement[] {
  return patternsOf(r).filter((e) => e.enabled);
}

export function slugOf(el: HarnessElement): string {
  return String(el.props.slug ?? "");
}

/** How many patterns a scene may hold (hidden ones count too). */
export function patternLimit(c: HarnessCatalogue | null): number {
  return c?.limits?.patterns ?? c?.kinds.find((k) => k.kind === "pattern")?.max ?? 1;
}

export function entryOf(c: HarnessCatalogue | null, slug: string): RebrandPattern | undefined {
  return c?.patterns.find((p) => p.slug === slug);
}

/** Blueprint, grid and full-only uploads: they take the whole frame. */
export function isFullOnly(p: RebrandPattern | null | undefined): boolean {
  return p?.placement === "full";
}

/** A pattern the server places per clip among several places (1.png). */
export function hasManyPlaces(p: RebrandPattern | null | undefined): boolean {
  return p?.placement === "auto" && (p.placements?.length ?? 0) > 1;
}

/** This element fills the whole frame, so nothing else may show with it. */
export function goesFull(el: HarnessElement, c: HarnessCatalogue | null): boolean {
  return el.placement.anchor === "full" || (el.placement.anchor === "auto" && isFullOnly(entryOf(c, slugOf(el))));
}

/** A full-screen pattern that is a transition: added, it shows this long at
 *  the scene's start (the catalogue's `transition`), not the whole scene. */
export function transitionOf(c: HarnessCatalogue | null, slug: string) {
  const p = entryOf(c, slug);
  return isFullOnly(p) ? p?.transition ?? null : null;
}

/** When the scene's length is not known yet: a span with no end reaches
 *  this far. */
const FAR = 1e6;

/** Every switched-on pattern as "a full-screen pattern goes alone while it
 *  shows" sees it. */
export function timedPatterns(r: HarnessRecipe, c: HarnessCatalogue | null): TimedPattern[] {
  return enabledPatterns(r).map((e) => ({
    id: e.id,
    name: entryOf(c, slugOf(e))?.name ?? (slugOf(e) || "A pattern"),
    full: goesFull(e, c),
    span: patternSpan(e),
  }));
}

/** The pattern element `id` would show together with over `span` (its own
 *  by default) when one of the two fills the frame — the server refuses
 *  that (pattern_el.check) — or null. `full`: as if it filled the frame
 *  (a new anchor). */
export function clashOf(
  r: HarnessRecipe,
  c: HarnessCatalogue | null,
  id: string,
  span?: readonly [number, number | null],
  full?: boolean,
): TimedPattern | null {
  const el = r.elements.find((e) => e.id === id);
  if (!el) return null;
  const others = timedPatterns(r, c).filter((o) => o.id !== id);
  return fullClash(others, { full: full ?? goesFull(el, c) }, span ?? patternSpan(el));
}

/** "pattern", then "pattern-2", "pattern-3" (the next free one). */
export function newPatternId(r: HarnessRecipe): string {
  const ids = new Set(r.elements.map((e) => e.id));
  if (!ids.has("pattern")) return "pattern";
  for (let n = 2; ; n++) if (!ids.has(`pattern-${n}`)) return `pattern-${n}`;
}

/** The preview's word on one pattern element. A server with one pattern at
 *  most sends only `pattern` + `placement`, which are the first ENABLED
 *  pattern of the recipe the preview was made from. */
export function previewPatternOf(
  preview: HarnessPreview | null | undefined,
  recipe: HarnessRecipe | null | undefined,
  id: string,
): HarnessPreviewPattern | null {
  if (!preview) return null;
  if (preview.patterns) return preview.patterns.find((p) => p.element_id === id) ?? null;
  const first = recipe ? enabledPatterns(recipe)[0] : undefined;
  if (!preview.pattern || !first || first.id !== id) return null;
  const p = preview.pattern;
  return {
    element_id: id,
    slug: p.slug,
    name: p.name,
    colour: p.colour,
    where: p.where,
    animation: p.animation,
    layout: p.layout,
    fell_back: p.fell_back,
    placement: preview.placement,
  };
}

/** The server chose this place among several (so a colour change or a
 *  removed neighbour could move it, unless it is pinned). */
export function isAutoPlaced(pp: HarnessPreviewPattern | null | undefined): boolean {
  return pp?.placement?.mode === "auto";
}

/** It could be drawn somewhere else once the patterns around it change: the
 *  server chose its place among several, moved it off its own (taken by
 *  another), or its pattern has several places (another may have narrowed
 *  them to one, so no choice was reported). Pinning it keeps it put. */
export function mayMove(pp: HarnessPreviewPattern | null | undefined, entry: RebrandPattern | null | undefined): boolean {
  return isAutoPlaced(pp) || !!pp?.moved || hasManyPlaces(entry);
}

// --- colour ------------------------------------------------------------------

/** The intro's own colour, when a preview tells it: the server's `colours`,
 *  or an Auto pattern drawn while the palette was "From the intro". */
export function introColourFrom(preview: HarnessPreview | null, recipe: HarnessRecipe | null): string | null {
  if (!preview) return null;
  if (preview.colours?.intro) return preview.colours.intro;
  if (!recipe || recipe.colours.source !== "intro") return null;
  for (const el of enabledPatterns(recipe)) {
    if (isHex(el.props.colour)) continue;
    const pp = previewPatternOf(preview, recipe, el.id);
    if (pp?.colour) return pp.colour;
  }
  return null;
}

/** What "Auto" paints with: the recipe's palette (colours.source). */
export function paletteColour(
  r: HarnessRecipe,
  ctx: { catalogue: HarnessCatalogue | null; introColour: string | null },
): string | null {
  if (r.colours.source === "brand") return ctx.catalogue?.brand_colour ?? BRAND_COLOUR;
  if (r.colours.source === "custom" && isHex(r.colours.custom)) return r.colours.custom;
  return ctx.introColour;
}

/** The colour a pattern is drawn in, from the DRAFT (never an older
 *  preview): mirrors the server's colour_override — its own hex, else the
 *  palette. null while the intro colour is not known yet. */
export function colourOf(
  el: HarnessElement,
  r: HarnessRecipe,
  ctx: { catalogue: HarnessCatalogue | null; introColour: string | null },
): string | null {
  if (isHex(el.props.colour)) return el.props.colour.toLowerCase();
  return paletteColour(r, ctx);
}

// --- editing -----------------------------------------------------------------

/** Any change makes it the user's own recipe, not the preset. */
function touch(r: HarnessRecipe, elements: HarnessElement[]): HarnessRecipe {
  return { ...r, preset: null, elements };
}

function withProps(el: HarnessElement, patch: Record<string, unknown>, drop: string[] = []): HarnessElement {
  const props: Record<string, unknown> = { ...el.props, ...patch };
  for (const k of drop) delete props[k];
  return { ...el, props };
}

export function updateElement(
  r: HarnessRecipe,
  id: string,
  fn: (el: HarnessElement) => HarnessElement,
): HarnessRecipe {
  let hit = false;
  const elements = r.elements.map((e) => {
    if (e.id !== id) return e;
    hit = true;
    return fn(e);
  });
  return hit ? touch(r, elements) : r;
}

/** Why a pattern can't be added right now (null: it can).
 *    cap           the scene holds the most patterns it may
 *    full-alone    this one fills the frame, and others show with it
 *    full-present  a pattern that fills the frame is on (or picked) the
 *                  whole time: there is nowhere left for this one to show */
export type AddBlocker = "cap" | "full-alone" | "full-present";

/** Where the full-screen patterns of `r` (and the picked ones, as they would
 *  be added: a transition at the scene's start, any other the whole scene)
 *  show on a scene `d` s long. */
function fullSpans(r: HarnessRecipe, c: HarnessCatalogue | null, picked: string[], d: number): SecSpan[] {
  const own = enabledPatterns(r)
    .filter((e) => goesFull(e, c))
    .map((e) => spanOn(e, d))
    .filter((s): s is SecSpan => !!s);
  const more = picked
    .filter((s) => isFullOnly(entryOf(c, s)))
    .map((s) => ({ from: 0, to: Math.min(d, transitionOf(c, s)?.sec ?? d) }));
  return [...own, ...more];
}

/** Why pattern `slug` can't be added now (null: it can), with `picked`
 *  being added along with it — "a full-screen pattern goes alone WHILE it
 *  shows": a transition (Blueprint) can come in when every pattern already
 *  showing at the scene's start can start once it is over; any other
 *  full-screen pattern only alone; a pattern beside full-screen ones only
 *  when they leave it half a second or more. `duration`: the scene's
 *  length (null: not known yet). */
export function addBlocker(
  r: HarnessRecipe,
  c: HarnessCatalogue | null,
  slug: string,
  picked: string[] = [],
  duration: number | null = null,
): AddBlocker | null {
  const d = duration ?? FAR;
  const count = patternsOf(r).length + picked.length;
  const full = isFullOnly(entryOf(c, slug));
  if (full) {
    const t = transitionOf(c, slug);
    if (!t) {
      if (enabledPatterns(r).length + picked.length > 0) return "full-alone";
    } else {
      // A transition: the others start once it is over — unless one can't
      // (a layer of the version added to, another full-screen one, or one
      // that would then show less than half a second).
      if (picked.some((s) => isFullOnly(entryOf(c, s)))) return "full-alone";
      const early = enabledPatterns(r).filter((e) => (spanOn(e, d)?.from ?? d) < t.sec - 1e-9);
      if (early.some((e) => e.id.startsWith(KEPT_PREFIX) || goesFull(e, c))) return "full-alone";
      if (startingAfter(early, t.sec, d) === null) return "full-alone";
    }
  } else {
    const spans = fullSpans(r, c, picked, d);
    if (spans.length && !freeSpan(spans, d)) return "full-present";
  }
  return count >= patternLimit(c) ? "cap" : null;
}

/** Full-screen patterns show the whole time: nowhere is left for another. */
export function fullEverywhere(r: HarnessRecipe, c: HarnessCatalogue | null, duration: number | null = null): boolean {
  const d = duration ?? FAR;
  const spans = fullSpans(r, c, [], d);
  return spans.length > 0 && !freeSpan(spans, d);
}

/** Why element `el` (carried over from a version, with its own time) can't
 *  join `r` (null: it can): the cap, or a full-screen pattern it would show
 *  together with. */
export function joinBlocker(r: HarnessRecipe, c: HarnessCatalogue | null, el: HarnessElement): AddBlocker | null {
  const full = goesFull(el, c);
  if (fullClash(timedPatterns(r, c), { full }, patternSpan(el))) return full ? "full-alone" : "full-present";
  return patternsOf(r).length >= patternLimit(c) ? "cap" : null;
}

/** The recipe with the layers of the version it is added on top of ("kept")
 *  in front of its own — first, as they are drawn — for the rules that count
 *  every layer the result will show: the cap, and "a full-screen pattern goes
 *  alone". Their ids are made unique; it is never sent anywhere. */
export function withKept(r: HarnessRecipe, kept: HarnessElement[]): HarnessRecipe {
  if (!kept.length) return r;
  return { ...r, elements: [...kept.map((e, i) => ({ ...e, id: `${KEPT_PREFIX}${i}-${e.id}` })), ...r.elements] };
}

/** withKept's ids: a layer of the version added to (it can't be moved). */
export const KEPT_PREFIX = "kept-";

/** The recipe with these patterns appended, auto-placed, in the order the
 *  server plans best: single-place patterns first (in tick order), then
 *  those with several places, which fit around the others. Respects the
 *  cap and "full goes alone while it shows" — counting `kept`, the layers
 *  of the version they are added on top of. A transition (Blueprint) comes
 *  in as 1.5 s at the scene's start, and every pattern showing then starts
 *  once it is over (each movable one pinned first where the preview on
 *  screen drew it: `opts.pins`); a pattern added beside a full-screen one
 *  shows where it doesn't. Returns the new element ids. */
export function addPatterns(
  r: HarnessRecipe,
  slugs: string[],
  c: HarnessCatalogue | null,
  kept: HarnessElement[] = [],
  opts: { duration?: number | null; pins?: Pins } = {},
): { recipe: HarnessRecipe; ids: string[] } {
  const single = slugs.filter((s) => !hasManyPlaces(entryOf(c, s)));
  const multi = slugs.filter((s) => hasManyPlaces(entryOf(c, s)));
  const all = withKept(r, kept);
  const d = opts.duration ?? FAR;
  let next = r;
  const ids: string[] = [];
  const added: string[] = [];
  for (const slug of [...single, ...multi]) {
    if (addBlocker(all, c, slug, added, opts.duration ?? null)) continue;
    const id = newPatternId(next);
    let el: HarnessElement = {
      id,
      kind: "pattern",
      enabled: true,
      z: "behind",
      exact: false,
      placement: { anchor: "auto" },
      animation: { entrance: "auto" },
      props: { slug, layout: null, colour: "auto" },
    };
    const t = transitionOf(c, slug);
    if (t) {
      el = asTransition(el, t, d);
      next = makeRoom(next, t.sec, d, opts.pins ?? null);
    } else if (!isFullOnly(entryOf(c, slug))) {
      const spans = fullSpans(withKept(next, kept), c, [], d);
      const free = spans.length ? freeSpan(spans, d) : null;
      if (free) el = withSpan(el, free, d);
    }
    next = touch(next, [...next.elements, el]);
    ids.push(id);
    added.push(slug);
  }
  return { recipe: next, ids };
}

/** Every switched-on pattern showing before `from` s starts at `from` (a
 *  transition goes alone over the scene's start) — each movable one pinned
 *  first where the preview on screen drew it (which patterns show together
 *  decides where the planner puts them). */
function makeRoom(r: HarnessRecipe, from: number, d: number, pins: Pins): HarnessRecipe {
  const early = (e: HarnessElement) => e.kind === "pattern" && e.enabled && (spanOn(e, d)?.from ?? d) < from - 1e-9;
  if (!r.elements.some(early)) return r;
  const pinned = pinMovable(r, pins, () => true);
  const moved = startingAfter(pinned.elements.filter(early), from, d);
  if (!moved) return r;
  const byId = new Map(moved.map((e) => [e.id, e]));
  return touch(pinned, pinned.elements.map((e) => byId.get(e.id) ?? e));
}

/** Where the preview ON SCREEN drew each pattern — only ever the preview of
 *  the recipe being edited (a stale one would pin old places): the caller
 *  passes null unless the preview is current. */
export type Pins = {
  preview: HarnessPreview | null;
  recipe: HarnessRecipe | null;
  catalogue?: HarnessCatalogue | null;
} | null;

/** Pin every switched-on pattern `which` picks that is not pinned yet and
 *  could move (mayMove) where the preview drew it — so an edit around it
 *  (a new palette, a removed or hidden neighbour) leaves it where it is.
 *  Returns `r` itself when nothing needed a pin. */
export function pinMovable(r: HarnessRecipe, pins: Pins, which: (el: HarnessElement) => boolean): HarnessRecipe {
  if (!pins?.preview) return r;
  let hit = false;
  const elements = r.elements.map((e) => {
    if (e.kind !== "pattern" || !e.enabled || e.props.layout || !which(e)) return e;
    const pp = previewPatternOf(pins.preview, pins.recipe, e.id);
    if (!pp?.layout || !mayMove(pp, entryOf(pins.catalogue ?? null, slugOf(e)))) return e;
    hit = true;
    return withProps(e, { layout: { ...pp.layout } });
  });
  return hit ? { ...r, elements } : r;
}

/** The recipe without element `id`. Every other pattern that could move is
 *  pinned where the preview on screen drew it (pinMovable), so removing one
 *  never moves the others. */
export function removeElement(r: HarnessRecipe, id: string, pins?: Pins): HarnessRecipe {
  if (!r.elements.some((e) => e.id === id)) return r;
  const pinned = pinMovable(r, pins ?? null, (e) => e.id !== id);
  return touch(pinned, pinned.elements.filter((e) => e.id !== id));
}

/** A short random tag for props.shuffle (^[a-z0-9]{1,16}$). */
export function shuffleTag(): string {
  return Math.random().toString(36).slice(2, 6).replace(/[^a-z0-9]/g, "") || "x";
}

/** Draw another of this pattern's best places: its pin is cleared and its
 *  own shuffle tag changes, so no other pattern moves. */
export function anotherSpot(r: HarnessRecipe, id: string, tag: string = shuffleTag()): HarnessRecipe {
  return updateElement(r, id, (el) => clearHand(withProps(el, { layout: null, shuffle: tag })));
}

/** A new colour ("auto" or #rrggbb). `pin` is where the current preview drew
 *  the pattern when the server placed it automatically: pinning it keeps a
 *  colour change from moving it. */
export function setPatternColour(
  r: HarnessRecipe,
  id: string,
  colour: string,
  pin?: HarnessPatternLayout | null,
): HarnessRecipe {
  return updateElement(r, id, (el) =>
    withProps(el, pin && !el.props.layout ? { colour, layout: { ...pin } } : { colour }),
  );
}

/** A new anchor clears the pin and the shuffle: the place is chosen anew. */
export function setAnchor(r: HarnessRecipe, id: string, anchor: string): HarnessRecipe {
  return updateElement(r, id, (el) => {
    const next = clearHand(withProps(el, { layout: null }, ["shuffle"]));
    return { ...next, placement: { ...next.placement, anchor } };
  });
}

/** Another pattern in this element's place in the list: the pin, the
 *  shuffle and a picked entrance belong to the old one. */
export function swapPattern(
  r: HarnessRecipe,
  id: string,
  slug: string,
  opts: { catalogue?: HarnessCatalogue | null; duration?: number | null; pins?: Pins } = {},
): HarnessRecipe {
  const c = opts.catalogue ?? null;
  const d = opts.duration ?? FAR;
  const was = r.elements.find((e) => e.id === id);
  const t = transitionOf(c, slug);
  // A transition in its place: it takes the transition's time, and the
  // others start once it is over. The time a transition had goes with it.
  const base = t ? makeRoom(r, t.sec, d, opts.pins ?? null) : r;
  return updateElement(base, id, (el) => {
    let next: HarnessElement = {
      ...clearHand(withProps(el, { slug, layout: null }, ["shuffle"])),
      animation: { ...el.animation, entrance: "auto" },
    };
    if (t) next = asTransition(next, t, d);
    else if (was && transitionOf(c, slugOf(was))) next = untimed(next, d);
    return next;
  });
}

/** An element with no time of its own: the whole scene, in and out as its
 *  own. */
function untimed(el: HarnessElement, d: number): HarnessElement {
  return withRamps(withSpan(el, { from: 0, to: d }, d), { entranceSec: null, exit: "auto", exitSec: null });
}

/** Pattern `id` moved, sized or turned by hand to `hand` (its move, size
 *  and turn from where it was placed — a gesture's result, or a slider's).
 *  Every other pattern that could move is pinned where the preview on
 *  screen drew it (pinMovable), and `id` itself is pinned to `drawn.layout`
 *  with `drawn.box` (the pivot) the first time. */
export function setHand(
  r: HarnessRecipe,
  id: string,
  hand: Pick<Hand, "dx" | "dy" | "scale" | "rotate">,
  drawn: HandDrawn | null,
  pins?: Pins,
): HarnessRecipe {
  const pinned = pinMovable(r, pins ?? null, (e) => e.id !== id);
  return updateElement(pinned, id, (el) => withHand(el, hand, drawn));
}

/** "Back to automatic": no move, no pin — placed again by the planner. */
export function backToAuto(r: HarnessRecipe, id: string): HarnessRecipe {
  return updateElement(r, id, withoutHand);
}

/** Whether a pattern moves with the camera's push-in ("room"), holds still
 *  on screen ("screen"), or the engine picks ("auto": left out, as the
 *  server leaves it out). */
export function setFollow(r: HarnessRecipe, id: string, follow: "auto" | "room" | "screen"): HarnessRecipe {
  return updateElement(r, id, (el) =>
    follow === "auto" ? withProps(el, {}, ["follow"]) : withProps(el, { follow }),
  );
}

/** A pattern behind the speaker (the kind's default: no key) or in front
 *  of her (uncut, over everything). */
export function setLayer(r: HarnessRecipe, id: string, z: "behind" | "front"): HarnessRecipe {
  return updateElement(r, id, (el) => {
    const { z: _old, ...rest } = el;
    return z === "front" ? { ...rest, z } : rest;
  });
}

/** Show or hide a pattern. Hiding one pins the others that could move
 *  (pinMovable), as Remove does: a pattern that moved off a side this one
 *  held stays put while it is hidden. */
export function setEnabled(r: HarnessRecipe, id: string, enabled: boolean, pins?: Pins): HarnessRecipe {
  const base = enabled ? r : pinMovable(r, pins ?? null, (e) => e.id !== id);
  return updateElement(base, id, (el) => ({ ...el, enabled }));
}

/** Why a hidden pattern can't be shown right now (null: it can): the same
 *  "full goes alone" rules as adding one. The cap never blocks: a hidden
 *  pattern already counts towards it. */
export function showBlocker(
  r: HarnessRecipe,
  c: HarnessCatalogue | null,
  id: string,
): Exclude<AddBlocker, "cap"> | null {
  const el = r.elements.find((e) => e.id === id);
  if (!el || el.enabled) return null;
  // While it would show, a full-screen pattern goes alone.
  const clash = fullClash(timedPatterns(r, c), { full: goesFull(el, c) }, patternSpan(el));
  if (!clash) return null;
  return goesFull(el, c) ? "full-alone" : "full-present";
}

/** A new palette (colours.source / custom) for every Auto colour. Each
 *  auto-placed pattern drawn in it is pinned first (pinMovable): the planner
 *  weighs the colour, so a new palette alone could move it. */
export function setPalette(r: HarnessRecipe, colours: HarnessRecipe["colours"], pins?: Pins): HarnessRecipe {
  if (same(r.colours, colours)) return r;
  const pinned = pinMovable(r, pins ?? null, (e) => !isHex(e.props.colour));
  return { ...pinned, preset: null, colours };
}

export function setEntrance(r: HarnessRecipe, id: string, entrance: string): HarnessRecipe {
  return updateElement(r, id, (el) => ({ ...el, animation: { ...el.animation, entrance } }));
}

/** Pattern `id` shows over `span` (scene seconds; the whole scene is kept as
 *  none). Every movable pattern — this one too — is pinned first where the
 *  preview on screen drew it: which patterns show together decides where
 *  the planner puts them, so nothing jumps. */
export function setSpan(r: HarnessRecipe, id: string, span: SecSpan, duration: number, pins?: Pins): HarnessRecipe {
  const pinned = pinMovable(r, pins ?? null, () => true);
  return updateElement(pinned, id, (el) => withSpan(el, span, duration));
}

/** How pattern `id` comes in and goes out, and over how long (each length at
 *  its default left out). */
export function setRamps(r: HarnessRecipe, id: string, ramps: Ramps): HarnessRecipe {
  return updateElement(r, id, (el) => withRamps(el, ramps));
}

export function isUntouchedDefault(draft: HarnessRecipe | null, def: HarnessRecipe | null): boolean {
  return !!draft && !!def && recipeKey(draft) === recipeKey(def);
}

// --- who draws it --------------------------------------------------------------

/** Made in house: our own graphics over the untouched footage, $0 — no
 *  Omni, no prompt. */
export function isHouse(r: HarnessRecipe | null | undefined): boolean {
  return r?.engine === "house";
}

/** The recipe made in house (the owner, 2026-09-29: no Omni for new work).
 *  Every new or derived draft goes through here — the starting draft, one
 *  saved before, Change it, Add to it, Make it in house. A frozen pattern
 *  (props.resolved: the server's, from a version) never comes along. */
export function asHouse(r: HarnessRecipe): HarnessRecipe {
  const frozen = r.elements.some((e) => e.kind === "pattern" && "resolved" in e.props);
  if (r.engine === "house" && !frozen) return r;
  return {
    ...r,
    engine: "house",
    elements: frozen
      ? r.elements.map((e) => {
          if (e.kind !== "pattern" || !("resolved" in e.props)) return e;
          const { resolved: _frozen, ...props } = e.props;
          return { ...e, props };
        })
      : r.elements,
  };
}

// --- the stray-drawing clean-up ------------------------------------------------

export function strayCleanOn(r: HarnessRecipe | null | undefined): boolean {
  return r?.guards.stray === "clean";
}

/** The clean-up on or off. Off is left out, as the server leaves it out, so
 *  the 42 default stays the 42 default. */
export function setStrayClean(r: HarnessRecipe, on: boolean): HarnessRecipe {
  const { stray: _off, ...rest } = r.guards;
  return { ...r, preset: null, guards: on ? { ...rest, stray: "clean" } : rest };
}

/** Whether a run of `r` can clean up at all (the server's
 *  guards.asks_omni_for_more, turned round): Omni is asked for nothing but
 *  the patterns — our icons stamped over a pattern the brief describes, or
 *  the harness's own opening — never for icons of its own. Without our
 *  icons the brief asks Omni for some, and they are no strays. */
export function strayCleanApplies(r: HarnessRecipe): boolean {
  const icon = iconsOf(r);
  if (icon?.enabled && !iconsOn(r)) return false;
  if (r.director.head !== "brief") return true;
  return iconsOn(r) && enabledPatterns(r).length > 0;
}

// --- what changed --------------------------------------------------------------

export type ChangeKind =
  | "none"
  /** Only what Omni is told changes (notes, feel, entrances, the flash
   *  guard): the target frame cannot change. */
  | "words"
  | "colour"
  | "hide"
  | "show"
  | "add"
  | "remove"
  | "pattern"
  | "place"
  /** Only a move by hand (a move, a size, a turn): the stage paints it at
   *  once from the pattern's own art until the exact frame lands. */
  | "hand"
  /** Only when a pattern shows, comes in or goes out (made in house: the
   *  pixels at some moments, and which patterns the still shows). */
  | "time"
  | "shuffle"
  /** Several kinds at once, or anything else that repaints. */
  | "pixels";

export interface ChangeInfo {
  kind: ChangeKind;
  /** The elements it touched (for row spinners and labels). */
  ids: string[];
}

function same(a: unknown, b: unknown): boolean {
  return recipeKey(a) === recipeKey(b);
}

/** What `next` changes about `prev` (the recipe of the preview on screen).
 *  null prev = the first preview of the scene. */
export function describeChange(prev: HarnessRecipe | null, next: HarnessRecipe): ChangeInfo {
  if (!prev) return { kind: "pixels", ids: [] };
  if (same(prev, next)) return { kind: "none", ids: [] };
  const kinds = new Set<ChangeKind>();
  const ids = new Set<string>();
  const hit = (k: ChangeKind, id?: string) => {
    kinds.add(k);
    if (id) ids.add(id);
  };

  if (!same(prev.seed, next.seed)) hit("shuffle");
  if (!same(prev.colours, next.colours)) hit("colour");
  if (!same(prev.director, next.director)) hit("words");
  const { veil: pv, ...pg } = prev.guards;
  const { veil: nv, ...ng } = next.guards;
  if (pv !== nv) hit("words");
  if (!same(pg, ng)) hit(prev.guards.bars !== next.guards.bars ? "pixels" : "words");
  if (prev.recipe_version !== next.recipe_version || prev.engine !== next.engine) hit("pixels");

  // Icons are stamped after Omni, never drawn into the frame Omni is shown:
  // switching them on or off changes only what Omni is told ("draw none"),
  // and everything else about them only their own plan.
  const drawn = (xs: HarnessElement[]) => xs.filter((e) => e.kind !== "icon");
  const before = new Map(drawn(prev.elements).map((e) => [e.id, e]));
  const after = new Map(drawn(next.elements).map((e) => [e.id, e]));
  const iconKey = (r: HarnessRecipe) => r.elements.filter((e) => e.kind === "icon");
  if (!same(iconKey(prev), iconKey(next))) hit("words");
  for (const [id, e] of after) {
    if (!before.has(id)) hit(e.enabled ? "add" : "words", id);
  }
  for (const [id, e] of before) {
    if (!after.has(id)) hit(e.enabled ? "remove" : "words", id);
  }
  const order = (xs: HarnessElement[]) => xs.filter((e) => before.has(e.id) && after.has(e.id)).map((e) => e.id);
  if (!same(order(drawn(prev.elements)), order(drawn(next.elements)))) hit("pixels");
  for (const [id, n] of after) {
    const p = before.get(id);
    if (!p) continue;
    if (p.enabled !== n.enabled) {
      hit(n.enabled ? "show" : "hide", id);
      continue;
    }
    // A hidden element's settings draw nothing.
    const quiet = !n.enabled;
    const mark = (k: ChangeKind) => hit(quiet ? "words" : k, id);
    if (p.kind !== n.kind || p.z !== n.z || p.exact !== n.exact) mark("pixels");
    // A move by hand (its move, size, turn and the box it was grabbed at)
    // is its own kind; anything else about the place is a new place.
    const { offset: po, rotate: pr, size: psz, box: pb, ...pPlace } = p.placement;
    const { offset: no, rotate: nr, size: nsz, box: nb, ...nPlace } = n.placement;
    if (!same(pPlace, nPlace)) mark("place");
    else if (!same([po ?? null, pr ?? null, psz ?? null, pb ?? null], [no ?? null, nr ?? null, nsz ?? null, nb ?? null]))
      mark(p.kind === "pattern" ? "hand" : "place");
    // Made in house a pattern's time is pixels at some moment (its span, its
    // entrance and exit); for Omni an entrance is only what it is told.
    if (!same(p.animation, n.animation)) mark(p.kind === "pattern" && next.engine === "house" ? "time" : "words");
    const { slug: ps, colour: pc, layout: pl, shuffle: psh, ...prest } = p.props;
    const { slug: ns, colour: nc, layout: nl, shuffle: nsh, ...nrest } = n.props;
    if (!same(ps, ns)) mark("pattern");
    if (!same(pc, nc)) mark("colour");
    if (!same(psh ?? null, nsh ?? null)) mark("shuffle");
    // A pin set alongside a colour (or cleared with a new shuffle or a new
    // pattern) is part of that edit; set alone it keeps the drawn place.
    if (!same(pl ?? null, nl ?? null) && same(ps, ns) && same(pc, nc) && same(psh ?? null, nsh ?? null)) {
      mark(nl ? "words" : "place");
    }
    if (!same(prest, nrest)) mark("pixels");
  }

  const visible = [...kinds].filter((k) => k !== "words");
  const kind: ChangeKind = visible.length === 0 ? "words" : visible.length === 1 ? visible[0] : "pixels";
  return { kind, ids: [...ids] };
}
