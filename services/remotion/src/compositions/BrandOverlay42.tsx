/**
 * `brand-overlay-42` — the 42 Abu Dhabi reel frame, GRAPHICS ONLY.
 *
 * Renders on a transparent canvas (no background anywhere) as a PNG sequence
 * that the backend overlays on the footage in one ffmpeg pass
 * (backend/app/services/brand_overlay.py). Footage never passes through this
 * composition.
 *
 * The look is 42's own "Myth vs Fact" reels (r128), measured frame by frame,
 * with the owner's own patterns and a digital glitch on top:
 *   bars layout  charcoal plate in the letterbox bars with the segment's
 *                PATTERN sliding left to right through them, a Violet B top
 *                band with a 45-degree cut and a bead string on it, the
 *                stacked logo top-right, a Violet A side slab rising from the
 *                bottom-right with its own bead string, and an optional typed
 *                heading + sub-line (in the top bar under the band when it has
 *                room, as in r128, else in the bottom bar).
 *   full layout  (no usable bars) Violet B corner slab top-right carrying the
 *                logo and a small Violet A wedge bottom-left with a bead string.
 *   both         the same pattern slides over the picture's LEFT EDGE (violet,
 *                fading out before the face zone) and topic icons (props.icons)
 *                glitch in and out on it, on scene segments only.
 *
 * Timing (per segment, see brand42/timeline.ts): the reel start and every
 * "handoff" seam run r128's entrance (band drops in, slab slides in, beads pop
 * on, the pattern slides in from the left, logo rises), each part glitching as
 * it arrives; a handoff first runs the exit (band out up-left, slab out
 * down-right, pattern glitches out). "cover"/"short" seams draw a slab wipe
 * over everything that hides the cut; a "glitch" seam glitches the whole
 * overlay over the cut (and the backend glitches the footage under it).
 *
 * Text is measured ONCE, in calculateMetadata (a real browser page, real
 * fonts), and handed to every frame as `text_layouts`; the outcome goes back
 * to the caller as `text_fit` (shown / shrunk / partial / dropped), which the
 * render server copies into its job JSON.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  AbsoluteFill,
  CalculateMetadataFunction,
  cancelRender,
  continueRender,
  delayRender,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

import { BeadTrail } from "../brand42/BeadTrail";
import { CornerSlab, CornerWedge, cornerSlabPoints } from "../brand42/CornerSlab";
import { EASE, ramp } from "../brand42/easing";
import { JOST_FAMILY, loadBrand42Fonts } from "../brand42/fonts";
import { geometryFor, type BarsGeometry, type FullGeometry, type Rect } from "../brand42/geometry";
import { burst, CLEAN, GlitchDefs, glitchIn, glitchOut, GlitchWrap, worst, type GlitchState } from "../brand42/Glitch";
import { IconBadge } from "../brand42/IconBadge";
import { Logo } from "../brand42/Logo";
import { PatternField, type PatternColour } from "../brand42/PatternField";
import { Plate } from "../brand42/Plate";
import { SideSlab } from "../brand42/SideSlab";
import { SlabWipeBody, SlabWipeString } from "../brand42/SlabWipe";
import { FrameSvg } from "../brand42/svg";
import { TextBlock } from "../brand42/TextBlock";
import { canvasMeasure, planReelText, type TextLayout } from "../brand42/textFit";
import {
  buildModel,
  iconAt,
  seamGlitchAt,
  segmentAt,
  validateProps,
  wipesAt,
  type ReelModel,
  type SegmentModel,
} from "../brand42/timeline";
import { FULL, GLITCH, ICON, MOTION, PATTERN, type Palette } from "../brand42/tokens";
import { TopBand } from "../brand42/TopBand";
import type { BrandOverlayProps } from "../brand42/types";

export type { BrandOverlayProps } from "../brand42/types";

const hasText = (t: BrandOverlayProps["text"]) => Boolean(t && (t.heading?.trim() || t.sub?.trim()));

/** Measures the text for every scene segment (fonts loaded first). */
async function measureReelText(props: BrandOverlayProps, W: number, H: number) {
  const segments = props.segments.map((seg, i) => ({ i, kind: seg.kind, index: seg.index, geom: geometryFor(seg, W, H) }));
  const want = hasText(props.text);
  if (want) await loadBrand42Fonts();
  return planReelText(props.text, segments, JOST_FAMILY, want ? canvasMeasure() : null);
}

