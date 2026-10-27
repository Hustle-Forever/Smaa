// Rebrand — one pattern card, and the tile of a pattern being uploaded.
//
// Shared by the classic panel's single-select picker (RebrandPatternPicker,
// role="radio") and the Rebrand studio's pattern library (role="checkbox"
// when several can be ticked, "radio" when one is swapped in). The classic
// picker renders exactly what it did before this was pulled out of it.

import { useState, type ReactNode } from "react";
import { X } from "lucide-react";

import type { RebrandPattern } from "../../lib/rebrandApi";
import { Spinner } from "../ui/Spinner";

/** An upload re-made under the same name can get the same slug, and so the
 *  same preview URL — its timestamp keeps the browser from showing the old one. */
export function previewSrc(p: RebrandPattern): string {
  if (p.source !== "upload" || !p.created_at) return p.preview_url;
  const sep = p.preview_url.includes("?") ? "&" : "?";
  return `${p.preview_url}${sep}v=${encodeURIComponent(p.created_at)}`;
}

export function PatternCard({
  p,
  selected,
  disabled,
  deleting,
  onSelect,
  onDelete,
  role = "radio",
  caption,
  title,
}: {
  p: RebrandPattern;
  selected: boolean;
  disabled: boolean;
  deleting: boolean;
  onSelect: () => void;
  onDelete: () => void;
  /** "checkbox" where several can be ticked (the studio's Add patterns). */
  role?: "radio" | "checkbox";
  /** A second line under the name ("Full screen", "Up to 3"). */
  caption?: ReactNode;
  /** Overrides the description tooltip (say why a card is disabled). */
  title?: string;
}) {
  const [broken, setBroken] = useState(false);
  return (
    // Two sibling buttons (a button can't hold one): the card picks, × deletes.
    <div className="relative min-w-0">
      <button
        type="button"
        role={role}
        aria-checked={selected}
        disabled={disabled}
        onClick={onSelect}
        title={title ?? (p.description || p.name)}
        className={`w-full text-left rounded-lg border overflow-hidden transition-colors disabled:opacity-50 ${
          selected ? "border-accent ring-2 ring-accent/40" : "border-border hover:border-border-strong"
        }`}
      >
        <span className="block aspect-[4/3] bg-fg">
          {broken ? (
            <span className="w-full h-full flex items-center justify-center text-2xs text-white/60">No preview</span>
          ) : (
            <img
              src={previewSrc(p)}
              alt=""
              loading="lazy"
              draggable={false}
              onError={() => setBroken(true)}
              className="w-full h-full object-contain"
            />
          )}
        </span>
        <span
          className={`block px-1.5 py-1 text-2xs font-medium truncate ${
            selected ? "bg-accent-fade text-accent-hover" : ""
          }`}
        >
          {selected && "✓ "}
          {p.name}
          {caption != null && <span className="block text-3xs font-normal text-muted truncate">{caption}</span>}
        </span>
      </button>
      {p.source === "upload" && (
        <span className="pointer-events-none absolute top-1 left-1 text-3xs font-medium px-1.5 py-0.5 rounded-full bg-white/90 text-fg">
          Uploaded
        </span>
      )}
      {p.deletable && (
        <button
          type="button"
          onClick={onDelete}
          disabled={disabled || deleting}
          aria-label={`Delete the pattern ${p.name}`}
          title="Delete this uploaded pattern"
          className="absolute top-1 right-1 w-5 h-5 rounded-full bg-black/60 text-white hover:bg-danger flex items-center justify-center transition-colors disabled:opacity-50"
        >
          {deleting ? <Spinner size="xs" /> : <X size={12} aria-hidden />}
        </button>
      )}
    </div>
  );
}

/** The tile of an image being analysed on its way into the library. */
export function UploadingCard({ name, previewUrl, note }: { name: string; previewUrl: string; note?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-accent/50 overflow-hidden min-w-0">
      <span className="relative block aspect-[4/3] bg-fg">
        <img src={previewUrl} alt="" className="w-full h-full object-contain opacity-40" />
        <span className="absolute inset-0 flex items-center justify-center">
          <Spinner size="lg" tone="accent" />
        </span>
      </span>
      <span className="block px-1.5 py-1 text-2xs text-muted truncate">
        {name}
        {note != null && <span className="block text-3xs tabular-nums truncate">{note}</span>}
      </span>
    </div>
  );
}
