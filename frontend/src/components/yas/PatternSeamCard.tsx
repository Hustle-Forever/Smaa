// The intro → main seam, designed from the 42 pattern kit.
//
// Different in kind from the two arms beside it. No video model is called: the
// card and the shot stay exactly as they were rendered, and the brand graphics
// are composited over the join. So it takes seconds instead of minutes, costs
// nothing, and cannot drift a face or slip the lip-sync.
//
// What the panel shows is the reasoning, not just the result — which frames it
// chose and why, where it took the colour from, and what each layer does —
// because that is what tells you whether a disappointing cut is a bad design
// or a bad input.

import { useState } from "react";

import {
  rebrandApi,
  type SeamMoment,
  type SeamRecipe,
  type SeamResult,
} from "../../lib/rebrandApi";
import { useToast } from "../ToastProvider";
import { Spinner } from "../ui/Spinner";

function Stars({
  value,
  onChange,
  disabled,
}: {
  value: number | null;
  onChange: (v: number) => void;
  disabled?: boolean;
}) {
  return (
    <div className="inline-flex gap-0.5" role="radiogroup" aria-label="Rating">
      {[1, 2, 3, 4, 5].map((n) => (
        <button
          key={n}
          type="button"
          disabled={disabled}
          onClick={() => onChange(n)}
          aria-label={`${n} star${n > 1 ? "s" : ""}`}
          className={`text-lg leading-none transition-colors disabled:opacity-50 ${
            value != null && n <= value
              ? "text-warning"
              : "text-border-strong hover:text-warning/60"
          }`}
        >
          ★
        </button>
      ))}
    </div>
  );
}

function Chip({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-md border border-border px-1.5 py-0.5 text-2xs">
      <span
        className="inline-block h-3 w-3 rounded-sm border border-border-strong"
        style={{ backgroundColor: color }}
      />
      <span className="text-muted">{label}</span>
      <code className="text-fg">{color}</code>
    </span>
  );
}

/** What the reader needs to trust the frame the design was built on. */
function MomentLine({ m, label }: { m: SeamMoment; label: string }) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
      <span className="text-muted w-20 shrink-0">{label}</span>
      <code className="text-fg">{m.at_sec.toFixed(2)}s</code>
      {m.fell_back ? (
        <span className="rounded bg-warning/15 border border-warning/30 px-1.5 py-0.5 text-2xs text-amber-700">
          no frame with content — this clip is dark throughout
        </span>
      ) : (
        <span className="text-2xs text-muted">
          brightness {Math.round(m.measured.mean_lum)}/255 · accent
        </span>
      )}
      {!m.fell_back && (
        <span
          className="inline-block h-3 w-3 rounded-sm border border-border-strong"
          style={{ backgroundColor: m.measured.accent }}
          title={m.measured.accent}
        />
      )}
      {typeof m.described?.summary === "string" && (
        <span className="text-2xs text-muted italic basis-full">
          “{m.described.summary as string}”
        </span>
      )}
    </div>
  );
}

