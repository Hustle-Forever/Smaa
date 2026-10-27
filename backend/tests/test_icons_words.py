"""Rebrand studio icons, word timing (rebrand_harness/words.py): Whisper's
word chunks aligned to the script, onsets snapped to the speech envelope, the
estimate fallback, the said transcript and its cache.

Whisper itself is faked everywhere except the real-clip test at the bottom,
which runs only where the pinned whisper-base.en weights are already cached
(and the studio's 806/815 scenes are on disk). No paid call can run: the
autouse guard makes Gemini raise."""
from __future__ import annotations

import json
import math
import sys
import types
import wave
from pathlib import Path

import numpy as np
import pytest

from app.services import brand_icons as bi
from app.services import rebrand_media as media
from app.services import vertex_client
from app.services.rebrand_harness import words as W

FIX = Path(__file__).parent / "fixtures" / "icons_words"
STUDIO = Path(__file__).resolve().parents[1] / "data" / "yas_producer" / "talking-head-v2" / "rebrand" / "_studio"
SR = W.SR


@pytest.fixture(autouse=True)
def _no_paid_calls(monkeypatch):
    async def refuse(*_a, **_k):
        raise AssertionError("a paid model call in a test")

    monkeypatch.setattr(vertex_client, "gemini_multimodal", refuse)
    monkeypatch.setattr(vertex_client, "gemini_image", refuse)


@pytest.fixture(autouse=True)
def _fresh_whisper(monkeypatch):
    """Each test starts with no Whisper loaded or failed."""
    monkeypatch.setattr(W, "_ASR", {"pipe": None, "error": None, "lasting": False, "failed_at": 0.0})


# ---------------------------------------------------------------------------
# Synthetic speech: tone bursts where the "words" are
# ---------------------------------------------------------------------------

def bursts(spans: list[tuple[float, float]], dur: float, *, noise: float = 0.001) -> np.ndarray:
    """Silence (with a little noise) and a 220 Hz tone over each span."""
    rng = np.random.default_rng(7)
    x = rng.normal(0.0, noise, int(dur * SR)).astype(np.float32)
    t = np.arange(len(x)) / SR
    for a, b in spans:
        m = (t >= a) & (t < b)
        x[m] += 0.3 * np.sin(2 * math.pi * 220 * t[m]).astype(np.float32)
    return x


def chunk(text: str, s: float, e: float) -> dict:
    return {"text": " " + text, "timestamp": [s, e]}


def said(body: dict) -> list[dict]:
    return [w for w in body["words"] if w["said"]]


def by_text(body: dict, text: str, n: int = 0) -> dict:
    return [w for w in body["words"] if w["text"].lower() == text.lower()][n]


# ---------------------------------------------------------------------------
# Tokens and alignment
# ---------------------------------------------------------------------------

def test_tokens_split_hyphens_keep_apostrophes_and_digits():
    toks = W.script_tokens("42 Abu Dhabi: It's a tuition-free, peer-learning school. 24/7!")
    assert [t["text"] for t in toks] == ["42", "Abu", "Dhabi", "It's", "a", "tuition", "free", "peer",
                                         "learning", "school", "24", "7"]
    assert [t["norm"] for t in toks][:4] == ["42", "abu", "dhabi", "its"]
    assert W.norm("Café") == "cafe" and W.norm("-free,") == "free"


def test_asr_tokens_split_a_multiword_chunk_by_letters_and_flag_zero_length():
    toks = W.asr_tokens([chunk("tuition-free,", 1.0, 2.0), chunk("A", 3.32, 3.32)])
    assert [t["norm"] for t in toks] == ["tuition", "free", "a"]
    assert toks[0]["s"] == 1.0 and 1.5 < toks[1]["s"] < 1.7        # "tuition" is 7 of 11 letters
    assert toks[1]["punct"] == ","
    assert toks[2]["zero"] and not toks[0]["zero"]


