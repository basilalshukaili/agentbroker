#!/usr/bin/env python3
"""Generate every MCP manifest from registry/servers.yaml, and prove they match.

WHAT THIS PREVENTS, in one sentence: on 2026-09-06 six hand-kept manifests
described the same servers with three different version numbers and two different
hostnames, both hostnames worked, nobody noticed, and when one of them stopped
working the outage was invisible because the site root still answered 200.

Manifests are not documentation. They are the addresses strangers and agents use
to reach us, published into catalogues we do not control, and a wrong one is a
product that does not exist as far as the caller is concerned.

    python scripts/gen_manifests.py            # show what would change
    python scripts/gen_manifests.py --write    # write the files
    python scripts/gen_manifests.py --check    # exit 1 if anything drifted (CI)

The --check mode is the point. Generating files is easy; the value is a build
that fails when somebody edits a generated file by hand, because they will.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
# THIS SCRIPT LIVES IN THE AGENTBROKER REPO ON PURPOSE. It first lived one level
# up in the hatchloop tree, next to the other operational scripts, and it could
# not gate anything: CI checks out THIS repository, so a generator outside it is
# a generator nobody runs on a push. A drift guard that only runs when a human
# remembers is the thing it was written to replace.
AB = os.path.dirname(HERE)
SOURCE = os.path.join(AB, "registry", "servers.yaml")

SCHEMA = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"

# The registry rejects a description over 100 characters, and a title over 100.
MAX_DESCRIPTION = 100
MAX_TITLE = 100


# The YAML reader lives in scripts/miniyaml.py with its own self-test. It is not
# inlined here on purpose: the first version was inline and silently put nested
# keys in the wrong map, which is the one failure mode this whole script exists
# to prevent. `python scripts/miniyaml.py` proves it still reads correctly.
# --------------------------------------------------------------------------
import miniyaml  # noqa: E402


def load_yaml(path: str) -> dict:
    cfg = miniyaml.load(path)
    # Prove the read produced the shape everything below assumes. A reader that
    # returns {} or drops a key would otherwise generate manifests from
    # defaults nobody set.
    if not isinstance(cfg, dict) or not isinstance(cfg.get('servers'), list) or not cfg['servers']:
        raise ValueError(f"{path}: no servers were read")
    d = cfg.get('defaults')
    required = ('namespace', 'canonical_host', 'alias_host', 'repository', 'transport')
    if not isinstance(d, dict):
        raise ValueError(f"{path}: defaults did not parse as a map")
    missing = [k for k in required if not d.get(k)]
    if missing:
        raise ValueError(f"{path}: defaults is missing {missing}")
    return cfg


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------
def canonical_url(defaults: dict, s: dict) -> str:
    return f"{defaults['canonical_host']}/mcp/{s['slug']}"


def alias_url(defaults: dict, s: dict) -> str:
    # The alias host is the ORIGIN, and the origin does not necessarily answer on
    # the same path as the canonical host - the canonical one is a rewrite. Any
    # server whose origin path differs says so in `alias_path`, because the first
    # version of this function assumed they matched and would have repointed the
    # Smithery listing at a 404.
    return f"{defaults['alias_host']}{s.get('alias_path') or '/mcp/' + s['slug']}"


def server_json(defaults: dict, s: dict) -> dict:
    doc = {
        "$schema": SCHEMA,
        "name": f"{defaults['namespace']}/{s['slug']}",
        "title": s["title"],
        "description": s["description"],
        "websiteUrl": s["docs"],
        "repository": {"url": defaults["repository"], "source": "github"},
        "version": str(s["version"]),
        "remotes": [{"type": defaults["transport"], "url": canonical_url(defaults, s)}],
    }
    if s.get("auth_header"):
        doc["remotes"][0]["headers"] = [{
            "name": s["auth_header"],
            "description": "Agent identity key. Free tier needs no key.",
            "isRequired": bool(s.get("auth_required", False)),
            "isSecret": True,
        }]
    return doc


def glama_json(defaults: dict, s: dict) -> dict:
    """Glama's catalogue entry.

    NOT merged with whatever is already on disk. An earlier version preserved
    unknown keys from the existing file, which sounds careful and is the opposite:
    a field nobody can find the source of is exactly the drift this script exists
    to end. Everything Glama shows comes from servers.yaml or it does not exist.
    """
    doc = {
        "$schema": "https://glama.ai/mcp/schemas/server.json",
        "maintainers": ["basilalshukaili"],
        "name": s["slug"],
        "displayName": s["title"],
        "description": s.get("long_description") or s["description"],
        "repository": {"url": defaults["repository"]},
        "remotes": [{"type": defaults["transport"], "url": canonical_url(defaults, s)}],
    }
    if s.get("license"):
        doc["license"] = s["license"]
    if s.get("tags"):
        doc["tags"] = list(s["tags"])
    if s.get("auth_header"):
        doc["auth"] = {
            "type": "required" if s.get("auth_required") else "optional",
            "header": s["auth_header"],
            "scheme": "bearer",
            "note": s.get("auth_note") or s["description"],
        }
    return doc


def _wrap(text: str, width: int) -> list[str]:
    """Wrap without importing textwrap's URL-splitting behaviour.

    A folded YAML scalar joins its lines with spaces, so a URL broken across two
    lines comes back with a space in the middle and stops being a link. Words are
    never split here; an over-long word simply gets its own line.
    """
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(cur)
    return lines


def smithery_yaml(defaults: dict, s: dict) -> str:
    # Smithery points at the ALIAS on purpose. It scans the endpoint, and the
    # alias sits on the origin with one hop fewer than the canonical host, so a
    # rewrite or a CDN change on hatchloop.dev cannot fail their scan and cost us
    # the listing. Every human-facing surface still shows the canonical URL.
    header = s.get("auth_header", "X-Agent-Identity")
    lines = [
        "# GENERATED by scripts/gen_manifests.py from registry/servers.yaml.",
        "# Do not edit by hand: CI compares this file against the generator and fails",
        "# on any difference. Change servers.yaml instead.",
        "startCommand:",
        "  type: http",
        f'  url: "{alias_url(defaults, s)}"',
        "  configSchema:",
        "    type: object",
        "    properties:",
        "      apiKey:",
        "        type: string",
        "        title: API Key (optional)",
        "        description: >",
        f"          Get one at {s['docs']}",
        f"          Pass as request header: {header}: <your-key>.",
    ]
    # THE SPECIFIC WORDING FROM THE REGISTRY, not a summary of it. The first
    # generated version replaced "15 of the 23 tools work with no key at all"
    # with "most tools work with no key at all" - vaguer than the hand-written
    # file it replaced. Vague free-tier wording on Smithery is the exact
    # complaint on the founder's board: a buyer sees a paywall that is not there.
    #
    # It goes LAST because a folded scalar joins its lines with spaces and this
    # note ends on a URL. A full stop welded onto the end of a link is how a link
    # stops being one.
    lines += [f"          {ln}" for ln in
              _wrap(s.get("auth_note") or s["description"], 88)]
    lines += ["    required: []", "  exampleConfig:", '    apiKey: ""']
    return "\n".join(lines) + "\n"


def probe_list(defaults: dict, servers: list[dict]) -> dict:
    """Every URL that must answer, for system_health to check."""
    out = []
    for s in servers:
        if not s.get("live", True):
            continue
        out.append({"slug": s["slug"], "kind": s["kind"],
                    "canonical": canonical_url(defaults, s),
                    "alias": alias_url(defaults, s),
                    "expect_tools": s.get("tool_count")})
    return {"generated_from": "agentbroker/registry/servers.yaml", "endpoints": out}


def probe(cfg: dict, timeout: int = 20) -> int:
    """Call every URL the manifests advertise and see who answers.

    THIS IS THE HONEST HALF OF THE SCRIPT. --check only proves the manifests
    agree with servers.yaml; if servers.yaml is confidently wrong, every file
    agrees on a dead address. The alias asymmetry that made
    api.hatchloop.dev/mcp/agent-broker a 404 was found here and nowhere else.

    It sends a real MCP `initialize` rather than a HEAD or a GET, because the
    endpoints answer 405 to GET and a 405 is indistinguishable from a route that
    exists. It also reads back the server NAME, so a rewrite pointing the wrong
    door at the wrong server is caught rather than counted as a pass.
    """
    import concurrent.futures as cf
    import urllib.error
    import urllib.request

    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "gen_manifests-probe", "version": "1"}},
    }).encode()
    # A default urllib User-Agent is refused by some edges as a bot; see memory
    # `heartbeat-wedge-and-ops-gotchas`.
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
               "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}

    def one(job):
        slug, which, url = job
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url, data=body, headers=headers), timeout=timeout)
            raw = r.read(8000).decode("utf-8", "replace")
            seg = raw[raw.index("data:") + 5:] if "data:" in raw else raw
            try:
                name = json.loads(seg.strip()).get("result", {}).get(
                    "serverInfo", {}).get("name", "")
            except ValueError:
                name = "(unparsed)"
            return slug, which, url, r.status, name
        except urllib.error.HTTPError as e:
            return slug, which, url, e.code, ""
        except Exception as e:                       # noqa: BLE001 - report, never raise
            return slug, which, url, f"ERR {type(e).__name__}", ""

    defaults = cfg["defaults"]
    jobs = []
    for s in cfg["servers"]:
        if not s.get("live", True):
            continue
        jobs.append((s["slug"], "canonical", canonical_url(defaults, s)))
        jobs.append((s["slug"], "alias", alias_url(defaults, s)))

    bad = []
    with cf.ThreadPoolExecutor(8) as ex:
        results = list(ex.map(one, jobs))
    print(f"{'server':24} {'which':10} {'code':>6}  answered as")
    for slug, which, url, code, name in results:
        ok = code == 200 and name == slug
        if not ok:
            bad.append((slug, which, url, code, name))
        print(f"{slug:24} {which:10} {str(code):>6}  {name or '-':24} {url}")

    if bad:
        print(f"\n{len(bad)} advertised URL(s) do not answer as themselves:")
        for slug, which, url, code, name in bad:
            why = f"HTTP {code}" if name in ("", "(unparsed)") else f"answered as {name!r}"
            print(f"  {slug} {which}: {why}  {url}")
        print("\nA manifest pointing here is a product that does not exist to the "
              "caller. Fix the route or correct servers.yaml.")
        return 1
    print(f"\nall {len(results)} advertised URL(s) answered as themselves")
    return 0


def targets(cfg: dict) -> dict[str, str]:
    defaults = cfg["defaults"]
    servers = [s for s in cfg["servers"]]
    files: dict[str, str] = {}

    for s in servers:
        if not s.get("live", True) or not (s.get("publish") or {}).get("registry"):
            continue
        if s["kind"] == "product":
            path = os.path.join(AB, "server.json")
        else:
            path = os.path.join(AB, "registry", s["slug"], "server.json")
        files[path] = json.dumps(server_json(defaults, s), indent=2) + "\n"

    for s in servers:
        if not s.get("live", True):
            continue
        pub = s.get("publish") or {}
        if pub.get("glama"):
            files[os.path.join(AB, "glama.json")] = (
                json.dumps(glama_json(defaults, s), indent=2) + "\n")
        if pub.get("smithery"):
            files[os.path.join(AB, "smithery.yaml")] = smithery_yaml(defaults, s)

    files[os.path.join(AB, "state", "mcp_endpoints.json")] = (
        json.dumps(probe_list(defaults, servers), indent=1) + "\n")
    return files


def validate(cfg: dict) -> list[str]:
    problems, seen_slug, seen_prefix = [], set(), {}
    for s in cfg["servers"]:
        slug = s.get("slug")
        if not slug or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            problems.append(f"bad slug {slug!r}: lowercase, digits and hyphens only")
        if slug in seen_slug:
            problems.append(f"duplicate slug {slug!r}")
        seen_slug.add(slug)
        if len(str(s.get("description", ""))) > MAX_DESCRIPTION:
            problems.append(f"{slug}: description is {len(s['description'])} chars, "
                            f"the registry rejects over {MAX_DESCRIPTION}")
        if len(str(s.get("title", ""))) > MAX_TITLE:
            problems.append(f"{slug}: title over {MAX_TITLE} chars")
        if not re.fullmatch(r"\d+\.\d+\.\d+", str(s.get("version", ""))):
            problems.append(f"{slug}: version {s.get('version')!r} is not semver - a bare "
                            f"date would permanently hold isLatest in the registry")
        if s.get("kind") == "door" and not s.get("of"):
            problems.append(f"{slug}: a door must name the product it belongs to")
        pfx = s.get("prefix")
        if not pfx or not pfx.endswith("_"):
            problems.append(f"{slug}: prefix {pfx!r} must end with an underscore")
        seen_prefix.setdefault(pfx, []).append(slug)

    # Two PRODUCTS sharing a tool prefix would collide in any client that loads
    # both. Doors share their product's prefix on purpose, so only products are
    # compared.
    products = {s["slug"]: s for s in cfg["servers"] if s.get("kind") == "product"}
    by_prefix: dict[str, list[str]] = {}
    for slug, s in products.items():
        by_prefix.setdefault(s.get("prefix"), []).append(slug)
    for pfx, slugs in by_prefix.items():
        if len(slugs) > 1:
            problems.append(f"products {slugs} share the tool prefix {pfx!r}; a client "
                            f"loading both would see two tools with the same name")
    return problems


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--probe", action="store_true",
                    help="call every advertised URL and check who answers")
    a = ap.parse_args(argv)

    cfg = load_yaml(SOURCE)
    problems = validate(cfg)
    if problems:
        print(f"servers.yaml has {len(problems)} problem(s):")
        for p in problems:
            print(f"  {p}")
        return 2

    if a.probe:
        return probe(cfg)

    files = targets(cfg)
    drifted, missing = [], []
    for path, content in sorted(files.items()):
        rel = os.path.relpath(path, AB).replace("\\", "/")
        if not os.path.isfile(path):
            missing.append(rel)
        elif io.open(path, encoding="utf-8", newline="").read() != content:
            drifted.append(rel)

    if a.check:
        if drifted or missing:
            print("MANIFESTS HAVE DRIFTED from registry/servers.yaml:")
            for r in missing:
                print(f"  missing  {r}")
            for r in drifted:
                print(f"  differs  {r}")
            print("\nRun: python scripts/gen_manifests.py --write")
            return 1
        print(f"check: {len(files)} generated file(s) all match servers.yaml")
        return 0

    if not a.write:
        print(f"{len(cfg['servers'])} server(s) in servers.yaml, "
              f"{len(files)} generated file(s)")
        for r in missing:
            print(f"  would create  {r}")
        for r in drifted:
            print(f"  would update  {r}")
        if not missing and not drifted:
            print("  everything already matches")
        print("\nNothing written. Re-run with --write.")
        return 0

    for path, content in sorted(files.items()):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        io.open(path, "w", encoding="utf-8", newline="").write(content)
    print(f"wrote {len(files)} file(s) from servers.yaml")
    for r in missing + drifted:
        print(f"  {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
