/**
 * Video Editor · tool rail — the vertical icon column that switches sidebar tabs.
 */
export type SidebarTab =
  | "ai"
  | "media"
  | "audio"
  | "text"
  | "font"
  | "sticker"
  | "shape"
  | "transition"
  | "effects";

export const SIDEBAR_TABS: { key: SidebarTab; icon: string; label: string }[] = [
  // AI assistant moved to the right sidebar (Chat tab) — more room there.
  { key: "media", icon: "🎬", label: "Media" },
  { key: "audio", icon: "♪", label: "Audio" },
  { key: "text", icon: "T", label: "Text" },
  { key: "font", icon: "Aa", label: "Fonts" },
  { key: "sticker", icon: "✦", label: "Stickers" },
  { key: "shape", icon: "◻", label: "Shapes" },
  { key: "transition", icon: "⇄", label: "Transitions" },
  { key: "effects", icon: "☀", label: "Effects" },
];

export function ToolRail({
  active,
  onSelect,
  tabs = SIDEBAR_TABS,
}: {
  active: SidebarTab | null;
  onSelect: (tab: SidebarTab) => void;
  /** Override the rail's tab list (e.g. the B-roll preset injects a leading tab). */
  tabs?: typeof SIDEBAR_TABS;
}) {
  return (
    <div className="w-[54px] shrink-0 border-r border-border bg-bg-subtle/60 flex flex-col py-1 gap-0.5 overflow-y-auto">
      {tabs.map((t) => (
        <button
          key={t.key}
          onClick={() => onSelect(t.key)}
          title={t.label}
          className={[
            "flex flex-col items-center gap-0.5 py-1.5 mx-1 rounded-md transition-colors",
            active === t.key ? "bg-accent/12 text-accent" : "text-muted hover:bg-bg-muted hover:text-fg",
          ].join(" ")}
        >
          <span className="text-[15px] leading-none">{t.icon}</span>
          <span className="text-[8.5px] leading-none">{t.label}</span>
        </button>
      ))}
    </div>
  );
}
