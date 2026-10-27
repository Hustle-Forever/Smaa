/**
 * Settings: everything you can change, in one place, in words people use.
 *
 * WHAT THIS REPLACED. A control panel for one feature, written in the
 * vocabulary of whoever built it: Mode, Source mix, Modality emphasis (website
 * portion), Output emphasis, eight 0-100 sliders, and a "What this means" panel
 * printing `per-phase caps`, `synthesis source limits` and `phases that will
 * run`. The owner is a technical person and could not read it. Meanwhile the
 * two settings people actually change lived three clicks deep inside SMAA
 * intelligence, behind a screen with two tab bars stacked on each other.
 *
 * THE RULE THE FIRST FOUR TABS ARE BUILT ON. A control whose right answer the
 * user cannot know is not a setting, it is a way to break the product quietly.
 * Nobody configuring a brand profile can answer "website weight 70 or 30", and a
 * wrong answer does not fail loudly: it returns a brand profile that reads
 * slightly off and nobody can say why. So the front of this page asks only
 * questions a person can answer.
 *
 * AND THEN THERE IS ADVANCED, which is the exception and exists on purpose. The
 * synthesizer's mode, weights, include flags and model settings were deleted
 * from this app under that rule, and they are back at the owner's request,
 * restored exactly as their author wrote them so that he is the one who decides
 * what happens to them. They are one tab, last, behind a plain word, rather than
 * the first thing a new person meets. See `components/settings/AdvancedSection`,
 * which also lists the known behaviour that was deliberately left unpatched.
 *
 * THREE SECTIONS, each named the way somebody would say it out loud:
 *
 *   Your brand         one question about 42, in your own words
 *   Posting and email  whether the two outward-facing things work
 *   Advanced           the synthesizer's own dials, for whoever owns them
 *
 * TWO SECTIONS WENT BACK. "News to look for" and "Accounts we watch" were
 * SMAA's Configure tab, mounted here for a while on the argument that
 * everything you change belongs in one place. They are SMAA's again, because
 * the stronger argument is the other one: those two are not settings for the
 * app, they are the inputs to that section's output, and the person tuning
 * either one is reading the Feed while they do it. They went back exactly as
 * they left, headings and all: see components/trends/ConfigurePanel.tsx.
 *
 * ONE SECTION IS READ-ONLY and it is new. Nothing in this app could previously
 * tell you whether posting worked or whether reminder emails would send. The
 * checks existed (`publish_status`, `email_configured`) and were never exposed.
 * The credentials stay in the server's environment; this answers "is it
 * working", which is the part a person on this screen can act on.
 *
 * ROUTED, because a page of sections that cannot be linked to is a page you
 * have to describe out loud. `#settings/connections` and `#settings/advanced`
 * are real addresses, and `#settings/news` / `#settings/accounts`, which were
 * real addresses for as long as those sections lived here, now redirect into
 * SMAA intelligence rather than dumping the reader on the brand box. Folding
 * them to the first section instead would silently drop somebody two clicks
 * from where they asked to be, with nothing saying why.
 */
import { lazy, Suspense, useCallback, useEffect, useState } from "react";

import { api } from "../lib/api";
import type { Connections, Settings as TSettings } from "../lib/types";
import { useToast } from "../components/ToastProvider";
import { Skeleton } from "../components/Skeleton";
import { TabBar } from "../components/trends/TabBar";
import { Notice, SaveBar } from "../components/ui/controls";
import { ErrorCard, SectionHeader } from "../components/ui/primitives";

// Lazy because it drags in `lib/hints.ts` and `WeightSlider`, and most people
// will never open it. The two lazy sections that sat beside it here are in
// SMAA intelligence now, split the same way by ConfigurePanel.
const AdvancedSection = lazy(() =>
  import("../components/settings/AdvancedSection").then((m) => ({
    default: m.AdvancedSection,
  })),
);

// ---------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------

export type SettingsSection = "brand" | "connections" | "advanced";

// Order is the point: the two a person can answer, then the one they cannot.
const SECTIONS: ReadonlyArray<{ key: SettingsSection; label: string }> = [
  { key: "brand", label: "Your brand" },
  { key: "connections", label: "Posting and email" },
  { key: "advanced", label: "Advanced" },
];

