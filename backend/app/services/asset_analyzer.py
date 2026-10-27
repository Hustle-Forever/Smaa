"""Image + font analyzers for the Assets Library.

- `describe_character_image(path, archetype)` → archetype-specific structured
  description via Gemini multimodal w/ JSON schema. Returns (ai_description,
  attributes_dict).
- `describe_generic_image(path, hint=None)` → prose description of any image.
- `describe_font(path)` → fontTools metadata + PIL preview render. Returns
  (description, attributes_dict, preview_png_bytes).

Video uploads use the existing video pipeline (no new code here).
"""
from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path

from .vertex_client import gemini_multimodal

log = logging.getLogger("archivist.asset_analyzer")


# ---------------------------------------------------------------------------
# Character schemas — archetype-driven
# ---------------------------------------------------------------------------

# Common fields every archetype has
_COMMON_PROPS = {
    "age_range":   {"type": "STRING", "description": "Perceived age band, e.g. '20-25', '35-40'"},
    "skin_tone":   {"type": "STRING"},
    "build":       {"type": "STRING", "description": "Slim / average / athletic / heavy"},
    "glasses":     {"type": "STRING", "description": "Frame shape + color, or 'none'"},
    "distinguishing_features": {
        "type": "ARRAY", "items": {"type": "STRING"},
        "description": "1-3 short specific markers (e.g. 'warm slight smile', 'lip ring', 'tattoo on right wrist')",
    },
}

_EMIRATI_MALE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        **_COMMON_PROPS,
        "facial_hair": {"type": "STRING", "description": "Clean-shaven / stubble / trimmed beard (color, density) / full beard"},
        "hair":        {"type": "STRING", "description": "Color + style if visible (often covered by ghutra)"},
        "kandura": {
            "type": "OBJECT",
            "properties": {
                "color":      {"type": "STRING"},
                "fabric":     {"type": "STRING", "description": "Cotton / linen / wool blend / smooth / textured"},
                "collar":     {"type": "STRING", "description": "Mandarin / shirt-style / open"},
                "cuffs":      {"type": "STRING", "description": "Buttoned / open / French cuff"},
                "embroidery": {"type": "STRING", "description": "Specific embroidery details, or 'none visible'"},
            },
        },
        "ghutra": {
            "type": "OBJECT",
            "properties": {
                "color":   {"type": "STRING"},
                "pattern": {"type": "STRING", "description": "Plain white / red-checked (shemagh) / other"},
                "egal":    {"type": "STRING", "description": "Black double-band / single-band / no egal"},
                "drape":   {"type": "STRING", "description": "How the ghutra falls: tight / loose / draped over shoulder"},
            },
        },
        "accessories": {"type": "STRING", "description": "Watch / rings / cufflinks / 'none visible'"},
    },
    "required": ["age_range", "skin_tone", "facial_hair", "kandura", "ghutra"],
}

_EMIRATI_FEMALE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        **_COMMON_PROPS,
        "makeup": {"type": "STRING", "description": "Subtle / minimal / bold; specific notes"},
        "hijab": {
            "type": "OBJECT",
            "properties": {
                "color":   {"type": "STRING"},
                "pattern": {"type": "STRING", "description": "Solid / floral / geometric / striped"},
                "fabric":  {"type": "STRING", "description": "Chiffon / silk / cotton / textured"},
                "style":   {"type": "STRING", "description": "Snug & framing face / loose-wrapped / turban-style"},
            },
        },
        "abaya": {
            "type": "OBJECT",
            "properties": {
                "color":         {"type": "STRING"},
                "embellishment": {"type": "STRING", "description": "Plain / embroidery (specify) / beading / lace trim / 'none visible'"},
                "sleeves":       {"type": "STRING", "description": "Standard / flared / bell / kimono-style"},
                "cut":           {"type": "STRING", "description": "Traditional / modern A-line / open-front"},
            },
        },
        "accessories": {"type": "STRING"},
    },
    "required": ["age_range", "skin_tone", "hijab", "abaya"],
}

