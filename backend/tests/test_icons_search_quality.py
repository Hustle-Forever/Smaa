"""The icon search's quality (icon_catalog.find): 83 names people really
type in the Rebrand studio (the design probe's list: 42 words, coding,
careers, campus life, the UAE), each with the icons a viewer would read as
that name. The old search needed every word and found nothing for 22 of
them; the old matcher was sure of only 54 and paid an AI for the rest.

A name is RIGHT when the first result is one of its icons (and, where it
says so, sure or not: a name no icon shows must NOT be sure, so the studio
offers "Generate with AI"). The floor is 90 %. Free: an autouse guard makes
every paid client raise; nothing reads the database.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config as app_config  # noqa: E402
from app.services import brand_icons as bi  # noqa: E402
from app.services import vertex_client  # noqa: E402
from app.services.rebrand_harness import director as D  # noqa: E402
from app.services.rebrand_harness import icon_catalog as ic  # noqa: E402
from app.services.rebrand_harness import words as W  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "icons_words"
FLOOR = 0.90

SURE, WEAK, ANY = True, False, None
# (name, the icons that are right first, whether it must be sure). None for
# the icons: no library icon shows it (right = not sure: Generate is offered).
CASES: list[tuple[str, frozenset | None, bool | None]] = [(n, frozenset(a) if a else None, s) for n, a, s in (
    ("school", ("graduation-cap", "student", "chalkboard-teacher", "backpack", "chalkboard"), SURE),
    ("developer", ("code",), SURE),
    ("teamwork", ("users-three", "users"), SURE),
    ("hackathon", ("code", "trophy", "lightning", "terminal-window", "laptop"), WEAK),
    ("internship", ("briefcase",), SURE),
    ("salary", ("coins", "money", "wallet", "money-wavy"), SURE),
    ("deadline", ("hourglass", "clock", "calendar-blank", "alarm", "timer"), SURE),
    ("swimming", ("swimming-pool", "person-simple-swim"), SURE),
    ("piscine", ("swimming-pool",), SURE),
    ("halal food", ("bowl-food", "fork-knife"), WEAK),
    ("coffee break", ("coffee",), SURE),
    ("no fees", ("piggy-bank",), SURE),
    ("coding bootcamp", ("code", "graduation-cap", "terminal-window", "laptop"), SURE),
    ("exam", ("exam",), SURE),
    ("mentor", ("chalkboard-teacher", "handshake", "student", "users"), WEAK),
    ("startup", ("buildings", "rocket-launch", "rocket", "lightbulb"), SURE),
    ("job offer", ("briefcase", "envelope-simple", "handshake"), WEAK),
    ("free", ("piggy-bank",), SURE),
    ("network", ("network", "share-network", "globe-hemisphere-east", "users-three", "handshake"), SURE),
    ("friends", ("users-three", "users", "smiley"), SURE),
    ("campus", ("map-pin", "buildings", "graduation-cap"), SURE),
    ("24/7", ("clock",), SURE),
    ("portfolio", ("briefcase", "folders", "folder"), SURE),
    ("resume", ("identification-card", "file-text"), SURE),
    ("cv", ("identification-card", "file-text"), SURE),
    ("interview", ("microphone", "handshake", "chats-circle"), SURE),
    ("phone", ("phone", "device-mobile"), SURE),
    ("chess", ("castle-turret", "crown", "crown-simple", "crown-cross", "horse"), ANY),
    ("football", ("football", "soccer-ball"), SURE),
    ("music", ("music-notes", "music-note", "music-notes-simple", "headphones"), SURE),
    ("medal", ("medal",), SURE),
    ("peer learning", ("users-three",), SURE),
    ("self paced", ("clock",), SURE),
    ("prize", ("gift", "trophy", "medal"), SURE),
    ("cyber security", ("shield-check", "lock"), SURE),
    ("cybersecurity", ("shield-check", "lock"), SURE),
    ("graduation", ("graduation-cap",), SURE),
    ("university", ("graduation-cap", "buildings", "student"), SURE),
    ("scholarship", ("piggy-bank", "graduation-cap"), SURE),
    ("visa", ("credit-card", "identification-card", "airplane-tilt"), ANY),
    ("dorm", ("bed", "house"), SURE),
    ("food", ("bowl-food", "fork-knife", "hamburger"), ANY),
    ("gym", ("barbell",), SURE),
    ("library", ("books", "book-open"), SURE),
    ("books", ("books", "book-open"), SURE),
    ("classroom", ("chalkboard-teacher", "chalkboard", "student"), SURE),
    ("exam stress", ("exam", "smiley-nervous"), WEAK),
    ("certificate", ("certificate",), SURE),
    ("hired", ("briefcase", "handshake", "user-plus"), SURE),
    ("remote work", ("laptop", "house"), SURE),
    ("abu dhabi", ("map-pin", "mosque", "buildings"), WEAK),
    ("desert", ("cactus", "sun-horizon"), SURE),
    ("falcon", ("bird",), WEAK),
    ("camel", None, WEAK),
    ("mosque", ("mosque",), SURE),
    ("ramadan", ("moon-stars", "star-and-crescent", "mosque"), SURE),
    ("emirati", None, WEAK),
    ("arabic", ("translate",), WEAK),
    ("open source", ("code", "git-branch"), WEAK),
    ("bug", ("bug",), SURE),
    ("debug", ("bug", "bug-beetle", "bug-droid"), SURE),
    ("keyboard", ("keyboard",), SURE),
    ("cloud", ("cloud",), SURE),
    ("database", ("database",), SURE),
    ("server", ("hard-drives", "database", "hard-drive"), SURE),
    ("algorithm", ("code", "flow-arrow", "tree-structure"), SURE),
    ("hacker", ("terminal-window", "code"), WEAK),
    ("games", ("game-controller",), SURE),
    ("vr", ("virtual-reality", "cube-focus"), SURE),
    ("3d printing", ("printer", "cube"), WEAK),
    ("drone", ("drone",), SURE),
    ("idea", ("lightbulb",), SURE),
    ("teacher", ("chalkboard-teacher",), SURE),
    ("no teachers", None, WEAK),
    ("coding", ("code",), SURE),
    ("code", ("code",), SURE),
    ("programmer", ("code",), SURE),
    ("web", ("wifi-high", "browser", "globe", "globe-simple", "globe-hemisphere-east"), SURE),
    ("app", ("app-window", "device-mobile"), ANY),
    ("mobile app", ("device-mobile", "app-window"), SURE),
    ("success", ("trophy",), SURE),
    ("growth", ("chart-line-up", "trend-up"), SURE),
    ("tuition free", ("piggy-bank",), SURE),
)]
# The ones the owner named: never allowed to miss.
MUST = ("school", "peer learning", "job offer", "remote work", "algorithm", "mobile app", "dorm", "ramadan",
        "music", "coding bootcamp", "cv", "server")


@pytest.fixture(autouse=True)
def guard(monkeypatch):
    """No paid call, no database: every Vertex call raises, the store
    refuses."""
    calls: list[str] = []

    def refuse(name):
        async def _refuse(*a, **k):
            calls.append(name)
            raise AssertionError(f"paid call to vertex_client.{name} in a test")
        return _refuse

    for name in ("gemini_multimodal", "gemini_image", "gemini_text"):
        if hasattr(vertex_client, name):
            monkeypatch.setattr(vertex_client, name, refuse(name))

    class NoStore:
        def __getattr__(self, name):
            async def _no(*a, **k):
                calls.append(f"store.{name}")
                raise AssertionError("the search read the database")
            return _no

    old = ic.set_store(NoStore())
    yield calls
    ic.set_store(old)
    assert not calls, f"unexpected calls: {calls}"


def right(name: str, ok: frozenset | None, sure: bool | None) -> bool:
    got = ic.find(name)
    first = got["items"][0]["id"] if got["items"] else None
    if ok is None:
        return not got["strong"]
    return first in ok and (sure is None or got["strong"] == sure)


def test_the_list_is_the_83_names():
    assert len(CASES) == 83 and len({n for n, _, _ in CASES}) == 83
    lib = set(ic.library_ids())
    for name, ok, _ in CASES:
        assert ok is None or ok <= lib, (name, ok - lib)


def test_at_least_90_percent_are_right():
    wrong = [(n, [(i["id"], i["score"], i["via"]) for i in ic.find(n, limit=3)["items"]])
             for n, ok, sure in CASES if not right(n, ok, sure)]
    assert 1 - len(wrong) / len(CASES) >= FLOOR, wrong


@pytest.mark.parametrize("name", MUST)
def test_the_owners_names_are_found_first(name):
    ok, sure = next((a, s) for n, a, s in CASES if n == name)
    got = ic.find(name)
    assert got["items"][0]["id"] in ok and got["strong"] == sure, got["items"][:3]


def test_typing_a_start_finds_the_icon():
    # "rock" is on its way to "rocket": the rocket first, not rocket-launch.
    got = ic.find("rock")
    assert [i["id"] for i in got["items"][:2]] == ["rocket", "rocket-launch"]
    assert ic.find("hackat")["items"] == [] or not ic.find("hackat")["strong"]


def test_music_is_the_notes_not_the_microphone():
    ranked = [i["id"] for i in ic.find("music", limit=60)["items"]]
    assert ranked[0].startswith("music-note")
    assert ranked.index("music-note") < ranked.index("microphone")


def test_a_negated_name_is_never_sure():
    for q in ("no teachers", "without teachers", "no school", "no laptop"):
        assert not ic.find(q)["strong"], q


def test_brand_logos_are_never_returned():
    for q in ("github", "instagram", "linkedin", "apple logo", "youtube", "whatsapp", "logo"):
        assert not any(i["id"].endswith("-logo") for i in ic.find(q, limit=200)["items"]), q


def test_the_directors_other_school_is_sure_now():
    """The one live answer refused "other:school" at 0.77 (a library gap):
    the director's resolver is the same free search now, so a new answer
    keeps it — the key of the old answer does not move (C9)."""
    cat = D.default_catalog()
    import asyncio

    got = asyncio.run(D._resolve(cat, "school"))
    assert got[0] == "graduation-cap" and got[1] >= D.MATCH_STRONG
    assert cat.version == ic.catalogue_version() == "phosphor-fill-469c06057a"


def test_the_director_key_is_unchanged(monkeypatch):
    """A scene's director key is exactly what it was before the one search:
    the resolver is not in the key, and the catalogue version is still the
    fill file's (815's real key is checked in test_icons_director_dryrun)."""
    monkeypatch.setattr(app_config, "config", dataclasses.replace(app_config.config,
                                                                  vertex_gemini_fast_model="gemini-2.5-flash"))
    fx = json.loads((FIX / "815.json").read_text(encoding="utf-8"))
    env = W.shape_envelope(fx["env"]["rms"], fx["env"]["thr"], fx["env"]["peak"], fx["env"]["voiced"])
    words = W.build_words(fx["chunks"], fx["dialogue"], None, fx["duration"], env=env)
    ctx = D.DirectorCtx(words=words, script=fx["dialogue"], work_dir=Path("."), topic="42 Abu Dhabi")
    assert D.director_key(ctx, D.default_catalog()) == KEY_815_FIXTURE


# director_key of the 815 fixture context above, made before the search
# changed (the catalogue version and the key's parts are as they were).
KEY_815_FIXTURE = "597ef9a950762e050ceb"


def test_every_synonym_is_a_library_icon_and_a_new_phrase():
    lib = set(ic.library_ids())
    names = {ic.name_key(a) for d, _, al in bi.NAMES for a in (d, *al)}
    seen: set[str] = set()
    for phrase, strength, ids in bi.SYNONYMS:
        k = ic.name_key(phrase)
        assert phrase == phrase.lower() and k == phrase, phrase
        assert k not in seen and k not in names, f"{phrase!r} repeats a phrase or a NAMES alias"
        seen.add(k)
        assert strength in ("strong", "related") and 1 <= len(ids) <= 3, phrase
        assert set(ids) <= lib and len(set(ids)) == len(ids), (phrase, set(ids) - lib)
        assert not any(i.endswith("-logo") for i in ids)
    assert len(bi.SYNONYMS) >= 100


def test_a_related_synonym_is_never_sure():
    for phrase, strength, ids in bi.SYNONYMS:
        if strength != "related":
            continue
        got = ic.find(phrase)
        assert not got["strong"] and got["items"][0]["id"] in ids, (phrase, got["items"][:2])


def test_search_is_quick():
    import time

    ic.find("warm up")
    t0 = time.perf_counter()
    for name, _, _ in CASES:
        ic.find(name)
    assert (time.perf_counter() - t0) / len(CASES) < 0.1
