/**
 * Shimmer-bar based skeleton — replaces `bg-bg-muted animate-pulse` blocks.
 * Reads as "loading" more clearly than a flat pulse.
 */
interface Props {
  className?: string;
  /** Convenience: tailwind h-N w-N. Falls back to className. */
  h?: string;
  w?: string;
  rounded?: string;
}

export function Skeleton({ className, h, w, rounded = "rounded" }: Props) {
  return (
    <div
      className={[
        "bg-bg-muted overflow-hidden relative",
        rounded,
        h ?? "",
        w ?? "",
        className ?? "",
      ].join(" ")}
    >
      <span
        className="absolute inset-0 shimmer-bar opacity-60"
        aria-hidden
      />
    </div>
  );
}
