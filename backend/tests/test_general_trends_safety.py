"""Tests for General Trends — deterministic content screening.

Module under test: ``app/services/general_trends_safety.py``.

The corpora below are FROZEN measurements, not invented examples:

  * ``NEGATIVES`` is the hand-picked false-positive corpus (Scunthorpe cases +
    real innocuous live titles). It is the test that must never regress — every
    entry is a word a naive list eats. One known exception ("Horny Toad
    lizard") has its own test that pins it as a *flag*, i.e. revealable.
  * ``POSITIVES`` is the true-positive corpus with the tier each one must land
    in, including the exact live-corpus rows that a summary-matching bait list
    got wrong.

Measured with this rule set on 2026-08-19: 1 false positive in 59 negatives,
1 miss in 31 positives, and keep 327 / flag 36 / drop 30 over the 393 unique
items of run #1.

Everything is pure: no network, no DB, no config.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services import general_trends_safety as gs  # noqa: E402


def _item(title: str, *, summary: str = "", url: str = "",
          sub: str = "", source: str = "reddit") -> dict:
    """A RawItem-shaped dict (see general_trends_sources.make_item)."""
    return {
        "source": source,
        "source_key": "k",
        "title": title,
        "url": url or None,
        "summary": summary,
        "image_url": None,
        "metric_value": None,
        "metric_label": None,
        "published_at": None,
        "region": "global",
        "category": None,
        "extra": {"sub": sub} if sub else {},
    }


# --------------------------------------------------------------------------- #
# The false-positive corpus. This is the test that must never regress.
# --------------------------------------------------------------------------- #

NEGATIVES = (
    # the Scunthorpe family: boundaries, not substrings
    "Essex", "Sussex", "Middlesex", "Wessex", "sexagenarian", "unisex",
    "Sussex Royal", "sexism in tech", "sexuality studies",
    "Sex Education season 5", "Sex and the City reboot",
    "Scunthorpe United", "Cockburn Sound", "Penistone line",
    "assassin creed", "bass guitar", "analysis of the data",
    "therapist appointment", "grape harvest", "drape the flag",
    "flame retardant fabric",
    # 'gf' / 'bf' outside a possessive frame
    "gfx card benchmark", "GFUEL", "G Fuel restock", "BF Goodrich tyres",
    "bfs vs dfs", "gluten free (gf) recipes",
    # words that only read as explicit when you drop the boundary
    "comic strip artist", "strip mall renovation", "The Kinks",
    "kinks in the supply chain", "nude colour palette", "naked mole rat",
    "naked eye", "Nudity in art history", "Nudge theory",
    "bikini bottom twitter",              # SpongeBob, not a substring match
    "top ornament sales",                 # must never squash into 'porn'
    "Dick Van Dyke turns 100",
    # 'date' / 'red flag' / 'body count' outside a relationship frame
    "Save the date", "update the date", "a date with destiny",
    "the date of the exam", "carbon dating of fossils",
    "red flag laws", "Red Flag exercise", "Simply Red",
    "Body count in Call of Duty", "Rizzo from Grease",
    "glow up your CV",
    # real innocuous live titles / on-brand subjects
    "SpongeBob", "Simpsons", "Magna Carta", "Hell Let Loose",
    "Trophy hunting ban", "Arsenal 3-0", "GitHub Copilot update",
    "Piscine at 42 Abu Dhabi",
)


@pytest.mark.parametrize("text", NEGATIVES)
def test_innocent_text_is_kept(text):
    assert gs.classify(text) == ("keep", ""), gs.explain(text)


@pytest.mark.parametrize("text", NEGATIVES)
def test_innocent_text_is_kept_in_the_summary_too(text):
    # Tiers A and B read the summary, so the negatives must survive there too.
    assert gs.classify("a perfectly ordinary title", text) == ("keep", "")


def test_the_one_known_false_positive_is_a_flag_not_a_drop():
    """'Horny Toad lizard' is the single measured FP in 59 negatives.

    It must stay a FLAG so the card is stored and revealable with
    include_filtered, and it must be fixable without a deploy.
    """
    verdict, reason, matched = gs.explain("Horny Toad lizard")
    assert (verdict, reason, matched) == ("flag", "bait", "horny")
    assert gs.classify("Horny Toad lizard",
                       allow_terms=frozenset({"horny"})) == ("keep", "")


# --------------------------------------------------------------------------- #
# The true-positive corpus, by tier
# --------------------------------------------------------------------------- #

POSITIVES = (
    ("OnlyFans model", "drop", "explicit"),
    ("sex tape leak", "drop", "explicit"),
    ("thirst trap compilation", "drop", "explicit"),
    ("leaked nudes of a celeb", "drop", "explicit"),
    ("hentai figure", "drop", "explicit"),
    ("NSFW subreddit", "drop", "explicit"),
    ("Ejaculation", "drop", "explicit"),          # the live Wikipedia page
    (".xxx", "drop", "explicit"),                 # ditto
    ("MASTURBATION study", "drop", "explicit"),
    ("rule 34 art", "drop", "explicit"),
    ("she is a stripper", "drop", "explicit"),
    ("beheading video", "drop", "harm"),
    ("convicted rapist", "drop", "harm"),
    ("child abuse ring", "drop", "harm"),
    # tier A is checked first, so an explicit word wins the reason slug even
    # when the phrase is also tier B — the verdict (drop) is the same either way
    ("child porn ring", "drop", "explicit"),
    ("Fractures Emerge Between GOP's Pro-Pedophilia Wings", "drop", "harm"),
    ("my gf left me", "flag", "bait"),
    ("my bf and i", "flag", "bait"),
    ("ex-girlfriend drama", "flag", "bait"),
    ("rate my outfit", "flag", "bait"),
    ("am i hot or not", "flag", "bait"),
    ("smash or pass", "flag", "bait"),
    ("how to pull a girl", "flag", "bait"),
    ("he has no rizz", "flag", "bait"),
    ("situationship explained", "flag", "bait"),
    ("Tinder date gone wrong", "flag", "bait"),
    ("first date fails", "flag", "bait"),
    ("cheating on my wife", "flag", "bait"),
    ("she is horny", "flag", "bait"),
    ("glow up transformation", "flag", "bait"),
    ("nude selfies leaked", "flag", "bait"),
    # the live-corpus bait row
    ("Tbh, that's why I don't like to date Billionaire's Daughter",
     "flag", "bait"),
    ("Trump threatens to bomb Oman", "flag", "avoid_topic"),
    ("Hayden Panettiere dies at 36", "flag", "avoid_topic"),
    ("Ukraine war updates", "flag", "avoid_topic"),
    ("DOJ deploys election monitors nationwide", "flag", "avoid_topic"),
    ("Ben-Gvir calls for nightly Gaza killings", "flag", "avoid_topic"),
)


@pytest.mark.parametrize("text, verdict, reason", POSITIVES)
def test_positive_corpus_lands_in_the_right_tier(text, verdict, reason):
    assert gs.classify(text) == (verdict, reason), gs.explain(text)


def test_curly_apostrophes_do_not_hide_a_bait_phrase():
    # Feeds emit U+2019, the pattern is written with a straight quote.
    assert gs.classify("that’s why I don’t like to date her")[1] == "bait"


# --------------------------------------------------------------------------- #
# Normalisation — both variants, and the obfuscation they exist for
# --------------------------------------------------------------------------- #

def test_normalise_variants_spaced_squashed_deduped():
    spaced, squashed, deduped = gs.normalise_variants("P.O.R.N")
    assert spaced == "porn"        # separators -> spaces, single-letter run rejoined
    assert squashed == "porn"      # separators deleted
    assert deduped == "porn"
    # spaces are never deleted by any variant — this is what protects
    # 'top ornament' from ever becoming 'porn'
    assert gs.normalise_variants("top ornament")[1] == "top ornament"


@pytest.mark.parametrize("text", [
    "p0rn addiction",          # leet substitution
    "p.o.r.n site down",       # squashed variant
    "p o r n site down",       # single-letter run in the spaced variant
    "P O R N",
    "pooorn",                  # deduped variant
    "s.e.x tape",
    "N S F W content",
    "MASTURBATION study",
    "​p​orn",        # zero-width injection
])
def test_obfuscated_explicit_terms_are_caught(text):
    assert gs.classify(text)[0] == "drop", gs.explain(text)


def test_vowel_deletion_is_a_known_documented_gap():
    """'P*RN' deletes a vowel rather than substituting it.

    A word list cannot recover a deleted letter without matching 'prn' as a
    substring, which would light up on everything. This is the single measured
    miss in 31 positives and it is accepted, not forgotten.
    """
    assert gs.classify("P*RN? no") == ("keep", "")


def test_repeat_collapsing_does_not_kill_the_xxx_term():
    """Regression: the prototype collapsed repeats BEFORE matching, so 'xxx'
    became 'xx' and the term could never fire — and '.xxx' is a real live
    Wikipedia row. Repeats are now collapsed only in the deduped variant."""
    assert gs.classify("XXX movie")[0] == "drop"
    assert gs.classify(".xxx")[0] == "drop"


def test_the_leet_fold_is_applied_to_the_term_lists_too():
    """Regression: 'rule 34' matched nothing because the fold rewrites the
    text's '3'->'e' and '4'->'a' while the term kept its digits."""
    assert gs.classify("rule 34 art")[0] == "drop"
    assert gs.classify("Rule34 gallery")[0] == "drop"


