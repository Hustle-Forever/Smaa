"""Tests for "Explore is one level only — no chaining" (Poster + Animation).

Explore used to be chainable without limit (explore an explored variant, forever).
Now the Explore affordance is shown ONLY on the root batch's variants
(`batchStack.length <= 1`); the 4 explored riffs (depth ≥ 1) have NO Explore
button. Navigating back to the originals via the breadcrumb re-enables it (the
stack is sliced back to length 1). Same rule in both creators.

Frontend-only change. These are source-string guards (no DB, no network), in the
same style as test_animation_variants / test_remove_logo. Critically, the gate is
a DEDICATED `canExplore` prop — NOT the shared per-card `disabled` — so Save /
Remove-logo / Animate stay usable on explored variants.

Runs standalone (``cd backend && python tests/test_explore_once.py``) and pytest.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_BACKEND = Path(__file__).resolve().parent.parent
if str(_REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(_REPO_BACKEND))

_REPO_ROOT = _REPO_BACKEND.parent


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


def _poster() -> str:
    return _read("frontend/src/components/yas/PosterCreator.tsx")


def _animation() -> str:
    return _read("frontend/src/components/yas/AnimationCreator.tsx")


# --- the depth gate is wired in both creators ---------------------------------

def test_poster_explore_gated_at_root():
    src = _poster()
    # The variant card receives a DEDICATED canExplore prop gated on depth (not
    # folded into the shared `disabled`, which would also kill Save/Remove-logo).
    assert "canExplore={batchStack.length <= 1}" in src
    assert "canExplore: boolean;" in src
    # The Explore button renders only under canExplore.
    assert "{canExplore && (" in src
    assert "Explore →" in src
    # Defense in depth: handleExplore refuses to go past depth 0.
    assert "if (batchStack.length > 1) return;" in src


def test_animation_explore_gated_at_root():
    src = _animation()
    assert "canExplore={batchStack.length <= 1}" in src
    assert "canExplore: boolean;" in src
    # Both Explore buttons (frame bottom-bar + ready-video top-bar) are gated, so
    # at least two canExplore-conditional blocks wrap Explore affordances.
    assert src.count("{canExplore && (") >= 2
    assert "Explore →" in src
    assert "if (batchStack.length > 1) return;" in src


def test_poster_library_delete_preserves_explore_depth():
    # Regression: a Library delete that empties the depth-0 batch must NOT collapse
    # the stack length (which would flip canExplore back on the explored riffs and
    # re-enable chaining). The prune trims only TRAILING-empty levels.
    src = _poster()
    # The naive interior-collapsing filter is gone.
    assert ".filter((batch) => batch.length > 0)" not in src
    # The depth-preserving trailing trim is in place.
    assert "next[next.length - 1].length === 0) next.pop()" in src


def test_explore_gate_does_not_disable_other_card_actions():
    # The gate must NOT be the shared `disabled` prop — Save (poster) and Animate /
    # Remove-logo (both) stay available on explored variants. Guard: the shared
    # disabled wiring is still the in-flight condition, never the depth check.
    poster = _poster()
    # Pinned by SHAPE, not by the exact string: the list of in-flight ids grows
    # whenever the card gains an action (it gained animatingId when "Animate"
    # arrived). What must never appear in it is the depth check.
    gate = poster.split("disabled={phase !== \"idle\"")[1].split("}")[0]
    for flag in ("savingId != null", "removingLogoId != null", "refiningId != null"):
        assert flag in gate, f"card disable lost {flag}"
    assert "batchStack" not in gate and "canExplore" not in gate, "depth leaked into the shared disable"
    # canExplore is a separate prop from disabled.
    assert "disabled={disabled}" in poster  # Explore button still uses in-flight disable when shown
    anim = _animation()
    assert "disabled={busy}" in anim  # card-level in-flight disable, independent of canExplore


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nALL {len(fns)} explore-once tests passed.")
