/**
 * Global Step 5 — Approval (the gatekeeper).
 *
 * Shows the finalized media, the scheduled time, and the approved caption with a
 * prominent "Approve & Post to Instagram" button. Clicking it triggers the live
 * publish routine on the backend (which self-gates if no publish token is set).
 *
 * Reached two ways: immediately after scheduling ("waiting for the scheduled
 * date"), or by following the reminder email's link on the scheduled day.
 */
import { useEffect, useMemo, useState } from "react";

import { useToast } from "../../ToastProvider";
import { CLOCK_12H } from "../../../lib/format";
import { roadmapApi } from "./api";
import type { FinalizedAsset } from "./types";

// Module-scope guard: schedule ids whose "Post now" auto-publish has already
// fired. Lives outside the component so React 18 StrictMode's mount → unmount →
// remount (dev only) can't fire a second concurrent publish for the same post.
const autoFiredIds = new Set<number>();

export function ApprovalStep({
  asset,
  onBack,
  onDone,
  backLabel = "Scheduling",
  autoApprove = false,
}: {
  asset: FinalizedAsset;
  onBack: () => void;
  onDone: () => void;
  /** Label for the back button — "Scheduling" in the roadmap, "Scheduled" from the queue. */
  backLabel?: string;
  /** "Post now" path: publish immediately on mount instead of waiting for the
   *  user to click "Approve & Post". Fires exactly once. */
  autoApprove?: boolean;
}) {
  const [posting, setPosting] = useState(false);
  const [posted, setPosted] = useState<{ permalink: string | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  // "Post now": kick off the publish automatically, once, as soon as we land
  // here with a scheduled row. Guarded by the module-scope id set so it fires
  // exactly once even across StrictMode's dev remount.
  useEffect(() => {
    if (autoApprove && asset.scheduleId != null && !autoFiredIds.has(asset.scheduleId)) {
      autoFiredIds.add(asset.scheduleId);
      void approve();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoApprove, asset.scheduleId]);

  const due = useMemo(() => {
    if (!asset.scheduledAt) return { isDue: true, pretty: "" };
    const when = new Date(asset.scheduledAt).getTime();
    const pretty = new Date(asset.scheduledAt).toLocaleString(undefined, {
      weekday: "short", year: "numeric", month: "short", day: "numeric",
      ...CLOCK_12H,
    });
    return { isDue: Date.now() >= when, pretty };
  }, [asset.scheduledAt]);

  async function approve() {
    if (posting || asset.scheduleId == null) return;
    setPosting(true);
    setError(null);
    try {
      const r = await roadmapApi.approve(asset.scheduleId);
      if (r.status === "published") {
        setPosted({ permalink: r.permalink });
        toast.info("Posted to Instagram", r.permalink || "Live now");
      } else {
        // Backend self-gated (e.g. no publish token) — surface its reason.
        setError(r.detail || "Publishing is not configured.");
        toast.error("Not published", r.detail || "Publishing not configured");
      }
    } catch (e: any) {
      const msg = e?.message || String(e);
      setError(msg);
      toast.error("Publish failed", msg);
    } finally {
      setPosting(false);
    }
  }

  return (
    <div className="space-y-5">
      <header className="flex items-center gap-3">
        <button onClick={onBack} className="btn btn-ghost text-sm">← {backLabel}</button>
        <h1 className="h1 flex-1">Approval</h1>
        {asset.scheduledAt && (
          <span
            className={[
              "font-mono text-2xs px-2 py-1 rounded",
              due.isDue ? "bg-accent/15 text-accent" : "bg-bg-muted text-muted",
            ].join(" ")}
            title="Scheduled publish time"
          >
            {due.isDue ? "Due now" : `Waiting · ${due.pretty}`}
          </span>
        )}
      </header>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 items-start">
        <section className="card p-4">
          <p className="eyebrow mb-2">Media</p>
          <div className="bg-black rounded-lg overflow-hidden flex items-center justify-center" style={{ maxHeight: "70vh" }}>
            {asset.mediaType === "video" ? (
              <video src={asset.mediaUrl} controls loop className="max-w-full max-h-[70vh] object-contain" />
            ) : (
              <img src={asset.mediaUrl} alt={asset.topic} className="max-w-full max-h-[70vh] object-contain" />
            )}
          </div>
        </section>

        <section className="card p-4 space-y-4">
          <div>
            <p className="eyebrow mb-1.5">Caption</p>
            <div className="rounded-lg bg-bg-muted px-3 py-2 text-sm text-fg whitespace-pre-wrap max-h-[40vh] overflow-y-auto">
              {asset.caption}
            </div>
          </div>

          {asset.scheduledAt && (
            <p className="text-sm text-muted">
              Scheduled for <span className="text-fg font-medium">{due.pretty}</span> (Asia/Dubai).
            </p>
          )}

          {posted ? (
            <div className="rounded-lg border border-accent/40 bg-accent/8 px-3 py-3 text-sm text-fg space-y-2">
              <p className="font-semibold">✓ Posted to Instagram</p>
              {posted.permalink && (
                <a href={posted.permalink} target="_blank" rel="noreferrer" className="text-accent underline break-all">
                  {posted.permalink}
                </a>
              )}
              <button onClick={onDone} className="btn btn-ghost text-sm w-full mt-1">Done — back to Ideation</button>
            </div>
          ) : (
            <>
              {error && <p className="text-xs text-danger" role="alert">{error}</p>}
              <button
                onClick={approve}
                disabled={posting}
                className="w-full rounded-xl px-5 py-4 text-base font-semibold transition-all shadow-sm hover:shadow-md bg-accent text-white hover:bg-accent-hover disabled:opacity-60"
              >
                {posting ? "Posting…" : "Approve & Post to Instagram"}
              </button>
              <p className="text-2xs text-muted text-center">
                Posts the media + caption live via the Instagram Graph API.
              </p>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
