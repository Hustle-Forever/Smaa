/**
 * Create a campaign.
 *
 * Portaled to document.body with an ESC handler and a body scroll lock, copying
 * AssetPickerModal — required, not stylistic: the page-transition animation
 * leaves a CSS `transform` on the page wrapper, which makes it the containing
 * block for `position: fixed`, so a non-portaled overlay would be clipped to the
 * content column instead of covering the viewport.
 *
 * The cost estimate is computed here from the same provider rates the backend
 * uses, so the number moves as the user drags the quotas — committing to a
 * weekly spend deserves to be visible before pressing Create, not after.
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { useModalChrome } from "../../lib/useModalChrome";
import { createPortal } from "react-dom";
import { Loader2, X } from "lucide-react";

import { useToast } from "../ToastProvider";
import { api } from "../../lib/api";
import type { Campaign, CampaignPostingWindow } from "../../lib/types";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const STYLES: { key: string; label: string; hint: string }[] = [
  { key: "voxel", label: "Voxel", hint: "3-D blocky scenes — the house look" },
  { key: "pattern", label: "Pattern", hint: "flat graphic, bold repeating motifs" },
  { key: "popup", label: "Pop-up", hint: "paper-craft, layered depth" },
];

// Provider rates, mirroring backend/app/services/vertex_client.py. If those move,
// this and routers/campaigns._cycle_cost_estimate move together.
const IMAGE_USD = 0.13;              // gemini-3-pro-image (Nano Banana Pro)
const VEO_VIDEO_USD_PER_SEC = 0.10;  // veo-3.1-fast
const VEO_AUDIO_USD_PER_SEC = 0.075;
const LLM_USD = 0.05;                // planning + captions, rounded up

/** Sensible default slots: weekdays, late afternoon, spread out. */
function defaultWindows(count: number): CampaignPostingWindow[] {
  const order = [0, 2, 4, 1, 3, 5, 6];
  return Array.from({ length: Math.max(1, count) }, (_, i) => ({
    weekday: order[i % order.length],
    time: "17:00",
  }));
}

interface Props {
  onClose: () => void;
  onCreated: (c: Campaign) => void | Promise<void>;
}

