# -*- coding: utf-8 -*-
"""
core/arabic_names.py: what it normalises, what it compares, what it refuses.

Every Arabic name in here is a name the EU, the UK or OFAC publishes (see
tests/fixtures/sanctions_arabic_real_entries.json and the 2026-10-03 snapshot it
records), or an ordinary Gulf name that is on no list. Nothing is invented to fit.

THE PROPERTIES PINNED, in the order a reader would worry about them:

  1. Spelling conventions are not identity. Diacritics, tatweel, the alef family,
     ya/alef maqsura, ta marbuta/heh and word spacing change how a name is
     WRITTEN, not who it is.
  2. Name structure is not content. The article, bin/ibn/bint, titles, and the
     compounds that are spaced either way (Abd al-Rahman, Abu Bakr, Nur al-Din)
     are normalised before anything is compared.
  3. Arabic and Latin land in the same sound space, so an Arabic query is compared
     with a Latin list entry directly.
  4. A sound-alike is graded, and the grade can be wrong in only one direction:
     toward caution. A name made of very common elements, or of generic words, or
     too short to identify anyone, is never HIGH, and often not listed at all.
  5. An ordinary English name never engages any of this.
"""
from __future__ import annotations

import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import arabic_names as an  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sanctions_arabic_real_entries.json")


def _entities():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)["entities"]


def _cmp(q: str, listed: str):
    return an.compare(an.analyse(q), an.analyse(listed))


# --------------------------------------------------------------------------
# 1. Orthography
# --------------------------------------------------------------------------

def test_script_detection():
    assert an.has_arabic_script("الظواهري")
    assert an.has_arabic_script("Ayman الظواهري")
    assert not an.has_arabic_script("Ayman Al-Zawahiri")
    assert not an.has_arabic_script("Сбербанк")
    assert not an.has_arabic_script("")


@pytest.mark.parametrize("variant", [
    "حامد عبد الله أحمد العلي",             # as published (EU 4140)
    "حامد عبدالله احمد العلي",              # alef-hamza written plain, name closed up
    "حَامِد عَبْد اللَّه أَحْمَد العَلِي",    # fully vowelled
    "حـــامد عبد الله أحمد العلي",          # tatweel
    "العلي أحمد عبد الله حامد",             # the other order
    "  حامد   عبد الله  أحمد  العلي ",      # stray spacing
])
def test_one_written_name_has_one_key(variant):
    assert an.arabic_name_key(variant) == an.arabic_name_key("حامد عبد الله أحمد العلي")


def test_ya_alef_maqsura_and_ta_marbuta_fold():
    # Mustafa written with a dotless ya, or an alef maqsura; Fatima with heh or ta marbuta
    assert an.normalise_arabic("مصطفى") == an.normalise_arabic("مصطفي")
    assert an.normalise_arabic("فاطمة") == an.normalise_arabic("فاطمه")


def test_persian_and_arabic_letter_variants_fold():
    # Persian yeh/kaf (U+06CC, U+06A9) against the Arabic ones; this is how one
    # Iranian name is typed on two keyboards
    assert an.normalise_arabic("محمد علی") == an.normalise_arabic("محمد علي")
    assert an.normalise_arabic("اکبر") == an.normalise_arabic("اكبر")


def test_digits_and_invisible_characters_fold():
    assert an.normalise_arabic("حزب ١٤") == an.normalise_arabic("حزب 14")
    assert an.normalise_arabic("عبد‌الله") == an.normalise_arabic("عبدالله")


def test_latin_folding_keeps_the_letters_a_romaniser_marks():
    # ALA-LC style: the old _normalize_name DELETES the dotted h and the macron
    # vowels outright; this keeps the letter
    assert an.fold_latin("Muḥammad ʿAlī al-Ṣāliḥ") == "muhammad ali al-salih"
    assert an.fold_latin("Da'esh") == "daesh"


# --------------------------------------------------------------------------
# 2. Structure
# --------------------------------------------------------------------------

def _texts(name):
    return [u.text.replace("|", "") for u in an.analyse(name).units]


