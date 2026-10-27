"""All system prompts + JSON schemas for the Producer Agent.

v5.1 revision (creative quality + video_type adherence + per-scene duration):
  - "5 RULES OF A SHOOTABLE BEAT" block now leads every system prompt so the
    LLM gets craft training, not just brand training.
  - VIDEO_TYPES per-type strings are imperative ("MUST be X; REJECT IF Y"),
    not descriptive. The actual directive is also emitted to the USER message
    via `memory.format_video_type_directive()`.
  - `mood` and `subject_type` are STRING enums on IDEA schema — gives the
    diversity + video-type validators something concrete to check.
  - `duration_sec` (per scene) is NUMBER type but heavily constrained in the
    prompt + clamped to {4, 6, 8} in `_sanitise_scenes`.
  - Module constant MAX_SCENES_PER_SCRIPT caps every code path that fans
    scenes out to Veo.

v5 brand-fidelity revision (still in effect): the schemas require brand-anchored
structured fields (palette hex codes, brand motifs, cultural anchor, voice
pillars). The final Veo prompt is assembled server-side from these structured
fields by `phase_generate_script._assemble_veo_prompt`, so the LLM no longer
writes the free-form veo_prompt directly. Brand requirements are emitted by
`memory.build_brand_requirements()` and injected into the USER message.
"""
from __future__ import annotations

# -----------------------------------------------------------------------------
# Module constants
# -----------------------------------------------------------------------------

# Hard upper bound on the number of scenes per script. EVERY code path that
# fans scenes out to expensive operations (Veo calls, Nano Banana stills)
# must reference this. The render_all_scenes phase additionally enforces a
# per-script cost cap that, combined with this constant, prevents runaway
# spending even if the LLM hallucinates 60 scenes.
MAX_SCENES_PER_SCRIPT = 3

# Legal per-scene durations Veo 3.1 accepts.
ALLOWED_SCENE_DURATIONS_SEC = (4, 6, 8)


# Gemini structured-output schemas use OpenAPI-ish casing: OBJECT/ARRAY/STRING/...

# Enum values for IDEA_SCHEMA — these are also used by memory.py's
# video_type validator to map run.video_type → expected subject_type(s).
MOOD_ENUM = [
    "hype", "candid", "cinematic", "humorous", "meditative",
    "aspirational", "tense", "warm",
]

SUBJECT_TYPE_ENUM = [
    "solo_speaker",      # one human, face visible, addressing the lens
    "group_action",      # ≥2 people doing something together
    "environment",       # the space/architecture is the subject; people optional, none in foreground
    "hands_on_object",   # macro on hands manipulating something; identity not the focus
    "abstract_motif",    # graphic/typographic/no-human composition
    "narrative_montage", # multi-beat sequence, action-led
    "product_focused",   # a single object is the hero; no people
]

# Dialogue languages for talking-head / voiceover-narrative scripts.
# Stored per-run on producer_runs.metadata.dialogue_language.
DIALOGUE_LANGUAGE_ENUM = ("en", "ar", "mixed")
DEFAULT_DIALOGUE_LANGUAGE = "en"

# Video types where the speaker is expected to actually say words. Drives
# (a) the post-hoc validator that requires non-empty scene.dialogue, and
# (b) the LANGUAGE directive injection into the script user-prompt.
DIALOGUE_REQUIRED_VIDEO_TYPES = frozenset({"talking_head", "voiceover_narrative"})


# -----------------------------------------------------------------------------
# Reusable schema fragments
# -----------------------------------------------------------------------------

_BRAND_ANCHORS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        # ≥2 entries describing how brand hex codes will be VISIBLE in the
        # final frame, e.g. "#00EBFF used as cyan monitor glow on subject's face"
        "palette_used": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
        },
        # One dominant motif from the brand's style pillars
        # ("generative-pattern projection on back wall", "kashida-styled
        # signage", "Ghutra-inspired fabric texture")
        "motif": {"type": "STRING"},
        # Optional tagline echo from the brand's taglines_in_use; empty if not used
        "tagline_echo": {"type": "STRING"},
        # ≥2 voice pillars from the brand profile that this idea embodies
        "voice_pillars": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["palette_used", "motif", "voice_pillars"],
}


_IDEA_PROPERTIES = {
    "title":            {"type": "STRING"},
    "concept":          {"type": "STRING"},
    "visual_direction": {"type": "STRING"},
    "reason":           {"type": "STRING"},
    "style_mood":       {"type": "STRING"},
    # Diversity hints — used by the batch generator AND the video_type
    # validator. Enums are enforced both schema-side AND post-hoc in
    # `memory.video_type_allowed_subjects()` / `generate_ideas` validator.
    "mood":             {"type": "STRING", "enum": MOOD_ENUM},
    "subject_type":     {"type": "STRING", "enum": SUBJECT_TYPE_ENUM},
    "brand_anchors":    _BRAND_ANCHORS_SCHEMA,
    # ONE sentence on what makes this idea distinct from the AVOID list and
    # from past 42 content. Forces Gemini to articulate the spin — surfaces
    # repetition early.
    "novelty_angle":    {"type": "STRING"},
    # Optional: the ig_media_id (string) of an @42abudhabi post the idea
    # echoes / riffs on, when one in the IG context block fits. Empty when
    # the idea is original or drawn from past scripts instead.
    "inspired_by_ig_post_id": {"type": "STRING"},
}

