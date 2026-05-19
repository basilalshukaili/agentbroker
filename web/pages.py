"""
Page renderers for the public web UI.

Each function returns a complete HTML string. main.py wires them to routes.
Live metrics (home page) are polled by a tiny vanilla-JS snippet — no React,
no build step, no external scripts.
"""
from __future__ import annotations

from web._partials import page, BRAND, DOMAIN, SUPPORT_EMAIL, PRIVACY_EMAIL, LEGAL_ENTITY


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
  <h1>Your AI agent books appointments on your behalf.</h1>
  <p class="lead">
    Agent Broker is a consumer-side booking tool. <strong>You</strong> ask your AI agent
    (Claude, Cursor, Continue, or any MCP-aware client) to book or follow up on an
    appointment with a specific business you named &mdash; using a booking URL you gave it
    (Cal.com, Calendly, Doctolib, Booksy, Fresha, OpenTable, Setmore, Square, Acuity,
    Schedulista, Squarespace, BookMyCity). The agent completes the transaction. The
    service is <strong>not</strong> a marketing, prospecting, or outbound-campaign platform.
  </p>
  <div class="cta">
    <a class="btn btn-primary" href="/docs">Browse the live API &rarr;</a>
    <a class="btn btn-secondary" href="#how">Connect to Claude</a>
    <a class="btn btn-secondary" href="/pricing">Pricing</a>
  </div>
  <p style="margin-top:36px; font-size:14px; color:var(--text-muted);">
    Example: a consumer types into their AI agent &mdash;<br>
    <code style="display:inline-block; margin-top:8px; padding:8px 14px; background:var(--surface-2); border-radius:6px; color:var(--text);">
      "Book me a haircut at https://cal.com/jane-salon next Tuesday at 3pm"
    </code><br>
    <span style="display:inline-block; margin-top:14px;">
      The agent uses Agent Broker to import the URL and complete the booking. The
      consumer is the originator; the SMB is the consumer's named target.
    </span>
  </p>
</header>

