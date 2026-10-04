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

  1. STALE CLAIMS ARE GONE. A list of phrases that were false and must not come back, checked on EVERY page this
     module renders, not only on the policy (a "PII is hashed only" card on the home page contradicted the policy).
  2. THE PAGE NAMES WHAT IS THERE. Every tool, every provider, both cookies, the sign-in email, every store that
     keeps a readable identifier. Where a store is a list of columns in code (the supply directory, the queue of
     over-budget messages), the columns are READ FROM THE CODE and compared with a pinned list, so a new column
     fails here and forces a decision about the policy.
  3. THE NUMBERS ARE THE CODE'S. A lifetime or a host written into the page is compared with the constant that
     enforces it, each bound to ITS OWN sentence. (The first version looked for "30 days" anywhere in the section,
     and a mutation from "refresh token 30 days" to "45 days" passed because "30 days" also appears elsewhere.)

A SECOND REVIEW (2026-10-04) found that the first version of this file pinned names and numbers but not the
behaviour behind them: raw addresses written into the usage log or the Connect tables would not have failed any
test, and a page could drop a provider that a different paragraph also named. The tests in section 5 capture what
the code actually WRITES (the usage-log payload, the Connect store, the log lines) and compare it with the page.

A change that fails one of these is not a test to be loosened. It is a change to what the service does with
people's data, and the page has to be updated with it, in the same commit.

Whether any contract clause should change is a decision for the company, not for a page-accuracy fix. Terms
sections 1, 3 to 5 and 7 to 13 are pinned word for word to the 29 April 2026 text (see the digests below). Section
6 has one sentence that described a mechanism the sending tools do not have (a `consent_record_id` on every
marketing send); it was corrected on 2026-10-04 and is pinned separately so any further edit is deliberate.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import html as _html
import json
import logging
import os
import re
import sys
from datetime import date
from types import SimpleNamespace

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
    """Visible text: tags dropped, entities decoded, whitespace collapsed. (The contract digests are computed on
    this exact function, so it must not change.)"""
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", markup, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = _html.unescape(body)
    for curly, straight in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"')):
        body = body.replace(curly, straight)
    return re.sub(r"\s+", " ", body).strip()


_INLINE_TAG = re.compile(r"</?(?:code|strong|em|a|span|b|i)\b[^>]*>", re.I)


def _prose(markup: str) -> str:
    """Like `_text`, but inline tags vanish instead of becoming a space, so `<code>hl_portal</code>, 30 days`
    reads "hl_portal, 30 days" and a sentence can be matched as it is written."""
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", markup, flags=re.S | re.I)
    body = _INLINE_TAG.sub("", body)
    body = re.sub(r"<[^>]+>", " ", body)
    body = _html.unescape(body)
    for curly, straight in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"')):
        body = body.replace(curly, straight)
    return re.sub(r"\s+", " ", body).strip()


def _sentence(text: str, needle: str) -> str:
    """The sentence of `text` (split on '. ') that contains `needle`."""
    i = text.index(needle)
    start = text.rfind(". ", 0, i)
    start = 0 if start < 0 else start + 2
    end = text.find(". ", i)
    return text[start:] if end < 0 else text[start:end + 1]


def _number_in(pattern: str, text: str) -> int:
    m = re.search(pattern, text)
    assert m, f"the page no longer says {pattern!r}"
    return int(m.group(1))


def _section(article: str, number: int) -> str:
    m = re.search(r"<h2>%d\. .*?</h2>(.*?)(?=<h2>|\Z)" % number, article, re.S)
    assert m, f"section {number} not found"
    return m.group(0)


def _all_rendered_pages() -> dict:
    """Every `render_*` function in web/pages.py, called with None for any required argument."""
    import inspect
    from web import pages
    out = {}
    for name, fn in inspect.getmembers(pages, inspect.isfunction):
        if name.startswith("render_") and fn.__module__ == pages.__name__:
            required = [p for p in inspect.signature(fn).parameters.values() if p.default is p.empty]
            out[name] = fn(*[None] * len(required))
    assert len(out) >= 7, f"expected the public pages, found {sorted(out)}"
    return out


def _dict_literal_keys(path_parts, function_name: str, variable: str) -> set:
    """The string keys of the dict literal assigned to `variable` inside `function_name` (any nesting): what the
    code WRITES, read from its syntax tree rather than from a comment or a docstring."""
    tree = ast.parse(_read(*path_parts))
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == function_name:
            for node in ast.walk(fn):
                if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                        and any(isinstance(t, ast.Name) and t.id == variable for t in node.targets)):
                    return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError(f"no `{variable} = {{...}}` in {function_name} of {'/'.join(path_parts)}")


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


@pytest.fixture(scope="module")
def all_pages() -> dict:
    return _all_rendered_pages()


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


# Claims that are false of the running system wherever they are written. The first review found the hashed-only
# claim on the HOME and CHECKOUT pages, where the stale-phrase test above (privacy page only) never looked.
STALE_ON_ANY_PAGE = [
    "SHA-256 hash only",                # leads, opt-outs, conversations, WhatsApp replies and the supply directory hold
    "hash only",                        # a phone number or email address in readable form
    "never stored in plaintext",
    "never in plaintext",
    "EU-hosted",
    "Frankfurt",
    "consent_record_id",                # the sending tools take no such argument (core/models.py SendMessageRequest)
    "only path to outbound",            # the sign-in, key and billing emails and the WhatsApp clarifying question
    "not Vercel, Render, Supabase or Cloudflare",   # Cloudflare relays the older workers.dev address
]


@pytest.mark.parametrize("phrase", STALE_ON_ANY_PAGE)
def test_no_public_page_carries_a_claim_that_is_false_of_the_running_system(all_pages, phrase):
    offenders = sorted(name for name, markup in all_pages.items() if phrase in _html.unescape(markup))
    assert not offenders, f"{offenders} still say {phrase!r}, which is not true of the running system"


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


