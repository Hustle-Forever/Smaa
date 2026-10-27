/**
 * Library → Scheduled tab. Lists every post booked into the calendar so the user
 * can get back to anything scheduled for later (the roadmap flow itself is
 * one-shot / in-memory). Click one to open its Approval screen (loaded by id);
 * cancel one to drop it from the queue and free its hosted media.
 */
import { useEffect, useMemo, useState } from "react";

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
  pending: { label: "Scheduled", cls: "bg-warning/15 text-amber-600" },
  reminded: { label: "Reminded", cls: "bg-warning/15 text-amber-600" },
  published: { label: "Posted", cls: "bg-success/15 text-emerald-600" },
  failed: { label: "Failed", cls: "bg-rose-500/15 text-rose-600" },
  cancelled: { label: "Cancelled", cls: "bg-bg-muted text-muted" },
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
      <div className="flex items-center gap-2">
        <p className="text-sm text-muted flex-1">
          {items ? `${items.length} scheduled post${items.length === 1 ? "" : "s"}` : "Loading…"}
        </p>
        <button onClick={load} className="btn btn-ghost text-xs">Refresh</button>
      </div>

      {error && <p className="text-sm text-danger">Couldn’t load scheduled posts: {error}</p>}

      {items && items.length === 0 && !error && (
        <div className="text-center py-16 space-y-1.5">
          <p className="text-sm text-fg">Nothing scheduled yet.</p>
          <p className="text-xs text-muted">Book a post from Create → Scheduling and it’ll show up here.</p>
        </div>
      )}

      <div className="space-y-2.5">
        {sorted.map((p) => {
          const cd = countdown(p.scheduled_at);
          const meta = STATUS_META[p.status] ?? STATUS_META.pending;
          const upcoming = p.status === "pending" || p.status === "reminded";
          const actionable = upcoming || p.status === "failed";
          return (
            <div key={p.id} className="card p-3 flex items-center gap-3">
              <div className="w-16 h-20 shrink-0 rounded-lg overflow-hidden bg-bg-muted flex items-center justify-center">
                {p.media_type === "video" ? (
                  <video src={p.display_url} muted preload="metadata" className="w-full h-full object-cover" />
                ) : (
                  <img src={p.display_url} alt="" loading="lazy" className="w-full h-full object-cover" />
                )}
              </div>

              <div className="flex-1 min-w-0 space-y-1">
                <div className="flex items-center gap-2">
                  <span className={`font-mono text-3xs px-1.5 py-0.5 rounded ${meta.cls}`}>{meta.label}</span>
                  {upcoming && (
                    <span className={`text-2xs font-medium ${cd.due ? "text-accent" : "text-muted"}`}>{cd.text}</span>
                  )}
                </div>
                <p className="text-xs text-fg/85 line-clamp-2">{p.caption || "(no caption)"}</p>
                <p className="text-[10.5px] font-mono text-muted">{prettyTime(p.scheduled_at)}</p>
                {p.status === "published" && p.permalink && (
                  <a href={p.permalink} target="_blank" rel="noreferrer" className="text-2xs text-accent underline break-all">
                    {p.permalink}
                  </a>
                )}
                {p.status === "failed" && p.error && (
                  <p className="text-2xs text-danger line-clamp-2">{p.error}</p>
                )}
              </div>

              {actionable && (
                <div className="shrink-0 flex flex-col gap-1.5">
                  <button onClick={() => setSelected(p)} className="btn btn-accent text-xs whitespace-nowrap">
                    {p.status === "failed" ? "Retry →" : "Approve →"}
                  </button>
                  {upcoming && (
                    <button
                      onClick={() => doCancel(p)}
                      disabled={cancelling === p.id}
                      className="btn btn-ghost text-xs"
                    >
                      {cancelling === p.id ? "…" : "Cancel"}
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
