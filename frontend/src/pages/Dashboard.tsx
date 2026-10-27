import { useEffect, useRef, useState } from "react";
import type { ComponentType, ReactNode } from "react";
import {
  ArrowRight,
  CalendarClock,
  CheckCircle2,
  Clapperboard,
  Film,
  Heart,
  Image as ImageIcon,
  Inbox,
  Instagram,
  Layers,
  MessageCircle,
  Palette,
  Plus,
  Scissors,
  Wallet,
  XCircle,
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

interface Props {
  // Kept for App's call signature; the command-center dashboard only navigates.
  onStartRun?: (agent: AgentId, runId: number) => void;
  navigate: (t: Tab, param?: string) => void;
}

const TYPE_META: Record<YasLibraryType, { label: string; pill: string }> = {
  talking_head: { label: "Human Element Video", pill: "bg-violet-500/15 text-violet-600" },
  talking_head_v2: { label: "Human Element Video", pill: "bg-fuchsia-500/15 text-fuchsia-600" },
  poster: { label: "Poster", pill: "bg-blue-500/15 text-blue-600" },
  poster_human: { label: "Poster + Human", pill: "bg-success/15 text-emerald-600" },
  animation: { label: "Animation", pill: "bg-orange-500/15 text-orange-600" },
  video_editor: { label: "Video Editor", pill: "bg-cyan-500/15 text-cyan-600" },
  general_video: { label: "General AI Video", pill: "bg-teal-500/15 text-teal-600" },
  broll: { label: "B-roll", pill: "bg-warning/15 text-amber-600" },
};

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

export function DashboardPage({ navigate }: Props) {
  const [stats, setStats] = useState<YasStats | null>(null);
  const [assets, setAssets] = useState<ProducerAssetsList["totals"] | null>(null);
  const [igOwn, setIgOwn] = useState<InstagramOwnSummary | null>(null);
  // Recent work comes from the shared library store so Dashboard <-> Library
  // navigation is served from cache. Subscribed (not read once) so a delete in
  // the Library prunes these tiles too.
  const libraryStore = useYasLibraryStore();
  const recent: YasLibraryItem[] = libraryStore.items;
  // Per-source loading. A SINGLE shared `loading` flag used to gate the whole
  // page on Promise.allSettled of all four fetches — so the slowest one decided
  // first paint. The Instagram summary fans out 40-60 sequential Graph calls
  // (11-17s), and until it settled the twelve tile <img> elements did not exist
  // in the DOM at all: the entire image pipeline was queued behind it.
  const [statsLoading, setStatsLoading] = useState(true);
  const [recentLoading, setRecentLoading] = useState(true);
  const toast = useToast();

  useEffect(() => {
    let cancelled = false;

    // Fired independently, each settling into its own state. No source can hold
    // another back, and one failure can't blank the dashboard.
    api.yasProducer
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
    // the round trip again. `limit: 12` gets its own cache entry — it does not
    // contend with the Library's `limit: 300`.
    libraryStore
      .fetch({ limit: 12 })
      .catch(() => {})
      .finally(() => { if (!cancelled) setRecentLoading(false); });

    api.producerAssets
      .list({ limit: 1 })
      .then((v) => { if (!cancelled) setAssets(v.totals); })
      .catch(() => {});

    // Deliberately last and deliberately un-awaited by anything: this is the
    // 11-17s call. It fills the engagement panel whenever it lands.
    api.yasProducer
      .instagramOwn()
      .then((v) => { if (!cancelled) setIgOwn(v); })
      .catch(() => {});

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

  // Content funnel: made-but-not-scheduled → scheduled → posted / cancelled.
  //
  // These four keep the API's spelling while the card shows what they MEAN, so
  // the mapping is written down once, here, rather than inferred from a label
  // three hundred lines away:
  //
  //   pending    -> "Not scheduled"  ready + saved + animations, minus
  //                                  everything ever scheduled
  //   in_process -> "Scheduled"      scheduled_instagram_posts pending/reminded
  //   declined   -> "Cancelled"      scheduled_instagram_posts cancelled
  //   approved   -> "Posted"         scheduled_instagram_posts published
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

  const doneTotal = catRows.reduce((s, r) => s + r.done, 0);
  const genTotal = catRows.reduce((s, r) => s + r.generating, 0);
  const failTotal = catRows.reduce((s, r) => s + r.failed, 0);
  const grandTotal = catRows.reduce((s, r) => s + r.total, 0);

  const activity = stats?.activity_14d ?? [];
  const activityTotal = activity.reduce((acc, d) => acc + d.count, 0);

  return (
    <div className="space-y-8 animate-pageEnter">
      {/* Header */}
      <section className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="eyebrow mb-1.5">Dashboard</p>
          <h1 className="h1">Overview</h1>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn btn-primary" onClick={() => navigate("producer")}>
            <Plus className="w-4 h-4" strokeWidth={2} />
            New content
          </button>
        </div>
      </section>

      {/* Grouped KPI cards — status · content · projects · spend */}
      <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        {/*
          THE ONE QUESTION THIS CARD ANSWERS IS "IS IT SCHEDULED?", so every row
          says so in its own words. The labels used to be Pending / In process /
          Declined / Approved, and all four described something other than what
          they counted:

            "Pending"    was not waiting on anybody — it is finished work that
                         was never put in the schedule.
            "In process" was not being worked on — it is booked and waiting to
                         go out, which is the opposite of in progress.
            "Approved"   was not awaiting posting — it is already live on
                         Instagram, which is the single most expensive word on
                         this page to get wrong.

          The wire format is untouched: `funnel.pending` is still `pending`.
          This codebase renames labels and never keys — see the note at the top
          of docs/GENERAL-TRENDS.md — because a key is carried by stored rows
          and a label is only read by people.
        */}
        <GroupCard label="Status" delay={0}>
          <StatRow
            Icon={Inbox}
            label="Not scheduled"
            value={pending}
            hint="Finished and ready, but never added to the schedule. These are the ones waiting on you."
          />
          <StatRow
            Icon={CalendarClock}
            label="Scheduled"
            value={inProcess}
            hint="Booked to go out. Nothing more to do — the poller publishes these at their time."
          />
          <StatRow
            Icon={XCircle}
            label="Cancelled"
            value={declined}
            hint="Was scheduled, then called off before it went out."
          />
          <StatRow
            Icon={CheckCircle2}
            label="Posted"
            value={approved}
            hint="Already published to Instagram."
          />
        </GroupCard>
        <GroupCard label="Content" delay={60}>
          <StatRow Icon={Film} label="Videos" value={videos} />
          <StatRow Icon={ImageIcon} label="Images" value={images} />
        </GroupCard>
        <GroupCard label="Projects" delay={120}>
          <StatRow Icon={Scissors} label="Editor projects" value={projects} />
          <StatRow Icon={Layers} label="Assets" value={assetCount} />
        </GroupCard>
        <GroupCard label="Spend" delay={180}>
          <div className="flex-1 flex flex-col justify-center">
            <p
              key={spend}
              className="font-mono text-2xl font-bold tabular-nums leading-none animate-countUp text-accent"
            >
              {fmtMoney(spend)}
            </p>
            <p className="font-mono text-[10.5px] text-muted mt-1.5 flex items-center gap-1.5">
              <Wallet className="w-3 h-3 text-fg/25" strokeWidth={1.5} />
              total spent
            </p>
          </div>
        </GroupCard>
      </section>

      {/* Analytics — two columns */}
      <section className="grid lg:grid-cols-2 gap-3 items-start">
        {/* Breakdown by category + status + activity */}
        <div className="card p-5 space-y-5">
          <div className="flex items-baseline justify-between">
            <p className="eyebrow">Breakdown by category</p>
            <span className="font-mono text-2xs text-muted">
              {fmtNumber(grandTotal)} total
            </span>
          </div>

          {catRows.length === 0 ? (
            <p className="text-sm text-muted">No work yet — start your first reel.</p>
          ) : (
            <div className="space-y-3">
              {catRows.map((r) => (
                <CategoryRow key={r.key} row={r} />
              ))}
            </div>
          )}

          {/* Status summary pills (across everything) */}
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <span className="pill pill-completed">{fmtNumber(doneTotal)} done</span>
            <span className="pill pill-running">{fmtNumber(genTotal)} in progress</span>
            <span className="pill pill-failed">{fmtNumber(failTotal)} failed</span>
          </div>

          {/* 14-day activity sparkline */}
          <div className="pt-2 border-t border-border">
            <div className="flex items-baseline justify-between mb-2">
              <p className="eyebrow text-3xs">Activity · 14 days</p>
              <span className="font-mono text-2xs text-fg/80">
                {fmtNumber(activityTotal)} created
              </span>
            </div>
            <Sparkline data={activity.map((d) => d.count)} />
          </div>
        </div>

        {/* Instagram — OUR posted content (not the 42AD ingest) */}
        <div className="space-y-3">
          <button
            onClick={() => navigate("instagram")}
            className="card card-hover p-5 w-full text-left space-y-4"
          >
            <div className="flex items-baseline justify-between">
              <p className="eyebrow">Instagram</p>
              <Instagram className="w-4 h-4 text-fg/20" strokeWidth={1.5} />
            </div>
            <div className="flex items-end gap-6">
              <div>
                <p className="font-mono text-2xl font-bold tabular-nums text-fg leading-none">
                  {fmtNumber(igOwn?.total_posts ?? 0)}
                </p>
                <p className="font-mono text-2xs text-muted mt-1.5">posts published</p>
              </div>
              <div className="flex items-center gap-4 pb-0.5">
                <Metric Icon={Heart} value={igOwn?.total_likes} />
                <Metric Icon={MessageCircle} value={igOwn?.total_comments} />
              </div>
            </div>
            {igOwn?.latest_post_at && (
              <p className="font-mono text-2xs text-muted">
                Last posted · {fmtRel(igOwn.latest_post_at)}
              </p>
            )}
          </button>

          {/* Engagement — per-post likes/comments (falls back to posts-over-time
              when IG metrics aren't available). */}
          <div className="card p-5 space-y-3">
            <div className="flex items-baseline justify-between">
              <p className="eyebrow">Engagement</p>
              <span className="font-mono text-2xs text-muted">recent posts</span>
            </div>
            <EngagementChart posts={igOwn?.posts ?? []} available={!!igOwn?.metrics_available} />
          </div>
        </div>
      </section>

      {/* Recent work */}
      <section>
        <div className="flex items-baseline justify-between mb-3">
          <p className="eyebrow">Recent work</p>
          <button
            onClick={() => navigate("library")}
            className="font-mono text-2xs text-fg/80 hover:text-fg transition-colors"
          >
            View all →
          </button>
        </div>
        {recentLoading ? (
          // Placeholder tiles rather than "No work yet." — the empty state must
          // not flash while the list is still in flight.
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} h="h-44" rounded="rounded-xl" />
            ))}
          </div>
        ) : recent.length === 0 ? (
          <p className="text-sm text-muted">No work yet.</p>
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
      <section>
        <p className="eyebrow mb-3">Quick actions</p>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
          <QuickCard title="Human Element Video" sub="AI presenter reel" Icon={Clapperboard} onClick={() => navigate("producer", "talking_head_v2")} />
          <QuickCard title="Video Editor" sub="Multi-track NLE" Icon={Scissors} onClick={() => navigate("producer", "video_editor")} />
          <QuickCard title="Posters" sub="Poster + human" Icon={ImageIcon} onClick={() => navigate("producer", "poster")} />
          <QuickCard title="Instagram" sub="Your posts" Icon={Instagram} onClick={() => navigate("instagram")} />
          <QuickCard title="Assets" sub="Library + uploads" Icon={Layers} onClick={() => navigate("assets")} />
          <QuickCard title="Brand" sub="Profile + palette" Icon={Palette} onClick={() => navigate("brand")} />
        </div>
      </section>
    </div>
  );
}

/* ---------- small pieces ---------- */

function GroupCard({
  label,
  delay = 0,
  children,
}: {
  label: string;
  delay?: number;
  children: ReactNode;
}) {
  return (
    <div
      style={{ animationDelay: `${delay}ms` }}
      className="card card-hover p-3.5 flex flex-col gap-2.5 animate-pageEnter"
    >
      <p className="eyebrow text-[9.5px]">{label}</p>
      <div className="flex-1 flex flex-col gap-1.5">{children}</div>
    </div>
  );
}

function StatRow({
  Icon,
  label,
  value,
  hint,
}: {
  Icon: ComponentType<any>;
  label: string;
  value: number;
  /** What this number literally counts. A KPI row has space for two or three
   *  words, and two or three words cannot say "finished but never added to the
   *  schedule" — so the label carries the distinction and this carries the
   *  definition. */
  hint?: string;
}) {
  return (
    <div
      title={hint}
      className="flex items-center justify-between gap-2 border-b border-border/60 last:border-0 pb-1.5 last:pb-0"
    >
      <span className="flex items-center gap-1.5 text-[11.5px] text-fg/75 min-w-0">
        <Icon className="w-3 h-3 text-fg/25 shrink-0" strokeWidth={1.5} />
        <span className="truncate">{label}</span>
      </span>
      <span
        key={value}
        className="font-mono text-[13px] font-bold tabular-nums leading-none text-fg shrink-0 animate-countUp"
      >
        {fmtNumber(value)}
      </span>
    </div>
  );
}

/** Multi-line engagement chart for the Instagram card: Views · Likes · Comments
 *  on one shared scale. Hand-rolled SVG (no charting lib), Sparkline house style
 *  with soft area under the top series + glow end-dots. A series only draws when
 *  it has real data; if NONE do, it falls back to a cumulative "posts published"
 *  growth curve so the card never reads as broken/empty. */
function EngagementChart({ posts, available }: { posts: InstagramOwnSummary["posts"]; available: boolean }) {
  const list = (posts ?? []).slice().reverse(); // oldest → newest, L→R
  if (list.length === 0) {
    return <div className="h-24 flex items-center text-2xs text-muted">No published posts yet.</div>;
  }

  const SERIES = [
    { key: "views" as const, label: "Views", color: "#06B6A2" },
    { key: "likes" as const, label: "Likes", color: "#F43F5E" },
    { key: "comments" as const, label: "Comments", color: "#6366F1" },
  ];
  // A metric draws only if it has at least one real (non-null) reading.
  const present = available
    ? SERIES.filter((s) => list.some((p) => p[s.key] != null))
    : [];

  const w = 280;
  const h = 96;
  const padX = 4;
  const padY = 10;
  const step = list.length > 1 ? (w - padX * 2) / (list.length - 1) : 0;
  const xAt = (i: number) => padX + i * step;
  const yAt = (v: number, max: number) => padY + (h - padY * 2) * (1 - v / max);

  // Shared scale across all drawn metrics so they're directly comparable.
  const max = Math.max(1, ...present.flatMap((s) => list.map((p) => p[s.key] ?? 0)));
  const total = (key: "views" | "likes" | "comments") =>
    list.reduce((acc, p) => acc + (p[key] ?? 0), 0);

  // Fallback: cumulative post count (a clean upward growth curve).
  const drawFallback = present.length === 0;
  const fbMax = list.length;
  const fbPts = list.map((_, i) => [xAt(i), yAt(i + 1, fbMax)] as const);

  return (
    <div className="space-y-2.5">
      <div className="relative">
        <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-24" preserveAspectRatio="none">
          <defs>
            {present.map((s) => (
              <linearGradient key={s.key} id={`engGrad-${s.key}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={s.color} stopOpacity="0.22" />
                <stop offset="100%" stopColor={s.color} stopOpacity="0" />
              </linearGradient>
            ))}
          </defs>

          {drawFallback ? (
            <>
              <polyline
                points={fbPts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ")}
                fill="none" stroke="#06B6A2" strokeWidth={2} strokeLinejoin="round"
                strokeLinecap="round" vectorEffect="non-scaling-stroke"
              />
              <circle cx={fbPts[fbPts.length - 1][0]} cy={fbPts[fbPts.length - 1][1]} r={3} fill="#06B6A2" />
            </>
          ) : (
            present.map((s, si) => {
              const pts = list.map((p, i) => [xAt(i), yAt(p[s.key] ?? 0, max)] as const);
              const lineStr = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
              const [lx, ly] = pts[pts.length - 1];
              // Soft fill only under the first (largest by convention = views) line.
              const areaStr =
                `M${pts[0][0].toFixed(1)},${(h - padY).toFixed(1)} ` +
                pts.map(([x, y]) => `L${x.toFixed(1)},${y.toFixed(1)}`).join(" ") +
                ` L${pts[pts.length - 1][0].toFixed(1)},${(h - padY).toFixed(1)} Z`;
              return (
                <g key={s.key} className="animate-countUp" style={{ animationDelay: `${si * 70}ms` }}>
                  {si === 0 && <path d={areaStr} fill={`url(#engGrad-${s.key})`} />}
                  <polyline
                    points={lineStr} fill="none" stroke={s.color} strokeWidth={2}
                    strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke"
                  />
                  <circle cx={lx} cy={ly} r={2.6} fill={s.color} />
                </g>
              );
            })
          )}
        </svg>
      </div>

      {/* Legend / totals */}
      {drawFallback ? (
        <div className="flex items-center justify-between font-mono text-3xs text-muted">
          <span>posts published · cumulative</span>
          <span>{list.length} recent{available ? " · no engagement data yet" : " · metrics unavailable"}</span>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1 font-mono text-3xs">
          {present.map((s) => (
            <span key={s.key} className="flex items-center gap-1.5 text-fg/75">
              <span className="w-2.5 h-[3px] rounded-full inline-block" style={{ background: s.color }} />
              {s.label}
              <b className="tabular-nums">{fmtNumber(total(s.key))}</b>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function Metric({ Icon, value }: { Icon: ComponentType<any>; value: number | null | undefined }) {
  return (
    <span className="flex items-center gap-1 font-mono text-2xs text-fg/70">
      <Icon className="w-3.5 h-3.5 text-muted" strokeWidth={1.5} />
      {fmtCompact(value)}
    </span>
  );
}

interface CatRow {
  key: string;
  label: string;
  done: number;
  generating: number;
  failed: number;
  total: number;
}

function CategoryRow({ row }: { row: CatRow }) {
  const { label, done, generating, failed, total } = row;
  const pct = (n: number) => (total > 0 ? (n / total) * 100 : 0);
  const parts: string[] = [];
  if (done) parts.push(`${fmtNumber(done)} done`);
  if (generating) parts.push(`${fmtNumber(generating)} in progress`);
  if (failed) parts.push(`${fmtNumber(failed)} failed`);
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-3 text-xs">
        <span className="text-fg/85 truncate">{label}</span>
        <span className="font-mono tabular-nums text-fg shrink-0">{fmtNumber(total)}</span>
      </div>
      {/* Stacked status bar: done (teal) · in-progress (amber) · failed (red) */}
      <div className="h-1.5 rounded-full bg-border overflow-hidden flex">
        <div className="h-full bg-accent" style={{ width: `${pct(done)}%` }} />
        <div className="h-full bg-warning" style={{ width: `${pct(generating)}%` }} />
        <div className="h-full bg-danger" style={{ width: `${pct(failed)}%` }} />
      </div>
      {parts.length > 0 && (
        <p className="font-mono text-3xs text-muted">{parts.join(" · ")}</p>
      )}
    </div>
  );
}

function Sparkline({ data }: { data: number[] }) {
  const w = 240;
  const h = 40;
  const pad = 3;
  if (data.length === 0) {
    return <div className="h-10 flex items-center text-2xs text-muted">No activity.</div>;
  }
  const max = Math.max(...data, 1);
  const step = data.length > 1 ? (w - pad * 2) / (data.length - 1) : 0;
  const pts = data.map((v, i) => {
    const x = pad + i * step;
    const y = pad + (h - pad * 2) * (1 - v / max);
    return [x, y] as const;
  });
  const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area =
    `M${pts[0][0].toFixed(1)},${(h - pad).toFixed(1)} ` +
    pts.map(([x, y]) => `L${x.toFixed(1)},${y.toFixed(1)}`).join(" ") +
    ` L${pts[pts.length - 1][0].toFixed(1)},${(h - pad).toFixed(1)} Z`;
  const [lastX, lastY] = pts[pts.length - 1];
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-10" preserveAspectRatio="none">
      <path d={area} fill="rgba(6,182,162,0.10)" />
      <polyline
        points={line}
        fill="none"
        stroke="#06B6A2"
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
      <circle cx={lastX} cy={lastY} r={2.5} fill="#06B6A2" />
    </svg>
  );
}

function RecentTile({ item, onOpen, delay = 0 }: { item: YasLibraryItem; onOpen: () => void; delay?: number }) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [imgError, setImgError] = useState(false);
  // Retry through the backend proxy before showing the error state — same
  // reasoning as the Library tile, and the same outage blanked both (#37).
  const [triedProxy, setTriedProxy] = useState(false);
  const meta = TYPE_META[item.type];

  // Show whatever is ready, best-first: FINAL video → non-final preview clip →
  // still/keyframe image → typed placeholder.
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
      style={{ animationDelay: `${delay}ms` }}
      className="card card-hover text-left p-0 overflow-hidden flex flex-col group animate-pageEnter"
      title={item.topic ?? undefined}
    >
      <div className="aspect-[3/4] bg-bg-muted relative overflow-hidden">
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
            className="w-full h-full object-cover"
          />
        ) : showStill ? (
          <img
            src={stillSrc!}
            alt=""
            className="w-full h-full object-cover"
            loading="lazy"
            onError={() => {
              if (!triedProxy && proxyThumb) setTriedProxy(true);
              else setImgError(true);
            }}
          />
        ) : (
          // Distinguish "failed", "broken" and "legitimately nothing to show"
          // instead of collapsing all three into one grey "no preview" box.
          <div className="w-full h-full flex flex-col items-center justify-center gap-1 px-3 text-center">
            {item.status === "failed" ? (
              <>
                <span className="text-base opacity-50">⚠</span>
                <span className="text-[10.5px] text-danger">failed</span>
              </>
            ) : imgError ? (
              <>
                <span className="text-base opacity-50">⚠</span>
                <span className="text-[10.5px] text-muted">Preview unavailable</span>
              </>
            ) : (
              <>
                <span className="text-base opacity-40">▤</span>
                <span className="text-[10.5px] text-muted">
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
        <span className={`absolute top-2 left-2 font-mono text-[9.5px] px-1.5 py-0.5 rounded ${meta.pill}`}>
          {meta.label}
        </span>
        {!finalVideo && item.preview_kind === "scene" && (
          <span className="absolute top-2 right-2 text-[9px] font-medium px-1.5 py-0.5 rounded bg-warning/85 text-white">
            scene
          </span>
        )}
      </div>
      <div className="p-2.5 space-y-1 flex-1 flex flex-col">
        <p className="text-xs text-fg/85 line-clamp-2">{item.topic || "(untitled)"}</p>
        <p className="mt-auto text-[10.5px] font-mono text-muted">{fmtRel(item.created_at)}</p>
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
      className="card card-hover p-4 text-left relative overflow-hidden group"
    >
      <Icon
        className="absolute -right-2 -bottom-2 w-16 h-16 text-fg/5 group-hover:text-accent/15 transition-colors duration-200"
        strokeWidth={1.5}
      />
      <div className="relative flex items-center justify-between mb-1.5">
        <h2 className="text-sm font-semibold text-fg">{title}</h2>
        <ArrowRight className="w-3.5 h-3.5 text-accent opacity-0 group-hover:opacity-100 transition-opacity" strokeWidth={2} />
      </div>
      <p className="relative text-2xs text-muted leading-snug">{sub}</p>
    </button>
  );
}

function SkeletonDashboard() {
  return (
    <div className="space-y-8">
      <div className="space-y-2">
        <Skeleton h="h-3" w="w-24" />
        <Skeleton h="h-8" w="w-64" />
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} h="h-[148px]" rounded="rounded-xl" />
        ))}
      </div>
      <div className="grid lg:grid-cols-2 gap-3">
        {Array.from({ length: 2 }).map((_, i) => (
          <Skeleton key={i} h="h-64" rounded="rounded-xl" />
        ))}
      </div>
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} h="h-44" rounded="rounded-xl" />
        ))}
      </div>
    </div>
  );
}
