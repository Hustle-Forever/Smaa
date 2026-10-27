/**
 * Producer (beta) — autonomous content campaigns.
 *
 * A campaign is a standing brief ("Cyber Security Week — 3 posters + 2 videos a
 * week"). A background worker invents each week's ideas, generates the media and
 * writes the captions on its own. Two things it can never do alone:
 *
 *   GATE 1  every generated item stops at "Needs review" until a human presses
 *           Approve or Decline here.
 *   GATE 2  approving only BOOKS the post. The final Post button lives in
 *           Library → Scheduled (the app's single publish path), so nothing
 *           reaches Instagram without a second, separate human click.
 *
 * This page renders in Layout's normal children (not the persistent slot): every
 * bit of campaign state lives server-side, so a remount just re-reads the store.
 *
 * Two views, switched by the hash: #campaigns is the hub, #campaigns/<id> is one
 * campaign's detail.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle, ArrowLeft, CheckCircle2, Clock, Loader2, Pause, Play,
  Plus, Radio, Sparkles, Trash2, XCircle,
} from "lucide-react";

import type { Tab } from "../App";
import { Skeleton } from "../components/Skeleton";
import { useConfirm } from "../components/ConfirmProvider";
import { useToast } from "../components/ToastProvider";
import { CampaignWizard } from "../components/campaigns/CampaignWizard";
import { ReviewCard } from "../components/campaigns/ReviewCard";
import { api } from "../lib/api";
import { useCampaignsStore } from "../lib/store";
import type { Campaign, CampaignItem, CampaignItemStatus } from "../lib/types";

// How often to re-read while the worker has something in flight. Slower than the
// creators' 5s polls: campaign work takes minutes, not seconds.
const POLL_MS = 8000;

/** Statuses that mean "a machine is still working" — worth polling for. */
const BUSY: CampaignItemStatus[] = ["planned", "generating", "approved"];

const STATUS_META: Record<CampaignItemStatus, { label: string; cls: string }> = {
  planned:          { label: "Queued",       cls: "bg-bg-muted text-muted" },
  generating:       { label: "Generating",   cls: "bg-sky-500/15 text-sky-600" },
  ready_for_review: { label: "Needs review", cls: "bg-accent/15 text-accent" },
  approved:         { label: "Booking",      cls: "bg-sky-500/15 text-sky-600" },
  scheduled:        { label: "Scheduled",    cls: "bg-warning/15 text-amber-600" },
  published:        { label: "Posted",       cls: "bg-success/15 text-emerald-600" },
  rejected:         { label: "Declined",     cls: "bg-bg-muted text-muted" },
  failed:           { label: "Failed",       cls: "bg-rose-500/15 text-rose-600" },
  publish_failed:   { label: "Post failed",  cls: "bg-rose-500/15 text-rose-600" },
  skipped:          { label: "Not posted",   cls: "bg-warning/15 text-amber-600" },
  expired:          { label: "Expired",      cls: "bg-bg-muted text-muted" },
  cancelled:        { label: "Cancelled",    cls: "bg-bg-muted text-muted" },
};

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** "in 3 hrs" / "due now" / "2 days ago" — same shape as the Scheduled queue. */
function countdown(iso: string | null): { text: string; due: boolean } | null {
  if (!iso) return null;
  const diff = new Date(iso).getTime() - Date.now();
  if (Number.isNaN(diff)) return null;
  const abs = Math.abs(diff);
  const mins = Math.round(abs / 60_000);
  const hours = Math.round(abs / 3_600_000);
  const days = Math.round(abs / 86_400_000);
  const unit =
    mins < 60 ? `${mins} min`
      : hours < 48 ? `${hours} hr${hours === 1 ? "" : "s"}`
        : `${days} day${days === 1 ? "" : "s"}`;
  if (diff <= 0) return { text: mins < 60 ? "due now" : `${unit} ago`, due: true };
  return { text: `in ${unit}`, due: false };
}

function money(n: number | null | undefined): string {
  return `$${(Number(n) || 0).toFixed(2)}`;
}

