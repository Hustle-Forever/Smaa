import { useCallback, useEffect, useState } from "react";

import { PostDetailDrawer } from "../components/instagram/PostDetail";
import { PostsGrid } from "../components/instagram/PostsGrid";
import { SyncPanel } from "../components/instagram/SyncPanel";
import { api } from "../lib/api";
import type { AgentId, InstagramHealth } from "../lib/types";

interface Props {
  onStartRun: (agent: AgentId, runId: number) => void;
  /** When mounted inside the Brand workspace, the workspace already renders a
   *  title bar — hide this page's own header to avoid a duplicate. */
  embedded?: boolean;
}

export function InstagramPage({ onStartRun, embedded = false }: Props) {
  const [health, setHealth] = useState<InstagramHealth | null>(null);
  const [loadingHealth, setLoadingHealth] = useState(true);
  const [openPostId, setOpenPostId] = useState<number | null>(null);
  // Bump this when a sync finishes (RunPanel doesn't tell us directly, so we
  // refresh on a timer when the user kicks one off, and on mount).
  const [gridKey, setGridKey] = useState(0);

  const refreshHealth = useCallback(() => {
    setLoadingHealth(true);
    api
      .instagramIngest.health()
      .then(setHealth)
      .catch(() => setHealth(null))
      .finally(() => {
        setLoadingHealth(false);
        setGridKey((k) => k + 1);
      });
  }, []);

  useEffect(() => {
    refreshHealth();
  }, [refreshHealth]);

  return (
    <div className="space-y-10">
      {!embedded && (
        <header>
          <p className="eyebrow mb-1.5">Agent 5</p>
          <h1 className="h1">Instagram ingest</h1>
          <p className="lead mt-2 max-w-2xl">
            Reads @42abudhabi's public feed via Meta's Business Discovery API,
            downloads every post + reel + carousel image, runs the same
            understanding pipeline used for the website corpus, and embeds
            captions into the producer's retrieval space so future scripts can
            learn from what's already worked.
          </p>
        </header>
      )}

      <SyncPanel
        onStartRun={onStartRun}
        health={health}
        loadingHealth={loadingHealth}
        onRefreshHealth={refreshHealth}
      />

      <PostsGrid refreshKey={gridKey} onOpen={(id) => setOpenPostId(id)} />

      {openPostId != null && (
        <PostDetailDrawer postId={openPostId} onClose={() => setOpenPostId(null)} />
      )}
    </div>
  );
}
