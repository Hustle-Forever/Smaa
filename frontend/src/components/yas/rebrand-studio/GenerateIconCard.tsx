// Rebrand studio — "Generate with AI": the one paid button in the icon
// picker (typing and searching are free). Shown as a card when nothing the
// search found is a sure match, else as a quiet link under the results.
//
// A press draws ONE bold icon for the typed name (~$0.04, about 10 s; a name
// drawn already comes back free), shown exactly as the video will stamp it —
// the circle and the chip with its words, pictures the server drew with the
// run's own sprite code — with Use it / Try again / Discard. The button is
// disabled while a drawing is on its way, so a double click can never pay
// twice (the server also shares one drawing per name). The drawing lands in
// "Your team's icons" on every machine. Beside it, "Upload your own" (free)
// when the picker can take a file.

import { useEffect, useRef, useState } from "react";
import { Sparkles, Upload } from "lucide-react";

import {
  harnessApi,
  HarnessHttpError,
  type HarnessIconGenerateOffer,
  type HarnessIconGenerated,
  type HarnessIconGlyph,
} from "../../../lib/rebrandHarnessApi";
import { rebrandErrorDetail } from "../../../lib/rebrandApi";
import { ADDED_MAX } from "../../../lib/rebrandIcons";
import { Spinner } from "../../ui/Spinner";
import { IconGlyph } from "./IconGlyph";
import { COPY } from "./studioCopy";

type Phase =
  | { kind: "idle" }
  | { kind: "drawing"; again: boolean }
  | { kind: "drawn"; got: HarnessIconGenerated }
  | { kind: "used"; label: string }
  | { kind: "discarded" }
  | { kind: "failed"; message: string; canRetry: boolean };

export function GenerateIconCard({
  offer,
  prominent,
  found,
  full,
  onUse,
  onTeamChanged,
  onUpload,
}: {
  /** The search's offer for the typed name. */
  offer: HarnessIconGenerateOffer;
  /** A card (nothing found is sure) rather than a quiet link. */
  prominent: boolean;
  /** Anything was found at all ("Nothing fits well" vs "No icon for"). */
  found: boolean;
  /** The user has added all a scene takes (5): it can be drawn, not added. */
  full: boolean;
  onUse: (glyph: HarnessIconGlyph, label: string) => void;
  /** The team's icons changed (a drawing landed, or was discarded). */
  onTeamChanged?: () => void;
  /** "Upload your own" instead (the card's second button). */
  onUpload?: () => void;
}) {
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const name = offer.name;
  const cost = offer.cost_usd;

  const draw = async (again: boolean) => {
    if (phase.kind === "drawing") return;
    setPhase({ kind: "drawing", again });
    try {
      const got = await harnessApi.iconGenerate(name, { again });
      if (!mounted.current) return;
      setPhase({ kind: "drawn", got });
      onTeamChanged?.();
    } catch (err) {
      if (!mounted.current) return;
      const status = err instanceof HarnessHttpError ? err.status : 0;
      // 422 (a brand, a drawing that was not a clean icon) and 429 (the
      // day's drawings are used) say why; only "the AI is down" is worth
      // another press in a minute.
      setPhase({ kind: "failed", message: rebrandErrorDetail(err), canRetry: status === 503 || status === 0 });
    }
  };

  const discard = async (got: HarnessIconGenerated) => {
    try {
      await harnessApi.iconHide(got.id);
    } catch {
      /* it stays in the team's icons; nothing else depends on it */
    }
    if (!mounted.current) return;
    setPhase({ kind: "discarded" });
    onTeamChanged?.();
  };

  const uploadButton = onUpload && (
    <button type="button" onClick={onUpload} title={COPY.icons.uploadTitle} className="btn btn-xs btn-secondary">
      <Upload className="h-3.5 w-3.5" aria-hidden />
      {COPY.icons.uploadYourOwn}
    </button>
  );

  // --- the offer ------------------------------------------------------------------
  if (phase.kind === "idle") {
    if (!offer.offer) {
      return prominent ? (
        <div className="space-y-2 rounded-xl border border-border bg-bg-muted/40 p-2.5 text-2xs text-muted">
          <div>
            <p className="text-xs text-fg">{found ? COPY.icons.weakResults(name) : COPY.icons.noResults(name)}</p>
            {offer.why_not && <p className="mt-1">{offer.why_not}</p>}
          </div>
          {uploadButton}
        </div>
      ) : null;
    }
    const title = COPY.icons.generateTitle(offer.left_today, offer.per_day);
    if (!prominent) {
      return (
        <button
          type="button"
          onClick={() => void draw(false)}
          title={title}
          className="inline-flex items-center gap-1.5 text-2xs text-muted underline-offset-2 hover:text-fg hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        >
          <Sparkles className="h-3 w-3" aria-hidden />
          {COPY.icons.generateLink(name, cost)}
        </button>
      );
    }
    return (
      <div className="space-y-2 rounded-xl border border-border bg-bg-muted/40 p-2.5">
        <p className="text-xs text-fg">{found ? COPY.icons.weakResults(name) : COPY.icons.noResults(name)}</p>
        <div className="flex flex-wrap items-center gap-1.5">
          <button type="button" onClick={() => void draw(false)} title={title} className="btn btn-accent btn-sm">
            <Sparkles className="h-3.5 w-3.5" aria-hidden />
            {COPY.icons.generate(cost)}
          </button>
          {uploadButton}
        </div>
      </div>
    );
  }

  // --- the press and what came of it ---------------------------------------------
  return (
    <div className="space-y-2 rounded-xl border border-border bg-bg-muted/40 p-2.5" aria-live="polite">
      {phase.kind === "drawing" && (
        <p className="flex items-center gap-2 text-xs text-fg">
          <Spinner size="sm" tone="accent" /> {COPY.icons.drawing(name)}
        </p>
      )}
      {phase.kind === "failed" && (
        <div className="space-y-1.5">
          <p className="rounded-lg border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-2xs text-danger">
            {COPY.icons.drawFailed} {phase.message}
          </p>
          {phase.canRetry && (
            <button type="button" onClick={() => void draw(false)} className="btn btn-xs btn-secondary">
              {COPY.icons.generate(cost)}
            </button>
          )}
        </div>
      )}
      {phase.kind === "used" && <p className="text-xs text-fg">{COPY.icons.usedIt(phase.label)}</p>}
      {phase.kind === "discarded" && <p className="text-2xs text-muted">{COPY.icons.discarded}</p>}
      {phase.kind === "drawn" && (
        <Drawn
          got={phase.got}
          full={full}
          cost={cost}
          onUse={(glyph, label) => {
            onUse(glyph, label);
            setPhase({ kind: "used", label });
          }}
          onAgain={() => void draw(true)}
          onDiscard={() => void discard(phase.got)}
        />
      )}
    </div>
  );
}

