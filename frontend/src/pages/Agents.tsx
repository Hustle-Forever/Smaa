import { useEffect, useState } from "react";
import type { ComponentType } from "react";
import { Globe, RefreshCw, UploadCloud } from "lucide-react";

import type { Tab } from "../App";
import { Skeleton } from "../components/Skeleton";
import { useToast } from "../components/ToastProvider";
import { api } from "../lib/api";
import { agentLabel, durationBetween, fmtRel } from "../lib/format";
import type { AgentId, RunSummary } from "../lib/types";

interface Props {
  onStartRun: (agent: AgentId, runId: number) => void;
  navigate: (t: Tab, param?: string) => void;
  /** When rendered inside the Brand workspace, the workspace owns the header. */
  embedded?: boolean;
}

interface TaggedRun extends RunSummary {
  _agent: AgentId;
}

interface AgentCard {
  id: AgentId;
  title: string;
  description: string;
  estimate: string;
  Icon: ComponentType<any>;
  /** If set, the card's button navigates instead of POSTing /run. */
  navigateTo?: Tab;
  navigateParam?: string;
  ctaLabel: string;
}

// brand-doc-ingest is intentionally a navigation card, not a Start button:
// the agent needs user-supplied files or text to do anything useful (mirrors
// the legacy n8n workflow's Form Trigger entry point). The Upload tab is the
// canonical surface for that. (Instagram sync lives on its own dedicated tab.)
const AGENTS: AgentCard[] = [
  {
    id: "website-ingest",
    title: "Crawl 42abudhabi.ae",
    description:
      "Discover URLs from the sitemap, fetch HTML, extract text + assets, summarize with GPT-4o-mini, embed everything, then re-synthesize the brand profile.",
    estimate: "~15 min on a cold corpus · ~1 min on a warm one",
    Icon: Globe,
    ctaLabel: "▶ Start",
  },
  {
    id: "brand-doc-ingest",
    title: "Upload brand documents",
    description:
      "Add owner-supplied PDFs, DOCX, TXT/MD, images, or paste free text. The agent processes each new item, embeds it, and re-synthesizes the brand profile.",
    estimate: "≈ 30s per item · runs synthesis at the end",
    Icon: UploadCloud,
    navigateTo: "brand",
    navigateParam: "upload",
    ctaLabel: "Go to upload →",
  },
  {
    id: "brand-synthesizer",
    title: "Re-synthesize brand profile",
    description:
      "Re-run synthesis only — no fresh ingest. Uses whatever you have in the corpus and your current settings.",
    estimate: "~80s · ~$0.10",
    Icon: RefreshCw,
    ctaLabel: "▶ Start",
  },
];

