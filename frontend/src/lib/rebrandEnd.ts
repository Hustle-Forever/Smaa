// A scene's end (the end grip under the stage), as pure functions — checked
// by scripts/check-rebrand-end.mjs.
//
// One unit everywhere: an end is a count of the reel's 30 fps frames from
// the clip's start — in the studio, the stitch and storage alike. null is
// "automatic": a main scene's Veo tail flash cut (`auto`), else the whole
// clip. The grip runs from 1.0 s to the clip's real end (`extent`); a drop
// snaps to the automatic cut, a part's start or a pause between words when
// it is within 8 px of one, and a drop within 2 frames of the automatic end
// IS the automatic end (stored as null).
//
// What the studio makes of an end: only an end before the prepared clip's
// own last frame cuts anything (`cutOf`, the server's _cut_n) — the scene's
// seconds then are the end's, rounded as the server rounds them
// (`sceneSeconds`); with no cut they stay exactly what the server said.
//
// A main scene's start (the start grip, on the ruler's left) is the same
// unit: frames from the clip's first, null for no trim. It runs from the
// clip's first frame to 1.0 s before the scene's end, and the end grip
// keeps 1.0 s after it (`EndFacts.start`), so the two never cross; a drop
// within 2 frames of the first frame IS no trim. The studio keeps working
// in the clip's own time (the ruler, the lanes, every request): only the
// reel's stitch skips the start.

export const FPS = 30;
/** The shortest a scene may be (1.0 s: the reel's crossfades need 25 frames). */
export const MIN_END = 30;
/** A drop this close (frames) to the automatic end is the automatic end
 *  (and one this close to the clip's first frame is no start). */
export const AUTO_NEAR = 2;
/** The first frame a start can be (closer to the clip's first is no trim). */
export const START_MIN = AUTO_NEAR + 1;
/** A drag snaps to a point this close (px). */
export const SNAP_PX = 8;

export interface EndFacts {
  /** The automatic cut (a main scene's Veo tail flash), or null. */
  auto: number | null;
  /** How far the clip really runs (frames): the ruler's reach. */
  extent: number;
  /** Where the scene starts (frames; absent or 0: the clip's first): the
   *  end stays 1.0 s after it. */
  start?: number;
}

/** The automatic end: the tail-flash cut, else the whole clip. */
export function autoEnd(f: EndFacts): number {
  return f.auto ?? f.extent;
}

/** Where the scene ends now (frames): the user's end, else automatic. */
export function endNow(user: number | null | undefined, f: EndFacts): number {
  return user ?? autoEnd(f);
}

/** The grip's range (frames): 1.0 s (after the start, when there is one)
 *  to the clip's real end. */
export function endRange(f: EndFacts): { min: number; max: number } {
  return { min: Math.min((f.start ?? 0) + MIN_END, f.extent), max: f.extent };
}

export function clampEnd(frame: number, f: EndFacts): number {
  const { min, max } = endRange(f);
  return Math.min(max, Math.max(min, Math.round(frame)));
}

/** Whether the automatic end leaves the scene 1.0 s after its start (always,
 *  with no start: as ever). */
export function autoFits(f: EndFacts): boolean {
  return !f.start || autoEnd(f) - f.start >= MIN_END;
}

/** What a drop stores: in range, and null (automatic) within AUTO_NEAR
 *  frames of the automatic end — when the automatic end fits after the
 *  start. `near` 0: only exactly on it (a key). */
export function dropEnd(frame: number, f: EndFacts, near = AUTO_NEAR): number | null {
  const at = clampEnd(frame, f);
  return autoFits(f) && Math.abs(at - autoEnd(f)) <= near ? null : at;
}

/** What "automatic" (a double-click, 0, Backspace) stores: null — or, when
 *  a start leaves the automatic end too close, the nearest end that fits. */
export function endReset(f: EndFacts): number | null {
  return autoFits(f) ? null : clampEnd(autoEnd(f), f);
}

/** A dragged frame, pulled to the nearest snap point within `within` px. */
export function snapEnd(frame: number, points: readonly number[], pxPerFrame: number, within = SNAP_PX): number {
  let best = frame;
  let gap = Infinity;
  for (const p of points) {
    const px = Math.abs(p - frame) * pxPerFrame;
    if (px <= within && px < gap) {
      gap = px;
      best = p;
    }
  }
  return best;
}

/** Where a drag snaps (frames, in range, once each): the automatic cut,
 *  each part's start, the pauses between words (a word's onset: what was
 *  said before it is over) and the end of speech. */
export function endSnaps(
  f: EndFacts,
  o: { parts?: readonly number[]; words?: readonly number[]; voicedEnd?: number | null } = {},
): number[] {
  const { min, max } = endRange(f);
  const secs = [...(o.parts ?? []), ...(o.words ?? []), ...(o.voicedEnd != null ? [o.voicedEnd] : [])];
  const frames = [...(f.auto != null ? [f.auto] : []), ...secs.map((s) => Math.round(s * FPS))];
  return [...new Set(frames.filter((x) => x >= min && x <= max))].sort((a, b) => a - b);
}

