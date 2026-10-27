I now have a complete grounding in the codebase. Here is the full architecture blueprint.

---

# Video Editor Frontend UX Upgrades — Architecture Blueprint

## Patterns and Conventions Found

**Established patterns:**

- All modals must use `createPortal(node, document.body)` — enforced by the page-transition transform trap (CLAUDE.md, `AssetPickerModalV2.tsx:107`, `VideoEditorLibraryDetail.tsx:81`)
- Zustand stores follow a TTL-cache pattern with in-flight dedup (`_inflight` map in `store.ts:33`)
- The video editor store uses immutable mutation + 700ms debounced autosave (`store.ts:374-380`)
- Drag interactions keep their delta in LOCAL component state, commit once on `pointerup` — never write to the store mid-drag (`ClipBlock.tsx:74-124`)
- `laneAt(clientY)` is already the cross-track resolution function, owned by `Timeline.tsx:92-101` and passed down to every `ClipBlock`
- `KeyframeProp` is already typed (`store.ts:255`); `setKeyframe`/`removeKeyframe`/`moveKeyframe` exist (`store.ts:663-706`)
- `addClipFromSource(source, opts?)` is the canonical "put a clip on a track" action (`store.ts:449`)
- `setTransition` already exists and ripples downstream clips (`store.ts:708`)
- All picker/detail modals use the `AssetPickerModalV2.tsx` pattern: portal, search box, grid of tiles, footer commit
- `YasLibraryItem` has `type:'video_editor'`, `topic`, `updated_at`, `thumbnail_url`, `video_url` — sufficient for recents display
- `useYasLibraryStore` is already populated with video-editor items; the `VideoEditorCreator` already accepts `initialRunId` (`VideoEditorCreator.tsx:25`)
- `EditorShell.tsx:101-115` renders `<aside className="w-56"><SourcesPanel/></aside>` — the 56-unit column is the slot to replace

**Key file locations:**

- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\frontend\src\components\yas\video-editor\` (all editor files)
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\frontend\src\lib\store.ts` (Zustand store)
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\frontend\src\lib\types.ts` (all types)
- `C:\Users\LTSoft\Desktop\42\marketing\project files\ground up\frontend\src\lib\api.ts` (API wrapper)

---

## Feature 1: CreateProjectGate — Recent Projects Tab

### Current State

`CreateProjectGate.tsx` is a single-panel form (name + aspect) with no project history. `VideoEditorCreator.tsx:116-117` renders it when `!loaded`, passing `onCreated`. The `useYasLibraryStore` already has all video-editor items fetched elsewhere in the tab.

### Design

**Replace the single card with a two-tab layout.** Tab state is local to the gate (`useState<'recent'|'new'>`). Default to "Recent" if the library has any `video_editor` items; default to "New" if none.

**"Recent projects" tab:**
- On mount, call `useYasLibraryStore.getState().fetch({ limit: 300 })` — this is idempotent with TTL cache, so no extra network hit if the library panel was already open
- Filter `store.items.filter(it => it.type === 'video_editor')`, sorted by `updated_at DESC` (the field exists on `YasLibraryItem`)
- Render as a vertical list of `RecentProjectRow` tiles (NOT a grid — the gate is a narrow modal card)
- Each row: thumbnail (16:9 aspect-fit with 64px width), project name (`topic`), relative timestamp (`updated_at`), composition status badge, a "Resume" button
- Clicking a row calls `onResume(item.id)` — a new prop on `CreateProjectGate` that the parent wires to `VideoEditorCreator`'s `load(p)` flow (which already exists)
- If store is loading, show a skeleton row. If no video_editor items at all, auto-switch to the "New" tab with a small note

**"New project" tab:** Exactly the current form content, unchanged.

**Tab chrome:** Two pill-tabs at the top of the card. The card remains `max-w-md`.

**Integration in VideoEditorCreator.tsx:**
- `CreateProjectGate` gains a required `onResume: (projectId: number) => void` prop
- In `VideoEditorCreator`, `onResume` triggers the same `api.yasProducer.videoEditor.fetch(id)` + `load(p)` flow already in the `initialRunId` `useEffect` — extract that into a shared async `_hydrate(id)` function and call it from both

### New file: `CreateProjectGate.tsx` (replace in-place)

No new files needed. The gate already lives at `frontend/src/components/yas/video-editor/CreateProjectGate.tsx`.

**Changes to `VideoEditorCreator.tsx`:**
- Extract a `hydrate(id: number)` async function
- Pass `onResume={hydrate}` to `<CreateProjectGate>`

---

## Feature 2: Cross-Track Drag Ghost

### Root Cause Analysis (from actual code)

In `ClipBlock.tsx`:
- `onPointerMove` at line 100 calls `setDrag(d => ({...d, dx: e.clientX - d.startX}))` — this is a bare React state update on every pointer event, ~60+ per second, with no rAF throttle
- `drag.dx` drives the HORIZONTAL preview (`computePreview` at line 190) but there is NO vertical/Y tracking at all in `drag` state (`DragState` at line 25 has `startX`, `dx`, `alt` — no `clientY`)
- Cross-lane assignment happens only at `pointerup` via `laneAt(e.clientY)` — so during the drag, the block stays in its original lane with no visual feedback about where it will land
- When `moveClip(id, from, newTrackId)` fires, the clip's `trackId` changes, causing it to be removed from its old `TrackLane`'s children and re-added to the new one's — each `TrackLane` renders `<ClipBlock>` wrapped in its own `AbsoluteFill`/`relative` div, which remounts `<OffthreadVideo>` inside (the Remotion player re-fetches the video source)

### Design

**Architecture decision: Timeline-level overlay ghost.**

The ghost lives as a `position: fixed` `<div>` (portaled to `document.body`) that is shown during any active cross-lane drag. It follows the raw cursor in BOTH x and y. The source `ClipBlock` renders as 40% opacity while being dragged. Lane highlighting is a border-highlight applied to `TrackLane` via a shared ref/context.

This is preferred over embedding ghost logic in `ClipBlock` because:
1. The ghost must escape the scrolling `contentRef` div to span vertically across all lanes
2. A Timeline-level overlay correctly receives the `lanesRef` bounding rect for lane highlight calculation
3. It keeps `ClipBlock` purely for local resize/trim state (its current clean design)

**Interaction state design:**

Add a new ephemeral store slice `dragGhost` to `useVideoEditorStore` (NOT autosaved — view state only):

```
dragGhost: {
  active: boolean
  clipId: string | null
  clientX: number      // raw cursor position for ghost rendering
  clientY: number
  ghostWidthPx: number  // clip.durationInFrames * pxPerFrame, captured at dragstart
  ghostLabel: string    // e.g. "2.4s"
  targetTrackId: string | null   // currently hovered compatible track
} | null
```

Store actions:
```
beginDragGhost(clipId, clientX, clientY, widthPx, label): void
updateDragGhost(clientX, clientY, targetTrackId): void
endDragGhost(): void
```

These actions do NOT call `scheduleSave` (they are view-only). They live in the same `useVideoEditorStore` because components already subscribe to it, but a separate lightweight atom would also work.

**rAF throttle:**

In `ClipBlock.onPointerMove`, wrap the ghost position update in an rAF guard:

```
let _rafId: number | null = null
onPointerMove(e):
  dx = e.clientX - drag.startX        // still update local dx for trim preview
  setDrag(d => ({...d, dx}))           // local state for trim (unchanged)
  if drag.kind === 'move':
    if _rafId: return                  // skip — already queued
    _rafId = requestAnimationFrame(() => {
      _rafId = null
      const targetTrack = laneAt(e.clientY)
      useVideoEditorStore.getState().updateDragGhost(
        e.clientX, e.clientY,
        targetTrack?.id ?? null
      )
    })
