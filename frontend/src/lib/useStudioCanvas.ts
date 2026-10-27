import { useEffect } from "react";

// How many mounted, visible pages currently want each body class. A counter
// rather than add/remove per page: the Ideation studio stays mounted (hidden)
// while other tabs are open, so two pages can hold the same class at once
// during a tab switch, and one page's cleanup must not strip the other's.
const holders = new Map<string, number>();

/** Adds `name` to <body> while `active`, shared safely between pages. */
export function useBodyClass(name: string, active = true): void {
  useEffect(() => {
    if (!active) return;
    holders.set(name, (holders.get(name) ?? 0) + 1);
    document.body.classList.add(name);
    return () => {
      const n = Math.max(0, (holders.get(name) ?? 1) - 1);
      holders.set(name, n);
      if (n === 0) document.body.classList.remove(name);
    };
  }, [name, active]);
}

/**
 * The quiet neutral ground of the first redesign (Assets, Brand, Ideation).
 * A body class rather than a wrapper element, because the canvas is fixed to
 * the viewport and page transitions leave a transform on the page wrapper.
 * `active` lets a page that stays mounted while hidden hold it only while
 * it is the visible tab.
 */
export function useStudioCanvas(active = true): void {
  useBodyClass("studio-canvas", active);
}

/** The 42 brand ground (42 Dark / 42 Light, see lib/theme.ts). */
export function useKCanvas(active = true): void {
  useBodyClass("k-canvas", active);
}
