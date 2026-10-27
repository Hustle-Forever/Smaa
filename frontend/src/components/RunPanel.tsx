import { useEffect, useMemo, useRef, useState } from "react";
import { AlertCircle, Check, CheckCircle2, Minus, X } from "lucide-react";

import { api, eventsUrl } from "../lib/api";
import { fmtDuration } from "../lib/format";
import type { AgentId, SSEEvent } from "../lib/types";
import { PhaseIcon } from "./PhaseIcon";

interface Props {
  agent: AgentId;
  runId: number;
  preface?: string;
  /** Wall-clock ms when the run was started — used so the elapsed timer
   *  stays continuous across hide/reopen cycles. */
  startedAtMs?: number;
  /** "Hide" — keeps the run tracked as a floating chip. */
  onHide: () => void;
  /** "Close" — dismiss entirely (only available after the run finishes). */
  onClose: () => void;
}

type PhaseState = "pending" | "running" | "completed" | "failed" | "skipped";

interface PhaseRow {
  name: string;
  state: PhaseState;
  processed: number;
  total: number;
  failed: number;
  skipped: number;
  extra?: Record<string, unknown>;
}

type RunState =
  | "connecting"
  | "running"
  | "completed"
  | "cancelled"
  | "failed";

// Canonical phase order per agent so the timeline reads top-to-bottom.
const PHASE_ORDER_BY_AGENT: Record<AgentId, string[]> = {
  "website-ingest": [
    "discovery",
    "fetch",
    "extract",
    "chunk",
    "summarize",
    "embed_text",
    "download:image",
    "image",
    "download:video",
    "video",
    "synthesize",
  ],
  "brand-doc-ingest": [
    "doc_process",
    "doc_summarize_embed",
    "pdf_pages",
    "brand_extract",
    "synthesize",
  ],
  "brand-synthesizer": ["synthesize"],
  "instagram-ingest": [
    "discover",
    "download",
    "image",
    "video",
    "caption_embed",
    "metrics_snapshot",
  ],
};

const PRETTY_PHASE: Record<string, string> = {
  discovery: "Discover URLs (sitemap)",
  fetch: "Fetch pages (HTTP)",
  extract: "Extract text + assets",
  chunk: "Chunk text for embedding",
  summarize: "Summarize pages (GPT-4o-mini)",
  embed_text: "Embed text (1536d)",
  "download:image": "Download images",
  image: "Describe + embed images",
  "download:video": "Download videos",
  video: "Process videos (Whisper + CLIP)",
  synthesize: "Synthesize brand profile",
  doc_process: "Extract document content",
  doc_summarize_embed: "Summarize + embed docs",
  pdf_pages: "Rasterize + analyse PDF pages",
  brand_extract: "Extract brand details (colours, fonts)",
  discover: "Discover IG posts (Business Discovery)",
  download: "Download IG media",
  caption_embed: "Embed captions (768d)",
  metrics_snapshot: "Snapshot engagement metrics",
};

const AGENT_LABEL: Record<AgentId, string> = {
  "website-ingest": "WEBSITE-INGEST",
  "brand-doc-ingest": "BRAND-DOC-INGEST",
  "brand-synthesizer": "BRAND-SYNTHESIZER",
  "instagram-ingest": "INSTAGRAM-INGEST",
};

