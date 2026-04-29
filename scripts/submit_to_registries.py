"""
Auto-submit to MCP registries that have programmatic APIs.

Currently submits to:
  - Smithery.ai      (using SMITHERY_API_KEY)
  - Glama.ai         (using GLAMA_API_KEY)

For registries that only accept GitHub PRs (modelcontextprotocol/servers,
punkpeye/awesome-mcp-servers), this script generates the exact `git`
commands the founder runs once.

Run:
    python scripts/submit_to_registries.py
    python scripts/submit_to_registries.py --dry-run
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _load_dotenv():
    p = os.path.join(_PROJECT_ROOT, ".env")
    if not os.path.exists(p):
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            if k and v and k.strip() not in os.environ:
                os.environ[k.strip()] = v


# ---------------------------------------------------------------------------
# Submission payloads (shared across registries)
# ---------------------------------------------------------------------------

def _payload() -> dict:
    base_url = os.getenv("PUBLIC_BASE_URL", "https://agentbroker.qzz.io")
    repo = os.getenv("GITHUB_REPO", "https://github.com/basilalshukaili/agentbroker")
    return {
        "name": "agent-broker",
        "displayName": "Agent Broker",
        "description": (
            "Horizontal agent-to-business action layer. 12 MCP tools to find, "
            "verify, message, and schedule appointments with small businesses "
            "worldwide. Built-in TCPA / GDPR / CASL / 10DLC compliance. "
            "Channel fallback: Cal.com → voice AI → SMS → email → web form. "
            "Free tier for any agent: 100 ops/month."
        ),
        "homepage": base_url,
        "repository": repo,
        "license": "proprietary",
        "categories": ["business", "automation", "scheduling", "messaging"],
        "tags": [
            "mcp", "smb", "business", "scheduling",
            "cal.com", "twilio", "vapi", "voice-ai",
            "compliance", "tcpa", "gdpr", "worldwide",
        ],
        "mcp_endpoint": f"{base_url}/mcp",
        "manifest_url": f"{base_url}/.well-known/mcp.json",
        "openapi_url": f"{base_url}/openapi.yaml",
        "discovery_card_url": f"{base_url}/.well-known/agent-service",
        "llms_txt_url": f"{base_url}/llms.txt",
        "tool_count": 12,
        "transport": "http",
        "auth_header": "X-Agent-Identity",
        "auth_scheme": "bearer",
    }


# ---------------------------------------------------------------------------
# Smithery
# ---------------------------------------------------------------------------

async def submit_smithery(dry_run: bool = False) -> dict:
    api_key = os.getenv("SMITHERY_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "SMITHERY_API_KEY missing"}
    payload = _payload()
    if dry_run:
        return {"ok": True, "dry_run": True, "payload": payload}
    try:
        import httpx
        # Smithery accepts server registration via their API. Endpoint pattern based on
        # their public docs at https://smithery.ai/docs ; specific path may need tweaking
        # if they update — script will surface the response either way.
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://registry.smithery.ai/servers",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            return {
                "ok": r.status_code in (200, 201, 202),
                "status_code": r.status_code,
                "response": r.text[:600],
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Glama
# ---------------------------------------------------------------------------

async def submit_glama(dry_run: bool = False) -> dict:
    api_key = os.getenv("GLAMA_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "GLAMA_API_KEY missing"}
    payload = _payload()
    if dry_run:
        return {"ok": True, "dry_run": True, "payload": payload}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://glama.ai/api/mcp/v1/servers",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            return {
                "ok": r.status_code in (200, 201, 202),
                "status_code": r.status_code,
                "response": r.text[:600],
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# GitHub-based registries — generate exact commands
# ---------------------------------------------------------------------------

def github_registry_instructions() -> str:
    repo = os.getenv("GITHUB_REPO", "https://github.com/basilalshukaili/agentbroker")
    base_url = os.getenv("PUBLIC_BASE_URL", "https://agentbroker.qzz.io")
    return f"""
GitHub-based registries — exact commands to copy-paste:

# 1. modelcontextprotocol/servers (the official MCP server list)

git clone https://github.com/modelcontextprotocol/servers.git mcp-servers-fork
cd mcp-servers-fork
git checkout -b add-agent-broker

# Open README.md, find the alphabetical position in the Community Servers
# section (between any "A..." entries), and insert this single line:
#
#   - **[Agent Broker]({repo})** — Horizontal agent-to-business action layer. 12 tools for finding, verifying, messaging, and scheduling with small/mid businesses worldwide. Built-in TCPA / GDPR / CASL compliance. Free tier: 100 ops/month. MCP endpoint: {base_url}/mcp

git add README.md
git commit -m "Add Agent Broker — agent-to-business action layer"

# Push to your fork (you need to fork the repo on GitHub first via the web UI)
git remote add fork https://github.com/basilalshukaili/servers.git
git push fork add-agent-broker

# Then open https://github.com/modelcontextprotocol/servers/compare/main...basilalshukaili:servers:add-agent-broker
# Click "Create Pull Request". Title: "Add Agent Broker"
# Body is in deploy/registry-submissions/mcp-servers-pr.md


# 2. punkpeye/awesome-mcp-servers (curated list)
# This one's just one line. Same flow:

git clone https://github.com/punkpeye/awesome-mcp-servers.git awesome-mcp-fork
cd awesome-mcp-fork
git checkout -b add-agent-broker

# Open README.md, find the "Business & Productivity" section, insert:
#
#   - [agent-broker]({repo}) — Horizontal agent-to-business action layer. 12 tools for finding, scheduling, messaging SMBs worldwide; built-in TCPA/GDPR/CASL compliance.

git add README.md
git commit -m "Add agent-broker"
git remote add fork https://github.com/basilalshukaili/awesome-mcp-servers.git
git push fork add-agent-broker

# Open https://github.com/punkpeye/awesome-mcp-servers/compare/main...basilalshukaili:awesome-mcp-servers:add-agent-broker
# Create PR.

These two are the ONLY remaining tasks in this script that aren't automated.
Total time: ~5 minutes once you've done it once.
""".strip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(dry_run: bool = False):
    _load_dotenv()
    print("=" * 70)
    print("MCP REGISTRY SUBMISSION")
    print("=" * 70)
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"Base URL: {os.getenv('PUBLIC_BASE_URL', 'unset')}")
    print(f"Repo:     {os.getenv('GITHUB_REPO', 'unset')}")
    print()

    print("1. Smithery.ai")
    res = await submit_smithery(dry_run=dry_run)
    print(f"   {'OK  ' if res.get('ok') else 'FAIL'}  ", end="")
    if dry_run:
        print(f"dry-run; payload prepared ({len(json.dumps(res.get('payload', {})))} bytes)")
    else:
        print(f"http {res.get('status_code', '?')}: {res.get('response', res.get('error', ''))[:200]}")
    print()

    print("2. Glama.ai")
    res = await submit_glama(dry_run=dry_run)
    print(f"   {'OK  ' if res.get('ok') else 'FAIL'}  ", end="")
    if dry_run:
        print(f"dry-run; payload prepared ({len(json.dumps(res.get('payload', {})))} bytes)")
    else:
        print(f"http {res.get('status_code', '?')}: {res.get('response', res.get('error', ''))[:200]}")
    print()

    print("3. GitHub-based registries (manual — exact commands below)")
    print()
    print(github_registry_instructions())


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    asyncio.run(main(dry_run=dry))
