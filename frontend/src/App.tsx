import { lazy, Suspense, useCallback, useEffect, useState } from "react";

import { Layout } from "./components/Layout";
import { MinimizedRunChip } from "./components/MinimizedRunChip";
import { RunPanel } from "./components/RunPanel";
import { ConfirmProvider } from "./components/ConfirmProvider";
import { ToastProvider } from "./components/ToastProvider";
import { DashboardPage } from "./pages/Dashboard";
import { Skeleton } from "./components/Skeleton";

/**
 * Routes are split; the shell is not.
 *
 * Everything used to arrive in one 1.28 MB chunk, so opening the Dashboard
 * downloaded the video editor, every creator and the whole trends tree first.
 * There is no router here -- routing is a hash read and a flat list of
 * `{tab === "x" && <Page/>}` guards -- so the lazy boundaries simply attach to
 * those guards, which is all React.lazy needs.
 *
 * Dashboard stays EAGER on purpose: it is the landing tab and the fallback for
 * every unrecognised hash, so making it lazy would trade a smaller bundle for a
 * skeleton flash on the one route everybody sees first.
 */
const AssetsPage = lazy(() =>
  import("./pages/Assets").then((m) => ({ default: m.AssetsPage })),
);
const BrandWorkspace = lazy(() =>
  import("./pages/BrandWorkspace").then((m) => ({ default: m.BrandWorkspace })),
);
const CampaignsPage = lazy(() =>
  import("./pages/Campaigns").then((m) => ({ default: m.CampaignsPage })),
);
const SettingsPage = lazy(() =>
  import("./pages/Settings").then((m) => ({ default: m.SettingsPage })),
);
const TrendFinder = lazy(() =>
  import("./pages/TrendFinder").then((m) => ({ default: m.TrendFinder })),
);
const YasProducerPage = lazy(() =>
  import("./pages/YasProducer").then((m) => ({ default: m.YasProducerPage })),
);
const YasLibrary = lazy(() =>
  import("./components/yas/YasLibrary").then((m) => ({ default: m.YasLibrary })),
);

/**
 * Suspense fallback.
 *
 * aria-busy and aria-live are not decoration: a fallback swapping to real
 * content is exactly the asynchronous change a screen reader is otherwise never
 * told about, and this app announces almost nothing at page level today.
 */
function RouteFallback() {
  return (
    <div className="space-y-4" aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading…</span>
      <Skeleton h="h-3" w="w-24" />
      <Skeleton h="h-10" w="w-full" className="max-w-md" />
      <Skeleton h="h-48" w="w-full" />
    </div>
  );
}
import {
  libraryOpenTarget,
  parseProducerFreshTool,
  parseProducerResume,
} from "./lib/producerHash";
import type { AgentId } from "./lib/types";

// Note: "producer" is the Producer studio (formerly "yas-producer"); the legacy
// Producer page was removed. Its REST API namespace stays at /api/yas-producer.
// "campaigns" is the autonomous Producer surface (sidebar label "Producer",
// beta). It is distinct from "producer", which is the hands-on Ideation studio.
export type Tab =
  | "dashboard" | "settings" | "agents" | "upload"
  | "brand" | "producer" | "campaigns" | "trends" | "library" | "instagram" | "assets";

// Must list every member of Tab: tabFromHash validates against THIS array, so a
// union member missing here silently falls back to "dashboard" at runtime.
const VALID_TABS: Tab[] = [
  "dashboard", "settings", "agents", "upload",
  "brand", "producer", "campaigns", "trends", "library", "instagram", "assets",
];

