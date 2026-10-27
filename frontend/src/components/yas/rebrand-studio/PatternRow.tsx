// Rebrand studio — one pattern in the scene: a single line until opened.
//
//   collapsed   thumbnail · name ("from v3" while it is still the version's
//               own, unchanged, carried on from it) · where it went and its
//               colour · a colour dot (the DRAFT's colour, never an older
//               preview's) · eye · ⋯
//   open        Layer (behind the character or in front; made in house), Back to
//               automatic while it was moved on the picture (placing, sizing
//               and turning are done on the picture), Colour (Auto = the palette in Style, four
//               brand swatches, Pick), Motion (its entrance) — or, made in
//               house, Time: when it shows (from / to, or the whole scene),
//               how it comes in and goes out and over how long (the
//               timeline's bar and wedges, as numbers) — and Camera (made in
//               house: moves with the camera's push-in, or holds still)
//
// Everything is a plain recipe edit handed up; this file only lays it out.

import { useEffect, useId, useState } from "react";
import { AlertTriangle, Eye, EyeOff, RotateCcw, Shuffle } from "lucide-react";

import type { RebrandPattern, RebrandPatternAnimation } from "../../../lib/rebrandApi";
import type { HarnessCatalogue, HarnessElement, HarnessSwatch } from "../../../lib/rebrandHarnessApi";
import { isHex } from "../../../lib/rebrandRecipe";
import { RAMP_SEC, SPAN_MIN_SEC, type Ramps, type SecSpan, type Timing } from "../../../lib/rebrandTiming";
import { ChoiceChips, type ChoiceOption } from "../../ui/ChoiceChips";
import { MenuItem, OverflowMenu } from "../../ui/controls";
import { Spinner } from "../../ui/Spinner";
import { previewSrc } from "../PatternLibraryGrid";
import { PickColour, SwatchDot } from "./PickColour";
import { COPY } from "./studioCopy";

/** A pattern moved by hand, as its row shows it (made in house only). */
export interface RowHand {
  /** Moved, sized or turned on the picture. */
  placed: boolean;
  /** Its size (× as placed) and turn (degrees) as drawn. */
  scale: number;
  rotate: number;
  /** A full-screen place: panned and zoomed, never boxed. */
  full: boolean;
  /** Where the preview drew it is known (the sliders need it). */
  canEdit: boolean;
  follow: "auto" | "room" | "screen";
}

/** A pattern's time, as its row shows it (made in house only). */
export interface RowTime {
  /** The scene's length (s). */
  duration: number;
  /** When it shows on the scene (null: it starts after the scene ends). */
  span: SecSpan | null;
  whole: boolean;
  /** As drawn (its entrance and exit resolved). */
  timing: Timing | null;
  /** The recipe's own: the entrance's length, the exit and its length. */
  entranceSec: number;
  exit: string;
  exitSec: number;
  /** The catalogue's exits (absent on an older server). */
  exits: RebrandPatternAnimation[];
}

/** Seconds typed in a box, kept as typed until it is let go (Enter, or
 *  leaving it): then clamped and handed up once. */
function SecInput({
  value,
  min,
  max,
  label,
  disabled,
  onCommit,
}: {
  value: number;
  min: number;
  max: number;
  label: string;
  disabled?: boolean;
  onCommit: (v: number) => void;
}) {
  const [text, setText] = useState(value.toFixed(1));
  useEffect(() => setText(value.toFixed(1)), [value]);
  const commit = () => {
    const v = Number(text);
    if (!Number.isFinite(v)) {
      setText(value.toFixed(1));
      return;
    }
    const c = Math.round(Math.min(max, Math.max(min, v)) * 100) / 100;
    setText(c.toFixed(1));
    if (Math.abs(c - value) >= 0.005) onCommit(c);
  };
  return (
    <input
      type="number"
      inputMode="decimal"
      step={0.1}
      min={min}
      max={max}
      value={text}
      aria-label={label}
      disabled={disabled}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          commit();
        }
      }}
      className="w-16 rounded-md border border-border bg-bg px-1.5 py-1 text-right font-mono text-2xs tabular-nums text-fg focus:border-accent focus:outline-none disabled:opacity-50"
    />
  );
}

export function rowDomId(elementId: string): string {
  return `rebrand-row-${elementId}`;
}

