// Rebrand harness — typed client for the Rebrand studio.
//
// A rebrand is a RECIPE: a list of elements (today up to three brand patterns;
// logo, text, icons and brand shapes arrive milestone by milestone), each
// switched on or off, placed and given an entrance, plus clip-wide settings
// (colours, who writes Omni's opening, the guards). The studio edits a recipe,
// previews it for free, and generates a version: made in house (`engine`
// "house": our own graphics over the untouched footage, $0 — every new draft
// since 2026-09-29), or, for the versions made before, by Omni. The server is
// the source of truth for the recipe's shape: GET /schema and GET /catalogue
// describe it.
//
// A version is an ordinary Rebrand experiment (rebrandApi.get polls it) that
// also carries the recipe it ran. A version can be built on another (`base`):
// added on top of it (stack) or changed and redone from the original
// (rebuild); preview and generate then carry that base.
//
// Icons (one "icon" element, ≤3 icons, always behind the speaker) are
// stamped by us after Omni. Their plan — which icons, on which words, where
// and in what colours — is asked for on its own (POST /icons/plan), so it
// never waits on the pattern preview; Generate sends the plan's key, and a
// plan that moved on since gives a 409 carrying the fresh one.
//
// Every field a newer server adds is OPTIONAL here, so the studio degrades
// (falls back to what it had before) against a server that predates it.
//
// The scene's end (the end grip): every request that names a scene carries
// `end_frame` (the reel's 30 fps frames) once the user has set one — and
// only then, so a request without one is the very request it always was
// (SceneView; its key, viewKey, joins the studio's cache keys only then).
// Kept past the automatic cut, the scene is branded on its whole clip:
// every request then says `variant: "full"` (a GET: ?variant=full).

import type { RebrandExperiment, RebrandIntensity, RebrandPattern, RebrandPatternAnimation } from "./rebrandApi";
import type { Timing } from "./rebrandTiming";

export type HarnessKind = "pattern" | "logo" | "text" | "icon" | "slab" | "beads" | "plate";
export type HarnessAnchor = "auto" | "edge-left" | "edge-right" | "edge-top" | "edge-bottom" | "full";
export type HarnessColourSource = "intro" | "brand" | "custom";

/** A rectangle in frame fractions (0..1, top-left origin). */
export interface HarnessBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface HarnessElement {
  id: string;
  kind: HarnessKind;
  enabled: boolean;
  /** behind: the speaker stands in front of it; front: over everything. */
  z?: "behind" | "front" | null;
  exact?: boolean | null;
  /** A pattern moved by hand (the in-house engine): `offset` (the move,
   *  frame fractions, ±1), `size` (× the size it was placed at, 0.5-3;
   *  null = as placed), `rotate` (degrees clockwise, ±180) about the centre
   *  of `box` (its visible box when it was grabbed) — only with a pinned
   *  `props.layout`. An icon reads `anchor`, `size` (S/M/L) and `box`. */
  placement: {
    anchor: HarnessAnchor | string;
    region?: string;
    size?: number | null;
    margin?: number;
    box?: HarnessBox | null;
    rotate?: number;
    offset?: [number, number];
  };
  /** A pattern's time (the in-house engine), scene seconds: from `start`
   *  (clip_start = 0, or at_sec) to `end` (absent = the scene's end), in over
   *  `duration_sec`, out with `exit` over `exit_sec`. The keys are deleted at
   *  their defaults, as the server leaves them out (lib/rebrandTiming.ts). */
  animation: {
    /** "auto" = the element's own entrance. */
    entrance: string;
    start?: { mode: string; sec?: number | null; word?: string | null };
    duration_sec?: number;
    end?: { mode: "clip_end" | "at_sec"; sec?: number | null } | null;
    /** absent / "auto": none when it lasts to the scene's end, else its
     *  entrance played backwards. */
    exit?: HarnessPatternExit | null;
    exit_sec?: number | null;
  };
  /** Per kind. pattern: HarnessPatternProps; icon: HarnessIconProps. */
  props: Record<string, unknown>;
}

/** How a pattern goes out (recipe.PATTERN_EXITS). */
export type HarnessPatternExit =
  | "auto"
  | "none"
  | "fade"
  | "glitch"
  | "to_left"
  | "to_right"
  | "to_top"
  | "to_bottom"
  | "slide_out";

/** A pattern element's props. `resolved` is the router's, written at
 *  Generate (the pattern exactly as drawn): a draft never holds it, and one
 *  a client sends is dropped. */
export interface HarnessPatternProps {
  slug?: string;
  colour?: string;
  /** A pinned place (null: placed automatically). */
  layout?: HarnessPatternLayout | null;
  /** A short tag for "Another spot". */
  shuffle?: string | null;
  /** The in-house engine: follow the camera's push-in ("room"), hold still on
   *  screen ("screen"), or pick ("auto", the same as absent). */
  follow?: "auto" | "room" | "screen";
  resolved?: unknown;
}

/** Who draws a recipe: "omni" (the paid video model, every version before
 *  2026-09-29) or "house" (our own renderer, $0 — every new draft). */
export type HarnessEngine = "omni" | "house";

export interface HarnessRecipe {
  recipe_version: 1;
  engine: HarnessEngine;
  target: string | null;
  preset: string | null;
  /** An auto-placed pattern draws its place from this: the preview and the
   *  run agree because both use it. */
  seed: string | null;
  colours: { source: HarnessColourSource; custom: string | null };
  director: { head: "brief" | "harness"; intensity: RebrandIntensity; notes: string };
  guards: {
    speaker_front: boolean;
    face: "shift" | "reject" | "warn";
    veil: "entrance" | "off";
    bars: "auto" | "on" | "off";
    /** The stray-drawing clean-up; left out while off (rebrandRecipe
     *  setStrayClean), as the server leaves it out. */
    stray?: "clean";
  };
  elements: HarnessElement[];
}

export interface HarnessKindInfo {
  kind: HarnessKind;
  label: string;
  /** Drawn today; the rest are listed as coming. */
  ready: boolean;
  z: "behind" | "front";
  max: number;
}

export interface HarnessOption {
  value: string;
  label: string;
  help?: string;
  /** An older look, offered only while a draft uses it. */
  old?: boolean;
}

/** One-click colour: "auto" (the recipe's palette) or a #rrggbb hex. */
export interface HarnessSwatch {
  value: string;
  label: string;
}

export interface HarnessCatalogue {
  recipe_version: number;
  kinds: HarnessKindInfo[];
  anchors: HarnessOption[];
  entrances: RebrandPatternAnimation[];
  /** How a pattern ends its span ("auto": its entrance played backwards, or
   *  none when it lasts to the scene's end). Absent on an older server. */
  exits?: RebrandPatternAnimation[];
  patterns: RebrandPattern[];
  default_pattern: string;
  colour_sources: HarnessOption[];
  intensities: HarnessOption[];
  presets: { id: string; name: string; builtin: boolean; recipe: HarnessRecipe }[];
  limits: { max_elements: number; notes: number; patterns?: number };
  /** Per-pattern colour choices (Auto, 42 violet, …). */
  swatches?: HarnessSwatch[];
  /** What "42 violet" resolves to. */
  brand_colour?: string;
}

/** How far the scene's analysis for the in-house engine is: its camera.
 *  "waiting" until the speaker masks are in (it starts after them). */
export type HarnessCameraState = "waiting" | "working" | "ready" | "failed";

/** What the studio is still warming for a scene's first preview. */
export interface HarnessWarmup {
  colours: "ready" | "working" | "failed";
  masks: { done: number; total: number };
  /** false: the server has not prepared the scene (yet, or since a restart). */
  prepared?: boolean;
  /** The speaker found on the scene's frames (its keyframes), for the
   *  in-house engine. Absent on an older server. */
  speaker_frames?: { done: number; total: number };
  /** The camera followed over the scene. Absent on an older server. */
  camera?: HarnessCameraState;
  /** An intro or outro: whether anyone is in it, once known (the scene's). */
  has_person?: boolean | null;
}

/** GET /house/camera: how the camera moves over the scene, once followed.
 *  Frame i's matrix maps a frame-0 pixel to frame i's:
 *  M = [[a, -b, tx], [b, a, ty]] from `m[i] = [a, b, tx, ty]`. */