def test_alignment_maps_uncoated_to_the_scripts_uncoded():
    script = "Your future becomes uncoded: a school."
    x = bursts([(0.2, 0.5), (0.6, 1.0), (1.1, 1.5), (1.6, 2.2), (2.5, 2.6), (2.7, 3.2)], 4.0)
    chunks = [chunk("Your", 0.2, 0.5), chunk("future", 0.6, 1.0), chunk("becomes", 1.1, 1.5),
              chunk("uncoated.", 1.6, 2.2), chunk("A", 2.5, 2.6), chunk("school.", 2.7, 3.2)]
    body = W.build_words(chunks, script, x, 4.0)
    assert body["source"] == "whisper"
    w = by_text(body, "uncoded")
    assert w["said"] and w["char"] == script.index("uncoded") and abs(w["s"] - 1.6) <= 0.02
    assert "uncoated" not in body["transcript"]
    assert body["transcript"] == "Your future becomes uncoded: a school."


def test_a_script_word_not_said_stays_greyed_and_a_heard_word_is_kept():
    script = "You learn by building real projects."
    x = bursts([(0.2, 0.5), (0.6, 0.9), (1.3, 1.8), (1.9, 2.1), (2.2, 2.5), (2.6, 3.2)], 3.6)
    # "by" is skipped; "cool" was said but is not in the script.
    chunks = [chunk("You", 0.2, 0.5), chunk("learn", 0.6, 0.9), chunk("building", 1.3, 1.8),
              chunk("cool", 1.9, 2.1), chunk("real", 2.2, 2.5), chunk("projects.", 2.6, 3.2)]
    body = W.build_words(chunks, script, x, 3.6)
    assert body["source"] == "whisper"
    by = by_text(body, "by")
    assert not by["said"] and by["tchar"] is None
    assert by_text(body, "learn")["s"] < by["s"] < by_text(body, "building")["s"]
    cool = by_text(body, "cool")
    assert cool["said"] and cool["char"] is None and cool["tchar"] is not None
    assert "cool" in body["transcript"] and " by " not in body["transcript"]
    # The script's words that were not said count against the match.
    assert body["matched"] == pytest.approx(5 / 6, abs=1e-3)


def test_transcript_offsets_point_at_their_words_and_keep_the_scripts_punctuation():
    fx = json.loads((FIX / "815.json").read_text(encoding="utf-8"))
    body = _replay(fx)
    t = body["transcript"]
    assert t == fx["dialogue"]
    for w in said(body):
        assert t[w["tchar"]: w["tchar"] + len(w["text"])] == w["text"]
    # The keyword table runs on it: no teacher on "without teachers".
    icons = [m.icon for m in bi.find_mentions(t)]
    assert "chalkboard-teacher" not in icons and "code" in icons and "piggy-bank" in icons
    assert W.word_at_tchar(body, t.index("coding") + 2) == by_text(body, "coding")["w"]


# ---------------------------------------------------------------------------
# Snapping onsets to the envelope
# ---------------------------------------------------------------------------

def test_onsets_snap_to_the_first_rise_after_a_pause():
    script = "Open peer learning now."
    # Words at 0.30, 1.00 ("peer" after a pause), 1.60, 2.40.
    x = bursts([(0.30, 0.80), (1.00, 1.45), (1.60, 2.20), (2.40, 2.90)], 3.5)
    chunks = [chunk("Open", 0.30, 0.80), chunk("peer", 0.88, 1.40),      # Whisper 0.12 early
              chunk("learning", 1.60, 2.20), chunk("now.", 2.40, 2.90)]
    body = W.build_words(chunks, script, x, 3.5)
    peer = by_text(body, "peer")
    assert abs(peer["s"] - 1.00) <= 0.02
    assert abs(by_text(body, "learning")["s"] - 1.60) <= 0.02


