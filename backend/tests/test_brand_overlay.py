"""The 42 brand frame: measure the clips, time the cuts, composite the graphics
over footage that is never regenerated.

Everything here is offline: ffmpeg builds synthetic clips, PIL writes the
"graphics", and nothing talks to Remotion, Vertex or the database.
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pytest
from PIL import Image

from app.services import brand_overlay as bo
from app.services import rebrand_media as media
from app.services import rebrand_pipeline as pipeline


def _ffmpeg():
    try:
        return media.ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


pytestmark = pytest.mark.skipif(_ffmpeg() is None, reason="ffmpeg unavailable")

VIOLET = (0x7F, 0x2B, 0xF2)


# ---------------------------------------------------------------------------
# Synthetic media
# ---------------------------------------------------------------------------

def _ff(*args: str) -> None:
    subprocess.run([media.ffmpeg_exe(), "-y", "-v", "error", *args],
                   check=True, capture_output=True)


def _clip(out: Path, *, size: str, rate: int, seconds: float, vf: str = "",
          audio_sec: float = 0.0, src: str = "testsrc2") -> Path:
    """A clip with real structure; `vf` post-processes it (pad, drawbox);
    `audio_sec` > 0 adds a 440 Hz tone of THAT length (may differ from the
    picture, like a real muxed clip)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["-f", "lavfi", "-i", f"{src}=size={size}:rate={rate}:duration={seconds}"]
    if audio_sec:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={audio_sec}"]
    if vf:
        args += ["-vf", vf]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p"]
    if audio_sec:
        args += ["-c:a", "aac", "-b:a", "128k"]
    _ff(*args, str(out))
    return out


