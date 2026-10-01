"""smb-broker.onrender.com is retired. Every request is sent on to the live server.

AgentBroker moved to https://api.hatchloop.dev on 2026-09-22. Directory listings written before that
still advertise the Render address, and callers who found AgentBroker there kept calling it - POST
/mcp included - long after the app behind it went stale. This process is all that runs there now.

  * every method and every path answers HTTP 308 with
        Location: https://api.hatchloop.dev<same path and query>
    308 (not 301/302) because a 301/302 lets a client turn POST into GET, which would silently break
    MCP's POST /mcp; 308 says "repeat the same request, same method and body, over there".
  * GET /health answers 200 with a pointer, so Render's own health check passes.
  * OPTIONS is answered 204 (a CORS preflight cannot follow a redirect), so a browser-based client
    still reaches the live server's own CORS rules on the redirected request.
  * it logs the method and the path ONLY - never the query string or any header, because those are
    where a caller's key ends up when it is pasted somewhere it should not be.
  * it holds no secrets and calls nothing. Standard library only.
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

TARGET = os.environ.get("TARGET_ORIGIN", "https://api.hatchloop.dev").rstrip("/")
_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")


def location_for(request_target: str) -> str:
    """The live-server URL for a request-target, keeping the path and query exactly as sent."""
    target = request_target or "/"
    if not target.startswith("/"):
        # absolute-form ("GET http://host/x HTTP/1.1") or garbage: keep only what follows the authority
        parts = urlsplit(target)
        target = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
        if not target.startswith("/"):
            target = "/" + target
    # Control characters cannot appear in a request-target the server accepted, but a Location header
    # is the one place a stray CR/LF would be a header-injection, so refuse them outright.
    target = "".join(ch for ch in target if ch >= " " and ch != "\x7f")
    return TARGET + target


class Redirect(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "retired-redirect"
    sys_version = ""

    def _send(self, status: int, headers: dict, body: bytes = b"") -> None:
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        # A POST body we never read would be parsed as the next request on a kept-alive connection.
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _handle(self) -> None:
        path_only = urlsplit(self.path).path or "/"
        print(f"{self.command} {path_only[:80]}", file=sys.stdout, flush=True)
        if path_only == "/health" and self.command in ("GET", "HEAD"):
            body = json.dumps({
                "status": "redirected",
                "message": "smb-broker.onrender.com is retired; every other path answers 308 to the live server.",
                "live": TARGET,
                "health": TARGET + "/health",
                "mcp": TARGET + "/mcp",
            }).encode()
            self._send(200, {"Content-Type": "application/json", "Cache-Control": "no-store"}, body)
            return
        if self.command == "OPTIONS":
            self._send(204, {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": self.headers.get("Access-Control-Request-Headers", "*"),
                "Access-Control-Max-Age": "86400",
            })
            return
        loc = location_for(self.path)
        self._send(308, {"Location": loc, "Cache-Control": "no-store",
                         "Content-Type": "text/plain; charset=utf-8"},
                   f"Moved permanently to {TARGET}\n".encode())

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _handle

    def log_message(self, *args, **kwargs) -> None:   # the default logger prints the full request line
        return


def serve(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("0.0.0.0", port), Redirect)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    print(f"redirect server on :{port} -> {TARGET}", flush=True)
    serve(port).serve_forever()
