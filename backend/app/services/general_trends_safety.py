"""General Trends — deterministic content screening, before any token is spent.

Runs on RAW items at fetch time (inside ``fetch_all``'s per-unit worker, before
the per-source cap), so nothing sexual, hateful or gory is ever embedded,
stored, scored or shown. That ordering is the whole point: run #1 embedded and
scored 108 war/politics rows and pages like "Ejaculation" / "Erection" / ".xxx"
at full token price and then hid them at query time.

FOUR tiers, not one list — measured on the live corpus (766 sightings /
363 trends, 2026-08-19):

  A. EXPLICIT  title + summary + subreddit + url  -> drop   no innocent usage
  B. HARM      title + summary                    -> drop   brand floor
  C. BAIT      TITLE ONLY                         -> flag   dating / thirst
  D. AVOID     TITLE ONLY                         -> flag   tragedy / politics

Why C and D are TITLE ONLY: a naive relationship word list matched 15 live
sightings and 11 of them were false positives, every single one of them from
matching the *summary* — summaries are third-party news copy:

    google_trends 'curtis brown'  <- "...and wife Kim comment publicly on
                                      drowning death of a 2-year-old"
    bluesky 'Trump cuts US-South Korea military exercises'
                                  <- "...his relationship with Kim Jong Un"
    reddit  'Steve Ballmer/Kawhi situation?' <- "the NBA has been in a hot mess"

Why C and D only FLAG: on the whole corpus tier D fires 35 times with ~3
arguable false positives (~10%). 10% is fine for a soft flag the owner can
reveal with ``include_filtered``; it would be indefensible for a drop. Tiers A
and B fired 29 times with no false positives, so they drop.

Matching rules that fall out of the measurement:

  * word / phrase / stem ALTERNATION with boundaries, never substrings —
    'sex' must not eat Essex/Sussex/sexagenarian/unisex, 'gf' must not eat
    'gfx', 'rape' must not eat 'grape', 'porn' must not eat 'top ornament'.
  * bare 'sex', 'ass', 'hot', 'date', 'nude', 'naked', 'kink', 'strip',
    'horny', 'relationship', 'wife', 'married' are NOT terms — see the
    per-tier comments. They are the false-positive engine.
  * two normalisation variants (spaced + squashed) so p0rn / p.o.r.n /
    p o r n / pooorn are caught while 'top ornament' still cannot be.
  * ``allow_terms`` suppresses any match it overlaps, so the owner fixes the
    next false positive with one env var instead of a deploy.

Measured with this rule set (2026-08-20, re-run of the prototype's corpora):
1 false positive in 59 hand-picked negatives ("Horny Toad lizard" -> flag, so
revealable), 1 miss in 31 positives ("P*RN? no" — a DELETED vowel, which no
word list can recover), and keep 327 / flag 36 / drop 30 over the 393 unique
items of run #1. The prototype scored 328/36/29; the extra drop is ".xxx",
which it missed because it collapsed repeats before matching (see
``normalise_variants``).

Pure: no network, no DB, no config import. Settings are passed in by the
caller so the tests drive this module directly.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from functools import lru_cache
from typing import Iterable, Optional

log = logging.getLogger("archivist.general_trends_safety")

# Verdicts. ``drop`` never reaches the database; ``flag`` is stored with
# safety='flagged', hidden from the default feed and never sent to the scorer.
KEEP = "keep"
FLAG = "flag"
DROP = "drop"

SAFETY_MODES: tuple[str, ...] = ("on", "flag_only", "off")

# The closed set of reason slugs. Callers persist these (general_trends.
# safety_reason), count them (stats[source]["dropped_by"]) and label them in
# the UI, so they stay short and stable. The matched text is NOT part of the
# reason — it is available from ``explain()`` and logged at DEBUG.
SAFETY_REASONS: tuple[str, ...] = ("explicit", "harm", "bait", "avoid_topic")

# --------------------------------------------------------------------------- #
# Normalisation — two variants, matched against both. No substring matching.
# --------------------------------------------------------------------------- #

_ZERO_WIDTH = re.compile(r"[​-‏‪-‮⁠﻿]")
# Arabic combining marks (tashkeel). They are category Mn, which is NOT a \w
# char in Python's re, so a diacritised word would break the \b boundaries.
_AR_MARKS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_AR_ALEF = str.maketrans({"أ": "ا", "إ": "ا",
                          "آ": "ا", "ٱ": "ا",
                          "ى": "ي"})
_QUOTES = str.maketrans({"‘": "'", "’": "'", "ʼ": "'",
                         "´": "'", "`": "'"})
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a",
                       "5": "s", "7": "t", "$": "s", "@": "a", "|": "l"})
_INWORD_SEP = re.compile(r"[\-_./*+~^]+")
# Three or more single characters in a row: 'p o r n' / 'N S F W'. Two is not
# enough — "a b test" and two-letter initialisms are common and innocent.
_SINGLE_RUN = re.compile(r"(?<!\w)(?:\w ){2,}\w(?!\w)")
_RUNS = re.compile(r"(\w)\1+")
_WS = re.compile(r"\s+")


def fold(text: object) -> str:
    """Case / obfuscation folding applied to BOTH the term lists and the text.

    Both sides must go through the same map or a term dies silently: 'rule 34'
    matched nothing in the prototype because the leet map rewrites the text's
    '3'->'e' and '4'->'a' while the term kept its digits.
    """
    s = unicodedata.normalize("NFKC", str(text if text is not None else "")).casefold()
    s = _ZERO_WIDTH.sub("", s)
    s = _AR_MARKS.sub("", s)
    s = s.translate(_AR_ALEF)
    s = s.translate(_QUOTES)
    return s.translate(_LEET)


def normalise_variants(text: object) -> tuple[str, str, str]:
    """(spaced, squashed, deduped).

    spaced    separators become SPACES, then runs of single letters are
              re-joined  -> 'hot-or-not' stays a phrase, 'p o r n' -> 'porn'
    squashed  separators are DELETED                   -> 'p.o.r.n' -> 'porn'
    deduped   squashed with every repeat run collapsed -> 'pooorn'  -> 'porn'

    Two variants because deleting separators would break phrase terms and
    spacing them would miss dot-obfuscation. SPACES are never deleted by any
    variant, which is exactly why 'top ornament' can never become 'porn'.

    Repeats are collapsed ONLY in ``deduped``: doing it up front (as the
    prototype did) silently killed the 'xxx' term, since 'xxx' -> 'xx'.
    """
    s = fold(text)
    spaced = _WS.sub(" ", _INWORD_SEP.sub(" ", s)).strip()
    spaced = _SINGLE_RUN.sub(lambda m: m.group(0).replace(" ", ""), spaced)
    squashed = _WS.sub(" ", _INWORD_SEP.sub("", s)).strip()
    deduped = _RUNS.sub(r"\1", squashed)
    return spaced, squashed, deduped


def compile_terms(terms: Iterable[str]) -> Optional[re.Pattern]:
    """Word / phrase / stem list -> one anchored alternation (None when empty).

        'porn'        exact word     \\bporn\\b
        'sex tape'    phrase         \\bsex\\s+tape\\b
        'masturbat*'  stem           \\bmasturbat\\w*

    Longest-first inside each group so the reported match is the specific one
    ('sex tape', not 'sex'). Boundaries everywhere: this function is the reason
    the Scunthorpe cases pass.
    """
    words: list[str] = []
    stems: list[str] = []
    phrases: list[str] = []
    for raw in terms:
        t = fold(raw).strip()
        if not t:
            continue
        if t.endswith("*"):
            stems.append(re.escape(t[:-1]))
        elif " " in t:
            phrases.append(r"\s+".join(re.escape(p) for p in t.split()))
        else:
            words.append(re.escape(t))
    parts: list[str] = []
    if phrases:
        parts.append(r"\b(?:%s)\b" % "|".join(sorted(phrases, key=len, reverse=True)))
    if stems:
        parts.append(r"\b(?:%s)\w*" % "|".join(sorted(stems, key=len, reverse=True)))
    if words:
        parts.append(r"\b(?:%s)\b" % "|".join(sorted(words, key=len, reverse=True)))
    return re.compile("|".join(parts)) if parts else None


# --------------------------------------------------------------------------- #
# Tier A — EXPLICIT. Hard drop, matched against title + summary + sub + url.
# --------------------------------------------------------------------------- #

EXPLICIT_TERMS: tuple[str, ...] = (
    # sites and services
    "porn", "porno", "pornhub", "pornstar", "xvideos", "xhamster", "redtube",
    "youporn", "spankbang", "onlyfans", "fansly", "chaturbate",
    "camgirl", "cam girl", "camwhore", "webcam girl",
    # markers and genres
    "nsfw", "nsfl", "xxx", "hentai", "rule34", "rule 34", "doujin", "doujinshi",
    "ecchi", "futanari", "yiff", "lolicon", "shotacon",
    # acts
    "blowjob", "handjob", "rimjob", "creampie", "cumshot", "gangbang",
    "deepthroat", "bukkake", "anal sex", "oral sex",
    # 'sex' ONLY in phrases. Bare \bsex\b clears Essex/unisex/sexagenarian on
    # boundaries alone, but it would still kill 'Sex Education season 5',
    # 'sexuality studies', 'sexism in tech', 'sex ratio' — all legitimate.
    "sex tape", "sextape", "sex scene", "sex doll", "sex toy", "sex toys",
    "sex work", "sex worker", "sex workers", "sexting", "sexual content",
    "sexually explicit", "sexual act", "sexual acts", "having sex",
    "sex position", "sex positions", "sex party",
    # stems
    "masturbat*", "ejaculat*", "orgasm*", "prostitut*", "fornicat*",
    # 'kinky' not 'kink' (supply-chain kinks, The Kinks)
    "erotic", "erotica", "bdsm", "kinky", "femdom", "dominatrix",
    # 'strip club' / 'stripper' not 'strip' (comic strip, strip mall)
    "escort service", "escort agency", "brothel", "strip club", "stripper",
    "strippers", "lap dance",
    "milf", "gilf", "dilf", "thirst trap", "thirsttrap", "thirst traps",
    # 'nudes' plural / phrases, not bare 'nude' (nude palette, nude in art)
    "nudes", "nude leak", "nude leaks", "leaked nudes", "nudify",
    "deepfake nude", "deepfake porn",
    "titties", "boobies", "nipple slip", "upskirt", "downblouse", "cameltoe",
    # 'hornypost' not 'horny' — 'Horny Toad' is a lizard. Bare 'horny' lives in
    # tier C, where a false positive is only a flag.
    "hornypost", "horny post", "coomer", "gooning", "gooner", "smut",
    "hooker", "sugar daddy", "sugar baby", "sugar mommy", "revenge porn",
    "bussy", "fleshlight", "dildo", "buttplug", "butt plug", "nude beach",
)

# Deliberately EXCLUDED from tier A, each with the collision that excludes it:
#   sex        Sex Education / sexuality studies / sexism in tech
#   ass        assassin, bass, analysis, Cassie
#   nude       nude colour palette, Nudity in art history
#   naked      naked mole rat, naked eye
#   hot        hot mess, hot takes, hotfix
#   date       save the date, carbon dating, the date of the exam
#   kink       kinks in the supply chain, The Kinks
#   strip      comic strip, strip mall, the Vegas Strip
#   goon       goons (crowd), goon squad
#   cp         cp (copy), CP violation, CP2077
#   horny      Horny Toad lizard   -> tier C instead
#   bikini     bikinibottomtwitter (SpongeBob)
#   single     single-page app, single mother
#   relationship / wife / husband / married  -> 11 of 15 measured live hits
#                                               were false positives

# --------------------------------------------------------------------------- #
# Tier B — HARM. Hard drop, matched against title + summary.
# --------------------------------------------------------------------------- #

HARM_TERMS: tuple[str, ...] = (
    "nigger", "nigga", "faggot", "tranny", "kike", "chink", "gook", "spic",
    "wetback",
    # words, not stems: \bretard\w* would eat 'flame retardant fabric'
    "retard", "retarded",
    "beheading", "beheaded", "decapitat*", "gore video", "snuff film",
    "self harm", "selfharm", "suicide method", "how to kill", "kill yourself",
    "child abuse", "child porn", "csam", "pedophil*", "paedophil*",
    "zoophil*", "bestiality", "incest",
    # \brape\b cannot match grape/drape/scrape — that is what the boundary is for
    "rape", "raping", "rapist", "molest*",
    "sexual assault", "sexual abuse", "child sexual", "sexual violence",
    "human trafficking", "sex trafficking",
)

# --------------------------------------------------------------------------- #
# Tier C — BAIT (dating / thirst / relationship). Soft flag, TITLE ONLY.
# Frames and phrases, never bare nouns: the bare-noun version of this list is
# the one that scored 11 false positives out of 15 live hits.
# --------------------------------------------------------------------------- #

BAIT_PATTERNS: tuple[str, ...] = (
    # the possessive frame is what makes 'gf' / 'bf' safe to use at all
    # ('gfx card', 'BF Goodrich', 'bfs vs dfs' never match)
    r"\b(?:my|his|her|your|their|ur)\s+(?:new\s+|hot\s+|ex[\s-]?)?"
    r"(?:gf|bf|girlfriend|boyfriend|situationship|fwb)\b",
    r"\bex[\s-]?(?:gf|bf|girlfriend|boyfriend|wife|husband)\b",
    r"\b(?:gf|bf)\s+(?:vs\.?|versus|and|or)\s+(?:gf|bf)\b",
    r"\brate\s+my\b",
    r"\bam\s+i\s+(?:hot|pretty|ugly|attractive|date\s?able)\b",
    r"\bhot\s+or\s+not\b",
    r"\bsmash\s+or\s+pass\b",
    r"\bwould\s+you\s+(?:date|smash)\b",
    r"\bhow\s+to\s+(?:get|pull|land|talk\s+to|rizz\s+up)\s+(?:a\s+|the\s+)?"
    r"(?:girl|girls|guy|guys|gf|bf|woman|women|man|men|baddie)\b",
    r"\brizz(?:ed|ing|ler|z)?\b",          # does NOT match 'Rizzo from Grease'
    r"\bpick\s?up\s+lines?\b",
    r"\bsituationship\b",
    r"\b(?:tinder|bumble|hinge|grindr|okcupid|badoo)\b",
    r"\bdating\s+(?:app|apps|advice|profile|show|scene|life|coach)\b",
    r"\b(?:blind|first|second|third)\s+date\b",
    r"\bdate\s+(?:a|an|my|your|his|her|him|them|this)\b",   # not 'the date'
    r"\bdon'?t\s+(?:like\s+to\s+)?date\b",
    r"\bcheat(?:ed|ing)\s+on\s+(?:my|his|her|their)\b",
    # 'red flags' only in a relationship frame — the bare form is 'red flag laws'
    r"\b(?:dating|relationship)\s+(?:red|beige|green)\s+flags?\b",
    r"\b(?:red|beige|green)\s+flags?\s+(?:in|of)\s+(?:a\s+)?"
    r"(?:guy|guys|girl|girls|man|men|woman|women|partner|date|relationship)\b",
    r"\bsimp(?:ing|ed|s)?\b",
    r"\b(?:husband|wife|boyfriend|girlfriend)\s+material\b",
    r"\bwife\s+guy\b",
    r"\bmy\s+(?:crush|type)\b",
    r"\bthirst(?:y)?\s+(?:trap|traps|post|posting)\b",
    r"\bhook\s?up(?:s)?\b",
    # 'body count' only when possessive — the bare form is Call of Duty
    r"\b(?:her|his|my|your)\s+body\s+count\b",
    r"\b(?:nude|naked)\s+(?:selfie|selfies|pic|pics|photo|photos|leak|leaks)\b",
    r"\bhorny\b",                          # known FP: 'Horny Toad lizard'
    r"\bthicc\b",
    r"\bbaddie\b",
    r"\bbikini\s+(?:body|pics|photo|photos|try\s?on)\b",
    r"\btry\s?on\s+haul\b",
    r"\bglow\s?up\s+(?:transformation|challenge|journey)\b",
    r"\bwho\s+would\s+you\s+choose\b",

    # -- ATTRACTION / APPEARANCE RATING ------------------------------------
    # Added after the owner rejected "'Dream Girl Wallahi' meme" by name: a
    # school's feed must not carry "is she hot" content even when it is a
    # perfectly real, widely-recreated format. Every pattern below is a FRAME
    # around a person, never a bare adjective — "beautiful" alone would eat
    # "beautiful sunset over Abu Dhabi" and "the most beautiful mosques".
    #
    # Deliberately NOT here: propose, proposal, engagement, wedding, couple.
    # The owner explicitly kept the skyscraper-proposal story as ACCEPTABLE
    # ("if it is a news like the sky scrapper thing it is okay") — that format
    # is about the notice/banner people remix, not about attraction.
    r"\bdream\s+(?:girl|girls|guy|guys|boy|boys|woman|women|man|men)\b",
    # "she's" / "shes" / "she is" / "she was" — all four spellings. The
    # contracted form alone missed "she is beautiful challenge".
    r"\b(?:she|he)(?:'?s|\s+is|\s+was)\s+(?:so\s+|too\s+|very\s+)?"
    r"(?:beautiful|gorgeous|sexy|stunning|bad)\b",
    r"\b(?:most|world'?s)\s+(?:beautiful|attractive|handsome)\s+"
    r"(?:girl|girls|woman|women|man|men|guy|guys|person|people)\b",
    r"\b(?:prettiest|hottest|sexiest|baddest)\s+"
    r"(?:girl|girls|woman|women|man|men|guy|guys)\b",
    # "she's a 10 but ..." — a rating format, not a number in a sentence.
    # NOTE: \d CANNOT BE USED ANYWHERE IN THIS MODULE. Every pattern is matched
    # against leet-folded text, and the fold maps 1->i, 0->o, 3->e, 4->a to
    # catch p0rn / h4rd — so "she's a 10 but" reaches the matcher as
    # "shes a io but" and any digit class silently never fires. Match the SHAPE
    # of the rating frame instead; the number is not what makes it one.
    r"\b(?:she|he)(?:'?s|\s+is)\s+an?\s+\w{1,3}\s+but\b",
    r"\brat(?:e|ing)\s+(?:her|him|girls|women|guys|men|people)\b",
    r"\bgyat+\b",                        # gyat / gyatt
    r"\b(?:milf|dilf|gilf)s?\b",
    r"\blooksmax\w*\b",
    r"\bhot\s+girl\s+(?:summer|walk|era|shit)\b",
    r"\bpretty\s+privilege\b",
    r"\bbody\s?count\s+(?:reveal|check)\b",
    r"\bwaifu\b",
    r"\bfemboy\b",
    r"\b(?:booty|butt|boob|thigh)\s+(?:check|challenge|trend|dance)\b",
    r"\btwerk(?:ing|ed)?\b",
)

# --------------------------------------------------------------------------- #
# Tier D — AVOID_TOPIC (tragedy / war / partisan politics). Soft flag, TITLE
# ONLY. This is the token bill: 108 of 363 rows in run #1 were embedded and
# scored, then hidden as sensitivity='avoid'. Flagging them here means they are
# never sent to the model at all.
# Deliberately NOT here: sport results, product launches, box office, awards —
# those are low-FIT, not brand-unsafe, and fit_score is the right tool.
# --------------------------------------------------------------------------- #

AVOID_TOPIC_TERMS: tuple[str, ...] = (
    "killed", "killing", "dead", "dies", "died", "death", "death toll",
    "murder*", "massacre", "shooting", "shooter", "gunman", "stabbed",
    "stabbing", "airstrike", "air strike", "bombing", "bombed", "missile",
    "drone strike", "genocide", "war crime", "hostage", "ceasefire",
    "funeral", "mourns", "mourning", "obituary", "tragedy", "tragic",
    "earthquake", "tsunami", "wildfire", "hurricane", "flooding",
    "plane crash", "sexual harassment",
    "election", "elections", "impeach*", "indicted", "verdict", "lawsuit",
    "president", "senate", "congress", "parliament", "protest", "protests",
    "riot", "riots", "coup", "terror*",
    "hamas", "israel", "gaza", "ukraine", "russia", "putin", "trump", "biden",
    "netanyahu", "zelensky", "west bank", "hezbollah",
)

# --------------------------------------------------------------------------- #
# Arabic explicit terms. 105 of the 240 live x_trends items are Arabic script
# and the English lists cannot see them at all.
# NOT here on purpose: جنس (jins) means "sex" AND "kind/type" and is a
# substring of الجنسية (nationality); نيك is a substring of Arabic spellings of
# "Nicholas". Substring matching on Arabic is exactly as wrong as on English.
# STARTER LIST — wants a native reviewer before it is treated as complete.
# --------------------------------------------------------------------------- #

EXPLICIT_AR: tuple[str, ...] = (
    "سكس",                                    # sks  — "sex"
    "اباحي",                        # ibahi — pornographic
    "اباحية",                  # ibahiya (fem.)
    "عاهرة",                        # 'ahira — prostitute
    "شرموطة",                  # sharmuta — whore
    "قحبة",                              # qahba — whore
    "عارية",                        # 'ariya — naked (fem.)
    "خلاعة",                        # khala'a — obscenity
)
# The definite article plus the one-letter conjunctions/prepositions that glue
# onto a noun in Arabic, longest first so 'wal-' wins over 'wa-'.
_AR_PREFIX = (r"(?:وال|بال|فال|"
              r"كال|لل|ال|"
              r"و|ف|ب|ل|ك)?")
# Up to two trailing letters for the feminine / plural / adjectival endings.
_AR_SUFFIX = r"[ء-ي]{0,2}"
RE_EXPLICIT_AR = re.compile(
    r"\b" + _AR_PREFIX
    + r"(?:" + "|".join(sorted((re.escape(fold(t)) for t in EXPLICIT_AR),
                               key=len, reverse=True)) + r")"
    + _AR_SUFFIX + r"\b")

# --------------------------------------------------------------------------- #
# Subreddit denylist. The ALLOWLIST in GENERAL_TRENDS_SUBREDDITS is the primary
# NSFW control (Reddit's Atom feed exposes no over_18 flag at all — measured on
# r/NSFW_GIF, and .json is 403); this list exists only so that a careless env
# edit cannot open the door. The verdict is always DROP; the tier only names
# the reason.
# --------------------------------------------------------------------------- #

EXPLICIT_SUBREDDITS = frozenset({
    "gonewild", "gonewildaudio", "realgirls", "nsfw", "nsfw_gif", "nsfwgif",
    "porn", "rule34", "hentai", "tiktokthots", "collegesluts", "onlyfans",
    "holdthemoan",
})
HARM_SUBREDDITS = frozenset({
    "watchpeopledie", "watchpeopledieinside", "gore", "eyeblech",
    "makemycoffin",
})
BAIT_SUBREDDITS = frozenset({
    "amiugly", "roastme", "rateme", "truerateme", "dating", "datingoverthirty",
    "relationship_advice", "relationships", "tinder", "bumble", "hingeapp",
    # r/TikTokCringe: 50 of 249 live reddit sightings, definitionally ragebait,
    # a common host for thirst-trap and relationship-drama clips, and every
    # post is one video rather than an internet-wide trend.
    "tiktokcringe",
})
BLOCKED_SUBREDDITS = EXPLICIT_SUBREDDITS | HARM_SUBREDDITS | BAIT_SUBREDDITS

# Prefixes, NOT substrings: r/bikinibottomtwitter is SpongeBob and a substring
# rule would ban it. The +2 keeps short innocent names (r/gwent) out.
_BLOCKED_SUB_PREFIXES = ("nsfw", "gonewild", "porn", "hentai", "rule34", "thot")
_SUB_CLEAN = re.compile(r"[^a-z0-9_]+")


def _clean_sub(sub: object) -> str:
    """Subreddit names are identifiers, so they get plain casefolding — NOT
    ``fold``, whose leet map would rewrite r/rule34 into 'ruleea'.

    A leading 'r/' is stripped first: the pipeline passes a bare name, but a
    display form must not silently become the unblocked 'rtiktokcringe'.
    """
    s = str(sub if sub is not None else "").casefold().strip().lstrip("/")
    if s.startswith("r/"):
        s = s[2:]
    return _SUB_CLEAN.sub("", s)


def subreddit_reason(sub: object) -> Optional[str]:
    """Reason slug for a denied subreddit, or None when it is allowed."""
    s = _clean_sub(sub)
    if not s:
        return None
    if s in EXPLICIT_SUBREDDITS:
        return "explicit"
    if s in HARM_SUBREDDITS:
        return "harm"
    if s in BAIT_SUBREDDITS:
        return "bait"
    if any(s.startswith(p) and len(s) > len(p) + 2 for p in _BLOCKED_SUB_PREFIXES):
        return "explicit"
    return None


# --------------------------------------------------------------------------- #
# Compiled tiers
# --------------------------------------------------------------------------- #

RE_EXPLICIT = compile_terms(EXPLICIT_TERMS)
RE_HARM = compile_terms(HARM_TERMS)
RE_BAIT = re.compile("|".join(BAIT_PATTERNS))
RE_AVOID = compile_terms(AVOID_TOPIC_TERMS)


# --------------------------------------------------------------------------- #
# allow_terms — the live escape hatch (GENERAL_TRENDS_ALLOW_TERMS)
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=32)
def _allow_pattern(terms: frozenset[str]) -> Optional[re.Pattern]:
    """Compile the operator's allow list once per distinct set.

    Terms go through ``normalise_variants`` first so an operator can type
    'hot-or-not' and have it line up with the normalised text.
    """
    return compile_terms(normalise_variants(t)[0] for t in sorted(map(str, terms)))


def _as_frozenset(terms) -> frozenset[str]:
    """Config hands us a list; the contract says frozenset. Accept both, and a
    bare string, rather than exploding inside a fetch worker."""
    if isinstance(terms, frozenset):
        return terms
    if not terms:
        return frozenset()
    if isinstance(terms, str):
        return frozenset({terms})
    return frozenset(str(t) for t in terms)


def _suppressed(match: re.Match, text: str, allow_rx: Optional[re.Pattern]) -> bool:
    """True when an allow term overlaps this match.

    Overlap, not equality, so BOTH escape-hatch shapes work: the blunt one
    (``horny`` — the term is allowed everywhere) and the surgical one
    (``horny toad`` — allowed only inside that phrase).
    """
    if allow_rx is None:
        return False
    lo, hi = match.span()
    for allowed in allow_rx.finditer(text):
        if allowed.start() < hi and lo < allowed.end():
            return True
    return False


def _first_hit(rx: Optional[re.Pattern], texts: Iterable[str],
               allow_rx: Optional[re.Pattern]) -> Optional[str]:
    """First non-suppressed match across ``texts``, or None.

    Scanning continues past a suppressed match, so allowing 'horny' still
    drops 'horny pornstar'.
    """
    if rx is None:
        return None
    for text in texts:
        if not text:
            continue
        for m in rx.finditer(text):
            if not _suppressed(m, text, allow_rx):
                return m.group(0)
    return None


# --------------------------------------------------------------------------- #
# The screen
# --------------------------------------------------------------------------- #

def explain(title: str, summary: str = "", *, subreddit: str = "",
            url: str = "", allow_terms: frozenset[str] = frozenset()
            ) -> tuple[str, str, str]:
    """(verdict, reason, matched) — ``classify`` plus the text that matched.

    For logging, admin views and tests; the pipeline calls ``screen``.
    """
    allow = _as_frozenset(allow_terms)
    allow_rx = _allow_pattern(allow)

    t_sp, t_sq, t_dd = normalise_variants(title)
    s_sp, s_sq, _ = normalise_variants(summary)
    u_sp, _, _ = normalise_variants(url)
    b_sp, b_sq, _ = normalise_variants(subreddit)

    # The sub gate is its own escape hatch: ALLOW_TERMS=tiktokcringe re-opens it.
    sub = _clean_sub(subreddit)
    if sub and sub not in {_clean_sub(t) for t in allow}:
        reason = subreddit_reason(sub)
        if reason:
            return DROP, reason, "r/" + str(subreddit)

    # Tier A — everything we have, including the subreddit name and the URL.
    hit = _first_hit(RE_EXPLICIT,
                     (t_sp, t_sq, t_dd, s_sp, s_sq, b_sp, b_sq, u_sp), allow_rx)
    if hit:
        return DROP, "explicit", hit
    hit = _first_hit(RE_EXPLICIT_AR, (t_sp, s_sp), allow_rx)
    if hit:
        return DROP, "explicit", hit
    # Tier B — title + summary. A slur in third-party copy is still a slur.
    hit = _first_hit(RE_HARM, (t_sp, t_sq, s_sp, s_sq), allow_rx)
    if hit:
        return DROP, "harm", hit
    # Tiers C and D — TITLE ONLY. See the module docstring for the measurement.
    hit = _first_hit(RE_BAIT, (t_sp, t_sq), allow_rx)
    if hit:
        return FLAG, "bait", hit
    hit = _first_hit(RE_AVOID, (t_sp,), allow_rx)
    if hit:
        return FLAG, "avoid_topic", hit
    return KEEP, "", ""


def classify(title: str, summary: str = "", *, subreddit: str = "",
             url: str = "", allow_terms: frozenset[str] = frozenset()
             ) -> tuple[str, str]:
    """(verdict, reason) for raw text.

    verdict is one of 'keep' / 'flag' / 'drop'; reason is one of
    ``SAFETY_REASONS``, or '' when the text is kept.
    """
    verdict, reason, _matched = explain(
        title, summary, subreddit=subreddit, url=url, allow_terms=allow_terms)
    return verdict, reason


def screen(item: dict, *, mode: str = "on",
           allow_terms: frozenset[str] = frozenset()) -> tuple[str, str]:
    """Screen one RawItem: title, summary, url and ``extra['sub']``.

    mode 'on'         drops are drops (default)
    mode 'flag_only'  every drop becomes a flag — "show me what you would hide"
    mode 'off'        no-op, always ('keep', '')

    An unknown mode is treated as 'on': failing closed is the only safe default
    for a brand filter.
    """
    normalised_mode = str(mode or "on").strip().casefold()
    if normalised_mode == "off":
        return KEEP, ""
    if normalised_mode not in SAFETY_MODES:
        log.debug("general-trends safety: unknown mode %r, using 'on'", mode)

    data = item if isinstance(item, dict) else {}
    extra = data.get("extra")
    subreddit = str((extra or {}).get("sub") or "") if isinstance(extra, dict) else ""
    verdict, reason, matched = explain(
        str(data.get("title") or ""),
        str(data.get("summary") or ""),
        subreddit=subreddit,
        url=str(data.get("url") or ""),
        allow_terms=allow_terms,
    )
    if verdict == DROP and normalised_mode == "flag_only":
        verdict = FLAG
    if verdict != KEEP:
        # The matched text is the only way an operator can pick the right
        # GENERAL_TRENDS_ALLOW_TERMS value, so it goes to the log even though
        # it deliberately never goes into the reason slug.
        log.debug("general-trends safety %s (%s: %r) title=%r source=%r",
                  verdict, reason, matched, data.get("title"), data.get("source"))
    return verdict, reason
