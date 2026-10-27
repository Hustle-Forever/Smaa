// Rebrand (Talking Head v2) — typed client.
//
// Two arms a new run can ask for: the deterministic "brand" arm (42 graphics
// composited around the untouched footage, whole reel only) and the generative
// Omni experiment, which regenerates pixels. Veo is no longer offered (the
// server ignores run_veo), but its arm stays in these types so older
// experiments that ran it still read back.
//
// Kept out of api.ts on purpose: the experiment ships as its own files so it can
// be removed (or promoted) without touching the shared client. Same-origin via
// the Vite proxy, same error shape as api.ts's `http` ("<status> <detail>").

export type RebrandArmName = "omni" | "veo" | "brand";
/** What a rating can name as the closest to the 42 brand. */
export type RebrandWinner = RebrandArmName | "neither";
/** What a run rebrands: the whole stitched reel, or ONE Videos-stage clip. */
export type RebrandTarget = "reel" | "intro" | "outro" | `scene_${number}`;
export type RebrandIntensity = "subtle" | "balanced" | "bold";
export type RebrandStatus =
  | "queued"
  | "briefing"
  | "running"
  | "ready"
  | "failed"
  | "cancelled"
  | "interrupted";
export type RebrandArmStatus = "queued" | "running" | "ready" | "failed" | "skipped";
/** How the brand arm hides each hard cut. "auto" = slab wipe on any cut that
 *  touches the intro/outro card, hand-off between two scenes. */
export type BrandTransitions = "auto" | "cover" | "handoff" | "short" | "glitch";
/** Brand arm output height class: "1080" = 1080x1920 (9:16) / 1920x1080 (16:9). */
export type BrandResolution = "1080" | "720";

export interface RebrandOptions {
  target: RebrandTarget;
  run_omni: boolean;
  /** Retired: the panel always sends false and the server ignores it. */
  run_veo: boolean;
  intensity: RebrandIntensity;
  notes: string;
  veo_scene_mode: "keep" | "rerender";
  scene_transitions: boolean;
  transition_sec: number;
  veo_quality: "normal" | "quality";
  // --- the brand pattern Omni puts behind the speaker (see RebrandPatternPicker) ---
  /** Put a 42 pattern in the shot at all. Server default true. */
  pattern_on: boolean;
  /** A slug from GET /api/rebrand/patterns. Server default "structure". */
  pattern: string;
  /** An `animations[].value`; null = the pattern's own `default_animation`. */
  pattern_animation: string | null;
  // --- the 42 brand frame (whole reel only: the server forces it off for a
  //     clip, and a clip run never sends these) ---
  /** Composite the 42 graphics around the untouched footage. Server default false. */
  run_brand?: boolean;
  brand_transitions?: BrandTransitions;
  /** null = auto from the script's intro headline, "" = no heading. The server
   *  takes up to 40 chars; the frame fits ~20 on two lines and leaves out (never
   *  cuts) one that doesn't fit (see BRAND_TEXT_FIT in RebrandPanel). */
  brand_heading?: string | null;
  /** null = auto from the hook, "" = no sub-line. Server max 140 chars; about
   *  80-90 fit the sub-line's three lines. */
  brand_sub?: string | null;
  /** Keep the logo bug up while the intro/outro card is on screen. */
  brand_logo_on_cards?: boolean;
  /** Topic icons picked from each scene's script (piggy bank for "free", </> for "code"). Server default true. */
  brand_icons?: boolean;
  /** Slide the segment's pattern over the video's left edge too (not only the bars). Server default true. */
  brand_edge_patterns?: boolean;
  brand_resolution?: BrandResolution;
}

/** The brand frame's report on its text (services/remotion brand42/textFit.ts
 *  TextFitReport; only the fields the panel reads). */
export interface BrandTextFit {
  /** "none" = no text asked for; otherwise the worst scene's outcome. */
  status: "none" | "shown" | "shrunk" | "partial" | "dropped";
  /** One sentence for the user, e.g. "Heading dropped: too long for the letterbox bars." */
  message: string;
  heading_shown: boolean;
  sub_shown: boolean;
  min_heading_scale?: number | null;
  min_sub_scale?: number | null;
}

/** Brand arm geometry — fractions of the frame, 0..1. */
export interface BrandBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** One source clip on the brand arm's timeline (30 fps frames). */
export interface BrandSegment {
  kind: "intro" | "scene" | "outro";
  index: number;
  from: number;
  frames: number;
  /** The picture area; outside it the source is black letterbox bars. */
  content: BrandBox;
  layout: "bars" | "full";
}

