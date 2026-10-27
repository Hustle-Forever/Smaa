import {
  AbsoluteFill,
  CalculateMetadataFunction,
  Easing,
  interpolate,
  OffthreadVideo,
  random,
  Sequence,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import React from "react";

/**
 * One seam, rendered: the tail of the intro card into the head of the talking
 * head, with the 42 pattern kit carrying the cut.
 *
 * WHAT THIS IS NOT: it is not a generated clip. Both halves are the original
 * footage, untouched and frame-accurate. Everything added is a tinted mask of
 * a brand graphic composited on top. That is why it renders in seconds, costs
 * nothing, and cannot drift a face.
 *
 * THE CUT IS LOSSLESS. `main` starts at its own frame 0 exactly at `cutAtSec`,
 * so no dialogue is trimmed, and `intro` plays up to that instant. The
 * transition works by covering the join with a graphic that is already in
 * motion, then sliding it away to uncover the shot — the way the cut is hidden
 * is the same way it would be on a desk in After Effects.
 *
 * Every number comes from the recipe (see transition_planner.py); nothing is
 * decided here. The renderer's only opinions are easing and how each `move`
 * translates into travel.
 */

const FPS = 30;

export type TransitionLayer = {
  pattern: string;
  move: "sweep" | "wipe" | "drift" | "scan" | "stamp";
  direction: "left" | "right" | "up" | "down" | "none";
  start: number;        // 0..1 of the transition
  end: number;          // 0..1
  box: { x: number; y: number; w: number; h: number };
  rotate: number;
  scale: number;
  color_from: string;
  color_to: string;
  opacity: number;
  blend: "normal" | "screen" | "plus-lighter";
  use_art: boolean;
  parallax: number;
  reveal: boolean;      // this layer's exit uncovers the incoming shot
  residue: number;      // opacity it holds at after the transition, 0 = leaves
  residue_end?: number; // 0..1 where that hold has faded to nothing
};

export type TransitionRecipe = {
  style: string;
  rationale?: string;
  duration_sec: number;
  cut_at_sec: number;
  palette: { from: string; to: string; ink: string; flash: string };
  flash: { at: number; strength: number };
  camera: { shake: number; push: number };
  grain: number;
  vignette: number;
  layers: TransitionLayer[];
  safe_zones?: { what?: string; x: number; y: number; w: number; h: number }[];
  /** The strip anything lingering is confined to. The rest of the frame is
   *  the real room and is never drawn on. */
  brand_region?: { side?: string; x: number; y: number; w: number; h: number };
  cover?: "seam" | "shot";
};

export type PatternAsset = {
  slug: string;
  mask: string;         // URL
  art: string | null;   // URL
  aspect: number;
};

export type PatternTransitionProps = {
  introUrl: string | null;
  mainUrl: string;
  /** Seconds into the intro where its tail begins (the part we re-render). */
  introTrimBeforeSec: number;
  recipe: TransitionRecipe;
  patterns: PatternAsset[];
  width?: number;
  height?: number;
  /** Draws the keep-clear boxes. Development only. */
  debugSafeZones?: boolean;
};

export const patternTransitionMetadata: CalculateMetadataFunction<
  PatternTransitionProps
> = ({ props }) => {
  const seconds = Math.max(0.5, props.recipe?.duration_sec || 3);
  return {
    durationInFrames: Math.max(1, Math.round(seconds * FPS)),
    fps: FPS,
    width: props.width || 720,
    height: props.height || 1280,
  };
};

// ---------------------------------------------------------------------------
// Colour
// ---------------------------------------------------------------------------

const parseHex = (hex: string): [number, number, number] => {
  let s = (hex || "").trim().replace("#", "");
  if (s.length === 3) s = s.split("").map((c) => c + c).join("");
  if (s.length !== 6) return [255, 255, 255];
  const n = parseInt(s, 16);
  if (Number.isNaN(n)) return [255, 255, 255];
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
};

const mixHex = (a: string, b: string, t: number): string => {
  const [r1, g1, b1] = parseHex(a);
  const [r2, g2, b2] = parseHex(b);
  const c = (x: number, y: number) => Math.round(x + (y - x) * Math.max(0, Math.min(1, t)));
  return `rgb(${c(r1, r2)}, ${c(g1, g2)}, ${c(b1, b2)})`;
};

// ---------------------------------------------------------------------------
// How a `move` travels
// ---------------------------------------------------------------------------

const AXIS: Record<string, [number, number]> = {
  left: [-1, 0],
  right: [1, 0],
  up: [0, -1],
  down: [0, 1],
  none: [0, 0],
};

/**
 * Offset in frame-fractions for a layer at local progress `p` (0..1).
 *
 * `sweep` and `scan` cross and exit; `wipe` arrives and stays; `drift` breathes
 * around its box; `stamp` does not travel at all (it is revealed by clip-path
 * instead, which reads as being drawn rather than flown in).
 */
const travel = (
  layer: TransitionLayer,
  p: number,
): { dx: number; dy: number; extra: number } => {
  const [ax, ay] = AXIS[layer.direction] ?? AXIS.none;
  const reach = 1.35 + layer.parallax * 0.5;
  switch (layer.move) {
    case "sweep":
    case "scan": {
      // Enters from behind the frame, exits past the far edge.
      const eased = interpolate(p, [0, 1], [-reach, reach], {
        easing: layer.move === "scan" ? Easing.linear : Easing.bezier(0.33, 0, 0.2, 1),
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
      });
      return { dx: ax * eased, dy: ay * eased, extra: 0 };
    }
    case "wipe": {
      // Arrives and settles — overshoots slightly, the way a real card lands.
      const eased = interpolate(p, [0, 0.55, 1], [-reach, 0.03, 0], {
        easing: Easing.bezier(0.16, 1, 0.3, 1),
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
      });
      return { dx: ax * eased, dy: ay * eased, extra: 0 };
    }
    case "drift": {
      const amp = 0.06 + layer.parallax * 0.12;
      const d = interpolate(p, [0, 1], [-amp, amp], { extrapolateRight: "clamp" });
      return { dx: ax * d, dy: ay * d, extra: 0 };
    }
    case "stamp":
    default:
      return { dx: 0, dy: 0, extra: 0 };
  }
};

/**
 * A layer's opacity at this instant — absolute, not a multiplier.
 *
 * Three phases: it arrives, it plays out its span, and then it either goes or
 * HOLDS at `residue` until `residue_end`, where it fades away for good. That
 * last phase is what keeps the branding from becoming a sticker: the panel is
 * supposed to leave, and the real room behind it is supposed to come back.
 */
const alphaFor = (
  layer: TransitionLayer,
  p: number,
  t: number,
  past: boolean,
  durationSec: number,
): number => {
  if (past) {
    const until = layer.residue_end ?? 1;
    if (layer.residue <= 0.002 || t >= until) return 0;
    // Fade out over ~0.6s of real time, so the exit reads the same whether
    // this render is a 3s seam or a 9s shot.
    const fade = Math.min(0.3, 0.6 / Math.max(durationSec, 0.01));
    return (
      layer.residue *
      interpolate(t, [until - fade, until], [1, 0], {
        easing: Easing.in(Easing.quad),
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
      })
    );
  }
  if (p <= 0) return 0;
  const fadeIn = layer.move === "stamp" ? 0.35 : 0.18;
  const rise = interpolate(p, [0, fadeIn], [0, 1], {
    easing: Easing.out(Easing.quad),
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  // Land exactly on `residue` at the end of the span, so the hold phase picks
  // up with no step in opacity.
  const tail =
    layer.residue > 0.02 ? layer.residue / Math.max(layer.opacity, 0.001) : 0;
  const fall = interpolate(p, [1 - 0.22, 1], [1, tail], {
    easing: Easing.in(Easing.quad),
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  return layer.opacity * Math.min(rise, fall);
};

// ---------------------------------------------------------------------------
// A single pattern layer
// ---------------------------------------------------------------------------

const PatternLayerView: React.FC<{
  layer: TransitionLayer;
  asset: PatternAsset;
  frame: number;
  totalFrames: number;
}> = ({ layer, asset, frame, totalFrames }) => {
  const t = totalFrames > 0 ? frame / totalFrames : 0;
  const span = Math.max(0.0001, layer.end - layer.start);
  const rawP = (t - layer.start) / span;
  const past = rawP > 1;
  const p = Math.max(0, Math.min(1, rawP));

  if (rawP < 0) return null;
  const opacity = alphaFor(layer, p, t, past, totalFrames / 30);
  if (opacity <= 0.002) return null;

  const { dx, dy } = travel(layer, p);
  const color = mixHex(layer.color_from, layer.color_to, past ? 1 : p);
  const scale = layer.scale * interpolate(p, [0, 1], [1 - 0.05 * layer.parallax, 1 + 0.05 * layer.parallax], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // `stamp` is revealed by a moving clip edge rather than by travelling, so a
  // schematic looks drawn on rather than slid in.
  const clip =
    layer.move === "stamp"
      ? (() => {
          const cut = interpolate(p, [0, 0.85], [0, 100], {
            easing: Easing.bezier(0.4, 0, 0.2, 1),
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
          });
          switch (layer.direction) {
            case "left":
              return `inset(0 0 0 ${100 - cut}%)`;
            case "right":
              return `inset(0 ${100 - cut}% 0 0)`;
            case "down":
              return `inset(0 0 ${100 - cut}% 0)`;
            default:
              return `inset(${100 - cut}% 0 0 0)`;
          }
        })()
      : undefined;

  const box = layer.box;

  // `box` is the space the layer OCCUPIES ON SCREEN, after any rotation — that
  // is what the planner reasons about and what the safe-zone check measures.
  // A quarter-turn therefore has to swap the art's own dimensions inside that
  // box, or a tall strip authored down the left edge renders as a wide bar
  // across the middle of the frame. (It did, straight across a speaker's eyes,
  // while the safe-zone check happily approved the unrotated box.)
  const quarterTurn = Math.abs(Math.sin((layer.rotate * Math.PI) / 180)) > 0.7;
  const innerW = quarterTurn && box.w > 0 ? (box.h / box.w) * 100 : 100;
  const innerH = quarterTurn && box.h > 0 ? (box.w / box.h) * 100 : 100;

  const outer: React.CSSProperties = {
    position: "absolute",
    left: `${(box.x + dx) * 100}%`,
    top: `${(box.y + dy) * 100}%`,
    width: `${box.w * 100}%`,
    height: `${box.h * 100}%`,
    opacity,
    mixBlendMode: layer.blend === "normal" ? undefined : (layer.blend as never),
    clipPath: clip,
    willChange: "transform, opacity",
  };

  const inner: React.CSSProperties = {
    position: "absolute",
    left: "50%",
    top: "50%",
    width: `${innerW}%`,
    height: `${innerH}%`,
    transform: `translate(-50%, -50%) rotate(${layer.rotate}deg) scale(${scale})`,
    transformOrigin: "center center",
  };

  const paint: React.CSSProperties =
    layer.use_art && asset.art
      ? {
          backgroundImage: `url(${asset.art})`,
          backgroundSize: "100% 100%",
          backgroundRepeat: "no-repeat",
        }
      : {
          // The mask carries the shape, the background carries the colour —
          // which is the whole reason the kit is stored as masks: one file,
          // any brand colour, decided at render time.
          backgroundColor: color,
          WebkitMaskImage: `url(${asset.mask})`,
          maskImage: `url(${asset.mask})`,
          WebkitMaskSize: "100% 100%",
          maskSize: "100% 100%",
          WebkitMaskRepeat: "no-repeat",
          maskRepeat: "no-repeat",
          WebkitMaskPosition: "center",
          maskPosition: "center",
        };

  return (
    <div style={outer}>
      <div style={{ ...inner, ...paint }} />
    </div>
  );
};

// ---------------------------------------------------------------------------
// The composition
// ---------------------------------------------------------------------------

export const PatternTransition: React.FC<PatternTransitionProps> = ({
  introUrl,
  mainUrl,
  introTrimBeforeSec,
  recipe,
  patterns,
  debugSafeZones,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames, width, height } = useVideoConfig();

  const cutFrame = Math.max(
    1,
    Math.min(durationInFrames - 1, Math.round((recipe.cut_at_sec || 0.8) * fps)),
  );
  const byslug = React.useMemo(
    () => Object.fromEntries(patterns.map((p) => [p.slug, p])),
    [patterns],
  );

  // The reveal layer's exit is what uncovers the incoming shot: while it is
  // still travelling away, the shot behind it is masked to the edge it trails.
  const reveal = recipe.layers.find((l) => l.reveal);
  const revealProgress = (() => {
    if (!reveal) return 1;
    const t = durationInFrames > 0 ? frame / durationInFrames : 0;
    const span = Math.max(0.0001, reveal.end - reveal.start);
    return Math.max(0, Math.min(1, (t - reveal.start) / span));
  })();

  const mainMask = (() => {
    if (!reveal || frame < cutFrame) return undefined;
    // Open from the edge the layer is heading towards, trailing just behind it.
    const cutT = durationInFrames > 0 ? cutFrame / durationInFrames : 0;
    const span = Math.max(0.0001, reveal.end - reveal.start);
    const atCut = Math.max(0, Math.min(1, (cutT - reveal.start) / span));
    const open = interpolate(revealProgress, [atCut, Math.min(1, atCut + 0.55)], [0, 1], {
      easing: Easing.bezier(0.33, 0, 0.2, 1),
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    });
    if (open >= 0.999) return undefined;
    const pct = open * 145 - 22;
    const deg =
      reveal.direction === "left" ? 270
        : reveal.direction === "right" ? 90
        : reveal.direction === "down" ? 180
        : 0;
    return `linear-gradient(${deg}deg, rgba(0,0,0,1) ${pct}%, rgba(0,0,0,0) ${pct + 22}%)`;
  })();

  // Energy at the cut: a short push and a settle, so the join has an impulse
  // behind it instead of being a dissolve.
  const sinceCut = (frame - cutFrame) / fps;
  const impulse = Math.exp(-Math.max(0, sinceCut) * 7.5) * (frame >= cutFrame ? 1 : 0);
  const push = 1 + recipe.camera.push * 0.12 * impulse;
  const shakeAmp = recipe.camera.shake * 14 * impulse;
  const shakeX = shakeAmp * (random(`sx${frame}`) - 0.5);
  const shakeY = shakeAmp * (random(`sy${frame}`) - 0.5);

  const flashFrame = (recipe.flash?.at ?? 0) * durationInFrames;
  const flash =
    (recipe.flash?.strength ?? 0) *
    Math.exp(-Math.abs(frame - flashFrame) / 2.6);

  return (
    <AbsoluteFill style={{ backgroundColor: "black", overflow: "hidden" }}>
      <AbsoluteFill
        style={{
          transform: `scale(${push}) translate(${shakeX}px, ${shakeY}px)`,
        }}
      >
        {/* Outgoing: the card, up to the instant of the cut. */}
        {introUrl ? (
          <Sequence from={0} durationInFrames={cutFrame} name="intro tail">
            <AbsoluteFill>
              <OffthreadVideo
                src={introUrl}
                trimBefore={Math.max(0, Math.round(introTrimBeforeSec * fps))}
                style={{ width: "100%", height: "100%", objectFit: "cover" }}
              />
            </AbsoluteFill>
          </Sequence>
        ) : null}

        {/* Incoming: the shot, from ITS frame zero. Nothing is trimmed off the
            front, so the dialogue starts exactly where it always did. */}
        <Sequence
          from={cutFrame}
          durationInFrames={durationInFrames - cutFrame}
          name="main head"
        >
          <AbsoluteFill
            style={
              mainMask
                ? {
                    WebkitMaskImage: mainMask,
                    maskImage: mainMask,
                    WebkitMaskSize: "100% 100%",
                    maskSize: "100% 100%",
                  }
                : undefined
            }
          >
            <OffthreadVideo
              src={mainUrl}
              style={{ width: "100%", height: "100%", objectFit: "cover" }}
            />
          </AbsoluteFill>
        </Sequence>

        {/* The kit. */}
        {recipe.layers.map((layer, i) => {
          const asset = byslug[layer.pattern];
          if (!asset) return null;
          return (
            <PatternLayerView
              key={`${layer.pattern}-${i}`}
              layer={layer}
              asset={asset}
              frame={frame}
              totalFrames={durationInFrames}
            />
          );
        })}

        {recipe.vignette > 0 ? (
          <AbsoluteFill
            style={{
              background: `radial-gradient(ellipse at center, rgba(0,0,0,0) 45%, rgba(0,0,0,${recipe.vignette}) 100%)`,
              pointerEvents: "none",
            }}
          />
        ) : null}

        {flash > 0.002 ? (
          <AbsoluteFill
            style={{
              backgroundColor: recipe.palette.flash,
              opacity: Math.min(0.8, flash),
              mixBlendMode: "screen",
            }}
          />
        ) : null}

        {recipe.grain > 0 ? <Grain amount={recipe.grain} frame={frame} width={width} height={height} /> : null}

        {debugSafeZones
          ? (recipe.safe_zones ?? []).map((z, i) => (
              <div
                key={`z${i}`}
                style={{
                  position: "absolute",
                  left: `${z.x * 100}%`,
                  top: `${z.y * 100}%`,
                  width: `${z.w * 100}%`,
                  height: `${z.h * 100}%`,
                  border: "3px solid rgba(0,255,120,0.9)",
                  boxSizing: "border-box",
                }}
              />
            ))
          : null}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

/** Film grain, so a flat vector overlay does not read as a sticker on video. */
const Grain: React.FC<{ amount: number; frame: number; width: number; height: number }> = ({
  amount,
  frame,
}) => {
  const seed = frame % 8;
  return (
    <AbsoluteFill
      style={{
        opacity: amount,
        mixBlendMode: "overlay",
        backgroundImage:
          `url("data:image/svg+xml;utf8,${encodeURIComponent(
            `<svg xmlns="http://www.w3.org/2000/svg" width="180" height="180">` +
              `<filter id="n"><feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves="3" seed="${seed}"/></filter>` +
              `<rect width="180" height="180" filter="url(#n)" opacity="0.55"/></svg>`,
          )}")`,
        backgroundRepeat: "repeat",
        pointerEvents: "none",
      }}
    />
  );
};
