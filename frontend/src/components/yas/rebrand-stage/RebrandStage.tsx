// The Rebrand stage: the reel's scenes in one thin row — Intro · Main 1 · …
// · Outro, only what the reel has (the server's reel block lists them) —
// with Finalize at its right end, and the Rebrand studio on the scene
// picked, inline, at the size the owner approved.
//
//   ┌ Main 1 · Main 2 · …           pill ↶ ↷ ⋯ │ Finalize → ┐  ≤ 48 px
//   │ the studio (filmstrip | stage | rail, Generate under them)   │
//
// The studio is mounted per scene (keyed by run, target and variant): every
// ref and saved key in it starts clean for the scene it opens, while the
// draft (this browser) and its undo (the page session) are kept per scene.
// The intro and outro have tiles when the server's studio opens them (the
// reel block's `bookends`; an older server would refuse them): the studio
// opens them empty, by hand only (its `kind`).
//
// A main scene kept past its automatic cut (its end set after a Veo tail
// flash) is branded on its whole clip: when the studio's Next wants those
// frames (the grip dragged into them, or Next shown with the end there),
// the stage prepares the whole clip in the background (free: the server's
// CPU), and mounts the studio again on it (the key's variant "full") once
// it is ready and the grip is let go — never mid-drag. Back at or before
// the cut, the studio goes back to the trimmed clip. The draft is the
// scene's either way, and the studio opens again on what it showed.
//
// What ships is the server's (run.reel): a tile says "Rebranded" when a
// version goes in (its picture becomes that version's), "Trimmed" when the
// original goes in with an end (or a main scene's start) set by hand, spins
// while a version is being made, and shows one amber "!" when something
// needs a look (the reason in its title). A click in the studio's filmstrip
// moves the ✓ and the tile at once (useReelChoices), and so does a move of
// the scene's end or start on the studio's timeline (its grips). Finalize
// says what it will do: "Making Main 2…"
// while a version is on its way (it would ship the older pick without
// saying so), "Rendering…", "See final →" when nothing that goes in changed
// since the reel was made, else "Finalize →". Either, pressed while a scene
// holds edits made this visit that were never generated, first asks in one
// line ("Main 2: edits not generated" [Show] [Finalize]).
//
// From lg up the page goes full-bleed (the app sidebar stays, as on every
// other step: the owner wants it there), with an 8 px margin at the sides,
// and the filmstrip is a narrow column so the stage keeps its width; on entry
// the stepper row is scrolled to the top of the screen once, and the stage
// fills the rest of it (at least 600 px), 16 px above the bottom. Below lg
// the studio stacks as it always did, edge to edge, the scene row stays
// under the mobile header, and Generate stays at the bottom of the screen.

import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { Film } from "lucide-react";

import { useFullBleed } from "../../LayoutChrome";
import { useToast } from "../../ToastProvider";
import { Spinner } from "../../ui/Spinner";
import { finalizeState, reelUnchanged, resolveTarget, stripTargets, tileState } from "../../../lib/rebrandStage";
import type { ReelTarget, TalkingHeadRun } from "../../../lib/types";
import { RebrandStudio } from "../rebrand-studio/RebrandStudio";
import { clearEdited, studioDraftKey } from "../rebrand-studio/useDraftHistory";
import { prepareAhead } from "../rebrand-studio/useStudioScene";
import { useReelChoices } from "./useReelChoices";

const LG = "(min-width: 1024px)";
type Whole = "preparing" | "ready" | "none" | "failed";
/** The mobile header's height (Layout: h-12): the scene row sticks under it. */
const MOBILE_HEADER = 48;

