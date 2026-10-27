import { useEffect, useRef, type KeyboardEvent as ReactKeyboardEvent } from "react";

const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),' +
  'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

/**
 * Everything a dialog owes a keyboard: Escape, a focus trap, a scroll lock, and
 * putting focus back where it came from.
 *
 * Not invented here -- lifted from TrendDetailDialog, which was the only place
 * in the app that got all four right. Two of roughly twenty dialogs trapped
 * focus; the other eighteen were modal to a mouse and a doorway to a keyboard,
 * letting Tab walk out into a filter bar the reader could not see behind the
 * overlay.
 *
 * Three details in here are the difference between working and looking like it
 * works, and all three were learned the hard way in the original:
 *
 * 1. `onClose` goes through a ref and the effect has EMPTY deps. Callers pass an
 *    inline arrow that changes identity every render, and these dialogs sit
 *    inside components that re-render on a poll tick. Depending on it would
 *    re-run the effect mid-interaction and yank focus back to the close button
 *    while someone was tabbing.
 * 2. Focus is restored only if the opener is STILL IN THE DOCUMENT. The card
 *    behind can be gone by the time the dialog closes -- a filter changed, a
 *    refresh landed -- and focusing a detached node silently does nothing while
 *    looking like it worked.
 * 3. The trap skips elements that are not laid out (`offsetParent === null`,
 *    which covers [hidden] and display:none), so a collapsed region does not
 *    become an invisible tab stop.
 */
export function useModalChrome<T extends HTMLElement>({
  onClose,
  panelRef,
  initialFocusRef,
}: {
  onClose: () => void;
  /** The dialog panel. The trap searches inside it. */
  panelRef: React.RefObject<T | null>;
  /**
   * What to focus on open. Prefer the close button: it is always present, and
   * the dialog role and label are announced along with it, so the reader hears
   * what opened. Falls back to the panel itself.
   */
  initialFocusRef?: React.RefObject<HTMLElement | null>;
}) {
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const openerRef = useRef<Element | null>(null);

  useEffect(() => {
    openerRef.current = document.activeElement;

    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onCloseRef.current();
    }
    window.addEventListener("keydown", onKey);

    (initialFocusRef?.current ?? panelRef.current)?.focus();

    return () => {
      document.body.style.overflow = prevOverflow;
      window.removeEventListener("keydown", onKey);
      const opener = openerRef.current;
      if (opener instanceof HTMLElement && document.contains(opener)) {
        opener.focus();
      }
    };
    // Empty on purpose -- see note 1 above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Spread onto the panel as `onKeyDown={trapTab}`. */
  function trapTab(e: ReactKeyboardEvent<HTMLElement>) {
    if (e.key !== "Tab") return;
    const root = panelRef.current;
    if (!root) return;
    const items = Array.from(
      root.querySelectorAll<HTMLElement>(FOCUSABLE),
    ).filter((el) => el.offsetParent !== null || el === document.activeElement);
    if (items.length === 0) return;

    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;

    if (e.shiftKey && (active === first || active === root)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  }

  return { trapTab };
}
