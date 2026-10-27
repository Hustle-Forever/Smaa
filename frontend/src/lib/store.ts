/**
 * Zustand stores with in-memory caching.
 *
 * Caches data so switching between tabs doesn't re-fetch from Supabase.
 * Cache lives for 24 hours. Survives tab switching but NOT page refresh.
 */
import { create } from "zustand";

import { sampleKeyframes } from "video-edit-schema";

import { api } from "./api";
import { applyTrimEdge } from "./videoEditOps";
import type {
  Campaign,
  CampaignItem,
  InstagramPostRow,
  InstagramPostsList,
  ProducerAssetRow,
  ProducerAssetsList,
  YasLibraryItem,
  YasLibraryType,
  VideoEditorProject,
  VideoEditorRevision,
  VideoEditProjectMeta,
  VideoEditTrack,
  VideoEditClip,
  VideoEditRender,
  VideoSource,
  VideoSourceList,
  TransitionPreset,
  TransitionTiming,
  MediaBinEntry,
  VideoEditClipFilters,
  VideoEditClipText,
  VideoEditClipShape,
  VideoEditShapeVariant,
  VideoEditAiFeature,
  VideoEditAiJob,
  VideoEditAiProviders,
} from "./types";
import { DEFAULT_TEXT_FONT } from "./fonts";
import type { ChatAttachmentApi } from "../components/yas/video-editor/sidebar/attachmentDnd";

const CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 24 hours

// In-flight request dedup. When several components mount at once (e.g. Assets +
// TalkingHeadCreator + AssetPickerModal) and request the same data, only ONE
// network call fires; the rest await the same promise. Keyed by store + params.
const _inflight = new Map<string, Promise<void>>();

// ---------------------------------------------------------------------------
// Instagram posts store
// ---------------------------------------------------------------------------

interface InstagramPostsState {
  items: InstagramPostRow[];
  totals: InstagramPostsList["totals"] | null;
  loading: boolean;
  error: string | null;
  lastFetchedAt: number | null;
  lastParams: string | null;

  fetch: (params?: {
    limit?: number;
    offset?: number;
    order?: "newest" | "engagement";
    category?: string;
  }) => Promise<void>;
  invalidate: () => void;
}

export const useInstagramPostsStore = create<InstagramPostsState>((set, get) => ({
  items: [],
  totals: null,
  loading: false,
  error: null,
  lastFetchedAt: null,
  lastParams: null,

  fetch: async (params = {}) => {
    const paramsKey = JSON.stringify(params);
    const state = get();

    if (
      state.lastParams === paramsKey &&
      state.lastFetchedAt &&
      Date.now() - state.lastFetchedAt < CACHE_TTL_MS &&
      !state.loading
    ) {
      return;
    }

    set({ loading: true, error: null, lastParams: paramsKey });
    try {
      const r = await api.instagramIngest.listPosts(params);
      set({
        items: r.items,
        totals: r.totals,
        loading: false,
        lastFetchedAt: Date.now(),
      });
    } catch (e: any) {
      set({ error: String(e?.message || e), loading: false });
    }
  },

  invalidate: () => set({ lastFetchedAt: null, lastParams: null }),
}));

// ---------------------------------------------------------------------------
// Producer assets store
// ---------------------------------------------------------------------------

interface ProducerAssetsState {
  items: ProducerAssetRow[];
  totals: ProducerAssetsList["totals"] | null;
  loading: boolean;
  error: string | null;
  lastFetchedAt: number | null;
  lastParams: string | null;

  fileUrlCache: Record<number, string>;

  fetch: (
    params?: {
      content_category?: string;
      limit?: number;
      offset?: number;
    },
    /** `force: true` bypasses the freshness check — use after a mutation
     * (upload/delete) instead of relying on a separate invalidate() call
     * landing first. */
    opts?: { force?: boolean },
  ) => Promise<void>;
  addItem: (item: ProducerAssetRow) => void;
  invalidate: () => void;
  cacheFileUrl: (assetId: number, url: string) => void;
  getCachedFileUrl: (assetId: number) => string | null;
}

export const useProducerAssetsStore = create<ProducerAssetsState>((set, get) => ({
  items: [],
  totals: null,
  loading: false,
  error: null,
  lastFetchedAt: null,
  lastParams: null,
  fileUrlCache: {},

  fetch: async (params = {}, opts = {}) => {
    const paramsKey = JSON.stringify(params);
    const state = get();

    if (
      !opts.force &&
      state.lastParams === paramsKey &&
      state.lastFetchedAt &&
      Date.now() - state.lastFetchedAt < CACHE_TTL_MS &&
      !state.loading
    ) {
      return;
    }

    const inflightKey = `assets:${paramsKey}`;
    const existing = _inflight.get(inflightKey);
    if (existing) return existing;

    const p = (async () => {
      set({ loading: true, error: null, lastParams: paramsKey });
      try {
        const r = await api.producerAssets.list(params);
        set({
          items: r.items,
          totals: r.totals,
          loading: false,
          lastFetchedAt: Date.now(),
        });
      } catch (e: any) {
        set({ error: String(e?.message || e), loading: false });
      } finally {
        _inflight.delete(inflightKey);
      }
    })();
    _inflight.set(inflightKey, p);
    return p;
  },

  addItem: (item) =>
    set((s) => {
      const cat = (item.metadata as any)?.content_category;
      const catKey = cat ? `cat_${cat}` : null;
      const newTotals = s.totals ? { ...s.totals, total: s.totals.total + 1 } : null;
      if (newTotals && catKey && catKey in newTotals) {
        (newTotals as any)[catKey] = ((newTotals as any)[catKey] ?? 0) + 1;
      }
      return { items: [item, ...s.items], totals: newTotals };
    }),

  invalidate: () => set({ lastFetchedAt: null, lastParams: null }),

  cacheFileUrl: (assetId, url) =>
    set((s) => ({ fileUrlCache: { ...s.fileUrlCache, [assetId]: url } })),

  getCachedFileUrl: (assetId) => get().fileUrlCache[assetId] ?? null,
}));

// ---------------------------------------------------------------------------
// Yas Producer library store — history of past generations (talking-head runs
// + poster+human images/videos). Same 24h-TTL + in-flight-dedup pattern as the
// assets store. Type/video filtering happens client-side from `items`.
// ---------------------------------------------------------------------------

interface YasLibraryState {
  items: YasLibraryItem[];
  loading: boolean;
  error: string | null;
  lastFetchedAt: number | null;
  lastParams: string | null;

  fetch: (params?: { limit?: number; offset?: number }) => Promise<void>;
  invalidate: () => void;
  /** Drop one item from the cached grid (after it's deleted server-side) so the
   *  Library updates instantly without a refetch. Keyed by (type, id) because
   *  ids are only unique within a type's own id-space. */
  remove: (type: YasLibraryType, id: number) => void;
  /** The most recent (type, id) removed via remove() — a FRESH object each call
   *  so a co-mounted creator can react (e.g. PosterCreator prunes the deleted
   *  poster from its own local history strip / canvas). Null until first remove. */
  lastRemoved: { type: YasLibraryType; id: number } | null;
}

/**
 * Library results cached PER PARAM SET.
 *
 * The store used to keep a single `lastParams` + `lastFetchedAt` pair, so two
 * pages asking for different sizes (the Library wants 300, the Dashboard 12)
 * would each see the other's entry as a miss and overwrite it — every navigation
 * between them refetched, and routing the Dashboard through the store naively
 * would have made that *worse*, not better. Keying by params lets both live at
 * once, so Dashboard <-> Library becomes instant in both directions.
 */
const _libByParams = new Map<string, { items: YasLibraryItem[]; at: number }>();

export const useYasLibraryStore = create<YasLibraryState>((set, get) => ({
  items: [],
  loading: false,
  error: null,
  lastFetchedAt: null,
  lastParams: null,

  fetch: async (params = {}) => {
    const paramsKey = JSON.stringify(params);
    const cached = _libByParams.get(paramsKey);

    if (cached && Date.now() - cached.at < CACHE_TTL_MS) {
      // Adopt the cached list for this param set. Cheap and synchronous — no
      // network, no spinner.
      if (get().lastParams !== paramsKey) {
        set({
          items: cached.items,
          lastParams: paramsKey,
          lastFetchedAt: cached.at,
          loading: false,
          error: null,
        });
      }
      return;
    }

    const inflightKey = `yaslib:${paramsKey}`;
    const existing = _inflight.get(inflightKey);
    if (existing) return existing;

    const p = (async () => {
      set({ loading: true, error: null, lastParams: paramsKey });
      try {
        const r = await api.yasProducer.library.list(params);
        const at = Date.now();
        _libByParams.set(paramsKey, { items: r.items, at });
        set({ items: r.items, loading: false, lastFetchedAt: at });
      } catch (e: any) {
        set({ error: String(e?.message || e), loading: false });
      } finally {
        _inflight.delete(inflightKey);
      }
    })();
    _inflight.set(inflightKey, p);
    return p;
  },

  invalidate: () => {
    _libByParams.clear();
    set({ lastFetchedAt: null, lastParams: null });
  },

  lastRemoved: null,

  remove: (type, id) => {
    // Prune EVERY cached param set, not just the visible list: with per-params
    // caching, dropping the item only from `items` would let it reappear as soon
    // as the other page adopted its own (stale) cache entry.
    for (const [k, v] of _libByParams) {
      _libByParams.set(k, {
        ...v,
        items: v.items.filter((it) => !(it.type === type && it.id === id)),
      });
    }
    set((s) => ({
      items: s.items.filter((it) => !(it.type === type && it.id === id)),
      lastRemoved: { type, id },
    }));
  },
}));

// ---------------------------------------------------------------------------
// Video Editor store — the LIVE editable document (NOT TTL-cached).
//
// Holds the timeline (project/tracks/clips), selection, playhead, and view
// state. Mutators replace the relevant arrays immutably + mark dirty, which
// triggers a 700ms-debounced autosave to the backend. Autosave IS the
// leave-&-resume / edit-history mechanism. Drag interactions keep their
// in-progress delta in LOCAL component state and commit once on pointerup.
// ---------------------------------------------------------------------------

export type VideoEditorSaveState = "idle" | "dirty" | "saving" | "saved" | "error";

export type KeyframeProp = "opacity" | "scale" | "x" | "y" | "rotation" | "volume";

type KF = { f: number; v: number };
function _kfUpsert(arr: KF[] | undefined, frame: number, value: number): KF[] {
  const out = (arr ? arr.filter((k) => k.f !== frame) : []);
  out.push({ f: frame, v: value });
  out.sort((a, b) => a.f - b.f);
  return out;
}

export type VideoEditorSelection =
  | { kind: "clip"; clipId: string }
  | { kind: "transition"; clipId: string; edge: "in" | "out" }
  | null;