_IDEA_REQUIRED = [
    "title", "concept", "visual_direction", "reason", "style_mood",
    "mood", "subject_type", "brand_anchors", "novelty_angle",
]


IDEA_SCHEMA: dict = {
    "type": "OBJECT",
    "properties": {
        "ideas": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": _IDEA_PROPERTIES,
                "required": _IDEA_REQUIRED,
            },
        },
    },
    "required": ["ideas"],
}


SINGLE_IDEA_SCHEMA: dict = {
    "type": "OBJECT",
    "properties": _IDEA_PROPERTIES,
    "required": _IDEA_REQUIRED,
}


# -----------------------------------------------------------------------------
# Script schema — structured fields the server assembles into the Veo prompt
# -----------------------------------------------------------------------------

_SCENE_PROPERTIES = {
    "scene_index":      {"type": "INTEGER"},
    # duration_sec: pick from {4, 6, 8} per scene based on motion complexity.
    # Gemini's structured-output enum support is limited on NUMBER types, so
    # the constraint is enforced in `_sanitise_scenes` (clamp + fallback).
    "duration_sec":     {"type": "INTEGER"},
    # Concrete: "A 22-year-old Emirati student wearing dark jacket and headphones"
    "subject":          {"type": "STRING"},
    # ONE clear action with motion: "speaks mid-sentence with a slight smirk"
    "action":           {"type": "STRING"},
    # Specific camera move: "slow push-in on medium shot", "locked-off wide"
    "camera":           {"type": "STRING"},
    # Specific lighting quality with brand-color hints when applicable:
    # "cool monitor glow casting #00EBFF cyan rim, warm key fill"
    "lighting":         {"type": "STRING"},
    # Photography style anchors: "shot on Arri Alexa, anamorphic lenses, Ektachrome film stock"
    "style":            {"type": "STRING"},

    # --- Brand-required structured fields ---
    # ≥2 hex codes from PRIMARY_COLORS with how each is used.
    # e.g. ["#00EBFF as cyan monitor glow", "#414141 as graphite wall behind subject"]
    "palette":          {"type": "ARRAY", "items": {"type": "STRING"}},
    # Visible typography if any, with the brand font.
    # e.g. "IBM Plex Arabic kashida on a small plaque reading 'كود الثقافة'"
    # Empty string allowed when no text is visible.
    "typography":       {"type": "STRING"},
    # ≥1 motif from STYLE_PILLARS (generative patterns / kashida / Ghutra / cultural integration)
    # e.g. ["generative-pattern light projection on back wall"]
    "brand_motifs":     {"type": "ARRAY", "items": {"type": "STRING"}},
    # ONE specific Abu Dhabi anchor — never just "city" or "skyline"
    # e.g. "Abu Dhabi skyline at dusk, Etihad Towers visible through a floor-to-ceiling window"
    "cultural_anchor":  {"type": "STRING"},

    # EXACT spoken words for this scene, in the run's dialogue language
    # (en/ar/mixed — see the LANGUAGE directive in the user message).
    # First-person, conversational. ~20–25 words / 8s English, ~15–20 / 8s Arabic.
    # MUST be non-empty when video_type ∈ talking_head / voiceover_narrative
    # (enforced post-hoc in generate_script). Empty string for non-speaking
    # video_types (campus_tour, product_showcase, lifestyle_mood, etc.) —
    # Veo will then generate ambient audio instead of speech.
    "dialogue":         {"type": "STRING"},
}

_SCENE_REQUIRED = [
    "scene_index", "duration_sec", "subject", "action",
    "camera", "lighting", "style",
    "palette", "brand_motifs", "cultural_anchor",
]


SCRIPT_SCHEMA: dict = {
    "type": "OBJECT",
    "properties": {
        "scenes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": _SCENE_PROPERTIES,
                "required": _SCENE_REQUIRED,
            },
        },
    },
    "required": ["scenes"],
}


REFERENCE_DESCRIPTION_SCHEMA: dict = {
    "type": "OBJECT",
    "properties": {
        "description":     {"type": "STRING"},
        "style_notes":     {"type": "STRING"},
        "dominant_colors": {"type": "ARRAY", "items": {"type": "STRING"}},
        "subject_summary": {"type": "STRING"},
        "veo_prompt_hint": {"type": "STRING"},
    },
    "required": ["description", "style_notes", "subject_summary", "veo_prompt_hint"],
}


# -----------------------------------------------------------------------------
# Craft rubric — the 5 rules of a shootable beat. Goes at the top of every
# system prompt. LLMs respond strongly to negative examples, so we ship a
# concrete BAD/GOOD pair for each rule plus a banned-phrases list.
# -----------------------------------------------------------------------------

