/**
 * "Make this" for a GENERAL trend.
 *
 * A thin adapter. The flow itself — three ideas first, then the Producer tool
 * that builds the chosen one — lives in `MakeThisFlow`, which was extracted
 * from this file when the owner asked for the same "Make this" on competitor
 * posts and Industry Signal stories, with the SAME components rather than a
 * second copy. Nothing on screen changed in the move.
 *
 * What is left here is only what a trend makes different: it is the one subject
 * that arrives as a bare hashtag, so the backend looks it up before writing the
 * ideas and the flow shows what it found — hence `researches: true`, which no
 * other feed sets.
 */
import { useMemo } from "react";

import type { GeneralTrend } from "../../lib/types";
import type { RoadmapTool } from "../yas/roadmap/types";
import { generalTrendsApi } from "./generalTrendsApi";
import { MakeThisFlow, TOOL_ORDER } from "./MakeThisFlow";
import type { MakeThisSource } from "./MakeThisFlow";

/** Sensible default from what the trend IS. A sound / challenge / format is a
 *  person-on-camera thing; a reel-shaped suggestion → the Veo tool; everything
 *  else starts on the poster tool. The user can always change it — this only
 *  decides which card starts selected. Every branch is in `TOOL_ORDER`, which
 *  is the full eight, so a trend can never default to a tool it is not offered. */
export function recommendedTool(trend: GeneralTrend): RoadmapTool {
  const kind = trend.trend_kind;
  if (kind === "sound" || kind === "challenge" || kind === "format") {
    return "talking_head_v2";
  }
  return trend.suggested_format === "reel" ? "general_video" : "poster";
}

function headline(trend: GeneralTrend): string {
  const t = (trend.title || trend.topic || trend.topic_summary || "This trend")
    .replace(/\s+/g, " ")
    .trim();
  return t.length > 80 ? `${t.slice(0, 79)}…` : t;
}

export function GeneralMakeThisDialog({
  trend,
  onClose,
}: {
  trend: GeneralTrend;
  onClose: () => void;
}) {
  const source = useMemo<MakeThisSource>(() => {
    const where = trend.sources.length
      ? trend.sources.join(" · ")
      : "no sources recorded";
    return {
      eyebrow: "Make this",
      headline: headline(trend),
      subline: `${trend.heat} · ${where}`,
      tools: TOOL_ORDER,
      defaultTool: recommendedTool(trend),
      intro:
        "Three ways 42 could do this trend, written for someone who has never heard of 42. Pick one, or say what you would rather see and get three more.",
      // Word for word the same as the competitor dialog's. Both screens are the
      // same component doing the same thing, and two hand-written versions of
      // one sentence is how they stop being the same screen.
      toolIntro:
        "Pick what you want made. The AI writes what that tool needs for this idea and opens it already filled in.",
      ideaNoun: "trend",
      // The only feed that claims a lookup, because it is the only one that
      // runs one: a hashtag on its own can be read entirely the wrong way, and
      // the card above the ideas is the proof that it wasn't.
      researches: true,
      brainstorm: async (opts) => {
        const res = await generalTrendsApi.brainstorm(trend.id, opts);
        // `research` is absent on a backend that predates the lookup and empty
        // when the search failed — the flow keeps whatever it last had, so an
        // unlucky round two cannot erase what round one proved.
        return { ideas: res.ideas, research: res.research ?? null };
      },
      prefill: async (tool, idea) => {
        const { prefill } = await generalTrendsApi.ideas(trend.id, tool, idea);
        return prefill;
      },
    };
  }, [trend]);

  return <MakeThisFlow source={source} onClose={onClose} />;
}
