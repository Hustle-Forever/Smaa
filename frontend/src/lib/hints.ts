// Client-side mirror of backend's services/settings_service.py::build_hints.
//
// Used by the Settings page to show a LIVE preview of what the next run will
// do without round-tripping to the backend. Keep in sync with the Python.

import type { Settings } from "./types";

const BUDGET_AUTO_PAGES = 12;
const BUDGET_AUTO_IMAGES = 10;
const BUDGET_AUTO_VIDEOS = 5;
const BUDGET_AUTO_UPLOADS = 8;
const BUDGET_TOTAL_MIX = 30;
const BASE_PAGES_PER_RUN = 50;
const BASE_IMAGES_PER_RUN = 60;

export interface Hints {
  mode: "auto" | "manual";
  limits: {
    pageSummaries: number;
    imageDescriptions: number;
    videoSummaries: number;
    ownerDocs: number;
  };
  emphasis: { voice: number; visual_identity: number; themes: number };
  emphasisText: string;
  skipImages: boolean;
  skipVideos: boolean;
  imageProcessingCap: number;
  pageProcessingCap: number;
  imagePromptPreamble: boolean;
}

function normalize(values: Record<string, number>): Record<string, number> {
  const total = Object.values(values).reduce((a, b) => a + b, 0);
  if (total <= 0) {
    const n = Math.max(1, Object.keys(values).length);
    return Object.fromEntries(Object.keys(values).map((k) => [k, 1 / n]));
  }
  return Object.fromEntries(Object.entries(values).map(([k, v]) => [k, v / total]));
}

function allocate(total: number, weights: Record<string, number>): Record<string, number> {
  const raw: Record<string, number> = {};
  const rounded: Record<string, number> = {};
  for (const [k, w] of Object.entries(weights)) {
    raw[k] = total * w;
    rounded[k] = Math.floor(total * w);
  }
  let deficit = total - Object.values(rounded).reduce((a, b) => a + b, 0);
  const fractional = Object.keys(raw)
    .map((k) => ({ k, frac: raw[k] - rounded[k] }))
    .sort((a, b) => b.frac - a.frac);
  for (let i = 0; i < deficit && i < fractional.length; i++) {
    rounded[fractional[i].k] += 1;
  }
  return rounded;
}

export function buildHints(s: Settings): Hints {
  if (s.mode === "auto") {
    return {
      mode: "auto",
      limits: {
        pageSummaries: BUDGET_AUTO_PAGES,
        imageDescriptions: BUDGET_AUTO_IMAGES,
        videoSummaries: BUDGET_AUTO_VIDEOS,
        ownerDocs: BUDGET_AUTO_UPLOADS,
      },
      emphasis: { voice: 1 / 3, visual_identity: 1 / 3, themes: 1 / 3 },
      emphasisText: "",
      skipImages: !s.include_images,
      skipVideos: !s.include_videos,
      imageProcessingCap: BASE_IMAGES_PER_RUN,
      pageProcessingCap: BASE_PAGES_PER_RUN,
      imagePromptPreamble: false,
    };
  }

  // Manual mode
  let mix = allocate(BUDGET_TOTAL_MIX, normalize({
    website: s.weight_website,
    uploads: s.weight_uploads,
  }));
  if (!s.include_website) {
    mix.uploads += mix.website;
    mix.website = 0;
  }
  if (!s.include_uploads) {
    mix.website += mix.uploads;
    mix.uploads = 0;
  }

  const modAlloc = allocate(mix.website, normalize({
    text: s.weight_text,
    visual: s.weight_visual,
    audio: s.weight_audio,
  }));

  const pageLimit = modAlloc.text;
  const imageLimit = s.include_images ? modAlloc.visual : 0;
  const videoLimit = s.include_videos ? modAlloc.audio : 0;
  const imageLimitPrompt = imageLimit * 2;

  const out = normalize({
    voice: s.weight_voice,
    visual_identity: s.weight_visual_identity,
    themes: s.weight_themes,
  });

  const pctV = Math.round(out.voice * 100);
  const pctVI = Math.round(out.visual_identity * 100);
  const pctT = Math.round(out.themes * 100);

  const emphasisText =
    `OUTPUT EMPHASIS — bias the brand profile toward: voice ${pctV}%, ` +
    `visual identity ${pctVI}%, themes ${pctT}%.`;

  const imageCap = Math.floor(BASE_IMAGES_PER_RUN * (1 + s.weight_visual / 50));
  const pageCap = Math.floor(BASE_PAGES_PER_RUN * (1 + s.weight_text / 50));

  const preamble = s.weight_visual_identity >= 50 || s.weight_visual >= 60;

  return {
    mode: "manual",
    limits: {
      pageSummaries: pageLimit,
      imageDescriptions: imageLimitPrompt,
      videoSummaries: videoLimit,
      ownerDocs: mix.uploads,
    },
    emphasis: { voice: out.voice, visual_identity: out.visual_identity, themes: out.themes },
    emphasisText,
    skipImages: !s.include_images || imageLimit === 0,
    skipVideos: !s.include_videos || videoLimit === 0,
    imageProcessingCap: Math.max(1, imageCap),
    pageProcessingCap: Math.max(1, pageCap),
    imagePromptPreamble: preamble,
  };
}
