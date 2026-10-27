/**
 * The output-format control, drawn once.
 *
 * Create Poster, Create Poster + Human and Human Character all ask the same
 * question, and before this they answered it three different ways: two of them
 * with a copy of these twenty lines under two different headings ("Shape" and
 * "Format") with two different naming conventions ("Square" against "1:1"), and
 * the third not at all. The same choice has to look like the same choice.
 *
 * THE BUTTON SAYS THE RATIO. "Square", "Story" and "Wide" were nicknames for
 * numbers, and the number is what the model is actually given. The line
 * underneath says what that shape is for, which is the part a reader needs and
 * the part a nickname was pretending to carry.
 *
 * The options come from `lib/aspect.ts`, beside the Tailwind classes that
 * render them, so a surface cannot offer a shape it has no cell for.
 */
import type { FormatOption } from "../../lib/aspect";

type Props<K extends string> = {
  options: FormatOption<K>[];
  value: K;
  onChange: (key: K) => void;
  /** "Format" everywhere. Overridable, but think twice: two headings for one
   *  control is how this drifted apart in the first place. */
  label?: string;
  disabled?: boolean;
};

export function FormatPicker<K extends string>({
  options,
  value,
  onChange,
  label = "Format",
  disabled,
}: Props<K>) {
  const chosen = options.find((o) => o.key === value) ?? options[0];
  return (
    <div>
      <p className="eyebrow mb-1.5">{label}</p>
      <div className="flex items-center gap-1 p-1 rounded-lg bg-bg-muted w-fit flex-wrap">
        {options.map((o) => (
          <button
            key={o.key}
            type="button"
            onClick={() => onChange(o.key)}
            disabled={disabled}
            title={o.hint}
            aria-pressed={value === o.key}
            className={[
              "px-3 py-1 rounded-md font-mono text-xs font-medium tabular-nums transition-all",
              "disabled:cursor-not-allowed disabled:opacity-50",
              value === o.key ? "bg-bg text-fg shadow-sm" : "text-muted hover:text-fg",
            ].join(" ")}
          >
            {o.label}
          </button>
        ))}
      </div>
      <p className="text-3xs text-muted mt-1.5 leading-snug">{chosen?.hint}</p>
    </div>
  );
}
