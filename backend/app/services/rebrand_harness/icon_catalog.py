"""The Rebrand studio's icon library — which glyph a topic icon draws.

Three sources, one id space:

  library   every Phosphor "fill" icon (MIT), baked ONCE into
            app/assets/brand42/phosphor_fill.json by
            services/remotion/scripts/make_brand42_assets.py --icon-catalog
            (~1,430 icons; brand logos are left out there and here, so a
            third-party trademark can never reach a 42 reel). Ids are
            Phosphor's ("piggy-bank"); the glyph is its SVG path data on a
            256 box — exactly what icon_render stamps and what the frontend
            draws. The same ids in Phosphor's BOLD weight sit in a separate
            phosphor_bold.json (--icon-catalog-bold): the circle and chip
            icons draw them (library_d(id, "bold")). Separate on purpose:
            phosphor_fill.json's hash (catalogue_version) keys the icon
            director's paid answers, so it never changes for a new weight.
  picks     the 60 ids the brand frame's keyword table uses
            (brand_icons / make_brand42_assets ICON_IDS), grouped Money /
            Code / People / Learning / Career / Time / Places / Fun: the grid
            the studio shows first.
  team      your team's own icons: drawn by the AI image model when someone
            PRESSES "Generate with AI" (Nano Banana, vertex_client.
            gemini_image — the only paid call in this module, and only on
            that press), or uploaded (an SVG or a picture, made into the same
            one-colour glyph by icon_import, free). A drawing is keyed to alpha,
            cleaned to its main shapes, its strokes brought to Phosphor
            bold's weight, and stored in the shared table yas_rebrand_icons
            (created lazily by the app, additive) with its PNG bytes — the
            database is shared by every dev machine and the VPS, so a glyph
            drawn on one renders on all; no storage bucket is ever used.
            Ids are "c-<sha12>": of the name's key for its first drawing, of
            "key#n" for the n-th ("Try again"), of "upload|" + the PNG's
            sha256 for an upload (the same picture is one icon); a glyph's
            pixels never change — a team icon is renamed, re-tagged or
            hidden, never redrawn.

find(q) is the ONE search, free and local: the Library box, the "nothing
fits: Generate" decision and the director's "other:<noun>" (resolve) all run
it. Every candidate scores 0..1 (MATCH_STRONG = 0.8 is a sure match):

  name      the whole query is an icon id, a studio name or alias, or a team
            icon's name                                   0.99 (team 0.995)
  synonym   a phrase of brand_icons.SYNONYMS in the query 0.93 .. 0.70
  keyword   the brand keyword table, negation-aware       0.60 .. 0.95
  tag       a Phosphor tag, said whole                    0.85 / 0.82 / 0.76
  words     ANY of its words: whole, by stem, or started  0.40 + 0.45 x cover
            (3+ letters)
  fuzzy     a close spelling (difflib >= 0.80)           0.85 x ratio

Typing never pays: nothing is re-ranked by an AI and nothing is drawn on its
own. The database and every model call sit behind small seams (the store,
vertex_client looked up at call time) so tests fake both.
"""
from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import io
import json
import logging
import os
import re
import socket
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol

from .. import brand_icons
from ...config import config

log = logging.getLogger("archivist.rebrand.harness.icons")

CATALOGUE_PATH = Path(__file__).resolve().parents[2] / "assets" / "brand42" / "phosphor_fill.json"
BOLD_PATH = CATALOGUE_PATH.with_name("phosphor_bold.json")
WEIGHTS = ("fill", "bold")
# The local copy of each team glyph's PNG (immutable per id).
CACHE_DIR = (config.asset_root.parent / "branding" / "icons").resolve()

MATCH_STRONG = 0.8                   # a match this good is sure (nothing to draw)
NAME_MAX = 60
LABEL_MAX = 16                       # a team icon's own short name (a chip's words by default)
SEARCH_MAX = 200                     # results per page, at most

# find()'s scores (module docstring).
SCORE_NAME = 0.99
SCORE_TEAM_NAME = 0.995
SYNONYM_SCORES = {"strong": (0.93, 0.90, 0.88), "related": (0.74, 0.72, 0.70)}
SYNONYM_INSIDE = 0.10                # a phrase inside a longer query: less, by the share it leaves out
WORD_WEIGHTS = {"id": 1.0, "name": 0.95, "synonym": 0.9, "tag": 0.8}
WORD_BASE, WORD_SPAN = 0.40, 0.45    # the words row: 0.40 + 0.45 x the query's words covered
WORD_ID_FIT = 0.03                   # ...plus this x the share of the icon's own id it names
PREFIX = 0.85                        # a query word (3+ letters) that only starts a word
FUZZY_CUTOFF = 0.80
FUZZY_SCALE = 0.85
NEGATED_CAP = 0.7                    # "no teachers" is never a sure match
PICK_BONUS = 0.01                    # ties: a 42 pick first...
TEAM_BONUS = 0.005                   # ...and a team icon before a library one

# The team's icons, read from the table: kept this long (a teammate's new
# icon shows within a minute), and a failed read is not retried sooner than.
TEAM_TTL_SEC = 60
TEAM_RETRY_SEC = 15
TEAM_MAX = 500
LEFT_TTL_SEC = 30                    # the search's "drawings left today" hint

# Drawing with the AI, on a press only.
DRAW_PROMPT = ("one flat solid black pictogram of {name} on pure white: bold uniform strokes about a "
               "tenth of the picture wide, rounded ends, simple geometric shapes like a Phosphor Bold "
               "icon, one shape, no text, no letters, no frame, centred, wide white margin")
DRAW_ATTEMPTS = 2
DRAW_TIMEOUT_SEC = 120
DRAW_BACKOFF_SEC = 600               # a name whose drawings were not clean icons: not drawn (billed) again for this long
DRAW_DOWN_SEC = 60                   # ... the model was down: asked again after this
DRAW_PER_DAY = 25                    # the whole team's AI drawings a day (env ICON_DRAWS_PER_DAY)
DRAW_WINDOW_H = 24
DRAW_COST_USD = 0.04                 # one drawing, when vertex_client's price table lacks the model
GLYPH_PX = 256                       # stored custom glyphs: this square, like the Phosphor box
GLYPH_MARGIN = 16                    # px kept clear inside it (Phosphor's own padding)
INK_RANGE = (0.03, 0.70)             # share of the drawing that is ink
EDGE_TOUCH = 0.01                    # "touching" an edge = within this share of it
WORK_PX = 1024                       # a drawing is squared and weighed at this size
BOLD_STROKE = 24 / 256               # Phosphor bold's stroke, a share of the glyph box
WEIGHT_RANGE = (0.75, 1.35)          # strokes within this x BOLD_STROKE are left as they are...
WEIGHT_STEP_MAX = 0.5                # ...others made bolder / thinner by at most this share
SOLID_SHARE = 0.30                   # a "stroke" wider than this share of the box is a solid shape
PREVIEW_PX = 112                     # the Generate card's pictures (the circle's L)

BRAND_WHY = "That's a brand, so it isn't drawn — pick an icon instead"
CAP_WHY = "Today's {n} AI drawings are used — try again tomorrow"
TABLE_DOWN = "Your team's icons can't be reached right now — try again in a minute"

CUSTOM_ID = re.compile(r"^c-[0-9a-f]{12}$")

# The 42 picks: the 60 ids of brand_icons' keyword table (and the
# composition's ICON_IDS), grouped for the studio's grid.
PICKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Money", ("piggy-bank", "coins", "gift", "calculator", "lock-key-open")),
    ("Code", ("code", "terminal-window", "git-branch", "laptop", "cpu", "robot", "wifi-high",
              "shield-check")),
    ("People", ("users-three", "users", "student", "chalkboard-teacher", "handshake", "chats-circle",
                "hand-waving", "microphone", "heart")),
    ("Learning", ("graduation-cap", "book-open", "lightbulb", "brain", "puzzle-piece", "question",
                  "magnifying-glass", "certificate", "atom")),
    ("Career", ("briefcase", "buildings", "trophy", "chart-line-up", "target", "rocket-launch",
                "clipboard-text", "seal-check", "flag")),
    ("Time", ("clock", "hourglass", "calendar-blank", "moon-stars", "infinity")),
    ("Places", ("swimming-pool", "globe-hemisphere-east", "map-pin", "house", "airplane-tilt",
                "compass", "mountains", "footprints")),
    ("Fun", ("game-controller", "sparkle", "barbell", "coffee", "smiley", "palette", "star")),
)
PICK_IDS: tuple[str, ...] = tuple(i for _, ids in PICKS for i in ids)

# Display name + aliases per id (Developer -> code, Free -> piggy-bank, ...).
NAMES = brand_icons.NAMES


class DrawError(RuntimeError):
    """A glyph could not be drawn; the message is fit for the UI.
    `status`: the answer to give (422 a brand or a bad drawing, 429 the
    day's drawings are used, 503 the model or the table is down); `backoff`:
    how long the same name is answered with it, with no new call."""

    def __init__(self, message: str, *, status: int = 422, backoff: float = 0.0):
        super().__init__(message)
        self.status = int(status)
        self.backoff = float(backoff)


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

