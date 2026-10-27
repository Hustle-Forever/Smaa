// Response shapes mirroring backend Pydantic models / SQL columns.
// Kept hand-rolled so we can adapt freely; the backend's OpenAPI is the source
// of truth.

// The one import in this file. `TrendIdea.tool` IS a Producer tool id, and
// re-declaring the union here would let the two drift apart silently. Safe in
// both directions: `roadmap/types` imports nothing at all (no cycle), and a
// type-only import is erased before it reaches the bundler.
import type { RoadmapTool } from "../components/yas/roadmap/types";

export type Mode = "auto" | "manual";

/**
 * Whether the two things that reach the outside world are set up.
 *
 * GET /api/settings/connections. Never carries a token, an account id or an
 * SMTP password: those live in the server's environment, and this reports only
 * the state a person can act on. `reason` is already a human sentence.
 */
export interface Connections {
  instagram: {
    ready: boolean;
    /** The account posts go to, without the @. Null when not configured. */
    handle: string | null;
    /** Why it is not ready. Null when it is. */
    reason: string | null;
  };
  email: {
    ready: boolean;
    /** Where reminders are sent. Null when not configured. */
    to: string | null;
  };
}

export interface Settings {
  id: number;
  mode: Mode;
  weight_website: number;
  weight_uploads: number;
  weight_text: number;
  weight_visual: number;
  weight_audio: number;
  weight_voice: number;
  weight_visual_identity: number;
  weight_themes: number;
  model: "gpt-4o" | "gpt-4o-mini" | "gpt-4-turbo";
  temperature: number;
  max_tokens: number;
  include_videos: boolean;
  include_images: boolean;
  include_uploads: boolean;
  include_website: boolean;
  custom_instructions: string | null;
  created_at?: string;
  updated_at?: string;
}

export type SettingsPatch = Partial<Omit<Settings, "id" | "created_at" | "updated_at">>;

export interface Health {
  ok: boolean;
  service: string;
  target_url: string;
  supabase_project: string;
  clip_enabled: boolean;
}

// ----- Website -----

export interface WebsitePage {
  id: number;
  url: string;
  page_title: string | null;
  fetched_status: string;
  ai_analyzed_status: string;
  embedded_status: string;
  last_fetched_at: string | null;
  last_analyzed_at: string | null;
  preview: string | null;
  text_chars: number | null;
}

export interface WebsiteList {
  items: WebsitePage[];
  totals: { total: number; fetched: number; analyzed: number; embedded: number };
  last_scrape_at: string | null;
}

// ----- Assets -----