function RecipeLayers({ recipe }: { recipe: SeamRecipe }) {
  return (
    <ul className="space-y-1">
      {recipe.layers.map((l, i) => (
        <li key={`${l.pattern}-${i}`} className="flex flex-wrap items-center gap-1.5 text-2xs">
          <span
            className="inline-block h-2.5 w-2.5 rounded-sm"
            style={{ backgroundColor: l.color_to }}
          />
          <code className="text-fg">{l.pattern}</code>
          <span className="text-muted">
            {l.move}
            {l.direction !== "none" ? ` ${l.direction}` : ""} ·{" "}
            {(l.start * recipe.duration_sec).toFixed(2)}–
            {(l.end * recipe.duration_sec).toFixed(2)}s
          </span>
          {l.reveal && (
            <span className="rounded bg-accent/15 border border-accent/30 px-1 text-2xs text-accent">
              carries the cut
            </span>
          )}
          {l.residue > 0.02 && (
            <span className="rounded bg-bg-subtle border border-border px-1 text-2xs text-muted">
              stays at {Math.round(l.residue * 100)}%
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

export function PatternSeamCard({ runId, aspect }: { runId: number; aspect: string }) {
  const toast = useToast();
  const [result, setResult] = useState<SeamResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState("");
  const [stars, setStars] = useState<number | null>(null);
  const [showReel, setShowReel] = useState(true);
  const [detail, setDetail] = useState(false);
  const [side, setSide] = useState<"auto" | "left" | "right">("right");
  const [holdSec, setHoldSec] = useState(3.5);

  const vertical = aspect !== "16:9";

  async function design() {
    setBusy(true);
    setError(null);
    try {
      const r = await rebrandApi.designSeam(runId, {
        notes, cover: "shot", side, hold_sec: holdSec,
      });
      setResult(r);
      setStars(null);
      toast.success(`Seam rendered — “${r.recipe.style}”`);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      toast.error(`Could not design the seam: ${msg}`);
    } finally {
      setBusy(false);
    }
  }

  async function rate(v: number) {
    if (!result?.recipe_id) return;
    setStars(v);
    try {
      await rebrandApi.rateSeam(result.recipe_id, { stars: v, notes });
      toast.success("Saved — the next seam will know");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  }

  // Cache-bust the file routes: the paths are stable per run, so a second
  // design would otherwise replay the first render from the browser cache.
  const stamp = result?.recipe_id ?? 0;
  const src = (part: "clip" | "reel") => `${rebrandApi.seamFile(runId, part)}?v=${stamp}`;

  return (
    <section className="rounded-xl border border-border bg-bg-subtle/50 p-3 space-y-3 text-xs">
      <header className="flex flex-wrap items-start gap-2">
        <div className="min-w-0">
          <h4 className="font-semibold text-sm">Intro → talking head</h4>
          <p className="text-2xs text-muted mt-0.5 leading-relaxed">
            One pattern carries the cut, then a narrow branded panel holds down one
            edge and fades away. The room you shot in is never drawn on — nothing is
            re-generated, so the background, the framing and the lip-sync all survive.
          </p>
        </div>
        <button
          type="button"
          onClick={design}
          disabled={busy}
          className="btn btn-accent text-xs disabled:opacity-50 ml-auto shrink-0"
        >
          {busy ? (
            <span className="inline-flex items-center gap-1.5">
              <Spinner size="sm" /> Designing…
            </span>
          ) : result ? (
            "Design again"
          ) : (
            "Design the transition"
          )}
        </button>
      </header>

      <div className="grid sm:grid-cols-2 gap-3">
        <div className="space-y-1">
          <div className="text-muted">Branded edge</div>
          <div className="flex gap-1">
            {(["left", "auto", "right"] as const).map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => setSide(v)}
                className={`btn text-2xs py-1 flex-1 ${side === v ? "btn-accent" : "btn-ghost"}`}
                title={v === "auto" ? "Put it wherever the shot is emptier" : undefined}
              >
                {v === "auto" ? "Auto" : v === "left" ? "Left" : "Right"}
              </button>
            ))}
          </div>
        </div>
        <div className="space-y-1">
          <div className="text-muted">
            Panel stays · {holdSec.toFixed(1)}s, then fades
          </div>
          <input
            type="range"
            min={1}
            max={10}
            step={0.5}
            value={holdSec}
            onChange={(e) => setHoldSec(Number(e.target.value))}
            className="w-full accent-accent"
          />
        </div>
      </div>

      <textarea
        value={notes}
        onChange={(e) => setNotes(e.target.value.slice(0, 600))}
        rows={1}
        placeholder="Anything specific? e.g. “keep it calmer”, “put the strip on the right”"
        className="w-full rounded-lg border border-border bg-bg px-2.5 py-2 text-xs focus:outline-none focus:border-accent"
      />

      {error && (
        <p className="text-xs text-danger bg-danger/10 border border-danger/30 rounded-lg px-3 py-2">
          {error}
        </p>
      )}

      {result && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold text-fg">“{result.recipe.style}”</span>
            <span className="rounded border border-border px-1.5 py-0.5 text-2xs text-muted">
              {result.recipe.planner === "gemini" ? "planned by Gemini" : "composed by rule"}
            </span>
            {result.critique?.score != null && (
              <span className="rounded border border-border px-1.5 py-0.5 text-2xs text-muted">
                self-review {result.critique.score}/10
              </span>
            )}
            {result.critique?.covers_face && (
              <span className="rounded bg-danger/10 border border-danger/30 px-1.5 py-0.5 text-2xs text-danger">
                covers the face
              </span>
            )}
          </div>

          {result.recipe.rationale && (
            <p className="text-2xs text-muted italic leading-relaxed">
              {result.recipe.rationale}
            </p>
          )}

          <div className="flex gap-1.5">
            <button
              type="button"
              onClick={() => setShowReel(true)}
              className={`btn text-2xs py-1 ${showReel ? "btn-accent" : "btn-ghost"}`}
            >
              In context
            </button>
            <button
              type="button"
              onClick={() => setShowReel(false)}
              className={`btn text-2xs py-1 ${!showReel ? "btn-accent" : "btn-ghost"}`}
            >
              The transition alone
            </button>
          </div>

          <video
            key={`${showReel}-${stamp}`}
            src={src(showReel && result.reel ? "reel" : "clip")}
            controls
            playsInline
            className={`w-full rounded-lg bg-black ${vertical ? "max-h-[70vh]" : ""}`}
            style={vertical ? { aspectRatio: "9 / 16" } : { aspectRatio: "16 / 9" }}
          />

          <div className="flex flex-wrap items-center gap-2">
            <span className="text-muted">Colour from the card, resolving to brand:</span>
            <Chip color={result.recipe.palette.from} label="from" />
            <Chip color={result.recipe.palette.to} label="to" />
          </div>

          {(() => {
            const panel = result.recipe.layers.find(
              (l) => l.residue > 0.02 && (l.residue_end ?? 1) < 1,
            );
            const region = result.recipe.brand_region;
            if (!panel || !region) return null;
            return (
              <p className="text-2xs text-muted">
                Branded strip on the <strong className="text-fg">{region.side}</strong>{" "}
                edge ({Math.round(region.w * 100)}% of the frame) — gone by{" "}
                <strong className="text-fg">
                  {((panel.residue_end ?? 1) * result.recipe.duration_sec).toFixed(1)}s
                </strong>
                . Everything outside it is the original footage.
              </p>
            );
          })()}

          {result.timing.dead_tail_trimmed_sec > 0 && (
            <p className="text-2xs text-muted">
              Trimmed {result.timing.dead_tail_trimmed_sec.toFixed(2)}s of dead black
              off the end of the card — the cut now leaves from the last frame that
              had something on it.
            </p>
          )}

          <div className="flex flex-wrap items-center gap-2 border-t border-border pt-2">
            <span className="text-muted">Did this land?</span>
            <Stars value={stars} onChange={rate} disabled={!result.recipe_id} />
            {result.precedents.length > 0 && (
              <span className="text-2xs text-muted ml-auto">
                learned from {result.precedents.length} past seam
                {result.precedents.length > 1 ? "s" : ""}
              </span>
            )}
          </div>

          <button
            type="button"
            onClick={() => setDetail((d) => !d)}
            className="text-2xs text-muted underline underline-offset-2 hover:text-fg"
          >
            {detail ? "Hide" : "Show"} what it decided
          </button>

          {detail && (
            <div className="space-y-2 rounded-lg border border-border bg-bg p-2.5">
              <div className="space-y-1">
                <MomentLine m={result.moments.intro_out} label="Card out" />
                <MomentLine m={result.moments.main_in} label="Shot in" />
              </div>
              <div className="border-t border-border pt-2">
                <div className="text-muted mb-1">
                  Layers · {result.recipe.duration_sec.toFixed(2)}s, cut at{" "}
                  {result.recipe.cut_at_sec.toFixed(2)}s
                </div>
                <RecipeLayers recipe={result.recipe} />
              </div>
              {(result.critique?.problems?.length ?? 0) > 0 && (
                <div className="border-t border-border pt-2">
                  <div className="text-muted mb-1">Its own complaints</div>
                  <ul className="list-disc list-inside space-y-0.5 text-2xs text-muted">
                    {result.critique.problems!.map((p, i) => (
                      <li key={i}>{p}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