_STOP = frozenset("a an the of and or for to in on at with my our your their its is are be "
                  "icon icons symbol sign logo picture".split())


def name_key(name: str) -> str:
    """A name as it is compared and stored: accents dropped, lower case,
    "-" / "_" read as spaces, other punctuation gone ("/" kept for 24/7),
    spaces collapsed, at most NAME_MAX characters."""
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"[-_]+", " ", s)
    s = re.sub(r"[^\w\s/']+", " ", s)
    s = re.sub(r"'", "", s)
    return re.sub(r"\s+", " ", s).strip()[:NAME_MAX].strip()


def _stem(tok: str) -> str:
    for suf in ("ing", "ers", "er", "es", "s"):
        if len(tok) > len(suf) + 2 and tok.endswith(suf):
            return tok[: -len(suf)]
    return tok


def _tokens(text: str) -> list[str]:
    return [t for t in name_key(text).split() if t]


def _content(tokens: Iterable[str]) -> list[str]:
    return [t for t in tokens if t not in _STOP]


def _display(name: str) -> str:
    """A typed name as it is shown and stored: spaces collapsed, trimmed."""
    return re.sub(r"\s+", " ", str(name or "")).strip()[:NAME_MAX].strip()


def _letters(key: str) -> int:
    return len(re.sub(r"[\W_]", "", key))


# ---------------------------------------------------------------------------
# The library
# ---------------------------------------------------------------------------

@dataclass
class _Entry:
    id: str
    name: str
    d: str
    tags: tuple[str, ...]
    categories: tuple[str, ...]
    pick: bool
    label: Optional[str]                         # NAMES display name
    aliases: tuple[str, ...]                     # NAMES display + aliases (keys)
    words: frozenset = field(default_factory=frozenset)     # every token (id, name, aliases, tags)
    stems: frozenset = field(default_factory=frozenset)


@dataclass
class _Catalogue:
    version: str
    source: str
    icons: dict[str, _Entry]
    exact: dict[str, list[str]]                  # key -> ids (id spaced, NAMES display / alias)
    tags: dict[str, list[str]]                   # tag key -> ids
    strings: dict[str, set[str]]                 # difflib vocabulary -> ids
    brands: frozenset = frozenset()              # names of the logos left out of the library


def _is_brand(icon_id: str, categories: Iterable[str]) -> bool:
    return icon_id.endswith("-logo") or "brands" in set(categories)


@lru_cache(maxsize=1)
def _catalogue() -> _Catalogue:
    raw = CATALOGUE_PATH.read_bytes()
    data = json.loads(raw)
    icons_raw = data.get("icons", data)
    labels: dict[str, str] = {}
    aliases: dict[str, list[str]] = {}
    for display, icon_id, als in NAMES:
        labels.setdefault(icon_id, display)
        aliases.setdefault(icon_id, []).extend([name_key(display), *(name_key(a) for a in als)])
    picks = set(PICK_IDS)
    icons: dict[str, _Entry] = {}
    exact: dict[str, list[str]] = {}
    tags: dict[str, list[str]] = {}
    strings: dict[str, set[str]] = {}
    for icon_id in sorted(icons_raw):
        v = icons_raw[icon_id]
        cats = tuple(v.get("categories") or ())
        if _is_brand(icon_id, cats):
            continue
        tg = tuple(dict.fromkeys(name_key(t) for t in v.get("tags") or () if name_key(t)))
        als = tuple(dict.fromkeys(aliases.get(icon_id, ())))
        spaced = icon_id.replace("-", " ")
        words = {w for s in (spaced, *als, *tg) for w in s.split()}
        e = _Entry(id=icon_id, name=str(v.get("name") or spaced.capitalize()), d=str(v["d"]),
                   tags=tg, categories=cats, pick=icon_id in picks, label=labels.get(icon_id),
                   aliases=als, words=frozenset(words), stems=frozenset(_stem(w) for w in words))
        icons[icon_id] = e
        for k in (spaced, *als):
            exact.setdefault(k, []).append(icon_id)
        for t in tg:
            tags.setdefault(t, []).append(icon_id)
        for s in (spaced, *als, *tg):
            strings.setdefault(s, set()).add(icon_id)
    # Line endings do not count: git (core.autocrlf) checks the file out with
    # CRLF on Windows and LF on the VPS, and both must key caches alike.
    lf = raw.replace(b"\r\n", b"\n")
    version = f"phosphor-fill-{hashlib.sha256(lf).hexdigest()[:10]}"
    brands = frozenset(name_key(b) for b in data.get("brands") or () if name_key(b))
    return _Catalogue(version=version, source=str(data.get("source") or ""), icons=icons,
                      exact=exact, tags=tags, strings=strings, brands=brands)


def catalogue_version() -> str:
    """Changes whenever the baked library changes (keys caches on it).
    The FILL file only: the icon director's paid cache is keyed on it, so
    the bold file (bold_version) never moves it."""
    return _catalogue().version


@dataclass
class _Bold:
    version: str
    icons: dict[str, str]                        # id -> bold path data


@lru_cache(maxsize=1)
def _bold() -> _Bold:
    """phosphor_bold.json: the library's ids in Phosphor's bold weight (an
    id it lacks is drawn in fill; see library_d). A checkout without it
    draws everything in fill."""
    try:
        raw = BOLD_PATH.read_bytes()
    except OSError:
        log.warning("[icons] %s is missing: bold icons are drawn in fill", BOLD_PATH.name)
        return _Bold(version="phosphor-bold-none", icons={})
    data = json.loads(raw)
    icons = {k: str(v["d"]) for k, v in (data.get("icons") or {}).items() if v.get("d")}
    # Line endings do not count (git's core.autocrlf), as for the fill file.
    lf = raw.replace(b"\r\n", b"\n")
    return _Bold(version=f"phosphor-bold-{hashlib.sha256(lf).hexdigest()[:10]}", icons=icons)


def bold_version() -> str:
    """Changes whenever the baked bold weight changes. Keys only what draws
    bold (sprites); never the director (catalogue_version)."""
    return _bold().version


_BOLD_MISSING_LOGGED: set[str] = set()


def count() -> int:
    return len(_catalogue().icons)


def library_ids() -> list[str]:
    return list(_catalogue().icons)


def is_custom_id(icon_id: str) -> bool:
    return bool(CUSTOM_ID.match(str(icon_id or "")))


def library_d(icon_id: str, weight: str = "fill") -> Optional[str]:
    """A library icon's SVG path data (256 box) in `weight` ("fill" |
    "bold"), or None when the library has no such icon. A bold shape the
    bold file lacks falls back to the fill shape (logged once)."""
    e = _catalogue().icons.get(str(icon_id or ""))
    if not e:
        return None
    if weight not in WEIGHTS:
        raise ValueError(f"no such icon weight: {weight!r}")
    if weight == "fill":
        return e.d
    d = _bold().icons.get(e.id)
    if d is None:
        if e.id not in _BOLD_MISSING_LOGGED:
            _BOLD_MISSING_LOGGED.add(e.id)
            log.info("[icons] no bold shape for %s: drawn in fill", e.id)
        return e.d
    return d


def icon(icon_id: str) -> Optional[dict]:
    """{id, name, label, d, tags, categories, pick} of a library icon, or None."""
    e = _catalogue().icons.get(str(icon_id or ""))
    if not e:
        return None
    return {"id": e.id, "name": e.name, "label": e.label, "d": e.d, "tags": list(e.tags),
            "categories": list(e.categories), "pick": e.pick}


def picks_public() -> list[dict]:
    """[{group, items: [{id, name, label, d, b}]}] — the 42 picks grid
    (`b`: the bold shape, which the circle and chip icons draw)."""
    cat = _catalogue().icons
    return [{"group": g, "items": [{"id": i, "name": cat[i].name, "label": cat[i].label, "d": cat[i].d,
                                    "b": library_d(i, "bold")}
                                   for i in ids if i in cat]} for g, ids in PICKS]


def catalogue_public() -> dict:
    """{version, picks, count}: GET /icons/catalogue's library part (the
    router adds styles, entrances, sizes, rules and what a drawing costs)."""
    return {"version": catalogue_version(), "picks": picks_public(), "count": count()}


# ---------------------------------------------------------------------------
# The search: find()
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Synonym:
    phrase: str                                  # name_key'd
    stems: tuple[str, ...]                       # its words, stemmed
    strength: str                                # "strong" | "related"
    ids: tuple[str, ...]


@dataclass
class _Index:
    """What the words row looks words up in: word -> {id: weight} (the
    heaviest role the word plays for that icon), the same by stem, and every
    word (for "started" words)."""
    words: dict[str, dict[str, float]]
    stems: dict[str, dict[str, float]]
    vocab: tuple[str, ...]
    own: dict[str, tuple[str, ...]]              # id -> the words of its own name (id / team name)


