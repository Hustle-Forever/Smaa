// Rebrand studio — the icons, as pure functions.
//
// A recipe holds at most ONE icon element ("icons"), always behind the
// speaker, showing up to five icons. Which ones is not a stored list: it is
// the live AI set (the server's director and scheduler, from what is said)
// plus what the user added, less what they removed —
//
//   props.added      icons the user picked (≤5, one per glyph), each with
//                    the moment it was added at (`at`, the playhead): one
//                    nothing said is tied to shows at the clear time
//                    nearest it. An icon the user adds always shows.
//   props.dismissed  icons the user removed (the AI's picks, or their own —
//                    which the AI may pick too): gone on every re-plan,
//                    until "Add back". Never an icon that is also added.
//   props.tweaks     per icon (by glyph id): pinned to another mention of its
//                    own idea (w + the digest of the words w indexes), or —
//                    only when the user chose it — a time off its word
//                    (off_word + sec: timed by hand, kept exactly there; two
//                    timed by hand may overlap); its length (hold_sec, 0.2 to
//                    30 s); the box the user put it in on the picture (box:
//                    kept exactly there, over the speaker too)
//   props.lanes      per icon (by glyph id): the timeline track the user
//                    dragged it to (1 and up: one of its own, so it shows
//                    with others; track 0, the AI's, one at a time, is no
//                    pin at all). The planner puts one of the user's that
//                    has no room on track 0 on a track of its own anyway.
//   props.looks      per icon (by glyph id): its own look — style, colour,
//                    glyph colour, words, size, behind / in front, how it
//                    comes in and goes — over the scene's (props.style,
//                    props.colour, the size, the entrance). An AI pick keeps
//                    its look whenever the AI shows it.
//   props.keep       "Keep v3's icons": that version's icons exactly as it
//                    froze them (the server plans nothing). Every icon edit
//                    here drops it — an edited icon is planned again.
//   props.resolved   the router's frozen answer, written at Generate. A
//                    draft never holds it: every edit here drops it.
//
// So "if the user didn't reorder it, the AI reorders it" always holds: an
// icon nobody touched is re-timed by every plan. The ORDER is speech order,
// derived by the server; reordering never swaps two icons' slots (that would
// show </> while they say "free"). It moves an icon to another mention of
// its own idea, or moves the ones it jumps over to theirs, or — when neither
// is said anywhere that fits — asks (reorder() → "choice").
//
// Controls set back to their default DELETE the prop, so a round trip gives
// the very same recipe, and a new scene's draft (the 42 default + icons)
// stays recognisable as that. A new draft's icons are circles; an unset
// style (every older version) is the 42 badge, as the server reads it.

import {
  recipeKey,
  type HarnessBox,
  type HarnessCatalogue,
  type HarnessElement,
  type HarnessIconAdd,
  type HarnessIconCatalogue,
  type HarnessIconGlyph,
  type HarnessIconIdea,
  type HarnessIconItem,
  type HarnessIconLook,
  type HarnessIconPlan,
  type HarnessIconProps,
  type HarnessIconRules,
  type HarnessIconShift,
  type HarnessIconStyle,
  type HarnessIconTweak,
  type HarnessRecipe,
} from "./rebrandHarnessApi";
import { iconSpanTweak, type Span, type SpanDrag } from "./rebrandSpan";
import { insideFrame, roundBox, type Box } from "./rebrandTransform";

export const ICON_ELEMENT_ID = "icons";
/** Icons a user may add to a scene (the server's IconProps cap). */
export const ADDED_MAX = 5;
const DISMISSED_MAX = 20;
/** Icons that may carry a pin (the server's ICON_TWEAKS_MAX: the user's
 *  five and the AI's three). */
const TWEAKS_MAX = 8;
/** Icons that may be put on a track, and the highest track (the server's
 *  ICON_LANES_MAX, ICON_LANE_MAX). */
const LANES_MAX = 8;
export const LANE_MAX = 7;
/** Icons that may keep a look of their own (the server's ICON_LOOKS_MAX). */
const LOOKS_MAX = 8;
/** A chip's words, at most (the server's CHIP_LABEL_MAX). */
export const CHIP_LABEL_MAX = 24;
/** One icon's own size, a share of the frame's short side. */
export const ICON_SIZE_RANGE = [0.08, 0.3] as const;

/** The scheduler's numbers, as the server's catalogue sends them (these are
 *  its defaults, for a catalogue that doesn't). */
export const DEFAULT_RULES: HarnessIconRules = {
  fps: 30,
  in_f: 8,
  out_f: 6,
  land_f: 2,
  hold_min: 1.0,
  hold_max: 3.0,
  gap_user: 0.3,
  gap_auto: 0.6,
  head_clear: 0.5,
  entrance_clear: 1.5,
  tail_clear: 0.3,
  items_max: 5,
  magnet: 0.5,
  user_hold_min: 0.2,
  user_hold_max: 30,
  overlap_max: 0.03,
  head_grow: 0.02,
  person: 0.5,
  window_pad: 0.25,
};

export function rulesOf(c: HarnessIconCatalogue | null | undefined): HarnessIconRules {
  return { ...DEFAULT_RULES, ...(c?.rules ?? {}) } as HarnessIconRules;
}

/** Size S / M / L as a share of the frame's short side (M is the default:
 *  no size written). */
export const ICON_SIZES = { S: 0.12, M: 0.16, L: 0.2 } as const;
export type IconSize = keyof typeof ICON_SIZES;

export const ICON_ENTRANCES = ["glitch", "pop", "fade", "slide", "none"] as const;
export type IconEntrance = (typeof ICON_ENTRANCES)[number];

/** The looks the studio offers: Circle and Circle + words. The 42 badge
 *  and "Just the icon" are older versions' looks, offered only while a
 *  draft uses them. */
export const NEW_STYLES = ["circle", "chip"] as const;
export const OLD_STYLES = ["badge", "glyph"] as const;

const DEFAULT_PROPS = {
  ai: true,
  style: "badge" as const,
  colour: "auto",
  omni_own: "hide" as const,
  hold_sec: null as number | null,
};

// --- reading -----------------------------------------------------------------

/** The recipe's icon element (there is at most one). */
export function iconsOf(r: HarnessRecipe | null | undefined): HarnessElement | null {
  return r?.elements.find((e) => e.kind === "icon") ?? null;
}