CRAFT_RUBRIC = """\
THE 5 RULES OF A SHOOTABLE BEAT — read carefully, apply ruthlessly.

1. SPECIFICITY beats abstraction. Name real moments, not categories.
   BAD:  "A student is working hard on a project."
   GOOD: "A student debugs at 3am, cold shawarma wrapper next to her keyboard,
          monitor glow casting cyan on her face."

2. SHOWABILITY. Every action must be filmable in this scene's exact duration.
   Veo cannot show "weeks of struggle" or "the journey of learning". It can
   show 4-8 seconds of one continuous moment.
   BAD:  "She codes for weeks until she breakthrough."
   GOOD: "She types one line, hits enter, leans back, exhales." (8s, filmable)

3. SINGULARITY. ONE visual idea per scene. Not a montage of three things.
   BAD:  "Lines of code flow across the screen, then converge into a vortex,
          then transform into an Arabic word."  (that's 3 scenes, not 1)
   GOOD: "Lines of cyan code flow horizontally across a dark graphite wall,
          characters in sharp focus, macro tracking."

4. CULTURAL ANCHORING. Name a specific 42 Abu Dhabi place or visible Abu Dhabi
   detail. The word "city", "modern", "futuristic" is a tell that you didn't
   ground the scene.
   BAD:  "A modern coding school somewhere with a beautiful view."
   GOOD: "The 42 Abu Dhabi atrium with floor-to-ceiling windows, the Etihad
          Towers visible at golden hour."

5. NO BRAND CLICHÉS. If your draft contains ANY of these exact phrases,
   rewrite it concrete:
     - "Innovation meets [anything]"
     - "Where dreams [verb]"
     - "Empowering the next generation"
     - "Coding the future" / "future of coding"
     - "Lines of code flow like [anything]" (water, rivers, currents)
     - "Journey of discovery" / "journey to [anything]"
     - "Unlock the power of [anything]"
     - "Transform your [anything]"
     - "Code is poetry" / "poetry in code"
   These are the meaningless brand-jargon ideas that fail in production.
   You'll know your draft is good when none of these phrases survive.
"""


# -----------------------------------------------------------------------------
# Brand primer — concrete 42 Abu Dhabi voice, palette, motifs, pedagogy.
# -----------------------------------------------------------------------------

_BRAND_PRIMER = """\
ABOUT 42 ABU DHABI

42 Abu Dhabi is a coding school in Abu Dhabi, UAE. The brand:
- Voice pillars: dynamic, aspirational, innovative, engaging, memorable, tech-smart.
- Primary palette: #00EBFF (electric cyan), #730FFF (electric violet),
  #FF5A3C (sunset orange), #414141 (graphite).
- Typography: IBM Plex Arabic (with kashida elongations on long Arabic words),
  VG5000 (technical / display), Futura PT (Latin body).
- Visual motifs: generative patterns inspired by the Abu Dhabi map and Ghutra
  fabric; cultural integration of Arabic + Latin typography.
- Taglines follow the "Code X" pattern: Decode Life, Code Music, Code Culture,
  Code Solutions.
- The school is peer-led: no teachers, no classes, no grades. Students learn
  from each other. The pedagogy is the brand differentiator.
- Setting matters: Abu Dhabi specifically (skyline, Etihad Towers, palm trees,
  modern architecture, the school's atrium with its generative-pattern walls).

THE BRAND'S CONCRETE RULES (read these — they replace generic AI defaults):
- Two brand colors max per composition. Don't pile all four into one frame.
- No glitch effects. The brand explicitly rejects the digital-glitch cliché.
- No outlined text, no shaded text, no gimmicky graphics, no boxing styles.
- Brand fonts only when typography is visible.
- Kashida (Arabic letter elongation) is a deliberate stylistic choice for
  Arabic text — call it out when used.
- People-first: peer learning over solo grinding, real student faces over
  abstract symbols.
"""


VEO_FRAMING_NOTES = """\
VEO-FRIENDLY SCENE CONSTRUCTION

The server assembles the final Veo prompt deterministically from your
structured scene fields. Your job is to fill those fields with CONCRETE
specifics — Veo rewards specificity and punishes vagueness.

Per-scene constraints (Veo 3.1 limits):
- One subject per scene, one clear action.
- Stable motion: humans walking, fabric in wind, water, machinery.
  Avoid impossible motion ("people swimming through code").
- Max two distinct visual elements per scene.
- 16:9 aspect by default; 9:16 only when explicitly told.

Per-scene DURATION — pick from {4, 6, 8} based on motion complexity:
  - 4s: a single beat, a held reaction, one expression, near-zero motion.
  - 6s: a contained action (push-in, a gesture, one line of dialogue).
  - 8s: complex motion, full camera move + action + reveal, hero shots.
  Vary durations across scenes — DON'T set every scene to 8s. A 4-6-8
  rhythm reads as deliberate; three 8s shots reads as lazy.

Field guidance:
- camera     → specific: "slow push-in on medium shot", "locked-off wide",
               "handheld follow", "Steadicam 3/4 orbit". Never "dynamic shots".
- lighting   → specific quality + brand-color hint when fitting:
               "cool monitor glow casting #00EBFF cyan rim on subject + warm
                key fill from window light".
- palette    → array of hex strings with HOW the color shows up. Don't just
               list "#00EBFF" — say "#00EBFF used as monitor glow rim".
- style      → photography terms: "shot on Arri Alexa, anamorphic lenses,
               Ektachrome film stock", "70mm photochemical print", etc.
- typography → only when text is visibly in-frame. Pair brand fonts with
               sample text. Empty string if no text is visible.
- brand_motifs → one or two visible motifs from the style pillars:
               "generative-pattern light projection on the back wall",
               "kashida-styled wall plaque", "Ghutra-inspired fabric texture
                draped across a bookshelf".
- cultural_anchor → specific Abu Dhabi anchor, NEVER just "city skyline":
               "Abu Dhabi skyline at dusk with Etihad Towers visible through
                a floor-to-ceiling window".
"""