def test_the_switched_off_integrations_are_not_said_to_receive_nothing_at_all(privacy):
    """/healthz/external is public and calls the status endpoint of the configured providers with OUR credentials
    (Vapi's /assistant on this deployment). No message or personal data is sent, but "receive nothing" was
    literally untrue, so the sentence says what they do not receive and names the health check."""
    prose = _prose(_section(privacy, 6))
    sentence = _sentence(prose, "Integrations that exist but are switched off")
    assert "receive no messages or personal data" in sentence
    assert "receive nothing" not in prose
    assert "/healthz/external" in prose and "our own credentials" in prose
    assert '"/healthz/external"' in _read("main.py")
    assert "api.vapi.ai" in _read("agent_interface", "health_external.py")


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


def test_the_privacy_change_note_says_what_it_removed_and_does_not_deny_cloudflare(privacy):
    """A promise removed without a word is a silent change of terms. The old page promised 30 days' notice of a
    material change and listed data it said it never collects; both are gone and the note says so. The note also
    must not claim the service avoids Cloudflare: a Cloudflare Worker relays the older workers.dev address."""
    note = _prose(re.search(r'<p class="updated">Changed in this version:(.*?)</p>', privacy, re.S).group(1))
    assert "30-day advance notice" in note, "the note does not say the 30-day notice promise was removed"
    assert "never collect" in note, "the note does not say the list of data we never collect was removed"
    assert not re.search(r"\bnot\b[^.]*Cloudflare", note), "the note denies using Cloudflare"
    assert "Cloudflare" in note and "workers.dev" in note


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
    business; escalate_to_human writes a ticket and sends no notification. The Terms must not say more, and each
    statement is bound to ITS tool's sentence (a mutation to "pages the on-call team" once passed because the
    words "operator queue" were elsewhere in the section)."""
    prose = _prose(terms)
    lead = _sentence(prose, "capture_lead records")
    assert "lead funnel" in lead and "does not notify the business" in lead
    ticket = _sentence(prose, "escalate_to_human writes")
    assert "operator queue" in ticket and "sends no notification" in ticket
    assert "makes no response-time commitment" in ticket
    assert "pages" not in ticket and "on-call" not in ticket


def test_terms_and_privacy_say_sms_and_voice_are_not_enabled(terms, privacy):
    for label, article in (("terms", terms), ("privacy section 5", _section(privacy, 5))):
        sentence = _sentence(_prose(article), "SMS and voice")
        assert "not enabled" in sentence, f"the {label} no longer says SMS and voice calling are not enabled"
        assert not re.search(r"\bare enabled\b", sentence)


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
    """Each provider is named in ITS OWN list inside section 6 (the old check looked at the whole page, so
    deleting the Vercel, Render or Polar entry passed: the name also occurs in the change note or section 2)."""
    s6 = _section(privacy, 6)
    always, _, particular = s6.partition("Providers involved only in particular cases")
    assert particular, "section 6 no longer separates the providers used in particular cases"
    always_names = set(re.findall(r"<strong>([^<]+)</strong>", always))
    particular_names = set(re.findall(r"<strong>([^<]+)</strong>", particular))
    for provider in ("Hostinger", "Vercel", "Resend", "Polar", "Forward Email", "Google (Gmail)", "Telegram"):
        assert provider in always_names, f"section 6 does not list {provider} among the providers that run the service"
    for provider in ("Meta", "Cal.com", "Cloudflare", "Render"):
        assert provider in particular_names, f"section 6 does not list {provider} among the providers used in particular cases"
    s3 = _text(_section(privacy, 3))
    for source in ("GLEIF", "OpenStreetMap", "USAspending", "Nominatim", "Overpass"):
        assert source in s3, f"section 3 does not name {source}"
    text = _text(privacy)
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
    s2 = _prose(_section(privacy, 2))
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
        "p_error_code": "error code",
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
    # For a web request that failed before a tool ran, detail is "<METHOD> <sanitised path>" (main.py
    # _log_http_outcome). The page names the method and a sanitised path so the field is not under-described.
    assert re.search(r"method and a sanitised (version of the )?path", s2), (
        "the usage log's detail field also holds the method and a sanitised path for a failed web request")
    assert 'safe_path(request.url.path)' in _read("main.py")


def test_privacy_says_where_lookup_text_stays_in_memory(privacy):
    s3 = _text(_section(privacy, 3))
    assert "in memory" in s3 or "in the server's memory" in s3
    assert "restart" in s3


# ---------------------------------------------------------------------------
# 3. The numbers are the code's
# ---------------------------------------------------------------------------

def test_cookie_names_and_lifetimes_are_the_codes(privacy):
    """Each lifetime is read from the sentence about THAT cookie and compared with the constant."""
    from agent_interface import portal
    from agent_interface.oauth import router as oauth_router, settings as oauth_settings
    s2 = _prose(_section(privacy, 2))
    assert oauth_router.COOKIE == "hl_oauth"
    assert "hl_oauth_ followed by" in s2, "the page does not name the sign-in cookie as hl_oauth_<identifier>"
    assert _number_in(r"hl_portal, (\d+) days", s2) * 86400 == portal._SESSION_TTL_S
    assert _number_in(r"one cookie, hl_oauth_ followed by.*? It lasts (\d+) minutes", s2) * 60 == oauth_settings.SIGNIN_TTL_S
    cookie_path = re.search(r"sent only to the sign-in pages \((/\w+)\)", s2)
    assert cookie_path and cookie_path.group(1) == "/oauth", "the sign-in cookie's path is no longer /oauth on the page"
    assert 'path="/oauth"' in _read("agent_interface", "oauth", "router.py")


def test_token_and_link_lifetimes_are_the_codes(privacy):
    """Each number is bound to its own sentence: 'a refresh token lasts N days', 'ends at the latest N days after
    you signed in', 'valid for N minutes'."""
    from agent_interface.oauth import settings as s
    text = _prose(_section(privacy, 2))
    assert s.ACCESS_TTL_S == 3600 and "access token lasts one hour" in text
    assert _number_in(r"refresh token lasts (\d+) days", text) * 86400 == s.REFRESH_TTL_S
    assert _number_in(r"ends at the latest (\d+) days after you signed in", text) * 86400 == s.REFRESH_FAMILY_TTL_S
    assert _number_in(r"one-time link, valid for (\d+) minutes", text) * 60 == s.SIGNIN_TTL_S


