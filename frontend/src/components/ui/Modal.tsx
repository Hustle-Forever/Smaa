import { useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

import { useModalChrome } from "../../lib/useModalChrome";

/**
 * The dialog shell.
 *
 * Six files carried byte-identical copies of the same four class strings --
 * panel, header, footer, close button -- and twenty files hand-rolled their own
 * overlay around them. Those overlays did not agree: six different scrim
 * opacities between 0.5 and 0.78, five z-index bands from 50 to 120, and two
 * different mechanisms for expressing it (inline style objects in some files,
 * Tailwind classes in others). The dark scrims are fossils: rgba(0,0,0,0.78)
 * was tuned for a dark app, and this app is white.
 *
 * One scrim and one z-index, here.
 *
 * createPortal is REQUIRED and is not a style choice. Page transitions apply a
 * transform to the content column, and a transformed ancestor becomes the
 * containing block for anything positioned inside it -- so a fixed-position
 * modal rendered in place is laid out against the column instead of the
 * viewport, and lands visibly offset. RunPanel is the one documented exemption,
 * because App.tsx already renders it outside that transform.
 *
 * Focus trap, Escape, scroll lock and focus restore all come from
 * useModalChrome, so every dialog that adopts this gets the behaviour the two
 * good dialogs already had.
 */
const SIZE = {
  sm: "max-w-md",
  md: "max-w-lg",
  lg: "max-w-2xl",
  xl: "max-w-3xl",
  full: "max-w-5xl",
  /** A workspace (the Rebrand studio): a sheet. Full screen on a phone; on
   *  anything wider, 1320 px and as tall as the screen allows (880 px). Its
   *  own panel and overlay classes are below (`sheet`), because it drops the
   *  card's radius, border and height cap rather than adding to them. */
  studio: "max-w-[1320px]",
} as const;

export function Modal({
  onClose,
  title,
  children,
  footer,
  size = "lg",
  panelClassName = "",
  bodyClassName = "",
  closeOnBackdrop = true,
  headerActions,
}: {
  onClose: () => void;
  /** Rendered as the dialog's accessible name. */
  title: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  size?: keyof typeof SIZE;
  panelClassName?: string;
  bodyClassName?: string;
  closeOnBackdrop?: boolean;
  /** Controls in the header, before the close button (the studio's undo,
   *  redo and menu). */
  headerActions?: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const { trapTab } = useModalChrome({
    onClose,
    panelRef,
    initialFocusRef: closeRef,
  });

  // Every other size renders the exact class strings it always did.
  const sheet = size === "studio";

  return createPortal(
    <div
      className={
        sheet
          ? "fixed inset-0 z-[100] flex items-center justify-center p-0 sm:p-4 bg-black/60 backdrop-blur-sm"
          : "fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
      }
      onClick={closeOnBackdrop ? onClose : undefined}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : undefined}
        tabIndex={-1}
        onKeyDown={trapTab}
        // The panel swallows the click so the backdrop handler above only ever
        // fires for the backdrop itself.
        onClick={(e) => e.stopPropagation()}
        className={(sheet
          ? [
              "relative w-full flex flex-col overflow-hidden outline-none",
              "bg-bg shadow-lift",
              "h-[100dvh] rounded-none border-0 sm:h-[min(94vh,880px)] sm:rounded-2xl sm:border sm:border-border",
              SIZE[size],
              panelClassName,
            ]
          : [
              "relative w-full flex flex-col overflow-hidden outline-none",
              "bg-bg rounded-2xl border border-border shadow-lift",
              "max-h-[min(840px,92vh)]",
              SIZE[size],
              panelClassName,
            ]
        ).join(" ")}
      >
        <header className="shrink-0 px-4 py-3 border-b border-border flex items-center gap-2.5 bg-bg">
          <h2 className={sheet ? "text-sm font-semibold text-fg flex-1 min-w-0 truncate" : "text-sm font-semibold text-fg flex-1 min-w-0"}>
            {title}
          </h2>
          {headerActions}
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="w-7 h-7 shrink-0 rounded-full bg-bg-muted hover:bg-border text-fg flex items-center justify-center transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
          >
            {/* Lucide, not the "×" character the old copies used: a glyph is
                font-dependent and cannot be sized or themed like the rest. */}
            <X className="w-3.5 h-3.5" strokeWidth={2.5} aria-hidden />
          </button>
        </header>

        <div className={`min-h-0 flex-1 overflow-y-auto ${bodyClassName}`}>
          {children}
        </div>

        {footer && (
          <footer className="shrink-0 px-4 py-3 border-t border-border bg-bg-muted/50 flex items-center gap-3">
            {footer}
          </footer>
        )}
      </div>
    </div>,
    document.body,
  );
}