def _index(entries: Iterable[tuple[str, Iterable[tuple[str, str]], tuple[str, ...]]]) -> _Index:
    """entries: (id, [(text, role)], own words) -> an _Index."""
    words: dict[str, dict[str, float]] = {}
    stems: dict[str, dict[str, float]] = {}
    own: dict[str, tuple[str, ...]] = {}
    for gid, texts, mine in entries:
        own[gid] = tuple(mine)
        for text, role in texts:
            k = WORD_WEIGHTS[role]
            for w in text.split():
                row = words.setdefault(w, {})
                row[gid] = max(row.get(gid, 0.0), k)
                srow = stems.setdefault(_stem(w), {})
                srow[gid] = max(srow.get(gid, 0.0), k)
    return _Index(words=words, stems=stems, vocab=tuple(sorted(words)), own=own)


@lru_cache(maxsize=1)
def _synonyms() -> tuple[_Synonym, ...]:
    """brand_icons.SYNONYMS, stemmed, the longest phrases first (a phrase
    found inside a longer one it is part of does not count again)."""
    cat = _catalogue().icons
    out = [_Synonym(phrase=name_key(p), stems=tuple(_stem(t) for t in name_key(p).split()), strength=s,
                    ids=tuple(i for i in ids if i in cat))
           for p, s, ids in brand_icons.SYNONYMS]
    return tuple(sorted(out, key=lambda s: -len(s.stems)))


@lru_cache(maxsize=1)
def _library_index() -> _Index:
    cat = _catalogue()
    # A one-word strong synonym is a word of its icons too ("dor" starts
    # "dorm": the bed). A longer phrase is not: its words alone ("free" of
    # "free education") would be sure matches they are not.
    by_id: dict[str, list[tuple[str, str]]] = {}
    for syn in _synonyms():
        if syn.strength == "strong" and len(syn.stems) == 1:
            for gid in syn.ids:
                by_id.setdefault(gid, []).append((syn.phrase, "synonym"))
    entries = []
    for e in cat.icons.values():
        texts = [(e.id.replace("-", " "), "id"), *((a, "name") for a in e.aliases),
                 *((t, "tag") for t in e.tags), *by_id.get(e.id, ())]
        entries.append((e.id, texts, tuple(e.id.split("-"))))
    return _index(entries)


def _team_rows(team: Optional[Iterable[dict]]) -> dict[str, dict]:
    """The team's icons as find() reads them: id -> {id, name, label, tags,
    origin, key, keys, tags_k} (hidden ones are never listed)."""
    out: dict[str, dict] = {}
    for r in team or ():
        gid = str(r.get("id") or "")
        if not is_custom_id(gid) or r.get("hidden"):
            continue
        name = str(r.get("name") or "")
        label = str(r.get("label") or "") or name[:LABEL_MAX]
        tags = [name_key(t) for t in r.get("tags") or () if name_key(t)]
        out[gid] = {"id": gid, "name": name, "label": label, "tags": tags,
                    "origin": str(r.get("origin") or "ai"),
                    "keys": {k for k in (name_key(name), name_key(label)) if k}}
    return out


def _team_index(rows: dict[str, dict]) -> _Index:
    return _index((gid, [*((k, "name") for k in r["keys"]), *((t, "tag") for t in r["tags"])],
                   tuple(name_key(r["name"]).split())) for gid, r in rows.items())


def _word_scores(toks: list[str], idx: _Index) -> dict[str, float]:
    """The words row for one index: id -> 0.40 + 0.45 x (sum of each query
    word's best hit / the query's words) + a little for naming the icon's own
    id. A query word hits a word whole (or by its stem) at its role's weight
    (id 1.0, name 0.95, synonym 0.9, tag 0.8), or starts it (3+ letters) at
    0.85 of that — OR, not AND: "job offer" finds the briefcase by "job"."""
    if not toks:
        return {}
    best: dict[str, list[float]] = {}
    for i, t in enumerate(toks):
        hits: dict[str, float] = {}
        for row in (idx.words.get(t), idx.stems.get(_stem(t))):
            for gid, k in (row or {}).items():
                hits[gid] = max(hits.get(gid, 0.0), k)
        if len(t) >= 3:
            for w in idx.vocab:
                if w != t and w.startswith(t):
                    for gid, k in idx.words[w].items():
                        hits[gid] = max(hits.get(gid, 0.0), PREFIX * k)
        for gid, k in hits.items():
            best.setdefault(gid, [0.0] * len(toks))[i] = k
    out: dict[str, float] = {}
    tstems = [(t, _stem(t)) for t in toks]
    for gid, row in best.items():
        mine = idx.own.get(gid) or ()
        named = sum(1 for w in mine if any(w == t or _stem(w) == s or (len(t) >= 3 and w.startswith(t))
                                           for t, s in tstems))
        fit = WORD_ID_FIT * named / len(mine) if mine else 0.0
        out[gid] = WORD_BASE + WORD_SPAN * sum(row) / len(toks) + fit
    return out


def _synonym_hits(key: str) -> list[tuple[str, float, bool]]:
    """(id, score, whole) of every SYNONYMS phrase found in the query, whole
    words by stem: the whole query 0.93 / 0.90 / 0.88 (strong) or 0.74 /
    0.72 / 0.70 (related) for the 1st / 2nd / 3rd id, a phrase inside a
    longer query less by the share it leaves out. A longer phrase takes its
    words first ("3d printing" is not also "3d"); one said inside a negation
    ("no school") does not count."""
    spans = [(m.start(), m.group()) for m in re.finditer(r"\S+", key)]
    qs = [_stem(w) for _, w in spans]
    n = len(qs)
    taken = [False] * n
    out: list[tuple[str, float, bool]] = []
    for syn in _synonyms():
        m = len(syn.stems)
        if not m or m > n:
            continue
        for at in range(n - m + 1):
            if tuple(qs[at:at + m]) != syn.stems or any(taken[at:at + m]):
                continue
            for j in range(at, at + m):
                taken[j] = True
            whole = m == n
            if not whole and brand_icons.negated_in_clause(key, spans[at][0]):
                continue
            less = 0.0 if whole else SYNONYM_INSIDE * (1.0 - m / n)
            for rank, gid in enumerate(syn.ids[:3]):
                out.append((gid, SYNONYM_SCORES[syn.strength][rank] - less, whole))
    return out


def _keyword_spans(text: str) -> list[tuple[str, int, int]]:
    """brand_icons' keyword table on `text`: (icon, tier, matched length) of
    each icon's first mention that is not negated (find_hits' rules)."""
    out: dict[str, tuple[str, int, int]] = {}
    for tier, icon_id, rx in brand_icons._COMPILED:
        m = next((m for m in rx.finditer(text) if not brand_icons._negated(text, m.start())), None)
        if not m:
            continue
        old = out.get(icon_id)
        got = (icon_id, tier, m.end() - m.start())
        if old is None or (tier, -got[2]) < (old[1], -old[2]):
            out[icon_id] = got
    return list(out.values())


@lru_cache(maxsize=1)
def _library_strings() -> tuple[str, ...]:
    return tuple(_catalogue().strings)


def _scores(key: str, team: dict[str, dict]) -> dict[str, tuple[float, str]]:
    """id -> (score before the tie bonuses, via) of every icon the query
    finds (module docstring)."""
    cat = _catalogue()
    best: dict[str, tuple[float, str]] = {}
    negated = brand_icons.negated_in_clause(key, len(key))
    synonyms = _synonym_hits(key)
    # A phrase of the synonym table that IS the query says what it means:
    # an icon found only by a tag, a word or a spelling stays under its
    # icons ("stress" is the nervous smiley, not the italic's typographic
    # tag "stress").
    sense = {gid for gid, _, whole in synonyms if whole}
    under = min((sc for _, sc, whole in synonyms if whole), default=None)

    def offer(gid: str, score: float, via: str) -> None:
        if gid not in cat.icons and gid not in team:
            return
        if negated and via in ("words", "tag", "fuzzy"):
            score = min(score, NEGATED_CAP)
        if under is not None and gid not in sense and via in ("words", "tag", "fuzzy"):
            score = min(score, under - 0.01)
        old = best.get(gid)
        if old is None or score > old[0]:
            best[gid] = (score, via)

    # Said exactly: an icon id, a studio name or alias, a team icon's name.
    for gid in cat.exact.get(key, ()):
        offer(gid, SCORE_NAME, "name")
    for gid, r in team.items():
        if key in r["keys"]:
            offer(gid, SCORE_TEAM_NAME, "name")
    for gid, score, _ in synonyms:
        offer(gid, score, "synonym")
    # The keyword table: scored by how much of the query the keyword covers.
    for gid, tier, n in _keyword_spans(key):
        cover = min(1.0, n / max(1, len(key)))
        offer(gid, (0.6 + 0.35 * cover) if tier == 0 else (0.5 + 0.3 * cover), "keyword")
    # A tag said whole. Only a tag few icons share is sure: "apple" tags four
    # Apple-product icons and "food" dozens (ids that contain the word first).
    toks = key.split()
    qtoks = _content(toks) or toks
    tagged = [*cat.tags.get(key, ()), *(gid for gid, r in team.items() if key in r["tags"])]
    base = 0.85 if len(tagged) == 1 else 0.82 if len(tagged) <= 3 else 0.76
    for gid in tagged:
        own = gid.split("-") if gid in cat.icons else name_key(team[gid]["name"]).split()
        offer(gid, base + (0.03 if any(t in own for t in qtoks) else 0.0), "tag")
    # Any of its words (OR).
    for idx in (_library_index(), *((_team_index(team),) if team else ())):
        for gid, score in _word_scores(qtoks, idx).items():
            offer(gid, score, "words")
    # Close spellings (difflib's quick filters keep this fast); the query
    # itself was scored above, by what it exactly is.
    vocab = [*_library_strings(), *sorted({k for r in team.values() for k in r["keys"]} - cat.strings.keys())]
    for s in difflib.get_close_matches(key, vocab, n=12, cutoff=FUZZY_CUTOFF):
        if s == key:
            continue
        r = difflib.SequenceMatcher(None, key, s).ratio()
        for gid in [*cat.strings.get(s, ()), *(g for g, row in team.items() if s in row["keys"])]:
            offer(gid, FUZZY_SCALE * r, "fuzzy")
    return best


