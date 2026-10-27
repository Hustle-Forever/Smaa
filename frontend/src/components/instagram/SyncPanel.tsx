import { useState } from "react";

import { useToast } from "../ToastProvider";
import { api } from "../../lib/api";
import { fmtNumber, fmtRel } from "../../lib/format";
import type { AgentId, InstagramHealth } from "../../lib/types";

interface Props {
  onStartRun: (agent: AgentId, runId: number) => void;
  health: InstagramHealth | null;
  loadingHealth: boolean;
  onRefreshHealth: () => void;
}

export function SyncPanel({ onStartRun, health, loadingHealth, onRefreshHealth }: Props) {
  const [busy, setBusy] = useState<"full" | "metrics" | "pilot" | null>(null);
  const [skipVideo, setSkipVideo] = useState(false);
  const [forceReDownload, setForceReDownload] = useState(false);
  const [classifyOnly, setClassifyOnly] = useState(false);
  const [reclassifyExisting, setReclassifyExisting] = useState(false);
  const toast = useToast();

  async function kick(kind: "full" | "metrics" | "pilot") {
    setBusy(kind);
    try {
      const body: Parameters<typeof api.instagramIngest.run>[0] = {};
      if (kind === "metrics") body.metrics_only = true;
      if (kind === "pilot") body.max_posts = 30;
      if (classifyOnly) {
        body.skip_phases = ["discover", "download", "image", "video", "video_deepen", "caption_embed", "metrics_snapshot"];
      } else if (skipVideo) {
        body.skip_phases = ["video"];
      }
      if (forceReDownload) body.force_re_download = true;
      if (reclassifyExisting) body.force_reclassify = true;
      const resp = await api.instagramIngest.run(body);
      toast.info(`Run #${resp.run_id} started`, "Instagram ingest");
      onStartRun("instagram-ingest", resp.run_id);
      // refresh health after a short delay so DB counts update
      setTimeout(onRefreshHealth, 1500);
    } catch (e: any) {
      toast.error("Couldn't start sync", e?.message || String(e));
    } finally {
      setBusy(null);
    }
  }

  const tokenOk = health?.token_ok === true;
  const tokenBad = health && health.configured && health.token_ok === false;

  return (
    <section className="card p-5 space-y-4">
      <div className="flex items-baseline justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-base font-semibold text-fg">
            Sync @{health?.target_username || "—"}
          </h2>
          <p className="text-sm text-muted mt-1">
            Pulls public posts via Meta Business Discovery. Engagement is{" "}
            <strong>likes + comments + view_count only</strong> — full insights
            (reach, impressions, saved) require @42abudhabi's team to authorize
            this app via Business Manager.
          </p>
        </div>
        <button
          onClick={onRefreshHealth}
          disabled={loadingHealth}
          className="btn btn-ghost text-xs"
        >
          {loadingHealth ? "Checking…" : "Refresh status"}
        </button>
      </div>

      {/* Credentials badge */}
      <div className="flex items-center gap-3 text-sm flex-wrap">
        {!health?.configured && (
          <span className="pill pill-failed">Credentials not set</span>
        )}
        {tokenOk && (
          <span className="pill pill-completed">
            Token OK · @{health?.account?.username || "?"}
          </span>
        )}
        {tokenBad && <span className="pill pill-failed">Token rejected</span>}
        {health?.error && (
          <span className="text-xs text-danger truncate max-w-md" title={health.error}>
            {health.error}
          </span>
        )}
      </div>

      {/* DB summary */}
      {health?.db && (
        <dl className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <Stat label="Posts" value={fmtNumber(health.db.total_posts)} />
          <Stat label="Embedded" value={fmtNumber(health.db.embedded_posts)} />
          <Stat label="Latest post" value={fmtRel(health.db.latest_post_at)} />
          <Stat label="Metrics refresh" value={fmtRel(health.db.latest_metrics_at)} />
        </dl>
      )}

      {/* Actions */}
      <div className="space-y-3 pt-2 border-t border-border">
        <label className="flex items-center gap-2 text-sm text-muted">
          <input
            type="checkbox"
            checked={forceReDownload}
            onChange={(e) => setForceReDownload(e.target.checked)}
            className="rounded border-border"
          />
          Force re-download media (use when files are missing or CDN URLs have expired)
        </label>
        <label className="flex items-center gap-2 text-sm text-muted">
          <input
            type="checkbox"
            checked={classifyOnly}
            onChange={(e) => setClassifyOnly(e.target.checked)}
            className="rounded border-border"
          />
          Classify only (skip all other phases — use when media is already downloaded)
        </label>
        <label className="flex items-center gap-2 text-sm text-muted">
          <input
            type="checkbox"
            checked={reclassifyExisting}
            onChange={(e) => setReclassifyExisting(e.target.checked)}
            className="rounded border-border"
          />
          Re-classify existing posts (overwrite current categories — use after the
          classification prompt is updated to fix bad rows)
        </label>
        <label className="flex items-center gap-2 text-sm text-muted">
          <input
            type="checkbox"
            checked={skipVideo}
            disabled={classifyOnly}
            onChange={(e) => setSkipVideo(e.target.checked)}
            className="rounded border-border"
          />
          Skip video phase (no Whisper transcription) — cheap pilot
        </label>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => kick("full")}
            disabled={busy !== null || !tokenOk}
            className="btn btn-primary"
          >
            {busy === "full" ? "Starting…" : "▶ Full sync"}
          </button>
          <button
            onClick={() => kick("pilot")}
            disabled={busy !== null || !tokenOk}
            className="btn btn-ghost"
            title="Limit to most recent 30 posts — verify the pipeline cheaply"
          >
            {busy === "pilot" ? "Starting…" : "Pilot (30 posts)"}
          </button>
          <button
            onClick={() => kick("metrics")}
            disabled={busy !== null || !tokenOk}
            className="btn btn-ghost"
            title="Refresh engagement counts only; no media re-downloads"
          >
            {busy === "metrics" ? "Starting…" : "Refresh metrics only"}
          </button>
        </div>
      </div>
    </section>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="font-mono text-3xs uppercase tracking-wider2 text-muted">
        {label}
      </dt>
      <dd className="font-semibold text-fg tabular-nums">{value}</dd>
    </div>
  );
}
