// Rebrand studio — the Icons tab.
//
//   [switch] Icons from what they say      the AI picks them (or: only yours)
//   IN SPEECH ORDER · Auto        [Auto ↺] each row: ⠿ · the icon in its own
//                                          look · label · time · on "word" ·
//                                          why · [✓ Keep] (the AI's picks) ·
//                                          [Why?] · ⋯ (a dot: changed by you)
//                                 Why? says who picked it, what was said and
//                                 when, why it helps, where it sits and why
//                                 that colour (the plan item's `why`)
//                                 a picked row opens that icon's own look
//                                 (IconLookPanel): style, colour, words,
//                                 size, behind / in front, enter / leave,
//                                 reset — [Reset all to auto] for every icon
//   (a reorder with nothing meaningful to move to asks, inline; only the
//   AI's track, one at a time, has an order: one on a track of its own is
//   moved on the timeline)
//   one of yours moved off its word (or the moment it was added at), out of
//   a zone: "Moved to 1.5 s, after the pattern comes in" · [Show at 0.2 s
//   anyway] (timed by hand at the moment it was added at, else on its word)
//   Every time a row says is the icon's one time: when it has settled
//   (iconTime), as the Moments chips and the timeline's mark say it.
//   [+ Add icon]                    n of 5
//   NOT ON SCREEN only icons you added that didn't land (over the limit ·
//                 Needs a word · Not in the library: an icon you add always
//                 shows otherwise), and only when there are any, with why
//                 and [Remove], so nothing you added vanishes. The AI's
//                 other ideas live in Add icon → Suggested (IconPicker),
//                 not here.
//   ALL ICONS     Where (only the sides this scene has room on) · Colour ·
//                 Size · Style (Circle · Circle + words; the older badge and
//                 bare glyph while in use) · Enter, and the AI's why
//   Omni is told to draw no icons.
//
// On an intro or outro (`byHand`) there is no switch and nothing is asked
// of the AI: the icons are only the ones added, and the list starts empty.
// Layer goes where no one is in the clip (`layers`).
//
// The list is the plan's (the server's) in speech order. Reordering never
// swaps two icons' slots: rebrandIcons.reorder moves an icon to another
// mention of its own idea (or the ones it jumps over to theirs), and when
// there is none it asks before showing one off its word. Every edit is a
// plain recipe edit handed up (undo, and the stage goes to Next).

import { useEffect, useId, useRef, useState, type KeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import { AlertTriangle, Check, GripVertical, Info, Plus, RotateCcw } from "lucide-react";

import type {
  HarnessIconCatalogue,
  HarnessIconIdea,
  HarnessIconItem,
  HarnessIconPlan,
  HarnessIconReorder,
  HarnessIconRules,
  HarnessIconStyle,
  HarnessRecipe,
} from "../../../lib/rebrandHarnessApi";
import {
  addIcon,
  ADDED_MAX,
  anywayOf,
  entranceOf,
  fmtSec,
  glyphId,
  hasIconEdits,
  hasTimingPins,
  ICON_ENTRANCES,
  iconProps,
  iconsOf,
  iconTag,
  iconTime,
  ideaTime,
  laneOf,
  listDrop,
  listStep,
  lookOf,
  NEW_STYLES,
  OLD_STYLES,
  pinOffWord,
  pinToWord,
  removeIcon,
  reorder,
  resetAllIcons,
  resetOrder,
  setIconColour,
  setIconEntrance,
  setIconsAi,
  setIconsEnabled,
  setLook,
  setIconStyle,
  setOmniOwn,
  setTweak,
  speechOrder,
  trackOrder,
  usesOldStyle,
  withTweaks,
  wordsDigestOf,
  type IconEntrance,
} from "../../../lib/rebrandIcons";
import { isHex, isHouse } from "../../../lib/rebrandRecipe";
import { ChoiceChips, type ChoiceOption } from "../../ui/ChoiceChips";
import { MenuItem, Notice, OverflowMenu } from "../../ui/controls";
import { Spinner } from "../../ui/Spinner";
import { IconGlyph } from "./IconGlyph";
import { IconLookPanel, type SceneColour } from "./IconLookPanel";
import { PickColour, SwatchDot } from "./PickColour";
import { COPY, mmss, sentence } from "./studioCopy";
import type { IconPlanState } from "./useIconPlan";

const VIOLET = "#7f2bf2";
const CHARCOAL = "#303030";
const WHITE = "#ffffff";

/** A question the studio asks before showing an icon off its word (the
 *  list's order; the lane asks none — a block let go off its word stays
 *  there, with "Snap back"). */
export interface IconChoice {
  from: "list" | "lane";
  text: string;
  /** "Show Code first anyway · 2.4 s, off its word" (null: no room at all). */
  anywayLabel: string | null;
  anyway: HarnessRecipe | null;
  keepLabel: string;
  /** Where it would go, drawn on the lane meanwhile. */
  ghost: { id: string; sec: number } | null;
}

export function iconRowId(id: string): string {
  return `rebrand-icon-${id}`;
}

export const ADD_ICON_ID = "rebrand-add-icon";

/** The choice row: "'Code' is only said at 4.7 s, after 'Free'." */
export function IconChoiceRow({
  choice,
  onAnyway,
  onKeep,
}: {
  choice: IconChoice;
  onAnyway: () => void;
  onKeep: () => void;
}) {
  return (
    <div
      role="group"
      aria-label={choice.text}
      className="space-y-2 rounded-xl border border-amber-300 bg-amber-50 px-3 py-2.5 text-xs text-fg"
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          onKeep();
        }
      }}
    >
      <p className="leading-snug">{choice.text}</p>
      {!choice.anywayLabel && <p className="text-2xs text-muted">{COPY.icons.noRoomAnyway}</p>}
      <div className="flex flex-wrap gap-1.5">
        {choice.anywayLabel && choice.anyway && (
          <button type="button" className="btn btn-xs btn-ghost border-amber-400" onClick={onAnyway}>
            {choice.anywayLabel}
          </button>
        )}
        <button
          type="button"
          className="btn btn-xs btn-secondary"
          onClick={onKeep}
          // The safe answer is the one a stray Enter gives.
          autoFocus
        >
          {choice.keepLabel}
        </button>
      </div>
    </div>
  );
}

