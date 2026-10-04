"""
Page renderers for the public web UI.

Each function returns a complete HTML string. main.py wires them to routes.
Live metrics (home page) are polled by a tiny vanilla-JS snippet — no React,
no build step, no external scripts.
"""
from __future__ import annotations

from web._partials import (page, BRAND, DOMAIN, SUPPORT_EMAIL,
                          PRIVACY_EMAIL, LEGAL_ENTITY)

# Prices are DERIVED, never hand-typed. billing/pricing.py is the single
# source of truth for per-operation cost (see its own docstring); this page
# must not fork a second copy of those numbers the way the old flat-rate
# $49/$499 "Developer"/"Business" plan table did -- that table was retired
# (docs/PRICING.md, "What we do not promise") but the numbers kept rendering
# here because nothing imported the real ones.
from billing.pricing import price_cents, max_credits, price_usd_str
# Every count the public pages state is DERIVED. The founder caught the payment
# page - which describes the credit rails for the whole platform - asserting a
# tool count belonging to one product. See web/facts.py for why the free-tool
# counts are two different numbers rather than one.
from web import facts
from core import tool_auth
# Credit COUNTS per package are code (billing/packages.py); the USD price of
# each package is set on Polar's dashboard and cannot be imported -- see
# _PACKAGE_USD below, mirrored from docs/PRICING.md / the live pricing page.
from billing.packages import PACKAGE_CREDITS

# The write tools that require a key and spend credits, shown with their price
# on the pricing and checkout pages.
#
# THIS WAS A HAND-TYPED COPY of core/tool_auth.WRITE_TOOLS_REQUIRING_AUTH,
# justified by a comment claiming test coverage kept the two in sync. Nothing
# did: it is a list literal in a rendering module and no test compared it to
# the set. Derived now, sorted so the table order is stable between renders.
_WRITE_OPS_FOR_CHECKOUT = sorted(tool_auth.WRITE_TOOLS_REQUIRING_AUTH)

_PACKAGE_USD = {"starter": 9, "growth": 29, "scale": 99}


def _op_cost_label(op: str) -> str:
    """Human-readable cost for a write op, derived from billing/pricing.py."""
    base = price_cents(op)
    cap = max_credits(op)
    if base == 0 and cap == 0:
        return "Free &mdash; adoption wedge, no charge."
    if cap > base:
        return (f"{base}&ndash;{cap} credits (${price_usd_str(op)}"
                f"&ndash;${cap / 100:.2f}). Reserves the max, settles the "
                f"actual cost from the receipt.")
    return f"{base} credits (${price_usd_str(op)}) per call."


# ---------------------------------------------------------------------------
# Home — landing page + live dashboard
# ---------------------------------------------------------------------------

_HOME_LIVE_JS = """
(function () {
  // Tiny live-metric updater. No frameworks, no external scripts.
  // Polls /api/metrics every 8s, fails silent. Stops when tab hidden.
  var nodes = document.querySelectorAll('[data-metric]');
  if (!nodes.length) return;
  var fmt = function (n) {
    return (typeof n === 'number') ? n.toLocaleString('en-US') : '0';
  };
  var update = async function () {
    try {
      var r = await fetch('/api/metrics', { headers: { 'Accept': 'application/json' } });
      if (!r.ok) return;
      var data = await r.json();
      nodes.forEach(function (el) {
        var k = el.getAttribute('data-metric');
        if (data[k] !== undefined) el.textContent = fmt(data[k]);
      });
    } catch (e) { /* offline OK */ }
  };
  update();
  var t = setInterval(function () {
    if (document.hidden) return;
    update();
  }, 8000);
  window.addEventListener('beforeunload', function () { clearInterval(t); });
})();
"""


