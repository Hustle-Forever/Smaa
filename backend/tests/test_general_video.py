"""Focused tests for the General AI Video tool + the Library preset mapping.

These are pure-function checks (no DB / no network):
  - the still system prompt is genuinely UNBRANDED (no logo / no text),
  - the i2v motion clause honours the audio toggle,
  - _gv_out surfaces frame/file URLs only when the media exists / is ready,
  - the Library item builder maps a Video Editor project's project.preset
    ("general_video" | "b_roll") to the right tile type so resume routes back to
    the originating Ideation creator.
"""
import app.routers.yas_producer as yp
from app.services import yas_video_editor as ve_service


def test_general_video_still_system_is_unbranded():
    sys = yp.GENERAL_VIDEO_STILL_SYSTEM.lower()
    assert "no" in sys and "logo" in sys          # explicitly forbids a logo
    assert "text overlay" in sys or "caption" in sys
    # It must NOT instruct baking the 42 mark in (that's the Animation tool).
    assert "42" not in sys


def test_gv_motion_prompt_audio_toggle():
    with_audio = yp._gv_motion_prompt("a city at dawn", with_audio=True)
    no_audio = yp._gv_motion_prompt("a city at dawn", with_audio=False)
    assert "a city at dawn" in with_audio
    assert "No audio" not in with_audio
    assert "No audio" in no_audio


def test_gv_out_urls():
    # frame-only (still rendered, not yet animated)
    frame_only = yp._gv_out({
        "id": 7, "status": "frame_ready", "frame_storage_path": "general-video/7-frame.png",
    })
    assert frame_only["frame_url"].endswith("/general-video/7/frame")
    assert "file_url" not in frame_only
    # ready (video done)
    ready = yp._gv_out({
        "id": 9, "status": "ready",
        "frame_local_path": "x.png", "video_storage_path": "general-video/9.mp4",
    })
    assert ready["file_url"].endswith("/general-video/9/file")


def _ve_row(preset):
    return {
        "id": 100,
        "mode": ve_service.MODE_PREVIEW,
        "topic": "demo",
        "status": "ready",
        "metadata": {"video_edit": {"project": {"preset": preset}, "render": {}}},
    }


def test_library_item_preset_maps_to_type():
    assert yp._library_item(_ve_row("general_video"))["type"] == "general_video"
    assert yp._library_item(_ve_row("b_roll"))["type"] == "broll"
    # A plain editor project (no preset) stays "video_editor".
    assert yp._library_item(_ve_row(None))["type"] == "video_editor"