export interface AssetRow {
  id: number;
  page_id: number | null;
  asset_type: string;
  canonical_url: string;
  local_path: string | null;
  width: number | null;
  height: number | null;
  bytes: number | null;
  mime_type: string | null;
  download_status: string;
  analyze_status: string;
  embed_status: string;
  preview: string | null;
  has_text_embed: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface AssetList {
  items: AssetRow[];
  totals: {
    total: number;
    images: number;
    videos: number;
    pdfs: number;
    downloaded: number;
    analyzed: number;
  };
}

// ----- Brand profile -----

export interface BrandProfileRun {
  id: number;
  status: string;
  finished_at: string | null;
  notes: string | null;
}

export interface BrandProfilePayload {
  brand_summary?: string;
  voice?: { pillars?: string[]; do?: string[]; dont?: string[] };
  visual_identity?: {
    primary_colors?: string[];
    secondary_colors?: string[];
    fonts?: string[];
    style_pillars?: string[];
  };
  audiences?: Array<{ label?: string; description?: string }>;
  message_themes?: Array<{ label?: string; description?: string }>;
  visual_themes?: Array<{ label?: string; description?: string }>;
  taglines_in_use?: string[];
  brand_dos?: string[];
  brand_donts?: string[];
  notes_for_producer_agent?: string;
  /** Per-field provenance — values look like "priority_1_canonical_brand_guides". */
  _sources?: Record<string, string>;
}

export interface BrandResponse {
  run: BrandProfileRun | null;
  payload: BrandProfilePayload | null;
  sources: {
    website_pages?: number;
    image_descriptions?: number;
    video_summaries?: number;
    owner_documents?: number;
  };
}

// ----- Agent runs -----

export type AgentId =
  | "website-ingest"
  | "brand-doc-ingest"
  | "brand-synthesizer"
  | "instagram-ingest";

export interface RunSummary {
  id: number;
  target_domain: string;
  sitemap_url?: string | null;
  status: "started" | "completed" | "failed";
  started_at: string | null;
  finished_at: string | null;
  pages_found_count?: number;
  pages_inserted_count?: number;
  errors_count?: number;
  notes: string | null;
}

export interface PhaseStatus {
  name: string;
  processed: number;
  failed: number;
  skipped: number;
  total: number;
  extra?: Record<string, unknown>;
}

export interface RunDetail extends RunSummary {
  phases: Record<string, PhaseStatus>;
  cancel_requested?: boolean;
}

export interface RunStartResponse {
  run_id: number;
  status: string;
}

// ----- Brand documents (Agent 2) -----

export interface BrandDoc {
  id: number;
  doc_kind: "pdf" | "docx" | "text" | "image" | "free_text" | "video";
  source_filename: string | null;
  bytes: number | null;
  mime_type: string | null;
  upload_status: "uploaded" | "processing" | "completed" | "failed";
  uploader_note: string | null;
  summarized?: boolean;
  embedded?: boolean;
  described?: boolean;
  clip_embedded?: boolean;
  text_chars: number | null;
  preview: string | null;
  created_at?: string;
  updated_at?: string;
}

/** Full document detail (GET /documents/{id}) — the actual ingested content,
 *  used by the Recent-uploads drawer. raw_text is the extracted document text. */
export interface BrandDocDetail {
  id: number;
  doc_kind: BrandDoc["doc_kind"];
  source_filename: string | null;
  bytes: number | null;
  mime_type: string | null;
  storage_path: string | null;
  raw_text: string | null;
  ai_description: string | null;
  ai_summary: string | null;
  uploader_note: string | null;
  upload_status: BrandDoc["upload_status"];
  metadata: Record<string, unknown> | null;
  has_embedding: boolean;
  has_visual_embedding: boolean;
  /** True when the ORIGINAL file is on this server (→ show a real preview). */
  has_file?: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface BrandDocList {
  items: BrandDoc[];
  totals: {
    total: number;
    completed: number;
    failed: number;
    in_progress: number;
    by_kind: Record<string, number>;
  };
}

// ----- Producer Agent -----

export type ProducerMode = "auto" | "user_idea" | "reference";

// Spoken-language for talking_head / voiceover_narrative video_types.
// Stored on producer_runs.metadata.dialogue_language, defaults to "en"
// server-side when omitted. Ignored for non-speech video_types.
export type DialogueLanguage = "en" | "ar" | "mixed";

export interface ProducerVideoType {
  id: string; // "talking_head" | ... | "generic"
  name: string;
  description: string;
  default_audio: boolean;
  recommended_aspect: string;
}

export type ProducerRunStatus =
  | "started"
  | "ideas_pending"
  | "idea_approved"
  | "script_pending"
  | "script_approved"
  | "video_pending"
  | "video_ready"
  | "completed"
  | "cancelled"
  | "failed";

export type DecisionStatus = "generated" | "approved" | "declined" | "retried";
export type VideoStatus =
  | "generating"
  | "ready"
  | "failed"
  | "approved"
  | "declined"
  | "retried";

export type NextAction =
  | "review_video"
  | "review_script"
  | "review_idea"
  | "video_generating"
  | "generate_video"
  | "generate_script"
  | "done"
  | "failed"
  | "cancelled"
  | "empty";

export interface ProducerRunRow {
  id: number;
  mode: ProducerMode;
  status: ProducerRunStatus;
  title: string | null;
  brand_profile_run_id: number | null;
  started_at: string | null;
  finished_at: string | null;
  notes: string | null;
  ideas_count?: number;
  // v3 aggregate fields (present on library + list endpoints)
  ideas_pending?: number;
  ideas_approved?: number;
  scripts_pending?: number;
  scripts_approved?: number;
  videos_generating?: number;
  videos_ready?: number;
  videos_approved?: number;
  ideas_needing_script?: number;
  scripts_needing_video?: number;
  next_action?: NextAction;
  metadata?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

export type ProductionFilterKey =
  | "all"
  | "pending"
  | "awaiting_idea"
  | "awaiting_script"
  | "awaiting_video"
  | "generating"
  | "done"
  | "failed";

export interface ProducerIdea {
  id: number;
  run_id?: number;
  idea_index: number;
  title: string;
  concept: string;
  visual_direction: string;
  reason: string;
  style_mood: string;
  status: DecisionStatus;
  decision_reason: string | null;
  decision_at: string | null;
  parent_idea_id: number | null;
  metadata?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

export interface SceneSpec {
  scene_index: number;
  prompt: string;
  duration_sec: number;
  subject?: string;
  action?: string;
  camera?: string;
  lighting?: string;
  style?: string;
}

export interface ProducerScript {
  id: number;
  idea_id: number;
  version_index: number;
  scenes: SceneSpec[];
  veo_prompt: string;
  reference_image_paths: string[] | null;
  status: DecisionStatus;
  decision_reason: string | null;
  decision_at: string | null;
  parent_script_id: number | null;
  created_at?: string;
  updated_at?: string;
}

export interface ProducerVideo {
  id: number;
  script_id: number;
  scene_index: number | null;
  veo_model: string;
  veo_operation_name: string | null;
  gcs_uri: string | null;
  local_path: string | null;
  thumbnail_path: string | null;
  duration_sec: number | null;
  cost_usd: number | null;
  status: VideoStatus;
  decision_reason: string | null;
  decision_at: string | null;
  parent_video_id: number | null;
  metadata?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

export interface ProducerReference {
  id: number;
  run_id: number;
  kind: "image" | "video" | "script_notes" | "prompt";
  original_path: string | null;
  enhanced_path: string | null;
  ai_description: string | null;
  status: "uploaded" | "described" | "enhanced" | "approved" | "declined";
  metadata?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

export type CompositionStatus =
  | "queued"
  | "rendering"
  | "ready"
  | "failed"
  | "approved"
  | "declined"
  | "retried";

export interface ProducerComposition {
  id: number;
  run_id: number;
  template_id: string;
  source_video_ids: number[];
  render_job_id: string | null;
  output_path: string | null;
  duration_sec: number | null;
  cost_usd: number;
  status: CompositionStatus;
  decision_reason: string | null;
  decision_at: string | null;
  error: string | null;
  metadata: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

export interface ProducerRunDetail {
  run: ProducerRunRow;
  ideas: ProducerIdea[];
  scripts: ProducerScript[];
  videos: ProducerVideo[];
  references: ProducerReference[];
  compositions: ProducerComposition[];
}

export interface ProducerBudget {
  spent_usd: number;
  budget_usd: number;
  by_model: Record<string, number>;
}

// Library row types — each carries enough parent context to navigate back.

export interface LibraryIdeaRow extends ProducerIdea {
  run_mode: ProducerMode;
  run_title: string | null;
  run_status: ProducerRunStatus;
}

export interface LibraryScriptRow extends ProducerScript {
  run_id: number;
  idea_title: string;
  run_mode: ProducerMode;
  run_title: string | null;
}

export interface LibraryVideoRow extends ProducerVideo {
  idea_id: number;
  run_id: number;
  idea_title: string;
  run_mode: ProducerMode;
  run_title: string | null;
}

// ----- Instagram ingest (Agent 5) -----

export type InstagramMediaType = "IMAGE" | "VIDEO" | "CAROUSEL_ALBUM";

export interface InstagramPostRow {
  id: number;
  ig_media_id: string;
  account_username: string;
  media_type: InstagramMediaType;
  media_product_type: string | null;
  caption: string | null;
  permalink: string | null;
  timestamp_posted: string | null;
  like_count: number | null;
  comments_count: number | null;
  view_count: number | null;
  children_count: number;
  embedded: boolean;
  content_category: "img_poster" | "img_poster_human" | "img_poster_animated" | "video_interview" | "other" | null;
  preview: string | null;
  asset_count: number;
  first_asset_id: number | null;
  first_asset_type: "image" | "instagram" | null;
  last_metrics_at: string | null;
  first_seen_at: string | null;
}

export interface InstagramPostsList {
  items: InstagramPostRow[];
  totals: {
    total: number;
    images: number;
    videos: number;
    carousels: number;
    embedded: number;
    total_likes: number;
    total_comments: number;
    total_views: number;
    img_poster: number;
    img_poster_human: number;
    img_poster_animated: number;
    video_interview: number;
    other: number;
    unclassified: number;
  };
}

export interface InstagramPostAsset {
  id: number;
  child_index: number;
  ig_child_media_id: string | null;
  asset_id: number;
  asset_type: "image" | "instagram";
  local_path: string | null;
  mime_type: string | null;
  width: number | null;
  height: number | null;
  bytes: number | null;
  ai_description: string | null;
  analyze_status: string;
  embed_status: string;
  video_summary: string | null;
  duration_sec: number | null;
}

export interface InstagramTranscriptSegment {
  segment_index: number;
  start_sec: number;
  end_sec: number;
  text: string;
}

export interface InstagramMetricsSnapshot {
  captured_at: string;
  like_count: number | null;
  comments_count: number | null;
  view_count: number | null;
}

export interface InstagramVideoAnalysis {
  narrative_arc?: string;
  hook_type?: string;
  hook_description?: string;
  pacing?: string;
  scene_count?: number;
  audio_profile?: {
    has_speech?: boolean;
    speech_language?: string;
    music_present?: boolean;
    energy?: string;
  };
  brand_presence?: {
    logo_visible?: boolean;
    logo_treatment?: string;
    palette_called_out?: string[];
    locations?: string[];
    people_described?: string[];
  };
  on_screen_text?: string[];
  cta?: string;
  performance_context?: string;
  why_this_likely_worked?: string;
  transferable_pattern?: string;
}

export interface InstagramScene {
  id: number;
  scene_index: number;
  start_sec: number;
  end_sec: number;
  description: string;
  transcript_excerpt: string | null;
  representative_frame_id: number | null;
  frame_local_path: string | null;
}

export interface InstagramPostDetail {
  id: number;
  ig_media_id: string;
  account_username: string;
  media_type: InstagramMediaType;
  media_product_type: string | null;
  caption: string | null;
  permalink: string | null;
  timestamp_posted: string | null;
  like_count: number | null;
  comments_count: number | null;
  view_count: number | null;
  children_count: number;
  reach: number | null;
  impressions: number | null;
  plays: number | null;
  saved: number | null;
  ai_summary: string | null;
  first_seen_at: string | null;
  last_metrics_at: string | null;
  metadata: Record<string, unknown> | null;
  content_category: "img_poster" | "img_poster_human" | "img_poster_animated" | "video_interview" | "other" | null;
  embedded: boolean;
  video_analysis: InstagramVideoAnalysis | null;
  video_deepened_at: string | null;
  assets: InstagramPostAsset[];
  transcript: InstagramTranscriptSegment[];
  metrics_history: InstagramMetricsSnapshot[];
  scenes: InstagramScene[];
}

export interface InstagramHealth {
  configured: boolean;
  target_username: string;
  api_version: string;
  token_ok?: boolean;
  error?: string;
  account?: {
    username: string | null;
    name: string | null;
    id: string | null;
  };
  db: {
    total_posts: number;
    embedded_posts: number;
    latest_post_at: string | null;
    latest_metrics_at: string | null;
  };
}

// ----- Assets Library (v3) -----

export type ProducerAssetKind = "image" | "video" | "font" | "audio" | "other";
export type ProducerAssetCategory = "character" | "logo" | "mood" | "free" | "sticker";
export type ProducerCharacterArchetype =
  | "emirati_male"
  | "emirati_female"
  | "casual_male"
  | "casual_female";

export interface ProducerAssetRow {
  id: number;
  slug: string;
  asset_kind: ProducerAssetKind;
  category: ProducerAssetCategory | null;
  archetype: ProducerCharacterArchetype | null;
  display_name: string | null;
  storage_bucket: string | null;
  storage_path: string | null;
  local_path: string | null;
  mime_type: string | null;
  bytes: number | null;
  width: number | null;
  height: number | null;
  duration_sec: number | null;
  preview: string | null;
  embedded: boolean;
  usage_count: number;
  last_used_at: string | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown> | null;
  attributes: Record<string, unknown> | null;
  /**
   * Server-minted ~WebP thumbnail URL, served straight from Supabase's CDN.
   * Present only for raster images stored in Supabase (SVG and video cannot be
   * transformed) — fall back to `api.producerAssets.fileUrl(id)` when absent.
   * ALWAYS prefer this in a grid or a chip: `fileUrl` is the full-resolution
   * original and averages ~3.1 MB (up to 9.3 MB for backgrounds).
   */
  thumb_url?: string | null;
}

export interface ProducerAssetDetail extends Omit<ProducerAssetRow, "preview"> {
  ai_description: string;
  source_post_id: number | null;
  source_asset_id: number | null;
  source_frame_id: number | null;
  /**
   * Mid-size (~1400px, contain) signed transform for the detail view — around
   * 150 kB instead of the 9-15 MB original. Null for fonts, video, SVG and
   * local-disk rows; fall back to `fileUrl(id)` then.
   */
  preview_url: string | null;
}

export interface ProducerAssetsList {
  items: ProducerAssetRow[];
  totals: {
    total: number;
    images: number;
    videos: number;
    fonts: number;
    audio: number;
    characters: number;
    stickers: number;
    enabled_count: number;
  };
}

// ----- Yas Producer · Poster + Human (generative) -----

export type YasGenStatus = "generating" | "ready" | "failed" | "saved_to_assets";
export type YasMode = "style_inspired" | "template_fill";
export type YasImageModel = "openai" | "gemini_pro";

export interface YasUploadRef {
  id: number;
  token: string;
  mime_type: string;
  bytes: number;
  width: number | null;
  height: number | null;
  created_at: string;
  url: string; // backend-served preview path
}

export type YasAnimationStatus = "generating" | "ready" | "failed";

export type YasAnimateDuration = 4 | 6 | 8;

export interface YasAnimation {
  id: number;
  yas_generation_id: number;
  status: YasAnimationStatus;
  veo_model: string;
  duration_sec: number;
  with_audio: boolean;
  aspect_ratio: string;
  cost_usd?: string | number | null;
  error?: string | null;
  created_at: string;
  updated_at: string;
  file_url?: string; // backend-served path when ready
}

// ----- Create Animation (standalone intro/outro card; local yas-output/) -----

export type YasAnimationAspect = "9:16" | "16:9";
// Selectable look for the static frame. "voxel" is the default; the rest are
// brand-grounded presets (see backend yas_animation_styles.py). "asis" is not
// offered in the picker: it is stamped on images the user BROUGHT IN, where we
// refuse to assume a look, and it only changes the Veo motion prompt.
export type YasAnimationStyle =
  | "voxel"
  | "wireframe"
  | "popup"
  | "pattern"
  | "asis";
export type YasAnimationGenStatus =
  | "frame_ready"
  | "animating"
  | "ready"
  | "failed";

export interface YasAnimationFrameRequest {
  prompt: string;
  aspect_ratio: YasAnimationAspect;
  style: YasAnimationStyle;
}

export interface YasAnimationAnimateRequest {
  duration_sec?: YasAnimateDuration | null;
  with_audio?: boolean | null;
}

// Bring an image the user ALREADY has into the animation store as a frame, so
// it can be animated like a generated one. Exactly ONE source per call.
export interface YasAnimationFromImageRequest {
  poster_gen_id?: number;  // a poster from Create Poster
  asset_id?: number;       // an image saved in the Assets library
  upload_token?: string;   // a file off the user's computer (posterHuman.uploadRef)
  aspect_ratio?: YasAnimationAspect; // inferred from the image when omitted
  prompt?: string;         // mood/music context for the motion prompt
  style?: YasAnimationStyle;
}

// One Create-Animation record: a static frame that may become a Veo clip.
export interface YasAnimationGen {
  id: number;
  prompt: string;
  status: YasAnimationGenStatus;
  aspect_ratio: YasAnimationAspect;
  style?: YasAnimationStyle; // backend defaults legacy records to "voxel"
  duration_sec?: number | null;
  with_audio?: boolean | null;
  veo_model?: string | null;
  frame_cost_usd?: string | number | null;
  veo_cost_usd?: string | number | null;
  error?: string | null;
  saved_asset_id?: number | null; // producer_assets id once the frame is saved
  created_at: string;
  updated_at: string;
  frame_url?: string; // static first frame, present once frame_ready
  file_url?: string;  // MP4, present once ready
}

// One "Generate frame" / "Explore" click returns a batch of frame variants
// (Midjourney style), mirroring YasPosterBatchResponse.
export interface YasAnimationBatchResponse {
  items: YasAnimationGen[];
}

// ----- General AI Video (unbranded Veo generator; own store) -----
// Same two-step lifecycle as Create-Animation (frame_ready → animating → ready),
// but no 42 logo and reference pictures feed the still.
export interface YasGeneralVideoGen {
  id: number;
  prompt: string;
  status: YasAnimationGenStatus;
  /** Veo clips are 9:16 or 16:9; a still made for Seedance may be any SeedanceAspect. */
  aspect_ratio: SeedanceAspect;
  duration_sec?: number | null;
  with_audio?: boolean | null;
  reference_upload_ids?: string | null;
  /** The engine that animated (or is animating) the still. null on old rows = Veo. */
  engine?: YasGeneralVideoEngine | null;
  resolution?: SeedanceResolution | null;
  veo_model?: string | null;
  frame_cost_usd?: string | number | null;
  veo_cost_usd?: string | number | null;
  seedance_cost_usd?: string | number | null;
  error?: string | null;
  created_at: string;
  updated_at: string;
  frame_url?: string; // static first frame, present once frame_ready
  file_url?: string; // MP4, present once ready
}

// ----- General AI Video engines -----
// The still is animated by Veo (the default) or Seedance 2.5 via Higgsfield.
// Seedance makes longer clips and more shapes; GET /general-video/engines says
// what each accepts right now (and whether Seedance's keys are on the server).
export type YasGeneralVideoEngine = "veo" | "seedance";
export type SeedanceAspect = "16:9" | "4:3" | "1:1" | "3:4" | "9:16" | "21:9";
export type SeedanceResolution = "480p" | "720p" | "1080p";

export interface YasGeneralVideoEngines {
  default: YasGeneralVideoEngine;
  veo: { connected: boolean; aspects: YasAnimationAspect[]; durations: number[] };
  seedance: {
    connected: boolean;
    not_connected_message: string | null;
    aspects: SeedanceAspect[];
    duration_min: number;
    duration_max: number;
    resolutions: SeedanceResolution[];
    default_resolution: SeedanceResolution;
    price_per_sec_usd: Record<SeedanceResolution, number>;
    budget_usd: number | null;
  };
}

export interface YasGeneration {
  id: number;
  topic: string;
  reference_asset_ids: number[];
  reference_upload_ids: number[];
  parent_generation_id: number | null;
  feedback_text: string | null;
  retry_kind: "tweak" | "pivot" | null;
  prompt_used: string;
  storage_bucket: string | null;
  storage_path: string | null;
  local_path: string | null;
  mime_type: string | null;
  bytes: number | null;
  width: number | null;
  height: number | null;
  status: YasGenStatus;
  saved_asset_id: number | null;
  cost_usd: string | number | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  file_url?: string; // backend-served path when ready
  mode?: YasMode;
  student_upload_id?: number | null;
  image_model?: YasImageModel;
  animation?: YasAnimation | null;
  // Siblings from one 2x2 Generate/Explore batch share this (smallest id in the
  // set). Grid grouping is client-side; this is just the shared marker.
  variant_set_id?: number | null;
}

// Envelope returned by /poster-human/generate | template-fill | variants — a
// 2x2 batch of 4 (mirrors YasPosterBatchResponse for Create Poster).
export interface YasGenerationBatchResponse {
  items: YasGeneration[];
  remaining_this_hour?: number;
}

// ----- Library / history -----

export type YasLibraryType =
  | "talking_head"
  | "talking_head_v2"
  | "poster"
  | "poster_human"
  | "animation"
  | "video_editor"
  | "general_video"
  | "broll";

/** Lightweight tile shape for the Yas Producer Library grid (GET /library). */
export interface YasLibraryItem {
  id: number;
  type: YasLibraryType;
  topic: string | null;
  status: YasGenStatus;
  image_model: YasImageModel | null;
  cost_usd: string | number | null;
  created_at: string;
  /** Last edit time — drives the Video Editor "recently edited" ordering + resume. */
  updated_at?: string | null;
  thumbnail_url: string | null;
  video_url: string | null;
  has_video: boolean;
  /** Full-res download URL (backend proxy route — forces a filename and serves the
   *  ORIGINAL, never a transform thumbnail). Display URLs above may be fast public
   *  CDN / transform URLs, so downloads use this instead. */
  download_url?: string | null;
  /** A non-final video clip to preview (hover-play) when no FINAL deliverable
   *  exists yet — e.g. a Talking Head's main scene clip before stitching. Kept
   *  separate from video_url/has_video so lifecycle (pending vs finished) stays
   *  keyed on whether the FINAL video exists. */
  preview_url?: string | null;
  /** What the tile is best able to show: "final" | "scene" | "still" | "none". */
  preview_kind?: "final" | "scene" | "still" | "none" | null;
  /** Talking-head only: stitch-pipeline status. Drives the tile's pending
   * placeholder when no final stitched video exists yet. Null for poster+human.
   * Video Editor reuses this with values draft|rendering|ready|failed. */
  composition_status?: YasThCompositionStatus | string | null;
  /** Producer Roadmap step this project was left on, if mid-roadmap
   * (preview|calendar|approval). Null/undefined when not in the roadmap.
   * Drives the Library's "Continue → <Step>" resume action. */
  roadmap_step?: string | null;
}

export interface YasLibraryList {
  items: YasLibraryItem[];
}

/** One work category in the Dashboard breakdown (per-status counts). */
export interface YasStatCategory {
  key: string;
  label: string;
  total: number;
  ready: number;
  generating: number;
  failed: number;
  saved: number;
}

/** Aggregate studio summary for the command-center Dashboard (GET /stats). */
export interface YasStats {
  generations: {
    total: number;
    by_status: {
      ready: number;
      generating: number;
      failed: number;
      saved: number;
    };
    categories: YasStatCategory[];
    // Convenience rollups for the KPI tiles.
    reels: number;
    posters: number;
    editor_projects: number;
  };
  animations: {
    total: number;
    ready: number;
    generating: number;
    failed: number;
  };
  spend: {
    generations_usd: number;
    animations_usd: number;
    total_usd: number;
  };
  activity_14d: { day: string; count: number }[];
  /** Content lifecycle funnel for the Dashboard. pending = finished but not yet
   *  scheduled; in_process = scheduled; declined = cancelled; approved = posted. */
  funnel: {
    pending: number;
    in_process: number;
    declined: number;
    approved: number;
  };
}

/** Dashboard Instagram card — OUR posted content (not the 42AD ingest).
 *  likes/comments are null when publishing isn't configured (UI shows "—"). */
export interface InstagramOwnPost {
  published_at: string | null;
  permalink: string | null;
  /** null when engagement metrics aren't available (publishing unconfigured). */
  likes: number | null;
  comments: number | null;
  /** Play/view count (insights edge). null when not readable for this media. */
  views?: number | null;
}

export interface InstagramOwnSummary {
  total_posts: number;
  total_likes: number | null;
  total_comments: number | null;
  total_views?: number | null;
  latest_post_at: string | null;
  metrics_available: boolean;
  /** Recent published posts (newest first, capped) for the engagement chart. */
  posts?: InstagramOwnPost[];
}

export interface YasGenerateRequest {
  topic: string;
  reference_asset_ids: number[];
  reference_upload_ids: number[];
  parent_generation_id?: number | null;
  feedback_text?: string | null;
  retry_kind?: "tweak" | "pivot" | null;
  image_model?: YasImageModel | null;
  /** Output shape, as on the poster-only request. Omitted means 1:1, which is
   *  what every Poster + Human render was before this. */
  aspect?: YasPosterAspect;
}

// ----- Yas Talking Head (3rd category) ----------------------------------

export type YasThCompositionStatus =
  | "queued"          // Veos in flight
  | "videos_ready"    // all 3 Veos done; preview-before-stitch checkpoint
  | "rendering"       // Remotion job in flight (user clicked Stitch)
  | "ready"           // final composition MP4 ready
  | "failed";

export interface TalkingHeadScript {
  hook: string;
  dialogue: string;
  subject: string;
  action: string;
  camera: string;
  lighting: string;
  palette: string[];
  style: string;
  mood: string;
  dialogue_language: "en" | "ar" | "mixed";
  // v3 — topic-aware text for intro / outro cards. GPT writes these; the
  // image-gen step uses them as the literal text on the card (replacing
  // any text on a user-provided reference). Subtitles may be empty.
  intro_headline: string;
  intro_subtitle: string;
  outro_headline: string;
  outro_subtitle: string;
  // v6 — per-scene dialogue lines. Single-scene runs get a 1-element
  // array mirroring the top-level dialogue/hook fields.
  scenes?: { dialogue: string; hook: string }[];
}

/** How a picked intro/outro image is used: "as_is" (the default: the card IS
 *  the image), "new_vibe" (a new design in its style) or "text_only" (runs
 *  made before "as_is": the image redrawn with the script's words). */
export type TalkingHeadRefMode = "as_is" | "new_vibe" | "text_only";

/** Output video aspect ratio. Veo supports only 9:16 and 16:9 (no 1:1). */
export type TalkingHeadAspect = "9:16" | "16:9";

/** How close the camera is: Close-up (head and shoulders), Above the hip
 *  (waist up, the default for a new reel) or Full body (head to toe). */
export type TalkingHeadFraming = "close" | "above_hip" | "full_body";

/** Veo tier for the run's clips. "normal" = standard model, "quality" = premium. */
export type TalkingHeadVideoQuality = "normal" | "quality";

export interface TalkingHeadRefs {
  character_asset_id: number | null;
  character_upload_id: number | null;
  bg_asset_id: number | null;
  bg_upload_id: number | null;
  intro_asset_id: number | null;
  intro_upload_id: number | null;
  outro_asset_id: number | null;
  outro_upload_id: number | null;
  /** Direct-video bookend: a yas_uploads id (video) used as-is (no card-gen). */
  intro_video_upload_id?: number | null;
  outro_video_upload_id?: number | null;
  /** Serve URL for the direct-video bookend, resolved by the run serializer so
   *  the uploaded clip previews on resume (live session uses the slot URL). */
  intro_video_url?: string | null;
  outro_video_url?: string | null;
  /** How a picked intro/outro reference is used (default "as_is"). */
  intro_ref_mode?: TalkingHeadRefMode;
  outro_ref_mode?: TalkingHeadRefMode;
  /** A previous reel's card picked as the intro/outro ("Previous videos"). */
  intro_card_id?: number | null;
  outro_card_id?: number | null;
  /** "Keep card text": a kept card keeps its own words (off: a headline
   *  that differs from the script is swapped for the script's words). */
  intro_keep_text?: boolean;
  outro_keep_text?: boolean;
}

/** What happened to a kept card's words. */
export interface TalkingHeadTextSwap {
  /** kept: "Keep card text"; unchanged: nothing to swap; swapped; failed:
   *  the swap did not read back right (the card keeps its own words);
   *  unread: the card's text could not be read. */
  status: "kept" | "unchanged" | "swapped" | "failed" | "unread";
  warning: string | null;
  swapped: { kind: "headline" | "subtitle"; old: string; new: string }[];
  /** A "swapped" card whose swap kept some lines' own words (the warning
   *  names them); absent on an older backend. */
  kept?: { kind: "headline" | "subtitle"; old: string; new: string; why: string }[];
}

/** One card of the intro/outro picker's "Previous videos": an earlier
 *  reel's current card (GET /talking-head-v2/cards/previous). */
export interface TalkingHeadPreviousCard {
  card_id: number;
  role: "intro" | "outro";
  run_id: number;
  topic: string;
  url: string;
  card_text: string | null;
  /** Its exact shape, or null for another shape (fitted when used). */
  aspect: TalkingHeadAspect | null;
  width: number | null;
  height: number | null;
  as_is: boolean;
  /** Its finished clip, reused (no new Veo) when the card is used as it is
   *  at that size with its words unchanged. */
  clip_animation_id: number | null;
  clip_style: "slow_zoom" | "animate_icons" | null;
  created_at: string | null;
}

export interface TalkingHeadImageModels {
  main: YasImageModel;
  intro: YasImageModel;
  outro: YasImageModel;
}

export interface TalkingHeadMain {
  status: string;
  url: string | null;
  width: number | null;
  height: number | null;
  error: string | null;
}

export interface TalkingHeadCard {
  id: number | null;
  status: string;
  card_type: "intro" | "outro" | null;
  width?: number | null;
  height?: number | null;
  url: string | null;
  error?: string | null;
  /** Set when the card is sourced from a saved producer_assets row or an
   *  ephemeral yas_uploads row rather than auto-generated. */
  asset_id?: number;
  upload_id?: number;
  /** The picked previous reel's card (source "card"), shown while no card
   *  was made from it. */
  card_id?: number;
  source?: "asset" | "upload" | "card";
  /** The pick itself, shown while no card was made from it. */
  card_missing?: boolean;
  /** The card IS the picked image (ref mode "as_is"). */
  as_is?: boolean;
  ref_mode?: TalkingHeadRefMode | null;
  /** The card's text: asked of a drawn card ("planned") or read off a kept
   *  one ("detected"); null when not known yet. */
  card_text?: string | null;
  card_text_source?: "planned" | "detected" | null;
  /** How its clip moves: "veo" (its icons), "zoom" (the slow zoom), null
   *  when not planned yet. */
  motion?: "veo" | "zoom" | null;
  /** False when the card was made from another image than the one picked
   *  now (it is not used; make it again). */
  matches_refs?: boolean;
  /** A kept card: whether it keeps its own words, what happened to them,
   *  whether its clip is a previous reel's (reused, no Veo), and the
   *  previous reel's card it was copied from. */
  keep_text?: boolean;
  text_swap?: TalkingHeadTextSwap | null;
  reused_clip?: boolean;
  source_card_id?: number | null;
}

export interface TalkingHeadAnimation {
  id: number;
  status: "generating" | "ready" | "failed";
  duration_sec: number | null;
  with_audio: boolean | null;
  cost_usd: number | null;
  error: string | null;
  url: string | null;
  /** An intro/outro clip: what it turned out to be ("veo_replaced": Veo
   * changed the card, so it is the slow zoom; "veo_flagged": it changed the
   * card but the slow zoom could not be made; "veo_unchecked": the card was
   * gone), and why it was replaced or flagged. null for a failed clip and
   * other clips; absent on an older backend. */
  motion?: "veo" | "zoom" | "veo_replaced" | "veo_checking" | "veo_flagged" | "veo_unchecked" | null;
  motion_reason?: string | null;
}

/** One clip the next "Make videos" / "Update clips" press would make, and
 * its price from the backend meter's own Veo rates ("veo_or_zoom": a card
 * not planned yet, $0 as the slow zoom up to usd_max as Veo). */
export interface TalkingHeadVideosPlanClip {
  target: string;
  why: "missing" | "failed" | "stale";
  engine: "veo" | "zoom" | "veo_or_zoom";
  model: string | null;
  seconds: number;
  audio: boolean;
  usd: number;
  usd_max: number;
}

export interface TalkingHeadVideosPlan {
  clips: TalkingHeadVideosPlanClip[];
  count: number;
  usd: number;
  usd_max: number;
  any_clip: boolean;
}

export interface TalkingHeadAnimations {
  intro: TalkingHeadAnimation | null;
  main:  TalkingHeadAnimation | null;
  outro: TalkingHeadAnimation | null;
}

/** v6 — per-scene record in the fetch_run payload. Always present (length
 *  1 for single-scene runs, length N for multi-scene). */
export interface TalkingHeadScene {
  index: number;
  dialogue: string;
  hook: string;
  user_authored: boolean;
  keyframe_url: string | null;
  /** v2 editor — keyframe (still) generation status. "generating" drives the
   * live spinner shown after an image apply auto-regenerates the still. */
  keyframe_status?: "generating" | "ready" | "failed" | null;
  /** While generating: what the background work is ("crop": a free framing
   * crop; "reshoot": the image model re-shoots it; "regen": made again);
   * null otherwise, absent on an older backend. */
  keyframe_work?: "crop" | "reshoot" | "regen" | null;
  /** The keyframe still's true size (null when there is none; absent on an
   * older backend). Lets the Stills tile show its real shape. */
  width?: number | null;
  height?: number | null;
  animation: TalkingHeadAnimation | null;
  /** Override the global character reference for THIS scene. null = inherit. */
  character_asset_id: number | null;
  character_upload_id: number | null;
  /** Override the global bg reference for THIS scene. null = inherit. */
  bg_asset_id: number | null;
  bg_upload_id: number | null;
  /** Image-gen error for this scene's keyframe — populated when the
   * generator raised. Null when the scene succeeded or was never attempted. */
  error?: string | null;
  /** v2 editor — the current Veo clip no longer matches this scene's keyframe/
   * dialogue/refs (set on image/ref/dialogue change, cleared on video retry). */
  video_stale?: boolean;
  /** v2 editor — user toggled this scene out of the final stitch. */
  excluded?: boolean;
  /** Per-scene Veo tier (per-still toggle). Falls back to the run's video_quality. */
  video_quality?: TalkingHeadVideoQuality;
  /** Keyframe is the character image used VERBATIM (New bg OFF), not a generated
   * still. Drives the "↻ New bg" toggle's restored state on resume. */
  as_is?: boolean;
  /** An earlier version of the still to go back to (the tile's Undo). Absent
   * on an older backend. */
  can_undo?: boolean;
  /** Framing: the scene's own (null: it follows the run's), the one a new
   * still is made at, and the one its still shows ("as_photo": the photo's
   * own; null: no still). Absent on an older backend. */
  framing?: TalkingHeadFraming | null;
  framing_effective?: TalkingHeadFraming;
  framing_used?: TalkingHeadFraming | "as_photo" | null;
  /** A re-shot still whose face shows another framing than the one asked
   * for says so (null: none). Absent on an older backend. */
  framing_warning?: string | null;
}

/** v2 editor — an archived ("removed") scene, kept restorable in the project
 *  tree. Lightweight shape returned by fetch_run. */
export interface TalkingHeadArchivedScene {
  archived_index: number;
  dialogue: string;
  hook: string;
  has_keyframe: boolean;
  has_animation: boolean;
  archived_at?: string | null;
}

export interface TalkingHeadRun {
  id: number;
  topic: string;
  status: string;
  /** Legacy single-model field — kept for back-compat. Prefer image_models. */
  image_model: YasImageModel;
  image_models: TalkingHeadImageModels;
  lock_character: boolean;
  aspect_ratio?: TalkingHeadAspect;
  /** The Video size can no longer change: a still or card has been made (the
   * backend refuses a new size with a 409). Absent on an older backend. */
  aspect_locked?: boolean;
  video_quality?: TalkingHeadVideoQuality;
  /** How close the camera is ("close" on a run made before framing
   * existed). Absent on an older backend. */
  framing?: TalkingHeadFraming;
  /** Per-segment Veo tier (per-still toggle). Falls back to `video_quality`. */
  intro_video_quality?: TalkingHeadVideoQuality;
  outro_video_quality?: TalkingHeadVideoQuality;
  main_video_quality?: TalkingHeadVideoQuality;
  script: TalkingHeadScript | null;
  /** Who is on camera when no character is picked. null on a run made before
   * the default speaker; absent on an older backend. */
  speaker?: TalkingHeadSpeaker | null;
  refs: TalkingHeadRefs;
  main: TalkingHeadMain;
  intro: TalkingHeadCard | null;
  outro: TalkingHeadCard | null;
  animations: TalkingHeadAnimations;
  /** Legacy alias for animations.main */
  animation: TalkingHeadAnimation | null;
  /** Vision-picked motion enum (one of INTRO_STYLES) — surfaced for the UI. */
  intro_animation_style?: string | null;
  /** Vision-picked motion enum (one of OUTRO_STYLES). */
  outro_animation_style?: string | null;
  composition_status: YasThCompositionStatus | null;
  composition_url: string | null;
  saved_asset_id: number | null;
  /** Finalize (again) — sent only by a backend that has it (absent: older).
   * reel_ready: every included clip made, none out of date. composition_seq:
   * renders asked for; composition_good_seq: the reel being served (the
   * `?v=` of composition_url); composition_error: why the newest render
   * failed while the last good reel stays; saved_current: the reel being
   * served is the one saved to assets. */
  reel_ready?: boolean;
  composition_seq?: number | null;
  composition_good_seq?: number | null;
  composition_error?: string | null;
  saved_current?: boolean;
  /** The reel scene by scene (the Rebrand stage): null until every included
   * clip is made; absent on an older backend. */
  reel?: ReelBlock | null;
  created_at: string | null;
  updated_at: string | null;
  /** v6 — number of main scenes in this run (1..5). 1 = legacy single-scene. */
  scene_count: number;
  /** v6 — unified per-scene array (always emitted, length === scene_count). */
  scenes: TalkingHeadScene[];
  /** Image-gen error messages — populated when the corresponding tile failed
   * to generate. Null when the tile succeeded or hasn't been attempted yet. */
  intro_error?: string | null;
  outro_error?: string | null;
  main_error?: string | null;
  /** v2 editor — bookend/main per-clip flags (scene flags live on each scene). */
  intro_video_stale?: boolean;
  outro_video_stale?: boolean;
  main_video_stale?: boolean;
  intro_excluded?: boolean;
  outro_excluded?: boolean;
  main_excluded?: boolean;
  /** v2 editor — removed scenes (project tree) + whether the stitched MP4 is
   * out of date relative to the current (included) clips. */
  archived_scenes?: TalkingHeadArchivedScene[];
  composition_stale?: boolean;
  /** What the next "Make videos" / "Update clips" press makes and costs
   * (nothing is made without that press). Absent on an older backend. */
  videos_plan?: TalkingHeadVideosPlan;
  /** What one still image costs now, USD (a Refine, a Regenerate, a
   * re-shoot): the meter's rate for the image model in use. Absent on an
   * older backend (lib/thStill stillImageCost). */
  still_image_usd?: number | null;
}

/** A version's state as the reel sees it: being made, failed (or stopped),
 * made on an older clip of the scene, its video not on this computer, or
 * ready. */
export type ReelPickState = "ready" | "running" | "failed" | "lost" | "other_clip";

/** One segment of the reel (services/th_reel ReelState.block): an included
 * main scene, or a bookend when something is found for it, in reel order.
 * `pick` is the newest choice (the version on its way, else the pick) and
 * its state; `ships` what actually goes in (null: the original); `making` a
 * version still being made for it; `end` its end in the reel's 30 fps
 * frames (`user`: set by hand, `auto`: the tail-flash cut, `full`: the
 * whole clip). */
export interface ReelTarget {
  target: string;
  role: "intro" | "main" | "outro";
  label: string;
  src: string | null;
  thumb_url: string | null;
  pick: { version_id: number; state: ReelPickState } | null;
  ships: number | null;
  making: number | null;
  end: { frame: number; auto: number | null; user: number | null; full: number };
}

/** GET/PUT …/reel and fetch_run.reel. `inputs_sha` is what Finalize sends
 * back (expect_sha). */
export interface ReelBlock {
  ready: boolean;
  targets: ReelTarget[];
  inputs_sha: string;
  /** This server's studio opens the intro and outro (their tiles show only
   *  then; an older server refuses them). */
  bookends?: boolean;
}

/** PUT …/reel/{target}: a key left out is kept. pick null = the original;
 * end_frame null = automatic. */
export interface ReelChoicePatch {
  pick?: number | null;
  end_frame?: number | null;
}

// ----- Talking Head v2 editor request bodies -----

export interface TalkingHeadV2EditRequest {
  topic?: string;
  scene_count?: number;
  restore_archived?: boolean;
  lock_character?: boolean;
  image_models?: TalkingHeadImageModels;
  scene_dialogues?: (string | null)[];
  /** Per-scene hook overrides (Look-phase inline edit). null = leave unchanged. */
  scene_hooks?: (string | null)[];
  scene_overrides?: (TalkingHeadSceneRefOverride | null)[];
  /** Intro/outro card wording (Look-phase inline edit). Baked into the card
   * image → editing marks that card stale. */
  intro_headline?: string;
  intro_subtitle?: string;
  outro_headline?: string;
  outro_subtitle?: string;
  /** Global one-shot "New bg" intent for the run-level bg slot (single-scene or
   * scenes inheriting the global bg). Not persisted. */
  bg_mode?: "keep" | "new";
  character_asset_id?: number | null;
  character_upload_id?: number | null;
  bg_asset_id?: number | null;
  bg_upload_id?: number | null;
  intro_asset_id?: number | null;
  intro_upload_id?: number | null;
  outro_asset_id?: number | null;
  outro_upload_id?: number | null;
  intro_video_upload_id?: number | null;
  outro_video_upload_id?: number | null;
  intro_ref_mode?: TalkingHeadRefMode;
  outro_ref_mode?: TalkingHeadRefMode;
  intro_card_id?: number | null;
  outro_card_id?: number | null;
  intro_keep_text?: boolean;
  outro_keep_text?: boolean;
  aspect_ratio?: TalkingHeadAspect;
  video_quality?: TalkingHeadVideoQuality;
  /** The Look step's Speaker field: who is on camera when no character is
   * picked. Free text, or a chip ("Emirati man" / "Emirati woman") the
   * backend turns into a default cast. */
  speaker_subject?: string;
  /** The run's framing (the Look step). Stills whose framing changes are
   * re-shot at it. */
  framing?: TalkingHeadFraming;
  /** Per scene (a Stills tile's framing buttons): null = unchanged,
   * "inherit" = follow the run's. A single-scene run's is the run's. */
  scene_framings?: (TalkingHeadFraming | "inherit" | null)[];
}

/** The run's speaker (fetch_run.speaker): the default Emirati cast
 * (alternating man / woman), the one the topic fixed, or the user's. */
export interface TalkingHeadSpeaker {
  subject: string;
  gender: "man" | "woman" | null;
  source: "default_emirati" | "topic" | "user";
}

/** Optional "look" payload sent with generate-previews in the first-creation
 * flow (refs/overrides/lock/engine picked on the Look page after the script was
 * generated at Topic→Look). `bg_mode` drives "use my uploaded photo as-is". */
export interface TalkingHeadV2GeneratePreviewsRequest {
  character_asset_id?: number | null;
  character_upload_id?: number | null;
  bg_asset_id?: number | null;
  bg_upload_id?: number | null;
  intro_asset_id?: number | null;
  intro_upload_id?: number | null;
  outro_asset_id?: number | null;
  outro_upload_id?: number | null;
  intro_video_upload_id?: number | null;
  outro_video_upload_id?: number | null;
  intro_ref_mode?: TalkingHeadRefMode;
  outro_ref_mode?: TalkingHeadRefMode;
  intro_card_id?: number | null;
  outro_card_id?: number | null;
  intro_keep_text?: boolean;
  outro_keep_text?: boolean;
  scene_overrides?: (TalkingHeadSceneRefOverride | null)[];
  lock_character?: boolean;
  image_models?: TalkingHeadImageModels;
  bg_mode?: "keep" | "new";
  aspect_ratio?: TalkingHeadAspect;
  video_quality?: TalkingHeadVideoQuality;
  /** How close the camera is, for the run and per scene (null = unchanged,
   * "inherit" = follow the run's). Every still is made at it. */
  framing?: TalkingHeadFraming;
  scene_framings?: (TalkingHeadFraming | "inherit" | null)[];
}

export type TalkingHeadV2ClipRef = "main" | "intro" | "outro" | `scene_${number}`;

export interface TalkingHeadV2ExcludeRequest {
  which: TalkingHeadV2ClipRef;
  excluded: boolean;
}

export interface TalkingHeadV2BatchItem {
  which: TalkingHeadV2ClipRef;
  feedback_text?: string | null;
  image_models?: TalkingHeadImageModels;
}

export interface TalkingHeadV2BatchRequest {
  items: TalkingHeadV2BatchItem[];
  target: "images" | "videos" | "both";
}

export interface TalkingHeadSceneRefOverride {
  character_asset_id?: number | null;
  character_upload_id?: number | null;
  bg_asset_id?: number | null;
  bg_upload_id?: number | null;
  /** v2 one-shot intent for a scene with NO bg ref: "new" regenerates a fresh
   * background while keeping the character; "keep" (default) passes the still
   * through untouched. Not persisted. */
  bg_mode?: "keep" | "new";
}

export interface TalkingHeadGenerateScriptRequest {
  topic: string;
  /** Optional user-authored dialogue. When set, GPT uses it verbatim as
   * `script.dialogue`; every other field is still derived from the topic.
   * Equivalent to user_dialogues=[user_dialogue] for scene_count=1. */
  user_dialogue?: string | null;
  /** v6 — multi-scene reels. 1..5. Defaults to 1 (current behaviour). */
  scene_count?: number;
  /** v6 — per-scene verbatim dialogue. Length should equal scene_count. */
  user_dialogues?: (string | null)[];
  /** v6 — per-scene character/bg overrides. Length should equal scene_count. */
  scene_overrides?: (TalkingHeadSceneRefOverride | null)[];
  image_model?: YasImageModel;
  image_models?: {
    main?: YasImageModel;
    intro?: YasImageModel;
    outro?: YasImageModel;
  };
  lock_character?: boolean;
  character_asset_id?: number | null;
  character_upload_id?: number | null;
  bg_asset_id?: number | null;
  bg_upload_id?: number | null;
  intro_asset_id?: number | null;
  intro_upload_id?: number | null;
  outro_asset_id?: number | null;
  outro_upload_id?: number | null;
  intro_video_upload_id?: number | null;
  outro_video_upload_id?: number | null;
  intro_ref_mode?: TalkingHeadRefMode;
  outro_ref_mode?: TalkingHeadRefMode;
  /** Bookend generate-toggle (v2). false = never generate that bookend (no
   * card, no Veo clip) + omit from the final stitch. Defaults true. */
  generate_intro?: boolean;
  generate_outro?: boolean;
  aspect_ratio?: TalkingHeadAspect;
  video_quality?: TalkingHeadVideoQuality;
  /** How close the camera is (v2). Absent = "above_hip". */
  framing?: TalkingHeadFraming;
}

export interface TalkingHeadRetryRequest {
  /** v6 — accepts legacy main/intro/outro AND scene_<N> for multi-scene tiles. */
  which: "main" | "intro" | "outro" | `scene_${number}`;
  feedback_text: string;
  /** Optional engine override — the user's current toggle wins over the
   * engine stored on the run. Backend also persists this back to
   * metadata.image_models so subsequent retries stay consistent. */
  image_models?: TalkingHeadImageModels;
}

export interface YasTemplateFillRequest {
  topic: string;
  template_asset_id: number;
  // Student face: EITHER an uploaded photo OR a saved Human Character asset.
  student_upload_id?: number | null;
  student_asset_id?: number | null;
  parent_generation_id?: number | null;
  feedback_text?: string | null;
  image_model?: YasImageModel | null;
  /** Output shape. The same three the poster pipeline accepts; the backend
   *  sends it to Gemini as a real ImageConfig parameter. */
  aspect?: YasPosterAspect;
}

// Yas Producer · Poster-only mode (Yasin's path).
// Same shape as YasGeneration plus variant-tree + rate-limit fields.
// Create-Poster look. Voxel is the default; voxel_classic is the same style with
// the original scene recipe (props + staging) still in its prompt, kept so the
// two can be compared on one topic; pattern + popup are brand-grounded graphic
// styles. Keep in sync with the backend registry (poster_styles.py).
export type YasPosterStyle = "voxel" | "voxel_classic" | "pattern" | "popup";

/** Create-Poster output shape. Square is the default; the other two exist
 *  because Nano Banana Pro takes the ratio as a real API parameter, so these are
 *  enforced rather than requested. Keep in sync with POSTER_ASPECTS in
 *  backend/app/services/poster_pipeline.py. */
export type YasPosterAspect = "1:1" | "9:16" | "16:9";

export interface YasPosterOnlyGeneration extends YasGeneration {
  remaining_this_hour?: number;
  variant_set_id?: number | null; // siblings in one 2×2 batch share this
  image_usage?: YasImageUsage;
  style?: YasPosterStyle; // backend defaults legacy records to "voxel"
}

// One Generate/Explore click returns a batch of 4 variants (Midjourney style).
export interface YasPosterBatchResponse {
  items: YasPosterOnlyGeneration[];
  remaining_this_hour?: number;
}

// ----- Yas Producer · Human Character (Ideation → Human Character) -----
// Photorealistic person generator. Reuses the poster two-stage pipeline (with no
// logo / no text), generating N distinct casting candidates per click. A chosen
// candidate is saved as a `character` asset (Talking Head presenter reference).
export interface YasHumanCharacterGeneration extends YasGeneration {
  remaining_this_hour?: number;
  variant_set_id?: number | null; // siblings in one batch share this
}

// One Generate click returns a batch of N candidate people.
export interface YasHumanCharacterBatchResponse {
  items: YasHumanCharacterGeneration[];
  remaining_this_hour?: number;
}

// User-selectable output format. Keep in sync with the backend
// HUMAN_CHARACTER_ASPECTS tuple.
export type YasHumanCharacterAspect = "1:1" | "3:4" | "9:16" | "16:9";

export interface YasHumanCharacterGenerateRequest {
  topic: string;
  aspect?: YasHumanCharacterAspect;
}

// How attached references are used in the two-stage poster pipeline.
//   "style"      — refs are a style reference (texture/lighting), not pasted in.
//   "background" — the ref image becomes the literal poster background.
export type YasImageUsage = "style" | "background";

// Poster-only generate request. Stage 1 (concepts) is fixed; the Stage-2 image
// engine is selectable via `image_model` (Nano Banana vs OpenAI gpt-image-1).
export interface YasPosterOnlyGenerateRequest {
  topic: string;
  reference_asset_ids: number[];
  reference_upload_ids: number[];
  image_usage?: YasImageUsage;
  image_model?: YasImageModel; // "gemini_pro" (Nano Banana, default) | "openai"
  style?: YasPosterStyle; // "voxel" (default) | "voxel_classic" | "pattern" | "popup"
  aspect?: YasPosterAspect; // "1:1" (default) | "9:16" | "16:9"
}

// ----- Frame extraction (video IG categories → Asset import) -----

export type FrameExtractionMode =
  | "talking_head"
  | "animated_poster"
  | "intro"
  | "outro";

export interface ExtractedFrame {
  frame_id: number;
  timestamp_sec: number;
  url: string; // backend frame proxy
}

export interface PersonCandidate {
  person_label: string;
  description: string;
  suggested_archetype: ProducerCharacterArchetype | null;
  best_frame: ExtractedFrame;
  alternates: ExtractedFrame[];
  score: number;
}

export interface FrameCandidate {
  rank: number;
  frame: ExtractedFrame;
}

export interface IntroOutroFrameCandidate {
  rank: number;
  frame: ExtractedFrame;
  text_completeness: number;
  visual_dominance: number;
  clarity: number;
  note: string | null;
}

export interface TalkingHeadExtractResult {
  mode: "talking_head";
  candidates: PersonCandidate[];
  degrade_reason: string | null;
  dominant_content: string | null;
  suggest_animated_poster: boolean;
  cached?: boolean;
}

export interface AnimatedPosterExtractResult {
  mode: "animated_poster";
  candidates: FrameCandidate[];
  confidence: "high" | "medium" | "low";
  rejected: Array<{ frame: ExtractedFrame; reason: string | null }>;
  cached?: boolean;
}

export interface IntroOutroExtractResult {
  mode: "intro" | "outro";
  position: "intro" | "outro";
  candidates: IntroOutroFrameCandidate[];
  confidence: "high" | "medium" | "low";
  rejected: Array<{ frame: ExtractedFrame; reason: string | null }>;
  degrade_reason: string | null;
  cached?: boolean;
}

export type ExtractFramesResult =
  | TalkingHeadExtractResult
  | AnimatedPosterExtractResult
  | IntroOutroExtractResult;

export type FrameAssetContentCategory =
  | "img_poster_animated"
  | "video_interview"
  | "img_intro"
  | "img_outro";

export interface FromFrameRequest {
  post_id: number;
  asset_id: number;
  frame_id: number;
  content_category: FrameAssetContentCategory;
  archetype?: ProducerCharacterArchetype | null;
  display_name?: string;
  person_label?: string;
  person_description?: string;
  analyze_attributes?: boolean;
}

// ----- Drive folder bulk import -----

export interface DriveFolderEntry {
  id: string;
  name: string;
  mime_type: string;
  size: number | null;
}

export interface DriveFolderEntrySupported extends DriveFolderEntry {
  inferred_kind: ProducerAssetKind;
}

export interface FolderPreviewResult {
  folder_id: string;
  supported: DriveFolderEntrySupported[];
  unsupported: DriveFolderEntry[];
  subfolders: DriveFolderEntry[];
  counts: {
    images: number;
    videos: number;
    fonts: number;
    other: number;
    unsupported: number;
    subfolders: number;
    total_supported: number;
  };
}

export interface FolderImportResult {
  folder_id: string;
  imported: Array<{
    id: number;
    drive_id: string;
    name: string;
    asset_kind: ProducerAssetKind;
    slug: string;
  }>;
  skipped: Array<{ id: string; name: string; mime_type: string; reason: string }>;
  failed: Array<{ id: string; name: string; reason: string }>;
  subfolders: Array<{ id: string; name: string }>;
  counts: {
    imported: number;
    skipped: number;
    failed: number;
    subfolders: number;
  };
}

// ----- SSE event types -----

export type SSEEvent =
  | { type: "run_started"; run_id: number; ts?: number; [k: string]: any }
  | { type: "phase_started"; run_id: number; phase: string; ts?: number }
  | {
      type: "phase_completed";
      run_id: number;
      phase: string;
      processed: number;
      failed: number;
      skipped: number;
      total: number;
      extra?: Record<string, unknown>;
      ts?: number;
    }
  | {
      type: "progress";
      run_id: number;
      phase: string;
      processed: number;
      total: number;
      ts?: number;
    }
  | { type: "run_completed"; run_id: number; result?: any; ts?: number; [k: string]: any }
  | { type: "run_cancelled"; run_id: number; reason?: string; ts?: number }
  | { type: "run_failed"; run_id: number; error?: string; ts?: number }
  | { type: "no_active_run"; run_id: number }
  | { type: string; [k: string]: any };

// ===========================================================================
// Video Editor (multi-track NLE) — shared timeline contract.
//
// Mirrors backend services/yas_video_editor.py + the Remotion `video-edit`
// composition props (services/remotion/src/compositions/schema.ts). All time
// fields are INTEGER FRAMES at 30fps. Keep this structurally identical to the
// other two copies — drift here means Player preview != export.
// ===========================================================================

export type VideoEditAspect = "9:16" | "16:9" | "1:1";

export type TransitionPreset =
  | "none"
  | "fade"
  | "slide"
  | "slideLeft"
  | "slideRight"
  | "slideUp"
  | "slideDown"
  | "wipe"
  | "flip"
  | "clockWipe"
  | "iris"
  | "zoom"
  | "blur";

export type TransitionTiming = "linear" | "spring";

/** One animation keyframe: frame (relative to clip start) → value. */
export interface VideoEditKeyframe {
  f: number;
  v: number;
}

/** Per-clip animatable properties. Empty array = static default. */
export interface VideoEditKeyframes {
  opacity?: VideoEditKeyframe[];
  scale?: VideoEditKeyframe[];
  x?: VideoEditKeyframe[];
  y?: VideoEditKeyframe[];
  rotation?: VideoEditKeyframe[];
  volume?: VideoEditKeyframe[];
}

export interface VideoEditTransition {
  preset: TransitionPreset;
  durationInFrames: number;
  timing?: TransitionTiming;
}

export interface VideoEditClipSource {
  kind: "animation" | "upload" | "composition" | "asset" | "still";
  animation_id?: number | null;
  upload_id?: number | null;
  composition_id?: number | null; // yas_generations.id (stitched final MP4)
  still_id?: number | null; // yas_generations.id (keyframe / card / poster image)
  asset_id?: number | null; // producer_assets.id (image / font / audio asset)
  /** Resolved at fetch for the Player; backend re-resolves for export. */
  url?: string | null;
  naturalDurationInFrames?: number | null;
  /** Audio-track clip from a true audio file → <Audio> (else hidden <OffthreadVideo>). */
  audioFile?: boolean;
}

/** Visual layer kind. Absent/null = "video" (back-compat with stored docs). */
export type VideoEditClipKind = "video" | "audio" | "text" | "shape" | "image";
export type VideoEditShapeVariant = "rect" | "ellipse" | "line" | "triangle" | "star";

/** Static CSS filters applied to a clip layer (Player AND export). */
export interface VideoEditClipFilters {
  brightness?: number; // 0..2 (1 = no change)
  contrast?: number; // 0..2
  saturate?: number; // 0..2
  blur?: number; // 0..40 px
}

export interface VideoEditClipText {
  content: string;
  fontFamily: string;
  fontUrl?: string | null; // brand font asset URL; null = Google/system
  fontSize: number; // px in composition space
  color: string;
  fontWeight?: number;
  textAlign?: "left" | "center" | "right";
  lineHeight?: number;
  background?: string | null;
  padding?: number;
  stroke?: string | null;
  strokeWidth?: number;
  shadow?: string | null;
}

export interface VideoEditClipShape {
  variant: VideoEditShapeVariant;
  fill: string;
  stroke?: string | null;
  strokeWidth?: number;
  cornerRadius?: number;
  width: number; // px in composition space
  height: number;
}

export interface VideoEditClip {
  id: string;
  trackId: string;
  /** Absent for sourceless layers (text / shape). */
  source?: VideoEditClipSource;
  /** Browser-playable URL the Player uses (set by fetch_project). */
  sourceUrl?: string | null;
  trimBefore: number; // into the SOURCE (frames)
  trimAfter: number; // into the SOURCE (frames)
  from: number; // placement on the TIMELINE (frames)
  durationInFrames: number;
  keyframes?: VideoEditKeyframes;
  transitionIn?: VideoEditTransition | null;
  transitionOut?: VideoEditTransition | null;
  muted?: boolean;
  /** Playback rate for video/audio (1 = normal, 2 = 2x, 0.5 = slow-mo).
   * durationInFrames already reflects the re-timed length. */
  playbackRate?: number | null;
  fit?: "cover" | "contain";
  // --- Iteration 2 layer types + effects (all optional, back-compat) ---
  kind?: VideoEditClipKind | null;
  text?: VideoEditClipText | null;
  shape?: VideoEditClipShape | null;
  filters?: VideoEditClipFilters | null;
  /** Letterbox fill for video/image when it doesn't cover the frame:
   * "blur" = blurred cover-copy, a hex = solid colour, null = none. */
  background?: string | null;
}

/** A reference held in a project's media bin (sidebar). */
export interface MediaBinEntry {
  kind: "animation" | "upload" | "composition" | "still";
  id: number;
  title?: string | null;
  durationInFrames?: number | null;
}

export interface VideoEditTrack {
  id: string;
  kind: "video" | "audio";
  index: number; // z-order: higher = composited on top
  muted?: boolean;
  locked?: boolean;
  name?: string | null;
}

export interface VideoEditProjectMeta {
  title: string;
  aspect: VideoEditAspect;
  width: number;
  height: number;
  fps: number;
  background: string;
  durationInFrames: number;
  /** Per-project media pool shown in the sidebar Media tab. */
  media_bin?: MediaBinEntry[];
  /** Opt-in editor mode chosen at create time. "b_roll" shows the B-roll
   * panel (drop clips → AI orders/joins/bookends/music). null/absent = normal. */
  preset?: "b_roll" | null;
}

export type VideoEditRenderStatus = "draft" | "rendering" | "ready" | "failed";

export interface VideoEditRender {
  composition_status: VideoEditRenderStatus;
  render_job_id?: string | null;
  composition_output_path?: string | null;
  composition_path?: string | null;
  composition_error?: string | null;
  rendered_rev?: number | null;
}

/** The full editable document (props passed to the Remotion composition). */
export interface VideoEditTimeline {
  project: VideoEditProjectMeta;
  tracks: VideoEditTrack[];
  clips: VideoEditClip[];
}

/** GET /video-editor/{id} payload — what the editor store hydrates from. */
export interface VideoEditorProject {
  id: number;
  title: string;
  status: YasGenStatus;
  created_at: string | null;
  updated_at: string | null;
  schema_version: number;
  project: VideoEditProjectMeta;
  tracks: VideoEditTrack[];
  clips: VideoEditClip[];
  history: { rev: number; updated_at?: string | null };
  render: VideoEditRender;
}

/** GET /video-editor/{id}/history — one saved version of the timeline. */
export interface VideoEditorRevision {
  rev: number;
  at: string | null;
  actor: string | null;
  summary: string | null;
  project: VideoEditProjectMeta;
  tracks: VideoEditTrack[];
  clips: VideoEditClip[];
}

/** PATCH /video-editor/{id}/timeline body. */
export interface VideoEditorSaveRequest {
  project: VideoEditProjectMeta;
  tracks: VideoEditTrack[];
  clips: VideoEditClip[];
  base_rev?: number | null;
}

/** AI assist (Phase 3) — POST /video-editor/{id}/ai/* features. */
export type VideoEditAiFeature =
  | "analyze"
  | "popups"
  | "compose"
  | "edit"
  | "voice"
  | "music"
  | "bookend"
  | "sfx"
  | "broll"
  // Agentic loop: the model looks at the video, thinks, and edits with tools.
  | "agent";

export type VideoEditAiJobStatus =
  | "queued"
  | "analyzing"
  | "planning"
  | "applying"
  | "generating_card"
  | "waiting_provider"
  | "done"
  | "failed";

/** One background AI job per project (POST /ai/* → {job}; poll GET /ai/status). */
export interface VideoEditAiJob {
  id: string;
  feature: VideoEditAiFeature;
  status: VideoEditAiJobStatus;
  progress: string;
  error: string | null;
  plan_id: string | null;
  ops_applied: number;
  ops_failed: { index: number; op: string; error: string }[];
  rev_after: number | null;
  started_at: string | null;
  finished_at: string | null;
  /** Agent feature only — one entry per tool call, oldest first, appended live
   * while the job runs (the tail is kept, so it's capped server-side). */
  agent_steps?: VideoEditAgentStep[];
  /** Agent feature only — which reasoning brain actually ran the loop. */
  agent_provider?: string;
  agent_model?: string;
}

/** One tool call the agent made (job.agent_steps). */
export interface VideoEditAgentStep {
  step: number;
  tool: string;
  ok: boolean;
  result: string;
  images?: number;
}

/** GET /video-editor/{id}/ai/status payload. */
export interface VideoEditAiStatus {
  job: VideoEditAiJob | null;
  analysis_ready: boolean;
  /** The server's authoritative "still running" verdict — it applies the same
   * staleness rule that guards new jobs, so a blob left at `planning` by a
   * backend restart reports false. Always prefer this over reading `status`:
   * adopting a dead job pauses autosave and undo for the rest of the session.
   * Optional so the client still works against an older backend. */
  job_active?: boolean;
}

/** GET /video-editor/ai/providers — which reasoning brain is live, and why.
 * Probed + cached server-side (vertex-claude → anthropic-claude → gemini). */
export interface VideoEditAiProviders {
  provider: string;
  model: string;
  ok: boolean;
  reason: string;
  checked_at: number;
  configured: {
    google_cloud_project: boolean;
    claude_vertex_region: string;
    claude_model: string;
    anthropic_api_key_set: boolean;
    anthropic_model: string;
    gemini_model: string;
    forced_provider: string;
  };
}

/** A brand font from the Assets Library (GET /video-editor/assets/fonts).
 * Merged with the curated Google fonts (lib/fonts.ts) in the Text picker. */
export interface VideoEditorFont {
  id: number;
  family: string;
  file_url: string;
  preview_url: string;
}

/** An uploaded audio/music file (GET /video-editor/assets/audio). */
export interface VideoEditorAudio {
  id: number;
  title: string;
  durationInFrames: number;
  url: string;
  created_at: string | null;
}

/** The user's saved voice clone ("my voice") — one per user, made from a short
 * sample via ElevenLabs instant voice cloning (POST /video-editor/ai/voice-clone). */
export interface VideoEditVoiceClone {
  voice_id: string;
  name: string;
  created_at: string;
}

/** User-facing filter category a picker source belongs to. */
export type VideoSourceCategory =
  | "talking_head_v2"
  | "talking_head"
  | "poster_animation"
  | "video_edit"
  | "final"
  | "still"
  | "poster"
  | "upload"
  | "bookend_intro"
  | "bookend_outro";

/** A pickable source clip for the Video Editor media bin (GET /video-editor/sources). */
export interface VideoSource {
  kind: "animation" | "upload" | "composition" | "still";
  id: number;
  category?: VideoSourceCategory;
  title: string;
  durationInFrames: number;
  naturalDurationInFrames: number;
  with_audio: boolean;
  width: number | null;
  height: number | null;
  url: string; // play URL (same-origin proxy, Range-served)
  thumbnail_url: string;
  created_at: string | null;
}

export interface VideoSourceList {
  items: VideoSource[];
  total: number;
  limit: number;
  offset: number;
}

// ----- Campaigns (autonomous Producer tab, beta) -------------------------
// Mirrors backend/app/services/campaign_store.py and routers/campaigns.py.
// Hand-maintained: there is no codegen in this project, so a backend field
// rename compiles clean here and only fails at runtime.

/**
 * An item's place in the pipeline. Two of these are HUMAN GATES:
 *   `ready_for_review` waits for Approve/Decline (gate 1, content), and
 *   `scheduled` waits for a human to press Post (gate 2, publishing —
 *   which goes through the existing roadmap approve endpoint).
 * Nothing is published by a machine.
 */
export type CampaignItemStatus =
  | "planned"           // the week's slot exists; nothing generated yet
  | "generating"        // claimed by the worker, AI running
  | "ready_for_review"  // ← GATE 1
  | "approved"          // content approved; the worker will book it
  | "scheduled"         // booked; ← GATE 2
  | "published"         // a human posted it
  | "rejected"          // declined, no regenerations left
  | "failed"            // generation gave up
  | "publish_failed"    // the Post attempt failed; retryable by hand
  | "skipped"           // Instagram isn't configured (dev)
  | "expired"           // its cycle ended before anyone reviewed it
  | "cancelled";        // dropped before going out

export type CampaignStatus = "active" | "paused" | "archived";
export type CampaignItemKind = "poster" | "video";

/** What the planner invented for one slot. */
export interface CampaignIdea {
  title?: string;
  angle?: string;
  key_message?: string;
  cta?: string;
  weekday_hint?: number;
}

/** One planned post. */
export interface CampaignItem {
  id: number;
  campaign_id: number;
  cycle_index: number;
  cycle_start: string | null;
  slot_index: number;
  kind: CampaignItemKind;
  status: CampaignItemStatus;
  idea: CampaignIdea;
  idea_title: string | null;
  /** Which store the generated media lives in — picks the file-URL builder. */
  gen_kind: "poster" | "animation" | null;
  gen_id: number | null;
  caption: string | null;
  proposed_time: string | null;
  scheduled_post_id: number | null;
  attempt: number;
  decline_note: string | null;
  error: string | null;
  cost_usd: number;
  notified_at: string | null;
  published_at: string | null;
  permalink: string | null;
  claimed_at: string | null;
  created_at: string;
  updated_at: string;
  /** Only present on the cross-campaign review queue. */
  campaign_name?: string;
  poster_style?: string;
}

/** A posting slot, in the brand's local time (Asia/Dubai). 0 = Monday. */
export interface CampaignPostingWindow {
  weekday: number;
  time: string; // "HH:MM"
}

export interface Campaign {
  id: number;
  name: string;
  topic_brief: string;
  status: CampaignStatus;
  /** Why a machine paused it (e.g. budget) — absent for a manual pause. */
  status_reason: string | null;
  posters_per_week: number;
  videos_per_week: number;
  duration_weeks: number | null; // null = runs until stopped
  posting_windows: CampaignPostingWindow[];
  poster_style: string;
  video_duration_sec: number;
  video_with_audio: boolean;
  weekly_budget_usd: number | null;
  notify_email: boolean;
  cycle_index: number;
  next_cycle_at: string | null;
  last_cycle_at: string | null;
  created_at: string;
  updated_at: string;
  // --- derived by the router, so the cards do no arithmetic ---
  cycle_cost_estimate_usd: number;
  per_week_total: number;
  // --- present on the list endpoint only (aggregated in one query) ---
  item_count?: number;
  awaiting_review?: number;
  in_flight?: number;
  scheduled_count?: number;
  published_count?: number;
  failed_count?: number;
  cycle_cost_usd?: number;
  next_post_at?: string | null;
}

export interface CampaignList {
  items: Campaign[];
}

export interface CampaignDetail {
  campaign: Campaign;
  items: CampaignItem[];
  spent_this_cycle_usd: number;
}

export interface CampaignReviewQueue {
  items: CampaignItem[];
}

export interface CampaignSummary {
  awaiting_review: number;
}

export interface CampaignCreateRequest {
  name: string;
  topic_brief?: string;
  posters_per_week?: number;
  videos_per_week?: number;
  duration_weeks?: number | null;
  posting_windows?: CampaignPostingWindow[];
  poster_style?: string;
  video_duration_sec?: number;
  video_with_audio?: boolean;
  weekly_budget_usd?: number | null;
  notify_email?: boolean;
  start_now?: boolean;
}

export type CampaignPatchRequest = Partial<
  Omit<CampaignCreateRequest, "start_now"> & { status: "active" | "paused" }
>;

// ----- Trend Finder (/api/trends) -----
//
// Watches competitor Instagram accounts and surfaces posts that OVERPERFORMED
// for their own account, then scores how practical the idea is for 42 to make.
// `outperformance` is the hero metric: a ratio where 1.0 = typical for that
// account and 4.7 = "4.7x their own average". Raw likes are meaningless across
// accounts of different sizes, so never lead with them.

export type TrendRegion = "uae" | "gcc" | "international";
export type TrendCompetitorStatus = "active" | "excluded";
export type TrendPostStatus = "new" | "saved" | "dismissed" | "used";
export type TrendMediaType = "IMAGE" | "VIDEO" | "CAROUSEL_ALBUM";
/**
 * Which discovery channel found a trend.
 *   `competitor` — a post from a watched Instagram account
 *   `news`       — a story from the AI News collector
 * Both live in one table and go through the same scoring and ranking; this only
 * says where the row came from, and drives the category tabs in the feed.
 */
export type TrendSource = "competitor" | "news";
export type TrendSort =
  | "opportunity" | "outperformance" | "engagement" | "recent" | "fit" | "viral";

/** Whether competitor scanning can work at all (GET /api/trends/health).
 *  `reason` distinguishes "no token configured" from "token there, API down". */
export interface TrendHealth {
  ok: boolean;
  /** the IG account whose token is in use — present only when ok */
  account?: string | null;
  account_id?: string | null;
  name?: string | null;
  error?: string | null;
  reason?: "not_configured" | "unreachable" | null;
}

/** Header numbers for the Trends page (GET /api/trends/stats). */
export interface TrendStats {
  competitors_active: number;
  competitors_excluded: number;
  posts_total: number;
  posts_unscored: number;
  posts_saved: number;
  /** split of posts_total by discovery channel */
  posts_competitor: number;
  posts_news: number;
  /** average fit_score across scored posts — null before anything is scored */
  avg_fit: number | null;
  last_scanned_at: string | null;
}

/**
 * ONE RUN of the morning search (part of GET /api/trends/news/health).
 *
 * `status` is the shared vocabulary from `news_store.RUN_STATUSES`, and the
 * screen renders one sentence per value - so a status the backend invents and
 * this list does not know renders as nothing at all. Kept as a union rather
 * than a bare string precisely so adding one fails the build.
 *
 * `empty` is NOT an error: some mornings there is genuinely nothing worth
 * posting about, and the whole point of refusing to pad is that this happens.
 */
export type TrendNewsRunStatus =
  | "running"
  | "ok"
  | "empty"
  | "ungrounded"
  | "search_failed"
  | "judge_failed"
  | "no_budget"
  | "unavailable"
  | "cancelled";

export interface TrendNewsRun {
  id: number;
  status: TrendNewsRunStatus;
  /** candidate stories the search came back with */
  found: number;
  /** how many survived judging and the plain-language checks */
  kept: number;
  /**
   * Why the rest were dropped: `{reason: count}`, keyed from a fixed set of
   * short sentences the backend writes. Kept for diagnosing a quiet morning,
   * NOT shown on any screen: a story the judge threw out is one nobody should
   * be shown, its reason included.
   */
  drops: Record<string, number>;
  error: string | null;
  /** "auto" for the morning run, "manual" for the button */
  trigger: string;
  started_at: string;
  finished_at: string | null;
}

/** What we look for, and how much of it (GET/PUT /api/trends/news/config). */
export interface TrendNewsConfig {
  id: number;
  /** the search brief, in the owner's own words - the whole configuration */
  brief: string;
  stories_per_day: number;
  enabled: boolean;
  next_run_at: string;
  updated_at: string;
}

/**
 * A config patch. Every field optional, and only what is sent is applied, so a
 * screen that toggled the switch cannot clobber a brief edited in another tab.
 */
export interface TrendNewsConfigPatch {
  brief?: string;
  stories_per_day?: number;
  enabled?: boolean;
}

/** One of the briefs the AI offers to write (POST /news/brief/suggest). */
export interface TrendNewsBriefSuggestion {
  /** 2-4 words, for the card that holds it */
  label: string;
  /** one sentence on how this one differs from the others */
  difference: string;
  /** the text itself, pasted straight into the brief field if chosen */
  brief: string;
}

/**
 * Did it run, what did it find, and when does it run next
 * (GET /api/trends/news/health).
 *
 * `ever_ran` distinguishes "has never run" from "ran and found nothing" - the
 * two need completely different empty states, and conflating them is how a
 * working feature ends up looking broken on a quiet week.
 *
 * `ok: false` is the shape a FAILED read comes back as. The route never raises,
 * because the feed has to render whatever this says.
 */
/**
 * One thing Industry Signal follows (GET /api/trends/news/watch).
 *
 * `kind` is worked out from what was typed, not asked for: anything with a dot
 * and no spaces is a website and is stored as its host alone, everything else
 * is a name. A name can be a company, a person or a subject, and the search
 * prompt treats all three the same way.
 */
export interface TrendNewsWatch {
  id: number;
  kind: "name" | "site";
  value: string;
  created_at: string;
}

/**
 * An outlet a kept story actually came from.
 *
 * NOT a source we poll. Nothing is polled: the search reads the open web, so
 * this is the record of where stories have come back from, which is the only
 * honest answer to "what do you watch".
 */
export interface TrendNewsOutlet {
  outlet: string;
  stories: number;
  last_seen: string | null;
}

export interface TrendNewsHealth {
  ok: boolean;
  error?: string | null;
  enabled: boolean;
  brief: string;
  /** Where the "how many" control opens. A remembered preference, not a
   *  schedule: nothing runs on its own any more. */
  stories_per_day: number;
  ever_ran: boolean;
  /** the most recent run that finished, or null before the first one */
  last_run: TrendNewsRun | null;
  /** a search is in flight right now */
  running: boolean;
  /** the last fortnight, newest first */
  runs: TrendNewsRun[];
  /**
   * The feed's fit floor. Zero now, deliberately: the judge already dropped
   * everything wrong for this audience, so a second bar here would hide
   * stories that passed the only test that matters. Kept because the feed
   * reads it by name.
   */
  min_fit_default: number;
}

/** A watched Instagram account (GET /api/trends/competitors). */
export interface TrendCompetitor {
  id: number;
  username: string;
  display_name: string | null;
  biography: string | null;
  profile_picture_url: string | null;
  followers_count: number | null;
  media_count: number | null;
  category: string | null;
  region: TrendRegion;
  /** why this account is worth watching (AI's reason, or the user's note) */
  why: string | null;
  /** "excluded" = kept on record but not scanned — the include/don't-include toggle */
  status: TrendCompetitorStatus;
  source: "manual" | "ai_research";
  median_engagement: number | null;
  last_scanned_at: string | null;
  last_scan_status: string | null;
  last_scan_error: string | null;
  posts_found: number | null;
  created_at: string | null;
  updated_at: string | null;
}

/** One discovered post, with the competitor joined on (GET /api/trends/posts). */
export interface TrendPost {
  id: number;
  /** null on a news story — it belongs to no watched account */
  competitor_id: number | null;
  /** unique external key; news rows namespace theirs as `news:<sha1(url)>` */
  ig_media_id: string;
  username: string;
  permalink: string | null;
  /** null on a news story — an article has no Instagram media format */
  media_type: TrendMediaType | null;
  media_product_type: string | null;
  media_url: string | null;
  thumbnail_url: string | null;
  /** relative path of the locally cached thumb; use api.trends.thumbUrl(id) to show it */
  local_thumb_path: string | null;
  caption: string | null;
  posted_at: string | null;

