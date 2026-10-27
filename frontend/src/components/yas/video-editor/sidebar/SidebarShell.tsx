/**
 * Video Editor · sidebar shell — the tool rail + the active tab's panel.
 * Replaces the old inline SourcesPanel. Tabs: Media (bin + add picker), Audio,
 * Text, Stickers, Shapes, Transitions, Effects. Clicking the active tab again
 * collapses to the rail. (The old B-roll preset tab was removed — intro/outro
 * and music are handled by the AI chat; B-roll clips are uploaded at creation.)
 */
import { useState } from "react";

import { AudioPanel } from "./AudioPanel";
import { EffectsPanel } from "./EffectsPanel";
import { FontsPanel } from "./FontsPanel";
import { MediaBinPanel } from "./MediaBinPanel";
import { ShapePanel } from "./ShapePanel";
import { StickerPanel } from "./StickerPanel";
import { TextPanel } from "./TextPanel";
import { SIDEBAR_TABS, ToolRail, type SidebarTab } from "./ToolRail";
import { TransitionPanel } from "./TransitionPanel";

export function SidebarShell() {
  const [tab, setTab] = useState<SidebarTab | null>("media");

  return (
    <div className="h-full flex rounded-xl border border-border bg-bg-subtle/40 overflow-hidden">
      <ToolRail tabs={SIDEBAR_TABS} active={tab} onSelect={(t) => setTab((cur) => (cur === t ? null : t))} />
      {tab && (
        <div className="w-[210px] shrink-0 border-l border-border">
          {tab === "media" && <MediaBinPanel />}
          {tab === "audio" && <AudioPanel />}
          {tab === "text" && <TextPanel />}
          {tab === "font" && <FontsPanel />}
          {tab === "sticker" && <StickerPanel />}
          {tab === "shape" && <ShapePanel />}
          {tab === "transition" && <TransitionPanel />}
          {tab === "effects" && <EffectsPanel />}
        </div>
      )}
    </div>
  );
}
