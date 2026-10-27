"""Background music under a finished TH v2 reel (services/th_reel_music).

  start_track   refuses without a finished reel, while a track is being made,
                without a vibe; asks Suno once, for an instrumental, claimed
                first (a racing second press is a 409, not a second track)
  poll_tracks   generating -> ready (downloaded, kept, laid under the reel,
                music on), -> failed (Suno's words), -> failed after 30 min;
                a file that lags Suno's SUCCESS is waited for
  the mix       the reel's exact length, its video stream copied (the same
                frames, the same bitstream), the track looped under a longer
                reel, a reel with no sound gets the bed alone
  the switch    off keeps the file and flips the flag; a level re-mixes; a
                reel finalized again gets its music again (free); a failed
                mix is not retried by the poller, a press retries it
  serving       composition/file without ?music is today's; with it, the mix
  elsewhere     the composition poller's phase never breaks its loop; saved
                to assets / posted: the music version when it is shown

No database (the music's store is in memory, every get_conn a tripwire), no
Suno (a fake provider and download), no bucket. Real ffmpeg on media it
synthesizes (lavfi color / sine / anullsrc).
"""
from __future__ import annotations

import asyncio
import copy
import re
import shutil
import subprocess
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.services import asset_storage
from app.services import rebrand_media as media
from app.services import th_reel
from app.services import th_reel_music as music
from app.services.media_providers import ProviderError
from app.workers import yas_th_v2_composition_poller as poller

RUN = 9401
REEL_SEC = 6.0


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Media, made once: a 6 s reel (picture + a voice-like tone), the same with
# silence, the same with no sound at all, and a 3 s track (shorter: it loops)
# ---------------------------------------------------------------------------

def _ff(*args: str) -> None:
    subprocess.run([media.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True, timeout=120)


@pytest.fixture(scope="module")
def clips(tmp_path_factory) -> types.SimpleNamespace:
    d = tmp_path_factory.mktemp("music_media")
    picture = ["-f", "lavfi", "-i", f"color=c=navy:s=180x320:r=30:d={REEL_SEC}"]
    enc = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"]
    _ff(*picture, "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={REEL_SEC}",
        *enc, str(d / "reel.mp4"))
    _ff(*picture, "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo", "-t", str(REEL_SEC),
        *enc, str(d / "quiet.mp4"))
    _ff(*picture, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "mute.mp4"))
    track = d / "track.mp3"
    try:
        _ff("-f", "lavfi", "-i", "sine=frequency=220:sample_rate=44100:duration=3",
            "-c:a", "libmp3lame", "-b:a", "128k", str(track))
    except subprocess.CalledProcessError:
        # No mp3 encoder in this build: a WAV under the same name (ffmpeg
        # reads the content, not the extension).
        _ff("-f", "lavfi", "-i", "sine=frequency=220:sample_rate=44100:duration=3",
            "-c:a", "pcm_s16le", "-f", "wav", str(track))
    return types.SimpleNamespace(reel=d / "reel.mp4", quiet=d / "quiet.mp4", mute=d / "mute.mp4",
                                 track=track, track_bytes=track.read_bytes())


# ---------------------------------------------------------------------------
# The world: a data root in tmp, a finished reel on it, the store in memory
# ---------------------------------------------------------------------------

class FakeSuno:
    def __init__(self) -> None:
        self.key = True
        self.calls: list[tuple[str, float]] = []
        self.checks: list[str] = []
        self.answer: object = {"status": "pending", "audio_url": None, "error": None}
        self.submit_error: Exception | None = None

    def available(self) -> bool:
        return self.key

    async def make_music(self, *, style_brief: str, duration_sec: float) -> dict:
        self.calls.append((style_brief, duration_sec))
        if self.submit_error is not None:
            raise self.submit_error
        return {"kind": "pending", "provider_job_id": f"job-{len(self.calls)}"}

    async def check_job(self, job_id: str) -> dict:
        self.checks.append(job_id)
        if isinstance(self.answer, Exception):
            raise self.answer
        return dict(self.answer)


