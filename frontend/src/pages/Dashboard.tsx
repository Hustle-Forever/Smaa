import { useEffect, useId, useRef, useState } from "react";
import type { ComponentType, KeyboardEvent, PointerEvent, ReactNode } from "react";
import {
  AlertTriangle,
  ArrowRight,
  ArrowUpRight,
  CalendarClock,
  CheckCircle2,
  Clapperboard,
  Film,
  FileVideo,
  Heart,
  Image as ImageIcon,
  Instagram,
  Layers,
  Library as LibraryIcon,
  MessageCircle,
  Minus,
  Palette,
  Plus,
  Scissors,
  TrendingDown,
  TrendingUp,
  Wallet,
} from "lucide-react";

import type { Tab } from "../App";
import { api } from "../lib/api";
import { fmtNumber, fmtRel } from "../lib/format";
import { useYasLibraryStore } from "../lib/store";
import type {
  AgentId,
  InstagramOwnSummary,
  ProducerAssetsList,
  YasLibraryItem,
  YasLibraryType,
  YasStats,
} from "../lib/types";
import { Skeleton } from "../components/Skeleton";
import { useToast } from "../components/ToastProvider";
import { useCurrentUser } from "../components/auth/AuthGate";
import { useKCanvas } from "../lib/useStudioCanvas";
import { ChartGate } from "../components/ChartGate";
import { KpiDetail, type KpiKind } from "../components/dashboard/KpiDetail";

interface Props {
  // Kept for App's call signature; the command-center dashboard only navigates.
  onStartRun?: (agent: AgentId, runId: number) => void;
  navigate: (t: Tab, param?: string) => void;
}

// What each content type is called on a tile. Deliberately no colour: this
// is a government-office surface, so type is carried by the words.
const TYPE_META: Record<YasLibraryType, { label: string }> = {
  talking_head: { label: "Human Element Video" },
  talking_head_v2: { label: "Human Element Video" },
  poster: { label: "Poster" },
  poster_human: { label: "Poster + Human" },
  animation: { label: "Animation" },
  video_editor: { label: "Video Editor" },
  general_video: { label: "General AI Video" },
  broll: { label: "B-roll" },
};

// Breakdown keys come from GET /yas-producer/stats (categories_def in the
// router) plus the "animations" row this page adds. Hex, not classes, because
// they feed inline widths on the bars.
const CATEGORY_HEX: Record<string, string> = {};
// One ink for every tool: the label says which tool it is, colour is kept
// for state (gold = still being made, red = did not work).
const CATEGORY_FALLBACK_HEX = "rgb(var(--k-text))";

function fmtMoney(n: number | null | undefined): string {
  const v = n ?? 0;
  return `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtCompact(n: number | null | undefined): string {
  if (n == null) return "—";
  return new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(n);
}

function greeting(now: Date): string {
  const h = now.getHours();
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  return "Good evening";
}

function plural(n: number, one: string, many: string): string {
  return `${fmtNumber(n)} ${n === 1 ? one : many}`;
}

/** "2026-09-17" as a LOCAL date. `new Date("2026-09-17")` is UTC midnight,
 *  which lands on the 16th anywhere west of Greenwich. */
function parseDay(day: string): Date {
  const [y, m, d] = day.slice(0, 10).split("-").map(Number);
  return new Date(y, (m || 1) - 1, d || 1);
}

type IgState = "loading" | "ready" | "error";

/** Design preview with sample numbers: dev server only, and only with `?demo`
 *  in the URL. `import.meta.env.DEV` is false in every production build, so
 *  this can never show sample numbers on the live site. */
const DEMO: boolean =
  Boolean((import.meta as any).env?.DEV) &&
  typeof window !== "undefined" &&
  new URLSearchParams(window.location.search).has("demo");

function sampleData(): {
  stats: YasStats;
  assets: ProducerAssetsList["totals"];
  ig: InstagramOwnSummary;
} {
  const today = new Date();
  const counts = [1, 0, 2, 3, 1, 0, 2, 4, 3, 5, 2, 3, 6, 4];
  const iso = (d: Date) =>
    `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const activity_14d = counts.map((count, i) => {
    const d = new Date(today);
    d.setDate(today.getDate() - (13 - i));
    return { day: iso(d), count };
  });
  const posts = Array.from({ length: 9 }, (_, k) => {
    const d = new Date(today);
    d.setDate(today.getDate() - k * 3);
    return {
      published_at: d.toISOString(),
      permalink: null,
      views: 1400 + ((k * 537) % 2100),
      likes: 60 + ((k * 41) % 140),
      comments: 4 + ((k * 7) % 18),
    };
  });
  const sum = (key: "views" | "likes" | "comments") => posts.reduce((a, p) => a + p[key], 0);
  return {
    stats: {
      generations: {
        total: 11,
        by_status: { ready: 8, generating: 0, failed: 1, saved: 2 },
        categories: [
          { key: "video_edit", label: "Editor projects", total: 4, ready: 4, generating: 0, failed: 0, saved: 0 },
          { key: "poster_human", label: "Poster + Human", total: 4, ready: 3, generating: 0, failed: 0, saved: 1 },
          { key: "talking_head_v2", label: "Human Element Video", total: 3, ready: 1, generating: 0, failed: 1, saved: 1 },
        ],
        reels: 3,
        posters: 4,
        editor_projects: 4,
      },
      animations: { total: 8, ready: 7, generating: 1, failed: 0 },
      spend: { generations_usd: 5.1, animations_usd: 3.27, total_usd: 8.37 },
      activity_14d,
      funnel: { pending: 18, in_process: 1, declined: 0, approved: 9 },
    },
    assets: { total: 87, images: 60, videos: 20, fonts: 3, audio: 2, characters: 2, stickers: 0, enabled_count: 87 },
    ig: {
      total_posts: posts.length,
      total_likes: sum("likes"),
      total_comments: sum("comments"),
      total_views: sum("views"),
      latest_post_at: posts[0].published_at,
      metrics_available: true,
      posts,
    },
  };
}

