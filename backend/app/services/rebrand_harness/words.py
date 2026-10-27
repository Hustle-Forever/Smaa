"""What the speaker actually SAYS in a scene, word by word, and when.

An icon is timed to the word that names its idea, so the words need real
onsets, and the icon director needs the words that were SAID (a Veo take
skips a script word now and then, or says one of its own).

  scene_words(base_clip, dialogue, work_dir)   the scene's Words, cached as
                                               work_dir/words.json
  carried(words, key=, duration=)              the same Words for the clip
                                               cut short after its last word
                                               (a Veo tail flash trimmed)

Words = {source: "whisper"|"estimate",
         words: [{w, text, s, e, char, said, tchar}],
         voiced: [t0, t1] | None, envelope: [200 floats], transcript,
         duration, matched, note, digest, key, version, model}

  w       the word's index in `words` (script words not said stay in the
          list, said: false, so the timeline can grey them)
  text    the script's spelling when the word is in the script ("uncoded",
          never Whisper's "uncoated"); Whisper's own words otherwise
  s, e    onset and end, seconds on the clip (e is the next word's onset, or
          the start of a pause after the word)
  char    the word's offset in the SCRIPT line (None for a word only Whisper
          heard)
  tchar   the word's offset in `transcript` (None when not said)
  transcript  the said words joined with the script's punctuation: what the
          keyword table and the negation rules run on (brand_icons
          find_mentions / negated_in_clause), never the script

How the onsets are found
  1. Local Whisper (openai/whisper-base.en, revision-pinned, CPU, greedy,
     transformers' pipeline with return_timestamps="word"). Loaded once per
     process and self-tested on a bundled two-second clip: fewer than three
     word-sized chunks back means this transformers cannot time words (4.30
     returns whole sentences), so the scene falls back to the estimate and
     says why ("word timing unavailable").
  2. Aligned to the script with difflib on normalised tokens (lower case, no
     punctuation, hyphens split, digits kept). Equal runs map 1:1, as do
     equal-length replacements that still look alike ("uncoded"/"uncoated");
     a compound split differently ("peer learning"/"peerlearning") maps by
     its letters.
  3. Onsets only: Whisper's word ENDS drift into pauses.
  4. Each onset snaps to the first rise of the RMS envelope (20 ms windows,
     10 ms hop, brand_icons.voiced_span's threshold) that follows at least
     60 ms below the threshold, within [-0.05, +0.18] s of Whisper's onset,
     and only when the snap keeps the words in order. Whisper tends to start
     a word early; a plosive's closure ("peer", "tech") is the silence
     before its real burst.
  5. A zero-length Whisper word ("A" 3.32-3.32, "Building" 6.84-6.84) has no
     onset: it is filled in. A word that opens a clause (after , . : ; …)
     starts where the speech resumes after the longest pause between its
     neighbours; any other is placed between its neighbours by its place in
     the script.
  6. Checked: onsets in order and inside the clip, at least 70% of the script
     matched, and the median distance from the char-linear estimate under a
     second. Otherwise the whole scene uses the estimate (the line's
     characters spread over the voiced span, as brand_icons.plan_scene does).

Cached by the base clip's bytes, the dialogue, the model and its revision,
and the transformers version. A model that could not be LOADED (no network)
is not cached: the next call tries again (at most every LOAD_RETRY_SEC).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import statistics
import subprocess
import threading
import time
import unicodedata
import wave
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("archivist.rebrand.harness.words")

# Bumped whenever the words a clip yields can change for the same inputs.
WORDS_VERSION = 1
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "openai/whisper-base.en")
WHISPER_REVISION = os.getenv("WHISPER_REVISION", "911407f4214e0e1d82085af863093ec0b66f9cd6")
WHISPER_PATH = os.getenv("WHISPER_PATH", "")          # a local model folder overrides the download
SELFTEST_WAV = Path(__file__).resolve().parents[2] / "assets" / "whisper_selftest.wav"

SR = 16000
WIN = 320                  # 20 ms RMS windows...
HOP = 160                  # ...every 10 ms
STEP = HOP / SR
# The snap (4. above).
SNAP_EARLY = 0.05
SNAP_LATE = 0.18
QUIET_MIN = 0.06           # this long below the threshold before a rise
RISE_HOLD = 0.03           # a "rise" stays up this long (a 10 ms click is not speech)
ORDER_ROOM = 0.02          # a snapped onset keeps this clear of its neighbours
END_PAUSE = 0.15           # a pause this long after a word ends it
# The check (6. above).
MIN_MATCHED = 0.70
MAX_DRIFT = 1.0
ENVELOPE_BINS = 200
LOAD_RETRY_SEC = 600
ZERO_LEN = 0.02            # a Whisper word shorter than this has no onset

_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
_CLAUSE_MARK = re.compile(r"[.?!;:,—–]")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def norm(token: str) -> str:
    """A token as alignment compares it: lower case, accents and
    punctuation gone ("It's" -> "its", "Café" -> "cafe")."""
    t = unicodedata.normalize("NFKD", (token or "").lower())
    return "".join(ch for ch in t if ch.isalnum() and not unicodedata.combining(ch))


def script_tokens(text: str) -> list[dict]:
    """The script's words: [{norm, text, start, end}] (hyphens and slashes
    split a word; an apostrophe does not)."""
    out = []
    for m in _WORD.finditer(text or ""):
        n = norm(m.group())
        if n:
            out.append({"norm": n, "text": m.group(), "start": m.start(), "end": m.end()})
    return out


def asr_tokens(chunks: list[dict]) -> list[dict]:
    """Whisper's word chunks as tokens: [{norm, text, s, e, zero, punct}].
    A chunk holding several words ("tuition-free") is split, its time shared
    by letters; `punct` is what follows a token inside its chunk (its
    trailing "." or ","); `zero` marks a word with no usable onset."""
    out = []
    for c in chunks or []:
        text = str(c.get("text") or "")
        ts = c.get("timestamp") or (None, None)
        s = ts[0] if len(ts) > 0 else None
        e = ts[1] if len(ts) > 1 else None
        if s is None:
            continue
        s = float(s)
        e = float(e) if e is not None else s
        found = [(m, norm(m.group())) for m in _WORD.finditer(text)]
        found = [(m, n) for m, n in found if n]
        if not found:
            continue
        zero = (e - s) < ZERO_LEN
        total = sum(len(n) for _, n in found)
        acc = 0
        for k, (m, n) in enumerate(found):
            nxt = found[k + 1][0].start() if k + 1 < len(found) else len(text)
            ts_k = s + (acc / total) * (e - s) if total else s
            acc += len(n)
            te_k = s + (acc / total) * (e - s) if total else e
            out.append({"norm": n, "text": m.group(), "s": round(ts_k, 3), "e": round(te_k, 3),
                        "zero": zero, "punct": text[m.end():nxt]})
    return out


# ---------------------------------------------------------------------------
# The audio
# ---------------------------------------------------------------------------

def load_audio(path: Path) -> Optional[Any]:
    """The clip's first audio stream as float32 mono at SR, or None when it
    has none."""
    import numpy as np

    from .. import rebrand_media as media

    if not media.has_audio(path):
        return None
    proc = subprocess.run(
        [media.ffmpeg_exe(), "-v", "error", "-i", str(path), "-map", "0:a:0",
         "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
        capture_output=True, timeout=120,
    )
    x = np.frombuffer(proc.stdout, np.int16).astype(np.float32) / 32768.0
    return x if len(x) else None


def envelope(x) -> dict:
    """The clip's loudness: {rms (10 ms hop), thr, voiced (t0, t1) | None,
    on (cleaned voiced flags), quiet [(from, rise)]}.

    The threshold and voiced span are brand_icons.voiced_span's (20 ms
    windows, 12% of the 95th percentile, at least 0.006; no speech when
    that percentile is under 0.01). `on` drops voiced blips shorter than
    RISE_HOLD; `quiet` lists every run at least QUIET_MIN long below the
    threshold, with the time the speech rises again at its end."""
    import numpy as np

    x = np.asarray(x if x is not None else [], dtype=np.float32)
    n20 = len(x) // WIN
    if n20 < 5 or len(x) < WIN:
        return shape_envelope(np.zeros(0, np.float32), 1.0, 0.0, None)
    rms20 = np.sqrt((x[: n20 * WIN].reshape(n20, WIN) ** 2).mean(axis=1))
    peak = float(np.percentile(rms20, 95))
    thr = max(0.006, 0.12 * peak)
    voiced = None
    if peak >= 0.01:
        on20 = np.flatnonzero(rms20 > thr)
        if len(on20):
            voiced = (float(on20[0] * WIN / SR), float((on20[-1] + 1) * WIN / SR))
    frames = np.lib.stride_tricks.sliding_window_view(x, WIN)[::HOP]
    rms = np.sqrt((frames.astype(np.float32) ** 2).mean(axis=1))
    return shape_envelope(rms, thr, peak, voiced)


def shape_envelope(rms, thr: float, peak: float, voiced: Optional[tuple[float, float]]) -> dict:
    """envelope()'s body from its measurements: the 10 ms RMS, the
    threshold, the 95th percentile and the voiced span (a test replays a
    real clip's envelope from these without its audio)."""
    import numpy as np

    rms = np.asarray(rms, dtype=np.float32)
    voiced = tuple(voiced) if voiced else None
    on = rms > thr
    hold = max(1, int(round(RISE_HOLD / STEP)))
    # Voiced blips shorter than RISE_HOLD are not speech resuming.
    i, n = 0, len(on)
    while i < n:
        if on[i]:
            j = i
            while j < n and on[j]:
                j += 1
            if j - i < hold:
                on[i:j] = False
            i = j
        else:
            i += 1
    quiet = []
    i = 0
    need = int(round(QUIET_MIN / STEP))
    while i < n:
        if not on[i]:
            j = i
            while j < n and not on[j]:
                j += 1
            if j - i >= need and j < n:
                quiet.append((round(i * STEP, 3), round(j * STEP, 3)))
            i = j
        else:
            i += 1
    return {"rms": rms, "thr": thr, "voiced": voiced, "on": on, "quiet": quiet, "peak": peak}


def envelope_bins(env: dict, duration: float, bins: int = ENVELOPE_BINS) -> list[float]:
    """The loudness as `bins` values in 0..1 over the clip (the timeline's
    speech lane): each bin's loudest 10 ms against the clip's 95th
    percentile."""
    import numpy as np

    rms = env.get("rms")
    peak = float(env.get("peak") or 0.0)
    if rms is None or not len(rms) or peak <= 0 or duration <= 0:
        return [0.0] * bins
    out = []
    for b in range(bins):
        lo = int(b * duration / bins / STEP)
        hi = max(lo + 1, int((b + 1) * duration / bins / STEP))
        seg = rms[lo:hi]
        out.append(round(float(min(1.0, (seg.max() if len(seg) else 0.0) / peak)), 3))
    return out


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------

def _word(text: str, n: str, *, char=None, cend=None, said=False, s=None, zero=False,
          punct: str = "", matched=False) -> dict:
    return {"text": text, "norm": n, "char": char, "cend": cend, "said": said, "s": s,
            "zero": zero, "punct": punct, "matched": matched}


def align(script: list[dict], heard: list[dict]) -> list[dict]:
    """Script tokens and Whisper tokens as one ordered word list (see the
    module docstring, 2.). Each word: {text, norm, char, cend, said, s,
    zero, punct, matched}; `s` is Whisper's onset (None for a script word
    not said, or a zero-length word's unusable one)."""
    a = [t["norm"] for t in script]
    b = [t["norm"] for t in heard]
    out: list[dict] = []

    def pair(t: dict, h: dict, s: Optional[float] = None) -> dict:
        return _word(t["text"], t["norm"], char=t["start"], cend=t["end"], said=True,
                     s=h["s"] if s is None else s, zero=h["zero"], matched=True)

    def unsaid(t: dict) -> dict:
        return _word(t["text"], t["norm"], char=t["start"], cend=t["end"])

    def only_heard(h: dict) -> dict:
        return _word(h["text"], h["norm"], said=True, s=h["s"], zero=h["zero"], punct=h["punct"])

    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out += [pair(script[i1 + k], heard[j1 + k]) for k in range(i2 - i1)]
        elif tag == "replace" and (i2 - i1) == (j2 - j1):
            for k in range(i2 - i1):
                t, h = script[i1 + k], heard[j1 + k]
                if difflib.SequenceMatcher(None, t["norm"], h["norm"]).ratio() >= 0.5:
                    out.append(pair(t, h))
                else:
                    out += [unsaid(t), only_heard(h)]
        elif tag == "replace" and "".join(a[i1:i2]) == "".join(b[j1:j2]):
            # One compound split two ways: map each script word by its letters.
            spans, acc = [], 0
            for h in heard[j1:j2]:
                spans.append((acc, acc + len(h["norm"]), h))
                acc += len(h["norm"])
            acc = 0
            for t in script[i1:i2]:
                lo, hi, h = next(sp for sp in spans if sp[0] <= acc < sp[1])
                frac = (acc - lo) / max(1, hi - lo)
                out.append(pair(t, h, round(h["s"] + frac * (h["e"] - h["s"]), 3)))
                acc += len(t["norm"])
        else:
            out += [unsaid(script[i]) for i in range(i1, i2)]
            out += [only_heard(heard[j]) for j in range(j1, j2)]
    return out


def _opens_clause(words: list[dict], k: int, script: str) -> bool:
    """Does word k start a clause (a , . : ; — right before it)?"""
    wd = words[k]
    if wd["char"] is not None:
        prev_end = max((p["cend"] for p in words[:k] if p["cend"] is not None), default=None)
        gap = script[prev_end:wd["char"]] if prev_end is not None else ""
        return bool(_CLAUSE_MARK.search(gap))
    prev = next((p for p in reversed(words[:k]) if p["said"]), None)
    return bool(prev and _CLAUSE_MARK.search(prev.get("punct") or ""))


def _between(words: list[dict], k: int, lo_s: float, lo_c, hi_s: float, hi_c, lo_k: int, hi_k: int) -> float:
    """Word k's time between two anchors, by its place in the script (by its
    place in the list when a char is missing)."""
    c = words[k]["char"]
    if c is not None and lo_c is not None and hi_c is not None and hi_c > lo_c:
        f = (c - lo_c) / (hi_c - lo_c)
    else:
        f = (k - lo_k) / max(1, hi_k - lo_k)
    return lo_s + min(1.0, max(0.0, f)) * (hi_s - lo_s)


def fill_unknown(words: list[dict], env: dict, script: str, span: tuple[float, float]) -> None:
    """Give every SAID word with no usable onset (zero-length in Whisper)
    one, in place (module docstring, 5.)."""
    t0, t1 = span
    for k, wd in enumerate(words):
        if not wd["said"] or (wd["s"] is not None and not wd["zero"]):
            continue
        lo = next((j for j in range(k - 1, -1, -1)
                   if words[j]["said"] and words[j]["s"] is not None and not words[j]["zero"]), None)
        hi = next((j for j in range(k + 1, len(words))
                   if words[j]["said"] and words[j]["s"] is not None and not words[j]["zero"]), None)
        lo_s, lo_c = (words[lo]["s"], words[lo]["char"]) if lo is not None else (t0, 0)
        hi_s, hi_c = (words[hi]["s"], words[hi]["char"]) if hi is not None else (t1, len(script))
        lo_k = lo if lo is not None else -1
        hi_k = hi if hi is not None else len(words)
        s = None
        if _opens_clause(words, k, script):
            runs = [q for q in env["quiet"] if q[0] >= lo_s + ORDER_ROOM and q[1] <= hi_s - ORDER_ROOM]
            if runs:
                s = max(runs, key=lambda q: (q[1] - q[0], -q[0]))[1]
        if s is None:
            s = _between(words, k, lo_s, lo_c, hi_s, hi_c, lo_k, hi_k)
        wd["s"] = round(float(s), 3)
        wd["zero"] = False


def snap_onsets(words: list[dict], env: dict) -> int:
    """Snap each said word's onset to the first rise of the envelope after a
    pause (module docstring, 4.), in place; the number moved."""
    rises = [q[1] for q in env["quiet"]]
    said = [w for w in words if w["said"] and w["s"] is not None]
    moved = 0
    prev = None
    for k, wd in enumerate(said):
        s = wd["s"]
        nxt = said[k + 1]["s"] if k + 1 < len(said) else None
        r = next((r for r in rises if s - SNAP_EARLY - 1e-9 <= r <= s + SNAP_LATE + 1e-9), None)
        if r is not None and abs(r - s) > 1e-9 \
                and (prev is None or r > prev + ORDER_ROOM) and (nxt is None or r < nxt - ORDER_ROOM):
            wd["s"] = round(r, 3)
            moved += 1
        prev = wd["s"]
    return moved


def _interpolate_unsaid(words: list[dict], span: tuple[float, float], script: str) -> None:
    t0, t1 = span
    for k, wd in enumerate(words):
        if wd["said"]:
            continue
        lo = next((j for j in range(k - 1, -1, -1) if words[j]["said"]), None)
        hi = next((j for j in range(k + 1, len(words)) if words[j]["said"]), None)
        lo_s, lo_c = (words[lo]["s"], words[lo]["char"]) if lo is not None else (t0, 0)
        hi_s, hi_c = (words[hi]["s"], words[hi]["char"]) if hi is not None else (t1, len(script))
        wd["s"] = round(_between(words, k, lo_s, lo_c, hi_s, hi_c,
                                 lo if lo is not None else -1, hi if hi is not None else len(words)), 3)


def set_ends(words: list[dict], env: dict, duration: float, span: tuple[float, float]) -> None:
    """Each word's end, in place: the next word's onset, or the start of a
    pause of at least END_PAUSE after it; the last said word ends at the
    voiced span's end (or its first pause)."""
    pauses = [q for q in env["quiet"] if q[1] - q[0] >= END_PAUSE]
    for k, wd in enumerate(words):
        s = wd["s"]
        pool = words[k + 1:] if not wd["said"] else [w for w in words[k + 1:] if w["said"]]
        nxt = pool[0]["s"] if pool else max(span[1], s)
        e = nxt
        if wd["said"]:
            p = next((q[0] for q in pauses if s + 0.05 < q[0] < nxt), None)
            if p is not None:
                e = p
        e = min(max(e, s + ZERO_LEN), duration) if duration > 0 else max(e, s + ZERO_LEN)
        wd["e"] = round(float(e), 3)


def char_estimate(char: int, length: int, span: tuple[float, float]) -> float:
    """brand_icons.plan_scene's timing: a character's place in the line,
    spread over the voiced span."""
    t0, t1 = span
    return t0 + (char / max(1, length)) * (t1 - t0)


def validate(words: list[dict], n_script: int, script_len: int, span: tuple[float, float],
             duration: float) -> tuple[bool, str, float]:
    """(ok, why not, matched share) for aligned words (module docstring, 6.)."""
    said = [w for w in words if w["said"]]
    if not said:
        return False, "no words heard", 0.0
    onsets = [w["s"] for w in said]
    if any(s is None for s in onsets):
        return False, "a word has no onset", 0.0
    if any(b < a - 1e-6 for a, b in zip(onsets, onsets[1:])):
        return False, "the words are out of order", 0.0
    if any(s < 0 or s > duration + 1e-6 for s in onsets):
        return False, "a word lies outside the clip", 0.0
    if not n_script:
        return True, "", 1.0
    matched = [w for w in words if w["matched"]]
    share = len(matched) / n_script
    if share < MIN_MATCHED:
        return False, f"only {share:.0%} of the script was heard", share
    drift = statistics.median(abs(w["s"] - char_estimate(w["char"], script_len, span)) for w in matched)
    if drift >= MAX_DRIFT:
        return False, f"the timing drifts {drift:.1f} s from the line", share
    return True, "", share


def _sep(gap: str, *, last: bool = False) -> str:
    """What goes after a word in the transcript: a joining mark as is
    ("tuition-free", "24/7"), else the gap's strongest clause mark and a
    space, else a space."""
    if gap and not re.search(r"\s", gap) and not _CLAUSE_MARK.search(gap):
        return "" if last else gap
    marks = re.sub(r"[^.?!;:,—–]", "", gap or "")
    hard = marks.replace(",", "")
    mark = (hard[0] if hard else marks[:1]) or ""
    if mark in ("—", "–"):
        mark = " —"
    return mark if last else (mark + " " if mark else " ")


def _gap_after(words: list[dict], wd: dict, nxt: Optional[dict], script: str) -> str:
    """What follows word `wd` before the next said word `nxt`: the script's
    text in between, with any unsaid words' letters taken out (their
    punctuation stays: "a b, c" said without "b" reads "a, c"); Whisper's
    own punctuation after a word only Whisper heard."""
    if wd["char"] is None:
        return wd.get("punct") or ""
    if nxt is not None and nxt["char"] is not None and nxt["char"] >= wd["cend"]:
        end = nxt["char"]
    else:
        end = next((t["char"] for t in words if t["char"] is not None and t["char"] >= wd["cend"]),
                   len(script))
    return re.sub(r"[^\W_]+", "", script[wd["cend"]:end])


def transcript_of(words: list[dict], script: str) -> str:
    """The SAID words joined with the script's punctuation; sets each word's
    `tchar` (None when not said)."""
    parts: list[str] = []
    pos = 0
    said = [w for w in words if w["said"]]
    for w in words:
        w["tchar"] = None
    for idx, wd in enumerate(said):
        wd["tchar"] = pos
        parts.append(wd["text"])
        pos += len(wd["text"])
        last = idx == len(said) - 1
        sep = _sep(_gap_after(words, wd, None if last else said[idx + 1], script), last=last)
        parts.append(sep)
        pos += len(sep)
    return "".join(parts)


def estimate_words(dialogue: str, span: tuple[float, float], duration: float) -> list[dict]:
    """The char-linear fallback: every script word said, at its place in the
    line spread over the voiced span."""
    toks = script_tokens(dialogue)
    L = len(dialogue or "")
    out = []
    for t in toks:
        wd = _word(t["text"], t["norm"], char=t["start"], cend=t["end"], said=True,
                   s=round(char_estimate(t["start"], L, span), 3), matched=True)
        out.append(wd)
    return out


def _span(env: dict, duration: float) -> tuple[float, float]:
    if env.get("voiced"):
        return env["voiced"]
    return (min(0.3, duration), max(min(0.3, duration), duration - 0.3))


def _finish(words: list[dict], source: str, dialogue: str, env: dict, duration: float,
            note: str, matched: float) -> dict:
    span = _span(env, duration)
    set_ends(words, env, duration, span)
    transcript = transcript_of(words, dialogue or "")
    out_words = []
    for k, wd in enumerate(words):
        out_words.append({"w": k, "text": wd["text"], "s": wd["s"], "e": wd["e"], "char": wd["char"],
                          "said": bool(wd["said"]), "tchar": wd["tchar"]})
    body = {
        "version": WORDS_VERSION,
        "source": source,
        "words": out_words,
        "voiced": [round(v, 3) for v in env["voiced"]] if env.get("voiced") else None,
        "envelope": envelope_bins(env, duration),
        "transcript": transcript,
        "duration": round(float(duration), 3),
        "matched": round(float(matched), 3),
        "note": note,
    }
    body["digest"] = words_digest(body)
    return body


def words_digest(body: dict) -> str:
    """What a plan keys the words by: the source and every word's text,
    onset and said flag."""
    rows = [[w["w"], w["text"], round(float(w["s"]), 3), bool(w["said"]), w["char"]]
            for w in body.get("words") or []]
    blob = json.dumps([body.get("source"), rows], separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def carried(body: dict, *, key: str, duration: float) -> dict:
    """The same Words for the same clip cut short at `duration` (a Veo tail
    flash trimmed off after every word said): the words, their timings and
    so their digest unchanged; keyed to the trimmed clip (`key`, its
    cache_key), with its length, and the loudness bins spread over it."""
    out = dict(body)
    out["key"] = key
    was = float(body.get("duration") or 0.0)
    if 0 < duration < was:
        bins = list(body.get("envelope") or [])
        n = len(bins)
        spread = []
        for b in range(n):
            lo = min(n - 1, int(b * duration / was))
            hi = max(lo + 1, int((b + 1) * duration / was))
            spread.append(max(bins[lo:hi]))
        out["envelope"] = spread if n else bins
        voiced = body.get("voiced")
        if voiced and len(voiced) == 2:
            out["voiced"] = [round(min(float(v), duration), 3) for v in voiced]
        out["duration"] = round(float(duration), 3)
    return out


def build_words(chunks: Optional[list[dict]], dialogue: str, audio, duration: float,
                *, note: str = "", env: Optional[dict] = None) -> dict:
    """The pure core of scene_words: Whisper's chunks (None when Whisper did
    not run) + the script + the clip's audio (or its `env`elope, already
    measured) -> Words."""
    env = env if env is not None else envelope(audio)
    duration = float(duration or (len(audio) / SR if audio is not None else 0.0))
    span = _span(env, duration)
    script = script_tokens(dialogue)
    if chunks is not None:
        heard = asr_tokens(chunks)
        if heard:
            words = align(script, heard) if script else [
                _word(h["text"], h["norm"], said=True, s=h["s"], zero=h["zero"], punct=h["punct"])
                for h in heard]
            fill_unknown(words, env, dialogue or "", span)
            snap_onsets(words, env)
            _interpolate_unsaid(words, span, dialogue or "")
            ok, why, share = validate(words, len(script), len(dialogue or ""), span, duration)
            if ok:
                return _finish(words, "whisper", dialogue, env, duration, note, share)
            note = why
            log.info("[words] Whisper's timing not used: %s", why)
        else:
            note = note or "no words heard"
    words = estimate_words(dialogue, span, duration)
    return _finish(words, "estimate", dialogue, env, duration, note, 1.0 if words else 0.0)


# ---------------------------------------------------------------------------
# Whisper
# ---------------------------------------------------------------------------

class WordTimingUnavailable(RuntimeError):
    """Whisper cannot time words here. `lasting`: it will not change until
    the model or transformers does (a failed self-test) — the fallback may
    be cached; otherwise (a failed download) the next call tries again."""

    def __init__(self, message: str, *, lasting: bool):
        super().__init__(message)
        self.lasting = lasting


_ASR: dict[str, Any] = {"pipe": None, "error": None, "lasting": False, "failed_at": 0.0}
# Loading and running share one lock: a pipeline is not promised thread-safe,
# and two scenes' plans may ask at once.
_ASR_LOCK = threading.Lock()


def transformers_version() -> str:
    try:
        from importlib.metadata import version

        return version("transformers")
    except Exception:  # noqa: BLE001 — not installed
        return "none"


def model_id() -> dict:
    return {"id": WHISPER_PATH or WHISPER_MODEL, "revision": "" if WHISPER_PATH else WHISPER_REVISION,
            "transformers": transformers_version()}


def selftest_chunks(chunks: list[dict]) -> tuple[bool, str]:
    """Do these chunks look like WORD timing? At least three chunks, none
    longer than two words (an old transformers returns whole sentences)."""
    sizes = [len(str(c.get("text") or "").split()) for c in chunks or []]
    if len(sizes) < 3:
        return False, f"{len(sizes)} chunks from the self-test clip"
    if any(n > 2 for n in sizes):
        return False, "the self-test came back in sentences, not words"
    return True, ""


def _read_wav(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1 or w.getframerate() != SR:
            raise ValueError(f"{path.name} is not 16-bit mono {SR} Hz")
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768.0


def _run(pipe, x) -> list[dict]:
    # The model's own generation config is already greedy (no num_beams, no
    # sampling). Do not pass generate_kwargs: with transformers 5.8 an
    # explicit {"num_beams": 1} rebuilds the logits processors and moves
    # several words by 0.02-0.4 s (815's "school" stops being zero-length),
    # so the fixtures and the cache would no longer match this path.
    kw: dict[str, Any] = {"return_timestamps": "word"}
    if len(x) / SR > 30:
        kw["chunk_length_s"] = 30
    out = pipe({"raw": x.copy(), "sampling_rate": SR}, **kw)
    return [{"text": str(c.get("text") or ""), "timestamp": list(c.get("timestamp") or (None, None))}
            for c in (out or {}).get("chunks") or []]


def _pipeline():
    """The loaded, self-tested Whisper pipeline (call under _ASR_LOCK)."""
    if _ASR["pipe"] is not None:
        return _ASR["pipe"]
    if _ASR["error"] and (_ASR["lasting"] or time.time() - _ASR["failed_at"] < LOAD_RETRY_SEC):
        raise WordTimingUnavailable(_ASR["error"], lasting=_ASR["lasting"])
    t0 = time.time()
    try:
        from transformers import pipeline

        kw: dict[str, Any] = {"model": WHISPER_PATH or WHISPER_MODEL, "device": "cpu"}
        if not WHISPER_PATH:
            kw["revision"] = WHISPER_REVISION
        pipe = pipeline("automatic-speech-recognition", **kw)
    except Exception as e:  # noqa: BLE001 — no model means the estimate, not a failure
        _ASR.update(error=f"word timing unavailable: the speech model did not load ({str(e)[:160]})",
                    lasting=False, failed_at=time.time())
        log.warning("[words] %s", _ASR["error"])
        raise WordTimingUnavailable(_ASR["error"], lasting=False) from e
    try:
        ok, why = selftest_chunks(_run(pipe, _read_wav(SELFTEST_WAV)))
    except Exception as e:  # noqa: BLE001
        ok, why = False, f"the self-test failed ({str(e)[:160]})"
    if not ok:
        _ASR.update(error=f"word timing unavailable: {why}", lasting=True, failed_at=time.time())
        log.warning("[words] %s (transformers %s)", _ASR["error"], transformers_version())
        raise WordTimingUnavailable(_ASR["error"], lasting=True)
    _ASR.update(pipe=pipe, error=None, lasting=False)
    log.info("[words] Whisper %s loaded and self-tested in %.1fs", kw["model"], time.time() - t0)
    return pipe


def transcribe(x) -> list[dict]:
    """Whisper's word chunks for float32 mono SR audio:
    [{text, timestamp: [s, e]}]. Raises WordTimingUnavailable."""
    with _ASR_LOCK:
        pipe = _pipeline()
        return _run(pipe, x)


def whisper_ready() -> Optional[bool]:
    """True once loaded, False after a failed load or self-test, None when
    not tried yet (the studio's step list)."""
    if _ASR["pipe"] is not None:
        return True
    return False if _ASR["error"] else None


# ---------------------------------------------------------------------------
# The scene
# ---------------------------------------------------------------------------

def cache_key(base_clip: Path, dialogue: str) -> Optional[str]:
    from . import stages as st

    d = st.digest(Path(base_clip))
    if d is None:
        return None
    m = model_id()
    body = json.dumps([WORDS_VERSION, d, hashlib.sha256((dialogue or "").encode("utf-8")).hexdigest(),
                       m["id"], m["revision"], m["transformers"]], separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:20]


def _read(saved: Path) -> Optional[dict]:
    try:
        return json.loads(saved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(saved: Path, body: dict) -> None:
    saved.parent.mkdir(parents=True, exist_ok=True)
    tmp = saved.with_name(saved.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, saved)


def scene_words(base_clip: Path, dialogue: str, work_dir: Path) -> dict:
    """The scene's Words (module docstring), cached as work_dir/words.json.
    Blocking (Whisper runs ~2-5 s per scene on this CPU, plus ~15 s to load
    once per process): call it from a thread."""
    base_clip, work_dir = Path(base_clip), Path(work_dir)
    saved = work_dir / "words.json"
    key = cache_key(base_clip, dialogue)
    if key is not None:
        cached = _read(saved)
        if cached and cached.get("key") == key and cached.get("version") == WORDS_VERSION:
            return cached
    from .. import rebrand_media as media

    audio = load_audio(base_clip) if base_clip.exists() else None
    duration = (media.probe_duration(base_clip) if base_clip.exists() else None) \
        or (len(audio) / SR if audio is not None else 0.0)
    env = envelope(audio)
    chunks, note, keep = None, "", True
    if audio is None:
        note = "the clip has no sound"
    elif env["voiced"] is None:
        note = "no speech in the clip"
    else:
        try:
            chunks = transcribe(audio)
        except WordTimingUnavailable as e:
            note, keep = str(e), e.lasting
    body = build_words(chunks, dialogue, audio, duration, note=note, env=env)
    body["key"] = key
    body["model"] = model_id()
    if key is not None and keep:
        _write(saved, body)
    log.info("[words] %s: %s, %d words, %d said%s", base_clip.name, body["source"], len(body["words"]),
             sum(1 for w in body["words"] if w["said"]), f" ({note})" if note else "")
    return body


# ---------------------------------------------------------------------------
# Reading Words (the director and the icon planner)
# ---------------------------------------------------------------------------

def said_indices(words: dict) -> list[int]:
    """The `w` of every said word, in order."""
    return [w["w"] for w in words.get("words") or [] if w.get("said")]


def word_at_tchar(words: dict, at: int) -> Optional[int]:
    """The said word covering character `at` of the transcript (the word
    that starts at or before it), or None."""
    best = None
    for w in words.get("words") or []:
        tc = w.get("tchar")
        if tc is None:
            continue
        if tc <= at:
            best = w["w"]
        else:
            break
    return best
