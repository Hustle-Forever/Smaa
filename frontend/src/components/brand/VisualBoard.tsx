import { useEffect, useMemo, useState, type ComponentType } from "react";
import { ImageOff } from "lucide-react";

import { api } from "../../lib/api";
import { topicIcon } from "./topicIcon";

type Theme = { label?: string; description?: string };

interface Picture {
  key: string;
  src: string;
  text: string;
  from: "Instagram" | "Assets";
  href?: string | null;
}

/**
 * "What our visuals show" as a mood board: each theme from the profile with
 * real pictures of it, found among our own Instagram posts and the Assets
 * library by the words in their descriptions. Read only (two GET lists).
 */
export function VisualBoard({ themes }: { themes: Theme[] }) {
  const [pool, setPool] = useState<Picture[] | null>(null);
  const [broken, setBroken] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    let alive = true;
    Promise.allSettled([
      api.instagramIngest.listPosts({ limit: 80, order: "newest" }),
      api.producerAssets.list({ kind: "image", limit: 150 }),
    ]).then(([ig, lib]) => {
      if (!alive) return;
      const out: Picture[] = [];
      if (ig.status === "fulfilled") {
        for (const p of ig.value.items ?? []) {
          if (p.first_asset_id == null || p.first_asset_type !== "image") continue;
          out.push({
            key: `ig${p.id}`,
            src: api.instagramIngest.assetFileUrl(p.id, p.first_asset_id),
            text: [p.preview, p.caption].filter(Boolean).join(" "),
            from: "Instagram",
            href: p.permalink,
          });
        }
      }
      if (lib.status === "fulfilled") {
        for (const a of lib.value.items ?? []) {
          if (a.asset_kind !== "image" || a.category === "logo" || a.category === "sticker" || !a.enabled) continue;
          out.push({
            key: `as${a.id}`,
            src: api.producerAssets.thumbUrlFor(a),
            text: [a.display_name, a.preview].filter(Boolean).join(" "),
            from: "Assets",
          });
        }
      }
      setPool(out);
    });
    return () => {
      alive = false;
    };
  }, []);

  const matches = useMemo(() => (pool ? matchThemes(themes, pool) : null), [themes, pool]);
  const found = matches ? matches.reduce((n, m) => n + m.length, 0) : 0;

  return (
    <div className="space-y-3">
      <ul
        className={[
          "grid grid-cols-1 gap-3",
          themes.length === 1 ? "" : themes.length === 2 || themes.length === 4 ? "md:grid-cols-2" : "md:grid-cols-2 xl:grid-cols-3",
        ].join(" ")}
      >
        {themes.map((t, i) => {
          const Icon = topicIcon(t.label, t.description);
          const pics = (matches?.[i] ?? []).filter((p) => !broken.has(p.key)).slice(0, 3);
          const sources = Array.from(new Set(pics.map((p) => p.from)));
          return (
            <li
              key={i}
              className="k-rise border border-k-border bg-k-inset overflow-hidden flex flex-col"
              style={{ animationDelay: `${i * 70}ms` }}
            >
              <Collage
                pictures={pics}
                loading={matches === null}
                compact={matches !== null && found === 0}
                label={t.label}
                Icon={Icon}
                onBroken={(k) => setBroken((s) => new Set(s).add(k))}
              />
              <div className="p-4 space-y-1.5">
                <p className="flex items-center gap-2 text-[14.5px] font-semibold text-fg leading-snug">
                  <Icon className="w-4 h-4 text-k-accent shrink-0" strokeWidth={1.8} aria-hidden />
                  {t.label}
                </p>
                {t.description && <p className="text-[13.5px] leading-[1.55] text-fg/65 text-pretty">{t.description}</p>}
                {pics.length > 0 && (
                  <p className="text-[11.5px] text-fg/45">
                    {pics.length === 1 ? "1 picture" : `${pics.length} pictures`} from {sources.join(" and ")}
                  </p>
                )}
              </div>
            </li>
          );
        })}
      </ul>
      {matches !== null && (
        <p className="text-[12px] text-fg/45">
          {found > 0
            ? "Pictures are picked from our Instagram posts and Assets whose descriptions match each theme."
            : "No pictures matched yet. They appear here once Instagram posts or Assets with matching descriptions are added."}
        </p>
      )}
    </div>
  );
}

