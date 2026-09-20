#!/usr/bin/env python3
"""Fail when the official MCP Registry disagrees with our manifests.

The publish workflow proves that a manifest was accepted once.  It does not
prove that the registry still exposes that manifest as the active latest
version.  Downstream catalogues ingest this API, so a missing, retired or stale
entry is a distribution outage even while every MCP endpoint is healthy.

This checker is deliberately read-only and stdlib-only.  It compares every
committed ``server.json`` with the registry's exact-name ``version=latest``
result.  It never publishes, opens an issue or contacts TechMate OS.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
REGISTRY_API = "https://registry.modelcontextprotocol.io/v0.1/servers"
OFFICIAL_META = "io.modelcontextprotocol.registry/official"
USER_AGENT = "HatchLoop-registry-parity/1.0"


class RegistryUnavailable(RuntimeError):
    """The registry could not provide a trustworthy response."""


def manifest_paths(repo: Path = REPO) -> list[Path]:
    """Return every official-registry manifest in stable order."""
    paths = [repo / "server.json", *sorted((repo / "registry").glob("*/server.json"))]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise ValueError(f"missing manifest(s): {', '.join(missing)}")
    return paths


def registry_url(name: str) -> str:
    query = urllib.parse.urlencode({
        "search": name,
        "version": "latest",
        "limit": "100",
    })
    return f"{REGISTRY_API}?{query}"


def fetch_json(
    url: str,
    *,
    timeout: float = 20,
    attempts: int = 3,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch JSON with bounded retries; raise instead of treating failure as drift."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    last_error: BaseException | None = None
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    for attempt in range(1, attempts + 1):
        try:
            with opener(request, timeout=timeout) as response:
                raw = response.read()
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("top-level JSON value is not an object")
            return payload
        except (OSError, TimeoutError, UnicodeError, ValueError) as exc:
            last_error = exc
            if attempt < attempts:
                sleep(float(attempt * 2))

    raise RegistryUnavailable(f"{url}: {type(last_error).__name__}: {last_error}")


def _headers(value: Any) -> list[dict[str, Any]]:
    """Normalize registry-omitted false defaults without hiding real drift."""
    if not isinstance(value, list):
        return []
    normalized = []
    for header in value:
        if not isinstance(header, dict):
            normalized.append({"invalid": header})
            continue
        normalized.append({
            "name": header.get("name"),
            "description": header.get("description", ""),
            "isRequired": bool(header.get("isRequired", False)),
            "isSecret": bool(header.get("isSecret", False)),
        })
    return sorted(normalized, key=lambda item: str(item.get("name", "")).lower())


def _remotes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized = []
    for remote in value:
        if not isinstance(remote, dict):
            normalized.append({"invalid": remote})
            continue
        item = {"type": remote.get("type"), "url": remote.get("url")}
        if "headers" in remote:
            item["headers"] = _headers(remote.get("headers"))
        normalized.append(item)
    return sorted(normalized, key=lambda item: (str(item.get("type", "")), str(item.get("url", ""))))


def public_truth(server: dict[str, Any]) -> dict[str, Any]:
    """Fields whose live registry value must equal the committed manifest."""
    return {
        "name": server.get("name"),
        "version": server.get("version"),
        "title": server.get("title"),
        "description": server.get("description"),
        "websiteUrl": server.get("websiteUrl"),
        "repository": server.get("repository"),
        "remotes": _remotes(server.get("remotes")),
    }


def exact_entry(payload: dict[str, Any], name: str) -> tuple[dict[str, Any] | None, list[str]]:
    rows = payload.get("servers")
    if not isinstance(rows, list):
        return None, ["response has no servers array"]

    matches = [row for row in rows if isinstance(row, dict)
               and isinstance(row.get("server"), dict)
               and row["server"].get("name") == name]
    if not matches:
        return None, [f"exact server {name!r} is absent from latest search results"]
    if len(matches) != 1:
        return None, [f"exact server {name!r} appeared {len(matches)} times"]
    return matches[0], []


def compare_entry(manifest: dict[str, Any], entry: dict[str, Any]) -> list[str]:
    name = str(manifest.get("name", "(unnamed)"))
    errors: list[str] = []
    live = entry.get("server")
    if not isinstance(live, dict):
        return [f"{name}: registry row has no server object"]

    expected_truth = public_truth(manifest)
    live_truth = public_truth(live)
    for field in expected_truth:
        if expected_truth[field] != live_truth[field]:
            errors.append(
                f"{name}: {field} drift: expected {expected_truth[field]!r}, "
                f"registry has {live_truth[field]!r}"
            )

    meta_root = entry.get("_meta")
    meta = meta_root.get(OFFICIAL_META) if isinstance(meta_root, dict) else None
    if not isinstance(meta, dict):
        errors.append(f"{name}: official registry metadata is absent")
    else:
        if meta.get("status") != "active":
            errors.append(f"{name}: registry status is {meta.get('status')!r}, expected 'active'")
        if meta.get("isLatest") is not True:
            errors.append(f"{name}: registry result is not marked latest")
    return errors


def check_manifests(
    paths: Iterable[Path],
    *,
    fetcher: Callable[[str], dict[str, Any]],
) -> tuple[list[dict[str, str]], list[str]]:
    checked: list[dict[str, str]] = []
    errors: list[str] = []
    for path in paths:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: cannot read manifest: {exc}")
            continue
        if not isinstance(manifest, dict) or not manifest.get("name"):
            errors.append(f"{path}: manifest has no name")
            continue

        name = str(manifest["name"])
        payload = fetcher(name)
        entry, lookup_errors = exact_entry(payload, name)
        errors.extend(f"{path}: {error}" for error in lookup_errors)
        if entry is None:
            continue
        entry_errors = compare_entry(manifest, entry)
        errors.extend(f"{path}: {error}" for error in entry_errors)
        checked.append({
            "name": name,
            "version": str(manifest.get("version", "")),
            "status": "drift" if entry_errors else "match",
        })
    return checked, errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", action="append", type=Path,
                        help="manifest to check; repeatable (default: all six)")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        paths = args.manifest or manifest_paths()

        def fetcher(name: str) -> dict[str, Any]:
            return fetch_json(registry_url(name), timeout=args.timeout, attempts=args.attempts)

        checked, errors = check_manifests(paths, fetcher=fetcher)
    except (RegistryUnavailable, ValueError) as exc:
        if args.as_json:
            print(json.dumps({"ok": False, "unavailable": str(exc)}, indent=2))
        else:
            print(f"UNAVAILABLE: {exc}", file=sys.stderr)
        return 2

    result = {"ok": not errors, "checked": checked, "errors": errors}
    if args.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif errors:
        print("Official MCP Registry drift detected:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
    else:
        for item in checked:
            print(f"OK {item['name']} {item['version']}")
        print(f"Official MCP Registry matches {len(checked)} committed manifests.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
