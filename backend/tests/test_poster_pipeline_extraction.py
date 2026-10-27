"""Guards on the poster-pipeline extraction.

The two-stage poster pipeline moved out of `routers/yas_producer.py` into
`services/poster_pipeline.py` so non-request callers (the campaign worker) can
drive it — a worker cannot import a router without creating an import cycle
through `main.py`.

These tests exist because the extraction is only safe while the router keeps
delegating to the SAME objects. If someone re-inlines a copy in the router, or
redefines one of the shared constants there, the two would drift silently: the
router would keep working and the worker would quietly use different prompts
or a different status vocabulary.
"""
import pytest
from fastapi import HTTPException

from app.routers import yas_producer as router
from app.services import poster_pipeline as pipeline


DELEGATED = [
    ("_single_stage2", "single_stage2"),
    ("_run_stage2_batch", "run_stage2_batch"),
    ("_poster_concepts", "poster_concepts"),
    ("_poster_recent_summaries", "poster_recent_summaries"),
    ("_poster_reserve", "poster_reserve"),
    ("_poster_refund", "poster_refund"),
    ("_poster_slots_remaining", "poster_slots_remaining"),
]


@pytest.mark.parametrize("router_name,service_name", DELEGATED)
def test_router_delegates_and_does_not_redefine(router_name, service_name):
    """The router name must BE the service object, not a copy of its body."""
    assert getattr(router, router_name) is getattr(pipeline, service_name), (
        f"{router_name} is no longer the same object as "
        f"poster_pipeline.{service_name} — someone re-inlined the pipeline "
        f"in the router. The campaign worker would then run different code."
    )


SHARED_CONSTANTS = [
    "STATUS_GENERATING", "STATUS_READY", "STATUS_FAILED", "STATUS_SAVED",
    "GEN_TIMEOUT_SEC", "GEN_TIMEOUT_GEMINI_PRO_SEC",
    "GEMINI_PRO_MODEL", "GEMINI_PRO_LOCATION",
    "ENGINE_OPENAI", "ENGINE_GEMINI_PRO",
    "POSTER_MODE", "POSTER_VARIANT_COUNT",
    "POSTER_MAX_REFS", "POSTER_CONCEPTS_MAX_TOKENS", "POSTER_LOGO_PATH",
]


@pytest.mark.parametrize("name", SHARED_CONSTANTS)
def test_shared_constants_have_one_definition(name):
    assert getattr(router, name) == getattr(pipeline, name), (
        f"{name} disagrees between the router and poster_pipeline — it must be "
        f"defined once (in the service) and imported, not re-declared."
    )


def _imported_modules(module):
    """Every module name this module imports, via AST (not substring matching —
    prose in a docstring must not trip the check)."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_pipeline_does_not_import_the_router():
    """The whole point of the extraction: no service -> router edge."""
    offenders = [m for m in _imported_modules(pipeline) if "router" in m]
    assert not offenders, (
        f"poster_pipeline imports {offenders} — that reintroduces the import "
        f"cycle the extraction removed (main.py imports both)."
    )


class TestNoCap:
    """There is deliberately NO local image cap.

    A rolling-hour ledger used to refuse the 21st render. It was removed, and
    these tests exist so nobody quietly puts a limit back without noticing that
    the router, the campaign worker and several API responses all assume the
    answer is always "plenty"."""

    def test_reserve_never_raises(self):
        for _ in range(50):
            assert pipeline.poster_reserve(4) == pipeline.UNCAPPED_SLOTS

    def test_slots_never_run_out(self):
        pipeline.poster_reserve(10_000)
        assert pipeline.poster_slots_remaining() == pipeline.UNCAPPED_SLOTS

    def test_refund_is_a_no_op(self):
        assert pipeline.poster_refund(4) is None
        assert pipeline.poster_slots_remaining() == pipeline.UNCAPPED_SLOTS

    def test_the_ledger_is_gone(self):
        assert not hasattr(pipeline, "_poster_call_log")
        assert not hasattr(pipeline, "POSTER_HOURLY_CAP")
