// Rebrand studio — the footer: one line of status and the one primary
// action. An error is a notice above the line, in full, never truncated.
// When the draft would make a version that already exists, the line offers
// that version instead ("That's v2 — open it"): seeing it is free. While the
// icons are being placed, the line says so and Generate waits (a plan that
// couldn't be had says so, with what to do); a plan that moved on since it
// was shown comes back as a notice (the fresh plan is on the stage).

import { Notice } from "../../ui/controls";
import { Spinner } from "../../ui/Spinner";
import { COPY } from "./studioCopy";

export function GenerateBar({
  nextNo,
  status,
  offer = null,
  disabled,
  disabledTitle,
  posting,
  error,
  onDismissError,
  onGenerate,
  notice = null,
  working = false,
  statusAction = null,
}: {
  nextNo: number;
  status: string;
  /** In place of the status: a version that already shows this draft. */
  offer?: { no: number; onOpen: () => void } | null;
  disabled: boolean;
  disabledTitle?: string;
  posting: boolean;
  error: string | null;
  onDismissError: () => void;
  onGenerate: () => void;
  /** Worth a look before generating ("The icons changed since you looked"). */
  notice?: { text: string; onDismiss: () => void } | null;
  /** The status line is something being worked out ("Placing icons…"). */
  working?: boolean;
  /** What to do about the status ("Couldn't place the icons — … Try again"). */
  statusAction?: { label: string; onClick: () => void } | null;
}) {
  return (
    <div className="flex w-full min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
      {error && (
        <Notice tone="danger" className="basis-full" onDismiss={onDismissError}>
          {COPY.generateFailed} {error}
        </Notice>
      )}
      {notice && !error && (
        <Notice tone="warning" className="basis-full" onDismiss={notice.onDismiss}>
          {notice.text}
        </Notice>
      )}
      <p className="flex min-w-0 flex-1 items-center gap-1.5 text-2xs leading-snug text-muted" aria-live="polite">
        {working && !offer && <Spinner size="xs" tone="accent" />}
        {offer ? (
          <>
            {COPY.sameAs(offer.no)}{" "}
            <button
              type="button"
              onClick={offer.onOpen}
              title={COPY.openItTitle(offer.no)}
              className="rounded font-semibold text-accent-hover underline-offset-2 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
            >
              {COPY.openIt}
            </button>
          </>
        ) : (
          <>
            <span className="min-w-0">{status}</span>
            {statusAction && (
              <button
                type="button"
                onClick={statusAction.onClick}
                className="shrink-0 rounded font-semibold text-accent-hover underline-offset-2 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
              >
                {statusAction.label}
              </button>
            )}
          </>
        )}
      </p>
      <button
        type="button"
        className="btn btn-accent btn-sm shrink-0"
        disabled={disabled || posting}
        title={disabled ? disabledTitle : COPY.generateShortcut}
        aria-keyshortcuts="Control+Enter Meta+Enter"
        onClick={onGenerate}
      >
        {posting && <Spinner size="xs" />}
        {COPY.generate(nextNo)}
        <kbd className="hidden sm:inline rounded border border-white/30 px-1 font-mono text-3xs font-normal text-white/80">
          {COPY.generateShortcut}
        </kbd>
      </button>
    </div>
  );
}
