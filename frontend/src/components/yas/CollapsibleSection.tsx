/**
 * CollapsibleSection — tiny accordion primitive for the Human Element Video studio
 * composer. Header row shows the section label + a one-line summary; the
 * body is hidden by default and revealed on click. Used for "Look & Feel"
 * and "Engines" rows in the left composer so the panel stays short until
 * the user opts in.
 *
 * Caller controls expanded state — lets the parent open the section
 * automatically the first time a slot is filled.
 */
import { ReactNode } from "react";

interface Props {
  label: string;
  summary: string;
  expanded: boolean;
  onToggle: () => void;
  children: ReactNode;
  /** Optional inline accent (e.g. green dot) when the section has user-set state. */
  hasState?: boolean;
}

export function CollapsibleSection({
  label,
  summary,
  expanded,
  onToggle,
  children,
  hasState = false,
}: Props) {
  return (
    <div className="border-t border-border pt-3">
      <button
        type="button"
        onClick={onToggle}
        className="w-full flex items-center gap-2 text-left group"
        aria-expanded={expanded}
      >
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="eyebrow">{label}</span>
            {hasState && (
              <span
                className="w-1.5 h-1.5 rounded-full bg-accent shrink-0"
                aria-label="set"
              />
            )}
          </div>
          <p className="text-[11.5px] text-muted truncate mt-0.5">{summary}</p>
        </div>
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 20 20"
          className={[
            "w-4 h-4 text-muted shrink-0 transition-transform group-hover:text-fg",
            expanded ? "rotate-180" : "rotate-0",
          ].join(" ")}
          fill="currentColor"
          aria-hidden
        >
          <path
            fillRule="evenodd"
            d="M5.23 7.21a.75.75 0 011.06.02L10 11.06l3.71-3.83a.75.75 0 111.08 1.04l-4.24 4.38a.75.75 0 01-1.08 0L5.21 8.27a.75.75 0 01.02-1.06z"
            clipRule="evenodd"
          />
        </svg>
      </button>
      {expanded && <div className="pt-3 space-y-2.5">{children}</div>}
    </div>
  );
}