def _item(gid: str, team: dict[str, dict], score: Optional[float], via: Optional[str]) -> dict:
    """One result: {id, source, origin, name, label, d, b, tags, score, via}
    (`d` / `b` the fill / bold paths of a library icon, None for a team
    icon, whose PNG the router adds as png_url)."""
    e = _catalogue().icons.get(gid)
    if e is not None:
        return {"id": gid, "source": "library", "origin": "phosphor", "name": e.name, "label": e.label,
                "d": e.d, "b": library_d(gid, "bold"), "tags": list(e.tags), "score": score, "via": via}
    r = team[gid]
    return {"id": gid, "source": "custom", "origin": r["origin"], "name": r["name"], "label": r["label"],
            "d": None, "b": None, "tags": list(r["tags"]), "score": score, "via": via}


def find(q: str = "", *, limit: int = 60, offset: int = 0, team: Optional[Iterable[dict]] = None,
         library_only: bool = False) -> dict:
    """The one icon search: free, local, pure (module docstring).

    Returns {"query", "total", "items": [{id, source, origin, name, label, d,
             b, tags, score, via}] (best first, then by id), "strong": the
             best >= MATCH_STRONG, "best": its score, "via": how it was found}.
    `team`: the team's icons (team_index rows) to search too, unless
    `library_only` (the director's resolver). An empty query lists the 42
    picks first, then the rest of the library A-Z."""
    cat = _catalogue().icons
    offset, limit = max(0, int(offset)), max(0, min(SEARCH_MAX, int(limit)))
    key = name_key(q)
    rows = {} if library_only else _team_rows(team)
    if not key:
        order = [i for i in PICK_IDS if i in cat] + [i for i in cat if i not in set(PICK_IDS)]
        return {"query": str(q or ""), "total": len(order), "strong": False, "best": 0.0, "via": "none",
                "items": [_item(i, rows, None, None) for i in order[offset:offset + limit]]}
    ranked = []
    for gid, (score, via) in _scores(key, rows).items():
        bonus = (PICK_BONUS if gid in cat and cat[gid].pick else 0.0) + (TEAM_BONUS if gid in rows else 0.0)
        ranked.append((round(min(1.0, score + bonus), 3), gid, via))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    best, via = (ranked[0][0], ranked[0][2]) if ranked else (0.0, "none")
    return {"query": str(q or ""), "total": len(ranked), "strong": best >= MATCH_STRONG, "best": best,
            "via": via, "items": [_item(g, rows, s, v) for s, g, v in ranked[offset:offset + limit]]}


def resolve(name: str) -> Optional[tuple[str, float, str]]:
    """The library's best icon for a name, (id, score, via) — find() on the
    library alone, free. The icon director's "other:<noun>" (it keeps it
    only at MATCH_STRONG or more). None when nothing is found."""
    if not name_key(name):
        return None
    got = find(name, limit=1, library_only=True)
    if not got["items"]:
        return None
    it = got["items"][0]
    return it["id"], float(it["score"]), str(it["via"])


async def search(q: str = "", *, offset: int = 0, limit: int = 60) -> dict:
    """GET /icons/search: find() over the library and the team's icons,
    plus `generate` — whether "Generate with AI" may be offered for the
    query (generate_offer). Free: the team's icons are read from the table
    (cached a minute), never drawn."""
    team = await team_index()
    got = find(q, limit=limit, offset=offset, team=team)
    got["generate"] = await generate_offer(q) if name_key(q) else _offer_base(q)
    return got


# ---------------------------------------------------------------------------
# Team glyphs: drawn by the AI on a press (and, later, uploaded)
# ---------------------------------------------------------------------------

def custom_id(name: str) -> str:
    """The id a name's first drawing is stored under ('c-' + 12 hex of its key)."""
    return "c-" + hashlib.sha256(name_key(name).encode()).hexdigest()[:12]


def draw_id(key: str, take: int) -> str:
    """The id of a name key's `take`-th drawing: custom_id for the first,
    then 'c-' + 12 hex of "key#n" — a new take is a new glyph, never the
    old one's pixels changed."""
    if take <= 0:
        return custom_id(key)
    return "c-" + hashlib.sha256(f"{name_key(key)}#{int(take)}".encode()).hexdigest()[:12]


def is_brand_name(name: str) -> bool:
    """True for a name the image model would answer with a company's logo:
    a brand whose logo the library leaves out ("instagram"), such a brand
    inside a longer name ("my tiktok video"; brands of 4+ letters), or any
    "... logo". Such names are searched, never drawn."""
    key = name_key(name)
    if not key:
        return False
    if key == "logo" or key.endswith(" logo") or key.startswith("logo "):
        return True
    brands = _catalogue().brands
    if key in brands:
        return True
    padded = f" {key} "
    return any(len(b) >= 4 and f" {b} " in padded for b in brands)


def key_drawing(data: bytes) -> tuple[Any, dict]:
    """The image model's picture keyed to alpha (1 - luma against the
    paper, near-white and near-black snapped): (alpha float32 0..1 at up to
    768 px, report {size}). Pure. Raises DrawError when it is not a black
    drawing on a white page."""
    import numpy as np
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            rgb = np.asarray(im.convert("RGB"))
    except Exception as e:  # noqa: BLE001
        raise DrawError("The drawing could not be read") from e
    h, w = rgb.shape[:2]
    if min(h, w) < 32:
        raise DrawError("The drawing is too small")
    if max(h, w) > 768:
        k = 768 / max(h, w)
        rgb = np.asarray(Image.fromarray(rgb).resize((max(1, round(w * k)), max(1, round(h * k))), Image.BOX))
        h, w = rgb.shape[:2]
    f = rgb.astype(np.float32)
    luma = (0.2126 * f[..., 0] + 0.7152 * f[..., 1] + 0.0722 * f[..., 2]) / 255.0
    # The paper: the brightest common tone (ink covers at most 70 %, so the
    # 90th percentile is paper). Keyed relative to it, an off-white page
    # still reads as clear.
    paper = float(np.percentile(luma, 90))
    if paper < 0.75:
        raise DrawError("The drawing is not on a white background")
    alpha = np.clip(((1.0 - luma / paper) - 0.1) / 0.8, 0.0, 1.0).astype(np.float32)
    ink_mask = alpha > 0.5
    chroma = f.max(axis=-1) - f.min(axis=-1)
    if ink_mask.any() and float((chroma[ink_mask] > 80).mean()) > 0.25:
        raise DrawError("The drawing is not flat black")
    return alpha, {"size": [w, h]}


def stroke_width(alpha: Any) -> Optional[float]:
    """A shape's usual stroke width in px: twice the distance to its edge
    along its ridge (the distance transform's local maxima), the median —
    each ridge point weighed by that distance, so the one-pixel nubs a
    curved edge leaves on the ridge do not outvote a disc's centre. None for
    an empty shape. Pure."""
    import cv2
    import numpy as np

    solid = (np.asarray(alpha) > 0.5).astype(np.uint8)
    if not solid.any():
        return None
    dt = cv2.distanceTransform(solid, cv2.DIST_L2, 5)
    ridge = (dt >= cv2.dilate(dt, np.ones((3, 3), np.uint8))) & (dt >= 1.0)
    vals = np.sort(dt[ridge].astype(np.float64))
    if not vals.size:
        return None
    cum = np.cumsum(vals)
    return float(2.0 * vals[int(np.searchsorted(cum, cum[-1] / 2.0))])


def _pieces(alpha: Any) -> tuple[int, int]:
    """(pieces of ink, holes in them) of a glyph (alpha > 0.5): what matching
    its weight may never change. Pure."""
    import cv2
    import numpy as np

    solid = (np.asarray(alpha) > 0.5).astype(np.uint8)
    ink = cv2.connectedComponents(solid, connectivity=8)[0] - 1
    # The clear parts, the outside joined by a border: minus the ink's own
    # label and the outside, the rest are holes.
    clear = np.pad(1 - solid, 1, constant_values=1)
    return ink, cv2.connectedComponents(clear, connectivity=4)[0] - 2


