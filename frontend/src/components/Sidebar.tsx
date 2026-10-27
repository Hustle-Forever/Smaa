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
    // 42 charcoal rail in both themes, after the 42abudhabi.ae header: the
    // white 42 mark, a thin cyan rule, white nav text, cyan for "you are here".
    <aside className="hidden lg:flex w-[256px] shrink-0 flex-col fixed inset-y-0 left-0 z-40 bg-k-rail text-white border-r border-white/[0.06]">
      <button
        onClick={() => onTab("dashboard")}
        className="mx-4 mt-5 mb-3 pt-3 border-t-2 border-k-cyan text-left group focus:outline-none focus-visible:ring-2 focus-visible:ring-k-cyan/70 rounded-sm"
        aria-label="Go to dashboard"
      >
        <span className="flex items-end gap-3 px-1">
          {/* The real 42 mark, cropped out of the school's own white lockup in
              backend/data/branding. */}
          <img src="/42-logo.png" alt="42" className="h-9 w-auto object-contain" />
          <span className="pb-0.5 leading-none">
            <span className="block k-display text-[18px] tracking-[0.02em] text-white">Studio</span>
            <span className="block mt-1 text-[11px] font-medium tracking-[0.06em] uppercase text-white/55">Abu Dhabi</span>
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
  className = "flex-1 px-3 py-3 space-y-0.5 overflow-y-auto",
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
            <Icon className="nav-icon" strokeWidth={1.8} aria-hidden />
            <span className="truncate whitespace-nowrap">{label}</span>
            {badge && (
              <span
                className={[
                  "ml-auto shrink-0 px-1 py-[3px] text-[9px] font-bold leading-none uppercase tracking-[0.06em]",
                  active ? "text-k-cyan ring-1 ring-k-cyan/50" : "text-white/55 ring-1 ring-white/20",
                ].join(" ")}
              >
                {badge}
              </span>
            )}
          </button>
        );
      })}
    </nav>
  );
}
