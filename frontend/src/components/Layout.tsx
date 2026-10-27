import { useCallback, useMemo, useRef, useState, type ReactNode } from "react";
import { Menu } from "lucide-react";

import type { Tab } from "../App";
import { ErrorBoundary } from "./ErrorBoundary";
import { MobileNav } from "./MobileNav";
import { MapBackdrop } from "./k42/MapBackdrop";
import { LayoutChromeProvider, type ChromeClaim } from "./LayoutChrome";
import { PageTransition } from "./PageTransition";
import { Sidebar } from "./Sidebar";

interface Props {
  tab: Tab;
  onTab: (t: Tab) => void;
  children: ReactNode;
  /**
   * "Keep-alive" surface: rendered alongside `children` but OUTSIDE the
   * key={tab} PageTransition, so it stays mounted across tab switches instead
   * of being remounted (and losing its state) on every navigation. The slot's
   * own content decides whether to show or hide itself per tab. Used by the
   * Producer page so in-progress generations + typed inputs survive tab
   * changes without touching any generation code.
   */
  persistent?: ReactNode;
  /** The tab the persistent surface shows on (it is hidden on every other). */
  persistentTab?: Tab;
}

const TAB_LABELS: Record<Tab, string> = {
  dashboard: "Dashboard",
  // agents/upload are legacy aliases that now render inside the Brand workspace.
  agents: "Brand",
  upload: "Brand",
  brand: "Brand",
  producer: "Ideation",
  campaigns: "Producer",
  trends: "SMAA intelligence",
  library: "Library",
  instagram: "Instagram",
  assets: "Assets",
  settings: "Settings",
};

export function Layout({ tab, onTab, children, persistent, persistentTab = "producer" }: Props) {
  // A mounted page (e.g. the Video Editor) can request full-bleed via
  // useFullBleed(), and the Rebrand stage the whole window (useHideSidebar()).
  // Each is a claim kept with the tab it holds on, and holds only there: a
  // page's claim, the tab open when it was made; the persistent surface's,
  // its own tab, whichever tab is open — it stays mounted, hidden, while
  // another tab is open, and a claim it makes then (a stage mounted again
  // after a refused Finalize) must not take that tab's sidebar.
  const [claims, setClaims] = useState<{ id: number; kind: ChromeClaim; tab: Tab }[]>([]);
  const tabRef = useRef(tab);
  tabRef.current = tab;
  const nextClaim = useRef(0);
  const claimOn = useCallback((on: Tab | null) => (kind: ChromeClaim) => {
    const id = ++nextClaim.current;
    setClaims((cs) => [...cs, { id, kind, tab: on ?? tabRef.current }]);
    return () => setClaims((cs) => cs.filter((c) => c.id !== id));
  }, []);
  const claim = useMemo(() => claimOn(null), [claimOn]);
  const claimPersistent = useMemo(() => claimOn(persistentTab), [claimOn, persistentTab]);
  const fullBleed = claims.some((c) => c.kind === "fullBleed" && c.tab === tab);
  const hideSidebar = claims.some((c) => c.kind === "hideSidebar" && c.tab === tab);
  const chrome = useMemo(() => ({ fullBleed, hideSidebar, claim }), [fullBleed, hideSidebar, claim]);
  const persistentChrome = useMemo(
    () => ({ fullBleed, hideSidebar, claim: claimPersistent }),
    [fullBleed, hideSidebar, claimPersistent],
  );
  const [navOpen, setNavOpen] = useState(false);
  // Stable identity: MobileNav's matchMedia effect depends on it, and an inline
  // arrow would re-subscribe the listener on every render of this layout.
  const closeNav = useCallback(() => setNavOpen(false), []);
  return (
    <div className="min-h-full">
      {!hideSidebar && <Sidebar tab={tab} onTab={onTab} />}

      {/* Mobile top bar. The sidebar is desktop-only, so this is where the
          eight destinations are reachable from below lg. */}
      <header className="lg:hidden sticky top-0 z-30 border-b border-white/[0.06] bg-k-rail text-white">
        <div className="px-4 h-12 flex items-center gap-3">
          <button
            onClick={() => setNavOpen(true)}
            className="-ml-1 w-10 h-10 shrink-0 rounded-lg flex items-center justify-center text-white hover:bg-white/[0.08] transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-k-cyan/70"
            aria-label="Open navigation"
            aria-expanded={navOpen}
          >
            <Menu className="w-5 h-5" strokeWidth={1.8} aria-hidden />
          </button>
          <button
            onClick={() => onTab("dashboard")}
            className="flex items-center gap-2 min-w-0"
            aria-label="Go to dashboard"
          >
            <img src="/42-logo.png" alt="42" className="h-6 w-auto object-contain shrink-0" />
            <span className="k-display text-[15px] text-white truncate">Studio</span>
          </button>
          <span className="ml-auto shrink-0 text-[12px] font-medium text-white/60">
            {TAB_LABELS[tab]}
          </span>
        </div>
      </header>

      {navOpen && <MobileNav tab={tab} onTab={onTab} onClose={closeNav} />}

      {/* A button, NOT <a href="#main">. App.tsx routes on the first hash
          segment, so an anchor would be read as an unknown tab and quietly
          navigate to the Dashboard -- a skip link that loses your page. */}
      <button
        type="button"
        onClick={() => document.getElementById("main-content")?.focus()}
        className="sr-only focus:not-sr-only focus:fixed focus:z-[110] focus:top-2 focus:left-2 btn btn-primary text-xs"
      >
        Skip to content
      </button>

      {/* The 42 city map, soft, behind every page. Not behind full-bleed
          tools (the Video Editor), which need a calm surface. */}
      {!fullBleed && <MapBackdrop />}

      {/* relative (no z-index): paints above the map without creating a
          stacking context, so modals still stack above the sidebar. */}
      <main id="main-content" tabIndex={-1} className={hideSidebar ? "relative outline-none" : "relative lg:pl-[256px] outline-none"}>
        <LayoutChromeProvider value={chrome}>
          {/* fullBleed: full main-area width (app-like surfaces e.g. Video
              Editor) with a small gutter; otherwise the centered content column. */}
          <div
            className={
              fullBleed
                ? "px-3 lg:px-4 py-2"
                : "max-w-7xl mx-auto px-6 md:px-10 py-8"
            }
          >
            {/* Keyed by tab so a page that throws is contained to that page --
                the sidebar survives, and navigating elsewhere remounts the
                boundary clean without the user having to do anything. */}
            <ErrorBoundary key={tab} label={TAB_LABELS[tab]}>
              <PageTransition tabKey={tab}>{children}</PageTransition>
            </ErrorBoundary>
            {/* Kept-alive surface — NOT inside PageTransition, so the key={tab}
                remount can't reset it. It self-hides when its tab is inactive,
                and its claims hold on its own tab only. */}
            <LayoutChromeProvider value={persistentChrome}>{persistent}</LayoutChromeProvider>
          </div>
        </LayoutChromeProvider>
      </main>
    </div>
  );
}
