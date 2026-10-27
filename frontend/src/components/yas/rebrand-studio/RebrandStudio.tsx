// Rebrand studio — one scene, rebranded from a recipe you can see: made in
// house (our own graphics over the untouched footage, $0 — every new draft
// since 2026-09-29: a draft saved before is made in house from here on), or,
// for the versions made before, by Omni. A version Omni made still plays,
// rates and downloads; made again it is made in house ("Make it in house",
// Change it), and added to, Omni's video stays under the new layers — as it
// does under anything added to what was added to it in house (a "house
// pass": its layers are drawn again over that same video). A version made in
// house from the original is changed rather than added to: redrawn from the
// original with everything it had, it is the very same picture.
//
// Omni is switched off for new work (the owner, 2026-09-30; the server
// refuses a recipe for Omni): only a draft made in house is ever previewed,
// planned or generated (draftOk), so nothing here asks Omni for anything.
//
//   filmstrip   Original · v1 … vN · Next: what the one canvas shows
//   stage       one canvas, one picture of the speaker at a time: the free
//               preview of your change (Next), a version's settled frames
//               (its video on Play), or the original
//   rail        Patterns | Style tabs (Icons joins when the server draws
//               them), and views pushed over them (the pattern library,
//               what Omni is told)
//   footer      one status line and the one primary action, Generate
//
// It lives inline on the Rebrand stage (rebrand-stage/RebrandStage), one
// scene at a time: the stage mounts it per scene (keyed by run and target,
// so every ref and saved key starts clean), puts its pill, undo, redo and
// menu on the stage's top row (`actionsSlot`, from lg up), and hands it what
// ships (`pick`). It opens on what goes in the reel: a version on its way,
// else the scene's pick, else the original — and the card that goes in
// carries a ✓. Clicking Original or a ready version in the filmstrip (or
// "Back to vN", or "open it") makes it what ships (`onPick`); opening the
// studio, Generate and its "carry on" never do (a version Generate makes
// takes the ✓ on the server's side once it is ready). The stage also hears
// of every change to the list of versions (`onVersionsChanged`: a version
// on its way shows on its tile at once) and whether the draft holds edits
// made this visit that no version has (`onDraftState`).
//
// Every edit is acknowledged at once: the stage switches to Next (with a
// "Back to vN" chip for a few seconds), dims and says what it is doing, and
// lands with "Updated" once the new frame has decoded (useStudioPreview). A
// colour change is not even waited for: the stage paints it from the preview
// on screen (RecolourCanvas — the server's own sums) until the server's frame
// replaces it.
//
// A version's ⋯ menu rates it (1-5 and a note, for the next brief). Try
// again on one whose icons would be placed differently now asks first.
//
// "Show camera tracking" (the header's ⋯ menu, remembered in this browser)
// draws how the camera moves over the scene on the stage: a check for the
// in-house engine, which lets patterns painted in the room follow it.
//
// The draft recipe is kept per scene in this browser, so closing the window
// loses nothing; undo/redo last for the session. An untouched studio sends
// exactly the server's 42 default. The server checks every recipe and a 422
// is shown in its own words.
//
// Carrying on from a finished version ("Add more to v2", then the bar on
// the rail: "Adding to v2"):
//   made in house   the draft is v2's whole recipe, every row tagged "from
//                   v2" until it changes (Change it): Next IS v2's picture
//                   until something changes, and Generate makes v3 = v2 +
//                   the change, $0
//   made by Omni    Add to it: the draft holds only the new layers, drawn on
//   (or in house    v2's own video (kept as it is; Next IS v2's picture
//   on top of one)  while nothing is new, made in house); or Change it: its
//                   whole look redone in house from the original — Omni's
//                   look comes out flat, so it is never "v2" again
// After Generate the studio carries on from the new version by itself — when
// it is ready and the draft is still exactly what was sent (one undo step:
// "Carrying on from v3. Undo"). It never changes a version with Omni's video
// under it by itself (one stacked as high as it goes is left as it is). "Start over from the original" (the bar) is
// a new scene's draft again: the 42 default + automatic icons. The icons of
// a draft carried on from a version are planned again; when they come out
// other than it froze them, "Keep v2's icons" keeps them exactly.
// The version and the mode are kept with the draft (per scene, one undo step
// per switch) and sent with every preview and Generate. With none, every
// request body is exactly what it was before.
//
// A version made in house whose video is gone from the server rests on its
// still, with "Make it again" (free: the same frames, from its recipe).
//
// Icons (when the server draws them) are ON in a new scene's draft: the 42
// default + automatic icons ("42 default + icons"); "Reset to 42 default" is
// the preset itself, with none. Their plan is its own request (useIconPlan),
// never waiting on the pattern preview. On the Icons tab, Next shows them in
// motion over the scene, with the lane under it; Generate waits for the plan
// ("Placing icons…") and sends its key.
//
// The reel's intro and outro (`kind`) are done by hand only: the draft opens
// empty (the server's own default, with its seed), nothing is added by
// itself, the icons are only the ones added (ai off: the director is never
// asked there, whatever is sent), and Add icon opens on Library (nothing is
// said, so nothing is suggested). The AI's switch, "Ask the AI again", the
// "42 default" pill and "Reset to 42 default" are not there; Layer goes
// when no one is in the clip (the server says: has_person); Generate waits
// for a pattern or an icon ("Add a pattern or an icon").
//
// The scene's timeline (SceneTimeline, under the stage, for a draft made in
// house): one ruler, one playhead (the stage's one clock: stageT, the
// player's), a bar per pattern (PatternLanes: when it shows, how it comes
// in and goes out) and the icons' lane, on one time scale. Its lanes are
// lib/rebrandTracks's and never depend on the rail's tab or on what is
// picked: picking only highlights (one thing at a time, a pattern or an
// icon) and brings its lane into view; a click on a pattern also opens its
// row. On Original and a version the same lanes show, read-only (the
// version's own patterns, and the icons it froze). The picture shows
// the playhead's moment: Next is the design still until the playhead is put
// somewhere (a click on the ruler, a bar let go, a row opened on a pattern
// the still doesn't show), then the exact frame at that moment
// (/house/moment), until "Still". A time that would put a full-screen
// pattern with another is put back, as the server would refuse it.
//
// The scene's end (the reel's, `end`; moved with `onEnd`): a grip on the
// timeline in every view of the scene — Next's, and a compact ruler on
// Original and on a version. Set by hand, every request carries it
// (`end_frame`, keyed only then): the preview, the plan, the moments, the
// draft render and Generate end there, and so does the scene's clock (its
// seconds, the patterns' spans, the icons' room, the player); Original
// plays up to it. A move of the end is an undo step like any edit. A
// version made for another end says so on its picture ("Ends at 7.8 s ·
// Remake": made again to this end, free).
//
// Kept past the automatic cut (a Veo tail flash), the frames after it can
// be branded too: on Next, dragging the grip there asks the stage for the
// whole clip (`onWantFull`; the hatched frames pulse while it is got ready,
// in the background), and once the grip is let go and it is ready the
// stage mounts the studio again on it (`variant` "full": every request
// says so). The draft is the scene's either way, and the studio opens
// again on what it showed (`resume`). Original needs none of this: the
// reel cuts the original clip wherever its end is.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, Crosshair, MapPin, Redo2, Undo2 } from "lucide-react";

import { isShown } from "../../../lib/isShown";
import { rebrandApi, type RebrandPattern } from "../../../lib/rebrandApi";
import {
  addOnDraft,
  changeDraft,
  continueMode,
  effectiveLayersOf,
  frozenIconsOf,
  frozenLanesOf,
  groundOf,
  hasOmniOwnIcons,
  housePass,
  inheritedFrom,
  keptItemLanes,
  layerSig,
  MAX_STACK_DEPTH,
  newOnVersion,
  sameAsVersion,
  shownIconOf,
  stackDepth,
  versionStillAt,
} from "../../../lib/rebrandBuildOn";
import {
  harnessApi,
  recipeKey,
  recipeOfVersion,
  sameBase,
  stillUrl,
  type HarnessBaseRef,
  type HarnessElement,
  type HarnessIconItem,
  type HarnessIconPlan,
  type HarnessLayer,
  type HarnessRecipe,
  type HarnessVersion,
  type SceneView,
  versionEndOf,
} from "../../../lib/rebrandHarnessApi";
import {
  anywayOf,
  cleanIconElement,
  fmtSec,
  glyphId,
  iconsDiffer,
  iconProps,
  iconsOf,
  iconsOn,
  iconsReady,
  iconTime,
  isStartingDraft,
  keepIcons,
  keptFrom,
  momentOf,
  pinOffWord,
  removeIcon,
  rulesOf,
  setBox,
  setIconsAi,
  setIconsEnabled,
  setLane,
  setLook,
  setOmniOwn,
  snapBack,
  speechOrder,
  stepMention,
  timeByHand,
  withStartingIcons,
} from "../../../lib/rebrandIcons";
import {
  clampCover,
  clampHand,
  composeHand,
  HAND_SCALE,
  handBox,
  handOf,
  type Hand,
} from "../../../lib/rebrandHand";
import {
  addPatterns,
  asHouse,
  backToAuto,
  clashOf,
  colourOf,
  enabledPatterns,
  entryOf,
  goesFull,
  introColourFrom,
  isHouse,
  isUntouchedDefault,
  patternsOf,
  previewPatternOf,
  setHand,
  setRamps,
  setSpan,
  slugOf,
  swapPattern,
  withKept,
  type Pins,
} from "../../../lib/rebrandRecipe";
import { focusTime, phase, timing as timingOn, type Timing } from "../../../lib/rebrandTiming";
import { cutOf, endNow, endSnaps, FPS, sceneSeconds, wantsWhole } from "../../../lib/rebrandEnd";
import { endSeconds } from "../../../lib/rebrandStage";
import type { ReelTarget } from "../../../lib/types";
import type { Span } from "../../../lib/rebrandSpan";
import type { Transform } from "../../../lib/rebrandTransform";
import { recolourOf } from "../../../lib/rebrandRecolour";
import {
  ADD_LANE_KEY,
  frozenIconBlocks,
  ICON_LANE_KEY,
  patternLaneKey,
  patternPick,
  timelineTracks,
  type IconLaneState,
} from "../../../lib/rebrandTracks";
import { usePersistentState } from "../../../lib/usePersistentState";
import { MenuItem, Notice, OverflowMenu } from "../../ui/controls";
import { Spinner } from "../../ui/Spinner";
import { Skeleton } from "../../Skeleton";
import { BuildOnBar } from "./BuildOnBar";
import { GenerateBar } from "./GenerateBar";
import { IconGlyph } from "./IconGlyph";
import { IconLane, IconLaneNote, IconLaneStatic, iconRowsOf } from "./IconLane";
import { IconPicker } from "./IconPicker";
import { iconRowId, IconsTab, type IconChoice } from "./IconsTab";
import type { HandSpec } from "./HandCanvas";
import { decodeAll } from "./imageDecode";
import { previewSrc } from "../PatternLibraryGrid";
import { PatternAddRow, PatternLanes, type LanePattern } from "./PatternLanes";
import { PatternLibrary } from "./PatternLibrary";
import { rowDomId } from "./PatternRow";
import { PatternsTab } from "./PatternsTab";
import { PromptView } from "./PromptView";
import { RateVersion } from "./RateVersion";
import { preloadRecolour } from "./RecolourCanvas";
import { SceneTimeline, type EndGrip } from "./SceneTimeline";
import { changeLabel, clock, COPY } from "./studioCopy";
import { StudioFilmstrip, type FilmCard } from "./StudioFilmstrip";
import {
  costOf,
  keptRows,
  lineageOf,
  madeInHouse,
  omniArm,
  startedAtOf,
  stillOf,
  versionPhase,
  visibleTabs,
  type RailTabId,
  type StageSel,
} from "./studioModel";
import { IconStageStatus, StagePlayer } from "./StagePlayer";
import { StudioRail } from "./StudioRail";
import { StudioStage } from "./StudioStage";
import { StyleTab } from "./StyleTab";
import { TransformBox } from "./TransformBox";
import {
  clearSeenIdeas,
  editedThisVisit,
  noteSeenIdeas,
  seenIdeasOf,
  studioDraftKey,
  useDraftHistory,
  type EndIo,
} from "./useDraftHistory";
import { useHouseRender } from "./useHouseRender";
import { useIconCatalogue } from "./useIconCatalogue";
import { useSpeakerSamples } from "./useSpeakerSamples";
import { useIconPlan } from "./useIconPlan";
import { useIconMoment, useIconPlayer } from "./useIconPlayer";
import { usePatternArt } from "./usePatternArt";
import { useRemake } from "./useRemake";
import { useSceneCamera } from "./useSceneCamera";
import { useStudioBase } from "./useStudioBase";
import { useStudioPreview } from "./useStudioPreview";
import { useStudioScene } from "./useStudioScene";
import { VersionActions } from "./VersionActions";
import { isActive, useStudioVersions } from "./useStudioVersions";

/** How long "Back to vN" stays once the edit's preview has landed. */
const BACK_CHIP_MS = 6000;
const NEXT_PULSE_MS = 1500;
/** The rail tabs this studio has a panel for (each also needs the server
 *  to draw its kinds). */
const BUILT_TABS: ReadonlySet<RailTabId> = new Set<RailTabId>(["patterns", "icons", "style"]);
const CIRCLED = ["①", "②", "③", "④", "⑤", "⑥"];
/** What each scene's studio shows (by its draft's key), for the page
 *  session: mounted again on the scene's other clip, it opens there. */
const SHOWN = new Map<string, StageSel>();

type Push =
  | { kind: "library"; mode: "add" | "swap"; id: string | null }
  | { kind: "prompt" }
  | { kind: "icons"; mode: "add" | "swap"; item: HarnessIconItem | null }
  | null;