_CASUAL_GENERAL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        **_COMMON_PROPS,
        "facial_hair": {"type": "STRING", "description": "(for males) clean-shaven / stubble / beard details"},
        "hair":        {"type": "STRING", "description": "Color, length, style"},
        "attire": {
            "type": "OBJECT",
            "properties": {
                "top":       {"type": "STRING", "description": "Specific top — colour, garment type, any logos/text"},
                "bottom":    {"type": "STRING", "description": "If visible — pants/skirt/shorts, colour"},
                "outerwear": {"type": "STRING", "description": "Jacket/hoodie/coat — or 'none'"},
                "layered":   {"type": "STRING", "description": "What's visible underneath, if anything"},
            },
        },
        "accessories": {"type": "STRING"},
    },
    "required": ["age_range", "skin_tone", "hair", "attire"],
}


_ARCHETYPE_SCHEMAS = {
    "emirati_male":   _EMIRATI_MALE_SCHEMA,
    "emirati_female": _EMIRATI_FEMALE_SCHEMA,
    "casual_male":    _CASUAL_GENERAL_SCHEMA,
    "casual_female":  _CASUAL_GENERAL_SCHEMA,
}


# Wrap-around schema so Gemini returns BOTH the structured attrs and a
# rich prose description in a single call.
def _bundle_schema(attrs_schema: dict) -> dict:
    return {
        "type": "OBJECT",
        "properties": {
            "ai_description": {
                "type": "STRING",
                "description": (
                    "200-400 word prose description focused on identifying "
                    "features that distinguish this person from others. "
                    "Mention fabric details, embroidery, accessories, "
                    "posture, facial features. Avoid generic adjectives "
                    "('vibrant', 'engaging'). Be concrete."
                ),
            },
            "attributes": attrs_schema,
        },
        "required": ["ai_description", "attributes"],
    }


_CHARACTER_SYSTEM = (
    "You are cataloging a character reference for a brand-content team at 42 "
    "Abu Dhabi (a tuition-free coding school). The character will be reused "
    "as a recurring on-screen face in AI-generated videos. Your job is to "
    "describe this specific person in enough detail that the next character "
    "we save can be visually distinguished from this one — focus on what "
    "makes THIS face/outfit specifically THIS person. Output strictly JSON."
)


# ---------------------------------------------------------------------------
# Public: image describers
# ---------------------------------------------------------------------------

async def describe_character_image(
    image_path: Path, *, archetype: str,
) -> tuple[str, dict]:
    """Returns (ai_description, attributes_dict). Raises ValueError on bad archetype."""
    schema = _ARCHETYPE_SCHEMAS.get(archetype)
    if schema is None:
        raise ValueError(f"Unknown archetype: {archetype!r}")

    bundle = _bundle_schema(schema)
    prompt = (
        f"This is a reference image for the archetype: {archetype}.\n"
        f"Describe the person in detail per the schema. The description "
        f"should be specific enough that a different person of the same "
        f"archetype would be obviously distinct in the JSON."
    )
    try:
        result = await gemini_multimodal(
            prompt,
            images=[image_path],
            system=_CHARACTER_SYSTEM,
            model="gemini-2.5-flash",
            json_schema=bundle,
            temperature=0.3,
            max_tokens=2500,
        )
    except Exception:
        log.exception("Gemini character description failed for %s", image_path)
        return ("", {})

    if isinstance(result, dict) and "ai_description" in result:
        return (
            str(result.get("ai_description") or "").strip(),
            dict(result.get("attributes") or {}),
        )
    # Fall-through: parse failed; return whatever raw is available
    raw = (result.get("_raw") if isinstance(result, dict) else str(result))[:800]
    return (raw or "", {})


_GENERIC_IMAGE_SYSTEM = (
    "You write rich, specific descriptions of brand-reference images for 42 "
    "Abu Dhabi. The image will be used by an AI video-generation pipeline as "
    "stylistic / mood / logo reference. Describe what's visually present in "
    "enough detail that the generator can echo the style: subject, "
    "composition, palette (name actual colours), lighting, mood, brand cues "
    "if any, any on-screen text. 200-400 words, prose only, no bullet lists. "
    "Avoid generic adjectives ('vibrant', 'engaging', 'modern')."
)


