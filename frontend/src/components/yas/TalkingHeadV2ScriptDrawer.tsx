/**
 * Right-edge slide-in drawer that renders the GPT-generated script JSON
 * as readable key/value rows. Replaces the inline collapsible script
 * panel that used to live in Step 3 — moving it to a drawer reclaims
 * ~220px of vertical space from the right canvas.
 *
 * Portaled to document.body to escape the page-transition transform
 * (same pattern as AssetPickerModal — see CLAUDE.md modal-pattern note).
 */
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Pencil } from "lucide-react";

import { api } from "../../lib/api";
import { cardTextLocked } from "../../lib/thBookends";
import { framingLabel } from "../../lib/thFraming";
import type {
  TalkingHeadRun,
  TalkingHeadScript,
  TalkingHeadV2EditRequest,
} from "../../lib/types";

interface Props {
  open: boolean;
  script: TalkingHeadScript | null;
  onClose: () => void;
}

/**
 * One script field. Read-only fields (no `onSave`) render as plain rows.
 * Editable fields reveal a pencil on hover; clicking it (or the text) opens an
 * inline input/textarea that saves on Enter or blur, cancels on Escape.
 */
function EditableField({
  label,
  value,
  onSave,
  multiline = false,
  mono = false,
  placeholder,
}: {
  label: string;
  value: string;
  onSave?: (next: string) => Promise<void>;
  multiline?: boolean;
  mono?: boolean;
  placeholder?: string;
}) {
  const canEdit = !!onSave;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!editing) setDraft(value);
  }, [value, editing]);

  // Hide an empty value only when it's read-only (nothing to add).
  if (!value && !canEdit) return null;

  async function commit() {
    if (saving) return;
    const next = draft.trim();
    const cur = (value || "").trim();
    if (!canEdit || next === cur) {
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      await onSave!(next);
    } catch {
      setDraft(value); // revert on failure
    } finally {
      setSaving(false);
      setEditing(false);
    }
  }

  if (editing) {
    const cls =
      "w-full rounded-md border border-accent/70 bg-bg px-2 py-1 text-[12.5px] leading-snug text-fg outline-none focus:ring-1 focus:ring-accent disabled:opacity-60";
    return (
      <div className="grid grid-cols-[88px_1fr] gap-3 py-1.5 border-b border-border/60">
        <span className="eyebrow mt-0.5">{label}</span>
        <div className="flex flex-col gap-1 min-w-0">
          {multiline ? (
            <textarea
              autoFocus
              rows={2}
              value={draft}
              disabled={saving}
              onFocus={(e) => e.currentTarget.select()}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={commit}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  setDraft(value);
                  e.currentTarget.blur();
                } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  e.currentTarget.blur();
                }
              }}
              className={cls + " resize-y"}
            />
          ) : (
            <input
              autoFocus
              value={draft}
              disabled={saving}
              onFocus={(e) => e.currentTarget.select()}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={commit}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  setDraft(value);
                  e.currentTarget.blur();
                } else if (e.key === "Enter") {
                  e.preventDefault();
                  e.currentTarget.blur();
                }
              }}
              className={cls}
            />
          )}
          {saving && <span className="text-3xs text-muted">Saving…</span>}
        </div>
      </div>
    );
  }

  return (
    <div className="group grid grid-cols-[88px_1fr] gap-3 py-1.5 border-b border-border/60">
      <span className="eyebrow mt-0.5">{label}</span>
      <div className="flex items-start gap-1.5 min-w-0">
        <p
          onClick={canEdit ? () => setEditing(true) : undefined}
          className={[
            "text-[12.5px] leading-snug flex-1 min-w-0 whitespace-pre-wrap break-words",
            mono ? "font-mono text-2xs" : "",
            value ? "text-fg" : "text-muted italic",
            canEdit ? "cursor-text" : "",
          ].join(" ")}
        >
          {value || (canEdit ? placeholder || "Add…" : "")}
        </p>
        {canEdit && (
          <button
            type="button"
            onClick={() => setEditing(true)}
            title={`Edit ${label.toLowerCase()}`}
            className="shrink-0 mt-0.5 opacity-0 group-hover:opacity-100 transition-opacity text-muted hover:text-accent"
          >
            <Pencil className="w-3.5 h-3.5" />
          </button>
        )}
      </div>
    </div>
  );
}

/**
 * The script rendered as readable rows, with the FULL per-scene breakdown.
 * - Read-only mode (drawer): pass `script`.
 * - Editable mode (Look stage): pass `run` + `onRunUpdate`. Dialogue, hook and
 *   the intro/outro card wording become inline-editable; structural/cinematic
 *   fields (subject/camera/lighting/…) stay read-only. Scenes are read from
 *   `run.scenes` (the edit-current array), so edits reflect immediately.
 */
