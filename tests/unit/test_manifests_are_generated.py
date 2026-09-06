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


@pytest.mark.parametrize("parent_published", [True, False])
def test_each_door_uses_its_own_products_version(cfg, parent_published):
    """A product may publish in other catalogues while its registry doors remain."""
    from copy import deepcopy

    cfg = deepcopy(cfg)
    products = {s["slug"]: s for s in cfg["servers"] if s["kind"] == "product"}
    products["agent-broker"]["publish"]["registry"] = parent_published
    assert gm.validate(cfg) == []
    versions = {}
    for s in cfg["servers"]:
        if not s.get("live", True) or not (s.get("publish") or {}).get("registry"):
            continue
        path = os.path.join(gm.manifest_directory(s), "server.json")
        versions[s["slug"]] = json.load(open(path, encoding="utf-8"))["version"]
    # Doors are views of one product built from one deployment, so they cannot
    # honestly carry different versions from it.
    for s in cfg["servers"]:
        if s.get("kind") == "door" and s["slug"] in versions:
            assert versions[s["slug"]] == str(products[s["of"]]["version"]), versions


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


@pytest.mark.parametrize("new_first", [False, True])
def test_second_product_keeps_every_existing_manifest(cfg, new_first):
    """Adding a independently versioned product used to replace the root files."""
    from copy import deepcopy

    original = gm.targets(cfg)
    expanded = deepcopy(cfg)
    product = _server(slug="second-product", prefix="second_", version="1.2.3",
                      docs="https://hatchloop.dev/second-product/",
                      publish={"registry": True, "glama": True, "smithery": True})
    expanded["servers"].insert(0 if new_first else len(expanded["servers"]), product)
    assert gm.validate(expanded) == []
    generated = gm.targets(expanded)

    for path, content in original.items():
        if os.path.basename(path) != "mcp_endpoints.json":
            assert generated[path] == content, path
    directory = os.path.join(AB, "registry", "second-product")
    server = json.loads(generated[os.path.join(directory, "server.json")])
    assert server["name"] == "dev.hatchloop/second-product"
    assert server["version"] == "1.2.3"
    assert server["remotes"][0]["url"].endswith("/mcp/second-product")
    glama = json.loads(generated[os.path.join(directory, "glama.json")])
    assert glama["name"] == "second-product"
    assert glama["remotes"][0]["url"].endswith("/mcp/second-product")
    assert "/mcp/second-product" in generated[os.path.join(directory, "smithery.yaml")]
    assert len(generated) == len(original) + 3


def test_a_door_publishing_to_more_catalogues_cannot_replace_its_product(cfg):
    from copy import deepcopy

    expanded = deepcopy(cfg)
    door = next(s for s in expanded["servers"] if s["slug"] == "sanctions-screening")
    door["publish"].update(glama=True, smithery=True)
    generated = gm.targets(expanded)
    original = gm.targets(cfg)
    for filename in ("server.json", "glama.json", "smithery.yaml"):
        assert generated[os.path.join(AB, filename)] == original[os.path.join(AB, filename)]
        assert os.path.join(AB, "registry", "sanctions-screening", filename) in generated


@pytest.mark.parametrize("payload,tool_count,expected", [
    ({"result": {"tools": []}}, 23, 1),
    ({"error": {"code": -32603, "message": "tool registry unavailable"}}, 23, 1),
    ({}, 23, 1),
    ({"error": {"code": -32603, "message": "unavailable"}}, 0, 1),
    ({}, None, 1),
    ({"result": {"tools": [{"name": None}]}}, 1, 1),
    ({"result": {"tools": []}}, 0, 0),
    ({"result": {"tools": [{"name": f"tool_{i}"} for i in range(23)]}}, 23, 0),
])
def test_endpoint_probe_validates_the_tool_response(cfg, monkeypatch, payload, tool_count, expected):
    """HTTP 200 and a correct initialize cannot hide a missing tool registry."""
    import urllib.request

    product = dict(next(s for s in cfg["servers"] if s["slug"] == "agent-broker"),
                   tool_count=tool_count)

    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def read(self, limit):
            return json.dumps(self.body).encode("utf-8")[:limit]

    def respond(request, timeout):
        method = json.loads(request.data)["method"]
        if method == "initialize":
            return Response({"result": {"serverInfo": {"name": "agent-broker"}}})
        assert method == "tools/list"
        return Response(payload)

    monkeypatch.setattr(urllib.request, "urlopen", respond)
    assert gm.probe({"defaults": cfg["defaults"], "servers": [product]}) == expected


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


def test_only_the_known_six_are_unprefixed(cfg):
    """A new server must namespace its tools. The six that already shipped cannot.

    Agent Broker published 23 unprefixed tool names - find_business, send_message,
    get_status - and tool names are as immutable as the slug: renaming them breaks
    every agent that installed us. Those six declare `legacy_unprefixed: true`
    rather than being quietly exempted, and this fails if a seventh appears, so
    the exception has to be an argued decision instead of a default.

    The registry's FIRST version claimed `prefix: broker_` on all six. Zero live
    tools carry it. That is what a source of truth looks like when nothing checks
    it against the running system.
    """
    legacy = {s["slug"] for s in cfg["servers"] if s.get("legacy_unprefixed")}
    assert legacy == {"agent-broker", "sanctions-screening", "company-verification",
                      "compliance-check", "sms-whatsapp-messaging",
                      "appointment-booking"}, legacy
    for s in cfg["servers"]:
        if s["slug"] in legacy:
            assert not s.get("prefix")
        else:
            assert str(s.get("prefix", "")).endswith("_"), s["slug"]


def test_door_counts_include_the_orientation_tools(cfg):
    """A door serves its capability tools PLUS the four orientation tools.

    servers.yaml first recorded only the capability count, so every door claimed
    four fewer tools than tools/list returns. The count published anywhere must be
    what a caller actually sees.
    """
    for s in cfg["servers"]:
        if s.get("kind") != "door" or not s.get("live", True):
            continue
        assert s.get("capability_tools"), f"{s['slug']} has no capability_tools"
        assert s["tool_count"] == s["capability_tools"] + 4, s["slug"]
