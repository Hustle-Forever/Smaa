// Rebrand studio — the StagePlayer: Next in motion (the generalised icon
// overlay player). ONE video element, on the stage's one clock (stageT:
// useIconPlayer), and at most one thing drawn over it.
//
//   the EXACT video   a draft made in house, once its render is ready
//                     (useHouseRender: the very file Generate copies) and
//                     only while it is the draft's own — played as it is,
//                     NOTHING drawn over it: the patterns and the icons are
//                     in it (never a second set)
//   a quick stand-in  until then ("Quick preview"): the scene (or, added on
//                     top of a version, that version) playing WITH its
//                     sound, and a canvas over it — each pattern's art UNCUT,
//                     moved by the camera at that moment (a pattern in the
//                     room grows with the push-in; one on a bar holds) in its
//                     entrance and exit (lib/rebrandPlate.ts, approximate),
//                     then the plan's icons at their times, all cut by the
//                     speaker's shape at that moment (the scene's matte bank,
//                     else the plan's masks — never blended across a jump
//                     cut) — then an icon put "in front" over her. For a
//                     version Omni made, the pattern is Omni's to paint: the
//                     layer the plan gives is drawn flat, marked approximate.
//
// At rest an icon is the server's sprite — the very pixels the run stamps —
// whenever the plan was made for the icon's look as the draft has it now; a
// look changed a click ago is drawn by the canvas itself (iconArt.drawBody)
// until the next plan lands. They move as the run moves them (fade, pop and
// slide frame for frame; the glitch approximated). A click on an icon picks
// it (its panel opens in the Icons tab).
//
// Resting on a time (a pause, a click on the lane, a Moments chip) swaps in
// the EXACT frame (/icons/moment, /house/moment), once it has decoded — the
// exact video needs none: its paused frame is the output's. The stage then
// shows that one picture: the video is taken off, never kept under it — one
// picture of the speaker at a time. Play puts the video back where it was.
// A video that plays to its end goes back to 0:00, paused (the stage takes
// Next back to its still when it was played from there).
//
// Nothing sits over the picture but the icons (and the play bar at its foot):
// what the frame is ("Exact video", "Quick preview", "Exact frame",
// "Updating icons…") is said above it (IconStageStatus, in the stage's
// header) — a chip on the frame would cover the very corner an icon can sit
// in.
//
// By hand: the icon picked on the Icons tab, while paused where it shows,
// has handles (TransformBox): drag it anywhere, drag a corner to size it —
// its outline green where it is clear, amber partly behind the speaker, red
// over her face (lib/rebrandCover.ts: the planner's own rule, over its
// window). A drag leaves the exact frame (it has the icon at its old place)
// for the quick stand-in, the icon drawn where the hand has it; let go, it
// is kept exactly there (`onBox`: props.tweaks[its glyph].box) and drawn
// there until the next plan and its exact frame land.

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Pause, Play, Volume2, VolumeX } from "lucide-react";

import type {
  HarnessBox,
  HarnessCameraTrack,
  HarnessHouseLayer,
  HarnessIconFonts,
  HarnessIconItem,
  HarnessIconMasks,
  HarnessIconPlan,
  HarnessIconRules,
  HarnessRecipe,
} from "../../../lib/rebrandHarnessApi";
import { coverOf, type SpeakerSamples } from "../../../lib/rebrandCover";
import { glyphId, iconProps, iconsOf, inkFor, lookInputs, sizeOfBox, styleOf, userBoxOf } from "../../../lib/rebrandIcons";
import { motionFor } from "../../../lib/rebrandIconFx";
import { cameraAt, layerAt } from "../../../lib/rebrandPlate";
import { isHex } from "../../../lib/rebrandRecipe";
import { iconBox, sameBox, type Box, type Transform } from "../../../lib/rebrandTransform";
import { Spinner } from "../../ui/Spinner";
import { chipWidth, drawBody, drawChipGrown, drawItem, isRtl, loadLabelFont, onArt, rememberPath, spriteImage, type ItemDraw } from "./iconArt";
import { COPY } from "./studioCopy";
import { TransformBox, type BoxTone } from "./TransformBox";
import type { IconMomentState, IconPlayer } from "./useIconPlayer";

const M = 0.16;
/** The safe margin handles snap to, of the frame's width (speaker_track.EDGE_FRAC). */
const EDGE = 0.04;

/** Where an icon's body rests in a box the user has it in now (canvas px),
 *  as the planner keeps it: a square centred at the box's smaller side; a
 *  chip at the box's height, as wide as it was drawn (in proportion), the
 *  edge nearest its side of the frame kept. */
