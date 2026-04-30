"""
Submit Agent Broker's OpenAPI spec to apis.guru/openapi-directory.

apis.guru is the canonical catalogue of public OpenAPI specs. Several
MCP-aware tools read it as a fallback discovery surface. Submission flow:

  1. fork APIs-guru/openapi-directory     (user does this once)
  2. add APIs/<host>/<version>/openapi.yaml  (this script does it)
  3. open PR upstream                     (user clicks one URL)

Step 2 is automated here. Step 1 is one-time. Step 3 is one click because
fine-grained PATs cannot open PRs against external public repos.

Run:
    python scripts/submit_apisguru.py
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from scripts.submit_to_registries import _load_dotenv, _public_url, _repo_slug


async def main():
    _load_dotenv()
    pat = os.getenv("GITHUB_PAT", "")
    if not pat:
        print("GITHUB_PAT missing"); return

    public_url = _public_url()
    host = public_url.replace("https://", "").replace("http://", "").rstrip("/")
    version = "1.0.0"
    target_path = f"APIs/{host}/{version}/openapi.yaml"

    upstream_owner = "APIs-guru"
    upstream_repo = "openapi-directory"
    owner, _ = _repo_slug()  # basilalshukaili
    branch = "add-agent-broker"

    import httpx
    H = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=30.0, headers=H) as c:
        # 1) confirm fork exists
        rf = await c.get(f"https://api.github.com/repos/{owner}/{upstream_repo}")
        if rf.status_code != 200:
            print(f"FAIL: fork {owner}/{upstream_repo} not accessible: {rf.status_code}")
            return
        default_branch = rf.json().get("default_branch", "main")
        print(f"[OK] fork found: {owner}/{upstream_repo}, default branch '{default_branch}'")

        # 2) sync fork's default branch with upstream (so our PR is on top of latest)
        r2 = await c.get(f"https://api.github.com/repos/{upstream_owner}/{upstream_repo}/git/refs/heads/{default_branch}")
        if r2.status_code != 200:
            print(f"FAIL: read upstream ref: {r2.status_code}")
            return
        base_sha = r2.json()["object"]["sha"]
        await c.patch(
            f"https://api.github.com/repos/{owner}/{upstream_repo}/git/refs/heads/{default_branch}",
            json={"sha": base_sha, "force": True},
        )

        # 3) create or fast-forward our branch
        r3 = await c.post(
            f"https://api.github.com/repos/{owner}/{upstream_repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        if r3.status_code == 422:
            await c.patch(
                f"https://api.github.com/repos/{owner}/{upstream_repo}/git/refs/heads/{branch}",
                json={"sha": base_sha, "force": True},
            )
        elif r3.status_code != 201:
            print(f"FAIL: create branch: {r3.status_code} {r3.text[:200]}")
            return
        print(f"[OK] branch '{branch}' ready on fork")

        # 4) fetch our live OpenAPI spec
        r4 = await c.get(f"{public_url}/openapi.yaml")
        if r4.status_code != 200:
            print(f"FAIL: fetch own openapi.yaml from {public_url}: {r4.status_code}")
            return
        openapi_yaml = r4.text
        print(f"[OK] fetched live OpenAPI spec ({len(openapi_yaml)} bytes)")

        # 5) splice in apis.guru required x-* extensions on top of info:
        #    apis.guru bot insists on x-providerName and x-origin to dedup.
        if "x-providerName" not in openapi_yaml:
            anchor = "info:"
            patched = openapi_yaml.replace(
                anchor,
                "info:\n"
                f"  x-providerName: {host}\n"
                "  x-origin:\n"
                f"    - format: openapi\n"
                f"      url: {public_url}/openapi.yaml\n"
                f"      version: \"3.0\"\n",
                1,
            )
        else:
            patched = openapi_yaml

        # 6) check whether file already exists on the branch (idempotency)
        existing_sha = None
        rcheck = await c.get(
            f"https://api.github.com/repos/{owner}/{upstream_repo}/contents/{target_path}",
            params={"ref": branch},
        )
        if rcheck.status_code == 200:
            existing_sha = rcheck.json().get("sha")

        # 7) commit the file
        body = {
            "message": "Add agent-broker (Agent Broker, smb-broker.onrender.com)",
            "content": base64.b64encode(patched.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if existing_sha:
            body["sha"] = existing_sha
        r5 = await c.put(
            f"https://api.github.com/repos/{owner}/{upstream_repo}/contents/{target_path}",
            json=body,
        )
        if r5.status_code not in (200, 201):
            print(f"FAIL: commit file: {r5.status_code} {r5.text[:300]}")
            return
        print(f"[OK] committed {target_path} on '{branch}'")

        # 8) cannot open PR via fine-grained PAT to external repo — print compare URL
        compare_url = (
            f"https://github.com/{upstream_owner}/{upstream_repo}/"
            f"compare/{default_branch}...{owner}:{upstream_repo}:{branch}"
        )
        print()
        print("Open this URL once and click 'Create pull request' — done:")
        print(f"  {compare_url}")
        print()
        print("Their CI (Travis) will validate the OpenAPI spec on the PR;")
        print("a maintainer typically merges within a week.")


if __name__ == "__main__":
    asyncio.run(main())
