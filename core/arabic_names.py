"""
arabic_names -- Arabic-script and transliteration-aware name matching.

WHY THIS MODULE EXISTS
======================
screen_sanctions used to turn a name into [a-z0-9] tokens and compare token SETS.
Two consequences, both measured, both about the people this tool is sold to:

  1. An Arabic-script name normalised to NOTHING. "Hizballah" written in Arabic
     could not be looked up at all; the tool reported the name as unscreenable.
     The text of _ascii() said "Transliteration is the fix and it is not built
     yet." This is that fix.

  2. A Latin-script Arabic name matched only if the SPELLING was identical.
     "Mohammed al-Zawahiri" and "Muhammad Al-Zawahri" share no token. Arabic has
     no standard romanisation: the lists themselves carry six spellings of one
     man in one entry ("Ayman Al-Zawahari", "Aiman Muhammed Rabi Al-Zawahiri",
     "Dhawahri Ayman", "Eddaouahiri Ayman" ...). A matcher that needs the same
     spelling is a matcher that misses Arabic names.

WHAT IT DOES
============
It reduces a name to a list of UNITS, then compares units by sound.

  * Structure. The article (al-, el-, ash-/ar-/as- assimilations), the filial
    particles (bin, ibn, bint, ould, Arabic and Latin), titles, and the compound
    name elements that are written either as one word or as several
    (Abd al-Rahman / Abdurrahman, Abu Bakr / Abubakr, Nur al-Din / Noureddine,
    Saif Allah / Saifullah) are normalised so that the way a name is SPACED does
    not change what it is.

  * Orthography (Arabic script only). Diacritics, tatweel, the alef family,
    alef maqsura/yeh, ta marbuta/heh, Persian and Pashto letter variants and the
    three digit sets are folded. Two spellings that differ only in these are the
    same written name.

  * Sound. Each unit becomes a SKELETON: the consonants, in order, in a small
    alphabet of phoneme classes (kh, gh, sh, th, dh each one symbol). Arabic and
    Latin tokens land in the same alphabet, so an Arabic query is compared with a
    Latin list entry directly and no romanisation has to be guessed. Short
    vowels, which Arabic does not write and romanisation spells every which way,
    are not part of the skeleton at all.

  * Distance. Skeletons are compared with a weighted edit distance whose
    substitution costs say which confusions are NORMAL between romanisations
    (q/k/g, kh/h, th/s/t, dh/z/d, j/g, sh/ch) and which are not. The result is a
    similarity in 0..1 per name element, then an alignment of query elements to
    listed elements, then a coverage score in each direction.

WHAT IT REFUSES TO DO
=====================
It never asserts a sanctions finding from a transliteration. Sound-alike is a
CANDIDATE, graded high/medium/low, with the alignment that produced it spelled
out. The one exception is deliberately narrow and lives in screen_sanctions: an
Arabic-script query equal, element for element, to an Arabic-script ALIAS the
publisher itself printed (same written name, after the orthographic folds above)
is the same kind of evidence as a Latin token-set equality and is treated alike.

There is no name-frequency data behind any of this (screen_sanctions.py explains
why we do not have it), so the module carries a short, explicit list of the
commonest Arab name elements and discounts them, instead of pretending to a
statistical calibration it does not have. The thresholds are calibrated against
the real EU and UK list copies; see docs/ARABIC_SANCTIONS_EVAL.md and
scripts/eval_arabic_sanctions.py for the method and the numbers.

Pure functions. No I/O, no network, no database. Source is ASCII-only: every
Arabic letter is a code point, every word list lives in arabic_name_data.py.
"""
from __future__ import annotations

import re
import unicodedata
from array import array
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Optional

from core import arabic_name_data as _data

# ---------------------------------------------------------------------------
# Script detection
# ---------------------------------------------------------------------------

_AR_RANGES = ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
              (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))


def _is_arabic_letter(ch: str) -> bool:
    o = ord(ch)
    for lo, hi in _AR_RANGES:
        if lo <= o <= hi:
            return unicodedata.category(ch).startswith("L")
    return False


def has_arabic_script(text: str) -> bool:
    """True when `text` contains at least one Arabic-script LETTER."""
    return any(_is_arabic_letter(c) for c in (text or ""))


# ---------------------------------------------------------------------------
# Arabic orthographic normalisation
# ---------------------------------------------------------------------------

TA_MARBUTA = "\u0629"
HEH = "\u0647"
ALLAH = "\u0627\u0644\u0644\u0647"            # the name Allah: NOT article + word
ART = "\u0627\u0644"                           # the article "al"
ABD = "\u0639\u0628\u062f"                     # "abd" (servant of)
DIN = "\u062f\u064a\u0646"                     # "din" (religion), after the article is stripped

# Marks and format characters removed before anything else: short-vowel marks,
# shadda, sukun, dagger alef, Quranic annotation marks, tatweel, and the
# invisible bidi / joiner characters that copy-paste from web pages drags in.
_STRIP = re.compile(
    "[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06dc\u06df-\u06e8\u06ea-\u06ed"
    "\u0640\u200b-\u200f\u202a-\u202e\u2066-\u2069\u061c\ufeff]")

# (target, sources). Each source letter is the same LETTER as the target in a
# different shape or language; folding them is orthography, not interpretation.
_FOLD_GROUPS = (
    (0x0627, (0x0623, 0x0625, 0x0622, 0x0671, 0x0672, 0x0673, 0x0675)),   # alef: hamza above/below, madda, wasla
    (0x064A, (0x0649, 0x0626, 0x06CC, 0x06D0, 0x06CD, 0x06CE, 0x06D2,
              0x06D3, 0x0678)),                       # yeh: alef maqsura, hamza seat, Farsi and Pashto yeh
    (0x0648, (0x0624, 0x0676, 0x0677, 0x06C6, 0x06C7, 0x06C8, 0x06C9, 0x06CB)),   # waw
    (0x0647, (0x06C3, 0x06C1, 0x06BE, 0x06D5, 0x06C0, 0x06C2)),            # heh variants (NOT ta marbuta)
    (0x0643, (0x06A9, 0x06AA)),                       # kaf
    (0x062A, (0x067C, 0x0679)),                       # Pashto and Urdu teh
    (0x062F, (0x0688, 0x0689)),                       # Urdu and Pashto dal
    (0x0631, (0x0691, 0x0693)),                       # Urdu and Pashto reh
    (0x0646, (0x06BA, 0x06BC)),                       # noon ghunna, Pashto noon
    (0x0634, (0x069A,)),                              # Pashto sheen
    (0x062C, (0x0681,)),                              # Pashto jeem
    (0x0635, (0x0685,)),                              # Pashto sad
    (0x0641, (0x06A4,)),                              # veh
)
_FOLD = {src: chr(tgt) for tgt, srcs in _FOLD_GROUPS for src in srcs}
_FOLD[0x0621] = ""                                    # a lone hamza is dropped

_PUNCT = re.compile(r"[\W_]+", re.UNICODE)


def _ascii_digits(text: str) -> str:
    out = []
    for c in text:
        if not c.isascii() and unicodedata.category(c) == "Nd":
            out.append(str(unicodedata.decimal(c)))
        else:
            out.append(c)
    return "".join(out)


@lru_cache(maxsize=8192)
def _arabic_tokens(text: str) -> tuple[str, ...]:
    """Orthographically folded tokens. Ta marbuta is KEPT here (the sound
    skeleton needs to tell it from heh); ortho_key() folds it for equality."""
    t = unicodedata.normalize("NFKC", text or "")
    t = _STRIP.sub("", t)
    t = t.translate(_FOLD)
    t = _ascii_digits(t).lower()
    t = _PUNCT.sub(" ", t)
    return tuple(t.split())


def ortho_key(token: str) -> str:
    """The form two spellings of one written Arabic word share."""
    return token.replace(TA_MARBUTA, HEH)