/** Its props, every default filled in (not `keep`: keptFrom). */
export function iconProps(el: HarnessElement | null | undefined): Required<Omit<HarnessIconProps, "resolved" | "keep">> {
  const p = (el?.props ?? {}) as HarnessIconProps;
  return {
    ai: p.ai ?? DEFAULT_PROPS.ai,
    style: p.style ?? DEFAULT_PROPS.style,
    colour: p.colour ?? DEFAULT_PROPS.colour,
    omni_own: p.omni_own ?? DEFAULT_PROPS.omni_own,
    hold_sec: p.hold_sec ?? DEFAULT_PROPS.hold_sec,
    added: Array.isArray(p.added) ? p.added : [],
    dismissed: Array.isArray(p.dismissed) ? p.dismissed : [],
    tweaks: p.tweaks && typeof p.tweaks === "object" ? p.tweaks : {},
    looks: p.looks && typeof p.looks === "object" ? p.looks : {},
    lanes: p.lanes && typeof p.lanes === "object" ? p.lanes : {},
  };
}

/** Our icons are on: an enabled element that isn't leaving them to Omni. */
export function iconsOn(r: HarnessRecipe | null | undefined): boolean {
  const el = iconsOf(r);
  return !!el && el.enabled && iconProps(el).omni_own !== "keep";
}

export function glyphId(g: HarnessIconGlyph | string | null | undefined): string {
  return typeof g === "string" ? g : g?.id ?? "";
}

/** A glyph by id: "c-<12 hex>" is one the AI drew, anything else the library. */
export function glyphOf(id: string): HarnessIconGlyph {
  return { source: /^c-[0-9a-f]{12}$/.test(id) ? "custom" : "library", id };
}

export function isAdded(r: HarnessRecipe, id: string): boolean {
  return iconProps(iconsOf(r)).added.some((a) => glyphId(a.glyph) === id);
}

export function sizeOf(el: HarnessElement | null | undefined): IconSize {
  const s = el?.placement.size;
  if (s == null) return "M";
  return s <= 0.14 ? "S" : s >= 0.18 ? "L" : "M";
}

/** The entrance as the chips show it ("auto" is the glitch). */
export function entranceOf(el: HarnessElement | null | undefined): IconEntrance {
  const e = el?.animation.entrance ?? "auto";
  return (ICON_ENTRANCES as readonly string[]).includes(e) ? (e as IconEntrance) : "glitch";
}

/** The plan's icons in speech order (the order they show in). */
export function speechOrder(items: HarnessIconItem[] | null | undefined): HarnessIconItem[] {
  return [...(items ?? [])].sort((a, b) => a.settle - b.settle || a.from - b.from);
}

/** The icons on track 0 (the AI's, one at a time) in speech order: the
 *  order "show this one first" moves an icon in. */
export function trackOrder(items: HarnessIconItem[] | null | undefined): HarnessIconItem[] {
  return speechOrder(items).filter((it) => laneOf(it) === 0);
}

/** The Icons list (speech order over every track): the row of track 0 a
 *  keyboard step (Alt+Up / Alt+Down, `dir`) from row `fromRow` moves to, past
 *  the rows on a track of their own; null when the row itself is on one, or
 *  no row of track 0 is that way. */
export function listStep(
  items: readonly Pick<HarnessIconItem, "lane">[],
  fromRow: number,
  dir: -1 | 1,
): number | null {
  if (laneOf(items[fromRow]) !== 0) return null;
  for (let r = fromRow + dir; r >= 0 && r < items.length; r += dir) {
    if (laneOf(items[r]) === 0) return r;
  }
  return null;
}

/** The Icons list: the row of track 0 a row dragged from `fromRow` and let
 *  go over row `toRow` goes to: that row when it is on track 0; over a row
 *  on a track of its own, the row of track 0 between it and where the drag
 *  came from (where it was let go, in track 0's order), else the next row
 *  of track 0 that way (as a keyboard step: the drag always moves it); null
 *  when the dragged row is on a track of its own, or nothing is that way. */
export function listDrop(
  items: readonly Pick<HarnessIconItem, "lane">[],
  fromRow: number,
  toRow: number,
): number | null {
  if (laneOf(items[fromRow]) !== 0 || toRow === fromRow || toRow < 0 || toRow >= items.length) return null;
  if (laneOf(items[toRow]) === 0) return toRow;
  const dir = toRow > fromRow ? 1 : -1;
  for (let r = toRow - dir; r !== fromRow; r -= dir) {
    if (laneOf(items[r]) === 0) return r;
  }
  return listStep(items, fromRow, dir);
}

/** An icon picked in the picker (ticked), as the plan has it: on screen;
 *  still being placed (the plan is not the draft's yet); or left out by the
 *  plan as it is now, with its idea's state and reason (the limit, a glyph
 *  gone from the library...), which "being placed" would hide. */
export type PickedState =
  | { kind: "on" }
  | { kind: "placing" }
  | { kind: "out"; state: string; reason: string | null };

export function pickedState(
  id: string,
  plan: Pick<HarnessIconPlan, "items" | "ideas"> | null | undefined,
  current: boolean,
): PickedState {
  if ((plan?.items ?? []).some((it) => glyphId(it.glyph) === id)) return { kind: "on" };
  if (!plan || !current) return { kind: "placing" };
  const idea = (plan.ideas ?? []).find((i) => glyphId(i.glyph) === id);
  if (!idea || idea.state === "on") return { kind: "placing" };
  return { kind: "out", state: idea.state, reason: idea.reason ?? null };
}

/** The digest of the words a pin's `w` indexes (a new take of the scene
 *  gives new words, and the server re-anchors or unpins). */
export function wordsDigestOf(plan: HarnessIconPlan | null | undefined): string | null {
  return plan?.words_digest ?? plan?.timeline?.digest ?? null;
}

// --- the starting draft --------------------------------------------------------

/** A new scene's icons: on, everything automatic, drawn as circles. */
export function startingIcons(): HarnessElement {
  return {
    id: ICON_ELEMENT_ID,
    kind: "icon",
    enabled: true,
    placement: { anchor: "auto" },
    animation: { entrance: "auto" },
    props: { style: "circle" },
  };
}

/** The draft a new scene starts from: the 42 default + automatic icons.
 *  ("Reset to 42 default" is the preset itself, with no icons.) */
export function withStartingIcons(def: HarnessRecipe): HarnessRecipe {
  if (iconsOf(def)) return def;
  return { ...def, preset: null, elements: [...def.elements, startingIcons()] };
}

export function isStartingDraft(draft: HarnessRecipe | null, def: HarnessRecipe | null): boolean {
  return !!draft && !!def && recipeKey(draft) === recipeKey(withStartingIcons(def));
}

/** The server draws icons today. */
export function iconsReady(c: HarnessCatalogue | null | undefined): boolean {
  return !!c?.kinds.find((k) => k.kind === "icon")?.ready;
}

// --- editing -----------------------------------------------------------------

/** Props as written: defaults dropped (a control set back gives the same
 *  recipe), empty lists and empty looks dropped, and never the router's
 *  `resolved`. */