export function DashboardPage({ navigate }: Props) {
  const [stats, setStats] = useState<YasStats | null>(null);
  const [assets, setAssets] = useState<ProducerAssetsList["totals"] | null>(null);
  const [igOwn, setIgOwn] = useState<InstagramOwnSummary | null>(null);
  const [igState, setIgState] = useState<IgState>("loading");
  // Recent work comes from the shared library store so Dashboard <-> Library
  // navigation is served from cache. Subscribed (not read once) so a delete in
  // the Library prunes these tiles too.
  const libraryStore = useYasLibraryStore();
  const recent: YasLibraryItem[] = libraryStore.items;
  // Per-source loading. A SINGLE shared `loading` flag used to gate the whole
  // page on Promise.allSettled of all four fetches, so the slowest one decided
  // first paint. The Instagram summary fans out 40-60 sequential Graph calls
  // (11-17s), and until it settled the twelve tile <img> elements did not exist
  // in the DOM at all: the entire image pipeline was queued behind it.
  const [statsLoading, setStatsLoading] = useState(true);
  const [recentLoading, setRecentLoading] = useState(true);
  const toast = useToast();
  const user = useCurrentUser();
  // Which total's detail window is open, and the tile it grows out of.
  const [detail, setDetail] = useState<KpiKind | null>(null);
  const [detailFrom, setDetailFrom] = useState<HTMLElement | null>(null);
  const openDetail = (k: KpiKind) => (e: { currentTarget: HTMLElement }) => {
    setDetailFrom(e.currentTarget);
    setDetail(k);
  };

  // The grid canvas the k-panel sits on, only while this page is open.
  useKCanvas();

  useEffect(() => {
    let cancelled = false;

    // Local design preview only (never in a production build): open
    // http://127.0.0.1:4242/?demo#dashboard to see the page with sample numbers
    // when the local database is empty. Recent work still comes from the API.
    if (DEMO) {
      const s = sampleData();
      setStats(s.stats);
      setAssets(s.assets);
      setIgOwn(s.ig);
      setIgState("ready");
      setStatsLoading(false);
    }

    // Fired independently, each settling into its own state. No source can hold
    // another back, and one failure can't blank the dashboard.
    if (!DEMO) api.yasProducer
      .stats()
      .then((v) => { if (!cancelled) setStats(v); })
      .catch((e) => {
        if (cancelled) return;
        toast.error(
          "Couldn't load Creative Command Center stats",
          String((e as any)?.message || e),
        );
      })
      .finally(() => { if (!cancelled) setStatsLoading(false); });

    // Through the SHARED library store (cached per param set, 24h) rather than a
    // bare fetch, so coming back to the Dashboard is instant instead of paying
    // the round trip again. `limit: 12` gets its own cache entry and does not
    // contend with the Library's `limit: 300`.
    libraryStore
      .fetch({ limit: 12 })
      .catch(() => {})
      .finally(() => { if (!cancelled) setRecentLoading(false); });

    if (!DEMO) api.producerAssets
      .list({ limit: 1 })
      .then((v) => { if (!cancelled) setAssets(v.totals); })
      .catch(() => {});

    // Deliberately last and deliberately un-awaited by anything: this is the
    // 11-17s call. It fills the Instagram panel whenever it lands.
    if (!DEMO) api.yasProducer
      .instagramOwn()
      .then((v) => {
        if (cancelled) return;
        setIgOwn(v);
        setIgState("ready");
      })
      .catch(() => { if (!cancelled) setIgState("error"); });

    return () => {
      cancelled = true;
    };
  }, [toast]);

  // Only the KPI row genuinely needs stats to render meaningfully. Everything
  // else fills in place.
  if (statsLoading) return <SkeletonDashboard />;

  const g = stats?.generations;
  // "Videos" merges talking-head reels + animated posters into one tile.
  const videos = (g?.reels ?? 0) + (stats?.animations.total ?? 0);
  const images = g?.posters ?? 0;
  const projects = g?.editor_projects ?? 0;
  const assetCount = assets?.total ?? 0;
  const spend = stats?.spend.total_usd ?? 0;

  // Content funnel: made-but-not-scheduled -> scheduled -> posted / cancelled.
  //
  // These four keep the API's spelling while the page shows what they MEAN, so
  // the mapping is written down once, here:
  //
  //   pending    -> "Ready to schedule"  ready + saved + animations, minus
  //                                      everything ever scheduled
  //   in_process -> "Scheduled"          scheduled_instagram_posts pending/reminded
  //   declined   -> "Cancelled"          scheduled_instagram_posts cancelled
  //   approved   -> "Posted"             scheduled_instagram_posts published
  //
  // The wire format is untouched: `funnel.pending` is still `pending`. This
  // codebase renames labels and never keys, because a key is carried by stored
  // rows and a label is only read by people.
  const pending = stats?.funnel.pending ?? 0;
  const inProcess = stats?.funnel.in_process ?? 0;
  const declined = stats?.funnel.declined ?? 0;
  const approved = stats?.funnel.approved ?? 0;

  // Per-category breakdown: generation categories + an Animations row, each
  // with done / in-progress / failed splits. Sorted by volume.
  const anim = stats?.animations;
  const catRows: CatRow[] = [
    ...(stats?.generations.categories ?? []).map((c) => ({
      key: c.key,
      label: c.label,
      done: c.ready + c.saved,
      generating: c.generating,
      failed: c.failed,
      total: c.total,
    })),
    ...(anim && anim.total > 0
      ? [
          {
            key: "animations",
            label: "Animations",
            done: anim.ready,
            generating: anim.generating,
            failed: anim.failed,
            total: anim.total,
          },
        ]
      : []),
  ].sort((a, b) => b.total - a.total);

  const activity = stats?.activity_14d ?? [];

  const now = new Date();
  const firstName = (user.name || "").trim().split(/\s+/)[0] || "there";

  return (
    <div className="relative space-y-5 animate-pageEnter">
      {/* Header: who, what day. */}
      <section className="flex flex-wrap items-end justify-between gap-x-6 gap-y-4 pt-2 pb-1">
        <div className="min-w-0 space-y-1.5">
          <p className="flex items-center gap-2 text-[13px] font-medium text-k-text/55">
            {now.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" })}
            {DEMO && (
              <span className="bg-k-text/[0.06] px-2 py-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-k-text/70 ring-1 ring-k-text/15">
                Sample numbers
              </span>
            )}
          </p>
          {/* 42abudhabi.ae headline style: "> " lead-in and the key word in a
              cyan code bracket, <NATI/>. */}
          <h1 className="k-display text-[34px] md:text-[46px] text-k-text text-balance">
            <span className="k-code" aria-hidden>&gt; </span>
            {greeting(now)},{" "}
            <span className="k-code whitespace-nowrap">
              <span className="k-pixel" aria-hidden>&lt;</span>
              {firstName}
              <span className="k-cursor" aria-hidden>_</span>
              <span className="k-pixel" aria-hidden>/&gt;</span>
            </span>
          </h1>
        </div>
        <div className="flex items-center gap-3.5">
          <button className="k-btn-ghost" onClick={() => navigate("library")}>
            <LibraryIcon className="w-4 h-4" strokeWidth={1.8} aria-hidden />
            Library
          </button>
          <button className="k-btn" onClick={() => navigate("producer")}>
            <Plus className="w-4 h-4" strokeWidth={2} aria-hidden />
            New content
          </button>
        </div>
      </section>

      <Reveal delay={60}>
        <NextStep pending={pending} scheduled={inProcess} navigate={navigate} />
      </Reveal>

      <Reveal delay={130}>
        <PipelinePanel
          pending={pending}
          scheduled={inProcess}
          posted={approved}
          cancelled={declined}
          navigate={navigate}
        />
      </Reveal>

      {/* Totals. Five readable figures, each with what it counts. */}
      <section aria-label="Totals" style={{ animationDelay: "200ms" }} className="animate-reveal grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        <KpiTile Icon={Film} label="Videos" value={videos} hint="Presenter reels and animations" onOpen={openDetail("videos")} />
        <KpiTile Icon={ImageIcon} label="Images" value={images} hint="Posters in every style" onOpen={openDetail("images")} />
        <KpiTile Icon={Scissors} label="Edited videos" value={projects} hint="Projects in the Video Editor" onOpen={openDetail("edited")} />
        <KpiTile Icon={Layers} label="Assets" value={assetCount} hint="Photos, clips and fonts you can reuse" onOpen={openDetail("assets")} />
        <KpiTile Icon={Wallet} label="Spent on AI" value={spend} format={fmtMoney} hint="All images and videos so far" wide onOpen={openDetail("spend")} />
      </section>
      <KpiDetail
        kind={detail}
        origin={detailFrom}
        stats={stats}
        assets={assets}
        demo={DEMO}
        navigate={navigate}
        onClose={() => setDetail(null)}
        Icon={{ videos: Film, images: ImageIcon, edited: Scissors, assets: Layers, spend: Wallet }[detail ?? "videos"]}
      />

      {/* Analysis. Its charts draw in when they come on screen. */}
      <ChartGate>
      <section style={{ animationDelay: "270ms" }} className="animate-reveal grid lg:grid-cols-12 gap-4 items-stretch">
        <ActivityPanel data={activity} />
        <ProductionPanel rows={catRows} onStart={() => navigate("producer")} />
      </section>
      </ChartGate>

      <ChartGate>
      <Reveal delay={340}>
      <InstagramPanel
        state={igState}
        data={igOwn}
        onOpen={() => navigate("instagram")}
        onSchedule={() => navigate("library", "scheduled")}
      />
      </Reveal>
      </ChartGate>

      {/* Recent work */}
      <section className="space-y-3">
        <SectionHead
          title="Your latest work"
          sub="Tap any piece to open it"
          action={
            <button
              onClick={() => navigate("library")}
              className="inline-flex items-center gap-1 text-[13px] font-medium text-k-text/70 hover:text-k-text transition-colors rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-k-cyan/70"
            >
              See everything
              <ArrowRight className="w-3.5 h-3.5" strokeWidth={2} aria-hidden />
            </button>
          }
        />
        {recentLoading ? (
          // Placeholder tiles rather than "No work yet." The empty state must
          // not flash while the list is still in flight.
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} h="h-64" rounded="rounded-2xl" />
            ))}
          </div>
        ) : recent.length === 0 ? (
          <div className="k-panel px-6 py-10 flex flex-col items-center text-center gap-3">
            <span className="w-12 h-12 rounded-2xl k-inset flex items-center justify-center">
              <ImageIcon className="w-5 h-5 text-k-text/60" strokeWidth={1.8} aria-hidden />
            </span>
            <div className="space-y-1">
              <p className="text-[15px] font-semibold text-k-text">Nothing made yet</p>
              <p className="text-[13px] text-k-text/60">Posters, videos and edits you make will show up here.</p>
            </div>
            <button className="k-btn k-btn-sm" onClick={() => navigate("producer")}>
              <Plus className="w-3.5 h-3.5" strokeWidth={2} aria-hidden />
              Make your first piece
            </button>
          </div>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-3">
            {recent.filter((i) => i.type !== "talking_head").map((item, i) => (
              <RecentTile
                key={`${item.type}-${item.id}`}
                item={item}
                delay={i * 40}
                onOpen={() => navigate("library", `${item.type}:${item.id}`)}
              />
            ))}
          </div>
        )}
      </section>

      {/* Quick actions */}
      <section className="space-y-3">
        <SectionHead title="Make something new" sub="Pick what you want to create" />
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          <QuickCard title="Presenter video" sub="A person talks about your topic" Icon={Clapperboard} onClick={() => navigate("producer", "talking_head_v2")} />
          <QuickCard title="Edit a video" sub="Cut clips, add music and text" Icon={Scissors} onClick={() => navigate("producer", "video_editor")} />
          <QuickCard title="Poster" sub="An image post, with or without a person" Icon={ImageIcon} onClick={() => navigate("producer", "poster")} />
          <QuickCard title="Instagram" sub="See what is already posted" Icon={Instagram} onClick={() => navigate("instagram")} />
          <QuickCard title="Assets" sub="Upload photos, clips and fonts" Icon={Layers} onClick={() => navigate("assets")} />
          <QuickCard title="Brand" sub="Colours, voice and rules" Icon={Palette} onClick={() => navigate("brand")} />
        </div>
      </section>
    </div>
  );
}

