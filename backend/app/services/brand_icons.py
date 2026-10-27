"""Topic icons for the 42 brand frame: what the speaker is talking about.

The Omni rebrand the owner liked put a piggy bank on screen while the speaker
said "free", then a </> for "code". The brand frame does the same WITHOUT a
model: each scene's own dialogue (from the TH v2 script) is matched against a
keyword table, and every hit becomes an icon cue timed to roughly when the
word is spoken.

Timing: Veo speech is steady, so a word's position in the line (in
characters) maps linearly onto the part of the clip where someone is actually
talking — the VOICED SPAN, measured from the clip's own audio (RMS against its
95th percentile), not the whole clip (there is a lead-in and a tail). The icon
lands (finishes its glitch-in) on the word. Deterministic, offline, free.

The icons are Phosphor "fill" icons vendored in the Remotion composition
(services/remotion/src/brand42/icons.ts); every id here must exist there.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from . import rebrand_media as media

FPS = 30
# What the composition does with a cue (brand42/tokens.ts ICON): glitch in
# over IN_FRAMES, out over the cue's last OUT_FRAMES.
IN_FRAMES = 8
MAX_PER_SCENE = 3
MIN_FRAMES = 36            # 1.2 s on screen at least...
MAX_FRAMES = 75            # ...2.5 s at most, or until the next icon
GAP_FRAMES = 45            # starts at least 1.5 s apart
# Keep off the cuts: a cover wipe uncovers the scene until F+10 and the next
# seam's transition starts up to 18 frames before its cut.
HEAD_CLEAR = 12
TAIL_CLEAR = 20

# (icon, patterns). STRONG words name the topic ("tuition-free", "Piscine");
# WEAK ones are common enough ("start", "time") that they only fill a scene
# that has nothing better. Within a tier, earlier rules win a tie.
STRONG: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("piggy-bank", (r"tuition[- ]free", r"free of charge", r"\bfree\b", r"no (?:tuition|fees?)",
                    r"\bcosts? nothing", r"\bscholarships?\b", r"\baffordab")),
    ("swimming-pool", (r"\bpiscine\b",)),
    ("code", (r"\bcod(?:e|es|ing|er|ers)\b", r"\bprogramm(?:ing|er|ers)?\b", r"\bsoftware\b",
              r"\bdevelop(?:er|ers|ment)\b", r"\balgorithms?\b")),
    ("terminal-window", (r"\bterminal\b", r"\bcommand line\b", r"\blinux\b", r"\bunix\b", r"\bshell\b")),
    ("git-branch", (r"\bgit(?:hub)?\b",)),
    ("robot", (r"\bAI\b", r"artificial intelligence", r"machine learning", r"\brobots?\b")),
    ("users-three", (r"peer[- ]to[- ]peer", r"\bpeers?\b", r"\bteam(?:s|mates?)?\b", r"\bcommunity\b",
                     r"\bcollaborat\w*", r"\btogether\b", r"\bfriends?\b")),
    ("student", (r"\bstudents?\b", r"\blearners?\b", r"\bcadets?\b")),
    ("chalkboard-teacher", (r"\bteachers?\b", r"\blectur\w*", r"\bprofessors?\b", r"\bclassrooms?\b")),
    ("graduation-cap", (r"\bdegrees?\b", r"\bdiplomas?\b", r"\bgraduat\w*", r"\buniversit\w*")),
    ("coins", (r"\bmoney\b", r"\bsalar(?:y|ies)\b", r"\bearn\w*", r"\bfees?\b", r"\bpaid\b")),
    ("globe-hemisphere-east", (r"\bglobal\b", r"\bworld\w*", r"\binternational\b", r"\bcountries\b",
                               r"\bnetwork\b")),
    # Not "Abu Dhabi": it is in the brand name, so nearly every script says it.
    ("map-pin", (r"\bUAE\b", r"\bcampus\w*")),
    ("laptop", (r"\blaptops?\b", r"\bcomputers?\b", r"\bmacs?\b")),
    ("briefcase", (r"\bjobs?\b", r"\binternships?\b", r"\bcareers?\b", r"\bhired?\b", r"\bemploy\w*")),
    ("buildings", (r"\bcompan(?:y|ies)\b", r"\bindustry\b", r"\bstartups?\b")),
    ("handshake", (r"\bpartners?(?:hips?)?\b",)),
    ("trophy", (r"\bwin(?:s|ning|ners?)?\b", r"\bsuccess\w*", r"\bachiev\w*", r"\bawards?\b",
                r"\bchampion\w*")),
    ("chats-circle", (r"\bfeedback\b", r"\bpeer review\w*", r"\bevaluat\w*", r"\bdiscuss\w*")),
    ("puzzle-piece", (r"\bpuzzles?\b", r"\bchalleng\w*", r"\bprojects?\b", r"\bproblems?\b")),
    ("lightbulb", (r"\bideas?\b", r"\binnovat\w*", r"\bcreativ\w*", r"\bsolv\w*")),
    ("clock", (r"\bself[- ]paced\b", r"\b24/7\b", r"\bhours?\b", r"\bschedule\w*")),
    ("hourglass", (r"\bdeadlines?\b", r"\bpatien\w*")),
    ("game-controller", (r"\bgam(?:e|es|ing)\b",)),
    ("shield-check", (r"\bcyber\w*", r"\bsecur\w*")),
    ("clipboard-text", (r"\bappl(?:y|ied|ying|ication|ications)\b", r"\bregist\w*", r"\bsign up\b",
                        r"\benrol\w*")),
    ("certificate", (r"\bcertific\w*", r"\bcredentials?\b")),
    ("infinity", (r"\bunlimited\b", r"\blimitless\b", r"\bno limits?\b")),
    ("barbell", (r"\bplanks?\b", r"\bworkouts?\b", r"\bgym\b", r"\bexercis\w*", r"\bfitness\b")),
    ("smiley", (r"\blaugh\w*", r"\bfun\b", r"\bhappy\b", r"\bsmil\w*")),
    ("hand-waving", (r"^\W*(?:hi|hey|hello)\b", r"\bwelcome\b")),
    ("question", (r"\bmyths?\b", r"\bquestions?\b", r"\bcurious\b")),
    ("coffee", (r"\bcoffee\b",)),
    ("wifi-high", (r"\binternet\b", r"\bonline\b")),
    ("airplane-tilt", (r"\btravel\w*", r"\babroad\b")),
    ("calculator", (r"\bmath\w*",)),
    ("atom", (r"\bscience\b", r"\bphysics\b")),
    ("palette", (r"\bdesign\w*", r"\bart\b")),
)
WEAK: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("book-open", (r"\blearn(?:s|ed|ing)?\b", r"\bstud(?:y|ying|ied)\b", r"\bknowledge\b")),
    ("rocket-launch", (r"\bfuture\b", r"\blaunch\w*", r"\bjourney\b", r"\bstart(?:ed|ing|s)?\b")),
    ("brain", (r"\bbrain\w*", r"\bsmart\b", r"\bthink(?:ing)?\b")),
    ("chart-line-up", (r"\bgrow(?:th|ing)?\b", r"\bimprov\w*", r"\bprogress\w*", r"\blevel up\b")),
    ("target", (r"\bgoals?\b", r"\bfocus\w*")),
    ("magnifying-glass", (r"\bexplor\w*", r"\bdiscover\w*", r"\bresearch\w*")),
    ("heart", (r"\blove\b", r"\bpassion\w*", r"\benjoy\w*")),
    ("star", (r"\bamazing\b", r"\bawesome\b", r"\bbest\b")),
    ("calendar-blank", (r"\bweeks?\b", r"\bmonths?\b")),
    ("clock", (r"\btime\b",)),
    ("sparkle", (r"\bexcit\w*", r"\bmagic\w*")),
    ("users", (r"\bpeople\b", r"\beveryone\b")),
    ("lock-key-open", (r"\banyone\b", r"\bunlock\w*")),
    ("mountains", (r"\bdifficult\w*", r"\btough\b")),
    ("moon-stars", (r"\bnights?\b", r"\bmidnight\b")),
    ("house", (r"\bhome\b",)),
    ("footprints", (r"\bsteps?\b",)),
    ("compass", (r"\bguid\w*", r"\bdirection\w*")),
    ("microphone", (r"\binterview\w*", r"\bpodcast\w*")),
    ("gift", (r"\bgifts?\b", r"\bprizes?\b")),
    ("seal-check", (r"\bverified\b", r"\bofficial\b")),
    ("cpu", (r"\bhardware\b", r"\bprocessors?\b")),
)

# NAMES: what people CALL an icon, for the Rebrand studio's library search
# and the icon director's picks (rebrand_harness/icon_catalog.py): (display
# name, icon id, aliases). One row per 42 pick plus a few from the full library (their
# ids are in backend/app/assets/brand42/phosphor_fill.json, not necessarily in
# the composition's icons.ts). Aliases are lower case and unique across the
# table once normalised (lower case, "-" read as a space). The brand frame
# never reads this table.
NAMES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # Money
    ("Free", "piggy-bank", ("tuition free", "no fees", "free of charge", "piggy bank", "savings",
                            "scholarship", "affordable")),
    ("Money", "money", ("cash", "banknote", "dollars")),
    ("Salary", "coins", ("coins", "fees", "earn", "earnings", "income", "pay")),
    ("Gift", "gift", ("gifts", "prize", "prizes", "present")),
    ("Math", "calculator", ("maths", "calculator", "calculate", "numbers")),
    ("Open to all", "lock-key-open", ("anyone", "unlock", "open access", "no barriers")),
    # Code
    ("Developer", "code", ("code", "coding", "coder", "programming", "programmer", "software",
                           "developers", "dev")),
    ("Terminal", "terminal-window", ("command line", "shell", "linux", "unix", "cli")),
    ("Git", "git-branch", ("github", "version control", "branch")),
    ("Laptop", "laptop", ("computer", "computers", "mac", "pc")),
    ("Hardware", "cpu", ("chip", "processor", "cpu")),
    ("AI", "robot", ("artificial intelligence", "machine learning", "ml", "robot", "robots",
                     "robotics")),
    ("Internet", "wifi-high", ("online", "wifi", "web")),
    ("Cybersecurity", "shield-check", ("cyber", "security", "cyber security", "secure", "safety")),
    # People
    ("Team", "users-three", ("peers", "peer", "peer to peer", "teamwork", "community",
                             "collaboration", "together", "friends")),
    ("People", "users", ("everyone", "group", "crowd")),
    ("Student", "student", ("students", "learner", "learners", "cadet", "cadets")),
    ("Teacher", "chalkboard-teacher", ("teachers", "lecture", "lectures", "professor", "classroom")),
    ("Partners", "handshake", ("partner", "partnership", "deal", "handshake")),
    ("Feedback", "chats-circle", ("discussion", "peer review", "evaluation", "talk")),
    ("Hello", "hand-waving", ("hi", "hey", "welcome", "wave")),
    ("Interview", "microphone", ("podcast", "microphone", "mic")),
    ("Love", "heart", ("passion", "enjoy", "heart", "like")),
    # Learning
    ("Degree", "graduation-cap", ("degrees", "diploma", "graduation", "graduate", "university",
                                  "college")),
    ("Learning", "book-open", ("learn", "study", "knowledge", "reading")),
    ("Idea", "lightbulb", ("ideas", "innovation", "creativity", "creative", "lightbulb")),
    ("Brain", "brain", ("smart", "thinking", "mind")),
    ("Projects", "puzzle-piece", ("project", "challenge", "challenges", "puzzle", "problem solving")),
    ("Question", "question", ("questions", "myth", "myths", "faq", "curious")),
    ("Explore", "magnifying-glass", ("search", "discover", "research")),
    ("Certificate", "certificate", ("certification", "credential", "credentials")),
    ("Science", "atom", ("physics", "atom", "chemistry")),
    # Career
    ("Jobs", "briefcase", ("job", "career", "careers", "internship", "internships", "hired",
                           "employment", "work")),
    ("Companies", "buildings", ("company", "industry", "startup", "startups", "business", "office")),
    ("Success", "trophy", ("win", "winner", "winning", "award", "awards", "achievement", "champion")),
    ("Growth", "chart-line-up", ("grow", "progress", "improve", "level up", "results")),
    ("Goals", "target", ("goal", "focus", "aim", "target")),
    ("Launch", "rocket-launch", ("future", "start", "journey", "kickstart")),
    ("Apply", "clipboard-text", ("application", "applications", "register", "sign up", "enrol",
                                 "enroll", "admission")),
    ("Verified", "seal-check", ("official", "approved", "quality")),
    ("Milestone", "flag", ("finish", "flag", "mission")),
    # Time
    ("Time", "clock", ("hours", "schedule", "self paced", "24/7", "clock")),
    ("Deadline", "hourglass", ("deadlines", "patience", "waiting", "hourglass")),
    ("Calendar", "calendar-blank", ("weeks", "months", "dates")),
    ("Night", "moon-stars", ("nights", "midnight", "late night", "sleep")),
    ("Unlimited", "infinity", ("limitless", "no limits", "forever", "infinite")),
    # Places
    ("Piscine", "swimming-pool", ("pool", "swimming", "swimming pool")),
    ("Global", "globe-hemisphere-east", ("world", "worldwide", "international", "countries")),
    ("Campus", "map-pin", ("location", "uae", "place", "map")),
    ("Home", "house", ("house", "remote")),
    ("Travel", "airplane-tilt", ("abroad", "flight", "plane")),
    ("Direction", "compass", ("guide", "guidance", "compass")),
    ("Tough", "mountains", ("difficult", "hard", "climb", "mountain", "mountains")),
    ("Steps", "footprints", ("step", "first steps", "footsteps")),
    # Fun
    ("Games", "game-controller", ("game", "gaming", "gamer", "video games")),
    ("Magic", "sparkle", ("excited", "exciting", "sparkle", "wow")),
    ("Fitness", "barbell", ("gym", "workout", "exercise", "plank", "sport", "sports")),
    ("Coffee", "coffee", ("cafe", "coffee break")),
    ("Fun", "smiley", ("happy", "smile", "laugh", "joy")),
    ("Design", "palette", ("art", "designer", "drawing")),
    ("Best", "star", ("amazing", "awesome", "favourite", "favorite", "star")),
)


# SYNONYMS: what people TYPE when they look for an icon, beyond the names
# above — the Rebrand studio's one library search (rebrand_harness/
# icon_catalog.find): (phrase, strength, icon ids best first, at most three).
# "strong": the icon shows that idea (found at 0.93 / 0.90 / 0.88 for the
# 1st / 2nd / 3rd id); "related": only nearby (0.74 / 0.72 / 0.70 — under the
# 0.8 a sure match needs, so the studio offers "Generate with AI" beside it).
# Phrases are lower case, unique once normalised and never a NAMES alias; a
# plural or "-ing" form finds its phrase through the search's stems. Every id
# is in backend/app/assets/brand42/phosphor_fill.json (a test checks). The
# brand frame and the icon director's keyword candidates never read this.
SYNONYMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # Learning
    ("school", "strong", ("graduation-cap", "student", "chalkboard-teacher")),
    ("coding school", "strong", ("code", "graduation-cap")),
    ("bootcamp", "strong", ("code", "graduation-cap")),
    ("coding bootcamp", "strong", ("code", "graduation-cap")),
    ("peer learning", "strong", ("users-three",)),
    ("project based learning", "strong", ("puzzle-piece", "code")),
    ("learn by doing", "strong", ("puzzle-piece", "code")),
    ("education", "strong", ("graduation-cap", "book-open")),
    ("free education", "strong", ("piggy-bank", "graduation-cap")),
    ("lesson", "strong", ("chalkboard-teacher", "book-open")),
    ("course", "strong", ("book-open", "graduation-cap")),
    ("curriculum", "strong", ("book-open-text", "list-checks")),
    ("homework", "strong", ("notebook", "pencil-simple")),
    ("quiz", "strong", ("exam", "question")),
    ("study group", "strong", ("users-three", "book-open")),
    ("classmates", "strong", ("users-three", "student")),
    ("alumni", "strong", ("graduation-cap", "users-three")),
    ("library", "strong", ("books", "book-open")),
    ("self learning", "strong", ("book-open", "student")),
    ("lab", "strong", ("flask", "test-tube")),
    ("laboratory", "strong", ("flask", "test-tube")),
    ("tutor", "related", ("chalkboard-teacher", "student")),
    ("mentor", "related", ("chalkboard-teacher", "handshake")),
    ("mentorship", "related", ("chalkboard-teacher", "handshake")),
    ("coach", "related", ("chalkboard-teacher", "target")),
    ("exam stress", "related", ("exam", "smiley-nervous")),
    ("stress", "related", ("smiley-nervous",)),
    # Code
    ("algorithm", "strong", ("code", "flow-arrow", "tree-structure")),
    ("programming language", "strong", ("code", "brackets-curly")),
    ("coding challenge", "strong", ("code", "puzzle-piece")),
    ("debugging", "strong", ("bug",)),
    ("mobile app", "strong", ("device-mobile", "app-window")),
    ("app development", "strong", ("device-mobile", "code")),
    ("website", "strong", ("browser", "globe-simple")),
    ("web development", "strong", ("browser", "code")),
    ("web dev", "strong", ("browser", "code")),
    ("server", "strong", ("hard-drives", "database")),
    ("data", "strong", ("database", "chart-bar")),
    ("big data", "strong", ("database", "chart-bar")),
    ("data science", "strong", ("chart-bar", "database")),
    ("chatbot", "strong", ("robot", "chats-circle")),
    ("tech", "strong", ("laptop", "cpu")),
    ("technology", "strong", ("laptop", "cpu")),
    ("ui design", "strong", ("layout", "palette")),
    ("game development", "strong", ("game-controller", "code")),
    ("game dev", "strong", ("game-controller", "code")),
    ("3d", "strong", ("cube", "cube-transparent")),
    ("electronics", "strong", ("circuitry", "cpu")),
    ("cloud computing", "strong", ("cloud", "hard-drives")),
    ("open source", "related", ("code", "git-branch")),
    ("hackathon", "related", ("code", "trophy", "lightning")),
    ("hacker", "related", ("terminal-window", "code")),
    ("hacking", "related", ("terminal-window", "code")),
    ("3d printing", "related", ("printer", "cube")),
    ("api", "related", ("plugs-connected", "code")),
    # Career
    ("cv", "strong", ("identification-card", "file-text")),
    ("resume", "strong", ("identification-card", "file-text")),
    ("portfolio", "strong", ("briefcase", "folders")),
    ("job interview", "strong", ("handshake", "briefcase")),
    ("networking", "strong", ("handshake", "users-three")),
    ("career fair", "strong", ("briefcase", "buildings")),
    ("recruiter", "strong", ("briefcase", "user-plus")),
    ("recruitment", "strong", ("briefcase", "user-plus")),
    ("hiring", "strong", ("user-plus", "briefcase")),
    ("freelance", "strong", ("laptop", "briefcase")),
    ("freelancer", "strong", ("laptop", "briefcase")),
    ("entrepreneur", "strong", ("rocket-launch", "lightbulb")),
    ("entrepreneurship", "strong", ("rocket-launch", "lightbulb")),
    ("remote work", "strong", ("laptop", "house")),
    ("remote job", "strong", ("laptop", "house")),
    ("work from home", "strong", ("laptop", "house")),
    ("meeting", "strong", ("users-three", "presentation")),
    ("leadership", "strong", ("crown", "flag")),
    ("job offer", "related", ("briefcase", "envelope-simple", "handshake")),
    ("promotion", "related", ("chart-line-up", "trophy")),
    ("skills", "related", ("puzzle-piece", "lightbulb")),
    ("soft skills", "related", ("chats-circle", "users-three")),
    # Campus life
    ("dorm", "strong", ("bed", "house")),
    ("dormitory", "strong", ("bed", "house")),
    ("accommodation", "strong", ("bed", "house")),
    ("housing", "strong", ("house", "bed")),
    ("cafeteria", "strong", ("fork-knife", "coffee")),
    ("canteen", "strong", ("fork-knife", "coffee")),
    ("lunch", "strong", ("fork-knife", "bowl-food")),
    ("dinner", "strong", ("fork-knife", "bowl-food")),
    ("breakfast", "strong", ("coffee", "egg")),
    ("music", "strong", ("music-notes", "music-note", "headphones")),
    ("party", "strong", ("confetti", "balloon")),
    ("celebration", "strong", ("confetti", "cake")),
    ("birthday", "strong", ("cake", "confetti", "gift")),
    ("congratulations", "strong", ("confetti", "trophy")),
    ("congrats", "strong", ("confetti", "trophy")),
    ("open day", "strong", ("door-open", "calendar-blank")),
    ("open house", "strong", ("door-open", "house")),
    ("social media", "strong", ("share-network", "chats-circle")),
    ("thank you", "strong", ("heart", "hands-clapping")),
    ("thanks", "strong", ("heart", "hands-clapping")),
    ("goodbye", "strong", ("hand-waving",)),
    ("bye", "strong", ("hand-waving",)),
    ("halal food", "related", ("bowl-food", "fork-knife")),
    ("motivation", "related", ("fire", "target")),
    # The UAE
    ("ramadan", "strong", ("moon-stars", "star-and-crescent", "mosque")),
    ("eid", "strong", ("star-and-crescent", "moon-stars", "gift")),
    ("iftar", "strong", ("moon-stars", "fork-knife")),
    ("desert", "strong", ("cactus", "sun-horizon")),
    ("language", "strong", ("translate",)),
    ("abu dhabi", "related", ("map-pin", "mosque", "buildings")),
    ("dubai", "related", ("buildings", "map-pin")),
    ("national day", "related", ("flag", "confetti")),
    ("falcon", "related", ("bird",)),
    ("arabic", "related", ("translate",)),
    ("english", "related", ("translate",)),
    # Money and time
    ("budget", "strong", ("wallet", "calculator")),
    ("price", "strong", ("tag", "coins")),
    ("cost", "strong", ("coins", "tag")),
    ("student visa", "strong", ("identification-card", "airplane-tilt")),
    ("passport", "strong", ("identification-card", "airplane-tilt")),
    ("flexible hours", "strong", ("clock", "calendar-blank")),
    ("flexible schedule", "strong", ("calendar-blank", "clock")),
    ("loan", "related", ("hand-coins", "bank")),
    ("expensive", "related", ("coins", "money")),
    ("all night", "related", ("moon-stars",)),
)


def name_rows() -> list[dict]:
    """NAMES as [{name, id, aliases}] in table order."""
    return [{"name": n, "id": i, "aliases": list(a)} for n, i, a in NAMES]


_COMPILED = [
    (tier, icon, re.compile(p, re.IGNORECASE))
    for tier, rules in ((0, STRONG), (1, WEAK))
    for icon, pats in rules
    for p in pats
]


def icon_ids() -> set[str]:
    """Every icon this table can ask for (must all exist in brand42/icons.ts)."""
    return {icon for rules in (STRONG, WEAK) for icon, _ in rules}


@dataclass(frozen=True)
class Hit:
    icon: str
    pos: int        # character offset of the word in the line
    tier: int       # 0 = strong, 1 = weak


# A keyword right after a negation says the opposite: "no teachers" must not
# put a teacher on screen, "you don't need a degree" no graduation cap. Up to
# two words may sit between ("without any lectures"), within one clause (a
# ".?!;:," ends it). Rules that name the
# negation themselves ("no tuition" -> piggy bank) start AT the "no", so the
# text before them is clean and they still fire.
_NEGATED = re.compile(
    r"\b(?:no|not|non|without|never|zero|nor|don't|doesn't|didn't|isn't|aren't|can't|won't)"
    r"[^\w.?!;:,]+(?:\w+[^\w.?!;:,]+){0,2}$", re.IGNORECASE)


def _negated(text: str, at: int) -> bool:
    return bool(_NEGATED.search(text[max(0, at - 40):at]))


def find_hits(text: str) -> list[Hit]:
    """Every keyword hit in `text`, one per icon (its FIRST mention that is
    not negated), in order."""
    text = text or ""
    first: dict[str, Hit] = {}
    for tier, icon, rx in _COMPILED:
        m = next((m for m in rx.finditer(text) if not _negated(text, m.start())), None)
        if not m:
            continue
        hit = Hit(icon, m.start(), tier)
        old = first.get(icon)
        if old is None or (hit.tier, hit.pos) < (old.tier, old.pos):
            first[icon] = hit
    return sorted(first.values(), key=lambda h: h.pos)


def voiced_span(path: Path, *, is_still: bool = False) -> Optional[tuple[float, float]]:
    """(first, last) second where someone is talking in the clip, from its
    audio's RMS envelope (20 ms windows above 12% of the 95th percentile).
    None when the clip has no audio or no speech."""
    if is_still or not media.has_audio(path):
        return None
    proc = subprocess.run(
        [media.ffmpeg_exe(), "-v", "error", "-i", str(path), "-map", "0:a:0",
         "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        capture_output=True, timeout=120,
    )
    x = np.frombuffer(proc.stdout, np.int16).astype(np.float32) / 32768.0
    win = 320  # 20 ms at 16 kHz
    n = len(x) // win
    if n < 5:
        return None
    rms = np.sqrt((x[: n * win].reshape(n, win) ** 2).mean(axis=1))
    peak = float(np.percentile(rms, 95))
    if peak < 0.01:
        return None
    on = np.flatnonzero(rms > max(0.006, 0.12 * peak))
    if not len(on):
        return None
    return float(on[0] * win / 16000), float((on[-1] + 1) * win / 16000)


def plan_scene(
    text: str, seg: dict, voiced: Optional[tuple[float, float]],
) -> list[dict]:
    """Icon cues for one scene segment, in reel frames: [{icon, from, frames}]."""
    hits = find_hits(text)
    if not hits:
        return []
    start, frames = int(seg["from"]), int(seg["frames"])
    seconds = frames / FPS
    t0, t1 = voiced if voiced else (0.3, max(0.3, seconds - 0.3))
    t1 = min(max(t1, t0 + 0.1), seconds)
    length = max(1, len(text))

    # Greedy by strength (strong words first, earlier first), each keeping
    # GAP_FRAMES from every icon already taken; then back into time order.
    lo, hi = start + HEAD_CLEAR, start + frames - TAIL_CLEAR
    cues: list[dict] = []
    for h in sorted(hits, key=lambda h: (h.tier, h.pos)):
        if len(cues) >= MAX_PER_SCENE:
            break
        said = t0 + (h.pos / length) * (t1 - t0)
        at = max(lo, start + round(said * FPS) - IN_FRAMES)
        if at + MIN_FRAMES > hi or any(abs(at - c["from"]) < GAP_FRAMES for c in cues):
            continue
        cues.append({"icon": h.icon, "from": at, "frames": MIN_FRAMES})
    cues.sort(key=lambda c: c["from"])
    # Hold each until the next one arrives (or MAX_FRAMES / the scene's end).
    for i, cue in enumerate(cues):
        nxt = cues[i + 1]["from"] if i + 1 < len(cues) else hi
        cue["frames"] = int(max(MIN_FRAMES, min(MAX_FRAMES, nxt - cue["from"])))
    return cues


def plan_icons(
    segments: list[dict], scene_text: dict[int, str], voiced: dict[int, Optional[tuple[float, float]]],
) -> list[dict]:
    """Cues for the whole reel. `scene_text` / `voiced` are keyed by the
    segment's POSITION in `segments` (not its scene index: a test reel may
    reuse an index)."""
    cues: list[dict] = []
    for i, seg in enumerate(segments):
        if seg.get("kind") != "scene":
            continue
        cues += plan_scene(scene_text.get(i, ""), seg, voiced.get(i))
    return cues


# ---------------------------------------------------------------------------
# The SAID transcript's rules (the Rebrand studio's icon director)
#
# find_hits above is what the classic brand frame runs on the SCRIPT, and it
# stays exactly as it is. The studio's icons run on what was actually SAID
# (rebrand_harness/words.py builds that transcript) and need three things the
# classic table never had:
#   - a negation that reaches the END OF ITS CLAUSE, through "or"/"and"/"nor"
#     lists: "You don't need a degree or any coding experience" put </> on
#     "coding" with the two-word window above; "Forget degrees" and "Stop
#     waiting for a teacher" were not negations at all;
#   - idioms that only look like a keyword: "feel free", "dress code";
#   - the brand's own words, which are never an idea.
# ---------------------------------------------------------------------------

# The brand and its place: never an icon (the table leaves "Abu Dhabi" out of
# map-pin for the same reason).
BRAND_WORDS: tuple[str, ...] = ("42", "forty-two", "forty two", "abu", "dhabi")
_BRAND_RX = re.compile(r"\b(?:42|forty[- ]two|abu|dhabi)\b", re.IGNORECASE)

# Words that turn what follows them into its opposite, to the end of the
# clause. "stop" only before an -ing verb ("stop waiting", not "one-stop" or
# "stop by"); "zero" not in "from zero"; "not only / not just" add, not negate.
_NEGATOR = re.compile(
    r"\b(?:no|not|non|none|nor|neither|never|without|nobody|nothing|cannot"
    r"|(?:do|does|did|is|are|was|were|ca|wo|would|should|could|need|have|has|had|must|ai)n['’]?t"
    r"|forget|skip|ditch|instead\s+of|rather\s+than|no\s+longer|no\s+more|goodbye\s+to"
    r"|stop(?=\s+\w+ing\b)|(?<!from\s)zero)\b(?!\s+(?:only|just)\b)",
    re.IGNORECASE)
# A clause ends here, always.
_CLAUSE_END = re.compile(r"[.?!;:—–()\[\]\"“”]")
# ...and a negation does not reach past a turn in the sentence.
_TURN = re.compile(r"\b(?:but|yet|instead|rather|because|although|though|whereas|while|so|unless|except)\b",
                   re.IGNORECASE)
# A comma is crossed only inside a LIST: the item after it is one to three
# words followed by another comma or by or/and/nor ("no degree, coding
# experience or a laptop"). "without teachers, building real projects." is
# two clauses, so "projects" stays positive.
_LIST_ITEM = re.compile(r"^\s*(?:[\w'’-]+\s+){0,2}[\w'’-]+\s*(?:,|\b(?:or|and|nor)\b)", re.IGNORECASE)
# The words that may sit between a negator and "to" without ending its reach:
# "never have to pay fees", "no need to pay". Anything else before the "to"
# makes it a purpose: "you don't need a degree to code" is about coding.
_MODAL = frozenset({"need", "needs", "needed", "have", "has", "had", "having", "got", "gotta", "be",
                    "been", "going", "want", "wants", "ought", "used", "able", "any", "even", "ever",
                    "really"})

# Idioms that only look like a keyword. A hit STARTING inside one is dropped
# ("tuition-free to all" starts at "tuition", so it stays).
_IDIOM = re.compile(
    r"\bfeel\s+free\b"
    r"|\bfree\s+to\b(?!\s+(?:join|apply|attend|enrol\w*|register|sign\s+up|all|everyone|anyone|students?)\b)"
    r"|\bfree\s+time\b|\bfree\s+(?:will|speech|rein|fall)\b|\bhands[- ]free\b"
    r"|\bdress\s+code\b|\b(?:qr|zip|postal|area)\s+code\b|\bcode\s+of\s+conduct\b",
    re.IGNORECASE)


def is_brand(text: str) -> bool:
    """True when `text` names the brand or its place (42, Abu Dhabi)."""
    return bool(_BRAND_RX.search(text or ""))


def idiom_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of every idiom in `text` that only looks like a keyword."""
    return [m.span() for m in _IDIOM.finditer(text or "")]


