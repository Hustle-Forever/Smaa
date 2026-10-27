import type { ReactNode } from "react";

interface Props {
  tabKey: string;
  children: ReactNode;
}

/**
 * Wraps page content; re-mounts (via `key`) on tab change so the inner
 * subtree replays the `pageEnter` keyframe (fade + 4px slide-up).
 */
export function PageTransition({ tabKey, children }: Props) {
  return (
    <div key={tabKey} className="animate-pageEnter">
      {children}
    </div>
  );
}