function placedBody(
  box: HarnessBox,
  chip: boolean,
  was: { w: number; h: number },
  W: number,
  H: number,
): { x: number; y: number; w: number; h: number } {
  const bx = box.x * W;
  const by = box.y * H;
  const bw = box.w * W;
  const bh = box.h * H;
  if (chip) {
    const w = was.w * (bh / Math.max(1e-6, was.h));
    return { x: bx + bw / 2 <= W / 2 ? bx : bx + bw - w, y: by, w, h: bh };
  }
  const L = Math.min(bw, bh);
  return { x: bx + bw / 2 - L / 2, y: by + bh / 2 - L / 2, w: L, h: L };
}

/** One plan item as the canvas draws it (canvas px), and whether it goes in
 *  front of the speaker: the server's sprite when the plan was made for its
 *  look as the draft has it, else the canvas's own drawing of the draft's
 *  look — its colour, words, style and size — on the plan's place. `moved`:
 *  the box the user has it in now when the plan has it elsewhere (a drag in
 *  progress, or one let go before its plan lands) — drawn there, the
 *  sprite moved with it while its look is the plan's. */
function toDraw(
  ctx: CanvasRenderingContext2D,
  it: HarnessIconItem,
  draft: HarnessRecipe | null,
  planRecipe: HarnessRecipe | null,
  plan: HarnessIconPlan | null,
  patternColour: string | null,
  W: number,
  H: number,
  rules: HarnessIconRules,
  moved: HarnessBox | null = null,
): { item: ItemDraw; front: boolean } {
  const id = glyphId(it.glyph);
  const same = !!planRecipe && lookInputs(draft ?? planRecipe, id) === lookInputs(planRecipe, id);
  const sameLook =
    !!planRecipe && lookInputs(draft ?? planRecipe, id, { box: false }) === lookInputs(planRecipe, id, { box: false });
  const base = { from: it.from, settle: it.settle, to: it.to, seed: it.glyph.id };
  const sprite = (same || (!!moved && sameLook)) && it.sprite ? spriteImage(it.sprite.url) : null;
  if (sprite && it.sprite) {
    const sp = it.sprite;
    const w = sp.at.w * W;
    const h = sp.at.h * H;
    const px = (sp.ox / sp.w) * w;
    const py = (sp.oy / sp.h) * h;
    let body = { x: sp.at.x * W + px, y: sp.at.y * H + py, w: w - 2 * px, h: h - 2 * py };
    let k = 1;
    if (moved) {
      const to = placedBody(moved, it.style === "chip", body, W, H);
      k = to.h / Math.max(1e-6, body.h);
      body = to;
    }
    const chip = it.style === "chip";
    return {
      item: {
        ...base,
        enter: motionFor(it.style, it.enter ?? "glitch") ?? "glitch",
        leave: motionFor(it.style, it.leave ?? null),
        body,
        pad: px * k,
        draw: (x, y) => ctx.drawImage(sprite, x - px * k, y - py * k, w * k, h * k),
        // A chip's grow: its pill drawn by the canvas while it unfolds.
        grow: chip
          ? {
              rtl: isRtl(it.text),
              draw: (x, y, u, wd) =>
                drawChipGrown(
                  ctx,
                  it.glyph,
                  { style: "chip", fill: it.fill ?? "#7f2bf2", ink: it.ink ?? "#ffffff", weight: it.weight ?? "bold", text: it.text },
                  { x, y, w: body.w, h: body.h },
                  rules,
                  u,
                  wd,
                ),
            }
          : undefined,
      },
      front: it.layer === "front",
    };
  }
  // The draft's look, drawn by the canvas on the plan's place.
  const el = iconsOf(draft);
  const p = iconProps(el);
  const own = p.looks[id] ?? {};
  const planEl = iconsOf(planRecipe);
  const planOwn = iconProps(planEl).looks[id] ?? {};
  const style = same ? it.style ?? styleOf(draft, id) : styleOf(draft, id);
  const sceneFill = isHex(p.colour) ? p.colour.toLowerCase() : plan?.colours?.fill ?? "#7f2bf2";
  const fill = same ? it.fill ?? sceneFill : own.colour ?? sceneFill;
  const ink = own.ink ?? (same ? it.ink ?? inkFor(fill, patternColour) : own.colour || isHex(p.colour) ? inkFor(fill, patternColour) : plan?.colours?.ink ?? inkFor(fill, patternColour));
  const custom = it.glyph.source === "custom";
  const weight = (style === "circle" || style === "chip") && !custom ? "bold" : "fill";
  const text = own.label ?? it.text ?? it.label;
  // A box the user has it in carries its own size.
  const scale = moved ? 1 : (own.size ?? el?.placement.size ?? M) / (planOwn.size ?? planEl?.placement.size ?? M);
  const at = moved ?? it.box;
  const bx = at.x * W;
  const by = at.y * H;
  const bw = at.w * W;
  const bh = at.h * H;
  const cx = bx + bw / 2;
  const cy = by + bh / 2;
  const chipH = rules.chip_h ?? 0.74;
  let body: { x: number; y: number; w: number; h: number };
  let L: number;
  if (style === "chip") {
    const h = (it.style === "chip" ? bh : Math.min(bw, bh) * chipH) * scale;
    const w = chipWidth(ctx, text, h, rules);
    // The edge nearest its side of the frame stays put, as the planner pins it.
    const x = cx <= W / 2 ? bx : bx + bw - w;
    body = { x, y: cy - h / 2, w, h };
    L = h / chipH;
  } else {
    L = (it.style === "chip" ? bh / chipH : Math.min(bw, bh)) * scale;
    body = { x: cx - L / 2, y: cy - L / 2, w: L, h: L };
  }
  const pad =
    style === "circle" || style === "chip"
      ? Math.ceil(3 * (rules.shadow_sigma ?? 0.08) * L + (rules.shadow_dy ?? 0.05) * L)
      : 0;
  return {
    item: {
      ...base,
      enter: motionFor(style, own.enter ?? (same ? it.enter : null) ?? (el?.animation.entrance || "auto")) ?? "auto",
      leave: motionFor(style, own.leave ?? null),
      body,
      pad,
      draw: (x, y) => {
        drawBody(ctx, it.glyph, { style, fill, ink, weight, text }, { x, y, w: body.w, h: body.h }, rules);
      },
      grow:
        style === "chip"
          ? {
              rtl: isRtl(text),
              draw: (x, y, u, wd) =>
                drawChipGrown(ctx, it.glyph, { style, fill, ink, weight, text }, { x, y, w: body.w, h: body.h }, rules, u, wd),
            }
          : undefined,
    },
    front: (own.layer ?? "behind") === "front",
  };
}

