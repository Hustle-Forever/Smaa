// Rebrand studio — picking icons: a view pushed into the rail (never a dialog
// over the dialog). It stays open, so several can be added in a row ("2
// added"); Done, Esc and Back return to the Icons tab.
//
//   Suggested   the ideas in what they say (the plan's), shown or not, in
//               time order, each with one time: shown, when its icon shows
//               (as the Icons list says it); not placed, when it is said
//               ("said at 6.0 s"): the one home for the AI's ideas it didn't
//               put on screen. Refused by the AI and gone from the library are
//               not offered; the AI's reason is the row's title; one you
//               removed comes back with a tap (restoreIdea: the AI's own
//               pick takes no add slot). What it listed is told (`onSeen`:
//               Generate says which ideas were seen and left)
//   Library     ONE search box, free: the whole library and your team's own
//               icons, ranked (a word, a name, a close spelling — any word
//               counts). Nothing typed: your team's icons (each with a ⋯
//               menu: rename, tags, delete), then the 42 picks. When nothing
//               fits well: "Generate with AI · ~$0.04", the only paid button
//               here, drawn only on that press (GenerateIconCard); when
//               something does, a quiet link. "Upload" (or a file dropped on
//               the tab) makes your own SVG or picture a team icon, free
//               (IconUpload).
//
// Changing an icon ("Change icon" on a row) is the same view: the one
// picked takes that icon's word.
//
// On an intro or outro nothing is said, so nothing is suggested: the view
// is Library alone (`suggested` false).

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
  type KeyboardEvent,
  type MutableRefObject,
} from "react";
import { ArrowLeft, Check, MoreHorizontal, Search, Upload } from "lucide-react";

import {
  harnessApi,
  type HarnessIconCatalogue,
  type HarnessIconGlyph,
  type HarnessIconIdea,
  type HarnessIconItem,
  type HarnessIconLibraryItem,
  type HarnessIconPlan,
  type HarnessIconSearch,
  type HarnessIconTeamItem,
  type HarnessRecipe,
} from "../../../lib/rebrandHarnessApi";
import {
  addBack,
  addIcon,
  ADDED_MAX,
  fmtSec,
  glyphId,
  iconProps,
  iconsOf,
  ideaTime,
  pickedState,
  removeIcon,
  restoreIdea,
  speechOrder,
  swapIcon,
  wordsDigestOf,
} from "../../../lib/rebrandIcons";
import { isShown } from "../../../lib/isShown";
import { rebrandErrorDetail } from "../../../lib/rebrandApi";
import { useConfirm } from "../../ConfirmProvider";
import { TabPanel, Tabs } from "../../ui/Tabs";
import { Spinner } from "../../ui/Spinner";
import { GenerateIconCard } from "./GenerateIconCard";
import { IconUpload, savedTags, tagsOf, UPLOAD_ACCEPT } from "./IconUpload";
import { rememberPaths } from "./iconArt";
import { IconGlyph } from "./IconGlyph";
import { COPY, sentence } from "./studioCopy";

type PickerTab = "suggested" | "library";

/** How many of the team's icons show before anything is typed. */
const TEAM_SHOWN = 24;

function glyphOf(it: { id: string; source?: string }): HarnessIconGlyph {
  return { source: it.source === "custom" ? "custom" : "library", id: it.id };
}

