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
from typing import Optional

CALLER_KEY: ContextVar[Optional[str]] = ContextVar("agentbroker_caller_key", default=None)
