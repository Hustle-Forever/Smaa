# Plan — Video Editor: foundational UX fixes + an AI edit engine (analyze → plan → execute)

## Context

The Video Editor (multi-track NLE: trim/split/layer/keyframe/transition → export) is powerful
but too hard for non-technical users, and it's missing safety nets. We will (1) fix the
foundational UX so editing is forgiving and discoverable, then (2) add an **AI edit engine**
that does the hard work for the user — pop-ups, auto-compose, holistic AI edit, voice change,
and music — with minimal UI and maximal functionality.

The editor is **fully programmatic**: the whole document is `{project, tracks, clips}` JSONB at
`yas_generations.metadata.video_edit` (mode `video_edit_preview`), integer frames @30fps, and the
Remotion `video-edit` composition already renders text/shape/image clips, keyframed
transforms (opacity/scale/x/y/rotation/volume), transitions, and audio. So every AI feature is
just: **analyze the media → produce a structured edit plan → apply it as deterministic timeline
mutations → save**. No Remotion changes are needed — the AI only emits clip types that already
render. Precedent for server-side timeline construction: `create_project_from_sources`
(`backend/app/services/yas_video_editor.py:433`) and `open_in_editor` (`yas_talking_head_v2.py:5341`).

**Decisions (from the user):** ElevenLabs speech-to-speech for voice (key arriving later);
music = Suno generation **default** + pick-from-library **alternative** (Suno behind a provider
interface until `SUNO_API_KEY` lands; library-pick works today); **phased** delivery
(foundational first, verify, then AI); AI edits **auto-apply** (the new undo/redo makes this safe).
**Models:** Gemini for media analysis, gpt-4o (proven, structured output) or Gemini for the
planner — Anthropic/Claude is NOT wired into the backend, so runtime uses Gemini/OpenAI only.
**Dev work uses Fable 5** (exploration/planning/implementation agents).

A deeper engineering blueprint produced during planning lives at
`there-is-a-last-lucky-teacup-agent-aeae8a9e0f21a4f6d.md` (reference for op schemas / edge cases).

---

## PHASE 1 — Foundational UX (ship + verify FIRST)

### 1A. Undo / Redo (does not exist today)
All timeline mutations funnel through the single zustand store `useVideoEditorStore`
(`frontend/src/lib/store.ts:462`); the persisted doc is exactly `{project, tracks, clips}`.
- Add a bounded history stack (snapshot those 3 refs). Wrap the commit path: push a snapshot at
  the **start of each logical change**, coalescing continuous gestures into ONE step.
- **Gesture coalescing is the real work:** `applyCanvasTransform` (`store.ts:826`) writes per-rAF
  during gizmo drags, and inspector sliders fire per `onChange` — wrap these as
  pointerdown→pointerup = one undo entry (begin/commit history API). Clip drags
  (`ClipBlock.tsx`) and keyframe-lane drags already commit once on pointerup — no coalescing needed.
- Exclude ephemeral state (selection/playhead/zoom/scroll/dragGhost/saveState/render) from snapshots.
- UI: `Ctrl+Z` / `Ctrl+Shift+Z` (+`Ctrl+Y`) in `Timeline.tsx` keyboard handler (~line 109) and two
  toolbar buttons. Undo/redo set state then `scheduleSave()` like every other mutator.

### 1B. Multi-transform keyframes (rotate AND scale on the same keyframe)
The data model already supports it (per-prop `{f,v}[]`). Two store bugs destroy it:
- `applyCanvasTransform` (`store.ts:839`): `existing.length > 1 ? upsert : [{f:0,v}]` **collapses a
  single-keyframe prop to static**, wiping the keyframe — so dragging rotate erases scale's keyframe
  and vice-versa. Fix: upsert at the playhead frame whenever the clip is in keyframe mode (the prop
  already has ≥1 keyframe, or any prop on the clip is animated); only write static `[{f:0,v}]` when
  the prop has zero keyframes and the clip isn't animated.
- `toggleMasterKeyframe` (`store.ts:807-820`): `hasAny` deletes keyframes across ALL props if *any*
  one prop has a keyframe at frame `f`, so you can't complete a partial keyframe. Fix: master-add
  fills every prop missing a keyframe at `f` (capturing current interpolated value); only
  master-remove when the frame is fully keyed (or make the ◆ button add-missing, with explicit
  per-diamond delete for removal — `KeyframeLane` double-click already removes).
- Net: gizmo rotate+scale at one playhead position both persist as keyframes at the same frame.

### 1C. Media picker category filters
`VideoSourcePickerModal.tsx` only filters all/animation/upload; backend `list_sources`
(`yas_video_editor.py:619`) only returns `yas_animations` + video `yas_uploads`.
- Backend: widen `list_sources` (and its `kind` param) into buckets the user named — **Talking Head
  v2 clips**, **Final / stitched videos** (rows where `composition_status==ready`: th, th_v2,
  video_edit + ready poster animations), **Stills** (scene/intro/outro keyframe images), **Posters**,
  **Uploads**. Reuse the type/`composition_status` derivation already in `_library_item`
  (`yas_producer.py:525`) and the mode constants (`*_preview`, `*_scene`, etc.).
