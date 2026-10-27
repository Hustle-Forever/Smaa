/**
 * Industry Signal - what we follow, the websites we always check, and where
 * the stories came from.
 *
 * THREE SECTIONS, and the middle one is the newest. The screen used to say
 * "we do not read a fixed list of websites", which was true and was also the
 * complaint: the owner wants BOTH. A fixed list of websites we read every
 * time, and the open search across the whole web that was already there. So
 * the follow list is shown as two lists, names and subjects in one, websites
 * in the other, and the server gives the websites a search of their own
 * (`news_search.find_stories(sites=)`) rather than a sentence in the open
 * search's brief asking it to prefer them.
 *
 * ONE TABLE UNDERNEATH. Both lists are rows of `news_watch`, told apart by
 * `kind`, which the server works out from the text: a link pasted into the
 * names box is still a website and is filed as one, with a toast saying so.
 * The websites box is stricter, because a name typed there would be followed
 * as a subject in silence and the person would wait for stories from a site
 * that was never searched. The server refuses it with a sentence and the
 * sentence is printed here.
 *
 * THE THIRD SECTION IS THE RECORD, NOT THE CONFIGURATION: where the stories
 * we KEPT actually came from, both the websites above and the rest of the
 * web. It answers the question the first two sections raise.
 *
 * THE SEARCH IS NOT STARTED FROM HERE, deliberately: it lives on the feed,
 * next to the control that caps it and above the cards it fills, so the person
 * who asks for stories is looking at where they arrive.
 *
 * NO JARGON ON THIS SCREEN, per the standing rule for every 42-studio surface.
 */
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Globe, Plus, X } from "lucide-react";

import { Skeleton } from "../Skeleton";
import { useToast } from "../ToastProvider";
import type { TrendNewsOutlet, TrendNewsWatch } from "../../lib/types";
import { ErrorCard, INPUT_CLS, SectionHeader } from "../ui/primitives";
import { humanError, trendsApi } from "./trendsApi";

