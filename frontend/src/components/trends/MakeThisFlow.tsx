/**
 * "Make this" — the whole two-step flow, in ONE place.
 *
 * Step 1 proposes three concrete ideas and lets the owner reject all three by
 * typing what they actually want; step 2 picks the Producer tool that builds
 * the chosen idea and opens it pre-filled.
 *
 * WHY two steps. The first version jumped straight from a tool card to a
 * finished brief, so the only way to reject the model's take was to bin the
 * whole brief and start over. The owner asked for the choice to happen BEFORE
 * the writing: three concrete ideas, pick one — and if none of them land, type
 * what you actually want and get three more (the titles already shown are sent
 * as `exclude`, so a re-roll comes back different instead of rewording itself).
 * The chosen idea then rides along to the prefill call, so the brief executes
 * THAT idea rather than inventing a fourth one.
 *
 * WHY it lives here rather than inside a dialog. The flow was written for the
 * general-trends feed, and the owner then asked for the same thing on
 * competitor posts and Industry Signal stories — explicitly the SAME
 * components, not a second copy. There was already one copy too many:
 * `GeneralMakeThisDialog` re-declared the tool order and the tool blurbs
 * because the originals in `MakeThisDialog` were module-private, so the picker
 * on two screens was two literals that nothing forced to agree. A third feed
 * would have made it three. Both dialogs are now thin adapters over this
 * component, which is why `TOOL_ORDER` and `TOOL_BLURB` are exported: there is
 * one of each, and a change to a blurb reaches every feed.
 *
 * What varies between feeds is described by `MakeThisSource` — the wording, the
 * tools on offer, and the two calls (brainstorm, prefill). Everything else —
 * the radiogroup semantics, the in-flight dimming, the research card, the
 * time-boxed clipboard hand-off — is identical by construction, because it is
 * the same code.
 *
 * WHY THE IDEAS ARE ROWS, AND WHY NOTHING ON THEM IS CUT SHORT. This has been
 * three ways. First three full-width blocks, four paragraphs each, which read
 * as a document to work through. Then three cards in a row, each led by a
 * picture tile with the words clamped to two lines, which was the owner's
 * turn to object: "I don't see the ideas anywhere, I can't even fully read
 * it." Both were right about the other. Three columns in a 42rem panel leave
 * about 11rem for the words, and two clamped lines is enough to tell ideas
 * apart and not enough to read one. So each idea is one full-width row: a
 * small format badge (`FORMAT_TILE`, the plain word "Video" or "Poster" next
 * to its icon) beside the title, ALL of what you would see, and why it works.
 * Three short paragraphs to read and one to click, which is what choosing is.
 * The chosen idea then rides into step 2 in full.
 */
import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { createPortal } from "react-dom";
import {
  ArrowLeft,
  Check,
  Film,
  Globe,
  Image as ImageIcon,
  Layers,
  RefreshCw,
  Sparkles,
  X,
} from "lucide-react";

import { Skeleton } from "../Skeleton";
import { useToast } from "../ToastProvider";
import { sendProducerBrief } from "../../lib/producerBrief";
import type {
  GeneralTrendResearch,
  TrendIdea,
  TrendPrefill,
} from "../../lib/types";
import { ROADMAP_TOOL_LABELS } from "../yas/roadmap/types";
import type { RoadmapTool } from "../yas/roadmap/types";
import { ErrorCard } from "./shared";
import { humanError } from "./trendsApi";

/**
 * Everything one feed has to say about itself. An adapter builds this from a
 * trend, a competitor post or a news story; the flow reads nothing else about
 * the subject, which is what keeps the three screens honestly identical.
 */
export interface MakeThisSource {
  /** Lead of the header line — the step ("· Step 1 of 2 · …") is appended. */
  eyebrow: string;
  /** The subject, already trimmed to something a heading can hold. */
  headline: string;
  /** The mono line under it. "" renders nothing rather than an empty line. */
  subline: string;
  /** The tools this subject may be built with, in display order. */
  tools: RoadmapTool[];
  /** Which card starts selected. MUST be a member of `tools` — see `ideaTool`. */
  defaultTool: RoadmapTool;
  /** Step 1's opening sentence. */
  intro: string;
  /** Step 2's opening sentence. */
  toolIntro: string;
  /** "trend" / "post" / "story" — the noun the flow's own copy needs. */
  ideaNoun: string;
  /**
   * True when this feed looks its subject up before writing the ideas, so the
   * flow may promise "Looking up what this actually is…" while the first round
   * is in flight. A competitor post has nothing to look up — its caption and
   * the scorer's read of it are already on the card — and promising a lookup
   * that never arrives is worse than saying nothing. Optional, default false:
   * a feed opts IN to the claim, so a new adapter cannot make it by accident.
   */
  researches?: boolean;
  brainstorm(opts: { feedback: string; exclude: string[] }): Promise<{
    ideas: TrendIdea[];
    research?: GeneralTrendResearch | null;
  }>;
  prefill(tool: RoadmapTool, idea?: TrendIdea): Promise<TrendPrefill>;
}

