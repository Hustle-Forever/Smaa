"""Posters show each picture as it is ready, and a busy Google fails fast.

- `start_stage2_batch` answers with every variant's `generating` row at once,
  renders them in the background, and fills those same rows.
- A batch that crashes still ends every card (failed, never stuck), and its
  cleanup runs.
- `vertex_client.quota_patience` caps how many tries a 429 gets; without it the
  image calls keep their patient six-attempt policy.
- The poster routes answer `progressive` requests from the background batch.
"""
import asyncio
import io

import pytest
from PIL import Image

from app.routers import yas_producer as router
from app.services import poster_pipeline as pp
from app.services import vertex_client as vc
from app.services.poster_styles import get_poster_style


# --- quota patience ----------------------------------------------------------

@pytest.fixture
def no_waits(monkeypatch):
    zero = lambda _rs: 0  # noqa: E731
    monkeypatch.setattr(vc, "_quota_wait", zero)
    monkeypatch.setattr(vc, "_quick_quota_wait", zero)
    monkeypatch.setattr(vc, "_other_wait", zero)


def _counting(exc: Exception):
    calls = {"n": 0}

    @vc._image_retry()
    async def call():
        calls["n"] += 1
        raise exc

    return call, calls


def test_a_429_gets_two_tries_under_quota_patience(no_waits):
    call, calls = _counting(RuntimeError("429 RESOURCE_EXHAUSTED. Resource exhausted."))
    with vc.quota_patience(2):
        with pytest.raises(RuntimeError):
            asyncio.run(call())
    assert calls["n"] == 2


def test_without_patience_a_429_keeps_the_six_try_policy(no_waits):
    call, calls = _counting(RuntimeError("429 RESOURCE_EXHAUSTED. Resource exhausted."))
    with pytest.raises(RuntimeError):
        asyncio.run(call())
    assert calls["n"] == 6


def test_patience_does_not_shorten_other_errors(no_waits):
    call, calls = _counting(RuntimeError("503 UNAVAILABLE"))
    with vc.quota_patience(2):
        with pytest.raises(RuntimeError):
            asyncio.run(call())
    assert calls["n"] == 6


def test_patience_is_undone_after_the_block(no_waits):
    with vc.quota_patience(2):
        assert vc._quota_attempts.get() == 2
    assert vc._quota_attempts.get() is None


# --- background batch -------------------------------------------------------

def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def fake_store(monkeypatch):
    rows: dict[int, dict] = {}
    seq = {"n": 0}

    async def create(**kw):
        seq["n"] += 1
        row = {"id": seq["n"], **kw}
        rows[row["id"]] = row
        return dict(row)

    async def update(gen_id, **kw):
        rows[int(gen_id)].update(kw)
        return dict(rows[int(gen_id)])

    async def get(gen_id):
        r = rows.get(int(gen_id))
        return dict(r) if r else None

    monkeypatch.setattr(pp.ystore, "create", create)
    monkeypatch.setattr(pp.ystore, "update", update)
    monkeypatch.setattr(pp.ystore, "get", get)

    async def upload(*, data, path, mime, bucket):
        return {"storage_bucket": bucket, "storage_path": path, "local_path": None}

    monkeypatch.setattr(pp.asset_storage, "upload", upload)
    return rows


def _specs(n):
    return [{"prompt": f"scene {i}", "concept": f"c{i}", "variation_ref_path": None,
             "variation_of": None} for i in range(n)]


async def _drain():
    while pp._BACKGROUND_BATCHES:
        await asyncio.gather(*list(pp._BACKGROUND_BATCHES), return_exceptions=True)


def _kwargs():
    return dict(topic="campus", reference_asset_ids=[], reference_upload_ids=[],
                parent_generation_id=None, stage1_cost_usd=0.0,
                style=get_poster_style("voxel"), aspect_ratio="1:1")


