/**
 * Global Step 4 — Calendar.
 *
 * Pick the date + time to publish. Saves the asset, caption, and timestamp into
 * the backend pending queue and arms a reminder email at that time. Times are
 * interpreted in Asia/Dubai (Gulf Standard Time, UTC+4, no DST).
 */
import { useMemo, useState } from "react";

import { useToast } from "../../ToastProvider";
import { CLOCK_12H } from "../../../lib/format";
import { roadmapApi } from "./api";
import type { FinalizedAsset } from "./types";

const TZ = "Asia/Dubai";
const TZ_OFFSET = "+04:00"; // GST, no daylight saving

const pad2 = (n: number) => String(n).padStart(2, "0");

// Today / now in Asia/Dubai, as the input defaults.
function nowParts(): { date: string; time: string } {
  const now = new Date();
  // Shift to Dubai wall-clock then read fields.
  const dubai = new Date(now.getTime() + (4 * 60 + now.getTimezoneOffset()) * 60_000);
  const date = `${dubai.getFullYear()}-${pad2(dubai.getMonth() + 1)}-${pad2(dubai.getDate())}`;
  // Default to the next whole hour.
  const hr = (dubai.getHours() + 1) % 24;
  return { date, time: `${pad2(hr)}:00` };
}

// Current instant as a Dubai-offset ISO string — the "Post now" timestamp.
function nowIsoDubai(): string {
  const now = new Date();
  const dubai = new Date(now.getTime() + (4 * 60 + now.getTimezoneOffset()) * 60_000);
  return (
    `${dubai.getFullYear()}-${pad2(dubai.getMonth() + 1)}-${pad2(dubai.getDate())}` +
    `T${pad2(dubai.getHours())}:${pad2(dubai.getMinutes())}:${pad2(dubai.getSeconds())}${TZ_OFFSET}`
  );
}