/** Display order of the picker. Every RoadmapTool appears exactly once. */
export const TOOL_ORDER: RoadmapTool[] = [
  "poster",
  "poster_human",
  "animation",
  "talking_head_v2",
  "general_video",
  "broll",
  "human_character",
  "video_editor",
];

/**
 * One line per tool: what it makes, then what arrives already written.
 *
 * Two of the eight cannot be fully seeded and the card is where we say so,
 * rather than letting somebody find out after the tool has opened half empty.
 *
 * Written for someone who has not used the Producer before, so the model names
 * (Veo) and our own field names ("the brief box", "the motion prompt") are out:
 * they describe the plumbing, and the reader is choosing between things to
 * make, not between prompts to fill.
 */
export const TOOL_BLURB: Record<RoadmapTool, string> = {
  poster: "A still poster with no people in it. Opens with the description written.",
  poster_human: "A still poster with students in it. Opens with the description written.",
  animation: "A 4 to 8 second animated card. Opens with the movement described.",
  talking_head_v2:
    "Someone talking to camera. Opens with the topic and the words to say.",
  general_video:
    "A short video the AI makes for you, with no filming. Opens with the shot described.",
  broll:
    "Footage you film yourself. Opens with the opening and closing lines, and copies the shot list for you to paste.",
  human_character:
    "Makes a reusable AI person. It asks who they are rather than what to post, so that is what gets written.",
  video_editor:
    "Cuts clips you already have into one video. It has no description box, so only the project name is filled in and the plan is copied for you to paste.",
};

/** Mirrors `trend_ideation.IDEAS_PER_ROUND`. Only drives how many skeleton
 *  cards are drawn — the real count comes from the response. */
export const IDEAS_PER_ROUND = 3;

/**
 * The tool an idea asks for, validated against THIS source's own list.
 *
 * Two failures to stop, not one. A model writes this field, so an unrecognised
 * string must not index `ROADMAP_TOOL_LABELS` into `undefined` and render a
 * blank chip — but a perfectly valid tool can still be one the subject refuses
 * to offer: nothing stops the model proposing a Veo clip for a news story, and
 * picking that idea would show a chip for a tool the picker does not list AND
 * leave step 2 with a selection no card can display. Clamping to `tools` rather
 * than to the global eight is what keeps the two in step.
 */
function ideaTool(idea: TrendIdea, source: MakeThisSource): RoadmapTool {
  return source.tools.includes(idea.tool) ? idea.tool : source.defaultTool;
}

/** True when the backend actually found something. `what_it_is` being empty is
 *  the one failure signal — a failed lookup still returns the whole shape. */
function found(r: GeneralTrendResearch | null | undefined): r is GeneralTrendResearch {
  return Boolean(r && r.what_it_is);
}

/**
 * What the lookup found, above the ideas.
 *
 * This is the owner's proof that the model LOOKED instead of guessing. They
 * clicked "Make this" on `#spidermanbrandnewday` and got three ideas about "a
 * fresh start" — the hashtag names a Spider-Man film, and nothing on screen
 * revealed that the model had misread it. Now the subject is stated, with the
 * pages it was read off, before a single idea.
 */