function clock(t: number): string {
  const s = Math.max(0, t);
  return `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`;
}

/** The speaker's masks, one small canvas per sample whose alpha is the
 *  speaker (from the grey strip), with the jump cuts (s) no blend may
 *  cross, and `step`: each sample held to the next (the matte bank's). */
interface MaskFrames {
  times: number[];
  frames: HTMLCanvasElement[];
  w: number;
  h: number;
  cuts: number[];
  step: boolean;
}

function maskFramesOf(img: HTMLImageElement, m: HarnessIconMasks): MaskFrames | null {
  const n = Math.min(m.times.length, Math.floor(img.naturalHeight / m.height));
  const frames: HTMLCanvasElement[] = [];
  for (let k = 0; k < n; k++) {
    const c = document.createElement("canvas");
    c.width = m.width;
    c.height = m.height;
    const x = c.getContext("2d");
    if (!x) return null;
    x.drawImage(img, 0, k * m.height, m.width, m.height, 0, 0, m.width, m.height);
    const d = x.getImageData(0, 0, m.width, m.height);
    for (let i = 0; i < d.data.length; i += 4) {
      d.data[i + 3] = d.data[i];
      d.data[i] = d.data[i + 1] = d.data[i + 2] = 0;
    }
    x.putImageData(d, 0, 0);
    frames.push(c);
  }
  return frames.length
    ? { times: m.times.slice(0, n), frames, w: m.width, h: m.height, cuts: m.cuts ?? [], step: !!m.step }
    : null;
}

/** Cut the speaker at `t` out of what the canvas holds: the two samples
 *  around it blended linearly (added, so the blend is exact), scaled up —
 *  never across a jump cut (the sample on t's side of it alone), and not at
 *  all for held samples. */
function cutSpeaker(ctx: CanvasRenderingContext2D, m: MaskFrames, mix: HTMLCanvasElement, t: number, W: number, H: number) {
  let k = 0;
  while (k + 1 < m.times.length && m.times[k + 1] <= t + 1e-6) k++;
  const k2 = Math.min(k + 1, m.frames.length - 1);
  const span = m.times[k2] - m.times[k];
  let f = m.step ? 0 : span > 0 ? Math.min(1, Math.max(0, (t - m.times[k]) / span)) : 0;
  const cut = m.cuts.find((c) => m.times[k] < c - 1e-6 && c <= m.times[k2] + 1e-6);
  if (cut !== undefined) f = t < cut - 1e-6 ? 0 : 1;
  if (mix.width !== m.w || mix.height !== m.h) {
    mix.width = m.w;
    mix.height = m.h;
  }
  const mx = mix.getContext("2d");
  if (!mx) return;
  mx.globalCompositeOperation = "source-over";
  mx.globalAlpha = 1;
  mx.clearRect(0, 0, m.w, m.h);
  mx.globalCompositeOperation = "lighter";
  mx.globalAlpha = 1 - f;
  mx.drawImage(m.frames[k], 0, 0);
  if (f > 0) {
    mx.globalAlpha = f;
    mx.drawImage(m.frames[k2], 0, 0);
  }
  mx.globalCompositeOperation = "source-over";
  mx.globalAlpha = 1;
  ctx.globalCompositeOperation = "destination-out";
  ctx.imageSmoothingEnabled = true;
  ctx.drawImage(mix, 0, 0, W, H);
  ctx.globalCompositeOperation = "source-over";
}

