/**
 * Brand workspace: a single tabbed surface that consolidates the brand pages:
 *   • Profile     → the synthesized brand profile (Brand.tsx)
 *   • Upload      → feed the corpus with docs / free text (Upload.tsx)
 *   • Synthesizer → run the crawl / ingest / re-synthesize agents (Agents.tsx)
 *   • Instagram   → ingest @42abudhabi's feed (Instagram.tsx)
 *
 * The active sub-tab is driven by the second hash segment (#brand/upload,
 * #brand/synthesizer) so each tab is deep-linkable and the back button works.
 * Legacy #upload / #agents hashes resolve here too (see App.tsx aliases).
 */
import type { ComponentType } from "react";
import { FileText, Instagram, Sparkles, UploadCloud } from "lucide-react";

import type { Tab } from "../App";
import type { AgentId } from "../lib/types";
import { useStudioCanvas } from "../lib/useStudioCanvas";
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

// Labels stay as the team knows them; the one-line explanation under the title
// says in everyday words what each tab is for.
const TABS: TabDef[] = [
  {
    key: "profile",
    label: "Profile",
    title: "Brand profile",
    sub: "How 42 Abu Dhabi looks and sounds. The AI follows this every time it makes a post.",
    Icon: Sparkles,
  },
  {
    key: "upload",
    label: "Upload",
    title: "Add brand documents",
    sub: "Add files or notes about the brand. They are used the next time the profile is rebuilt.",
    Icon: UploadCloud,
  },
  {
    key: "synthesizer",
    label: "Synthesizer",
    title: "Brand synthesizer",
    sub: "Read the website and your documents again, then rebuild the brand profile.",
    Icon: FileText,
  },
  {
    key: "instagram",
    label: "Instagram",
    title: "Instagram ingest",
    sub: "Bring in @42abudhabi's posts so new content can learn from what already worked.",
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
  useStudioCanvas();

  function go(key: SubTab) {
    // Profile is the bare #brand route; others carry the sub-tab as the param.
    navigate("brand", key === "profile" ? undefined : key);
  }

  return (
    <div className="space-y-6 animate-pageEnter">
      <header className="space-y-4 pt-2">
        <div className="space-y-1.5">
          <p className="text-[13px] font-medium text-fg/55">Brand</p>
          <h1 className="k-display text-[32px] md:text-[42px] text-fg text-balance">
            <span className="k-code" aria-hidden>&gt; </span>
            {meta.title}
          </h1>
          <p className="text-[15px] text-fg/60 max-w-2xl text-pretty">{meta.sub}</p>
        </div>

        {/* Glass tab switch. Page-local rather than the shared TabBar, which
            Settings and SMAA intelligence also use and which keeps its look. */}
        <nav aria-label="Brand sections" className="overflow-x-auto -mx-1 px-1 pb-1">
          <div className="inline-flex gap-0.5 p-0.5 bg-k-inset ring-1 ring-k-border">
            {TABS.map((t) => {
              const on = t.key === active;
              return (
                <button
                  key={t.key}
                  type="button"
                  onClick={() => go(t.key)}
                  aria-current={on ? "page" : undefined}
                  className={[
                    "h-9 px-4 inline-flex items-center gap-2 text-[12.5px] font-bold uppercase tracking-[0.05em] whitespace-nowrap transition-all duration-200",
                    "focus:outline-none focus-visible:ring-2 focus-visible:ring-fg/30",
                    on
                      ? "bg-k-btn text-k-onbtn"
                      : "text-fg/65 hover:text-fg hover:bg-k-surface",
                  ].join(" ")}
                >
                  <t.Icon className="w-4 h-4" strokeWidth={1.7} aria-hidden />
                  {t.label}
                </button>
              );
            })}
          </div>
        </nav>
      </header>

      {/* Active panel */}
      <div>
        {active === "profile" && <BrandPage embedded onOpenSynthesizer={() => go("synthesizer")} />}
        {active === "upload" && <UploadPage embedded onStartRun={onStartRun} />}
        {active === "synthesizer" && (
          <AgentsPage embedded onStartRun={onStartRun} navigate={navigate} />
        )}
        {active === "instagram" && <InstagramPage embedded onStartRun={onStartRun} />}
      </div>
    </div>
  );
}
