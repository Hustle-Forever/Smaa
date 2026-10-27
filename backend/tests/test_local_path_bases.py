"""Stored local paths use two bases; every reader must find a file under either.

Local-mode uploads (asset_storage.upload) store paths relative to backend/,
while Poster+Human renders and the Talking Head / Video Editor services store
them relative to backend/data. Each reader resolves against one base, so a file
written by the other convention was unreadable: Poster+Human remove-logo and
variants could not load their image, and the Video Editor could not source a
Poster+Human animation.

Every test creates its file BEFORE resolving: `reader(rel) == _touch(path)`
evaluates the reader first, so the file would not exist yet and the test would
assert the wrong thing.
"""
from pathlib import Path

import pytest

from app.config import config
from app.services import asset_storage, yas_talking_head, yas_talking_head_v2, yas_video_editor
from app.services.storage import first_existing
from app.workers import yas_th_composition_poller, yas_th_v2_composition_poller

DATA_BASE_READERS = [
    yas_video_editor._abs_from_rel,
    yas_talking_head._abs_from_rel,
    yas_talking_head_v2._abs_from_rel,
    yas_th_composition_poller._abs_from_rel,
    yas_th_v2_composition_poller._abs_from_rel,
]


@pytest.fixture
def roots(tmp_path):
    """Point both bases at a temp tree: backend/ and backend/data."""
    backend = tmp_path / "backend"
    data = backend / "data"
    old = (config.asset_root, config.producer_assets_root)
    object.__setattr__(config, "asset_root", data / "assets")
    object.__setattr__(config, "producer_assets_root", data / "assets_library")
    yield backend, data
    object.__setattr__(config, "asset_root", old[0])
    object.__setattr__(config, "producer_assets_root", old[1])


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def test_first_existing_prefers_primary_then_fallback(tmp_path):
    primary, fallback = tmp_path / "a", tmp_path / "b"
    assert first_existing(primary, fallback) == primary  # neither exists
    _touch(fallback)
    assert first_existing(primary, fallback) == fallback  # only fallback
    _touch(primary)
    assert first_existing(primary, fallback) == primary  # both: primary wins


@pytest.mark.parametrize("reader", DATA_BASE_READERS)
def test_data_base_readers_find_a_backend_relative_file(roots, reader):
    backend, _data = roots
    rel = "data/assets_library/yas/animations/332.mp4"
    stored = _touch(backend / rel)
    assert reader(rel) == stored


@pytest.mark.parametrize("reader", DATA_BASE_READERS)
def test_data_base_readers_keep_their_own_base(roots, reader):
    backend, data = roots
    rel = "yas_producer/talking-head/1/main.png"
    own = _touch(data / rel)
    _touch(backend / rel)  # the other base has the same path too
    assert reader(rel) == own


@pytest.mark.parametrize("reader", DATA_BASE_READERS)
def test_a_path_that_exists_nowhere_keeps_the_reader_base(roots, reader):
    _backend, data = roots
    rel = "yas_producer/new/out.mp4"
    assert reader(rel) == data / rel


def test_local_to_abs_finds_a_data_relative_poster_human_image(roots):
    _backend, data = roots
    rel = "yas_producer/generations/7.png"
    stored = _touch(data / rel)
    assert asset_storage.local_to_abs(rel) == stored


def test_local_to_abs_keeps_the_backend_base_and_absolute_paths(roots):
    backend, data = roots
    rel = "data/assets_library/poster/1.png"
    own = _touch(backend / rel)
    _touch(data / rel)  # the other base has the same path too
    assert asset_storage.local_to_abs(rel) == own
    assert asset_storage.local_to_abs("nowhere/x.png") == backend / "nowhere/x.png"
    assert asset_storage.local_to_abs(str(own)) == own
