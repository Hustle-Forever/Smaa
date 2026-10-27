import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";

export type ToastIntent = "success" | "info" | "error";

export interface ToastData {
  id: string;
  title: string;
  description?: string;
  intent: ToastIntent;
  /** ms; 0 = persistent */
  duration?: number;
}

interface Props {
  toast: ToastData;
  onDismiss: (id: string) => void;
}

const ICON: Record<ToastIntent, typeof CheckCircle2> = {
  success: CheckCircle2,
  info: Info,
  error: AlertTriangle,
};

const COLOR: Record<ToastIntent, { ring: string; icon: string }> = {
  success: { ring: "border-success/40", icon: "text-success" },
  info: { ring: "border-accent/40", icon: "text-accent" },
  error: { ring: "border-danger/40", icon: "text-danger" },
};

export function Toast({ toast, onDismiss }: Props) {
  const [leaving, setLeaving] = useState(false);
  const Icon = ICON[toast.intent];
  const c = COLOR[toast.intent];

  useEffect(() => {
    if (!toast.duration || toast.duration <= 0) return;
    const t = setTimeout(() => setLeaving(true), toast.duration);
    return () => clearTimeout(t);
  }, [toast.duration]);

  useEffect(() => {
    if (!leaving) return;
    const t = setTimeout(() => onDismiss(toast.id), 180);
    return () => clearTimeout(t);
  }, [leaving, onDismiss, toast.id]);

  return (
    <div
      role="status"
      className={[
        "flex items-start gap-3 pl-3 pr-2 py-2.5 rounded-lg",
        "bg-bg-subtle border shadow-lift",
        c.ring,
        leaving ? "animate-fadeIn opacity-0" : "animate-riseFade",
        "transition-opacity duration-200",
      ].join(" ")}
    >
      <Icon className={`w-4 h-4 mt-0.5 shrink-0 ${c.icon}`} strokeWidth={2} />
      <div className="flex-1 min-w-0 pr-2">
        <p className="text-sm font-medium text-fg leading-tight">{toast.title}</p>
        {toast.description && (
          <p className="text-xs text-muted mt-0.5 leading-snug">
            {toast.description}
          </p>
        )}
      </div>
      <button
        onClick={() => setLeaving(true)}
        className="w-6 h-6 rounded-full hover:bg-bg-muted text-muted hover:text-fg flex items-center justify-center transition-colors shrink-0"
        aria-label="Dismiss"
      >
        <X className="w-3.5 h-3.5" strokeWidth={2.5} />
      </button>
    </div>
  );
}
