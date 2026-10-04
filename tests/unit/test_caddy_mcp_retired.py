"""The Caddyfile change that routes the retired MCP doors to the origin: prepared, tested offline, NOT applied.

Same discipline as test_caddy_mcp_direct.py (its LIVE fixture has the live file's anchors, and the real
change is applied first because that is the live box's state): the transform is pure text, idempotent,
removes nothing, and refuses a file that no longer looks like the one it was written for.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CADDY = ROOT / "deploy" / "caddy"
sys.path.insert(0, str(CADDY))

import mcp_direct as md  # noqa: E402
import mcp_retired as mr  # noqa: E402

from agent_interface import profiles, retired_doors  # noqa: E402
from tests.unit.test_caddy_mcp_direct import LIVE, DOORS, _directives  # noqa: E402

SLUGS = sorted(retired_doors.RETIRED_DOORS)
AFTER_DIRECT = md.apply(LIVE, DOORS)               # the live box today: mcp_direct is already applied


def _installer():
    spec = importlib.util.spec_from_file_location("install_mcp_direct_t", CADDY / "install_mcp_direct.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_block_goes_before_the_catch_all_and_after_the_existing_mcp_routes():
    new = mr.apply(AFTER_DIRECT, SLUGS)
    direct_at = new.index(md.MARK_END)
    retired_at = new.index("@mcp_retired")
    catch_all = new.index("reverse_proxy 127.0.0.1:3000")
    assert direct_at < retired_at < catch_all, "handle blocks are first-match-wins in source order"


def test_it_also_applies_to_a_file_that_never_had_mcp_direct():
    assert "@mcp_retired" in mr.apply(LIVE, SLUGS)


def test_every_retired_door_is_routed_four_ways_and_only_those():
    new = mr.apply(AFTER_DIRECT, SLUGS)
    line = next(l for l in new.splitlines() if l.strip().startswith("@mcp_retired"))
    listed = line.split()[2:]
    assert sorted(listed) == sorted(
        p for d in SLUGS for p in (f"/mcp/{d}", f"/mcp/{d}/", f"/mcp/{d}/mcp", f"/mcp/{d}/mcp/"))
    for live in list(profiles.PROFILES) + ["agent-broker"]:
        assert f"/mcp/{live} " not in line + " " and f"/mcp/{live}/" not in line, f"live door {live} must not be routed here"


def test_no_wildcard_and_no_bare_mcp():
    active = _directives(mr.apply(AFTER_DIRECT, SLUGS))
    assert "/mcp/*" not in active and "path /mcp " not in active


def test_the_slash_is_stripped_before_the_proxy_and_the_real_ip_header_is_not_trusted():
    new = mr.apply(AFTER_DIRECT, SLUGS)
    start = new.index("handle @mcp_retired {")
    body = _directives(new[start:new.index(mr.MARK_END)])
    assert body.index("uri strip_suffix /") < body.index("reverse_proxy"), "the origin has no trailing-slash route"
    assert "reverse_proxy 127.0.0.1:8010" in body and "header_up -X-Real-IP" in body
    assert "trusted_proxies" not in _directives(new)


def test_it_is_idempotent_and_changes_no_existing_line():
    once = mr.apply(AFTER_DIRECT, SLUGS)
    assert mr.apply(once, SLUGS) == once
    removed = [l for l in md.unified_diff(AFTER_DIRECT, once).splitlines() if l.startswith("-") and not l.startswith("---")]
    assert removed == [], "this change must not delete or alter any existing line"
    assert mr.is_applied(once) and not mr.is_applied(AFTER_DIRECT)


def test_line_endings_come_from_the_file():
    crlf = AFTER_DIRECT.replace("\n", "\r\n")
    new = mr.apply(crlf, SLUGS)
    assert "\r\n" in new and "\n" not in new.replace("\r\n", "")


def test_a_half_applied_file_is_refused():
    once = mr.apply(AFTER_DIRECT, SLUGS)
    broken = once.replace(mr.MARK_END, "# (hand edited)")
    with pytest.raises(md.CaddyfileShapeError):
        mr.apply(broken, SLUGS)


def test_a_file_that_changed_shape_is_refused_and_nothing_is_written():
    with pytest.raises(md.CaddyfileShapeError):
        mr.apply(LIVE.replace("reverse_proxy 127.0.0.1:3000", "reverse_proxy 127.0.0.1:3001"), SLUGS)
    with pytest.raises(ValueError):
        mr.retired_block([])


def test_the_probes_handshake_and_get_only_and_cover_what_must_not_move():
    checks = mr.post_checks(SLUGS)
    for d in SLUGS:
        urls = {(c[0], c[1]): c for c in checks}
        for url in (f"https://hatchloop.dev/mcp/{d}", f"https://hatchloop.dev/mcp/{d}/",
                    f"https://hatchloop.dev/mcp/{d}/mcp"):
            m, _u, body, status, text = urls[("POST", url)]
            assert status == 200 and text == "(RETIRED)" and body["method"] == "initialize"
        assert urls[("GET", f"https://hatchloop.dev/mcp/{d}")][3:] == (410, "server_retired")
    live = {c[1] for c in checks}
    assert {"https://hatchloop.dev/mcp/agent-broker", "https://hatchloop.dev/mcp/sanctions-screening",
            "https://api.hatchloop.dev/health"} <= live
    for _m, _u, body, _s, _t in checks:
        if body is not None:
            assert body["method"] == "initialize", "a probe must never call a tool, send a message or place a call"


def test_the_installer_works_the_second_change_through_the_same_flow():
    inst = _installer()

    class Args:
        change = "retired"

    mod, names = inst._change(Args())
    assert mod is mr and names == SLUGS

    class Direct:
        change = "direct"

    mod2, names2 = inst._change(Direct())
    # The LISTED doors: the ChatGPT door is submitted on api.hatchloop.dev and is not routed on the site (the block is
    # already applied on the box, so a longer list would not route it, only make `verify` probe a URL nobody serves).
    assert mod2 is md and names2 == sorted(profiles.listed_profiles())

    class FakeConn:
        def __init__(self, text):
            self.text = text

        def get_bytes(self, remote):
            return self.text.encode("utf-8")

    plan = inst.make_plan(FakeConn(AFTER_DIRECT), mod, names)
    assert plan["already_applied"] is False and "@mcp_retired" in plan["new"]
    again = inst.make_plan(FakeConn(plan["new"]), mod, names)
    assert again["already_applied"] is True


def test_the_origin_answers_what_the_probes_expect(monkeypatch):
    """The probes and the origin must agree, or the installer would roll back a correct change: drive the
    origin with the exact initialize body the probes send and compare with what they want."""
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app)
    main._rl_buckets.clear()
    for m, url, body, status, text in mr.post_checks(SLUGS):
        if not url.startswith("https://hatchloop.dev/mcp/") or not any(f"/mcp/{d}" in url for d in SLUGS):
            continue
        path = url.replace("https://hatchloop.dev", "")
        if path.endswith("/") and not path.endswith("/mcp/"):
            path = path.rstrip("/")                      # Caddy strips the slash before proxying
        r = c.request(m, path, json=body)
        assert r.status_code == status and text in r.text, (m, url, r.status_code)
        main._rl_buckets.clear()
