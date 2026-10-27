/**
 * Layout chrome context — lets a deeply-nested page request a "full-bleed"
 * layout (no centered max-width container / page padding) from the app shell.
 *
 * The app's `Layout` wraps every page in a centered `max-w-7xl … px-… py-…`
 * container. App-like surfaces (the Video Editor) want the full main-area width
 * instead. They call `useFullBleed()` while mounted; `Layout` reads `fullBleed`
 * and drops the constraining wrapper.
 *
 * A workspace that needs the whole window can also tuck the app sidebar away
 * while mounted (`useHideSidebar()`). Nothing does today: the Rebrand stage
 * used to, and the owner wants the sidebar kept there.
 *
 * Each request is a claim that holds while its component is mounted, and only
 * on the tab it was made on — or, made from the kept-alive Producer page, on
 * the Producer tab whenever it is made: that page stays mounted, hidden, when
 * another tab is opened, and that tab keeps its own layout and sidebar. Claims
 * are counted, so one surface letting go (a hidden creator unmounting) never
 * takes the layout from another that still holds it.
 */
import { createContext, useContext, useEffect } from "react";

export type ChromeClaim = "fullBleed" | "hideSidebar";

interface LayoutChrome {
  fullBleed: boolean;
  hideSidebar: boolean;
  /** Hold `kind` on the current tab; call the returned function to let go. */
  claim: (kind: ChromeClaim) => () => void;
}

const LayoutChromeContext = createContext<LayoutChrome>({
  fullBleed: false,
  hideSidebar: false,
  claim: () => () => {},
});

export const LayoutChromeProvider = LayoutChromeContext.Provider;

export function useLayoutChrome(): LayoutChrome {
  return useContext(LayoutChromeContext);
}

function useClaim(kind: ChromeClaim): void {
  const { claim } = useContext(LayoutChromeContext);
  useEffect(() => claim(kind), [claim, kind]);
}

/** Request full-bleed layout for as long as the calling component is mounted. */
export function useFullBleed(): void {
  useClaim("fullBleed");
}

/** Tuck the app sidebar away (and its left gutter) for as long as the
 *  calling component is mounted. */
export function useHideSidebar(): void {
  useClaim("hideSidebar");
}
