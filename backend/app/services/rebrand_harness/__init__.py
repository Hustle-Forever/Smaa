"""The Rebrand harness — a rebrand described as a RECIPE of elements.

The hard-coded Rebrand (tag checkpoint-rebrand-hardcoded) knew one pattern
path, fixed prompt clauses and one run per clip. The harness turns that into
data: a recipe lists elements (pattern, logo, text, icons, brand shapes), each
with where it goes and how it enters; Gemini Omni draws them all from a TARGET
frame we compose; guards fix what Omni must never get wrong (the speaker in
front of every element, the face never covered, no frosted veil); and every
run is a VERSION the user can edit and retry.

Modules (built up milestone by milestone, see the plan on branch
rebrand-harness):
  recipe      the contract — pydantic models, defaults, hashing, presets,
              the mapping to and from the legacy options
  elements/   one module per element kind: pattern, and icon (which icons a
              scene shows, on which spoken words, where and in what
              colours — from words, speaker_track and director; drawn by
              icon_render from icon_catalog's glyphs)
  compose     the TARGET frame Omni is shown: layers behind / in front of
              the speaker
  prompt      what Omni is told: the opening + one clause per element
  guards      what is fixed after Omni: speaker in front, entrance veil, bars
              — and our icons, stamped by the same pass behind the speaker
  stages      step keys and the run's manifest (what a retry may reuse)
  runner      the harness run of one scene, chunk by chunk (and, for a
              recipe with icons, each part's pre-stamp and clean clips)
  stray       whether Omni drew a symbol of its own, on a run that asks it
              for nothing but the patterns (guards.checks_strays; the
              opt-in clean-up, guards.stray "clean", takes such marks away)
  studio      the free side: a scene prepared once, any recipe previewed
              (and its icons, to the exact frame the run will make)
  base        a finished version's own parts and their settled first frames
              (what the studio shows for it, and what building on it starts
              from), its lineage and effective layers, the hold that keeps
              it exactly as it was when more is added on top, and — with our
              icons — its own pass, to make it again with other icons

The in-house engine (engine "house": every new version since 2026-09-29, no
Omni, $0) lays our own graphics over the untouched footage:
  mattes      the speaker on every frame of a scene, made once (local MODNet)
  camera      how the camera moves over the scene (a similarity per frame)
  plate       one frame's graphics — the patterns in their motion, moved by
              the camera, our icons, cut by the speaker — the ONE function
              the run and the studio draw with
  house       the scene's plate spec, its render (ffmpeg lays the plate over
              the source in one encode, the sound copied), stills, and the
              run's branch for a house recipe

Omni is switched off for new work (2026-09-30; recipe.omni_on, REBRAND_OMNI
off unless set to on): the recipe routes and the pipeline refuse it, so the
Omni modules above (compose's target, prompt, runner, stray, the Omni side of
guards) only serve the versions Omni made — shown, rated, added on top of and
made again in house — and the golden lock. No paid call is reachable from the
in-house engine, the studio or the icon modules but the two the owner asks
for on purpose: the icon director (one per scene, cached) and "Generate with
AI" (tests/test_rebrand_omni_off.py holds that).
"""
