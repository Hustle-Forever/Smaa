// Rebrand studio — building on a finished version, as pure functions.
//
//   Add to it   (base mode "stack")    the draft holds only the NEW layers;
//               the version's own stay exactly as they are ("kept": listed,
//               read-only). Omni works on the version's own video.
//   Change it   (base mode "rebuild")  the draft becomes the version's whole
//               look, every layer editable, each pattern pinned where its run
//               drew it (so loading it moves nothing). Omni redoes it from the
//               original, reusing the parts whose input didn't change.
//
// Layers are compared by what they draw — kind, pattern, the place asked
// for, colour, motion, shuffle — never by id (every pass numbers its own from
// "pattern") and never by a pin (a pin keeps a place; it isn't a new one).
// That is how the studio sees what a draft adds to a version, and when a
// draft would make a version that already exists ("That's v2 — open it") —
// for that, each pattern must also be drawn where the version's run put it.
//
// Icons are the exception to "added on top": a version shows at most one
// icon element, stamped by us on its pre-stamp clip. Added to, a version
// with our icons puts ITS icons in the draft, editable — Generate re-stamps
// them in place of the old ones, with no Omni at all. A draft's icons never
// carry the router's `resolved`, nor do its patterns.
//
// Every draft made here is made in house (asHouse: engine "house", $0 — the
// owner, 2026-09-29). Added to, a version Omni made keeps Omni's video under
// the new layers; so does a version made in house ON TOP of one Omni made (a
// "house pass", housePass): the server draws its own layers again over that
// same video, under the new ones — its layers are kept, like Omni's. A
// version made in house from the original is changed instead (the studio
// sends Change it: redrawn from the original with all it had, the very same
// picture). Changed, anything with Omni's video under it (groundOf) is
// redone flat: never "the same picture" as the version (sameAsVersion).
//
// After Generate the studio carries on from the new version ("Add more",
// continueMode): made in house from the original, the draft becomes its
// whole recipe — each row it had tagged "from v3" (inheritedFrom) — so Next
// IS v3's picture until something changes; with Omni's video under it, the
// new layers go on top of it. Its icons are planned again by the server;
// when they come out other than the version froze them (frozenIconsOf),
// "Keep v3's icons" keeps them exactly (rebrandIcons.keepIcons). A version's
// read-only timeline shows them on the tracks its draft had (frozenLanesOf),
// and so does a draft that keeps them (keptItemLanes).

import {
  recipeKey,
  recipeOfVersion,
  recordedPatterns,
  type HarnessBaseRef,
  type HarnessCatalogue,
  type HarnessElement,
  type HarnessIconProps,
  type HarnessLayer,
  type HarnessPatternLayout,
  type HarnessRecipe,
  type HarnessRecordedPattern,
  type HarnessVersion,
} from "./rebrandHarnessApi";
import { handSig, isHandPlaced } from "./rebrandHand";
import { cleanIconElement, cleanIconProps, iconsOf, iconsOn } from "./rebrandIcons";
import { asHouse, BRAND_COLOUR, enabledPatterns, isHex, joinBlocker, slugOf } from "./rebrandRecipe";
import { timeSig } from "./rebrandTiming";
import { frozenLanes } from "./rebrandTracks";

/** How many "Add to it" passes can sit on a version made from the original. */
export const MAX_STACK_DEPTH = 3;

/** A version and the ones it was added on top of, oldest first (it last). */
export function chainOf(v: HarnessVersion, versions: HarnessVersion[]): HarnessVersion[] {
  const out = [v];
  let cur = v;
  while (cur.base?.mode === "stack" && out.length <= MAX_STACK_DEPTH + 4) {
    const id = cur.base.version_id;
    const parent = versions.find((x) => x.id === id);
    if (!parent || out.includes(parent)) break;
    out.unshift(parent);
    cur = parent;
  }
  return out;
}

/** Made in house (our own graphics over the footage), not by Omni. (The
 *  studio's madeInHouse reads the same.) */
