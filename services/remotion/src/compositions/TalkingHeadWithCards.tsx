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
 * Talking-head Yas composition: [intro video] + [main Veo video] + [outro video].
 *
 * All three segments are now Veo clips (the previous v1 had still cards for
 * intro/outro). Each segment plays at its native duration; fades sit on the
 * seams between segments. The audio track of the main clip carries the
 * dialogue; intro and outro are silent by convention.
 *
 * Intro and outro are optional — when their `*_video_path` is null, that
 * segment is skipped and the composition gets shorter. The main video is
 * always required.
 *
 * All `*_video_path` props are URLs (Remotion's renderer can't read file://
 * reliably in headless Chrome). The Express server mounts the backend
 * `data/` directory at /data so the poller passes
 * `http://127.0.0.1:3001/data/<rel-path>`.
 */

export type TalkingHeadWithCardsProps = {
  // Field names kept as `*_path` for backward compatibility with the
  // poller's existing payload shape. Values are HTTP URLs (see above).
  intro_image_path: string | null;   // legacy still-image fallback (unused in v2)
  intro_video_path?: string | null;  // primary input in v2
  main_video_path: string;
  outro_image_path: string | null;   // legacy still-image fallback (unused in v2)
  outro_video_path?: string | null;  // primary input in v2

  intro_duration_sec: number;
  outro_duration_sec: number;
  fade_sec: number;
  main_duration_sec?: number;
};

const FPS = 30;
const DEFAULT_MAIN_SEC = 8;
const DEFAULT_BOOKEND_SEC = 4;


function pickIntroSrc(props: TalkingHeadWithCardsProps): string | null {
  return props.intro_video_path || props.intro_image_path || null;
}

function pickOutroSrc(props: TalkingHeadWithCardsProps): string | null {
  return props.outro_video_path || props.outro_image_path || null;
}


export const talkingHeadCalculateMetadata: CalculateMetadataFunction<
  TalkingHeadWithCardsProps
> = ({ props }) => {
  const introSrc = pickIntroSrc(props);
  const outroSrc = pickOutroSrc(props);
  const intro = introSrc
    ? Math.max(0.1, props.intro_duration_sec || DEFAULT_BOOKEND_SEC)
    : 0;
  const outro = outroSrc
    ? Math.max(0.1, props.outro_duration_sec || DEFAULT_BOOKEND_SEC)
    : 0;
  const main = Math.max(0.5, props.main_duration_sec || DEFAULT_MAIN_SEC);
  const fade = Math.max(0, props.fade_sec || 0);
  const seams = (intro > 0 ? 1 : 0) + (outro > 0 ? 1 : 0);
  const totalSec = Math.max(0.5, intro + main + outro - seams * fade);
  return {
    durationInFrames: Math.max(1, Math.ceil(totalSec * FPS)),
    fps: FPS,
    width: 1080,
    height: 1920,
  };
};


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


export const TalkingHeadWithCards: React.FC<TalkingHeadWithCardsProps> = (props) => {
  const introSrc = pickIntroSrc(props);
  const outroSrc = pickOutroSrc(props);
  const { main_video_path, fade_sec, main_duration_sec } = props;
  const introSec = introSrc
    ? Math.max(0.1, props.intro_duration_sec || DEFAULT_BOOKEND_SEC)
    : 0;
  const outroSec = outroSrc
    ? Math.max(0.1, props.outro_duration_sec || DEFAULT_BOOKEND_SEC)
    : 0;
  const mainSec = Math.max(0.5, main_duration_sec || DEFAULT_MAIN_SEC);
  const { fps } = useVideoConfig();
  const fadeFrames = Math.max(0, Math.round((fade_sec || 0) * fps));

  const introFrames = Math.max(1, Math.round(introSec * fps));
  const mainFrames = Math.max(1, Math.round(mainSec * fps));
  const outroFrames = Math.max(1, Math.round(outroSec * fps));

  let cursor = 0;
  const introStart = cursor;
  if (introSrc && introFrames > 0) {
    cursor += introFrames - fadeFrames;
  }
  const mainStart = cursor;
  cursor += mainFrames - (outroSrc ? fadeFrames : 0);
  const outroStart = cursor;

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {introSrc && introFrames > 0 ? (
        <Sequence from={introStart} durationInFrames={introFrames}>
          <FadingFrame
            durationFrames={introFrames}
            fadeInFrames={fadeFrames}
            fadeOutFrames={fadeFrames}
          >
            <AbsoluteFill>
              <OffthreadVideo
                src={introSrc}
                style={{ width: "100%", height: "100%", objectFit: "cover" }}
              />
            </AbsoluteFill>
          </FadingFrame>
        </Sequence>
      ) : null}

      <Sequence from={mainStart} durationInFrames={mainFrames}>
        <FadingFrame
          durationFrames={mainFrames}
          fadeInFrames={introSrc ? fadeFrames : 0}
          fadeOutFrames={outroSrc ? fadeFrames : 0}
        >
          <AbsoluteFill>
            <OffthreadVideo
              src={main_video_path}
              style={{ width: "100%", height: "100%", objectFit: "cover" }}
            />
          </AbsoluteFill>
        </FadingFrame>
      </Sequence>

      {outroSrc && outroFrames > 0 ? (
        <Sequence from={outroStart} durationInFrames={outroFrames}>
          <FadingFrame
            durationFrames={outroFrames}
            fadeInFrames={fadeFrames}
            fadeOutFrames={fadeFrames}
          >
            <AbsoluteFill>
              <OffthreadVideo
                src={outroSrc}
                style={{ width: "100%", height: "100%", objectFit: "cover" }}
              />
            </AbsoluteFill>
          </FadingFrame>
        </Sequence>
      ) : null}
    </AbsoluteFill>
  );
};
