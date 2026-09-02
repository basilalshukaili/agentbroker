"""
Shared HTML partials for the web UI.

We deliberately do NOT use React, Vite, or any client-side framework.
Reasons:
  - We're an MCP server. Humans visit the site rarely; agents call /mcp.
  - Each rendered page is <15 KB on the wire; React-from-CDN + Babel-standalone
    was 3 MB+ and recompiled JSX in the browser on every page load.
  - Server-rendered HTML is faster, indexable by LLM crawlers (llms.txt),
    works without JavaScript, and survives a totally cold Render free-tier wake.

A single Python file owns the chrome (head, nav, footer) so all pages stay
visually identical without a build step.
"""
from __future__ import annotations

# THE LEGAL PAGES LOOKED LIKE A DIFFERENT COMPANY'S SITE.
#
# /terms and /refund are served from here and rewritten onto hatchloop.dev,
# while /privacy is a page on the marketing site. So a buyer clicking "Terms"
# immediately before paying went from HatchLoop branding to an "Agent Broker"
# wordmark with a different nav - and an outside reviewer named that the
# single worst moment in their evaluation, the point at which they would have
# closed the tab.
#
# HatchLoop is the product line these pages govern; AgentBroker is one MCP
# server within it, and Techmate is the contracting party named in the
# document itself. Branding them HatchLoop is both the true hierarchy and the
# one that matches where the visitor came from.
BRAND = "HatchLoop"
# Single-source-of-truth for the public hostname. We own hatchloop.dev, so the
# DEFAULT must be our own domain: falling back to a *.workers.dev host put a
# generic domain into og:url on public pages whenever PUBLIC_BASE_URL was
# unset. (The old comment justified it by a Paddle checkout-domain approval;
# billing moved to Polar, so that reason is gone too.)
import os as _os
DOMAIN = _os.environ.get(
    "PUBLIC_BASE_URL",
    "https://hatchloop.dev",
).replace("https://", "").replace("http://", "").rstrip("/")
# The API origin as a full absolute URL, for links to routes that ONLY the
# origin serves (see ORIGIN_ONLY below).
#
# THE ONE normalised value, imported rather than re-derived. This block used to
# read PUBLIC_BASE_URL itself and normalise it; well_known.py read the same
# variable and did not, so the two disagreed whenever it was set scheme-less.
# Now there is nothing to disagree about, and test_public_links_resolve.py
# asserts the two stay identical - a claim this comment previously made about
# a test that did not exist.
try:
    from config import PUBLIC_BASE_URL as API_ORIGIN
except Exception:  # noqa: BLE001 - web pages must render even if config moves
    API_ORIGIN = "https://api.hatchloop.dev"
# Role addresses on our own domain. These defaulted to the founder's PERSONAL
# Gmail, which then appeared as the support and privacy contact on public
# pages - a privacy exposure, and it does not survive him being unavailable.
SUPPORT_EMAIL = _os.environ.get("SUPPORT_EMAIL", "hello@hatchloop.dev")
PRIVACY_EMAIL = _os.environ.get("PRIVACY_EMAIL", "privacy@hatchloop.dev")
# THE CONTRACTING PARTY. This one string is interpolated into Terms sections 7
# (who owns the Service), 9 (who you indemnify), 13 (the contracting party and
# notice address), the Privacy "who we are" controller declaration, the Refund
# policy, and the footnote on every page including /billing/checkout.
#
# It used to read "Agent Broker (sole proprietor: <the founder's full personal
# name>, Sultanate of Oman)". That was wrong in two ways at once: it named a
# legal form that holds no commercial registration, and it put the founder
# PERSONALLY on the indemnification clause of a contract governed by Omani law
# in the courts of Muscat - while the entity actually receiving the money,
# Techmate, got no contractual protection at all.
#
# Founder's ruling (2026-08-28): "we already registered techmate, we will treat
# techmate as legal company and hatchloop as its one of the products." So the
# seller is always Techmate; AgentBroker and HatchLoop are product names and are
# never a party to anything.
LEGAL_ENTITY = _os.environ.get(
    "LEGAL_ENTITY",
    "Techmate (شركة رفيق التقنية), "
    "CR 1661879, Muscat, Sultanate of Oman",
)

# The footer's merchant-of-record line used to name a payment company we never
# onboarded: no credentials for it exist anywhere, .env.example does not list it
# among the valid providers, and the checkout page's own body already said
# Polar. So one page told a buyer two different things about who takes the money.
# Kept as a Python comment, NOT an HTML comment - an HTML comment ships to the
# reader and would put that company's name back on the page it was removed from.


