import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";

/**
 * App-wide confirm dialog — a themed replacement for the browser's native
 * window.confirm() (which renders an ugly, un-styled "<host> says" popup and
 * can't match our dark UI). Same context shape as ToastProvider: mount once at
 * the root, call `useConfirm()` anywhere.
 *
 * Unlike window.confirm (synchronous), this returns a Promise<boolean>:
 *
 *   const confirm = useConfirm();
 *   if (!(await confirm({ message: "Delete this?", danger: true }))) return;
 */
interface ConfirmOptions {
  /** Bold heading. Defaults to "Are you sure?". */
  title?: string;
  /** Body copy — the question being asked. */
  message: ReactNode;
  /** Confirm button label. Defaults to "OK". */
  confirmLabel?: string;
  /** Cancel button label. Defaults to "Cancel". */
  cancelLabel?: string;
  /** Render the confirm button in the danger (destructive) style. */
  danger?: boolean;
}

type ConfirmFn = (opts: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn | null>(null);

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<ConfirmOptions | null>(null);
  // Kept in a ref (not state) so the state updater stays pure — resolving the
  // promise is a side effect that must run exactly once, in settle().
  const resolveRef = useRef<((ok: boolean) => void) | null>(null);

  const confirm = useCallback<ConfirmFn>(
    (opts) =>
      new Promise<boolean>((resolve) => {
        resolveRef.current = resolve;
        setPending(opts);
      }),
    [],
  );

  const settle = useCallback((ok: boolean) => {
    const resolve = resolveRef.current;
    resolveRef.current = null;
    setPending(null);
    resolve?.(ok);
  }, []);

  // Keyboard: Enter confirms, Escape cancels — matches native confirm UX.
  useEffect(() => {
    if (!pending) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") settle(false);
      else if (e.key === "Enter") settle(true);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [pending, settle]);

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {pending &&
        createPortal(
          <div
            style={{
              position: "fixed",
              inset: 0,
              zIndex: 120,
              background: "rgba(0,0,0,0.75)",
              backdropFilter: "blur(2px)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              padding: "16px",
            }}
            onClick={() => settle(false)}
            role="dialog"
            aria-modal="true"
          >
            <div
              onClick={(e) => e.stopPropagation()}
              className="relative bg-bg rounded-2xl shadow-2xl border border-border overflow-hidden flex flex-col"
              style={{ width: "min(420px, 100%)" }}
            >
              <div className="px-5 pt-5 pb-4">
                <h2 className="text-sm font-semibold text-fg">
                  {pending.title ?? "Are you sure?"}
                </h2>
                <div className="mt-2 text-sm text-muted leading-relaxed">
                  {pending.message}
                </div>
              </div>
              <div className="px-5 py-3 border-t border-border flex items-center justify-end gap-2 bg-bg">
                <button
                  onClick={() => settle(false)}
                  className="btn btn-ghost text-xs"
                >
                  {pending.cancelLabel ?? "Cancel"}
                </button>
                <button
                  autoFocus
                  onClick={() => settle(true)}
                  className={`btn text-xs ${pending.danger ? "btn-danger" : "btn-primary"}`}
                >
                  {pending.confirmLabel ?? "OK"}
                </button>
              </div>
            </div>
          </div>,
          document.body,
        )}
    </ConfirmContext.Provider>
  );
}

export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext);
  if (!ctx) throw new Error("useConfirm must be inside <ConfirmProvider>");
  return ctx;
}
