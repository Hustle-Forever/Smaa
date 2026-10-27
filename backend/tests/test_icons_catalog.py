"""The icon library (rebrand_harness/icon_catalog.py): the baked Phosphor
set, the 42 picks, the one search (find: names, synonyms, keywords, tags,
words, spelling, the team's icons — free), and the team's glyphs drawn by
the AI image model ONLY on a press ("Generate with AI"), capped per day and
stored once per (name, take).

No real model or database is ever touched: an autouse guard makes the paid
Vertex calls raise unless a test patches them, and the team-glyph store is
an in-memory fake. Caches go to tmp_path. The search's quality on 83
realistic names is test_icons_search_quality.py.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import brand_icons as bi  # noqa: E402
from app.services import vertex_client  # noqa: E402
from app.services.rebrand_harness import icon_catalog as ic  # noqa: E402
from app.services.rebrand_harness import icon_render as ir  # noqa: E402
from app.services.rebrand_harness import label_font as lf  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


class FakeStore:
    """yas_rebrand_icons in memory. `old` rows were made more than a day ago
    (they do not count against today's drawings)."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.clock = 0
        self.down = False

    def _up(self):
        if self.down:
            raise ConnectionError("the database is down")

    async def by_id(self, icon_id):
        self._up()
        r = self.rows.get(icon_id)
        return dict(r) if r else None

    async def takes(self, name_key, origin):
        self._up()
        found = [dict(r) for r in self.rows.values() if r["name_key"] == name_key and r["origin"] == origin]
        return sorted(found, key=lambda r: (r["take"], r["created_at"]))

    async def insert(self, row):
        self._up()
        if row["id"] not in self.rows:
            self.clock += 1
            self.rows[row["id"]] = {"label": "", "tags": [], "origin": "ai", "take": 0, **row,
                                    "created_at": f"t{self.clock:04d}", "hidden_at": None}
        return dict(self.rows[row["id"]])

    async def set_hidden(self, icon_id, hidden):
        self._up()
        r = self.rows.get(icon_id)
        if not r or bool(r["hidden_at"]) == hidden:
            return False
        r["hidden_at"] = "now" if hidden else None
        return True

    async def listing(self, limit, include_hidden, origin=None):
        self._up()
        rows = [{k: v for k, v in r.items() if k not in ("png", "prompt", "report")} for r in self.rows.values()
                if (include_hidden or not r["hidden_at"]) and (origin is None or r["origin"] == origin)]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)[:limit]

    async def count_since(self, origin, hours):
        self._up()
        return sum(1 for r in self.rows.values() if r["origin"] == origin and not r.get("old"))


@pytest.fixture(autouse=True)
def guard(monkeypatch, tmp_path):
    """Paid calls raise unless patched; a fake store; caches in tmp_path."""
    calls: list[str] = []

    def refuse(name):
        async def _refuse(*a, **k):
            calls.append(name)
            raise AssertionError(f"paid call to vertex_client.{name} in a test")
        return _refuse

    monkeypatch.setattr(vertex_client, "gemini_multimodal", refuse("gemini_multimodal"))
    monkeypatch.setattr(vertex_client, "gemini_image", refuse("gemini_image"))
    monkeypatch.setattr(ic, "CACHE_DIR", tmp_path / "icons_cache")
    monkeypatch.delenv("ICON_DRAWS_PER_DAY", raising=False)
    ic._DRAW_FAILED.clear()
    ic._REJECTED.clear()
    store = FakeStore()
    old = ic.set_store(store)
    state = {"calls": calls, "store": store}
    yield state
    ic.set_store(old)
    ic._REJECTED.clear()
    assert not calls, f"unexpected paid calls: {calls}"


def run(coro):
    return asyncio.run(coro)


