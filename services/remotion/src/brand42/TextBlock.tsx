import React from "react";

import { FrameSvg } from "./svg";
import type { TextLayout } from "./textFit";
import { MOTION, VG5000_FAMILY } from "./tokens";

/**
 * Heading (VG5000, violet A) and sub-line (Jost 400, white), typed on the way
 * r128 types them: the heading at 3.33 frames per character with a trailing
 * "_" cursor that goes when the heading is complete (a wrapped heading types
 * its lines in turn, the line break counting as one character), then the sub
 * at 1.14 characters per frame, no cursor. Placement is decided by fitText.
 */
export const TextBlock: React.FC<{
  W: number;
  H: number;
  layout: TextLayout;
  frame: number;
  typeStart: number;
  subFamily: string;
  violet: string;
  ink: string;
}> = ({ W, H, layout, frame, typeStart, subFamily, violet, ink }) => {
  if (frame < typeStart) return null;
  const { heading, sub } = layout;
  const elapsed = frame - typeStart;

  const headingLines: string[] = [];
  let subStart = 0;
  if (heading) {
    const n = heading.lines.reduce((a, l) => a + l.length, 0) + heading.lines.length - 1;
    const typed = Math.min(n, Math.floor(elapsed / MOTION.headingFramesPerChar) + 1);
    let budget = typed;
    for (const line of heading.lines) {
      if (budget <= 0) break;
      headingLines.push(line.slice(0, budget));
      budget -= line.length + 1; // the wrapped space
    }
    if (typed < n) headingLines[headingLines.length - 1] += "_";
    subStart = Math.ceil(n * MOTION.headingFramesPerChar);
  }

  const subLines: string[] = [];
  if (sub && elapsed >= subStart) {
    let budget = Math.floor((elapsed - subStart) * MOTION.subCharsPerFrame) + 1;
    for (const line of sub.lines) {
      if (budget <= 0) break;
      subLines.push(line.slice(0, budget));
      budget -= line.length + 1; // the wrapped space
    }
  }

  return (
    <FrameSvg W={W} H={H}>
      {heading
        ? headingLines.map((line, i) => (
            <text
              key={`h${i}`}
              x={heading.x}
              y={heading.baselines[i]}
              fontFamily={`"${VG5000_FAMILY}"`}
              fontWeight={400}
              fontSize={heading.size}
              fill={violet}
              style={{ whiteSpace: "pre" }}
            >
              {line}
            </text>
          ))
        : null}
      {sub
        ? subLines.map((line, i) => (
            <text
              key={`s${i}`}
              x={sub.x}
              y={sub.baselines[i]}
              fontFamily={`"${subFamily}"`}
              fontWeight={400}
              fontSize={sub.size}
              fill={ink}
              style={{ whiteSpace: "pre" }}
            >
              {line}
            </text>
          ))
        : null}
    </FrameSvg>
  );
};
