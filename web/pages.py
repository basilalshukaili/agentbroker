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
# Credit COUNTS per package are code (billing/packages.py); the USD price of
# each package is set on Polar's dashboard and cannot be imported -- see
# _PACKAGE_USD below, mirrored from docs/PRICING.md / the live pricing page.
from billing.packages import PACKAGE_CREDITS

# The 8 write tools that require a key and spend credits (see
# agent_interface/mcp_server.py::_WRITE_TOOLS_REQUIRING_AUTH -- kept in sync
# with that frozenset by test coverage there, not duplicated here).
_WRITE_OPS_FOR_CHECKOUT = [
    "send_message", "capture_lead", "schedule_appointment",
    "send_transactional_confirmation", "handle_inbound",
    "escalate_to_human", "import_booking_url", "call_business",
]

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
      <p>Marketing to recipients without a verified
         <code class="inline">consent_record_id</code>. Bulk / list-based / drip campaigns.
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
    <div class="card"><h3>{n_keyless} tools &mdash; always free</h3><p><code class="inline">find_business</code>, <code class="inline">verify_business</code>, <code class="inline">check_booking_link</code>, <code class="inline">check_compliance</code>, <code class="inline">preview_cost</code>, <code class="inline">get_status</code>, <code class="inline">get_outcome</code>, <code class="inline">self_test</code>, <code class="inline">get_conversation</code>, <code class="inline">check_quota</code>, <code class="inline">mint_key</code>, <code class="inline">lookup_us_contracts</code>. No key, unmetered.</p></div>
    <div class="card"><h3>{n_quota} tools &mdash; free within a daily quota</h3><p><code class="inline">verify_company_record</code> (GLEIF LEI + SEC EDGAR), <code class="inline">screen_sanctions</code> (OFAC SDN + EU Consolidated + UK Sanctions List), <code class="inline">map_trade_restriction</code>. 500/day with a free key, 100/day anonymous, then $0.02/call.</p></div>
    <div class="card"><h3>{n_needs_key} tools &mdash; need a free key</h3><p><code class="inline">send_message</code>, <code class="inline">capture_lead</code>, <code class="inline">schedule_appointment</code>, <code class="inline">send_transactional_confirmation</code>, <code class="inline">handle_inbound</code>, <code class="inline">escalate_to_human</code>, <code class="inline">import_booking_url</code>, <code class="inline">call_business</code>. 100 write ops/day free, then credits or x402.</p></div>
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
    <div class="card metric"><div class="num">23</div><div class="label">Callable tools</div></div>
    <div class="card metric"><div class="num">12</div><div class="label">Booking platforms supported</div></div>
    <div class="card metric"><div class="num">26</div><div class="label">Jurisdictions with native compliance</div></div>
    <div class="card metric"><div class="num">2</div><div class="label">Payment rails (card via Polar, or x402/USDC)</div></div>
  </div>
  <div class="grid grid-4" style="margin-top:18px;">
    <div class="card metric"><div class="num">7</div><div class="label">Discovery protocols</div></div>
    <div class="card metric"><div class="num">15</div><div class="label">Tools usable with no key at all</div></div>
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
    <div class="card"><span class="tag tag-ok">Compliance</span><h3>Non-bypassable gate</h3><p>Pre-check is the only path to outbound. PII stored as SHA-256 hash only.</p></div>
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
  <code class="inline">get_conversation</code>, <code class="inline">check_quota</code>,
  <code class="inline">mint_key</code>, <code class="inline">lookup_us_contracts</code>.</p>
  <p class="lead">3 premium data tools are free up to a daily quota &mdash;
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
  <p class="lead">The {n_needs_key} write tools require a free email-verified key (100
  write ops/day, no cost) &mdash; beyond that, credits or x402.
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
  <p style="color:var(--text-muted);">Yes. {n_keyless} tools are free, no key, unmetered.
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
    These {{n_needs_key}} tools need a free email-verified key (100 write ops/day, no
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
    <li><strong>Compliance gate.</strong> Every outbound message routes through
        <a href="__ORIGIN__/compliance/check">/compliance/check</a> &mdash; TCPA, GDPR, CASL,
        PDPL across 26 jurisdictions. Marketing without a verified consent_record_id
        is rejected at runtime regardless of how you paid.</li>
    <li><strong>14-day refund</strong> on credit packages. See <a href="/refund">Refund Policy</a>.</li>
    <li><strong>Privacy.</strong> PII (phone, email) is stored as a SHA-256 hash only.
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
  <p class="updated">Last updated: 29 April 2026.</p>

  <h2>1. Acceptance</h2>
  <p>By using {BRAND} (the &ldquo;Service&rdquo;), you agree to these Terms.
  If you do not agree, do not use the Service.</p>

  <h2>2. Service description &amp; scope</h2>
  <p>The Service is a Model Context Protocol (MCP) server that lets AI agents
  discover, verify, communicate with, schedule with, and transact with small
  and mid-sized businesses. It supports five message types (transactional,
  marketing, reminder, follow_up, notification) over WhatsApp, SMS, email and
  voice; live booking execution via Cal.com, with eleven further booking
  platforms recognised for import (Calendly, Doctolib, Booksy, Fresha,
  OpenTable, Setmore, Square, Acuity, Schedulista, Squarespace, BookMyCity);
  and compliance screening against live registries (GLEIF, SEC EDGAR, the OFAC
  SDN list, the EU Consolidated list and the UK Sanctions List). The UN
  consolidated list is NOT screened - it carries no licence permitting
  commercial redistribution, and screen_sanctions says so in its own
  output.</p>
  <p>Every outbound communication routes through a non-bypassable compliance
  gate that enforces TCPA / GDPR / CASL / PDPL rules across 26 jurisdictions.
  Marketing messages require a verified opt-in <code>consent_record_id</code>
  at send time &mdash; without one, the gate rejects the send with a
  structured <code>compliance_violation</code> receipt that never reaches a
  carrier. The gate, not the API surface, is the safety mechanism.</p>
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
        marketing message must reference a valid
        <code>consent_record_id</code> in the consent_store; the compliance
        gate verifies the consent at send time and rejects any send tagged
        <code>marketing</code> that does not.</li>
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
  <p class="updated">Last updated: 29 April 2026.</p>

  <h2>1. Who we are</h2>
  <p>{LEGAL_ENTITY}. Privacy contact:
     <a href="mailto:{PRIVACY_EMAIL}">{PRIVACY_EMAIL}</a>.</p>

  <h2>2. What we collect</h2>
  <ul>
    <li><strong>Account data:</strong> agent identity tokens, billing email,
        company name (if provided).</li>
    <li><strong>Operational metadata:</strong> request timestamps, operation
        names, response codes, latency. Used for billing, abuse prevention,
        and debugging.</li>
    <li><strong>Business data passed by your agent:</strong> phone numbers
        and email addresses are <strong>never stored in plaintext</strong>;
        we keep only an HMAC-SHA256 hash for compliance audit. Free-text
        message bodies are retained for 30 days at most, then deleted.</li>
  </ul>

  <h2>3. What we never collect</h2>
  <ul>
    <li>End-user payment card details &mdash; Polar holds these on its PCI&#8209;DSS
        Level 1 infrastructure; we receive only a redacted token.</li>
    <li>Biometric or special-category data.</li>
    <li>Recordings of voice calls (Vapi-side retention is configurable; we
        do not pull them into our systems).</li>
  </ul>

  <h2>4. How we use the data</h2>
  <ul>
    <li>To deliver the Service you requested.</li>
    <li>To bill you accurately. Every operation returns an itemised receipt
        showing what was charged and why; call <code>preview_cost</code>
        (free) to see the price before you commit.</li>
    <li>To prove compliance with TCPA, GDPR, CASL, PDPL, and equivalents on
        request from a regulator or recipient.</li>
    <li>To detect abuse and enforce the Terms of Service.</li>
  </ul>

  <h2>5. Legal bases (GDPR / UK GDPR)</h2>
  <ul>
    <li><strong>Contract:</strong> processing necessary to deliver the
        Service you signed up for.</li>
    <li><strong>Legitimate interests:</strong> abuse prevention,
        security logging, service improvement.</li>
    <li><strong>Legal obligation:</strong> tax records, regulatory disclosures.</li>
  </ul>

  <h2>6. International transfers</h2>
  <p>Our application is hosted in Frankfurt, Germany (EU). Sub-processors
  may store data in the United States (Twilio, Resend, Polar, Cal.com).
  Where required, we rely on EU Standard Contractual Clauses and the
  Data Privacy Framework.</p>

  <h2>7. Sub-processors</h2>
  <ul>
    <li><strong>Render</strong> (Frankfurt) &mdash; application hosting.</li>
    <li><strong>Twilio</strong> &mdash; SMS / voice carrier.</li>
    <li><strong>Vapi</strong> &mdash; voice AI agent fallback.</li>
    <li><strong>Resend</strong> &mdash; transactional email delivery.</li>
    <li><strong>Cal.com</strong> &mdash; calendar API for booking flows.</li>
    <li><strong>Polar</strong> &mdash; Merchant of Record for billing.</li>
  </ul>

  <h2>8. Retention</h2>
  <ul>
    <li>Operational logs: 90 days.</li>
    <li>Free-text message bodies: 30 days.</li>
    <li>Compliance hashes: 7 years (statute of limitations for TCPA).</li>
    <li>Billing records: 7 years (Omani tax law).</li>
  </ul>

  <h2>9. Your rights</h2>
  <p>You may request access, correction, deletion, restriction, or
  portability of your data. EU/UK residents may also lodge a complaint
  with their supervisory authority. Email
  <a href="mailto:{PRIVACY_EMAIL}">{PRIVACY_EMAIL}</a> &mdash; we respond
  within 30 days.</p>

  <h2>10. CCPA notice (California residents)</h2>
  <p>We do not sell personal information. You may request what we hold
  about you and ask for deletion at the address above.</p>

  <h2>11. Children</h2>
  <p>The Service is not directed at children under 16. We do not
  knowingly collect data from them.</p>

  <h2>12. Changes</h2>
  <p>Material changes to this policy will be announced on this page at
  least 30 days before they take effect.</p>
</article>
"""
    return page("Privacy Policy", body, active="privacy",
                description=f"{BRAND} privacy policy. PII stored as SHA-256 hash only. EU-hosted.")


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
  provided without charge; nothing to refund. Fifteen of the twenty-three
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