def test_the_article_is_not_part_of_the_name():
    assert _texts("الظواهري") == _texts("ظواهري") == ["ظواهري"]
    assert _texts("Ayman Al-Zawahiri") == ["ayman", "zawahiri"]
    assert _texts("Ayman az-Zawahiri") == ["ayman", "zawahiri"]
    assert _texts("Ayman ash-Sharif") == ["ayman", "sharif"]


def test_bin_ibn_bint_are_particles_not_names():
    assert _texts("خالد بن محمد") == ["خالد", "محمد"]
    assert _texts("Khalid bin Mohammed") == ["khalid", "mohammed"]
    assert _texts("Fatima bint Ali") == ["fatima", "ali"]


@pytest.mark.parametrize("a,b", [
    ("Abd al-Rahman", "Abdurrahman"),
    ("Abdul Rahman", "Abdelrahman"),
    ("Abd Allah", "Abdullah"),
    ("Abu Bakr", "Abubakr"),
    ("Abou Bakr", "Abu-Bakr"),
    ("Nur al-Din", "Noureddine"),
    ("Saif Allah", "Saifullah"),
])
def test_compound_elements_are_the_same_however_they_are_spaced(a, b):
    sa = an.analyse(a)
    sb = an.analyse(b)
    best = max(an.unit_similarity(x, y) for x in sa.units for y in sb.units)
    assert best >= 0.9, (a, b, [u.skels for u in sa.units], [u.skels for u in sb.units])
    # and spaced Arabic equals closed Arabic exactly
    assert an.arabic_name_key("عبد الله") == an.arabic_name_key("عبدالله")
    assert an.arabic_name_key("نور الدين") == an.arabic_name_key("نورالدين")
    assert an.arabic_name_key("أبو بكر") == an.arabic_name_key("أبوبكر")


def test_titles_are_optional_not_required():
    u = an.analyse("الشيخ حسن نصر الله").units
    assert [x.optional for x in u][:1] == [True]
    q = an.analyse("Sheikh Hassan Nasrallah")
    assert any(x.optional for x in q.units)
    # a title one side has and the other lacks costs nothing
    assert _cmp("Hassan Nasrallah", "Sheikh Hassan Nasrallah") is not None


def test_arabic_generic_words_identify_nobody():
    units = an.analyse("شركة النور للتجارة").units
    assert any(u.generic for u in units)
    # the whole point of the English _GENERIC_NAME_WORDS list, in Arabic: two
    # trading companies share "company" and "trading", not an identity
    assert _cmp("شركة النور للتجارة", "شركة الرافدين للتجارة") is None


# --------------------------------------------------------------------------
# 3. One sound space for both scripts
# --------------------------------------------------------------------------

@pytest.mark.parametrize("arabic,latin", [
    ("محمد", "Mohammed"), ("محمد", "Muhammad"), ("محمد", "Mohamed"),
    ("خالد", "Khalid"), ("خالد", "Khaled"),
    ("يوسف", "Yousef"), ("يوسف", "Yusuf"),
    ("عثمان", "Othman"), ("عبد الرحمن", "Abdelrahman"),
    ("عبد الرحمن", "Abd al-Rahman"),
    ("القذافي", "Gaddafi"), ("القذافي", "Qaddafi"), ("القذافي", "Kadhafi"),
    ("الظواهري", "Zawahiri"), ("الظواهري", "Zawahri"),
    ("نصر الله", "Nasrallah"), ("حزب الله", "Hizballah"), ("حزب الله", "Hezbollah"),
    ("شريف", "Cherif"), ("شريف", "Sharif"),
    ("رضا", "Reza"), ("رضا", "Rida"),
])
def test_arabic_and_latin_spellings_of_one_name_sound_alike(arabic, latin):
    a = an.analyse(arabic).units
    b = an.analyse(latin).units
    best = 0.0
    for x in a:
        for y in b:
            best = max(best, an.unit_similarity(x, y))
    # also the merged reading: "Nas rallah" = Nasrallah
    if best < an.TOKEN_MATCH_MIN and len(a) == 2:
        merged = an.Unit(text="m", script="ar", skels=(a[0].skels[0] + a[1].skels[0],))
        best = max(an.unit_similarity(merged, y) for y in b)
    assert best >= an.TOKEN_MATCH_MIN, (arabic, latin, best)