def normalise_arabic(text: str) -> str:
    """Orthographically folded text, tokens joined by one space. For display
    and tests; matching uses the unit structure below."""
    return " ".join(ortho_key(t) for t in _arabic_tokens(text))


# ---------------------------------------------------------------------------
# Latin folding
# ---------------------------------------------------------------------------

_LATIN_EXTRA = {"\u00df": "ss", "\u00e6": "ae", "\u0153": "oe", "\u00f8": "o",
                "\u0111": "d", "\u0142": "l", "\u0131": "i", "\u00f0": "d",
                "\u00fe": "th"}
# Apostrophes, ayn/hamza modifier letters and the ASCII look-alikes romanisers
# use for them. DELETED, not split on, for the reason _normalize_name gives: a
# stranded "s" or "a" is debris, not an identity.
_APOS = dict.fromkeys(map(ord, "'`\u00b4\u2018\u2019\u201b\u02b9\u02bb\u02bc\u02bd"
                          "\u02be\u02bf\u02c8\u02ca\u02cb\u2032\u2035"), None)


@lru_cache(maxsize=8192)
def fold_latin(text: str) -> str:
    """Lowercase ASCII with diacritics removed.

    "Mu\u1e25ammad \u02bfAl\u012b al-\u1e62\u0101li\u1e25" -> "muhammad ali al-salih".
    The existing _normalize_name DROPS a non-ASCII letter outright, so an ALA-LC
    romanisation like that one lost its h's and its vowels there; this keeps the
    letter."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower().translate(_APOS)
    return "".join(_LATIN_EXTRA.get(c, c) for c in t)


@lru_cache(maxsize=8192)
def _latin_tokens(text: str) -> tuple[str, ...]:
    # Digits are kept: "Hizb 14" style names exist; units drop single characters.
    return tuple(re.findall(r"[a-z0-9]+", fold_latin(text)))


# ---------------------------------------------------------------------------
# Skeletons
#
# Symbols (one character each):
#   b p t 3(th) j c(ch) h x(kh) d 4(dh) r z s S(sh) G(gh) f q k g l m n w y
#   T = a ta marbuta: a "t" that is allowed to be absent
# Vowels and the letters ayn / hamza / alef are not symbols.
# ---------------------------------------------------------------------------

_AR_SKEL = {
    0x0628: "b", 0x062A: "t", 0x062B: "3", 0x062C: "j", 0x062D: "h",
    0x062E: "x", 0x062F: "d", 0x0630: "4", 0x0631: "r", 0x0632: "z",
    0x0633: "s", 0x0634: "S", 0x0635: "s", 0x0636: "d", 0x0637: "t",
    0x0638: "z", 0x0639: "", 0x063A: "G", 0x0641: "f", 0x0642: "q",
    0x0643: "k", 0x0644: "l", 0x0645: "m", 0x0646: "n", 0x0647: "h",
    0x0648: "w", 0x064A: "y", 0x0629: "T", 0x067E: "p", 0x0686: "c",
    0x0698: "j", 0x06AF: "g", 0x0627: "",
}
# How the same letters are pronounced in Persian and Pashto. Reza is written
# with the Arabic letter dad and romanised with a z; Qasem is Ghasem. A token
# containing one of these letters gets a second, Persian-reading skeleton.
_FA_OVERRIDE = {0x0636: "z", 0x0638: "z", 0x0630: "z", 0x062B: "s", 0x0642: "G",
                0x0637: "t"}
_FA_TRIGGER = frozenset(_FA_OVERRIDE)


def arabic_skeleton(token: str, persian: bool = False) -> str:
    """Sound skeleton of ONE Arabic-script token (already folded)."""
    out: list[str] = []
    for ch in token:
        o = ord(ch)
        sym = (_FA_OVERRIDE.get(o) if persian and o in _FA_OVERRIDE
               else _AR_SKEL.get(o))
        if sym is None:
            if ch.isdigit():
                sym = ch
            else:
                continue
        if not sym:
            continue
        # NOT collapsed. Arabic leaves out short vowels, so two identical
        # letters side by side are either one doubled consonant (Allah) or two
        # consonants with an unwritten vowel between them (mamlouk). The
        # distance below makes dropping the second of a pair cheap instead of
        # guessing which it is.
        out.append(sym)
    return "".join(out)


_DIGRAPHS = (("tch", "c"), ("kh", "x"), ("gh", "G"), ("sh", "S"), ("ch", "c"),
             ("th", "3"), ("dh", "4"), ("dj", "j"), ("dg", "j"), ("zh", "j"),
             ("ph", "f"), ("ck", "k"))


@lru_cache(maxsize=20000)
def latin_skeleton(token: str, ch_as_sh: bool = False) -> str:
    """Sound skeleton of ONE folded Latin token.

    A "|" marks where two name elements were joined (abd|ghani), so that the
    d and gh of "Abd Ghani" are not read as the dg of "Abdghani". `ch_as_sh`
    reads ch as sh (French and English romanisations of Arabic: Chaoui, Cherif)
    instead of as the Persian/English ch."""
    if "|" in token:
        return "".join(latin_skeleton(part, ch_as_sh) for part in token.split("|"))
    t = re.sub(r"[^a-z0-9]", "", token)
    out: list[str] = []
    gap = True                      # a vowel since the last symbol?
    i, n = 0, len(t)
    while i < n:
        c = t[i]
        sym = None
        for dg, sy in _DIGRAPHS:
            if t.startswith(dg, i):
                sym = "S" if (ch_as_sh and sy == "c") else sy
                i += len(dg)
                break
        if sym is None:
            i += 1
            if c in "aeiou":
                gap = True
                continue
            if c == "c":
                sym = "s" if i < n and t[i] in "eiy" else "k"
            elif c == "v":
                sym = "w"
            else:
                sym = c
        # "ss", "ll", "bb" are one consonant; "b-u-b" (Abubakr) is two.
        if gap or not out or out[-1] != sym:
            out.append(sym)
        gap = False
    return "".join(out)


# Edit costs. Strong consonants cost 1 to delete or insert. The letters that
# romanisers add or drop freely cost far less.
_DEL = {"w": 0.06, "y": 0.06, "T": 0.10, "h": 0.40}
_WEIGHT = {"w": 0.0, "y": 0.0, "T": 0.0, "h": 0.6}   # share of a "full" consonant

_SUB_PAIRS = {
    ("t", "3"): 0.35, ("s", "3"): 0.30, ("3", "4"): 0.40, ("d", "4"): 0.25,
    ("z", "4"): 0.25, ("d", "t"): 0.55, ("s", "z"): 0.45, ("S", "c"): 0.25,
    ("S", "s"): 0.55, ("x", "h"): 0.35, ("x", "k"): 0.45, ("c", "x"): 0.40,
    ("q", "k"): 0.30, ("q", "g"): 0.25, ("q", "G"): 0.45, ("k", "g"): 0.35,
    ("j", "g"): 0.25, ("j", "z"): 0.50, ("j", "c"): 0.45, ("j", "y"): 0.50,
    ("G", "g"): 0.25, ("G", "x"): 0.55, ("b", "p"): 0.30, ("f", "p"): 0.50,
    ("T", "t"): 0.05, ("T", "h"): 0.30, ("z", "d"): 0.45, ("c", "k"): 0.55,
    ("c", "s"): 0.55, ("w", "f"): 0.50, ("S", "x"): 0.55, ("h", "G"): 0.65,
    ("q", "x"): 0.65,
}
_SUB: dict[tuple[str, str], float] = {}
for (_a, _b), _c in _SUB_PAIRS.items():
    _SUB[(_a, _b)] = _c
    _SUB[(_b, _a)] = _c


_SUB_ROW: dict[str, dict[str, float]] = {}
for (_a, _b), _c in _SUB.items():
    _SUB_ROW.setdefault(_a, {})[_b] = _c
_NO_SUB: dict[str, float] = {}


def _wlen(s: str) -> float:
    return sum(_WEIGHT.get(c, 1.0) for c in s)


def _core(s: str) -> str:
    return s.replace("w", "").replace("y", "").replace("T", "")


_DUP = 0.15          # cost of dropping the second of two identical letters
FUZZY_MIN_WEIGHT = 2.0   # a skeleton lighter than this is compared by equality only


def _dels(s: str) -> list[float]:
    dget = _DEL.get
    out = []
    prev = ""
    for c in s:
        base = dget(c, 1.0)
        out.append(_DUP if (c == prev and base > _DUP) else base)
        prev = c
    return out


def _wdist(a: str, b: str, limit: float) -> float:
    """Weighted Levenshtein; gives up (returns > limit) once every cell in a row
    already exceeds `limit`."""
    la, lb = len(a), len(b)
    dels_a = _dels(a)
    dels_b = _dels(b)
    prev = [0.0] * (lb + 1)
    for j in range(lb):
        prev[j + 1] = prev[j] + dels_b[j]
    for i in range(la):
        ca = a[i]
        da = dels_a[i]
        row = _SUB_ROW.get(ca, _NO_SUB)
        cur = [prev[0] + da] + [0.0] * lb
        rowmin = cur[0]
        for j in range(lb):
            cb = b[j]
            if ca == cb:
                v = prev[j]
            else:
                v = prev[j] + row.get(cb, 1.0)
            w = prev[j + 1] + da
            if w < v:
                v = w
            w = cur[j] + dels_b[j]
            if w < v:
                v = w
            cur[j + 1] = v
            if v < rowmin:
                rowmin = v
        if rowmin > limit:
            return limit + 1.0
        prev = cur
    return prev[lb]


@lru_cache(maxsize=60000)
def skel_similarity(a: str, b: str) -> float:
    """0..1 similarity of two skeletons. 1.0 only for identical skeletons."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    wa, wb = _wlen(a), _wlen(b)
    # A skeleton with fewer than two real consonants identifies nobody: it is
    # compared on equality of its core and nothing fuzzier.
    if min(wa, wb) < FUZZY_MIN_WEIGHT:
        return 0.95 if (_core(a) and _core(a) == _core(b)) else 0.0
    denom = max(wa, wb)
    if denom <= 0:
        return 0.0
    d = _wdist(a, b, limit=denom * 0.35)
    if d > denom * 0.35:
        return 0.0
    return max(0.0, 1.0 - d / denom)


