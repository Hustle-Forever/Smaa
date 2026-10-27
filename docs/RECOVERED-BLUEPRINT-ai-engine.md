I now have a thorough understanding of the entire codebase. Let me compose the complete architecture blueprint.

---

# Production Video Editor Extension: Architecture Blueprint

## Patterns and Conventions Found

**Schema duality (the critical constraint):** `schema.ts` is deliberately framework-free and is imported by BOTH the Remotion composition service AND the frontend via the Vite alias `video-edit-schema`. Every type added to `schema.ts` must be mirrored identically in `frontend/src/lib/types.ts`. Drift between the two means Player preview diverges from export. The Python backend `yas_video_editor.py` does not import the TS schema — it constructs the same shapes as plain dicts and passes them through as JSON.

**ClipSource resolution pattern:** `yas_video_editor.py` has two resolution paths that must stay in sync:
- `_resolve_clip_sources` → attaches `sourceUrl` pointing at `/api/yas-producer/video-editor/sources/{kind}/{id}/video` for the browser Player  
- `build_render_props` / `resolve_source_local` → downloads to `VE_ROOT` on disk, then produces `http://127.0.0.1:3001/data/...` URL for the headless Remotion renderer

Any new source kind (`asset`, `audio-upload`) must implement both paths identically.

**TransitionSeries contract:** `buildRuns` at `VideoEdit.tsx:87` groups clips by adjacency. Non-video clips on video tracks will enter this same path via the existing `VideoTrackContent` — so new layer types (text/shape/image) must slot into that existing grouping, with `VideoClipLayer` replaced by a dispatcher.

**Store mutation pattern:** `store.ts` — all clip mutations go through `mutate(fn)` or direct `set(...)` + `scheduleSave()`. New clip kinds need new store actions for creation that mirror `addClipFromSource`. The `VideoEditClip` type in `types.ts` maps to `Clip` in `schema.ts` — these two interfaces are the central schema synchronization point.

**Audio track branching:** `AudioClipLayer` at `VideoEdit.tsx:140` uses `<OffthreadVideo>` (comment: "for playing a video's audio"). True audio files (`.mp3`, `.wav`, `.aac`) need `<Audio>` from Remotion instead. The branch must live inside `AudioClipLayer` keyed on a flag in the clip.

**Asset file resolution:** `producer_assets` rows have `storage_bucket + storage_path` (Supabase bucket) OR `local_path` (disk fallback). The existing `asset_storage.url_for()` in `backend/app/services/asset_storage.py` produces signed URLs for the bucket path. For export, signed URLs (which are HTTPS redirects) are not fetchable by headless Chrome — files must be downloaded to `VE_ROOT/sources/` and served via `REMOTION_DATA_PREFIX`, exactly like `ensure_animation_local` does at `yas_video_editor.py:662`.

**Poller check for resolved clips:** `yas_video_edit_composition_poller.py:79` counts `clip.get("sourceUrl")` to decide whether the export is possible. This check must be extended: text/shape clips have no source, so they must not be counted as unresolved. Image clips with a resolved asset path must count as resolved.

**Font storage on `producer_assets`:** The `describe_font` path in `producer_assets.py` stores the font file under `storage_bucket + storage_path` with `asset_kind='font'`. The `/api/agents/producer/assets/{id}/file` endpoint at line 1456 already serves the raw font bytes with `Cache-Control: private, max-age=86400`. This is the URL brand fonts will load from in the Player. For export, the font bytes must be in `VE_ROOT/fonts/{id}.ttf` on disk.

**`@remotion/transitions` version lock:** Both `services/remotion/package.json` and `frontend/package.json` pin at `4.0.461`. All new Remotion packages (`@remotion/fonts`, `@remotion/google-fonts/*`) must be added at this EXACT version in both locations.

---

## Architecture Decision

**Chosen approach: A single `ClipLayer` dispatcher in `VideoEdit.tsx` that branches on `clip.kind`.**

Every clip, regardless of type, lives on the same `Track`, participates in the same `buildRuns` run-grouping, and receives the same keyframed transform + filter CSS wrapper. The `VideoTrackContent` component becomes agnostic — it calls `<ClipLayer clip={c} ...>` instead of `<VideoClipLayer clip={c} ...>` and the dispatcher handles branching. This:
- Preserves all transition logic unchanged (`TransitionSeries` wraps `ClipLayer` regardless of content type)
- Makes keyframes, transforms, and filters uniform across all visual layer types
- Is backward-compatible: `clip.kind` defaults to `'video'` if absent, so all existing saved documents render correctly

**Font loading:** A dedicated `fonts.ts` module is imported by `VideoEdit.tsx` at module-load time (top-level, before any component executes). It calls `loadFont()` for each curated Google Font. Brand fonts from `producer_assets` are loaded on demand via a registry pattern using `@remotion/fonts` + `delayRender/continueRender`. The module is imported by BOTH the Remotion composition (server) AND the frontend composition (via the alias), so fonts load identically in both contexts.

