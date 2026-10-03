"""Which country does a recipient belong to - the question every consent rule depends on.

WHY THIS EXISTS (Door Reliability Run defect D2, 2026-10-03). `check_compliance` for the Omani number
+96891234567 answered with a US statute (TCPA) as the legal basis. One cause was that the jurisdiction was
whatever the caller typed: the tool's schema promises "auto-inferred from phone if omitted", and nothing did
that except the quiet-hours check, which kept its own 27-entry table. A number that says where it is from was
being judged under rules chosen by a field the caller may leave out, or fill in wrongly.

WHAT A NUMBER CAN AND CANNOT SAY. An E.164 number begins with a country calling code (ITU-T E.164), and the
codes are prefix-free, so the first one, two or three digits name the numbering plan unambiguously. That is
the same basis carriers and messaging providers use to route and to apply destination-country rules. It is
NOT proof of where the person is standing (a roaming or ported number is still that country's number), and
the evidence receipt says so. Two codes are shared by several countries and name no single one: +1 (the North
American Numbering Plan: the US, Canada and a score of Caribbean states) and +7 (Russia and Kazakhstan). For
those the caller must say which, and a country outside the shared set is a contradiction, not a guess.

ONLY A REAL E.164 STRING IS READ. A leading "+" or "00", then 7 to 15 digits with spacing characters allowed.
The quiet-hours version of this lookup took every digit out of any string, so an email address such as
user44@example.com read as a British number; this one does not.

THE RULES BELOW ARE DELIBERATE:
  * When the number names exactly one country and the caller's country_code names another, the NUMBER wins and
    the contradiction is reported in plain words. A caller cannot move a number's rules by typing a different
    country, and every rule set this gate implements is opt-in or stricter for marketing, so the number's own
    country is the safe side to resolve towards.
  * When the number names only a shared region and the caller's country_code is outside it, the jurisdiction is
    UNRESOLVED (country None), never guessed. Marketing is then refused as `jurisdiction_required`.
  * Nothing here talks to the network, a database or the clock. Pure functions.

Territories that share a calling code with their sovereign state (Guernsey, Jersey and the Isle of Man with
+44; the Aland Islands with +358; Svalbard with +47; Western Sahara with +212; the Cocos and Christmas Islands
with +61; Mayotte with +262; Saint Barthelemy and Saint Martin with +590; Bonaire with +599) are reported as
the code's principal country. No rule set in this service distinguishes them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Calling codes that name exactly one country (its principal one; see the module docstring).
CALLING_CODES: dict = {
    # Africa and the 2xx block
    "20": "EG", "211": "SS", "212": "MA", "213": "DZ", "216": "TN", "218": "LY", "220": "GM", "221": "SN",
    "222": "MR", "223": "ML", "224": "GN", "225": "CI", "226": "BF", "227": "NE", "228": "TG", "229": "BJ",
    "230": "MU", "231": "LR", "232": "SL", "233": "GH", "234": "NG", "235": "TD", "236": "CF", "237": "CM",
    "238": "CV", "239": "ST", "240": "GQ", "241": "GA", "242": "CG", "243": "CD", "244": "AO", "245": "GW",
    "246": "IO", "247": "AC", "248": "SC", "249": "SD", "250": "RW", "251": "ET", "252": "SO", "253": "DJ",
    "254": "KE", "255": "TZ", "256": "UG", "257": "BI", "258": "MZ", "260": "ZM", "261": "MG", "262": "RE",
    "263": "ZW", "264": "NA", "265": "MW", "266": "LS", "267": "BW", "268": "SZ", "269": "KM", "27": "ZA",
    "290": "SH", "291": "ER", "297": "AW", "298": "FO", "299": "GL",
    # Europe, 3xx and 4xx
    "30": "GR", "31": "NL", "32": "BE", "33": "FR", "34": "ES", "350": "GI", "351": "PT", "352": "LU",
    "353": "IE", "354": "IS", "355": "AL", "356": "MT", "357": "CY", "358": "FI", "359": "BG", "36": "HU",
    "370": "LT", "371": "LV", "372": "EE", "373": "MD", "374": "AM", "375": "BY", "376": "AD", "377": "MC",
    "378": "SM", "379": "VA", "380": "UA", "381": "RS", "382": "ME", "383": "XK", "385": "HR", "386": "SI",
    "387": "BA", "389": "MK", "39": "IT",
    "40": "RO", "41": "CH", "420": "CZ", "421": "SK", "423": "LI", "43": "AT", "44": "GB", "45": "DK",
    "46": "SE", "47": "NO", "48": "PL", "49": "DE",
    # The Americas, 5xx
    "500": "FK", "501": "BZ", "502": "GT", "503": "SV", "504": "HN", "505": "NI", "506": "CR", "507": "PA",
    "508": "PM", "509": "HT", "51": "PE", "52": "MX", "53": "CU", "54": "AR", "55": "BR", "56": "CL",
    "57": "CO", "58": "VE", "590": "GP", "591": "BO", "592": "GY", "593": "EC", "594": "GF", "595": "PY",
    "596": "MQ", "597": "SR", "598": "UY", "599": "CW",
    # South-east Asia and Oceania, 6xx
    "60": "MY", "61": "AU", "62": "ID", "63": "PH", "64": "NZ", "65": "SG", "66": "TH", "670": "TL",
    "672": "NF", "673": "BN", "674": "NR", "675": "PG", "676": "TO", "677": "SB", "678": "VU", "679": "FJ",
    "680": "PW", "681": "WF", "682": "CK", "683": "NU", "685": "WS", "686": "KI", "687": "NC", "688": "TV",
    "689": "PF", "690": "TK", "691": "FM", "692": "MH",
    # East Asia, 8xx
    "81": "JP", "82": "KR", "84": "VN", "850": "KP", "852": "HK", "853": "MO", "855": "KH", "856": "LA",
    "86": "CN", "880": "BD", "886": "TW",
    # West, South and Central Asia, 9xx
    "90": "TR", "91": "IN", "92": "PK", "93": "AF", "94": "LK", "95": "MM", "960": "MV", "961": "LB",
    "962": "JO", "963": "SY", "964": "IQ", "965": "KW", "966": "SA", "967": "YE", "968": "OM", "970": "PS",
    "971": "AE", "972": "IL", "973": "BH", "974": "QA", "975": "BT", "976": "MN", "977": "NP", "98": "IR",
    "992": "TJ", "993": "TM", "994": "AZ", "995": "GE", "996": "KG", "998": "UZ",
}

# The two codes shared by several countries, and who shares them.
NANP_COUNTRIES = frozenset({
    "US", "CA", "AG", "AI", "AS", "BB", "BM", "BS", "DM", "DO", "GD", "GU", "JM", "KN", "KY", "LC", "MP",
    "MS", "PR", "SX", "TC", "TT", "VC", "VG", "VI",
})
SHARED_CALLING_CODES: dict = {
    "1": NANP_COUNTRIES,
    "7": frozenset({"RU", "KZ"}),
}
_SHARED_REGION_NAME = {
    "1": "the North American Numbering Plan (the US, Canada and Caribbean states)",
    "7": "Russia or Kazakhstan",
}

_E164 = re.compile(r"^(?:\+|00)([0-9][0-9 ().\-]{5,29})$")
# A country_code is repeated in OUR sentences, labels and audit rows only if it looks like a code. Anything else
# (a phrase, a sentence of instructions) is ignored and said to be ignored, never echoed.
_SAFE_COUNTRY = re.compile(r"^[A-Z][A-Z0-9_-]{1,15}$")
_SAFE_STATE = re.compile(r"^[A-Z0-9]{1,3}$")


@dataclass(frozen=True)
class Resolution:
    """The country a send is judged under, and how that was decided."""
    country: Optional[str]           # ISO 3166-1 alpha-2, upper case; None when nothing settles it
    source: str                      # "caller" | "recipient_number" | "unknown"
    number_country: Optional[str]    # what the number alone names (None for email, shared codes, odd input)
    supplied: Optional[str]          # the caller's country_code, trimmed and upper-cased
    conflict: Optional[str]          # a sentence when the caller's country_code contradicts the number
    calling_code: Optional[str] = None


def _digits(recipient_id) -> Optional[str]:
    """The digits of an E.164 number (country calling code first), or None if it is not one."""
    if not isinstance(recipient_id, str):
        return None
    m = _E164.match(recipient_id.strip())
    if not m:
        return None
    digits = re.sub(r"[^0-9]", "", m.group(1))
    return digits if 7 <= len(digits) <= 15 else None


def _calling_code(digits: str) -> Optional[str]:
    """The country calling code at the front of `digits`. The codes are prefix-free, so the first match wins."""
    for length in (1, 2, 3):
        head = digits[:length]
        if head in SHARED_CALLING_CODES or head in CALLING_CODES:
            return head
    return None


def country_of_number(recipient_id) -> Optional[str]:
    """The single country an E.164 number belongs to, or None (not a number, an unassigned code, or a
    shared code such as +1 or +7 that does not name one country). Never raises."""
    digits = _digits(recipient_id)
    if digits is None:
        return None
    return CALLING_CODES.get(_calling_code(digits) or "")


def _normalize(country_code) -> tuple:
    """(code, ignored): the caller's country_code trimmed and upper-cased; `ignored` is True when something was
    supplied that is not shaped like a country code, in which case the code is None."""
    if not isinstance(country_code, str):
        return None, country_code is not None
    code = country_code.strip().upper()
    if not code:
        return None, False
    if not _SAFE_COUNTRY.match(code):
        return None, True
    return code, False


def resolve_jurisdiction(recipient_id, country_code) -> Resolution:
    """Decide the country a send is judged under. See the module docstring for the rules."""
    supplied, ignored = _normalize(country_code)
    digits = _digits(recipient_id)
    code = _calling_code(digits) if digits else None
    note = ("country_code was not a recognizable country code and was ignored." if ignored else None)

    if code in CALLING_CODES:
        number_country = CALLING_CODES[code]
        if supplied is None:
            return Resolution(number_country, "recipient_number", number_country, None, note, code)
        if supplied == number_country:
            return Resolution(number_country, "caller", number_country, supplied, None, code)
        conflict = (f"country_code '{supplied}' contradicts the recipient number: +{code} is the "
                    f"country calling code of {number_country}, so {number_country} was used.")
        return Resolution(number_country, "recipient_number", number_country, supplied, conflict, code)

    if code in SHARED_CALLING_CODES:
        shared = SHARED_CALLING_CODES[code]
        if supplied is None:
            return Resolution(None, "unknown", None, None, note, code)
        if supplied in shared:
            return Resolution(supplied, "caller", None, supplied, None, code)
        conflict = (f"country_code '{supplied}' contradicts the recipient number: its country calling "
                    f"code +{code} belongs to {_SHARED_REGION_NAME[code]}, so the jurisdiction cannot be "
                    f"determined. Pass the recipient's real country_code.")
        return Resolution(None, "unknown", None, supplied, conflict, code)

    if supplied:
        return Resolution(supplied, "caller", None, supplied, None, None)
    return Resolution(None, "unknown", None, None, note, None)


def jurisdiction_label(country: Optional[str], state: Optional[str] = None) -> str:
    """The label used in answers and the audit log: "OM", "US-CA", or "unknown" - never a guess of "US"."""
    if not country:
        return "unknown"
    state = state.strip().upper() if isinstance(state, str) and state.strip() else None
    if state is not None and not _SAFE_STATE.match(state):
        state = None
    return f"{country}-{state}" if state else country
