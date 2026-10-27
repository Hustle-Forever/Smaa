"""
Tests for the Industry Signal follow list (news_store's pure half).

`clean_watch` and `watch_text` touch no database, so this suite runs standalone:

    cd backend && python tests/test_news_watch.py

It is ALSO pytest-compatible (every check is a ``test_*`` function). The
``__main__`` block runs all checks, prints PASS/FAIL per test, and exits
non-zero on any failure.

What it guards:
  • A WEBSITE AND A NAME ARE TOLD APART FROM WHAT WAS TYPED, never by asking.
    The trap is a dot: "Node.js" and "Next.js" are names, "theverge.com" is a
    site, and only an ending websites actually use decides it.
  • A pasted link keeps its host and nothing else: scheme, www, path and query
    all go, because what we can follow is the site, not one article.
  • Every refusal is a sentence a person can read, since the screen prints the
    server's words rather than inventing its own.
  • watch_text carries the load-bearing instruction: a company name on its own
    returns that company's press releases, so the block that holds the names
    says to report what they DID instead.
"""
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services.news_store import (  # noqa: E402
    DEFAULT_SITES,
    MAX_WATCH_CHARS,
    _WATCH_DDL,
    clean_watch,
    resolve_watch,
    watch_sites,
    watch_text,
)


# ---------------------------------------------------------------------------
# 1. Names
# ---------------------------------------------------------------------------

def test_a_company_is_a_name():
    assert clean_watch("Anthropic") == ("name", "Anthropic")


def test_a_subject_is_a_name():
    assert clean_watch("  AI   in schools ") == ("name", "AI in schools")


def test_a_dot_does_not_make_it_a_website():
    """The regression that would bite first: half of what a coding school
    follows has a dot in its name."""
    for value in ("Node.js", "Next.js", "Vue.js"):
        kind, cleaned = clean_watch(value)
        assert (kind, cleaned) == ("name", value), f"{value} was read as a website"


# ---------------------------------------------------------------------------
# 2. Websites
# ---------------------------------------------------------------------------

def test_a_bare_domain_is_a_site():
    assert clean_watch("theverge.com") == ("site", "theverge.com")


def test_a_pasted_link_keeps_only_the_host():
    assert clean_watch("https://www.theverge.com/ai/2026/9/20?x=1") == \
        ("site", "theverge.com")


def test_a_host_is_lowercased_and_stripped_of_www():
    assert clean_watch("WWW.TechCrunch.com") == ("site", "techcrunch.com")


def test_a_scheme_makes_it_a_site_whatever_it_ends_in():
    assert clean_watch("http://news.internal.corp/feed") == ("site", "news.internal.corp")


def test_a_link_to_nothing_that_looks_like_a_host_is_refused():
    for bad in ("http://nope", "https://   "):
        try:
            clean_watch(bad)
        except ValueError as exc:
            assert str(exc), "a refusal must carry a sentence"
        else:  # pragma: no cover - the assert below is the failure
            raise AssertionError(f"{bad!r} was accepted as a website")


# ---------------------------------------------------------------------------
# 3. Refusals people can read
# ---------------------------------------------------------------------------

def test_empty_is_refused_with_a_sentence():
    for blank in (None, "", "   "):
        try:
            clean_watch(blank)
        except ValueError as exc:
            assert "Type a" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("an empty value was accepted")


def test_too_long_is_refused_with_the_limit_in_it():
    try:
        clean_watch("x" * (MAX_WATCH_CHARS + 1))
    except ValueError as exc:
        assert str(MAX_WATCH_CHARS) in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an over-long value was accepted")


# ---------------------------------------------------------------------------
# 4. The instruction the list turns into
# ---------------------------------------------------------------------------

def test_an_empty_list_renders_to_nothing():
    """The caller falls back to the stored brief on empty, so this must not
    invent a block that would search for nothing."""
    assert watch_text([]) == ""
    assert watch_text(None) == ""