/**
 * Every spelling the hash segment resolves to. It is NOT `SettingsSection`:
 * "moved:accounts" and "moved:news" are not sections, they are the two
 * addresses this page used to own, kept so the body can send the reader to
 * SMAA intelligence instead of swallowing the request.
 */
type Route = SettingsSection | "moved:accounts" | "moved:news";

/** A hash segment to a route. Anything unknown is the first section, never a
 *  blank page. `competitors` is the oldest spelling of the accounts screen and
 *  still works, here as well as there. */
export function routeFromHash(raw?: string | null): Route {
  const s = (raw || "").toLowerCase();
  if (s === "accounts" || s === "competitors") return "moved:accounts";
  if (s === "news") return "moved:news";
  if (s === "connections") return "connections";
  if (s === "advanced") return "advanced";
  return "brand";
}

/** Where a route this page no longer serves should send the reader. */
const MOVED_TO: Record<string, { sub: string; label: string }> = {
  "moved:accounts": { sub: "accounts", label: "Accounts we watch" },
  "moved:news": { sub: "news", label: "News to look for" },
};

// ---------------------------------------------------------------------------
// Your brand
// ---------------------------------------------------------------------------

/**
 * One field, and it is the only column of `brand_profile_settings` a person can
 * reasonably answer. The rest of that row is real again and lives on Advanced.
 *
 * THE TWO TABS SHARE A ROW AND DO NOT KNOW IT. Advanced PUTs the whole row from
 * whatever it loaded, this one PUTs a single field, and neither reloads when the
 * other saves. Left alone on purpose: see the notes at the foot of
 * `components/settings/AdvancedSection`.
 */