/** Per-feature params bag for runAi (one bag — only the relevant fields are read). */
export interface VideoEditAiRunParams {
  force?: boolean; // analyze
  text?: string; // popups
  hint?: string; // popups
  keys?: string[]; // compose
  style?: string; // compose
  generateBookends?: boolean; // compose — also generate an AI intro + outro
  instruction?: string; // edit, agent
  clip_id?: string; // voice, agent (the clip the user is pointing at)
  voice_id?: string; // voice
  useCloned?: boolean; // voice — re-voice with the saved "my voice" clone
  useAsIs?: boolean; // voice — drop the provided audio in directly (no cloning)
  sourceUploadId?: number | null; // voice — audio-asset upload id for useAsIs
  mode?: "auto" | "suno" | "library"; // music
  query?: string; // music
  role?: "intro" | "outro" | "both"; // bookend
  reuse?: boolean; // bookend — reuse an already-rendered card when ready
  introOutro?: boolean; // broll
  withMusic?: boolean; // broll
  musicMode?: "auto" | "suno" | "library"; // broll
  title?: string; // broll — legacy single override (fallback)
  bookendMode?: "generate" | "upload"; // broll — generate Veo from topics | use own clips
  introTopic?: string; // broll — generated intro topic
  outroTopic?: string; // broll — generated outro topic
  introUploadId?: number | null; // broll — upload mode: clip pinned FIRST
  outroUploadId?: number | null; // broll — upload mode: clip pinned LAST
}

/** Ephemeral cross-track drag state (view-only — never autosaved). */
export interface DragGhost {
  clipId: string;
  clientX: number;
  clientY: number;
  widthPx: number;
  label: string;
  targetTrackId: string | null;
}

const VIDEO_KF_PROPS: KeyframeProp[] = ["opacity", "scale", "x", "y", "rotation"];
const KF_DEFAULTS: Record<KeyframeProp, number> = {
  opacity: 1,
  scale: 1,
  x: 0,
  y: 0,
  rotation: 0,
  volume: 1,
};

interface VideoEditorState {
  projectId: number | null;
  title: string;
  project: VideoEditProjectMeta | null;
  tracks: VideoEditTrack[];
  clips: VideoEditClip[];
  render: VideoEditRender | null;
  rev: number; // history.rev — sent as base_rev for optimistic concurrency

  // View / interaction state (ephemeral; never autosaved)
  selection: VideoEditorSelection;
  /** Clips the AI chat scopes its commands to (multi-select). Parallel to
   * `selection` (which stays single, for the inspector/gizmo). */
  multiSelection: string[];
  /** When ON, a plain click on a clip (timeline/canvas) TOGGLES it in
   * `multiSelection` instead of single-selecting — the chat's "Select items" mode. */
  selectMode: boolean;
  playheadFrame: number;
  pxPerFrame: number;
  scrollFrame: number;

  loaded: boolean;
  saveState: VideoEditorSaveState;
  lastSavedAt: number | null;
  error: string | null;
  /** True when there is at least one prior document to undo to / redo to. */
  canUndo: boolean;
  canRedo: boolean;

  load: (p: VideoEditorProject) => void;
  reset: () => void;
  /** Apply an immutable mutation to {project,tracks,clips} then autosave. */
  mutate: (fn: (s: VideoEditorState) => Partial<VideoEditorState>) => void;
  /** Step the document back/forward through the edit history. */
  undo: () => void;
  redo: () => void;
  /** Bracket a continuous gesture so per-frame writes collapse to one undo step. */
  beginHistoryGesture: () => void;
  endHistoryGesture: () => void;
  /** Apply a server-produced doc (AI edit) as one undoable step (vs load() which resets history). */
  applyServerDoc: (doc: Partial<VideoEditorProject>) => void;
  /** Append a clip built from a picked/uploaded source onto a video track. */
  addClipFromSource: (source: VideoSource, opts?: { trackId?: string }) => void;
  /** Place a picked source FULL-FRAME at an explicit track + start frame (used
   * by timeline drag-drop). Videos and stills both land full-frame — unlike the
   * PiP `addImageClip`. Normal undoable mutation. */
  addSourceAtPosition: (source: VideoSource, trackId: string, fromFrame: number) => void;
  /** Place a picked source as an intro (frame 0, shifts everything right) or
   * an outro (appended at the timeline end). Normal undoable mutation. */
  addBookendFromSource: (source: VideoSource, role: "intro" | "outro") => void;
  removeClip: (clipId: string) => void;
  /** Trim a clip's in/out edge by a frame delta (left moves `from`, right moves `trimAfter`). */
  trimClipEdge: (clipId: string, edge: "left" | "right", deltaFrames: number) => void;
  /** Reposition (and optionally re-track) a clip on the timeline. */
  moveClip: (clipId: string, fromFrame: number, trackId?: string) => void;
  /** Split a clip at an absolute timeline frame into two clips. */
  splitClip: (clipId: string, atFrame: number) => void;
  updateClip: (clipId: string, patch: Partial<VideoEditClip>) => void;
  // --- multi-track (Phase D) ---
  addTrack: (kind: "video" | "audio") => void;
  removeTrack: (trackId: string) => void;
  /** Reorder a video track's z-order: "up" = toward the foreground (higher index). */
  reorderTrack: (trackId: string, dir: "up" | "down") => void;
  setTrackMuted: (trackId: string, muted: boolean) => void;
  setTrackLocked: (trackId: string, locked: boolean) => void;
  /** Audio/video separation: copy a video clip's audio onto an audio track + mute the video. */
  detachAudio: (clipId: string) => void;
  /** Set a single static keyframe for a property (opacity/volume/scale/…). */
  setClipStaticProp: (
    clipId: string,
    prop: "opacity" | "scale" | "x" | "y" | "rotation" | "volume",
    value: number,
  ) => void;
  // --- keyframes (Phase F) ---
  /** Upsert a keyframe at a clip-local frame (insert, or update if one exists there). */
  setKeyframe: (clipId: string, prop: KeyframeProp, frame: number, value: number) => void;
  removeKeyframe: (clipId: string, prop: KeyframeProp, frame: number) => void;
  /** Retime a keyframe (drag a diamond in the lane). */
  moveKeyframe: (clipId: string, prop: KeyframeProp, fromFrame: number, toFrame: number) => void;
  /** CapCut-style master keyframe at the playhead: toggles a keyframe across all
   * transform props (video) or volume (audio) for the selected clip. */
  toggleMasterKeyframe: (clipId: string, timelineFrame: number) => void;
  /** On-canvas gizmo write: set x/y/scale/rotation. Keyframes at the clip-local
   * frame when the prop is already animated, else writes a static value (f=0). */
  applyCanvasTransform: (
    clipId: string,
    patch: Partial<Record<"x" | "y" | "scale" | "rotation", number>>,
    localFrame: number,
  ) => void;
  /** One-click "✦ Animate" for text/sticker (image) clips: applies a random
   * tasteful entrance+exit keyframe preset (pop/fade/slide/title). One undo
   * step. No-op for any other clip kind. */
  animateClip: (clipId: string) => void;
  // --- media bin + filters (Iteration 2) ---
  addToBin: (entry: MediaBinEntry) => void;
  removeFromBin: (entry: { kind: string; id: number }) => void;
  setClipFilters: (clipId: string, patch: Partial<VideoEditClipFilters>) => void;
  // --- layer types (Iteration 2) ---
  /** Add a centered text overlay clip at the playhead on a dedicated overlay track. */
  addTextClip: (opts?: { content?: string; preset?: Partial<VideoEditClipText> }) => void;
  /** Patch a text clip's text properties (inspector). */
  setClipText: (clipId: string, patch: Partial<VideoEditClipText>) => void;
  /** Add a shape overlay clip (rect/ellipse/line/triangle/star) at the playhead. */
  addShapeClip: (variant: VideoEditShapeVariant) => void;
  /** Patch a shape clip's properties (inspector). */
  setClipShape: (clipId: string, patch: Partial<VideoEditClipShape>) => void;
  /** Add an image/sticker overlay clip from an Assets-Library image at the playhead. */
  addImageClip: (asset: { id: number; fit?: "cover" | "contain"; source?: "asset" | "still" }) => void;
  /** Add a music/audio clip onto an audio track (auto-provisioned). */
  addAudioClip: (audio: { id: number; durationInFrames: number; title?: string }) => void;
  // --- cross-track drag ghost (Iteration 2; ephemeral) ---
  dragGhost: DragGhost | null;
  beginDragGhost: (clipId: string, clientX: number, clientY: number, widthPx: number, label: string) => void;
  updateDragGhost: (clientX: number, clientY: number, targetTrackId: string | null) => void;
  endDragGhost: () => void;
  /** Set/replace/remove the transition INTO a clip from its previous track
   * neighbour. Ripples this clip + later clips so the overlap equals the
   * transition duration (keeps timeline == Player == export). */
  setTransition: (
    clipId: string,
    preset: TransitionPreset,
    durationFrames: number,
    timing?: TransitionTiming,
  ) => void;
  setTitle: (title: string) => void;
  select: (sel: VideoEditorSelection) => void;
  /** Multi-select (AI chat scope) — independent of single `selection`. */
  setMultiSelection: (clipIds: string[]) => void;
  clearMultiSelection: () => void;
  toggleClipInSelection: (clipId: string) => void;
  selectClipsByKind: (kind: "all" | "video" | "audio" | "text" | "image" | "shape") => void;
  /** Toggle the explicit "Select items" mode (plain click marks clips). */
  setSelectMode: (on: boolean) => void;
  /** Mark every clip on the timeline (whole-timeline scope). */
  selectAll: () => void;
  setPlayhead: (frame: number) => void;
  setZoom: (pxPerFrame: number) => void;
  setScroll: (frame: number) => void;
  /** Update render/export state from a server poll WITHOUT triggering autosave. */
  setRender: (render: VideoEditRender) => void;
  flush: () => Promise<void>;
  /** Best-effort keepalive save for `beforeunload` (can't await on unload). */
  flushBeacon: () => void;

  // --- AI assist (Phase 3) ---
  /** The last-started (or adopted) AI job — kept after `done` so the panel can
   * show the "Applied N change(s)" summary. */
  aiJob: VideoEditAiJob | null;
  /** True from POST until the job reaches done/failed. Autosave is paused. */
  aiBusy: boolean;
  aiError: string | null;
  /** Server-side media-analysis cache exists (GET /ai/status.analysis_ready). */
  analysisReady: boolean;
  /** Flush local edits, pause autosave, POST the feature, then poll /ai/status
   * until done (refetch + applyServerDoc as ONE undo step) or failed. */
  runAi: (feature: VideoEditAiFeature, params?: VideoEditAiRunParams) => Promise<void>;
  /** One-shot status check (panel mount): learns analysis_ready and adopts a
   * job already running on the server (e.g. after a page reload mid-job). */
  syncAiStatus: () => Promise<void>;
  /** Which reasoning brain the agent runs on (GET /ai/providers) — null until
   * syncAgentProviders() has answered. Informational only (the panel badge).
   * NOT per-project, so it survives load()/reset(). */
  agentProviders: VideoEditAiProviders | null;
  /** Probe the reasoning brain ONCE per session and stash it. Errors are
   * swallowed — without it the badge simply stays hidden. */
  syncAgentProviders: () => Promise<void>;
  dismissAiError: () => void;
  /** The AI-assistant chat thread (survives sidebar-tab switches). */
  aiChat: ChatMsg[];
  /** Send a plain-language message: the backend smart-routes it to the right
   * AI feature and runs it; the result lands as ONE undo step. `attachments` are
   * sidebar items the user dragged onto the chat (concrete ids the AI applies). */
  runChat: (
    message: string,
    selectedClipIds?: string[] | null,
    attachments?: ChatAttachmentApi[] | null,
  ) => Promise<void>;
  clearChat: () => void;
}