export interface HarnessCameraTrack {
  state: "ready";
  version: number;
  fps: number;
  n: number;
  width: number;
  height: number;
  method: "features" | "ecc" | "still";
  m: [number, number, number, number][];
  cuts_sec: number[];
  /** From here on the camera is held (it couldn't be followed). */
  held_from_sec: number | null;
  warnings: string[];
  zoom_max: number;
  shift_max_px: number;
  /** The speaker exactly as the in-house engine cuts them out (the scene's matte
   *  bank): one sample per keyframe window, held (`step`), never across a
   *  cut — what the moving preview's quick stand-in cuts by. Absent on an
   *  older server. */
  masks?: HarnessIconMasks | null;
  /** An intro or outro: whether anyone is in it, once known. */
  has_person?: boolean | null;
}

export type HarnessCamera =
  | HarnessCameraTrack
  | {
      state: Exclude<HarnessCameraState, "ready">;
      speaker_frames: { done: number; total: number };
      error?: string;
      has_person?: boolean | null;
    };

export interface HarnessScene {
  run_id: number;
  target: string;
  aspect: string;
  width: number;
  height: number;
  /** Omni edits ≤10 s at a time; each part has its own first frame. */
  chunks: { index: number; seconds: number; frame_url: string }[];
  default_seed: string;
  /** The scene exactly as a run cuts it (the normalised mp4). */
  video_url?: string | null;
  duration_sec?: number;
  /** Where the footage itself jumps to another shot (s); these stay in it. */
  cuts_sec?: number[];
  /** Seconds of a Veo tail flash (another shot's last frames) cut off the
   *  end, as a run cuts them too. 0 when the clip ends on its own shot. */
  trimmed_sec?: number;
  /** A main scene, or the reel's own intro or outro (done by hand only).
   *  Absent on a server before the reel's bookends: a scene. */
  kind?: "scene" | "intro" | "outro";
  /** An intro or outro: whether anyone is in it (false for the reel's own
   *  card or card animation; null for an uploaded clip until its analysis
   *  says — /warmup and /house/camera then carry it). null on a scene. */
  has_person?: boolean | null;
  /** The end grip's numbers. Absent on a server without the grip. */
  end?: HarnessSceneEnd | null;
  /** "full": the clip kept whole past its automatic cut (asked for with
   *  variant "full"; absent: the trimmed scene, or a server without it). */
  variant?: "full" | null;
}

/** A scene's end, in the reel's 30 fps frames: the source's own rate and
 *  frames, where the reel cuts it by itself (`auto`: a main scene's Veo tail
 *  flash; null for an intro, an outro or a clean clip), the prepared clip's
 *  frames (what can be branded on it) and how far the clip really runs
 *  (`extent`: the ruler's reach). */
export interface HarnessSceneEnd {
  src_fps: number | null;
  src_frames: number | null;
  auto: number | null;
  frames: number;
  extent: number;
}

/** How the studio looks at a scene when it isn't the scene as prepared:
 *  its end set by hand (30 fps frames), and the whole clip past its
 *  automatic cut ("full"). Absent / null: as ever. */
export interface SceneView {
  endFrame?: number | null;
  variant?: "full" | null;
}

/** What a scene request adds for its view (nothing when nothing is set). */
function viewBody(view: SceneView | null | undefined): Record<string, unknown> {
  return {
    ...(view?.endFrame != null ? { end_frame: view.endFrame } : {}),
    ...(view?.variant === "full" ? { variant: "full" } : {}),
  };
}

/** ... and what a GET adds to its query. */
function viewQuery(view: SceneView | null | undefined): string {
  return view?.variant === "full" ? "&variant=full" : "";
}

/** A view's part of a cache key: "" when nothing is set, so every key made
 *  without an end stays exactly as it was. */
export function viewKey(view: SceneView | null | undefined): string {
  return (view?.endFrame != null ? `|e${view.endFrame}` : "") + (view?.variant === "full" ? "|full" : "");
}

/** Building on a finished version:
 *    stack    "Add to it" — Omni works on the version's own video and draws
 *             only the new layers; outside them the version stays exactly as
 *             it is (the recipe then holds only the new layers)
 *    rebuild  "Change it" — redone from the original with the full, edited
 *             list; parts whose input did not change are reused */
export interface HarnessBaseRef {
  version_id: number;
  mode: "stack" | "rebuild";
}

/** Where a version's run really drew one of its patterns (its evidence),
 *  as the server lists it with the layer; null until it has run. */
export interface HarnessLayerPlaced {
  slug: string | null;
  name: string | null;
  where: string | null;
  layout: HarnessPatternLayout;
  /** The place it holds ("left", "top-right", "full"). */
  slot?: string | null;
}

/** A layer a version shows, with the version that drew it (a stacked
 *  version shows its base's layers too). `from_version` and `placed` are
 *  the server's notes about the layer, never part of a recipe element. */
export type HarnessLayer = HarnessElement & { from_version: number; placed?: HarnessLayerPlaced | null };

/** A version made ready to add on top of: each part's frame as Omni will
 *  get it (the speaker from frame 0, the version's graphics settled). */
export interface HarnessBaseInfo {
  version_id: number;
  chunks: { index: number; seconds: number; frame_url: string }[];
  layers: HarnessLayer[];
  /** How many "Add to it" passes it already sits on. */
  depth?: number;
  /** The most there can be (one more is refused). */
  max_depth?: number;
}

/** A version = a Rebrand experiment + the recipe it ran (null: a run from
 *  the classic panel, before recipes). */
export type HarnessVersion = RebrandExperiment & {
  recipe: HarnessRecipe | null;
  /** Per part: its settled first frame (the speaker from frame 0, the
   *  graphics settled), once the version is ready. */
  stills?: (string | null)[] | null;
  /** Lineage: the version it was built on, and how. */
  base?: HarnessBaseRef | null;
  /** Every layer it shows (its base's too), each with the version that drew it. */
  layers?: HarnessLayer[] | null;
  /** How many "Add to it" passes it sits on (0: made from the original). */
  depth?: number | null;
  /** Made in house, and its video is gone from the server: it can be made
   *  again, free (POST /versions/{id}/remake — the same frames). */
  lost?: boolean;
};

/** POST/GET /versions/{id}/remake: a version made in house, made again. */
export interface HarnessRemake {
  version_id: number;
  state: "running" | "ready" | "failed";
  progress: number;
  /** The studio's render of the same spec was copied (no render at all). */
  reused?: boolean;
  /** It came out of the very spec its run rendered. */
  same?: boolean;
  error?: string;
}

export interface HarnessPrepared {
  scene: HarnessScene;
  default_recipe: HarnessRecipe;
  versions: HarnessVersion[];
  warming?: HarnessWarmup;
}

/** One drawn layer of a part: which element, and its alpha as the target
 *  lays it in (after the speaker's cut) — a white PNG whose opacity is it. */
export interface HarnessPreviewLayer {
  element_id: string | null;
  alpha_url: string;
}

export interface HarnessPreviewChunk {
  index: number;
  seconds: number;
  frame_url: string;
  /** The pattern enters on this part (the first, unless it has no entrance). */
  entering: boolean;
  /** What Omni is told for this part (after the brief's opening). */
  prompt: string;
  /** The frame Omni is shown; null without a pattern. */
  target_url: string | null;
  /** The target's layers, back to front: `frame_url` with each alpha laid
   *  in, in its element's colour, IS the target (rebrandRecolour.ts). The
   *  alphas carry no colour, so a colour change keeps their addresses. */
  layers?: HarnessPreviewLayer[];
  /** `hold`: added on top of a version, which stays exactly as it is
   *  outside the new layers. */
  guards: {
    speaker_front: boolean;
    clear_veil_sec: number;
    clear_bars: boolean;
    veil_keep?: boolean;
    hold?: boolean;
    /** The run puts back what Omni drew outside the recipe's layers. */
    stray_clean?: boolean;
  } | null;
  /** Made in house (the first part): where the design still's speaker cut
   *  leaves a pattern seen — a white PNG whose opacity is it (1 − the scene's
   *  matte on frame 0, the bars kept). The stage's stand-in cuts a pattern a
   *  hand is moving by it. */
  keep_url?: string;
}