def test_a_rise_outside_the_window_or_across_the_next_word_is_not_used():
    script = "One two three."
    x = bursts([(0.30, 0.60), (1.00, 1.40), (1.50, 2.00)], 2.6)
    # "two": Whisper says 0.70, the speech rises at 1.00 (+0.30, outside +0.18).
    # "three": Whisper says 1.45 but "two" is still sounding; the rise at 1.50
    # would be kept (+0.05) only if it stays clear of its neighbours.
    chunks = [chunk("One", 0.30, 0.60), chunk("two", 0.70, 1.40), chunk("three.", 1.45, 2.0)]
    body = W.build_words(chunks, script, x, 2.6)
    assert by_text(body, "two")["s"] == pytest.approx(0.70, abs=1e-6)
    assert abs(by_text(body, "three")["s"] - 1.50) <= 0.02
    onsets = [w["s"] for w in said(body)]
    assert onsets == sorted(onsets)


def test_a_zero_length_word_opening_a_clause_starts_after_the_longest_pause():
    script = "No teachers, building real projects."
    x = bursts([(0.2, 0.5), (0.6, 1.3), (1.9, 2.3), (2.4, 2.8), (2.9, 3.6)], 4.0)
    chunks = [chunk("No", 0.2, 0.5), chunk("teachers.", 0.6, 1.8), chunk("Building", 2.4, 2.4),
              chunk("real", 2.4, 2.8), chunk("projects.", 2.9, 3.6)]
    body = W.build_words(chunks, script, x, 4.0)
    assert abs(by_text(body, "building")["s"] - 1.9) <= 0.02
    assert by_text(body, "teachers")["e"] <= 1.35                       # ends at its pause, not 1.8


def test_a_zero_length_word_mid_clause_goes_between_its_neighbours():
    script = "We build real things."
    x = bursts([(0.2, 2.8)], 3.2)                                        # one long voiced run
    chunks = [chunk("We", 0.2, 0.4), chunk("build", 1.0, 1.0), chunk("real", 1.4, 1.8),
              chunk("things.", 2.0, 2.8)]
    body = W.build_words(chunks, script, x, 3.2)
    s = by_text(body, "build")["s"]
    assert 0.2 < s < 1.4


def test_the_envelope_is_two_hundred_bins_and_the_voiced_span_is_measured():
    x = bursts([(1.0, 2.0)], 4.0)
    body = W.build_words(None, "Hello there.", x, 4.0)
    assert len(body["envelope"]) == 200 and max(body["envelope"]) == 1.0
    assert body["envelope"][0] < 0.1 and body["envelope"][75] > 0.9
    assert body["voiced"][0] == pytest.approx(1.0, abs=0.03)
    assert body["voiced"][1] == pytest.approx(2.0, abs=0.03)


# ---------------------------------------------------------------------------
# Validation and the estimate
# ---------------------------------------------------------------------------

def _line_words(n: int, t0: float = 0.5, dt: float = 0.4) -> tuple[str, list[dict], np.ndarray, float]:
    names = [f"word{chr(97 + k)}" for k in range(n)]
    script = " ".join(names) + "."
    chunks = [chunk(nm, t0 + k * dt, t0 + k * dt + dt * 0.8) for k, nm in enumerate(names)]
    dur = t0 + n * dt + 0.5
    x = bursts([(t0 + k * dt, t0 + k * dt + dt * 0.8) for k in range(n)], dur)
    return script, chunks, x, dur


def test_fewer_than_seventy_percent_matched_falls_back_to_the_estimate():
    script, chunks, x, dur = _line_words(10)
    body = W.build_words(chunks[:6], script, x, dur)
    assert body["source"] == "estimate" and "60%" in body["note"]
    assert all(w["said"] for w in body["words"]) and len(body["words"]) == 10


def test_onsets_out_of_order_or_outside_the_clip_fall_back():
    script, chunks, x, dur = _line_words(6)
    swapped = [dict(c) for c in chunks]
    swapped[3]["timestamp"] = [0.2, 0.3]
    body = W.build_words(swapped, script, x, dur)
    assert body["source"] == "estimate" and "order" in body["note"]
    late = [dict(c) for c in chunks]
    late[-1]["timestamp"] = [dur + 2.0, dur + 2.5]
    body = W.build_words(late, script, x, dur)
    assert body["source"] == "estimate"


