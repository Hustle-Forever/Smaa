/**
 * Talking Head v2 "Video size": when it is fixed, and whether a still is the
 * shape the video is made at.
 *
 * Every still and card is made in exactly the run's shape (9:16 or 16:9), and
 * Veo animates exactly the still the user approved. So once a still or card
 * exists the size cannot change under it: the backend refuses a new size with
 * a 409, and the Look step disables the control. A still made before stills
 * were conformed can still be a little (768x1344) or far (1248x832) off; the
 * tile shows it whole and says the video fits it.
 *
 * Pure functions (checked by scripts/check-th-video-size.mjs).
 */
import type { TalkingHeadRun } from "./types";

export const VIDEO_SIZE_HINT =
  "All stills, cards and the final video are made at this size. It is fixed once stills are made.";
export const VIDEO_SIZE_LOCKED_HINT =
  "Video size is fixed once stills are made. Start a new reel to change it.";

/** Whether the run's Video size can no longer change. The backend says so
 *  (`aspect_locked`); an older backend that does not is read from the run:
 *  any scene still or generated card. */
export function videoSizeLocked(run: TalkingHeadRun | null | undefined): boolean {
  if (!run) return false;
  if (typeof run.aspect_locked === "boolean") return run.aspect_locked;
  return (
    !!run.main?.url ||
    (run.scenes ?? []).some((s) => !!s.keyframe_url) ||
    (run.intro?.id != null && !!run.intro?.url) ||
    (run.outro?.id != null && !!run.outro?.url)
  );
}

/** True when a still of width x height is not exactly `aspect` (9:16 unless
 *  "16:9"). Unknown sizes are never flagged. */
export function stillShapeOff(
  width: number | null | undefined,
  height: number | null | undefined,
  aspect: string | null | undefined,
): boolean {
  if (!width || !height) return false;
  const [aw, ah] = aspect === "16:9" ? [16, 9] : [9, 16];
  return width * ah !== height * aw;
}
