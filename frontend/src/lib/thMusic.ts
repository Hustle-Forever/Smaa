/**
 * Background music under a finished TH v2 reel (the Final step's music
 * panel): what the run's music state means for the page.
 */
import type { ReelMusicState } from "./types";

/** Whether the creator should keep polling the run: Suno is composing the
 *  track, or the server is laying it under the reel. */
export function musicBusy(m: ReelMusicState | null | undefined): boolean {
  return !!m && (m.track_status === "generating" || m.mixing);
}

/** A length as m:ss (22 -> "0:22", 75.4 -> "1:15"), whole seconds down,
 *  the way the video's own controls show the same reel (22.8 -> "0:22"). */
export function clock(sec: number): string {
  const s = Math.max(0, Math.floor(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