/** A place as the server pins it (rebrand_pattern_ref Layout.public()). */
export interface HarnessPatternLayout {
  placement: string;
  anchor?: string;
  rotate?: number;
  scale?: number;
  reach?: number;
  fit?: string;
  valign?: string;
}

export interface HarnessPlacementReport {
  mode: string;
  reason?: string;
  picked?: { label?: string; layout?: HarnessPatternLayout };
  [key: string]: unknown;
}

/** One pattern of a preview, in draw order (first = back-most). One that
 *  could not be built is listed too, with `error` set and nulls for what it
 *  would have drawn. */
export interface HarnessPreviewPattern {
  element_id: string;
  slug: string | null;
  name: string | null;
  colour: string | null;
  where: string | null;
  animation: string | null;
  layout: HarnessPatternLayout | null;
  fell_back?: boolean;
  placement?: HarnessPlacementReport | null;
  /** Set when its own place was taken and it moved (a note, or true). */
  moved?: string | boolean | null;
  /** Share of its ink over earlier patterns (0..1). */
  overlap?: number | null;
  /** Where its ink is, in frame fractions. */
  box?: HarnessBox | null;
  /** The place it holds ("left", "top-right", "full"). */
  slot?: string | null;
  /** Why it could not be drawn (the others still are). */
  error?: string | null;
  /** Moved by hand: the move as drawn (a full-screen composition's size
   *  raised so it fills the frame); null when it is not. */
  hand?: { dx: number; dy: number; scale: number; rotate: number } | null;
  /** Its visible box BEFORE any move (frame fractions): the box a first move
   *  is grabbed at — the pivot is its centre. */
  base_box?: HarnessBox | null;
  /** The centre its size and turn are about (frame fractions). */
  pivot?: [number, number] | null;
  /** A full-screen place: panned, zoomed and turned, never boxed. */
  full?: boolean;
  /** Its art's key (POST /pattern/art): the same while its place is. */
  art_key?: string | null;
  /** A move by hand that hides it (behind the head, mostly behind the speaker,
   *  mostly off the picture), in words. */
  warning?: string | null;
  /** When it shows (scene s: its span, its entrance at the start and its
   *  exit at the end, resolved) — null when it starts after the scene ends
   *  or could not be drawn. Absent on an older server. */
  timing?: Timing | null;
}

export interface HarnessPreview {
  recipe_hash: string;
  seed: string;
  opening: string;
  /** The first pattern (older clients read only this). */
  pattern: {
    slug: string;
    name: string;
    colour: string;
    ground: string;
    where: string;
    animation: string;
    fell_back: boolean;
    layout: HarnessPatternLayout;
  } | null;
  pattern_error: string | null;
  placement: HarnessPlacementReport | null;
  steps: { name: string; status: string; detail: string }[];
  warnings: string[];
  chunks: HarnessPreviewChunk[];
  /** Every drawn pattern (absent on a server with one pattern at most). */
  patterns?: HarnessPreviewPattern[];
  /** The parts Generate would send to Omni (absent: all of them). Built on
   *  a version: only the parts the new layers show in (Add to it), or whose
   *  Omni clip can't be reused (Change it). */
  omni_parts?: number[];
  /** The version it was drawn for (absent: a fresh draft). */
  base?: HarnessBaseRef | null;
  /** "house": the draft is made by our own renderer ($0, omni_parts []). */
  engine?: HarnessEngine;
  /** Built on a version (only then): it, or one under it, shows icons Omni
   *  drew itself (a brief asked for them). */
  parent_has_omni_icons?: boolean;
  timings?: Record<string, number>;
  colours?: { intro: string | null; brand: string; fell_back: boolean };
  /** false: drawn without everything it needs (the speaker's mask, a
   *  pattern that failed or fell back), so neither the server nor the
   *  studio keeps it for this recipe — asked again, it is drawn again. */
  kept?: boolean;
  /** Made in house: the first part's picture IS the design still (the run's
   *  own plate over frame 0, the patterns fully in at `at` s), and how each
   *  pattern moves — what the moving preview's quick stand-in draws until
   *  the exact video is made. Absent for Omni, or on an older server. */
  house?: HarnessHousePreview | null;
}

/** A pattern as the in-house engine draws it: its art (straight RGBA, uncut:
 *  frame-sized — moved by hand, its place UNMOVED past the frame as far as
 *  the move needs; the frame's origin at `margin` art px, which may be
 *  negative), its ink box (art px), its
 *  time (scene s), whether it moves with the camera ("room") or holds still
 *  on screen, the edge a slide comes from, its hand move (2x3, frame px). */
export interface HarnessHouseLayer {
  element_id: string;
  art_url: string;
  ink_box: [number, number, number, number];
  margin: [number, number];
  timing: Timing | null;
  follow: "room" | "screen" | string;
  z: "behind" | "front" | string;
  edge: "left" | "right" | "top" | "bottom" | string;
  colour: string;
  seed: string;
  hand: number[][] | null;
}

export interface HarnessHousePreview {
  engine_v: number;
  still_url: string;
  /** The moment the still shows the patterns at (s). */
  at: number;
  width: number;
  height: number;
  fps: number;
  n: number;
  layers: HarnessHouseLayer[];
  /** The design still's speaker cut (as chunks[0].keep_url). */
  keep_url?: string | null;
}

/** POST /pattern/art: one pattern of the draft UNMOVED and uncut, past the
 *  frame's edges — a white PNG (the stage colours it) and where it lies, in
 *  frame fractions (x and y may be negative). `art_rect`: a full-screen
 *  composition's own outline (its zoom never goes below covering the
 *  frame); null for every other. */
export interface HarnessPatternArt {
  art_url: string;
  art_key: string;
  width: number;
  height: number;
  art_window: HarnessBox;
  art_rect: HarnessBox | null;
}

/** POST /house/render: the draft rendered exactly as Generate will make it,
 *  in the background. `url` once ready; a new draft stops the old render. */
export interface HarnessHouseRender {
  key: string;
  state: "queued" | "running" | "ready" | "failed" | "cancelled";
  progress: number;
  url?: string;
  seconds?: number;
  error?: string;
}

/** POST /house/moment: the draft's exact frame at t, and what the stand-in
 *  draws there. */
export interface HarnessHouseMoment {
  url: string;
  frame: number;
  t: number;
  key?: string | null;
  ingredients?: {
    camera: [number, number, number, number];
    layers: { element_id: string; phase: string; q: number }[];
  };
}

// --- icons -----------------------------------------------------------------

/** A library icon (Phosphor fill, by id) or one the AI drew (c-<12 hex>). */
export interface HarnessIconGlyph {
  source: "library" | "custom";
  id: string;
}

/** An icon the user added (the AI's picks are the server's, live). `at`:
 *  the moment it was added at (the playhead, scene seconds): one nothing
 *  they say is tied to shows at the clear time nearest it. */
export interface HarnessIconAdd {
  glyph: HarnessIconGlyph;
  label: string;
  at?: number;
}

/** A per-icon pin, by glyph id: another mention of its idea (`w`, with the
 *  digest of the words it indexes), a time off its word (`off_word` +
 *  `sec`, only when the user chose that), its place (the body's box) and its
 *  length (a length set by hand: 0.2-30 s). */
export interface HarnessIconTweak {
  w?: number | null;
  words_digest?: string | null;
  off_word?: boolean;
  sec?: number | null;
  box?: HarnessBox | null;
  hold_sec?: number | null;
}

/** How an icon looks: circle (a disc, the bold glyph), chip (a pill with its
 *  words), badge (the 42 badge — every older version; unset means it) or
 *  just the glyph. */
export type HarnessIconStyle = "circle" | "chip" | "badge" | "glyph";
/** An icon's own entrance or exit ("grow": a chip unfolds from its circle). */
export type HarnessIconMotion = "none" | "fade" | "pop" | "slide" | "glitch" | "grow";

/** One icon's own look, by glyph id. Every key is optional: absent is the
 *  scene's (the element's style, colour, size and entrance). */
export interface HarnessIconLook {
  style?: HarnessIconStyle | null;
  /** #rrggbb, the body. */
  colour?: string | null;
  /** #rrggbb, the glyph and words (absent: worked out from the colour). */
  ink?: string | null;
  /** A chip's words (≤ 24 characters). */
  label?: string | null;
  /** Its own size, a share of the frame's short side (0.08-0.30). */
  size?: number | null;
  layer?: "behind" | "front" | null;
  enter?: HarnessIconMotion | null;
  /** absent: the entrance mirrored. */
  leave?: HarnessIconMotion | null;
}

