// Rebrand studio — the icon catalogue (the 42 picks, the
// scheduler's numbers), asked for once the server says it draws icons. Its
// paths go straight into the art cache, so the list, the lane and the moving
// preview draw the picks without asking again.

import { useCallback, useEffect, useState } from "react";

import { rebrandErrorDetail } from "../../../lib/rebrandApi";
import { harnessApi, type HarnessIconCatalogue } from "../../../lib/rebrandHarnessApi";
import { rememberPaths } from "./iconArt";

export function useIconCatalogue(enabled: boolean) {
  const [catalogue, setCatalogue] = useState<HarnessIconCatalogue | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!enabled || catalogue) return;
    let cancelled = false;
    setError(null);
    harnessApi
      .iconCatalogue()
      .then((c) => {
        if (cancelled) return;
        rememberPaths(c.picks?.flatMap((g) => g.items));
        setCatalogue(c);
      })
      .catch((e) => !cancelled && setError(rebrandErrorDetail(e)));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, nonce]);

  const retry = useCallback(() => setNonce((n) => n + 1), []);
  return { catalogue, error, retry };
}