_INFO = {"w": 0.4, "y": 0.4, "T": 0.0, "h": 0.8}


def skel_info(s: str) -> float:
    """How many consonants' worth of identity a skeleton carries. Unlike the
    edit-distance weight, w and y count for something here: in "Zawahiri" the w
    is a consonant, and a name element is not weak because romanisers are casual
    about those two letters."""
    return sum(_INFO.get(c, 1.0) for c in s)


# ---------------------------------------------------------------------------
# Name units
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Unit:
    """One name element after structural normalisation."""
    text: str                     # canonical text (Arabic: ortho key; Latin: folded)
    script: str                   # "ar" | "la"
    skels: tuple[str, ...]        # alternative sound skeletons, best-effort
    optional: bool = False        # a title: matched if both sides carry it, never required
    generic: bool = False         # "company", "trading": identifies nobody
    common: bool = False          # one of the commonest Arab name elements
    marker: bool = False          # carried Arabic-name structure (al-, abd, abu, bin ...)


_TABLES: dict = {}


def _tables() -> dict:
    """Word sets, built once from arabic_name_data (lazily, so import order is
    never a problem)."""
    if _TABLES:
        return _TABLES

    def fold_set(words: Iterable[str]) -> frozenset:
        out = set()
        for w in words:
            for t in _arabic_tokens(w):
                out.add(t)
            joined = "".join(_arabic_tokens(w))
            if joined:
                out.add(joined)
        return frozenset(out)

    def skel_set(words: Iterable[str]) -> frozenset:
        out = set()
        for w in words:
            toks = _arabic_tokens(w)
            for cand in list(toks) + ["".join(toks)]:
                if cand.startswith(ART) and len(cand) >= 4 and cand != ALLAH:
                    cand = cand[2:]
                for fa in (False, True):
                    sk = arabic_skeleton(cand, persian=fa)
                    if sk:
                        out.add(sk)
                        if _core(sk):
                            out.add(_core(sk))
                        # the trailing ta marbuta is a "t" that may be spoken as
                        # a heh or not at all
                        if sk.endswith("T"):
                            out.add(sk[:-1])
                            out.add(sk[:-1] + "h")
        return frozenset(out)

    _TABLES.update(
        titles=fold_set(_data.TITLES),
        generic=fold_set(_data.GENERIC),
        common=fold_set(_data.COMMON),
        given=fold_set(_data.GIVEN),
        persian_suffix=fold_set(_data.PERSIAN_SUFFIXES),
        generic_skels=skel_set(_data.GENERIC),
        common_skels=skel_set(_data.COMMON),
        given_skels=skel_set(_data.GIVEN),
        generic_latin=frozenset({
            "llc", "ltd", "limited", "inc", "corp", "co", "company", "plc",
            "est", "establishment", "fze", "fzc", "fzco", "wll", "psc", "jsc",
            "trading", "trade", "group", "holding", "holdings", "international",
            "enterprise", "enterprises", "services", "general", "global",
            "industries", "industrial", "commercial", "business", "contracting",
            "investment", "investments", "development", "projects", "supplies",
            "export", "import", "bank", "office", "and", "of", "the", "for"}),
    )
    return _TABLES


def _in_skel_set(sk: str, table) -> bool:
    """Membership by skeleton, ignoring the weak letters w, y and ta marbuta:
    Arabic writes the long vowel in Ali (ly) that a romaniser does not (l)."""
    return sk in table or _core(sk) in table


def set_generic_latin(words: Iterable[str]) -> None:
    """screen_sanctions hands over its own _GENERIC_NAME_WORDS so the two
    matchers can never disagree about what a generic word is."""
    t = _tables()
    t["generic_latin"] = frozenset(t["generic_latin"]) | frozenset(words)
    skeleton_cache_clear()


def skeleton_cache_clear() -> None:
    analyse.cache_clear()


# --- Arabic-script structure ------------------------------------------------

_AR_KUNYA = frozenset({"\u0627\u0628\u0648", "\u0627\u0628\u064a",
                       "\u0627\u0628\u0627", "\u0627\u0645",
                       "\u0628\u0648"})   # abu abi aba umm bu
_AR_PATRONYMIC = frozenset({"\u0628\u0646", "\u0627\u0628\u0646",
                            "\u0628\u0646\u062a", "\u0627\u0628\u0646\u0629",
                            "\u0648\u0644\u062f", "\u0628\u0646\u064a",
                            "\u0627\u0628\u0646\u0647"})        # bin ibn bint ibna walad bani


def _strip_article(tok: str) -> tuple[str, bool]:
    if tok == ART:
        return "", True
    if tok in (ALLAH, "\u0644\u0644\u0647"):
        return ALLAH, False
    if len(tok) >= 4 and tok.startswith(ART):
        return tok[2:], True
    return tok, False


def _ar_skels(text: str) -> tuple[str, ...]:
    out: list[str] = []
    persian = any(ord(c) in _FA_TRIGGER for c in text)
    for fa in ((False, True) if persian else (False,)):
        sk = arabic_skeleton(text, persian=fa)
        if sk and sk not in out:
            out.append(sk)
        if sk.endswith("T"):                    # ta marbuta spoken as heh or silent
            for v in (sk[:-1], sk[:-1] + "h"):
                if v and v not in out:
                    out.append(v)
    return tuple(out)


