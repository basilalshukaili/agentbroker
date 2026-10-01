#!/usr/bin/env bash
# Runs ON the box. Two modes, both refusing to act on a Caddyfile that is not the one the plan was made from.
#
#   remote_install.sh check   <candidate> <expected-live-sha256>
#       Read-only. `caddy adapt` parses and adapts the candidate WITHOUT provisioning it, so it opens no
#       log file and creates nothing under /var/log/caddy.
#
#   remote_install.sh apply   <candidate> <expected-live-sha256>
#       Back up -> `caddy validate` -> fix log ownership -> install -> `systemctl reload caddy` (never restart).
#       Any failure restores the backup and reloads it. Prints BACKUP=<path> on success.
#
#   remote_install.sh restore <backup>
#       Put a backup back and reload.
#
# CRLF/LF: the candidate is installed byte for byte; the line ending was decided by the planner from the
# live file itself.
set -euo pipefail
LIVE=/etc/caddy/Caddyfile
LOGDIR=/var/log/caddy
mode="${1:-}"; shift || true

restore() {
  local bak="$1"
  cp -p "$bak" "$LIVE"
  systemctl reload caddy
  echo "RESTORED from $bak"
}

case "$mode" in
  check)
    cand="$1"; want="$2"
    have=$(sha256sum "$LIVE" | cut -d' ' -f1)
    if [ "$have" != "$want" ]; then echo "REFUSE: live Caddyfile is $have, the plan was made from $want"; exit 3; fi
    caddy adapt --config "$cand" --adapter caddyfile > /dev/null
    echo "CHECK OK: candidate adapts cleanly (nothing provisioned, nothing written outside /tmp)"
    ;;
  apply)
    cand="$1"; want="$2"
    have=$(sha256sum "$LIVE" | cut -d' ' -f1)
    if [ "$have" != "$want" ]; then echo "REFUSE: live Caddyfile is $have, the plan was made from $want"; exit 3; fi
    systemctl is-active --quiet caddy || { echo "REFUSE: caddy is not active before the change"; exit 4; }
    bak="$LIVE.bak-mcp-direct-$(date -u +%Y%m%dT%H%M%SZ)"
    cp -p "$LIVE" "$bak"
    echo "BACKUP=$bak"
    # `caddy validate` PROVISIONS the config, so it opens (and creates) the log files as whoever runs it.
    # Run as root it makes a root-owned api.hatchloop.dev.log that caddy (uid 999) then cannot open, and
    # the reload fails on a perfectly valid config. Create it correctly first, and fix anything validate made.
    touch "$LOGDIR/api.hatchloop.dev.log"
    chown caddy:caddy "$LOGDIR/api.hatchloop.dev.log"
    chmod 640 "$LOGDIR/api.hatchloop.dev.log"
    if ! caddy validate --config "$cand" --adapter caddyfile; then
      echo "VALIDATE FAILED; live config untouched"; exit 5
    fi
    find "$LOGDIR" -maxdepth 1 -user root -exec chown caddy:caddy {} +
    install -m 644 -o root -g root "$cand" "$LIVE"
    if ! systemctl reload caddy; then
      echo "RELOAD FAILED"; restore "$bak"; exit 6
    fi
    sleep 2
    if ! systemctl is-active --quiet caddy; then
      echo "CADDY NOT ACTIVE AFTER RELOAD"; restore "$bak"; exit 7
    fi
    echo "APPLIED; caddy active"
    ;;
  restore)
    restore "$1"
    ;;
  *)
    echo "usage: $0 check|apply <candidate> <live-sha256> | restore <backup>"; exit 2
    ;;
esac
