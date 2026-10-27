/**
 * Library → Scheduled tab. Lists every post booked into the calendar so the user
 * can get back to anything scheduled for later (the roadmap flow itself is
 * one-shot / in-memory). Click one to open its Approval screen (loaded by id);
 * cancel one to drop it from the queue and free its hosted media.
 */
import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ArrowRight, CalendarClock, ExternalLink, RefreshCw } from "lucide-react";

import { useConfirm } from "../../ConfirmProvider";
import { useToast } from "../../ToastProvider";
import { CLOCK_12H } from "../../../lib/format";
import { roadmapApi, type ScheduledPost } from "./api";
import { ApprovalStep } from "./ApprovalStep";
import type { FinalizedAsset } from "./types";

function countdown(iso: string): { text: string; due: boolean } {
  const diff = new Date(iso).getTime() - Date.now();
  const due = diff <= 0;
  const abs = Math.abs(diff);
  const mins = Math.round(abs / 60_000);
  const hours = Math.round(abs / 3_600_000);
  const days = Math.round(abs / 86_400_000);
  const unit =
    mins < 60 ? `${mins} min` : hours < 48 ? `${hours} hr${hours === 1 ? "" : "s"}` : `${days} day${days === 1 ? "" : "s"}`;
  if (due) return { text: mins < 60 ? "due now" : `${unit} ago`, due: true };
  return { text: `in ${unit}`, due: false };
}

function prettyTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      weekday: "short", year: "numeric", month: "short", day: "numeric",
      ...CLOCK_12H,
    });
  } catch {
    return iso;
  }
}

const STATUS_META: Record<ScheduledPost["status"], { label: string; cls: string }> = {
  // 42 palette: purple = booked, cyan = live, coral = failed.
  pending: { label: "Scheduled", cls: "bg-k-purple text-white" },
  reminded: { label: "Reminder sent", cls: "bg-k-purple text-white" },
  published: { label: "Posted", cls: "bg-k-cyan text-k-oncyan" },
  failed: { label: "Didn't post", cls: "bg-k-coral text-white" },
  cancelled: { label: "Cancelled", cls: "bg-k-inset text-fg/60 ring-1 ring-k-border" },
};

function toAsset(p: ScheduledPost): FinalizedAsset {
  return {
    tool: p.tool,
    refId: p.ref_id,
    mediaType: p.media_type,
    mediaUrl: p.display_url,
    topic: "",
    caption: p.caption,
    scheduledAt: p.scheduled_at,
    scheduleId: p.id,
  };
}