```

**Ghost component: `DragGhost.tsx`**

New file: `frontend/src/components/yas/video-editor/DragGhost.tsx`

Rendered inside `Timeline.tsx` (portaled to `document.body`). Subscribes to `useVideoEditorStore(s => s.dragGhost)`. When active:
- Renders a `position:fixed` semi-transparent clone of a clip block (width = `ghostWidthPx`, height = `LANE_H - 6`, background = cyan-600/70, rounded, with duration label)
- Positioned at `left: clientX - ghostWidthPx / 2, top: clientY - (LANE_H/2)` — follows cursor naturally
- Has `pointer-events: none` so it does not interfere with pointer capture

**Lane highlight:**

`TrackLane` receives a `highlighted: boolean` prop. When `dragGhost.targetTrackId === track.id`, the lane's container gets an `outline: 2px solid var(--accent)` ring. This is just a prop comparison — no additional state needed.

`Timeline.tsx` passes `highlighted={dragGhost?.targetTrackId === t.id}` to each `TrackLane`.

**Drag begin:**

`ClipBlock.onPointerDown` (move kind only) calls:
```
useVideoEditorStore.getState().beginDragGhost(
  clip.id, e.clientX, e.clientY,
  clip.durationInFrames * pxPerFrame,
  `${(clip.durationInFrames/30).toFixed(1)}s`
)
```

**Drag commit (`onPointerUp`):**

Current logic (`ClipBlock.tsx:103-123`) already correctly calls `moveClip(id, newFrom, target.id)`. Add `endDragGhost()` before `setDrag(null)`. The source clip returns to full opacity; ghost disappears.

**Video reload mitigation:**

The reload happens because a clip moving to a new track causes its `<ClipBlock>` to unmount in the old `TrackLane` and mount in the new `TrackLane`. The Remotion `<Player>` in `PreviewPane` (not the timeline) re-derives `inputProps` via `useMemo` on `[project, tracks, clips]` — a `clips` mutation during `moveClip` causes a re-render but does NOT remount the player itself, so the preview is fine. The timeline thumbnail `<img>` tags also survive unmount/remount because they are just `<img src={thumbUrl}>` — no video element.

The reload flash the owner noticed is likely the `<OffthreadVideo>` INSIDE the Remotion Player re-fetching when `clips` changes (which remounts it). The Remotion Player already memoizes `inputProps` at `PreviewPane.tsx:60-63` via `useMemo`. Provided that memoization is correct, the only remaining cause is the Player re-rendering on the clip array reference change. The existing pattern is already correct for this. No structural change is needed here unless profiling reveals otherwise. Document this as a known limitation.

---

## Feature 3: CapCut-Style Master Keyframe Toggle

### Current State

`KeyframeEditor.tsx` has per-property `◆` buttons at the Inspector level. The Timeline toolbar (`Timeline.tsx:166-195`) has Split/Delete/zoom — no keyframe button. `ClipBlock.tsx` renders the clip block with NO keyframe diamonds.

### Design

**Part A: Master keyframe button in the timeline toolbar**

In `Timeline.tsx` toolbar (after the Split button, before the zoom controls):

Add a `◆` button (label: "Keyframe" or just `◆`) that:
1. Is disabled when no clip is selected
2. Calls `toggleMasterKeyframe(clipId, playheadFrame)`

**New store action: `toggleMasterKeyframe(clipId, frame)`**

Logic:
- Look up the clip
- Compute `localFrame = Math.max(0, playhead - clip.from)`, clamp to `[0, clip.durationInFrames - 1]`
- If ANY prop in `['opacity','scale','x','y','rotation']` (video) or `['volume']` (audio) has a keyframe at `localFrame`, REMOVE the keyframe at that local frame from ALL props (master remove)
- Otherwise, for each prop, read the current sampled value via `sampleKeyframes(kfs, localFrame, defaultVal)` and call the existing `_kfUpsert` for that prop at `localFrame` (master add)
- `isAudio` is determined by looking up the clip's track kind in `tracks`

This action calls `scheduleSave`.

**Part B: Keyframe diamonds ON the clip block in the timeline**

`ClipBlock.tsx` currently renders the clip's visual block. Add a keyframe diamond overlay layer ABOVE the gradient/thumbnail layers.

Data needed: the UNION of all keyframe frames across all props. Derive from `clip.keyframes`:
```
const allKeyframeFrames: number[] = useMemo(() => {
  const frames = new Set<number>()
  for (const kfs of Object.values(clip.keyframes ?? {})) {
    for (const kf of (kfs ?? [])) frames.add(kf.f)
  }
  return Array.from(frames).sort((a,b) => a-b)
}, [clip.keyframes])
```

Each unique frame `f` maps to a clip-local x position: `(f / clip.durationInFrames) * width` pixels from the clip's left edge.

Render as:
```
{allKeyframeFrames.map(f => (
  <button
    key={f}
    style={{
      left: (f / clip.durationInFrames) * width,
      bottom: 2,
      transform: 'translateX(-50%) rotate(45deg)',
    }}
    className="absolute w-2 h-2 bg-cyan-400 border border-cyan-600 pointer-events-auto"
    onClick={(e) => {
      e.stopPropagation()
      setPlayhead(clip.from + f)
    }}
    title={`Keyframe at frame ${f} — click to seek`}
  />
))}
```

The `setPlayhead` store action is already available. The diamond click seeks the playhead to the timeline frame (clip.from + f), consistent with `KeyframeLane.tsx:92`.

**Interaction with Inspector:** The per-property `KeyframeEditor` and `KeyframeLane` panels remain unchanged — they are still the fine-control surface.

---

## Feature 4: Tabbed Asset Sidebar

### Design Decision

Replace the `<aside className="w-56"><SourcesPanel/></aside>` in `EditorShell.tsx` with `<aside className="w-14 flex-row"><ToolRail/><SidebarPanel/></aside>` where the rail takes ~56px and the expanded panel takes ~224px (total ~280px). When no tab is active, the sidebar collapses to the rail only (56px). Active tab expands it.

The total width should not exceed ~280px to keep the editor usable. At `w-14` (56px) for the rail plus `w-56` (224px) for the panel, the shell's horizontal flex will need to widen slightly — adjust `EditorShell` to use `w-72` for the sidebar region when expanded, collapsing when no tab is active.

### Component Tree

```
EditorShell.tsx
  └─ SidebarShell.tsx          (new)
       ├─ ToolRail.tsx          (new)  w-14, flex-col, icon buttons
       └─ SidebarPanel.tsx      (new, conditional on activeTab)
            ├─ MediaBinPanel.tsx
            ├─ AudioPanel.tsx
            ├─ TextPanel.tsx
            ├─ StickerPanel.tsx
            ├─ ShapePanel.tsx
            ├─ TransitionPanel.tsx
            └─ EffectsPanel.tsx