def test_openstreetmap_hosts_and_cache_lifetimes_are_the_codes(privacy):
    from supply import osm_client
    from urllib.parse import urlsplit
    s3 = _prose(_section(privacy, 3))
    for url in (osm_client.DEFAULT_NOMINATIM_URL, osm_client.DEFAULT_OVERPASS_URL):
        host = urlsplit(url).netloc
        assert host in s3, f"the page does not name {host}, which find_business sends the place text to"
    assert _number_in(r"remembers a place for up to (\d+) days", s3) * 86400 == osm_client.GEOCODE_TTL_S
    assert _number_in(r"businesses found there for (\d+) hours", s3) * 3600 == osm_client.OVERPASS_TTL_S


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


def test_the_jev_route_is_closed_in_this_deployment(monkeypatch, tmp_path):
    """The page's sentence is true only while NO route to a `jev` binary exists on the host. The routes are the
    ones compliance/jev_advisory.resolve_jev_binary walks: the JEV_BIN variable, a `jev` on PATH, and one
    hard-coded script path under a Windows user profile. This pins all three: the Dockerfile sets none of them,
    the fallback path cannot exist in a Linux image, and with none configured nothing resolves. (The previous
    test pinned the Dockerfile's spelling only, not the route.)"""
    from compliance import jev_advisory as ja
    dockerfile = _read("Dockerfile")
    assert ja.JEV_BIN_ENV_VAR == "JEV_BIN"
    assert "JEV_BIN" not in dockerfile and "jev" not in dockerfile.lower()
    assert re.match(r"^[A-Za-z]:[\\/]Users[\\/]", ja._DEV_FALLBACK_SCRIPT), (
        "the hard-coded jev script path is no longer a Windows user-profile path; it could now exist in the image")
    monkeypatch.delenv("JEV_BIN", raising=False)
    monkeypatch.setattr(ja.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(ja, "_DEV_FALLBACK_SCRIPT", str(tmp_path / "does-not-exist"))
    argv, why = ja.resolve_jev_binary()
    assert argv is None and "binary not found" in why
    # and the route is REAL: put a `jev` where PATH can find it and it resolves, so the page's sentence
    # depends on this absence and a later change to the resolver has to come through here.
    fake = tmp_path / "jev"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(ja.shutil, "which", lambda name, *a, **k: str(fake) if name == "jev" else None)
    assert ja.resolve_jev_binary()[0] == [str(fake)]


def test_the_integrations_the_page_calls_switched_off_are_off_in_this_deployments_configuration(monkeypatch):
    """The names (not values) of the variables set on the live container, from the 2026-10-03 deploy receipt:
    Resend, WhatsApp, Vapi key and number, Cal.com, Polar, Telegram, and an x402 receiver address. No Twilio, no
    VAPI_OUTBOUND_VERIFIED, no X402_ENABLED. Under exactly that configuration SMS and voice are unavailable and
    email and WhatsApp are available, which is what privacy section 5 and Terms section 2 say."""
    from core import channel_status as cs
    _live_channel_configuration(monkeypatch)
    assert cs.channel_state(cs.SMS).available is False
    assert cs.channel_state(cs.VOICE).available is False
    assert cs.channel_state(cs.EMAIL).available is True
    assert cs.channel_state(cs.WHATSAPP).available is True
    # x402 (Coinbase) is off unless X402_ENABLED is set; the default in code is off.
    assert re.search(r'^X402_ENABLED = _env_bool\("X402_ENABLED", default=False\)', _read("config.py"), re.M), (
        "the code default for X402_ENABLED changed; the privacy page says on-chain payments are switched off")


def _live_channel_configuration(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("TWILIO_", "SENDGRID_", "VAPI_", "WHATSAPP_", "RESEND_", "ALLOW_STUB", "X402_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    for name in ("RESEND_API_KEY", "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_ID", "VAPI_API_KEY",
                 "VAPI_PHONE_NUMBER_ID"):
        monkeypatch.setenv(name, "placeholder-not-a-secret")


# ---------------------------------------------------------------------------
# 4. The contract clauses are not touched
# ---------------------------------------------------------------------------

# sha256 of the visible text of Terms sections 1, 3, 4, 5 and 7 to 13 as they stood on 29 April 2026 (build
# 1f85885, the last version before this file), each section as "N. Heading" plus its text, one line per section,
# in order. These are contract clauses. Changing one is a legal decision, not an accuracy fix: do it on purpose,
# and replace the digest in the same commit. (Until 2026-10-04 the digest also covered section 6, whose first
# bullet is now pinned separately below; the digest over 1 and 3 to 13 was ab0d7820...adc8.)
UNCHANGED_CLAUSES_SHA256 = "420a6cc5c2d5d5bf3a653d1c44aca8aeba44e322068e9ecf7079bcab3f52cbad"
UNCHANGED_CLAUSE_NUMBERS = (1, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13)

# Section 6 (Prohibited uses) as corrected on 2026-10-04: its first bullet no longer says a marketing message
# "must reference a valid consent_record_id", a field the sending tools do not have, nor that the gate "rejects
# any send tagged marketing that does not" (a US marketing email is lawful without opt-in under CAN-SPAM and the
# gate allows it). The PROHIBITION is unchanged: marketing without recorded opt-in consent is prohibited.
# A FOUNDER DECISION to ratify before this branch is merged; replace this digest in the same commit as any edit.
CLAUSE_6_SHA256 = "89aa4c18a5e83bd1f12b8a7aae5e191ba00194565f414a98fc90f27489a13ccd"


def _clauses_text(terms_article: str, numbers) -> str:
    lines = []
    for n in numbers:
        m = re.search(r"<h2>%d\. (.*?)</h2>(.*?)(?=<h2>|\Z)" % n, terms_article, re.S)
        assert m, f"terms section {n} not found"
        lines.append(f"{n}. {_text(m.group(1))} {_text(m.group(2))}")
    return "\n".join(lines)


def test_terms_clauses_other_than_2_and_6_are_word_for_word_the_april_text(terms):
    digest = hashlib.sha256(_clauses_text(terms, UNCHANGED_CLAUSE_NUMBERS).encode("utf-8")).hexdigest()
    assert digest == UNCHANGED_CLAUSES_SHA256, (
        "a Terms clause other than sections 2 and 6 changed. Those are contract clauses: change them only as a "
        "deliberate decision, then replace UNCHANGED_CLAUSES_SHA256.")


def test_terms_clause_6_is_the_corrected_text_and_keeps_every_prohibition(terms):
    digest = hashlib.sha256(_clauses_text(terms, (6,)).encode("utf-8")).hexdigest()
    assert digest == CLAUSE_6_SHA256, (
        "Terms section 6 changed. It is a contract clause: change it only as a deliberate decision, then "
        "replace CLAUSE_6_SHA256.")
    s6 = _prose(_section(terms, 6))
    for prohibition in ("Marketing without recorded opt-in consent", "Bulk, list-based, A/B test, or drip",
                        "Cold outreach", "Sales prospecting", "Bulk communications", "Harassing, threatening",
                        "Impersonating", "Reverse-engineering", "Circumventing", "Any use that violates"):
        assert prohibition in s6, f"Terms section 6 no longer carries the prohibition {prohibition!r}"
    assert "consent_record_id" not in s6


def test_terms_section_2_keeps_the_gate_paragraph_and_the_as_is_sentence(terms):
    s2 = _text(_section(terms, 2))
    assert "non-bypassable compliance gate" in s2 and "26 jurisdictions" in s2
    assert "as-is" in s2 and "no implied warranties" in s2


# ---------------------------------------------------------------------------
# 5. The second review (2026-10-04): what the code WRITES, and what the gate DOES
# ---------------------------------------------------------------------------

# --- the supply directory: import_booking_url writes a persistent, readable business record ----------------------

SMB_SUPPLY_COLUMNS = {
    "smb_id", "name", "vertical", "address", "city", "state", "zip_code", "country", "capabilities",
    "channels_available", "calcom_event_type_id", "square_location_id", "vapi_assistant_id", "phone", "email",
    "website", "price_min_usd", "price_max_usd", "is_demo", "active", "booking_url", "source", "verified_at",
}

# the phrase section 5 must carry for each column that can hold a name, a place, a phone number or an address
SMB_SUPPLY_DISCLOSED = {
    "name": "business name", "address": "address", "city": "address", "state": "address", "zip_code": "address",
    "country": "country", "phone": "phone number", "email": "email address", "website": "booking-page address",
    "booking_url": "booking-page address", "capabilities": "capabilities", "channels_available": "channels",
}


def test_the_supply_directory_columns_are_the_ones_the_page_discloses():
    written = _dict_literal_keys(("supply", "smb_directory.py"), "_persist_to_supabase", "row")
    assert written == SMB_SUPPLY_COLUMNS, (
        f"smb_supply columns changed: new {sorted(written - SMB_SUPPLY_COLUMNS)}, gone "
        f"{sorted(SMB_SUPPLY_COLUMNS - written)}. Update privacy section 5 and this table together.")


def test_privacy_discloses_that_import_booking_url_saves_a_readable_business_record(privacy):
    """import_booking_url writes the business's name, location, contact details and booking address to the
    smb_supply table, in readable form, and keeps them in memory so other callers' find_business and
    verify_business can read them. Policy sections 2, 3 and 5 said it only 'fetches the page' and that the values
    passed to lookup tools were 'not saved in the database'."""
    s5 = _prose(_section(privacy, 5))
    bullet = _sentence(s5, "import_booking_url saves")
    for column, phrase in SMB_SUPPLY_DISCLOSED.items():
        assert phrase in s5, f"section 5 does not describe the {column!r} that import_booking_url saves ({phrase!r})"
    assert "supply directory" in s5 and "readable form" in s5
    assert "smb_supply" in s5
    assert "verify_business" in s5 and "find_business" in s5, "the page does not say who else can read the record"
    assert "email" in bullet and "phone" in bullet
    # who actually reads the stored contact details, from the code: schedule_appointment reads the booking-page
    # address (website) and call_business reads the phone; nothing in the messaging tools reads either, which is
    # why the page names exactly those two and not "the messaging tools".
    readers = _sentence(s5, "can be read back by any other")
    assert "schedule_appointment" in readers and "call_business" in readers and "booking-page address" in readers
    assert 'getattr(smb, "website", None)' in _read("core", "schedule_appointment.py")
    assert "entry.phone" in _read("core", "call_business.py")
    for rel in (("core", "send_message.py"), ("core", "send_transactional_confirmation.py")):
        assert not re.search(r"smb\.(phone|email|website)|entry\.(phone|email|website)", _read(*rel)), (
            f"{'/'.join(rel)} now reads a stored directory contact; privacy section 5 says which tools do")
    s3 = _prose(_section(privacy, 3))
    assert "import_booking_url" in s3 and "saves a business record" in _sentence(s3, "import_booking_url")
    s2 = _prose(_section(privacy, 2))
    assert "importing a booking page" in s2, "section 2 does not list importing a booking page among the tools that store a record"
    exemption = _sentence(s2, "are not saved in the database")
    assert "lookup" in exemption and "for those tools" in exemption, (
        "the 'not saved in the database' sentence must be limited to the lookup tools")


def test_the_only_live_writer_of_the_supply_directory_is_import_booking_url():
    """Three onboarding modules also call SMBDirectory.upsert but nothing in the application imports them. If one
    is wired in, the page's account of who writes the directory (and the 'deletion' route) is incomplete."""
    for rel in ("main.py", "agent_interface", "api", "channels", "core", "billing", "supply"):
        path = os.path.join(ROOT, rel)
        files = [path] if path.endswith(".py") else [
            os.path.join(dp, f) for dp, _, fs in os.walk(path) for f in fs if f.endswith(".py")]
        for f in files:
            src = open(f, encoding="utf-8").read()
            assert not re.search(r"from onboarding|import onboarding", src), (
                f"{os.path.relpath(f, ROOT)} imports onboarding/, which writes smb_supply; "
                "the privacy page names only import_booking_url")


# --- the queue of over-budget messages ---------------------------------------------------------------------------

PENDING_REQUEST_COLUMNS = {
    "request_id", "idem_key", "business_id", "business_number", "agent_id", "end_user_ref", "intent", "ref_token",
    "state", "created_at", "expires_at",
}


def test_the_queue_of_over_budget_messages_is_disclosed_with_its_real_contents(privacy):
    from core import demand_queue
    written = _dict_literal_keys(("core", "demand_queue.py"), "enqueue", "row")
    assert written == PENDING_REQUEST_COLUMNS, (
        f"pending_requests columns changed: new {sorted(written - PENDING_REQUEST_COLUMNS)}, gone "
        f"{sorted(PENDING_REQUEST_COLUMNS - written)}. Update privacy section 5 and this table together.")
    assert demand_queue._TABLE == "pending_requests"
    cut = re.search(r"intent=request\.content\.body\[:(\d+)\]", _read("core", "send_message.py"))
    assert cut, "send_message no longer queues the first N characters of the body as the request's intent"
    s5 = _prose(_section(privacy, 5))
    queued = _sentence(s5, "queues the request")
    assert int(_number_in(r"first (\d+) characters of the message", s5)) == int(cut.group(1))
    assert _number_in(r"for (\d+) hours it is marked expired", s5) == demand_queue._QUEUE_TTL_HOURS
    assert "not deleted" in s5
    for phrase in ("over its message budget", "recipient's phone number", "your agent's identifier",
                   "reference for the end user", "WhatsApp digest"):
        assert phrase in s5, f"section 5 does not say {phrase!r} about queued messages"
    assert "business_id" in queued


# --- messaging channels: derived from the gate, not asserted ------------------------------------------------------

def test_each_messaging_tools_reachable_channels_match_the_page(monkeypatch, terms, privacy):
    """send_transactional_confirmation picks the email adapter when the recipient has an '@' and the SMS adapter
    otherwise; it never uses WhatsApp. Under the live configuration SMS is off, so a confirmation is email-only,
    while send_message still reaches phone numbers by WhatsApp. The page says exactly that; the proof is the
    gate's own answer for each case."""
    from core import channel_status as cs
    from core.models import ChannelPreference
    _live_channel_configuration(monkeypatch)
    phone, mail = "+14155550100", "owner@example.test"
    assert cs.gate("send_transactional_confirmation", recipient=mail) is None
    refused = cs.gate("send_transactional_confirmation", recipient=phone)
    assert refused is not None and cs.SMS in refused.unavailable
    assert cs.gate("send_message", recipient=phone, preferred_channel=ChannelPreference.WHATSAPP) is None
    assert cs.gate("send_message", recipient=mail, preferred_channel=ChannelPreference.EMAIL) is None
    assert cs.gate("send_message", recipient=phone, preferred_channel=ChannelPreference.SMS) is not None
    assert "whatsapp" not in _read("core", "send_transactional_confirmation.py").lower().replace("whatsapp_", ""), (
        "send_transactional_confirmation now mentions WhatsApp; the page says it is email-only")

    t = _prose(terms)
    assert "send_message sends messages on the channels enabled on the current deployment, which are WhatsApp and email" in t
    confirmation = _sentence(t, "send_transactional_confirmation sends")
    assert "by email only" in confirmation and "phone-number recipient needs SMS" in confirmation
    assert "send_message and send_transactional_confirmation send" not in t, (
        "the page again lumps the two tools together on WhatsApp and email")
    s5 = _prose(_section(privacy, 5))
    assert "send_message works on WhatsApp and email" in s5
    assert "send_transactional_confirmation works by email only" in s5


# --- the compliance gate: what it does, and where it does not apply -----------------------------------------------

def test_the_gate_does_what_the_page_says_about_marketing_and_consent():
    """The page used to say a marketing send needs a `consent_record_id` and is rejected without one. SendMessageRequest
    has no such field, and the gate keys on the RECIPIENT's recorded consent per jurisdiction: a US marketing email
    is lawful without opt-in (CAN-SPAM) and is allowed; Germany (GDPR) and Oman (PDPL) require opt-in and reject;
    and a marketing send with no country_code is refused, because the regime cannot be chosen."""
    from compliance.pre_check import pre_check
    from core.models import ComplianceViolationError, SendMessageRequest

    assert "consent_record_id" not in SendMessageRequest.model_fields
    common = dict(message_type="marketing", content="Spring offer on haircuts. Reply STOP to opt out.",
                  agent_id="legal-pages-test", trace_id="legal-pages-test", preview=True)
    recipient = "owner.nobody@example.test"

    pre_check(recipient_id=recipient, channel="email", country_code="US", **common)     # allowed: opt-out regime
    for country, rule in (("DE", "GDPR_marketing_consent"), ("OM", "email_marketing_consent")):
        with pytest.raises(ComplianceViolationError) as caught:
            pre_check(recipient_id=recipient, channel="email", country_code=country, **common)
        assert caught.value.rule == rule
    with pytest.raises(ComplianceViolationError) as caught:
        pre_check(recipient_id=recipient, channel="email", country_code=None, **common)
    assert caught.value.rule == "jurisdiction_required"


def test_terms_describe_the_gate_as_it_is_and_name_what_it_does_not_cover(terms):
    prose = _prose(terms)
    gate = _sentence(prose, "routes through a non-bypassable compliance gate")
    assert "Every message sent through the messaging tools" in gate, (
        "the paragraph again says EVERY outbound communication passes the gate; the sign-in, key and billing "
        "emails and the WhatsApp clarifying question do not")
    assert "Every outbound communication" not in prose
    assert "26 jurisdictions" in gate
    paragraph = prose[prose.index("Every message sent through the messaging tools"):]
    paragraph = paragraph[:paragraph.index("The Service is offered")]
    for phrase in ("sign-in", "key and billing emails", "WhatsApp", "clarifying question", "checks the opt-out list first",
                   "country_code", "opt-in", "opt-out", "unsubscribe", "compliance_violation", "never reaches a carrier"):
        assert phrase in paragraph, f"the gate paragraph does not say {phrase!r}"
    assert "consent_record_id" not in paragraph and "verified opt-in" not in paragraph


def test_the_things_the_page_says_bypass_the_gate_really_do():
    """The sentence names exceptions. This pins the code side: the WhatsApp clarifying question goes straight to
    the Meta Graph API after an opt-out check and never calls pre_check; the sign-in, key and billing emails go
    straight to Resend. If any of them is routed through the gate, the exception on the page is false."""
    for rel in (("agent_interface", "whatsapp_webhook.py"), ("agent_interface", "oauth", "emailer.py"),
                ("billing", "emails.py"), ("agent_interface", "key_request_logic.py")):
        assert "pre_check" not in _read(*rel), f"{'/'.join(rel)} now calls the compliance gate; update the Terms' exceptions"
    assert "is_opted_out" in _read("agent_interface", "whatsapp_webhook.py")


def test_the_live_sending_guidance_does_not_tell_agents_to_pass_a_consent_record_id():
    """The cookbook resource and the prompt arguments are served to every agent that connects. They told agents a
    marketing send 'requires a valid consent_record_id', an argument send_message does not take."""
    from agent_interface import mcp_server
    served = json.dumps([
        asyncio.run(mcp_server._h_resources_read({"uri": "agent-broker://cookbook"})),
        asyncio.run(mcp_server._h_prompts_list({})),
    ])
    assert "consent_record_id" not in served
    cookbook = asyncio.run(mcp_server._h_resources_read({"uri": "agent-broker://cookbook"}))["contents"][0]["text"]
    assert "country_code" in cookbook and "opt-in" in cookbook


# --- Cloudflare: a relay for the older workers.dev address -------------------------------------------------------

def test_privacy_names_the_cloudflare_relay(privacy):
    """A Cloudflare Worker (agent-broker-edge.basil-agent.workers.dev) is live, public, and relays tools/call to the
    origin; the Smithery listing and some directories still point at it. A caller through that address sends tool
    arguments and key headers to Cloudflare. hatchloop.dev and api.hatchloop.dev are served directly (Caddy)."""
    toml = _read("edge", "wrangler.toml")
    assert re.search(r'^name = "agent-broker-edge"', toml, re.M)
    assert "rl:${ip}" in _read("edge", "src", "rate-limit.ts"), "the relay no longer counts calls per IP address"
    s6 = _prose(_section(privacy, 6))
    entry = s6[s6.index("Cloudflare"):]
    entry = entry[:entry.index("Render runs")] if "Render runs" in entry else entry[:900]
    for phrase in ("agent-broker-edge.basil-agent.workers.dev", "tool arguments", "key header", "IP address",
                   "hatchloop.dev and api.hatchloop.dev do not go through Cloudflare"):
        assert phrase in entry, f"the Cloudflare entry does not say {phrase!r}"
    s7 = _prose(_section(privacy, 7))
    assert "Cloudflare" in s7, "section 7 does not say where the Cloudflare relay processes requests"


# --- the program log: no request lines, no plaintext addresses ---------------------------------------------------

def test_the_container_does_not_write_an_access_log_with_query_strings():
    """uvicorn's access log is on by default and records the full query string: a free-key verification token
    (/keys/verify?token=...) and a Connect sign-in link (/oauth/verify?t=...) were readable in `docker logs`.
    The page says the web server's log removes session tokens and key parameters; the program log must not
    reintroduce them."""
    cmd = re.search(r"^CMD (.*)$", _read("Dockerfile"), re.M).group(1)
    assert "uvicorn main:app" in cmd and "--no-access-log" in cmd


def test_privacy_describes_the_program_log_and_its_real_retention(privacy):
    s8 = _prose(_section(privacy, 8))
    log = _sentence(s8, "program log")
    for phrase in ("program log", "container", "request", "masked", "kept stopped", "rollback", "by hand",
                   "no fixed schedule", "sign-in links", "session tokens"):
        assert phrase in s8, f"section 8 does not say {phrase!r} about the service's own log"
    assert "For as long as the container exists" in s8 or "as long as the container exists" in s8
    assert "recovery" in s8 and "paid order" in s8, "section 8 does not name the one line that keeps a buyer's address"
    assert "masked" in log or "mask" in s8


class _FakeResponse:
    def __init__(self, status, text):
        self.status_code, self.text = status, text


def _fake_httpx_client(status: int, body: str):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _FakeResponse(status, body)
    return _Client


ADDRESS = "zeta.person.4821@example.org"


def _no_plain_address(caplog):
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert text, "nothing was logged, so this test proves nothing"
    assert ADDRESS not in text, f"a log line carries the plaintext address: {text!r}"
    assert "zeta.person" not in text


def test_the_free_key_emails_log_a_masked_address_on_every_failure_path(monkeypatch, caplog):
    import httpx
    from agent_interface import key_request_logic as krl
    caplog.set_level(logging.DEBUG)

    monkeypatch.delenv("RESEND_API_KEY", raising=False)                       # skipped: no key configured
    assert asyncio.run(krl.send_verification_email(ADDRESS, "https://example.test/v")) is False
    asyncio.run(krl.send_key_email(ADDRESS, "tok", "2027-01-01"))

    monkeypatch.setenv("RESEND_API_KEY", "placeholder-not-a-secret")
    body = '{"message":"Invalid `to` field: %s"}' % ADDRESS                   # Resend echoes the address back
    monkeypatch.setattr(httpx, "AsyncClient", _fake_httpx_client(422, body))
    assert asyncio.run(krl.send_verification_email(ADDRESS, "https://example.test/v")) is False
    asyncio.run(krl.send_key_email(ADDRESS, "tok", "2027-01-01"))

    class _Boom(_fake_httpx_client(200, "")):
        async def post(self, *a, **k):
            raise RuntimeError(f"connection reset while sending to {ADDRESS}")
    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    assert asyncio.run(krl.send_verification_email(ADDRESS, "https://example.test/v")) is False
    asyncio.run(krl.send_key_email(ADDRESS, "tok", "2027-01-01"))
    _no_plain_address(caplog)


def test_storing_a_pending_key_logs_a_masked_address_when_it_fails(monkeypatch, caplog):
    from agent_interface import key_request_logic as krl
    import storage.supabase_client as sc
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(sc, "_get_config", lambda: ("https://db.example.test", "placeholder-not-a-secret"))

    async def refuses(name, payload):
        raise RuntimeError(f"HTTP 409: duplicate key value violates unique constraint (email)=({ADDRESS})")
    monkeypatch.setattr(sc, "rpc", refuses)
    assert asyncio.run(krl.store_pending(ADDRESS, "tok", 4102444800.0)) is False

    async def odd(name, payload):
        return {"stored": "maybe"}
    monkeypatch.setattr(sc, "rpc", odd)
    assert asyncio.run(krl.store_pending(ADDRESS, "tok", 4102444800.0)) is False
    _no_plain_address(caplog)


def test_billing_emails_log_a_masked_address(monkeypatch, caplog):
    import httpx
    from billing import emails
    caplog.set_level(logging.DEBUG)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    assert asyncio.run(emails._send(ADDRESS, "Welcome", "<p>x</p>")) is False
    monkeypatch.setenv("RESEND_API_KEY", "placeholder-not-a-secret")
    monkeypatch.setattr(httpx, "AsyncClient", _fake_httpx_client(422, '{"message":"bad to: %s"}' % ADDRESS))
    assert asyncio.run(emails._send(ADDRESS, "Welcome", "<p>x</p>")) is False
    monkeypatch.setattr(httpx, "AsyncClient", _fake_httpx_client(200, ""))
    assert asyncio.run(emails._send(ADDRESS, "Welcome", "<p>x</p>")) is True
    _no_plain_address(caplog)


# One log line deliberately keeps a buyer's address: when a paid order cannot be recorded, the line is the only
# record of it ("THIS ORDER IS NOW ONLY IN THIS LOG LINE") and a masked address could not be used to replay it.
# Privacy section 8 names it. Any other file or line that passes an email-named variable to a logger fails.
PLAINTEXT_EMAIL_LOG_ALLOWED = {("billing", "polar_webhook.py")}
EMAIL_NAMES = {"email", "customer_email", "user_email", "to_email", "recipient_email"}


def test_no_log_call_passes_a_bare_email_variable():
    levels = {"debug", "info", "warning", "error", "exception", "critical"}
    loggers = {"logger", "log", "_log", "LOG", "_logger"}
    offenders = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in {".git", "tests", "edge", "node_modules", "__pycache__", "docs"}]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            rel = tuple(os.path.relpath(path, ROOT).replace("\\", "/").split("/"))
            if rel in PLAINTEXT_EMAIL_LOG_ALLOWED:
                continue
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in levels and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in loggers):
                    args = list(node.args[1:]) + [k.value for k in node.keywords]
                    for a in args:
                        if isinstance(a, ast.Name) and a.id in EMAIL_NAMES:
                            offenders.append(f"{'/'.join(rel)}:{node.lineno} logs `{a.id}`")
    assert not offenders, "a log line carries a plaintext email address (mask it with compliance.log_redactor): " + "; ".join(offenders)