/** One join between two segments; the hard cut happens at `frame`. */
export interface BrandSeam {
  frame: number;
  style: "cover" | "handoff" | "short" | "glitch";
  from_kind: BrandSegment["kind"];
  to_kind: BrandSegment["kind"];
}

export interface RebrandStep {
  name: string;
  status: "running" | "done" | "failed";
  detail?: string;
  at?: string;
}

export interface RebrandAutoReview {
  score: number;
  brand_fidelity: number;
  identity_preserved: boolean;
  transition_quality: number;
  strengths: string[];
  issues: string[];
  lesson: string;
}

export interface RebrandArm {
  status: RebrandArmStatus;
  steps?: RebrandStep[];
  error?: string | null;
  note?: string;
  model?: string;
  provider?: string | null;
  /** "house": a studio version made by our own renderer over the untouched
   *  footage ($0); absent on everything Omni made. */
  engine?: "omni" | "house";
  scene_mode?: string;
  scenes_edited?: number;
  scenes_rerendered?: number;
  scenes_total?: number;
  duration_sec?: number;
  auto_review?: RebrandAutoReview | null;
  cost_note?: string;
  started_at?: string;
  finished_at?: string;
  /** Worth knowing about the finished video (a studio version with icons:
   *  "Omni drew its own symbol at 4.2 s — Try again", an icon left out). */
  warnings?: string[];
  // brand arm only
  segments?: BrandSegment[];
  seams?: BrandSeam[];
  /** Output size the brand arm rendered at, width x height (e.g. "1080x1920";
   *  the panel also accepts a bare height class like "1080"). */
  resolution?: string;
  /** The transitions option the arm ran with. */
  transitions?: BrandTransitions;
  /** The heading + sub-line the arm handed the frame to type on (after the
   *  auto-from-script defaults); null = no text. This is what was ASKED for —
   *  `text_fit` says what the frame drew. Absent on runs that predate it. */
  text?: { heading: string; sub: string } | null;
  /** The frame's own report on that text (measured with the real fonts):
   *  shown / shrunk / partial / dropped. Absent on runs that predate it. */
  text_fit?: BrandTextFit | null;
  /** Topic icons the frame showed: Phosphor icon id + reel frames. */
  icons?: { icon: string; from: number; frames: number }[];
  graphics_seconds?: number;
  composite_seconds?: number;
  url: string | null;
  download_url: string | null;
  parts: Record<string, string>;
}

export interface RebrandBrief {
  brand_read?: string;
  palette?: string[];
  background?: { description?: string; motion?: string };
  icons?: { name?: string; look?: string; placement?: string; motion?: string }[];
  logo?: { placement?: string; treatment?: string };
  transition_in?: { style?: string; description?: string };
  transition_out?: { style?: string; description?: string };
  scene_transition?: { style?: string; description?: string };
  lessons_applied?: string[];
  avoid?: string[];
  omni_scene_prompt?: string;
  veo_transition_in_prompt?: string;
  _fallback?: boolean;
  [key: string]: unknown;
}

export interface RebrandEvidence {
  ig_posts_analysed?: number;
  ig_top_posts?: { ig_media_id?: string; likes?: number; caption?: string }[];
  ig_frames_used?: number;
  brand_dna?: { palette?: string[]; logo_treatments?: string[]; transitions?: string[] };
  edit_recipes_used?: number;
  past_experiments_used?: number[];
  logo_attached?: boolean;
  brief_error?: string;
  segments?: { intro: string; scenes: number; outro: string };
}

export interface RebrandRating {
  omni_stars: number | null;
  veo_stars: number | null;
  /** Absent on ratings saved before the brand arm existed. */
  brand_stars?: number | null;
  notes: string;
  rated_at?: string;
}

