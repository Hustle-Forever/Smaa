// Rebrand studio — the filmstrip: what the one canvas can show.
//
//   Original · v1 … vN · Next
//
// Oldest first; Next (your change, the free preview) is pinned at the end,
// dashed, because it is not a version yet. Vertical beside the stage on a
// wide screen, a row under it on a narrow one. Kept deliberately tiny (a
// 48 px column of small thumbnails, name only): the stage gets the room, and
// the status line lives in the title and the accessible name. Plain buttons
// with aria-pressed (not a fake listbox) and names that say the status. The
// card that goes in the reel carries a small ✓ ("In the reel").

import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Check, Film } from "lucide-react";

import { Spinner } from "../../ui/Spinner";
import { COPY, mmss } from "./studioCopy";
import { sameSel, type StageSel } from "./studioModel";

export interface FilmCard {
  key: string;
  sel: StageSel;
  label: string;
  /** Under the label: "ready", "Failed", or a running clock. */
  sub?: string;
  /** Running since (ms): the sub-line counts up. */
  runningSince?: number;
  thumb: string | null;
  busy?: boolean;
  problem?: "failed" | "stopped";
  tag?: string;
  dashed?: boolean;
  /** Its rating (1-5), once it has one. */
  stars?: number | null;
  /** It is what goes in the reel. */
  inReel?: boolean;
}

function Clock({ since }: { since: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);
  return <span className="font-mono tabular-nums">{mmss((now - since) / 1000)}</span>;
}

function Card({
  card,
  selected,
  portrait,
  pulse,
  onSelect,
}: {
  card: FilmCard;
  selected: boolean;
  portrait: boolean;
  pulse: boolean;
  onSelect: () => void;
}) {
  const aria = COPY.cardAria(
    card.label,
    [
      card.problem === "failed" ? COPY.failedTag : card.problem === "stopped" ? COPY.stoppedTag : card.sub,
      card.tag,
      card.stars ? COPY.ratedAria(card.stars) : null,
      card.inReel ? COPY.inReel.toLowerCase() : null,
    ]
      .filter(Boolean)
      .join(", "),
  );
  return (
    <button
      type="button"
      aria-pressed={selected}
      aria-label={aria}
      title={aria}
      onClick={onSelect}
      className={`group flex shrink-0 flex-col items-stretch gap-1 rounded-xl p-1 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 lg:mx-auto lg:gap-0.5 lg:rounded-lg lg:p-0.5 lg:text-center ${
        portrait ? "w-[52px] lg:w-[36px]" : "w-[84px] lg:w-full"
      } ${selected ? "bg-accent-fade" : "hover:bg-bg-muted"} ${pulse ? "animate-ringPulse" : ""}`}
    >
      <span
        className={`relative block w-full overflow-hidden rounded-lg bg-black ${portrait ? "aspect-[9/16]" : "aspect-video"} ${
          selected ? "ring-2 ring-accent" : card.dashed ? "outline-dashed outline-1 outline-offset-2 outline-border-strong" : "ring-1 ring-border"
        }`}
      >
        {card.thumb ? (
          <img
            src={card.thumb}
            alt=""
            draggable={false}
            loading="lazy"
            className={`h-full w-full object-cover ${card.busy || card.problem ? "opacity-50" : ""}`}
          />
        ) : (
          <span className="flex h-full w-full items-center justify-center text-white/40">
            <Film className="h-4 w-4" aria-hidden />
          </span>
        )}
        {card.busy && (
          <span className="absolute inset-0 flex items-center justify-center">
            <Spinner size="sm" className="text-white" />
          </span>
        )}
        {card.problem === "failed" && (
          <span className="absolute inset-0 flex items-center justify-center text-danger">
            <AlertTriangle className="h-4 w-4" aria-hidden />
          </span>
        )}
        {card.inReel && (
          <span
            title={COPY.inReel}
            data-in-reel
            className="absolute left-0.5 top-0.5 flex h-3.5 w-3.5 items-center justify-center rounded-full bg-accent text-white shadow-soft"
          >
            <Check className="h-2.5 w-2.5" strokeWidth={3} aria-hidden />
          </span>
        )}
        {!!card.stars && (
          <span
            aria-hidden
            className="absolute right-0.5 top-0.5 rounded-full bg-black/70 px-1 py-px font-mono text-3xs font-semibold leading-none text-white tabular-nums"
          >
            <span className="text-warning">★</span>
            {card.stars}
          </span>
        )}
      </span>
      <span className="min-w-0 px-0.5 leading-tight lg:px-0">
        <span className={`block truncate text-2xs font-semibold lg:text-3xs ${selected ? "text-accent-hover" : "text-fg"}`}>{card.label}</span>
        {card.runningSince && (
          // A version being made still shows its clock: the only live signal.
          <span className="block truncate text-3xs text-muted">
            <Clock since={card.runningSince} />
          </span>
        )}
      </span>
    </button>
  );
}

export function StudioFilmstrip({
  cards,
  next,
  sel,
  onSelect,
  portrait,
  nextPulse,
  className = "",
}: {
  /** Original, then the versions, oldest first. */
  cards: FilmCard[];
  next: FilmCard;
  sel: StageSel;
  onSelect: (s: StageSel) => void;
  portrait: boolean;
  /** Next was just switched to by an edit: pulse it once. */
  nextPulse: boolean;
  className?: string;
}) {
  // The card on stage stays in view (a new version lands at the end). The
  // list scrolls itself: scrollIntoView would scroll the page too, now that
  // the studio sits on the page rather than in a window of its own.
  const listRef = useRef<HTMLDivElement>(null);
  const selKey = sel.kind === "version" ? `v${sel.id}` : sel.kind;
  useEffect(() => {
    const list = listRef.current;
    const el = list?.querySelector<HTMLElement>('[aria-pressed="true"]');
    if (!list || !el) return;
    const l = list.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    if (r.top < l.top) list.scrollTop -= l.top - r.top;
    else if (r.bottom > l.bottom) list.scrollTop += r.bottom - l.bottom;
    if (r.left < l.left) list.scrollLeft -= l.left - r.left;
    else if (r.right > l.right) list.scrollLeft += r.right - l.right;
  }, [selKey, cards.length]);

  return (
    <nav
      aria-label={COPY.stripLabel}
      className={`min-h-0 min-w-0 gap-1.5 lg:flex-col lg:gap-1 lg:border-r lg:border-border lg:p-1 ${className || "flex"}`}
    >
      <div
        ref={listRef}
        className="flex min-w-0 flex-1 gap-1.5 overflow-x-auto overflow-y-hidden [scrollbar-width:none] lg:min-h-0 lg:flex-col lg:gap-1 lg:overflow-y-auto lg:overflow-x-hidden [&::-webkit-scrollbar]:hidden"
      >
        {cards.map((c) => (
          <Card
            key={c.key}
            card={c}
            selected={sameSel(c.sel, sel)}
            portrait={portrait}
            pulse={false}
            onSelect={() => onSelect(c.sel)}
          />
        ))}
      </div>
      <div className="shrink-0 border-l border-border pl-1.5 lg:border-l-0 lg:border-t lg:pl-0 lg:pt-1">
        <Card card={next} selected={sameSel(next.sel, sel)} portrait={portrait} pulse={nextPulse} onSelect={() => onSelect(next.sel)} />
      </div>
    </nav>
  );
}
