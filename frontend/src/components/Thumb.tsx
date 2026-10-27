/**
 * One image tile, with the loading states a grid actually needs.
 *
 * Before this existed, none of the 51 `<img>` sites in the app had an `onLoad`:
 * a tile was a solid `bg-black`/`bg-bg-muted` square until the bytes landed, so
 * a slow grid read as *frozen* rather than *loading*. This adds a shimmer
 * underneath, a fade-in on load, and — importantly — a two-step error fallback.
 *
 * The `fallbackSrc` chain is load-bearing, not cosmetic. Grid thumbnails are
 * Supabase image transformations, which are a paid Storage feature; if the plan
 * lapses or a quota trips, every thumbnail 400s at once. Falling back to the
 * full-resolution proxy makes that failure mode "slow", never "blank".
 *
 * `eager` and `priority` are PROPS, not hardcoded: a detail hero must stay eager,
 * while an off-screen grid tile must not be.
 */
import { useEffect, useRef, useState } from "react";

type Props = {
  src: string | null | undefined;
  alt?: string;
  /** Tried once if `src` fails — normally the full-resolution proxy URL. */
  fallbackSrc?: string | null;
  className?: string;
  /** Load immediately instead of lazily. Use for detail views, never for grids. */
  eager?: boolean;
  /** Hint the browser this image matters (above-the-fold hero). */
  priority?: boolean;
  /** Rendered when there is no src, or when both src and fallback fail. */
  empty?: React.ReactNode;
  objectFit?: "cover" | "contain";
};

export function Thumb({
  src,
  alt = "",
  fallbackSrc = null,
  className = "",
  eager = false,
  priority = false,
  empty = null,
  objectFit = "cover",
}: Props) {
  const [loaded, setLoaded] = useState(false);
  const [stage, setStage] = useState<"primary" | "fallback" | "dead">("primary");
  const imgRef = useRef<HTMLImageElement>(null);

  // A changed src is a different image: reset, or the new one inherits the old
  // one's loaded/failed state and either never fades in or stays hidden.
  useEffect(() => {
    setLoaded(false);
    setStage("primary");
  }, [src, fallbackSrc]);

  // A picture the browser already has (cached, or decoded before React attached
  // onLoad) can be complete before the reset above runs, and then no load event
  // ever comes: it would sit at opacity 0 for good. Runs after every render and
  // costs one property read.
  useEffect(() => {
    const img = imgRef.current;
    if (img && img.complete && img.naturalWidth > 0) setLoaded(true);
  });

  const current =
    stage === "primary" ? src : stage === "fallback" ? fallbackSrc : null;

  if (!current) {
    return <>{empty}</>;
  }

  return (
    <div className={`relative overflow-hidden ${className}`}>
      {/* Shimmer sits UNDER the image and is removed once it paints, so there is
          no flash of empty space and no layout shift. */}
      {!loaded && (
        <div className="absolute inset-0 bg-bg-muted animate-pulse" aria-hidden="true" />
      )}
      <img
        ref={imgRef}
        src={current}
        alt={alt}
        loading={eager ? "eager" : "lazy"}
        decoding="async"
        // fetchPriority is still not in every TS DOM lib; harmless if ignored.
        {...({ fetchpriority: priority ? "high" : undefined } as any)}
        onLoad={() => setLoaded(true)}
        onError={() => {
          setStage((s) =>
            s === "primary" && fallbackSrc ? "fallback" : "dead",
          );
        }}
        className={[
          "w-full h-full",
          objectFit === "cover" ? "object-cover" : "object-contain",
          "transition-opacity duration-200",
          loaded ? "opacity-100" : "opacity-0",
        ].join(" ")}
      />
    </div>
  );
}