export interface RebrandExperiment {
  id: number;
  run_id: number;
  target: RebrandTarget;
  status: RebrandStatus;
  stage: string | null;
  options: Partial<RebrandOptions>;
  brief: RebrandBrief | null;
  evidence: RebrandEvidence | null;
  /** Partial: a server that predates an arm doesn't return it at all. */
  arms: Partial<Record<RebrandArmName, RebrandArm>>;
  winner: RebrandWinner | null;
  rating: RebrandRating | null;
  lesson: string | null;
  error: string | null;
  cost_usd: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface RebrandCapabilities {
  omni: {
    model: string;
    providers: string[];
    preferred: string | null;
    configured: boolean;
    /** No GEMINI_API_KEY on the server — Omni is a Gemini-API-only model. */
    needs_api_key?: boolean;
    /** Why it cannot run, in full, when `configured` is false. */
    note?: string | null;
    /** Omni makes nothing new (REBRAND_OMNI, off by default since
     *  2026-09-30): the panel hides it and says what to use instead. Its
     *  older runs still list, play, rate and download. */
    switched_off?: boolean;
  };
  veo: { configured: boolean; model_normal: string; model_quality: string };
}

export interface RebrandRateRequest {
  winner?: RebrandWinner | null;
  omni_stars?: number | null;
  veo_stars?: number | null;
  brand_stars?: number | null;
  notes?: string;
}

// --- brand patterns (the Omni arm's pattern library) -----------------------
// Built-in patterns ship with the server; uploads are analysed by a model on
// the way in, which writes the description, the placement and the animation
// that suits the pattern.

/** One place a pattern can take in the frame. */
export type RebrandPatternPlace = "left" | "right" | "top" | "bottom" | "full";

export interface RebrandPatternCandidate {
  placement: RebrandPatternPlace;
  /** The corner a top/bottom band grows from. */
  anchor: "left" | "right";
  /** Clockwise degrees the art is turned (270 = a quarter turn counter-clockwise). */
  rotate: number;
  /** "left strip", "top-right band", "full frame". */
  label: string;
}

export interface RebrandPattern {
  slug: string;
  name: string;
  source: "builtin" | "upload";
  description: string;
  /** Where in the frame the pattern goes; "auto" = one of `placements`,
   *  chosen per clip by the server from that clip's own framing. */
  placement: RebrandPatternPlace | "auto";
  /** Every place it can take (one for a fixed pattern). Absent from older servers. */
  placements?: RebrandPatternCandidate[];
  /** The same, as a sentence: "the left half of the frame, behind the speaker". */
  where: string;
  /** A value from `animations`. */
  default_animation: string;
  /** Light lines on dark, ~480 px (served by the API). */
  preview_url: string;
  source_file: string | null;
  created_at: string | null;
  /** Only uploads can be deleted. */
  deletable: boolean;
  /** A full-screen transition (Blueprint): added to a scene it shows this
   *  long at the scene's start (s), in over `in_sec`, out over `out_sec` —
   *  not the whole scene. Absent for every other pattern. */
  transition?: { sec: number; in_sec: number; out_sec: number };
}

export interface RebrandPatternAnimation {
  value: string;
  label: string;
  help: string;
}

export interface RebrandPatternList {
  patterns: RebrandPattern[];
  animations: RebrandPatternAnimation[];
  default_pattern: string;
}


// --- the intro -> main seam -------------------------------------------------
// Superseded in the UI by the brand arm (the panel no longer mounts
// PatternSeamCard); the routes and these types stay for that card's file.
// A different kind of thing from the generative arms: no video model is called.
// The two clips stay exactly as they were shot and the 42 pattern kit is
// composited over the join, so it renders in seconds and costs nothing.

export interface SeamLayer {
  pattern: string;
  move: "sweep" | "wipe" | "drift" | "scan" | "stamp";
  direction: "left" | "right" | "up" | "down" | "none";
  start: number;
  end: number;
  box: { x: number; y: number; w: number; h: number };
  rotate: number;
  scale: number;
  color_from: string;
  color_to: string;
  opacity: number;
  blend: string;
  use_art: boolean;
  parallax: number;
  /** The one layer whose exit uncovers the incoming shot. */
  reveal: boolean;
  /** Opacity it settles to over the talking head; 0 means it leaves. */
  residue: number;
  /** 0..1 point at which that hold has faded to nothing. */
  residue_end?: number;
}

export interface SeamRecipe {
  style: string;
  rationale?: string;
  duration_sec: number;
  cut_at_sec: number;
  palette: { from: string; to: string; ink: string; flash: string };
  flash: { at: number; strength: number };
  camera: { shake: number; push: number };
  grain: number;
  vignette: number;
  layers: SeamLayer[];
  /** The strip anything lingering is confined to — the rest of the frame is
   *  the real room and is never drawn on. */
  brand_region?: { side?: string; x: number; y: number; w: number; h: number };
  cover?: "seam" | "shot";
  planner?: "gemini" | "composed" | "override";
  safe_zones?: { what?: string; x: number; y: number; w: number; h: number }[];
}

export interface SeamMoment {
  kind: string;
  ref: string;
  at_sec: number;
  /** True when no frame with real content was found — the clip is dark. */
  fell_back: boolean;
  measured: {
    mean_lum: number;
    stddev: number;
    is_dark: boolean;
    palette: { hex: string; weight: number; sat: number; lum: number }[];
    accent: string;
    accent_sat: number;
    busy: number[];
    edge_energy: number;
    motion: string | null;
    motion_strength: number;
  };
  described: Record<string, unknown>;
}

export interface SeamCritique {
  score?: number;
  reads_as_transition?: boolean;
  covers_face?: boolean;
  palette_continuous?: boolean;
  problems?: string[];
  lesson?: string;
}

export interface SeamResult {
  run_id: number;
  recipe_id: number | null;
  recipe: SeamRecipe;
  clip: string;
  clip_url: string | null;
  reel: string | null;
  reel_url: string | null;
  stills: (string | null)[];
  timing: { pre_sec: number; post_sec: number; dead_tail_trimmed_sec: number };
  moments: { intro_out: SeamMoment; main_in: SeamMoment };
  precedents: { id: number; stars: number | null; style: string | null }[];
  critique: SeamCritique;
}

export interface SeamRequest {
  notes?: string;
  pre_sec?: number;
  post_sec?: number;
  /** "shot" brands a strip across the whole talking head then lets it leave;
   *  "seam" only covers the join. */
  cover?: "shot" | "seam";
  side?: "auto" | "left" | "right";
  /** Seconds the branded panel holds before fading away. */
  hold_sec?: number;
  build_reel?: boolean;
  critique?: boolean;
  debug_safe_zones?: boolean;
  recipe?: SeamRecipe | null;
}

export const REBRAND_ACTIVE: RebrandStatus[] = ["queued", "briefing", "running"];

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  return readJson<T>(res);
}

