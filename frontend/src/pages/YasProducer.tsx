import { useEffect, useState } from "react";

import type { Tab } from "../App";
import { TalkingHeadV2Creator } from "../components/yas/TalkingHeadV2Creator";
import { ProducerRoadmap } from "../components/yas/roadmap/ProducerRoadmap";
import { useYasLibraryStore } from "../lib/store";
import type { RoadmapTool } from "../components/yas/roadmap/types";
import type { YasLibraryType } from "../lib/types";

export function YasProducerPage({
  initialRunId = null,
  initialType = null,
  initialStep = null,
  initialRunIds = null,
  initialFreshTool = null,
  navigate,
}: {
  initialRunId?: number | null;
  /** Library tool type for the resume target (#producer/<type>:<id>). Lets the
   *  ResumeRouter dispatch without a cache lookup — needed because poster /
   *  animation / poster_human ids live in separate id-spaces and can collide. */
  initialType?: YasLibraryType | null;
  /** Wizard sub-step from the resume URL (#producer/<type>:<id>@<step>) — seeds
   *  the creator's starting sub-step (Human Element Video, B-roll). */
  initialStep?: string | null;
  /** Batch grid ids from the resume URL (#producer/<type>:<id1>+<id2>+…) — seeds
   *  a batch tool's 2x2 grid (Poster / Animation / Poster+Human). */
  initialRunIds?: number[] | null;
  /** "Fresh tool" deep link (#producer/<tool>) — open this creator blank,
   *  bypassing the hub grid. Ignored when initialRunId is set (resume wins). */
  initialFreshTool?: RoadmapTool | null;
  navigate?: (tab: Tab, param?: string) => void;
} = {}) {
  // Deep-linked resume (#producer/<runId>) → open the right creator pre-loaded
  // on that run (originates from a Library "Continue editing" button). Back
  // returns to the top-level Library tab.
  if (initialRunId != null) {
    return (
      <ResumeRouter
        key={`${initialType ?? ""}:${initialRunId}`}
        runId={initialRunId}
        forcedType={initialType}
        initialStep={initialStep}
        initialRunIds={initialRunIds}
        onBack={() => navigate?.("library")}
        navigate={navigate}
      />
    );
  }

  // Fresh-tool deep link (#producer/<tool>) → boot straight into that creator,
  // blank. `initialTool` with no `initialRunId` opens the editor without a
  // resume fetch or a localStorage-draft wipe — URL/bookmark entry behaves like
  // a resume (keep any in-progress draft), not an explicit hub-card click. Keyed
  // by the tool so navigating between fresh-tool links remounts into the new one.
  if (initialFreshTool) {
    return (
      <ProducerRoadmap
        key={initialFreshTool}
        navigate={navigate}
        initialTool={initialFreshTool}
      />
    );
  }

  // Producer = the global creation roadmap. The Video Editor is now a roadmap
  // tool (Editing stage), not a separate full-screen surface.
  return <ProducerRoadmap navigate={navigate} />;
}

// ---------------------------------------------------------------------------
// Resume router — picks the creator for a deep-linked run by its Library type.
//
// The Library is the only source of "Continue editing"; its cached items carry
// the `type`. On a cold deep-link the cache is empty, so we fetch the list once
// then resolve. Unknown type falls back to the v2 creator (prior behavior).
// ---------------------------------------------------------------------------

function ResumeRouter({
  runId,
  forcedType = null,
  initialStep = null,
  initialRunIds = null,
  onBack,
  navigate,
}: {
  runId: number;
  /** Type threaded through the resume URL — unambiguous across id-spaces. */
  forcedType?: YasLibraryType | null;
  /** Wizard sub-step to resume at (#producer/<type>:<id>@<step>). */
  initialStep?: string | null;
  /** Batch grid ids to seed on resume (#producer/<type>:<id1>+<id2>+…). */
  initialRunIds?: number[] | null;
  onBack: () => void;
  navigate?: (tab: Tab, param?: string) => void;
}) {
  const items = useYasLibraryStore((s) => s.items);
  const fetchLib = useYasLibraryStore((s) => s.fetch);
  const [checked, setChecked] = useState(false);

  // Prefer the type from the URL. Fall back to a cache lookup only for legacy
  // bare-id deep-links (#producer/<id>) that don't carry a type.
  const cachedType = items.find((it) => it.id === runId)?.type ?? null;
  const type = forcedType ?? cachedType;

  useEffect(() => {
    if (type) {
      setChecked(true);
      return;
    }
    let cancelled = false;
    // The cached list can be hours old and miss a just-created project (the
    // deep-linked id would then fall through to the wrong creator → 404).
    // Invalidate so this fetch actually hits the network.
    useYasLibraryStore.getState().invalidate();
    fetchLib({ limit: 300 }).finally(() => {
      if (!cancelled) setChecked(true);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  if (!type && !checked) {
    return (
      <div className="flex items-center justify-center h-[calc(100vh-80px)]">
        <p className="text-sm text-muted">Loading…</p>
      </div>
    );
  }

  // Roadmap tools resume INSIDE the roadmap so the Editing → Finalization →
  // Scheduling → Approval rail is available AND a left-off roadmap (saved per
  // project) reopens at its exact step. The matching creator hydrates from
  // initialRunId. (talking_head — v1 — is NOT a roadmap tool → standalone.)
  if (
    type === "video_editor" ||
    type === "poster" ||
    type === "animation" ||
    type === "poster_human" ||
    type === "talking_head_v2" ||
    type === "general_video" ||
    type === "broll"
  ) {
    return (
      <ProducerRoadmap
        key={runId}
        navigate={navigate}
        initialTool={type}
        initialRunId={runId}
        initialStep={initialStep}
        initialRunIds={initialRunIds}
      />
    );
  }
  return (
    <TalkingHeadV2Creator
      key={runId}
      initialRunId={runId}
      initialStep={initialStep}
      onBack={onBack}
      navigate={navigate}
    />
  );
}