@pytest.mark.parametrize("arabic,latin", [
    ("الظواهري", "Rahimi"), ("محمد", "Karim"), ("خالد", "Hamad"),
    ("نصر الله", "Nasser"), ("بلال", "Khalid"),
])
def test_different_names_do_not_sound_alike(arabic, latin):
    a = an.analyse(arabic).units
    b = an.analyse(latin).units
    for x in a:
        for y in b:
            if y.text in ("jr", "khan") or x.optional or y.optional:
                continue
            assert an.unit_similarity(x, y) < an.TOKEN_MATCH_MIN, (arabic, latin)


def test_sound_alike_is_not_same_person_and_the_matcher_says_so_in_the_grade():
    # Hasan, Hassan and Hussein share a consonant skeleton. They are common
    # names. The candidate may surface; it must never be called HIGH.
    al = _cmp("حسن علي", "Hussein Ali")
    assert al is None or al.confidence in ("low",)


# --------------------------------------------------------------------------
# 4. Real entries: the same party, two scripts
# --------------------------------------------------------------------------

# Entries where the Arabic-script name and one of the Latin names are the SAME
# STRING OF NAME ELEMENTS in two scripts. Pinned by id so a regression names the
# party. Others in the fixture carry a different alias in each script (an
# 'Abu Jihad' with an Arabic nasab), which is the lists' choice, not a matching
# failure; test_gold_recall_over_the_fixture covers the whole set.
SAME_NAME_IN_TWO_SCRIPTS = [
    ("EU", "551"), ("EU", "534"), ("EU", "923"), ("EU", "514"), ("EU", "1095"),
    ("EU", "3780"), ("EU", "3782"), ("EU", "3808"), ("EU", "4000"), ("EU", "4140"),
    ("EU", "5529"), ("EU", "5759"), ("EU", "6017"), ("EU", "6177"), ("EU", "6215"),
    ("EU", "6303"), ("EU", "6304"), ("EU", "6307"), ("EU", "6310"), ("EU", "6400"),
    ("EU", "6478"), ("EU", "6612"),
    ("UK", "IRQ0145"), ("UK", "IRQ0097"), ("UK", "IRQ0112"), ("UK", "AQD0190"),
    ("UK", "AQD0271"), ("UK", "AQD0305"), ("UK", "IRN0168"), ("UK", "SYR0023"),
]


@pytest.mark.parametrize("lst,eid", SAME_NAME_IN_TWO_SCRIPTS)
def test_the_arabic_script_name_reaches_its_latin_entry(lst, eid):
    e = next(x for x in _entities() if (x["list"], x["id"]) == (lst, eid))
    best = None
    for ar in e["arabic"]:
        for lat in e["latin"]:
            al = _cmp(ar, lat)
            if al and (best is None or an._CONF_RANK[al.confidence] > an._CONF_RANK[best.confidence]):
                best = al
    assert best is not None, (e["latin"][:3], e["arabic"][:2])
    assert best.confidence in ("high", "medium"), (best.confidence, e["latin"][:3])
    assert best.basis == "transliteration"


def test_gold_recall_over_the_fixture():
    """The aggregate the thresholds were calibrated on, in miniature: of the
    fixture's INDIVIDUALS whose Arabic name has a Latin counterpart, how many does
    the Arabic name reach through ANY of the party's Latin names at medium or
    above? The full-list figure is in docs/ARABIC_SANCTIONS_EVAL.md."""
    inds = [e for e in _entities() if e["etype"] == "INDIVIDUAL"]
    hit = 0
    for e in inds:
        ok = False
        for ar in e["arabic"]:
            for lat in e["latin"]:
                al = _cmp(ar, lat)
                if al and al.confidence in ("high", "medium"):
                    ok = True
        hit += ok
    assert hit / len(inds) >= 0.80, f"{hit}/{len(inds)}"