/* ---------- motion helpers ---------- */

/** One section of the page, rising in after the ones above it. */
function Reveal({ delay, children }: { delay: number; children: ReactNode }) {
  return (
    <div className="animate-reveal" style={{ animationDelay: `${delay}ms` }}>
      {children}
    </div>
  );
}

/** A number that counts up to its value (and from old to new value when the
 *  data changes). Screen readers get the final value only; with reduced
 *  motion on, the final value shows at once. */
function CountUp({
  value,
  format = fmtNumber,
  duration = 900,
}: {
  value: number;
  format?: (n: number) => string;
  duration?: number;
}) {
  const [shown, setShown] = useState(0);
  // Where the number actually is right now, so a restart (new data, or React
  // re-running the effect in development) continues from there instead of
  // jumping to the end.
  const current = useRef(0);
  useEffect(() => {
    const reduce =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const start = current.current;
    if (reduce || start === value) {
      current.current = value;
      setShown(value);
      return;
    }
    let raf = 0;
    // Timed from the first frame, not performance.now(): a frame's timestamp
    // can be slightly EARLIER than "now", which gave a negative first step
    // ("-0", "$-0.07") for one frame.
    let t0 = -1;
    const tick = (t: number) => {
      if (t0 < 0) t0 = t;
      const k = Math.min(1, Math.max(0, (t - t0) / duration));
      const eased = 1 - Math.pow(1 - k, 3);
      const v = k === 1 ? value : start + (value - start) * eased;
      current.current = v;
      setShown(v);
      if (k < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, duration]);
  const display = Number.isInteger(value) ? Math.round(shown) : shown;
  return (
    <>
      <span aria-hidden>{format(display)}</span>
      <span className="sr-only">{format(value)}</span>
    </>
  );
}

/* ---------- next step ---------- */

/** One sentence, one button: the single thing worth doing right now. */
function NextStep({
  pending,
  scheduled,
  navigate,
}: {
  pending: number;
  scheduled: number;
  navigate: (t: Tab, param?: string) => void;
}) {
  const step =
    pending > 0
      ? {
          title: `${plural(pending, "piece is", "pieces are")} ready to go out`,
          body: "Give each one a date so it gets posted.",
          cta: "Schedule them",
          go: () => navigate("library"),
        }
      : scheduled > 0
        ? {
            title: "You're all caught up",
            body: `${plural(scheduled, "post is", "posts are")} scheduled. We'll email you when it's time to post.`,
            cta: "See the schedule",
            go: () => navigate("library", "scheduled"),
          }
        : {
            title: "Nothing is waiting on you",
            body: "Start a poster or a video, and it will show up here when it's ready.",
            cta: "Make something",
            go: () => navigate("producer"),
          };
  return (
    // The purple feature block from 42abudhabi.ae (news cards, footer): white
    // type, cyan accents and the cyan 42 button, in both themes.
    <section className="k-feature px-6 py-5 md:px-8 md:py-6 flex flex-col sm:flex-row sm:items-center gap-5" aria-label="Next step">
      <div className="min-w-0 flex-1 space-y-1.5">
        <p className="k-title text-[12px] tracking-[0.12em] text-k-accent before:content-none">
          <span className="k-pixel" aria-hidden>&lt; </span>Next step<span className="k-pixel" aria-hidden> /&gt;</span>
        </p>
        <p className="k-display text-[22px] md:text-[26px] text-white">{step.title}</p>
        <p className="text-[15px] text-white/80">{step.body}</p>
      </div>
      <button className="k-btn shrink-0 self-start sm:self-auto mr-1" onClick={step.go}>
        {step.cta}
        <ArrowRight className="w-4 h-4" strokeWidth={2} aria-hidden />
      </button>
    </section>
  );
}

/* ---------- pipeline ---------- */

function PipelinePanel({
  pending,
  scheduled,
  posted,
  cancelled,
  navigate,
}: {
  pending: number;
  scheduled: number;
  posted: number;
  cancelled: number;
  navigate: (t: Tab, param?: string) => void;
}) {
  const total = pending + scheduled + posted + cancelled;
  const pct = (n: number) => (total > 0 ? (n / total) * 100 : 0);

  return (
    <section aria-labelledby="pipeline-title" className="k-panel p-5 md:p-6 space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 id="pipeline-title" className="k-title">Where your posts are</h2>
          <p className="text-[13px] text-k-text/60">Every finished piece moves from left to right.</p>
        </div>
        <span className="text-[13px] text-k-text/55 tabular-nums">{plural(total, "piece", "pieces")} in total</span>
      </div>

      <ol className="grid grid-cols-1 md:grid-cols-[1fr_auto_1fr_auto_1fr] gap-3 md:gap-2 items-stretch">
        <PipelineStage
          step={1}
          ring="rgb(var(--k-chart))"
          total={total}
          label="Ready to schedule"
          value={pending}
          body="Finished, but no date picked yet."
          startHere={pending > 0}
          cta="Schedule"
          onClick={() => navigate("library")}
        />
        <PipelineArrow />
        <PipelineStage
          step={2}
          ring="rgb(var(--k-purple))"
          total={total}
          label="Scheduled"
          value={scheduled}
          body="Has a date. We email you, then you press Post."
          cta="See dates"
          onClick={() => navigate("library", "scheduled")}
        />
        <PipelineArrow />
        <PipelineStage
          step={3}
          ring="rgb(var(--k-text))"
          total={total}
          label="Posted"
          value={posted}
          body="Live on Instagram."
          cta="See posts"
          onClick={() => navigate("instagram")}
        />
      </ol>

      {/* Where everything sits right now, as one proportional bar. */}
      <div className="space-y-2">
        <div
          className="h-2.5 bg-k-text/[0.08] overflow-hidden flex gap-[3px]"
          role="img"
          aria-label={`${pending} ready to schedule, ${scheduled} scheduled, ${posted} posted, ${cancelled} cancelled`}
        >
          {pending > 0 && <div className="h-full bg-k-cyan transition-all duration-500" style={{ width: `${pct(pending)}%` }} />}
          {scheduled > 0 && <div className="h-full bg-k-purple transition-all duration-500" style={{ width: `${pct(scheduled)}%` }} />}
          {posted > 0 && <div className="h-full bg-k-text transition-all duration-500" style={{ width: `${pct(posted)}%` }} />}
          {cancelled > 0 && <div className="h-full bg-k-text/20 transition-all duration-500" style={{ width: `${pct(cancelled)}%` }} />}
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12.5px] text-k-text/60">
          <LegendDot dot="bg-k-cyan" label="Ready to schedule" value={pending} />
          <LegendDot dot="bg-k-purple" label="Scheduled" value={scheduled} />
          <LegendDot dot="bg-k-text" label="Posted" value={posted} />
          {cancelled > 0 && <LegendDot dot="bg-k-text/25" label="Cancelled before going out" value={cancelled} />}
        </div>
      </div>
    </section>
  );
}

function PipelineStage({
  step,
  ring,
  total,
  label,
  value,
  body,
  startHere = false,
  cta,
  onClick,
}: {
  /** Position in the flow. These ARE a sequence, so the number is information. */
  step: number;
  /** The same colour as this stage's slice of the bar below. */
  ring: string;
  /** Everything in the pipeline, so the ring can show this stage's share. */
  total: number;
  label: string;
  value: number;
  body: string;
  startHere?: boolean;
  cta: string;
  onClick: () => void;
}) {
  return (
    <li className="min-w-0">
      <button
        type="button"
        onClick={onClick}
        className={[
          "group relative h-full w-full text-left rounded-[2px] p-4 md:p-5 flex flex-col gap-3 transition-all duration-300",
          "focus:outline-none focus-visible:ring-2 focus-visible:ring-k-cyan/70",
          startHere
            ? "bg-k-surface border-2 border-k-btn hover:-translate-y-0.5"
            : "k-inset hover:border-k-btn hover:-translate-y-0.5",
        ].join(" ")}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-2.5 min-w-0">
            <span className="k-well w-7 h-7 !rounded-none text-[12px] font-semibold tabular-nums">{step}</span>
            <span className="k-title text-[13px] before:content-none leading-tight">{label}</span>
          </span>
          {startHere ? (
            <span className="shrink-0 inline-flex items-center px-2 py-1 bg-k-btn text-k-onbtn k-title text-[11px] tracking-[0.08em] before:content-none">
              Start here
            </span>
          ) : null}
        </div>
        <div className="flex items-center justify-between gap-4">
          <p className="k-num text-[46px] md:text-[56px] leading-none text-k-text">
            <CountUp value={value} />
          </p>
          <ShareRing value={value} total={total} color={ring} label={label} />
        </div>
        <p className="text-[14px] leading-snug text-k-text/60">{body}</p>
        <span
          className={[
            "mt-auto inline-flex items-center gap-1 text-[14px] font-semibold transition-colors",
            startHere ? "text-k-accent" : "text-k-text/65 group-hover:text-k-text",
          ].join(" ")}
        >
          {cta}
          <ArrowRight className="w-4 h-4 transition-transform duration-200 group-hover:translate-x-0.5" strokeWidth={2} aria-hidden />
        </span>
      </button>
    </li>
  );
}

function LegendDot({ dot, label, value }: { dot: string; label: string; value: number }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`w-2 h-2 rounded-full ${dot}`} aria-hidden />
      {label}
      <b className="font-semibold text-k-text tabular-nums">{fmtNumber(value)}</b>
    </span>
  );
}

