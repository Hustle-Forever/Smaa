import { Composition, registerRoot } from "remotion";

import { Concat, conceptCalculateMetadata } from "./compositions/Concat";
import {
  TalkingHeadWithCards,
  talkingHeadCalculateMetadata,
} from "./compositions/TalkingHeadWithCards";
import {
  TalkingHeadMultiScene,
  multiSceneCalculateMetadata,
} from "./compositions/TalkingHeadMultiScene";
import {
  TalkingHeadV2WithCards,
  talkingHeadV2CalculateMetadata,
} from "./compositions/TalkingHeadV2WithCards";
import {
  TalkingHeadV2MultiScene,
  multiSceneV2CalculateMetadata,
} from "./compositions/TalkingHeadV2MultiScene";
import { VideoEdit, videoEditCalculateMetadata } from "./compositions/VideoEdit";
import {
  PatternTransition,
  patternTransitionMetadata,
} from "./compositions/PatternTransition";
import {
  BrandOverlay42,
  brandOverlayCalculateMetadata,
  type BrandOverlayProps,
} from "./compositions/BrandOverlay42";

// Design-time defaults for `brand-overlay-42` (Remotion Studio only; real
// renders always pass props). A square TH reel letterboxed in 9:16: 16:9
// intro card -> two scenes -> outro card, every seam type once.
const BRAND_OVERLAY_DEFAULTS: BrandOverlayProps = {
  width: 1080,
  height: 1920,
  fps: 30,
  durationInFrames: 450,
  segments: [
    { kind: "intro", index: 0, from: 0, frames: 120, content: { x: 0, y: 0.323, w: 1, h: 0.352 }, layout: "bars" },
    { kind: "scene", index: 0, from: 120, frames: 120, content: { x: 0, y: 0.219, w: 1, h: 0.562 }, layout: "bars" },
    { kind: "scene", index: 1, from: 240, frames: 120, content: { x: 0, y: 0.219, w: 1, h: 0.562 }, layout: "bars" },
    { kind: "outro", index: 0, from: 360, frames: 90, content: { x: 0, y: 0.323, w: 1, h: 0.352 }, layout: "bars" },
  ],
  seams: [
    { frame: 120, style: "cover", from_kind: "intro", to_kind: "scene" },
    { frame: 240, style: "handoff", from_kind: "scene", to_kind: "scene" },
    { frame: 360, style: "cover", from_kind: "scene", to_kind: "outro" },
  ],
  text: { heading: "Myth", sub: "You need to start coding young." },
  logo: { on_cards: false },
};

/**
 * Registers all available compositions. Add new template components here as
 * the service grows beyond the v4 "concat" scope.
 */