let _veSaveTimer: ReturnType<typeof setTimeout> | null = null;
let _veResaveQueued = false; // an edit landed while a save was in flight
const VE_AUTOSAVE_MS = 700;

// --- AI assist (Phase 3) ---------------------------------------------------
// While an AI job runs, the SERVER owns the document (the job writes the
// timeline directly). Autosave is last-write-wins, so a stray debounced PATCH
// during the job would clobber the AI's write — `_veAiActive` makes
// scheduleSave/flush no-ops for the duration. Resumed when the job ends.
let _veAiActive = false;
let _veAiPollTimer: ReturnType<typeof setTimeout> | null = null;
const VE_AI_POLL_MS = 2000;
// True while a CHAT-initiated job is running, so pollAi can post the result
// (done / failed) back into the chat thread.
let _veChatActive = false;
// Guards the one-shot reasoning-brain probe (two panels mounting at once must
// not fire two GETs).
let _veProvidersInFlight = false;
let _veChatSeq = 0;
const _chatId = (): string => `c${++_veChatSeq}`;

/** A message in the AI assistant chat thread. */
export interface ChatMsg {
  id: string;
  role: "user" | "assistant";
  text: string;
  kind?: "working" | "done" | "error" | "info";
}

/** Map a raw AI job status to friendly, jargon-free language for the chat. */
export function aiStatusLabel(job: VideoEditAiJob | null): string {
  switch (job?.status) {
    case "queued": return "Getting started…";
    case "analyzing": return "Looking at your video…";
    case "planning": return "Thinking it through…";
    case "applying": return "Editing…";
    case "generating_card": return "Designing the opening and ending…";
    case "waiting_provider": return "Putting the video together… (a minute or two)";
    default: return "Working…";
  }
}

function _veStopAiPoll(): void {
  if (_veAiPollTimer) {
    clearTimeout(_veAiPollTimer);
    _veAiPollTimer = null;
  }
}

/** api.ts http() throws `Error("422 Voice changer needs …")` — drop the
 * status-code prefix so users see just the human message. */
function _veHumanError(e: unknown): string {
  const msg = e instanceof Error ? e.message : String(e);
  return msg.replace(/^\d{3}\s+/, "");
}

// ---------------------------------------------------------------------------
// Undo / redo history.
//
// The persisted document is exactly {project, tracks, clips}. Because every
// mutator replaces those arrays immutably and then calls scheduleSave(), we
// snapshot them at that single choke point. The undo stack holds PRIOR
// committed documents; redo holds the ones we've stepped back from.
//
// Coalescing — so a continuous gesture (gizmo drag = per-rAF writes, a slider
// dragged = per-onChange writes) becomes ONE undo step:
//   - explicit gesture brackets (beginHistoryGesture/endHistoryGesture) wrap
//     the gizmo; the first commit in a gesture pushes a history entry, the
//     rest fold into it.
//   - a time+key window folds rapid same-target commits (sliders) even without
//     explicit brackets.
// ---------------------------------------------------------------------------
type VideoEditSnapshot = {
  project: VideoEditProjectMeta | null;
  tracks: VideoEditTrack[];
  clips: VideoEditClip[];
};
const VE_HISTORY_LIMIT = 100;
const VE_COALESCE_MS = 350;
let _veUndo: VideoEditSnapshot[] = [];
let _veRedo: VideoEditSnapshot[] = [];
let _vePresent: VideoEditSnapshot | null = null; // last committed doc (baseline)
let _veApplyingHistory = false; // true while undo/redo writes (suppress capture)
let _veGestureDepth = 0;
let _veGestureDirty = false; // has this gesture already pushed its baseline?
let _veLastCommitAt = 0;
let _veLastCoalesceKey: string | null = null;

function _veSnapshot(s: VideoEditorState): VideoEditSnapshot {
  return { project: s.project, tracks: s.tracks, clips: s.clips };
}
/** Reset history to a fresh baseline (on load) or empty (on reset). */
function _veResetHistory(baseline: VideoEditSnapshot | null): void {
  _veUndo = [];
  _veRedo = [];
  _vePresent = baseline;
  _veGestureDepth = 0;
  _veGestureDirty = false;
  _veLastCommitAt = 0;
  _veLastCoalesceKey = null;
}
/** A document's identity for history comparisons. `sourceUrl` is left out: the
 *  server re-derives it on every read, so it says nothing about the edit. */
function _veDocKey(s: VideoEditSnapshot): string {
  return JSON.stringify({
    project: s.project,
    tracks: s.tracks,
    clips: s.clips.map(({ sourceUrl: _url, ...c }) => c),
  });
}

let _clipSeq = 0;
function _newClipId(): string {
  _clipSeq += 1;
  return `clp_${Date.now().toString(36)}_${_clipSeq}`;
}

let _trackSeq = 0;
function _newTrackId(kind: "video" | "audio"): string {
  _trackSeq += 1;
  return `trk_${kind === "audio" ? "a" : "v"}_${Date.now().toString(36)}_${_trackSeq}`;
}

/** A video track dedicated to overlays (text/shape/image), above all video.
 * Reuses an existing overlay-only track or creates a new top-z one. Returns the
 * (possibly extended) tracks array + the overlay track id. */
function _overlayTrack(
  tracks: VideoEditTrack[],
  clips: VideoEditClip[],
): { tracks: VideoEditTrack[]; trackId: string } {
  const vtracks = tracks.filter((t) => t.kind === "video");
  const isOverlayOnly = (tid: string) => {
    const cs = clips.filter((c) => c.trackId === tid);
    return (
      cs.length > 0 &&
      cs.every((c) => c.kind === "text" || c.kind === "shape" || c.kind === "image")
    );
  };
  const reuse = vtracks.filter((t) => isOverlayOnly(t.id)).sort((a, b) => b.index - a.index)[0];
  if (reuse) return { tracks, trackId: reuse.id };
  const maxIdx = vtracks.reduce((m, t) => Math.max(m, t.index), -1);
  const track: VideoEditTrack = {
    id: _newTrackId("video"),
    kind: "video",
    index: maxIdx + 1,
    muted: false,
    locked: false,
    name: "Overlay",
  };
  return { tracks: [...tracks, track], trackId: track.id };
}

/** End frame of the last clip on a track (for append placement). */
function _trackEndFrame(clips: VideoEditClip[], trackId: string): number {
  let end = 0;
  for (const c of clips) {
    if (c.trackId !== trackId) continue;
    const e = (c.from || 0) + (c.durationInFrames || 0);
    if (e > end) end = e;
  }
  return end;
}

