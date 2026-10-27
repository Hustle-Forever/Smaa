/**
 * Look step: the Speaker field, shown while no character is picked. It holds
 * who is on camera (run.speaker: the default Emirati speaker, the one the
 * topic fixed, or the user's), with chips for a default Emirati man or woman.
 * The draft is saved with the Look step's main action (lib/thSpeaker).
 */
import { SPEAKER_CHIPS, SPEAKER_HINT, activeChip, chipClick, speakerSourceLabel } from "../../lib/thSpeaker";
import type { TalkingHeadRun } from "../../lib/types";

export function TalkingHeadV2SpeakerField({
  run,
  value,
  onChange,
  disabled,
}: {
  run: TalkingHeadRun | null;
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
}) {
  const chip = activeChip(value, run);
  const saved = (run?.speaker?.subject ?? "").trim();
  const dirty = value.trim() !== "" && value.trim() !== saved;
  const source = speakerSourceLabel(run?.speaker);
  return (
    <div className="rounded-lg border border-border bg-bg-subtle/40 p-2 space-y-1.5">
      <div className="flex items-center gap-2">
        <p className="flex-1 text-[11.5px] font-medium text-fg leading-tight">Speaker</p>
        {dirty ? (
          <span className="text-3xs text-accent font-mono uppercase tracking-wider2">Unsaved</span>
        ) : source ? (
          <span className="text-3xs text-muted font-mono uppercase tracking-wider2">{source}</span>
        ) : null}
      </div>
      <textarea
        rows={2}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        maxLength={400}
        aria-label="Speaker"
        placeholder="e.g. an Emirati woman in her twenties in a navy abaya and shayla"
        className="w-full rounded border border-border bg-bg text-[11px] px-1.5 py-1 resize-none focus:outline-none focus:border-accent disabled:opacity-60"
      />
      <div className="flex items-center gap-1.5">
        {SPEAKER_CHIPS.map((c) => (
          <button
            key={c}
            type="button"
            // The chip already chosen changes nothing (the run's own
            // speaker would only come back as "Unsaved").
            onClick={() => {
              const next = chipClick(c, value, run);
              if (next != null) onChange(next);
            }}
            disabled={disabled}
            aria-pressed={chip === c}
            className={`text-[10.5px] rounded px-2 py-1 border transition-colors disabled:opacity-40 ${
              chip === c
                ? "bg-accent/15 border-accent text-accent"
                : "bg-bg-subtle border-border text-muted hover:text-fg"
            }`}
          >
            {c}
          </button>
        ))}
      </div>
      <p className="text-[10.5px] text-muted leading-snug">{SPEAKER_HINT}</p>
    </div>
  );
}