  like_count: number | null;
  comments_count: number | null;
  /** VIDEO only — null for IMAGE / CAROUSEL_ALBUM */
  view_count: number | null;
  engagement: number | null;
  engagement_rate: number | null;
  /** the hero metric: multiples of this account's own average (1.0 = typical) */
  outperformance: number | null;

  /** the transferable ANGLE 42 could reuse */
  topic: string | null;
  topic_summary: string | null;
  theme: string | null;
  /** 0-100: can 42 realistically make this? */
  fit_score: number | null;
  fit_reason: string | null;
  uae_relevance: number | null;
  /** how 42 would make it */
  suggested_angle: string | null;
  suggested_format: string | null;
  effort: string | null;
  tags: string[] | null;
  opportunity_score: number | null;
  scored_at: string | null;
  status: TrendPostStatus;

  /** which channel found this — "news" rows come from the AI News collector */
  source: TrendSource;
  /** own descriptive axes; a competitor row inherits region from its account */
  region: TrendRegion | null;
  /** ISO-ish language code, when the source reports one */
  language: string | null;
  /** where the conversation is happening, e.g. "twitter", "news" */
  platform: string | null;
  /**
   * Source-specific extras that don't deserve their own column — the baseline a
   * ratio was measured against, and for General Trends the mention `volume`,
   * `reach`, `sentiment`, `keywords` and `hashtags`.
   */
  metadata: Record<string, unknown> | null;