export function cleanIconProps(props: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(props)) {
    if (k === "resolved" || v === undefined || (k === "keep" && v === null)) continue;
    if (k in DEFAULT_PROPS && (v === DEFAULT_PROPS[k as keyof typeof DEFAULT_PROPS] || (v === null && k === "hold_sec"))) {
      continue;
    }
    if ((k === "added" || k === "dismissed") && Array.isArray(v) && v.length === 0) continue;
    if (k === "looks" && v && typeof v === "object") {
      const looks = Object.fromEntries(
        Object.entries(v as Record<string, HarnessIconLook>)
          .map(([id, look]) => [id, cleanLook(look)] as const)
          .filter((e): e is readonly [string, HarnessIconLook] => e[1] !== null),
      );
      if (Object.keys(looks).length) out[k] = looks;
      continue;
    }
    if ((k === "tweaks" || k === "lanes") && v && typeof v === "object" && Object.keys(v).length === 0) continue;
    out[k] = v;
  }
  return out;
}

/** A look with only what it sets; null when it sets nothing. */
function cleanLook(look: HarnessIconLook | null | undefined): HarnessIconLook | null {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(look ?? {})) if (v !== null && v !== undefined && v !== "") out[k] = v;
  return Object.keys(out).length ? (out as HarnessIconLook) : null;
}

/** An icon element as a draft may hold it (no `resolved`, no defaults). */
export function cleanIconElement(el: HarnessElement): HarnessElement {
  return el.kind === "icon" ? { ...el, props: cleanIconProps(el.props) } : el;
}

/** Every icon edit goes through here: the element (made when missing),
 *  its props changed by `fn`, `resolved` dropped — and `keep` too (an icon
 *  edited is planned again; only keepIcons sets it) — and the recipe is the
 *  user's own (not the preset) from now on. */
export function withIcons(
  r: HarnessRecipe,
  fn: (p: ReturnType<typeof iconProps>) => Partial<HarnessIconProps> | null,
  patch: Partial<Pick<HarnessElement, "enabled" | "placement" | "animation">> = {},
): HarnessRecipe {
  const el = iconsOf(r) ?? startingIcons();
  const changed = fn(iconProps(el));
  const { keep: _kept, ...own } = el.props;
  const props = cleanIconProps({ ...own, ...(changed ?? {}) });
  const next: HarnessElement = {
    ...el,
    ...patch,
    placement: patch.placement ?? el.placement,
    animation: patch.animation ?? el.animation,
    props,
  };
  const has = r.elements.some((e) => e.id === el.id);
  const elements = has ? r.elements.map((e) => (e.id === el.id ? next : e)) : [...r.elements, next];
  const out = { ...r, preset: null, elements };
  return recipeKey(out.elements) === recipeKey(r.elements) ? r : out;
}

/** The version whose icons the draft keeps exactly as it froze them
 *  ("Keep v3's icons"), or null. */
export function keptFrom(r: HarnessRecipe | null | undefined): number | null {
  const k = iconsOf(r)?.props.keep;
  return typeof k === "number" && k > 0 ? k : null;
}

/** "Keep v3's icons": the draft shows version `versionId`'s icons exactly as
 *  it froze them (the server plans nothing) — until an icon is edited. */
export function keepIcons(r: HarnessRecipe, versionId: number): HarnessRecipe {
  return withIcons(r, () => ({ keep: versionId }));
}

export function setIconsEnabled(r: HarnessRecipe, on: boolean): HarnessRecipe {
  return withIcons(r, () => null, { enabled: on });
}

/** "Icons from what they say": the AI picks them (on), or only the ones
 *  the user adds, with the keyword finds offered as suggestions (off). */
export function setIconsAi(r: HarnessRecipe, on: boolean): HarnessRecipe {
  return withIcons(r, () => ({ ai: on }));
}

export function setIconStyle(r: HarnessRecipe, style: HarnessIconStyle): HarnessRecipe {
  return withIcons(r, () => ({ style }));
}

/** The style icon `id` is asked to show with: its own, else the scene's. */
export function styleOf(r: HarnessRecipe | null | undefined, id: string): HarnessIconStyle {
  const p = iconProps(iconsOf(r));
  return p.looks[id]?.style ?? p.style;
}

/** The draft uses one of the older looks (the badge, the bare glyph):
 *  those stay on offer while it does. */
export function usesOldStyle(r: HarnessRecipe | null | undefined): boolean {
  const p = iconProps(iconsOf(r));
  const old = (s: string | null | undefined) => !!s && (OLD_STYLES as readonly string[]).includes(s);
  return old(p.style) || Object.values(p.looks).some((l) => old(l?.style));
}

/** "auto" or #rrggbb. */
export function setIconColour(r: HarnessRecipe, colour: string): HarnessRecipe {
  return withIcons(r, () => ({ colour: colour === "auto" ? "auto" : colour.toLowerCase() }));
}

/** The placement with `patch` applied, a null in it meaning "the default":
 *  the key is dropped, so setting a control back gives the same recipe. */
function placementWith(
  p: HarnessElement["placement"],
  patch: Partial<HarnessElement["placement"]>,
): HarnessElement["placement"] {
  const out: Record<string, unknown> = { ...p };
  for (const [k, v] of Object.entries(patch)) {
    if (v == null) delete out[k];
    else out[k] = v;
  }
  return out as HarnessElement["placement"];
}

export function setIconSize(r: HarnessRecipe, size: IconSize): HarnessRecipe {
  const el = iconsOf(r) ?? startingIcons();
  return withIcons(r, () => null, { placement: placementWith(el.placement, { size: size === "M" ? null : ICON_SIZES[size] }) });
}

/** Where: "auto" (the AI's home spot) or a side the scene has room on. A
 *  new side drops every dragged box: they belonged to the old one. */
export function setIconWhere(r: HarnessRecipe, anchor: string): HarnessRecipe {
  const el = iconsOf(r) ?? startingIcons();
  return withIcons(
    r,
    (p) => ({ tweaks: mapTweaks(p.tweaks, (t) => ({ ...t, box: null })) }),
    { placement: placementWith(el.placement, { anchor, box: null }) },
  );
}

/** Glitch (the default, written "auto"), pop, fade, slide or none. */
export function setIconEntrance(r: HarnessRecipe, e: IconEntrance): HarnessRecipe {
  const el = iconsOf(r) ?? startingIcons();
  return withIcons(r, () => null, { animation: { ...el.animation, entrance: e === "glitch" ? "auto" : e } });
}

/** keep: a version made with Omni's own icons keeps them, and ours are off;
 *  hide: Omni is told to draw none, and ours are stamped. */
export function setOmniOwn(r: HarnessRecipe, own: "hide" | "keep"): HarnessRecipe {
  return withIcons(r, () => ({ omni_own: own }));
}