export const brandOverlayCalculateMetadata: CalculateMetadataFunction<BrandOverlayProps> = async ({ props }) => {
  // Fail the render loudly on a malformed timeline: the backend checks that
  // the frame count equals durationInFrames, and a silently re-derived length
  // would put every graphic on the wrong frame of the footage.
  validateProps(props);
  const { layouts, report } = await measureReelText(props, props.width, props.height);
  return {
    width: props.width,
    height: props.height,
    fps: props.fps || 30,
    durationInFrames: props.durationInFrames,
    // Every render tab gets the same, already-measured layout, and the server
    // reads `text_fit` off the resolved props for its job JSON.
    props: { ...props, text_fit: report, text_layouts: layouts },
  };
};

/** Text layout per segment position (scene segments on a bars layout only). */
type TextLayouts = Record<string, TextLayout | null>;

export const BrandOverlay42: React.FC<BrandOverlayProps> = (props) => {
  const frame = useCurrentFrame();
  const { width: W, height: H } = useVideoConfig();
  // Keyed on the props' content, not their identity, so nothing below is
  // recomputed per frame.
  const propsKey = JSON.stringify(props);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const reel = useMemo(() => buildModel(props, W, H), [propsKey, W, H]);

  // Fonts (to draw the text) load once per page, before the first capture.
  // The layout itself comes measured from calculateMetadata; it is measured
  // here only when something rendered the component without it.
  const [handle] = useState(() => delayRender("brand-overlay-42: fonts and text layout"));
  const released = useRef(false);
  const [layouts, setLayouts] = useState<TextLayouts | null>(null);
  useEffect(() => {
    let alive = true;
    (async () => {
      let result: TextLayouts = {};
      if (hasText(props.text)) {
        await loadBrand42Fonts();
        result = props.text_layouts ?? (await measureReelText(props, W, H)).layouts;
      }
      if (alive) setLayouts(result);
    })().catch((err) => cancelRender(err));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [propsKey, W, H]);
  useEffect(() => {
    if (layouts && !released.current) {
      released.current = true;
      continueRender(handle);
    }
  }, [layouts, handle]);

  if (!layouts) return null;

  const m = segmentAt(reel, frame);
  const wipes = wipesAt(reel, frame);
  // A "glitch" seam glitches everything at once; the layers' own glitches
  // stand down meanwhile (a glitch inside a glitch multiplies the work for
  // no visible gain).
  const seam = seamGlitchAt(reel, frame);
  const quiet = seam !== null;
  const s = m.geom.s;
  const layers = (
    <>
      {m.geom.layout === "bars" ? (
        <BarsLayer model={reel} m={m} g={m.geom} frame={frame} text={layouts[String(m.i)] ?? null} quiet={quiet} />
      ) : (
        <FullLayer model={reel} m={m} g={m.geom} frame={frame} />
      )}
      <EdgeLayer model={reel} m={m} frame={frame} quiet={quiet} />
      <IconLayer model={reel} m={m} frame={frame} quiet={quiet} />
    </>
  );
  return (
    <AbsoluteFill>
      <GlitchDefs />
      {seam ? (
        <GlitchWrap
          frame={frame}
          state={seam.state}
          seed={`seam-${seam.seam.frame}`}
          region={{ x0: 0, y0: 0, x1: W, y1: H }}
          shift={GLITCH.shift * s * 1.6}
          split={GLITCH.split * s * 1.5}
          slices={16}
          blocks={[reel.palette.violet, reel.palette.violetB, reel.palette.ink]}
        >
          {layers}
        </GlitchWrap>
      ) : (
        layers
      )}
      {wipes.map((w) => (
        <React.Fragment key={w.seam.frame}>
          <SlabWipeBody wipe={w} W={W} H={H} s={m.geom.s} palette={reel.palette} />
          {/* The wipe grows UNDER the outgoing band: the band keeps its colour
              and its beads while the wipe emerges from beneath it. */}
          {w.phase === "in" && w.outgoing.geom.layout === "bars" ? (
            <BandUnit m={w.outgoing} g={w.outgoing.geom} frame={frame} palette={reel.palette} quiet />
          ) : null}
          <SlabWipeString wipe={w} W={W} H={H} s={m.geom.s} palette={reel.palette} frame={frame} />
        </React.Fragment>
      ))}
      {props.debug ? <DebugLayer m={m} text={layouts[String(m.i)] ?? null} /> : null}
    </AbsoluteFill>
  );
};

// ------------------------------------------------------------ motion helpers

/** Entrance progress of a part that starts `delay` frames into the entrance (1 = home). */
const entering = (m: SegmentModel, frame: number, delay: number, frames: number, curve: (t: number) => number) =>
  m.entrance === null ? 1 : ramp(frame, m.entrance + delay, frames, curve);

/** Exit progress (0 = home, 1 = gone at the last frame before the hand-off cut). */
const exiting = (m: SegmentModel, frame: number) => {
  if (m.exitAt === null) return 0;
  const start = Math.max(m.seg.from, m.exitAt - MOTION.exit);
  return ramp(frame, start, m.exitAt - 1 - start, EASE.inExp);
};

/**
 * Frame the outgoing beads are cut: the first frame the exit has covered
 * MOTION.beadCutProgress of its travel (F-7 on the 18-frame exit, where r128
 * cuts its beads). null = never.
 */
const beadCut = (m: SegmentModel): number | null => {
  if (m.exitAt === null) return null;
  for (let f = Math.max(m.seg.from, m.exitAt - MOTION.exit); f < m.exitAt; f++) {
    if (exiting(m, f) >= MOTION.beadCutProgress) return f;
  }
  return m.exitAt;
};

const popStart = (m: SegmentModel, delay: number) => (m.entrance === null ? null : m.entrance + delay);

const logoReveal = (m: SegmentModel, frame: number) => {
  if (!m.logoVisible) return 0;
  if (m.logoEntrance === null) return 1;
  return ramp(frame, m.logoEntrance, MOTION.logoIn, EASE.outPoly5);
};

/** A slab's entrance glitch: it tears for the first frames of its slide, all slices showing. */
const slabGlitch = (m: SegmentModel, frame: number, delay: number, quiet: boolean): GlitchState => {
  if (quiet || m.entrance === null) return CLEAN;
  return { amount: glitchIn(frame, m.entrance + delay, GLITCH.slabIn).amount, reveal: 1 };
};

/**
 * The pattern's glitch on this segment: in at the entrance (slices arriving
 * one by one), out before a hand-off cut, and an idle twitch now and then.
 */
const patternGlitch = (m: SegmentModel, frame: number, key: string, quiet: boolean): GlitchState => {
  if (quiet) return CLEAN;
  let st = burst(frame, `${key}-${m.i}`, GLITCH.burstPeriod);
  if (m.entrance !== null) st = worst(st, glitchIn(frame, m.entrance, GLITCH.in));
  if (m.exitAt !== null) st = worst(st, glitchOut(frame, m.exitAt, GLITCH.out));
  return st;
};

/** Pattern slide-in: uncovered left to right from the entrance. */
const patternReveal = (m: SegmentModel, frame: number) =>
  m.entrance === null ? 1 : ramp(frame, m.entrance, PATTERN.reveal, EASE.inOutCubic);

/** Rightward travel of every pattern lattice (one clock for the whole reel). */
const patternDrift = (frame: number, s: number) => PATTERN.drift * s * frame;

/** Tile scale: pattern tiles are sized for 1080 on the short side. */
const tileScale = (W: number, H: number) => Math.min(W, H) / 1080;

const barsClip = (g: BarsGeometry) => {
  const { W, H, content: c } = g;
  return `path(evenodd, "M0 0H${W}V${H}H0Z M${c.x} ${c.y}H${c.x + c.w}V${c.y + c.h}H${c.x}Z")`;
};

/** TopBand offset: drops in (translateY -hb -> 0), leaves up-left along its diagonal. */
const bandOffset = (m: SegmentModel, g: BarsGeometry, frame: number) => {
  const bandIn = entering(m, frame, 0, MOTION.bandIn, EASE.outExp);
  const bandOut = (Math.min(g.hb, g.bandCut / 2) + 2 * g.s) * exiting(m, frame);
  return { dx: -bandOut, dy: -(g.hb + 2 * g.s) * (1 - bandIn) - bandOut };
};

/** The top band and the bead string on its cut, clipped to the bars. */
const BandUnit: React.FC<{ m: SegmentModel; g: BarsGeometry; frame: number; palette: Palette; quiet?: boolean }> = ({
  m,
  g,
  frame,
  palette,
  quiet = false,
}) => {
  const band = bandOffset(m, g, frame);
  return (
    <AbsoluteFill style={{ clipPath: barsClip(g) }}>
      <GlitchWrap
        frame={frame}
        state={slabGlitch(m, frame, 0, quiet)}
        seed={`band-${m.i}`}
        region={{ x0: 0, y0: 0, x1: g.W, y1: g.topBar.h }}
        shift={GLITCH.shift * g.s}
        split={GLITCH.split * g.s}
        slices={6}
      >
        <TopBand g={g} dx={band.dx} dy={band.dy} color={palette.violetB} />
      </GlitchWrap>
      <BeadTrail
        W={g.W}
        H={g.H}
        line={g.topBeads}
        s={g.s}
        frame={frame}
        color={palette.ink}
        dx={band.dx}
        dy={band.dy}
        popStart={popStart(m, MOTION.topBeadsDelay)}
        cutAt={beadCut(m)}
      />
    </AbsoluteFill>
  );
};

/** A charcoal patch with feathered edges: keeps the sliding pattern off the logo / words. */
const Scrim: React.FC<{ rect: Rect; pad: number; color: string; opacity: number }> = ({ rect, pad, color, opacity }) =>
  opacity <= 0 || rect.w <= 0 || rect.h <= 0 ? null : (
    <div
      style={{
        position: "absolute",
        left: rect.x - pad / 2,
        top: rect.y - pad / 2,
        width: rect.w + pad,
        height: rect.h + pad,
        background: color,
        borderRadius: pad,
        boxShadow: `0 0 ${pad * 1.4}px ${pad * 0.8}px ${color}`,
        opacity,
      }}
    />
  );

// ---------------------------------------------------------------- bars layer

const BarsLayer: React.FC<{
  model: ReelModel;
  m: SegmentModel;
  g: BarsGeometry;
  frame: number;
  text: TextLayout | null;
  quiet: boolean;
}> = ({ model, m, g, frame, text, quiet }) => {
  const { W, H, s, content } = g;
  const pal = model.palette;
  const out = exiting(m, frame);

  // SideSlab: slides in from off-frame bottom-right along the diagonal, sinks
  // slowly for the rest of the segment, leaves the way it came.
  const slabIn = entering(m, frame, MOTION.slabInDelay, MOTION.slabIn, EASE.outExp);
  const slabAway = Math.min(g.ws, (H - g.yTop) / 2) + 2 * s;
  const sink = MOTION.slabSinkH * H * ramp(frame, m.seg.from, m.seg.frames, EASE.outSin);
  const slabD = slabAway * (1 - slabIn) + slabAway * out;
  const slab = { dx: slabD, dy: slabD + sink };

  // The pattern bed: this segment's pattern through both bars, one lattice
  // (so it lines up across the picture), sliding left to right.
  const bed = PATTERN.bars[m.pattern];
  const drift = patternDrift(frame, s);
  const reveal = patternReveal(m, frame);
  const k = tileScale(W, H);

  const showText = text !== null && m.seg.kind === "scene" && model.typeStart !== null;
  const textScrim =
    showText && model.typeStart !== null ? ramp(frame, model.typeStart - 4, 8, EASE.linear) : 0;

  // The logo stays put across a hand-off that keeps it in the same place;
  // otherwise it fades out with the exit instead of vanishing at the cut.
  const logoAlpha = m.logoStays ? 1 : 1 - out;
  const logoShown = logoReveal(m, frame) * logoAlpha;
  const logoGlitch: GlitchState =
    quiet || m.logoEntrance === null ? CLEAN : glitchIn(frame, m.logoEntrance, GLITCH.in);

  return (
    <AbsoluteFill style={{ clipPath: barsClip(g) }}>
      <Plate W={W} H={H} content={content} color={pal.charcoal} />
      <GlitchWrap
        frame={frame}
        state={patternGlitch(m, frame, "bed", quiet)}
        seed={`bed-${m.i}`}
        region={{ x0: 0, y0: 0, x1: W, y1: H }}
        shift={GLITCH.shift * s}
        split={GLITCH.split * s}
        slices={18}
        blocks={[pal.violet, pal.ink]}
      >
        <AbsoluteFill>
          {[g.topBar, g.bottomBar].map((rect, i) => (
            <PatternField
              key={i}
              slug={m.pattern}
              colour={bed.colour as PatternColour}
              rect={rect}
              scale={k}
              drift={drift}
              anchorY={0}
              opacity={bed.opacity}
              reveal={reveal}
            />
          ))}
        </AbsoluteFill>
      </GlitchWrap>
      <Scrim rect={g.logo} pad={26 * s} color={pal.charcoal} opacity={logoShown} />
      {showText ? (
        <Scrim
          rect={{ x: text.left, y: text.top, w: text.right - text.left, h: text.bottom - text.top }}
          pad={30 * s}
          color={pal.charcoal}
          opacity={textScrim}
        />
      ) : null}
      <GlitchWrap
        frame={frame}
        state={slabGlitch(m, frame, MOTION.slabInDelay, quiet)}
        seed={`slab-${m.i}`}
        region={{ x0: 0, y0: g.bottomBar.y, x1: W, y1: H }}
        shift={GLITCH.shift * s}
        split={GLITCH.split * s}
        slices={6}
      >
        <SideSlab g={g} dx={slab.dx} dy={slab.dy} color={pal.violet} />
      </GlitchWrap>
      <BandUnit m={m} g={g} frame={frame} palette={pal} quiet={quiet} />
      <BeadTrail
        W={W}
        H={H}
        line={g.slabBeads}
        s={s}
        frame={frame}
        color={pal.ink}
        dx={slab.dx}
        dy={slab.dy}
        popStart={popStart(m, MOTION.slabBeadsDelay)}
        cutAt={beadCut(m)}
      />
      <GlitchWrap
        frame={frame}
        state={logoGlitch}
        seed={`logo-${m.i}`}
        region={{ x0: g.logo.x, y0: g.logo.y, x1: g.logo.x + g.logo.w, y1: g.logo.y + g.logo.h }}
        shift={GLITCH.shift * s * 0.8}
        split={GLITCH.split * s}
        slices={7}
      >
        <Logo rect={g.logo} reveal={logoReveal(m, frame)} rise={MOTION.logoRise * s} alpha={logoAlpha} />
      </GlitchWrap>
      {showText ? (
        <TextBlock
          W={W}
          H={H}
          layout={text}
          frame={frame}
          typeStart={model.typeStart as number}
          subFamily={JOST_FAMILY}
          violet={pal.violet}
          ink={pal.ink}
        />
      ) : null}
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------- full layer

const FullLayer: React.FC<{ model: ReelModel; m: SegmentModel; g: FullGeometry; frame: number }> = ({ model, m, g, frame }) => {
  const { W, H, s } = g;
  const pal = model.palette;
  const out = exiting(m, frame);

  // Corner slab: slides in from the right and leaves to the right (r143's
  // corner-slab grammar; its bottom edge never moves). Across a hand-off to
  // an identical full layout it simply stays, logo and all.
  const away = W - Math.max(g.slabLeft, g.cornerC - g.hs) + 2 * s;
  const cornerIn = m.cornerStaysIn ? 1 : entering(m, frame, 0, MOTION.bandIn, EASE.outExp);
  const cornerOut = m.cornerStaysOut ? 0 : out;
  const cornerDx = away * (1 - cornerIn) + away * cornerOut;

  // Wedge: slides in from off-frame bottom-left along its diagonal.
  const wedgeAway = g.leg / 2 + 2 * s;
  const wd = wedgeAway * (1 - entering(m, frame, MOTION.slabInDelay, MOTION.slabIn, EASE.outExp)) + wedgeAway * out;

  // The logo sits ON the slab: it travels with it and is clipped to it (so
  // it can never show on bare footage), rising into place as in r143.
  const slabClip = `polygon(${cornerSlabPoints(g, cornerDx)
    .map(([x, y]) => `${x}px ${y}px`)
    .join(", ")})`;
  return (
    <AbsoluteFill>
      <CornerWedge g={g} dx={-wd} dy={wd} color={pal.violet} />
      <CornerSlab g={g} dx={cornerDx} color={pal.violetB} />
      {g.wedgeBeads ? (
        <BeadTrail
          W={W}
          H={H}
          line={g.wedgeBeads}
          s={s}
          frame={frame}
          color={pal.ink}
          dx={-wd}
          dy={wd}
          popStart={popStart(m, MOTION.slabBeadsDelay)}
          cutAt={beadCut(m)}
        />
      ) : null}
      <AbsoluteFill style={{ clipPath: slabClip }}>
        <Logo rect={g.logo} reveal={logoReveal(m, frame)} rise={MOTION.logoRise * s} dx={cornerDx} />
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

// ------------------------------------------------- left edge + topic icons

/**
 * The segment's pattern sliding over the picture's left edge, violet, fading
 * out before the face zone and above a talking head's shoulders. Scenes only:
 * an intro / outro card carries its own design (and text) on that edge.
 */
const EdgeLayer: React.FC<{ model: ReelModel; m: SegmentModel; frame: number; quiet: boolean }> = ({
  model,
  m,
  frame,
  quiet,
}) => {
  const g = m.geom;
  const e = g.edge;
  if (!model.edgePatterns || m.seg.kind !== "scene" || e.w <= 0 || e.h <= 0) return null;
  const pct = (v: number) => `${(v * 100).toFixed(2)}%`;
  const mask =
    `linear-gradient(to right, #000 0%, #000 ${pct(PATTERN.edgeSolid)}, transparent 100%), ` +
    `linear-gradient(to bottom, transparent 0%, #000 ${pct(PATTERN.edgeFadeTop)}, #000 ${pct(PATTERN.edgeFadeFrom)}, transparent ${pct(PATTERN.edgeFadeTo)})`;
  return (
    <GlitchWrap
      frame={frame}
      state={patternGlitch(m, frame, "edge", quiet)}
      seed={`edge-${m.i}`}
      region={{ x0: e.x, y0: e.y, x1: e.x + e.w, y1: e.y + e.h }}
      shift={GLITCH.shift * g.s}
      split={GLITCH.split * g.s}
      slices={12}
      blocks={[model.palette.violet, model.palette.ink]}
    >
      <PatternField
        slug={m.pattern}
        colour={PATTERN.edge.colour}
        rect={e}
        scale={tileScale(g.W, g.H)}
        drift={patternDrift(frame, g.s)}
        anchorY={0}
        opacity={PATTERN.edge.opacity}
        reveal={patternReveal(m, frame)}
        mask={mask}
      />
    </GlitchWrap>
  );
};

/** The topic icon on screen now (if any), glitching in, twitching, glitching out. */
const IconLayer: React.FC<{ model: ReelModel; m: SegmentModel; frame: number; quiet: boolean }> = ({
  model,
  m,
  frame,
  quiet,
}) => {
  const cue = iconAt(model, frame);
  if (!cue) return null;
  const g = m.geom;
  const box = g.icon;
  let state = worst(glitchIn(frame, cue.from, ICON.in), glitchOut(frame, cue.from + cue.frames, ICON.out));
  if (quiet) state = { amount: 0, reveal: state.reveal > 0 ? 1 : 0 };
  else state = worst(state, burst(frame, `icon-${cue.from}`, 70, 2, 0.32));
  return (
    <GlitchWrap
      frame={frame}
      state={state}
      seed={`icon-${cue.from}`}
      region={{ x0: box.x, y0: box.y, x1: box.x + box.size, y1: box.y + box.size }}
      shift={GLITCH.shift * g.s * 0.9}
      split={GLITCH.split * g.s * 1.2}
      slices={7}
      blocks={[model.palette.violetB, model.palette.ink]}
    >
      <IconBadge W={g.W} H={g.H} box={box} icon={cue.icon} violet={model.palette.violet} ink={model.palette.ink} />
    </GlitchWrap>
  );
};

// ---------------------------------------------------------------- debug

const DebugLayer: React.FC<{ m: SegmentModel; text: TextLayout | null }> = ({ m, text }) => {
  const g = m.geom;
  const { W, H } = g;
  const c = g.content;
  return (
    <FrameSvg W={W} H={H}>
      <g fill="none" strokeWidth={2}>
        <rect x={c.x + 1} y={c.y + 1} width={c.w - 2} height={c.h - 2} stroke="#00EBFF" />
        <rect x={g.edge.x} y={g.edge.y} width={g.edge.w} height={g.edge.h} stroke="#FFD400" strokeDasharray="4 8" />
        <rect x={g.icon.x} y={g.icon.y} width={g.icon.size} height={g.icon.size} stroke="#FFD400" />
        {g.layout === "bars" ? (
          <>
            {g.textRegions.map((r) => (
              <rect
                key={r.placement}
                x={r.left}
                y={r.top}
                width={r.right - r.left}
                height={r.bottom - r.top}
                stroke="#FF5A3C"
                strokeDasharray={text?.placement === r.placement ? undefined : "3 9"}
              />
            ))}
            {text ? <line x1={text.right} y1={0} x2={text.right} y2={H} stroke="#FF5A3C" strokeDasharray="6 6" /> : null}
          </>
        ) : null}
        <rect
          x={FULL.faceZone.x0 * W}
          y={FULL.faceZone.y0 * H}
          width={(FULL.faceZone.x1 - FULL.faceZone.x0) * W}
          height={(FULL.faceZone.y1 - FULL.faceZone.y0) * H}
          stroke="#FF5A3C"
          strokeDasharray="2 6"
        />
      </g>
      <text x={12} y={H - 12} fill="#00EBFF" fontSize={24} fontFamily="monospace">
        {`${m.seg.kind}#${m.seg.index} ${m.seg.layout} ${m.pattern} f${m.seg.from}+${m.seg.frames}`}
      </text>
    </FrameSvg>
  );
};