def _arabic_units(toks: tuple[str, ...]) -> list[Unit]:
    T = _tables()
    out: list[Unit] = []
    pending_patronymic: Optional[str] = None
    i, n = 0, len(toks)
    while i < n:
        tok = toks[i]
        i += 1
        if tok in _AR_PATRONYMIC:
            pending_patronymic = tok
            continue
        stripped, had_art = _strip_article(tok)
        if not stripped:
            continue
        marker = had_art or pending_patronymic is not None
        alts = [tok] if (had_art and tok != stripped) else []
        if pending_patronymic and not had_art:
            alts.append(pending_patronymic + stripped)

        def take_next() -> str:
            nonlocal i
            nxt = ""
            while i < n:
                cand, _ = _strip_article(toks[i])
                i += 1
                if cand and cand not in _AR_PATRONYMIC:
                    nxt = cand
                    break
            return nxt

        if stripped in _AR_KUNYA and i < n:
            nxt = take_next()
            if nxt:
                stripped = stripped + nxt
                marker = True
                alts = []
        elif stripped == ABD:
            nxt = take_next()
            if nxt:
                stripped = ABD + nxt
            marker = True
            alts = []
        elif stripped.startswith(ABD) and len(stripped) > 5:
            rest = stripped[3:]
            if rest.startswith(ART) and rest != ALLAH and len(rest) > 3:
                rest = rest[2:]
            elif rest.startswith("\u0644\u0644\u0647"):      # abd + lillah
                rest = ALLAH
            stripped = ABD + rest
            marker = True
            alts = []
        elif stripped.endswith(ART + "\u062f\u064a\u0646") and len(stripped) > 6:
            stripped = stripped[:-5] + DIN
            marker = True
        elif stripped == DIN and out and not out[-1].optional:
            prev = out.pop()
            stripped = prev.text + DIN
            marker = True
            alts = []
        elif stripped in T["persian_suffix"] and out and not out[-1].optional:
            prev = out.pop()
            stripped = prev.text + stripped
            alts = []
        elif stripped == ALLAH and out and not out[-1].optional \
                and out[-1].text not in (ABD,):
            prev = out.pop()
            stripped = prev.text + ALLAH
            marker = True
            alts = []
        pending_patronymic = None

        key = ortho_key(stripped)
        skels: list[str] = []
        for cand in [stripped] + alts:
            for sk in _ar_skels(cand):
                if sk not in skels:
                    skels.append(sk)
        if not skels:
            continue
        out.append(Unit(
            text=key, script="ar", skels=tuple(skels),
            optional=key in T["titles"],
            generic=key in T["generic"] or any(s in T["generic_skels"] for s in skels[:1]),
            # Arabic-script elements are common by SPELLING only. By sound
            # (as Latin ones must be) the surname Salami would join Salim, Salem
            # and Salma, and "Hossein Salami" would read as two common names.
            common=key in T["common"],
            marker=marker))
    return out


# --- Latin structure -------------------------------------------------------

_LA_ARTICLE = frozenset({"al", "el", "ul", "il"})
_LA_ASSIM = {"ar": "r", "as": "s", "ash": "sh", "ad": "d", "at": "t",
             "az": "z", "an": "n", "ath": "th", "adh": "dh", "ez": "z",
             "es": "s", "esh": "sh", "ed": "d", "er": "r", "en": "n",
             "et": "t", "ush": "sh", "us": "s", "ud": "d", "ur": "r",
             "un": "n", "ut": "t", "uz": "z"}
_LA_PATRONYMIC = frozenset({"bin", "ibn", "ben", "bint", "binti", "ould",
                            "walad", "bani", "banu", "ibnat"})
_LA_KUNYA = frozenset({"abu", "abou", "abo", "aboo", "abi", "aba", "umm", "oum"})
_LA_ABD = frozenset({"abd", "abdul", "abdel", "abdal", "abdol", "abdoul", "abdu",
                     "abdur", "abdus", "abdun", "abdush", "abdut", "abduz",
                     "abdud", "abdl"})
_LA_ABD_PREFIX = re.compile(
    r"^abd(?:ul|el|al|ol|oul|ur|us|un|ush|ud|ut|uz|u)?(?=[a-z]{3,}$)")
_LA_DIN_SUFFIX = re.compile(r"(?:[aeiou]d{1,2}|d{1,2})(?:i|ee|ie)ne?$")
_LA_DIN_WORD = frozenset({"din", "deen", "dine", "eddin", "eddine", "uddin",
                          "uddeen", "addin", "eddeen", "ddin", "ddine", "ddeen",
                          "udin", "eldin", "eldine", "aldin", "aldine", "uldin",
                          "uldeen", "aldeen", "eldeen", "adin", "edin"})
# Persian name suffixes written either attached or as a separate word
# (Arlanizadeh / Arlani zadeh, Ahmadinejad / Ahmadi nejad).
_LA_PERSIAN_SUFFIX = frozenset({"zadeh", "zade", "zad", "zadegan", "pour", "pur",
                                "poor", "far", "fard", "nia", "nejad", "nezhad",
                                "nezad", "abadi", "abad"})
_LA_ALLAH = frozenset({"allah", "alla", "ullah", "ulla", "llah"})
_LA_TITLES = frozenset({
    "sheikh", "shaikh", "sheik", "shaykh", "sheyh", "shaik", "sheykh", "hajj",
    "haji", "hadji", "hajji", "doctor", "dr", "mullah", "mulla", "mawlawi",
    "maulvi", "maulana", "moulana", "mawlana", "ustad", "ustadh", "engineer",
    "eng", "seyed", "sayed", "sayyid", "seyyed", "syed", "sayyed", "agha",
    "aga", "aqa", "sardar", "hazrat", "amir", "emir", "mr", "mrs", "ms"})


def _latin_units(toks: tuple[str, ...]) -> list[Unit]:
    T = _tables()
    out: list[Unit] = []
    pending_patronymic: Optional[str] = None
    pending_article = False
    i, n = 0, len(toks)
    toks_l = [t for t in toks if len(t) > 1 or t.isdigit()]
    toks = tuple(toks_l)
    n = len(toks)
    while i < n:
        tok = toks[i]
        i += 1
        if tok in _LA_PATRONYMIC and i < n:
            pending_patronymic = tok
            continue
        if tok in _LA_ARTICLE and i < n:
            pending_article = True
            continue
        if tok in _LA_ASSIM and i < n and toks[i].startswith(_LA_ASSIM[tok]):
            pending_article = True
            continue
        marker = pending_article or pending_patronymic is not None
        text = tok
        alts: list[str] = []
        had_art = pending_article
        if pending_article:
            alts.append("al" + tok)
        elif len(tok) >= 6 and tok[:2] in ("al", "el") and tok.isalpha() \
                and not tok.startswith(("alex", "alf", "alb", "alm", "alp", "ale")):
            alts.append(tok)
            text = tok[2:]
            marker = True
        if pending_patronymic and not had_art:
            alts.append(pending_patronymic + tok)

        def take_next() -> str:
            nonlocal i
            while i < n:
                cand = toks[i]
                i += 1
                if cand in _LA_ARTICLE or (
                        cand in _LA_ASSIM and i < n
                        and toks[i].startswith(_LA_ASSIM[cand])):
                    continue
                if cand in _LA_PATRONYMIC:
                    continue
                return cand
            return ""

        if tok in _LA_ABD and i < n:
            nxt = take_next()
            if nxt:
                text = "abd|" + nxt
                marker = True
                alts = []
        elif tok in ("abul", "abol") and i < n:
            nxt = take_next()
            if nxt:
                text = "abu|" + nxt
                marker = True
                alts = []
        elif tok in _LA_KUNYA and i < n:
            nxt = take_next()
            if nxt:
                text = tok + "|" + nxt
                marker = True
                alts = []
        elif _LA_ABD_PREFIX.match(tok):
            m = _LA_ABD_PREFIX.match(tok)
            alts = [tok] + alts
            text = "abd|" + tok[m.end():]
            marker = True
        elif tok in _LA_DIN_WORD and out and not out[-1].optional:
            prev = out.pop()
            text = prev.text + "|din"
            marker = True
            alts = []
        elif tok in _LA_PERSIAN_SUFFIX and out and not out[-1].optional:
            prev = out.pop()
            text = prev.text + "|" + tok
            alts = []
        elif _LA_DIN_SUFFIX.search(tok) and len(tok) > 6:
            alts = [tok] + alts
            text = _LA_DIN_SUFFIX.sub("din", tok)
            marker = True
        elif tok in _LA_ALLAH and out and not out[-1].optional \
                and out[-1].text != "abd":
            prev = out.pop()
            text = prev.text + "|allah"
            marker = True
            alts = []
        if tok.endswith(("ullah", "allah", "uddin", "eddine", "eddin", "uddeen")):
            marker = True
        pending_patronymic = None
        pending_article = False

        skels: list[str] = []
        for cand in [text] + alts:
            sk = latin_skeleton(cand)
            if sk and sk not in skels:
                skels.append(sk)
            if "ch" in cand:
                sk2 = latin_skeleton(cand, True)
                if sk2 and sk2 not in skels:
                    skels.append(sk2)
        if not skels:
            continue
        out.append(Unit(
            text=text, script="la", skels=tuple(skels),
            optional=text in _LA_TITLES,
            generic=text in T["generic_latin"] or skels[0] in T["generic_skels"],
            common=_in_skel_set(skels[0], T["common_skels"]),
            marker=marker or (len(skels[0]) >= 3
                              and _in_skel_set(skels[0], T["given_skels"]))))
    return out


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Analysis:
    raw: str
    script: str                       # "arabic" | "latin" | "mixed"
    units: tuple[Unit, ...]
    engaged: bool                     # does the Arabic/transliteration layer apply?
    weak_reason: Optional[str]        # why the phonetic search is not run, if it is not
    arabic_key: tuple[str, ...]       # ortho keys of the Arabic-script units, sorted, unique
    arabic_all_key: tuple[str, ...]   # ... of ALL Arabic-script units incl. generic/optional

    @property
    def phonetic_ok(self) -> bool:
        return self.engaged and self.weak_reason is None

    @property
    def required(self) -> tuple[Unit, ...]:
        return tuple(u for u in self.units if not u.optional and not u.generic)


