"""Visual RAG for reel seams — what a moment looks like, and what worked before.

A transition cannot be designed blind. Before anything is drawn, this module
answers two questions about the two frames a transition has to join:

  WHAT IS THERE   measured from the pixels (palette, brightness, contrast, a
                  busy-ness grid, edge energy, motion direction) and described
                  by a vision model (subject box, mood, what the frame is of).
                  The measurements are not decoration: they are what makes the
                  planner able to put a pattern where the frame is EMPTY and
                  tint it in a colour the frame already contains.

  WHAT WORKED     the nearest past seams by visual meaning, carrying the recipe
                  that was rendered for them and the stars the team gave it.

The measured half never fails and never costs anything, so every downstream
caller degrades to it when Vertex is unreachable. The model half and the DB
half are both best-effort by contract.

Two tables, created lazily like yas_rebrand_experiments:

  yas_visual_moments      one row per analysed frame, embedded for retrieval
  yas_transition_recipes  one row per rendered transition, with its rating

The pair (moment, moment) is what gets embedded for a seam, so retrieval asks
"have we joined two frames that LOOK like these before, and did it land?"
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Optional

from psycopg.types.json import Jsonb

from ..db import get_conn
from . import rebrand_media as media
from .vertex_client import gemini_embed

log = logging.getLogger("archivist.rebrand.moments")

# A frame counts as CONTENT when it is neither near-black nor flat. Cards fade
# out to black, so the literal last frame of an intro is routinely useless as
# the thing a transition grows out of.
MIN_MEAN_LUM = 14.0       # 0..255; below this the frame is essentially black
MIN_STDDEV = 11.0         # below this it is a flat colour field, not a picture
TAIL_SCAN_SEC = 2.5       # how far back from the end to look for content
HEAD_SCAN_SEC = 1.5       # how far in from the start (scenes can fade in too)
SCAN_FPS = 12.0           # scan resolution; 12/s is finer than any fade needs
# A frame must keep this much of the window's own peak brightness and contrast
# to count as a fair picture of the clip. Set by what a fade does: at 0.6 the
# scan stops before a card has dimmed enough for its palette to go muddy, while
# still accepting clips that are simply dark to begin with.
FADE_FLOOR = 0.6

GRID_COLS = 3
GRID_ROWS = 4             # 3x4 suits a 9:16 frame; the busy map is reported as
                          # row-major cells, each 0..1

_SCHEMA_READY = False


# ---------------------------------------------------------------------------
# Measurement (pixels only — no model, no network)
# ---------------------------------------------------------------------------

@dataclass
class Measured:
    """What the pixels say. Every field is computed, none is guessed."""
    mean_lum: float               # 0..255
    stddev: float                 # contrast
    is_dark: bool
    palette: list[dict]           # [{hex, weight, sat, lum}] by weight desc
    accent: str                   # most usable saturated colour, #rrggbb
    accent_sat: float             # 0..1 — how saturated that accent really is
    ink: str                      # a colour that will read ON this frame
    busy: list[float]             # GRID_ROWS*GRID_COLS, row-major, 0..1
    quietest_cell: int            # index into `busy`
    busiest_cell: int
    edge_energy: float            # 0..1, global detail
    motion: Optional[str] = None  # left|right|up|down|none, when a pair is given
    motion_strength: float = 0.0

    def quiet_cells(self, k: int = 4) -> list[int]:
        order = sorted(range(len(self.busy)), key=lambda i: self.busy[i])
        return order[:k]

    def cell_box(self, idx: int) -> dict:
        """Cell index → fractional box, for placing something in it."""
        r, c = divmod(idx, GRID_COLS)
        return {
            "x": round(c / GRID_COLS, 4), "y": round(r / GRID_ROWS, 4),
            "w": round(1 / GRID_COLS, 4), "h": round(1 / GRID_ROWS, 4),
        }


def _rgb_to_hsl_sat_lum(r: float, g: float, b: float) -> tuple[float, float]:
    mx, mn = max(r, g, b), min(r, g, b)
    lum = (mx + mn) / 2.0
    if mx == mn:
        return 0.0, lum / 255.0
    d = mx - mn
    sat = d / (510.0 - mx - mn) if lum > 127.5 else d / (mx + mn or 1.0)
    return float(sat), lum / 255.0


def _hex(rgb) -> str:
    r, g, b = (int(max(0, min(255, round(float(v))))) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _palette(arr, k: int = 6) -> list[dict]:
    """Dominant colours by simple binned clustering — deterministic, fast, and
    stable across frames (k-means seeds wander, which would make the tint of a
    transition jitter from render to render)."""
    import numpy as np

    px = arr.reshape(-1, 3).astype("float32")
    # 5 bits per channel: fine enough to separate a brand red from a skin tone,
    # coarse enough that noise does not split a flat wall into ten colours.
    keys = ((px[:, 0] // 32) * 64 + (px[:, 1] // 32) * 8 + (px[:, 2] // 32)).astype("int32")
    counts = np.bincount(keys, minlength=512)
    top = np.argsort(counts)[::-1][:k]
    out: list[dict] = []
    total = float(len(px))
    for key in top:
        if counts[key] <= 0:
            continue
        sel = px[keys == key]
        mean = sel.mean(axis=0)
        sat, lum = _rgb_to_hsl_sat_lum(*mean)
        out.append({
            "hex": _hex(mean),
            "weight": round(float(counts[key]) / total, 4),
            "sat": round(sat, 3),
            "lum": round(lum, 3),
        })
    return out


def _pick_accent(palette: list[dict]) -> tuple[str, float]:
    """The colour a pattern should be tinted with so it belongs to this frame.

    Wants saturation first (a grey wall is not an accent) but will not take a
    colour that occupies a sliver of the frame, because a two-pixel highlight
    is noise, not palette."""
    usable = [c for c in palette if c["weight"] >= 0.02 and 0.12 <= c["lum"] <= 0.94]
    if not usable:
        usable = palette or [{"hex": "#ffffff", "weight": 1.0, "sat": 0.0, "lum": 1.0}]
    best = max(usable, key=lambda c: c["sat"] * (0.45 + min(c["weight"], 0.35)))
    return best["hex"], float(best["sat"])


def _busy_map(gray) -> tuple[list[float], float]:
    """Per-cell detail, as the normalised gradient magnitude of each cell.

    This is what tells the planner where the frame is EMPTY — a pattern goes
    in a quiet cell, not over the speaker."""
    import numpy as np

    gy, gx = np.gradient(gray.astype("float32"))
    mag = np.hypot(gx, gy)
    h, w = mag.shape
    cells: list[float] = []
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            y0, y1 = r * h // GRID_ROWS, (r + 1) * h // GRID_ROWS
            x0, x1 = c * w // GRID_COLS, (c + 1) * w // GRID_COLS
            cells.append(float(mag[y0:y1, x0:x1].mean()))
    peak = max(cells) or 1.0
    energy = float(min(1.0, mag.mean() / 24.0))
    return [round(v / peak, 4) for v in cells], round(energy, 4)


def _gray(img):
    import numpy as np

    a = np.asarray(img.convert("RGB")).astype("float32")
    return a[:, :, 0] * 0.299 + a[:, :, 1] * 0.587 + a[:, :, 2] * 0.114


def _open_small(path: Path, long_edge: int = 320):
    from PIL import Image

    im = Image.open(path).convert("RGB")
    im.thumbnail((long_edge, long_edge))
    return im


def measure(frame: Path, *, prev: Optional[Path] = None) -> Measured:
    """Everything the pixels of one frame will say, plus a motion direction
    when the frame before it is supplied."""
    import numpy as np

    im = _open_small(frame)
    arr = np.asarray(im)
    gray = _gray(im)
    palette = _palette(arr)
    accent, accent_sat = _pick_accent(palette)
    busy, energy = _busy_map(gray)
    mean_lum = float(gray.mean())
    stddev = float(gray.std())
    # Ink: something that will actually read against this frame.
    ink = "#0b0b12" if mean_lum > 150 else "#f4f4f8"

    motion, strength = None, 0.0
    if prev is not None and prev.exists():
        motion, strength = _motion_between(prev, frame)

    return Measured(
        mean_lum=round(mean_lum, 2),
        stddev=round(stddev, 2),
        is_dark=mean_lum < 70,
        palette=palette,
        accent=accent,
        accent_sat=round(accent_sat, 3),
        ink=ink,
        busy=busy,
        quietest_cell=int(min(range(len(busy)), key=lambda i: busy[i])),
        busiest_cell=int(max(range(len(busy)), key=lambda i: busy[i])),
        edge_energy=energy,
        motion=motion,
        motion_strength=strength,
    )


def _motion_between(a: Path, b: Path) -> tuple[Optional[str], float]:
    """Coarse global motion by comparing shifted copies — enough to know which
    way the frame is travelling so a sweep can go WITH it instead of against
    it. Not optical flow, and does not pretend to be."""
    import numpy as np

    ga, gb = _gray(_open_small(a, 160)), _gray(_open_small(b, 160))
    if ga.shape != gb.shape:
        return None, 0.0
    base = float(np.abs(ga - gb).mean())
    if base < 1.0:
        return "none", 0.0
    shift = 4
    trials = {
        "left":  float(np.abs(ga[:, shift:] - gb[:, :-shift]).mean()),
        "right": float(np.abs(ga[:, :-shift] - gb[:, shift:]).mean()),
        "up":    float(np.abs(ga[shift:, :] - gb[:-shift, :]).mean()),
        "down":  float(np.abs(ga[:-shift, :] - gb[shift:, :]).mean()),
    }
    best = min(trials, key=lambda k: trials[k])
    gain = (base - trials[best]) / base
    if gain < 0.06:
        return "none", 0.0
    return best, round(float(min(1.0, gain * 4)), 3)


# ---------------------------------------------------------------------------
# Finding a frame worth using
# ---------------------------------------------------------------------------

@dataclass
class PickedFrame:
    path: Path
    at_sec: float
    measured: Measured
    fell_back: bool = False       # nothing passed the content test
    scanned: int = 0


def _extract_at(video: Path, out: Path, t: float) -> Optional[Path]:
    from PIL import Image

    try:
        media._run(
            [media.ffmpeg_exe(), "-y", "-ss", f"{max(0.0, t):.3f}", "-i", str(video),
             "-frames:v", "1", "-q:v", "2", str(out)],
            what="scan frame",
        )
    except media.MediaError:
        return None
    if not out.exists() or out.stat().st_size == 0:
        return None
    try:
        Image.open(out).verify()
    except Exception:  # noqa: BLE001 — a truncated seek write is not a frame
        return None
    return out


def _is_content(m: Measured) -> bool:
    return m.mean_lum >= MIN_MEAN_LUM and m.stddev >= MIN_STDDEV


def pick_content_frame(
    video: Path, out_dir: Path, *, edge: str, label: str = "frame",
) -> PickedFrame:
    """The last (edge="last") or first (edge="first") frame of `video` that is
    a fair picture of what the clip looks like.

    Intro cards end on a fade to black and generated clips often open on one,
    so the literal boundary frame is regularly a black rectangle. Handing that
    to a transition designer produces a transition designed for a black
    rectangle: no palette to inherit, no busy map, no subject.

    A fixed brightness floor is not enough. A frame most of the way through a
    fade clears any floor you would dare set while still being a dim ghost of
    the card, and a palette taken from it comes out muddy. So the scan reads
    the whole window first and judges each frame against the BRIGHTEST one in
    it — a fade is a relative collapse, not an absolute level — then takes the
    frame nearest the edge that is still substantially itself.

    Reports `fell_back` honestly when the clip really is dark throughout, so
    callers can stop inferring a palette from it and stop trimming its tail."""
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{label}.png"
    dur = media.probe_duration(video) or 0.0
    span = TAIL_SCAN_SEC if edge == "last" else HEAD_SCAN_SEC
    if dur <= 0:
        # Unprobeable: take the plain boundary frame and say so.
        media.extract_frame(video, final, at="last" if edge == "last" else "first")
        return PickedFrame(final, 0.0, measure(final), fell_back=True, scanned=1)

    span = min(span, max(0.0, dur - 0.05))
    steps = max(1, int(span * SCAN_FPS))
    # Ordered from the boundary INWARDS, so the first acceptable frame is the
    # one closest to the cut — the one the transition actually has to match.
    times = [
        (dur - 0.04 - i / SCAN_FPS) if edge == "last" else (0.02 + i / SCAN_FPS)
        for i in range(steps + 1)
    ]
    times = [t for t in times if 0.0 <= t <= dur]

    scratch = out_dir / f"{label}.scan.png"
    seen: list[tuple[float, Measured]] = []
    for t in times:
        if _extract_at(video, scratch, t) is None:
            continue
        seen.append((t, measure(scratch)))
    try:
        scratch.unlink(missing_ok=True)
    except OSError:
        pass

    if not seen:
        media.extract_frame(video, final, at="last" if edge == "last" else "first")
        return PickedFrame(final, 0.0, measure(final), fell_back=True,
                           scanned=len(times))

    brightest = max(m.mean_lum for _, m in seen)
    strongest = max(m.stddev for _, m in seen)
    chosen: Optional[tuple[float, Measured]] = None
    for t, m in seen:                       # already boundary-first
        if (_is_content(m)
                and m.mean_lum >= FADE_FLOOR * brightest
                and m.stddev >= FADE_FLOOR * strongest):
            chosen = (t, m)
            break
    if chosen is None:
        # Nothing is "substantially itself" — accept anything that is simply
        # not black, rather than giving up on a clip that is merely dim.
        chosen = next(((t, m) for t, m in seen if _is_content(m)), None)

    if chosen is None:
        log.warning(
            "[moments] %s: no frame with content within %.1fs of the %s edge — "
            "using the boundary frame", video.name, span, edge,
        )
        t, m = seen[0]
        _extract_at(video, final, t)
        return PickedFrame(final if final.exists() else seen[0][0], t, m,  # type: ignore[arg-type]
                           fell_back=True, scanned=len(seen))

    t, m = chosen
    if _extract_at(video, final, t) is None:
        media.extract_frame(video, final, at="last" if edge == "last" else "first")
    log.info(
        "[moments] %s %s frame at %.2fs (lum=%.1f of %.1f, sd=%.1f, %d scanned)",
        video.name, edge, t, m.mean_lum, brightest, m.stddev, len(seen),
    )
    return PickedFrame(final, round(t, 3), m, fell_back=False,
                       scanned=seen.index(chosen) + 1)


def pick_pair(video: Path, out_dir: Path, *, edge: str, label: str) -> tuple[PickedFrame, Optional[Path]]:
    """A content frame plus the frame a beat before it, so motion can be read."""
    picked = pick_content_frame(video, out_dir, edge=edge, label=label)
    back = picked.at_sec - 0.18 if edge == "last" else picked.at_sec + 0.18
    neighbour = _extract_at(video, out_dir / f"{label}.prev.png", max(0.0, back))
    if neighbour is not None:
        a, b = (neighbour, picked.path) if edge == "last" else (picked.path, neighbour)
        motion, strength = _motion_between(a, b)
        picked.measured.motion = motion
        picked.measured.motion_strength = strength
    return picked, neighbour


# ---------------------------------------------------------------------------
# Description (vision model — best-effort)
# ---------------------------------------------------------------------------

_MOMENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING", "description": "One sentence: what this frame shows."},
        "subject": {"type": "STRING", "description": "person | card | graphic | empty | other"},
        "subject_box": {
            "type": "OBJECT",
            "description": "Fractional box of the main subject, 0..1 from top-left. Omit if there is no subject.",
            "properties": {
                "x": {"type": "NUMBER"}, "y": {"type": "NUMBER"},
                "w": {"type": "NUMBER"}, "h": {"type": "NUMBER"},
            },
        },
        "keep_clear": {
            "type": "ARRAY",
            "description": "Fractional boxes nothing may cover: face, mouth, burned-in text, logo.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "what": {"type": "STRING"},
                    "x": {"type": "NUMBER"}, "y": {"type": "NUMBER"},
                    "w": {"type": "NUMBER"}, "h": {"type": "NUMBER"},
                },
            },
        },
        "mood": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "2-4 adjectives."},
        "depth": {"type": "STRING", "description": "flat | shallow | deep — how much the frame recedes."},
        "graphic_language": {
            "type": "STRING",
            "description": "Any graphic treatment already present (grid, glow, brackets, type) or 'none'.",
        },
    },
    "required": ["summary", "subject", "mood"],
}

_MOMENT_SYSTEM = """You describe ONE video frame so that a motion designer who cannot see it
can lay graphics over it safely.