```

All new files under `frontend/src/components/yas/video-editor/sidebar/`.

---

### ToolRail.tsx

Seven icon buttons stacked vertically in a 56px-wide column with `border-r border-border`. Each button highlights when its tab is active. Clicking an already-active tab collapses the panel (toggle behavior). Icons are text/SVG characters (no external icon library needed — consistent with the project's existing emoji+text button style).

Tab definitions (in display order):
1. `media` — "Media" — icon: `⬛` or a film-strip character
2. `audio` — "Audio" — icon: `♪`
3. `text` — "Text" — icon: `T`
4. `sticker` — "Stickers" — icon: `✦`
5. `shape` — "Shapes" — icon: `◻`
6. `transition` — "Transitions" — icon: `⇄`
7. `effects` — "Effects" — icon: `✦` (distinct styling)

Tab state is LOCAL to `SidebarShell` (`useState<SidebarTab | null>`).

---

### MediaBinPanel.tsx

This replaces the current `SourcesPanel.tsx` entirely.

**The media bin concept:** A per-project list of clips the user has deliberately ADDED to the project's media pool. Not all available sources — just the ones the owner chose. This is distinct from `useVideoSourcesStore` (which holds ALL generated clips globally).

**Data model addition to `VideoEditProjectMeta`:**

Add `media_bin?: MediaBinEntry[]` to `VideoEditProjectMeta` in `types.ts`:
```typescript
export interface MediaBinEntry {
  kind: 'animation' | 'upload'
  id: number
}
```

The store already persists `project` to the backend via `flush()` — `media_bin` goes into the `VideoEditProjectMeta` blob, which is stored in the `metadata` JSONB column on the backend (already a passthrough JSONB field). No schema change needed.

**Store actions to add:**

```typescript
addToBin(entry: MediaBinEntry): void
// Idempotent — deduplicates by (kind, id)
// Calls scheduleSave via mutate

