/**
 * Global Producer Roadmap — shared types.
 *
 * The Producer tab wraps the asset-making creators in a 5-step master workflow:
 *   1 Producer → 2 Editor → 3 Preview → 4 Calendar → 5 Approval
 *
 * "Create Video Editor" bypasses this roadmap entirely (it is its own full-screen
 * surface). The four asset makers below flow through all five steps.
 */

// The five global steps, in order.
export type GlobalStep =
  | "producer"
  | "editor"
  | "preview"
  | "calendar"
  | "approval";

export const GLOBAL_STEP_ORDER: GlobalStep[] = [
  "producer",
  "editor",
  "preview",
  "calendar",
  "approval",
];

// Display names for the 5 roadmap steps. The internal keys stay
// (producer/editor/preview/calendar/approval) — only the labels change.
export const GLOBAL_STEP_LABELS: Record<GlobalStep, string> = {
  producer: "Ideation",
  editor: "Editing",
  preview: "Finalization",
  calendar: "Scheduling",
  approval: "Approval",
};

// The creators that flow through the roadmap. The Video Editor is now a
// first-class roadmap tool (it mounts in the Editing stage and advances to
// Finalization on export, like the others).
export type RoadmapTool =
  | "poster"
  | "animation"
  | "poster_human"
  | "talking_head_v2"
  | "video_editor"
  | "human_character"
  | "general_video"
  | "broll";

export const ROADMAP_TOOL_LABELS: Record<RoadmapTool, string> = {
  poster: "Poster",
  animation: "Animation",
  poster_human: "Poster + Human",
  talking_head_v2: "Human Element Video",
  video_editor: "Video Editor",
  human_character: "Human Character",
  general_video: "General AI Video",
  broll: "B-roll",
};

/**
 * The finished media a creator hands to the Editor→Preview boundary. Carried
 * through Preview (caption), Calendar (schedule), and Approval (publish).
 */
export interface FinalizedAsset {
  tool: RoadmapTool;
  /** generation id (poster/animation/poster_human) or run id (Human Element Video). */
  refId: number;
  mediaType: "image" | "video";
  /** Same-origin URL to fetch the final media (image src / video src). */
  mediaUrl: string;
  /** Topic / brief — used both for display and as the RAG caption query. */
  topic: string;

  // ---- filled in as the user advances ----
  /** Brand-consistent caption (generated in Preview, user-editable). */
  caption?: string;
  /** ISO timestamp the user picked in Calendar. */
  scheduledAt?: string;
  /** Backend scheduled_posts row id (set once scheduled). */
  scheduleId?: number;
  /** When ≥2, this finalizes as an Instagram CAROUSEL. The full ordered slide
   *  list (slide 1 == this asset). Built in Finalization, published as one post. */
  carouselItems?: CarouselSlide[];
  /** Reel cover frame (milliseconds into the video) chosen in Finalization.
   *  Single video posts only — set when the user scrubs the cover slider. */
  thumbOffsetMs?: number;
}

/** One slide of an Instagram carousel (a finished post added in Finalization). */
export interface CarouselSlide {
  tool: RoadmapTool;
  refId: number;
  mediaType: "image" | "video";
  /** Same-origin URL for the slide thumbnail / publish source. */
  mediaUrl: string;
  topic?: string;
}

/**
 * Callback a creator invokes when the user finalizes one asset and clicks
 * "Continue to Preview →". The roadmap injects the `tool`. Optional on every
 * creator — when present, the creator renders the Continue affordance in its
 * final/ready state; when absent (standalone use), the creator is unchanged.
 */
export type ContinueToPreview = (asset: {
  refId: number;
  mediaType: "image" | "video";
  mediaUrl: string;
  topic: string;
}) => void;