export const useVideoEditorStore = create<VideoEditorState>((set, get) => {
  // Capture the just-committed document into the undo history. Runs inside
  // scheduleSave (the universal mutation choke point), BEFORE the debounced
  // flush. Suppressed while undo/redo is applying.
  const recordHistory = (coalesceKey?: string) => {
    if (_veApplyingHistory) return;
    const cur = _veSnapshot(get());
    if (_vePresent === null) {
      _vePresent = cur; // first commit since load — establish the baseline
      return;
    }
    const now = Date.now();
    const inGesture = _veGestureDepth > 0;
    // Inside an explicit gesture the gesture flag is authoritative: the first
    // commit pushes a new entry, the rest fold into it. Outside a gesture, fold
    // rapid same-target commits (slider/typing streams) via the time+key window.
    const timeCoalesce =
      coalesceKey != null &&
      coalesceKey === _veLastCoalesceKey &&
      now - _veLastCommitAt < VE_COALESCE_MS;
    const coalesce = inGesture ? _veGestureDirty : timeCoalesce;
    if (coalesce) {
      _vePresent = cur; // fold into the current step — no new undo entry
    } else {
      _veUndo.push(_vePresent);
      if (_veUndo.length > VE_HISTORY_LIMIT) _veUndo.shift();
      _veRedo = [];
      _vePresent = cur;
      if (inGesture) _veGestureDirty = true;
      set({ canUndo: _veUndo.length > 0, canRedo: false });
    }
    _veLastCommitAt = now;
    _veLastCoalesceKey = coalesceKey ?? null;
  };

  const scheduleSave = (coalesceKey?: string) => {
    if (_veAiActive) return; // AI job owns the server doc — autosave paused
    recordHistory(coalesceKey);
    set({ saveState: "dirty" });
    if (_veSaveTimer) clearTimeout(_veSaveTimer);
    _veSaveTimer = setTimeout(() => {
      void get().flush();
    }, VE_AUTOSAVE_MS);
  };

  // --- AI assist (Phase 3): single poll loop ------------------------------
  // One module-level timer (like _veSaveTimer); each tick re-arms itself until
  // the job reaches done/failed. `waiting_provider` (Suno) keeps polling.
  const pollAi = async (): Promise<void> => {
    const pid = get().projectId;
    if (pid == null) {
      _veStopAiPoll();
      _veAiActive = false;
      return;
    }
    let st: Awaited<ReturnType<typeof api.yasProducer.videoEditor.ai.status>>;
    try {
      st = await api.yasProducer.videoEditor.ai.status(pid);
    } catch {
      // Transient poll error (proxy hiccup) — keep polling.
      _veStopAiPoll();
      _veAiPollTimer = setTimeout(() => void pollAi(), VE_AI_POLL_MS);
      return;
    }
    if (get().projectId !== pid) return; // project switched mid-poll — reset() cleaned up
    const job = st.job;
    set({ analysisReady: st.analysis_ready, ...(job ? { aiJob: job } : {}) });
    if (!job) {
      // Job blob vanished server-side — nothing left to wait for.
      _veStopAiPoll();
      _veAiActive = false;
      set({ aiBusy: false });
      return;
    }
    if (job.status === "failed") {
      _veStopAiPoll();
      _veAiActive = false; // resume autosave
      // A job can fail AFTER applying some ops (e.g. phase B rate-limited after
      // phase A saved structure) — pull those so they're visible, not hidden.
      if ((job.ops_applied ?? 0) > 0) {
        try {
          const doc = await api.yasProducer.videoEditor.fetch(pid);
          if (get().projectId === pid) get().applyServerDoc(doc);
        } catch {
          /* keep the error banner; nothing to apply */
        }
      }
      set({ aiBusy: false, aiError: job.error || "AI job failed" });
      if (_veChatActive) {
        _veChatActive = false;
        const t = job.error || "Sorry — that didn't work. Want to try again?";
        set((s) => ({ aiChat: [...s.aiChat, { id: _chatId(), role: "assistant", text: t, kind: "error" }] }));
      }
      return;
    }
    if (job.status === "done") {
      _veStopAiPoll();
      if (job.feature === "analyze") {
        // Analyze only builds the server-side cache — no doc change to pull.
        _veAiActive = false;
        set({ aiBusy: false });
        return;
      }
      try {
        const doc = await api.yasProducer.videoEditor.fetch(pid);
        _veAiActive = false; // resume autosave BEFORE applying (applyServerDoc schedules a save)
        if (get().projectId === pid) get().applyServerDoc(doc); // ONE undoable step
      } catch (e) {
        _veAiActive = false;
        set({ aiError: _veHumanError(e) });
      }
      set({ aiBusy: false }); // keep aiJob so the panel can show ops_applied
      if (_veChatActive) {
        _veChatActive = false;
        const n = job.ops_applied ?? 0;
        const t = `Done — made ${n} change${n === 1 ? "" : "s"}. Not feeling it? Tap Undo.`;
        set((s) => ({ aiChat: [...s.aiChat, { id: _chatId(), role: "assistant", text: t, kind: "done" }] }));
      }
      return;
    }
    // queued / analyzing / planning / applying / waiting_provider → keep going
    _veStopAiPoll();
    _veAiPollTimer = setTimeout(() => void pollAi(), VE_AI_POLL_MS);
  };

  // Apply a history snapshot to the live document, then persist WITHOUT
  // recording a new history entry (guarded by _veApplyingHistory).
  const applySnapshot = (snap: VideoEditSnapshot) => {
    _veApplyingHistory = true;
    set({ project: snap.project, tracks: snap.tracks, clips: snap.clips });
    scheduleSave(); // recordHistory bails (guarded); schedules the autosave
    _veApplyingHistory = false;
  };

  // Put the project's saved versions underneath the undo stack, so Undo keeps
  // working after a reload instead of starting from nothing. Arrives after
  // load(): anything edited or undone in the meantime stays on top of it.
  const seedHistory = async (pid: number, baseline: VideoEditSnapshot, currentRev: number) => {
    let items: VideoEditorRevision[];
    try {
      ({ items } = await api.yasProducer.videoEditor.history(pid));
    } catch {
      return; // no saved history is a missing convenience, not an editor error
    }
    if (get().projectId !== pid || !items?.length) return;
    const older: VideoEditSnapshot[] = [];
    let lastKey: string | null = null;
    for (const r of items) {
      if (r.rev >= currentRev) continue;
      const snap: VideoEditSnapshot = { project: r.project, tracks: r.tracks || [], clips: r.clips || [] };
      const key = _veDocKey(snap);
      if (key === lastKey) continue;
      older.push(snap);
      lastKey = key;
    }
    // The newest saved version is usually the document that just loaded.
    const baseKey = _veDocKey(baseline);
    while (older.length && _veDocKey(older[older.length - 1]) === baseKey) older.pop();
    if (!older.length) return;
    _veUndo = [...older, ..._veUndo].slice(-VE_HISTORY_LIMIT);
    set({ canUndo: _veUndo.length > 0 });
  };

  return {
    projectId: null,
    title: "",
    project: null,
    tracks: [],
    clips: [],
    render: null,
    rev: 0,

    selection: null,
    multiSelection: [],
    selectMode: false,
    playheadFrame: 0,
    pxPerFrame: 2,
    scrollFrame: 0,

    loaded: false,
    saveState: "idle",
    lastSavedAt: null,
    error: null,
    canUndo: false,
    canRedo: false,

    aiJob: null,
    aiBusy: false,
    aiError: null,
    analysisReady: false,
    agentProviders: null,
    aiChat: [],

    load: (p) => {
      _veStopAiPoll();
      _veAiActive = false;
      set({
        projectId: p.id,
        title: p.title,
        project: p.project,
        tracks: p.tracks || [],
        clips: p.clips || [],
        render: p.render,
        rev: p.history?.rev ?? 0,
        selection: null,
        multiSelection: [],
        selectMode: false,
        playheadFrame: 0,
        scrollFrame: 0,
        loaded: true,
        saveState: "idle",
        lastSavedAt: null,
        error: null,
        canUndo: false,
        canRedo: false,
        aiJob: null,
        aiBusy: false,
        aiError: null,
        analysisReady: false,
        aiChat: [],
      });
      // Fresh baseline — the loaded doc is the first undo anchor.
      const baseline = { project: p.project, tracks: p.tracks || [], clips: p.clips || [] };
      _veResetHistory(baseline);
      // Then the project's saved versions underneath it, once they arrive.
      void seedHistory(p.id, baseline, p.history?.rev ?? 0);
    },

    reset: () => {
      if (_veSaveTimer) {
        clearTimeout(_veSaveTimer);
        _veSaveTimer = null;
      }
      _veResaveQueued = false;
      _veStopAiPoll();
      _veAiActive = false;
      _veResetHistory(null);
      set({
        projectId: null,
        title: "",
        project: null,
        tracks: [],
        clips: [],
        render: null,
        rev: 0,
        selection: null,
        multiSelection: [],
        selectMode: false,
        playheadFrame: 0,
        pxPerFrame: 2,
        scrollFrame: 0,
        loaded: false,
        saveState: "idle",
        lastSavedAt: null,
        error: null,
        dragGhost: null,
        canUndo: false,
        canRedo: false,
        aiJob: null,
        aiBusy: false,
        aiError: null,
        analysisReady: false,
        aiChat: [],
      });
    },

    mutate: (fn) => {
      set((s) => fn(s));
      scheduleSave();
    },

    // --- undo / redo -------------------------------------------------------
    undo: () => {
      if (!_veUndo.length || _vePresent === null) return;
      _veRedo.push(_vePresent);
      const prev = _veUndo.pop()!;
      _vePresent = prev;
      applySnapshot(prev);
      set({ canUndo: _veUndo.length > 0, canRedo: _veRedo.length > 0 });
    },
    redo: () => {
      if (!_veRedo.length || _vePresent === null) return;
      _veUndo.push(_vePresent);
      const next = _veRedo.pop()!;
      _vePresent = next;
      applySnapshot(next);
      set({ canUndo: _veUndo.length > 0, canRedo: _veRedo.length > 0 });
    },
    /** Bracket a continuous gesture (gizmo/slider drag) so it = one undo step. */
    beginHistoryGesture: () => {
      _veGestureDepth += 1;
      if (_veGestureDepth === 1) _veGestureDirty = false;
    },
    endHistoryGesture: () => {
      _veGestureDepth = Math.max(0, _veGestureDepth - 1);
      if (_veGestureDepth === 0) _veGestureDirty = false;
    },
    /** Apply a server-produced document (e.g. an AI edit) as ONE undoable step,
     * unlike load() which resets history. Used by the AI features so the user
     * can undo an AI edit. */
    applyServerDoc: (doc) => {
      set({
        project: doc.project ?? get().project,
        tracks: doc.tracks ?? get().tracks,
        clips: doc.clips ?? get().clips,
        render: doc.render ?? get().render,
        rev: doc.history?.rev ?? get().rev,
      });
      scheduleSave(); // records a normal history entry → undo reverts the AI edit
    },

    addClipFromSource: (source, opts) => {
      set((s) => {
        const trackId =
          opts?.trackId ||
          s.tracks.find((t) => t.kind === "video")?.id ||
          "trk_v0";
        const natural = source.naturalDurationInFrames || source.durationInFrames || 1;
        const from = _trackEndFrame(s.clips, trackId);
        const clip: VideoEditClip = {
          id: _newClipId(),
          trackId,
          source: {
            kind: source.kind,
            animation_id: source.kind === "animation" ? source.id : null,
            upload_id: source.kind === "upload" ? source.id : null,
            composition_id: source.kind === "composition" ? source.id : null,
            naturalDurationInFrames: natural,
          },
          sourceUrl: source.url,
          trimBefore: 0,
          trimAfter: natural,
          from,
          durationInFrames: Math.max(1, natural),
          keyframes: {},
          muted: false,
          fit: "cover",
        };
        return { clips: [...s.clips, clip], selection: { kind: "clip", clipId: clip.id } };
      });
      scheduleSave();
    },

    addSourceAtPosition: (source, trackId, fromFrame) => {
      set((s) => {
        const isStill = source.kind === "still";
        const natural = source.naturalDurationInFrames || source.durationInFrames || 1;
        // Stills carry no real duration — hold 3s @30fps (same as bookend stills).
        const dur = isStill && natural <= 1 ? 90 : Math.max(1, natural);
        const from = Math.max(0, Math.round(fromFrame));
        const clip: VideoEditClip = {
          id: _newClipId(),
          trackId,
          kind: isStill ? "image" : undefined,
          source: {
            kind: source.kind,
            animation_id: source.kind === "animation" ? source.id : null,
            upload_id: source.kind === "upload" ? source.id : null,
            composition_id: source.kind === "composition" ? source.id : null,
            still_id: isStill ? source.id : null,
            naturalDurationInFrames: natural,
          },
          sourceUrl: source.url,
          trimBefore: 0,
          trimAfter: isStill ? dur : natural,
          from,
          durationInFrames: dur,
          // Full-frame — deliberately NO 0.6 scale keyframe (that's the PiP path).
          keyframes: {},
          muted: false,
          fit: "cover",
        };
        return { clips: [...s.clips, clip], selection: { kind: "clip", clipId: clip.id } };
      });
      scheduleSave();
    },

    addBookendFromSource: (source, role) => {
      set((s) => {
        const trackId = s.tracks.find((t) => t.kind === "video")?.id || "trk_v0";
        const natural = source.naturalDurationInFrames || source.durationInFrames || 1;
        // Bookend stills come through with no real duration — hold 3s @30fps.
        const dur = source.kind === "still" && natural <= 1 ? 90 : Math.max(1, natural);
        const clip: VideoEditClip = {
          id: _newClipId(),
          trackId,
          kind: source.kind === "still" ? "image" : undefined,
          source: {
            kind: source.kind,
            animation_id: source.kind === "animation" ? source.id : null,
            upload_id: source.kind === "upload" ? source.id : null,
            composition_id: source.kind === "composition" ? source.id : null,
            still_id: source.kind === "still" ? source.id : null,
            naturalDurationInFrames: natural,
          },
          sourceUrl: source.url,
          trimBefore: 0,
          trimAfter: source.kind === "still" ? dur : natural,
          from: 0,
          durationInFrames: dur,
          keyframes: {},
          muted: false,
          fit: "cover",
        };
        if (role === "intro") {
          // Insert at frame 0 and push EVERY existing clip right by its length.
          const shifted = s.clips.map((c) => ({ ...c, from: c.from + dur }));
          return { clips: [...shifted, clip], selection: { kind: "clip", clipId: clip.id } };
        }
        // outro — append at the current timeline end (across all tracks).
        clip.from = s.clips.reduce((m, c) => Math.max(m, c.from + c.durationInFrames), 0);
        return { clips: [...s.clips, clip], selection: { kind: "clip", clipId: clip.id } };
      });
      scheduleSave();
    },

    removeClip: (clipId) => {
      set((s) => ({
        clips: s.clips.filter((c) => c.id !== clipId),
        selection:
          s.selection && "clipId" in s.selection && s.selection.clipId === clipId
            ? null
            : s.selection,
      }));
      scheduleSave();
    },

    trimClipEdge: (clipId, edge, deltaFrames) => {
      set((s) => ({
        clips: s.clips.map((c) =>
          c.id === clipId ? { ...c, ...applyTrimEdge(c, edge, deltaFrames) } : c,
        ),
      }));
      scheduleSave(`trim:${clipId}:${edge}`);
    },

    moveClip: (clipId, fromFrame, trackId) => {
      set((s) => ({
        clips: s.clips.map((c) =>
          c.id === clipId
            ? { ...c, from: Math.max(0, Math.round(fromFrame)), trackId: trackId || c.trackId }
            : c,
        ),
      }));
      scheduleSave();
    },

    splitClip: (clipId, atFrame) => {
      set((s) => {
        const idx = s.clips.findIndex((c) => c.id === clipId);
        if (idx < 0) return {};
        const c = s.clips[idx];
        const local = Math.round(atFrame - c.from);
        if (local <= 0 || local >= c.durationInFrames) return {}; // playhead not inside
        const splitKf = (arr?: { f: number; v: number }[]) => {
          const left: { f: number; v: number }[] = [];
          const right: { f: number; v: number }[] = [];
          for (const k of arr || []) {
            if (k.f < local) left.push(k);
            else right.push({ f: k.f - local, v: k.v });
          }
          return [left, right] as const;
        };
        const leftKf: Record<string, { f: number; v: number }[]> = {};
        const rightKf: Record<string, { f: number; v: number }[]> = {};
        const kf = c.keyframes || {};
        for (const prop of ["opacity", "scale", "x", "y", "rotation", "volume"] as const) {
          const [l, r] = splitKf(kf[prop]);
          if (l.length) leftKf[prop] = l;
          if (r.length) rightKf[prop] = r;
        }
        const left: VideoEditClip = {
          ...c,
          durationInFrames: local,
          trimAfter: c.trimBefore + local,
          transitionOut: null,
          keyframes: leftKf,
        };
        const right: VideoEditClip = {
          ...c,
          id: _newClipId(),
          from: c.from + local,
          trimBefore: c.trimBefore + local,
          durationInFrames: c.durationInFrames - local,
          transitionIn: null,
          keyframes: rightKf,
        };
        const clips = [...s.clips];
        clips.splice(idx, 1, left, right);
        return { clips, selection: { kind: "clip", clipId: right.id } };
      });
      scheduleSave();
    },

    updateClip: (clipId, patch) => {
      set((s) => ({
        clips: s.clips.map((c) => (c.id === clipId ? { ...c, ...patch } : c)),
      }));
      scheduleSave();
    },

    addTrack: (kind) => {
      set((s) => {
        const same = s.tracks.filter((t) => t.kind === kind);
        const maxIdx = same.reduce((m, t) => Math.max(m, t.index), kind === "audio" ? 99 : -1);
        const track: VideoEditTrack = {
          id: _newTrackId(kind),
          kind,
          index: maxIdx + 1,
          muted: false,
          locked: false,
        };
        return { tracks: [...s.tracks, track] };
      });
      scheduleSave();
    },

    removeTrack: (trackId) => {
      set((s) => {
        const t = s.tracks.find((x) => x.id === trackId);
        if (!t) return {};
        if (t.kind === "video" && s.tracks.filter((x) => x.kind === "video").length <= 1) return {};
        const removedClipIds = new Set(s.clips.filter((c) => c.trackId === trackId).map((c) => c.id));
        const selClip = s.selection && "clipId" in s.selection ? s.selection.clipId : null;
        return {
          tracks: s.tracks.filter((x) => x.id !== trackId),
          clips: s.clips.filter((c) => c.trackId !== trackId),
          selection: selClip && removedClipIds.has(selClip) ? null : s.selection,
        };
      });
      scheduleSave();
    },

    reorderTrack: (trackId, dir) => {
      set((s) => {
        const t = s.tracks.find((x) => x.id === trackId);
        if (!t) return {};
        const sameKind = s.tracks.filter((x) => x.kind === t.kind).sort((a, b) => a.index - b.index);
        const pos = sameKind.findIndex((x) => x.id === trackId);
        const swapPos = dir === "up" ? pos + 1 : pos - 1; // up = higher index (foreground)
        if (swapPos < 0 || swapPos >= sameKind.length) return {};
        const other = sameKind[swapPos];
        return {
          tracks: s.tracks.map((x) =>
            x.id === t.id ? { ...x, index: other.index } : x.id === other.id ? { ...x, index: t.index } : x,
          ),
        };
      });
      scheduleSave();
    },

    setTrackMuted: (trackId, muted) => {
      set((s) => ({ tracks: s.tracks.map((t) => (t.id === trackId ? { ...t, muted } : t)) }));
      scheduleSave();
    },

    setTrackLocked: (trackId, locked) => {
      set((s) => ({ tracks: s.tracks.map((t) => (t.id === trackId ? { ...t, locked } : t)) }));
      scheduleSave();
    },

    detachAudio: (clipId) => {
      set((s) => {
        const clip = s.clips.find((c) => c.id === clipId);
        if (!clip) return {};
        const srcTrack = s.tracks.find((t) => t.id === clip.trackId);
        if (!srcTrack || srcTrack.kind !== "video") return {};
        let tracks = s.tracks;
        let audioTrack = s.tracks.find((t) => t.kind === "audio");
        if (!audioTrack) {
          audioTrack = { id: _newTrackId("audio"), kind: "audio", index: 100, muted: false, locked: false };
          tracks = [...s.tracks, audioTrack];
        }
        const audioClip: VideoEditClip = {
          ...clip,
          id: _newClipId(),
          trackId: audioTrack.id,
          muted: false,
          // carry volume keyframes; visual transforms are irrelevant for audio
          keyframes: { volume: clip.keyframes?.volume },
        };
        const clips = s.clips.map((c) => (c.id === clipId ? { ...c, muted: true } : c));
        clips.push(audioClip);
        return { tracks, clips, selection: { kind: "clip", clipId: audioClip.id } };
      });
      scheduleSave();
    },

    setClipStaticProp: (clipId, prop, value) => {
      set((s) => ({
        clips: s.clips.map((c) =>
          c.id === clipId
            ? { ...c, keyframes: { ...(c.keyframes || {}), [prop]: [{ f: 0, v: value }] } }
            : c,
        ),
      }));
      scheduleSave(`static:${clipId}:${prop}`);
    },

    setKeyframe: (clipId, prop, frame, value) => {
      set((s) => ({
        clips: s.clips.map((c) => {
          if (c.id !== clipId) return c;
          const dur = Math.max(1, c.durationInFrames);
          const f = Math.max(0, Math.min(Math.round(frame), dur - 1));
          const kfs = _kfUpsert(c.keyframes?.[prop], f, value);
          return { ...c, keyframes: { ...(c.keyframes || {}), [prop]: kfs } };
        }),
      }));
      scheduleSave(`kf:${clipId}:${prop}`);
    },

    removeKeyframe: (clipId, prop, frame) => {
      set((s) => ({
        clips: s.clips.map((c) => {
          if (c.id !== clipId) return c;
          const f = Math.round(frame);
          const kfs = (c.keyframes?.[prop] || []).filter((k) => k.f !== f);
          const next = { ...(c.keyframes || {}) };
          if (kfs.length) next[prop] = kfs;
          else delete next[prop];
          return { ...c, keyframes: next };
        }),
      }));
      scheduleSave();
    },

    moveKeyframe: (clipId, prop, fromFrame, toFrame) => {
      set((s) => ({
        clips: s.clips.map((c) => {
          if (c.id !== clipId) return c;
          const dur = Math.max(1, c.durationInFrames);
          const arr = c.keyframes?.[prop] || [];
          const ff = Math.round(fromFrame);
          const kf = arr.find((k) => k.f === ff);
          if (!kf) return c;
          const tf = Math.max(0, Math.min(Math.round(toFrame), dur - 1));
          const kfs = _kfUpsert(arr.filter((k) => k.f !== ff), tf, kf.v);
          return { ...c, keyframes: { ...(c.keyframes || {}), [prop]: kfs } };
        }),
      }));
      scheduleSave();
    },

    toggleMasterKeyframe: (clipId, timelineFrame) => {
      set((s) => {
        const clip = s.clips.find((c) => c.id === clipId);
        if (!clip) return {};
        const track = s.tracks.find((t) => t.id === clip.trackId);
        const props: KeyframeProp[] = track?.kind === "audio" ? ["volume"] : VIDEO_KF_PROPS;
        const dur = Math.max(1, clip.durationInFrames);
        const f = Math.max(0, Math.min(Math.round(timelineFrame - clip.from), dur - 1));
        const kf = clip.keyframes || {};
        // Only REMOVE when the frame is ALREADY fully keyed across every prop.
        // Otherwise ADD — filling in whichever props are missing a keyframe at
        // this frame. This lets the ◆ button COMPLETE a partial keyframe (e.g.
        // a frame that only has `scale`) instead of wiping everything, so you
        // can build up rotate + scale + position on the same keyframe.
        const fullyKeyed = props.every((p) => (kf[p] || []).some((k) => k.f === f));
        const next: typeof kf = { ...kf };
        for (const p of props) {
          if (fullyKeyed) {
            // master remove
            const arr = (next[p] || []).filter((k) => k.f !== f);
            if (arr.length) next[p] = arr;
            else delete next[p];
          } else {
            // master add / complete — capture the current interpolated value.
            // Props that already have a keyframe here keep their value.
            const val = sampleKeyframes(next[p], f, KF_DEFAULTS[p]);
            next[p] = _kfUpsert(next[p], f, val);
          }
        }
        return { clips: s.clips.map((c) => (c.id === clipId ? { ...c, keyframes: next } : c)) };
      });
      scheduleSave();
    },

    applyCanvasTransform: (clipId, patch, localFrame) => {
      set((s) => ({
        clips: s.clips.map((c) => {
          if (c.id !== clipId) return c;
          const dur = Math.max(1, c.durationInFrames);
          const f = Math.max(0, Math.min(Math.round(localFrame), dur - 1));
          const next = { ...(c.keyframes || {}) };
          // Decide PER-PROPERTY whether a drag moves a static value or writes a
          // keyframe at the playhead. A property is "static at zero" when it has
          // no keyframes, or exactly one at f=0 — a plain non-animated value
          // (e.g. an AI popup's x/y, where only scale/opacity are animated).
          // Dragging such a prop MOVES that single value instead of inserting a
          // keyframe at the playhead — the old code keyed this decision on the
          // WHOLE clip, so a popup's animated scale forced position into a
          // 2-keyframe slide that fought the scale envelope ("stuck to a
          // keyframe"). Props that are animated (>1 kf) OR carry an intentional
          // keyframe off f=0 (e.g. one placed by the ◆ button) still keyframe at
          // the playhead, so real animations + multi-prop keyframing are intact.
          const staticAtZero = (arr?: KF[]) =>
            !arr || arr.length === 0 || (arr.length === 1 && arr[0].f === 0);
          for (const k of Object.keys(patch) as ("x" | "y" | "scale" | "rotation")[]) {
            const v = patch[k];
            if (v == null) continue;
            const existing = next[k];
            if (staticAtZero(existing)) {
              next[k] = [{ f: 0, v }]; // move the static value
            } else {
              next[k] = _kfUpsert(existing, f, v); // keyframe at playhead, others preserved
            }
          }
          return { ...c, keyframes: next };
        }),
      }));
      scheduleSave(`gizmo:${clipId}`);
    },

    animateClip: (clipId) => {
      const clip = get().clips.find((c) => c.id === clipId);
      if (!clip || (clip.kind !== "text" && clip.kind !== "image")) return; // text + stickers only
      const dur = Math.max(1, clip.durationInFrames);
      const end = dur - 1;
      // Base values at f=0 — preserve a sticker's sizing + any gizmo position.
      const baseScale = sampleKeyframes(clip.keyframes?.scale, 0, 1);
      const x0 = sampleKeyframes(clip.keyframes?.x, 0, 0);
      const y0 = sampleKeyframes(clip.keyframes?.y, 0, 0);

      // Window shapes mirror the backend popup presets (_popup_keyframes in
      // yas_video_edit_ai_ops.py): entrance over the first ~12 frames, eased
      // exit over the last ~9; both shrink on tiny clips so in/out never
      // cross. The renderer interpolates LINEARLY, so easing is faked with
      // extra intermediate milestones (fast start, slow settle).
      const inF = Math.min(12, Math.max(1, Math.floor(dur / 3)));
      const outF = Math.min(9, Math.max(1, Math.floor(dur / 3)));
      const outStart = Math.max(inF, dur - outF);
      const inMid = Math.max(1, Math.round(inF * 0.5)); // overshoot peak
      const inSettle = Math.max(1, Math.round(inF * 0.8)); // undershoot before settling
      const inVis = Math.max(1, Math.round(inF * 0.6)); // opacity fully in

      // Clamp milestones to [0, end], dedupe frame collisions (earliest write
      // wins, so tiny clips degrade gracefully instead of spiking), sort.
      const lane = (pts: KF[]): KF[] => {
        const seen = new Map<number, number>();
        for (const p of pts) {
          const f = Math.max(0, Math.min(Math.round(p.f), end));
          if (!seen.has(f)) seen.set(f, p.v);
        }
        return [...seen.entries()].map(([f, v]) => ({ f, v })).sort((a, b) => a.f - b.f);
      };

      // Vary each click: random preset (and for slide, random direction+travel).
      const presets = ["pop", "fade", "slide", "title"] as const;
      const preset = presets[Math.floor(Math.random() * presets.length)];

      let opacity: KF[];
      let scale: KF[] = [{ f: 0, v: baseScale }]; // pinned when not animated
      let xs: KF[] = [{ f: 0, v: x0 }]; // x/y pinned when not animated
      let ys: KF[] = [{ f: 0, v: y0 }];

      if (preset === "pop") {
        // Springy pop: fade in fast while scale overshoots and settles
        // (0.90 → 1.05 → 0.985 → 1.0), then an eased fade + gentle shrink out.
        opacity = [{ f: 0, v: 0 }, { f: inVis, v: 1 }, { f: outStart, v: 1 }, { f: end, v: 0 }];
        scale = [
          { f: 0, v: baseScale * 0.9 },
          { f: inMid, v: baseScale * 1.05 },
          { f: inSettle, v: baseScale * 0.985 },
          { f: inF, v: baseScale },
          { f: outStart, v: baseScale },
          { f: end, v: baseScale * 0.94 },
        ];
      } else if (preset === "title") {
        // Title card: eased pop entrance, subtle "breathing" hold (scale
        // drifts to 1.03× across the body), then an eased fade/shrink out.
        opacity = [{ f: 0, v: 0 }, { f: inVis, v: 1 }, { f: outStart, v: 1 }, { f: end, v: 0 }];
        scale = [
          { f: 0, v: baseScale * 0.92 },
          { f: inMid, v: baseScale * 1.04 },
          { f: inSettle, v: baseScale * 0.995 },
          { f: inF, v: baseScale },
          { f: outStart, v: baseScale * 1.03 },
          { f: end, v: baseScale * 0.97 },
        ];
      } else if (preset === "slide") {
        // Slide: random direction + small 22–34px travel, shaped fast-then-
        // settle (~79% of the travel by 55% of the window); drifts halfway
        // back out on the exit.
        const dirs = ["up", "down", "left", "right"] as const;
        const dir = dirs[Math.floor(Math.random() * dirs.length)];
        const dist = 22 + Math.random() * 12; // 22–34px
        const ox = dir === "left" ? dist : dir === "right" ? -dist : 0;
        const oy = dir === "up" ? dist : dir === "down" ? -dist : 0;
        const slideMid = Math.max(1, Math.round(inF * 0.55));
        opacity = [{ f: 0, v: 0 }, { f: inF, v: 1 }, { f: outStart, v: 1 }, { f: end, v: 0 }];
        if (ox !== 0) {
          xs = [
            { f: 0, v: x0 + ox },
            { f: slideMid, v: x0 + ox * 0.21 },
            { f: inF, v: x0 },
            { f: outStart, v: x0 },
            { f: end, v: x0 + ox * 0.5 },
          ];
        }
        if (oy !== 0) {
          ys = [
            { f: 0, v: y0 + oy },
            { f: slideMid, v: y0 + oy * 0.21 },
            { f: inF, v: y0 },
            { f: outStart, v: y0 },
            { f: end, v: y0 + oy * 0.5 },
          ];
        }
      } else {
        // Fade: plain opacity in/out.
        opacity = [{ f: 0, v: 0 }, { f: inF, v: 1 }, { f: outStart, v: 1 }, { f: end, v: 0 }];
      }

      // Replace the opacity/scale/x/y lanes wholesale (rotation kept as-is)
      // and apply as ONE updateClip inside a gesture → one undo + one save.
      const keyframes: VideoEditClip["keyframes"] = {
        ...(clip.keyframes || {}),
        opacity: lane(opacity),
        scale: lane(scale),
        x: lane(xs),
        y: lane(ys),
      };
      const st = get();
      st.beginHistoryGesture();
      st.updateClip(clipId, { keyframes });
      st.endHistoryGesture();
    },

    dragGhost: null,
    beginDragGhost: (clipId, clientX, clientY, widthPx, label) =>
      set({ dragGhost: { clipId, clientX, clientY, widthPx, label, targetTrackId: null } }),
    updateDragGhost: (clientX, clientY, targetTrackId) =>
      set((s) => (s.dragGhost ? { dragGhost: { ...s.dragGhost, clientX, clientY, targetTrackId } } : {})),
    endDragGhost: () => set({ dragGhost: null }),

    addToBin: (entry) => {
      set((s) => {
        if (!s.project) return {};
        const bin = s.project.media_bin || [];
        if (bin.some((b) => b.kind === entry.kind && b.id === entry.id)) return {};
        return { project: { ...s.project, media_bin: [...bin, entry] } };
      });
      scheduleSave();
    },
    removeFromBin: (entry) => {
      set((s) => {
        if (!s.project) return {};
        const bin = (s.project.media_bin || []).filter((b) => !(b.kind === entry.kind && b.id === entry.id));
        return { project: { ...s.project, media_bin: bin } };
      });
      scheduleSave();
    },
    setClipFilters: (clipId, patch) => {
      set((s) => ({
        clips: s.clips.map((c) => (c.id === clipId ? { ...c, filters: { ...(c.filters || {}), ...patch } } : c)),
      }));
      scheduleSave(`filters:${clipId}`);
    },

    addTextClip: (opts) => {
      set((s) => {
        if (!s.project) return {};
        const { tracks, trackId } = _overlayTrack(s.tracks, s.clips);
        const dur = 90; // 3s @30fps
        const from = Math.max(0, s.playheadFrame || 0);
        const text: VideoEditClipText = {
          content: opts?.content ?? "Your text",
          fontFamily: DEFAULT_TEXT_FONT,
          fontUrl: null,
          fontSize: 96,
          color: "#ffffff",
          fontWeight: 700,
          textAlign: "center",
          lineHeight: 1.15,
          background: null,
          padding: 16,
          stroke: null,
          strokeWidth: 0,
          shadow: "0 4px 18px rgba(0,0,0,0.45)",
          ...(opts?.preset || {}),
        };
        const clip: VideoEditClip = {
          id: _newClipId(),
          trackId,
          kind: "text",
          trimBefore: 0,
          trimAfter: dur,
          from,
          durationInFrames: dur,
          keyframes: {},
          text,
        };
        return { tracks, clips: [...s.clips, clip], selection: { kind: "clip", clipId: clip.id } };
      });
      scheduleSave();
    },

    setClipText: (clipId, patch) => {
      set((s) => ({
        clips: s.clips.map((c) =>
          c.id === clipId && c.text ? { ...c, text: { ...c.text, ...patch } } : c,
        ),
      }));
      scheduleSave(`text:${clipId}`);
    },

    addShapeClip: (variant) => {
      set((s) => {
        if (!s.project) return {};
        const { tracks, trackId } = _overlayTrack(s.tracks, s.clips);
        const dur = 90; // 3s @30fps
        const from = Math.max(0, s.playheadFrame || 0);
        const W = s.project.width;
        const H = s.project.height;
        const base = Math.round(Math.min(W, H) * 0.4);
        const shape: VideoEditClipShape = {
          variant,
          fill: "#0a84ff",
          stroke: null,
          strokeWidth: 0,
          cornerRadius: variant === "rect" ? 24 : 0,
          width: variant === "line" ? Math.round(W * 0.6) : base,
          height: variant === "line" ? 8 : base,
        };
        const clip: VideoEditClip = {
          id: _newClipId(),
          trackId,
          kind: "shape",
          trimBefore: 0,
          trimAfter: dur,
          from,
          durationInFrames: dur,
          keyframes: {},
          shape,
        };
        return { tracks, clips: [...s.clips, clip], selection: { kind: "clip", clipId: clip.id } };
      });
      scheduleSave();
    },

    setClipShape: (clipId, patch) => {
      set((s) => ({
        clips: s.clips.map((c) =>
          c.id === clipId && c.shape ? { ...c, shape: { ...c.shape, ...patch } } : c,
        ),
      }));
      scheduleSave(`shape:${clipId}`);
    },

    addImageClip: (asset) => {
      set((s) => {
        if (!s.project) return {};
        const { tracks, trackId } = _overlayTrack(s.tracks, s.clips);
        const dur = 90; // 3s @30fps
        const from = Math.max(0, s.playheadFrame || 0);
        // "asset" = Assets-Library image/sticker; "still" = a yas_generations
        // keyframe/card/poster image picked from the media-bin picker.
        const isStill = asset.source === "still";
        const clip: VideoEditClip = {
          id: _newClipId(),
          trackId,
          kind: "image",
          source: isStill
            ? { kind: "still", still_id: asset.id }
            : { kind: "asset", asset_id: asset.id },
          // Same-origin file route (Player); backend re-resolves to a /data URL
          // for the headless export.
          sourceUrl: isStill
            ? `/api/yas-producer/video-editor/sources/still/${asset.id}/file`
            : `/api/agents/producer/assets/${asset.id}/file`,
          trimBefore: 0,
          trimAfter: dur,
          from,
          durationInFrames: dur,
          // Start at 60% size so logos/stickers read as overlays, not full-frame.
          keyframes: { scale: [{ f: 0, v: 0.6 }] },
          fit: asset.fit || "contain",
        };
        return { tracks, clips: [...s.clips, clip], selection: { kind: "clip", clipId: clip.id } };
      });
      scheduleSave();
    },

    addAudioClip: (audio) => {
      set((s) => {
        let tracks = s.tracks;
        let at = s.tracks.find((t) => t.kind === "audio");
        if (!at) {
          at = { id: _newTrackId("audio"), kind: "audio", index: 100, muted: false, locked: false };
          tracks = [...s.tracks, at];
        }
        const dur = Math.max(1, audio.durationInFrames || 90);
        const from = _trackEndFrame(s.clips, at.id);
        const clip: VideoEditClip = {
          id: _newClipId(),
          trackId: at.id,
          kind: "audio",
          // Music lives in the Assets Library (producer_assets, asset_kind=audio).
          source: { kind: "asset", asset_id: audio.id, audioFile: true, naturalDurationInFrames: dur },
          sourceUrl: `/api/agents/producer/assets/${audio.id}/file`,
          trimBefore: 0,
          trimAfter: dur,
          from,
          durationInFrames: dur,
          keyframes: {},
          muted: false,
        };
        return { tracks, clips: [...s.clips, clip], selection: { kind: "clip", clipId: clip.id } };
      });
      scheduleSave();
    },

    setTransition: (clipId, preset, durationFrames, timing = "linear") => {
      set((s) => {
        const clip = s.clips.find((c) => c.id === clipId);
        if (!clip) return {};
        // Previous neighbour on the same track (closest clip starting before this).
        const prev = s.clips
          .filter((c) => c.trackId === clip.trackId && c.id !== clipId && c.from < clip.from)
          .sort((a, b) => b.from - a.from)[0];
        if (!prev) return {};
        const prevEnd = prev.from + prev.durationInFrames;
        if (clip.from > prevEnd) return {}; // gap — can't transition
        const currentOverlap = prevEnd - clip.from;
        const maxOverlap = Math.max(1, Math.min(prev.durationInFrames, clip.durationInFrames) - 1);
        const desired = preset === "none" ? 0 : Math.max(1, Math.min(Math.round(durationFrames), maxOverlap));
        const delta = desired - currentOverlap; // shift this clip + later left by delta
        const clips = s.clips.map((c) => {
          if (c.trackId !== clip.trackId) return c;
          let nc = c;
          if (c.from >= clip.from) nc = { ...nc, from: Math.max(0, c.from - delta) };
          if (c.id === clipId)
            nc = {
              ...nc,
              transitionIn: preset === "none" ? null : { preset, durationInFrames: desired, timing },
            };
          return nc;
        });
        return { clips };
      });
      scheduleSave();
    },

    setTitle: (title) => {
      set((s) => ({ project: s.project ? { ...s.project, title } : s.project, title }));
      scheduleSave("title");
    },

    select: (sel) => set({ selection: sel }),

    setMultiSelection: (ids) => set({ multiSelection: Array.from(new Set(ids)) }),
    clearMultiSelection: () => set({ multiSelection: [] }),
    toggleClipInSelection: (id) =>
      set((s) => ({
        multiSelection: s.multiSelection.includes(id)
          ? s.multiSelection.filter((x) => x !== id)
          : [...s.multiSelection, id],
      })),
    selectClipsByKind: (kind) =>
      set((s) => ({
        multiSelection: s.clips
          .filter((c) =>
            kind === "all"
              ? true
              : kind === "video"
                ? !c.kind || c.kind === "video"
                : c.kind === kind,
          )
          .map((c) => c.id),
      })),
    setSelectMode: (on) => set({ selectMode: on }),
    selectAll: () => set((s) => ({ multiSelection: s.clips.map((c) => c.id) })),
    setPlayhead: (frame) => set({ playheadFrame: Math.max(0, Math.round(frame)) }),
    setZoom: (pxPerFrame) => set({ pxPerFrame: Math.max(0.05, Math.min(20, pxPerFrame)) }),
    setScroll: (frame) => set({ scrollFrame: Math.max(0, frame) }),
    setRender: (render) => set({ render }),

    flush: async () => {
      if (_veAiActive) return; // AI job owns the server doc — autosave paused
      const s = get();
      if (s.projectId == null || !s.project) return;
      // A save is already in flight → remember to re-save the latest edits when
      // it finishes (so nothing made during the save is lost).
      if (s.saveState === "saving") {
        _veResaveQueued = true;
        return;
      }
      set({ saveState: "saving" });
      try {
        const updated = await api.yasProducer.videoEditor.save(s.projectId, {
          project: s.project,
          tracks: s.tracks,
          clips: s.clips,
          base_rev: s.rev,
        });
        // Only adopt the backend's canonical project block if no newer edit has
        // landed mid-save (else we'd clobber the user's in-progress change).
        const patch: Partial<VideoEditorState> = {
          rev: updated.history?.rev ?? s.rev,
          render: updated.render,
          saveState: "saved",
          lastSavedAt: Date.now(),
          error: null,
        };
        if (!_veResaveQueued) patch.project = updated.project;
        set(patch);
      } catch (e: any) {
        set({ saveState: "error", error: String(e?.message || e) });
      } finally {
        if (_veResaveQueued) {
          _veResaveQueued = false;
          void get().flush(); // persist the edits that arrived during the save
        }
      }
    },

    flushBeacon: () => {
      if (_veAiActive) return; // AI job owns the server doc — autosave paused
      const s = get();
      if (s.projectId == null || !s.project) return;
      // Only when there are unsaved edits (dirty / mid-save / failed).
      if (s.saveState === "saved" || s.saveState === "idle") return;
      try {
        fetch(`/api/yas-producer/video-editor/${s.projectId}/timeline`, {
          method: "PATCH",
          keepalive: true, // survives page unload
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            project: s.project,
            tracks: s.tracks,
            clips: s.clips,
            base_rev: s.rev,
          }),
        });
      } catch {
        /* best effort */
      }
    },

    // --- AI assist (Phase 3) ------------------------------------------------
    runAi: async (feature, params = {}) => {
      const s = get();
      if (s.aiBusy) return; // one job per project — the backend 409s anyway
      const pid = s.projectId;
      if (pid == null) return;

      // 1. Push the latest local edits so the AI plans against the current doc.
      //    A save may already be in flight (flush() would just queue a resave
      //    and return) — wait it out briefly, then flush the pending edits.
      if (_veSaveTimer) {
        clearTimeout(_veSaveTimer);
        _veSaveTimer = null;
      }
      for (let i = 0; i < 50 && get().saveState === "saving"; i++) {
        await new Promise((r) => setTimeout(r, 100));
      }
      await get().flush();

      // 2. Pause autosave for the duration of the job (last-write-wins: a stray
      //    debounced PATCH would overwrite the AI's server-side write).
      _veAiActive = true;
      if (_veSaveTimer) {
        clearTimeout(_veSaveTimer);
        _veSaveTimer = null;
      }
      set({ aiBusy: true, aiError: null, aiJob: null });

      const ai = api.yasProducer.videoEditor.ai;
      try {
        let r!: { job: VideoEditAiJob };
        switch (feature) {
          case "analyze":
            r = await ai.analyze(pid, !!params.force);
            break;
          case "popups":
            r = await ai.popups(pid, { text: params.text || "", hint: params.hint });
            break;
          case "compose":
            r = await ai.compose(pid, {
              keys: params.keys,
              style: params.style,
              generate_bookends: params.generateBookends ?? false,
            });
            break;
          case "edit":
            r = await ai.edit(pid, { instruction: params.instruction });
            break;
          case "voice":
            r = await ai.voice(pid, {
              clip_id: params.clip_id || "",
              voice_id: params.voice_id,
              use_cloned: params.useCloned,
              use_as_is: params.useAsIs,
              source_upload_id: params.sourceUploadId,
            });
            break;
          case "music":
            r = await ai.music(pid, { mode: params.mode, query: params.query });
            break;
          case "sfx":
            r = await ai.sfx(pid, {
              clip_id: params.clip_id,
              query: params.query,
              mode: params.mode,
            });
            break;
          case "bookend":
            r = await ai.bookend(pid, {
              role: params.role ?? "both",
              reuse: params.reuse ?? true,
            });
            break;
          case "broll":
            r = await ai.broll(pid, {
              intro_outro: params.introOutro ?? true,
              bookend_mode: params.bookendMode ?? "generate",
              music: params.withMusic ?? true,
              music_mode: params.musicMode,
              title: params.title,
              intro_topic: params.introTopic,
              outro_topic: params.outroTopic,
              intro_upload_id: params.introUploadId,
              outro_upload_id: params.outroUploadId,
            });
            break;
          case "agent":
            r = await ai.agent(pid, {
              instruction: params.instruction || "",
              clip_id: params.clip_id ?? null,
            });
            break;
        }
        set({ aiJob: r.job });
        _veStopAiPoll();
        _veAiPollTimer = setTimeout(() => void pollAi(), VE_AI_POLL_MS);
      } catch (e) {
        // 409 (job already active) / 422 (missing key or field) / network.
        _veAiActive = false; // resume autosave
        set({ aiBusy: false, aiError: _veHumanError(e) });
      }
    },

    syncAiStatus: async () => {
      const pid = get().projectId;
      if (pid == null) return;
      try {
        const st = await api.yasProducer.videoEditor.ai.status(pid);
        if (get().projectId !== pid) return;
        const job = st.job;
        // Trust the SERVER's verdict, not the raw status. A backend restart
        // mid-run leaves the blob stuck at "planning" forever; adopting that
        // dead job would pin _veAiActive true, which silently kills autosave,
        // undo capture and the unload save for the rest of the session.
        // `job_active` applies the same staleness rule start_job uses.
        // (`?? ` keeps this working against a backend that predates the field.)
        const active =
          !!job &&
          (st.job_active ?? (job.status !== "done" && job.status !== "failed"));
        if (active && !get().aiBusy) {
          // A job is already running server-side (e.g. page reload mid-job):
          // adopt it — pause autosave + resume polling so it lands as usual.
          _veAiActive = true;
          if (_veSaveTimer) {
            clearTimeout(_veSaveTimer);
            _veSaveTimer = null;
          }
          set({ aiBusy: true, aiJob: job, aiError: null, analysisReady: st.analysis_ready });
          _veStopAiPoll();
          _veAiPollTimer = setTimeout(() => void pollAi(), VE_AI_POLL_MS);
        } else {
          set({ analysisReady: st.analysis_ready });
        }
      } catch {
        /* the panel works fine without the pre-check */
      }
    },

    syncAgentProviders: async () => {
      // Once per session: the server probes + caches the brain anyway, and the
      // answer isn't per-project. In-flight guard keeps two panels honest.
      if (get().agentProviders || _veProvidersInFlight) return;
      _veProvidersInFlight = true;
      try {
        const p = await api.yasProducer.videoEditor.ai.providers();
        set({ agentProviders: p });
      } catch {
        /* the panel works fine without the badge */
      } finally {
        _veProvidersInFlight = false;
      }
    },

    dismissAiError: () => set({ aiError: null }),

    clearChat: () => set({ aiChat: [] }),

    runChat: async (message, selectedClipIds, attachments) => {
      const text = (message || "").trim();
      if (!text) return;
      const pid = get().projectId;
      if (pid == null) return;

      if (get().aiBusy) {
        set((s) => ({
          aiChat: [...s.aiChat, {
            id: _chatId(), role: "user", text,
          }, {
            id: _chatId(), role: "assistant",
            text: "Hang on — I'm still finishing the last one.", kind: "info",
          }],
        }));
        return;
      }

      // Conversation memory = the thread BEFORE this message.
      const history = get().aiChat.slice(-8).map((m) => ({ role: m.role, text: m.text }));
      // Echo the user's message into the thread.
      set((s) => ({ aiChat: [...s.aiChat, { id: _chatId(), role: "user", text }] }));

      // Push local edits first so the AI plans against the current timeline,
      // then pause autosave for the duration (same as runAi).
      if (_veSaveTimer) {
        clearTimeout(_veSaveTimer);
        _veSaveTimer = null;
      }
      for (let i = 0; i < 50 && get().saveState === "saving"; i++) {
        await new Promise((r) => setTimeout(r, 100));
      }
      await get().flush();
      _veAiActive = true;
      // Drop the previous job, exactly as runAi does. Keeping it made the
      // finished run's agent_steps re-render as a LIVE "what I'm doing" list
      // the moment aiBusy flipped true again — the user would watch the last
      // run's tool calls replay as if they were happening now.
      set({ aiBusy: true, aiError: null, aiJob: null });

      try {
        const res = await api.yasProducer.videoEditor.ai.chat(pid, {
          message: text,
          history,
          selected_clip_ids: selectedClipIds ?? [],
          playhead_frame: Math.max(0, Math.round(get().playheadFrame || 0)),
          attachments: attachments ?? [],
        });

        // Generation (music/sfx/intro/voice) → a background job: poll it.
        if (res.job) {
          if (res.reply) {
            set((s) => ({
              aiChat: [...s.aiChat, {
                id: _chatId(), role: "assistant", text: res.reply, kind: "working",
              }],
            }));
          }
          _veChatActive = true;
          set({ aiJob: res.job });
          _veStopAiPoll();
          _veAiPollTimer = setTimeout(() => void pollAi(), VE_AI_POLL_MS);
          return;
        }

        // No job — either a direct edit applied server-side, or a reply only.
        _veAiActive = false; // resume autosave before applying (applyServerDoc schedules a save)
        if ((res.applied ?? 0) > 0) {
          // Adopt the edited doc as ONE undo step (same as the job-done path).
          try {
            const doc = await api.yasProducer.videoEditor.fetch(pid);
            if (get().projectId === pid) get().applyServerDoc(doc);
          } catch {
            /* the edit saved server-side regardless; a reload will show it */
          }
          set((s) => ({
            aiBusy: false,
            aiChat: [...s.aiChat, {
              id: _chatId(), role: "assistant",
              text: res.reply || "Done.", kind: "done",
            }],
          }));
        } else {
          set((s) => ({
            aiBusy: false,
            aiChat: res.reply
              ? [...s.aiChat, { id: _chatId(), role: "assistant", text: res.reply, kind: "info" }]
              : s.aiChat,
          }));
        }
      } catch (e) {
        _veAiActive = false;
        set((s) => ({
          aiBusy: false,
          aiChat: [...s.aiChat, {
            id: _chatId(), role: "assistant", text: _veHumanError(e), kind: "error",
          }],
        }));
      }
    },
  };
});