removeFromBin(entry: MediaBinEntry): void
// Removes from project.media_bin
// Calls scheduleSave
```

**Panel layout:**
- Header: "Media" eyebrow, `+ Add` button (opens `VideoSourcePickerModal`)
- Empty state: "Add clips to your media bin to get started"
- Bin list: `MediaBinTile` components — one per `project.media_bin` entry, resolved against `useVideoSourcesStore.items`

**MediaBinTile:**
- 16:9 thumbnail (64px wide) with hover-to-play video preview (reuse `YasLibrary.tsx LibraryTile` pattern: swap `<img>` for `<video autoPlay muted loop playsInline>` on `mouseenter`, back to `<img>` on `mouseleave`)
- Duration badge
- Clip name
- Remove (×) button
- Click or drag: `addClipFromSource(source)` (existing action)

**VideoSourcePickerModal.tsx** (new file in `video-editor/`):

Following the `AssetPickerModalV2.tsx` pattern exactly (portal, search, grid, footer commit), but with VIDEO tiles instead of image tiles.

Tile content: 16:9 thumbnail with hover-to-play `<video>` (same hover pattern as `YasLibrary.tsx LibraryTile`). Uses `api.yasProducer.videoEditor.sourceVideoUrl(kind, id)` for the video src, `api.yasProducer.videoEditor.sourceThumbUrl(kind, id)` for the thumbnail.

Items are drawn from `useVideoSourcesStore.items`. On mount, calls `useVideoSourcesStore.getState().fetch({ limit: 500 })`.

Multi-select: a `Set<string>` of `"kind-id"` keys. "Add to bin" button calls `addToBin` for each selected source, then closes. Already-in-bin items are shown with a checkmark overlay (not excluded, because the picker can also re-add).

**Existing `SourcesPanel.tsx`:** Keep the file but it can be retired. The `EditorShell` import switches to `SidebarShell`.

---

### AudioPanel.tsx

A simplified panel showing clips currently on audio tracks (read from store `clips.filter(c => tracks.find(t=>t.id===c.trackId)?.kind==='audio')`). An "Add audio" button opens a file picker (`input type="file" accept="audio/*"`) and calls `api.yasProducer.videoEditor.upload(file)` + `addClipFromSource` with the returned source (the upload endpoint already handles video files; audio uploads are a backend concern — flag this as a risk, see below). Falls outside Phase 1 scope.

---

### TextPanel.tsx, ShapePanel.tsx, StickerPanel.tsx

These three panels add clips of new kinds (`text`, `shape`, `image`) to the timeline. They depend on the composition work that defines those clip types. The panels are **stub-only in Phase 2**, showing "Coming soon" empty states, with the actions pre-declared in the store.

**Store actions pre-declared (no-op stubs until composition work lands):**

```typescript
addTextClip(trackId?: string): void
// Creates a clip with source.kind='text', appends to first video track

addShapeClip(trackId?: string, shapeKind?: string): void
// source.kind='shape'

addImageClip(trackId?: string, imageUrl?: string): void
// source.kind='image'

