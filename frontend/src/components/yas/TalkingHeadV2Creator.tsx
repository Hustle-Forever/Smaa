/**
 * Yas Producer — Human Element Video studio (v5 UI).
 *
 * Pipeline (unchanged from v4):
 *   • Topic + optional dialogue + optional references
 *   • → GPT script + 3 image previews
 *   • → 3 Veo clips (intro 4s · main 8s w/ audio · outro 4s)
 *   • → Rebrand (the studio on each scene, what ships picked per scene)
 *   • → Finalize: Remotion stitch → final 9:16 MP4 → save to assets
 *
 * v5 changes (purely presentational):
 *   • Two-column "studio" layout: sticky composer (left) + canvas (right).
 *   • StepChips progress indicator at the top of the canvas.
 *   • Empty-canvas hero before generation; phase-aware canvas content.
 *   • Collapsible "Look & Feel" + "Engines" sections in the composer.
 *   • Single primary CTA at the bottom of the composer; label adapts to
 *     phase with cost preview + shimmer bar during work.
 *   • Script JSON moved from inline panel to a portaled side drawer.
 *   • Tighter tile dimensions so the 3-up row fits a laptop screen.
 *   • Cmd/Ctrl+Enter fires the primary CTA when it's enabled.
 */
import React, { useEffect, useRef, useState } from "react";

import { aspectClass as aspectClassFor } from "../../lib/aspect";
import {
  VIDEO_SIZE_HINT,
  VIDEO_SIZE_LOCKED_HINT,
  stillShapeOff,
  videoSizeLocked,
} from "../../lib/thVideoSize";
import {
  DEFAULT_FRAMING,
  FRAMING_CHOICES,
  framingButtonTitle,
  framingChangeToast,
  framingFromRun,
  framingHint,
  framingMove,
  sceneFramingPatch,
  stillFraming,
  type StillFramingState,
} from "../../lib/thFraming";
import {
  anyCharacterSet,
  lockCharacterField,
  speakerApplies,
  speakerDraftFromRun,
  speakerPatch,
} from "../../lib/thSpeaker";
import {
  DEFAULT_REF_MODE,
  REF_MODE_CHOICES,
  bookendClipWaits,
  cardRetryButton,
  cardTileNote,
  cardWordsNote,
  cardsPhrase,
  editingExisting as runIsBeingEdited,
  keepTextHint,
  missingCards,
  normRefMode,
  pickedCardFrom,
  pickedCardFromRefs,
  pickedCardLabel,
  previewsReady as runPreviewsReady,
  refModeHint,
  type BookendRole,
  type PickedCard,
} from "../../lib/thBookends";
import {
  STILL_KEPT_AFTER_ERROR,
  STILL_REFINE_PLACEHOLDER,
  STILL_REGEN_FAILED_HINT,
  UNDO_TITLE,
  previewTileStatus,
  stillBusyLabel,
  stillDoneToast,
  stillImageCost,
  stillFailTitle,
  stillLimitHint,
  stillStatusLine,
  stillTileButtons,
  stillWorkStatusLine,
  stillWorkToast,
  type StillAction,
} from "../../lib/thStill";
import { shortcutFires, videosAction, videosStaleNote } from "../../lib/thVideos";
import { api } from "../../lib/api";
import { isShown } from "../../lib/isShown";
import { prefetchAssetImages } from "../../lib/prefetchAssets";
import type { ProducerBrief } from "../../lib/producerBrief";
import { useProducerAssetsStore, useYasLibraryStore } from "../../lib/store";
import { useToast } from "../ToastProvider";
import type {
  ProducerAssetRow,
  TalkingHeadAnimation,
  TalkingHeadAspect,
  TalkingHeadCard,
  TalkingHeadFraming,
  TalkingHeadRefMode,
  TalkingHeadRun,
  TalkingHeadVideoQuality,
  YasImageModel,
  YasUploadRef,
} from "../../lib/types";

import { landingStage as landingOf, parseRebrandStep, rebrandStep, stageOfStep } from "../../lib/rebrandStage";
import { AssetPickerModal } from "./AssetPickerModal";
import { EnhanceButton } from "./EnhanceButton";
import { RebrandStage } from "./rebrand-stage/RebrandStage";
import type { ContinueToPreview } from "./roadmap/types";
import { SceneRow } from "./SceneRow";
import { type StudioStage } from "./TalkingHeadV2StepChips";
import { StepperNav, type StageStatus } from "./StepperNav";
import { TalkingHeadV2ScriptDrawer, ScriptReview } from "./TalkingHeadV2ScriptDrawer";
import { TalkingHeadV2SpeakerField } from "./TalkingHeadV2SpeakerField";
import { TalkingHeadV2PreviousCards } from "./TalkingHeadV2PreviousCards";
import {
  TalkingHeadV2ScenesEditor,
  MAX_SCENES,
  EMPTY_SCENE_OVERRIDE,
  isSceneOverridden,
  type SceneOverride,
} from "./TalkingHeadV2ScenesEditor";

type Phase =
  | "idle"
  | "scripting"
  | "previewing"
  | "retrying_main"
  | "retrying_intro"
  | "retrying_outro"
  | "retrying_video_main"
  | "retrying_video_intro"
  | "retrying_video_outro"
  // v6 — multi-scene retry phases. The index of the active scene lives
  // in retryingSceneIdx state alongside.
  | "retrying_scene"
  | "retrying_scene_video"
  // v2 editor — applying a new image auto-regenerates the affected still(s);
  // this phase polls fetch_run until no scene's keyframe is still generating.
  | "polling_stills"
  | "finalizing"
  | "polling_videos"
  | "videos_ready"
  | "stitching"
  | "ready"
  | "saving"
  | "saved";

type SlotKey = "character" | "bg" | "intro" | "outro";

interface RefSlotState {
  asset: ProducerAssetRow | null;
  upload: YasUploadRef | null;
  /** intro/outro only: a previous video's card ("Previous videos"). */
  card?: PickedCard | null;
  /** intro/outro only: how a picked image is used (default "as_is": the
   *  card is the image itself). */
  refMode?: TalkingHeadRefMode;
  /** intro/outro only: "Keep card text" (a kept card keeps its own words;
   *  off, a headline that differs from the script is swapped). */
  keepText?: boolean;
}

const EMPTY_SLOT: RefSlotState = { asset: null, upload: null, card: null, refMode: DEFAULT_REF_MODE, keepText: false };

// Static cost estimates shown on the primary CTA so the user knows what
// they're committing to. Real GCP / OpenAI billing remains the source of
// truth — these are just helpful hints, not exact figures.
// The clips' cost is not one of them: the Videos button shows the
// backend's videos_plan, priced at the meter's own Veo rates (lib/thVideos).
const COST_HINT = {
  generate_previews: "~$0.05",
  retry_preview: "~$0.02",
};

interface Props {
  onBack: () => void;
  /** v2 — when set, hydrate from this existing run ("Continue editing"). */
  initialRunId?: number | null;
  /** App navigation — used to deep-link into a freshly-created editor project. */
  navigate?: (tab: "producer", param?: string) => void;
  /** Roadmap: hand the finished composition to the Preview step. Absent standalone. */
  onContinue?: ContinueToPreview;
  /** Roadmap: "Open in editor" hands the new VE project to the roadmap (which
   *  switches its Editing stage to the editor, in place). Absent standalone → deep-link. */
  onOpenInEditor?: (projectId: number) => void;
  /** Roadmap: mirror the live run id up to the URL so a hard refresh (F5)
   *  restores THIS run at its stage. Absent standalone. */
  onRunId?: (id: number) => void;
  /** Roadmap: report the wizard's current sub-step (activeStage) so a hard
   *  refresh restores THIS run at its EXACT stage. Absent standalone. */
  onStep?: (step: string) => void;
  /** Roadmap: seed the starting sub-step on resume (deep-link "@<step>"). */
  initialStep?: string | null;
  /** Trend Finder "Make this" seed. One-shot, applied at mount only — the
   *  roadmap bumps projectNonce for every hand-off, so this component is always
   *  freshly mounted when a brief arrives. Never set together with
   *  initialRunId (a resume wins; the roadmap clears resumeRunId first). */
  initialBrief?: ProducerBrief | null;
  /** Roadmap: this creator is the one on screen (it stays mounted, hidden,
   *  when another tool or step is open). The Rebrand studio mounts, and
   *  ⌘/Ctrl+Enter acts, only while it is. Standalone: always. */
  active?: boolean;
}