- Frontend: replace the 3 chips with a category filter (chips or a small dropdown) driving the
  server `kind`/`q` params (already supported). Widen `MediaBinEntry.kind` / `VideoSource.kind`
  (`types.ts:1439,1528`) and the `addToBin` cast + the backend `resolve_source_local` switch to the
  new kinds (image stills become `image` clips; finals are `composition`/`upload`).

### 1D. Multi-sticker upload + embedding, and user-addable categories
- Sticker/asset upload is single-file (`upload_asset` takes one `File`; `StickerPanel.tsx:61` &
  `Assets.tsx:297` read `files?.[0]`). Add `multiple` to the inputs and loop the **existing**
  embedding endpoint `api.producerAssets.upload` per file (reuse the multi-file loop + progress
  pattern already in `Assets.tsx` `UploadDialog` ~463-522). Every file flows through
  `_process_asset_bytes` → `gemini_embed` so all are embedded. (Optional: a batch endpoint; the loop
  is enough.)
- User-addable categories: the DB has **no CHECK** on `producer_assets.category` (verified) — only
  the app whitelist `ALLOWED_CATEGORIES` (`producer_assets.py:64`) blocks it. Relax it (accept
  free-text category, keep the known ones as suggestions) + a small "add category" affordance in the
  Assets UI and the editor's sticker/media filters.

**Critical files (Phase 1):** `frontend/src/lib/store.ts`, `.../video-editor/Timeline.tsx`,
`CanvasGizmo.tsx`, `KeyframeEditor.tsx`/`KeyframeLane.tsx`, `VideoSourcePickerModal.tsx`,
`MediaBinPanel.tsx`, `sidebar/StickerPanel.tsx`, `pages/Assets.tsx`, `lib/types.ts`, `lib/api.ts`;
backend `yas_video_editor.py` (`list_sources`, `resolve_source_local`), `routers/yas_producer.py`,
`routers/producer_assets.py`.

---

## PHASE 2 — AI edit engine (ANALYZE → PLAN → EXECUTE)

The backbone all 5 AI features share. The user's model: *"each edit must be planned before — a
general plan, then each step implemented properly; before planning, each frame is processed and the
sound transcribed."*

**Storage rule (verified gotcha):** `save_timeline` rebuilds `video_edit` from only
`{schema_version, project, tracks, clips, history, render}` — so AI analysis + job state must live at
the **sibling key `metadata.video_edit_ai`** (written via a `jsonb_set` helper like `_patch_render`),
or the next frontend autosave drops it. Autosave is last-write-wins, so the frontend **pauses
autosave while an AI job runs and refetches on completion**; the executor re-reads the doc
immediately before its single `save_timeline` write.

### 2A. Analysis — `backend/app/services/yas_video_edit_ai_analysis.py` (new)
Per source clip, cached under `metadata.video_edit_ai.analysis` keyed by `"{kind}:{source_id}"` +
size/mtime hash (recompute only when media changes):
- `resolve_source_local` → PySceneDetect (`ContentDetector(threshold=27.0)`) → sample ≤12 mid-scene
  JPEGs (≤720px) via the bundled ffmpeg (`imageio_ffmpeg`) — copy the helper pattern from
  `agents/website_ingest/phases/video.py` to stay decoupled from IG-ingest tables.
- Transcript: `transcribe()` (Whisper verbose_json → segment timestamps → integer frames). Fallback
  to Gemini-audio transcription if Whisper is blocked (OpenAI billing history).
- One `gemini_multimodal(images=[jpegs], json_schema=...)` call → per-scene visual summary, face
  rects, and a 3×3 safe-area occupancy grid (for pop-up placement). **Never** send the whole MP4
  (≈20 MB inline cap) — frames only.

### 2B. Op schema + executor — `yas_video_edit_ai_ops.py` (new)
Strict, LLM-emittable JSON list of typed operations that map 1:1 to deterministic mutations:
`set_clip_position`, `reorder_clips`, `add_transition`, `add_text_popup`, `add_sticker`,
`set_keyframes`, `set_filters`, `add_music`, `replace_voice`, `trim_clip`, `split_clip`, `mute_clip`.
Ops reference clips by id; frames are clip-local where relevant. The **executor owns all math the
LLM must not** — transition overlap ripple (shift later clip's `from` left by the *missing* overlap
so re-runs don't double-shift; ripple successors; clamp), ducking volume keyframes (computed from
transcript), keyframe sort/clamp/int-coercion, duration re-derive. Deterministic ids
(`clip_ai_{plan}_{i}`) for idempotent re-apply; per-op try/except; one persist via `save_timeline`.