/** The icon element's props. Unset = the default (written that way, so a
 *  control set back gives the very same recipe). `resolved` is the router's,
 *  written at Generate: a draft never holds it. */
export interface HarnessIconProps {
  /** false: keywords only — no automatic icons, just suggestions. */
  ai?: boolean;
  /** Unset: the 42 badge (every older version). New drafts write "circle". */
  style?: HarnessIconStyle;
  /** Each icon's own look, by glyph id (at most 8). */
  looks?: Record<string, HarnessIconLook>;
  colour?: string;
  /** hide: Omni is told to draw none (ours are stamped); keep: Omni's own
   *  stay and ours are off (only on a version that has Omni's own). */
  omni_own?: "hide" | "keep";
  hold_sec?: number | null;
  added?: HarnessIconAdd[];
  dismissed?: string[];
  tweaks?: Record<string, HarnessIconTweak>;
  /** The track the user put an icon on, by glyph id (1 and up: one of its
   *  own, so it may show with others; at most 8 icons). */
  lanes?: Record<string, number>;
  /** "Keep v3's icons": that version's (by id) exactly as it froze them —
   *  nothing planned. Any icon edit drops it. */
  keep?: number | null;
  resolved?: unknown[] | null;
}

export interface HarnessIconLibraryItem {
  id: string;
  name: string;
  /** The SVG path (viewBox 0 0 256 256), the same one the server stamps
   *  (null for a team icon: its picture is `png_url`). */
  d: string | null;
  /** The same icon in Phosphor's bold weight (what the circle and chip
   *  icons draw); the fill path when the bold set lacks it. */
  b?: string | null;
  label?: string | null;
  tags?: string[];
  /** Search results: "library" (Phosphor) or "custom" (your team's own). */
  source?: "library" | "custom";
  origin?: "phosphor" | "ai" | "upload";
  /** A team icon's PNG (black on clear, one shape). */
  png_url?: string | null;
  /** 0..1; 0.8 or more is a sure match. */
  score?: number | null;
  /** How it was found: name, synonym, keyword, tag, words, fuzzy. */
  via?: string | null;
}

/** GET /icons/search: the one search (the library and the team's icons),
 *  and whether "Generate with AI" may be offered for the query. */
export interface HarnessIconSearch {
  query: string;
  total: number;
  items: HarnessIconLibraryItem[];
  /** The best result is a sure match. */
  strong: boolean;
  best: number;
  via: string;
  generate: HarnessIconGenerateOffer;
}

/** Whether the typed name may be drawn by the AI (a card when nothing
 *  found is sure, a quiet link otherwise). */
export interface HarnessIconGenerateOffer {
  offer: boolean;
  name: string;
  cost_usd: number;
  per_day: number;
  /** The team's drawings left today (null: not known right now). */
  left_today: number | null;
  /** Why it can't be drawn (a brand, the day's drawings used). */
  why_not: string | null;
}

/** A team icon (drawn by the AI on a press, or uploaded): shared by every
 *  machine. */
export interface HarnessIconTeamItem {
  id: string;
  name: string;
  label: string;
  tags: string[];
  origin: "ai" | "upload";
  source: "custom";
  png_url: string;
  created_at: string | null;
  created_by: string;
  hidden: boolean;
}

/** POST /icons/generate: the name drawn by the AI (or the drawing made
 *  already, free), with the circle and the chip exactly as a video stamps
 *  them. */
export interface HarnessIconGenerated {
  id: string;
  name: string;
  label: string;
  origin: "ai";
  take: number;
  /** Drawn now (paid); false: the drawing made already, free. */
  created: boolean;
  /** One you discarded earlier, shown again. */
  was_hidden: boolean;
  report: {
    shapes?: number;
    dropped?: number;
    ink?: number;
    weight?: "kept" | "bolder" | "thinner" | "solid" | "empty";
    weight_from?: number | null;
    weight_to?: number | null;
  } | null;
  cost_usd: number;
  png_url: string;
  /** PNG data URLs: the circle (style E) and the chip with its words (H). */
  preview: { circle: string | null; chip: string | null };
  left_today: number | null;
}

/** How an upload is read: "auto"; "shape" keeps a plate the icon sits on;
 *  "marks" takes the marks off it (offered when `report.plate` is set). */
export type HarnessIconUploadMode = "auto" | "shape" | "marks";

/** What the clean-up did to an upload (the numbers the card shows). */
export interface HarnessIconUploadReport {
  kind?: "svg" | "png" | "webp" | "jpeg";
  /** How its shape was found. */
  via?: "shapes" | "transparency" | "light page" | "dark ground";
  /** It sits on a plate: null none, "kept", or "dropped" (only the marks). */
  plate?: "kept" | "dropped" | null;
  shapes?: number;
  dropped?: number;
  ink?: number;
  weight?: "kept" | "bolder" | "thinner" | "solid" | "empty";
  weight_from?: number | null;
  weight_to?: number | null;
  svg?: { fills?: number; strokes?: number; knockouts?: number; faint?: number; white_ink?: boolean; skipped?: string[] };
}

/** POST /icons/upload/preview: the file made into its icon — free, nothing
 *  saved — as the circle and the chip will stamp it. */
export interface HarnessIconUploadPreview {
  /** The id it will be saved under (the same file is always one icon). */
  id: string;
  /** The cleaned glyph, a PNG data URL. */
  png_url: string;
  preview: { circle: string | null; chip: string | null };
  report: HarnessIconUploadReport;
  name_guess: string;
  kind: "svg" | "png" | "webp" | "jpeg";
  /** Your team has this very icon already. */
  duplicate_of: { id: string; name: string; hidden: boolean } | null;
}

/** POST /icons/upload: saved to the team's icons (`duplicate`: it was there
 *  already, its own name kept). */
export type HarnessIconUploaded = HarnessIconTeamItem & { duplicate: boolean; report?: HarnessIconUploadReport | null };

/** What the studio checks before sending a file. */
export interface HarnessIconUploadLimits {
  max_bytes: number;
  svg_max_bytes: number;
  kinds: string[];
  name_max: number;
  tags_max: number;
  tag_max: number;
}

/** The numbers the scheduler works with (seconds unless named in frames),
 *  and the circle / chip geometry (shares of L, or of the chip's height h)
 *  the studio draws with while a plan is on its way. */
export interface HarnessIconRules {
  fps: number;
  in_f: number;
  out_f: number;
  land_f: number;
  hold_min: number;
  hold_max: number;
  gap_user: number;
  gap_auto: number;
  head_clear: number;
  entrance_clear: number;
  tail_clear: number;
  items_max: number;
  magnet: number;
  /** The exact frame worth showing: this long after an icon settles. */
  moment_after?: number;
  /** One icon's own size, a share of the frame's short side. */
  size_min?: number;
  size_max?: number;
  /** A chip's words, at most. */
  label_max?: number;
  looks_max?: number;
  user_hold_min?: number;
  user_hold_max?: number;
  circle_glyph?: number;
  chip_h?: number;
  chip_glyph?: number;
  chip_pad_glyph?: number;
  chip_gap?: number;
  chip_pad_words?: number;
  chip_em?: number;
  chip_em_min?: number;
  chip_max_w?: number;
  shadow_sigma?: number;
  shadow_alpha?: number;
  shadow_dy?: number;
  /** The speaker rules a box is judged by (an icon dragged by hand is
   *  outlined by them): the speaker may cover this much of it; the head box
   *  grows by this share of the frame's width; a matte this high is the
   *  speaker; each icon's window is its time on screen ± this. */
  overlap_max?: number;
  head_grow?: number;
  person?: number;
  window_pad?: number;
  /** Icons that may be put on a track (props.lanes), and the highest track. */
  lanes_max?: number;
  lane_max?: number;
}

