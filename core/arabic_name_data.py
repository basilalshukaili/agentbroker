# -*- coding: utf-8 -*-
"""
Word lists for core/arabic_names.py.

KEPT IN ITS OWN FILE ON PURPOSE. The matching logic in arabic_names.py is
ASCII-only and spells every Arabic letter as a code point, so nothing in it can
be corrupted by an editor or a console. This file is the opposite: it is DATA,
it is meant to be read by a person who reads Arabic, and the readable form is
the safe form. Every entry is written exactly as a person would write it, WITHOUT
short-vowel marks; arabic_names.normalise_arabic() is applied to each entry when
the sets are built, so the spelling variants (hamza on alef, ta marbuta, alef
maqsura, Persian yeh/kaf) do not have to be listed here.

NOTHING HERE IS A SANCTIONS LIST. These are the ingredients of the matcher's
judgement about how much a shared word proves:

  (The words that join names together and identify nobody - bin, ibn, bint, the
  article al- - are structure, not data, and live in arabic_names.py.)

  * TITLES      - honorifics that a listing may or may not carry.
  * GENERIC     - the Arabic counterparts of "Company", "Trading", "Group",
                  "Holding": a name made of these identifies nobody. This is the
                  Arabic twin of _GENERIC_NAME_WORDS in screen_sanctions.py and
                  exists for the same measured reason (an ordinary trading
                  company reported as sanctioned because it shared "Trading
                  LLC" with a listed one).
  * COMMON      - the most frequent name elements in the Arab world. Sharing
                  "Muhammad" with a listed party is nearly meaningless; sharing
                  "Nasrallah" is not. We have no population name-frequency
                  data (screen_sanctions.py says so at length), so this is a
                  short, explicit, curated list rather than a statistical claim.
  * GIVEN       - a wider list of Arabic/Persian/Pashto name elements. It is
                  used for ONE thing: deciding whether a Latin-script query
                  looks Arabic-origin enough to be worth the transliteration
                  search at all.
"""

# Titles. Matched after the article is stripped, so "الشيخ" and "شيخ" are one.
TITLES = [
    "شيخ", "حاج", "حاجي", "حاجه", "حجي", "دكتور", "دكتوره", "د", "سيد", "سيدي",
    "امير", "ملا", "مولوي", "مولانا", "استاذ", "مهندس", "فضيله", "سماحه",
    "اقا", "آقا", "اغا", "آغا", "سردار", "جناب",
]

GENERIC = [
    # legal forms and corporate nouns
    "شركه", "شركات", "مؤسسه", "موسسه", "بنك", "مصرف", "مجموعه", "مكتب",
    "محدوده", "مساهمه", "قابضه", "وشركاه", "وشركاؤه", "واولاده", "واخوانه",
    "ش", "م", "ذ", "ع", "ذمم",
    # activity words
    "تجاره", "تجاريه", "للتجاره", "التجاريه", "استثمار", "للاستثمار",
    "مقاولات", "للمقاولات", "صناعه", "صناعيه", "للصناعه", "خدمات", "للخدمات",
    "نقل", "للنقل", "توريد", "للتوريد", "تصدير", "استيراد", "عامه", "العامه",
    "دوليه", "الدوليه", "عالميه", "وطنيه",
    # regional and positional words (the Arabic "Gulf", "Middle East")
    "الخليج", "خليج", "الشرق", "الاوسط", "عربيه", "العربيه", "المتحده",
    "الاسلاميه", "اسلاميه",
]

# The most frequent elements of Arab, Persian and Afghan names. Deliberately
# short: a longer list would start to hide real evidence.
COMMON = [
    "محمد", "احمد", "علي", "حسن", "حسين", "عبدالله", "عبدالرحمن", "عبدالعزيز",
    "عمر", "خالد", "يوسف", "ابراهيم", "اسماعيل", "محمود", "مصطفي", "سعيد",
    "سالم", "صالح", "عثمان", "ياسين", "جمال", "كمال", "سلطان", "ناصر",
    "فاطمه", "عايشه", "مريم", "زينب", "خديجه", "رضا", "مهدي", "عباس",
    "حميد", "حامد", "جاسم", "راشد", "سليمان", "موسي", "عيسي", "يحيي",
    "ماجد", "طارق", "وليد", "ياسر", "سمير", "سامي", "فيصل", "فهد",
    "عبدالكريم", "عبدالقادر", "عبدالمجيد", "عبدالحميد", "عبدالرزاق",
    "ابوبكر", "ابوعبدالله", "ابومحمد", "الدين", "الله",
]