def _weak_reason(units: list[Unit]) -> Optional[str]:
    req = [u for u in units if not u.optional and not u.generic]
    if not req:
        return ("the name consists only of generic and title words, which "
                "identify no specific party")
    info = sum(min(skel_info(u.skels[0]), 5.0) for u in req)
    if info < 3.0:
        return ("too short to identify anyone by sound: fewer than three "
                "consonants of name content")
    if len(req) == 1 and req[0].common:
        return ("a single very common name element, which identifies nobody "
                "by itself")
    return None


def _analyse_uncached(name: str) -> Analysis:
    """Reduce a name to units and decide whether the Arabic layer applies."""
    # Nothing that is a name is longer than this; a longer string is an abuse of
    # a free tool, and the alignment below is quadratic in the number of elements.
    raw = (name or "").strip()[:MAX_NAME_CHARS]
    ar = has_arabic_script(raw)
    units: list[Unit] = []
    if ar:
        # Split into script runs so a mixed query ("Mohammed ...") keeps both.
        ar_part = " ".join(
            t for t in re.split(r"\s+", raw) if has_arabic_script(t))
        la_part = " ".join(
            t for t in re.split(r"\s+", raw) if t and not has_arabic_script(t))
        units += _arabic_units(_arabic_tokens(ar_part))
        if la_part.strip():
            units += _latin_units(_latin_tokens(la_part))
        script = "mixed" if la_part.strip() and _latin_tokens(la_part) else "arabic"
    else:
        units += _latin_units(_latin_tokens(raw))
        script = "latin"
    units = units[:MAX_UNITS]
    arabic_units = [u for u in units if u.script == "ar"]
    engaged = ar or any(u.marker for u in units)
    # NOTE a Latin query is "engaged" only on positive evidence of Arabic-name
    # structure or vocabulary. An ordinary English name never is, which is what
    # keeps every behaviour already pinned by the existing tests unchanged.
    weak = _weak_reason(units) if engaged else None
    key_all = tuple(sorted({u.text for u in arabic_units}))
    key_req = tuple(sorted({u.text for u in arabic_units
                            if not u.optional and not u.generic}))
    return Analysis(raw=raw, script=script, units=tuple(units), engaged=engaged,
                    weak_reason=weak, arabic_key=key_req, arabic_all_key=key_all)


@lru_cache(maxsize=4096)
def analyse(name: str) -> Analysis:
    """Cached analysis of one name (queries and the few list names a query reaches)."""
    return _analyse_uncached(name)


def arabic_name_key(name: str) -> tuple[str, list[str]]:
    """(name_key, tokens) for storing an Arabic-script listed name in the index.

    Mirrors how the Latin rows are stored: tokens are the name's units, name_key
    is the sorted unique tokens joined by a space."""
    a = analyse(name)
    toks = sorted({u.text for u in a.units if u.script == "ar"})
    return " ".join(toks), toks


# ---------------------------------------------------------------------------
# Name-level comparison
# ---------------------------------------------------------------------------

TOKEN_MATCH_MIN = 0.80        # below this two elements are not "the same element"
_INFO_CAP = 5.0


def unit_similarity(a: Unit, b: Unit) -> float:
    if a.script == "ar" and b.script == "ar" and a.text == b.text:
        return 1.0
    best = 0.0
    for sa in a.skels:
        for sb in b.skels:
            s = skel_similarity(sa, sb)
            if s > best:
                best = s
                if best >= 1.0:
                    return best
    return best


def _weight(u: Unit) -> float:
    w = min(skel_info(u.skels[0]), _INFO_CAP)
    return w * (0.5 if u.common else 1.0)


@dataclass(frozen=True)
class Alignment:
    score: float                      # 0..1, query coverage weighted by precision
    confidence: str                   # "high" | "medium" | "low"
    q_coverage: float
    l_coverage: float
    pairs: tuple[tuple[str, str, float, str], ...]     # (query, listed, sim, how)
    unmatched_query: tuple[str, ...]
    unmatched_listed: tuple[str, ...]
    basis: str                        # "arabic_script_exact" | "transliteration" | "romanisation_variant"
    explanation: str


_LATIN_W = re.compile(r"[wuov]")
_LATIN_Y = re.compile(r"[yiej]")


def _unsupported_weak(a: Unit, b: Unit) -> int:
    """Number of weak letters (w, y) one side has that the other side shows no
    sign of. The edit distance makes dropping them cheap, because romanisers drop
    them; this is the check that it is not being used to turn Zawahiri into Zahar.

    Across scripts, the Arabic side's waw and non-final yeh are looked for in the
    Latin word. Within Latin, a written w or y must have a w/u/o/v or y/i/e/j on
    the other side."""
    if a.script != b.script:
        ar, la = (a, b) if a.script == "ar" else (b, a)
        sk = ar.skels[0]
        lat = la.text.replace("|", "")
        n = 0
        if "w" in sk and not _LATIN_W.search(lat):
            n += 1
        if "y" in sk[:-1] and not _LATIN_Y.search(lat):
            n += 1
        return n
    if a.script == "la":
        n = 0
        for x, y in ((a.text, b.text), (b.text, a.text)):
            x = x.replace("|", "")
            y = y.replace("|", "")
            if "w" in x and not _LATIN_W.search(y):
                n += 1
            if "y" in x and not _LATIN_Y.search(y):
                n += 1
        return n
    return 0


def _how(a: Unit, b: Unit, sim: float) -> str:
    if a.script == "ar" and b.script == "ar":
        return "same_written_name" if sim >= 1.0 else "arabic_spelling_variant"
    if a.script != b.script:
        return "transliteration" if sim < 1.0 else "transliteration_exact"
    return "spelling_variant" if sim < 1.0 else "same_sound"


