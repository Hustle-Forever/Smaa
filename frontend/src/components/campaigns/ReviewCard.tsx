/**
 * GATE 1 — the human decision on one generated item.
 *
 * The campaign worker produced an image or video and a caption; nothing goes any
 * further until someone presses Approve or Decline here. Approving only BOOKS the
 * post (a second, separate human click in Library → Scheduled actually publishes
 * it), so the copy on this card is careful never to imply it posts.
 *
 * The reviewer can fix the caption and move the time inline before approving,
 * because a small edit is much more common than a full regeneration and it would
 * be a waste to spend another render on a wording tweak.
 *
 * Declining opens a note field. The note is not decoration: it goes into the next
 * generation's prompt AND into future weeks' planning, so the campaign learns
 * what this reviewer keeps rejecting.
 */
import { useState } from "react";
import { Loader2 } from "lucide-react";

import { Thumb } from "../Thumb";
import { useToast } from "../ToastProvider";
import { api } from "../../lib/api";
import { CLOCK_12H } from "../../lib/format";
import type { CampaignItem } from "../../lib/types";

/** Local time the backend schedules in (Gulf Standard Time, no DST). */
const TZ_OFFSET = "+04:00";

/** ISO → the `datetime-local` shape, in Dubai wall-clock. */
function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  // Shift to UTC+4 then read the "UTC" fields, so the box shows Dubai time
  // whatever timezone the reviewer's laptop is in.
  const shifted = new Date(d.getTime() + 4 * 60 * 60 * 1000);
  return shifted.toISOString().slice(0, 16);
}

/** `datetime-local` value → an offset-bearing ISO the backend can cast. */
function fromLocalInput(value: string): string | undefined {
  return value ? `${value}:00${TZ_OFFSET}` : undefined;
}

function prettyTime(iso: string | null): string {
  if (!iso) return "no time set";
  try {
    return new Date(iso).toLocaleString(undefined, {
      weekday: "short", month: "short", day: "numeric",
      ...CLOCK_12H,
    });
  } catch {
    return iso;
  }
}

interface Props {
  item: CampaignItem;
  /** Called after a decision lands, so the parent can refetch. */
  onDecided: (itemId: number) => Promise<void> | void;
}

