"""
Retired MCP doors, answered with a tombstone a client can read.

WHY. Six MCP servers from the earlier multi-product era are gone, and directories still list them. In
the four days to 2026-10-03 the three busiest (data-enrichment, pdf-generator, url-shortener) took 1,849
POSTs, about 440 a day, from directory scorers and uptime checkers (mcpbeat 1,146, ProofBench 208, an
undeclared `node` client 93, MCPScoringEngine 63). Each was answered `410 Gone` by the Next.js site with a
JSON body that is NOT a JSON-RPC message, so a client library parsing it as one reports a protocol
failure rather than the reason, and every scorecard that probes the address counts it as an error that
the next probe repeats (docs/reviews/2026-10-03-mcp-demand-evidence.md, item 7; the verdict's A8:
"delist the retired doors, or answer them with a single tombstone tool").

Delisting is a message to five directories and not something code can do. This is the part code can do:
a retired door speaks MCP, truthfully, in one sentence.

  * `initialize` completes, and says in its name and its instructions that the server is RETIRED;
  * `tools/list` offers exactly one tool, `server_retired`, whose description says the same and whose
    result is the live server's address and what it offers;
  * calling any other tool name returns that same information as an `isError` result the model sees;
  * GET and HEAD on the address are `410 Gone` with `Link: rel="successor-version"` - the right answer
    for a crawler and a person, who are not speaking MCP.

IT DOES NOT PRETEND THE SERVER IS ALIVE. Nothing here runs a retired tool, nothing is billed, and no
retired door counts toward the public request counters (main.py's telemetry middleware skips them), so
scorer traffic cannot inflate "agent requests" or "operations completed".

The wording matches the site's own 410 pages (web_hatchloop_v2/src/lib/retired-mcp.ts). The tool counts
in it are derived (core/tool_auth.py), never typed.

Pure functions: no network, no state.
"""
from __future__ import annotations

from typing import Any, Optional

# slug -> what the server was. The six directories the site tombstones. A retired slug may never also be a
# live door: tests/unit/test_retired_doors.py fails if one is.
RETIRED_DOORS: dict = {
    "ai-visibility": "an AEO / AI-visibility auditing server",
    "data-enrichment": "a data-enrichment and lead-intelligence server",
    "driftwatch": "a change-monitoring server",
    "email-sending": "a standalone email-sending server",
    "pdf-generator": "a PDF generation server",
    "url-shortener": "a link-shortening server",
}

LIVE_URL = "https://hatchloop.dev/mcp/agent-broker"
REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0/servers?search=agent-broker"
TOMBSTONE_TOOL = "server_retired"

# A retired door is permanent, so a day is a fair time for a scorer or proxy to remember that.
CACHE_CONTROL = "public, max-age=86400"
GONE_HEADERS = {"Cache-Control": CACHE_CONTROL, "Link": f'<{LIVE_URL}>; rel="successor-version"'}


def is_retired(slug: Any) -> bool:
    return isinstance(slug, str) and slug in RETIRED_DOORS


def path_is_retired(path: str) -> bool:
    """True for /mcp/<retired> and /mcp/<retired>/mcp (and a trailing slash): the paths this module answers."""
    parts = [p for p in path.split("/") if p]
    if len(parts) == 2 and parts[0] == "mcp":
        return is_retired(parts[1])
    if len(parts) == 3 and parts[0] == "mcp" and parts[2] == "mcp":
        return is_retired(parts[1])
    return False


def _counts() -> tuple:
    from core import tool_auth
    return tool_auth.usable_without_key(), tool_auth.total_tools()


def human_message(slug: str) -> str:
    free, total = _counts()
    return (f"The '{slug}' MCP server was retired. HatchLoop now runs one server, AgentBroker, at {LIVE_URL} - "
            "sanctions screening, company verification, trade-restriction checks, and find/verify/message/book "
            f"for small businesses. {free} of its {total} tools work with no key.")


def body(slug: str) -> dict:
    """The tombstone, as the site's 410 pages give it (same field names) - the content of the tool result and
    of the GET 410."""
    return {
        "error": "server_retired",
        "retired": slug,
        "what_it_was": RETIRED_DOORS[slug],
        "human_message": human_message(slug),
        "live_server": {"name": "dev.hatchloop/agent-broker", "url": LIVE_URL, "transport": "streamable-http",
                        "registry": REGISTRY_URL},
        "retriable": False,
    }


def tombstone_tool(slug: str) -> dict:
    return {
        "name": TOMBSTONE_TOOL,
        "description": (f"RETIRED: the '{slug}' MCP server ({RETIRED_DOORS[slug]}) no longer exists and offers no "
                        f"tools. Returns the address of the live server, {LIVE_URL}. Free, no key."),
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"title": "Server retired", "readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
        "_meta": {"hatchloop/readiness": {"state": "unavailable", "summary": f"The '{slug}' server is retired."}},
    }


def _ok(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _text_result(slug: str, is_error: bool) -> dict:
    import json
    return {"content": [{"type": "text", "text": json.dumps(body(slug), indent=2)}], "isError": is_error}


def _one(slug: str, msg: Any) -> Optional[dict]:
    from agent_interface.mcp_server import negotiate_protocol_version

    if not isinstance(msg, dict):
        return _err(None, -32600, "Request must be a JSON object")
    method = msg.get("method")
    if "id" not in msg and isinstance(method, str):
        return None                                        # a notification: never answered (JSON-RPC 2.0)
    rid = msg.get("id")
    if not isinstance(method, str) or not method:
        return None if ("result" in msg or "error" in msg) else _err(rid, -32600, "Missing 'method' field")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": negotiate_protocol_version(params),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": f"dev.hatchloop/{slug} (RETIRED)", "version": "retired"},
            "instructions": human_message(slug),
        })
    if method == "tools/list":
        return _ok(rid, {"tools": [tombstone_tool(slug)]})
    if method == "tools/call":
        # Calling the tombstone tool is a success (it did what it says). Any other name is a request for
        # something that no longer exists: answer with the same facts, as an error the model can read.
        return _ok(rid, _text_result(slug, is_error=params.get("name") != TOMBSTONE_TOOL))
    if method == "ping":
        return _ok(rid, {})
    if method == "resources/list":
        return _ok(rid, {"resources": []})
    if method == "resources/templates/list":
        return _ok(rid, {"resourceTemplates": []})
    if method == "prompts/list":
        return _ok(rid, {"prompts": []})
    return _err(rid, -32601, f"Method '{method}' not found")


def handle(slug: str, payload: Any) -> Any:
    """The JSON-RPC answer for a POST to a retired door: a dict, a list (a batch), or None (202, no body)."""
    if not is_retired(slug):
        raise KeyError(slug)
    if isinstance(payload, list):
        if not payload:
            return _err(None, -32600, "Empty batch")
        replies = [r for r in (_one(slug, m) for m in payload[:16]) if r is not None]
        return replies or None
    return _one(slug, payload)
