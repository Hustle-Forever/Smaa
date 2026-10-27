/**
 * Video Editor · right sidebar — tabbed [AI Chat | Properties].
 *
 * The AI assistant (chat + tools) lives here now (more room than the left rail),
 * alongside the clip Properties inspector. Both panels are chromeless; this
 * container owns the rounded border + the tab header.
 */
import { useState } from "react";

import { ClipInspector } from "./ClipInspector";
import { AiPanel } from "./sidebar/AiPanel";

type RightTab = "chat" | "props";

export function RightSidebar() {
  // Settings first: the editor opens on the clip's properties; Ask AI is one click.
  const [tab, setTab] = useState<RightTab>("props");
  return (
    <div className="h-full flex flex-col rounded-xl border border-border bg-bg-subtle/40 overflow-hidden">
      <div className="shrink-0 flex border-b border-border">
        {(
          [
            { key: "chat" as const, label: "✨ Ask AI" },
            { key: "props" as const, label: "Settings" },
          ]
        ).map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={[
              "flex-1 text-2xs font-medium py-2 transition-colors",
              tab === t.key
                ? "text-fg border-b-2 border-accent -mb-px"
                : "text-muted hover:text-fg border-b-2 border-transparent",
            ].join(" ")}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="flex-1 min-h-0">
        {tab === "chat" ? <AiPanel /> : <ClipInspector />}
      </div>
    </div>
  );
}
