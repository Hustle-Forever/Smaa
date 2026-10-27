// Rebrand studio — every word the studio shows, in one place.
//
// Short, plain and friendly, and each fact said once. The speaker is "the
// speaker" or "they". Omni never sees any of this: a hex code, a number or
// "42" in here is for the owner's eyes only.

import type { HarnessPatternLayout } from "../../../lib/rebrandHarnessApi";
import type { ChangeKind } from "../../../lib/rebrandRecipe";

const plural = (n: number, one: string, many = `${one}s`) => (n === 1 ? one : many);
const usd = (n: number) => `$${n.toFixed(2)}`;

export const COPY = {
  // the studio's own heading (read by screen readers; the stage's scene
  // tabs say which scene)
  studio: "Rebrand studio",

  // header
  defaultPill: "42 default",
  defaultPillTitle: "The approved setup, untouched",
  defaultIconsPill: "42 default + icons",
  defaultIconsPillTitle: "The approved setup, with icons picked from what they say",
  undo: "Undo",
  redo: "Redo",
  menu: "More",
  reset: "Reset to 42 default",
  // a check for the in-house engine: frame 0's box drawn where the camera
  // takes that part of the room on each frame
  cameraShow: "Show camera tracking",
  cameraHide: "Hide camera tracking",
  cameraMenuTitle: "Draw how the camera moves over the scene — play the original to see it",

  // opening the scene
  gettingReady: "Getting the scene ready",
  gettingReadyHint: "The first time takes about 20 s.",
  prepareFailed: "Couldn't get the scene ready.",
  retry: "Retry",
  catalogueFailed: "Couldn't load the pattern list.",

  // filmstrip
  stripLabel: "What the stage shows",
  original: "Original",
  next: "Next",
  nextSub: "your change",
  version: (n: number) => `v${n}`,
  classicTag: "classic",
  waitingTag: "Waiting",
  failedTag: "Failed",
  stoppedTag: "Stopped",
  cardAria: (name: string, status: string) => (status ? `${name}, ${status}` : name),
  // the ✓ on what goes in the reel
  inReel: "In the reel",

  // stage captions
  captionNext: "Next",
  captionNextSub: "free preview",
  captionNextOn: (n: number) => `on top of v${n}`,
  captionNextFrom: (n: number) => `from v${n}'s settings`,
  nextOn: (n: number) => `on v${n}`,
  nextFrom: (n: number) => `from v${n}`,
  captionOriginal: "Original",
  captionOriginalSub: "as shot",
  captionVersionSub: (seconds: number | null) => (seconds ? `ready · ${Math.round(seconds)} s` : "ready"),
  captionRunning: "on the way",
  captionFailed: "didn't finish",
  captionStopped: "stopped",
  showing: (what: string, part: number, parts: number) =>
    parts > 1 ? `Showing ${what}, part ${part} of ${parts}` : `Showing ${what}`,
  holdBefore: "Hold: before",
  holdBeforeTitle: "Hold to see the frame without your change (Space works too)",
  showingBefore: "Before",
  backTo: (n: number) => `Back to v${n}`,
  backToOriginal: "Back to the original",
  play: "Play",
  playAgain: "Play again",
  playTitle: (what: string) => `Play ${what}`,
  still: "Still",
  stillTitle: "Back to the still frame",
  // Next in motion, made in house: the exact video, or a quick stand-in
  // until it is made
  stage: {
    exact: "Exact",
    exactVideo: "Exact video",
    exactTitle: "The very video Generate will make — Generate copies it, so it's instant",
    quick: "Quick preview",
    quickTitle: "Drawn roughly while the exact video is made — the pattern's motion is only approximate",
    making: (p: number | null) =>
      p != null && p > 0 ? `making the exact video… ${Math.round(p * 100)}%` : "making the exact video…",
    exactFailed: "the exact video couldn't be made here — Generate still makes it",
    playNextTitle: "Play your change: the exact video once it's made, a quick preview until then",
  },
  enters: (label: string) => `Enters: ${label.toLowerCase()}`,
  entersMany: (n: number) => `${n} patterns enter`,
  // Show camera tracking (the stage's chip)
  cameraWorking: (done: number, total: number) =>
    total > 0 ? `Following the camera… ${Math.round((100 * done) / total)}%` : "Following the camera…",
  cameraZoom: (zoom: number) => `Camera ×${zoom.toFixed(2)}`,
  cameraHeld: (sec: number) => `held from ${clock(sec)}`,
  cameraStill: "The camera couldn't be followed: patterns hold still",
  cameraFailed: "Couldn't follow the camera",
  cameraBox: "Where the start's room is now",

  // the preview's own states
  updated: "Updated",
  checking: "Checking…",
  wordsChip: "Updating the words…",
  slowHint: "Taking a little longer than usual.",
  firstTitle: "Getting your preview ready",
  stepSpeaker: "Finding the speaker",
  stepCamera: "Following the camera",
  stepCameraFailed: "Couldn't follow the camera, so patterns will hold still",
  stepColours: "Reading the intro colours",
  stepColoursFailed: "No intro colour, so 42 violet is used",
  stepPattern: "Laying in the pattern",
  errorTitle: "Couldn't update the preview.",
  refusedTitle: "This mix can't be made:",
  tooLong: "This is taking too long.",
  serverError: "Something went wrong on the server.",
  offline: "Couldn't reach the server.",
  notLatest: "Not your latest change",
  tryAgain: "Try again",

  // versions
  making: (n: number, house = false) => (house ? `Making v${n} in house` : `Omni is making v${n}`),
  queued: (n: number) => `v${n} is waiting to start`,
  failed: (n: number) => `v${n} didn't finish`,
  // a failed version with no reason of Omni's own
  noVideoBack: "Omni didn't send a video back — Try again.",
  // ... or of its own, made in house
  noVideoHouse: "The video didn't finish — Try again.",
  stopped: (n: number) => `v${n} was stopped`,
  noVideo: "The video isn't here any more.",
  stop: "Stop",
  download: "Download",
  // "Add more to v3": carry on from a version (after Generate the studio
  // does it by itself)
  buildOn: (n: number) => `Add more to v${n}`,
  buildOnTitle: (n: number) => `Carry on from v${n}: everything you change makes a new version`,
  buildingOn: (n: number) => `Adding to v${n}`,
  makesNext: (next: number) => `everything you change here makes v${next}`,
  // a version made in house whose video is gone from the server
  remake: "Make it again",
  remakeTitle: "Its video isn't here any more — made again from its recipe and the original: the same video, free, about 20 s",
  remaking: (p: number | null) => (p != null && p > 0 ? `Making it again… ${Math.round(p * 100)}%` : "Making it again…"),
  remakeFailed: "Couldn't make it again:",
  remakeUnfinished: "it didn't finish — try once more.",
  lostNote: (n: number) => `v${n}'s video isn't on the server any more — make it again, free.`,
  buildOnClassic: "Made in the classic panel, so there's no recipe to build on",
  retryVersion: "Try again",
  retryVersionTitle: "Make it again with the same settings — a fresh take",
  // a version Omni made, redone by our own renderer (new work is made in house)
  makeInHouse: "Make it in house",
  makeInHouseTitle:
    "Redo it with our own renderer, from the original — $0, about 20 s. The patterns come out flat, not painted in.",
  // a version Omni drew its own symbol on
  cleanUp: "Clean it up",
  cleanUpTitle: "Make it again without what Omni drew on its own — reuses its clip, $0",
  // Try again on a version with icons, when they'd come out differently now
  freshIcons: "The icons will be placed fresh — keep going?",
  keepGoing: "Keep going",
  cancel: "Cancel",
  versionMenu: (n: number) => `More for v${n}`,
  // rating a version (its ⋯ menu)
  rate: "Rate",
  rateAgain: "Change rating",
  rateTitle: (n: number) => `Rate v${n}`,
  rateStars: "How good is it?",
  starAria: (n: number) => `${n} star${n > 1 ? "s" : ""}`,
  ratePlaceholder: "A note (optional): what worked, what didn't",
  saveRating: "Save",
  savingRating: "Saving…",
  rated: (n: number) => `v${n} rated`,
  // Omni is switched off, so no brief is written any more: a rating is
  // saved with its version, whoever made it
  ratedHint: "Saved with the version.",
  rateFailed: "Couldn't save the rating",
  ratedAria: (stars: number) => `rated ${stars} of 5`,
  // lineage: "v3 · on v2" (added on top), "v3 · from v2" (changed)
  lineage: (mode: "stack" | "rebuild", n: number | null) =>
    `${mode === "stack" ? "on" : "from"} ${n ? `v${n}` : "an older version"}`,

  // building on a version (the bar at the top of the rail)
  addToIt: "Add to it",
  changeIt: "Change it",
  buildMode: "How to build on it",
  leaveBuild: (n: number) => `Stop building on v${n}`,
  addExplain: (n: number) => `Keeps v${n} exactly as it is and adds on top.`,
  changeExplain: "Redone from the original, so kept layers can look a bit different.",
  addDisabled: (n: number) => `v${n} is stacked as high as it goes — change it instead`,
  // new work is made in house: on a version Omni made, and on one made in house
  addExplainOmni: (n: number) => `Keeps v${n}'s video exactly as it is and adds yours on top — made in house, $0.`,
  changeExplainOmni: "Redone in house from the original — the patterns come out flat, not painted in.",
  changeExplainHouse: "Redrawn from the original with everything it has — change anything, $0.",
  addHouse: (n: number) => `v${n} was made in house — Change it keeps all of it and adds yours: the same picture`,
  // made in house on top of a version Omni made: Omni's video stays under it
  addExplainPass: (n: number, under: number | null) =>
    `Keeps v${n} exactly as it is — Omni's video${under ? ` from v${under}` : ""} stays underneath — and adds yours on top, $0.`,
  changeExplainPass: (under: number | null) =>
    `Redone in house from the original — Omni's look${under ? ` from v${under}` : ""} comes out flat, not painted in.`,
  gettingBase: (n: number) => `Getting v${n} ready…`,
  loadedFrom: (n: number) => `Loaded v${n}'s settings.`,
  // after Generate: the studio carried on from the new version by itself
  continuedFrom: (n: number) => `Carrying on from v${n}.`,
  // one click back to a fresh scene: the 42 default + automatic icons
  startOver: "Start over from the original",
  startOverTitle: "The 42 default with automatic icons, on the original — Undo brings this back",
  // an intro or outro: done by hand, it starts empty
  startOverEmptyTitle: "An empty draft, on the original — Undo brings this back",
  bookendEmpty: "Add a pattern or an icon",
  // the draft IS the version it carries on from
  thisIsBase: (n: number, next: number) => `This is v${n} — add or change something to make v${next}.`,
  thisIsBaseTitle: (n: number) => `The draft is exactly v${n}: change something first`,
  // a re-plan of the icons came out other than the version froze them
  iconsMoved: (n: number) => `The icons came out differently from v${n}'s this time.`,
  keepIcons: (n: number) => `Keep v${n}'s icons`,
  keepIconsTitle: "Show exactly its icons, where and when it has them — until you change one",
  iconsKept: (n: number) => `v${n}'s icons, exactly as they are — change one and they're placed afresh.`,
  changeInstead: "Change it instead",
  baseRefused: (n: number) => `Can't build on v${n} right now:`,
  // the version a kept draft was built on can't be found
  baseDropped: (n: number | null) =>
    `${n ? `v${n} isn't` : "The version you were adding to isn't"} available any more — your new layers are now a fresh draft.`,
  baseUnchecked: "Couldn't check the version this draft builds on, so it's a fresh draft for now.",
  baseCantGenerate: (n: number) => `Can't build on v${n} right now — see the stage.`,
  newLayers: "New",
  nothingNew: (n: number) => `Nothing new yet — add a pattern to put on v${n}.`,
  keptIn: (n: number) => `In v${n} (kept)`,
  keptTitle: "Kept exactly as it is",
  keptFrom: (n: number) => `from v${n}`,
  toRemoveIt: "To remove it:",
  // what the stage says while the draft moves onto (or off) a version
  drawingOn: (n: number) => `Putting it on v${n}…`,
  loadingFrom: (n: number) => `Loading v${n}'s settings…`,
  drawingFresh: "Back to the original…",

  // the rail
  railLabel: "Settings",
  tabs: { patterns: "Patterns", icons: "Icons", style: "Style" },
  back: "Back",

  // patterns
  noPatterns: "No patterns — Omni rebrands without one.",
  noPatternsHouse: "No patterns — just the icons, over the untouched video.",
  addPatterns: "Add patterns",
  changePattern: "Change pattern",
  capReached: (n: number) => `Up to ${n} ${plural(n, "pattern")}`,
  rowActions: (name: string) => `${name}: more`,
  swap: "Swap pattern",
  anotherSpot: "Another spot",
  anotherSpotTitle: "Draw another of the places that fit",
  remove: "Remove",
  show: (name: string) => `Show ${name}`,
  hide: (name: string) => `Hide ${name}`,
  hidden: "Hidden",
  place: "Place",
  layer: "Layer",
  layerBehind: "Behind the character",
  layerFront: "In front of the character",
  colour: "Colour",
  motion: "Motion",
  placeChip: {
    auto: "Auto",
    "edge-left": "Left",
    "edge-right": "Right",
    "edge-top": "Top",
    "edge-bottom": "Bottom",
    full: "Full",
  } as Record<string, string>,
  fullGoesAlone: "Full-screen patterns go alone",
  pickColour: "Pick",
  pickColourTitle: "Any colour",
  colourAuto: "Auto",
  colourOwn: "Own colour",
  autoTitle: (hex: string | null) => (hex ? `The palette's colour (${hex}) — set in Style` : "The palette's colour — set in Style"),
  moved: "Its own place was taken, so it moved",
  overlaps: "Overlaps another pattern — try Another spot",
  placing: "Placing…",
  // a pattern moved, sized or turned by hand (the handles on the picture)
  hand: {
    byHand: "By hand",
    byHandShort: "by hand",
    backToAuto: "Back to automatic",
    backToAutoTitle: "Let it be placed automatically again",
    hint: "Drag it on the picture to move it, a corner to size it, the turn button to turn it.",
    hintFull: "Drag the picture to move it, − / + to zoom, the turn button to turn it. It always fills the frame.",
    size: "Size",
    turn: "Turn",
    // the round purple button beside the pattern on the picture
    turnButton: (name: string) => (name ? `Turn ${name}` : "Turn"),
    turnButtonTitle: "Drag to turn · Shift: 15° steps",
    sizeValue: (pct: number) => `${pct}%`,
    turnValue: (deg: number) => `${deg}°`,
    reset: "Reset",
    resetSizeTitle: "Back to its own size",
    resetTurnTitle: "Back to upright",
    follow: "Camera",
    followOptions: [
      { value: "auto", label: "Automatic" },
      { value: "room", label: "Moves with the camera" },
      { value: "screen", label: "Holds still on screen" },
    ] as { value: "auto" | "room" | "screen"; label: string }[],
    followHelp: "In the picture it moves with the camera's push-in; on the letterbox bars it holds still.",
    handlesLabel: (name: string) => `${name}: move, size and turn it`,
    handlesHint:
      "Drag to move · a corner to size · the turn button to turn · arrows nudge, + / − size, [ / ] turn, 0 back to automatic",
    panLabel: (name: string) => `${name}: pan, zoom and turn it`,
    panHint: "Drag to pan · − / + zoom · the turn button turns it · arrows nudge, [ / ] turn, 0 back to automatic",
    zoomIn: "Bigger",
    zoomOut: "Smaller",
    placing: (name: string) => `Placing ${name}…`,
    pickTitle: "Click a pattern on the picture to pick it",
  },
  // a pattern's time: the timeline under the stage, and the row's Time
  time: {
    section: "Time",
    shows: "Shows",
    wholeScene: "Whole scene",
    wholeSceneTitle: "Show it for the whole scene",
    from: "From",
    to: "to",
    fromLabel: (name: string) => `${name}: starts at (seconds)`,
    toLabel: (name: string) => `${name}: ends at (seconds)`,
    comesIn: "Comes in",
    goesOut: "Goes out",
    // An older server lists no exits: its own is the only one.
    exitOwn: "Its own",
    over: "over",
    inLabel: (name: string) => `${name}: how it comes in`,
    inSecLabel: (name: string) => `${name}: how long it takes to come in (seconds)`,
    outLabel: (name: string) => `${name}: how it goes out`,
    outSecLabel: (name: string) => `${name}: how long it takes to go out (seconds)`,
    seconds: "s",
    holdsToCut: "It lasts to the end of the scene, so it holds to the cut.",
    notShown: (name: string) => `${name} starts after this scene ends, so it doesn't show.`,
    collapsed: (from: number, to: number) => `${clock(from)}–${clock(to)}`,
    hint: "Drag its bar under the picture to move it, an end to trim it (at least half a second); the wedges are how long it takes to come in and go out.",
    // the timeline
    rulerLabel: "The scene's timeline",
    rulerTitle: "Click or drag to see that moment",
    lanesLabel: "The scene's tracks",
    // a lane with no pattern in it
    addPattern: "Add a pattern",
    addPatternTitle: "Pick one from the pattern library",
    noPatterns: "No patterns",
    cutMark: "The footage jumps to another shot here",
    partMark: (n: number) => `Part ${n} starts here`,
    // the scene's end grip
    endLabel: "Scene end",
    endTitle: "Drag to cut · double-click: automatic",
    flashMark: "Jumps to another shot here",
    flashBusy: "Getting these frames ready…",
    barAria: (name: string) => `${name}: when it shows`,
    barValue: (from: number, to: number) => `from ${clock(from)} to ${clock(to)}`,
    barTitle: (name: string) =>
      `${name} · drag to move it, an end to trim it · ←/→ move it, Shift+←/→ its end, Alt+←/→ its start · double-click: the whole scene`,
    startHandle: (name: string) => `Drag to change when ${name} starts (its end stays)`,
    endHandle: (name: string) => `Drag to change when ${name} ends`,
    inHandle: (name: string) => `Drag to change how long ${name} takes to come in`,
    outHandle: (name: string) => `Drag to change how long ${name} takes to go out`,
    live: (from: number, to: number) => `${clock(from)} – ${clock(to)}`,
    fullMark: "Full screen",
    keptMark: "kept",
    refused: (name: string) => `${name} fills the frame then, so nothing else can show with it — it was put back.`,
    refusedOther: (name: string) => `${name} fills the frame then — it was put back.`,
    clashLive: (name: string) => `${name} fills the frame then: nothing else can show with it.`,
    // the stage at the playhead's moment
    atMoment: (t: number) => `Exact frame · ${clock(t)}`,
    atMomentTitle: "The very frame Generate makes at this moment",
    loadingMoment: "Getting that moment…",
    goThere: (name: string) => `${name} isn't on screen here — go to where it shows`,
  },

  // the library
  libAdd: "Add patterns",
  libSwap: (name: string) => `Swap ${name}`,
  libAddHint: "Tap to pick one or more.",
  libSwapHint: "Tap one to put it in its place.",
  libAddButton: (n: number) => (n ? `Add ${n} ${plural(n, "pattern")}` : "Pick a pattern"),
  libUpload: "Upload",
  libUploadHint: "PNG, JPG or WebP",
  libUploading: (s: number) => `${s}s · usually 10–40 s`,
  libUploadFailed: "Couldn't add the pattern:",
  libFullCaption: "Full screen",
  libTransitionCaption: (sec: number) => `Transition · ${sec} s`,
  libDeleteTitle: "Delete this pattern?",
  libDeleteMessage: (name: string) => `“${name}” will no longer be offered for new rebrands.`,
  libDeleteConfirm: "Delete",
  libAdded: "Pattern added",
  libDeleted: "Pattern deleted",
  libDeleteFailed: "Couldn't delete the pattern",
  blocked: {
    cap: (n: number) => `Up to ${n}`,
    "full-alone": "Goes alone",
    "full-present": "A full-screen one is on",
  },
  blockedTitle: {
    cap: (n: number) => `${n} is the most a scene takes — remove one to add another.`,
    "full-alone": "Full-screen patterns go alone while they show — make room for it first.",
    "full-present": "A full-screen pattern is on the whole time, and it goes alone.",
  },
  // Why a hidden pattern's eye is off.
  showBlockedTitle: {
    "full-alone": "It fills the frame, so it goes alone — hide the others first.",
    "full-present": "A full-screen pattern is on — hide it first.",
  },

  // style
  palette: "Colours",
  paletteHint: "What every Auto colour uses.",
  paletteIntro: "From the intro",
  paletteBrand: "42 violet",
  paletteCustom: "Pick",
  feel: "Feel",
  note: "Note for Omni",
  notePlaceholder: "e.g. keep it calm and minimal",
  advanced: "Advanced",
  whiteFlash: "Clear the white flash",
  whiteFlashHint: "Removes the frosted panel Omni sometimes slides in with an entrance.",
  strayClean: "Clean up stray drawings",
  strayCleanHint: "Removes anything Omni adds outside your patterns and icons.",
  strayCleanOnTop: (n: number) => `Adding to v${n} keeps it as it is. To clean v${n} itself, use Change it.`,
  strayCleanNeedsIcons: "Only with a pattern and our icons on — otherwise Omni is asked to draw its own.",
  seePrompt: "See what Omni is told",
  // made in house: nothing to tell anyone
  houseStyle: "Made in house: nothing is painted over the footage, so there's no prompt, feel or note to set.",

  // what Omni is told
  promptTitle: "What Omni is told",
  promptOpening: "Opening",
  promptPart: (n: number) => `Part ${n}`,
  promptNone: "No pattern: Omni gets the opening only.",
  promptStale: "Showing the words for the last preview — your change is on its way.",
  promptEmpty: "The words appear with the first preview.",

  // generate
  generate: (n: number) => `Generate v${n}`,
  generateShortcut: "Ctrl+Enter",
  onTheWay: (n: number) => `v${n} is on the way — one at a time per scene.`,
  cost: (minutes: number, dollars: number) =>
    dollars > 0 ? `Preview is free · Omni takes ~${minutes} min (~$${dollars.toFixed(2)})` : "Preview is free · no Omni cost",
  // made in house: Generate is free too
  houseCost: (mode: "stack" | "rebuild" | null, n: number | null) =>
    mode && n
      ? `${mode === "stack" ? "Adds to" : "Changes"} v${n} · $0, made in house in about 20 s`
      : "Preview is free · Generate is $0 too, made in house in about 20 s",
  // built on vN: what Generate adds or redoes, from the parts Omni would redo
  costOn: (mode: "stack" | "rebuild", n: number, minutes: number, dollars: number) =>
    dollars > 0
      ? `${mode === "stack" ? "Adds to" : "Changes"} v${n} · ~${minutes} min (~$${dollars.toFixed(2)})`
      : `$0 · reuses v${n}`,
  // icons and no pattern, fresh: Omni still redraws every part
  iconsNoPatternCost: (minutes: number, dollars: number) =>
    `No pattern, but Omni still redraws the scene · ~${minutes} min (~$${dollars.toFixed(2)}). Icons alone are $0 on a finished version.`,
  addALayer: (n: number) => `Add a layer to put on v${n}`,
  sameAs: (n: number) => `That's v${n} —`,
  openIt: "open it",
  openItTitle: (n: number) => `v${n} already shows exactly this: see it, free`,
  cantGenerate: "This mix can't be made yet — see the stage.",
  // a version made for another end of the scene: made again to this one
  endsAt: (sec: string) => `Ends at ${sec} s · Remake`,
  endsAtTitle: "Made for another end of the scene — Remake makes it again to this end, free",
  // kept past the automatic cut: Generate waits for the whole clip
  wholeWaiting: "Getting the whole clip ready…",
  wholeFailed: "Couldn't get the whole clip ready.",
  generateFailed: "Couldn't start it:",

  // the scene's parts (a version Omni made in parts): marked on the timeline
  partTitle: "Omni edits the scene ≤10 s at a time",

  // --- icons ---------------------------------------------------------------
  icons: {
    auto: "Icons from what they say",
    autoOn: "The AI picks them from the script and lands each on its word.",
    autoOff: "Only the icons you add. The AI's ideas are in Add icon → Suggested.",
    off: "Icons are off.",
    turnOn: "Turn icons on",
    turnOff: "Turn icons off",
    order: "In speech order",
    orderAuto: "Auto",
    orderPinned: "Some are yours",
    resetOrder: "Auto ↺",
    resetOrderTitle: "Let the AI time them again",
    none: "No icons fit this scene yet — add one, or let the AI try with more to say.",
    noneAiOff: "No icons yet — add one.",
    add: "Add icon",
    count: (n: number, max: number) => `${n} of ${max} on screen`,
    addedCount: (n: number) => (n === 1 ? "1 added" : `${n} added`),
    done: "Done",
    capTitle: (n: number) => `Up to ${n} icons in a scene`,
    handle: (label: string) => `Move ${label} (drag, or Alt+Up / Alt+Down)`,
    rowAria: (label: string, at: string, word: string | null) =>
      word ? `${label}, ${at}, on “${word}”` : `${label}, ${at}`,
    onWord: (word: string) => `on “${word}”`,
    atTime: (s: string) => s,
    // An idea in a list not placed: when its word is said (one shown says
    // the time its icon shows at, as the icon list does: ideaTime).
    saidAt: (s: string) => `said at ${s}`,
    yours: "yours",
    pinned: "pinned",
    kept: "kept",
    ai: "AI",
    keyword: "keyword",
    offWord: "off its word",
    offWordPurpose: "Your timing — before or after it's said",
    // ✓ Keep on the AI's picks
    keepPickButton: "Keep",
    keepPickTitle: "Keep this one whatever the AI picks next — it becomes yours",
    keepPickAria: (label: string) => `Keep ${label}`,
    keptPick: "kept",
    keptPickTitle: "The AI's pick, kept by you — it stays whatever the AI picks next",
    // Why? (who picked it, what was said, where, what colour)
    whyTitle: "Why it's here",
    whyAria: (label: string) => `Why ${label} is here`,
    whyWho: (by: string, priority: number | null) =>
      by === "ai"
        ? priority
          ? `The AI picked it — a key idea, ${priority} of 5.`
          : "The AI picked it."
        : by === "kept"
          ? priority
            ? `The AI picked it (${priority} of 5) and you kept it — it stays whatever the AI picks next.`
            : "The AI picked it and you kept it — it stays whatever the AI picks next."
          : by === "keyword"
            ? "A keyword match — the AI wasn't asked."
            : by === "team"
              ? "Your team's icon, added by you."
              : by === "version"
                ? "Kept from the version, exactly as it was."
                : "You added it.",
    whySaid: (word: string, at: string | null, pinned: boolean) =>
      pinned
        ? `On “${word}”${at ? ` at ${at}` : ""} — the word you picked.`
        : `On “${word}”${at ? `, said at ${at}` : ""}.`,
    whyTimed: (at: string) => `At ${at} — the time you set.`,
    whyHelps: (purpose: string) => `Why it helps: ${purpose.replace(/[.\s]+$/, "")}.`,
    whyWhere: (by: string, text: string) =>
      by === "you"
        ? "Where: the place you put it."
        : by === "ai"
          ? `Where: the spot the AI chose${text ? ` — ${text.replace(/[.\s]+$/, "")}` : ""}.`
          : by === "spot"
            ? "Where: a clear spot of its own (the others' spot isn't clear while it shows)."
            : by === "version"
              ? "Where: where that version had it."
              : `Where: ${text.replace(/[.\s]+$/, "") || "the clearest spot beside the speaker"}.`,
    whyColour: (by: string, text: string) =>
      by === "you"
        ? "Colour: its own — your choice."
        : by === "version"
          ? "Colour: as that version had it."
          : `Colour: ${text.replace(/[.\s]+$/, "") || "the scene's"}.`,
    // Why an idea isn't on screen
    whyNot: {
      ai: "The AI left it out",
      check: "The AI picked it, a check left it out",
      silent: "The AI didn't judge it, so it's left out.",
      suggest: "The AI suggests it — shown only if you add it",
      keyword: "A keyword match",
      gone: "Not in the icon library any more",
      room: "Left out",
    } as Record<string, string>,
    hopeful: "Hopeful but general — shown only if you add it.",
    teamSaid: (at: string | null) => (at ? `Your team's icon — said at ${at}.` : "Your team's icon — said here."),
    snapBack: "Back on its word",
    notTied: "Not tied to anything they say —",
    pickWord: "Pick a word",
    pickWordLabel: (label: string) => `The word ${label} lands on`,
    needsWord: "Needs a word",
    rowMenu: (label: string) => `${label}: more`,
    change: "Change icon",
    remove: "Remove",
    playFrom: "Play from here",
    moved: (label: string, first: boolean, word: string, at: string) =>
      `${label} moved to ${first ? "first" : "later"}, on “${word}” at ${at}`,
    choiceBefore: (a: string, at: string, b: string) => `“${a}” is only said at ${at}, after “${b}”.`,
    choiceAfter: (a: string, at: string, b: string) => `“${a}” is only said at ${at}, before “${b}”.`,
    choiceDrop: (a: string, word: string) => `That's off “${word}”, where ${a} is said.`,
    anywayFirst: (a: string, at: string) => `Show ${a} first anyway · ${at}, off its word`,
    anywayAfter: (a: string, at: string) => `Show ${a} after anyway · ${at}, off its word`,
    anywayHere: (a: string, at: string) => `Show ${a} at ${at} anyway · off its word`,
    noRoomAnyway: "There's no room for it there, even off its word.",
    keepOrder: "Keep speech order",
    keepWord: "Keep it on its word",
    // yours that didn't land (the AI's other ideas are in Suggested)
    notOnScreen: "Not on screen",
    removeIdea: "Remove",
    state: {
      on: "On",
      no_room: "No room",
      zone: "Pattern moving",
      near_end: "Near the end",
      limit: "Over the limit",
      no_spot: "No clear spot",
      refused: "Refused by AI",
      dismissed: "Removed by you",
      available: "Suggested",
      gone: "Not in the library",
    } as Record<string, string>,
    look: "Look",
    where: "Where",
    whereChip: {
      auto: "Auto",
      left: "Left",
      right: "Right",
      top: "Top",
      bottom: "Bottom",
      topBar: "Top bar",
      bottomBar: "Bottom bar",
    } as Record<string, string>,
    colour: "Colour",
    colourChip: {
      auto: "Auto",
      violet: "42 violet",
      pattern: "Pattern",
      charcoal: "Charcoal",
      white: "White",
    } as Record<string, string>,
    patternColourUnknown: "The pattern's colour shows once the preview is made",
    size: "Size",
    style: "Style",
    styleCircle: "Circle",
    styleChip: "Circle + words",
    styleBadge: "Badge",
    styleGlyph: "Just the icon",
    styleOldTitle: "The look older versions have",
    enter: "Enter",
    enterChip: { glitch: "Glitch", pop: "Pop", fade: "Fade", slide: "Slide", none: "None", grow: "Grow" } as Record<string, string>,
    growTitle: "The circle pops, then opens out to its words",
    // one icon's own look
    allIcons: "All icons",
    resetAll: "Reset all to auto",
    resetAllTitle: "Every icon's own look and timing go back to automatic — what you added or removed stays",
    edited: "Changed by you",
    panelLabel: (label: string) => `${label}: its own look`,
    ownColour: "Colour",
    colourScene: "As the others",
    colourSceneTitle: (hex: string) => `The colour the other icons have (${hex})`,
    colourPurple: "42 purple",
    colourIntro: "Intro",
    glyphColour: "Icon colour",
    inkAuto: "Auto",
    inkAutoTitle: "White, or charcoal where white would be hard to read",
    words: "Words",
    wordsPlaceholder: (label: string) => label,
    useTheirWord: (word: string) => `Use their word “${word}”`,
    wordsHint: (n: number) => `Up to ${n} letters. Too wide for its place, it shows as a circle.`,
    ownSize: "Size",
    sizeValue: (pct: number) => `${pct}%`,
    sizeSceneTitle: "The size the other icons have",
    layer: "Layer",
    behind: "Behind the character",
    front: "In front of the character",
    frontHint: "Drawn over the speaker.",
    leave: "Leave",
    leaveMirror: "As it came in",
    position: "Position",
    positionAuto: "Automatic",
    positionYours: "Placed by you",
    positionReset: "Back to automatic",
    positionHint: "Drag it on the picture to move it; drag a corner to size it.",
    // By hand, on the picture.
    handlesLabel: (label: string) => `${label} on the picture`,
    handlesHint: "Drag to move it, a corner to size it. Arrows move it (Shift: more), + and − size it.",
    coveredSpeaker: (label: string) => `${label} is partly behind the speaker.`,
    coveredFace: (label: string) => `${label} is behind the speaker's head.`,
    bringFront: "Bring to front",
    bringFrontTitle: "Put it in front of the character — it never covers the face",
    moveClear: "Move to a clear spot",
    moveClearTitle: "The clear spot nearest to where you put it",
    noClear: "No spot is clear of the character while it shows.",
    // By hand, on the lane.
    snapWords: "Snap to words",
    snapWordsTitle: "Let go near a word it's about and it lands on it. Hold Alt to put it anywhere.",
    offWordNote: (label: string, at: string) => `${label} settles at ${at}, off its word — where you put it.`,
    resetIcon: "Reset this icon",
    resetIconTitle: "Its own look, place and timing go back to the scene's",
    youWhere: "You chose the side.",
    colourWhy: (why: string) => why,
    omniTold: "Omni is told to draw no icons.",
    provisional: "Place and colour settle once the pattern preview is made.",
    keywordsOnly: "AI unavailable — these come from keywords.",
    askAgain: "Ask the AI again",
    askingAgain: "Asking the AI…",
    timingEstimated: "Timing is estimated — drag an icon onto its word if it's off.",
    updating: "Updating icons…",
    iconsTooLong: "The icons took too long.",
    errorTitle: "Couldn't place the icons.",
    refusedTitle: "These icons can't be made:",
    // warming, the scene's first plan
    warmTitle: "Getting the icons ready",
    step: {
      words: "Listening to the words",
      spots: "Finding clear spots",
      director: "Choosing the icons",
    } as Record<string, string>,
    stepFirstTime: "The first time takes about 30 s.",
    // Omni's own icons on the version built on
    omniOwn: (n: number) => `v${n} already has Omni's own icons.`,
    omniOwnAdd: "Adding ours on top would show two sets.",
    omniOwnChange: "Redone, Omni is told to draw none — ours replace them.",
    omniOwnHouse: "Redone in house, Omni's icons are gone — ours replace them.",
    keepThem: "Keep them",
    keepThemTitle: "Keep Omni's icons and leave ours off",
    keeping: (n: number) => `Keeping v${n}'s own icons — ours are off.`,
    keepingOmni: "Keeping Omni's own icons — ours are off.",
    useOurs: "Use ours instead",
    onVersion: (n: number) => `These are v${n}'s icons — changing them re-stamps v${n}, no Omni.`,
    // the picker
    pickerAdd: "Add icons",
    pickerChange: (label: string) => `Change ${label}`,
    tabSuggested: "Suggested",
    tabLibrary: "Library",
    tabsLabel: "Where to pick from",
    suggestedHint: "From what they say. Tap to add.",
    suggestedNone: "The AI's ideas show here once the icons are ready.",
    search: "Search icons",
    searchPlaceholder: (n: number) => `Search ${n.toLocaleString()} icons — any word…`,
    picks: "42 picks",
    results: (n: number) => `${n} ${plural(n, "icon")}`,
    noResults: (q: string) => `No icon for “${q}”.`,
    weakResults: (q: string) => `Nothing fits “${q}” well.`,
    searchFailed: "Couldn't search right now —",
    onScreen: "On screen — tap to remove",
    tapToAdd: "Tap to add",
    tapToUse: "Tap to use it",
    // your team's icons
    team: "Your team's icons",
    teamTag: "Team",
    teamTitle: "Made by your team — on every machine",
    teamNone: "Icons your team draws with AI or uploads show up here, for everyone.",
    // the team tile's menu
    teamMenu: (name: string) => `Manage “${name}”`,
    teamMenuTitle: "Rename, tag or delete",
    teamEditLabel: (name: string) => `Change “${name}”`,
    teamName: "Name",
    teamTags: "Tags",
    teamSave: "Save",
    teamSaving: "Saving…",
    teamDelete: "Delete…",
    teamDeleteTitle: "Delete this icon?",
    teamDeleteMessage: (name: string) =>
      `“${name}” is hidden from the library for everyone. Versions that use it still show it.`,
    teamDeleteConfirm: "Delete",
    teamEditFailed: "Couldn't change it:",
    teamEditHint: "A new name changes how your team finds it — icons already added keep their words.",
    close: "Close",
    // upload your own (free: the file is cleaned into a one-colour icon)
    upload: "Upload",
    uploadTitle: "Upload your own icon — an SVG, PNG, WebP or JPEG",
    uploadYourOwn: "Upload your own",
    dropHere: "Drop an SVG or picture to make it an icon",
    uploadKinds: (maxMb: number, svgKb: number) => `SVG, PNG, WebP or JPEG · up to ${maxMb} MB (an SVG ${svgKb} KB)`,
    uploadWrongKind: "Upload an SVG, PNG, WebP or JPEG.",
    uploadTooBig: (limit: string) => `The file is larger than ${limit}.`,
    uploadReading: (name: string) => `Cleaning up “${name}”…`,
    uploadFailed: "Couldn't use this file:",
    uploadAnother: "Choose another file",
    uploadYours: "Your file",
    uploadVia: {
      shapes: "Read as shapes",
      transparency: "Its clear background kept clear",
      "light page": "Taken off its light background",
      "dark ground": "Taken off its dark background",
    } as Record<string, string>,
    uploadPlateKept: "on its plate",
    uploadPlateDropped: "just the marks on its plate",
    uploadPlate: "Its plate",
    uploadPlateKeep: "Keep the plate",
    uploadPlateMarks: "Just the marks",
    uploadPlateTitle: "It sits on a plate (a disc, a badge): keep the whole shape, or use only the marks on it",
    uploadName: "Name",
    uploadNamePlaceholder: "What it shows — e.g. Piscine",
    uploadNameHint: "The words in its chip, and how your team finds it.",
    uploadNameNeeded: "Give it a name.",
    uploadTags: "Tags",
    uploadTagsPlaceholder: "More words that find it, split by commas",
    uploadDuplicate: (name: string, hidden: boolean) =>
      hidden
        ? `Your team has this icon already, hidden, as “${name}” — saving brings it back.`
        : `Already in your team's icons as “${name}”.`,
    uploadSave: "Save to team icons",
    uploadSaveDuplicate: "Use your team's icon",
    uploadSaving: "Saving…",
    uploadSaved: (name: string) => `“${name}” is in your team's icons — on every machine.`,
    uploadSavedAdded: (name: string) => `“${name}” is in your team's icons, and added.`,
    uploadSavedFull: (name: string, max: number) =>
      `“${name}” is in your team's icons. You've added all a scene takes (${max} of ${max}), so it isn't added.`,
    uploadSaveFailed: "Couldn't save it:",
    uploadPrivacy: "Only this cleaned icon is kept for use — your file is never shown to anyone.",
    uploadUpdating: "Updating…",
    // Generate with AI (the one paid button here; typing is free)
    generate: (cost: number) => `Generate with AI · ~${usd(cost)}`,
    generateLink: (q: string, cost: number) => `Not it? Generate “${q}” with AI · ~${usd(cost)}`,
    generateTitle: (left: number | null, perDay: number) =>
      left == null
        ? "The AI draws one bold icon for this name. It costs a few cents; typing never does."
        : `The AI draws one bold icon for this name. ${left} of ${perDay} left today for your team.`,
    drawing: (q: string) => `Drawing “${q}”… about 10 s`,
    drawnNew: (q: string) => `Drawn for “${q}”.`,
    drawnBefore: (q: string) => `Drawn before for “${q}” — no charge.`,
    drawnBack: (q: string) => `You discarded this one for “${q}” — here it is again, no charge.`,
    drawReport: (r: { shapes?: number; dropped?: number; weight?: string } | null) =>
      r
        ? [
            r.shapes ? `${r.shapes} ${plural(r.shapes, "shape")}` : null,
            r.dropped ? `${r.dropped} ${plural(r.dropped, "speck")} cleaned off` : null,
            r.weight === "bolder"
              ? "strokes made bolder"
              : r.weight === "thinner"
                ? "strokes made thinner"
                : r.weight === "kept"
                  ? "strokes already bold"
                  : null,
          ]
            .filter(Boolean)
            .join(" · ")
        : "",
    /** An upload's clean-up, in a line: how its shape was found, its plate,
     *  then the drawing's own numbers. */
    uploadReport: (
      r: { via?: string; plate?: string | null; shapes?: number; dropped?: number; weight?: string } | null,
    ): string =>
      r
        ? [
            r.via ? COPY.icons.uploadVia[r.via] ?? null : null,
            r.plate === "dropped" ? COPY.icons.uploadPlateDropped : r.plate === "kept" ? COPY.icons.uploadPlateKept : null,
            COPY.icons.drawReport(r) || null,
          ]
            .filter(Boolean)
            .join(" · ")
        : "",
    previewCircle: "Circle",
    previewChip: "Circle + words",
    previewTitle: "Exactly as the video will show it",
    useIt: "Use it",
    tryAgain: (cost: number) => `Try again · ~${usd(cost)}`,
    tryAgainTitle: "Draws a new one; this one is hidden",
    discard: "Discard",
    discardTitle: "Hidden from your team's icons (versions that use it keep it)",
    discarded: "Discarded — hidden from your team's icons.",
    usedIt: (q: string) => `“${q}” added.`,
    drawFailed: "Couldn't draw it:",
    // the stage
    showSpots: "Show spots",
    showSpotsTitle: "Where icons can go: clear, the AI's no, or blocked by the speaker",
    spot: { good: "Clear", avoid: "AI: avoid", blocked: "Blocked by the speaker", home: "Home" } as Record<string, string>,
    playing: "Pause",
    paused: "Play",
    mute: "Mute",
    unmute: "Sound on",
    exact: "Exact frame",
    exactLoading: "Exact frame…",
    exactTitle: "This is the frame the run will make",
    captionScene: "Icons over the original · Omni paints the pattern in motion",
    captionSceneHouse: "Icons over the original · the pattern shown roughly — the video draws it exactly",
    captionVersion: (n: number | null) => (n ? `Icons over v${n}` : "Icons over the version"),
    patternApprox: "≈ pattern",
    patternApproxTitle: "The pattern is drawn roughly here — the exact frame shows it as the run will",
    moments: "Moments",
    momentAria: (i: number, label: string, at: string) => `Exact frame ${i}: ${label} at ${at}`,
    laneLabel: "Icons on the timeline",
    // the lane with no blocks to show
    laneOff: "Icons are off",
    laneTurnOn: "Turn on",
    laneEmpty: "No icons yet",
    laneNone: "No icons",
    blockAria: (label: string, at: string, word: string | null) =>
      `${label}, ${at}${word ? `, on ${word}` : ""}. Left and Right: another mention (0.1 s with Snap to words off); Shift: shorter or longer; Alt: when it starts; Alt+Up and Alt+Down: another track; Enter: exact frame`,
    zoneTitle: (why: string) => `No icon here — ${why}`,
    lengthHandle: (label: string) => `${label}: how long it shows`,
    startHandle: (label: string) => `${label}: when it starts (where it ends stays)`,
    placing: "Placing icons…",
    placingFailed: "Couldn't place the icons —",
    placingRefused: "The icons can't be made as they are.",
    openIcons: "Open Icons",
    restampCost: "$0 · about 1 min",
    restamp: "Place icons again",
    restampTitle: "Lays the icons on this version again — no Omni, $0",
    conflict: "The icons changed since you looked — check them.",
    // one of yours moved off its word, out of a zone (the plan item's `shifted`)
    shifted: (at: string, why: string) => `Moved to ${at}, ${COPY_SHIFT_WHY[why] ?? COPY_SHIFT_WHY.entrance}`,
    showAnyway: (at: string) => `Show at ${at} anyway`,
    showAnywayTitle: "On its word, as if you timed it by hand: the pattern may still be moving there",
    // added at a moment (the playhead): "anyway" is that moment, not the word
    showAnywayTitleOf: (shift: { asked?: number | null; why: string }) =>
      typeof shift.asked === "number"
        ? `Where you added it, as if you timed it by hand${
            shift.why === "start" || shift.why === "end" ? "" : ": the pattern may still be moving there"
          }`
        : "On its word, as if you timed it by hand: the pattern may still be moving there",
    // the tracks (a block dragged up or down)
    track: (n: number) => `Track ${n + 1}`,
    ownTrack: "On a track of its own, so it shows with the others",
    newTrack: "New track",
    trackTitle: "Drag it up or down to another track (Alt+Up / Alt+Down)",
    trackOrderNote: "That one is on a track of its own: move it on the timeline",
    // the picker: added, and not in the plan yet; or left out by it (why)
    addedNotShown: "Added, being placed: tap to remove",
    addedLeftOut: (why: string) => `Added, not shown (${why}): tap to remove`,
  },
};