function Collage({
  pictures,
  loading,
  compact,
  label,
  Icon,
  onBroken,
}: {
  pictures: Picture[];
  loading: boolean;
  /** No theme has pictures: a short band instead of an empty frame each. */
  compact: boolean;
  label?: string;
  Icon: ComponentType<any>;
  onBroken: (key: string) => void;
}) {
  if (loading) {
    return (
      <div className="aspect-[16/10] grid grid-cols-3 grid-rows-2 gap-0.5 bg-k-border" aria-hidden>
        <span className="col-span-2 row-span-2 bg-k-surface animate-pulse" />
        <span className="bg-k-surface animate-pulse" />
        <span className="bg-k-surface animate-pulse" />
      </div>
    );
  }
  if (pictures.length === 0) {
    return (
      <div className={["k-hatch flex flex-col items-center justify-center gap-2 text-fg/45", compact ? "h-28" : "aspect-[16/10]"].join(" ")}>
        <span className={["k-well text-k-accent", compact ? "w-12 h-12" : "w-14 h-14"].join(" ")}>
          <Icon className="w-6 h-6" strokeWidth={1.5} aria-hidden />
        </span>
        {!compact && (
          <span className="flex items-center gap-1.5 text-[12px]">
            <ImageOff className="w-3.5 h-3.5" strokeWidth={1.8} aria-hidden />
            No matching pictures yet
          </span>
        )}
      </div>
    );
  }
  const layout =
    pictures.length >= 3 ? "grid-cols-3 grid-rows-2" : pictures.length === 2 ? "grid-cols-2 grid-rows-1" : "grid-cols-1 grid-rows-1";
  return (
    <div className={`aspect-[16/10] grid gap-0.5 bg-k-border overflow-hidden ${layout}`}>
      {pictures.map((p, i) => (
        <figure
          key={p.key}
          className={["relative overflow-hidden bg-k-surface min-h-0", pictures.length >= 3 && i === 0 ? "col-span-2 row-span-2" : ""].join(" ")}
        >
          <img
            src={p.src}
            alt={p.text ? p.text.slice(0, 140) : label ?? ""}
            title={p.text ? p.text.slice(0, 240) : undefined}
            loading="lazy"
            decoding="async"
            onError={() => onBroken(p.key)}
            className="absolute inset-0 h-full w-full object-cover transition-transform duration-700 ease-[cubic-bezier(0.16,1,0.3,1)] hover:scale-[1.04]"
          />
        </figure>
      ))}
    </div>
  );
}

/* ---------- matching pictures to themes by their words ---------- */

const STOP = new Set(
  "the and for with that this from into onto over under our your their his her its are was were been being have has had not but all any can will who what when where which while about across more most some such than then them they these those very also just only each every other many much one two three abu dhabi image images photo photos picture pictures post posts shows showing show visual visuals scene scenes".split(
    " ",
  ),
);

function stem(w: string): string {
  if (w.length > 5 && w.endsWith("ies")) return w.slice(0, -3) + "y";
  if (w.length > 5 && w.endsWith("ing")) return w.slice(0, -3);
  if (w.length > 4 && w.endsWith("ed")) return w.slice(0, -2);
  if (w.length > 4 && w.endsWith("es")) return w.slice(0, -2);
  if (w.length > 3 && w.endsWith("s")) return w.slice(0, -1);
  return w;
}

function words(text: string): Set<string> {
  const out = new Set<string>();
  for (const w of text.toLowerCase().split(/[^a-z0-9]+/)) {
    if (w.length < 3 || STOP.has(w)) continue;
    out.add(stem(w));
  }
  return out;
}

function matchThemes(themes: Theme[], pool: Picture[]): Picture[][] {
  const bags = pool.map((p) => words(p.text));
  const df = new Map<string, number>();
  for (const b of bags) for (const w of b) df.set(w, (df.get(w) ?? 0) + 1);
  const N = pool.length;
  const idf = (w: string) => Math.log((N + 1) / ((df.get(w) ?? 0) + 1));

  const used = new Set<string>();
  return themes.map((t) => {
    const want = new Map<string, number>();
    for (const w of words(t.description ?? "")) want.set(w, 1);
    for (const w of words(t.label ?? "")) want.set(w, 2);
    const scored = pool
      .map((p, i) => {
        let s = 0;
        for (const [w, weight] of want) if (bags[i].has(w)) s += weight * idf(w);
        return { p, s };
      })
      .filter((x) => x.s >= 1.2 && !used.has(x.p.key))
      .sort((a, b) => b.s - a.s)
      .slice(0, 4)
      .map((x) => x.p);
    scored.forEach((p) => used.add(p.key));
    return scored;
  });
}