/** The grip's keys: ←/→ one frame (Shift: 10), Home / End the limits, 0 or
 *  Backspace automatic. `undefined`: not one of its keys. A step that lands
 *  exactly on the automatic end is automatic. */
export function keyEnd(
  key: string,
  shift: boolean,
  user: number | null | undefined,
  f: EndFacts,
): { end: number | null; nudge: boolean } | undefined {
  const now = endNow(user, f);
  const { min, max } = endRange(f);
  if (key === "ArrowLeft" || key === "ArrowRight") {
    const step = (shift ? 10 : 1) * (key === "ArrowLeft" ? -1 : 1);
    return { end: dropEnd(now + step, f, 0), nudge: true };
  }
  if (key === "Home") return { end: dropEnd(min, f, 0), nudge: false };
  if (key === "End") return { end: dropEnd(max, f, 0), nudge: false };
  if (key === "0" || key === "Backspace") return { end: endReset(f), nudge: false };
  return undefined;
}

// --- the start ----------------------------------------------------------------------

/** The start grip's range (frames): the clip's first frame to 1.0 s before
 *  the scene's end (`end`: where it ends now). */
export function startRange(end: number): { min: number; max: number } {
  return { min: 0, max: Math.max(0, end - MIN_END) };
}

export function clampStart(frame: number, end: number): number {
  const { min, max } = startRange(end);
  return Math.min(max, Math.max(min, Math.round(frame)));
}

/** What a drop stores: in range, and null (no trim) within AUTO_NEAR frames
 *  of the clip's first frame (the server stores those as none too). */
export function dropStart(frame: number, end: number, near = AUTO_NEAR): number | null {
  const at = clampStart(frame, end);
  return at <= near ? null : at;
}

/** Where a drag snaps (frames, in range, once each): the clip's first
 *  frame, each part's start, each word's onset (a cut there starts on the
 *  word) and where speech starts. */
export function startSnaps(
  end: number,
  o: { parts?: readonly number[]; words?: readonly number[]; voicedStart?: number | null } = {},
): number[] {
  const { min, max } = startRange(end);
  const secs = [...(o.parts ?? []), ...(o.words ?? []), ...(o.voicedStart != null ? [o.voicedStart] : [])];
  const frames = [0, ...secs.map((s) => Math.round(s * FPS))];
  return [...new Set(frames.filter((x) => x >= min && x <= max))].sort((a, b) => a - b);
}

/** The start grip's keys: ←/→ one frame (Shift: 10), Home, 0 or Backspace
 *  no trim, End the latest it can be. `undefined`: not one of its keys. A
 *  step into the first AUTO_NEAR frames is no trim going left, and the
 *  first start kept (START_MIN) going right — else → from no trim would
 *  never get anywhere. */
export function keyStart(
  key: string,
  shift: boolean,
  user: number | null | undefined,
  end: number,
): { start: number | null; nudge: boolean } | undefined {
  const { max } = startRange(end);
  if (key === "ArrowLeft" || key === "ArrowRight") {
    const step = (shift ? 10 : 1) * (key === "ArrowLeft" ? -1 : 1);
    let at = clampStart((user ?? 0) + step, end);
    if (at <= AUTO_NEAR) at = step > 0 && max >= START_MIN ? START_MIN : 0;
    return { start: at > 0 ? at : null, nudge: true };
  }
  if (key === "End") return { start: dropStart(max, end), nudge: false };
  if (key === "Home" || key === "0" || key === "Backspace") return { start: null, nudge: false };
  return undefined;
}

/** The out-point an end makes on a clip of `frames` frames: the user's end
 *  when it is before the clip's own last frame, else null — the whole clip,
 *  exactly as ever (the server's _cut_n). */
export function cutOf(user: number | null | undefined, frames: number | null | undefined): number | null {
  if (user == null || !frames) return null;
  return user < frames ? user : null;
}

/** Frames as the server says seconds (duration_sec: 2 decimals). */
export function framesSec(frames: number): number {
  return Math.round((frames / FPS) * 100) / 100;
}

/** The scene's seconds the studio works in: cut short by the end, else
 *  exactly what the server said for the whole clip. */
export function sceneSeconds(whole: number, cut: number | null): number {
  return cut != null ? framesSec(cut) : whole;
}

/** The grip's bubble: "7.50 s". */
export function endBubble(frames: number): string {
  return `${(frames / FPS).toFixed(2)} s`;
}

/** Whether Generate waits for the whole clip: the scene's end is past the
 *  automatic cut, and the studio isn't on the whole clip yet (`onWhole`)
 *  though it can ask for it (`canAsk`) and this server has it (`whole` is
 *  not "none"). A version made of the cut clip would end short of the end
 *  kept, and nothing would say so. */
export function wantsWhole(o: {
  end: number | null | undefined;
  auto: number | null | undefined;
  onWhole: boolean;
  canAsk: boolean;
  whole: "preparing" | "ready" | "none" | "failed" | null | undefined;
}): boolean {
  return o.end != null && o.auto != null && o.end > o.auto && !o.onWhole && o.canAsk && o.whole !== "none";
}