export function IconPicker({
  mode,
  item,
  recipe,
  plan,
  current = false,
  catalogue,
  onEdit,
  onBack,
  suggested = true,
  onSeen,
  at = null,
  onAdded,
}: {
  mode: "add" | "swap";
  /** The icon being changed (swap). */
  item: HarnessIconItem | null;
  recipe: HarnessRecipe;
  plan: HarnessIconPlan | null;
  /** `plan` is the draft's own (it has placed every add): an add it left
   *  out says why, not "being placed". */
  current?: boolean;
  catalogue: HarnessIconCatalogue | null;
  onEdit: (next: HarnessRecipe) => void;
  onBack: () => void;
  /** The Suggested tab (the ideas in what is said): not on an intro or
   *  outro, which opens on Library. */
  suggested?: boolean;
  /** The ideas Suggested listed (glyph ids), each time it lists them. */
  onSeen?: (glyphIds: string[]) => void;
  /** The playhead (scene seconds): what an add is added at (an icon nothing
   *  said is tied to shows at the clear time nearest it). */
  at?: number | null;
  /** An icon was added (its glyph id): the studio picks it once it shows. */
  onAdded?: (glyphId: string) => void;
}) {
  const idBase = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const onBackRef = useRef(onBack);
  onBackRef.current = onBack;
  // "Delete this icon?" is open: Esc is its Cancel, not Back.
  const confirming = useRef(false);
  const [tab, setTab] = useState<PickerTab>(mode === "swap" || !suggested ? "library" : "suggested");

  useEffect(() => {
    headingRef.current?.focus();
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape" || !isShown(headingRef.current)) return;
      e.stopPropagation();
      if (confirming.current) return;
      onBackRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    rememberPaths(catalogue?.picks.flatMap((g) => g.items));
  }, [catalogue]);

  const p = iconProps(iconsOf(recipe));
  const items = speechOrder(plan?.items);
  // On screen: what the plan shows (never just what was added: an add the
  // plan hasn't placed yet is "added", and says so).
  const onScreen = new Set(items.map((it) => glyphId(it.glyph)));
  const added = new Set(p.added.map((a) => glyphId(a.glyph)));
  // Picked (ticked; a tap takes it off): on screen, or added.
  const chosen = new Set([...onScreen, ...added]);
  const full = p.added.length >= ADDED_MAX;
  const digest = wordsDigestOf(plan);

  /** Tap on an icon: add it, at the playhead (or, picked already, take it
   *  off); in swap mode, put it in the changed icon's place. */
  const pick = (glyph: HarnessIconGlyph, label: string) => {
    if (mode === "swap" && item) {
      onEdit(swapIcon(recipe, item, glyph, label, digest));
      onBack();
      return;
    }
    const id = glyphId(glyph);
    if (chosen.has(id)) onEdit(removeIcon(recipe, glyph));
    else if (!full) {
      onEdit(addIcon(recipe, glyph, label, null, at));
      onAdded?.(id);
    }
    // Five of theirs already: one they removed may still come back as the AI's.
    else if (p.dismissed.includes(id)) onEdit(addBack(recipe, id));
  };
  /** A tile's title: on screen, added and on its way, or added and left
   *  out by the plan as it is now (and why). */
  const onTitle = (id: string) => {
    const got = pickedState(id, plan, current);
    if (got.kind === "on") return COPY.icons.onScreen;
    if (got.kind === "placing") return COPY.icons.addedNotShown;
    return COPY.icons.addedLeftOut(COPY.icons.state[got.state] ?? got.reason ?? got.state);
  };
  /** Tap on an idea removed by you: the AI's own pick is simply un-removed
   *  (no add slot), any other comes back as one of yours. */
  const restore = (idea: HarnessIconIdea) => onEdit(restoreIdea(recipe, idea));

  const title = mode === "swap" && item ? COPY.icons.pickerChange(item.label) : COPY.icons.pickerAdd;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-1.5 border-b border-border px-2 py-1.5">
        <button
          type="button"
          onClick={onBack}
          aria-label={COPY.back}
          title={COPY.back}
          className="h-8 w-8 rounded-lg flex items-center justify-center text-muted hover:text-fg hover:bg-bg-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden />
        </button>
        <h3 ref={headingRef} tabIndex={-1} className="min-w-0 flex-1 truncate text-xs font-semibold text-fg focus:outline-none">
          {title}
        </h3>
        {mode === "add" && (
          <>
            <span className="text-2xs tabular-nums text-muted" title={COPY.icons.capTitle(ADDED_MAX)}>
              {COPY.icons.addedCount(p.added.length)}
            </span>
            <button type="button" onClick={onBack} className="btn btn-xs btn-primary ml-1">
              {COPY.icons.done}
            </button>
          </>
        )}
      </div>

      <Tabs
        label={COPY.icons.tabsLabel}
        idBase={idBase}
        tabs={[
          ...(suggested ? [{ id: "suggested" as const, label: COPY.icons.tabSuggested }] : []),
          { id: "library" as const, label: COPY.icons.tabLibrary },
        ]}
        value={tab}
        onChange={setTab}
        className="shrink-0 px-2"
      />
      <TabPanel idBase={idBase} id={tab} className="min-h-0 flex-1 overflow-y-auto p-3">
        {tab === "suggested" && suggested && (
          <SuggestedTab
            plan={plan}
            recipe={recipe}
            chosen={chosen}
            onTitle={onTitle}
            full={full}
            onPick={pick}
            onRestore={restore}
            swap={mode === "swap"}
            onSeen={onSeen}
          />
        )}
        {tab === "library" && (
          <LibraryTab
            catalogue={catalogue}
            chosen={chosen}
            onTitle={onTitle}
            full={full}
            onPick={pick}
            swap={mode === "swap"}
            confirming={confirming}
          />
        )}
      </TabPanel>
    </div>
  );
}

