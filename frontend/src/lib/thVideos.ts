/**
 * Talking Head v2 "Videos": clips are made ONLY by an explicit press.
 *
 * Run 830 got two rounds of Veo nobody asked for ($3.60 and $1.40): a page
 * opened on the Videos step (a "@videos" link) and a click on the out-of-date
 * Videos step after an Undo both sent /finalize from an effect. Now entering
 * or clicking the Videos step only shows the clips. They are made by the
 * Videos button, "Make videos" (no clip yet) or "Update N clips" (some are
 * missing, failed or out of date), which shows what it costs: the backend's
 * videos_plan, priced with the meter's own Veo rates ($1.40 an 8 s scene with
 * audio on Veo Fast, $0.40 a 4 s Veo intro or outro, $0 the slow zoom). The
 * button sends confirm, without which the backend refuses. Cmd/Ctrl+Enter
 * never starts anything that costs: a paid button needs a click.
 *
 * Pure functions (checked by scripts/check-th-videos.mjs, which also reads the
 * creator's source: no effect may start paid work).
 */
import type { TalkingHeadRun, TalkingHeadVideosPlan, TalkingHeadVideosPlanClip } from "./types";

/** "$1.40". */
export function usd(v: number): string {
  return `$${(Math.round(v * 100) / 100).toFixed(2)}`;
}

/** What a press costs: "~$3.60", a range while a card's motion is not
 *  planned yet ("~$2.80 to $3.60": the slow zoom is free, Veo is not),
 *  "free" when nothing in it costs, null when the backend does not say (an
 *  older one). */
export function videosCost(plan: TalkingHeadVideosPlan | null | undefined): string | null {
  if (!plan) return null;
  if (plan.usd_max <= 0) return "free";
  if (Math.abs(plan.usd_max - plan.usd) < 0.005) return `~${usd(plan.usd)}`;
  return `~${usd(plan.usd)} to ${usd(plan.usd_max)}`;
}

/** "Scene 2", "Main", "Intro", "Outro". */
export function clipTargetLabel(target: string): string {
  const m = /^scene_(\d+)$/.exec(target);
  if (m) return `Scene ${Number(m[1]) + 1}`;
  return target ? target.charAt(0).toUpperCase() + target.slice(1) : "";
}

/** One clip of a press in words: "Scene 1: 8 s, Veo Fast with audio, $1.40". */
export function clipLine(c: TalkingHeadVideosPlanClip): string {
  const what = clipTargetLabel(c.target);
  if (c.engine === "zoom") return `${what}: slow zoom, free`;
  const tier = (c.model ?? "").includes("fast") ? "Veo Fast" : "Veo Quality";
  const how = `${c.seconds} s, ${tier}${c.audio ? " with audio" : ""}`;
  if (c.engine === "veo_or_zoom") return `${what}: ${how} or the slow zoom, up to ${usd(c.usd_max)}`;
  return `${what}: ${how}, ${usd(c.usd)}`;
}

export interface VideosAction {
  /** make: no clip yet; update: some clips to make again; none: all fresh. */
  kind: "make" | "update" | "none";
  /** "Make videos", "Update 1 clip", "Update 3 clips" ("" for none). */
  label: string;
  count: number;
  /** videosCost of the press (null: unknown). */
  cost: string | null;
  /** What it makes, clip by clip. */
  hint: string;
  disabled: boolean;
  /** Why the button waits, or null. */
  waiting: string | null;
}

export interface VideosActionState {
  /** Something blocking runs on the page. */
  busy: boolean;
  /** Scene 0's still and every wanted card are made. */
  previewsReady: boolean;
}

type ClipFlags = { status?: string | null; stale?: boolean; excluded?: boolean };

/** The included clips as the run shows them (for an older backend with no
 *  videos_plan, and for "being made"). */
function clipsOf(run: TalkingHeadRun): ClipFlags[] {
  const out: ClipFlags[] = [];
  const scenes = run.scenes ?? [];
  if ((run.scene_count ?? 1) > 1) {
    for (const s of scenes) out.push({ status: s.animation?.status, stale: !!s.video_stale, excluded: !!s.excluded });
  } else {
    out.push({ status: run.animations?.main?.status, stale: !!run.main_video_stale });
  }
  out.push({ status: run.animations?.intro?.status, stale: !!run.intro_video_stale, excluded: !!run.intro_excluded });
  out.push({ status: run.animations?.outro?.status, stale: !!run.outro_video_stale, excluded: !!run.outro_excluded });
  return out;
}

/** The Videos button: "Make videos" / "Update N clips" with its cost, or
 *  nothing to do. It waits while a still is being made (the clip would
 *  animate the old one), while clips are being made, or until the stills
 *  and cards are ready. */
export function videosAction(run: TalkingHeadRun | null | undefined, st: VideosActionState): VideosAction {
  const none: VideosAction = { kind: "none", label: "", count: 0, cost: null, hint: "", disabled: true, waiting: null };
  if (!run) return none;
  const plan = run.videos_plan;
  const clips = clipsOf(run);
  const anyClip = plan ? plan.any_clip : clips.some((c) => !!c.status);
  const count = plan
    ? plan.count
    : clips.filter((c) => !c.excluded && (c.stale || !c.status || c.status === "failed")).length;
  if (count === 0) return none;
  const kind = anyClip ? "update" : "make";
  const label = kind === "make" ? "Make videos" : `Update ${count} clip${count === 1 ? "" : "s"}`;
  const hint = plan && plan.clips.length
    ? `${plan.count} clip${plan.count === 1 ? "" : "s"}: ${plan.clips.map(clipLine).join("; ")}.`
    : "Animates each still into a clip with Veo.";
  let waiting: string | null = null;
  if ((run.scenes ?? []).some((s) => s.keyframe_status === "generating")) waiting = "Wait for the still being made.";
  else if (clips.some((c) => !c.excluded && c.status === "generating")) waiting = "Clips are being made.";
  else if (!st.previewsReady) waiting = "Make the stills and cards first.";
  return {
    kind, label, count, cost: videosCost(plan), hint,
    disabled: st.busy || waiting !== null, waiting,
  };
}

/** Whether a primary's cost means money ("~$0.05", "~$3.60"): not null,
 *  empty or "free". */
export function costsMoney(cost: string | null | undefined): boolean {
  return !!cost && cost.trim() !== "" && cost.trim().toLowerCase() !== "free";
}

/** Whether Cmd/Ctrl+Enter may fire a primary: never one that costs money
 *  (a refine typed in a tile and sent with Cmd+Enter must not make the
 *  videos), nor one that gives the key away (`shortcut` false). */
export function shortcutFires(cost: string | null | undefined, shortcut?: boolean): boolean {
  return shortcut !== false && !costsMoney(cost);
}

/** The Videos stage's line while clips are out of date: what the button
 *  does, and that nothing is made without it. */
export function videosStaleNote(action: VideosAction): string | null {
  if (action.kind !== "update") return null;
  const cost = action.cost ? ` (${action.cost})` : "";
  return `${action.count} clip${action.count === 1 ? " is" : "s are"} missing or out of date. ` +
    `"${action.label}"${cost} makes ${action.count === 1 ? "it" : "them"} again; nothing is made until you press it.`;
}

/** The elapsed time of a running change, "0:45" / "2:05". */
export function elapsedLabel(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
