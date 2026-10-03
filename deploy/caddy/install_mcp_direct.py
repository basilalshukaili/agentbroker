#!/usr/bin/env python3
"""Plan / install / verify / roll back / scrub the Caddy change in deploy/caddy/mcp_direct.py.

    python deploy/caddy/install_mcp_direct.py plan              # read-only: diff + `caddy adapt` on the box
    python deploy/caddy/install_mcp_direct.py scrub             # read-only: counts of key-header values in logs
    python deploy/caddy/install_mcp_direct.py install --yes     # MUTATES: backup, validate, reload, probe, auto-rollback
    python deploy/caddy/install_mcp_direct.py scrub --apply --yes   # MUTATES: blank key-header values in logs
    python deploy/caddy/install_mcp_direct.py verify            # read-only: the public-URL probes
    python deploy/caddy/install_mcp_direct.py rollback --yes    # restore the newest mcp-direct backup, reload

    Add `--change retired` to plan / install / verify to work on the SECOND change instead (deploy/caddy/
    mcp_retired.py: the retired MCP doors answered by the origin's tombstone). Deploy the origin first.

Connection: --target user@host and --key PATH, or VPS_SSH_TARGET / VPS_SSH_KEY, or --env-file PATH with
VPS_IP (or VPS_HOST) and optionally VPS_USER, plus --key. No host or credential is stored in this repo.

RULES THE INSTALLER ENFORCES (they are this estate's, learned the hard way):
  * a remote script is uploaded and run BY PATH, never built inside an ssh command string (a nested
    heredoc once exited 0 having changed nothing);
  * it refuses to act unless the live Caddyfile still hashes to what `plan` saw;
  * it reloads, never restarts;
  * after the reload it probes the PUBLIC urls from here (JSON-RPC handshakes and GETs only - no tool
    call, no message, no phone call), and on any failure restores the backup and reloads;
  * line endings are taken from the file, not assumed;
  * nothing it prints contains a header value or a credential.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

import mcp_direct  # noqa: E402
import mcp_retired  # noqa: E402

LIVE = "/etc/caddy/Caddyfile"
REMOTE_TMP = "/tmp/_mcp_direct"


class Conn:
    def __init__(self, target: str, key: str) -> None:
        self.target, self.key = target, key

    def _ssh_base(self) -> list:
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-i", self.key, self.target]

    def run(self, command: str, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
        p = subprocess.run(self._ssh_base() + [command], capture_output=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        if check and p.returncode != 0:
            raise SystemExit(f"remote command failed ({p.returncode}): {command}\n{p.stdout}\n{p.stderr}")
        return p

    def put(self, local: str, remote: str, timeout: int = 60) -> None:
        p = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-i", self.key,
                            local, f"{self.target}:{remote}"],
                           capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
        if p.returncode != 0:
            raise SystemExit(f"scp failed: {p.stdout}\n{p.stderr}")

    def get_bytes(self, remote: str) -> bytes:
        p = subprocess.run(self._ssh_base() + [f"cat {remote}"], capture_output=True, timeout=60)
        if p.returncode != 0:
            raise SystemExit(f"could not read {remote}: {p.stderr.decode('utf-8', 'replace')}")
        return p.stdout


def _read_env(path: str, names: list) -> dict:
    out = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        k, _, v = line.partition("=")
        if k.strip() in names and v.strip():
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def connect(args) -> Conn:
    key = args.key or os.environ.get("VPS_SSH_KEY", "")
    target = args.target or os.environ.get("VPS_SSH_TARGET", "")
    if not target and args.env_file:
        e = _read_env(args.env_file, ["VPS_IP", "VPS_HOST", "VPS_USER"])
        host = e.get("VPS_IP") or e.get("VPS_HOST")
        if host:
            target = f"{e.get('VPS_USER', 'root')}@{host}"
    if not (target and key):
        raise SystemExit("need --target and --key (or VPS_SSH_TARGET / VPS_SSH_KEY, or --env-file + --key)")
    return Conn(target, key)


def _doors() -> list:
    from agent_interface import profiles
    return sorted(profiles.PROFILES)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _change(args) -> tuple:
    """(module, names) for --change: the module that transforms the Caddyfile and the doors it routes."""
    if getattr(args, "change", "direct") == "retired":
        from agent_interface import retired_doors
        return mcp_retired, sorted(retired_doors.RETIRED_DOORS)
    return mcp_direct, _doors()


def make_plan(conn: Conn, mod=mcp_direct, names=None) -> dict:
    names = _doors() if names is None else names
    live_bytes = conn.get_bytes(LIVE)
    live = live_bytes.decode("utf-8")
    new = mod.apply(live, names)
    return {
        "live_sha256": _sha(live_bytes),
        "live": live,
        "new": new,
        "new_bytes": new.encode("utf-8"),
        "eol": "CRLF" if mod.detect_eol(live) == "\r\n" else "LF",
        "already_applied": mod.is_applied(live),
    }


def cmd_plan(conn: Conn, args) -> int:
    mod, names = _change(args)
    plan = make_plan(conn, mod, names)
    print(f"live Caddyfile: sha256 {plan['live_sha256'][:16]}..., line endings {plan['eol']}, "
          f"already applied: {plan['already_applied']}")
    if plan["already_applied"]:
        print("nothing to do.")
        return 0
    print(mod.unified_diff(plan["live"], plan["new"], "Caddyfile"))
    if args.write_patch:
        Path(args.write_patch).write_text(
            mod.unified_diff(plan["live"], plan["new"], "Caddyfile", context=args.patch_context),
            encoding="utf-8", newline="\n")
        print(f"patch written to {args.write_patch} (context lines: {args.patch_context})")
    with tempfile.TemporaryDirectory() as td:
        cand = Path(td) / "Caddyfile.candidate"
        cand.write_bytes(plan["new_bytes"])
        conn.run(f"mkdir -p {REMOTE_TMP} && chmod 700 {REMOTE_TMP}")
        conn.put(str(cand), f"{REMOTE_TMP}/Caddyfile.candidate")
        conn.put(str(HERE / "remote_install.sh"), f"{REMOTE_TMP}/remote_install.sh")
        p = conn.run(f"bash {REMOTE_TMP}/remote_install.sh check {REMOTE_TMP}/Caddyfile.candidate "
                     f"{plan['live_sha256']}", check=False)
        print(p.stdout.strip() or p.stderr.strip())
        conn.run(f"rm -rf {REMOTE_TMP}", check=False)
        return p.returncode


def run_probes(doors: list, mod=mcp_direct) -> list:
    import httpx
    failures = []
    with httpx.Client(timeout=20.0, follow_redirects=False) as c:
        for method, url, body, want_status, want_text in mod.post_checks(doors):
            try:
                r = c.request(method, url, json=body, headers={"user-agent": "mcp-direct-verify/1"})
                ok = r.status_code == want_status and (not want_text or want_text in r.text)
                detail = f"{r.status_code}"
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, type(exc).__name__
            print(f"  {'ok  ' if ok else 'FAIL'} {method} {url} -> {detail} (want {want_status})")
            if not ok:
                failures.append(url)
    return failures


def _probe(names: list, mod) -> list:
    """run_probes for the chosen change. The first change keeps the one-argument call it always had."""
    return run_probes(names) if mod is mcp_direct else run_probes(names, mod)


def cmd_verify(conn: Conn, args) -> int:
    mod, names = _change(args)
    failures = _probe(names, mod)
    print("all probes pass" if not failures else f"{len(failures)} probe(s) failed")
    return 1 if failures else 0


def cmd_install(conn: Conn, args) -> int:
    if not args.yes:
        print("refusing to change the live Caddyfile without --yes")
        return 2
    mod, names = _change(args)
    plan = make_plan(conn, mod, names)
    if plan["already_applied"]:
        print("already applied; nothing to do")
        return 0
    with tempfile.TemporaryDirectory() as td:
        cand = Path(td) / "Caddyfile.candidate"
        cand.write_bytes(plan["new_bytes"])
        conn.run(f"mkdir -p {REMOTE_TMP} && chmod 700 {REMOTE_TMP}")
        conn.put(str(cand), f"{REMOTE_TMP}/Caddyfile.candidate")
        conn.put(str(HERE / "remote_install.sh"), f"{REMOTE_TMP}/remote_install.sh")
    p = conn.run(f"bash {REMOTE_TMP}/remote_install.sh apply {REMOTE_TMP}/Caddyfile.candidate "
                 f"{plan['live_sha256']}", timeout=120, check=False)
    print(p.stdout.strip())
    if p.returncode != 0:
        print(p.stderr.strip())
        print(f"install failed (exit {p.returncode}); the remote script has already restored the backup "
              f"where it had changed anything")
        conn.run(f"rm -rf {REMOTE_TMP}", check=False)
        return p.returncode
    backup = next((ln.split("=", 1)[1] for ln in p.stdout.splitlines() if ln.startswith("BACKUP=")), "")
    time.sleep(2)
    print("probing the public URLs from here:")
    failures = _probe(names, mod)
    if failures:
        print(f"{len(failures)} probe(s) failed after the reload -> restoring {backup}")
        r = conn.run(f"bash {REMOTE_TMP}/remote_install.sh restore {backup}", timeout=120, check=False)
        print(r.stdout.strip() or r.stderr.strip())
        time.sleep(2)
        print("probes after restore:")
        _probe(names, mod)
        conn.run(f"rm -rf {REMOTE_TMP}", check=False)
        return 1
    conn.run(f"rm -rf {REMOTE_TMP}", check=False)
    print(f"installed; rollback is: bash remote_install.sh restore {backup}  (or: rollback --yes)")
    return 0


def cmd_rollback(conn: Conn, args) -> int:
    if not args.yes:
        print("refusing to change the live Caddyfile without --yes")
        return 2
    p = conn.run("ls -1t /etc/caddy/Caddyfile.bak-mcp-direct-* 2>/dev/null | head -1", check=False)
    backup = p.stdout.strip()
    if not backup:
        print("no mcp-direct backup found")
        return 1
    conn.run(f"mkdir -p {REMOTE_TMP}")
    conn.put(str(HERE / "remote_install.sh"), f"{REMOTE_TMP}/remote_install.sh")
    r = conn.run(f"bash {REMOTE_TMP}/remote_install.sh restore {backup}", timeout=120, check=False)
    print(r.stdout.strip() or r.stderr.strip())
    conn.run(f"rm -rf {REMOTE_TMP}", check=False)
    return r.returncode


def cmd_scrub(conn: Conn, args) -> int:
    if args.apply and not args.yes:
        print("refusing to modify log files without --yes")
        return 2
    conn.run(f"mkdir -p {REMOTE_TMP} && chmod 700 {REMOTE_TMP}")
    conn.put(str(HERE / "remote_scrub_logs.py"), f"{REMOTE_TMP}/remote_scrub_logs.py")
    p = conn.run(f"timeout 300 python3 {REMOTE_TMP}/remote_scrub_logs.py {'--apply' if args.apply else ''}",
                 timeout=330, check=False)
    print(p.stdout.strip())
    if p.returncode != 0:
        print(p.stderr.strip())
    conn.run(f"rm -rf {REMOTE_TMP}", check=False)
    return p.returncode


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "install", "verify", "rollback", "scrub"])
    ap.add_argument("--target")
    ap.add_argument("--key")
    ap.add_argument("--env-file")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--change", choices=["direct", "retired"], default="direct",
                    help="which Caddyfile change plan / install / verify work on (default: direct)")
    ap.add_argument("--apply", action="store_true", help="scrub: actually blank the values")
    ap.add_argument("--write-patch", metavar="PATH", help="plan: also write the unified diff here")
    ap.add_argument("--patch-context", type=int, default=3,
                    help="plan: context lines in --write-patch. Use 0 for a diff that quotes none of the "
                         "live file (safe for a public repo; the live Caddyfile's comments are internal)")
    args = ap.parse_args(argv)
    conn = connect(args) if args.command != "verify" else None
    if args.command == "verify":
        return cmd_verify(None, args)  # type: ignore[arg-type]
    return {"plan": cmd_plan, "install": cmd_install, "rollback": cmd_rollback,
            "scrub": cmd_scrub}[args.command](conn, args)


if __name__ == "__main__":
    sys.exit(main())