function Tile({
  glyph,
  d,
  name,
  team,
  on,
  onLabel = COPY.icons.onScreen,
  disabled,
  swap,
  onPick,
}: {
  glyph: HarnessIconGlyph;
  d?: string | null;
  name: string;
  /** One of your team's own icons (a small "Team" mark). */
  team?: boolean;
  /** Picked: on screen, or added and being placed (`onLabel` says which). */
  on: boolean;
  onLabel?: string;
  disabled: boolean;
  swap: boolean;
  onPick: () => void;
}) {
  return (
    <button
      type="button"
      role={swap ? undefined : "checkbox"}
      aria-checked={swap ? undefined : on}
      disabled={disabled && !on}
      onClick={onPick}
      title={on && !swap ? onLabel : disabled ? COPY.icons.capTitle(ADDED_MAX) : swap ? COPY.icons.tapToUse : COPY.icons.tapToAdd}
      className={`relative flex w-full min-w-0 flex-col items-center gap-1 rounded-lg border px-1 py-2 text-3xs transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-40 ${
        on ? "border-accent bg-accent-fade text-accent-hover" : "border-border text-muted hover:border-border-strong hover:text-fg"
      }`}
    >
      <IconGlyph glyph={glyph} d={d} size={24} className="text-fg" />
      <span className="w-full truncate text-center">{name}</span>
      {team && (
        <span className="absolute left-1 top-1 rounded-full bg-bg-muted px-1 text-[9px] leading-[14px] text-muted" title={COPY.icons.teamTitle}>
          {COPY.icons.teamTag}
        </span>
      )}
      {on && !swap && (
        <span className="absolute right-1 top-1 flex h-3.5 w-3.5 items-center justify-center rounded-full bg-accent text-white" aria-hidden>
          <Check className="h-2.5 w-2.5" strokeWidth={3} />
        </span>
      )}
    </button>
  );
}