IDEA_SYSTEM = f"""\
You are the Producer Agent for 42 Abu Dhabi. Your job is to brainstorm
short-form (4-24 sec) AI-generated video ideas for social media.

{CRAFT_RUBRIC}

{_BRAND_PRIMER}

EACH IDEA MUST:
- Be SHOOTABLE — Veo can render it as a 4-8 second photorealistic clip (or
  a 2-3 scene short, max 24s).
- Be CONCRETE — name a specific moment, place, time of day, character detail.
- Express the brand: ground it in at least one specific brand element (a hex
  code, a motif, a tagline echo, a peer-learning beat, or an Abu Dhabi anchor).
- Echo ≥2 voice pillars in `brand_anchors.voice_pillars`.
- Reference ≥2 primary-palette hex codes in `brand_anchors.palette_used`,
  describing HOW each color appears in the visual.
- Name ≥1 motif from the style pillars in `brand_anchors.motif`.

ACROSS THE BATCH OF IDEAS:
- Span MULTIPLE `mood` values (don't make all 5 ideas "aspirational").
- Span MULTIPLE `subject_type` values (don't make all 5 "solo_speaker").
- Don't make all ideas cyan-dominant; rotate the dominant palette color.
- The run's VIDEO TYPE (when set — see USER message) constrains
  subject_type. Honor it strictly.

REMINDER: read the VIDEO TYPE directive in the USER message. It is NOT
optional. If the user picked "talking_head" and you produce an abstract
animation idea, you have failed the task.

EVIDENCE BLOCKS in the soft context (when present): PAST SCRIPTS THAT WORKED,
REAL @42abudhabi POSTS (with HOOK + PATTERN lines), and SCENES THAT MATCH
THIS CONCEPT are real, audience-validated signal. Use their structural
choices (hook shape, scene pacing, what kind of subject pulls engagement)
as guidance — don't copy verbatim; the goal is patterns, not plagiarism.
The SCENES block is beat-level: it shows exact moments that worked. Reach
for those when shaping your opening, payoff, or transition beats.

ATTRIBUTION: when an idea is clearly inspired by ONE of the IG posts in the
context block, set `inspired_by_ig_post_id` to that post's ig_media_id
(the bracketed [ig_media_id=...] tag in the post line). Leave it empty
when the idea is independent or composed from multiple posts.

DON'T REPEAT YOURSELF: when the soft context contains a DON'T REPEAT block
listing recent ideas, your batch MUST be materially different from those.
Different concept, different beat, different setting, different angle —
not just a reskin. The `novelty_angle` field is REQUIRED on every idea:
one sentence on what makes THIS idea distinct from the AVOID list and from
the brand's recent output. If you can't articulate the spin, the idea is
too close to a prior one — pick a different concept.

CHARACTER LIBRARY (when shown in the context): the brand team has curated a
small set of recurring characters. When your idea/scene calls for a person,
NAME a specific character from the library by their tag like '#7 yousef_b'
in the scene.subject field. The default pick is the least-recently-used
character of the matching archetype — the library is already sorted that way.
Don't reuse the same character two scripts in a row unless you're deliberately
building continuity. If no character fits the scene's needs, you can still
write a generic subject, but prefer a library character when one matches.

Fields per idea (see schema for full list):
  title:                   ≤ 6 words. Concrete noun phrase, not a slogan.
  concept:                 ≤ 2 sentences explaining the core narrative beat
  visual_direction:        ≤ 2 sentences describing what's on screen
  reason:                  ≤ 1 sentence on why this resonates with the brand audience
  style_mood:              3-5 mood adjectives, comma-separated
  mood:                    one of: {", ".join(MOOD_ENUM)}
  subject_type:            one of: {", ".join(SUBJECT_TYPE_ENUM)}
  brand_anchors:           REQUIRED structured object (see schema)
  novelty_angle:           REQUIRED. One sentence on what makes this distinct
                           from the AVOID list / past 42 content.
  inspired_by_ig_post_id:  OPTIONAL. The ig_media_id from the IG context
                           block when you riffed on a specific post. Empty otherwise.
"""