async function readJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.detail || JSON.stringify(body);
    } catch {
      /* ignore */
    }
    throw new Error(`${res.status} ${detail}`);
  }
  return res.json() as Promise<T>;
}

/** The server's own words from an error thrown above: drops the "<status> "
 *  prefix so a toast reads "Image too large", not "413 Image too large". */
export function rebrandErrorDetail(err: unknown): string {
  const msg = String((err as Error)?.message ?? err);
  return msg.replace(/^\d{3}\s+/, "") || msg;
}

export const rebrandApi = {
  list: (runId: number, target: RebrandTarget = "reel") =>
    http<{ capabilities: RebrandCapabilities; experiments: RebrandExperiment[] }>(
      `/api/rebrand/th-v2/${runId}?target=${encodeURIComponent(target)}`,
    ),

  start: (runId: number, options: RebrandOptions) =>
    http<RebrandExperiment>(`/api/rebrand/th-v2/${runId}`, {
      method: "POST",
      body: JSON.stringify(options),
    }),

  get: (expId: number) => http<RebrandExperiment>(`/api/rebrand/experiments/${expId}`),

  cancel: (expId: number) =>
    http<RebrandExperiment>(`/api/rebrand/experiments/${expId}/cancel`, { method: "POST" }),

  rate: (expId: number, body: RebrandRateRequest) =>
    http<RebrandExperiment>(`/api/rebrand/experiments/${expId}/rate`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Every pattern Omni can put behind the speaker, built-in and uploaded. */
  patterns: () => http<RebrandPatternList>("/api/rebrand/patterns"),

  /** Add a pattern. Slow (~10-40 s): a model reads the image and writes its
   *  description, placement and default animation before this returns. No
   *  Content-Type header — the browser sets the multipart boundary itself. */
  uploadPattern: async (file: File, name?: string) => {
    const form = new FormData();
    form.append("file", file, file.name);
    if (name?.trim()) form.append("name", name.trim());
    const res = await fetch("/api/rebrand/patterns", { method: "POST", body: form });
    return readJson<RebrandPattern>(res);
  },

  /** Uploads only — the server refuses (403) to delete a built-in. */
  deletePattern: (slug: string) =>
    http<{ ok: boolean }>(`/api/rebrand/patterns/${encodeURIComponent(slug)}`, { method: "DELETE" }),

  /** Design and render the intro->main transition. Synchronous: a ~3 s
   *  Remotion render, not a generation, so there is nothing to poll. */
  designSeam: (runId: number, body: SeamRequest = {}) =>
    http<SeamResult>(`/api/rebrand/th-v2/${runId}/seam`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Where the seam's own files are served from. */
  seamFile: (runId: number, part: "clip" | "reel" | `still_${number}`) =>
    `/api/rebrand/th-v2/${runId}/seam/file/${part}`,

  /** Stars go straight into retrieval, so the next seam at a similar cut is
   *  designed knowing whether this one landed. */
  rateSeam: (recipeId: number, body: { stars?: number; notes?: string }) =>
    http<{ ok: boolean; recipe_id: number; stars: number | null }>(
      `/api/rebrand/seams/${recipeId}/rate`,
      { method: "POST", body: JSON.stringify(body) },
    ),
};
