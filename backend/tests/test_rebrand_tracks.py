"""The Rebrand studio's timeline lanes (frontend/src/lib/rebrandTracks.ts).

The scene's timeline always shows every track: the version's kept layers,
each pattern of the draft (a hidden one too), "Add a pattern" when it has
none, and the icons' lane whenever the server draws icons; the list never
depends on the rail's tab or on what is picked, every lane key is unique even
when a kept layer and a draft pattern share an element id, and the timeline
fits its cap at 1366x768 and 1920x1080. frontend/scripts/check-rebrand-tracks.mjs
checks all of that against the TypeScript itself; this runs it (node 22.6+,
skipped where there is none), like the other check-rebrand-*.mjs tests.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def test_the_timeline_shows_every_track():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed here")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    if tuple(int(x) for x in version.lstrip("v").split(".")[:2]) < (22, 6):
        pytest.skip(f"node {version} cannot strip types")
    out = subprocess.run([node, "--experimental-strip-types", "scripts/check-rebrand-tracks.mjs"],
                         capture_output=True, text=True, timeout=120, cwd=REPO / "frontend")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "cases agree" in out.stdout and "FAIL" not in out.stdout
    for group in ("tab x selection never changes the lanes", "exactly one lane",
                  "the icons' lane: there whenever icons are served", "1366x768 and 1920x1080"):
        assert group in out.stdout, group