# --------------------------------------------------------------------------- #
# Which fields each tier reads — the measured reason for TITLE ONLY
# --------------------------------------------------------------------------- #

def test_bait_and_avoid_match_the_title_only():
    """The exact live rows a summary-matching bait list got wrong.

    11 of its 15 live hits were false positives and every one came from the
    summary, which is third-party news copy.
    """
    assert gs.classify(
        "curtis brown",
        "Former BYU star Curtis Brown and wife Kim comment publicly on the "
        "drowning death of a 2-year-old") == ("keep", "")
    assert gs.classify(
        "Steve Ballmer/Kawhi situation?",
        "the NBA has been in a hot mess") == ("keep", "")
    assert gs.classify(
        "roberto carlos",
        "reportedly embraces Islam - Souhaila Ettahiri: Moroccan, married"
    ) == ("keep", "")
    # ... while the same words IN THE TITLE still flag
    assert gs.classify("Trump cuts US-South Korea military exercises") == (
        "flag", "avoid_topic")


def test_explicit_and_harm_still_match_the_summary():
    assert gs.classify("a harmless looking title",
                       "watch the full sex tape here") == ("drop", "explicit")
    assert gs.classify("a harmless looking title",
                       "the convicted rapist appealed") == ("drop", "harm")


def test_explicit_matches_the_url_but_bait_does_not():
    assert gs.classify("cute clip", url="https://pornhub.com/view") == (
        "drop", "explicit")
    # a URL slug is not a title: soft tiers must not read it
    assert gs.classify("cute clip", url="https://example.com/hot-or-not") == (
        "keep", "")