def test_only_the_nature_of_the_evidence_changes_the_basis():
    # Arabic against its own published alias is the one 'exact' basis
    e = next(x for x in _entities() if (x["list"], x["id"]) == ("EU", "4140"))
    al = _cmp(e["arabic"][0], e["arabic"][0])
    assert al is not None and al.basis == "arabic_script_exact"
    # Latin against Latin is a spelling variant, never 'exact'
    al = _cmp("Mohammed al-Zawahiri", "Muhammad Al-Zawahri")
    assert al is not None and al.basis == "romanisation_variant"
    # Arabic against Latin is a transliteration
    al = _cmp("الظواهري محمد", "Muhammad Al-Zawahri")
    assert al is not None and al.basis == "transliteration"


def test_a_title_the_listing_lacks_keeps_it_from_being_called_exact():
    """'Exact' is about the whole written name. A query with a title the listing
    does not carry aligns perfectly on the elements that remain and is still a
    different written name, so it must not be explained as 'an exact name match'
    (found by an independent review of the first version)."""
    e = next(x for x in _entities() if (x["list"], x["id"]) == ("EU", "4140"))
    alias = e["arabic"][0]
    plain = _cmp(alias, alias)
    assert plain is not None and plain.basis == "arabic_script_exact"
    titled = _cmp("الشيخ " + alias, alias)
    assert titled is not None
    assert titled.basis != "arabic_script_exact"
    assert "exact name match" not in titled.explanation


def test_every_candidate_explains_itself_in_our_words():
    al = _cmp("أيمن الظواهري", "Ayman Al-Zawahari")
    assert al is not None
    assert "CANDIDATE" in al.explanation and "not a finding" in al.explanation
    assert al.pairs and all(len(p) == 4 for p in al.pairs)
    # the explanation is a template: no text from the listed name leaks into prose
    assert "zawahari" not in al.explanation.lower()


# --------------------------------------------------------------------------
# 5. Caution
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["علي", "محمد", "أحمد السيد", "ياس"])
def test_a_name_too_short_or_too_common_is_not_searched_by_sound(name):
    a = an.analyse(name)
    assert a.weak_reason and not a.phonetic_ok


def test_common_elements_alone_are_not_evidence():
    # "Muhammad Ali" is on thousands of passports. Listed 'Muhammad Ali DURGHAM'
    # shares both elements; that is not a candidate worth a caller's attention.
    al = _cmp("محمد علي", "Muhammad Ali Durgham")
    assert al is None or al.confidence == "low"


def test_four_common_elements_together_are_evidence():
    # ...but a full four-element name made of common elements is still a lot
    al = _cmp("حامد عبد الله أحمد العلي", "HAMID ABDALLAH AHMAD AL-ALI")
    assert al is not None and al.confidence in ("high", "medium")


def test_a_weak_letter_with_no_trace_in_the_latin_name_blocks_high():
    # waw in al-Zawahiri has a w in Zawahiri; not in al-Zahar
    good = _cmp("أيمن الظواهري", "Ayman Al-Zawahiri")
    bad = _cmp("محمد الظواهري", "Mahmoud Al-Zahar")
    assert good is not None and good.confidence == "high"
    assert bad is None or bad.confidence != "high"


def test_an_unrelated_listing_does_not_come_back():
    for listed in ("Kim Jong Un", "Vladimir Putin", "Sberbank of Russia",
                   "Ramzan Kadyrov", "Rosneft Oil Company"):
        assert _cmp("أيمن الظواهري", listed) is None
        assert _cmp("سالم بن سعيد البلوشي", listed) is None


# --------------------------------------------------------------------------
# 6. English names never engage the layer
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Maria Garcia", "John Smith Consulting", "Star Trading LLC", "Acme Trading LLC",
    "Joe's Pizza LLC", "Muscat Coffee House", "Bright Star Trading Company",
    "Gulf General Trading LLC", "Sam's Barbershop", "Kim Jong Un", "Vladimir Putin",
    "Rosneft", "Sberbank", "Wagner Group", "Bank Melli Iran",
])
def test_ordinary_names_are_not_engaged(name):
    a = an.analyse(name)
    assert not a.engaged, [u for u in a.units if u.marker]


