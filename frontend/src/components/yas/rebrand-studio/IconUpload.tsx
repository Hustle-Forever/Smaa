// Rebrand studio — "Upload your own": a dropped or chosen SVG / PNG / WebP /
// JPEG made into one of your team's icons. Free: the server cleans the file
// into a one-colour glyph (its main shapes, strokes at the library's bold
// weight) and shows it exactly as the video will stamp it — the circle, and
// the chip with its name — pictures the server drew with the run's own
// sprite code. The file itself is shown only as an <img> (an SVG is never
// put into the page), and only the cleaned PNG is ever served back.
//
// Name it (the chip's words, and how the team finds it), tag it, and "Save
// to team icons": the icon lands in "Your team's icons" on every machine
// and is added when there is room. The same file saved again is the same
// icon (the server says so before you save).

import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { X } from "lucide-react";

import {
  harnessApi,
  type HarnessIconGlyph,
  type HarnessIconUploadLimits,
  type HarnessIconUploadMode,
  type HarnessIconUploadPreview,
} from "../../../lib/rebrandHarnessApi";
import { rebrandErrorDetail } from "../../../lib/rebrandApi";
import { ADDED_MAX } from "../../../lib/rebrandIcons";
import { ChoiceChips } from "../../ui/ChoiceChips";
import { Spinner } from "../../ui/Spinner";
import { COPY } from "./studioCopy";

/** The server's own limits, until the catalogue says otherwise. */
const LIMITS: HarnessIconUploadLimits = {
  max_bytes: 2 * 1024 * 1024,
  svg_max_bytes: 512 * 1024,
  kinds: ["svg", "png", "webp", "jpeg"],
  name_max: 40,
  tags_max: 12,
  tag_max: 24,
};

/** What the file input offers. */
export const UPLOAD_ACCEPT = ".svg,.png,.webp,.jpg,.jpeg,image/svg+xml,image/png,image/webp,image/jpeg";

/** A file's kind from its type or name ("svg" | "png" | "webp" | "jpeg"), or
 *  null. The server reads the bytes themselves; this only saves a trip. */
export function uploadKind(file: File): string | null {
  const t = (file.type || "").toLowerCase();
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  if (t === "image/svg+xml" || ext === "svg") return "svg";
  if (t === "image/png" || ext === "png") return "png";
  if (t === "image/webp" || ext === "webp") return "webp";
  if (t === "image/jpeg" || ext === "jpg" || ext === "jpeg") return "jpeg";
  return null;
}

/** Why the studio won't send this file (null: it will). */
export function uploadRefusal(file: File, limits: HarnessIconUploadLimits | null | undefined): string | null {
  const l = limits ?? LIMITS;
  const kind = uploadKind(file);
  if (!kind || !l.kinds.includes(kind)) return COPY.icons.uploadWrongKind;
  const max = kind === "svg" ? l.svg_max_bytes : l.max_bytes;
  if (file.size > max) return COPY.icons.uploadTooBig(max >= 1024 * 1024 ? `${Math.round(max / (1024 * 1024))} MB` : `${Math.round(max / 1024)} KB`);
  return null;
}

/** Words typed as tags: "a, b" -> ["a", "b"]. */
export function tagsOf(text: string): string[] {
  return text
    .split(/[,;\n]+/)
    .map((t) => t.trim())
    .filter(Boolean);
}

/** Tags as the server saves them (icon_import.clean_tags: each as
 *  icon_catalog.name_key keeps a name — accents dropped, lower case, "-" and
 *  "_" read as spaces, other punctuation gone, "/" kept — at most 24
 *  characters, no repeats, at most 12): what a typed list is compared with. */
