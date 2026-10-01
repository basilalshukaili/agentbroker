"""The Caddy change is prepared, tested offline, and NOT applied here.

deploy/caddy/mcp_direct.py rewrites the text of /etc/caddy/Caddyfile; install_mcp_direct.py does the ssh;
remote_scrub_logs.py blanks key-header values already on disk. These tests run the real transforms against
a fixture with the same anchors as the live file, and the installer's control flow against a fake box.
Nothing here opens a connection.
"""
from __future__ import annotations

import gzip
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CADDY = ROOT / "deploy" / "caddy"
sys.path.insert(0, str(CADDY))

import mcp_direct as md  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, CADDY / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scrub = _load("remote_scrub_logs")
installer = _load("install_mcp_direct")

from agent_interface import profiles  # noqa: E402

DOORS = sorted(profiles.PROFILES)

# Same anchors as the live file (trimmed): the snippet with the moved-dashboard handle, the catch-all
# to Next.js, the access-log filter, the host blocks, and api.hatchloop.dev with no log of its own.
LIVE = """\
(umami_proxy) {
	@umami path /u/script.js /u/api/send
	handle @umami {
		uri strip_prefix /u
		reverse_proxy 127.0.0.1:8130
	}
}

(hatchloop_site) {
	encode zstd gzip
	header {
		-Server
	}

	@tmos_moved path_regexp ^/(?:os|tm)-[A-Za-z0-9]+(?:/.*)?$
	handle @tmos_moved {
		header Content-Type "text/plain; charset=utf-8"
		respond "TechMate OS has moved to techmate.om" 410
	}

	handle {
		reverse_proxy 127.0.0.1:3000 {
			header_up X-Forwarded-Proto https
		}
	}

	log {
		output file /var/log/caddy/hatchloop.dev.log {
			roll_size 20MiB
			roll_keep 10
		}
		format filter {
			wrap json
			request>uri query {
				delete b
				delete t
			}
		}
	}
}

hatchloop.dev {
	import umami_proxy
	import hatchloop_site
}

www.hatchloop.dev {
	redir https://hatchloop.dev{uri} permanent
}

api.hatchloop.dev {
	encode zstd gzip
	header {
		-Server
	}
	reverse_proxy 127.0.0.1:8010 {
		header_up X-Forwarded-Proto https
	}
}

delivery.techmate.om {
	respond "x" 503
}
"""


def _directives(text: str) -> str:
    """The Caddyfile with comment lines removed - comments explain, only directives act."""
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))


def _bash_works() -> bool:
    exe = shutil.which("bash")
    if not exe:
        return False
    try:   # on Windows `bash` can be the WSL launcher stub with no distribution behind it
        probe = subprocess.run([exe, "--version"], capture_output=True, timeout=15)
        return probe.returncode == 0 and b"GNU bash" in probe.stdout
    except Exception:  # noqa: BLE001
        return False


def test_the_edit_adds_the_mcp_routes_before_the_catch_all():
    new = md.apply(LIVE, DOORS)
    block_at = new.index("@mcp_agent_broker")
    catch_all = new.index("reverse_proxy 127.0.0.1:3000")
    moved = new.index("handle @tmos_moved")
    assert moved < block_at < catch_all, "handle blocks are first-match-wins in source order"
    assert "rewrite * /mcp" in new, "/mcp/agent-broker is /mcp on the origin, as the Next.js rewrite had it"


def test_every_capability_door_is_routed_with_and_without_a_trailing_slash():
    new = md.apply(LIVE, DOORS)
    line = next(l for l in new.splitlines() if l.strip().startswith("@mcp_doors"))
    for d in DOORS:
        assert f"/mcp/{d} " in line + " " and f"/mcp/{d}/" in line, d
    assert len(DOORS) >= 5


def _handle_body(text: str, matcher: str) -> str:
    """The text of `handle @<matcher> { ... }` (one level of nested braces), comments removed."""
    start = text.index(f"handle @{matcher} {{")
    depth, i = 0, text.index("{", start)
    for j in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return _directives(text[start:j + 1])
    raise AssertionError("unbalanced block")