async def describe_generic_image(
    image_path: Path, *, hint: str | None = None,
) -> tuple[str, dict]:
    """Returns (ai_description, attributes_dict). Attributes is intentionally
    small — just a tag list + dominant_palette."""
    bundle = {
        "type": "OBJECT",
        "properties": {
            "ai_description": {"type": "STRING"},
            "attributes": {
                "type": "OBJECT",
                "properties": {
                    "tags": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "5-10 short keywords"},
                    "dominant_palette": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Up to 5 actual colour names or hex"},
                    "mood": {"type": "STRING"},
                    "composition": {"type": "STRING", "description": "Centered / rule-of-thirds / symmetrical / negative-space"},
                    "subject_type": {"type": "STRING", "description": "logo / mood photo / illustration / typography / abstract / portrait / landscape"},
                },
            },
        },
        "required": ["ai_description", "attributes"],
    }
    prompt = (
        "Describe this brand-reference image."
        + (f" Hint from the uploader: {hint}" if hint else "")
    )
    try:
        result = await gemini_multimodal(
            prompt,
            images=[image_path],
            system=_GENERIC_IMAGE_SYSTEM,
            model="gemini-2.5-flash",
            json_schema=bundle,
            temperature=0.3,
            max_tokens=2000,
        )
    except Exception:
        log.exception("Gemini generic image describe failed for %s", image_path)
        return ("", {})

    if isinstance(result, dict) and "ai_description" in result:
        return (
            str(result.get("ai_description") or "").strip(),
            dict(result.get("attributes") or {}),
        )
    raw = (result.get("_raw") if isinstance(result, dict) else str(result))[:800]
    return (raw or "", {})


# ---------------------------------------------------------------------------
# Audio / music analyzer
# ---------------------------------------------------------------------------
#
# Music + sfx the owner uploads for the Video Editor. Gemini 2.5 reads audio
# natively (gemini_multimodal passes Part.from_bytes with the file's MIME), so
# we get mood/genre/energy/instrumentation for a future "auto-edit" that picks
# the right track per scene. The prose ai_description is what gets embedded
# (gemini-embedding-001, 768d) for semantic retrieval.

# Gemini inline-data is capped (~20MB); above this we describe from the title
# only rather than hang the upload.
_AUDIO_INLINE_MAX_BYTES = 18 * 1024 * 1024

_AUDIO_SYSTEM = (
    "You are cataloguing a music / audio track for a brand-content team at 42 "
    "Abu Dhabi so an automated video editor can later pick the right track for "
    "a scene. Listen to the audio and describe it for retrieval: genre, mood, "
    "energy, tempo feel, instrumentation, whether it has vocals (and language), "
    "and what kinds of moments it would score well (intro sting, background "
    "bed, fast montage, emotional outro, dramatic reveal). 120-220 words of "
    "specific prose — name real instruments and comparable styles, avoid empty "
    "adjectives ('nice', 'cool'). Output strictly JSON."
)

_AUDIO_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "ai_description": {"type": "STRING"},
        "attributes": {
            "type": "OBJECT",
            "properties": {
                "mood": {"type": "STRING", "description": "e.g. uplifting / tense / calm / triumphant / playful"},
                "genre": {"type": "STRING", "description": "e.g. cinematic / lo-fi hip hop / corporate / orchestral / electronic"},
                "energy": {"type": "INTEGER", "description": "1 (calm) to 5 (high energy)"},
                "tempo_feel": {"type": "STRING", "description": "slow / mid / up"},
                "instrumentation": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Main instruments / sounds"},
                "has_vocals": {"type": "BOOLEAN"},
                "language": {"type": "STRING", "description": "Vocal language if any, else 'instrumental'"},
                "use_cases": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "intro / background / montage / outro / dramatic / transition"},
            },
        },
    },
    "required": ["ai_description", "attributes"],
}