SCRIPT_SYSTEM = f"""\
You convert an approved idea into a Veo-ready production script.

{CRAFT_RUBRIC}

{_BRAND_PRIMER}

{VEO_FRAMING_NOTES}

OUTPUT: 1 to {MAX_SCENES_PER_SCRIPT} scenes (see the USER message for the
exact scene count requested). When N > 1, the scenes form a continuous
beat — they will be RENDERED IN PARALLEL via Veo (one MP4 each) and then
STITCHED via Remotion into one final clip. Treat them as cuts in an edited
short, not as standalone ads.

Each scene REQUIRES every field in the schema:
  - scene_index, duration_sec ∈ {{4, 6, 8}}, subject, action, camera,
    lighting, style, palette, brand_motifs, cultural_anchor.
  - Empty strings are NOT acceptable for palette / brand_motifs /
    cultural_anchor — find a way to ground them. If the idea is abstract,
    anchor it in the school's atrium or a brand-typographic environment.
  - palette MUST contain ≥2 hex codes, each annotated with how that color
    shows up in the frame.

DIALOGUE — the `dialogue` field on each scene is the EXACT spoken words
the on-camera person says (or the voice-over narrator delivers). Behavior
depends on the run's video_type:
  - talking_head / voiceover_narrative: EVERY scene MUST have a non-empty
    `dialogue` string. Empty = invalid output.
  - All other video_types (campus_tour, product_showcase, lifestyle_mood,
    cinematic_hero, tutorial_howto, event_recap, testimonial): leave
    `dialogue` empty ("") unless the type addon explicitly calls for it.
When dialogue is present, write what a real 19–25-year-old 42 Abu Dhabi
student would actually say — conversational, specific, no corporate buzz
("game-changing", "leveraging", "synergy", "empower"). Open with a HOOK
in the first 1.5 seconds (a question, a claim, a curious fact). Match the
length to the duration: ~20-25 words/scene at 8s in English, ~15-20 at 8s
in Arabic. The LANGUAGE directive in the USER message says which language
to write in (en / ar / mixed). When the SCENES context block contains
SAYS: lines from real high-engagement IG reels, echo their rhythm and
vocabulary — do NOT copy lines verbatim.

VIDEO TYPE adherence: read the VIDEO TYPE directive in the USER message.
It dictates the kind of shot every scene must be. If the type is
"talking_head", every scene is a medium-close-up on one speaker. Do not
drift into B-roll or environment shots within a talking_head script.

EVIDENCE BLOCKS in the soft context (when present): PAST SCRIPTS THAT WORKED,
REAL @42abudhabi POSTS (with HOOK and PATTERN lines), and SCENES THAT MATCH
THIS CONCEPT are real, audience-validated signal — treat them as ground
truth for what hooks this audience. Echo their structural choices (opening
beat, pacing, motif) where the current idea naturally allows; never copy
lines verbatim. The SCENES block names specific beats from real reels —
use them as concrete reference for the first scene, hand-off transitions,
and payoff moments.

CHARACTER LIBRARY (when shown in the context): when a scene needs a person,
NAME a specific character from the library in the scene.subject field using
the tag format '#<id> <slug>' (e.g. '#7 yousef_b — close on his face as he
explains the project'). The library is sorted least-recently-used first by
archetype — that's your default pick. Don't pick the same character used in
adjacent scenes unless continuity demands. The renderer resolves '#<id>'
back to the actual reference image and passes it to Veo automatically.

Do NOT write a top-level free-form veo_prompt — the server assembles the
final Veo prompt from your structured fields.
"""


USER_IDEA_SYSTEM = f"""\
You polish a user's rough text idea into ONE concrete video concept.

{CRAFT_RUBRIC}

{_BRAND_PRIMER}

Rules:
- Keep what the user wants. Don't replace their idea — refine and concretise it.
- Apply the 5 rules of a shootable beat. If their rough idea is a cliché
  (one of the banned phrases), rewrite it into a specific moment that
  preserves the user's intent but is actually filmable.
- Add concrete details: subject, action, mood, setting, brand_anchors.
- If their idea is impossible to render in 8s of AI video, propose the
  closest workable alternative and note the change in `reason`.
- All the same brand-anchoring requirements as IDEA_SYSTEM apply — the user's
  rough idea is the seed, the brand profile is the canvas.

Return exactly one idea object (full schema, including brand_anchors).
"""


REFERENCE_ANALYSIS_SYSTEM = """\
You analyse a reference image the user uploaded to inspire a video.

Return:
  description:     2-3 sentences on what is visually happening
  style_notes:     2-3 sentences on the visual style (palette, composition,
                   lighting, era, treatment)
  dominant_colors: ≤6 hex codes you can confidently see
  subject_summary: one short phrase describing the main subject (used for
                   nearest-neighbour search)
  veo_prompt_hint: a Veo-ready prompt that would produce a video in this style.
                   One sentence, includes camera + lighting + subject.
"""