# --------------------------------------------------------------------------- #
# Subreddit gate
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sub, blocked", [
    ("TikTokCringe", True), ("tiktokcringe", True), ("r/TikTokCringe", True),
    ("gonewildaudio", True), ("nsfw_gif", True), ("relationship_advice", True),
    ("watchpeopledieinside", True), ("nsfwfunny", True),   # prefix rule
    ("memes", False), ("ProgrammerHumor", False), ("technology", False),
    ("Damnthatsinteresting", False),
    ("bikinibottomtwitter", False),   # a substring rule would ban SpongeBob
    ("gwent", False), ("gwcouples", False), ("", False),
])
def test_subreddit_gate(sub, blocked):
    verdict, _reason = gs.classify("a harmless title", subreddit=sub)
    assert (verdict == "drop") is blocked


def test_subreddit_gate_reason_names_the_tier():
    assert gs.classify("x", subreddit="nsfw_gif") == ("drop", "explicit")
    assert gs.classify("x", subreddit="gore") == ("drop", "harm")
    assert gs.classify("x", subreddit="TikTokCringe") == ("drop", "bait")


def test_subreddit_gate_can_be_reopened_by_allow_terms():
    assert gs.classify("a harmless title", subreddit="TikTokCringe",
                       allow_terms=frozenset({"tiktokcringe"})) == ("keep", "")


