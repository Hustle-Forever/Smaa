"""The icon director: which ideas of a scene get an icon, on which SAID word,
and why — one Gemini call per scene, then deterministic code for the rest.

  direct(ctx) -> Director   (async; cached as ctx.work_dir/director.json)

Director = {version, key, source: "ai"|"keywords", ideas: [Idea],
            room: {spots: [{n, id, verdict, why}], home, home_n, home_why},
            candidates: [Candidate], note, failed_at?, error?}

Idea = {id, cand_id, source: "ai"|"keyword", concept, quote, w, w_end, word,
        s, end, mentions: [{w, w_end, quote, s}], table_mentions: [w], icon,
        label, purpose, priority, strong, verdict: "keep"|"refuse"|"suggest",
        reason, list_span, auto}

What the director decides: which ideas are worth an icon, the word each lands
on (an index into the scene's Words), which mention when an idea is said twice,
a purpose line, a priority, and a one-time read of the room (frame 0 with the
speaker_track spots numbered on it: a VETO only — good / avoid — never a
coordinate). It never outputs seconds, pixels or colours: the icon planner
turns its answer into times and boxes.

The call (one per scene, until DIRECTOR_VERSION or an input changes):
vertex_client.gemini_multimodal, the fast model, temperature 0, a 512-token
thinking budget, a JSON schema with EVERY field required (Gemini drops the
optional ones). It sees the SAID transcript with word indices, the script, the
topic, the other scenes' lines (context only), the keyword candidates — each
with an id — the 42 picks with their names, and the room image.

Nothing it says is trusted as is (the 2026-09-27 critique):
  - it must give a verdict for EVERY candidate id; a missing one asks once
    more, then that candidate is refused ("no verdict"), so the AI's silence
    never becomes an icon;
  - an idea's `quote` must BE the said words at w..w_end (a slip of up to
    QUOTE_SLACK words is absorbed and corrected); no similarity guessing;
  - the words must not be said inside a negation, to the end of their clause
    (brand_icons.negated_in_clause on the SAID transcript), nor inside an
    idiom ("feel free", "dress code") — unless the keyword table itself
    affirms that icon there ("no fees" is a piggy bank);
  - never the brand (42, Abu Dhabi), never a brand logo;
  - "other:<noun>" must resolve in the icon library with a score >= 0.8;
  - a LIST said together ("code, design, AI and games") keeps one icon for
    the whole list at most — marked by the AI (list_span) or found here;
  - one idea per icon (merged by icon id: the AI's chosen mention wins) and
    per word; at most MAX_EXTRA ideas the candidates missed.

AUTO: an idea is placed on its own only when it is kept, priority >= 3, and
(a STRONG keyword or priority >= 4). Without the AI (Vertex down, props.ai
false) the keyword hits are SUGGESTIONS only — never placed on their own.

HOPEFUL words ("your future", "the journey"): hopeful but general, so never
placed on their own, and never refused either — a one-tap suggestion. An
answer given from this prompt on says "suggest" for them (the checks above
still apply: a negated one is refused). An answer given before is never
asked again for it (the key has no prompt in it): as_suggestions() shows the
AI's own refusal of a hopeful idea, said and meant, as a suggestion instead
— for the studio's lists only; the stored answer, its key and every plan key
stay as they were.

A failed call is cached too ({source: "keywords", failed_at}) and tried again
only on a new take ("Ask the AI again") or after FAIL_BACKOFF_SEC, so a warm-up
poll never re-bills it every second.

What the call was, kept beside the answer (none of it is in the key, so no
scene is asked again for it): `raw`, the model's answer as it came (up to
RAW_MAX bytes), `prompt_digest` (what it was asked: the system line, the
prompt and the schema), `reasked` (the candidate ids a second ask named) and
`inputs` (the key's parts, so a changed key can say what changed). From
`raw`, replay() judges the same answer again with today's rules — a change to
the validation is tried on every stored scene for free
(director_dryrun). explain() lists every keyword the transcript matched and
why each one that is not a candidate was dropped.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

from .. import brand_icons
from . import words as wordsmod

log = logging.getLogger("archivist.rebrand.harness.director")

# Bumped only for a change every scene must be asked again for (one paid
# call each). A change of the prompt, the schema or the validation that new
# answers alone should follow keeps it: an answer already given stands
# (prompt_digest records which prompt it answered, replay() judges it by
# today's rules for free).
DIRECTOR_VERSION = 1
MAX_EXTRA = 2              # ideas the keyword candidates missed
LABEL_MAX = 16
PURPOSE_MAX = 60
WHY_MAX = 60
QUOTE_SLACK = 3            # an index slip absorbed when finding a quote
SPAN_MAX = 8               # w_end <= w + SPAN_MAX
MATCH_STRONG = 0.8         # an "other:<noun>" must match the library this well
FAIL_BACKOFF_SEC = 600
ROOM_WIDTH = 720           # the room image's width (a spot there is ~115 px)
CALL_TIMEOUT_SEC = 90
MAX_TOKENS = 4096
THINKING_BUDGET = 512
AUTO_MIN_PRIORITY = 3
AUTO_SOLO_PRIORITY = 4     # a non-STRONG idea needs this to be placed on its own
RAW_MAX = 16_384           # bytes of the model's answer kept in director.json

# Why a rule here refused an idea the AI kept (an idea's `reason`). Any other
# reason on a refused idea is the AI's own words (refused_by).
WHY_NOT_THERE = "the quote is not what was said there"
WHY_NO_ICON_GIVEN = "no icon given"
WHY_NO_ICON_FOR = "no icon for '{name}'"         # "other:<noun>" not in the library
WHY_NO_ICON = "no icon '{name}'"                 # an id the library does not have
WHY_LOGO = "a brand logo"
WHY_NOT_SAID = "not said"
WHY_BRAND = "the brand, not an idea"
WHY_NEGATED = "said as a negation"
WHY_IDIOM = "an idiom, not the idea"
WHY_LIST = "part of a list — one icon for the whole list"
WHY_SAME_ICON = "same icon as '{label}'"
WHY_SAME_WORD = "same word as '{label}'"
WHY_EXTRAS = "too many extra ideas"
WHY_SILENT = "no verdict from the AI"
_CHECKS = (WHY_NOT_THERE, WHY_NO_ICON_GIVEN, WHY_NO_ICON_FOR, WHY_NO_ICON, WHY_LOGO, WHY_NOT_SAID,
           WHY_BRAND, WHY_NEGATED, WHY_IDIOM, WHY_LIST, WHY_SAME_ICON, WHY_SAME_WORD, WHY_EXTRAS)
_CHECK_RX = re.compile("^(?:" + "|".join(re.escape(c).replace(r"\{name\}", ".*").replace(r"\{label\}", ".*")
                                         for c in _CHECKS) + ")$", re.DOTALL)

# Hopeful but general words (module docstring): a suggestion, never an icon
# on its own and never refused.
HOPEFUL_RX = re.compile(r"\b(?:futures?|journeys?|dreams?|goals?|ambitions?|potential|opportunit(?:y|ies)|"
                        r"possibilit(?:y|ies)|tomorrow|success(?:es)?)\b", re.IGNORECASE)
WHY_HOPEFUL = "hopeful but general — shown only if you add it"

# explain(): why a keyword the transcript matched is not a candidate.
DROPPED_WHY = {
    "negated": "said as a “no”",
    "idiom": "an idiom, not the idea",
    "brand": "the brand's own words",
    "overlap": "inside a longer match for the same icon",
}

Resolver = Callable[[str], Union[Optional[tuple[str, float]], Awaitable[Optional[tuple[str, float]]]]]


# ---------------------------------------------------------------------------
# The icon list (icon_catalog when it is there, else the keyword table's ids)
# ---------------------------------------------------------------------------

@dataclass
class Catalog:
    """What the director may pick from. `picks`: [{id, name, aliases}] (the
    42 picks, shown in the prompt); `exists(id)`: any library id is fine;
    `resolve(name)` -> (id, score) | None for "other:<noun>" (sync or async,
    free — no AI)."""

    version: str
    picks: list[dict]
    exists: Callable[[str], bool]
    resolve: Optional[Resolver] = None


def keyword_catalog() -> Catalog:
    """The keyword table's own ids, named after them (no library needed)."""
    ids = sorted(brand_icons.icon_ids())
    names: dict[str, dict] = {}
    for row in getattr(brand_icons, "name_rows", lambda: [])():
        names.setdefault(row["id"], {"name": row["name"], "aliases": list(row.get("aliases") or [])})
    picks = [{"id": i, "name": names.get(i, {}).get("name") or i.replace("-", " ").capitalize(),
              "aliases": names.get(i, {}).get("aliases", [])} for i in ids]
    blob = json.dumps(picks, sort_keys=True).encode("utf-8")
    known = set(ids)

    def resolve(name: str) -> Optional[tuple[str, float]]:
        key = re.sub(r"[\s_]+", "-", (name or "").strip().lower())
        if key in known:
            return key, 1.0
        for p in picks:
            if name.strip().lower() in [p["name"].lower(), *[a.lower() for a in p["aliases"]]]:
                return p["id"], 1.0
        hits = brand_icons.find_hits(name or "")
        return (hits[0].icon, 0.9) if hits else None

    return Catalog(version="keywords-" + hashlib.sha256(blob).hexdigest()[:10], picks=picks,
                   exists=lambda i: i in known, resolve=resolve)


