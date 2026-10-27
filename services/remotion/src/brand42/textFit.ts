/**
 * Fits the optional heading + sub-line into the bars, and reports how.
 *
 * Sizes start at the reel's (VG5000 158 px; Jost 47 px on a 55.7 px pitch,
 * @720) and shrink one percent at a time until the block fits its region:
 *  - the heading may wrap to 2 lines (balanced) and shrink to 40% (50% in the
 *    top bar); the sub-line shrinks with it but never below 60%, wrapping to
 *    at most 3 lines;
 *  - regions are tried best first (top bar under the band, then the bottom
 *    bar), and within the three forms in this order: heading + sub, sub only,
 *    heading only. A heading too long for the bars therefore costs the
 *    heading, never the sub-line (the old rule dropped the whole text, sub
 *    included, whenever a heading of more than ~7 characters did not fit).
 * Nothing is truncated: a part that cannot fit is left out and REPORTED
 * (`TextFit`), and the backend tells the user.
 *
 * Widths come from canvas measureText with the real faces loaded, so this must
 * run after loadBrand42Fonts() resolved. A 2% allowance covers the difference
 * between canvas and SVG kerning.
 */
import type { BarsGeometry, TextRegion } from "./geometry";
import { JOST_METRICS, TYPE, VG5000_FAMILY, VG5000_METRICS } from "./tokens";

export type Measure = (font: string, text: string) => number;

export type TextLines = { lines: string[]; size: number; x: number; baselines: number[]; widths: number[] };

export type TextLayout = {
  placement: TextRegion["placement"];
  /** Heading / sub size as a share of the reel's (1 = full size). */
  headingScale: number;
  subScale: number;
  heading: TextLines | null;
  sub: TextLines | null;
  /** The block's box (a scrim keeps the sliding pattern off the words). */
  left: number;
  top: number;
  right: number;
  bottom: number;
};

export type FitStatus = "shown" | "shrunk" | "partial" | "dropped";

/** How one segment's text came out. */
export type TextFit = {
  status: FitStatus;
  placement: TextRegion["placement"] | null;
  heading_shown: boolean;
  sub_shown: boolean;
  heading_scale: number | null;
  sub_scale: number | null;
  heading_lines: string[];
  sub_lines: string[];
  /** Why a requested part is missing (empty when nothing is). */
  reason: string;
};

const ALLOW = 1.02;

export const headingFont = (px: number) => `400 ${px}px "${VG5000_FAMILY}"`;
export const subFont = (family: string, px: number) => `400 ${px}px "${family}"`;

export function canvasMeasure(): Measure {
  const ctx = document.createElement("canvas").getContext("2d");
  if (!ctx) throw new Error("brand-overlay-42: no 2d canvas to measure text with");
  return (font, text) => {
    ctx.font = font;
    return ctx.measureText(text).width * ALLOW;
  };
}

/** Greedy wrap; null when a single word is wider than the block. */
function wrap(words: string[], maxWidth: number, width: (t: string) => number): string[] | null {
  const lines: string[] = [];
  let line = "";
  for (const word of words) {
    const next = line ? `${line} ${word}` : word;
    if (width(next) <= maxWidth) {
      line = next;
      continue;
    }
    if (!line) return null;
    lines.push(line);
    line = word;
    if (width(line) > maxWidth) return null;
  }
  if (line) lines.push(line);
  return lines;
}

/** One line if it fits, else the most balanced two-line split that fits; null if neither. */
function headingLines(text: string, maxWidth: number, width: (t: string) => number): string[] | null {
  if (width(text) <= maxWidth) return [text];
  if (TYPE.headingMaxLines < 2) return null;
  const words = text.split(" ");
  let best: string[] | null = null;
  let bestW = Infinity;
  for (let i = 1; i < words.length; i++) {
    const a = words.slice(0, i).join(" ");
    const b = words.slice(i).join(" ");
    const w = Math.max(width(a), width(b));
    if (w <= maxWidth && w < bestW) {
      best = [a, b];
      bestW = w;
    }
  }
  return best;
}

type Form = "both" | "sub" | "heading";

