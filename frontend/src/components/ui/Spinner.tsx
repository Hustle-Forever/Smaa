/**
 * The busy spinner.
 *
 * The ring idiom -- a bordered circle with one transparent edge, spun -- was
 * hand-written 25 times across 11 files, in four sizes and two colours, with no
 * two call sites agreeing on which. This is that idiom, once.
 *
 * Sizes are complete class literals in a map rather than an interpolated
 * `w-${n}`, because Tailwind's scanner only sees literals and a built string
 * compiles to nothing.
 */
const SIZE = {
  xs: "w-2.5 h-2.5 border",
  sm: "w-3.5 h-3.5 border-2",
  md: "w-4 h-4 border-2",
  lg: "w-6 h-6 border-2",
} as const;

const TONE = {
  /** Inherits the parent's text colour -- the right default inside a button. */
  current: "border-current border-t-transparent",
  accent: "border-accent border-t-transparent",
} as const;

export function Spinner({
  size = "md",
  tone = "current",
  className = "",
  label,
}: {
  size?: keyof typeof SIZE;
  tone?: keyof typeof TONE;
  className?: string;
  /**
   * Announce the wait. Omit it when a visible label already says what is
   * happening ("Generating 4 variants…"), which is the common case -- a second
   * announcement of the same fact is noise.
   */
  label?: string;
}) {
  return (
    <span
      className={`inline-block rounded-full animate-spin ${SIZE[size]} ${TONE[tone]} ${className}`}
      role={label ? "status" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    />
  );
}