class World:
    def __init__(self, root: Path, clips: types.SimpleNamespace) -> None:
        self.root = root
        self.clips = clips
        self.suno = FakeSuno()
        self.download: bytes = b""
        self.downloads: list[list[str]] = []
        self.backups: list[str] = []
        self.backup_answer: tuple = (None, None)
        self.store = music.MemoryStore()

    def comp(self, name: str) -> Path:
        return poller.COMP_DIR / str(RUN) / name

    def reel(self, seq: int, src: Path | None = None) -> str:
        """A finished reel final-<seq>.mp4 on this disk; its stored path."""
        p = self.comp(f"final-{seq}.mp4")
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src or self.clips.reel, p)
        return poller._to_rel(p)

    def md(self) -> dict:
        return self.store.rows[RUN]["metadata"]

    def rm(self) -> dict:
        return self.md().get("reel_music") or {}


def _finished_md(rel: str, seq: int = 2) -> dict:
    return {"aspect_ratio": "9:16", "scene_count": 1, "composition_status": "ready",
            "render_owner": th_reel.INSTANCE_ID, "render_job_id": "job", "composition_path": rel,
            "composition_seq": seq, "composition_good_seq": seq,
            "composition_plan": {"seq": seq, "total_frames": int(REEL_SEC * 30)}}


class _Tripped(AssertionError):
    pass


@pytest.fixture
def world(tmp_path, monkeypatch, clips) -> World:
    root = tmp_path / "data"
    monkeypatch.setattr(poller, "config", types.SimpleNamespace(
        asset_root=root / "assets", producer_assets_root=root / "assets" / "assets_library"))
    monkeypatch.setattr(poller, "COMP_DIR", root / "yas_producer" / "talking-head-v2")
    w = World(root, clips)
    w.store.rows[RUN] = {"id": RUN, "metadata": _finished_md(w.reel(2))}
    music.set_store(w.store)
    monkeypatch.setattr(music, "provider", lambda: w.suno)

    async def download(run_id, urls):
        w.downloads.append(list(urls))
        return w.download

    monkeypatch.setattr(music, "_download_first", download)

    async def backup(*, abs_path, bucket_key, mime="video/mp4"):
        w.backups.append(bucket_key)
        return w.backup_answer

    monkeypatch.setattr(asset_storage, "persist_local_file", backup)

    def trip(*_a, **_k):
        raise _Tripped("the database was reached")

    monkeypatch.setattr(music, "get_conn", trip)
    return w


def _done(w: World) -> None:
    """Suno has finished the track and its file downloads."""
    w.suno.answer = {"status": "done", "audio_url": "https://cdn1.suno.ai/a.mp3",
                     "audio_urls": ["https://cdn1.suno.ai/a.mp3", "https://tempfile/a.mp3"], "error": None}
    w.download = w.clips.track_bytes


def _with_music(w: World) -> None:
    """A track made and laid under the reel (music on)."""
    run(music.start_track(RUN, "slow jazz piano"))
    _done(w)
    run(music.poll_tracks())
    assert w.rm()["mix"]["for_seq"] == 2


def _ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _state(w: World) -> dict:
    return music.public_state(RUN, w.md())


# ---------------------------------------------------------------------------
# start_track
# ---------------------------------------------------------------------------

def test_no_music_is_the_default_state(world):
    got = _state(world)
    assert got["on"] is False and got["track_status"] is None and got["has_track"] is False
    assert got["applied"] is False and got["mixing"] is False and got["music_url"] is None
    assert got["available"] is True and got["level"] == "medium" and got["reel_sec"] == REEL_SEC
    assert music.current_id(world.md()) is None and music.served_mix(world.md()) is None
    assert run(music.current_bytes(world.md())) is None


def test_start_track_refuses_without_a_finished_reel(world):
    world.md().pop("composition_path")
    with pytest.raises(HTTPException) as e:
        run(music.start_track(RUN, "slow jazz piano"))
    assert e.value.status_code == 409 and world.suno.calls == []
    # Its file gone and no bucket copy: not finished either.
    world.md()["composition_path"] = "yas_producer/talking-head-v2/9401/final-9.mp4"
    with pytest.raises(HTTPException) as e:
        run(music.start_track(RUN, "slow jazz piano"))
    assert e.value.status_code == 409 and world.suno.calls == [] and world.store.writes == []