export function fitText(
  text: { heading: string; sub: string } | null,
  geom: BarsGeometry,
  subFamily: string,
  measure: Measure,
): { layout: TextLayout | null; fit: TextFit } {
  const heading = (text?.heading ?? "").trim().replace(/\s+/g, " ");
  const sub = (text?.sub ?? "").trim().replace(/\s+/g, " ");
  const words = sub ? sub.split(" ") : [];
  const { s } = geom;

  const at = (region: TextRegion, kh: number, form: Form): TextLayout | null => {
    const withHeading = form !== "sub";
    const withSub = form !== "heading";
    const maxW = region.right - region.left;
    const maxH = region.bottom - region.top;
    if (maxW <= 0 || maxH <= 0) return null;
    // The sub follows the heading's scale down to its own floor; alone, it
    // shrinks on its own scale (kh is then the sub's).
    const ks = withHeading ? Math.max(kh, TYPE.subMinScale) : kh;
    const hs = TYPE.headingSize * s * kh;
    const ss = TYPE.subSize * s * ks;

    let head: TextLines | null = null;
    let y = 0; // block-relative, from the block's top
    let lastBaseline = 0;
    if (withHeading) {
      const lines = headingLines(heading, maxW, (t) => measure(headingFont(hs), t));
      if (!lines) return null;
      const baselines = lines.map((_, i) => VG5000_METRICS.capHeight * hs + i * TYPE.headingPitch * hs);
      const widths = lines.map((l) => measure(headingFont(hs), l));
      // Place the ink (not the advance box) at the block's left edge.
      head = { lines, size: hs, x: region.left - VG5000_METRICS.leftBearing * hs, baselines, widths };
      lastBaseline = baselines[baselines.length - 1];
      y = lastBaseline + VG5000_METRICS.descent * hs;
    }
    let subL: TextLines | null = null;
    if (withSub) {
      const lines = wrap(words, maxW, (t) => measure(subFont(subFamily, ss), t));
      if (!lines || lines.length > TYPE.subMaxLines) return null;
      const first = head
        ? lastBaseline + TYPE.headingToSubGap * s * ks + JOST_METRICS.capHeight * ss
        : JOST_METRICS.capHeight * ss;
      const baselines = lines.map((_, i) => first + i * TYPE.subPitch * s * ks);
      const widths = lines.map((l) => measure(subFont(subFamily, ss), l));
      subL = { lines, size: ss, x: region.left, baselines, widths };
      y = baselines[baselines.length - 1] + JOST_METRICS.descent * ss;
    }
    if (y > maxH) return null;

    // Block-relative -> canvas: top-aligned, or centred in the region.
    const top = region.valign === "centre" ? region.top + (maxH - y) / 2 : region.top;
    const place = (t: TextLines | null) => (t ? { ...t, baselines: t.baselines.map((b) => b + top) } : null);
    const widest = Math.max(0, ...(head?.widths ?? []), ...(subL?.widths ?? []));
    return {
      placement: region.placement,
      headingScale: withHeading ? kh : 0,
      subScale: withSub ? ks : 0,
      heading: place(head),
      sub: place(subL),
      left: region.left,
      top,
      right: region.left + widest,
      bottom: top + y,
    };
  };

  const search = (form: Form): TextLayout | null => {
    for (const region of geom.textRegions) {
      const floor = form === "sub" ? TYPE.subMinScale : region.minScale;
      for (let pct = 100; pct >= Math.round(floor * 100); pct -= 1) {
        const fit = at(region, pct / 100, form);
        if (fit) return fit;
      }
    }
    return null;
  };

  const forms: Form[] = heading && sub ? ["both", "sub", "heading"] : heading ? ["heading"] : sub ? ["sub"] : [];
  let layout: TextLayout | null = null;
  for (const form of forms) {
    layout = search(form);
    if (layout) break;
  }
  return { layout, fit: describe(layout, Boolean(heading), Boolean(sub)) };
}

function describe(layout: TextLayout | null, wantHeading: boolean, wantSub: boolean): TextFit {
  const headingShown = Boolean(layout?.heading);
  const subShown = Boolean(layout?.sub);
  const missing = [wantHeading && !headingShown ? "heading" : "", wantSub && !subShown ? "sub-line" : ""].filter(Boolean);
  let status: FitStatus;
  if (!layout) status = "dropped";
  else if (missing.length) status = "partial";
  else status = (layout.heading && layout.headingScale < 1) || (layout.sub && layout.subScale < 1) ? "shrunk" : "shown";
  return {
    status,
    placement: layout?.placement ?? null,
    heading_shown: headingShown,
    sub_shown: subShown,
    heading_scale: layout?.heading ? layout.headingScale : null,
    sub_scale: layout?.sub ? layout.subScale : null,
    heading_lines: layout?.heading?.lines ?? [],
    sub_lines: layout?.sub?.lines ?? [],
    reason: missing.length ? `${missing.join(" and ")} too long for the letterbox bars` : "",
  };
}

// ------------------------------------------------------------ whole reel ----

/** What the backend gets back (job JSON `text_fit`, /still response `text_fit`). */
export type TextFitReport = {
  /** "none" = no text was asked for. Otherwise the worst scene's outcome. */
  status: "none" | FitStatus;
  /** One sentence for the user, e.g. "Sub-line dropped: too long for the letterbox bars." */
  message: string;
  heading_shown: boolean;
  sub_shown: boolean;
  /** Smallest heading / sub scale used on any scene (null = that part shown nowhere). */
  min_heading_scale: number | null;
  min_sub_scale: number | null;
  /** One entry per scene segment (`segment` = its position in props.segments). */
  scenes: Array<TextFit & { segment: number; index: number }>;
};

