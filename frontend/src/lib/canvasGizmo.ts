/**
 * Video Editor — pure geometry for the on-canvas transform gizmo.
 *
 * Maps the clip transform model (x/y in composition px, uniform `scale`,
 * `rotation` in degrees, transform-origin = canvas CENTER — exactly how
 * `VideoEdit.tsx`'s ClipLayer applies `translate(x,y) scale(s) rotate(r)` to a
 * full-frame layer) to/from on-screen pixels, and computes corner/rotate handle
 * positions + hit-testing. All "screen" coords here are **container-relative**
 * (subtract the overlay container's origin) so the overlay can position with
 * plain absolute CSS.
 */

export interface DOMRectLike {
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface GizmoTransform {
  x: number; // composition px translate
  y: number;
  scale: number; // uniform scalar (1 = 100%)
  rotation: number; // degrees
}

export interface Pt {
  x: number;
  y: number;
}

export interface GizmoBox {
  cx: number; // center (container-relative screen px)
  cy: number;
  w: number; // on-screen width/height (already × scale × screenScale)
  h: number;
  rot: number; // degrees
  s: number; // screenScale = screen px per composition px
}

/** Screen px per composition px (uniform — the Player preserves aspect). */
export function screenScale(rootRect: DOMRectLike, compW: number): number {
  return rootRect.width / Math.max(1, compW);
}

/** The element's on-screen oriented box from its transform + natural (comp-px)
 * size. `rootRect`/`containerRect` are getBoundingClientRect() (viewport px). */
export function computeGizmoBox(
  t: GizmoTransform,
  naturalW: number,
  naturalH: number,
  rootRect: DOMRectLike,
  containerRect: DOMRectLike,
  compW: number,
  compH: number,
): GizmoBox {
  const s = screenScale(rootRect, compW);
  // Element center = canvas center + translate (translate is NOT scaled).
  const cxComp = compW / 2 + t.x;
  const cyComp = compH / 2 + t.y;
  const cx = rootRect.left + cxComp * s - containerRect.left;
  const cy = rootRect.top + cyComp * s - containerRect.top;
  return {
    cx,
    cy,
    w: Math.max(1, naturalW * t.scale * s),
    h: Math.max(1, naturalH * t.scale * s),
    rot: t.rotation,
    s,
  };
}

/** The 4 corners (TL, TR, BR, BL) of the rotated box, container-relative px. */
export function boxCorners(box: GizmoBox): Pt[] {
  const hw = box.w / 2;
  const hh = box.h / 2;
  const rad = (box.rot * Math.PI) / 180;
  const cos = Math.cos(rad);
  const sin = Math.sin(rad);
  const locals: Pt[] = [
    { x: -hw, y: -hh },
    { x: hw, y: -hh },
    { x: hw, y: hh },
    { x: -hw, y: hh },
  ];
  return locals.map((l) => ({
    x: box.cx + l.x * cos - l.y * sin,
    y: box.cy + l.x * sin + l.y * cos,
  }));
}

/** Rotate-handle position (above the top edge), container-relative px. */
export function rotateHandlePos(box: GizmoBox, offset = 26): Pt {
  const rad = (box.rot * Math.PI) / 180;
  const d = box.h / 2 + offset;
  return { x: box.cx + d * Math.sin(rad), y: box.cy - d * Math.cos(rad) };
}

/** Is the (container-relative) point inside the rotated box? `pad` in px. */
export function hitTest(box: GizmoBox, px: number, py: number, pad = 0): boolean {
  const rad = (-box.rot * Math.PI) / 180; // un-rotate the point
  const cos = Math.cos(rad);
  const sin = Math.sin(rad);
  const dx = px - box.cx;
  const dy = py - box.cy;
  const lx = dx * cos - dy * sin;
  const ly = dx * sin + dy * cos;
  return Math.abs(lx) <= box.w / 2 + pad && Math.abs(ly) <= box.h / 2 + pad;
}

// --- drag → transform deltas ----------------------------------------------

/** Move by a screen-px delta → new x/y (composition px). */
export function moveDelta(t0: GizmoTransform, dxScreen: number, dyScreen: number, s: number): Partial<GizmoTransform> {
  return { x: t0.x + dxScreen / s, y: t0.y + dyScreen / s };
}

/** Corner resize, center-anchored uniform: ratio of pointer radii from center. */
export function resizeDelta(t0: GizmoTransform, center: Pt, ptr0: Pt, ptr1: Pt): Partial<GizmoTransform> {
  const r0 = Math.hypot(ptr0.x - center.x, ptr0.y - center.y);
  if (r0 < 1) return {};
  const r1 = Math.hypot(ptr1.x - center.x, ptr1.y - center.y);
  return { scale: Math.max(0.05, Math.min(20, t0.scale * (r1 / r0))) };
}

/** Rotate by the change in pointer angle around center. Shift → 15° snap. */
export function rotateDelta(t0: GizmoTransform, center: Pt, ptr0: Pt, ptr1: Pt, snap = false): Partial<GizmoTransform> {
  const a0 = Math.atan2(ptr0.y - center.y, ptr0.x - center.x);
  const a1 = Math.atan2(ptr1.y - center.y, ptr1.x - center.x);
  let rot = t0.rotation + ((a1 - a0) * 180) / Math.PI;
  if (snap) rot = Math.round(rot / 15) * 15;
  // normalise to [-180, 180] for a stable inspector value
  rot = ((((rot + 180) % 360) + 360) % 360) - 180;
  return { rotation: Math.round(rot * 10) / 10 };
}