@pytest.mark.parametrize("name", [
    "Mohammed Hassan", "Abu Bakr", "Abdul Rahman", "Salim bin Said Al Balushi",
    "Nur al-Din", "Hamid Abdallah Ahmad Al-Ali",
])
def test_arabic_structure_engages_it(name):
    assert an.analyse(name).engaged


# --------------------------------------------------------------------------
# 7. Retrieval support
# --------------------------------------------------------------------------

def _regex_for(sk: str):
    return re.compile(an.skeleton_regex(sk), re.IGNORECASE)


@pytest.mark.parametrize("token", ["zawahiri", "zawahri", "zawahari", "dhawahri",
                                   "alzawahiri"])
def test_the_retrieval_regex_fetches_the_spellings_the_lists_use(token):
    # " tok " the way the index stores a token inside a name_key
    assert _regex_for(an.arabic_skeleton(an.analyse("ظواهري").units[0].text)).search(
        f"aiman {token} muhammad rabi")


def test_the_retrieval_regex_does_not_fetch_everything():
    rx = _regex_for(an.arabic_skeleton(an.analyse("ظواهري").units[0].text))
    for token in ("rahimi", "mohammed", "khalid", "putin", "sberbank", "salman"):
        assert not rx.search(f"x {token} y")


def test_retrieval_filters_and_two_elements_and_skip_short_ones():
    two = an.retrieval_filters(an.analyse("أيمن الظواهري"))
    one = an.retrieval_filters(an.analyse("الظواهري"))
    assert one and "and" not in one[0] and one[0]["name_key"].startswith("imatch.")
    # ayman is too short to anchor a regex on its own; zawahiri alone is used
    assert two and "name_key" in two[0]
    both = an.retrieval_filters(an.analyse("محمد باقر الظواهري"))
    assert any("and" in f for f in both)
    # no backslash anywhere: the text travels inside a quoted PostgREST filter
    assert all("\\" not in str(v) for f in both for v in f.values())


# --------------------------------------------------------------------------
# 8. The in-process index (OFAC)
# --------------------------------------------------------------------------

def test_skeleton_index_finds_the_party_and_keeps_only_light_state():
    idx = an.SkeletonIndex()
    names = ["AL ZAWAHIRI, Dr. Ayman", "AL-ASSAD, Bashar", "HIZBALLAH",
             "KIM, Jong Un", "ROSNEFT OIL COMPANY", "NASRALLAH, Hassan",
             "AGHA, Haji Abdul Manan", "AL-FAWAZ, Khalid Abd al-Rahman Hamd"]
    for n, name in enumerate(names):
        idx.add(name, n)
    got = idx.search(an.analyse("أيمن الظواهري"), limit=5, min_confidence="medium")
    assert got and idx.names[got[0][1]] == "AL ZAWAHIRI, Dr. Ayman"
    got = idx.search(an.analyse("بشار الأسد"), limit=5, min_confidence="medium")
    assert got and idx.names[got[0][1]] == "AL-ASSAD, Bashar"
    got = idx.search(an.analyse("حزب الله"), limit=5, min_confidence="medium")
    assert got and idx.names[got[0][1]] == "HIZBALLAH"
    got = idx.search(an.analyse("عبد المنان آغا"), limit=5, min_confidence="medium")
    assert got and idx.names[got[0][1]] == "AGHA, Haji Abdul Manan"
    # nothing for a name that is on no list, and nothing from a weak query
    assert not idx.search(an.analyse("سالم بن سعيد البلوشي"), 5, "medium")
    assert not idx.search(an.analyse("محمد"), 5, "low")
    # memory: strings and integer posting lists only, no per-name objects
    assert not any(hasattr(idx, a) for a in ("_units", "analyses"))