@pytest.mark.parametrize("brief", ["", "   ", "x" * (music.BRIEF_MAX + 1)])
def test_start_track_refuses_an_empty_or_long_vibe(world, brief):
    with pytest.raises(HTTPException) as e:
        run(music.start_track(RUN, brief))
    assert e.value.status_code == 400 and world.suno.calls == [] and world.store.writes == []


def test_start_track_asks_suno_once_for_an_instrumental(world):
    run(music.start_track(RUN, "  slow jazz   piano "))
    assert world.suno.calls == [("slow jazz piano, instrumental background music, no vocals", REEL_SEC)]
    track = world.rm()["track"]
    assert track["status"] == "generating" and track["suno_job_id"] == "job-1"
    assert track["brief"] == "slow jazz piano" and track["started_at"]
    got = _state(world)
    assert got["track_status"] == "generating" and got["brief"] == "slow jazz piano"
    assert got["has_track"] is False and got["mixing"] is False


def test_start_track_refuses_while_a_track_is_being_made(world):
    run(music.start_track(RUN, "slow jazz piano"))
    with pytest.raises(HTTPException) as e:
        run(music.start_track(RUN, "corporate"))
    assert e.value.status_code == 409 and len(world.suno.calls) == 1
    # One nobody will finish (past the give-up) does not block a new one.
    world.md()["reel_music"]["track"]["started_at"] = _ago(music.GIVE_UP_MIN + 1)
    run(music.start_track(RUN, "corporate"))
    assert len(world.suno.calls) == 2 and world.rm()["track"]["suno_job_id"] == "job-2"


def test_a_second_press_that_read_before_the_first_claim_is_refused(world, monkeypatch):
    before = copy.deepcopy(world.store.rows[RUN])
    run(music.start_track(RUN, "slow jazz piano"))

    async def stale_read(run_id):
        return copy.deepcopy(before)                   # this press read the row before the claim

    monkeypatch.setattr(world.store, "run", stale_read)
    with pytest.raises(HTTPException) as e:
        run(music.start_track(RUN, "corporate"))
    assert e.value.status_code == 409 and len(world.suno.calls) == 1
    assert world.rm()["track"]["suno_job_id"] == "job-1"


def test_start_track_without_a_suno_key_is_503(world):
    world.suno.key = False
    with pytest.raises(HTTPException) as e:
        run(music.start_track(RUN, "slow jazz piano"))
    assert e.value.status_code == 503 and world.suno.calls == [] and world.store.writes == []
    assert _state(world)["available"] is False


def test_suno_refusing_is_502_in_its_own_words(world):
    world.suno.submit_error = ProviderError("Suno: insufficient credits — top up at sunoapi.org.")
    with pytest.raises(HTTPException) as e:
        run(music.start_track(RUN, "slow jazz piano"))
    assert e.value.status_code == 502 and "insufficient credits" in e.value.detail
    got = _state(world)
    assert got["track_status"] == "failed" and "insufficient credits" in got["error"]
    # A failed track does not block the next press.
    world.suno.submit_error = None
    run(music.start_track(RUN, "slow jazz piano"))
    assert world.rm()["track"]["status"] == "generating"


# ---------------------------------------------------------------------------
# poll_tracks
# ---------------------------------------------------------------------------