function houseMade(v: HarnessVersion): boolean {
  const opts = (v.options ?? {}) as Record<string, unknown>;
  return recipeOfVersion(v)?.engine === "house" || opts.engine === "house" || v.arms?.omni?.engine === "house";
}

/** Made in house ON TOP of another version (Add to it on one Omni made):
 *  its own layers over that version's video, which stays under them — and
 *  under anything added to it. */
export function housePass(v: HarnessVersion): boolean {
  return houseMade(v) && v.base?.mode === "stack";
}

/** The version whose (Omni-made) video is under v: v itself when Omni made
 *  it, the one its house passes sit on — or null for one made in house from
 *  the original (or whose version under it isn't known). */
export function groundOf(v: HarnessVersion, versions: HarnessVersion[]): HarnessVersion | null {
  const chain = chainOf(v, versions);
  for (let i = chain.length - 1; i >= 0; i--) {
    if (!housePass(chain[i])) return houseMade(chain[i]) ? null : chain[i];
  }
  return null;
}

/** Every layer a version shows, each with the version that drew it: the
 *  server's list when it sends one; else its recipe, after its base's when
 *  it was added on top of one. */
export function effectiveLayersOf(v: HarnessVersion, versions: HarnessVersion[]): HarnessLayer[] {
  if (Array.isArray(v.layers)) return v.layers;
  return chainOf(v, versions).flatMap((x) =>
    (recipeOfVersion(x)?.elements ?? []).map((e) => ({ ...e, from_version: x.id })),
  );
}

/** The elements a version (and every version it was added on top of)
 *  holds that aren't patterns — icons, and whatever comes after them. The
 *  server's layer list names patterns only, so these are read from the
 *  recipes. Of the icon elements only the last pass's counts: a pass with
 *  icons re-stamps them in place of the ones under it. */
function otherElementsOf(v: HarnessVersion, versions: HarnessVersion[]): HarnessLayer[] {
  const all = chainOf(v, versions).flatMap((x) =>
    (recipeOfVersion(x)?.elements ?? [])
      .filter((e) => e.kind !== "pattern")
      .map((e) => ({ ...e, from_version: x.id })),
  );
  const lastIcon = [...all].reverse().find((e) => e.kind === "icon");
  return all.filter((e) => e.kind !== "icon" || e === lastIcon);
}

/** The icon element a version shows (its own, or the last pass's under
 *  it), as a draft may hold it; null when it has none of ours. */
export function shownIconOf(v: HarnessVersion, versions: HarnessVersion[]): HarnessElement | null {
  const el = otherElementsOf(v, versions).find((e) => e.kind === "icon");
  return el ? cleanIconElement(elementOf(el)) : null;
}

/** The version was made from a brief that asked Omni for icons of its own
 *  (the 42 default does): building ours on it would show two sets, so the
 *  studio steers to Change it (redone with Omni told to draw none), or lets
 *  the user keep Omni's (omni_own "keep", ours off). */
export function hasOmniOwnIcons(v: HarnessVersion, versions: HarnessVersion[]): boolean {
  const root = recipeOfVersion(chainOf(v, versions)[0]);
  // Made in house, Omni drew nothing at all.
  return !!root && root.engine !== "house" && root.director.head === "brief" && !iconsOn(root);
}

/** Everything a version shows: its pattern layers (effectiveLayersOf) and
 *  every other element of its recipes, each with the version that drew it. */
export function shownElementsOf(v: HarnessVersion, versions: HarnessVersion[]): HarnessLayer[] {
  return [
    ...effectiveLayersOf(v, versions).filter((l) => l.kind === "pattern"),
    ...otherElementsOf(v, versions),
  ];
}

/** How many "Add to it" passes it sits on (0: made from the original). */
export function stackDepth(v: HarnessVersion, versions: HarnessVersion[]): number {
  const chain = chainOf(v, versions);
  const whole = chain[0].base?.mode !== "stack";
  return whole ? chain.length - 1 : Math.max(chain.length - 1, v.depth ?? 0);
}