addAudioClip(trackId?: string): void
// kind='audio', appends to first/new audio track
```

The store's `addClipFromSource` is already generic. These new actions are convenience wrappers that construct a synthetic `VideoSource`-like object and call through. They call `scheduleSave` via `mutate`.

---

### TransitionPanel.tsx

A palette of the 6 transition presets (plus "None"). Rendered as a `3-column grid` of `TransitionCard` tiles, each showing:
- Preview thumbnail (a static visual illustration — a small SVG or CSS animation showing the transition concept)
- Label (Fade, Slide, Wipe, Flip, Clock Wipe, Iris)

Clicking a card:
1. Checks if there is a selected clip (`selectedClipId`)
2. Looks up the clip's previous adjacent neighbour (same logic as in `ClipInspector.tsx:59-63`)
3. If eligible, calls `setTransition(clipId, preset, 15, 'linear')` — the existing store action
4. If no eligible clip, shows an inline tooltip "Select a clip with a preceding neighbour first"

**No drag onto boundary** in Phase 1 — flagged as stretch.

---

### EffectsPanel.tsx

Brightness / contrast / saturate / blur sliders applied to the SELECTED clip.

**New store action and type additions:**

In `types.ts`, add to `VideoEditClip`:
```typescript
filters?: {
  brightness?: number   // 0..2, default 1
  contrast?: number     // 0..2, default 1
  saturate?: number     // 0..2, default 1
  blur?: number         // 0..20, px, default 0
}
```

Store action:
```typescript
setClipFilters(clipId: string, filters: Partial<VideoEditClip['filters']>): void
// Merges into clip.filters, calls scheduleSave
```

Panel layout: four labeled sliders. Each slider calls `setClipFilters` with its value. Reset button sets all to defaults.

Note: the Remotion composition will need to read `clip.filters` and apply CSS filter strings. This is wired in the composition task, not here. The Inspector and panel write to the store correctly regardless.

---

## Feature 5: ClipInspector Additions per Clip Kind

The Inspector currently shows trim/keyframes/transition/audio for all clips uniformly. It needs to branch on `clip.source?.kind` once new clip kinds exist.

**In `ClipInspector.tsx`:**

Add a kind-dispatch section after the existing trim panel:

```
clip.source.kind === 'text'   → <TextClipPanel clip={clip} />
clip.source.kind === 'shape'  → <ShapeClipPanel clip={clip} />
clip.source.kind === 'image'  → <ImageClipPanel clip={clip} />
```

For VIDEO/AUDIO clips (existing kinds `'animation' | 'upload' | 'composition'`): add a new `<FiltersPanel clip={clip} />` section below the keyframes section.

**FiltersPanel (sub-component within ClipInspector.tsx or its own file):**

Four sliders reading from `clip.filters`, calling `setClipFilters`. A "Reset" button.

**TextClipPanel (stub for Phase 3):** Fields: content (textarea), font (select), size (number), color (color input), align (left/center/right buttons). All write to `clip.textProps` (new field on `VideoEditClip`, parallel task defines this).

**ImageClipPanel (stub for Phase 3):** Fit toggle: cover/contain/fill. Writes to existing `clip.fit` field (already in `VideoEditClip`).

---

## Store Additions — Complete Signature List

All additions are to `useVideoEditorStore` in `store.ts`.

**State additions:**

```typescript
// Drag ghost (ephemeral — never autosaved)
dragGhost: {
  clipId: string
  clientX: number
  clientY: number
  widthPx: number
  label: string
  targetTrackId: string | null
} | null
```

**Action additions:**

```typescript
// Drag ghost (no scheduleSave)
beginDragGhost(clipId: string, x: number, y: number, widthPx: number, label: string): void
updateDragGhost(x: number, y: number, targetTrackId: string | null): void
endDragGhost(): void

// Media bin (autosaved via project.media_bin)
addToBin(entry: MediaBinEntry): void
removeFromBin(entry: MediaBinEntry): void

// Master keyframe (autosaved)
toggleMasterKeyframe(clipId: string, timelineFrame: number): void

// Filters (autosaved)
setClipFilters(clipId: string, filters: Partial<VideoEditClip['filters']>): void

// New clip kinds (stubs — autosaved)
addTextClip(opts?: { trackId?: string }): void
addShapeClip(opts?: { trackId?: string; shapeKind?: string }): void
addImageClip(opts?: { trackId?: string; imageUrl?: string }): void
addAudioClip(opts?: { trackId?: string }): void
```

**Type additions to `types.ts`:**

```typescript
// To VideoEditClip:
filters?: {
  brightness?: number
  contrast?: number
  saturate?: number
  blur?: number
}
// clip.source.kind extends to: 'animation'|'upload'|'composition'|'text'|'shape'|'image'

// New top-level:
export interface MediaBinEntry {
  kind: 'animation' | 'upload'
  id: number
}

// To VideoEditProjectMeta:
media_bin?: MediaBinEntry[]
```

---

## Data Flow

**Drag ghost flow (complete):**

```
User presses pointer on ClipBlock body (kind='move')
  → ClipBlock.onPointerDown
      → select({ kind:'clip', clipId })
      → setPointerCapture
      → setDrag({ kind:'move', startX, dx:0 })
      → beginDragGhost(clipId, clientX, clientY, widthPx, label)  ← NEW

User moves pointer
  → ClipBlock.onPointerMove
      → compute dx = clientX - startX
      → setDrag({...d, dx})                       [existing local state for trim preview]
      → if rAF slot open:                          ← NEW rAF guard
          requestAnimationFrame(() => {
            targetTrack = laneAt(clientY)
            updateDragGhost(clientX, clientY, targetTrack?.id)
          })

