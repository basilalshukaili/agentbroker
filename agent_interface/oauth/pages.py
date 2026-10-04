"""The four pages a person sees while connecting an assistant, and the headers they are served with.

Plain HTML, one stylesheet, one small script (only on the waiting page), no third-party anything: no fonts, no
analytics, no images. Every dynamic value goes through html.escape; the one value that goes into a script
(the sign-in id and poll secret) is serialised as JSON with `<` escaped, so no input can close the tag.

SECURITY HEADERS on every page: a CSP that allows nothing but this page's own nonce'd style and script and
requests back to this origin; `frame-ancestors 'none'` plus X-Frame-Options (these pages must never sit in a
frame - a framed "Confirm" button is how consent is stolen); `no-store` (a page that carried a link or a
poll secret must not be cached or restored by the back button); `Referrer-Policy: no-referrer` (the link in
the address bar must not leak to any later page).

`form-action` is set on the pages whose forms post to this origin and DELIBERATELY NOT on the confirmation
page: its form's reply is a redirect to the app's own return address, and browsers apply `form-action` to
redirects too, so naming 'self' there would block the very hand-back the page exists to perform.

WHAT THE CONSENT TEXT SAYS is limited to what is true of the token that follows: it acts as the account, it
can spend that account's credits, it cannot buy credits, change billing, or see the email address. Wording
reviewed against agent_interface/oauth/tokens.py.
"""
from __future__ import annotations

import html
import json
import secrets
from typing import Optional

from agent_interface.oauth.clients import ClientInfo, is_loopback_uri, redirect_host

SITE = "https://hatchloop.dev"

_CSS = """
:root{color-scheme:light dark;--bg:#f6f7f8;--card:#fff;--ink:#18181b;--mute:#52525b;--line:#e4e4e7;--accent:#34d399;--accent-ink:#052e22;--warn:#92400e;--warn-bg:#fef3c7;--err:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#0e0f11;--card:#17181b;--ink:#f4f4f5;--mute:#a1a1aa;--line:#2a2b30;--accent:#34d399;--accent-ink:#052e22;--warn:#fcd34d;--warn-bg:#3a2e0b;--err:#f87171}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px}
main{max-width:30rem;margin:6vh auto 0;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px 24px}
h1{font-size:1.3rem;line-height:1.3;margin:0 0 .5rem}
p{margin:.5rem 0}.mute{color:var(--mute);font-size:.92rem}
dl{margin:1rem 0;padding:.75rem .9rem;border:1px solid var(--line);border-radius:10px;font-size:.92rem}
dt{color:var(--mute);margin-top:.4rem}dt:first-child{margin-top:0}dd{margin:0;word-break:break-all}
ul{margin:.5rem 0 1rem 1.1rem;padding:0}li{margin:.3rem 0}
label{display:block;font-weight:600;margin:1rem 0 .3rem}
input[type=email]{width:100%;padding:.75rem .8rem;font-size:1rem;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink)}
button{font:inherit;font-weight:700;cursor:pointer;border-radius:999px;padding:.8rem 1.4rem;border:1px solid var(--line);background:transparent;color:var(--ink)}
button.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);width:100%;margin-top:1rem}
button:disabled{opacity:.55;cursor:not-allowed}
.row{display:flex;gap:.6rem;margin-top:1rem}.row button{flex:1}.row button.primary{margin-top:0;width:auto}
.warn{background:var(--warn-bg);color:var(--warn);padding:.6rem .8rem;border-radius:10px;font-size:.92rem;margin:.8rem 0}
.err{color:var(--err);font-weight:600}
a{color:inherit}footer{margin-top:1.4rem;font-size:.82rem;color:var(--mute)}
code{font-size:.9em}
.code{display:inline-block;font:700 1.6rem/1 ui-monospace,Consolas,monospace;letter-spacing:.35em;padding:.5rem .8rem;border:1px dashed var(--line);border-radius:10px}
"""


def new_nonce() -> str:
    return secrets.token_urlsafe(16)


def headers(nonce: str, *, form_action_self: bool = True) -> dict:
    csp = (f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; connect-src 'self'; "
           "base-uri 'none'; frame-ancestors 'none'" + ("; form-action 'self'" if form_action_self else ""))
    return {
        "Content-Security-Policy": csp,
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
    }