export interface HarnessIconCatalogue {
  version: string;
  /** The 60 "42 picks", grouped (Money, Code, People…). */
  picks: { group: string; items: HarnessIconLibraryItem[] }[];
  /** What one AI drawing costs, and how many the team may make a day. */
  generate?: { cost_usd: number; per_day: number };
  /** What may be uploaded as a team icon. */
  upload?: HarnessIconUploadLimits;
  styles?: (string | HarnessOption)[];
  entrances?: (string | HarnessOption)[];
  /** S / M / L as a share of the frame's short side. */
  sizes?: Record<string, number>;
  rules?: Partial<HarnessIconRules> & Record<string, number>;
  count?: number;
  /** The chip icons' label fonts: the key new chips use, and each label
   *  key's two faces (Latin, Arabic) as TTF URLs. */
  fonts?: HarnessIconFonts;
}

export interface HarnessIconFonts {
  default?: string;
  labels?: Record<string, { latin: string; arabic: string }>;
}

export interface HarnessIconMention {
  /** Word index into timeline.words. */
  w: number;
  /** Its onset (s). */
  at: number;
}

/** One icon the plan shows, in speech order. Times are scene seconds:
 *  `from` it starts coming in, `settle` it is fully in, `to` it is gone. */
export interface HarnessIconItem {
  glyph: HarnessIconGlyph;
  label: string;
  /** kept: a version's own, as it froze them ("Keep v3's icons"). */
  source: "ai" | "keyword" | "user" | "kept";
  /** Its timing is the user's (a pin), not the AI's. */
  pinned: boolean;
  /** Shown off its word, because the user chose that. */
  off_word: boolean;
  from: number;
  settle: number;
  to: number;
  box: HarnessBox;
  /** The word it lands on (null: not tied to anything said). */
  word: string | null;
  w: number | null;
  purpose: string;
  priority: number;
  /** Every mention of its idea the AI confirmed (where it may move to). */
  mentions: HarnessIconMention[];
  /** When the exact frame of it fully in is worth showing. */
  moment_t: number;
  /** How long it stays fully on (s). */
  hold?: number;
  /** The spot it sits in. */
  spot?: string | null;
  /** The most of it the speaker ever covers in its window (0..1). */
  max_overlap?: number;
  // Its look, exactly as the run draws it (absent from an older server).
  /** The style it shows with; `asked` the one asked for (a chip with no
   *  room shows as a circle). */
  style?: HarnessIconStyle;
  asked?: HarnessIconStyle;
  /** The glyph's weight (the circle and the chip draw bold). */
  weight?: "fill" | "bold";
  fill?: string;
  ink?: string;
  /** A chip's words as drawn, and their label font's key. */
  text?: string | null;
  font?: string | null;
  layer?: "behind" | "front";
  look_v?: number;
  enter?: string;
  /** null: the entrance mirrored. */
  leave?: string | null;
  /** Its size, a share of the frame's short side. */
  size?: number;
  /** What the plan says about this icon ("too wide here, so a circle"). */
  notes?: string[];
  /** Set by the user: its look, its place, its time. */
  edited?: { look: boolean; place: boolean; time: boolean };
  /** Its glyph's path in its weight (library icons). */
  d?: string | null;
  /** Its settled look as the run stamps it (null: it couldn't be drawn). */
  sprite?: HarnessIconSprite | null;
  /** In a box the user put it in: what the speaker does to it there
   *  ("speaker": partly behind the speaker; "face": behind the head; null: clear),
   *  and the clear spot nearest to it ("Move to a clear spot"). */
  covered?: "speaker" | "face" | null;
  clear?: HarnessBox | null;
  /** "Why?": who picked it, what was said and when, why it helps, where it
   *  sits and why that colour (absent from an older server). */
  why?: HarnessIconWhy | null;
  /** Its track on the timeline: 0 the AI's (one at a time), 1+ one of
   *  yours that shows with others (null or absent: a version's own, or an
   *  older server; the lane stacks them as they overlap). */
  lane?: number | null;
  /** Moved off its word, out of a zone (the opening, a pattern coming in or
   *  going out, the end): `at` the settle it would have on its word, `said`
   *  when the word is said (s), and why it moved. */
  shifted?: HarnessIconShift | null;
}

/** Why one of yours moved off its word, or (never said) off the moment it
 *  was added at (elements/icon.Scheduler._shift_of, _moment_shift). */
export interface HarnessIconShift {
  /** The settle it would have on its word (s); never said, the moment. */
  at: number;
  /** When its word is said (s); null: never said. */
  said: number | null;
  /** The moment it was added at (IconAdd.at, s), when it was: what "Show
   *  at … anyway" offers and pins (rebrandIcons.anywayOf). */
  asked?: number | null;
  why: "entrance" | "exit_after" | "exit_before" | "start" | "end" | string;
}

/** A shown icon's "Why?" (elements/icon.why_of) — the facts; the studio says
 *  them in its own words.
 *  - by: who picked it — "ai" (on its own) · "keyword" · "you" (added) ·
 *    "kept" (the AI's pick, kept by you) · "team" (your team's icon, added)
 *    · "version" (kept from a version)
 *  - priority: the AI's 1-5 for the idea (null: the AI didn't judge it)
 *  - said: the word it lands on and when it is said (null: off its word)
 *  - time: "word" · "pinned" (a word you picked) · "hand" (a time you set)
 *  - place: "you" (your box) · the home spot's "ai" / "score" / "you" and
 *    its why · "spot" (a place of its own) · "version"
 *  - colour: "you" (its own) · "scene" (the scene's, and why) · "version" */
export interface HarnessIconWhy {
  by: "ai" | "keyword" | "you" | "kept" | "team" | "version" | string;
  text: string;
  priority: number | null;
  said: { word: string; at: number | null } | null;
  time: "word" | "pinned" | "hand" | "version" | string;
  purpose: string;
  place: { by: string; text: string };
  colour: { by: string; text: string } | null;
}

/** Why an idea is where it is (elements/icon.idea_why): "on" · "you"
 *  (removed by you) · "word" (yours, waiting for a word) · "room" · "ai" (the
 *  AI left it out) · "check" (one of our rules did) · "silent" (the AI gave
 *  no verdict) · "hopeful" (hopeful but general: yours to add) · "suggest"
 *  (the AI suggests it) · "keyword" · "team" (your team's icon, said here) ·
 *  "gone" — with the plain reason. */
export interface HarnessIconIdeaWhy {
  by: string;
  text: string;
}

/** An icon's settled sprite: the run's own pixels, a PNG `w` x `h` px whose
 *  body starts `ox`, `oy` px in (the shadow's margin); `at` is where the PNG
 *  rests on the frame (fractions). */
export interface HarnessIconSprite {
  url: string;
  w: number;
  h: number;
  ox: number;
  oy: number;
  at: { x: number; y: number; w: number; h: number };
}

/** `gone`: one the user added that the icon library no longer has (listed
 *  so it can be removed). Left out: `no_room` (another icon is there),
 *  `zone` (said in the opening, or while a pattern comes in or goes out),
 *  `near_end`, `limit` (over the scene's limit), `no_spot` (no clear spot). */
export type HarnessIconIdeaState =
  | "on"
  | "no_room"
  | "zone"
  | "near_end"
  | "limit"
  | "no_spot"
  | "refused"
  | "dismissed"
  | "available"
  | "gone";

/** An idea the AI (or the keyword list) found in what is said, shown or not. */
export interface HarnessIconIdea {
  glyph: HarnessIconGlyph;
  label: string;
  word: string | null;
  w: number | null;
  at: number | null;
  purpose: string;
  priority: number;
  state: HarnessIconIdeaState;
  reason: string | null;
  /** The user added it. */
  added?: boolean;
  /** "team": one of your team's icons, said here (a suggestion). */
  source?: "ai" | "keyword" | "user" | "team" | string;
  /** The AI shows it on its own (removed, "Add back" only undoes that). */
  auto?: boolean;
  why?: HarnessIconIdeaWhy;
}

/** The speaker's masks over the scene, as one grey PNG strip: sample k (at
 *  `times[k]` s) in rows [k·height, (k+1)·height); white is the speaker.
 *  `cuts` (s): where the footage jumps to another shot — two samples either
 *  side of a cut are never blended (the times are uneven there). `step`:
 *  each sample holds until the next (never blended at all). */
