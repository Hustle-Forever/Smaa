import { useCallback, useEffect, useRef, useState } from "react";

/** How long the accent ring pulses around a freshly opened panel. */
const FLASH_MS = 1600;

/**
 * Reveal a panel that opens somewhere the user is not looking.
 *
 * A panel rendered below the fold opens silently: the click looks like it did
 * nothing, and the user has to go and find the new input themselves. Call
 * `reveal()` in the same handler that opens the panel and three things happen
 * once React has committed it to the DOM:
 *
 *   1. the panel scrolls into the middle of the viewport,
 *   2. its input takes focus, so the caret is already where they must type,
 *   3. an accent ring pulses around it twice.
 *
 * Two details are load-bearing:
 *
 *   - Focus is taken with `preventScroll`, because focusing an offscreen
 *     element jumps the page to it instantly, which would cancel the smooth
 *     scroll started on the line above.
 *   - The pulse runs through `Element.animate` rather than a CSS class. A
 *     class toggled by state will not replay if it is already on, so opening
 *     the panel again within FLASH_MS (clicking Refine on a second card) would
 *     flash nothing. Every `animate()` call starts fresh.
 *
 * Under `prefers-reduced-motion` the scroll is instant and the pulse is skipped.
 *
 * Usage:
 *   const refine = useRevealPanel<HTMLDivElement, HTMLTextAreaElement>();
 *   <button onClick={() => { setTargetId(id); refine.reveal(); }}>Refine</button>
 *   {target && (
 *     <div ref={refine.panelRef}>
 *       <textarea ref={refine.focusRef} />
 *     </div>
 *   )}
 */
export function useRevealPanel<
  P extends HTMLElement,
  F extends HTMLElement,
>() {
  const panelRef = useRef<P | null>(null);
  const focusRef = useRef<F | null>(null);

  // A counter rather than a boolean: asking for the panel again while it is
  // already open (Refine on a second card) has to re-run the effect below, and
  // `true` -> `true` would not.
  const [nudge, setNudge] = useState(0);
  const reveal = useCallback(() => setNudge((n) => n + 1), []);

  useEffect(() => {
    if (nudge === 0) return; // first render; nobody has asked for anything yet
    const panel = panelRef.current;
    if (!panel) return; // opener did not render a panel; nothing to reveal

    const still =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    panel.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "center" });
    focusRef.current?.focus({ preventScroll: true });
    if (still || typeof panel.animate !== "function") return;

    // accent teal, matching `accent.DEFAULT` in tailwind.config.ts
    const ring = (spread: string, alpha: number) => `0 0 0 ${spread} rgba(6,182,162,${alpha})`;
    panel.animate(
      [
        { boxShadow: ring("0px", 0) },
        { boxShadow: ring("6px", 0.32), offset: 0.12 },
        { boxShadow: ring("0px", 0), offset: 0.46 },
        { boxShadow: ring("6px", 0.32), offset: 0.58 },
        { boxShadow: ring("0px", 0) },
      ],
      { duration: FLASH_MS, easing: "ease-out" },
    );
  }, [nudge]);

  return { panelRef, focusRef, reveal };
}