export function ScriptReview({
  script,
  run,
  onRunUpdate,
}: {
  script?: TalkingHeadScript | null;
  run?: TalkingHeadRun | null;
  onRunUpdate?: (fresh: TalkingHeadRun) => void;
}) {
  const s = run?.script ?? script ?? null;
  const editable = !!(run && onRunUpdate);
  const introLocked = cardTextLocked(run, "intro");
  const outroLocked = cardTextLocked(run, "outro");
  if (!s) return <p className="text-sm text-muted">No script yet.</p>;

  // Unified scene list: prefer run.scenes (kept current by edits); fall back to
  // script.scenes (read-only drawer) or a single synthesized scene.
  const sceneList: { dialogue: string; hook: string }[] =
    run?.scenes && run.scenes.length
      ? run.scenes.map((sc) => ({ dialogue: sc.dialogue || "", hook: sc.hook || "" }))
      : s.scenes && s.scenes.length
        ? s.scenes.map((sc) => ({ dialogue: sc.dialogue || "", hook: sc.hook || "" }))
        : [{ dialogue: s.dialogue || "", hook: s.hook || "" }];

  async function save(body: TalkingHeadV2EditRequest) {
    if (!run || !onRunUpdate) return;
    const fresh = await api.yasProducer.talkingHeadV2.editRun(run.id, body);
    onRunUpdate(fresh);
  }

  return (
    <div className="space-y-0.5">
      {/* Per-scene spoken content (editable) */}
      <div className="space-y-2 pb-1.5">
        {sceneList.map((sc, i) => (
          <div
            key={i}
            className="rounded-lg border border-border/60 bg-bg-subtle/30 px-2 pt-1 pb-0.5"
          >
            <p className="eyebrow mb-0.5">Scene {i + 1}</p>
            <EditableField
              label="Hook"
              value={sc.hook}
              placeholder="Opening line…"
              onSave={
                editable
                  ? async (v) => {
                      const hooks = sceneList.map((_, idx) => (idx === i ? v : null));
                      await save({ scene_hooks: hooks });
                    }
                  : undefined
              }
            />
            <EditableField
              label="Dialogue"
              value={sc.dialogue}
              multiline
              placeholder="What the speaker says…"
              onSave={
                editable
                  ? async (v) => {
                      const dlgs = sceneList.map((x, idx) => (idx === i ? v : x.dialogue));
                      await save({ scene_dialogues: dlgs });
                    }
                  : undefined
              }
            />
          </div>
        ))}
      </div>

      {/* Structural / cinematic — read-only */}
      <EditableField label="Subject" value={s.subject} />
      <EditableField label="Action" value={s.action} />
      {/* How close the camera is (set on the Look step); Camera is its movement. */}
      <EditableField label="Framing" value={framingLabel(run?.framing)} />
      <EditableField label="Camera" value={s.camera} />
      <EditableField label="Lighting" value={s.lighting} />
      <EditableField label="Palette" value={(s.palette || []).join(", ")} mono />
      <EditableField label="Style" value={s.style} />
      <EditableField label="Mood" value={s.mood} />
      <EditableField label="Language" value={s.dialogue_language} mono />

      {/* Intro / outro card wording (editable). A card kept with its own
          words ("Keep card text") sets them: read-only here. */}
      <EditableField
        label="Intro"
        value={s.intro_headline}
        placeholder="Intro headline"
        onSave={editable && !introLocked ? async (v) => save({ intro_headline: v }) : undefined}
      />
      <EditableField
        label="Intro sub"
        value={s.intro_subtitle}
        placeholder="Intro subtitle (optional)"
        onSave={editable && !introLocked ? async (v) => save({ intro_subtitle: v }) : undefined}
      />
      <EditableField
        label="Outro"
        value={s.outro_headline}
        placeholder="Outro call-to-action"
        onSave={editable && !outroLocked ? async (v) => save({ outro_headline: v }) : undefined}
      />
      <EditableField
        label="Outro sub"
        value={s.outro_subtitle}
        placeholder="Outro subtitle (optional)"
        onSave={editable && !outroLocked ? async (v) => save({ outro_subtitle: v }) : undefined}
      />
      {editable && (introLocked || outroLocked) && (
        <p className="text-3xs text-muted leading-snug pt-1">
          {introLocked && outroLocked
            ? "The intro and outro cards keep their own words"
            : `The ${introLocked ? "intro" : "outro"} card keeps its own words`}{" "}
          (Keep card text on the Look step), so the script says them.
        </p>
      )}
    </div>
  );
}

export function TalkingHeadV2ScriptDrawer({ open, script, onClose }: Props) {
  // ESC to close + body scroll lock while open
  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prev;
      window.removeEventListener("keydown", onKey);
    };
  }, [open, onClose]);

  if (!open) return null;

  return createPortal(
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 110,
        background: "rgba(0,0,0,0.6)",
        backdropFilter: "blur(2px)",
        display: "flex",
        justifyContent: "flex-end",
      }}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Script details"
    >
      <aside
        onClick={(e) => e.stopPropagation()}
        className="relative bg-bg border-l border-border shadow-2xl overflow-hidden flex flex-col h-full"
        style={{ width: "min(480px, 100%)" }}
      >
        <header className="shrink-0 px-4 py-3 border-b border-border flex items-center gap-2.5">
          <h2 className="text-sm font-semibold text-fg flex-1">Script</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="w-7 h-7 rounded-full bg-bg-muted hover:bg-border text-fg flex items-center justify-center text-base leading-none transition-colors"
          >
            ×
          </button>
        </header>

        <div className="flex-1 min-h-0 overflow-y-auto p-4">
          <ScriptReview script={script} />
        </div>
      </aside>
    </div>,
    document.body,
  );
}