/** The palette ("Auto") a layer was drawn with: its own version's. */
function paletteOf(layer: HarnessLayer, versions: HarnessVersion[], fallback: HarnessRecipe["colours"]) {
  const v = versions.find((x) => x.id === layer.from_version);
  return (v && recipeOfVersion(v)?.colours) || fallback;
}

/** The colour a layer is drawn in, as the server's colour_override reads
 *  it: its own hex, else the palette's (42 violet and a custom 42 violet are
 *  one colour; the intro's is "intro" until a preview says it). */
function colourKey(el: HarnessElement, colours: HarnessRecipe["colours"]): string {
  const own = el.props.colour;
  if (isHex(own)) return own.toLowerCase();
  if (colours.source === "brand") return BRAND_COLOUR;
  if (colours.source === "custom" && isHex(colours.custom)) return colours.custom.toLowerCase();
  return "intro";
}

/** A layer as what it draws. An icon element: its settings and the icons
 *  the user asked for (never the router's `resolved`, never a default
 *  written out). */
export function layerSig(el: HarnessElement, colours: HarnessRecipe["colours"]): string {
  if (el.kind === "icon") {
    // "Keep v3's icons" keeps the icons the same element asks for: not a
    // new look of its own.
    const { keep: _kept, ...props } = el.props;
    return recipeKey([
      "icon",
      el.placement.anchor,
      el.placement.size ?? null,
      el.placement.box ?? null,
      el.animation.entrance === "glitch" ? "auto" : el.animation.entrance,
      cleanIconProps(props),
    ]);
  }
  // Never the router's frozen place (`resolved`): a pin keeps a place, it
  // isn't a new one.
  const { slug, colour: _c, layout: _l, shuffle, resolved: _r, ...rest } = el.props;
  // A move by hand (its move, size, turn and pivot) draws it elsewhere, and
  // its own time (its span, its ramps, its exit) at other moments; each is
  // left out while there is none, so every layer before keeps its sign.
  const hand = handSig(el);
  const time = timeSig(el);
  return recipeKey([
    el.kind,
    slug ?? null,
    el.placement.anchor,
    colourKey(el, colours),
    el.animation.entrance,
    shuffle ?? null,
    rest,
    ...(hand === null ? [] : [hand]),
    ...(time === null ? [] : [{ time }]),
  ]);
}

/** Where (and in what colour) the run that drew this layer really put it:
 *  from its version's evidence, else from the place the server lists with
 *  the layer (a version further down a stack than the list reaches). */
export function recordedOf(layer: HarnessLayer, versions: HarnessVersion[]): HarnessRecordedPattern | null {
  if (layer.kind !== "pattern") return null;
  const v = versions.find((x) => x.id === layer.from_version);
  const placed = layer.placed?.layout && typeof layer.placed.layout.placement === "string" ? layer.placed : null;
  const fromServer: HarnessRecordedPattern | null = placed
    ? { element_id: layer.id, slug: placed.slug, name: placed.name, colour: null, layout: placed.layout }
    : null;
  if (!v) return fromServer;
  const rec = recordedPatterns(v);
  const byId = rec.find((r) => r.element_id === layer.id);
  if (byId) return byId;
  // A one-pattern run records just that one: the version's first pattern on.
  const loose = rec.filter((r) => r.element_id === null);
  const recipe = recipeOfVersion(v);
  const first = recipe ? enabledPatterns(recipe)[0] : undefined;
  if (loose.length === 1 && first?.id === layer.id && (!loose[0].slug || loose[0].slug === slugOf(layer))) {
    return loose[0];
  }
  return fromServer;
}

/** A layer as a recipe element: without what the server notes about it
 *  (the version that drew it, where it went) — a recipe refuses keys it
 *  doesn't know. */
function elementOf(layer: HarnessLayer): HarnessElement {
  const { from_version: _from, placed: _placed, ...el } = layer;
  if (el.kind !== "pattern" || !("resolved" in el.props)) return el;
  const { resolved: _frozen, ...props } = el.props;
  return { ...el, props };
}