function SuggestedTab({
  plan,
  recipe,
  chosen,
  onTitle,
  full,
  onPick,
  onRestore,
  swap,
  onSeen,
}: {
  plan: HarnessIconPlan | null;
  recipe: HarnessRecipe;
  /** On screen, or added (a tap takes it off). */
  chosen: Set<string>;
  onTitle: (id: string) => string;
  full: boolean;
  onPick: (g: HarnessIconGlyph, label: string) => void;
  onRestore: (idea: HarnessIconIdea) => void;
  swap: boolean;
  onSeen?: (glyphIds: string[]) => void;
}) {
  // Refused by the AI, or gone from the library: nothing to offer (one of
  // yours that is gone is listed on the Icons tab, to remove). In the order
  // of the time each says (ideaTime: when its icon shows, as the icon list
  // says it; not placed, when it is said).
  const when = (i: HarnessIconIdea) => ideaTime(i, plan?.items);
  const ideas = (plan?.ideas ?? [])
    .filter((i) => i.state !== "refused" && i.state !== "gone")
    .sort((a, b) => (when(a)?.t ?? 99) - (when(b)?.t ?? 99));
  // What was listed here: seen.
  const listed = ideas.map((i) => glyphId(i.glyph)).join(",");
  const onSeenRef = useRef(onSeen);
  onSeenRef.current = onSeen;
  useEffect(() => {
    if (listed) onSeenRef.current?.(listed.split(","));
  }, [listed]);
  const dismissed = iconProps(iconsOf(recipe)).dismissed;
  if (!ideas.length) return <p className="text-2xs text-muted">{COPY.icons.suggestedNone}</p>;
  return (
    <div className="space-y-2">
      <p className="text-2xs text-muted">{COPY.icons.suggestedHint}</p>
      <ul className="space-y-1">
        {ideas.map((idea) => {
          const id = glyphId(idea.glyph);
          const on = chosen.has(id);
          const removed = !on && (idea.state === "dismissed" || dismissed.includes(id));
          // The AI's own pick, removed, comes back without an add slot.
          const blocked = full && !on && !swap && !(removed && idea.auto);
          // A chip only when it isn't simply there to add: why the AI's left
          // it out (No room, Pattern moving, Near the end…), Removed by you.
          const state = on ? null : removed ? "dismissed" : idea.state === "available" || idea.state === "on" ? null : idea.state;
          const why = sentence(idea.reason || idea.purpose);
          const t = when(idea);
          const at = t && (t.said ? COPY.icons.saidAt(fmtSec(t.t)) : fmtSec(t.t));
          return (
            <li key={`${id}:${idea.w ?? ""}`}>
              <button
                type="button"
                role={swap ? undefined : "checkbox"}
                aria-checked={swap ? undefined : on}
                disabled={blocked}
                title={on && !swap ? onTitle(id) : why || undefined}
                onClick={() => (removed && !swap ? onRestore(idea) : onPick(idea.glyph, idea.label))}
                className={`flex w-full items-center gap-2.5 rounded-lg border px-2 py-2 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:opacity-40 ${
                  on ? "border-accent bg-accent-fade" : "border-border hover:border-border-strong"
                }`}
              >
                <IconGlyph glyph={idea.glyph} size={22} className="text-fg" />
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-x-1.5">
                    <span className="text-xs font-medium text-fg">{idea.label}</span>
                    {(idea.word || at) && (
                      <span className="text-2xs text-muted">
                        {idea.word ? `“${idea.word}”` : ""}
                        {idea.word && at ? " · " : ""}
                        {at}
                      </span>
                    )}
                    {state && (
                      <span className="rounded-full bg-bg-muted px-1.5 text-3xs text-muted">{COPY.icons.state[state] ?? state}</span>
                    )}
                    {idea.source === "team" && (
                      <span className="rounded-full bg-fg/10 px-1.5 text-3xs font-medium text-fg" title={COPY.icons.teamTitle}>
                        {COPY.icons.teamTag}
                      </span>
                    )}
                  </span>
                </span>
                {on && <Check className="h-3.5 w-3.5 shrink-0 text-accent-hover" aria-hidden />}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function LibraryTab({
  catalogue,
  chosen,
  onTitle,
  full,
  onPick,
  swap,
  confirming,
}: {
  catalogue: HarnessIconCatalogue | null;
  /** On screen, or added (a tap takes it off). */
  chosen: Set<string>;
  onTitle: (id: string) => string;
  full: boolean;
  onPick: (g: HarnessIconGlyph, label: string) => void;
  swap: boolean;
  /** Set while a confirm is open (Esc is its, not the picker's). */
  confirming: MutableRefObject<boolean>;
}) {
  const [q, setQ] = useState("");
  const [found, setFound] = useState<(HarnessIconSearch & { q: string }) | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [team, setTeam] = useState<HarnessIconTeamItem[] | null>(null);
  // Bumped when the team's icons change (a drawing landed or was
  // discarded): the team list is read again. The results are not — the
  // card holding the drawing stays until the next query.
  const [teamRev, setTeamRev] = useState(0);
  const inputId = useId();
  // Your own icon: the file chosen or dropped, its card open.
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  // The team icon whose ⋯ menu is open.
  const [editing, setEditing] = useState<HarnessIconTeamItem | null>(null);

  useEffect(() => {
    const ctl = new AbortController();
    harnessApi
      .iconTeam({ limit: TEAM_SHOWN, signal: ctl.signal })
      .then((r) => setTeam(r.items))
      .catch(() => !ctl.signal.aborted && setTeam((t) => t ?? []));
    return () => ctl.abort();
  }, [teamRev]);

  useEffect(() => {
    const query = q.trim();
    if (!query) {
      setFound(null);
      setError(null);
      setBusy(false);
      return;
    }
    const ctl = new AbortController();
    setBusy(true);
    const t = window.setTimeout(() => {
      harnessApi
        .iconSearch(query, { limit: 60, signal: ctl.signal })
        .then((r) => {
          rememberPaths(r.items);
          setFound({ ...r, q: query });
          setError(null);
        })
        .catch((err) => !ctl.signal.aborted && setError(rebrandErrorDetail(err)))
        .finally(() => !ctl.signal.aborted && setBusy(false));
    }, 250);
    return () => {
      window.clearTimeout(t);
      ctl.abort();
    };
  }, [q]);

  const teamChanged = useCallback(() => setTeamRev((n) => n + 1), []);
  const chooseFile = useCallback(() => fileRef.current?.click(), []);
  const onFile = (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (f) setUploadFile(f);
  };
  const hasFiles = (e: DragEvent<HTMLElement>) => Array.from(e.dataTransfer?.types ?? []).includes("Files");

  // Your team's icons, each with its ⋯ menu (rename, tags, delete).
  const teamTiles = (list: HarnessIconTeamItem[]) => (
    <div className="grid grid-cols-4 gap-1.5 sm:grid-cols-5">
      {list.map((it) => {
        const glyph = glyphOf(it);
        const name = it.label || it.name;
        return (
          <div key={it.id} className="relative min-w-0">
            <Tile
              glyph={glyph}
              name={name}
              on={chosen.has(it.id)}
              onLabel={onTitle(it.id)}
              disabled={full && !swap}
              swap={swap}
              onPick={() => onPick(glyph, name)}
            />
            <button
              type="button"
              onClick={() => setEditing((e) => (e?.id === it.id ? null : it))}
              aria-label={COPY.icons.teamMenu(it.name)}
              aria-expanded={editing?.id === it.id}
              title={COPY.icons.teamMenuTitle}
              className="absolute left-0.5 top-0.5 flex h-5 w-5 items-center justify-center rounded text-muted hover:bg-bg-muted hover:text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
            >
              <MoreHorizontal className="h-3.5 w-3.5" aria-hidden />
            </button>
          </div>
        );
      })}
    </div>
  );

  const tiles = (list: (HarnessIconLibraryItem | HarnessIconTeamItem)[]) => (
    <div className="grid grid-cols-4 gap-1.5 sm:grid-cols-5">
      {list.map((it) => {
        const glyph = glyphOf(it);
        const name = it.label || it.name;
        return (
          <Tile
            key={it.id}
            glyph={glyph}
            d={"d" in it ? it.d : null}
            name={name}
            team={glyph.source === "custom"}
            on={chosen.has(it.id)}
            onLabel={onTitle(it.id)}
            disabled={full && !swap}
            swap={swap}
            onPick={() => onPick(glyph, name)}
          />
        );
      })}
    </div>
  );

  // What the search found, and the offer to draw it: a card above loose
  // results (so it is seen), a quiet link under sure ones.
  const offer = found?.generate ?? null;
  const card = found && offer && !found.strong && (
    <GenerateIconCard
      key={`card:${found.q}`}
      offer={offer}
      prominent
      found={found.items.length > 0}
      full={full && !swap}
      onUse={onPick}
      onTeamChanged={teamChanged}
      onUpload={chooseFile}
    />
  );
  const link = found && offer && found.strong && offer.offer && (
    <GenerateIconCard
      key={`link:${found.q}`}
      offer={offer}
      prominent={false}
      found
      full={full && !swap}
      onUse={onPick}
      onTeamChanged={teamChanged}
    />
  );

  return (
    <div
      className="relative space-y-3"
      onDragEnter={(e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        setDragging(true);
      }}
      onDragOver={(e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false);
      }}
      onDrop={(e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        setDragging(false);
        const f = e.dataTransfer.files?.[0];
        if (f) setUploadFile(f);
      }}
    >
      {dragging && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-xl border-2 border-dashed border-accent bg-bg/85 p-4 text-center text-xs text-fg">
          {COPY.icons.dropHere}
        </div>
      )}
      <div className="flex items-center gap-1.5">
        <div className="relative min-w-0 flex-1">
          <label htmlFor={inputId} className="sr-only">
            {COPY.icons.search}
          </label>
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" aria-hidden />
          <input
            id={inputId}
            type="search"
            value={q}
            maxLength={60}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape" && q) {
                e.stopPropagation();
                e.nativeEvent.stopImmediatePropagation();
                setQ("");
              }
            }}
            placeholder={COPY.icons.searchPlaceholder(catalogue?.count ?? 1400)}
            className="w-full rounded-lg border border-border bg-bg py-1.5 pl-8 pr-8 text-xs text-fg placeholder:text-muted/70 focus:border-accent focus:outline-none"
          />
          {busy && <Spinner size="xs" tone="accent" className="absolute right-2.5 top-1/2 -translate-y-1/2" />}
        </div>
        <button type="button" onClick={chooseFile} title={COPY.icons.uploadTitle} className="btn btn-xs btn-secondary shrink-0">
          <Upload className="h-3.5 w-3.5" aria-hidden />
          {COPY.icons.upload}
        </button>
      </div>
      <input ref={fileRef} type="file" accept={UPLOAD_ACCEPT} className="hidden" onChange={onFile} />
      {uploadFile && (
        <IconUpload
          key={`${uploadFile.name}:${uploadFile.size}:${uploadFile.lastModified}`}
          file={uploadFile}
          limits={catalogue?.upload}
          full={full && !swap}
          onUse={onPick}
          onTeamChanged={teamChanged}
          onClose={() => setUploadFile(null)}
          onAnother={chooseFile}
        />
      )}
      {q.trim() && error && !found && (
        <p className="text-2xs text-danger">
          {COPY.icons.searchFailed} {error}
        </p>
      )}
      {found ? (
        <section className="space-y-2" aria-live="polite">
          {card}
          {found.items.length > 0 && (
            <div className="space-y-1.5">
              <h4 className="eyebrow">{COPY.icons.results(found.total)}</h4>
              {tiles(found.items)}
            </div>
          )}
          {link && <div className="pt-1">{link}</div>}
        </section>
      ) : (
        <>
          <section className="space-y-1.5">
            <h4 className="eyebrow" title={COPY.icons.teamTitle}>
              {COPY.icons.team}
            </h4>
            {team == null ? (
              <Spinner size="xs" tone="accent" />
            ) : team.length ? (
              teamTiles(team)
            ) : (
              <p className="text-2xs text-muted">{COPY.icons.teamNone}</p>
            )}
            {editing && (
              <TeamIconEditor
                key={editing.id}
                item={editing}
                confirming={confirming}
                onDone={() => {
                  setEditing(null);
                  teamChanged();
                }}
                onClose={() => setEditing(null)}
              />
            )}
          </section>
          <section className="space-y-3">
            <h4 className="eyebrow">{COPY.icons.picks}</h4>
            {(catalogue?.picks ?? []).map((g) => (
              <div key={g.group} className="space-y-1.5">
                <p className="text-2xs font-medium text-fg">{g.group}</p>
                {tiles(g.items.map((it) => ({ ...it, source: "library" as const })))}
              </div>
            ))}
          </section>
        </>
      )}
    </div>
  );
}

/** A team icon's ⋯ menu: rename it, change its tags, or delete it (hidden
 *  from the library for everyone; versions that use it still show it). Its
 *  picture never changes. */
function TeamIconEditor({
  item,
  confirming,
  onDone,
  onClose,
}: {
  item: HarnessIconTeamItem;
  confirming: MutableRefObject<boolean>;
  /** Changed (or deleted): the team's icons are read again. */
  onDone: () => void;
  onClose: () => void;
}) {
  const confirm = useConfirm();
  const [name, setName] = useState(item.name);
  const [tags, setTags] = useState(item.tags.join(", "));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  useEffect(() => nameRef.current?.focus(), []);

  const save = async () => {
    const n = name.trim();
    if (!n) {
      setError(COPY.icons.uploadNameNeeded);
      return;
    }
    const t = tagsOf(tags);
    const patch: { name?: string; tags?: string[] } = {};
    if (n !== item.name) patch.name = n;
    // Compared as the server saves them: "Café" is the "cafe" it holds.
    if (savedTags(t).join("|") !== item.tags.join("|")) patch.tags = t;
    if (!patch.name && !patch.tags) {
      onClose();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await harnessApi.iconUpdate(item.id, patch);
      onDone();
    } catch (err) {
      setError(rebrandErrorDetail(err));
      setBusy(false);
    }
  };

  const remove = async () => {
    confirming.current = true;
    let ok = false;
    try {
      ok = await confirm({
        title: COPY.icons.teamDeleteTitle,
        message: COPY.icons.teamDeleteMessage(item.name),
        confirmLabel: COPY.icons.teamDeleteConfirm,
        danger: true,
      });
    } finally {
      confirming.current = false;
    }
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      await harnessApi.iconUpdate(item.id, { hidden: true });
      onDone();
    } catch (err) {
      setError(rebrandErrorDetail(err));
      setBusy(false);
    }
  };

  // Enter saves; Esc closes the menu, not the picker.
  const keys = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      void save();
    } else if (e.key === "Escape") {
      e.stopPropagation();
      e.nativeEvent.stopImmediatePropagation();
      onClose();
    }
  };

  return (
    <div
      role="group"
      aria-label={COPY.icons.teamEditLabel(item.name)}
      className="space-y-2 rounded-lg border border-border bg-bg-muted/40 p-2.5"
      data-team-editor={item.id}
    >
      <div className="flex items-center gap-2">
        <IconGlyph glyph={{ source: "custom", id: item.id }} size={22} className="shrink-0 text-fg" />
        <p className="min-w-0 flex-1 truncate text-xs font-medium text-fg">{item.name}</p>
      </div>
      <label className="block space-y-1">
        <span className="text-2xs font-medium text-fg">{COPY.icons.teamName}</span>
        <input
          ref={nameRef}
          type="text"
          value={name}
          maxLength={40}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={keys}
          disabled={busy}
          className="w-full rounded border border-border bg-bg px-2 py-1 text-xs text-fg focus:border-accent focus:outline-none disabled:opacity-60"
        />
      </label>
      <label className="block space-y-1">
        <span className="text-2xs font-medium text-fg">{COPY.icons.teamTags}</span>
        <input
          type="text"
          value={tags}
          placeholder={COPY.icons.uploadTagsPlaceholder}
          onChange={(e) => setTags(e.target.value)}
          onKeyDown={keys}
          disabled={busy}
          className="w-full rounded border border-border bg-bg px-2 py-1 text-xs text-fg focus:border-accent focus:outline-none disabled:opacity-60"
        />
      </label>
      <p className="text-3xs text-muted">{COPY.icons.teamEditHint}</p>
      {error && (
        <p className="rounded-lg border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-2xs text-danger">
          {COPY.icons.teamEditFailed} {error}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-1.5">
        <button type="button" onClick={() => void save()} disabled={busy} className="btn btn-primary btn-sm">
          {busy ? COPY.icons.teamSaving : COPY.icons.teamSave}
        </button>
        <button type="button" onClick={() => void remove()} disabled={busy} className="btn btn-xs btn-ghost text-danger">
          {COPY.icons.teamDelete}
        </button>
        <button type="button" onClick={onClose} disabled={busy} className="btn btn-xs btn-ghost">
          {COPY.icons.close}
        </button>
      </div>
    </div>
  );
}