export interface HarnessIconMasks {
  url: string;
  times: number[];
  width: number;
  height: number;
  cuts?: number[];
  step?: boolean;
  // The icon plan's (the speaker track's): what the planner judges a box by.
  /** Each sample's head box (frame px; null: none found). */
  heads?: (number[] | null)[];
  /** The frame (px). */
  frame?: [number, number];
  /** Without a cut, the samples are this many frames apart (at fps). */
  sample_frames?: number;
  fps?: number;
  /** The letterbox bars at the masks' size: [row, x0, x1) runs. */
  bars?: number[][];
}

/** A place an icon may go (the fixed grid beside the speaker, or the bars). */
export interface HarnessIconSpot {
  id: string;
  group: string;
  box: HarnessBox;
  /** Clear of the speaker for the icons' windows. */
  ok: boolean;
  /** The AI's read of the room (a veto only). */
  ai: { verdict: "good" | "avoid" | string; why: string } | null;
  /** The icons' home spot. */
  home?: boolean;
  /** How calm the wall behind it is (0..1). */
  calm?: number;
}

export interface HarnessIconStep {
  name: "words" | "spots" | "director" | string;
  state: "queued" | "running" | "done" | "failed" | string;
  seconds?: number | null;
}

export interface HarnessIconTimeline {
  seconds: number;
  voiced: [number, number] | null;
  /** 200 bins of speech loudness, 0..1. */
  envelope: number[];
  /** `s` is null for a script word that was not said. */
  words: { w: number; text: string; s: number | null; said: boolean }[];
  chunks: { start: number; seconds: number }[];
  /** Where no icon may be (the opening, the pattern's entrance, the end). */
  zones: { from: number; to: number; why: string }[];
  digest?: string | null;
}

export interface HarnessIconPlan {
  /** warming: the words, the spots or the AI are still being worked out —
   *  ask again in a second. */
  state: "warming" | "ready";
  steps: HarnessIconStep[];
  plan_key: string | null;
  /** Place and colour wait on the pattern preview: until it is made, they
   *  are the plan's best guess. */
  provisional: boolean;
  source?: { words: "whisper" | "estimate" | string; director: "ai" | "keywords" | string } | null;
  items: HarnessIconItem[];
  ideas: HarnessIconIdea[];
  colours?: { fill: string; ink: string; why: string } | null;
  home?: { spot: string | null; why: string; by: "ai" | "score" | "you" | string } | null;
  spots?: HarnessIconSpot[];
  /** The Where choices this scene has ("auto", "left", "right", "top"…). */
  anchors_available?: string[];
  warnings?: string[];
  timeline?: HarnessIconTimeline | null;
  /** What the icons are drawn over: the scene, or the version built on.
   *  `pattern_url`: the pattern layer, when the plan gives one (approximate).
   *  `clean`: the version's video without our icons (these replace them).
   *  `masks`: the speaker's shape over the scene, to cut the pattern and the
   *  icons by as it moves. */
  underlay?: {
    kind: "scene" | "version" | string;
    video_url: string | null;
    pattern_url?: string | null;
    version_id?: number | null;
    clean?: boolean;
    /** The video carries our icons already (no clean clip): nothing is
     *  drawn over it. */
    baked?: boolean;
    masks?: HarnessIconMasks | null;
  } | null;
  /** Built on a version (only then): it, or one under it, shows icons Omni
   *  drew itself — ours on top would make two sets. */
  parent_has_omni_icons?: boolean;
  /** The digest of the words a pin's `w` indexes. */
  words_digest?: string | null;
  /** true: the recipe has no icon element. */
  off?: boolean;
  /** "Keep v3's icons": the plan IS that version's icons (nothing planned). */
  kept?: { version_id: number } | null;
  /** Asked with `reorder`: the pins that show that icon in that place. */
  reorder?: HarnessIconReorder | null;
}

/** The server's answer to "show this icon at place `to`": pins that keep
 *  every icon on a mention of its own idea — or, when there is none, a
 *  question (`choice`) with the off-its-word pin for "anyway". */
export interface HarnessIconReorder {
  ok: boolean;
  tweaks?: Record<string, HarnessIconTweak>;
  message?: string | null;
  choice?: { message: string; anyway: { tweaks: Record<string, HarnessIconTweak>; sec: number } | null } | null;
}

/** POST /icons/moment: the exact frame at t, built the way the run builds it. */
export interface HarnessIconMoment {
  url: string;
  frame: number;
  t: number;
}

/** A failed request, with the server's own words and its status, so the
 *  studio can tell a recipe it refuses (422) from a hiccup worth retrying.
 *  The message keeps the "<status> <detail>" shape rebrandErrorDetail strips.
 *  `payload` is what the server sent with it (a 409's fresh icon plan). */
export class HarnessHttpError extends Error {
  status: number;
  detail: string;
  errors: { element_id?: string | null; field?: string; message: string }[];
  payload: unknown;

  constructor(status: number, detail: string, errors: HarnessHttpError["errors"] = [], payload: unknown = null) {
    super(`${status} ${detail}`);
    this.name = "HarnessHttpError";
    this.status = status;
    this.detail = detail;
    this.errors = errors;
    this.payload = payload;
  }
}

/** The fresh icon plan a 409 from Generate carries ("The icons changed
 *  since you looked"), or null for any other refusal. */
export function iconConflictOf(e: unknown): { message: string; plan: HarnessIconPlan } | null {
  if (!(e instanceof HarnessHttpError) || e.status !== 409) return null;
  const p = e.payload as { plan?: unknown; detail?: { plan?: unknown } } | null;
  const plan = (p?.plan ?? p?.detail?.plan) as HarnessIconPlan | undefined;
  return plan && typeof plan === "object" && Array.isArray(plan.items) ? { message: e.detail, plan } : null;
}

/** One refusal as the studio shows it. The harness sends {element_id, field,
 *  message}; a recipe pydantic refuses, or a request FastAPI refuses, sends
 *  {loc, msg} instead. */