async def describe_audio(audio_path: Path, *, title: str | None = None) -> tuple[str, dict]:
    """Returns (ai_description, attributes_dict) for a music/audio file via
    Gemini 2.5 (audio in). Falls back to a title-only description for files too
    large to send inline, so the upload never hangs."""
    label = title or audio_path.name
    try:
        size = audio_path.stat().st_size
    except OSError:
        size = 0
    if size > _AUDIO_INLINE_MAX_BYTES:
        log.info("[asset] audio %s is %d bytes — too large for inline analysis; title-only", label, size)
        return (
            f"Uploaded audio track '{label}'. (File too large for automatic content "
            f"analysis; tag manually for richer retrieval.)",
            {"analyze_status": "skipped_too_large"},
        )

    prompt = f"Analyse this audio track. Filename / title: {label!r}."
    try:
        result = await gemini_multimodal(
            prompt,
            images=[audio_path],  # param name is historical; it sends any file via MIME
            system=_AUDIO_SYSTEM,
            model="gemini-2.5-flash",
            json_schema=_AUDIO_SCHEMA,
            temperature=0.3,
            max_tokens=1500,
            thinking_budget=0,  # Flash + schema → no thinking budget (see gotchas)
        )
    except Exception:
        log.exception("Gemini audio describe failed for %s", audio_path)
        return (f"Uploaded audio track '{label}'.", {"analyze_status": "failed"})

    if isinstance(result, dict) and "ai_description" in result:
        return (
            str(result.get("ai_description") or "").strip(),
            dict(result.get("attributes") or {}),
        )
    raw = (result.get("_raw") if isinstance(result, dict) else str(result))[:800]
    return (raw or f"Uploaded audio track '{label}'.", {})


# ---------------------------------------------------------------------------
# Background / location analyzer
# ---------------------------------------------------------------------------
#
# Backgrounds are owner-uploaded photos of locations and environments (campus
# quad, rooftop, lecture hall, desert at sunset, etc.) that future producer
# / Veo flows reference for scene composition. We need structured retrieval
# axes (palette, lighting, time-of-day, indoor/outdoor) PLUS a prose
# description rich enough to drive semantic search.

_BACKGROUND_ATTRIBUTES_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "palette": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": (
                "3-5 dominant colors as #RRGGBB hex strings, ordered by "
                "prevalence in the image. Be honest about the actual hues — "
                "don't snap to primary colors if the real palette is muted."
            ),
        },
        "lighting": {
            "type": "STRING",
            "description": (
                "Pick exactly one: golden_hour | blue_hour | midday | "
                "overcast | low_key | studio | neon | mixed"
            ),
        },
        "time_of_day": {
            "type": "STRING",
            "description": (
                "Pick exactly one: dawn | morning | midday | afternoon | "
                "sunset | twilight | night | unknown"
            ),
        },
        "indoor_outdoor": {
            "type": "STRING",
            "description": "Pick exactly one: indoor | outdoor | mixed",
        },
        "location_type": {
            "type": "STRING",
            "description": (
                "Free-form, ≤6 words. Concrete: 'campus quad', 'rooftop "
                "terrace', 'modernist lecture hall', 'open desert dune', "
                "'industrial workshop', 'coastal boardwalk'."
            ),
        },
        "mood": {
            "type": "STRING",
            "description": (
                "Free-form, ≤4 words. Examples: 'calm contemplative', "
                "'energetic urban', 'stark minimalist', 'warm communal'."
            ),
        },
        "subjects_count": {
            "type": "INTEGER",
            "description": (
                "Approximate count of visible people. 0 = clean background. "
                "Higher = incidental subjects in the frame (people present "
                "but not the focus)."
            ),
        },
        "primary_orientation": {
            "type": "STRING",
            "description": "Pick exactly one: landscape | portrait | square",
        },
    },
    "required": [
        "palette", "lighting", "time_of_day", "indoor_outdoor",
        "location_type", "mood", "primary_orientation",
    ],
}