DragGhost component (portaled to body)
  → subscribes to dragGhost store slice
  → renders fixed div at (clientX - widthPx/2, clientY - LANE_H/2)
  → pointer-events:none

TrackLane components
  → receive highlighted={dragGhost?.targetTrackId === track.id}  ← passed from Timeline
  → render outline ring when highlighted

User releases pointer
  → ClipBlock.onPointerUp
      → endDragGhost()                             ← NEW (clears ghost + highlight)
      → compute newFrom (existing snap logic)
      → target = laneAt(e.clientY)                 (existing)
      → moveClip(clipId, newFrom, target?.id)       (existing)
      → setDrag(null)
```

**Master keyframe toggle flow:**

```
User clicks ◆ in Timeline toolbar
  → Timeline reads selectedClipId, playheadFrame
  → calls toggleMasterKeyframe(clipId, playheadFrame)
  → store action:
      - find clip, determine localFrame
      - determine isAudio from track.kind
      - props = isAudio ? ['volume'] : ['opacity','scale','x','y','rotation']
      - hasAny = props.some(p => clip.keyframes?.[p]?.some(k => k.f === localFrame))
      - if hasAny: removeKeyframe for each prop at localFrame
      - else: for each prop, setKeyframe at localFrame with sampled current value
      - scheduleSave
  → ClipBlock re-renders: allKeyframeFrames recalculates → diamond overlay updates
  → KeyframeEditor re-renders (Inspector): per-prop ◆ buttons update (existing)
```

**Media bin + picker flow:**

```
User clicks "Add" in MediaBinPanel
  → VideoSourcePickerModal opens (portaled to body)
  → useVideoSourcesStore.fetch() (cached)
  → user browses, hovers tiles for video preview, multi-selects
  → clicks "Add to bin (N)"
  → for each selected: store.addToBin({kind, id})
  → project.media_bin grows → scheduleSave → flush persists to backend
  → modal closes

User clicks a bin tile
  → addClipFromSource(source, { trackId: firstVideoTrack.id })
  → clip appended to timeline at end of track
  → autosave fires after 700ms