function refusal(e: unknown): HarnessHttpError["errors"][number] | null {
  if (!e || typeof e !== "object") return null;
  const o = e as { element_id?: string | null; field?: string; message?: unknown; msg?: unknown; loc?: unknown };
  const message = typeof o.message === "string" ? o.message : typeof o.msg === "string" ? o.msg : "";
  if (!message) return null;
  const field = o.field ?? (Array.isArray(o.loc) ? o.loc.join(".") : undefined);
  return { element_id: o.element_id ?? null, field, message };
}

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  // A file goes as multipart: the browser sets its Content-Type (with the
  // boundary) itself.
  const form = typeof FormData !== "undefined" && init?.body instanceof FormData;
  const res = await fetch(path, {
    ...init,
    headers: form ? { ...(init?.headers || {}) } : { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    let errors: HarnessHttpError["errors"] = [];
    let payload: unknown = null;
    try {
      const body = await res.json();
      const d = body?.detail;
      payload = d && typeof d === "object" && !Array.isArray(d) ? d : body;
      const listed: unknown[] = Array.isArray(d) ? d : Array.isArray(d?.errors) ? d.errors : [];
      errors = listed.map(refusal).filter((x): x is NonNullable<typeof x> => !!x);
      detail =
        typeof d === "string"
          ? d
          : d?.message ||
            body?.message ||
            (errors.length ? errors.map((x) => x.message).join("; ") : JSON.stringify(d ?? body));
    } catch {
      /* ignore */
    }
    throw new HarnessHttpError(res.status, detail, errors, payload);
  }
  return res.json() as Promise<T>;
}

export const harnessApi = {
  catalogue: () => http<HarnessCatalogue>("/api/rebrand/harness/catalogue"),

  /** Slow the first time (~10-30 s): the scene is cut exactly as a run cuts it.
   *  `target`: scene_N, or the reel's "intro" | "outro" (a server that has
   *  them says so on the reel block: `bookends`). */
  prepare: (runId: number, target: string, view?: SceneView | null) =>
    http<HarnessPrepared>("/api/rebrand/harness/prepare", {
      method: "POST",
      body: JSON.stringify({ run_id: runId, target, ...viewBody(view) }),
    }),

  /** What the first preview is still waiting on (`prepared: false` when the
   *  server has not prepared the scene, yet or since a restart). */
  warmup: (runId: number, target: string, signal?: AbortSignal, view?: SceneView | null) =>
    http<HarnessWarmup | { prepared: false }>(
      `/api/rebrand/harness/warmup?run_id=${runId}&target=${encodeURIComponent(target)}${viewQuery(view)}`,
      { signal },
    ),

  /** Free: how the camera moves over the scene — or how far its analysis
   *  is (ask again until `state` is "ready" or "failed"). */
  houseCamera: (runId: number, target: string, signal?: AbortSignal, view?: SceneView | null) =>
    http<HarnessCamera>(
      `/api/rebrand/harness/house/camera?run_id=${runId}&target=${encodeURIComponent(target)}${viewQuery(view)}`,
      { signal },
    ),

  /** Free: no Omni call. With `base`, it is drawn as Generate would make it
   *  on that version (stack: on the version's own frames). Without, the body
   *  is exactly what it always was. */
  preview: (
    runId: number,
    target: string,
    recipe: HarnessRecipe,
    opts: { signal?: AbortSignal; base?: HarnessBaseRef | null; view?: SceneView | null } = {},
  ) =>
    http<HarnessPreview>("/api/rebrand/harness/preview", {
      method: "POST",
      body: JSON.stringify({
        run_id: runId,
        target,
        recipe,
        ...(opts.base ? { base: asBaseRef(opts.base) } : {}),
        ...viewBody(opts.view),
      }),
      signal: opts.signal,
    }),

  /** Free: one pattern unmoved and uncut past the frame's edges — what the
   *  stage draws under the hand while it moves, sizes or turns it. */
  patternArt: (
    runId: number,
    target: string,
    recipe: HarnessRecipe,
    elementId: string,
    opts: { signal?: AbortSignal; base?: HarnessBaseRef | null; view?: SceneView | null } = {},
  ) =>
    http<HarnessPatternArt>("/api/rebrand/harness/pattern/art", {
      method: "POST",
      body: JSON.stringify({
        run_id: runId,
        target,
        recipe,
        element_id: elementId,
        ...(opts.base ? { base: asBaseRef(opts.base) } : {}),
        ...viewBody(opts.view),
      }),
      signal: opts.signal,
    }),

  /** `fresh` (Try again): a changed version gets a new Omni take — nothing
   *  is reused from the version it was changed from (base.fresh).
   *  `iconsPlanKey`: the icon plan the studio showed; the server re-derives
   *  it and answers 409 with the fresh one when it moved on (iconConflictOf).
   *  `seenIdeas`: the icons seen in Add icon → Suggested while this draft
   *  was made (glyph ids, at most 60) — what the feedback log calls
   *  "ignored" when they weren't taken; sent only when there are any (an
   *  older server ignores it). */
  generate: (
    runId: number,
    target: string,
    recipe: HarnessRecipe,
    opts: {
      base?: HarnessBaseRef | null;
      fresh?: boolean;
      iconsPlanKey?: string | null;
      view?: SceneView | null;
      seenIdeas?: readonly string[] | null;
    } = {},
  ) =>
    http<HarnessVersion>("/api/rebrand/harness/versions", {
      method: "POST",
      body: JSON.stringify({
        run_id: runId,
        target,
        recipe,
        ...(opts.base
          ? { base: opts.fresh && opts.base.mode === "rebuild" ? { ...asBaseRef(opts.base), fresh: true } : asBaseRef(opts.base) }
          : {}),
        ...(opts.iconsPlanKey ? { icons_plan_key: opts.iconsPlanKey } : {}),
        ...viewBody(opts.view),
        ...(opts.seenIdeas?.length ? { seen_ideas: opts.seenIdeas.slice(0, 60) } : {}),
      }),
    }),

  // --- icons ---------------------------------------------------------------

  iconCatalogue: () => http<HarnessIconCatalogue>("/api/rebrand/harness/icons/catalogue"),

  /** The one icon search — the library and your team's icons, best first
   *  (the 42 picks first on an empty query). Free: nothing is drawn. */
  iconSearch: (q: string, opts: { offset?: number; limit?: number; signal?: AbortSignal } = {}) =>
    http<HarnessIconSearch>(
      `/api/rebrand/harness/icons/search?q=${encodeURIComponent(q)}&offset=${opts.offset ?? 0}&limit=${opts.limit ?? 60}`,
      { signal: opts.signal },
    ),

  /** Your team's own icons, newest first. */
  iconTeam: (opts: { limit?: number; signal?: AbortSignal } = {}) =>
    http<{ items: HarnessIconTeamItem[]; total: number }>(`/api/rebrand/harness/icons/team?limit=${opts.limit ?? 24}`, {
      signal: opts.signal,
    }),

  /** "Generate with AI" — the only paid icon call (~$0.04), made only on
   *  the user's press. A name drawn already comes back free; `again` draws
   *  a new one and hides the one shown. 422 a brand or a bad drawing, 429
   *  the team's drawings for today are used, 503 try again in a minute. */
  iconGenerate: (name: string, opts: { again?: boolean } = {}) =>
    http<HarnessIconGenerated>("/api/rebrand/harness/icons/generate", {
      method: "POST",
      body: JSON.stringify({ name, ...(opts.again ? { again: true } : {}) }),
    }),

  /** Hide a team icon from the library, for everyone (versions that use it
   *  still draw it). */
  iconHide: (id: string) =>
    http<unknown>(`/api/rebrand/harness/icons/custom/${encodeURIComponent(id)}`, { method: "DELETE" }),

  /** Free, nothing saved: an SVG / PNG / WebP / JPEG made into a one-colour
   *  icon, shown as the circle and the chip (with `label`'s words) will stamp
   *  it. 413 too large, 415 not a kind the server reads, 422 not an icon. */
  iconUploadPreview: (
    file: Blob,
    opts: { filename?: string; mode?: HarnessIconUploadMode; label?: string; signal?: AbortSignal } = {},
  ) => {
    const form = new FormData();
    form.append("file", file, opts.filename ?? (file instanceof File ? file.name : "icon"));
    if (opts.mode && opts.mode !== "auto") form.append("mode", opts.mode);
    if (opts.label?.trim()) form.append("label", opts.label.trim());
    return http<HarnessIconUploadPreview>("/api/rebrand/harness/icons/upload/preview", {
      method: "POST",
      body: form,
      signal: opts.signal,
    });
  },

  /** Save an upload to your team's icons, for every machine: exactly the
   *  icon its preview showed (the same file in the same mode). */
  iconUpload: (
    file: Blob,
    opts: { filename?: string; name: string; tags?: string; mode?: HarnessIconUploadMode },
  ) => {
    const form = new FormData();
    form.append("file", file, opts.filename ?? (file instanceof File ? file.name : "icon"));
    form.append("name", opts.name.trim());
    if (opts.tags?.trim()) form.append("tags", opts.tags.trim());
    if (opts.mode && opts.mode !== "auto") form.append("mode", opts.mode);
    return http<HarnessIconUploaded>("/api/rebrand/harness/icons/upload", { method: "POST", body: form });
  },

  /** Rename, re-tag, hide or show again one of your team's icons, for
   *  everyone (its picture never changes). */
  iconUpdate: (id: string, patch: { name?: string; label?: string; tags?: string[]; hidden?: boolean }) =>
    http<HarnessIconTeamItem>(`/api/rebrand/harness/icons/custom/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  /** The icon plan for a recipe: free, and quick after the scene's first
   *  (state "warming" meanwhile — ask again every second). `askAgain`: a new
   *  take of the AI's choices (one call); `reorder`: also work out the pins
   *  that show icon `glyph` at place `to` (the answer's `reorder`). Neither
   *  key is sent unless asked for. */
  iconPlan: (
    runId: number,
    target: string,
    recipe: HarnessRecipe,
    opts: {
      base?: HarnessBaseRef | null;
      signal?: AbortSignal;
      askAgain?: boolean;
      reorder?: { glyph: string; to: number } | null;
      view?: SceneView | null;
    } = {},
  ) =>
    http<HarnessIconPlan>("/api/rebrand/harness/icons/plan", {
      method: "POST",
      body: JSON.stringify({
        run_id: runId,
        target,
        recipe,
        ...(opts.base ? { base: asBaseRef(opts.base) } : {}),
        ...(opts.askAgain ? { ask_again: true } : {}),
        ...(opts.reorder ? { reorder: opts.reorder } : {}),
        ...viewBody(opts.view),
      }),
      signal: opts.signal,
    }),

  /** The exact frame at t with the plan's icons, built the way the run
   *  builds it (on the version when `versionId` is given). */
  iconMoment: (
    runId: number,
    target: string,
    recipe: HarnessRecipe,
    planKey: string,
    t: number,
    opts: { versionId?: number | null; signal?: AbortSignal; view?: SceneView | null } = {},
  ) =>
    http<HarnessIconMoment>("/api/rebrand/harness/icons/moment", {
      method: "POST",
      body: JSON.stringify({
        run_id: runId,
        target,
        recipe,
        plan_key: planKey,
        t,
        ...(opts.versionId != null ? { version_id: opts.versionId } : {}),
        ...viewBody(opts.view),
      }),
      signal: opts.signal,
    }),

  // --- made in house ---------------------------------------------------------

  /** Free: render a draft made in house exactly as Generate will make it, in
   *  the background (a Generate of the same draft copies it). Asked again,
   *  the same render answers; a new draft stops the old one. 409: the icon
   *  plan shown is gone, or a pattern the preview couldn't draw. */
  houseRender: (
    runId: number,
    target: string,
    recipe: HarnessRecipe,
    opts: { base?: HarnessBaseRef | null; iconsPlanKey?: string | null; signal?: AbortSignal; view?: SceneView | null } = {},
  ) =>
    http<HarnessHouseRender>("/api/rebrand/harness/house/render", {
      method: "POST",
      body: JSON.stringify({
        run_id: runId,
        target,
        recipe,
        ...(opts.base ? { base: asBaseRef(opts.base) } : {}),
        ...(opts.iconsPlanKey ? { icons_plan_key: opts.iconsPlanKey } : {}),
        ...viewBody(opts.view),
      }),
      signal: opts.signal,
    }),

  /** How far a draft render is (404: never asked for here — ask again). */
  houseRenderState: (runId: number, target: string, key: string, signal?: AbortSignal, view?: SceneView | null) =>
    http<HarnessHouseRender>(
      `/api/rebrand/harness/house/render/${encodeURIComponent(key)}?run_id=${runId}&target=${encodeURIComponent(target)}${viewQuery(view)}`,
      { signal },
    ),

  /** Free: a draft made in house, its exact frame at t (as the run makes it). */
  houseMoment: (
    runId: number,
    target: string,
    recipe: HarnessRecipe,
    t: number,
    opts: { base?: HarnessBaseRef | null; iconsPlanKey?: string | null; signal?: AbortSignal; view?: SceneView | null } = {},
  ) =>
    http<HarnessHouseMoment>("/api/rebrand/harness/house/moment", {
      method: "POST",
      body: JSON.stringify({
        run_id: runId,
        target,
        recipe,
        t,
        ...(opts.base ? { base: asBaseRef(opts.base) } : {}),
        ...(opts.iconsPlanKey ? { icons_plan_key: opts.iconsPlanKey } : {}),
        ...viewBody(opts.view),
      }),
      signal: opts.signal,
    }),

  /** A version made ready to add on top of (its frames and layers). The
   *  first time takes ~5-10 s. 409: it is not ready (or on its way); 410:
   *  its video is gone — Change it still works. */
  base: (runId: number, target: string, versionId: number, signal?: AbortSignal, view?: SceneView | null) =>
    http<HarnessBaseInfo>("/api/rebrand/harness/base", {
      method: "POST",
      body: JSON.stringify({ run_id: runId, target, version_id: versionId, ...viewBody(view) }),
      signal,
    }),

  versions: (runId: number, target: string) =>
    http<{ versions: HarnessVersion[] }>(
      `/api/rebrand/harness/versions?run_id=${runId}&target=${encodeURIComponent(target)}`,
    ),

  /** Free: a version made in house whose video is lost, made again (the
   *  same frames), in the background. 409: one Omni made. */
  remake: (versionId: number) =>
    http<HarnessRemake>(`/api/rebrand/harness/versions/${versionId}/remake`, { method: "POST" }),

  /** How far making it again is (404: not asked for here). */
  remakeState: (versionId: number, signal?: AbortSignal) =>
    http<HarnessRemake>(`/api/rebrand/harness/versions/${versionId}/remake`, { signal }),
};

// --- helpers (pure) -----------------------------------------------------------

/** The recipe's first pattern element (older call sites). */
export function patternOf(r: HarnessRecipe): HarnessElement | null {
  return r.elements.find((e) => e.kind === "pattern") ?? null;
}

/** Where the server serves an icon's glyph: SVG for a library icon (in its
 *  fill or bold weight), PNG (black on clear, one shape) for a team icon. */
export function iconGlyphUrl(id: string, weight: "fill" | "bold" = "fill"): string {
  const url = `/api/rebrand/harness/icons/glyph/${encodeURIComponent(id)}`;
  return weight === "bold" ? `${url}?weight=bold` : url;
}

/** Where the server serves a ready version's part `part` as its settled
 *  first frame (GET /versions/{id}/still/{j}) — what the versions list sends
 *  as `stills`, for a version read on its own. */
export function stillUrl(versionId: number, part: number): string {
  return `/api/rebrand/harness/versions/${versionId}/still/${part}`;
}

/** The end a version was made for (30 fps frames): null for the automatic
 *  end (and for every version made before the end grip). */
export function versionEndOf(v: HarnessVersion): number | null {
  const e = (v.options as Record<string, unknown> | undefined)?.end_frame;
  return typeof e === "number" && Number.isFinite(e) ? e : null;
}

/** The recipe a version ran (null: a classic-panel run, before recipes). */
export function recipeOfVersion(v: HarnessVersion): HarnessRecipe | null {
  return v.recipe ?? ((v.options as Record<string, unknown> | undefined)?.recipe as HarnessRecipe | undefined) ?? null;
}

/** Where a version's run really drew one of its patterns (its evidence):
 *  `element_id` is null for a one-pattern run, which records only the one. */
export interface HarnessRecordedPattern {
  element_id: string | null;
  slug: string | null;
  name: string | null;
  colour: string | null;
  layout: HarnessPatternLayout | null;
}

/** The patterns a version's run recorded, in draw order ([] for none, or
 *  for a server that records nothing). */
export function recordedPatterns(v: HarnessVersion): HarnessRecordedPattern[] {
  const ev = (v.evidence ?? null) as Record<string, unknown> | null;
  const one = (p: unknown, id: string | null): HarnessRecordedPattern | null => {
    if (!p || typeof p !== "object") return null;
    const o = p as Record<string, unknown>;
    const layout = o.layout && typeof o.layout === "object" ? (o.layout as HarnessPatternLayout) : null;
    return {
      element_id: typeof o.element_id === "string" ? o.element_id : id,
      slug: typeof o.slug === "string" ? o.slug : null,
      name: typeof o.name === "string" ? o.name : null,
      colour: typeof o.colour === "string" ? o.colour : null,
      layout: layout && typeof layout.placement === "string" ? layout : null,
    };
  };
  if (Array.isArray(ev?.omni_patterns)) {
    return ev.omni_patterns.map((p) => one(p, null)).filter((x): x is HarnessRecordedPattern => !!x);
  }
  const single = one(ev?.omni_pattern, null);
  return single ? [single] : [];
}

/** A base as stored in this browser, if it still has the shape of one. */
export function asBaseRef(x: unknown): HarnessBaseRef | null {
  if (!x || typeof x !== "object") return null;
  const o = x as Record<string, unknown>;
  return typeof o.version_id === "number" && (o.mode === "stack" || o.mode === "rebuild")
    ? { version_id: o.version_id, mode: o.mode }
    : null;
}

export function sameBase(a: HarnessBaseRef | null | undefined, b: HarnessBaseRef | null | undefined): boolean {
  return (a ?? null) === (b ?? null) || (!!a && !!b && a.version_id === b.version_id && a.mode === b.mode);
}

/** True for a warmup reply about a scene the server has prepared. */
export function isWarmup(w: HarnessWarmup | { prepared: false } | null | undefined): w is HarnessWarmup {
  return !!w && "colours" in w && "masks" in w && w.prepared !== false;
}

/** Key-sorted JSON: two recipes that mean the same thing compare equal. */
export function recipeKey(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(recipeKey).join(",")}]`;
  if (v && typeof v === "object") {
    const o = v as Record<string, unknown>;
    return `{${Object.keys(o)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${recipeKey(o[k])}`)
      .join(",")}}`;
  }
  return JSON.stringify(v ?? null);
}