function unpinned(el: HarnessElement): HarnessElement {
  // A pattern moved by hand keeps the place it was moved from (the move
  // means nothing without it — and it holds no slot for others to avoid).
  if (isHandPlaced(el)) return el;
  return "layout" in el.props ? { ...el, props: { ...el.props, layout: null } } : el;
}

/** What a version shows that stacks up, as what it draws (each in its own
 *  palette): every layer but its icons, which are replaced, not added to. */
function sigsOf(v: HarnessVersion, versions: HarnessVersion[], fallback: HarnessRecipe["colours"]): string[] {
  return shownElementsOf(v, versions)
    .filter((l) => l.enabled && l.kind !== "icon")
    .map((l) => layerSig(l, paletteOf(l, versions, fallback)));
}

/** What the draft adds to version v: its switched-on layers less one of each
 *  of v's — when it holds every one of v's (it builds on v); else null.
 *  `under`: what the draft already stands on (added on top of another
 *  version, that version's layers), which counts towards v's first. Icons
 *  are left out on both sides (the callers carry them on their own). */
export function sinceVersion(
  draft: HarnessRecipe,
  v: HarnessVersion,
  versions: HarnessVersion[],
  under: string[] = [],
): HarnessElement[] | null {
  const left = sigsOf(v, versions, draft.colours);
  for (const sig of under) {
    const i = left.indexOf(sig);
    if (i >= 0) left.splice(i, 1);
  }
  const extra: HarnessElement[] = [];
  for (const el of draft.elements) {
    if (!el.enabled || el.kind === "icon") continue;
    const i = left.indexOf(layerSig(el, draft.colours));
    if (i >= 0) left.splice(i, 1);
    else extra.push(el);
  }
  return left.length === 0 ? extra : null;
}

/** The draft for "Add to it" on v: the clip-wide settings stay, and the
 *  layers are only what is new — what the draft had already added to v, or
 *  the new layers of an "Add to it" on another version, less what v already
 *  shows (v2 made from an "Add to it" on v1 has them: built on, v2 starts
 *  with nothing new). They are placed afresh, around v's graphics (no pins).
 *  Its icons are v's own, to edit (re-stamped for $0), or — on a version
 *  without ours — the draft's. */
export function addOnDraft(
  draft: HarnessRecipe,
  current: HarnessBaseRef | null,
  v: HarnessVersion,
  versions: HarnessVersion[],
): HarnessRecipe {
  const layers = draft.elements.filter((e) => e.kind !== "icon");
  let carried: HarnessElement[];
  if (current?.mode === "stack") {
    const on = versions.find((x) => x.id === current.version_id);
    const under = on ? sigsOf(on, versions, draft.colours) : [];
    carried = sinceVersion(draft, v, versions, under) ?? layers;
  } else {
    carried = sinceVersion(draft, v, versions) ?? [];
  }
  const own = iconsOf(draft);
  const icon = shownIconOf(v, versions) ?? (own ? cleanIconElement(own) : null);
  return asHouse({ ...draft, preset: null, elements: [...carried.map(unpinned), ...(icon ? [icon] : [])] });
}

/** In "Add to it" on v, what the draft adds — its layers, and its icons
 *  unless they are v's own exactly as they are (then Generate would only
 *  make v again). */
export function newOnVersion(draft: HarnessRecipe, v: HarnessVersion, versions: HarnessVersion[]): HarnessElement[] {
  const shown = shownIconOf(v, versions);
  const sig = (e: HarnessElement) => recipeKey([e.enabled, layerSig(e, draft.colours)]);
  return draft.elements.filter((e) => {
    if (e.kind !== "icon") return e.enabled;
    // v's own icons, switched off or changed, are re-stamped: that is new.
    if (shown) return sig(e) !== sig(shown);
    return iconsOn({ ...draft, elements: [e] });
  });
}

