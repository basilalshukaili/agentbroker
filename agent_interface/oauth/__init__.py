"""OAuth "Connect" sign-in for consumer assistants (MCP authorization).

See docs/OAUTH-CONNECT.md for the whole design. The pieces:

    settings   tunables and the kill switch                  (no I/O)
    resources  which URLs are "us"                           (no I/O)
    limits     ceilings on what an anonymous caller can cause
    tokens     secrets, PKCE, email -> account -> access token
    store      the spine (production) / memory (tests, dev)
    clients    client identification, redirect URIs, the SSRF-safe metadata fetch
    emailer    the sign-in link email
    pages      the four pages a person sees
    router     the HTTP endpoints
    challenge  the 401 / _meta signal and tools/list securitySchemes
    link       billing -> sign-in: which credit account an email bought

Imports are lazy so that `import agent_interface.oauth` costs nothing and cannot create an import cycle with
identity.py (which calls back into `resources.is_our_resource`).
"""
from __future__ import annotations


def __getattr__(name):
    if name == "router":
        from agent_interface.oauth.router import router
        return router
    if name in ("apply", "annotate_tools", "schemes_for"):
        from agent_interface.oauth import challenge
        return getattr(challenge, name)
    raise AttributeError(name)
