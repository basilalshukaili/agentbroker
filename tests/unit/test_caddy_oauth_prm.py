"""The Caddyfile change that gives hatchloop.dev's OAuth protected-resource documents the right `resource`: prepared,
tested offline, NOT applied here.

Same discipline as test_caddy_mcp_retired.py: the transform is pure text, idempotent, removes nothing, and refuses a file
that no longer looks like the one it was written for. The probes and the origin must agree, or the installer would roll
back a correct change.

THE DEFECT IT FIXES (deploy/caddy/oauth_prm.py has the long form): hatchloop.dev/.well-known/* is proxied to the origin by
a Next.js rewrite that replaces the Host header, and the origin builds a protected-resource document's `resource` from
Host, so every capability door's metadata named the API host instead of the URL a client connected to.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CADDY = ROOT / "deploy" / "caddy"
sys.path.insert(0, str(CADDY))

import mcp_direct as md  # noqa: E402
import mcp_retired as mr  # noqa: E402
import oauth_prm as op  # noqa: E402

from agent_interface import profiles, retired_doors  # noqa: E402
from tests.unit.test_caddy_mcp_direct import LIVE, DOORS, _directives, _handle_body  # noqa: E402

SLUGS = sorted(retired_doors.RETIRED_DOORS)
PRM = "/.well-known/oauth-protected-resource"
AFTER_DIRECT = md.apply(LIVE, DOORS)
AFTER_BOTH = mr.apply(AFTER_DIRECT, SLUGS)           # the live box today: mcp_direct and mcp_retired are applied


def _installer():
    spec = importlib.util.spec_from_file_location("install_mcp_direct_prm", CADDY / "install_mcp_direct.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_block_goes_before_the_catch_all_after_every_existing_route():
    new = op.apply(AFTER_BOTH, DOORS)
    assert new.index(md.MARK_END) < new.index(mr.MARK_END) < new.index("@oauth_prm") < new.index("reverse_proxy 127.0.0.1:3000"), (
        "handle blocks are first-match-wins in source order, and the catch-all must stay last")
    assert new.index("handle @tmos_moved") < new.index("@oauth_prm")


def test_it_applies_to_a_file_that_has_neither_earlier_change_and_in_either_order():
    assert "@oauth_prm" in op.apply(LIVE, DOORS)
    assert "@oauth_prm" in op.apply(AFTER_DIRECT, DOORS)
    prm_first = mr.apply(op.apply(AFTER_DIRECT, DOORS), SLUGS)
    assert prm_first.count("@oauth_prm path") == 1 and prm_first.count("@mcp_retired path") == 1
    assert prm_first.index("@oauth_prm") < prm_first.index("reverse_proxy 127.0.0.1:3000")


def test_exactly_the_protected_resource_family_is_routed_and_nothing_wider():
    new = op.apply(AFTER_BOTH, DOORS)
    line = next(l for l in new.splitlines() if l.strip().startswith("@oauth_prm"))
    assert line.split()[2:] == [PRM, PRM + "/*"]
    active = _directives(new)
    assert "/.well-known/*" not in active, "a wildcard over /.well-known/ would swallow files the site puts there"
    for other in ("oauth-authorization-server", "mcp.json", "agent-card", "glama", "x402", "server-card"):
        assert other not in _handle_body(new, "oauth_prm"), f"{other} is Host-independent and stays where it is"


def test_the_host_header_reaches_the_origin_untouched_and_the_real_ip_header_is_not_trusted():
    new = op.apply(AFTER_BOTH, DOORS)
    body = _handle_body(new, "oauth_prm")
    assert "reverse_proxy 127.0.0.1:8010" in body
    assert "header_up -X-Real-IP" in body and "header_up X-Forwarded-Proto https" in body
    # THE POINT: never "fix" this by rewriting Host, or the origin is back to naming the wrong URL
    assert not re.search(r"(?i)header_up\s+\+?Host\b", body), "Host must pass through unchanged"
    for forbidden in ("rewrite", "uri ", "header_down", "respond", "redir"):
        assert forbidden not in body, forbidden
    assert "trusted_proxies" not in _directives(new)


def test_it_is_idempotent_and_changes_no_existing_line():
    once = op.apply(AFTER_BOTH, DOORS)
    assert op.apply(once, DOORS) == once
    removed = [l for l in md.unified_diff(AFTER_BOTH, once).splitlines() if l.startswith("-") and not l.startswith("---")]
    assert removed == [], "this change must not delete or alter any existing line"
    assert op.is_applied(once) and not op.is_applied(AFTER_BOTH)


def test_line_endings_come_from_the_file():
    crlf = AFTER_BOTH.replace("\n", "\r\n")
    new = op.apply(crlf, DOORS)
    assert "\r\n" in new and "\n" not in new.replace("\r\n", "")
    lf = op.apply(AFTER_BOTH, DOORS)
    assert "\r" not in lf


def test_a_half_applied_file_is_refused():
    once = op.apply(AFTER_BOTH, DOORS)
    with pytest.raises(md.CaddyfileShapeError):
        op.apply(once.replace(op.MARK_END, "# (hand edited)"), DOORS)
    with pytest.raises(md.CaddyfileShapeError):
        op.apply(once.replace("@oauth_prm", "@something_else"), DOORS)


def test_a_file_that_changed_shape_is_refused_and_nothing_is_written():
    with pytest.raises(md.CaddyfileShapeError):
        op.apply(LIVE.replace("reverse_proxy 127.0.0.1:3000", "reverse_proxy 127.0.0.1:3001"), DOORS)
    with pytest.raises(md.CaddyfileShapeError):
        op.apply(LIVE.replace("(hatchloop_site) {", "(hatchloop_site_renamed) {"), DOORS)


def test_the_diff_is_additions_only_and_quotes_none_of_the_live_file_at_zero_context():
    diff = md.unified_diff(AFTER_BOTH, op.apply(AFTER_BOTH, DOORS), context=0)
    body = [l for l in diff.splitlines() if l and l[0] in "+-" and not l.startswith(("+++", "---"))]
    assert body and all(l.startswith("+") for l in body)


def test_the_committed_patch_adds_exactly_the_block_and_quotes_none_of_the_live_file():
    """deploy/caddy/oauth_prm.patch was written by `install_mcp_direct.py plan --change oauth_prm --patch-context 0`
    against the real Caddyfile (sha256 43c377cb9fe5999a..., 2026-10-04): additions only, so it is safe in a public repo."""
    patch = (CADDY / "oauth_prm.patch").read_text(encoding="utf-8").replace("\r\n", "\n")
    body = [l for l in patch.splitlines() if l and l[0] in "+-" and not l.startswith(("+++", "---"))]
    assert body and all(l.startswith("+") for l in body)
    assert "\n".join(l[1:] for l in body) + "\n" == op.prm_block()


def test_the_probes_prove_the_fix_and_what_must_not_move():
    checks = op.post_checks(DOORS)
    by_url = {(m, u): (b, s, t) for m, u, b, s, t in checks}
    # the fix: every door, the full server and the bare path are named as the SITE URL
    for d in DOORS + ["agent-broker"]:
        _b, status, text = by_url[("GET", f"https://hatchloop.dev{PRM}/mcp/{d}")]
        assert status == 200 and text == f'"resource":"https://hatchloop.dev/mcp/{d}"', d
    assert by_url[("GET", f"https://hatchloop.dev{PRM}")][2] == '"resource":"https://hatchloop.dev"'
    # not moved
    assert by_url[("GET", f"https://api.hatchloop.dev{PRM}/mcp")][2] == '"resource":"https://api.hatchloop.dev/mcp"'
    assert ("GET", "https://hatchloop.dev/.well-known/oauth-authorization-server") in by_url
    assert ("GET", "https://hatchloop.dev/.well-known/mcp.json") in by_url
    assert ("GET", "https://hatchloop.dev/") in by_url and ("GET", "https://api.hatchloop.dev/health") in by_url
    for method, _u, body, _s, _t in checks:
        if body is not None:
            assert method == "POST" and body["method"] == "initialize", "a probe never calls a tool, sends a message or places a call"


def test_the_probes_discriminate_the_defect_from_the_fix():
    """Each probe on the site host asks for the SITE URL as `resource`. Through the Next.js rewrite the origin sees the API
    host, which is exactly what the probe refuses; a probe that also accepted that answer could not tell the change
    from no change."""
    for m, u, _b, _s, text in op.post_checks(DOORS):
        if u.startswith("https://hatchloop.dev" + PRM):
            assert "api.hatchloop.dev" not in text and text.startswith('"resource":"https://hatchloop.dev'), u


def test_the_origin_answers_what_the_probes_expect_when_it_is_asked_on_the_host_the_probe_names():
    """Drive the origin with the Host header Caddy will pass, and compare with what the installer wants."""
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app)
    checked = 0
    for m, url, _body, status, text in op.post_checks(DOORS):
        host, _, path = url.removeprefix("https://").partition("/")
        path = "/" + path
        if m != "GET" or not path.startswith("/.well-known/"):
            continue
        r = c.get(path, headers={"host": host})
        assert r.status_code == status and text in r.text, (url, r.status_code, r.text[:200])
        checked += 1
    assert checked >= len(DOORS) + 5


def test_the_installer_works_the_third_change_through_the_same_flow():
    inst = _installer()

    class Args:
        change = "oauth_prm"

    mod, names = inst._change(Args())
    assert mod is op and names == sorted(profiles.PROFILES)

    class FakeConn:
        def __init__(self, text):
            self.text = text

        def get_bytes(self, remote):
            return self.text.encode("utf-8")

    plan = inst.make_plan(FakeConn(AFTER_BOTH), mod, names)
    assert plan["already_applied"] is False and "@oauth_prm" in plan["new"]
    assert inst.make_plan(FakeConn(plan["new"]), mod, names)["already_applied"] is True
    assert "oauth_prm" in inst.main.__globals__["__doc__"], "the installer's usage text names the change"


def test_the_documents_this_change_does_not_route_still_have_the_nextjs_rewrite_to_fall_back_on():
    """Everything under /.well-known/ except the protected-resource family keeps going through the site's rewrite. Drift
    guard against the sibling repo; skips when it is not checked out."""
    cfg = os.environ.get("NEXT_CONFIG_PATH") or str(ROOT.parent / "web_hatchloop_v2" / "next.config.ts")
    if not os.path.exists(cfg):
        pytest.skip("web_hatchloop_v2/next.config.ts not present in this checkout")
    text = Path(cfg).read_text(encoding="utf-8")
    assert re.search(r'source:\s*"/\.well-known/:path\*",\s*destination:\s*"https://api\.hatchloop\.dev/\.well-known/:path\*"', text), (
        "the site no longer proxies /.well-known/* to the origin: mcp.json, agent-card.json, the authorization-server "
        "metadata, glama.json and the x402 files would 404 on hatchloop.dev")