<section class="section" id="scope">
  <h2>What this service is, and what it is not.</h2>
  <div class="grid grid-3">
    <div class="card">
      <h3 style="color:var(--accent);">&#10003; In scope</h3>
      <p>Consumer-initiated booking, rescheduling, cancellation, and follow-up
         on a transaction the consumer named. Booking confirmations to the
         consumer. Replies to messages the SMB sent the consumer first.</p>
    </div>
    <div class="card">
      <h3 style="color:#fca5a5;">&#10007; Out of scope</h3>
      <p>Marketing messages, promotional offers, cold outreach, sales prospecting,
         drip campaigns, list-based messaging, A/B test sends, lead-gen blasts.
         The compliance gate rejects these at the API layer; the public schema
         does not even permit a <code class="inline">marketing</code> message
         type.</p>
    </div>
    <div class="card">
      <h3>How enforcement works</h3>
      <p><a href="/compliance/check">/compliance/check</a> runs before every
         outbound channel call. TCPA, GDPR, CASL, PDPL rules across 22
         jurisdictions. A request that violates the gate returns a structured
         <code class="inline">compliance_violation</code> receipt and never
         reaches a carrier.</p>
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
  <p class="lead">We expose the same 12 tools through every protocol agents speak today.</p>
  <div class="grid grid-3">
    <div class="card">
      <h3>MCP &mdash; Claude Desktop / Cursor / Continue</h3>
      <pre><code>{
  "mcpServers": {
    "agent-broker": {
      "url": "https://smb-broker.onrender.com/mcp",
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
  "https://smb-broker.onrender.com"
  "/.well-known/openai-tools.json"
).json()["tools"]</code></pre>
    </div>
    <div class="card">
      <h3>Anthropic tool_use</h3>
      <pre><code>tools = httpx.get(
  "https://smb-broker.onrender.com"
  "/.well-known/anthropic-tools.json"
).json()["tools"]</code></pre>
    </div>
  </div>
</section>

<section class="section" id="tools">
  <h2>12 operations. One contract. Worldwide.</h2>
  <p class="lead">
    Same OutcomeReceipt schema for every operation. Same compliance gate.
    Same idempotency contract. No surprises.
  </p>
  <div class="grid grid-3">
    <div class="card"><h3>find_business</h3><p>Search verified SMBs by vertical + location + capability. <code>~$0.01</code></p></div>
    <div class="card"><h3>verify_business</h3><p>Confirm an SMB's capabilities before committing. <code>~$0.02</code></p></div>
    <div class="card"><h3>send_message</h3><p>Consumer-initiated transactional message (SMS / email / voice premium). <code>~$0.05</code> base</p></div>
    <div class="card"><h3>capture_lead</h3><p>Structured intake when a consumer asks to be contacted by an SMB. <code>~$0.10</code></p></div>
    <div class="card"><h3>schedule_appointment</h3><p>Direct API or voice fallback. <code>$0.25 attempt + $0.75 on confirmed booking</code></p></div>
    <div class="card"><h3>send_transactional_confirmation</h3><p>Booking / receipt / reminder — reliable delivery, OTP-grade. <code>~$0.03</code></p></div>
    <div class="card"><h3>handle_inbound</h3><p>Classify and route inbound messages an SMB received. <code>~$0.08</code></p></div>
    <div class="card"><h3>escalate_to_human</h3><p>Hand a stuck operation to a human operator. <code>~$0.50</code></p></div>
    <div class="card"><h3>get_status / get_outcome</h3><p>Async polling and final result retrieval. <code>~$0.001</code></p></div>
    <div class="card"><h3>preview_cost</h3><p>&plusmn;5% accurate cost estimate before committing. <strong>Free</strong></p></div>
    <div class="card"><h3>self_test</h3><p>Live capability probe / health check. <strong>Free</strong></p></div>
    <div class="card"><h3>import_booking_url</h3><p>Turn any supported booking URL into a callable SMB. <code>~$0.005</code></p></div>
  </div>
</section>

<section class="section" id="why">
  <h2>Why agents pick us &mdash; measured, not assumed.</h2>
  <p class="lead">
    504 simulated trials &times; 3 agent personas (cost / quality / latency) with
    adversarial tasks where we honestly lose. We do lose those &mdash; and win the rest.
  </p>
  <div class="grid grid-4">
    <div class="card metric"><div class="num">82%</div><div class="label">Aggregate WinRate (simulated)</div></div>
    <div class="card metric"><div class="num">7</div><div class="label">Discovery protocols supported</div></div>
    <div class="card metric"><div class="num">22</div><div class="label">Jurisdictions with native rules</div></div>
    <div class="card metric"><div class="num">$0</div><div class="label">Free tier: 100 ops/month</div></div>
  </div>
</section>

<section class="section">
  <h2>Built right.</h2>
  <div class="grid grid-3">
    <div class="card"><span class="tag tag-ok">Discovery</span><h3>7 agent protocols</h3><p>MCP, OpenAI plugin, OpenAI tools, Anthropic tools, A2A, llms.txt, OpenAPI.</p></div>
    <div class="card"><span class="tag tag-ok">Compliance</span><h3>Non-bypassable gate</h3><p>Pre-check is the only path to outbound. PII stored as SHA-256 hash only.</p></div>
    <div class="card"><span class="tag tag-ok">Reliability</span><h3>Fallback chain</h3><p>direct_api &rarr; voice_ai &rarr; sms &rarr; email &rarr; web_form. Circuit breakers per channel.</p></div>
    <div class="card"><span class="tag tag-ok">Idempotency</span><h3>24h TTL</h3><p>Scoped per <code>(agent_id, operation, key)</code>. Safe to retry.</p></div>
    <div class="card"><span class="tag tag-ok">Async</span><h3>Webhook callbacks</h3><p>HMAC-SHA256 signed. Up to 24h retry with exponential backoff.</p></div>
    <div class="card"><span class="tag tag-ok">Worldwide</span><h3>Jurisdiction-detected</h3><p>22 countries with native rules. International conservative default for the rest.</p></div>
  </div>
</section>
""" + f'<script>{_HOME_LIVE_JS}</script>'
    return page("Home", body, active="home",
                description=f"{BRAND} — horizontal MCP server. 12 tools, 7 discovery protocols, 22 jurisdictions, free tier for AI agents.")


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def render_pricing() -> str:
    body = """
<header class="hero">
  <h1>Pricing for consumer-initiated bookings.</h1>
  <p class="lead">
    You pay per operation an AI agent completes <strong>on behalf of a named
    consumer</strong> &mdash; finding a business the consumer asked about,
    confirming a booking they requested, following up on a quote they
    solicited. Free tier covers low-volume consumer use. Paid tiers fit
    agent platforms serving many users. Marketing, promotional, and
    unsolicited outbound use cases are not sold here at any price.
  </p>
  <div class="cta" style="margin-top:8px;">
    <a class="btn btn-primary" href="/checkout?plan=developer">Get started &mdash; Developer $49/mo</a>
    <a class="btn btn-secondary" href="mailto:""" + SUPPORT_EMAIL + """?subject=Enterprise%20plan%20inquiry">Talk to us &mdash; Enterprise</a>
  </div>
</header>

<section class="section">
  <h2>Plans</h2>
  <table>
    <thead>
      <tr><th>Tier</th><th>Monthly</th><th>Included ops</th><th>Overage</th><th>SLA</th></tr>
    </thead>
    <tbody>
      <tr><td>Free</td><td>$0</td><td>100</td><td>$0.10/op</td><td>none</td></tr>
      <tr><td>Developer</td><td>$49</td><td>10,000</td><td>$0.04/op</td><td>99.0%</td></tr>
      <tr><td>Business</td><td>$499</td><td>100,000</td><td>$0.025/op</td><td>99.5%</td></tr>
      <tr><td>Enterprise</td><td>negotiated</td><td>negotiated</td><td>$0.015/op</td><td>99.9%</td></tr>
    </tbody>
  </table>
  <p style="margin-top:18px;color:var(--text-muted);font-size:14px;">
    <strong>Outcome-based premium:</strong> <code class="inline">schedule_appointment</code> charges
    <code class="inline">$0.15</code> per attempt + <code class="inline">$0.85</code> only on a confirmed booking.
    Cost-per-success is bounded.
  </p>
</section>

<section class="section">
  <h2>Per-operation cost</h2>
  <p class="lead">All prices in USD. <code class="inline">preview_cost</code> returns the same numbers programmatically (free) and is the authoritative source: any drift between this page and <code class="inline">preview_cost</code> is a bug. We price for volume, not extraction &mdash; reads are free, writes are cheap enough that any agent can afford them.</p>
  <div class="grid grid-3">
    <div class="card"><h3>find_business</h3><p><strong>Free</strong> via x402 (anonymous evaluation). Subscription overage: ~$0.01.</p></div>
    <div class="card"><h3>verify_business</h3><p><strong>Free</strong> via x402. Subscription overage: ~$0.02.</p></div>
    <div class="card"><h3>send_message</h3><p>~$0.02 per send (SMS / email). +$0.20 voice premium when fallback hits Vapi.</p></div>
    <div class="card"><h3>capture_lead</h3><p>~$0.05 per dedup&#8209;handoff into the SMB&rsquo;s pipeline.</p></div>
    <div class="card"><h3>schedule_appointment</h3><p>$0.15 attempt + $0.35 only on a confirmed booking. Cost-per-success bounded.</p></div>
    <div class="card"><h3>send_transactional_confirmation</h3><p>~$0.02 per send (OTP / booking confirmation / receipt).</p></div>
    <div class="card"><h3>handle_inbound</h3><p>~$0.03 per classified inbound message.</p></div>
    <div class="card"><h3>escalate_to_human</h3><p>~$0.20 per escalation (full context bundle handoff).</p></div>
    <div class="card"><h3>get_status / get_outcome</h3><p><strong>Free</strong> via x402. Subscription overage: ~$0.001.</p></div>
    <div class="card"><h3>import_booking_url</h3><p>~$0.005 per supported booking URL imported.</p></div>
    <div class="card"><h3>preview_cost</h3><p><strong>Free.</strong> Read-only quote, ±5% accurate.</p></div>
    <div class="card"><h3>self_test</h3><p><strong>Free.</strong> Live capability probe / health check.</p></div>
  </div>
  <p style="margin-top:18px; font-size:14px; color:var(--text-muted);"><strong>Two payment rails:</strong> AI agents pay per-call in USDC on Base (x402 micropayments &mdash; no signup, no account, no card). Human developers can subscribe via Paddle for $49/mo (Developer) or $499/mo (Business) for higher quota + SLA.</p>
</section>

<section class="section">
  <h2>Billing &amp; payments</h2>
  <p class="lead">
    Payments processed by <strong>Paddle</strong> (Merchant of Record).
    Paddle handles VAT/sales tax in 100+ countries on our behalf, so any
    customer worldwide can pay with card, Apple/Google Pay, PayPal, or wire.
  </p>
  <div class="grid grid-3">
    <div class="card"><h3>Free tier</h3><p>No card required. 100 ops/month, any agent.</p></div>
    <div class="card"><h3>Self-serve paid</h3><p>Card via Paddle Checkout. Monthly or annual.</p></div>
    <div class="card"><h3>Enterprise</h3><p>Wire transfer / PO. Email <a href="mailto:""" + SUPPORT_EMAIL + """">""" + SUPPORT_EMAIL + """</a>.</p></div>
  </div>
</section>

<section class="section">
  <h2>FAQ</h2>
  <h3>Do you offer a free tier?</h3>
  <p style="color:var(--text-muted);">Yes. 100 operations per month per agent identity, forever. No card required.</p>
  <h3>Can I change plan at any time?</h3>
  <p style="color:var(--text-muted);">Yes. Upgrades take effect immediately and are prorated. Downgrades take effect at the end of the current billing period.</p>
  <h3>What payment methods do you accept?</h3>
  <p style="color:var(--text-muted);">Cards (Visa, Mastercard, AmEx), Apple Pay, Google Pay, PayPal, and SEPA &mdash; all routed through Paddle. Enterprise plans can pay by wire.</p>
  <h3>Is there a contract?</h3>
  <p style="color:var(--text-muted);">No. Cancel anytime. Enterprise plans use a flexible MSA.</p>
</section>
"""
    return page("Pricing", body, active="pricing",
                description=f"{BRAND} pricing. Free tier 100 ops/month. Paid tiers from $49/month. Outcome-based premiums on bookings.")


# ---------------------------------------------------------------------------
# Checkout — Paddle Merchant-of-Record subscription start
# ---------------------------------------------------------------------------

_PLAN_PRICES = {
    "developer": {"label": "Developer", "monthly_usd": 49, "ops": "10,000"},
    "business":  {"label": "Business",  "monthly_usd": 499, "ops": "100,000"},
}


def render_checkout(plan: str | None) -> str:
    plan_key = (plan or "developer").lower()
    if plan_key not in _PLAN_PRICES:
        plan_key = "developer"
    p = _PLAN_PRICES[plan_key]
    body = f"""
<header class="hero">
  <h1>Subscribe — {p['label']} plan</h1>
  <p class="lead">
    ${p['monthly_usd']} per month. Includes {p['ops']} agent operations. Billed by
    <strong>Paddle</strong> as Merchant of Record &mdash; Paddle handles VAT, sales tax,
    and chargebacks on our behalf. Cancel anytime from the customer portal.
  </p>
</header>

<section class="section">
  <h2>What you are buying</h2>
  <p style="color:var(--text-muted);">
    A monthly subscription to {BRAND} for AI agents acting on behalf of consumers
    who explicitly request a booking, reschedule, or follow-up with a specific
    business. <strong>This is a consumer-side booking tool</strong> &mdash; it is
    not sold for marketing, prospecting, or unsolicited outreach. See our
    <a href="/terms">Terms</a> §6 for the full list of prohibited uses.
  </p>

  <h2 style="margin-top:32px;">How payment works</h2>
  <ol style="color:var(--text-muted);">
    <li>Click <em>Continue to Paddle</em>. The Paddle Checkout overlay opens
        directly from this page.</li>
    <li>Paddle collects payment details on its own PCI-DSS Level 1 infrastructure.
        We never see your card number.</li>
    <li>On success, Paddle returns a signed receipt and provisions your API
        key. The key arrives by email within 60 seconds.</li>
    <li>Recurring charges run monthly until you cancel. Refunds follow our
        <a href="/refund">Refund Policy</a>.</li>
  </ol>

  <div class="cta" style="margin-top:28px;">
    <button class="btn btn-primary" id="paddle-checkout-btn" disabled>
      Continue to Paddle &rarr;
    </button>
    <a class="btn btn-secondary" href="mailto:{SUPPORT_EMAIL}?subject=Subscribe%20to%20{p['label']}%20plan">
      Email {SUPPORT_EMAIL} to subscribe
    </a>
  </div>

  <p style="margin-top:18px;font-size:13px;color:var(--text-muted);">
    The Paddle Checkout button activates after our Paddle Vendor account
    completes review. While review is pending, email
    <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a> with the plan name and
    we will issue a manual invoice + activate your account within one business
    day.
  </p>
</section>

<section class="section">
  <h2>Your rights</h2>
  <ul style="color:var(--text-muted);">
    <li><strong>14-day refund</strong> on the initial subscription if you have
        used fewer than 100 paid operations. See <a href="/refund">Refund Policy</a>.</li>
    <li><strong>Cancel anytime</strong> from the customer portal Paddle emails
        you after first charge.</li>
    <li><strong>Privacy.</strong> We never store your card number. Paddle does;
        we only receive a redacted token. See <a href="/privacy">Privacy Policy</a>.</li>
    <li><strong>Governing law:</strong> Sultanate of Oman. EU/UK/CA consumer
        statutory rights are preserved. See <a href="/terms">Terms</a>.</li>
  </ul>
</section>

<script src="https://cdn.paddle.com/paddle/v2/paddle.js"></script>
<script>
  // Paddle Checkout will be enabled once the Paddle Vendor account is approved
  // and a price_id is configured. Until then the button is disabled and the
  // mailto fallback is the live subscription path.
  (function () {{
    var vendorId = ""; // set after Paddle approval
    var priceId  = ""; // set per plan after Paddle product is created
    if (!window.Paddle || !vendorId || !priceId) return;
    Paddle.Setup({{ token: vendorId }});
    var btn = document.getElementById("paddle-checkout-btn");
    if (!btn) return;
    btn.disabled = false;
    btn.addEventListener("click", function () {{
      Paddle.Checkout.open({{ items: [{{ priceId: priceId, quantity: 1 }}] }});
    }});
  }})();
</script>
"""
    return page(f"Subscribe — {p['label']}", body, active="pricing",
                description=f"Subscribe to the {p['label']} plan. Paid by Paddle as Merchant of Record. Consumer-initiated booking transactions only.")


# ---------------------------------------------------------------------------
# Terms of Service
# ---------------------------------------------------------------------------

def render_terms() -> str:
    body = f"""
<article class="legal">
  <h1>Terms of Service</h1>
  <p class="updated">Last updated: 29 April 2026.</p>

  <h2>1. Acceptance</h2>
  <p>By using {BRAND} (the &ldquo;Service&rdquo;), you agree to these Terms.
  If you do not agree, do not use the Service.</p>

  <h2>2. Service description &amp; scope</h2>
  <p>The Service is a Model Context Protocol (MCP) server that lets an
  AI agent complete <strong>consumer-initiated</strong> booking and
  transactional tasks with small and mid-sized businesses on behalf of
  an identifiable end-user (the &ldquo;Consumer&rdquo;). Every operation
  the Service performs must trace back to a specific Consumer request
  naming a specific business.</p>
  <p>The Service is <strong>not</strong> a marketing platform, lead-list
  vendor, prospecting tool, cold-outreach service, sequencer, autoresponder,
  campaign sender, A/B testing tool, or any other form of unsolicited
  communication infrastructure. It may not be used to contact recipients
  who have not initiated or pre-authorized the communication.</p>
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
    <li><strong>Marketing or promotional messaging</strong> &mdash; advertising
        a product, service, offer, discount, or event to a recipient. The
        Service's compliance gate rejects messages tagged
        <code>marketing</code> and the public API schema does not even permit
        that value.</li>
    <li><strong>Unsolicited outbound communication of any kind</strong>
        &mdash; cold SMS, cold email, cold voice calls, mass outreach, drip
        sequences, cadenced follow-ups not requested by the recipient,
        list-based campaigns, A/B test sends, or contacting any recipient
        the Consumer has not specifically named in their request.</li>
    <li><strong>Sales prospecting or lead generation</strong> &mdash; using
        the Service to find businesses or individuals for the purpose of
        pitching them, regardless of the channel used to make the pitch.</li>
    <li><strong>Acting on behalf of a third party rather than an end-Consumer</strong>
        &mdash; for example, an SMB cannot use the Service to message its own
        prospects; only a Consumer who has independently chosen to engage that
        SMB may direct the agent to communicate.</li>
    <li>Bulk communications (&ldquo;spam&rdquo;) by any definition.</li>
    <li>Harassing, threatening, or defrauding any person or business.</li>
    <li>Impersonating another person or entity, including misrepresenting the
        identity of the Consumer on whose behalf the agent acts.</li>
    <li>Reverse-engineering, scraping, or rate-abusing the Service.</li>
    <li>Circumventing or attempting to circumvent the compliance pre-check.</li>
    <li>Any use that violates applicable telecommunications, privacy, or
        consumer-protection law (including TCPA, CAN-SPAM, GDPR, CASL, PDPL).</li>
  </ul>

  <h2>7. Intellectual property</h2>
  <p>The Service, including code, manifests, and discovery surfaces, is
  owned by {LEGAL_ENTITY}. The 12 operations and OutcomeReceipt schema are
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
    <li>End-user payment card details &mdash; Paddle holds these on its PCI&#8209;DSS
        Level 1 infrastructure; we receive only a redacted token.</li>
    <li>Biometric or special-category data.</li>
    <li>Recordings of voice calls (Vapi-side retention is configurable; we
        do not pull them into our systems).</li>
  </ul>

  <h2>4. How we use the data</h2>
  <ul>
    <li>To deliver the Service you requested.</li>
    <li>To bill you accurately and produce signed receipts.</li>
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
  may store data in the United States (Twilio, Resend, Paddle, Cal.com).
  Where required, we rely on EU Standard Contractual Clauses and the
  Data Privacy Framework.</p>

  <h2>7. Sub-processors</h2>
  <ul>
    <li><strong>Render</strong> (Frankfurt) &mdash; application hosting.</li>
    <li><strong>Twilio</strong> &mdash; SMS / voice carrier.</li>
    <li><strong>Vapi</strong> &mdash; voice AI agent fallback.</li>
    <li><strong>Resend</strong> &mdash; transactional email delivery.</li>
    <li><strong>Cal.com</strong> &mdash; calendar API for booking flows.</li>
    <li><strong>Paddle</strong> &mdash; Merchant of Record for billing.</li>
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

  <h2>2. 14-day satisfaction window (subscriptions)</h2>
  <p>For monthly or annual subscription tiers (Developer, Business),
  you may request a full refund within <strong>14 days</strong> of the
  initial purchase, provided you have used fewer than 100 paid operations
  during that window.</p>

  <h2>3. Usage charges</h2>
  <p>Per-operation usage charges (overage, success-based premiums on
  <code>schedule_appointment</code> and <code>escalate_to_human</code>) are
  consumed when the operation completes and are non-refundable, except in
  the cases listed in section 1.</p>

  <h2>4. Free tier</h2>
  <p>The free tier (100 ops/month) is provided without charge; nothing to
  refund.</p>

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
  <p>You may cancel any time from your account dashboard. Cancellation
  takes effect at the end of the current billing period; we do not
  prorate the unused portion of a cancelled month, but we do not bill
  you again.</p>

  <h2>8. Contact</h2>
  <p>{LEGAL_ENTITY}<br>
  Email: <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a></p>
</article>
"""
    return page("Refund Policy", body, active="refund",
                description=f"{BRAND} refund policy. 14-day window on subscriptions, full refund for outages over 24h.")
