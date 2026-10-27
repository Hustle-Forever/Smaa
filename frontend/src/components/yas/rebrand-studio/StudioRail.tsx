// Rebrand studio — the right rail: tabs from the registry (studioModel
// RAIL_TABS; a tab shows only when the server can draw its kinds, so there
// is no "coming soon"), or a view pushed over them (the pattern library,
// what Omni is told). The rail body is what scrolls. While the draft is built
// on a version, its bar (Add to it | Change it) sits on top of the tabs.

import { useId, type ReactNode } from "react";

import { TabPanel, Tabs } from "../../ui/Tabs";
import { COPY } from "./studioCopy";
import type { RailTabId } from "./studioModel";

export function StudioRail({
  tabs,
  tab,
  onTab,
  panel,
  push,
  bar = null,
  className = "",
}: {
  tabs: { id: RailTabId; label: string; badge?: number }[];
  tab: RailTabId;
  onTab: (id: RailTabId) => void;
  /** The active tab's content. */
  panel: ReactNode;
  /** A view that replaces the tabs until it is closed. */
  push: ReactNode | null;
  /** Above the tabs (BuildOnBar). */
  bar?: ReactNode;
  className?: string;
}) {
  const idBase = useId();
  return (
    <aside aria-label={COPY.railLabel} className={`flex min-h-0 flex-col bg-bg ${className}`}>
      {push}
      {/* Kept mounted under a pushed view, so what opened it (and any open
          disclosure) is still there to go back to. */}
      <div className={push ? "hidden" : "flex min-h-0 flex-1 flex-col"}>
        {bar}
        <Tabs
          label={COPY.railLabel}
          idBase={idBase}
          tabs={tabs.map((t) => ({ id: t.id, label: t.label, badge: t.badge ? t.badge : undefined }))}
          value={tab}
          onChange={onTab}
          className="shrink-0 px-2"
        />
        <TabPanel idBase={idBase} id={tab} className="min-h-0 flex-1 overflow-y-auto p-3">
          {panel}
        </TabPanel>
      </div>
    </aside>
  );
}