// ---------------------------------------------------------------------------
// Video sources store — the pickable generated-clips/uploads list for the
// editor's media bin. Same 24h-TTL + in-flight-dedup shape as the library
// store; search/kind filtering is done client-side from the cached list.
// ---------------------------------------------------------------------------

interface VideoSourcesState {
  items: VideoSource[];
  loading: boolean;
  error: string | null;
  lastFetchedAt: number | null;

  fetch: (params?: { limit?: number }) => Promise<void>;
  addItem: (item: VideoSource) => void;
  invalidate: () => void;
}

export const useVideoSourcesStore = create<VideoSourcesState>((set, get) => ({
  items: [],
  loading: false,
  error: null,
  lastFetchedAt: null,

  fetch: async (params = {}) => {
    const state = get();
    if (
      state.lastFetchedAt &&
      Date.now() - state.lastFetchedAt < CACHE_TTL_MS &&
      !state.loading
    ) {
      return;
    }
    const inflightKey = "videosrc:list";
    const existing = _inflight.get(inflightKey);
    if (existing) return existing;

    const p = (async () => {
      set({ loading: true, error: null });
      try {
        const r: VideoSourceList = await api.yasProducer.videoEditor.listSources({
          limit: params.limit ?? 500,
        });
        set({ items: r.items, loading: false, lastFetchedAt: Date.now() });
      } catch (e: any) {
        set({ error: String(e?.message || e), loading: false });
      } finally {
        _inflight.delete(inflightKey);
      }
    })();
    _inflight.set(inflightKey, p);
    return p;
  },

  // Newly-uploaded sources go to the front so they're pickable immediately.
  addItem: (item) => set((s) => ({ items: [item, ...s.items.filter((i) => !(i.kind === item.kind && i.id === item.id))] })),

  invalidate: () => set({ lastFetchedAt: null }),
}));