def test_a_blocked_subreddit_beats_a_clean_title():
    verdict, reason, matched = gs.explain("wholesome dog video",
                                          subreddit="gonewild")
    assert (verdict, reason) == ("drop", "explicit")
    assert "gonewild" in matched


# --------------------------------------------------------------------------- #
# allow_terms — the live escape hatch (GENERAL_TRENDS_ALLOW_TERMS)
# --------------------------------------------------------------------------- #

def test_allow_terms_blunt_form_removes_a_builtin_term():
    assert gs.classify("she is horny")[0] == "flag"
    assert gs.classify("she is horny", allow_terms=frozenset({"horny"})) == (
        "keep", "")


def test_allow_terms_surgical_form_only_frees_the_phrase():
    allow = frozenset({"horny toad"})
    assert gs.classify("Horny Toad lizard", allow_terms=allow) == ("keep", "")
    assert gs.classify("she is horny", allow_terms=allow)[0] == "flag"


def test_allow_terms_do_not_open_the_door_to_other_terms():
    """Scanning continues past a suppressed match."""
    assert gs.classify("horny pornstar",
                       allow_terms=frozenset({"horny"})) == ("drop", "explicit")


def test_allow_terms_accept_the_shapes_config_can_hand_us():
    for allow in (frozenset({"trump"}), ["trump"], ("trump",), "trump"):
        assert gs.classify("Trump card game", allow_terms=allow) == ("keep", "")
    assert gs.classify("Trump card game", allow_terms=None)[0] == "flag"


def test_allow_terms_are_normalised_like_the_text():
    assert gs.classify("am i hot or not")[0] == "flag"
    assert gs.classify("am i hot or not",
                       allow_terms=frozenset({"Hot-Or-Not"})) == ("keep", "")


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #

def test_mode_on_is_the_default():
    assert gs.screen(_item("OnlyFans model")) == ("drop", "explicit")
    assert gs.screen(_item("OnlyFans model"), mode="on") == ("drop", "explicit")


def test_flag_only_mode_turns_every_drop_into_a_flag():
    assert gs.screen(_item("OnlyFans model"), mode="flag_only") == (
        "flag", "explicit")
    assert gs.screen(_item("wholesome", sub="TikTokCringe"),
                     mode="flag_only") == ("flag", "bait")
    # flags and keeps are untouched
    assert gs.screen(_item("she is horny"), mode="flag_only") == ("flag", "bait")
    assert gs.screen(_item("Essex"), mode="flag_only") == ("keep", "")


def test_off_mode_is_a_no_op():
    for text in ("OnlyFans model", "beheading video", "she is horny",
                 "Trump threatens to bomb Oman"):
        assert gs.screen(_item(text), mode="off") == ("keep", "")
    assert gs.screen(_item("x", sub="gonewild"), mode="off") == ("keep", "")


def test_an_unknown_mode_fails_closed():
    # A typo in the env var must not silently disable the brand filter.
    assert gs.screen(_item("OnlyFans model"), mode="ON") == ("drop", "explicit")
    assert gs.screen(_item("OnlyFans model"), mode="nonsense") == (
        "drop", "explicit")
    assert gs.screen(_item("OnlyFans model"), mode="") == ("drop", "explicit")
    assert gs.screen(_item("OnlyFans model"), mode=None) == ("drop", "explicit")


def test_safety_modes_constant_matches_what_screen_accepts():
    assert gs.SAFETY_MODES == ("on", "flag_only", "off")


# --------------------------------------------------------------------------- #
# screen() over a RawItem
# --------------------------------------------------------------------------- #

def test_screen_reads_title_summary_url_and_extra_sub():
    assert gs.screen(_item("Essex")) == ("keep", "")
    assert gs.screen(_item("cute clip", summary="full sex tape inside")) == (
        "drop", "explicit")
    assert gs.screen(_item("cute clip", url="https://xhamster.com/x")) == (
        "drop", "explicit")
    assert gs.screen(_item("cute clip", sub="TikTokCringe")) == ("drop", "bait")


