"""
Send the six outreach emails via Gmail SMTP using an app password.

Sends from the founder's gmail inbox, one message per recipient cluster:

  1. Anthropic developer relations
  2. Cursor MCP catalog
  3. Continue.dev
  4. Perplexity API hub
  5. You.com partnerships
  6. (Cline goes via GitHub issue, not email — see scripts/open_cline_issue.py)

Each message body is the canonical template from OUTREACH_KIT.md.
Idempotent: a tiny ledger at reports/outreach_log.txt records what's
been sent so re-running this script doesn't double-send.
"""
from __future__ import annotations

import os
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# load .env so we pick up GMAIL_USER / GMAIL_APP_PASSWORD
from scripts.submit_to_registries import _load_dotenv, _public_url, _repo_url
_load_dotenv()

GMAIL_USER = os.getenv("GMAIL_USER", "basilalshukaili@gmail.com")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
SENDER_NAME = os.getenv("GMAIL_SENDER_NAME", "Basil Al Shukaili")

PUBLIC = _public_url()
REPO = _repo_url()
LEDGER = _PROJECT_ROOT / "reports" / "outreach_log.txt"


def _connect_snippet() -> str:
    return (
        '{\n'
        '    "mcpServers": {\n'
        '      "agent-broker": {\n'
        f'        "url": "{PUBLIC}/mcp"\n'
        '      }\n'
        '    }\n'
        '  }'
    )


CAMPAIGNS: list[dict] = [
    {
        "id": "anthropic",
        "to": ["developers@anthropic.com", "mcp@anthropic.com"],
        "subject": "MCP server: 12-tool agent-to-business action layer (live, free tier)",
        "body": f"""Hi team,

I built an MCP server that gives Claude 12 tools to find, verify,
message, and schedule appointments with small businesses worldwide.
Live, free for any agent up to 100 ops/month, full TCPA / GDPR / CASL
compliance gate built in.

Live MCP endpoint:    {PUBLIC}/mcp
Repo (open code):     {REPO}
Smithery listing:     https://smithery.ai/server/lordbasil147/agent-broker
Anthropic-tools JSON: {PUBLIC}/.well-known/anthropic-tools.json

Test it in 30 seconds — drop this into Claude Desktop's
claude_desktop_config.json:

  {_connect_snippet()}

Happy to demo on a call or hand over a test agent identity.

— Basil Al Shukaili
   Sultanate of Oman
""",
    },
    {
        "id": "cursor",
        "to": ["hi@cursor.com", "support@cursor.com"],
        "subject": "Submission: agent-broker MCP server for Cursor catalog",
        "body": f"""Hi,

Submitting Agent Broker for the Cursor MCP catalog.

Endpoint: {PUBLIC}/mcp
Repo:     {REPO}
12 tools: find / verify / message / schedule across small businesses worldwide.
Free tier 100 ops/month. No auth required for read-only ops.

Connection JSON:

  {_connect_snippet()}

Smithery already lists us. Glama auto-indexes our `mcp-server` topic.
Adding to the Cursor catalog completes the major IDE clients.

— Basil Al Shukaili
""",
    },
    {
        "id": "continue",
        "to": ["hello@continue.dev"],
        "subject": "MCP server submission: agent-broker (12 tools, free tier)",
        "body": f"""Hi,

Submitting Agent Broker for the Continue.dev MCP catalog.

Endpoint: {PUBLIC}/mcp
Repo:     {REPO}
12 tools: find / verify / message / schedule across small businesses worldwide.
Free tier 100 ops/month. No auth required for read-only ops.

Connection JSON:

  {_connect_snippet()}

Already listed on Smithery. Adding Continue completes the major IDE
clients alongside Claude Desktop and Cursor.

— Basil Al Shukaili
""",
    },
    {
        "id": "perplexity",
        "to": ["api@perplexity.ai"],
        "subject": "Tool submission: agent-broker MCP for Perplexity Tools API",
        "body": f"""Hi,

Submitting Agent Broker as a tool target for Perplexity's tool-use API.

Endpoint:               {PUBLIC}/mcp
OpenAI-tools format:    {PUBLIC}/.well-known/openai-tools.json
12 tools, free tier 100 ops/month, MoR billing via Paddle for paid traffic.

If Perplexity has a tools-marketplace, please point me to the submission
form. Otherwise treating this as a notice that we exist and would be a
useful addition for any Perplexity user asking "find me a salon in Tokyo
that does X" or similar long-tail SMB queries.

— Basil Al Shukaili
""",
    },
    {
        "id": "you_com",
        "to": ["partnerships@you.com"],
        "subject": "Agent Broker — MCP tool for you.com agents",
        "body": f"""Hi,

Submitting Agent Broker as a tool target for You.com agents.

Endpoint:               {PUBLIC}/mcp
OpenAI-tools format:    {PUBLIC}/.well-known/openai-tools.json
12 tools, free tier 100 ops/month.

If You.com has a tools-marketplace, please point me to the submission
form. Otherwise treating this as a notice that we exist and would be a
useful addition for any agent that needs to talk to small businesses
on the user's behalf.

— Basil Al Shukaili
""",
    },
]


def _already_sent(campaign_id: str) -> bool:
    if not LEDGER.exists():
        return False
    return campaign_id in LEDGER.read_text(encoding="utf-8")


def _record(campaign_id: str, recipients: list[str], status: str, info: str = "") -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\t{status}\t{campaign_id}\t{','.join(recipients)}\t{info}\n"
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(line)


def main(*, dry_run: bool = False) -> None:
    if not GMAIL_APP_PASSWORD:
        print("GMAIL_APP_PASSWORD missing in .env — cannot send.")
        sys.exit(1)

    print(f"Sender: {GMAIL_USER}")
    print(f"Public URL referenced: {PUBLIC}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print()

    if dry_run:
        for c in CAMPAIGNS:
            print(f"[DRY] {c['id']:12s} -> {', '.join(c['to'])}")
            print(f"       subject: {c['subject']}")
            print(f"       body: {len(c['body'])} bytes")
        return

    ctx = ssl.create_default_context()
    sent, skipped, failed = 0, 0, 0

    # Strip spaces from app password (Google formats them with spaces)
    app_password = GMAIL_APP_PASSWORD.replace(" ", "")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as smtp:
        smtp.login(GMAIL_USER, app_password)
        for c in CAMPAIGNS:
            if _already_sent(c["id"]):
                print(f"[skip] {c['id']:12s} — already sent (in ledger)")
                skipped += 1
                continue
            msg = EmailMessage()
            msg["From"] = f"{SENDER_NAME} <{GMAIL_USER}>"
            msg["To"] = ", ".join(c["to"])
            msg["Subject"] = c["subject"]
            msg["Reply-To"] = GMAIL_USER
            msg.set_content(c["body"])
            try:
                resp = smtp.send_message(msg)
                # send_message returns dict of failed recipients (empty = all good)
                if resp:
                    print(f"[partial] {c['id']:12s} — some rejected: {resp}")
                    _record(c["id"], c["to"], "partial", str(resp)[:200])
                    failed += 1
                else:
                    print(f"[OK]   {c['id']:12s} -> {', '.join(c['to'])}")
                    _record(c["id"], c["to"], "ok")
                    sent += 1
            except Exception as e:
                print(f"[FAIL] {c['id']:12s} — {e}")
                _record(c["id"], c["to"], "fail", str(e)[:200])
                failed += 1

    print()
    print(f"Summary: sent={sent}  skipped={skipped}  failed={failed}")
    print(f"Ledger:  {LEDGER}")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