@dataclass(frozen=True)
class _Entry:
    """A unit, or two neighbouring units read as one word (Ahmadreza =
    Ahmad Reza; Bashar al-Asad written Basharalasad)."""
    unit: Unit
    covers: tuple[int, ...]


def _entries(units: list[Unit], allow_merge: bool) -> list[_Entry]:
    out = [_Entry(u, (i,)) for i, u in enumerate(units)]
    if allow_merge and len(units) <= MAX_MERGE_UNITS:
        for i in range(len(units) - 1):
            a, b = units[i], units[i + 1]
            for first, second in ((a, b), (b, a)):
                # Every pairing of the two words' skeleton alternatives, because
                # an alternative is how the article is carried: "Zu" + "al-Qadr"
                # is written Zolqadr, and the "l" lives in the alternative.
                sks = tuple(dict.fromkeys(
                    x + y for x in first.skels for y in second.skels))
                merged = Unit(text=first.text + second.text, script=first.script,
                              skels=sks, common=a.common and b.common)
                out.append(_Entry(merged, (i, i + 1)))
    return out


def compare(q: Analysis, listed: Analysis) -> Optional[Alignment]:
    """Align the query's name elements with a listed name's, or None.

    Query-centred on purpose (the same asymmetry _word_match_score defends): the
    question is "is the party in front of me on the list", so what matters first
    is how much of the QUERY the listed name accounts for; the listed side only
    damps the score."""
    qr = [u for u in q.units if not u.optional and not u.generic]
    lr = [u for u in listed.units if not u.optional and not u.generic]
    if not qr or not lr:
        return None

    qe = _entries(qr, True)
    le = _entries(lr, True)
    cand = []
    for qx, qn in enumerate(qe):
        for lx, ln in enumerate(le):
            # at most one side may be a merged word: two merged words matching
            # each other is a coincidence of spacing, not evidence
            if len(qn.covers) > 1 and len(ln.covers) > 1:
                continue
            s = unit_similarity(qn.unit, ln.unit)
            # A word glued from two elements is a stronger claim than one word
            # matched to one word, so it needs a closer match to count.
            glued = len(qn.covers) > 1 or len(ln.covers) > 1
            if s >= (MERGED_MATCH_MIN if glued else TOKEN_MATCH_MIN):
                # prefer the plain alignment when a merged one is no better
                cand.append((s - 0.001 * (len(qn.covers) + len(ln.covers) - 2),
                             qx, lx))
    if not cand:
        return None
    cand.sort(key=lambda x: (-x[0], x[1], x[2]))
    used_q: set[int] = set()
    used_l: set[int] = set()
    pairs: list[tuple[_Entry, _Entry, float]] = []
    for s, qx, lx in cand:
        qn, ln = qe[qx], le[lx]
        if used_q.intersection(qn.covers) or used_l.intersection(ln.covers):
            continue
        used_q.update(qn.covers)
        used_l.update(ln.covers)
        pairs.append((qn, ln, max(0.0, min(1.0, round(s + 0.001 * (
            len(qn.covers) + len(ln.covers) - 2), 4)))))

    def cw(units: list[Unit], covers: tuple[int, ...]) -> float:
        return sum(_weight(units[i]) for i in covers)

    wq_all = sum(_weight(u) for u in qr) or 1.0
    wl_all = sum(_weight(u) for u in lr) or 1.0
    wq_m = sum(cw(qr, qn.covers) for qn, ln, s in pairs)
    wl_m = sum(cw(lr, ln.covers) for qn, ln, s in pairs)
    q_cov = wq_m / wq_all                       # share of the QUERY accounted for
    l_cov = wl_m / wl_all                       # share of the LISTING accounted for
    mean_sim = (sum(cw(qr, qn.covers) * s for qn, ln, s in pairs) / wq_m) if wq_m else 0.0
    matched_info = sum(min(skel_info(qr[i].skels[0]), _INFO_CAP)
                       for qn, ln, s in pairs for i in qn.covers)
    # EVIDENCE MASS: how much identifying content the matched elements carry,
    # with the commonest Arab name elements at half weight and each element
    # discounted by how closely it matched. This is the stand-in for the
    # name-frequency data we do not have: "Hamid Abdallah Ahmad Al-Ali" is four
    # common elements and still a lot of evidence together; "Muhammad Ali" is
    # two and is not.
    evidence = sum(cw(qr, qn.covers) * s for qn, ln, s in pairs)

    # What counts as enough to surface at all. Calibrated; see the eval doc.
    if matched_info < MIN_MATCHED_INFO:
        return None
    if q_cov < MIN_Q_COVERAGE:
        return None
    if evidence < LOW_EVIDENCE:
        return None

    score = q_cov * mean_sim * (0.75 + 0.25 * l_cov)
    full = q_cov >= 0.99
    # A weak letter the Arabic spelling has and the Latin one has no trace of
    # (a waw with no w, u or o anywhere in the Latin word) was dropped by the
    # distance because it is cheap to drop. That is how "Zawahiri" ends up
    # sounding like "Zahar". It stays a candidate, but not a HIGH one.
    unsupported = sum(_unsupported_weak(qn.unit, ln.unit) for qn, ln, s in pairs)
    # When part of the query has no counterpart in the listing, the elements that
    # DID match are all the evidence there is. "Ahmed" and "Salim" are matched by
    # thousands of people, so the element that carries the identification - the
    # one that is NOT among the commonest Arab names - has to be matched closely,
    # not merely above the floor for "same element". Without this, a loose
    # sound-alike of a family name plus one common given name listed a stranger
    # (measured: it was most of what ordinary Gulf names drew from the lists).
    distinct_sim = min([s for qn, ln, s in pairs if not qn.unit.common] or [1.0])
    if (full and mean_sim >= 0.90 and l_cov >= 0.60 and evidence >= HIGH_EVIDENCE
            and not unsupported):
        conf = "high"
    elif ((full and evidence >= MEDIUM_EVIDENCE)
          or (q_cov >= 0.75 and l_cov >= 0.60 and evidence >= MEDIUM_EVIDENCE
              and distinct_sim >= PARTIAL_DISTINCT_SIM)
          # The query and the listing are the SAME set of elements, each matched
          # closely, and one of them is not among the commonest names ("Ali
          # Fadavi", "Ali Wanus"). Few consonants of evidence, but nothing left
          # over on either side, which is what a coincidence would leave.
          or (full and l_cov >= 0.99 and distinct_sim >= PARTIAL_DISTINCT_SIM
              and any(not qn.unit.common for qn, ln, s in pairs)
              and evidence >= TIGHT_EVIDENCE)):
        conf = "medium"
    else:
        conf = "low"

    only_ar = all(qn.unit.script == "ar" and ln.unit.script == "ar"
                  for qn, ln, s in pairs)
    all_same_script = all(qn.unit.script == ln.unit.script for qn, ln, s in pairs)
    # "Exact" is a claim about the WHOLE written name, titles and generic words
    # included - not about the elements that were scored. A query with a title the
    # listing lacks ("Sheikh Ayman al-Zawahiri") aligns perfectly on what is left
    # and is still not the written name the publisher printed.
    same_written_name = (
        all(u.script == "ar" for u in q.units) and all(u.script == "ar" for u in listed.units)
        and {u.text for u in q.units} == {u.text for u in listed.units})
    if (only_ar and same_written_name and all(s >= 1.0 for _, _, s in pairs)
            and q_cov >= 0.99 and l_cov >= 0.99):
        basis = "arabic_script_exact"
    elif all_same_script and q.script == "latin":
        basis = "romanisation_variant"
    else:
        basis = "transliteration"

    out_pairs = tuple(
        (qn.unit.text.replace("|", ""), ln.unit.text.replace("|", ""),
         round(s, 2), _how(qn.unit, ln.unit, s))
        for qn, ln, s in pairs)
    un_q = tuple(qr[i].text.replace("|", "") for i in range(len(qr)) if i not in used_q)
    un_l = tuple(lr[i].text.replace("|", "") for i in range(len(lr)) if i not in used_l)
    return Alignment(
        score=round(score, 3), confidence=conf, q_coverage=round(q_cov, 3),
        l_coverage=round(l_cov, 3), pairs=out_pairs, unmatched_query=un_q,
        unmatched_listed=un_l, basis=basis,
        explanation=explain(basis, conf, len(qr), len(pairs), un_q, un_l, mean_sim))