def in_idiom(text: str, at: int) -> bool:
    """True when character `at` of `text` sits inside an idiom ("feel free",
    "dress code"…)."""
    return any(s <= at < e for s, e in idiom_spans(text))


def _reaches(text: str, neg_end: int, at: int) -> bool:
    """Does a negation that ends at `neg_end` still hold at `at`?"""
    between = text[neg_end:at]
    if _TURN.search(between):
        return False
    for i, ch in enumerate(between):
        if ch == "," and not _LIST_ITEM.match(text[neg_end + i + 1:]):
            return False
    words = re.findall(r"[\w'’]+", between.lower())
    if "to" in words and any(w not in _MODAL for w in words[:words.index("to")]):
        return False
    return True


def negated_in_clause(text: str, at: int) -> bool:
    """True when the word at character `at` of `text` is said inside a
    negation: a negator earlier in the same clause whose reach runs through
    list commas and or/and/nor, and stops at a clause end, a turn ("but",
    "because"…), a comma that is not a list's, or a "to" of purpose."""
    text = text or ""
    at = max(0, min(int(at), len(text)))
    start = 0
    for m in _CLAUSE_END.finditer(text, 0, at):
        start = m.end()
    for m in _NEGATOR.finditer(text, start, at):
        if m.end() <= at and _reaches(text, m.end(), at):
            return True
    return False


