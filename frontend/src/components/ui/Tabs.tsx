import { useRef, type KeyboardEvent, type ReactNode } from "react";

/**
 * Tabs, the real WAI-ARIA pattern.
 *
 * The app's other tab rows are plain buttons on purpose (a TabBar that
 * navigates is not a tablist). This one switches panels in place, so it takes
 * the whole contract: one tab stop for the row (roving tabindex), Left/Right
 * and Home/End move between tabs, the selected tab owns aria-selected and
 * names its panel with aria-controls, and the panel is labelled by its tab.
 *
 * Arrow keys select as they move (automatic activation). Every panel this is
 * used for renders instantly, and a tab that needs a second key press to show
 * what it points at is a tab a keyboard user learns to distrust.
 */
export interface TabDef<T extends string> {
  id: T;
  label: ReactNode;
  /** A small count after the label (e.g. how many patterns). */
  badge?: ReactNode;
}

export function tabIds(idBase: string, id: string) {
  return { tab: `${idBase}-tab-${id}`, panel: `${idBase}-panel-${id}` };
}

export function Tabs<T extends string>({
  label,
  tabs,
  value,
  onChange,
  idBase,
  className = "",
}: {
  /** Accessible name of the tab row. */
  label: string;
  tabs: TabDef<T>[];
  value: T;
  onChange: (id: T) => void;
  /** Prefix for the tab and panel ids (useId() is fine). */
  idBase: string;
  className?: string;
}) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});

  const onKey = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    let j: number | null = null;
    if (e.key === "ArrowRight") j = (i + 1) % tabs.length;
    else if (e.key === "ArrowLeft") j = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") j = 0;
    else if (e.key === "End") j = tabs.length - 1;
    if (j === null) return;
    e.preventDefault();
    const next = tabs[j];
    onChange(next.id);
    refs.current[next.id]?.focus();
  };

  return (
    <div role="tablist" aria-label={label} className={`flex items-end gap-1 border-b border-border ${className}`}>
      {tabs.map((t, i) => {
        const selected = t.id === value;
        const ids = tabIds(idBase, t.id);
        return (
          <button
            key={t.id}
            ref={(el) => {
              refs.current[t.id] = el;
            }}
            id={ids.tab}
            type="button"
            role="tab"
            aria-selected={selected}
            aria-controls={ids.panel}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(t.id)}
            onKeyDown={(e) => onKey(e, i)}
            className={`relative -mb-px inline-flex items-center gap-1.5 px-3 py-2 text-xs font-medium border-b-2 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 rounded-t ${
              selected ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg"
            }`}
          >
            {t.label}
            {t.badge != null && t.badge !== "" && (
              <span
                className={`min-w-[1.125rem] h-[1.125rem] px-1 inline-flex items-center justify-center rounded-full text-3xs font-semibold tabular-nums ${
                  selected ? "bg-accent text-white" : "bg-bg-muted text-muted"
                }`}
              >
                {t.badge}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/** The panel for one tab. Render only the active one, or all of them with
 *  `active` set so inactive panels are hidden (and out of the tab order). */
export function TabPanel({
  idBase,
  id,
  active = true,
  className = "",
  children,
}: {
  idBase: string;
  id: string;
  active?: boolean;
  className?: string;
  children: ReactNode;
}) {
  const ids = tabIds(idBase, id);
  return (
    <div role="tabpanel" id={ids.panel} aria-labelledby={ids.tab} hidden={!active} className={className}>
      {children}
    </div>
  );
}
