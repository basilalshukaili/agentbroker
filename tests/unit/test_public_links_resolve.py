"""
Every link on a public page must resolve on the host that serves that page.

WHAT WENT WRONG. The footer links "Status" -> /health and "Manifest" ->
/manifest. Both are served by the API origin only. The legal pages are ALSO
served under hatchloop.dev, where the edge does not proxy those two routes - so
on the pages a payment processor and a cautious buyer actually read, Status and
Manifest were 404s, and had been for as long as the footer existed. Nothing
noticed, because nothing had ever fetched a link on a rendered page.

The head also carried `<link rel="manifest" href="/manifest">`. That slot means
a PWA web-app manifest; /manifest is our API contract. Every page load fetched
it and failed to parse it - or 404'd, at the edge.

THIS FILE IS OFFLINE ON PURPOSE. It renders the pages in-process and checks the
shape of every href. A network link-checker is a good thing to have and a bad
thing to gate a build on: it fails when an unrelated host is slow, so it gets
marked flaky and then ignored. The rule enforced here needs no network -
"origin-only paths are written absolute" is a property of the HTML itself.
`scripts/check_public_links.py` does the live crawl separately, on both hosts.

THE OTHER HALF is the token. Most bodies in pages.py are PLAIN triple-quoted
strings, so `{link('/health')}` written into one renders as that literal text.
The first version of this fix did exactly that and shipped a visibly broken
href into the home page; it was caught by rendering the page, not by reading
the diff. So: the substitution happens in `page()`, and the last test here
fails if a rendered page still contains an unresolved token or a stray
`{link(` - the two ways this can silently regress.
"""
from __future__ import annotations

import inspect
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from web import pages  # noqa: E402
from web._partials import API_ORIGIN, ORIGIN_ONLY, ORIGIN_TOKEN, link  # noqa: E402


def _rendered() -> dict[str, str]:
    """Every page function that can be called with no arguments."""
    out = {}
    for name, fn in inspect.getmembers(pages, inspect.isfunction):
        if name.startswith("_"):
            continue
        sig = inspect.signature(fn)
        if any(p.default is inspect.Parameter.empty
               and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
               for p in sig.parameters.values()):
            continue
        try:
            html = fn()
        except Exception:  # noqa: BLE001 - a page that needs live state, not our concern
            continue
        if isinstance(html, str) and html.strip():
            out[name] = html
    return out


PAGES = _rendered()

# HTML comments are not markup the browser acts on, and this file must not
# flag the comment that DOCUMENTS a removal as the thing being removed - which
# it did on the first run: the note explaining why there is no rel="manifest"
# tripped the rel="manifest" assertion. Strip comments, then assert.
LIVE = {name: re.sub(r"<!--.*?-->", "", html, flags=re.S)
        for name, html in PAGES.items()}


def test_there_are_pages_to_check():
    """Guard the guard.

    If `_rendered()` silently returns nothing - a renamed module, a signature
    change, an import error swallowed by the except - every other test in this
    file passes vacuously while checking zero pages. That is the exact failure
    shape this repo keeps rediscovering, so it gets its own assertion.
    """
    # The threshold was >= 4 against exactly 5 collectible pages, so ONE page
    # could start raising inside the swallowed except and vanish silently while
    # this stayed green - and the likeliest casualty is render_home, which
    # carries most of the __ORIGIN__ tokens. Name the pages that must be there.
    must_have = {"render_home", "render_pricing", "render_terms",
                 "render_privacy", "render_refund"}
    missing = must_have - set(PAGES)
    assert not missing, (
        f"these pages did not render and were silently skipped: {sorted(missing)}. "
        f"Every assertion in this file passes vacuously for a page that is not "
        f"in PAGES.")


