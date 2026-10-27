// Rebrand studio — what can be done with the version on stage, under it.
//
//   ready              Add more to vN (carry on from it: made in house from
//                      the original, its whole recipe; with Omni's video
//                      under it — made by Omni, or in house on top of one —
//                      Add to it | Change it in the rail — after Generate the
//                      studio does it itself), Download
//   ready, video lost  Make it again (made in house: from its recipe and
//                      the original, the same video, $0) in place of
//                      Download
//   on its way         Stop
//   failed / stopped   Try again (the same recipe, on the same version when it
//                      was built on one — a fresh take)
//   made by Omni       Make it in house (in place of Try again: new work is
//                      made in house — its look redone by our own renderer
//                      from the original, $0)
//   ready, with a warning   a symbol Omni drew itself: Try again (a fresh
//                      Omni take), or Clean it up ($0 — Change it on it with
//                      the stray-drawing clean-up, reusing Omni's clip);
//                      icons that couldn't be placed or drawn: Place icons
//                      again ($0 — the icons laid on it again, no Omni)
//   ready, ⋯           Rate (1-5 and a note: RateVersion takes this row)
//   Try again, when its icons would come out differently now: one line
//                      asks first ("The icons will be placed fresh — keep
//                      going?") in place of the buttons
//
// Secondary buttons only: Generate is the one accent button in the studio.

import { Download, Eraser, Home, Layers, RefreshCw, RotateCcw, Square, Stamp, Star } from "lucide-react";

import { recipeOfVersion, type HarnessVersion } from "../../../lib/rebrandHarnessApi";
import { MenuItem, OverflowMenu } from "../../ui/controls";
import { Spinner } from "../../ui/Spinner";
import { COPY } from "./studioCopy";
import { madeInHouse, omniArm, versionPhase } from "./studioModel";

/** The run's warning that Omni drew a symbol of its own (stray.warning). */
const STRAY = /^Omni drew its own symbol\b/;
/** ... that some of our icons are missing (runner.icons_for). */
const ICONS_MISSING = /^The icons couldn't be placed\b|couldn't be drawn, so it was left out$/;