function useIsLg(): boolean {
  const [lg, setLg] = useState(() => typeof window !== "undefined" && window.matchMedia(LG).matches);
  useEffect(() => {
    const mq = window.matchMedia(LG);
    const on = () => setLg(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return lg;
}

/** The stage's height from lg up: the rest of the screen under the stepper
 *  row, at least 600 px (the studio's grid needs a definite height). On
 *  entry the stepper row is scrolled to the top of the screen, once. */
function useStageHeight(anchorRef: RefObject<HTMLElement>, cardRef: RefObject<HTMLElement>, lg: boolean) {
  const [height, setHeight] = useState<number | null>(null);
  useLayoutEffect(() => {
    if (!lg) {
      setHeight(null);
      return;
    }
    const measure = () => {
      const a = anchorRef.current;
      const c = cardRef.current;
      if (!a || !c) return;
      const offset = c.getBoundingClientRect().top - a.getBoundingClientRect().top;
      setHeight(Math.max(600, Math.floor(window.innerHeight - offset - 16)));
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [lg, anchorRef, cardRef]);
  const scrolled = useRef(false);
  useLayoutEffect(() => {
    if (scrolled.current || (lg && height == null)) return;
    const a = anchorRef.current;
    if (!a) return;
    scrolled.current = true;
    const top = a.getBoundingClientRect().top - (lg ? 0 : MOBILE_HEADER);
    window.scrollTo({ top: Math.max(0, window.scrollY + top), behavior: "auto" });
  }, [lg, height, anchorRef]);
  return height;
}

/** A tile's picture: the server's (the shipped version's first still, else
 *  the keyframe or card), then the run's own keyframe or card; a film glyph
 *  when none loads. */
function Thumb({ urls, portrait }: { urls: (string | null | undefined)[]; portrait: boolean }) {
  const list = urls.filter((u): u is string => !!u);
  const key = list.join("|");
  const [i, setI] = useState(0);
  useEffect(() => setI(0), [key]);
  const size = portrait ? "h-8 w-[22px]" : "h-[22px] w-10";
  const url = list[i];
  return (
    <span className={`relative hidden shrink-0 overflow-hidden rounded-[4px] bg-black sm:block ${size}`}>
      {url ? (
        <img
          src={url}
          alt=""
          draggable={false}
          className="h-full w-full object-cover"
          onError={() => setI((n) => n + 1)}
        />
      ) : (
        <span className="flex h-full w-full items-center justify-center text-white/50">
          <Film className="h-3 w-3" aria-hidden />
        </span>
      )}
    </span>
  );
}

export function RebrandStage({
  run,
  setRun,
  target,
  onTarget,
  anchorRef,
  rendering,
  onFinalize,
  onSeeFinal,
  onGoVideos,
}: {
  run: TalkingHeadRun;
  /** The creator's run: the reel block a PUT or a poll returns is laid on it. */
  setRun: React.Dispatch<React.SetStateAction<TalkingHeadRun | null>>;
  /** The scene asked for (a refresh, a tile click); the stage opens the
   *  first included main scene when it is not in the reel. */
  target: string | null;
  onTarget: (target: string) => void;
  /** The stepper row: scrolled to the top of the screen on entry. */
  anchorRef: RefObject<HTMLElement>;
  /** The reel is being made (Finalize waits). */
  rendering: boolean;
  /** Finalize: freeze the reel as the server last returned it (`expectSha`). */
  onFinalize: (expectSha: string | null) => void;
  /** Finalized and nothing changed: straight to Final, no render. */
  onSeeFinal: () => void;
  onGoVideos: () => void;
}) {
  useFullBleed();
  const toast = useToast();
  const lg = useIsLg();
  const cardRef = useRef<HTMLDivElement>(null);
  const height = useStageHeight(anchorRef, cardRef as RefObject<HTMLElement>, lg);
  const [slot, setSlot] = useState<HTMLDivElement | null>(null);
  const ids = useId();

  const runRef = useRef(run);
  runRef.current = run;
  const choices = useReelChoices({
    run,
    setRun,
    onRefused: (message) => toast.error("Couldn't change what goes in the reel", message),
  });
  // A scene got ready: the reel is read again (preparing it may have
  // fetched its clip from the bucket, and an intro or outro whose Veo clip
  // is now here goes in instead of its card: a new inputs hash).
  const refresh = choices.refresh;
  const onPrepared = useCallback(() => void refresh(), [refresh]);
  const reel = run.reel ?? null;
  // The intro and outro only when this server's studio opens them.
  const bookends = reel?.bookends === true;
  const tiles = stripTargets(reel, bookends);
  const current = resolveTarget(reel, target, bookends);
  const tile = tiles.find((t) => t.target === current) ?? null;
  const portrait = run.aspect_ratio !== "16:9";

  // The whole clip past the automatic cut, per scene: being got ready,
  // ready, not on this server (its answer has no variant), or not got
  // ready this time (asked again by the next ask).
  const [whole, setWhole] = useState<Record<string, Whole>>({});
  const wholeRef = useRef(whole);
  wholeRef.current = whole;
  // The scene whose grip is being dragged (the studio stays as it is).
  const [dragging, setDragging] = useState<string | null>(null);
  const onEndDrag = useCallback((t: string, frame: number | null) => setDragging(frame != null ? t : null), []);
  const onWantFull = useCallback(
    (t: string) => {
      const was = wholeRef.current[t];
      if (was && was !== "failed") return;
      setWhole((w) => ({ ...w, [t]: "preparing" }));
      const at = runRef.current.reel?.targets.find((x) => x.target === t)?.end.user ?? null;
      prepareAhead(run.id, t, { ...(at != null ? { endFrame: at } : {}), variant: "full" })
        .then((p) => {
          setWhole((w) => ({ ...w, [t]: p.scene.variant === "full" ? "ready" : "none" }));
          void refresh();
        })
        // Not now: Generate says so (with Try again), and the next ask
        // tries again.
        .catch(() => setWhole((w) => ({ ...w, [t]: "failed" })));
    },
    [run.id, refresh],
  );
  // Past the cut, once the whole clip is ready and the grip let go.
  const variantOf = (t: ReelTarget): "full" | "base" =>
    t.end.user != null && t.end.auto != null && t.end.user > t.end.auto && whole[t.target] === "ready" && dragging !== t.target
      ? "full"
      : "base";
  const variant = tile ? variantOf(tile) : "base";
  // Mounted again on the same scene (its clip changed): it opens on what it
  // showed.
  const mounted = useRef<{ target: string; variant: string } | null>(null);
  const resume = !!tile && !!mounted.current && mounted.current.target === tile.target && mounted.current.variant !== variant;
  useEffect(() => {
    mounted.current = tile ? { target: tile.target, variant } : null;
  });

  // A scene asked for that is no longer in the reel: the URL says the one
  // shown.
  useEffect(() => {
    if (current && current !== target) onTarget(current);
  }, [current, target, onTarget]);

  // Scenes whose draft holds edits made this visit that were never
  // generated (the studio says so; kept when it switches to another scene).
  const [unmade, setUnmade] = useState<Record<string, boolean>>({});
  const onDraftState = useCallback((t: string, u: boolean) => {
    setUnmade((m) => (!!m[t] === u ? m : { ...m, [t]: u }));
  }, []);
  const [guard, setGuard] = useState<string[] | null>(null);

  // A tile switch lands every pick first (the studio it leaves may have
  // just sent one).
  const switching = useRef(false);
  const pickTile = async (t: ReelTarget) => {
    if (t.target === current || switching.current) return;
    switching.current = true;
    try {
      await choices.flush();
    } finally {
      switching.current = false;
    }
    setGuard(null);
    onTarget(t.target);
  };
  const tabRefs = useRef(new Map<string, HTMLButtonElement>());
  const onTabKey = (e: React.KeyboardEvent, i: number) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const n = tiles[(i + (e.key === "ArrowRight" ? 1 : tiles.length - 1)) % tiles.length];
    if (!n) return;
    void pickTile(n);
    tabRefs.current.get(n.target)?.focus();
  };

  const makingOf = (t: ReelTarget) => t.making ?? choices.started[t.target]?.id ?? null;
  const makingTile = tiles.find((t) => makingOf(t) != null) ?? null;
  const fin = finalizeState({ making: makingTile?.label ?? null, rendering, unchanged: reelUnchanged(run) });

  // Finalize (or See final): the one line first, when a scene holds edits
  // never generated; then every pick lands, and what to do is decided on
  // the run as it is then (a pick just made makes the reel new).
  const proceed = async () => {
    setGuard(null);
    await choices.flush();
    // The last block the server returned is on screen (its inputs_sha is
    // the one Finalize sends).
    const want = choices.lastSha();
    for (let i = 0; i < 100 && want && runRef.current.reel?.inputs_sha !== want; i++) {
      await new Promise((r) => window.setTimeout(r, 10));
    }
    const r = runRef.current;
    if (reelUnchanged(r)) onSeeFinal();
    else onFinalize(r.reel?.inputs_sha ?? null);
  };
  const onPrimary = () => {
    const names = tiles.filter((t) => unmade[t.target]).map((t) => t.label);
    if (names.length && !guard) {
      setGuard(names);
      return;
    }
    void proceed();
  };
  const finalizeAnyway = () => {
    clearEdited(tiles.filter((t) => unmade[t.target]).map((t) => studioDraftKey(run.id, t.target)));
    setUnmade({});
    void proceed();
  };
  const showUnmade = () => {
    const first = tiles.find((t) => unmade[t.target]);
    setGuard(null);
    if (first) void pickTile(first);
  };

  const thumbs = (t: ReelTarget) => {
    if (t.role === "main") {
      const i = Number(t.target.split("_")[1]);
      const sc = (run.scenes ?? [])[i];
      return [t.thumb_url, sc?.keyframe_url, i === 0 ? run.main?.url : null];
    }
    return [t.thumb_url, (t.role === "intro" ? run.intro : run.outro)?.url];
  };

  const panelId = `${ids}-panel`;
  const notReady = !run.reel_ready || !reel || !tile;

  return (
    <div
      ref={cardRef}
      style={height != null ? { height } : undefined}
      className="studio-classic card -mx-3 flex flex-col rounded-none border-x-0 p-0 lg:-mx-2 lg:overflow-hidden lg:rounded-xl lg:border-x"
    >
      {/* The scene row (44 px), sticky under the mobile header below lg. */}
      <div className="sticky top-12 z-20 flex h-11 shrink-0 items-center gap-2 border-b border-border bg-bg-subtle pl-2 pr-2 lg:static lg:pr-3">
        <div
          role="tablist"
          aria-label="Scenes"
          className="flex min-w-0 flex-1 items-center gap-1.5 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        >
          {tiles.map((t, i) => {
            const selected = t.target === current;
            const st = tileState(t, { making: makingOf(t), no: choices.no(t.target), endOf: choices.endOf(t.target) });
            const title = [st.makingTitle, st.problem, st.badgeTitle].filter(Boolean).join(" · ") || undefined;
            const badge = st.badge === "rebranded" ? "Rebranded" : st.badge === "trimmed" ? "Trimmed" : null;
            return (
              <button
                key={t.target}
                ref={(el) => {
                  if (el) tabRefs.current.set(t.target, el);
                  else tabRefs.current.delete(t.target);
                }}
                type="button"
                role="tab"
                id={`${ids}-tab-${t.target}`}
                aria-selected={selected}
                aria-controls={panelId}
                tabIndex={selected ? 0 : -1}
                data-target={t.target}
                data-badge={st.badge ?? undefined}
                data-making={st.making ? "" : undefined}
                data-problem={st.problem ? "" : undefined}
                title={title}
                onClick={() => void pickTile(t)}
                onKeyDown={(e) => onTabKey(e, i)}
                className={`flex h-10 shrink-0 items-center gap-2 rounded-lg px-1.5 pr-2.5 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 ${
                  selected ? "bg-accent-fade ring-1 ring-accent" : "hover:bg-bg-muted"
                }`}
              >
                <span className="relative hidden shrink-0 sm:block">
                  <Thumb urls={thumbs(t)} portrait={portrait} />
                  {st.making && (
                    <span className="absolute inset-0 flex items-center justify-center rounded-[4px] bg-black/45">
                      <Spinner size="xs" className="text-white" />
                    </span>
                  )}
                </span>
                <span className="flex min-w-0 flex-col leading-tight">
                  <span className={`flex items-center gap-1 truncate text-xs font-semibold ${selected ? "text-accent-hover" : "text-fg"}`}>
                    {t.label}
                    {st.making && <Spinner size="xs" className="text-accent sm:hidden" />}
                    {st.problem && (
                      <span
                        role="img"
                        aria-label={st.problem}
                        className="inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full bg-warning text-[9px] font-bold leading-none text-white"
                      >
                        !
                      </span>
                    )}
                  </span>
                  {/* The badge's line, kept so a tile never changes height;
                      below sm the badge is a dot. */}
                  <span className="hidden h-3 items-center sm:flex">
                    {badge && (
                      <span className="rounded-full bg-accent-fade px-1.5 text-[10px] font-semibold leading-3 text-accent-hover">
                        {badge}
                      </span>
                    )}
                  </span>
                </span>
                {badge && <span aria-hidden className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent sm:hidden" />}
                {badge && <span className="sr-only sm:hidden">{badge}</span>}
              </button>
            );
          })}
        </div>
        {/* The studio's pill, undo, redo and menu (from lg up). */}
        <div ref={setSlot} className="hidden shrink-0 items-center lg:flex" />
        <span className="hidden h-6 w-px shrink-0 bg-border lg:block" aria-hidden />
        <button
          type="button"
          disabled={!fin.enabled || notReady}
          onClick={onPrimary}
          data-kind={fin.kind}
          className="inline-flex h-9 shrink-0 items-center gap-1.5 rounded-lg bg-accent px-4 text-sm font-semibold text-white transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:bg-bg-muted disabled:text-muted"
        >
          {(fin.kind === "rendering" || fin.kind === "making") && <Spinner size="xs" />}
          {fin.label}
        </button>
      </div>

      {/* Asked once, when a scene holds edits made this visit that were
          never generated. */}
      {guard && (
        <div
          role="alert"
          className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-warning/30 bg-warning/10 px-3 py-1.5 text-2xs text-amber-800"
        >
          <span>{guard.join(", ")}: edits not generated</span>
          <button type="button" onClick={showUnmade} className="font-semibold underline-offset-2 hover:underline">
            Show
          </button>
          <button type="button" onClick={finalizeAnyway} className="font-semibold underline-offset-2 hover:underline">
            {fin.kind === "see" ? "See final" : "Finalize"}
          </button>
        </div>
      )}

      {notReady ? (
        <div className="m-auto space-y-3 px-6 py-16 text-center">
          <p className="text-sm text-fg">Finish the videos first.</p>
          <button type="button" className="btn btn-sm btn-ghost" onClick={onGoVideos}>
            Go to Videos
          </button>
        </div>
      ) : (
        <div
          role="tabpanel"
          id={panelId}
          aria-labelledby={`${ids}-tab-${tile.target}`}
          className="flex min-h-0 flex-1 flex-col"
        >
          <RebrandStudio
            key={`${run.id}:${tile.target}:${variant}`}
            runId={run.id}
            target={tile.target}
            kind={tile.role === "main" ? "scene" : tile.role}
            pick={tile.ships}
            end={tile.end}
            variant={variant === "full" ? "full" : null}
            resume={resume}
            whole={whole[tile.target] ?? null}
            actionsSlot={lg ? slot : null}
            onPick={choices.pick}
            onEnd={choices.end}
            onEndDrag={onEndDrag}
            onStart={choices.start}
            onWantFull={onWantFull}
            onVersionsChanged={choices.noteVersions}
            onDraftState={onDraftState}
            onPrepared={onPrepared}
          />
        </div>
      )}
    </div>
  );
}
