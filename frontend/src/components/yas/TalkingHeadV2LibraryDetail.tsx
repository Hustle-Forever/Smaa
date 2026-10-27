/**
 * Yas Producer · Library · Talking-Head detail
 *
 * Per-chat detail view for a single talking-head run. Hydrates the FULL run via
 * api.yasProducer.talkingHeadV2.fetch(runId) and shows everything one chat
 * produced, grouped by media type:
 *   1. final stitched video (or a pending notice)
 *   2. Script        (collapsible)
 *   3. Images         (collapsible) — Intro / Outro / Scenes tabs
 *   4. Videos         (collapsible) — Intro / Scenes / Outro tabs
 *
 * Portaled to document.body to escape the page-transition transform that traps
 * `fixed` descendants (see CLAUDE.md modal-pattern note). Read-only — does not
 * touch the creator / composer flows.
 */
import { useEffect, useRef, useState } from "react";

import { useModalChrome } from "../../lib/useModalChrome";
import { createPortal } from "react-dom";

import { api } from "../../lib/api";
import type {
  TalkingHeadRun,
  YasThCompositionStatus,
} from "../../lib/types";
import { CollapsibleSection } from "./CollapsibleSection";
import { useConfirm } from "../ConfirmProvider";
import { useToast } from "../ToastProvider";

/** Human label for the stitch-pipeline status — shared with the tile. */
function compositionLabel(
  status: YasThCompositionStatus | null | undefined,
): string {
  switch (status) {
    case "queued":
      return "Rendering clips…";
    case "videos_ready":
      return "Ready to rebrand";
    case "rendering":
      return "Stitching…";
    case "failed":
      return "Render failed";
    case "ready":
      return "Ready";
    default:
      return "No final video yet";
  }
}

