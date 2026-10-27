// Rebrand — sits under the final Talking Head v2 video.
//
// The whole reel gets up to two arms per run:
//   • 42 Brand frame (the primary, default-on option) — NOTHING is regenerated.
//     Remotion draws only the 42 graphics (violet slabs, bead strings, the
//     owner's patterns sliding left to right, logo, topic icons, optional
//     heading) on a transparent canvas and ffmpeg lays them over the clips in
//     one pass; wipes / glitch cuts hide the hard cuts. It needs only
//     Remotion, so it is never gated on the Omni capability.
//   • Experimental — regenerates pixels (default off): Gemini Omni Flash edits
//     each talking-head clip (branded background + icons + the picked brand
//     pattern, speaker kept, original audio re-muxed) and generates the
//     intro→reel / reel→outro transitions from the exact boundary frames. It
//     works from a brief (Instagram brand DNA + past rebrand lessons + this
//     reel's intro/outro).
// The results play side by side; stars + notes feed the next brief (the RAG
// loop that learns from its own results).
//
// Veo is no longer offered (the owner's call; the server ignores run_veo and
// the panel always sends false). Older experiments whose Veo arm ran still get
// their Veo card and stars, so history reads back as it was.
//
// It could also rebrand ONE clip (the `target` prop): a scene (Omni edit), or
// the intro/outro card with its branded transition into/out of the speaker.
// Nothing opens it any more: every clip is rebranded on the Rebrand stage
// (rebrand-stage/RebrandStage); old Omni runs stay in the database.
// The brand frame is a whole-reel treatment: a clip run neither offers it nor
// sends any of its fields. The brand pattern (RebrandPatternPicker) is Omni's,
// so it goes with every run that has Omni on.
//
// Omni is switched off for new work (the owner, 2026-09-30: the server's
// capabilities say `omni.switched_off`, and it refuses an Omni run): the reel
// offers the brand frame only, and a clip opens on its earlier Omni runs
// (still played, rated and downloaded) with a plain note on what to use
// instead — the Rebrand studio for a scene (made in house, free), the brand
// frame on the whole reel for the intro and outro cards.

import { useEffect, useMemo, useState } from "react";

import {
  REBRAND_ACTIVE,
  rebrandApi,
  type BrandResolution,
  type BrandSeam,
  type BrandTextFit,
  type BrandTransitions,
  type RebrandArm,
  type RebrandArmName,
  type RebrandCapabilities,
  type RebrandExperiment,
  type RebrandIntensity,
  type RebrandOptions,
  type RebrandTarget,
  type RebrandWinner,
} from "../../lib/rebrandApi";
import { usePoll } from "../../lib/usePoll";
import { useToast } from "../ToastProvider";
import { Spinner } from "../ui/Spinner";
import { DEFAULT_REBRAND_PATTERN, RebrandPatternPicker } from "./RebrandPatternPicker";
import { SegmentedControl } from "./SegmentedControl";

/** Whole reel: the brand frame is the answer and Omni is an opt-in
 *  experiment. A single clip runs Omni and carries no brand fields at all.
 *  Veo is never asked for (retired); its two settings keep their old values
 *  only because the body's shape still has them. */
function defaultOptions(target: RebrandTarget): RebrandOptions {
  const base: RebrandOptions = {
    target,
    run_omni: target !== "reel",
    run_veo: false,
    intensity: "balanced",
    notes: "",
    veo_scene_mode: "keep",
    scene_transitions: false,
    transition_sec: 1.6,
    veo_quality: "normal",
    pattern_on: true,
    pattern: DEFAULT_REBRAND_PATTERN,
    pattern_animation: null,
  };
  if (target !== "reel") return base;
  return {
    ...base,
    run_brand: true,
    brand_transitions: "auto",
    brand_logo_on_cards: false,
    brand_resolution: "1080",
    brand_icons: true,
    brand_edge_patterns: true,
  };
}

/** Every option only the brand arm reads — stripped from a clip run's body. */
const BRAND_KEYS = [
  "run_brand",
  "brand_transitions",
  "brand_heading",
  "brand_sub",
  "brand_logo_on_cards",
  "brand_resolution",
  "brand_icons",
  "brand_edge_patterns",
] as const;

const BRAND_TRANSITIONS: { value: BrandTransitions; label: string; help: string }[] = [
  {
    value: "auto",
    label: "Auto",
    help: "Slab wipe into and out of the intro/outro cards; a glitch cut between scenes.",
  },
  {
    value: "glitch",
    label: "Glitch on every cut",
    help: "The frame tears into slices and splits red/cyan for a third of a second, the next shot glitching in slice by slice.",
  },
  {
    value: "cover",
    label: "Slab wipe on every cut",
    help: "A violet slab sweeps across the whole frame and hides every cut.",
  },
  {
    value: "handoff",
    label: "Hand-off",
    help: "The slabs slide out along their 45° edge and the next shot's slabs slide in.",
  },
  {
    value: "short",
    label: "Short wipe",
    help: "A quicker slab wipe (a third of a second) on every cut.",
  },
];

const SEAM_STYLE_LABEL: Record<BrandSeam["style"], [string, string]> = {
  glitch: ["glitch cut", "glitch cuts"],
  cover: ["slab wipe", "slab wipes"],
  handoff: ["hand-off", "hand-offs"],
  short: ["short wipe", "short wipes"],
};

/** How much text the frame holds (services/remotion brand42/textFit.ts),
 *  measured by rendering the real composition — its own fit report — on the
 *  tightest TH v2 frame, a 1:1 picture band (text in the bottom bar beside the
 *  slab); a 3:2 band also has room in the top bar under the band, as in r128.
 *  The same at 720p and 1080p:
 *  - heading (VG5000, up to 2 lines, shrinks to 40%): one short word at full
 *    size ("Myth"); ~20 characters on two lines ("Why I Chose 42 Abu Dhabi"
 *    0.42); 24+ depends on the letters ("Is coding really for me?" doesn't
 *    fit) and 29 never does;
 *  - sub-line (Jost, up to 3 lines, shrinks to 60%): ~90 characters; 100 never.
 *  The frame never cuts text: a part that can't fit is left OUT (a heading
 *  that doesn't fit costs only the heading, never the sub-line), and the
 *  result card says exactly what happened (the arm's `text_fit`). Character
 *  counts only approximate glyph widths, so the hints say "about". */
const BRAND_TEXT_FIT = {
  /** Input cap for the heading (the server itself takes 40). */
  headingMax: 32,
  headingFull: 8,
  headingFits: 20,
  /** Input cap for the sub-line (= the server's). */
  subMax: 140,
  subFits: 80,
  subTight: 90,
} as const;

type FitLevel = "ok" | "tight" | "over";

function headingFit(len: number): FitLevel {
  if (len <= BRAND_TEXT_FIT.headingFits) return "ok";
  return len <= BRAND_TEXT_FIT.headingFits + 4 ? "tight" : "over";
}