@pytest.mark.parametrize("path", ORIGIN_ONLY)
def test_origin_only_paths_are_never_linked_relatively(path):
    """A relative href to an origin-only path is a 404 at the edge."""
    for name, html in LIVE.items():
        assert f'href="{path}"' not in html, (
            f"{name} links {path} relatively. That page is also served under "
            f"hatchloop.dev, where {path} is not proxied, so the link 404s "
            f"there. Write it as {ORIGIN_TOKEN}{path}.")


def test_link_helper_absolutises_exactly_the_origin_only_set():
    for path in ORIGIN_ONLY:
        assert link(path) == API_ORIGIN + path
    # ...and leaves everything else alone. /pricing and the legal pages ARE
    # proxied; making them absolute would bounce buyers off the marketing site
    # onto the raw API host mid-journey, which is a worse bug than the one
    # being fixed here.
    for path in ("/pricing", "/terms", "/privacy", "/refund", "/docs", "/llms.txt"):
        assert link(path) == path, f"{path} must stay relative"


def test_no_page_declares_a_pwa_manifest():
    """`rel="manifest"` means a web-app manifest. We do not have one."""
    for name, html in LIVE.items():
        assert 'rel="manifest"' not in html, (
            f"{name} declares a PWA manifest. /manifest is the API contract - "
            f"the browser cannot parse it as a web-app manifest, and at the "
            f"edge it 404s on every page load.")


def test_nothing_unresolved_survives_rendering():
    """The token must be gone, and no f-string-only helper may leak as text."""
    for name, html in PAGES.items():
        assert ORIGIN_TOKEN not in html, (
            f"{name} still contains {ORIGIN_TOKEN} - it was not passed through "
            f"page(), so the token reached the browser as a literal href.")
        assert "{link(" not in html, (
            f"{name} contains a literal '{{link(' - that body is a PLAIN "
            f"string, not an f-string, so the call was never evaluated. Use "
            f"the {ORIGIN_TOKEN} token instead.")


def test_absolute_links_point_at_our_own_origin():
    """A typo'd or stale origin would send buyers to someone else's host."""
    for name, html in LIVE.items():
        for url in re.findall(r'href="(https?://[^"]+)"', html):
            host = url.split("/")[2]
            # endswith() alone accepts evil-hatchloop.dev. Match the apex
            # exactly or a real subdomain of it.
            ok = (host == "hatchloop.dev" or host.endswith(".hatchloop.dev")
                  or host == "polar.sh" or host.endswith(".polar.sh"))
            assert ok, f"{name} links off-site to {host}"


def test_the_site_and_the_agent_descriptors_agree_on_the_origin():
    """One env var, one meaning.

    `web/_partials.py` and `agent_interface/well_known.py` both need the API
    origin. They used to read PUBLIC_BASE_URL separately: _partials stripped a
    trailing slash and prepended a scheme when one was missing, well_known did
    neither. Set PUBLIC_BASE_URL scheme-less - exactly the case the _partials
    handling exists for - and the footer emitted
    `https://api.hatchloop.dev/health` while every machine-readable descriptor
    emitted `api.hatchloop.dev/openapi.yaml`, which no agent can fetch.

    A comment in _partials.py claimed THIS FILE already asserted they never
    drift. It did not. Both now import config.PUBLIC_BASE_URL, and this is the
    assertion that comment was describing.
    """
    from agent_interface.well_known import BASE_URL
    from config import PUBLIC_BASE_URL

    assert API_ORIGIN == BASE_URL, (
        f"the website says {API_ORIGIN} and the agent descriptors say "
        f"{BASE_URL} - one env var must not have two meanings")
    assert API_ORIGIN == PUBLIC_BASE_URL, (
        "both must be the canonical config value, not a re-derivation of it")
    assert API_ORIGIN.startswith("https://"), (
        f"{API_ORIGIN} has no scheme - descriptors would emit relative URLs")
    assert not API_ORIGIN.endswith("/"), (
        f"{API_ORIGIN} ends in a slash - every link becomes //path")