MAX_MERGE_UNITS = 8
MAX_NAME_CHARS = 300
MAX_UNITS = 24
MAX_INDEX_PAIR_UNITS = 4      # longer names are not indexed as glued pairs
MIN_MATCHED_INFO = 3.5
MIN_Q_COVERAGE = 0.66
LOW_EVIDENCE = 2.0
MEDIUM_EVIDENCE = 3.5
HIGH_EVIDENCE = 5.0
MERGED_MATCH_MIN = 0.92       # a glued word (Ahmadreza = Ahmad Reza) needs a closer match
PARTIAL_DISTINCT_SIM = 0.95   # partly covered query: its distinctive element must match this closely
TIGHT_EVIDENCE = 2.6          # same elements both sides, each closely matched: evidence floor


def explain(basis: str, conf: str, n_query: int, n_matched: int,
            un_q: tuple, un_l: tuple, mean_sim: float) -> str:
    """A sentence that says what kind of evidence this is, in OUR words only.

    It carries no text from the publisher's list: those strings are third-party
    data and travel in token_alignment, where they are structured data."""
    if basis == "arabic_script_exact":
        return ("The query is the same written name as an Arabic-script alias "
                "the publisher lists, after folding only spelling conventions "
                "(diacritics, alef and yeh forms, ta marbuta, word spacing). "
                "Treat it as you would an exact name match.")
    how = ("sounds like" if basis == "transliteration"
           else "is a spelling variant of")
    s = (f"{n_matched} of {n_query} name element(s) in the query {how} "
         f"element(s) of this listed name (average similarity {mean_sim:.2f}). ")
    if un_q:
        s += f"{len(un_q)} query element(s) have no counterpart in it. "
    if un_l:
        s += (f"The listing has {len(un_l)} further element(s) the query does "
              f"not (patronymic, family or alias parts). ")
    s += ("This is a sound-based CANDIDATE, not a finding: Arabic has no single "
          "romanisation, so identity must be confirmed against the official "
          "source and your own records.")
    if conf == "low":
        s += " Confidence is LOW: a shared element may be coincidence."
    return s


def exact_alignment(q: Analysis) -> Alignment:
    """The alignment of an Arabic-script query with an Arabic-script alias that
    is EQUAL to it element for element. Built directly, not scored, because
    equality needs no threshold: a short name must not lose its exact match to a
    cut-off meant for fuzzy ones."""
    req = [u for u in q.units if u.script == "ar"]
    pairs = tuple((u.text, u.text, 1.0, "same_written_name") for u in req)
    return Alignment(
        score=1.0, confidence="high", q_coverage=1.0, l_coverage=1.0,
        pairs=pairs, unmatched_query=(), unmatched_listed=(),
        basis="arabic_script_exact",
        explanation=explain("arabic_script_exact", "high", len(req), len(req),
                            (), (), 1.0))


def compare_names(query: str, listed_name: str) -> Optional[Alignment]:
    """Convenience: analyse both and compare."""
    return compare(analyse(query), analyse(listed_name))


# ---------------------------------------------------------------------------
# Retrieval support (the database path)
# ---------------------------------------------------------------------------

# Latin letters that survive every romanisation of the Arabic letter they stand
# for, so a pattern built from them cannot exclude a true spelling. 'd' is left
# out (dh, z, th), as are q/k/g, s/z/th/t and every h/kh.
_INVARIANT = frozenset("mnlrbf")


# The Latin spellings each skeleton symbol may stand for, as POSIX regex
# fragments. This is the retrieval counterpart of the substitution table: every
# confusion the distance treats as cheap is spelled out here, so a regex built
# from a skeleton fetches the rows the distance would accept.
_RX = {
    "b": "[bp]", "p": "[pb]", "t": "(th|t|d)", "3": "(th|t|s|dh|z)",
    "j": "(dj|dg|zh|j|g|y|ch)", "c": "(tch|ch|c|k|sh|x|s)", "h": "(kh|h)?",
    "x": "(kh|h|k|ch|x)", "d": "(dh|d|t|z)", "4": "(dh|d|z|th|t)", "r": "r",
    "z": "(z|dh|th|s|d)", "s": "(s|z|th|c)", "S": "(sh|ch|s)",
    "G": "(gh|g|kh)", "f": "(f|ph|v|p)", "q": "(q|k|g|c|kh|gh)",
    "k": "(k|c|q|ck|g|kh)", "g": "(g|gh|j|k|q)", "l": "l", "m": "m", "n": "n",
    "w": "(w|v|u|o)?", "y": "(y|i|j|e)?", "T": "(t|h)?",
}


def skeleton_regex(sk: str) -> str:
    """POSIX (Postgres ARE) regex matching a whole lowercase Latin token whose
    sound skeleton is close to `sk`: consonant classes in order, vowels free
    between them, an optional article in front.

    Token boundaries are the start of the string and the single space the index
    puts between tokens, written as (^| ) and ( |$) rather than with the regex
    word-boundary escapes: this text travels inside a quoted PostgREST filter,
    where a backslash is an escape character and would be re-read."""
    parts = ["(^| )(a?l)?[aeiouy]*"]
    for ch in sk:
        frag = _RX.get(ch)
        if frag is None:
            frag = ch if ch.isdigit() else ""
        if not frag:
            continue
        parts.append(frag if frag.endswith("?") else frag + "+")
        parts.append("[aeiouy]*")
    parts.append("( |$)")
    return "".join(parts)


def retrieval_filters(a: Analysis, max_elements: int = 3) -> list[dict]:
    """PostgREST filters (on name_key) that over-fetch the Latin index rows which
    could be this query, for the most identifying elements.

    Each element becomes a regex (skeleton_regex). Two elements are AND-ed,
    because a true match carries both: that is what keeps a common-consonant
    regex from returning a sixth of the list. With one usable element there is
    nothing to AND and the single regex goes alone. Elements that carry too
    little (under 2.8 consonants' worth) are skipped: their regex would match a
    large share of the index and prove nothing. compare() re-scores everything
    that comes back, so over-fetching costs rows, never correctness."""
    scored = []
    for u in a.units:
        if u.optional or u.generic:
            continue
        sk = u.skels[0]
        if skel_info(sk) < 2.8:
            continue
        scored.append((u.common, -skel_info(sk), skeleton_regex(sk)))
    scored.sort()
    rxs: list[str] = []
    for _, _, rx in scored:
        if rx not in rxs:
            rxs.append(rx)
        if len(rxs) >= max_elements:
            break
    if not rxs:
        return []
    if len(rxs) == 1:
        return [{"name_key": "imatch." + rxs[0]}]
    out = []
    for i in range(len(rxs)):
        for j in range(i + 1, len(rxs)):
            out.append({"and": '(name_key.imatch."%s",name_key.imatch."%s")'
                               % (rxs[i], rxs[j])})
    return out


def retrieval_patterns(a: Analysis, max_patterns: int = 3) -> list[str]:
    """ILIKE patterns (PostgREST `*` wildcards) that over-fetch the Latin
    rows which could be this query, for the most identifying elements.

    The index stores names as sorted lowercase token strings, so a subsequence
    of the invariant consonants of one element is a necessary condition for it
    to be present. The caller re-scores everything fetched with compare()."""
    scored = []
    for u in a.units:
        if u.optional or u.generic:
            continue
        inv = [c for c in u.skels[0] if c in _INVARIANT]
        if len(inv) < 2:
            continue
        # prefer elements that are rare and long
        scored.append((u.common, -len(inv), "*" + "*".join(inv) + "*"))
    scored.sort()
    seen, out = set(), []
    for _, _, pat in scored:
        if pat not in seen:
            seen.add(pat)
            out.append(pat)
        if len(out) >= max_patterns:
            break
    return out


