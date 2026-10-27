"""Tests for the agentic video-editor layer.

Two modules under test, neither of which may be touched from here:

  * ``app/services/model_gateway.py``      — provider resolution + the tool loop
  * ``app/services/yas_video_edit_agent.py`` — the 12-tool agent itself

Everything is mocked: no network, no database, no ffmpeg, no Remotion. The
gateway is exercised through a FAKE Claude client so the *real* loop code runs
(step cap, tool-error recovery, image delivery, step recording) rather than a
stand-in for it.

There is no pytest-asyncio in this venv, so coroutines are driven with
``asyncio.run`` via the ``_run`` helper, matching
tests/test_th_video_role_supabase_fallback.py.

Runs standalone or under pytest.
"""
from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services import model_gateway as g  # noqa: E402
from app.services import yas_video_edit_agent as agent  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Fixtures / stubs
# --------------------------------------------------------------------------- #
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _cfg(**over) -> SimpleNamespace:
    """A stand-in for the frozen `config` dataclass — only the fields the
    gateway actually reads, so a test can flip one without touching the app."""
    base = dict(
        google_cloud_project="proj-1",
        claude_vertex_region="us-east5",
        claude_model="claude-vertex-model",
        anthropic_api_key="",
        anthropic_model="claude-direct-model",
        ve_agent_provider="auto",
        ve_agent_gemini_model="gemini-model",
        ve_agent_max_steps=24,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _clean_gateway():
    """Every test starts (and leaves) the gateway with no cached client/verdict."""
    g.reset_clients()
    yield
    g.reset_clients()


def _stub_probes(monkeypatch, *, vertex=(False, "no vertex"),
                 anthropic=(False, "no key"), gemini=(True, "reachable")):
    """Replace the three real probes. Returns the list of (provider, model)
    probe attempts so a test can assert what was (not) contacted."""
    calls: list[tuple[str, str]] = []
    vc, ac = object(), object()
    monkeypatch.setattr(g, "_get_vertex_claude_client", lambda: vc)
    monkeypatch.setattr(g, "_get_anthropic_client", lambda: ac)

    async def _probe_claude(client, model):
        which = "vertex" if client is vc else "anthropic"
        calls.append((which, model))
        return vertex if which == "vertex" else anthropic

    async def _probe_gemini(model):
        calls.append(("gemini", model))
        return gemini

    monkeypatch.setattr(g, "_probe_claude", _probe_claude)
    monkeypatch.setattr(g, "_probe_gemini", _probe_gemini)
    return calls


# --- a fake Anthropic client that replays a scripted conversation ----------- #
class _Block(SimpleNamespace):
    pass


def _text(text: str) -> _Block:
    return _Block(type="text", text=text)


def _tool(name: str, inp: dict, tid: str = "tu_1") -> _Block:
    return _Block(type="tool_use", name=name, input=inp, id=tid)


class _Resp:
    def __init__(self, content: list, stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, turns, repeat_last: bool, raises=None) -> None:
        self._turns = list(turns)
        self._repeat_last = repeat_last
        self._raises = raises
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        i = len(self.calls) - 1
        if i < len(self._turns):
            return self._turns[i]
        if self._repeat_last and self._turns:
            return self._turns[-1]
        return _Resp([_text("done")], "end_turn")


class _FakeClient:
    def __init__(self, turns=(), *, repeat_last=False, raises=None) -> None:
        self.messages = _FakeMessages(turns, repeat_last, raises)


def _use_fake_claude(monkeypatch, client: _FakeClient,
                     model: str = "fake-model") -> None:
    info = g.ReasonerInfo(g.VERTEX_CLAUDE, model, True, "test")

    async def _resolve(force: bool = False):
        return info

    monkeypatch.setattr(g, "resolve_reasoner", _resolve)
    monkeypatch.setattr(g, "_get_vertex_claude_client", lambda: client)
    monkeypatch.setattr(g, "config", _cfg())


def _last_tool_results(client: _FakeClient) -> list[dict]:
    """The tool_result blocks the loop fed back to the model on its last turn."""
    msgs = client.messages.calls[-1]["messages"]
    for m in reversed(msgs):
        content = m.get("content")
        if m.get("role") == "user" and isinstance(content, list):
            return [b for b in content if b.get("type") == "tool_result"]
    return []


# =========================================================================== #
# 1. _schema_to_gemini — JSON Schema -> the uppercase OpenAPI-ish dialect
# =========================================================================== #
def test_schema_to_gemini_uppercases_json_types():
    for json_t, gem_t in [("string", "STRING"), ("integer", "INTEGER"),
                          ("number", "NUMBER"), ("boolean", "BOOLEAN"),
                          ("array", "ARRAY")]:
        assert g._schema_to_gemini({"type": json_t})["type"] == gem_t
    # An object needs properties to stay an OBJECT — see the empty-object test.
    assert g._schema_to_gemini(
        {"type": "object", "properties": {"a": {"type": "string"}}}
    )["type"] == "OBJECT"


def test_schema_to_gemini_nested_objects():
    out = g._schema_to_gemini({
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "description": "a nested thing",
                "properties": {"inner": {"type": "integer"}},
                "required": ["inner"],
            },
            "flag": {"type": "boolean"},
        },
        "required": ["outer"],
    })
    assert out["type"] == "OBJECT"
    assert out["required"] == ["outer"]
    outer = out["properties"]["outer"]
    assert outer["type"] == "OBJECT"
    assert outer["description"] == "a nested thing"
    assert outer["properties"]["inner"]["type"] == "INTEGER"
    assert outer["required"] == ["inner"]
    assert out["properties"]["flag"]["type"] == "BOOLEAN"


def test_schema_to_gemini_never_emits_an_object_without_properties():
    """Vertex rejects an OBJECT whose properties map is empty — anywhere in the
    tree, not just at the top. A free-form object has nothing sensible to
    declare, so it degrades to a JSON string instead of an illegal schema."""
    assert g._schema_to_gemini({"type": "object"})["type"] == "STRING"
    assert g._schema_to_gemini({"type": "object", "properties": {}})["type"] == "STRING"
    # Nested inside an array — the shape that broke the `edit` tool.
    out = g._schema_to_gemini(
        {"type": "object",
         "properties": {"ops": {"type": "array", "items": {"type": "object"}}}}
    )
    assert out["properties"]["ops"]["items"]["type"] == "STRING"