def _still(out: Path, w: int, h: int, seed: int = 1) -> Path:
    rng = np.random.default_rng(seed)
    arr = rng.integers(40, 220, size=(h // 4, w // 4, 3), dtype=np.uint8)
    Image.fromarray(arr).resize((w, h), Image.NEAREST).save(out)
    return out


def _src(path: Path, kind: str = "scene", index: int = 0) -> bo.BrandSource:
    return bo.BrandSource(kind=kind, index=index, path=path, is_still=False)


def _px(box: dict, w: int, h: int) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) in canvas pixels."""
    return (round(box["x"] * w), round(box["y"] * h),
            round((box["x"] + box["w"]) * w), round((box["y"] + box["h"]) * h))


def _seg(kind: str, frames: int, start: int, index: int = 0) -> dict:
    return {"kind": kind, "index": index, "from": start, "frames": frames,
            "content": {"x": 0.0, "y": 0.2, "w": 1.0, "h": 0.6}, "layout": "bars"}


def _timeline(*spec: tuple[str, int]) -> list[dict]:
    out, cur = [], 0
    for i, (kind, frames) in enumerate(spec):
        out.append(_seg(kind, frames, cur, index=i))
        cur += frames
    return out


# ---------------------------------------------------------------------------
# Options + text defaults (the arm wrapper)
# ---------------------------------------------------------------------------

def test_options_default_keeps_old_api_and_brand_off():
    o = pipeline.normalize_options({})
    assert o["run_omni"] is True
    # Veo is retired from Rebrand (owner, 2026-09-25): not an option any more.
    assert not any(k == "run_veo" or k.startswith("veo_") for k in o)
    assert o["run_brand"] is False
    assert o["brand_transitions"] == "auto"
    assert o["brand_heading"] is None and o["brand_sub"] is None
    assert o["brand_logo_on_cards"] is False
    assert o["brand_resolution"] == "1080"


def test_options_brand_is_whole_reel_only():
    assert pipeline.normalize_options({"run_brand": True})["run_brand"] is True
    for target in ("intro", "outro", "scene_0", "scene_3"):
        o = pipeline.normalize_options({"run_brand": True, "target": target})
        assert o["target"] == target and o["run_brand"] is False


def test_options_brand_values_are_clamped():
    o = pipeline.normalize_options({
        "run_brand": True, "brand_transitions": "COVER", "brand_heading": "x" * 60,
        "brand_sub": "y" * 200, "brand_resolution": "720p", "brand_logo_on_cards": 1,
    })
    assert o["brand_transitions"] == "cover"
    assert len(o["brand_heading"]) == 40 and len(o["brand_sub"]) == 140
    assert o["brand_resolution"] == "720" and o["brand_logo_on_cards"] is True
    o = pipeline.normalize_options({"brand_transitions": "zoom", "brand_resolution": "4k",
                                    "brand_heading": "   ", "brand_sub": ""})
    assert o["brand_transitions"] == "auto" and o["brand_resolution"] == "1080"
    # "" (and blank) = NO text, which is not the same as None = auto.
    assert o["brand_heading"] == "" and o["brand_sub"] == ""
    assert pipeline.normalize_options({"brand_resolution": 720})["brand_resolution"] == "720"


def test_brand_dims_follow_aspect_and_resolution():
    assert pipeline._brand_dims("9:16", "1080") == (1080, 1920)
    assert pipeline._brand_dims("16:9", "1080") == (1920, 1080)
    assert pipeline._brand_dims("9:16", "720") == (720, 1280)
    assert pipeline._brand_dims("16:9", "720") == (1280, 720)


def test_text_defaults_come_from_the_script():
    run_ctx = {"intro_headline": "  Why 42 has  no teachers ",
               "hook": "Imagine a school where nobody lectures you."}
    opts = pipeline.normalize_options({"run_brand": True})
    # None = auto: the headline and the hook, whitespace collapsed, never cut
    # here — the composition measures them and leaves out what cannot fit.
    assert pipeline._brand_text(opts, run_ctx) == {
        "heading": "Why 42 has no teachers", "sub": "Imagine a school where nobody lectures you."}
    # "" means no text even when the script has some; typed text is kept as is.
    assert pipeline._brand_text({"brand_heading": "", "brand_sub": ""}, run_ctx) is None
    assert pipeline._brand_text({"brand_heading": "Myth vs Fact", "brand_sub": ""}, run_ctx) == \
        {"heading": "Myth vs Fact", "sub": ""}
    assert pipeline._brand_text({"brand_heading": "", "brand_sub": None}, run_ctx) == \
        {"heading": "", "sub": "Imagine a school where nobody lectures you."}
    # A script without either line gives no text at all.
    assert pipeline._brand_text(opts, {}) is None


# The owner's own headlines + hooks (the most recent TH v2 scripts). At 22
# characters 22 of 30 such headlines were cut into fragments; none may be.
REAL_SCRIPTS = [
    ("Your Future is Calling", "42 Abu Dhabi is where your future in tech begins."),
    ("Why I Chose 42 Abu Dhabi", "I joined 42 Abu Dhabi because it's completely free"),
    ("Meet the Team at 42 Abu Dhabi",
     "We're continuously working to make your learning experience effective."),
    ("The Norminette is Roaming?", "I just saw the announcement that the Norminette"),
    ("When Planks Turn Into Laughter", "We both collapsed into laughter"),
    ("Self-Paced Learning at 42 Abu Dhabi", "I joined 42 Abu Dhabi to learn"),
    ("What is 42?", "42 is a global network of tuition-free coding schools."),
    ("Hi everyone", "Hi everyone, I'm Fatima Al Foulathi, CEO of 42 Abu Dhabi."),
    ("Everything You Wanted to Know About the Piscine at 42 Abu Dhabi",
     "The Piscine is a four-week coding bootcamp that decides whether you join 42 Abu "
     "Dhabi, and nobody tells you what it is really like until you are in it."),
]


def test_auto_text_is_whole_or_nothing():
    """The heading is typed in the display face: a fragment of a headline
    ("Why I Chose 42 Abu") reads as a mistake, so it is whole or absent. The
    sub-line is a sentence: past BRAND_SUB_AUTO_MAX it is cut at a word and
    closed with an ellipsis, which reads as intended."""
    opts = pipeline.normalize_options({"run_brand": True})
    for headline, hook in REAL_SCRIPTS:
        t = pipeline._brand_text(opts, {"intro_headline": headline, "hook": hook})
        assert t is not None
        if len(headline) <= pipeline.BRAND_HEADING_AUTO_MAX:
            assert t["heading"] == headline
        else:
            assert t["heading"] == "", t
        if len(hook) <= pipeline.BRAND_SUB_AUTO_MAX:
            assert t["sub"] == hook
        else:
            assert t["sub"].endswith("…") and len(t["sub"]) <= pipeline.BRAND_SUB_AUTO_MAX, t
            kept = t["sub"][:-1]
            assert hook.startswith(kept) and hook[len(kept)] in " ,;:-.", t


def test_cut_at_word():
    assert pipeline._cut_at_word("  a  b c ", 10) == "a b c"
    assert pipeline._cut_at_word("Hello there, general Kenobi", 16) == "Hello there…"
    assert pipeline._cut_at_word("Supercalifragilistic", 5) == ""



def test_text_note_reports_what_the_frame_left_out():
    auto = {"brand_heading": None, "brand_sub": None}
    text = {"heading": "Why I Chose 42 Abu Dhabi", "sub": "It is free."}
    partial = {"status": "partial", "message": "Heading dropped: too long for the letterbox bars.",
               "heading_shown": False, "sub_shown": True}
    note = pipeline._brand_text_note(auto, text, partial)
    assert note.startswith('Auto heading "Why I Chose 42 Abu Dhabi" left out') and "sub-line is shown" in note
    # A typed heading: the composition's own sentence.
    assert pipeline._brand_text_note({"brand_heading": "Why I Chose"}, text, partial) == partial["message"]
    shrunk = {"status": "shrunk", "message": "Text shrunk to 72% to fit the letterbox bars."}
    assert pipeline._brand_text_note(auto, text, shrunk) == shrunk["message"]
    # Drawn as asked, no text, or no report: nothing to say.
    assert pipeline._brand_text_note(auto, text, {"status": "shown", "message": "x"}) == ""
    assert pipeline._brand_text_note(auto, None, partial) == ""
    assert pipeline._brand_text_note(auto, text, None) == ""


def test_running_stage_names_only_the_arms_that_run():
    assert pipeline._running_stage({"run_brand": True}) == "Applying the 42 brand frame"
    s = pipeline._running_stage({"run_brand": True, "run_omni": True})
    assert s == "The 42 brand frame and Omni are working in parallel"
    # Veo is retired: an old option naming it adds nothing.
    assert pipeline._running_stage({"run_omni": True, "run_veo": True}) == "Omni is working"


def test_memory_summary_survives_a_brand_only_run():
    from app.services import rebrand_brief

    arms = {
        "omni": {"status": "skipped"},
        "brand": {"status": "ready", "segments": [{"layout": "bars"}, {"layout": "full"}],
                  "seams": [{"style": "cover"}]},
    }
    embed, lesson = rebrand_brief.summarize_for_memory({"topic": "t"}, {}, arms, None)
    assert "brand: 42 frame" in lesson and "bars/full" in lesson and "cover" in lesson
    assert "42 brand frame" in embed
    _, lesson = rebrand_brief.summarize_for_memory(
        {}, None, arms, {"brand_stars": 4, "notes": "crisp"})
    assert "brand 4/5" in lesson and "crisp" in lesson
    # An older / hand-edited brief with plain strings must not crash either.
    embed, _ = rebrand_brief.summarize_for_memory(
        {}, {"background": "room kept", "transition_in": "wipe"}, {}, None)
    assert "room kept" in embed


# ---------------------------------------------------------------------------
# Seams + props (pure)
# ---------------------------------------------------------------------------

def test_auto_seams_cover_cards_and_glitch_between_scenes():
    segs = _timeline(("intro", 60), ("scene", 240), ("scene", 240), ("outro", 60))
    seams = bo.build_seams(segs, "auto")
    assert [s["style"] for s in seams] == ["cover", "glitch", "cover"]
    assert [s["frame"] for s in seams] == [60, 300, 540]
    assert [(s["from_kind"], s["to_kind"]) for s in seams] == [
        ("intro", "scene"), ("scene", "scene"), ("scene", "outro")]
    assert bo.build_seams(_timeline(("scene", 90)), "auto") == []


def test_seams_downgrade_when_the_frames_cannot_hold_them():
    # cover needs 8 frames before the cut and 11 after.
    assert bo.build_seams(_timeline(("intro", 8), ("scene", 11)))[0]["style"] == "cover"
    assert bo.build_seams(_timeline(("intro", 7), ("scene", 90)))[0]["style"] == "short"
    assert bo.build_seams(_timeline(("intro", 60), ("scene", 10)))[0]["style"] == "short"
    # handoff needs 18 frames of the outgoing scene (nothing of the incoming).
    assert bo.build_seams(_timeline(("scene", 18), ("scene", 1)), "handoff")[0]["style"] == "handoff"
    assert bo.build_seams(_timeline(("scene", 17), ("scene", 90)), "handoff")[0]["style"] == "short"
    # glitch needs 6 frames on each side (the footage frames it glitches).
    assert bo.build_seams(_timeline(("scene", 6), ("scene", 6)))[0]["style"] == "glitch"
    assert bo.build_seams(_timeline(("scene", 5), ("scene", 90)))[0]["style"] == "short"
    assert bo.build_seams(_timeline(("scene", 90), ("scene", 5)))[0]["style"] == "short"
    # short is the floor.
    assert bo.build_seams(_timeline(("intro", 2), ("scene", 2)))[0]["style"] == "short"


def test_explicit_seam_styles_apply_to_every_cut():
    segs = _timeline(("intro", 60), ("scene", 240), ("scene", 240), ("outro", 60))
    assert {s["style"] for s in bo.build_seams(segs, "cover")} == {"cover"}
    assert {s["style"] for s in bo.build_seams(segs, "handoff")} == {"handoff"}
    assert {s["style"] for s in bo.build_seams(segs, "short")} == {"short"}
    assert {s["style"] for s in bo.build_seams(segs, "glitch")} == {"glitch"}
    assert [s["style"] for s in bo.build_seams(segs, "zoom")] == ["cover", "glitch", "cover"]
    # An explicit style still yields to the frame budget.
    tight = _timeline(("scene", 240), ("scene", 6))
    assert bo.build_seams(tight, "cover")[0]["style"] == "short"


def test_props_match_the_contract():
    segs = _timeline(("intro", 60), ("scene", 240), ("outro", 60))
    seams = bo.build_seams(segs)
    p = bo.build_props(segs, seams, width=1080, height=1920,
                       text={"heading": "Myth vs Fact", "sub": ""}, logo_on_cards=False)
    assert set(p) == {"width", "height", "fps", "durationInFrames", "segments", "seams",
                      "text", "logo", "icons", "edge_patterns"}
    assert p["icons"] == [] and p["edge_patterns"] is True
    q = bo.build_props(segs, seams, width=1080, height=1920, edge_patterns=False,
                       icons=[{"icon": "code", "from": 70.0, "frames": "40", "extra": 1}])
    assert q["icons"] == [{"icon": "code", "from": 70, "frames": 40}] and q["edge_patterns"] is False
    assert (p["width"], p["height"], p["fps"], p["durationInFrames"]) == (1080, 1920, 30, 360)
    assert set(p["segments"][0]) == {"kind", "index", "from", "frames", "content", "layout"}
    assert set(p["segments"][0]["content"]) == {"x", "y", "w", "h"}
    assert set(p["seams"][0]) == {"frame", "style", "from_kind", "to_kind"}
    assert p["text"] == {"heading": "Myth vs Fact", "sub": ""}
    assert p["logo"] == {"on_cards": False}
    json.dumps(p)  # must travel as JSON
    assert bo.build_props(segs, seams, width=720, height=1280, text=None)["text"] is None
    assert bo.build_props(segs, seams, width=720, height=1280,
                          text={"heading": " ", "sub": ""})["text"] is None
    assert bo.build_props(segs, seams, width=720, height=1280,
                          logo_on_cards=True)["logo"] == {"on_cards": True}
    broken = [dict(segs[0]), dict(segs[1], **{"from": 61})]
    with pytest.raises(ValueError):
        bo.build_props(broken, [], width=720, height=1280)


def test_layout_rule():
    W, H = 1080, 1920
    assert bo.layout_for({"x": 0, "y": 0.2, "w": 1, "h": 0.6}, W, H) == "bars"
    assert bo.layout_for({"x": 0, "y": 0.10, "w": 1, "h": 0.80}, W, H) == "bars"
    assert bo.layout_for({"x": 0, "y": 0.09, "w": 1, "h": 0.82}, W, H) == "full"
    assert bo.layout_for({"x": 0, "y": 0.2, "w": 1, "h": 0.75}, W, H) == "full"   # bottom 5%
    assert bo.layout_for({"x": 0, "y": 0, "w": 1, "h": 1}, W, H) == "full"
    assert bo.layout_for({"x": 0, "y": 0.2, "w": 1, "h": 0.6}, H, W) == "full"    # landscape


# ---------------------------------------------------------------------------
# Measuring real (synthetic) clips
# ---------------------------------------------------------------------------

CW, CH = 180, 320      # a small 9:16 canvas


def test_content_box_of_a_baked_in_square_band(tmp_path):
    clip = _clip(tmp_path / "sq.mp4", size="180x180", rate=30, seconds=1,
                 vf="pad=180:320:0:70:black")
    box = bo.detect_content_box(_src(clip), CW, CH)
    x0, y0, x1, y1 = _px(box, CW, CH)
    # never smaller than the picture, at most a few rows of edge ringing larger
    assert (x0, x1) == (0, CW)
    assert 67 <= y0 <= 70 and 250 <= y1 <= 253, (y0, y1)
    assert bo.layout_for(box, CW, CH) == "bars"
    # The same clip on a bigger canvas: the box scales with it.
    big = bo.detect_content_box(_src(clip), 360, 640)
    _, by0, _, by1 = _px(big, 360, 640)
    assert 134 <= by0 <= 140 and 500 <= by1 <= 506, (by0, by1)


def test_content_box_of_a_baked_in_3_2_band(tmp_path):
    clip = _clip(tmp_path / "wide.mp4", size="180x120", rate=30, seconds=1,
                 vf="pad=180:320:0:100:black")
    x0, y0, x1, y1 = _px(bo.detect_content_box(_src(clip), CW, CH), CW, CH)
    assert 97 <= y0 <= 100 and 220 <= y1 <= 223, (y0, y1)


def test_full_bleed_clip_has_no_bars(tmp_path):
    clip = _clip(tmp_path / "full.mp4", size="180x320", rate=30, seconds=1)
    box = bo.detect_content_box(_src(clip), CW, CH)
    assert _px(box, CW, CH) == (0, 0, CW, CH)
    assert bo.layout_for(box, CW, CH) == "full"


def test_a_dark_picture_edge_is_not_a_bar(tmp_path):
    # Top and bottom 50 rows are a flat very dark grey (RGB 10 ≈ luma 25), the
    # kind of shadowed ceiling a real shot has. It is picture, not letterbox.
    clip = _clip(tmp_path / "dark.mp4", size="180x320", rate=30, seconds=1,
                 vf="drawbox=x=0:y=0:w=180:h=50:color=0x0a0a0a:t=fill,"
                    "drawbox=x=0:y=270:w=180:h=50:color=0x0a0a0a:t=fill")
    box = bo.detect_content_box(_src(clip), CW, CH)
    assert _px(box, CW, CH) == (0, 0, CW, CH)
    assert bo.layout_for(box, CW, CH) == "full"


def test_a_landscape_source_is_padded_into_the_portrait_canvas(tmp_path):
    clip = _clip(tmp_path / "land.mp4", size="320x180", rate=30, seconds=1)
    box = bo.detect_content_box(_src(clip), CW, CH)
    x0, y0, x1, y1 = _px(box, CW, CH)
    # 320x180 → 180x101 placed at y=108 (pad rounds the offset down to even
    # for 4:2:0), so the band is roughly centred, not exactly.
    assert (x0, x1) == (0, CW)
    assert 98 <= y1 - y0 <= 106 and 104 <= y0 <= 112, (y0, y1)
    assert bo.layout_for(box, CW, CH) == "bars"


def test_a_portrait_source_on_a_landscape_canvas_gets_side_bars(tmp_path):
    clip = _clip(tmp_path / "port.mp4", size="180x320", rate=30, seconds=1)
    box = bo.detect_content_box(_src(clip), 320, 180)
    x0, y0, x1, y1 = _px(box, 320, 180)
    assert (y0, y1) == (0, 180)
    assert 98 <= x1 - x0 <= 106 and 104 <= x0 <= 112, (x0, x1)
    assert bo.layout_for(box, 320, 180) == "full"


def test_a_still_card_is_measured_through_the_same_pad(tmp_path):
    img = _still(tmp_path / "card.png", 180, 180)
    src = bo.BrandSource(kind="intro", index=0, path=img, is_still=True, still_seconds=2.0)
    x0, y0, x1, y1 = _px(bo.detect_content_box(src, CW, CH), CW, CH)
    assert (x0, x1) == (0, CW) and 67 <= y0 <= 70 and 250 <= y1 <= 253, (y0, y1)


def test_a_half_black_picture_edge_goes_to_the_bar(tmp_path):
    # A flat grey 1:1 band whose first and last rows are a black blend (luma
    # ~37 against ~140), like the anti-aliased edge of a Veo band: left in
    # the box it is a dark keyline between the plate and the picture.
    clip = _clip(tmp_path / "blend.mp4", size="180x180", rate=30, seconds=1,
                 vf="drawbox=x=0:y=0:w=180:h=180:color=0x969696:t=fill,"
                    "drawbox=x=0:y=0:w=180:h=1:color=0x181818:t=fill,"
                    "drawbox=x=0:y=179:w=180:h=1:color=0x181818:t=fill,pad=180:320:0:70:black")
    x0, y0, x1, y1 = _px(bo.detect_content_box(_src(clip), CW, CH), CW, CH)
    assert (x0, x1) == (0, CW)
    assert (y0, y1) == (71, 249), (y0, y1)      # one row in at each edge, no more
    # A picture edge that is merely dark, not a blend, is kept.
    even = _clip(tmp_path / "even.mp4", size="180x180", rate=30, seconds=1,
                 vf="drawbox=x=0:y=0:w=180:h=180:color=0x202020:t=fill,pad=180:320:0:70:black")
    _, y0, _, y1 = _px(bo.detect_content_box(_src(even), CW, CH), CW, CH)
    assert (y0, y1) == (70, 250), (y0, y1)


def test_timeline_frame_math(tmp_path):
    a = _clip(tmp_path / "a30.mp4", size="180x320", rate=30, seconds=1.0)
    b = _clip(tmp_path / "b24.mp4", size="180x320", rate=24, seconds=1.0)   # raw-Veo rate
    c = _clip(tmp_path / "c30.mp4", size="180x320", rate=30, seconds=0.5)
    still = _still(tmp_path / "s.png", 180, 320)
    # The 24 fps clip is 1 s long, not 24/30 s.
    assert abs(media.video_seconds(b) - 1.0) < 1e-6
    sources = [
        bo.BrandSource("intro", 0, still, True, still_seconds=0.5),
        bo.BrandSource("scene", 0, a, False),
        bo.BrandSource("scene", 1, b, False),
        bo.BrandSource("outro", 0, c, False),
    ]
    segs = bo.build_timeline(sources, CW, CH)
    assert [s["frames"] for s in segs] == [15, 30, 30, 15]
    assert [s["from"] for s in segs] == [0, 15, 45, 75]
    assert [s["kind"] for s in segs] == ["intro", "scene", "scene", "outro"]
    assert [s["index"] for s in segs] == [0, 0, 1, 0]
    p = bo.build_props(segs, bo.build_seams(segs), width=CW, height=CH)
    assert p["durationInFrames"] == 90


# ---------------------------------------------------------------------------
# The composite (ffmpeg, end to end)
# ---------------------------------------------------------------------------

PW, PH = 160, 284       # composite canvas
RECT = (8, 6, 72, 40)   # opaque violet block (x0, y0, x1, y1) on every frame
COVER = range(28, 33)   # frames fully covered, straddling the cut at 30


def _alpha(i: int, *, draw: bool) -> np.ndarray:
    a = np.zeros((PH, PW), np.uint8)
    if not draw:
        return a
    if i in COVER:
        a[:] = 255
        return a
    x0, y0, x1, y1 = RECT
    a[y0:y1, x0:x1] = 255
    # a soft 50% stripe too, so partial alpha is exercised
    a[PH - 20:PH - 10, :] = 128
    return a


def _write_overlay(out_dir: Path, count: int, *, draw: bool = True, start: int = 1) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        rgba = np.zeros((PH, PW, 4), np.uint8)
        rgba[..., :3] = VIOLET
        rgba[..., 3] = _alpha(i, draw=draw)
        Image.fromarray(rgba, "RGBA").save(out_dir / f"element-{start + i:03d}.png")
    return {"frame_pattern": "element-%03d.png", "start_number": start, "frame_count": count}


@pytest.fixture(scope="module")
def reel(tmp_path_factory):
    """Three sources that disagree on everything: a letterboxed 30 fps clip
    whose audio outruns its picture, a still, and a 24 fps clip with no audio."""
    d = tmp_path_factory.mktemp("brand_reel")
    a = _clip(d / "a.mp4", size="160x160", rate=30, seconds=1.0,
              vf="pad=160:284:0:62:black", audio_sec=1.2)
    s = _still(d / "card.png", 160, 284, seed=7)
    c = _clip(d / "c.mp4", size="160x284", rate=24, seconds=1.0, src="testsrc")
    sources = [
        bo.BrandSource("scene", 0, a, False),
        bo.BrandSource("outro", 0, s, True, still_seconds=0.5),
        bo.BrandSource("scene", 1, c, False),
    ]
    segs = bo.build_timeline(sources, PW, PH)
    assert [x["frames"] for x in segs] == [30, 15, 30]
    return d, sources, segs


def _decode_y(path: Path) -> np.ndarray:
    raw = subprocess.run(
        [media.ffmpeg_exe(), "-v", "error", "-i", str(path), "-map", "0:v:0",
         "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
        capture_output=True, check=True).stdout
    fsz = PW * PH * 3 // 2
    n = len(raw) // fsz
    frames = np.frombuffer(raw[: n * fsz], np.uint8).reshape(n, fsz)
    return frames


def _decode_audio(path: Path) -> np.ndarray:
    raw = subprocess.run(
        [media.ffmpeg_exe(), "-v", "error", "-i", str(path), "-map", "0:a:0",
         "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def test_composite_is_frame_exact_in_sync_and_leaves_footage_alone(reel):
    d, sources, segs = reel
    total = sum(s["frames"] for s in segs)
    runs = {}
    for name, draw in (("branded", True), ("clear", False)):
        info = _write_overlay(d / f"g_{name}", total, draw=draw)
        runs[name] = bo.composite(
            sources, segs, d / f"g_{name}", info["frame_pattern"], info["start_number"],
            d / f"{name}.mp4", width=PW, height=PH)

    out = runs["branded"]
    # 1) exactly the frames the graphics were drawn for
    frames = _decode_y(out)
    assert len(frames) == total == bo.count_frames(out)

    # 2) audio as long as the picture (within a frame), and on the right frames:
    #    the tone belongs to the first 30 frames only (its extra 0.2 s is cut,
    #    the still and the silent 24 fps clip are silence).
    audio = _decode_audio(out)
    video_sec = total / 30
    assert abs(len(audio) / 48000 - video_sec) <= 1 / 30, (len(audio) / 48000, video_sec)
    env = np.abs(audio)
    loud = np.flatnonzero(env > 0.1 * env.max())
    assert abs(loud[-1] / 48000 - 1.0) <= 1 / 30, loud[-1] / 48000
    assert loud[0] / 48000 <= 1 / 30
    assert env[int(1.1 * 48000):].max() < 0.01

    # 3) outside the graphics, the footage is the footage: compare Y against
    #    the same pipeline with a fully transparent overlay.
    clear = _decode_y(runs["clear"])
    ys = frames[:, : PW * PH].reshape(total, PH, PW).astype(np.float64)
    yc = clear[:, : PW * PH].reshape(total, PH, PW).astype(np.float64)
    mask = np.stack([_alpha(i, draw=True) == 0 for i in range(total)])
    mse = ((ys - yc)[mask] ** 2).mean()
    psnr = 99.0 if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)
    assert psnr >= 45.0, psnr

    # 4) the graphics are really there, in the token colour (BT.709 limited).
    r, g, b = VIOLET
    y_exp = 16 + 219 * (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
    x0, y0, x1, y1 = RECT
    inner = ys[0, y0 + 2:y1 - 2, x0 + 2:x1 - 2]
    assert abs(inner.mean() - y_exp) < 3, (inner.mean(), y_exp)
    assert abs(ys[COVER[2]].mean() - y_exp) < 3          # a fully covered frame
    assert abs(yc[0, y0 + 2:y1 - 2, x0 + 2:x1 - 2].mean() - y_exp) > 10   # not in the clear run


def test_composite_keeps_each_sources_audio_offset(tmp_path):
    """A clip whose sound starts after (or before) its picture — an uploaded
    bookend, a remux, a Remotion render — keeps that offset. Zeroing both
    streams on their own used to pull a 200 ms late voice 200 ms early."""
    base = tmp_path / "base.mp4"
    _ff("-f", "lavfi", "-i", f"testsrc2=size={PW}x{PH}:rate=30:duration=1",
        "-f", "lavfi", "-i", "aevalsrc=0.8*sin(2*PI*440*t)*between(t\\,0.4\\,0.6):s=48000:d=1",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", str(base))
    late, early = tmp_path / "late.mp4", tmp_path / "early.mp4"
    _ff("-i", str(base), "-itsoffset", "0.2", "-i", str(base),
        "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", str(late))
    _ff("-itsoffset", "0.2", "-i", str(base), "-i", str(base),
        "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", str(early))
    info = _write_overlay(tmp_path / "g", 30, draw=False)
    # burst at 0.4 s of the sound: 0.2 s later / earlier against the picture
    for clip, onset in ((base, 0.4), (late, 0.6), (early, 0.2)):
        src = _src(clip)
        frames = bo.segment_frames(src)
        assert frames == 30, (clip.name, frames)       # the picture's own length
        seg = dict(_seg("scene", frames, 0), content={"x": 0, "y": 0, "w": 1, "h": 1},
                   layout="full")
        out = bo.composite([src], [seg], tmp_path / "g", info["frame_pattern"],
                           info["start_number"], tmp_path / f"out_{clip.name}", width=PW, height=PH)
        env = np.abs(_decode_audio(out))
        first = np.flatnonzero(env > 0.3 * env.max())[0] / 48000
        assert abs(first - onset) <= 0.01, (clip.name, first, onset)
        assert abs(len(env) / 48000 - 1.0) <= 1 / 30


def test_render_brand_reel_runs_the_whole_chain(reel, tmp_path):
    _, sources, _ = reel
    seen = {}

    def fake_graphics(props, out_dir):
        seen["props"] = props
        seen["dir"] = out_dir
        return _write_overlay(out_dir, props["durationInFrames"], start=0)

    res = asyncio.run(bo.render_brand_reel(
        sources, tmp_path / "work", width=PW, height=PH, style="auto",
        text={"heading": "Myth vs Fact", "sub": ""}, render_graphics=fake_graphics))
    assert res.final.is_file() and bo.count_frames(res.final) == 75
    assert res.props == seen["props"] and res.props["durationInFrames"] == 75
    assert [s["style"] for s in res.seams] == ["cover", "cover"]
    assert res.props["text"] == {"heading": "Myth vs Fact", "sub": ""}
    assert not seen["dir"].exists(), "the PNG sequence is removed after a good composite"
    assert json.loads((tmp_path / "work" / "props.json").read_text()) == res.props


def test_the_compositions_text_report_reaches_the_result(reel, tmp_path, monkeypatch):
    """The composition measures the text with the real fonts and reports what
    it drew; the server echoes that on the job, and it must survive the trip
    to BrandResult.text_fit (the arm shows it and stores it)."""
    from app.services import remotion_client

    _, sources, _ = reel
    report = {"status": "partial", "message": "Heading dropped: too long for the letterbox bars.",
              "heading_shown": False, "sub_shown": True}

    async def render_frames(template, out_dir, props, *, timeout=900.0):
        assert template == bo.TEMPLATE and timeout >= 900.0
        return {**_write_overlay(out_dir, props["durationInFrames"]), "status": "done",
                "text_fit": report}

    monkeypatch.setattr(remotion_client, "render_frames", render_frames)
    res = asyncio.run(bo.render_brand_reel(
        sources, tmp_path / "work", width=PW, height=PH,
        text={"heading": "A heading far too long for the bars", "sub": "Kept."}))
    assert res.text_fit == report
    # Sent whole: the backend never trims what the composition will measure.
    assert res.props["text"] == {"heading": "A heading far too long for the bars", "sub": "Kept."}


# ---------------------------------------------------------------------------
# Glitch cuts (the footage frames around the cut)
# ---------------------------------------------------------------------------

def test_glitch_plate_hands_over_slice_by_slice_and_is_deterministic():
    old = np.zeros((96, 64, 3), np.uint8)          # outgoing shot: black
    new = np.full((96, 64, 3), 255, np.uint8)      # incoming shot: white

    def white_rows(k):
        amount = bo.GLITCH_BEFORE[k + 6] if k < 0 else bo.GLITCH_AFTER[k]
        plate = bo.glitch_plate(old, new, k, amount, "cut-30")
        return (plate[..., 1].mean(axis=1) > 127).mean()

    # Six frames out it is all the old shot, six frames in all the new one...
    assert white_rows(-6) == 0 and white_rows(5) == 1
    # ...and around the cut each slice switches at its own frame.
    assert 0 < white_rows(-1) < 1 and 0 < white_rows(0) < 1
    assert white_rows(-3) <= white_rows(-1) <= white_rows(0) <= white_rows(2)
    a = bo.glitch_plate(old, new, 0, 1.0, "cut-30")
    assert np.array_equal(a, bo.glitch_plate(old, new, 0, 1.0, "cut-30"))
    assert not np.array_equal(a, bo.glitch_plate(old, new, 0, 1.0, "cut-31"))


def test_glitch_cut_rewrites_only_its_twelve_frames(reel, tmp_path):
    """Two scenes joined by a glitch cut: the frames F-6..F+5 of the overlay
    become opaque glitched footage, every other frame keeps its transparent
    pixels, and the composite stays frame exact."""
    _, sources, segs = reel
    two = [sources[0], sources[2]]                   # scene (30 f) + scene (30 f)
    t = bo.build_timeline(two, PW, PH)
    seams = bo.build_seams(t, "auto")
    assert [s["style"] for s in seams] == ["glitch"] and seams[0]["frame"] == 30
    info = _write_overlay(tmp_path / "g", 60, draw=False, start=0)
    n = bo.apply_glitch_plates(two, t, seams, tmp_path / "g", info["frame_pattern"], 0, PW, PH)
    assert n == 12
    for i in range(60):
        alpha = np.asarray(Image.open(tmp_path / "g" / f"element-{i:03d}.png").convert("RGBA"))[..., 3]
        assert alpha.min() == (255 if 24 <= i < 36 else 0), i
    out = bo.composite(two, t, tmp_path / "g", info["frame_pattern"], 0, tmp_path / "glitch.mp4",
                       width=PW, height=PH)
    assert bo.count_frames(out) == 60


def test_opaque_rgb_frames_are_promoted_before_the_composite(tmp_path):
    """Chrome writes a fully covered frame as RGB; a sequence mixing RGB and
    RGBA made ffmpeg buffer the whole reel (16 GB). Every frame leaves RGBA."""
    info = _write_overlay(tmp_path / "g", 6, start=0)
    Image.new("RGB", (PW, PH), VIOLET).save(tmp_path / "g" / "element-003.png")
    assert bo._promote_to_rgba(tmp_path / "g", info["frame_pattern"], 0, 6) == 1
    with Image.open(tmp_path / "g" / "element-003.png") as im:
        assert im.mode == "RGBA" and im.getpixel((5, 5)) == (*VIOLET, 255)
    assert bo._promote_to_rgba(tmp_path / "g", info["frame_pattern"], 0, 6) == 0


def test_render_brand_reel_refuses_a_short_graphics_render(reel, tmp_path):
    _, sources, _ = reel

    async def short_graphics(props, out_dir):     # async renderers work too
        return _write_overlay(out_dir, props["durationInFrames"] - 1)

    with pytest.raises(media.MediaError, match="74 frames"):
        asyncio.run(bo.render_brand_reel(
            sources, tmp_path / "work", width=PW, height=PH, render_graphics=short_graphics))
    assert not (tmp_path / "work" / "final.mp4").exists()


def test_brand_arm_uses_raw_sources_and_skips_the_critique(reel, tmp_path, monkeypatch):
    """The arm wrapper end to end with the DB, bucket, critic and Remotion
    stubbed: raw sources (no 720p `base` copies exist), still hold time from
    the TH v2 constants, script text, and no Gemini self-review."""
    from app.services import asset_storage, rebrand_brief
    from app.services import rebrand_store as store

    _, sources, _ = reel
    patches: list[dict] = []
    steps: list[str] = []

    async def patch_arm(exp_id, arm, patch):
        assert arm == "brand"
        patches.append(patch)

    async def append_arm_step(exp_id, arm, step):
        steps.append(step["name"])

    async def persist(**_kw):
        return None, None

    async def no_critique(**_kw):
        raise AssertionError("the brand arm must not be critiqued")

    fit = {"status": "partial", "message": "Heading dropped: too long for the letterbox bars.",
           "heading_shown": False, "sub_shown": True}

    async def graphics(props, out_dir):
        return {**_write_overlay(out_dir, props["durationInFrames"]), "text_fit": fit}

    monkeypatch.setattr(store, "patch_arm", patch_arm)
    monkeypatch.setattr(store, "append_arm_step", append_arm_step)
    monkeypatch.setattr(asset_storage, "persist_local_file", persist)
    monkeypatch.setattr(rebrand_brief, "critique", no_critique)
    monkeypatch.setattr(bo, "_remotion_graphics", graphics)
    monkeypatch.setitem(pipeline.BRAND_RESOLUTIONS, "1080", (PW, PH))

    a, still, c = sources[0].path, sources[1].path, sources[2].path
    ctx = pipeline.Ctx(
        exp_id=1, run_id=1, aspect="9:16", width=720, height=1280, work=tmp_path,
        options=pipeline.normalize_options({"run_brand": True, "run_omni": False,
                                            "run_veo": False}),
        run_ctx={"intro_headline": "Myth vs Fact", "hook": "Do you need a degree?"},
        intro=pipeline.Segment(kind="intro", index=0, source=c, is_still=False),
        # The letterboxed clip is the scene: the frame writes on bars scenes.
        scenes=[pipeline.Segment(kind="scene", index=0, source=a, is_still=False)],
        outro=pipeline.Segment(kind="outro", index=0, source=still, is_still=True),
    )
    asyncio.run(pipeline._brand_arm(ctx))

    final = patches[-1]
    assert final.get("status") == store.ARM_READY, patches
    assert final["resolution"] == f"{PW}x{PH}"
    frames = [s["frames"] for s in final["segments"]]
    assert frames == [30, 30, round(pipeline.OUTRO_STILL_SEC * 30)]
    assert [s["style"] for s in final["seams"]] == ["cover", "cover"]
    assert [s["layout"] for s in final["segments"]] == ["full", "bars", "full"]
    # The script's text goes to the composition whole; what it could draw
    # comes back as text_fit, is stored, and is reported as a step.
    assert final["text"] == {"heading": "Myth vs Fact", "sub": "Do you need a degree?"}
    assert final["text_fit"] == fit
    assert any(s.startswith("Text") for s in steps), steps
    assert final["auto_review"] is None
    assert steps[:3] == list(pipeline._BRAND_STEPS.values())
    assert steps[-1] == "Done" and not any("Self-review" in s for s in steps)
    assert (tmp_path / "brand" / "final.mp4").is_file()


# ---------------------------------------------------------------------------
# remotion_client.render_frames (HTTP mocked — no server)
# ---------------------------------------------------------------------------

import httpx  # noqa: E402

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _mock_remotion(monkeypatch, states: list[dict]):
    from app.services import remotion_client as rc

    sent: dict = {}
    polls = iter(states)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/render":
            sent["body"] = json.loads(request.content)
            return httpx.Response(200, json={"job_id": "j1"})
        if request.method == "GET" and request.url.path == "/jobs/j1":
            return httpx.Response(200, json=next(polls))
        return httpx.Response(404, json={"error": "nope"})

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(rc.httpx, "AsyncClient", client)
    monkeypatch.setattr(rc, "_FRAMES_POLL_SEC", 0.0)
    return rc, sent


def test_render_frames_submits_a_png_sequence_and_polls_to_done(monkeypatch, tmp_path):
    done = {"status": "done", "frame_count": 90, "frame_pattern": "element-%02d.png",
            "start_number": 0}
    rc, sent = _mock_remotion(monkeypatch, [
        {"status": "queued"}, {"status": "rendering", "progress": 0.5}, done])
    job = asyncio.run(rc.render_frames("brand-overlay-42", tmp_path / "g", {"a": 1}))
    assert job == done
    assert sent["body"] == {"template": "brand-overlay-42", "output_path": str(tmp_path / "g"),
                            "props": {"a": 1}, "format": "png-sequence"}


def test_render_frames_raises_on_failure_and_on_an_old_server(monkeypatch, tmp_path):
    rc, _ = _mock_remotion(monkeypatch, [{"status": "failed", "error": "boom"}])
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(rc.render_frames("brand-overlay-42", tmp_path / "g", {}))
    # A server without png-sequence support renders an MP4 and says "done".
    rc, _ = _mock_remotion(monkeypatch, [{"status": "done", "output_path": "x"}])
    with pytest.raises(RuntimeError, match="png-sequence"):
        asyncio.run(rc.render_frames("brand-overlay-42", tmp_path / "g", {}))
    rc, _ = _mock_remotion(monkeypatch, [{"status": "rendering"}] * 50)
    with pytest.raises(RuntimeError, match="did not finish"):
        asyncio.run(rc.render_frames("brand-overlay-42", tmp_path / "g", {}, timeout=0.0))


# ---------------------------------------------------------------------------
# Topic icons (brand_icons)
# ---------------------------------------------------------------------------

from app.services import brand_icons as bi  # noqa: E402


def test_icon_ids_all_exist_in_the_composition():
    import re

    ts = Path(__file__).resolve().parents[2] / "services" / "remotion" / "src" / "brand42" / "icons.ts"
    if not ts.is_file():
        pytest.skip("the Remotion sources are not in this checkout")
    have = set(re.findall(r'^\s*"([a-z0-9-]+)":', ts.read_text(encoding="utf-8"), re.M))
    assert bi.icon_ids() <= have, bi.icon_ids() - have


def test_keywords_prefer_the_topic_over_common_words():
    hits = bi.find_hits("I joined 42 Abu Dhabi because it's completely free and I started coding.")
    icons = [h.icon for h in hits]
    assert icons == ["piggy-bank", "rocket-launch", "code"]
    assert {h.icon: h.tier for h in hits} == {"piggy-bank": 0, "rocket-launch": 1, "code": 0}
    # The brand name alone is not a topic.
    assert bi.find_hits("Welcome to 42 Abu Dhabi")[0].icon == "hand-waving"
    assert not any(h.icon == "map-pin" for h in bi.find_hits("42 Abu Dhabi"))
    assert [h.icon for h in bi.find_hits("The Piscine is four weeks of planks")] == \
        ["swimming-pool", "calendar-blank", "barbell"]
    # A negated keyword says the opposite: no icon for it...
    assert [h.icon for h in bi.find_hits("There are no teachers and no lectures here.")] == []
    assert [h.icon for h in bi.find_hits("You don't need a degree to code.")] == ["code"]
    assert [h.icon for h in bi.find_hits("Learn without any lectures at all")] == ["book-open"]
    # ...unless the rule names the negation itself.
    assert [h.icon for h in bi.find_hits("No tuition, no fees.")][0] == "piggy-bank"
    # A later, plain mention still counts.
    assert "chalkboard-teacher" in [h.icon for h in bi.find_hits("No teachers? The teachers are your peers.")]


def test_icon_plan_keeps_the_rules():
    seg = {"kind": "scene", "index": 0, "from": 100, "frames": 240}
    text = ("Coding at 42 is free, you work with peers on projects, get feedback, "
            "find jobs, win awards and love it.")
    cues = bi.plan_scene(text, seg, (0.3, 7.6))
    assert 1 <= len(cues) <= bi.MAX_PER_SCENE
    froms = [c["from"] for c in cues]
    assert froms == sorted(froms)
    assert all(b - a >= bi.GAP_FRAMES for a, b in zip(froms, froms[1:]))
    for c in cues:
        assert c["from"] >= 100 + bi.HEAD_CLEAR
        assert c["from"] + c["frames"] <= 100 + 240 - bi.TAIL_CLEAR or c["frames"] == bi.MIN_FRAMES
        assert bi.MIN_FRAMES <= c["frames"] <= bi.MAX_FRAMES
    # Strong words win the slots: none of these is a weak-tier icon.
    assert {c["icon"] for c in cues} <= {h.icon for h in bi.find_hits(text) if h.tier == 0}
    # The first icon lands on its word: "Coding" opens the line.
    assert cues[0]["icon"] == "code" and cues[0]["from"] == 100 + bi.HEAD_CLEAR
    # Nothing to say, or a scene too short to hold one: no icons.
    assert bi.plan_scene("", seg, None) == []
    assert bi.plan_scene("Coding is free", dict(seg, frames=60), None) == []
    # plan_icons keys by segment POSITION and skips cards.
    segs = [dict(seg, kind="intro", **{"from": 0, "frames": 100}), seg]
    assert bi.plan_icons(segs, {0: text, 1: text}, {}) == bi.plan_scene(text, seg, None)


def test_voiced_span_finds_the_speech_in_the_clip(tmp_path):
    clip = tmp_path / "talk.mp4"
    _ff("-f", "lavfi", "-i", "color=c=gray:s=64x64:r=30:d=3",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=3",
        "-filter_complex", "[1:a]volume=enable='lt(t,0.5)+gt(t,2.5)':volume=0[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-shortest", str(clip))
    t0, t1 = bi.voiced_span(clip)
    assert abs(t0 - 0.5) < 0.1 and abs(t1 - 2.5) < 0.1
    silent = _clip(tmp_path / "silent.mp4", size="64x64", rate=30, seconds=1.0)
    assert bi.voiced_span(silent) is None


def test_scene_text_follows_each_source_to_its_own_line():
    sources = [bo.BrandSource("intro", 0, Path("i.mp4"), False),
               bo.BrandSource("scene", 0, Path("a.mp4"), False),
               bo.BrandSource("scene", 1, Path("b.mp4"), False),
               bo.BrandSource("outro", 0, Path("o.mp4"), False)]
    run_ctx = {"scene_dialogue": {"0": "It is free.", "1": "Learn to code."}}
    assert pipeline._brand_scene_text(sources, run_ctx) == {1: "It is free.", 2: "Learn to code."}
    assert pipeline._brand_scene_text(sources, {}) == {}
    o = pipeline.normalize_options({"run_brand": True})
    assert o["brand_icons"] is True and o["brand_edge_patterns"] is True
    o = pipeline.normalize_options({"brand_icons": False, "brand_edge_patterns": 0,
                                    "brand_transitions": "GLITCH"})
    assert (o["brand_icons"], o["brand_edge_patterns"], o["brand_transitions"]) == (False, False, "glitch")
    segs = [pipeline.Segment("scene", 0, Path("a.mp4"), False), pipeline.Segment("scene", 1, Path("b.mp4"), False)]
    md = {"scene_count": 2, "scenes": [{"dialogue": "It is free."}, {"dialogue": ""}], "script": {}}
    ctx = pipeline._run_ctx({"topic": "t"}, md, segs, "9:16")
    assert ctx["scene_dialogue"] == {0: "It is free.", 1: ""} and ctx["dialogue"] == ["It is free."]