REFERENCE_IDEA_SYSTEM = f"""\
You turn a visual reference image into ONE concrete video concept that
visually resembles the reference (Veo will be given the reference image as a
style anchor — so describe what should happen *within* that visual world).

{CRAFT_RUBRIC}

{_BRAND_PRIMER}

The user has attached:
  - A description of the reference image (subject, palette, mood)
  - The reference image's style notes
  - Optional free-form notes about what they want different / what to keep

Rules:
- The video must feel visually consistent with the reference. Match palette,
  lighting style, composition vocabulary.
- Don't just replicate the reference frame — design a 4-8 second beat of
  motion that lives in that visual world.
- Honor the user's optional notes when present. If they conflict with the
  reference (e.g. "make it sunny" on a moody night reference), prefer the user.
- All brand-anchoring requirements still apply — anchor the concept to the
  brand even when the reference is style-led.

Return ONE idea object (full schema, including brand_anchors).
"""


# -----------------------------------------------------------------------------
# Video types — imperative directives the user picks at run-start.
# -----------------------------------------------------------------------------
#
# v5.1: every `idea_addon` / `script_addon` is now an IMPERATIVE with explicit
# REJECT IF rules. The strings are also injected into the USER message via
# `memory.format_video_type_directive()` — the system-prompt copy is the
# secondary signal; the user-message copy is the primary one.
#
# `allowed_subject_types` is consumed by `generate_ideas.run` to validate the
# LLM's output. If a returned idea has a subject_type outside this set, the
# validator fires one retry. Empty `allowed_subject_types` means "any" (e.g.
# cinematic_hero — any subject can be heroic).