// ---------------------------------------------------------------------------
// Campaigns (autonomous Producer tab)
//
// Merges the two best caching shapes already in this file: the per-params Map +
// in-flight dedup from useYasLibraryStore, and the `force` escape hatch from
// useProducerAssetsStore. Neither store had both, and campaigns need both — the
// hub asks for one list while a detail view asks for one campaign, and every
// approve/decline must refetch immediately rather than wait out the TTL.
//
// The TTL here is deliberately SHORT. Campaign rows change on their own: a
// background worker generates, captions and books items with no user action, so
// a 24h cache would show a stale board. The page also polls while anything is
// in flight; this cache exists to make tab switches instant, not to avoid work.
// ---------------------------------------------------------------------------

const CAMPAIGN_TTL_MS = 15_000;

interface CampaignsState {
  items: Campaign[];
  loading: boolean;
  error: string | null;
  lastFetchedAt: number | null;
  /** Items awaiting gate 1 across all campaigns (drives the review queue). */
  review: CampaignItem[];
  reviewFetchedAt: number | null;

  fetch: (opts?: { force?: boolean }) => Promise<void>;
  fetchReview: (opts?: { force?: boolean }) => Promise<void>;
  /** Refetch both after a mutation — the two views share one source of truth. */
  refresh: () => Promise<void>;
  invalidate: () => void;
  /** Patch one campaign in place (optimistic pause/resume, rename). */
  patchLocal: (campaignId: number, patch: Partial<Campaign>) => void;
  /** Drop an item from the review queue the moment it's decided. */
  dropReviewItem: (itemId: number) => void;
}