/** One pattern of a draft made in house at scene time t, as the stand-in
 *  draws it: its uncut art in its motion (rebrandPlate.layerAt), moved by
 *  the camera at t when it follows the room, and by its hand move — frame
 *  px scaled to the canvas (`s` canvas px per frame px). */
function drawLayer(
  ctx: CanvasRenderingContext2D,
  layer: HarnessHouseLayer,
  img: HTMLImageElement,
  t: number,
  fps: number,
  size: readonly [number, number],
  camera: HarnessCameraTrack | null,
  s: number,
  dpr: number,
) {
  const f = layerAt(layer, t, Math.round(t * fps), size);
  if (!f || f.alpha <= 0) return;
  ctx.save();
  ctx.setTransform(dpr * s, 0, 0, dpr * s, 0, 0);
  if (layer.follow === "room" && camera) {
    const [a, b, tx, ty] = cameraAt(camera, t);
    ctx.transform(a, b, -b, a, tx, ty);
  }
  const h = layer.hand;
  if (h && h.length >= 2) ctx.transform(h[0][0], h[1][0], h[0][1], h[1][1], h[0][2], h[1][2]);
  ctx.translate(f.dx - layer.margin[0], f.dy - layer.margin[1]);
  if (f.clip) {
    ctx.beginPath();
    ctx.rect(f.clip.x0, f.clip.y0, Math.max(0, f.clip.x1 - f.clip.x0), Math.max(0, f.clip.y1 - f.clip.y0));
    ctx.clip();
  }
  ctx.globalAlpha = f.alpha;
  ctx.drawImage(img, 0, 0);
  ctx.restore();
}

