"""The Terms and Privacy pages must say what the running system does, and the numbers in them must be the code's.

WHY THIS FILE EXISTS (2026-10-04). `render_terms()` and `render_privacy()` were last edited on 29 April 2026 and
described a different product: a Frankfurt host, Render as the application host, Twilio and Vapi as processors,
phone numbers "never stored in plaintext", a 90-day log retention and a 30-day message retention that no code
implements, and an eleven-platform booking list. The privacy policy is the document a directory reviewer, a
regulator and a customer read to learn who receives their data, and Anthropic rejects a listing outright when it
is missing or incomplete. The first correction (2026-10-03) was written against build 1f85885 and never
re-checked after release 1 added OAuth Connect (a second cookie, a sign-in email through Resend, tokens held as
hashes), Arabic-name sanctions screening (an analysis cache keyed on the name you screen), readiness labels on the
tools and the key identifier now recorded in the usage log.

THE METHOD. Three kinds of test, because a page can be wrong in three different ways:

  1. STALE CLAIMS ARE GONE. A list of phrases that were false and must not come back.
  2. THE PAGE NAMES WHAT IS THERE. Every tool, every provider, both cookies, the sign-in email.
  3. THE NUMBERS ARE THE CODE'S. A lifetime or a host written into the page is compared with the constant that
     enforces it, so a change to the constant fails here and forces a deliberate edit of the policy, rather than
     leaving a statement that was true on the day it was written.

A change that fails one of these is not a test to be loosened. It is a change to what the service does with
people's data, and the page has to be updated with it, in the same commit.

Whether any contract clause should change is a decision for the company, not for a page-accuracy fix: Terms
sections 1 and 3 to 13 are pinned word for word to the 29 April 2026 text (see the digest below).
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import os
import re
import sys
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), "rb") as fh:
        return fh.read().decode("utf-8")


def _render(name: str) -> str:
    from web import pages
    return getattr(pages, name)()


def _article(markup: str) -> str:
    m = re.search(r'<article class="legal">(.*?)</article>', markup, re.S)
    assert m, "the page has no <article class=\"legal\"> body"
    return m.group(1)


def _text(markup: str) -> str:
    """Visible text: tags dropped, entities decoded, whitespace collapsed."""
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", markup, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = _html.unescape(body)
    for curly, straight in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"')):
        body = body.replace(curly, straight)
    return re.sub(r"\s+", " ", body).strip()


def _section(article: str, number: int) -> str:
    m = re.search(r"<h2>%d\. .*?</h2>(.*?)(?=<h2>|\Z)" % number, article, re.S)
    assert m, f"section {number} not found"
    return m.group(0)


@pytest.fixture(scope="module")
def privacy_html() -> str:
    return _render("render_privacy")


@pytest.fixture(scope="module")
def terms_html() -> str:
    return _render("render_terms")


@pytest.fixture(scope="module")
def privacy(privacy_html) -> str:
    return _article(privacy_html)


@pytest.fixture(scope="module")
def terms(terms_html) -> str:
    return _article(terms_html)


def _manifest_tool_names() -> set:
    data = json.loads(_read("manifest", "mcp_tools.json"))
    tools = data["tools"] if isinstance(data, dict) else data
    return {t["name"] for t in tools}


# ---------------------------------------------------------------------------
# 1. Stale claims are gone
# ---------------------------------------------------------------------------

STALE_PRIVACY_PHRASES = [
    "Frankfurt",                        # the server is in Kuala Lumpur
    "Standard Contractual Clauses",     # no evidence we hold them
    "Data Privacy Framework",           # same
    "HMAC-SHA256",                      # the compliance audit hash is a plain SHA-256
    "never stored in plaintext",        # opt-outs, leads, conversations are stored readable
    "SHA-256 hash only",                # meta description of the old page
    "EU-hosted",                        # meta description of the old page
    "privacy@hatchloop.dev",            # not forwarded: only hello@ and support@ are
    "Operational logs: 90 days",        # no code deletes logs after 90 days
    "Free-text message bodies: 30 days",  # no code deletes message bodies
    "Compliance hashes: 7 years",       # no purge exists
    "Billing records: 7 years",         # no purge exists
    "Render</strong> (Frankfurt)",      # Render serves only a 308 redirect now
]


@pytest.mark.parametrize("phrase", STALE_PRIVACY_PHRASES)
def test_privacy_page_carries_no_claim_that_stopped_being_true(privacy_html, phrase):
    assert phrase not in _html.unescape(privacy_html), (
        f"the privacy page still says {phrase!r}, which is not true of the running system")


def test_privacy_page_names_twilio_and_vapi_only_as_switched_off(privacy):
    """Neither is configured on this deployment (no Twilio credentials; the Vapi outbound line is unverified),
    so neither receives data. They may be named only in the sentence that says so."""
    marker = "Integrations that exist but are switched off"
    assert marker in privacy, "the page does not say which integrations are switched off"
    head = privacy.split(marker)[0]
    for name in ("Twilio", "Vapi"):
        assert name not in head, f"{name} is listed as if it received data; it is switched off"
    tail = privacy.split(marker, 1)[1]
    for name in ("Twilio", "Vapi", "Coinbase"):
        assert name in tail, f"{name} is not named among the integrations that are switched off"


def test_terms_page_describes_the_current_service_not_the_april_one(terms_html):
    text = _text(_article(terms_html))
    for stale in ("five message types", "eleven further booking", "Calendly", "Doctolib", "BookMyCity",
                  "over WhatsApp, SMS, email and voice"):
        assert stale not in text, f"the terms still describe the April product: {stale!r}"
    assert "29 April 2026" not in text.split("Changed in this version")[0], "the terms are still dated April"


@pytest.mark.parametrize("which", ["terms", "privacy"])
def test_both_pages_carry_a_date_that_is_not_the_stale_one(which, terms, privacy):
    article = {"terms": terms, "privacy": privacy}[which]
    m = re.search(r"Last updated: (\d{1,2}) (\w+) (\d{4})", _text(article))
    assert m, f"the {which} page has no 'Last updated' line"
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
              "October", "November", "December"]
    d = date(int(m.group(3)), months.index(m.group(2)) + 1, int(m.group(1)))
    assert d >= date(2026, 10, 3), f"the {which} page is dated {d}: older than the rewrite"


# ---------------------------------------------------------------------------
# 2. The page names what is there
# ---------------------------------------------------------------------------

def test_terms_lists_exactly_the_tools_the_service_publishes(terms):
    """Terms section 2 says 'At the date above the tools are:' and lists them. That list is the manifest, no more
    and no fewer, so a tool added or removed without touching the Terms fails here."""
    marker = "At the date above the tools are:"
    assert marker in _text(terms), "terms section 2 no longer lists the tools"
    listed = terms.split("the tools are:", 1)[1]
    ul = re.search(r"<ul>(.*?)</ul>", listed, re.S)
    assert ul, "the tool list is not a <ul>"
    named = set(re.findall(r"<code>([a-z_]+)</code>", ul.group(1)))
    published = _manifest_tool_names()
    assert named == published, (
        f"the Terms name {sorted(named - published)} that the service does not publish and omit "
        f"{sorted(published - named)} that it does. Edit render_terms() section 2 (and nothing else) to match.")


def test_terms_say_what_capture_lead_and_escalate_to_human_do_not_do(terms):
    """Their own readiness notes say: capture_lead stops at AgentBroker's lead funnel and does not notify the
    business; escalate_to_human writes a ticket and sends no notification. The Terms must not say more."""
    text = _text(terms)
    assert "lead funnel" in text and "operator queue" in text
    assert re.search(r"does not notify the business|not notif", text), (
        "terms section 2 does not say that capture_lead does not notify the business")
    assert "no response-time commitment" in text or "promises no response time" in text


def test_terms_explain_the_readiness_labels(terms):
    text = _text(terms)
    for word in ("beta", "limited", "unavailable"):
        assert word in text, f"terms section 2 does not explain the readiness label {word!r}"


def test_terms_say_arabic_names_are_screened_and_sound_alikes_are_not_matches(terms):
    text = _text(terms)
    assert "Arabic" in text, "release 1 made screen_sanctions read Arabic names; the Terms do not say so"
    assert re.search(r"sound[s]? (alike|like)", text), "the Terms do not say a sound-alike is not a match"
    assert re.search(r"unverified candidate", text)


def test_privacy_names_every_provider_that_receives_data(privacy):
    text = _text(privacy)
    for provider in ("Hostinger", "Vercel", "Resend", "Polar", "Forward Email", "Google", "Telegram", "Meta",
                     "Cal.com", "Render", "GLEIF", "OpenStreetMap", "USAspending", "Nominatim", "Overpass"):
        assert provider in text, f"the privacy page does not name {provider}"
    assert "Kuala Lumpur" in text and "Malaysia" in text


def test_privacy_says_the_server_is_not_where_it_used_to_say(privacy):
    s7 = _text(_section(privacy, 7))
    assert "Kuala Lumpur" in s7 and "Frankfurt" not in s7


def test_privacy_says_what_connect_sends_to_resend(privacy):
    """Release 1 added an email to the person who types an address on a sign-in page: the address, and a message
    that names the app and carries a one-time link, go to Resend. The sub-processor list must say exactly that."""
    s6 = _text(_section(privacy, 6))
    i = s6.index("Resend")
    resend = s6[i:i + 700]
    assert "Connect" in resend, "the Resend entry does not mention the Connect sign-in email"
    assert "one-time link" in resend
    assert "address you typed" in resend or "address you type" in resend
    assert "names the app" in resend


def test_privacy_names_both_cookies_and_no_longer_says_there_is_only_one(privacy):
    text = _text(privacy)
    assert "The only cookie we set" not in text, "a second cookie exists since release 1"
    assert "hl_portal" in text
    assert "hl_oauth_" in text


def test_privacy_describes_what_the_connect_sign_in_keeps(privacy):
    text = _text(privacy)
    assert "Connect" in text
    assert "do not store it" in text, "the page must say the address itself is not stored by Connect"
    assert "masked hint" in text
    assert "SHA-256" in text
    assert "client-metadata document" in text, "our server fetches a document from the app's host"


def test_privacy_describes_every_field_the_usage_log_writes(privacy):
    s2 = _text(_section(privacy, 2))
    phrases = {
        "p_tool": "tool name",
        "p_method": "type of request",
        "p_args_hash": "hash of the arguments",
        "p_ip_hash": "hash of your IP address",
        "p_user_agent": "user agent",
        "p_key_id": "its identifier",
        "p_key_state": "its state",
        "p_session_kind": "label we derive",
        "p_outcome": "the outcome",
        "p_error_code": "the outcome",
        "p_http_status": "status",
        "p_latency_ms": "how long it took",
        "p_client_name": "client name and version",
        "p_client_version": "client name and version",
        "p_arg_names": "names of the arguments",
        "p_requested_name": "the name asked for",
        "p_detail": "protocol version",
    }
    for field, phrase in phrases.items():
        assert phrase in s2, f"usage log field {field} is not described: section 2 lacks {phrase!r}"


def test_privacy_says_where_lookup_text_stays_in_memory(privacy):
    s3 = _text(_section(privacy, 3))
    assert "in memory" in s3 or "in the server's memory" in s3
    assert "restart" in s3


# ---------------------------------------------------------------------------
# 3. The numbers are the code's
# ---------------------------------------------------------------------------

def test_cookie_names_and_lifetimes_are_the_codes(privacy):
    from agent_interface import portal
    from agent_interface.oauth import router as oauth_router, settings as oauth_settings
    text = _text(privacy)
    assert portal._SESSION_TTL_S == 30 * 24 * 3600
    assert "hl_portal" in text and "30 days" in text
    assert oauth_router.COOKIE == "hl_oauth"
    assert f"{oauth_router.COOKIE}_" in text
    assert oauth_settings.SIGNIN_TTL_S == 15 * 60
    section2 = _text(_section(privacy, 2))
    assert "15 minutes" in section2, "the sign-in cookie lasts 15 minutes and the page must say so"


def test_token_and_link_lifetimes_are_the_codes(privacy):
    from agent_interface.oauth import settings as s
    text = _text(_section(privacy, 2))
    assert s.ACCESS_TTL_S == 3600 and "one hour" in text
    assert s.REFRESH_TTL_S == 30 * 86400 and "30 days" in text
    assert s.REFRESH_FAMILY_TTL_S == 90 * 86400 and "90 days" in text
    assert s.SIGNIN_TTL_S == 900 and "valid for 15 minutes" in text


def test_openstreetmap_hosts_and_cache_lifetimes_are_the_codes(privacy):
    from supply import osm_client
    from urllib.parse import urlsplit
    s3 = _text(_section(privacy, 3))
    for url in (osm_client.DEFAULT_NOMINATIM_URL, osm_client.DEFAULT_OVERPASS_URL):
        host = urlsplit(url).netloc
        assert host in s3, f"the page does not name {host}, which find_business sends the place text to"
    assert osm_client.GEOCODE_TTL_S == 7 * 24 * 3600 and "7 days" in s3
    assert osm_client.OVERPASS_TTL_S == 6 * 3600 and "6 hours" in s3


def test_public_registry_hosts_are_the_codes(privacy):
    from urllib.parse import urlsplit
    from core import lookup_us_contracts, verify_company_record
    s3 = _text(_section(privacy, 3))
    assert urlsplit(verify_company_record._GLEIF_BASE).netloc in s3
    assert urlsplit(lookup_us_contracts._USASPENDING_SEARCH_URL).netloc in s3


def test_connect_tables_hold_no_email_address():
    """The page says the address is used to send one message and is not stored: a hash and a masked hint only.
    The tables are the proof; a column named email would make the sentence false."""
    sql = _read("migrations", "spine", "011_oauth_connect.sql")
    tables = re.findall(r"create table if not exists public\.(oauth_\w+) \((.*?)\n\);", sql, re.S)
    assert len(tables) == 5, f"expected the five Connect tables, found {[t for t, _ in tables]}"
    for name, body in tables:
        cols = re.findall(r"^\s{4}([a-z_]+)\s", body, re.M)
        assert cols, f"could not read the columns of {name}"
        offenders = [c for c in cols if c in ("email", "email_address", "address", "recipient", "mail")]
        assert not offenders, f"{name} stores an email address in {offenders}; the privacy page says it does not"
        for c in cols:
            assert not (c.startswith("email") and c not in ("email_hash", "email_hint", "email_sent_count")), (
                f"{name}.{c}: a new email-shaped column needs a line in the privacy policy")


def test_usage_log_writes_only_the_fields_the_privacy_page_describes():
    """billing/usage_logger.py sends these parameters to the spine. A new one is a new thing kept about callers."""
    src = _read("billing", "usage_logger.py")
    written = set(re.findall(r'"(p_[a-z_]+)":', src))
    described = {"p_tool", "p_method", "p_args_hash", "p_ip_hash", "p_user_agent", "p_key_id", "p_key_state",
                 "p_session_kind", "p_outcome", "p_error_code", "p_http_status", "p_latency_ms", "p_client_name",
                 "p_client_version", "p_arg_names", "p_requested_name", "p_detail"}
    assert written == described, (
        f"usage log fields changed: new {sorted(written - described)}, gone {sorted(described - written)}. "
        f"Update PRIVACY section 2 (render_privacy) and the table in this test together.")


def test_no_ai_model_client_is_installed_in_the_image():
    """Privacy section 3 says the service sends no tool input, message or personal data to an AI model provider.
    compliance/jev_advisory.py would pipe an outbound message to a `jev` command-line tool if one existed on the
    host, which sends it to an AI model API. It is inert because the image does not contain it. If the image ever
    gains it (or an SDK for any model provider), that sentence is false and the policy must change first."""
    packaged = (_read("Dockerfile") + "\n" + _read("requirements.txt")).lower()
    for token in ("jev", "openai", "anthropic", "deepseek", "openrouter", "typesafe", "generativeai"):
        assert not re.search(r"\b%s\b" % re.escape(token), packaged), (
            f"{token!r} is in the Dockerfile or requirements; privacy section 3 says no AI provider receives data")


def test_the_integrations_the_page_calls_switched_off_are_off_in_this_deployments_configuration(monkeypatch):
    """The names (not values) of the variables set on the live container, from the 2026-10-03 deploy receipt:
    Resend, WhatsApp, Vapi key and number, Cal.com, Polar, Telegram, and an x402 receiver address. No Twilio, no
    VAPI_OUTBOUND_VERIFIED, no X402_ENABLED. Under exactly that configuration SMS and voice are unavailable and
    email and WhatsApp are available, which is what privacy section 5 and Terms section 2 say."""
    from core import channel_status as cs
    for name in list(os.environ):
        if name.startswith(("TWILIO_", "SENDGRID_", "VAPI_", "WHATSAPP_", "RESEND_", "ALLOW_STUB", "X402_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    for name in ("RESEND_API_KEY", "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_ID", "VAPI_API_KEY",
                 "VAPI_PHONE_NUMBER_ID"):
        monkeypatch.setenv(name, "placeholder-not-a-secret")
    assert cs.channel_state(cs.SMS).available is False
    assert cs.channel_state(cs.VOICE).available is False
    assert cs.channel_state(cs.EMAIL).available is True
    assert cs.channel_state(cs.WHATSAPP).available is True
    # x402 (Coinbase) is off unless X402_ENABLED is set; the default in code is off.
    assert re.search(r'^X402_ENABLED = _env_bool\("X402_ENABLED", default=False\)', _read("config.py"), re.M), (
        "the code default for X402_ENABLED changed; the privacy page says on-chain payments are switched off")


# ---------------------------------------------------------------------------
# 4. The contract clauses are not touched
# ---------------------------------------------------------------------------

# sha256 of the visible text of Terms sections 1 and 3 to 13 as they stood on 29 April 2026 (build 1f85885, the
# last version before this file), each section as "N. Heading" plus its text, one line per section, in order.
# These are contract clauses. Changing one is a legal decision, not an accuracy fix: do it on purpose, and
# replace the digest in the same commit.
CLAUSES_1_AND_3_TO_13_SHA256 = "ab0d78204f26e78b3ef2586696277840c0e8c5f2696082dc4b903bfab4a5adc8"


def _clauses_text(terms_article: str) -> str:
    lines = []
    for n in [1] + list(range(3, 14)):
        m = re.search(r"<h2>%d\. (.*?)</h2>(.*?)(?=<h2>|\Z)" % n, terms_article, re.S)
        assert m, f"terms section {n} not found"
        lines.append(f"{n}. {_text(m.group(1))} {_text(m.group(2))}")
    return "\n".join(lines)


def test_terms_clauses_1_and_3_to_13_are_word_for_word_the_april_text(terms):
    digest = hashlib.sha256(_clauses_text(terms).encode("utf-8")).hexdigest()
    assert digest == CLAUSES_1_AND_3_TO_13_SHA256, (
        "a Terms clause other than section 2 changed. Those are contract clauses: change them only as a "
        "deliberate decision, then replace CLAUSES_1_AND_3_TO_13_SHA256.")


def test_terms_section_2_keeps_the_gate_paragraph_and_the_as_is_sentence(terms):
    s2 = _text(_section(terms, 2))
    assert "non-bypassable compliance gate" in s2 and "26 jurisdictions" in s2
    assert "as-is" in s2 and "no implied warranties" in s2