function mapTweaks(
  tweaks: Record<string, HarnessIconTweak>,
  fn: (t: HarnessIconTweak) => HarnessIconTweak,
): Record<string, HarnessIconTweak> {
  const out: Record<string, HarnessIconTweak> = {};
  for (const [k, t] of Object.entries(tweaks)) {
    const next = cleanTweak(fn(t));
    if (next) out[k] = next;
  }
  return out;
}

/** A tweak with only what it pins; null when it pins nothing. */
function cleanTweak(t: HarnessIconTweak): HarnessIconTweak | null {
  const out: HarnessIconTweak = {};
  if (t.off_word) {
    out.off_word = true;
    if (t.sec != null) out.sec = round2(t.sec);
  } else if (t.w != null) {
    out.w = t.w;
    if (t.words_digest) out.words_digest = t.words_digest;
  }
  if (t.box) out.box = t.box;
  if (t.hold_sec != null) out.hold_sec = round2(t.hold_sec);
  return Object.keys(out).length ? out : null;
}

function round2(x: number): number {
  return Math.round(x * 100) / 100;
}

/** One icon's pin, merged into what it had. At most eight icons carry one
 *  (every icon a scene can list: five of the user's, three of the AI's), so
 *  a pin never pushes another out: a removed icon's stale pin goes first,
 *  and a pin past the limit even then is not made (the recipe is returned
 *  as it was) rather than losing one the user set. */
export function setTweak(r: HarnessRecipe, id: string, patch: HarnessIconTweak): HarnessRecipe {
  const p = iconProps(iconsOf(r));
  const tweaks = { ...p.tweaks };
  const next = cleanTweak({ ...(tweaks[id] ?? {}), ...patch });
  delete tweaks[id];
  if (next) tweaks[id] = next;
  if (Object.keys(tweaks).length > TWEAKS_MAX) {
    for (const k of Object.keys(tweaks)) if (k !== id && p.dismissed.includes(k)) delete tweaks[k];
    if (Object.keys(tweaks).length > TWEAKS_MAX) return r;
  }
  return withIcons(r, () => ({ tweaks }));
}

/** Pins the server worked out (a reorder), merged icon by icon. */
export function withTweaks(r: HarnessRecipe, tweaks: Record<string, HarnessIconTweak> | null | undefined): HarnessRecipe {
  let next = r;
  for (const [id, t] of Object.entries(tweaks ?? {})) next = setTweak(next, id, t);
  return next;
}

/** Pin an icon to word `w` (a mention of its idea, or a word the user
 *  picked for an icon that wasn't tied to any). */
export function pinToWord(r: HarnessRecipe, id: string, w: number, digest: string | null): HarnessRecipe {
  return setTweak(r, id, { w, words_digest: digest, off_word: false, sec: null });
}

/** The user's explicit choice: shown at `sec`, off its word. */
export function pinOffWord(r: HarnessRecipe, id: string, sec: number): HarnessRecipe {
  return setTweak(r, id, { off_word: true, sec, w: null, words_digest: null });
}

/** How long it stays fully on screen: a length set by hand, 0.2 to 30 s. */
export function setHold(r: HarnessRecipe, id: string, hold: number, rules: HarnessIconRules = DEFAULT_RULES): HarnessRecipe {
  return setTweak(r, id, { hold_sec: clampHold(hold, rules) });
}

function clampHold(hold: number, rules: HarnessIconRules): number {
  return clamp(hold, rules.user_hold_min ?? 0.2, rules.user_hold_max ?? 30);
}

// --- by hand: on the picture, and on the lane ----------------------------------

/** The box the user put icon `id` in on the picture (its body), or null. */
export function userBoxOf(r: HarnessRecipe | null | undefined, id: string): HarnessBox | null {
  return iconProps(iconsOf(r)).tweaks[id]?.box ?? null;
}

/** Icon `id` put in `box` by hand (frame fractions, its body; a chip's
 *  height counts — its width is its words'): kept inside the frame, to the
 *  ten-thousandth, and the planner keeps it exactly there, over the speaker
 *  too. `size` (a share of the frame's short side), when the box was
 *  resized: its own size as well, so "Back to automatic" keeps it (null,
 *  or the scene's: the scene's). */
export function setBox(r: HarnessRecipe, id: string, box: Box, size?: number | null): HarnessRecipe {
  const kept = roundBox(insideFrame(box));
  let next = setTweak(r, id, { box: kept });
  if (size !== undefined) {
    const scene = iconsOf(next)?.placement.size ?? ICON_SIZES.M;
    const s = size == null ? null : Math.round(clamp(size, ICON_SIZE_RANGE[0], ICON_SIZE_RANGE[1]) * 1000) / 1000;
    next = setLook(next, id, { size: s == null || Math.abs(s - scene) < 1e-9 ? null : s });
  }
  return next;
}

/** The size (a share of the frame's short side) of an icon whose body is
 *  `box` on a `frame` (px): a square's side, or a chip's height over its
 *  share of the square (rules.chip_h). */
export function sizeOfBox(box: Box, chip: boolean, frame: { w: number; h: number }, rules: HarnessIconRules = DEFAULT_RULES): number {
  const short = Math.min(frame.w, frame.h);
  const side = chip ? (box.h * frame.h) / (rules.chip_h ?? 0.74) : Math.min(box.w * frame.w, box.h * frame.h);
  return side / Math.max(1, short);
}

/** `box` at `size` about its centre (a chip keeps its shape), inside the
 *  frame: the panel's size for an icon placed by hand. */
export function resizedBox(box: Box, size: number, chip: boolean, frame: { w: number; h: number }, rules: HarnessIconRules = DEFAULT_RULES): Box {
  const k = size / Math.max(1e-6, sizeOfBox(box, chip, frame, rules));
  const w = box.w * k;
  const h = box.h * k;
  return roundBox(insideFrame({ x: box.x + box.w / 2 - w / 2, y: box.y + box.h / 2 - h / 2, w, h }));
}

/** Where icon `item`'s settle may snap to ("Snap to words"): each mention of
 *  its own idea, and the word it is on — just before each word, as the
 *  planner lands it. `words`: the timeline's. */
export function snapPoints(
  item: Pick<HarnessIconItem, "mentions" | "w">,
  words: { w: number; s: number | null }[] | null | undefined,
  rules: HarnessIconRules = DEFAULT_RULES,
): { at: number; w: number }[] {
  const out = new Map<number, number>();
  for (const m of item.mentions ?? []) out.set(m.w, settleAt(m.at, rules));
  const own = item.w != null ? (words ?? []).find((x) => x.w === item.w) : undefined;
  if (own && own.s != null && !out.has(own.w)) out.set(own.w, settleAt(own.s, rules));
  return [...out.entries()].map(([w, at]) => ({ w, at })).sort((a, b) => a.at - b.at);
}

