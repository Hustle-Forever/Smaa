// ---------------------------------------------------------------------------
// EnhanceButton — shared "✦ Enhance" button used by the Yas Producer creators.
// Canonical styling matches the existing Enhance buttons pixel-for-pixel.
// ---------------------------------------------------------------------------

type IconProps = { className?: string };

function SparkleIcon({ className = "w-4 h-4" }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3z"/>
      <path d="M19 14l.8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8L19 14z"/>
    </svg>
  );
}

const CANONICAL =
  "absolute bottom-2 right-2 inline-flex items-center gap-1 rounded-md border border-border bg-bg px-2 py-1 text-2xs font-medium text-fg hover:bg-bg-muted disabled:opacity-50 disabled:cursor-not-allowed transition-colors";

export function EnhanceButton({
  onClick,
  disabled,
  busy,
  label = "Enhance",
  busyLabel = "Enhancing…",
  title,
  className = "",
}: {
  onClick: () => void;
  disabled?: boolean;
  busy?: boolean;
  label?: string;
  busyLabel?: string;
  title?: string;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || busy}
      title={title}
      className={CANONICAL + " " + className}
    >
      {busy ? (
        <>
          <span className="inline-block w-3 h-3 border-2 border-current border-t-transparent rounded-full animate-spin" />
          {busyLabel}
        </>
      ) : (
        <>
          <SparkleIcon className="w-3.5 h-3.5" />
          {label}
        </>
      )}
    </button>
  );
}