VIDEO_TYPES: dict[str, dict] = {
    "talking_head": {
        "name": "Talking head",
        "description": "Person speaking directly to camera.",
        "idea_addon": (
            "VIDEO TYPE — TALKING HEAD: every idea MUST be a single human "
            "speaking directly to the camera. REJECT IF the idea is an "
            "abstract animation, a montage, a landscape, a product, or "
            "involves multiple people. The subject is ONE face, addressing "
            "the viewer."
        ),
        "script_addon": (
            "TALKING HEAD scripts: every scene is a medium close-up or "
            "closer of ONE human speaking. Camera is locked-off or a slow "
            "push-in (no whip pans, no orbits, no dolly moves). NO cuts to "
            "b-roll, NO environment shots, NO product inserts. The face is "
            "in frame the whole time.\n"
            "EVERY scene MUST have a non-empty `dialogue` field — the EXACT "
            "words the speaker says, in the language given by the LANGUAGE "
            "directive in the user message. Open with a HOOK in the first "
            "1.5 seconds (a question, a claim, a curious fact). Conversational "
            "first-person. NO buzzwords. Length: ~20-25 words per 8-second "
            "scene in English, ~15-20 in Arabic. Empty dialogue is INVALID."
        ),
        "default_audio": True,
        "recommended_aspect": "16:9",
        "scenes_target": 1,
        "allowed_subject_types": ["solo_speaker"],
    },
    "campus_tour": {
        "name": "Campus tour",
        "description": "Moving camera through 42's spaces. No dialogue.",
        "idea_addon": (
            "VIDEO TYPE — CAMPUS TOUR: the SPACE is the subject. Ideas MUST "
            "be about exploring the 42 Abu Dhabi physical environment — "
            "atrium, labs, generative-pattern walls, courtyards. REJECT IF "
            "anyone is talking, REJECT IF the subject is a single static "
            "person, REJECT IF the focus is an object. People may appear "
            "in-frame but as inhabitants, not speakers."
        ),
        "script_addon": (
            "CAMPUS TOUR scripts: the camera moves through space (handheld "
            "walk, smooth dolly, or tracking). Wide-to-medium shots. People "
            "may appear in the background; no one addresses the camera, "
            "no one speaks audibly. Subject = space; humans = inhabitants."
        ),
        "default_audio": False,
        "recommended_aspect": "16:9",
        "scenes_target": 1,
        "allowed_subject_types": ["environment", "group_action"],
    },
    "voiceover_narrative": {
        "name": "Voice-over narrative",
        "description": "Mini-story with implied narration.",
        "idea_addon": (
            "VIDEO TYPE — VOICE-OVER NARRATIVE: ideas MUST have a clear "
            "narrative beat (one moment of decision, change, or arrival) "
            "with implied voice-over. REJECT IF it's a static talking head, "
            "REJECT IF there's no human element, REJECT IF the action is "
            "abstract animation."
        ),
        "script_addon": (
            "VOICE-OVER NARRATIVE scripts: each scene has internal motion "
            "and ONE decisive camera move. Audio is on for a brief narrated "
            "line. The visible action carries the story; the VO completes it. "
            "Subject is a person or a clearly-narratable moment, NEVER an "
            "abstract motif.\n"
            "EVERY scene MUST have a non-empty `dialogue` field — the EXACT "
            "narrated line (one short sentence) in the language from the "
            "LANGUAGE directive. The on-camera subject does NOT have to be "
            "speaking; the line is the voice-over heard over the visible "
            "action. Length: ~12-18 words per 8-second scene. Empty is INVALID."
        ),
        "default_audio": True,
        "recommended_aspect": "16:9",
        "scenes_target": 1,
        "allowed_subject_types": [
            "solo_speaker", "group_action", "environment",
            "hands_on_object", "narrative_montage",
        ],
    },
    "product_showcase": {
        "name": "Product showcase",
        "description": "Hero shot of an object.",
        "idea_addon": (
            "VIDEO TYPE — PRODUCT SHOWCASE: an OBJECT is the hero. "
            "REJECT IF any human face is in focus, REJECT IF the subject is "
            "an environment, REJECT IF the idea is purely abstract typography. "
            "Hands or partial body may appear as scale reference; full faces "
            "may not."
        ),
        "script_addon": (
            "PRODUCT SHOWCASE scripts: static or slow-rotating camera around "
            "a hero object. Dramatic studio lighting (high contrast, strong "
            "rim, soft fill). Background neutral or branded-but-quiet. "
            "Subject is the object — never a face."
        ),
        "default_audio": False,
        "recommended_aspect": "16:9",
        "scenes_target": 1,
        "allowed_subject_types": ["product_focused", "hands_on_object"],
    },
    "lifestyle_mood": {
        "name": "Lifestyle / mood",
        "description": "Aesthetic ambient — people in space, no dialogue.",
        "idea_addon": (
            "VIDEO TYPE — LIFESTYLE / MOOD: aesthetic-first, observational. "
            "Ideas MUST show people in their natural state inside the brand "
            "world — not addressing the camera. REJECT IF anyone speaks to "
            "the lens, REJECT IF the subject is a product alone, REJECT IF "
            "the visual is purely abstract animation."
        ),
        "script_addon": (
            "LIFESTYLE / MOOD scripts: slow camera drift or static. Soft "
            "natural lighting (golden hour, window light, soft overcast). "
            "Subjects are mid-activity (reading, talking off-camera, "
            "thinking) — never addressing the lens. Background brand-anchored."
        ),
        "default_audio": False,
        "recommended_aspect": "16:9",
        "scenes_target": 1,
        "allowed_subject_types": [
            "solo_speaker", "group_action", "environment", "hands_on_object",
        ],
    },
    "cinematic_hero": {
        "name": "Cinematic hero shot",
        "description": "Trailer-style single dramatic moment.",
        "idea_addon": (
            "VIDEO TYPE — CINEMATIC HERO: ONE dramatic, trailer-style moment. "
            "Ideas can use any subject type but MUST have a clear hero beat "
            "(a reveal, a decision, a sweep). REJECT IF the idea is a flat "
            "documentary shot or an abstract pattern without a hero moment."
        ),
        "script_addon": (
            "CINEMATIC HERO scripts: ONE decisive camera move on a strong "
            "central subject. High-contrast cinematic lighting. Punchy "
            "palette. Anamorphic feel preferred. Treat each scene like a "
            "trailer beat."
        ),
        "default_audio": False,
        "recommended_aspect": "16:9",
        "scenes_target": 1,
        # cinematic_hero is intentionally permissive — any subject can be heroic
        "allowed_subject_types": [],
    },
    "tutorial_howto": {
        "name": "Tutorial / how-to",
        "description": "Step demonstration. Narration-friendly.",
        "idea_addon": (
            "VIDEO TYPE — TUTORIAL: ideas MUST demonstrate a step, technique, "
            "or process visually. The HANDS or the SCREEN action is the "
            "subject. REJECT IF the idea is a static talking head, REJECT IF "
            "it's an environment shot, REJECT IF it's purely aspirational "
            "without showing a step."
        ),
        "script_addon": (
            "TUTORIAL scripts: top-down or over-the-shoulder framing. The "
            "action (hands typing, fingers on a touchscreen, a tool meeting "
            "a material) is the subject. Camera static or slow zoom. "
            "No to-camera speech; narration is OFF-SCREEN."
        ),
        "default_audio": True,
        "recommended_aspect": "16:9",
        "scenes_target": 1,
        "allowed_subject_types": ["hands_on_object", "solo_speaker"],
    },
    "event_recap": {
        "name": "Event recap",
        "description": "Hackathon / gathering energy.",
        "idea_addon": (
            "VIDEO TYPE — EVENT RECAP: ideas MUST capture group energy — "
            "multiple people, parallel action, density. REJECT IF the idea "
            "is one quiet person speaking, REJECT IF the visual is a single "
            "hero object, REJECT IF it's a static establishing shot with no "
            "human action."
        ),
        "script_addon": (
            "EVENT RECAP scripts: densely-packed shots — multiple people "
            "doing different things in one frame. Camera may sweep or track. "
            "Energy and parallel action over stillness."
        ),
        "default_audio": False,
        "recommended_aspect": "16:9",
        "scenes_target": 1,
        "allowed_subject_types": ["group_action", "narrative_montage"],
    },
    "testimonial": {
        "name": "Testimonial",
        "description": "Candid speaker, conversational.",
        "idea_addon": (
            "VIDEO TYPE — TESTIMONIAL: a single candid speaker, mid-thought, "
            "NOT strictly addressing the lens (3/4 angle). REJECT IF the "
            "subject is fully to-camera (that's talking_head, not "
            "testimonial), REJECT IF there's no human, REJECT IF the "
            "speaker is performing."
        ),
        "script_addon": (
            "TESTIMONIAL scripts: off-axis medium shot (3/4 angle, eyes off "
            "the lens). Soft natural lighting. Subject mid-thought, "
            "candid — not performing. Slow drift or static."
        ),
        "default_audio": True,
        "recommended_aspect": "16:9",
        "scenes_target": 1,
        "allowed_subject_types": ["solo_speaker"],
    },
}