_BACKGROUND_SYSTEM = (
    "You are analyzing a photograph that will be used as a BACKGROUND / "
    "LOCATION reference for future AI-generated video scenes. Be concrete "
    "and visual.\n\n"
    "Your prose `ai_description` must be 120-220 words and name:\n"
    "  - the setting (campus quad / rooftop / lecture hall / desert dune / "
    "auditorium / corridor / etc.) with any signature architecture, "
    "landmarks, or signage that anchors it to a specific place;\n"
    "  - light direction and quality (low warm rim from camera left, "
    "diffused midday top-light, harsh single key, etc.);\n"
    "  - the actual color palette in plain words (warm terracotta + muted "
    "olive + cream) — match this with the structured `palette` hex list;\n"
    "  - dominant objects and textures (concrete pillars, palm fronds, "
    "exposed brick, glass curtain wall);\n"
    "  - mood / atmosphere in concrete terms (early-morning hush, end-of-day "
    "warmth, fluorescent institutional);\n"
    "  - what kind of subject a director might frame against this "
    "background (\"a single figure in profile would read well against the "
    "negative space camera-right\").\n\n"
    "Avoid generic adjectives ('beautiful', 'nice', 'amazing'). If people "
    "are visible, mention them briefly as scale, not as subject — this is a "
    "location asset, not a character portrait. Output strictly JSON per "
    "the schema."
)


async def describe_background_image(image_path: Path) -> tuple[str, dict]:
    """Returns (ai_description, attributes_dict) for a background photo.

    Mirrors the contract of `describe_character_image()` so the upload
    pipeline can call either analyzer with the same two-tuple shape.
    """
    bundle = _bundle_schema(_BACKGROUND_ATTRIBUTES_SCHEMA)

    # Derive orientation hint from PIL — cheaper than asking Gemini to
    # guess, and lets the model fill the rest of the schema with confidence.
    orientation_hint = ""
    try:
        from PIL import Image
        with Image.open(image_path) as im:
            w, h = im.size
        if w > h * 1.15:
            orientation_hint = " (orientation: landscape)"
        elif h > w * 1.15:
            orientation_hint = " (orientation: portrait)"
        else:
            orientation_hint = " (orientation: square)"
    except Exception:
        # Not fatal — model can infer from the image
        pass

    prompt = (
        f"This is a background / location reference image{orientation_hint}.\n"
        f"Describe the location per the schema. The description should be "
        f"specific enough that a director picking from a library could "
        f"reach for this one over another."
    )
    try:
        result = await gemini_multimodal(
            prompt,
            images=[image_path],
            system=_BACKGROUND_SYSTEM,
            model="gemini-2.5-flash",
            json_schema=bundle,
            temperature=0.2,
            max_tokens=2500,
        )
    except Exception:
        log.exception("Gemini background describe failed for %s", image_path)
        return ("", {})

    if isinstance(result, dict) and "ai_description" in result:
        return (
            str(result.get("ai_description") or "").strip(),
            dict(result.get("attributes") or {}),
        )
    raw = (result.get("_raw") if isinstance(result, dict) else str(result))[:800]
    return (raw or "", {})


# ---------------------------------------------------------------------------
# Font analyzer
# ---------------------------------------------------------------------------

def _font_name_record(font, name_id: int) -> str | None:
    """Pull a localised string from the font's name table. Prefer English."""
    for rec in font["name"].names:
        if rec.nameID != name_id:
            continue
        try:
            s = rec.toUnicode()
        except Exception:
            continue
        if rec.platformID == 3 and rec.platEncID == 1 and rec.langID == 0x409:
            return s  # Windows Unicode English-US — preferred
    # Fall back to any record with this nameID
    for rec in font["name"].names:
        if rec.nameID == name_id:
            try:
                return rec.toUnicode()
            except Exception:
                continue
    return None


