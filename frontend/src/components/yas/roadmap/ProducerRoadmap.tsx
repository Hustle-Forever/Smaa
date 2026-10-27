/**
 * Global Producer Roadmap orchestrator.
 *
 * Owns the master 5-step workflow state (Producer → Editor → Preview → Calendar
 * → Approval), the chosen tool, and the finalized asset that flows between steps.
 * Renders the GlobalStepper on top and the active step's content beneath it.
 *
 * - Step 1 "Producer": the tool selection grid.
 * - Step 2 "Editor": the chosen creator (its OWN local sub-stepper runs beneath
 *   the frozen global "Editor" chip). The creator calls `onContinue` when the
 *   user finalizes one asset.
 * - Steps 3-5: Preview (RAG caption), Calendar (schedule), Approval (publish).
 *
 * The Video Editor is a first-class roadmap tool: it mounts in the Editing stage
 * and its "Finish & continue →" (export) advances to Finalization, like the rest.
 */
import { useEffect, useRef, useState } from "react";
import { Link2 } from "lucide-react";

import type { Tab } from "../../../App";
import { PRODUCER_BRIEF_EVENT, clearProducerBrief, peekProducerBrief, type ProducerBrief } from "../../../lib/producerBrief";
import { PRODUCER_HUB_HASH, producerResumeHash, producerToolHash, replaceHash } from "../../../lib/producerHash";
import { useCopyLink } from "../../../lib/useCopyLink";
import { roadmapApi } from "./api";
import { AnimationCreator } from "../AnimationCreator";
import { BrollCreator } from "../BrollCreator";
import { GeneralVideoCreator } from "../GeneralVideoCreator";
import { HumanCharacterCreator } from "../HumanCharacterCreator";
import { PosterCreator } from "../PosterCreator";
import { PosterHumanCreator } from "../PosterHumanCreator";
import { TalkingHeadV2Creator } from "../TalkingHeadV2Creator";
import { VideoEditorCreator } from "../video-editor/VideoEditorCreator";
import { GlobalStepper } from "./GlobalStepper";
import { PreviewStep } from "./PreviewStep";
import { CalendarStep } from "./CalendarStep";
import { ApprovalStep } from "./ApprovalStep";
import {
  GLOBAL_STEP_ORDER,
  ROADMAP_TOOL_LABELS,
  type CarouselSlide,
  type ContinueToPreview,
  type FinalizedAsset,
  type GlobalStep,
  type RoadmapTool,
} from "./types";

// ONE SHORT SENTENCE EACH, IN WORDS SOMEBODY WOULD SAY OUT LOUD.
// These used to be pipeline notation: arrows, tool names, durations and file
// concepts ("static frame (logo baked in)", "multi-track timeline", "Veo 3",
// "B-roll"). That told a reader who already knew the system what it does, and
// told everybody else nothing. A card here answers one question, "what comes
// out of this?", because the reader is choosing, not operating.
const HUB_CARDS = [
  { key: "poster", label: "Poster", color: "bg-blue-500/80", desc: "A designed picture. No people in it." },
  { key: "animation", label: "Animation", color: "bg-orange-500/80", desc: "A poster that moves, a few seconds long." },
  { key: "poster_human", label: "Poster + Human", color: "bg-success/80", desc: "A designed picture with people in it." },
  { key: "human_character", label: "Human Character", color: "bg-rose-500/80", desc: "Make a person you can use again in videos." },
  { key: "talking_head_v2", label: "Human Element Video", color: "bg-fuchsia-500/80", desc: "A short video of someone talking." },
  { key: "video_editor", label: "Video Editor", color: "bg-cyan-500/80", desc: "Cut and join clips into one video." },
] as const;

// The hub splits into "create new content" tools (the centered grid) and the
// Video Editor — an "edit existing footage" action shown as its own full-width
// card beneath the grid, so it reads as a distinct kind of action.
const CREATE_CARDS = HUB_CARDS.filter((c) => c.key !== "video_editor");
const VIDEO_EDITOR_CARD = HUB_CARDS.find((c) => c.key === "video_editor")!;