  // joined from trend_competitors — all null on a news story
  competitor_region: TrendRegion | null;
  competitor_name: string | null;
  competitor_avatar: string | null;
  competitor_followers: number | null;
}

/** The 0..1 blend behind `opportunity_score` — moving these re-ranks instantly. */
export interface TrendWeights {
  outperformance: number;
  fit: number;
  uae: number;
}

export interface TrendPostsList {
  items: TrendPost[];
  /** matching rows AFTER the min-fit bar */
  total: number;
  /**
   * How many rows the min-fit bar removed. The news feed hides technical
   * stories by default, so it states this rather than quietly shrinking.
   */
  hidden_by_min_fit: number;
  weights: TrendWeights;
}

/** A scan job (GET /api/trends/scan/latest). Poll every ~3s while `active`. */
export interface TrendScan {
  id: number;
  status: "queued" | "fetching" | "scoring" | "done" | "failed";
  /** short machine-ish phase label */
  phase: string | null;
  /** a human sentence — show this verbatim */
  progress: string | null;
  competitors_total: number | null;
  competitors_done: number | null;
  posts_found: number | null;
  posts_scored: number | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  /** server-computed: status is neither done nor failed */
  active: boolean;
}

/** A theme bucket rolled up across scored posts (GET /api/trends/themes). */
export interface TrendTheme {
  theme: string;
  post_count: number;
  avg_fit: number | null;
  avg_uae: number | null;
  avg_outperformance: number | null;
  best_score: number | null;
}

/** An AI-suggested handle that was verified as readable. */
export interface TrendResearchCandidate {
  username: string;
  ok: boolean;
  display_name: string | null;
  biography?: string | null;
  profile_picture_url?: string | null;
  followers_count?: number | null;
  media_count?: number | null;
  category?: string | null;
  region?: TrendRegion;
  why: string | null;
}

/** A suggestion that could NOT be used — `error` is already plain language. */
export interface TrendResearchRejected {
  username: string;
  ok?: boolean;
  display_name?: string | null;
  why?: string | null;
  error: string;
}

/** POST /api/trends/competitors/research. `note` explains an empty result. */
export interface TrendResearchResult {
  candidates: TrendResearchCandidate[];
  rejected: TrendResearchRejected[];
  /** how many handles the model proposed before verification */
  tried: number;
  note?: string;
}

// --- Research focus dials (POST /api/trends/competitors/research) -----------
// They steer WHICH accounts the model proposes. They are NOT the feed's
// opportunity weights (TrendWeights) — those re-rank posts that already exist.
// Omitting a dial reproduces the previous, UAE-weighted behaviour exactly.
export type TrendResearchGeo = "uae" | "global";
export type TrendResearchRelevance = "normal" | "high";
export type TrendResearchEngagement = "any" | "high";
export type TrendResearchSize = "any" | "small" | "large";

export interface TrendResearchFocus {
  geo?: TrendResearchGeo;
  relevance?: TrendResearchRelevance;
  engagement?: TrendResearchEngagement;
  size?: TrendResearchSize;
}

// --- "Make this" prefill (POST /api/trends/posts/{id}/ideas) ----------------
/** One Producer tool's brief, written by the model from a trend post. EVERY
 *  field is always present — "" / [] when the tool doesn't use it — so there is
 *  no optionality to unwrap on the way into a creator. */
export interface TrendPrefill {
  tool: string;
  post_id: number;
  title: string;
  topic: string;
  /** talking_head_v2 only — one spoken line per scene, in order. */
  dialogue: string[];
  /** broll only. */
  intro_topic: string;
  outro_topic: string;
  /** human_character only — a CASTING brief, not a post topic. */
  character: string;
  notes: string;
  permalink: string | null;
}

// --- "Make this" step 1: three ideas first --------------------------------
// (POST /api/trends/general/items/{id}/brainstorm)
//
// "Make this" used to jump straight from a tool pick to a finished brief, so
// the only way to reject the model's take was to throw the whole brief away.
// Now the model proposes THREE concrete posts first; the owner picks one, or
// types what they actually want and gets three more. The chosen idea rides
// along to /ideas so the brief executes THAT idea instead of inventing a
// fourth one.
export interface TrendIdea {
  /** Stable within one round only: "idea-1" / "idea-2" / "idea-3". */
  id: string;
  /** <= 70 chars, plain language — no insider jargon. */
  title: string;
  /** 1-2 sentences: what is actually on screen, and who is in it. */
  what_you_see: string;
  /** 1 sentence, aimed at someone who has never heard of 42. */
  why_it_works: string;
  /** Which Producer tool this idea wants — pre-selects the picker in step 2. */
  tool: RoadmapTool;
  format: "reel" | "carousel" | "static";
}

// --- What the trend ACTUALLY is (the lookup that runs before the ideas) -----
//
// "Make this" on `#spidermanbrandnewday` returned three ideas about "a fresh
// start", because the model read the hashtag literally — it is the title of a
// Spider-Man film. So the backend now runs one Google-grounded lookup FIRST and
// hands the answer back here, both to build the ideas from and to show, so the
// owner can see it looked instead of guessed.
export interface GeneralTrendResearchSource {
  /** Usually the domain ("wikipedia.org") — Vertex rarely returns a real title. */
  title: string;
  /** A Vertex grounding redirect; long, and safe to open. May be "". */
  uri: string;
}

export interface GeneralTrendResearch {
  /** "" means the lookup found nothing / failed — the ONLY failure signal. */
  what_it_is: string;
  why_people_post: string;
  sources: GeneralTrendResearchSource[];
  /** ISO 8601, or "" when nothing was found. */
  researched_at: string;
  /** True when this answer came from cache rather than a fresh search. */
  cached: boolean;
}

export interface TrendIdeaList {
  ideas: TrendIdea[];
  trend: { id: number; title: string };
  /** Additive: absent on any client/server that predates the lookup, and empty
   *  whenever the search failed — the ideas are still returned either way. */
  research?: GeneralTrendResearch;
}

/** The same three ideas, asked of a COMPETITOR POST or an Industry Signal
 *  article instead of a general trend (POST /api/trends/posts/{id}/brainstorm).
 *
 *  `TrendIdea` is reused verbatim and deliberately NOT forked, so one round of
 *  ideas renders identically wherever it came from. Only two things differ, and
 *  both are forced by the source: the echo is `post`, not `trend`; and there is
 *  no `research` field, because a post already carries its caption and the
 *  scorer's read of it — there is nothing left to go and look up. A general
 *  trend arrives as a bare hashtag, which is why that one needs the lookup. */
export interface PostIdeaList {
  ideas: TrendIdea[];
  post: { id: number; title: string };
}

// ----- General trends (/api/trends/general) -----
//
// The second kind of trend on the Trends page: not "what worked for a
// competitor on Instagram" but "what the whole internet is doing right now" —
// a meme, a format, a sound, a moment. Found from free public sources (Google
// Trends, Bluesky, Reddit, Know Your Meme, Wikipedia, YouTube, X), clustered
// into ONE card per trend, and scored for how well 42 Abu Dhabi could ride it.
// Status reuses TrendPostStatus (new|saved|dismissed|used) and "Make this"
// reuses the TrendPrefill envelope, so the card actions are identical.

export type GeneralTrendKind =
  | "meme" | "format" | "sound" | "challenge" | "news"
  | "event" | "product" | "culture" | "other";
export type GeneralTrendSensitivity = "ok" | "caution" | "avoid";
/** Band of `velocity`: >=0.7 hot, >=0.45 rising, else warm. */
export type GeneralTrendHeat = "hot" | "rising" | "warm";
export type GeneralTrendSort =
  | "opportunity" | "velocity" | "fit" | "recent" | "sources";

/** One sighting shown on the card — links out to where we saw it. */
export interface GeneralTrendEvidence {
  source: string;
  region: string | null;
  title: string | null;
  url: string | null;
  metric_value: number | null;
  metric_label: string | null;
  seen_at: string | null;
}

/** One clustered trend (GET /api/trends/general/items). */
export interface GeneralTrend {
  id: number;
  slug: string;
  /** display name — the AI's clean_title after scoring, the first sighting's title before */
  title: string;
  summary: string | null;
  url: string | null;
  image_url: string | null;
  /** relative path of the locally cached thumb; use api.generalTrends.thumbUrl(id) */
  local_thumb_path: string | null;
  category: string | null;
  /** distinct region codes seen ("US", "AE", "global") */
  regions: string[];
  /** distinct source ids seen */
  sources: string[];
  source_count: number;
  sighting_count: number;
  peak_metric: number | null;
  /** 0..1 heat, recomputed every refresh */
  velocity: number | null;
  first_seen_at: string | null;
  last_seen_at: string | null;

