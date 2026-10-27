/**
 * Final step: background music under the finished reel.
 *
 * The user describes a vibe (or taps one), Suno makes an instrumental track
 * (paid: only "Make music" asks for one), and the server lays it under the
 * whole reel, intro to outro, ducked under the speaker. The reel itself never
 * changes: "With music | Without music" switches between it and its music
 * version, and what is switched on is what Continue, Save and the editor use
 * (run.music: services/th_reel_music.public_state).
 *
 * The creator polls the run while a track is composed or mixed
 * (lib/thMusic.musicBusy); this panel only reads run.music and sends the
 * presses.
 */
import { useEffect, useRef, useState } from "react";
import { Loader2, Music } from "lucide-react";

import { api } from "../../lib/api";
import { clock } from "../../lib/thMusic";
import type { ReelMusicLevel, TalkingHeadRun } from "../../lib/types";
import { useToast } from "../ToastProvider";

// One tap fills the description (nothing is made until "Make music").
const VIBES: { label: string; brief: string }[] = [
  { label: "Slow jazz piano", brief: "slow jazz piano, warm and calm" },
  { label: "Corporate", brief: "light corporate, optimistic piano and soft strings" },
  { label: "Upbeat pop", brief: "upbeat pop, bright synths and claps, feel-good" },
  { label: "Cinematic", brief: "cinematic strings, slowly building, inspiring" },
  { label: "Lo-fi", brief: "lo-fi beat, mellow keys, soft and relaxed" },
  { label: "Arabic oud", brief: "Arabic oud with soft percussion, elegant and modern" },
];

const LEVELS: { value: ReelMusicLevel; label: string }[] = [
  { value: "soft", label: "Soft" },
  { value: "medium", label: "Medium" },
  { value: "loud", label: "Loud" },
];

// The server's limit (th_reel_music.BRIEF_MAX).
const BRIEF_MAX = 200;

type Busy = null | "make" | "on" | "off" | "level" | "remove";

function serverWords(e: unknown): string {
  return String((e as { message?: string })?.message ?? e ?? "").replace(/^\d{3}\s+/, "");
}

// A segmented control's button: theme tokens only, pressed state for AT.
function segClass(active: boolean): string {
  return [
    "flex-1 rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
    "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40",
    "disabled:opacity-50 disabled:cursor-not-allowed",
    active ? "bg-bg-subtle text-fg shadow-soft" : "text-muted hover:text-fg",
  ].join(" ");
}