def test_timing_that_drifts_a_second_from_the_line_falls_back():
    script, chunks, x, dur = _line_words(8, t0=0.3, dt=0.2)
    far = [{"text": c["text"], "timestamp": [c["timestamp"][0] + 5.0, c["timestamp"][1] + 5.0]}
           for c in chunks]
    body = W.build_words(far, script, x, 9.0)
    assert body["source"] == "estimate" and "drifts" in body["note"]


def test_the_estimate_spreads_the_line_over_the_voiced_span():
    script = "Free code for everyone."
    x = bursts([(1.0, 3.0)], 4.0)
    body = W.build_words(None, script, x, 4.0)
    assert body["source"] == "estimate"
    free, code = by_text(body, "free"), by_text(body, "code")
    assert free["s"] == pytest.approx(1.0, abs=0.03)
    assert code["s"] == pytest.approx(1.0 + script.index("code") / len(script) * 2.0, abs=0.03)
    assert body["transcript"] == script


# ---------------------------------------------------------------------------
# The real 806 / 815 onsets (Whisper's real answer + the clips' real envelope)
# ---------------------------------------------------------------------------

# Hand-labelled 2026-09-27 from each clip's RMS envelope (the rise after the
# closure of a plosive, the end of a pause); words said inside continuous
# speech keep Whisper's onset.
LABELS = {
    806: [("42", 0, 0.09), ("where", 0, 1.31), ("future", 0, 1.74), ("in", 0, 2.22), ("tech", 0, 2.52),
          ("begins", 0, 2.78), ("It's", 0, 3.78), ("a", 0, 3.91), ("tuition", 0, 4.01), ("school", 0, 4.55),
          ("where", 1, 4.85), ("learn", 0, 5.32), ("building", 0, 6.01), ("projects", 0, 6.48)],
    815: [("future", 0, 1.44), ("a", 0, 3.26), ("tuition", 0, 3.35), ("peer", 0, 4.24), ("coding", 0, 4.68),
          ("teachers", 0, 5.58), ("building", 0, 6.61), ("projects", 0, 7.00)],
}


def _replay(fx: dict) -> dict:
    env = W.shape_envelope(fx["env"]["rms"], fx["env"]["thr"], fx["env"]["peak"], fx["env"]["voiced"])
    return W.build_words(fx["chunks"], fx["dialogue"], None, fx["duration"], env=env)


@pytest.mark.parametrize("rid", [806, 815])
def test_real_onsets_match_the_hand_labels(rid):
    fx = json.loads((FIX / f"{rid}.json").read_text(encoding="utf-8"))
    body = _replay(fx)
    assert body["source"] == "whisper" and body["matched"] == 1.0
    for word, n, ref in LABELS[rid]:
        assert abs(by_text(body, word, n)["s"] - ref) <= 0.15, (word, by_text(body, word, n)["s"], ref)
    onsets = [w["s"] for w in said(body)]
    assert onsets == sorted(onsets)


def test_real_815_fixes_whispers_known_slips():
    """"peer" read inside a silence (the plosive's burst is at 4.24), "a" and
    "building" read early across a pause, a zero-length "school"
    (5.36-5.36) and "uncoated" for the script's "uncoded"."""
    fx = json.loads((FIX / "815.json").read_text(encoding="utf-8"))
    raw = {c["text"].strip(" ,.").lower(): c["timestamp"] for c in fx["chunks"]}
    assert raw["peer"][0] == 4.12 and raw["school"] == [5.36, 5.36] and "uncoated" in raw
    body = _replay(fx)
    assert by_text(body, "peer")["s"] == pytest.approx(4.24, abs=0.02)
    assert by_text(body, "a")["s"] == pytest.approx(3.26, abs=0.02)          # Whisper 3.18
    assert by_text(body, "building")["s"] == pytest.approx(6.61, abs=0.02)   # Whisper 6.48
    school = by_text(body, "school")["s"]
    assert by_text(body, "coding")["s"] < school < by_text(body, "without")["s"]
    assert by_text(body, "uncoded")["said"]