interface Props {
  /** Second hash segment: a campaign id opens its detail view. */
  campaignId: string | null;
  navigate: (tab: Tab, param?: string) => void;
}

export function CampaignsPage({ campaignId, navigate }: Props) {
  const id = campaignId && /^\d+$/.test(campaignId) ? Number(campaignId) : null;
  return id == null
    ? <CampaignsHub navigate={navigate} />
    : <CampaignDetail campaignId={id} navigate={navigate} />;
}

// ===========================================================================
// Hub — the cards plus everything awaiting review
// ===========================================================================

function CampaignsHub({ navigate }: { navigate: Props["navigate"] }) {
  const { items, review, loading, error, fetch, fetchReview, refresh, patchLocal,
    dropReviewItem } = useCampaignsStore();
  const [wizardOpen, setWizardOpen] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const toast = useToast();
  const confirm = useConfirm();

  useEffect(() => {
    void fetch();
    void fetchReview();
  }, [fetch, fetchReview]);

  // Poll only while a machine is actually working. The dep is a STRING of the
  // in-flight counts so the effect re-subscribes when that set changes rather
  // than on every store write.
  const busyKey = useMemo(
    () => items.map((c) => `${c.id}:${c.in_flight ?? 0}`).filter((s) => !s.endsWith(":0")).join(","),
    [items],
  );
  useEffect(() => {
    if (!busyKey) return;             // nothing generating ⇒ no timer at all
    let cancelled = false;
    const h = window.setInterval(() => {
      if (!cancelled) void refresh();
    }, POLL_MS);
    return () => { cancelled = true; window.clearInterval(h); };
  }, [busyKey, refresh]);

  const togglePause = useCallback(async (c: Campaign) => {
    const next = c.status === "active" ? "paused" : "active";
    setBusyId(c.id);
    patchLocal(c.id, { status: next });     // optimistic: the button responds now
    try {
      await api.campaigns.patch(c.id, { status: next });
      toast.info(next === "paused" ? "Campaign paused" : "Campaign resumed",
        next === "paused"
          ? "No new ideas or generations. Anything already approved still goes out."
          : "It will plan its next cycle on the next check.");
      await refresh();
    } catch (e: any) {
      patchLocal(c.id, { status: c.status });   // roll the optimistic edit back
      toast.error("Couldn't change the campaign", String(e?.message || e));
    } finally {
      setBusyId(null);
    }
  }, [patchLocal, refresh, toast]);

  const remove = useCallback(async (c: Campaign) => {
    const ok = await confirm({
      title: `Delete "${c.name}"?`,
      message:
        "Its queued and scheduled posts are cancelled. Posts already published stay " +
        "up, and every generated image and video stays in your Library.",
      confirmLabel: "Delete campaign",
      danger: true,
    });
    if (!ok) return;
    setBusyId(c.id);
    try {
      const r = await api.campaigns.remove(c.id);
      toast.info("Campaign deleted",
        r.bookings_cancelled
          ? `${r.bookings_cancelled} scheduled post${r.bookings_cancelled === 1 ? "" : "s"} cancelled.`
          : undefined);
      await refresh();
    } catch (e: any) {
      toast.error("Couldn't delete the campaign", String(e?.message || e));
    } finally {
      setBusyId(null);
    }
  }, [confirm, refresh, toast]);

  const runCycle = useCallback(async (c: Campaign) => {
    setBusyId(c.id);
    try {
      await api.campaigns.runCycle(c.id);
      toast.success("Planning this week now",
        "Ideas, images and captions appear here for review as they finish.");
      await refresh();
    } catch (e: any) {
      // A 409 here is expected and informative: the previous cycle is still busy.
      toast.error("Not yet", String(e?.message || e).replace(/^\d{3}\s+/, ""));
    } finally {
      setBusyId(null);
    }
  }, [refresh, toast]);

  const decided = useCallback(async (itemId: number) => {
    dropReviewItem(itemId);   // leave the queue immediately
    await refresh();
  }, [dropReviewItem, refresh]);

  const showSkeleton = loading && items.length === 0;

  return (
    <div className="space-y-6">
      <header className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <div className="eyebrow flex items-center gap-2">
            Producer
            <span className="pill bg-accent/10 text-accent border border-accent/20">beta</span>
          </div>
          <h1 className="text-xl font-semibold tracking-tight text-fg mt-0.5">
            Autonomous campaigns
          </h1>
          <p className="text-sm text-muted mt-1 max-w-2xl leading-relaxed">
            Give it a topic and a weekly quota. It comes up with the ideas, makes the
            posters and videos, writes the captions and picks the times — then waits
            for you to approve each one. Nothing is posted without you.
          </p>
        </div>
        <button
          className="btn btn-accent shrink-0"
          onClick={() => setWizardOpen(true)}
        >
          <Plus className="w-4 h-4" /> New campaign
        </button>
      </header>

      {error && (
        <div className="card p-3 text-sm text-danger flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 shrink-0" /> {error}
        </div>
      )}

      {/* ---- Review queue: GATE 1. Above the cards, because it's the only part
              of this page that is waiting on the human. ---- */}
      {review.length > 0 && (
        <section className="space-y-3">
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-semibold text-fg">
              {review.length} waiting for your approval
            </h2>
            <span className="text-2xs text-muted">
              approve to schedule · decline to have it remade
            </span>
          </div>
          <div className="space-y-2.5">
            {review.map((item) => (
              <ReviewCard key={item.id} item={item} onDecided={decided} />
            ))}
          </div>
        </section>
      )}

      {/* ---- Campaign cards ---- */}
      <section className="space-y-3">
        {(items.length > 0 || showSkeleton) && (
          <h2 className="text-sm font-semibold text-fg">
            {showSkeleton ? "Campaigns" : `${items.length} campaign${items.length === 1 ? "" : "s"}`}
          </h2>
        )}
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {showSkeleton
            ? [0, 1, 2].map((i) => <Skeleton key={i} className="h-[190px]" rounded="rounded-xl" />)
            : items.map((c) => (
              <CampaignCard
                key={c.id}
                c={c}
                busy={busyId === c.id}
                onOpen={() => navigate("campaigns", String(c.id))}
                onTogglePause={() => togglePause(c)}
                onRunCycle={() => runCycle(c)}
                onDelete={() => remove(c)}
              />
            ))}
          {!showSkeleton && (
            <button
              onClick={() => setWizardOpen(true)}
              className="rounded-xl border border-dashed border-border-strong p-6 text-left flex flex-col items-start gap-2 text-muted hover:text-fg hover:border-accent/50 transition-colors min-h-[190px] justify-center"
            >
              <Plus className="w-5 h-5" />
              <span className="text-sm font-medium">New campaign</span>
              <span className="text-xs leading-relaxed">
                e.g. “Cyber Security Week” — 3 posters and 2 videos every week
              </span>
            </button>
          )}
        </div>
      </section>

      {items.length === 0 && !showSkeleton && review.length === 0 && (
        <div className="text-center py-10 space-y-1.5">
          <p className="text-sm text-fg">No campaigns yet.</p>
          <p className="text-xs text-muted">
            A campaign keeps producing on a weekly rhythm so you only have to review.
          </p>
        </div>
      )}

      {wizardOpen && (
        <CampaignWizard
          onClose={() => setWizardOpen(false)}
          onCreated={async (c) => {
            setWizardOpen(false);
            toast.success(`“${c.name}” is live`,
              "It starts planning on the next check — usually within a minute.");
            await refresh();
          }}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------

function CampaignCard({
  c, busy, onOpen, onTogglePause, onRunCycle, onDelete,
}: {
  c: Campaign;
  busy: boolean;
  onOpen: () => void;
  onTogglePause: () => void;
  onRunCycle: () => void;
  onDelete: () => void;
}) {
  const paused = c.status === "paused";
  const awaiting = c.awaiting_review ?? 0;
  const inFlight = c.in_flight ?? 0;
  const next = countdown(c.next_post_at ?? null);
  const done = c.published_count ?? 0;
  const failed = c.failed_count ?? 0;

  return (
    <div className="card p-5 flex flex-col gap-3">
      <div className="flex items-start gap-2">
        <button onClick={onOpen} className="flex-1 min-w-0 text-left group">
          <div className="flex items-center gap-2">
            <span className={[
              "w-2 h-2 rounded-full shrink-0",
              paused ? "bg-muted" : inFlight > 0 ? "bg-sky-500 animate-pulse" : "bg-success",
            ].join(" ")} />
            <h3 className="font-semibold text-fg truncate group-hover:text-accent transition-colors">
              {c.name}
            </h3>
          </div>
          {c.topic_brief && (
            <p className="text-xs text-muted mt-1 line-clamp-2 leading-relaxed">
              {c.topic_brief}
            </p>
          )}
        </button>
        {paused && <span className="pill bg-bg-muted text-muted shrink-0">paused</span>}
      </div>

      {/* Why a machine paused it — budget, or a provider being out of credit. */}
      {c.status_reason && (
        <p className="text-2xs text-amber-600 flex items-start gap-1.5 leading-relaxed">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-px" /> {c.status_reason}
        </p>
      )}

      <div className="flex flex-wrap gap-1.5 text-2xs">
        {c.posters_per_week > 0 && (
          <span className="pill bg-bg-muted text-muted">
            {c.posters_per_week} poster{c.posters_per_week === 1 ? "" : "s"}/wk
          </span>
        )}
        {c.videos_per_week > 0 && (
          <span className="pill bg-bg-muted text-muted">
            {c.videos_per_week} video{c.videos_per_week === 1 ? "" : "s"}/wk
          </span>
        )}
        <span className="pill bg-bg-muted text-muted">{c.poster_style}</span>
      </div>

      <dl className="grid grid-cols-2 gap-y-1.5 gap-x-3 text-[11.5px]">
        {awaiting > 0 && (
          <>
            <dt className="text-muted">Needs review</dt>
            <dd className="text-accent font-semibold tabular-nums text-right">{awaiting}</dd>
          </>
        )}
        {inFlight > 0 && (
          <>
            <dt className="text-muted">Working on</dt>
            <dd className="text-fg tabular-nums text-right">{inFlight}</dd>
          </>
        )}
        {next && (
          <>
            <dt className="text-muted">Next post</dt>
            <dd className={`text-right tabular-nums ${next.due ? "text-accent" : "text-fg"}`}>
              {next.text}
            </dd>
          </>
        )}
        <dt className="text-muted">Posted</dt>
        <dd className="text-fg tabular-nums text-right">{done}</dd>
        {failed > 0 && (
          <>
            <dt className="text-muted">Failed</dt>
            <dd className="text-danger tabular-nums text-right">{failed}</dd>
          </>
        )}
        <dt className="text-muted">Spent</dt>
        <dd className="text-fg tabular-nums text-right">
          {money(c.cycle_cost_usd)}
          <span className="text-muted"> / ~{money(c.cycle_cost_estimate_usd)}/wk</span>
        </dd>
      </dl>

      <div className="flex items-center gap-1.5 mt-auto pt-1">
        <button className="btn btn-ghost text-xs" onClick={onOpen}>Open</button>
        <button className="btn btn-ghost text-xs" onClick={onTogglePause} disabled={busy}>
          {paused ? <><Play className="w-3.5 h-3.5" /> Resume</>
            : <><Pause className="w-3.5 h-3.5" /> Pause</>}
        </button>
        <button
          className="btn btn-ghost text-xs"
          onClick={onRunCycle}
          disabled={busy || paused}
          title="Plan this week's posts now instead of waiting for the weekly cycle"
        >
          <Sparkles className="w-3.5 h-3.5" /> Run now
        </button>
        <button
          className="btn btn-ghost text-xs ml-auto text-danger"
          onClick={onDelete}
          disabled={busy}
          aria-label={`Delete ${c.name}`}
        >
          {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
        </button>
      </div>
    </div>
  );
}

// ===========================================================================
// Detail — one campaign, its cycles and every item
// ===========================================================================

function CampaignDetail({
  campaignId, navigate,
}: { campaignId: number; navigate: Props["navigate"] }) {
  const [campaign, setCampaign] = useState<Campaign | null>(null);
  const [items, setItems] = useState<CampaignItem[]>([]);
  const [spent, setSpent] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      const r = await api.campaigns.get(campaignId);
      setCampaign(r.campaign);
      setItems(r.items);
      setSpent(r.spent_this_cycle_usd);
      setError(null);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setLoading(false);
    }
  }, [campaignId]);

  useEffect(() => { void load(); }, [load]);

  // Same idiom as the creators: a string key over the in-flight ids, so the
  // effect only re-subscribes when that SET changes, and no timer exists at all
  // when the worker has nothing to do.
  const busyKey = useMemo(
    () => items.filter((i) => BUSY.includes(i.status)).map((i) => i.id).sort((a, b) => a - b).join(","),
    [items],
  );
  useEffect(() => {
    if (!busyKey) return;
    let cancelled = false;
    const h = window.setInterval(() => { if (!cancelled) void load(); }, POLL_MS);
    return () => { cancelled = true; window.clearInterval(h); };
  }, [busyKey, load]);

  // Group by cycle, newest week first.
  const cycles = useMemo(() => {
    const by = new Map<number, CampaignItem[]>();
    for (const it of items) {
      const arr = by.get(it.cycle_index) ?? [];
      arr.push(it);
      by.set(it.cycle_index, arr);
    }
    return [...by.entries()].sort((a, b) => b[0] - a[0]);
  }, [items]);

  if (loading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-40" rounded="rounded-xl" />
      </div>
    );
  }

  if (error || !campaign) {
    return (
      <div className="space-y-4">
        <button className="btn btn-ghost text-sm" onClick={() => navigate("campaigns")}>
          <ArrowLeft className="w-4 h-4" /> Campaigns
        </button>
        <div className="card p-4 text-sm text-danger">{error || "Campaign not found."}</div>
      </div>
    );
  }

  const awaiting = items.filter((i) => i.status === "ready_for_review");

  return (
    <div className="space-y-6">
      <header className="flex items-start gap-3">
        <button
          className="btn btn-ghost text-sm shrink-0"
          onClick={() => navigate("campaigns")}
        >
          <ArrowLeft className="w-4 h-4" /> Campaigns
        </button>
        <div className="flex-1 min-w-0">
          <h1 className="text-lg font-semibold tracking-tight text-fg truncate">
            {campaign.name}
          </h1>
          <p className="text-xs text-muted mt-0.5">
            {campaign.posters_per_week > 0 && `${campaign.posters_per_week} poster${campaign.posters_per_week === 1 ? "" : "s"}`}
            {campaign.posters_per_week > 0 && campaign.videos_per_week > 0 && " · "}
            {campaign.videos_per_week > 0 && `${campaign.videos_per_week} video${campaign.videos_per_week === 1 ? "" : "s"}`}
            {" a week · week "}{(campaign.cycle_index ?? 0) + 1}
            {" · "}{money(spent)} spent this week
          </p>
        </div>
        {campaign.status === "paused" && (
          <span className="pill bg-bg-muted text-muted shrink-0">paused</span>
        )}
      </header>

      {campaign.topic_brief && (
        <p className="text-sm text-muted leading-relaxed max-w-2xl">
          {campaign.topic_brief}
        </p>
      )}

      {/* Posting rhythm, so it's obvious when things will go out. */}
      {campaign.posting_windows.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-2xs text-muted mr-1">Posting slots:</span>
          {campaign.posting_windows.map((w, i) => (
            <span key={i} className="pill bg-bg-muted text-muted">
              {WEEKDAYS[w.weekday] ?? "?"} {w.time}
            </span>
          ))}
        </div>
      )}

      {awaiting.length > 0 && (
        <section className="space-y-3">
          <h2 className="text-sm font-semibold text-fg">
            {awaiting.length} waiting for your approval
          </h2>
          <div className="space-y-2.5">
            {awaiting.map((item) => (
              <ReviewCard key={item.id} item={item} onDecided={async () => { await load(); }} />
            ))}
          </div>
        </section>
      )}

      {cycles.map(([cycleIndex, cycleItems]) => (
        <section key={cycleIndex} className="space-y-2">
          <h2 className="text-sm font-semibold text-fg">
            Week {cycleIndex + 1}
            <span className="ml-2 text-2xs font-normal text-muted">
              {cycleItems[0]?.cycle_start ?? ""}
            </span>
          </h2>
          <div className="space-y-1.5">
            {cycleItems.map((it) => (
              <ItemRow key={it.id} item={it} onChanged={load} toastError={toast.error} />
            ))}
          </div>
        </section>
      ))}

      {items.length === 0 && (
        <div className="text-center py-12 space-y-1.5">
          <p className="text-sm text-fg">Nothing planned yet.</p>
          <p className="text-xs text-muted">
            {campaign.status === "paused"
              ? "This campaign is paused — resume it to start the next week."
              : "The first week is planned on the next check, usually within a minute."}
          </p>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------

function ItemRow({
  item, onChanged, toastError,
}: {
  item: CampaignItem;
  onChanged: () => Promise<void> | void;
  toastError: (t: string, d?: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const meta = STATUS_META[item.status] ?? STATUS_META.planned;
  const when = countdown(item.proposed_time);
  const title = item.idea_title || item.idea?.title || `${item.kind} #${item.slot_index + 1}`;

  const cancel = async () => {
    setBusy(true);
    try {
      await api.campaigns.cancel(item.id);
      await onChanged();
    } catch (e: any) {
      toastError("Couldn't cancel", String(e?.message || e).replace(/^\d{3}\s+/, ""));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card p-3 flex items-center gap-3">
      <span className={`pill shrink-0 ${meta.cls}`}>{meta.label}</span>
      <div className="flex-1 min-w-0">
        <p className="text-sm text-fg truncate">{title}</p>
        <p className="text-2xs text-muted flex items-center gap-2 mt-0.5">
          <span className="capitalize">{item.kind}</span>
          {when && (
            <span className="flex items-center gap-1">
              <Clock className="w-3 h-3" /> {when.text}
            </span>
          )}
          {item.attempt > 1 && <span>attempt {item.attempt}</span>}
          {item.cost_usd > 0 && <span className="tabular-nums">{money(item.cost_usd)}</span>}
        </p>
        {item.error && (
          <p className="text-2xs text-danger mt-0.5 line-clamp-2">{item.error}</p>
        )}
        {item.decline_note && item.status === "rejected" && (
          <p className="text-2xs text-muted mt-0.5 italic line-clamp-1">
            you said: {item.decline_note}
          </p>
        )}
      </div>

      {/* GATE 2 lives in Library → Scheduled, so a booked item links there
          rather than growing a second Post button. */}
      {item.status === "scheduled" && (
        <a
          href="#library/scheduled"
          className="btn btn-accent text-xs shrink-0 whitespace-nowrap"
          title="Approved and booked — post it from the Scheduled queue"
        >
          Post →
        </a>
      )}
      {item.status === "published" && item.permalink && (
        <a
          href={item.permalink}
          target="_blank"
          rel="noreferrer"
          className="text-2xs text-accent underline shrink-0"
        >
          View
        </a>
      )}
      {item.status === "published" && !item.permalink && (
        <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" />
      )}
      {(item.status === "scheduled" || item.status === "approved") && (
        <button
          className="btn btn-ghost text-xs shrink-0"
          onClick={cancel}
          disabled={busy}
        >
          {busy ? "…" : "Cancel"}
        </button>
      )}
      {(item.status === "rejected" || item.status === "expired" || item.status === "cancelled") && (
        <XCircle className="w-4 h-4 text-muted shrink-0" />
      )}
      {item.status === "generating" && (
        <Loader2 className="w-4 h-4 text-sky-600 animate-spin shrink-0" />
      )}
      {item.status === "planned" && <Radio className="w-4 h-4 text-muted shrink-0" />}
    </div>
  );
}