/** The draft for "Change it" on v: v's whole look — its settings and every
 *  layer it shows, the ones it was added on top of too, patterns and every
 *  other element (icons) — each pattern pinned where its run drew it, so
 *  loading it moves nothing. A layer an earlier pass drew in another palette
 *  keeps its colour. What the draft had added to v comes along, as far as
 *  the cap and "full goes alone" allow. */
export function changeDraft(
  draft: HarnessRecipe,
  current: HarnessBaseRef | null,
  v: HarnessVersion,
  versions: HarnessVersion[],
  catalogue: HarnessCatalogue | null,
): HarnessRecipe {
  const own = recipeOfVersion(v);
  if (!own) return draft;
  const root = recipeOfVersion(chainOf(v, versions)[0]) ?? own;
  const taken = new Set<string>();
  const unique = (el: HarnessElement) => {
    let id = el.id;
    for (let n = 2; taken.has(id); n++) id = `${el.kind}-${n}`;
    taken.add(id);
    return id;
  };

  const elements: HarnessElement[] = shownElementsOf(v, versions).map((layer) => {
    const el = elementOf(layer);
    if (el.kind === "icon") return cleanIconElement({ ...el, id: unique(el) });
    const props: Record<string, unknown> = { ...el.props };
    const rec = recordedOf(layer, versions);
    if (rec?.layout && !props.layout) props.layout = { ...rec.layout };
    const palette = paletteOf(layer, versions, own.colours);
    if (rec && isHex(rec.colour) && !isHex(props.colour) && recipeKey(palette) !== recipeKey(own.colours)) {
      props.colour = rec.colour.toLowerCase();
    }
    return { ...el, id: unique(el), props };
  });

  let next: HarnessRecipe = {
    ...own,
    // Redone in house, whoever made it (Omni's version: its patterns come
    // out flat, not painted in).
    engine: "house",
    target: draft.target,
    preset: null,
    // An added-on version ran with the harness's opening; the look it
    // shows was first written by its root's.
    director: { ...own.director, head: root.director.head },
    elements,
  };
  const onIt = current?.mode === "stack" && current.version_id === v.id;
  const extras = (onIt ? draft.elements : current ? [] : sinceVersion(draft, v, versions) ?? []).filter(
    (e) => e.kind !== "icon",
  );
  for (const el of extras) {
    if (el.kind === "pattern" && joinBlocker(next, catalogue, el)) continue;
    next = { ...next, elements: [...next.elements, { ...unpinned(el), id: unique(el) }] };
  }
  // Icons: the ones being edited on v stay as edited; the draft's own come
  // along to a version with none of ours — redone, Omni is told to draw none
  // and ours replace any it drew. From another version, only when they are
  // on: that is when the studio sends "v1 has Omni's own icons" here.
  const mine = iconsOf(draft);
  const theirs = next.elements.findIndex((e) => e.kind === "icon");
  const carried = theirs < 0 && (!current || iconsOn({ ...draft, elements: mine ? [mine] : [] }));
  if (mine && (onIt || carried)) {
    const icon = cleanIconElement({ ...mine, id: theirs >= 0 ? next.elements[theirs].id : unique(mine) });
    next = {
      ...next,
      elements: theirs >= 0 ? next.elements.map((e, i) => (i === theirs ? icon : e)) : [...next.elements, icon],
    };
  }
  return asHouse(next);
}

/** What else changes the result besides the layers: who draws it, and —
 *  for Omni — the guards and the words it gets (the feel and the note; made
 *  in house there are none, so they change nothing). */
function settingsKey(r: HarnessRecipe): string {
  if (r.engine === "house") return recipeKey(["house", r.guards.speaker_front]);
  return recipeKey(["omni", r.guards, r.director.intensity, r.director.notes]);
}

/** Where a pattern is drawn — its side, or a top / bottom band and its
 *  corner (layoutLabel's reading) — or null when that isn't known. */