# ---------------------------------------------------------------------------
# In-memory index for the OFAC list (about 29,000 names with aliases)
# ---------------------------------------------------------------------------

_COMPAT: dict[str, frozenset] = {}
for (_a, _b), _c in _SUB.items():
    if _c < 0.7:
        _COMPAT.setdefault(_a, set()).add(_b)          # type: ignore[arg-type]
for _k in list(_COMPAT):
    _COMPAT[_k] = frozenset(_COMPAT[_k] | {_k})        # type: ignore[operator]


def _leads(sk: str) -> list[str]:
    """The symbols a skeleton may be reached by: its first real consonant, and
    the one after it when the first is an h (romanisers drop it)."""
    out = []
    for i, c in enumerate(sk):
        if c in "wyT":
            continue
        out.append(c)
        if c == "h" and i + 1 < len(sk) and sk[i + 1] not in "wyT":
            out.append(sk[i + 1])
        break
    return out or [sk[0]]


class SkeletonIndex:
    """Token-skeleton inverted index over a set of names.

    OFAC's list is parsed from a CSV held in this process. Scoring every name
    against a query on every call is affordable for the exact matcher and not
    for an edit-distance one, so the names are analysed ONCE per copy of the
    list and a query is compared against the DISTINCT skeletons only.

    Finding the skeletons near a query one is done with a bigram filter: two
    skeletons within the similarity bar (at most one real edit, or a couple of
    cheap ones) must share some adjacent pair of consonants, so only the
    skeletons sharing enough pairs are put through the edit-distance. Skeletons
    of three symbols or fewer are too short for that and are scanned in their own
    small bucket.

    Memory is the constraint this service has been killed for before, so the
    index keeps only the name strings, a small metadata object and integer
    posting lists. The unit structure is recomputed (and cached) for the few
    hundred names a query actually reaches."""

    def __init__(self) -> None:
        self.names: list[str] = []
        self.meta: list[object] = []
        self._kid: Optional[dict[str, int]] = {}
        self._keys: list[str] = []
        self._post: list = []                  # list[list[int]] while building
        self._bigram: dict = {}
        self._short: dict = {}
        self._frozen = False

    def freeze(self) -> "SkeletonIndex":
        """Stop adding; pack the posting lists into flat integer arrays and drop
        the build-time lookup table. A frozen index is about a third the size.
        search() works either way, so freezing is an optimisation, not a mode."""
        if self._frozen:
            return self
        off = array("I", [0])
        data = array("I")
        for lst in self._post:
            data.extend(lst)
            off.append(len(data))
        self._post = (off, data)
        self._bigram = {g: array("I", ids) for g, ids in self._bigram.items()}
        self._short = {n: array("I", ids) for n, ids in self._short.items()}
        self._kid = None
        self._frozen = True
        return self

    def _posting(self, kid: int):
        if self._frozen:
            off, data = self._post
            return data[off[kid]:off[kid + 1]]
        return self._post[kid]

    def _add_key(self, sk: str, idx: int) -> None:
        if self._frozen:
            raise RuntimeError("SkeletonIndex is frozen")
        kid = self._kid.get(sk)
        if kid is None:
            kid = len(self._keys)
            self._kid[sk] = kid
            self._keys.append(sk)
            self._post.append([idx])
            if len(sk) <= 3:
                self._short.setdefault(len(sk), []).append(kid)
            else:
                for g in {sk[i:i + 2] for i in range(len(sk) - 1)}:
                    self._bigram.setdefault(g, []).append(kid)
        else:
            post = self._post[kid]
            if post[-1] != idx:
                post.append(idx)

    def add(self, name: str, meta: object) -> None:
        idx = len(self.names)
        self.names.append(name)
        self.meta.append(meta)
        req = [u for u in _analyse_uncached(name).units
               if not u.optional and not u.generic]
        for u in req:
            for sk in u.skels:
                self._add_key(sk, idx)
        # Neighbouring elements are ALSO indexed as one word, because lists
        # write "Ahmad Reza" as Ahmadreza and Persian input does the reverse.
        if len(req) <= MAX_INDEX_PAIR_UNITS:
            # Both orders: OFAC writes "SURNAME, Given" and people type the reverse.
            for a, b in zip(req, req[1:]):
                self._add_key(a.skels[0] + b.skels[0], idx)
                self._add_key(b.skels[0] + a.skels[0], idx)

    def __len__(self) -> int:
        return len(self.names)

    def _near(self, sk: str) -> list[int]:
        """Key ids whose skeleton is within the token-match bar of `sk`."""
        n = len(sk)
        out: list[int] = []
        if n <= 3:
            leads: set[str] = set()
            for lead in _leads(sk):
                leads |= _COMPAT.get(lead, frozenset({lead}))
            for ln in range(max(1, n - 1), 5):
                for kid in self._short.get(ln, ()):
                    cand = self._keys[kid]
                    if _leads(cand)[0] in leads or cand == sk:
                        if cand == sk or skel_similarity(sk, cand) >= TOKEN_MATCH_MIN:
                            out.append(kid)
            return out
        grams = {sk[i:i + 2] for i in range(n - 1)}
        need = 1 if n == 4 else 2 if n < 8 else max(2, int(0.3 * len(grams)))
        count: dict[int, int] = {}
        for g in grams:
            for kid in self._bigram.get(g, ()):
                count[kid] = count.get(kid, 0) + 1
        keys = self._keys
        for kid, c in count.items():
            if c < need:
                continue
            cand = keys[kid]
            if abs(len(cand) - n) > 2:
                continue
            if cand == sk or skel_similarity(sk, cand) >= TOKEN_MATCH_MIN:
                out.append(kid)
        # short keys can still be a match for a 4-symbol query (one deleted)
        if n == 4:
            for kid in self._short.get(3, ()):
                cand = keys[kid]
                if skel_similarity(sk, cand) >= TOKEN_MATCH_MIN:
                    out.append(kid)
        return out

    def search(self, q: Analysis, limit: int = 8, min_confidence: str = "medium"
               ) -> list[tuple[Alignment, int]]:
        if not q.phonetic_ok:
            return []
        want = _CONF_RANK[min_confidence]
        hit_idx: set[int] = set()
        qsk: list[tuple[str, bool]] = []
        req = [u for u in q.units if not u.optional and not u.generic]
        for u in req:
            for sk in u.skels:
                qsk.append((sk, u.common))
        if len(req) <= MAX_MERGE_UNITS:
            for a, b in zip(req, req[1:]):
                both_common = a.common and b.common
                qsk.append((a.skels[0] + b.skels[0], both_common))
                qsk.append((b.skels[0] + a.skels[0], both_common))
        # Arabic writes the long vowels that romanisation does not: the same
        # skeleton without w, y and ta marbuta is looked up as well.
        qsk += [(_core(sk), c) for sk, c in qsk if _core(sk) != sk and len(_core(sk)) >= 3]
        # Candidate names must contain a skeleton near a NON-common query element
        # (or near any element when all of them are common).
        has_distinctive = any(not c for _, c in qsk)
        for sk, common in qsk:
            if has_distinctive and common:
                continue
            for kid in self._near(sk):
                hit_idx.update(self._posting(kid))
        results: list[tuple[Alignment, int]] = []
        for idx in hit_idx:
            al = compare(q, analyse(self.names[idx]))
            if al is None or _CONF_RANK[al.confidence] < want:
                continue
            results.append((al, idx))
        results.sort(key=lambda r: (-_CONF_RANK[r[0].confidence], -r[0].score, r[1]))
        return results[:limit]


_CONF_RANK = {"low": 0, "medium": 1, "high": 2}