// "General video" is a CATEGORY card (not a tool itself). Clicking it opens a
// sub-chooser with two dedicated, non-editor tools.
const GENERAL_VIDEO_SUBCARDS = [
  {
    key: "general_video",
    label: "General AI Video",
    color: "bg-teal-500/80",
    desc: "Describe a video and we make it for you.",
  },
  {
    key: "broll",
    label: "B-roll",
    color: "bg-warning/80",
    desc: "Drop in your own clips, we join them up.",
  },
] as const;

// localStorage keys each creator uses to recover an in-progress draft/generation.
// Wiped when starting a NEW project of that tool so the slate is truly blank
// (talking_head_v2 / poster_human / video_editor keep no such state → omitted).
const TOOL_DRAFT_KEYS: Partial<Record<RoadmapTool, string[]>> = {
  poster: [
    "yas.poster.topic", "yas.poster.refs", "yas.poster.imageUsage",
    "yas.poster.style", "yas.poster.batchStack",
  ],
  animation: [
    "yas.animation.prompt", "yas.animation.aspect", "yas.animation.style",
    "yas.animation.duration", "yas.animation.withAudio", "yas.animation.batchStack",
  ],
  human_character: ["yas.humanchar.topic", "yas.humanchar.aspect", "yas.humanchar.batch"],
};

/** Mirror the active Producer tool to the URL (replaceState, so switching tools
 *  doesn't spam the browser back-history) so a reload / copy-URL restores it.
 *  Video Editor writes its own project hash (#producer/video_editor:<id>), so
 *  it's never routed through here. Grammar lives in lib/producerHash. */
function writeToolHash(tool: RoadmapTool | null) {
  replaceHash(tool ? producerToolHash(tool) : PRODUCER_HUB_HASH);
}

/** Map a roadmap tool key to its Library type for building a resume hash. It's
 *  the identity for every tool EXCEPT human_character, which has no Library type
 *  (a save-to-Assets utility) → null means "never mirror it into the URL". */
function toolLibraryType(tool: RoadmapTool): string | null {
  return tool === "human_character" ? null : tool;
}

