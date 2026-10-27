/**
 * Today's top picks: the daily AI and coding radar, shown inside the SMAA
 * intelligence Feed. "Competitor Activity" picks belong under Competitors /
 * Sentiment, every other pick under Industry Signal, both under All.
 */
import { useCallback, useEffect, useState } from "react";
import {
  Bot,
  Brain,
  Cog,
  Code2,
  ExternalLink,
  FlaskConical,
  GitBranch,
  GraduationCap,
  Landmark,
  Lightbulb,
  RefreshCw,
  Rocket,
  ShieldCheck,
  Swords,
  Wand2,
  Wrench,
} from "lucide-react";

import { useToast } from "../ToastProvider";
import { Skeleton } from "../Skeleton";
import { api, type Radar, type RadarCard } from "../../lib/api";
import { sendProducerBrief } from "../../lib/producerBrief";
import { usePoll } from "../../lib/usePoll";

const COMPETITOR_CATEGORY = "Competitor Activity";

const CATEGORY_ICON: Record<string, typeof Brain> = {
  "AI Models": Brain,
  "AI Agents": Bot,
  Coding: Code2,
  "Developer Tools": Wrench,
  "Open Source": GitBranch,
  Robotics: Cog,
  Cybersecurity: ShieldCheck,
  "AI Education": GraduationCap,
  Startups: Rocket,
  Research: FlaskConical,
  "UAE/GCC AI": Landmark,
  "Competitor Activity": Swords,
};

/** Five small bars, filled up to `value`. Colour is never the only signal:
 *  the number is in the label for screen readers. */
function Meter({ label, value }: { label: string; value: number }) {
  return (
    <div className="min-w-0" role="img" aria-label={`${label}: ${value} out of 5`}>
      <div className="flex gap-[3px]">
        {[1, 2, 3, 4, 5].map((n) => (
          <span
            key={n}
            className={[
              "h-2 flex-1 rounded-[1px]",
              n <= value ? "bg-k-cyan" : "bg-fg/15",
            ].join(" ")}
          />
        ))}
      </div>
      <p className="mt-1 text-[11px] font-semibold uppercase tracking-[0.05em] text-fg/60 truncate">
        {label}
      </p>
    </div>
  );
}

function Card({ card, rank }: { card: RadarCard; rank: number }) {
  const toast = useToast();
  const Icon = CATEGORY_ICON[card.category] ?? Brain;

  function makePost() {
    sendProducerBrief({
      tool: "poster",
      title: card.idea || card.title,
      topic: `${card.idea || card.title}. Based on: ${card.title}. ${card.why}`.trim(),
      dialogue: [],
      introTopic: "",
      outroTopic: "",
      character: "",
      notes: card.why,
      postId: 0,
      permalink: card.url,
    });
    toast.success("Opening Poster", card.idea || card.title);
  }

  return (
    <article className="flex flex-col gap-4 p-5 bg-k-surface border border-k-border">
      <div className="flex items-center gap-3">
        <span className="k-display text-[28px] leading-none text-k-accent tabular-nums">
          {rank}
        </span>
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 text-[12px] font-bold uppercase tracking-[0.05em] bg-k-cyan/15 text-k-accent">
          <Icon className="w-4 h-4" strokeWidth={2} aria-hidden />
          {card.category}
        </span>
      </div>

      <div className="space-y-1.5">
        <h2 className="text-[19px] font-bold leading-snug text-fg text-balance">
          {card.title}
        </h2>
        {card.why && <p className="text-[15px] text-fg/70">{card.why}</p>}
      </div>

      <div className="grid grid-cols-3 gap-3">
        <Meter label="Important" value={card.important} />
        <Meter label="Good for 42" value={card.good_for_42} />
        <Meter label="Post it" value={card.post_it} />
      </div>

      {card.idea && (
        <p className="flex gap-2 items-start text-[15px] font-semibold text-fg">
          <Lightbulb className="w-5 h-5 shrink-0 mt-px text-k-accent" strokeWidth={2} aria-hidden />
          {card.idea}
        </p>
      )}

      <div className="mt-auto flex flex-wrap gap-3 pt-1">
        <a
          href={card.url}
          target="_blank"
          rel="noopener noreferrer"
          className="k-btn-ghost"
        >
          <ExternalLink className="w-4 h-4" aria-hidden />
          Open
        </a>
        <button type="button" onClick={makePost} className="k-btn">
          <Wand2 className="w-4 h-4" aria-hidden />
          Make a post
        </button>
      </div>
    </article>
  );
}

export function RadarPicks({ which }: { which: "" | "competitor" | "news" }) {
  const toast = useToast();
  const [radar, setRadar] = useState<Radar | null>(null);
  const [starting, setStarting] = useState(false);

  const load = useCallback(async () => {
    try {
      const next = await api.radar.get();
      setRadar(next);
      return next;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // While a build runs, look again every few seconds; stop when it is done.
  usePoll({
    enabled: Boolean(radar?.building),
    intervalMs: 3000,
    onTick: async () => {
      const next = await load();
      if (next && !next.building) {
        if (next.error) toast.error("Could not refresh", "Showing the last picks.");
        return "stop";
      }
    },
  });

  async function refresh() {
    setStarting(true);
    try {
      setRadar(await api.radar.refresh());
    } catch {
      toast.error("Could not start", "Try again in a moment.");
    } finally {
      setStarting(false);
    }
  }

  const building = Boolean(radar?.building) || starting;
  const items = (radar?.items ?? []).filter((i) => {
    const competitor = i.category === COMPETITOR_CATEGORY;
    return which === "" || (which === "competitor") === competitor;
  });

  if (!radar) return <Skeleton h="h-40" w="w-full" />;
  // Nothing to show for this chip and nothing being built: stay out of the way.
  if (items.length === 0 && !building) return null;

  return (
    <section className="space-y-4" aria-label="Today's top picks">
      <div className="flex items-center justify-between gap-3">
        <h2 className="k-display text-[22px] text-fg">Today's top picks</h2>
        <button type="button" onClick={refresh} disabled={building} className="k-btn">
          <RefreshCw className={["w-4 h-4", building ? "animate-spin" : ""].join(" ")} aria-hidden />
          {building ? "Looking…" : "Refresh"}
        </button>
      </div>
      {items.length > 0 && (
        <div className="grid gap-5 lg:grid-cols-2">
          {items.map((card) => (
            <Card key={card.url} card={card} rank={radar.items.indexOf(card) + 1} />
          ))}
        </div>
      )}
    </section>
  );
}
