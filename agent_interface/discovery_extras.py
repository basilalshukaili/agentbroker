"""Three discovery files that scanners and directories ask for, each served only when it is TRUE (verdict item A8).

Measured in the production Caddy logs, 2026-09-30 to 2026-10-04 (counts only): `/.well-known/glama.json` 268 x 404 from
Glama's own checker, `/.well-known/x402.json` 101 x 404, `/.well-known/mcp/server-card.json` 36 x 404. The verdict's
wording ("serve glama.json, the server card and an x402.json alias") hid the exact paths; these are the ones asked for.

THE RULE FOR ALL THREE: a document exists exactly when the thing it describes exists. A route that always answers 200
would silence the scanner and make a claim in doing it, which is the opposite of what a directory scorer is measuring.

  /.well-known/glama.json          Glama issues a CLAIM TOKEN to the account that owns the listing (it appears in that
                                   account's claim panel; it is public by design and carries no personal data) and
                                   re-checks that this exact file is served from the connector's origin, so the file is
                                   `{"$schema": ".../connector.json", "claim": "<token>"}` and nothing else. We hold no
                                   token until the founder reads one off the panel, so until GLAMA_CLAIM_TOKEN is set this
                                   is a 404, and a value that is not shaped like a token is never echoed.
  /.well-known/mcp/server-card.json  An MCP server card (SEP-1649, a DRAFT: not part of the 2026-07-28 revision, whose
                                   `server/discover` is the in-protocol answer). Built from the same sources as the
                                   handshake and tools/list (agent_interface/well_known.get_server_card).
  /.well-known/x402.json           The same document as /.well-known/x402, byte for byte, or 404 while that is. The one
                                   writer is billing.x402_gate.discovery_document (branch feat/x402-advertise-20261003):
                                   on a build without it this is a 404, and it lights up when that function exists.
                                   `mpp` and `payment-manifest` are probed too (about 100 times in four days) and are
                                   deliberately NOT aliased: we implement neither, and an answer would be a claim.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("smb_broker.discovery")

router = APIRouter()

GLAMA_SCHEMA = "https://glama.ai/mcp/schemas/connector.json"
# Glama's published shape: the prefix and exactly 32 characters of [A-Za-z0-9_-].
_GLAMA_TOKEN = re.compile(r"^glama_claim_[A-Za-z0-9_-]{32}$")

_PUBLIC = {"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"}
_warned_malformed = False


def glama_claim_token() -> Optional[str]:
    """The configured claim token, or None. The default is the literal "none", not an empty string: the deploy
    check (scripts/check_deploy_env.py in the hatchloop tree) treats an empty default as 'a variable this
    deployment REQUIRES', and nothing should have to set this one."""
    global _warned_malformed
    raw = os.getenv("GLAMA_CLAIM_TOKEN", "none").strip()
    if not raw or raw.lower() == "none":
        return None
    if _GLAMA_TOKEN.fullmatch(raw):
        return raw
    if not _warned_malformed:
        _warned_malformed = True
        log.warning("glama_claim_token_malformed length=%d - not served", len(raw))     # length only, never the value
    return None


def _not_found() -> Response:
    return JSONResponse({"detail": "Not Found"}, status_code=404)


@router.api_route("/.well-known/glama.json", methods=["GET", "HEAD"], include_in_schema=False)
async def glama_claim():
    token = glama_claim_token()
    if token is None:
        return _not_found()
    return JSONResponse({"$schema": GLAMA_SCHEMA, "claim": token}, headers=_PUBLIC)


@router.api_route("/.well-known/mcp/server-card.json", methods=["GET", "HEAD"], tags=["Discovery"])
async def server_card():
    """MCP server card (SEP-1649 draft shape): identity, endpoint, protocol versions, capabilities, who needs an
    account. Public and cacheable: it says nothing about who is asking."""
    from agent_interface.well_known import get_server_card
    return JSONResponse(get_server_card(), headers=_PUBLIC)


def x402_document() -> Optional[dict]:
    """The document /.well-known/x402 serves, or None when this host does not take x402 payment (or this build has
    no such document). Never built here: a second builder is a second place to disagree about the address."""
    try:
        from billing import x402_gate
        build = getattr(x402_gate, "discovery_document", None)
        return build() if callable(build) else None
    except Exception:                                      # noqa: BLE001 - a discovery file must not 500
        log.exception("x402_discovery_document_failed")
        return None


@router.api_route("/.well-known/x402.json", methods=["GET", "HEAD"], include_in_schema=False)
async def x402_alias():
    doc = x402_document()
    if doc is None:
        return _not_found()
    return JSONResponse(doc)