def _e(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _shell(title: str, body: str, nonce: str, script: str = "") -> str:
    scr = f'<script nonce="{nonce}">{script}</script>' if script else ""
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{_e(title)}</title><style nonce=\"{nonce}\">{_CSS}</style></head>"
        f"<body><main>{body}"
        f'<footer>HatchLoop AgentBroker &middot; <a href="{SITE}/terms">Terms</a> &middot; '
        f'<a href="{SITE}/privacy">Privacy</a></footer></main>{scr}</body></html>')


def _app_block(client: ClientInfo, redirect_uri: str) -> str:
    rows = []
    if client.kind == "cimd":
        rows.append(f"<dt>App</dt><dd>{_e(client.host)}</dd>")
        if client.name:
            rows.append(f'<dt>Calls itself</dt><dd>{_e(client.name)} <span class="mute">(chosen by the app, not verified)</span></dd>')
    else:
        rows.append(f'<dt>App</dt><dd>{_e(client.name or "An app")} <span class="mute">(name chosen by the app, not verified)</span></dd>')
    rows.append(f"<dt>You will be sent back to</dt><dd>{_e(redirect_host(redirect_uri))}</dd>")
    warn = ""
    if is_loopback_uri(redirect_uri):
        warn = ('<p class="warn">This app runs on your own device. Continue only if you started this '
                'from a program on this computer.</p>')
    elif redirect_uri.split(":", 1)[0] not in ("http", "https"):
        warn = ('<p class="warn">This opens an app installed on your device. Continue only if you '
                'started this from that app.</p>')
    return f"<dl>{''.join(rows)}</dl>{warn}"


def what_it_allows() -> str:
    """The list of what an approved app can do. The credits line is there only while credits exist: with
    CREDITS_ENABLED off nothing can be bought or spent, and the page would be advertising a rail that is
    off (billing/switches.py is the one reader of the switch)."""
    from billing import switches
    credits_li = (
        "<li>Spend credits on your account, if you have bought any. Credits are bought on "
        f'<a href="{SITE}/pricing">hatchloop.dev</a>, never inside the assistant.</li>'
        if switches.credits_enabled() else "")
    return (
        "<ul>"
        "<li>Use AgentBroker's tools as your account - the free tools, and your account's free daily allowance.</li>"
        + credits_li +
        "<li>It does not see your email address, and cannot change your account or billing.</li>"
        "</ul>")


def start_page(client: ClientInfo, redirect_uri: str, rid: str, nonce: str, *, error: str = "",
               poll_secret: str = "") -> str:
    err = f'<p class="err" role="alert">{_e(error)}</p>' if error else ""
    body = (
        f"<h1>Connect {_e(client.label)} to AgentBroker</h1>"
        '<p class="mute">Sign in with your email. There is no password.</p>'
        f"{_app_block(client, redirect_uri)}"
        "<p>If you continue, this app will be able to:</p>"
        f"{what_it_allows()}"
        f"{err}"
        '<form method="post" action="/oauth/authorize/email" autocomplete="on">'
        f'<input type="hidden" name="rid" value="{_e(rid)}">'
        f'<input type="hidden" name="poll_secret" value="{_e(poll_secret)}">'
        '<label for="email">Your email address</label>'
        '<input id="email" name="email" type="email" inputmode="email" autocomplete="email" '
        'required maxlength="254" autofocus placeholder="you@example.com">'
        '<button class="primary" type="submit">Email me a sign-in link</button>'
        '<p class="mute">We send one link. It works once and expires in 15 minutes.</p>'
        "</form>")
    return _shell("Connect to AgentBroker", body, nonce)


_POLL_JS = """
(function(){
  var cfg = JSON.parse(document.getElementById('cfg').textContent);
  var msg = document.getElementById('status');
  var started = Date.now(), delay = 2000, stopped = false;
  function say(t){ msg.textContent = t; }
  function poll(){
    if (stopped) return;
    if (Date.now() - started > cfg.maxMs){ say('This sign-in expired. Go back to the app and choose Connect again.'); return; }
    fetch('/oauth/authorize/poll', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({rid: cfg.rid, poll_secret: cfg.poll}), credentials:'same-origin', cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      if (j.status === 'redirect' && j.location){ stopped = true; say('Confirmed. Returning to the app...'); window.location.replace(j.location); return; }
      if (j.status === 'expired'){ stopped = true; say('This sign-in expired. Go back to the app and choose Connect again.'); return; }
      if (j.status === 'unknown'){ stopped = true; say('This sign-in is no longer valid. Go back to the app and choose Connect again.'); return; }
      delay = Math.min(delay * 1.15, 5000); setTimeout(poll, delay);
    })
    .catch(function(){ delay = Math.min(delay * 1.5, 8000); setTimeout(poll, delay); });
  }
  setTimeout(poll, 1500);
  var btn = document.getElementById('resend');
  if (btn){ btn.disabled = true; setTimeout(function(){ btn.disabled = false; }, 25000); }
})();
"""


