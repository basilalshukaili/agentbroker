"""Offline contract tests for scripts/check_live_registry.py."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "check_live_registry", REPO / "scripts" / "check_live_registry.py"
)
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def manifest(name: str = "dev.hatchloop/agent-broker") -> dict:
    return {
        "$schema": "ignored-by-parity-check",
        "name": name,
        "title": "Agent Broker",
        "description": "Current description",
        "websiteUrl": "https://hatchloop.dev/agent-broker/",
        "repository": {
            "url": "https://github.com/basilalshukaili/agentbroker",
            "source": "github",
        },
        "version": "0.2.13",
        "remotes": [{
            "type": "streamable-http",
            "url": "https://hatchloop.dev/mcp/agent-broker",
            "headers": [{
                "name": "X-Agent-Identity",
                "description": "Optional identity",
                "isRequired": False,
                "isSecret": True,
            }],
        }],
    }


def row(server: dict, *, status: str = "active", latest: bool = True) -> dict:
    # The live API omits false-valued header defaults.  That is semantically
    # equal to the explicit false in our generated manifest.
    live = json.loads(json.dumps(server))
    live["remotes"][0]["headers"][0].pop("isRequired", None)
    return {
        "server": live,
        "_meta": {
            checker.OFFICIAL_META: {"status": status, "isLatest": latest},
        },
    }


class RegistryParityTests(unittest.TestCase):
    def test_registry_url_requests_exact_latest_and_encodes_slash(self):
        url = checker.registry_url("dev.hatchloop/agent-broker")
        self.assertIn("search=dev.hatchloop%2Fagent-broker", url)
        self.assertIn("version=latest", url)

    def test_exact_match_accepts_registry_omitted_false_default(self):
        local = manifest()
        entry = row(local)
        self.assertEqual(checker.compare_entry(local, entry), [])

    def test_public_field_drift_is_reported(self):
        local = manifest()
        entry = row(local)
        entry["server"]["version"] = "0.2.12"
        entry["server"]["remotes"][0]["url"] = "https://old.example/mcp"
        errors = checker.compare_entry(local, entry)
        self.assertTrue(any("version drift" in error for error in errors))
        self.assertTrue(any("remotes drift" in error for error in errors))

    def test_retired_or_non_latest_entry_fails(self):
        local = manifest()
        errors = checker.compare_entry(local, row(local, status="deleted", latest=False))
        self.assertTrue(any("status" in error for error in errors))
        self.assertTrue(any("not marked latest" in error for error in errors))

    def test_search_result_requires_exact_name(self):
        local = manifest()
        near = manifest("dev.hatchloop/agent-broker-plus")
        entry, errors = checker.exact_entry({"servers": [row(near)]}, local["name"])
        self.assertIsNone(entry)
        self.assertIn("absent", errors[0])

    def test_check_manifests_reports_missing_exact_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "server.json"
            path.write_text(json.dumps(manifest()), encoding="utf-8")
            checked, errors = checker.check_manifests(
                [path], fetcher=lambda _name: {"servers": []}
            )
        self.assertEqual(checked, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("absent", errors[0])

    def test_check_manifests_marks_clean_entry(self):
        local = manifest()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "server.json"
            path.write_text(json.dumps(local), encoding="utf-8")
            checked, errors = checker.check_manifests(
                [path], fetcher=lambda _name: {"servers": [row(local)]}
            )
        self.assertEqual(errors, [])
        self.assertEqual(
            checked,
            [{"name": local["name"], "version": "0.2.13", "status": "match"}],
        )


if __name__ == "__main__":
    unittest.main()