_BASE_CSS = """
*,*::before,*::after { box-sizing: border-box; }
:root {
  --bg: #0b0d12;
  --surface: #161922;
  --surface-2: #1f2330;
  --text: #e8eaf0;
  --text-muted: #9ba0ad;
  --accent: #6ee7b7;
  --accent-2: #60a5fa;
  --border: #2a2f3d;
  --radius: 12px;
}
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue",
               "Inter", "Roboto", sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.55; min-height: 100vh;
}
.container { max-width: 1100px; margin: 0 auto; padding: 0 24px; }
a { color: var(--accent-2); text-decoration: none; }
a:hover { text-decoration: underline; }
nav.site {
  display: flex; justify-content: space-between; align-items: center;
  padding: 20px 0; border-bottom: 1px solid var(--border);
}
nav.site .brand { font-weight: 600; letter-spacing: -.01em; font-size: 17px; color: var(--text); }
nav.site ul { display: flex; gap: 24px; list-style: none; padding: 0; margin: 0; flex-wrap: wrap; }
nav.site a {
  color: var(--text-muted); text-decoration: none; font-size: 14px;
}
nav.site a:hover { color: var(--text); }
nav.site a.active { color: var(--text); font-weight: 500; }
header.hero { padding: 72px 0 80px; text-align: center; }
header.hero h1 { font-size: clamp(36px, 5.5vw, 60px); margin: 0 0 22px; letter-spacing: -.025em; line-height: 1.05; }
header.hero p.lead { font-size: 18px; color: var(--text-muted); max-width: 720px; margin: 0 auto 36px; }
.cta { display: inline-flex; gap: 12px; flex-wrap: wrap; justify-content: center; }
.btn {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 12px 22px; border-radius: 10px;
  text-decoration: none; font-weight: 500; font-size: 15px;
  border: 1px solid transparent; transition: background .15s, border-color .15s, color .15s;
  cursor: pointer; font-family: inherit;
}
.btn-primary { background: var(--accent); color: #052e1f; }
.btn-primary:hover { background: #5dd6a4; text-decoration: none; }
.btn-secondary { background: transparent; border-color: var(--border); color: var(--text); }
.btn-secondary:hover { border-color: var(--text-muted); text-decoration: none; }
.section { padding: 56px 0; border-top: 1px solid var(--border); }
.section h2 { font-size: 28px; margin: 0 0 14px; letter-spacing: -.015em; }
.section p.lead { color: var(--text-muted); font-size: 16px; max-width: 720px; margin: 0 0 24px; }
.grid { display: grid; gap: 18px; margin-top: 24px; }
.grid-3 { grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
.grid-4 { grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 22px; transition: border-color .15s;
}
.card:hover { border-color: var(--text-muted); }
.card h3 { margin: 0 0 8px; font-size: 16px; }
.card p { margin: 0; color: var(--text-muted); font-size: 14px; }
.card code { background: var(--surface-2); padding: 2px 6px; border-radius: 4px; font-size: 13px; }
.metric { text-align: center; padding: 22px; }
.metric .num { font-size: 36px; font-weight: 600; color: var(--accent); line-height: 1.1; }
.metric .label { color: var(--text-muted); font-size: 13px; margin-top: 6px; }
.metric .num[data-live]::after {
  content: "";
  display: inline-block; width: 7px; height: 7px;
  margin-left: 8px; border-radius: 50%; background: var(--accent);
  animation: pulse 1.6s infinite; vertical-align: middle;
}
@keyframes pulse { 0%,100% { opacity: .25; } 50% { opacity: 1; } }
table { width: 100%; border-collapse: collapse; margin-top: 18px; font-size: 14px; }
th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--border); }
th { color: var(--text-muted); font-weight: 500; }
.tag { display: inline-block; padding: 2px 8px; background: var(--surface-2); border-radius: 4px; font-size: 12px; color: var(--text-muted); }
.tag-ok { color: var(--accent); }
.tag-ok::before { content: "✓ "; }
pre {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 16px; overflow-x: auto; font-size: 13px; line-height: 1.5;
  font-family: "SFMono-Regular", Menlo, Consolas, monospace; margin: 0;
}
code.inline { background: var(--surface-2); padding: 2px 6px; border-radius: 4px; font-size: 13px; }
article.legal h2 { font-size: 22px; margin: 32px 0 10px; }
article.legal h3 { font-size: 17px; margin: 22px 0 8px; color: var(--text); }
article.legal p, article.legal li { color: var(--text-muted); font-size: 15px; }
article.legal ul, article.legal ol { padding-left: 22px; }
article.legal .updated { color: var(--text-muted); font-size: 13px; }
footer.site {
  padding: 40px 0 60px; text-align: center; color: var(--text-muted);
  font-size: 13px; border-top: 1px solid var(--border); margin-top: 60px;
}
footer.site a { color: var(--text-muted); }
footer.site .footnote { margin-top: 12px; font-size: 12px; opacity: .7; }
@media (max-width: 720px) {
  nav.site { flex-direction: column; gap: 14px; align-items: flex-start; }
  nav.site ul { gap: 16px; }
  header.hero { padding: 40px 0 60px; }
}
"""


