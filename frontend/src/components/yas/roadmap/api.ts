/**
 * Roadmap step API calls (Preview caption · Calendar schedule · Approval publish).
 *
 * Kept local to the roadmap module and same-origin (Vite proxies /api to FastAPI),
 * mirroring the lib/api.ts http() helper. Endpoints are implemented by the
 * yas_producer router under /api/yas-producer/roadmap/*.
 */
import type { FinalizedAsset, GlobalStep, RoadmapTool } from "./types";

async function http<T>(path: string, body: unknown): Promise<T> {
  return send<T>(path, "POST", body);
}

async function send<T>(path: string, method: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = j?.detail || JSON.stringify(j);
    } catch {
      /* keep statusText */
    }
    throw new Error(`${res.status} ${detail}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** The frontend-owned blob saved per project so a roadmap can resume to its step. */
export interface RoadmapSavedState {
  asset: FinalizedAsset;
  draftCaption: string;
}

/** GET /roadmap/state response — `{}` (all fields undefined) when none is saved. */
export interface RoadmapStateResponse {
  tool?: RoadmapTool;
  ref_id?: number;
  step?: GlobalStep;
  state?: RoadmapSavedState;
}

export interface CaptionResponse {
  caption: string;
  references: { caption: string; permalink: string | null; likes: number }[];
}

export interface ScheduleResponse {
  id: number;
  scheduled_at: string;
  email_configured: boolean;
  /** True once the media is copied to the public bucket (Instagram can fetch it). */
  media_hosted: boolean;
  media_url: string | null;
  /** Set when hosting failed (e.g. no public media storage set up); else null. */
  upload_warning: string | null;
}

export interface ApproveResponse {
  status: "published" | "skipped";
  permalink: string | null;
  detail: string | null;
}

export type ScheduledStatus =
  | "pending" | "reminded" | "published" | "failed" | "cancelled";

export interface ScheduledPost {
  id: number;
  tool: RoadmapTool;
  ref_id: number;
  media_type: "image" | "video";
  media_url: string | null;
  /** What the UI renders the thumbnail from (hosted URL or same-origin proxy). */
  display_url: string;
  caption: string;
  scheduled_at: string;
  timezone: string;
  status: ScheduledStatus;
  reminder_count: number;
  published_at: string | null;
  permalink: string | null;
  error: string | null;
  created_at: string;
  /** Set for carousel posts — the ordered hosted slides. */
  is_carousel?: boolean;
  carousel_items?: {
    tool: RoadmapTool;
    ref_id: number;
    media_type: "image" | "video";
    media_url: string;
  }[];
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = j?.detail || JSON.stringify(j);
    } catch {
      /* keep statusText */
    }
    throw new Error(`${res.status} ${detail}`);
  }
  return (await res.json()) as T;
}

export const roadmapApi = {
  /** Generate a brand-consistent IG caption via RAG over @42abudhabi history. */
  caption: (body: { tool: RoadmapTool; ref_id: number; topic: string }) =>
    http<CaptionResponse>("/api/yas-producer/roadmap/caption", body),

  /** Save the asset+caption+timestamp to the pending queue; arm the reminder.
   *  Pass carousel_items (≥2, slide 1 == tool/ref_id) to book a carousel post. */
  schedule: (body: {
    tool: RoadmapTool;
    ref_id: number;
    media_type: "image" | "video";
    caption: string;
    scheduled_at: string; // ISO 8601 with offset
    timezone: string;
    /** True for "Post now" — suppresses the reminder email (publishes at once). */
    immediate?: boolean;
    carousel_items?: {
      tool: RoadmapTool;
      ref_id: number;
      media_type: "image" | "video";
    }[];
    /** Reel cover frame (ms into the video); single video posts only. */
    thumb_offset?: number | null;
  }) => http<ScheduleResponse>("/api/yas-producer/roadmap/schedule", body),

  /** Approve + push live to Instagram (self-gates if no publish token). */
  approve: (scheduleId: number) =>
    http<ApproveResponse>(`/api/yas-producer/roadmap/${scheduleId}/approve`, {}),

  /** All scheduled posts for the Library's Scheduled queue. */
  listScheduled: () =>
    get<{ items: ScheduledPost[] }>("/api/yas-producer/roadmap/scheduled"),

  /** Cancel a scheduled post (frees its hosted media). */
  cancel: (scheduleId: number) =>
    http<{ ok: boolean }>(`/api/yas-producer/roadmap/${scheduleId}/cancel`, {}),

  // ---- Per-project roadmap progress (save/resume/clear) -------------------
  /** Persist a project's current step + state so it can resume from the Library. */
  saveState: (body: {
    tool: RoadmapTool;
    ref_id: number;
    step: GlobalStep;
    state: RoadmapSavedState;
  }) => send<{ ok: boolean; step: GlobalStep }>("/api/yas-producer/roadmap/state", "PUT", body),

  /** The saved roadmap position for a project (`{}` if none). */
  getState: (tool: RoadmapTool, refId: number) =>
    get<RoadmapStateResponse>(`/api/yas-producer/roadmap/state/${tool}/${refId}`),

  /** Drop a project's saved roadmap row (on publish, or a discard). */
  clearState: (tool: RoadmapTool, refId: number) =>
    send<{ ok: boolean }>(`/api/yas-producer/roadmap/state/${tool}/${refId}`, "DELETE"),
};