export function PatternRow({
  el,
  entry,
  catalogue,
  swatches,
  open,
  onToggle,
  busy,
  colour,
  paletteHex,
  note,
  canAnotherSpot,
  showBlocked = null,
  canSwap,
  onEnabled,
  onColour,
  onEntrance,
  onAnotherSpot,
  onSwap,
  onRemove,
  fromNo = null,
  hand = null,
  onBackToAuto,
  onFollow,
  onLayer,
  time = null,
  onSpan,
  onWhole,
  onRamps,
}: {
  el: HarnessElement;
  entry: RebrandPattern | undefined;
  catalogue: HarnessCatalogue | null;
  swatches: HarnessSwatch[];
  open: boolean;
  onToggle: () => void;
  /** Its part of the preview is being redrawn. */
  busy: boolean;
  /** The colour it is drawn in (null: the intro's, not known yet). */
  colour: string | null;
  /** What Auto resolves to. */
  paletteHex: string | null;
  /** "Left side", "Top right", "Placing…". */
  placeText: string;
  /** A warning from the preview (it moved, it overlaps another). */
  note: string | null;
  canAnotherSpot: boolean;
  /** Full screen is not an option while other patterns are on. */
  fullBlocked: boolean;
  /** Why this hidden pattern can't be shown right now (null: it can). */
  showBlocked?: string | null;
  canSwap: boolean;
  onEnabled: (v: boolean) => void;
  onAnchor: (anchor: string) => void;
  onColour: (colour: string) => void;
  onEntrance: (entrance: string) => void;
  onAnotherSpot: () => void;
  onSwap: () => void;
  onRemove: () => void;
  /** Still the version's own, unchanged (carried on from v3 → 3). */
  fromNo?: number | null;
  /** Made in house: by hand (null: an Omni draft, none of it). */
  hand?: RowHand | null;
  onHand?: (patch: { scale?: number; rotate?: number }) => void;
  onBackToAuto?: () => void;
  onFollow?: (follow: "auto" | "room" | "screen") => void;
  /** Made in house: behind the character or in front of the character. */
  onLayer?: (z: "behind" | "front") => void;
  /** Made in house: when it shows (null: an Omni draft — its Motion only). */
  time?: RowTime | null;
  onSpan?: (span: SecSpan) => void;
  onWhole?: () => void;
  onRamps?: (ramps: Ramps) => void;
}) {
  const panelId = useId();
  const motionId = useId();
  const followId = useId();
  const eyeWhyId = useId();
  const outId = useId();
  const name = entry?.name ?? String(el.props.slug ?? "Pattern");
  const own = el.props.colour;
  const colourValue = isHex(own) ? own.toLowerCase() : "auto";
  const isSwatch = swatches.some((s) => s.value.toLowerCase() === colourValue);
  const colourName =
    colourValue === "auto"
      ? COPY.colourAuto
      : swatches.find((s) => s.value.toLowerCase() === colourValue)?.label ?? COPY.colourOwn;

  const swatchOptions: ChoiceOption<string>[] = swatches.map((s) => {
    const auto = s.value === "auto";
    const hex = auto ? paletteHex : s.value;
    return {
      value: auto ? "auto" : s.value.toLowerCase(),
      label: s.label,
      ariaLabel: auto ? COPY.colourAuto : s.label,
      title: auto ? COPY.autoTitle(paletteHex) : `${s.label} (${s.value})`,
      icon: <SwatchDot colour={hex ?? "#a1a1aa"} mark={auto ? "A" : undefined} />,
    };
  });
  const entrances = catalogue?.entrances ?? [{ value: "auto", label: "Its own", help: "" }];
  const timeText = time?.span && !time.whole ? COPY.time.collapsed(time.span.from, time.span.to) : null;

  return (
    <li
      className={`rounded-xl border transition-colors ${
        open ? "border-border-strong bg-bg shadow-soft" : "border-border bg-bg hover:border-border-strong"
      }`}
    >
      <div className="flex items-center gap-1.5 pl-1.5 pr-1 py-1.5 min-h-[48px]">
        <button
          id={rowDomId(el.id)}
          type="button"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={onToggle}
          className="flex-1 min-w-0 flex items-center gap-2.5 rounded-lg px-1 py-0.5 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        >
          <span className={`h-9 w-12 shrink-0 overflow-hidden rounded-md bg-fg ${el.enabled ? "" : "opacity-40"}`}>
            {entry && <img src={previewSrc(entry)} alt="" draggable={false} className="h-full w-full object-contain" />}
          </span>
          <span className={`min-w-0 ${el.enabled ? "" : "opacity-60"}`}>
            <span className="flex min-w-0 items-center gap-1.5">
              <span className="truncate text-xs font-medium text-fg">{name}</span>
              {fromNo && (
                <span className="shrink-0 rounded-full bg-bg-muted px-1.5 text-3xs font-medium text-muted">
                  {COPY.keptFrom(fromNo)}
                </span>
              )}
            </span>
            <span className="block truncate text-2xs text-muted">
              {el.enabled
                ? `${colourName}${timeText ? ` · ${timeText}` : ""}${el.z === "front" ? ` · ${COPY.layerFront}` : ""}`
                : COPY.hidden}
            </span>
          </span>
        </button>
        {busy && <Spinner size="xs" tone="accent" className="shrink-0" />}
        <span
          className={`h-3.5 w-3.5 shrink-0 rounded-full border border-black/10 ${el.enabled ? "" : "opacity-40"}`}
          style={{ background: colour ?? "#d4d4d8" }}
          title={colour ?? undefined}
          aria-hidden
        />
        <button
          type="button"
          aria-label={el.enabled ? COPY.hide(name) : COPY.show(name)}
          aria-describedby={!el.enabled && showBlocked ? eyeWhyId : undefined}
          title={el.enabled ? COPY.hide(name) : showBlocked ?? COPY.show(name)}
          disabled={!el.enabled && !!showBlocked}
          onClick={() => onEnabled(!el.enabled)}
          className="h-8 w-8 shrink-0 rounded-lg flex items-center justify-center text-muted hover:text-fg hover:bg-bg-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:opacity-35 disabled:hover:bg-transparent disabled:hover:text-muted disabled:cursor-not-allowed"
        >
          {el.enabled ? <Eye className="h-4 w-4" aria-hidden /> : <EyeOff className="h-4 w-4" aria-hidden />}
        </button>
        {!el.enabled && showBlocked && (
          <span id={eyeWhyId} className="sr-only">
            {showBlocked}
          </span>
        )}
        <OverflowMenu label={COPY.rowActions(name)} size="sm">
          {canSwap && <MenuItem onClick={onSwap}>{COPY.swap}</MenuItem>}
          {canAnotherSpot && (
            <MenuItem onClick={onAnotherSpot} Icon={Shuffle} title={COPY.anotherSpotTitle}>
              {COPY.anotherSpot}
            </MenuItem>
          )}
          <MenuItem onClick={onRemove}>{COPY.remove}</MenuItem>
        </OverflowMenu>
      </div>

      {note && el.enabled && (
        <p className="mx-3 -mt-0.5 mb-2 flex items-start gap-1.5 text-2xs text-amber-700">
          <AlertTriangle className="mt-px h-3 w-3 shrink-0" aria-hidden />
          {note}
        </p>
      )}

      <div id={panelId} hidden={!open} className="space-y-3 border-t border-border px-3 pb-3 pt-2.5">
        {open && (
          <>
            {onLayer && (
              <div className="space-y-1.5">
                <span className="eyebrow block">{COPY.layer}</span>
                <ChoiceChips
                  label={`${name}: ${COPY.layer.toLowerCase()}`}
                  options={[
                    { value: "behind", label: COPY.layerBehind },
                    { value: "front", label: COPY.layerFront },
                  ]}
                  value={el.z === "front" ? "front" : "behind"}
                  onChange={(v) => onLayer(v === "front" ? "front" : "behind")}
                  disabled={!el.enabled}
                />
              </div>
            )}

            {hand?.placed && onBackToAuto && el.enabled && (
              <button
                type="button"
                onClick={onBackToAuto}
                title={COPY.hand.backToAutoTitle}
                className="btn btn-xs btn-ghost gap-1"
              >
                <RotateCcw className="h-3 w-3" aria-hidden /> {COPY.hand.backToAuto}
              </button>
            )}

            <div className="space-y-1.5">
              <div className="flex items-baseline gap-2">
                <span className="eyebrow">{COPY.colour}</span>
                <span className="text-2xs text-muted truncate">{colourName}</span>
              </div>
              <div className="flex flex-wrap items-center gap-2.5">
                <ChoiceChips
                  variant="swatch"
                  label={`${name}: ${COPY.colour.toLowerCase()}`}
                  options={swatchOptions}
                  value={colourValue}
                  onChange={onColour}
                  disabled={!el.enabled}
                />
                <PickColour
                  value={isHex(own) ? own.toLowerCase() : colour}
                  checked={colourValue !== "auto" && !isSwatch}
                  onCommit={onColour}
                  label={COPY.pickColour}
                  title={COPY.pickColourTitle}
                  disabled={!el.enabled}
                />
              </div>
            </div>

            {time ? (
              <div className="space-y-1.5">
                <div className="flex items-baseline gap-2">
                  <span className="eyebrow">{COPY.time.section}</span>
                  <span className="truncate text-2xs text-muted">
                    {time.span
                      ? time.whole
                        ? COPY.time.wholeScene
                        : COPY.time.collapsed(time.span.from, time.span.to)
                      : COPY.time.notShown(name)}
                  </span>
                </div>
                <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-2 gap-y-1.5">
                  <span className="text-2xs text-muted">{COPY.time.shows}</span>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <SecInput
                      value={time.span?.from ?? 0}
                      min={0}
                      max={Math.max(0, (time.span?.to ?? time.duration) - SPAN_MIN_SEC)}
                      label={COPY.time.fromLabel(name)}
                      disabled={!el.enabled}
                      onCommit={(v) => onSpan?.({ from: v, to: time.span?.to ?? time.duration })}
                    />
                    <span className="text-2xs text-muted">{COPY.time.to}</span>
                    <SecInput
                      value={time.span?.to ?? time.duration}
                      min={Math.min(time.duration, (time.span?.from ?? 0) + SPAN_MIN_SEC)}
                      max={time.duration}
                      label={COPY.time.toLabel(name)}
                      disabled={!el.enabled}
                      onCommit={(v) => onSpan?.({ from: time.span?.from ?? 0, to: v })}
                    />
                    <span className="text-2xs text-muted">{COPY.time.seconds}</span>
                    <button
                      type="button"
                      onClick={onWhole}
                      disabled={!el.enabled || time.whole}
                      title={COPY.time.wholeSceneTitle}
                      className="btn btn-xs btn-ghost"
                    >
                      {COPY.time.wholeScene}
                    </button>
                  </div>
                  <label htmlFor={motionId} className="text-2xs text-muted">
                    {COPY.time.comesIn}
                  </label>
                  <div className="flex items-center gap-1.5">
                    <select
                      id={motionId}
                      value={el.animation.entrance}
                      disabled={!el.enabled}
                      aria-label={COPY.time.inLabel(name)}
                      onChange={(e) => onEntrance(e.target.value)}
                      className="min-w-0 flex-1 rounded-lg border border-border bg-bg px-2 py-1 text-xs text-fg focus:outline-none focus:border-accent disabled:opacity-50"
                    >
                      {entrances.map((a) => (
                        <option key={a.value} value={a.value}>
                          {a.label}
                        </option>
                      ))}
                    </select>
                    <span className="text-2xs text-muted">{COPY.time.over}</span>
                    <SecInput
                      value={time.entranceSec}
                      min={RAMP_SEC[0]}
                      max={RAMP_SEC[1]}
                      label={COPY.time.inSecLabel(name)}
                      disabled={!el.enabled || el.animation.entrance === "none" || time.timing?.entrance === "none"}
                      onCommit={(v) => onRamps?.({ entranceSec: v })}
                    />
                  </div>
                  <label htmlFor={outId} className="text-2xs text-muted">
                    {COPY.time.goesOut}
                  </label>
                  <div className="flex items-center gap-1.5">
                    <select
                      id={outId}
                      value={time.exit}
                      disabled={!el.enabled}
                      aria-label={COPY.time.outLabel(name)}
                      onChange={(e) => onRamps?.({ exit: e.target.value })}
                      className="min-w-0 flex-1 rounded-lg border border-border bg-bg px-2 py-1 text-xs text-fg focus:outline-none focus:border-accent disabled:opacity-50"
                    >
                      {(time.exits.length ? time.exits : [{ value: "auto", label: COPY.time.exitOwn, help: "" }]).map((a) => (
                        <option key={a.value} value={a.value} title={a.help}>
                          {a.label}
                        </option>
                      ))}
                    </select>
                    <span className="text-2xs text-muted">{COPY.time.over}</span>
                    <SecInput
                      value={time.exitSec}
                      min={RAMP_SEC[0]}
                      max={RAMP_SEC[1]}
                      label={COPY.time.outSecLabel(name)}
                      disabled={!el.enabled || time.timing?.exit === "none"}
                      onCommit={(v) => onRamps?.({ exitSec: v })}
                    />
                  </div>
                </div>
              </div>
            ) : (
              <div className="space-y-1.5">
                <label htmlFor={motionId} className="eyebrow block">
                  {COPY.motion}
                </label>
                <select
                  id={motionId}
                  value={el.animation.entrance}
                  disabled={!el.enabled}
                  onChange={(e) => onEntrance(e.target.value)}
                  className="w-full rounded-lg border border-border bg-bg px-2.5 py-1.5 text-xs text-fg focus:outline-none focus:border-accent disabled:opacity-50"
                >
                  {entrances.map((a) => (
                    <option key={a.value} value={a.value}>
                      {a.label}
                    </option>
                  ))}
                </select>
              </div>
            )}

            {hand && onFollow && (
              <div className="space-y-1.5">
                <label htmlFor={followId} className="eyebrow block">
                  {COPY.hand.follow}
                </label>
                <select
                  id={followId}
                  value={hand.follow}
                  disabled={!el.enabled}
                  title={COPY.hand.followHelp}
                  onChange={(e) => onFollow(e.target.value as "auto" | "room" | "screen")}
                  className="w-full rounded-lg border border-border bg-bg px-2.5 py-1.5 text-xs text-fg focus:outline-none focus:border-accent disabled:opacity-50"
                >
                  {COPY.hand.followOptions.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </div>
            )}
          </>
        )}
      </div>
    </li>
  );
}