export function ProducerRoadmap({
  navigate,
  initialTool = null,
  initialRunId = null,
  initialStep = null,
  initialRunIds = null,
}: {
  navigate?: (tab: Tab, param?: string) => void;
  /** Phase C resume — open straight into this tool's editor, pre-loaded on
   *  `initialRunId` (a Library "Continue editing" deep-link). */
  initialTool?: RoadmapTool | null;
  initialRunId?: number | null;
  /** Wizard sub-step to resume at (#producer/<type>:<id>@<step>) — forwarded to
   *  the Human Element Video / B-roll creator so an F5 lands on the exact sub-step. */
  initialStep?: string | null;
  /** Batch grid ids to seed on resume (#producer/<type>:<id1>+<id2>+…) —
   *  forwarded to Poster / Animation / Poster+Human so an F5 restores the grid. */
  initialRunIds?: number[] | null;
}) {
  // When resuming, boot directly into the chosen tool's editor (step 2) with
  // that tool already "visited" so its creator is mounted and hydrates by id.
  const [step, setStep] = useState<GlobalStep>(initialTool ? "editor" : "producer");
  const [tool, setTool] = useState<RoadmapTool | null>(initialTool);
  const [asset, setAsset] = useState<FinalizedAsset | null>(null);
  const [maxReached, setMaxReached] = useState(initialTool ? 1 : 0);
  // "Post now" path: when true, the Approval step publishes on arrival instead
  // of waiting for the user to click "Approve & Post".
  const [autoApprove, setAutoApprove] = useState(false);
  // Every tool the user has opened this session. Their creators stay mounted
  // (hidden unless active) so switching tools and coming back doesn't discard
  // an in-progress generation. Cleared only on a full reset (Approval → Done).
  const [visited, setVisited] = useState<Set<RoadmapTool>>(
    () => new Set(initialTool ? [initialTool] : []),
  );
  // Resume id is ONE-SHOT: it seeds the initial editor only. Cleared on a full
  // reset (Approval → Done) and on any manual tool pick, so a later remount of
  // the same tool starts clean instead of re-hydrating the old generation.
  const [resumeRunId, setResumeRunId] = useState<number | null>(initialRunId);
  // The Finalization caption draft lives HERE (not in PreviewStep) so it SURVIVES
  // leaving and returning to Finalization — it never auto-regenerates and never
  // gets lost. Cleared on a full reset, or when a DIFFERENT asset is finalized.
  const [draftCaption, setDraftCaption] = useState("");
  // Bumped on every "start a new project" so the active creator REMOUNTS clean
  // (its React `key`), instead of dropping the user back into the previous,
  // still-mounted project. Resume keeps it at 0 (no remount).
  const [projectNonce, setProjectNonce] = useState(0);
  // Trend Finder "Make this" hand-off. Drained during the FIRST RENDER (a pure
  // peek, no side effect) so the creator children mount already holding it. The
  // tool guard is what makes the window listener below safe: an instance that is
  // about to be replaced by the hash-driven remount has a different initialTool
  // and never claims the brief.
  const [brief, setBrief] = useState<ProducerBrief | null>(() => {
    const b = peekProducerBrief();
    return b && b.tool === initialTool ? b : null;
  });
  // True only while we fetch a resumed project's saved step from the backend, so
  // the persist effect doesn't write stale state before hydration lands.
  const [hydrating, setHydrating] = useState(initialTool != null && initialRunId != null);
  // The hub "General video" category card opens a two-tool sub-chooser in place
  // of the main tool grid.
  const [subChooser, setSubChooser] = useState(false);
  const copyLink = useCopyLink();

  const stepIndex = (s: GlobalStep) => GLOBAL_STEP_ORDER.indexOf(s);
  const bump = (i: number) => setMaxReached((m) => Math.max(m, i));
  const isPostFinalize = (s: GlobalStep) =>
    s === "preview" || s === "calendar" || s === "approval";

  // ----- Per-project persistence: resume a left-off roadmap from the Library --
  // On a resume mount (initialTool + initialRunId), restore the SAVED step +
  // asset + caption so the user lands back at Finalization/Scheduling/Approval —
  // not always at Editing. Runs once per mount (ResumeRouter remounts via key).
  useEffect(() => {
    let cancelled = false;
    if (!(initialTool && initialRunId != null)) {
      setHydrating(false);
      return;
    }
    roadmapApi
      .getState(initialTool, initialRunId)
      .then((s) => {
        if (cancelled) return;
        if (s?.step && s.state?.asset && isPostFinalize(s.step)) {
          setAsset(s.state.asset);
          setDraftCaption(s.state.draftCaption ?? "");
          setStep(s.step);
          setMaxReached(Math.max(1, stepIndex(s.step)));
        }
      })
      .catch(() => {/* no saved state → stay at editor */})
      .finally(() => {
        if (!cancelled) setHydrating(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Persist the current position (debounced) once an asset is finalized, keyed
  // by (tool, refId) so each project is independent and survives leaving.
  useEffect(() => {
    if (hydrating || !asset || !isPostFinalize(step)) return;
    const t = setTimeout(() => {
      roadmapApi
        .saveState({
          tool: asset.tool,
          ref_id: asset.refId,
          step,
          state: { asset, draftCaption },
        })
        .catch(() => {/* best-effort */});
    }, 500);
    return () => clearTimeout(t);
  }, [hydrating, step, asset, draftCaption]);

  // Clearing the brief slot is a side effect → an effect, not the initializer
  // above (StrictMode double-invokes initializers).
  useEffect(() => {
    if (brief) clearProducerBrief(brief.id);
  }, [brief]);

  // Already on this exact tool: the hand-off writes the SAME hash, so nothing
  // remounts and the window event is the only delivery. Bump projectNonce
  // ourselves (via startFreshProject) so the creator remounts seeded.
  useEffect(() => {
    const onBrief = (e: Event) => {
      const b = (e as CustomEvent<ProducerBrief>).detail;
      if (!b || b.tool !== initialTool) return; // not ours — let the remount take it
      clearProducerBrief(b.id);
      startFreshProject(b.tool); // wipes that tool's localStorage drafts + bumps projectNonce
      setResumeRunId(null);
      setVisited((s) => new Set(s).add(b.tool));
      setTool(b.tool);
      setStep("editor");
      bump(1);
      setBrief(b);
    };
    window.addEventListener(PRODUCER_BRIEF_EVENT, onBrief);
    return () => window.removeEventListener(PRODUCER_BRIEF_EVENT, onBrief);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialTool]);

  // Handed to exactly one creator, and only while it is still the brief's tool.
  const briefFor = (t: RoadmapTool) =>
    brief && brief.tool === t ? brief : undefined;

  // Visited creators stay MOUNTED but hidden (see the comment above the creator
  // wrappers below) so in-progress work survives tool switches — but the
  // `hidden` attribute is display:none, which does NOT pause HTML5 media. An
  // unmuted <video> the user played (an animated poster, a Human Element Video clip)
  // would keep sounding invisibly after "Use →" / back-to-hub / tool switches.
  // On every tool/step change, pause any video inside a hidden wrapper.
  const rootRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    rootRef.current
      ?.querySelectorAll<HTMLVideoElement>("[hidden] video")
      .forEach((v) => {
        if (!v.paused) v.pause();
      });
  }, [tool, step]);

  // ----- Deep-link mirroring: keep the URL in sync with the active tool's live
  // run id(s) + sub-step so an F5 / shared link reopens exactly here. A ref
  // accumulator (not state) collects whatever the active creator reports —
  // wizards report { runId, step }, batch tools report { ids } — and each report
  // rewrites the hash via replaceState (no history spam, no remount). The
  // accumulator resets whenever the active tool changes so a hidden, still-
  // mounted creator's late report can't clobber the new tool's URL.
  const dlRef = useRef<{ runId: number | null; ids: number[] | null; step: string | null }>({
    runId: null,
    ids: null,
    step: null,
  });
  function writeDeepLink() {
    if (!tool) return;
    const type = toolLibraryType(tool);
    if (!type) return; // human_character: never mirrored into the URL
    const d = dlRef.current;
    if (d.ids && d.ids.length) {
      replaceHash(producerResumeHash(type, d.ids)); // batch grid (no step)
    } else if (d.runId != null) {
      replaceHash(producerResumeHash(type, d.runId, d.step ?? undefined)); // wizard (+ optional step)
    }
  }
  // Factory callbacks handed to each creator, GUARDED on the active tool so a
  // hidden mounted creator firing late never overwrites the visible tool's URL.
  const onToolRunId = (key: RoadmapTool) => (id: number) => {
    if (key !== tool) return;
    dlRef.current.runId = id;
    dlRef.current.ids = null;
    writeDeepLink();
  };
  const onToolStep = (key: RoadmapTool) => (step: string) => {
    if (key !== tool) return;
    dlRef.current.step = step;
    writeDeepLink();
  };
  const onToolBatch = (key: RoadmapTool) => (ids: number[]) => {
    if (key !== tool) return;
    dlRef.current = { runId: null, ids, step: null };
    writeDeepLink();
  };
  // Reset the accumulator when the active tool changes (see comment above).
  useEffect(() => {
    dlRef.current = { runId: null, ids: null, step: null };
  }, [tool]);

  function goTo(s: GlobalStep) {
    if (stepIndex(s) <= maxReached) setStep(s);
  }

  function reset() {
    setStep("producer");
    setTool(null);
    setAsset(null);
    setMaxReached(0);
    setVisited(new Set()); // unmount all creators — true fresh start
    setResumeRunId(null); // drop the one-shot resume id
    setDraftCaption(""); // drop the caption draft
    setAutoApprove(false); // clear the "post now" flag
    setSubChooser(false); // back to the main tool grid
    writeToolHash(null); // finished/left the project → clear its deep link
  }

  // The editor's "back" returns to the hub but KEEPS the tool + the mounted
  // creator, so an in-progress generation isn't destroyed — re-opening the tool
  // (or the stepper's Editor chip) resumes it. Full wipe (`reset`) is reserved
  // for finishing the flow (Approval → Done).
  function backToHub() {
    setStep("producer");
    // Reflect the hub in the URL, but never clobber a resume/id deep link (those
    // carry a ":" — e.g. #producer/talking_head_v2:87 — and must survive an F5).
    if (!window.location.hash.includes(":")) writeToolHash(null);
  }

  // Starting a NEW project from the hub: drop the in-session asset/caption and
  // REMOUNT the creator clean (projectNonce). For tools that restore generation
  // state from localStorage (poster/animation), wipe those keys so the slate is
  // truly blank — the previous project stays saved in producer_roadmap, so it's
  // resumable from the Library, just not auto-reopened here.
  function startFreshProject(key: RoadmapTool) {
    for (const k of TOOL_DRAFT_KEYS[key] ?? []) {
      try {
        localStorage.removeItem(k);
      } catch {/* ignore */}
    }
    setAsset(null);
    setDraftCaption("");
    setMaxReached(1);
    // A brief is consumed by exactly ONE mount. projectNonce remounts every
    // creator, and it is bumped for reasons that have nothing to do with a
    // hand-off (any hub pick, "Create Video Editor"), so a brief left in state
    // would be re-seeded into a later, unrelated visit — overwriting whatever
    // the user had typed, and (via usePersistentState) saving that stale trend
    // topic over their draft. Drop it here, before the remount.
    // The hand-off path itself calls this and THEN setBrief(b), so the new
    // brief still lands: same handler, React batches, last write wins.
    setBrief(null);
    setProjectNonce((n) => n + 1);
  }

  function choose(key: RoadmapTool) {
    setSubChooser(false); // collapse the General-video sub-chooser if it was open
    if (key === "video_editor") {
      openVideoEditor(null); // fresh project — the editor shows its CreateProjectGate
      return;
    }
    startFreshProject(key);
    setResumeRunId(null); // a manual pick is a fresh start, not a resume
    setVisited((s) => new Set(s).add(key));
    setTool(key);
    setStep("editor");
    bump(1);
    writeToolHash(key); // reflect the open tool in the URL (shareable / F5-safe)
  }

  // Open the Video Editor in the Editing stage. `projectId` seeds it (a Talking
  // Head handoff, or a resume) — null means a fresh project (CreateProjectGate).
  function openVideoEditor(projectId: number | null) {
    if (projectId == null) startFreshProject("video_editor"); // hub "Create Video Editor"
    setResumeRunId(projectId);
    setVisited((s) => new Set(s).add("video_editor"));
    setTool("video_editor");
    setStep("editor");
    bump(1);
  }

  // "Animate" on a finished poster. The poster has already been imported as an
  // animation frame by then, so this just moves the user onto the Animation
  // tool with that frame in its grid: they pick a length, animate it, and carry
  // on through Finalization → Scheduling → Approval like any other animation.
  // Deliberately NOT its own little flow: the whole point is to land in the
  // one that already goes all the way to publish. Mirrors openVideoEditor.
  function openAnimation(animId: number) {
    startFreshProject("animation"); // wipe stale drafts + remount with the new id
    setResumeRunId(animId);
    setVisited((s) => new Set(s).add("animation"));
    setTool("animation");
    setStep("editor");
    bump(1);
  }

  // Approval → Done: the project is finished/left, so drop its saved roadmap row
  // (it leaves the Library's "Continue" list) and wipe the in-session state.
  function finishAndReset() {
    if (asset) roadmapApi.clearState(asset.tool, asset.refId).catch(() => {/* best-effort */});
    reset();
  }

  const finalize: ContinueToPreview = (a) => {
    if (!tool) return;
    // A genuinely different finished asset → drop the previous caption draft.
    // Re-finishing the SAME asset (e.g. an editor re-export) keeps it.
    if (asset && asset.refId !== a.refId) setDraftCaption("");
    setAsset({ tool, ...a });
    setStep("preview");
    bump(2);
  };

  function onCaptionConfirmed(
    caption: string,
    carouselItems?: CarouselSlide[],
    thumbOffsetMs?: number,
  ) {
    setAsset((a) => (a ? { ...a, caption, carouselItems, thumbOffsetMs } : a));
    setStep("calendar");
    bump(3);
  }

  function onScheduled(scheduleId: number, scheduledAt: string) {
    setAutoApprove(false); // scheduled for later — user approves on the due date
    setAsset((a) => (a ? { ...a, scheduleId, scheduledAt } : a));
    setStep("approval");
    bump(4);
  }

  function onPostNow(scheduleId: number, scheduledAt: string) {
    setAutoApprove(true); // Approval step publishes immediately on arrival
    setAsset((a) => (a ? { ...a, scheduleId, scheduledAt } : a));
    setStep("approval");
    bump(4);
  }

  // What to seed the Animation creator's grid with.
  //
  // Two sources, and they are not interchangeable. `resumeRunId` is current and
  // per-tool: a Library resume, or the frame the "Animate" hand-off just made.
  // `initialRunIds` is a full-grid deep link (#producer/animation:1+2+3) that
  // belongs to whatever tool the roadmap MOUNTED with, so it must never leak
  // into a tool the user reached later: after a hand-off from
  // #producer/poster:1+2+3, those are POSTER ids.
  function animationSeedIds(): number[] | null | undefined {
    if (tool !== "animation") return undefined;
    if (initialTool === "animation" && initialRunIds) return initialRunIds;
    return resumeRunId != null ? [resumeRunId] : null;
  }

  return (
    <div ref={rootRef} className="space-y-5">
      {/* Global roadmap rail (frozen on "Editor" while a creator is open). */}
      <div className="flex items-center gap-3 flex-wrap">
        <p className="eyebrow shrink-0">Roadmap</p>
        <GlobalStepper
          activeStep={step}
          maxReachedIndex={maxReached}
          editorLabel={tool ? `Editing: ${ROADMAP_TOOL_LABELS[tool]}` : undefined}
          onNavigate={goTo}
        />
        {/* Copy a shareable link to this project (only while one is open). */}
        {tool && (
          <button
            onClick={() => copyLink(window.location.href, "Reopens this project where you left off.")}
            title="Copy a shareable link to this project"
            aria-label="Copy link to this project"
            className="ml-auto inline-flex items-center gap-1.5 text-2xs font-medium text-muted hover:text-fg px-2 py-1 rounded-lg border border-border hover:border-border-strong transition"
          >
            <Link2 className="w-3 h-3" />
            Copy link
          </button>
        )}
      </div>

      {step === "producer" && (
        <div className="max-w-3xl mx-auto space-y-6">
          <h1 className="h1 text-center">
            {subChooser ? "General video" : "What do you want to create?"}
          </h1>

          {subChooser ? (
            <div className="space-y-4">
              <button onClick={() => setSubChooser(false)} className="btn btn-ghost text-sm">
                ← All tools
              </button>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {GENERAL_VIDEO_SUBCARDS.map((m) => (
                  <button
                    key={m.key}
                    onClick={() => choose(m.key)}
                    className="card card-hover text-left p-6 flex flex-col gap-3 group"
                  >
                    <div className="flex items-center gap-2">
                      <span className={`w-3 h-3 rounded-full ${m.color}`} />
                      <span className="font-semibold text-fg text-base">{m.label}</span>
                    </div>
                    <p className="text-sm text-muted leading-relaxed">{m.desc}</p>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <>
              {/* Create-new-content tools */}
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                {CREATE_CARDS.map((m) => (
                  <button
                    key={m.key}
                    onClick={() => choose(m.key)}
                    className="card card-hover text-left p-6 flex flex-col gap-3 group"
                  >
                    <div className="flex items-center gap-2">
                      <span className={`w-3 h-3 rounded-full ${m.color}`} />
                      <span className="font-semibold text-fg text-base">{m.label}</span>
                    </div>
                    <p className="text-sm text-muted leading-relaxed">{m.desc}</p>
                  </button>
                ))}
                {/* General video — a category that opens a two-tool sub-chooser. */}
                <button
                  onClick={() => setSubChooser(true)}
                  className="card card-hover text-left p-6 flex flex-col gap-3 group"
                >
                  <div className="flex items-center gap-2">
                    <span className="w-3 h-3 rounded-full bg-teal-500/80" />
                    <span className="font-semibold text-fg text-base">General video</span>
                  </div>
                  <p className="text-sm text-muted leading-relaxed">
                    A short video, made for you or built from your own clips.
                  </p>
                </button>
              </div>

              {/* Video Editor — its own full-width card under the grid (edit
                  existing footage, a distinct action from the create tools). */}
              <button
                onClick={() => choose(VIDEO_EDITOR_CARD.key)}
                className="card card-hover w-full text-left p-6 flex items-center gap-4 group"
              >
                <span className="w-11 h-11 rounded-xl bg-cyan-500/15 flex items-center justify-center shrink-0">
                  <span className={`w-3.5 h-3.5 rounded-full ${VIDEO_EDITOR_CARD.color}`} />
                </span>
                <div className="min-w-0">
                  <div className="font-semibold text-fg text-base">{VIDEO_EDITOR_CARD.label}</div>
                  <p className="text-sm text-muted leading-relaxed">{VIDEO_EDITOR_CARD.desc}</p>
                </div>
                <span className="ml-auto text-lg text-muted group-hover:text-fg transition-colors shrink-0">
                  →
                </span>
              </button>
            </>
          )}
        </div>
      )}

      {/* Every visited tool's creator stays mounted (hidden unless it's the
          active tool on the editor step) so switching tools — or the back
          button, or stepping to Preview — never resets an in-progress
          generation. Cleared only by a full reset (Approval → Done). */}
      {visited.has("poster") && (
        <div hidden={!(tool === "poster" && step === "editor")}>
          <PosterCreator
            key={`poster-${projectNonce}`}
            onBack={backToHub}
            onContinue={finalize}
            onBatch={onToolBatch("poster")}
            onAnimate={openAnimation}
            initialRunIds={initialTool === "poster" ? (initialRunIds ?? (resumeRunId != null ? [resumeRunId] : null)) : undefined}
            initialRunId={initialTool === "poster" ? resumeRunId : undefined}
            initialBrief={briefFor("poster")}
          />
        </div>
      )}
      {visited.has("animation") && (
        <div hidden={!(tool === "animation" && step === "editor")}>
          <AnimationCreator
            key={`animation-${projectNonce}`}
            onBack={backToHub}
            onContinue={finalize}
            onBatch={onToolBatch("animation")}
            initialRunIds={animationSeedIds()}
            initialRunId={tool === "animation" ? resumeRunId : undefined}
            initialBrief={briefFor("animation")}
          />
        </div>
      )}
      {visited.has("poster_human") && (
        <div hidden={!(tool === "poster_human" && step === "editor")}>
          <PosterHumanCreator
            key={`poster_human-${projectNonce}`}
            onBack={backToHub}
            onContinue={finalize}
            onBatch={onToolBatch("poster_human")}
            initialRunIds={initialTool === "poster_human" ? (initialRunIds ?? (resumeRunId != null ? [resumeRunId] : null)) : undefined}
            initialRunId={initialTool === "poster_human" ? resumeRunId : undefined}
            initialBrief={briefFor("poster_human")}
          />
        </div>
      )}
      {visited.has("human_character") && (
        // Save-to-Assets utility tool — no Preview/Instagram handoff (no
        // onContinue), so it never advances past Editing. Back returns to the hub.
        <div hidden={!(tool === "human_character" && step === "editor")}>
          <HumanCharacterCreator
            key={`human_character-${projectNonce}`}
            onBack={backToHub}
            initialBrief={briefFor("human_character")}
          />
        </div>
      )}
      {visited.has("talking_head_v2") && (
        <div hidden={!(tool === "talking_head_v2" && step === "editor")}>
          <TalkingHeadV2Creator
            key={`talking_head_v2-${projectNonce}`}
            onBack={backToHub}
            navigate={navigate}
            onContinue={finalize}
            onOpenInEditor={(projectId) => openVideoEditor(projectId)}
            // Upgrade the fresh-tool hash (#producer/talking_head_v2) to a resume
            // deep-link (#producer/talking_head_v2:<id>[@<step>]) once the run
            // exists, so F5 restores it at its stage. replaceState → no reload.
            onRunId={onToolRunId("talking_head_v2")}
            onStep={onToolStep("talking_head_v2")}
            initialStep={initialStep}
            initialRunId={initialTool === "talking_head_v2" ? resumeRunId : undefined}
            initialBrief={briefFor("talking_head_v2")}
            // On screen: the Rebrand studio mounts (and polls) only then.
            active={tool === "talking_head_v2" && step === "editor"}
          />
        </div>
      )}
      {visited.has("video_editor") && (
        // Fixed-height wrapper so the (full-bleed, full-height) editor fits UNDER
        // the global stepper instead of overflowing the viewport.
        <div
          hidden={!(tool === "video_editor" && step === "editor")}
          className="h-[calc(100vh-150px)]"
        >
          <VideoEditorCreator
            key={`video_editor-${projectNonce}`}
            inRoadmap
            onBack={backToHub}
            onContinue={finalize}
            initialRunId={tool === "video_editor" ? resumeRunId : undefined}
            initialBrief={briefFor("video_editor")}
          />
        </div>
      )}
      {visited.has("general_video") && (
        <div hidden={!(tool === "general_video" && step === "editor")}>
          <GeneralVideoCreator
            key={`general_video-${projectNonce}`}
            onBack={backToHub}
            onContinue={finalize}
            onOpenInEditor={(projectId) => openVideoEditor(projectId)}
            onRunId={onToolRunId("general_video")}
            initialRunId={initialTool === "general_video" ? resumeRunId : undefined}
            initialBrief={briefFor("general_video")}
          />
        </div>
      )}
      {visited.has("broll") && (
        <div hidden={!(tool === "broll" && step === "editor")}>
          <BrollCreator
            key={`broll-${projectNonce}`}
            onBack={backToHub}
            onContinue={finalize}
            onOpenInEditor={(projectId) => openVideoEditor(projectId)}
            onStep={onToolStep("broll")}
            initialStep={initialStep}
            initialRunId={initialTool === "broll" ? resumeRunId : undefined}
            initialBrief={briefFor("broll")}
          />
        </div>
      )}

      {step === "preview" && asset && (
        <PreviewStep
          asset={asset}
          caption={draftCaption}
          onCaptionChange={setDraftCaption}
          onBack={() => goTo("editor")}
          onConfirm={onCaptionConfirmed}
        />
      )}

      {step === "calendar" && asset && (
        <CalendarStep
          asset={asset}
          onBack={() => goTo("preview")}
          onScheduled={onScheduled}
          onPostNow={onPostNow}
        />
      )}

      {step === "approval" && asset && (
        <ApprovalStep
          asset={asset}
          onBack={() => goTo("calendar")}
          onDone={finishAndReset}
          autoApprove={autoApprove}
        />
      )}
    </div>
  );
}