Be literal. Report what is in the frame, not what it evokes. Boxes are fractions of the
frame (0..1) measured from the top-left. `keep_clear` is the important field: a face, a
mouth, burned-in captions and a logo must never be covered, so give each its box with a
little margin. If the frame is a title card with no person, say so and keep its text clear."""


async def describe(frame: Path, *, role: str) -> dict:
    """Vision description of one frame. {} when the model is unreachable —
    every caller must work without it."""
    from .vertex_client import gemini_multimodal

    try:
        out = await gemini_multimodal(
            f"This frame is the {role} of a vertical social video. Describe it.",
            images=[frame],
            system=_MOMENT_SYSTEM,
            json_schema=_MOMENT_SCHEMA,
            temperature=0.2,
            max_tokens=900,
        )
        return out if isinstance(out, dict) else {}
    except Exception as e:  # noqa: BLE001 — vision is an enhancement, not a gate
        log.warning("[moments] describe(%s) unavailable: %s", role, str(e)[:160])
        return {}


# ---------------------------------------------------------------------------
# The moment record
# ---------------------------------------------------------------------------

@dataclass
class Moment:
    kind: str                     # intro_out | scene_in | scene_out | outro_in
    ref: str                      # 'intro' | 'scene_0' | ...
    frame: Path
    at_sec: float
    measured: Measured
    described: dict = field(default_factory=dict)
    fell_back: bool = False       # the content scan found nothing
    row_id: Optional[int] = None

    # -- the text that gets embedded, and that the planner reads --------------
    def as_text(self) -> str:
        m = self.measured
        d = self.described or {}
        keep = "; ".join(
            f"{b.get('what', 'area')} at ({b.get('x', 0):.2f},{b.get('y', 0):.2f})"
            f" {b.get('w', 0):.2f}x{b.get('h', 0):.2f}"
            for b in (d.get("keep_clear") or [])
        )
        return "\n".join(filter(None, [
            f"{self.kind} ({self.ref}) at {self.at_sec:.2f}s",
            f"summary: {d.get('summary', 'not described')}",
            f"subject: {d.get('subject', 'unknown')}; mood: {', '.join(d.get('mood') or []) or 'n/a'}",
            f"graphic language: {d.get('graphic_language', 'unknown')}; depth: {d.get('depth', 'unknown')}",
            f"brightness {m.mean_lum:.0f}/255 ({'dark' if m.is_dark else 'light'}), "
            f"contrast {m.stddev:.0f}, detail {m.edge_energy:.2f}",
            f"palette: {', '.join(c['hex'] for c in m.palette[:4])}; accent {m.accent} (sat {m.accent_sat:.2f})",
            f"motion: {m.motion or 'unknown'} ({m.motion_strength:.2f})",
            f"quiet cells: {m.quiet_cells()} of a {GRID_COLS}x{GRID_ROWS} grid",
            f"keep clear: {keep}" if keep else "",
        ]))

    def to_public(self) -> dict:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "at_sec": self.at_sec,
            "fell_back": self.fell_back,
            "measured": asdict(self.measured),
            "described": self.described,
        }


async def read_moment(
    video: Path, work: Path, *, kind: str, ref: str, edge: str, describe_it: bool = True,
) -> Moment:
    """Pick the frame that matters at one edge of a clip and understand it."""
    picked, _prev = pick_pair(video, work, edge=edge, label=f"{kind}_{ref}")
    described = await describe(picked.path, role=kind.replace("_", " ")) if describe_it else {}
    return Moment(
        kind=kind, ref=ref, frame=picked.path, at_sec=picked.at_sec,
        measured=picked.measured, described=described, fell_back=picked.fell_back,
    )


# ---------------------------------------------------------------------------
# Storage + retrieval (the RAG proper)
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS yas_visual_moments (
    id          bigserial PRIMARY KEY,
    run_id      bigint,
    kind        text NOT NULL,
    ref         text NOT NULL DEFAULT '',
    at_sec      double precision,
    frame_path  text,
    measured    jsonb NOT NULL DEFAULT '{}'::jsonb,
    described   jsonb NOT NULL DEFAULT '{}'::jsonb,
    embed_text  text,
    embedding   vector(768),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS yas_visual_moments_run_idx
    ON yas_visual_moments (run_id, kind);

CREATE TABLE IF NOT EXISTS yas_transition_recipes (
    id            bigserial PRIMARY KEY,
    run_id        bigint,
    exp_id        bigint,
    seam          text NOT NULL DEFAULT 'intro_to_main',
    from_moment   bigint REFERENCES yas_visual_moments (id) ON DELETE SET NULL,
    to_moment     bigint REFERENCES yas_visual_moments (id) ON DELETE SET NULL,
    recipe        jsonb NOT NULL,
    planner       text,
    render_path   text,
    critique      jsonb,
    stars         smallint,
    notes         text,
    embed_text    text,
    embedding     vector(768),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS yas_transition_recipes_run_idx
    ON yas_transition_recipes (run_id, seam);
"""