def wait_page(rid: str, poll_secret: str, email_hint: str, nonce: str, *, notice: str = "", max_s: int = 900,
              match_code: str = "") -> str:
    cfg = json.dumps({"rid": rid, "poll": poll_secret, "maxMs": max_s * 1000}).replace("<", "\\u003c")
    note = f'<p class="mute">{_e(notice)}</p>' if notice else ""
    body = (
        "<h1>Check your email</h1>"
        f"<p>We sent a sign-in link to <strong>{_e(email_hint)}</strong>.</p>"
        "<p>Open it on this device or any other, review what you are approving, and press "
        "<strong>Confirm</strong>. This page then finishes connecting by itself.</p>"
        f'{note}<p id="status" class="mute" role="status" aria-live="polite">Waiting for you to confirm...</p>'
        + (f'<p class="mute">If the link opens in a different browser or on another device, it will ask for this '
           f'code:</p><p><span class="code" id="match-code">{_e(match_code)}</span></p>'
           '<p class="mute">Only enter it on a page you opened yourself from this sign-in. Never give it to anyone '
           'who asks for it.</p>' if match_code else '') +
        '<details><summary class="mute">Did not arrive, or wrong address?</summary>'
        '<form method="post" action="/oauth/authorize/email">'
        f'<input type="hidden" name="rid" value="{_e(rid)}">'
        f'<input type="hidden" name="poll_secret" value="{_e(poll_secret)}">'
        '<label for="email">Email address</label>'
        '<input id="email" name="email" type="email" inputmode="email" autocomplete="email" required maxlength="254">'
        '<button id="resend" class="primary" type="submit">Send the link again</button>'
        "</form></details>"
        f'<script id="cfg" type="application/json" nonce="{nonce}">{cfg}</script>')
    return _shell("Check your email", body, nonce, _POLL_JS)


def confirm_page(client_label: str, redirect_uri: str, email_hint: str, magic_token: str, nonce: str, *,
                 client: Optional[ClientInfo] = None, ask_code: bool = False, error: str = "") -> str:
    code_block = (
        '<label for="code">Code from the app window</label>'
        '<input id="code" name="code" inputmode="numeric" pattern="[0-9]{4}" maxlength="4" size="6" '
        'autocomplete="one-time-code" required>'
        '<p class="mute">Enter the 4-digit code shown on the page where you started connecting. You do not have '
        'one if you did not start this - press Cancel and nothing will connect.</p>') if ask_code else ""
    err = f'<p class="err" role="alert">{_e(error)}</p>' if error else ""
    app = _app_block(client, redirect_uri) if client else (
        f"<dl><dt>App</dt><dd>{_e(client_label)}</dd>"
        f"<dt>You will be sent back to</dt><dd>{_e(redirect_host(redirect_uri))}</dd></dl>")
    body = (
        "<h1>Confirm sign-in</h1>"
        f"<p><strong>{_e(client_label)}</strong> is asking to use AgentBroker as "
        f"<strong>{_e(email_hint)}</strong>.</p>"
        f"{app}<p>If you confirm, it will be able to:</p>{what_it_allows()}{err}"
        '<form method="post" action="/oauth/verify">'
        f'<input type="hidden" name="t" value="{_e(magic_token)}">'
        f"{code_block}"
        '<div class="row"><button type="submit" name="decision" value="deny" formnovalidate>Cancel</button>'
        '<button type="submit" name="decision" value="approve" class="primary">Confirm and connect</button></div>'
        '<p class="mute">Did not start this? Press Cancel, or close this page: nothing is connected until you confirm.</p>'
        "</form>")
    return _shell("Confirm sign-in", body, nonce)


def message_page(title: str, message: str, nonce: str, *, error: bool = False) -> str:
    cls = ' class="err"' if error else ""
    return _shell(title, f"<h1{cls}>{_e(title)}</h1><p>{_e(message)}</p>", nonce)