def default_catalog() -> Catalog:
    """icon_catalog's library (the 42 picks + NAMES, any library id, and
    "other:x" through the studio's own free search, icon_catalog.resolve:
    names, synonyms, keywords, tags, words, spelling); the keyword catalogue
    when it cannot load (not baked yet). Library ids only: a team glyph is
    the user's to add, never the director's (its table would enter the key
    and re-ask every scene when a teammate adds one), and a Stamper can
    draw one only after it is fetched from the database. The version is the
    FILL file's (catalogue_version): a smarter search changes new answers
    only, never the key of one already given."""
    try:
        from . import icon_catalog as ic

        version = ic.catalogue_version()
        rows: dict[str, dict] = {}
        for row in brand_icons.name_rows():
            rows.setdefault(row["id"], {"name": row["name"], "aliases": list(row.get("aliases") or [])})
        picks = []
        for i in ic.PICK_IDS:
            meta = ic.icon(i) or {}
            r = rows.get(i, {})
            picks.append({"id": i, "name": r.get("name") or meta.get("label") or meta.get("name") or i,
                          "aliases": r.get("aliases", [])})

        def exists(i: str) -> bool:
            try:
                return ic.icon(i) is not None
            except Exception:  # noqa: BLE001
                return False

        def resolve(name: str) -> Optional[tuple[str, float]]:
            got = ic.resolve(name)
            return (got[0], got[1]) if got else None

        return Catalog(version=version, picks=picks, exists=exists, resolve=resolve)
    except Exception as e:  # noqa: BLE001 — the keyword table stands in
        log.info("[director] icon library unavailable (%s); using the keyword table's ids", str(e)[:120])
        return keyword_catalog()


async def _resolve(catalog: Catalog, name: str) -> Optional[tuple[str, float]]:
    if catalog.resolve is None or not (name or "").strip():
        return None
    try:
        got = catalog.resolve(name)
        if inspect.isawaitable(got):
            got = await got
        return (str(got[0]), float(got[1])) if got else None
    except Exception as e:  # noqa: BLE001 — an unresolvable name is refused, not an error
        log.info("[director] could not resolve %r: %s", name, str(e)[:120])
        return None


# ---------------------------------------------------------------------------
# The inputs
# ---------------------------------------------------------------------------

@dataclass
class DirectorCtx:
    """One scene's director inputs. `words` is words.scene_words' result;
    `spots` speaker_track's [{id, group, box}] (the room image numbers them
    1..N in this order); `frame0` the image they are drawn on; `take` is
    bumped by "Ask the AI again"; `ai` false = keywords only (free)."""

    words: dict
    script: str
    work_dir: Path
    topic: str = ""
    others: list[str] = field(default_factory=list)
    frame0: Optional[Path] = None
    spots: list[dict] = field(default_factory=list)
    take: int = 0
    ai: bool = True
    catalog: Optional[Catalog] = None


def _said(words: dict) -> list[dict]:
    return [w for w in words.get("words") or [] if w.get("said")]


def _by_w(words: dict) -> dict[int, dict]:
    return {int(w["w"]): w for w in words.get("words") or []}


def _span_quote(words: dict, w0: int, w1: int) -> str:
    """The said words w0..w1 as they read in the transcript."""
    t = words.get("transcript") or ""
    by = _by_w(words)
    a, b = by.get(w0), by.get(w1)
    if not a or not b or a.get("tchar") is None or b.get("tchar") is None:
        return ""
    return t[a["tchar"]: b["tchar"] + len(b["text"])]


def candidates(words: dict) -> list[dict]:
    """The keyword candidates on the SAID transcript (brand_icons
    find_mentions: negation to the clause end, idioms and the brand out),
    one per icon: [{id: "k1", icon, tier, strong, w, w_end, quote,
    mentions: [{w, w_end, quote}]}], in speech order."""
    text = words.get("transcript") or ""
    by_icon: dict[str, dict] = {}
    for m in brand_icons.find_mentions(text):
        w0 = wordsmod.word_at_tchar(words, m.pos)
        w1 = wordsmod.word_at_tchar(words, max(m.pos, m.end - 1))
        if w0 is None or w1 is None:
            continue
        ment = {"w": w0, "w_end": max(w0, w1), "quote": _span_quote(words, w0, max(w0, w1))}
        c = by_icon.get(m.icon)
        if c is None:
            by_icon[m.icon] = {"icon": m.icon, "tier": m.tier, "strong": m.tier == 0,
                               "w": w0, "w_end": ment["w_end"], "quote": ment["quote"], "mentions": [ment]}
        else:
            c["mentions"].append(ment)
            if m.tier < c["tier"]:
                c["tier"], c["strong"] = m.tier, m.tier == 0
    out = sorted(by_icon.values(), key=lambda c: (c["w"], c["icon"]))
    for k, c in enumerate(out, 1):
        c["id"] = f"k{k}"
    return [{"id": c["id"], **{k: v for k, v in c.items() if k != "id"}} for c in out]