/** A lane block let go (SpanBlock): moved, or its start or end trimmed.
 *   move / start  snapped onto a word of its own idea (`snappedW`): pinned
 *                 there; else timed by hand, exactly where it was put (off
 *                 its word). A move keeps its length; a start trim keeps
 *                 where it ends.
 *   end           its length (0.2 to 30 s). One the AI timed is pinned to
 *                 its word as well, so the length it was given keeps its
 *                 place ahead of the AI's next pick.
 *  `span`: the block (from its entrance to gone). */
export function timeByHand(
  r: HarnessRecipe,
  plan: HarnessIconPlan,
  item: HarnessIconItem,
  span: Span,
  kind: SpanDrag,
  snappedW: number | null,
  rules: HarnessIconRules = DEFAULT_RULES,
): HarnessRecipe {
  const id = glyphId(item.glyph);
  const digest = wordsDigestOf(plan);
  const outS = outSec(rules);
  if (kind === "end") {
    let next = r;
    if (!item.pinned && !item.off_word && item.w != null) next = pinToWord(next, id, item.w, digest);
    return setHold(next, id, span.to - outS - item.settle, rules);
  }
  const { sec, hold_sec } = iconSpanTweak(span, rules);
  const hold = kind === "start" ? hold_sec : item.hold ?? item.to - item.settle - outS;
  if (snappedW != null) {
    // On its word, the planner keeps it out of the scene's last moments: a
    // length that would run into them is cut to fit (else it could not show).
    const seconds = plan.timeline?.seconds;
    const fits = seconds ? Math.min(hold, seconds - rules.tail_clear - outS - sec) : hold;
    let next = pinToWord(r, id, snappedW, digest);
    if (kind === "start" || iconProps(iconsOf(r)).tweaks[id]?.hold_sec != null) next = setHold(next, id, fits, rules);
    return next;
  }
  return setSpan(r, id, kind === "start" ? span : { from: span.from, to: span.from + inSec(rules) + hold + outS }, rules);
}

/** Icon `id` timed by hand to exactly `span` (its block: from its entrance
 *  to gone), off its word: the planner keeps it there (a fixed slot). */
export function setSpan(r: HarnessRecipe, id: string, span: Span, rules: HarnessIconRules = DEFAULT_RULES): HarnessRecipe {
  const { sec, hold_sec } = iconSpanTweak(span, rules);
  return setHold(pinOffWord(r, id, sec), id, hold_sec, rules);
}

/** "Snap back": an icon timed by hand goes back on its word (the AI's
 *  timing again; its length and place stay). */
export function snapBack(r: HarnessRecipe, id: string): HarnessRecipe {
  return setTweak(r, id, { off_word: false, sec: null });
}

/** "Auto ↺": every icon's timing back to the AI (lengths and dragged boxes
 *  stay); the tracks the user picked too. */
export function resetOrder(r: HarnessRecipe): HarnessRecipe {
  return withIcons(r, (p) => ({
    tweaks: mapTweaks(p.tweaks, (t) => ({ box: t.box ?? null, hold_sec: t.hold_sec ?? null })),
    lanes: {},
  }));
}

/** Timing pins exist (so "Auto ↺" has something to do): a word, a time or
 *  a track the user picked. */
export function hasTimingPins(r: HarnessRecipe): boolean {
  const p = iconProps(iconsOf(r));
  return Object.values(p.tweaks).some((t) => t.off_word || t.w != null) || Object.keys(p.lanes).length > 0;
}

// --- tracks ------------------------------------------------------------------------

/** An item's track on the timeline (0, the AI's, when the plan doesn't say). */
export function laneOf(item: Pick<HarnessIconItem, "lane"> | null | undefined): number {
  const n = item?.lane;
  return typeof n === "number" && n > 0 ? Math.floor(n) : 0;
}

/** The track the user put icon `id` on (null: none, the planner's). */
export function laneSetOf(r: HarnessRecipe | null | undefined, id: string): number | null {
  const n = iconProps(iconsOf(r)).lanes[id];
  return typeof n === "number" ? n : null;
}

/** Icon `id` dragged to track `lane` on the timeline: 1 and up, one of its
 *  own (it shows with the others there, and is the user's from then on);
 *  0, back on the AI's (one at a time): no pin at all, as the planner puts
 *  one there whenever it has room. At most eight icons carry one: past
 *  that the recipe is returned as it was (none is pushed out). */
export function setLane(r: HarnessRecipe, id: string, lane: number): HarnessRecipe {
  const p = iconProps(iconsOf(r));
  const lanes = { ...p.lanes };
  delete lanes[id];
  const n = Math.min(LANE_MAX, Math.max(0, Math.round(lane)));
  if (n > 0) lanes[id] = n;
  if (Object.keys(lanes).length > LANES_MAX) return r;
  return withIcons(r, () => ({ lanes }));
}

/** Add an icon. It lands on its own mention when it is said (the server
 *  finds it); `pin` puts it on a given word instead. `at`: the moment it is
 *  added at (the playhead, scene seconds): one nothing said is tied to
 *  shows at the clear time nearest it. An icon the user removed comes back
 *  as theirs (off the removed list, onto the added one: the two never share
 *  an icon). */
export function addIcon(
  r: HarnessRecipe,
  glyph: HarnessIconGlyph,
  label: string,
  pin?: { w: number; digest: string | null } | null,
  at?: number | null,
): HarnessRecipe {
  const id = glyphId(glyph);
  const p = iconProps(iconsOf(r));
  let next = r;
  if (!p.added.some((a) => glyphId(a.glyph) === id)) {
    if (p.added.length >= ADDED_MAX) return r;
    const add: HarnessIconAdd = { glyph: { source: glyph.source, id }, label: label.slice(0, 40) };
    if (typeof at === "number" && Number.isFinite(at) && at >= 0) add.at = Math.min(600, round2(at));
    next = withIcons(r, (q) => ({ added: [...q.added, add], dismissed: q.dismissed.filter((x) => x !== id) }));
  }
  return pin ? pinToWord(next, id, pin.w, pin.digest) : next;
}

/** Remove a shown icon, whoever chose it: it goes off the added list AND
 *  onto the removed one, so an icon the AI picks too (or picks later) stays
 *  gone — in Add icon → Suggested as "Removed by you", a tap brings it
 *  back. Its pins (its word, time and track) go too. */
export function removeIcon(r: HarnessRecipe, glyph: HarnessIconGlyph | string): HarnessRecipe {
  const id = glyphId(glyph);
  return withIcons(r, (p) => {
    const tweaks = { ...p.tweaks };
    delete tweaks[id];
    const lanes = { ...p.lanes };
    delete lanes[id];
    const dismissed = p.dismissed.includes(id) ? p.dismissed : [...p.dismissed, id].slice(-DISMISSED_MAX);
    return { added: p.added.filter((a) => glyphId(a.glyph) !== id), dismissed, tweaks, lanes };
  });
}