def test_the_trailing_slash_form_is_normalised_before_it_reaches_the_container():
    """AUDIT FINDING (gate, P2): the matcher sends /mcp/<door>/ to the container unchanged, and the
    origin has no trailing-slash route, so it answers 307 with an http:// Location - where today (via the
    Next.js rewrite) the same POST answers 200. A client that follows the redirect would resend the POST
    and its key header in clear before Caddy's own http->https redirect. The route must therefore strip
    the slash, BEFORE the proxy."""
    new = md.apply(LIVE, DOORS)
    body = _handle_body(new, "mcp_doors")
    assert "uri strip_suffix /" in body, body
    assert body.index("uri strip_suffix /") < body.index("reverse_proxy"), "the path must be fixed before proxying"
    # /mcp/agent-broker/ is rewritten to /mcp outright, so it never reaches the origin with a slash
    ab = _handle_body(new, "mcp_agent_broker")
    assert "rewrite * /mcp" in ab and "uri strip_suffix" not in ab


def test_the_installer_probes_the_slash_form_of_every_door_and_agent_broker():
    """The first version of this change shipped with probes for the no-slash URLs only, which is why the
    307 above could not have been caught by the install's automatic rollback."""
    checks = md.post_checks(DOORS)
    wanted = {f"https://hatchloop.dev/mcp/{d}/" for d in DOORS} | {"https://hatchloop.dev/mcp/agent-broker/"}
    got = {c[1]: c for c in checks if c[0] == "POST"}
    for url in wanted:
        assert url in got, f"no POST probe for {url}"
        method, _, body, status, text = got[url]
        assert status == 200 and '"serverInfo"' in text, (url, status, text)
        assert body["method"] == "initialize"


def test_a_new_profile_is_routed_automatically_because_the_list_is_generated():
    new = md.apply(LIVE, DOORS + ["brand-new-door"])
    assert "/mcp/brand-new-door" in new


def test_retired_servers_and_other_mcp_pages_still_go_to_nextjs():
    new = md.apply(LIVE, DOORS)
    for retired in ("data-enrichment", "pdf-generator", "url-shortener", "ai-visibility",
                    "driftwatch", "email-sending"):
        assert f"/mcp/{retired}" not in new
    active = _directives(new)
    assert "/mcp/*" not in active and "path /mcp " not in active, "a wildcard would swallow them"


def test_no_trusted_proxies_and_real_ip_is_stripped():
    new = md.apply(LIVE, DOORS)
    assert "trusted_proxies" not in _directives(new), (
        "with none configured Caddy replaces a client-sent X-Forwarded-For (proved on the box by "
        "deploy/caddy/verify_caddy_claims.sh); configuring it would make the header spoofable")
    assert new.count("header_up -X-Real-IP") == 2


def test_the_key_headers_are_redacted_in_both_logs_and_the_old_filter_survives():
    new = md.apply(LIVE, DOORS)
    assert new.count("request>headers>X-Agent-Identity replace REDACTED") == 2
    assert new.count("request>headers>X-Api-Key replace REDACTED") == 2
    assert "request>uri query {" in new and "delete b" in new and "delete t" in new


def test_api_hatchloop_dev_gets_an_access_log_and_only_that_block():
    new = md.apply(LIVE, DOORS)
    api = re.search(r"^api\.hatchloop\.dev \{\n.*?^\}\n", new, re.S | re.M).group(0)
    assert "output file /var/log/caddy/api.hatchloop.dev.log" in api
    assert api.index("reverse_proxy 127.0.0.1:8010") < api.index("log {")
    other = re.search(r"^delivery\.techmate\.om \{\n.*?^\}\n", new, re.S | re.M).group(0)
    assert "log {" not in other


def test_the_api_access_log_does_not_write_one_time_tokens_from_the_query_string():
    """AUDIT FINDING (gate, P3): the hatchloop.dev log scrubs b= and t= because session tokens must not
    reach disk. The new api.hatchloop.dev log serves the emailed verification link (/keys/verify?token=...)
    and the unsubscribe link (/unsubscribe?t=...), so it needs the same scrub - and the WhatsApp
    verification handshake token too."""
    new = md.apply(LIVE, DOORS)
    api = re.search(r"^api\.hatchloop\.dev \{\n.*?^\}\n", new, re.S | re.M).group(0)
    assert "request>uri query {" in api, "the api log has no query-string filter"
    q = api[api.index("request>uri query {"):]
    q = q[:q.index("}")]
    for name in ("token", "t", "b", "hub.verify_token"):
        assert f"delete {name}\n" in q, name


