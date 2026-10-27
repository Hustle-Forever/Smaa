/**
 * The row of scene tiles in the Human Element Video (TalkingHeadV2Creator).
 *
 * Up to 3 tiles wrap and centre (the 3-up look of a single-scene run: intro +
 * main + outro); 4 or more scroll sideways with snap, so they stay on one row
 * instead of breaking awkwardly across two.
 *
 * Tiles cap their own width (max-w-[220px]); this only decides how they flow.
 */
import { Children, type ReactNode } from "react";

export function SceneRow({ count, children }: { count: number; children: ReactNode }) {
  if (count <= 3) {
    return (
      <div className="flex flex-wrap gap-4 justify-center items-stretch">
        {children}
      </div>
    );
  }
  return (
    <div className="flex gap-4 overflow-x-auto snap-x snap-mandatory pb-2 px-1 -mx-1">
      <SnapList>{children}</SnapList>
    </div>
  );
}

/** Wraps each child in a snap-start container so horizontal scrolling lands
 *  cleanly on tile boundaries. */
function SnapList({ children }: { children: ReactNode }) {
  const kids = Children.toArray(children);
  return (
    <>
      {kids.map((k, i) => (
        <div key={i} className="snap-start shrink-0">
          {k}
        </div>
      ))}
    </>
  );
}