def get_video_type(key: str | None) -> dict | None:
    """Return the type config for `key`, or None for 'generic' / unknown."""
    if not key or key == "generic":
        return None
    return VIDEO_TYPES.get(key)


# -----------------------------------------------------------------------------
# Dialogue language — per-run flag injected into the SCRIPT user prompt.
# -----------------------------------------------------------------------------

_DIALOGUE_LANGUAGE_BODY: dict[str, str] = {
    "en": (
        "LANGUAGE — ENGLISH: write every scene's `dialogue` field in natural "
        "spoken English. Use the vocabulary a real 19-25-year-old 42 Abu Dhabi "
        "student would actually use. Contractions are fine. Single Arabic "
        "interjections (Yalla, Inshallah, Habibi, Khalas) are OK when they "
        "feel natural — do NOT pile them on. No corporate buzzwords."
    ),
    "ar": (
        "LANGUAGE — ARABIC: write every scene's `dialogue` field in natural "
        "spoken Khaleeji Arabic (Abu Dhabi dialect). Use Arabic script. "
        "Conversational, first-person, NOT formal Modern Standard Arabic. "
        "Latin acronyms (42, AI, UAE) may appear in their Latin form. "
        "Keep lines short — ~15-20 words per 8-second scene."
    ),
    "mixed": (
        "LANGUAGE — MIXED (CODE-SWITCH): write every scene's `dialogue` field "
        "in natural Emirati code-switching as @42abudhabi students actually "
        "speak. English backbone, Arabic phrases for emphasis (Yalla, "
        "Inshallah, Habibi, Hayya, Khalas, Mashallah, w'Allah). One full "
        "Arabic sentence per scene is acceptable when it lands harder than "
        "the English equivalent. No buzzwords."
    ),
}


def format_dialogue_language_directive(
    language: str | None,
    *,
    required: bool,
) -> str:
    """Render the dialogue-language directive for the SCRIPT user prompt.

    Args:
        language: en | ar | mixed (case-insensitive). Falls back to default.
        required: True when the run's video_type forces non-empty dialogue
            (talking_head / voiceover_narrative). False = optional dialogue.

    Returns:
        A short block ready to interpolate into the user prompt. Always
        returns a non-empty string — even non-speech video_types get the
        directive so the model knows what to do if it CHOOSES to add a
        spoken line.
    """
    lang = (language or DEFAULT_DIALOGUE_LANGUAGE).strip().lower()
    if lang not in DIALOGUE_LANGUAGE_ENUM:
        lang = DEFAULT_DIALOGUE_LANGUAGE
    body = _DIALOGUE_LANGUAGE_BODY[lang]
    if required:
        header = (
            "DIALOGUE DIRECTIVE — this run's video_type REQUIRES non-empty "
            "`dialogue` on every scene."
        )
    else:
        header = (
            "DIALOGUE DIRECTIVE — this run's video_type does NOT require "
            "dialogue. Leave `dialogue` as \"\" (empty string) unless a "
            "spoken line is essential to the scene."
        )
    return f"\n{header}\n{body}\n"


def format_audio_clause(dialogue: str, language: str | None, delivery: str | None = None) -> str:
    """Build the audio clause appended to the Veo prompt when a scene has
    non-empty dialogue. Returns empty string when dialogue is empty.

    Veo 3.1 generates speech from the visual prompt itself (no separate
    SDK audio parameter), so we weave the dialogue into the prompt with an
    explicit speech instruction.

    `delivery` optionally overrides the trailing delivery phrase (default
    "clear enunciation, lip-synced") — e.g. talking-head callers pass a
    calmer, slower delivery so the lip-sync stays natural.
    """
    text = (dialogue or "").strip()
    if not text:
        return ""
    lang = (language or DEFAULT_DIALOGUE_LANGUAGE).strip().lower()
    # Escape any embedded double-quotes so the prompt stays parseable.
    safe = text.replace('"', '\\"')
    tail = delivery.strip() if (delivery and delivery.strip()) else "clear enunciation, lip-synced"
    if lang == "ar":
        tone = f"in natural Khaleeji Arabic (Abu Dhabi dialect), {tail}"
    elif lang == "mixed":
        tone = (
            "naturally code-switching between English and Arabic as Emirati "
            f"speakers do, {tail}"
        )
    else:  # en / unknown
        tone = f"in a natural conversational English tone, {tail}"
    return f'Audio: the speaker says "{safe}" {tone}.'