export function AgentsPage({ onStartRun, navigate, embedded = false }: Props) {
  const [runs, setRuns] = useState<TaggedRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<AgentId | null>(null);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const lists = await Promise.all(
          AGENTS.map((a) => api.listRuns(a.id, 10).then((r) => ({ a: a.id, r }))),
        );
        if (cancelled) return;
        const merged: TaggedRun[] = [];
        for (const { a, r } of lists) {
          for (const row of r.items) merged.push({ ...row, _agent: a });
        }
        merged.sort((x, y) => y.id - x.id);
        setRuns(merged.slice(0, 20));
      } catch (e: any) {
        setError(String(e?.message || e));
      } finally {
        setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  async function start(agent: AgentId) {
    setBusy(agent);
    setError(null);
    try {
      const body =
        agent === "brand-synthesizer"
          ? { notes_suffix: "agents page" }
          : {};
      const resp = await api.startRun(agent, body);
      toast.info(`Run #${resp.run_id} started`, agentLabel(agent));
      onStartRun(agent, resp.run_id);
    } catch (e: any) {
      const msg = `Failed to start ${agent}: ${e?.message || e}`;
      setError(msg);
      toast.error("Couldn't start agent", msg);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-8">
      {!embedded && (
        <header>
          <p className="eyebrow mb-1.5">Synthesizer</p>
          <h1 className="h1">Three agents, one corpus</h1>
          <p className="lead mt-2 max-w-2xl">
            Each agent writes a row to <code className="font-mono">scrape_runs</code>{" "}
            and (when relevant) <code className="font-mono">brand_profile_runs</code>{" "}
            so every action is traceable.
          </p>
        </header>
      )}

      <section className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {AGENTS.map((a) => (
          <div
            key={a.id}
            className="card card-hover relative p-4 flex flex-col gap-3 overflow-hidden"
          >
            <div className="flex items-start gap-3">
              <span className="shrink-0 inline-flex w-10 h-10 items-center justify-center rounded-lg bg-accent/10 text-accent">
                <a.Icon className="w-5 h-5" strokeWidth={1.8} />
              </span>
              <div className="min-w-0 flex-1">
                <h2 className="text-sm font-semibold text-fg leading-tight">{a.title}</h2>
                <span className="font-mono text-[10.5px] text-muted">{a.id}</span>
              </div>
            </div>
            <p className="text-[13px] text-muted leading-snug flex-1">{a.description}</p>
            <p className="font-mono text-2xs text-muted">{a.estimate}</p>
            {a.navigateTo ? (
              <button
                onClick={() => navigate(a.navigateTo as Tab, a.navigateParam)}
                className="btn btn-ghost w-full"
              >
                {a.ctaLabel}
              </button>
            ) : (
              <button
                onClick={() => start(a.id)}
                disabled={busy === a.id}
                className="btn btn-primary w-full"
              >
                {busy === a.id ? "Starting…" : a.ctaLabel}
              </button>
            )}
          </div>
        ))}
      </section>
      {error && (
        <p className="text-sm text-danger" role="alert">
          {error}
        </p>
      )}

      <section>
        <p className="eyebrow mb-3">Recent runs across all agents</p>
        {loading ? (
          <ul className="space-y-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <li key={i} className="rounded-xl">
                <Skeleton h="h-14" rounded="rounded-xl" />
              </li>
            ))}
          </ul>
        ) : runs.length === 0 ? (
          <p className="text-sm text-muted">No runs yet — start one above.</p>
        ) : (
          <div className="overflow-hidden card">
            {/* The card clips to its rounded corners, so a table wider than the
                column was cut off with no way to reach the rest. The scroll
                container goes INSIDE the clip, not instead of it. */}
            <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-sm">
              <thead>
                <tr className="text-left border-b border-border bg-bg-muted">
                  <th className="px-3 py-1.5 font-mono text-2xs uppercase tracking-wider2 text-muted">Run</th>
                  <th className="px-3 py-1.5 font-mono text-2xs uppercase tracking-wider2 text-muted">Agent</th>
                  <th className="px-3 py-1.5 font-mono text-2xs uppercase tracking-wider2 text-muted">Status</th>
                  <th className="px-3 py-1.5 font-mono text-2xs uppercase tracking-wider2 text-muted">Duration</th>
                  <th className="px-3 py-1.5 font-mono text-2xs uppercase tracking-wider2 text-muted">Started</th>
                  <th className="px-3 py-1.5 font-mono text-2xs uppercase tracking-wider2 text-muted">Notes</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => {
                  const cls =
                    r.status === "completed"
                      ? "pill-completed"
                      : r.status === "failed"
                      ? "pill-failed"
                      : "pill-running";
                  return (
                    <tr
                      key={`${r._agent}-${r.id}`}
                      className="border-b border-border/40 hover:bg-bg-muted cursor-pointer"
                      onClick={() => onStartRun(r._agent, r.id)}
                    >
                      <td className="px-3 py-1.5 font-mono text-xs">#{r.id}</td>
                      <td className="px-3 py-1.5 text-fg">{agentLabel(r._agent)}</td>
                      <td className="px-3 py-1.5">
                        <span className={`pill ${cls}`}>{r.status}</span>
                      </td>
                      <td className="px-3 py-1.5 font-mono text-xs tabular-nums">
                        {durationBetween(r.started_at, r.finished_at)}
                      </td>
                      <td className="px-3 py-1.5 font-mono text-xs text-muted">
                        {fmtRel(r.started_at)}
                      </td>
                      <td className="px-3 py-1.5 text-xs text-fg/80 truncate max-w-[16rem]">
                        {r.notes ?? "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            </div>
          </div>
        )}
        <p className="font-mono text-2xs text-muted mt-2">
          Tip: click a row to re-open its live progress stream.
        </p>
      </section>
    </div>
  );
}