```

---

## Implementation Map — Complete File List

### New files to create:

| Path | Description |
|------|-------------|
| `frontend/src/components/yas/video-editor/DragGhost.tsx` | Portal ghost that follows cursor during cross-lane move |
| `frontend/src/components/yas/video-editor/sidebar/SidebarShell.tsx` | Outer container: ToolRail + conditional SidebarPanel |
| `frontend/src/components/yas/video-editor/sidebar/ToolRail.tsx` | 56px icon column, tab toggle buttons |
| `frontend/src/components/yas/video-editor/sidebar/MediaBinPanel.tsx` | Per-project media pool with hover-to-play tiles |
| `frontend/src/components/yas/video-editor/sidebar/AudioPanel.tsx` | Audio clips list + upload stub |
| `frontend/src/components/yas/video-editor/sidebar/TextPanel.tsx` | Stub panel (Phase 3) |
| `frontend/src/components/yas/video-editor/sidebar/StickerPanel.tsx` | Stub panel (Phase 3) |
| `frontend/src/components/yas/video-editor/sidebar/ShapePanel.tsx` | Stub panel (Phase 3) |
| `frontend/src/components/yas/video-editor/sidebar/TransitionPanel.tsx` | 6-preset palette, applies to selected clip |
| `frontend/src/components/yas/video-editor/sidebar/EffectsPanel.tsx` | brightness/contrast/saturate/blur sliders |
| `frontend/src/components/yas/video-editor/VideoSourcePickerModal.tsx` | Multi-select video picker with hover-play tiles |

### Existing files to modify:

| Path | Change |
|------|--------|
| `frontend/src/components/yas/video-editor/CreateProjectGate.tsx` | Add `onResume` prop, two-tab layout, `RecentProjectRow` list |
| `frontend/src/components/yas/video-editor/VideoEditorCreator.tsx` | Extract `_hydrate(id)`, pass `onResume` to gate |
| `frontend/src/components/yas/video-editor/ClipBlock.tsx` | Add `dragGhost` store calls, rAF throttle on pointermove, opacity fade during drag, diamond overlay render |
| `frontend/src/components/yas/video-editor/TrackLane.tsx` | Add `highlighted: boolean` prop, render accent ring |
| `frontend/src/components/yas/video-editor/Timeline.tsx` | Import `DragGhost`, pass `dragGhost` state to `TrackLane` for highlights, add `◆ Keyframe` toolbar button |
| `frontend/src/components/yas/video-editor/ClipInspector.tsx` | Add `FiltersPanel` section, kind-dispatch for text/shape/image stub panels |
| `frontend/src/components/yas/video-editor/EditorShell.tsx` | Replace `<aside w-56><SourcesPanel/>` with `<SidebarShell>`, adjust widths |
| `frontend/src/lib/store.ts` | Add `dragGhost` state + 3 ghost actions, `addToBin`/`removeFromBin`, `toggleMasterKeyframe`, `setClipFilters`, 4 stub clip-add actions |
| `frontend/src/lib/types.ts` | Add `MediaBinEntry`, `media_bin` to `VideoEditProjectMeta`, `filters` to `VideoEditClip`, extend `VideoEditClipSource.kind` |

---

## Build Sequence (Phased Checklist)

### Phase 1 — Gate + Drag + Master Keyframe (independently demoable)

These three features touch different areas and can be built and demoed in isolation before the sidebar work. Complete before Phase 2.

- [ ] **Gate — Recents tab**
  - [ ] Add `onResume` prop to `CreateProjectGate`
  - [ ] Extract `_hydrate(id)` in `VideoEditorCreator`, pass to gate
  - [ ] Add `RecentProjectRow` tile component in `CreateProjectGate`
  - [ ] Fetch + filter library store for `type:'video_editor'`, sort by `updated_at DESC`
  - [ ] Default tab logic (has recents → recents, else new)

- [ ] **Drag ghost**
  - [ ] Add `dragGhost` slice + 3 actions to `store.ts` (no scheduleSave)
  - [ ] Create `DragGhost.tsx` (portal, fixed, pointer-events-none)
  - [ ] Modify `ClipBlock.onPointerDown` (move kind) to call `beginDragGhost`
  - [ ] Add rAF throttle to `ClipBlock.onPointerMove` for ghost position updates
  - [ ] Add opacity fade (40%) to `ClipBlock` container when `dragGhost?.clipId === clip.id`
  - [ ] Call `endDragGhost()` in `ClipBlock.onPointerUp` before setDrag(null)
  - [ ] Add `highlighted` prop to `TrackLane`, render accent ring
  - [ ] Modify `Timeline` to compute + pass `highlighted` to each `TrackLane`, render `<DragGhost>`

- [ ] **Master keyframe button**
  - [ ] Add `toggleMasterKeyframe` to store (uses existing `_kfUpsert` + `sampleKeyframes`)
  - [ ] Add `◆ Keyframe` button to `Timeline.tsx` toolbar (disable when no selection)
  - [ ] Add keyframe diamond overlay to `ClipBlock` (union of frames, click to seek)

### Phase 2 — Sidebar Shell + Media Bin + Picker (depends on Phase 1 complete)

- [ ] **Store additions**
  - [ ] Add `MediaBinEntry` type to `types.ts`
  - [ ] Add `media_bin?: MediaBinEntry[]` to `VideoEditProjectMeta`
  - [ ] Add `addToBin`/`removeFromBin` store actions

- [ ] **SidebarShell + ToolRail**
  - [ ] Create `sidebar/SidebarShell.tsx` (tab state, ToolRail + panel container)
  - [ ] Create `sidebar/ToolRail.tsx` (7 icon buttons, active/collapsed states)
  - [ ] Wire `SidebarShell` into `EditorShell.tsx` (replace SourcesPanel aside)
  - [ ] Adjust aside widths in `EditorShell` for collapsed/expanded states

- [ ] **VideoSourcePickerModal**
  - [ ] Create `VideoSourcePickerModal.tsx` (portal, search, grid, hover-play tiles, multi-select, "Add to bin")

- [ ] **MediaBinPanel**
  - [ ] Create `sidebar/MediaBinPanel.tsx` (bin list, hover-play tiles, "+ Add" opens picker, remove button, click-to-add-to-timeline)

- [ ] **TransitionPanel**
  - [ ] Create `sidebar/TransitionPanel.tsx` (6-preset grid, click applies to selected eligible clip)

### Phase 3 — Effects, Text, Shapes/Stickers (depends on Phase 2, parallel with composition work)

- [ ] **Store additions**
  - [ ] Add `filters` to `VideoEditClip` in `types.ts`
  - [ ] Add `setClipFilters` action to store
  - [ ] Add `addTextClip`/`addShapeClip`/`addImageClip`/`addAudioClip` stub actions

- [ ] **EffectsPanel**
  - [ ] Create `sidebar/EffectsPanel.tsx` (4 sliders, reset)
  - [ ] Add `FiltersPanel` section to `ClipInspector.tsx`

- [ ] **Inspector kind dispatch**
  - [ ] Add kind-dispatch section to `ClipInspector` for text/shape/image stub panels
  - [ ] Create stub `TextClipPanel`, `ShapeClipPanel`, `ImageClipPanel` sub-components

- [ ] **Text/Shape/Sticker panels (stub)**
  - [ ] Create `sidebar/TextPanel.tsx` (palette of text styles, calls `addTextClip`)
  - [ ] Create `sidebar/ShapePanel.tsx` (palette of 6-8 shapes, calls `addShapeClip`)
  - [ ] Create `sidebar/StickerPanel.tsx` (stub "coming soon" + image upload path)

### Phase 4 — Audio Panel (depends on backend audio upload support)

- [ ] Create `sidebar/AudioPanel.tsx`
- [ ] Verify backend `/video-editor/upload` accepts audio MIME types
- [ ] Create audio track auto-provisioning in `addAudioClip`

---

## Critical Details

### Error Handling

- `VideoSourcePickerModal`: if `useVideoSourcesStore` fetch fails, show error inline with a Retry button (consistent with `SourcesPanel`'s current `store.error` handling)
- `addToBin` is purely local — no network call. Cannot fail. Only persisted at next autosave.
- `toggleMasterKeyframe`: the `sampleKeyframes` import from `video-edit-schema` (already used in `KeyframeEditor.tsx:13`) must be available in the store file. Since the store is not a React component, import it directly
- `DragGhost` should guard against stale ghost state: `endDragGhost()` must be called in all `onPointerUp` paths including the locked-track early return at `ClipBlock.tsx:88`

### State Management

- `dragGhost` must NOT be included in `flush()` payload — it is view-only. The `flush()` sends `{ project, tracks, clips }` (`store.ts:761`). The ghost is not in any of those. Safe by construction.
- `media_bin` IS in `project` → it IS persisted to the backend on every autosave. The backend stores it in the JSONB `metadata` column of the project row (it's a passthrough). No backend change needed assuming the PATCH endpoint stores `project` as-is. Verify this with the backend engineer.
- `toggleMasterKeyframe` calls `scheduleSave` via direct calls to the existing `setKeyframe`/`removeKeyframe` logic (copy the `_kfUpsert` pattern inline, or call `get().setKeyframe()` in a loop). Do NOT call `get().setKeyframe()` in a loop from a Zustand action — it triggers N separate re-renders and N autosave debounce resets. Instead, perform all prop mutations in a single `set(s => ...)` call like the existing keyframe ops.

### Testing

There are no test suites. Smoke-test each phase against the `/docs` Swagger and manual browser interaction:
- Phase 1: drag a clip from V1 to V2, verify ghost appears, target lane highlights, clip lands correctly, no console errors
- Phase 1: press `◆` toolbar button with a selected clip mid-timeline, verify diamonds appear on clip block in expected positions, clicking a diamond seeks playhead
- Phase 2: open picker, select 3 sources, confirm bin shows them, click a bin tile, confirm clip on timeline
- Phase 3: apply blur=10 to a clip, export, verify the MP4 has the blur effect (requires composition integration)

### Performance

- rAF throttle on `pointermove` is the single most important perf fix. On a 60Hz screen it limits ghost position updates to 60/s instead of potentially 200+/s. Measure with Chrome DevTools flame chart before/after.
- `allKeyframeFrames` `useMemo` in `ClipBlock` depends on `clip.keyframes`. Since `clip` is already a stable object reference (Zustand immutable updates), this will only recompute when keyframes change. Good.
- The `VideoSourcePickerModal` hover-play: mount a single `<video>` element per tile (not eagerly, on `mouseenter`), clear `src` on `mouseleave` to prevent multiple concurrent video decoders. Follow `YasLibrary.tsx LibraryTile`'s exact hover pattern.

### Risks and Open Questions

1. **Backend audio upload support:** The `/video-editor/upload` endpoint accepts `video/*`. Whether it also accepts `audio/*` MIME types must be verified before `AudioPanel` is built. Flag for Phase 4.

2. **`media_bin` in `VideoEditProjectMeta` passthrough:** The backend's `PATCH /video-editor/{id}/timeline` receives `project` as a raw dict and stores it. Confirm this is a full passthrough (not filtered by Pydantic model that would strip unknown fields). If `VideoEditProjectMeta` is a strict Pydantic model server-side, `media_bin` will be stripped silently. The fix is to add `media_bin: list[dict] | None = None` to the Pydantic model — a small backend change.

3. **`sampleKeyframes` import in store.ts:** `store.ts` already imports `applyTrimEdge` from `./videoEditOps` (line 10). `sampleKeyframes` is imported from `video-edit-schema` in `KeyframeEditor.tsx:12`. The same import needs to be available in the store for `toggleMasterKeyframe`. Since `video-edit-schema` is already a workspace package dependency (it is used in `PreviewPane.tsx:18`), the import will work.

4. **Drag ghost Z-index:** The ghost is portaled to `document.body` with `position:fixed`. Set `z-index: 999` (above the sidebar and modal overlays at z=110, but below any error toasts). Verify against `ToastProvider.tsx` z-index.

5. **Clip kind extension:** Adding `'text'|'shape'|'image'` to `VideoEditClipSource.kind` in `types.ts` is backward-compatible (the Remotion composition handles unknown kinds gracefully if it defaults unknown kinds to a transparent layer). The composition task must explicitly handle these before `addTextClip` etc. fire.

6. **TransitionPanel "no eligible clip" state:** The `setTransition` store action silently returns `{}` when there is no preceding adjacent neighbour (`store.ts:715-716`). The `TransitionPanel` must surface this to the user — an inline message when `selectedClipId` exists but the clip has no eligible predecessor.

7. **Hover-play video tiles and browser autoplay policy:** `<video autoPlay muted playsInline>` with `muted` satisfies Chrome's autoplay policy. Ensure `muted` is always set on hover-play videos in `MediaBinTile` and `VideoSourcePickerModal`.