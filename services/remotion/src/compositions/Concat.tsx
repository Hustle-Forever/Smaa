import {
  AbsoluteFill,
  CalculateMetadataFunction,
  interpolate,
  OffthreadVideo,
  Sequence,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

export type Clip = {
  url: string;
  durationSec: number;
};

export type ConcatProps = {
  clips: Clip[];
  crossfadeSec: number;
};

const FPS = 30;

/**
 * Compute total duration from clips + crossfade overlap so Remotion picks the
 * right frame count without us having to pre-compute on the server.
 *
 *   total_sec = sum(clip durations) - (N-1) * crossfade
 */
export const conceptCalculateMetadata: CalculateMetadataFunction<ConcatProps> = ({ props }) => {
  const clips = props.clips || [];
  const crossfade = Math.max(0, props.crossfadeSec || 0);
  const sum = clips.reduce((acc, c) => acc + (c.durationSec || 0), 0);
  const overlap = Math.max(0, clips.length - 1) * crossfade;
  const totalSec = Math.max(0.1, sum - overlap);
  return {
    durationInFrames: Math.max(1, Math.ceil(totalSec * FPS)),
    fps: FPS,
  };
};

/**
 * Concat plays an ordered list of clips back-to-back with optional crossfade
 * overlap between adjacent clips. v4 uses two clips (parent + extension); the
 * implementation is N-clip-safe so v5 chains will work without code changes.
 */
export const Concat: React.FC<ConcatProps> = ({ clips, crossfadeSec }) => {
  const { fps } = useVideoConfig();
  const crossfadeFrames = Math.max(0, Math.round((crossfadeSec || 0) * fps));

  // Walk the timeline placing each clip so its tail overlaps the next clip's
  // head by `crossfadeFrames` frames. The first clip starts at 0; each
  // subsequent clip starts `crossfadeFrames` before the previous one ends.
  let cursor = 0;
  const placements = clips.map((clip, i) => {
    const clipFrames = Math.max(1, Math.round((clip.durationSec || 0) * fps));
    const startFrame = cursor;
    // Advance cursor by clip length minus the crossfade overlap with the NEXT
    // clip (not the last clip — it plays through fully).
    cursor += clipFrames - (i < clips.length - 1 ? crossfadeFrames : 0);
    return { startFrame, clipFrames, url: clip.url, isFirst: i === 0, isLast: i === clips.length - 1 };
  });

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {placements.map((p, i) => (
        <Sequence
          key={i}
          from={p.startFrame}
          durationInFrames={p.clipFrames}
        >
          <FadingClip
            url={p.url}
            clipFrames={p.clipFrames}
            crossfadeFrames={crossfadeFrames}
            fadeIn={!p.isFirst}
            fadeOut={!p.isLast}
          />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};

/**
 * Renders one clip with optional fade-in (when crossfading from a prior clip)
 * and fade-out (when the next clip will fade in on top). Boundaries that don't
 * crossfade just hard-cut.
 */
const FadingClip: React.FC<{
  url: string;
  clipFrames: number;
  crossfadeFrames: number;
  fadeIn: boolean;
  fadeOut: boolean;
}> = ({ url, clipFrames, crossfadeFrames, fadeIn, fadeOut }) => {
  const frame = useCurrentFrame(); // 0..clipFrames-1 within this Sequence

  let opacity = 1;
  if (fadeIn && crossfadeFrames > 0 && frame < crossfadeFrames) {
    opacity = interpolate(frame, [0, crossfadeFrames], [0, 1], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    });
  } else if (fadeOut && crossfadeFrames > 0 && frame >= clipFrames - crossfadeFrames) {
    opacity = interpolate(
      frame,
      [clipFrames - crossfadeFrames, clipFrames],
      [1, 0],
      { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
    );
  }

  return (
    <AbsoluteFill style={{ opacity }}>
      <OffthreadVideo src={url} />
    </AbsoluteFill>
  );
};