GIVEN = COMMON + [
    "اسامه", "ايمن", "ايوب", "بدر", "بشار", "بكر", "بلال", "تميم", "ثامر",
    "جابر", "جعفر", "جلال", "جواد", "جميل", "حاتم", "حافظ", "حبيب", "حذيفه",
    "حمزه", "حمد", "حمود", "حيدر", "خليل", "خليفه", "داود", "ذياب", "رائد",
    "رامي", "رباح", "رشيد", "رعد", "رمضان", "رياض", "زايد", "زكريا", "زكي",
    "زهير", "زياد", "زيد", "سامر", "سراج", "سعد", "سعود", "سفيان", "سلمان",
    "سلمي", "سلام", "سليم", "سهيل", "سيف", "شاكر", "شريف", "شعبان", "شهاب",
    "صادق", "صباح", "صبحي", "صدام", "صقر", "ضياء", "طلال", "طه", "ظافر",
    "عادل", "عارف", "عامر", "عبدالباقي", "عبدالجبار", "عبدالحكيم", "عبداللطيف",
    "عبدالمطلب", "عبدالناصر", "عبدالواحد", "عبدالوهاب", "عبيد", "عدنان",
    "عصام", "عطيه", "عقيل", "علاء", "عماد", "عمار", "عمران", "عواد", "عوض",
    "عيد", "غازي", "غالب", "غسان", "فارس", "فاروق", "فتحي", "فخري", "فراس",
    "فرج", "فضل", "فؤاد", "قاسم", "قتيبه", "قيس", "كاظم", "لطفي", "لؤي",
    "مازن", "مبارك", "متعب", "مجاهد", "مجيد", "مختار", "مراد", "مرتضي",
    "مروان", "مسعود", "مشعل", "مصعب", "مطر", "معاذ", "معتز", "مقداد", "منذر",
    "منصور", "منير", "مهند", "مؤيد", "ميثم", "نادر", "ناجي", "نايف", "نبيل",
    "نزار", "نصر", "نصرالله", "نعيم", "نمر", "نواف", "نور", "نوري", "هادي",
    "هاشم", "هاني", "هشام", "هلال", "همام", "وائل", "وسيم", "وضاح", "يزيد",
    "يعقوب", "يونس", "امين", "امينه", "نوره", "سارا", "ليلي", "هدي", "رقيه",
    "عائشه", "سكينه", "صفيه", "حفصه", "اسيا", "اسماء", "ايمان", "رنا",
    "سيدي", "ملا", "الحق", "الاسلام", "الرحمن", "الرحيم", "العزيز", "القادر",
    "قدير", "قادر", "رحمن", "رحيم", "عزيز", "كريم", "حكيم", "لطيف", "وهاب",
    "غفور", "غني", "سلام", "رزاق", "جبار", "مجيد", "حميد", "ودود", "هداي",
    # Persian and Afghan elements
    "علي", "اكبر", "اصغر", "جان", "خان", "زاده", "زاد", "پور", "فر", "نيا",
    "آبادي", "اباد", "شاه", "مير", "ميرزا", "بيگ", "بيك", "اوغلو", "ولي",
    "مولا", "الدين", "الله", "الدين",
]

# Persian name suffixes (-zadeh, -pour, -far, -nia, -nejad, -abadi). Persian
# writes them attached to the stem or as a separate word; the matcher joins them
# to the preceding element so the spacing does not decide the match.
PERSIAN_SUFFIXES = [
    "زاده", "زاد", "زادگان", "پور", "فر", "نيا", "نيا", "نژاد", "اباد", "ابادي",
]