def render_home() -> str:
    body = """
<header class="hero">
  <h1>The agent-callable layer for SMB transactions.</h1>
  <p class="lead">
    Agent Broker is the MCP server that lets autonomous AI agents (Claude, Cursor, Continue,
    any MCP client) actually <strong>do business</strong> with the long tail of small
    and mid-sized businesses worldwide &mdash; finding them, verifying them, booking
    appointments, sending messages, escalating to a human when stuck &mdash; with full
    TCPA / GDPR / CASL / PDPL compliance enforced at runtime by a non-bypassable gate.
  </p>
  <div class="cta">
    <a class="btn btn-primary" href="/docs">Browse the live API &rarr;</a>
    <a class="btn btn-secondary" href="#how">Connect to Claude</a>
    <a class="btn btn-secondary" href="/pricing">Pricing</a>
  </div>
  <p style="margin-top:36px; font-size:14px; color:var(--text-muted);">
    Example: an agent gets a real consumer request &mdash;<br>
    <code style="display:inline-block; margin-top:8px; padding:8px 14px; background:var(--surface-2); border-radius:6px; color:var(--text);">
      "Book me a haircut at https://cal.com/jane-salon next Tuesday at 3pm"
    </code><br>
    <span style="display:inline-block; margin-top:14px;">
      Or an SMB asks its agent to text its opted-in customers about a sale. Or a
      customer texts the salon "STOP" &mdash; our <code class="inline">handle_inbound</code>
      classifies the opt-out and records it in the consent store automatically.
      <strong>The compliance gate decides what's allowed, not the marketing copy.</strong>
    </span>
  </p>
</header>

<section class="section" id="scope">
  <h2>Five message types, four channels, 26 jurisdictions.</h2>
  <div class="grid grid-3">
    <div class="card">
      <h3 style="color:var(--accent);">What we facilitate</h3>
      <p>Consumer-initiated bookings. SMB-initiated messages to opted-in customers
         (marketing, reminders, transactional). Voice calls with two-party recording
         consent. Cold-start discovery via <code class="inline">import_booking_url</code>.
         Inbound classification + automatic STOP / opt-out handling.</p>
    </div>
    <div class="card">
      <h3 style="color:#fca5a5;">What the gate rejects</h3>
      <p>Marketing to recipients without recorded opt-in consent, where their
         country requires it. Bulk / list-based / drip campaigns.
         Cold outreach to non-opted-in numbers. A/B test sends. Spam by any definition.
         The gate runs synchronously before every send and returns a structured
         <code class="inline">compliance_violation</code> receipt on rejection.</p>
    </div>
    <div class="card">
      <h3>How enforcement works</h3>
      <p><a href="__ORIGIN__/compliance/check">/compliance/check</a> runs before every outbound
         channel call. TCPA, GDPR, CASL, PDPL rules across 26 jurisdictions, including
         GCC (UAE, SA, OM, QA, KW, BH). A request that violates returns a structured
         receipt and never reaches a carrier.</p>
    </div>
  </div>
</section>

<section class="section" id="live">
  <h2>Live activity</h2>
  <p class="lead">
    Public counters from this service. Update every 8 seconds.
    Numbers reset on each deploy.
  </p>
  <div class="grid grid-4">
    <div class="card metric">
      <div class="num" data-metric="total_agents_requested" data-live>0</div>
      <div class="label">Agent requests</div>
    </div>
    <div class="card metric">
      <div class="num" data-metric="total_businesses_found" data-live>0</div>
      <div class="label">Businesses returned</div>
    </div>
    <div class="card metric">
      <div class="num" data-metric="total_messages_sent" data-live>0</div>
      <div class="label">Messages sent</div>
    </div>
    <div class="card metric">
      <div class="num" data-metric="total_operations_completed" data-live>0</div>
      <div class="label">Operations completed</div>
    </div>
  </div>
</section>

<section class="section" id="how">
  <h2>Connect in one line, in any agent ecosystem.</h2>
  <p class="lead">We expose the same {n_tools} tools through every protocol agents speak today.</p>
  <div class="grid grid-3">
    <div class="card">
      <h3>MCP &mdash; Claude Desktop / Cursor / Continue</h3>
      <pre><code>{
  "mcpServers": {
    "agent-broker": {
      "url": "https://hatchloop.dev/mcp/agent-broker",
      "headers": {
        "X-Agent-Identity": "$TOKEN"
      }
    }
  }
}</code></pre>
    </div>
    <div class="card">
      <h3>OpenAI function calling</h3>
      <pre><code>tools = httpx.get(
  "https://hatchloop.dev"
  "/.well-known/openai-tools.json"
).json()["tools"]</code></pre>
    </div>
    <div class="card">
      <h3>Anthropic tool_use</h3>
      <pre><code>tools = httpx.get(
  "https://hatchloop.dev"
  "/.well-known/anthropic-tools.json"
).json()["tools"]</code></pre>
    </div>
  </div>
</section>

<section class="section" id="tools">
  <h2>{n_tools} tools. One contract. Worldwide.</h2>
  <p class="lead">
    Same OutcomeReceipt schema for every operation. Same compliance gate.
    Same idempotency contract. No surprises.
  </p>
  <div class="grid grid-3">
    <div class="card"><h3>{n_keyless} tools &mdash; always free</h3><p><code class="inline">find_business</code>, <code class="inline">verify_business</code>, <code class="inline">check_booking_link</code>, <code class="inline">check_compliance</code>, <code class="inline">preview_cost</code>, <code class="inline">get_status</code>, <code class="inline">get_outcome</code>, <code class="inline">self_test</code>, <code class="inline">check_quota</code>, <code class="inline">mint_key</code>, <code class="inline">lookup_us_contracts</code>. No key, unmetered.</p></div>
    <div class="card"><h3>{n_quota} tools &mdash; free within a daily quota</h3><p><code class="inline">verify_company_record</code> (GLEIF LEI + SEC EDGAR), <code class="inline">screen_sanctions</code> (OFAC SDN + EU Consolidated + UK Sanctions List), <code class="inline">map_trade_restriction</code>. 500/day with a free key, 100/day anonymous, then $0.02/call.</p></div>
    <div class="card"><h3>{n_needs_key} tools &mdash; need a free key</h3><p><code class="inline">send_message</code>, <code class="inline">capture_lead</code>, <code class="inline">schedule_appointment</code>, <code class="inline">send_transactional_confirmation</code>, <code class="inline">handle_inbound</code>, <code class="inline">escalate_to_human</code>, <code class="inline">import_booking_url</code>, <code class="inline">call_business</code>. 100 write ops/day free, then credits or x402. <code class="inline">get_conversation</code> is in this group and costs nothing &mdash; the key is what proves the thread is yours.</p></div>
  </div>
</section>

<section class="section" id="why">
  <h2>What's actually here &mdash; verifiable, not vibes.</h2>
  <p class="lead">
    Every number on this row maps to something you can confirm with one
    <code class="inline">curl</code>. Nothing simulated, nothing aspirational &mdash;
    just what the live service does today.
  </p>
  <div class="grid grid-4">
    <div class="card metric"><div class="num">{n_tools}</div><div class="label">Callable tools</div></div>
    <div class="card metric"><div class="num">12</div><div class="label">Booking platforms supported</div></div>
    <div class="card metric"><div class="num">26</div><div class="label">Jurisdictions with native compliance</div></div>
    <div class="card metric"><div class="num">2</div><div class="label">Payment rails (card via Polar, or x402/USDC)</div></div>
  </div>
  <div class="grid grid-4" style="margin-top:18px;">
    <div class="card metric"><div class="num">7</div><div class="label">Discovery protocols</div></div>
    <div class="card metric"><div class="num">{n_no_key}</div><div class="label">Tools usable with no key at all</div></div>
    <div class="card metric"><div class="num">$0</div><div class="label">Free tier &middot; reads always free</div></div>
    <div class="card metric"><div class="num">100/day</div><div class="label">Free write ops with a key</div></div>
  </div>
  <p style="margin-top:18px; font-size:14px; color:var(--text-muted);">
    Verify each: <a href="/.well-known/mcp.json">/.well-known/mcp.json</a> for tools,
    <a href="__ORIGIN__/supply/platforms">/supply/platforms</a> for the 12 booking integrations,
    <a href="__ORIGIN__/compliance/jurisdictions">/compliance/jurisdictions</a> for the 26 rule sets,
    <a href="__ORIGIN__/manifest">/manifest</a> for the canonical contract,
    <a href="__ORIGIN__/health">/health</a> for live status.
  </p>
</section>

<section class="section">
  <h2>Built right.</h2>
  <div class="grid grid-3">
    <div class="card"><span class="tag tag-ok">Discovery</span><h3>7 agent protocols</h3><p>MCP, OpenAI plugin, OpenAI tools, Anthropic tools, A2A, llms.txt, OpenAPI.</p></div>
    <div class="card"><span class="tag tag-ok">Compliance</span><h3>Non-bypassable gate</h3><p>Every message sent through the messaging tools passes the pre-check first. In the compliance audit log, recipient identifiers are stored as SHA-256 hashes; the privacy policy lists the other records.</p></div>
    <div class="card"><span class="tag tag-ok">Reliability</span><h3>Fallback chain</h3><p>direct_api &rarr; voice_ai &rarr; sms &rarr; email &rarr; web_form. Circuit breakers per channel.</p></div>
    <div class="card"><span class="tag tag-ok">Idempotency</span><h3>24h TTL</h3><p>Scoped per <code>(agent_id, operation, key)</code>. Safe to retry.</p></div>
    <div class="card"><span class="tag tag-ok">Async</span><h3>Webhook callbacks</h3><p>HMAC-SHA256 signed. Up to 24h retry with exponential backoff.</p></div>
    <div class="card"><span class="tag tag-ok">Worldwide</span><h3>Jurisdiction-detected</h3><p>26 jurisdictions with native rules. International conservative default for the rest.</p></div>
  </div>
</section>
""" + f'<script>{_HOME_LIVE_JS}</script>'
    return page("Home", body, active="home",
                description=f"{BRAND} — horizontal MCP server. "
                            f"{facts.total_tools()} tools, 7 discovery protocols, "
                            f"26 jurisdictions, free tier for AI agents.")


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def render_pricing() -> str:
    op_rows = "".join(
        f"<div class=\"card\"><h3><code class=\"inline\">{op}</code></h3>"
        f"<p>{_op_cost_label(op)}</p></div>"
        for op in _WRITE_OPS_FOR_CHECKOUT
    )
    package_rows = "".join(
        f"<tr><td>{name.title()}</td><td>${_PACKAGE_USD[name]}</td>"
        f"<td>{PACKAGE_CREDITS.get(name, 0):,}</td></tr>"
        for name in ("starter", "growth", "scale")
    )
    body = """
<header class="hero">
  <h1>Pay per call. No subscription, ever.</h1>
  <p class="lead">
    Two rails, both metered per call: credits bought by card through Polar,
    or pay-per-call in USDC on Base via <strong>x402</strong> &mdash; no
    signup, no card, no account. Reads are free on both rails; writes cost a
    few cents each. The compliance gate, not the price page, decides what
    gets sent &mdash; marketing requires verified opt-in regardless of how
    you pay.
  </p>
  <div class="cta" style="margin-top:8px;">
    <a class="btn btn-primary" href="/billing/checkout">Pay with card via Polar &rarr;</a>
    <a class="btn btn-secondary" href="/docs">See the live API &rarr;</a>
    <a class="btn btn-secondary" href="mailto:""" + SUPPORT_EMAIL + """?subject=Question">Questions &mdash; email us</a>
  </div>
</header>

<section class="section">
  <h2>What's free</h2>
  <p class="lead">{n_keyless} utility tools are free, no key, unmetered, forever:
  <code class="inline">find_business</code>, <code class="inline">verify_business</code>,
  <code class="inline">check_booking_link</code>, <code class="inline">check_compliance</code>,
  <code class="inline">preview_cost</code>, <code class="inline">get_status</code>,
  <code class="inline">get_outcome</code>, <code class="inline">self_test</code>,
  <code class="inline">check_quota</code>,
  <code class="inline">mint_key</code>, <code class="inline">lookup_us_contracts</code>.</p>
  <p class="lead"><code class="inline">get_conversation</code> is free and unmetered
  as well, and it still takes a key: a message thread is readable only by the
  agent identity that opened it, so a call with no key is refused rather than
  answered.</p>
  <p class="lead">{n_quota} premium data tools are free up to a daily quota &mdash;
  <code class="inline">verify_company_record</code>, <code class="inline">screen_sanctions</code>,
  <code class="inline">map_trade_restriction</code>: 500/day with a free key, 100/day
  anonymous, then $0.02/call past the quota. Past the quota the tool returns an
  honest failure (<code class="inline">free_quota_exceeded</code>), never a silent charge.</p>
</section>

<section class="section">
  <h2>Credit packages</h2>
  <p class="lead">1 credit = 1 US cent. Credits never expire. There is no
  subscription and nothing recurs &mdash; buy a package, spend it per call,
  buy another when you want more.</p>
  <table>
    <thead><tr><th>Package</th><th>Price</th><th>Credits</th></tr></thead>
    <tbody>""" + package_rows + """</tbody>
  </table>
  <p style="margin-top:12px;color:var(--text-muted);font-size:14px;">
  Buy at <a href="/billing/checkout">/billing/checkout</a>. Need volume beyond
  these packages, or a human conversation about your use case? Email
  <a href="mailto:""" + SUPPORT_EMAIL + """">""" + SUPPORT_EMAIL + """</a>.</p>
</section>

<section class="section">
  <h2>Write-tool cost per call</h2>
  <p class="lead">The {n_write_tools} write tools require a free email-verified key (100
  write ops/day, no cost) &mdash; beyond that, credits or x402.
  ({n_needs_key} tools need a key in total: these writes plus
  <code class="inline">get_conversation</code>, which costs nothing and spends
  no part of that allowance.)
  <code class="inline">preview_cost</code> returns these same numbers
  programmatically (free) and is the authoritative source: any drift between
  this page and <code class="inline">preview_cost</code> is a bug.</p>
  <div class="grid grid-3">""" + op_rows + """</div>
</section>

<section class="section">
  <h2>Billing &amp; payments</h2>
  <p class="lead">
    Card payments are processed by <strong>Polar</strong> (Merchant of
    Record) &mdash; Polar handles VAT/sales tax worldwide, and your
    <a href="/billing/checkout">pre-paid API key</a> is emailed automatically
    on payment. The x402 rail settles on-chain (USDC on Base); attach a
    signed payment in <code class="inline">params._meta["x402/payment"]</code>
    on a <code class="inline">tools/call</code> and the server answers an
    unpaid attempt with a priced offer first &mdash; no key, no account.
  </p>
  <div class="grid grid-3">
    <div class="card"><h3>Free tier</h3><p>No card required. {n_no_key} tools need no key at all; write tools get 100 free ops/day with a key.</p></div>
    <div class="card"><h3>Card (Polar)</h3><p><a href="/billing/checkout">Buy credits</a> &mdash; instant, emailed API key.</p></div>
    <div class="card"><h3>x402 (USDC on Base)</h3><p>Pay per call, no signup. See <a href="/docs">the API docs</a> for the payment flow.</p></div>
  </div>
</section>

<section class="section">
  <h2>FAQ</h2>
  <h3>Do you offer a free tier?</h3>
  <p style="color:var(--text-muted);">Yes. {n_keyless} tools are free, no key, unmetered
  (<code class="inline">get_conversation</code> is free too and takes a key, because
  it returns your own message threads).
  3 more are free up to a daily quota. Write tools get 100 free ops/day with a
  free email-verified key &mdash; no card required for any of it.</p>
  <h3>Can I change plan at any time?</h3>
  <p style="color:var(--text-muted);">There are no plans to change. Credits are
  bought in packages, spent per call, and never expire - buy a bigger package
  when you want more, and nothing recurs. This answer used to describe
  prorated upgrades and downgrades at the end of a billing period, which was
  left over from a subscription we retired.</p>
  <h3>What payment methods do you accept?</h3>
  <p style="color:var(--text-muted);">Cards (Visa, Mastercard, AmEx), Apple Pay,
  and Google Pay, routed through Polar &mdash; or USDC on Base via x402, with
  no signup at all.</p>
  <h3>Is there a contract?</h3>
  <p style="color:var(--text-muted);">No, and there is nothing to cancel -
  we do not bill on a recurring basis at all.</p>
</section>
"""
    return page("Pricing", body, active="pricing",
                description=f"{BRAND} pricing. {{n_keyless}} utility tools free with no key, {{n_quota}} more free within a daily quota. Write tools: free email-verified key (100 ops/day), then credits from $9 per 1,000, or x402. No subscription.")


