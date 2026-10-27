import {
  AbsoluteFill,
  CalculateMetadataFunction,
  interpolate,
  OffthreadVideo,
  Sequence,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

/**
 * Multi-scene talking-head composition (v6).
 *
 * Layout (variable number of segments):
 *   [intro 4s] -> [scene 1 8s] -> [scene 2 8s] -> ... -> [scene N 8s] -> [outro 4s]
 *
 * Each adjacent pair shares a crossfade of `fade_sec` seconds (visual
 * opacity fade). The audio carried in each scene's video track gets a
 * matching short audio fade-in / fade-out (0.2 s by default) to avoid
 * pops at scene cuts.
 *
 * intro_video_path / outro_video_path can be null when the user skipped
 * a bookend; the composition adapts seamlessly. `scenes` must contain at
 * least 1 entry (multi-scene runs guarantee 2+, but the math works at 1
 * if some upstream caller wires it that way).
 */

export type MultiSceneEntry = {
  path: string;            // HTTP URL the Remotion server can fetch
  duration_sec: number;    // typically 8s for main scenes
};

export type TalkingHeadV2MultiSceneProps = {
  intro_video_path?: string | null;
  outro_video_path?: string | null;
  scenes: MultiSceneEntry[];
  intro_duration_sec: number;
  outro_duration_sec: number;
  fade_sec: number;
  /** Audio fade window at every seam, seconds. Defaults to 0.2. */
  audio_fade_sec?: number;
  width?: number;
  height?: number;
};

const FPS = 30;
const DEFAULT_BOOKEND_SEC = 4;
const DEFAULT_AUDIO_FADE_SEC = 0.2;


function buildSegments(props: TalkingHeadV2MultiSceneProps): {
  src: string;
  durSec: number;
  silent: boolean;
}[] {
  const segments: { src: string; durSec: number; silent: boolean }[] = [];
  if (props.intro_video_path) {
    segments.push({
      src: props.intro_video_path,
      durSec: Math.max(0.1, props.intro_duration_sec || DEFAULT_BOOKEND_SEC),
      silent: true, // bookends are silent by convention
    });
  }
  for (const s of props.scenes || []) {
    if (!s?.path) continue;
    segments.push({
      src: s.path,
      durSec: Math.max(0.5, s.duration_sec || 8),
      silent: false,
    });
  }
  if (props.outro_video_path) {
    segments.push({
      src: props.outro_video_path,
      durSec: Math.max(0.1, props.outro_duration_sec || DEFAULT_BOOKEND_SEC),
      silent: true,
    });
  }
  return segments;
}


export const multiSceneV2CalculateMetadata: CalculateMetadataFunction<
  TalkingHeadV2MultiSceneProps
> = ({ props }) => {
  const segments = buildSegments(props);
  const fade = Math.max(0, props.fade_sec || 0);
  const seams = Math.max(0, segments.length - 1);
  const totalSec =
    segments.reduce((acc, s) => acc + s.durSec, 0) - seams * fade;
  return {
    durationInFrames: Math.max(1, Math.ceil(Math.max(0.5, totalSec) * FPS)),
    fps: FPS,
    width: props.width || 1080,
    height: props.height || 1920,
  };
};


/** Visual fade-in + fade-out wrapper. Same pattern as TalkingHeadWithCards. */
const FadingFrame: React.FC<{
  durationFrames: number;
  fadeInFrames: number;
  fadeOutFrames: number;
  children: React.ReactNode;
}> = ({ durationFrames, fadeInFrames, fadeOutFrames, children }) => {
  const frame = useCurrentFrame();
  let opacity = 1;
  if (fadeInFrames > 0 && frame < fadeInFrames) {
    opacity = interpolate(frame, [0, fadeInFrames], [0, 1], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    });
  } else if (
    fadeOutFrames > 0 &&
    frame >= durationFrames - fadeOutFrames
  ) {
    opacity = interpolate(
      frame,
      [durationFrames - fadeOutFrames, durationFrames],
      [1, 0],
      { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
    );
  }
  return <AbsoluteFill style={{ opacity }}>{children}</AbsoluteFill>;
};


/** Computes the per-frame audio volume for a clip with short audio fades
 *  at its head + tail. Skips audio entirely when `silent` is true. */
function audioVolume({
  silent,
  audioFadeInFrames,
  audioFadeOutFrames,
  durationFrames,
}: {
  silent: boolean;
  audioFadeInFrames: number;
  audioFadeOutFrames: number;
  durationFrames: number;
}): (frame: number) => number {
  if (silent) return () => 0;
  return (frame: number) => {
    if (audioFadeInFrames > 0 && frame < audioFadeInFrames) {
      return interpolate(frame, [0, audioFadeInFrames], [0, 1], {
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
      });
    }
    if (
      audioFadeOutFrames > 0 &&
      frame >= durationFrames - audioFadeOutFrames
    ) {
      return interpolate(
        frame,
        [durationFrames - audioFadeOutFrames, durationFrames],
        [1, 0],
        { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
      );
    }
    return 1;
  };
}


export const TalkingHeadV2MultiScene: React.FC<TalkingHeadV2MultiSceneProps> = (
  props,
) => {
  const segments = buildSegments(props);
  const { fps } = useVideoConfig();
  const fadeFrames = Math.max(0, Math.round((props.fade_sec || 0) * fps));
  const audioFadeFrames = Math.max(
    0,
    Math.round((props.audio_fade_sec ?? DEFAULT_AUDIO_FADE_SEC) * fps),
  );

  // Compute each segment's start frame, overlapping by `fadeFrames` at
  // every seam.
  const segMeta: {
    src: string;
    silent: boolean;
    durFrames: number;
    startFrame: number;
    hasFadeIn: boolean;
    hasFadeOut: boolean;
  }[] = [];
  let cursor = 0;
  for (let i = 0; i < segments.length; i++) {
    const s = segments[i];
    const durFrames = Math.max(1, Math.round(s.durSec * fps));
    const hasFadeIn = i > 0;
    const hasFadeOut = i < segments.length - 1;
    segMeta.push({
      src: s.src,
      silent: s.silent,
      durFrames,
      startFrame: cursor,
      hasFadeIn,
      hasFadeOut,
    });
    // Advance cursor by (durFrames - fadeFrames) for all segments except
    // the last, so the next segment starts inside the current's tail.
    if (i < segments.length - 1) {
      cursor += durFrames - fadeFrames;
    } else {
      cursor += durFrames;
    }
  }

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {segMeta.map((m, i) => (
        <Sequence
          key={i}
          from={m.startFrame}
          durationInFrames={m.durFrames}
        >
          <FadingFrame
            durationFrames={m.durFrames}
            fadeInFrames={m.hasFadeIn ? fadeFrames : 0}
            fadeOutFrames={m.hasFadeOut ? fadeFrames : 0}
          >
            <AbsoluteFill>
              <SceneClip
                src={m.src}
                silent={m.silent}
                durationFrames={m.durFrames}
                audioFadeInFrames={m.hasFadeIn ? audioFadeFrames : 0}
                audioFadeOutFrames={m.hasFadeOut ? audioFadeFrames : 0}
              />
            </AbsoluteFill>
          </FadingFrame>
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};


/** A single video clip with an interpolated audio volume curve. Lives in
 *  its own component so useCurrentFrame() reads the per-Sequence frame
 *  (Remotion resets it inside each <Sequence>). */
const SceneClip: React.FC<{
  src: string;
  silent: boolean;
  durationFrames: number;
  audioFadeInFrames: number;
  audioFadeOutFrames: number;
}> = ({ src, silent, durationFrames, audioFadeInFrames, audioFadeOutFrames }) => {
  const frame = useCurrentFrame();
  const volumeFn = audioVolume({
    silent,
    audioFadeInFrames,
    audioFadeOutFrames,
    durationFrames,
  });
  const volume = volumeFn(frame);
  return (
    <OffthreadVideo
      src={src}
      volume={volume}
      muted={silent}
      style={{ width: "100%", height: "100%", objectFit: "cover" }}
    />
  );
};
