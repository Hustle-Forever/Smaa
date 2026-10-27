/**
 * Video Editor — brand fonts (Assets Library, asset_kind=font) for the Text
 * picker. Backed by a tiny zustand store so an in-editor font upload
 * (`refreshBrandFonts()`) immediately reflects in the inspector's font select.
 */
import { useEffect } from "react";
import { create } from "zustand";

import { api } from "../../../lib/api";
import type { VideoEditorFont } from "../../../lib/types";

interface BrandFontsState {
  items: VideoEditorFont[];
  loaded: boolean;
  loading: boolean;
  fetch: (force?: boolean) => Promise<void>;
}

const useBrandFontsStore = create<BrandFontsState>((set, get) => ({
  items: [],
  loaded: false,
  loading: false,
  fetch: async (force = false) => {
    const s = get();
    if (s.loading || (s.loaded && !force)) return;
    set({ loading: true });
    try {
      const r = await api.yasProducer.videoEditor.listFonts();
      set({ items: r.items, loaded: true, loading: false });
    } catch {
      set({ loading: false });
    }
  },
}));

export function useBrandFonts(): VideoEditorFont[] {
  const items = useBrandFontsStore((s) => s.items);
  const fetch = useBrandFontsStore((s) => s.fetch);
  useEffect(() => {
    void fetch();
  }, [fetch]);
  return items;
}

/** Force-refetch the brand fonts (after an in-editor font upload). */
export function refreshBrandFonts(): Promise<void> {
  return useBrandFontsStore.getState().fetch(true);
}