def _png(arr: np.ndarray) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def drawing(kind: str = "disc", size: int = 512) -> bytes:
    """A synthetic 'model answer': black shapes on white."""
    import cv2

    img = np.full((size, size, 3), 255, np.uint8)
    c = size // 2
    if kind in ("disc", "specks"):
        cv2.circle(img, (c, c), size // 4, (0, 0, 0), -1)
    if kind == "specks":
        for x, y in ((30, 40), (470, 60), (60, 470)):
            cv2.circle(img, (x, y), 3, (0, 0, 0), -1)
    if kind == "two":
        cv2.rectangle(img, (100, 150), (220, 360), (0, 0, 0), -1)
        cv2.rectangle(img, (290, 150), (410, 360), (0, 0, 0), -1)
    if kind == "black":
        img[:] = 0
    if kind == "speck":
        cv2.circle(img, (c, c), 8, (0, 0, 0), -1)
    if kind == "frame":                               # a bar touching three edges
        img[:, :60] = 0
        img[:60, :] = 0
        img[-60:, :] = 0
    if kind == "grey_page":
        img[:] = 120
        cv2.circle(img, (c, c), size // 4, (0, 0, 0), -1)
    if kind == "colour":
        cv2.circle(img, (c, c), size // 4, (0, 0, 255), -1)   # a red disc (BGR)
    if kind == "thin_ring":                           # a hairline pictogram, far from Phosphor bold
        cv2.circle(img, (c, c), int(size * 0.4), (0, 0, 0), max(2, size // 50), lineType=cv2.LINE_AA)
    return _png(img[..., ::-1].copy())


# ---------------------------------------------------------------------------
# The library
# ---------------------------------------------------------------------------

def test_the_baked_library_loads_without_brand_logos():
    data = json.loads(ic.CATALOGUE_PATH.read_text(encoding="utf-8"))
    assert data["source"].startswith("@phosphor-icons/core@2.1.1") and data["viewbox"] == 256
    assert ic.count() == len(ic.library_ids()) > 1000
    for icon_id in ic.library_ids():
        e = ic.icon(icon_id)
        assert not icon_id.endswith("-logo") and "brands" not in e["categories"]
        assert e["d"].startswith("M") and e["name"]
    assert (ic.CATALOGUE_PATH.parent / "ICONS-LICENSE.txt").read_text(encoding="utf-8").count("MIT")
    assert ic.catalogue_version().startswith("phosphor-fill-")


def test_the_catalogue_version_does_not_depend_on_line_endings(tmp_path, monkeypatch):
    # git (core.autocrlf) checks the JSON out with CRLF on Windows and LF on
    # the VPS: both must give one version, or every cache keyed on it splits.
    raw = ic.CATALOGUE_PATH.read_bytes().replace(b"\r\n", b"\n")
    versions = []
    for name, body in (("lf.json", raw), ("crlf.json", raw.replace(b"\n", b"\r\n"))):
        (tmp_path / name).write_bytes(body)
        monkeypatch.setattr(ic, "CATALOGUE_PATH", tmp_path / name)
        ic._catalogue.cache_clear()
        try:
            versions.append(ic.catalogue_version())
        finally:
            ic._catalogue.cache_clear()
    monkeypatch.undo()
    assert versions[0] == versions[1] == ic.catalogue_version()


def test_every_keyword_icon_and_pick_is_in_the_library():
    lib = set(ic.library_ids())
    assert bi.icon_ids() <= lib
    script = (REPO / "services" / "remotion" / "scripts" / "make_brand42_assets.py").read_text(encoding="utf-8")
    block = script[script.index("ICON_IDS: tuple"):script.index(")\n", script.index("ICON_IDS: tuple"))]
    icon_ids = set(re.findall(r'"([a-z0-9-]+)"', block))
    assert len(icon_ids) == 60
    assert len(ic.PICK_IDS) == 60 == len(set(ic.PICK_IDS))
    assert set(ic.PICK_IDS) == icon_ids and icon_ids <= lib
    assert [g for g, _ in ic.PICKS] == ["Money", "Code", "People", "Learning", "Career", "Time",
                                        "Places", "Fun"]


def test_the_picks_draw_the_same_paths_as_the_brand_frame():
    ts = REPO / "services" / "remotion" / "src" / "brand42" / "icons.ts"
    if not ts.is_file():
        pytest.skip("the Remotion sources are not in this checkout")
    src = ts.read_text(encoding="utf-8")
    vendored = json.loads(src[src.index("= {") + 2: src.rindex("}") + 1])
    assert {k: ic.library_d(k) for k in vendored} == vendored


def test_names_table():
    rows = bi.name_rows()
    by_name = {r["name"]: r["id"] for r in rows}
    for name, icon_id in {"Developer": "code", "Free": "piggy-bank", "Piscine": "swimming-pool",
                          "AI": "robot", "Team": "users-three", "Jobs": "briefcase",
                          "Money": "money"}.items():
        assert by_name[name] == icon_id
    lib = set(ic.library_ids())
    assert all(r["id"] in lib for r in rows) and len(rows) == len(bi.NAMES)
    # Every display name and alias is one key, used once in the whole table.
    seen: dict[str, str] = {}
    for display, icon_id, aliases in bi.NAMES:
        for a in (display, *aliases):
            k = ic.name_key(a)
            assert k and k not in seen, f"{a!r} ({icon_id}) repeats {seen.get(k)}"
            seen[k] = icon_id
            if a in aliases:
                assert a == a.lower()
    # A plain alias never shadows another library icon's own id.
    spaced = {i.replace("-", " "): i for i in lib}
    for display, icon_id, aliases in bi.NAMES:
        for a in aliases:
            other = spaced.get(ic.name_key(a))
            assert other in (None, icon_id), f"alias {a!r} of {icon_id} is the id {other}"
    # Every pick has a studio name.
    assert set(ic.PICK_IDS) <= {i for _, i, _ in bi.NAMES}


def test_catalogue_public_shape():
    pub = ic.catalogue_public()
    assert set(pub) == {"version", "picks", "count"}          # no "names": the Names tab is gone
    assert sum(len(g["items"]) for g in pub["picks"]) == 60
    item = pub["picks"][1]["items"][0]
    assert item == {"id": "code", "name": "Code", "label": "Developer", "d": ic.library_d("code"),
                    "b": ic.library_d("code", "bold")}


def test_name_key():
    assert ic.name_key("  Tuition-Free!! ") == "tuition free"
    assert ic.name_key("Café  Culture") == "cafe culture"
    assert ic.name_key("24/7") == "24/7"
    assert ic.name_key("x" * 100) == "x" * ic.NAME_MAX
    assert ic.name_key("") == ""


# ---------------------------------------------------------------------------
# find(): the one search
# ---------------------------------------------------------------------------

def ids(got: dict) -> list[str]:
    return [i["id"] for i in got["items"]]


def test_find_puts_the_id_first_and_pages():
    got = ic.find("code")
    assert ids(got)[0] == "code" and got["strong"] and got["via"] == "name"
    item = got["items"][0]
    assert set(item) == {"id", "source", "origin", "name", "label", "d", "b", "tags", "score", "via"}
    assert item["source"] == "library" and item["origin"] == "phosphor" and item["label"] == "Developer"
    assert item["d"] == ic.library_d("code") and item["b"] == ic.library_d("code", "bold")
    assert ids(ic.find("cod"))[0] == "code"                   # a start finds it
    assert ids(ic.find("developer"))[0] == "code"             # a studio name
    assert ids(ic.find("money"))[0] == "money"
    full = ic.find("arrow", limit=200)
    assert full["total"] > 20
    page = ic.find("arrow", offset=10, limit=5)
    assert page["total"] == full["total"] and ids(page) == ids(full)[10:15]
    scores = [i["score"] for i in full["items"]]
    assert scores == sorted(scores, reverse=True)             # best first


def test_an_empty_query_lists_the_picks_first():
    got = ic.find("", limit=60)
    assert got["total"] == ic.count() and not got["strong"]
    assert ids(got) == list(ic.PICK_IDS)
    assert ids(ic.find("", offset=60, limit=3)) == sorted(set(ic.library_ids()) - set(ic.PICK_IDS))[:3]


def test_find_never_returns_brand_logos_or_nonsense():
    for q in ("logo", "instagram", "github", "apple", "twitter"):
        assert not any(i.endswith("-logo") for i in ids(ic.find(q, limit=200)))
    got = ic.find("zzqqxx")
    assert got["items"] == [] and got["total"] == 0 and not got["strong"] and got["best"] == 0.0


def test_any_word_counts_not_every_word():
    # "job offer": no icon has both words, and the briefcase is still found
    # (the old search needed every word and found nothing).
    got = ic.find("job offer")
    assert ids(got)[0] == "briefcase" and not got["strong"]
    # A whole name still wins over its words.
    assert ids(ic.find("car battery"))[0] == "car-battery"


@pytest.mark.parametrize("name, want", [
    ("Developer", "code"), ("developers", "code"), ("Piscine", "swimming-pool"), ("AI", "robot"),
    ("artificial intelligence", "robot"), ("Team", "users-three"), ("peer-to-peer", "users-three"),
    ("Free", "piggy-bank"), ("tuition free", "piggy-bank"), ("Jobs", "briefcase"),
    ("Money", "money"), ("rocket", "rocket"), ("coffee", "coffee"), ("Café", "coffee"),
    ("swimming pool", "swimming-pool"), ("pizza", "pizza"), ("graduation", "graduation-cap"),
    ("develper", "code"), ("school", "graduation-cap"), ("peer learning", "users-three"),
    ("remote working", "laptop"), ("dorms", "bed"),
])
def test_strong_names_are_found_first_for_free(name, want):
    got = ic.find(name)
    assert got["strong"] and ids(got)[0] == want, got["items"][:3]
    assert got["items"][0]["score"] >= ic.MATCH_STRONG and got["best"] == got["items"][0]["score"]


def test_how_each_was_found():
    assert ic.find("Developer")["via"] == "name"
    assert ic.find("school")["via"] == "synonym"
    assert ic.find("develper")["via"] == "fuzzy"
    assert ic.find("rock")["via"] == "words"
    assert ic.find("self paced learning")["via"] in ("name", "keyword", "words", "synonym")


def test_a_negated_query_is_never_sure():
    for q in ("no teachers", "no school", "without a laptop"):
        assert not ic.find(q)["strong"], q
    # ...but a name that says "no" itself is one ("No fees" is the piggy bank).
    got = ic.find("no fees")
    assert got["strong"] and ids(got)[0] == "piggy-bank"


def test_a_tag_many_icons_share_is_weak():
    # "apple" tags four Apple-product icons: not an apple.
    assert not ic.find("apple")["strong"]
    assert not ic.find("chess")["strong"]


def test_a_longer_phrase_takes_its_words_first():
    # "3d printing" is a related synonym (the printer): not the sure "3d".
    got = ic.find("3d printing")
    assert ids(got)[0] == "printer" and not got["strong"]
    # A phrase inside a longer query counts, a little less.
    inside = ic.find("remote work from dubai")
    assert ids(inside)[0] == "laptop" and inside["strong"]


def test_resolve_is_the_library_alone():
    assert ic.resolve("cash") == ("money", 0.99, "name")
    assert ic.resolve("school")[:2] == ("graduation-cap", 0.94)
    assert ic.resolve("cyber cafe")[1] < ic.MATCH_STRONG
    assert ic.resolve("") is None and ic.resolve("zzqqxx") is None
    team = [{"id": "c-aaaaaaaaaaaa", "name": "Money", "label": "Money", "tags": [], "origin": "ai"}]
    assert ic.find("money", team=team, library_only=True)["items"][0]["id"] == "money"


def test_team_icons_are_found_by_name_label_and_tags():
    team = [{"id": "c-aaaaaaaaaaaa", "name": "Hackathon", "label": "Hackathon", "tags": ["coding night"],
             "origin": "ai"},
            {"id": "c-bbbbbbbbbbbb", "name": "Falcon", "label": "Falcon", "tags": [], "origin": "upload",
             "hidden": True}]
    got = ic.find("hackathon", team=team)
    top = got["items"][0]
    assert top["id"] == "c-aaaaaaaaaaaa" and got["strong"] and top["source"] == "custom"
    assert top["origin"] == "ai" and top["d"] is None and top["b"] is None and top["score"] == 1.0
    assert ids(ic.find("coding night", team=team))[0] == "c-aaaaaaaaaaaa"
    assert ids(ic.find("hackatho", team=team))[0] == "c-aaaaaaaaaaaa"      # still typing
    assert "c-bbbbbbbbbbbb" not in ids(ic.find("falcon", team=team))       # hidden: never listed
    # Without the team, the library's own answer (weak: Generate is offered).
    assert not ic.find("hackathon")["strong"]


def test_search_carries_the_generate_offer(guard):
    got = run(ic.search("hackathon"))
    assert not got["strong"]
    g = got["generate"]
    assert g == {"offer": True, "name": "hackathon", "cost_usd": ic.draw_cost_usd(), "per_day": 25,
                 "left_today": 25, "why_not": None}
    brand = run(ic.search("Instagram"))["generate"]
    assert brand["offer"] is False and "brand" in brand["why_not"]
    assert run(ic.search("x"))["generate"]["offer"] is False            # one letter
    assert run(ic.search(""))["generate"]["offer"] is False
    # The team's icon is searched too, once it is drawn.
    guard["store"].rows["c-aaaaaaaaaaaa"] = {"id": "c-aaaaaaaaaaaa", "name": "Hackathon", "name_key": "hackathon",
                                             "label": "Hackathon", "tags": [], "origin": "ai", "take": 0,
                                             "png": b"x", "created_at": "t0001", "hidden_at": None}
    ic._team_changed()
    again = run(ic.search("hackathon"))
    assert again["items"][0]["id"] == "c-aaaaaaaaaaaa" and again["strong"]
    assert again["generate"]["left_today"] == 24


def test_the_team_index_is_cached_and_survives_a_dead_table(guard, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(ic, "_clock", lambda: clock["now"])
    ic._team_changed()
    reads = []
    real = guard["store"].listing

    async def listing(*a, **k):
        reads.append(a)
        return await real(*a, **k)

    monkeypatch.setattr(guard["store"], "listing", listing)
    run(ic.team_index())
    run(ic.team_index())
    assert len(reads) == 1                                 # cached
    clock["now"] += ic.TEAM_TTL_SEC + 1
    guard["store"].down = True
    assert run(ic.team_index()) == []                      # what was known, and the search still works
    assert ids(run(ic.search("code")))[0] == "code"
    guard["store"].down = False
    run(ic.team_index())
    assert len(reads) == 2                                 # a failed read waits TEAM_RETRY_SEC
    clock["now"] += ic.TEAM_RETRY_SEC + 1
    run(ic.team_index())
    assert len(reads) == 3


# ---------------------------------------------------------------------------
# Team glyphs: the drawing's clean-up
# ---------------------------------------------------------------------------

def test_prepare_drawing_keys_crops_and_checks():
    png, report = ic.prepare_drawing(drawing("disc"))
    a = ir.glyph_alpha(("png", png), ic.GLYPH_PX)
    assert a.shape == (ic.GLYPH_PX, ic.GLYPH_PX)
    ys, xs = np.nonzero(a > 0.5)
    m = ic.GLYPH_MARGIN
    # Cropped and centred: the disc fills the box inside the margin.
    assert m - 2 <= ys.min() <= m + 2 and m - 2 <= xs.min() <= m + 2
    assert ic.GLYPH_PX - m - 3 <= ys.max() <= ic.GLYPH_PX - m + 1
    assert 0.03 <= report["ink"] <= 0.70 and report["shapes"] == 1
    assert report["weight"] == "solid"                     # a disc is a solid shape: left alone
    from PIL import Image

    im = Image.open(io.BytesIO(png))
    assert im.mode == "RGBA" and np.asarray(im)[..., :3].max() == 0   # black ink, alpha = shape


def test_prepare_drawing_drops_specks_keeps_real_parts():
    _, report = ic.prepare_drawing(drawing("specks"))
    assert report["shapes"] == 1 and report["dropped"] == 3
    _, two = ic.prepare_drawing(drawing("two"))
    assert two["shapes"] == 2 and two["dropped"] == 0


@pytest.mark.parametrize("kind, why", [
    ("black", "white background"), ("speck", "too faint"), ("frame", "edge to edge"),
    ("grey_page", "white background"), ("colour", "flat black"),
])
def test_prepare_drawing_refuses_bad_drawings(kind, why):
    with pytest.raises(ic.DrawError, match=why):
        ic.prepare_drawing(drawing(kind))
    with pytest.raises(ic.DrawError):
        ic.prepare_drawing(b"not an image")


def _ring(share: float, px: int = ic.WORK_PX) -> np.ndarray:
    import cv2

    a = np.zeros((px, px), np.float32)
    cv2.circle(a, (px // 2, px // 2), int(px * 0.36), 1.0, max(1, round(share * px)), lineType=cv2.LINE_AA)
    return a


def test_stroke_width_reads_phosphor_bold_as_its_own_weight():
    a = ir.path_alpha(ic.library_d("code", "bold"), ic.WORK_PX)
    assert ic.stroke_width(a) / ic.WORK_PX == pytest.approx(ic.BOLD_STROKE, rel=0.03)
    assert ic.match_weight(a)[1]["weight"] == "kept"
    for share in (0.04, 0.1, 0.2):
        assert ic.stroke_width(_ring(share)) / ic.WORK_PX == pytest.approx(share, rel=0.06)
    assert ic.stroke_width(np.zeros((64, 64), np.float32)) is None


def test_match_weight_brings_strokes_to_phosphor_bold():
    lo, hi = (r * ic.BOLD_STROKE for r in ic.WEIGHT_RANGE)
    for share, how in ((0.055, "bolder"), (0.2, "thinner")):
        out, rep = ic.match_weight(_ring(share))
        w = ic.stroke_width(out) / ic.WORK_PX
        assert rep["weight"] == how and lo <= w <= hi, (share, w)
        assert rep["weight_to"] == pytest.approx(w * ic.GLYPH_PX, abs=0.2)
    # A hairline moves towards it by at most half its own width (detail is
    # never crushed), a stroke already bold stays, a disc is left alone.
    out, rep = ic.match_weight(_ring(0.04))
    assert rep["weight"] == "bolder"
    assert ic.stroke_width(out) / ic.stroke_width(_ring(0.04)) == pytest.approx(1 + ic.WEIGHT_STEP_MAX, abs=0.08)
    for share in (0.094, 0.12):
        a = _ring(share)
        out, rep = ic.match_weight(a)
        assert rep["weight"] == "kept" and np.array_equal(out, a)
    import cv2

    disc = np.zeros((ic.WORK_PX, ic.WORK_PX), np.float32)
    cv2.circle(disc, (512, 512), 440, 1.0, -1)
    out, rep = ic.match_weight(disc)
    assert rep["weight"] == "solid" and np.array_equal(out, disc)


def test_match_weight_never_opens_a_knock_out_or_joins_strokes():
    """A filled shape with a hole close to its edge (an icon set's "fill"
    style) is thinned, but never through that edge: the full step would open
    the hole (what it did before), so it is halved until the glyph keeps its
    one piece and its one hole. Two thin strokes made bolder stay two."""
    import cv2

    a = np.zeros((ic.WORK_PX, ic.WORK_PX), np.float32)
    cv2.rectangle(a, (150, 250), (874, 774), 1.0, -1)
    cv2.circle(a, (512, 600), 120, 0.0, -1)                         # 54 px from the bottom edge
    w = ic.stroke_width(a)
    want = max(ic.BOLD_STROKE * ic.WORK_PX, w * (1.0 - ic.WEIGHT_STEP_MAX))
    r = int(round((w - want) / 2.0))
    full = cv2.erode(a, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
    assert ic._pieces(a) == (1, 1) and ic._pieces(full) == (1, 0)    # the full step opens it
    out, rep = ic.match_weight(a)
    assert ic._pieces(out) == (1, 1) and rep["weight"] == "thinner"
    assert rep["weight_to"] < rep["weight_from"]

    lines = np.zeros((ic.WORK_PX, ic.WORK_PX), np.float32)
    for x in (400, 440):                  # 30 px strokes 10 px apart: the full step (8 px a side) joins them
        cv2.line(lines, (x, 200), (x, 820), 1.0, 30)
    assert ic._pieces(lines) == (2, 0)
    out, rep = ic.match_weight(lines)
    assert ic._pieces(out) == (2, 0) and rep["weight"] == "bolder"


def test_a_hairline_drawing_is_made_bolder():
    png, report = ic.prepare_drawing(drawing("thin_ring", 768))
    assert report["weight"] == "bolder" and report["weight_to"] > report["weight_from"]
    a = ir.glyph_alpha(("png", png), ic.GLYPH_PX)
    assert ic.stroke_width(a) == pytest.approx(report["weight_to"], rel=0.12)


# ---------------------------------------------------------------------------
# Drawing with the AI: only on a press
# ---------------------------------------------------------------------------

def _fake_image(monkeypatch, answers):
    calls = []

    async def fake(prompt, **kw):
        calls.append((prompt, kw))
        a = answers[min(len(calls), len(answers)) - 1]
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(vertex_client, "gemini_image", fake)
    return calls


def test_a_press_draws_once_ever_in_the_bold_style(monkeypatch, guard):
    calls = _fake_image(monkeypatch, [drawing("disc")])
    got = run(ic.draw("Halal food"))
    cid = ic.custom_id("halal food")
    assert got == {"id": cid, "name": "Halal food", "label": "Halal food", "origin": "ai", "take": 0,
                   "created": True, "was_hidden": False, "report": got["report"],
                   "cost_usd": ic.draw_cost_usd()}
    assert got["report"]["shapes"] == 1 and len(calls) == 1
    prompt, kw = calls[0]
    assert prompt == ic.DRAW_PROMPT.format(name="Halal food") and kw.get("aspect_ratio") == "1:1"
    assert "Phosphor Bold" in prompt and "no text" in prompt and "one shape" in prompt
    row = guard["store"].rows[cid]
    assert row["name_key"] == "halal food" and row["png"].startswith(b"\x89PNG") and row["prompt"] == prompt
    assert row["origin"] == "ai" and row["take"] == 0 and row["source_kind"] == "ai" and row["report"]
    assert row["label"] == "Halal food" and row["tags"] == [] and "created_by" in row
    # The same name again (any case / spacing, any machine): the stored
    # glyph, no drawing, no cost.
    again = run(ic.draw("  HALAL   food "))
    assert again["id"] == cid and not again["created"] and again["cost_usd"] == 0 and len(calls) == 1
    assert ic.is_custom_id(cid) and re.fullmatch(r"c-[0-9a-f]{12}", cid)


def test_try_again_is_a_new_take_and_the_old_one_steps_aside(monkeypatch, guard):
    calls = _fake_image(monkeypatch, [drawing("disc"), drawing("two")])
    first = run(ic.draw("falcon"))
    second = run(ic.draw("falcon", again=True))
    assert len(calls) == 2 and second["created"] and second["take"] == 1
    assert second["id"] == ic.draw_id("falcon", 1) != first["id"] == ic.draw_id("falcon", 0)
    rows = guard["store"].rows
    assert rows[first["id"]]["hidden_at"] and not rows[second["id"]]["hidden_at"]
    # The first take's pixels never changed (its id is its own).
    assert rows[first["id"]]["png"] != rows[second["id"]]["png"]
    # Without "again": the take shown, no call.
    assert run(ic.draw("Falcon"))["id"] == second["id"] and len(calls) == 2


def test_a_discarded_drawing_comes_back_free(monkeypatch, guard):
    calls = _fake_image(monkeypatch, [drawing("disc")])
    cid = run(ic.draw("falcon"))["id"]
    assert run(ic.hide(cid))
    back = run(ic.draw("Falcon"))
    assert back["id"] == cid and back["was_hidden"] and not back["created"] and back["cost_usd"] == 0
    assert len(calls) == 1 and guard["store"].rows[cid]["hidden_at"] is None


def test_brand_names_are_never_drawn(monkeypatch):
    calls = _fake_image(monkeypatch, [drawing("disc")])
    for name in ("Instagram", "TikTok", "nike logo", "my youtube channel"):
        assert ic.is_brand_name(name)
        with pytest.raises(ic.DrawError, match="brand") as e:
            run(ic.draw(name))
        assert e.value.status == 422
    with pytest.raises(ic.DrawError, match="Type a name") as e:
        run(ic.draw(" x "))
    assert e.value.status == 422
    assert not ic.is_brand_name("halal food") and not ic.is_brand_name("cyber cafe")
    assert not calls


def test_the_teams_drawings_are_capped_per_day(monkeypatch, guard):
    """25 a day for the whole team (every machine's rows in the table, plus
    this process's rejected drawings): past it, 429 before any call."""
    calls = _fake_image(monkeypatch, [drawing("disc")])
    for n in range(ic.DRAW_PER_DAY):
        guard["store"].rows[f"c-{n:012x}"] = {"id": f"c-{n:012x}", "name": f"n{n}", "name_key": f"n{n}",
                                               "origin": "ai", "take": 0, "png": b"x", "created_at": "t",
                                               "hidden_at": None}
    assert run(ic.draws_left()) == 0
    with pytest.raises(ic.DrawError, match="25 AI drawings") as e:
        run(ic.draw("falcon"))
    assert e.value.status == 429 and not calls
    offer = run(ic.generate_offer("falcon"))
    assert offer["offer"] is False and offer["left_today"] == 0 and "25" in offer["why_not"]
    # Yesterday's do not count; uploads never count.
    for n in range(3):
        guard["store"].rows[f"c-{n:012x}"]["old"] = True
    guard["store"].rows["c-ffffffffffff"] = {"id": "c-ffffffffffff", "name": "u", "name_key": "u", "origin": "upload",
                                             "take": 0, "png": b"x", "created_at": "t", "hidden_at": None}
    assert run(ic.draws_left()) == 3
    # The env var sets the cap.
    monkeypatch.setenv("ICON_DRAWS_PER_DAY", "22")
    assert run(ic.draws_left()) == 0
    monkeypatch.delenv("ICON_DRAWS_PER_DAY")
    assert run(ic.draw("falcon"))["created"] and len(calls) == 1


def test_a_rejected_drawing_counts_against_the_day(monkeypatch, guard):
    monkeypatch.setenv("ICON_DRAWS_PER_DAY", "2")
    calls = _fake_image(monkeypatch, [drawing("black"), drawing("black"), drawing("disc")])
    with pytest.raises(ic.DrawError, match="simpler word"):
        run(ic.draw("camel"))
    assert len(calls) == 2 and run(ic.draws_left()) == 0     # two paid, nothing kept
    with pytest.raises(ic.DrawError) as e:
        run(ic.draw("falcon"))
    assert e.value.status == 429 and len(calls) == 2


def test_the_second_try_stops_at_the_cap(monkeypatch, guard):
    monkeypatch.setenv("ICON_DRAWS_PER_DAY", "1")
    calls = _fake_image(monkeypatch, [drawing("black"), drawing("disc")])
    with pytest.raises(ic.DrawError, match="simpler word"):
        run(ic.draw("camel"))
    assert len(calls) == 1                                   # no second, paid try past the cap


def test_draw_retries_one_bad_drawing_then_gives_up(monkeypatch, guard):
    calls = _fake_image(monkeypatch, [drawing("black"), drawing("disc")])
    got = run(ic.draw("falcon"))
    assert got["created"] and len(calls) == 2 and got["report"]["shapes"] == 1
    assert got["cost_usd"] == pytest.approx(2 * ic.draw_cost_usd())
    calls2 = _fake_image(monkeypatch, [drawing("black"), drawing("frame")])
    with pytest.raises(ic.DrawError, match="edge to edge") as e:
        run(ic.draw("camel"))
    assert e.value.status == 422
    assert len(calls2) == 2 and ic.custom_id("camel") not in guard["store"].rows


def test_draw_when_the_model_or_the_table_is_down(monkeypatch, guard):
    _fake_image(monkeypatch, [RuntimeError("quota")])
    with pytest.raises(ic.DrawError, match="try again") as e:
        run(ic.draw("falcon"))
    assert e.value.status == 503 and not guard["store"].rows
    # The table down: nothing is drawn where it could not be kept.
    ic._DRAW_FAILED.clear()
    calls = _fake_image(monkeypatch, [drawing("disc")])
    guard["store"].down = True
    with pytest.raises(ic.DrawError, match="can't be reached") as e:
        run(ic.draw("camel"))
    assert e.value.status == 503 and not calls


def test_a_drawing_that_cant_be_kept_is_a_503_and_counts(monkeypatch, guard):
    """Drawn (and billed), then the table fails to keep it: a plain 503 the
    card can offer again, the drawing counted against the day, and no new
    paid call for that name at once."""
    monkeypatch.setenv("ICON_DRAWS_PER_DAY", "3")
    calls = _fake_image(monkeypatch, [drawing("disc")])

    async def broken(row):
        raise ConnectionError("the database went away")

    monkeypatch.setattr(guard["store"], "insert", broken)
    with pytest.raises(ic.DrawError, match="can't be reached") as e:
        run(ic.draw("falcon"))
    assert e.value.status == 503 and len(calls) == 1 and not guard["store"].rows
    assert run(ic.draws_left()) == 2
    with pytest.raises(ic.DrawError, match="can't be reached"):
        run(ic.draw("falcon"))
    assert len(calls) == 1


def test_a_failed_drawing_is_not_billed_again_at_once(monkeypatch, guard):
    """Two drawings that were not clean icons: the same name again (any
    case) gets the same answer with no call, until the backoff is over; a
    model that was down is asked again after a minute."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(ic, "_clock", lambda: clock["now"])
    calls = _fake_image(monkeypatch, [drawing("black"), drawing("frame")])
    with pytest.raises(ic.DrawError, match="edge to edge") as first:
        run(ic.draw("camel"))
    assert "simpler word" in str(first.value) and len(calls) == 2
    with pytest.raises(ic.DrawError, match="edge to edge"):
        run(ic.draw("  Camel ", again=True))
    assert len(calls) == 2, "no new call inside the backoff"
    clock["now"] += ic.DRAW_BACKOFF_SEC + 1
    calls2 = _fake_image(monkeypatch, [drawing("disc")])
    assert run(ic.draw("camel"))["created"] and len(calls2) == 1
    # The model down: a minute.
    down = _fake_image(monkeypatch, [RuntimeError("quota")])
    with pytest.raises(ic.DrawError, match="try again"):
        run(ic.draw("falcon"))
    with pytest.raises(ic.DrawError, match="try again") as e:
        run(ic.draw("falcon"))
    assert len(down) == 1 and e.value.status == 503
    clock["now"] += ic.DRAW_DOWN_SEC + 1
    up = _fake_image(monkeypatch, [drawing("disc")])
    assert run(ic.draw("falcon"))["created"] and len(up) == 1


def test_concurrent_presses_of_one_name_share_one_call(monkeypatch):
    calls = []

    async def slow(prompt, **kw):
        calls.append(prompt)
        await asyncio.sleep(0.05)
        return drawing("disc")

    monkeypatch.setattr(vertex_client, "gemini_image", slow)

    async def both():
        return await asyncio.gather(ic.draw("Falcon"), ic.draw("falcon ", again=True))

    a, b = run(both())
    assert a["id"] == b["id"] and len(calls) == 1


def test_glyph_hide_and_the_team_listing(monkeypatch, guard):
    kind, d = run(ic.glyph("code"))
    assert (kind, d) == ("svg", ic.library_d("code"))
    with pytest.raises(KeyError):
        run(ic.glyph("no-such-icon"))
    with pytest.raises(KeyError):
        run(ic.glyph("c-000000000000"))
    _fake_image(monkeypatch, [drawing("disc")])
    cid = run(ic.draw("falcon"))["id"]
    kind, png = run(ic.glyph(cid))
    assert kind == "png" and png == guard["store"].rows[cid]["png"]
    team = run(ic.team_icons())
    assert [r["id"] for r in team] == [cid]
    assert set(team[0]) == {"id", "name", "label", "tags", "origin", "created_at", "created_by", "hidden"}
    assert [r["id"] for r in run(ic.team_index())] == [cid]
    assert run(ic.hide(cid)) and not run(ic.hide(cid)) and not run(ic.hide("code"))
    assert run(ic.team_icons()) == [] and run(ic.team_index()) == []    # hidden for everyone, at once here
    assert run(ic.team_icons(include_hidden=True))[0]["hidden"] is True
    # Hidden glyphs still draw (stored versions use them)...
    assert run(ic.glyph(cid))[0] == "png"
    # ...and are read from the local copy when the database is not there.
    guard["store"].rows.clear()
    assert run(ic.glyph(cid)) == ("png", png)


def test_glyphs_for_fetches_only_custom_glyphs(monkeypatch):
    _fake_image(monkeypatch, [drawing("disc")])
    cid = run(ic.draw("falcon"))["id"]
    items = [{"glyph": {"source": "library", "id": "code"}},
             {"glyph": {"source": "custom", "id": cid}},
             {"glyph": {"source": "custom", "id": cid}}]
    got = run(ic.glyphs_for(items))
    assert list(got) == [cid] and got[cid][0] == "png"
    # What a Stamper needs to draw them.
    stamper = ir.Stamper([dict(it, from_sec=0, settle_sec=0.27, to_sec=1, fill="#303030", ink="#ffffff",
                               box={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}, style="badge",
                               fx={"entrance": "none", "seed": "s"}) for it in items[:2]],
                         frame_size=(200, 200), glyphs=got)
    assert stamper.active(0.5) == [0, 1]


def test_a_hidden_drawing_still_stamps_in_the_circle(monkeypatch, guard):
    _fake_image(monkeypatch, [drawing("disc")])
    cid = run(ic.draw("falcon"))["id"]
    run(ic.hide(cid))
    glyphs = run(ic.glyphs_for([{"glyph": {"source": "custom", "id": cid}}]))
    item = {"glyph": {"source": "custom", "id": cid}, "from_sec": 0, "settle_sec": 0.27, "to_sec": 1,
            "fill": "#7f2bf2", "ink": "#ffffff", "box": {"x": 0.2, "y": 0.2, "w": 0.3, "h": 0.3},
            "style": "circle", "weight": "bold", "look_v": 1, "fx": {"entrance": "none", "seed": "s"}}
    st = ir.Stamper([item], frame_size=(200, 200), glyphs=glyphs)
    frame = np.zeros((200, 200, 3), np.uint8)
    assert not np.array_equal(st.under(frame, 0.5), frame)


def test_the_previews_are_the_run_s_own_sprites(monkeypatch, guard):
    from PIL import Image

    from app.services.rebrand_harness.elements import icon as icon_el

    _fake_image(monkeypatch, [drawing("disc")])
    cid = run(ic.draw("falcon"))["id"]
    g = run(ic.glyph(cid))
    got = ic.look_previews(g, "Falcon")

    def pixels(url):
        assert url.startswith("data:image/png;base64,")
        return np.asarray(Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGBA"))

    circle = ir.make_sprite("circle", g, ic.PREVIEW_PX, "#7f2bf2", "#ffffff", look_v=1).rgba
    assert np.array_equal(pixels(got["circle"]), circle)
    chip = ir.make_sprite("chip", g, icon_el.chip_height(ic.PREVIEW_PX), "#7f2bf2", "#ffffff",
                          text="Falcon", font=lf.default_key(), look_v=1).rgba
    assert np.array_equal(pixels(got["chip"]), chip)


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import rebrand_harness as rt

    app = FastAPI()
    app.include_router(rt.router)
    return TestClient(app)


def test_typing_makes_no_model_call(client, monkeypatch, guard):
    """GET /icons/search is free, however weak the name: no AI re-rank, no
    drawing — the guard makes every paid call raise and checks none was
    made; the fake counts the tries. The Names box's POST /icons/match is
    gone, with its automatic drawing."""
    images = _fake_image(monkeypatch, [drawing("disc")])
    url = "/api/rebrand/harness/icons/search"
    for name in ("developer", "halal food", "cyber cafe", "zzqx wobble", "instagram", "hackathon"):
        got = client.get(url, params={"q": name})
        assert got.status_code == 200, name
        assert set(got.json()) == {"query", "total", "items", "strong", "best", "via", "generate"}
    assert images == [] and guard["store"].rows == {}
    body = client.get(url, params={"q": "developer"}).json()
    assert body["items"][0]["id"] == "code" and body["items"][0]["png_url"] is None and body["strong"]
    assert client.post("/api/rebrand/harness/icons/match", json={"name": "falcon"}).status_code in (404, 405)
    assert not hasattr(ic, "match_or_draw") and not hasattr(ic, "_rerank") and not hasattr(ic, "match")


def test_the_generate_route(client, monkeypatch, guard):
    calls = _fake_image(monkeypatch, [drawing("disc"), drawing("two")])
    url = "/api/rebrand/harness/icons/generate"
    got = client.post(url, json={"name": "Hackathon"})
    assert got.status_code == 200
    body = got.json()
    cid = ic.custom_id("hackathon")
    assert body["id"] == cid and body["created"] and body["take"] == 0 and len(calls) == 1
    assert body["png_url"] == f"/api/rebrand/harness/icons/glyph/{cid}" and body["left_today"] == 24
    assert body["preview"]["circle"].startswith("data:image/png;base64,")
    assert body["preview"]["chip"].startswith("data:image/png;base64,")
    assert body["cost_usd"] == ic.draw_cost_usd()
    # In the team's icons and found by its name, on this machine at once.
    team = client.get("/api/rebrand/harness/icons/team").json()
    assert team["total"] == 1 and team["items"][0]["id"] == cid and team["items"][0]["source"] == "custom"
    assert team["items"][0]["png_url"] == body["png_url"]
    found = client.get("/api/rebrand/harness/icons/search", params={"q": "hackathon"}).json()
    assert found["items"][0]["id"] == cid and found["items"][0]["png_url"] == body["png_url"]
    assert client.get(body["png_url"]).content[:4] == b"\x89PNG"
    # Pressed again: free. Try again: a new take, the first steps aside.
    assert client.post(url, json={"name": "hackathon"}).json()["cost_usd"] == 0 and len(calls) == 1
    retry = client.post(url, json={"name": "hackathon", "again": True}).json()
    assert retry["take"] == 1 and retry["id"] != cid and len(calls) == 2
    assert [i["id"] for i in client.get("/api/rebrand/harness/icons/team").json()["items"]] == [retry["id"]]
    # Discard: hidden for everyone, and still drawn where it is used.
    assert client.delete(f"/api/rebrand/harness/icons/custom/{retry['id']}").json() == {"id": retry["id"],
                                                                                          "hidden": True}
    assert client.get("/api/rebrand/harness/icons/team").json()["total"] == 0
    assert client.get(f"/api/rebrand/harness/icons/glyph/{retry['id']}").status_code == 200


def test_the_generate_route_refuses_before_any_call(client, monkeypatch, guard):
    calls = _fake_image(monkeypatch, [drawing("disc")])
    url = "/api/rebrand/harness/icons/generate"
    brand = client.post(url, json={"name": "Instagram"})
    assert brand.status_code == 422 and "brand" in brand.json()["detail"]
    assert client.post(url, json={"name": ""}).status_code == 422
    assert client.post(url, json={"name": "x" * (ic.NAME_MAX + 1)}).status_code == 422
    monkeypatch.setenv("ICON_DRAWS_PER_DAY", "0")
    capped = client.post(url, json={"name": "falcon"})
    assert capped.status_code == 429 and "AI drawings" in capped.json()["detail"]
    assert not calls
    monkeypatch.delenv("ICON_DRAWS_PER_DAY")
    _fake_image(monkeypatch, [RuntimeError("quota")])
    down = client.post(url, json={"name": "falcon"})
    assert down.status_code == 503 and down.headers["retry-after"] == str(ic.DRAW_DOWN_SEC)


def test_the_catalogue_says_what_a_drawing_costs(client):
    cat = client.get("/api/rebrand/harness/icons/catalogue").json()
    assert "names" not in cat
    assert cat["generate"] == {"cost_usd": ic.draw_cost_usd(), "per_day": 25}


def test_no_hidden_paid_path_in_the_icon_library():
    """The only image call in rebrand_harness/ is the press's drawing
    (_draw_new), and the only Gemini call the director's once-per-scene
    _ask — typing, searching and the director's resolver can reach none."""
    root = Path(ic.__file__).resolve().parent
    found = []
    for p in sorted(root.rglob("*.py")):
        src = p.read_text(encoding="utf-8")
        for m in re.finditer(r"\b(gemini_image|gemini_multimodal|gemini_text|gemini_image_multipart)\(", src):
            fn = re.findall(r"^\s*(?:async\s+)?def\s+(\w+)", src[:m.start()], re.M)
            found.append((p.name, m.group(1), fn[-1] if fn else None))
    assert sorted(found) == [("director.py", "gemini_multimodal", "_ask"),
                             ("icon_catalog.py", "gemini_image", "_draw_new")], found


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

def test_the_table_is_additive_only():
    ddl = " ".join([ic._DDL, *ic._DDL_COLUMNS, ic._DDL_INDEX, ic._DDL_INDEX_ORIGIN]).upper()
    assert "CREATE TABLE IF NOT EXISTS YAS_REBRAND_ICONS" in ddl and "CREATE INDEX IF NOT EXISTS" in ddl
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE|RENAME|TYPE)\b", ddl)
    # The only ALTERs add a column, only if it is not there.
    assert all(s.startswith("ALTER TABLE yas_rebrand_icons ADD COLUMN IF NOT EXISTS ") for s in ic._DDL_COLUMNS)
    flat = re.sub(r"\s+", " ", ddl)
    for col in ("ID TEXT PRIMARY KEY", "NAME TEXT NOT NULL", "NAME_KEY TEXT NOT NULL", "LABEL TEXT",
                "TAGS TEXT[]", "ORIGIN TEXT", "TAKE INTEGER", "PNG BYTEA NOT NULL", "PROMPT TEXT",
                "SOURCE_KIND TEXT", "SOURCE_SHA TEXT", "SOURCE BYTEA", "REPORT JSONB", "CREATED_BY TEXT",
                "CREATED_AT TIMESTAMPTZ", "UPDATED_AT TIMESTAMPTZ", "HIDDEN_AT TIMESTAMPTZ"):
        assert col in flat, col
    # Every column a later build adds is in the safety net too.
    added = {s.split("IF NOT EXISTS ", 1)[1].split()[0] for s in ic._DDL_COLUMNS}
    assert added == {"label", "tags", "origin", "take", "source_kind", "source_sha", "source", "report",
                     "created_by", "updated_at"}
    # Closed to the public API, apart from the table: row-level security on.
    assert ic._DDL_RLS == "ALTER TABLE yas_rebrand_icons ENABLE ROW LEVEL SECURITY"


def test_the_postgres_store_sql(monkeypatch):
    """PgStore against a recording fake connection (never the real database):
    the table is created lazily once, reads run in autocommit, inserts keep
    the first row on a clash, placeholders match their arguments, listings
    never read the bytes."""
    from contextlib import asynccontextmanager

    from app import db

    log: list[tuple] = []
    row = {"id": "c-aaaaaaaaaaaa", "name": "Falcon", "name_key": "falcon", "label": "Falcon", "tags": ["bird"],
           "origin": "ai", "take": 0, "png": memoryview(b"\x89PNG..."), "prompt": "p", "report": {"ink": 0.1},
           "source_kind": "ai", "created_by": "pc", "created_at": None, "updated_at": None, "hidden_at": None}

    class Cur:
        rowcount = 1

        def __init__(self, autocommit):
            self.autocommit = autocommit
            self._got = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, sql, args=()):
            assert sql.count("%s") == len(args), sql
            log.append((" ".join(sql.split()), tuple(args), self.autocommit))
            self._got = [{"n": 3}] if "count(*)" in sql else [dict(row)]

        async def fetchone(self):
            return self._got[0] if self._got else None

        async def fetchall(self):
            return list(self._got)

    class Conn:
        def __init__(self, autocommit):
            self.autocommit = autocommit

        def cursor(self):
            return Cur(self.autocommit)

    @asynccontextmanager
    async def fake_get_conn(*, autocommit=False):
        yield Conn(autocommit)

    monkeypatch.setattr(db, "get_conn", fake_get_conn)
    store = ic.PgStore()

    async def go():
        takes = await store.takes("falcon", "ai")
        one = await store.by_id("c-aaaaaaaaaaaa")
        new = await store.insert({"id": "c-aaaaaaaaaaaa", "name": "Falcon", "name_key": "falcon", "png": b"x",
                                  "prompt": "p", "report": {"ink": 0.1}, "tags": [], "take": 0})
        hid = await store.set_hidden("c-aaaaaaaaaaaa", True)
        listed = await store.listing(10, False, origin="ai")
        n = await store.count_since("ai", 24)
        return takes, one, new, hid, listed, n

    takes, one, new, hid, listed, n = run(go())
    assert takes[0]["png"] == b"\x89PNG..." and isinstance(takes[0]["png"], bytes) and one["id"] == takes[0]["id"]
    assert new["name"] == "Falcon" and hid is True and listed[0]["id"] == "c-aaaaaaaaaaaa" and n == 3
    sql = [s for s, _, _ in log]
    assert sum("CREATE TABLE IF NOT EXISTS yas_rebrand_icons" in s for s in sql) == 1   # once per store
    assert sum("ADD COLUMN IF NOT EXISTS" in s for s in sql) == len(ic._DDL_COLUMNS)
    assert [ac for s, _, ac in log if "ENABLE ROW LEVEL SECURITY" in s] == [True], "once, on its own"
    assert any("ON CONFLICT (id) DO NOTHING" in s and "%s::jsonb" in s for s in sql)
    assert all(ac for s, _, ac in log if s.startswith("SELECT"))                       # reads: autocommit
    assert not any(ac for s, _, ac in log if s.startswith(("INSERT", "UPDATE", "CREATE", "ALTER TABLE yas_rebrand_icons ADD")))
    assert any(s.startswith("UPDATE yas_rebrand_icons SET hidden_at = now()") for s in sql)
    listing = next(s for s in sql if "ORDER BY created_at DESC" in s)
    assert "hidden_at IS NULL" in listing and "origin = %s" in listing and "LIMIT %s" in listing
    assert "png" not in listing.split("FROM")[0]                                        # never the bytes
    assert any("WHERE origin = %s AND name_key = %s ORDER BY take" in s for s in sql)
    counted = next((s, a) for s, a, _ in log if "count(*)" in s)
    assert "origin = %s" in counted[0] and counted[1] == ("ai", 24 * 3600.0)
    inserted = next(a for s, a, _ in log if s.startswith("INSERT"))
    assert json.loads(inserted[10]) == {"ink": 0.1} and inserted[4] == []


# ---------------------------------------------------------------------------
# The bold weight and the chip label fonts (the circle and chip icons)
# ---------------------------------------------------------------------------

# The fill file keys the icon director's PAID answers (director_key holds
# catalogue_version()): it must never change, or every scene is asked again.
FILL_LF_SHA256 = "469c06057a15eedb25c3207811f4d64cf1613329e933de044d20467c61178b15"


def test_the_fill_file_and_its_version_never_move():
    import hashlib

    from app.services.rebrand_harness import director

    raw = ic.CATALOGUE_PATH.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == FILL_LF_SHA256
    assert ic.catalogue_version() == "phosphor-fill-469c06057a" == director.default_catalog().version
    # The bold file never enters it.
    assert ic.bold_version() != ic.catalogue_version()


def test_the_bold_weight_has_the_fill_ids_and_every_one_rasterises():
    data = json.loads(ic.BOLD_PATH.read_text(encoding="utf-8"))
    assert data["weight"] == "bold" and data["viewbox"] == 256 and data["ids_from"] == ic.catalogue_version()
    bold = set(data["icons"])
    fill = set(json.loads(ic.CATALOGUE_PATH.read_text(encoding="utf-8"))["icons"])
    assert bold | set(data["missing"]) == fill and not bold & set(data["missing"])
    assert len(bold) == data["count"] > 1400 and set(ic.PICK_IDS) <= bold
    bad = []
    try:
        for icon_id in sorted(bold):
            cov = float(ir.path_alpha(data["icons"][icon_id]["d"], 64).mean())
            if not 0.01 <= cov <= 0.95:
                bad.append((icon_id, round(cov, 3)))
    finally:
        ir._path_alpha_cached.cache_clear()
    assert not bad, bad


def test_bold_code_is_the_thick_brackets():
    import cv2

    bold, fill = ic.library_d("code", "bold"), ic.library_d("code")
    assert bold and bold != fill and ic.library_d("code", "fill") == fill
    a = ir.path_alpha(bold, 128)
    n, _ = cv2.connectedComponents((a > 0.5).astype(np.uint8), connectivity=8)
    assert n - 1 == 3                                   # "<", "/" and ">", no rounded square
    assert 0.1 < float(a.mean()) < float(ir.path_alpha(fill, 128).mean())
    with pytest.raises(ValueError):
        ic.library_d("code", "heavy")
    assert ic.library_d("no-such-icon", "bold") is None


def test_the_bold_version_does_not_depend_on_line_endings(tmp_path, monkeypatch):
    raw = ic.BOLD_PATH.read_bytes().replace(b"\r\n", b"\n")
    versions = []
    for name, body in (("lf.json", raw), ("crlf.json", raw.replace(b"\n", b"\r\n"))):
        (tmp_path / name).write_bytes(body)
        monkeypatch.setattr(ic, "BOLD_PATH", tmp_path / name)
        ic._bold.cache_clear()
        try:
            versions.append(ic.bold_version())
        finally:
            ic._bold.cache_clear()
    monkeypatch.undo()
    assert versions[0] == versions[1] == ic.bold_version() and versions[0].startswith("phosphor-bold-")


def test_a_missing_bold_shape_is_drawn_in_fill(tmp_path, monkeypatch):
    # One the bake left out (a dot too small to read), and a checkout with
    # no bold file at all: the fill shape, never nothing.
    missing = json.loads(ic.BOLD_PATH.read_text(encoding="utf-8"))["missing"]
    assert missing and all(ic.library_d(i, "bold") == ic.library_d(i) for i in missing)
    monkeypatch.setattr(ic, "BOLD_PATH", tmp_path / "none.json")
    ic._bold.cache_clear()
    try:
        assert ic.library_d("code", "bold") == ic.library_d("code")
        assert ic.bold_version() == "phosphor-bold-none"
    finally:
        monkeypatch.undo()
        ic._bold.cache_clear()
    assert ic.library_d("code", "bold") != ic.library_d("code")


def test_picks_and_search_carry_the_bold_shape():
    item = ic.picks_public()[1]["items"][0]
    assert item["id"] == "code" and item["b"] == ic.library_d("code", "bold")
    got = ic.find("coffee")["items"][0]
    assert got["id"] == "coffee" and got["b"] == ic.library_d("coffee", "bold")


def test_glyph_takes_a_weight(monkeypatch, guard):
    assert run(ic.glyph("code", "bold")) == ("svg", ic.library_d("code", "bold"))
    assert run(ic.glyph("code")) == ("svg", ic.library_d("code"))


def test_the_glyph_and_font_routes(client):
    svg = client.get("/api/rebrand/harness/icons/glyph/code", params={"weight": "bold"})
    assert svg.status_code == 200 and ic.library_d("code", "bold")[:40] in svg.text
    assert ic.library_d("code")[:40] in client.get("/api/rebrand/harness/icons/glyph/code").text
    assert client.get("/api/rebrand/harness/icons/glyph/code", params={"weight": "heavy"}).status_code == 422
    cat = client.get("/api/rebrand/harness/icons/catalogue").json()
    fonts = cat["fonts"]
    assert fonts["default"] == lf.default_key() and set(fonts["labels"]) == set(lf.label_keys())
    for url in fonts["labels"][fonts["default"]].values():
        got = client.get(url)
        key = url.rsplit("/", 1)[1][:-len(".ttf")]
        assert got.status_code == 200 and got.headers["content-type"] == "font/ttf"
        assert got.content == lf.face_file(key).read_bytes() and "immutable" in got.headers["cache-control"]
    assert client.get("/api/rebrand/harness/icons/font/jost-600@00000000.ttf").status_code == 404
    assert client.get(f"/api/rebrand/harness/icons/font/{lf.default_key()}.ttf").status_code == 404


def test_the_label_fonts_match_their_keys():
    import hashlib

    m = lf.manifest()
    assert m["default"] in m["labels"]
    for key, face in m["faces"].items():
        data = (lf.FONTS_DIR / face["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == face["sha256"]
        slug = {"Jost": "jost", "Noto Kufi Arabic": "kufi"}[face["family"]]
        assert key == f"{slug}-{face['weight']}@{face['sha256'][:8]}" and face["weight"] == 600
        licence = (lf.FONTS_DIR / face["licence"]).read_text(encoding="utf-8")
        assert "SIL Open Font License" in licence
    for key, lab in m["labels"].items():
        assert key == "label-600@" + hashlib.sha256(f"{lab['latin']}|{lab['arabic']}".encode()).hexdigest()[:8]
        assert m["faces"][lab["latin"]]["scripts"] == ["Latn"] and m["faces"][lab["arabic"]]["scripts"] == ["Arab"]


def test_harfbuzz_is_pinned():
    import uharfbuzz

    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    assert "uharfbuzz==0.56.*" in req and uharfbuzz.__version__.startswith("0.56.")