# ---------------------------------------------------------------------------
# scene_words: the cache and the model
# ---------------------------------------------------------------------------

SCRIPT = "Free code for everyone."


@pytest.fixture
def clip(tmp_path, monkeypatch):
    """A 'base clip' whose bytes key the cache, with fake audio behind it."""
    path = tmp_path / "scene0.mp4"
    path.write_bytes(b"not really a video")
    x = bursts([(0.5, 0.9), (1.0, 1.4), (1.5, 1.8), (1.9, 2.6)], 3.0)
    monkeypatch.setattr(W, "load_audio", lambda p: x)
    monkeypatch.setattr(media, "probe_duration", lambda p: 3.0)
    return path


def _fake_transcribe(calls: list):
    def run(x):
        calls.append(len(x))
        return [chunk("Free", 0.5, 0.9), chunk("code", 1.0, 1.4), chunk("for", 1.5, 1.8),
                chunk("everyone.", 1.9, 2.6)]
    return run


def test_scene_words_is_cached_and_a_hit_makes_no_model_call(clip, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(W, "transcribe", _fake_transcribe(calls))
    a = W.scene_words(clip, SCRIPT, tmp_path / "icons")
    assert a["source"] == "whisper" and len(calls) == 1
    assert (tmp_path / "icons" / "words.json").is_file()
    b = W.scene_words(clip, SCRIPT, tmp_path / "icons")
    assert len(calls) == 1 and b == a
    assert a["model"]["revision"] == W.WHISPER_REVISION and a["digest"] == W.words_digest(a)


def test_the_cache_key_follows_the_clip_the_dialogue_the_revision_and_transformers(clip, monkeypatch):
    k0 = W.cache_key(clip, SCRIPT)
    assert W.cache_key(clip, SCRIPT + " Really.") != k0
    monkeypatch.setattr(W, "WHISPER_REVISION", "0" * 40)
    k1 = W.cache_key(clip, SCRIPT)
    assert k1 != k0
    monkeypatch.setattr(W, "transformers_version", lambda: "4.30.0")
    assert W.cache_key(clip, SCRIPT) != k1
    clip.write_bytes(b"another take")
    assert W.cache_key(clip, SCRIPT) not in (k0, k1)


def test_a_model_that_did_not_load_is_not_cached_but_a_failed_selftest_is(clip, tmp_path, monkeypatch):
    def down(_x):
        raise W.WordTimingUnavailable("word timing unavailable: no network", lasting=False)

    monkeypatch.setattr(W, "transcribe", down)
    body = W.scene_words(clip, SCRIPT, tmp_path / "a")
    assert body["source"] == "estimate" and "no network" in body["note"]
    assert not (tmp_path / "a" / "words.json").exists()

    def old(_x):
        raise W.WordTimingUnavailable("word timing unavailable: sentences", lasting=True)

    monkeypatch.setattr(W, "transcribe", old)
    body = W.scene_words(clip, SCRIPT, tmp_path / "b")
    assert body["source"] == "estimate" and (tmp_path / "b" / "words.json").is_file()


def test_the_selftest_wants_word_sized_chunks():
    assert W.selftest_chunks([chunk("A", 0, 0.1), chunk("tuition", 0.1, 0.3), chunk("-free,", 0.3, 0.8)])[0]
    ok, why = W.selftest_chunks([chunk("A tuition-free, peer-learning coding school.", 0, 2.0)])
    assert not ok and "chunks" in why
    ok, why = W.selftest_chunks([chunk("a b c", 0, 1), chunk("d", 1, 2), chunk("e", 2, 3)])
    assert not ok and "sentences" in why
    with wave.open(str(W.SELFTEST_WAV), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, SR)
        assert w.getnframes() == 2 * SR


def test_the_image_prefetches_the_revision_words_pins_and_transformers_is_pinned():
    root = Path(__file__).resolve().parents[1]
    docker = (root / "Dockerfile").read_text(encoding="utf-8")
    assert f"snapshot_download('{W.WHISPER_MODEL}', revision='{W.WHISPER_REVISION}'" in docker
    reqs = (root / "requirements.txt").read_text(encoding="utf-8").splitlines()
    assert any(r.startswith("transformers>=5") for r in reqs)


def _fake_transformers(monkeypatch, make):
    mod = types.ModuleType("transformers")
    mod.pipeline = make
    monkeypatch.setitem(sys.modules, "transformers", mod)


def test_a_transformers_that_returns_sentences_is_word_timing_unavailable(monkeypatch):
    made = []

    def make(task, **kw):
        made.append(kw)
        return lambda inp, **k: {"text": "x", "chunks": [{"text": " A tuition-free, peer-learning coding.",
                                                          "timestamp": (0.0, 2.0)}]}

    _fake_transformers(monkeypatch, make)
    with pytest.raises(W.WordTimingUnavailable) as e:
        W.transcribe(np.zeros(SR, np.float32))
    assert e.value.lasting and "unavailable" in str(e.value)
    assert made[0]["revision"] == W.WHISPER_REVISION and made[0]["device"] == "cpu"
    with pytest.raises(W.WordTimingUnavailable):                   # remembered: no second load
        W.transcribe(np.zeros(SR, np.float32))
    assert len(made) == 1


def test_a_failed_load_is_retried_only_after_the_backoff(monkeypatch):
    made = []

    def make(task, **kw):
        made.append(1)
        raise OSError("no route to the hub")

    _fake_transformers(monkeypatch, make)
    for _ in range(2):
        with pytest.raises(W.WordTimingUnavailable) as e:
            W.transcribe(np.zeros(SR, np.float32))
        assert not e.value.lasting
    assert len(made) == 1
    W._ASR["failed_at"] -= W.LOAD_RETRY_SEC + 1
    with pytest.raises(W.WordTimingUnavailable):
        W.transcribe(np.zeros(SR, np.float32))
    assert len(made) == 2


def test_a_word_timing_pipeline_passes_the_selftest_and_is_kept(monkeypatch):
    runs = []

    def make(task, **kw):
        def pipe(inp, **k):
            runs.append(k)
            return {"text": "x", "chunks": [chunk("A", 0, 0.1), chunk("tuition", 0.1, 0.3),
                                            chunk("-free,", 0.3, 0.8)]}
        return pipe

    _fake_transformers(monkeypatch, make)
    got = W.transcribe(np.zeros(SR, np.float32))
    assert [c["text"] for c in got] == [" A", " tuition", " -free,"]
    assert runs[0] == {"return_timestamps": "word"} and W.whisper_ready() is True
    W.transcribe(np.zeros(SR * 40, np.float32))
    assert runs[-1]["chunk_length_s"] == 30                              # long clips are chunked


# ---------------------------------------------------------------------------
# The real model on the real scenes (skipped unless cached here)
# ---------------------------------------------------------------------------

def _whisper_cached() -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache

        got = try_to_load_from_cache(W.WHISPER_MODEL, "model.safetensors", revision=W.WHISPER_REVISION)
        return isinstance(got, str)
    except Exception:  # noqa: BLE001
        return False


def _scene(rid: int):
    return next((STUDIO / str(rid)).glob("scene_0_*/base/scene0.mp4"), None)


@pytest.mark.skipif(not (_whisper_cached() and _scene(806) and _scene(815)),
                    reason="whisper-base.en not cached here, or the studio scenes are not on disk")
@pytest.mark.parametrize("rid", [806, 815])
def test_real_whisper_on_the_studio_scenes(rid, tmp_path):
    fx = json.loads((FIX / f"{rid}.json").read_text(encoding="utf-8"))
    body = W.scene_words(_scene(rid), fx["dialogue"], tmp_path)
    assert body["source"] == "whisper", body["note"]
    for word, n, ref in LABELS[rid]:
        assert abs(by_text(body, word, n)["s"] - ref) <= 0.15, (word, by_text(body, word, n)["s"], ref)
    # Deterministic: the same words as the fixture replay.
    assert body["digest"] == _replay(fx)["digest"]