def _nav(active: str) -> str:
    items = [
        ("home", "https://hatchloop.dev/", "Home"),
        ("mcps", "https://hatchloop.dev/mcps/", "MCP servers"),
        ("pricing", "https://hatchloop.dev/pricing/", "Pricing"),
        ("docs", "https://hatchloop.dev/docs/", "Docs"),
        ("terms", "/terms", "Terms"),
        ("privacy", "/privacy", "Privacy"),
        ("refund", "/refund", "Refunds"),
    ]
    lis = "\n".join(
        f'      <li><a href="{href}" class="{("active" if key == active else "")}">{label}</a></li>'
        for key, href, label in items
    )
    return f"""<nav class="site container">
  <a href="/" class="brand">{BRAND}</a>
  <ul>
{lis}
  </ul>
</nav>"""


# These paths exist ONLY on the API origin. The edge (hatchloop.dev) proxies
# some origin routes and not these, so a RELATIVE link to one renders fine on
# api.hatchloop.dev and 404s on hatchloop.dev - and the legal pages, which are
# the ones a payment processor and a cautious buyer actually read, are served
# under BOTH. "Status" 404'd there for as long as the footer has existed.
#
# Absolute is the fix that survives whichever host renders the HTML. Keep this
# set honest: tests/test_public_links_resolve.py fetches every link on every
# public page against both hosts and fails the build on a dead one.
ORIGIN_ONLY = ("/health", "/manifest", "/supply/platforms",
               "/compliance/jurisdictions", "/compliance/check")


ORIGIN_TOKEN = "__ORIGIN__"


def link(path: str) -> str:
    """Absolute for origin-only paths, relative for everything else.

    Prefer writing `href="__ORIGIN__/health"` in page bodies over calling this:
    `page()` substitutes the token no matter what kind of string the body is.
    A `{link('/health')}` written into a PLAIN triple-quoted body renders as
    that literal text - which is how the first version of this fix shipped a
    visibly broken href into the home page and was caught only by rendering it.
    """
    return f"{API_ORIGIN}{path}" if path in ORIGIN_ONLY else path


def _footer() -> str:
    return f"""<footer class="site container">
  <p>
    <a href="/pricing">Pricing</a> &middot;
    <a href="/terms">Terms of Service</a> &middot;
    <a href="/privacy">Privacy Policy</a> &middot;
    <a href="/refund">Refund Policy</a> &middot;
    <a href="mailto:{SUPPORT_EMAIL}">Contact</a>
  </p>
  <p>
    <a href="{link('/manifest')}">Manifest</a> &middot;
    <a href="/openapi.yaml">OpenAPI</a> &middot;
    <a href="/llms.txt">llms.txt</a> &middot;
    <a href="{link('/status')}">Status</a> &middot;
    <a href="/docs">API docs</a>
  </p>
  <p>&copy; 2026 {BRAND}. All rights reserved.</p>
  <p class="footnote">{LEGAL_ENTITY} &middot; Payments by Polar as Merchant of Record.</p>
</footer>"""


def page(title: str, body_html: str, *, active: str, description: str | None = None) -> str:
    """Render a complete HTML page. ``body_html`` is inserted between nav and footer."""
    desc = description or (
        "Horizontal MCP server for AI agents. Find, verify, message, and "
        "schedule appointments with small businesses worldwide. 15 of 23 "
        "tools need no key at all; write tools get 100 free ops/day."
    )
    # Resolve origin-only links LAST, after the body is built. Doing it here
    # rather than at the call site is the whole point: a body written as a
    # plain triple-quoted string cannot interpolate anything, and that is the
    # form most of pages.py uses. A token survives both forms identically.
    body_html = body_html.replace(ORIGIN_TOKEN, API_ORIGIN)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width,initial-scale=1.0" />
<title>{title} &middot; {BRAND}</title>
<meta name="description" content="{desc}" />
<link rel="alternate" type="application/json" href="/.well-known/ai-plugin.json" title="OpenAI plugin" />
<link rel="alternate" type="application/json" href="/.well-known/anthropic-tools.json" title="Anthropic tool_use" />
<link rel="alternate" type="application/json" href="/.well-known/agents.json" title="A2A descriptor" />
<link rel="alternate" type="application/json" href="/.well-known/mcp.json" title="MCP descriptor" />
<!-- No <link rel="manifest">. That slot means a PWA WEB APP manifest, and
     /manifest is our API contract - the browser fetched it on every page load,
     found no top-level name/start_url/icons, and logged a parse error; under
     hatchloop.dev, where /manifest is not proxied, it logged a 404 instead. We
     are not a PWA, so the correct number of web-app manifests is zero. The
     contract is still linked from the footer, where it belongs. -->
<meta property="og:title" content="{title} &middot; {BRAND}" />
<meta property="og:description" content="{desc}" />
<meta property="og:type" content="website" />
<meta property="og:url" content="https://{DOMAIN}" />
<style>{_BASE_CSS}</style>
</head>
<body>
{_nav(active)}
<main class="container">
{body_html}
</main>
{_footer()}
</body>
</html>"""