function Drawn({
  got,
  full,
  cost,
  onUse,
  onAgain,
  onDiscard,
}: {
  got: HarnessIconGenerated;
  full: boolean;
  cost: number;
  onUse: (glyph: HarnessIconGlyph, label: string) => void;
  onAgain: () => void;
  onDiscard: () => void;
}) {
  const glyph: HarnessIconGlyph = { source: "custom", id: got.id };
  const said = got.was_hidden
    ? COPY.icons.drawnBack(got.name)
    : got.created
      ? COPY.icons.drawnNew(got.name)
      : COPY.icons.drawnBefore(got.name);
  const report = COPY.icons.drawReport(got.report);
  return (
    <div className="space-y-2">
      <p className="text-xs text-fg">{said}</p>
      <div className="flex flex-wrap items-end gap-3" title={COPY.icons.previewTitle}>
        <figure className="flex flex-col items-center gap-1">
          {got.preview.circle ? (
            <img src={got.preview.circle} alt="" className="h-14 w-auto" />
          ) : (
            <IconGlyph glyph={glyph} size={48} look={{ style: "circle", fill: "#7f2bf2", ink: "#ffffff" }} />
          )}
          <figcaption className="text-3xs text-muted">{COPY.icons.previewCircle}</figcaption>
        </figure>
        <figure className="flex min-w-0 flex-col items-center gap-1">
          {got.preview.chip ? (
            <img src={got.preview.chip} alt="" className="h-11 w-auto max-w-full" />
          ) : (
            <IconGlyph glyph={glyph} size={40} look={{ style: "chip", fill: "#7f2bf2", ink: "#ffffff" }} />
          )}
          <figcaption className="text-3xs text-muted">{COPY.icons.previewChip}</figcaption>
        </figure>
      </div>
      {report && <p className="text-2xs text-muted">{report}</p>}
      <div className="flex flex-wrap items-center gap-1.5">
        <button
          type="button"
          onClick={() => onUse(glyph, got.name)}
          disabled={full}
          title={full ? COPY.icons.capTitle(ADDED_MAX) : undefined}
          className="btn btn-primary btn-sm"
        >
          {COPY.icons.useIt}
        </button>
        <button type="button" onClick={onAgain} title={COPY.icons.tryAgainTitle} className="btn btn-xs btn-secondary">
          {COPY.icons.tryAgain(cost)}
        </button>
        <button type="button" onClick={onDiscard} title={COPY.icons.discardTitle} className="btn btn-xs btn-ghost">
          {COPY.icons.discard}
        </button>
      </div>
    </div>
  );
}
