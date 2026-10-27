/**
 * Human Element Video — Scenes Editor (v7).
 *
 * Replaces the single "Dialogue (optional)" textarea with an N-scene
 * editor. For N=1 the UI looks identical to v5 (no scene number badge,
 * no inheritance footer, no override toggle). For N>1 it shows N stacked
 * cards, each with:
 *   - Scene number badge
 *   - Dialogue textarea (verbatim when filled)
 *   - ✨ Enhance button (per-scene)
 *
 * Scene count stepper [–] N [+] sits next to the SCENES eyebrow. Locked
 * once a run starts (caller passes disabled=true).
 *
 * v7 — per-scene character/bg overrides have been LIFTED OUT of the scene
 * card and into the Look & feel section's per-scene grid (driven from the
 * parent). Scene cards are now dialogue + enhance only. The
 * `SceneOverride` type still lives here because it describes the wire
 * format the parent's payload builder uses.
 */
import type {
  ProducerAssetRow,
  YasUploadRef,
} from "../../lib/types";
import { EnhanceButton } from "./EnhanceButton";

export const MAX_SCENES = 5;

export interface SceneOverride {
  // null = inherit from global Look & Feel (single-scene mode), or no pick
  // yet (multi-scene mode where every scene must be filled or copied).
  character_asset: ProducerAssetRow | null;
  character_upload: YasUploadRef | null;
  bg_asset: ProducerAssetRow | null;
  bg_upload: YasUploadRef | null;
  // One-shot intent for a scene with NO bg ref: "new" regenerates a fresh
  // background (character kept); "keep" (default) passes the still through.
  // NOT a reference — deliberately excluded from isSceneOverridden().
  bg_mode?: "keep" | "new";
}

export const EMPTY_SCENE_OVERRIDE: SceneOverride = {
  character_asset: null,
  character_upload: null,
  bg_asset: null,
  bg_upload: null,
  bg_mode: "keep",
};

export function isSceneOverridden(o: SceneOverride): boolean {
  return !!(
    o.character_asset ||
    o.character_upload ||
    o.bg_asset ||
    o.bg_upload
  );
}

interface Props {
  sceneCount: number;
  setSceneCount: (n: number) => void;
  /** length === MAX_SCENES; only the first sceneCount entries are shown. */
  dialogues: string[];
  setDialogue: (idx: number, text: string) => void;
  /** Called when user clicks ✨ Enhance for scene idx. */
  onEnhance: (idx: number) => void;
  /** Per-scene enhance busy state. */
  enhancingIdx: number | null;
  /** True when the run is in flight — locks the stepper + textareas. */
  disabled: boolean;
}

export function TalkingHeadV2ScenesEditor({
  sceneCount,
  setSceneCount,
  dialogues,
  setDialogue,
  onEnhance,
  enhancingIdx,
  disabled,
}: Props) {
  const multi = sceneCount > 1;
  return (
    <div>
      <div className="flex items-center justify-between gap-2 mb-1.5">
        <span className="eyebrow">
          {multi ? "Scenes" : "Dialogue"}
          {!multi && (
            <span className="text-muted/70 normal-case tracking-normal">
              {" "}
              (optional)
            </span>
          )}
        </span>
        <Stepper
          value={sceneCount}
          min={1}
          max={MAX_SCENES}
          disabled={disabled}
          onChange={setSceneCount}
        />
      </div>

      <div className="space-y-2">
        {Array.from({ length: sceneCount }, (_, i) => (
          <SceneCard
            key={i}
            index={i}
            multi={multi}
            dialogue={dialogues[i] ?? ""}
            setDialogue={(t) => setDialogue(i, t)}
            onEnhance={() => onEnhance(i)}
            enhancing={enhancingIdx === i}
            disabled={disabled}
          />
        ))}
      </div>

      {multi && (
        <p className="text-[10.5px] text-muted leading-snug mt-2 pl-1">
          Each scene is an 8-second clip with its own dialogue. Leave a
          dialogue blank to let GPT write it. Pick a character and
          background per scene in <span className="text-fg">Look &amp; feel</span> below.
        </p>
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Stepper
// ---------------------------------------------------------------------------

function Stepper({
  value,
  min,
  max,
  disabled,
  onChange,
}: {
  value: number;
  min: number;
  max: number;
  disabled?: boolean;
  onChange: (v: number) => void;
}) {
  const dec = () => onChange(Math.max(min, value - 1));
  const inc = () => onChange(Math.min(max, value + 1));
  return (
    <div className="inline-flex items-center rounded-md border border-border bg-bg-subtle text-2xs overflow-hidden">
      <button
        type="button"
        onClick={dec}
        disabled={disabled || value <= min}
        className="w-6 h-6 text-fg hover:bg-bg-muted disabled:opacity-30 disabled:hover:bg-transparent transition-colors"
        title="Fewer scenes"
        aria-label="Decrease scene count"
      >
        –
      </button>
      <span
        className="px-2 font-mono text-fg select-none min-w-[2.25rem] text-center"
        aria-live="polite"
      >
        {value}
      </span>
      <button
        type="button"
        onClick={inc}
        disabled={disabled || value >= max}
        className="w-6 h-6 text-fg hover:bg-bg-muted disabled:opacity-30 disabled:hover:bg-transparent transition-colors"
        title="More scenes"
        aria-label="Increase scene count"
      >
        +
      </button>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Scene card
// ---------------------------------------------------------------------------

interface SceneCardProps {
  index: number;
  multi: boolean;
  dialogue: string;
  setDialogue: (t: string) => void;
  onEnhance: () => void;
  enhancing: boolean;
  disabled: boolean;
}

function SceneCard({
  index,
  multi,
  dialogue,
  setDialogue,
  onEnhance,
  enhancing,
  disabled,
}: SceneCardProps) {
  // For single-scene mode, render the card flush (no border/badge) so the
  // UI looks identical to v5's lone Dialogue textarea.
  const wrapClass = multi
    ? "rounded-lg border border-border bg-bg-subtle/40 p-2.5 space-y-2"
    : "";

  return (
    <div className={wrapClass}>
      {multi && (
        <div className="flex items-center justify-between gap-2">
          <span className="inline-flex items-center gap-1.5 text-2xs font-medium text-fg">
            <span className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-fg text-bg text-3xs font-mono">
              {index + 1}
            </span>
            <span>Scene {index + 1}</span>
          </span>
        </div>
      )}
      <div className="relative">
        <textarea
          rows={multi ? 2 : 3}
          value={dialogue}
          onChange={(e) => setDialogue(e.target.value)}
          disabled={disabled}
          placeholder={
            multi
              ? `What the speaker says in scene ${index + 1}…`
              : "What the speaker says (~20-25 words). Leave blank to auto-write."
          }
          className="w-full rounded-lg border border-border bg-bg-subtle text-sm px-2.5 py-1.5 pr-24 resize-none focus:outline-none focus:border-accent disabled:opacity-60"
        />
        <EnhanceButton
          onClick={onEnhance}
          busy={enhancing}
          disabled={enhancing || !dialogue.trim() || disabled}
          title="Polish this scene's line"
        />
      </div>
    </div>
  );
}
