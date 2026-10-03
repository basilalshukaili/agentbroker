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


# Common Western given names and surnames. ONE job: a Latin-script query is searched by sound as an Arabic name
# when it contains a known Arab given name, and several of those sound like ordinary English ones (David /
# Dawud, Mary / Maryam, Ryan / Rayyan, Samuel, Daniel, Emily ...). A word on this list never counts as evidence
# that a name is Arabic - only structure (an article, a patronymic, abd/abu, -din, -allah) or a given name that
# is NOT on it does. It is a veto on a weak signal and nothing else: a query this list keeps out of the Arabic
# layer is still screened exactly as it always was (identical spelling), and a name that is on it AND carries
# structure ("David bin Salim") is still read as Arabic. Names that are common in the Arab world as well (Adam,
# Sara, Karim, Sami, Omar, Amin, Ali, Hassan ...) are deliberately NOT here. Measured on 125 ordinary English
# names: 22 were engaged without this list and 2 with it (docs/ARABIC_SANCTIONS_EVAL.md).
WESTERN = """
james john robert michael william david richard joseph thomas charles christopher daniel matthew anthony mark
donald steven paul andrew joshua kenneth kevin brian george timothy ronald edward jason jeffrey ryan jacob gary
nicholas eric jonathan stephen larry justin scott brandon benjamin samuel gregory alexander patrick frank
raymond jack dennis jerry tyler aaron jose henry zachary douglas peter kyle noah ethan jeremy walter christian
keith roger terry austin sean gerald carl harold dylan arthur lawrence jordan jesse bryan billy bruce gabriel joe
logan alan juan albert willie elijah wayne randy vincent mason roy ralph bobby russell bradley philip eugene
howard fred stanley leonard nathan norman todd curtis glen rodney lee jimmy johnny tommy ricky danny jamie
mary patricia jennifer linda elizabeth barbara susan jessica sarah karen lisa nancy betty margaret sandra
ashley kimberly emily donna michelle carol amanda melissa deborah stephanie rebecca sharon laura cynthia
kathleen amy angela shirley anna brenda pamela emma nicole helen samantha katherine christine debra rachel
carolyn janet catherine maria heather diane olivia julie joyce victoria ruth virginia lauren kelly christina
joan evelyn judith andrea hannah megan cheryl jacqueline martha madison teresa gloria janice ann kathryn
abigail sophia frances jean alice judy julia grace amber denise danielle marilyn beverly isabella theresa
diana natalie brittany charlotte marie kayla alexis lori aladdin alonso alistair eleanor alison allison
smith johnson williams brown jones garcia miller davis rodriguez martinez hernandez lopez gonzalez wilson
anderson taylor moore jackson martin perez thompson white harris sanchez clark ramirez lewis robinson walker
young king wright torres nguyen hill flores green adams nelson baker hall rivera campbell mitchell carter
roberts gomez phillips evans turner diaz parker cruz edwards collins reyes stewart morris morales murphy cook
rogers gutierrez ortiz morgan cooper peterson bailey reed howard ramos cox ward richardson watson brooks
chavez wood bennett gray mendoza ruiz hughes price alvarez castillo sanders myers long ross foster jimenez
powell jenkins perry sullivan bell coleman butler henderson barnes gonzales fisher vasquez simmons romero
patterson hamilton graham reynolds griffin wallace moreno west cole hayes bryant herrera gibson ellis tran
medina aguilar stevens murray ford castro marshall owens harrison fernandez mcdonald woods washington kennedy
wells vargas freeman webb tucker guzman burns crawford olson simpson porter hunter gordon mendez silva shaw
snyder dixon munoz hunt hicks holmes palmer wagner black robertson boyd rose stone salazar fox warren mills
meyer rice schmidt garza daniels ferguson nichols stephens soto weaver gardner payne grant dunn kelley
spencer hawkins arnold pierce vazquez hansen peters santos hart knight elliott cunningham duncan armstrong
hudson carroll lane riley andrews alvarado ray delgado berry perkins hoffman johnston matthews pena richards
contreras willis carpenter sandoval guerrero chapman rios estrada ortega watkins greene nunez wheeler valdez
harper burke larson santiago maldonado morrison franklin carlson dominguez carr lawson jacobs obrien lynch
vega bishop montgomery oliver jensen harvey williamson gilbert dean sims espinoza howell reid hanson mccoy
garrett burton fuller weber welch rojas lucas marquez fields park little banks padilla day walsh bowman
schultz luna fowler mejia davidson acosta brewer holland juarez newman pearson curtis cortez schneider
barrett navarro figueroa keller avila wade molina hopkins campos barnett bates chambers caldwell beck lambert
miranda byrd craig ayala lowe frazier powers neal carrillo sutton fleming rhodes shelton schwartz norris
jennings watts duran walters cohen mcdaniel moran parks steele vaughn becker holt deleon barker hale
benson haynes horton miles lyons graves bush thornton wolfe warner cabrera mckinney mann zimmerman dawson
lara fletcher page mccarthy love robles cervantes solis erickson reeves klein salinas fuentes baldwin hardy
higgins aguirre cummings chandler sharp barber bowen ochoa robbins ramsey francis griffith blair oconnor
cardenas pacheco cross calderon quinn moss swanson rivas hodges mcclain mcbride hayden
"""