function subFit(len: number): FitLevel {
  if (len <= BRAND_TEXT_FIT.subFits) return "ok";
  return len <= BRAND_TEXT_FIT.subTight ? "tight" : "over";
}

const FIT_TONE: Record<FitLevel, string> = {
  ok: "text-muted",
  tight: "text-amber-700",
  over: "text-danger",
};

/** The hint line under the heading / sub-line inputs. */
function brandTextHint(which: "heading" | "sub", text: string): { level: FitLevel; msg: string } {
  const len = text.trim().length;
  if (which === "heading") {
    const level = headingFit(len);
    return {
      level,
      msg:
        level === "ok"
          ? len <= BRAND_TEXT_FIT.headingFull
            ? "One short word reads best, like the reels' Myth / Fact."
            : "Fits: it wraps to two lines and shrinks. Shorter reads bigger."
          : level === "tight"
            ? "Tight: about 20 characters fit, and wide letters (M, W) take more room."
            : "Probably too long — a heading that doesn't fit is left out (the sub-line stays).",
    };
  }
  const level = subFit(len);
  return {
    level,
    msg:
      level === "ok"
        ? "About 80–90 characters fit on up to three lines."
        : level === "tight"
          ? "Tight: it shrinks to its smallest size to fit three lines."
          : "Probably too long for three lines — a sub-line that doesn't fit is left out of the video.",
  };
}

/** Short names for toasts, history chips and the winner picker. */
const ARM_SHORT: Record<RebrandArmName, string> = {
  brand: "Brand frame",
  omni: "Omni",
  veo: "Veo",
};

function armMeta(name: RebrandArmName, target: RebrandTarget): { title: string; blurb: string } {
  const isScene = target.startsWith("scene_");
  const isCard = target === "intro" || target === "outro";
  if (name === "brand") {
    return {
      title: "42 Brand frame",
      blurb: "Your footage (never regenerated) inside 42's violet slabs, sliding patterns, topic icons and logo; wipes and glitch cuts hide the cuts.",
    };
  }
  if (name === "omni") {
    return {
      title: "Gemini Omni Flash",
      blurb: isScene
        ? "Video-to-video edit of this clip: branded background + icons, speaker and voice kept."
        : isCard
          ? `Restyles the ${target} card (text + logo kept) and generates its branded transition ${target === "intro" ? "into" : "out of"} the speaker.`
          : "Video-to-video edit: branded background + icons on every scene, generated transitions.",
    };
  }
  // Veo: only ever shown for an older experiment whose Veo arm ran.
  return {
    title: "Veo",
    blurb: `${
      isScene
        ? "Can't edit video: put the speaker on a branded set (new keyframe) and re-rendered the clip."
        : isCard
          ? `Can't edit video: kept the ${target} card and generated the branded transition ${target === "intro" ? "into" : "out of"} the speaker.`
          : "First/last-frame transitions (Veo can't edit video); optional branded re-render."
    } No longer offered for new rebrands.`,
  };
}

/** While Omni is switched off: what to use instead, for this target. */
function omniOffNote(target: RebrandTarget, label: string): string {
  if (target === "reel") {
    return "Gemini Omni is switched off, so nothing here regenerates the footage: the 42 brand frame is the rebrand, and it's free.";
  }
  if (target === "intro" || target === "outro") {
    return `Gemini Omni is switched off, so the ${label.toLowerCase()} card isn't redrawn any more. To brand it, use ✨ Rebrand under the finished reel: the 42 brand frame wipes into and out of the intro and outro cards, for free.`;
  }
  return "Gemini Omni is switched off. Make a new version of this scene in the Rebrand studio — made in house, for free.";
}

const STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  briefing: "Writing the brief",
  running: "Rendering",
  ready: "Ready",
  failed: "Failed",
  cancelled: "Cancelled",
  interrupted: "Interrupted",
  skipped: "Off",
};

/** Stand-in for an arm a (pre-brand) server didn't return. */
const MISSING_ARM: RebrandArm = { status: "skipped", url: null, download_url: null, parts: {} };

function isActive(e: RebrandExperiment | null | undefined): boolean {
  return !!e && REBRAND_ACTIVE.includes(e.status);
}

/** Arms that actually ran (anything not switched off), in display order. */
function ranArms(e: RebrandExperiment, order: RebrandArmName[]): RebrandArmName[] {
  return order.filter((a) => {
    const arm = e.arms[a];
    return !!arm && arm.status !== "skipped";
  });
}