def test_screen_survives_a_malformed_item():
    for bad in ({}, {"title": None}, {"title": 42}, {"extra": None},
                {"extra": "not-a-dict"}, {"title": "x", "extra": {"sub": None}},
                {"title": "x", "summary": None, "url": None}, None, "junk", 7):
        assert gs.screen(bad)[0] in ("keep", "flag", "drop")


def test_screen_allow_terms_reach_the_classifier():
    item = _item("Horny Toad lizard")
    assert gs.screen(item) == ("flag", "bait")
    assert gs.screen(item, allow_terms=frozenset({"horny"})) == ("keep", "")


# --------------------------------------------------------------------------- #
# Arabic (starter list — 105 of 240 live x_trends items are Arabic script)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    "سكس",                                 # sks
    "سكس فيديو",  # sks video
    "السكس",                     # al-sks (article glued on)
    "#سكس",                                # x_trends hashtag form
    "فيديو اباحي",
    "موقع إباحية",   # alef-hamza folded
])
def test_arabic_explicit_terms_are_dropped(text):
    assert gs.classify(text) == ("drop", "explicit"), gs.explain(text)


@pytest.mark.parametrize("text", [
    "الجنسية الإماراتية",  # nationality
    "أخبار اليوم",   # today's news
    "مدرسة البرمجة",  # coding school
])
def test_innocent_arabic_is_kept(text):
    # 'jins' (sex/kind) and 'nik' are deliberately absent from the list: they
    # are substrings of 'nationality' and of Arabic spellings of 'Nicholas'.
    assert gs.classify(text) == ("keep", ""), gs.explain(text)


# --------------------------------------------------------------------------- #
# Invariants
# --------------------------------------------------------------------------- #

def test_every_pattern_compiles_and_matches_nothing_in_the_empty_string():
    patterns = [gs.RE_EXPLICIT, gs.RE_HARM, gs.RE_BAIT, gs.RE_AVOID,
                gs.RE_EXPLICIT_AR]
    for rx in patterns:
        assert isinstance(rx, re.Pattern)
        assert rx.search("") is None
        assert rx.search(" ") is None
    assert gs.classify("") == ("keep", "")
    assert gs.classify("", "", subreddit="", url="") == ("keep", "")
    assert gs.screen(_item("")) == ("keep", "")


def test_each_tier_list_is_lowercase_and_boundaried():
    for term in gs.EXPLICIT_TERMS + gs.HARM_TERMS + gs.AVOID_TOPIC_TERMS:
        assert term == term.lower(), term
        assert term.strip() == term, term
    for pattern in gs.BAIT_PATTERNS:
        re.compile(pattern)                       # each one compiles alone
        assert pattern.startswith("\\b"), pattern  # never a substring


def test_compile_terms_handles_words_phrases_stems_and_nothing():
    rx = gs.compile_terms(["porn", "sex tape", "masturbat*", "", "  "])
    assert rx.search("hardcore porn") is not None
    assert rx.search("top ornament") is None          # word, not substring
    assert rx.search("a sex   tape leak") is not None  # phrase, any whitespace
    assert rx.search("masturbation") is not None       # stem
    assert gs.compile_terms([]) is None
    assert gs.compile_terms(["", "   "]) is None


def test_reason_is_always_one_of_the_published_slugs():
    texts = list(NEGATIVES) + [t for t, _v, _r in POSITIVES]
    for text in texts:
        verdict, reason = gs.classify(text)
        if verdict == "keep":
            assert reason == ""
        else:
            assert reason in gs.SAFETY_REASONS, (text, reason)
    for sub in ("TikTokCringe", "gore", "gonewild", "memes"):
        _verdict, reason = gs.classify("x", subreddit=sub)
        assert reason in gs.SAFETY_REASONS or reason == ""


