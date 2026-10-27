/**
 * Small line drawings for the Ideation hub: one per tool, each showing what
 * comes OUT of that tool (a poster, a moving poster, a person, a talking
 * video, a film strip, a timeline). Drawn in code so they stay crisp, cost no
 * requests, and look right even when the library is empty.
 *
 * House style: ink strokes, white fills, a single gold accent. No colour per
 * tool; the drawing itself tells them apart.
 */
import type { ReactNode } from "react";

export type IllustrationKind =
  | "poster"
  | "animation"
  | "poster_human"
  | "human_character"
  | "talking_head_v2"
  | "general_video_category"
  | "general_video"
  | "broll"
  | "video_editor";

// Theme colours (styles.css): the drawings follow 42 Dark / 42 Light.
const INK = "rgb(var(--k-text))";
const MID = "rgb(var(--k-text) / 0.45)";
const SOFT = "rgb(var(--k-border))";
const GOLD = "rgb(var(--k-accent))";
const PAPER = "rgb(var(--k-surface))";

function Frame({ children, wide = false }: { children: ReactNode; wide?: boolean }) {
  return (
    <svg
      viewBox="0 0 240 135"
      className="w-full h-full"
      fill="none"
      stroke={INK}
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      preserveAspectRatio={wide ? "xMidYMid meet" : "xMidYMid meet"}
    >
      {children}
    </svg>
  );
}

/** Head + shoulders, centred on (cx, cy) at scale s. */
function Person({ cx, cy, s = 1, fill = INK }: { cx: number; cy: number; s?: number; fill?: string }) {
  return (
    <g fill={fill} stroke="none">
      <circle cx={cx} cy={cy - 12 * s} r={9 * s} />
      <path d={`M${cx - 20 * s},${cy + 20 * s} C${cx - 20 * s},${cy + 2 * s} ${cx - 10 * s},${cy - 1 * s} ${cx},${cy - 1 * s} C${cx + 10 * s},${cy - 1 * s} ${cx + 20 * s},${cy + 2 * s} ${cx + 20 * s},${cy + 20 * s} Z`} />
    </g>
  );
}

function PosterCard({ x = 88, y = 14, w = 64, h = 107 }: { x?: number; y?: number; w?: number; h?: number }) {
  return (
    <g>
      <rect x={x + 4} y={y + 5} width={w} height={h} rx={0} fill={SOFT} stroke="none" />
      <rect x={x} y={y} width={w} height={h} rx={0} fill={PAPER} />
    </g>
  );
}