export function StagePlayer({
  plan,
  planRecipe,
  draft,
  videoUrl,
  exactUrl = null,
  poster,
  patternUrl = null,
  layers = null,
  frameSize = null,
  camera = null,
  masks = null,
  iconsBaked = false,
  player,
  moment,
  showSpots = false,
  selected = null,
  rules,
  patternColour,
  card = null,
  fonts = null,
  onPick,
  onBox,
  coverSamples = null,
}: {
  /** The plan on screen (the draft's, or the last one while it updates). */
  plan: HarnessIconPlan | null;
  /** The recipe that plan was made for. */
  planRecipe: HarnessRecipe | null;
  draft: HarnessRecipe | null;
  /** What the stand-in plays under its canvas: the scene, or the version
   *  added to. */
  videoUrl: string | null;
  /** The draft's exact video (made in house, ready, the draft's own): played
   *  in place of the stand-in, nothing drawn over it. */
  exactUrl?: string | null;
  poster: string | null;
  /** A version Omni made: its pattern as one flat layer (approximate). */
  patternUrl?: string | null;
  /** A draft made in house: each pattern as the engine draws it (the
   *  preview's `house.layers`), in its motion, under the icons. */
  layers?: HarnessHouseLayer[] | null;
  /** The frame the layers and the camera are in (px). */
  frameSize?: { width: number; height: number; fps: number } | null;
  /** The scene's camera: a pattern in the room moves with it. */
  camera?: HarnessCameraTrack | null;
  /** The speaker's shape over the scene (the matte bank's, else the plan's):
   *  cut out of the patterns and the icons as they move. */
  masks?: HarnessIconMasks | null;
  /** The video under the stand-in carries our icons already (a version
   *  with no clean clip): none are drawn over it — never two sets. */
  iconsBaked?: boolean;
  player: IconPlayer;
  moment: IconMomentState;
  showSpots?: boolean;
  /** The icon picked in the list or the lane (glyph id): outlined. */
  selected?: string | null;
  rules: HarnessIconRules;
  /** The first pattern's colour (the ink on a white fill). */
  patternColour: string | null;
  /** Over the frame: the icons warming up, or why they couldn't be placed. */
  card?: ReactNode;
  /** The chip label font (the catalogue's). */
  fonts?: HarnessIconFonts | null;
  /** A click on an icon on the stage picks it (glyph id). */
  onPick?: (id: string) => void;
  /** The picked icon moved or sized by hand on the picture (its body box;
   *  `resized`: its size changed, `size` its size then — a share of the
   *  frame's short side; `key`: a keyboard nudge). Without it, no handles. */
  onBox?: (id: string, box: HarnessBox, opts: { resized: boolean; key: boolean; size: number }) => void;
  /** The icon plan's speaker samples (its masks, heads and bars): the
   *  handles' outline — clear, partly behind her, over her face. */
  coverSamples?: SpeakerSamples | null;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const patternImg = useRef<HTMLImageElement | null>(null);
  const layerImgs = useRef(new Map<string, HTMLImageElement>());
  const maskRef = useRef<MaskFrames | null>(null);
  const mixRef = useRef<HTMLCanvasElement | null>(null);
  // An icon being moved or sized by hand: the stand-in shows it where the
  // hand has it (the exact frame and the exact video have it at its old
  // place, so they step aside until it is let go).
  const [live, setLive] = useState<{ id: string; box: HarnessBox } | null>(null);
  const liveRef = useRef(live);
  liveRef.current = live;
  // The exact video carries the patterns and the icons: nothing is drawn
  // over it, and it is the one video (the stand-in's is taken off).
  const exactVideo = !!exactUrl && !live;
  const src = (live ? null : exactUrl) ?? videoUrl;
  // A small frame (a phone with the picker open) keeps only the essentials.
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setNarrow(el.clientWidth < 140));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const items = useMemo(() => plan?.items ?? [], [plan]);
  const exact = live ? null : moment.exact;
  // The bold paths the plan sends, and the chip's font: what the canvas
  // draws a changed look with until the next plan's sprites.
  useEffect(() => {
    for (const it of items) rememberPath(it.glyph.id, it.weight ?? "fill", it.d);
  }, [items]);
  useEffect(() => loadLabelFont(fonts), [fonts]);

  /** The box icon `id` is in now when the plan has it elsewhere: where a
   *  hand has it (a drag), else where the draft puts it before its plan
   *  lands. */
  const movedOf = (id: string): HarnessBox | null => {
    const l = liveRef.current;
    if (l && l.id === id) return l.box;
    const mine = userBoxOf(draft, id);
    return mine && !sameBox(mine, userBoxOf(planRecipe, id)) ? mine : null;
  };

  /** The icon on screen at the playhead under a click, if any. */
  const hitAt = (e: { clientX: number; clientY: number; currentTarget: Element }): string | null => {
    const r = e.currentTarget.getBoundingClientRect();
    if (!onPick || r.width <= 0 || r.height <= 0) return null;
    const fx = (e.clientX - r.left) / r.width;
    const fy = (e.clientY - r.top) / r.height;
    const t = player.tRef.current;
    const hit = items.find(
      (it) => t >= it.from && t < it.to && fx >= it.box.x && fx <= it.box.x + it.box.w && fy >= it.box.y && fy <= it.box.y + it.box.h,
    );
    return hit ? glyphId(hit.glyph) : null;
  };

  // Drawn with whatever is newest, from the frame clock or a redraw.
  const drawRef = useRef<(t: number) => void>(() => {});
  drawRef.current = (t: number) => {
    const c = canvasRef.current;
    if (!c) return;
    const W = c.clientWidth;
    const H = c.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) {
      c.width = Math.round(W * dpr);
      c.height = Math.round(H * dpr);
    }
    const ctx = c.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    if (exactVideo) return;
    const size: [number, number] = frameSize ? [frameSize.width, frameSize.height] : [W, H];
    const fps = frameSize?.fps ?? camera?.fps ?? 30;
    const s = W / Math.max(1, size[0]);
    const drawLayers = (front: boolean) => {
      for (const ly of layers ?? []) {
        if ((ly.z === "front") !== front) continue;
        const img = layerImgs.current.get(ly.art_url);
        if (img?.complete && img.naturalWidth > 0) drawLayer(ctx, ly, img, t, fps, size, camera, s, dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    if (layers?.length) drawLayers(false);
    else if (patternImg.current?.complete) {
      ctx.globalAlpha = 0.9;
      ctx.drawImage(patternImg.current, 0, 0, W, H);
      ctx.globalAlpha = 1;
    }
    const shown = iconsBaked
      ? []
      : items.map((it) => toDraw(ctx, it, draft, planRecipe, plan, patternColour, W, H, rules, movedOf(glyphId(it.glyph))));
    for (const sh of shown) if (!sh.front) drawItem(ctx, sh.item, t, { w: W, h: H }, rules);
    // Behind the speaker, as the run puts them.
    if (maskRef.current) {
      const mix = mixRef.current ?? (mixRef.current = document.createElement("canvas"));
      cutSpeaker(ctx, maskRef.current, mix, t, W, H);
    }
    // In front of her (the plan keeps those off her face), then any pattern
    // placed in front.
    for (const sh of shown) if (sh.front) drawItem(ctx, sh.item, t, { w: W, h: H }, rules);
    if (layers?.length) drawLayers(true);
  };

  // The patterns' art, uncut: read once each (a redraw when one lands).
  const layerUrls = (layers ?? []).map((l) => l.art_url).join("|");
  useEffect(() => {
    const known = layerImgs.current;
    const want = new Set((layers ?? []).map((l) => l.art_url));
    for (const url of Array.from(known.keys())) if (!want.has(url)) known.delete(url);
    for (const url of want) {
      if (known.has(url)) continue;
      const img = new Image();
      img.onload = () => drawRef.current(player.tRef.current);
      img.src = url;
      known.set(url, img);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layerUrls]);

  useEffect(() => {
    if (!patternUrl) {
      patternImg.current = null;
      return;
    }
    const img = new Image();
    img.onload = () => drawRef.current(player.tRef.current);
    img.src = patternUrl;
    patternImg.current = img;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [patternUrl]);

  const masksKey = masks ? `${masks.url}|${masks.times.length}` : "";
  useEffect(() => {
    maskRef.current = null;
    if (!masks) return;
    let dead = false;
    const img = new Image();
    img.onload = () => {
      if (dead) return;
      try {
        maskRef.current = maskFramesOf(img, masks);
      } catch {
        maskRef.current = null; // drawn uncut, as before
      }
      drawRef.current(player.tRef.current);
    };
    img.src = masks.url;
    return () => {
      dead = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [masksKey]);

  // Paused, or anything about the icons changed: draw once.
  useEffect(() => {
    if (!exact) drawRef.current(player.tRef.current);
  });

  // Art that arrives late (a glyph fetched by id), and resizes.
  useEffect(() => {
    const redraw = () => drawRef.current(player.tRef.current);
    const off = onArt(redraw);
    const c = canvasRef.current;
    const ro = c ? new ResizeObserver(redraw) : null;
    if (c && ro) ro.observe(c);
    return () => {
      off();
      ro?.disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [exact]);

  // Playing: every presented frame (requestVideoFrameCallback where there
  // is one), with a timer as well — a background or headless tab throttles
  // animation frames.
  useEffect(() => {
    if (!player.playing || exact) return;
    const v = player.videoRef.current as (HTMLVideoElement & {
      requestVideoFrameCallback?: (cb: (now: number, meta: { mediaTime: number }) => void) => number;
      cancelVideoFrameCallback?: (h: number) => void;
    }) | null;
    let stopped = false;
    let handle = 0;
    // A video still loading (just swapped in) has no time of its own yet:
    // the clock keeps its own until it has (the video is sought to it).
    const onFrame = (at?: number) => {
      if (stopped) return;
      if (v && v.readyState >= 1) {
        const now = at ?? v.currentTime;
        drawRef.current(now);
        player.tick(now);
      }
      schedule();
    };
    const schedule = () => {
      if (v?.requestVideoFrameCallback) handle = v.requestVideoFrameCallback((_n, meta) => onFrame(meta.mediaTime));
      else handle = window.requestAnimationFrame(() => onFrame());
    };
    schedule();
    const iv = window.setInterval(() => {
      if (!v || v.readyState < 1) return;
      drawRef.current(v.currentTime);
      player.tick(v.currentTime);
    }, 50);
    return () => {
      stopped = true;
      window.clearInterval(iv);
      if (v?.cancelVideoFrameCallback) v.cancelVideoFrameCallback(handle);
      else window.cancelAnimationFrame(handle);
    };
    // The video swapped (the exact one ready): followed from the new one.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [player.playing, exact, src]);

  const sel = selected ? items.find((it) => it.glyph.id === selected) : undefined;
  const spots = showSpots ? plan?.spots ?? [] : [];
  const home = plan?.home?.spot ?? null;

  // The picked icon's handles: paused where it shows (not over a video with
  // our icons in it already).
  const [box0, setBox0] = useState<{ w: number; h: number } | null>(null);
  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setBox0({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const frame = frameSize
    ? { w: frameSize.width, h: frameSize.height }
    : coverSamples
      ? { w: coverSamples.frame[0], h: coverSamples.frame[1] }
      : box0;
  const hand =
    onBox && sel && !player.playing && !iconsBaked && frame && frame.w > 0 && player.t >= sel.from && player.t < sel.to
      ? sel
      : null;
  const handBox: HarnessBox | null = hand
    ? (() => {
        const mine = userBoxOf(draft, hand.glyph.id);
        return mine && !sameBox(mine, userBoxOf(planRecipe, hand.glyph.id)) ? mine : hand.box;
      })()
    : null;
  const pad = rules.window_pad ?? 0.25;
  const toneOf = (b: Box): BoxTone => {
    if (!hand) return "plain";
    if (!coverSamples) return "plain";
    const c = coverOf(coverSamples, b, hand.from - pad, hand.to + pad, rules);
    return c === "over" ? "over" : c === "near" ? "near" : "clear";
  };
  const restTone: BoxTone = !hand
    ? "plain"
    : coverSamples && handBox
      ? toneOf(handBox)
      : hand.covered === "face"
        ? "over"
        : hand.covered === "speaker"
          ? "near"
          : "clear";
  const handChip = hand?.style === "chip";
  const handSize = hand && handBox && frame ? sizeOfBox(handBox, handChip, frame, rules) : 0;
  const sizeRange: [number, number] =
    handSize > 0 ? [(rules.size_min ?? 0.08) / handSize, (rules.size_max ?? 0.3) / handSize] : [1, 1];
  const snaps = useMemo(() => {
    const aspect = frame && frame.h > 0 ? frame.w / frame.h : 9 / 16;
    const x = [0, EDGE, 0.5, 1 - EDGE, 1];
    const y = [0, EDGE * aspect, 0.5, 1 - EDGE * aspect, 1];
    for (const sp of plan?.spots ?? []) {
      x.push(sp.box.x, sp.box.x + sp.box.w);
      y.push(sp.box.y, sp.box.y + sp.box.h);
    }
    return { x, y };
  }, [plan, frame]);
  const onHand = (t: Transform, opts: { final: boolean }, keyed = false) => {
    if (!hand || !handBox) return;
    const id = glyphId(hand.glyph);
    const b = iconBox(handBox, t);
    if (!opts.final) {
      setLive({ id, box: b });
      return;
    }
    setLive(null);
    if (!sameBox(b, handBox) && frame) {
      onBox?.(id, b, { resized: Math.abs(t.scale - 1) > 1e-6, key: keyed, size: sizeOfBox(b, handChip, frame, rules) });
    }
  };

  return (
    <div ref={rootRef} className="absolute inset-0">
      {exact ? (
        <img
          key={exact.url}
          src={exact.url}
          alt=""
          draggable={false}
          className="h-full w-full select-none object-contain"
          onClick={(e) => {
            const id = hitAt(e);
            if (id) onPick?.(id);
            else player.play();
          }}
          title={COPY.icons.exactTitle}
        />
      ) : src ? (
        <>
          <video
            key={src}
            ref={player.attach}
            src={src}
            poster={poster ?? undefined}
            playsInline
            preload="auto"
            muted={player.muted}
            data-stage={exactVideo ? "exact" : "quick"}
            onClick={(e) => {
              const id = hitAt(e);
              if (id) onPick?.(id);
              else player.toggle();
            }}
            className="h-full w-full cursor-pointer object-contain"
          />
          {!exactVideo && (
            <canvas ref={canvasRef} aria-hidden className="pointer-events-none absolute inset-0 h-full w-full" />
          )}
        </>
      ) : poster ? (
        <>
          <img
            src={poster}
            alt=""
            draggable={false}
            className="h-full w-full select-none object-contain"
            onClick={(e) => {
              const id = hitAt(e);
              if (id) onPick?.(id);
            }}
          />
          <canvas ref={canvasRef} aria-hidden className="pointer-events-none absolute inset-0 h-full w-full" />
        </>
      ) : null}

      {/* Where icons can go: clear, the AI's no (with why), blocked. */}
      {spots.map((s) => {
        const kind = !s.ok ? "blocked" : s.ai?.verdict === "avoid" ? "avoid" : "good";
        const title = [COPY.icons.spot[kind], s.ai?.why, s.id === home ? COPY.icons.spot.home : null]
          .filter(Boolean)
          .join(" — ");
        return (
          <span
            key={s.id}
            title={title}
            data-spot={kind}
            className={`absolute rounded-sm border-2 ${
              kind === "good"
                ? "border-emerald-400/90 bg-emerald-400/10"
                : kind === "avoid"
                  ? "border-dashed border-amber-400/90 bg-amber-400/10"
                  : "border-red-500/80 bg-red-500/10"
            } ${s.id === home ? "ring-2 ring-white/80" : ""}`}
            style={{ left: `${s.box.x * 100}%`, top: `${s.box.y * 100}%`, width: `${s.box.w * 100}%`, height: `${s.box.h * 100}%` }}
          >
            <span className="absolute left-0.5 top-0.5 rounded bg-black/60 px-1 text-[9px] font-medium leading-tight text-white">
              {s.id === home ? `${s.id} · ${COPY.icons.spot.home}` : s.id}
            </span>
          </span>
        );
      })}

      {/* The icon picked in the list or the lane (its handles, when it has them). */}
      {sel && !exact && !hand && (
        <span
          aria-hidden
          className="pointer-events-none absolute rounded-md border-2 border-dashed border-accent shadow-[0_0_0_1px_rgba(0,0,0,0.3)]"
          style={{
            left: `${sel.box.x * 100}%`,
            top: `${sel.box.y * 100}%`,
            width: `${sel.box.w * 100}%`,
            height: `${sel.box.h * 100}%`,
          }}
        />
      )}

      {hand && handBox && frame && (
        <TransformBox
          box={handBox}
          frame={frame}
          scaleRange={sizeRange}
          snaps={snaps}
          tone={restTone}
          toneOf={coverSamples ? toneOf : undefined}
          label={COPY.icons.handlesLabel(hand.label)}
          hint={COPY.icons.handlesHint}
          onChange={(t, opts) => onHand(t, opts, opts.final && !live)}
          onCancel={() => setLive(null)}
        />
      )}

      {card}

      {/* Play / pause, where it is, sound. */}
      <div className="absolute inset-x-2 bottom-2 flex items-center gap-2 rounded-full bg-black/65 px-1.5 py-1 text-2xs text-white shadow-lift">
        <button
          type="button"
          onClick={player.toggle}
          disabled={!src}
          aria-label={player.playing ? COPY.icons.playing : COPY.icons.paused}
          title={player.playing ? COPY.icons.playing : COPY.icons.paused}
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full hover:bg-white/15 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-40"
        >
          {player.playing ? <Pause className="h-3.5 w-3.5" aria-hidden /> : <Play className="h-3.5 w-3.5" aria-hidden />}
        </button>
        <span className="whitespace-nowrap font-mono tabular-nums">
          {clock(player.t)}
          {player.duration ? <span className="text-white/60"> / {clock(player.duration)}</span> : null}
        </span>
        <span className="ml-auto" />
        {!exact && !narrow && exactVideo ? (
          <span className="truncate font-medium text-white" title={COPY.stage.exactTitle}>
            ✓ {COPY.stage.exact}
          </span>
        ) : !exact && !narrow && layers?.length ? (
          <span className="truncate text-white/60" title={COPY.stage.quickTitle}>
            {COPY.stage.quick}
          </span>
        ) : (
          patternUrl &&
          !exact &&
          !narrow && (
            <span className="truncate text-white/60" title={COPY.icons.patternApproxTitle}>
              {COPY.icons.patternApprox}
            </span>
          )
        )}
        <button
          type="button"
          onClick={() => player.setMuted(!player.muted)}
          aria-label={player.muted ? COPY.icons.unmute : COPY.icons.mute}
          title={player.muted ? COPY.icons.unmute : COPY.icons.mute}
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full hover:bg-white/15 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {player.muted ? <VolumeX className="h-3.5 w-3.5" aria-hidden /> : <Volume2 className="h-3.5 w-3.5" aria-hidden />}
        </button>
      </div>
    </div>
  );
}

/** What the icon stage shows, said above it: the exact frame (the run's own),
 *  the moving preview (what it is drawn over), an exact frame on its way,
 *  a new plan on its way. */
export function IconStageStatus({
  moment,
  caption,
  updating,
  video = null,
}: {
  moment: IconMomentState;
  caption: string;
  updating: boolean;
  /** A draft made in house: whether the exact video plays, or how far it is
   *  (null: an Omni draft — the caption says what shows). */
  video?: { exact: boolean; progress: number | null; failed: boolean } | null;
}) {
  return (
    <span className="inline-flex min-w-0 max-w-full flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs text-muted" role="status">
      {moment.exact ? (
        <span className="font-medium text-accent-hover" title={COPY.icons.exactTitle}>
          ✓ {COPY.icons.exact}
        </span>
      ) : moment.loading ? (
        <span className="inline-flex items-center gap-1">
          <Spinner size="xs" tone="accent" /> {COPY.icons.exactLoading}
        </span>
      ) : video?.exact ? (
        <span className="font-medium text-accent-hover" title={COPY.stage.exactTitle}>
          ✓ {COPY.stage.exactVideo}
        </span>
      ) : video ? (
        <span className="inline-flex min-w-0 items-center gap-1" title={COPY.stage.quickTitle}>
          {COPY.stage.quick}
          {video.failed ? (
            <span className="text-muted"> · {COPY.stage.exactFailed}</span>
          ) : (
            <>
              <Spinner size="xs" tone="accent" /> {COPY.stage.making(video.progress)}
            </>
          )}
        </span>
      ) : (
        <span className="min-w-0">{caption}</span>
      )}
      {updating && (
        <span className="inline-flex items-center gap-1">
          <Spinner size="xs" tone="accent" /> {COPY.icons.updating}
        </span>
      )}
    </span>
  );
}