### 2C. Planner / orchestrator — `yas_video_edit_ai.py` (new)
One LLM call per feature (shared planning core + per-feature system prompt) taking the analysis +
intent + available assets, returning ops via strict structured output. Grounded by a compact project
digest with a **whitelist of real clip ids** (model may only reference those). Gemini default
(flat op object — Gemini `response_schema` dislikes `anyOf`), gpt-4o alternative. Voice needs no LLM.

### 2D. Providers — `media_providers.py` (new)
`VoiceProvider` (ElevenLabs STS; clean 422 "needs ELEVENLABS_API_KEY" when absent) and
`MusicProvider` (`SunoMusicProvider` default behind `SUNO_API_KEY`; `LibraryMusicProvider` via the
producer-assets semantic-search service fn — works today, results use `source:{kind:"asset"}`).
Missing keys degrade gracefully with a clear message, never crash. Add optional
`ELEVENLABS_API_KEY` / `SUNO_API_KEY` to `config.py`.

### 2E. Endpoints + job tracking (`yas_producer.py`, respecting route-order)
`POST /video-editor/{id}/ai/analyze`, `POST /video-editor/{id}/ai/{popups|compose|edit|voice|music}`,
`GET /video-editor/{id}/ai/status`. Long work (analysis, ElevenLabs, Suno) runs as an async task
writing job state (`queued|running|done|failed` + message) to `metadata.video_edit_ai.job` via the
jsonb_set helper; Suno's submit-then-poll phase reuses the render-poller pattern (optional twin
worker). Frontend polls status, refetches the project on `done`.

---

## PHASE 3 — The 5 AI features (built on Phase 2, auto-apply)

UI: minimal — one **AI** section in the editor (a small toolbar/panel) with: **Compose**,
**AI Edit**, **Add Pop-up** (text field), **Voice**, **Music** (Suno-generate default / pick-from-
library toggle), plus a tiny progress indicator. Auto-apply; undo reverts.
1. **Add Pop-ups** — user types text → planner picks best frame range + safe-area x/y (avoids
   faces/text via the occupancy grid) → emits a `text` clip with tasteful fade/scale keyframes.
2. **Compose** — orders media-bin items + chooses transitions (preset+duration) and lays them out
   with correct overlaps (executor does ripple).
3. **AI Edit** — holistic: may combine reorder + transitions + captions/pop-ups + color filters +
   music in one plan.
4. **Voice changer** — extract the clip's audio for its trim window (frames/30 → seconds) → ElevenLabs
   STS → import as an `audioFile` upload (like `import_audio`) → add an audio clip at the same
   `from`/`duration` → mute the original. Timing preserved.
5. **Music** — Suno-generate (planner picks style/mood from analysis) or library-pick → add an
   `audioFile` audio clip with ducking volume keyframes (fade in/out, lower under speech).

**New backend files:** `yas_video_edit_ai_analysis.py`, `yas_video_edit_ai_ops.py`,
`yas_video_edit_ai.py`, `media_providers.py` (+ optional Suno poller). **Edited:**
`routers/yas_producer.py`, `services/yas_video_editor.py`, `config.py`. **Frontend:** a new AI panel
component under `components/yas/video-editor/`, plus `lib/api.ts`, `lib/types.ts`, store hooks for
job polling / autosave-pause / refetch. **Remotion: no changes.**

---

## Verification (servers stay up — backend `AUTO_RELOAD=1`, Vite HMR; never restart Remotion)

**Phase 1 (manual, in the running app):** undo/redo via Ctrl+Z/Y and buttons across drag/trim/
keyframe/gizmo edits (one gesture = one step); on the canvas, rotate AND scale at the same playhead
frame and confirm both persist as keyframes (inspect clip JSON) and animate in the preview; media
picker shows the new category buckets and adding from each drops a working clip; selecting multiple
sticker files uploads them all and each shows `embedded: true`; add a custom asset category and see
it filterable.
**Phase 2/3 (per feature):** `POST /ai/analyze` returns scenes + transcript + safe-areas cached at
`metadata.video_edit_ai`; **Add Pop-up** places readable text off faces at a sensible moment;
**Compose** on ≥3 bin items yields a sensible order + transitions that render; **AI Edit** produces a
coherent multi-op plan; each AI action is fully revertible with one undo; **Music** library-pick adds
a ducked bed today (Suno path returns the graceful "needs key" message until `SUNO_API_KEY` is set);
**Voice** returns the graceful "needs key" message until `ELEVENLABS_API_KEY` is set, then swaps
voice while keeping timing. Confirm an AI job + a frontend autosave don't clobber each other
(autosave paused during job, refetch on done) and that `metadata.video_edit_ai` survives subsequent
autosaves. Export a project end-to-end after AI edits to confirm the Remotion render is unaffected.