function isTextField(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

/** "Place icons again" on a version whose icons couldn't all be placed or
 *  drawn: its own icon element alone (switched on, our icons), to lay on it
 *  again — null when it has none. */
function restampOf(v: HarnessVersion): HarnessRecipe | null {
  const r = recipeOfVersion(v);
  const el = r ? iconsOf(r) : null;
  if (!r || !el) return null;
  const icon = cleanIconElement({ ...el, enabled: true });
  return iconsOn({ ...r, elements: [icon] }) ? { ...r, preset: null, elements: [icon] } : null;
}

/** An intro or outro is done by hand: its icons are only the ones added
 *  (never the AI's — the server forces it too). */
function byHand(r: HarnessRecipe): HarnessRecipe {
  const el = iconsOf(r);
  return el && iconProps(el).ai ? setIconsAi(r, false) : r;
}

/** A per-viewer flag in this browser. Unreadable storage counts as set (so
 *  nothing is changed on the strength of it). */
function readFlag(key: string): boolean {
  try {
    return window.localStorage.getItem(key) === "1";
  } catch {
    return true;
  }
}

function writeFlag(key: string): void {
  try {
    window.localStorage.setItem(key, "1");
  } catch {
    // a convenience only
  }
}

function HeaderButton({
  label,
  shortcut,
  disabled,
  onClick,
  children,
}: {
  label: string;
  shortcut: string;
  disabled: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={`${label} (${shortcut})`}
      aria-keyshortcuts={shortcut.replace("Ctrl", "Control")}
      disabled={disabled}
      onClick={onClick}
      className="h-8 w-8 shrink-0 rounded-lg flex items-center justify-center text-fg hover:bg-bg-muted disabled:opacity-35 disabled:hover:bg-transparent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
    >
      {children}
    </button>
  );
}

export function RebrandStudio({
  runId,
  target,
  kind = "scene",
  pick,
  end = null,
  variant = null,
  resume = false,
  whole = null,
  actionsSlot,
  onPick,
  onEnd,
  onEndDrag,
  onWantFull,
  onVersionsChanged,
  onDraftState,
  onPrepared,
}: {
  runId: number;
  /** scene_N, or the reel's "intro" | "outro". */
  target: string;
  /** A main scene, or the reel's intro or outro (done by hand only). */
  kind?: "scene" | "intro" | "outro";
  /** The version that goes in the reel for this scene (null: the original):
   *  what the studio opens on when nothing is on its way, and the card
   *  with the ✓. */
  pick: number | null;
  /** Where the scene ends in the reel (the stage's word: 30 fps frames,
   *  `user` set by hand); null: a server without the end grip. */
  end?: ReelTarget["end"] | null;
  /** "full": the scene's whole clip, past its automatic cut (the stage
   *  mounts the studio on it once it is ready). */
  variant?: "full" | null;
  /** Mounted again on the same scene (its clip changed): open on what was
   *  shown. */
  resume?: boolean;
  /** The whole clip, as the stage has it: being got ready (the frames past
   *  the cut pulse), ready, not on this server, or not got ready this time. */
  whole?: "preparing" | "ready" | "none" | "failed" | null;
  /** Where the header's pill, undo, redo and menu go (the stage's top row);
   *  null: at the top of the studio itself (below lg, where they scroll
   *  away with it). */
  actionsSlot: HTMLElement | null;
  /** Original (null) or a ready version was clicked: it goes in the reel. */
  onPick?: (target: string, versionId: number | null) => void;
  /** The end grip moved (null: automatic; `nudge`: a key step). */
  onEnd?: (target: string, frame: number | null, nudge: boolean) => void;
  /** The grip is being dragged (where; null: let go). */
  onEndDrag?: (target: string, frame: number | null) => void;
  /** Next wants the frames past the automatic cut: the whole clip. */
  onWantFull?: (target: string) => void;
  /** This scene's versions changed (polled, or Generate just made one). */
  onVersionsChanged?: (target: string, versions: HarnessVersion[]) => void;
  /** Whether the draft holds edits made this visit that no version has. */
  onDraftState?: (target: string, unmade: boolean) => void;
  /** The scene is prepared (its clip may have just been fetched). */
  onPrepared?: (target: string) => void;
}) {
  // The scene's end set by hand, and the whole clip past the automatic cut:
  // every request says so (only then).
  const endUser = end?.user ?? null;
  const full = variant === "full";
  const view = useMemo<SceneView | null>(
    () =>
      endUser != null || full
        ? { ...(endUser != null ? { endFrame: endUser } : {}), ...(full ? { variant: "full" as const } : {}) }
        : null,
    [endUser, full],
  );
  const {
    catalogue,
    setCatalogue,
    catalogueError,
    retryCatalogue,
    prepared,
    preparedView,
    prepareError,
    retryPrepare,
    startedAt,
  } = useStudioScene(runId, target, view);
  const bookend = kind !== "scene";
  const draftKey = studioDraftKey(runId, target);
  // The end on the undo stack: the stage holds it (and sends it).
  const endUserRef = useRef(endUser);
  endUserRef.current = endUser;
  const onEndRef = useRef(onEnd);
  onEndRef.current = onEnd;
  const hasOnEnd = !!onEnd;
  const endIo = useMemo<EndIo | null>(
    () => (hasOnEnd ? { now: () => endUserRef.current, set: (f, nudge) => onEndRef.current?.(target, f, nudge) } : null),
    [hasOnEnd, target],
  );
  const history = useDraftHistory(draftKey, `rebrand.studio.base.${runId}.${target}`, endIo);
  // Where the scene ends on the clip this studio shows (the server's
  // numbers), and the out-point an end set by hand makes on it: only an end
  // before the clip's own last frame cuts anything.
  const endInfo = prepared?.scene.end ?? null;
  const cut = endInfo ? cutOf(endUser, endInfo.frames) : null;
  // Past the automatic cut, branding needs the whole clip (a main scene's,
  // on a server that has it): asked for once per clip this studio shows.
  const onEndDragRef = useRef(onEndDrag);
  onEndDragRef.current = onEndDrag;
  const onWantFullRef = useRef(onWantFull);
  onWantFullRef.current = onWantFull;
  const pastCut = (f: number | null) => f != null && endInfo?.auto != null && f > endInfo.auto;
  const wantFull = () => {
    if (!full && prepared?.scene.variant !== "full") onWantFullRef.current?.(target);
  };
  const fullBusy = whole === "preparing";
  // Kept past the automatic cut, a version is made of the whole clip: until
  // the stage mounts the studio on it, Generate waits (made of the cut clip,
  // it would end short of the end kept, and nothing would say so).
  const wholeWait = wantsWhole({
    end: endUser,
    auto: endInfo?.auto ?? null,
    onWhole: full || prepared?.scene.variant === "full",
    canAsk: !!onWantFull,
    whole,
  });
  const { draft, base } = history;
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const defaultRecipe = prepared?.default_recipe ?? null;

  // Icons are drawn by this server (the catalogue says so).
  const iconsServed = iconsReady(catalogue);
  const catalogueSettled = !!catalogue || !!catalogueError;

  // The saved draft, if it is this scene's and of this recipe version; else
  // the server's 42 default as served — with icons on, when the server draws
  // them (so the catalogue is waited for before a new scene's draft is made).
  // Made in house, always: Omni is switched off for new work, so a draft
  // saved for Omni is never previewed, planned or generated — it is made in
  // house first (just below).
  const draftOk =
    !!draft &&
    !!defaultRecipe &&
    draft.target === target &&
    draft.recipe_version === defaultRecipe.recipe_version &&
    isHouse(draft);
  useEffect(() => {
    if (!defaultRecipe || !catalogueSettled) return;
    const d = draftRef.current;
    const started = `rebrand.studio.iconsStart.${runId}.${target}`;
    if (!d || d.target !== target || d.recipe_version !== defaultRecipe.recipe_version) {
      // An intro or outro opens on the server's own default: empty.
      history.reset(!bookend && iconsServed ? withStartingIcons(defaultRecipe) : defaultRecipe);
    } else if (
      !bookend &&
      // A draft saved before icons came (or while the icon catalogue couldn't
      // be read), still the untouched 42 default, on a scene with no version
      // yet: it starts as a new scene does, with icons on — once. A "Reset
      // to 42 default" made since is the user's, and stays.
      iconsServed &&
      !readFlag(started) &&
      !history.base &&
      !(prepared?.versions.length ?? 0) &&
      isUntouchedDefault(d, defaultRecipe)
    ) {
      history.reset(withStartingIcons(defaultRecipe));
    } else if (d.engine !== "house") {
      // Saved before new work was made in house: the same draft, made in
      // house (and on the same version, if it was built on one).
      history.reset(asHouse(d), history.base);
    }
    if (iconsServed && !bookend) writeFlag(started);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultRecipe, target, catalogueSettled, iconsServed]);

  // Every part's first frame, decoded once: "Hold: before", hiding the only
  // pattern and Original are then instant.
  useEffect(() => {
    if (prepared) void decodeAll(prepared.scene.chunks.map((c) => c.frame_url));
  }, [prepared]);
  // The stage hears the scene is ready (it reads the reel again).
  const onPreparedRef = useRef(onPrepared);
  onPreparedRef.current = onPrepared;
  useEffect(() => {
    if (prepared) onPreparedRef.current?.(target);
  }, [prepared, target]);

  const { versions, active, posting, error: generateError, clearError, generate: postVersion, cancel, adopt } =
    useStudioVersions(runId, target, prepared?.versions);
  // The stage hears of every change to the list (a version on its way, one
  // made ready): its tile and Finalize follow at once.
  const onVersionsRef = useRef(onVersionsChanged);
  onVersionsRef.current = onVersionsChanged;
  useEffect(() => {
    if (versions.length) onVersionsRef.current?.(target, versions);
  }, [versions, target]);
  // --- the version the draft is built on ---------------------------------------
  // The rows the server listed, before this window's own list has them.
  const knownVersions = versions.length ? versions : prepared?.versions ?? [];
  const baseVersion = base ? knownVersions.find((v) => v.id === base.version_id) ?? null : null;
  const baseUsable = !!baseVersion && versionPhase(baseVersion) === "ready" && !!recipeOfVersion(baseVersion);
  // The list holds the scene's latest 20 versions only: one the draft is
  // built on that isn't in it is asked for by id (and joins the list) before
  // the draft gives up on it. Meanwhile nothing is previewed or generated.
  const [lookups, setLookups] = useState<Record<number, "checking" | "gone" | "failed">>({});
  const baseId = base?.version_id ?? null;
  const lookup = baseId !== null && !baseVersion && prepared ? lookups[baseId] ?? "checking" : null;
  useEffect(() => {
    if (!prepared || baseId === null || baseVersion || lookups[baseId]) return;
    setLookups((l) => ({ ...l, [baseId]: "checking" }));
    rebrandApi
      .get(baseId)
      .then((row) => {
        if (row.run_id !== runId || row.target !== target) {
          setLookups((l) => ({ ...l, [baseId]: "gone" }));
          return;
        }
        // Read on its own it carries no stills: the list's, for this scene's parts.
        const stills = row.status === "ready" ? prepared.scene.chunks.map((c) => stillUrl(row.id, c.index)) : null;
        adopt({ ...row, recipe: recipeOfVersion(row as HarnessVersion), stills });
      })
      .catch((e) =>
        setLookups((l) => ({ ...l, [baseId]: /^404\b/.test(String(e?.message ?? "")) ? "gone" : "failed" })),
      );
  }, [prepared, baseId, baseVersion, lookups, adopt, runId, target]);
  const retryLookup = () => {
    if (baseId !== null) setLookups(({ [baseId]: _, ...rest }) => rest);
  };
  // Kept in this browser, it can name a version that is gone, or that can
  // never be built on: the draft goes on as a fresh one — and says so when
  // it held only what was being added to it.
  const { forgetBase } = history;
  const [dropped, setDropped] = useState<{ no: number | null } | null>(null);
  const baseGone = !!prepared && !!base && (baseVersion ? !baseUsable : lookup === "gone");
  useEffect(() => {
    if (!base || !baseGone) return;
    const no = baseVersion ? knownVersions.findIndex((v) => v.id === baseVersion.id) + 1 : 0;
    forgetBase(base.version_id);
    if (base.mode === "stack") setDropped({ no: no || null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base, baseGone, forgetBase]);
  const baseChecking = lookup === "checking";
  // What the draft is built on — only ever a version that can be.
  const building = baseUsable ? base : null;
  const baseRef = useRef(building);
  baseRef.current = building;
  const baseNo = baseVersion ? knownVersions.findIndex((v) => v.id === baseVersion.id) + 1 : null;
  // Made in house from the original: changed, never added to (the bar has
  // no Add). Omni's video under it (made by Omni, or a house pass on one):
  // the version whose video it is ("from v25"), for the bar's words.
  const flatHouse = !!baseVersion && madeInHouse(baseVersion) && !housePass(baseVersion);
  const groundVersion = baseVersion ? groundOf(baseVersion, knownVersions) : null;
  const groundNo = groundVersion ? knownVersions.findIndex((v) => v.id === groundVersion.id) + 1 || null : null;
  const stack = building?.mode === "stack";
  const baseState = useStudioBase(runId, target, building, !!prepared, view);
  // Added on top of it: its layers, kept exactly as they are.
  const kept: HarnessLayer[] =
    stack && baseVersion ? baseState.info?.layers ?? effectiveLayersOf(baseVersion, knownVersions) : [];
  const keptOn = kept.filter((l) => l.enabled);
  // "Loaded v2's settings · Undo", right after Change it — or "Carrying on
  // from v2 · Undo" when the studio did it after Generate (`auto`).
  const [loaded, setLoaded] = useState<{ id: number; auto: boolean } | null>(null);

  const preview = useStudioPreview({
    runId,
    target,
    draft: draftOk ? draft : null,
    base: draftOk ? building : null,
    enabled: !!prepared && draftOk && !baseChecking,
    warming: prepared?.warming ?? null,
    view,
  });

  // --- what the stage shows ---------------------------------------------------
  const [sel, setSel] = useState<StageSel>({ kind: "original" });
  const selRef = useRef(sel);
  selRef.current = sel;
  const [part, setPart] = useState(0);
  const [backTo, setBackTo] = useState<StageSel | null>(null);
  const [nextPulse, setNextPulse] = useState(false);
  const pulseTimer = useRef<number | undefined>();
  const sentTargets = useRef(new Map<number, string[]>());

  // On open, what goes in the reel: a version on its way, if there is one;
  // else the scene's pick (the version that ships), else the original. An
  // edit still takes the stage to Next, with the way back (toNext).
  const selInit = useRef(false);
  const pickRef = useRef(pick);
  pickRef.current = pick;
  // Mounted again on the same scene (its clip changed): what it showed.
  const resumeRef = useRef(resume);
  useEffect(() => {
    if (!prepared || selInit.current) return;
    selInit.current = true;
    const was = resumeRef.current ? SHOWN.get(draftKey) : undefined;
    if (was) {
      setSel(was);
      return;
    }
    const running = prepared.versions.find(isActive);
    const ships = pickRef.current != null ? prepared.versions.find((v) => v.id === pickRef.current) : undefined;
    const open = running ?? ships;
    setSel(open ? { kind: "version", id: open.id } : { kind: "original" });
  }, [prepared]);
  // A version that is gone from the list takes the stage back to Next.
  useEffect(() => {
    if (sel.kind === "version" && versions.length && !versions.some((v) => v.id === sel.id)) setSel({ kind: "next" });
  }, [sel, versions]);
  useEffect(() => {
    if (selInit.current) SHOWN.set(draftKey, sel);
  }, [sel, draftKey]);
  // The scene's end past the automatic cut — let go there, or found there
  // on opening (a reload, Original then Next): the whole clip, got ready in
  // the background, and the stage mounts the studio on it.
  useEffect(() => {
    if (pastCut(endUser)) wantFull();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [endUser, prepared]);
  useEffect(() => () => window.clearTimeout(pulseTimer.current), []);

  const select = useCallback((s: StageSel) => {
    setSel(s);
    setBackTo(null);
  }, []);
  // What the user clicks goes in the reel: Original, or a version that is
  // ready (one on its way, failed or stopped is only looked at).
  const onPickRef = useRef(onPick);
  onPickRef.current = onPick;
  const versionsRef = useRef<HarnessVersion[]>([]);
  versionsRef.current = versions;
  const choose = useCallback(
    (s: StageSel) => {
      select(s);
      if (s.kind === "original") onPickRef.current?.(target, null);
      else if (s.kind === "version") {
        const v = versionsRef.current.find((x) => x.id === s.id);
        if (v && versionPhase(v) === "ready") onPickRef.current?.(target, v.id);
      }
    },
    [select, target],
  );

  /** An edit shows its result: the stage goes to Next, with a way back. */
  const toNext = useCallback(() => {
    const cur = selRef.current;
    if (cur.kind === "next") return;
    setSel({ kind: "next" });
    setBackTo(cur);
    setNextPulse(true);
    window.clearTimeout(pulseTimer.current);
    pulseTimer.current = window.setTimeout(() => setNextPulse(false), NEXT_PULSE_MS);
  }, []);

  const { set: setStep, undo: undoStep, redo: redoStep } = history;
  const edit = useCallback(
    (next: HarnessRecipe, opts?: { coalesce?: string; base?: HarnessBaseRef | null }) => {
      if (!setStep(bookend ? byHand(next) : next, opts)) return false;
      toNext();
      setLoaded(null);
      return true;
    },
    [setStep, toNext, bookend],
  );
  // An undo of the end alone leaves the stage where it is.
  const undo = useCallback(() => {
    if (undoStep() !== "draft") return;
    toNext();
    setLoaded(null);
  }, [undoStep, toNext]);
  const redo = useCallback(() => {
    if (redoStep() !== "draft") return;
    toNext();
    setLoaded(null);
  }, [redoStep, toNext]);

  // "Back to vN" stays while the change is being drawn, then a few seconds.
  const previewSettled = preview.status === "idle" || preview.status === "error";
  useEffect(() => {
    if (!backTo || !previewSettled) return;
    const t = window.setTimeout(() => setBackTo(null), BACK_CHIP_MS);
    return () => window.clearTimeout(t);
  }, [backTo, previewSettled]);

  // --- the rail -----------------------------------------------------------------
  const tabs = visibleTabs(catalogue, BUILT_TABS);
  const [savedTab, setSavedTab] = usePersistentState<RailTabId>("rebrand.studio.tab", "patterns");
  // Show camera tracking (a per-viewer check, never part of a recipe).
  const [cameraDebug, setCameraDebug] = usePersistentState<boolean>("rebrand.studio.cameraDebug", false);
  // A draft made in house: Next plays its exact video (or a quick stand-in,
  // which moves the patterns with the camera and cuts them by the scene's
  // matte bank — both come with the camera).
  const houseDraft = draftOk && isHouse(draft);
  const sceneCamera = useSceneCamera(runId, target, (cameraDebug || houseDraft) && !!prepared, view);
  const cameraTrack = sceneCamera && sceneCamera.state === "ready" ? sceneCamera : null;
  // An intro or outro with no one in it (the reel's own card or card
  // animation; an uploaded clip once its analysis says so): nothing to go
  // behind, so Layer isn't offered.
  const hasPerson = prepared?.scene.has_person ?? preview.warmup?.has_person ?? sceneCamera?.has_person ?? null;
  const noPerson = bookend && hasPerson === false;
  const tab: RailTabId = tabs.some((t) => t.id === savedTab) ? savedTab : tabs[0]?.id ?? "patterns";
  const [push, setPush] = useState<Push>(null);
  const pushTrigger = useRef<HTMLElement | null>(null);

  // --- icons ------------------------------------------------------------------------
  const iconCat = useIconCatalogue(iconsServed);
  const rules = rulesOf(iconCat.catalogue);
  const iconPlan = useIconPlan({
    runId,
    target,
    draft: draftOk ? draft : null,
    base: draftOk ? building : null,
    enabled: iconsServed && !!prepared && !baseChecking,
    patternCurrent: preview.current,
    patternUnkept: preview.current && preview.shown?.preview.kept === false,
    patternKey: preview.shown?.key ?? null,
    view,
  });
  // The version built on shows icons Omni drew itself: the server's word for
  // it (on this draft's own plan or preview), else read from its recipes.
  const serverOmniIcons = iconPlan.current
    ? iconPlan.plan?.parent_has_omni_icons
    : preview.current
      ? preview.shown?.preview.parent_has_omni_icons
      : undefined;
  const parentOmniIcons = !!baseVersion && (serverOmniIcons ?? hasOmniOwnIcons(baseVersion, knownVersions));
  const player = useIconPlayer(cut != null ? cut / FPS : null);
  const [selIcon, setSelIcon] = useState<string | null>(null);
  const [iconChoice, setIconChoice] = useState<IconChoice | null>(null);
  const [showSpots, setShowSpots] = useState(false);
  // "Snap to words" on the icon lane (per viewer; on unless switched off).
  const [snapWords, setSnapWords] = usePersistentState<boolean>("rebrand.studio.snapWords", true);
  // A question about the draft as it was goes once the draft moves on.
  const draftKeyNow = draft ? recipeKey(draft) : "";
  useEffect(() => setIconChoice(null), [draftKeyNow]);
  // Next shows the icons in motion while the Icons tab is open; a draft made
  // in house also plays from its still on the other tabs (Play) — the same
  // StagePlayer, the same clock — and goes back to the still when it ends.
  const iconsMode = tab === "icons" && sel.kind === "next" && !!prepared && iconPlan.on;
  const [nextPlaying, setNextPlaying] = useState(false);
  const playerMode = sel.kind === "next" && !!prepared && (iconsMode || (houseDraft && nextPlaying));
  // Next rests on the timeline's playhead (a draft made in house): its exact
  // frame at that moment stands in for the design still until "Still".
  const [atMoment, setAtMoment] = useState(false);
  useEffect(() => {
    if (!playerMode && player.playing) player.pause();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playerMode]);
  useEffect(() => {
    if (sel.kind !== "next" || !houseDraft) {
      setNextPlaying(false);
      setAtMoment(false);
    }
  }, [sel.kind, houseDraft]);
  const endedSeen = useRef(player.ended);
  useEffect(() => {
    if (player.ended === endedSeen.current) return;
    endedSeen.current = player.ended;
    if (!iconsMode) {
      setNextPlaying(false);
      setAtMoment(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [player.ended]);
  // The draft's exact video (made in house): asked for once its preview and
  // (with icons) its plan are the draft's own; the stage plays it only
  // while it is the draft's — else the quick stand-in.
  const planShownKey = iconPlan.on && iconPlan.current ? iconPlan.plan?.plan_key ?? null : null;
  const houseRender = useHouseRender({
    runId,
    target,
    draft: draftOk ? draft : null,
    base: building,
    planKey: planShownKey,
    view,
    enabled:
      houseDraft &&
      !!prepared &&
      !baseChecking &&
      preview.current &&
      preview.status !== "error" &&
      (!iconPlan.on || (iconPlan.ready && !!planShownKey)),
  });
  const exactUrl = sel.kind === "next" ? houseRender.url : null;
  // Resting on a moment, not in motion: the exact frame there is Next's
  // picture (only for the draft's own preview).
  const stillAtMoment = atMoment && houseDraft && sel.kind === "next" && !playerMode && preview.current;
  const moment = useIconMoment({
    runId,
    target,
    recipe: houseDraft ? draft : iconPlan.shown?.recipe ?? null,
    planKey: iconPlan.current ? iconPlan.plan?.plan_key ?? null : null,
    versionId: building?.mode === "stack" ? building.version_id : null,
    stop: player.stop,
    playing: player.playing,
    // The exact video's own paused frame is the output's: no still asked.
    // A draft made in house with icons waits for the draft's own plan (its
    // frame holds those icons; asked without it, the server says 409).
    enabled: (playerMode ? !exactUrl : stillAtMoment) && (!houseDraft || !iconPlan.on || !!planShownKey),
    house: houseDraft && draft ? { key: recipeKey(draft) + (building ? `@${building.mode}:${building.version_id}` : ""), base: building } : null,
    view,
  });
  // The moment on screen stays until the next one has decoded (a new frame
  // replaces it hard, never the design still in between).
  const [shownMoment, setShownMoment] = useState<string | null>(null);
  useEffect(() => {
    if (!atMoment) setShownMoment(null);
    else if (stillAtMoment && moment.exact) setShownMoment(moment.exact.url);
  }, [atMoment, stillAtMoment, moment.exact]);

  const [openRow, setOpenRow] = useState<string | null>(null);
  const [focusRow, setFocusRow] = useState<string | null>(null);
  const clearFocusRow = useCallback(() => setFocusRow(null), []);
  // One thing picked at a time: a pattern (its open row) or an icon. Picking
  // never changes the timeline's lanes (lib/rebrandTracks).
  const pickPattern = useCallback((id: string | null) => {
    setOpenRow(id);
    if (id) setSelIcon(null);
  }, []);
  const pickIconOnly = useCallback((id: string | null) => {
    setSelIcon(id);
    if (id) setOpenRow(null);
  }, []);
  // What the pattern lanes have to say (a drop put back, a clash while
  // dragging), on the timeline's status line.
  const [laneNote, setLaneNote] = useState<string | null>(null);

  const openPush = (p: Exclude<Push, null>, trigger: HTMLElement | null) => {
    pushTrigger.current = trigger;
    setPush(p);
  };
  const closePush = (focus?: string) => {
    setPush(null);
    const back = focus ? null : pushTrigger.current;
    window.setTimeout(() => {
      if (focus) document.getElementById(focus)?.focus({ preventScroll: true });
      else if (back && document.contains(back)) back.focus({ preventScroll: true });
    }, 0);
  };

  // The intro's colour, once any preview has told it (what "Auto" paints).
  const [introColour, setIntroColour] = useState<string | null>(null);
  useEffect(() => {
    const c = introColourFrom(preview.shown?.preview ?? null, preview.shown?.recipe ?? null);
    if (c) setIntroColour(c);
  }, [preview.shown]);

  // --- versions -------------------------------------------------------------------
  const noOf = (v: HarnessVersion) => versions.findIndex((x) => x.id === v.id) + 1;
  // "Make it again" (a version made in house whose video is lost): once it
  // is, its row is read again (no longer lost; its video plays).
  const remakes = useRemake((id) => {
    void harnessApi
      .versions(runId, target)
      .then(({ versions: rows }) => {
        const row = rows.find((x) => x.id === id);
        if (row) adopt(row);
      })
      .catch(() => {
        const row = versions.find((x) => x.id === id);
        if (row) adopt({ ...row, lost: false });
      });
  });
  const version = sel.kind === "version" ? versions.find((v) => v.id === sel.id) ?? null : null;
  const nextNo = versions.length + 1;
  const stackFull = (v: HarnessVersion) => stackDepth(v, knownVersions) >= MAX_STACK_DEPTH;

  /** Build the draft on version v: Add to it (stack) or Change it
   *  (rebuild). One undo step: the draft and the mode together. `pick`:
   *  the studio chooses — a version made with Omni's own icons is changed,
   *  not added to, when the draft has icons of ours (two sets otherwise; the
   *  Icons tab says why, and Keep them / Add to it are a click away).
   *  `auto`: the studio carries on from the version Generate just made — the
   *  stage stays on it and the rail stays where it is. */
  const buildOn = (v: HarnessVersion, mode: "stack" | "rebuild", pick = false, auto = false) => {
    const d = draftRef.current;
    const cur = baseRef.current;
    if (!d) return;
    const twoSets =
      pick && mode === "stack" && hasOmniOwnIcons(v, knownVersions) && iconsOn(addOnDraft(d, cur, v, knownVersions));
    // Made in house from the original, a version is changed rather than
    // added to: redrawn from the original with everything it had, it is the
    // very same picture. One made in house on top of a version Omni made is
    // added to over Omni's video, as that version is.
    const m = twoSets || (mode === "stack" && madeInHouse(v) && !housePass(v)) ? "rebuild" : mode;
    if (sameBase(cur, { version_id: v.id, mode: m })) return;
    const next = m === "stack" ? addOnDraft(d, cur, v, knownVersions) : changeDraft(d, cur, v, knownVersions, catalogue);
    const on = { version_id: v.id, mode: m };
    if (auto ? !setStep(next, { base: on }) : !edit(next, { base: on })) return;
    setDropped(null);
    if (m === "rebuild" || auto) setLoaded({ id: v.id, auto });
    if (auto) return;
    setPush(null);
    setOpenRow(null);
    setSavedTab(twoSets ? "icons" : "patterns");
  };

  /** "Start over from the original": a new scene's draft again (the 42
   *  default + automatic icons; an intro or outro: empty), on the original —
   *  one undo step. */
  const startOver = () => {
    if (defaultRecipe) edit(!bookend && iconsServed ? withStartingIcons(defaultRecipe) : defaultRecipe, { base: null });
  };

  /** "Make it in house" on a version Omni made: its whole look — every
   *  layer, each pattern pinned where its run drew it — redone by our own
   *  renderer from the original ("from vN"), $0. One that never finished
   *  has no look to load: its own recipe again, made in house, on what it
   *  was built on. Nothing of the draft open comes along (it stays as it
   *  is); the icons are planned afresh by the server. */
  const makeInHouse = (v: HarnessVersion) => {
    const own = recipeOfVersion(v);
    const d = draftRef.current;
    if (!own || !d || active) return;
    if (versionPhase(v) === "ready") {
      const look = changeDraft({ ...own, target: d.target, elements: [] }, { version_id: v.id, mode: "rebuild" }, v,
        knownVersions, catalogue);
      void run(asHouse(look), { version_id: v.id, mode: "rebuild" }, null, true);
      return;
    }
    void run(asHouse(own), v.base ?? null, sentTargets.current.get(v.id) ?? null, true);
  };

  /** Stop building on it: Add keeps its new layers (drawn on the original
   *  from now on), Change keeps the settings it loaded. */
  const leaveBuild = () => {
    const d = draftRef.current;
    if (d && baseRef.current) edit(d, { base: null });
  };

  // "v2 is ready", said once to a screen reader.
  const [announce, setAnnounce] = useState("");
  const phases = useRef(new Map<number, string>());
  useEffect(() => {
    versions.forEach((v, i) => {
      const was = phases.current.get(v.id);
      const now = versionPhase(v);
      if (was && was !== now && now === "ready") setAnnounce(`v${i + 1} is ready`);
      phases.current.set(v.id, now);
    });
  }, [versions]);

  const refused = preview.status === "error" && !!preview.error && !preview.error.retryable;
  // Refused because of the version it is built on (on its way, or gone).
  const baseRefused =
    refused && !!building && [404, 409, 410].includes(preview.error?.status ?? 0);
  // Added on top of a version, Generate needs something new to add (its
  // own icons, unchanged, are not new).
  const added = stack && draft && baseVersion ? newOnVersion(draft, baseVersion, knownVersions) : [];
  const nothingNew = stack && !!draft && added.length === 0;
  // Only icons are new: they are re-stamped on the version, no Omni.
  const iconsOnly = stack && added.length > 0 && added.every((e) => e.kind === "icon");
  // Generate waits for the icons' plan (and sends its key) — or, when the
  // plan could not be had, says so (with Try again) instead of waiting on.
  const placingIcons = iconPlan.on && !iconPlan.ready;
  const iconsFailed = placingIcons && iconPlan.status === "error" ? iconPlan.error : null;
  // An intro or outro starts empty: Generate waits for a pattern or an icon.
  const emptyBookend =
    bookend &&
    !building &&
    !!draft &&
    enabledPatterns(draft).length === 0 &&
    (!iconsOn(draft) || iconProps(iconsOf(draft)).added.length === 0);
  const canGenerateAny =
    !!prepared && draftOk && !active && !refused && !nothingNew && !baseChecking && !placingIcons && !emptyBookend &&
    !wholeWait;

  // Try again on a version whose icons would be placed differently now: the
  // one line that asks first, and what Keep going sends.
  const [freshAsk, setFreshAsk] = useState<{ versionId: number; go: () => Promise<void> } | null>(null);
  // The end a Generate is made to (read when it is sent).
  const viewRef = useRef(view);
  viewRef.current = view;
  // The version being rated (its ⋯ menu → Rate).
  const [rating, setRating] = useState<number | null>(null);

  const run = useCallback(
    async (
      recipe: HarnessRecipe,
      on: HarnessBaseRef | null,
      sent: string[] | null,
      fresh = false,
      iconsPlanKey: string | null = null,
      // Try again: the version it makes again (and so its frozen icons).
      retryOf: HarnessVersion | null = null,
      // The draft's Generate: the ideas seen in Suggested while it was made.
      seenIdeas: string[] | null = null,
    ): Promise<HarnessVersion | null> => {
      // Try again (`fresh`) makes a version's own recipe again: its icons are
      // planned afresh on the server, so the plan it answers with is the one
      // to send — asked once more, never shown as "changed" (the draft is
      // not what was sent). When that plan isn't the version's own icons
      // (props.resolved) it asks first: they will be placed fresh.
      const again = { key: null as string | null, plan: null as HarnessIconPlan | null };
      const post = (key: string | null, retry: boolean) =>
        postVersion(recipe, {
          base: on,
          fresh,
          iconsPlanKey: key,
          view: viewRef.current,
          seenIdeas,
          // Asked again, a second "changed" is an error like any other.
          onIconConflict: retry
            ? undefined
            : (c) => {
                if (fresh && c.plan.plan_key) {
                  again.key = c.plan.plan_key;
                  again.plan = c.plan;
                  return;
                }
                // The icons moved on since they were shown: show the fresh plan.
                iconPlan.adopt(c.plan, COPY.icons.conflict, { recipe, base: on });
                setSel({ kind: "next" });
                setSavedTab("icons");
              },
        });
      const done = (v: HarnessVersion) => {
        if (sent) sentTargets.current.set(v.id, sent);
        select({ kind: "version", id: v.id });
      };
      let v = await post(iconsPlanKey, false);
      if (!v && again.key) {
        const key = again.key;
        const frozen = retryOf ? iconsOf(recipe)?.props.resolved : undefined;
        if (retryOf && again.plan && iconsDiffer(again.plan, frozen)) {
          setFreshAsk({
            versionId: retryOf.id,
            go: async () => {
              setFreshAsk(null);
              const made = await post(key, true);
              if (made) done(made);
            },
          });
          return null;
        }
        v = await post(key, true);
      }
      if (!v) return null;
      done(v);
      return v;
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [postVersion, select, iconPlan.adopt],
  );
  // The question is about the version on stage; it goes with it.
  useEffect(() => {
    if (freshAsk && !(sel.kind === "version" && sel.id === freshAsk.versionId)) setFreshAsk(null);
    if (rating !== null && !(sel.kind === "version" && sel.id === rating)) setRating(null);
  }, [sel, freshAsk, rating]);

  // Whether Generate may go (set below, once the draft is known not to be
  // the very version it carries on from).
  const canGenerateRef = useRef(false);
  // After Generate, the studio carries on from the version it made (once
  // it is ready, and only while the draft is still exactly what was sent).
  const carryOn = useRef<{ versionId: number; sentKey: string; sentBase: HarnessBaseRef | null } | null>(null);
  const generate = useCallback(async () => {
    const d = draftRef.current;
    if (!d || !canGenerateRef.current || posting) return;
    const shown = preview.current ? preview.shown : null;
    const sentBase = baseRef.current;
    const v = await run(
      d,
      sentBase,
      shown ? shown.preview.chunks.map((c) => c.target_url ?? c.frame_url) : null,
      false,
      iconPlan.on ? iconPlan.plan?.plan_key ?? null : null,
      null,
      seenIdeasOf(draftKey),
    );
    if (v) {
      carryOn.current = { versionId: v.id, sentKey: recipeKey(d), sentBase };
      clearSeenIdeas(draftKey);
    }
  }, [posting, preview.current, preview.shown, run, iconPlan.on, iconPlan.plan, draftKey]);
  useEffect(() => {
    const c = carryOn.current;
    const v = c ? versions.find((x) => x.id === c.versionId) : undefined;
    if (!c || !v) return;
    const ph = versionPhase(v);
    if (ph === "queued" || ph === "running") return;
    carryOn.current = null;
    const d = draftRef.current;
    // Edited since (or it didn't finish): nothing is switched — "Add more
    // to vN" stays one click away under it.
    if (ph !== "ready" || !d || recipeKey(d) !== c.sentKey || !sameBase(baseRef.current, c.sentBase)) return;
    const mode = continueMode(v, versions);
    if (mode) buildOn(v, mode, true, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [versions]);

  const generateRef = useRef(generate);
  generateRef.current = generate;

  // Ctrl/⌘+Enter generates; Ctrl/⌘+Z undoes and Ctrl/⌘+Shift+Z or Ctrl+Y
  // redoes, except inside a text field (whose own undo wins there) — and
  // only while the studio is on screen (another app tab open: the page
  // holding it is kept, hidden).
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
      if (!isShown(rootRef.current)) return;
      if (e.key === "Enter") {
        e.preventDefault();
        void generateRef.current();
        return;
      }
      if (isTextField(e.target)) return;
      const k = e.key.toLowerCase();
      if (k === "z" && !e.shiftKey) {
        e.preventDefault();
        undo();
      } else if ((k === "z" && e.shiftKey) || k === "y") {
        e.preventDefault();
        redo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [undo, redo]);

  // --- derived for the stage and the strip ---------------------------------------
  const scene = prepared?.scene ?? null;
  const partIndex = scene ? Math.min(part, scene.chunks.length - 1) : 0;
  const frame0 = scene?.chunks[0]?.frame_url ?? null;
  const busy = preview.status === "waiting" || preview.status === "loading";
  const shown = preview.shown;
  // The preview on screen was drawn for the base the draft is on (or, like
  // the draft, for none). One drawn for another — the original, or another
  // version — never stands in for Next while the draft's own is on its way.
  const shownFits = !shown || sameBase(shown.base, building);

  // --- the timeline: when each pattern shows, and the stage's clock ---------------
  // Cut short by the end set by hand; else exactly the server's (a prepare
  // that already carried a cut says the cut's: the whole is the parts').
  const partsSec = scene ? scene.chunks.reduce((a, c) => a + c.seconds, 0) : 0;
  const preparedCut = endInfo ? cutOf(preparedView?.endFrame ?? null, endInfo.frames) : null;
  const wholeSec = scene ? (preparedCut != null ? Math.round(partsSec * 100) / 100 : scene.duration_sec ?? partsSec) : 0;
  const sceneDuration = scene ? sceneSeconds(wholeSec, cut) : 0;
  const partStarts = scene ? scene.chunks.slice(1).map((_, j) => scene.chunks.slice(0, j + 1).reduce((a, c) => a + c.seconds, 0)) : [];
  const partAt = (t: number) => {
    let j = 0;
    while (j < partStarts.length && t >= partStarts[j]) j++;
    return j;
  };
  // The design still's moment (what Next shows until the playhead moves).
  const stillAt = houseDraft && preview.current && shownFits ? shown?.preview.house?.at ?? 0 : 0;
  // stageT: the moment the picture shows — the one clock (the player's) in
  // motion or at rest on a moment, else the still's.
  const stageT = playerMode || atMoment ? player.t : stillAt;
  /** A pattern's time on this scene, as the server works it out (the TS
   *  mirror, drawn with the entrance the preview resolved: its own, aimed). */
  const timingOf = (el: HarnessElement): Timing | null => {
    if (sceneDuration <= 0) return null;
    const pp = shown ? previewPatternOf(shown.preview, shown.recipe, el.id) : null;
    const entrance =
      pp?.animation ??
      (el.animation.entrance !== "auto" ? el.animation.entrance : entryOf(catalogue, slugOf(el))?.default_animation ?? "fade");
    return timingOn(el, { duration: sceneDuration, entrance });
  };
  /** Whether a pattern is on screen at the picture's moment. */
  const showsAt = (tm: Timing | null, t: number) => {
    if (!tm) return false;
    const [ph] = phase(tm, t);
    return ph !== "before" && ph !== "after";
  };
  /** Go to a moment: in motion, the player goes there (paused, its exact
   *  frame); on the still, Next rests on that moment (a draft made in
   *  house). A version or the original: the part that holds it. */
  const seekStage = (t: number) => {
    if (sel.kind !== "next" || !houseDraft) {
      setPart(partAt(t));
      return;
    }
    if (!playerMode) setAtMoment(true);
    player.seek(t);
  };

  const changeText = useMemo(() => {
    // Onto (or off) a version: the whole picture changes, so say that.
    if (shown && !sameBase(shown.base, building)) {
      if (!building || !baseNo) return COPY.drawingFresh;
      return building.mode === "stack" ? COPY.drawingOn(baseNo) : COPY.loadingFrom(baseNo);
    }
    const ch = preview.change;
    if (!ch) return changeLabel("pixels", []);
    const names = ch.ids.map((id) => {
      const el = draft?.elements.find((e) => e.id === id) ?? shown?.recipe.elements.find((e) => e.id === id);
      return (el && entryOf(catalogue, slugOf(el))?.name) || "the pattern";
    });
    return changeLabel(ch.kind, names);
  }, [preview.change, draft, shown, catalogue, building, baseNo]);

  // This part in the draft's colours, from the preview on screen: what the
  // stage paints while only a colour is changing (RecolourCanvas).
  // Not while Next rests on a moment: that picture is another moment's
  // footage, and the stage waits for its own new frame instead.
  const liveColour = useMemo(
    () =>
      shown && shownFits && draftOk && !atMoment
        ? recolourOf(shown.preview, draft, partIndex, { catalogue, introColour })
        : null,
    [shown, shownFits, draftOk, draft, partIndex, catalogue, introColour, atMoment],
  );
  // Its frame and alphas, read ahead once the preview is on screen, so the
  // first colour change paints at once.
  const shownChunk = shown && shownFits ? shown.preview.chunks[partIndex] ?? null : null;
  useEffect(() => {
    if (!shownChunk?.layers) return;
    const t = window.setTimeout(() => preloadRecolour(shownChunk), 300);
    return () => window.clearTimeout(t);
  }, [shownChunk]);

  const selectedBox =
    openRow && shown && preview.current ? previewPatternOf(shown.preview, shown.recipe, openRow)?.box ?? null : null;
  // Where the preview on screen drew each pattern, while it is the draft's:
  // what an edit around a pattern pins it to.
  const pins: Pins = preview.current && shown ? { preview: shown.preview, recipe: shown.recipe, catalogue } : null;

  // --- a pattern moved, sized or turned by hand (made in house) -------------------
  // The open pattern has handles on Next: its box before any move (the one
  // the recipe keeps — its pivot — else the preview's), moved, sized and
  // turned by its hand. A gesture paints the pattern at once from its own
  // art under the hand (HandCanvas), and, let go, is one edit (setHand: the
  // others pinned first); keyboard nudges join one undo step.
  const [handLive, setHandLive] = useState<{ id: string; hand: Hand } | null>(null);
  useEffect(() => setHandLive(null), [openRow]);
  const handEl =
    houseDraft && tab === "patterns" && openRow && draft
      ? draft.elements.find((e) => e.id === openRow && e.kind === "pattern" && e.enabled) ?? null
      : null;
  const handPP = handEl && shown && shownFits ? previewPatternOf(shown.preview, shown.recipe, handEl.id) : null;
  // The preview on screen drew it in the place it is pinned to (a hand
  // edit keeps the place: its handles stay up while the next is made).
  const handSamePlace =
    !!handPP?.layout && (!handEl?.props.layout || recipeKey(handEl.props.layout) === recipeKey(handPP.layout));
  const handBase = handEl && handSamePlace ? handEl.placement.box ?? handPP?.base_box ?? null : null;
  const handStart: Hand | null =
    handEl && handBase ? { ...handOf(handEl), pivot: [handBase.x + handBase.w / 2, handBase.y + handBase.h / 2] } : null;
  const handDrawn = handPP?.layout && handPP.base_box ? { layout: handPP.layout, box: handPP.base_box } : null;
  const handFull = !!handPP?.full;
  const keepUrl = (partIndex === 0 && shownChunk?.keep_url) || null;
  const handArt = usePatternArt({
    runId,
    target,
    draft: draftOk ? draft : null,
    base: building,
    elementId: handEl?.id ?? null,
    artKey: handPP?.art_key ?? null,
    keepUrl,
    enabled: !!handEl && preview.current,
    view,
  });
  // A full-screen composition's own outline (frame px): it never zooms
  // below filling the frame.
  const coverRect = (() => {
    const r = handFull ? handArt?.art_rect : null;
    if (!r || !scene) return null;
    return [r.x * scene.width, r.y * scene.height, (r.x + r.w) * scene.width, (r.y + r.h) * scene.height] as const;
  })();
  const applyHand = (t: Transform, opts: { final: boolean; key?: boolean }) => {
    if (!draft || !handEl || !handStart || !scene) return;
    let next = clampHand(composeHand(handStart, t));
    if (coverRect) next = clampHand(clampCover(next, coverRect, scene.width, scene.height));
    if (!opts.final) {
      setHandLive({ id: handEl.id, hand: next });
      return;
    }
    setHandLive(null);
    edit(setHand(draft, handEl.id, next, handDrawn, pins), opts.key ? { coalesce: `hand:${handEl.id}` } : undefined);
  };
  const handName = handEl ? entryOf(catalogue, slugOf(handEl))?.name ?? COPY.icons.colourChip.pattern : "";
  const handLeast =
    handStart && coverRect && scene
      ? clampCover({ ...handStart, scale: HAND_SCALE[0] }, coverRect, scene.width, scene.height).scale
      : HAND_SCALE[0];
  // Its handles only where it is on screen: at another moment, the way there.
  const handTiming = handEl ? timingOf(handEl) : null;
  const handHere = !handTiming || showsAt(handTiming, stageT);
  const patternHandles =
    handEl && handBase && handStart && scene && draft && !handHere && handTiming ? (
      <div className="absolute inset-x-3 top-3 flex justify-center">
        <button
          type="button"
          onClick={() => seekStage(focusTime(handTiming))}
          className="rounded-full bg-black/70 px-3 py-1.5 text-2xs font-medium text-white shadow-lift hover:bg-black/85 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {COPY.time.goThere(handName)}
        </button>
      </div>
    ) : handEl && handBase && handStart && scene && draft ? (
      <TransformBox
        box={handBox(handBase, handStart)}
        rotation={handStart.rotate}
        frame={{ w: scene.width, h: scene.height }}
        scaleRange={[Math.min(1, handLeast / handStart.scale), HAND_SCALE[1] / handStart.scale]}
        snaps={
          handFull
            ? null
            : {
                x: [0, 0.5, 1, handBase.x, handBase.x + handBase.w / 2, handBase.x + handBase.w],
                y: [0, 0.5, 1, handBase.y, handBase.y + handBase.h / 2, handBase.y + handBase.h],
              }
        }
        tone="plain"
        label={handFull ? COPY.hand.panLabel(handName) : COPY.hand.handlesLabel(handName)}
        hint={handFull ? COPY.hand.panHint : COPY.hand.handlesHint}
        name={handName}
        mode={handFull ? "pan" : "box"}
        rotate
        keep="partly"
        zoomLabels={{ in: COPY.hand.zoomIn, out: COPY.hand.zoomOut }}
        zoom={handStart.scale}
        onReset={() => {
          setHandLive(null);
          edit(backToAuto(draft, handEl.id));
        }}
        onChange={applyHand}
        onCancel={() => setHandLive(null)}
      />
    ) : null;
  // The pattern being moved, painted at once (the stage shows it while the
  // hand moves it, and until the server's frame of the move lands).
  const handSpec: HandSpec | null =
    handEl && handStart && handArt && shownChunk?.layers && draft
      ? {
          base: shownChunk.frame_url,
          layers: shownChunk.layers
            .filter((l) => !!l.alpha_url)
            .map((l) => {
              const e = draft.elements.find((x) => x.id === l.element_id);
              return {
                url: l.alpha_url,
                elementId: l.element_id,
                colour: (e ? colourOf(e, draft, { catalogue, introColour }) : null) ?? "#7f2bf2",
              };
            }),
          grabbed: {
            elementId: handEl.id,
            artUrl: handArt.art_url,
            window: handArt.art_window,
            colour: colourOf(handEl, draft, { catalogue, introColour }) ?? "#7f2bf2",
            hand: handLive?.id === handEl.id ? handLive.hand : handStart,
            keepUrl,
          },
        }
      : null;

  // Only the preview of THIS draft knows which parts Omni would redo (built
  // on a version, the reused ones cost nothing).
  const omniParts = preview.current ? shown?.preview.omni_parts : undefined;
  const cost = scene ? costOf(scene, omniParts) : null;
  // Next, added on top of a version, is drawn on its frames: the server's
  // once they are ready, the version's stills until then.
  const baseFrames =
    stack && scene && baseVersion
      ? scene.chunks.map(
          (c, j) => baseState.info?.chunks.find((x) => x.index === c.index)?.frame_url ?? stillOf(baseVersion, j),
        )
      : null;
  // A draft that would make a version that already exists: the same layers,
  // each drawn where that version has it (the preview on screen says where
  // the draft's unpinned ones go).
  const same =
    draftOk && draft && building
      ? sameAsVersion(draft, building, kept, knownVersions, (el) =>
          pins ? previewPatternOf(pins.preview, pins.recipe, el.id)?.layout ?? null : null,
        )
      : null;
  // Carried on from a version (changed, or added to), its icons are planned
  // again: when they come out other than it froze them, "Keep v3's icons"
  // keeps them exactly (the server then plans nothing: props.keep).
  const baseFrozen = building && baseVersion ? frozenIconsOf(baseVersion, knownVersions) : null;
  const baseIconEl = baseVersion ? shownIconOf(baseVersion, knownVersions) : null;
  const draftIconEl = draftOk && draft ? iconsOf(draft) : null;
  const iconsAsked =
    !!draft && !!baseIconEl && !!draftIconEl && iconsOn(draft) &&
    layerSig(draftIconEl, draft.colours) === layerSig(baseIconEl, draft.colours);
  const keptIcons = draftOk && baseVersion && keptFrom(draft) === baseVersion.id;
  const iconsMoved =
    !!baseFrozen && iconsAsked && !keptIcons && iconPlan.current && iconPlan.plan?.state === "ready" &&
    !iconPlan.plan.provisional && iconsDiffer(iconPlan.plan, baseFrozen);
  // The draft IS the version it carries on from (nothing changed, its icons
  // the same — and over the same video: sameAsVersion never matches a flat
  // redo to a version with Omni's video under it): Next is that version's
  // own picture, and Generate would only make it again. Added to, only a
  // version made in house is its draft exactly (its layers are drawn again
  // over the same video: Play gives its own render).
  const isBase = !!same && !!building && same.id === building.version_id && !iconsMoved;
  const nextStill =
    isBase && baseVersion && scene && (building?.mode === "rebuild" || housePass(baseVersion))
      ? scene.chunks.map((_, j) => stillOf(baseVersion, j))
      : null;
  // Carried on from a version: the rows still its own, "from v3".
  const fromNo = useMemo(() => {
    if (building?.mode !== "rebuild" || !baseVersion || !draftOk || !draft) return null;
    const out: Record<string, number> = {};
    for (const [id, vid] of Object.entries(inheritedFrom(draft, baseVersion, knownVersions))) {
      const n = knownVersions.findIndex((x) => x.id === vid) + 1;
      if (n > 0) out[id] = n;
    }
    return out;
  }, [building?.mode, baseVersion, draftOk, draft, knownVersions]);
  // A version added on top of another, while it is on its way (or once it
  // failed or was stopped), with no frames of its own to show: the version
  // under it, per part — never the original's, which would look as if that
  // version's graphics were lost.
  const underFrames = (v: HarnessVersion): (string | null)[] | null => {
    if (v.base?.mode !== "stack" || !scene) return null;
    const under = versions.find((x) => x.id === v.base?.version_id);
    return under ? scene.chunks.map((_, j) => stillOf(under, j)) : null;
  };

  // --- the icons on the stage -------------------------------------------------------
  const firstPattern = draft ? enabledPatterns(draft)[0] : undefined;
  // The first pattern's colour: the icons' "Pattern" swatch, and the ink on
  // a white fill — added on top of a version with no new pattern, the first
  // of the version's own.
  const keptColour =
    stack && baseVersion ? keptRows(kept, knownVersions, baseVersion.id, { catalogue, introColour })[0]?.colour ?? null : null;
  const patternColour =
    (draft && firstPattern ? colourOf(firstPattern, draft, { catalogue, introColour }) : null) ?? keptColour;
  // The scene's own colours, one click each in an icon's own colour: each
  // pattern's (by its name), then the intro's.
  const sceneColours = useMemo(() => {
    const out: { label: string; colour: string }[] = [];
    for (const el of draft ? enabledPatterns(draft) : []) {
      const c = colourOf(el, draft!, { catalogue, introColour });
      if (c) out.push({ label: entryOf(catalogue, slugOf(el))?.name ?? COPY.icons.colourChip.pattern, colour: c });
    }
    if (introColour) out.push({ label: COPY.icons.colourIntro, colour: introColour });
    return out;
  }, [draft, catalogue, introColour]);
  // A plan that keeps a version's icons ("Keep v3's icons") has them on
  // that version's tracks, as its own timeline shows them.
  const iconItems = keptItemLanes(
    speechOrder(iconPlan.plan?.items),
    iconPlan.plan?.kept?.version_id,
    knownVersions,
    rules.gap_user,
  );
  /** An icon picked in the list or on the lane (the one thing picked): the
   *  stage goes to where it shows (paused), so its handles are there to take.
   *  Off the Icons tab, Next rests on that moment (its exact frame), as a
   *  click on the lane does: the design still shows no icon. */
  const pickIcon = (id: string | null) => {
    pickIconOnly(id);
    const it = id ? iconItems.find((x) => glyphId(x.glyph) === id) : null;
    if (!it || player.playing) return;
    const now = sel.kind !== "next" || playerMode || atMoment ? player.tRef.current : -1;
    if (now >= it.from && now < it.to) return;
    const at = momentOf(it);
    if (sel.kind === "next") seekStage(at);
    else player.seek(at);
  };
  // An icon just added (the picker): picked, and the stage taken to it, once
  // the plan that shows it is in (an add always shows: elements/icon).
  const addedRef = useRef<string | null>(null);
  useEffect(() => {
    const id = addedRef.current;
    if (!id || !iconPlan.current || !iconItems.some((it) => glyphId(it.glyph) === id)) return;
    addedRef.current = null;
    pickIcon(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [iconPlan.plan, iconPlan.current]);
  const iconFill = iconPlan.plan?.colours?.fill ?? "#7f2bf2";
  const iconInk = iconPlan.plan?.colours?.ink ?? "#ffffff";
  // What the icons are drawn over: the scene, or the version added to.
  const underVersion = stack && baseVersion ? baseVersion : null;
  const underlay = iconPlan.plan?.underlay ?? null;
  // What an icon dragged by hand is outlined by: the plan's speaker samples.
  const coverSamples = useSpeakerSamples(iconsMode ? underlay?.masks : null);
  const underlayUrl =
    underlay?.video_url ?? (underVersion ? omniArm(underVersion)?.url ?? null : scene?.video_url ?? null);
  const overVersion = underlay ? underlay.kind === "version" : !!underVersion;
  const iconCard =
    iconsMode && !iconPlan.plan ? (
      <div className="absolute inset-x-3 top-[28%] flex justify-center">
        <div className="w-full max-w-[18rem] rounded-2xl bg-black/75 px-4 py-3 text-xs text-white shadow-lift backdrop-blur-sm">
          {iconPlan.error ? (
            <div role="alert" className="space-y-2">
              <p className="font-medium">{iconPlan.error.retryable ? COPY.icons.errorTitle : COPY.icons.refusedTitle}</p>
              <p className="text-2xs text-white/75">{iconPlan.error.message}</p>
              {iconPlan.error.retryable && (
                <button type="button" className="btn btn-xs btn-ghost text-white" onClick={iconPlan.retry}>
                  {COPY.tryAgain}
                </button>
              )}
            </div>
          ) : (
            <>
              <p className="mb-1.5 font-medium">{COPY.icons.warmTitle}</p>
              <ul className="space-y-1 text-2xs">
                {(iconPlan.warming?.steps.length ? iconPlan.warming.steps : [{ name: "words", state: "running" }, { name: "spots", state: "queued" }, { name: "director", state: "queued" }]).map((s) => (
                  <li key={s.name} className={`flex items-center gap-2 ${s.state === "queued" ? "text-white/50" : ""}`}>
                    <span aria-hidden className="w-3.5 text-center">
                      {s.state === "done" ? "✓" : s.state === "failed" ? "!" : s.state === "running" ? "…" : "·"}
                    </span>
                    {COPY.icons.step[s.name] ?? s.name}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      </div>
    ) : null;
  // A draft made in house: its patterns as the engine moves them (the
  // preview's own, while it is the draft's), for the quick stand-in.
  const housePreview = houseDraft && preview.current && shownFits ? shown?.preview.house ?? null : null;
  const houseStatus = houseDraft
    ? {
        exact: !!exactUrl,
        progress: houseRender.render?.progress ?? null,
        // Stopped even when asked for again: said, not spun on.
        failed: !!houseRender.error || houseRender.render?.state === "failed" || houseRender.render?.state === "cancelled",
      }
    : null;
  // Next in motion — the one StagePlayer: the icons on the Icons tab, and a
  // draft made in house played from its still. One video, one clock.
  const iconStage = playerMode ? (
    <StagePlayer
      plan={iconPlan.on ? iconPlan.plan : null}
      planRecipe={iconPlan.shown?.recipe ?? null}
      draft={draft}
      videoUrl={underlayUrl}
      exactUrl={exactUrl}
      poster={(underVersion ? baseFrames?.[0] : null) ?? frame0}
      patternUrl={housePreview ? null : underlay?.pattern_url ?? null}
      layers={housePreview?.layers ?? null}
      frameSize={housePreview ? { width: housePreview.width, height: housePreview.height, fps: housePreview.fps } : null}
      camera={cameraTrack}
      // The scene's matte bank (the very cut the video makes), else the plan's.
      masks={(houseDraft ? cameraTrack?.masks : null) ?? underlay?.masks ?? null}
      iconsBaked={!!underlay?.baked}
      player={player}
      moment={moment}
      showSpots={iconsMode && showSpots}
      selected={iconsMode ? selIcon : null}
      rules={rules}
      patternColour={patternColour}
      card={iconsMode ? iconCard : null}
      fonts={iconCat.catalogue?.fonts ?? null}
      onPick={iconsMode ? pickIconOnly : undefined}
      onBox={
        iconsMode && draft
          ? (id, box, { resized, key, size }) =>
              edit(setBox(draft, id, box, resized ? size : undefined), key ? { coalesce: `box:${id}` } : undefined)
          : undefined
      }
      coverSamples={coverSamples}
    />
  ) : null;
  // Above the picture while Next plays from its still (not on the Icons
  // tab): what plays, and the way back to the still.
  const playerHeader = (
    <>
      <IconStageStatus moment={moment} caption="" updating={false} video={houseStatus} />
      <button
        type="button"
        title={COPY.stillTitle}
        onClick={() => {
          player.pause();
          setNextPlaying(false);
          setAtMoment(false);
        }}
        className="btn btn-xs btn-ghost"
      >
        {COPY.still}
      </button>
    </>
  );
  // Above Next's picture while it rests on a moment: which, and the way back.
  const momentHeader = stillAtMoment || (atMoment && houseDraft && sel.kind === "next" && !playerMode) ? (
    <>
      <span className="inline-flex items-center gap-1 text-2xs text-muted" role="status" title={COPY.time.atMomentTitle}>
        {moment.loading ? (
          <>
            <Spinner size="xs" tone="accent" /> {COPY.time.loadingMoment}
          </>
        ) : shownMoment ? (
          <span className="font-medium text-accent-hover">✓ {COPY.time.atMoment(player.t)}</span>
        ) : (
          <span>{clock(player.t)}</span>
        )}
      </span>
      <button type="button" title={COPY.stillTitle} onClick={() => setAtMoment(false)} className="btn btn-xs btn-ghost">
        {COPY.still}
      </button>
    </>
  ) : null;
  // Above the picture (never on it): what the stage shows, and Show spots.
  const spotsToggle = (
    <>
      <IconStageStatus
        moment={moment}
        caption={
          overVersion
            ? COPY.icons.captionVersion(baseNo)
            : isHouse(draft)
              ? COPY.icons.captionSceneHouse
              : COPY.icons.captionScene
        }
        updating={iconPlan.updating}
        video={houseStatus}
      />
      <button
        type="button"
        aria-pressed={showSpots}
        title={COPY.icons.showSpotsTitle}
        onClick={() => setShowSpots((v) => !v)}
        className={`btn btn-xs gap-1 ${showSpots ? "btn-secondary" : "btn-ghost"}`}
      >
        <MapPin className="h-3 w-3" aria-hidden /> {COPY.icons.showSpots}
      </button>
    </>
  );
  // One line, scrolled sideways when they don't fit: the picture keeps its
  // size however many icons there are. Each says the icon's one time (when
  // it has settled, as the list and the notes do); a click rests the stage
  // on its exact still, a moment later.
  const momentsRow =
    iconItems.length > 0 ? (
      <div className="flex min-w-0 max-w-full flex-nowrap items-center gap-1.5 overflow-x-auto">
        <span className="shrink-0 text-2xs text-muted">{COPY.icons.moments}</span>
        {iconItems.map((it, i) => {
          const id = glyphId(it.glyph);
          const at = momentOf(it);
          return (
            <button
              key={id}
              type="button"
              aria-label={COPY.icons.momentAria(i + 1, it.label, fmtSec(iconTime(it)))}
              title={COPY.icons.exactTitle}
              onClick={() => {
                pickIconOnly(id);
                player.seek(at);
              }}
              className={`inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-2xs transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 ${
                selIcon === id ? "border-accent bg-accent-fade text-accent-hover" : "border-border text-fg hover:border-border-strong"
              }`}
            >
              <span aria-hidden>{CIRCLED[i] ?? i + 1}</span>
              <IconGlyph
                glyph={it.glyph}
                d={it.d}
                size={14}
                look={{ style: it.style ?? "badge", fill: it.fill ?? iconFill, ink: it.ink ?? iconInk, weight: it.weight }}
              />
              {it.label}
              <span className="font-mono tabular-nums text-muted">{fmtSec(iconTime(it))}</span>
            </button>
          );
        })}
      </div>
    ) : null;
  // A box of the user's over the speaker (the plan says so): bring it to the
  // front (where it stays off her face), or move it to the clear spot
  // nearest to it — said under the picture, where the hand is.
  const selItem = iconItems.find((it) => glyphId(it.glyph) === selIcon) ?? null;
  const handNote =
    iconsMode && draft && selItem?.covered && selItem.layer !== "front" ? (
      <p role="status" className="flex max-w-full flex-wrap items-center justify-center gap-x-2 gap-y-1 text-2xs text-amber-800">
        <AlertTriangle className="h-3 w-3 shrink-0" aria-hidden />
        {selItem.covered === "face" ? COPY.icons.coveredFace(selItem.label) : COPY.icons.coveredSpeaker(selItem.label)}
        {selItem.covered === "speaker" && (
          <button
            type="button"
            className="btn btn-xs btn-ghost"
            title={COPY.icons.bringFrontTitle}
            onClick={() => edit(setLook(draft, glyphId(selItem.glyph), { layer: "front" }))}
          >
            {COPY.icons.bringFront}
          </button>
        )}
        {selItem.clear ? (
          <button
            type="button"
            className="btn btn-xs btn-ghost"
            title={COPY.icons.moveClearTitle}
            onClick={() => edit(setBox(draft, glyphId(selItem.glyph), selItem.clear!))}
          >
            {COPY.icons.moveClear}
          </button>
        ) : (
          <span className="text-muted">{COPY.icons.noClear}</span>
        )}
      </p>
    ) : null;
  const iconPlanNow = iconPlan.plan;
  // The speech shows as soon as the words are in, before the icons are.
  const timeline = iconPlanNow?.timeline ?? iconPlan.warming?.timeline ?? null;
  // Next IS the version it carries on from (its own still): the playhead is
  // at that still's moment.
  const shownT = !playerMode && !atMoment && nextStill && baseFrozen ? versionStillAt(baseFrozen, stillAt) : stageT;

  // --- the scene's tracks: the same lanes on every tab, whatever is picked ---------
  // The icons' lane while the server draws icons: the plan's blocks, or one
  // thin row saying why there are none.
  const iconRows = iconRowsOf(iconItems.map((it) => ({ from: it.from, to: it.to, lane: it.lane })));
  const iconLane: IconLaneState = !iconsServed
    ? { kind: "none" }
    : !draftOk || !draft
      ? { kind: "placing", words: false }
      : draftIconEl && draftIconEl.enabled && iconProps(draftIconEl).omni_own === "keep"
        ? { kind: "omni" }
        : !iconsOn(draft)
          ? bookend && !draftIconEl
            ? { kind: "empty" }
            : { kind: "off" }
          : iconPlanNow
            ? { kind: "ready", words: !!timeline, rows: iconRows.count }
            : iconPlan.error
              ? { kind: "error", words: !!timeline, retryable: iconPlan.error.retryable }
              : { kind: "placing", words: !!timeline };
  // The version added to: its pattern layers (never an icon element of its
  // recipes), locked, before the draft's own.
  const keptPatterns = keptOn.filter((e) => e.kind === "pattern");
  const tracks = timelineTracks({
    kept: houseDraft ? keptPatterns : [],
    patterns: houseDraft && draft ? draft.elements : [],
    icons: iconLane,
  });
  /** A layer's time by its own entrance: a kept layer's, a version's (never
   *  the draft preview's word on a pattern that happens to share its id). */
  const ownTimingOf = (el: HarnessElement): Timing | null => {
    if (sceneDuration <= 0) return null;
    const entrance =
      el.animation.entrance !== "auto" ? el.animation.entrance : entryOf(catalogue, slugOf(el))?.default_animation ?? "fade";
    return timingOn(el, { duration: sceneDuration, entrance });
  };
  /** One pattern's lane: its name, look and time. */
  const lanePattern = (key: string, el: HarnessElement, locked: boolean, colour: string | null, tm: Timing | null): LanePattern => {
    const entry = entryOf(catalogue, slugOf(el));
    return {
      key,
      el,
      locked,
      name: entry?.name ?? (slugOf(el) || COPY.icons.colourChip.pattern),
      thumb: entry ? previewSrc(entry) : null,
      colour,
      full: goesFull(el, catalogue),
      timing: tm,
    };
  };
  // A bar per pattern on the timeline (made in house): the version's kept
  // layers (added on top of one) first, locked, then the draft's own.
  const lanePatterns: LanePattern[] =
    draft && houseDraft && sceneDuration > 0
      ? tracks.lanes.flatMap((l) => {
          const el =
            l.kind !== "pattern" || l.index == null ? undefined : l.locked ? keptPatterns[l.index] : draft.elements[l.index];
          if (!el) return [];
          const colour = colourOf(el, draft, { catalogue, introColour });
          return [lanePattern(l.key, el, l.locked, colour, l.locked ? ownTimingOf(el) : timingOf(el))];
        })
      : [];
  /** A bar let go (or a key): the span it lands on; the stage goes to where
   *  it is fully in when the picture's moment no longer shows it (a drag:
   *  always — the entrance just set is what the owner looks at). */
  const onLaneSpan = (id: string, span: Span, key: boolean) => {
    if (!draft || sceneDuration <= 0) return;
    const next = setSpan(draft, id, span, sceneDuration, pins);
    if (!edit(next, key ? { coalesce: `span:${id}` } : undefined)) return;
    const el = next.elements.find((e) => e.id === id);
    const tm = el ? timingOf(el) : null;
    if (tm && !player.playing && (!key || !showsAt(tm, stageT))) seekStage(focusTime(tm));
  };
  // Opening a pattern's row shows it: when the picture's moment doesn't, the
  // stage goes to where it is fully in.
  useEffect(() => {
    if (!openRow || !draft || !houseDraft || sel.kind !== "next" || playerMode) return;
    const el = draft.elements.find((e) => e.id === openRow && e.kind === "pattern");
    const tm = el ? timingOf(el) : null;
    if (tm && !showsAt(tm, stageT)) seekStage(focusTime(tm));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openRow]);
  const wordTimes = (timeline?.words ?? []).filter((w) => w.said && typeof w.s === "number").map((w) => w.s as number);
  // The scene's end: a grip in every view (once the server has it), an
  // undo step when it moves (keyboard steps join).
  const facts = endInfo ? { auto: endInfo.auto, extent: endInfo.extent } : null;
  const grip: EndGrip | null =
    facts && end && onEnd
      ? {
          frame: endNow(endUser, facts),
          user: endUser,
          auto: facts.auto,
          extent: facts.extent,
          snaps: endSnaps(facts, { parts: partStarts, words: wordTimes, voicedEnd: timeline?.voiced?.[1] ?? null }),
          busy: fullBusy,
          onEnd: (f, nudge) => history.setEnd(f, nudge ? { coalesce: "end" } : undefined),
          onDrag: (f) => {
            onEndDragRef.current?.(target, f);
            if (f != null && sel.kind === "next" && pastCut(f)) wantFull();
          },
        }
      : null;
  // The status line under the lanes (one line, always there): a drop put
  // back, else the picked icon off its word (with the way back), else the
  // picked icon moved off its word (or the moment it was added at) out of a
  // zone (with "Show at 0.2 s anyway": that moment, else its word, timed by
  // hand: anywayOf).
  const offWord = sel.kind === "next" && houseDraft && iconLane.kind === "ready" && selItem?.off_word ? selItem : null;
  const shifted =
    sel.kind === "next" && houseDraft && iconLane.kind === "ready" && selItem?.shifted && !selItem.off_word ? selItem : null;
  const lineButton =
    "shrink-0 font-medium underline-offset-2 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40";
  const timelineStatus = laneNote ? (
    <span role="status" className="block truncate text-red-700">
      {laneNote}
    </span>
  ) : offWord && draft ? (
    <span role="status" className="flex min-w-0 items-center gap-2 text-amber-800">
      <span className="truncate">{COPY.icons.offWordNote(offWord.label, fmtSec(iconTime(offWord)))}</span>
      <button type="button" className={lineButton} onClick={() => edit(snapBack(draft, glyphId(offWord.glyph)))}>
        {COPY.icons.snapBack}
      </button>
    </span>
  ) : shifted?.shifted && draft ? (
    <span role="status" className="flex min-w-0 items-center gap-2 text-amber-800">
      <span className="truncate">
        {shifted.label}: {COPY.icons.shifted(fmtSec(iconTime(shifted)), shifted.shifted.why)}
      </span>
      <button
        type="button"
        className={lineButton}
        title={COPY.icons.showAnywayTitleOf(shifted.shifted)}
        onClick={() => edit(pinOffWord(draft, glyphId(shifted.glyph), anywayOf(shifted.shifted!).sec))}
      >
        {COPY.icons.showAnyway(fmtSec(anywayOf(shifted.shifted).label))}
      </button>
    </span>
  ) : null;
  // "Snap to words" (the icons' blocks), at the status line's end.
  const snapToggle =
    iconLane.kind === "ready" ? (
      <label className="inline-flex shrink-0 cursor-pointer items-center gap-1.5 text-muted" title={COPY.icons.snapWordsTitle}>
        <input
          type="checkbox"
          checked={snapWords}
          onChange={(e) => setSnapWords(e.target.checked)}
          className="h-3 w-3 accent-accent"
        />
        {COPY.icons.snapWords}
      </label>
    ) : null;
  // What is picked brings its lane into view, inside the timeline.
  const laneFocus = selIcon && iconLane.kind === "ready" ? ICON_LANE_KEY : openRow ? patternLaneKey(openRow) : null;
  /** The icons' lane on Next: its blocks, or why there are none. */
  const iconLaneOf = (width: number): React.ReactNode => {
    if (!draft) return null;
    const note = (children: React.ReactNode, tone: "muted" | "danger" = "muted") => (
      <IconLaneNote width={width} duration={sceneDuration} timeline={timeline} onSeek={seekStage} tone={tone}>
        {children}
      </IconLaneNote>
    );
    const act = `${lineButton} text-accent-hover`;
    switch (iconLane.kind) {
      case "none":
        return null;
      case "off":
        return note(
          <>
            <span className="truncate">{COPY.icons.laneOff}</span>
            <button type="button" className={act} onClick={() => edit(setIconsEnabled(draft, true))}>
              {COPY.icons.laneTurnOn}
            </button>
          </>,
        );
      case "omni":
        return note(
          <>
            <span className="truncate">{COPY.icons.keepingOmni}</span>
            <button type="button" className={act} onClick={() => edit(setOmniOwn(draft, "hide"))}>
              {COPY.icons.useOurs}
            </button>
          </>,
        );
      case "empty":
        return note(
          <>
            <span className="truncate">{COPY.icons.laneEmpty}</span>
            <button
              type="button"
              className={act}
              onClick={(e) => openPush({ kind: "icons", mode: "add", item: null }, e.currentTarget)}
            >
              {COPY.icons.add}
            </button>
          </>,
        );
      case "placing":
        return note(
          <>
            <Spinner size="xs" tone="accent" />
            <span className="truncate">{COPY.icons.placing}</span>
          </>,
        );
      case "error":
        return note(
          <>
            <span className="truncate">
              {iconLane.retryable ? COPY.icons.errorTitle : COPY.icons.refusedTitle} {iconPlan.error?.message ?? ""}
            </span>
            {iconLane.retryable ? (
              <button type="button" className={act} onClick={iconPlan.retry}>
                {COPY.tryAgain}
              </button>
            ) : (
              <button
                type="button"
                className={act}
                onClick={() => {
                  setSavedTab("icons");
                  setPush(null);
                }}
              >
                {COPY.icons.openIcons}
              </button>
            )}
          </>,
          "danger",
        );
      case "ready":
        return (
          <IconLane
            width={width}
            duration={sceneDuration}
            timeline={timeline}
            items={iconItems}
            fill={iconFill}
            ink={iconInk}
            selected={selIcon}
            onSelect={pickIcon}
            onSeek={seekStage}
            rules={rules}
            snapOn={snapWords}
            onSpan={(item, span, kind, snappedW, lane) => {
              if (!iconPlanNow) return;
              // Moved in time, and up or down to another track with it: one edit.
              const timed = timeByHand(draft, iconPlanNow, item, span, kind, snappedW, rules);
              edit(lane != null ? setLane(timed, glyphId(item.glyph), lane) : timed, { coalesce: `span:${glyphId(item.glyph)}` });
              // Moved, or its start: the stage goes to it, fully in, where it now is.
              if (kind !== "end" && !player.playing) {
                seekStage(Math.min(span.from + rules.in_f / rules.fps + (rules.moment_after ?? 0.4), span.to - 0.05));
              }
            }}
            onLane={(item, lane) => edit(setLane(draft, glyphId(item.glyph), lane))}
            onStep={(item, dir) => {
              const s = iconPlanNow ? stepMention(draft, iconPlanNow, item, dir) : null;
              if (s) edit(s.recipe);
            }}
            onRemove={(item) => {
              if (selIcon === glyphId(item.glyph)) setSelIcon(null);
              edit(removeIcon(draft, item.glyph));
            }}
            ghost={iconChoice?.ghost ?? null}
            busy={iconPlan.updating}
          />
        );
    }
  };
  /** Next's lanes, in lib/rebrandTracks's order: the patterns' (kept, then
   *  the draft's), "Add a pattern" when it has none, the icons'. */
  const draftLanes = (width: number) => (
    <>
      {lanePatterns.length > 0 && (
        <PatternLanes
          // Its own instance (never the read-only one's): leaving Next
          // unmounts it, which clears what it said on the status line.
          key="draft"
          width={width}
          duration={sceneDuration}
          patterns={lanePatterns}
          selected={openRow}
          onSelect={(id, how) => {
            pickPattern(id);
            // A click opens its row in the rail; a focus (a tab onto it, a
            // grab) only picks it. The timeline stays as it is either way.
            // Leaving the Icons tab's motion, the stage rests on the moment
            // the click goes to (the lanes seek there next), not the still.
            const pick = patternPick(how, iconsMode && !!houseDraft);
            if (pick.openRow) {
              setPush(null);
              setSavedTab("patterns");
            }
            if (pick.restOnMoment) setAtMoment(true);
          }}
          onSpan={onLaneSpan}
          onRamps={(id, r) => draft && edit(setRamps(draft, id, r), { coalesce: `ramps:${id}` })}
          onGoTo={seekStage}
          points={[shownT, ...partStarts, ...wordTimes]}
          clash={(id, span) => (draft ? clashOf(withKept(draft, keptPatterns), catalogue, id, span) : null)}
          onNote={setLaneNote}
        />
      )}
      {tracks.lanes.some((l) => l.kind === "add") && (
        <PatternAddRow
          laneKey={ADD_LANE_KEY}
          disabled={!catalogue}
          onAdd={(trigger) => {
            if (catalogue) openPush({ kind: "library", mode: "add", id: null }, trigger);
          }}
        />
      )}
      {iconLaneOf(width)}
    </>
  );

  // On Original and a version: the same lanes, read-only. A version's own
  // patterns (as its run drew them: its colours) and the icons it froze,
  // each on the track the draft it was made from had it on; the original
  // has none.
  const roVersion = sel.kind === "version" ? version : null;
  const roLayers = roVersion
    ? effectiveLayersOf(roVersion, knownVersions).filter((l) => l.kind === "pattern" && l.enabled)
    : [];
  const roIcons =
    roVersion && iconsServed
      ? frozenIconBlocks(
          frozenIconsOf(roVersion, knownVersions),
          frozenLanesOf(roVersion, knownVersions, rules.gap_user),
        )
      : [];
  const roTracks = timelineTracks({
    kept: [],
    patterns: roLayers,
    icons: !iconsServed
      ? { kind: "none" }
      : roIcons.length
        ? { kind: "ready", words: false, rows: iconRowsOf(roIcons).count }
        : { kind: "empty" },
    readOnly: true,
  });
  const roRows = roVersion ? keptRows(roLayers, knownVersions, roVersion.id, { catalogue, introColour }) : [];
  const roPatterns: LanePattern[] = roTracks.lanes.flatMap((l) => {
    const el = l.kind === "pattern" && l.index != null ? roLayers[l.index] : undefined;
    if (!el || l.index == null) return [];
    const row = roRows[l.index];
    const lp = lanePattern(l.key, el, true, row?.colour ?? null, ownTimingOf(el));
    return [row?.name ? { ...lp, name: row.name } : lp];
  });
  const toPart = (t: number) => setPart(partAt(t));
  const readOnlyLanes = (width: number) => (
    <>
      {roPatterns.length > 0 && (
        <PatternLanes
          key="read-only"
          width={width}
          duration={sceneDuration}
          patterns={roPatterns}
          selected={null}
          onSelect={() => undefined}
          onSpan={() => undefined}
          onRamps={() => undefined}
          onGoTo={toPart}
          clash={() => null}
          readOnly
        />
      )}
      {roTracks.lanes.some((l) => l.kind === "empty") && <PatternAddRow laneKey={ADD_LANE_KEY} onAdd={null} />}
      {roTracks.lanes.some((l) => l.kind === "icons") &&
        (roIcons.length ? (
          <IconLaneStatic duration={sceneDuration} blocks={roIcons} />
        ) : (
          <IconLaneNote width={width} duration={sceneDuration} onSeek={toPart}>
            <span className="truncate">{COPY.icons.laneNone}</span>
          </IconLaneNote>
        ))}
    </>
  );
  const timelineBlock =
    !scene || sceneDuration <= 0 ? null : sel.kind === "next" && houseDraft ? (
      <SceneTimeline
        duration={sceneDuration}
        t={shownT}
        onSeek={seekStage}
        parts={partStarts}
        cuts={scene.cuts_sec ?? []}
        grip={grip}
        status={timelineStatus}
        aside={snapToggle}
        focus={laneFocus}
      >
        {draftLanes}
      </SceneTimeline>
    ) : (
      // Original or a version (its lanes, read-only), or Next while its
      // draft is got ready: the ruler picks the part that holds the moment.
      <SceneTimeline
        duration={sceneDuration}
        t={partStarts[partIndex - 1] ?? 0}
        onSeek={toPart}
        parts={partStarts}
        cuts={scene.cuts_sec ?? []}
        grip={grip}
      >
        {sel.kind === "next" ? undefined : readOnlyLanes}
      </SceneTimeline>
    );

  // The ✓: what goes in the reel (the stage's word for this scene).
  const cards: FilmCard[] = [
    {
      key: "original",
      sel: { kind: "original" },
      label: COPY.original,
      sub: COPY.captionOriginalSub,
      thumb: frame0,
      inReel: pick == null,
    },
    ...versions.map((v, i): FilmCard => {
      const ph = versionPhase(v);
      // "on v2" / "from v2" once it is made; the status while it isn't.
      const lineage = lineageOf(v, versions);
      return {
        key: `v${v.id}`,
        sel: { kind: "version", id: v.id },
        label: COPY.version(i + 1),
        sub: ph === "queued" ? COPY.waitingTag : (lineage ?? (ph === "ready" ? "ready" : undefined)),
        runningSince: ph === "running" ? startedAtOf(v) : undefined,
        thumb:
          stillOf(v, 0) ??
          sentTargets.current.get(v.id)?.[0] ??
          (ph === "ready" ? null : underFrames(v)?.[0] ?? frame0),
        busy: ph === "queued" || ph === "running",
        problem: ph === "failed" ? "failed" : ph === "stopped" ? "stopped" : undefined,
        tag: recipeOfVersion(v) ? undefined : COPY.classicTag,
        stars: v.rating?.omni_stars ?? null,
        inReel: pick === v.id,
      };
    }),
  ];
  const shownFirst = shownFits ? shown?.preview.chunks[0] : undefined;
  const nextCard: FilmCard = {
    key: "next",
    sel: { kind: "next" },
    label: COPY.next,
    sub: building && baseNo ? (building.mode === "stack" ? COPY.nextOn(baseNo) : COPY.nextFrom(baseNo)) : COPY.nextSub,
    thumb: nextStill?.[0] ?? (shownFirst ? shownFirst.target_url ?? shownFirst.frame_url : null) ?? baseFrames?.[0] ?? frame0,
    // A change of words only (icons, the note) leaves the picture as it is.
    busy: (busy && preview.change?.kind !== "words") || (!!prepared && !shown && preview.status !== "error"),
    dashed: true,
  };

  const backChip = backTo
    ? {
        label:
          backTo.kind === "version"
            ? COPY.backTo(versions.findIndex((v) => v.id === backTo.id) + 1)
            : COPY.backToOriginal,
        onClick: () => choose(backTo),
      }
    : null;

  // --- the rail's content ---------------------------------------------------------
  let panel: React.ReactNode = null;
  if (!draft) {
    panel = (
      <div className="space-y-2" aria-busy="true">
        <Skeleton h="h-12" w="w-full" rounded="rounded-xl" />
        <Skeleton h="h-8" w="w-32" rounded="rounded-lg" />
      </div>
    );
  } else if (tab === "patterns") {
    panel = (
      <PatternsTab
        kept={
          stack && baseVersion && baseNo
            ? {
                no: baseNo,
                rows: keptRows(kept, knownVersions, baseVersion.id, { catalogue, introColour }),
                elements: keptOn,
                onChangeIt: () => buildOn(baseVersion, "rebuild"),
              }
            : null
        }
        recipe={draft}
        fromNo={fromNo}
        catalogue={catalogue}
        catalogueError={catalogueError}
        onRetryCatalogue={retryCatalogue}
        preview={preview}
        introColour={introColour}
        openId={openRow}
        onOpen={pickPattern}
        onEdit={(r, o) => edit(r, o)}
        onLibrary={(mode, id, trigger) => catalogue && openPush({ kind: "library", mode, id }, trigger)}
        focusId={focusRow}
        onFocused={clearFocusRow}
        duration={sceneDuration > 0 ? sceneDuration : null}
        layers={!noPerson}
      />
    );
  } else if (tab === "icons") {
    panel = (
      <IconsTab
        recipe={draft}
        plan={iconPlan}
        catalogue={iconCat.catalogue}
        rules={rules}
        patternColour={patternColour}
        selected={selIcon}
        onSelect={pickIcon}
        onEdit={(r) => edit(r)}
        frame={scene ? { w: scene.width, h: scene.height } : null}
        onAdd={(trigger) => openPush({ kind: "icons", mode: "add", item: null }, trigger)}
        onChange={(item, trigger) => openPush({ kind: "icons", mode: "swap", item }, trigger)}
        onPlayFrom={(item) => {
          toNext();
          player.seek(Math.max(0, item.from - 0.8), { play: true });
        }}
        choice={iconChoice}
        onChoice={setIconChoice}
        omniOwn={
          building && baseVersion && baseNo && parentOmniIcons
            ? { no: baseNo, mode: building.mode, onChangeIt: () => buildOn(baseVersion, "rebuild") }
            : null
        }
        onVersion={stack && baseVersion && baseNo && shownIconOf(baseVersion, knownVersions) ? baseNo : null}
        keptNo={keptIcons && baseNo ? baseNo : null}
        sceneColours={sceneColours}
        byHand={bookend}
        layers={!noPerson}
        onServerReorder={async (glyph, to) => {
          const d = draftRef.current;
          if (!d) return null;
          try {
            const p = await harnessApi.iconPlan(runId, target, d, { base: baseRef.current, reorder: { glyph, to }, view });
            return p.state === "ready" ? p.reorder ?? null : null;
          } catch {
            return null;
          }
        }}
      />
    );
  } else if (tab === "style") {
    panel = (
      <StyleTab
        recipe={draft}
        catalogue={catalogue}
        introColour={introColour}
        pins={pins}
        onTop={stack ? baseNo : null}
        onEdit={edit}
        onSeePrompt={(trigger) => openPush({ kind: "prompt" }, trigger)}
      />
    );
  }

  let pushView: React.ReactNode = null;
  if (push?.kind === "library" && catalogue && draft) {
    const swapId = push.id;
    pushView = (
      <PatternLibrary
        mode={push.mode}
        catalogue={catalogue}
        // The kept layers count towards the cap and "full goes alone".
        recipe={withKept(draft, keptOn)}
        swapId={swapId}
        onBack={() => closePush()}
        onPatternsChanged={(patterns: RebrandPattern[]) => setCatalogue((c) => (c ? { ...c, patterns } : c))}
        duration={sceneDuration > 0 ? sceneDuration : null}
        onAdd={(slugs) => {
          const { recipe, ids } = addPatterns(draft, slugs, catalogue, keptOn, {
            duration: sceneDuration > 0 ? sceneDuration : null,
            pins,
          });
          edit(recipe);
          setPush(null);
          setOpenRow(null);
          setFocusRow(ids[0] ?? null);
        }}
        onSwap={(slug) => {
          if (!swapId) return;
          edit(swapPattern(draft, swapId, slug, { catalogue, duration: sceneDuration > 0 ? sceneDuration : null, pins }));
          closePush(rowDomId(swapId));
        }}
      />
    );
  } else if (push?.kind === "prompt") {
    pushView = <PromptView shown={shown} current={preview.current} onBack={() => closePush()} />;
  } else if (push?.kind === "icons" && draft) {
    pushView = (
      <IconPicker
        mode={push.mode}
        item={push.item}
        recipe={draft}
        plan={iconPlan.plan}
        current={!!iconPlan.current}
        catalogue={iconCat.catalogue}
        onEdit={(r) => edit(r)}
        // Added at the playhead: one nothing said is tied to shows nearest it.
        at={shownT}
        onAdded={(id) => (addedRef.current = id)}
        suggested={!bookend}
        onSeen={(ids) => noteSeenIdeas(draftKey, ids)}
        onBack={() =>
          closePush(push.mode === "swap" && push.item ? iconRowId(glyphId(push.item.glyph)) : undefined)
        }
      />
    );
  }

  // --- the footer ------------------------------------------------------------------
  const cantGenerate = baseRefused && baseNo ? COPY.baseCantGenerate(baseNo) : COPY.cantGenerate;
  // A fresh draft of icons and no pattern still sends every part to Omni
  // (the opening alone): said with its price, and the $0 way to icons.
  const iconsNoPattern = !building && !!draft && draftOk && enabledPatterns(draft).length === 0 && iconsOn(draft);
  // Made in house, Generate costs nothing either.
  const house = isHouse(draft);
  // Carried on from vN and nothing changed yet: Generate would make vN again.
  const baseAgain = isBase && !!baseNo && !active && !refused && !nothingNew;
  const canGenerate = canGenerateAny && !baseAgain;
  canGenerateRef.current = canGenerate;
  const wholeWords = whole === "failed" ? COPY.wholeFailed : COPY.wholeWaiting;
  const status = active
    ? COPY.onTheWay(noOf(active))
    : refused
      ? cantGenerate
      : wholeWait
        ? wholeWords
      : baseAgain && baseNo
        ? COPY.thisIsBase(baseNo, nextNo)
      : nothingNew && baseNo
        ? COPY.addALayer(baseNo)
        : emptyBookend
          ? COPY.bookendEmpty
        : iconsFailed
          ? iconsFailed.retryable
            ? `${COPY.icons.placingFailed} ${iconsFailed.message}`
            : COPY.icons.placingRefused
          : placingIcons
            ? COPY.icons.placing
            : house
              ? COPY.houseCost(building?.mode ?? null, baseNo)
              : iconsOnly
              ? COPY.icons.restampCost
              : cost && building && baseNo
                ? COPY.costOn(building.mode, baseNo, cost.minutes, cost.dollars)
                : cost && iconsNoPattern && cost.dollars > 0
                  ? COPY.iconsNoPatternCost(cost.minutes, cost.dollars)
                  : cost
                    ? COPY.cost(cost.minutes, cost.dollars)
                    : "";
  // The icons' plan failed: its Try again (or the Icons tab, for a refusal).
  const statusAction =
    wholeWait && whole === "failed" && !active && !refused
      ? { label: COPY.tryAgain, onClick: wantFull }
      : iconsFailed && !active && !refused && !wholeWait && !(nothingNew && baseNo)
      ? iconsFailed.retryable
        ? { label: COPY.tryAgain, onClick: iconPlan.retry }
        : { label: COPY.icons.openIcons, onClick: () => { setSavedTab("icons"); setPush(null); } }
      : null;
  const offer =
    same && !active && !refused && !nothingNew && !baseAgain
      ? { no: noOf(same), onOpen: () => choose({ kind: "version", id: same.id }) }
      : null;

  // A version made for another end than the scene's now (longer or
  // shorter): said on its picture, and made again to this end — its own
  // recipe, made in house, free (one Omni made is made in house).
  const versionEnd = version ? versionEndOf(version) : null;
  const endChip =
    grip && facts && version && versionPhase(version) === "ready" && recipeOfVersion(version) && versionEnd !== endUser
      ? {
          label: COPY.endsAt(endSeconds(versionEnd ?? endInfo?.auto ?? endInfo?.frames ?? facts.extent)),
          title: active ? COPY.onTheWay(noOf(active)) : COPY.endsAtTitle,
          disabled: !!active || posting,
          onRemake: () => {
            const r = recipeOfVersion(version);
            if (!r || active) return;
            if (madeInHouse(version)) void run(asHouse(r), version.base ?? null, null, true);
            else makeInHouse(version);
          },
        }
      : null;

  const resetToDefault = () => {
    if (defaultRecipe) edit(defaultRecipe, { base: null });
  };
  const isDefault = !building && isUntouchedDefault(draft, defaultRecipe);
  // A new scene's own draft: the 42 default with automatic icons.
  const isStarting = !building && !isDefault && isStartingDraft(draft, defaultRecipe);
  // Edits made this visit that no version has (the stage asks once, before
  // Finalize, whether to generate them first).
  const unmade = draftOk && !isDefault && !isStarting && !same && editedThisVisit(draftKey);
  const onDraftRef = useRef(onDraftState);
  onDraftRef.current = onDraftState;
  useEffect(() => {
    onDraftRef.current?.(target, unmade);
  }, [unmade, target]);
  const headerActions = (
    <div className="flex items-center gap-0.5">
      {!bookend && (isDefault || isStarting) && (
        <span
          title={isDefault ? COPY.defaultPillTitle : COPY.defaultIconsPillTitle}
          className="mr-1.5 hidden items-center rounded-full border border-accent/30 bg-accent-fade px-2 py-0.5 text-3xs font-semibold text-accent-hover sm:inline-flex"
        >
          {isDefault ? COPY.defaultPill : COPY.defaultIconsPill}
        </span>
      )}
      <HeaderButton label={COPY.undo} shortcut="Ctrl+Z" disabled={!history.canUndo} onClick={undo}>
        <Undo2 className="h-4 w-4" aria-hidden />
      </HeaderButton>
      <HeaderButton label={COPY.redo} shortcut="Ctrl+Shift+Z" disabled={!history.canRedo} onClick={redo}>
        <Redo2 className="h-4 w-4" aria-hidden />
      </HeaderButton>
      <OverflowMenu label={COPY.menu} size="sm">
        {!bookend && (
          <MenuItem onClick={resetToDefault} disabled={!defaultRecipe || isDefault}>
            {COPY.reset}
          </MenuItem>
        )}
        <MenuItem onClick={() => setCameraDebug((v) => !v)} Icon={Crosshair} title={COPY.cameraMenuTitle}>
          {cameraDebug ? COPY.cameraHide : COPY.cameraShow}
        </MenuItem>
      </OverflowMenu>
    </div>
  );

  const footer = (
    <GenerateBar
      nextNo={nextNo}
      status={status}
      offer={offer}
      disabled={!canGenerate}
      disabledTitle={
        active
          ? COPY.onTheWay(noOf(active))
          : refused
            ? cantGenerate
            : wholeWait
              ? wholeWords
            : baseAgain && baseNo
              ? COPY.thisIsBaseTitle(baseNo)
            : nothingNew && baseNo
              ? COPY.addALayer(baseNo)
              : emptyBookend
                ? COPY.bookendEmpty
              : iconsFailed
                ? status
                : placingIcons
                  ? COPY.icons.placing
                  : undefined
      }
      posting={posting}
      error={generateError}
      onDismissError={clearError}
      onGenerate={() => void generate()}
      working={((wholeWait && whole !== "failed") || (placingIcons && !iconsFailed)) && !active && !refused}
      statusAction={statusAction}
      notice={iconPlan.notice ? { text: iconPlan.notice, onDismiss: iconPlan.dismissNotice } : null}
    />
  );

  return (
    <div ref={rootRef} data-target={target} className="flex min-h-0 flex-1 flex-col">
      <h2 className="sr-only">{COPY.studio}</h2>
      <span className="sr-only" role="status" aria-live="polite">
        {announce}
      </span>
      {/* The pill, undo, redo and menu: on the stage's top row from lg up,
          else at the top of the studio (they scroll away with it). */}
      {actionsSlot ? (
        createPortal(headerActions, actionsSlot)
      ) : (
        <div className="flex shrink-0 justify-end px-3 pt-2">{headerActions}</div>
      )}
      {prepareError ? (
        <div className="m-auto max-w-sm space-y-3 px-6 py-16 text-center" role="alert">
          <p className="text-sm text-fg">{COPY.prepareFailed}</p>
          <p className="text-2xs text-muted">{prepareError}</p>
          <button type="button" className="btn btn-sm btn-ghost" onClick={retryPrepare}>
            {COPY.retry}
          </button>
        </div>
      ) : (
        <div className="grid min-h-0 flex-1 grid-cols-1 grid-rows-[auto_auto_minmax(0,1fr)] [grid-template-areas:'stage'_'strip'_'rail'] lg:grid-cols-[48px_minmax(0,1fr)_360px] lg:grid-rows-[minmax(0,1fr)] lg:[grid-template-areas:'strip_stage_rail']">
          <StudioFilmstrip
            className={`px-3 pb-2 [grid-area:strip] lg:flex lg:px-2 lg:pb-2 ${push ? "hidden" : "flex"}`}
            cards={cards}
            next={nextCard}
            sel={sel}
            onSelect={choose}
            portrait={scene ? scene.height >= scene.width : true}
            nextPulse={nextPulse}
          />
          <StudioStage
            className="[grid-area:stage] lg:bg-bg-deep"
            scene={scene}
            preparingSince={startedAt}
            sel={sel}
            part={partIndex}
            onPart={setPart}
            preview={preview}
            changeText={changeText}
            cameraDebug={cameraDebug}
            camera={sceneCamera}
            instantHide={!!draft && draftOk && enabledPatterns(draft).length === 0}
            selectedBox={selectedBox}
            version={version}
            versionNo={version ? noOf(version) : null}
            versionLineage={version ? lineageOf(version, versions) : null}
            sentTargets={version ? sentTargets.current.get(version.id) ?? null : null}
            versionBaseFrames={version ? underFrames(version) : null}
            baseFrames={baseFrames}
            shownFits={shownFits}
            nextSub={
              building && baseNo
                ? building.mode === "stack"
                  ? COPY.captionNextOn(baseNo)
                  : COPY.captionNextFrom(baseNo)
                : undefined
            }
            baseNote={stack && baseNo && baseState.loading ? COPY.gettingBase(baseNo) : null}
            baseError={
              building && baseVersion && baseNo && baseRefused
                ? {
                    title: COPY.baseRefused(baseNo),
                    actions: (
                      <>
                        {building.mode === "stack" && (
                          <button type="button" className="btn btn-xs btn-ghost" onClick={() => buildOn(baseVersion, "rebuild")}>
                            {COPY.changeInstead}
                          </button>
                        )}
                        <button type="button" className="btn btn-xs btn-ghost" onClick={leaveBuild}>
                          {COPY.leaveBuild(baseNo)}
                        </button>
                      </>
                    ),
                  }
                : null
            }
            actions={
              version && rating === version.id ? (
                <RateVersion
                  key={version.id}
                  version={version}
                  no={noOf(version)}
                  onSaved={(row) => adopt({ ...version, ...row })}
                  onClose={() => setRating(null)}
                />
              ) : version ? (
                <VersionActions
                  version={version}
                  no={noOf(version)}
                  busy={!!active}
                  building={!!building && building.version_id === version.id}
                  onBuildOn={() => buildOn(version, continueMode(version, knownVersions) ?? "rebuild", true)}
                  onRemake={() => void remakes.start(version.id)}
                  remaking={remakes.state[version.id] ?? null}
                  onStop={() => void cancel(version)}
                  onRate={() => setRating(version.id)}
                  // New work is made in house: a version Omni drew a symbol
                  // on is made in house (no symbol there) — its "Clean it up"
                  // (Omni's clip again) is not offered.
                  onMakeInHouse={() => makeInHouse(version)}
                  confirmFresh={
                    freshAsk && freshAsk.versionId === version.id
                      ? { onGo: () => void freshAsk.go(), onCancel: () => setFreshAsk(null) }
                      : null
                  }
                  onTryAgain={() => {
                    // The same recipe, on the same version when it was built
                    // on one: again (offered on one made in house that
                    // didn't finish — Omni's are made in house instead).
                    const r = recipeOfVersion(version);
                    if (r && !active) {
                      void run(asHouse(r), version.base ?? null, sentTargets.current.get(version.id) ?? null, true, null,
                        version);
                    }
                  }}
                  onRestamp={
                    madeInHouse(version)
                      ? () => {
                          // Made in house: its own recipe again, the icons
                          // planned afresh — nothing is added on top of it.
                          const r = recipeOfVersion(version);
                          if (r && !active) void run(asHouse(r), version.base ?? null, null, true);
                        }
                      : restampOf(version) && !stackFull(version)
                      ? () => {
                          // Its icons alone, laid on it again, made in house
                          // over its own video — no Omni, $0 (planned afresh
                          // on the server, so its answer is the plan sent).
                          const r = restampOf(version);
                          if (r && !active) void run(asHouse(r), { version_id: version.id, mode: "stack" }, null, true);
                        }
                      : undefined
                  }
                />
              ) : null
            }
            strip={timelineBlock}
            iconStage={iconStage}
            headerExtra={iconsMode ? spotsToggle : playerHeader}
            nextExtra={momentHeader}
            underIcons={
              iconsMode && (momentsRow || handNote) ? (
                <>
                  {momentsRow}
                  {handNote}
                </>
              ) : null
            }
            nextStill={atMoment && shownMoment ? [shownMoment] : nextStill}
            onPlayNext={
              houseDraft && !iconsMode && preview.current && !!scene?.video_url
                ? () => {
                    // From the moment it rests on (the playhead), else the start.
                    const from = atMoment ? player.tRef.current : 0;
                    setNextPlaying(true);
                    player.seek(from, { play: true });
                  }
                : null
            }
            liveColour={liveColour}
            handSpec={handSpec}
            handGesture={!!handLive}
            holdLive={atMoment && moment.loading}
            patternHandles={patternHandles}
            onPickPattern={houseDraft && tab === "patterns" && draftOk ? pickPattern : null}
            backTo={backChip}
            canUndo={history.canUndo}
            onUndo={undo}
            onReset={defaultRecipe && !isDefault && !bookend ? resetToDefault : undefined}
            compact={!!push}
            originalEnd={cut != null ? cut / FPS : null}
            endChip={endChip}
          />
          <StudioRail
            className="border-t border-border [grid-area:rail] lg:border-l lg:border-t-0"
            tabs={tabs.map((t) => ({
              id: t.id,
              label: COPY.tabs[t.id],
              // Icons: how many the plan shows.
              badge:
                t.id === "icons"
                  ? iconPlan.on
                    ? iconPlan.plan?.items.length
                    : undefined
                  : draft && t.count
                    ? t.count(draft)
                    : undefined,
            }))}
            tab={tab}
            onTab={(t) => {
              setSavedTab(t);
              setPush(null);
            }}
            panel={panel}
            push={pushView}
            bar={
              building && baseVersion && baseNo && draft ? (
                <BuildOnBar
                  no={baseNo}
                  nextNo={nextNo}
                  mode={building.mode}
                  onMode={(m) => buildOn(baseVersion, m)}
                  onLeave={leaveBuild}
                  house={flatHouse}
                  onStartOver={startOver}
                  startOverTitle={bookend ? COPY.startOverEmptyTitle : undefined}
                  iconsMoved={iconsMoved ? { onKeep: () => edit(keepIcons(draft, baseVersion.id)) } : null}
                  iconsKept={!!keptIcons}
                  addBlocked={
                    stackFull(baseVersion) ? COPY.addDisabled(baseNo) : flatHouse ? COPY.addHouse(baseNo) : null
                  }
                  addText={
                    flatHouse
                      ? undefined
                      : housePass(baseVersion)
                        ? COPY.addExplainPass(baseNo, groundNo)
                        : COPY.addExplainOmni(baseNo)
                  }
                  changeText={
                    flatHouse
                      ? COPY.changeExplainHouse
                      : housePass(baseVersion)
                        ? COPY.changeExplainPass(groundNo)
                        : COPY.changeExplainOmni
                  }
                  base={baseState}
                  loaded={loaded?.id === baseVersion.id}
                  continued={loaded?.id === baseVersion.id && loaded.auto}
                  onUndoLoaded={undo}
                  onDismissLoaded={() => setLoaded(null)}
                />
              ) : lookup === "failed" || dropped ? (
                <div className="shrink-0 border-b border-border px-3 py-2">
                  {lookup === "failed" ? (
                    <Notice
                      tone="warning"
                      action={
                        <button type="button" className="btn btn-xs btn-ghost" onClick={retryLookup}>
                          {COPY.retry}
                        </button>
                      }
                    >
                      {COPY.baseUnchecked}
                    </Notice>
                  ) : (
                    <Notice onDismiss={() => setDropped(null)}>{COPY.baseDropped(dropped?.no ?? null)}</Notice>
                  )}
                </div>
              ) : null
            }
          />
        </div>
      )}
      {/* Generate: the studio's own footer row (sticky at the bottom of the
          screen while the stacked studio scrolls, below lg). */}
      <footer className="sticky bottom-0 z-10 shrink-0 border-t border-border bg-bg lg:static">
        <div className="flex items-center gap-3 bg-bg-muted/50 px-4 py-3">{footer}</div>
      </footer>
    </div>
  );
}