def test_schema_to_gemini_arrays_recurse_into_items():
    out = g._schema_to_gemini({
        "type": "array",
        "items": {"type": "object", "properties": {"x": {"type": "number"}}},
    })
    assert out["type"] == "ARRAY"
    assert out["items"]["type"] == "OBJECT"
    assert out["items"]["properties"]["x"]["type"] == "NUMBER"


def test_schema_to_gemini_array_without_items_defaults_to_strings():
    out = g._schema_to_gemini({"type": "array"})
    assert out["items"] == {"type": "STRING"}


def test_schema_to_gemini_enums_are_stringified():
    out = g._schema_to_gemini({"type": "string", "enum": ["captions", "duck", 3]})
    assert out["type"] == "STRING"
    assert out["enum"] == ["captions", "duck", "3"]


def test_schema_to_gemini_enum_without_type_gets_string():
    out = g._schema_to_gemini({"enum": ["a", "b"]})
    assert out["type"] == "STRING" and out["enum"] == ["a", "b"]


def test_schema_to_gemini_type_list_takes_the_concrete_type():
    assert g._schema_to_gemini({"type": ["string", "null"]})["type"] == "STRING"
    assert g._schema_to_gemini({"type": ["null", "integer"]})["type"] == "INTEGER"
    # all-null degrades to the safe default rather than emitting nothing
    assert g._schema_to_gemini({"type": ["null"]})["type"] == "STRING"


def test_schema_to_gemini_unknown_and_non_dict_degrade_to_string():
    assert g._schema_to_gemini({"type": "date-time"})["type"] == "STRING"
    assert g._schema_to_gemini("nope") == {"type": "STRING"}
    assert g._schema_to_gemini(None) == {"type": "STRING"}
    assert g._schema_to_gemini({}) == {"type": "STRING"}


def test_schema_to_gemini_drops_unsupported_keywords():
    out = g._schema_to_gemini({
        "type": "string", "minLength": 3, "additionalProperties": False,
        "$schema": "http://json-schema.org/draft-07/schema#",
    })
    assert out == {"type": "STRING"}


