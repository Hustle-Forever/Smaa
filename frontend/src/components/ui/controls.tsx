/**
 * The Trends page's control system: search, filter disclosure, active-filter
 * chips, an overflow menu and a compact notice.
 *
 * Why this file exists. The feed used to put every control it owns on screen at
 * once: a search box, five selects, a toggle, a weights button, a count, three
 * undo chips, Clear filters, an unscored warning and Re-score all, in two rows
 * above the grid. Nothing there was broken, but a reader looking for one of
 * them had to read all eleven, and the row was the tallest thing above the
 * fold. The fix is not to hide controls, it is to rank them:
 *
 *   always on screen   search, sort, the one toggle people use, Filters
 *   one click away     region, format, theme, min fit, show, ranking weights
 *   accounted for      a chip per filter that is actually hiding rows
 *   rare, in a menu    Re-score all, and anything else run once a month
 *
 * The chips are the load-bearing part. A feed that quietly drops a quarter of
 * its rows is a feed nobody can trust, so every filter that removes something
 * names itself in a chip and that chip is its own one-click undo. Folding
 * controls into a panel is only safe because of that: the panel hides the
 * CONTROL, never the CONSEQUENCE.
 *
 * Presentational only. Nothing here owns filter state, fetches, or knows what a
 * trend is. Callers keep their state exactly where it was.
 *
 * Every colour band is a COMPLETE class string in a map. Tailwind's content
 * scanner only reads literals, so `"bg-" + tone` would compile to nothing.
 */
import { useEffect, useId, useRef, useState } from "react";
import type { ComponentType, ReactNode, RefObject } from "react";
import { MoreHorizontal, Search, SlidersHorizontal, X } from "lucide-react";

// ---------------------------------------------------------------------------
// Toolbar
// ---------------------------------------------------------------------------

/**
 * The one row of controls that never collapses.
 *
 * Wraps rather than scrolls: a horizontally clipped control row is a control
 * nobody can reach on a narrow screen, and `items-center` keeps buttons and
 * inputs of different heights on one optical line.
 */
export function Toolbar({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`flex flex-wrap items-center gap-2 ${className}`}>
      {children}
    </div>
  );
}

/** Pushes what follows to the right of the toolbar, and does nothing on a
 *  wrapped row where "right" would be a line of its own. */
export function ToolbarSpacer() {
  return <div className="ml-auto" />;
}

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

/**
 * The feed's search box.
 *
 * Three things live inside the field rather than beside it, because the field
 * is the widest control in the row and has the space: the magnifier, the "/"
 * shortcut hint, and the clear button. The hint and the clear button occupy the
 * same slot and are mutually exclusive, so the text never has to share a
 * position with either.
 *
 * The shortcut has to be VISIBLE to exist. It is hidden once there is text,
 * where it would sit on top of the last characters typed and has nothing left
 * to say anyway, since the box already has focus by then.
 */