export const useCampaignsStore = create<CampaignsState>((set, get) => ({
  items: [],
  loading: false,
  error: null,
  lastFetchedAt: null,
  review: [],
  reviewFetchedAt: null,

  fetch: async (opts = {}) => {
    const st = get();
    if (
      !opts.force &&
      st.lastFetchedAt &&
      Date.now() - st.lastFetchedAt < CAMPAIGN_TTL_MS &&
      !st.loading
    ) {
      return;
    }
    const key = "campaigns:list";
    const existing = _inflight.get(key);
    if (existing) return existing;

    const p = (async () => {
      set({ loading: true, error: null });
      try {
        const r = await api.campaigns.list();
        set({ items: r.items, loading: false, lastFetchedAt: Date.now() });
      } catch (e: any) {
        set({ error: String(e?.message || e), loading: false });
      } finally {
        _inflight.delete(key);
      }
    })();
    _inflight.set(key, p);
    return p;
  },

  fetchReview: async (opts = {}) => {
    const st = get();
    if (
      !opts.force &&
      st.reviewFetchedAt &&
      Date.now() - st.reviewFetchedAt < CAMPAIGN_TTL_MS
    ) {
      return;
    }
    const key = "campaigns:review";
    const existing = _inflight.get(key);
    if (existing) return existing;

    const p = (async () => {
      try {
        const r = await api.campaigns.reviewQueue();
        set({ review: r.items, reviewFetchedAt: Date.now() });
      } catch (e: any) {
        set({ error: String(e?.message || e) });
      } finally {
        _inflight.delete(key);
      }
    })();
    _inflight.set(key, p);
    return p;
  },

  refresh: async () => {
    await Promise.all([
      get().fetch({ force: true }),
      get().fetchReview({ force: true }),
    ]);
  },

  invalidate: () => set({ lastFetchedAt: null, reviewFetchedAt: null }),

  patchLocal: (campaignId, patch) =>
    set((s) => ({
      items: s.items.map((c) => (c.id === campaignId ? { ...c, ...patch } : c)),
    })),

  dropReviewItem: (itemId) =>
    set((s) => ({ review: s.review.filter((i) => i.id !== itemId) })),
}));
