/**
 * The page-wide line-art map behind every page, after the cyan city map on
 * 42abudhabi.ae: dense blocks along the left and right edges, long thin
 * streets crossing the page, and a few pixel boats, hearts, cars and a pin.
 *
 * It is drawn once (memoised), fixed to the viewport, never takes clicks, and
 * stays soft: low-alpha lines that fade out toward the middle where people
 * read. Colours come from --k-map (cyan in Dark, purple in Light).
 */
import { memo } from "react";

const W = 1440;
const H = 900;

/** Small deterministic random, so the map is the same on every load. */
function rng(seed: number) {
  let s = seed;
  return () => {
    s = (s * 16807) % 2147483647;
    return (s - 1) / 2147483646;
  };
}

/** A district of city blocks: streets every 30–60px, a few blocks missing. */
function district(x0: number, y0: number, w: number, h: number, seed: number) {
  const r = rng(seed);
  const cols: number[] = [x0];
  while (cols[cols.length - 1] < x0 + w) cols.push(cols[cols.length - 1] + 30 + Math.round(r() * 34));
  const rows: number[] = [y0];
  while (rows[rows.length - 1] < y0 + h) rows.push(rows[rows.length - 1] + 26 + Math.round(r() * 30));
  const out: Array<[number, number, number, number]> = [];
  const gap = 7;
  for (let i = 0; i < cols.length - 1; i++) {
    for (let j = 0; j < rows.length - 1; j++) {
      if (r() < 0.22) continue; // a square, a park, a gap
      const bx = cols[i] + gap / 2;
      const by = rows[j] + gap / 2;
      const bw = cols[i + 1] - cols[i] - gap;
      const bh = rows[j + 1] - rows[j] - gap;
      // sometimes split a block in two, like the website's map
      if (r() < 0.25 && bw > 34) {
        const half = Math.round(bw / 2) - 2;
        out.push([bx, by, half, bh], [bx + half + 4, by, bw - half - 4, bh]);
      } else {
        out.push([bx, by, bw, bh]);
      }
    }
  }
  return out;
}

const BLOCKS = [
  ...district(-10, 120, 230, 330, 7),
  ...district(-10, 560, 170, 360, 19),
  ...district(1210, -10, 250, 300, 31),
  ...district(1250, 420, 210, 500, 43),
  ...district(620, 790, 260, 140, 59),
];

// Long streets crossing the page (the website's thin straight lines).
const STREETS = [
  "M-20 470 L420 410 L1460 470",
  "M-20 690 L560 560 L1460 640",
  "M120 -20 L520 930",
  "M-20 160 L700 300",
  "M760 -20 L690 260 L1460 120",
  "M1460 330 L980 520 L840 930",
  "M300 930 L900 640 L1460 760",
  "M1080 -20 L1180 930",
  "M-20 860 L260 770",
];

/** Diamond "beads" along a few streets (the website's dotted circuit lines),
 *  every ~110px, glowing one after another along the street. */
function beadsAlong(d: string, every = 110): Array<[number, number, number]> {
  const pts = [...d.matchAll(/-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?/g)].map((m) => m[0].split(/\s+/).map(Number));
  const out: Array<[number, number, number]> = [];
  let walked = 0;
  for (let i = 0; i < pts.length - 1; i++) {
    const [x1, y1] = pts[i];
    const [x2, y2] = pts[i + 1];
    const len = Math.hypot(x2 - x1, y2 - y1);
    for (let t = every - (walked % every); t < len; t += every) {
      out.push([x1 + ((x2 - x1) * t) / len, y1 + ((y2 - y1) * t) / len, out.length]);
    }
    walked += len;
  }
  return out;
}
const BEADS = [3, 7, 4].flatMap((i) => beadsAlong(STREETS[i]));

/** Streets that carry a travelling light. */
const PULSES = [0, 1, 5, 2, 6];

type Sprite = "pin" | "heart" | "boat" | "car";
const SPRITE_PX: Record<Sprite, string[]> = {
  pin: [".###.", "#...#", "#.#.#", "#...#", ".#.#.", "..#.."],
  heart: [".#.#.", "#####", "#####", ".###.", "..#.."],
  boat: ["..#...", "..##..", "..###.", "..#...", "######", ".####."],
  car: ["..###...", ".#...#..", "########", "#.####.#", ".#....#."],
};
const SPRITES: Array<[Sprite, number, number]> = [
  ["boat", 860, 360], ["boat", 905, 392], ["boat", 960, 350], ["boat", 1010, 400], ["boat", 930, 430],
  ["heart", 236, 520], ["heart", 236, 540],
  ["heart", 1380, 330], ["heart", 1380, 350], ["heart", 1380, 370],
  ["car", 380, 690], ["car", 402, 690], ["car", 424, 690], ["car", 446, 690],
  ["pin", 1032, 498],
];

function SpriteMark({ kind, x, y }: { kind: Sprite; x: number; y: number }) {
  return (
    <g transform={`translate(${x} ${y}) scale(2.2)`} shapeRendering="crispEdges">
      {SPRITE_PX[kind].flatMap((row, ry) =>
        [...row].map((c, rx) => (c === "#" ? <rect key={`${rx}-${ry}`} x={rx} y={ry} width={1} height={1} /> : null)),
      )}
    </g>
  );
}

export const MapBackdrop = memo(function MapBackdrop({
  className = "",
  variant = "page",
}: {
  className?: string;
  /** "page": fixed behind the whole page, soft. "panel": fills its (charcoal)
   *  parent, cyan in both themes and a little stronger (the sign-in panel). */
  variant?: "page" | "panel";
}) {
  return (
    <div className={`${variant === "panel" ? "k-backdrop-panel" : "k-backdrop"} pointer-events-none select-none ${className}`} aria-hidden>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid slice" width="100%" height="100%" focusable="false">
        <g className="k-backdrop-blocks" fill="none" strokeWidth={1}>
          {BLOCKS.map(([x, y, w, h], i) => (
            <rect key={i} x={x} y={y} width={w} height={h} vectorEffect="non-scaling-stroke" />
          ))}
        </g>
        <g className="k-backdrop-streets" fill="none" strokeWidth={1.2}>
          {STREETS.map((d, i) => (
            <path key={i} d={d} vectorEffect="non-scaling-stroke" />
          ))}
        </g>
        {/* Light running along a few streets, one after another (the
            website's travelling pulse). Still when reduced motion is on. */}
        <g className="k-backdrop-pulses" fill="none" strokeWidth={1.8} strokeLinecap="round">
          {PULSES.map((i, n) => (
            <path
              key={i}
              d={STREETS[i]}
              pathLength={1000}
              vectorEffect="non-scaling-stroke"
              style={{ animationDelay: `${n * 2.6}s` }}
            />
          ))}
        </g>
        <g className="k-backdrop-beads">
          {BEADS.map(([x, y, n], i) => (
            <rect
              key={i}
              x={x - 3}
              y={y - 3}
              width={6}
              height={6}
              transform={`rotate(45 ${x} ${y})`}
              style={{ animationDelay: `${(n % 12) * 0.35}s` }}
            />
          ))}
        </g>
        <g className="k-backdrop-sprites">
          {SPRITES.map(([k, x, y], i) => (
            <SpriteMark key={i} kind={k} x={x} y={y} />
          ))}
        </g>
      </svg>
    </div>
  );
});
