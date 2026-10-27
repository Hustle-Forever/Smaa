import { useRef, type KeyboardEvent, type ReactNode } from "react";

/**
 * A row of chips where exactly one is chosen: a real radio group.
 *
 * The chips this replaces declared role="radio" and then behaved like a row
 * of buttons, so a keyboard user tabbed through every option. A radio group
 * is ONE tab stop (the chosen chip, or the first when none is), and the arrow
 * keys move the choice, which is what the role promises a screen reader.
 *
 * Arrows choose as they move, like native radios. Disabled chips are skipped.
 * A value that matches no chip (say, a colour picked with the colour input
 * next to the row) leaves every chip unchecked, and the first stays reachable.
 *
 * variant="swatch" draws each option as a round colour swatch (its `icon`)
 * with no text: the name goes in `ariaLabel` and `title`.
 */
export interface ChoiceOption<T extends string> {
  value: T;
  label: ReactNode;
  /** Accessible name when the label is an icon or a swatch. */
  ariaLabel?: string;
  title?: string;
  /** Drawn before the label (a colour dot, an icon). */
  icon?: ReactNode;
  disabled?: boolean;
}

const SIZE = {
  sm: "px-2.5 py-1 text-2xs gap-1.5 min-h-[28px]",
  md: "px-3 py-1.5 text-xs gap-1.5 min-h-[32px]",
} as const;

export function ChoiceChips<T extends string>({
  label,
  options,
  value,
  onChange,
  size = "sm",
  variant = "chip",
  disabled = false,
  className = "",
}: {
  /** Accessible name of the group. */
  label: string;
  options: ChoiceOption<T>[];
  value: T | null | undefined;
  onChange: (v: T) => void;
  size?: keyof typeof SIZE;
  variant?: "chip" | "swatch";
  disabled?: boolean;
  className?: string;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const usable = (i: number) => !disabled && !options[i]?.disabled;
  const checkedIndex = options.findIndex((o) => o.value === value);
  const firstUsable = options.findIndex((_, i) => usable(i));
  const stop = checkedIndex >= 0 && usable(checkedIndex) ? checkedIndex : firstUsable;

  const move = (from: number, step: number) => {
    for (let n = 1; n <= options.length; n++) {
      const j = (from + step * n + options.length * n) % options.length;
      if (usable(j)) return j;
    }
    return from;
  };

  const onKey = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    let j: number | null = null;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") j = move(i, 1);
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") j = move(i, -1);
    else if (e.key === "Home") j = firstUsable;
    else if (e.key === "End") j = move(0, -1);
    if (j === null || j < 0) return;
    e.preventDefault();
    refs.current[j]?.focus();
    if (options[j].value !== value) onChange(options[j].value);
  };

  return (
    <div
      role="radiogroup"
      aria-label={label}
      aria-disabled={disabled || undefined}
      className={`flex flex-wrap ${variant === "swatch" ? "gap-2" : "gap-1"} ${className}`}
    >
      {options.map((o, i) => {
        const checked = o.value === value;
        return (
          <button
            key={o.value}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role="radio"
            aria-checked={checked}
            aria-label={o.ariaLabel}
            title={o.title}
            disabled={!usable(i)}
            tabIndex={i === stop ? 0 : -1}
            onClick={() => !checked && onChange(o.value)}
            onKeyDown={(e) => onKey(e, i)}
            className={
              variant === "swatch"
                ? `relative inline-flex h-7 w-7 items-center justify-center rounded-full transition-shadow focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 focus-visible:ring-offset-2 disabled:opacity-40 disabled:cursor-not-allowed ${
                    checked ? "ring-2 ring-accent ring-offset-2" : "hover:ring-2 hover:ring-border-strong hover:ring-offset-1"
                  }`
                : `inline-flex items-center rounded-full border transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:opacity-40 disabled:cursor-not-allowed ${SIZE[size]} ${
                    checked
                      ? "border-accent bg-accent-fade text-accent-hover font-medium"
                      : "border-border text-muted hover:text-fg hover:border-border-strong"
                  }`
            }
          >
            {o.icon}
            {variant === "swatch" ? null : o.label}
          </button>
        );
      })}
    </div>
  );
}
