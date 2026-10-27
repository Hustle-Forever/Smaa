/**
 * Configure - the two things in SMAA intelligence you SET UP rather than read.
 *
 * WHY IT IS BACK HERE. Both screens spent a spell as sections of the Settings
 * page, on the argument that everything you change belongs in one place. In
 * use that argument lost to a simpler one: these two are not settings for the
 * app, they are the inputs to THIS section's output. Which accounts we watch
 * decides what the Feed can contain, and the news brief decides what a search
 * brings back, so the person tuning either one is reading the Feed while they
 * do it. Sending them to Settings and back was the whole cost.
 *
 * A shell and nothing else: it owns no data, fetches nothing, and holds no
 * sub-tab state. The open sub-tab arrives as a prop and a click goes straight
 * back out through `onSub`, because the page shell turns it into a hash change.
 * That is what makes the browser's back button step through sub-tabs and a
 * copied URL reopen the one you were looking at.
 *
 * The sub-tabs ride the SECOND hash segment (#trends/accounts, #trends/news)
 * rather than a third, because the hash grammar has exactly two
 * (App.routeParamFromHash = split("/")[1]). `#trends/competitors` is the older
 * spelling of the accounts screen and is still accepted by the page shell, so
 * bookmarks from before either move keep landing on it.
 *
 * NO HEADING HERE, on purpose, and unlike the version of this file that
 * existed before the Settings detour. Both bodies grew a `SectionHeader` of
 * their own while they were sections of a page that stacked four of them, and
 * those headers are better copy than anything a shell could print: each names
 * its screen and says what it is for, right above its own controls. So this
 * file adds one bar and nothing else, and pages/TrendFinder.tsx prints no
 * sentence for this tab either.
 *
 * BOTH BODIES ARE LAZY. CompetitorsTab is 2,200 lines and NewsConfigPanel
 * pulls its own API surface with it; the Feed is what most visits come for and
 * should not pay for either. Splitting them separately rather than splitting
 * this shell means opening the news screen does not also fetch the accounts
 * screen's chunk.
 */
import { lazy, Suspense } from "react";
import { Newspaper, Users } from "lucide-react";

import { Skeleton } from "../Skeleton";
import { TabBar, type TabBarItem } from "./TabBar";

const CompetitorsTab = lazy(() =>
  import("./CompetitorsTab").then((m) => ({ default: m.CompetitorsTab })),
);
const NewsConfigPanel = lazy(() =>
  import("./NewsConfigPanel").then((m) => ({ default: m.NewsConfigPanel })),
);

export type ConfigureSub = "accounts" | "news";

/**
 * Two labels, both saying what the screen holds in the words somebody would say
 * out loud, and both carried over from the Settings page rather than reverted
 * to the old "Competitors" and "AI News". They are also the two hash spellings,
 * so the URL in the address bar and the tab under the cursor never use
 * different words for the same screen.
 */
const SUB_TABS: readonly TabBarItem<ConfigureSub>[] = [
  { key: "accounts", label: "Accounts we watch", Icon: Users },
  { key: "news", label: "News to look for", Icon: Newspaper },
];

/** Which sub-tab a bare `#trends/configure` opens. */
export const CONFIGURE_DEFAULT: ConfigureSub = "accounts";

interface Props {
  sub: ConfigureSub;
  onSub: (s: ConfigureSub) => void;
}

export function ConfigurePanel({ sub, onSub }: Props) {
  return (
    <div className="space-y-5">
      {/* size="sub" so this bar reads as subordinate to the page bar directly
          above it - two equal-weight tab rows stacked is the thing that makes
          people lose track of which one they last clicked. `label` matters more
          here than anywhere else in the app: this is the only screen with two
          tab bars on it at once. */}
      <TabBar
        items={SUB_TABS}
        active={sub}
        onSelect={onSub}
        size="sub"
        label="Configure sections"
      />

      {/* The fallback sits BELOW the bar, not around it, so the two tabs stay
          on screen and clickable while a chunk loads. Mounted, not routed:
          both bodies fetch their own data on mount and neither keeps anything
          in module scope, so switching sub-tabs re-syncs from the server
          exactly like a tab change does. */}
      <Suspense fallback={<Skeleton h="h-40" className="w-full" />}>
        {sub === "accounts" ? <CompetitorsTab /> : <NewsConfigPanel />}
      </Suspense>
    </div>
  );
}