export function ReviewCard({ item, onDecided }: Props) {
  const [caption, setCaption] = useState(item.caption ?? "");
  const [when, setWhen] = useState(toLocalInput(item.proposed_time));
  const [busy, setBusy] = useState<"approve" | "decline" | null>(null);
  const [declining, setDeclining] = useState(false);
  const [note, setNote] = useState("");
  const [regenerate, setRegenerate] = useState(true);
  const toast = useToast();

  const isVideo = item.kind === "video";
  // Poster items live in the poster store; video items are animations, whose
  // still frame is the right thing to show in a review list (the mp4 would cost
  // megabytes per row).
  const previewUrl =
    item.gen_id == null
      ? null
      : isVideo
        ? api.yasProducer.animation.frameUrl(item.gen_id, item.updated_at)
        : api.yasProducer.posterOnly.fileUrl(item.gen_id, item.updated_at);

  const approve = async () => {
    setBusy("approve");
    try {
      await api.campaigns.approve(item.id, {
        caption: caption.trim() !== (item.caption ?? "").trim() ? caption.trim() : undefined,
        proposed_time: when !== toLocalInput(item.proposed_time) ? fromLocalInput(when) : undefined,
      });
      toast.success("Approved and scheduled",
        "It's in Library → Scheduled. You still press Post to publish it.");
      await onDecided(item.id);
    } catch (e: any) {
      toast.error("Couldn't approve", String(e?.message || e).replace(/^\d{3}\s+/, ""));
    } finally {
      setBusy(null);
    }
  };

  const decline = async () => {
    setBusy("decline");
    try {
      await api.campaigns.decline(item.id, { note: note.trim() || undefined, regenerate });
      toast.info(regenerate ? "Making another one" : "Declined",
        regenerate
          ? "It'll come back for review with your note taken into account."
          : "This slot is closed.");
      await onDecided(item.id);
    } catch (e: any) {
      toast.error("Couldn't decline", String(e?.message || e).replace(/^\d{3}\s+/, ""));
    } finally {
      setBusy(null);
      setDeclining(false);
    }
  };

  const title = item.idea_title || item.idea?.title || `${item.kind} for review`;

  return (
    <div className="card p-3">
      <div className="flex items-start gap-3">
        {/* Preview. Uses the shared Thumb so a thumbnail outage degrades to the
            full-resolution proxy instead of an empty box. */}
        <div className="w-20 h-24 shrink-0 rounded-lg overflow-hidden bg-bg-muted">
          <Thumb
            src={previewUrl}
            alt=""
            className="w-full h-full"
            empty={
              <div className="w-full h-full grid place-items-center text-3xs text-muted">
                no preview
              </div>
            }
          />
        </div>

        <div className="flex-1 min-w-0 space-y-1.5">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="pill bg-accent/15 text-accent">needs review</span>
            <span className="text-2xs text-muted capitalize">{item.kind}</span>
            {item.campaign_name && (
              <span className="text-2xs text-muted truncate">{item.campaign_name}</span>
            )}
            {item.attempt > 1 && (
              <span className="text-2xs text-muted">attempt {item.attempt}</span>
            )}
          </div>

          <p className="text-sm font-medium text-fg truncate">{title}</p>

          <textarea
            className="textarea w-full text-[12.5px] leading-relaxed"
            rows={3}
            value={caption}
            onChange={(e) => setCaption(e.target.value)}
            placeholder="Caption…"
            disabled={busy !== null}
            aria-label="Caption"
          />

          <div className="flex items-center gap-2 flex-wrap">
            <label className="text-2xs text-muted">Posts</label>
            <input
              type="datetime-local"
              className="input text-[11.5px] py-1"
              value={when}
              onChange={(e) => setWhen(e.target.value)}
              disabled={busy !== null}
              aria-label="Scheduled time"
            />
            <span className="text-[10.5px] text-muted">
              {prettyTime(item.proposed_time)} (Asia/Dubai)
            </span>
          </div>
        </div>

        <div className="shrink-0 flex flex-col gap-1.5">
          <button
            className="btn btn-accent text-xs whitespace-nowrap"
            onClick={approve}
            disabled={busy !== null}
          >
            {busy === "approve"
              ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Approving</>
              : "Approve →"}
          </button>
          <button
            className="btn btn-ghost text-xs"
            onClick={() => setDeclining((v) => !v)}
            disabled={busy !== null}
          >
            Decline
          </button>
        </div>
      </div>

      {declining && (
        <div className="mt-3 pt-3 border-t border-border space-y-2">
          <label className="block text-2xs text-muted" htmlFor={`note-${item.id}`}>
            What's wrong with it? This shapes the next attempt and future weeks.
          </label>
          <textarea
            id={`note-${item.id}`}
            className="textarea w-full text-[12.5px]"
            rows={2}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="e.g. too busy, and the headline buries the point"
            disabled={busy !== null}
          />
          <div className="flex items-center gap-3 flex-wrap">
            <label className="flex items-center gap-1.5 text-[11.5px] text-fg cursor-pointer">
              <input
                type="checkbox"
                checked={regenerate}
                onChange={(e) => setRegenerate(e.target.checked)}
                disabled={busy !== null}
              />
              Make another one for this slot
            </label>
            <button
              className="btn btn-danger text-xs ml-auto"
              onClick={decline}
              disabled={busy !== null}
            >
              {busy === "decline"
                ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Declining</>
                : regenerate ? "Decline and remake" : "Decline"}
            </button>
            <button
              className="btn btn-ghost text-xs"
              onClick={() => setDeclining(false)}
              disabled={busy !== null}
            >
              Keep
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