/** Undo a removal of one of the AI's own picks: it may show it again
 *  (where it has room). */
export function addBack(r: HarnessRecipe, id: string): HarnessRecipe {
  return withIcons(r, (p) => ({ dismissed: p.dismissed.filter((x) => x !== id) }));
}

/** A removed idea tapped in Suggested: the AI's own pick is simply undismissed;
 *  any other (one the user had added, a suggestion) comes back as added. */
export function restoreIdea(r: HarnessRecipe, idea: Pick<HarnessIconIdea, "glyph" | "label" | "auto">): HarnessRecipe {
  const id = glyphId(idea.glyph);
  return idea.auto ? addBack(r, id) : addIcon(r, idea.glyph, idea.label);
}

// --- one icon's own look ----------------------------------------------------------

/** Icon `id`'s own look ({} when it takes the scene's). */
export function lookOf(r: HarnessRecipe | null | undefined, id: string): HarnessIconLook {
  return iconProps(iconsOf(r)).looks[id] ?? {};
}

/** Icon `id`'s look with `patch` applied: a null (or "") deletes that key,
 *  a look left empty is dropped, and at most eight icons keep one (the
 *  oldest goes first). Colours are written in lower case. */
export function setLook(r: HarnessRecipe, id: string, patch: { [K in keyof HarnessIconLook]?: HarnessIconLook[K] | null }): HarnessRecipe {
  return withIcons(r, (p) => {
    const looks = { ...p.looks };
    const next: Record<string, unknown> = { ...(looks[id] ?? {}) };
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === undefined || v === "") delete next[k];
      else next[k] = (k === "colour" || k === "ink") && typeof v === "string" ? v.toLowerCase() : v;
    }
    delete looks[id];
    const clean = cleanLook(next as HarnessIconLook);
    if (clean) looks[id] = clean;
    const keys = Object.keys(looks);
    while (keys.length > LOOKS_MAX) delete looks[keys.shift()!];
    return { looks };
  });
}

/** "Reset this icon": its own look and its pins (its track too) go: the
 *  AI's again. */
export function resetIcon(r: HarnessRecipe, id: string): HarnessRecipe {
  return withIcons(r, (p) => {
    const looks = { ...p.looks };
    const tweaks = { ...p.tweaks };
    const lanes = { ...p.lanes };
    delete looks[id];
    delete tweaks[id];
    delete lanes[id];
    return { looks, tweaks, lanes };
  });
}

/** "Reset all to auto": every icon's own look and pins (and tracks) go;
 *  what was added or removed stays. */
export function resetAllIcons(r: HarnessRecipe): HarnessRecipe {
  return withIcons(r, () => ({ looks: {}, tweaks: {}, lanes: {} }));
}

/** Some icon has a look of its own, a pin or a track ("Reset all" has work). */
export function hasIconEdits(r: HarnessRecipe | null | undefined): boolean {
  const p = iconProps(iconsOf(r));
  return Object.keys(p.looks).length > 0 || Object.keys(p.tweaks).length > 0 || Object.keys(p.lanes).length > 0;
}

/** Everything that decides how icon `id` looks in `r` — the scene's and its
 *  own together — as one string: two recipes that give the same draw it the
 *  same, so a plan made for one shows its exact sprite for the other. */
export function lookInputs(r: HarnessRecipe | null | undefined, id: string, opts: { box?: boolean } = {}): string {
  const el = iconsOf(r);
  const p = iconProps(el);
  const own = p.looks[id] ?? {};
  // Without the box: the same look, wherever the user has put it.
  const box = opts.box === false ? null : p.tweaks[id]?.box ?? null;
  return JSON.stringify([
    own.style ?? p.style,
    own.colour ?? p.colour,
    own.ink ?? null,
    own.label ?? null,
    own.size ?? el?.placement.size ?? null,
    own.layer ?? "behind",
    own.enter ?? el?.animation.entrance ?? "auto",
    own.leave ?? null,
    box,
  ]);
}

/** Change an icon for another: the new one takes its word (pinned there,
 *  so it keeps the meaning it was given), the moment the old one was added
 *  at (IconAdd.at: a never-said add stays there, not at the earliest clear
 *  time) and its look, the old one is removed. */
export function swapIcon(
  r: HarnessRecipe,
  item: Pick<HarnessIconItem, "glyph" | "w" | "off_word">,
  glyph: HarnessIconGlyph,
  label: string,
  digest: string | null,
): HarnessRecipe {
  if (glyphId(item.glyph) === glyphId(glyph)) return r;
  const props = iconProps(iconsOf(r));
  const tweak = props.tweaks[glyphId(item.glyph)] ?? null;
  const at = props.added.find((a) => glyphId(a.glyph) === glyphId(item.glyph))?.at ?? null;
  let next = removeIcon(r, item.glyph);
  next = addIcon(next, glyph, label, null, at);
  if (tweak?.off_word && tweak.sec != null) next = pinOffWord(next, glyphId(glyph), tweak.sec);
  else if (item.w != null) next = pinToWord(next, glyphId(glyph), item.w, digest);
  if (tweak?.hold_sec != null) next = setTweak(next, glyphId(glyph), { hold_sec: tweak.hold_sec });
  const lane = laneSetOf(r, glyphId(item.glyph));
  if (lane != null) next = setLane(next, glyphId(glyph), lane);
  const look = lookOf(r, glyphId(item.glyph));
  if (Object.keys(look).length) {
    next = withIcons(next, (p) => {
      const looks = { ...p.looks };
      delete looks[glyphId(item.glyph)];
      return { looks };
    });
    next = setLook(next, glyphId(glyph), look);
  }
  return next;
}

// --- order and time --------------------------------------------------------------

function clamp(x: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, x));
}

const inSec = (k: HarnessIconRules) => k.in_f / k.fps;
const outSec = (k: HarnessIconRules) => k.out_f / k.fps;
const landSec = (k: HarnessIconRules) => k.land_f / k.fps;

/** An icon's settle time were it anchored on a word said at `at`. */
export function settleAt(at: number, rules: HarnessIconRules = DEFAULT_RULES): number {
  return at - landSec(rules);
}

/** A loser of a clash is shifted up to this much before it is dropped (the
 *  scheduler's rule), so a move that crowds a neighbour by less than this
 *  still gives the order asked for. */
const SHIFT_MAX = 0.6;

/** The earliest and latest an icon may settle on this scene: nothing on
 *  screen in the opening, the first settle once the pattern has come in (a
 *  no-icon zone from 0), and gone before the end (a zone to the end). */