def test_poll_makes_a_finished_track_ready_and_lays_it_under_the_reel(world):
    run(music.start_track(RUN, "slow jazz piano"))
    _done(world)
    run(music.poll_tracks())
    rm = world.rm()
    track = rm["track"]
    assert track["status"] == "ready" and 2.9 <= track["sec"] <= 3.2
    assert world.downloads == [["https://cdn1.suno.ai/a.mp3", "https://tempfile/a.mp3"]]
    kept = poller._abs_from_rel(track["rel"])
    assert kept == world.comp("music/track-job-1.mp3") and kept.read_bytes() == world.clips.track_bytes
    assert rm["on"] is True and rm["mix"]["for_seq"] == 2 and rm["mix"]["made"] == "job-1|medium"
    mixed = poller._abs_from_rel(rm["mix"]["rel"])
    assert mixed.is_file() and re.fullmatch(r"music-2-[0-9a-f]{6}\.mp4", mixed.name)
    assert world.backups == [f"yas/compositions/talking-head/{RUN}-music-track.mp3",
                             f"yas/compositions/talking-head/{RUN}-music.mp4"]
    got = _state(world)
    assert got["track_status"] == "ready" and got["applied"] is True and got["mixing"] is False
    assert got["has_track"] and got["track_brief"] == "slow jazz piano" and got["on"] is True
    assert got["music_url"] == (f"/api/yas-producer/talking-head-v2/{RUN}/composition/file"
                                f"?music=1&v=2&m={rm['mix']['id']}")
    assert music.current_id(world.md()) == rm["mix"]["id"]
    assert run(music.current_bytes(world.md())) == mixed.read_bytes()
    # Nothing left to do: the next poll changes nothing.
    assert run(world.store.due()) == []
    # The canonical reel is untouched.
    assert world.md()["composition_path"].endswith("final-2.mp4")


def test_poll_waits_while_suno_cooks_and_while_its_file_lags(world):
    run(music.start_track(RUN, "slow jazz piano"))
    run(music.poll_tracks())
    assert world.rm()["track"]["status"] == "generating" and world.suno.checks == ["job-1"]
    _done(world)
    world.download = b""                              # the URL lags SUCCESS
    run(music.poll_tracks())
    assert world.rm()["track"]["status"] == "generating" and len(world.downloads) == 1
    world.suno.answer = ProviderError("Suno: rate limited — try again shortly.")
    run(music.poll_tracks())                          # transient: kept
    assert world.rm()["track"]["status"] == "generating"
    _done(world)
    run(music.poll_tracks())
    assert world.rm()["track"]["status"] == "ready"


def test_poll_fails_a_track_suno_failed(world):
    run(music.start_track(RUN, "slow jazz piano"))
    world.suno.answer = {"status": "failed", "audio_url": None, "error": "SENSITIVE_WORD_ERROR"}
    run(music.poll_tracks())
    got = _state(world)
    assert got["track_status"] == "failed" and got["error"] == "SENSITIVE_WORD_ERROR"
    assert got["has_track"] is False and got["on"] is False and run(world.store.due()) == []


def test_poll_gives_up_after_30_minutes(world):
    run(music.start_track(RUN, "slow jazz piano"))
    world.md()["reel_music"]["track"]["started_at"] = _ago(music.GIVE_UP_MIN + 1)
    run(music.poll_tracks())
    got = _state(world)
    assert got["track_status"] == "failed" and got["error"] == music.TOO_LONG
    assert world.suno.checks == []


def test_a_claim_whose_suno_call_never_came_back_is_given_up(world):
    run(music.start_track(RUN, "slow jazz piano"))
    track = world.md()["reel_music"]["track"]
    track["suno_job_id"] = None
    run(music.poll_tracks())                          # a press may still be asking Suno
    assert world.rm()["track"]["status"] == "generating"
    track["started_at"] = _ago(music.CLAIM_LOST_MIN + 1)
    run(music.poll_tracks())
    assert world.rm()["track"]["status"] == "failed" and world.rm()["track"]["error"] == music.CLAIM_LOST


def test_a_track_replaced_meanwhile_is_not_made_ready(world, monkeypatch):
    run(music.start_track(RUN, "slow jazz piano"))
    _done(world)
    real = music._download_first

    async def download_then_replaced(run_id, urls):
        world.md()["reel_music"]["track"]["suno_job_id"] = "job-newer"
        return await real(run_id, urls)

    monkeypatch.setattr(music, "_download_first", download_then_replaced)
    run(music.poll_tracks())
    assert world.rm()["track"]["status"] == "generating" and world.rm()["track"]["suno_job_id"] == "job-newer"
    assert "mix" not in world.rm()


# ---------------------------------------------------------------------------
# The mix itself (real ffmpeg)
# ---------------------------------------------------------------------------

def _video_bitstream(p: Path) -> bytes:
    return subprocess.run([media.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(p),
                           "-map", "0:v:0", "-c", "copy", "-f", "h264", "-"],
                          check=True, capture_output=True, timeout=60).stdout


