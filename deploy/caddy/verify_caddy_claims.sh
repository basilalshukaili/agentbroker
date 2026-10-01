#!/usr/bin/env bash
# Throwaway proof of the two claims the Caddy change (deploy/caddy/mcp_direct.py) rests on.
#
#   1. "With no trusted_proxies configured, Caddy replaces a client-sent X-Forwarded-For with the real
#       peer address, and header_up -X-Real-IP strips X-Real-IP" - so the app can read the client's
#       address from that header and a caller cannot choose their own.
#   2. "A log `format filter` with `request>headers>X-Agent-Identity replace REDACTED` keeps the key header
#       value out of the access log" - including when the client spells the header in lower case.
#
# It starts a SEPARATE Caddy (admin API off, own storage dir, loopback ports 18080-18082) in front of a
# one-file echo server, sends spoofed headers and a recognisable fake key, and prints what the upstream
# received and what reached the log file. It touches no production config, certificate, port or log, and
# removes everything it made. The fake key is the literal string PROBE-NOT-A-KEY.
#
#     scp deploy/caddy/verify_caddy_claims.sh root@<box>:/tmp/ && ssh root@<box> 'bash /tmp/verify_caddy_claims.sh'
set -u
WORK=$(mktemp -d /tmp/caddy-claims-proof.XXXXXX)
cleanup() { [ -n "${CADDY_PID:-}" ] && kill "$CADDY_PID" 2>/dev/null; [ -n "${ECHO_PID:-}" ] && kill "$ECHO_PID" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

for p in 18080 18081 18082; do
  if (exec 3<>/dev/tcp/127.0.0.1/$p) 2>/dev/null; then echo "SKIP: port $p is in use"; exit 2; fi
done

cat > "$WORK/echo.py" <<'PY'
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"xff": self.headers.get("X-Forwarded-For"),
                           "real_ip": self.headers.get("X-Real-IP"),
                           "proto": self.headers.get("X-Forwarded-Proto"),
                           "key_header_reached_upstream": self.headers.get("X-Agent-Identity") is not None}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", 18081), H).serve_forever()
PY

cat > "$WORK/Caddyfile" <<EOF
{
	admin off
	auto_https off
	storage file_system $WORK/data
}
:18080 {
	reverse_proxy 127.0.0.1:18081 {
		header_up X-Forwarded-Proto https
		header_up -X-Real-IP
	}
}
:18082 {
	log {
		output file $WORK/access.log
		format filter {
			wrap json
			request>headers>X-Agent-Identity replace REDACTED
			request>headers>X-Api-Key replace REDACTED
			request>uri query {
				delete b
				delete t
			}
		}
	}
	reverse_proxy 127.0.0.1:18081
}
EOF

python3 "$WORK/echo.py" & ECHO_PID=$!
XDG_DATA_HOME="$WORK/xdg" XDG_CONFIG_HOME="$WORK/xdgc" caddy run --config "$WORK/Caddyfile" --adapter caddyfile >"$WORK/caddy.log" 2>&1 & CADDY_PID=$!
for i in $(seq 1 40); do
  curl -s -o /dev/null http://127.0.0.1:18080/ && break
  sleep 0.25
done

echo "CLAIM 1 - upstream saw, for a request that sent X-Forwarded-For: 9.9.9.9 and X-Real-IP: 8.8.8.8:"
curl -s -H 'X-Forwarded-For: 9.9.9.9' -H 'X-Real-IP: 8.8.8.8' http://127.0.0.1:18080/
echo
echo "CLAIM 1 - upstream saw, for a request that sent neither:"
curl -s http://127.0.0.1:18080/
echo

curl -s -o /dev/null -H 'X-Agent-Identity: PROBE-NOT-A-KEY' -H 'x-api-key: PROBE-NOT-A-KEY-2' \
     -H 'Authorization: Bearer PROBE-NOT-A-KEY-3' "http://127.0.0.1:18082/?b=PROBE-QUERY&keep=1"
sleep 1
echo "CLAIM 2 - access log line count: $(grep -c . "$WORK/access.log")"
echo "CLAIM 2 - lines containing the fake key text:  $(grep -c 'PROBE-NOT-A-KEY' "$WORK/access.log")   (must be 0)"
echo "CLAIM 2 - lines containing the query secret:   $(grep -c 'PROBE-QUERY' "$WORK/access.log")   (must be 0)"
# NB: the `replace` filter writes the value as the STRING "REDACTED", not the one-element array that
# Caddy's built-in Authorization redaction produces. Both keep the value off disk.
echo "CLAIM 2 - lines showing X-Agent-Identity REDACTED: $(grep -c '"X-Agent-Identity":"REDACTED"' "$WORK/access.log")   (must be 1)"
echo "CLAIM 2 - lines showing X-Api-Key REDACTED:        $(grep -c '"X-Api-Key":"REDACTED"' "$WORK/access.log")   (must be 1)"
echo "CLAIM 2 - the logged request headers (fake values only):"
grep -o '"headers":{[^}]*}' "$WORK/access.log"