export function CalendarStep({
  asset,
  onBack,
  onScheduled,
  onPostNow,
}: {
  asset: FinalizedAsset;
  onBack: () => void;
  onScheduled: (scheduleId: number, scheduledAt: string) => void;
  /** "Post now" — booked the row for the current instant; the Approval step
   *  auto-publishes on arrival. */
  onPostNow: (scheduleId: number, scheduledAt: string) => void;
}) {
  const defaults = useMemo(nowParts, []);
  const [date, setDate] = useState(asset.scheduledAt?.slice(0, 10) ?? defaults.date);
  const [time, setTime] = useState(asset.scheduledAt?.slice(11, 16) ?? defaults.time);
  const [saving, setSaving] = useState(false);
  const [postingNow, setPostingNow] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();
  const busy = saving || postingNow;

  // Carousel slides (≥2) flatten to the schedule payload; shared by both the
  // "schedule for later" and "post now" paths.
  const carouselPayload = () =>
    asset.carouselItems && asset.carouselItems.length >= 2
      ? asset.carouselItems.map((s) => ({
          tool: s.tool, ref_id: s.refId, media_type: s.mediaType,
        }))
      : undefined;

  const scheduledAt = `${date}T${time}:00${TZ_OFFSET}`;
  const pretty = useMemo(() => {
    try {
      return new Date(scheduledAt).toLocaleString(undefined, {
        weekday: "short", year: "numeric", month: "short", day: "numeric",
        ...CLOCK_12H,
      });
    } catch {
      return scheduledAt;
    }
  }, [scheduledAt]);

  async function schedule() {
    if (busy) return;
    setSaving(true);
    setError(null);
    try {
      const r = await roadmapApi.schedule({
        tool: asset.tool,
        ref_id: asset.refId,
        media_type: asset.mediaType,
        caption: asset.caption ?? "",
        scheduled_at: scheduledAt,
        timezone: TZ,
        carousel_items: carouselPayload(),
        thumb_offset: asset.thumbOffsetMs ?? null,
      });
      if (!r.media_hosted) {
        // The post is queued, but the media couldn't be copied to the public
        // bucket. Approve & Post will retry, but flag it so it's not a surprise.
        toast.error(
          "Scheduled, but media isn’t hosted",
          r.upload_warning ||
            "Couldn’t copy the media to public storage. Approve & Post will try again.",
        );
      } else if (!r.email_configured) {
        toast.info("Scheduled", "Email reminders aren’t configured — it’ll wait in the queue.");
      } else {
        toast.info("Scheduled", `Reminder email set for ${pretty}`);
      }
      onScheduled(r.id, r.scheduled_at);
    } catch (e: any) {
      const msg = e?.message || String(e);
      setError(msg);
      toast.error("Couldn’t schedule", msg);
    } finally {
      setSaving(false);
    }
  }

  // "Post now": book the row for the current instant + host the media, then
  // hand off to Approval which auto-publishes. Same backend as scheduling —
  // just `scheduled_at = now` followed by an immediate approve.
  async function postNow() {
    if (busy) return;
    setPostingNow(true);
    setError(null);
    try {
      const r = await roadmapApi.schedule({
        tool: asset.tool,
        ref_id: asset.refId,
        media_type: asset.mediaType,
        caption: asset.caption ?? "",
        scheduled_at: nowIsoDubai(),
        timezone: TZ,
        immediate: true, // publishes right away — no reminder email
        carousel_items: carouselPayload(),
        thumb_offset: asset.thumbOffsetMs ?? null,
      });
      if (!r.media_hosted) {
        // Approval re-hosts before publishing, so this isn't fatal — just warn.
        toast.error(
          "Media isn’t hosted yet",
          r.upload_warning || "Couldn’t upload to the public bucket — publishing will retry.",
        );
      }
      onPostNow(r.id, r.scheduled_at);
    } catch (e: any) {
      const msg = e?.message || String(e);
      setError(msg);
      toast.error("Couldn’t post", msg);
    } finally {
      setPostingNow(false);
    }
  }

  return (
    <div className="space-y-5">
      <header className="flex items-center gap-3">
        <button onClick={onBack} className="btn btn-ghost text-sm">← Finalization</button>
        <h1 className="h1 flex-1">Scheduling</h1>
      </header>

      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,360px)_minmax(0,1fr)] gap-5 items-start">
        {/* Picker */}
        <section className="card p-5 space-y-4">
          <div>
            <p className="eyebrow mb-1.5">Date</p>
            <input
              type="date"
              value={date}
              min={defaults.date}
              onChange={(e) => setDate(e.target.value)}
              className="input w-full"
            />
          </div>
          <div>
            <p className="eyebrow mb-1.5">Time <span className="normal-case font-normal text-muted">({TZ})</span></p>
            <input
              type="time"
              value={time}
              onChange={(e) => setTime(e.target.value)}
              className="input w-full"
            />
          </div>
          <div className="rounded-lg bg-bg-muted px-3 py-2 text-sm text-fg">
            <span className="font-semibold">Post now</span> publishes immediately.
            <span className="block text-2xs text-muted mt-0.5">
              Or <span className="font-medium">schedule for later</span> — books{" "}
              {pretty}, sends a reminder email, then you approve &amp; post.
            </span>
          </div>
          {error && <p className="text-xs text-danger" role="alert">{error}</p>}
          <div className="space-y-2">
            <button onClick={postNow} disabled={busy} className="btn btn-accent w-full">
              {postingNow ? "Posting…" : "⚡ Post now"}
            </button>
            <button
              onClick={schedule}
              disabled={busy}
              className="btn btn-ghost w-full border border-border"
            >
              {saving ? "Scheduling…" : "Schedule for later →"}
            </button>
          </div>
        </section>

        {/* Summary */}
        <section className="card p-4 space-y-3">
          <p className="eyebrow">Post summary</p>
          <div className="bg-black rounded-lg overflow-hidden flex items-center justify-center" style={{ maxHeight: "40vh" }}>
            {asset.mediaType === "video" ? (
              <video src={asset.mediaUrl} muted loop className="max-w-full max-h-[40vh] object-contain" />
            ) : (
              <img src={asset.mediaUrl} alt={asset.topic} className="max-w-full max-h-[40vh] object-contain" />
            )}
          </div>
          <p className="text-sm text-fg whitespace-pre-wrap line-clamp-6">{asset.caption}</p>
        </section>
      </div>
    </div>
  );
}