@dataclass(frozen=True)
class Mention:
    icon: str
    pos: int        # character offset where the keyword starts
    end: int        # ...and where it ends
    tier: int       # 0 = strong, 1 = weak


def keyword_spans(text: str, icon: Optional[str] = None) -> list[Mention]:
    """Every raw keyword match in `text` (of `icon` only, when given),
    negated or not, in order — what find_mentions filters."""
    text = text or ""
    return sorted((Mention(ic, m.start(), m.end(), tier) for tier, ic, rx in _COMPILED
                   if icon is None or ic == icon for m in rx.finditer(text)),
                  key=lambda h: (h.pos, h.tier, h.icon))


def find_mentions(text: str) -> list[Mention]:
    """EVERY keyword mention in `text` (the said transcript) that is not said
    inside a negation (negated_in_clause), not inside an idiom and not the
    brand, in order. Overlapping mentions of one icon are one mention (the
    earliest, then the strongest: "tuition-free", not also its "free");
    find_hits keeps only one per icon."""
    text = text or ""
    brand = [m.span() for m in _BRAND_RX.finditer(text)]
    found: list[Mention] = []
    for tier, icon, rx in _COMPILED:
        for m in rx.finditer(text):
            at = m.start()
            if negated_in_clause(text, at) or in_idiom(text, at) or any(s <= at < e for s, e in brand):
                continue
            found.append(Mention(icon, at, m.end(), tier))
    kept: list[Mention] = []
    for mm in sorted(found, key=lambda h: (h.pos, h.tier, -h.end, h.icon)):
        if any(k.icon == mm.icon and mm.pos < k.end and k.pos < mm.end for k in kept):
            continue
        kept.append(mm)
    return kept


def find_hits_said(text: str) -> list[Hit]:
    """find_hits with the said transcript's rules: one Hit per icon, its
    FIRST mention that find_mentions keeps, in order."""
    first: dict[str, Hit] = {}
    for mm in find_mentions(text):
        hit = Hit(mm.icon, mm.pos, mm.tier)
        old = first.get(mm.icon)
        if old is None or (hit.tier, hit.pos) < (old.tier, old.pos):
            first[mm.icon] = hit
    return sorted(first.values(), key=lambda h: h.pos)
