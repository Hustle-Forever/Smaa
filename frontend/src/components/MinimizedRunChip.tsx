import { useEffect, useState } from "react";
import { CheckCircle2, ChevronUp, Loader2, X } from "lucide-react";

import type { ActiveRun } from "../App";
import { api } from "../lib/api";
import { fmtDuration } from "../lib/format";

interface Props {
  run: ActiveRun;
  onClick: () => void;
  onDismiss: () => void;
}

const AGENT_SHORT: Record<string, string> = {
  "website-ingest": "Website crawl",
  "brand-doc-ingest": "Doc ingest",
  "brand-synthesizer": "Re-synthesize",
};

type Status = "started" | "completed" | "failed" | "unknown";

/**
 * Floating chip representing a minimized RunPanel.
 *
 * Polls the run's DB status every 4 seconds so the chip reflects "running"
 * vs "completed" / "failed". Click body → reopen drawer. Click × → dismiss
 * without reopening.
 */
export function MinimizedRunChip({ run, onClick, onDismiss }: Props) {
  const [status, setStatus] = useState<Status>("started");
  const [now, setNow] = useState<number>(Date.now());

  // Poll DB status
  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const r = await api.getRun(run.agent, run.runId);
        if (cancelled) return;
        setStatus((r.status as Status) || "started");
      } catch {
        if (cancelled) return;
        setStatus("unknown");
      }
    }
    poll();
    const t = setInterval(poll, 4000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [run.agent, run.runId]);

  // Elapsed ticker (stops when terminal)
  useEffect(() => {
    if (status !== "started") return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [status]);

  const elapsed = fmtDuration((now - run.startedAtMs) / 1000);
  const terminal = status === "completed" || status === "failed";

  return (
    // Wrapper is presentational: it cannot be the button, because the dismiss
    // control lives inside it and nesting interactive elements is invalid. So
    // the chip BODY is the button and dismiss is its sibling. Previously the
    // wrapper carried role="button" with no tabIndex and no key handler, which
    // announced itself as a button to a screen reader while being unreachable
    // and unactivatable from the keyboard.
    <div
      className={[
        "group flex items-center gap-2 pl-3 pr-2 py-2 rounded-full bg-bg-subtle",
        "shadow-lift border animate-riseFade",
        status === "completed"
          ? "border-success/40"
          : status === "failed"
          ? "border-danger/40"
          : "border-border",
        status === "started" ? "animate-pulseGlow" : "",
        "hover:border-accent transition-colors",
      ].join(" ")}
    >
      <button
        type="button"
        onClick={onClick}
        aria-label={`Reopen ${AGENT_SHORT[run.agent] ?? run.agent} run #${run.runId}`}
        className="flex items-center gap-3 text-left rounded-full cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
      >
      {status === "started" && (
        <Loader2 className="w-4 h-4 text-accent animate-spin" strokeWidth={2} />
      )}
      {status === "completed" && (
        <CheckCircle2
          className="w-4 h-4 text-success animate-scaleIn"
          strokeWidth={2}
        />
      )}
      {status === "failed" && (
        <X className="w-4 h-4 text-danger animate-scaleIn" strokeWidth={2.5} />
      )}

      <div className="leading-tight pr-1">
        <p className="text-xs font-semibold text-fg">
          {AGENT_SHORT[run.agent] ?? run.agent}{" "}
          <span className="font-mono text-muted">#{run.runId}</span>
        </p>
        <p className="font-mono text-3xs text-muted">
          {terminal ? status : `running · ${elapsed}`}
        </p>
      </div>

      {/* Decorative: the button above already carries the accessible name.
          An aria-label on a bare SVG is not reliably announced. */}
      <ChevronUp
        className="w-4 h-4 text-muted opacity-0 group-hover:opacity-100 transition-opacity"
        strokeWidth={2}
        aria-hidden
      />
      </button>

      <button
        onClick={(e) => {
          e.stopPropagation();
          onDismiss();
        }}
        className="w-5 h-5 rounded-full bg-bg-muted hover:bg-border text-muted hover:text-fg flex items-center justify-center transition-colors"
        aria-label="Dismiss"
      >
        <X className="w-3 h-3" strokeWidth={2.5} />
      </button>
    </div>
  );
}