  /** the transferable ANGLE 42 could reuse */
  topic: string | null;
  /** what the trend IS, in plain words */
  topic_summary: string | null;
  theme: string | null;
  trend_kind: GeneralTrendKind | null;
  sensitivity: GeneralTrendSensitivity | null;
  /** 0-100: can 42 realistically ride this? */
  fit_score: number | null;
  fit_reason: string | null;
  uae_relevance: number | null;
  /** how 42 would ride it */
  suggested_angle: string | null;
  suggested_format: string | null;
  effort: string | null;
  tags: string[] | null;
  opportunity_score: number | null;
  scored_at: string | null;
  status: TrendPostStatus;
  created_at: string | null;
  updated_at: string | null;

  // --- ingest screens ------------------------------------------------------
  /** the ingest safety screen's verdict; 'flagged' rows are stored for audit,
   *  never sent to the AI, and hidden from the feed unless `include_filtered` */
  safety: "ok" | "flagged";
  /** why: "explicit" | "harm" | "bait" | "avoid_topic" (or "" when ok) */
  safety_reason: string | null;
  /** newest CONTENT time across the sightings — "is anything NEW happening",
   *  as opposed to `last_seen_at`, which only says a source still lists it */
  latest_content_at: string | null;
  /** distinct (source, source_key) sightings — the real evidence count.
   *  `sighting_count` is a POLL counter and rises every refresh regardless. */
  evidence_count: number;
  /** normalised canonical name; the exact-match key that merges across runs */
  canonical_key: string | null;