def test_it_is_idempotent_byte_for_byte():
    once = md.apply(LIVE, DOORS)
    assert md.apply(once, DOORS) == once
    assert md.is_applied(once) and not md.is_applied(LIVE)


def test_line_endings_are_taken_from_the_file_not_assumed():
    crlf_live = LIVE.replace("\n", "\r\n")
    new = md.apply(crlf_live, DOORS)
    assert "\r\n" in new
    assert re.search(r"(?<!\r)\n", new) is None, "a bare LF crept into a CRLF file"
    assert md.apply(new, DOORS) == new
    assert md.apply(crlf_live, DOORS).replace("\r\n", "\n") == md.apply(LIVE, DOORS)


def test_a_changed_shape_is_refused_not_guessed():
    for broken in (
        LIVE.replace("reverse_proxy 127.0.0.1:3000", "reverse_proxy 127.0.0.1:3001"),     # catch-all moved
        LIVE.replace("wrap json", "wrap console"),                                         # filter changed
        LIVE.replace("api.hatchloop.dev {", "api2.hatchloop.dev {"),                       # block renamed
        LIVE + "\n" + LIVE[LIVE.index("api.hatchloop.dev {"):],                            # ambiguous
    ):
        with pytest.raises(md.CaddyfileShapeError):
            md.apply(broken, DOORS)


def test_a_half_applied_file_is_refused():
    once = md.apply(LIVE, DOORS)
    with pytest.raises(md.CaddyfileShapeError):
        md.apply(once.replace("\t# <<< mcp_direct\n", "", 1), DOORS)


def test_an_api_block_that_already_has_a_log_is_not_given_a_second():
    pre = LIVE.replace("reverse_proxy 127.0.0.1:8010 {\n\t\theader_up X-Forwarded-Proto https\n\t}\n",
                       "reverse_proxy 127.0.0.1:8010 {\n\t\theader_up X-Forwarded-Proto https\n\t}\n\tlog {\n\t}\n")
    with pytest.raises(md.CaddyfileShapeError):
        md.apply(pre, DOORS)


def test_an_empty_door_list_is_refused():
    with pytest.raises(ValueError):
        md.apply(LIVE, [])


def test_the_diff_is_only_additions():
    new = md.apply(LIVE, DOORS)
    removed = [l for l in md.unified_diff(LIVE, new).splitlines() if l.startswith("-") and not l.startswith("---")]
    assert removed == [], "this change must not delete or alter any existing line"


def test_post_checks_cover_every_door_and_what_must_not_move():
    checks = md.post_checks(DOORS)
    urls = [c[1] for c in checks]
    for d in DOORS:
        assert f"https://hatchloop.dev/mcp/{d}" in urls
    assert "https://hatchloop.dev/mcp/agent-broker" in urls and "https://api.hatchloop.dev/mcp" in urls
    gone = [c for c in checks if c[3] == 410]
    assert len(gone) >= 4, "the retired-server pages and the moved dashboard must keep answering 410"
    for method, url, body, status, text in checks:
        if body is not None:
            assert body["method"] == "initialize", "a probe must never call a tool, send a message or place a call"


def test_the_public_urls_match_the_nextjs_rewrites_when_that_file_is_available():
    """Drift guard against the file this replaces. Skips when the sibling repo is not checked out."""
    cfg = os.environ.get("NEXT_CONFIG_PATH") or str(ROOT.parent / "web_hatchloop_v2" / "next.config.ts")
    if not os.path.exists(cfg):
        pytest.skip("web_hatchloop_v2/next.config.ts not present in this checkout")
    text = Path(cfg).read_text(encoding="utf-8")
    sources = set(re.findall(r'source:\s*"(/mcp/[a-z0-9\-]+)"', text))
    # the keys/webhooks/llms.txt paths nested under /mcp/agent-broker/ are NOT MCP and stay in Next.js
    exact = {s for s in sources}
    new = md.apply(LIVE, DOORS)
    routed = set(re.findall(r"(/mcp/[a-z0-9\-]+)(?=[ /\n])", " ".join(
        l for l in new.splitlines() if l.strip().startswith("@mcp_"))))
    assert exact == routed, f"rewrites {sorted(exact - routed)} are not routed; routes {sorted(routed - exact)} have no rewrite"


# ---------------------------------------------------------------------------
# the log scrub
# ---------------------------------------------------------------------------