function placeKey(layout: HarnessPatternLayout | null | undefined): string | null {
  if (!layout || typeof layout.placement !== "string") return null;
  const p = layout.placement;
  return p === "top" || p === "bottom" ? `${p}-${layout.anchor === "right" ? "right" : "left"}` : p;
}

// A draft pattern whose place isn't known yet (its preview isn't in).
const NOT_YET = "?";

/** Each of `mine` drawn where one of `theirs` with its signature is. A place
 *  a version didn't record (null) fits any; a draft pattern not placed yet
 *  fits none: it isn't known to be the same. */
function samePlaces(mine: { sig: string; place: string | null }[], theirs: { sig: string; place: string | null }[]) {
  const left = [...theirs];
  // Known places first, so one that fits any doesn't take the match a known one needs.
  for (const m of [...mine].sort((a, b) => Number(a.place === null) - Number(b.place === null))) {
    if (m.place === NOT_YET) return false;
    const i = left.findIndex((t) => t.sig === m.sig && (t.place === null || m.place === null || t.place === m.place));
    if (i < 0) return false;
    left.splice(i, 1);
  }
  return true;
}

/** The ready version that already shows exactly what this draft would make
 *  (newest first) — the same layers, drawn in the same places, the same
 *  other elements (icons) and the same settings, over the same video (made
 *  in house: the original, or the Omni-made one under it) — or null. Only asked while
 *  building on a version: that is when a draft can land back on one
 *  ("remove the beads" gives v2 back). `kept`: the pattern layers of the
 *  version added on top of. `drawnAt`: where the preview on screen drew a
 *  draft pattern that isn't pinned (null while it hasn't) — a layer set can
 *  match a version whose run put one of them elsewhere, which is not this. */
export function sameAsVersion(
  draft: HarnessRecipe,
  base: HarnessBaseRef | null,
  kept: HarnessLayer[],
  versions: HarnessVersion[],
  drawnAt?: (el: HarnessElement) => HarnessPatternLayout | null,
): HarnessVersion | null {
  if (!base) return null;
  const sigs = (layers: HarnessLayer[]) =>
    layers.filter((l) => l.enabled).map((l) => layerSig(l, paletteOf(l, versions, draft.colours)));
  // Where each layer is drawn: a version's (and a kept one's) as its run
  // recorded it; the draft's from its pin, else the preview on screen.
  const recorded = (layers: HarnessLayer[]) =>
    layers
      .filter((l) => l.enabled)
      .map((l) => ({
        sig: layerSig(l, paletteOf(l, versions, draft.colours)),
        place: l.kind === "pattern" ? placeKey(recordedOf(l, versions)?.layout) : "",
      }));
  const drawn = (el: HarnessElement): string | null => {
    if (el.kind !== "pattern") return "";
    const pin = placeKey(el.props.layout as HarnessPatternLayout | null | undefined);
    if (pin || !drawnAt) return pin;
    return placeKey(drawnAt(el)) ?? NOT_YET;
  };
  const on = base.mode === "stack" ? versions.find((x) => x.id === base.version_id) : undefined;
  // Added on top, the draft's icons (when it has any) are the ones shown.
  const replaced = (l: HarnessLayer) => l.kind === "icon" && !!iconsOf(draft);
  const under =
    base.mode === "stack"
      ? [
          ...kept.filter((l) => l.kind === "pattern"),
          ...(on ? otherElementsOf(on, versions).filter((l) => !replaced(l)) : []),
        ]
      : [];
  const mine = [
    ...sigs(under),
    ...draft.elements.filter((e) => e.enabled).map((e) => layerSig(e, draft.colours)),
  ].sort();
  const minePlaced = [
    ...recorded(under),
    ...draft.elements.filter((e) => e.enabled).map((e) => ({ sig: layerSig(e, draft.colours), place: drawn(e) })),
  ];
  // Omni's own icons are in the picture too, though no layer says so: added
  // on top, the draft shows them when the version does; redone, when its
  // brief asks Omni for them (no icons of ours).
  const omniOwn = on
    ? hasOmniOwnIcons(on, versions)
    : draft.engine !== "house" && draft.director.head === "brief" && !iconsOn(draft);
  // Made in house, the video under the layers is part of the picture: the
  // original (redone flat) or the Omni-made video of the version added on
  // top of. A version over another video is another picture, whatever its
  // layers.
  const ground = (v: HarnessVersion | undefined) => (v ? groundOf(v, versions)?.id ?? null : null);
  const draftGround = draft.engine === "house" ? ground(on) : undefined;
  for (const v of [...versions].reverse()) {
    const own = recipeOfVersion(v);
    if (v.status !== "ready" || !own || settingsKey(own) !== settingsKey(draft)) continue;
    if (hasOmniOwnIcons(v, versions) !== omniOwn) continue;
    if (draftGround !== undefined && ground(v) !== draftGround) continue;
    const shown = shownElementsOf(v, versions);
    if (recipeKey(sigs(shown).sort()) === recipeKey(mine) && samePlaces(minePlaced, recorded(shown))) return v;
  }
  return null;
}