  // page-only extras
  evidence: GeneralTrendEvidence[];
  heat: GeneralTrendHeat;
}

/** The 0..1 blend behind `opportunity_score` — moving these re-ranks instantly. */
export interface GeneralTrendWeights {
  velocity: number;
  fit: number;
  uae: number;
}

export interface GeneralTrendsList {
  items: GeneralTrend[];
  total: number;
  weights: GeneralTrendWeights;
}

/** Query string for GET /api/trends/general/items. */
export interface GeneralTrendsQuery {
  sort?: GeneralTrendSort;
  status?: TrendPostStatus;
  region?: string;
  source?: string;
  theme?: string;
  kind?: GeneralTrendKind;
  min_fit?: number;
  q?: string;
  include_sensitive?: boolean;
  /** also show rows the INGEST safety screen flagged. Paired with
   *  `include_sensitive` behind one "Hidden trends" toggle: the distinction
   *  between the two gates is ours, not something the reader should learn. */
  include_filtered?: boolean;
  /** also show kinds that are NOT participatory — news, events, product
   *  launches. A trend is a format people are RECREATING, so the feed carries
   *  only meme / format / sound / challenge by default; the rest are things
   *  that happened, which nobody can make their own version of. */
  all_kinds?: boolean;
  /** feed window in days over `last_seen_at`; omitted = the server default (7) */
  days?: number;
  /** max CONTENT age in days over `latest_content_at`; omitted = default (30) */
  content_days?: number;
  limit?: number;
  offset?: number;
  /** 0..1 opportunity-blend weights. */
  w_vel?: number;
  w_fit?: number;
  w_uae?: number;
}

/** A refresh run (GET /api/trends/general/runs/latest). Poll ~3s while `active`. */
export interface GeneralTrendRun {
  id: number;
  status: "queued" | "fetching" | "clustering" | "scoring" | "done" | "failed";
  phase: string | null;
  /** a human sentence — show this verbatim */
  progress: string | null;
  sources_total: number | null;
  sources_done: number | null;
  items_found: number | null;
  trends_new: number | null;
  trends_touched: number | null;
  trends_scored: number | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  /** server-computed: status is neither done nor failed */
  active: boolean;
  metadata: {
    trigger?: string;
    sources?: Record<string, { ok: boolean; count: number | null; error: string | null }>;
    embeddings?: string;
    /** stamped by every progress write — the liveness signal stale detection uses */
    heartbeat_at?: string;
  } | null;
}

/** Header numbers (GET /api/trends/general/stats). */
export interface GeneralTrendStats {
  trends_total: number;
  trends_unscored: number;
  trends_saved: number;
  trends_new_24h: number;
  last_run_at: string | null;
  sources_ok: number;
  sources_total: number;
  refresh_every_hours: number;
  next_auto_refresh_at: string | null;
}

/** One configured source + how it did on the latest run (GET /api/trends/general/sources). */
export interface GeneralTrendSource {
  id: string;
  label: string;
  enabled: boolean;
  regions: string[];
  /** null = not fetched yet */
  ok: boolean | null;
  count: number | null;
  error: string | null;
  /** items the ingest screen threw away before the per-source cap; null when
   *  the run predates the screen (so "0 dropped" is never faked) */
  dropped: number | null;
  /** reason slug -> how many, e.g. {"bait": 9, "stale": 3} */
  dropped_by: Record<string, number> | null;
}

/** A theme bucket rolled up across scored trends (GET /api/trends/general/themes). */
export interface GeneralTrendTheme {
  theme: string;
  trend_count: number;
  avg_fit: number | null;
  avg_uae: number | null;
  best_score: number | null;
}

// ---------------------------------------------------------------------------
// Trend DETAIL (one item, everything we know)
// ---------------------------------------------------------------------------
//
// The list endpoints deliberately truncate: `GeneralTrend.evidence` carries a
// short preview so a 36-card grid stays one request. The detail endpoints below
// carry the whole thing — every sighting, and the cached web lookup when one
// was ever run — because the detail dialog is where a reader decides whether to
// believe a number, and a truncated evidence list is exactly the wrong place to
// economise.

/**
 * A sighting as the DETAIL endpoint returns it. Six fields wider than the list
 * payload's preview rows, which is why it is a separate type: everything extra
 * is optional, so a preview row from a card is still assignable here and the
 * dialog can render the card's rows until the server's land.
 */
export interface GeneralTrendEvidenceDetail extends GeneralTrendEvidence {
  id?: number;
  /** the source's own id for the item — what de-duplicates across runs */
  source_key?: string | null;
  category?: string | null;
  image_url?: string | null;
  summary?: string | null;
  /** when the CONTENT was published, as distinct from when we saw it */
  published_at?: string | null;
}

/** GET /api/trends/general/items/{id} */
export interface GeneralTrendDetail {
  trend: GeneralTrend;
  /**
   * Every sighting, not the preview rows embedded in the list payload. The
   * server caps this at 200 rows — a number no real trend has reached, but the
   * detail dialog says so when a list arrives at the cap rather than presenting
   * a truncated list as complete.
   */
  evidence: GeneralTrendEvidenceDetail[];
  /** The cached "what this actually is" lookup; null when one never ran. */
  research: GeneralTrendResearch | null;
}

/** GET /api/trends/posts/{id} */
export interface TrendPostDetail {
  post: TrendPost;
}

// ---------------------------------------------------------------------------
// Social-listening report (/api/reports/social-listening)
// ---------------------------------------------------------------------------
//
// ONE payload feeds two renderers: the on-screen preview and the PDF. Every
// section states its own `basis` (what it was computed from) and its own
// `estimated` flag (true = a model produced it, so it is a judgement, not a
// measurement). A section with nothing behind it arrives with `available:false`
// and a reason, and is RENDERED as such — never dropped, because a reader has
// to be able to see what is missing.

export type ReportMode = "simple" | "full";

/**
 * Which signal streams a report is built from.
 *
 * "all" is the whole picture — the three streams together, which is what the
 * Reports tab has always produced. The other three narrow it to ONE stream, so
 * the same section renderers draw a category report. A scope the server does
 * not know is a 422, never a silent widening to "all": a single-stream report
 * that quietly became the whole picture is the one failure mode that would be
 * read as a finding.
 */
export type ReportScope = "all" | "competitors" | "news" | "viral";

/**
 * Which table a single-item report reads from.
 *
 * "post" covers BOTH a competitor post and an Industry Signal story — they are
 * the same row shape in the same table, and the report says which it was. A
 * "trend" is a viral social trend, which lives in its own table with its own
 * scoring.
 */
export type ReportItemKind = "post" | "trend";

export type ReportSectionKind =
  | "prose"
  | "bullets"
  | "kpis"
  | "table"
  | "chart"
  | "signals";

/**
 * `body` is intentionally `unknown`: its shape depends on `kind`, and the
 * renderer narrows it with the runtime guards in `ReportsPanel` rather than
 * with a cast. The per-kind shapes are exported below so a consumer can name
 * what it narrowed to.
 */
export interface ReportSection {
  id: string;
  title: string;
  available: boolean;
  /** required whenever `available` is false */
  unavailable_reason: string | null;
  /** ONE line: exactly what this section was computed from */
  basis: string;
  /** true = model-produced. Must be shown as such wherever it renders. */
  estimated: boolean;
  kind: ReportSectionKind;
  body: unknown;
}

export interface ReportProseBody {
  paragraphs: string[];
}

export interface ReportBulletsBody {
  items: string[];
}

export type ReportDeltaDir = "up" | "down" | "flat";

export interface ReportKpi {
  label: string;
  value: string;
  /** already formatted by the server ("+12%", "-3"); null when there is no
   *  previous window to compare against — which is not the same as "0". */
  delta: string | null;
  delta_dir: ReportDeltaDir | null;
  note: string | null;
}

export interface ReportKpisBody {
  items: ReportKpi[];
}

export type ReportAlign = "left" | "right" | "center";

export interface ReportTableBody {
  columns: string[];
  /** pre-formatted strings — the server owns number formatting so the PDF and
   *  the screen can never disagree about a value */
  rows: string[][];
  align: ReportAlign[];
}

export interface ReportChartSeries {
  name: string;
  values: number[];
}

export interface ReportChartBody {
  chart: "bar" | "line";
  labels: string[];
  series: ReportChartSeries[];
  y_label: string;
}

export interface ReportSignalLink {
  label: string;
  url: string;
}

export interface ReportSignalMetric {
  label: string;
  value: string;
}

/**
 * We store no comment text, so nothing in the database measures how an audience
 * FELT. `estimated` is therefore always true here: the label is the model's
 * read of the text we do hold (caption, headline, topic summary), and it has to
 * be presented that way everywhere it appears.
 */
export interface ReportSignalSentiment {
  label: string;
  estimated: boolean;
}

export interface ReportSignalItem {
  name: string;
  link: string | null;
  extra_links: ReportSignalLink[];
  metrics: ReportSignalMetric[];
  sentiment: ReportSignalSentiment | null;
  suggestion: string | null;
}

export interface ReportSignalGroup {
  group: string;
  items: ReportSignalItem[];
}

export interface ReportSignalsBody {
  groups: ReportSignalGroup[];
}

export interface ReportWindow {
  days: number;
  /** ISO 8601 */
  from: string;
  to: string;
}

export interface SocialReportMeta {
  title: string;
  brand: string;
  mode: ReportMode;
  generated_at: string;
  window: ReportWindow;
  /** the same-length window immediately before `window` — what deltas compare to */
  previous_window: { from: string; to: string };
  /** which slice this report covers. Absent on a report built before scopes
   *  existed; "all" means all three streams. */
  scope?: ReportScope;
  scope_label?: string;
  /** set only on a single-item report, so a reader can never mistake one item
   *  for a window. */
  item?: { kind: ReportItemKind; id: number; name: string } | null;
}

/** The measured volumes the whole report stands on.
 *
 *  A count is `null` — NOT 0 — when the stream is outside this report's scope.
 *  The distinction is load-bearing: "0 competitor posts" says we looked and
 *  found none, "not in scope" says we never looked. Rendering a null as 0 would
 *  be the exact class of quiet lie this report is built to avoid. */
export interface SocialReportBasis {
  competitor_handles: number | null;
  competitor_posts: number | null;
  news_items: number | null;
  /** OPTIONAL, and the third state the two above cannot express. `null` means
   *  "in the product, outside this report's scope"; ABSENT means the viral
   *  stream is switched off at the backend (`GENERAL_TRENDS_ENABLED`), so there
   *  is no such measurement to be in or out of scope. A dash labelled "not in
   *  scope" would point the reader at a stream that is not being collected, so
   *  the stat is not rendered at all when the key is missing. */
  viral_trends?: number | null;
  /** caveats worth reading before any number below, e.g. "shares are not
   *  exposed by the Instagram Graph API" */
  notes: string[];
}

/**
 * One report that has already been BUILT and kept.
 *
 * The index entry (what the tab lists) and the document (what it renders) are
 * the same row: `payload` is absent on a listing and present on a fetch, so a
 * listing never ships tens of kilobytes of envelope per row.
 */
/**
 * The three shapes a report may cover, and nothing in between.
 *
 * WHY THERE ARE ONLY THREE. A window anyone could drag out by hand is an
 * unbounded set of documents, no two of which can be laid beside each other
 * ("posts rose 20%" over what?), and every one of them costs two model passes.
 * The names are the ones the team already uses; the WINDOW is the calendar
 * period, so a "15 days" second half is thirteen days in February.
 *
 * The backend owns this list and enforces it (services/report_periods): a
 * hand-edited URL gets the same refusal the picker would give.
 */
export type ReportGrain = "week" | "half" | "month";

/** One window that may be asked for. Every one of these has FINISHED: a period
 *  that is still collecting would produce a report about part of itself with
 *  nothing on the page to say so. */
export interface ReportPeriod {
  /** Stable and sortable: "2026-W37", "2026-09-H1", "2026-09". */
  key: string;
  grain: ReportGrain;
  /** Inclusive ISO dates. */
  from: string;
  to: string;
  /** Whole days, counting both ends. Not fixed per grain: a month is 28 to 31.
   *  This is the `days` the builder is given. */
  days: number;
  /** "Mon 7 - Sun 13 Sep 2026", "1 - 15 Sep 2026", "August 2026". */
  label: string;
  /** Rows collected inside it. Zero means nothing happened in a period we were
   *  collecting through, which is not worth two model passes to write up. */
  records: number;
  /** The report that already covers this period, if there is one. A finished
   *  period cannot gain another day, so it is built once and then only read. */
  report_id: number | null;
}

export interface SavedReport {
  id: number;
  scope: ReportScope;
  mode: ReportMode;
  days: number;
  /** ISO dates, the window the builder actually measured. */
  window_from: string | null;
  window_to: string;
  generated_at: string;
  payload?: SocialReport;
  /** What this report covers, in words: its period, or a plain description of
   *  its dates for one built before the three shapes existed. Written by the
   *  backend so the two cannot describe the same row differently. */
  label?: string;
  /** False for those older rows. They are the ones that read as something
   *  random that got downloaded by accident. */
  period?: boolean;
}

/**
 * The answer to "generate this report".
 *
 * `cached: true` means nothing was rebuilt: a report for this scope, mode,
 * range and day already existed and this is it. That is the normal answer to a
 * second click, and the tab says so rather than silently spending two model
 * passes on a near-identical page.
 */
export interface GeneratedReport extends SavedReport {
  cached: boolean;
  payload: SocialReport;
}

export interface SocialReport {
  meta: SocialReportMeta;
  basis: SocialReportBasis;
  /** ordered — render in order */
  sections: ReportSection[];
}