function BrandSection() {
  const toast = useToast();
  const [saved, setSaved] = useState<string>("");
  const [draft, setDraft] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const s: TSettings = await api.settings();
        if (cancelled) return;
        const text = s.custom_instructions ?? "";
        setSaved(text);
        setDraft(text);
      } catch (e: any) {
        if (!cancelled) setError(String(e?.message || e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const save = useCallback(async () => {
    setSaving(true);
    setError(null);
    try {
      // Null, not "", because an emptied box means "there is nothing special".
      // NOTE: the route is back to `exclude_none`, which drops the null before
      // it reaches the UPDATE, so emptying this box does not currently clear the
      // stored sentence. Left as-is with the rest of the restored feature.
      const updated = await api.updateSettings({
        custom_instructions: draft.trim() || null,
      });
      const text = updated.custom_instructions ?? "";
      setSaved(text);
      setDraft(text);
      toast.success("Saved");
    } catch (e: any) {
      const msg = String(e?.message || e);
      setError(msg);
      toast.error("Couldn't save", msg);
    } finally {
      setSaving(false);
    }
  }, [draft, toast]);

  if (loading) {
    return (
      <div className="space-y-3">
        <Skeleton h="h-6" className="w-64" />
        <Skeleton h="h-28" className="w-full" />
      </div>
    );
  }

  return (
    <section aria-labelledby="set-brand" className="space-y-4">
      <SectionHeader id="set-brand" title="Your brand">
        Anything we should always keep in mind about 42? Write it however you
        like. We use it every time your brand profile is rebuilt.
      </SectionHeader>

      {error && <ErrorCard message={error} />}

      <label className="block">
        <span className="sr-only">Anything we should always keep in mind</span>
        <textarea
          rows={5}
          value={draft}
          onChange={(e) => setDraft(e.currentTarget.value)}
          placeholder="e.g. Always sound like a student, never like a brochure. Never call it a university."
          className="textarea"
        />
      </label>

      <p className="text-xs text-muted">
        Leave it empty if there is nothing special. This is used the next time
        your brand profile is built, which you start from{" "}
        {/* A LINK, not a bold word. The old page saved and kicked off the
            rebuild in one button; the rebuild is an action and lives on Brand
            now, so the least this page can do is take you there. Naming a
            destination the reader then has to go and hunt for is how the news
            empty state ended up pointing at a deleted tab. */}
        <a href="#brand/synthesizer" className="text-accent underline underline-offset-2">
          Brand
        </a>
        .
      </p>

      <SaveBar dirty={draft.trim() !== saved.trim()} saving={saving} onSave={() => void save()} />
    </section>
  );
}

// ---------------------------------------------------------------------------
// Posting and email
// ---------------------------------------------------------------------------

/**
 * Read-only on purpose.
 *
 * The Instagram token and the SMTP password are environment values read once at
 * startup into a frozen object, so there is nothing here to type into. That is
 * not the limitation it looks like: the question a person on this screen has is
 * "is it working", and until now the app answered it nowhere at all. A publish
 * that silently self-gated and a reminder that silently never sent both looked
 * exactly like nothing happening.
 */
function ConnectionsSection() {
  const [conn, setConn] = useState<Connections | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const c = await api.connections();
        if (!cancelled) setConn(c);
      } catch (e: any) {
        if (!cancelled) setError(String(e?.message || e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <section aria-labelledby="set-conn" className="space-y-4">
      <SectionHeader id="set-conn" title="Posting and email">
        The two things that reach the outside world. You cannot change these
        here, but you can see whether they work.
      </SectionHeader>

      {error && <ErrorCard message={error} />}
      {loading && <Skeleton h="h-20" className="w-full" />}

      {conn && (
        <div className="space-y-3">
          <Notice tone={conn.instagram.ready ? "success" : "warning"}>
            <strong className="text-fg">Instagram.</strong>{" "}
            {conn.instagram.ready ? (
              <>
                Posts go to{" "}
                <span className="font-medium text-fg">
                  @{conn.instagram.handle ?? "your account"}
                </span>
                .
              </>
            ) : (
              <>
                Not set up, so nothing can be posted. {conn.instagram.reason}
              </>
            )}
          </Notice>

          <Notice tone={conn.email.ready ? "success" : "warning"}>
            <strong className="text-fg">Reminder emails.</strong>{" "}
            {conn.email.ready ? (
              <>
                We email{" "}
                <span className="font-medium text-fg">{conn.email.to}</span> when
                a scheduled post is due.
              </>
            ) : (
              <>
                Not set up. Scheduled posts still wait for you in the Library,
                you just will not be emailed about them.
              </>
            )}
          </Notice>

          <p className="text-xs text-muted">
            Both are set up by whoever runs the server, not from this screen.
          </p>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

export function SettingsPage({
  section,
  navigate,
}: {
  /** The hash's second segment, e.g. "advanced" in `#settings/advanced`. */
  section?: string | null;
  /** Writes the hash back, so a section is a real address. */
  navigate?: (section: SettingsSection) => void;
}) {
  const route = routeFromHash(section);
  // A retired address selects the first section, so the bar still reads as
  // something while the redirect notice is on screen.
  const moved = MOVED_TO[route];
  const active: SettingsSection = moved ? "brand" : (route as SettingsSection);

  return (
    <div className="space-y-6 animate-pageEnter">
      <header>
        <h1 className="h1">Settings</h1>
        <p className="lead mt-1.5 max-w-2xl">
          Everything you can change about how 42 Studio works.
        </p>
      </header>

      <TabBar
        items={SECTIONS.map((s) => ({ key: s.key, label: s.label }))}
        active={active}
        onSelect={(k) => navigate?.(k)}
        label="Settings sections"
      />

      {/* Room under the content so a sticky save bar never covers the last
          control. Only the page knows how tall its own content is. */}
      <div className="pb-24">
        {moved ? (
          <Notice
            tone="info"
            action={
              <a href={`#trends/${moved.sub}`} className="btn btn-primary text-sm">
                Take me there
              </a>
            }
          >
            <strong className="text-fg">{moved.label}</strong> moved to SMAA
            intelligence. You'll find it under Configure, beside the feed it
            fills.
          </Notice>
        ) : (
          <Suspense fallback={<Skeleton h="h-40" className="w-full" />}>
            {active === "brand" && <BrandSection />}
            {active === "connections" && <ConnectionsSection />}
            {active === "advanced" && <AdvancedSection />}
          </Suspense>
        )}
      </div>
    </div>
  );
}