function joinAnd(parts: string[]): string {
  if (parts.length <= 1) return parts.join("");
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

function fmtTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function StatusPill({ status }: { status: string }) {
  const tone =
    status === "ready"
      ? "bg-success/10 text-success border-success/30"
      : status === "failed" || status === "interrupted"
        ? "bg-danger/10 text-danger border-danger/30"
        : status === "skipped" || status === "cancelled"
          ? "bg-bg-muted text-muted border-border"
          : "bg-accent-fade text-accent-hover border-accent/30";
  return (
    <span className={`inline-flex items-center gap-1 text-2xs font-medium px-2 py-0.5 rounded-full border ${tone}`}>
      {(status === "running" || status === "briefing" || status === "queued") && <Spinner size="xs" />}
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

function Stars({
  value,
  onChange,
  disabled,
}: {
  value: number | null;
  onChange: (v: number) => void;
  disabled?: boolean;
}) {
  return (
    <div className="inline-flex gap-0.5" role="radiogroup" aria-label="Rating">
      {[1, 2, 3, 4, 5].map((n) => (
        <button
          key={n}
          type="button"
          disabled={disabled}
          onClick={() => onChange(n)}
          aria-label={`${n} star${n > 1 ? "s" : ""}`}
          className={`text-lg leading-none transition-colors disabled:opacity-50 ${
            value != null && n <= value ? "text-warning" : "text-border-strong hover:text-warning/60"
          }`}
        >
          ★
        </button>
      ))}
    </div>
  );
}

function Swatch({ color }: { color: string }) {
  const isHex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(color.trim());
  return (
    <span className="inline-flex items-center gap-1 text-2xs px-1.5 py-0.5 rounded border border-border bg-bg">
      {isHex && <span className="w-3 h-3 rounded-sm border border-border" style={{ background: color }} />}
      {color}
    </span>
  );
}

// ---------------------------------------------------------------------------
// One arm (brand, Omni, or an older run's Veo)
// ---------------------------------------------------------------------------

/** The brand arm's measured facts: what it rendered and how each cut was hidden. */
function brandFacts(arm: RebrandArm): string[] {
  const out: string[] = [];
  if (arm.resolution) out.push(/^\d+$/.test(arm.resolution) ? `${arm.resolution}p` : arm.resolution);
  if (arm.segments?.length) {
    const n = arm.segments.length;
    out.push(`${n} clip${n > 1 ? "s" : ""}`);
    if (arm.segments.some((s) => s.layout === "full")) {
      const full = arm.segments.filter((s) => s.layout === "full").length;
      out.push(full === n ? "full-frame layout" : `${full} full-frame`);
    }
  }
  if (arm.seams?.length) {
    const counts = new Map<BrandSeam["style"], number>();
    for (const s of arm.seams) counts.set(s.style, (counts.get(s.style) ?? 0) + 1);
    const styles = [...counts.entries()].map(([k, c]) => `${c} ${SEAM_STYLE_LABEL[k]?.[c > 1 ? 1 : 0] ?? k}`);
    out.push(`${arm.seams.length} cut${arm.seams.length > 1 ? "s" : ""} (${styles.join(", ")})`);
  }
  if (arm.icons?.length) {
    const names = arm.icons.map((c) => c.icon.replace(/-/g, " "));
    out.push(`${names.length} icon${names.length > 1 ? "s" : ""} (${names.join(", ")})`);
  }
  if (arm.graphics_seconds) out.push(`graphics ${arm.graphics_seconds.toFixed(0)}s`);
  if (arm.composite_seconds) out.push(`composite ${arm.composite_seconds.toFixed(0)}s`);
  return out;
}

/** Tone of the frame's own fit report: left out = red, partly = amber. */
const FIT_STATUS_LEVEL: Record<BrandTextFit["status"], FitLevel> = {
  none: "ok",
  shown: "ok",
  shrunk: "ok",
  partial: "tight",
  dropped: "over",
};

/** The text the brand arm handed the frame (after its auto-from-script
 *  defaults) and what the frame actually drew — its own report, measured with
 *  the real fonts (`text_fit`), so a heading it left out reads as "left out",
 *  not as a silent bug. Runs from before that report fall back to the same
 *  character-count estimate the form shows. Nothing for runs that predate the
 *  text field or haven't finished. */
function BrandTextResult({ arm }: { arm: RebrandArm }) {
  if (arm.text === undefined) return null;
  const heading = arm.text?.heading?.trim() ?? "";
  const sub = arm.text?.sub?.trim() ?? "";
  if (!heading && !sub) {
    return (
      <p className="text-2xs text-muted">
        <span className="font-medium text-fg">Text:</span> none
      </p>
    );
  }
  const fit = arm.text_fit;
  let note: { level: FitLevel; msg: string } | null = null;
  if (fit && fit.status !== "none") {
    if (fit.message && fit.status !== "shown") note = { level: FIT_STATUS_LEVEL[fit.status] ?? "tight", msg: fit.message };
  } else {
    // Text lives only in the bars of a "bars" scene; the full-frame layout
    // (full-bleed or 16:9 footage) has no text area at all.
    const textArea = arm.segments ? arm.segments.some((s) => s.kind === "scene" && s.layout === "bars") : true;
    const h = heading ? headingFit(heading.length) : "ok";
    const s = sub ? subFit(sub.length) : "ok";
    if (!textArea) note = { level: "over", msg: "not drawn: the full-frame layout has no text area" };
    else if (h === "over") note = { level: "over", msg: "heading probably too long, so it was likely left out" };
    else if (s === "over") note = { level: "over", msg: "sub-line probably too long, so it was likely left out" };
    else if (h === "tight" || s === "tight") note = { level: "tight", msg: "tight fit — check it made it into the video" };
  }
  // Struck through: the part the frame reported leaving out.
  const headingOut = fit ? fit.status !== "none" && !fit.heading_shown : false;
  const subOut = fit ? fit.status !== "none" && !fit.sub_shown : false;
  return (
    <p className="text-2xs leading-snug">
      <span className="font-medium">Text:</span>{" "}
      {heading && <span className={`font-semibold ${headingOut ? "line-through text-muted" : ""}`}>“{heading}”</span>}
      {heading && sub && <span className="text-muted"> · </span>}
      {sub && <span className={`text-muted ${subOut ? "line-through" : ""}`}>“{sub}”</span>}
      {note && <span className={FIT_TONE[note.level]}> — {note.msg}</span>}
    </p>
  );
}

function ArmCard({
  name,
  target,
  arm,
  aspect,
  stars,
  onStars,
  isWinner,
  rateDisabled,
}: {
  name: RebrandArmName;
  target: RebrandTarget;
  arm: RebrandArm;
  aspect: string;
  stars: number | null;
  onStars: (v: number) => void;
  isWinner: boolean;
  rateDisabled: boolean;
}) {
  const [showSteps, setShowSteps] = useState(false);
  const steps = arm.steps ?? [];
  const lastSteps = showSteps ? steps : steps.slice(-4);
  const review = arm.auto_review;
  const parts = arm.parts ?? {};
  const partLabels: Record<string, string> = {
    transition_in: "Transition in",
    transition_out: "Transition out",
  };
  const meta = armMeta(name, target);

  return (
    <div
      className={`rounded-xl border p-3 flex flex-col gap-3 min-w-0 ${
        isWinner ? "border-accent ring-1 ring-accent/40" : "border-border"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h4 className="text-sm font-semibold">{meta.title}</h4>
            {isWinner && <span className="text-2xs font-semibold text-accent">★ Winner</span>}
          </div>
          <p className="text-2xs text-muted leading-snug">{meta.blurb}</p>
        </div>
        <StatusPill status={arm.status} />
      </div>

      {arm.url ? (
        <video
          key={arm.url}
          src={arm.url}
          controls
          playsInline
          className="rounded-lg bg-black w-full"
          style={{ aspectRatio: aspect.replace(":", "/"), maxHeight: "60vh", objectFit: "contain" }}
        />
      ) : (
        <div
          className="rounded-lg bg-bg-muted border border-border flex items-center justify-center text-xs text-muted w-full"
          style={{ aspectRatio: aspect.replace(":", "/"), maxHeight: "40vh" }}
        >
          {arm.status === "running" || arm.status === "queued" ? (
            <div className="text-center space-y-2 px-4">
              <div className="flex justify-center">
                <Spinner size="lg" tone="accent" />
              </div>
              <p>
                {steps.length
                  ? steps[steps.length - 1].name
                  : name === "brand"
                    ? "Starting…"
                    : "Waiting for the brief…"}
              </p>
            </div>
          ) : arm.status === "skipped" ? (
            <p className="px-4 text-center">{arm.note || "Turned off for this run."}</p>
          ) : (
            <p className="px-4 text-center text-danger">{arm.error || "No video."}</p>
          )}
        </div>
      )}

      {(arm.url || Object.keys(parts).length > 0) && (
        <div className="flex flex-wrap items-center gap-1.5 text-2xs">
          {arm.download_url && (
            <a href={arm.download_url} className="btn btn-ghost text-2xs py-1">
              ⬇ Download
            </a>
          )}
          {Object.entries(parts).map(([k, url]) => (
            <a
              key={k}
              href={url}
              target="_blank"
              rel="noreferrer"
              className="px-2 py-1 rounded-md border border-border hover:border-accent text-muted hover:text-fg"
              title="Open this piece on its own"
            >
              {partLabels[k] ?? k.replace("scene_", "Scene ").replace("between_", "Scene cut ").replace(/_/g, " ")}
            </a>
          ))}
        </div>
      )}

      <div className="text-2xs text-muted flex flex-wrap gap-x-3 gap-y-0.5">
        {name === "brand" && brandFacts(arm).map((f) => <span key={f}>{f}</span>)}
        {arm.model && <span>model: {arm.model}</span>}
        {arm.provider && <span>via {arm.provider === "api_key" ? "Gemini API" : "Vertex"}</span>}
        {arm.scenes_total != null && arm.scenes_edited != null && (
          <span>
            scenes edited: {arm.scenes_edited}/{arm.scenes_total}
          </span>
        )}
        {arm.scenes_total != null && arm.scenes_rerendered != null && arm.scene_mode === "rerender" && (
          <span>
            scenes re-rendered: {arm.scenes_rerendered}/{arm.scenes_total}
          </span>
        )}
        {arm.duration_sec ? <span>{arm.duration_sec.toFixed(1)}s</span> : null}
      </div>

      {name === "brand" && <BrandTextResult arm={arm} />}

      {review && (
        <div className="rounded-lg bg-bg-muted/60 border border-border p-2.5 space-y-1.5 text-xs">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="font-semibold">Self-review {review.score}/10</span>
            <span className="text-muted">brand {review.brand_fidelity}/10</span>
            <span className="text-muted">transitions {review.transition_quality}/10</span>
            <span className={review.identity_preserved ? "text-success" : "text-danger"}>
              {review.identity_preserved ? "speaker intact" : "speaker changed"}
            </span>
          </div>
          {review.strengths?.length > 0 && (
            <p className="text-2xs">
              <span className="text-success font-medium">+ </span>
              {review.strengths.slice(0, 3).join(" · ")}
            </p>
          )}
          {review.issues?.length > 0 && (
            <p className="text-2xs">
              <span className="text-danger font-medium">− </span>
              {review.issues.slice(0, 3).join(" · ")}
            </p>
          )}
          {review.lesson && <p className="text-2xs text-muted italic">Lesson: {review.lesson}</p>}
        </div>
      )}

      {arm.status !== "skipped" && (
        <div className="flex items-center justify-between gap-2">
          <span className="text-2xs text-muted">Your rating</span>
          <Stars value={stars} onChange={onStars} disabled={rateDisabled} />
        </div>
      )}

      {steps.length > 0 && (
        <div className="text-2xs space-y-0.5">
          {lastSteps.map((s, i) => (
            <div key={`${s.at}-${i}`} className="flex gap-1.5 items-start">
              <span className={s.status === "failed" ? "text-danger" : s.status === "done" ? "text-success" : "text-muted"}>
                {s.status === "failed" ? "✕" : s.status === "done" ? "✓" : "•"}
              </span>
              <span className="min-w-0">
                {s.name}
                {s.detail && <span className="text-muted"> — {s.detail}</span>}
              </span>
            </div>
          ))}
          {steps.length > 4 && (
            <button type="button" className="text-accent hover:underline" onClick={() => setShowSteps((v) => !v)}>
              {showSteps ? "Show fewer steps" : `Show all ${steps.length} steps`}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The brief
// ---------------------------------------------------------------------------

function BriefCard({ exp }: { exp: RebrandExperiment }) {
  const [open, setOpen] = useState(false);
  const b = exp.brief;
  const ev = exp.evidence;
  if (!b) return null;
  const learned: string[] = [];
  if (ev?.ig_posts_analysed) learned.push(`${ev.ig_posts_analysed} Instagram posts`);
  if (ev?.edit_recipes_used) learned.push(`${ev.edit_recipes_used} edit recipes`);
  if (ev?.past_experiments_used?.length)
    learned.push(`${ev.past_experiments_used.length} past rebrand${ev.past_experiments_used.length > 1 ? "s" : ""}`);
  if (ev?.logo_attached) learned.push("the 42 logo");

  return (
    <div className="rounded-xl border border-border bg-bg-deep p-3 text-xs space-y-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="font-semibold text-sm">The brief</div>
          {learned.length > 0 && <p className="text-2xs text-muted">Learned from {learned.join(", ")}.</p>}
        </div>
        <button type="button" className="btn btn-ghost text-2xs py-1 shrink-0" onClick={() => setOpen((v) => !v)}>
          {open ? "Hide details" : "Details"}
        </button>
      </div>
      {b._fallback && (
        <p className="text-2xs text-amber-700 bg-warning/10 border border-warning/30 rounded px-2 py-1">
          The brief model was unavailable, so a basic brand brief was used{ev?.brief_error ? ` (${ev.brief_error})` : ""}.
        </p>
      )}
      {b.brand_read && <p className="leading-relaxed">{b.brand_read}</p>}
      {b.palette && b.palette.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {b.palette.map((c) => (
            <Swatch key={c} color={c} />
          ))}
        </div>
      )}
      <div className="grid sm:grid-cols-2 gap-x-4 gap-y-1.5">
        {b.transition_in?.style && (
          <p>
            <span className="text-muted">In:</span> <b>{b.transition_in.style}</b>
            {open && b.transition_in.description ? ` — ${b.transition_in.description}` : ""}
          </p>
        )}
        {b.transition_out?.style && (
          <p>
            <span className="text-muted">Out:</span> <b>{b.transition_out.style}</b>
            {open && b.transition_out.description ? ` — ${b.transition_out.description}` : ""}
          </p>
        )}
        {b.background?.description && (
          <p className="sm:col-span-2">
            <span className="text-muted">Background:</span> {b.background.description}
            {open && b.background.motion ? ` (${b.background.motion})` : ""}
          </p>
        )}
        {b.icons && b.icons.length > 0 && (
          <p className="sm:col-span-2">
            <span className="text-muted">Icons:</span>{" "}
            {b.icons
              .map((i) => (open ? `${i.name} — ${i.look}, ${i.placement}` : i.name))
              .filter(Boolean)
              .join(open ? "; " : ", ")}
          </p>
        )}
        {open && b.logo?.placement && (
          <p className="sm:col-span-2">
            <span className="text-muted">Logo:</span> {b.logo.placement}
            {b.logo.treatment ? ` — ${b.logo.treatment}` : ""}
          </p>
        )}
      </div>
      {open && b.lessons_applied && b.lessons_applied.length > 0 && (
        <div>
          <div className="text-muted">Lessons applied from past rebrands:</div>
          <ul className="list-disc pl-4">
            {b.lessons_applied.map((l, i) => (
              <li key={i}>{l}</li>
            ))}
          </ul>
        </div>
      )}
      {open && (
        <details className="text-2xs">
          <summary className="cursor-pointer text-muted">Model prompts</summary>
          <div className="mt-1 space-y-1.5">
            {b.omni_scene_prompt && (
              <p>
                <b>Omni scene edit:</b> {b.omni_scene_prompt}
              </p>
            )}
            {b.veo_transition_in_prompt && (
              <p>
                <b>Veo transition in:</b> {b.veo_transition_in_prompt}
              </p>
            )}
          </div>
        </details>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

const INPUT_CLS =
  "w-full rounded-lg border border-border bg-bg px-2.5 py-1.5 text-xs focus:outline-none focus:border-accent disabled:opacity-50 disabled:bg-bg-muted";

export function RebrandPanel({
  runId,
  aspect,
  sceneCount,
  target = "reel",
  targetLabel,
  embedded = false,
}: {
  runId: number;
  aspect: string;
  sceneCount: number;
  /** "reel" = the whole stitched video; otherwise ONE Videos-stage clip. */
  target?: RebrandTarget;
  /** Human name of the target ("Intro", "Scene 2"...). */
  targetLabel?: string;
  /** Rendered inside a modal: always open, no own frame or Close button. */
  embedded?: boolean;
}) {
  const toast = useToast();
  const [open, setOpen] = useState(embedded);
  const isReel = target === "reel";
  const isScene = target.startsWith("scene_");
  const label = targetLabel ?? (isReel ? "this reel" : target);
  // Every arm an experiment may carry, in display order. New runs only ever
  // have brand/Omni; "veo" is here so an older experiment whose Veo arm ran
  // still shows it. A clip run never has a brand arm.
  const armOrder: RebrandArmName[] = isReel ? ["brand", "omni", "veo"] : ["omni", "veo"];
  const [caps, setCaps] = useState<RebrandCapabilities | null>(null);
  const [items, setItems] = useState<RebrandExperiment[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [opts, setOpts] = useState<RebrandOptions>(() => defaultOptions(target));
  // Brand text drafts: "" in a box = auto from the script; "No text" sends "".
  const [brandHeading, setBrandHeading] = useState("");
  const [brandSub, setBrandSub] = useState("");
  const [brandNoText, setBrandNoText] = useState(false);
  const [composing, setComposing] = useState(false);
  const [starting, setStarting] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Rating draft for the selected experiment.
  const [omniStars, setOmniStars] = useState<number | null>(null);
  const [veoStars, setVeoStars] = useState<number | null>(null);
  const [brandStars, setBrandStars] = useState<number | null>(null);
  const [winner, setWinner] = useState<RebrandWinner | null>(null);
  const [notes, setNotes] = useState("");
  const [savingRating, setSavingRating] = useState(false);

  const selected = useMemo(
    () => items.find((e) => e.id === selectedId) ?? items[0] ?? null,
    [items, selectedId],
  );

  // Load this reel's experiments (and whether Omni is configured).
  useEffect(() => {
    let cancelled = false;
    setItems([]);
    setSelectedId(null);
    setLoadError(null);
    rebrandApi
      .list(runId, target)
      .then((r) => {
        if (cancelled) return;
        setCaps(r.capabilities);
        setItems(r.experiments);
        if (r.experiments.length) {
          setOpen(true);
          setSelectedId(r.experiments[0].id);
        }
      })
      .catch((e) => !cancelled && setLoadError(String(e?.message ?? e)));
    return () => {
      cancelled = true;
    };
  }, [runId, target]);

  // Seed the rating draft whenever a different experiment is shown.
  useEffect(() => {
    setOmniStars(selected?.rating?.omni_stars ?? null);
    setVeoStars(selected?.rating?.veo_stars ?? null);
    setBrandStars(selected?.rating?.brand_stars ?? null);
    setWinner(selected?.winner ?? null);
    setNotes(selected?.rating?.notes ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id]);

  const replace = (e: RebrandExperiment) =>
    setItems((prev) => {
      const i = prev.findIndex((x) => x.id === e.id);
      if (i < 0) return [e, ...prev];
      const next = prev.slice();
      next[i] = e;
      return next;
    });

  usePoll({
    enabled: isActive(selected),
    intervalMs: 4000,
    onTick: async () => {
      if (!selected) return "stop";
      const e = await rebrandApi.get(selected.id);
      replace(e);
      if (!REBRAND_ACTIVE.includes(e.status)) {
        if (e.status === "ready") {
          const ran = ranArms(e, armOrder);
          toast.success(
            "Rebrand ready",
            ran.length > 1
              ? `Compare ${joinAnd(ran.map((a) => ARM_SHORT[a]))} side by side.`
              : ran[0] === "brand"
                ? "Your 42 brand frame is ready."
                : ran.length
                  ? `${ARM_SHORT[ran[0]]} is ready.`
                  : undefined,
          );
        } else if (e.status === "failed") toast.error("Rebrand failed", e.error ?? undefined);
        return "stop";
      }
    },
  });

  const anyActive = items.some(isActive);
  const omniOk = caps?.omni.configured ?? true;
  // Omni makes nothing new while the server has it switched off — and until
  // the server has said otherwise, nothing Omni is offered.
  const omniOff = !caps || caps.omni.switched_off === true;
  // What this run would do. The brand arm needs only Remotion — no capability gate.
  const brandOn = isReel && !!opts.run_brand;
  const omniOn = opts.run_omni && omniOk && !omniOff;
  const canStart = brandOn || omniOn;
  const transitionHelp = BRAND_TRANSITIONS.find((t) => t.value === (opts.brand_transitions ?? "auto"))?.help;
  // A 16:9 canvas always gets the full-frame layout (corner slab + logo), which
  // has no text area — the text fields would be a promise the frame can't keep.
  const brandTextless = aspect === "16:9";
  const textOff = brandNoText || brandTextless;
  const headingHint = brandTextHint("heading", brandHeading);
  const subHint = brandTextHint("sub", brandSub);

  const startLabel = !isReel
    ? `Rebrand ${label}`
    : brandOn
      ? omniOn
        ? "Apply 42 brand frame + Omni"
        : "Apply 42 brand frame"
      : omniOn
        ? "Rebrand with Omni"
        : "Rebrand this reel";

  const start = async () => {
    setStarting(true);
    try {
      const body: RebrandOptions = {
        ...opts,
        target,
        run_omni: omniOn,
        // Veo is retired; the server ignores this, the panel never asks for it.
        run_veo: false,
      };
      if (isReel) {
        body.run_brand = brandOn;
        // null = auto from the script, "" = no text on the frame (also what a
        // 16:9 reel gets, so its result card reports "none" truthfully).
        body.brand_heading = textOff ? "" : brandHeading.trim().slice(0, BRAND_TEXT_FIT.headingMax) || null;
        body.brand_sub = textOff ? "" : brandSub.trim().slice(0, BRAND_TEXT_FIT.subMax) || null;
      } else {
        // A clip run sends exactly what it always did.
        for (const k of BRAND_KEYS) delete body[k];
      }
      const e = await rebrandApi.start(runId, body);
      replace(e);
      setSelectedId(e.id);
      setComposing(false);
    } catch (err) {
      toast.error("Couldn't start the rebrand", String((err as Error)?.message ?? err));
    } finally {
      setStarting(false);
    }
  };

  const cancel = async () => {
    if (!selected) return;
    try {
      replace(await rebrandApi.cancel(selected.id));
    } catch (err) {
      toast.error("Couldn't cancel", String((err as Error)?.message ?? err));
    }
  };

  const saveRating = async () => {
    if (!selected) return;
    setSavingRating(true);
    try {
      replace(
        await rebrandApi.rate(selected.id, {
          winner,
          omni_stars: omniStars,
          // Veo stars only exist where a Veo card is shown: an older run that ran it.
          ...(ranArms(selected, ["veo"]).length ? { veo_stars: veoStars } : {}),
          ...(isReel ? { brand_stars: brandStars } : {}),
          notes,
        }),
      );
      toast.success("Rating saved", omniOff ? undefined : "The next rebrand brief will learn from it.");
    } catch (err) {
      toast.error("Couldn't save the rating", String((err as Error)?.message ?? err));
    } finally {
      setSavingRating(false);
    }
  };

  if (!open) {
    return (
      <div className="flex flex-col items-center gap-1">
        <button
          type="button"
          onClick={() => {
            setOpen(true);
            setComposing(true);
          }}
          className="btn btn-ghost text-xs"
          title="Frame this reel in the 42 brand look — slabs, bead patterns, grid and logo — without regenerating it"
        >
          ✨ Rebrand
        </button>
        <p className="text-2xs text-muted">42 brand frame · your footage untouched</p>
      </div>
    );
  }

  const showForm = composing || (!selected && !anyActive);
  const ready = selected?.status === "ready";
  const starsFor: Record<RebrandArmName, [number | null, (v: number) => void]> = {
    omni: [omniStars, setOmniStars],
    veo: [veoStars, setVeoStars],
    brand: [brandStars, setBrandStars],
  };
  // Reel: only the arms that ran get a card (a brand-only run is one card, not
  // one card and an "Off" box). A clip always has its Omni card (it says why
  // when Omni didn't run). Either way an older run's Veo arm keeps its card
  // when it ran, and never gets an "Off" box when it didn't.
  const selectedRan = selected ? ranArms(selected, armOrder) : [];
  const cardArms: RebrandArmName[] = isReel
    ? selectedRan.length
      ? selectedRan
      : omniOff
        ? ["brand"]
        : ["brand", "omni"]
    : ["omni", ...(selectedRan.includes("veo") ? (["veo"] as const) : [])];
  const winnerArms = isReel ? selectedRan : cardArms;
  const brandOnly = isReel && selectedRan.length === 1 && selectedRan[0] === "brand";
  // Which pattern an Omni run used (runs from before the picker don't say).
  const selOpts = selected?.options;
  const patternSummary =
    selectedRan.includes("omni") && selOpts && selOpts.pattern_on !== undefined
      ? selOpts.pattern_on
        ? `pattern: ${selOpts.pattern ?? DEFAULT_REBRAND_PATTERN}${
            selOpts.pattern_animation ? ` (${selOpts.pattern_animation.replace(/_/g, " ")})` : ""
          }`
        : "no pattern"
      : "";

  // The Omni switch (+ the capability note) — the "Experimental" group in a
  // reel run. A clip run is Omni by definition: no switch, only the note when
  // Omni can't run.
  const generativePicker = (
    <>
      {isReel && (
        <label className={`flex items-center gap-1.5 ${omniOk ? "" : "opacity-50"}`}>
          <input
            type="checkbox"
            checked={opts.run_omni && omniOk}
            disabled={!omniOk}
            onChange={(e) => setOpts((o) => ({ ...o, run_omni: e.target.checked }))}
          />
          Gemini Omni Flash
          {!omniOk && <span className="text-danger">(unavailable)</span>}
        </label>
      )}

      {!omniOk && (
        <p className="text-2xs text-amber-700 bg-warning/10 border border-warning/30 rounded-lg px-2.5 py-2 leading-relaxed">
          {caps?.omni.note || "Gemini Omni Flash isn't available on this server."}
        </p>
      )}
    </>
  );

  // Settings that only the Omni brief and render read.
  const generativeSettings = (
    <>
      <RebrandPatternPicker
        value={{ pattern_on: opts.pattern_on, pattern: opts.pattern, pattern_animation: opts.pattern_animation }}
        onChange={(v) => setOpts((o) => ({ ...o, ...v }))}
        disabled={starting || !omniOk}
      />

      <div className="grid sm:grid-cols-2 gap-3">
        <div className="space-y-1">
          <div className="text-muted">Branding intensity</div>
          <SegmentedControl<RebrandIntensity>
            variant="compact"
            value={opts.intensity}
            onChange={(v) => setOpts((o) => ({ ...o, intensity: v }))}
            options={[
              { value: "subtle", label: "Subtle" },
              { value: "balanced", label: "Balanced" },
              { value: "bold", label: "Bold" },
            ]}
          />
        </div>
        {!isScene && (
        <div className="space-y-1">
          <div className="text-muted">Transition length · {opts.transition_sec.toFixed(1)}s</div>
          <input
            type="range"
            min={0.8}
            max={3}
            step={0.2}
            value={opts.transition_sec}
            onChange={(e) => setOpts((o) => ({ ...o, transition_sec: Number(e.target.value) }))}
            className="w-full accent-accent"
          />
        </div>
        )}
      </div>

      {isReel && sceneCount > 1 && (
        <label className="flex items-center gap-1.5">
          <input
            type="checkbox"
            checked={opts.scene_transitions}
            onChange={(e) => setOpts((o) => ({ ...o, scene_transitions: e.target.checked }))}
          />
          Also generate branded transitions between scenes
        </label>
      )}

      <textarea
        value={opts.notes}
        onChange={(e) => setOpts((o) => ({ ...o, notes: e.target.value.slice(0, 600) }))}
        rows={2}
        placeholder="Anything specific? e.g. “use the blue grid from our hackathon posts, keep the logo small”"
        className="w-full rounded-lg border border-border bg-bg px-2.5 py-2 text-xs focus:outline-none focus:border-accent"
      />
    </>
  );

  return (
    <section
      className={
        embedded
          ? "w-full space-y-4"
          : "w-full max-w-5xl rounded-2xl border border-border bg-bg shadow-soft p-4 space-y-4"
      }
    >
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          {!embedded && (
            <h3 className="text-sm font-semibold">✨ Rebrand · 42 brand frame</h3>
          )}
          <p className="text-2xs text-muted leading-snug max-w-xl">
            {isReel
              ? omniOff
                ? "Frames the finished reel in 42's brand graphics without regenerating a pixel."
                : "Frames the finished reel in 42's brand graphics without regenerating a pixel. Gemini Omni stays available as an experiment that regenerates it; it learns from your ratings of the runs before."
              : omniOff
                ? items.length
                  ? // The form (and its note) is closed while there are
                    // earlier runs: the note on what to use instead goes here.
                    `Earlier Gemini Omni rebrands of ${isScene ? label : `the ${label.toLowerCase()} card`}, to play, rate and download. ${omniOffNote(target, label)}`
                  : null
                : (
                <>
                  {isScene
                    ? `Rebrands ${label} alone with Gemini Omni — branded background and icons from the 42 Instagram look, plus the brand pattern you pick.`
                    : `Rebrands the ${label.toLowerCase()} card and its transition ${target === "intro" ? "into" : "out of"} the speaker with Gemini Omni, from the 42 Instagram look.`}{" "}
                  Each run learns from your ratings of the ones before.
                </>
              )}
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {!showForm && !anyActive && (isReel || !omniOff) && (
            <button type="button" className="btn btn-accent text-xs" onClick={() => setComposing(true)}>
              New rebrand
            </button>
          )}
          {!embedded && (
            <button
              type="button"
              className="btn btn-ghost text-xs"
              onClick={() => {
                setOpen(false);
                setComposing(false);
              }}
            >
              Close
            </button>
          )}
        </div>
      </div>

      {loadError && <p className="text-xs text-danger">Couldn&apos;t load past rebrands: {loadError}</p>}

      {/* History */}
      {items.length > 1 && (
        <div className="flex flex-wrap gap-1.5">
          {items.map((e) => (
            <button
              key={e.id}
              type="button"
              onClick={() => {
                setSelectedId(e.id);
                setComposing(false);
              }}
              className={`text-2xs px-2 py-1 rounded-md border transition-colors ${
                selected?.id === e.id && !showForm
                  ? "border-accent bg-accent-fade text-fg"
                  : "border-border text-muted hover:text-fg"
              }`}
            >
              #{e.id} · {STATUS_LABEL[e.status] ?? e.status}
              {e.winner && e.winner !== "neither" ? ` · ${ARM_SHORT[e.winner] ?? e.winner} won` : ""} ·{" "}
              {fmtTime(e.created_at)}
            </button>
          ))}
        </div>
      )}

      {/* New-run form */}
      {showForm && (
        <div className="rounded-xl border border-border bg-bg-deep p-3 space-y-3 text-xs">
          {isReel && (
            <div
              className={`rounded-lg border p-3 space-y-3 transition-colors ${
                brandOn ? "border-accent/50 bg-accent-fade" : "border-border bg-bg"
              }`}
            >
              <label className="flex items-start gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={brandOn}
                  onChange={(e) => setOpts((o) => ({ ...o, run_brand: e.target.checked }))}
                />
                <span className="min-w-0">
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-semibold">42 Brand frame</span>
                    <span className="text-2xs font-medium px-1.5 py-0.5 rounded-full border border-success/30 bg-success/10 text-success">
                      Recommended
                    </span>
                  </span>
                  <span className="block text-2xs text-muted leading-snug mt-0.5">
                    Adds 42&apos;s violet slabs, your patterns sliding left to right, topic icons and the logo around
                    your video; slab wipes and glitch cuts hide the cuts. Nothing is regenerated.
                  </span>
                </span>
              </label>

              {brandOn && (
                <div className="grid sm:grid-cols-2 gap-3 sm:pl-6">
                  <div className="space-y-1">
                    <label className="text-muted block" htmlFor={`rb-trans-${runId}`}>
                      Transitions
                    </label>
                    <select
                      id={`rb-trans-${runId}`}
                      value={opts.brand_transitions ?? "auto"}
                      onChange={(e) =>
                        setOpts((o) => ({ ...o, brand_transitions: e.target.value as BrandTransitions }))
                      }
                      className={INPUT_CLS}
                    >
                      {BRAND_TRANSITIONS.map((t) => (
                        <option key={t.value} value={t.value}>
                          {t.label}
                        </option>
                      ))}
                    </select>
                    {transitionHelp && <p className="text-2xs text-muted leading-snug">{transitionHelp}</p>}
                  </div>
                  <div className="space-y-1">
                    <div className="text-muted">Output</div>
                    <SegmentedControl<BrandResolution>
                      variant="compact"
                      value={opts.brand_resolution ?? "1080"}
                      onChange={(v) => setOpts((o) => ({ ...o, brand_resolution: v }))}
                      options={[
                        { value: "1080", label: "1080p", title: "Full HD — the default" },
                        { value: "720", label: "720p", title: "Smaller file, quicker render" },
                      ]}
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-muted block" htmlFor={`rb-head-${runId}`}>
                      Heading <span className="text-2xs">(optional)</span>
                    </label>
                    <input
                      id={`rb-head-${runId}`}
                      type="text"
                      value={textOff ? "" : brandHeading}
                      disabled={textOff}
                      maxLength={BRAND_TEXT_FIT.headingMax}
                      onChange={(e) => setBrandHeading(e.target.value.slice(0, BRAND_TEXT_FIT.headingMax))}
                      placeholder={textOff ? "No text" : "Auto from the script"}
                      aria-describedby={textOff ? undefined : `rb-head-hint-${runId}`}
                      aria-invalid={!textOff && headingHint.level === "over"}
                      className={INPUT_CLS}
                    />
                    {!textOff && (
                      <div id={`rb-head-hint-${runId}`} className="flex items-start justify-between gap-2 text-2xs leading-snug">
                        <span className={FIT_TONE[headingHint.level]} aria-live="polite">
                          {headingHint.msg}
                        </span>
                        <span className={`shrink-0 tabular-nums ${FIT_TONE[headingHint.level]}`}>
                          {brandHeading.trim().length}/~{BRAND_TEXT_FIT.headingFits}
                        </span>
                      </div>
                    )}
                  </div>
                  <div className="space-y-1">
                    <label className="text-muted block" htmlFor={`rb-sub-${runId}`}>
                      Sub-line <span className="text-2xs">(optional)</span>
                    </label>
                    <input
                      id={`rb-sub-${runId}`}
                      type="text"
                      value={textOff ? "" : brandSub}
                      disabled={textOff}
                      maxLength={BRAND_TEXT_FIT.subMax}
                      onChange={(e) => setBrandSub(e.target.value.slice(0, BRAND_TEXT_FIT.subMax))}
                      placeholder={textOff ? "No text" : "Auto from the script"}
                      aria-describedby={textOff ? undefined : `rb-sub-hint-${runId}`}
                      aria-invalid={!textOff && subHint.level === "over"}
                      className={INPUT_CLS}
                    />
                    {!textOff && (
                      <div id={`rb-sub-hint-${runId}`} className="flex items-start justify-between gap-2 text-2xs leading-snug">
                        <span className={FIT_TONE[subHint.level]} aria-live="polite">
                          {subHint.msg}
                        </span>
                        <span className={`shrink-0 tabular-nums ${FIT_TONE[subHint.level]}`}>
                          {brandSub.trim().length}/~{BRAND_TEXT_FIT.subFits}
                        </span>
                      </div>
                    )}
                  </div>
                  {brandTextless && (
                    <p className="sm:col-span-2 text-2xs text-muted leading-snug">
                      16:9 reels get the full-frame layout (corner slab and logo), which has no room for text — the
                      heading and sub-line only appear on 9:16 reels.
                    </p>
                  )}
                  <div className="sm:col-span-2 flex flex-wrap gap-x-4 gap-y-1.5">
                    <label className={`flex items-center gap-1.5 ${brandTextless ? "opacity-50" : ""}`}>
                      <input
                        type="checkbox"
                        checked={textOff}
                        disabled={brandTextless}
                        onChange={(e) => setBrandNoText(e.target.checked)}
                      />
                      No text
                    </label>
                    <label className="flex items-center gap-1.5">
                      <input
                        type="checkbox"
                        checked={!!opts.brand_logo_on_cards}
                        onChange={(e) => setOpts((o) => ({ ...o, brand_logo_on_cards: e.target.checked }))}
                      />
                      Show logo on intro/outro cards
                    </label>
                    <label className="flex items-center gap-1.5" title="Icons picked from what each scene says (free → piggy bank, code → </>), glitching in and out">
                      <input
                        type="checkbox"
                        checked={opts.brand_icons !== false}
                        onChange={(e) => setOpts((o) => ({ ...o, brand_icons: e.target.checked }))}
                      />
                      Topic icons
                    </label>
                    <label className="flex items-center gap-1.5" title="The pattern also slides over the video's left edge, never over the person">
                      <input
                        type="checkbox"
                        checked={opts.brand_edge_patterns !== false}
                        onChange={(e) => setOpts((o) => ({ ...o, brand_edge_patterns: e.target.checked }))}
                      />
                      Patterns on the video's left edge
                    </label>
                  </div>
                </div>
              )}
            </div>
          )}

          {omniOff ? (
            // Omni makes nothing new: say so plainly, and what to use instead.
            <p className="text-2xs text-muted leading-relaxed bg-bg border border-border rounded-lg px-2.5 py-2">
              {omniOffNote(target, label)}
            </p>
          ) : isReel ? (
            <div className="rounded-lg border border-dashed border-border-strong p-3 space-y-3">
              <div>
                <div className="font-semibold">Experimental — regenerates pixels</div>
                <p className="text-2xs text-muted leading-snug">
                  Gemini Omni redraws the video with a generative model, so the speaker and set can change. Takes
                  minutes.
                </p>
              </div>
              {generativePicker}
              {omniOn && generativeSettings}
            </div>
          ) : (
            <>
              {generativePicker}
              {generativeSettings}
            </>
          )}

          <div className="flex flex-wrap items-center gap-3">
            {(isReel || !omniOff) && (
              <button
                type="button"
                onClick={start}
                disabled={starting || anyActive || !canStart}
                className="btn btn-accent text-xs disabled:opacity-50"
              >
                {starting ? (
                  <span className="inline-flex items-center gap-1.5">
                    <Spinner size="sm" /> Starting…
                  </span>
                ) : (
                  startLabel
                )}
              </button>
            )}
            {items.length > 0 && (
              <button type="button" className="btn btn-ghost text-xs" onClick={() => setComposing(false)}>
                Back to results
              </button>
            )}
            {(isReel || !omniOff) && (
              <p className="text-2xs text-muted">
                {isReel
                  ? !canStart
                    ? omniOff
                      ? "Turn on the 42 brand frame to rebrand this reel."
                      : "Pick the brand frame or the Omni experiment."
                    : omniOn
                      ? "Takes a few minutes — Omni edits each scene and generates the transitions."
                      : "No generation and no model cost: the footage stays exactly as shot; only the graphics are drawn on."
                  : !omniOk
                    ? "Gemini Omni is unavailable, so this clip can't be rebranded right now."
                    : isScene
                      ? "Takes a few minutes. Omni edits this clip; the speaker and voice are kept."
                      : "Takes a few minutes. Omni generates the branded transition from the real frames."}
              </p>
            )}
          </div>
        </div>
      )}

      {/* Selected experiment */}
      {!showForm && selected && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <StatusPill status={selected.status} />
            <span className="text-muted">
              #{selected.id} · {selected.stage || ""}
              {patternSummary ? ` · ${patternSummary}` : ""}
              {selected.cost_usd > 0 ? ` · ≈ $${selected.cost_usd.toFixed(2)}` : ""}
            </span>
            {isActive(selected) && (
              <button type="button" className="btn btn-ghost text-2xs py-1 ml-auto" onClick={cancel}>
                Cancel
              </button>
            )}
          </div>
          {selected.error && (
            <p className="text-xs text-danger bg-danger/10 border border-danger/30 rounded-lg px-3 py-2">
              {selected.error}
            </p>
          )}

          <BriefCard exp={selected} />

          <div className={`grid gap-3 ${cardArms.length >= 3 ? "md:grid-cols-3" : "md:grid-cols-2"}`}>
            {cardArms.map((a) => (
              <ArmCard
                key={a}
                name={a}
                target={target}
                arm={selected.arms[a] ?? MISSING_ARM}
                aspect={aspect}
                stars={starsFor[a][0]}
                onStars={starsFor[a][1]}
                isWinner={selected.winner === a}
                rateDisabled={!ready}
              />
            ))}
          </div>

          {ready && (
            <div className="rounded-xl border border-border p-3 space-y-2 text-xs">
              {winnerArms.length > 1 ? (
                <div className="flex flex-wrap items-center gap-3">
                  <span className="font-semibold">Which one is closer to the 42 brand?</span>
                  <SegmentedControl<RebrandWinner | "">
                    variant="compact"
                    value={winner ?? ""}
                    onChange={(v) => setWinner(v || null)}
                    options={[
                      ...winnerArms.map((a) => ({ value: a, label: ARM_SHORT[a] })),
                      { value: "neither" as const, label: "Neither" },
                    ]}
                  />
                </div>
              ) : (
                <div className="font-semibold">How close is it to the 42 brand?</div>
              )}
              <textarea
                value={notes}
                onChange={(e) => setNotes(e.target.value.slice(0, 1000))}
                rows={2}
                placeholder={
                  brandOnly
                    ? "What worked, what didn't? e.g. “the slab wipe hides the cut well, but the heading is too long”"
                    : isReel && winnerArms.length > 1
                      ? "What worked, what didn't? e.g. “the brand frame's wipes are cleaner, but Omni's background is more on-brand”"
                      : "What worked, what didn't? e.g. “the pattern sits well behind the speaker, but the logo warps”"
                }
                className="w-full rounded-lg border border-border bg-bg px-2.5 py-2 text-xs focus:outline-none focus:border-accent"
              />
              <div className="flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  onClick={saveRating}
                  disabled={savingRating || (!omniStars && !veoStars && !brandStars && !winner && !notes.trim())}
                  className="btn btn-accent text-xs disabled:opacity-50"
                >
                  {savingRating ? "Saving…" : selected.rating ? "Update rating" : "Save rating"}
                </button>
                <p className="text-2xs text-muted">
                  {omniOff
                    ? "Saved with this rebrand."
                    : "Ratings and notes go into the rebrand memory, so the next brief repeats what you liked."}
                </p>
              </div>
              {selected.lesson && (
                <p className="text-2xs text-muted">
                  <span className="font-medium text-fg">Stored lesson:</span> {selected.lesson}
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
