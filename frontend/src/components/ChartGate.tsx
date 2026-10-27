import { useEffect, useRef, useState, type ReactNode } from "react";

/**
 * Holds the chart animations inside it (line draw-in, bars growing, fades)
 * until the chart is actually on screen, then lets them play once.
 *
 * Nothing re-renders: the animations are only paused by CSS
 * (.k-chart-gate:not(.is-in) * { animation-play-state: paused }), so a chart
 * far down the page draws itself in front of the reader instead of finishing
 * unseen while the page opened. Without IntersectionObserver it just plays.
 */
export function ChartGate({ children, className = "" }: { children: ReactNode; className?: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [inView, setInView] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") {
      setInView(true);
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setInView(true);
          io.disconnect();
        }
      },
      { threshold: 0.25 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return (
    <div ref={ref} className={`k-chart-gate ${inView ? "is-in" : ""} ${className}`}>
      {children}
    </div>
  );
}