export function ScheduledQueue() {
  const [items, setItems] = useState<ScheduledPost[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<ScheduledPost | null>(null);
  const [cancelling, setCancelling] = useState<number | null>(null);
  const toast = useToast();
  const confirm = useConfirm();

  async function load() {
    try {
      const r = await roadmapApi.listScheduled();
      setItems(r.items);
      setError(null);
    } catch (e: any) {
      setError(e?.message || String(e));
    }
  }
  useEffect(() => {
    load();
  }, []);

  // Upcoming (pending/reminded) first, soonest at top; everything else after.
  const sorted = useMemo(() => {
    if (!items) return [];
    const rank = (s: string) => (s === "pending" || s === "reminded" ? 0 : 1);
    return [...items].sort((a, b) => {
      const ra = rank(a.status);
      const rb = rank(b.status);
      if (ra !== rb) return ra - rb;
      const ta = new Date(a.scheduled_at).getTime();
      const tb = new Date(b.scheduled_at).getTime();
      return ra === 0 ? ta - tb : tb - ta;
    });
  }, [items]);

  async function doCancel(p: ScheduledPost) {
    if (cancelling != null) return;
    if (
      !(await confirm({
        title: "Cancel scheduled post?",
        message: "It won’t be posted, and its media will be removed.",
        confirmLabel: "Cancel post",
        cancelLabel: "Keep",
        danger: true,
      }))
    )
      return;
    setCancelling(p.id);
    try {
      await roadmapApi.cancel(p.id);
      toast.info("Cancelled", "Removed from the schedule.");
      await load();
    } catch (e: any) {
      toast.error("Couldn’t cancel", e?.message || String(e));
    } finally {
      setCancelling(null);
    }
  }

  if (selected) {
    return (
      <ApprovalStep
        asset={toAsset(selected)}
        backLabel="Scheduled"
        onBack={() => setSelected(null)}
        onDone={() => {
          setSelected(null);
          load();
        }}
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex-1 min-w-0">
          <h2 className="k-title">Scheduled posts</h2>
          <p className="text-[13px] text-fg/60">
            {items
              ? `${items.length} ${items.length === 1 ? "post" : "posts"}. The soonest is at the top. Press Approve when one is due.`
              : "Loading…"}
          </p>
        </div>
        <button onClick={load} className="btn btn-ghost btn-sm">
          <RefreshCw className="w-3.5 h-3.5" strokeWidth={2} aria-hidden />
          Refresh
        </button>
      </div>

      {error && (
        <div className="k-panel px-5 py-4 flex items-start gap-3" role="alert">
          <AlertTriangle className="w-5 h-5 text-k-danger shrink-0 mt-0.5" strokeWidth={1.8} aria-hidden />
          <div className="space-y-0.5 min-w-0">
            <p className="text-[14px] font-semibold text-fg">Couldn't load scheduled posts</p>
            <p className="text-[13px] text-fg/60 break-words">{error}</p>
          </div>
        </div>
      )}

      {items && items.length === 0 && !error && (
        <div className="k-panel px-6 py-12 flex flex-col items-center text-center gap-3">
          <span className="k-well w-12 h-12">
            <CalendarClock className="w-5 h-5" strokeWidth={1.5} aria-hidden />
          </span>
          <div className="space-y-1 max-w-md">
            <p className="k-title before:content-none text-[15px]">Nothing scheduled yet</p>
            <p className="text-[13.5px] text-fg/60">
              Finish a piece in Ideation, write its caption and pick a date. It will show up here.
            </p>
          </div>
          <a href="#producer" className="k-btn k-btn-sm mt-1">Make something</a>
        </div>
      )}

      <div className="space-y-2.5">
        {sorted.map((p) => {
          const cd = countdown(p.scheduled_at);
          const meta = STATUS_META[p.status] ?? STATUS_META.pending;
          const upcoming = p.status === "pending" || p.status === "reminded";
          const actionable = upcoming || p.status === "failed";
          return (
            <div key={p.id} className={["card p-3 flex items-center gap-4", upcoming && cd.due ? "!border-2 !border-k-btn" : ""].join(" ")}>
              <div className="w-16 h-20 shrink-0 overflow-hidden bg-bg-muted flex items-center justify-center">
                {p.media_type === "video" ? (
                  <video src={p.display_url} muted preload="metadata" className="w-full h-full object-cover" />
                ) : (
                  <img src={p.display_url} alt="" loading="lazy" className="w-full h-full object-cover" />
                )}
              </div>

              <div className="flex-1 min-w-0 space-y-1">
                <div className="flex items-center gap-2">
                  <span className={`px-2 py-0.5 text-[10.5px] font-bold uppercase tracking-[0.06em] ${meta.cls}`}>{meta.label}</span>
                  {upcoming && (
                    <span className={`text-[12px] font-semibold ${cd.due ? "text-k-accent" : "text-fg/55"}`}>
                      {cd.due ? `Due ${cd.text === "due now" ? "now" : cd.text}` : cd.text}
                    </span>
                  )}
                </div>
                <p className="text-[13.5px] text-fg/85 line-clamp-2">{p.caption || "No caption yet"}</p>
                <p className="text-[12px] text-fg/55">{prettyTime(p.scheduled_at)}</p>
                {p.status === "published" && p.permalink && (
                  <a href={p.permalink} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-[12px] font-semibold text-k-accent hover:underline">
                    See it on Instagram <ExternalLink className="w-3 h-3" strokeWidth={2} aria-hidden />
                  </a>
                )}
                {p.status === "failed" && p.error && (
                  <p className="text-[12px] text-k-danger line-clamp-2">{p.error}</p>
                )}
              </div>

              {actionable && (
                <div className="shrink-0 flex flex-col gap-1.5">
                  <button onClick={() => setSelected(p)} className="btn btn-accent btn-sm whitespace-nowrap">
                    {p.status === "failed" ? "Try again" : "Approve"}
                    <ArrowRight className="w-3.5 h-3.5" strokeWidth={2.2} aria-hidden />
                  </button>
                  {upcoming && (
                    <button
                      onClick={() => doCancel(p)}
                      disabled={cancelling === p.id}
                      className="btn btn-ghost btn-sm"
                    >
                      {cancelling === p.id ? "Cancelling…" : "Cancel"}
                    </button>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
