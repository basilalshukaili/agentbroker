"""The manifests must be derived from registry/servers.yaml, and the validator
must actually reject the things it claims to reject.

WHY. On 2026-09-06 six hand-kept manifests described the same servers with three
different version numbers and two different hostnames. All of them worked, so
nobody looked - until a Cloudflare worker was deleted, every hatchloop.dev/mcp/*
route began answering 404, and two of the three catalogue listings were
advertising a dead address while the site root kept returning 200.

CI runs `gen_manifests.py --check`, which proves the committed files match the
registry. These tests prove the other half: that the validator would object to a
registry which is itself wrong. A generator with a permissive validator produces
six files that agree on the same mistake, which is worse than six that disagree,
because disagreement is at least visible.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

AB = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(AB, "scripts"))

import gen_manifests as gm  # noqa: E402
import miniyaml  # noqa: E402


@pytest.fixture(scope="module")
def cfg():
    return gm.load_yaml(gm.SOURCE)


def test_the_reader_itself_still_reads(cfg):
    """miniyaml's own self-test, run as part of the suite.

    It lives in the module so it can be run by hand, and here so it cannot be
    forgotten. A YAML reader that quietly puts nested keys in the wrong map -
    which the first implementation did - would let every check below pass
    against a config nobody wrote.
    """
    assert miniyaml._selftest() == 0


def test_registry_describes_the_servers_we_actually_run(cfg):
    slugs = {s["slug"] for s in cfg["servers"]}
    # The five capability doors plus the product. Named explicitly rather than
    # counted, so deleting one fails here instead of silently shrinking a total.
    assert {"agent-broker", "sanctions-screening", "company-verification",
            "compliance-check", "sms-whatsapp-messaging",
            "appointment-booking"} <= slugs
    assert not gm.validate(cfg), gm.validate(cfg)


def test_every_generated_file_matches_the_registry(cfg):
    """The same assertion CI makes, so a laptop run catches it first."""
    drifted = []
    for path, content in gm.targets(cfg).items():
        rel = os.path.relpath(path, AB).replace("\\", "/")
        if not os.path.isfile(path):
            drifted.append(f"{rel} is missing")
        elif open(path, encoding="utf-8", newline="").read() != content:
            drifted.append(f"{rel} differs from the generator")
    assert not drifted, (
        "run: python scripts/gen_manifests.py --write\n  " + "\n  ".join(drifted))


def test_one_version_across_every_manifest(cfg):
    """The exact drift that started this: 0.2.12, 0.2.10 and 0.1.0 at once."""
    versions = {}
    for s in cfg["servers"]:
        if not s.get("live", True) or not (s.get("publish") or {}).get("registry"):
            continue
        path = (os.path.join(AB, "server.json") if s["kind"] == "product"
                else os.path.join(AB, "registry", s["slug"], "server.json"))
        versions[s["slug"]] = json.load(open(path, encoding="utf-8"))["version"]
    # Doors are views of one product built from one deployment, so they cannot
    # honestly carry different versions from it.
    assert len(set(versions.values())) == 1, versions


def test_a_door_never_advertises_the_products_url(cfg):
    """A door pointing at /mcp would serve all 23 tools through a narrow name.

    That is not a cosmetic error. The whole reason doors exist is a token budget:
    23 tools cost roughly 13,000 tokens and a door about 4,500. A door wired to
    the full server silently spends the budget it was created to save.
    """
    for s in cfg["servers"]:
        if s.get("kind") != "door" or not s.get("live", True):
            continue
        assert gm.canonical_url(cfg["defaults"], s).endswith("/mcp/" + s["slug"])


def test_the_alias_path_override_is_honoured(cfg):
    """api.hatchloop.dev/mcp/agent-broker is a 404; the alias is the bare /mcp.

    Measured on 2026-09-06. The canonical host reaches the full server through a
    Vercel rewrite, and the origin has no such route. Assuming the two hosts were
    symmetric would have repointed the Smithery listing at a dead URL.
    """
    defaults = cfg["defaults"]
    product = next(s for s in cfg["servers"] if s["slug"] == "agent-broker")
    assert gm.alias_url(defaults, product) == defaults["alias_host"] + "/mcp"
    door = next(s for s in cfg["servers"] if s["slug"] == "sanctions-screening")
    assert gm.alias_url(defaults, door).endswith("/mcp/sanctions-screening")


# ---------------------------------------------------------------------------
# The validator must REFUSE. A gate that inspects nothing gets trusted.
# ---------------------------------------------------------------------------
def _server(**over):
    base = {"slug": "thing", "kind": "product", "title": "Thing",
            "description": "A short description.", "prefix": "thing_",
            "version": "1.0.0", "docs": "https://hatchloop.dev/thing/"}
    base.update(over)
    return base


@pytest.mark.parametrize("bad,expect", [
    ({"slug": "Bad Slug"}, "slug"),
    ({"description": "x" * 101}, "description"),
    # A bare date holds isLatest in the official registry for ever, because
    # nothing sorts above it.
    ({"version": "2026-09-06"}, "semver"),
    ({"prefix": "nounderscore"}, "prefix"),
    ({"kind": "door", "of": None}, "door"),
])
def test_validator_rejects_a_broken_entry(bad, expect):
    problems = gm.validate({"servers": [_server(**bad)]})
    assert problems, f"accepted {bad!r}"
    assert any(expect in p for p in problems), problems


def test_validator_rejects_two_products_sharing_a_tool_prefix():
    """Two products with the same prefix collide in any client loading both.

    Doors share their product's prefix deliberately, so only products compare.
    """
    cfg = {"servers": [_server(slug="one"), _server(slug="two")]}
    assert any("prefix" in p for p in gm.validate(cfg))


def test_validator_accepts_doors_sharing_the_products_prefix():
    cfg = {"servers": [
        _server(slug="one"),
        _server(slug="door", kind="door", of="one"),
    ]}
    assert gm.validate(cfg) == []


def test_a_server_marked_not_live_is_never_published(cfg):
    """grocery-cart sits in the registry so the probe and the budget test know
    about it before it ships. Nothing may advertise an endpoint that 404s."""
    written = "\n".join(gm.targets(cfg).values())
    not_live = [s["slug"] for s in cfg["servers"] if not s.get("live", True)]
    assert not_live, "the fixture for this test needs a not-live server"
    for slug in not_live:
        assert slug not in written, f"{slug} is not live but appears in a manifest"


# ---------------------------------------------------------------------------
# Counts on public pages are derived, never typed.
# ---------------------------------------------------------------------------
def test_no_typed_tool_counts_on_public_pages():
    """The founder found "23 tools" on the page describing the credit rails for
    the whole platform. Eight more were in the same two files. The numbers were
    right; the defect is that a product fact reached a public surface by being
    typed, in a company being built to run fifty servers."""
    import subprocess
    p = subprocess.run([sys.executable,
                        os.path.join(AB, "scripts", "check_no_typed_counts.py")],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=AB)
    assert p.returncode == 0, p.stdout + p.stderr


def test_every_tool_falls_in_exactly_one_bucket():
    """keyless + quota + needs-key must equal the total.

    The first version of needs_key() subtracted the daily-quota tools, which are
    not in the auth set, and would have published 5 where the truth is 8.
    Deriving a number is not the same as deriving it correctly.
    """
    sys.path.insert(0, AB)
    from web import facts
    assert facts.total_tools() > 0
    assert facts.keyless() + facts.quota_free() + facts.needs_key() == facts.total_tools()


def test_pages_render_no_unsubstituted_token():
    """A token that reaches a reader is worse than the digit it replaced."""
    import re
    sys.path.insert(0, AB)
    from web import pages
    for name in ("render_home", "render_pricing", "render_status",
                 "render_terms", "render_refund", "render_privacy"):
        html = getattr(pages, name)()
        left = re.findall(r"\{n_[a-z_]+\}", html)
        assert not left, f"{name} rendered {left}"
