# Create Poster — variant diversity & image-anchored Explore

How the Create-Poster grid produces **four genuinely different** posters, and how
**Explore** produces **close variations of a chosen one**. This documents the
2026-06-13 rewrite that fixed "all 4 variants look the same" and "Piscine is
always the same pool."

## The bug it replaced

The old pipeline expanded the topic into **one** scene description (Stage 1, gpt-4o)
and rendered that single prompt **4×** on Gemini. The only difference between the
four was Gemini's render noise — so they looked like siblings of one idea. Worse,
the Stage-1 system prompt **hardcoded literal examples** (`Piscine → swimming pool`,
`Logic Game → arcade cabinet`, floating `cursor` / `question mark` props), so the
model anchored to the same reading every time. Explore reused the parent prompt
verbatim, so "more like this" was just "the same again."

## The pipeline now

```
POST /poster/generate {topic, refs?, image_usage?}
   │
   ├─ Stage 1  _poster_concepts(topic, 4)
   │     gpt-4o, response_format=json_object, POSTER_CONCEPTS_SYSTEM
   │     → { "concepts": [ {summary, prompt} × 4 ] }      ← 4 DISTINCT ideas
   │     → parse_concepts → dedupe_concepts → top-up if < 4
   │     avoid-list seeded from the last ~8 stored `concept` summaries
   │
   └─ Stage 2  _run_stage2_batch(specs)                    ← one render per concept
         4 concurrent Gemini calls, each with its OWN concept prompt
         → 4 local records (yas-output/), shared variant_set_id
```

```
POST /poster/variants {parent_id}            ("Explore" — variations of the chosen)
   └─ _run_stage2_batch(specs)               ← Stage 1 SKIPPED
         each spec = parent.prompt_used
                   + VARIATION_NUDGES[i]      ← one small directed change each
                   + parent PNG as a Gemini reference part (VARIATION_REF_INSTRUCTION)
         → 4 renders that stay NEAR the chosen poster but differ from each other
```

This mirrors Midjourney: the initial grid is diverse; the **V** buttons give
subtle variations of the picked image.

## The four moving parts

### 1. Distinct concepts (`POSTER_CONCEPTS_SYSTEM` + `build_concepts_user_message`)
One gpt-4o call returns **N concepts at once** under a hard **[DIVERSITY MANDATE]**:
they must differ across explicit axes — **hero subject, interpretation angle**
(literal / metaphorical / environmental / action / abstract), **composition/layout,
dominant palette pairing, camera angle & energy**. Seeing all four in one response
lets the model deliberately spread them instead of drifting to one idea.

### 2. De-hardcoded, free-within-theme (`VOXEL_SCENE_RULES`)
The brand/scene rules are now one shared block with **no literal examples**. What's
kept is the *theme*, not a *script*: voxel diorama, the retro gaming-&-tech universe,
the strict 5-colour palette (`#00EBFF #730FFF #FF5A3C #414141 #FFFFFF`), bold
background splits, and the `[TEXT TO RENDER]` contract. The model is free to
interpret any topic however it likes inside that theme. A regression test
(`test_no_hardcoded_literal_examples`) fails if the old examples ever creep back.

### 3. Image-anchored Explore (`VARIATION_REF_INSTRUCTION`, `VARIATION_NUDGES`)
Explore keeps the parent's concept text and **sends the chosen PNG to Gemini as a
reference part**, told to produce a *close variation* (same concept/composition/
palette/text — change only secondary details). Each of the four children gets a
**distinct micro-nudge** (camera angle / lighting / prop arrangement / crop) so they
differ from each other while staying near the parent. Children carry
`parent_generation_id` and `variation_of = parent.id`.

### 4. Durable anti-repetition (`concept` field + `_poster_recent_summaries`)
Every poster record stores a short `concept` summary in the local store
(`yas-output/index.json`). The next batch feeds the last ~8 summaries into Stage 1
as an **avoid-list**, so successive grids (and "Generate a new set") don't repeat
earlier concepts. This replaced the old in-memory `_voxel_history` (full prompts,
lost on restart) with concept signatures that persist.

## Robustness