export function RunPanel({
  agent,
  runId,
  preface,
  startedAtMs,
  onHide,
  onClose,
}: Props) {
  const [phases, setPhases] = useState<Record<string, PhaseRow>>({});
  const [runState, setRunState] = useState<RunState>("connecting");
  const [startedAt] = useState<number>(startedAtMs ?? Date.now());
  const [now, setNow] = useState<number>(Date.now());
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [mounted, setMounted] = useState(false);

  const seenRef = useRef<Set<string>>(new Set());

  // Drawer entry animation: start off-screen, then slide in next paint.
  useEffect(() => {
    const t = requestAnimationFrame(() => setMounted(true));
    return () => cancelAnimationFrame(t);
  }, []);

  // Elapsed ticker
  useEffect(() => {
    if (
      runState === "completed" ||
      runState === "cancelled" ||
      runState === "failed"
    )
      return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [runState]);

  // SSE wiring (same as before — only the rendering changes)
  useEffect(() => {
    const url = eventsUrl(agent, runId);
    const es = new EventSource(url);

    const handle = (evt: MessageEvent) => {
      let data: SSEEvent;
      try {
        data = JSON.parse(evt.data);
      } catch {
        return;
      }
      const key = `${data.type}:${(data as any).phase ?? ""}:${(data as any).ts ?? Math.random()}`;
      if (seenRef.current.has(key)) return;
      seenRef.current.add(key);

      setPhases((prev) => {
        const next = { ...prev };
        const phaseName = (data as any).phase;
        switch (data.type) {
          case "run_started":
            return prev;
          case "phase_started":
            next[phaseName] = {
              name: phaseName,
              state: "running",
              processed: 0,
              total: 0,
              failed: 0,
              skipped: 0,
            };
            return next;
          case "progress": {
            const cur =
              next[phaseName] ??
              ({
                name: phaseName,
                state: "running",
                processed: 0,
                total: 0,
                failed: 0,
                skipped: 0,
              } as PhaseRow);
            next[phaseName] = {
              ...cur,
              state: "running",
              processed: (data as any).processed,
              total: (data as any).total,
            };
            return next;
          }
          case "phase_completed": {
            const d = data as any;
            const isSkipped =
              d.processed === 0 &&
              d.total === 0 &&
              (d.skipped > 0 || d.failed === 0);
            const isFailed = d.failed > 0 && d.processed === 0;
            next[phaseName] = {
              name: phaseName,
              state: isFailed ? "failed" : isSkipped ? "skipped" : "completed",
              processed: d.processed ?? 0,
              total: d.total ?? 0,
              failed: d.failed ?? 0,
              skipped: d.skipped ?? 0,
              extra: d.extra,
            };
            return next;
          }
          default:
            return prev;
        }
      });

      switch (data.type) {
        case "run_started":
          setRunState("running");
          break;
        case "run_completed":
          setRunState("completed");
          es.close();
          break;
        case "run_cancelled":
          setRunState("cancelled");
          es.close();
          break;
        case "run_failed":
          setRunState("failed");
          setError((data as any).error || "unknown error");
          es.close();
          break;
        case "no_active_run":
          setRunState("failed");
          setError("Run is no longer active.");
          es.close();
          break;
      }
    };

    es.addEventListener("run_started", handle as any);
    es.addEventListener("phase_started", handle as any);
    es.addEventListener("phase_completed", handle as any);
    es.addEventListener("progress", handle as any);
    es.addEventListener("run_completed", handle as any);
    es.addEventListener("run_cancelled", handle as any);
    es.addEventListener("run_failed", handle as any);
    es.addEventListener("info", handle as any);
    es.addEventListener("message", handle as any);

    return () => {
      es.close();
    };
  }, [agent, runId]);

  const orderedPhases = useMemo<PhaseRow[]>(() => {
    const known = PHASE_ORDER_BY_AGENT[agent] ?? [];
    const rest = Object.keys(phases).filter((k) => !known.includes(k));
    return [...known, ...rest].map((name) => {
      const existing = phases[name];
      if (existing) return existing;
      return {
        name,
        state: "pending",
        processed: 0,
        total: 0,
        failed: 0,
        skipped: 0,
      };
    });
  }, [agent, phases]);

  const completedCount = orderedPhases.filter(
    (p) => p.state === "completed" || p.state === "skipped",
  ).length;
  const totalPhases = orderedPhases.length;
  const overallPct =
    totalPhases > 0 ? Math.min(100, (completedCount / totalPhases) * 100) : 0;

  const elapsedSec = (now - startedAt) / 1000;
  const elapsed = fmtDuration(elapsedSec);
  const terminal =
    runState === "completed" ||
    runState === "cancelled" ||
    runState === "failed";

  // Escape dismisses, and the page behind must not scroll while the drawer is
  // open. Both were missing: this was the one dialog in the tree with
  // role="dialog" + aria-modal that a keyboard could not leave and that let the
  // page scroll underneath it.
  //
  // Dismissing goes through a ref so the listener binds ONCE. `terminal` flips
  // mid-run as SSE arrives, and re-subscribing on every status tick is how you
  // get a stale closure calling the wrong one of onClose/onHide.
  //
  // NOTE: this drawer deliberately does NOT createPortal, unlike every other
  // dialog here. App.tsx renders it as a sibling of <Layout>, already outside
  // the PageTransition transform that mis-positions non-portalled modals
  // elsewhere. Portalling it would be a no-op at best and would break the
  // hide-vs-close backdrop semantics at worst. Leave it where it is.
  const dismissRef = useRef<() => void>(() => {});
  dismissRef.current = terminal ? onClose : onHide;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") dismissRef.current();
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, []);

  async function cancel() {
    if (cancelling) return;
    setCancelling(true);
    try {
      await api.cancelRun(agent, runId);
    } catch (e) {
      setError(String(e));
    } finally {
      setCancelling(false);
    }
  }

  const headerTint =
    runState === "completed"
      ? "bg-state-completed"
      : runState === "failed" || runState === "cancelled"
      ? "bg-state-failed"
      : "bg-state-running";

  const progressColor =
    runState === "completed"
      ? "bg-success"
      : runState === "failed" || runState === "cancelled"
      ? "bg-danger"
      : "bg-accent";

  const titleColor =
    runState === "completed"
      ? "text-success"
      : runState === "failed" || runState === "cancelled"
      ? "text-danger"
      : "text-fg";

  const titleText =
    runState === "connecting"
      ? "Connecting…"
      : runState === "running"
      ? "Running"
      : runState === "completed"
      ? "Completed"
      : runState === "cancelled"
      ? "Cancelled"
      : "Failed";

  return (
    <div
      className="fixed inset-0 z-50 flex justify-end"
      role="dialog"
      aria-modal="true"
    >
      {/* Backdrop — click hides (keeps the chip) if still running, closes if terminal */}
      <div
        className={[
          "absolute inset-0 bg-fg/30 transition-opacity duration-200",
          mounted ? "opacity-100" : "opacity-0",
        ].join(" ")}
        onClick={terminal ? onClose : onHide}
        aria-hidden
      />

      {/* Drawer */}
      <div
        className={[
          "relative h-full w-full md:max-w-[480px] bg-bg-subtle border-l border-border shadow-lift",
          "flex flex-col transition-transform duration-300",
          mounted ? "translate-x-0" : "translate-x-full",
        ].join(" ")}
      >
        {/* Header */}
        <div className={`relative px-6 py-4 border-b border-border ${headerTint}`}>
          <p className="eyebrow mb-1.5">
            Run #{runId} · {AGENT_LABEL[agent]}
          </p>
          <div className="flex items-baseline justify-between gap-4">
            <div className="flex items-center gap-3">
              <h3 className={`h2 ${titleColor}`}>{titleText}</h3>
              {runState === "completed" && (
                <CheckCircle2
                  className="w-6 h-6 text-success animate-scaleIn"
                  strokeWidth={2}
                />
              )}
              {(runState === "failed" || runState === "cancelled") && (
                <AlertCircle
                  className="w-6 h-6 text-danger animate-scaleIn"
                  strokeWidth={2}
                />
              )}
            </div>
            <div className="text-right">
              <p className="font-mono text-3xs uppercase tracking-wider2 text-muted">
                Elapsed
              </p>
              <p className="font-mono text-base font-semibold tabular-nums text-fg">
                {elapsed}
              </p>
            </div>
          </div>

          {/* Overall progress strip */}
          <div className="mt-4 h-[3px] bg-border rounded-full overflow-hidden">
            <div
              className={`h-full ${progressColor} transition-all duration-500 ease-out`}
              style={{
                width:
                  runState === "connecting"
                    ? "0%"
                    : runState === "completed"
                    ? "100%"
                    : `${overallPct}%`,
              }}
            />
          </div>
          {runState === "connecting" && (
            <div className="absolute bottom-0 left-0 right-0 h-[3px] shimmer-bar" />
          )}
        </div>

        {preface && (
          <div className="px-6 py-2.5 border-b border-border bg-bg-muted">
            <p className="font-mono text-2xs text-fg/80">{preface}</p>
          </div>
        )}

        {/* Timeline — sits on a slightly deeper surface for visual layering */}
        <div className="flex-1 overflow-y-auto bg-bg-deep px-6 py-5">
          <ul className="relative space-y-0.5">
            {orderedPhases.map((p, idx) => (
              <PhaseRowItem
                key={p.name}
                phase={p}
                index={idx}
                isLast={idx === orderedPhases.length - 1}
              />
            ))}
          </ul>
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-border bg-bg-subtle flex items-center justify-between gap-3">
          {error ? (
            <p
              className="text-xs text-danger truncate flex-1 font-mono"
              title={error}
            >
              {error}
            </p>
          ) : (
            <span className="flex-1" />
          )}
          <div className="flex items-center gap-2">
            {!terminal && (
              <button onClick={cancel} disabled={cancelling} className="btn btn-ghost">
                {cancelling ? "Cancelling…" : "Cancel run"}
              </button>
            )}
            <button
              onClick={terminal ? onClose : onHide}
              className="btn btn-primary"
              title={
                terminal
                  ? "Dismiss this run"
                  : "Hide — run keeps going, click the floating chip to reopen"
              }
            >
              {terminal ? "Close" : "Hide"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

interface RowProps {
  phase: PhaseRow;
  index: number;
  isLast: boolean;
}

function PhaseRowItem({ phase: p, index, isLast }: RowProps) {
  const pct =
    p.total > 0 ? Math.min(100, Math.round((p.processed / p.total) * 100)) : 0;

  return (
    <li
      className="relative pl-10 pr-2 py-1.5 animate-riseFade"
      style={{ animationDelay: `${index * 50}ms` }}
    >
      {/* Connecting line */}
      {!isLast && (
        <span
          className={[
            "absolute left-[15px] top-8 bottom-[-2px] w-px",
            p.state === "completed" || p.state === "skipped"
              ? "bg-success/60"
              : "bg-border",
          ].join(" ")}
          aria-hidden
        />
      )}

      {/* Dot */}
      <Dot state={p.state} />

      {/* Body */}
      <div className="flex items-center gap-2 mb-1">
        <PhaseIcon phase={p.name} className="w-4 h-4 text-muted" />
        <p className="text-sm font-medium text-fg">
          {PRETTY_PHASE[p.name] ?? p.name}
        </p>
      </div>
      <p className="font-mono text-2xs text-muted">
        {p.state === "pending" && "waiting"}
        {p.state === "running" &&
          (p.total > 0 ? `${p.processed} / ${p.total} (${pct}%)` : "in progress")}
        {p.state === "completed" &&
          `${p.processed} processed${p.skipped ? ` · ${p.skipped} skipped` : ""}${
            p.failed ? ` · ${p.failed} failed` : ""
          }`}
        {p.state === "skipped" && "skipped"}
        {p.state === "failed" && `${p.failed} failed`}
      </p>

      {p.state === "running" && p.total > 0 && (
        <div className="mt-2 bar">
          <div style={{ width: `${pct}%` }} />
        </div>
      )}
    </li>
  );
}

function Dot({ state }: { state: PhaseState }) {
  const base =
    "absolute left-2 top-2 w-5 h-5 rounded-full flex items-center justify-center";
  if (state === "running") {
    return (
      <span
        className={`${base} bg-accent text-white animate-ringPulse`}
        aria-label="in progress"
      />
    );
  }
  if (state === "completed") {
    return (
      <span className={`${base} bg-success text-white`} aria-label="completed">
        <Check className="w-3 h-3 animate-scaleIn" strokeWidth={3} />
      </span>
    );
  }
  if (state === "failed") {
    return (
      <span className={`${base} bg-danger text-white`} aria-label="failed">
        <X className="w-3 h-3 animate-scaleIn" strokeWidth={3} />
      </span>
    );
  }
  if (state === "skipped") {
    return (
      <span
        className={`${base} bg-bg-muted border border-border text-muted`}
        aria-label="skipped"
      >
        <Minus className="w-3 h-3" strokeWidth={3} />
      </span>
    );
  }
  // pending
  return (
    <span
      className={`${base} bg-bg-subtle border-2 border-border`}
      aria-label="pending"
    />
  );
}
