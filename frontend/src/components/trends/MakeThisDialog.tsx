/**
 * "Make this" for a COMPETITOR POST or an Industry Signal story.
 *
 * A thin adapter. The flow itself — three concrete ideas first, then the
 * Producer tool that builds the one you picked — lives in `MakeThisFlow`,
 * shared with the general-trends feed: the owner asked for the same "Make this"
 * on every feed and explicitly for the SAME components, not a second copy of a
 * dialog that would then drift.
 *
 * What is left here is only what a post makes different: which tools it may be
 * built with, what the header says about it, and the two calls to make. The
 * exported names and props are unchanged, so `TrendsFeed` needed no edit.
 *
 * The tool still has to be chosen BEFORE the brief is written, not after: a
 * poster brief, a Veo prompt and a casting brief are different shapes of text,
 * and asking for all eight in one reply is exactly what makes the model
 * truncate. One post, one tool, one call.
 */
import { useMemo } from "react";

import type { RoadmapTool } from "../yas/roadmap/types";
import { MakeThisFlow, TOOL_ORDER } from "./MakeThisFlow";
import type { MakeThisSource } from "./MakeThisFlow";
import { trendsApi } from "./trendsApi";
import type { TrendPost } from "./types";

/** Sensible default from the source post's format. VIDEO → the tool that makes
 *  a person-on-camera reel; stills → the poster tool. The user can always
 *  change it — this only decides which card starts selected. */
export function recommendedTool(post: TrendPost): RoadmapTool {
  return post.media_type === "VIDEO" ? "talking_head_v2" : "poster";
}

/**
 * News offers four of the eight. The excluded four are not merely worse here —
 * each is actively wrong for a story:
 *
 *   `general_video`   a Veo clip depicting a real news event is a factual
 *                     accuracy hazard on a school's public account. This is
 *                     the one exclusion that is about risk rather than fit.
 *   `broll`           needs a physical shoot, which defeats the timeliness
 *                     that makes a news post worth making at all.
 *   `human_character` writes a CASTING brief, not a post.
 *   `video_editor`    has no brief box — only a project name would arrive.
 */
const NEWS_TOOL_ORDER: RoadmapTool[] = [
  "poster",
  "poster_human",
  "talking_head_v2",
  "animation",
];

/**
 * `recommendedTool` clamped into the list it will actually be shown beside.
 *
 * A default that is not in `tools` is not a cosmetic problem: the picker opens
 * with no card highlighted, the button offers to open a tool that is nowhere on
 * screen, and pressing it sends an excluded tool to `/ideas` — for a story,
 * precisely the `general_video` the list above rules out on accuracy grounds.
 *
 * Today's rule survives the news list by luck rather than by design: a news row
 * has no `media_type` (an article has no Instagram media format), so it returns
 * `poster`, and its other branch, `talking_head_v2`, happens to be listed too.
 * Nothing enforced that, and either list could be edited without the other, so
 * the pairing is made structural here instead of left to hold by coincidence.
 */
function defaultToolFor(post: TrendPost, tools: RoadmapTool[]): RoadmapTool {
  const rec = recommendedTool(post);
  return tools.includes(rec) ? rec : tools[0];
}

/**
 * What the picker puts above the three ideas.
 *
 * The caption branch is load-bearing, not a nicety. Scoring is a separate
 * batched pass, so a freshly ingested row has no `topic` and no
 * `topic_summary` — and `TrendsFeed.makeThis` deliberately admits such a row on
 * the caption alone. Without the fallback the header read the literal "This
 * post" over three ideas about a named story.
 *
 * News inverts the order for the same reason the card does (`headlineFor` in
 * TrendsFeed): the real headline IS the story, while the scorer's angle is
 * 42's idea about it, and putting our interpretation up top presents it as the
 * news.
 */
function headline(post: TrendPost): string {
  const caption = post.caption?.trim();
  const best =
    post.source === "news"
      ? caption || post.topic || post.topic_summary
      : post.topic || post.topic_summary || caption;
  const t = (best || "This post").replace(/\s+/g, " ").trim();
  return t.length > 80 ? `${t.slice(0, 79)}…` : t;
}

/** "24 Aug". `TrendsFeed` keeps an identical private one — `shared.tsx` is
 *  where a third caller should move it to. */
function fmtDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

/**
 * The mono line under the headline.
 *
 * A news story has no account behind it. `username` on those rows holds the
 * outlet's name — `news_ingest` writes `item.outlet or source.name` into it —
 * so rendering it as "@handle" the way the competitor feed does would invent an
 * Instagram account that does not exist. `platform` carries the real publisher
 * (for an aggregated story that is the original outlet, never the aggregator),
 * so a story names where it ran and when instead.
 */
function subline(post: TrendPost): string {
  if (post.source === "news") {
    const outlet = post.platform || post.username || "Industry Signal";
    const when = post.posted_at ? fmtDate(post.posted_at) : "recent";
    return when ? `${outlet} · ${when}` : outlet;
  }
  const handle = post.username?.replace(/^@/, "") ?? "";
  return handle ? `@${handle}` : "";
}

export function MakeThisDialog({
  post,
  onClose,
}: {
  post: TrendPost;
  onClose: () => void;
}) {
  const source = useMemo<MakeThisSource>(() => {
    const isNews = post.source === "news";
    const tools = isNews ? NEWS_TOOL_ORDER : TOOL_ORDER;
    return {
      eyebrow: "Make this",
      headline: headline(post),
      subline: subline(post),
      tools,
      defaultTool: defaultToolFor(post, tools),
      // Same component, right noun: one of these is a post someone else made,
      // the other is something that happened.
      intro: isNews
        ? "Three ways 42 could post about this story, written for someone who has never heard of 42. Pick one, or say what you would rather see and get three more."
        : "Three ways 42 could do this post, written for someone who has never heard of 42. Pick one, or say what you would rather see and get three more.",
      toolIntro:
        "Pick what you want made. The AI writes what that tool needs for this idea and opens it already filled in.",
      ideaNoun: isNews ? "story" : "post",
      // No `researches` flag, and no research card: a post arrives with its own
      // caption and the scorer's read of it, so unlike a bare hashtag there is
      // nothing to go and look up first.
      //
      // BOTH CALLS ARE BARE ON PURPOSE. Nothing catches here and substitutes
      // canned ideas. Both go through Vertex, which refuses everything while
      // the Google Cloud project has billing off, and the temptation when
      // demoing is to fall back to a fixture so the screen still fills. Then
      // the screen is lying: the owner picks an idea that no model wrote, sends
      // it to a Producer tool that cannot run either, and the one thing they
      // needed to know (this is switched off, here is who can switch it on)
      // never reaches them. The failure is the honest output, and
      // `MakeThisFlow.problemFor` is where it is turned into a sentence.
      brainstorm: async (opts) => {
        const res = await trendsApi.postBrainstorm(post.id, opts);
        return { ideas: res.ideas };
      },
      prefill: async (tool, idea) => {
        const { prefill } = await trendsApi.postIdeas(post.id, tool, idea);
        return prefill;
      },
    };
  }, [post]);

  return <MakeThisFlow source={source} onClose={onClose} />;
}
