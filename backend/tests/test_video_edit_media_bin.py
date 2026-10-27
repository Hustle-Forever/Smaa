"""Tests for the media-bin → timeline AI additions.

Covers the three backend behaviours added so the AI chat can place bin media:

  * ``_op_add_media_clip`` with ``placement="append"`` appends full-frame on the
    MAIN user video track (excluding the AI lanes) and CASCADES across a batch —
    the "add all my media to the timeline" path.
  * the default / non-append path is UNCHANGED (still the AI media lane), so
    B-roll / bookend features don't regress.
  * ``validate_and_filter_ops`` accepts ``add_media_clip`` for a source in
    ``valid_clip_sources`` (now seeded from the bin) and rejects one that isn't.
  * ``_media_bin_block`` lists bin items with their source_kind + source_id.

Pure functions — no DB / network. Runs standalone or under pytest.
"""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services import yas_video_edit_ai as ai  # noqa: E402
from app.services import yas_video_edit_ai_ops as ops  # noqa: E402


def _base_tracks() -> list[dict]:
    return [
        {"id": "trk_v0", "kind": "video", "index": 0, "muted": False, "locked": False},
        {"id": "trk_a0", "kind": "audio", "index": 100, "muted": False, "locked": False},
    ]


# --------------------------------------------------------------------------- #
# placement="append" — sequential main-track append + cascade
# --------------------------------------------------------------------------- #
def test_append_places_full_frame_on_main_track():
    tracks, clips = _base_tracks(), []
    op = {"op": "add_media_clip", "source_kind": "upload", "source_id": 7,
          "durationInFrames": 60, "placement": "append"}
    ops._op_add_media_clip({}, tracks, clips, op, "c1")
    assert len(clips) == 1
    c = clips[0]
    assert c["trackId"] == "trk_v0"          # main user lane, not an AI lane
    assert c["from"] == 0                      # first clip starts at 0
    assert c["durationInFrames"] == 60
    assert c["fit"] == "cover"                 # full-frame
    assert c["keyframes"] == {}                # NOT the 0.6-scale PiP overlay
    assert c["source"]["upload_id"] == 7


def test_append_cascades_across_a_batch():
    """apply_plan runs ops in order mutating `clips`; each append recomputes the
    track end fresh, so a batch lands back-to-back with no cross-op arithmetic."""
    tracks, clips = _base_tracks(), []
    for i, (sid, dur) in enumerate([(7, 60), (8, 90), (9, 45)]):
        op = {"op": "add_media_clip", "source_kind": "upload", "source_id": sid,
              "durationInFrames": dur, "placement": "append"}
        ops._op_add_media_clip({}, tracks, clips, op, f"c{i}")
    assert [c["from"] for c in clips] == [0, 60, 150]
    assert all(c["trackId"] == "trk_v0" for c in clips)


def test_append_stills_are_full_frame_images():
    tracks, clips = _base_tracks(), []
    op = {"op": "add_media_clip", "source_kind": "still", "source_id": 5,
          "durationInFrames": 90, "placement": "append"}
    ops._op_add_media_clip({}, tracks, clips, op, "c1")
    c = clips[0]
    assert c["kind"] == "image"
    assert c["trackId"] == "trk_v0"
    assert c["source"]["still_id"] == 5


def test_append_targets_main_track_even_with_ai_lanes_present():
    tracks = _base_tracks() + [
        {"id": ops.AI_MEDIA_TRACK_ID, "kind": "video", "index": 5},
        {"id": ops.AI_VIDEO_TRACK_ID, "kind": "video", "index": 6},
    ]
    clips = [{"id": "x", "trackId": ops.AI_MEDIA_TRACK_ID, "from": 0, "durationInFrames": 30}]
    op = {"op": "add_media_clip", "source_kind": "upload", "source_id": 7,
          "durationInFrames": 60, "placement": "append"}
    ops._op_add_media_clip({}, tracks, clips, op, "c1")
    new = clips[-1]
    assert new["trackId"] == "trk_v0"          # never an AI lane
    assert new["from"] == 0                      # main track was empty → start at 0


# --------------------------------------------------------------------------- #
# default (non-append) path is UNCHANGED — AI media lane
# --------------------------------------------------------------------------- #
def test_default_media_clip_still_lands_on_ai_media_lane():
    tracks, clips = _base_tracks(), []
    op = {"op": "add_media_clip", "source_kind": "upload", "source_id": 7,
          "durationInFrames": 60}  # no placement, no overlay
    ops._op_add_media_clip({}, tracks, clips, op, "c1")
    assert clips[0]["trackId"] == ops.AI_MEDIA_TRACK_ID


def test_overlay_wins_over_append():
    """overlay=true is a PiP inset regardless of placement — never the main lane."""
    tracks, clips = _base_tracks(), []
    op = {"op": "add_media_clip", "source_kind": "upload", "source_id": 7,
          "durationInFrames": 60, "overlay": True, "placement": "append"}
    ops._op_add_media_clip({}, tracks, clips, op, "c1")
    assert clips[0]["trackId"] == ops.AI_VIDEO_TRACK_ID


# --------------------------------------------------------------------------- #
# validation — bin-seeded valid_clip_sources
# --------------------------------------------------------------------------- #
def test_validate_accepts_bin_source():
    op = {"op": "add_media_clip", "source_kind": "upload", "source_id": 7,
          "placement": "append"}
    accepted, rejected = ops.validate_and_filter_ops(
        [op], feature="edit", valid_clip_ids=set(), valid_asset_ids=set(),
        valid_clip_sources={("upload", 7)})
    assert len(accepted) == 1 and not rejected


def test_validate_rejects_unavailable_source():
    op = {"op": "add_media_clip", "source_kind": "upload", "source_id": 7,
          "placement": "append"}
    accepted, rejected = ops.validate_and_filter_ops(
        [op], feature="edit", valid_clip_ids=set(), valid_asset_ids=set(),
        valid_clip_sources=set())
    assert not accepted and len(rejected) == 1


# --------------------------------------------------------------------------- #
# _media_bin_block — the planner's "what's available to add" grounding
# --------------------------------------------------------------------------- #
def test_media_bin_block_lists_sources():
    block = ai._media_bin_block({"project": {"media_bin": [
        {"kind": "upload", "id": 3, "title": "Intro clip", "durationInFrames": 60},
        {"kind": "still", "id": 9, "title": "Poster"},
    ]}})
    assert "source_kind=upload source_id=3" in block
    assert "source_kind=still source_id=9" in block
    assert "(image)" in block          # stills tagged as image
    assert "(video, ~2s)" in block     # 60f @30 ≈ 2s


def test_media_bin_block_empty_when_no_bin():
    assert ai._media_bin_block({"project": {}}) == ""
    assert ai._media_bin_block({}) == ""


if __name__ == "__main__":  # allow `python tests/test_video_edit_media_bin.py`
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
