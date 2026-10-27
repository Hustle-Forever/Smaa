/**
 * Video Editor · track header (gutter row) — mute / lock / reorder / remove.
 */
import { useVideoEditorStore } from "../../../lib/store";
import type { VideoEditTrack } from "../../../lib/types";

function IconBtn({
  children,
  active,
  disabled,
  danger,
  title,
  onClick,
}: {
  children: React.ReactNode;
  active?: boolean;
  disabled?: boolean;
  danger?: boolean;
  title: string;
  onClick: () => void;
}) {
  return (
    <button
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={[
        "w-[18px] h-[18px] rounded text-3xs leading-none flex items-center justify-center transition-colors",
        disabled
          ? "text-muted/30 cursor-not-allowed"
          : active
          ? danger
            ? "bg-danger/15 text-danger"
            : "bg-accent/15 text-accent"
          : "text-muted hover:bg-bg-muted hover:text-fg",
      ].join(" ")}
    >
      {children}
    </button>
  );
}

export function TrackHeader({
  track,
  label,
  height,
  canUp,
  canDown,
  canRemove,
}: {
  track: VideoEditTrack;
  label: string;
  height: number;
  canUp: boolean;
  canDown: boolean;
  canRemove: boolean;
}) {
  const setTrackMuted = useVideoEditorStore((s) => s.setTrackMuted);
  const setTrackLocked = useVideoEditorStore((s) => s.setTrackLocked);
  const reorderTrack = useVideoEditorStore((s) => s.reorderTrack);
  const removeTrack = useVideoEditorStore((s) => s.removeTrack);

  return (
    <div
      style={{ height }}
      className="border-b border-border/70 flex items-center gap-0.5 px-1.5"
    >
      <span
        className="text-3xs font-mono text-muted w-5 shrink-0"
        title={track.kind === "video" ? "A row for video" : "A row for sound"}
      >
        {label}
      </span>
      <IconBtn
        title={track.muted ? "Turn the sound back on" : "Turn the sound off"}
        active={track.muted}
        danger
        onClick={() => setTrackMuted(track.id, !track.muted)}
      >
        {track.muted ? "🔇" : "🔊"}
      </IconBtn>
      <IconBtn
        title={track.locked ? "Unlock this row so you can change it" : "Lock this row so nothing moves by accident"}
        active={track.locked}
        onClick={() => setTrackLocked(track.id, !track.locked)}
      >
        {track.locked ? "🔒" : "🔓"}
      </IconBtn>
      <div className="flex-1" />
      {track.kind === "video" && (
        <>
          <IconBtn title="Move this row up, in front of the others" disabled={!canUp} onClick={() => reorderTrack(track.id, "up")}>
            ↑
          </IconBtn>
          <IconBtn title="Move this row down, behind the others" disabled={!canDown} onClick={() => reorderTrack(track.id, "down")}>
            ↓
          </IconBtn>
        </>
      )}
      <IconBtn title="Delete this row and everything on it" danger disabled={!canRemove} onClick={() => removeTrack(track.id)}>
        ×
      </IconBtn>
    </div>
  );
}