/** "18 Sep" from a timestamp, and nothing at all from a missing one. */
function shortDay(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

type Kind = "name" | "site";

export function NewsConfigPanel() {
  const toast = useToast();
  const followId = useId();
  const sitesId = useId();
  const outletsId = useId();
  const nameBoxId = useId();
  const siteBoxId = useId();

  const [watch, setWatch] = useState<TrendNewsWatch[]>([]);
  const [outlets, setOutlets] = useState<TrendNewsOutlet[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // One draft per box. They are different questions and a shared draft would
  // carry a half-typed company name into the websites box.
  const [drafts, setDrafts] = useState<Record<Kind, string>>({ name: "", site: "" });
  const [adding, setAdding] = useState<Kind | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    try {
      const got = await trendsApi.newsWatch();
      if (!alive.current) return;
      setWatch(got.watch || []);
      setOutlets(got.outlets || []);
      setError(null);
    } catch (e) {
      if (alive.current) setError(humanError(e));
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const names = watch.filter((w) => w.kind !== "site");
  const sites = watch.filter((w) => w.kind === "site");

  async function add(kind: Kind) {
    const value = drafts[kind].trim();
    if (!value || adding) return;
    setAdding(kind);
    try {
      const got = await trendsApi.newsWatchAdd(value, kind);
      setDrafts((d) => ({ ...d, [kind]: "" }));
      // A link typed into the names box is filed as a website. Say so, or the
      // person looks for it in the list they typed it into and does not find it.
      if (kind === "name" && got.item.kind === "site") {
        toast.info(
          `${got.item.value} is a website`,
          "So it went under Websites we always check.",
        );
      }
      // Refetched rather than appended: the server decides the order and it
      // also cleans the value, so a pasted link comes back as the host.
      // Guessing either here is how a list that looks right until you reload
      // happens.
      await load();
    } catch (e) {
      toast.error(humanError(e));
    } finally {
      if (alive.current) setAdding(null);
    }
  }

  async function remove(item: TrendNewsWatch) {
    setBusyId(item.id);
    // Off the screen first. This is a one-click undo (type it again) and the
    // row sitting there through a round trip is the thing that gets clicked
    // twice.
    setWatch((rows) => rows.filter((r) => r.id !== item.id));
    try {
      await trendsApi.newsWatchRemove(item.id);
    } catch (e) {
      toast.error(humanError(e));
      await load();
    } finally {
      if (alive.current) setBusyId(null);
    }
  }

  function addBox(kind: Kind, id: string, placeholder: string, label: string) {
    const busy = adding === kind;
    return (
      <div className="flex items-center gap-2">
        <input
          id={id}
          className={INPUT_CLS}
          value={drafts[kind]}
          onChange={(e) => setDrafts((d) => ({ ...d, [kind]: e.target.value }))}
          onKeyDown={(e) => {
            if (e.key === "Enter") void add(kind);
          }}
          placeholder={placeholder}
          aria-label={label}
          disabled={busy}
        />
        <button
          type="button"
          onClick={() => void add(kind)}
          disabled={busy || !drafts[kind].trim()}
          className="btn btn-primary shrink-0 text-sm"
        >
          <Plus className="h-4 w-4" aria-hidden />
          {busy ? "Adding…" : "Add"}
        </button>
      </div>
    );
  }

  function chips(items: TrendNewsWatch[], empty: string) {
    if (items.length === 0) {
      return <p className="text-sm text-muted">{empty}</p>;
    }
    return (
      <ul className="flex flex-wrap gap-2">
        {items.map((item) => (
          <li
            key={item.id}
            className="flex max-w-full items-center gap-2 rounded-full border border-line bg-surface-2 py-1 pl-3 pr-1 text-sm"
          >
            {item.kind === "site" && (
              <Globe className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />
            )}
            <span className="truncate">{item.value}</span>
            <button
              type="button"
              onClick={() => void remove(item)}
              disabled={busyId === item.id}
              title={`Remove ${item.value}`}
              aria-label={`Remove ${item.value}`}
              className="rounded-full p-1 text-muted transition-colors hover:bg-surface hover:text-fg disabled:opacity-40"
            >
              <X className="h-3.5 w-3.5" aria-hidden />
            </button>
          </li>
        ))}
      </ul>
    );
  }

  return (
    <div className="space-y-8">
      {/* ---------------- what we follow ---------------- */}
      <section aria-labelledby={followId} className="space-y-4">
        <SectionHeader id={followId} title="What we follow">
          A company, a person or a subject. Type Anthropic and every search
          looks across the whole web for what Anthropic has done that matters
          to students and developers, not their announcements about
          themselves. Press Find stories on the feed when you want a search.
        </SectionHeader>

        {error && <ErrorCard message={error} />}

        {loading ? (
          <Skeleton className="h-32 w-full" />
        ) : (
          <>
            {addBox("name", nameBoxId, "Anthropic, OpenAI, AI in schools",
              "Something to follow")}
            {chips(names,
              "Nothing here yet. Add the first thing above and the next search will go looking for it.")}
          </>
        )}
      </section>

      {/* ---------------- websites we always check ---------------- */}
      <section aria-labelledby={sitesId} className="space-y-4">
        <SectionHeader id={sitesId} title="Websites we always check">
          Every search reads these websites first and brings back their newest
          stories. Then it also looks across the rest of the web for what we
          follow above, so you get both. Company websites work too: openai.com
          brings in what OpenAI announced. Every story still has to pass the
          same plain-English test, whichever website it came from.
        </SectionHeader>

        {loading ? (
          <Skeleton className="h-24 w-full" />
        ) : (
          <>
            {addBox("site", siteBoxId, "theverge.com, or paste a link",
              "A website to always check")}
            {chips(sites,
              "No websites yet. Add one and every search will read it first.")}
          </>
        )}
      </section>

      {/* ---------------- where the stories came from ---------------- */}
      <section aria-labelledby={outletsId} className="space-y-4">
        <SectionHeader id={outletsId} title="Where the stories came from">
          Where the stories you have kept so far actually came from, both the
          websites above and the rest of the web. Add a website above if you
          want more from it.
        </SectionHeader>

        {loading ? (
          <Skeleton className="h-24 w-full" />
        ) : outlets.length === 0 ? (
          <p className="text-sm text-muted">
            Nothing collected yet. Press Find stories on the feed and they will
            show up here.
          </p>
        ) : (
          <ul className="divide-y divide-line rounded-lg border border-line">
            {outlets.map((o) => (
              <li
                key={o.outlet}
                className="flex items-center justify-between gap-3 px-3 py-2 text-sm"
              >
                <span className="truncate">{o.outlet}</span>
                <span className="shrink-0 text-xs text-muted">
                  {o.stories === 1 ? "1 story" : `${o.stories} stories`}
                  {shortDay(o.last_seen) && ` · ${shortDay(o.last_seen)}`}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
