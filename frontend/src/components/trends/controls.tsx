/**
 * Re-export shim. The controls themselves now live in components/ui/controls.tsx.
 *
 * Nothing in this file is trends-specific -- Toolbar, SearchField, FilterChip,
 * OverflowMenu and the rest never referenced a trend, a region or a score. They
 * sat in components/trends/ only because that is where they were written, which
 * meant the rest of the app could not use them without importing from another
 * feature's folder. Two places already did exactly that (pages/TrendFinder.tsx
 * reaches in for Notice, pages/BrandWorkspace.tsx for TabBar), which is the
 * signal that they had outgrown the folder.
 *
 * The shim is not politeness. Six files here import from "./controls", two of
 * them (GeneralTrendsTab, ThemesTab) behind VIRAL_SOCIAL_TRENDS_ENABLED and
 * THEMES_TAB_ENABLED. featureFlags.ts promises that flipping one constant is
 * the entire revert, which only holds while those files stay byte-identical.
 * Rewriting their imports would have quietly broken that promise to save one
 * file. New code should import from components/ui/controls directly.
 */
export {
  Toolbar,
  ToolbarSpacer,
  SearchField,
  FilterToggle,
  FilterPanel,
  ChipRow,
  FilterChip,
  OverflowMenu,
  MenuItem,
  MenuNote,
  Notice,
  ResultCount,
} from "../ui/controls";