LINE = (b'{"level":"info","ts":1.7e9,"msg":"handled request","request":{"method":"POST","uri":"/mcp/agent-broker",'
        b'"headers":{"User-Agent":["node"],"X-Agent-Identity":["env:AGENTBROKER_API_KEY_PLACEHOLDER"],'
        b'"Content-Type":["application/json"]}},"status":200}\n')
KEYISH = b"eyJhZ2VudF9pZCI6ImZyZWVfeCJ9." + b"a" * 64


def test_scrub_blanks_the_value_keeps_the_length_and_keeps_the_json_valid():
    import json
    out = scrub.blank_bytes(LINE)
    assert len(out) == len(LINE)
    assert b"AGENTBROKER_API_KEY_PLACEHOLDER" not in out
    o = json.loads(out)
    assert o["request"]["headers"]["X-Agent-Identity"] == ["*" * len(b"env:AGENTBROKER_API_KEY_PLACEHOLDER")]
    assert o["request"]["headers"]["User-Agent"] == ["node"], "other headers are untouched"
    assert scrub.blank_bytes(out) == out, "idempotent"


def test_scrub_handles_every_spelling_both_headers_and_multiple_values():
    data = (b'{"headers":{"x-agent-identity":["aaa","bbb"],"X-Api-Key":["' + KEYISH + b'"],'
            b'"Authorization":["REDACTED"]}}')
    out = scrub.blank_bytes(data)
    assert b"aaa" not in out and b"bbb" not in out and KEYISH not in out
    assert b'"Authorization":["REDACTED"]' in out
    assert len(out) == len(data)


def test_scrub_leaves_already_redacted_lines_alone():
    clean = b'{"headers":{"X-Agent-Identity":"REDACTED","X-Api-Key":["REDACTED"]}}'
    assert scrub.blank_bytes(clean) == clean
    assert scrub.scan_bytes(clean) == {"key_shaped": 0, "other": 0}


def test_scrub_counts_key_shaped_values_separately_and_never_returns_them():
    t = scrub.scan_bytes(LINE + b'{"headers":{"X-Agent-Identity":["' + KEYISH + b'"]}}\n')
    assert t == {"key_shaped": 1, "other": 1}


def test_scrub_apply_edits_a_plain_log_in_place_and_a_gz_log_atomically(tmp_path, monkeypatch):
    plain = tmp_path / "hatchloop.dev.log"
    gz = tmp_path / "hatchloop.dev-2026-09-30T05-13-06.411-size.log.gz"
    body = LINE * 3 + b'{"ok":true}\n'
    plain.write_bytes(body)
    with gzip.open(gz, "wb") as fh:
        fh.write(body)
    inode = plain.stat().st_ino
    monkeypatch.setattr(scrub, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(os, "chown", lambda *a, **k: None, raising=False)   # no chown on Windows
    assert scrub.main([]) == 0                         # dry run changes nothing
    assert plain.read_bytes() == body
    assert scrub.main(["--apply"]) == 0
    assert b"PLACEHOLDER" not in plain.read_bytes() and len(plain.read_bytes()) == len(body)
    assert plain.stat().st_ino == inode, "the active log must be edited in place - Caddy holds it open"
    assert b"PLACEHOLDER" not in gzip.open(gz, "rb").read()
    assert not list(tmp_path.glob("*.tmp"))
    assert scrub.main([]) == 0
    assert scrub.scan_bytes(plain.read_bytes()) == {"key_shaped": 0, "other": 0}


def test_scrub_output_never_contains_a_header_value(tmp_path, monkeypatch, capsys):
    (tmp_path / "hatchloop.dev.log").write_bytes(LINE)
    monkeypatch.setattr(scrub, "LOG_DIR", str(tmp_path))
    scrub.main([])
    out = capsys.readouterr().out
    assert "PLACEHOLDER" not in out and "AGENTBROKER" not in out and "env:" not in out


# ---------------------------------------------------------------------------
# the installer's control flow, against a fake box
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self, live_text, apply_exit=0):
        self.live = live_text.encode("utf-8")
        self.commands, self.uploads = [], []
        self.apply_exit = apply_exit

    def get_bytes(self, remote):
        return self.live

    def put(self, local, remote, timeout=60):
        self.uploads.append(remote)

    def run(self, command, timeout=60, check=True):
        self.commands.append(command)
        out, code = "", 0
        if "remote_install.sh check" in command:
            out = "CHECK OK"
        elif "remote_install.sh apply" in command:
            out, code = ("BACKUP=/etc/caddy/Caddyfile.bak-mcp-direct-X\nAPPLIED; caddy active", 0) \
                if self.apply_exit == 0 else ("REFUSE", self.apply_exit)
        elif "remote_install.sh restore" in command:
            out = "RESTORED"
        return subprocess.CompletedProcess(command, code, out, "")


