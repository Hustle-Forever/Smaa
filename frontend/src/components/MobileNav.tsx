import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

import type { Tab } from "../App";
import { useModalChrome } from "../lib/useModalChrome";
import { NavList } from "./Sidebar";
import { AccountFooter } from "./auth/AccountFooter";

/**
 * Navigation below the lg breakpoint.
 *
 * Until this existed there was none. Sidebar is `hidden lg:flex` and the mobile
 * top bar held a logo and a read-only label, so under 1024px a user could reach
 * the Dashboard and nothing else -- seven of the eight destinations were simply
 * unreachable. That was the only defect in the whole UI audit where a feature
 * could not be got to at all, as opposed to being awkward once you were there.
 *
 * A drawer rather than a bottom tab bar: there are eight destinations and two
 * of them are called "Ideation" and "SMAA intelligence", which do not survive
 * being squeezed into a five-slot bar. The drawer shows all eight at full
 * width, with the same labels and the same order as the desktop rail, because
 * it renders the same NavList.
 *
 * Uses useModalChrome, so it gets the focus trap, Escape, scroll lock and
 * focus-restore that the good dialogs have. Portalled for the same reason every
 * other overlay here is: a transformed ancestor would become its containing
 * block and lay it out against the content column instead of the viewport.
 */
export function MobileNav({
  tab,
  onTab,
  onClose,
}: {
  tab: Tab;
  onTab: (t: Tab) => void;
  onClose: () => void;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const { trapTab } = useModalChrome({
    onClose,
    panelRef,
    initialFocusRef: closeRef,
  });

  /**
   * Close when the viewport grows past the breakpoint.
   *
   * Without this, opening the drawer on a narrow window and then widening it
   * (or rotating a tablet) leaves the drawer "open" in state while Tailwind
   * hides the desktop rail's counterpart -- and because the scroll lock is
   * released on unmount, the page would stay unscrollable behind a panel nobody
   * can see. Closing on the breakpoint change unmounts it properly and hands
   * the scroll back.
   */
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1024px)");
    const onChange = (e: MediaQueryListEvent) => {
      if (e.matches) onClose();
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [onClose]);

  return createPortal(
    <div
      className="lg:hidden fixed inset-0 z-[100] bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label="Navigation"
        tabIndex={-1}
        onKeyDown={trapTab}
        onClick={(e) => e.stopPropagation()}
        className="absolute inset-y-0 left-0 w-[280px] max-w-[85vw] flex flex-col bg-bg-subtle border-r border-border shadow-lift outline-none"
      >
        <div className="shrink-0 flex items-center gap-2.5 px-5 py-4 border-b border-border">
          <span className="inline-flex w-7 h-7 shrink-0 items-center justify-center rounded bg-fg p-1.5">
            {/* The real 42 mark. Same asset as the desktop rail. */}
            <img src="/42-logo.png" alt="" className="h-full w-full object-contain" />
          </span>
          <span className="min-w-0 leading-tight flex-1">
            <span className="block text-[13px] font-semibold tracking-tight text-fg">
              42 Studio
            </span>
            <span className="block truncate text-[11.5px] font-semibold tracking-wide text-fg/70 mt-0.5">
              Creative Command Center
            </span>
          </span>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="Close navigation"
            className="w-8 h-8 shrink-0 rounded-full bg-bg-muted hover:bg-border text-fg flex items-center justify-center transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
          >
            <X className="w-4 h-4" strokeWidth={2.5} aria-hidden />
          </button>
        </div>

        {/* Same entries as the desktop rail, and closing on pick -- a drawer
            that stays open over the page you just asked for is a dead end. */}
        <NavList
          tab={tab}
          onTab={(t) => {
            onTab(t);
            onClose();
          }}
          className="flex-1 px-3 py-4 space-y-1 overflow-y-auto"
        />

        {/* Same account block as the desktop rail. */}
        <AccountFooter onSignedOut={onClose} />
      </div>
    </div>,
    document.body,
  );
}