function PipelineArrow() {
  return (
    <li aria-hidden className="hidden md:flex items-center justify-center w-8">
      <span className="k-well w-8 h-8 !rounded-none text-k-text/50">
        <ArrowRight className="w-3.5 h-3.5" strokeWidth={2} />
      </span>
    </li>
  );
}

/** A round gauge: this stage's share of everything in the pipeline, in the
 *  stage's own colour, with the percentage in the middle. It draws itself in
 *  from zero when the page opens (instant when reduced motion is on, via the
 *  global rule in styles.css). */
function ShareRing({
  value,
  total,
  color,
  label,
}: {
  value: number;
  total: number;
  color: string;
  label: string;
}) {
  const size = 84;
  const stroke = 8;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const share = total > 0 ? value / total : 0;
  const pct = Math.round(share * 100);
  // Start at 0 and move to the real share on the next frame, so it sweeps in.
  const [shown, setShown] = useState(0);
  useEffect(() => {
    const id = requestAnimationFrame(() => setShown(share));
    return () => cancelAnimationFrame(id);
  }, [share]);

  return (
    <span
      className="relative shrink-0 inline-flex items-center justify-center"
      style={{ width: size, height: size }}
      role="img"
      aria-label={`${label}: ${pct}% of all pieces`}
    >
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90" aria-hidden>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="rgb(var(--k-line) / 0.07)" strokeWidth={stroke} />
        {shown > 0 && (
          <circle
            cx={size / 2}
            cy={size / 2}
            r={r}
            fill="none"
            stroke={color}
            strokeWidth={stroke}
            strokeLinecap="round"
            strokeDasharray={c}
            strokeDashoffset={c * (1 - Math.min(shown, 0.999))}
            style={{ transition: "stroke-dashoffset 1100ms cubic-bezier(0.16, 1, 0.3, 1)" }}
          />
        )}
      </svg>
      <span className="absolute inset-0 flex flex-col items-center justify-center leading-none">
        <span className="text-[17px] font-semibold tracking-[-0.02em] tabular-nums text-k-text">{pct}%</span>
        <span className="mt-1 text-[10.5px] text-k-text/50">of all</span>
      </span>
    </span>
  );
}

/* ---------- KPI tiles ---------- */

function KpiTile({
  Icon,
  label,
  value,
  format = fmtNumber,
  hint,
  wide = false,
  onOpen,
}: {
  Icon: ComponentType<any>;
  label: string;
  value: number;
  /** How to print the number (money, plain count). */
  format?: (n: number) => string;
  hint: string;
  /** Spans two cells below lg (5 tiles in a 2- or 3-column grid) so the last
   *  row is always full. */
  wide?: boolean;
  /** Opens the detail window, growing out of this tile. */
  onOpen: (e: { currentTarget: HTMLElement }) => void;
}) {
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-haspopup="dialog"
      aria-label={`${label}: ${format(value)}. Show details`}
      className={[
        "group k-panel k-hover px-5 pt-4 pb-4 flex flex-col gap-4 text-left",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-k-btn",
        wide ? "col-span-2 sm:col-span-1 lg:col-span-1" : "",
      ].join(" ")}
    >
      <span className="flex items-center justify-between gap-2 w-full">
        <span className="text-[13.5px] font-medium text-k-text/65 leading-tight">{label}</span>
        <Icon className="w-4 h-4 text-k-text/35 shrink-0 transition-colors group-hover:text-k-accent" strokeWidth={1.6} aria-hidden />
      </span>
      <span className="space-y-1.5 block">
        <span className="k-num block text-[34px] leading-none text-k-accent">
          <CountUp value={value} format={format} />
        </span>
        <span className="block text-[12.5px] text-k-text/50 leading-snug">{hint}</span>
      </span>
      <span className="mt-auto inline-flex items-center gap-1 text-[11.5px] font-bold uppercase tracking-[0.08em] text-k-text/40 transition-colors group-hover:text-k-accent group-focus-visible:text-k-accent">
        Details
        <ArrowRight className="w-3.5 h-3.5 transition-transform duration-200 group-hover:translate-x-0.5" strokeWidth={2.2} aria-hidden />
      </span>
    </button>
  );
}

/* ---------- what you've made ---------- */

interface CatRow {
  key: string;
  label: string;
  done: number;
  generating: number;
  failed: number;
  total: number;
}

