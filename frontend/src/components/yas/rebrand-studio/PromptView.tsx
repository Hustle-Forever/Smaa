// Rebrand studio — what Omni is told, read-only, pushed into the rail from
// Style > Advanced: the opening (the brief writes it at Generate) and the
// words for each part. A debugging window, so it lives out of the way.

import { useEffect, useRef } from "react";
import { ArrowLeft } from "lucide-react";

import { isShown } from "../../../lib/isShown";
import { COPY } from "./studioCopy";
import type { ShownPreview } from "./useStudioPreview";

export function PromptView({
  shown,
  current,
  onBack,
}: {
  shown: ShownPreview | null;
  current: boolean;
  onBack: () => void;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const onBackRef = useRef(onBack);
  onBackRef.current = onBack;

  useEffect(() => {
    headingRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || !isShown(headingRef.current)) return;
      e.stopPropagation();
      onBackRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const p = shown?.preview ?? null;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-1.5 border-b border-border px-2 py-1.5">
        <button
          type="button"
          onClick={onBack}
          aria-label={COPY.back}
          title={COPY.back}
          className="h-8 w-8 rounded-lg flex items-center justify-center text-muted hover:text-fg hover:bg-bg-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden />
        </button>
        <h3 ref={headingRef} tabIndex={-1} className="text-xs font-semibold text-fg focus:outline-none">
          {COPY.promptTitle}
        </h3>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-3 space-y-3 text-2xs leading-relaxed">
        {!p ? (
          <p className="text-muted">{COPY.promptEmpty}</p>
        ) : (
          <>
            {!current && <p className="rounded-lg bg-bg-muted px-2.5 py-1.5 text-muted">{COPY.promptStale}</p>}
            {p.opening && (
              <section className="space-y-1">
                <h4 className="eyebrow">{COPY.promptOpening}</h4>
                <p className="italic text-muted">{p.opening}</p>
              </section>
            )}
            {p.chunks.map((c) => (
              <section key={c.index} className="space-y-1">
                {p.chunks.length > 1 && <h4 className="eyebrow">{COPY.promptPart(c.index + 1)}</h4>}
                <p className="whitespace-pre-wrap rounded-lg bg-bg-muted p-2.5 text-fg/85">{c.prompt || COPY.promptNone}</p>
              </section>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