# ---------------------------------------------------------------------------
# Checkout - Polar (card, Merchant of Record) + x402 (USDC on Base). Two
# metered rails; no subscription at any price (the retired "Developer $49" /
# "Business $499" flat-rate plan table lived here until this pass -- it never
# existed as a real product, per docs/PRICING.md's "What we do not promise").
# ---------------------------------------------------------------------------

def render_checkout(plan: str | None) -> str:
    plan_key = (plan or "starter").lower()
    if plan_key not in _PACKAGE_USD:
        plan_key = "starter"

    # NO BACKSLASH INSIDE AN f-STRING EXPRESSION.
    # Python 3.12 allows it; the production image is python:3.11-slim, which
    # raises SyntaxError at import so the container exits 1. CI runs 3.12, so
    # this parsed everywhere it was checked and failed only in production -
    # four deploys in a row, each reported as "update_failed" with a perfectly
    # healthy build. The escaped quotes are hoisted out of the f-string.
    _SELECTED_STYLE = ' style="color:var(--accent)"'
    package_rows = "".join(
        f"<tr{_SELECTED_STYLE if name == plan_key else ''}>"
        f"<td>{name.title()}{' &larr; selected' if name == plan_key else ''}</td>"
        f"<td>${_PACKAGE_USD[name]}</td>"
        f"<td>{PACKAGE_CREDITS.get(name, 0):,}</td></tr>"
        for name in ("starter", "growth", "scale")
    )
    op_rows = "".join(
        f'<tr><td><code class="inline">{op}</code></td>'
        f"<td>{_op_cost_label(op)}</td></tr>"
        for op in _WRITE_OPS_FOR_CHECKOUT
    )
    body = f"""
<header class="hero">
  <h1>How you pay</h1>
  <p class="lead">
    Two rails, both metered per call &mdash; no subscription at any price.
    Credits, bought by card through Polar. Or pay per call in USDC on Base
    via <strong>x402</strong>, with no signup and no account: attach a signed
    payment in <code class="inline">params._meta["x402/payment"]</code> on any
    paid tool call and the server answers an unpaid attempt with a priced
    offer first.
  </p>
  <p class="lead" style="font-size:16px;">
    One balance covers every HatchLoop server. Credits are the platform's unit,
    not any one product's: a credit bought today is spendable on whatever we run
    tomorrow, and each server states its own free tier and its own per-call
    price on its own page.
  </p>
</header>

<section class="section">
  <h2>Credit packages (card, via Polar)</h2>
  <p style="color:var(--text-muted);">
    1 credit = 1 US cent. Credits never expire, and they are not tied to a
    single server. On payment we email you a pre-paid key; your agent sends it
    as <code class="inline">X-Agent-Identity</code>, or as
    <code class="inline">Authorization: Bearer</code> or
    <code class="inline">X-Api-Key</code> if that is all your client can send
    &mdash; some connector hosts allow only standard header names.
  </p>
  <table>
    <thead><tr><th>Package</th><th>Price</th><th>Credits</th></tr></thead>
    <tbody>{package_rows}</tbody>
  </table>
  <div class="cta" style="margin-top:18px;">
    <a class="btn btn-primary" href="/billing/checkout">Pay with card via Polar &rarr;</a>
    <a class="btn btn-secondary" href="/docs">Or just try the free tools directly &rarr;</a>
  </div>
  <p style="margin-top:12px;color:var(--text-muted);font-size:14px;">
    Need volume beyond these packages? Email
    <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>.
  </p>
</section>

<section class="section">
  <h2>Write-tool cost per call &mdash; Agent Broker</h2>
  <p style="color:var(--text-muted);">
    Prices below are Agent Broker's. Every server publishes its own table; the
    credits are the same credits.
    These {{n_write_tools}} tools need a free email-verified key (100 write ops/day, no
    cost) before they spend anything; beyond that, credits or x402.
    <code class="inline">preview_cost</code> returns these same numbers
    programmatically for free.
  </p>
  <table>
    <thead><tr><th>Tool</th><th>Cost</th></tr></thead>
    <tbody>{op_rows}</tbody>
  </table>
</section>

<section class="section">
  <h2>Your rights either way</h2>
  <ul style="color:var(--text-muted);">
    <li><strong>Compliance gate.</strong> Every message sent through the messaging tools routes through
        <a href="__ORIGIN__/compliance/check">/compliance/check</a> &mdash; TCPA, GDPR, CASL,
        PDPL across 26 jurisdictions. Where the recipient&rsquo;s country requires opt-in, marketing
        without recorded consent is rejected at runtime regardless of how you paid.</li>
    <li><strong>14-day refund</strong> on credit packages. See <a href="/refund">Refund Policy</a>.</li>
    <li><strong>Privacy.</strong> In the compliance audit log, recipient phone numbers and emails are
        stored as SHA-256 hashes; the privacy policy lists every other record we keep.
        See <a href="/privacy">Privacy Policy</a>.</li>
    <li><strong>Governing law:</strong> Sultanate of Oman. EU/UK/CA consumer statutory
        rights are preserved. See <a href="/terms">Terms</a>.</li>
  </ul>
</section>
"""
    return page("How you pay", body, active="pricing",
                description=f"Credits, bought by card through Polar. Or pay per call in USDC via x402, with no signup. {BRAND} does not require human signup to use the {{n_no_key}} free tools.")


