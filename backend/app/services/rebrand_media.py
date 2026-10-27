"""Rebrand experiment — ffmpeg plumbing.

Every helper here is SYNC (subprocess) and is called through `asyncio.to_thread`
by the pipeline, so a multi-second encode never blocks the event loop. They all
use the bundled imageio-ffmpeg binary, like the rest of the backend.

The one rule that makes the final stitch cheap and safe: every segment that goes
into `concat()` is first run through `normalize()`, which forces the SAME
size / fps / pixel format / audio layout. The concat demuxer can then join them
with `-c copy` — no second full re-encode, and no "non-monotonous DTS" or
audio-drift surprises from mixing Veo, Omni and still-card sources.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Literal, Optional

log = logging.getLogger("archivist.rebrand.media")

FPS = 30
AUDIO_RATE = 44100
ENCODE_TIMEOUT_SEC = 600

AudioMode = Literal["keep", "silent", "whoosh"]
# How a source that is not the canvas's shape is put on it: "letterbox"
# (the whole picture, black bars: every segment until now) or "cover" (the
# canvas filled, the overhang cut off: what the reel's Remotion templates do
# with an intro or outro — objectFit: cover — so a bookend is rebranded as
# the reel shows it).
Fit = Literal["letterbox", "cover"]


def _fit_filter(width: int, height: int, fit: Fit) -> str:
    """The scale step that puts a picture on the width x height canvas."""
    if fit == "cover":
        return (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1")
    return (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")


class MediaError(RuntimeError):
    """An ffmpeg step failed; the message carries the stderr tail."""


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(cmd: list[str], *, what: str, timeout: float = ENCODE_TIMEOUT_SEC) -> None:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-600:]
        raise MediaError(f"ffmpeg {what} failed: {tail}")


def _probe(path: Path) -> str:
    """ffmpeg's stream header (stderr of `ffmpeg -i`). With no output file
    ffmpeg exits non-zero but still prints everything we need."""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    return proc.stderr or ""


def probe_duration(path: Path) -> Optional[float]:
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", _probe(path))
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def has_audio(path: Path) -> bool:
    return bool(re.search(r"Stream #\S+.*Audio:", _probe(path)))


def stream_starts(path: Path) -> tuple[Optional[float], Optional[float]]:
    """(video, audio) timestamp of each stream's FIRST DECODED frame, in
    seconds on the timeline a filter graph sees (None: no such stream).

    Their difference is how far the sound starts after the picture. A filter
    chain that zeroes each stream with `PTS-STARTPTS` on its own throws that
    offset away: an uploaded bookend or a Remotion-muxed clip (whose audio
    lags 45 ms) would slide out of sync. Decoded, not read off the packets:
    a remux can leave the AAC priming frame in the stream, and it is the
    first decoded frame that STARTPTS lines up. No ffprobe (the bundled
    ffmpeg ships without it): one frame of each stream through showinfo."""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path),
         "-map", "0:v:0?", "-map", "0:a:0?",
         "-filter:v", "showinfo", "-filter:a", "ashowinfo",
         "-frames:v", "1", "-frames:a", "1", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace", timeout=60,
    )
    starts: dict[str, Optional[float]] = {"showinfo": None, "ashowinfo": None}
    for ln in (proc.stderr or "").splitlines():
        m = re.search(r"Parsed_(a?showinfo)_\d+.*?\bpts_time:\s*(-?[\d.]+(?:e-?\d+)?)", ln)
        if m and starts.get(m.group(1)) is None:
            starts[m.group(1)] = float(m.group(2))
    return starts["showinfo"], starts["ashowinfo"]


def dims_for(aspect: str) -> tuple[int, int]:
    """Output canvas for an aspect. 720p-class, matching what Veo and Omni
    return by default, so nothing is upscaled for no reason."""
    return (1280, 720) if aspect == "16:9" else (720, 1280)


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------

def extract_frame(video: Path, out: Path, *, at: Literal["first", "last"]) -> Path:
    """Write the first or last frame of `video` as a PNG.

    `last` seeks from the end (`-sseof`) and keeps overwriting the single
    output image (`-update 1`), so what lands on disk is the final decoded
    frame — the exact pixel state the next segment has to continue from."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if at == "first":
        cmd = [ffmpeg_exe(), "-y", "-i", str(video), "-frames:v", "1", str(out)]
    else:
        cmd = [
            ffmpeg_exe(), "-y", "-sseof", "-0.3", "-i", str(video),
            "-update", "1", "-q:v", "2", str(out),
        ]
    _run(cmd, what=f"extract {at} frame")
    if not out.exists() or out.stat().st_size == 0:
        raise MediaError(f"no {at} frame extracted from {video.name}")
    return out


