// ---------------------------------------------------------------------------
// SegmentedControl — shared option-row selector used by the Yas Producer
// creators. Three visual variants matching existing inline implementations.
// ---------------------------------------------------------------------------

import type { ReactNode } from "react";

const VARIANTS = {
  pill: {
    wrapper: "inline-flex flex-wrap gap-2",
    btn: "px-3 py-1.5 rounded-full border text-[13px] transition-colors",
    active: "border-accent text-accent bg-accent/10",
    idle: "border-border text-fg/70 hover:border-fg/40",
  },
  compact: {
    wrapper: "inline-flex rounded-lg border border-border p-0.5 gap-0.5",
    btn: "px-2.5 py-1 rounded-md text-xs transition-colors",
    active: "bg-fg text-bg",
    idle: "text-fg/70 hover:bg-bg-muted",
  },
  tab: {
    wrapper: "inline-flex rounded-lg bg-bg-muted p-1 gap-1",
    btn: "px-3 py-1 rounded-md text-sm transition-colors",
    active: "bg-bg-subtle shadow-sm ring-1 ring-border text-fg",
    idle: "text-fg/60 hover:text-fg",
  },
} as const;

export function SegmentedControl<T extends string | number>({
  options,
  value,
  onChange,
  disabled,
  variant = "pill",
  className = "",
}: {
  options: { value: T; label: ReactNode; title?: string }[];
  value: T;
  onChange: (v: T) => void;
  disabled?: boolean;
  variant?: "pill" | "compact" | "tab";
  className?: string;
}) {
  const v = VARIANTS[variant];
  return (
    <div className={v.wrapper + " " + className}>
      {options.map((opt) => {
        const active = opt.value === value;
        return (
          <button
            key={String(opt.value)}
            type="button"
            onClick={() => onChange(opt.value)}
            disabled={disabled}
            title={opt.title}
            className={
              v.btn +
              " " +
              (active ? v.active : v.idle) +
              " disabled:opacity-50 disabled:cursor-not-allowed"
            }
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
