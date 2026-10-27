/**
 * Video Editor · Fonts tab (sidebar)
 *
 * Browse the curated Google fonts + uploaded brand fonts, and DRAG one onto the
 * AI chat ("use this font"). Until now fonts were only pickable per-text-layer
 * in the right inspector — this gives a place to drag them from. Curated fonts
 * carry just a family; brand fonts also carry their producer_assets id
 * (→ font_asset_id) so the renderer loads the real face.
 */
import { useRef, useState } from "react";

import { api } from "../../../../lib/api";
import { CURATED_FONTS } from "../../../../lib/fonts";
import { refreshBrandFonts, useBrandFonts } from "../useBrandFonts";
import { attachmentDragProps } from "./attachmentDnd";

export function FontsPanel() {
  const brandFonts = useBrandFonts();
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  async function onUpload(file: File) {
    setUploading(true);
    try {
      await api.producerAssets.upload({ file, asset_kind: "font", display_name: file.name });
      await refreshBrandFonts();
    } catch {
      /* ignore */
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="h-full flex flex-col">
      <div className="shrink-0 p-2.5 border-b border-border flex items-center justify-between">
        <span className="eyebrow">Fonts</span>
        <button
          onClick={() => fileRef.current?.click()}
          disabled={uploading}
          className="btn btn-ghost text-2xs px-2 py-0.5"
        >
          {uploading ? "Uploading…" : "↑ Upload"}
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".ttf,.otf,.woff,.woff2,font/*"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void onUpload(f);
            e.target.value = "";
          }}
        />
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto p-2 space-y-1.5">
        <p className="text-[10.5px] text-muted px-1 pb-0.5">
          Drag a font into the chat, then say "use this font".
        </p>

        {brandFonts.length > 0 && (
          <>
            <p className="eyebrow px-1 pt-1">Brand fonts</p>
            {brandFonts.map((f) => (
              <FontRow
                key={`brand-${f.id}`}
                family={f.family}
                label={f.family}
                attachment={{
                  kind: "font",
                  name: f.family,
                  fontFamily: f.family,
                  assetId: f.id,
                  brandFont: true,
                }}
              />
            ))}
          </>
        )}

        <p className="eyebrow px-1 pt-1.5">More fonts</p>
        {CURATED_FONTS.map((f) => (
          <FontRow
            key={`curated-${f.family}`}
            family={f.family}
            label={f.label}
            attachment={{
              kind: "font",
              name: f.family,
              fontFamily: f.family,
              brandFont: false,
            }}
          />
        ))}
      </div>
    </div>
  );
}

function FontRow({
  family,
  label,
  attachment,
}: {
  family: string;
  label: string;
  attachment: Parameters<typeof attachmentDragProps>[0];
}) {
  return (
    <div
      {...attachmentDragProps(attachment)}
      title="Drag it into the chat to use it"
      className="flex items-center gap-2.5 p-2 rounded-lg border border-border bg-bg hover:bg-bg-muted hover:border-accent/50 transition-colors cursor-grab active:cursor-grabbing select-none"
    >
      <span className="w-9 h-9 shrink-0 rounded-md bg-black/80 text-white flex items-center justify-center text-[17px]" style={{ fontFamily: family }}>
        Aa
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-[12px] text-fg/90 truncate" style={{ fontFamily: family }}>
          {label}
        </span>
        <span className="block text-[9px] font-mono text-muted">drag to chat</span>
      </span>
    </div>
  );
}