export function SearchField({
  value,
  onChange,
  onClear,
  placeholder,
  inputRef,
  label = "Search",
  hint = "/",
  className = "",
}: {
  value: string;
  onChange: (v: string) => void;
  /** Called by the clear button AND by Escape, so both empty the same things. */
  onClear: () => void;
  placeholder: string;
  inputRef?: RefObject<HTMLInputElement>;
  /** Accessible name. Visible labels live on the panel's fields, not here:
   *  a magnifier plus a placeholder is the one input pattern readers do not
   *  need taught, and a label above it would make this row two lines tall. */
  label?: string;
  /** Keyboard hint drawn in the field. Pass "" to draw none. */
  hint?: string;
  className?: string;
}) {
  return (
    <div className={`relative ${className}`}>
      <Search
        className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted"
        aria-hidden
      />
      <input
        ref={inputRef}
        type="search"
        aria-label={label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          // Escape empties the box AND whatever query it applied. Clearing only
          // the input would leave the feed filtered by a term that is no longer
          // written anywhere on screen.
          if (e.key !== "Escape") return;
          e.stopPropagation();
          onClear();
          e.currentTarget.blur();
        }}
        placeholder={placeholder}
        // `appearance-none` kills WebKit's own search cancel button: it renders
        // a second, differently styled X on top of ours.
        className="w-full appearance-none rounded border border-border bg-bg-subtle py-1.5 pl-8 pr-9 text-sm text-fg placeholder:text-muted/70 transition-colors focus:border-accent focus:outline-none [&::-webkit-search-cancel-button]:appearance-none"
      />
      {value ? (
        <button
          type="button"
          onClick={onClear}
          aria-label="Clear search"
          // 24x24 minimum pointer target (WCAG 2.2 AA), which is larger than
          // the 12px glyph it draws.
          className="absolute right-1.5 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded text-muted transition-colors hover:bg-bg-muted hover:text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        >
          <X className="h-3.5 w-3.5" aria-hidden />
        </button>
      ) : hint ? (
        <kbd
          aria-hidden
          className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 rounded border border-border bg-bg-subtle px-1.5 py-0.5 font-mono text-3xs leading-none text-muted"
        >
          {hint}
        </kbd>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Filter disclosure
// ---------------------------------------------------------------------------

/**
 * The button that opens the filter panel, carrying a count of how many filters
 * are currently set.
 *
 * The count is the whole reason this is safe. Collapsing controls is only
 * honest if the collapsed state still says how much is going on behind it, and
 * the badge is what turns "Filters" from a drawer into a status.
 *
 * The badge's slot is reserved whether or not there is a number in it, so
 * setting the first filter does not shove every control to its right along the
 * row. That is `content-jumping` in the UX rules and it is very visible here,
 * because the thing to the right is usually the button someone is aiming at.
 */
export function FilterToggle({
  open,
  count,
  onClick,
  panelId,
  title = "Region, format, theme, fit and what to show",
}: {
  open: boolean;
  count: number;
  onClick: () => void;
  /** The id of the FilterPanel this controls, for `aria-controls`. */
  panelId: string;
  /** What is behind the button while nothing is set. The default names the
   *  feed's axes; a screen with different filters passes its own, because a
   *  tooltip promising "format, theme, fit" over a panel of three other
   *  things is a small lie the reader catches on the first click. */
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-expanded={open}
      aria-controls={panelId}
      title={
        count > 0
          ? `${count} filter${count === 1 ? "" : "s"} set. Open the panel to change them.`
          : title
      }
      className={`btn text-xs ${open || count > 0 ? "btn-accent" : "btn-ghost"}`}
    >
      <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden />
      Filters
      {/* Fixed width so the label never moves. `aria-hidden` because the
          button's own title already says the count in a sentence, and a bare
          "2" read out after "Filters" is a riddle. */}
      <span
        aria-hidden
        className="inline-flex h-4 w-4 items-center justify-center rounded-full font-mono text-3xs font-bold tabular-nums"
        style={{
          background: count > 0 ? "rgba(255,255,255,0.22)" : "transparent",
        }}
      >
        {count > 0 ? count : ""}
      </span>
    </button>
  );
}

/**
 * The panel itself: a titled surface over a responsive grid of `Field`s.
 *
 * A REAL SURFACE, not a grey slab. It used to be `bg-bg-muted`, the same fill
 * the selects inside it use, so the controls had nothing to sit on and the
 * whole thing read as a smudge under the toolbar. It is the page's own
 * background now, lifted on a soft shadow, which is the treatment every other
 * raised block in this app already uses. One idea, one place.
 *
 * THE RESET MOVED TO THE HEADER. It was a footer under its own divider, which
 * put the one control that undoes everything at the bottom of a box whose
 * height changes with the screen width, so it landed somewhere different every
 * time. In the header it is always in the same corner, always visible, and the
 * divider it needed is gone.
 *
 * `hidden` rather than unmounting, so a select keeps its scroll position and
 * the browser keeps its own autofill state between openings. `[hidden]` is
 * respected by the base stylesheet, so a closed panel is out of the layout and
 * out of the tab order.
 */
export function FilterPanel({
  id,
  open,
  children,
  footer,
  title = "Narrow it down",
}: {
  id: string;
  open: boolean;
  children: ReactNode;
  /** The reset, and any control that acts on all of them. Sits in the header. */
  footer?: ReactNode;
  /** What this panel filters, in the panel's own words. */
  title?: string;
}) {
  return (
    <div
      id={id}
      hidden={!open}
      className="rounded-xl border border-border bg-bg p-4 shadow-soft sm:p-5"
    >
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <p className="eyebrow">{title}</p>
        {footer}
      </div>
      {/* Roomier than it was on both axes: these are labelled selects, and at
          gap-y-3 a label sat closer to the control above it than to its own. */}
      <div className="grid gap-x-5 gap-y-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {children}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Save bar
// ---------------------------------------------------------------------------

/**
 * The row that appears when there is something to save.
 *
 * ONLY WHEN DIRTY, and that is the whole design. A save button that is always
 * on screen and usually disabled teaches people to ignore it; one that appears
 * the moment they change something is the change telling them it is unsaved.
 *
 * STICKY TO THE BOTTOM, so it is reachable without scrolling back on a long
 * page. The caller is responsible for leaving room under its content, because
 * only the caller knows how much content there is.
 *
 * Extracted because the Settings page now holds sections that each used to be
 * their own screen, and each had its own answer to this: one a sticky bar, one
 * a plain button disabled unless dirty. Two patterns on one page is two things
 * to learn about the same job.
 */
export function SaveBar({
  dirty,
  saving,
  onSave,
  label = "Save",
  message = "Unsaved changes.",
  children,
}: {
  dirty: boolean;
  saving: boolean;
  onSave: () => void;
  /** The button's own word. "Save" unless the section does something more. */
  label?: string;
  /** What is unsaved, in the open. */
  message?: string;
  /** Anything else that belongs on this row, such as a second action. */
  children?: ReactNode;
}) {
  if (!dirty) return null;
  return (
    <div className="sticky bottom-4 z-10 animate-riseFade">
      <div
        className={[
          "flex flex-wrap items-center justify-between gap-3 rounded-xl px-4 py-3",
          "bg-bg-subtle/95 backdrop-blur supports-[backdrop-filter]:bg-bg-subtle/85",
          "border border-border shadow-lift",
        ].join(" ")}
      >
        <p className="text-sm text-muted">
          <span className="font-medium text-fg">{message}</span>{" "}
          Press save to keep them.
        </p>
        <div className="flex items-center gap-2">
          {children}
          <button
            type="button"
            onClick={onSave}
            disabled={saving}
            className="btn btn-primary"
          >
            {saving ? "Saving…" : label}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Active-filter chips
// ---------------------------------------------------------------------------

/**
 * Wraps before it shrinks. The `chip-collection-reflow` rule: a clipped chip
 * row hides values, and a hidden value is a filter the reader cannot undo.
 */
export function ChipRow({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">{children}</div>
  );
}

/** Complete literals, one per tone. */
const CHIP_TONE = {
  /** A filter the reader set. Neutral: it is doing what they asked. */
  filter:
    "border-border bg-bg-subtle text-fg hover:border-border-strong",
  /** Rows being withheld by a default rather than by a choice. Amber, because
   *  this one is telling the reader something they did not ask for. */
  hidden:
    "border-warning/30 bg-warning/10 text-warning hover:border-warning/50",
} as const;

const CHIP_X_TONE = {
  filter: "hover:bg-bg-muted hover:text-fg",
  hidden: "hover:bg-warning/20",
} as const;

/**
 * One active filter, and its undo.
 *
 * `label` names the axis and `value` the setting, so a row of chips reads as a
 * sentence: Theme robotics, Format Carousel, Fit 60 and up. The X is a real
 * button at 24x24 rather than a decorated span, so it is tabbable, has its own
 * accessible name, and meets the pointer-target minimum.
 *
 * Colour is never the only signal: `hidden` chips say the number and the reason
 * in words, and the tone only reinforces it.
 */
export function FilterChip({
  label,
  value,
  onRemove,
  removeLabel,
  title,
  tone = "filter",
  disabled = false,
}: {
  /** The axis. Omit for a chip whose value speaks for itself. */
  label?: string;
  value: ReactNode;
  onRemove: () => void;
  /** Accessible name for the X. Defaults to a sentence built from the label. */
  removeLabel?: string;
  title?: string;
  tone?: keyof typeof CHIP_TONE;
  /**
   * Makes the X inert while something else owns the value.
   *
   * The case this exists for: a chip standing for a value that is part of a
   * form currently being saved. The request on the wire does not carry the
   * removal, and the reply overwrites local state with what the server still
   * has, so a click during the save looks like it worked and then silently
   * undoes itself. Disabled rather than a no-op handler, because a control
   * that looks live and does nothing is the worse of the two.
   */
  disabled?: boolean;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded-full border py-0.5 pl-2.5 pr-0.5 text-xs transition-colors ${CHIP_TONE[tone]}`}
    >
      {label && (
        <span className="font-mono text-3xs uppercase tracking-wider2 opacity-70">
          {label}
        </span>
      )}
      <span className="max-w-[14rem] truncate font-medium">{value}</span>
      <button
        type="button"
        onClick={onRemove}
        disabled={disabled}
        aria-label={
          removeLabel ?? `Remove filter: ${label ? `${label} ` : ""}${String(value)}`
        }
        className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-40 ${CHIP_X_TONE[tone]}`}
      >
        <X className="h-3 w-3" aria-hidden />
      </button>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Overflow menu
// ---------------------------------------------------------------------------

/**
 * Complete literals, one per size. The trigger has to be the height of
 * whatever it sits beside, and the two places a kebab lives are different
 * heights: a toolbar's SearchField and selects are 34px, which `.btn` with
 * small type happens to match, while a row's `.btn-xs` / `.btn-sm` buttons are
 * 30px. A medium kebab beside small buttons is the one visibly uneven control
 * in an otherwise even cluster.
 */
const KEBAB_SIZE = {
  md: "btn text-xs px-2",
  sm: "btn btn-sm px-2",
} as const;

/**
 * The kebab: actions that are real but rare.
 *
 * Deliberately NOT `role="menu"`. That role promises arrow-key roving focus and
 * typeahead, which this does not implement, and a lying role is worse than no
 * role. It is a disclosure whose panel happens to contain buttons, so Tab
 * reaches every item and Escape closes it, which is what a keyboard user
 * actually gets. The same reasoning as the TabBar's plain buttons.
 *
 * Closes on Escape and on a pointer press outside. Escape returns focus to the
 * trigger, because leaving focus on a element that just vanished drops the
 * keyboard user back at the top of the document.
 */
export function OverflowMenu({
  label,
  children,
  align = "right",
  side = "bottom",
  size = "md",
}: {
  /** Accessible name for the trigger. Say what is inside, not "More". */
  label: string;
  children: ReactNode;
  align?: "left" | "right";
  /** "top" for a kebab at the foot of a clipped area: the panel opens upward. */
  side?: "top" | "bottom";
  /** "sm" for a kebab inside a row of `.btn-xs` / `.btn-sm` buttons. */
  size?: keyof typeof KEBAB_SIZE;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();

  useEffect(() => {
    if (!open) return;
    const onPointer = (e: PointerEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={wrapRef} className="relative">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={label}
        title={label}
        className={`${KEBAB_SIZE[size]} btn-ghost ${open ? "border-border-strong" : ""}`}
      >
        <MoreHorizontal className="h-4 w-4" aria-hidden />
      </button>

      <div
        id={panelId}
        hidden={!open}
        className={`absolute z-30 min-w-[13rem] rounded-lg border border-border bg-bg-subtle p-1 shadow-lift ${
          side === "top" ? "bottom-full mb-1" : "top-full mt-1"
        } ${align === "right" ? "right-0" : "left-0"}`}
      >
        {/* Clicking any item closes the menu. Every item in here is a one-shot
            action, so a menu that stayed open would be a panel the reader has
            to dismiss after every use.

            Focus goes back to the trigger on the way out, the same as the
            Escape path above. Without it the activated item is hidden while it
            still holds focus, and the browser drops focus to <body>, which
            sends the next Tab back to the top of the document. Today both
            shipped items open a confirm dialog that takes focus in the same
            commit, so this is only visible for an item that acts immediately.
            It is the kind of gap that is invisible until the day someone adds
            one. */}
        <div
          onClick={() => {
            setOpen(false)
            triggerRef.current?.focus()
          }}
        >
          {children}
        </div>
      </div>
    </div>
  );
}

/** One row in an OverflowMenu. A plain button, full width, left aligned. */
export function MenuItem({
  onClick,
  disabled,
  Icon,
  title,
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  Icon?: ComponentType<any>;
  title?: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className="flex w-full items-center gap-2 rounded px-2.5 py-2 text-left text-xs font-medium text-fg transition-colors hover:bg-bg-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-50"
    >
      {Icon && <Icon className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />}
      {children}
    </button>
  );
}

/** A non-interactive line of context inside an OverflowMenu. */
export function MenuNote({ children }: { children: ReactNode }) {
  return (
    <p className="px-2.5 py-1.5 text-2xs leading-relaxed text-muted">
      {children}
    </p>
  );
}

// ---------------------------------------------------------------------------
// Notice
// ---------------------------------------------------------------------------

const NOTICE_TONE = {
  info: "border-border bg-bg-muted text-fg",
  warning: "border-warning/30 bg-warning/10 text-fg",
  danger: "border-danger/30 bg-danger/10 text-fg",
  success: "border-success/30 bg-success/10 text-fg",
} as const;

const NOTICE_ICON_TONE = {
  info: "text-muted",
  warning: "text-warning",
  danger: "text-danger",
  success: "text-success",
} as const;

/**
 * One line of status, with an optional action.
 *
 * Replaces the page's several hand-rolled banners, which had drifted into three
 * different paddings and two different ways of saying the same thing. It is
 * deliberately ONE ROW tall by default: these appear above content the reader
 * came for, and a four-line amber box at the top of a page is read once and
 * then trained out.
 *
 * `role` follows the tone rather than being passed in, so a danger notice is
 * always announced and an informational one never interrupts.
 */
export function Notice({
  tone = "info",
  Icon,
  children,
  action,
  onDismiss,
  className = "",
}: {
  tone?: keyof typeof NOTICE_TONE;
  Icon?: ComponentType<any>;
  children: ReactNode;
  action?: ReactNode;
  /** Renders a dismiss button. Omit for a notice the reader cannot dismiss. */
  onDismiss?: () => void;
  className?: string;
}) {
  return (
    <div
      role={tone === "danger" ? "alert" : "status"}
      className={`flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg border px-3 py-2 text-xs leading-relaxed ${NOTICE_TONE[tone]} ${className}`}
    >
      {Icon && (
        <Icon
          className={`h-4 w-4 shrink-0 ${NOTICE_ICON_TONE[tone]}`}
          aria-hidden
        />
      )}
      <div className="min-w-0 flex-1">{children}</div>
      {action}
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Dismiss"
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-muted transition-colors hover:bg-bg-muted hover:text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        >
          <X className="h-3.5 w-3.5" aria-hidden />
        </button>
      )}
    </div>
  );
}

/**
 * The count under the toolbar: how much of the list is on screen.
 *
 * Its own component because it is the one number on the page that must never
 * jump. `tabular-nums` fixes the digit width and the wrapper reserves a line
 * whether or not there is a number yet, so the grid below does not step up by
 * a row the moment the first fetch lands.
 */
export function ResultCount({
  loading,
  shown,
  total,
  noun = "idea",
}: {
  loading: boolean;
  shown: number;
  total: number;
  noun?: string;
}) {
  return (
    <p
      // Polite rather than assertive: the number changes on every keystroke in
      // the search box, and an assertive region would talk over the typing.
      aria-live="polite"
      className="font-mono text-2xs text-muted tabular-nums"
    >
      {loading
        ? "Loading…"
        : `${shown} of ${total} ${noun}${total === 1 ? "" : "s"}`}
    </p>
  );
}