function ProductionPanel({ rows: allRows, onStart }: { rows: CatRow[]; onStart: () => void }) {
  // A category with nothing in it has no bar; it would only divide by zero.
  const rows = allRows.filter((r) => r.total > 0);
  const grandTotal = rows.reduce((s, r) => s + r.total, 0);
  const doneTotal = rows.reduce((s, r) => s + r.done, 0);
  const genTotal = rows.reduce((s, r) => s + r.generating, 0);
  const failTotal = rows.reduce((s, r) => s + r.failed, 0);
  const largest = Math.max(1, ...rows.map((r) => r.total));

  return (
    <div className="k-panel p-5 md:p-6 lg:col-span-4 min-w-0 flex flex-col gap-5">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-0.5">
          <h2 className="k-title">What you've made</h2>
          <p className="text-[13px] text-k-text/60">By tool, most used first</p>
        </div>
        <p className="k-num text-[30px] leading-none text-k-text">
          <CountUp value={grandTotal} />
        </p>
      </div>

      {rows.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center text-center gap-3 py-8">
          <p className="text-[15px] font-semibold text-k-text">Nothing made yet</p>
          <p className="text-[13px] text-k-text/60 max-w-xs">Pick a tool to make your first poster or video.</p>
          <button className="k-btn k-btn-sm" onClick={onStart}>
            <Plus className="w-3.5 h-3.5" strokeWidth={2} aria-hidden />
            New content
          </button>
        </div>
      ) : (
        <>
          <ul className="space-y-4">
            {rows.map((r, i) => (
              <CategoryRow key={r.key} row={r} largest={largest} index={i} />
            ))}
          </ul>

          {/* The whole story in one plain line. */}
          <div className="mt-auto pt-4 border-t border-k-text/[0.07] flex flex-wrap gap-x-4 gap-y-1.5 text-[13px]">
            <span className="inline-flex items-center gap-1.5 text-k-text/75">
              <CheckCircle2 className="w-4 h-4 text-k-accent" strokeWidth={2} aria-hidden />
              <b className="font-semibold text-k-text tabular-nums">{fmtNumber(doneTotal)}</b> ready
            </span>
            {genTotal > 0 && (
              <span className="inline-flex items-center gap-1.5 text-k-text/75">
                <span className="w-2 h-2 rounded-full bg-k-cyan" aria-hidden />
                <b className="font-semibold text-k-text tabular-nums">{fmtNumber(genTotal)}</b> still being made
              </span>
            )}
            {failTotal > 0 && (
              <span className="inline-flex items-center gap-1.5 text-k-text/75">
                <AlertTriangle className="w-4 h-4 text-k-danger" strokeWidth={2} aria-hidden />
                <b className="font-semibold text-k-text tabular-nums">{fmtNumber(failTotal)}</b> didn't work
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function CategoryRow({ row, largest, index = 0 }: { row: CatRow; largest: number; index?: number }) {
  const { key, label, done, generating, failed, total } = row;
  const hex = CATEGORY_HEX[key] ?? CATEGORY_FALLBACK_HEX;
  const of = (n: number) => (largest > 0 ? (n / largest) * 100 : 0);
  return (
    <li className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <span className="flex items-center gap-2 min-w-0">
          <span className="text-[14px] font-medium text-k-text truncate">{label}</span>
        </span>
        <span className="text-[15px] font-bold tabular-nums text-k-text">{fmtNumber(total)}</span>
      </div>
      {/* Length = volume against the busiest tool; amber and red = still
          running and failed. */}
      <div
        className="h-2 rounded-full bg-k-text/[0.06] overflow-hidden"
        role="img"
        aria-label={`${label}: ${done} ready${generating ? `, ${generating} still being made` : ""}${failed ? `, ${failed} did not work` : ""}`}
      >
        <div
          className="h-full flex rounded-full overflow-hidden k-grow-x"
          style={{ width: `${of(total)}%`, animationDelay: `${250 + index * 110}ms` }}
        >
          <div className="h-full" style={{ width: `${(done / total) * 100}%`, backgroundColor: hex }} />
          <div className="h-full bg-k-cyan" style={{ width: `${(generating / total) * 100}%` }} />
          <div className="h-full bg-k-danger" style={{ width: `${(failed / total) * 100}%` }} />
        </div>
      </div>
    </li>
  );
}

/* ---------- activity: the analysis chart ---------- */

function niceCeil(n: number): number {
  if (n <= 4) return Math.max(1, n);
  const steps = [5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 80, 100];
  for (const s of steps) if (n <= s) return s;
  const mag = 10 ** Math.floor(Math.log10(n));
  return Math.ceil(n / mag) * mag;
}

/** Monotone cubic path (Fritsch-Carlson): smooth like Apple's charts, but it
 *  never swings above a peak or below zero the way a plain spline does. */
function monotonePath(pts: [number, number][]): string {
  const n = pts.length;
  if (n === 0) return "";
  if (n === 1) return `M${pts[0][0]},${pts[0][1]}`;
  const dx: number[] = [];
  const m: number[] = [];
  for (let i = 0; i < n - 1; i++) {
    dx.push(pts[i + 1][0] - pts[i][0]);
    m.push((pts[i + 1][1] - pts[i][1]) / (dx[i] || 1));
  }
  const t: number[] = [m[0]];
  for (let i = 1; i < n - 1; i++) t.push(m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2);
  t.push(m[n - 2]);
  for (let i = 0; i < n - 1; i++) {
    if (m[i] === 0) {
      t[i] = 0;
      t[i + 1] = 0;
      continue;
    }
    const a = t[i] / m[i];
    const b = t[i + 1] / m[i];
    const s = a * a + b * b;
    if (s > 9) {
      const k = 3 / Math.sqrt(s);
      t[i] = k * a * m[i];
      t[i + 1] = k * b * m[i];
    }
  }
  let d = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
  for (let i = 0; i < n - 1; i++) {
    const h = dx[i] / 3;
    d += ` C${(pts[i][0] + h).toFixed(1)},${(pts[i][1] + t[i] * h).toFixed(1)} ${(pts[i + 1][0] - h).toFixed(1)},${(pts[i + 1][1] - t[i + 1] * h).toFixed(1)} ${pts[i + 1][0].toFixed(1)},${pts[i + 1][1].toFixed(1)}`;
  }
  return d;
}

type ChartMode = "daily" | "total";

function ActivityPanel({ data }: { data: { day: string; count: number }[] }) {
  const [mode, setMode] = useState<ChartMode>("daily");
  const total = data.reduce((s, d) => s + d.count, 0);
  const peak = data.reduce<{ day: string; count: number } | null>(
    (best, d) => (!best || d.count > best.count ? d : best),
    null,
  );
  // Plain comparison: the last seven days against the seven before them.
  const thisWeek = data.slice(-7).reduce((s, d) => s + d.count, 0);
  const lastWeek = data.slice(-14, -7).reduce((s, d) => s + d.count, 0);

  let trend: { Icon: ComponentType<any>; text: string; tone: string };
  if (lastWeek === 0 && thisWeek === 0) {
    trend = { Icon: Minus, text: "No change from last week", tone: "bg-k-surface/70 text-k-text/75 ring-k-text/10" };
  } else if (lastWeek === 0) {
    trend = { Icon: TrendingUp, text: "Up from nothing last week", tone: "bg-k-surface/70 text-k-text/75 ring-k-text/10" };
  } else {
    const change = Math.round(((thisWeek - lastWeek) / lastWeek) * 100);
    trend =
      change > 0
        ? { Icon: TrendingUp, text: `${change}% more than last week`, tone: "bg-k-surface/70 text-k-text/75 ring-k-text/10" }
        : change < 0
          ? { Icon: TrendingDown, text: `${Math.abs(change)}% less than last week`, tone: "bg-k-surface/70 text-k-text/75 ring-k-text/10" }
          : { Icon: Minus, text: "Same as last week", tone: "bg-k-surface/70 text-k-text/75 ring-k-text/10" };
  }

  return (
    <div className="k-panel p-5 md:p-6 lg:col-span-8 min-w-0 flex flex-col gap-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-0.5">
          <h2 className="k-title">How much you made</h2>
          <p className="text-[13px] text-k-text/55">New posters and videos, last 2 weeks</p>
        </div>
        <div className="k-seg" role="group" aria-label="Chart view">
          <button type="button" aria-pressed={mode === "daily"} onClick={() => setMode("daily")}>Per day</button>
          <button type="button" aria-pressed={mode === "total"} onClick={() => setMode("total")}>Running total</button>
        </div>
      </div>

      {/* The key numbers, in words people use. */}
      <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <div className="grid grid-cols-3 gap-4 w-full sm:w-auto sm:flex sm:items-end sm:gap-8">
          <InlineStat value={fmtNumber(total)} label="in 2 weeks" />
          <span className="hidden sm:block w-px h-9 bg-k-text/10 self-center" aria-hidden />
          <InlineStat value={fmtNumber(thisWeek)} label="this week" />
          <span className="hidden sm:block w-px h-9 bg-k-text/10 self-center" aria-hidden />
          <InlineStat
            value={peak && peak.count > 0 ? fmtNumber(peak.count) : "0"}
            label={
              peak && peak.count > 0
                ? `best day, ${parseDay(peak.day).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })}`
                : "best day"
            }
          />
        </div>
        <span className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[12.5px] font-semibold ring-1 ${trend.tone}`}>
          <trend.Icon className="w-3.5 h-3.5" strokeWidth={2.2} aria-hidden />
          {trend.text}
        </span>
      </div>

      {data.length === 0 ? (
        <p className="flex-1 flex items-center justify-center text-[14px] text-k-text/60 py-10 k-inset">
          Nothing made in the last 2 weeks yet.
        </p>
      ) : (
        <TrendChart data={data} mode={mode} />
      )}
    </div>
  );
}

function InlineStat({ value, label }: { value: string; label: string }) {
  return (
    <div className="min-w-0">
      <p className="k-num text-[30px] leading-none text-k-text">{value}</p>
      <p className="mt-1.5 text-[12.5px] text-k-text/55 sm:whitespace-nowrap">{label}</p>
    </div>
  );
}

function TrendChart({ data, mode }: { data: { day: string; count: number }[]; mode: ChartMode }) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(640);
  const [active, setActive] = useState<number | null>(null);
  const gid = useId().replace(/:/g, "");

  // Draw at the real pixel width so text and dots are never stretched.
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      setWidth(Math.max(260, Math.round(entry.contentRect.width)));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Per day, or everything so far on each day.
  let run = 0;
  const values = data.map((d) => (mode === "total" ? (run += d.count) : d.count));

  const H = 260;
  const padL = 34;
  const padR = 16;
  const padT = 22;
  const padB = 32;
  const n = data.length;
  const innerW = width - padL - padR;
  const innerH = H - padT - padB;
  const yMax = niceCeil(Math.max(0, ...values));
  const avg = n ? data.reduce((s, d) => s + d.count, 0) / n : 0;
  const x = (i: number) => padL + (n > 1 ? (i * innerW) / (n - 1) : innerW / 2);
  const y = (v: number) => padT + innerH * (1 - v / yMax);
  const pts = values.map((v, i) => [x(i), y(v)] as [number, number]);
  const line = monotonePath(pts);
  const base = (padT + innerH).toFixed(1);
  const area = `${line} L${x(n - 1).toFixed(1)},${base} L${x(0).toFixed(1)},${base} Z`;
  const ticks = Array.from(new Set([0, yMax / 4, yMax / 2, (yMax * 3) / 4, yMax])).filter((v) => Number.isInteger(v));
  const todayKey = new Date().toDateString();
  const markIdx =
    mode === "total" ? n - 1 : values.reduce((bi, v, i) => (v > values[bi] ? i : bi), 0);
  const showMark = values[markIdx] > 0;

  const indexAt = (clientX: number) => {
    const rect = wrapRef.current?.getBoundingClientRect();
    if (!rect || n === 0) return null;
    const px = clientX - rect.left;
    const i = n > 1 ? Math.round(((px - padL) / innerW) * (n - 1)) : 0;
    return Math.min(n - 1, Math.max(0, i));
  };
  const onMove = (e: PointerEvent<HTMLDivElement>) => setActive(indexAt(e.clientX));
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      const cur = active ?? n - 1;
      setActive(Math.min(n - 1, Math.max(0, cur + (e.key === "ArrowRight" ? 1 : -1))));
    } else if (e.key === "Escape") {
      setActive(null);
    }
  };

  const shown = active;
  const tipValue = shown != null ? values[shown] : 0;
  const tipLeft = shown != null ? Math.min(Math.max(x(shown), 80), width - 80) : 0;
  const tipAbove = shown != null && y(tipValue) - 18 >= 62;
  const fmtLong = (day: string) =>
    parseDay(day).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
  const describe = (i: number) =>
    mode === "total"
      ? `${plural(values[i], "piece", "pieces")} made so far`
      : `${plural(values[i], "piece", "pieces")} made`;
  const font = "Inter, system-ui, sans-serif";

  return (
    <div
      ref={wrapRef}
      className="relative w-full min-w-0 select-none touch-pan-y rounded-xl focus:outline-none focus-visible:ring-2 focus-visible:ring-k-cyan/70"
      tabIndex={0}
      role="img"
      aria-label={`${mode === "total" ? "Running total" : "Pieces made per day"} over the last 2 weeks. Use the left and right arrow keys to read each day.${shown != null ? ` ${fmtLong(data[shown].day)}: ${describe(shown)}.` : ""}`}
      onPointerMove={onMove}
      onPointerDown={onMove}
      onPointerLeave={() => setActive(null)}
      onKeyDown={onKey}
      onFocus={() => setActive((a) => a ?? n - 1)}
      onBlur={() => setActive(null)}
    >
      <svg width={width} height={H} viewBox={`0 0 ${width} ${H}`} className="block overflow-visible" aria-hidden>
        <defs>
          <linearGradient id={`area-${gid}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgb(var(--k-chart))" stopOpacity="0.09" />
            <stop offset="60%" stopColor="rgb(var(--k-chart))" stopOpacity="0.03" />
            <stop offset="100%" stopColor="rgb(var(--k-chart))" stopOpacity="0" />
          </linearGradient>
          <linearGradient id={`stroke-${gid}`} x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="rgb(var(--k-chart))" />
            <stop offset="55%" stopColor="rgb(var(--k-chart))" />
            <stop offset="100%" stopColor="rgb(var(--k-chart))" />
          </linearGradient>
          <filter id={`glow-${gid}`} x="-10%" y="-40%" width="120%" height="180%">
            <feGaussianBlur stdDeviation="6" />
          </filter>
        </defs>

        {/* Scale lines with their values. */}
        {ticks.map((v) => (
          <g key={v}>
            <line
              x1={padL} x2={width - padR} y1={y(v)} y2={y(v)}
              stroke={v === 0 ? "rgb(var(--k-line) / 0.14)" : "rgb(var(--k-line) / 0.06)"}
              strokeWidth={1}
              strokeDasharray={v === 0 ? undefined : "2 4"}
            />
            <text x={padL - 12} y={y(v) + 4} textAnchor="end" fontSize="11" fill="rgb(var(--k-line) / 0.45)" fontFamily={font}>
              {v}
            </text>
          </g>
        ))}

        <path key={`area-${mode}`} d={area} fill={`url(#area-${gid})`} className="animate-fadeIn" style={{ animationDuration: "900ms", animationDelay: "350ms", animationFillMode: "both" }} />
        {/* The line. */}
        <path key={`line-${mode}`} d={line} fill="none" stroke={`url(#stroke-${gid})`} strokeWidth={2.25} strokeLinecap="round" strokeLinejoin="round" pathLength={1} strokeDasharray={1} className="animate-drawLine" />

        {/* Average line: the everyday "normal" (per-day view only). */}
        {mode === "daily" && avg > 0 && (
          <g>
            <line x1={padL} x2={width - padR} y1={y(avg)} y2={y(avg)} stroke="rgb(var(--k-line) / 0.32)" strokeWidth={1} strokeDasharray="5 5" />
            <rect x={width - padR - 116} y={y(avg) - 22} width={116} height={18} rx={0} fill="rgb(var(--k-surface) / 0.9)" />
            <text x={width - padR - 58} y={y(avg) - 9} textAnchor="middle" fontSize="11" fontWeight={500} fill="rgb(var(--k-line) / 0.6)" fontFamily={font}>
              {`Average ${avg.toFixed(1)} a day`}
            </text>
          </g>
        )}

        {/* Today: a soft "live" ring, once the line has reached it. */}
        {shown == null && values[n - 1] > 0 && (
          <g key={`today-${mode}`} className="animate-fadeIn" style={{ animationDuration: "300ms", animationDelay: "1350ms", animationFillMode: "both" }}>
            <circle cx={x(n - 1)} cy={y(values[n - 1])} r={6} fill="rgb(var(--k-chart))" className="k-ping" />
            <circle cx={x(n - 1)} cy={y(values[n - 1])} r={3.5} fill="rgb(var(--k-chart))" />
          </g>
        )}

        {/* Best day (per day) or today's total (running total), always marked. */}
        {showMark && (
          // Fades in once, when the line has drawn up to it; hidden (not
          // unmounted) while hovering, so it doesn't replay the fade.
          <g
            key={`mark-${mode}`}
            className="animate-fadeIn"
            style={{ animationDuration: "400ms", animationDelay: "1250ms", animationFillMode: "both" }}
          >
          <g style={{ opacity: shown == null ? 1 : 0, transition: "opacity 150ms" }}>
            <circle cx={x(markIdx)} cy={y(values[markIdx])} r={11} fill="rgb(var(--k-chart))" opacity={0.18} />
            <circle cx={x(markIdx)} cy={y(values[markIdx])} r={5} fill="rgb(var(--k-surface))" stroke="rgb(var(--k-chart))" strokeWidth={2.5} />
            <text
              x={x(markIdx)}
              y={y(values[markIdx]) - 16}
              textAnchor={markIdx === n - 1 ? "end" : "middle"}
              fontSize="12"
              fontWeight={600}
              fill="rgb(var(--k-text))"
              fontFamily={font}
            >
              {mode === "total" ? `${values[markIdx]} so far` : `Best: ${values[markIdx]}`}
            </text>
          </g>
          </g>
        )}

        {/* Crosshair for the day under the finger or cursor. */}
        {shown != null && (
          <g>
            <line x1={x(shown)} x2={x(shown)} y1={padT - 6} y2={padT + innerH} stroke="rgb(var(--k-line) / 0.22)" strokeWidth={1} />
            <circle cx={x(shown)} cy={y(values[shown])} r={12} fill="rgb(var(--k-chart))" opacity={0.18} />
            <circle cx={x(shown)} cy={y(values[shown])} r={5.5} fill="rgb(var(--k-surface))" stroke="rgb(var(--k-chart))" strokeWidth={3} />
          </g>
        )}

        {/* Day labels: every other day, plus today. */}
        {data.map((d, i) => {
          const date = parseDay(d.day);
          const isToday = date.toDateString() === todayKey;
          const everyOther = (n - 1 - i) % 2 === 0;
          if (!isToday && !everyOther) return null;
          const label = isToday ? "Today" : date.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
          return (
            <text
              key={d.day}
              x={x(i)}
              y={H - 8}
              textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}
              fontSize="11"
              fontWeight={isToday || shown === i ? 600 : 400}
              fill={isToday || shown === i ? "rgb(var(--k-text))" : "rgb(var(--k-line) / 0.45)"}
              fontFamily={font}
            >
              {label}
            </text>
          );
        })}
      </svg>

      {/* Frosted tooltip: above the point when there is room, otherwise below,
          so it never sits on the dot it describes. */}
      {shown != null && (
        <div
          className={[
            "pointer-events-none absolute z-10 -translate-x-1/2 k-panel !rounded-2xl px-3.5 py-2.5 whitespace-nowrap",
            tipAbove ? "-translate-y-full" : "",
          ].join(" ")}
          style={{ left: tipLeft, top: tipAbove ? y(tipValue) - 18 : y(tipValue) + 18 }}
        >
          <p className="relative z-[2] text-[12px] text-k-text/55">{fmtLong(data[shown].day)}</p>
          <p className="relative z-[2] text-[15px] font-semibold text-k-text tabular-nums">{describe(shown)}</p>
        </div>
      )}
    </div>
  );
}

/* ---------- instagram ---------- */

function InstagramPanel({
  state,
  data,
  onOpen,
  onSchedule,
}: {
  state: IgState;
  data: InstagramOwnSummary | null;
  onOpen: () => void;
  onSchedule: () => void;
}) {
  const posts = data?.posts ?? [];
  return (
    <section className="k-panel overflow-hidden grid lg:grid-cols-12">
      <div className="lg:col-span-4 p-5 md:p-6 flex flex-col gap-5 border-b lg:border-b-0 lg:border-r border-k-text/[0.07]">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <span className="k-well w-10 h-10">
              <Instagram className="w-[19px] h-[19px]" strokeWidth={1.5} aria-hidden />
            </span>
            <div>
              <h2 className="k-title">Instagram</h2>
              <p className="text-[12.5px] text-k-text/55">What you posted from here</p>
            </div>
          </div>
          <button
            onClick={onOpen}
            className="w-9 h-9 shrink-0 rounded-full k-inset flex items-center justify-center text-k-text/60 hover:text-k-text transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-k-cyan/70"
            aria-label="Open Instagram"
            title="Open Instagram"
          >
            <ArrowUpRight className="w-4 h-4" strokeWidth={2} aria-hidden />
          </button>
        </div>

        {state === "loading" ? (
          <div className="space-y-3" aria-label="Loading Instagram numbers">
            <Skeleton h="h-9" w="w-24" />
            <Skeleton h="h-4" w="w-40" />
          </div>
        ) : (
          <div className="grid grid-cols-3 gap-2">
            <IgFigure label="Posts" value={state === "error" ? "—" : fmtNumber(data?.total_posts ?? 0)} />
            <IgFigure label="Likes" value={state === "error" ? "—" : fmtCompact(data?.total_likes)} Icon={Heart} />
            <IgFigure label="Comments" value={state === "error" ? "—" : fmtCompact(data?.total_comments)} Icon={MessageCircle} />
          </div>
        )}
        {data?.latest_post_at && (
          <p className="text-[12.5px] text-k-text/55">Last posted {fmtRel(data.latest_post_at)}</p>
        )}
      </div>

      <div className="lg:col-span-8 p-5 md:p-6 flex flex-col gap-3 min-h-[230px]">
        <div className="flex items-baseline justify-between gap-3">
          <h3 className="k-title text-[13px]">How people reacted</h3>
          <span className="text-[12.5px] text-k-text/55">Recent posts, oldest to newest</span>
        </div>
        {state === "loading" ? (
          <Skeleton h="h-40" rounded="rounded-xl" />
        ) : state === "error" ? (
          <div className="flex-1 flex flex-col items-center justify-center text-center gap-2 k-inset p-6">
            <AlertTriangle className="w-5 h-5 text-k-text/55" strokeWidth={1.8} aria-hidden />
            <p className="text-[15px] font-semibold text-k-text">Can't load Instagram right now</p>
            <p className="text-[13px] text-k-text/60 max-w-sm">Check the Instagram connection in Settings, then reload this page.</p>
          </div>
        ) : posts.length === 0 ? (
          <div className="flex-1 flex flex-col items-center justify-center text-center gap-3 k-inset p-6">
            <span className="k-well w-11 h-11">
              <CalendarClock className="w-5 h-5" strokeWidth={1.5} aria-hidden />
            </span>
            <div className="space-y-1">
              <p className="text-[15px] font-semibold text-k-text">Nothing posted yet</p>
              <p className="text-[13px] text-k-text/60 max-w-sm">
                Once you post something, you'll see its likes and comments here.
              </p>
            </div>
            <button className="k-btn-ghost h-9 px-3.5 text-[13px]" onClick={onSchedule}>
              See what's scheduled
              <ArrowRight className="w-3.5 h-3.5" strokeWidth={2} aria-hidden />
            </button>
          </div>
        ) : (
          <EngagementChart posts={posts} available={!!data?.metrics_available} />
        )}
      </div>
    </section>
  );
}

function IgFigure({ label, value, Icon }: { label: string; value: string; Icon?: ComponentType<any> }) {
  return (
    <div className="k-inset px-3 py-2.5 min-w-0">
      <p className="k-num text-[22px] leading-none text-k-text">{value}</p>
      <p className="mt-1.5 flex items-center gap-1 text-[12px] text-k-text/60">
        {Icon && <Icon className="w-3.5 h-3.5" strokeWidth={1.9} aria-hidden />}
        {label}
      </p>
    </div>
  );
}

/** Multi-line engagement chart: Views · Likes · Comments on one shared scale.
 *  A series only draws when it has real data; if NONE do, it falls back to a
 *  cumulative "posts published" growth curve so the panel never reads as broken. */
function EngagementChart({ posts, available }: { posts: NonNullable<InstagramOwnSummary["posts"]>; available: boolean }) {
  const list = posts.slice().reverse(); // oldest -> newest, L -> R
  const gid = useId().replace(/:/g, "");

  const SERIES = [
    { key: "views" as const, label: "Views", color: "rgb(var(--k-chart))" },
    { key: "likes" as const, label: "Likes", color: "rgb(var(--k-text))" },
    { key: "comments" as const, label: "Comments", color: "rgb(var(--k-text) / 0.45)" },
  ];
  // A metric draws only if it has at least one real (non-null) reading.
  const present = available
    ? SERIES.filter((s) => list.some((p) => p[s.key] != null))
    : [];

  const w = 600;
  const h = 160;
  const padX = 6;
  const padY = 12;
  const step = list.length > 1 ? (w - padX * 2) / (list.length - 1) : 0;
  const xAt = (i: number) => (list.length > 1 ? padX + i * step : w / 2);
  const yAt = (v: number, max: number) => padY + (h - padY * 2) * (1 - v / max);

  // Shared scale across all drawn metrics so they're directly comparable.
  const max = Math.max(1, ...present.flatMap((s) => list.map((p) => p[s.key] ?? 0)));
  const total = (key: "views" | "likes" | "comments") =>
    list.reduce((acc, p) => acc + (p[key] ?? 0), 0);

  const drawFallback = present.length === 0;
  const fbMax = list.length;
  const fbPts = list.map((_, i) => [xAt(i), yAt(i + 1, fbMax)] as [number, number]);

  return (
    <div className="flex-1 flex flex-col gap-3">
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-40" preserveAspectRatio="none" role="img" aria-label="Views, likes and comments across recent posts">
        <defs>
          {present.map((s) => (
            <linearGradient key={s.key} id={`eng-${gid}-${s.key}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={s.color} stopOpacity="0.08" />
              <stop offset="100%" stopColor={s.color} stopOpacity="0" />
            </linearGradient>
          ))}
        </defs>
        {[0.25, 0.5, 0.75].map((f) => (
          <line key={f} x1={0} x2={w} y1={padY + (h - padY * 2) * f} y2={padY + (h - padY * 2) * f} stroke="rgb(var(--k-line) / 0.07)" strokeWidth={1} vectorEffect="non-scaling-stroke" />
        ))}
        <line x1={0} x2={w} y1={h - padY} y2={h - padY} stroke="rgb(var(--k-line) / 0.16)" strokeWidth={1} vectorEffect="non-scaling-stroke" />

        {drawFallback ? (
          <path d={monotonePath(fbPts)} fill="none" stroke="rgb(var(--k-chart))" strokeWidth={2.5} strokeLinecap="round" vectorEffect="non-scaling-stroke" />
        ) : (
          present.map((s, si) => {
            const pts = list.map((p, i) => [xAt(i), yAt(p[s.key] ?? 0, max)] as [number, number]);
            const lineStr = monotonePath(pts);
            const areaStr = `${lineStr} L${pts[pts.length - 1][0].toFixed(1)},${(h - padY).toFixed(1)} L${pts[0][0].toFixed(1)},${(h - padY).toFixed(1)} Z`;
            return (
              <g key={s.key}>
                {si === 0 && (
                  <path
                    d={areaStr}
                    fill={`url(#eng-${gid}-${s.key})`}
                    className="animate-fadeIn"
                    style={{ animationDuration: "900ms", animationDelay: "700ms", animationFillMode: "both" }}
                  />
                )}
                <path
                  d={lineStr}
                  fill="none"
                  stroke={s.color}
                  strokeWidth={2.5}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  pathLength={1}
                  strokeDasharray={1}
                  className="animate-drawLine"
                  style={{ animationDelay: `${200 + si * 180}ms` }}
                />
              </g>
            );
          })
        )}
      </svg>

      {drawFallback ? (
        <div className="flex flex-wrap items-center justify-between gap-2 text-[12.5px] text-k-text/60">
          <span>Posts published, running total</span>
          <span>
            {list.length} recent · {available ? "no likes or comments yet" : "likes and comments unavailable"}
          </span>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          {present.map((s) => (
            <span key={s.key} className="inline-flex items-center gap-2 rounded-full k-inset px-3 py-1.5 text-[13px] text-k-text/75">
              <span className="w-2.5 h-2.5 rounded-full" style={{ background: s.color }} aria-hidden />
              {s.label}
              <b className="font-bold text-k-text tabular-nums">{fmtNumber(total(s.key))}</b>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---------- recent work + quick actions ---------- */

function SectionHead({ title, sub, action }: { title: string; sub?: string; action?: ReactNode }) {
  return (
    <div className="flex items-end justify-between gap-3">
      <div>
        <h2 className="k-title">{title}</h2>
        {sub && <p className="text-[13px] text-k-text/55">{sub}</p>}
      </div>
      {action}
    </div>
  );
}

function RecentTile({ item, onOpen, delay = 0 }: { item: YasLibraryItem; onOpen: () => void; delay?: number }) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [imgError, setImgError] = useState(false);
  // Retry through the backend proxy before showing the error state: same
  // reasoning as the Library tile, and the same outage blanked both (#37).
  const [triedProxy, setTriedProxy] = useState(false);
  const meta = TYPE_META[item.type] ?? { label: item.type };

  // Show whatever is ready, best-first: FINAL video -> non-final preview clip ->
  // still/keyframe image -> typed placeholder.
  const finalVideo = item.has_video && item.video_url ? item.video_url : null;
  const previewVideoUrl = finalVideo || item.preview_url || null;
  const showVideo = !!previewVideoUrl;
  const proxyThumb =
    item.download_url && item.download_url !== item.thumbnail_url ? item.download_url : null;
  const stillSrc = triedProxy ? proxyThumb : item.thumbnail_url;
  const showStill = !showVideo && !!stillSrc && !imgError;

  function play() {
    videoRef.current?.play().catch(() => {});
  }
  function stop() {
    const v = videoRef.current;
    if (v) {
      v.pause();
      v.currentTime = 0;
    }
  }

  return (
    <button
      onClick={onOpen}
      onMouseEnter={showVideo ? play : undefined}
      onMouseLeave={showVideo ? stop : undefined}
      onFocus={showVideo ? play : undefined}
      onBlur={showVideo ? stop : undefined}
      style={{ animationDelay: `${delay}ms` }}
      className="k-panel k-hover text-left p-1.5 overflow-hidden flex flex-col group animate-pageEnter focus:outline-none focus-visible:ring-2 focus-visible:ring-k-cyan/70"
      title={item.topic ?? undefined}
    >
      <div className="aspect-[3/4] bg-k-text/[0.04] relative overflow-hidden rounded-[2px]">
        {showVideo ? (
          <video
            ref={videoRef}
            src={previewVideoUrl!}
            // For a comp-ready video-editor row the backend aliases
            // thumbnail_url = video_url, so passing it as `poster` asks the image
            // loader to decode a whole MP4 (measured 29 MB). Only use it as a
            // poster when it is genuinely a different, still, resource.
            poster={
              item.thumbnail_url && item.thumbnail_url !== previewVideoUrl
                ? item.thumbnail_url
                : undefined
            }
            muted
            loop
            playsInline
            // "none", not "metadata": hover-play (below) fetches on demand, so
            // metadata for every off-screen tile is pure waste.
            preload="none"
            className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-[1.04]"
          />
        ) : showStill ? (
          <img
            src={stillSrc!}
            alt=""
            className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-[1.04]"
            loading="lazy"
            onError={() => {
              if (!triedProxy && proxyThumb) setTriedProxy(true);
              else setImgError(true);
            }}
          />
        ) : (
          // Distinguish "failed", "broken" and "legitimately nothing to show"
          // instead of collapsing all three into one grey "no preview" box.
          <div className="w-full h-full flex flex-col items-center justify-center gap-2 px-3 text-center">
            {item.status === "failed" ? (
              <>
                <AlertTriangle className="w-5 h-5 text-k-danger" strokeWidth={1.8} aria-hidden />
                <span className="text-[12px] font-medium text-k-danger">Didn't work</span>
              </>
            ) : imgError ? (
              <>
                <AlertTriangle className="w-5 h-5 text-k-text/40" strokeWidth={1.8} aria-hidden />
                <span className="text-[12px] text-k-text/60">Preview unavailable</span>
              </>
            ) : (
              <>
                <FileVideo className="w-5 h-5 text-k-text/40" strokeWidth={1.8} aria-hidden />
                <span className="text-[12px] text-k-text/60">
                  {item.type === "video_editor" ||
                  item.type === "general_video" ||
                  item.type === "broll"
                    ? "Empty timeline"
                    : "Nothing to preview"}
                </span>
              </>
            )}
          </div>
        )}
        {/* Frosted chip so the label reads on any image, light or dark. */}
        <span className="absolute top-2 left-2 max-w-[calc(100%-1rem)] inline-flex items-center gap-1.5 rounded-full bg-k-surface/90 backdrop-blur-md px-2 py-1 text-[11px] font-bold uppercase tracking-[0.06em] text-k-text">
          <span className="truncate">{meta.label}</span>
        </span>
        {!finalVideo && item.preview_kind === "scene" && (
          <span className="absolute top-2 right-2 rounded-full bg-black/70 px-2 py-1 text-[11px] font-semibold text-white shadow-sm">
            Scene
          </span>
        )}
      </div>
      <div className="px-2 pt-2.5 pb-1.5 flex-1 flex flex-col gap-1.5">
        <p className="text-[13px] font-medium leading-snug text-k-text line-clamp-2">{item.topic || "Untitled"}</p>
        <p className="mt-auto text-[12px] text-k-text/55">{fmtRel(item.created_at)}</p>
      </div>
    </button>
  );
}

function QuickCard({
  title,
  sub,
  Icon,
  onClick,
}: {
  title: string;
  sub: string;
  Icon: ComponentType<any>;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className="k-panel k-hover p-4 pr-5 text-left flex items-center gap-4 group focus:outline-none focus-visible:ring-2 focus-visible:ring-k-cyan/70"
    >
      <span className="k-well w-11 h-11 transition-colors duration-200 group-hover:text-k-text">
        <Icon className="w-[20px] h-[20px]" strokeWidth={1.5} aria-hidden />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-[15px] font-semibold tracking-[-0.01em] text-k-text leading-tight">{title}</span>
        <span className="block text-[13px] text-k-text/55 mt-1 leading-snug">{sub}</span>
      </span>
      <ArrowRight
        className="w-4 h-4 shrink-0 text-k-text/25 transition-all duration-200 group-hover:text-k-text group-hover:translate-x-0.5"
        strokeWidth={2}
        aria-hidden
      />
    </button>
  );
}

function SkeletonDashboard() {
  return (
    <div className="space-y-6" aria-busy="true" aria-label="Loading dashboard">
      <div className="space-y-2">
        <Skeleton h="h-4" w="w-40" />
        <Skeleton h="h-10" w="w-72" />
      </div>
      <Skeleton h="h-[92px]" rounded="rounded-2xl" />
      <Skeleton h="h-[300px]" rounded="rounded-2xl" />
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} h="h-[132px]" rounded="rounded-2xl" />
        ))}
      </div>
      <div className="grid lg:grid-cols-12 gap-4">
        <div className="lg:col-span-8"><Skeleton h="h-96" rounded="rounded-2xl" /></div>
        <div className="lg:col-span-4"><Skeleton h="h-96" rounded="rounded-2xl" /></div>
      </div>
    </div>
  );
}
