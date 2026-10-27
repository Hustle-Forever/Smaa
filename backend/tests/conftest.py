"""Make ``import app...`` resolve no matter where pytest is invoked from.

pytest puts each test file's DIRECTORY (backend/tests/) on sys.path — not
backend/ — so the two tests without their own bootstrap
(test_general_video.py, test_th_video_role_supabase_fallback.py) would fail
with ModuleNotFoundError unless cwd happens to be backend/. This conftest is
imported by pytest before any test module and fixes that for every
invocation style. The other tests' own sys.path bootstraps stay harmless.

It also pins the storage config for every test. app.config loads the repo-root
.env into the test process, so without the pin a developer's S3_ENDPOINT or
Supabase key would silently move every test onto a real object store. Tests
opt into S3 mode explicitly with the `s3_config` or `s3_storage` fixtures.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parents[1]  # tests/ -> backend/
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

# The fake S3 endpoint the fixtures below point at. moto only intercepts
# endpoints it knows, and it reads this list once, when moto.s3 is first
# imported, so it must be in the environment before any test imports moto.
S3_TEST_ENDPOINT = "http://minio.test:9000"
S3_TEST_PUBLIC_BASE = "https://studio.test/media"
S3_TEST_ACCESS_KEY = "test-access"
S3_TEST_SECRET_KEY = "test-secret-key"
_moto_endpoints = [e for e in os.environ.get("MOTO_S3_CUSTOM_ENDPOINTS", "").split(",") if e]
if S3_TEST_ENDPOINT not in _moto_endpoints:
    os.environ["MOTO_S3_CUSTOM_ENDPOINTS"] = ",".join(_moto_endpoints + [S3_TEST_ENDPOINT])

# Default for every test: backend "auto" with no S3 and no Supabase settings,
# which resolves to local disk. A test sets the fields it needs on top.
_STORAGE_PINS = {
    "storage_backend": "auto",
    "s3_endpoint": "",
    "s3_public_base_url": "",
    "s3_access_key": "",
    "s3_secret_key": "",
    "s3_region": "us-east-1",
    "supabase_url": "",
    "supabase_service_role_key": "",
}


def _set_config(values: dict) -> None:
    from app.config import config

    # `config` is a frozen dataclass; tests bypass the freeze on purpose.
    for name, value in values.items():
        object.__setattr__(config, name, value)


@pytest.fixture(autouse=True)
def _pin_storage_config():
    """Pin the storage settings and clear asset_storage's caches around every
    test, restoring the previous values afterwards."""
    from app.config import config
    from app.services import asset_storage

    saved = {name: getattr(config, name) for name in _STORAGE_PINS}
    _set_config(_STORAGE_PINS)
    asset_storage.clear_caches()
    try:
        yield
    finally:
        _set_config(saved)
        asset_storage.clear_caches()


@pytest.fixture
def s3_config():
    """S3 mode with NO store behind it: the config points at the fake endpoint
    and every cache is clear. Presigning works (it is offline); monkeypatch the
    asset_storage / s3_backend functions for any other I/O. An unpatched network
    call fails with AssetStorageError; it never reaches a real service.

    Yields a namespace with `endpoint` (internal origin), `public_base` (the
    /media origin) and `config`.
    """
    from app.config import config
    from app.services import asset_storage

    _set_config({
        "storage_backend": "s3",
        "s3_endpoint": S3_TEST_ENDPOINT,
        "s3_public_base_url": S3_TEST_PUBLIC_BASE,
        "s3_access_key": S3_TEST_ACCESS_KEY,
        "s3_secret_key": S3_TEST_SECRET_KEY,
        "s3_region": "us-east-1",
    })
    asset_storage.clear_caches()
    # The autouse fixture restores the config and clears the caches after.
    yield SimpleNamespace(
        endpoint=S3_TEST_ENDPOINT, public_base=S3_TEST_PUBLIC_BASE, config=config,
    )


@pytest.fixture
def s3_storage(s3_config):
    """S3 mode against moto's in-process S3, with the app's four buckets
    created (private, no policy yet). Skips when moto is not installed.

    Yields the `s3_config` namespace plus `client` (a plain boto3 client on the
    same fake store, for arranging and inspecting objects) and `buckets`
    (assets, media, brand docs, approved)."""
    moto = pytest.importorskip("moto")
    import boto3
    from botocore.config import Config as BotoConfig

    cfg = s3_config.config
    with moto.mock_aws():
        client = boto3.session.Session().client(
            "s3",
            endpoint_url=S3_TEST_ENDPOINT,
            aws_access_key_id=S3_TEST_ACCESS_KEY,
            aws_secret_access_key=S3_TEST_SECRET_KEY,
            region_name="us-east-1",
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        buckets = [
            cfg.producer_assets_bucket, cfg.producer_media_bucket,
            cfg.brand_docs_bucket, cfg.approved_media_bucket,
        ]
        for bucket in buckets:
            client.create_bucket(Bucket=bucket)
        s3_config.client = client
        s3_config.buckets = buckets
        yield s3_config



def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "omni_default: run with REBRAND_OMNI as a server has it by default (unset: Omni switched off)",
    )


@pytest.fixture(autouse=True)
def _omni_on_for_the_omni_tests(request, monkeypatch):
    """Omni is switched off for new work (rebrand_harness.recipe.omni_on:
    REBRAND_OMNI, off unless set to on). The tests written for the Omni
    engine — every stored recipe's default engine: the golden lock, the Omni
    studio, runner and build-on tests — run with it switched back on, as a
    tests/legacy folder of their own would (moving them waits for the
    owner). A test marked omni_default runs with the switch as a server has
    it: unset, so off (tests/test_rebrand_omni_off.py)."""
    if request.node.get_closest_marker("omni_default") is None:
        monkeypatch.setenv("REBRAND_OMNI", "on")
    else:
        monkeypatch.delenv("REBRAND_OMNI", raising=False)


class _NoTeamIcons:
    """yas_rebrand_icons as a test sees it by default: empty, and never the
    shared database (a test that needs icons sets its own store)."""

    async def by_id(self, icon_id):
        return None

    async def takes(self, name_key, origin):
        return []

    async def insert(self, row):
        return dict(row)

    async def set_hidden(self, icon_id, hidden):
        return False

    async def update(self, icon_id, fields):
        return None

    async def listing(self, limit, include_hidden, origin=None):
        return []

    async def count_since(self, origin, hours):
        return 0


@pytest.fixture(autouse=True)
def _no_shared_database_for_icons():
    """No test writes to (or creates a table in) the shared database through
    the rebrand icon stores: when a test has loaded them, the team's icons
    are an empty in-memory store and the director feedback log is kept in
    memory (tests that need either set their own on top of these)."""
    ic = sys.modules.get("app.services.rebrand_harness.icon_catalog")
    fb = sys.modules.get("app.services.rebrand_harness.icon_feedback")
    old_ic = ic.set_store(_NoTeamIcons()) if ic is not None else None
    old_fb = fb.set_store(fb.MemoryStore()) if fb is not None else None
    yield
    if fb is not None:
        fb.set_store(old_fb)
    if ic is not None:
        ic.set_store(old_ic)


@pytest.fixture(autouse=True)
def _no_shared_database_for_the_reel():
    """No test writes a TH v2 run row through the reel (services/th_reel:
    Finalize's compare-and-set, the poller's patches of a reel it renders,
    a scene's pick and end — set on every Generate): it reads and writes an
    empty in-memory store (a test that needs rows sets its own on top).
    Imported here rather than only swapped when loaded: the service and the
    router load it lazily, after this fixture has run."""
    from app.services import th_reel

    old = th_reel.set_store(th_reel.MemoryStore())
    yield
    th_reel.set_store(old)


@pytest.fixture(autouse=True)
def _no_shared_database_for_the_reel_music():
    """The same for the reel's background music (services/th_reel_music):
    its store is an empty in-memory one unless a test sets its own."""
    from app.services import th_reel_music

    old = th_reel_music.set_store(th_reel_music.MemoryStore())
    yield
    th_reel_music.set_store(old)


@pytest.fixture(autouse=True)
def _no_shared_database_for_the_rebrand_memory(monkeypatch):
    """No test writes (or creates) yas_rebrand_choices, nor reads the shared
    database through the rebrand memory: a ready reel's snapshot (the
    poller's ready branch starts one) and every retrieval go to an empty
    in-memory store — and its one paid call, the embed, is switched off (a
    test that wants it sets REBRAND_MEMORY_EMBED on over a fake embed)."""
    from app.services.rebrand_harness import rebrand_memory

    monkeypatch.setenv("REBRAND_MEMORY_EMBED", "off")
    old = rebrand_memory.set_store(rebrand_memory.MemoryStore())
    yield
    rebrand_memory.set_store(old)
