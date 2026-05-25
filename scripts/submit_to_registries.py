"""
Auto-submit to MCP registries.

Distribution strategy (verified against each registry's docs in 2026-Q2):

  Smithery       — real REST API; PUT /servers/{namespace}/{server}/releases.
                   Multipart form, payload field is JSON {"type":"external","url":...}.
  Glama          — auto-crawls GitHub repos with the `mcp-server` topic.
                   No public REST submission; we ensure the topic is set
                   via the GitHub PAT and verify they index us by polling
                   their public listing.
  modelcontext-  — README.md PR. We open it programmatically with the
    protocol/        GitHub PAT (fork → branch → edit → PR).
    servers
  punkpeye/      — same flow, different file location.
    awesome-mcp-
    servers
  apis.guru      — public OpenAPI catalogue. They accept PRs against
                   APIs-guru/openapi-directory. We generate the exact
                   patch the human pastes once; their bot validates.
  MCP Hub        — open directory. Auto-listed when a server publishes
                   a discoverable mcp.json + llms.txt; we already do.

Run:
    python scripts/submit_to_registries.py            # live
    python scripts/submit_to_registries.py --dry-run  # preview only
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
# Submission payloads
# ---------------------------------------------------------------------------

def _public_url() -> str:
    return os.getenv("PUBLIC_BASE_URL", "https://agent-broker-edge.basil-agent.workers.dev").rstrip("/")


def _repo_url() -> str:
    return os.getenv("GITHUB_REPO", "https://github.com/basilalshukaili/agentbroker").rstrip("/")


def _repo_slug() -> tuple[str, str]:
    """Return (owner, repo) from GITHUB_REPO env or default."""
    url = _repo_url()
    parts = url.replace("https://github.com/", "").split("/")
    return parts[0], parts[1] if len(parts) > 1 else "agentbroker"


# ---------------------------------------------------------------------------
# 1) Smithery — real API
# ---------------------------------------------------------------------------

async def submit_smithery(dry_run: bool = False) -> dict:
    """
    Two-step Smithery flow:
      1. PUT /servers/{namespace}%2F{server}        — idempotently create the server
         body: {"displayName": "...", "description": "..."}
      2. PUT /servers/{namespace}%2F{server}/releases — publish a release
         multipart/form-data; form field "payload" = JSON {"type":"external","url":...}

    The first call has to succeed before the second one will accept anything;
    this was the reason for the original 400 ('expected string, received undefined').
    """
    api_key = os.getenv("SMITHERY_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "SMITHERY_API_KEY missing"}

    namespace = os.getenv("SMITHERY_NAMESPACE", "basilalshukaili")
    server = os.getenv("SMITHERY_SERVER_SLUG", "agent-broker")
    qualified = f"{namespace}/{server}"
    encoded = qualified.replace("/", "%2F")

    base_url = _public_url()
    create_body = {
        "displayName": "Agent Broker",
        "description": (
            "Horizontal agent-to-business action layer. 14 MCP tools for AI agents "
            "to find, verify, message, and book appointments with small businesses "
            "worldwide. Agents pay per call in USDC on Base via x402 — no signup, no "
            "API key (reads free, writes paid). Built-in compliance gate "
            "(TCPA / GDPR / CASL / PDPL across 22 jurisdictions)."
        ),
    }
    release_payload_json = json.dumps({
        "type": "external",
        "upstreamUrl": f"{base_url}/mcp",
    })

    if dry_run:
        return {
            "ok": True, "dry_run": True,
            "step_1": f"PUT https://api.smithery.ai/servers/{encoded}  body={create_body}",
            "step_2": f"PUT https://api.smithery.ai/servers/{encoded}/releases  payload={release_payload_json}",
        }

    try:
        import httpx
        async with httpx.AsyncClient(timeout=20.0) as client:
            # Step 1 — create / upsert server entry
            r1 = await client.put(
                f"https://api.smithery.ai/servers/{encoded}",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=create_body,
            )
            if r1.status_code not in (200, 201, 204):
                return {
                    "ok": False, "step": "create",
                    "status_code": r1.status_code,
                    "response": r1.text[:400],
                    "qualified_name": qualified,
                }

            # Step 2 — publish release pointing at our hosted /mcp
            r2 = await client.put(
                f"https://api.smithery.ai/servers/{encoded}/releases",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"payload": (None, release_payload_json, "application/json")},
            )
            return {
                "ok": r2.status_code in (200, 201, 202),
                "step": "release",
                "status_code": r2.status_code,
                "response": r2.text[:400],
                "qualified_name": qualified,
                "listing_url": f"https://smithery.ai/server/{qualified}",
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 2) GitHub — set topics so Glama (and others) auto-index us
# ---------------------------------------------------------------------------

async def set_github_topics(dry_run: bool = False) -> dict:
    """
    Glama and GitHub-search-based aggregators index repos with topic 'mcp-server'.
    Set the canonical topic set in one PUT call. Idempotent.
    """
    pat = os.getenv("GITHUB_PAT", "")
    if not pat:
        return {"ok": False, "error": "GITHUB_PAT missing"}

    owner, repo = _repo_slug()
    topics = [
        "mcp",
        "mcp-server",
        "model-context-protocol",
        "ai-agents",
        "anthropic-tools",
        "openai-plugin",
        "claude-mcp",
        "agent-tools",
        "small-business",
        "scheduling",
        "compliance",
    ]

    if dry_run:
        return {"ok": True, "dry_run": True, "owner": owner, "repo": repo, "topics": topics}

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.put(
                f"https://api.github.com/repos/{owner}/{repo}/topics",
                headers={
                    "Authorization": f"Bearer {pat}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"names": topics},
            )
            return {
                "ok": r.status_code == 200,
                "status_code": r.status_code,
                "topics": r.json().get("names", []) if r.headers.get("content-type", "").startswith("application/json") else None,
                "response": r.text[:300],
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 3) modelcontextprotocol/servers — open a real PR via GitHub API
# ---------------------------------------------------------------------------

async def submit_mcp_servers_pr(dry_run: bool = False) -> dict:
    """
    Programmatically open a PR against modelcontextprotocol/servers.

    A *fine-grained* GitHub PAT is scoped to specific repos and CAN'T fork
    external ones (403). Two ways forward:

      (a) the user pre-forks via the browser (one click on
          https://github.com/modelcontextprotocol/servers/fork) and we then
          have permission against the fork — script auto-resumes from there.
      (b) the user issues a *classic* PAT with `public_repo` scope.

    Either way: this function is idempotent. If the fork exists, it skips
    creation. If the branch exists, it fast-forwards. If the PR is open,
    it returns the URL. Run repeatedly without harm.
    """
    pat = os.getenv("GITHUB_PAT", "")
    if not pat:
        return {"ok": False, "error": "GITHUB_PAT missing"}

    upstream_owner = "modelcontextprotocol"
    upstream_repo = "servers"
    branch = "add-agent-broker"

    owner, _ = _repo_slug()
    base_url = _public_url()
    repo_url = _repo_url()
    line = (
        f"- **[Agent Broker]({repo_url})** — "
        "Horizontal agent-to-business action layer. 14 MCP tools for AI agents to "
        "find, verify, message, and book appointments with small businesses worldwide. "
        "Agents pay per call in USDC on Base via x402 (no signup; reads free, writes "
        f"paid). Built-in TCPA / GDPR / CASL compliance gate. Endpoint: {base_url}/mcp"
    )

    if dry_run:
        return {
            "ok": True, "dry_run": True,
            "would_fork": f"{upstream_owner}/{upstream_repo} -> {owner}/{upstream_repo}",
            "branch": branch,
            "insert_line": line,
        }

    try:
        import httpx, base64
        H = {
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=30.0, headers=H) as c:
            # 1) check if a fork already exists; if so, skip the fork API call
            #    (fine-grained PATs can't call /forks but CAN read existing forks)
            rf = await c.get(f"https://api.github.com/repos/{owner}/{upstream_repo}")
            if rf.status_code == 404:
                # try to fork; this works for classic PATs only
                r = await c.post(f"https://api.github.com/repos/{upstream_owner}/{upstream_repo}/forks")
                if r.status_code not in (200, 202):
                    return {
                        "ok": False, "step": "fork",
                        "status_code": r.status_code,
                        "response": r.text[:200],
                        "manual_fix": (
                            f"Open https://github.com/{upstream_owner}/{upstream_repo}/fork "
                            "in a browser, click 'Create fork' once, then re-run this script. "
                            "Fine-grained PATs cannot fork external repos via API."
                        ),
                    }
                # poll for fork readiness
                await asyncio.sleep(2)
                for _ in range(10):
                    rf = await c.get(f"https://api.github.com/repos/{owner}/{upstream_repo}")
                    if rf.status_code == 200:
                        break
                    await asyncio.sleep(2)
                else:
                    return {"ok": False, "step": "fork-ready"}

            if rf.status_code != 200:
                return {"ok": False, "step": "fork-read", "status_code": rf.status_code, "response": rf.text[:200]}
            default_branch = rf.json().get("default_branch", "main")

            # 2) get the SHA the upstream default branch points at, so we branch from there
            r2 = await c.get(f"https://api.github.com/repos/{upstream_owner}/{upstream_repo}/git/refs/heads/{default_branch}")
            if r2.status_code != 200:
                return {"ok": False, "step": "upstream-ref", "response": r2.text[:300]}
            base_sha = r2.json()["object"]["sha"]

            # 3) sync the fork's default branch to upstream so our PR base is current
            await c.patch(
                f"https://api.github.com/repos/{owner}/{upstream_repo}/git/refs/heads/{default_branch}",
                json={"sha": base_sha, "force": True},
            )

            # 4) create or update the working branch on the fork
            r3 = await c.post(
                f"https://api.github.com/repos/{owner}/{upstream_repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            )
            if r3.status_code not in (201, 422):  # 422 = already exists; we'll force-update next
                return {"ok": False, "step": "create-branch", "status_code": r3.status_code, "response": r3.text[:300]}
            if r3.status_code == 422:
                # branch exists — fast-forward it to base_sha
                await c.patch(
                    f"https://api.github.com/repos/{owner}/{upstream_repo}/git/refs/heads/{branch}",
                    json={"sha": base_sha, "force": True},
                )

            # 5) read README.md from upstream
            r4 = await c.get(f"https://api.github.com/repos/{upstream_owner}/{upstream_repo}/contents/README.md?ref={default_branch}")
            if r4.status_code != 200:
                return {"ok": False, "step": "read-readme", "response": r4.text[:300]}
            readme_data = r4.json()
            readme_text = base64.b64decode(readme_data["content"]).decode("utf-8")

            # 6) splice our entry after the "Community Servers" header (if present)
            #    fallback: append before the first "## " heading after the title
            marker = "## 🌎 Community Servers"
            alt = "## Community Servers"
            target = marker if marker in readme_text else (alt if alt in readme_text else None)
            if target is None:
                # safety: append at end
                new_readme = readme_text.rstrip() + "\n\n" + line + "\n"
            else:
                idx = readme_text.find(target)
                head = readme_text[:idx + len(target)]
                tail = readme_text[idx + len(target):]
                # insert after the immediate following blank line
                next_nl = tail.find("\n\n")
                insert_at = next_nl + 2 if next_nl != -1 else 0
                new_readme = head + tail[:insert_at] + line + "\n" + tail[insert_at:]

            if line in readme_text:
                return {"ok": True, "note": "already present in upstream README", "no_op": True}

            # 7) commit on the fork branch
            r5 = await c.put(
                f"https://api.github.com/repos/{owner}/{upstream_repo}/contents/README.md",
                json={
                    "message": "Add Agent Broker to Community Servers",
                    "content": base64.b64encode(new_readme.encode("utf-8")).decode("ascii"),
                    "sha": readme_data["sha"],
                    "branch": branch,
                },
            )
            if r5.status_code not in (200, 201):
                return {"ok": False, "step": "commit", "status_code": r5.status_code, "response": r5.text[:300]}

            # 8) open PR upstream
            r6 = await c.post(
                f"https://api.github.com/repos/{upstream_owner}/{upstream_repo}/pulls",
                json={
                    "title": "Add Agent Broker — agent-to-business action layer",
                    "head": f"{owner}:{branch}",
                    "base": default_branch,
                    "body": (
                        "Adds Agent Broker to the Community Servers list.\n\n"
                        "- 14 MCP tools (find/verify/message/book appointments with SMBs worldwide)\n"
                        "- Agent-native payments: pay per call in USDC on Base via x402 "
                        "(no signup/API key; reads free, writes paid)\n"
                        "- Built-in TCPA / GDPR / CASL compliance gate (22 jurisdictions)\n\n"
                        f"Live MCP endpoint: {base_url}/mcp\n"
                        f"Repo: {repo_url}\n"
                        f"Discovery: {base_url}/.well-known/mcp.json"
                    ),
                    "maintainer_can_modify": True,
                },
            )
            if r6.status_code == 201:
                return {"ok": True, "pr_url": r6.json()["html_url"]}
            if r6.status_code == 422 and "pull request already exists" in r6.text.lower():
                # find the existing PR
                r7 = await c.get(
                    f"https://api.github.com/repos/{upstream_owner}/{upstream_repo}/pulls",
                    params={"head": f"{owner}:{branch}", "state": "open"},
                )
                items = r7.json() if r7.status_code == 200 else []
                if items:
                    return {"ok": True, "pr_url": items[0]["html_url"], "note": "already open"}
            return {"ok": False, "step": "create-pr", "status_code": r6.status_code, "response": r6.text[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 4) punkpeye/awesome-mcp-servers — same machinery, different file location
# ---------------------------------------------------------------------------

async def submit_awesome_mcp_pr(dry_run: bool = False) -> dict:
    """Open a PR adding our entry to punkpeye/awesome-mcp-servers."""
    pat = os.getenv("GITHUB_PAT", "")
    if not pat:
        return {"ok": False, "error": "GITHUB_PAT missing"}

    upstream_owner = "punkpeye"
    upstream_repo = "awesome-mcp-servers"
    branch = "add-agent-broker"
    owner, _ = _repo_slug()
    repo_url = _repo_url()
    base_url = _public_url()

    line = (
        f"- [Agent Broker]({repo_url}) — "
        "AI agents find, verify, message, and book appointments with small businesses "
        "worldwide, paying per call in USDC on Base via x402 (no signup; reads free, "
        f"writes paid). Built-in TCPA/GDPR/CASL compliance gate. {base_url}"
    )

    if dry_run:
        return {"ok": True, "dry_run": True, "branch": branch, "insert_line": line}

    try:
        import httpx, base64
        H = {
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=30.0, headers=H) as c:
            rf = await c.get(f"https://api.github.com/repos/{owner}/{upstream_repo}")
            if rf.status_code == 404:
                r = await c.post(f"https://api.github.com/repos/{upstream_owner}/{upstream_repo}/forks")
                if r.status_code not in (200, 202):
                    return {
                        "ok": False, "step": "fork",
                        "status_code": r.status_code,
                        "manual_fix": (
                            f"Open https://github.com/{upstream_owner}/{upstream_repo}/fork "
                            "in a browser, click 'Create fork' once, then re-run."
                        ),
                    }
                await asyncio.sleep(2)
                for _ in range(10):
                    rf = await c.get(f"https://api.github.com/repos/{owner}/{upstream_repo}")
                    if rf.status_code == 200:
                        break
                    await asyncio.sleep(2)
            if rf.status_code != 200:
                return {"ok": False, "step": "fork-read", "status_code": rf.status_code}
            default_branch = rf.json().get("default_branch", "main")
            r2 = await c.get(f"https://api.github.com/repos/{upstream_owner}/{upstream_repo}/git/refs/heads/{default_branch}")
            base_sha = r2.json()["object"]["sha"]
            await c.patch(
                f"https://api.github.com/repos/{owner}/{upstream_repo}/git/refs/heads/{default_branch}",
                json={"sha": base_sha, "force": True},
            )
            r3 = await c.post(
                f"https://api.github.com/repos/{owner}/{upstream_repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            )
            if r3.status_code == 422:
                await c.patch(
                    f"https://api.github.com/repos/{owner}/{upstream_repo}/git/refs/heads/{branch}",
                    json={"sha": base_sha, "force": True},
                )

            r4 = await c.get(f"https://api.github.com/repos/{upstream_owner}/{upstream_repo}/contents/README.md?ref={default_branch}")
            if r4.status_code != 200:
                return {"ok": False, "step": "read-readme", "response": r4.text[:300]}
            data = r4.json()
            readme = base64.b64decode(data["content"]).decode("utf-8")
            if line in readme:
                return {"ok": True, "no_op": True, "note": "already present"}

            # punkpeye organizes by category — try Browser/Communication, fallback to end
            for cat in ("### 💬 <a name=\"communication\"></a>Communication", "### 🌐 Communication", "### Communication", "### 💬 Communication"):
                if cat in readme:
                    idx = readme.find(cat)
                    head = readme[:idx + len(cat)]
                    tail = readme[idx + len(cat):]
                    nl = tail.find("\n\n")
                    cut = nl + 2 if nl != -1 else 0
                    new_readme = head + tail[:cut] + line + "\n" + tail[cut:]
                    break
            else:
                new_readme = readme.rstrip() + "\n\n" + line + "\n"

            r5 = await c.put(
                f"https://api.github.com/repos/{owner}/{upstream_repo}/contents/README.md",
                json={
                    "message": "Add Agent Broker",
                    "content": base64.b64encode(new_readme.encode("utf-8")).decode("ascii"),
                    "sha": data["sha"],
                    "branch": branch,
                },
            )
            if r5.status_code not in (200, 201):
                return {"ok": False, "step": "commit", "response": r5.text[:300]}

            r6 = await c.post(
                f"https://api.github.com/repos/{upstream_owner}/{upstream_repo}/pulls",
                json={
                    "title": "Add Agent Broker — horizontal agent-to-business layer",
                    "head": f"{owner}:{branch}",
                    "base": default_branch,
                    "body": f"Adds Agent Broker (14-tool MCP server; agents pay per call in USDC on Base via x402, reads free; worldwide TCPA/GDPR/CASL compliance gate). Live: {base_url}/mcp",
                    "maintainer_can_modify": True,
                },
            )
            if r6.status_code == 201:
                return {"ok": True, "pr_url": r6.json()["html_url"]}
            if r6.status_code == 422 and "already exists" in r6.text.lower():
                r7 = await c.get(
                    f"https://api.github.com/repos/{upstream_owner}/{upstream_repo}/pulls",
                    params={"head": f"{owner}:{branch}", "state": "open"},
                )
                if r7.status_code == 200 and r7.json():
                    return {"ok": True, "pr_url": r7.json()[0]["html_url"], "note": "already open"}
            return {"ok": False, "step": "create-pr", "response": r6.text[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 5) Glama — verify auto-index by polling their public listing
# ---------------------------------------------------------------------------

async def verify_glama_indexed(dry_run: bool = False) -> dict:
    """Glama crawls GitHub. After we set the topic, they typically index within ~24h."""
    if dry_run:
        return {"ok": True, "dry_run": True, "note": "Glama auto-indexes from GitHub topic 'mcp-server'"}
    try:
        import httpx
        owner, repo = _repo_slug()
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
            # Glama's repo-search URL pattern; if we 404, we're not indexed yet
            r = await c.get(f"https://glama.ai/mcp/servers/@{owner}/{repo}")
            return {
                "ok": r.status_code == 200,
                "status_code": r.status_code,
                "indexed": r.status_code == 200,
                "note": (
                    "Indexed by Glama — listed publicly."
                    if r.status_code == 200
                    else "Not indexed yet. Glama crawls the GitHub `mcp-server` topic; expect 1-3 days."
                ),
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(dry_run: bool = False):
    _load_dotenv()
    print("=" * 70)
    print("MCP REGISTRY SUBMISSION")
    print("=" * 70)
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"Base URL: {_public_url()}")
    print(f"Repo:     {_repo_url()}")
    print()

    steps = [
        ("Smithery — PUT /releases", submit_smithery),
        ("GitHub topics (Glama auto-indexes from these)", set_github_topics),
        ("modelcontextprotocol/servers — open PR", submit_mcp_servers_pr),
        ("punkpeye/awesome-mcp-servers — open PR", submit_awesome_mcp_pr),
        ("Glama — verify indexed", verify_glama_indexed),
    ]

    results = {}
    for label, fn in steps:
        print(f">> {label}")
        res = await fn(dry_run=dry_run)
        results[label] = res
        marker = "OK  " if res.get("ok") else "FAIL"
        print(f"   [{marker}] {json.dumps({k: v for k, v in res.items() if k != 'response'}, default=str)[:280]}")
        if not res.get("ok") and res.get("response"):
            print(f"          response: {str(res['response'])[:200]}")
        print()

    summary_ok = sum(1 for r in results.values() if r.get("ok"))
    print(f"Summary: {summary_ok}/{len(steps)} steps OK")
    print()
    print("Manual follow-ups (no API):")
    print(" - apis.guru: open PR at https://github.com/APIs-guru/openapi-directory/")
    print("     add an entry under  APIs/agent-broker-edge.basil-agent.workers.dev/1.0.0/openapi.yaml")
    print("     Their bot validates the OpenAPI spec; merge is usually within a week.")
    print(" - Anthropic / Cursor / Continue: see OUTREACH_KIT.md for email templates.")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    asyncio.run(main(dry_run=dry))