async def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await cur.execute(_DDL)
    _SCHEMA_READY = True


async def _embed_one(text: str) -> Optional[list[float]]:
    try:
        vecs = await gemini_embed([text[:6000]])
        return vecs[0] if vecs and vecs[0] else None
    except Exception as e:  # noqa: BLE001
        log.warning("[moments] embed unavailable: %s", str(e)[:160])
        return None


async def store_moment(moment: Moment, *, run_id: Optional[int]) -> Optional[int]:
    """Persist and embed one moment. Returns its row id, or None if the DB is
    unreachable — storage is memory, not a dependency of rendering."""
    try:
        await ensure_schema()
        text = moment.as_text()
        vec = await _embed_one(text)
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO yas_visual_moments
                        (run_id, kind, ref, at_sec, frame_path, measured, described,
                         embed_text, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                    RETURNING id
                    """,
                    (run_id, moment.kind, moment.ref, moment.at_sec, str(moment.frame),
                     Jsonb(asdict(moment.measured)), Jsonb(moment.described or {}),
                     text[:6000], vec),
                )
                row = await cur.fetchone()
        moment.row_id = int(row["id"]) if row else None
        return moment.row_id
    except Exception:  # noqa: BLE001
        log.exception("[moments] store_moment failed (non-fatal)")
        return None


def seam_text(a: Moment, b: Moment) -> str:
    """The retrieval key for a seam: both sides, described together."""
    return f"SEAM {a.kind} -> {b.kind}\n\nFROM:\n{a.as_text()}\n\nTO:\n{b.as_text()}"


async def similar_seams(
    a: Moment, b: Moment, *, k: int = 4, exclude_run: Optional[int] = None,
) -> list[dict]:
    """Past transitions rendered for seams that LOOK like this one.

    Rated recipes come first regardless of distance — a star rating is a much
    stronger signal than visual similarity, and there will never be many of
    them. [] on any failure."""
    try:
        await ensure_schema()
        cols = ("id, run_id, seam, recipe, planner, critique, stars, notes, "
                "embed_text, created_at")
        out: dict[int, dict] = {}
        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""SELECT {cols} FROM yas_transition_recipes
                         WHERE stars IS NOT NULL AND (%s::bigint IS NULL OR run_id IS DISTINCT FROM %s)
                         ORDER BY stars DESC, updated_at DESC LIMIT 3""",
                    (exclude_run, exclude_run),
                )
                for r in await cur.fetchall():
                    out[int(r["id"])] = dict(r)

        vec = await _embed_one(seam_text(a, b))
        if vec and k > 0:
            async with get_conn(autocommit=True) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT {cols} FROM yas_transition_recipes
                             WHERE embedding IS NOT NULL
                               AND (%s::bigint IS NULL OR run_id IS DISTINCT FROM %s)
                             ORDER BY embedding <=> %s::vector LIMIT %s""",
                        (exclude_run, exclude_run, vec, int(k)),
                    )
                    for r in await cur.fetchall():
                        out.setdefault(int(r["id"]), dict(r))
        rows = list(out.values())
        rows.sort(key=lambda r: ((r.get("stars") or 0), int(r["id"])), reverse=True)
        return rows[: k + 3]
    except Exception:  # noqa: BLE001
        log.exception("[moments] similar_seams failed (non-fatal)")
        return []


def format_precedents(rows: list[dict]) -> str:
    """Past recipes compressed into what to repeat and what to avoid."""
    if not rows:
        return "No past transitions on record yet — this is the first."
    lines = ["PAST TRANSITIONS AT SIMILAR SEAMS (learn from these):"]
    for r in rows[:6]:
        recipe = r.get("recipe") or {}
        if isinstance(recipe, str):
            try:
                recipe = json.loads(recipe)
            except Exception:  # noqa: BLE001
                recipe = {}
        layers = ", ".join(
            f"{l.get('pattern')}/{l.get('move')}" for l in (recipe.get("layers") or [])[:4]
        )
        stars = r.get("stars")
        verdict = f"{stars}/5 from the team" if stars else "not rated"
        crit = r.get("critique") or {}
        if isinstance(crit, str):
            try:
                crit = json.loads(crit)
            except Exception:  # noqa: BLE001
                crit = {}
        lesson = (r.get("notes") or crit.get("lesson") or "").strip()
        lines.append(
            f"- [{verdict}] {recipe.get('style', 'unnamed')}: {layers or 'no layers recorded'}"
            + (f" — {lesson[:220]}" if lesson else "")
        )
    return "\n".join(lines)


async def store_recipe(
    *, run_id: Optional[int], exp_id: Optional[int], seam: str, recipe: dict,
    planner: str, from_moment: Optional[int], to_moment: Optional[int],
    render_path: Optional[str], embed_text: str,
) -> Optional[int]:
    """Persist a rendered transition so the next one can learn from it."""
    try:
        await ensure_schema()
        vec = await _embed_one(embed_text)
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO yas_transition_recipes
                        (run_id, exp_id, seam, from_moment, to_moment, recipe,
                         planner, render_path, embed_text, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                    RETURNING id
                    """,
                    (run_id, exp_id, seam, from_moment, to_moment, Jsonb(recipe),
                     planner, render_path, embed_text[:6000], vec),
                )
                row = await cur.fetchone()
        return int(row["id"]) if row else None
    except Exception:  # noqa: BLE001
        log.exception("[moments] store_recipe failed (non-fatal)")
        return None


async def rate_recipe(
    recipe_id: int, *, stars: Optional[int] = None, notes: Optional[str] = None,
    critique: Optional[dict] = None,
) -> None:
    """Fold a judgement back into the row, so retrieval starts preferring it."""
    try:
        await ensure_schema()
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE yas_transition_recipes
                       SET stars    = COALESCE(%s, stars),
                           notes    = COALESCE(%s, notes),
                           critique = COALESCE(%s, critique),
                           updated_at = now()
                     WHERE id = %s
                    """,
                    (stars, notes, Jsonb(critique) if critique is not None else None,
                     int(recipe_id)),
                )
    except Exception:  # noqa: BLE001
        log.exception("[moments] rate_recipe failed (non-fatal)")
