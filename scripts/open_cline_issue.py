"""
Open the Cline GitHub issue programmatically.

Per OUTREACH_KIT.md, Cline submission is via a GitHub issue (not email).
We can do this with the existing PAT — opening an issue against an
external public repo only requires the PAT to be authenticated; no
write access on the upstream repo is needed.

Run:
    python scripts/open_cline_issue.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.submit_to_registries import _load_dotenv, _public_url, _repo_url


async def main():
    _load_dotenv()
    pat = os.getenv("GITHUB_PAT", "")
    if not pat:
        print("GITHUB_PAT missing"); return

    public = _public_url()
    repo = _repo_url()

    title = "Add Agent Broker to recommended MCP servers list"
    body = f"""Hi,

Submitting Agent Broker for the Cline recommended-MCP-servers list:

  Endpoint:   {public}/mcp
  Repo:       {repo}
  Smithery:   https://smithery.ai/server/lordbasil147/agent-broker
  Tools:      12 (find_business, verify_business, send_message,
                  capture_lead, schedule_appointment, send_transactional_confirmation,
                  handle_inbound, escalate_to_human, get_status, get_outcome,
                  preview_cost, self_test)
  Free tier:  100 ops/month, any agent, no card.

Gives Cline users the ability to actually book appointments and message
small businesses, not just simulate. Compliance gate is non-bypassable
(TCPA / GDPR / CASL across 22 jurisdictions + INTERNATIONAL fallback).

Live MCP `tools/list`:

```bash
curl -X POST {public}/mcp \\
  -H "Content-Type: application/json" \\
  -d '{{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{{}}}}'
```

Discovery surfaces:
- {public}/.well-known/anthropic-tools.json
- {public}/.well-known/openai-tools.json
- {public}/.well-known/mcp.json
- {public}/llms.txt

Happy to submit a PR if you'd prefer that over an issue.

— Basil
"""

    import httpx
    H = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=30.0, headers=H) as c:
        # Idempotency: don't duplicate if we already opened one
        rs = await c.get(
            "https://api.github.com/search/issues",
            params={
                "q": "repo:cline/cline is:issue in:title \"Agent Broker\"",
                "per_page": 5,
            },
        )
        if rs.status_code == 200:
            for issue in rs.json().get("items", []):
                if "agent broker" in issue.get("title", "").lower():
                    print(f"[skip] issue already exists: {issue['html_url']}")
                    return

        r = await c.post(
            "https://api.github.com/repos/cline/cline/issues",
            json={"title": title, "body": body},
        )
        if r.status_code == 201:
            print(f"[OK] issue opened: {r.json()['html_url']}")
        else:
            print(f"[FAIL] {r.status_code}: {r.text[:300]}")


if __name__ == "__main__":
    asyncio.run(main())
