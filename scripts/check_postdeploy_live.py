#!/usr/bin/env python3
"""Fail closed unless the reviewed AgentBroker build and contracts are live.

Run only after the rollback-first VPS harness. A green code CI run is not
evidence that its commit reached api.hatchloop.dev. This read-only gate checks
the exact full build SHA, the authoritative target, and every legal page that
renders the contracting entity. It never calls a state-changing MCP tool.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Callable


BASE_URL = "https://api.hatchloop.dev"
DEPLOY_TARGET = "vps-agentbroker"
CORRECT_ARABIC_NAME = "الرفيق التقني"
WRONG_ARABIC_NAME = "رفيق التقنية"
LATIN_ENTITY_NAME = "Techmate"
COMMERCIAL_REGISTRATION = "1661879"
LEGAL_COUNTS = {
    "/terms": 4,
    "/privacy": 2,
    "/refund": 2,
    "/billing/checkout": 0,
}
MAX_BYTES = 1_000_000


def fetch(path: str) -> tuple[int, str, str]:
    """Return status, media type, and body from one fixed public origin path."""
    if path not in ("/health", *LEGAL_COUNTS):
        raise ValueError(f"unapproved path: {path!r}")
    url = BASE_URL + path
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json" if path == "/health" else "text/html",
                 "User-Agent": "agentbroker-postdeploy-acceptance/1"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        if response.geturl() != url:
            raise RuntimeError(f"unexpected redirect on {path}")
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise RuntimeError(f"response too large on {path}")
        return response.status, response.headers.get_content_type(), body.decode(
            "utf-8", errors="replace")


def inspect_health(status: int, media_type: str, body: str,
                   expected_build: str) -> dict:
    problems = []
    payload = {}
    if status != 200:
        problems.append(f"HTTP status {status}, expected 200")
    if media_type != "application/json":
        problems.append(f"media type {media_type!r}, expected application/json")
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("health body is not an object")
    except (json.JSONDecodeError, ValueError) as exc:
        problems.append(str(exc))
        payload = {}
    build = payload.get("build_commit")
    target = payload.get("deploy_target")
    if build != expected_build:
        problems.append(f"build_commit {build!r} does not equal expected full SHA")
    if target != DEPLOY_TARGET:
        problems.append(f"deploy_target {target!r} does not equal {DEPLOY_TARGET!r}")
    return {"path": "/health", "ok": not problems, "build_commit": build,
            "deploy_target": target, "problems": problems}


def inspect_legal(path: str, status: int, media_type: str, body: str) -> dict:
    normalized = unicodedata.normalize("NFC", html.unescape(body))
    expected = LEGAL_COUNTS[path]
    observed = {
        "correct_arabic": normalized.count(CORRECT_ARABIC_NAME),
        "wrong_arabic": normalized.count(WRONG_ARABIC_NAME),
        "latin_entity": normalized.count(LATIN_ENTITY_NAME),
        "commercial_registration": normalized.count(COMMERCIAL_REGISTRATION),
    }
    problems = []
    if status != 200:
        problems.append(f"HTTP status {status}, expected 200")
    if media_type != "text/html":
        problems.append(f"media type {media_type!r}, expected text/html")
    if observed["correct_arabic"] != expected:
        problems.append(f"correct Arabic name count {observed['correct_arabic']}, expected {expected}")
    if observed["wrong_arabic"]:
        problems.append(f"wrong Arabic name count {observed['wrong_arabic']}, expected 0")
    for key in ("latin_entity", "commercial_registration"):
        if observed[key] < expected:
            problems.append(f"{key} count {observed[key]}, expected at least {expected}")
    return {"path": path, "ok": not problems, "observed": observed,
            "problems": problems}


def check(expected_build: str, get: Callable[[str], tuple[int, str, str]] = fetch) -> dict:
    results = []
    errors = []
    for path in ("/health", *LEGAL_COUNTS):
        try:
            status, media_type, body = get(path)
            result = (inspect_health(status, media_type, body, expected_build)
                      if path == "/health" else
                      inspect_legal(path, status, media_type, body))
            results.append(result)
        except (OSError, RuntimeError, ValueError, UnicodeError,
                urllib.error.URLError) as exc:
            errors.append({"path": path, "error": str(exc)})
    return {"ready": len(results) == 5 and not errors and all(r["ok"] for r in results),
            "expected_build": expected_build, "expected_target": DEPLOY_TARGET,
            "results": results, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-build", required=True,
                        help="full lowercase 40-character reviewed commit SHA")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_build):
        parser.error("--expected-build must be a full lowercase 40-character SHA")
    result = check(args.expected_build)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