def test_rows_come_back_generating_then_fill_in(fake_store, monkeypatch):
    gate = asyncio.Event()
    seen_patience = []

    async def fake_gemini(parts, **kw):
        seen_patience.append(vc._quota_attempts.get())
        await gate.wait()
        return _png()

    monkeypatch.setattr(pp, "gemini_image_multipart", fake_gemini)

    async def go():
        rows = await pp.start_stage2_batch(_specs(3), [], "style", quota_attempts=2, **_kwargs())
        assert [r["status"] for r in rows] == ["generating"] * 3
        assert len(fake_store) == 3  # made up front, not one by one
        gate.set()
        await _drain()
        return rows

    rows = asyncio.run(go())
    final = [fake_store[r["id"]] for r in rows]
    assert [r["status"] for r in final] == ["ready"] * 3
    assert len(fake_store) == 3  # the background filled THOSE rows, made none
    assert {r["variant_set_id"] for r in final} == {rows[0]["id"]}
    assert seen_patience == [2, 2, 2]


def test_a_refused_render_fails_its_card_and_the_rest_carry_on(fake_store, monkeypatch):
    calls = {"n": 0}

    async def fake_gemini(parts, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("429 RESOURCE_EXHAUSTED. Resource exhausted.")
        return _png()

    monkeypatch.setattr(pp, "gemini_image_multipart", fake_gemini)

    async def go():
        rows = await pp.start_stage2_batch(_specs(3), [], "style", quota_attempts=2, **_kwargs())
        await _drain()
        return rows

    rows = asyncio.run(go())
    assert [fake_store[r["id"]]["status"] for r in rows] == ["ready", "failed", "ready"]


def test_a_crashed_batch_ends_every_card_and_cleans_up(fake_store, monkeypatch):
    cleaned = []

    async def boom(*a, **kw):
        raise RuntimeError("batch exploded")

    monkeypatch.setattr(pp, "run_stage2_batch", boom)

    async def go():
        rows = await pp.start_stage2_batch(_specs(2), [], "style",
                                           cleanup=lambda: cleaned.append(True), **_kwargs())
        await _drain()
        return rows

    rows = asyncio.run(go())
    assert [fake_store[r["id"]]["status"] for r in rows] == ["failed", "failed"]
    assert all("Try again" in fake_store[r["id"]]["error"] for r in rows)
    assert cleaned == [True]


# --- routes -----------------------------------------------------------------

def test_generate_progressive_answers_from_the_background_batch(monkeypatch):
    seen = {}

    async def no_guard():
        return None

    async def concepts(topic, n, style):
        return [{"prompt": f"p{i}", "summary": f"s{i}"} for i in range(n)], 0.01

    async def start(specs, ref_paths, image_usage, **kw):
        seen.update(kw, n=len(specs))
        return [{"id": i, "status": "generating"} for i in range(len(specs))]

    async def run_inline(*a, **kw):
        raise AssertionError("a progressive request must not wait for the batch")

    monkeypatch.setattr(router, "_budget_guard", no_guard)
    monkeypatch.setattr(router, "_poster_concepts", concepts)
    monkeypatch.setattr(router, "_start_stage2_batch", start)
    monkeypatch.setattr(router, "_run_stage2_batch", run_inline)

    out = asyncio.run(router.poster_generate(router.PosterGenerateRequest(topic="campus", progressive=True)))
    assert out["progressive"] is True
    assert [r["status"] for r in out["items"]] == ["generating"] * router.POSTER_VARIANT_COUNT
    assert seen["quota_attempts"] == router.POSTER_QUOTA_ATTEMPTS
    assert seen["n"] == router.POSTER_VARIANT_COUNT


def test_generate_without_progressive_still_waits_and_fails_fast(monkeypatch):
    seen = {}

    async def no_guard():
        return None

    async def concepts(topic, n, style):
        return [{"prompt": f"p{i}", "summary": f"s{i}"} for i in range(n)], 0.0

    async def run_inline(specs, ref_paths, image_usage, **kw):
        seen.update(kw)
        return [{"id": 1, "status": "ready"}]

    monkeypatch.setattr(router, "_budget_guard", no_guard)
    monkeypatch.setattr(router, "_poster_concepts", concepts)
    monkeypatch.setattr(router, "_run_stage2_batch", run_inline)

    out = asyncio.run(router.poster_generate(router.PosterGenerateRequest(topic="campus")))
    assert "progressive" not in out
    assert seen["sequential"] is True
    assert seen["quota_attempts"] == router.POSTER_QUOTA_ATTEMPTS
