"""Sound-effect resolution for the AI video editor.

Three-tier sourcing, best-effort and never raising to the caller:
  1. ElevenLabs Sound Generation (great quality, prompt-driven → naturally varied)
  2. A bundled library under app/assets/sfx/<category>/*.mp3 (CC0 files, if present)
  3. ffmpeg-synthesized fallback (always available — guarantees a sound)

The resolved mp3 is imported as a yas_uploads audio file; resolve_sfx returns
{"source_upload_id", "durationInFrames", "key"} or None on total failure. `key`
identifies the specific sound used so callers can pass it back in `avoid` to keep
successive effects different.
"""
from __future__ import annotations

import asyncio
import logging
import random
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from . import media_providers
from . import yas_video_editor as ve
from .media_providers import ProviderError

log = logging.getLogger("archivist.yas_sfx")

# Bundled CC0 library root (may be empty — that's fine, tiers 1 & 3 cover it).
_SFX_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "sfx"

# category -> ElevenLabs prompt variants (the variety pool). Keep them short.
SFX_CATEGORIES: dict[str, list[str]] = {
    "whoosh": ["quick swish transition", "fast cinematic whoosh", "soft air whoosh", "smooth swoosh"],
    "pop": ["soft bubble pop", "quick cartoon pop", "bright pop", "playful pop"],
    "ding": ["bright ding chime", "gentle notification ding", "sparkle ding"],
    "sparkle": ["magic sparkle shimmer", "twinkle sparkle", "fairy shimmer rise"],
    "drop": ["water droplet plink", "soft bubble drop", "subtle bass drop"],
    "click": ["soft UI click", "light tick"],
}
_DEFAULT_CATEGORY = "pop"


def _pick_prompt(category: str, avoid: set[str]) -> tuple[str, str]:
    """Return (prompt, key). Prefer a variant not in `avoid`; if all are used,
    fall back to any. `key` is the chosen variant string."""
    variants = SFX_CATEGORIES.get(category) or SFX_CATEGORIES[_DEFAULT_CATEGORY]
    fresh = [v for v in variants if v not in avoid]
    chosen = random.choice(fresh) if fresh else random.choice(variants)
    return chosen, chosen


def _bundled_files(category: str) -> list[Path]:
    d = _SFX_ASSETS_DIR / category
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.mp3") if p.is_file())


def _synth_sfx_sync(category: str) -> bytes:
    """Synthesize a short category-appropriate effect with ffmpeg. Always works
    (no network / no bundled files needed). Returns mp3 bytes."""
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    # (lavfi input string, audio filter) per category.
    recipes = {
        "whoosh": ("anoisesrc=d=0.6:c=pink:a=0.6", "highpass=f=250,afade=t=in:d=0.12,afade=t=out:st=0.32:d=0.26,volume=2.0"),
        "pop":    ("sine=frequency=520:duration=0.16",  "afade=t=out:st=0.03:d=0.12,volume=1.4"),
        "ding":   ("sine=frequency=1100:duration=0.6",  "afade=t=out:st=0.06:d=0.52"),
        "sparkle":("sine=frequency=1500:duration=0.55", "tremolo=f=18:d=0.7,afade=t=out:st=0.1:d=0.42"),
        "drop":   ("sine=frequency=300:duration=0.32",  "afade=t=out:st=0.05:d=0.25,volume=1.3"),
        "click":  ("sine=frequency=800:duration=0.07",  "afade=t=out:st=0.01:d=0.05"),
    }
    src, af = recipes.get(category, recipes["pop"])
    tmp = Path(tempfile.mktemp(suffix=".mp3"))
    try:
        cmd = [ff, "-y", "-f", "lavfi", "-i", src, "-af", af,
               "-ac", "1", "-ar", "44100", "-b:a", "128k", "-f", "mp3", str(tmp)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg sfx synth failed: {(proc.stderr or '')[:300]}")
        return tmp.read_bytes()
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


async def resolve_sfx(
    *,
    category: Optional[str] = None,
    prompt: Optional[str] = None,
    duration_sec: float = 0.9,
    avoid: Optional[set[str]] = None,
    prefer_generate: bool = True,
) -> Optional[dict]:
    """Best-effort: returns {"source_upload_id", "durationInFrames", "key"} or
    None. Never raises. `avoid` holds keys already used this run (variety)."""
    avoid = avoid or set()
    cat = (category or _DEFAULT_CATEGORY).strip().lower()
    if cat not in SFX_CATEGORIES:
        cat = _DEFAULT_CATEGORY
    chosen_prompt, key = (prompt.strip(), prompt.strip()) if (prompt and prompt.strip()) else _pick_prompt(cat, avoid)

    raw: Optional[bytes] = None
    # Tier 1 — ElevenLabs generation.
    if prefer_generate:
        try:
            provider = media_providers.get_voice_provider()
            if provider.available():
                raw = await provider.generate_sfx(prompt=chosen_prompt, duration_seconds=duration_sec)
        except (ProviderError, Exception) as exc:  # noqa: BLE001 — fall through to fallback
            log.warning("[sfx] generation failed, falling back: %s", str(exc)[:200])
            raw = None
    # Tier 2 — bundled CC0 file not already used.
    if raw is None:
        files = [p for p in _bundled_files(cat) if p.name not in avoid]
        if not files:
            files = _bundled_files(cat)
        if files:
            pick = random.choice(files)
            try:
                raw = pick.read_bytes()
                key = pick.name
            except OSError:
                raw = None
    # Tier 3 — ffmpeg synthesis (always available).
    if raw is None:
        try:
            raw = await asyncio.to_thread(_synth_sfx_sync, cat)
            key = f"synth:{cat}"
        except Exception as exc:  # noqa: BLE001
            log.exception("[sfx] synth fallback failed")
            return None

    if not raw:
        return None
    try:
        desc = await ve.import_audio(raw=raw, filename=f"sfx_{cat}.mp3", mime="audio/mpeg")
    except Exception as exc:  # noqa: BLE001
        log.exception("[sfx] import_audio failed")
        return None
    return {
        "source_upload_id": int(desc["id"]),
        "durationInFrames": int(desc.get("durationInFrames") or 24),
        "key": key,
    }