export function TalkingHeadV2MusicPanel({
  run,
  onRun,
}: {
  run: TalkingHeadRun;
  onRun: (r: TalkingHeadRun) => void;
}) {
  const toast = useToast();
  const m = run.music;
  const [draft, setDraft] = useState("");
  // "Try another vibe": the description again, the current music kept until
  // the new track is ready.
  const [composing, setComposing] = useState(false);
  const [busy, setBusy] = useState<Busy>(null);

  // Say so when a track lands or fails (it may take minutes; the user may
  // have scrolled away from the panel).
  const lastStatus = useRef(m?.track_status ?? null);
  useEffect(() => {
    const was = lastStatus.current;
    const now = m?.track_status ?? null;
    lastStatus.current = now;
    if (was !== "generating" || now === "generating") return;
    if (now === "ready") {
      toast.info("Your music is ready", "It's under the whole video now. Switch it off any time.");
    } else if (now === "failed") {
      toast.error("Suno couldn't make the music", m?.error ?? "Try again.");
      // The vibe stays in the box for Try again.
      setDraft((d) => d || m?.brief || "");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [m?.track_status]);

  if (!m) return null; // an older backend: no music

  const generating = m.track_status === "generating";
  const failed = m.track_status === "failed";
  const view: "generating" | "compose" | "ready" = generating
    ? "generating"
    : composing || !m.has_track
      ? "compose"
      : "ready";
  const reelLen = m.reel_sec ? clock(m.reel_sec) : null;
  const trimmed = draft.trim();
  const retrying = failed && !!m.brief && trimmed === m.brief;

  async function make(brief: string) {
    const b = brief.trim();
    if (!b || busy) return;
    setBusy("make");
    try {
      onRun(await api.yasProducer.talkingHeadV2.music(run.id, b));
      setComposing(false);
      setDraft("");
    } catch (e) {
      toast.error("Couldn't start the music", serverWords(e));
      api.yasProducer.talkingHeadV2.fetch(run.id).then(onRun).catch(() => {
        // the next poll or press reads it again
      });
    } finally {
      setBusy(null);
    }
  }

  async function apply(kind: Exclude<Busy, null | "make">, body: { on: boolean; level?: ReelMusicLevel; remove?: boolean }) {
    if (busy) return;
    setBusy(kind);
    try {
      onRun(await api.yasProducer.talkingHeadV2.musicApply(run.id, body));
      if (kind === "remove") {
        setComposing(false);
        toast.info("Music removed", "The video is back to its own sound.");
      }
    } catch (e) {
      toast.error("Couldn't change the music", serverWords(e));
      api.yasProducer.talkingHeadV2.fetch(run.id).then(onRun).catch(() => {
        // the next poll or press reads it again
      });
    } finally {
      setBusy(null);
    }
  }

  const header = (
    <div className="flex items-center gap-2">
      <Music className="w-4 h-4 text-accent shrink-0" aria-hidden />
      <h3 id={`music-title-${run.id}`} className="text-sm font-semibold text-fg">Background music</h3>
      {view === "compose" && composing && m.has_track && (
        <button
          type="button"
          onClick={() => setComposing(false)}
          className="ml-auto text-2xs text-muted hover:text-fg underline-offset-2 hover:underline"
        >
          Keep the current music
        </button>
      )}
    </div>
  );

  return (
    <section
      aria-labelledby={`music-title-${run.id}`}
      className="card w-full max-w-md p-4 space-y-3 text-left"
    >
      {header}

      {!m.available && view !== "ready" ? (
        <p className="text-xs text-muted">Background music isn't set up on this server yet.</p>
      ) : view === "generating" ? (
        <div role="status" aria-live="polite" className="flex items-start gap-3">
          <Loader2 className="w-5 h-5 mt-0.5 text-accent animate-spin shrink-0" aria-hidden />
          <div className="min-w-0 space-y-1">
            <p className="text-sm font-medium text-fg">Composing your track…</p>
            <p className="text-xs text-muted">Usually 1–3 minutes. You can leave this page; it carries on.</p>
            {m.brief && <p className="text-xs text-fg break-words">“{m.brief}”</p>}
            {m.has_track && (
              <p className="text-2xs text-muted">The current music stays on until the new track is ready.</p>
            )}
          </div>
        </div>
      ) : view === "compose" ? (
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            void make(draft);
          }}
        >
          <div className="space-y-1.5">
            <label htmlFor={`music-brief-${run.id}`} className="block text-xs font-medium text-fg">
              Describe the music
            </label>
            <input
              id={`music-brief-${run.id}`}
              type="text"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              maxLength={BRIEF_MAX}
              placeholder="slow jazz piano, warm and calm"
              disabled={busy !== null}
              className="input text-sm py-2"
              autoComplete="off"
            />
          </div>
          <div role="group" aria-label="Vibe ideas" className="flex flex-wrap gap-1.5">
            {VIBES.map((v) => {
              const active = trimmed === v.brief;
              return (
                <button
                  key={v.label}
                  type="button"
                  onClick={() => setDraft(v.brief)}
                  disabled={busy !== null}
                  aria-pressed={active}
                  title={v.brief}
                  className={[
                    "rounded-full border px-2.5 py-1 text-2xs transition-colors disabled:opacity-50",
                    "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40",
                    active ? "border-accent bg-accent/10 text-accent" : "border-border text-muted hover:text-fg hover:border-border-strong",
                  ].join(" ")}
                >
                  {v.label}
                </button>
              );
            })}
          </div>
          {failed && (
            <p role="alert" className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger break-words">
              Suno couldn't make it: {m.error || "unknown error"}
            </p>
          )}
          <button
            type="submit"
            disabled={!trimmed || busy !== null}
            className="btn btn-accent w-full"
          >
            {busy === "make" ? (
              <Loader2 className="w-4 h-4 animate-spin" aria-hidden />
            ) : (
              <Music className="w-4 h-4" aria-hidden />
            )}
            {busy === "make"
              ? "Asking Suno…"
              : `${retrying ? "Try again" : "Make music"}${reelLen ? ` for the full ${reelLen}` : ""}`}
          </button>
          <p className="text-2xs text-muted text-center">Made with Suno · uses Suno credits</p>
        </form>
      ) : (
        <div className="space-y-3">
          {m.track_brief && (
            <p className="text-xs text-muted break-words">
              Vibe: <span className="text-fg">“{m.track_brief}”</span>
            </p>
          )}
          <div
            role="group"
            aria-label="Music on the video"
            className="flex gap-0.5 rounded-lg border border-border bg-bg-muted p-0.5"
          >
            <button
              type="button"
              aria-pressed={m.on}
              disabled={busy !== null}
              onClick={() => !m.on && void apply("on", { on: true, level: m.level })}
              className={segClass(m.on)}
            >
              With music
            </button>
            <button
              type="button"
              aria-pressed={!m.on}
              disabled={busy !== null}
              onClick={() => m.on && void apply("off", { on: false })}
              className={segClass(!m.on)}
            >
              Without music
            </button>
          </div>
          {m.on && (
            <div className="flex items-center gap-3">
              <span id={`music-level-${run.id}`} className="text-xs text-muted shrink-0">Level</span>
              <div
                role="group"
                aria-labelledby={`music-level-${run.id}`}
                className="flex flex-1 gap-0.5 rounded-lg border border-border bg-bg-muted p-0.5"
              >
                {LEVELS.map((l) => (
                  <button
                    key={l.value}
                    type="button"
                    aria-pressed={m.level === l.value}
                    disabled={busy !== null}
                    onClick={() => m.level !== l.value && void apply("level", { on: true, level: l.value })}
                    className={segClass(m.level === l.value)}
                  >
                    {l.label}
                  </button>
                ))}
              </div>
            </div>
          )}
          {m.on && (m.mixing || busy === "on" || busy === "level") && (
            <p role="status" className="inline-flex items-center gap-1.5 text-2xs text-muted">
              <Loader2 className="w-3 h-3 animate-spin text-accent" aria-hidden />
              Putting the music under the video…
            </p>
          )}
          {m.on && m.mix_error && busy === null && (
            <p role="alert" className="text-xs text-danger break-words">
              {m.mix_error}{" "}
              <button
                type="button"
                onClick={() => void apply("on", { on: true, level: m.level })}
                className="font-semibold underline-offset-2 hover:underline"
              >
                Try again
              </button>
            </p>
          )}
          {m.on && !m.applied && !m.mixing && !m.mix_error && busy === null && (
            <button
              type="button"
              onClick={() => void apply("on", { on: true, level: m.level })}
              className="btn btn-ghost btn-sm"
            >
              Put the music back on
            </button>
          )}
          {failed && (
            <p role="alert" className="text-xs text-danger break-words">
              The new track failed: {m.error || "unknown error"}{" "}
              <button
                type="button"
                onClick={() => void make(m.brief ?? "")}
                disabled={!m.brief || busy !== null}
                className="font-semibold underline-offset-2 hover:underline disabled:opacity-50"
              >
                Try again
              </button>
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <button
              type="button"
              onClick={() => {
                setDraft("");
                setComposing(true);
              }}
              disabled={busy !== null || !m.available}
              className="btn btn-ghost btn-sm"
            >
              Try another vibe
            </button>
            <button
              type="button"
              onClick={() => void apply("remove", { on: false, remove: true })}
              disabled={busy !== null}
              className="btn btn-ghost btn-sm text-danger"
            >
              {busy === "remove" ? "Removing…" : "Remove music"}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