def match_weight(alpha: Any) -> tuple[Any, dict]:
    """A glyph's strokes brought towards Phosphor bold's (BOLD_STROKE of the
    square it is drawn on): measured with stroke_width; within WEIGHT_RANGE
    x the target they stay, else they are dilated or eroded towards it by at
    most WEIGHT_STEP_MAX of their own width (fine detail is never crushed);
    a solid shape (a "stroke" wider than SOLID_SHARE of the box) is left
    alone. Never so far that a piece or a hole comes or goes (_pieces): a
    filled icon's knockout thinned through its edge, two strokes made bolder
    into one — the step is halved until it doesn't, else the glyph stays as
    it is. Returns (alpha, {weight: kept|bolder|thinner|solid|empty,
    weight_from, weight_to} — widths in px of a GLYPH_PX glyph). Pure."""
    import cv2
    import numpy as np

    a = np.asarray(alpha, dtype=np.float32)
    px = max(a.shape[:2])
    to_glyph = GLYPH_PX / float(px)
    w = stroke_width(a)
    if w is None:
        return a, {"weight": "empty", "weight_from": None, "weight_to": None}
    report = {"weight": "kept", "weight_from": round(w * to_glyph, 1), "weight_to": round(w * to_glyph, 1)}
    target = BOLD_STROKE * px
    if w > SOLID_SHARE * px:
        return a, {**report, "weight": "solid"}
    if WEIGHT_RANGE[0] * target <= w <= WEIGHT_RANGE[1] * target:
        return a, report
    want = min(max(target, w * (1.0 - WEIGHT_STEP_MAX)), w * (1.0 + WEIGHT_STEP_MAX))
    r = int(round(abs(want - w) / 2.0))
    shape = _pieces(a) if r >= 1 else None
    while r >= 1:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        out = cv2.dilate(a, kernel) if want > w else cv2.erode(a, kernel)
        if _pieces(out) == shape:
            break
        r //= 2
    if r < 1:
        return a, report
    got = stroke_width(out)
    return out, {**report, "weight": "bolder" if want > w else "thinner",
                 "weight_to": round((got if got is not None else want) * to_glyph, 1)}


def _squared(alpha: Any, px: int, margin: int) -> Any:
    """`alpha` cropped to its shapes (anti-aliasing included), squared and
    centred in a px square with `margin` clear (float32 0..1)."""
    import numpy as np
    from PIL import Image

    ys, xs = np.nonzero(alpha > 0.02)
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
    side = max(y1 - y0, x1 - x0)
    canvas = np.zeros((side, side), np.float32)
    oy, ox = (side - (y1 - y0)) // 2, (side - (x1 - x0)) // 2
    canvas[oy:oy + (y1 - y0), ox:ox + (x1 - x0)] = alpha[y0:y1, x0:x1]
    inner = px - 2 * margin
    img = Image.fromarray(np.clip(np.rint(canvas * 255), 0, 255).astype(np.uint8), "L")
    img = img.resize((inner, inner), Image.BOX if side >= inner else Image.LANCZOS)
    out = np.zeros((px, px), np.float32)
    out[margin:margin + inner, margin:margin + inner] = np.asarray(img, dtype=np.float32) / 255.0
    return out


