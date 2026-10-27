"""
When nobody is picked, the person on a Poster, a Poster + Human or an Animation
frame is Emirati.

  • Create Poster (voxel): both stages say who a figure is when the topic does
    not, and neither asks for a figure where the topic has none.
  • Create Animation: its frames render with the same poster styles, so the
    default figure reaches them through get_poster_style.
  • Poster + Human: the hero is an Emirati national in Emirati dress unless the
    content names or describes someone else; a picked student photo goes
    through template fill, which keeps that face.

Pure prompt assertions: no network, no image is rendered.
"""
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services import poster_prompts as vp                       # noqa: E402
from app.services import yas_poster_prompts as php                  # noqa: E402
from app.services.poster_styles import get_poster_style             # noqa: E402
from app.routers import yas_producer as yp                          # noqa: E402


def _is_emirati_default(text: str) -> bool:
    low = text.lower()
    return (
        "emirati" in low
        and "kandura" in low and "ghutra" in low
        and "abaya" in low and "shayla" in low
    )


def test_voxel_poster_defaults_a_figure_to_emirati():
    assert _is_emirati_default(vp.VOXEL_DEFAULT_FIGURE)
    assert vp.VOXEL_DEFAULT_FIGURE in vp.VOXEL_SCENE_RULES
    assert vp.VOXEL_DEFAULT_FIGURE in vp.POSTER_CONCEPTS_SYSTEM
    assert vp.VOXEL_DEFAULT_FIGURE in vp.GEMINI_STAGE2_PROMPT


def test_the_default_never_asks_for_a_figure_and_yields_to_the_topic():
    low = vp.VOXEL_DEFAULT_FIGURE.lower()
    assert low.startswith("when a person appears"), "a default, not a mandate"
    assert "does not say who they are" in low
    assert "different person wins" in low
    assert "never as a caricature" in low


def test_animation_frames_get_the_default_through_the_voxel_style():
    style = get_poster_style("voxel")
    assert vp.VOXEL_DEFAULT_FIGURE in style.concepts_system
    assert vp.VOXEL_DEFAULT_FIGURE in style.gemini_stage2_system
    # An unknown or legacy key falls back to voxel, so it carries it too.
    assert vp.VOXEL_DEFAULT_FIGURE in get_poster_style("wireframe").concepts_system


def test_poster_human_hero_defaults_to_emirati():
    block = php._HUMAN_HERO_BLOCK
    assert _is_emirati_default(block)
    assert "unless the content names or describes a different person" in block.lower()
    assert "fully visible face" in block.lower()
    # Stage 1 decides the person and carries the block; the single-shot
    # session prompt carries it too.
    assert block in php.POSTER_HUMAN_CONCEPTS_SYSTEM
    assert block in php.build_initial_prompt(topic="Piscine applications open")


def test_poster_human_diversity_keeps_the_hero_emirati():
    low = php.POSTER_HUMAN_CONCEPTS_SYSTEM.lower()
    assert "still emirati unless the content names someone else" in low
    # The old mandate varied ethnicity across the batch, which overrode the default.
    assert "age / gender / ethnicity / dress" not in low


def test_poster_human_chat_helper_defaults_to_emirati():
    low = yp.POSTER_HUMAN_SYSTEM.lower()
    assert "emirati" in low and "kandura" in low and "abaya" in low
    assert "unless the user names or describes a different person" in low