export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="Concat"
        component={Concat}
        // Per-render metadata (durationInFrames + fps) is computed from props
        // via calculateMetadata; these are the design-time defaults shown in
        // Remotion Studio when you preview with `npm run studio`.
        durationInFrames={30 * 16}
        fps={30}
        width={1280}
        height={720}
        defaultProps={{
          clips: [
            { url: "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4", durationSec: 8 },
            { url: "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4", durationSec: 8 },
          ],
          crossfadeSec: 0.5,
        }}
        calculateMetadata={conceptCalculateMetadata}
      />
      <Composition
        id="talking-head-with-cards"
        component={TalkingHeadWithCards}
        durationInFrames={30 * 12}
        fps={30}
        width={1080}
        height={1920}
        defaultProps={{
          intro_image_path: null,
          intro_video_path: null,
          main_video_path:
            "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
          outro_image_path: null,
          outro_video_path: null,
          intro_duration_sec: 4,
          outro_duration_sec: 4,
          fade_sec: 0.4,
          main_duration_sec: 8,
        }}
        calculateMetadata={talkingHeadCalculateMetadata}
      />
      <Composition
        id="talking-head-multi-scene"
        component={TalkingHeadMultiScene}
        // Design-time default: 3 scenes × 8s + 4s intro + 4s outro - 4 seams × 0.4s fade ≈ 30.4s.
        durationInFrames={30 * 31}
        fps={30}
        width={1080}
        height={1920}
        defaultProps={{
          intro_video_path: null,
          outro_video_path: null,
          scenes: [
            {
              path: "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
              duration_sec: 8,
            },
            {
              path: "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
              duration_sec: 8,
            },
            {
              path: "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
              duration_sec: 8,
            },
          ],
          intro_duration_sec: 4,
          outro_duration_sec: 4,
          fade_sec: 0.4,
          audio_fade_sec: 0.2,
        }}
        calculateMetadata={multiSceneCalculateMetadata}
      />
      <Composition
        id="talking-head-v2-with-cards"
        component={TalkingHeadV2WithCards}
        durationInFrames={30 * 12}
        fps={30}
        width={1080}
        height={1920}
        defaultProps={{
          intro_image_path: null,
          intro_video_path: null,
          main_video_path:
            "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
          outro_image_path: null,
          outro_video_path: null,
          intro_duration_sec: 4,
          outro_duration_sec: 4,
          fade_sec: 0.4,
          main_duration_sec: 8,
        }}
        calculateMetadata={talkingHeadV2CalculateMetadata}
      />
      <Composition
        id="talking-head-v2-multi-scene"
        component={TalkingHeadV2MultiScene}
        durationInFrames={30 * 31}
        fps={30}
        width={1080}
        height={1920}
        defaultProps={{
          intro_video_path: null,
          outro_video_path: null,
          scenes: [
            {
              path: "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
              duration_sec: 8,
            },
            {
              path: "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
              duration_sec: 8,
            },
            {
              path: "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
              duration_sec: 8,
            },
          ],
          intro_duration_sec: 4,
          outro_duration_sec: 4,
          fade_sec: 0.4,
          audio_fade_sec: 0.2,
        }}
        calculateMetadata={multiSceneV2CalculateMetadata}
      />
      <Composition
        id="video-edit"
        component={VideoEdit}
        // Design-time defaults; real width/height/duration come from props via
        // calculateMetadata (shared with the frontend <Player>).
        durationInFrames={30 * 10}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={{
          project: {
            title: "Untitled project",
            aspect: "16:9" as const,
            width: 1920,
            height: 1080,
            fps: 30,
            background: "#000000",
            durationInFrames: 30 * 8,
          },
          tracks: [
            { id: "trk_v0", kind: "video" as const, index: 0 },
            { id: "trk_a0", kind: "audio" as const, index: 100 },
          ],
          clips: [
            {
              id: "clp_demo",
              trackId: "trk_v0",
              source: {
                kind: "upload" as const,
                url: "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
              },
              sourceUrl:
                "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
              trimBefore: 0,
              trimAfter: 30 * 8,
              from: 0,
              durationInFrames: 30 * 8,
            },
          ],
        }}
        calculateMetadata={videoEditCalculateMetadata}
      />
      <Composition
        id="pattern-transition"
        component={PatternTransition}
        // Everything real arrives in props: the two clips, and the recipe the
        // planner wrote for this particular seam. These defaults exist only so
        // the composition opens in `remotion studio` without a backend.
        durationInFrames={30 * 3}
        fps={30}
        width={720}
        height={1280}
        defaultProps={{
          introUrl: null,
          mainUrl:
            "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
          introTrimBeforeSec: 0,
          patterns: [],
          recipe: {
            style: "preview",
            duration_sec: 3,
            cut_at_sec: 0.8,
            palette: {
              from: "#de423e",
              to: "#3c3386",
              ink: "#f4f4f8",
              flash: "#ffd9d8",
            },
            flash: { at: 0.27, strength: 0.3 },
            camera: { shake: 0.18, push: 0.1 },
            grain: 0.06,
            vignette: 0.12,
            layers: [],
          },
        }}
        calculateMetadata={patternTransitionMetadata}
      />
      <Composition
        id="brand-overlay-42"
        component={BrandOverlay42}
        // Graphics only, on a transparent canvas: rendered as a PNG sequence
        // and overlaid on the untouched footage by the backend's ffmpeg pass.
        // Size, fps and length always come from props (calculateMetadata).
        durationInFrames={BRAND_OVERLAY_DEFAULTS.durationInFrames}
        fps={30}
        width={1080}
        height={1920}
        defaultProps={BRAND_OVERLAY_DEFAULTS}
        calculateMetadata={brandOverlayCalculateMetadata}
      />
    </>
  );
};

// Bundler entry: must call registerRoot at the top level of the file passed
// to `bundle(entryPoint=...)`.
registerRoot(RemotionRoot);
