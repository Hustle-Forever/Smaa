// Rebrand studio — rate a version: 1-5 and a note if you like, from its ⋯
// menu, under the stage.
//
// The classic panel's rating (POST /experiments/{id}/rate), for a studio
// version: the stars are kept on its one arm (the "omni" key, whoever made
// it) and saved with it. Nothing feeds a brief any more: one made in house
// had none, and Omni, whose brief learned from them, is switched off for new
// work (the server then saves the rating and embeds nothing). Whatever
// it was rated with before (a winner, another arm's stars) is sent back as
// it was, so a rating here never wipes one made in the classic panel.
//
// Saved, the row says so for a moment and closes; a failure is said in the
// row, which stays open (a toast would sit under the studio's backdrop).

import { useEffect, useRef, useState } from "react";
import { Check } from "lucide-react";

import { rebrandApi, rebrandErrorDetail, type RebrandExperiment } from "../../../lib/rebrandApi";
import type { HarnessVersion } from "../../../lib/rebrandHarnessApi";
import { COPY } from "./studioCopy";

const SAVED_MS = 2500;

export function Stars({
  value,
  onChange,
  disabled = false,
}: {
  value: number | null;
  onChange: (v: number) => void;
  disabled?: boolean;
}) {
  return (
    <div className="inline-flex gap-0.5" role="radiogroup" aria-label={COPY.rateStars}>
      {[1, 2, 3, 4, 5].map((n) => (
        <button
          key={n}
          type="button"
          role="radio"
          aria-checked={value === n}
          aria-label={COPY.starAria(n)}
          disabled={disabled}
          onClick={() => onChange(n)}
          className={`rounded text-lg leading-none transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:opacity-50 ${
            value != null && n <= value ? "text-warning" : "text-border-strong hover:text-warning/60"
          }`}
        >
          ★
        </button>
      ))}
    </div>
  );
}

export function RateVersion({
  version,
  no,
  onSaved,
  onClose,
}: {
  version: HarnessVersion;
  no: number;
  /** The version as the server has it now, rating and all. */
  onSaved: (row: RebrandExperiment) => void;
  onClose: () => void;
}) {
  const [stars, setStars] = useState<number | null>(version.rating?.omni_stars ?? null);
  const [note, setNote] = useState(version.rating?.notes ?? "");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!saved) return;
    const t = window.setTimeout(() => onCloseRef.current(), SAVED_MS);
    return () => window.clearTimeout(t);
  }, [saved]);

  const save = async () => {
    if (!stars || saving) return;
    setSaving(true);
    setError(null);
    try {
      const row = await rebrandApi.rate(version.id, {
        winner: version.winner ?? null,
        omni_stars: stars,
        veo_stars: version.rating?.veo_stars ?? null,
        brand_stars: version.rating?.brand_stars ?? null,
        notes: note.trim(),
      });
      onSaved(row);
      setSaved(true);
    } catch (e) {
      setError(`${COPY.rateFailed}: ${rebrandErrorDetail(e)}`);
    } finally {
      setSaving(false);
    }
  };

  if (saved) {
    return (
      <p role="status" className="inline-flex items-center gap-1.5 text-xs text-fg motion-safe:animate-fadeIn">
        <Check className="h-3.5 w-3.5 text-accent" strokeWidth={3} aria-hidden />
        <span className="font-medium">{COPY.rated(no)}</span>
        <span className="text-warning" aria-hidden>
          {"★".repeat(stars ?? 0)}
        </span>
        <span className="text-muted">· {COPY.ratedHint}</span>
      </p>
    );
  }

  return (
    <form
      aria-label={COPY.rateTitle(no)}
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
      className="flex w-full max-w-xl flex-wrap items-center justify-center gap-x-2 gap-y-1.5 motion-safe:animate-fadeIn"
    >
      <span className="text-2xs font-semibold text-fg">{COPY.rateTitle(no)}</span>
      <Stars value={stars} onChange={setStars} disabled={saving} />
      <input
        type="text"
        value={note}
        maxLength={1000}
        onChange={(e) => setNote(e.target.value)}
        placeholder={COPY.ratePlaceholder}
        aria-label={COPY.ratePlaceholder}
        disabled={saving}
        className="min-w-[12rem] flex-1 rounded-lg border border-border bg-bg px-2.5 py-1 text-xs text-fg placeholder:text-muted/70 focus:border-accent focus:outline-none"
      />
      <button type="submit" className="btn btn-xs btn-secondary" disabled={!stars || saving}>
        {saving ? COPY.savingRating : COPY.saveRating}
      </button>
      <button type="button" className="btn btn-xs btn-ghost" onClick={onClose} disabled={saving}>
        {COPY.cancel}
      </button>
      {error && (
        <p role="alert" className="w-full text-center text-2xs text-danger">
          {error}
        </p>
      )}
    </form>
  );
}