# ---------------------------------------------------------------------------
# Terms of Service
# ---------------------------------------------------------------------------

def render_status() -> str:
    """A status page a person can read.

    The footer linked "Status" straight at /health, which serves raw JSON. A
    buyer checking whether we are up got a machine payload - and an outside
    reviewer listed it among the things that cost us trust.

    It is DERIVED FROM THE SAME health_check() the monitors and Render use, so
    this page cannot claim "operational" while the endpoint says otherwise.
    That is the whole point: a status page maintained separately from the
    thing it reports on eventually lies, and a green light nobody computes is
    the purest form of the defect this codebase keeps finding.
    """
    from agent_interface.discovery import health_check

    h = health_check()
    ok = h.get("status") == "healthy"
    colour = "#10b981" if ok else "#f59e0b"
    word = "All systems operational" if ok else "Degraded"

    rows = "".join(
        f'<tr><td style="padding:.5rem 1rem .5rem 0">{k}</td>'
        f'<td style="padding:.5rem 0;color:'
        f'{"#10b981" if v == "ok" else "#f59e0b"}">{v}</td></tr>'
        for k, v in (h.get("checks") or {}).items()
    )

    body = f"""
  <h1>Status</h1>
  <p style="font-size:1.15rem;color:{colour};font-weight:600">{word}</p>
  <p style="color:var(--text-muted);font-size:.9rem">Checked {h.get('timestamp')}.
  This page runs the same checks as our monitoring - it is not a separately
  maintained light.</p>

  <h2>Service checks</h2>
  <table style="border-collapse:collapse">{rows}</table>

  <h2>What these mean</h2>
  <ul>
    <li><strong>manifest</strong> - the tool catalogue loads and is non-empty.</li>
    <li><strong>directory</strong> - the supply directory loads.</li>
    <li><strong>compliance</strong> - the jurisdiction rules are present.</li>
  </ul>
  <p style="color:var(--text-muted);font-size:.9rem">These are checks on THIS
  service. A dependency being slow - a sanctions authority, a registry - does
  not show here; every tool reports that in its own response instead, naming
  the source that was unavailable. That is deliberate: a status page that goes
  red when someone else's server blinks trains you to ignore it.</p>

  <h2>Machine-readable</h2>
  <p><a href="__ORIGIN__/health">__ORIGIN__/health</a> returns the same data as JSON.</p>
"""
    return page("Status", body, active="", description="Live service status.")


