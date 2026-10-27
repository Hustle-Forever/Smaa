import {
  Boxes,
  LayoutDashboard,
  Library as LibraryIcon,
  Paintbrush,
  Radio,
  Settings as SettingsIcon,
  Sparkles,
  TrendingUp,
} from "lucide-react";

import type { Tab } from "../App";
import { AccountFooter } from "./auth/AccountFooter";

interface NavItem {
  id: Tab;
  label: string;
  Icon: typeof LayoutDashboard;
  /** Small trailing tag, e.g. "beta". Rendered right-aligned. */
  badge?: string;
}

// The "Brand" entry is a tabbed workspace (Profile · Upload · Synthesizer ·
// Instagram); the old standalone "agents"/"upload"/"instagram" tabs now live
// inside it and only persist as deep-link aliases (see App.tsx + activeFor below).
//
// "Ideation" (id `producer`) is the hands-on studio; "Producer" (id `campaigns`)
// is the autonomous one. Two different jobs, so two entries.
export const NAV: NavItem[] = [
  { id: "dashboard", label: "Dashboard", Icon: LayoutDashboard },
  { id: "brand", label: "Brand", Icon: Sparkles },
  { id: "assets", label: "Assets", Icon: Boxes },
  { id: "producer", label: "Ideation", Icon: Paintbrush },
  { id: "campaigns", label: "Producer", Icon: Radio, badge: "beta" },
  // Trend finding feeds ideation, so it sits directly after it.
  { id: "trends", label: "SMAA intelligence", Icon: TrendingUp, badge: "beta" },
  { id: "library", label: "Library", Icon: LibraryIcon },
  { id: "settings", label: "Settings", Icon: SettingsIcon },
];

// Keep the Brand nav item lit when on any of its sub-routes / legacy aliases.
export function activeFor(id: Tab, tab: Tab): boolean {
  if (id === "brand")
    return tab === "brand" || tab === "upload" || tab === "agents" || tab === "instagram";
  return tab === id;
}

interface Props {
  tab: Tab;
  onTab: (t: Tab) => void;
}

export function Sidebar({ tab, onTab }: Props) {
  return (
    <aside className="hidden lg:flex w-[240px] shrink-0 flex-col border-r border-border bg-bg-subtle fixed inset-y-0 left-0 z-40">
      {/* Brand mark — "42 Studio" lockup + one-line product subtitle. */}
      <button
        onClick={() => onTab("dashboard")}
        className="flex items-center gap-2.5 px-5 py-5 border-b border-border text-left group"
        aria-label="Go to dashboard"
      >
        <span
          className={[
            "inline-flex w-7 h-7 shrink-0 items-center justify-center rounded",
            "bg-fg p-1.5",
            "transition-shadow duration-200 group-hover:shadow-glow",
          ].join(" ")}
        >
          {/* The real 42 mark, cropped out of the school's own white lockup in
              backend/data/branding. What stood here was the two characters "4"
              and "2" set in the UI font, which is not the logo and never was. */}
          <img src="/42-logo.png" alt="" className="h-full w-full object-contain" />
        </span>
        <span className="min-w-0 leading-tight">
          <span className="block text-[13px] font-semibold tracking-tight text-fg group-hover:text-accent transition-colors">
            42 Studio
          </span>
          <span className="block truncate text-[11.5px] font-semibold tracking-wide text-fg/70 mt-0.5">
            Creative Command Center
          </span>
        </span>
      </button>

      {/* Nav links */}
      <NavList tab={tab} onTab={onTab} />

      {/* Account area */}
      <AccountFooter />
    </aside>
  );
}

/**
 * The nav entries themselves, shared by the desktop rail and the mobile drawer.
 *
 * Extracted rather than copied: the drawer showing a different or stale set of
 * destinations than the rail is exactly the bug that a second copy produces the
 * first time someone adds a tab.
 */
export function NavList({
  tab,
  onTab,
  className = "flex-1 px-3 py-5 space-y-1 overflow-y-auto",
}: {
  tab: Tab;
  onTab: (t: Tab) => void;
  className?: string;
}) {
  return (
    <nav className={className} aria-label="Main">
      {NAV.map(({ id, label, Icon, badge }) => {
        const active = activeFor(id, tab);
        return (
          <button
            key={id}
            onClick={() => onTab(id)}
            aria-current={active ? "page" : undefined}
            className={["nav-item", active ? "active" : ""].join(" ")}
          >
            <Icon className="nav-icon" strokeWidth={1.8} />
            <span>{label}</span>
            {badge && (
              <span className="pill ml-auto bg-accent/10 text-accent border border-accent/20">
                {badge}
              </span>
            )}
          </button>
        );
      })}
    </nav>
  );
}
