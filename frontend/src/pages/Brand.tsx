import { useEffect, useState } from "react";

import { api } from "../lib/api";
import { fmtRel } from "../lib/format";
import type { BrandResponse } from "../lib/types";
import { Skeleton } from "../components/Skeleton";

export function BrandPage({ embedded = false }: { embedded?: boolean } = {}) {
  const [data, setData] = useState<BrandResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .brand()
      .then((d) => {
        if (cancelled) return;
        setData(d);
      })
      .catch((e) => setError(String(e?.message || e)))
      .finally(() => setLoading(false));
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) {
    return (
      <div className="space-y-4">
        <Skeleton h="h-3" w="w-24" />
        <Skeleton h="h-10" w="w-96" />
      </div>
    );
  }

  if (error) {
    return <p className="text-sm text-danger">{error}</p>;
  }

  if (!data?.payload) {
    return (
      <div className="space-y-4">
        {!embedded && <p className="eyebrow">Brand profile</p>}
        <p className="lead">
          No brand profile yet. Run the Synthesizer (crawl, ingest, or
          re-synthesize) to build one.
        </p>
      </div>
    );
  }

  const p = data.payload;
  const r = data.run;

  return (
    <article className="space-y-8">
      {embedded ? (
        r && (
          <p className="font-mono text-2xs text-muted">
            run #{r.id} · {r.notes ?? "—"} · synthesized {fmtRel(r.finished_at)}
          </p>
        )
      ) : (
        <header>
          <p className="eyebrow mb-1.5">Brand profile</p>
          <h1 className="h1">42 Abu Dhabi</h1>
          {r && (
            <p className="font-mono text-2xs text-muted mt-2">
              run #{r.id} · {r.notes ?? "—"} · synthesized {fmtRel(r.finished_at)}
            </p>
          )}
        </header>
      )}

      {p.brand_summary && (
        <section>
          <p className="eyebrow mb-3">Summary</p>
          <p className="text-base leading-relaxed text-fg/90 max-w-3xl">
            {p.brand_summary}
          </p>
        </section>
      )}

      {p.voice && (
        <section>
          <p className="eyebrow mb-3">Voice</p>
          {p.voice.pillars && p.voice.pillars.length > 0 && (
            <p className="mb-4 text-sm">
              <span className="font-medium">Pillars:</span>{" "}
              {p.voice.pillars.join(" · ")}
            </p>
          )}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div className="border-l-2 border-success/40 pl-4">
              <p className="font-mono text-2xs text-success mb-2">DO</p>
              <ul className="space-y-1.5 text-sm">
                {(p.voice.do ?? []).map((d, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="text-success">✓</span>
                    <span>{d}</span>
                  </li>
                ))}
              </ul>
            </div>
            <div className="border-l-2 border-danger/40 pl-4">
              <p className="font-mono text-2xs text-danger mb-2">DON'T</p>
              <ul className="space-y-1.5 text-sm">
                {(p.voice.dont ?? []).map((d, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="text-danger">✕</span>
                    <span>{d}</span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </section>
      )}

      {p.visual_identity && (
        <section>
          <div className="flex items-baseline gap-3 mb-3">
            <p className="eyebrow">Visual identity</p>
            <SourceBadge source={p._sources?.primary_colors} />
          </div>
          {p.visual_identity.primary_colors &&
            p.visual_identity.primary_colors.length > 0 && (
              <div className="mb-4">
                <p className="text-xs text-muted mb-2">Primary colours</p>
                <div className="flex flex-wrap">
                  {p.visual_identity.primary_colors.map((c) => (
                    <Swatch key={c} hex={c} />
                  ))}
                </div>
              </div>
            )}
          {p.visual_identity.secondary_colors &&
            p.visual_identity.secondary_colors.length > 0 && (
              <div className="mb-4">
                <p className="text-xs text-muted mb-2">Secondary colours</p>
                <div className="flex flex-wrap">
                  {p.visual_identity.secondary_colors.map((c) => (
                    <Swatch key={c} hex={c} />
                  ))}
                </div>
              </div>
            )}
          {p.visual_identity.style_pillars &&
            p.visual_identity.style_pillars.length > 0 && (
              <p className="text-sm">
                <span className="font-medium">Style pillars:</span>{" "}
                {p.visual_identity.style_pillars.join(" · ")}
              </p>
            )}
        </section>
      )}

      {p.visual_identity?.fonts && p.visual_identity.fonts.length > 0 && (
        <section>
          <div className="flex items-baseline gap-3 mb-3">
            <p className="eyebrow">Typography</p>
            <SourceBadge source={p._sources?.fonts} />
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {p.visual_identity.fonts.map((f) => (
              <div key={f} className="card p-4">
                <p
                  className="text-2xl text-fg leading-none"
                  style={{ fontFamily: `"${f}", system-ui, sans-serif` }}
                >
                  The quick brown fox
                </p>
                <p className="font-mono text-2xs text-muted mt-2">{f}</p>
              </div>
            ))}
          </div>
        </section>
      )}

      <Section title="Audiences" items={p.audiences} />
      <Section title="Message themes" items={p.message_themes} />
      <Section title="Visual themes" items={p.visual_themes} />

      {p.taglines_in_use && p.taglines_in_use.length > 0 && (
        <section>
          <p className="eyebrow mb-3">Taglines / phrases</p>
          <ul className="space-y-1.5 text-sm">
            {p.taglines_in_use.map((t, i) => (
              <li key={i} className="text-fg/80">— {t}</li>
            ))}
          </ul>
        </section>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {p.brand_dos && p.brand_dos.length > 0 && (
          <section>
            <p className="eyebrow text-success mb-3">Brand do's</p>
            <ul className="space-y-1.5 text-sm">
              {p.brand_dos.map((d, i) => (
                <li key={i} className="flex gap-2">
                  <span className="text-success">✓</span>
                  <span>{d}</span>
                </li>
              ))}
            </ul>
          </section>
        )}
        {p.brand_donts && p.brand_donts.length > 0 && (
          <section>
            <p className="eyebrow text-danger mb-3">Brand don'ts</p>
            <ul className="space-y-1.5 text-sm">
              {p.brand_donts.map((d, i) => (
                <li key={i} className="flex gap-2">
                  <span className="text-danger">✕</span>
                  <span>{d}</span>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>

      {p.notes_for_producer_agent && (
        <section className="border-t border-border pt-8">
          <p className="eyebrow mb-3">Producer notes</p>
          <p className="text-sm text-fg/80 leading-relaxed max-w-3xl italic">
            {p.notes_for_producer_agent}
          </p>
        </section>
      )}

      <div className="border-t border-border pt-6 grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Pages" value={data.sources.website_pages ?? 0} />
        <Stat label="Image descriptions" value={data.sources.image_descriptions ?? 0} />
        <Stat label="Video summaries" value={data.sources.video_summaries ?? 0} />
        <Stat label="Owner docs" value={data.sources.owner_documents ?? 0} />
      </div>
    </article>
  );
}

function Section({
  title,
  items,
}: {
  title: string;
  items?: Array<{ label?: string; description?: string }>;
}) {
  if (!items || items.length === 0) return null;
  return (
    <section>
      <p className="eyebrow mb-3">{title}</p>
      <ul className="grid grid-cols-1 lg:grid-cols-2 gap-x-6 gap-y-2.5">
        {items.map((it, i) => (
          <li key={i} className="text-sm">
            <span className="font-medium text-fg">{it.label}</span>
            {it.description && (
              <>
                {" — "}
                <span className="text-fg/80">{it.description}</span>
              </>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function Swatch({ hex }: { hex: string }) {
  return (
    <div className="inline-flex flex-col items-center mr-3 mb-3 group">
      <span
        className="relative block w-16 h-16 rounded-lg border border-border transition-all duration-200
                   group-hover:-translate-y-0.5 group-hover:shadow-lift cursor-default"
        style={{ backgroundColor: hex }}
        title={hex}
      >
        <span
          className="absolute inset-0 flex items-center justify-center text-3xs font-mono font-medium
                     bg-fg/70 text-white rounded-lg opacity-0 group-hover:opacity-100 transition-opacity"
        >
          {hex}
        </span>
      </span>
      <span className="font-mono text-3xs mt-1 text-muted">{hex}</span>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <p className="eyebrow mb-1">{label}</p>
      <p className="font-mono text-base font-bold tabular-nums text-fg">
        {value}
      </p>
    </div>
  );
}

/**
 * Renders provenance for a brand-profile field — e.g.
 *   _sources.primary_colors = "priority_1_canonical_brand_guides" →
 *   "from brand guide".
 *
 * Returns null when source is undefined.
 */
function SourceBadge({ source }: { source?: string }) {
  if (!source) return null;
  const map: Record<string, string> = {
    priority_1_canonical_brand_guides: "from brand guide",
    priority_2_owner_visual_samples: "from owner-uploaded image",
    priority_3_owner_narrative: "from owner free-text",
    priority_4_website_voice_tone: "inferred from website voice",
    priority_5_website_clusters: "inferred from website clusters",
    priority_6_pixel_palette: "inferred from website pixels",
  };
  const text = map[source] ?? source;
  const isCanonical = source.startsWith("priority_1");
  return (
    <span
      className={[
        "font-mono text-3xs uppercase tracking-wider2 px-1.5 py-0.5 rounded",
        isCanonical
          ? "text-success bg-success/10 border border-success/20"
          : "text-muted bg-bg-muted border border-border",
      ].join(" ")}
      title={source}
    >
      {text}
    </span>
  );
}