def render_terms() -> str:
    body = f"""
<article class="legal">
  <h1>Terms of Service</h1>
  <p class="updated">Last updated: 4 October 2026.</p>
  <p class="updated">Changed in this version: section 2 (service description) now describes the service as it runs today, including what each tool does and does not do, which channels each messaging tool can use, the readiness labels, Arabic-name screening, what the compliance gate does and does not cover, and the date above. In section 6, the first prohibition no longer says that a marketing message must carry a consent record identifier, an argument the sending tools do not take; it still prohibits marketing without recorded opt-in consent. Sections 1, 3 to 5 and 7 to 13 are unchanged from the version of 29 April 2026.</p>

  <h2>1. Acceptance</h2>
  <p>By using {BRAND} (the &ldquo;Service&rdquo;), you agree to these Terms.
  If you do not agree, do not use the Service.</p>

  <h2>2. Service description &amp; scope</h2>
  <p>The Service is HatchLoop&rsquo;s AgentBroker, a Model Context Protocol (MCP) server and API operated by {LEGAL_ENTITY}. It gives AI agents tools to check and find businesses and, on the channels that are enabled, to message them and book with them. The published tool list, with each tool&rsquo;s description and whether it is available on the current deployment, is served by the Service&rsquo;s MCP endpoint (<code>https://hatchloop.dev/mcp/agent-broker</code>, method <code>tools/list</code>) and summarised in the <a href="https://hatchloop.dev/docs/">documentation</a>. At the date above the tools are:</p>
  <ul>
    <li><strong>Company and sanctions checks.</strong> <code>verify_company_record</code> looks a company up in the GLEIF global LEI registry and in SEC EDGAR. <code>screen_sanctions</code> screens a name against the OFAC SDN list, the EU consolidated list and the UK Sanctions List; a name written in Arabic script is read as well and compared with the Arabic-script names the EU and UK lists publish, and a name that only sounds like a listed one is returned as an unverified candidate for a person to check, never as a match. <code>map_trade_restriction</code> screens a destination and parties for cross-border trade restrictions. <code>lookup_us_contracts</code> searches US federal contract awards through the public USAspending.gov API.</li>
    <li><strong>Business discovery.</strong> <code>find_business</code> finds businesses near a place using OpenStreetMap data, and <code>verify_business</code> looks businesses up in the Service&rsquo;s own supply network.</li>
    <li><strong>Messaging and booking.</strong> <code>send_message</code> sends messages on the channels enabled on the current deployment, which are WhatsApp and email. <code>send_transactional_confirmation</code> sends by email only on the current deployment: a phone-number recipient needs SMS, which is not enabled. SMS and voice calling (<code>call_business</code>) are listed but are not enabled on the current deployment, and those tools say so in their own responses. <code>handle_inbound</code> classifies a reply by keyword rules and <code>get_conversation</code> reads a conversation. <code>capture_lead</code> records a lead in the Service&rsquo;s own lead funnel and does not notify the business. <code>escalate_to_human</code> writes a ticket to the Service&rsquo;s operator queue; it sends no notification and makes no response-time commitment. <code>schedule_appointment</code> books through Cal.com when a business&rsquo;s booking link is connected to the Service&rsquo;s Cal.com account; otherwise it reports that it cannot, and does not invent a booking.</li>
    <li><strong>Utility tools.</strong> <code>preview_cost</code>, <code>check_quota</code>, <code>check_compliance</code>, <code>check_booking_link</code>, <code>import_booking_url</code>, <code>get_status</code>, <code>get_outcome</code>, <code>self_test</code> and <code>mint_key</code>.</li>
  </ul>
  <p>A tool that has a limit carries a readiness label in its description: <em>beta</em> (it works, with a limit on what you can rely on), <em>limited</em> (it works for a narrow set of inputs and fails honestly for the rest) or <em>unavailable</em> (the current deployment lacks what it needs). The sentence in the tool&rsquo;s metadata says what the limit is; read it before relying on the tool.</p>
  <p>Screening and lookup results are informational. Each response names the public lists and registries it used and the ones it did not (the UN consolidated list, for example, is NOT screened), and those sources can be incomplete or out of date. A result is not legal advice, not a clearance and not a compliance determination, and &ldquo;no match&rdquo; is not a finding that a person or company is free of sanctions or restrictions. Prices and free daily allowances are published on the <a href="https://hatchloop.dev/pricing/">pricing</a> page, refunds on the <a href="https://hatchloop.dev/refund">refund</a> page, and how data is handled in the <a href="__ORIGIN__/privacy">privacy policy</a>.</p>
  <p>Every message sent through the messaging tools (<code>send_message</code> and
  <code>send_transactional_confirmation</code>) routes through a non-bypassable compliance
  gate that enforces TCPA / GDPR / CASL / PDPL rules across 26 jurisdictions.
  A marketing message must state the recipient&rsquo;s country (<code>country_code</code>).
  Where that country&rsquo;s rules require the recipient&rsquo;s opt-in, the gate checks the
  consent recorded for that recipient and channel and, without one, rejects the send with a
  structured <code>compliance_violation</code> receipt that never reaches a carrier; where
  the rules work by opt-out instead (a marketing email in the United States, for example),
  the gate enforces the opt-out and unsubscribe rules. An opt-out is honoured in every
  jurisdiction. The gate, not the API surface, is the safety mechanism. It does not cover
  everything the Service itself sends: the sign-in, key and billing emails that go to our own
  users, and the clarifying question that the WhatsApp inbound handler sends back to a person
  who has messaged us (which checks the opt-out list first), are not sent through it.</p>
  <p>The Service is offered on an &ldquo;as-is&rdquo; basis with no
  implied warranties.</p>

  <h2>3. Eligibility</h2>
  <p>You must be at least 18 years old. By using the Service you represent
  that you meet this requirement.</p>

  <h2>4. Your responsibilities</h2>
  <ul>
    <li>Keep your API credentials confidential. You are responsible for all
        activity under your <code>X-Agent-Identity</code> token.</li>
    <li>Comply with applicable telecommunications and privacy law in every
        jurisdiction your agent reaches (TCPA, GDPR, CASL, PDPL, and
        country-specific equivalents).</li>
    <li>Provide accurate and up-to-date contact information for your account.</li>
  </ul>

  <h2>5. Compliance gate</h2>
  <p>The Service implements a non-bypassable compliance pre-check on
  outbound communications (SMS, voice, email). If the pre-check returns
  <code>not_allowed</code>, the operation will not be sent. You may not
  attempt to circumvent this gate.</p>

  <h2>6. Prohibited uses</h2>
  <p>The following uses are <strong>strictly prohibited</strong> and will result
  in immediate suspension of your account:</p>
  <ul>
    <li><strong>Marketing without recorded opt-in consent.</strong> Every
        marketing message must be sent only to a recipient whose opt-in
        consent is on record; where the recipient&rsquo;s country requires
        opt-in, the compliance gate checks that record at send time and
        rejects any send tagged <code>marketing</code> without one.</li>
    <li><strong>Bulk, list-based, A/B test, or drip outbound communication</strong>
        to recipients who did not request that specific outreach. We are a
        per-call transaction broker, not a campaign sender.</li>
    <li><strong>Cold outreach</strong> &mdash; contacting any recipient who has
        no prior relationship with the SMB and has not initiated or
        pre-authorized the communication.</li>
    <li><strong>Sales prospecting</strong> &mdash; using the Service to find
        businesses or individuals for the purpose of pitching them.</li>
    <li>Bulk communications (&ldquo;spam&rdquo;) by any definition.</li>
    <li>Harassing, threatening, or defrauding any person or business.</li>
    <li>Impersonating another person or entity.</li>
    <li>Reverse-engineering, scraping, or rate-abusing the Service.</li>
    <li>Circumventing or attempting to circumvent the compliance pre-check.</li>
    <li>Any use that violates applicable telecommunications, privacy, or
        consumer-protection law (including TCPA, CAN-SPAM, GDPR, CASL, PDPL).</li>
  </ul>

  <h2>7. Intellectual property</h2>
  <p>The Service, including code, manifests, and discovery surfaces, is
  owned by {LEGAL_ENTITY}. The published tool set and OutcomeReceipt schema are
  free to call under the agreed terms; the underlying implementation is
  proprietary.</p>

  <h2>8. Limitation of liability</h2>
  <p>To the fullest extent permitted by law, the Service&rsquo;s aggregate
  liability for any claim arising from your use is limited to the greater
  of (a) the fees you paid in the 12 months preceding the claim, or
  (b) USD 100. The Service is not liable for indirect, incidental,
  consequential, or punitive damages.</p>

  <h2>9. Indemnification</h2>
  <p>You agree to indemnify and hold harmless {LEGAL_ENTITY} from any
  claim, damages, or costs arising from your violation of these Terms,
  applicable law, or any third party&rsquo;s rights.</p>

  <h2>10. Termination</h2>
  <p>We may suspend or terminate your access for any breach of these
  Terms or for misuse of the compliance gate. You may stop using the
  Service at any time.</p>

  <h2>11. Governing law</h2>
  <p>These Terms are governed by the laws of the Sultanate of Oman.
  Disputes shall be resolved by the courts of Muscat, Oman, without
  prejudice to any non-waivable consumer rights you may have under
  the laws of your country of residence.</p>

  <h2>12. Changes to these Terms</h2>
  <p>We may modify these Terms. Material changes will be announced on
  this page at least 30 days before they take effect.</p>

  <h2>13. Contact</h2>
  <p>{LEGAL_ENTITY}<br>
  Email: <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a></p>
</article>
"""
    return page("Terms of Service", body, active="terms",
                description=f"{BRAND} Terms of Service. Governing law: Sultanate of Oman.")