def clean_glyph(alpha: Any, *, ink_range: tuple[float, float] = INK_RANGE, max_edges: int = 2,
                weight: bool = True, what: str = "drawing") -> tuple[bytes, dict]:
    """A keyed picture (alpha 0..1) -> the stored glyph: a GLYPH_PX square
    RGBA PNG, black ink whose alpha is the shape. Pure.

    Cleaned to its main shapes (specks under 3 % of the largest go), then
    checked: ink must cover `ink_range` of the picture and may touch at most
    `max_edges` of its edges (more = a filled background or a frame, not a
    pictogram). Squared at WORK_PX, its strokes matched to Phosphor bold
    (match_weight, when `weight`), then cropped to the shapes and centred in
    GLYPH_PX with GLYPH_MARGIN clear. Returns (png bytes, report {ink,
    shapes, dropped, edges, weight...}); raises DrawError with the reason
    (about the `what`: an AI "drawing", an uploaded "icon")."""
    import cv2
    import numpy as np
    from PIL import Image

    alpha = np.asarray(alpha, dtype=np.float32)
    h, w = alpha.shape[:2]
    solid = (alpha > 0.5).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(solid, connectivity=8)
    if n <= 1:
        raise DrawError(f"The {what} is empty")
    areas = stats[1:, cv2.CC_STAT_AREA]
    keep_ids = [i + 1 for i, a in enumerate(areas) if a >= 0.03 * areas.max()]
    keep = np.isin(labels, keep_ids).astype(np.uint8)
    keep = cv2.dilate(keep, np.ones((5, 5), np.uint8))
    alpha = alpha * keep
    ink_mask = alpha > 0.5
    ink = float(ink_mask.mean())
    if ink < ink_range[0]:
        raise DrawError(f"The {what} is too faint or too small")
    if ink > ink_range[1]:
        raise DrawError(f"The {what} is mostly ink, not a pictogram")
    ys, xs = np.nonzero(ink_mask)
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
    mx, my = EDGE_TOUCH * w, EDGE_TOUCH * h
    touches = sum((x0 <= mx, y0 <= my, x1 >= w - mx, y1 >= h - my))
    if touches > max_edges:
        raise DrawError(f"The {what} fills the picture edge to edge")
    report: dict[str, Any] = {"ink": round(ink, 3), "shapes": len(keep_ids),
                              "dropped": int(len(areas) - len(keep_ids)), "edges": int(touches)}
    if weight:
        work, weighed = match_weight(_squared(alpha, WORK_PX, GLYPH_MARGIN * WORK_PX // GLYPH_PX))
        report.update(weighed)
    else:
        work = alpha
    glyph = _squared(work, GLYPH_PX, GLYPH_MARGIN)
    out = np.zeros((GLYPH_PX, GLYPH_PX, 4), np.uint8)
    out[..., 3] = np.clip(np.rint(glyph * 255), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(out, "RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue(), report


def prepare_drawing(data: bytes) -> tuple[bytes, dict]:
    """The image model's picture -> the stored glyph (key_drawing, then
    clean_glyph with the drawing rules): (png bytes, report); raises
    DrawError with the reason."""
    alpha, first = key_drawing(data)
    png, report = clean_glyph(alpha)
    return png, {**report, **first}


def look_previews(glyph: tuple[str, Any], label: str, *, fill: str = "#7f2bf2", ink: str = "#ffffff",
                  size: int = PREVIEW_PX) -> dict:
    """The glyph as the videos will stamp it — {"circle": style E, "chip":
    style H with `label`'s words (None without the label fonts)} as PNG
    data: URLs, drawn by the run's own sprite code (icon_render.make_sprite:
    the same body, weight, shadow and words)."""
    from . import icon_render as ir
    from .elements import icon as icon_el

    out: dict[str, Optional[str]] = {
        "circle": _data_url(ir.make_sprite("circle", glyph, size, fill, ink, look_v=ir.LOOK_V).rgba), "chip": None}
    font = icon_el.label_key()
    if font:
        out["chip"] = _data_url(ir.make_sprite("chip", glyph, icon_el.chip_height(size), fill, ink,
                                               text=icon_el.chip_text(label or ""), font=font,
                                               look_v=ir.LOOK_V).rgba)
    return out


def _data_url(rgba: Any) -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


class IconStore(Protocol):
    """Where team glyphs live (the shared table; tests pass a fake)."""
    async def by_id(self, icon_id: str) -> Optional[dict]: ...
    async def takes(self, name_key: str, origin: str) -> list[dict]: ...
    async def insert(self, row: dict) -> dict: ...
    async def set_hidden(self, icon_id: str, hidden: bool) -> bool: ...
    async def update(self, icon_id: str, fields: dict) -> Optional[dict]: ...
    async def listing(self, limit: int, include_hidden: bool, origin: Optional[str] = None) -> list[dict]: ...
    async def count_since(self, origin: str, hours: float) -> int: ...


# The table was never created before this shape, so it is all in CREATE
# TABLE; the ADD COLUMN lines are a safety net for a table an older build
# made (additive only: nothing is dropped, renamed or retyped).
_DDL = """
CREATE TABLE IF NOT EXISTS yas_rebrand_icons (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    name_key    text NOT NULL,
    label       text NOT NULL DEFAULT '',
    tags        text[] NOT NULL DEFAULT '{}',
    origin      text NOT NULL DEFAULT 'ai',
    take        integer NOT NULL DEFAULT 0,
    png         bytea NOT NULL,
    prompt      text NOT NULL DEFAULT '',
    source_kind text NOT NULL DEFAULT 'ai',
    source_sha  text,
    source      bytea,
    report      jsonb,
    created_by  text NOT NULL DEFAULT '',
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now(),
    hidden_at   timestamptz
)
"""
_DDL_COLUMNS = tuple(
    f"ALTER TABLE yas_rebrand_icons ADD COLUMN IF NOT EXISTS {c}" for c in (
        "label text NOT NULL DEFAULT ''", "tags text[] NOT NULL DEFAULT '{}'",
        "origin text NOT NULL DEFAULT 'ai'", "take integer NOT NULL DEFAULT 0",
        "source_kind text NOT NULL DEFAULT 'ai'", "source_sha text", "source bytea", "report jsonb",
        "created_by text NOT NULL DEFAULT ''", "updated_at timestamptz DEFAULT now()"))
_DDL_INDEX = "CREATE INDEX IF NOT EXISTS yas_rebrand_icons_name_key ON yas_rebrand_icons (name_key)"
_DDL_INDEX_ORIGIN = ("CREATE INDEX IF NOT EXISTS yas_rebrand_icons_origin_created "
                     "ON yas_rebrand_icons (origin, created_at)")
# Closed to the database's public API (PostgREST and its anon key, which
# nothing here uses): row-level security on, with no policy. The backend's
# own role owns the table, so its reads and writes are as they were.
_DDL_RLS = "ALTER TABLE yas_rebrand_icons ENABLE ROW LEVEL SECURITY"
# What a listing reads: never the bytes (the glyph route serves those). No
# read ever selects `source` (an uploaded SVG, kept only to make its glyph
# again): it is never served.
_PUB_COLS = "id, name, name_key, label, tags, origin, take, source_kind, created_by, created_at, updated_at, hidden_at"
_COLS = f"{_PUB_COLS}, png, prompt, report"
# What a team icon's owner may change afterwards (PATCH /icons/custom/{id}).
_EDITABLE = ("name", "label", "tags")


class PgStore:
    """yas_rebrand_icons on the shared database: created lazily by the app
    (additive, IF NOT EXISTS, like pattern_library's table), reads in
    autocommit. Standalone scripts on Windows need
    asyncio.WindowsSelectorEventLoopPolicy before they touch it (psycopg)."""

    def __init__(self) -> None:
        self._ready = False

    async def ensure_table(self) -> None:
        if self._ready:
            return
        from ...db import get_conn

        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_DDL)
                for sql in _DDL_COLUMNS:
                    await cur.execute(sql)
                await cur.execute(_DDL_INDEX)
                await cur.execute(_DDL_INDEX_ORIGIN)
        try:
            # Apart, and never fatal: a table another role made can't be
            # altered by this one, and the icons work the same either way.
            async with get_conn(autocommit=True) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(_DDL_RLS)
        except Exception:  # noqa: BLE001
            log.warning("[icons] couldn't switch row-level security on for yas_rebrand_icons", exc_info=True)
        self._ready = True

    async def _read(self, sql: str, args: tuple) -> list[dict]:
        from ...db import get_conn

        await self.ensure_table()
        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, args)
                return [_row(r) for r in await cur.fetchall()]

    async def by_id(self, icon_id: str) -> Optional[dict]:
        got = await self._read(f"SELECT {_COLS} FROM yas_rebrand_icons WHERE id = %s LIMIT 1", (icon_id,))
        return got[0] if got else None

    async def takes(self, name_key: str, origin: str) -> list[dict]:
        return await self._read(f"SELECT {_COLS} FROM yas_rebrand_icons WHERE origin = %s AND name_key = %s "
                                f"ORDER BY take, created_at", (origin, name_key))

    async def insert(self, row: dict) -> dict:
        from ...db import get_conn

        await self.ensure_table()
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                # Two machines drawing the same take at once: the first row
                # wins everywhere (the id is the take's hash).
                await cur.execute(
                    "INSERT INTO yas_rebrand_icons (id, name, name_key, label, tags, origin, take, png, prompt, "
                    "source_kind, report, created_by, source_sha, source) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                    (row["id"], row["name"], row["name_key"], row.get("label") or "", list(row.get("tags") or []),
                     row.get("origin") or "ai", int(row.get("take") or 0), row["png"], row.get("prompt") or "",
                     row.get("source_kind") or "ai", json.dumps(row.get("report")) if row.get("report") else None,
                     row.get("created_by") or "", row.get("source_sha"), row.get("source")))
        return await self.by_id(row["id"]) or row

    async def set_hidden(self, icon_id: str, hidden: bool) -> bool:
        from ...db import get_conn

        await self.ensure_table()
        q = ("UPDATE yas_rebrand_icons SET hidden_at = now(), updated_at = now() WHERE id = %s AND hidden_at IS NULL"
             if hidden else
             "UPDATE yas_rebrand_icons SET hidden_at = NULL, updated_at = now() WHERE id = %s AND hidden_at IS NOT NULL")
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(q, (icon_id,))
                return cur.rowcount > 0

    async def update(self, icon_id: str, fields: dict) -> Optional[dict]:
        """A team icon's name, label and tags set (only those: never its
        id, its pixels or the key it was made under). The row, or None."""
        from ...db import get_conn

        cols = [c for c in _EDITABLE if c in fields]
        if cols:
            await self.ensure_table()
            sets = ", ".join(f"{c} = %s" for c in cols)
            args = [list(fields[c]) if c == "tags" else str(fields[c]) for c in cols]
            async with get_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(f"UPDATE yas_rebrand_icons SET {sets}, updated_at = now() WHERE id = %s",
                                      (*args, icon_id))
        return await self.by_id(icon_id)

    async def listing(self, limit: int, include_hidden: bool, origin: Optional[str] = None) -> list[dict]:
        where = [] if include_hidden else ["hidden_at IS NULL"]
        args: list[Any] = []
        if origin:
            where.append("origin = %s")
            args.append(origin)
        sql = (f"SELECT {_PUB_COLS} FROM yas_rebrand_icons "
               + (f"WHERE {' AND '.join(where)} " if where else "") + "ORDER BY created_at DESC LIMIT %s")
        return await self._read(sql, (*args, int(limit)))

    async def count_since(self, origin: str, hours: float) -> int:
        got = await self._read("SELECT count(*) AS n FROM yas_rebrand_icons WHERE origin = %s "
                               "AND created_at > now() - make_interval(secs => %s)", (origin, float(hours) * 3600.0))
        return int((got[0] if got else {}).get("n") or 0)


def _row(r: Any) -> dict:
    d = dict(r)
    for k in ("png",):
        if d.get(k) is not None:
            d[k] = bytes(d[k])
    for k in ("created_at", "updated_at", "hidden_at"):
        v = d.get(k)
        d[k] = v.isoformat() if hasattr(v, "isoformat") else v
    if "tags" in d:
        d["tags"] = list(d.get("tags") or [])
    return d


_store: IconStore = PgStore()


def set_store(store: IconStore) -> IconStore:
    """Swap the team-glyph store (tests); returns the previous one."""
    global _store
    old, _store = _store, store
    _team_changed()
    return old


def _png_cache(icon_id: str) -> Path:
    return CACHE_DIR / "custom" / f"{icon_id}.png"


def _remember_png(icon_id: str, png: bytes) -> None:
    try:
        p = _png_cache(icon_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(png)
        tmp.replace(p)
    except OSError:
        pass


def _clock() -> float:
    return time.monotonic()


def _machine() -> str:
    """Who made a team icon: the machine (there are no user accounts)."""
    try:
        return socket.gethostname()[:64]
    except OSError:
        return ""


# --- the team's icons, as the search reads them ---------------------------------

_TEAM: dict[str, Any] = {"at": None, "failed_at": None, "rows": []}
_LEFT: dict[str, Any] = {"at": None, "used": 0}


def _team_changed() -> None:
    """This process changed the team's icons: read them (and the day's
    drawings) again at the next ask."""
    _TEAM.update(at=None, failed_at=None)
    _LEFT.update(at=None)


def _team_public(row: dict) -> dict:
    name = str(row.get("name") or "")
    return {"id": row["id"], "name": name, "label": str(row.get("label") or "") or name[:LABEL_MAX],
            "tags": list(row.get("tags") or []), "origin": str(row.get("origin") or "ai"),
            "created_at": row.get("created_at"), "created_by": row.get("created_by") or "",
            "hidden": bool(row.get("hidden_at"))}


async def team_index() -> list[dict]:
    """The team's visible icons (newest first, at most TEAM_MAX), read from
    the table and kept TEAM_TTL_SEC — another machine's new icon shows within
    a minute, this one's at once. A read that fails keeps what was known
    (nothing, at first) and is tried again after TEAM_RETRY_SEC."""
    now = _clock()
    if _TEAM["at"] is not None and now - _TEAM["at"] < TEAM_TTL_SEC:
        return _TEAM["rows"]
    if _TEAM["failed_at"] is not None and now - _TEAM["failed_at"] < TEAM_RETRY_SEC:
        return _TEAM["rows"]
    try:
        rows = await _store.listing(TEAM_MAX, False)
    except Exception as e:  # noqa: BLE001 — the library still searches
        log.warning("[icons] couldn't read the team's icons: %s", str(e)[:200])
        _TEAM["failed_at"] = now
        return _TEAM["rows"]
    _TEAM.update(at=now, failed_at=None, rows=[_team_public(r) for r in rows])
    return _TEAM["rows"]


def team_known() -> list[dict]:
    """The team's visible icons as this process last read them (team_index)
    — no database read. An icon plan offers the ones that are said, and an
    added one lands where its name and tags are said, from what the studio
    already knows (the icon routes read the table again every
    TEAM_TTL_SEC)."""
    return list(_TEAM["rows"])


async def team_icons(limit: int = 60, *, include_hidden: bool = False) -> list[dict]:
    """GET /icons/team: the team's icons, newest first — [{id, name, label,
    tags, origin, created_at, created_by, hidden}] (read now, not cached)."""
    rows = await _store.listing(max(1, min(TEAM_MAX, int(limit))), include_hidden)
    return [_team_public(r) for r in rows]


# --- drawing, on a press -----------------------------------------------------------

_DRAWING: dict[str, asyncio.Future] = {}
# name_key -> (until when its drawing failed, the message, the status): asked
# again before then, the same answer, with no model call.
_DRAW_FAILED: dict[str, tuple[float, str, int]] = {}
# When this process paid for a drawing that was not a clean icon: those count
# against the day's drawings too (the table only holds the ones kept).
_REJECTED: list[float] = []


def draws_per_day() -> int:
    """The whole team's AI drawings a day (ICON_DRAWS_PER_DAY, default 25)."""
    try:
        return max(0, int(os.getenv("ICON_DRAWS_PER_DAY", "") or DRAW_PER_DAY))
    except ValueError:
        return DRAW_PER_DAY


def draw_cost_usd() -> float:
    """What one drawing costs (vertex_client's price of the image model in
    use), for the button and the answer."""
    try:
        from .. import vertex_client

        return float(vertex_client._IMAGE_PRICE_USD.get(config.vertex_image_model, DRAW_COST_USD))
    except Exception:  # noqa: BLE001
        return DRAW_COST_USD


async def draws_left(*, cached: bool = False) -> int:
    """How many AI drawings the team may still ask for today: the day's cap
    less the drawings made in the last DRAW_WINDOW_H hours on every machine
    (the table) and this process's rejected ones. `cached`: the table's
    count may be up to LEFT_TTL_SEC old (the search's hint; a press always
    reads it). Raises when the table can't be read."""
    now = _clock()
    if cached and _LEFT["at"] is not None and now - _LEFT["at"] < LEFT_TTL_SEC:
        used = int(_LEFT["used"])
    else:
        used = int(await _store.count_since("ai", DRAW_WINDOW_H))
        _LEFT.update(at=now, used=used)
    cutoff = now - DRAW_WINDOW_H * 3600.0
    _REJECTED[:] = [t for t in _REJECTED if t > cutoff]
    return max(0, draws_per_day() - used - len(_REJECTED))


def _offer_base(q: str) -> dict:
    return {"offer": False, "name": _display(q), "cost_usd": draw_cost_usd(), "per_day": draws_per_day(),
            "left_today": None, "why_not": None}


async def generate_offer(q: str) -> dict:
    """Whether "Generate with AI" may be offered for a typed name:
    {offer, name, cost_usd, per_day, left_today, why_not}. Never for fewer
    than 2 letters, a brand (why_not says so) or once the day's drawings
    are used; the studio shows it as a card when nothing found is strong,
    else as a quiet link. Free: reads the day's count (cached)."""
    out = _offer_base(q)
    key = name_key(q)
    if _letters(key) < 2:
        return out
    if is_brand_name(key):
        return {**out, "why_not": BRAND_WHY}
    try:
        left: Optional[int] = await draws_left(cached=True)
    except Exception as e:  # noqa: BLE001 — the press itself will say so
        log.warning("[icons] couldn't count today's drawings: %s", str(e)[:200])
        left = None
    out["left_today"] = left
    if left == 0:
        return {**out, "why_not": CAP_WHY.format(n=draws_per_day())}
    return {**out, "offer": True}


def _drawn(row: dict, *, created: bool, was_hidden: bool, report: Optional[dict] = None,
           calls: int = 0) -> dict:
    name = str(row.get("name") or "")
    return {"id": row["id"], "name": name, "label": str(row.get("label") or "") or name[:LABEL_MAX],
            "origin": str(row.get("origin") or "ai"), "take": int(row.get("take") or 0), "created": created,
            "was_hidden": was_hidden, "report": report if report is not None else row.get("report"),
            "cost_usd": round(calls * draw_cost_usd(), 4)}


async def draw(name: str, *, again: bool = False, attempts: int = DRAW_ATTEMPTS) -> dict:
    """"Generate with AI": the team glyph for `name`, drawn by the AI image
    model ONLY here, on the user's press.

    Returns {"id": "c-<sha12>", "name", "label", "origin": "ai", "take",
             "created": drawn now, "was_hidden": an earlier drawing the user
             had discarded, shown again, "report": the clean-up's numbers,
             "cost_usd": what this press cost}.
    The name's drawing already made (by anyone) is returned at no cost;
    `again` ("Try again") draws a new take and hides the one shown. Checked
    BEFORE any call: a brand (422), the team's day of drawings (429) and the
    table being reachable (503). Concurrent presses for one name share one
    drawing; a name whose drawings were not clean icons gets the same answer
    for DRAW_BACKOFF_SEC with no new call. Raises DrawError."""
    display = _display(name)
    key = name_key(display)
    if _letters(key) < 2:
        raise DrawError("Type a name to draw")
    if is_brand_name(key):
        raise DrawError(BRAND_WHY)
    running = _DRAWING.get(key)
    if running is not None:
        return await asyncio.shield(running)
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _DRAWING[key] = fut
    try:
        result = await _draw(display, key, again, attempts)
        fut.set_result(result)
        return result
    except asyncio.CancelledError:
        fut.cancel()
        raise
    except Exception as e:
        if isinstance(e, DrawError) and e.backoff > 0:
            _DRAW_FAILED[key] = (_clock() + e.backoff, str(e), e.status)
        fut.set_exception(e)
        fut.exception()               # marked retrieved: no "never retrieved" noise
        raise
    finally:
        _DRAWING.pop(key, None)


async def _draw(display: str, key: str, again: bool, attempts: int) -> dict:
    try:
        takes = await _store.takes(key, "ai")
    except Exception as e:  # noqa: BLE001 — nothing is drawn where it can't be kept
        log.warning("[icons] couldn't read the drawings of %r: %s", key, str(e)[:200])
        raise DrawError(TABLE_DOWN, status=503) from e
    shown = [r for r in takes if not r.get("hidden_at")]
    if not again and shown:
        return _drawn(shown[-1], created=False, was_hidden=False)
    if not again and takes:
        row = takes[-1]
        await _store.set_hidden(row["id"], False)
        _team_changed()
        return _drawn(row, created=False, was_hidden=True)
    failed = _DRAW_FAILED.get(key)
    if failed is not None and _clock() < failed[0]:
        raise DrawError(failed[1], status=failed[2])
    take = max((int(r.get("take") or 0) for r in takes), default=-1) + 1
    got = await _draw_new(display, key, take, attempts)
    _DRAW_FAILED.pop(key, None)
    for r in shown:                   # "Try again": the take shown until now steps aside
        await _store.set_hidden(r["id"], True)
    _team_changed()
    return got


async def _draw_new(display: str, key: str, take: int, attempts: int) -> dict:
    from .. import vertex_client

    prompt = DRAW_PROMPT.format(name=re.sub(r"[\"'<>{}\[\]]", "", display))
    reason = "The drawing did not work"
    calls = 0
    for attempt in range(max(1, attempts)):
        try:
            left = await draws_left()
        except Exception as e:  # noqa: BLE001
            log.warning("[icons] couldn't count today's drawings: %s", str(e)[:200])
            raise DrawError(TABLE_DOWN, status=503) from e
        if left <= 0:
            if attempt:
                break                 # the first was not a clean icon, and the day's are used
            raise DrawError(CAP_WHY.format(n=draws_per_day()), status=429)
        try:
            data = await asyncio.wait_for(vertex_client.gemini_image(prompt, aspect_ratio="1:1"),
                                          timeout=DRAW_TIMEOUT_SEC)
        except Exception as e:  # noqa: BLE001 — the model is down: say so
            log.warning("[icons] drawing %r failed: %s", key, str(e)[:200])
            raise DrawError("The AI could not draw it right now — try again in a minute",
                            status=503, backoff=DRAW_DOWN_SEC) from e
        calls += 1
        try:
            png, report = await asyncio.to_thread(prepare_drawing, data)
        except DrawError as e:
            reason = str(e)
            _REJECTED.append(_clock())
            log.info("[icons] drawing %r try %d rejected: %s", key, attempt + 1, reason)
            continue
        try:
            row = await _store.insert({"id": draw_id(key, take), "name": display, "name_key": key,
                                       "label": display[:LABEL_MAX].strip(), "tags": [], "origin": "ai",
                                       "take": take, "png": png, "prompt": prompt, "source_kind": "ai",
                                       "report": report, "created_by": _machine()})
        except Exception as e:  # noqa: BLE001 — drawn (and billed), but it can't be kept
            _REJECTED.append(_clock())
            log.warning("[icons] couldn't keep the drawing of %r: %s", key, str(e)[:200])
            raise DrawError(TABLE_DOWN, status=503, backoff=DRAW_DOWN_SEC) from e
        _remember_png(row["id"], bytes(row.get("png") or png))
        log.info("[icons] drew %r as %s (take %d, %s)", key, row["id"], take, report)
        return _drawn(row, created=True, was_hidden=False, report=report, calls=calls)
    # Drawings that were not clean icons: this name is not drawn again for a
    # while (it would be billed again for the same result).
    raise DrawError(f"{reason} — try a simpler word", backoff=DRAW_BACKOFF_SEC)


async def glyph(icon_id: str, weight: str = "fill") -> tuple[str, Any]:
    """("svg", path data in `weight`) for a library icon, ("png", bytes) for
    a team one (one shape, no weights; hidden ones too: stored versions
    still draw them). KeyError when there is no such icon."""
    icon_id = str(icon_id or "")
    d = library_d(icon_id, weight)
    if d:
        return "svg", d
    if not is_custom_id(icon_id):
        raise KeyError(icon_id)
    p = _png_cache(icon_id)
    try:
        return "png", p.read_bytes()
    except OSError:
        pass
    row = await _store.by_id(icon_id)
    if not row or not row.get("png"):
        raise KeyError(icon_id)
    png = bytes(row["png"])
    _remember_png(icon_id, png)
    return "png", png


async def glyphs_for(items: Iterable[Any]) -> dict[str, tuple[str, Any]]:
    """{glyph id: glyph} for every CUSTOM glyph a list of resolved items
    uses (dicts or models with glyph {source, id}) — what a Stamper needs
    beyond the library. KeyError if one is missing."""
    out: dict[str, tuple[str, Any]] = {}
    for it in items:
        g = it.get("glyph") if isinstance(it, dict) else getattr(it, "glyph", None)
        if hasattr(g, "model_dump"):
            g = g.model_dump()
        gid = str((g or {}).get("id") or "")
        if gid and gid not in out and not library_d(gid):
            out[gid] = await glyph(gid)
    return out


async def hide(icon_id: str) -> bool:
    """Hide a team glyph from the library, for everyone (the bytes stay:
    versions reference it). False when there is no such visible glyph."""
    if not is_custom_id(icon_id):
        return False
    done = await _store.set_hidden(icon_id, True)
    _team_changed()
    return done


# --- your own icons, uploaded --------------------------------------------------------

UPLOAD_SLOTS = 2                     # files made into glyphs at once (each takes ~100 MB for a moment)
_IMPORTING = threading.BoundedSemaphore(UPLOAD_SLOTS)
NAME_NEEDED = "Give the icon a name"
# The last few files made into glyphs, by what they were made of (the
# importer is pure): the upload card asks again each time the chip's words
# pause, and Save sends the same file — neither makes it again.
IMPORTS_KEPT = 8
_IMPORTS: dict[tuple, Any] = {}
_IMPORTS_LOCK = threading.Lock()


def upload_limits() -> dict:
    """What the studio checks before sending a file: {max_bytes,
    svg_max_bytes, kinds, name_max, tags_max, tag_max}."""
    from . import icon_import as ii

    return {"max_bytes": ii.MAX_BYTES, "svg_max_bytes": ii.SVG_MAX_BYTES, "kinds": list(ii.KINDS),
            "name_max": ii.NAME_MAX, "tags_max": ii.TAGS_MAX, "tag_max": ii.TAG_MAX}


def _prepared(data: bytes, filename: str, mode: str, label: Optional[str]) -> tuple[Any, Optional[dict]]:
    """icon_import.import_upload in a worker thread (at most UPLOAD_SLOTS at
    once; the same file in the same mode made once — _IMPORTS), plus —
    unless `label` is None — its circle and chip as the videos will stamp
    them, the chip with `label`'s words (else the name guessed). A file
    refused is refused again each time (nothing is kept for it)."""
    from . import icon_import as ii

    key = (hashlib.sha256(data).hexdigest(), str(filename or ""), str(mode))
    with _IMPORTING:
        with _IMPORTS_LOCK:
            got = _IMPORTS.pop(key, None)
        if got is None:
            got = ii.import_upload(data, filename, mode=mode)
        with _IMPORTS_LOCK:
            _IMPORTS[key] = got                         # the most recent last
            while len(_IMPORTS) > IMPORTS_KEPT:
                _IMPORTS.pop(next(iter(_IMPORTS)), None)
        if label is None:
            return got, None
        return got, look_previews(("png", got.png), label or got.name_guess or "Icon")


def _png_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


async def upload_preview(data: bytes, filename: str = "", *, mode: str = "auto",
                         label: Optional[str] = None) -> dict:
    """POST /icons/upload/preview: an uploaded file made into its glyph —
    free, and nothing is saved.

    Returns {"id": the id it would be saved under, "png_url": the glyph (a
             PNG data URL), "preview": {circle, chip} as the videos will
             stamp it (the chip with `label`'s words, else the name
             guessed), "report", "name_guess", "kind", "duplicate_of":
             {id, name, hidden} when your team has this very icon already}.
    Saving the same file in the same mode stores exactly these bytes (the
    importer is pure). Raises icon_import.UploadError."""
    from . import icon_import as ii

    got, preview = await asyncio.to_thread(_prepared, data, filename, mode, ii.clean_name(label or ""))
    duplicate = None
    try:
        row = await _store.by_id(got.id)
    except Exception as e:  # noqa: BLE001 — the preview stands without it
        log.warning("[icons] couldn't look for %s among the team's icons: %s", got.id, str(e)[:200])
        row = None
    if row:
        duplicate = {"id": row["id"], "name": str(row.get("name") or ""), "hidden": bool(row.get("hidden_at"))}
    return {"id": got.id, "png_url": _png_url(got.png), "preview": preview, "report": got.report,
            "name_guess": got.name_guess, "kind": got.kind, "duplicate_of": duplicate}


async def upload(data: bytes, filename: str = "", *, name: str, tags: Any = (),
                 mode: str = "auto") -> tuple[dict, bool]:
    """POST /icons/upload: an uploaded file saved to your team's icons, for
    every machine — (the icon as GET /icons/team lists it + its report,
    created). The same picture saved again is the icon already there (its
    name kept; shown again if it had been hidden), created False. The file
    is made into its glyph exactly as the preview made it; the PNG bytes go
    in the row (no bucket), and a small SVG is kept beside them, never
    served. Raises icon_import.UploadError (the file, a missing name) or
    DrawError 503 (the table)."""
    from . import icon_import as ii

    display = ii.clean_name(name)
    if not display:
        raise ii.UploadError(NAME_NEEDED)
    got, _ = await asyncio.to_thread(_prepared, data, filename, mode, None)
    try:
        row = await _store.by_id(got.id)
        if row:
            if row.get("hidden_at"):
                await _store.set_hidden(got.id, False)
                row = await _store.by_id(got.id) or row
                _team_changed()
            created = False
        else:
            row = await _store.insert({
                "id": got.id, "name": display, "name_key": name_key(display),
                "label": display[:LABEL_MAX].strip(), "tags": ii.clean_tags(tags), "origin": "upload",
                "take": 0, "png": got.png, "prompt": "", "source_kind": got.kind,
                "source_sha": got.source_sha, "source": ii.kept_source(data, got.kind),
                "report": got.report, "created_by": _machine()})
            created = True
            _team_changed()
    except Exception as e:  # noqa: BLE001 — nothing is half-saved: the id is the picture's
        log.warning("[icons] couldn't save the upload %s: %s", got.id, str(e)[:200])
        raise DrawError(TABLE_DOWN, status=503) from e
    _remember_png(got.id, bytes(row.get("png") or got.png))
    log.info("[icons] upload %s as %s (%s, %s)", display, got.id, "new" if created else "already there", got.report)
    return {**_team_public(row), "report": row.get("report") or got.report}, created


async def update(icon_id: str, *, name: Optional[str] = None, label: Optional[str] = None,
                 tags: Any = None, hidden: Optional[bool] = None) -> dict:
    """PATCH /icons/custom/{id}: a team icon renamed, re-tagged, hidden or
    shown again — for everyone. Its id and pixels never change, so every
    version that uses it draws the same icon (and a chip's words are frozen
    in the recipe: a rename changes future adds and the search only). A new
    name sets the short label too, unless one is given. Returns the icon as
    GET /icons/team lists it. KeyError: no such team icon;
    icon_import.UploadError: an empty name."""
    from . import icon_import as ii

    if not is_custom_id(icon_id):
        raise KeyError(icon_id)
    row = await _store.by_id(icon_id)
    if not row:
        raise KeyError(icon_id)
    fields: dict[str, Any] = {}
    if name is not None:
        display = ii.clean_name(name)
        if not display:
            raise ii.UploadError(NAME_NEEDED)
        fields["name"] = display
        fields["label"] = display[:LABEL_MAX].strip()
    if label is not None:
        fields["label"] = (ii.clean_name(label)[:LABEL_MAX].strip()
                           or str(fields.get("name") or row.get("name") or "")[:LABEL_MAX].strip())
    if tags is not None:
        fields["tags"] = ii.clean_tags(tags)
    if fields:
        row = await _store.update(icon_id, fields) or row
    if hidden is not None:
        await _store.set_hidden(icon_id, bool(hidden))
        row = await _store.by_id(icon_id) or row
    _team_changed()
    return _team_public(row)