def list_groups(words: dict, cands: list[dict]) -> list[tuple[int, int]]:
    """(w_first, w_last) of every LIST the keyword candidates form: three or
    more said one after another with nothing but commas and and/or/nor
    between them ("Code, design, AI, games and cybersecurity")."""
    text = words.get("transcript") or ""
    by = _by_w(words)
    items = sorted({(m["w"], m["w_end"]) for c in cands for m in c["mentions"]})
    sep = re.compile(r"^\s*(?:,\s*(?:(?:and|or|nor)\s+)?|(?:and|or|nor|&)\s+)$", re.IGNORECASE)
    groups, run = [], []
    for it in items:
        if run:
            prev = run[-1]
            a, b = by.get(prev[1]), by.get(it[0])
            between = text[a["tchar"] + len(a["text"]): b["tchar"]] if a and b and b["tchar"] is not None \
                and a["tchar"] is not None and b["tchar"] >= a["tchar"] + len(a["text"]) else None
            if between is not None and sep.match(between):
                run.append(it)
                continue
            if len(run) >= 3:
                groups.append((run[0][0], run[-1][1]))
            run = []
        run = [it]
    if len(run) >= 3:
        groups.append((run[0][0], run[-1][1]))
    return groups


def explain(words: dict) -> list[dict]:
    """Every keyword the SAID transcript matched (brand_icons.keyword_spans),
    in order, and why each one that is not a candidate was dropped:
    [{icon, tier, strong, said, pos, end (its characters), w, w_end, quote, s,
    dropped: None | "negated" | "idiom" | "brand" | "overlap"}]
    ("without teachers" drops the teacher
    as negated; "overlap" is the "free" inside "tuition-free"). The ones kept
    (dropped None) are brand_icons.find_mentions, which candidates() groups
    by icon. Free."""
    text = words.get("transcript") or ""
    by = _by_w(words)
    brand = [m.span() for m in brand_icons._BRAND_RX.finditer(text)]
    dropped: dict[tuple, Optional[str]] = {}
    for m in brand_icons.keyword_spans(text):
        why = ("negated" if brand_icons.negated_in_clause(text, m.pos)
               else "idiom" if brand_icons.in_idiom(text, m.pos)
               else "brand" if any(s <= m.pos < e for s, e in brand) else None)
        dropped[(m.icon, m.pos, m.end, m.tier)] = why
    # find_mentions' own merge: one icon's overlapping matches are one (the
    # earliest, then the strongest, then the longest).
    kept: list[brand_icons.Mention] = []
    for m in sorted((brand_icons.Mention(*k) for k, why in dropped.items() if why is None),
                    key=lambda h: (h.pos, h.tier, -h.end, h.icon)):
        if any(k.icon == m.icon and m.pos < k.end and k.pos < m.end for k in kept):
            dropped[(m.icon, m.pos, m.end, m.tier)] = "overlap"
        else:
            kept.append(m)
    out = []
    for m in brand_icons.keyword_spans(text):
        if (m.icon, m.pos, m.end, m.tier) not in dropped:
            continue                                  # listed once (two patterns, one match)
        why = dropped.pop((m.icon, m.pos, m.end, m.tier))
        w0 = wordsmod.word_at_tchar(words, m.pos)
        w1 = wordsmod.word_at_tchar(words, max(m.pos, m.end - 1))
        w1 = max(w0, w1) if w0 is not None and w1 is not None else w1
        out.append({
            "icon": m.icon, "tier": m.tier, "strong": m.tier == 0, "said": text[m.pos:m.end],
            "pos": m.pos, "end": m.end, "w": w0, "w_end": w1,
            "quote": _span_quote(words, w0, w1) if w0 is not None and w1 is not None else "",
            "s": by[w0]["s"] if w0 in by else None,
            "dropped": why,
        })
    return out