# ---------------------------------------------------------------------------
# Privacy Policy
# ---------------------------------------------------------------------------

def render_privacy() -> str:
    body = f"""
<article class="legal">
  <h1>Privacy Policy</h1>
  <p class="updated">Last updated: 4 October 2026.</p>
  <p class="updated">Changed in this version: where the service is hosted (our own server rather than Vercel, Render or Supabase; Vercel now only hosts our DNS records, and Cloudflare relays only the older workers.dev address), who else receives data, what the usage log holds, where the text you send to lookup tools goes and what is kept in memory, the business records and queued messages that some tools leave in our database, how long things are kept (including the service&rsquo;s own program log), and the sign-in that AI assistants use (&ldquo;Connect&rdquo;): its cookie, what it stores and what it sends to Resend. Two things the previous version promised are no longer promised: 30-day advance notice of material changes to this policy (the Terms keep their own 30 days for changes to the Terms) and the list of data we never collect (card numbers are still covered in section 2). The previous version of this page was dated 29 April 2026.</p>

  <h2>1. Who we are</h2>
  <p>HatchLoop and AgentBroker are products of <strong>{LEGAL_ENTITY}</strong>. Techmate is the legal entity behind this site and the data controller for everything described below; HatchLoop is the name of the product, not a separate company.</p>
  <p>This policy covers the website hatchloop.dev and the AgentBroker service at hatchloop.dev and api.hatchloop.dev: an MCP server and API that lets AI agents check and find businesses and, on the channels that are enabled, message and book with them. Contact for anything in this policy: <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>.</p>

  <h2>2. What we collect</h2>
  <p><strong>When you visit hatchloop.dev or call the API.</strong></p>
  <ul>
    <li>Our web server keeps an access log of every request: time, IP address, the address requested (with session tokens and key parameters removed), user agent, response status and size. API key headers are replaced with a placeholder before anything is written.</li>
    <li>We measure visits with Umami, which runs on our own server and not at an analytics company. It sets no cookies. It records the page address, referrer, page title, browser, operating system, device type, screen size, language and approximate location (country, region, city). Its database has no field for your IP address.</li>
    <li>Every page asks our API whether you are signed in, which is a request to our server like any other. We set a cookie only when you sign in: the sign-in session cookie (<code>hl_portal</code>, 30 days) after you sign in to the portal, and one short-lived cookie while you connect an AI assistant (see &ldquo;Connecting an assistant&rdquo; below). Your browser also remembers, in local storage, which credit package you chose while signing in.</li>
  </ul>
  <p><strong>When your agent calls a tool.</strong> For each call the usage log records:</p>
  <ul>
    <li>the time, the type of request (such as <code>tools/call</code>), the tool name (or the name asked for, if it is not one of ours), the outcome, the error code and status for a call that failed, and how long it took;</li>
    <li>the door of the service that was called (such as <code>agent-broker</code>) and the protocol version; for a web request that failed before a tool ran, the method and a sanitised version of the path (words that look like identifiers only, never a key or token), such as <code>POST /mcp</code>;</li>
    <li>the client name and version your software reports;</li>
    <li>whether a key was used, its state and its identifier (for a free key, a short code derived from a hash of your email address; for a paid account, your Polar customer identifier);</li>
    <li>the names of the arguments you sent, never their values, and a short one-way hash of the arguments;</li>
    <li>a short one-way hash of your IP address, and your user agent;</li>
    <li>a label we derive from the call: a registry crawler, a caller with no key, or a caller with a key.</li>
    <li>The values you pass to lookup and screening tools (a company name, a person&rsquo;s name, a place) are not written to the usage log and, for those tools, are not saved in the database; section 3 says what is held in memory. Tools that change state (sending a message, capturing a lead, booking, escalating, importing a booking page) store a record of the operation, described in section 5.</li>
    <li>A billing record of each charge: the tool, the amount, the status, the time and your key identifier.</li>
  </ul>
  <p><strong>Keys, sign-in and payments.</strong></p>
  <ul>
    <li>If you request a free key we store your email address (lower-cased), a verification token with an expiry (deleted once you use the link), and the key we issue to you.</li>
    <li>Sign-in to the portal is by an emailed link; there are no passwords. We keep your email, your plan, your credit balance and the ledger of credits granted and spent.</li>
    <li>Card payments are handled by Polar as merchant of record. We receive your email address, the order and the amount. We never see or store card numbers. When credits are bought we also keep a SHA-256 hash of the buyer&rsquo;s email address next to the Polar customer identifier and the plan, so that an assistant connected with the same address can spend them.</li>
  </ul>
  <p><strong>Connecting an assistant (&ldquo;Connect&rdquo;).</strong> Some AI assistants open a sign-in page on api.hatchloop.dev so that you can use the tools that need an account. What that records:</p>
  <ul>
    <li>The page asks for your email address and we email you a one-time link, valid for 15 minutes. We use the address to send that one message and do not store it. We keep a SHA-256 hash of it, so that the same address reaches the same account next time, and a masked hint such as <code>j***@gmail.com</code> for the confirmation page.</li>
    <li>We keep the sign-in itself (the app that asked, the address it returns to and the permission requested) and SHA-256 hashes of the link, the authorisation code and the refresh token we give the assistant, never the values. An access token lasts one hour. A refresh token lasts 30 days, is replaced each time it is used and ends at the latest 90 days after you signed in.</li>
    <li>While you sign in we set one cookie, <code>hl_oauth_</code> followed by the sign-in&rsquo;s identifier. It lasts 15 minutes, is sent only to the sign-in pages (<code>/oauth</code>) and proves that the browser that opened the page is the one that started the sign-in. Signing in needs it.</li>
    <li>To learn who an app is, our server fetches the app&rsquo;s published client-metadata document from the web address that identifies it; that request carries our server&rsquo;s address and nothing about you. An app that registers itself with us is kept with its name, its return addresses and a short hash of its network address.</li>
    <li>Abuse limits count sign-in attempts per network address and per hashed email address, in the server&rsquo;s memory only.</li>
  </ul>
  <p><strong>Forms and email.</strong></p>
  <ul>
    <li>The feedback form stores the note you type and your browser&rsquo;s user agent. The waitlist forms store your email address, the page you used and an optional note. Neither is sent to anyone else.</li>
    <li>Email you send to hello@hatchloop.dev is received by our email-forwarding provider and delivered to a Gmail mailbox read by our team.</li>
  </ul>

  <h2>3. Lookup tools: where the text you send goes</h2>
  <ul>
    <li><code>screen_sanctions</code> and the party screening inside <code>map_trade_restriction</code> compare the name with copies of the OFAC, EU and UK sanctions lists that we download from their publishers and hold on our server. The name is not sent to the publishers or to anyone else.</li>
    <li><code>verify_company_record</code> sends the company name (and country, if given) to the GLEIF LEI registry (api.gleif.org). For US public companies it downloads the SEC&rsquo;s public company file and matches the name on our server.</li>
    <li><code>lookup_us_contracts</code> sends the company name to the USAspending.gov public API (api.usaspending.gov).</li>
    <li><code>find_business</code> sends the place text you give it (up to 200 characters), the search area and the business category to the public OpenStreetMap services: Nominatim for geocoding, run by the OpenStreetMap Foundation (nominatim.openstreetmap.org), and the public Overpass API for businesses, which has its own operator (overpass-api.de). They also receive our server&rsquo;s IP address. Treat that field as public and do not put a private person&rsquo;s home address in it. The server remembers a place for up to 7 days and the businesses found there for 6 hours, so that a repeat costs the public servers nothing. The map data is &copy; OpenStreetMap contributors, available under the ODbL.</li>
    <li><code>import_booking_url</code> fetches the booking page address you give it, from our server, and saves a business record built from it (and from any name, phone number or email address you pass with it) in our supply directory, in readable form: see section 5.</li>
    <li>The Retail Broker at hatchloop.dev/retail forwards a visitor&rsquo;s product search to the public catalogue endpoints of the stores it searches.</li>
  </ul>
  <p>Some of this is held in the server&rsquo;s memory only, never in the database: the places and results above, and, so that the sanctions screen is fast, its analysis of recently screened names and name parts. A restart of the service empties all of it.</p>
  <p>The service does not send tool inputs, messages or personal data to any AI model provider. HatchLoop is operated with the help of AI assistants, and the people and assistants who operate it can read the records described in this policy.</p>

  <h2>4. How we use it</h2>
  <p>To provide and bill the service, run the compliance gate (including opt-outs), prevent abuse and keep the service reliable. We do <strong>not</strong> sell personal data and we do not use message content for advertising. The legal bases we rely on, where GDPR or UK GDPR applies, are contract (delivering the service you asked for), legitimate interests (abuse prevention, security logging, reliability) and legal obligation (tax records and regulatory disclosures).</p>

  <h2>5. Messaging</h2>
  <p><code>send_message</code> works on WhatsApp and email, the channels enabled on the current deployment; <code>send_transactional_confirmation</code> works by email only, because a phone-number recipient would need SMS. SMS and voice calling are not enabled, and nothing is sent to an SMS or voice provider. When your agent sends a message we process its content and the recipient&rsquo;s identifier to deliver it and to run the compliance gate (TCPA, GDPR, CASL and PDPL checks, consent and opt-out enforcement). Replying <strong>STOP</strong> on a channel, including WhatsApp, records an opt-out that blocks further messages to you on that channel.</p>
  <p>What these tools store:</p>
  <ul>
    <li>The compliance audit log keeps a SHA-256 hash of each recipient identifier, with the channel, the decision and the reason, and no plaintext identifier.</li>
    <li>Other records hold identifiers in readable form: opt-outs (the recipient and the channel), leads your agent captures (name, phone, email, notes), conversation records (the numbers involved and the message text) and replies received on WhatsApp (sender number, profile name and text), which are kept so the requesting agent can read them.</li>
    <li>Receipts of operations that change state, which can include the free text your agent supplied, and the ticket that a human escalation writes to our operator queue (the reason, the recommended next step your agent wrote and your key identifier).</li>
    <li>The supply directory. <code>import_booking_url</code> saves a business record in our database table <code>smb_supply</code> (and keeps it in memory so that it can be found), in readable form: the business name (the one your agent gives, or the title of the page), the booking-page address and the booking platform it detects, the country and kind of business, the capabilities and channels, and any phone number or email address your agent passes with it. A record made by one agent can be read back by any other: <code>find_business</code> and <code>verify_business</code> return its name, location, capabilities and channels, <code>schedule_appointment</code> uses its booking-page address, and <code>call_business</code>, which is not enabled on the current deployment, would call its stored phone number. Nothing deletes these records on a schedule; to have one removed, email <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>.</li>
    <li>Queued messages. When the business your agent is messaging (identified by the <code>business_id</code> your agent supplies) is over its message budget, <code>send_message</code> queues the request instead of sending it, and stores, in readable form, the recipient&rsquo;s phone number, your agent&rsquo;s identifier, your agent&rsquo;s reference for the end user and the first 200 characters of the message. Once a request has been queued for 48 hours it is marked expired, not deleted. Queued requests can be sent to that business together, in one WhatsApp digest message that carries that text and your agent&rsquo;s reference for the end user, when the business&rsquo;s service window is open; a record of each digest (the numbers, the request identifiers and their state, not the message text) is kept.</li>
  </ul>

  <h2>6. Who else receives data</h2>
  <p><strong>Providers we use to run the service.</strong></p>
  <ul>
    <li><strong>Hostinger</strong> (Hostinger International Limited) rents us the virtual server that runs the website, the API, the database and its backups.</li>
    <li><strong>Vercel</strong> hosts the DNS records (the name servers) for hatchloop.dev. It does not serve the site or carry its traffic.</li>
    <li><strong>Resend</strong> sends our email: sign-in links, key verification, billing notices, and email sent through the messaging tools. It receives the recipient address, the subject and the text of each message. For Connect that is the address you typed on the sign-in page and a message that names the app asking to connect, says where it will return to and carries the one-time link.</li>
    <li><strong>Polar</strong> is the merchant of record for card payments, and receives your email and order details.</li>
    <li><strong>Forward Email</strong> and <strong>Google (Gmail)</strong> receive and hold the email you send to our contact address.</li>
    <li><strong>Telegram</strong> receives internal purchase alerts for our team, in which the customer&rsquo;s email address is masked. The Telegram Mini App page at hatchloop.dev/app loads a script from telegram.org.</li>
  </ul>
  <p><strong>Providers involved only in particular cases.</strong></p>
  <ul>
    <li><strong>Meta</strong> (WhatsApp Business Platform) carries WhatsApp messages and receives the recipient&rsquo;s number and the text. Meta&rsquo;s own terms and privacy policy also apply to that channel.</li>
    <li><strong>Cal.com</strong> receives the attendee&rsquo;s name, email address and notes when a booking is made.</li>
    <li><strong>Cloudflare</strong> runs a relay, a Cloudflare Worker at agent-broker-edge.basil-agent.workers.dev, for the older address under which AgentBroker was first listed in some MCP directories, and it is still running. A client that connects through that address sends its requests, including the tool arguments and the key header, to Cloudflare, which passes them on to our server; the relay keeps a daily counter per IP address for the free allowance, and Cloudflare may log the requests it handles. Connections to hatchloop.dev and api.hatchloop.dev do not go through Cloudflare.</li>
    <li><strong>Render</strong> runs only a redirect from our former address (smb-broker.onrender.com) to api.hatchloop.dev. A client that still uses the old address sends its first request there, so Render sees that request.</li>
  </ul>
  <p><strong>Public sources</strong> that receive the text you ask us to look up are listed in section 3: GLEIF, OpenStreetMap services, USAspending.gov and the stores searched by the Retail Broker.</p>
  <p><strong>Integrations that exist but are switched off</strong> and receive no messages or personal data: SMS (Twilio), voice calls (Vapi) and on-chain payments (Coinbase). The service&rsquo;s public health check (<code>/healthz/external</code>) does call the status endpoints of the providers that are configured, Vapi among them, with our own credentials, to see whether they are up.</p>

  <h2>7. Where data is processed</h2>
  <p>The server is in Kuala Lumpur, Malaysia. We operate from Oman, and the database backups are also copied to a company computer. The providers in section 6 process data in their own locations, which include the United States and Europe, and the Cloudflare relay for the older address handles each request in whichever Cloudflare location is nearest to the caller.</p>

  <h2>8. How long we keep it</h2>
  <ul>
    <li>Web server access logs are kept in rotating files limited by size (20 MB a file, 10 files per site), which at current traffic is a matter of days. The server&rsquo;s system journal keeps at most 7 days.</li>
    <li>The service&rsquo;s own program log (what it prints while it runs: warnings and errors) is kept by the container runtime for as long as the container exists. It has no line for each web request, and the lines that mention a customer&rsquo;s email address show it masked, with one exception: when a paid order cannot be recorded, the line that preserves the order, as a recovery record, holds the buyer&rsquo;s address. Earlier releases of the service are kept stopped, for rollback, together with their logs, and are removed by hand with no fixed schedule; their logs can still hold sign-in links and session tokens (from the request lines they used to write) and some email addresses in plain text.</li>
    <li>Database backups are written every 12 hours and kept for 30 days, on the server and on that company computer.</li>
    <li>Connect sign-ins, authorisation codes and refresh tokens expire on their own (see section 2). Expired sign-ins and codes, and refresh tokens past their 90-day limit, are deleted at least a day later, a few at a time whenever a new sign-in starts, so they can remain for some time after that.</li>
    <li>We do not yet delete the other records described above on a fixed schedule. Usage, billing and compliance audit records are kept so that we can bill, prevent abuse and show what the compliance gate decided; account records, the apps that registered themselves and the link between a hashed email address and a paid account are kept while the account or the service exists, except that when the list of registered apps reaches 50,000, registrations that were never used and are more than 30 days old are removed to make room; business records saved by <code>import_booking_url</code> and queued messages (which are marked expired after 48 hours, not deleted) are kept until we are asked to remove them; opt-outs are kept indefinitely because that is what makes them enforceable.</li>
    <li>To have your records deleted, email <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>; we respond within 30 days. Billing and tax records can only be deleted where the law allows it.</li>
  </ul>

  <h2>9. Your rights</h2>
  <p>You may ask for access to, correction, deletion, restriction or portability of your data. Residents of the EU and UK may also complain to their supervisory authority. We do not sell personal information, including for California residents. Email <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>; we respond within 30 days.</p>

  <h2>10. Children</h2>
  <p>The service is for adults (see the Terms of Service) and is not directed at children. We do not knowingly collect data from them.</p>

  <h2>11. Changes</h2>
  <p>We will update this page when our practices change and adjust the date above. Material changes will be noted on this page.</p>
</article>
"""
    return page("Privacy Policy", body, active="privacy",
                description=f"{BRAND} privacy policy: what is collected, who receives it, where it is processed and how long it is kept.")