export function VersionActions({
  version,
  no,
  onStop,
  onTryAgain,
  onMakeInHouse,
  onRestamp,
  onBuildOn,
  onRate,
  onCleanUp,
  onRemake,
  remaking = null,
  confirmFresh = null,
  building = false,
  busy = false,
}: {
  version: HarnessVersion;
  no: number;
  onStop: () => void;
  onTryAgain: () => void;
  /** A version Omni made, redone in house — absent where it can't be. */
  onMakeInHouse?: () => void;
  /** Its icons laid on it again, no Omni — absent where it can't be. */
  onRestamp?: () => void;
  /** "Build on vN" — absent where building on a version isn't offered. */
  onBuildOn?: () => void;
  /** Rate it (its ⋯ menu) — absent where it can't be rated. */
  onRate?: () => void;
  /** "Clean it up" beside Omni's own symbol — absent where it can't be. */
  onCleanUp?: () => void;
  /** A version made in house whose video is lost, made again ($0). */
  onRemake?: () => void;
  /** It is being made again: how far (null: not known yet); `error` when
   *  making it again failed (said beside the button). */
  remaking?: { progress: number | null; error: string | null; running: boolean } | null;
  /** Try again would place its icons afresh: go on, or not. */
  confirmFresh?: { onGo: () => void; onCancel: () => void } | null;
  /** The draft is already built on this version. */
  building?: boolean;
  /** Another version is on its way (one at a time per scene). */
  busy?: boolean;
}) {
  const phase = versionPhase(version);
  const download = omniArm(version)?.download_url ?? null;
  const recipe = recipeOfVersion(version);
  const warnings = phase === "ready" ? omniArm(version)?.warnings ?? [] : [];
  // A finished version the run warns about ("Omni drew its own symbol at
  // 4.2 s — Try again") can be made again as it is.
  const stray = warnings.some((w) => STRAY.test(w));
  const iconsMissing = warnings.some((w) => ICONS_MISSING.test(w));
  // Omni's: made again, it is made in house (Try again would be Omni again).
  const house = madeInHouse(version);
  const inHouse = !house && !!recipe && !!onMakeInHouse && (phase === "ready" || phase === "failed" || phase === "stopped");
  if (confirmFresh) {
    return (
      <div role="alert" className="flex flex-wrap items-center justify-center gap-2 text-xs text-fg motion-safe:animate-fadeIn">
        <span>{COPY.freshIcons}</span>
        <button type="button" className="btn btn-xs btn-secondary" onClick={confirmFresh.onGo} disabled={busy}>
          {COPY.keepGoing}
        </button>
        <button type="button" className="btn btn-xs btn-ghost" onClick={confirmFresh.onCancel}>
          {COPY.cancel}
        </button>
      </div>
    );
  }
  const stars = version.rating?.omni_stars ?? null;
  // Made in house, its video gone: made again from what it is made of.
  const lost = phase === "ready" && house && !!version.lost;
  return (
    <div className="flex flex-wrap items-center justify-center gap-2">
      {lost && remaking?.running && (
        <span role="status" className="flex items-center gap-1.5 text-2xs text-muted">
          <Spinner size="xs" tone="accent" /> {COPY.remaking(remaking.progress)}
        </span>
      )}
      {lost && !remaking?.running && onRemake && (
        <>
          {remaking?.error && (
            <span role="alert" className="basis-full text-center text-2xs text-danger">
              {COPY.remakeFailed} {remaking.error}
            </span>
          )}
          <button type="button" className="btn btn-xs btn-secondary" onClick={onRemake} title={COPY.remakeTitle}>
            <RefreshCw className="h-3 w-3" aria-hidden /> {COPY.remake}
          </button>
        </>
      )}
      {(phase === "queued" || phase === "running") && (
        <button type="button" className="btn btn-xs btn-ghost" onClick={onStop}>
          <Square className="h-3 w-3" aria-hidden /> {COPY.stop}
        </button>
      )}
      {(phase === "failed" || phase === "stopped" || stray) && recipe && house && (
        <button
          type="button"
          className="btn btn-xs btn-ghost"
          onClick={onTryAgain}
          disabled={busy}
          title={COPY.retryVersionTitle}
        >
          <RotateCcw className="h-3 w-3" aria-hidden /> {COPY.retryVersion}
        </button>
      )}
      {inHouse && (
        <button
          type="button"
          className="btn btn-xs btn-ghost"
          onClick={onMakeInHouse}
          disabled={busy}
          title={COPY.makeInHouseTitle}
        >
          <Home className="h-3 w-3" aria-hidden /> {COPY.makeInHouse}
        </button>
      )}
      {stray && onCleanUp && (
        <button
          type="button"
          className="btn btn-xs btn-ghost"
          onClick={onCleanUp}
          disabled={busy}
          title={COPY.cleanUpTitle}
        >
          <Eraser className="h-3 w-3" aria-hidden /> {COPY.cleanUp}
        </button>
      )}
      {iconsMissing && onRestamp && (
        <button
          type="button"
          className="btn btn-xs btn-ghost"
          onClick={onRestamp}
          disabled={busy}
          title={COPY.icons.restampTitle}
        >
          <Stamp className="h-3 w-3" aria-hidden /> {COPY.icons.restamp}
        </button>
      )}
      {phase === "ready" && onBuildOn && (
        <button
          type="button"
          className={`btn btn-xs ${building ? "btn-secondary" : "btn-ghost"}`}
          onClick={onBuildOn}
          // A classic-panel version has no recipe to build on.
          disabled={!recipe || building}
          aria-pressed={building}
          title={!recipe ? COPY.buildOnClassic : building ? COPY.buildingOn(no) : COPY.buildOnTitle(no)}
        >
          <Layers className="h-3 w-3" aria-hidden /> {building ? COPY.buildingOn(no) : COPY.buildOn(no)}
        </button>
      )}
      {phase === "ready" && download && !lost && (
        <a href={download} className="btn btn-xs btn-ghost">
          <Download className="h-3 w-3" aria-hidden /> {COPY.download}
        </a>
      )}
      {phase === "ready" && onRate && (
        <OverflowMenu label={COPY.versionMenu(no)} size="sm" align="right" side="top">
          <MenuItem onClick={onRate} Icon={Star}>
            {stars ? `${COPY.rateAgain} (${"★".repeat(stars)})` : COPY.rate}
          </MenuItem>
        </OverflowMenu>
      )}
    </div>
  );
}