export function savedTags(tags: string[]): string[] {
  const out: string[] = [];
  for (const t of tags) {
    const k = t
      .normalize("NFKD")
      .replace(/\p{M}+/gu, "")
      .toLowerCase()
      .replace(/[-_]+/g, " ")
      .replace(/[^\p{L}\p{N}_\s/']+/gu, " ")
      .replace(/'/g, "")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 24)
      .trim();
    if (k && !out.includes(k)) out.push(k);
  }
  return out.slice(0, 12);
}

type Reading =
  | { kind: "reading" }
  | { kind: "ready"; got: HarnessIconUploadPreview; mode: HarnessIconUploadMode; words: string }
  | { kind: "failed"; message: string };

type Saved = { name: string; added: boolean; full: boolean } | null;

export function IconUpload({
  file,
  limits,
  full,
  onUse,
  onTeamChanged,
  onClose,
  onAnother,
}: {
  file: File;
  limits: HarnessIconUploadLimits | null | undefined;
  /** The user has added all a scene takes (5): it is saved, not added. */
  full: boolean;
  /** Add it (or, changing an icon, put it in that one's place). */
  onUse: (glyph: HarnessIconGlyph, label: string) => void;
  /** The team's icons changed (it was saved). */
  onTeamChanged: () => void;
  onClose: () => void;
  /** Choose another file. */
  onAnother: () => void;
}) {
  const l = limits ?? LIMITS;
  const refused = useMemo(() => uploadRefusal(file, l), [file, l]);
  // The file itself, as an image (made and let go in one effect: StrictMode
  // runs it twice).
  const [original, setOriginal] = useState<string | null>(null);
  useEffect(() => {
    const url = URL.createObjectURL(file);
    setOriginal(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  const [mode, setMode] = useState<HarnessIconUploadMode>("auto");
  const [name, setName] = useState("");
  // Typed by the user (or filled from the first preview): kept from then on.
  const named = useRef(false);
  const [words, setWords] = useState("");
  const [tags, setTags] = useState("");
  const [reading, setReading] = useState<Reading>({ kind: "reading" });
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [nameError, setNameError] = useState(false);
  const [saved, setSaved] = useState<Saved>(null);
  const readyRef = useRef<Reading>(reading);
  readyRef.current = reading;

  // The chip shows the name: asked for again (free) once typing pauses.
  useEffect(() => {
    const t = window.setTimeout(() => setWords(name.trim()), 450);
    return () => window.clearTimeout(t);
  }, [name]);

  useEffect(() => {
    if (refused) {
      setReading({ kind: "failed", message: refused });
      return;
    }
    const now = readyRef.current;
    if (now.kind === "ready" && now.mode === mode && (words || now.got.name_guess || "Icon") === now.words) return;
    const ctl = new AbortController();
    setBusy(true);
    harnessApi
      .iconUploadPreview(file, { mode, label: words, signal: ctl.signal })
      .then((got) => {
        setReading({ kind: "ready", got, mode, words: words || got.name_guess || "Icon" });
        if (!named.current) {
          named.current = true;
          setName(got.duplicate_of?.name || got.name_guess);
        }
      })
      .catch((err) => {
        if (ctl.signal.aborted) return;
        // A mode the file can't take keeps the picture it had.
        if (readyRef.current.kind === "ready" && mode !== "auto") {
          setSaveError(rebrandErrorDetail(err));
          setMode(readyRef.current.mode);
          return;
        }
        setReading({ kind: "failed", message: rebrandErrorDetail(err) });
      })
      .finally(() => !ctl.signal.aborted && setBusy(false));
    return () => ctl.abort();
  }, [file, mode, words, refused]);

  // Saved in the mode of the picture on show (a plate choice still on its
  // way never changes what Save stores): the preview's bytes ARE the icon.
  const save = async (got: HarnessIconUploadPreview, shown: HarnessIconUploadMode) => {
    const n = name.trim();
    if (!n) {
      setNameError(true);
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const row = await harnessApi.iconUpload(file, { name: n, tags, mode: shown });
      onTeamChanged();
      const glyph: HarnessIconGlyph = { source: "custom", id: row.id || got.id };
      if (!full) onUse(glyph, row.name || n);
      setSaved({ name: row.name || n, added: !full, full });
    } catch (err) {
      setSaveError(rebrandErrorDetail(err));
    } finally {
      setSaving(false);
    }
  };

  // Esc in a field closes the card, not the picker.
  const escCloses = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key !== "Escape") return;
    e.stopPropagation();
    e.nativeEvent.stopImmediatePropagation();
    onClose();
  };

  return (
    <section
      aria-label={COPY.icons.uploadTitle}
      aria-live="polite"
      className="space-y-2 rounded-xl border border-border bg-bg-muted/40 p-2.5"
      data-icon-upload
    >
      <div className="flex items-center gap-2">
        <p className="min-w-0 flex-1 truncate text-xs font-medium text-fg" title={file.name}>
          {file.name}
        </p>
        {busy && reading.kind === "ready" && <Spinner size="xs" tone="accent" />}
        <button
          type="button"
          onClick={onClose}
          aria-label={COPY.icons.close}
          title={COPY.icons.close}
          className="h-6 w-6 shrink-0 rounded flex items-center justify-center text-muted hover:text-fg hover:bg-bg-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        >
          <X className="h-3.5 w-3.5" aria-hidden />
        </button>
      </div>

      {reading.kind === "reading" && (
        <p className="flex items-center gap-2 text-xs text-fg">
          <Spinner size="sm" tone="accent" /> {COPY.icons.uploadReading(file.name)}
        </p>
      )}

      {reading.kind === "failed" && (
        <div className="space-y-1.5">
          <p className="rounded-lg border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-2xs text-danger">
            {COPY.icons.uploadFailed} {reading.message}
          </p>
          <p className="text-3xs text-muted">{COPY.icons.uploadKinds(Math.round(l.max_bytes / (1024 * 1024)), Math.round(l.svg_max_bytes / 1024))}</p>
          <button type="button" onClick={onAnother} className="btn btn-xs btn-secondary">
            {COPY.icons.uploadAnother}
          </button>
        </div>
      )}

      {reading.kind === "ready" && (
        <Ready
          got={reading.got}
          original={original}
          mode={mode}
          onMode={(m) => {
            setSaveError(null);
            setMode(m);
          }}
          name={name}
          onName={(v) => {
            setName(v);
            named.current = true;
            setNameError(false);
          }}
          nameError={nameError}
          tags={tags}
          onTags={setTags}
          limits={l}
          full={full}
          saving={saving}
          saved={saved}
          saveError={saveError}
          onSave={() => void save(reading.got, reading.mode)}
          onClose={onClose}
          onKeyDown={escCloses}
        />
      )}
    </section>
  );
}

function Ready({
  got,
  original,
  mode,
  onMode,
  name,
  onName,
  nameError,
  tags,
  onTags,
  limits,
  full,
  saving,
  saved,
  saveError,
  onSave,
  onClose,
  onKeyDown,
}: {
  got: HarnessIconUploadPreview;
  original: string | null;
  mode: HarnessIconUploadMode;
  onMode: (m: HarnessIconUploadMode) => void;
  name: string;
  onName: (v: string) => void;
  nameError: boolean;
  tags: string;
  onTags: (v: string) => void;
  limits: HarnessIconUploadLimits;
  full: boolean;
  saving: boolean;
  saved: Saved;
  saveError: string | null;
  onSave: () => void;
  onClose: () => void;
  onKeyDown: (e: KeyboardEvent<HTMLElement>) => void;
}) {
  const report = COPY.icons.uploadReport(got.report);
  const dup = got.duplicate_of;
  const plate = got.report.plate;
  const tagCount = tagsOf(tags).length;

  if (saved) {
    return (
      <div className="space-y-2">
        <p className="text-xs text-fg">
          {saved.added
            ? COPY.icons.uploadSavedAdded(saved.name)
            : saved.full
              ? COPY.icons.uploadSavedFull(saved.name, ADDED_MAX)
              : COPY.icons.uploadSaved(saved.name)}
        </p>
        <button type="button" onClick={onClose} className="btn btn-xs btn-secondary">
          {COPY.icons.close}
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-2.5">
      <div className="flex flex-wrap items-end gap-3" title={COPY.icons.previewTitle}>
        <figure className="flex flex-col items-center gap-1">
          {/* The file as an image only: an SVG is never put into the page. */}
          {original ? (
            <img src={original} alt="" className="h-14 w-14 rounded border border-border bg-bg object-contain" />
          ) : (
            <span className="h-14 w-14 rounded border border-border bg-bg" />
          )}
          <figcaption className="text-3xs text-muted">{COPY.icons.uploadYours}</figcaption>
        </figure>
        <figure className="flex flex-col items-center gap-1">
          {got.preview.circle ? (
            <img src={got.preview.circle} alt="" className="h-14 w-auto" />
          ) : (
            <img src={got.png_url} alt="" className="h-14 w-14 object-contain" />
          )}
          <figcaption className="text-3xs text-muted">{COPY.icons.previewCircle}</figcaption>
        </figure>
        {got.preview.chip && (
          <figure className="flex min-w-0 flex-col items-center gap-1">
            <img src={got.preview.chip} alt="" className="h-11 w-auto max-w-full" />
            <figcaption className="text-3xs text-muted">{COPY.icons.previewChip}</figcaption>
          </figure>
        )}
      </div>
      {report && <p className="text-2xs text-muted">{report}</p>}

      {plate != null && (
        <div className="space-y-1" title={COPY.icons.uploadPlateTitle}>
          <p className="text-2xs font-medium text-fg">{COPY.icons.uploadPlate}</p>
          <ChoiceChips
            label={COPY.icons.uploadPlate}
            options={[
              { value: "shape", label: COPY.icons.uploadPlateKeep },
              { value: "marks", label: COPY.icons.uploadPlateMarks },
            ]}
            value={plate === "dropped" ? "marks" : "shape"}
            onChange={(v) => {
              const want: HarnessIconUploadMode = v === "marks" ? "marks" : "shape";
              if (want !== mode) onMode(want);
            }}
            disabled={saving}
          />
        </div>
      )}

      {dup && <p className="text-2xs text-fg">{COPY.icons.uploadDuplicate(dup.name, dup.hidden)}</p>}

      <label className="block space-y-1">
        <span className="text-2xs font-medium text-fg">{COPY.icons.uploadName}</span>
        <input
          type="text"
          value={name}
          maxLength={limits.name_max}
          placeholder={COPY.icons.uploadNamePlaceholder}
          aria-invalid={nameError || undefined}
          onChange={(e) => onName(e.target.value)}
          onKeyDown={(e) => {
            onKeyDown(e);
            if (e.key === "Enter") {
              e.preventDefault();
              onSave();
            }
          }}
          disabled={saving || (!!dup && !dup.hidden)}
          className={`w-full rounded border bg-bg px-2 py-1 text-xs text-fg focus:border-accent focus:outline-none disabled:opacity-60 ${
            nameError ? "border-danger" : "border-border"
          }`}
        />
        <span className={`block text-3xs ${nameError ? "text-danger" : "text-muted"}`}>
          {nameError ? COPY.icons.uploadNameNeeded : COPY.icons.uploadNameHint}
        </span>
      </label>
      {!dup && (
        <label className="block space-y-1">
          <span className="text-2xs font-medium text-fg">{COPY.icons.uploadTags}</span>
          <input
            type="text"
            value={tags}
            placeholder={COPY.icons.uploadTagsPlaceholder}
            onChange={(e) => onTags(e.target.value)}
            onKeyDown={onKeyDown}
            disabled={saving}
            aria-invalid={tagCount > limits.tags_max || undefined}
            className="w-full rounded border border-border bg-bg px-2 py-1 text-xs text-fg focus:border-accent focus:outline-none disabled:opacity-60"
          />
        </label>
      )}

      {saveError && (
        <p className="rounded-lg border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-2xs text-danger">
          {COPY.icons.uploadSaveFailed} {saveError}
        </p>
      )}
      <p className="text-3xs text-muted">{COPY.icons.uploadPrivacy}</p>
      <div className="flex flex-wrap items-center gap-1.5">
        <button type="button" onClick={onSave} disabled={saving} className="btn btn-primary btn-sm">
          {saving ? COPY.icons.uploadSaving : dup && !dup.hidden ? COPY.icons.uploadSaveDuplicate : COPY.icons.uploadSave}
        </button>
        <button type="button" onClick={onClose} disabled={saving} className="btn btn-xs btn-ghost">
          {COPY.cancel}
        </button>
      </div>
    </div>
  );
}
