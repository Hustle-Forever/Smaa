/**
 * Unified asset picker modal for Yas Producer.
 *
 * Backward-compatible superset of the three historical pickers:
 * - AssetPickerModal (v1, Poster + Human): single content_category filter,
 *   multi-select up to a cap with a "Cap reached" toast (block behavior).
 * - AssetPickerModalV2 (Human Element Video): string[] categories, preselected
 *   asset pre-seeded into the picked set, radio single-select (clicking a
 *   different asset REPLACES the pick instead of being blocked).
 * - GeneralRefPickerModal (General AI Video): category-chip filter row across
 *   the whole library + image-only restriction (Gemini refs need images).
 *
 * Existing v1 callers (PosterCreator, PosterHumanCreator) pass the old prop
 * shape and get byte-identical behavior: string contentCategory, no chips,
 * block single-select, "Done" confirm label.
 *
 * Portaled to document.body to escape the page-transition transform that
 * traps `fixed` descendants (see CLAUDE.md modal-pattern note).
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { useModalChrome } from "../../lib/useModalChrome";
import { createPortal } from "react-dom";

import { api } from "../../lib/api";
import { Thumb } from "../Thumb";
import { useProducerAssetsStore } from "../../lib/store";
import { useToast } from "../ToastProvider";
import type { ProducerAssetRow } from "../../lib/types";

// Image content-categories a reference can come from (mirrors Assets.tsx, minus
// the video-only "video_interview" kind which has no still to feed Gemini).
const CATEGORIES: { key: string; label: string }[] = [
  { key: "all", label: "All" },
  { key: "img_bg", label: "Background" },
  { key: "img_human_character", label: "Human Characters" },
  { key: "img_poster_human", label: "Poster + Human" },
  { key: "img_poster", label: "Poster" },
  { key: "img_poster_animated", label: "Animated" },
  { key: "img_intro", label: "Intro" },
  { key: "img_outro", label: "Outro" },
];

interface Props {
  /** One content_category, or several (e.g. the character slot accepts both
   *  interview-extracted characters AND generated Human Characters).
   *  Omit when `showCategoryChips` drives the filtering instead. */
  contentCategory?: string | string[];
  maxSelection: number;           // 2 for v1
  alreadySelectedAssetIds: number[]; // hide these from the grid
  /** Current slot selection — pre-seeded + highlighted so the user sees what's
   *  already picked and can switch to a different asset in one click. */
  preselectedAssetId?: number | null;
  /** Behavior when maxSelection === 1: "block" (default, v1) rejects a second
   *  pick with a cap toast; "radio" replaces the current pick in one click. */
  singleSelectMode?: "block" | "radio";
  /** Render the category-chip filter row (whole-library browsing). */
  showCategoryChips?: boolean;
  /** Restrict the grid to image assets (asset_kind === "image"). */
  imageOnly?: boolean;
  /** Footer primary button label (defaults to "Done"). */
  confirmLabel?: string;
  /** Opt-in second source, shown as a tab next to the library (the Human
   *  Element Video intro/outro picker's "Previous videos"). Its content
   *  picks on its own; omitted, the modal is the library alone, as before. */
  extraTab?: { label: string; content: ReactNode };
  onPick: (assets: ProducerAssetRow[]) => void;
  onSkip?: () => void;            // optional "skip refs entirely" shortcut
  onClose: () => void;
}