function Switch({
  checked,
  onChange,
  label,
  hint,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  hint: string;
}) {
  const id = useId();
  return (
    <div className="flex items-start gap-2.5">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-labelledby={`${id}-l`}
        aria-describedby={`${id}-h`}
        onClick={() => onChange(!checked)}
        className={`relative mt-0.5 h-5 w-9 shrink-0 rounded-full transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 ${
          checked ? "bg-accent" : "bg-border-strong"
        }`}
      >
        <span
          aria-hidden
          className={`absolute left-0 top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform ${checked ? "translate-x-[18px]" : "translate-x-0.5"}`}
        />
      </button>
      <span className="min-w-0">
        <span id={`${id}-l`} className="block text-xs font-semibold text-fg">
          {label}
        </span>
        <span id={`${id}-h`} className="block text-2xs leading-snug text-muted">
          {hint}
        </span>
      </span>
    </div>
  );
}

function Steps({
  warming,
  since,
  title = COPY.icons.warmTitle,
  firstTime = true,
}: {
  warming: HarnessIconPlan | null;
  since: number | null;
  title?: string;
  firstTime?: boolean;
}) {
  const steps = warming?.steps ?? [];
  const names = steps.length ? steps.map((s) => s.name) : ["words", "spots", "director"];
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);
  return (
    <div className="rounded-xl border border-border bg-bg-muted/50 px-3 py-2.5">
      <p className="mb-1.5 text-xs font-medium text-fg">{title}</p>
      <ul className="space-y-1 text-2xs" role="status" aria-live="polite">
        {names.map((name) => {
          const s = steps.find((x) => x.name === name);
          const state = s?.state ?? "queued";
          return (
            <li key={name} className={`flex items-center gap-2 ${state === "queued" ? "text-muted" : "text-fg"}`}>
              <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center" aria-hidden>
                {state === "done" ? (
                  <span className="text-accent">✓</span>
                ) : state === "running" ? (
                  <Spinner size="xs" tone="accent" />
                ) : state === "failed" ? (
                  <span className="text-warning">!</span>
                ) : (
                  <span className="h-1.5 w-1.5 rounded-full bg-border-strong" />
                )}
              </span>
              {COPY.icons.step[name] ?? name}
              {s?.seconds != null && state === "done" && (
                <span className="ml-auto font-mono tabular-nums text-muted">{s.seconds.toFixed(1)} s</span>
              )}
            </li>
          );
        })}
      </ul>
      <p className="mt-1.5 text-3xs text-muted">
        {since ? <span aria-hidden className="font-mono tabular-nums">{mmss((now - since) / 1000)}{firstTime ? " · " : ""}</span> : null}
        {firstTime ? COPY.icons.stepFirstTime : null}
      </p>
    </div>
  );
}

