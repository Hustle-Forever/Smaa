import { useCallback, useMemo, useRef, useState, type ReactNode } from "react";
import { Menu } from "lucide-react";

import type { Tab } from "../App";
import { ErrorBoundary } from "./ErrorBoundary";
import { MobileNav } from "./MobileNav";
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
      <header className="lg:hidden sticky top-0 z-30 border-b border-border bg-bg-subtle/95 backdrop-blur supports-[backdrop-filter]:bg-bg-subtle/80">
        <div className="px-4 h-12 flex items-center gap-3">
          <button
            onClick={() => setNavOpen(true)}
            className="-ml-1 w-9 h-9 shrink-0 rounded flex items-center justify-center text-fg hover:bg-bg-muted transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
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
            <span className="inline-flex w-6 h-6 items-center justify-center rounded bg-fg text-white text-3xs font-bold leading-none">
              42
            </span>
            <span className="font-semibold tracking-tight text-fg truncate">Creative Command Center</span>
          </button>
          <span className="ml-auto shrink-0 font-mono text-2xs text-muted">
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

      <main id="main-content" tabIndex={-1} className={hideSidebar ? "outline-none" : "lg:pl-[240px] outline-none"}>
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