export function ToolIllustration({ kind }: { kind: IllustrationKind }) {
  switch (kind) {
    case "poster":
      return (
        <Frame>
          <PosterCard />
          <circle cx={134} cy={36} r={9} fill={GOLD} stroke="none" />
          <rect x={98} y={62} width={40} height={6} rx={2} fill={INK} stroke="none" />
          <rect x={98} y={73} width={30} height={6} rx={2} fill={INK} stroke="none" />
          <path d="M98 88h44M98 95h36M98 102h40" stroke={MID} />
          <rect x={98} y={108} width={9} height={7} rx={1.5} fill={INK} stroke="none" />
        </Frame>
      );
    case "animation":
      return (
        <Frame>
          <PosterCard x={80} />
          <circle cx={126} cy={40} r={9} fill={GOLD} stroke="none" />
          <rect x={90} y={66} width={40} height={6} rx={2} fill={INK} stroke="none" />
          <rect x={90} y={77} width={28} height={6} rx={2} fill={INK} stroke="none" />
          <path d="M90 92h40M90 99h32" stroke={MID} />
          {/* motion */}
          <path d="M156 44h16M160 54h22M156 64h14" stroke={MID} strokeWidth={2} />
          <circle cx={146} cy={108} r={13} fill={INK} stroke={PAPER} strokeWidth={2.5} />
          <path d="M142.5 102.5v11l9-5.5z" fill={PAPER} stroke="none" />
        </Frame>
      );
    case "poster_human":
      return (
        <Frame>
          <PosterCard />
          <rect x={98} y={24} width={40} height={6} rx={2} fill={INK} stroke="none" />
          <rect x={98} y={34} width={26} height={4} rx={2} fill={MID} stroke="none" />
          <rect x={96} y={46} width={48} height={67} rx={3} fill={SOFT} stroke="none" />
          <Person cx={120} cy={88} s={1.05} />
          <circle cx={137} cy={53} r={4} fill={GOLD} stroke="none" />
        </Frame>
      );
    case "human_character":
      return (
        <Frame>
          {/* the one you make */}
          <circle cx={120} cy={66} r={38} fill={PAPER} />
          <clipPath id="hc-clip">
            <circle cx={120} cy={66} r={37} />
          </clipPath>
          <g clipPath="url(#hc-clip)">
            <Person cx={120} cy={84} s={1.35} />
          </g>
          {/* reused later */}
          <path d="M163 44c14 6 20 16 20 26" stroke={GOLD} strokeDasharray="3 4" />
          <path d="M77 88c-14-6-20-16-20-26" stroke={GOLD} strokeDasharray="3 4" />
          <circle cx={190} cy={82} r={15} fill={PAPER} />
          <Person cx={190} cy={89} s={0.55} fill={MID} />
          <circle cx={50} cy={50} r={15} fill={PAPER} />
          <Person cx={50} cy={57} s={0.55} fill={MID} />
        </Frame>
      );
    case "talking_head_v2":
      return (
        <Frame>
          <rect x={44} y={24} width={152} height={88} rx={8} fill={SOFT} stroke="none" transform="translate(4 5)" />
          <rect x={44} y={24} width={152} height={88} rx={8} fill={PAPER} />
          <rect x={52} y={32} width={80} height={72} rx={4} fill={SOFT} stroke="none" />
          <Person cx={92} cy={82} s={1.1} />
          {/* voice */}
          <g stroke={GOLD} strokeWidth={3}>
            <path d="M146 64v10M154 58v22M162 52v34M170 60v18M178 64v10" />
          </g>
          <path d="M52 112h96" stroke={INK} strokeWidth={2.5} transform="translate(0 -3)" />
          <path d="M148 109h40" stroke={SOFT} strokeWidth={2.5} />
        </Frame>
      );
    case "general_video_category":
    case "general_video":
      return (
        <Frame>
          {/* describe → video */}
          <rect x={30} y={40} width={70} height={56} rx={6} fill={PAPER} />
          <path d="M40 54h50M40 63h42M40 72h48M40 81h30" stroke={MID} />
          <path d="M110 68h20m0 0-6-6m6 6-6 6" stroke={GOLD} strokeWidth={2} />
          <rect x={140} y={34} width={76} height={68} rx={6} fill={INK} stroke="none" />
          <circle cx={178} cy={68} r={13} fill={PAPER} stroke="none" />
          <path d="M174.5 62.5v11l9-5.5z" fill={INK} stroke="none" />
        </Frame>
      );
    case "broll":
      return (
        <Frame>
          {/* your clips … */}
          <rect x={34} y={30} width={40} height={28} rx={4} fill={PAPER} />
          <rect x={82} y={30} width={40} height={28} rx={4} fill={SOFT} />
          <rect x={130} y={30} width={40} height={28} rx={4} fill={PAPER} />
          <path d="M54 62v10M102 62v10M150 62v10" stroke={MID} />
          <path d="M54 72h96" stroke={MID} />
          <path d="M102 72v10" stroke={GOLD} strokeWidth={2} />
          {/* … joined into one */}
          <rect x={40} y={86} width={160} height={24} rx={5} fill={INK} stroke="none" />
          <path d="M92 86v24M144 86v24" stroke={PAPER} strokeOpacity={0.35} />
          <circle cx={186} cy={98} r={5} fill={GOLD} stroke="none" />
        </Frame>
      );
    case "video_editor":
      return (
        <Frame wide>
          {/* timeline */}
          <rect x={24} y={30} width={192} height={82} rx={8} fill={PAPER} />
          <path d="M24 46h192" stroke={SOFT} />
          <path d="M40 38h4M56 38h4M72 38h4M88 38h4M104 38h4M120 38h4M136 38h4M152 38h4M168 38h4M184 38h4M200 38h4" stroke={MID} strokeWidth={1} />
          <rect x={34} y={54} width={62} height={14} rx={3} fill={INK} stroke="none" />
          <rect x={100} y={54} width={80} height={14} rx={3} fill={INK} stroke="none" />
          <rect x={48} y={74} width={96} height={12} rx={3} fill={MID} stroke="none" />
          <rect x={34} y={92} width={150} height={10} rx={3} fill={SOFT} stroke="none" />
          {/* playhead */}
          <path d="M128 46v62" stroke={GOLD} strokeWidth={2} />
          <path d="M122 42h12l-6 7z" fill={GOLD} stroke="none" />
        </Frame>
      );
  }
}