export function CampaignWizard({ onClose, onCreated }: Props) {
  const [name, setName] = useState("");
  const [topic, setTopic] = useState("");
  const [posters, setPosters] = useState(3);
  const [videos, setVideos] = useState(0);
  const [ongoing, setOngoing] = useState(true);
  const [weeks, setWeeks] = useState(4);
  const [style, setStyle] = useState("voxel");
  const [videoSecs, setVideoSecs] = useState(6);
  const [withAudio, setWithAudio] = useState(true);
  const [budget, setBudget] = useState<string>("");
  const [notify, setNotify] = useState(true);
  const [windows, setWindows] = useState<CampaignPostingWindow[]>(defaultWindows(3));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  // ESC closes + body scroll lock (verbatim from AssetPickerModal).
  // Escape, scroll lock, focus trap and focus restore in one place. The first
  // two were already here; the trap is the new part -- without it Tab walked
  // straight out of the dialog and into the page behind the overlay, which
  // makes a dialog modal only to a mouse.
  const panelRef = useRef<HTMLDivElement>(null);
  const { trapTab } = useModalChrome({ onClose, panelRef });

  const total = posters + videos;

  // Keep the slot count in step with the quota until the user edits slots by
  // hand, so the common path needs no thought.
  const [slotsTouched, setSlotsTouched] = useState(false);
  useEffect(() => {
    if (!slotsTouched) setWindows(defaultWindows(total));
  }, [total, slotsTouched]);

  const perCycle = useMemo(() => {
    const perSec = VEO_VIDEO_USD_PER_SEC + (withAudio ? VEO_AUDIO_USD_PER_SEC : 0);
    return posters * IMAGE_USD + videos * (IMAGE_USD + videoSecs * perSec) + LLM_USD;
  }, [posters, videos, videoSecs, withAudio]);

  const submit = async () => {
    if (!name.trim()) { setError("Give the campaign a name."); return; }
    if (total === 0) { setError("Set at least one poster or video per week."); return; }
    setSaving(true);
    setError(null);
    try {
      const created = await api.campaigns.create({
        name: name.trim(),
        topic_brief: topic.trim(),
        posters_per_week: posters,
        videos_per_week: videos,
        duration_weeks: ongoing ? null : weeks,
        posting_windows: windows,
        poster_style: style,
        video_duration_sec: videoSecs,
        video_with_audio: withAudio,
        weekly_budget_usd: budget.trim() ? Number(budget) : null,
        notify_email: notify,
        start_now: true,
      });
      await onCreated(created);
    } catch (e: any) {
      const msg = String(e?.message || e).replace(/^\d{3}\s+/, "");
      setError(msg);
      toast.error("Couldn't create the campaign", msg);
    } finally {
      setSaving(false);
    }
  };

  return createPortal(
    <div
      style={{
        position: "fixed", inset: 0, zIndex: 110,
        background: "rgba(0,0,0,0.75)", backdropFilter: "blur(2px)",
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: "16px",
      }}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="New campaign"
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        onKeyDown={trapTab}
        onClick={(e) => e.stopPropagation()}
        className="relative bg-bg rounded-2xl shadow-2xl border border-border overflow-hidden flex flex-col"
        style={{ width: "min(640px, 100%)", maxHeight: "min(880px, 92vh)" }}
      >
        <header className="shrink-0 px-4 py-3 border-b border-border flex items-center gap-2">
          <div className="flex-1 min-w-0">
            <h2 className="text-sm font-semibold text-fg">New campaign</h2>
            <p className="text-2xs text-muted">
              It produces on a weekly rhythm; you approve everything before it posts.
            </p>
          </div>
          <button
            onClick={onClose}
            className="w-7 h-7 rounded-full bg-bg-muted grid place-items-center text-muted hover:text-fg"
            aria-label="Close"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </header>

        <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-4">
          <div className="space-y-1.5">
            <label className="block text-[11.5px] font-medium text-fg" htmlFor="cw-name">
              Name
            </label>
            <input
              id="cw-name"
              className="input w-full"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Cyber Security Week"
              autoFocus
            />
          </div>

          <div className="space-y-1.5">
            <label className="block text-[11.5px] font-medium text-fg" htmlFor="cw-topic">
              What is it about?
            </label>
            <textarea
              id="cw-topic"
              className="textarea w-full text-[13px] leading-relaxed"
              rows={3}
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="Cyber security awareness for students — phishing, passwords, 2FA, public wifi. Practical, a bit playful, never fear-mongering."
            />
            <p className="text-2xs text-muted leading-relaxed">
              This is the brief the campaign invents each week's ideas from. The more
              specific about angle and tone, the less you'll decline.
            </p>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <Stepper label="Posters a week" value={posters} onChange={setPosters} max={14} />
            <Stepper label="Videos a week" value={videos} onChange={setVideos} max={14} />
          </div>

          {videos > 0 && (
            <div className="flex items-center gap-3 flex-wrap pl-1">
              <label className="text-[11.5px] text-muted">Video length</label>
              {[4, 6, 8].map((s) => (
                <button
                  key={s}
                  onClick={() => setVideoSecs(s)}
                  className={[
                    "px-2.5 py-1 rounded-lg border text-[12px] font-medium transition-colors",
                    videoSecs === s
                      ? "border-accent bg-accent/10 text-fg"
                      : "border-border bg-bg-subtle text-muted hover:bg-bg-muted",
                  ].join(" ")}
                >
                  {s}s
                </button>
              ))}
              <label className="flex items-center gap-1.5 text-[11.5px] text-fg cursor-pointer ml-1">
                <input
                  type="checkbox"
                  checked={withAudio}
                  onChange={(e) => setWithAudio(e.target.checked)}
                />
                With sound
              </label>
            </div>
          )}

          <div className="space-y-1.5">
            <span className="block text-[11.5px] font-medium text-fg">Look</span>
            <div className="grid grid-cols-3 gap-2">
              {STYLES.map((s) => (
                <button
                  key={s.key}
                  onClick={() => setStyle(s.key)}
                  className={[
                    "rounded-lg border p-2.5 text-left transition-colors",
                    style === s.key
                      ? "border-accent bg-accent/8"
                      : "border-border bg-bg-subtle hover:bg-bg-muted",
                  ].join(" ")}
                >
                  <span className="block text-[12.5px] font-medium text-fg">{s.label}</span>
                  <span className="block text-[10.5px] text-muted leading-snug mt-0.5">
                    {s.hint}
                  </span>
                </button>
              ))}
            </div>
          </div>

          {/* Posting slots */}
          <div className="space-y-1.5">
            <span className="block text-[11.5px] font-medium text-fg">
              When to post ({windows.length} slot{windows.length === 1 ? "" : "s"})
            </span>
            <div className="space-y-1.5">
              {windows.map((w, i) => (
                <div key={i} className="flex items-center gap-1.5">
                  <div className="flex gap-1 flex-wrap flex-1">
                    {WEEKDAYS.map((d, wd) => (
                      <button
                        key={d}
                        onClick={() => {
                          setSlotsTouched(true);
                          setWindows((prev) =>
                            prev.map((p, j) => (j === i ? { ...p, weekday: wd } : p)));
                        }}
                        className={[
                          "px-2 py-1 rounded text-2xs font-medium transition-colors",
                          w.weekday === wd
                            ? "bg-accent/15 text-accent"
                            : "bg-bg-muted text-muted hover:text-fg",
                        ].join(" ")}
                      >
                        {d}
                      </button>
                    ))}
                  </div>
                  <input
                    type="time"
                    className="input text-[11.5px] py-1 w-[92px]"
                    value={w.time}
                    onChange={(e) => {
                      setSlotsTouched(true);
                      setWindows((prev) =>
                        prev.map((p, j) => (j === i ? { ...p, time: e.target.value } : p)));
                    }}
                    aria-label={`Slot ${i + 1} time`}
                  />
                </div>
              ))}
            </div>
            <p className="text-2xs text-muted">Times are Asia/Dubai.</p>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <span className="block text-[11.5px] font-medium text-fg">Run for</span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setOngoing(true)}
                  className={[
                    "px-2.5 py-1 rounded-lg border text-[12px] font-medium",
                    ongoing ? "border-accent bg-accent/10 text-fg"
                      : "border-border bg-bg-subtle text-muted",
                  ].join(" ")}
                >
                  Until I stop it
                </button>
                <button
                  onClick={() => setOngoing(false)}
                  className={[
                    "px-2.5 py-1 rounded-lg border text-[12px] font-medium",
                    !ongoing ? "border-accent bg-accent/10 text-fg"
                      : "border-border bg-bg-subtle text-muted",
                  ].join(" ")}
                >
                  {weeks} weeks
                </button>
              </div>
              {!ongoing && (
                <input
                  type="range"
                  min={1}
                  max={26}
                  value={weeks}
                  onChange={(e) => setWeeks(Number(e.target.value))}
                  className="w-full"
                  aria-label="Weeks"
                />
              )}
            </div>

            <div className="space-y-1.5">
              <label className="block text-[11.5px] font-medium text-fg" htmlFor="cw-budget">
                Weekly cap (optional)
              </label>
              <input
                id="cw-budget"
                className="input w-full"
                inputMode="decimal"
                value={budget}
                onChange={(e) => setBudget(e.target.value.replace(/[^\d.]/g, ""))}
                placeholder="e.g. 10"
              />
              <p className="text-2xs text-muted leading-relaxed">
                It pauses itself if a week costs more than this.
              </p>
            </div>
          </div>

          <label className="flex items-start gap-2 text-[12px] text-fg cursor-pointer">
            <input
              type="checkbox"
              checked={notify}
              onChange={(e) => setNotify(e.target.checked)}
              className="mt-0.5"
            />
            <span>
              Email me when there's something to review
              <span className="block text-2xs text-muted">
                Needs SMTP configured; otherwise items just wait here.
              </span>
            </span>
          </label>

          <div className="rounded-lg border border-border bg-bg-muted/40 px-3 py-2.5 space-y-1">
            <div className="flex items-baseline gap-2">
              <span className="text-[11.5px] text-muted">Roughly</span>
              <span className="text-base font-semibold text-fg tabular-nums">
                ${perCycle.toFixed(2)}
              </span>
              <span className="text-[11.5px] text-muted">a week</span>
            </div>
            <p className="text-2xs text-muted leading-relaxed">
              {posters > 0 && `${posters} image${posters === 1 ? "" : "s"}`}
              {posters > 0 && videos > 0 && " + "}
              {videos > 0 && `${videos} video${videos === 1 ? "" : "s"} (${videoSecs}s${withAudio ? " with sound" : ""})`}
              {" at current rates. Declining something makes a replacement, so a week you "}
              reject a lot can cost more.
            </p>
          </div>

          {error && <p className="text-[12px] text-danger">{error}</p>}
        </div>

        <footer className="shrink-0 px-4 py-3 border-t border-border bg-bg-muted/50 flex items-center gap-3">
          <p className="text-2xs text-muted flex-1">
            You approve every post before it goes out.
          </p>
          <button className="btn btn-ghost text-sm" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button className="btn btn-accent text-sm" onClick={submit} disabled={saving}>
            {saving
              ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Creating</>
              : "Create campaign"}
          </button>
        </footer>
      </div>
    </div>,
    document.body,
  );
}

function Stepper({
  label, value, onChange, max,
}: { label: string; value: number; onChange: (n: number) => void; max: number }) {
  return (
    <div className="space-y-1.5">
      <span className="block text-[11.5px] font-medium text-fg">{label}</span>
      <div className="flex items-center gap-1.5">
        <button
          className="btn btn-ghost text-sm w-8 justify-center"
          onClick={() => onChange(Math.max(0, value - 1))}
          disabled={value <= 0}
          aria-label={`One fewer ${label}`}
        >
          −
        </button>
        <span className="w-8 text-center text-sm font-semibold text-fg tabular-nums">
          {value}
        </span>
        <button
          className="btn btn-ghost text-sm w-8 justify-center"
          onClick={() => onChange(Math.min(max, value + 1))}
          disabled={value >= max}
          aria-label={`One more ${label}`}
        >
          +
        </button>
      </div>
    </div>
  );
}