def test_classify_never_raises_on_junk_input():
    for value in (None, 0, 3.5, [], {}, b"bytes", "😀😀",
                  "\x00\x01", "a" * 5000):
        verdict, reason = gs.classify(value, value, subreddit=value, url=value)
        assert verdict in ("keep", "flag", "drop")
        assert reason in ("",) + gs.SAFETY_REASONS


def test_explain_returns_the_matched_text_for_the_operator():
    verdict, reason, matched = gs.explain("watch the sex tape leak")
    assert (verdict, reason) == ("drop", "explicit")
    # longest-first alternation: the specific phrase, not the bare word
    assert matched == "sex tape"


# =========================================================================== #
# ATTRACTION / APPEARANCE — added after the owner rejected a real card by name
# =========================================================================== #
# "'Dream Girl Wallahi' meme" was the top-ranked trend in the first live run
# (fit 95, uae 100). The owner's rule: "dream girl or she is beautiful, any
# thing sexual, since this is a professional" — but a news-shaped story like the
# skyscraper proposal is explicitly ACCEPTABLE. So attraction is filtered and
# romance-as-news is not.

ATTRACTION_MUST_FILTER = (
    "'Dream Girl Wallahi' meme",
    "dream girl wallahi",
    "my dream guy checklist",
    "she is beautiful challenge",
    "shes so gorgeous",
    "she's beautiful",
    "he is so bad",
    "the most beautiful woman in the world",
    "prettiest girl trend",
    "rating girls at the mall",
    "gyatt",
    "gyat",
    "hot girl walk",
    "twerk challenge",
    "twerking",
    "looksmaxxing",
    "pretty privilege discourse",
    "she's a 10 but she codes in PHP",
)

# The owner named the first two as things that MUST still come through.
ATTRACTION_MUST_KEEP = (
    "Couple proposes at top of skyscraper with a notice",
    "People put their own promo on the proposal notice",
    "Nihilist Penguin walks toward the mountains",
    "beautiful sunset over Abu Dhabi",
    "the most beautiful mosques in the UAE",
    "a beautiful proof of the Pythagorean theorem",
    "Dream job at 42 Abu Dhabi",
    "the fine print of the new visa rules",
    "Dream Theater announce a tour",
    "Fine Arts department opens",
)


def test_attraction_content_is_filtered():
    for title in ATTRACTION_MUST_FILTER:
        verdict, reason = gs.classify(title)
        assert verdict != "keep", f"should not have passed: {title!r}"
        assert reason == "bait", f"{title!r} -> {reason}"


def test_romance_as_news_and_ordinary_beauty_still_pass():
    """The attraction group is FRAMES around a person, never bare adjectives —
    otherwise it eats 'beautiful sunset' and the proposal story the owner kept."""
    for title in ATTRACTION_MUST_KEEP:
        verdict, reason = gs.classify(title)
        assert verdict == "keep", f"false positive on {title!r} -> {reason}"


def test_attraction_terms_are_flagged_not_dropped():
    """Flag, not drop: these are judgement calls, so they stay revealable behind
    'Show hidden' and killable with GENERAL_TRENDS_ALLOW_TERMS."""
    assert gs.classify("'Dream Girl Wallahi' meme")[0] == "flag"
    assert gs.classify("'Dream Girl Wallahi' meme",
                    allow_terms=frozenset({"dream girl"}))[0] == "keep"


def test_no_pattern_uses_a_digit_class():
    r"""Guard rail. Every pattern is matched against LEET-FOLDED text, and the
    fold maps 1->i, 0->o, 3->e, 4->a so that p0rn and h4rd are caught. That
    means a \d in any pattern silently never fires — "she's a 10 but" arrives
    as "shes a io but". This test exists because that cost a real bug once."""
    import re as _re
    for group in (gs.EXPLICIT_TERMS, gs.HARM_TERMS, gs.BAIT_PATTERNS,
                  gs.AVOID_TOPIC_TERMS):
        for pattern in group:
            assert r"\d" not in pattern, f"digit class can never match: {pattern!r}"
    # and prove the premise rather than just asserting it
    assert _re.search(r"\d", gs.fold("she's a 10 but she codes in PHP")) is None
