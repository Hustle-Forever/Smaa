/**
 * "Previous videos": the intro/outro picker's second source. Lists each
 * earlier Human Element Video's current intro (or outro) card, newest first.
 * A picked card is kept as it is (its headline swapped for the script's words
 * when they differ, unless "Keep card text" is on); used at its own size with
 * its words unchanged, its finished clip comes with it (no new Veo).
 *
 * Rendered inside AssetPickerModal's extra tab; picking a card calls
 * `onPick` (the modal closes from there).
 */
import { useEffect, useState } from "react";

import { api } from "../../lib/api";
import { aspectClass } from "../../lib/aspect";
import { previousCardNote } from "../../lib/thBookends";
import type { TalkingHeadAspect, TalkingHeadPreviousCard } from "../../lib/types";

interface Props {
  role: "intro" | "outro";
  /** The run being edited: its own card is not listed. */
  excludeRunId: number | null;
  videoAspect: TalkingHeadAspect;
  selectedCardId: number | null;
  onPick: (card: TalkingHeadPreviousCard) => void;
}

export function TalkingHeadV2PreviousCards({ role, excludeRunId, videoAspect, selectedCardId, onPick }: Props) {
  const [items, setItems] = useState<TalkingHeadPreviousCard[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setItems(null);
    setError(null);
    api.yasProducer.talkingHeadV2
      .previousCards(role, { excludeRunId, limit: 30 })
      .then((r) => {
        if (!cancelled) setItems(r.items ?? []);
      })
      .catch((e: any) => {
        if (!cancelled) setError(e?.message || String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [role, excludeRunId]);

  if (error) {
    return <p className="text-sm text-danger text-center py-8">Couldn't load earlier videos: {error}</p>;
  }
  if (items === null) {
    return <p className="text-sm text-muted text-center py-8">Loading…</p>;
  }
  if (items.length === 0) {
    return (
      <div className="space-y-2 py-6 text-center">
        <p className="text-sm text-fg">No {role} cards from earlier videos yet.</p>
        <p className="text-xs text-muted leading-relaxed max-w-md mx-auto">
          Each video's {role} card shows up here once it is made.
        </p>
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <p className="text-xs text-muted leading-relaxed">
        The card is used as it is. A headline that differs from your script is swapped for your
        script's words (turn on Keep card text to keep the card's own); logos, web addresses and
        calls to action never change.
      </p>
      <div className="grid grid-cols-3 gap-3">
        {items.map((c) => {
          const note = previousCardNote(c, videoAspect);
          const picked = selectedCardId === c.card_id;
          return (
            <button
              key={c.card_id}
              type="button"
              onClick={() => onPick(c)}
              aria-pressed={picked}
              className={[
                "relative rounded-xl overflow-hidden border-2 transition-all text-left bg-bg-muted/30",
                picked ? "border-accent ring-2 ring-accent/30" : "border-border hover:border-fg/30",
              ].join(" ")}
            >
              <div className={`${aspectClass(videoAspect, "9:16")} bg-black flex items-center justify-center`}>
                <img src={c.url} alt={note.title} loading="lazy" className="w-full h-full object-contain" />
              </div>
              <div className="px-2 py-1.5 bg-bg-muted/50 space-y-0.5">
                <p className="text-xs text-fg truncate" title={note.title}>
                  {note.title}
                </p>
                {note.text && (
                  <p className="text-3xs text-muted line-clamp-2 break-words" title={note.text}>
                    {note.text}
                  </p>
                )}
                <p className="text-3xs text-muted">{note.shape}</p>
                {note.clip && <p className="text-3xs text-success leading-snug">{note.clip}</p>}
              </div>
              {picked && (
                <span className="absolute top-1.5 right-1.5 w-6 h-6 rounded-full bg-accent text-white text-xs flex items-center justify-center">
                  ✓
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