export function IconsTab({
  recipe,
  plan,
  catalogue,
  rules,
  patternColour,
  selected,
  onSelect,
  onEdit,
  onAdd,
  onChange,
  onPlayFrom,
  choice,
  onChoice,
  omniOwn = null,
  onVersion = null,
  keptNo = null,
  onServerReorder,
  sceneColours = [],
  frame = null,
  byHand = false,
  layers = true,
}: {
  recipe: HarnessRecipe;
  plan: IconPlanState;
  catalogue: HarnessIconCatalogue | null;
  rules: HarnessIconRules;
  /** The first pattern's colour ("Pattern"), when it is known. */
  patternColour: string | null;
  selected: string | null;
  onSelect: (id: string | null) => void;
  onEdit: (next: HarnessRecipe) => void;
  onAdd: (trigger: HTMLElement) => void;
  onChange: (item: HarnessIconItem, trigger: HTMLElement | null) => void;
  onPlayFrom: (item: HarnessIconItem) => void;
  choice: IconChoice | null;
  onChoice: (c: IconChoice | null) => void;
  /** The version built on was made with Omni's own icons. */
  omniOwn?: { no: number; mode: "stack" | "rebuild"; onChangeIt: () => void } | null;
  /** Added on top of vN, whose own icons these are. */
  onVersion?: number | null;
  /** "Keep v3's icons": these are v3's, exactly as it froze them. */
  keptNo?: number | null;
  /** Ask the server where a reorder lands (it plans it to be sure); null:
   *  it can't say, and the studio works it out itself. */
  onServerReorder?: (glyph: string, to: number) => Promise<HarnessIconReorder | null>;
  /** The scene's own colours (each pattern's, the intro's): one click each
   *  in an icon's own colour. */
  sceneColours?: SceneColour[];
  /** The frame (px): an icon placed by hand is sized in it. */
  frame?: { w: number; h: number } | null;
  /** An intro or outro: only the icons added (no AI switch, nothing asked
   *  of it); with no icon element yet, the list is simply empty. */
  byHand?: boolean;
  /** Behind / in front of the character is offered (not on an intro or
   *  outro with no one in it). */
  layers?: boolean;
}) {
  const el = iconsOf(recipe);
  const p = iconProps(el);
  const recipeRef = useRef(recipe);
  recipeRef.current = recipe;
  const [reordering, setReordering] = useState(false);
  const [note, setNote] = useState("");
  const noteTimer = useRef<number | undefined>();
  const rowRefs = useRef<Record<string, HTMLLIElement | null>>({});
  const [drag, setDrag] = useState<{ from: number; to: number; y0: number } | null>(null);
  const [whyOpen, setWhyOpen] = useState<string | null>(null);
  const shown = plan.plan;
  const items = speechOrder(shown?.items);
  const digest = wordsDigestOf(shown);
  const fill = shown?.colours?.fill ?? VIOLET;
  const ink = shown?.colours?.ink ?? WHITE;

  const say = (text: string) => {
    setNote(text);
    window.clearTimeout(noteTimer.current);
    noteTimer.current = window.setTimeout(() => setNote(""), 5000);
  };

  if (el ? !el.enabled : !byHand) {
    return (
      <div className="space-y-3">
        <p className="rounded-xl border border-dashed border-border px-3 py-4 text-center text-xs text-muted">{COPY.icons.off}</p>
        <button type="button" className="btn btn-sm btn-ghost" onClick={() => onEdit(setIconsEnabled(recipe, true))}>
          {COPY.icons.turnOn}
        </button>
      </div>
    );
  }

  if (p.omni_own === "keep") {
    return (
      <div className="space-y-3">
        <Notice
          action={
            <button type="button" className="btn btn-xs btn-ghost" onClick={() => onEdit(setOmniOwn(recipe, "hide"))}>
              {COPY.icons.useOurs}
            </button>
          }
        >
          {omniOwn ? COPY.icons.keeping(omniOwn.no) : COPY.icons.keepingOmni}
        </Notice>
      </div>
    );
  }

  const doReorder = async (fromRow: number, toRow: number, via: "list" | "lane" = "list") => {
    if (!shown || reordering || fromRow === toRow || toRow < 0 || toRow >= items.length) return;
    const item = items[fromRow];
    const id = glyphId(item.glyph);
    // Only the AI's track (one at a time) has an order: one on a track of
    // its own is moved on the timeline. The places are track 0's (the
    // callers pass a row of track 0: listStep / listDrop).
    if (laneOf(item) !== 0) {
      say(COPY.icons.trackOrderNote);
      return;
    }
    if (laneOf(items[toRow]) !== 0) return;
    const order = trackOrder(shown.items);
    const from = order.indexOf(item);
    const to = order.indexOf(items[toRow]);
    if (from < 0 || to < 0 || from === to) return;
    // The server's answer first: it plans the result to be sure of it.
    if (onServerReorder) {
      setReordering(true);
      const answer = await onServerReorder(id, to).catch(() => null);
      setReordering(false);
      if (answer) {
        const latest = recipeRef.current;
        if (answer.ok) {
          if (answer.tweaks && Object.keys(answer.tweaks).length) {
            onChoice(null);
            onEdit(withTweaks(latest, answer.tweaks));
          }
          if (answer.message) say(answer.message);
        } else if (answer.choice) {
          const any = answer.choice.anyway;
          onChoice({
            from: via,
            text: answer.choice.message,
            anywayLabel: any
              ? to < from
                ? COPY.icons.anywayFirst(item.label, fmtSec(any.sec))
                : COPY.icons.anywayAfter(item.label, fmtSec(any.sec))
              : null,
            anyway: any ? withTweaks(latest, any.tweaks) : null,
            keepLabel: COPY.icons.keepOrder,
            ghost: any ? { id, sec: any.sec } : null,
          });
        } else if (answer.message) say(answer.message);
        return;
      }
    }
    const r = reorder(recipeRef.current, shown, from, to, rules);
    if (r.kind === "moved") {
      onChoice(null);
      onEdit(r.recipe);
      say(r.note);
    } else if (r.kind === "choice") {
      const at = fmtSec(r.item.mentions.find((m) => m.w === r.item.w)?.at ?? r.item.settle);
      onChoice({
        from: via,
        text:
          r.side === "before"
            ? COPY.icons.choiceBefore(r.item.label, at, r.other.label)
            : COPY.icons.choiceAfter(r.item.label, at, r.other.label),
        anywayLabel:
          r.sec != null
            ? r.side === "before"
              ? COPY.icons.anywayFirst(r.item.label, fmtSec(r.sec))
              : COPY.icons.anywayAfter(r.item.label, fmtSec(r.sec))
            : null,
        anyway: r.anyway,
        keepLabel: COPY.icons.keepOrder,
        ghost: r.sec != null ? { id: glyphId(r.item.glyph), sec: r.sec } : null,
      });
    }
  };

  // A step or a drop past rows on a track of their own goes to the nearest
  // row of track 0 (listStep / listDrop): a row of track 0 is never stuck.
  const moveRow = (from: number, to: number | null) => {
    if (laneOf(items[from]) !== 0) say(COPY.icons.trackOrderNote);
    else if (to != null) void doReorder(from, to);
  };
  const onRowKey = (e: KeyboardEvent, i: number) => {
    if (!e.altKey || (e.key !== "ArrowUp" && e.key !== "ArrowDown")) return;
    e.preventDefault();
    moveRow(i, listStep(items, i, e.key === "ArrowUp" ? -1 : 1));
  };

  // Drag by the handle: the row it is let go over is where it asked to go.
  const rowAt = (y: number) => {
    let best = drag?.from ?? 0;
    items.forEach((it, i) => {
      const r = rowRefs.current[glyphId(it.glyph)]?.getBoundingClientRect();
      if (r && y >= r.top && y <= r.bottom) best = i;
    });
    return best;
  };
  const dragStart = (e: ReactPointerEvent, i: number) => {
    if (e.button !== 0) return;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    setDrag({ from: i, to: i, y0: e.clientY });
  };
  const dragMove = (e: ReactPointerEvent) => drag && setDrag({ ...drag, to: rowAt(e.clientY) });
  const dragEnd = () => {
    if (drag && drag.to !== drag.from) moveRow(drag.from, listDrop(items, drag.from, drag.to));
    setDrag(null);
  };

  const shownIds = new Set(items.map((it) => glyphId(it.glyph)));
  // Only yours that didn't land: the AI's other ideas are in Suggested. Yours
  // not tied to anything said (a drawn name, say) come first: they wait for a
  // word to land on.
  const mineOff = (shown?.ideas ?? [])
    .filter((i) => i.added && i.state !== "on" && !shownIds.has(glyphId(i.glyph)))
    .sort((a, b) => Number(looseAdd(b)) - Number(looseAdd(a)));
  const count = items.length;
  const addedFull = p.added.length >= ADDED_MAX;
  const said = (shown?.timeline?.words ?? []).filter(
    (w): w is typeof w & { s: number } => w.said && typeof w.s === "number",
  );
  const pins = hasTimingPins(recipe);

  // Layer, for all of them: in front when every icon is, behind when none
  // is (null: some are, some are not).
  const fronts = items.filter((it) => lookOf(recipe, glyphId(it.glyph)).layer === "front").length;
  const allLayer = fronts === 0 ? "behind" : fronts === items.length ? "front" : null;

  const colourValue =
    p.colour === "auto"
      ? "auto"
      : p.colour === VIOLET
        ? "violet"
        : p.colour === CHARCOAL
          ? "charcoal"
          : p.colour === WHITE
            ? "white"
            : patternColour && p.colour === patternColour.toLowerCase()
              ? "pattern"
              : null;
  const colourOptions: ChoiceOption<string>[] = [
    { value: "auto", label: COPY.icons.colourChip.auto, ariaLabel: COPY.icons.colourChip.auto, title: shown?.colours?.why ?? COPY.icons.colourChip.auto, icon: <SwatchDot colour={fill} mark="A" /> },
    { value: "violet", label: COPY.icons.colourChip.violet, ariaLabel: COPY.icons.colourChip.violet, title: `${COPY.icons.colourChip.violet} (${VIOLET})`, icon: <SwatchDot colour={VIOLET} /> },
    {
      value: "pattern",
      label: COPY.icons.colourChip.pattern,
      ariaLabel: COPY.icons.colourChip.pattern,
      title: patternColour ? `${COPY.icons.colourChip.pattern} (${patternColour})` : COPY.icons.patternColourUnknown,
      icon: <SwatchDot colour={patternColour ?? "#d4d4d8"} mark="P" />,
      disabled: !patternColour,
    },
    { value: "charcoal", label: COPY.icons.colourChip.charcoal, ariaLabel: COPY.icons.colourChip.charcoal, title: `${COPY.icons.colourChip.charcoal} (${CHARCOAL})`, icon: <SwatchDot colour={CHARCOAL} /> },
    { value: "white", label: COPY.icons.colourChip.white, ariaLabel: COPY.icons.colourChip.white, title: `${COPY.icons.colourChip.white} (${WHITE})`, icon: <SwatchDot colour={WHITE} /> },
  ];
  const pickColour = (v: string) =>
    onEdit(
      setIconColour(
        recipe,
        v === "auto" ? "auto" : v === "violet" ? VIOLET : v === "charcoal" ? CHARCOAL : v === "white" ? WHITE : patternColour ?? "auto",
      ),
    );

  const busy = plan.status === "waiting" || plan.status === "loading";
  const firstTime = !shown && (plan.status === "warming" || plan.status === "loading" || plan.status === "waiting");

  return (
    <div className="space-y-4">
      {omniOwn && (
        <Notice
          tone="warning"
          action={
            <span className="flex flex-wrap gap-1.5">
              {omniOwn.mode === "stack" && (
                <button type="button" className="btn btn-xs btn-secondary" onClick={omniOwn.onChangeIt}>
                  {COPY.changeIt}
                </button>
              )}
              {/* Made in house nothing tells Omni anything: added on top, its
                  video keeps its icons as they are (ours off); redone, they
                  are gone — there is nothing to keep. */}
              {(!isHouse(recipe) || omniOwn.mode === "stack") && (
                <button
                  type="button"
                  className="btn btn-xs btn-ghost"
                  title={COPY.icons.keepThemTitle}
                  onClick={() =>
                    onEdit(isHouse(recipe) ? setIconsEnabled(recipe, false) : setOmniOwn(recipe, "keep"))
                  }
                >
                  {COPY.icons.keepThem}
                </button>
              )}
            </span>
          }
        >
          <span className="font-medium">{COPY.icons.omniOwn(omniOwn.no)}</span>{" "}
          {omniOwn.mode === "stack"
            ? COPY.icons.omniOwnAdd
            : isHouse(recipe)
              ? COPY.icons.omniOwnHouse
              : COPY.icons.omniOwnChange}
        </Notice>
      )}
      {onVersion && !omniOwn && <p className="text-2xs leading-snug text-muted">{COPY.icons.onVersion(onVersion)}</p>}
      {keptNo && !onVersion && <p className="text-2xs leading-snug text-muted">{COPY.iconsKept(keptNo)}</p>}

      {!byHand && (
        <Switch
          checked={p.ai}
          onChange={(v) => onEdit(setIconsAi(recipe, v))}
          label={COPY.icons.auto}
          hint={p.ai ? COPY.icons.autoOn : COPY.icons.autoOff}
        />
      )}

      {firstTime && <Steps warming={plan.warming} since={plan.warmingSince} />}
      {plan.error && (
        <Notice
          tone={plan.error.retryable ? "warning" : "danger"}
          action={
            plan.error.retryable ? (
              <button type="button" className="btn btn-xs btn-ghost" onClick={plan.retry}>
                <RotateCcw className="h-3 w-3" aria-hidden /> {COPY.tryAgain}
              </button>
            ) : undefined
          }
        >
          <span className="font-medium">{plan.error.retryable ? COPY.icons.errorTitle : COPY.icons.refusedTitle}</span>{" "}
          {plan.error.message}
        </Notice>
      )}
      {shown && p.ai && !byHand && (shown.source?.director === "keywords" || plan.asking) && (
        <Notice
          tone="warning"
          action={
            <button
              type="button"
              className="btn btn-xs btn-ghost"
              onClick={plan.askAgain}
              // One take at a time: each is a paid call.
              disabled={plan.asking}
              aria-busy={plan.asking}
            >
              {plan.asking && <Spinner size="xs" tone="accent" />}
              {plan.asking ? COPY.icons.askingAgain : COPY.icons.askAgain}
            </button>
          }
        >
          {COPY.icons.keywordsOnly}
        </Notice>
      )}
      {plan.asking && (
        <Steps warming={plan.askingSteps} since={plan.askingSince} title={COPY.icons.askingAgain} firstTime={false} />
      )}

      {shown && (
        <section className="space-y-2">
          <div className="flex items-center gap-2">
            <h4 className="eyebrow flex-1">
              {COPY.icons.order} · {pins ? COPY.icons.orderPinned : COPY.icons.orderAuto}
            </h4>
            {plan.updating && (
              <span className="inline-flex items-center gap-1 text-2xs text-muted" role="status">
                <Spinner size="xs" tone="accent" /> {COPY.icons.updating}
              </span>
            )}
            {pins && (
              <button
                type="button"
                className="btn btn-xs btn-ghost"
                title={COPY.icons.resetOrderTitle}
                onClick={() => {
                  onChoice(null);
                  onEdit(resetOrder(recipe));
                }}
              >
                {COPY.icons.resetOrder}
              </button>
            )}
            {hasIconEdits(recipe) && (
              <button
                type="button"
                className="btn btn-xs btn-ghost"
                title={COPY.icons.resetAllTitle}
                onClick={() => {
                  onChoice(null);
                  onEdit(resetAllIcons(recipe));
                }}
              >
                {COPY.icons.resetAll}
              </button>
            )}
          </div>

          {items.length === 0 ? (
            <p className="rounded-xl border border-dashed border-border px-3 py-4 text-center text-xs text-muted">
              {p.ai && !byHand && shown.source?.director !== "keywords" ? COPY.icons.none : COPY.icons.noneAiOff}
            </p>
          ) : (
            <ul className={`space-y-1.5 ${busy ? "opacity-80" : ""}`} aria-label={COPY.icons.order}>
              {items.map((it, i) => {
                const id = glyphId(it.glyph);
                const mine = it.source === "user";
                const tweak = p.tweaks[id];
                const over = drag && drag.to === i && drag.from !== i;
                return (
                  <li
                    key={id}
                    ref={(n) => {
                      rowRefs.current[id] = n;
                    }}
                    className={`rounded-xl border bg-bg transition-colors ${
                      selected === id ? "border-accent/60 shadow-soft" : "border-border hover:border-border-strong"
                    } ${over ? (drag!.to < drag!.from ? "border-t-2 border-t-accent" : "border-b-2 border-b-accent") : ""} ${
                      drag?.from === i ? "opacity-60" : ""
                    }`}
                  >
                    <div className="flex min-h-[52px] items-center gap-1 py-1.5 pl-0.5 pr-1">
                      <button
                        type="button"
                        aria-label={COPY.icons.handle(it.label)}
                        title={COPY.icons.handle(it.label)}
                        onPointerDown={(e) => dragStart(e, i)}
                        onPointerMove={dragMove}
                        onPointerUp={dragEnd}
                        onPointerCancel={() => setDrag(null)}
                        onKeyDown={(e) => onRowKey(e, i)}
                        className="flex h-9 w-6 shrink-0 cursor-grab touch-none items-center justify-center rounded text-muted hover:text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 active:cursor-grabbing"
                      >
                        <GripVertical className="h-4 w-4" aria-hidden />
                      </button>
                      <button
                        id={iconRowId(id)}
                        type="button"
                        aria-pressed={selected === id}
                        aria-label={COPY.icons.rowAria(it.label, fmtSec(iconTime(it)), it.word)}
                        onClick={() => onSelect(selected === id ? null : id)}
                        onKeyDown={(e) => onRowKey(e, i)}
                        className="flex min-w-0 flex-1 items-center gap-2.5 rounded-lg px-1 py-0.5 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
                      >
                        <span className="relative shrink-0">
                          <IconGlyph
                            glyph={it.glyph}
                            d={it.d}
                            size={30}
                            look={{ style: it.style ?? p.style, fill: it.fill ?? fill, ink: it.ink ?? ink, weight: it.weight }}
                          />
                          {(it.edited?.look || it.edited?.place) && (
                            <span
                              className="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full border border-bg bg-accent"
                              title={COPY.icons.edited}
                              aria-label={COPY.icons.edited}
                            />
                          )}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="flex items-center gap-1.5">
                            <span className="truncate text-xs font-semibold text-fg">{it.text && it.style === "chip" ? it.text : it.label}</span>
                            <span className="shrink-0 font-mono text-2xs tabular-nums text-muted">{fmtSec(iconTime(it))}</span>
                            <span
                              className={`shrink-0 rounded-full px-1.5 text-3xs font-medium ${
                                it.off_word
                                  ? "bg-amber-100 text-amber-800"
                                  : mine || it.pinned
                                    ? "bg-fg/10 text-fg"
                                    : "border border-dashed border-border-strong text-muted"
                              }`}
                              title={it.why?.by === "kept" ? COPY.icons.keptPickTitle : undefined}
                            >
                              {COPY.icons[iconTag(it)]}
                            </span>
                          </span>
                          <span
                            className="block truncate text-2xs text-muted"
                            title={laneOf(it) > 0 ? COPY.icons.ownTrack : undefined}
                          >
                            {[
                              laneOf(it) > 0 ? COPY.icons.track(laneOf(it)) : null,
                              ...(it.off_word ? [COPY.icons.offWordPurpose] : [it.word ? COPY.icons.onWord(it.word) : null, it.purpose]),
                            ]
                              .filter(Boolean)
                              .join(" · ")}
                          </span>
                        </span>
                      </button>
                      {it.source === "ai" && (
                        // ✓ Keep: the AI's pick becomes yours, so it stays
                        // whatever the AI picks next (a new take, Ask again).
                        <button
                          type="button"
                          className="btn btn-xs btn-ghost shrink-0 gap-0.5 px-1.5"
                          disabled={addedFull}
                          title={addedFull ? COPY.icons.capTitle(ADDED_MAX) : COPY.icons.keepPickTitle}
                          aria-label={COPY.icons.keepPickAria(it.label)}
                          onClick={() => onEdit(addIcon(recipe, it.glyph, it.label))}
                        >
                          <Check className="h-3 w-3" aria-hidden /> {COPY.icons.keepPickButton}
                        </button>
                      )}
                      {it.why && (
                        <button
                          type="button"
                          className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-muted hover:text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 ${
                            whyOpen === id ? "bg-accent-fade text-accent-hover" : ""
                          }`}
                          aria-expanded={whyOpen === id}
                          aria-label={COPY.icons.whyAria(it.label)}
                          title={COPY.icons.whyTitle}
                          onClick={() => setWhyOpen(whyOpen === id ? null : id)}
                        >
                          <Info className="h-3.5 w-3.5" aria-hidden />
                        </button>
                      )}
                      <OverflowMenu label={COPY.icons.rowMenu(it.label)} size="sm">
                        <MenuItem onClick={() => onChange(it, document.getElementById(iconRowId(id)))}>{COPY.icons.change}</MenuItem>
                        <MenuItem
                          onClick={() => {
                            onChoice(null);
                            if (selected === id) onSelect(null);
                            onEdit(removeIcon(recipe, it.glyph));
                          }}
                        >
                          {COPY.icons.remove}
                        </MenuItem>
                        <MenuItem onClick={() => onPlayFrom(it)}>{COPY.icons.playFrom}</MenuItem>
                      </OverflowMenu>
                    </div>
                    {it.off_word && tweak?.off_word && (
                      <p className="mx-3 -mt-0.5 mb-2 text-2xs text-amber-800">
                        <button
                          type="button"
                          className="font-medium underline-offset-2 hover:underline"
                          onClick={() => onEdit(setTweak(recipe, id, { off_word: false, sec: null }))}
                        >
                          {COPY.icons.snapBack}
                        </button>
                      </p>
                    )}
                    {it.shifted && !it.off_word && (
                      // Its word (or the moment it was added at) is in a zone
                      // (the opening, a pattern coming in or going out, the
                      // end): moved to the nearest time allowed. "Anyway"
                      // times it by hand at that moment, else on its word.
                      <p className="mx-3 -mt-0.5 mb-2 flex flex-wrap items-center gap-x-2 text-2xs text-amber-800">
                        <span>{COPY.icons.shifted(fmtSec(iconTime(it)), it.shifted.why)}</span>
                        <button
                          type="button"
                          className="font-medium underline-offset-2 hover:underline"
                          title={COPY.icons.showAnywayTitleOf(it.shifted)}
                          onClick={() => onEdit(pinOffWord(recipe, id, anywayOf(it.shifted!).sec))}
                        >
                          {COPY.icons.showAnyway(fmtSec(anywayOf(it.shifted).label))}
                        </button>
                      </p>
                    )}
                    {whyOpen === id && it.why && <WhyLines item={it} />}
                    {selected === id && (
                      <IconLookPanel
                        recipe={recipe}
                        item={it}
                        sceneFill={fill}
                        sceneColours={sceneColours}
                        rules={rules}
                        frame={frame}
                        onEdit={onEdit}
                        layer={layers}
                      />
                    )}
                    {!it.word && !it.off_word && said.length > 0 && (
                      <div className="mx-3 -mt-0.5 mb-2 flex flex-wrap items-center gap-1.5 text-2xs text-amber-800">
                        <AlertTriangle className="h-3 w-3 shrink-0" aria-hidden />
                        {COPY.icons.notTied}
                        <select
                          aria-label={COPY.icons.pickWordLabel(it.label)}
                          value=""
                          onChange={(e) => e.target.value && onEdit(pinToWord(recipe, id, Number(e.target.value), digest))}
                          className="rounded border border-amber-300 bg-bg px-1 py-0.5 text-2xs text-fg"
                        >
                          <option value="">{COPY.icons.pickWord}</option>
                          {said.map((w) => (
                            <option key={w.w} value={w.w}>
                              {w.text} · {fmtSec(w.s)}
                            </option>
                          ))}
                        </select>
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}

          {choice && choice.from === "list" && (
            <IconChoiceRow
              choice={choice}
              onAnyway={() => {
                if (choice.anyway) onEdit(choice.anyway);
                onChoice(null);
              }}
              onKeep={() => onChoice(null)}
            />
          )}
          <p className="min-h-[1rem] text-2xs text-accent-hover" role="status" aria-live="polite">
            {note}
          </p>
        </section>
      )}

      <div className="flex items-center gap-2">
        <button
          id={ADD_ICON_ID}
          type="button"
          className="btn btn-sm btn-ghost gap-1.5"
          disabled={addedFull}
          title={addedFull ? COPY.icons.capTitle(ADDED_MAX) : undefined}
          onClick={(e) => onAdd(e.currentTarget)}
        >
          <Plus className="h-3.5 w-3.5" aria-hidden /> {COPY.icons.add}
        </button>
        <span className="ml-auto text-2xs tabular-nums text-muted" title={COPY.icons.capTitle(rules.items_max)}>
          {COPY.icons.count(count, rules.items_max)}
        </span>
      </div>

      {shown && mineOff.length > 0 && (
        <section className="space-y-1.5" aria-label={COPY.icons.notOnScreen}>
          <h4 className="eyebrow">{COPY.icons.notOnScreen}</h4>
          <ul className="space-y-1">
            {mineOff.map((idea) => (
              <IdeaRow
                key={`${glyphId(idea.glyph)}:${idea.w ?? ""}`}
                idea={idea}
                items={items}
                recipe={recipe}
                onEdit={onEdit}
                said={said}
                digest={digest}
              />
            ))}
          </ul>
        </section>
      )}

      <section className="space-y-3 border-t border-border pt-3" aria-label={COPY.icons.allIcons}>
        <h4 className="eyebrow">{COPY.icons.allIcons}</h4>
        {isHouse(recipe) && layers && items.length > 0 && (
          <div className="space-y-1.5">
            <span className="block text-2xs font-medium text-fg">{COPY.icons.layer}</span>
            <ChoiceChips
              label={COPY.icons.layer}
              options={[
                { value: "behind", label: COPY.icons.behind },
                { value: "front", label: COPY.icons.front, title: COPY.icons.frontHint },
              ]}
              value={allLayer}
              onChange={(v) =>
                onEdit(items.reduce((r, it) => setLook(r, glyphId(it.glyph), { layer: v === "front" ? "front" : null }), recipe))
              }
            />
          </div>
        )}
        <div className="space-y-1.5">
          <span className="text-2xs font-medium text-fg">{COPY.icons.colour}</span>
          <div className="flex flex-wrap items-center gap-2.5">
            <ChoiceChips variant="swatch" label={COPY.icons.colour} options={colourOptions} value={colourValue} onChange={pickColour} />
            <PickColour
              value={isHex(p.colour) ? p.colour : fill}
              checked={p.colour !== "auto" && colourValue === null}
              onCommit={(hex) => onEdit(setIconColour(recipe, hex))}
              label={COPY.pickColour}
              title={COPY.pickColourTitle}
            />
          </div>
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-3">
          <div className="space-y-1.5">
            <span className="block text-2xs font-medium text-fg">{COPY.icons.style}</span>
            <ChoiceChips<HarnessIconStyle>
              label={COPY.icons.style}
              options={[
                ...NEW_STYLES.map((s) => ({ value: s as HarnessIconStyle, label: s === "circle" ? COPY.icons.styleCircle : COPY.icons.styleChip })),
                // The older looks: offered only while this draft has them.
                ...(usesOldStyle(recipe)
                  ? OLD_STYLES.map((s) => ({
                      value: s as HarnessIconStyle,
                      label: s === "badge" ? COPY.icons.styleBadge : COPY.icons.styleGlyph,
                      title: COPY.icons.styleOldTitle,
                    }))
                  : []),
              ]}
              value={p.style}
              onChange={(s) => onEdit(setIconStyle(recipe, s))}
            />
          </div>
        </div>
        <div className="space-y-1.5">
          <span className="block text-2xs font-medium text-fg">{COPY.icons.enter}</span>
          <ChoiceChips
            label={COPY.icons.enter}
            options={ICON_ENTRANCES.map((e) => ({ value: e, label: COPY.icons.enterChip[e] }))}
            value={entranceOf(el)}
            onChange={(e: IconEntrance) => onEdit(setIconEntrance(recipe, e))}
          />
        </div>
      </section>

      <button
        type="button"
        className="text-2xs font-medium text-muted underline-offset-2 hover:text-fg hover:underline"
        onClick={() => {
          onChoice(null);
          onEdit(setIconsEnabled(recipe, false));
        }}
      >
        {COPY.icons.turnOff}
      </button>
    </div>
  );
}

/** A shown icon's "Why?" (the plan item's `why`): who picked it, what was
 *  said and when, why it helps, where it sits and why that colour. */
function WhyLines({ item }: { item: HarnessIconItem }) {
  const why = item.why;
  if (!why) return null;
  const lines = [
    COPY.icons.whyWho(why.by, why.priority),
    why.time === "hand"
      ? COPY.icons.whyTimed(fmtSec(iconTime(item)))
      : why.said
        ? COPY.icons.whySaid(why.said.word, why.said.at != null ? fmtSec(why.said.at) : null, why.time === "pinned")
        : null,
    why.purpose ? COPY.icons.whyHelps(why.purpose) : null,
    COPY.icons.whyWhere(why.place.by, why.place.text),
    why.colour ? COPY.icons.whyColour(why.colour.by, why.colour.text) : null,
  ].filter((l): l is string => !!l);
  return (
    <ul className="mx-3 -mt-0.5 mb-2 space-y-0.5 rounded-lg bg-bg-muted/60 px-2.5 py-2 text-2xs leading-snug text-fg" aria-label={COPY.icons.whyAria(item.label)}>
      {lines.map((l) => (
        <li key={l}>{l}</li>
      ))}
    </ul>
  );
}

/** Why an idea isn't on screen, in words: who left it out and why (the
 *  idea's `why`), else its reason. */
function whyNot(idea: HarnessIconIdea): string | null {
  const why = idea.why;
  const reason = idea.reason ? sentence(idea.reason) : null;
  if (!why) return reason || (idea.purpose ? sentence(idea.purpose) : null);
  if (why.by === "hopeful") return COPY.icons.hopeful;
  if (why.by === "team") return COPY.icons.teamSaid(idea.at != null ? fmtSec(idea.at) : null);
  if (why.by === "silent") return COPY.icons.whyNot.silent;
  const lead = COPY.icons.whyNot[why.by];
  if (lead && why.text) return `${lead}: ${sentence(why.text)}`;
  return lead ?? reason ?? (idea.purpose ? sentence(idea.purpose) : null);
}

/** An icon the user added that no word they say is tied to (it waits off
 *  screen until one is picked). */
function looseAdd(idea: HarnessIconIdea): boolean {
  return !!idea.added && idea.w == null && idea.state === "available";
}

/** One of yours that isn't on screen, with why and [Remove] — for one tied
 *  to nothing said, the word to land it on. */
function IdeaRow({
  idea,
  items,
  recipe,
  onEdit,
  said,
  digest,
}: {
  idea: HarnessIconIdea;
  /** The plan's icons: the time an idea says is its icon's when shown
   *  (ideaTime), else when it is said. */
  items: HarnessIconItem[];
  recipe: HarnessRecipe;
  onEdit: (next: HarnessRecipe) => void;
  said: { w: number; text: string; s: number }[];
  digest: string | null;
}) {
  const id = glyphId(idea.glyph);
  const p = iconProps(iconsOf(recipe));
  const added = p.added.some((a) => glyphId(a.glyph) === id);
  const loose = added && looseAdd(idea) && said.length > 0;
  const chip = loose ? COPY.icons.needsWord : COPY.icons.state[idea.state] ?? idea.state;
  const line = whyNot(idea);
  const t = ideaTime(idea, items);
  const at = t && (t.said ? COPY.icons.saidAt(fmtSec(t.t)) : fmtSec(t.t));
  return (
    <li className="flex items-start gap-2 rounded-lg px-1.5 py-1.5 hover:bg-bg-muted/60">
      <IconGlyph glyph={idea.glyph} size={20} className="mt-0.5 text-fg/70" />
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-x-1.5">
          <span className="text-xs font-medium text-fg">{idea.label}</span>
          {idea.word && (
            <span className="text-2xs text-muted">
              “{idea.word}”{at ? ` · ${at}` : ""}
            </span>
          )}
          <span
            className={`rounded-full px-1.5 text-3xs font-medium ${
              loose
                ? "bg-amber-100 text-amber-800"
                : idea.state === "refused"
                  ? "bg-red-50 text-red-700"
                  : idea.state === "available"
                    ? "bg-accent-fade text-accent-hover"
                    : "bg-amber-100 text-amber-800"
            }`}
          >
            {chip}
          </span>
          {idea.source === "team" && (
            <span className="rounded-full bg-fg/10 px-1.5 text-3xs font-medium text-fg" title={COPY.icons.teamTitle}>
              {COPY.icons.teamTag}
            </span>
          )}
        </span>
        {line && <span className="block text-2xs leading-snug text-muted">{line}</span>}
        {loose && (
          <select
            aria-label={COPY.icons.pickWordLabel(idea.label)}
            value=""
            onChange={(e) => e.target.value && onEdit(pinToWord(recipe, id, Number(e.target.value), digest))}
            className="mt-1 rounded border border-amber-300 bg-bg px-1 py-0.5 text-2xs text-fg"
          >
            <option value="">{COPY.icons.pickWord}</option>
            {said.map((w) => (
              <option key={w.w} value={w.w}>
                {w.text} · {fmtSec(w.s)}
              </option>
            ))}
          </select>
        )}
      </span>
      <button type="button" className="btn btn-xs btn-ghost shrink-0" onClick={() => onEdit(removeIcon(recipe, idea.glyph))}>
        {COPY.icons.removeIdea}
      </button>
    </li>
  );
}
