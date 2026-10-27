// Rebrand studio — "Show camera tracking": how the camera moves over the
// scene, drawn over the stage (a check for the in-house engine, from the ⋯
// menu).
//
// The server follows the camera once per scene (GET /house/camera): for
// every frame a similarity, M = [[a, -b, tx], [b, a, ty]], taking a pixel of
// frame 0 to where that spot of the room is on frame i. Drawn here: frame
// 0's middle box (a fifth in from each edge), dashed where it started, and
// solid where the camera has carried it by the moment on screen. On a slow
// push-in (run 815: ~12% over 8 s) the solid box grows with the room; a
// pattern painted in the room will do exactly that in the in-house video.
// A still is frame 0, where the two boxes are one.

import { useEffect, useState, type RefObject } from "react";

import type { HarnessCamera, HarnessCameraTrack } from "../../../lib/rebrandHarnessApi";
import { COPY } from "./studioCopy";

/** Frame 0's middle box, as fractions of the frame. */
const INSET = 0.2;

/** The frame on screen: the video's own time while one shows (followed
 *  each animation frame; an unchanged frame renders nothing), else 0. */
function useFrame(track: HarnessCameraTrack | null, videoRef: RefObject<HTMLVideoElement | null>, video: boolean) {
  const [frame, setFrame] = useState(0);
  useEffect(() => {
    if (!track) return;
    const at = () => {
      const t = video ? videoRef.current?.currentTime ?? 0 : 0;
      return Math.min(track.n - 1, Math.max(0, Math.round(t * track.fps)));
    };
    setFrame(at());
    if (!video) return;
    let raf = 0;
    const tick = () => {
      setFrame(at());
      raf = window.requestAnimationFrame(tick);
    };
    raf = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(raf);
  }, [track, videoRef, video]);
  return frame;
}

/** Frame 0's point (x, y) on frame i. */
function carry(m: [number, number, number, number], x: number, y: number): [number, number] {
  const [a, b, tx, ty] = m;
  return [a * x - b * y + tx, b * x + a * y + ty];
}

export function CameraOverlay({
  camera,
  videoRef,
  video,
}: {
  /** null while it is first asked for. */
  camera: HarnessCamera | null;
  /** The stage's video element, while `video` (one shows). */
  videoRef: RefObject<HTMLVideoElement | null>;
  video: boolean;
}) {
  const track = camera && camera.state === "ready" ? camera : null;
  const frame = useFrame(track, videoRef, video);

  let chip: string;
  if (track) {
    if (track.method === "still") {
      chip = COPY.cameraStill;
    } else {
      const [a, b] = track.m[frame] ?? [1, 0];
      chip = COPY.cameraZoom(Math.hypot(a, b));
      if (track.held_from_sec != null) chip += ` · ${COPY.cameraHeld(track.held_from_sec)}`;
    }
  } else if (camera?.state === "failed") {
    chip = COPY.cameraFailed;
  } else {
    const f = camera && camera.state !== "ready" ? camera.speaker_frames : { done: 0, total: 0 };
    chip = COPY.cameraWorking(f.done, f.total);
  }

  let box: string | null = null;
  let start: string | null = null;
  if (track) {
    const W = track.width;
    const H = track.height;
    const corners: [number, number][] = [
      [INSET * W, INSET * H],
      [(1 - INSET) * W, INSET * H],
      [(1 - INSET) * W, (1 - INSET) * H],
      [INSET * W, (1 - INSET) * H],
    ];
    const m = track.m[frame] ?? [1, 0, 0, 0];
    start = corners.map(([x, y]) => `${x},${y}`).join(" ");
    box = corners.map(([x, y]) => carry(m, x, y).join(",")).join(" ");
  }

  return (
    <>
      {track && box && start && (
        <svg
          aria-label={COPY.cameraBox}
          role="img"
          className="pointer-events-none absolute inset-0 h-full w-full"
          viewBox={`0 0 ${track.width} ${track.height}`}
          preserveAspectRatio="none"
        >
          <polygon points={start} fill="none" stroke="rgba(255,255,255,0.7)" strokeWidth={3} strokeDasharray="14 10" />
          <polygon points={box} fill="none" className="stroke-accent" strokeWidth={5} />
        </svg>
      )}
      <span
        role="status"
        className="pointer-events-none absolute right-2 top-2 max-w-[calc(100%-1rem)] truncate whitespace-nowrap rounded-full bg-black/65 px-2.5 py-1 font-mono text-2xs tabular-nums text-white shadow-soft"
      >
        {chip}
      </span>
    </>
  );
}