# ---------------------------------------------------------------------------
# Refund Policy
# ---------------------------------------------------------------------------

def render_refund() -> str:
    body = f"""
<article class="legal">
  <h1>Refund Policy</h1>
  <p class="updated">Last updated: 29 April 2026.</p>

  <h2>1. The promise</h2>
  <p>If the Service was unavailable for &gt; 24 consecutive hours during
  your billing month, or if you were charged through our error, we
  refund the affected charge in full.</p>

  <h2>2. 14-day satisfaction window (credit packages)</h2>
  <p>For credit packages (Starter, Growth, Scale) you may request a full
  refund within <strong>14 days</strong> of purchase, provided fewer than
  100 credits have been spent. Unspent credits never expire and remain
  usable indefinitely.</p>
  <p>We do not sell subscriptions. Credits are the only thing that can be
  purchased, and this section is the term that covers them.</p>

  <h2>3. Usage charges</h2>
  <p>Credits are consumed when an operation completes and are non-refundable
  once spent, except in the cases listed in section 1. Read operations are
  free and consume nothing.</p>
  <p>Payments settled on-chain via x402 are final and cannot be reversed by
  us; section 1 applies only to card payments taken through Polar.</p>

  <h2>4. Free tier</h2>
  <p>The free tier (100 gated operations per day with a verified key) is
  provided without charge; nothing to refund. {{n_no_key}} of the {{n_tools}}
  tools need no key at all.</p>

  <h2>5. How to request a refund</h2>
  <ol>
    <li>Email <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a> with
        your account email and the charge ID.</li>
    <li>We respond within 5 business days.</li>
    <li>Approved refunds reach your card or bank within 7&ndash;10 business
        days &mdash; the timing depends on your card issuer.</li>
  </ol>

  <h2>6. Chargebacks</h2>
  <p>If you dispute a charge with your bank without contacting us first,
  we may suspend the account pending resolution. Please email us first &mdash;
  it is faster.</p>

  <h2>7. Cancellation</h2>
  <p>There is nothing to cancel. We do not sell subscriptions and we do not
  bill on a recurring basis, so there is no billing period to end and no
  renewal to stop. Credits you have bought stay on your account and do not
  expire. If you simply stop calling us, you are never charged again.</p>
  <p style="color:var(--text-muted);font-size:.9rem">This section used to
  describe cancelling at the end of a billing period and forfeiting the
  unused part of a month. That was left over from a subscription we retired,
  and it contradicted section 2 of this same document - in the page a
  customer reads immediately before paying.</p>

  <h2>8. Contact</h2>
  <p>{LEGAL_ENTITY}<br>
  Email: <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a></p>
</article>
"""
    return page("Refund Policy", body, active="refund",
                description=f"{BRAND} refund policy. 14-day window on credit packages, full refund for outages over 24h.")