def test_names_carry_the_what_they_did_instruction():
    text = watch_text([{"kind": "name", "value": "Anthropic"}])
    assert "Anthropic" in text
    low = text.lower()
    assert "did" in low and "announcements" in low, \
        "a bare company name returns press releases; the instruction is what stops it"


def test_sites_are_a_separate_block_and_never_a_hard_filter():
    text = watch_text([
        {"kind": "name", "value": "OpenAI"},
        {"kind": "site", "value": "theverge.com"},
    ])
    names_block, _, sites_block = text.partition("THESE SITES")
    assert "OpenAI" in names_block and "theverge.com" not in names_block
    assert "theverge.com" in sites_block
    assert "never limit" in sites_block.lower()


def test_blank_values_are_dropped_rather_than_printed_as_empty_bullets():
    assert watch_text([{"kind": "name", "value": "   "}]) == ""


# ---------------------------------------------------------------------------
# 4. The websites we always check
# ---------------------------------------------------------------------------

def test_the_sites_come_out_as_bare_hosts_in_list_order():
    rows = [
        {"kind": "name", "value": "OpenAI"},
        {"kind": "site", "value": "TheVerge.com"},
        {"kind": "site", "value": "techcrunch.com"},
        {"kind": "site", "value": "theverge.com"},   # a second spelling of the first
    ]
    assert watch_sites(rows) == ["theverge.com", "techcrunch.com"]


def test_no_sites_is_an_empty_list_not_none():
    assert watch_sites([]) == []
    assert watch_sites(None) == []
    assert watch_sites([{"kind": "name", "value": "Anthropic"}]) == []


def test_a_name_typed_into_the_websites_box_is_refused_with_a_sentence():
    """Followed as a subject in silence, the person would wait for stories
    from a website that was never searched."""
    try:
        resolve_watch("Anthropic", "site")
    except ValueError as exc:
        assert "website" in str(exc).lower()
        assert "theverge.com" in str(exc), "the sentence shows what a website looks like"
    else:
        raise AssertionError("a name in the websites box was accepted")


def test_a_link_typed_into_the_names_box_is_still_a_website():
    """The text decides. The box only ever adds a refusal, never a relabel."""
    assert resolve_watch("https://www.theverge.com/ai", "name") == ("site", "theverge.com")


def test_no_box_at_all_behaves_like_clean_watch():
    assert resolve_watch("Anthropic", None) == clean_watch("Anthropic")
    assert resolve_watch("theverge.com", "") == clean_watch("theverge.com")


def test_googles_own_ending_is_a_website():
    """blog.google is where Google announces things, and `.google` is a real
    ending the list had never heard of."""
    assert clean_watch("blog.google") == ("site", "blog.google")


def test_the_websites_box_takes_any_website_shaped_address():
    """The names box has to guess from the ending. The websites box does not:
    a person who typed something host-shaped there meant a website."""
    assert resolve_watch("news.some-lab.institute", "site") == ("site", "news.some-lab.institute")
    assert resolve_watch("https://WWW.Some-Lab.Institute/ai", "site") == ("site", "some-lab.institute")


def test_the_default_websites_are_seeded_when_the_table_is_born():
    """A fresh database starts with the three the owner asked for, and only a
    fresh one: the seed sits inside the creation guard, so a list somebody
    emptied on purpose is never refilled by a restart."""
    for site in DEFAULT_SITES:
        assert f"('site', '{site}')" in _WATCH_DDL
    assert {"openai.com", "anthropic.com", "blog.google"} <= set(DEFAULT_SITES)
    create_at = _WATCH_DDL.index("CREATE TABLE news_watch")
    seed_at = _WATCH_DDL.index("('site', 'openai.com')")
    end_if = _WATCH_DDL.index("END IF")
    assert create_at < seed_at < end_if, "the seed must be inside the creation guard"


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = sorted(
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    )
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {name}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