**Audio-music clips:** A boolean flag `clip.audioFile: true` on clips whose `track.kind === 'audio'` distinguishes true audio files (use Remotion's `<Audio>`) from detached-video audio (keep `<OffthreadVideo>`). This flag is set at clip creation, never mutated, and is passed through the export unchanged.

---

## Component Design

### New File: `services/remotion/src/compositions/fonts.ts`

**Responsibilities:** Module-level font loading (called once at import). Registers curated Google Fonts + provides the brand-font registry.

**Curated fonts (8 total):**
- `Inter`, `Poppins`, `Nunito`, `Raleway` (sans-serif — modern/clean)
- `Playfair Display`, `Merriweather` (serif — editorial)
- `Cairo`, `Tajawal` (Arabic-capable, essential for 42 Abu Dhabi bilingual content)

**Interfaces:**
```
loadCuratedFonts(): void  — called at module top level; each calls @remotion/google-fonts
type BrandFontEntry = { family: string; url: string; handle: Handle }
brandFontRegistry: Map<string, BrandFontEntry>
loadBrandFont(family: string, url: string): void  — delayRender + loadFont + continueRender
```

**Dependencies:** `@remotion/fonts` (for `loadFont`, `delayRender`, `continueRender`), individual `@remotion/google-fonts/<Name>` packages for each curated font.

**Import chain:** `VideoEdit.tsx` imports `"./fonts"` at the top. The Vite alias for `video-edit-composition` resolves to `VideoEdit.tsx`, which carries the fonts.ts import into the frontend Player context automatically.

### Modified File: `services/remotion/src/compositions/schema.ts`

**New types to add:**

```typescript
export type ClipKind = 'video' | 'audio' | 'text' | 'shape' | 'image';

export type ShapeVariant = 'rect' | 'ellipse' | 'line' | 'triangle' | 'star';

export interface ClipFilters {
  brightness?: number;   // 0..2, default 1
  contrast?: number;     // 0..2, default 1
  saturate?: number;     // 0..2, default 1
  blur?: number;         // 0..40 (px), default 0
}

export interface ClipText {
  content: string;
  fontFamily: string;          // CSS font-family name (curated or brand)
  fontUrl?: string | null;     // non-null = brand font, must be resolved at export
  fontSize: number;            // px relative to composition height, e.g. 80
  color: string;               // CSS color
  fontWeight?: number;         // 400, 600, 700, 900
  textAlign?: 'left' | 'center' | 'right';
  lineHeight?: number;         // em units, default 1.2
  background?: string | null;  // box background color, null = none
  padding?: number;            // px, default 0
  stroke?: string | null;      // CSS color for text stroke, null = none
  strokeWidth?: number;        // px
  shadow?: string | null;      // CSS text-shadow value, null = none
}

export interface ClipShape {
  variant: ShapeVariant;
  fill: string;                // CSS color
  stroke?: string | null;
  strokeWidth?: number;
  cornerRadius?: number;       // rect only, px
  width: number;               // px (in composition space)
  height: number;              // px
}
```

**Modified `ClipSource`:** Add new kind:
```typescript
export interface ClipSource {
  kind: 'animation' | 'upload' | 'composition' | 'asset';
  animation_id?: number | null;
  upload_id?: number | null;
  asset_id?: number | null;     // NEW — producer_assets.id
  url?: string | null;
  naturalDurationInFrames?: number | null;
  audioFile?: boolean;          // NEW — true = use <Audio>, false = <OffthreadVideo>
}
```

**Modified `Clip`:** Add optional fields (all optional = backward-compatible JSONB):
```typescript
export interface Clip {
  // ... all existing fields unchanged ...
  kind?: ClipKind | null;           // omit or null = 'video' (back-compat)
  text?: ClipText | null;
  shape?: ClipShape | null;
  filters?: ClipFilters | null;
}
```

**Default construction for new clip types:**
- Text clip: `kind='text'`, `durationInFrames=90` (3s), `trimBefore=0`, `trimAfter=90`, `from=<track end>`, `source={kind:'animation'}` (empty source, no URL needed), `text={content:'', fontFamily:'Inter', fontSize:80, color:'#ffffff', textAlign:'center'}`
- Shape clip: same duration defaults, `source={kind:'animation'}`, `shape={variant:'rect', fill:'#ffffff', width:400, height:400}`
- Image/sticker clip: `kind='image'`, `source={kind:'asset', asset_id:N}`, `durationInFrames=90`, `fit='contain'`

### Modified File: `services/remotion/src/compositions/VideoEdit.tsx`

**New component: `ClipLayer` dispatcher**

Replaces all direct references to `VideoClipLayer` within `VideoTrackContent`. The filter string is built here and applied to the outermost `<AbsoluteFill>` so it wraps all clip types uniformly:

```
function buildFilterStyle(f: ClipFilters | null | undefined): string | undefined
  → CSS filter string, or undefined if all defaults

const ClipLayer: React.FC<{ clip: Clip; trackMuted?: boolean; width: number; height: number }>
  → reads clip.kind (default 'video')
  → reads clip.keyframes, clip.filters
  → builds opacity/scale/x/y/rotation transforms + filter CSS
  → branches:
      'video' (default) → <VideoClipLayer clip={clip} trackMuted={trackMuted} />
      'text'            → <TextClipLayer clip={clip} />
      'shape'           → <ShapeClipLayer clip={clip} width={width} height={height} />
      'image'           → <ImageClipLayer clip={clip} />
```

The outer `<AbsoluteFill style={{opacity, transform, filter}}>` wraps all branches. The inner layers (VideoClipLayer, etc.) must NOT apply opacity/transform themselves — they are stripped of those responsibilities and only render their medium.

**VideoTrackContent:** Replace `<VideoClipLayer>` calls with `<ClipLayer>` and pass `width`/`height` through.

**New component: `TextClipLayer`**

```
const TextClipLayer: React.FC<{ clip: Clip }>
  → reads clip.text
  → renders <AbsoluteFill style={{display:'flex', alignItems:'center', justifyContent: textAlign-mapped}}>
      <div style={{fontFamily, fontSize, color, fontWeight, lineHeight, background, padding, WebkitTextStroke, textShadow}}>
        {content}
      </div>
```

No `useCurrentFrame` needed here (text is static); only the outer `ClipLayer` uses frame for keyframes. If future animated text is needed, `clip.keyframes` already supports arbitrary numeric properties.

**New component: `ShapeClipLayer`**

```
const ShapeClipLayer: React.FC<{ clip: Clip; width: number; height: number }>
  → reads clip.shape
  → 'rect': <div style={{width, height, backgroundColor:fill, borderRadius:cornerRadius, border:stroke}}/>
  → 'ellipse': same div with borderRadius:50%
  → 'line': thin rotated div
  → 'triangle': border-trick CSS triangle
  → 'star': inline SVG <polygon>
  → centered in an AbsoluteFill
```

**New component: `ImageClipLayer`**

```
const ImageClipLayer: React.FC<{ clip: Clip }>
  → reads clip.sourceUrl (resolved by backend at fetch/export)
  → renders <Img src={clip.sourceUrl} style={{width:'100%', height:'100%', objectFit: clip.fit||'contain'}}/>
  → <Img> from remotion is render-safe (blocks frame until loaded)
  → if no sourceUrl: null
```

**Modified `AudioClipLayer`:** Branch on `clip.source?.audioFile`:
```
if audioFile → <Audio src={url} startFrom={trimBefore} endAt={trimAfter} volume={volume} />
else         → existing <OffthreadVideo> pattern (unchanged)
```

Note: `<Audio>` from Remotion uses `startFrom`/`endAt` (frame numbers) not `trimBefore`/`trimAfter`. These are semantically the same values — confirm the naming matches Remotion's `<Audio>` API at the implementation phase.

**Poller check fix:** The `_kick_render` in the poller at `yas_video_edit_composition_poller.py:79` counts `sum(1 for c in props.get("clips", []) if c.get("sourceUrl"))`. This must become: count only clips where `c.get("kind", "video") in ('video', 'audio', 'image')` (those need a URL) and those clips have `c.get("sourceUrl")`. Text and shape clips are always resolvable.

---

## Font Loading Architecture

### `services/remotion/src/compositions/fonts.ts`

This module is framework-free except for the Remotion font APIs. It is imported at the top of `VideoEdit.tsx` for its side effects.

**Package installation (both repos, same version 4.0.461):**
```
# services/remotion
npm install @remotion/fonts@4.0.461 @remotion/google-fonts@4.0.461

# frontend
npm install @remotion/fonts@4.0.461 @remotion/google-fonts@4.0.461
```

**Curated Google Fonts loading (module-level side effects):**
```typescript
import { loadFont as loadInter } from "@remotion/google-fonts/Inter";
import { loadFont as loadPoppins } from "@remotion/google-fonts/Poppins";
import { loadFont as loadNunito } from "@remotion/google-fonts/Nunito";
import { loadFont as loadRaleway } from "@remotion/google-fonts/Raleway";
import { loadFont as loadPlayfair } from "@remotion/google-fonts/PlayfairDisplay";
import { loadFont as loadMerriweather } from "@remotion/google-fonts/Merriweather";
import { loadFont as loadCairo } from "@remotion/google-fonts/Cairo";
import { loadFont as loadTajawal } from "@remotion/google-fonts/Tajawal";

// Each called once at module load — Remotion's delayRender is internal to these.
loadInter(); loadPoppins(); loadNunito(); loadRaleway();
loadPlayfair(); loadMerriweather(); loadCairo(); loadTajawal();

export const CURATED_FONTS = [
  { family: 'Inter', label: 'Inter (sans)' },
  { family: 'Poppins', label: 'Poppins (sans)' },
  { family: 'Nunito', label: 'Nunito (rounded)' },
  { family: 'Raleway', label: 'Raleway (light)' },
  { family: 'Playfair Display', label: 'Playfair Display (serif)' },
  { family: 'Merriweather', label: 'Merriweather (serif)' },
  { family: 'Cairo', label: 'Cairo (Arabic/Latin)' },
  { family: 'Tajawal', label: 'Tajawal (Arabic/Latin)' },
];
```

**Brand font registry (on-demand loading):**
```typescript
import { loadFont, continueRender, delayRender } from "@remotion/fonts";

const _loaded = new Set<string>();

export function ensureBrandFont(family: string, url: string): void {
  if (_loaded.has(family)) return;
  _loaded.add(family);
  const handle = delayRender(`brand-font:${family}`);
  loadFont({ family, url, weights: ["400", "700"] })
    .then(() => continueRender(handle))
    .catch(() => continueRender(handle)); // don't block render on font failure
}
```

**How `TextClipLayer` uses it:**
```typescript
// Inside TextClipLayer, before the return:
if (clip.text?.fontUrl) {
  ensureBrandFont(clip.text.fontFamily, clip.text.fontUrl);
}
```

**Vite alias for fonts.ts:** The frontend already has `resolve.alias["video-edit-composition"]` pointing to `VideoEdit.tsx`. Since `fonts.ts` is imported by `VideoEdit.tsx` as a relative `./fonts`, it resolves correctly through the alias chain without a separate alias.

**Export-time font URL resolution:** Brand font `fontUrl` in a text clip is the Player-side URL (`/api/agents/producer/assets/{id}/file`). This is a FastAPI endpoint — not fetchable by headless Chrome during `renderMedia`. The backend must rewrite this URL to a `http://127.0.0.1:3001/data/...` URL pointing to a downloaded copy in `VE_ROOT/fonts/{id}.{ext}`.

---

## Backend Changes

### Modified File: `backend/app/services/yas_video_editor.py`

**1. Extend `ClipSource` kind to include `'asset'` and audio-file handling**

New helper function `_clip_source_kind_id`:
```python
def _clip_source_kind_id(clip: dict) -> tuple[str | None, int | None]:
    src = clip.get("source") or {}
    kind = src.get("kind")
    if kind == "animation":   return "animation", src.get("animation_id")
    if kind == "upload":      return "upload", src.get("upload_id")
    if kind == "composition": return "composition", src.get("composition_id")
    if kind == "asset":       return "asset", src.get("asset_id")   # NEW
    return None, None
```

**2. New source resolver: `_fetch_asset_row(asset_id: int)`**

Queries `producer_assets` for `id, storage_bucket, storage_path, local_path, mime_type, asset_kind, attributes`. Returns the row dict or None. This mirrors `_fetch_animation_row`.

**3. New: `ensure_asset_local(asset_id: int) -> Optional[Path]`**

```python
async def ensure_asset_local(asset_id: int) -> Optional[Path]:
    row = await _fetch_asset_row(asset_id)
    if not row: return None
    # 1. local_path first
    if row.get("local_path"):
        p = asset_storage.local_to_abs(row["local_path"])
        if p.exists(): return p
    # 2. bucket download + cache at VE_ROOT/assets/{asset_id}.{ext}
    if row.get("storage_bucket") and row.get("storage_path"):
        ext = Path(row["storage_path"]).suffix or ".bin"
        cache = VE_ROOT / "assets" / f"asset-{asset_id}{ext}"
        if cache.exists() and cache.stat().st_size > 0: return cache
        signed = await asset_storage.url_for(
            storage_bucket=row["storage_bucket"],
            storage_path=row["storage_path"],
            local_path=None, expires=3600,
        )
        if signed and await _download_to(signed, cache): return cache
    return None
```

**4. Extend `resolve_source_local`:**
```python
async def resolve_source_local(kind: str, source_id: int) -> Optional[Path]:
    if kind == "animation": return await ensure_animation_local(int(source_id))
    if kind == "upload":    return await ensure_upload_local(int(source_id))
    if kind == "asset":     return await ensure_asset_local(int(source_id))    # NEW
    return None
```

**5. New: `ensure_font_local(asset_id: int) -> Optional[Path]`**

```python
async def ensure_font_local(asset_id: int) -> Optional[Path]:
    row = await _fetch_asset_row(asset_id)
    if not row or row.get("asset_kind") != "font": return None
    ext = Path(row.get("storage_path") or "").suffix or ".ttf"
    cache = VE_ROOT / "fonts" / f"font-{asset_id}{ext}"
    if cache.exists() and cache.stat().st_size > 0: return cache
    # resolve through same logic as ensure_asset_local
    local = await ensure_asset_local(asset_id)
    if local and local.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        import shutil; shutil.copy2(local, cache)
        return cache
    return None
```

**6. New: Player-side source URL for assets**

The existing `_source_play_url` pattern produces `/api/yas-producer/video-editor/sources/{kind}/{id}/video`. Add a new kind:
```python
def _asset_play_url(asset_id: int) -> str:
    return f"/api/agents/producer/assets/{asset_id}/file"
```

This reuses the existing `GET /api/agents/producer/assets/{id}/file` endpoint which already handles both bucket redirect and local-file streaming. No new endpoint needed.

**7. Modify `_resolve_clip_sources`:**
```python
async def _resolve_clip_sources(timeline: dict) -> dict:
    for clip in timeline.get("clips") or []:
        clip_kind = clip.get("kind") or "video"
        if clip_kind in ("text", "shape"):
            continue  # no source URL needed
        kind, sid = _clip_source_kind_id(clip)
        if kind == "asset" and sid is not None:
            clip["sourceUrl"] = _asset_play_url(int(sid))
        elif kind and sid is not None:
            clip["sourceUrl"] = _source_play_url(kind, int(sid))
        # Text clips: also resolve fontUrl if present
        text = clip.get("text") or {}
        if text.get("fontUrl") and text.get("fontFamily"):
            # fontUrl is already the Player URL — no transformation needed for Player
            pass
    return timeline
```

**8. Modify `build_render_props` (the export path):**

```python
async def build_render_props(project_id: int) -> tuple[dict, Path]:
    row = await _fetch_row(project_id)
    timeline = _video_edit(row["metadata"]) or {}
    clips = [dict(c) for c in (timeline.get("clips") or [])]
    
    for clip in clips:
        clip_kind = clip.get("kind") or "video"
        if clip_kind in ("text", "shape"):
            # Resolve brand font URL for text clips
            text = clip.get("text") or {}
            if text.get("fontUrl"):
                asset_id = _extract_asset_id_from_font_url(text["fontUrl"])
                if asset_id:
                    font_path = await ensure_font_local(asset_id)
                    if font_path:
                        clip["text"] = {**text, "fontUrl": _to_remotion_url(font_path)}
            clip["sourceUrl"] = None
            continue
        
        kind, sid = _clip_source_kind_id(clip)
        local = await resolve_source_local(kind, int(sid)) if (kind and sid is not None) else None
        clip["sourceUrl"] = _to_remotion_url(local) if local else None
    
    props = {
        "project": timeline.get("project") or {},
        "tracks": timeline.get("tracks") or [],
        "clips": clips,
    }
    out_path = VE_ROOT / str(project_id) / "final.mp4"
    return props, out_path
```

New helper `_extract_asset_id_from_font_url`: parses `/api/agents/producer/assets/{id}/file` to extract the integer id.

**9. Extend `_ensure_dirs`:**
```python
def _ensure_dirs() -> None:
    VE_ROOT.mkdir(parents=True, exist_ok=True)
    (VE_ROOT / "uploads").mkdir(parents=True, exist_ok=True)
    (VE_ROOT / "assets").mkdir(parents=True, exist_ok=True)    # NEW
    (VE_ROOT / "fonts").mkdir(parents=True, exist_ok=True)     # NEW
```

### Modified File: `backend/app/workers/yas_video_edit_composition_poller.py`

**Fix the "0 resolved clips" guard:** At line 79:
```python
# OLD:
resolved = sum(1 for c in props.get("clips", []) if c.get("sourceUrl"))

# NEW:
def _needs_url(c: dict) -> bool:
    return (c.get("kind") or "video") not in ("text", "shape")

total_media = sum(1 for c in props.get("clips", []) if _needs_url(c))
resolved = sum(1 for c in props.get("clips", []) if _needs_url(c) and c.get("sourceUrl"))
if total_media > 0 and resolved == 0:
    # existing failure path
```

This allows a project containing only text/shape layers (with no video clips) to export successfully.

### New Endpoint: `backend/app/routers/yas_producer.py`

Add endpoints for the new layer type pickers (sidebar bins for the frontend). These reuse `producer_assets` list filtering:

**`GET /api/yas-producer/video-editor/assets/images`**
```
→ calls list_assets(kind='image', limit=200)
→ attaches signed_url per item for preview thumbnails
```

**`GET /api/yas-producer/video-editor/assets/fonts`**
```
→ calls list_assets(kind='font', limit=200)  
→ attaches preview_url = /api/agents/producer/assets/{id}/preview per item
→ attaches file_url = /api/agents/producer/assets/{id}/file per item (for CSS @font-face)
```

**`GET /api/yas-producer/video-editor/assets/audio`**
```
→ calls list_assets with audio filter (asset_kind='other' AND mime_type LIKE 'audio/%')
→ or a dedicated audio upload endpoint (see Phase 5 below)
```

The audio use-case needs `yas_uploads` extended for audio, OR a new `producer_assets` category. The design decision: **store audio clips in `yas_uploads`** (same table that stores video uploads) since the upload pipeline already handles arbitrary MIME types. The `list_sources` endpoint already queries `yas_uploads` filtered to `mime_type LIKE 'video/%%'` — add an `audio` kind parameter that queries `mime_type LIKE 'audio/%%'`.

**`POST /api/yas-producer/video-editor/upload`** (existing endpoint) already handles arbitrary uploads. Extend `import_upload` to accept audio MIMEs (`audio/mpeg`, `audio/wav`, `audio/aac`, etc.) in addition to video. The duration probe (OpenCV) won't work for audio-only — use `ffprobe` instead for audio files, or skip duration probing and leave `naturalDurationInFrames = null` (the user sets clip duration manually).

---

## Schema Changes — Complete Diff

### `services/remotion/src/compositions/schema.ts` additions

After the existing `ClipTransition` interface and before `ClipSource`:

```typescript
// NEW: clip visual kind (default 'video' for backward-compat with stored docs)
export type ClipKind = 'video' | 'audio' | 'text' | 'shape' | 'image';

// NEW: shape variant
export type ShapeVariant = 'rect' | 'ellipse' | 'line' | 'triangle' | 'star';

// NEW: CSS filter bag — static for v1
export interface ClipFilters {
  brightness?: number;   // 0..2 (default 1 = no change)
  contrast?: number;     // 0..2
  saturate?: number;     // 0..2
  blur?: number;         // 0..40 px Gaussian radius
}

// NEW: text layer content
export interface ClipText {
  content: string;
  fontFamily: string;
  fontUrl?: string | null;   // brand font asset URL; null = Google/system font
  fontSize: number;          // px
  color: string;
  fontWeight?: number;
  textAlign?: 'left' | 'center' | 'right';
  lineHeight?: number;       // em
  background?: string | null;
  padding?: number;
  stroke?: string | null;
  strokeWidth?: number;
  shadow?: string | null;
}

// NEW: shape layer content
export interface ClipShape {
  variant: ShapeVariant;
  fill: string;
  stroke?: string | null;
  strokeWidth?: number;
  cornerRadius?: number;
  width: number;   // px in composition space
  height: number;
}
```

Modify `ClipSource`:
```typescript
export interface ClipSource {
  kind: 'animation' | 'upload' | 'composition' | 'asset';  // 'asset' is NEW
  animation_id?: number | null;
  upload_id?: number | null;
  asset_id?: number | null;                                 // NEW
  url?: string | null;
  naturalDurationInFrames?: number | null;
  audioFile?: boolean;                                      // NEW — true = <Audio>, false = <OffthreadVideo>
}
```

Modify `Clip`:
```typescript
export interface Clip {
  // ... all existing fields unchanged ...
  kind?: ClipKind | null;       // NEW — null/omit = 'video' (back-compat)
  text?: ClipText | null;       // NEW — defined when kind='text'
  shape?: ClipShape | null;     // NEW — defined when kind='shape'
  filters?: ClipFilters | null; // NEW — applies to all kinds including video
}
```

### `frontend/src/lib/types.ts` additions (mirror of schema.ts)

Identical new types under the `// ----- Video Editor -----` section:
```typescript
export type VideoEditClipKind = 'video' | 'audio' | 'text' | 'shape' | 'image';
export type VideoEditShapeVariant = 'rect' | 'ellipse' | 'line' | 'triangle' | 'star';

export interface VideoEditClipFilters {
  brightness?: number;
  contrast?: number;
  saturate?: number;
  blur?: number;
}

export interface VideoEditClipText {
  content: string;
  fontFamily: string;
  fontUrl?: string | null;
  fontSize: number;
  color: string;
  fontWeight?: number;
  textAlign?: 'left' | 'center' | 'right';
  lineHeight?: number;
  background?: string | null;
  padding?: number;
  stroke?: string | null;
  strokeWidth?: number;
  shadow?: string | null;
}

export interface VideoEditClipShape {
  variant: VideoEditShapeVariant;
  fill: string;
  stroke?: string | null;
  strokeWidth?: number;
  cornerRadius?: number;
  width: number;
  height: number;
}
```

Modify `VideoEditClipSource`:
```typescript
export interface VideoEditClipSource {
  kind: 'animation' | 'upload' | 'composition' | 'asset';  // 'asset' NEW
  animation_id?: number | null;
  upload_id?: number | null;
  asset_id?: number | null;                                 // NEW
  url?: string | null;
  naturalDurationInFrames?: number | null;
  audioFile?: boolean;                                      // NEW
}
```

Modify `VideoEditClip`:
```typescript
export interface VideoEditClip {
  // ... all existing fields unchanged ...
  kind?: VideoEditClipKind | null;   // NEW
  text?: VideoEditClipText | null;   // NEW
  shape?: VideoEditClipShape | null; // NEW
  filters?: VideoEditClipFilters | null; // NEW
}
```

---

## Data Flow

**Text clip creation (frontend):**
1. User clicks "Add Text" in a new `LayersPanel` sidebar section
2. Store dispatches `addTextClip(trackId)` → creates clip with `kind='text'`, default `ClipText`, `source={kind:'animation'}`, `durationInFrames=90`
3. Selection set → `ClipInspector` detects `clip.kind === 'text'` → renders `TextInspector` sub-panel
4. `scheduleSave()` → autosave → backend `save_timeline` stores the updated clips array in `yas_generations.metadata.video_edit.clips`
5. Player preview: `VideoEdit.tsx` receives the clip, `ClipLayer` branches to `TextClipLayer`, which reads `clip.text.fontUrl && ensureBrandFont(...)` if set
6. On export: `build_render_props` walks clips, sees `kind='text'`, resolves `fontUrl` to a `/data` filesystem path, sets `clip.text.fontUrl = http://127.0.0.1:3001/data/yas_producer/video-editor/fonts/font-{id}.ttf`

**Image/sticker clip creation:**
1. User opens new `AssetImagePicker` panel (list from `GET /api/yas-producer/video-editor/assets/images`) 
2. User clicks an asset → `addImageClip(assetId, assetUrl)` → clip with `kind='image'`, `source={kind:'asset', asset_id:N}`, `sourceUrl='/api/agents/producer/assets/{N}/file'`
3. Player preview: `ImageClipLayer` uses `clip.sourceUrl` → `<Img src={sourceUrl}>`
4. On export: `build_render_props` → `resolve_source_local('asset', N)` → `ensure_asset_local(N)` → download to `VE_ROOT/assets/asset-{N}.{ext}` → `_to_remotion_url(path)` → headless Chrome fetches from `:3001/data/...`

**Filters:**
1. User adjusts brightness slider in `ClipInspector` → `updateClip(id, {filters: {brightness: 1.3}})` → autosave
2. Player: `buildFilterStyle` in `ClipLayer` converts `{brightness:1.3}` → `"brightness(1.3)"` CSS string → applied to `<AbsoluteFill style={{filter: ...}}>`
3. Export: Same CSS filter in the same component — headless Chrome's compositor respects CSS `filter` in `renderMedia` (this is well-established Remotion behavior)

**Audio-music clip creation:**
1. User uploads audio file → new `uploadAudio` action hits the extended `POST /api/yas-producer/video-editor/upload` (accepts `audio/*` MIME)
2. Backend stores in `yas_uploads` with audio MIME, returns source descriptor with `kind='upload'`, `audioFile=true`
3. Store `addAudioClip(source)` → creates clip on an audio track with `source.audioFile=true`
4. `AudioClipLayer` branches: `src.audioFile === true` → `<Audio src={url} startFrom={trimBefore} endAt={trimAfter} volume={volume} muted={muted} />`

---

## Implementation Map

### Files to Create

**`services/remotion/src/compositions/fonts.ts`**
- Module-level `loadFont()` calls for 8 curated Google Fonts
- `ensureBrandFont(family, url)` with `delayRender`/`continueRender` 
- `CURATED_FONTS` constant array export
- No React, no Remotion composition primitives — pure font-loading side effects

**`frontend/src/components/yas/video-editor/LayersPanel.tsx`**
- "Add Text", "Add Shape", "Add Image/Sticker" buttons
- Dispatches store actions for new clip types
- Image section: embedded asset picker (calls `GET /api/yas-producer/video-editor/assets/images`)
- Font section for text: calls `GET /api/yas-producer/video-editor/assets/fonts`

**`frontend/src/components/yas/video-editor/TextInspector.tsx`**
- Sub-panel shown in `ClipInspector` when `clip.kind === 'text'`
- Inputs: textarea (content), font family dropdown (curated + brand fonts), font size, color picker, weight, align, line height, background toggle + color, stroke toggle, shadow preset

**`frontend/src/components/yas/video-editor/ShapeInspector.tsx`**
- Sub-panel for `clip.kind === 'shape'`: variant selector, fill color, stroke toggle, corner radius (rect only), width/height

**`frontend/src/components/yas/video-editor/FiltersPanel.tsx`**
- Sub-section in `ClipInspector` for all clip kinds: four sliders (brightness/contrast/saturate/blur) with live preview via Player seek

**`frontend/src/components/yas/video-editor/AudioMusicPanel.tsx`**
- Upload audio file button + uploaded audio list
- Calls extended `list_sources(kind='audio')` to show uploaded audio clips

### Files to Modify

**`services/remotion/src/compositions/schema.ts`**
- Add `ClipKind`, `ClipFilters`, `ClipText`, `ClipShape`, `ShapeVariant` types
- Extend `ClipSource` with `asset_id?`, `audioFile?`
- Extend `Clip` with `kind?`, `text?`, `shape?`, `filters?`

**`services/remotion/src/compositions/VideoEdit.tsx`**
- Import `fonts.ts` at top (side-effect import: `import "./fonts"`)
- Add `buildFilterStyle(filters)` pure function
- Add `ClipLayer` dispatcher component
- Add `TextClipLayer`, `ShapeClipLayer`, `ImageClipLayer` components
- Modify `VideoClipLayer`: strip its opacity/transform application (moved to ClipLayer wrapper)
- Modify `VideoTrackContent`: replace `<VideoClipLayer>` with `<ClipLayer>`
- Modify `AudioClipLayer`: branch on `clip.source?.audioFile`
- Import `Img`, `Audio` from `remotion`

**`frontend/src/lib/types.ts`**
- Add all new VideoEdit* types mirroring schema.ts additions

**`frontend/src/lib/store.ts`**
- Add `addTextClip(trackId?: string)` action
- Add `addShapeClip(trackId?: string)` action  
- Add `addImageClip(assetId: number, assetUrl: string, trackId?: string)` action
- Add `addAudioFileClip(source: VideoSource, trackId?: string)` action
- Extend `splitClip` to handle non-video `kind` (text/shape have no trim arithmetic — treat as non-trimmable, just split `from`/`durationInFrames`)
- Extend `KeyframeProp` type if new keyframable properties are added

**`backend/app/services/yas_video_editor.py`**
- Extend `_clip_source_kind_id` for `'asset'` kind
- Add `_fetch_asset_row`, `ensure_asset_local`, `ensure_font_local`
- Add `_asset_play_url`
- Add `_extract_asset_id_from_font_url`
- Modify `_resolve_clip_sources` (skip text/shape, use `_asset_play_url` for image kind)
- Modify `build_render_props` (text clip font resolution + image/audio asset resolution)
- Modify `_ensure_dirs` (new `assets/` and `fonts/` subdirs)
- Extend `import_upload` to accept audio MIMEs (`audio/mpeg`, `audio/wav`, `audio/aac`, `audio/ogg`)
- Extend `list_sources` to support `kind='audio'` querying `yas_uploads` for audio MIMEs

**`backend/app/workers/yas_video_edit_composition_poller.py`**
- Fix `_kick_render` resolved-clip count to skip text/shape clips

**`backend/app/routers/yas_producer.py`**
- Add `GET /api/yas-producer/video-editor/assets/images` → list producer_assets images
- Add `GET /api/yas-producer/video-editor/assets/fonts` → list producer_assets fonts
- Add `GET /api/yas-producer/video-editor/sources/audio/{id}/video` serving endpoint (mirrors the existing upload/animation video proxy pattern but for audio MIMEs)

**`frontend/src/lib/api.ts`**
- Add `api.yasProducer.videoEditor.listImageAssets()`
- Add `api.yasProducer.videoEditor.listFontAssets()`
- Add `api.yasProducer.videoEditor.listAudioSources()`

**`frontend/src/components/yas/video-editor/ClipInspector.tsx`**
- Branch on `clip.kind` → show `TextInspector`, `ShapeInspector`, or existing trim/audio controls
- Add `FiltersPanel` section below the existing controls (visible for all kinds)

**`frontend/src/components/yas/video-editor/EditorShell.tsx`**
- Add `LayersPanel` tab/section to the sources sidebar alongside the existing `SourcesPanel`
- Or integrate into SourcesPanel as a tabbed section

**`services/remotion/src/Root.tsx`**
- No change needed — composition id `video-edit` remains; `VideoEdit.tsx` is the component

---

## Build Sequence (Phased Checklist)

### Phase 1: Schema + Text Clips (independently testable — export a project with a text overlay)

- [ ] Add new types to `schema.ts`: `ClipKind`, `ClipFilters`, `ClipText`, `ClipShape`, `ShapeVariant`; extend `ClipSource` and `Clip`
- [ ] Mirror identically in `frontend/src/lib/types.ts`
- [ ] Create `fonts.ts` with 8 Google Font loadFont calls + `ensureBrandFont`
- [ ] Run `npm install @remotion/fonts@4.0.461 @remotion/google-fonts@4.0.461` in BOTH `services/remotion` and `frontend`
- [ ] Add `import "./fonts"` to `VideoEdit.tsx`
- [ ] Add `buildFilterStyle` helper to `VideoEdit.tsx`
- [ ] Refactor `VideoClipLayer` to remove its own opacity/transform (move them to the wrapper)
- [ ] Create `ClipLayer` dispatcher; replace `VideoClipLayer` usage in `VideoTrackContent`
- [ ] Add `TextClipLayer` — static font rendering with `ensureBrandFont` call
- [ ] Add `addTextClip` to store; add `TextInspector.tsx`
- [ ] Add `LayersPanel.tsx` ("Add Text" button)
- [ ] Test: create a project, add a text clip, verify Player preview shows text, verify export renders text; verify existing video clips still work
- [ ] Run `npx tsc --noEmit` in both `services/remotion` and `frontend`

### Phase 2: Fonts — Brand Font Picker

- [ ] Add `GET /api/yas-producer/video-editor/assets/fonts` endpoint (filters `producer_assets` by `asset_kind='font'`)
- [ ] Add `api.yasProducer.videoEditor.listFontAssets()` in api.ts
- [ ] Add font picker dropdown in `TextInspector`: shows curated fonts + brand fonts fetched from the endpoint
- [ ] Backend: implement `ensure_font_local` + `_extract_asset_id_from_font_url`
- [ ] Backend: in `build_render_props`, walk text clips and rewrite `fontUrl` to `/data` path
- [ ] Test: upload a `.ttf` to producer_assets, create a text clip using it, export and verify font appears in the rendered MP4

### Phase 3: Shapes

- [ ] Add `ShapeClipLayer` to `VideoEdit.tsx` (CSS/SVG shapes for rect, ellipse, line, triangle, star)
- [ ] Add `addShapeClip` to store; add `ShapeInspector.tsx`
- [ ] Add "Add Shape" button in `LayersPanel`
- [ ] Backend: shape clips have no source — `build_render_props` skips source resolution for `kind='shape'`; fix the poller's zero-URL guard
- [ ] Test: add shapes on multiple tracks, export, verify shapes appear at correct z-order with transitions

### Phase 4: Image/Sticker Clips

- [ ] Add `ImageClipLayer` using `<Img>` from Remotion
- [ ] Backend: implement `ensure_asset_local` + `_asset_play_url`; extend `_clip_source_kind_id` for `'asset'`; extend `_resolve_clip_sources` and `build_render_props`
- [ ] Add `GET /api/yas-producer/video-editor/assets/images` endpoint
- [ ] Add `addImageClip` to store
- [ ] Add image/sticker picker section in `LayersPanel` (grid of producer_assets thumbnails)
- [ ] Ensure `_ensure_dirs` creates `VE_ROOT/assets/`
- [ ] Test: pick an image from producer_assets, place it on the timeline, export; verify it renders at the correct position; test with both bucket and local-disk assets

### Phase 5: Filters

- [ ] Add `buildFilterStyle` (already done in Phase 1's refactor)
- [ ] Add `FiltersPanel.tsx` with four sliders
- [ ] Wire into `ClipInspector` for all clip kinds
- [ ] Confirm CSS `filter` applies correctly in Player and exported MP4 (headless Chrome supports all four CSS filter functions)
- [ ] Test: apply brightness reduction to a dark scene; export; compare frames

### Phase 6: Music/Audio File Clips

- [ ] Extend `import_upload` in `yas_video_editor.py` to accept `audio/*` MIME types (skip OpenCV duration probe for audio; use `ffprobe` via subprocess if needed, or accept `null` duration)
- [ ] Extend `list_sources` to support `kind='audio'`
- [ ] Add `GET /api/yas-producer/video-editor/sources/audio/{id}/video` serving endpoint (same FileResponse pattern as existing video proxy)
- [ ] Extend `AudioClipLayer` to branch on `clip.source?.audioFile` → use `<Audio>` from Remotion
- [ ] Add `addAudioFileClip` to store
- [ ] Add `AudioMusicPanel.tsx` with audio upload + list
- [ ] Test: upload an MP3, place on audio track, export, verify audio plays in the final MP4

---

## Critical Details

### Error Handling

- **Font load failure:** `ensureBrandFont` catches font load errors and calls `continueRender` anyway. A failed brand font falls back to the browser's default sans-serif. Log the failure but don't block export.
- **Asset not found during export:** If `ensure_asset_local` returns None for an image clip, `_to_remotion_url` returns None, `clip["sourceUrl"] = None`, and `ImageClipLayer` renders null (empty space). The export still completes. The poller's warning log ("project reported ready but file missing") will not fire since image clips are handled differently from video clips in the count guard.
- **Text clip with empty content:** `TextClipLayer` should render nothing (guard on `clip.text?.content`).

### State Management

- `kind`, `text`, `shape`, `filters` fields are passed through the store's `updateClip(id, patch)` unchanged. No special handling needed in the store for these fields.
- `splitClip` must be extended: for `kind === 'text'` or `kind === 'shape'` or `kind === 'image'`, the trim arithmetic (`trimBefore`/`trimAfter`) is irrelevant — set `trimBefore=0`, `trimAfter=durationInFrames` on both halves (they have no "source" to trim into).
- Font picker state: fetched once and cached in a local component state (not the global Zustand store, since fonts are infrequently changed and don't need timeline reactivity).

### Testing

Each phase has a verifiable export as its acceptance criterion. The key test for correctness: the Player preview and the exported MP4 must match visually. The shared `VideoEdit.tsx` composition guarantees this mechanically, but brand font URL rewriting is the one code path that differs between Player and export — it must be tested end-to-end with a real brand font upload.

### Performance

- `<Img>` in Remotion performs preloading per-frame by default (it calls `preloadImage` internally). For image clips with large files, this is appropriate.
- CSS `filter` is GPU-accelerated in Chrome and does not significantly impact rendering performance for typical values.
- The `ensureBrandFont` call inside `TextClipLayer` is guarded by `_loaded.has(family)` — it fires at most once per family per render session, not per frame.
- `delayRender` calls in `ensureBrandFont` during server-side `renderMedia` will block the render thread until the font is loaded. This is the intended behavior. The `continueRender` in the `.catch()` means a network failure for a brand font will not permanently hang the render — it proceeds after ~30s (Remotion's default `delayRender` timeout), producing text in the fallback font. Consider setting a shorter custom timeout.

### Risks and Open Questions

**Risk 1 — `delayRender` timeout in headless export for brand fonts.** During `renderMedia`, if the brand font URL (`http://127.0.0.1:3001/data/.../fonts/font-{id}.ttf`) is unreachable (e.g., Remotion service hasn't started, or the file wasn't downloaded by `ensure_font_local`), `delayRender` will time out after 30 seconds per font call. Mitigation: the backend must call `ensure_font_local` eagerly in `build_render_props` BEFORE submitting the render job — this guarantees the file exists at the expected path when the headless renderer fetches it.

**Risk 2 — `@remotion/google-fonts` package availability at version 4.0.461.** Google Fonts packages in `@remotion/google-fonts` are published as individual packages (e.g., `@remotion/google-fonts/Inter` is a path import into a monorepo package). Verify that `@remotion/google-fonts@4.0.461` exists on npm and supports the exact 8 fonts listed. If a font package doesn't exist, fall back to loading from Google Fonts CDN via `@remotion/fonts` `loadFont({family, url: 'https://fonts.gstatic.com/...'})` — this works in Player but may not load in headless due to network restrictions. In that case, download the font to `VE_ROOT/fonts/google/{family}.woff2` at server startup and serve via `:3001/data`.

**Risk 3 — CSS `filter` on `<AbsoluteFill>` creating a stacking context.** Applying `filter: blur(...)` on an `<AbsoluteFill>` creates a new stacking context in the browser. This means `z-index` of descendant elements behaves differently. In the current architecture, z-ordering is handled at the track level (sorted by `track.index`), not within a clip — so this should not matter. But it is worth a visual regression test after Phase 5.

**Risk 4 — Audio `<Audio>` vs `<OffthreadVideo>` naming mismatch.** Remotion's `<Audio>` component uses `startFrom` (frame number) and `endAt` (frame number) for trimming, while `<OffthreadVideo>` uses `trimBefore`/`trimAfter`. The existing `Clip` schema uses `trimBefore`/`trimAfter`. In `AudioClipLayer`, the `audioFile` branch must translate: `startFrom={clip.trimBefore}`, `endAt={clip.trimAfter}`. Verify this against the Remotion 4.0.461 `<Audio>` API docs before implementing.

**Risk 5 — `Img` import naming collision.** Remotion exports `Img` (capital I) as a render-safe image tag. The frontend also imports React-DOM's `img`. Make sure `import { Img } from "remotion"` is used in `ImageClipLayer`, not the HTML `<img>` tag, which is not render-safe and will break frame-accurate export.

**Open question — shape clip transitions.** The `buildRuns` / `TransitionSeries` logic works on adjacent clips regardless of kind. A shape clip transitioning into a video clip via a `TransitionSeries.Transition` will composite correctly since the outer `<AbsoluteFill>` structure is preserved. No code changes needed, but this should be tested explicitly (e.g., text fading into a video via a fade transition).

**Open question — audio clip `naturalDurationInFrames` for true audio files.** If `import_upload` can't probe audio duration (OpenCV is video-only), the source descriptor returns `naturalDurationInFrames: null`. The store's `addAudioFileClip` should default the clip to a fixed duration (e.g., 300 frames = 10s) and allow the user to extend it via trim handles. This is a UX gap that can be addressed by integrating `ffprobe` for audio files in Phase 6.

---

## File Path Summary

These are the exact absolute paths for every file created or modified:

**Created:**
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\services\remotion\src\compositions\fonts.ts`
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\frontend\src\components\yas\video-editor\LayersPanel.tsx`
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\frontend\src\components\yas\video-editor\TextInspector.tsx`
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\frontend\src\components\yas\video-editor\ShapeInspector.tsx`
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\frontend\src\components\yas\video-editor\FiltersPanel.tsx`
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\frontend\src\components\yas\video-editor\AudioMusicPanel.tsx`

**Modified:**
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\services\remotion\src\compositions\schema.ts`
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\services\remotion\src\compositions\VideoEdit.tsx`
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\services\remotion\package.json`
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\frontend\src\lib\types.ts`
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\frontend\src\lib\store.ts`
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\frontend\src\lib\api.ts`
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\frontend\src\lib\videoEditOps.ts` (extend `applyTrimEdge` to no-op for text/shape)
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\frontend\src\components\yas\video-editor\ClipInspector.tsx`
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\frontend\src\components\yas\video-editor\EditorShell.tsx`
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\frontend\package.json`
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\backend\app\services\yas_video_editor.py`
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\backend\app\workers\yas_video_edit_composition_poller.py`
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\backend\app\routers\yas_producer.py`