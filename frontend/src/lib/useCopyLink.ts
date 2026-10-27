import { useCallback } from "react";

import { useToast } from "../components/ToastProvider";

/**
 * Shared "copy a link to the clipboard + toast" helper.
 *
 * Returns a stable callback that writes `url` to the clipboard and surfaces a
 * success (or error) toast. `successDetail` customises the success toast's
 * description so each call-site keeps its own wording. Centralises the pattern
 * that would otherwise be copy-pasted into every share affordance.
 *
 * Note: `navigator.clipboard?.` optional-chains the whole `.writeText(...).then`
 * chain, so a browser without the Clipboard API is a silent no-op (never throws).
 */
export function useCopyLink() {
  const toast = useToast();
  return useCallback(
    (url: string, successDetail?: string) => {
      navigator.clipboard?.writeText(url).then(
        () => toast.success("Link copied", successDetail),
        () => toast.error("Couldn’t copy link"),
      );
    },
    [toast],
  );
}