def _font_supports_arabic(font) -> bool:
    """True if the cmap maps any Arabic codepoint."""
    cmap = font.getBestCmap() or {}
    # 0x0600-0x06FF = Arabic block; 0xFE70-0xFEFF = Arabic Presentation Forms-B
    for cp in (0x0627, 0x0628, 0x0641, 0x0644, 0xFE8E, 0xFEDB):
        if cp in cmap:
            return True
    return False


def _render_preview(font_path: Path, *, has_arabic: bool) -> bytes:
    """Render a 600×220 preview PNG. Two lines: English sample + Arabic if supported."""
    from PIL import Image, ImageDraw, ImageFont

    W, H = 600, 220
    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    # English line — large
    try:
        f_en = ImageFont.truetype(str(font_path), size=42)
    except Exception:
        f_en = ImageFont.load_default()
    en_line = "The quick brown fox 1234567890"
    draw.text((20, 30), en_line, font=f_en, fill=(20, 20, 28))

    # Arabic line (if the font supports it) — smaller
    if has_arabic:
        try:
            f_ar = ImageFont.truetype(str(font_path), size=38)
            ar_line = "في 42 أبوظبي نبني المستقبل"
            # Right-align: measure then position
            tb = draw.textbbox((0, 0), ar_line, font=f_ar)
            tw = tb[2] - tb[0]
            draw.text((W - 20 - tw, 110), ar_line, font=f_ar, fill=(20, 20, 28))
        except Exception:
            log.exception("Arabic preview render failed for %s", font_path)

    # Family name footer
    try:
        f_meta = ImageFont.truetype(str(font_path), size=14)
    except Exception:
        f_meta = ImageFont.load_default()
    draw.text((20, 180), font_path.name, font=f_meta, fill=(120, 120, 130))

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def describe_font(font_path: Path) -> tuple[str, dict, bytes | None]:
    """Inspect the font, render a preview PNG. Returns (description, attrs, preview_bytes)."""
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        log.error("fontTools not installed — font analysis disabled")
        return ("Font file (analysis unavailable: fontTools missing).", {}, None)

    try:
        font = TTFont(str(font_path), lazy=True)
    except Exception as e:
        log.exception("fontTools failed to open %s", font_path)
        return (f"Could not parse font file: {e}", {"error": str(e)[:200]}, None)

    family    = _font_name_record(font, 1)  or font_path.stem
    subfamily = _font_name_record(font, 2)  or ""
    full_name = _font_name_record(font, 4)  or family
    version   = _font_name_record(font, 5)  or ""
    designer  = _font_name_record(font, 9)  or ""
    foundry   = _font_name_record(font, 8)  or ""
    has_arabic = _font_supports_arabic(font)

    # Classify weight / style from OS/2 table if available
    weight = None
    italic = False
    try:
        os2 = font["OS/2"]
        weight = int(getattr(os2, "usWeightClass", 0)) or None
        italic = bool(getattr(os2, "fsSelection", 0) & 1)
    except Exception:
        pass

    attrs = {
        "family": family,
        "subfamily": subfamily,
        "full_name": full_name,
        "version": version,
        "designer": designer or None,
        "foundry": foundry or None,
        "weight_class": weight,
        "italic": italic,
        "supports_arabic": has_arabic,
    }
    style_bits = []
    if weight:
        style_bits.append(f"weight class {weight}")
    if italic:
        style_bits.append("italic")
    if has_arabic:
        style_bits.append("Arabic-capable")
    style_summary = ", ".join(style_bits) or "regular"

    description = (
        f"{full_name} — a {subfamily.lower() or 'regular'} cut of the "
        f"{family} family ({style_summary})"
        + (f", designed by {designer}" if designer else "")
        + (f" for {foundry}" if foundry else "")
        + ". Use this font when scripts call for its tonal register; the "
        "rendered preview shows the actual letterforms."
    )

    try:
        preview = _render_preview(font_path, has_arabic=has_arabic)
    except Exception:
        log.exception("Preview render failed for %s", font_path)
        preview = None

    return description, attrs, preview


__all__ = [
    "describe_character_image",
    "describe_generic_image",
    "describe_background_image",
    "describe_font",
]