- `parse_concepts` tolerates fenced ```json blocks, prose-wrapped JSON, bare lists,
  and alternate key names — and **never raises** (garbage → `[]`).
- `dedupe_concepts` drops near-duplicate summaries (case/punctuation-insensitive).
- If gpt-4o returns fewer than 4 distinct concepts, `_poster_concepts` **tops up**
  with extra calls (passing the accumulated summaries as the avoid-list), up to 3
  attempts. If it still falls short, the batch renders what it has and **refunds**
  the unused hourly-cap slots.

## Image-engine toggle (Nano Banana ↔ OpenAI)

Stage 1 (the 4 distinct concepts) is engine-agnostic. **Stage 2 — the actual image
render — is selectable** via a toggle in the composer (`image_model`):

| Engine | Model | Logo |
|---|---|---|
| **Nano Banana** (default) | `gemini-3-pro-image` | Baked in-render: the white logo PNG is sent as an image part + a logo-firewall instruction. |
| **OpenAI** | `gpt-image-1` (dall·e-3 fallback) | The prompt tells the model **not** to draw a logo and to leave a clean top-right corner; the real white logo is **composited on afterward** (`poster_logo.composite_logo_png`) so it stays crisp and consistently placed. |

`gpt-image-1` is OpenAI's current flagship image model — notably better text rendering
than DALL·E 3 (matters for poster headlines) and it accepts input images, so:

- **Generate** (no refs) → `openai_image()` (text→image) → composite logo.
- **Generate with refs** → `openai_image_edit(image_paths=[refs])` → composite logo.
- **Explore** → `openai_image_edit(image_paths=[chosen, refs])` — edits the chosen
  poster into a close variation → composite logo.

Both engines flow through the same `_run_stage2_batch`, which now meters **both** the
Vertex and OpenAI cost (so the per-image cost split is correct whichever engine ran).
The chosen engine is persisted on each record's `image_model`. Explore uses whatever
engine is toggled at click time.

Brand rules for the OpenAI path live in `OPENAI_RENDER_PREAMBLE` (palette, voxel
render, typography, English-only, "do not draw a logo") — the gpt-image-1 equivalent
of Gemini's `GEMINI_STAGE2_PROMPT` system instruction.

## Poster styles (Voxel · Pattern · Pop-up)

The look is selectable via a **Style** toggle in the composer (`style`). It is
orthogonal to both the diversity pipeline (you still get 4 genuinely different
concepts) and the engine toggle (each style renders on Nano Banana *or* OpenAI).
`poster_styles.py` is a small registry mirroring the Create-Animation one
(`yas_animation_styles.py`):

| Style | Look | Stage-1 / Stage-2 prompts |
|---|---|---|
| **Voxel** (default) | Retro-voxel toy diorama | References the original `poster_prompts.py` symbols **by identity** → byte-for-byte the v1 pipeline. The diversity rewrite and the engine toggle above are untouched. |
| **Pattern** | One bold full-bleed brand pattern (Ghutra / Map) on a two-color split | Poster-tuned `PATTERN_*` prompts. |
| **Pop-up** | Code-centric OS-window event card (●●● bar, `<tag/>` titles) | Poster-tuned `POPUP_*` prompts. |

Each `PosterStyle` bundles the three prompt surfaces the pipeline feeds in —
`concepts_system` (Stage-1 gpt-4o), `gemini_stage2_system` (Nano Banana),
`openai_render_preamble` (gpt-image-1) — plus per-style `variation_nudges`. The
router only changes *which strings it feeds*; the two-stage shape is unchanged.
`get_poster_style()` falls back to voxel for unknown/None/legacy keys, so old
records and typos never break generation.

**A poster carries the FULL message.** The Create-Animation popup/pattern presets are
short intro/outro *title cards* (one headline). A poster is not — so the Pattern and
Pop-up presets embed `POSTER_TEXT_CONTRACT`, an info-rich `[TEXT TO RENDER]` block
with `HEADLINE / SUBHEAD / DATE_TIME / LOCATION / DETAILS / CTA` (render every field
the topic provides; omit only what's truly absent). For **Pop-up** specifically, that
information lives *inside* the OS window(s) and the **CTA becomes a highlighted
button** — a real event card. This is why posters don't reuse the animation prompts.

**Logo** works exactly as in the engine toggle, per style: Nano Banana bakes it via
the logo image part + a per-style firewall; OpenAI is told **not** to draw it and
`poster_logo.composite_logo_png` stamps it top-right afterward.

**Explore inherits the parent's style.** "More like this" reads the chosen poster's
persisted `style` and renders the 4 variations in that same style, using that style's
`variation_nudges`.

The styles are guarded by `tests/test_poster_styles.py` (voxel byte-identity, safe
fallback, the info-rich Pop-up contract, per-engine logo handling, and a no-"voxel"
-bleed check on the flat-graphic styles).

## Files

| File | Role |
|---|---|
| `backend/app/services/poster_prompts.py` | `VOXEL_SCENE_RULES`, `POSTER_CONCEPTS_SYSTEM`, `build_concepts_user_message`, `parse_concepts`, `dedupe_concepts`, `VARIATION_REF_INSTRUCTION`, `VARIATION_NUDGES`, `build_variation_user_message`, `GEMINI_STAGE2_PROMPT`, `OPENAI_RENDER_PREAMBLE` |
| `backend/app/services/poster_styles.py` | style registry: `PosterStyle`, `POSTER_STYLES` (voxel/pattern/popup), `POSTER_BRAND_LAW`, info-rich `POSTER_TEXT_CONTRACT`, `get_poster_style` |
| `backend/app/routers/yas_producer.py` | `_poster_concepts(…, style)`, spec-based `_run_stage2_batch` / `_single_stage2` (per-style Stage-2), `_render_openai(…, style)`, `_norm_style`, `poster_generate`, `poster_variants` (inherits parent style) |
| `backend/app/services/poster_logo.py` | pure PIL logo compositor (OpenAI path) |
| `backend/app/services/yas_local_store.py` | persists `concept` + `variation_of` + `image_model` + `style` |
| `backend/tests/test_poster_diversity.py` | 26 pure tests (no DB/creds): de-hardcode guard, diversity mandate, JSON parsing, dedupe, Explore surface, engine preamble + logo composite |
| `backend/tests/test_poster_styles.py` | 14 pure tests: voxel byte-identity, safe fallback, info-rich Pop-up contract, per-engine logo handling, no-voxel-bleed |
| `frontend/src/lib/types.ts` · `PosterCreator.tsx` | `YasPosterStyle` type + `style` on the request; real **Style** pill toggle (Voxel / Pattern / Pop-up) in the composer |

## Run the tests

```bash
cd backend && python tests/test_poster_diversity.py     # standalone, no pytest needed
# or, if pytest is installed:
cd backend && python -m pytest tests/ -q
```

## Cost & limits (unchanged)

Initial grid = 1 gpt-4o concepts call (+ top-ups only if needed) + 4 Gemini renders.
Explore = 0 Stage-1 + 4 Gemini renders. Both reserve 4 of the 20/hour cap; a short
concept batch refunds the unused slots.
