/**
 * A module-level handle to the live <Player> so the timeline (ruler/playhead/
 * transport) can drive playback without prop-drilling a ref. PreviewPane sets
 * `current` on mount and clears it on unmount.
 */
import type { PlayerRef } from "@remotion/player";

export const playerHandle: { current: PlayerRef | null } = { current: null };