def room_image(frame0: Optional[Path], spots: list[dict], out: Path) -> Optional[Path]:
    """frame0 at ROOM_WIDTH px with every spot outlined and numbered 1..N
    (JPEG), or None without a frame or spots."""
    if not frame0 or not Path(frame0).is_file() or not spots:
        return None
    import cv2

    img = cv2.imread(str(frame0), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    s = ROOM_WIDTH / w
    img = cv2.resize(img, (ROOM_WIDTH, max(1, int(round(h * s)))), interpolation=cv2.INTER_AREA)
    H, W = img.shape[:2]
    for n, sp in enumerate(spots, 1):
        b = sp["box"]
        x0, y0 = int(round(b["x"] * W)), int(round(b["y"] * H))
        x1, y1 = int(round((b["x"] + b["w"]) * W)) - 1, int(round((b["y"] + b["h"]) * H)) - 1
        cv2.rectangle(img, (x0, y0), (x1, y1), (0, 0, 0), 5)
        cv2.rectangle(img, (x0, y0), (x1, y1), (0, 230, 255), 2)
        label = str(n)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        cv2.rectangle(img, (cx - tw // 2 - 6, cy - th // 2 - 6), (cx + tw // 2 + 6, cy + th // 2 + 6),
                      (0, 0, 0), -1)
        cv2.putText(img, label, (cx - tw // 2, cy + th // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    (0, 230, 255), 2, cv2.LINE_AA)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    return out


def spots_digest(frame0: Optional[Path], spots: list[dict]) -> str:
    """The room read's key: the frame's bytes and the spot boxes (not the
    JPEG, which a library upgrade may encode differently)."""
    from . import stages as st

    d = st.digest(Path(frame0)) if frame0 else None
    rows = [[s.get("id"), s.get("box")] for s in spots or []]
    return hashlib.sha256(json.dumps([d, rows], sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def key_inputs(ctx: DirectorCtx, catalog: Catalog) -> dict:
    """The parts of the cache key, named and in the key's order (stored as
    director.json's `inputs`, so a key that moved can say which part did)."""
    from ...config import config

    return {"version": DIRECTOR_VERSION, "words": ctx.words.get("digest"), "script": _sha(ctx.script),
            "topic": (ctx.topic or "").strip(), "others": _sha(json.dumps(list(ctx.others or []))),
            "catalog": catalog.version, "spots": spots_digest(ctx.frame0, ctx.spots),
            "take": int(ctx.take or 0), "model": config.vertex_gemini_fast_model}


def _key_of(inputs: dict) -> str:
    return hashlib.sha256(json.dumps(list(inputs.values()), default=str).encode("utf-8")).hexdigest()[:20]


def director_key(ctx: DirectorCtx, catalog: Catalog) -> str:
    return _key_of(key_inputs(ctx, catalog))


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

DIRECTOR_SYSTEM = (
    "You are the icon director for 42 Abu Dhabi talking-head reels. An icon is a small flat "
    "pictogram beside the speaker that appears the moment the speaker says the idea it shows. "
    "Icons exist to make a KEY IDEA land, never to decorate. Fewer is better."
)

_RULES = """Decide which ideas in this scene get an icon.

Keep an idea only if ALL THREE hold:
(a) it is SAID: w is the index of the word in TRANSCRIPT that names it, w_end the last word of its
    phrase, and quote is exactly the transcript's words from w to w_end;
(b) it is a concrete key idea of THIS line: a benefit, feature, claim or number the viewer should remember;
(c) one icon shows it literally enough to be read in half a second.

Suggest (verdict suggest, never keep) hopeful but general words the viewer may still like to see: future,
journey, dreams, potential, opportunity. A suggestion never shows on its own; the user adds it with one tap.

Refuse:
- filler or generic words (amazing, start, time, things, people);
- the brand or the place (42, Abu Dhabi); greetings;
- anything said inside a negation: "without teachers" is NOT a teacher icon, "forget degrees" is not a
  degree icon, "you don't need a degree or any coding experience" is neither a degree nor code;
- idioms that only look like a keyword: "feel free" is not about price, "dress code" is not code;
- a second icon for the same idea.

Judge EVERY candidate: return exactly one idea per candidate id, with its cand_id and a verdict keep, suggest
or refuse (refuse_reason says why it is refused, or for suggest why it is too general). You may add at most
two ideas the candidates missed (cand_id "new").
When several things are listed together ("code, design, AI and games"), set list_span true on those
ideas and keep at most ONE icon for the whole list (an umbrella), or none.
If an idea is said more than once, set w to the mention where it is the point and list every mention of
it in mentions (w and quote each).
icon: an id from ICONS, or "other:<plain noun>" when none of them fits.
label: 16 characters or fewer. purpose: 60 characters or fewer, plain words, why it helps the viewer.
priority: 1-5, 5 = the core claim of the line. For a refused idea still fill every field.

ROOM: the image is the scene's first frame; each numbered box is a spot where an icon could sit. For
every number answer good or avoid; avoid means the box covers a sign, text, a logo, a screen, another
face or a busy object. home is the ONE good spot best for all this scene's icons, beside the speaker;
home_why says why in 60 characters or fewer. Never mention colours, pixels or seconds.{no_image}"""


def _indexed(words: dict) -> str:
    """The said words with their indices: "0:42 1:Abu … 12:tuition-free," —
    each followed by the punctuation said after it."""
    t = words.get("transcript") or ""
    said = _said(words)
    parts = []
    for k, w in enumerate(said):
        end = w["tchar"] + len(w["text"])
        nxt = said[k + 1]["tchar"] if k + 1 < len(said) else len(t)
        tail = t[end:nxt].strip()
        parts.append(f"{w['w']}:{w['text']}{tail}")
    return " ".join(parts)


def build_prompt(ctx: DirectorCtx, cands: list[dict], catalog: Catalog, *, has_image: bool,
                 missing: Optional[list[str]] = None) -> str:
    """The user prompt of the call (DIRECTOR_SYSTEM is its system line)."""
    lines = [_RULES.format(no_image="" if has_image else
                           "\nThere is no image this time: return room with no spots, home 0 and home_why \"\".")]
    lines.append("")
    lines.append(f"TOPIC: {(ctx.topic or '').strip() or '(none)'}")
    others = [o.strip() for o in ctx.others or [] if (o or "").strip()]
    if others:
        lines.append("OTHER SCENES OF THIS REEL (context only; each scene gets its own icons):")
        lines += [f"- {o}" for o in others]
    lines.append(f"SCRIPT OF THIS SCENE: {(ctx.script or '').strip()}")
    lines.append(f"TRANSCRIPT (what was said, index:word): {_indexed(ctx.words)}")
    lines.append("CANDIDATES (from a keyword table; judge every one):")
    if cands:
        for c in cands:
            ments = ", ".join(str(m["w"]) for m in c["mentions"])
            lines.append(f"- {c['id']}: icon {c['icon']} on \"{c['quote']}\" (w {c['w']}; said at w {ments}; "
                         f"{'strong' if c['strong'] else 'weak'} keyword)")
    else:
        lines.append("- (none)")
    lines.append("ICONS (id: name; also called):")
    for p in catalog.picks:
        al = ", ".join(p.get("aliases", [])[:3])
        lines.append(f"- {p['id']}: {p['name']}" + (f"; {al}" if al else ""))
    if missing:
        lines.append("")
        lines.append("Your last answer gave no verdict for " + ", ".join(missing)
                     + ". Return one idea for EVERY candidate id.")
    return "\n".join(lines)


def prompt_digest(prompt: str) -> str:
    """What the AI was asked, as a digest: its system line, the prompt and the
    answer's schema (the room image is in the key's spots)."""
    return _sha(json.dumps([DIRECTOR_SYSTEM, prompt, DIRECTOR_SCHEMA], sort_keys=True))


def _obj(props: dict) -> dict:
    return {"type": "OBJECT", "properties": props, "required": list(props)}


DIRECTOR_SCHEMA: dict = _obj({
    "ideas": {"type": "ARRAY", "items": _obj({
        "cand_id": {"type": "STRING", "description": "The candidate's id (k1, k2 ...), or \"new\"."},
        "concept": {"type": "STRING", "description": "The idea in a few plain words."},
        "quote": {"type": "STRING", "description": "The transcript's exact words from w to w_end."},
        "w": {"type": "INTEGER"},
        "w_end": {"type": "INTEGER"},
        "mentions": {"type": "ARRAY", "items": _obj({"w": {"type": "INTEGER"}, "quote": {"type": "STRING"}})},
        "icon": {"type": "STRING", "description": "An icon id from ICONS, or other:<plain noun>."},
        "label": {"type": "STRING"},
        "purpose": {"type": "STRING"},
        "priority": {"type": "INTEGER"},
        "verdict": {"type": "STRING", "enum": ["keep", "suggest", "refuse"]},
        "refuse_reason": {"type": "STRING"},
        "list_span": {"type": "BOOLEAN"},
    })},
    "room": _obj({
        "spots": {"type": "ARRAY", "items": _obj({
            "n": {"type": "INTEGER"},
            "verdict": {"type": "STRING", "enum": ["good", "avoid"]},
            "why": {"type": "STRING"},
        })},
        "home": {"type": "INTEGER"},
        "home_why": {"type": "STRING"},
    }),
})


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _clip(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _int(v: Any, default: int = -1) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def well_formed(answer: Any) -> bool:
    """Is this answer readable at all (a dict with an ideas list)?"""
    return isinstance(answer, dict) and isinstance(answer.get("ideas"), list)


VERDICTS = ("keep", "suggest", "refuse")     # what the AI may answer for an idea


def missing_verdicts(answer: Any, cands: list[dict]) -> list[str]:
    """Candidate ids the answer gave no keep / suggest / refuse verdict for."""
    got = set()
    if well_formed(answer):
        for it in answer["ideas"]:
            if isinstance(it, dict) and str(it.get("verdict") or "").lower() in VERDICTS:
                got.add(str(it.get("cand_id") or "").strip().lower())
    return [c["id"] for c in cands if c["id"] not in got]


def locate(words: dict, w: int, quote: str, slack: int = QUOTE_SLACK) -> Optional[tuple[int, int]]:
    """(w, w_end) where `quote` is said, looking up to `slack` said words
    either side of `w`; None when those words are not said there."""
    q = [t for t in (wordsmod.norm(m.group()) for m in wordsmod._WORD.finditer(quote or "")) if t]
    said = _said(words)
    if not q or not said or len(q) > SPAN_MAX + 1:
        return None
    idx = [int(x["w"]) for x in said]
    toks = [wordsmod.norm(x["text"]) for x in said]
    p = min(range(len(idx)), key=lambda k: (abs(idx[k] - int(w)), idx[k]))
    for d in sorted(range(-slack, slack + 1), key=lambda d: (abs(d), d)):
        i = p + d
        if 0 <= i and i + len(q) <= len(idx) and toks[i:i + len(q)] == q:
            return idx[i], idx[i + len(q) - 1]
    return None


def _affirmed(words: dict, w0: int, w1: int, icon: str) -> bool:
    """Does the keyword table itself find `icon` said (not negated, not an
    idiom) inside w0..w1? ("no fees" is a piggy bank.)"""
    by = _by_w(words)
    a, b = by.get(w0), by.get(w1)
    if not a or not b or a.get("tchar") is None or b.get("tchar") is None:
        return False
    lo, hi = a["tchar"], b["tchar"] + len(b["text"])
    return any(m.icon == icon and lo <= m.pos < hi
               for m in brand_icons.find_mentions(words.get("transcript") or ""))


def _span_problem(words: dict, w0: int, w1: int, icon: str) -> Optional[str]:
    """Why the words w0..w1 cannot carry `icon`, or None. The words checked
    for a negation or an idiom are the head word and every word where the
    icon's own keyword matches ("Forget degrees" is checked at "degrees";
    "coding school without teachers" is not refused for </> because of its
    last word). The keyword table affirming the icon there wins ("no fees")."""
    t = words.get("transcript") or ""
    by = _by_w(words)
    span = [by[k] for k in range(w0, w1 + 1) if k in by and by[k].get("said")]
    if not span or span[0]["w"] != w0:
        return WHY_NOT_SAID
    head = span[0]
    content = [x for x in span if wordsmod.norm(x["text"]) not in ("a", "an", "the", "of", "in", "at", "to")]
    if brand_icons.is_brand(head["text"]) or (content and all(brand_icons.is_brand(x["text"]) for x in content)):
        return WHY_BRAND
    if _affirmed(words, w0, w1, icon):
        return None
    lo, hi = head["tchar"], span[-1]["tchar"] + len(span[-1]["text"])
    ats = {head["tchar"]} | {m.pos for m in brand_icons.keyword_spans(t, icon) if lo <= m.pos < hi}
    if any(brand_icons.negated_in_clause(t, at) for at in ats):
        return WHY_NEGATED
    if any(brand_icons.in_idiom(t, at) for at in ats):
        return WHY_IDIOM
    return None


async def resolve_icons(answer: dict, catalog: Catalog) -> dict[str, Optional[tuple[str, float]]]:
    """Every "other:<noun>" (and unknown id) in a kept or suggested idea,
    resolved once."""
    names = set()
    for it in answer.get("ideas") or []:
        if not isinstance(it, dict) or str(it.get("verdict") or "").lower() not in ("keep", "suggest"):
            continue
        icon = str(it.get("icon") or "").strip()
        if icon.lower().startswith("other:"):
            names.add(icon[6:].strip())
        elif icon and not catalog.exists(icon.lower()):
            names.add(icon)
    return {n: await _resolve(catalog, n) for n in sorted(names)}


def _icon_of(icon: str, catalog: Catalog, resolved: dict) -> tuple[Optional[str], str]:
    """(library id, "") or (None, why not)."""
    icon = (icon or "").strip()
    if not icon:
        return None, WHY_NO_ICON_GIVEN
    if icon.lower().startswith("other:"):
        name = icon[6:].strip()
        got = resolved.get(name)
        if not got or got[1] < MATCH_STRONG:
            return None, WHY_NO_ICON_FOR.format(name=_clip(name, 30))
        icon_id = got[0]
    elif catalog.exists(icon.lower()):
        icon_id = icon.lower()
    else:
        got = resolved.get(icon)
        if not got or got[1] < MATCH_STRONG:
            return None, WHY_NO_ICON.format(name=_clip(icon, 30))
        icon_id = got[0]
    if icon_id.endswith("-logo"):
        return None, WHY_LOGO
    return icon_id, ""


def _name_of(catalog: Catalog, icon_id: str) -> str:
    p = next((p for p in catalog.picks if p["id"] == icon_id), None)
    return p["name"] if p else icon_id.replace("-", " ").capitalize()


def _mention(words: dict, w0: int, w1: int) -> dict:
    by = _by_w(words)
    return {"w": w0, "w_end": w1, "quote": _span_quote(words, w0, w1), "s": by[w0]["s"]}


def _idea_times(words: dict, idea: dict) -> None:
    by = _by_w(words)
    idea["word"] = by[idea["w"]]["text"] if idea["w"] in by else ""
    idea["s"] = by[idea["w"]]["s"] if idea["w"] in by else None
    idea["end"] = by[idea["w_end"]]["e"] if idea["w_end"] in by else idea["s"]


def validate(answer: dict, words: dict, cands: list[dict], catalog: Catalog,
             resolved: Optional[dict] = None, *, n_spots: int = 0,
             spots: Optional[list[dict]] = None) -> tuple[list[dict], dict]:
    """(ideas, room) from a well-formed answer: every rule in the module
    docstring applied; candidates the answer skipped come back refused ("no
    verdict from the AI"). Deterministic."""
    resolved = resolved or {}
    by_cand = {c["id"]: c for c in cands}
    ideas: list[dict] = []
    seen_cands: set[str] = set()
    for raw in answer.get("ideas") or []:
        if not isinstance(raw, dict):
            continue
        cid = str(raw.get("cand_id") or "").strip().lower()
        cand = by_cand.get(cid)
        if cand is not None:
            if cid in seen_cands:
                continue
            seen_cands.add(cid)
        verdict = str(raw.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS:
            continue
        idea = {
            "cand_id": cand["id"] if cand else None,
            "source": "ai",
            "concept": _clip(raw.get("concept"), 60),
            "quote": "",
            "w": cand["w"] if cand else -1, "w_end": cand["w_end"] if cand else -1,
            "mentions": [], "table_mentions": [m["w"] for m in cand["mentions"]] if cand else [],
            "icon": cand["icon"] if cand else "",
            "label": _clip(raw.get("label"), LABEL_MAX),
            "purpose": _clip(raw.get("purpose"), PURPOSE_MAX),
            "priority": max(1, min(5, _int(raw.get("priority"), 3))),
            "strong": bool(cand and cand["strong"]),
            "verdict": verdict,
            "reason": _clip(raw.get("refuse_reason"), 80) if verdict != "keep" else "",
            "list_span": bool(raw.get("list_span")),
        }
        # Where it is said: the quote, found near w. A kept (or suggested)
        # idea whose quote is not said there is refused; a refused candidate
        # keeps the table's place; an extra idea said nowhere has nothing to
        # show and is dropped. A suggestion passes the same checks as a keep.
        spot = locate(words, _int(raw.get("w"), idea["w"]), str(raw.get("quote") or ""))
        if spot is not None:
            idea["w"], idea["w_end"] = spot
        elif verdict != "refuse":
            idea["verdict"], idea["reason"] = "refuse", WHY_NOT_THERE
        if idea["w"] < 0:
            continue
        icon_id, why = _icon_of(str(raw.get("icon") or ""), catalog, resolved)
        if icon_id is not None:
            idea["icon"] = icon_id
        elif idea["verdict"] != "refuse":
            idea["verdict"], idea["reason"] = "refuse", why
        if idea["verdict"] != "refuse":
            why = _span_problem(words, idea["w"], idea["w_end"], idea["icon"])
            if why:
                idea["verdict"], idea["reason"] = "refuse", why
        idea["quote"] = _span_quote(words, idea["w"], idea["w_end"])
        # Every mention the AI names that is really said there (the idea's own first).
        ments = {idea["w"]: _mention(words, idea["w"], idea["w_end"])}
        for m in raw.get("mentions") or []:
            if not isinstance(m, dict):
                continue
            got = locate(words, _int(m.get("w")), str(m.get("quote") or ""))
            if got and got[0] not in ments and got[1] - got[0] <= SPAN_MAX \
                    and not _span_problem(words, got[0], got[1], idea["icon"]):
                ments[got[0]] = _mention(words, *got)
        idea["mentions"] = [ments[k] for k in sorted(ments)]
        if not idea["label"]:
            idea["label"] = _clip(_name_of(catalog, idea["icon"]) if idea["icon"] else idea["concept"], LABEL_MAX)
        if not idea["purpose"]:
            idea["purpose"] = _clip(f"Says '{idea['quote']}'", PURPOSE_MAX)
        ideas.append(idea)

    # Candidates the AI never judged: refused, never an icon.
    for c in cands:
        if c["id"] not in seen_cands:
            ideas.append(_from_candidate(words, c, catalog, verdict="refuse", reason=WHY_SILENT))

    _one_per_list(ideas, list_groups(words, cands))
    _one_per_icon_and_word(ideas)
    _cap_extras(ideas)
    for idea in ideas:
        _idea_times(words, idea)
    room = validate_room(answer.get("room"), spots or [], n_spots or len(spots or []))
    return _finish(ideas), room


def _from_candidate(words: dict, c: dict, catalog: Catalog, *, verdict: str, reason: str) -> dict:
    word = c["quote"] or _span_quote(words, c["w"], c["w"])
    idea = {
        "cand_id": c["id"], "source": "keyword", "concept": _name_of(catalog, c["icon"]),
        "quote": c["quote"], "w": c["w"], "w_end": c["w_end"],
        "mentions": [_mention(words, m["w"], m["w_end"]) for m in c["mentions"]],
        "table_mentions": [m["w"] for m in c["mentions"]],
        "icon": c["icon"], "label": _clip(_name_of(catalog, c["icon"]), LABEL_MAX),
        "purpose": _clip(f"Says '{word}'", PURPOSE_MAX),
        "priority": 3 if c["strong"] else 2, "strong": c["strong"], "verdict": verdict,
        "reason": reason, "list_span": False,
    }
    _idea_times(words, idea)
    return idea


def _kept(ideas: list[dict]) -> list[dict]:
    return [i for i in ideas if i["verdict"] == "keep"]


def _better(a: dict, b: dict) -> bool:
    """Does a outrank b? Priority, then STRONG, then the earlier word."""
    return (-a["priority"], 0 if a["strong"] else 1, a["w"]) < (-b["priority"], 0 if b["strong"] else 1, b["w"])


def _one_per_list(ideas: list[dict], groups: list[tuple[int, int]]) -> None:
    """At most one kept icon per list said together."""
    spans = list(groups)
    flagged = sorted((i for i in _kept(ideas) if i["list_span"]), key=lambda i: i["w"])
    for i in flagged:
        # Items of one list sit a separator word apart ("websites or apps").
        lo, hi = i["w"] - 2, i["w_end"] + 2
        for k, (a, b) in enumerate(spans):
            if lo <= b and a <= hi:
                spans[k] = (min(a, i["w"]), max(b, i["w_end"]))
                break
        else:
            spans.append((i["w"], i["w_end"]))
    for a, b in spans:
        inside = [i for i in _kept(ideas) if a <= i["w"] <= b]
        if len(inside) <= 1:
            continue
        best = inside[0]
        for i in inside[1:]:
            if _better(i, best):
                best = i
        for i in inside:
            if i is not best:
                i["verdict"], i["reason"] = "refuse", WHY_LIST


def _one_per_icon_and_word(ideas: list[dict]) -> None:
    """One kept idea per icon (its mentions merged into the winner) and per
    word."""
    for key in ("icon", "w"):
        best: dict[Any, dict] = {}
        for i in _kept(ideas):
            k = i[key]
            if k not in best or _better(i, best[k]):
                best[k] = i
        for i in _kept(ideas):
            win = best[i[key]]
            if i is win:
                continue
            i["verdict"] = "refuse"
            i["reason"] = (WHY_SAME_ICON if key == "icon" else WHY_SAME_WORD).format(label=win["label"])
            if key == "icon":
                have = {m["w"] for m in win["mentions"]}
                win["mentions"] = sorted(win["mentions"] + [m for m in i["mentions"] if m["w"] not in have],
                                         key=lambda m: m["w"])


def _cap_extras(ideas: list[dict]) -> None:
    """At most MAX_EXTRA kept ideas the candidates missed — and as many
    suggested ones."""
    for verdict in ("keep", "suggest"):
        extras = sorted((i for i in ideas if i["verdict"] == verdict and i["cand_id"] is None),
                        key=lambda i: (-i["priority"], i["w"]))
        for i in extras[MAX_EXTRA:]:
            i["verdict"], i["reason"] = "refuse", WHY_EXTRAS


def auto_pick(idea: dict) -> bool:
    """The AUTO floor: kept, priority >= 3, and (STRONG or priority >= 4)."""
    return idea.get("verdict") == "keep" and idea.get("source") == "ai" \
        and int(idea.get("priority") or 0) >= AUTO_MIN_PRIORITY \
        and (bool(idea.get("strong")) or int(idea.get("priority") or 0) >= AUTO_SOLO_PRIORITY)


def rank_key(idea: dict) -> tuple:
    """Who wins a clash among auto ideas: priority, STRONG, earlier word."""
    return (-int(idea.get("priority") or 0), 0 if idea.get("strong") else 1, int(idea.get("w") or 0))


def refused_by(idea: dict, answer: Optional[dict] = None) -> Optional[str]:
    """Who refused an idea: "ai" (its own verdict), "check" (the AI kept it
    and a rule here refused it — a negation, the library, a list…), "silent"
    (the AI gave it no verdict), or None when it is not refused. With the raw
    `answer`, a candidate's verdict there decides (validate reads the first
    item for each candidate); else the reason does: the rules' reasons are
    the WHY_* texts, anything else is the AI's own words."""
    if idea.get("verdict") != "refuse":
        return None
    reason = str(idea.get("reason") or "")
    if reason == WHY_SILENT:
        return "silent"
    if answer is not None and idea.get("cand_id") and well_formed(answer):
        for it in answer["ideas"]:
            if isinstance(it, dict) and str(it.get("cand_id") or "").strip().lower() == idea["cand_id"]:
                verdict = str(it.get("verdict") or "").strip().lower()
                if verdict in VERDICTS:
                    return "ai" if verdict == "refuse" else "check"
                break
    return "check" if _CHECK_RX.match(reason) else "ai"


def hopeful(idea: dict) -> bool:
    """Is this idea a hopeful but general word ("your future", "the
    journey") — by what was said, its concept or its label?"""
    text = " ".join(str(idea.get(k) or "") for k in ("quote", "word", "concept", "label"))
    return bool(HOPEFUL_RX.search(text))


def as_suggestions(body: dict, words: Optional[dict] = None) -> dict:
    """The director's answer as the studio lists it (module docstring:
    HOPEFUL): an idea the AI itself refused (refused_by "ai", not one of our
    rules) that is hopeful but general, said and meant there (not in a
    negation, an idiom or the brand, when `words` are given), comes back as a
    suggestion — verdict "suggest", `hopeful` true, the AI's own reason kept
    as `ai_reason`. A suggestion the AI gave itself is marked `hopeful` when
    it is one. Never automatic. Pure: `body` itself when nothing changes,
    else a copy (the stored answer, its key and the plan's keys never see
    it)."""
    if body.get("source") != "ai":
        return body
    raw = body.get("raw")
    ideas = body.get("ideas") or []
    out: list[dict] = []
    changed = False
    for idea in ideas:
        verdict = idea.get("verdict")
        if verdict == "suggest" and "hopeful" not in idea:
            idea = {**idea, "hopeful": hopeful(idea)}
            changed = True
        elif (verdict == "refuse" and idea.get("icon") and int(idea.get("w", -1)) >= 0 and hopeful(idea)
              and refused_by(idea, raw) == "ai"
              and (words is None or _span_problem(words, int(idea["w"]), int(idea.get("w_end", idea["w"])),
                                                  idea["icon"]) is None)):
            idea = {**idea, "verdict": "suggest", "hopeful": True, "ai_reason": idea.get("reason") or "",
                    "reason": WHY_HOPEFUL, "auto": False}
            changed = True
        out.append(idea)
    return {**body, "ideas": out} if changed else body


def _finish(ideas: list[dict]) -> list[dict]:
    ideas = sorted(ideas, key=lambda i: (i["w"], i["cand_id"] or "~", i["icon"]))
    for k, i in enumerate(ideas, 1):
        i["id"] = f"i{k}"
        i["auto"] = auto_pick(i)
    return ideas


def validate_room(room: Any, spots: list[dict], n_spots: int) -> dict:
    """The room read, trusted only as far as it goes: unknown spot numbers
    dropped, one answer per spot, the home only when it was marked good."""
    out = {"spots": [], "home": None, "home_n": None, "home_why": ""}
    if not isinstance(room, dict) or n_spots <= 0:
        return out
    seen = set()
    for s in room.get("spots") or []:
        if not isinstance(s, dict):
            continue
        n = _int(s.get("n"))
        verdict = str(s.get("verdict") or "").lower()
        if not (1 <= n <= n_spots) or n in seen or verdict not in ("good", "avoid"):
            continue
        seen.add(n)
        out["spots"].append({"n": n, "id": spots[n - 1]["id"] if n - 1 < len(spots) else None,
                             "verdict": verdict, "why": _clip(s.get("why"), WHY_MAX)})
    out["spots"].sort(key=lambda s: s["n"])
    home = _int(room.get("home"))
    if any(s["n"] == home and s["verdict"] == "good" for s in out["spots"]):
        out["home_n"] = home
        out["home"] = spots[home - 1]["id"] if home - 1 < len(spots) else None
        out["home_why"] = _clip(room.get("home_why"), WHY_MAX)
    return out


# ---------------------------------------------------------------------------
# The call, the fallback and the cache
# ---------------------------------------------------------------------------

def fallback(words: dict, cands: list[dict], catalog: Catalog, reason: str) -> list[dict]:
    """Without the AI: every keyword candidate as a SUGGESTION (never auto)."""
    return _finish([_from_candidate(words, c, catalog, verdict="suggest", reason=reason) for c in cands])


def _body(key: Optional[str], source: str, ideas: list[dict], room: dict, cands: list[dict],
          note: str = "", **extra: Any) -> dict:
    body = {"version": DIRECTOR_VERSION, "key": key, "source": source, "ideas": ideas,
            "room": room or {"spots": [], "home": None, "home_n": None, "home_why": ""},
            "candidates": cands, "note": note}
    body.update(extra)
    return body


def _read(saved: Path) -> Optional[dict]:
    try:
        return json.loads(saved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(saved: Path, body: dict) -> None:
    saved.parent.mkdir(parents=True, exist_ok=True)
    tmp = saved.with_name(saved.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(body, separators=(",", ":"), default=str), encoding="utf-8")
    os.replace(tmp, saved)


def cached(work_dir: Path, key: str, *, now: Optional[float] = None) -> Optional[dict]:
    """The stored answer for `key` when it still stands: an AI answer
    always, a failure only within FAIL_BACKOFF_SEC."""
    body = _read(Path(work_dir) / "director.json")
    if not body or body.get("key") != key or body.get("version") != DIRECTOR_VERSION:
        return None
    if body.get("source") == "ai":
        return body
    failed = body.get("failed_at")
    if failed and ((now if now is not None else time.time()) - float(failed)) < FAIL_BACKOFF_SEC:
        return body
    return None


async def _ask(prompt: str, image: Optional[Path]) -> Any:
    from ...config import config
    from .. import vertex_client

    return await asyncio.wait_for(
        vertex_client.gemini_multimodal(
            prompt, images=[image] if image else None, system=DIRECTOR_SYSTEM,
            model=config.vertex_gemini_fast_model, json_schema=DIRECTOR_SCHEMA,
            temperature=0.0, max_tokens=MAX_TOKENS, thinking_budget=THINKING_BUDGET),
        timeout=CALL_TIMEOUT_SEC)


_INFLIGHT: dict[str, asyncio.Task] = {}


async def direct(ctx: DirectorCtx) -> dict:
    """The scene's Director (module docstring). At most two model calls (the
    second only for an unreadable answer, a failed call or a missing
    verdict); none when cached, when ctx.ai is false or when nothing was
    said."""
    catalog = ctx.catalog or default_catalog()
    cands = candidates(ctx.words)
    if not ctx.ai:
        return _body(None, "keywords", fallback(ctx.words, cands, catalog, "keywords only"), {}, cands,
                     note="AI off")
    if not _said(ctx.words):
        return _body(None, "keywords", [], {}, cands, note="nothing said")
    inputs = key_inputs(ctx, catalog)
    key = _key_of(inputs)
    hit = cached(ctx.work_dir, key)
    if hit is not None:
        return hit
    task = _INFLIGHT.get(key)
    if task is None or task.done():
        task = asyncio.ensure_future(_direct(ctx, catalog, cands, key, inputs))
        _INFLIGHT[key] = task
        task.add_done_callback(lambda t, k=key: _INFLIGHT.pop(k, None) if _INFLIGHT.get(k) is t else None)
    return await asyncio.shield(task)


def _call_record(answer: dict, prompt: str, asked: Optional[list[str]], inputs: dict) -> dict:
    """What director.json keeps of the call beside its answer (module
    docstring; none of it is in the key): the answer as it came when it is
    at most RAW_MAX bytes, what was asked, and the key's parts."""
    out: dict = {"prompt_digest": prompt_digest(prompt), "inputs": inputs}
    if asked:
        out["reasked"] = list(asked)
    text = json.dumps(answer, ensure_ascii=False, separators=(",", ":"), default=str)
    size = len(text.encode("utf-8"))
    if size <= RAW_MAX:
        out["raw"] = json.loads(text)
    else:
        out["raw_bytes"] = size
        log.info("[director] the answer (%d bytes) is too big to keep; it cannot be replayed", size)
    return out


async def _direct(ctx: DirectorCtx, catalog: Catalog, cands: list[dict], key: str,
                  inputs: Optional[dict] = None) -> dict:
    t0 = time.time()
    inputs = inputs if inputs is not None else key_inputs(ctx, catalog)
    image = room_image(ctx.frame0, ctx.spots, Path(ctx.work_dir) / "director_room.jpg")
    # (answer, candidates it gave no verdict for, the prompt it answered, the
    # ids that prompt asked about again)
    answers: list[tuple[dict, list[str], str, Optional[list[str]]]] = []
    errors: list[str] = []
    missing: Optional[list[str]] = None
    for _attempt in range(2):
        asked = missing
        prompt = build_prompt(ctx, cands, catalog, has_image=image is not None, missing=asked)
        try:
            got = await _ask(prompt, image)
        except Exception as e:  # noqa: BLE001 — the keyword fallback stands in
            errors.append(f"{type(e).__name__}: {str(e)[:160]}")
            continue
        if not well_formed(got):
            errors.append("an unreadable answer")
            continue
        missing = missing_verdicts(got, cands)
        answers.append((got, missing, prompt, asked))
        if not missing:
            break
    if not answers:
        err = "; ".join(errors)[:300] or "no answer"
        log.warning("[director] AI unavailable (%s); keyword suggestions only", err)
        body = _body(key, "keywords", fallback(ctx.words, cands, catalog, "AI unavailable"), {}, cands,
                     note="AI unavailable", failed_at=time.time(), error=err, inputs=inputs)
        _write(Path(ctx.work_dir) / "director.json", body)
        return body
    answer, _miss, prompt, asked = min(answers, key=lambda a: (len(a[1]), -answers.index(a)))
    raw = hashlib.sha256(json.dumps(answer, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    call = _call_record(answer, prompt, asked, inputs)
    try:
        resolved = await resolve_icons(answer, catalog)
        ideas, room = validate(answer, ctx.words, cands, catalog, resolved, spots=ctx.spots)
    except Exception as e:  # noqa: BLE001 — a paid answer we cannot read is a failure, not a retry loop
        log.exception("[director] could not validate the answer")
        # Its raw answer is kept: once the validation is fixed, it can be
        # judged again for free (replay).
        body = _body(key, "keywords", fallback(ctx.words, cands, catalog, "AI unavailable"), {}, cands,
                     note="AI unavailable", failed_at=time.time(), error=f"validation: {str(e)[:200]}",
                     raw_digest=raw, **call)
        _write(Path(ctx.work_dir) / "director.json", body)
        return body
    body = _body(key, "ai", ideas, room, cands, raw_digest=raw, **call)
    _write(Path(ctx.work_dir) / "director.json", body)
    log.info("[director] %d ideas (%d kept, %d auto) in %.1fs", len(ideas),
             sum(i["verdict"] == "keep" for i in ideas), sum(i["auto"] for i in ideas), time.time() - t0)
    return body


def auto_ideas(director: dict) -> list[dict]:
    """The ideas placed on their own, best first (rank_key)."""
    return sorted((i for i in director.get("ideas") or [] if i.get("auto")), key=rank_key)


async def replay(body: dict, words: dict, catalog: Optional[Catalog] = None, *,
                 spots: Optional[list[dict]] = None,
                 answer: Optional[dict] = None) -> Optional[tuple[list[dict], dict]]:
    """A stored answer judged again by today's rules, with no model call:
    (ideas, room) from validate() on body["raw"] (or `answer`), with the
    candidates the AI was shown (body["candidates"]; today's when none are
    stored), the scene's `words` and its room `spots` (numbered 1..N as the
    room image showed them). None when there is no answer to judge (a
    director.json from before answers were kept, or one too big to keep).
    With the rules unchanged it gives back exactly the stored ideas and room."""
    answer = answer if answer is not None else body.get("raw")
    if not well_formed(answer):
        return None
    catalog = catalog or default_catalog()
    cands = body.get("candidates")
    if not isinstance(cands, list):
        cands = candidates(words)
    resolved = await resolve_icons(answer, catalog)
    return validate(answer, words, cands, catalog, resolved, spots=spots)