export function TalkingHeadV2Creator({ onBack, initialRunId = null, navigate, onContinue, onOpenInEditor, onRunId, onStep, initialStep = null, initialBrief = null, active = true }: Props) {
  // ----- Inputs -----
  const [topic, setTopic] = useState("");

  // v6 — scene count (1..MAX_SCENES). Per-scene dialogues and overrides
  // live in fixed-length arrays so reducing scene_count doesn't lose the
  // user's typing if they bump it back up. Only the first `sceneCount`
  // entries are sent to the backend.
  const [sceneCount, setSceneCount] = useState(1);
  const [sceneDialogues, setSceneDialogues] = useState<string[]>(
    () => Array.from({ length: MAX_SCENES }, () => ""),
  );
  const [sceneOverrides, setSceneOverrides] = useState<SceneOverride[]>(
    () => Array.from({ length: MAX_SCENES }, () => ({ ...EMPTY_SCENE_OVERRIDE })),
  );
  const [enhancingSceneIdx, setEnhancingSceneIdx] = useState<number | null>(null);
  // Per-scene picker target (when the user clicks "Pick" inside a scene's
  // override expansion). Distinct from the global Look & Feel picker.
  const [scenePicker, setScenePicker] = useState<{
    idx: number;
    kind: "character" | "bg";
  } | null>(null);
  const [sceneUploading, setSceneUploading] = useState<{
    idx: number;
    kind: "character" | "bg";
  } | null>(null);

  const [slots, setSlots] = useState<Record<SlotKey, RefSlotState>>({
    character: { ...EMPTY_SLOT },
    bg: { ...EMPTY_SLOT },
    intro: { ...EMPTY_SLOT },
    outro: { ...EMPTY_SLOT },
  });
  const [lockCharacter, setLockCharacter] = useState(true);
  // One-shot "New bg" intent for the GLOBAL bg slot (single-scene edits, or
  // multi-scene scenes that inherit the global bg). Reset after each Apply.
  const [globalBgMode, setGlobalBgMode] = useState<"keep" | "new">("keep");
  // Bookend generate-toggles (default on = today's behavior). Off → the
  // bookend is never generated and is omitted from the final stitch. Backed by
  // the run's `intro_excluded`/`outro_excluded` flag (single "include" switch).
  const [generateIntro, setGenerateIntro] = useState(true);
  const [generateOutro, setGenerateOutro] = useState(true);
  // Output video size. Veo supports only 9:16 and 16:9. Drives all stills +
  // cards + Veo + the final stitch dimensions.
  const [videoAspect, setVideoAspect] = useState<TalkingHeadAspect>("9:16");
  // Veo tier — "quality" routes clips through the premium model.
  const [videoQuality, setVideoQuality] = useState<TalkingHeadVideoQuality>("normal");
  // How close the camera is for every still (Above the hip for a new reel;
  // a run made before framing existed reads as Close-up). A still's tile can
  // re-shoot that one still at another framing.
  const [framing, setFraming] = useState<TalkingHeadFraming>(DEFAULT_FRAMING);
  // Portrait fallback preserved -- this surface is vertical-first, whereas
  // GeneralVideoCreator's equivalent falls back to landscape. That
  // disagreement is exactly why the fallback is an explicit argument.
  const aspectClass = aspectClassFor(videoAspect, "9:16");

  // ----- Pickers -----
  const [pickerOpen, setPickerOpen] = useState<null | SlotKey>(null);
  const uploadInputs = {
    character: useRef<HTMLInputElement>(null),
    bg: useRef<HTMLInputElement>(null),
    intro: useRef<HTMLInputElement>(null),
    outro: useRef<HTMLInputElement>(null),
  };
  const [uploading, setUploading] = useState<Record<SlotKey, boolean>>({
    character: false, bg: false, intro: false, outro: false,
  });

  // ----- Pipeline state -----
  const [phase, setPhase] = useState<Phase>("idle");
  const [run, setRun] = useState<TalkingHeadRun | null>(null);
  // What one still image costs now (the backend's meter rate): the Stills
  // tiles, toasts and the Framing hint say it.
  const imageCost = stillImageCost(run?.still_image_usd);
  const [enhancing, setEnhancing] = useState(false);
  const [feedback, setFeedback] = useState<Record<"main" | "intro" | "outro", string>>({
    main: "", intro: "", outro: "",
  });
  const [videoFeedback, setVideoFeedback] = useState<Record<"main" | "intro" | "outro", string>>({
    main: "", intro: "", outro: "",
  });
  // v6 — per-scene feedback maps. Keyed by scene index. Distinct from the
  // legacy main/intro/outro maps so single-scene flows don't change.
  const [sceneFeedback, setSceneFeedback] = useState<Record<number, string>>({});
  const [sceneVideoFeedback, setSceneVideoFeedback] = useState<Record<number, string>>({});
  // Tracks which scene tile is mid-retry; null = none. The current `phase`
  // also flips to "retrying_scene" / "retrying_scene_video" while busy.
  const [retryingSceneIdx, setRetryingSceneIdx] = useState<number | null>(null);
  // What the main / scene still in flight is doing (Refine, Regenerate or
  // Undo): its tile overlay and the status line say so.
  const [stillAction, setStillAction] = useState<StillAction | null>(null);
  // When the still change in flight (or the re-shoot being polled) began:
  // its tile and the status line count the seconds, so a slow image model
  // is never a silent wait. `clock` ticks once a second while one runs.
  const [stillStartedAt, setStillStartedAt] = useState<number | null>(null);
  const [clock, setClock] = useState(() => Date.now());
  useEffect(() => {
    if (stillStartedAt == null) return;
    const t = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [stillStartedAt]);
  const stillSeconds = stillStartedAt == null ? null : Math.max(0, (clock - stillStartedAt) / 1000);
  // The intro / outro cards being made by makeCards (their tiles show it).
  const [makingCards, setMakingCards] = useState<BookendRole[]>([]);
  // The Look step's Speaker field. Follows the run's speaker whenever the
  // server's value changes (a new run, a re-script, a save).
  const [speakerDraft, setSpeakerDraft] = useState("");
  const runSpeakerSubject = speakerDraftFromRun(run);
  useEffect(() => {
    setSpeakerDraft(runSpeakerSubject);
  }, [run?.id, runSpeakerSubject]);

  // ----- v5 UI-only state -----
  // Look & Feel section opens once the user fills any ref slot for the first
  // time; remembers its open/closed state thereafter.
  const [lookOpen, setLookOpen] = useState(false);
  const [enginesOpen, setEnginesOpen] = useState(false);
  const [scriptDrawerOpen, setScriptDrawerOpen] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  // ----- Stage-workspace state -----
  // activeStage: which stage the user is VIEWING (Topic→Final), user-controlled
  //   via StepperNav. Decoupled from `phase` (which now only tracks the in-flight
  //   async op). This is the core of the redesign — navigation is first-class,
  //   so every reached stage is freely reachable + editable.
  // notFound: a resumed run no longer exists (deleted) → clean empty state.
  // hydrating: loading an existing run via initialRunId (resume).
  // applying: an /edit or batch request is in flight.
  // batchMode/selectedScenes: batch-regen selection (used by Stills + Videos).
  const [activeStage, setActiveStage] = useState<StudioStage>("topic");
  // The Rebrand stage's scene (scene_<i>, later intro/outro): a refresh on
  // "@rebrand_scene_1" reopens it; null opens the first included main scene.
  const [rebrandTarget, setRebrandTarget] = useState<string | null>(
    () => parseRebrandStep(initialStep)?.target ?? null,
  );
  // The stepper row: the Rebrand stage scrolls it to the top of the screen.
  const stepperRowRef = useRef<HTMLDivElement>(null);
  // The creator's own box (its ⌘/Ctrl+Enter acts only while it is on screen).
  const rootRef = useRef<HTMLDivElement>(null);
  const [notFound, setNotFound] = useState(false);
  const [hydrating, setHydrating] = useState<boolean>(!!initialRunId);
  const [applying, setApplying] = useState(false);
  // Finalize sent and not answered yet: Final shows the reel being made, but
  // the phase (and its 5 s poll, which would read the last render's status
  // and say "ready" or "failed" for it) waits for the server's answer.
  const [sendingReel, setSendingReel] = useState(false);
  const [batchMode, setBatchMode] = useState(false);
  const [selectedScenes, setSelectedScenes] = useState<Set<number>>(new Set());

  // Jump to a stage from the stepper (clears any batch selection in flight).
  function goToStage(s: StudioStage) {
    setActiveStage(s);
    setBatchMode(false);
    setSelectedScenes(new Set());
  }

  const toast = useToast();
  const assetsStore = useProducerAssetsStore();

  // Trend Finder "Make this" — seed the topic and, when the brief carries them,
  // the per-scene spoken lines. Mount-only: the roadmap remounts this creator
  // (projectNonce) for every hand-off. `sceneDialogues` is a FIXED-LENGTH
  // MAX_SCENES array, so the lines are written BY INDEX (its length never
  // changes); only the first `sceneCount` entries are ever submitted. With no
  // initialRunId there is no hydration, so activeStage is already "topic" and
  // the seeded fields are the first thing on screen.
  useEffect(() => {
    if (!initialBrief || initialRunId != null) return;
    setTopic(initialBrief.topic);
    const lines = initialBrief.dialogue.filter((s) => s.trim()).slice(0, MAX_SCENES);
    if (lines.length) {
      setSceneCount(lines.length);
      setSceneDialogues((cur) => cur.map((d, i) => lines[i] ?? d));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    assetsStore.fetch({ limit: 500 });
  }, []);

  // Warm the browser cache for the Look-slot picker thumbnails as soon as the
  // asset list lands, so opening the character/bg/intro/outro picker paints
  // instantly instead of firing every image request only when the modal mounts.
  // Covers both the global slot pickers and the per-scene override pickers.
  useEffect(() => {
    if (assetsStore.items.length === 0) return;
    prefetchAssetImages(assetsStore.items, [
      "video_interview",
      "img_human_character",
      "img_bg",
      "img_intro",
      "img_outro",
    ]);
  }, [assetsStore.items]);

  // ----- Resume: hydrate from an existing run (Continue editing) -----
  // No ref-guard here: the `cancelled` flag makes this correct under React
  // StrictMode's mount→unmount→mount double-invoke (a persisted ref would let
  // the first run cancel and then block the real second mount → no hydration).
  useEffect(() => {
    if (!initialRunId) return;
    let cancelled = false;
    (async () => {
      try {
        // Ensure the assets list is loaded so we can resolve ref thumbnails.
        if (assetsStore.items.length === 0) {
          try { await assetsStore.fetch({ limit: 500 }); } catch { /* non-fatal */ }
        }
        const fresh = await api.yasProducer.talkingHeadV2.fetch(initialRunId);
        if (cancelled) return;
        rehydrateFromRun(fresh);
      } catch (e: any) {
        // Run is gone (deleted) or unreachable → clean "no longer exists" state
        // instead of leaving a run that 404s on every action.
        if (!cancelled) {
          setNotFound(true);
          setHydrating(false);
        }
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialRunId]);

  // A run-mutating action can 404 if the run was deleted out from under us
  // (e.g. cleaned up elsewhere, or a stale deep-link). Surface the clean
  // "no longer exists" state instead of a toast that's easy to miss — otherwise
  // the button just appears to do nothing. Returns true when it handled the error.
  function handleRunGone(e: any): boolean {
    const msg = String(e?.message ?? e ?? "");
    if (/not found/i.test(msg) || /(^|\D)404(\D|$)/.test(msg)) {
      setNotFound(true);
      return true;
    }
    return false;
  }

  function assetById(id: number | null | undefined): ProducerAssetRow | null {
    if (id == null) return null;
    // Read the LIVE store, not the render-bound `assetsStore` snapshot: resume
    // hydration awaits the assets fetch and then resolves refs in the SAME tick,
    // so the captured `assetsStore.items` is still the empty render-0 array.
    // Reading getState() picks up the just-fetched items, so per-scene character
    // / bg refs hydrate correctly (otherwise Apply would send null and clear the
    // character — "the character also gets changed").
    const items = useProducerAssetsStore.getState().items;
    return items.find((a) => a.id === id) ?? null;
  }

  function slotFromRefs(
    assetId: number | null | undefined,
    uploadId: number | null | undefined,
    refMode?: TalkingHeadRefMode,
    videoUploadId?: number | null | undefined,
    videoUrl?: string | null | undefined,
    cardId?: number | null | undefined,
    keepText?: boolean | null | undefined,
  ): RefSlotState {
    const mode: TalkingHeadRefMode = normRefMode(refMode);
    const keep = !!keepText;
    // A previous video's card: the slot shows it from its id (an upload
    // still wins, as the server reads the picks).
    const card = uploadId == null ? pickedCardFromRefs(cardId) : null;
    if (card) return { asset: null, upload: null, card, refMode: mode, keepText: keep };
    const asset = assetById(assetId);
    if (asset) return { asset, upload: null, card: null, refMode: mode, keepText: keep };
    if (videoUploadId != null) {
      // Direct-video bookend on resume. Tag the mime so slotIsVideo() detects it
      // and the next generate re-sends it as a video. The serializer resolves the
      // upload id → serve URL (refs.*_video_url) so the clip previews on resume.
      return {
        asset: null,
        upload: { id: videoUploadId, token: "", url: videoUrl ?? null, mime_type: "video/mp4" } as unknown as YasUploadRef,
        refMode: mode,
      };
    }
    if (uploadId != null) {
      // Minimal upload ref so the slot reads as "set" on resume. We don't have
      // the upload token here (only the id), so no thumbnail — the user can
      // re-pick if they want to change it.
      return {
        asset: null,
        upload: { id: uploadId, token: "", url: null } as unknown as YasUploadRef,
        card: null,
        refMode: mode,
        keepText: keep,
      };
    }
    return { ...EMPTY_SLOT };
  }

  function derivePhaseFromRun(r: TalkingHeadRun): Phase {
    const cs = r.composition_status;
    if (cs === "ready") return "ready";
    if (cs === "rendering") return "stitching";
    if (cs === "queued") return "polling_videos";
    if (cs === "videos_ready" || cs === "failed") return "videos_ready";
    return "idle"; // script/previews stage
  }

  // Where to land the workspace when (re)opening an existing run — the furthest
  // reached stage (lib/rebrandStage: a reel that can be finalized opens on
  // Rebrand; a finished one whose clips went out of date on Final, never on
  // Videos, whose auto-generation would spend Veo). Script written but no
  // stills yet (Topic→Look flow, closed before generating) → Look, so the
  // script + ref pickers show. Afterwards the user navigates freely.
  function landingStage(r: TalkingHeadRun): StudioStage {
    return landingOf({
      composition_status: r.composition_status,
      reel_ready: r.reel_ready,
      hasStills: !!r.main?.url || (r.scenes ?? []).some((s) => !!s.keyframe_url),
    });
  }

  function rehydrateFromRun(r: TalkingHeadRun) {
    setRun(r);
    setTopic(r.topic ?? "");
    const sc = Math.max(1, r.scene_count ?? 1);
    setSceneCount(sc);
    setLockCharacter(!!r.lock_character);
    setGenerateIntro(!r.intro_excluded);
    setGenerateOutro(!r.outro_excluded);
    setVideoAspect(r.aspect_ratio === "16:9" ? "16:9" : "9:16");
    setVideoQuality(r.video_quality === "quality" ? "quality" : "normal");
    setFraming(framingFromRun(r));
    // Per-scene dialogues (fixed-length array; fill from run scenes by index).
    const dlgs = Array.from({ length: MAX_SCENES }, () => "");
    (r.scenes ?? []).forEach((s) => {
      if (s.index >= 0 && s.index < MAX_SCENES) dlgs[s.index] = s.dialogue ?? "";
    });
    setSceneDialogues(dlgs);
    // Per-scene overrides.
    const ovs = Array.from({ length: MAX_SCENES }, () => ({ ...EMPTY_SCENE_OVERRIDE }));
    (r.scenes ?? []).forEach((s) => {
      if (s.index < 0 || s.index >= MAX_SCENES) return;
      const charSlot = slotFromRefs(s.character_asset_id, s.character_upload_id);
      const bgSlot = slotFromRefs(s.bg_asset_id, s.bg_upload_id);
      ovs[s.index] = {
        character_asset: charSlot.asset,
        character_upload: charSlot.upload,
        bg_asset: bgSlot.asset,
        bg_upload: bgSlot.upload,
        // Restore the "↻ New bg" toggle: a scene with a GENERATED still (has a
        // keyframe and was NOT used as-is) → "new" (on); a scene used as-is or
        // not yet generated → "keep" (off). This is how the toggle state is saved.
        bg_mode: s.keyframe_url && !s.as_is ? "new" : "keep",
      } as SceneOverride;
    });
    setSceneOverrides(ovs);
    // Global ref slots.
    const refs = r.refs ?? ({} as any);
    setSlots({
      character: slotFromRefs(refs.character_asset_id, refs.character_upload_id),
      bg: slotFromRefs(refs.bg_asset_id, refs.bg_upload_id),
      intro: slotFromRefs(refs.intro_asset_id, refs.intro_upload_id, refs.intro_ref_mode, refs.intro_video_upload_id, refs.intro_video_url, refs.intro_card_id, refs.intro_keep_text),
      outro: slotFromRefs(refs.outro_asset_id, refs.outro_upload_id, refs.outro_ref_mode, refs.outro_video_upload_id, refs.outro_video_url, refs.outro_card_id, refs.outro_keep_text),
    });
    // Global "↻ New bg" toggle (single-scene): reflect scene 0's saved state.
    const s0 = (r.scenes ?? [])[0];
    setGlobalBgMode(s0 && s0.keyframe_url && !s0.as_is ? "new" : "keep");
    setPhase(derivePhaseFromRun(r));
    // Land at the EXACT requested sub-step on a deep-link resume ("@<step>",
    // "@rebrand_scene_1" with its scene), falling back to the furthest-
    // inferred stage when initialStep is absent/bogus so legacy links behave
    // exactly as before. Rebrand only on a backend that has it.
    setActiveStage(stageOfStep(initialStep, r.reel_ready !== undefined) ?? landingStage(r));
    setHydrating(false);
  }

  // ----- Polling -----
  const pollingRef = useRef<number | null>(null);
  useEffect(() => {
    function clear() {
      if (pollingRef.current !== null) {
        window.clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
    }
    if (!run) { clear(); return; }
    if (
      phase !== "polling_videos" &&
      phase !== "stitching" &&
      phase !== "finalizing" &&
      phase !== "polling_stills"
    ) { clear(); return; }
    clear();
    pollingRef.current = window.setInterval(async () => {
      try {
        const fresh = await api.yasProducer.talkingHeadV2.fetch(run.id);
        setRun(fresh);
        // v2 editor — stills polling: applying a new image auto-regenerates
        // the affected keyframe(s). Stay here until none are generating, then
        // drop back to idle so the new still(s) are visible in the grid.
        if (phase === "polling_stills") {
          const stillGenerating = (fresh.scenes ?? []).some(
            (s) => s.keyframe_status === "generating",
          );
          if (!stillGenerating) {
            setPhase("idle");
            setStillStartedAt(null);
            const anyFailed = (fresh.scenes ?? []).some(
              (s) => s.keyframe_status === "failed",
            );
            if (anyFailed) {
              toast.error("Still regeneration failed", STILL_REGEN_FAILED_HINT);
            } else {
              toast.info("Still updated", "The new image is ready — continue to Videos to animate it.");
            }
          }
          return;
        }
        const cs = fresh.composition_status;
        if (cs === "videos_ready") {
          setPhase("videos_ready");
          toast.info("Videos ready", "Human Element Video");
        } else if (cs === "rendering") {
          setPhase("stitching");
        } else if (cs === "ready") {
          setPhase("ready");
          // A reel finalized again whose render failed is back to ready with
          // the last good reel and the error (Final says so, with Try again).
          if (fresh.composition_error) toast.error("The new render failed", fresh.composition_error);
          else toast.info("Final composition ready", "Human Element Video");
        } else if (cs === "failed") {
          setPhase("videos_ready");
          toast.error("Composition step failed", "See the panel for details");
        }
      } catch (e: any) {
        console.warn("[talking-head] poll failed", e);
      }
    }, 5000);
    return clear;
  }, [run?.id, phase]);

  // Mirror the live run id to the URL the moment a run exists (script written,
  // or resume) so a hard refresh restores the run AND its stage — the resume
  // path (App → ResumeRouter → rehydrateFromRun → landingStage) derives the
  // stage from the run. Guarded by the parent to the active tool.
  useEffect(() => {
    if (run?.id != null) onRunId?.(run.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.id]);

  // Mirror the current sub-step (activeStage) to the URL whenever it changes and
  // a run id exists — so a hard refresh restores THIS run at its EXACT stage,
  // not just the furthest-inferred one. onStep is meaningless before a run id
  // exists (the URL needs the id), so gate on run?.id; the ProducerRoadmap guard
  // also no-ops it when this tool isn't the active one.
  // On the Rebrand stage the step names the scene too ("rebrand_scene_1").
  useEffect(() => {
    if (run?.id != null) onStep?.(activeStage === "rebrand" ? rebrandStep(rebrandTarget) : activeStage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeStage, run?.id, rebrandTarget]);

  // ----- Helpers -----
  function setSlot(key: SlotKey, next: Partial<RefSlotState>) {
    setSlots((prev) => ({ ...prev, [key]: { ...prev[key], ...next } }));
    // Auto-open Look & Feel the first time a slot gets set.
    if (next.asset || next.upload) setLookOpen(true);
  }

  function clearSlot(key: SlotKey) {
    setSlots((prev) => ({ ...prev, [key]: { ...EMPTY_SLOT } }));
  }

  // intro/outro: how a picked image is used ("as_is" keeps it as the card,
  // "new_vibe" draws a new design in its style). Updates the slot and, when a
  // run exists and the slot has a pick, saves the mode at once: once the run
  // has stills the backend makes a kept card again from the pick (free) or
  // drops a drawn one, which the card tile's "Make card" then makes in the
  // new mode (no feedback needed). Apply look sends the mode too.
  async function applyRefMode(key: "intro" | "outro", mode: TalkingHeadRefMode) {
    setSlot(key, { refMode: mode });
    const slot = slots[key];
    if (run && (slot.asset || slot.upload || slot.card)) {
      const body = key === "intro" ? { intro_ref_mode: mode } : { outro_ref_mode: mode };
      try {
        setRun(await api.yasProducer.talkingHeadV2.editRun(run.id, body));
      } catch (e: any) {
        if (handleRunGone(e)) return;
        toast.error("Couldn't save the card mode", e?.message || String(e));
      }
    }
  }

  // intro/outro "Keep card text": saved at once like the mode. Once the run
  // has stills, the backend makes the kept card again (free) or, when its
  // words must now be swapped, drops it for the card tile's "Make card".
  async function applyKeepText(key: "intro" | "outro", on: boolean) {
    setSlot(key, { keepText: on });
    const slot = slots[key];
    if (run && slotHasImage(slot)) {
      const body = key === "intro" ? { intro_keep_text: on } : { outro_keep_text: on };
      try {
        setRun(await api.yasProducer.talkingHeadV2.editRun(run.id, body));
      } catch (e: any) {
        if (handleRunGone(e)) return;
        toast.error("Couldn't save Keep card text", e?.message || String(e));
      }
    }
  }

  // Manual re-fetch — pulls the latest run state (clip statuses, video URLs,
  // composition status) and re-syncs the phase. Fixes the "stuck on
  // generating" view when the live poll dropped (e.g. after a backend
  // restart) or a clip finished while the tab was backgrounded.
  async function refreshRun() {
    if (!run || refreshing) return;
    setRefreshing(true);
    try {
      const fresh = await api.yasProducer.talkingHeadV2.fetch(run.id);
      setRun(fresh);
      const cs = fresh.composition_status;
      if (cs === "ready") setPhase("ready");
      else if (cs === "rendering") setPhase("stitching");
      else if (cs === "videos_ready" || cs === "failed") setPhase("videos_ready");
      else if (cs === "queued") setPhase("polling_videos");
      // cs null/undefined → still in stills/scripting; leave phase untouched.
    } catch (e: any) {
      if (handleRunGone(e)) return;
      toast.error("Refresh failed", e?.message || "Couldn't fetch the latest state");
    } finally {
      setRefreshing(false);
    }
  }

  async function handleUploadFile(key: SlotKey, file: File) {
    // intro/outro accept a finished VIDEO clip → upload as a direct bookend
    // (stitched in as-is, no card-gen / Veo). Everything else stays an image ref.
    const isVideo = file.type.startsWith("video/");
    if (isVideo && key !== "intro" && key !== "outro") {
      toast.error("Videos aren't supported here", "Only the intro/outro accept a video clip.");
      return;
    }
    setUploading((p) => ({ ...p, [key]: true }));
    try {
      const ref = isVideo
        ? await api.yasProducer.posterHuman.uploadClip(file)
        : await api.yasProducer.posterHuman.uploadRef(file);
      // An upload keeps the slot's mode (an uploaded card is kept as-is by
      // default, like a picked one).
      setSlots((prev) => ({
        ...prev,
        [key]: {
          asset: null, upload: ref, card: null,
          refMode: prev[key].refMode ?? DEFAULT_REF_MODE, keepText: !!prev[key].keepText,
        },
      }));
      setLookOpen(true);
    } catch (e: any) {
      toast.error("Upload failed", e?.message || String(e));
    } finally {
      setUploading((p) => ({ ...p, [key]: false }));
    }
  }

  /** A bookend slot whose upload is a finished video (used as-is). */
  function slotIsVideo(s: RefSlotState): boolean {
    return !!s.upload && (s.upload.mime_type || "").startsWith("video/");
  }

  /** A bookend slot holding an image (picked or uploaded): a card is made
   *  from it, in the slot's mode. */
  function slotHasImage(s: RefSlotState): boolean {
    return !!s.asset || !!s.card || (!!s.upload && !slotIsVideo(s));
  }

  function previewUrlForSlot(s: RefSlotState): string | null {
    if (s.upload) return s.upload.url;
    if (s.card) return s.card.url;
    if (s.asset) return api.producerAssets.thumbUrlFor(s.asset);
    return null;
  }

  function slotLabel(s: RefSlotState): string {
    if (s.upload) return s.upload.token.slice(0, 10) + "…";
    if (s.card) return pickedCardLabel(s.card);
    if (s.asset) return s.asset.display_name || s.asset.slug;
    return "";
  }

  const slotIsSet = (s: RefSlotState) => !!s.asset || !!s.upload || !!s.card;
  const hasAnyRefs =
    slotIsSet(slots.character) ||
    slotIsSet(slots.bg) ||
    slotIsSet(slots.intro) ||
    slotIsSet(slots.outro);

  // Speaker (lib/thSpeaker): who is on camera when no character is picked.
  // The field shows while it applies; lock_character goes out only with a
  // character set, so a run with none never claims one.
  const characterPicks = {
    sceneCount,
    globalCharacter: slotIsSet(slots.character),
    sceneCharacters: sceneOverrides.map((o) => !!(o.character_asset || o.character_upload)),
  };
  const speakerOn = speakerApplies(characterPicks);
  const characterSet = anyCharacterSet(characterPicks);

  const canGenerateScript = topic.trim().length >= 4 && phase === "idle";

  async function enhanceTopic() {
    const text = topic.trim();
    if (!text || enhancing) return;
    setEnhancing(true);
    try {
      const resp = await api.yasProducer.enhance(text, "topic");
      if (resp?.enhanced) setTopic(resp.enhanced);
    } catch (e: any) {
      toast.error("Enhance failed", e?.message || String(e));
    } finally {
      setEnhancing(false);
    }
  }

  // v6 — per-scene dialogue enhance. Preserves voice + tightens to
  // ~20-25 spoken words. Single-scene mode uses scene index 0.
  async function enhanceScene(idx: number) {
    const text = (sceneDialogues[idx] || "").trim();
    if (!text || enhancingSceneIdx !== null) return;
    setEnhancingSceneIdx(idx);
    try {
      const resp = await api.yasProducer.enhance(text, "dialogue");
      if (resp?.enhanced) {
        setSceneDialogues((prev) => {
          const next = [...prev];
          next[idx] = resp.enhanced;
          return next;
        });
      }
    } catch (e: any) {
      toast.error("Enhance failed", e?.message || String(e));
    } finally {
      setEnhancingSceneIdx(null);
    }
  }

  function setSceneDialogue(idx: number, text: string) {
    setSceneDialogues((prev) => {
      const next = [...prev];
      next[idx] = text;
      return next;
    });
  }

  function setSceneOverridePartial(idx: number, patch: Partial<SceneOverride>) {
    setSceneOverrides((prev) => {
      const next = [...prev];
      next[idx] = { ...next[idx], ...patch };
      return next;
    });
  }

  async function handleSceneUpload(
    idx: number,
    kind: "character" | "bg",
    file: File,
  ) {
    setSceneUploading({ idx, kind });
    try {
      const ref = await api.yasProducer.posterHuman.uploadRef(file);
      setSceneOverridePartial(idx, {
        ...(kind === "character"
          ? { character_asset: null, character_upload: ref }
          : { bg_asset: null, bg_upload: ref }),
      });
    } catch (e: any) {
      toast.error("Upload failed", e?.message || String(e));
    } finally {
      setSceneUploading(null);
    }
  }

  // Topic → Look: write the script (NO refs yet — those are picked on the Look
  // page) so the Look page shows the script + the bg toggle (run exists). If a
  // run already exists (came back to Topic), re-script it in place instead of
  // creating a new one. Refs only lock the image later, never the words.
  async function startScript() {
    if (!canGenerateScript || genGuardRef.current) return;
    genGuardRef.current = true;
    const userDialogues: (string | null)[] = sceneDialogues
      .slice(0, sceneCount)
      .map((d) => (d?.trim() ? d.trim() : null));
    setPhase("scripting");
    try {
      let fresh: TalkingHeadRun;
      if (run) {
        // Existing run — update topic/dialogue/scene_count in place (rescript).
        fresh = await api.yasProducer.talkingHeadV2.editRun(run.id, {
          topic: topic.trim(),
          scene_count: sceneCount,
          scene_dialogues: userDialogues,
        });
      } else {
        fresh = await api.yasProducer.talkingHeadV2.generateScript({
          topic: topic.trim(),
          user_dialogue: userDialogues[0],
          scene_count: sceneCount,
          user_dialogues: userDialogues,
          ...lockCharacterField(characterSet, lockCharacter),
          generate_intro: generateIntro,
          generate_outro: generateOutro,
          aspect_ratio: videoAspect,
          video_quality: videoQuality,
          framing,
        });
      }
      setRun(fresh);
      setActiveStage("look");
      setPhase("idle");
      // Stop a carried-over click from immediately firing "Generate stills"
      // (same button position as "Next").
      armPrimaryCooldown();
    } catch (e: any) {
      setPhase("idle");
      toast.error("Couldn't write the script", e?.message || String(e));
    } finally {
      genGuardRef.current = false;
    }
  }

  // Look → Stills: persist the picked look + generate the stills. The look is
  // sent to generate-previews (patched server-side, no regen); `bg_mode` drives
  // "use my uploaded photo as-is" (uploaded character + no bg + keep → no gen).
  async function generateStills() {
    if (!run || genGuardRef.current) return;
    genGuardRef.current = true;
    const scene_overrides = sceneOverrides.slice(0, sceneCount).map((o) =>
      isSceneOverridden(o) || o.bg_mode === "new"
        ? {
            character_asset_id: o.character_asset?.id ?? null,
            character_upload_id: o.character_upload?.id ?? null,
            bg_asset_id: o.bg_asset?.id ?? null,
            bg_upload_id: o.bg_upload?.id ?? null,
            bg_mode: o.bg_mode ?? "keep",
          }
        : null,
    );
    const globalBg =
      !slotIsSet(slots.bg) && globalBgMode === "new" ? "new" : "keep";
    setActiveStage("stills");
    setPhase("previewing");
    try {
      // An edited Speaker is saved first: the stills are made with it (no
      // stills exist yet, so the save re-shoots nothing).
      const speaker = speakerPatch(speakerDraft, run, speakerOn);
      if (speaker.speaker_subject) {
        const saved = await api.yasProducer.talkingHeadV2.editRun(run.id, speaker);
        setRun(saved);
        // The field shows what was saved (a chip becomes the speaker itself).
        setSpeakerDraft(speakerDraftFromRun(saved));
      }
      const withPreviews = await api.yasProducer.talkingHeadV2.generatePreviews(run.id, {
        character_asset_id: slots.character.asset?.id ?? null,
        character_upload_id: slots.character.upload?.id ?? null,
        bg_asset_id: slots.bg.asset?.id ?? null,
        bg_upload_id: slots.bg.upload?.id ?? null,
        intro_asset_id: slots.intro.asset?.id ?? null,
        intro_upload_id: slotIsVideo(slots.intro) ? null : (slots.intro.upload?.id ?? null),
        outro_asset_id: slots.outro.asset?.id ?? null,
        outro_upload_id: slotIsVideo(slots.outro) ? null : (slots.outro.upload?.id ?? null),
        // Direct-video bookend → its own field (used as-is, no card-gen / Veo).
        intro_video_upload_id: slotIsVideo(slots.intro) ? slots.intro.upload!.id : null,
        outro_video_upload_id: slotIsVideo(slots.outro) ? slots.outro.upload!.id : null,
        intro_ref_mode: slots.intro.refMode ?? DEFAULT_REF_MODE,
        outro_ref_mode: slots.outro.refMode ?? DEFAULT_REF_MODE,
        // A previous video's card, and whether a kept card keeps its words.
        intro_card_id: slots.intro.card?.card_id ?? null,
        outro_card_id: slots.outro.card?.card_id ?? null,
        intro_keep_text: !!slots.intro.keepText,
        outro_keep_text: !!slots.outro.keepText,
        scene_overrides,
        ...lockCharacterField(characterSet, lockCharacter),
        bg_mode: globalBg,
        aspect_ratio: videoAspect,
        video_quality: videoQuality,
        framing,
      });
      setRun(withPreviews);
      // NOTE: do NOT reset the "↻ New bg" toggles here — they're a persistent
      // per-scene setting now (saved via `as_is`), so they must keep reflecting
      // what was generated (on = generated bg, off = character image as-is).
    } catch (e: any) {
      if (handleRunGone(e)) return;
      toast.error("Preview generation failed", e?.message || String(e));
    } finally {
      genGuardRef.current = false;
    }
    setPhase("idle");
  }

  // The intro / outro card tiles (main and scene stills use stillTileAction).
  // Text is optional for a card: without it the card is made again from the
  // picked image and mode, which is how a changed mode or a dropped card is
  // applied; with it, the note steers a drawn card.
  async function retryTile(which: "main" | "intro" | "outro" | `scene_${number}`) {
    // Nothing else may be running: its finally sets the phase back to idle,
    // which would stop a poll another step started.
    if (!run || inputsDisabled) return;
    let text: string;
    let sceneIdx: number | null = null;
    if (which.startsWith("scene_")) {
      sceneIdx = parseInt(which.split("_")[1], 10);
      text = (sceneFeedback[sceneIdx] || "").trim();
    } else {
      text = feedback[which as "main" | "intro" | "outro"].trim();
    }
    if (!text && (which === "main" || sceneIdx !== null)) {
      toast.info(
        "Add feedback first",
        `Tell us what to change in the ${
          sceneIdx !== null ? `scene ${sceneIdx + 1}` : which
        } tile.`,
      );
      return;
    }
    if (sceneIdx !== null) {
      setRetryingSceneIdx(sceneIdx);
      setPhase("retrying_scene");
    } else {
      setPhase(
        which === "main" ? "retrying_main"
          : which === "intro" ? "retrying_intro"
            : "retrying_outro",
      );
    }
    try {
      const fresh = await api.yasProducer.talkingHeadV2.retryPreview(run.id, {
        which, feedback_text: text,
      });
      setRun(fresh);
      if (sceneIdx !== null) {
        setSceneFeedback((prev) => ({ ...prev, [sceneIdx as number]: "" }));
        toast.info(`Scene ${sceneIdx + 1} regenerated`, "Human Element Video");
      } else {
        setFeedback((prev) => ({
          ...prev,
          [which as "main" | "intro" | "outro"]: "",
        }));
        toast.info(`${cap(which as string)} regenerated`, "Human Element Video");
      }
    } catch (e: any) {
      if (handleRunGone(e)) return;
      toast.error("Retry failed", e?.message || String(e));
    } finally {
      setRetryingSceneIdx(null);
      setPhase("idle");
    }
  }

  // The intro / outro cards the reel wants and does not have (dropped by an
  // edit of the pick, mode or words, never made, or made from another image):
  // made together, each from its picked image and mode (a kept card is a
  // free copy). Returns the run as it is after.
  async function makeCards(base: TalkingHeadRun, roles: BookendRole[]): Promise<TalkingHeadRun> {
    if (roles.length === 0) return base;
    setMakingCards(roles);
    let latest = base;
    try {
      const results = await Promise.allSettled(
        roles.map((which) =>
          api.yasProducer.talkingHeadV2.retryPreview(base.id, { which, feedback_text: "" }),
        ),
      );
      latest = await api.yasProducer.talkingHeadV2.fetch(base.id);
      setRun(latest);
      const failed = roles.filter((_, i) => results[i].status === "rejected");
      if (failed.length) {
        const why = results.find((r) => r.status === "rejected") as PromiseRejectedResult | undefined;
        toast.error(`Couldn't make ${cardsPhrase(failed)}`, why?.reason?.message || "Try again from its tile.");
      } else {
        toast.info(`Made ${cardsPhrase(roles)}`, "Human Element Video");
      }
    } catch (e: any) {
      if (handleRunGone(e)) return latest;
      toast.error(`Couldn't make ${cardsPhrase(roles)}`, e?.message || String(e));
    } finally {
      setMakingCards([]);
    }
    return latest;
  }

  // Main / scene stills: Refine makes the ONE change typed in the tile on
  // the current still, Regenerate makes the still again the way first
  // generation did (no text; a failed still too), Undo puts back the version
  // before the last change. Each is a new version on the server, so a bad
  // result is never the end of a still.
  async function stillTileAction(which: "main" | `scene_${number}`, action: StillAction) {
    // Nothing else may be running (a re-shoot or the videos being polled,
    // another tile's change): its finally sets the phase back to idle.
    if (!run || inputsDisabled) return;
    const sceneIdx = which.startsWith("scene_") ? parseInt(which.split("_")[1], 10) : null;
    const label = sceneIdx !== null ? `Scene ${sceneIdx + 1}` : "Main";
    let change = "";
    if (action === "refine") {
      change = (sceneIdx !== null ? sceneFeedback[sceneIdx] || "" : feedback.main).trim();
      if (!change) {
        toast.info("Describe one change first", `Type what to change in the ${label.toLowerCase()} still.`);
        return;
      }
    }
    if (sceneIdx !== null) {
      setRetryingSceneIdx(sceneIdx);
      setPhase("retrying_scene");
    } else {
      setPhase("retrying_main");
    }
    setStillAction(action);
    setStillStartedAt(Date.now());
    setClock(Date.now());
    try {
      const fresh =
        action === "refine"
          ? await api.yasProducer.talkingHeadV2.refineStill(run.id, which, change)
          : action === "regenerate"
            ? await api.yasProducer.talkingHeadV2.regenerateStill(run.id, which)
            : await api.yasProducer.talkingHeadV2.undoStill(run.id, which);
      setRun(fresh);
      // Undoing a framing re-shoot puts the run's framing back too.
      if (action === "undo" && framingFromRun(fresh) !== framingFromRun(run)) setFraming(framingFromRun(fresh));
      if (action === "refine") {
        if (sceneIdx !== null) setSceneFeedback((prev) => ({ ...prev, [sceneIdx]: "" }));
        else setFeedback((prev) => ({ ...prev, main: "" }));
      }
      toast.info(stillDoneToast(action, label), "Human Element Video");
    } catch (e: any) {
      if (handleRunGone(e)) return;
      // The server's own words ("The image model didn't answer in time...").
      toast.error(stillFailTitle(action), serverWords(e));
    } finally {
      setRetryingSceneIdx(null);
      setStillAction(null);
      setStillStartedAt(null);
      setPhase("idle");
    }
  }

  // A still tile's framing buttons: that one still is re-framed on the
  // server, in the background like an applied look, so the tile shows it
  // being made: a closer framing is a free crop of the still, a wider one a
  // re-shoot by the image model (the still itself is edited, so the same
  // person, outfit and room come back). A single scene's framing is the run's.
  async function setStillFraming(sceneIdx: number, value: TalkingHeadFraming) {
    if (!run || applying) return;
    setApplying(true);
    try {
      const before = stillFraming(run.scenes?.[sceneIdx]).current;
      const fresh = await api.yasProducer.talkingHeadV2.editRun(
        run.id, sceneFramingPatch(run, sceneIdx, value),
      );
      setRun(fresh);
      if ((fresh.scene_count ?? 1) <= 1) setFraming(framingFromRun(fresh));
      if ((fresh.scenes ?? []).some((s) => s.keyframe_status === "generating")) {
        setPhase("polling_stills");
        setStillStartedAt(Date.now());
        setClock(Date.now());
        const work = fresh.scenes?.[sceneIdx]?.keyframe_work ?? framingMove(before, value);
        toast.info(...framingChangeToast(work, value, stillImageCost(fresh.still_image_usd)));
      }
    } catch (e: any) {
      if (handleRunGone(e)) return;
      toast.error("Couldn't change the framing", e?.message || String(e));
    } finally {
      setApplying(false);
    }
  }

  // Re-entrancy guard: synchronously block a second startScript/generateStills
  // before React state (phase) commits — stops a rapid double-click or a
  // ⌘↵-plus-click from firing generation twice / creating a duplicate run.
  const genGuardRef = useRef(false);
  // Brief cooldown after Topic→Look navigation: the primary button changes from
  // "Next" to "Generate stills" in the SAME spot, so a carried-over/reflexive
  // click could otherwise immediately fire generation. Disable it for a beat.
  const [primaryCooldown, setPrimaryCooldown] = useState(false);
  const cooldownTimerRef = useRef<number | null>(null);
  function armPrimaryCooldown(ms = 600) {
    setPrimaryCooldown(true);
    if (cooldownTimerRef.current !== null) window.clearTimeout(cooldownTimerRef.current);
    cooldownTimerRef.current = window.setTimeout(() => setPrimaryCooldown(false), ms);
  }

  // Make (or make again) the clips that need it: paid Veo, so ONLY the
  // Videos button ("Make videos" / "Update N clips", with its cost) calls
  // this, never an effect, a step click or a mount (run 830 got two unasked
  // rounds from the old enter-Videos auto-fire). The backend refuses a call
  // without confirm. scripts/check-th-videos.mjs holds the creator to it.
  async function makeVideos() {
    if (!run || genGuardRef.current) return;
    genGuardRef.current = true;
    setPhase("finalizing");
    setActiveStage("videos");
    try {
      const fresh = await api.yasProducer.talkingHeadV2.finalize(run.id, { confirm: true });
      setRun(fresh);
      // Resolve the phase from the RETURNED status immediately instead of
      // always landing on "polling_videos". When the backend kicked Veo this
      // is "queued"→polling; when nothing needed regen it's already
      // "videos_ready", so we don't sit on a fake 5s "loading" stall.
      setPhase(derivePhaseFromRun(fresh));
    } catch (e: any) {
      setPhase("idle");
      if (handleRunGone(e)) return;
      toast.error("Could not start videos", serverWords(e));
    } finally {
      genGuardRef.current = false;
    }
  }

  async function retryVideoTile(
    which: "main" | "intro" | "outro" | `scene_${number}`,
    // Set when the tile's <video> failed to LOAD (e.g. the file is gone from
    // disk and bucket). The run still reports status='ready', so we skip the
    // "add feedback first" gate and re-run the generation as-is.
    forceNoFeedback = false,
  ) {
    if (!run) return;
    let text: string;
    let sceneIdx: number | null = null;
    if (which.startsWith("scene_")) {
      sceneIdx = parseInt(which.split("_")[1], 10);
      text = (sceneVideoFeedback[sceneIdx] || "").trim();
    } else {
      text = videoFeedback[which as "main" | "intro" | "outro"].trim();
    }
    // Inspect the targeted tile's current status. On `failed` we allow
    // empty feedback (the user is recovering from a transient Veo error
    // like "service experiencing high load" — there's nothing to refine,
    // just re-run as-is). On `ready` we still require feedback because
    // that path is creative iteration.
    const tileStatus: string | undefined =
      sceneIdx !== null
        ? run.scenes?.[sceneIdx]?.animation?.status
        : which === "main"
          ? run.animations?.main?.status
          : which === "intro"
            ? run.animations?.intro?.status
            : run.animations?.outro?.status;
    if (!text && tileStatus !== "failed" && !forceNoFeedback) {
      toast.info(
        "Add feedback first",
        `Tell us what to change in the ${
          sceneIdx !== null ? `scene ${sceneIdx + 1}` : which
        } video.`,
      );
      return;
    }
    if (sceneIdx !== null) {
      setRetryingSceneIdx(sceneIdx);
      setPhase("retrying_scene_video");
    } else {
      setPhase(
        which === "main" ? "retrying_video_main"
          : which === "intro" ? "retrying_video_intro"
            : "retrying_video_outro",
      );
    }
    try {
      const fresh = await api.yasProducer.talkingHeadV2.retryVideo(run.id, {
        which, feedback_text: text,
      });
      setRun(fresh);
      if (sceneIdx !== null) {
        setSceneVideoFeedback((prev) => ({ ...prev, [sceneIdx as number]: "" }));
        toast.info(`Scene ${sceneIdx + 1} video retry kicked`, "Human Element Video");
      } else {
        setVideoFeedback((prev) => ({
          ...prev,
          [which as "main" | "intro" | "outro"]: "",
        }));
        toast.info(`${cap(which as string)} video retry kicked`, "Human Element Video");
      }
      setPhase("polling_videos");
    } catch (e: any) {
      setPhase("videos_ready");
      if (handleRunGone(e)) return;
      toast.error("Video retry failed", e?.message || String(e));
    } finally {
      setRetryingSceneIdx(null);
    }
  }

  // Finalize: freeze the reel as this page last read it (`expectSha`: the
  // reel block's inputs_sha — a reel changed elsewhere since is a 409) and
  // go to Final. A refusal (a scene's clip missing, a version still being
  // made…) says so in the server's words and stays where it was, and the
  // run is read again: the reel this page held may be what was out of date,
  // and the next press must not be refused for it too.
  async function finalizeReel(expectSha: string | null = null) {
    if (!run) return;
    const from = activeStage;
    setSendingReel(true);
    setActiveStage("final");
    try {
      const fresh = await api.yasProducer.talkingHeadV2.stitch(run.id, { expect_sha: expectSha });
      setRun(fresh);
      setPhase(derivePhaseFromRun(fresh));
    } catch (e: any) {
      if (handleRunGone(e)) return;
      setActiveStage(from);
      toast.error("Couldn't finalize", serverWords(e));
      if (/^409\b/.test(String(e?.message ?? ""))) {
        api.yasProducer.talkingHeadV2.fetch(run.id).then(setRun).catch(() => {
          // the next poll or press reads it again
        });
      }
    } finally {
      setSendingReel(false);
    }
  }

  async function saveToAssets() {
    if (!run) return;
    setPhase("saving");
    try {
      await api.yasProducer.talkingHeadV2.saveToAssets(run.id, {
        display_name: topic.trim().slice(0, 80) || undefined,
      });
      setPhase("saved");
      toast.info("Saved to assets", "Human Element Video");
      assetsStore.invalidate();
      assetsStore.fetch({ limit: 500 });
    } catch (e: any) {
      setPhase("ready");
      if (handleRunGone(e)) return;
      toast.error("Save failed", e?.message || String(e));
    }
  }

  // Open this reel in the multi-track Video Editor as a NEW project.
  //   mode="final" → the stitched composition as one clip (Final stage)
  //   mode="clips" → the individual ready clips in order (Videos stage)
  const [openingEditor, setOpeningEditor] = useState(false);
  async function openInEditor(mode: "clips" | "final") {
    if (!run || openingEditor) return;
    setOpeningEditor(true);
    try {
      const { project_id } = await api.yasProducer.talkingHeadV2.openInEditor(run.id, mode);
      toast.info("Opening editor", "Your video editor project is ready");
      // The Library cache predates this brand-new project; invalidate so the
      // ResumeRouter resolves its type fresh instead of 404ing on a stale list.
      useYasLibraryStore.getState().invalidate();
      if (onOpenInEditor) {
        // Inside the roadmap: hand the project to the Editing stage IN PLACE
        // (keeps the global stepper; no full remount / deep-link).
        onOpenInEditor(project_id);
      } else if (navigate) {
        // Standalone resume: deep-link. openInEditor creates a Video Editor
        // project, so resume must carry that type (ids collide across the
        // poster/animation/DB id-spaces — see parseProducerResume).
        navigate("producer", `video_editor:${project_id}`);
      } else {
        window.location.hash = `producer/video_editor:${project_id}`;
      }
    } catch (e: any) {
      if (handleRunGone(e)) return;
      toast.error("Couldn't open the editor", e?.message || String(e));
    } finally {
      setOpeningEditor(false);
    }
  }

  // ----- v2 editor handlers -----
  async function applyEdits() {
    if (!run || applying) return;
    // The button said "Apply look and make cards" (with its cost) only when
    // a card was already missing: only then does this press make cards.
    const cardsPromised = cardsToMake.length > 0;
    setApplying(true);
    try {
      const userDialogues = sceneDialogues
        .slice(0, sceneCount)
        .map((d) => (d?.trim() ? d.trim() : null));
      // Include a scene's override when it has a ref OR a one-shot "New bg"
      // intent (the toggle alone, with no ref, must still reach the backend).
      const scene_overrides = sceneOverrides.slice(0, sceneCount).map((o) =>
        isSceneOverridden(o) || o.bg_mode === "new"
          ? {
              character_asset_id: o.character_asset?.id ?? null,
              character_upload_id: o.character_upload?.id ?? null,
              bg_asset_id: o.bg_asset?.id ?? null,
              bg_upload_id: o.bg_upload?.id ?? null,
              bg_mode: o.bg_mode ?? "keep",
            }
          : null,
      );
      // Global "New bg" only applies when the global bg slot is empty.
      const globalBg =
        !slotIsSet(slots.bg) && globalBgMode === "new" ? "new" : "keep";
      const speaker = speakerPatch(speakerDraft, run, speakerOn);
      const fresh = await api.yasProducer.talkingHeadV2.editRun(run.id, {
        topic: topic.trim(),
        scene_count: sceneCount,
        ...lockCharacterField(characterSet, lockCharacter),
        // An edited Speaker: the scenes showing it are re-shot.
        ...speaker,
        scene_dialogues: userDialogues,
        scene_overrides,
        bg_mode: globalBg,
        character_asset_id: slots.character.asset?.id ?? null,
        character_upload_id: slots.character.upload?.id ?? null,
        bg_asset_id: slots.bg.asset?.id ?? null,
        bg_upload_id: slots.bg.upload?.id ?? null,
        intro_asset_id: slots.intro.asset?.id ?? null,
        intro_upload_id: slotIsVideo(slots.intro) ? null : (slots.intro.upload?.id ?? null),
        outro_asset_id: slots.outro.asset?.id ?? null,
        outro_upload_id: slotIsVideo(slots.outro) ? null : (slots.outro.upload?.id ?? null),
        // Direct-video bookend → its own field (used as-is, no card-gen / Veo).
        intro_video_upload_id: slotIsVideo(slots.intro) ? slots.intro.upload!.id : null,
        outro_video_upload_id: slotIsVideo(slots.outro) ? slots.outro.upload!.id : null,
        // A previous video's card picked as the intro/outro (null clears it).
        intro_card_id: slots.intro.card?.card_id ?? null,
        outro_card_id: slots.outro.card?.card_id ?? null,
        // How each picked intro/outro image is used (kept as-is by default)
        // and whether a kept card keeps its own words; only sent for a slot
        // holding an image, where they mean something.
        ...(slotHasImage(slots.intro)
          ? { intro_ref_mode: slots.intro.refMode ?? DEFAULT_REF_MODE, intro_keep_text: !!slots.intro.keepText }
          : {}),
        ...(slotHasImage(slots.outro)
          ? { outro_ref_mode: slots.outro.refMode ?? DEFAULT_REF_MODE, outro_keep_text: !!slots.outro.keepText }
          : {}),
        aspect_ratio: videoAspect,
        video_quality: videoQuality,
        // The run's framing: the stills that follow it and show another
        // framing are re-shot at it (never one that is the character photo).
        framing,
      });
      setRun(fresh);
      // The Speaker field shows what was saved: a chip that kept the same
      // speaker changes nothing the field follows, so it is set here.
      if (speaker.speaker_subject) setSpeakerDraft(speakerDraftFromRun(fresh));
      // On the Look step, when the button offered it ("Apply look and make
      // cards", with its cost), a card the edit dropped (a new pick in "New
      // design" mode, new words on a drawn card) or never made is made now:
      // the reel never animates a card made from another image. A plain
      // "Apply look" makes no paid card: the Stills step offers it with its
      // cost. (Update script on the Topic step leaves it to the Stills step.)
      const toMake = activeStage === "look" && cardsPromised && runIsBeingEdited(fresh) ? missingCards(fresh) : [];
      if (toMake.length) {
        goToStage("stills");
        await makeCards(fresh, toMake);
      }
      // NOTE: the "↻ New bg" toggles are persistent (saved via `as_is`) — don't
      // reset them. edit_run's guard prevents re-rolling an already-generated
      // background, so a no-op Apply won't regenerate anything.
      // Surface what changed + advance to the next stage. Previously this only
      // showed a toast and stayed put, so "Apply look" felt like it did nothing.
      const staleCount =
        (fresh.scenes ?? []).filter((s) => !s.excluded && s.video_stale).length +
        (!fresh.intro_excluded && fresh.intro_video_stale ? 1 : 0) +
        (!fresh.outro_excluded && fresh.outro_video_stale ? 1 : 0) +
        ((fresh.scene_count ?? 1) <= 1 && fresh.main_video_stale ? 1 : 0);
      const freshHasKeyframe =
        !!fresh.main?.url || (fresh.scenes ?? []).some((s) => !!s.keyframe_url);
      // v2 editor — applying a new image AUTO-regenerates the affected still(s)
      // server-side. If any keyframe is now generating, jump to Stills and poll
      // so the user watches the new image come in (no manual retry).
      const regenerating = (fresh.scenes ?? []).some(
        (s) => s.keyframe_status === "generating",
      );
      if (regenerating) {
        goToStage("stills");
        setPhase("polling_stills");
        setStillStartedAt(Date.now());
        setClock(Date.now());
        // What the background work is: a free crop, a re-shoot, a new still.
        toast.info(...stillWorkToast(
          (fresh.scenes ?? []).filter((s) => s.keyframe_status === "generating").map((s) => s.keyframe_work),
          stillImageCost(fresh.still_image_usd),
        ));
      } else if (staleCount > 0) {
        toast.info(
          "Look applied",
          `${staleCount} clip${staleCount === 1 ? "" : "s"} now out of date — regenerate ${staleCount === 1 ? "it" : "them"} in Stills/Videos.`,
        );
        if (freshHasKeyframe) goToStage("stills");
      } else {
        toast.info("Changes applied", "Nothing needed regenerating.");
        if (freshHasKeyframe) goToStage("stills");
      }
    } catch (e: any) {
      if (handleRunGone(e)) return;
      toast.error("Couldn't apply changes", e?.message || String(e));
    } finally {
      setApplying(false);
    }
  }

  async function toggleExclude(which: string, excluded: boolean) {
    if (!run) return;
    try {
      const fresh = await api.yasProducer.talkingHeadV2.setExcluded(run.id, {
        which: which as any,
        excluded,
      });
      setRun(fresh);
    } catch (e: any) {
      if (handleRunGone(e)) return;
      toast.error("Couldn't update clip", e?.message || String(e));
    }
  }

  // Per-still Veo tier (Stills stage). Optimistic local flip, then persist;
  // on failure refetch the run to resync.
  async function setSegmentQuality(which: string, quality: "normal" | "quality") {
    if (!run) return;
    setRun((prev) => {
      if (!prev) return prev;
      if (which === "intro") return { ...prev, intro_video_quality: quality };
      if (which === "outro") return { ...prev, outro_video_quality: quality };
      if (which === "main") return { ...prev, main_video_quality: quality };
      if (which.startsWith("scene_")) {
        const idx = Number(which.split("_")[1]);
        const scenes = [...(prev.scenes || [])];
        if (scenes[idx]) scenes[idx] = { ...scenes[idx], video_quality: quality };
        return { ...prev, scenes };
      }
      return prev;
    });
    try {
      const fresh = await api.yasProducer.talkingHeadV2.setSegmentQuality(run.id, {
        which,
        quality,
      });
      setRun(fresh);
    } catch (e: any) {
      if (handleRunGone(e)) return;
      toast.error("Couldn't update video quality", e?.message || String(e));
      try { setRun(await api.yasProducer.talkingHeadV2.fetch(run.id)); } catch {}
    }
  }

  // Look-stage "Generate intro/outro" switch. In create mode it just sets local
  // state (seeded into the generate-script payload). In edit mode it flips the
  // run's `*_excluded` flag (same unified switch as the Videos ✕). Re-enabling
  // a bookend that has no card makes nothing by itself: its card is made by
  // the button that offers it with its cost ("Apply look and make cards" on
  // Look, "Make the intro card" on Stills), never by a switch.
  async function toggleGenerateBookend(which: "intro" | "outro", on: boolean) {
    if (which === "intro") setGenerateIntro(on);
    else setGenerateOutro(on);
    if (!run) return; // create mode — applied at generate-script time
    try {
      const fresh = await api.yasProducer.talkingHeadV2.setExcluded(run.id, {
        which,
        excluded: !on,
      });
      setRun(fresh);
      const card = which === "intro" ? fresh.intro : fresh.outro;
      if (on && !card && runIsBeingEdited(fresh)) {
        toast.info(`Make the ${which} card`, "Its button shows what it costs; nothing is made until you press it.");
      }
    } catch (e: any) {
      // revert the optimistic local toggle on failure
      if (which === "intro") setGenerateIntro(!on);
      else setGenerateOutro(!on);
      if (handleRunGone(e)) return;
      toast.error("Couldn't update bookend", e?.message || String(e));
    }
  }

  async function restoreArchived(archivedIndex: number) {
    if (!run) return;
    try {
      const fresh = await api.yasProducer.talkingHeadV2.restoreScene(run.id, archivedIndex);
      setRun(fresh);
      setSceneCount(Math.max(1, fresh.scene_count ?? 1));
      toast.info("Scene restored", "Human Element Video");
    } catch (e: any) {
      if (handleRunGone(e)) return;
      toast.error("Couldn't restore scene", e?.message || String(e));
    }
  }

  async function batchRegenerate(target: "images" | "videos" | "both") {
    if (!run || selectedScenes.size === 0 || applying) return;
    const items = Array.from(selectedScenes).map((i) => ({
      which: `scene_${i}` as const,
    }));
    setApplying(true);
    try {
      const fresh = await api.yasProducer.talkingHeadV2.batchRegen(run.id, {
        items,
        target,
      });
      setRun(fresh);
      setSelectedScenes(new Set());
      setBatchMode(false);
      if (target !== "images") setPhase("polling_videos");
      toast.info("Batch regeneration started", "Human Element Video");
    } catch (e: any) {
      if (handleRunGone(e)) return;
      toast.error("Batch regen failed", e?.message || String(e));
    } finally {
      setApplying(false);
    }
  }

  function resetForNew() {
    setRun(null);
    setTopic("");
    setSceneCount(1);
    setSceneDialogues(Array.from({ length: MAX_SCENES }, () => ""));
    setSceneOverrides(
      Array.from({ length: MAX_SCENES }, () => ({ ...EMPTY_SCENE_OVERRIDE })),
    );
    setSlots({
      character: { ...EMPTY_SLOT },
      bg: { ...EMPTY_SLOT },
      intro: { ...EMPTY_SLOT },
      outro: { ...EMPTY_SLOT },
    });
    setFeedback({ main: "", intro: "", outro: "" });
    setVideoFeedback({ main: "", intro: "", outro: "" });
    setFraming(DEFAULT_FRAMING);
    setLookOpen(false);
    setEnginesOpen(false);
    setScriptDrawerOpen(false);
    setBatchMode(false);
    setSelectedScenes(new Set());
    setNotFound(false);
    setActiveStage("topic");
    setPhase("idle");
    setStillStartedAt(null);
  }

  // ----- Derived -----
  const main = run?.main;
  const intro = run?.intro;
  const outro = run?.outro;
  const animations = run?.animations;
  const compositionUrl = run?.composition_url;
  const compositionStatus = run?.composition_status;
  // v6 — scene count + scenes array. Always derived from the run; never
  // less than 1 (even if a legacy payload is missing the field).
  const runSceneCount = Math.max(1, run?.scene_count ?? 1);
  const runScenes = run?.scenes ?? [];
  // The videos wait for scene 0's still AND every intro/outro card the reel
  // wants: the pick shown while no card exists, or a card made from another
  // image (run 824), is not a card (lib/thBookends).
  const previewsReady = runPreviewsReady(run);
  const cardsToMake = missingCards(run);
  // ----- Derived gating + stage status -----
  // "Editing an already-generated run" = generate-previews ran: scene 0's
  // still or a card exists. NOTE: as of the Topic→Look script flow, `run` is
  // set as soon as the script is written (no stills yet), so this must not be
  // `run !== null`; nor any keyframe, or a later scene's keyframe made by a
  // scene count change turned "Generate stills" into "Apply look" and the
  // cards were never made (run 827).
  const editingExisting = runIsBeingEdited(run);
  const hasRun = run !== null;
  const _busyPhases: Phase[] = [
    "scripting", "previewing", "polling_stills", "finalizing", "polling_videos",
    "stitching",
    "retrying_main", "retrying_intro", "retrying_outro",
    "retrying_video_main", "retrying_video_intro", "retrying_video_outro",
    "retrying_scene", "retrying_scene_video", "saving",
  ];
  const busy = _busyPhases.includes(phase) || applying || sendingReel || makingCards.length > 0;
  // The reel is being made: rendering, or Finalize sent and not answered.
  const stitching = phase === "stitching" || sendingReel;
  // Editing is inherent per stage now (no editMode toggle): inputs are live
  // whenever their stage is open and nothing blocking is running.
  const inputsDisabled = busy || hydrating;
  // Look step: the Speaker field, while no character is picked (needs the
  // run: the speaker is written with the script).
  const speakerField = speakerOn && run ? (
    <TalkingHeadV2SpeakerField
      run={run}
      value={speakerDraft}
      onChange={setSpeakerDraft}
      disabled={inputsDisabled}
    />
  ) : null;
  // Any INCLUDED clip out of date → block stitch (mirrors the backend gate).
  const hasStaleIncluded =
    !!run &&
    ((run.scenes ?? []).some((s) => !s.excluded && s.video_stale) ||
      (!run.intro_excluded && !!run.intro_video_stale) ||
      (!run.outro_excluded && !!run.outro_video_stale) ||
      (runSceneCount <= 1 && !!run.main_video_stale));

  // Reachability + per-stage status dots for the StepperNav.
  const hasKeyframe = !!main?.url || runScenes.some((s) => !!s.keyframe_url);
  // Video size is fixed once a still or card exists (the backend refuses a
  // new size): every still is made in the run's shape and Veo animates it.
  const sizeLocked = videoSizeLocked(run);
  const hasAnyClip =
    !!animations?.main || !!animations?.intro || !!animations?.outro ||
    runScenes.some((s) => !!s.animation);
  const hasComposition = !!compositionStatus;
  // A backend with the Rebrand stage sends reel_ready (an older one: none of
  // it, and the flow stays Videos → Stitch → Final).
  const hasRebrand = run?.reel_ready !== undefined;
  // A reel to show on Final: a served file, or one being made. (A status
  // alone is no reel: `queued` is set while clips are still being made.)
  const hasReel = !!compositionUrl || compositionStatus === "rendering";
  const reelMaking = (run?.reel?.targets ?? []).some((t) => t.making != null);
  const _genStills =
    phase === "previewing" || phase === "retrying_main" ||
    phase === "retrying_intro" || phase === "retrying_outro" ||
    phase === "retrying_scene" || phase === "polling_stills" ||
    makingCards.length > 0;
  const _genVideos =
    phase === "finalizing" || phase === "polling_videos" ||
    phase === "retrying_video_main" || phase === "retrying_video_intro" ||
    phase === "retrying_video_outro" || phase === "retrying_scene_video";
  const stageStatus: Record<StudioStage, StageStatus> = {
    topic: { reachable: true, done: editingExisting, generating: phase === "scripting", stale: false },
    look: { reachable: true, done: hasAnyRefs || editingExisting, generating: false, stale: false },
    stills: { reachable: editingExisting, done: hasKeyframe, generating: _genStills, stale: false },
    videos: {
      reachable: editingExisting && (hasKeyframe || hasAnyClip || hasComposition),
      done: hasAnyClip, generating: _genVideos, stale: hasStaleIncluded,
    },
    rebrand: {
      reachable: editingExisting && !!run?.reel_ready,
      done: hasReel && !run?.composition_stale,
      generating: reelMaking,
      stale: !!run?.composition_stale,
      hidden: !hasRebrand,
    },
    final: {
      reachable: hasRebrand ? hasReel : editingExisting && (hasAnyClip || hasComposition),
      done: compositionStatus === "ready",
      generating: stitching || compositionStatus === "rendering",
      stale: !!run?.composition_stale,
    },
  };

  // Entering the Videos stage (its step, a "@videos" link, the Rebrand
  // stage's way back, a refresh) only SHOWS the clips. It used to make the
  // missing or out-of-date ones by itself: run 830 got $3.60 of Veo from a
  // page opened on Videos and $1.40 more from a click on the out-of-date
  // Videos step after an Undo. Clips are made by the Videos button alone
  // (videosButton, below), which shows what it costs.
  const videosButton = videosAction(run, { busy, previewsReady });

  // Per-stage primary action — the contextual button at the bottom of the
  // active stage panel (also fired by ⌘↵). Recomputed every render (closures
  // must stay fresh — see note below).
  type Primary = {
    label: string;
    onClick: () => void;
    disabled: boolean;
    busy: boolean;
    cost: string | null;
    hint?: string;
    /** false: ⌘/Ctrl+Enter is not this action's (the Rebrand stage, where it
     *  is Generate's; an edit of a run with stills, which may re-shoot them,
     *  a paid image edit its button cannot price). */
    shortcut?: boolean;
  };
  const primary: Primary = (() => {
    if (activeStage === "topic") {
      if (phase === "scripting" || phase === "previewing") {
        return { label: phase === "scripting" ? "Generating script…" : "Generating previews…", onClick: () => {}, disabled: true, busy: true, cost: null };
      }
      if (!editingExisting) {
        // Create flow: writing the script happens HERE (Topic→Look) so the Look
        // page shows the script + the bg toggle. Refs are picked on the Look
        // page next; they only lock the image, never the words.
        return { label: "Next: choose the look →", onClick: startScript, disabled: !canGenerateScript, busy: false, cost: null, hint: "Writes the script, then pick characters, background & engine." };
      }
      return { label: applying ? "Updating…" : "Update script", onClick: applyEdits, disabled: applying || busy, busy: applying, cost: null, shortcut: !hasKeyframe, hint: "Re-applies topic, scene count & dialogue. Changed clips are flagged out-of-date — regenerate them in Stills/Videos." };
    }
    if (activeStage === "look") {
      if (phase === "scripting" || phase === "previewing") {
        return { label: phase === "scripting" ? "Generating script…" : "Generating previews…", onClick: () => {}, disabled: true, busy: true, cost: null };
      }
      if (!editingExisting) {
        // Create flow: the script already exists (written at Topic→Look). This
        // generates the still previews from the look you picked. An uploaded
        // character + no bg with "↻ New bg" OFF is used as-is (no image-gen).
        return { label: "Generate stills →", onClick: generateStills, disabled: !hasRun || busy, busy: false, cost: COST_HINT.generate_previews, hint: "Generates the still previews from the look you picked." };
      }
      // Apply look also makes any intro/outro card that is missing (an edit
      // dropped it, or it was never made): the Look step never leaves one out.
      if (cardsToMake.length) {
        return { label: applying ? "Applying…" : "Apply look and make cards", onClick: applyEdits, disabled: applying || busy, busy: applying, cost: COST_HINT.retry_preview, hint: `Re-applies references, character lock & engine, then makes ${cardsPhrase(cardsToMake)}.` };
      }
      // An edit of a run with stills may re-shoot them (a new framing,
      // speaker or reference, more scenes): a click, never ⌘/Ctrl+Enter.
      return { label: applying ? "Applying…" : "Apply look", onClick: applyEdits, disabled: applying || busy, busy: applying, cost: null, shortcut: !hasKeyframe, hint: "Re-applies references, character lock & engine. Affected clips are flagged out-of-date." };
    }
    if (activeStage === "stills") {
      if (_genStills) return { label: makingCards.length ? "Making cards…" : "Generating images…", onClick: () => {}, disabled: true, busy: true, cost: null };
      if (!hasKeyframe) return { label: "Generate previews", onClick: generateStills, disabled: !hasRun || busy, busy: false, cost: COST_HINT.generate_previews };
      // The videos wait for every wanted intro/outro card (a pick alone is no card).
      if (cardsToMake.length && run) {
        const r = run;
        return { label: `Make ${cardsPhrase(cardsToMake)}`, onClick: () => void makeCards(r, cardsToMake), disabled: busy, busy: false, cost: COST_HINT.retry_preview, hint: "Each card is made from its picked image and mode, then the videos can be made." };
      }
      // The clips: made only by this press, with what it costs; with every
      // clip up to date the button just goes to Videos.
      if (videosButton.kind !== "none") {
        return { label: `${videosButton.label} →`, onClick: makeVideos, disabled: videosButton.disabled, busy: false, cost: videosButton.cost, hint: videosButton.waiting ?? videosButton.hint };
      }
      return { label: "Go to Videos →", onClick: () => goToStage("videos"), disabled: !hasAnyClip, busy: false, cost: null, hint: "Every clip is up to date." };
    }
    if (activeStage === "videos") {
      if (_genVideos) return { label: "Veo generating…", onClick: () => {}, disabled: true, busy: true, cost: null };
      // A wanted intro/outro card is missing (an edit dropped it): its clip
      // waits for it, so it is made here rather than leave a dead end.
      if (makingCards.length) return { label: "Making cards…", onClick: () => {}, disabled: true, busy: true, cost: null };
      if (cardsToMake.length && run) {
        const r = run;
        return { label: `Make ${cardsPhrase(cardsToMake)}`, onClick: () => void makeCards(r, cardsToMake), disabled: busy, busy: false, cost: COST_HINT.retry_preview, hint: "The clips wait for it: each card is made from its picked image and mode, then its clip." };
      }
      // Missing or out-of-date clips: "Make videos" / "Update N clips", the
      // only way they are made (entering this stage makes nothing).
      if (videosButton.kind !== "none") {
        return { label: videosButton.label, onClick: makeVideos, disabled: videosButton.disabled, busy: false, cost: videosButton.cost, hint: videosButton.waiting ?? videosButton.hint };
      }
      if (hasRebrand) {
        return { label: "Rebrand →", onClick: () => goToStage("rebrand"), disabled: !run?.reel_ready || hasStaleIncluded, busy: false, cost: null };
      }
      return { label: "Stitch → Final", onClick: () => void finalizeReel(), disabled: compositionStatus !== "videos_ready" || hasStaleIncluded, busy: false, cost: "free", hint: "Remotion stitches the included clips into one MP4." };
    }
    if (activeStage === "rebrand") {
      // Not rendered: the stage has its own Finalize (top row) and the
      // studio its Generate, which owns ⌘/Ctrl+Enter here.
      return { label: "Finalize →", onClick: () => {}, disabled: true, busy: false, cost: null, shortcut: false };
    }
    // final
    if (compositionStatus === "ready") {
      // saved_current: the reel being served is the one saved (a reel finalized
      // again is offered for saving again). Absent on an older backend.
      if (phase === "saved" || (run?.saved_current ?? run?.saved_asset_id != null)) {
        return { label: "✓ Saved · Start a new one", onClick: resetForNew, disabled: false, busy: false, cost: null };
      }
      return { label: phase === "saving" ? "Saving…" : "Save to assets", onClick: saveToAssets, disabled: phase === "saving", busy: phase === "saving", cost: null };
    }
    if (stitching || compositionStatus === "rendering") {
      return { label: "Rendering…", onClick: () => {}, disabled: true, busy: true, cost: null };
    }
    if (hasRebrand) {
      return { label: "Go to Rebrand →", onClick: () => goToStage("rebrand"), disabled: !run?.reel_ready, busy: false, cost: null };
    }
    return { label: "Stitch from Videos →", onClick: () => goToStage("videos"), disabled: false, busy: false, cost: null, hint: "Generate & stitch the clips first." };
  })();

  // ----- Cmd/Ctrl+Enter fires the primary CTA when enabled -----
  // Keep cta in a ref so the keydown listener reads the latest value
  // without re-registering on every render (cta is no longer memoized).
  const primaryRef = useRef(primary);
  primaryRef.current = primary;
  const primaryCooldownRef = useRef(primaryCooldown);
  primaryCooldownRef.current = primaryCooldown;
  const activeRef = useRef(active);
  activeRef.current = active;
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        const c = primaryRef.current;
        // Hidden (another tool is on screen, or another app tab: the page
        // is kept, hidden), or the key is someone else's (the Rebrand
        // studio's Generate): not ours. Nor a primary that costs money:
        // Cmd+Enter in a tile's change box must never make the videos.
        if (!activeRef.current || !isShown(rootRef.current) || !shortcutFires(c.cost, c.shortcut)) return;
        if (!c.disabled && !primaryCooldownRef.current) {
          e.preventDefault();
          c.onClick();
        }
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // ----- Status line (under the tile row on stills / videos canvases) -----
  function statusLine(): string | null {
    if (activeStage === "stills") {
      if (phase === "scripting") return "Writing the script (~10 s)…";
      if (phase === "previewing") return "Generating 3 image previews (~15–30 s)…";
      if (makingCards.length) return `Making ${cardsPhrase(makingCards)}…`;
      if (phase === "retrying_intro") return "Regenerating the intro card…";
      // A still change says how long it has run, and a refine that it stops
      // by itself (stillLimitHint).
      if (phase === "retrying_main") {
        return [stillStatusLine(stillAction, "the main still", stillSeconds), stillLimitHint(stillAction)]
          .filter(Boolean).join(" ");
      }
      if (phase === "retrying_scene" && retryingSceneIdx !== null) {
        return [stillStatusLine(stillAction, `scene ${retryingSceneIdx + 1}'s still`, stillSeconds),
          stillLimitHint(stillAction)].filter(Boolean).join(" ");
      }
      if (phase === "polling_stills" && stillSeconds != null) {
        // A free crop never reads as a new still (lib/thStill).
        return stillWorkStatusLine(
          runScenes.filter((s) => s.keyframe_status === "generating").map((s) => s.keyframe_work),
          stillSeconds,
          imageCost,
        );
      }
      if (phase === "retrying_outro") return "Regenerating the outro card…";
      if (previewsReady) return "All ready — refine any tile, or move on to videos.";
      return null;
    }
    if (activeStage === "videos") {
      const clips: { status?: string; stale?: boolean; excluded?: boolean }[] = [];
      if (animations?.intro) {
        clips.push({
          status: animations.intro.status,
          stale: run?.intro_video_stale,
          excluded: run?.intro_excluded,
        });
      }
      runScenes.forEach((s) =>
        clips.push({
          status: s.animation?.status ?? undefined,
          stale: s.video_stale,
          excluded: s.excluded,
        }),
      );
      if (animations?.outro) {
        clips.push({
          status: animations.outro.status,
          stale: run?.outro_video_stale,
          excluded: run?.outro_excluded,
        });
      }
      const included = clips.filter((c) => !c.excluded);
      const ready = included.filter((c) => c.status === "ready").length;
      const gen = included.filter((c) => c.status === "generating").length;
      const failed = included.filter((c) => c.status === "failed").length;
      const stale = included.filter((c) => c.stale).length;
      const excluded = clips.filter((c) => c.excluded).length;
      const parts: string[] = [`${ready} of ${included.length} ready`];
      if (gen) parts.push(`${gen} generating`);
      if (failed) parts.push(`${failed} failed`);
      if (stale) parts.push(`${stale} out of date`);
      if (excluded) parts.push(`${excluded} excluded`);
      return parts.join(" · ");
    }
    return null;
  }

  // The Rebrand stage is full-bleed (it asks the layout itself): no negative
  // margins there, which would overflow the page, and just enough room under
  // it for the stage to end 16 px above the bottom of the screen.
  const onRebrand = !hydrating && !notFound && activeStage === "rebrand";

  return (
    // The page wrapper caps content at max-w-6xl. Negative horizontal
    // margins on lg+ break out of that cap so the studio can use the
    // ample whitespace on the sides instead of squeezing five preview
    // tiles into a 720px canvas. Sized to keep ~16-32px of true margin
    // even on a 1920px viewport.
    <div ref={rootRef} className={onRebrand ? "pb-2" : "pb-12 lg:-mx-4 xl:-mx-10 2xl:-mx-20"}>
      {/* ===== Header ===== */}
      <header className="flex items-center gap-3 mb-5">
        <button onClick={onBack} className="btn btn-ghost text-sm">← Back</button>
        <div className="flex-1 min-w-0">
          <p className="eyebrow">Ideation</p>
          <h1 className="h1">Human Element Video Studio</h1>
        </div>
      </header>

      {/* ===== Stage workspace =====
          The stepper is true navigation: click any reached stage to view +
          edit it. One focused full-width panel at a time. Driven by
          `activeStage` (user-controlled), not by `phase`. */}
      {notFound ? (
        <section className="card p-10 text-center space-y-3">
          <p className="text-lg">🗑️</p>
          <h2 className="text-base font-semibold text-fg">This run no longer exists</h2>
          <p className="text-sm text-muted max-w-sm mx-auto">
            It may have been deleted. Start a fresh Human Element Video, or head back to the Library.
          </p>
          <div className="flex items-center justify-center gap-2 pt-1">
            <button onClick={resetForNew} className="btn btn-primary text-sm">Start a new one</button>
            <button onClick={onBack} className="btn btn-ghost text-sm">← Back</button>
          </div>
        </section>
      ) : (
        <>
          {/* Stepper nav + run controls */}
          <div ref={stepperRowRef} className={`flex items-center justify-between gap-3 flex-wrap ${onRebrand ? "mb-2" : "mb-3"}`}>
            <StepperNav activeStage={activeStage} status={stageStatus} onNavigate={goToStage} />
            <div className="flex items-center gap-3">
              {run && (
                <button
                  type="button"
                  onClick={refreshRun}
                  disabled={refreshing}
                  title="Re-fetch the latest generated clips and status"
                  className="text-2xs text-muted hover:text-accent transition-colors disabled:opacity-50 inline-flex items-center gap-1"
                >
                  <span className={refreshing ? "inline-block animate-spin" : "inline-block"} aria-hidden>↻</span>
                  {refreshing ? "Refreshing…" : "Refresh"}
                </button>
              )}
              {run?.script && (
                <button
                  type="button"
                  onClick={() => setScriptDrawerOpen(true)}
                  className="text-2xs text-muted hover:text-accent transition-colors"
                >
                  View script →
                </button>
              )}
            </div>
          </div>

          {/* The Rebrand stage: its own card (the scene row, Finalize, the
              studio), mounted only while this creator is on screen — nothing
              of it runs under a hidden one. */}
          {onRebrand && run && (active ? (
            <RebrandStage
              run={run}
              setRun={setRun}
              target={rebrandTarget}
              onTarget={setRebrandTarget}
              anchorRef={stepperRowRef}
              rendering={stitching || compositionStatus === "rendering"}
              onFinalize={(sha) => void finalizeReel(sha)}
              onSeeFinal={() => goToStage("final")}
              onGoVideos={() => goToStage("videos")}
            />
          ) : null)}

          {/* Active stage panel */}
          {!onRebrand && (
          <section className="card p-5 lg:p-6 space-y-4 min-h-[480px]">
            {hydrating && (
              <div className="flex items-center justify-center gap-2 text-sm text-muted py-16">
                <span className="w-4 h-4 rounded-full border-2 border-accent border-t-transparent animate-spin" />
                Loading your draft…
              </div>
            )}

            {/* ---------- TOPIC ---------- */}
            {!hydrating && activeStage === "topic" && (
              <div className="space-y-3.5 max-w-2xl">
                <div>
                  <div className="flex items-center justify-between gap-2 mb-1.5">
                    <span className="eyebrow">Topic</span>
                  </div>
                  <div className="relative">
                    <textarea
                      rows={2}
                      value={topic}
                      onChange={(e) => setTopic(e.target.value)}
                      disabled={inputsDisabled}
                      placeholder="e.g. Why I joined 42 Abu Dhabi"
                      className="w-full rounded-lg border border-border bg-bg-subtle text-sm px-3 py-2 resize-none focus:outline-none focus:border-accent disabled:opacity-60 pr-24"
                    />
                    <EnhanceButton
                      onClick={enhanceTopic}
                      busy={enhancing}
                      disabled={enhancing || !topic.trim() || inputsDisabled}
                      title="Polish the topic into brand-on-tone copy"
                    />
                  </div>
                </div>
                <TalkingHeadV2ScenesEditor
                  sceneCount={sceneCount}
                  setSceneCount={setSceneCount}
                  dialogues={sceneDialogues}
                  setDialogue={setSceneDialogue}
                  onEnhance={enhanceScene}
                  enhancingIdx={enhancingSceneIdx}
                  disabled={inputsDisabled}
                />
                {editingExisting && (run?.archived_scenes?.length ?? 0) > 0 && (
                  <div className="rounded-lg border border-border bg-bg-subtle p-3 space-y-2">
                    <p className="text-2xs font-medium text-muted uppercase tracking-wider">
                      Removed scenes
                    </p>
                    <div className="flex flex-wrap gap-2">
                      {run!.archived_scenes!.map((a) => (
                        <div
                          key={a.archived_index}
                          className="flex items-center gap-2 rounded-md border border-border bg-bg px-2 py-1"
                        >
                          <span className="text-2xs text-fg/80 max-w-[160px] truncate">
                            {a.dialogue || a.hook || `Scene ${a.archived_index + 1}`}
                          </span>
                          <button
                            type="button"
                            onClick={() => restoreArchived(a.archived_index)}
                            disabled={busy || runSceneCount >= MAX_SCENES}
                            className="text-2xs text-accent hover:underline disabled:opacity-40"
                          >
                            Restore
                          </button>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* ---------- LOOK ---------- */}
            {!hydrating && activeStage === "look" && (
              <div className="flex gap-5 items-start">
                <div className="space-y-3.5 max-w-2xl flex-1 min-w-0">
                {sceneCount === 1 ? (
                  <>
                    <RefSlotRow
                      label="Character"
                      hint="A saved interview asset or upload a portrait."
                      slot={slots.character}
                      previewUrl={previewUrlForSlot(slots.character)}
                      previewLabel={slotLabel(slots.character)}
                      uploading={uploading.character}
                      uploadInputRef={uploadInputs.character}
                      disabled={inputsDisabled}
                      onPick={() => setPickerOpen("character")}
                      onClear={() => clearSlot("character")}
                      onFile={(f) => handleUploadFile("character", f)}
                    />
                    {slotIsSet(slots.character) && (
                      <label className="flex items-center gap-2 text-[11.5px] text-fg cursor-pointer pl-1">
                        <input
                          type="checkbox"
                          checked={lockCharacter}
                          onChange={(e) => setLockCharacter(e.target.checked)}
                          disabled={inputsDisabled}
                          className="rounded border-border"
                        />
                        <span>Lock to this exact character (face + outfit)</span>
                      </label>
                    )}
                    {speakerField}
                    <RefSlotRow
                      label="Background"
                      hint="A location photo to anchor the setting."
                      slot={slots.bg}
                      previewUrl={previewUrlForSlot(slots.bg)}
                      previewLabel={slotLabel(slots.bg)}
                      uploading={uploading.bg}
                      uploadInputRef={uploadInputs.bg}
                      disabled={inputsDisabled}
                      onPick={() => setPickerOpen("bg")}
                      onClear={() => clearSlot("bg")}
                      onFile={(f) => handleUploadFile("bg", f)}
                      bgMode={globalBgMode}
                      onSetBgMode={run ? setGlobalBgMode : undefined}
                    />
                  </>
                ) : (
                  <>
                    <div className="grid grid-cols-2 gap-3">
                      <div>
                        <p className="text-[10.5px] font-mono uppercase tracking-wider2 text-muted mb-2">
                          Character · per scene
                        </p>
                        <div className="space-y-2">
                          {Array.from({ length: sceneCount }, (_, i) => (
                            <SceneSlotRow
                              key={`char-${i}`}
                              sceneIdx={i}
                              kind="character"
                              override={sceneOverrides[i] ?? EMPTY_SCENE_OVERRIDE}
                              uploading={
                                sceneUploading?.idx === i &&
                                sceneUploading.kind === "character"
                              }
                              disabled={inputsDisabled}
                              fileUrlFor={(a) => api.producerAssets.thumbUrlFor(a)}
                              onPick={() => setScenePicker({ idx: i, kind: "character" })}
                              onUpload={(f) => handleSceneUpload(i, "character", f)}
                              onClear={() =>
                                setSceneOverridePartial(i, {
                                  character_asset: null,
                                  character_upload: null,
                                })
                              }
                              onCopyToAll={() => {
                                const src = sceneOverrides[i];
                                for (let j = 0; j < sceneCount; j++) {
                                  if (j === i) continue;
                                  setSceneOverridePartial(j, {
                                    character_asset: src.character_asset,
                                    character_upload: src.character_upload,
                                  });
                                }
                              }}
                            />
                          ))}
                        </div>
                      </div>
                      <div>
                        <p className="text-[10.5px] font-mono uppercase tracking-wider2 text-muted mb-2">
                          Background · per scene
                        </p>
                        <div className="space-y-2">
                          {Array.from({ length: sceneCount }, (_, i) => (
                            <SceneSlotRow
                              key={`bg-${i}`}
                              sceneIdx={i}
                              kind="bg"
                              override={sceneOverrides[i] ?? EMPTY_SCENE_OVERRIDE}
                              uploading={
                                sceneUploading?.idx === i &&
                                sceneUploading.kind === "bg"
                              }
                              disabled={inputsDisabled}
                              fileUrlFor={(a) => api.producerAssets.thumbUrlFor(a)}
                              onPick={() => setScenePicker({ idx: i, kind: "bg" })}
                              onUpload={(f) => handleSceneUpload(i, "bg", f)}
                              onClear={() =>
                                setSceneOverridePartial(i, {
                                  bg_asset: null,
                                  bg_upload: null,
                                })
                              }
                              onCopyToAll={() => {
                                const src = sceneOverrides[i];
                                for (let j = 0; j < sceneCount; j++) {
                                  if (j === i) continue;
                                  setSceneOverridePartial(j, {
                                    bg_asset: src.bg_asset,
                                    bg_upload: src.bg_upload,
                                  });
                                }
                              }}
                              onSetBgMode={
                                run
                                  ? (m) => setSceneOverridePartial(i, { bg_mode: m })
                                  : undefined
                              }
                            />
                          ))}
                        </div>
                      </div>
                    </div>
                    {sceneOverrides.slice(0, sceneCount).some(
                      (o) => o.character_asset || o.character_upload,
                    ) && (
                      <label className="flex items-center gap-2 text-[11.5px] text-fg cursor-pointer pl-1 pt-1">
                        <input
                          type="checkbox"
                          checked={lockCharacter}
                          onChange={(e) => setLockCharacter(e.target.checked)}
                          disabled={inputsDisabled}
                          className="rounded border-border"
                        />
                        <span>Lock to each scene's exact character (face + outfit)</span>
                      </label>
                    )}
                    {speakerField}
                  </>
                )}
                <RefSlotRow
                  label="Intro card"
                  hint="Your intro card (kept as-is, or a new design in its style), or a finished video used as the intro."
                  slot={slots.intro}
                  previewUrl={previewUrlForSlot(slots.intro)}
                  previewLabel={slotLabel(slots.intro)}
                  uploading={uploading.intro}
                  uploadInputRef={uploadInputs.intro}
                  disabled={inputsDisabled}
                  onPick={() => setPickerOpen("intro")}
                  onClear={() => clearSlot("intro")}
                  onFile={(f) => handleUploadFile("intro", f)}
                  generate={generateIntro}
                  onToggleGenerate={(on) => toggleGenerateBookend("intro", on)}
                  onSetRefMode={(m) => applyRefMode("intro", m)}
                  onSetKeepText={(on) => applyKeepText("intro", on)}
                  acceptVideo
                />
                <RefSlotRow
                  label="Outro card"
                  hint="Your outro card (kept as-is, or a new design in its style), or a finished video used as the outro."
                  slot={slots.outro}
                  previewUrl={previewUrlForSlot(slots.outro)}
                  previewLabel={slotLabel(slots.outro)}
                  uploading={uploading.outro}
                  uploadInputRef={uploadInputs.outro}
                  disabled={inputsDisabled}
                  onPick={() => setPickerOpen("outro")}
                  onClear={() => clearSlot("outro")}
                  onFile={(f) => handleUploadFile("outro", f)}
                  generate={generateOutro}
                  onToggleGenerate={(on) => toggleGenerateBookend("outro", on)}
                  onSetRefMode={(m) => applyRefMode("outro", m)}
                  onSetKeepText={(on) => applyKeepText("outro", on)}
                  acceptVideo
                />
                <div className="pt-3 border-t border-border space-y-2">
                  <p className="eyebrow">Framing</p>
                  <div
                    className="inline-flex rounded-lg border border-border bg-bg-subtle p-0.5 w-full"
                    role="group"
                    aria-label="Framing"
                  >
                    {FRAMING_CHOICES.map((c) => (
                      <button
                        key={c.value}
                        type="button"
                        onClick={() => setFraming(c.value)}
                        disabled={inputsDisabled}
                        aria-pressed={framing === c.value}
                        title={c.hint}
                        className={[
                          "flex-1 px-2 py-1 rounded-md text-2xs font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed",
                          framing === c.value ? "bg-fg text-bg" : "text-muted hover:text-fg",
                        ].join(" ")}
                      >
                        {c.label}
                      </button>
                    ))}
                  </div>
                  <p className="text-[10.5px] text-muted leading-snug">{framingHint(framing, run, imageCost)}</p>
                </div>
                <div className="pt-3 border-t border-border space-y-2">
                  <p className="eyebrow">Video size</p>
                  <div
                    className="inline-flex rounded-lg border border-border bg-bg-subtle p-0.5 w-full"
                    role="group"
                    aria-label="Video size"
                  >
                    {([
                      ["9:16", "9:16 Portrait"],
                      ["16:9", "16:9 Landscape"],
                    ] as const).map(([val, lbl]) => (
                      <button
                        key={val}
                        type="button"
                        onClick={() => setVideoAspect(val)}
                        disabled={inputsDisabled || sizeLocked}
                        aria-pressed={videoAspect === val}
                        title={sizeLocked ? VIDEO_SIZE_LOCKED_HINT : undefined}
                        className={[
                          "flex-1 px-2 py-1 rounded-md text-2xs font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed",
                          videoAspect === val ? "bg-fg text-bg" : "text-muted hover:text-fg",
                        ].join(" ")}
                      >
                        {lbl}
                      </button>
                    ))}
                  </div>
                  <p className="text-[10.5px] text-muted leading-snug">
                    {sizeLocked ? VIDEO_SIZE_LOCKED_HINT : VIDEO_SIZE_HINT}
                  </p>
                </div>
                </div>
                {/* Script review — fills the right side so the user can read
                    the generated script while choosing the look. Shown only
                    when a script exists (editing an existing run); the create
                    flow has no script yet, so the panel stays hidden. */}
                {run?.script && (
                  <aside className="hidden lg:flex flex-col flex-1 min-w-[300px] self-start sticky top-2 max-h-[calc(100vh-8.5rem)] rounded-xl border border-border bg-bg-subtle/40 overflow-hidden">
                    <header className="shrink-0 px-3 py-2 border-b border-border/60 flex items-center justify-between gap-2">
                      <span className="eyebrow">Script</span>
                      <span className="text-3xs text-muted font-mono uppercase tracking-wider2">hover to edit</span>
                    </header>
                    <div className="overflow-y-auto p-3 min-h-0">
                      <ScriptReview run={run} onRunUpdate={setRun} />
                    </div>
                  </aside>
                )}
              </div>
            )}

            {/* ---------- STILLS ---------- */}
            {!hydrating && activeStage === "stills" && (
              run ? (
                <div className="space-y-3">
                  <SceneRow count={runSceneCount + 2}>
                    <PreviewTile
                      aspectClass={aspectClass}
                      videoAspect={videoAspect}
                      title="Intro"
                      modelLabel={run.image_models.intro}
                      card={intro}
                      externalError={run.intro_error}
                      excluded={!!run.intro_excluded}
                      feedback={feedback.intro}
                      setFeedback={(t) => setFeedback((p) => ({ ...p, intro: t }))}
                      onRetry={() => retryTile("intro")}
                      busy={phase === "retrying_intro" || makingCards.includes("intro")}
                      pageBusy={inputsDisabled}
                      pickUrl={slotHasImage(slots.intro) ? previewUrlForSlot(slots.intro) : null}
                      busyLabel={makingCards.includes("intro") ? "Making the card…" : undefined}
                      clip={animations?.intro ?? null}
                      clipStale={!!run.intro_video_stale}
                      quality={run.intro_video_quality}
                      onQualityChange={(q) => setSegmentQuality("intro", q)}
                      directVideoUrl={slotIsVideo(slots.intro) ? previewUrlForSlot(slots.intro) : null}
                    />
                    {runSceneCount === 1 ? (
                      <PreviewTile
                        aspectClass={aspectClass}
                        videoAspect={videoAspect}
                        title="Main"
                        modelLabel={run.image_models.main}
                        card={
                          main
                            ? ({
                                status: main.url ? "ready" : main.error ? "failed" : "generating",
                                url: main.url,
                                error: main.error,
                                width: main.width,
                                height: main.height,
                              } as unknown as TalkingHeadCard)
                            : null
                        }
                        externalError={run.main_error}
                        feedback={feedback.main}
                        setFeedback={(t) => setFeedback((p) => ({ ...p, main: t }))}
                        onRetry={() => stillTileAction("main", "refine")}
                        onRegenerate={() => stillTileAction("main", "regenerate")}
                        onUndo={() => stillTileAction("main", "undo")}
                        canUndo={!!runScenes[0]?.can_undo}
                        busy={
                          phase === "retrying_main" ||
                          runScenes[0]?.keyframe_status === "generating"
                        }
                        busyLabel={
                          phase === "retrying_main"
                            ? stillBusyLabel(stillAction, stillSeconds)
                            : stillBusyLabel(null, phase === "polling_stills" ? stillSeconds : null,
                                runScenes[0]?.keyframe_work)
                        }
                        prominent
                        framingWarning={runScenes[0]?.framing_warning ?? null}
                        quality={run.main_video_quality}
                        onQualityChange={(q) => setSegmentQuality("main", q)}
                        framing={stillFraming(runScenes[0])}
                        pageBusy={inputsDisabled}
                        asIsPhoto={!!main?.url && !!runScenes[0]?.as_is}
                        onFramingChange={(f) => setStillFraming(0, f)}
                        imageCost={imageCost}
                      />
                    ) : (
                      runScenes.map((s, i) => (
                        <PreviewTile
                          key={`scene-${i}`}
                          aspectClass={aspectClass}
                          videoAspect={videoAspect}
                          title={`Scene ${i + 1}`}
                          modelLabel={run.image_models.main}
                          card={
                            s.keyframe_url
                              ? ({
                                  status: "ready", url: s.keyframe_url, error: null,
                                  width: s.width, height: s.height,
                                } as unknown as TalkingHeadCard)
                              : (null as TalkingHeadCard | null)
                          }
                          externalError={s.error}
                          feedback={sceneFeedback[i] || ""}
                          setFeedback={(t) => setSceneFeedback((prev) => ({ ...prev, [i]: t }))}
                          onRetry={() => stillTileAction(`scene_${i}`, "refine")}
                          onRegenerate={() => stillTileAction(`scene_${i}`, "regenerate")}
                          onUndo={() => stillTileAction(`scene_${i}`, "undo")}
                          canUndo={!!s.can_undo}
                          busy={
                            (phase === "retrying_scene" && retryingSceneIdx === i) ||
                            s.keyframe_status === "generating"
                          }
                          busyLabel={
                            phase === "retrying_scene" && retryingSceneIdx === i
                              ? stillBusyLabel(stillAction, stillSeconds)
                              : stillBusyLabel(null, phase === "polling_stills" ? stillSeconds : null,
                                  s.keyframe_work)
                          }
                          prominent={i === 0}
                          framingWarning={s.framing_warning ?? null}
                          quality={s.video_quality}
                          onQualityChange={(q) => setSegmentQuality(`scene_${i}`, q)}
                          framing={stillFraming(s)}
                          pageBusy={inputsDisabled}
                          asIsPhoto={!!s.keyframe_url && !!s.as_is}
                          onFramingChange={(f) => setStillFraming(i, f)}
                          imageCost={imageCost}
                        />
                      ))
                    )}
                    <PreviewTile
                      aspectClass={aspectClass}
                      videoAspect={videoAspect}
                      title="Outro"
                      modelLabel={run.image_models.outro}
                      card={outro}
                      externalError={run.outro_error}
                      excluded={!!run.outro_excluded}
                      feedback={feedback.outro}
                      setFeedback={(t) => setFeedback((p) => ({ ...p, outro: t }))}
                      onRetry={() => retryTile("outro")}
                      busy={phase === "retrying_outro" || makingCards.includes("outro")}
                      pageBusy={inputsDisabled}
                      pickUrl={slotHasImage(slots.outro) ? previewUrlForSlot(slots.outro) : null}
                      busyLabel={makingCards.includes("outro") ? "Making the card…" : undefined}
                      clip={animations?.outro ?? null}
                      clipStale={!!run.outro_video_stale}
                      quality={run.outro_video_quality}
                      onQualityChange={(q) => setSegmentQuality("outro", q)}
                      directVideoUrl={slotIsVideo(slots.outro) ? previewUrlForSlot(slots.outro) : null}
                    />
                  </SceneRow>
                  <p className="text-[10.5px] text-muted text-center">
                    Set each still's video quality below — Quality uses the premium Veo
                    model (sharper faces, higher cost per clip).
                  </p>
                  <StatusLine text={statusLine()} />
                </div>
              ) : (
                <div className="text-center text-sm text-muted py-16">
                  Generate the script first (Topic stage) to see the still previews here.
                </div>
              )
            )}

            {/* ---------- VIDEOS ---------- */}
            {!hydrating && activeStage === "videos" && (
              animations ? (
                <div className="space-y-3">
                  {runSceneCount > 1 && (
                    <div className="flex items-center justify-between gap-2 flex-wrap">
                      <button
                        type="button"
                        onClick={() => { setBatchMode((b) => !b); setSelectedScenes(new Set()); }}
                        className="btn btn-ghost text-xs"
                      >
                        {batchMode ? "Cancel selection" : "Select scenes"}
                      </button>
                      {batchMode && (
                        <div className="flex items-center gap-2">
                          <span className="text-2xs text-muted">{selectedScenes.size} selected</span>
                          <button type="button" disabled={selectedScenes.size === 0 || applying} onClick={() => batchRegenerate("images")} className="btn btn-ghost text-xs disabled:opacity-50">Regen images</button>
                          <button type="button" disabled={selectedScenes.size === 0 || applying} onClick={() => batchRegenerate("videos")} className="btn btn-ghost text-xs disabled:opacity-50">Regen videos</button>
                          <button type="button" disabled={selectedScenes.size === 0 || applying} onClick={() => batchRegenerate("both")} className="btn btn-primary text-xs disabled:opacity-50">Regen both</button>
                        </div>
                      )}
                    </div>
                  )}
                  <SceneRow count={runSceneCount + 2}>
                    <VideoTile
                      aspectClass={aspectClass}
                      title="Intro"
                      anim={animations.intro}
                      motionLine={cardTileNote(run?.intro, animations.intro, !!run?.intro_video_stale).motionLine}
                      feedback={videoFeedback.intro}
                      setFeedback={(t) => setVideoFeedback((p) => ({ ...p, intro: t }))}
                      onRetry={(force) => retryVideoTile("intro", force)}
                      busy={phase === "retrying_video_intro"}
                      retryAllowed={compositionStatus === "videos_ready"}
                      waitingFor={bookendClipWaits(run, "intro")}
                      stale={!!run?.intro_video_stale}
                      excluded={!!run?.intro_excluded}
                      onToggleExclude={() => toggleExclude("intro", !run?.intro_excluded)}
                      directVideoUrl={slotIsVideo(slots.intro) ? previewUrlForSlot(slots.intro) : null}
                    />
                    {runSceneCount === 1 ? (
                      <VideoTile
                        aspectClass={aspectClass}
                        title="Main"
                        anim={animations.main}
                        prominent
                        feedback={videoFeedback.main}
                        setFeedback={(t) => setVideoFeedback((p) => ({ ...p, main: t }))}
                        onRetry={(force) => retryVideoTile("main", force)}
                        busy={phase === "retrying_video_main"}
                        retryAllowed={compositionStatus === "videos_ready"}
                        stale={!!run?.main_video_stale}
                      />
                    ) : (
                      runScenes.map((s, i) => (
                        <VideoTile
                          key={`scene-vid-${i}`}
                          aspectClass={aspectClass}
                          title={`Scene ${i + 1}`}
                          anim={s.animation}
                          prominent={i === 0}
                          feedback={sceneVideoFeedback[i] || ""}
                          setFeedback={(t) => setSceneVideoFeedback((prev) => ({ ...prev, [i]: t }))}
                          onRetry={(force) => retryVideoTile(`scene_${i}` as `scene_${number}`, force)}
                          busy={phase === "retrying_scene_video" && retryingSceneIdx === i}
                          retryAllowed={compositionStatus === "videos_ready"}
                          stale={!!s.video_stale}
                          excluded={!!s.excluded}
                          onToggleExclude={() => toggleExclude(`scene_${i}`, !s.excluded)}
                          selectable={batchMode}
                          selected={selectedScenes.has(i)}
                          onToggleSelect={() =>
                            setSelectedScenes((prev) => {
                              const n = new Set(prev);
                              if (n.has(i)) n.delete(i); else n.add(i);
                              return n;
                            })
                          }
                        />
                      ))
                    )}
                    <VideoTile
                      aspectClass={aspectClass}
                      title="Outro"
                      anim={animations.outro}
                      motionLine={cardTileNote(run?.outro, animations.outro, !!run?.outro_video_stale).motionLine}
                      feedback={videoFeedback.outro}
                      setFeedback={(t) => setVideoFeedback((p) => ({ ...p, outro: t }))}
                      onRetry={(force) => retryVideoTile("outro", force)}
                      busy={phase === "retrying_video_outro"}
                      retryAllowed={compositionStatus === "videos_ready"}
                      waitingFor={bookendClipWaits(run, "outro")}
                      stale={!!run?.outro_video_stale}
                      excluded={!!run?.outro_excluded}
                      onToggleExclude={() => toggleExclude("outro", !run?.outro_excluded)}
                      directVideoUrl={slotIsVideo(slots.outro) ? previewUrlForSlot(slots.outro) : null}
                    />
                  </SceneRow>
                  <StatusLine text={statusLine()} />
                  {videosStaleNote(videosButton) && (
                    <p className="text-[11px] text-amber-700 text-center leading-relaxed">{videosStaleNote(videosButton)}</p>
                  )}
                </div>
              ) : (
                <div className="text-center text-sm text-muted py-16 space-y-1">
                  <p>No clips yet.</p>
                  <p className="text-xs">
                    {videosButton.kind === "make"
                      ? `Press "Make videos" below to make them${videosButton.cost ? ` (${videosButton.cost})` : ""}: nothing is made until you do.`
                      : "Make the still previews first."}
                  </p>
                </div>
              )
            )}

            {/* ---------- FINAL ---------- */}
            {!hydrating && activeStage === "final" && (
              <div className="flex flex-col items-center gap-4 py-3">
                {run?.composition_stale && compositionStatus === "ready" && !run?.composition_error && (
                  hasRebrand ? (
                    <div className="w-full max-w-md text-[12px] text-amber-700 bg-warning/10 border border-warning/30 rounded-lg px-3 py-2 flex items-center justify-center gap-3">
                      <span>Changed since you finalized.</span>
                      <button type="button" onClick={() => goToStage("rebrand")} disabled={!run?.reel_ready} className="font-semibold underline-offset-2 hover:underline disabled:opacity-50">
                        Back to Rebrand
                      </button>
                    </div>
                  ) : (
                    <div className="w-full max-w-md text-[12px] text-amber-700 bg-warning/10 border border-warning/30 rounded-lg px-3 py-2 text-center">
                      This composition is out of date — a clip changed. Re-stitch from the
                      Videos stage to refresh it.
                    </div>
                  )
                )}
                {/* The reel being served: the newest, or, while a new one is
                    made or after it failed, the last good one (its URL names
                    it, ?v=, so the player swaps when a new one is ready). */}
                {compositionUrl && (hasRebrand || compositionStatus === "ready") ? (
                  <>
                    <video
                      key={compositionUrl}
                      src={compositionUrl}
                      controls
                      playsInline
                      className="rounded-xl bg-black shadow-lift"
                      style={{ aspectRatio: videoAspect.replace(":", "/"), maxHeight: "70vh", width: "auto" }}
                    />
                    {(stitching || compositionStatus === "rendering") && (
                      <p role="status" className="inline-flex items-center gap-1.5 text-2xs text-muted">
                        <span className="w-3 h-3 rounded-full border-2 border-accent border-t-transparent animate-spin" aria-hidden />
                        Making the new reel…
                      </p>
                    )}
                    {compositionStatus === "ready" && !!run?.composition_error && (
                      <p role="alert" className="inline-flex items-center gap-2 text-2xs text-danger">
                        The new render failed.
                        <button type="button" onClick={() => void finalizeReel(run?.reel?.inputs_sha ?? null)} className="font-semibold underline-offset-2 hover:underline">
                          Try again
                        </button>
                      </p>
                    )}
                  </>
                ) : compositionStatus === "failed" ? (
                  hasRebrand ? (
                    <div role="alert" className="bg-danger/10 border border-danger/30 rounded-lg p-6 text-sm text-danger max-w-md text-center space-y-2">
                      <p>The final render failed.</p>
                      <button type="button" onClick={() => void finalizeReel(run?.reel?.inputs_sha ?? null)} className="btn btn-ghost text-xs">
                        Try again
                      </button>
                    </div>
                  ) : (
                    <div className="bg-danger/10 border border-danger/30 rounded-lg p-6 text-sm text-danger max-w-md text-center">
                      Composition failed. Regenerate the videos and stitch again.
                    </div>
                  )
                ) : stitching || compositionStatus === "rendering" ? (
                  <div className={`${aspectClass} w-[240px] rounded-xl bg-bg-muted border border-border flex items-center justify-center`}>
                    <div className="text-center text-xs text-muted space-y-2">
                      <div className="w-6 h-6 mx-auto rounded-full border-2 border-accent border-t-transparent animate-spin" />
                      <p>{hasRebrand ? "Making the reel…" : "Stitching final…"}</p>
                    </div>
                  </div>
                ) : hasRebrand ? (
                  <div className="text-center text-sm text-muted py-10 space-y-2">
                    <p>Not finalized yet.</p>
                    <button onClick={() => goToStage("rebrand")} disabled={!run?.reel_ready} className="btn btn-ghost text-xs disabled:opacity-50">
                      Go to Rebrand →
                    </button>
                  </div>
                ) : (
                  <div className="text-center text-sm text-muted py-10 space-y-2">
                    <p>No composition yet.</p>
                    <button onClick={() => goToStage("videos")} className="btn btn-ghost text-xs">
                      Go to Videos to stitch →
                    </button>
                  </div>
                )}
                {compositionStatus === "ready" && (
                  <>
                    <button
                      type="button"
                      onClick={() => openInEditor("final")}
                      disabled={openingEditor}
                      className="btn btn-ghost text-xs disabled:opacity-50"
                      title="Open this final video in the multi-track editor as a new project"
                    >
                      {openingEditor ? "Opening editor…" : "🎬 Open in editor"}
                    </button>
                    {onContinue && run && (
                      <button
                        type="button"
                        onClick={() =>
                          onContinue({
                            refId: run.id,
                            mediaType: "video",
                            mediaUrl: api.yasProducer.talkingHeadV2.compositionFileUrl(run.id, run.composition_good_seq),
                            topic: run.topic || topic || "",
                          })
                        }
                        className="btn btn-accent text-xs"
                        title="Use this final video — continue to Preview"
                      >
                        Continue to Preview →
                      </button>
                    )}
                    <p className="text-2xs text-muted">
                      Final composition · {videoAspect}
                    </p>
                  </>
                )}
              </div>
            )}

            {/* ---------- Per-stage primary action ---------- */}
            {!hydrating && (
              <div className="pt-3 border-t border-border flex items-center gap-x-4 gap-y-2 flex-wrap">
                <button
                  type="button"
                  onClick={primary.onClick}
                  disabled={primary.disabled || primaryCooldown}
                  className={[
                    "relative overflow-hidden rounded-lg px-5 py-2.5 text-sm font-semibold transition shrink-0",
                    primary.disabled || primaryCooldown
                      ? "bg-bg-muted text-muted cursor-not-allowed"
                      : "bg-accent text-white hover:bg-accent-hover",
                  ].join(" ")}
                >
                  <span className="relative z-10 flex items-center gap-1.5">
                    {primary.label}
                    {primary.cost && !primary.busy && (
                      <span className="text-2xs font-normal opacity-80">· {primary.cost}</span>
                    )}
                  </span>
                  {primary.busy && (
                    <span
                      className="absolute inset-y-0 w-1/3 bg-white/20 pointer-events-none animate-sheen"
                      aria-hidden
                    />
                  )}
                </button>
                {/* Before stitch: send the individual ready clips into the editor. */}
                {activeStage === "videos" && compositionStatus === "videos_ready" && (
                  <button
                    type="button"
                    onClick={() => openInEditor("clips")}
                    disabled={openingEditor}
                    className="btn btn-ghost text-xs disabled:opacity-50 shrink-0"
                    title="Open these clips in the multi-track editor as a new project"
                  >
                    {openingEditor ? "Opening editor…" : "🎬 Open in editor"}
                  </button>
                )}
                {(primary.hint || !primary.busy) && (
                  <p className="text-[10.5px] text-muted leading-relaxed min-w-0">
                    {primary.hint}{" "}
                    {/* A paid button takes a click: the shortcut never spends. */}
                    {shortcutFires(primary.cost, primary.shortcut) && (
                      <kbd className="font-mono text-[9px] px-1 py-0.5 rounded border border-border bg-bg-subtle">
                        ⌘↵
                      </kbd>
                    )}
                  </p>
                )}
              </div>
            )}
          </section>
          )}
        </>
      )}

      {/* Asset picker modal — global Look & Feel slots */}
      {pickerOpen && (
        <AssetPickerModal
          singleSelectMode="radio"
          contentCategory={
            pickerOpen === "character"
              ? ["video_interview", "img_human_character"]
              : pickerOpen === "bg"
                ? "img_bg"
                : pickerOpen === "intro"
                  ? "img_intro"
                  : "img_outro"
          }
          maxSelection={1}
          alreadySelectedAssetIds={[]}
          preselectedAssetId={slots[pickerOpen].asset?.id ?? null}
          onPick={(assets) => {
            const a = assets[0];
            if (a) setSlot(pickerOpen, { asset: a, upload: null, card: null });
            setPickerOpen(null);
          }}
          // Intro / outro: the cards of earlier videos too. A picked one is
          // kept as it is (its words swapped only when they differ).
          extraTab={
            pickerOpen === "intro" || pickerOpen === "outro"
              ? {
                  label: "Previous videos",
                  content: (
                    <TalkingHeadV2PreviousCards
                      role={pickerOpen}
                      excludeRunId={run?.id ?? null}
                      videoAspect={videoAspect}
                      selectedCardId={slots[pickerOpen].card?.card_id ?? null}
                      onPick={(c) => {
                        setSlot(pickerOpen, {
                          asset: null, upload: null, card: pickedCardFrom(c), refMode: "as_is",
                        });
                        setLookOpen(true);
                        setPickerOpen(null);
                      }}
                    />
                  ),
                }
              : undefined
          }
          onSkip={() => setPickerOpen(null)}
          onClose={() => setPickerOpen(null)}
        />
      )}

      {/* v6 — Asset picker modal for per-scene character/bg overrides */}
      {scenePicker && (
        <AssetPickerModal
          singleSelectMode="radio"
          contentCategory={scenePicker.kind === "character" ? ["video_interview", "img_human_character"] : "img_bg"}
          maxSelection={1}
          alreadySelectedAssetIds={[]}
          preselectedAssetId={
            (scenePicker.kind === "character"
              ? sceneOverrides[scenePicker.idx]?.character_asset?.id
              : sceneOverrides[scenePicker.idx]?.bg_asset?.id) ?? null
          }
          onPick={(assets) => {
            const a = assets[0];
            if (a) {
              setSceneOverridePartial(
                scenePicker.idx,
                scenePicker.kind === "character"
                  ? { character_asset: a, character_upload: null }
                  : { bg_asset: a, bg_upload: null },
              );
            }
            setScenePicker(null);
          }}
          onSkip={() => setScenePicker(null)}
          onClose={() => setScenePicker(null)}
        />
      )}

      {/* Script side drawer */}
      <TalkingHeadV2ScriptDrawer
        open={scriptDrawerOpen}
        script={run?.script ?? null}
        onClose={() => setScriptDrawerOpen(false)}
      />
    </div>
  );
}


// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function cap(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** A refusal in the server's own words ("Main 2 is still being made."),
 *  without the status code the api client puts in front. */
function serverWords(e: any): string {
  return String(e?.message ?? e ?? "").replace(/^\d{3}\s+/, "");
}

function StatusLine({ text }: { text: string | null }) {
  if (!text) return null;
  return (
    <p className="text-[11.5px] text-muted text-center leading-relaxed pt-1">
      {text}
    </p>
  );
}

/** Horizontal compact slot row for the sidebar Look & Feel section.
 *  Empty: shows two small buttons (Pick / Upload).
 *  Set: shows thumb + filename + Clear. */
function RefSlotRow({
  label,
  hint,
  slot,
  previewUrl,
  previewLabel,
  uploading,
  uploadInputRef,
  disabled,
  onPick,
  onClear,
  onFile,
  bgMode,
  onSetBgMode,
  generate,
  onToggleGenerate,
  onSetRefMode,
  onSetKeepText,
  acceptVideo,
}: {
  label: string;
  hint: string;
  slot: RefSlotState;
  previewUrl: string | null;
  previewLabel: string;
  uploading: boolean;
  uploadInputRef: React.RefObject<HTMLInputElement>;
  disabled?: boolean;
  onPick: () => void;
  onClear: () => void;
  onFile: (f: File) => void;
  /** Edit-mode only: toggle a fresh AI background while keeping the existing
   *  character. Omitted → no toggle shown. */
  bgMode?: "keep" | "new";
  onSetBgMode?: (mode: "keep" | "new") => void;
  /** Bookend generate-toggle (intro/outro). When provided, a header switch
   *  decides whether this bookend is produced at all. Omitted → no switch. */
  generate?: boolean;
  onToggleGenerate?: (on: boolean) => void;
  /** intro/outro only: how a picked image is used. When provided AND an
   *  image is set, a "Keep as-is / New design in this style" control is shown. */
  onSetRefMode?: (mode: TalkingHeadRefMode) => void;
  /** intro/outro only: "Keep card text", shown for an image kept as-is. */
  onSetKeepText?: (on: boolean) => void;
  /** intro/outro: also accept a finished video clip (used as-is, no card-gen). */
  acceptVideo?: boolean;
}) {
  const hasSomething = !!slot.asset || !!slot.upload || !!slot.card;
  const isVideo = !!slot.upload && (slot.upload.mime_type || "").startsWith("video/");
  const hasGenToggle = !!onToggleGenerate;
  const on = generate !== false;
  const skipped = hasGenToggle && !on;
  const actsDisabled = disabled || skipped;
  const refMode: TalkingHeadRefMode = normRefMode(slot.refMode);
  return (
    <div
      className={`rounded-lg border border-border bg-bg-subtle/40 p-2 transition-opacity ${
        skipped ? "opacity-55" : ""
      }`}
    >
      <div className="flex items-center gap-2">
        {/* Thumb or placeholder */}
        <div className="w-10 h-10 rounded bg-bg-muted border border-border shrink-0 overflow-hidden flex items-center justify-center">
          {!skipped && hasSomething && previewUrl ? (
            isVideo ? (
              <video src={previewUrl} muted playsInline className="w-full h-full object-cover" />
            ) : (
              <img src={previewUrl} alt={previewLabel} className="w-full h-full object-cover" />
            )
          ) : (
            <span className="text-[18px] text-muted/60">{skipped ? "∅" : "+"}</span>
          )}
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-[11.5px] font-medium text-fg leading-tight">{label}</p>
          {skipped ? (
            <p className="text-[10.5px] text-muted truncate">Skipped — won't be generated</p>
          ) : hasSomething ? (
            <p className="text-[10.5px] text-muted truncate">{previewLabel}</p>
          ) : (
            <p className="text-[10.5px] text-muted truncate">{hint}</p>
          )}
        </div>
        {hasGenToggle && (
          <button
            type="button"
            role="switch"
            aria-checked={on}
            onClick={() => onToggleGenerate(!on)}
            disabled={disabled}
            title={on ? "Generate this bookend" : "Don't generate this bookend"}
            className={`relative h-4 w-7 rounded-full shrink-0 transition-colors disabled:opacity-40 ${
              on ? "bg-accent" : "bg-bg-muted border border-border"
            }`}
          >
            <span
              className={`absolute top-0.5 h-3 w-3 rounded-full bg-bg transition-all ${
                on ? "left-3.5" : "left-0.5"
              }`}
            />
          </button>
        )}
        {skipped ? null : hasSomething ? (
          <button
            type="button"
            onClick={onClear}
            disabled={actsDisabled}
            className="text-3xs text-muted hover:text-fg disabled:opacity-40 transition-colors px-1.5 py-0.5"
          >
            Clear
          </button>
        ) : (
          <div className="flex items-center gap-1 shrink-0">
            {onSetBgMode && (
              <button
                type="button"
                onClick={() => onSetBgMode(bgMode === "new" ? "keep" : "new")}
                disabled={disabled}
                aria-pressed={bgMode === "new"}
                className={`text-[10.5px] rounded px-2 py-1 border transition-colors disabled:opacity-40 ${
                  bgMode === "new"
                    ? "bg-accent/15 border-accent text-accent"
                    : "bg-bg-subtle border-border text-muted hover:text-fg"
                }`}
                title={
                  bgMode === "new"
                    ? "A fresh background will be generated (character kept)"
                    : "No change — the current still passes through untouched"
                }
              >
                ↻ New bg
              </button>
            )}
            <button
              type="button"
              onClick={onPick}
              disabled={uploading || disabled}
              className="text-[10.5px] text-fg bg-bg-subtle border border-border rounded px-2 py-1 hover:border-accent disabled:opacity-40 transition-colors"
            >
              Pick
            </button>
            <input
              ref={uploadInputRef}
              type="file"
              accept={acceptVideo ? "image/*,video/*" : "image/*"}
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) onFile(f);
                if (uploadInputRef.current) uploadInputRef.current.value = "";
              }}
              className="hidden"
            />
            <button
              type="button"
              onClick={() => uploadInputRef.current?.click()}
              disabled={uploading || disabled}
              className="text-[10.5px] text-fg bg-bg-subtle border border-border rounded px-2 py-1 hover:border-accent disabled:opacity-40 transition-colors"
            >
              {uploading ? "…" : "Upload"}
            </button>
          </div>
        )}
      </div>
      {/* How the picked image is used: only when an image is set and this
          slot supports modes. Kept as-is is the default. */}
      {onSetRefMode && hasSomething && !skipped && !isVideo && (
        <div className="mt-2 pl-12">
          <div className="inline-flex rounded-md border border-border overflow-hidden">
            {REF_MODE_CHOICES.map(([val, lbl, tip], i) => (
              <button
                key={val}
                type="button"
                onClick={() => onSetRefMode(val)}
                disabled={disabled}
                aria-pressed={refMode === val}
                title={tip}
                className={`text-[10.5px] px-2.5 py-1 transition-colors disabled:opacity-40 ${
                  refMode === val
                    ? "bg-accent/15 text-accent"
                    : "bg-bg-subtle text-muted hover:text-fg"
                } ${i > 0 ? "border-l border-border" : ""}`}
              >
                {lbl}
              </button>
            ))}
          </div>
          <p className="text-3xs text-muted mt-1 leading-snug">{refModeHint(refMode)}</p>
          {/* A kept card's words: swapped for the script's when they differ,
              unless the card keeps its own. */}
          {onSetKeepText && refMode === "as_is" && (
            <>
              <label className="mt-1.5 flex items-center gap-1.5 text-[10.5px] text-fg cursor-pointer">
                <input
                  type="checkbox"
                  checked={!!slot.keepText}
                  onChange={(e) => onSetKeepText(e.target.checked)}
                  disabled={disabled}
                  className="rounded border-border"
                />
                <span>Keep card text</span>
              </label>
              <p className="text-3xs text-muted mt-0.5 leading-snug">{keepTextHint(!!slot.keepText)}</p>
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** Single-row segmented engine toggle for the Engines collapsible. */
/** Per-scene character or background picker row (multi-scene Look & feel
 *  grid). Compact one-line layout: [scene badge] [thumb] [label] [actions].
 *  Filled state offers a "↓ apply to all" link that broadcasts this scene's
 *  pick to every other scene in the same column. */
function SceneSlotRow({
  sceneIdx,
  kind,
  override,
  uploading,
  disabled,
  fileUrlFor,
  onPick,
  onUpload,
  onClear,
  onCopyToAll,
  onSetBgMode,
}: {
  sceneIdx: number;
  kind: "character" | "bg";
  override: SceneOverride;
  uploading: boolean;
  disabled: boolean;
  fileUrlFor: (a: ProducerAssetRow) => string;
  onPick: () => void;
  onUpload: (f: File) => void;
  onClear: () => void;
  onCopyToAll: () => void;
  /** bg-only, edit-mode: toggle a fresh AI background for this scene (keeps the
   *  character). Omitted → no toggle shown. */
  onSetBgMode?: (mode: "keep" | "new") => void;
}) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const asset = kind === "character" ? override.character_asset : override.bg_asset;
  const upload = kind === "character" ? override.character_upload : override.bg_upload;
  const hasSomething = !!asset || !!upload;
  const previewUrl = asset
    ? fileUrlFor(asset)
    : upload?.url ?? null;
  const previewLabel = asset?.display_name
    ?? upload?.token?.slice(0, 10)
    ?? "(empty)";
  return (
    <div className="rounded-md border border-border bg-bg p-2">
      <div className="flex items-center gap-2">
        <span
          className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-fg text-bg text-3xs font-mono shrink-0"
          title={`Scene ${sceneIdx + 1}`}
        >
          {sceneIdx + 1}
        </span>
        <div className="w-9 h-9 rounded bg-bg-muted border border-border shrink-0 overflow-hidden flex items-center justify-center">
          {hasSomething && previewUrl ? (
            <img
              src={previewUrl}
              alt={previewLabel}
              className="w-full h-full object-cover"
            />
          ) : (
            <span className="text-[16px] text-muted/60">+</span>
          )}
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-[10.5px] text-fg truncate leading-tight">
            {hasSomething ? previewLabel : (
              <span className="text-muted">(empty)</span>
            )}
          </p>
          {hasSomething && (
            <button
              type="button"
              onClick={onCopyToAll}
              disabled={disabled}
              className="text-[9.5px] text-muted hover:text-accent disabled:opacity-40 transition-colors leading-tight mt-0.5"
              title="Apply this pick to every scene in this column"
            >
              ↓ apply to all
            </button>
          )}
        </div>
        {hasSomething ? (
          <button
            type="button"
            onClick={onClear}
            disabled={disabled}
            className="text-[10.5px] text-muted hover:text-fg disabled:opacity-40 transition-colors shrink-0 w-5 h-5 rounded hover:bg-bg-muted flex items-center justify-center"
            title="Clear"
          >
            ✕
          </button>
        ) : (
          <div className="flex items-center gap-1 shrink-0">
            {kind === "bg" && onSetBgMode && (
              <button
                type="button"
                onClick={() =>
                  onSetBgMode(override.bg_mode === "new" ? "keep" : "new")
                }
                disabled={disabled}
                aria-pressed={override.bg_mode === "new"}
                className={`text-3xs rounded px-1.5 py-0.5 border transition-colors disabled:opacity-40 ${
                  override.bg_mode === "new"
                    ? "bg-accent/15 border-accent text-accent"
                    : "bg-bg-subtle border-border text-muted hover:text-fg"
                }`}
                title={
                  override.bg_mode === "new"
                    ? "A fresh background will be generated for this scene (character kept)"
                    : "No change — the current still passes through untouched"
                }
              >
                ↻ New bg
              </button>
            )}
            <button
              type="button"
              onClick={onPick}
              disabled={uploading || disabled}
              className="text-3xs text-fg bg-bg-subtle border border-border rounded px-1.5 py-0.5 hover:border-accent disabled:opacity-40 transition-colors"
            >
              Pick
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) onUpload(f);
                if (fileInputRef.current) fileInputRef.current.value = "";
              }}
              className="hidden"
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading || disabled}
              className="text-3xs text-fg bg-bg-subtle border border-border rounded px-1.5 py-0.5 hover:border-accent disabled:opacity-40 transition-colors"
            >
              {uploading ? "…" : "Up"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

/** Compact preview tile (~200 px wide) for both image cards and the
 *  main keyframe. Header pill shows status; feedback textarea + retry
 *  button below the image. */
function PreviewTile({
  title,
  modelLabel,
  card,
  externalError,
  excluded = false,
  feedback,
  setFeedback,
  onRetry,
  onRegenerate,
  onUndo,
  canUndo = false,
  busy,
  busyLabel,
  prominent = false,
  aspectClass = "aspect-[9/16]",
  videoAspect = "9:16",
  quality,
  onQualityChange,
  directVideoUrl,
  framing,
  pageBusy = false,
  asIsPhoto = false,
  pickUrl = null,
  onFramingChange,
  framingWarning = null,
  clip = null,
  clipStale = false,
  imageCost,
}: {
  title: string;
  modelLabel: YasImageModel;
  card: TalkingHeadCard | null | undefined;
  /** Error pulled from run.{intro,outro,main}_error — populated when the
   *  generator threw and no card row was ever inserted. Wins over the
   *  "empty" default so silent failures stop being invisible. */
  externalError?: string | null;
  /** Bookend skipped via the Generate toggle — show a "Skipped" body, no
   *  spinner, no refine controls. */
  excluded?: boolean;
  feedback: string;
  setFeedback: (t: string) => void;
  /** Cards: Retry with the text. A still tile (onRegenerate given): Refine,
   *  the ONE change in the text made on the current still. */
  onRetry: () => void;
  /** Still tiles only: make the still again the way first generation did
   *  (no text; offered on a failed tile too). Its presence turns the tile's
   *  controls into Refine / Regenerate / Undo. */
  onRegenerate?: () => void;
  /** Still tiles only: put back the version before the last change. */
  onUndo?: () => void;
  canUndo?: boolean;
  busy: boolean;
  /** The overlay text while busy (default "Regenerating…"). */
  busyLabel?: string;
  prominent?: boolean;
  aspectClass?: string;
  /** The video's size. The still is shown whole in its true shape (never
   *  cropped to the tile), so what the user approves is what Veo animates;
   *  a still of another shape (made before stills were conformed) says so. */
  videoAspect?: TalkingHeadAspect;
  /** Per-still Veo tier — renders the compact Normal/Quality toggle when
   *  onQualityChange is provided (Stills stage only). */
  quality?: "normal" | "quality";
  onQualityChange?: (q: "normal" | "quality") => void;
  /** Direct-video bookend (intro/outro): the user uploaded a finished clip
   *  that's stitched in as-is — there's no card to generate, so render the
   *  uploaded video here and hide the refine / Veo-quality controls. */
  directVideoUrl?: string | null;
  /** Still tiles only: the framing buttons (lib/thFraming). The still's
   *  framing is lit; a closer one crops the still (free), a wider one has it
   *  re-shot (paid; framingButtonTitle says which). Off for a still
   *  that is the character photo ("Framing as in your photo"). */
  framing?: StillFramingState;
  /** Something blocking is running on the page (another tile's change, a
   *  re-shoot or the videos being polled): every button waits, so nothing
   *  stops that polling or runs twice. */
  pageBusy?: boolean;
  /** Still tiles only: the still is the character photo itself (as-is), so
   *  Regenerate is off and says how to get a new one. */
  asIsPhoto?: boolean;
  /** Card tiles only: the picked image, shown while no card is made from it
   *  (the server has no picture for a pick that is not a card yet). */
  pickUrl?: string | null;
  onFramingChange?: (f: TalkingHeadFraming) => void;
  /** Still tiles only: a re-shot still whose face shows another framing
   *  than the one asked for says so. */
  framingWarning?: string | null;
  /** Card tiles only: the clip made from the card (and whether it is out of
   *  date), so the tile says what its motion turned out to be. */
  clip?: TalkingHeadAnimation | null;
  clipStale?: boolean;
  /** Still tiles only: what one image costs now (lib/thStill stillImageCost). */
  imageCost?: string;
}) {
  const directVideo = !!directVideoUrl;
  const url = card?.url || null;
  const cardErr = (card as any)?.error;
  const err = cardErr || externalError || null;
  // A pick with no card made from it is waiting to be made, not being made.
  const tileStatus = previewTileStatus(card, err);
  const notMade = tileStatus === "not_made";
  const status: "ready" | "generating" | "failed" | "empty" = notMade ? "empty" : tileStatus;
  const skipped = !!excluded;
  const shapeOff =
    status === "ready" && !directVideo && !skipped &&
    stillShapeOff(card?.width, card?.height, videoAspect);
  // A still tile (main / scene): Refine needs a still and one change;
  // Regenerate is always offered, so a failed still can be made again.
  const stillMode = !!onRegenerate;
  // A card tile (intro / outro): kept as-is or drawn, its text and motion,
  // and a button that works without a note (lib/thBookends).
  const cardNote = cardTileNote(stillMode ? null : card, clip, clipStale);
  const cardWords = cardWordsNote(stillMode ? null : card);
  const cardButton = cardRetryButton(feedback, cardNote.needsMaking || status !== "ready");
  const stillButtons = stillTileButtons({
    hasStill: status === "ready" && !!url,
    busy: busy || status === "generating",
    pageBusy,
    change: feedback,
    canUndo: canUndo && !!onUndo,
    asIsPhoto,
    cost: imageCost,
  });
  return (
    <div
      className={[
        "rounded-xl overflow-hidden flex flex-col bg-bg-subtle/40 max-w-[220px] mx-auto w-full transition-opacity",
        prominent ? "border-2 border-accent/40 shadow-soft" : "border border-border",
        skipped ? "opacity-55" : "",
      ].join(" ")}
    >
      <div className="px-2 py-1.5 flex items-center justify-between gap-1 border-b border-border/60 bg-bg-subtle">
        <span
          className={[
            "text-3xs font-mono uppercase tracking-wider2",
            prominent ? "text-accent" : "text-muted",
          ].join(" ")}
        >
          {title}
          {!directVideo && card?.source === "asset" && <span className="ml-1 text-success">· asset</span>}
          {!directVideo && card?.source === "upload" && <span className="ml-1 text-warning">· upload</span>}
        </span>
        {directVideo ? (
          <span className="text-[9px] font-mono uppercase tracking-wider2 text-success px-1.5 py-0.5 rounded bg-success/10">
            your video
          </span>
        ) : skipped ? (
          <span className="text-[9px] font-mono uppercase tracking-wider2 text-muted px-1.5 py-0.5 rounded bg-bg-muted">
            skipped
          </span>
        ) : (
          <StatusPill status={status} />
        )}
      </div>
      <div className={`${aspectClass} bg-black flex items-center justify-center overflow-hidden relative`}>
        {directVideo ? (
          <video
            src={directVideoUrl!}
            muted
            loop
            playsInline
            controls
            className="w-full h-full object-cover"
          />
        ) : skipped ? (
          <p className="text-[10.5px] text-muted p-3 text-center">
            Skipped — won't be generated
          </p>
        ) : status === "ready" && url ? (
          <img src={url} alt={title} className="w-full h-full object-contain" />
        ) : status === "failed" ? (
          <p className="text-2xs text-danger p-3 text-center">{err}</p>
        ) : notMade && pickUrl ? (
          <img src={pickUrl} alt={`${title} pick`} className="w-full h-full object-contain opacity-60" />
        ) : notMade ? (
          <p className="text-[10.5px] text-muted p-3 text-center">Not made yet</p>
        ) : status === "empty" ? (
          <p className="text-[10.5px] text-muted p-3 text-center">
            (auto-generated)
          </p>
        ) : (
          <div className="text-center text-2xs text-muted space-y-1.5">
            <div className="w-5 h-5 mx-auto rounded-full border-2 border-accent border-t-transparent animate-spin" />
            <p>Generating…</p>
          </div>
        )}
        {/* v2 editor — regen overlay: keeps the OLD still visible underneath
            while the new one renders (image apply or manual retry). */}
        {busy && (
          <div className="absolute inset-0 bg-black/55 backdrop-blur-[1px] flex flex-col items-center justify-center gap-1.5 text-2xs text-white">
            <div className="w-5 h-5 rounded-full border-2 border-accent border-t-transparent animate-spin" />
            <p>{busyLabel ?? "Regenerating…"}</p>
          </div>
        )}
      </div>
      <div className="p-1.5 space-y-1 bg-bg-subtle">
        {directVideo && (
          <p className="text-3xs text-muted text-center leading-snug px-1">
            Your uploaded video — used as-is.
          </p>
        )}
        {shapeOff && (
          <p
            className="text-3xs text-warning text-center leading-snug px-1"
            title={`This still is ${card?.width}×${card?.height}. The video is ${videoAspect}, so it is fitted to that shape before it is animated.`}
          >
            Not {videoAspect}: fitted for the video
          </p>
        )}
        {!skipped && !directVideo && stillMode && (
          <>
            {stillButtons.showRefine && (
              <textarea
                rows={2}
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
                placeholder={STILL_REFINE_PLACEHOLDER}
                maxLength={500}
                className="w-full rounded border border-border bg-bg text-[10.5px] px-1.5 py-1 resize-none focus:outline-none focus:border-accent"
              />
            )}
            <div className="flex gap-1">
              {stillButtons.showRefine && (
                <button
                  type="button"
                  onClick={onRetry}
                  disabled={!stillButtons.refineEnabled}
                  className="flex-1 text-[10.5px] py-1 rounded border border-border bg-bg hover:bg-bg-muted text-fg disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  title={stillButtons.refineTitle}
                >
                  Refine
                </button>
              )}
              <button
                type="button"
                onClick={onRegenerate}
                disabled={!stillButtons.regenerateEnabled}
                className="flex-1 text-[10.5px] py-1 rounded border border-border bg-bg hover:bg-bg-muted text-fg disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                title={stillButtons.regenerateTitle}
              >
                Regenerate
              </button>
            </div>
            {stillButtons.regenerateHint && (
              <p className="text-3xs text-muted text-center leading-snug px-1">{stillButtons.regenerateHint}</p>
            )}
            {status === "ready" && err && (
              <p className="text-3xs text-danger text-center leading-snug px-1 break-words" title={err}>
                {STILL_KEPT_AFTER_ERROR}
              </p>
            )}
            {stillButtons.showUndo && (
              <button
                type="button"
                onClick={onUndo}
                disabled={!stillButtons.undoEnabled}
                className="w-full text-[10px] text-muted hover:text-fg underline underline-offset-2 disabled:opacity-40 disabled:cursor-not-allowed"
                title={UNDO_TITLE}
              >
                Undo last change
              </button>
            )}
            {framing?.supported && onFramingChange && (
              <div className="space-y-0.5">
                <div
                  className="inline-flex w-full rounded border border-border bg-bg p-0.5"
                  role="group"
                  aria-label={`Framing of the ${title.toLowerCase()} still`}
                >
                  {FRAMING_CHOICES.map((c) => {
                    const on = framing.current === c.value;
                    return (
                      <button
                        key={c.value}
                        type="button"
                        onClick={() => onFramingChange(c.value)}
                        disabled={on || framing.disabled || pageBusy || busy || status === "generating"}
                        aria-pressed={on}
                        className={[
                          "flex-1 px-1 py-0.5 rounded text-[9px] font-medium uppercase tracking-wider2 transition-colors disabled:cursor-not-allowed",
                          on ? "bg-fg text-bg" : "text-muted hover:text-fg disabled:opacity-40",
                        ].join(" ")}
                        title={framingButtonTitle(c, framing, status === "ready" && !!url, imageCost)}
                      >
                        {c.short}
                      </button>
                    );
                  })}
                </div>
                {framing.hint && (
                  <p className="text-3xs text-muted text-center leading-snug px-1">{framing.hint}</p>
                )}
                {framingWarning && (
                  <p className="text-3xs text-warning text-center leading-snug px-1 break-words">{framingWarning}</p>
                )}
              </div>
            )}
          </>
        )}
        {!skipped && !directVideo && !stillMode && (
          <>
            {cardNote.needsMaking && (
              <p className="text-3xs text-warning text-center leading-snug px-1">
                No card from this pick yet
              </p>
            )}
            {cardNote.kept && (
              <p
                className="text-3xs text-success text-center leading-snug px-1"
                title={
                  card?.text_swap?.status === "swapped"
                    ? "This card is your picked image with only the words below swapped (every other pixel kept)"
                    : "This card is your picked image, pixel for pixel (fitted to the video size)"
                }
              >
                Kept as-is
              </p>
            )}
            {cardNote.textLine && (
              <p className="text-3xs text-muted text-center leading-snug px-1 break-words" title={cardNote.textLine}>
                {cardNote.textLine}
              </p>
            )}
            {cardNote.motionLine && (
              <p className="text-3xs text-muted text-center leading-snug px-1">{cardNote.motionLine}</p>
            )}
            {cardWords.swapLine && (
              <p className="text-3xs text-muted text-center leading-snug px-1 break-words" title={cardWords.swapLine}>
                {cardWords.swapLine}
              </p>
            )}
            {cardWords.warning && (
              <p className="text-3xs text-warning text-center leading-snug px-1 break-words">{cardWords.warning}</p>
            )}
            {cardWords.clipLine && (
              <p className="text-3xs text-success text-center leading-snug px-1">{cardWords.clipLine}</p>
            )}
            {/* A kept card is the picked image: a note has nothing to change
                on it. A drawn card takes an optional note. */}
            {!cardNote.kept && (
              <textarea
                rows={2}
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
                placeholder={`Note for the ${title.toLowerCase()} card (optional)…`}
                className="w-full rounded border border-border bg-bg text-[10.5px] px-1.5 py-1 resize-none focus:outline-none focus:border-accent"
              />
            )}
            <button
              type="button"
              onClick={onRetry}
              disabled={busy || pageBusy}
              className="w-full text-[10.5px] py-1 rounded border border-border bg-bg hover:bg-bg-muted text-fg disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              title={cardButton.title}
            >
              {busy ? "Making…" : cardButton.label}
            </button>
          </>
        )}
        {!skipped && !directVideo && onQualityChange && (
          <div className="inline-flex w-full rounded border border-border bg-bg p-0.5">
            {(["normal", "quality"] as const).map((q) => (
              <button
                key={q}
                type="button"
                onClick={() => onQualityChange(q)}
                className={[
                  "flex-1 px-1 py-0.5 rounded text-[9px] font-medium uppercase tracking-wider2 transition-colors",
                  (quality ?? "normal") === q ? "bg-fg text-bg" : "text-muted hover:text-fg",
                ].join(" ")}
                title={q === "quality" ? "Premium Veo model — sharper, more stable faces (higher cost)" : "Fast Veo model (default)"}
              >
                {q === "normal" ? "Normal" : "Quality"}
              </button>
            ))}
          </div>
        )}
        <p className="text-[9.5px] text-muted/70 font-mono uppercase tracking-wider2 text-center">
          {skipped || directVideo ? "—" : modelLabel === "openai" ? "OpenAI" : "Nano Banana"}
        </p>
      </div>
    </div>
  );
}

/** Compact video tile — mirrors PreviewTile shape but renders a video
 *  element. An intro/outro tile says how its clip moves under it
 *  (motionLine): what the clip became, else the card's current plan, never
 *  the style an older clip was started with. */
function VideoTile({
  title,
  anim,
  prominent = false,
  feedback,
  setFeedback,
  onRetry,
  busy,
  retryAllowed,
  stale = false,
  excluded = false,
  onToggleExclude,
  selectable = false,
  selected = false,
  onToggleSelect,
  aspectClass = "aspect-[9/16]",
  directVideoUrl,
  waitingFor = null,
  motionLine = null,
}: {
  title: string;
  anim: {
    status: string;
    url: string | null;
    error: string | null;
    cost_usd: number | null;
    duration_sec: number | null;
  } | null;
  aspectClass?: string;
  /** Direct-video bookend (intro/outro): user-uploaded clip stitched in
   *  as-is. Render it here; there's no Veo clip to retry or exclude. */
  directVideoUrl?: string | null;
  prominent?: boolean;
  feedback?: string;
  setFeedback?: (t: string) => void;
  onRetry?: (forceNoFeedback?: boolean) => void;
  busy?: boolean;
  retryAllowed?: boolean;
  /** v2 editor — the source image/dialogue/refs changed after this clip rendered. */
  stale?: boolean;
  /** v2 editor — excluded from the final stitch. */
  excluded?: boolean;
  /** v2 editor — toggle exclude (omit for non-removable clips, e.g. single main). */
  onToggleExclude?: () => void;
  /** v2 editor — batch-select mode. */
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: () => void;
  /** An intro/outro whose card is missing: no clip can be made from it
   *  yet (a retry would animate the bare pick), so no retry, and this says
   *  why. */
  waitingFor?: string | null;
  /** An intro/outro clip: what it turned out to be (lib/thBookends
   *  cardTileNote), e.g. the slow zoom that replaced a Veo clip. */
  motionLine?: string | null;
}) {
  // Retry surface widened: on `ready` it's the legacy "refine after
  // success" path (gated on retryAllowed + setFeedback, requires
  // feedback text). On `failed` it's a recovery path — always allowed,
  // no feedback required, no compositionStatus gate. Without this, a
  // transient Veo failure ("high load, try again later") leaves the
  // user with a red tile and no actionable button.
  // A 'ready' tile whose <video> src fails to load (e.g. a genuine 410 when
  // the file is gone from both disk and bucket) must surface visibly rather
  // than render a blank box under a green "Ready" pill. Track that per-tile
  // and reset whenever the source changes.
  const [mediaError, setMediaError] = useState(false);
  useEffect(() => {
    setMediaError(false);
  }, [anim?.url]);
  const directVideo = !!directVideoUrl;
  const canRetry =
    !directVideo &&
    !waitingFor &&
    !!onRetry &&
    (mediaError || stale || anim?.status === "ready" || anim?.status === "failed") &&
    (mediaError ||
      stale ||
      anim?.status === "failed" ||
      (retryAllowed === true && !!setFeedback));
  const status: "ready" | "generating" | "failed" | "empty" =
    anim?.status === "ready" && anim.url
      ? "ready"
      : anim?.status === "failed"
        ? "failed"
        : anim?.status === "generating"
          ? "generating"
          : "empty";
  // A media load failure on an otherwise-ready tile reads as "failed" in the UI.
  const displayStatus: "ready" | "generating" | "failed" | "empty" =
    status === "ready" && mediaError ? "failed" : status;
  return (
    <div
      className={[
        "relative rounded-xl overflow-hidden flex flex-col bg-bg-subtle/40 max-w-[220px] mx-auto w-full transition-opacity",
        prominent ? "border-2 border-accent/40 shadow-soft" : "border border-border",
        excluded ? "opacity-40" : "",
        selected ? "ring-2 ring-accent" : "",
      ].join(" ")}
    >
      {selectable && (
        <button
          type="button"
          onClick={onToggleSelect}
          title={selected ? "Deselect" : "Select for batch regen"}
          className="absolute top-1.5 left-1.5 z-10 w-5 h-5 rounded border border-border bg-bg/90 flex items-center justify-center text-3xs text-accent"
        >
          {selected ? "✓" : ""}
        </button>
      )}
      <div className="px-2 py-1.5 flex items-center justify-between gap-1 border-b border-border/60 bg-bg-subtle">
        <span
          className={[
            "text-3xs font-mono uppercase tracking-wider2",
            prominent ? "text-accent" : "text-muted",
          ].join(" ")}
        >
          {title}
        </span>
        <div className="flex items-center gap-1">
          {!directVideo && stale && !excluded && (
            <span
              className="text-[8.5px] font-mono uppercase px-1 py-0.5 rounded bg-warning/20 text-amber-600"
              title="Out of date — regenerate before finalizing"
            >
              stale
            </span>
          )}
          {directVideo ? (
            <span className="text-[9px] font-mono uppercase tracking-wider2 text-success px-1.5 py-0.5 rounded bg-success/10">
              your video
            </span>
          ) : (
            <StatusPill status={displayStatus} />
          )}
          {!directVideo && onToggleExclude && (
            <button
              type="button"
              onClick={onToggleExclude}
              title={excluded ? "Include in the final reel" : "Exclude from the final reel"}
              className="w-4 h-4 rounded-full bg-bg-muted hover:bg-danger/20 text-2xs leading-none flex items-center justify-center text-muted hover:text-danger"
            >
              {excluded ? "+" : "×"}
            </button>
          )}
        </div>
      </div>
      <div className={`${aspectClass} bg-black flex items-center justify-center overflow-hidden`}>
        {directVideo ? (
          <video
            src={directVideoUrl!}
            controls
            playsInline
            className="w-full h-full object-cover"
          />
        ) : excluded && displayStatus !== "ready" ? (
          <p className="text-[10.5px] text-muted p-3 text-center">
            Skipped — not in the final video
          </p>
        ) : displayStatus === "ready" && anim?.url ? (
          <video
            src={anim.url}
            controls
            playsInline
            onError={() => setMediaError(true)}
            className="w-full h-full object-cover"
          />
        ) : displayStatus === "failed" ? (
          <p className="text-2xs text-danger p-3 text-center">
            {mediaError
              ? "Couldn't load this video (the file may be unavailable). Retry to regenerate."
              : anim?.error || "Generation failed"}
          </p>
        ) : displayStatus === "generating" ? (
          <div className="text-center text-2xs text-muted space-y-1.5">
            <div className="w-5 h-5 mx-auto rounded-full border-2 border-accent border-t-transparent animate-spin" />
            <p>Veo generating…</p>
          </div>
        ) : (
          <p className="text-[10.5px] text-muted">(waiting)</p>
        )}
      </div>
      <div className="px-2 py-1 flex items-center justify-between bg-bg-subtle text-[9.5px] font-mono uppercase tracking-wider2 text-muted/80">
        {directVideo ? (
          <>
            <span>your upload</span>
            <span>used as-is</span>
          </>
        ) : (
          <>
            <span>{anim?.duration_sec ? `${anim.duration_sec}s` : "—"}</span>
            <span>{anim?.cost_usd != null ? `$${anim.cost_usd.toFixed(2)}` : ""}</span>
          </>
        )}
      </div>
      {!directVideo && motionLine && (
        <p className="px-2 py-1 text-3xs text-muted text-center leading-snug border-t border-border/60 bg-bg-subtle">
          {motionLine}
        </p>
      )}
      {!directVideo && waitingFor && (
        <p className="p-1.5 text-3xs text-warning text-center leading-snug border-t border-border/60 bg-bg-subtle">
          {waitingFor}
        </p>
      )}
      {canRetry && (
        <div className="p-1.5 space-y-1 border-t border-border/60 bg-bg-subtle">
          {setFeedback && (
            <textarea
              rows={2}
              value={feedback ?? ""}
              onChange={(e) => setFeedback(e.target.value)}
              placeholder={
                anim?.status === "failed" || mediaError
                  ? "Optional: describe a change, or leave empty to retry as-is…"
                  : `Refine ${title.toLowerCase()}…`
              }
              className="w-full rounded border border-border bg-bg text-[10.5px] px-1.5 py-1 resize-none focus:outline-none focus:border-accent"
            />
          )}
          <button
            type="button"
            onClick={() => onRetry?.(mediaError || stale)}
            disabled={
              !!busy ||
              (anim?.status !== "failed" &&
                !mediaError &&
                !stale &&
                !(feedback ?? "").trim())
            }
            className="w-full text-[10.5px] py-1 rounded border border-border bg-bg hover:bg-bg-muted text-fg disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            {busy
              ? "Regenerating…"
              : stale
                ? "Regenerate (out of date)"
                : "Retry video"}
          </button>
        </div>
      )}
    </div>
  );
}

/** Small coloured status pill shown in tile headers. */
function StatusPill({ status }: { status: "ready" | "generating" | "failed" | "empty" }) {
  const cfg = {
    ready: { label: "Ready", cls: "bg-success/15 text-emerald-600" },
    generating: { label: "…", cls: "bg-warning/15 text-amber-600" },
    failed: { label: "Failed", cls: "bg-danger/15 text-danger" },
    empty: { label: "—", cls: "bg-bg-muted text-muted" },
  }[status];
  return (
    <span
      className={[
        "text-[9.5px] px-1.5 py-0.5 rounded-full font-mono uppercase tracking-wider2",
        cfg.cls,
      ].join(" ")}
    >
      {cfg.label}
    </span>
  );
}
