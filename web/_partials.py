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

BRAND = "Agent Broker"
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
# Role addresses on our own domain. These defaulted to the founder's PERSONAL
# Gmail, which then appeared as the support and privacy contact on public
# pages - a privacy exposure, and it does not survive him being unavailable.
SUPPORT_EMAIL = _os.environ.get("SUPPORT_EMAIL", "hello@hatchloop.dev")
PRIVACY_EMAIL = _os.environ.get("PRIVACY_EMAIL", "privacy@hatchloop.dev")
LEGAL_ENTITY = "Agent Broker (sole proprietor: Basil Mubarak Ali Al Shukaili, Sultanate of Oman)"


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
        ("home", "/", "Home"),
        ("pricing", "/pricing", "Pricing"),
        ("docs", "/docs", "API docs"),
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
    <a href="/manifest">Manifest</a> &middot;
    <a href="/openapi.yaml">OpenAPI</a> &middot;
    <a href="/llms.txt">llms.txt</a> &middot;
    <a href="/health">Status</a> &middot;
    <a href="/docs">API docs</a>
  </p>
  <p>&copy; 2026 {BRAND}. All rights reserved.</p>
  <p class="footnote">{LEGAL_ENTITY} &middot; Payments by Paddle.com as Merchant of Record.</p>
</footer>"""


def page(title: str, body_html: str, *, active: str, description: str | None = None) -> str:
    """Render a complete HTML page. ``body_html`` is inserted between nav and footer."""
    desc = description or (
        "Horizontal MCP server for AI agents. Find, verify, message, and "
        "schedule appointments with small businesses worldwide. Free for any "
        "agent up to 100 ops/month."
    )
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
<link rel="manifest" href="/manifest" />
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
