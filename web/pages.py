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
  <h1>The agent-to-business layer.</h1>
  <p class="lead">
    Horizontal MCP server. 12 tools your AI agent can call to find, verify,
    message, and schedule with small businesses worldwide. TCPA, GDPR, CASL,
    and recording-consent compliance built in. Free for any agent up to 100 ops/month.
  </p>
  <div class="cta">
    <a class="btn btn-primary" href="/mcp">Connect via MCP &rarr;</a>
    <a class="btn btn-secondary" href="/docs">API docs</a>
    <a class="btn btn-secondary" href="/.well-known/openai-tools.json">OpenAI tools JSON</a>
  </div>
</header>

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
    <div class="card"><h3>find_business</h3><p>Search SMBs by vertical + location + capability. <code>~$0.01</code></p></div>
    <div class="card"><h3>verify_business</h3><p>Confirm capabilities before you commit. <code>~$0.01</code></p></div>
    <div class="card"><h3>send_message</h3><p>SMS / email / voice with full compliance pre-check. <code>~$0.05</code></p></div>
    <div class="card"><h3>capture_lead</h3><p>Hand a prospect to an SMB with dedup. <code>~$0.02</code></p></div>
    <div class="card"><h3>schedule_appointment</h3><p>Cal.com &rarr; voice fallback. <code>$0.15 + $0.85 success</code></p></div>
    <div class="card"><h3>send_transactional_confirmation</h3><p>Booking / receipt / reminder. <code>~$0.04</code></p></div>
    <div class="card"><h3>handle_inbound</h3><p>Classify customer messages. <code>~$0.03</code></p></div>
    <div class="card"><h3>escalate_to_human</h3><p>Hand off when stuck. <code>$0.10 + $0.40 success</code></p></div>
    <div class="card"><h3>get_status / get_outcome</h3><p>Async polling / final result. <code>~$0.001</code></p></div>
    <div class="card"><h3>preview_cost</h3><p>&plusmn;5% accurate cost estimate. <strong>Free</strong></p></div>
    <div class="card"><h3>self_test</h3><p>Service health check. <strong>Free</strong></p></div>
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
  <h1>Pricing aligned with value.</h1>
  <p class="lead">
    Free tier covers the curious. Paid tiers cover the productive.
    Outcome-based premiums on bookings &mdash; you only pay full price when value lands.
  </p>
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
  <p class="lead">All prices in USD. <code class="inline">preview_cost</code> returns the same numbers programmatically (free).</p>
  <div class="grid grid-3">
    <div class="card"><h3>find_business</h3><p>~$0.01 per call.</p></div>
    <div class="card"><h3>verify_business</h3><p>~$0.01 per call.</p></div>
    <div class="card"><h3>send_message</h3><p>~$0.05 per send (SMS/email/voice).</p></div>
    <div class="card"><h3>capture_lead</h3><p>~$0.02 per dedup&#8209;handoff.</p></div>
    <div class="card"><h3>schedule_appointment</h3><p>$0.15 attempt + $0.85 on confirmed booking.</p></div>
    <div class="card"><h3>send_transactional_confirmation</h3><p>~$0.04 per send.</p></div>
    <div class="card"><h3>handle_inbound</h3><p>~$0.03 per classify.</p></div>
    <div class="card"><h3>escalate_to_human</h3><p>$0.10 attempt + $0.40 on completed handoff.</p></div>
    <div class="card"><h3>get_status / get_outcome</h3><p>~$0.001 per poll.</p></div>
    <div class="card"><h3>preview_cost</h3><p><strong>Free.</strong> Read-only.</p></div>
    <div class="card"><h3>self_test</h3><p><strong>Free.</strong> Read-only.</p></div>
  </div>
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

  <h2>2. Service description</h2>
  <p>The Service is a Model Context Protocol (MCP) server providing AI agents
  with tools to find, verify, message, and schedule appointments with small
  and mid-sized businesses. The Service is offered on an &ldquo;as-is&rdquo;
  basis with no implied warranties.</p>

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
  <ul>
    <li>Sending unsolicited bulk communications (&ldquo;spam&rdquo;).</li>
    <li>Harassing, threatening, or defrauding any person or business.</li>
    <li>Impersonating another person or entity.</li>
    <li>Reverse-engineering, scraping, or rate-abusing the Service.</li>
    <li>Any use that violates applicable law.</li>
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