function bounds(plan: HarnessIconPlan, rules: HarnessIconRules): { first: number; last: number } {
  const zones = plan.timeline?.zones ?? [];
  const seconds = plan.timeline?.seconds ?? Math.max(0, ...plan.items.map((i) => i.to + rules.tail_clear));
  const first = Math.max(rules.head_clear + inSec(rules), ...zones.filter((z) => z.from <= 1e-3).map((z) => z.to));
  const end = Math.min(seconds - rules.tail_clear, ...zones.filter((z) => z.to >= seconds - 1e-3).map((z) => z.from));
  return { first, last: end - rules.hold_min - outSec(rules) };
}

function fits(settle: number, b: { first: number; last: number }): boolean {
  return settle >= b.first - 1e-6 && settle <= b.last + 1e-6;
}

export type ReorderResult =
  | { kind: "none" }
  /** Re-anchored to other mentions of the same ideas: meaning kept. */
  | { kind: "moved"; recipe: HarnessRecipe; note: string }
  /** Not said anywhere that fits: the user decides. `anyway` shows `item`
   *  at `sec`, off its word (null: there is no room for even that). */
  | {
      kind: "choice";
      item: HarnessIconItem;
      other: HarnessIconItem;
      /** "before": item asked to come before `other`; "after": after. */
      side: "before" | "after";
      sec: number | null;
      anyway: HarnessRecipe | null;
    };

/** Move the icon at `from` (speech order) to `to`, by meaning, never by
 *  swapping slots:
 *   1. it is said again where it fits → it moves to that mention (the
 *      nearest one: the smallest move);
 *   2. else every icon it jumps over is said again on its other side →
 *      those move to their mentions;
 *   3. else → "choice": "'Code' is only said at 4.7 s, after 'Free'" —
 *      [Show Code first anyway · off its word] / [Keep speech order].
 *  The server works the same out by planning it (IconsTab asks it first);
 *  this is the studio's own reckoning when it can't. `from` and `to` are
 *  places in track 0's order (trackOrder: the AI's track, one at a time);
 *  an icon on a track of its own is moved on the timeline instead. */
export function reorder(
  r: HarnessRecipe,
  plan: HarnessIconPlan,
  from: number,
  to: number,
  rules: HarnessIconRules = DEFAULT_RULES,
): ReorderResult {
  const items = trackOrder(plan.items);
  if (from === to || from < 0 || to < 0 || from >= items.length || to >= items.length) return { kind: "none" };
  const item = items[from];
  const digest = wordsDigestOf(plan);
  const b = bounds(plan, rules);
  const body = rules.hold_min + outSec(rules);
  const s = (m: { at: number }) => settleAt(m.at, rules);

  if (to < from) {
    // Earlier: before `other`, and before everything between them.
    const other = items[to];
    const jumped = items.slice(to, from);
    const mine = item.mentions
      .filter((m) => m.w !== item.w && fits(s(m), b) && s(m) + body + rules.gap_user <= other.from + SHIFT_MAX)
      .sort((x, y) => y.at - x.at);
    if (mine[0]) {
      return {
        kind: "moved",
        recipe: pinToWord(r, glyphId(item.glyph), mine[0].w, digest),
        note: `${item.label} now shows first — on its earlier mention at ${fmtSec(mine[0].at)}`,
      };
    }
    const later = jumped.map(
      (j) =>
        j.mentions
          .filter((m) => m.w !== j.w && fits(s(m), b) && s(m) >= item.to + rules.gap_user + inSec(rules) - SHIFT_MAX)
          .sort((x, y) => x.at - y.at)[0],
    );
    if (later.every(Boolean)) {
      let next = r;
      jumped.forEach((j, i) => (next = pinToWord(next, glyphId(j.glyph), later[i]!.w, digest)));
      return {
        kind: "moved",
        recipe: next,
        note: `${jumped.map((j) => j.label).join(" and ")} now ${jumped.length > 1 ? "show" : "shows"} later — on ${jumped.length > 1 ? "their" : "its"} later mention`,
      };
    }
    // The earliest it fits in its new place: after the icon now before it.
    const before = to > 0 ? items[to - 1] : null;
    const sec = round2(Math.max(b.first, before ? before.to + rules.gap_user + inSec(rules) : 0));
    const ok = fits(sec, b) && sec < other.settle;
    return { kind: "choice", item, other, side: "before", sec: ok ? sec : null, anyway: ok ? pinOffWord(r, glyphId(item.glyph), sec) : null };
  }

  // Later: after `other`, and after everything between them.
  const other = items[to];
  const jumped = items.slice(from + 1, to + 1);
  const mine = item.mentions
    .filter((m) => m.w !== item.w && fits(s(m), b) && s(m) >= other.to + rules.gap_user + inSec(rules) - SHIFT_MAX)
    .sort((x, y) => x.at - y.at);
  if (mine[0]) {
    return {
      kind: "moved",
      recipe: pinToWord(r, glyphId(item.glyph), mine[0].w, digest),
      note: `${item.label} now shows later — on its mention at ${fmtSec(mine[0].at)}`,
    };
  }
  const earlier = jumped.map(
    (j) =>
      j.mentions
        .filter((m) => m.w !== j.w && fits(s(m), b) && s(m) + body + rules.gap_user <= item.from + SHIFT_MAX)
        .sort((x, y) => y.at - x.at)[0],
  );
  if (earlier.every(Boolean)) {
    let next = r;
    jumped.forEach((j, i) => (next = pinToWord(next, glyphId(j.glyph), earlier[i]!.w, digest)));
    return {
      kind: "moved",
      recipe: next,
      note: `${jumped.map((j) => j.label).join(" and ")} now ${jumped.length > 1 ? "show" : "shows"} first — on ${jumped.length > 1 ? "their" : "its"} earlier mention`,
    };
  }
  const sec = round2(other.to + rules.gap_user + inSec(rules));
  const ok = fits(sec, b);
  return { kind: "choice", item, other, side: "after", sec: ok ? sec : null, anyway: ok ? pinOffWord(r, glyphId(item.glyph), sec) : null };
}

/** The keyboard's ←/→ on a lane block: the previous / next mention of its
 *  own idea (null: it is said just once that way). */
export function stepMention(
  r: HarnessRecipe,
  plan: HarnessIconPlan,
  item: HarnessIconItem,
  dir: -1 | 1,
): { recipe: HarnessRecipe; at: number } | null {
  const ms = [...item.mentions].sort((a, b) => a.at - b.at);
  const here = ms.findIndex((m) => m.w === item.w);
  const pick =
    here >= 0
      ? ms[here + dir]
      : dir > 0
        ? ms.find((m) => m.at > item.settle)
        : [...ms].reverse().find((m) => m.at < item.settle);
  if (!pick) return null;
  return { recipe: pinToWord(r, glyphId(item.glyph), pick.w, wordsDigestOf(plan)), at: pick.at };
}

