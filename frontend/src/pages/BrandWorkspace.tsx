/**
 * Brand workspace — a single tabbed surface that consolidates the three
 * brand-archive pages:
 *   • Profile     → the synthesized brand profile (Brand.tsx)
 *   • Upload      → feed the corpus with docs / free text (Upload.tsx)
 *   • Synthesizer → run the crawl / ingest / re-synthesize agents (Agents.tsx)
 *
 * The active sub-tab is driven by the second hash segment (#brand/upload,
 * #brand/synthesizer) so each tab is deep-linkable and the back button works.
 * Legacy #upload / #agents hashes resolve here too (see App.tsx aliases).
 */
import type { ComponentType } from "react";
import { FileText, Instagram, Sparkles, UploadCloud } from "lucide-react";

import type { Tab } from "../App";
import type { AgentId } from "../lib/types";
import { TabBar } from "../components/trends/TabBar";
import { BrandPage } from "./Brand";
import { UploadPage } from "./Upload";
import { AgentsPage } from "./Agents";
import { InstagramPage } from "./Instagram";

type SubTab = "profile" | "upload" | "synthesizer" | "instagram";

interface TabDef {
  key: SubTab;
  label: string;
  title: string;
  sub: string;
  Icon: ComponentType<any>;
}

const TABS: TabDef[] = [
  {
    key: "profile",
    label: "Profile",
    title: "Brand profile",
    sub: "The synthesized identity — summary, voice, palette, typography, audiences.",
    Icon: Sparkles,
  },
  {
    key: "upload",
    label: "Upload",
    title: "Add brand documents",
    sub: "Feed the corpus — files or free text become brand_documents for the next synthesis.",
    Icon: UploadCloud,
  },
  {
    key: "synthesizer",
    label: "Synthesizer",
    title: "Brand synthesizer",
    sub: "Crawl the site, ingest documents, and re-synthesize the brand profile.",
    Icon: FileText,
  },
  {
    key: "instagram",
    label: "Instagram",
    title: "Instagram ingest",
    sub: "Ingest @42abudhabi's feed into the producer's retrieval space so future scripts learn from what's already worked.",
    Icon: Instagram,
  },
];

function normSub(s?: string | null): SubTab {
  if (s === "upload") return "upload";
  if (s === "synthesizer" || s === "agents") return "synthesizer";
  if (s === "instagram") return "instagram";
  return "profile";
}

interface Props {
  subtab?: string | null;
  navigate: (t: Tab, param?: string) => void;
  onStartRun: (agent: AgentId, runId: number, preface?: string) => void;
}

export function BrandWorkspace({ subtab, navigate, onStartRun }: Props) {
  const active = normSub(subtab);
  const meta = TABS.find((t) => t.key === active) ?? TABS[0];

  function go(key: SubTab) {
    // Profile is the bare #brand route; others carry the sub-tab as the param.
    navigate("brand", key === "profile" ? undefined : key);
  }

  return (
    <div className="space-y-6 animate-pageEnter">
      <header>
        <p className="eyebrow mb-1.5">Brand</p>
        <h1 className="h1">{meta.title}</h1>
        <p className="lead mt-1.5 max-w-2xl">{meta.sub}</p>
      </header>

      {/* Underline tab bar. The markup used to be inline here and byte-identical
          to the one in pages/TrendFinder.tsx; it now lives in
          components/trends/TabBar.tsx so the Trends Configure sub-bar could
          reuse it instead of becoming a third copy. Renders the same, except
          the row now wraps rather than squeezing on a narrow window. */}
      <TabBar items={TABS} active={active} onSelect={go} />

      {/* Active panel */}
      <div>
        {active === "profile" && <BrandPage embedded />}
        {active === "upload" && <UploadPage embedded onStartRun={onStartRun} />}
        {active === "synthesizer" && (
          <AgentsPage embedded onStartRun={onStartRun} navigate={navigate} />
        )}
        {active === "instagram" && <InstagramPage embedded onStartRun={onStartRun} />}
      </div>
    </div>
  );
}