function tabFromHash(): Tab {
  // Support hash with optional route segment, e.g. "producer/42".
  let h = window.location.hash.replace(/^#/, "").split("/")[0];
  // Legacy alias: the studio moved from #yas-producer → #producer. Keep old
  // deep-links (e.g. an already-open Video Editor session) resolving correctly.
  if (h === "yas-producer") h = "producer";
  return VALID_TABS.includes(h as Tab) ? (h as Tab) : "dashboard";
}

/** Optional second hash segment, e.g. "producer/42" → "42". Used to deep-
 *  link a Human Element Video run for "Continue editing". */
function routeParamFromHash(): string | null {
  const parts = window.location.hash.replace(/^#/, "").split("/");
  return parts.length > 1 && parts[1] ? parts[1] : null;
}

// The #producer / #library hash grammar (parsers + builders) lives in
// ./lib/producerHash so in-app URL writers and these readers share one source.

export interface ActiveRun {
  agent: AgentId;
  runId: number;
  /** info messages from the upload endpoint (e.g. "skipped foo.zip") */
  preface?: string;
  /** wall-clock ms at which the user kicked off this run */
  startedAtMs: number;
}

export default function App() {
  const [tab, setTab] = useState<Tab>(tabFromHash);

  /**
   * The studio mounts on first visit and stays mounted after that.
   *
   * Layout's `persistent` slot exists so an in-flight generation survives a tab
   * switch, which means the studio used to be mounted from the first paint for
   * everybody -- including people who never open Ideation. With the route split
   * that also meant its chunk was fetched immediately, undoing the split for the
   * single biggest page in the app. Gating on first visit keeps the keep-alive
   * behaviour exactly as it was (once you have been there, it never unmounts)
   * while people who never go there never pay for it.
   *
   * Safe because YasProducerPage is prop-driven and returns a keyed
   * ResumeRouter, so mounting later behaves the same as mounting at boot.
   */
  const [producerMounted, setProducerMounted] = useState(
    () => tabFromHash() === "producer",
  );
  useEffect(() => {
    if (tab === "producer") setProducerMounted(true);
  }, [tab]);
  const [routeParam, setRouteParam] = useState<string | null>(routeParamFromHash);
  const [activeRun, setActiveRun] = useState<ActiveRun | null>(null);
  /** runs the user hid (clicked Hide) — shown as floating chips, click to reopen */
  const [minimized, setMinimized] = useState<ActiveRun[]>([]);

  // Sync hash <-> state
  useEffect(() => {
    const onHash = () => {
      setTab(tabFromHash());
      setRouteParam(routeParamFromHash());
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const navigate = useCallback((t: Tab, param?: string) => {
    window.location.hash = param ? `${t}/${param}` : t;
    setTab(t);
    setRouteParam(param ?? null);
  }, []);

  const startRun = useCallback(
    (agent: AgentId, runId: number, preface?: string) => {
      // If the run is already minimized (user reopened it via a list), promote
      // it back out of the minimized list.
      setMinimized((prev) =>
        prev.filter((r) => !(r.agent === agent && r.runId === runId)),
      );
      setActiveRun({ agent, runId, preface, startedAtMs: Date.now() });
    },
    [],
  );

  /** "Hide" from the drawer: keep tracking, render as a chip. */
  const hideRun = useCallback(() => {
    setActiveRun((cur) => {
      if (cur) {
        setMinimized((m) => {
          // Avoid duplicates if user spam-clicks
          if (m.some((r) => r.agent === cur.agent && r.runId === cur.runId)) return m;
          return [...m, cur];
        });
      }
      return null;
    });
  }, []);

  /** "Close" from the drawer on terminal state: dismiss entirely, no chip. */
  const closeRun = useCallback(() => setActiveRun(null), []);

  /** Click a chip → reopen the drawer for that run. */
  const reopenRun = useCallback((r: ActiveRun) => {
    setMinimized((prev) =>
      prev.filter((x) => !(x.agent === r.agent && x.runId === r.runId)),
    );
    setActiveRun(r);
  }, []);

  /** Dismiss a chip without reopening. */
  const dismissChip = useCallback((r: ActiveRun) => {
    setMinimized((prev) =>
      prev.filter((x) => !(x.agent === r.agent && x.runId === r.runId)),
    );
  }, []);

  return (
    <ToastProvider>
      <ConfirmProvider>
      <Layout
        tab={tab}
        onTab={navigate}
        persistent={
          !producerMounted ? null : (
          // Producer studio (formerly "yas-producer"; route #producer, API
          // namespace /api/yas-producer). Kept mounted across tab switches so
          // an in-progress generation + typed inputs aren't lost on navigation
          // (its background poll loop keeps running). Hidden when inactive.
          <div hidden={tab !== "producer"}>
            <Suspense fallback={null}>
            <YasProducerPage
              initialRunId={tab === "producer" ? (parseProducerResume(routeParam)?.id ?? null) : null}
              initialType={tab === "producer" ? (parseProducerResume(routeParam)?.type ?? null) : null}
              initialStep={tab === "producer" ? (parseProducerResume(routeParam)?.step ?? null) : null}
              initialRunIds={tab === "producer" ? (parseProducerResume(routeParam)?.ids ?? null) : null}
              initialFreshTool={tab === "producer" ? parseProducerFreshTool(routeParam) : null}
              navigate={navigate}
            />
            </Suspense>
          </div>
          )
        }
      >
        <Suspense fallback={<RouteFallback />}>
        {tab === "dashboard" && (
          <DashboardPage onStartRun={startRun} navigate={navigate} />
        )}
        {/* Settings is sectioned and every section is a real address, so it
            takes the route param like the other workspaces do. */}
        {tab === "settings" && (
          <SettingsPage
            section={routeParam}
            navigate={(s) => navigate("settings", s === "brand" ? undefined : s)}
          />
        )}
        {/* Brand workspace — Profile · Upload · Synthesizer. The legacy
            "agents"/"upload" tabs are deep-link aliases into the same surface. */}
        {tab === "brand" && (
          <BrandWorkspace subtab={routeParam} navigate={navigate} onStartRun={startRun} />
        )}
        {tab === "agents" && (
          <BrandWorkspace subtab="synthesizer" navigate={navigate} onStartRun={startRun} />
        )}
        {tab === "upload" && (
          <BrandWorkspace subtab="upload" navigate={navigate} onStartRun={startRun} />
        )}
        {/* Instagram now lives inside the Brand workspace (4th sub-tab). The
            #instagram route (e.g. from the Dashboard card) deep-links straight
            to that sub-tab; the page itself is unchanged. */}
        {tab === "instagram" && (
          <BrandWorkspace subtab="instagram" navigate={navigate} onStartRun={startRun} />
        )}
        {tab === "assets" && <AssetsPage />}
        {/* Autonomous campaigns. Rendered here, NOT in the persistent slot: all
            of its state (what's generating, what awaits review) lives in the
            database, so a remount just re-reads the store cache. The persistent
            slot exists only for in-flight CLIENT state. #campaigns/<id> opens
            one campaign's detail. */}
        {tab === "campaigns" && (
          <CampaignsPage campaignId={routeParam} navigate={navigate} />
        )}
        {/* Trend finder — competitor watch → overperforming posts → angles 42
            can actually make. Sub-tab rides the second hash segment
            (#trends/themes, #trends/accounts, #trends/news); bare #trends is
            whichever tab the section is currently landing on (TrendFinder's
            LANDING). */}
        {tab === "trends" && <TrendFinder subtab={routeParam} navigate={navigate} />}
        {/* Library — generation history + the Scheduled posts queue. Top-level
            tab; the reminder email deep-links to #library/scheduled. */}
        {tab === "library" && (
          <YasLibrary
            initialSubtab={routeParam === "scheduled" ? "scheduled" : undefined}
            initialOpen={libraryOpenTarget(routeParam)}
            onContinue={(runId, type) => navigate("producer", `${type}:${runId}`)}
          />
        )}
        {/* Producer studio renders in Layout's `persistent` slot (above) so it
            survives tab switches — see the comment there. */}
        </Suspense>
      </Layout>

      {activeRun && (
        <RunPanel
          key={`${activeRun.agent}-${activeRun.runId}`}
          agent={activeRun.agent}
          runId={activeRun.runId}
          preface={activeRun.preface}
          startedAtMs={activeRun.startedAtMs}
          onHide={hideRun}
          onClose={closeRun}
        />
      )}

      {/* Minimized run chips — bottom-right stack, click to reopen */}
      {minimized.length > 0 && (
        <div className="fixed bottom-4 right-4 z-40 flex flex-col-reverse gap-2">
          {minimized.map((r) => (
            <MinimizedRunChip
              key={`${r.agent}-${r.runId}`}
              run={r}
              onClick={() => reopenRun(r)}
              onDismiss={() => dismissChip(r)}
            />
          ))}
        </div>
      )}
      </ConfirmProvider>
    </ToastProvider>
  );
}