/** "3.3 s". */
export function fmtSec(t: number): string {
  return `${(Math.round(t * 10) / 10).toFixed(1)} s`;
}

// --- one time per icon ------------------------------------------------------------

/** The one time an icon shows at, wherever the studio says it (the list,
 *  the Moments chips, the notes, the mark on its block): when it has
 *  settled, fully in. Its block starts earlier, at its entrance (`from`:
 *  settleMark), and a click rests the stage a little later (momentOf). */
export function iconTime(item: Pick<HarnessIconItem, "settle">): number {
  return item.settle;
}

/** The time an idea says in a list (Add icon's Suggested, and yours not on
 *  screen), by the same rule as the icons: shown (an item of the plan has
 *  its glyph), the one time its icon shows at (iconTime: when it settles,
 *  as the list says it); not placed, when its word is said (`said`: shown
 *  as "said at 6.0 s"). Null: neither (never said, and not shown). */
export function ideaTime(
  idea: Pick<HarnessIconIdea, "glyph" | "at">,
  items: readonly Pick<HarnessIconItem, "glyph" | "settle">[] | null | undefined,
): { t: number; said: boolean } | null {
  const id = glyphId(idea.glyph);
  const shown = (items ?? []).find((it) => glyphId(it.glyph) === id);
  if (shown) return { t: iconTime(shown), said: false };
  return typeof idea.at === "number" ? { t: idea.at, said: true } : null;
}

/** Where the stage rests for an icon (a click on its chip or its block):
 *  its exact still, a moment after it settles, while it is still on screen. */
export function momentOf(item: Pick<HarnessIconItem, "settle" | "to" | "moment_t">): number {
  return Math.min(item.moment_t ?? item.settle + 0.4, item.to - 0.05);
}

/** How far into its block (s, from its entrance) an icon settles: where its
 *  block marks the time the studio says it shows at. */
export function settleMark(item: Pick<HarnessIconItem, "from" | "settle" | "to">): number {
  return Math.min(Math.max(0, item.settle - item.from), Math.max(0, item.to - item.from));
}

/** "Show at Y s anyway" for one of yours moved off its word or its moment
 *  (the item's `shifted`): the time it offers (`label`) and the time it is
 *  timed by hand at, off its word (`sec`). Added at a moment (the
 *  playhead): that moment, both; else the word, when it is said (labelled)
 *  and the settle that gives (pinned). */
export function anywayOf(shift: Pick<HarnessIconShift, "at" | "said" | "asked">): { label: number; sec: number } {
  if (typeof shift.asked === "number") return { label: shift.asked, sec: shift.asked };
  return { label: shift.said ?? shift.at, sec: shift.at };
}

/** Who an icon in the list is (the chip after its time): off its word; the
 *  AI's pick kept by you; your team's; yours (added, even an icon the AI
 *  picked too); a version's kept; pinned; a keyword match; the AI's. */
export type IconTag = "offWord" | "keptPick" | "teamTag" | "yours" | "kept" | "pinned" | "keyword" | "ai";

export function iconTag(item: Pick<HarnessIconItem, "off_word" | "source" | "pinned" | "why">): IconTag {
  if (item.off_word) return "offWord";
  if (item.why?.by === "kept") return "keptPick";
  if (item.why?.by === "team") return "teamTag";
  if (item.source === "user") return "yours";
  if (item.source === "kept") return "kept";
  if (item.pinned) return "pinned";
  return item.source === "keyword" ? "keyword" : "ai";
}

// --- a version's frozen icons ------------------------------------------------------

/** One icon as a version froze it at Generate (props.resolved: recipe.py
 *  IconResolved), as far as these checks read it. */
interface FrozenIcon {
  glyph?: { id?: string } | null;
  from_sec?: number;
  settle_sec?: number;
  to_sec?: number;
  box?: { x?: number; y?: number; w?: number; h?: number } | null;
  fill?: string;
  ink?: string;
  /** Unset: the badge; the others as drawn (text: a chip's words). */
  style?: string;
  text?: string | null;
  layer?: string;
}

const SAME_SEC = 0.02;
const SAME_BOX = 0.002;

/** Whether a plan shows other icons than a version froze (`resolved`, its
 *  icon element's props.resolved): another icon, count or order, another
 *  moment, place, colour or look (style, words, in front). None frozen and
 *  none planned are the same. What "Try again" asks about before placing
 *  them fresh. */
export function iconsDiffer(plan: Pick<HarnessIconPlan, "items" | "colours">, resolved: unknown): boolean {
  const items = plan.items ?? [];
  const was = (Array.isArray(resolved) ? resolved : []) as FrozenIcon[];
  if (items.length !== was.length) return true;
  const near = (a: number | undefined, b: number | undefined, tol: number) =>
    typeof a === "number" && typeof b === "number" && Math.abs(a - b) <= tol;
  const sameHex = (a: string | undefined | null, b: string | undefined) =>
    !a || !b || a.toLowerCase() === b.toLowerCase();
  return items.some((it, i) => {
    const r = was[i];
    if (!r || it.glyph.id !== r.glyph?.id) return true;
    if (!near(it.from, r.from_sec, SAME_SEC) || !near(it.settle, r.settle_sec, SAME_SEC) || !near(it.to, r.to_sec, SAME_SEC)) {
      return true;
    }
    const b = r.box ?? {};
    if (!near(it.box.x, b.x, SAME_BOX) || !near(it.box.y, b.y, SAME_BOX) || !near(it.box.w, b.w, SAME_BOX) || !near(it.box.h, b.h, SAME_BOX)) {
      return true;
    }
    if (!sameHex(it.fill ?? plan.colours?.fill, r.fill) || !sameHex(it.ink ?? plan.colours?.ink, r.ink)) return true;
    if ((it.style ?? "badge") !== (r.style ?? "badge") || (it.layer ?? "behind") !== (r.layer ?? "behind")) return true;
    return (it.text ?? null) !== (r.text ?? null);
  });
}

// --- colour ----------------------------------------------------------------------

function rgbOf(hex: string): [number, number, number] | null {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function luminance(hex: string): number {
  const c = rgbOf(hex);
  if (!c) return 0;
  const [r, g, b] = c.map((v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrast(a: string, b: string): number {
  const la = luminance(a);
  const lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** The ink for a fill the user picked, the way the server picks it: white,
 *  else charcoal when white is hard to read; on a white fill, the pattern's
 *  colour. (The next plan confirms it; this is what shows meanwhile.) */
export function inkFor(fill: string, pattern: string | null): string {
  if (luminance(fill) > 0.8) return pattern ?? "#7f2bf2";
  return contrast("#ffffff", fill) >= 3 ? "#ffffff" : "#303030";
}