export function TalkingHeadV2LibraryDetail({
  runId,
  topic,
  onClose,
  onContinue,
  onDelete,
}: {
  runId: number;
  topic: string | null;
  onClose: () => void;
  /** Reopen this run in the v2 creator to keep working on it. */
  onContinue?: () => void;
  /** Permanently delete this run (server-side + cached grid). */
  onDelete?: () => void | Promise<void>;
}) {
  const [run, setRun] = useState<TalkingHeadRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const toast = useToast();
  const confirm = useConfirm();

  // Which collapsible sections are open (Script open by default).
  const [openScript, setOpenScript] = useState(true);
  const [openImages, setOpenImages] = useState(true);
  const [openVideos, setOpenVideos] = useState(true);

  // Escape, scroll lock, focus trap and focus restore in one place. The first
  // two were already here; the trap is the new part -- without it Tab walked
  // straight out of the dialog and into the page behind the overlay, which
  // makes a dialog modal only to a mouse.
  const panelRef = useRef<HTMLDivElement>(null);
  const { trapTab } = useModalChrome({ onClose, panelRef });

  useEffect(() => {
    let cancelled = false;
    api.yasProducer.talkingHeadV2
      .fetch(runId)
      .then((r) => {
        if (!cancelled) setRun(r);
      })
      .catch((e: unknown) => {
        if (!cancelled)
          setError(e instanceof Error ? e.message : "Failed to load");
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  // A reel finalized again keeps its last good file served (with its URL)
  // while a new one renders or after one failed; an older backend sends the
  // URL only when ready.
  const stitched = !!run?.composition_url;

  return createPortal(
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 110,
        background: "rgba(0,0,0,0.78)",
        backdropFilter: "blur(2px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "16px",
      }}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        onKeyDown={trapTab}
        onClick={(e) => e.stopPropagation()}
        className="relative bg-bg rounded-2xl shadow-2xl border border-border overflow-hidden flex flex-col"
        style={{ width: "min(620px, 100%)", maxHeight: "min(940px, 94vh)" }}
      >
        <header className="shrink-0 px-4 py-3 border-b border-border flex items-center gap-2.5 bg-bg">
          <span className="font-mono text-3xs px-1.5 py-0.5 rounded bg-violet-500/15 text-violet-600">
            Human Element Video
          </span>
          <h2 className="text-sm font-semibold text-fg flex-1 truncate">
            {run?.topic || topic || "(untitled)"}
          </h2>
          <button
            onClick={onClose}
            aria-label="Close"
            className="w-7 h-7 rounded-full bg-bg-muted hover:bg-border text-fg flex items-center justify-center text-base leading-none transition-colors"
          >
            ×
          </button>
        </header>

        <div className="flex-1 min-h-0 overflow-y-auto">
          {/* Loading / error */}
          {!run && !error && (
            <p className="text-sm text-muted text-center py-16">Loading…</p>
          )}
          {error && (
            <p className="text-sm text-danger text-center py-16 px-4">
              Couldn’t load this run: {error}
            </p>
          )}

          {run && (
            <>
              {/* 1. Final stitched video (or pending notice) */}
              <div className="bg-black flex items-center justify-center aspect-[9/16] max-h-[55vh]">
                {stitched ? (
                  <video
                    src={run.composition_url!}
                    controls
                    autoPlay
                    playsInline
                    className="w-full h-full object-contain bg-black"
                  />
                ) : (
                  <div className="text-center px-6 py-10 space-y-2">
                    <div className="text-2xl">⏳</div>
                    <p className="text-[13px] font-medium text-white/90">
                      {compositionLabel(run.composition_status)}
                    </p>
                    <p className="text-2xs text-white/55">
                      The final stitched video isn’t ready yet. Browse the
                      images and clips below.
                    </p>
                  </div>
                )}
              </div>

              <div className="px-4">
                {/* 2. Script */}
                <CollapsibleSection
                  label="Script"
                  summary={run.script?.hook || "—"}
                  expanded={openScript}
                  onToggle={() => setOpenScript((v) => !v)}
                  hasState={!!run.script}
                >
                  {run.script ? (
                    <ScriptBody run={run} />
                  ) : (
                    <p className="text-[12px] text-muted">No script.</p>
                  )}
                </CollapsibleSection>

                {/* 3. Images */}
                <CollapsibleSection
                  label="Images"
                  summary="Intro · Outro · Scenes"
                  expanded={openImages}
                  onToggle={() => setOpenImages((v) => !v)}
                  hasState
                >
                  <ImagesGroup run={run} />
                </CollapsibleSection>

                {/* 4. Videos */}
                <CollapsibleSection
                  label="Videos"
                  summary="Intro · Scenes · Outro"
                  expanded={openVideos}
                  onToggle={() => setOpenVideos((v) => !v)}
                  hasState
                >
                  <VideosGroup run={run} />
                </CollapsibleSection>
              </div>
            </>
          )}
        </div>

        <footer className="shrink-0 px-4 py-3 border-t border-border bg-bg-muted/50 flex items-center gap-3">
          <p className="text-2xs font-mono text-muted flex-1">#{runId}</p>
          <button onClick={onClose} className="btn btn-ghost text-xs">
            Close
          </button>
          {onContinue && (
            <button onClick={onContinue} className="btn btn-ghost text-xs">
              Continue editing
            </button>
          )}
          {onDelete && (
            <button
              onClick={async () => {
                if (deleting) return;
                if (
                  !(await confirm({
                    title: "Delete this Human Element Video permanently?",
                    message:
                      "Its videos, images, and final video will be removed from the library. This can’t be undone.",
                    confirmLabel: "Delete",
                    danger: true,
                  }))
                )
                  return;
                setDeleting(true);
                try {
                  await onDelete();
                  toast.success("Deleted");
                } catch (err: any) {
                  toast.error("Delete failed", err?.message || String(err));
                  setDeleting(false);
                }
              }}
              disabled={deleting}
              className="btn btn-ghost text-xs text-danger hover:bg-danger/10 hover:border-danger/30 disabled:opacity-50"
            >
              {deleting ? "Deleting…" : "Delete"}
            </button>
          )}
          {stitched && (
            <a
              href={api.yasProducer.talkingHeadV2.compositionFileUrl(runId, run?.composition_good_seq, true)}
              download
              className="btn btn-primary text-xs"
            >
              Download video
            </a>
          )}
        </footer>
      </div>
    </div>,
    document.body,
  );
}

// ---------------------------------------------------------------------------

function ScriptBody({ run }: { run: TalkingHeadRun }) {
  const s = run.script!;
  const perScene = s.scenes && s.scenes.length > 0 ? s.scenes : null;
  return (
    <div className="space-y-2.5 text-[12px] text-fg/85">
      <Field label="Hook" value={s.hook} />
      {perScene ? (
        <div className="space-y-1.5">
          <p className="eyebrow">Dialogue</p>
          {perScene.map((sc, i) => (
            <p key={i} className="text-fg/85">
              <span className="font-mono text-3xs text-muted mr-1.5">
                {i + 1}.
              </span>
              {sc.dialogue || "—"}
            </p>
          ))}
        </div>
      ) : (
        <Field label="Dialogue" value={s.dialogue} />
      )}
      {(s.intro_headline || s.intro_subtitle) && (
        <Field
          label="Intro card"
          value={[s.intro_headline, s.intro_subtitle]
            .filter(Boolean)
            .join(" — ")}
        />
      )}
      {(s.outro_headline || s.outro_subtitle) && (
        <Field
          label="Outro card"
          value={[s.outro_headline, s.outro_subtitle]
            .filter(Boolean)
            .join(" — ")}
        />
      )}
      <div className="flex flex-wrap gap-x-5 gap-y-1.5 pt-1">
        {s.style && <Inline label="Style" value={s.style} />}
        {s.mood && <Inline label="Mood" value={s.mood} />}
      </div>
      {s.palette && s.palette.length > 0 && (
        <div className="flex items-center gap-1.5 pt-0.5">
          <span className="eyebrow">Palette</span>
          {s.palette.map((c, i) => (
            <span
              key={i}
              title={c}
              className="w-4 h-4 rounded-full border border-border"
              style={{ background: c }}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function Field({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="space-y-0.5">
      <p className="eyebrow">{label}</p>
      <p className="text-fg/85 whitespace-pre-wrap">{value || "—"}</p>
    </div>
  );
}

function Inline({ label, value }: { label: string; value: string }) {
  return (
    <span className="text-[12px]">
      <span className="eyebrow mr-1">{label}</span>
      <span className="text-fg/85">{value}</span>
    </span>
  );
}

// ----- Images group --------------------------------------------------------

type ImgTab = "intro" | "outro" | "scenes";

function ImagesGroup({ run }: { run: TalkingHeadRun }) {
  const [tab, setTab] = useState<ImgTab>("scenes");
  const [sceneIdx, setSceneIdx] = useState(0);

  const sceneCount = run.scene_count || run.scenes?.length || 1;

  let url: string | null = null;
  let err: string | null | undefined = null;
  if (tab === "intro") {
    url = run.intro?.url ?? null;
    err = run.intro_error ?? run.intro?.error;
  } else if (tab === "outro") {
    url = run.outro?.url ?? null;
    err = run.outro_error ?? run.outro?.error;
  } else {
    const sc = run.scenes?.[sceneIdx];
    url = sc?.keyframe_url ?? (sceneIdx === 0 ? run.main?.url ?? null : null);
    err = sc?.error ?? (sceneIdx === 0 ? run.main_error : null);
  }

  return (
    <div className="space-y-2">
      <TabBar
        tabs={[
          ["intro", "Intro"],
          ["outro", "Outro"],
          ["scenes", "Scenes"],
        ]}
        active={tab}
        onChange={(t) => setTab(t as ImgTab)}
      />
      {tab === "scenes" && sceneCount > 1 && (
        <SceneChips count={sceneCount} active={sceneIdx} onChange={setSceneIdx} />
      )}
      <MediaFrame
        kind="image"
        url={url}
        error={err}
        emptyLabel="no image"
      />
    </div>
  );
}

// ----- Videos group --------------------------------------------------------

type VidTab = "intro" | "scenes" | "outro";

function VideosGroup({ run }: { run: TalkingHeadRun }) {
  const [tab, setTab] = useState<VidTab>("scenes");
  const [sceneIdx, setSceneIdx] = useState(0);

  const sceneCount = run.scene_count || run.scenes?.length || 1;

  let anim = null;
  if (tab === "intro") anim = run.animations?.intro ?? null;
  else if (tab === "outro") anim = run.animations?.outro ?? null;
  else {
    const sc = run.scenes?.[sceneIdx];
    anim = sc?.animation ?? (sceneIdx === 0 ? run.animations?.main ?? null : null);
  }

  const url = anim?.url ?? null;
  const subState =
    !url && anim
      ? anim.status === "generating"
        ? "generating…"
        : anim.status === "failed"
          ? anim.error || "render failed"
          : null
      : null;

  return (
    <div className="space-y-2">
      <TabBar
        tabs={[
          ["intro", "Intro"],
          ["scenes", "Scenes"],
          ["outro", "Outro"],
        ]}
        active={tab}
        onChange={(t) => setTab(t as VidTab)}
      />
      {tab === "scenes" && sceneCount > 1 && (
        <SceneChips count={sceneCount} active={sceneIdx} onChange={setSceneIdx} />
      )}
      <MediaFrame
        kind="video"
        url={url}
        error={subState}
        emptyLabel="no video available"
      />
    </div>
  );
}

// ----- shared bits ---------------------------------------------------------

function TabBar({
  tabs,
  active,
  onChange,
}: {
  tabs: [string, string][];
  active: string;
  onChange: (t: string) => void;
}) {
  return (
    <div className="inline-flex rounded-lg border border-border bg-bg-subtle p-0.5 gap-0.5">
      {tabs.map(([key, label]) => (
        <button
          key={key}
          type="button"
          onClick={() => onChange(key)}
          className={[
            "px-2.5 py-1 rounded-md text-2xs font-medium transition-colors",
            active === key
              ? "bg-bg text-fg shadow-sm"
              : "text-muted hover:text-fg",
          ].join(" ")}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function SceneChips({
  count,
  active,
  onChange,
}: {
  count: number;
  active: number;
  onChange: (i: number) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1">
      {Array.from({ length: count }, (_, i) => (
        <button
          key={i}
          type="button"
          onClick={() => onChange(i)}
          className={[
            "w-6 h-6 rounded-md text-2xs font-mono transition-colors",
            active === i
              ? "bg-accent/15 text-fg border border-accent"
              : "bg-bg-subtle text-muted border border-border hover:bg-bg-muted",
          ].join(" ")}
        >
          {i + 1}
        </button>
      ))}
    </div>
  );
}

function MediaFrame({
  kind,
  url,
  error,
  emptyLabel,
}: {
  kind: "image" | "video";
  url: string | null;
  error?: string | null;
  emptyLabel: string;
}) {
  const [loadError, setLoadError] = useState(false);
  // Reset the load-error flag whenever the source changes.
  useEffect(() => {
    setLoadError(false);
  }, [url]);

  return (
    <div className="aspect-[9/16] max-h-[42vh] mx-auto bg-black/90 rounded-lg overflow-hidden flex items-center justify-center">
      {url && !loadError ? (
        kind === "video" ? (
          <video
            src={url}
            controls
            playsInline
            onError={() => setLoadError(true)}
            className="w-full h-full object-contain bg-black"
          />
        ) : (
          <img
            src={url}
            alt=""
            onError={() => setLoadError(true)}
            className="w-full h-full object-contain bg-black"
          />
        )
      ) : (
        <div className="text-center px-4">
          <p className="text-2xs text-white/55">
            {loadError ? "file unavailable" : emptyLabel}
          </p>
          {error && !loadError && (
            <p className="text-3xs text-danger/80 mt-1">{error}</p>
          )}
        </div>
      )}
    </div>
  );
}
