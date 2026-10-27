import React from "react";

/** A full-canvas SVG layer in output px. */
export const FrameSvg: React.FC<{ W: number; H: number; children: React.ReactNode; style?: React.CSSProperties }> = ({
  W,
  H,
  children,
  style,
}) => (
  <svg
    width={W}
    height={H}
    viewBox={`0 0 ${W} ${H}`}
    style={{ position: "absolute", left: 0, top: 0, ...style }}
  >
    {children}
  </svg>
);

/** SVG `points` from [x, y] pairs, shifted by (dx, dy). */
export const pts = (points: ReadonlyArray<readonly [number, number]>, dx = 0, dy = 0): string =>
  points.map(([x, y]) => `${x + dx},${y + dy}`).join(" ");

/** How far every slab polygon runs past the canvas, so no motion exposes its end. */
export const beyond = (W: number, H: number) => W + H;