function WhatThisIs({ research }: { research: GeneralTrendResearch }) {
  const sources = (research.sources ?? []).filter((s) => s.uri || s.title);
  return (
    <div className="card bg-bg-muted p-3">
      <p className="eyebrow mb-1 flex items-center gap-1.5">
        <Globe className="h-3 w-3" aria-hidden />
        What this actually is
      </p>
      <p className="text-xs leading-relaxed text-fg break-words">
        {research.what_it_is}
      </p>
      {research.why_people_post && (
        <p className="mt-1.5 text-2xs leading-relaxed text-muted break-words">
          <span className="font-medium text-fg">Why people post it:</span>{" "}
          {research.why_people_post}
        </p>
      )}
      {sources.length > 0 && (
        <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs text-muted">
          <span>Looked it up on</span>
          {sources.slice(0, 4).map((s, i) => (
            <a
              key={`${s.uri || s.title}-${i}`}
              href={s.uri || undefined}
              target="_blank"
              rel="noreferrer"
              className="underline underline-offset-2 hover:text-fg break-all"
            >
              {s.title || s.uri}
            </a>
          ))}
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Failures
// ---------------------------------------------------------------------------

/** What goes on screen, and the server's own words kept behind it. */
type Problem = { message: string; detail: string | null };

/** `http()` throws `Error("<status> <detail>")`. The number is the only
 *  reliable part of it: the detail is prose, and prose gets rewritten. */
function statusOf(e: unknown): number | null {
  const m = e instanceof Error ? /^(\d{3})\s/.exec(e.message) : null;
  return m ? Number(m[1]) : null;
}

/**
 * A failure, in words the person looking at it can act on.
 *
 * WHY THIS IS NOT JUST `humanError`. That helper strips the status code off
 * what `http()` threw and hands the backend's sentence straight to the screen.
 * Most of the time that is right, and it is why the backend bothers to write
 * plain English at all. It is wrong here for one specific reason: an ideation
 * call that fails because the Google Cloud project has billing off is
 * classified by the same helper the SCORER uses, so the string that arrives is
 *
 *   "AI scoring is unavailable: the Google Cloud project has billing disabled.
 *    Posts are ranked by engagement until it's enabled, then re-scan or hit
 *    Re-score to fill in the fit scores."
 *
 * Not one clause of that is about this dialog. A reader who follows it goes and
 * presses a button on another tab, comes back, and hits the same wall. Its
 * sibling is worse: it appends 160 characters of the raw exception.
 *
 * So the status code decides the shape of the answer and the words decide the
 * detail. 422 is a configuration problem somebody with access to the AI account
 * has to fix, 5xx is the model not answering (worth another go), 404 means the
 * row went away underneath us, and no status at all means the request never
 * left the browser.
 *
 * The server's own text is NOT thrown away. It goes in `detail`, one click
 * away, because it is what whoever fixes this will ask to have pasted to them.
 * The same bargain `services/gen_errors.py` strikes on the backend: a readable
 * sentence for the reader, the verbatim failure for the person debugging.
 */
function problemFor(
  e: unknown,
  /** Which of the two calls failed, so the sentence can name what did not happen. */
  doing: "ideas" | "brief",
  /** "trend" / "post" / "story", for the one message that has to name the row. */
  noun: string,
): Problem {
  const raw = humanError(e, "");
  const words = raw.toLowerCase();
  const status = statusOf(e);
  const cant = doing === "ideas" ? "write ideas" : "write the brief";
  // No status means fetch itself threw, so there is no server message to keep.
  const detail = status === null ? null : raw || null;

  // Word matches first: these three are precise enough to beat the status code,
  // and each has a different answer. Spend cap before billing, because a cap
  // that has been hit is not a switch anyone has turned off.
  if (words.includes("spend cap") || words.includes("budget")) {
    return {
      message: `The AI account has hit its spending limit, so it will not ${cant} until the limit is raised or the meter resets. Whoever manages the Google Cloud project can do that.`,
      detail,
    };
  }
  if (words.includes("billing")) {
    return {
      message: `The AI account this runs on has billing switched off, so it cannot ${cant}. Ask whoever manages the Google Cloud project to turn billing back on, then try again.`,
      detail,
    };
  }
  if (words.includes("unreadable") || words.includes("unparseable")) {
    return {
      message:
        "The AI sent back an answer we could not read. Try again, it usually works the second time.",
      detail,
    };
  }
  if (
    status === 429 ||
    words.includes("rate limit") ||
    words.includes("resource_exhausted")
  ) {
    return {
      message: "The AI service is busy right now. Wait a minute, then try again.",
      detail,
    };
  }
  if (
    words.includes("api key") ||
    words.includes("credential") ||
    words.includes("unauthenticated") ||
    words.includes("permission") ||
    words.includes("not enabled")
  ) {
    return {
      message: `This app is not signed in to the AI account, so it cannot ${cant}. Ask whoever set up the Google Cloud project to check the key, then try again.`,
      detail,
    };
  }

  if (status === null) {
    return {
      message: `We could not reach the server, so it could not ${cant}. Check you are online, then try again.`,
      detail,
    };
  }
  if (status === 404) {
    return {
      message: `This ${noun} is not in the database any more. Close this and refresh the page.`,
      detail,
    };
  }
  if (status >= 500) {
    return {
      message: `The AI did not answer, so it could not ${cant}. That is usually temporary, so try again.`,
      detail,
    };
  }
  // A 422 we have no specific words for. Say only what is certain: it is a
  // setup problem, not something the reader mistyped, and it needs whoever runs
  // the server. The exact reason is under "What the server said".
  return {
    message: `The AI is not set up to ${cant} at the moment. This is not something you can fix from here, so pass it to whoever runs the server.`,
    detail,
  };
}

/**
 * A failure the reader has to see, with the server's own words one click away.
 *
 * `ErrorCard` carries the sentence, so a failure here looks like a failure
 * everywhere else on this page and keeps its `role="alert"`. The disclosure
 * under it is the `truncation-strategy` half of the bargain: the sentence is
 * what to READ, the raw text is what gets PASTED to whoever fixes it, and
 * dropping the second one to look tidy just moves the work to a log nobody has
 * open. Shut by default, because most of the time nobody needs it.
 */
function FlowError({ problem }: { problem: Problem }) {
  return (
    <div className="space-y-1.5">
      <ErrorCard message={problem.message} />
      {problem.detail && (
        <details className="text-2xs leading-relaxed text-muted">
          {/* `py-1` rather than the text's own height: this is a pointer target
              and 24x24 CSS px is the floor (`web-target-size`). */}
          <summary className="cursor-pointer rounded px-1 py-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40">
            What the server said
          </summary>
          <p className="mt-1 break-words px-1 font-mono">{problem.detail}</p>
        </details>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The flow
// ---------------------------------------------------------------------------

/**
 * Everything Tab can land on inside the panel. The focus trap has to know where
 * the ring is allowed to go before it can stop it leaving. Same literal as
 * `TrendDetailDialog`'s, which is the other portalled dialog on this page.
 */
const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),' +
  'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

type Step = "idea" | "tool";

export function MakeThisFlow({
  source,
  onClose,
}: {
  source: MakeThisSource;
  onClose: () => void;
}) {
  const toast = useToast();
  const [step, setStep] = useState<Step>("idea");

  // --- step 1: the three ideas ---
  const [ideas, setIdeas] = useState<TrendIdea[]>([]);
  const [chosen, setChosen] = useState<TrendIdea | null>(null);
  const [thinking, setThinking] = useState(true); // the first round starts on open
  const [ideasError, setIdeasError] = useState<Problem | null>(null);
  const [feedback, setFeedback] = useState("");
  // What the backend looked up before writing the ideas. Kept across rounds:
  // a re-roll researches the same subject, so a round that comes back without
  // it (an older backend, a failed search) must not erase what we already
  // showed. A feed that never researches leaves this null forever.
  const [research, setResearch] = useState<GeneralTrendResearch | null>(null);
  // Every title shown so far, across rounds. Sent as `exclude` so round two is
  // actually new — the model otherwise re-serves its favourite angle reworded.
  const [seen, setSeen] = useState<string[]>([]);
  const brainFlight = useRef(false); // StrictMode / double-click guard
  const cardRefs = useRef<Array<HTMLButtonElement | null>>([]);

  // --- step 2: the tool + hand-off ---
  const [tool, setTool] = useState<RoadmapTool>(() => source.defaultTool);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Problem | null>(null);
  const inFlight = useRef(false); // StrictMode / double-click guard

  // --- the dialog itself ---
  const panelRef = useRef<HTMLDivElement | null>(null);
  /** Whatever had focus when this opened, so closing hands it straight back
   *  instead of dropping the keyboard at the top of the document. */
  const openerRef = useRef<Element | null>(null);
  /** True only when the press that started a click landed on the backdrop.
   *  Selecting the text of an idea and releasing the mouse outside the panel
   *  used to close the dialog and throw the whole round away. */
  const pressedBackdrop = useRef(false);
  // `useId` rather than literal ids: this is portalled to `document.body`, and
  // a hard-coded "make-this-title" would collide with any other copy of itself.
  const titleId = useId();
  const stepId = useId();
  const feedbackId = useId();

  // `onClose` without the mount effect depending on its identity. Both callers
  // pass an inline arrow, so it is a new function on every render of the card
  // that owns it, and `TrendsFeed` re-renders the whole grid every few seconds
  // while a scan is polling. See the mount effect for why that matters.
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  /**
   * Mount-only, and the empty dependency list is load-bearing.
   *
   * Everything here is about the dialog EXISTING: the page behind it must not
   * scroll, Escape must close it from either step, and whatever opened it must
   * get focus back. It used to depend on `[onClose]`, which was harmless while
   * all it did was lock the scroll, and is not harmless now: the callers pass
   * an inline arrow and `TrendsFeed` re-renders its grid every few seconds
   * while a scan polls, so the effect would tear down and set up repeatedly and
   * re-record the opener as whatever is focused at that moment, which is
   * something INSIDE the dialog. Closing would then hand focus back to a node
   * that no longer exists.
   */
  useEffect(() => {
    openerRef.current = document.activeElement;

    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onCloseRef.current();
    }
    window.addEventListener("keydown", onKey);

    return () => {
      document.body.style.overflow = prevOverflow;
      window.removeEventListener("keydown", onKey);
      const opener = openerRef.current;
      // Only if it is still in the document: the card behind can be gone by now
      // (a filter changed, a refresh landed), and focusing a detached node does
      // nothing at all while looking like it worked.
      if (opener instanceof HTMLElement && document.contains(opener)) {
        opener.focus();
      }
    };
  }, []);

  /**
   * Focus lands on the panel itself, on open and again on every step change.
   *
   * The panel, not the first control. The first control is Close, and a dialog
   * that opens by announcing its own dismiss button has told the reader
   * nothing; landing on the panel announces the dialog role and its label,
   * which is the step number and the subject.
   *
   * Re-running on `step` is not a nicety. Each step is a separate subtree, so
   * "Change idea" and "Back" DELETE the button that was just pressed, and the
   * browser hands focus to `document.body`, outside the dialog entirely.
   */
  useEffect(() => {
    panelRef.current?.focus();
  }, [step]);

  /**
   * The focus trap.
   *
   * A modal that Tab can walk out of is modal only to the mouse: the reader
   * ends up typing into the filter bar they cannot see behind the overlay. This
   * wraps the ring from the last control back to the first and back again.
   * Elements that are not laid out (`offsetParent === null`, which covers
   * `display:none`) are skipped so nothing becomes an invisible stop.
   *
   * Escape still leaves, so this is a modal trap and not a keyboard trap.
   */
  function trapTab(e: ReactKeyboardEvent<HTMLDivElement>) {
    if (e.key !== "Tab") return;
    const root = panelRef.current;
    if (!root) return;
    const items = Array.from(
      root.querySelectorAll<HTMLElement>(FOCUSABLE),
    ).filter((el) => el.offsetParent !== null || el === document.activeElement);
    if (items.length === 0) return;

    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;

    if (e.shiftKey && (active === first || active === root)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  }

  async function runBrainstorm(note: string) {
    if (brainFlight.current) return;
    brainFlight.current = true;
    setThinking(true);
    setIdeasError(null);
    try {
      const res = await source.brainstorm({
        feedback: note.trim(),
        exclude: seen,
      });
      const fresh = res.ideas ?? [];
      if (found(res.research)) setResearch(res.research);
      setIdeas(fresh);
      setSeen((prevSeen) => [...prevSeen, ...fresh.map((i) => i.title)]);
      setChosen(null); // the round they were choosing from is gone
      setFeedback(""); // the request consumed it
      cardRefs.current = [];
    } catch (e) {
      // A failed re-roll must NOT blank the three already on screen: they are
      // still perfectly pickable, and losing them to a 502 is the worst
      // possible outcome of pressing "try different ideas".
      setIdeasError(problemFor(e, "ideas", source.ideaNoun));
    } finally {
      brainFlight.current = false;
      setThinking(false);
    }
  }

  // First round fires on open — the owner should never have to ask for it.
  useEffect(() => {
    void runBrainstorm("");
    // Mount only: the subject cannot change without the dialog remounting.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function pick(idea: TrendIdea) {
    setChosen(idea);
    setTool(ideaTool(idea, source)); // pre-selects step 2's picker
  }

  /** Radiogroup semantics: an arrow key both moves focus and selects. */
  function moveTo(index: number) {
    if (!ideas.length) return;
    const i = ((index % ideas.length) + ideas.length) % ideas.length;
    pick(ideas[i]);
    cardRefs.current[i]?.focus();
  }

  function onIdeaKey(e: ReactKeyboardEvent<HTMLButtonElement>, i: number) {
    if (e.key === "ArrowDown" || e.key === "ArrowRight") {
      e.preventDefault();
      moveTo(i + 1);
    } else if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
      e.preventDefault();
      moveTo(i - 1);
    } else if (e.key === "Home") {
      e.preventDefault();
      moveTo(0);
    } else if (e.key === "End") {
      e.preventDefault();
      moveTo(ideas.length - 1);
    }
  }

  async function go() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const p = await source.prefill(tool, chosen ?? undefined);
      // Clipboard is a genuine fallback, not decoration: video_editor has no
      // brief box, so the plan has to be pasteable into its AI chat by hand.
      // Time-boxed: a clipboard permission prompt that never resolves (headless
      // / embedded browsers) must not hold the hand-off hostage.
      try {
        await Promise.race([
          navigator.clipboard.writeText(p.topic),
          new Promise<void>((resolve) => setTimeout(resolve, 1500)),
        ]);
      } catch {
        /* blocked outside a secure context */
      }
      sendProducerBrief({
        tool,
        title: p.title,
        topic: p.topic,
        dialogue: p.dialogue,
        introTopic: p.intro_topic,
        outroTopic: p.outro_topic,
        character: p.character,
        notes: p.notes,
        postId: p.post_id,
        permalink: p.permalink,
      });
      toast.success(`Opening ${ROADMAP_TOOL_LABELS[tool]}`, p.notes || p.title);
      onClose();
    } catch (e) {
      setError(problemFor(e, "brief", source.ideaNoun));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  const showSkeletons = thinking && ideas.length === 0;
  // Only a feed that really does look things up may say that it is looking.
  const showLookup = Boolean(source.researches) && showSkeletons;
  const showEmpty = !thinking && !ideasError && ideas.length === 0;
  const regenLabel = thinking
    ? "Thinking…"
    : feedback.trim()
      ? "Get 3 more"
      : "Try 3 different ideas";

  return createPortal(
    <div
      style={{
        position: "fixed", inset: 0, zIndex: 100, background: "rgba(0,0,0,0.7)",
        backdropFilter: "blur(2px)", display: "flex", alignItems: "center",
        justifyContent: "center", padding: 16,
      }}
      // A click only dismisses when the press that STARTED it also landed on
      // the backdrop. Dragging a selection across an idea and letting go over
      // the dark area used to bin the whole round.
      onMouseDown={(e) => {
        pressedBackdrop.current = e.target === e.currentTarget;
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget && pressedBackdrop.current) onClose();
      }}
    >
      {/* The dialog is the PANEL, not the backdrop. `tabIndex={-1}` makes it a
          landing target for focus without putting it in the tab ring, so it can
          be focused on open and on each step change; `focus:outline-none` is
          right for exactly that reason, since nothing keyboard-reachable is
          losing its ring. The label is the step line plus the subject, so the
          reader hears where they are as well as what this is about. */}
      <div
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${stepId} ${titleId}`}
        onKeyDown={trapTab}
        className="card w-full max-w-2xl max-h-[85vh] flex flex-col overflow-hidden focus:outline-none"
      >
        <div className="flex shrink-0 items-start justify-between gap-3 border-b border-border p-5">
          <div className="min-w-0">
            <p id={stepId} className="eyebrow mb-1">
              {step === "idea"
                ? `${source.eyebrow} · Step 1 of 2 · Pick an idea`
                : `${source.eyebrow} · Step 2 of 2 · Choose the tool`}
            </p>
            <h2
              id={titleId}
              className="text-lg font-semibold text-fg break-words"
            >
              {source.headline}
            </h2>
            {source.subline && (
              <p className="font-mono text-2xs text-muted mt-1">
                {source.subline}
              </p>
            )}
          </div>
          <button onClick={onClose} className="btn btn-ghost px-2" aria-label="Close">
            <X className="w-4 h-4" strokeWidth={1.8} />
          </button>
        </div>

        {/* ---------------------------- STEP 1 ---------------------------- */}
        {step === "idea" && (
          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
            <p className="text-xs text-muted leading-relaxed">{source.intro}</p>

            {research ? (
              <WhatThisIs research={research} />
            ) : (
              showLookup && (
                <div className="card bg-bg-muted space-y-2 p-3">
                  <p className="eyebrow flex items-center gap-1.5">
                    <Globe className="h-3 w-3" aria-hidden />
                    Looking up what this actually is…
                  </p>
                  <Skeleton h="h-3" className="w-full" />
                  <Skeleton h="h-3" className="w-4/5" />
                </div>
              )
            )}

            {ideasError && <FlowError problem={ideasError} />}

            {/* One line that is always mounted, so it is a live region the
                whole time rather than one that appears mid-announcement, and so
                the grid under it never steps up or down. It says the wait out
                loud because the wait is long: `Loading Indicators` in the UX
                rules is explicit that a long one must not be left unexplained,
                and the shimmer bars below say "busy" without saying "how long".
                When the round lands it names the next action instead. */}
            <p role="status" className="min-h-[1rem] text-2xs text-muted">
              {thinking
                ? "Writing three ideas. This usually takes 10 to 30 seconds."
                : ideas.length > 0
                  ? `${ideas.length} idea${ideas.length === 1 ? "" : "s"} ready. Click one to pick it, then press Use this idea.`
                  : ""}
            </p>

            {showSkeletons && (
              // `aria-hidden`: the line above already says what is happening in
              // words, and three empty cards announced as a list of nothing is
              // noise. Same shape as the real cards, tile included, so the
              // layout does not jump when they are swapped in.
              <div className="space-y-2" aria-hidden>
                {Array.from({ length: IDEAS_PER_ROUND }).map((_, i) => (
                  <div key={i} className="card flex gap-3 p-4">
                    <Skeleton className="h-14 w-14 shrink-0" rounded="rounded-lg" />
                    <div className="flex-1 space-y-2">
                      <Skeleton h="h-4" className="w-3/5" />
                      <Skeleton h="h-3" className="w-full" />
                      <Skeleton h="h-3" className="w-4/5" />
                    </div>
                  </div>
                ))}
              </div>
            )}

            {showEmpty && (
              <div className="card p-4 text-sm text-muted">
                No ideas came back for this {source.ideaNoun}. Say what you want
                below and try again.
              </div>
            )}

            {ideas.length > 0 && (
              <div
                role="radiogroup"
                aria-label={`Ideas for this ${source.ideaNoun}`}
                aria-busy={thinking}
                // Dimmed while a new round is in flight: these three are about
                // to be replaced, so they should not read as live choices.
                //
                // A STACK, NOT A ROW. The first version put three cards side
                // by side, each with a big picture tile and the idea clamped
                // to two lines underneath. The owner's report was that they
                // could not find the ideas and could not read them, which is
                // the whole verdict on that layout: three columns in a 42rem
                // panel leaves about 11rem for the words. One idea per row
                // gives every idea the full width and shows all of it. The
                // reader is reading three short paragraphs, which is what
                // choosing between them is.
                className={thinking ? "space-y-2 opacity-50" : "space-y-2"}
              >
                {ideas.map((idea, i) => {
                  const active = chosen?.id === idea.id;
                  const t = ideaTool(idea, source);
                  const f = FORMAT_TILE[idea.format] ?? FORMAT_TILE.static;
                  return (
                    <button
                      key={idea.id}
                      ref={(el) => {
                        cardRefs.current[i] = el;
                      }}
                      type="button"
                      role="radio"
                      aria-checked={active}
                      // Roving tabindex: one stop for the whole group, as a
                      // radiogroup should be. Arrows move within it.
                      tabIndex={chosen ? (active ? 0 : -1) : i === 0 ? 0 : -1}
                      disabled={thinking}
                      onClick={() => pick(idea)}
                      onKeyDown={(e) => onIdeaKey(e, i)}
                      // Complete literal class strings: Tailwind's scanner
                      // never sees a concatenated `"border-" + x`. Everything
                      // inside is a <span>, because a button may only hold
                      // phrasing content and a <p> in it is invalid HTML.
                      className={
                        active
                          ? "card card-hover flex w-full gap-3 p-4 text-left border-accent shadow-lift focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
                          : "card card-hover flex w-full gap-3 p-4 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
                      }
                    >
                      {/* What you would get, as an icon and a plain word:
                          "Video", "Poster", "Carousel". Small and beside the
                          idea rather than a tile above it, so the words get
                          the width. The word is `text-fg`: muted grey clears
                          4.5:1 on white but not on a tinted wash. */}
                      <span
                        className={`flex h-14 w-14 shrink-0 flex-col items-center justify-center gap-1 rounded-lg ${f.wash}`}
                      >
                        <f.Icon
                          className={`h-5 w-5 ${f.ink}`}
                          strokeWidth={1.5}
                          aria-hidden
                        />
                        <span className="font-mono text-3xs uppercase tracking-wider2 text-fg">
                          {f.label}
                        </span>
                      </span>

                      <span className="min-w-0 flex-1">
                        <span className="flex items-start justify-between gap-2">
                          <span className="text-sm font-semibold leading-snug text-fg">
                            {idea.title}
                          </span>
                          {/* The tick is a SHAPE, so "chosen" survives being
                              read in greyscale. `aria-checked` on the row
                              carries it for anyone not reading at all. */}
                          <span
                            aria-hidden
                            className={
                              active
                                ? "mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-accent text-white"
                                : "mt-0.5 h-5 w-5 shrink-0 rounded-full border border-border"
                            }
                          >
                            {active && (
                              <Check className="h-3 w-3" strokeWidth={2.5} />
                            )}
                          </span>
                        </span>
                        {/* ALL of it, on every row. Two clamped lines was
                            enough to tell three ideas apart and not enough to
                            read one, and the reader has to read them to
                            choose. */}
                        <span className="mt-1 block text-xs leading-relaxed text-muted">
                          {idea.what_you_see}
                        </span>
                        {idea.why_it_works && (
                          <span className="mt-1.5 block text-xs leading-relaxed text-muted">
                            <span className="font-medium text-fg">
                              Why it works:
                            </span>{" "}
                            {idea.why_it_works}
                          </span>
                        )}
                        <span className="mt-2 block font-mono text-3xs uppercase tracking-wider2 text-muted">
                          Opens in {ROADMAP_TOOL_LABELS[t]}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>
            )}

            <form
              onSubmit={(e) => {
                e.preventDefault();
                void runBrainstorm(feedback);
              }}
              className="space-y-2 border-t border-border pt-4"
            >
              <label
                htmlFor={feedbackId}
                className="block text-xs font-medium text-fg"
              >
                None of these? Say what you want instead
              </label>
              <div className="flex flex-col gap-2 sm:flex-row">
                <input
                  id={feedbackId}
                  type="text"
                  value={feedback}
                  onChange={(e) => setFeedback(e.target.value)}
                  disabled={thinking}
                  placeholder="e.g. no talking, filmed at night, funnier"
                  className="input flex-1 text-sm"
                />
                <button
                  type="submit"
                  disabled={thinking}
                  className="btn btn-ghost text-sm whitespace-nowrap"
                >
                  <RefreshCw
                    className={thinking ? "h-4 w-4 animate-spin" : "h-4 w-4"}
                    aria-hidden
                  />
                  {regenLabel}
                </button>
              </div>
              <p className="text-2xs leading-relaxed text-muted">
                The three above get replaced with three new ones. You will not
                be shown the same idea twice.
              </p>
            </form>

            {/* Only when the brainstorm is a dead end: the old one-step flow is
                still a working way out, rather than a dialog you can only
                cancel. */}
            {ideasError && ideas.length === 0 && (
              <button
                type="button"
                onClick={() => {
                  setError(null);
                  setStep("tool");
                }}
                className="btn btn-ghost w-full text-sm"
              >
                Skip the ideas and let the AI choose the angle
              </button>
            )}
          </div>
        )}

        {/* ---------------------------- STEP 2 ---------------------------- */}
        {step === "tool" && (
          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
            <div className="card bg-bg-muted p-3">
              <p className="eyebrow mb-1">Your idea</p>
              {chosen ? (
                <>
                  <p className="text-sm font-semibold text-fg break-words">
                    {chosen.title}
                  </p>
                  <p className="mt-1 text-2xs leading-relaxed text-muted">
                    {chosen.what_you_see}
                  </p>
                  {/* The other half of the card's disclosure. The reason for
                      picking this idea is worth more here than it was in the
                      grid: this is the screen where it gets committed to. */}
                  {chosen.why_it_works && (
                    <p className="mt-1 text-2xs leading-relaxed text-muted">
                      <span className="font-medium text-fg">Why it works:</span>{" "}
                      {chosen.why_it_works}
                    </p>
                  )}
                </>
              ) : (
                <p className="text-sm text-muted">
                  No idea picked. The AI will choose its own angle.
                </p>
              )}
              <button
                type="button"
                onClick={() => {
                  setError(null);
                  setStep("idea"); // selection and the seen-titles list survive
                }}
                className="btn btn-ghost mt-2 text-xs"
              >
                <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
                Change idea
              </button>
            </div>

            <p className="text-xs text-muted leading-relaxed">
              {source.toolIntro}
            </p>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {source.tools.map((t) => (
                <button
                  key={t}
                  type="button"
                  onClick={() => setTool(t)}
                  aria-pressed={t === tool}
                  // Complete literal class strings — Tailwind's scanner never
                  // sees a concatenated `"border-" + x`. The ring matches the
                  // idea cards: `.card` is a surface, not a control, so it
                  // brings no focus style of its own and one has to be said.
                  className={
                    t === tool
                      ? "card card-hover border-accent p-3 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
                      : "card card-hover p-3 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
                  }
                >
                  <div className="flex items-start justify-between gap-2">
                    <p className="text-sm font-semibold text-fg">
                      {ROADMAP_TOOL_LABELS[t]}
                    </p>
                    {/* A tick, because a teal border is the only other thing
                        saying which of the eight is chosen, and a border colour
                        is exactly the signal `color-not-only` rules out. */}
                    {t === tool && (
                      <Check
                        className="h-4 w-4 shrink-0 text-accent"
                        strokeWidth={2.5}
                        aria-hidden
                      />
                    )}
                  </div>
                  <p className="mt-1 text-2xs leading-relaxed text-muted">
                    {TOOL_BLURB[t]}
                  </p>
                </button>
              ))}
            </div>

            {/* Same bargain as step 1: the bars say "busy", the sentence says
                how long, and it is always mounted so it is a live region
                before the wait starts rather than one that arrives with it. */}
            <p role="status" className="min-h-[1rem] text-2xs text-muted">
              {busy ? "Writing the brief. This usually takes 10 to 30 seconds." : ""}
            </p>

            {busy && (
              <div className="space-y-2" aria-hidden>
                {Array.from({ length: 3 }).map((_, i) => (
                  <Skeleton key={i} h="h-4" className="w-full" />
                ))}
              </div>
            )}

            {error && <FlowError problem={error} />}
          </div>
        )}

        <div className="flex shrink-0 items-center justify-end gap-2 border-t border-border p-4">
          {step === "tool" && (
            <button
              type="button"
              onClick={() => {
                setError(null);
                setStep("idea");
              }}
              className="btn btn-ghost mr-auto text-sm"
            >
              <ArrowLeft className="h-4 w-4" aria-hidden />
              Back
            </button>
          )}
          <button type="button" onClick={onClose} className="btn btn-ghost text-sm">
            Cancel
          </button>
          {step === "idea" ? (
            <button
              type="button"
              onClick={() => setStep("tool")}
              disabled={!chosen}
              className="btn btn-primary text-sm"
            >
              <Sparkles className="h-4 w-4" aria-hidden />
              Use this idea
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void go()}
              disabled={busy}
              className="btn btn-primary text-sm"
            >
              <Sparkles className="h-4 w-4" aria-hidden />
              {busy
                ? "Writing the brief…"
                : `Open in ${ROADMAP_TOOL_LABELS[tool]}`}
            </button>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

// ---------------------------------------------------------------------------
// The idea row's format badge
// ---------------------------------------------------------------------------

/** Icon, wash and word per output format. Complete literal class strings, so
 *  Tailwind's scanner never has to see a concatenated `"bg-" + x`. The wash
 *  colours are ones the app already uses elsewhere (fuchsia for the on-camera
 *  tool, indigo on the assets grid, the brand teal); nothing new is introduced
 *  here, and none of them carries meaning on its own. */
const FORMAT_TILE: Record<
  string,
  { Icon: typeof Film; wash: string; ink: string; label: string }
> = {
  reel: {
    Icon: Film,
    wash: "bg-gradient-to-br from-fuchsia-500/15 to-fuchsia-500/5",
    ink: "text-fuchsia-600",
    label: "Video",
  },
  carousel: {
    Icon: Layers,
    wash: "bg-gradient-to-br from-indigo-500/15 to-indigo-500/5",
    ink: "text-indigo-600",
    label: "Carousel",
  },
  static: {
    Icon: ImageIcon,
    wash: "bg-gradient-to-br from-accent/15 to-accent/5",
    ink: "text-accent",
    label: "Poster",
  },
};