/** How the studio carries on from version v once Generate has made it
 *  ("Add more to vN"): made in house from the original, its whole recipe is
 *  the draft ("rebuild": redrawn from the original with all it had — the
 *  very same picture until something changes); with Omni's video under it
 *  (made by Omni, or in house on top of a version Omni made), the new
 *  layers go on top of it ("stack") and Omni's video stays. null: a version
 *  with no recipe to build on (the classic panel's), one that isn't ready —
 *  or one with Omni's video under it that is stacked as high as it goes:
 *  only Change it is left, which redoes Omni's look flat, and that is the
 *  owner's to ask for (the studio never does it by itself). */
export function continueMode(v: HarnessVersion, versions: HarnessVersion[]): "stack" | "rebuild" | null {
  if (v.status !== "ready" || !recipeOfVersion(v)) return null;
  if (houseMade(v) && !housePass(v)) return "rebuild";
  return stackDepth(v, versions) >= MAX_STACK_DEPTH ? null : "stack";
}

/** Changed from version v (rebuild), the draft's rows that are still v's
 *  own, unchanged — each with the version that drew it (v, or one it was
 *  added on top of): the rows the rail tags "from v3". Matched by what they
 *  draw, one for one; a row edited since is the draft's own. */
export function inheritedFrom(
  draft: HarnessRecipe,
  v: HarnessVersion,
  versions: HarnessVersion[],
): Record<string, number> {
  const left = shownElementsOf(v, versions)
    .filter((l) => l.enabled)
    .map((l) => ({ sig: layerSig(elementOf(l), paletteOf(l, versions, draft.colours)), from: l.from_version }));
  const out: Record<string, number> = {};
  for (const el of draft.elements) {
    if (!el.enabled) continue;
    const i = left.findIndex((x) => x.sig === layerSig(el, draft.colours));
    if (i < 0) continue;
    out[el.id] = left[i].from;
    left.splice(i, 1);
  }
  return out;
}

/** Where a version's still is taken (the server's house.version_still_at):
 *  its first icon fully in and a moment more (base.ICON_STILL_AFTER), else
 *  `fallback` (the design still's own moment). `frozen`: its icons. */
export function versionStillAt(frozen: unknown[] | null, fallback: number, fps = 30): number {
  const rows = (frozen ?? []).filter(
    (r): r is { settle_sec: number; to_sec: number } =>
      !!r && typeof (r as { settle_sec?: unknown }).settle_sec === "number" &&
      Number((r as { to_sec?: unknown }).to_sec) > Number((r as { settle_sec: number }).settle_sec),
  );
  if (!rows.length) return fallback;
  const first = rows.reduce((a, r) => (r.settle_sec < a.settle_sec ? r : a));
  return Math.round(Math.min(first.settle_sec + 0.4, first.to_sec - 1 / fps) * 1000) / 1000;
}

/** The icons version v shows, exactly as it froze them (its icon
 *  element's props.resolved — the last pass's that has one); null when it
 *  has none of ours. What a re-plan is checked against (iconsDiffer). */