def sample_frames(video: Path, out_dir: Path, *, count: int = 4) -> list[Path]:
    """`count` frames spread evenly across the clip (for the self-critique)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = probe_duration(video) or 0.0
    frames: list[Path] = []
    if dur <= 0:
        return frames
    for i in range(count):
        t = dur * (i + 0.5) / count
        out = out_dir / f"sample_{i}.jpg"
        try:
            _run(
                [ffmpeg_exe(), "-y", "-ss", f"{t:.3f}", "-i", str(video),
                 "-frames:v", "1", "-q:v", "3", str(out)],
                what="sample frame",
            )
            if out.exists() and out.stat().st_size > 0:
                frames.append(out)
        except MediaError:
            log.warning("sample frame %d of %s failed", i, video.name)
    return frames


# ---------------------------------------------------------------------------
# Segment normalisation
# ---------------------------------------------------------------------------

def _atempo_chain(speed: float) -> str:
    """atempo only accepts 0.5..2.0 per instance — chain for larger factors."""
    parts: list[str] = []
    s = speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append(f"atempo={s:.5f}")
    return ",".join(parts)


def _whoosh_filter(dur: float) -> str:
    """A pink-noise swell: the same recipe family as yas_sfx's whoosh, stretched
    to the transition length (in fast, peak just before the midpoint, out)."""
    d = max(0.4, dur)
    return (
        f"anoisesrc=d={d:.3f}:c=pink:a=0.35,highpass=f=300,lowpass=f=6500,"
        f"afade=t=in:d={d * 0.4:.3f},afade=t=out:st={d * 0.45:.3f}:d={d * 0.55:.3f},"
        f"aformat=sample_rates={AUDIO_RATE}:channel_layouts=stereo"
    )


def normalize(
    src: Path,
    out: Path,
    *,
    width: int,
    height: int,
    audio: AudioMode = "keep",
    speed: float = 1.0,
    end_frame: Optional[int] = None,
    fit: Fit = "letterbox",
) -> Path:
    """Re-encode one segment onto the shared canvas.

    - letterboxes (never crops) into width x height, constant 30 fps, yuv420p
      — or, `fit` "cover", fills it and cuts the overhang off (a bookend, as
      the reel's templates show it)
    - `speed` > 1 compresses time (used to turn a 4 s generated transition into
      a snappy ~2 s one without losing its first or last frame)
    - `audio`: keep the source track (time-stretched with the video), lay down
      silence, or synthesise a whoosh for a transition. A `keep` source with
      no audio track silently falls back to silence.
    - `end_frame` N keeps only the source's first N frames, and its sound up
      to the same instant (normalize_scene: a Veo tail flash cut off). None
      makes exactly the command it always made, so a clean clip comes out
      byte for byte as before. Each stream keeps its own start: zeroing them
      separately would throw away how far the sound starts after the picture.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    src_dur = probe_duration(src) or 0.0
    trim = atrim = ""
    if end_frame is not None:
        src_fps = video_fps(src) or float(FPS)
        trim = f"trim=end_frame={int(end_frame)},"
        atrim = f"atrim=end={int(end_frame) / src_fps:.6f},"
        if src_dur:
            src_dur = min(src_dur, int(end_frame) / src_fps)
    out_dur = src_dur / speed if speed and src_dur else src_dur
    vf = (
        f"{trim}{_fit_filter(width, height, fit)},"
        f"setpts=PTS/{speed:.5f},fps={FPS},format=yuv420p"
    )
    keep = audio == "keep" and has_audio(src)
    cmd = [ffmpeg_exe(), "-y", "-i", str(src)]
    if keep:
        af = f"{atrim}aresample={AUDIO_RATE}"
        if abs(speed - 1.0) > 1e-3:
            af += "," + _atempo_chain(speed)
        cmd += ["-filter_complex", f"[0:v]{vf}[v];[0:a]{af}[a]", "-map", "[v]", "-map", "[a]"]
    else:
        if audio == "whoosh":
            cmd += ["-f", "lavfi", "-i", _whoosh_filter(out_dur or 2.0)]
        else:
            cmd += ["-f", "lavfi", "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo"]
        cmd += [
            "-filter_complex", f"[0:v]{vf}[v];[1:a]apad[a]",
            "-map", "[v]", "-map", "[a]",
        ]
        if out_dur:
            cmd += ["-t", f"{out_dur:.3f}"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k", "-ar", str(AUDIO_RATE), "-ac", "2",
        "-shortest", "-movflags", "+faststart", str(out),
    ]
    _run(cmd, what=f"normalize {src.name}")
    return out


# ---------------------------------------------------------------------------
# Shot cuts, and the Veo tail flash
# ---------------------------------------------------------------------------
#
# About a third of Veo's 8-second clips end on a flash of ANOTHER shot: the
# last 1-5 frames jump to a wider framing (anim-351, run 815's scene: frames
# 188-191 of 192). Nothing downstream plans for a shot change, and pasting
# the speaker back from such a frame over the old shot is how a version came
# to end on two people. So a scene is normalised without it
# (normalize_scene), in the studio and in the run alike.
#
# A cut is found the plain way: each frame shrunk to CUT_SIDE on its long
# side, grey, the letterbox bars left out, and the mean |difference| from
# the frame before. A new shot is a jump of at least SHOT_CUT_ABS (0-255)
# and SHOT_CUT_REL x the clip's median jump. On the 148 clips on disk this
# finds exactly the 25 flashes, and none in a 4-second clip.

SHOT_CUT_ABS = 12.0
SHOT_CUT_REL = 8.0
CUT_SIDE = 160
# A new shot starting this close to the end (in the source's own frames) is
# a tail flash, trimmed off; one further in is the clip's own jump cut (a
# push-in to a closer framing), which stays.
TAIL_FLASH_MAX = 12
# A column or row of the shrunk frames never brighter than this, in any
# frame, is a letterbox bar (black: nothing changes there).
BAR_LEVEL = 24
_DIFFS: dict[tuple, Any] = {}
DIFFS_KEPT = 64


def _frame_diffs(clip: Path) -> Any:
    """Each frame's mean |grey difference| from the frame before (float32,
    one per decoded frame; the first is 0), inside the picture. Empty when
    the clip cannot be read. Remembered by the file's path, size and time."""
    import cv2
    import numpy as np

    try:
        stat = os.stat(clip)
    except OSError:
        return np.zeros(0, np.float32)
    memo = (str(Path(clip).resolve()), stat.st_size, stat.st_mtime_ns)
    if memo in _DIFFS:
        return _DIFFS[memo]
    small = []
    cap = cv2.VideoCapture(str(clip))
    try:
        while cap.isOpened():
            ok, f = cap.read()
            if not ok:
                break
            g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
            h, w = g.shape
            s = CUT_SIDE / max(h, w)
            small.append(cv2.resize(g, (max(1, int(round(w * s))), max(1, int(round(h * s)))),
                                    interpolation=cv2.INTER_AREA))
    finally:
        cap.release()
    diffs = np.zeros(len(small), np.float32)
    if len(small) > 1:
        stack = np.stack(small)
        lit = stack.max(axis=0) > BAR_LEVEL
        rows, cols = np.flatnonzero(lit.any(axis=1)), np.flatnonzero(lit.any(axis=0))
        if rows.size and cols.size:
            stack = stack[:, rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
        grey = stack.astype(np.float32)
        diffs[1:] = np.abs(grey[1:] - grey[:-1]).mean(axis=(1, 2))
    _DIFFS[memo] = diffs
    while len(_DIFFS) > DIFFS_KEPT:
        _DIFFS.pop(next(iter(_DIFFS)), None)
    return diffs


def _cut_level(diffs: Any) -> float:
    import numpy as np

    median = float(np.median(diffs[1:])) if len(diffs) > 2 else 0.0
    return max(SHOT_CUT_ABS, SHOT_CUT_REL * median)


def shot_cuts(clip: Path) -> list[int]:
    """The first frame of every new shot (a hard cut) in the clip, in its
    own frame numbering. Empty for one shot, or a clip that can't be read."""
    diffs = _frame_diffs(Path(clip))
    level = _cut_level(diffs)
    return [i for i in range(1, len(diffs)) if float(diffs[i]) > level]


def tail_cut(src: Path) -> Optional[int]:
    """The first frame of a Veo tail flash — the earliest new shot in the
    last TAIL_FLASH_MAX frames — or None when the clip ends on its own shot."""
    diffs = _frame_diffs(Path(src))
    n = len(diffs)
    tail = [i for i in shot_cuts(src) if i >= n - TAIL_FLASH_MAX]
    return tail[0] if tail else None


def scene_info_path(clip: Path) -> Path:
    """Where normalize_scene says what it did to `clip`: <clip>.cuts.json."""
    clip = Path(clip)
    return clip.with_name(f"{clip.stem}.cuts.json")


def scene_info(clip: Path) -> Optional[dict]:
    """What normalize_scene wrote beside `clip` ({source_fps, source_cut,
    trimmed_sec, cuts}), or None when it is not there."""
    import json

    try:
        body = json.loads(scene_info_path(clip).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) and isinstance(body.get("cuts"), list) else None


def cuts_of(clip: Path) -> list[int]:
    """The clip's own jump cuts (its frames): what normalize_scene wrote
    beside it, else found (shot_cuts). Empty for one shot, or a clip that
    can't be read."""
    info = scene_info(clip)
    if info is not None:
        return [int(c) for c in info["cuts"]]
    return shot_cuts(Path(clip))


def describe_scene(src: Path, clip: Path, *, info: Optional[Path] = None, whole: bool = False) -> dict:
    """What a normalised scene is, written beside it (as scene_info reads it
    back): the source's frame rate, the source frame its tail flash began at
    (None: no flash — the clip is the whole source), the seconds cut off, and
    the clip's own shot cuts (its frames), which stay in it. `info` writes it
    for another name (normalize_scene describes the clip before it takes its
    place). `whole`: the clip is the whole source whatever its tail (an intro
    or outro, never trimmed; a scene kept past its flash) — nothing cut off,
    and a flash in it is one of its own cuts. Written whole or not at all."""
    import json

    src = Path(src)
    still = src.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
    source_cut = None if (whole or still) else tail_cut(src)
    fps = (None if still else video_fps(src)) or float(FPS)
    frames = len(_frame_diffs(src)) if source_cut is not None else 0
    trimmed = (frames - source_cut) / fps if source_cut is not None and frames else 0.0
    body = {"source_fps": round(float(fps), 3), "source_cut": source_cut,
            "trimmed_sec": round(max(0.0, trimmed), 3), "cuts": shot_cuts(Path(clip))}
    saved = Path(info) if info is not None else scene_info_path(clip)
    saved.parent.mkdir(parents=True, exist_ok=True)
    tmp = saved.with_name(f"{saved.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(body), encoding="utf-8")
    os.replace(tmp, saved)
    return body


def normalize_scene(src: Path, out: Path, *, width: int, height: int) -> Path:
    """normalize() of a talking-head scene (its sound kept), minus a Veo tail
    flash: the source's frames from tail_cut on are left out, and the sound
    with them. A clip with no flash is exactly what normalize() makes of it.
    Writes <out>.cuts.json (describe_scene) BEFORE the clip takes its place,
    so a clip on disk is always described; the clip itself appears whole or
    not at all. The studio and the run both make their scene this way, so
    what the studio previews is what a run edits."""
    src, out = Path(src), Path(out)
    cut = tail_cut(src)
    if cut is not None:
        log.info("[media] %s ends on a %d-frame flash of another shot; trimmed at frame %d",
                 src.name, len(_frame_diffs(src)) - cut, cut)
    tmp = out.with_name(f"{out.stem}.part{out.suffix}")
    normalize(src, tmp, width=width, height=height, audio="keep", end_frame=cut)
    describe_scene(src, tmp, info=scene_info_path(out))
    os.replace(tmp, out)
    return out


def still_to_clip(
    image: Path, out: Path, *, seconds: float, width: int, height: int, fit: Fit = "letterbox",
) -> Path:
    """A card still held for `seconds` (with silence) so it can sit in the
    concat like any other segment — the same fallback the TH v2 stitch uses
    when a bookend never got a Veo animation. `fit` as normalize's."""
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"{_fit_filter(width, height, fit)},"
        f"fps={FPS},format=yuv420p"
    )
    _run(
        [
            ffmpeg_exe(), "-y", "-loop", "1", "-i", str(image),
            "-f", "lavfi", "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo",
            "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]", "-map", "1:a",
            "-t", f"{seconds:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k", "-ar", str(AUDIO_RATE), "-ac", "2",
            "-movflags", "+faststart", str(out),
        ],
        what=f"still→clip {image.name}",
    )
    return out


def fit_video_to_audio(
    edited: Path, original: Path, out: Path, *, width: int, height: int,
) -> Path:
    """Put the ORIGINAL scene's audio back under an edited video.

    Omni does not preserve (or edit) voice, and its output length can drift a
    few frames from the input. Stretch the edited picture to the original
    duration so the mouth stays on the words, then map the original audio.
    The result is already normalised for the concat."""
    out.parent.mkdir(parents=True, exist_ok=True)
    orig_dur = probe_duration(original) or 0.0
    edit_dur = probe_duration(edited) or 0.0
    factor = (orig_dur / edit_dur) if (orig_dur and edit_dur) else 1.0
    vf = (
        f"setpts={factor:.6f}*PTS,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
        f"fps={FPS},format=yuv420p"
    )
    cmd = [ffmpeg_exe(), "-y", "-i", str(edited)]
    if has_audio(original):
        cmd += [
            "-i", str(original),
            "-filter_complex", f"[0:v]{vf}[v];[1:a]aresample={AUDIO_RATE}[a]",
            "-map", "[v]", "-map", "[a]",
        ]
    else:
        cmd += [
            "-f", "lavfi", "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo",
            "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]", "-map", "1:a",
        ]
    if orig_dur:
        cmd += ["-t", f"{orig_dur:.3f}"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k", "-ar", str(AUDIO_RATE), "-ac", "2",
        "-movflags", "+faststart", str(out),
    ]
    _run(cmd, what=f"re-mux audio {original.name}")
    return out


def frame_count(path: Path) -> int:
    """How many video frames `path` decodes to — counted, not read off the
    header (a container's frame count and duration are rounded, and a part
    one frame short is exactly what conform() fixes)."""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace", timeout=ENCODE_TIMEOUT_SEC,
    )
    found = re.findall(r"frame=\s*(\d+)", proc.stderr or "")
    if proc.returncode != 0 or not found:
        raise MediaError(f"counting the frames of {Path(path).name} failed: {(proc.stderr or '')[-300:]}")
    return int(found[-1])


def conform(
    src: Path, like: Path, out: Path, *, width: Optional[int] = None, height: Optional[int] = None,
) -> Path:
    """`src`'s picture made exactly as long as `like`'s, frame for frame, over
    `like`'s own audio (copied, not re-encoded).

    A finished version's part is Omni's clip fitted back to the ORIGINAL
    part (fit_video_to_audio), which comes out a frame short (238 frames
    against 239). Building on that version, Omni must get the same length
    and timing as the first pass had, and the speaker comes back from the
    original frame for frame — so the part is padded by holding its last
    frame, or cut, to the original's count, from its own first frame.
    `width`/`height` letterbox it onto the canvas first (a part cut from a
    final already is). Written whole or not at all."""
    n = frame_count(like)
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = "setpts=PTS-STARTPTS"
    if width and height:
        vf += (f",scale={width}:{height}:force_original_aspect_ratio=decrease,"
               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    vf += (f",fps={FPS},tpad=stop_mode=clone:stop={n},trim=end_frame={n},"
           f"setpts=PTS-STARTPTS,format=yuv420p")
    tmp = out.with_name(out.stem + ".part" + out.suffix)
    _run(
        [
            ffmpeg_exe(), "-y", "-i", str(src), "-i", str(like),
            "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]", "-map", "1:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart", str(tmp),
        ],
        what=f"conform {Path(src).name} to {Path(like).name}",
    )
    import os

    os.replace(tmp, out)
    return out


def split_chunks(src: Path, out_dir: Path, *, max_sec: float = 10.0) -> list[Path]:
    """Omni only accepts ≤10 s of input video. TH scene clips are 8 s Veo
    renders so this is normally a no-op, but uploaded/longer clips are cut into
    equal chunks (re-encoded so every chunk starts on a keyframe)."""
    dur = probe_duration(src) or 0.0
    if dur <= max_sec + 0.05:
        return [src]
    out_dir.mkdir(parents=True, exist_ok=True)
    n = int(dur // max_sec) + (1 if dur % max_sec > 0.05 else 0)
    step = dur / n
    chunks: list[Path] = []
    for i in range(n):
        out = out_dir / f"{src.stem}_chunk{i}.mp4"
        _run(
            [
                ffmpeg_exe(), "-y", "-ss", f"{i * step:.3f}", "-i", str(src),
                "-t", f"{step:.3f}", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "18", "-c:a", "aac", "-b:a", "160k", str(out),
            ],
            what=f"split chunk {i}",
        )
        chunks.append(out)
    return chunks


def concat(segments: list[Path], out: Path) -> Path:
    """Join NORMALISED segments with the concat demuxer (stream copy)."""
    if not segments:
        raise MediaError("nothing to concatenate")
    out.parent.mkdir(parents=True, exist_ok=True)
    list_file = out.with_suffix(".txt")
    lines = []
    for s in segments:
        p = str(s.resolve()).replace("\\", "/").replace("'", r"'\''")
        lines.append(f"file '{p}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        _run(
            [
                ffmpeg_exe(), "-y", "-f", "concat", "-safe", "0",
                "-i", str(list_file), "-c", "copy", "-movflags", "+faststart",
                str(out),
            ],
            what="concat",
        )
    finally:
        try:
            list_file.unlink(missing_ok=True)
        except OSError:
            pass
    return out


def slice_clip(
    src: Path, out: Path, *, start: float, end: Optional[float] = None,
) -> Optional[Path]:
    """A [start, end) span of an already-normalised clip, re-encoded.

    Re-encoding rather than stream-copying is deliberate: a copy can only cut
    on a keyframe, so a 0.8 s tail asked for by the transition planner would
    silently become whatever the nearest keyframe allowed, and the graphic
    covering the seam would no longer line up with the seam. Returns None when
    the span is empty, so callers can drop the piece instead of concatenating
    a zero-length file."""
    dur = probe_duration(src) or 0.0
    start = max(0.0, float(start))
    stop = dur if end is None else min(float(end), dur)
    if stop - start <= 0.02:
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    # `-ss` AFTER `-i` decodes from the head and discards, which is slower but
    # lands on the exact requested instant in BOTH streams. Placing it before
    # `-i` seeks by packet instead, and an audio packet is a whole AAC frame:
    # the resumed shot came back 35 ms early, so the voice ran ahead of the
    # lips for the rest of the reel. These clips are seconds long; accuracy is
    # worth more than the seek.
    cmd = [
        ffmpeg_exe(), "-y", "-i", str(src),
        "-ss", f"{start:.3f}", "-t", f"{stop - start:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k", "-ar", str(AUDIO_RATE), "-ac", "2",
        "-movflags", "+faststart", str(out),
    ]
    _run(cmd, what=f"slice {src.name}")
    return out if out.exists() and out.stat().st_size > 0 else None


def mux_seam_audio(
    video: Path,
    out: Path,
    *,
    intro: Optional[Path],
    intro_from: float,
    main: Path,
    cut_at: float,
    duration: float,
) -> Path:
    """Replace a rendered seam's audio with one built from the same sources.

    The renderer muxes its own AAC track, and that track carries encoder
    priming — measured at ~45 ms here, which puts the speaker's voice behind
    her lips for the whole shot and then snaps back into sync at the next
    join. Audio assembled here is locked to the video by construction: the
    card's own tail, then the shot from its first sample at exactly the cut.

    The video stream is copied, never re-encoded, so this costs nothing and
    cannot alter a frame."""
    out.parent.mkdir(parents=True, exist_ok=True)
    post = max(0.05, duration - cut_at)
    cmd = [ffmpeg_exe(), "-y", "-i", str(video)]
    parts: list[str] = []
    filters: list[str] = []

    if intro is not None and has_audio(intro):
        cmd += ["-i", str(intro)]
        idx = len(parts) + 1
        # apad then a hard atrim forces EXACTLY cut_at seconds. An mp4's audio
        # stream routinely ends a little before its video does — the intro here
        # was 23 ms short — and without the pad that shortfall slides the
        # shot's dialogue earlier than the cut it is supposed to land on.
        filters.append(
            f"[{idx}:a]atrim=start={max(0.0, intro_from):.3f}:"
            f"end={max(0.0, intro_from) + cut_at:.3f},"
            f"asetpts=PTS-STARTPTS,aresample={AUDIO_RATE},apad,"
            f"atrim=duration={cut_at:.3f},asetpts=PTS-STARTPTS[a{idx}]"
        )
        parts.append(f"[a{idx}]")
    else:
        cmd += ["-f", "lavfi", "-t", f"{cut_at:.3f}",
                "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo"]
        parts.append(f"[{len(parts) + 1}:a]")

    if has_audio(main):
        cmd += ["-i", str(main)]
        idx = len(parts) + 1
        filters.append(
            f"[{idx}:a]atrim=start=0:end={post:.3f},"
            f"asetpts=PTS-STARTPTS,aresample={AUDIO_RATE},apad,"
            f"atrim=duration={post:.3f},asetpts=PTS-STARTPTS[a{idx}]"
        )
        parts.append(f"[a{idx}]")
    else:
        cmd += ["-f", "lavfi", "-t", f"{post:.3f}",
                "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo"]
        parts.append(f"[{len(parts) + 1}:a]")

    filters.append(f"{''.join(parts)}concat=n={len(parts)}:v=0:a=1[a]")
    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-ar", str(AUDIO_RATE), "-ac", "2",
        "-movflags", "+faststart", str(out),
    ]
    _run(cmd, what=f"mux seam audio {video.name}")
    return out


def _stream_rates(path: Path) -> list[float]:
    """The first video stream's header rates, in order: the average frame
    rate ("24 fps", "29.97 fps", "1k fps") then tbr. Either may be missing."""
    head = _probe(path)
    line = next((ln for ln in head.splitlines() if re.search(r"Stream #\S+.*Video:", ln)), "")
    rates: list[float] = []
    for unit in ("fps", "tbr"):
        m = re.search(rf"(\d+(?:\.\d+)?)(k?) {unit}\b", line)
        if m:
            val = float(m.group(1)) * (1000.0 if m.group(2) else 1.0)
            if val > 0:
                rates.append(val)
    return rates


def video_size(path: Path) -> Optional[tuple[int, int]]:
    """The first video stream's (width, height) from its header, or None."""
    head = _probe(path)
    line = next((ln for ln in head.splitlines() if re.search(r"Stream #\S+.*Video:", ln)), "")
    m = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line)
    return (int(m.group(1)), int(m.group(2))) if m else None


def video_fps(path: Path) -> Optional[float]:
    """The first video stream's own frame rate from its header (average rate,
    else tbr). None when absent."""
    rates = _stream_rates(path)
    return rates[0] if rates else None


def video_seconds(path: Path) -> float:
    """Length of the VIDEO stream alone, by counting decoded frames.

    `probe_duration` reads the container, which reports the longest stream. At
    a seam the two disagree — a muxed clip can carry 3.000 s of video under
    3.019 s of audio — and joining by the wrong one is what slides a reel out
    of sync.

    The count is divided by the clip's OWN frame rate, not by 30: raw Veo
    clips are 24 fps, and 192 frames / 30 called an 8 s clip 6.4 s long. The
    header can disagree with itself (a concat_exact output reads "30.51 fps,
    30 tbr"), so the rate that agrees with the decode's own end time wins;
    that end time (10 ms resolution) is the answer when no rate does (VFR)."""
    out = subprocess.run(
        [ffmpeg_exe(), "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    ).stderr
    frames = re.findall(r"frame=\s*(\d+)", out)
    if not frames:
        return probe_duration(path) or 0.0
    n = int(frames[-1])
    rates = _stream_rates(path) + [float(FPS)]
    stamps = re.findall(r"time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if not stamps or n <= 0:
        return n / rates[0]
    h, m, s = stamps[-1]
    end = int(h) * 3600 + int(m) * 60 + float(s)

    def _agrees(length: float) -> Optional[float]:
        best = min(rates, key=lambda r: abs(n / r - length))
        # ffmpeg truncates the printed time to 10 ms; allow that plus half a frame.
        return n / best if abs(n / best - length) <= 0.5 / best + 0.011 else None

    found = _agrees(end)
    if found is not None:
        return found
    # The printed end is measured from the FILE's start, which is the earlier
    # of the two streams: a picture that starts 0.12 s after its sound read
    # 0.12 s long (4 frames of frozen tail in a reel). Measure from its own
    # first frame instead.
    v0, _ = stream_starts(path)
    if v0:
        found = _agrees(end - v0)
        if found is not None:
            return found
        return max(0.0, end - v0)
    return end


def concat_exact(
    segments: list[Path], out: Path,
    *, width: Optional[int] = None, height: Optional[int] = None,
) -> Path:
    """Join segments through the concat FILTER, re-encoding once.

    ``concat()`` above stream-copies, which is fast but joins the video and
    audio streams end to end independently. AAC encodes in 1024-sample frames,
    so a segment's audio track finishes up to 21 ms before its video does, and
    every join hands that shortfall to the next segment's audio: by the third
    piece the voice is audibly ahead of the lips. The filter re-times both
    streams together, so the result is exact no matter what the pieces do.

    Costs one re-encode of a short reel, which is worth it at a seam whose
    whole purpose is that the join is invisible."""
    if not segments:
        raise MediaError("nothing to concatenate")
    if len(segments) == 1:
        return segments[0]
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_exe(), "-y"]
    for seg in segments:
        cmd += ["-i", str(seg)]
    chains, labels = [], []
    for i, seg in enumerate(segments):
        # The concat filter advances each stream by that stream's own length,
        # so a segment whose audio outruns its video pushes every later
        # segment's sound out of step with its picture. Pinning the audio to
        # this segment's exact video length — padding it when short, cutting it
        # when long — makes the join exact whatever the pieces arrived as.
        vd = video_seconds(seg)
        # Conform here rather than in a pass of its own. The renderer writes
        # full-range yuvj420p and everything else is limited-range yuv420p, and
        # re-encoding the odd one out beforehand cost a frame every time — 33 ms
        # of picture silently removed from under audio that kept its length.
        vf = f"fps={FPS},setpts=PTS-STARTPTS"
        if width and height:
            vf += (f",scale={width}:{height}:force_original_aspect_ratio=decrease,"
                   f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")
        vf += ",setsar=1,format=yuv420p"
        chains.append(f"[{i}:v]{vf}[v{i}]")
        if has_audio(seg):
            chains.append(
                f"[{i}:a]aresample={AUDIO_RATE},asetpts=PTS-STARTPTS,apad,"
                f"atrim=duration={vd:.4f},asetpts=PTS-STARTPTS[a{i}]"
            )
        else:
            # A silent segment still needs a track, or concat refuses the set.
            chains.append(
                f"anullsrc=r={AUDIO_RATE}:cl=stereo,"
                f"atrim=duration={vd:.4f}[a{i}]"
            )
        labels.append(f"[v{i}][a{i}]")
    chains.append(f"{''.join(labels)}concat=n={len(segments)}:v=1:a=1[v][a]")
    cmd += [
        "-filter_complex", ";".join(chains),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE), "-ac", "2",
        "-movflags", "+faststart", str(out),
    ]
    _run(cmd, what="concat (exact)")
    return out
