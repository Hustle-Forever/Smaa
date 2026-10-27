import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { Toast } from "./Toast";
import type { ToastData, ToastIntent } from "./Toast";

interface ToastApi {
  show: (input: ShowInput) => string;
  success: (title: string, description?: string) => string;
  info: (title: string, description?: string) => string;
  error: (title: string, description?: string) => string;
  dismiss: (id: string) => void;
}

interface ShowInput {
  title: string;
  description?: string;
  intent?: ToastIntent;
  duration?: number;
}

const ToastContext = createContext<ToastApi | null>(null);

const MAX_TOASTS = 3;
const DEFAULT_DURATION = 3500;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastData[]>([]);

  const dismiss = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const show = useCallback((input: ShowInput) => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const toast: ToastData = {
      id,
      title: input.title,
      description: input.description,
      intent: input.intent ?? "info",
      duration: input.duration ?? DEFAULT_DURATION,
    };
    setToasts((prev) => {
      const next = [...prev, toast];
      // FIFO cap
      if (next.length > MAX_TOASTS) next.splice(0, next.length - MAX_TOASTS);
      return next;
    });
    return id;
  }, []);

  // Stable reference — if we recreated this every render, any consumer that
  // depends on `toast` in a useEffect deps array would loop forever.
  const api = useMemo<ToastApi>(
    () => ({
      show,
      success: (title, description) => show({ title, description, intent: "success" }),
      info: (title, description) => show({ title, description, intent: "info" }),
      error: (title, description) => show({ title, description, intent: "error" }),
      dismiss,
    }),
    [show, dismiss],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      {/* Stack bottom-left so it doesn't collide with the drawer/chip bottom-right.
          Above the modals (ui/Modal, z-[100]): a toast raised inside one — the
          Rebrand studio's pattern library — must not land behind its scrim. */}
      {toasts.length > 0 && (
        <div className="fixed bottom-4 left-4 z-[110] flex flex-col gap-2 max-w-sm w-[22rem]">
          {toasts.map((t) => (
            <Toast key={t.id} toast={t} onDismiss={dismiss} />
          ))}
        </div>
      )}
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be inside <ToastProvider>");
  return ctx;
}
