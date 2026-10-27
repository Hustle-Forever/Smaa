/**
 * Video Editor — drag-and-drop "chat attachments".
 *
 * Any left-sidebar item (sticker / font / audio / media / shape / text preset)
 * can be dragged onto the AI chat. The drag payload carries the item's CONCRETE
 * reference (asset id / font family / source kind+id / shape variant) so the AI
 * applies the EXACT item the user picked instead of guessing from a description.
 *
 * Shared by every sidebar panel (the drag source) and AiPanel (the drop target).
 */
import type { DragEvent } from "react";

export type ChatAttachmentKind = "sticker" | "font" | "audio" | "media" | "shape" | "text";

export interface ChatAttachment {
  /** Local-only unique id for the chip (key + remove). Not sent to the backend. */
  uid: string;
  kind: ChatAttachmentKind;
  /** Human label shown on the chip + given to the AI as context. */
  name: string;

  // --- concrete references (kind-specific) ---
  /** producer_assets id — sticker (image), audio, or a BRAND font. */
  assetId?: number;
  /** font family string (both curated + brand fonts carry this). */
  fontFamily?: string;
  /** true = brand font (has assetId → font_asset_id); false = curated Google font. */
  brandFont?: boolean;
  /** media-bin source. */
  sourceKind?: "animation" | "upload" | "composition" | "still";
  sourceId?: number;
  /** shape variant (rect / ellipse / …). */
  variant?: string;
  /** text-style preset bundle (fontFamily/fontSize/fontWeight/background…). */
  preset?: Record<string, unknown>;

  /** Optional thumbnail (stickers) for a richer chip. Not sent to the backend. */
  thumbUrl?: string;
}

export const ATTACHMENT_MIME = "application/x-ve-chat-attachment";

let _uid = 0;
export function makeAttachmentUid(): string {
  _uid += 1;
  return `att_${_uid}_${Math.round(performance.now())}`;
}

/** A stable key for an attachment's referenced item — used to de-duplicate drops. */
export function attachmentRefKey(a: Pick<ChatAttachment, "kind" | "assetId" | "fontFamily" | "sourceKind" | "sourceId" | "variant" | "name">): string {
  switch (a.kind) {
    case "sticker":
    case "audio":
      return `${a.kind}:${a.assetId}`;
    case "font":
      return `font:${a.fontFamily}`;
    case "media":
      return `media:${a.sourceKind}:${a.sourceId}`;
    case "shape":
      return `shape:${a.variant}`;
    case "text":
      return `text:${a.name}`;
    default:
      return `${a.kind}:${a.name}`;
  }
}

/** Spread onto a draggable sidebar item: `<button {...attachmentDragProps(att)}>`. */
export function attachmentDragProps(att: Omit<ChatAttachment, "uid">) {
  return {
    draggable: true as const,
    onDragStart: (e: DragEvent) => {
      try {
        e.dataTransfer.setData(ATTACHMENT_MIME, JSON.stringify(att));
        e.dataTransfer.setData("text/plain", att.name); // graceful fallback
        e.dataTransfer.effectAllowed = "copy";
      } catch {
        /* some browsers throw on custom MIME — ignore, drag just won't attach */
      }
    },
  };
}

/** Read an attachment payload from a drop event's dataTransfer (null if none). */
export function parseAttachmentDrop(dt: DataTransfer): Omit<ChatAttachment, "uid"> | null {
  try {
    const raw = dt.getData(ATTACHMENT_MIME);
    if (!raw) return null;
    const a = JSON.parse(raw);
    if (a && typeof a === "object" && typeof a.kind === "string") return a;
  } catch {
    /* not our payload */
  }
  return null;
}

/** The minimal payload sent to the backend (drops the local-only fields). */
export interface ChatAttachmentApi {
  kind: string;
  name: string;
  asset_id?: number;
  font_family?: string;
  brand_font?: boolean;
  source_kind?: string;
  source_id?: number;
  variant?: string;
}

export function attachmentForApi(a: ChatAttachment): ChatAttachmentApi {
  return {
    kind: a.kind,
    name: a.name,
    asset_id: a.assetId,
    font_family: a.fontFamily,
    brand_font: a.brandFont,
    source_kind: a.sourceKind,
    source_id: a.sourceId,
    variant: a.variant,
  };
}