type SceneInput = { i: number; kind: string; index: number; geom: import("./geometry").Geometry };

const RANK: Record<FitStatus, number> = { shown: 0, shrunk: 1, partial: 2, dropped: 3 };

/**
 * Lays the text out for every scene segment (text only ever shows on scenes,
 * and only on a "bars" layout) and summarises the outcome. Segments with the
 * same bars share one fit.
 */
export function planReelText(
  text: { heading: string; sub: string } | null,
  segments: SceneInput[],
  subFamily: string,
  measure: Measure | null,
): { layouts: Record<string, TextLayout | null>; report: TextFitReport } {
  const wantHeading = Boolean(text?.heading?.trim());
  const wantSub = Boolean(text?.sub?.trim());
  const layouts: Record<string, TextLayout | null> = {};
  const empty: TextFitReport = {
    status: "none",
    message: "",
    heading_shown: false,
    sub_shown: false,
    min_heading_scale: null,
    min_sub_scale: null,
    scenes: [],
  };
  if (!wantHeading && !wantSub) return { layouts, report: empty };
  if (!measure) throw new Error("brand-overlay-42: text needs a measure function");

  const cache = new Map<string, { layout: TextLayout | null; fit: TextFit }>();
  const scenes: TextFitReport["scenes"] = [];
  for (const m of segments) {
    if (m.kind !== "scene") continue;
    if (m.geom.layout !== "bars") {
      scenes.push({
        segment: m.i,
        index: m.index,
        status: "dropped",
        placement: null,
        heading_shown: false,
        sub_shown: false,
        heading_scale: null,
        sub_scale: null,
        heading_lines: [],
        sub_lines: [],
        reason: "no letterbox bars on this scene (the full-frame layout carries no text)",
      });
      continue;
    }
    const g = m.geom;
    const key = JSON.stringify([g.W, g.H, g.content]);
    let res = cache.get(key);
    if (!res) {
      res = fitText(text, g, subFamily, measure);
      cache.set(key, res);
    }
    layouts[String(m.i)] = res.layout;
    scenes.push({ segment: m.i, index: m.index, ...res.fit });
  }

  if (!scenes.length) {
    return { layouts, report: { ...empty, status: "dropped", message: "Text dropped: the reel has no scene to put it on." } };
  }
  let status: FitStatus = scenes.reduce<FitStatus>((w, sc) => (RANK[sc.status] > RANK[w] ? sc.status : w), "shown");
  const dropped = scenes.filter((sc) => sc.status === "dropped").length;
  if (status === "dropped" && dropped < scenes.length) status = "partial";
  const scales = (k: "heading_scale" | "sub_scale") => {
    const v = scenes.map((sc) => sc[k]).filter((x): x is number => x !== null);
    return v.length ? Math.min(...v) : null;
  };
  const report: TextFitReport = {
    status,
    message: "",
    heading_shown: scenes.some((sc) => sc.heading_shown),
    sub_shown: scenes.some((sc) => sc.sub_shown),
    min_heading_scale: scales("heading_scale"),
    min_sub_scale: scales("sub_scale"),
    scenes,
  };
  report.message = message(report, wantHeading, wantSub, dropped);
  return { layouts, report };
}

function message(r: TextFitReport, wantHeading: boolean, wantSub: boolean, dropped: number): string {
  const pct = (k: number | null) => (k === null ? "" : `${Math.round(k * 100)}%`);
  const noBars = r.scenes.every((sc) => sc.status !== "dropped" || sc.reason.startsWith("no letterbox"));
  if (r.status === "shown") return "Text shown at full size.";
  if (r.status === "shrunk") {
    const k = Math.min(r.min_heading_scale ?? 1, r.min_sub_scale ?? 1);
    return `Text shrunk to ${pct(k)} to fit the letterbox bars.`;
  }
  if (r.status === "dropped") {
    return noBars && r.scenes.some((sc) => sc.reason.startsWith("no letterbox"))
      ? "Text dropped: the video has no letterbox bars to put it in."
      : "Text dropped: too long for the letterbox bars.";
  }
  // partial
  const lost = [wantHeading && !r.heading_shown ? "Heading" : "", wantSub && !r.sub_shown ? "Sub-line" : ""].filter(Boolean);
  if (lost.length) return `${lost.join(" and ")} dropped: too long for the letterbox bars.`;
  if (dropped) return `Text shown on ${r.scenes.length - dropped} of ${r.scenes.length} scenes (the others have no room for it).`;
  const partly = r.scenes.find((sc) => sc.status === "partial");
  return partly ? `Part of the text dropped on scene ${partly.index + 1}: ${partly.reason}.` : "Text partly shown.";
}