class Args:
    yes = True
    apply = False
    write_patch = None


def test_install_refuses_without_yes(capsys):
    a = Args()
    a.yes = False
    c = FakeConn(LIVE)
    assert installer.cmd_install(c, a) == 2
    assert not any("apply" in x for x in c.commands)


def test_install_is_a_no_op_when_already_applied():
    c = FakeConn(md.apply(LIVE, DOORS))
    assert installer.cmd_install(c, Args()) == 0
    assert not any("apply" in x for x in c.commands)


def test_install_passes_the_live_hash_so_a_changed_file_is_refused_by_the_box(monkeypatch):
    c = FakeConn(LIVE, apply_exit=3)
    monkeypatch.setattr(installer, "run_probes", lambda d: pytest.fail("probed after a refused install"))
    assert installer.cmd_install(c, Args()) == 3
    import hashlib
    want = hashlib.sha256(LIVE.encode()).hexdigest()
    assert any(want in x and "remote_install.sh apply" in x for x in c.commands)


def test_a_failing_probe_after_the_reload_restores_the_backup(monkeypatch):
    c = FakeConn(LIVE)
    calls = []

    def probes(doors):
        calls.append(1)
        return ["https://hatchloop.dev/mcp/agent-broker"] if len(calls) == 1 else []
    monkeypatch.setattr(installer, "run_probes", probes)
    monkeypatch.setattr(installer.time, "sleep", lambda s: None)
    assert installer.cmd_install(c, Args()) == 1
    restore = [x for x in c.commands if "remote_install.sh restore" in x]
    assert restore and "Caddyfile.bak-mcp-direct-X" in restore[0]
    assert len(calls) == 2, "probes must run again after the restore"


def test_a_clean_install_probes_once_and_does_not_restore(monkeypatch):
    c = FakeConn(LIVE)
    monkeypatch.setattr(installer, "run_probes", lambda d: [])
    monkeypatch.setattr(installer.time, "sleep", lambda s: None)
    assert installer.cmd_install(c, Args()) == 0
    assert not any("restore" in x for x in c.commands)


def test_the_remote_scripts_have_valid_syntax_and_lf_endings():
    for sh in ("remote_install.sh", "verify_caddy_claims.sh"):
        data = (CADDY / sh).read_bytes()
        assert b"\r\n" not in data, f"{sh} has CRLF endings and will not run under bash"
        if _bash_works():
            # via stdin: a Windows path means nothing to a WSL bash, and the content is what matters
            r = subprocess.run(["bash", "-n"], input=data, capture_output=True)
            if b"WSL" in r.stderr:      # the Windows WSL launcher stub, no distribution behind it
                continue
            assert r.returncode == 0, (sh, r.stderr)
    assert b"\r\n" not in (CADDY / "remote_scrub_logs.py").read_bytes()


def test_remote_install_reloads_and_never_restarts():
    sh = (CADDY / "remote_install.sh").read_text(encoding="utf-8")
    assert "systemctl reload caddy" in sh
    assert "restart caddy" not in sh and "systemctl restart" not in sh
    assert "sha256sum" in sh and "REFUSE" in sh
    assert sh.index("caddy validate") < sh.index('install -m 644 -o root -g root "$cand" "$LIVE"')


def test_the_committed_patch_applies_cleanly_to_the_live_shape():
    patch = CADDY / "mcp_direct.patch"
    assert patch.exists()
    text = patch.read_text(encoding="utf-8")
    assert "mcp_direct" in text and "+\t@mcp_agent_broker" in text
    assert not [l for l in text.splitlines() if l.startswith("-") and not l.startswith("---")], (
        "the patch must be additions only")
    quoted = [l for l in text.splitlines()
              if not (l.startswith("+") or l.startswith("@@") or l.startswith("---"))]
    assert quoted == [], (
        "the committed patch must quote NONE of the live Caddyfile (zero-context diff): its comments "
        "describe internal systems and this repo is public. The full-context patch lives in the "
        "private ops tree.")