# --- what the writers actually store ----------------------------------------------------------------------------

def test_the_usage_log_payload_holds_no_argument_values_and_no_raw_ip(monkeypatch):
    """The page says: argument NAMES, never values; a hash of the IP address; and a detail field limited to the
    door and protocol version. The earlier tests pinned the parameter NAMES, so `p_detail: str(event.arguments)`
    or a raw IP passed. This runs the real writer and inspects the payload it sends."""
    from billing import usage_logger as ul
    import storage.supabase_client as sc
    sent = []

    async def capture(name, payload):
        sent.append((name, dict(payload)))
        return {"ok": True}
    monkeypatch.setattr(sc, "rpc", capture)
    monkeypatch.setattr(ul, "_v2_missing_until", 0.0)
    secret = "ZETA-SECRET-VALUE-91"
    event = ul.UsageEvent(
        method="tools/call", tool_name="screen_sanctions", arguments={"name": secret, "country": "OM"},
        ip="203.0.113.77", user_agent="legal-pages-test/1", key_id="free_abcdef0123456789", outcome="ok",
        http_status=200, latency_ms=12, client_name="client", client_version="1", key_state="valid",
        arg_names=["name", "country"], requested_name="screen_sanctions", detail="door=agent-broker pv=2026-07-28")
    asyncio.run(ul.log_usage_outcome(event))
    assert sent and sent[0][0] == "usage_events_insert_v2"
    payload = sent[0][1]
    blob = json.dumps(payload, default=str)
    assert secret not in blob, "an argument VALUE reached the usage log"
    assert "203.0.113.77" not in blob, "the raw IP address reached the usage log"
    assert payload["p_ip_hash"] and payload["p_ip_hash"] != "203.0.113.77"
    assert payload["p_args_hash"] and secret not in str(payload["p_args_hash"])
    assert payload["p_arg_names"] == ["name", "country"]
    assert payload["p_detail"] == "door=agent-broker pv=2026-07-28"