def test_every_real_agent_tool_schema_translates():
    """The 12 live tool specs must survive the Gemini translation intact."""
    defs = agent._tool_defs()
    assert {t["name"] for t in defs} >= {
        "inspect_timeline", "read_transcript", "look", "preview",
        "suggest_overlay_slots", "search_assets", "get_popup_examples",
        "read_edit_log", "generate_image", "run_pass", "edit", "finish",
    }
    def empty_objects(node, path):
        """Every OBJECT-with-no-properties in the translated tree — each one is
        a schema Vertex rejects, which would 400 the whole request."""
        bad = []
        if isinstance(node, dict):
            if str(node.get("type", "")).upper() == "OBJECT" and not node.get("properties"):
                bad.append(path)
            for k, v in node.items():
                bad += empty_objects(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                bad += empty_objects(v, f"{path}[{i}]")
        return bad

    for t in defs:
        schema = t["input_schema"]
        if not (schema.get("properties") or {}):
            # Parameter-less tools: the loop omits `parameters` entirely rather
            # than sending an empty OBJECT, so there is nothing to translate.
            continue
        out = g._schema_to_gemini(schema)
        assert out["type"] == "OBJECT", t["name"]
        assert not empty_objects(out, t["name"]), empty_objects(out, t["name"])

    # `edit` carries the array-of-objects op list — the shape that broke Vertex.
    edit = next(t for t in defs if t["name"] == "edit")
    ops = g._schema_to_gemini(edit["input_schema"])["properties"]["ops"]
    assert ops["type"] == "ARRAY"
    assert ops["items"]["type"] == "OBJECT"
    assert ops["items"]["properties"], "op items must declare real fields"
    assert "op" in ops["items"]["properties"]


# =========================================================================== #
# 2. _normalise_tool_output
# =========================================================================== #
def test_normalise_plain_string():
    assert g._normalise_tool_output("hello") == ("hello", [])


def test_normalise_non_string_scalars_are_stringified():
    text, images = g._normalise_tool_output(42)
    assert text == "42" and images == []


def test_normalise_dict_with_text():
    assert g._normalise_tool_output({"text": "ok"}) == ("ok", [])


def test_normalise_dict_without_text_is_empty_not_none():
    text, images = g._normalise_tool_output({"images": []})
    assert text == "" and images == []


def test_normalise_truncates_long_text():
    long_str = "x" * (g._MAX_TOOL_RESULT_CHARS + 500)
    assert len(g._normalise_tool_output(long_str)[0]) == g._MAX_TOOL_RESULT_CHARS
    assert len(g._normalise_tool_output({"text": long_str})[0]) == g._MAX_TOOL_RESULT_CHARS


def test_normalise_keeps_real_images(tmp_path):
    a, b = tmp_path / "a.jpg", tmp_path / "b.png"
    a.write_bytes(b"jpeg"), b.write_bytes(_PNG)
    text, images = g._normalise_tool_output({"text": "look", "images": [a, str(b)]})
    assert text == "look"
    assert [p.name for p in images] == ["a.jpg", "b.png"]
    assert all(isinstance(p, Path) for p in images)


def test_normalise_caps_the_image_count(tmp_path):
    paths = []
    for i in range(g._MAX_IMAGES_PER_RESULT + 3):
        p = tmp_path / f"f{i}.jpg"
        p.write_bytes(b"jpeg")
        paths.append(p)
    _, images = g._normalise_tool_output({"text": "many", "images": paths})
    assert len(images) == g._MAX_IMAGES_PER_RESULT


def test_normalise_only_considers_the_first_n_entries(tmp_path):
    """The cap is a slice of the RAW list, applied before the files are checked:
    a tool that returns stale paths first loses the good ones behind them. Worth
    knowing when a `look()` result comes back with fewer frames than expected."""
    good = tmp_path / "good.jpg"
    good.write_bytes(b"jpeg")
    stale = [str(tmp_path / f"gone{i}.jpg") for i in range(g._MAX_IMAGES_PER_RESULT)]
    _, images = g._normalise_tool_output({"text": "t", "images": stale + [good]})
    assert images == []


def test_normalise_drops_missing_and_unusable_paths(tmp_path):
    real = tmp_path / "real.jpg"
    real.write_bytes(b"jpeg")
    _, images = g._normalise_tool_output({
        "text": "t",
        "images": [str(tmp_path / "gone.jpg"), real, tmp_path, None],
    })
    assert images == [real]          # missing file, a directory and None all dropped


# =========================================================================== #
# 3. _claude_tools — one cache breakpoint, on the LAST tool
# =========================================================================== #
def test_claude_tools_cache_breakpoint_is_on_the_last_tool_only():
    out = g._claude_tools([
        {"name": "a", "description": "A", "input_schema": {"type": "object"}},
        {"name": "b", "description": "B", "input_schema": {"type": "object"}},
        {"name": "c", "description": "C", "input_schema": {"type": "object"}},
    ])
    assert [t["name"] for t in out] == ["a", "b", "c"]
    assert [("cache_control" in t) for t in out] == [False, False, True]
    assert out[-1]["cache_control"] == {"type": "ephemeral"}


def test_claude_tools_single_tool_gets_the_breakpoint():
    out = g._claude_tools([{"name": "only"}])
    assert out[0]["cache_control"] == {"type": "ephemeral"}


def test_claude_tools_empty_list_is_empty():
    assert g._claude_tools([]) == []


def test_claude_tools_fills_missing_description_and_schema():
    out = g._claude_tools([{"name": "bare"}])
    assert out[0]["description"] == ""
    assert out[0]["input_schema"] == {"type": "object", "properties": {}}


def test_claude_tools_does_not_mutate_the_caller_specs():
    specs = [{"name": "a"}, {"name": "b"}]
    g._claude_tools(specs)
    assert specs == [{"name": "a"}, {"name": "b"}]


def test_claude_tools_on_the_real_agent_tools_caches_after_finish():
    out = g._claude_tools(agent._tool_defs())
    assert out[-1]["name"] == "finish"
    assert sum(1 for t in out if "cache_control" in t) == 1


# =========================================================================== #
# 4. resolve_reasoner — order, forcing, caching
# =========================================================================== #
def test_resolve_picks_vertex_claude_when_it_probes_ok(monkeypatch):
    monkeypatch.setattr(g, "config", _cfg(anthropic_api_key="sk-test"))
    calls = _stub_probes(monkeypatch, vertex=(True, "reachable"),
                         anthropic=(True, "reachable"))
    info = _run(g.resolve_reasoner())
    assert info.provider == g.VERTEX_CLAUDE
    assert info.model == "claude-vertex-model"
    assert info.ok is True
    # nothing after vertex was even contacted
    assert calls == [("vertex", "claude-vertex-model")]


def test_resolve_falls_through_to_anthropic_when_vertex_fails(monkeypatch):
    monkeypatch.setattr(g, "config", _cfg(anthropic_api_key="sk-test"))
    calls = _stub_probes(monkeypatch, vertex=(False, "403 not enabled"),
                         anthropic=(True, "reachable"))
    info = _run(g.resolve_reasoner())
    assert info.provider == g.ANTHROPIC_CLAUDE
    assert info.model == "claude-direct-model"
    assert info.ok is True
    assert [c[0] for c in calls] == ["vertex", "anthropic"]


def test_resolve_skips_anthropic_without_a_key(monkeypatch):
    monkeypatch.setattr(g, "config", _cfg(anthropic_api_key=""))
    calls = _stub_probes(monkeypatch, vertex=(False, "403"))
    info = _run(g.resolve_reasoner())
    assert info.provider == g.GEMINI
    assert [c[0] for c in calls] == ["vertex", "gemini"]
    assert "ANTHROPIC_API_KEY not set" in info.reason


def test_resolve_falls_through_to_gemini_when_no_claude_works(monkeypatch):
    monkeypatch.setattr(g, "config", _cfg(anthropic_api_key="sk-test"))
    calls = _stub_probes(monkeypatch, vertex=(False, "403 forbidden"),
                         anthropic=(False, "401 bad key"))
    info = _run(g.resolve_reasoner())
    assert info.provider == g.GEMINI
    assert info.model == "gemini-model"
    assert info.ok is True
    assert [c[0] for c in calls] == ["vertex", "anthropic", "gemini"]
    # the reason carries WHY each Claude was skipped — that is the whole
    # diagnostic value of the blob the UI shows.
    assert "vertex-claude unavailable" in info.reason
    assert "anthropic-claude unavailable" in info.reason


def test_resolve_returns_gemini_even_when_its_own_probe_fails(monkeypatch):
    """Gemini is the floor: a failed probe is reported, not swallowed into
    'no provider at all'."""
    monkeypatch.setattr(g, "config", _cfg())
    _stub_probes(monkeypatch, gemini=(False, "quota"))
    info = _run(g.resolve_reasoner())
    assert info.provider == g.GEMINI
    assert info.ok is False
    assert "Gemini probe failed" in info.reason


def test_resolve_honours_a_forced_provider(monkeypatch):
    monkeypatch.setattr(g, "config",
                        _cfg(ve_agent_provider=g.GEMINI, anthropic_api_key="sk-test"))
    calls = _stub_probes(monkeypatch, vertex=(True, "reachable"),
                         anthropic=(True, "reachable"))
    info = _run(g.resolve_reasoner())
    assert info.provider == g.GEMINI
    assert [c[0] for c in calls] == ["gemini"]     # no Claude was contacted


def test_forcing_anthropic_skips_vertex(monkeypatch):
    monkeypatch.setattr(g, "config", _cfg(ve_agent_provider=g.ANTHROPIC_CLAUDE,
                                          anthropic_api_key="sk-test"))
    calls = _stub_probes(monkeypatch, vertex=(True, "reachable"),
                         anthropic=(True, "reachable"))
    info = _run(g.resolve_reasoner())
    assert info.provider == g.ANTHROPIC_CLAUDE
    assert [c[0] for c in calls] == ["anthropic"]


def test_forcing_vertex_skips_anthropic(monkeypatch):
    monkeypatch.setattr(g, "config", _cfg(ve_agent_provider=g.VERTEX_CLAUDE,
                                          anthropic_api_key="sk-test"))
    calls = _stub_probes(monkeypatch, vertex=(False, "403"),
                         anthropic=(True, "reachable"))
    info = _run(g.resolve_reasoner())
    # forced vertex that cannot answer still lands on the floor, never on the
    # provider the operator pinned away from
    assert info.provider == g.GEMINI
    assert [c[0] for c in calls] == ["vertex", "gemini"]


def test_resolve_skips_vertex_without_a_project(monkeypatch):
    monkeypatch.setattr(g, "config", _cfg(google_cloud_project=""))
    calls = _stub_probes(monkeypatch)
    info = _run(g.resolve_reasoner())
    assert info.provider == g.GEMINI
    assert [c[0] for c in calls] == ["gemini"]


def test_resolve_caches_the_verdict(monkeypatch):
    monkeypatch.setattr(g, "config", _cfg())
    calls = _stub_probes(monkeypatch, vertex=(True, "reachable"))

    async def _twice():
        return await g.resolve_reasoner(), await g.resolve_reasoner()

    first, second = _run(_twice())
    assert first is second                 # same object, not a re-probe
    assert len(calls) == 1


def test_force_true_reprobes(monkeypatch):
    monkeypatch.setattr(g, "config", _cfg())
    calls = _stub_probes(monkeypatch, vertex=(True, "reachable"))

    async def _both():
        a = await g.resolve_reasoner()
        b = await g.resolve_reasoner(force=True)
        return a, b

    first, second = _run(_both())
    assert first is not second
    assert len(calls) == 2


def test_reset_clients_clears_the_cached_verdict(monkeypatch):
    monkeypatch.setattr(g, "config", _cfg())
    calls = _stub_probes(monkeypatch, vertex=(True, "reachable"))
    _run(g.resolve_reasoner())
    g.reset_clients()
    _run(g.resolve_reasoner())
    assert len(calls) == 2


def test_a_failed_verdict_expires_sooner_than_a_healthy_one(monkeypatch):
    """A broken Claude must not be re-probed on every request, but must be
    retried sooner than a healthy provider would be."""
    monkeypatch.setattr(g, "config", _cfg())
    calls = _stub_probes(monkeypatch, gemini=(False, "quota"))
    info = _run(g.resolve_reasoner())
    assert info.ok is False
    # still inside the short fail-TTL -> cached
    _run(g.resolve_reasoner())
    assert len([c for c in calls if c[0] == "gemini"]) == 1
    # push the verdict past the fail-TTL but keep it inside the healthy TTL
    g._cached_reasoner.checked_at -= g._PROBE_FAIL_TTL_SEC + 1
    assert g._PROBE_FAIL_TTL_SEC < g._PROBE_TTL_SEC
    _run(g.resolve_reasoner())
    assert len([c for c in calls if c[0] == "gemini"]) == 2


def test_a_client_that_cannot_be_built_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(g, "config", _cfg())
    _stub_probes(monkeypatch)

    def _boom():
        raise RuntimeError("anthropic[vertex] not installed")

    monkeypatch.setattr(g, "_get_vertex_claude_client", _boom)
    info = _run(g.resolve_reasoner())
    assert info.provider == g.GEMINI
    assert "not installed" in info.reason


def test_reasoner_status_exposes_the_configured_block(monkeypatch):
    monkeypatch.setattr(g, "config", _cfg(anthropic_api_key="sk-test"))
    _stub_probes(monkeypatch, vertex=(True, "reachable"))
    st = _run(g.reasoner_status())
    assert st["provider"] == g.VERTEX_CLAUDE
    assert st["ok"] is True
    assert isinstance(st["checked_at"], float)
    cfgblk = st["configured"]
    assert cfgblk == {
        "google_cloud_project": True,
        "claude_vertex_region": "us-east5",
        "claude_model": "claude-vertex-model",
        "anthropic_api_key_set": True,
        "anthropic_model": "claude-direct-model",
        "gemini_model": "gemini-model",
        "forced_provider": "auto",
    }


# =========================================================================== #
# 5. reasoner_tool_loop — driven through a fake Claude
# =========================================================================== #
def test_loop_runs_a_tool_and_returns_the_final_text(monkeypatch):
    client = _FakeClient([
        _Resp([_text("let me look"), _tool("inspect_timeline", {})], "tool_use"),
        _Resp([_text("all done")], "end_turn"),
    ])
    _use_fake_claude(monkeypatch, client)
    seen: list[tuple[str, dict]] = []

    async def dispatch(name, inp):
        seen.append((name, inp))
        return "timeline: 3 clips"

    out = _run(g.reasoner_tool_loop(
        system="sys", user_message="do a thing", tools=agent._tool_defs(),
        tool_dispatch=dispatch, max_steps=5))

    assert seen == [("inspect_timeline", {})]
    assert out["text"] == "all done"
    assert out["stopped"] == "done"
    assert out["provider"] == g.VERTEX_CLAUDE
    assert out["model"] == "fake-model"
    assert out["steps"] == [{"step": 1, "tool": "inspect_timeline", "input": {},
                             "ok": True, "result": "timeline: 3 clips", "images": 0}]


def test_loop_records_every_tool_result_and_truncates_it(monkeypatch):
    client = _FakeClient([
        _Resp([_tool("read_transcript", {"clip_id": "c1"}, "t1")], "tool_use"),
        _Resp([_tool("edit", {"ops": [{"op": "mute_clip"}]}, "t2")], "tool_use"),
        _Resp([_text("finished")], "end_turn"),
    ])
    _use_fake_claude(monkeypatch, client)

    async def dispatch(name, inp):
        return "y" * 1000 if name == "edit" else "hello world"

    out = _run(g.reasoner_tool_loop(
        system="s", user_message="u", tools=[{"name": "read_transcript"},
                                             {"name": "edit"}],
        tool_dispatch=dispatch, max_steps=8))

    assert [(s["step"], s["tool"], s["ok"]) for s in out["steps"]] == [
        (1, "read_transcript", True), (2, "edit", True)]
    assert out["steps"][0]["input"] == {"clip_id": "c1"}
    assert out["steps"][0]["result"] == "hello world"
    assert len(out["steps"][1]["result"]) == 400      # steps stay skimmable
    assert out["stopped"] == "done"


def test_loop_enforces_the_step_cap(monkeypatch):
    """A model that never stops asking for tools is cut off, not run forever."""
    client = _FakeClient([_Resp([_tool("look", {"at_s": 1})], "tool_use")],
                         repeat_last=True)
    _use_fake_claude(monkeypatch, client)
    n = 0

    async def dispatch(name, inp):
        nonlocal n
        n += 1
        return "a frame"

    out = _run(g.reasoner_tool_loop(
        system="s", user_message="u", tools=[{"name": "look"}],
        tool_dispatch=dispatch, max_steps=3))

    assert out["stopped"] == "max_steps"
    assert len(out["steps"]) == 3
    assert n == 3
    assert len(client.messages.calls) == 3


def test_loop_step_cap_is_clamped_to_the_hard_ceiling(monkeypatch):
    client = _FakeClient([_Resp([_tool("look", {})], "tool_use")], repeat_last=True)
    _use_fake_claude(monkeypatch, client)

    async def dispatch(name, inp):
        return "frame"

    out = _run(g.reasoner_tool_loop(
        system="s", user_message="u", tools=[{"name": "look"}],
        tool_dispatch=dispatch, max_steps=500))
    assert len(out["steps"]) == 60          # never more than the hard ceiling
    assert out["stopped"] == "max_steps"


def test_loop_default_step_cap_comes_from_config(monkeypatch):
    client = _FakeClient([_Resp([_tool("look", {})], "tool_use")], repeat_last=True)
    _use_fake_claude(monkeypatch, client)
    monkeypatch.setattr(g, "config", _cfg(ve_agent_max_steps=2))

    async def dispatch(name, inp):
        return "frame"

    out = _run(g.reasoner_tool_loop(
        system="s", user_message="u", tools=[{"name": "look"}],
        tool_dispatch=dispatch))
    assert len(out["steps"]) == 2


def test_a_failing_tool_is_reported_to_the_model_and_the_run_continues(monkeypatch):
    """A tool blowing up must become an error the model can react to — killing
    the whole run on one bad op would lose every edit already applied."""
    client = _FakeClient([
        _Resp([_tool("edit", {"ops": [{"op": "delete_clip", "clip_id": "ghost"}]},
                     "bad")], "tool_use"),
        _Resp([_tool("edit", {"ops": [{"op": "mute_clip", "clip_id": "c1"}]},
                     "good")], "tool_use"),
        _Resp([_text("recovered")], "end_turn"),
    ])
    _use_fake_claude(monkeypatch, client)
    seen: list[str] = []

    async def dispatch(name, inp):
        seen.append(str(inp))
        if len(seen) == 1:
            raise ValueError("no such clip 'ghost'")
        return "applied 1 op"

    out = _run(g.reasoner_tool_loop(
        system="s", user_message="u", tools=[{"name": "edit"}],
        tool_dispatch=dispatch, max_steps=6))

    assert len(seen) == 2                       # the run kept going
    assert out["stopped"] == "done"
    assert out["text"] == "recovered"
    assert [s["ok"] for s in out["steps"]] == [False, True]
    assert "no such clip" in out["steps"][0]["result"]
    assert out["steps"][0]["result"].startswith("ValueError")

    # ...and the failure was actually handed back to the model.
    errors = [b for m in client.messages.calls[-1]["messages"]
              if isinstance(m.get("content"), list)
              for b in m["content"]
              if isinstance(b, dict) and b.get("type") == "tool_result"
              and b.get("is_error")]
    assert len(errors) == 1
    assert errors[0]["tool_use_id"] == "bad"
    assert "no such clip 'ghost'" in errors[0]["content"]


def test_several_tool_calls_in_one_turn_are_all_answered(monkeypatch):
    client = _FakeClient([
        _Resp([_tool("look", {"at_s": 1}, "a"), _tool("look", {"at_s": 9}, "b")],
              "tool_use"),
        _Resp([_text("ok")], "end_turn"),
    ])
    _use_fake_claude(monkeypatch, client)

    async def dispatch(name, inp):
        return f"frame at {inp['at_s']}"

    out = _run(g.reasoner_tool_loop(
        system="s", user_message="u", tools=[{"name": "look"}],
        tool_dispatch=dispatch, max_steps=4))
    assert [s["result"] for s in out["steps"]] == ["frame at 1", "frame at 9"]
    assert [s["step"] for s in out["steps"]] == [1, 1]   # same round-trip
    results = _last_tool_results(client)
    assert [r["tool_use_id"] for r in results] == ["a", "b"]


def test_tool_images_are_delivered_as_real_image_blocks(monkeypatch, tmp_path):
    """`look()` returning frames is the whole point — they must reach the model
    as image content, not as a filename in a string."""
    png = tmp_path / "frame.png"
    png.write_bytes(_PNG)
    client = _FakeClient([
        _Resp([_tool("look", {"at_s": 2}, "img")], "tool_use"),
        _Resp([_text("I can see it")], "end_turn"),
    ])
    _use_fake_claude(monkeypatch, client)

    async def dispatch(name, inp):
        return {"text": '{"at_s": 2}', "images": [png]}

    out = _run(g.reasoner_tool_loop(
        system="s", user_message="u", tools=[{"name": "look"}],
        tool_dispatch=dispatch, max_steps=4))

    assert out["steps"][0]["images"] == 1
    content = _last_tool_results(client)[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": '{"at_s": 2}'}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["media_type"] == "image/png"
    assert base64.b64decode(content[1]["source"]["data"]) == _PNG


def test_image_result_with_no_text_still_carries_a_text_block(monkeypatch, tmp_path):
    png = tmp_path / "frame.png"
    png.write_bytes(_PNG)
    client = _FakeClient([
        _Resp([_tool("look", {}, "img")], "tool_use"),
        _Resp([_text("ok")], "end_turn"),
    ])
    _use_fake_claude(monkeypatch, client)

    async def dispatch(name, inp):
        return {"text": "", "images": [png]}

    _run(g.reasoner_tool_loop(system="s", user_message="u",
                              tools=[{"name": "look"}],
                              tool_dispatch=dispatch, max_steps=3))
    content = _last_tool_results(client)[0]["content"]
    assert content[0]["text"] == "(see image)"      # never an empty text block


def test_empty_tool_result_is_not_sent_as_an_empty_content_block(monkeypatch):
    """Regression: the API rejects empty content blocks, so a tool that returns
    nothing must still say something or the whole run 500s."""
    client = _FakeClient([
        _Resp([_tool("read_edit_log", {}, "e")], "tool_use"),
        _Resp([_text("ok")], "end_turn"),
    ])
    _use_fake_claude(monkeypatch, client)

    async def dispatch(name, inp):
        return {"text": "", "images": []}

    _run(g.reasoner_tool_loop(system="s", user_message="u",
                              tools=[{"name": "read_edit_log"}],
                              tool_dispatch=dispatch, max_steps=3))
    assert _last_tool_results(client)[0]["content"] != ""


def test_on_step_is_called_for_each_step_and_its_errors_are_swallowed(monkeypatch):
    client = _FakeClient([
        _Resp([_tool("look", {"at_s": 1}, "a")], "tool_use"),
        _Resp([_tool("look", {"at_s": 2}, "b")], "tool_use"),
        _Resp([_text("ok")], "end_turn"),
    ])
    _use_fake_claude(monkeypatch, client)
    got: list[dict] = []

    def on_step(step):
        got.append(step)
        raise RuntimeError("the progress writer died")   # must not kill the run

    async def dispatch(name, inp):
        return "frame"

    out = _run(g.reasoner_tool_loop(
        system="s", user_message="u", tools=[{"name": "look"}],
        tool_dispatch=dispatch, max_steps=5, on_step=on_step))
    assert [s["tool"] for s in got] == ["look", "look"]
    assert out["stopped"] == "done"


def test_loop_sends_cached_system_and_tools_every_step(monkeypatch):
    client = _FakeClient([
        _Resp([_tool("look", {}, "a")], "tool_use"),
        _Resp([_text("ok")], "end_turn"),
    ])
    _use_fake_claude(monkeypatch, client)

    async def dispatch(name, inp):
        return "frame"

    _run(g.reasoner_tool_loop(system="SYSTEM", user_message="u",
                              tools=[{"name": "look"}],
                              tool_dispatch=dispatch, max_steps=4))
    for call in client.messages.calls:
        assert call["system"][0]["text"] == "SYSTEM"
        assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert call["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        assert call["model"] == "fake-model"


def test_a_provider_that_dies_mid_run_invalidates_the_cached_verdict(monkeypatch):
    """A stale 'this provider works' verdict must not pin every later run to a
    dead brain."""
    info = g.ReasonerInfo(g.VERTEX_CLAUDE, "fake-model", True, "test")
    g._cached_reasoner = info

    async def _resolve(force: bool = False):
        return info

    monkeypatch.setattr(g, "resolve_reasoner", _resolve)
    monkeypatch.setattr(g, "config", _cfg())
    monkeypatch.setattr(g, "_get_vertex_claude_client",
                        lambda: _FakeClient(raises=ValueError("boom")))

    async def dispatch(name, inp):
        return "never reached"

    with pytest.raises(ValueError):
        _run(g.reasoner_tool_loop(system="s", user_message="u",
                                  tools=[{"name": "look"}],
                                  tool_dispatch=dispatch, max_steps=2))
    assert g._cached_reasoner is None


def test_retry_classifier_only_retries_transient_failures():
    assert g._is_retryable(RuntimeError("429 Too Many Requests"))
    assert g._is_retryable(RuntimeError("RESOURCE_EXHAUSTED"))
    assert g._is_retryable(RuntimeError("Overloaded"))
    assert g._is_retryable(RuntimeError("529 overloaded_error"))
    assert not g._is_retryable(ValueError("400 invalid_request_error"))
    assert not g._is_retryable(ValueError("no such clip"))


# =========================================================================== #
# 6. The agent: frame maths, clip lookup, and the single write path
# =========================================================================== #
def test_frame_seconds_round_trip():
    assert agent.FPS == 30
    assert agent._s2f(1.5) == 45
    assert agent._f2s(45) == 1.5
    assert agent._s2f(agent._f2s(90)) == 90
    assert agent._f2s(0) == 0.0
    assert agent._s2f(0) == 0


def test_s2f_rounds_to_the_nearest_frame():
    assert agent._s2f(0.51) == 15          # 15.3 -> 15
    assert agent._s2f(0.517) == 16         # 15.51 -> 16
    assert agent._s2f("2") == 60           # numeric strings are fine


def test_s2f_never_returns_a_negative_frame():
    assert agent._s2f(-3) == 0


def test_f2s_rounds_to_two_decimals():
    assert agent._f2s(31) == 1.03
    assert agent._f2s(100) == 3.33


def test_frame_helpers_survive_garbage():
    """The model supplies these values; a bad one must not explode the tool."""
    for bad in (None, "soon", "", {}, []):
        assert agent._f2s(bad) == 0.0
        assert agent._s2f(bad) == 0


def _tl(*clips) -> dict:
    return {"clips": list(clips)}


def test_clip_at_finds_the_covering_clip():
    tl = _tl(
        {"id": "a", "kind": "video", "trackId": "trk_v0", "from": 0,
         "durationInFrames": 60},
        {"id": "b", "kind": "video", "trackId": "trk_v0", "from": 60,
         "durationInFrames": 60},
    )
    assert agent._clip_at(tl, 0)["id"] == "a"
    assert agent._clip_at(tl, 59)["id"] == "a"
    assert agent._clip_at(tl, 60)["id"] == "b"      # end is exclusive
    assert agent._clip_at(tl, 119)["id"] == "b"


def test_clip_at_returns_none_off_the_end_and_in_a_gap():
    tl = _tl({"id": "a", "kind": "video", "trackId": "trk_v0", "from": 30,
              "durationInFrames": 30})
    assert agent._clip_at(tl, 29) is None
    assert agent._clip_at(tl, 60) is None
    assert agent._clip_at({}, 0) is None
    assert agent._clip_at({"clips": []}, 5) is None


def test_clip_at_prefers_the_main_lane_over_an_ai_overlay_lane():
    """look() must return the footage under the overlays, not the overlay clip."""
    tl = _tl(
        {"id": "overlay", "kind": "video", "trackId": "trk_v_ai", "from": 0,
         "durationInFrames": 90},
        {"id": "base", "kind": "video", "trackId": "trk_v0", "from": 0,
         "durationInFrames": 90},
    )
    assert agent._clip_at(tl, 45)["id"] == "base"
    # ...regardless of the order they sit in the timeline
    tl["clips"].reverse()
    assert agent._clip_at(tl, 45)["id"] == "base"


def test_clip_at_filters_by_kind_and_defaults_to_video():
    tl = _tl(
        {"id": "music", "kind": "audio", "trackId": "trk_a0", "from": 0,
         "durationInFrames": 300},
        {"id": "untagged", "trackId": "trk_v0", "from": 0, "durationInFrames": 300},
    )
    assert agent._clip_at(tl, 10)["id"] == "untagged"      # missing kind == video
    assert agent._clip_at(tl, 10, kind="audio")["id"] == "music"
    assert agent._clip_at(tl, 10, kind="image") is None


def test_clip_at_tolerates_missing_timing_fields():
    tl = _tl({"id": "a", "kind": "video", "trackId": "trk_v0"})
    assert agent._clip_at(tl, 0) is None       # zero-length clip covers nothing


def _no_writes(monkeypatch):
    """Wire every write/read path the agent could reach to an explosion."""
    def boom(*a, **k):
        raise AssertionError("the database / apply path was touched")

    monkeypatch.setattr(agent, "apply_plan", boom)
    monkeypatch.setattr(agent, "get_conn", boom)
    monkeypatch.setattr(agent.ve, "_fetch_row", boom)


def test_apply_rejects_an_empty_op_list_without_touching_the_db(monkeypatch):
    _no_writes(monkeypatch)

    async def _never():
        raise AssertionError("_enabled_asset_ids hit the database")

    monkeypatch.setattr(agent, "_enabled_asset_ids", _never)
    monkeypatch.setattr(agent, "validate_and_filter_ops",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("validation ran on an empty plan")))

    run = agent._Run(7, {})
    out = _run(run.apply([], label="edit"))
    assert out == {"applied": 0, "rejected": [], "note": "no ops given"}
    assert run.applied == 0 and run.rev is None


def test_apply_rejects_non_list_ops_without_touching_the_db(monkeypatch):
    _no_writes(monkeypatch)
    run = agent._Run(7, {})
    for bad in (None, {}, "ops", 0):
        out = _run(run.apply(bad, label="edit"))
        assert out["applied"] == 0 and out["note"] == "no ops given"
    assert run.applied == 0


def test_apply_does_not_write_when_every_op_is_rejected(monkeypatch):
    """All-invalid ids must come back as an explainable rejection, not an
    apply_plan call that sweeps the timeline."""
    _no_writes(monkeypatch)

    async def _assets():
        return set()

    monkeypatch.setattr(agent, "_enabled_asset_ids", _assets)
    run = agent._Run(7, {})
    run.timeline = {"clips": [{"id": "real", "trackId": "trk_v0", "from": 0,
                               "durationInFrames": 30}]}

    out = _run(run.apply([{"op": "mute_clip", "clip_id": "ghost"}], label="edit"))
    assert out["applied"] == 0
    assert out["rejected"] and run.all_rejected == out["rejected"]
    assert "inspect_timeline" in out["note"]
    assert run.applied == 0


def test_apply_uses_a_random_plan_id_and_regrounds(monkeypatch):
    """Additive agent work must never reuse a stable plan id — apply_plan sweeps
    the previous clips of the same plan id first."""
    seen: list[str] = []

    async def _apply_plan(project_id, *, plan_id, ops, analysis):
        seen.append(plan_id)
        return {"applied": len(ops), "rev": 12 + len(seen), "failed": []}

    async def _assets():
        return set()

    monkeypatch.setattr(agent, "apply_plan", _apply_plan)
    monkeypatch.setattr(agent, "_enabled_asset_ids", _assets)
    monkeypatch.setattr(agent, "validate_and_filter_ops",
                        lambda ops, **k: (list(ops), []))

    regrounds = []
    run = agent._Run(7, {})
    run.timeline = {"clips": []}

    async def _reground():
        regrounds.append(1)

    monkeypatch.setattr(run, "reground", _reground)

    out1 = _run(run.apply([{"op": "mute_clip", "clip_id": "c1"}], label="edit"))
    out2 = _run(run.apply([{"op": "mute_clip", "clip_id": "c1"}], label="edit"))

    assert out1["applied"] == 1 and out2["applied"] == 1
    assert run.applied == 2                       # totals accumulate across calls
    assert run.rev == 14                          # latest rev wins
    assert len(seen) == len(set(seen)) == 2       # a fresh plan id every time
    assert all(p.startswith("ag7_") for p in seen)
    assert len(regrounds) == 2                    # re-grounded after every apply


if __name__ == "__main__":  # allow `python tests/test_video_edit_agent.py`
    raise SystemExit(pytest.main([__file__, "-q"]))


# =========================================================================== #
# 7. Regressions from the adversarial review
# =========================================================================== #
def test_source_frame_mapping_at_normal_speed():
    """timeline_f -> source_f is offset by (from - trimBefore)."""
    clip = {"from": 100, "trimBefore": 40, "trimAfter": 340,
            "durationInFrames": 300}
    assert agent.source_frame_for(clip, 100) == 40      # clip start
    assert agent.source_frame_for(clip, 160) == 100     # 2s in
    # Clamped inside the trim window rather than running off the end.
    assert agent.source_frame_for(clip, 10_000) == 339


def test_source_frame_mapping_respects_playback_rate():
    """REGRESSION: set_speed keeps the trim window and shrinks the duration, so
    a naive `timeline_f - offset` reads the wrong moment on a re-timed clip."""
    # 300 source frames played at 2x => 150 timeline frames.
    clip = {"from": 0, "trimBefore": 0, "trimAfter": 300,
            "durationInFrames": 150, "playbackRate": 2.0}
    assert agent.source_frame_for(clip, 0) == 0
    assert agent.source_frame_for(clip, 120) == 240     # not 120
    assert agent.source_frame_for(clip, 149) == 298
    # Half speed reads the source more slowly than the timeline advances.
    slow = {"from": 0, "trimBefore": 0, "trimAfter": 150,
            "durationInFrames": 300, "playbackRate": 0.5}
    assert agent.source_frame_for(slow, 200) == 100


def _fake_run(height=1920):
    run = agent._Run.__new__(agent._Run)
    run.project_id = 1
    run.analysis = {}
    run.timeline = {"project": {"height": height, "width": 1080}, "clips": []}
    run.memory = {}
    return run


def test_pass_captions_uses_the_real_segment_keys_and_pixel_y(monkeypatch):
    """REGRESSION: the segments are keyed start_f/end_f (NOT from/
    durationInFrames), and `y` is a CENTRE-RELATIVE PIXEL offset — passing the
    0.34 lower-third fraction raw plants every caption on the speaker's face."""
    import app.services.yas_video_edit_ai as veai

    monkeypatch.setattr(
        veai, "_caption_segments_timeline",
        lambda timeline, analysis: [
            {"start_f": 120, "end_f": 195, "text": "hello there"},
            {"start_f": 200, "end_f": 205, "text": "tiny"},
        ],
    )

    async def _style():
        return "#FFFFFF", "Inter"

    monkeypatch.setattr(veai, "_brand_caption_style", _style)

    ops, note = _run(agent._pass_captions(_fake_run(height=1920)))

    assert len(ops) == 2, note
    first = ops[0]
    assert first["op"] == "add_text_popup"
    assert first["from"] == 120
    assert first["durationInFrames"] == 75          # end_f - start_f
    assert first["text"] == "hello there"
    # 0.34 * 1920 — a pixel offset, not the raw fraction.
    assert first["y"] == 653
    # A 5-frame phrase is floored so it stays readable.
    assert ops[1]["durationInFrames"] == 18


def test_pass_captions_reports_cleanly_when_there_is_no_transcript(monkeypatch):
    import app.services.yas_video_edit_ai as veai

    monkeypatch.setattr(veai, "_caption_segments_timeline",
                        lambda timeline, analysis: [])
    ops, note = _run(agent._pass_captions(_fake_run()))
    assert ops == []
    assert "transcript" in note.lower()


def test_a_non_retryable_failure_demotes_the_provider(monkeypatch):
    """REGRESSION: clearing the probe cache alone re-selects the same broken
    provider forever, because the tiny probe still passes."""
    g.reset_clients()
    monkeypatch.setattr(g, "_is_retryable", lambda exc: False)
    g._demoted.clear()
    g._demoted[g.VERTEX_CLAUDE] = __import__("time").time()
    assert g._is_demoted(g.VERTEX_CLAUDE)
    # A demotion older than the TTL expires on its own.
    g._demoted[g.VERTEX_CLAUDE] = __import__("time").time() - g._DEMOTE_TTL_SEC - 1
    assert not g._is_demoted(g.VERTEX_CLAUDE)
    g.reset_clients()


def test_every_advertised_op_actually_validates():
    """REGRESSION: the `edit` tool's op enum is a promise to the model. Any op
    it names that `validate_and_filter_ops` rejects is a guaranteed dead end —
    the model emits it, the validator drops it, and the agent burns a step
    working out why. (`mute_clip` shipped without its required `muted` field.)"""
    from app.services.yas_video_edit_ai_ops import validate_and_filter_ops

    minimal = {
        "reorder_clips": {"track_id": "t1", "clip_ids": ["c1"]},
        "set_clip_position": {"clip_id": "c1", "from": 10},
        "trim_clip": {"clip_id": "c1", "trimBefore": 5},
        "add_transition": {"clip_id": "c1", "preset": "fade", "durationInFrames": 14},
        "add_text_popup": {"text": "hi", "from": 10},
        "add_framed_popup": {"text": "hi", "from": 10, "frame_asset_id": 1},
        "add_sticker": {"asset_id": 1, "from": 10},
        "set_text": {"clip_id": "c1", "text": "x"},
        "set_keyframes": {"clip_id": "c1", "property": "volume",
                          "points": [{"f": 0, "v": 1}]},
        "set_filters": {"clip_id": "c1", "brightness": 1.0},
        "resize_clip": {"clip_id": "c1", "factor": 0.8},
        "set_speed": {"clip_id": "c1", "speed": 2},
        "mute_clip": {"clip_id": "c1", "muted": True},
        "delete_clip": {"clip_id": "c1"},
        "add_music": {"style_or_query": "calm"},
        "add_media_clip": {"source_kind": "upload", "source_id": 1},
        "trim_to_video_end": {"clip_id": "c1"},
    }

    advertised = agent._OP_ITEM_SCHEMA["properties"]["op"]["enum"]
    # Every advertised op must have a documented minimal form in this test, so
    # adding one to the enum without checking it here fails loudly.
    assert set(advertised) == set(minimal), set(advertised) ^ set(minimal)

    rejected = {}
    for name in advertised:
        accepted, rej = validate_and_filter_ops(
            [{"op": name, **minimal[name]}], feature="edit",
            valid_clip_ids={"c1"}, valid_asset_ids={1},
            valid_clip_sources={("upload", 1)},
        )
        if not accepted:
            rejected[name] = rej[0].get("error") if rej else "?"
    assert not rejected, rejected


def test_every_advertised_op_field_is_declared_in_the_schema():
    """The fields those ops need must be declarable — otherwise a provider that
    enforces the schema (Gemini) strips them before they ever reach us."""
    props = agent._OP_ITEM_SCHEMA["properties"]
    for field in ("op", "clip_id", "track_id", "clip_ids", "from",
                  "durationInFrames", "trimBefore", "text", "preset", "muted",
                  "points", "property", "asset_id", "frame_asset_id",
                  "source_kind", "source_id", "speed", "factor"):
        assert field in props, field