export function AssetPickerModal({
  contentCategory,
  maxSelection,
  alreadySelectedAssetIds,
  preselectedAssetId,
  singleSelectMode = "block",
  showCategoryChips = false,
  imageOnly = false,
  confirmLabel = "Done",
  extraTab,
  onPick,
  onSkip,
  onClose,
}: Props) {
  const store = useProducerAssetsStore();
  const [search, setSearch] = useState("");
  const [cat, setCat] = useState<string>("all");
  const [tab, setTab] = useState<"library" | "extra">("library");
  const onExtra = !!extraTab && tab === "extra";
  const [picked, setPicked] = useState<Set<number>>(
    () => (preselectedAssetId != null ? new Set([preselectedAssetId]) : new Set()),
  );
  const toast = useToast();

  // Fetch the FULL list (same cache key as the Assets page / TalkingHead studio
  // → reuses the already-loaded list instead of refetching). The category
  // narrowing happens client-side in `visibleItems` below.
  useEffect(() => {
    store.fetch({ limit: 500 });
  }, []);

  // ESC closes + body scroll lock
  // Escape, scroll lock, focus trap and focus restore in one place. The first
  // two were already here; the trap is the new part -- without it Tab walked
  // straight out of the dialog and into the page behind the overlay, which
  // makes a dialog modal only to a mouse.
  const panelRef = useRef<HTMLDivElement>(null);
  const { trapTab } = useModalChrome({ onClose, panelRef });

  // The store holds ALL fetched assets in memory. Filter for this category
  // + exclude already-picked + client-side search match on display_name + preview.
  const visibleItems = useMemo(() => {
    const q = search.trim().toLowerCase();
    const excluded = new Set(alreadySelectedAssetIds);
    return store.items.filter((a) => {
      if (imageOnly && a.asset_kind !== "image") return false; // images only (Gemini refs)
      if (excluded.has(a.id)) return false;
      const meta = (a.metadata as any) ?? {};
      if (showCategoryChips) {
        // Chip row drives the filter; "all" = no category narrowing.
        if (cat !== "all" && meta.content_category !== cat) return false;
      } else if (contentCategory != null) {
        if (Array.isArray(contentCategory)) {
          if (!contentCategory.includes(meta.content_category)) return false;
        } else if (meta.content_category !== contentCategory) {
          return false;
        }
      }
      if (!q) return true;
      const hay = [a.display_name ?? "", a.preview ?? "", a.slug].join(" ").toLowerCase();
      return hay.includes(q);
    });
  }, [store.items, search, cat, showCategoryChips, imageOnly, contentCategory, alreadySelectedAssetIds]);

  function toggle(asset: ProducerAssetRow) {
    if (maxSelection === 1 && singleSelectMode === "radio") {
      // Radio behavior: click selected → deselect; click other → replace.
      setPicked(picked.has(asset.id) ? new Set() : new Set([asset.id]));
      return;
    }
    // Cap-reached check happens OUTSIDE the setState updater. Calling
    // toast.info() inside the updater mutates ToastProvider state during
    // reconciliation of this component and triggers a React warning.
    if (!picked.has(asset.id) && picked.size >= maxSelection) {
      toast.info(`You can pick up to ${maxSelection} more`, "Cap reached");
      return;
    }
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(asset.id)) {
        next.delete(asset.id);
      } else {
        next.add(asset.id);
      }
      return next;
    });
  }

  function commit() {
    const chosen = store.items.filter((a) => picked.has(a.id));
    onPick(chosen);
  }

  const empty = !store.loading && visibleItems.length === 0 && !search;

  // Human-readable category tag for the empty state (v1 shows the raw
  // content_category string; arrays / chip mode fall back to a generic label).
  const emptyCategoryLabel = Array.isArray(contentCategory)
    ? contentCategory.join(", ")
    : contentCategory ?? "matching";

  return createPortal(
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 110,
        background: "rgba(0,0,0,0.75)",
        backdropFilter: "blur(2px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "16px",
      }}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        onKeyDown={trapTab}
        onClick={(e) => e.stopPropagation()}
        className="relative bg-bg rounded-2xl shadow-2xl border border-border overflow-hidden flex flex-col"
        style={{ width: "min(720px, 100%)", maxHeight: "min(840px, 92vh)" }}
      >
        {/* Header */}
        <header className="shrink-0 px-4 py-3 border-b border-border flex items-center gap-2.5 bg-bg">
          <h2 className="text-sm font-semibold text-fg flex-1">
            Choose reference{maxSelection > 1 ? `s (up to ${maxSelection})` : ""}
          </h2>
          <button
            onClick={onClose}
            aria-label="Close"
            className="w-7 h-7 rounded-full bg-bg-muted hover:bg-border text-fg flex items-center justify-center text-base leading-none transition-colors"
          >
            ×
          </button>
        </header>

        {/* Opt-in second source (a tab next to the library). */}
        {extraTab && (
          <div className="shrink-0 px-4 pt-2 flex gap-1.5 border-b border-border" role="tablist">
            {(["library", "extra"] as const).map((t) => (
              <button
                key={t}
                type="button"
                role="tab"
                aria-selected={tab === t}
                onClick={() => setTab(t)}
                className={[
                  "px-3 py-1.5 -mb-px text-[12px] border-b-2 transition-colors",
                  tab === t ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg",
                ].join(" ")}
              >
                {t === "library" ? "Library" : extraTab.label}
              </button>
            ))}
          </div>
        )}

        {onExtra ? (
          <>
            <div className="flex-1 min-h-0 overflow-y-auto p-4">{extraTab?.content}</div>
            <footer className="shrink-0 px-4 py-3 border-t border-border bg-bg-muted/50 flex items-center gap-3">
              <p className="text-xs text-muted flex-1">Click a card to choose it.</p>
              <button onClick={onClose} className="btn btn-ghost text-xs">Cancel</button>
            </footer>
          </>
        ) : (
        <>
        {/* Category chips */}
        {showCategoryChips && (
          <div className="shrink-0 px-4 pt-3 flex flex-wrap gap-1.5">
            {CATEGORIES.map((c) => (
              <button
                key={c.key}
                onClick={() => setCat(c.key)}
                className={[
                  "px-2.5 py-1 rounded-full border text-[12px] transition-colors",
                  cat === c.key
                    ? "border-accent bg-accent/8 text-fg"
                    : "border-border bg-bg-subtle text-muted hover:bg-bg-muted",
                ].join(" ")}
              >
                {c.label}
              </button>
            ))}
          </div>
        )}

        {/* Search */}
        <div className="shrink-0 px-4 py-2 border-b border-border">
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by name or description…"
            className="w-full rounded-lg border border-border bg-bg-subtle px-3 py-2 text-sm focus:outline-none focus:border-accent"
            autoFocus
          />
        </div>

        {/* Body */}
        <div className="flex-1 min-h-0 overflow-y-auto p-4">
          {store.loading && (
            <p className="text-sm text-muted text-center py-8">Loading…</p>
          )}

          {empty && (
            <div className="space-y-3 py-6 text-center">
              <p className="text-sm text-fg">
                No <span className="font-mono text-xs px-1.5 py-0.5 rounded bg-bg-muted">{emptyCategoryLabel}</span> references yet.
              </p>
              <p className="text-xs text-muted leading-relaxed max-w-md mx-auto">
                Either add one to the Assets library, or skip references and
                generate from the topic alone.
              </p>
              <div className="flex items-center gap-2 justify-center pt-2">
                {onSkip && (
                  <button
                    onClick={() => {
                      onSkip();
                      onClose();
                    }}
                    className="btn btn-primary text-xs"
                  >
                    Skip — generate from topic only
                  </button>
                )}
                <a
                  href="#assets"
                  onClick={onClose}
                  className="btn btn-ghost text-xs"
                >
                  Go to Assets
                </a>
              </div>
            </div>
          )}

          {!store.loading && visibleItems.length === 0 && search && (
            <p className="text-sm text-muted text-center py-8">
              No matches for “{search}”.
            </p>
          )}

          {visibleItems.length > 0 && (
            <div className="grid grid-cols-3 gap-3">
              {visibleItems.map((a) => {
                const isPicked = picked.has(a.id);
                return (
                  <button
                    key={a.id}
                    onClick={() => toggle(a)}
                    className={[
                      "relative rounded-xl overflow-hidden border-2 transition-all text-left bg-bg-muted/30",
                      isPicked
                        ? "border-accent ring-2 ring-accent/30"
                        : "border-border hover:border-fg/30",
                    ].join(" ")}
                  >
                    <div className="aspect-square bg-black flex items-center justify-center">
                      <Thumb
                        src={api.producerAssets.thumbUrlFor(a)}
                        fallbackSrc={api.producerAssets.fileUrl(a.id)}
                        alt={a.display_name ?? a.slug}
                        className="w-full h-full"
                      />
                    </div>
                    <div className="px-2 py-1.5 bg-bg-muted/50">
                      <p className="text-xs text-fg truncate">
                        {a.display_name || a.slug}
                      </p>
                      {a.preview && (
                        <p className="text-3xs text-muted truncate">
                          {a.preview}
                        </p>
                      )}
                    </div>
                    {isPicked && (
                      <span className="absolute top-1.5 right-1.5 w-6 h-6 rounded-full bg-accent text-white text-xs flex items-center justify-center">
                        ✓
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {/* Footer */}
        <footer className="shrink-0 px-4 py-3 border-t border-border bg-bg-muted/50 flex items-center gap-3">
          <p className="text-xs text-muted flex-1">
            {maxSelection === 1 && singleSelectMode === "radio"
              ? "Click an asset to choose it (replaces the current pick)."
              : picked.size === 0
                ? `Pick up to ${maxSelection}.`
                : `${picked.size} of ${maxSelection} selected.`}
          </p>
          <button onClick={onClose} className="btn btn-ghost text-xs">Cancel</button>
          <button
            onClick={commit}
            disabled={picked.size === 0}
            className="btn btn-primary text-xs"
          >
            {confirmLabel} {picked.size > 0 ? `(${picked.size})` : ""}
          </button>
        </footer>
        </>
        )}
      </div>
    </div>,
    document.body,
  );
}