export function frozenIconsOf(v: HarnessVersion, versions: HarnessVersion[]): unknown[] | null {
  for (const x of [...chainOf(v, versions)].reverse()) {
    const el = iconsOf(recipeOfVersion(x));
    if (!el) continue;
    const frozen = el.props.resolved;
    return Array.isArray(frozen) ? frozen : null;
  }
  return null;
}

/** A frozen icon row's glyph id ("" when it has none). */
function frozenGlyphId(r: unknown): string {
  const g = r && typeof r === "object" ? (r as { glyph?: unknown }).glyph : null;
  if (typeof g === "string") return g;
  const id = g && typeof g === "object" ? (g as { id?: unknown }).id : null;
  return typeof id === "string" ? id : "";
}

/** The tracks a version kept beside its `n` frozen icons at Generate
 *  (options.icon_lanes: a whole number from 0 up for each), else null. */
function keptLanes(x: HarnessVersion, n: number): number[] | null {
  const got = (x.options as Record<string, unknown> | undefined)?.icon_lanes;
  return Array.isArray(got) && got.length === n && got.every((k) => typeof k === "number" && Number.isInteger(k) && k >= 0)
    ? [...(got as number[])]
    : null;
}

/** The track each icon version v froze is on (frozenIconsOf's rows, in
 *  order), as the timeline of the draft it was made from had them: the
 *  planner's, kept beside them at Generate (options.icon_lanes; none when
 *  all are on track 0); its icons kept from another version ("Keep v3's
 *  icons"), that version's; else worked out from its recipe as the planner
 *  puts them (rebrandTracks.frozenLanes: a version made before they were
 *  kept). `gap`: the clear air on track 0 (rules.gap_user). Null when it
 *  froze none. */
export function frozenLanesOf(
  v: HarnessVersion,
  versions: HarnessVersion[],
  gap?: number,
  seen: ReadonlySet<number> = new Set(),
): number[] | null {
  for (const x of [...chainOf(v, versions)].reverse()) {
    const el = iconsOf(recipeOfVersion(x));
    if (!el) continue;
    const props = el.props as HarnessIconProps;
    const rows = props.resolved;
    if (!Array.isArray(rows)) return null;
    const kept = keptLanes(x, rows.length);
    if (kept) return kept;
    const from = typeof props.keep === "number" && !seen.has(x.id) ? versions.find((y) => y.id === props.keep) : undefined;
    const theirs = from ? frozenIconsOf(from, versions) : null;
    if (from && theirs && theirs.length === rows.length && theirs.every((r, i) => frozenGlyphId(r) === frozenGlyphId(rows[i]))) {
      const lanes = frozenLanesOf(from, versions, gap, new Set([...seen, x.id]));
      if (lanes) return lanes;
    }
    return frozenLanes(rows, props, gap);
  }
  return null;
}

/** A draft that keeps a version's icons (its plan's `kept`: "Keep v3's
 *  icons") shows them on the tracks that version's own timeline has them
 *  on (frozenLanesOf): each item with no track of the planner's takes its
 *  glyph's. Any other plan (`keptId` null), or one whose items all have
 *  their track: the very same items. */
export function keptItemLanes<T extends { glyph: { id: string }; lane?: number | null }>(
  items: T[],
  keptId: number | null | undefined,
  versions: HarnessVersion[],
  gap?: number,
): T[] {
  if (keptId == null || !items.some((it) => it.lane == null)) return items;
  const v = versions.find((x) => x.id === keptId);
  const rows = v ? frozenIconsOf(v, versions) : null;
  const lanes = v ? frozenLanesOf(v, versions, gap) : null;
  if (!rows || !lanes) return items;
  const by = new Map(rows.map((r, i) => [frozenGlyphId(r), lanes[i]]));
  return items.map((it) => (it.lane == null && by.has(it.glyph.id) ? { ...it, lane: by.get(it.glyph.id) } : it));
}