def test_the_mcp_door_builds_the_detail_field_from_the_door_and_protocol_version_only():
    from agent_interface import mcp_server
    detail = mcp_server._event_detail("agent-broker", SimpleNamespace(era_version="2026-07-28"))
    assert detail == "door=agent-broker pv=2026-07-28"
    assert mcp_server._event_detail(None, SimpleNamespace(era_version=None)) is None


def test_connect_stores_a_hash_and_a_masked_hint_and_never_the_address(monkeypatch):
    """Runs a real sign-in start through the app against the in-memory store and looks at everything the store
    holds. (The migration test proves the SQL has no email column; this proves the code does not put the address
    somewhere else, such as the hint column.)"""
    from fastapi.testclient import TestClient
    import main
    from agent_interface.oauth import clients as oclients, limits, tokens
    from agent_interface.oauth.store import MemoryStore, set_store
    from tests.oauth_support import HTTPS_BASE, install_mailbox, pkce, poll_secret_of, rid_of

    store = MemoryStore()
    set_store(store)
    limits.LIMITS.reset()
    oclients.FETCHER.clear()
    try:
        box = install_mailbox(monkeypatch)
        client = TestClient(main.app, base_url=HTTPS_BASE, follow_redirects=False)
        redirect = "https://assistant.example.org/callback"
        reg = client.post("/oauth/register", json={
            "redirect_uris": [redirect], "client_name": "Legal Pages Test", "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        assert reg.status_code == 201, reg.text
        _, challenge = pkce()
        page = client.get("/oauth/authorize", params={
            "response_type": "code", "client_id": reg.json()["client_id"], "redirect_uri": redirect,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "s",
            "scope": "agentbroker.tools offline_access"})
        assert page.status_code == 200, page.text
        rid = rid_of(page.text)
        sent = client.post("/oauth/authorize/email", data={
            "rid": rid, "poll_secret": poll_secret_of(page.text), "email": ADDRESS})
        assert sent.status_code == 200 and box.sent, sent.text
        row = store.requests[rid]
        assert row["email_hint"] == tokens.mask_email(ADDRESS) and ADDRESS not in row["email_hint"]
        assert row["email_hash"] == tokens.email_hash(ADDRESS)
        everything = repr(vars(store))
        assert ADDRESS not in everything and "zeta.person" not in everything, (
            "the Connect store holds the plaintext address somewhere")
    finally:
        set_store(None)
        limits.LIMITS.reset()


# --- the links people are sent to ---------------------------------------------------------------------------------

def test_the_policy_links_in_this_repository_point_at_the_page_this_repository_serves(terms_html):
    """hatchloop.dev/privacy is a separate site (web_hatchloop_v2) whose copy lacked the Connect cookie, the Resend
    sign-in email, the key identifier in the usage log, the in-memory note, the readiness labels and Arabic
    screening. Until that copy carries the same text, the Terms page and the sign-in page footer link to the
    page this service serves, which is the corrected one."""
    from agent_interface.oauth import pages as opages, settings as osettings
    from web._partials import API_ORIGIN
    links = re.findall(r'href="([^"]*/privacy[^"]*)"', _article(terms_html))
    assert links, "the Terms page no longer links to the privacy policy"
    for link in links:
        assert link.startswith(API_ORIGIN + "/privacy"), f"the Terms link to the policy goes elsewhere: {link}"
    footer = opages._shell("t", "<p>x</p>", "nonce")
    for target in ("/privacy", "/terms"):
        hrefs = re.findall(r'href="([^"]*%s)"' % re.escape(target), footer)
        assert hrefs, f"the sign-in page footer has no link to {target}"
        for href in hrefs:
            assert href == osettings.issuer() + target, f"the sign-in page sends people to {href}, not this service's own page"


# --- retention exceptions and small statements ------------------------------------------------------------------

def test_privacy_section_8_states_the_registry_ceiling_exception(privacy):
    """oauth_client_register deletes up to 5,000 registrations that were never used and are older than 30 days
    when the registry reaches 50,000. The retention sentence for self-registered apps has to say so."""
    sql = _read("migrations", "spine", "011_oauth_connect.sql")
    assert re.search(r"v_count >= 50000", sql) and "limit 5000" in sql and "interval '30 days'" in sql
    s8 = _prose(_section(privacy, 8))
    sentence = _sentence(s8, "registered themselves")
    assert "50,000" in sentence and "never used" in sentence and "30 days" in sentence and "to make room" in sentence
