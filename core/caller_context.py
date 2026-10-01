"""
Who is calling, for per-caller limits deep inside a handler.

Handlers such as core.find_business are called as plain functions and are not
handed the HTTP request. The server's per-IP rate-limit middleware (main.py)
already works out the caller for every /mcp and /ops call; it stores that here
for the duration of the request, and handlers read it.

A handler called outside an HTTP request (a unit test, an internal script,
the /demo route) sees None and is not per-caller limited - the limit exists to
protect a free upstream from the public internet, not to throttle our own code.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

CALLER_KEY: ContextVar[Optional[str]] = ContextVar("agentbroker_caller_key", default=None)

# The caller's resolved network address (core/client_ip.py), as opposed to CALLER_KEY, which is the
# rate-limit BUCKET and is the key id for a caller holding a valid key. Telemetry wants the
# address; the limiter wants the bucket; they are no longer the same string.
CALLER_IP: ContextVar[Optional[str]] = ContextVar("agentbroker_caller_ip", default=None)


@dataclass
class RequestObservation:
    """A mutable box the HTTP middleware hands down and the MCP handler writes back into.

    A ContextVar set inside the endpoint is invisible to the middleware once the endpoint's task
    ends, so the reverse channel has to be a shared object, not a rebinding. `logged` is set by
    handle_mcp_request when it has already recorded this request's outcome, which is how the
    middleware knows not to log the same request a second time as a bare HTTP status."""
    logged: bool = False


REQUEST_OBSERVATION: ContextVar[Optional[RequestObservation]] = ContextVar(
    "agentbroker_request_observation", default=None)