/** Why one of yours moved off its word (HarnessIconShift.why). */
const COPY_SHIFT_WHY: Record<string, string> = {
  entrance: "after the pattern comes in",
  exit_after: "after the pattern goes out",
  exit_before: "before the pattern goes out",
  start: "after the opening",
  end: "so it's gone before the end",
};

/** What the stage says while an edit is being drawn. */
export function changeLabel(kind: ChangeKind, names: string[]): string {
  const name = names[0] ?? "the pattern";
  switch (kind) {
    case "colour":
      return "Painting the new colour…";
    case "pattern":
      return `Laying in ${name}…`;
    case "add":
      return names.length > 1 ? `Laying in ${names.length} patterns…` : `Laying in ${name}…`;
    case "remove":
    case "hide":
      return `Taking ${name} out…`;
    case "show":
      return `Putting ${name} back…`;
    case "place":
      return `Moving ${name}…`;
    case "hand":
      return COPY.hand.placing(name);
    case "time":
      return `Retiming ${name}…`;
    case "shuffle":
      return "Finding another spot…";
    case "words":
      return COPY.wordsChip;
    default:
      return "Updating the preview…";
  }
}

/** "Left side", "Top right", "Full screen" — where a pattern was drawn. */
export function layoutLabel(l: HarnessPatternLayout | null | undefined): string | null {
  if (!l) return null;
  const corner = l.anchor === "right" ? "right" : "left";
  switch (l.placement) {
    case "left":
      return "Left side";
    case "right":
      return "Right side";
    case "top":
      return `Top ${corner}`;
    case "bottom":
      return `Bottom ${corner}`;
    case "full":
      return "Full screen";
    default:
      return null;
  }
}

/** Where a pattern goes before any preview has placed it. */
export function anchorLabel(anchor: string): string {
  switch (anchor) {
    case "edge-left":
      return "Left side";
    case "edge-right":
      return "Right side";
    case "edge-top":
      return "Top";
    case "edge-bottom":
      return "Bottom";
    case "full":
      return "Full screen";
    default:
      return "Its own place";
  }
}

/** m:ss for elapsed counters. */
/** 0:07.8 — a moment in a scene, to a tenth of a second. */
export function clock(totalSeconds: number): string {
  const t = Math.max(0, Math.round(totalSeconds * 10) / 10);
  const m = Math.floor(t / 60);
  return `${m}:${(t - m * 60).toFixed(1).padStart(4, "0")}`;
}

export function mmss(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** A reason as a line of copy: the icon AI's own checks write theirs in lower
 *  case ("said as a negation"). */
export function sentence(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}
