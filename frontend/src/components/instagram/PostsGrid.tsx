import { useEffect, useState } from "react";

import { Skeleton } from "../Skeleton";
import { api } from "../../lib/api";
import { fmtNumber, fmtRel } from "../../lib/format";
import { useInstagramPostsStore } from "../../lib/store";
import type { InstagramPostRow } from "../../lib/types";

interface Props {
  refreshKey?: number;
  onOpen: (postId: number) => void;
}

const PAGE = 24;

export function PostsGrid({ refreshKey, onOpen }: Props) {
  const store = useInstagramPostsStore();
  const [offset, setOffset] = useState(0);
  const [order, setOrder] = useState<"newest" | "engagement">("newest");
  const [category, setCategory] = useState<string>("");

  useEffect(() => {
    store.fetch({ limit: PAGE, offset, order, category: category || undefined });
  }, [offset, order, category, refreshKey]);

  useEffect(() => {
    if (refreshKey) store.invalidate();
  }, [refreshKey]);

  const items = store.items;
  const totals = store.totals;
  const loading = store.loading;
  const error = store.error;

  const hasPrev = offset > 0;
  const hasNext = totals != null && offset + PAGE < totals.total;

  return (
    <section className="space-y-4">
      <header className="flex items-baseline gap-4 flex-wrap">
        <div>
          <p className="eyebrow mb-1">Posts library</p>
          <h2 className="text-base font-semibold text-fg">
            {totals ? `${fmtNumber(totals.total)} ingested posts` : "Loading…"}
          </h2>
          {totals && (
            <p className="font-mono text-2xs text-muted mt-1">
              {fmtNumber(totals.images)} images · {fmtNumber(totals.videos)} videos ·{" "}
              {fmtNumber(totals.carousels)} carousels · {fmtNumber(totals.embedded)} embedded ·{" "}
              ♥ {fmtNumber(totals.total_likes)} 💬 {fmtNumber(totals.total_comments)}{" "}
              ▶ {fmtNumber(totals.total_views)}
            </p>
          )}
        </div>
        <div className="ml-auto flex items-center gap-4">
          <div className="flex items-center gap-2">
            <label className="text-xs text-muted">Category</label>
            <select
              value={category}
              onChange={(e) => {
                setCategory(e.target.value);
                setOffset(0);
              }}
              className="rounded border border-border bg-bg-subtle text-sm px-2 py-1"
            >
              <option value="">All</option>
              <option value="img_poster">Poster</option>
              <option value="img_poster_human">Poster + Human</option>
              <option value="img_poster_animated">Animated</option>
              <option value="video_interview">Interview</option>
              <option value="other">Other</option>
            </select>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-muted">Sort</label>
            <select
              value={order}
              onChange={(e) => {
                setOrder(e.target.value as "newest" | "engagement");
                setOffset(0);
              }}
              className="rounded border border-border bg-bg-subtle text-sm px-2 py-1"
            >
              <option value="newest">Newest</option>
              <option value="engagement">Engagement</option>
            </select>
          </div>
        </div>
      </header>

      {error && (
        <p className="text-sm text-danger" role="alert">
          {error}
        </p>
      )}

      {loading ? (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
          {Array.from({ length: PAGE }).map((_, i) => (
            <Skeleton key={i} h="h-48" rounded="rounded-xl" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <p className="text-sm text-muted">
          No posts ingested yet. Run a sync from the panel above.
        </p>
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
          {items.map((p) => (
            <PostCard key={p.id} post={p} onClick={() => onOpen(p.id)} />
          ))}
        </div>
      )}

      <footer className="flex items-center justify-between text-sm">
        <button
          onClick={() => setOffset(Math.max(0, offset - PAGE))}
          disabled={!hasPrev || loading}
          className="btn btn-ghost"
        >
          ← Previous
        </button>
        <span className="font-mono text-xs text-muted tabular-nums">
          {totals
            ? `${Math.min(offset + 1, totals.total)}–${Math.min(offset + PAGE, totals.total)} of ${totals.total}`
            : ""}
        </span>
        <button
          onClick={() => setOffset(offset + PAGE)}
          disabled={!hasNext || loading}
          className="btn btn-ghost"
        >
          Next →
        </button>
      </footer>
    </section>
  );
}

const CATEGORY_LABELS: Record<string, { label: string; color: string }> = {
  img_poster: { label: "Poster", color: "bg-blue-500/80" },
  img_poster_human: { label: "Human", color: "bg-success/80" },
  img_poster_animated: { label: "Animated", color: "bg-purple-500/80" },
  video_interview: { label: "Interview", color: "bg-warning/80" },
  other: { label: "Other", color: "bg-gray-500/80" },
};

function PostCard({ post, onClick }: { post: InstagramPostRow; onClick: () => void }) {
  const thumb =
    post.first_asset_id != null
      ? api.instagramIngest.assetFileUrl(post.id, post.first_asset_id)
      : null;
  const isVideo = post.first_asset_type === "instagram";
  const typeBadge =
    post.media_type === "VIDEO"
      ? "REEL"
      : post.media_type === "CAROUSEL_ALBUM"
      ? `CAROUSEL ×${post.children_count || post.asset_count}`
      : "IMAGE";
  const cat = post.content_category ? CATEGORY_LABELS[post.content_category] : null;
  return (
    <button
      onClick={onClick}
      className="card card-hover text-left p-0 overflow-hidden flex flex-col group"
      title={post.caption || ""}
    >
      <div className="aspect-square bg-bg-muted relative overflow-hidden">
        {thumb && isVideo ? (
          <>
            {/* Use <video preload=metadata> so the browser fetches enough
                bytes to display the first frame as a thumbnail. muted +
                playsInline lets some browsers actually render frame 0
                without a play tap. */}
            <video
              src={thumb}
              className="w-full h-full object-cover"
              preload="metadata"
              muted
              playsInline
              // Jumping a hair past 0 forces frame extraction on more browsers
              onLoadedMetadata={(e) => {
                const v = e.currentTarget;
                if (v.duration > 0.1) v.currentTime = 0.1;
              }}
            />
            <span
              aria-hidden
              className="pointer-events-none absolute inset-0 flex items-center justify-center"
            >
              <span className="w-10 h-10 rounded-full bg-black/55 backdrop-blur-sm flex items-center justify-center text-white text-base shadow-lg">
                ▶
              </span>
            </span>
          </>
        ) : thumb ? (
          <img
            src={thumb}
            alt=""
            className="w-full h-full object-cover"
            loading="lazy"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-muted text-xs">
            no media
          </div>
        )}
        <span className="absolute top-2 left-2 font-mono text-3xs px-1.5 py-0.5 rounded bg-black/70 text-white">
          {typeBadge}
        </span>
        {cat && (
          <span className={`absolute bottom-2 left-2 font-mono text-3xs px-1.5 py-0.5 rounded text-white ${cat.color}`}>
            {cat.label}
          </span>
        )}
        {!post.embedded && (
          <span className="absolute top-2 right-2 font-mono text-3xs px-1.5 py-0.5 rounded bg-danger/20 text-danger">
            unembedded
          </span>
        )}
      </div>
      <div className="p-3 space-y-1 flex-1 flex flex-col">
        <p className="text-xs text-fg/80 line-clamp-2">
          {post.preview || post.caption || "(no caption)"}
        </p>
        <div className="mt-auto flex items-center gap-3 font-mono text-2xs text-muted tabular-nums">
          <span>♥ {fmtNumber(post.like_count ?? 0)}</span>
          <span>💬 {fmtNumber(post.comments_count ?? 0)}</span>
          {post.view_count != null && <span>▶ {fmtNumber(post.view_count)}</span>}
          <span className="ml-auto">{fmtRel(post.timestamp_posted)}</span>
        </div>
      </div>
    </button>
  );
}