def _mean_volume(p: Path, start: float, dur: float) -> float:
    err = subprocess.run([media.ffmpeg_exe(), "-hide_banner", "-ss", str(start), "-t", str(dur), "-i", str(p),
                          "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True, timeout=60).stderr
    return float(re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", err).group(1).replace("-inf", "-200"))


def test_the_mix_is_the_reels_length_and_copies_its_video(clips, tmp_path):
    out = tmp_path / "music-1-abc.mp4"
    sec = music.mix_under(clips.reel, clips.track, out, gain_db=music.LEVELS["medium"])
    assert abs(sec - REEL_SEC) < 0.05
    assert abs(media.video_seconds(out) - media.video_seconds(clips.reel)) <= 0.1
    assert abs((media.probe_duration(out) or 0) - REEL_SEC) <= 0.1
    assert media.frame_count(out) == media.frame_count(clips.reel)
    assert _video_bitstream(out) == _video_bitstream(clips.reel)      # copied, not re-encoded
    assert media.has_audio(out)
    assert not list(tmp_path.glob("*.part"))


def test_a_shorter_track_loops_under_the_whole_reel(clips, tmp_path):
    out = tmp_path / "music-1-loop.mp4"
    music.mix_under(clips.quiet, clips.track, out, gain_db=0.0)
    # The 3 s track is still playing in the reel's second half.
    assert _mean_volume(out, 3.5, 1.0) > -45.0
    assert abs(media.video_seconds(out) - REEL_SEC) <= 0.1


def test_a_reel_with_no_sound_gets_the_bed_alone(clips, tmp_path):
    out = tmp_path / "music-1-mute.mp4"
    music.mix_under(clips.mute, clips.track, out, gain_db=-8.0)
    assert media.has_audio(out) and _mean_volume(out, 1.0, 2.0) > -45.0
    assert media.frame_count(out) == media.frame_count(clips.mute)


def test_the_level_sets_the_beds_loudness(clips, tmp_path):
    soft, loud = tmp_path / "music-1-soft.mp4", tmp_path / "music-1-loud.mp4"
    music.mix_under(clips.quiet, clips.track, soft, gain_db=music.LEVELS["soft"])
    music.mix_under(clips.quiet, clips.track, loud, gain_db=music.LEVELS["loud"])
    assert _mean_volume(loud, 2.0, 2.0) - _mean_volume(soft, 2.0, 2.0) > 5.0


# ---------------------------------------------------------------------------
# The switch, the level, the reel finalized again
# ---------------------------------------------------------------------------

def test_music_off_keeps_the_file_and_flips_the_flag(world):
    _with_music(world)
    mix = copy.deepcopy(world.rm()["mix"])
    run(music.apply_mix(RUN, False))
    got = _state(world)
    assert world.rm()["on"] is False and got["applied"] is False and got["music_url"] is None
    assert got["has_track"] is True and got["track_status"] == "ready"
    assert poller._abs_from_rel(mix["rel"]).is_file()
    assert music.served_mix(world.md())["path"] == poller._abs_from_rel(mix["rel"])
    assert music.current_id(world.md()) is None and run(music.current_bytes(world.md())) is None
    assert run(world.store.due()) == []
    # Back on: the same mix, nothing made again.
    run(music.apply_mix(RUN, True))
    assert world.rm()["mix"]["id"] == mix["id"] and _state(world)["applied"] is True


def test_remove_starts_the_panel_over_and_keeps_the_files(world):
    _with_music(world)
    rel = world.rm()["mix"]["rel"]
    run(music.apply_mix(RUN, False, remove=True))
    got = _state(world)
    assert got["on"] is False and got["has_track"] is False and got["track_status"] is None
    assert poller._abs_from_rel(rel).is_file()
    with pytest.raises(HTTPException) as e:
        run(music.apply_mix(RUN, True))
    assert e.value.status_code == 409


def test_a_level_makes_the_mix_again_and_keeps_the_one_before(world):
    _with_music(world)
    first = world.rm()["mix"]
    run(music.apply_mix(RUN, True, "loud"))
    second = world.rm()["mix"]
    assert second["id"] != first["id"] and second["made"] == "job-1|loud" and second["level"] == "loud"
    assert world.rm()["level"] == "loud" and _state(world)["applied"] is True
    run(music.apply_mix(RUN, True, "soft"))
    third = world.rm()["mix"]
    names = sorted(p.name for p in world.comp("").glob("music-*.mp4"))
    # The new one and the one a page may still be playing; older ones go.
    assert names == sorted(Path(m["rel"]).name for m in (second, third))
    # final*.mp4 is never touched.
    assert world.comp("final-2.mp4").is_file()
    with pytest.raises(HTTPException) as e:
        run(music.apply_mix(RUN, True, "deafening"))
    assert e.value.status_code == 400


def test_apply_without_a_track_is_409(world):
    with pytest.raises(HTTPException) as e:
        run(music.apply_mix(RUN, True))
    assert e.value.status_code == 409


def test_a_reel_finalized_again_gets_its_music_again(world):
    _with_music(world)
    old = world.rm()["mix"]
    md = world.md()
    md.update(composition_path=world.reel(3, world.clips.quiet), composition_seq=3, composition_good_seq=3,
              composition_plan={"seq": 3, "total_frames": 180})
    got = _state(world)
    assert got["applied"] is False and got["mixing"] is True and got["music_url"] is None
    assert music.current_id(md) is None                  # the old reel's music is not this reel's
    assert [r["id"] for r in run(world.store.due())] == [RUN]
    run(music.poll_tracks())
    new = world.rm()["mix"]
    assert new["for_seq"] == 3 and new["id"] != old["id"] and new["id"].startswith("3-")
    assert _state(world)["applied"] is True and "&v=3&" in _state(world)["music_url"]


def test_a_failed_mix_is_left_for_a_press_to_retry(world, monkeypatch):
    run(music.start_track(RUN, "slow jazz piano"))
    _done(world)
    calls: list = []
    real = music.mix_under

    def broken(*a, **k):
        calls.append(a)
        raise media.MediaError("ffmpeg music under final-2.mp4 failed: boom")

    monkeypatch.setattr(music, "mix_under", broken)
    run(music.poll_tracks())
    got = _state(world)
    assert len(calls) == 1 and got["mix_error"] == music.MIX_FAILED and got["mixing"] is False
    assert got["track_status"] == "ready" and got["applied"] is False
    run(music.poll_tracks())
    assert len(calls) == 1 and run(world.store.due()) == []        # not retried every tick
    with pytest.raises(HTTPException) as e:
        run(music.apply_mix(RUN, True))
    assert e.value.status_code == 500 and e.value.detail == music.MIX_FAILED and len(calls) == 2
    monkeypatch.setattr(music, "mix_under", real)
    run(music.apply_mix(RUN, True))
    assert _state(world)["applied"] is True and _state(world)["mix_error"] is None


def test_a_new_vibe_keeps_the_music_until_it_is_ready(world):
    _with_music(world)
    first = world.rm()["mix"]
    run(music.start_track(RUN, "corporate"))
    got = _state(world)
    assert got["track_status"] == "generating" and got["applied"] is True
    assert got["track_brief"] == "slow jazz piano" and got["music_url"].endswith(f"m={first['id']}")
    # A level change meanwhile is made with the music playing.
    run(music.apply_mix(RUN, True, "soft"))
    assert world.rm()["mix"]["made"] == "job-1|soft"
    _done(world)
    run(music.poll_tracks())
    got = _state(world)
    assert world.rm()["mix"]["made"] == "job-2|soft" and got["applied"] is True
    assert got["track_brief"] == "corporate"


def test_the_bucket_copy_stands_in_for_a_wiped_disk(world, monkeypatch):
    world.backup_answer = ("producer-assets", "k")
    _with_music(world)
    rm = world.rm()
    assert rm["track_storage_job"] == "job-1" and rm["mix"]["bucket"] == "producer-assets"
    mixed = poller._abs_from_rel(rm["mix"]["rel"])
    blob = mixed.read_bytes()
    mixed.unlink()

    async def download(*, storage_bucket, storage_path):
        return blob

    monkeypatch.setattr(asset_storage, "download_bytes", download)
    assert _state(world)["applied"] is True                 # still shown, from the bucket
    assert run(music.current_bytes(world.md())) == blob
    assert music.served_mix(world.md()) == {"path": None, "bucket": "producer-assets", "key": "k"}


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------

def _client(monkeypatch, row: dict) -> TestClient:
    from app.routers import yas_producer as yp

    async def fetch(run_id):
        return copy.deepcopy(row)

    monkeypatch.setattr(yp.th_v2_service, "_fetch_run_row", fetch)
    monkeypatch.setattr(yp.th_v2_service, "_abs_from_rel", poller._abs_from_rel)
    app = FastAPI()
    app.include_router(yp.router)
    return TestClient(app)


def test_the_reel_without_music_is_served_as_before(world, monkeypatch):
    _with_music(world)
    row = {"id": RUN, "metadata": world.md()}
    client = _client(monkeypatch, row)
    base = f"/api/yas-producer/talking-head-v2/{RUN}/composition/file"
    reel = poller._abs_from_rel(world.md()["composition_path"]).read_bytes()
    for url in (f"{base}?v=2", f"{base}?v=2&music=0", f"{base}?v=2&download=1"):
        r = client.get(url)
        assert r.status_code == 200 and r.content == reel and r.headers["cache-control"] == "private, no-cache"
    assert "talking-head-9401.mp4" in client.get(f"{base}?download=1").headers["content-disposition"]
    # A legacy reel still has its ready gate, music or not.
    legacy = copy.deepcopy(world.md())
    legacy.pop("render_owner")
    legacy["composition_status"] = "rendering"
    assert _client(monkeypatch, {"id": RUN, "metadata": legacy}).get(base).status_code == 409


def test_the_music_version_is_served_with_music_1(world, monkeypatch):
    base = f"/api/yas-producer/talking-head-v2/{RUN}/composition/file"
    assert _client(monkeypatch, {"id": RUN, "metadata": world.md()}).get(f"{base}?music=1").status_code == 409
    _with_music(world)
    mixed = poller._abs_from_rel(world.rm()["mix"]["rel"]).read_bytes()
    client = _client(monkeypatch, {"id": RUN, "metadata": world.md()})
    r = client.get(_state(world)["music_url"])
    assert r.status_code == 200 and r.content == mixed and r.headers["cache-control"] == "private, no-cache"
    r = client.get(f"{base}?music=1&download=1")
    assert "talking-head-9401-music.mp4" in r.headers["content-disposition"]


def test_the_music_routes(world, monkeypatch):
    from app.routers import yas_producer as yp

    async def fetch_run(run_id):
        return {"id": run_id, "music": music.public_state(run_id, world.md())}

    monkeypatch.setattr(yp.th_v2_service, "fetch_run", fetch_run)
    app = FastAPI()
    app.include_router(yp.router)
    client = TestClient(app)
    base = f"/api/yas-producer/talking-head-v2/{RUN}/music"
    r = client.post(base, json={"brief": "slow jazz piano"})
    assert r.status_code == 200 and r.json()["music"]["track_status"] == "generating"
    assert client.post(base, json={"brief": "corporate"}).status_code == 409
    assert client.post(base, json={}).status_code == 400
    assert len(world.suno.calls) == 1
    _done(world)
    run(music.poll_tracks())
    r = client.post(f"{base}/apply", json={"on": False})
    assert r.status_code == 200 and r.json()["music"]["on"] is False
    r = client.post(f"{base}/apply", json={"on": True, "level": "loud"})
    assert r.status_code == 200 and r.json()["music"]["level"] == "loud" and r.json()["music"]["applied"]
    assert client.post(f"{base}/apply", json={"on": True, "level": "deafening"}).status_code == 422


# ---------------------------------------------------------------------------
# Elsewhere: the poller's phase, saving, posting
# ---------------------------------------------------------------------------

def test_the_poller_phase_never_breaks_its_loop(monkeypatch):
    async def none():
        return []

    for name in ("_pending_queued", "_pending_rendering_unsubmitted", "_pending_rendering_owned",
                 "_pending_rendering_submitted"):
        monkeypatch.setattr(poller, name, none)
    kicked: list = []

    def broken_kick():
        kicked.append(True)
        raise RuntimeError("boom")

    monkeypatch.setattr(music, "kick", broken_kick)
    run(poller._poll_once())
    assert kicked == [True]


def test_kick_runs_one_poll_at_a_time(monkeypatch):
    calls: list = []

    async def slow_poll():
        calls.append(True)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(music, "poll_tracks", slow_poll)
    monkeypatch.setattr(music, "_poll_task", None)

    async def go():
        a = music.kick()
        b = music.kick()
        await a
        c = music.kick()
        await c
        return a is b, a is not c

    assert run(go()) == (True, True) and len(calls) == 2


def test_saved_current_follows_the_music_shown(world):
    from app.services import yas_talking_head_v2 as svc

    _with_music(world)
    md = world.md()
    mix_id = md["reel_music"]["mix"]["id"]
    assert svc.saved_current({"saved_asset_id": 7}, {**md, "saved_seq": 2, "saved_music": mix_id}) is True
    assert svc.saved_current({"saved_asset_id": 7}, {**md, "saved_seq": 2}) is False   # saved before the music
    run(music.apply_mix(RUN, False))
    assert svc.saved_current({"saved_asset_id": 7}, {**world.md(), "saved_seq": 2}) is True


def test_save_to_assets_saves_the_music_version_shown(world, monkeypatch):
    from app.services import yas_talking_head_v2 as svc

    _with_music(world)
    row = {"id": RUN, "topic": "t", "metadata": world.md()}
    uploaded: list = []
    sql: list = []

    async def fetch(run_id):
        return copy.deepcopy(row)

    async def upload(*, data, path, mime):
        uploaded.append(data)
        return {"storage_bucket": None, "storage_path": None, "local_path": "x.mp4"}

    async def embed(_texts):
        return [None]

    class Cur:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def execute(self, s, params=()):
            sql.append((s, params))

        async def fetchone(self):
            return {"id": 77}

    class Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def cursor(self, *_a, **_k):
            return Cur()

    monkeypatch.setattr(svc, "_fetch_run_row", fetch)
    monkeypatch.setattr(svc, "_abs_from_rel", poller._abs_from_rel)
    monkeypatch.setattr(svc.asset_storage, "upload", upload)
    monkeypatch.setattr(svc, "gemini_embed", embed)
    monkeypatch.setattr(svc, "get_conn", lambda *a, **k: Conn())
    run(svc.save_to_assets(run_id=RUN))
    mixed = poller._abs_from_rel(world.rm()["mix"]["rel"]).read_bytes()
    assert uploaded == [mixed]
    update = next(p for s, p in sql if "UPDATE yas_generations" in s)
    assert update[2].obj == {"saved_seq": 2, "saved_music": world.rm()["mix"]["id"]}
    # Music off: the reel, as before.
    run(music.apply_mix(RUN, False))
    row["metadata"] = world.md()
    run(svc.save_to_assets(run_id=RUN))
    assert uploaded[-1] == poller._abs_from_rel(world.md()["composition_path"]).read_bytes()
    update = [p for s, p in sql if "UPDATE yas_generations" in s][-1]
    assert update[2].obj == {"saved_seq": 2, "saved_music": None}


def test_posting_takes_the_music_version_shown(world, monkeypatch):
    from app.services import approved_media as am
    from app.services import yas_talking_head_v2 as svc

    _with_music(world)

    async def fetch(run_id):
        return {"metadata": copy.deepcopy(world.md())}

    monkeypatch.setattr(svc, "_fetch_run_row", fetch)
    monkeypatch.setattr(svc, "_abs_from_rel", poller._abs_from_rel)
    mixed = poller._abs_from_rel(world.rm()["mix"]["rel"]).read_bytes()
    assert run(am._resolve_media("talking_head_v2", RUN)) == (mixed, "video/mp4")
    run(music.apply_mix(RUN, False))
    data, mime = run(am._resolve_media("talking_head_v2", RUN))
    assert data == poller._abs_from_rel(world.md()["composition_path"]).read_bytes()
