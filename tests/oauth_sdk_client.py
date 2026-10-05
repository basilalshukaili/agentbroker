"""Official SDK client only: never import the production app into this interpreter."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from importlib.metadata import version
from urllib.parse import urlsplit

from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientMetadata
from pydantic import AnyUrl


class Storage:
    tokens = None
    client = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client

    async def set_client_info(self, client):
        self.client = client


def emit(value):
    print(json.dumps(value), flush=True)


async def run(request):
    base, scenario = request["base"], request["scenario"]
    parsed = urlsplit(base)
    assert parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port, "SDK tests require loopback HTTP"
    if scenario == "guidance":
        async with streamablehttp_client(f"{base}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("send_message", {})
                assert result.isError is True
                assert "auth_required" in result.content[0].text
                return {"isError": result.isError, "text": result.content[0].text}

    storage, visits, callback, first = Storage(), [], {}, {}

    async def redirect(url):
        visits.append(url)
        emit({"event": "redirect", "url": url})
        callback.update(json.loads(await asyncio.to_thread(sys.stdin.readline)))

    async def receive_callback():
        return callback["code"], callback["state"]

    metadata = OAuthClientMetadata(
        client_name="SDK conformance", redirect_uris=[AnyUrl(request["redirect_uri"])],
        grant_types=["authorization_code", "refresh_token"], response_types=["code"],
        token_endpoint_auth_method="none",
    )
    extra = {"client_metadata_url": request["cimd"]} if scenario == "cimd" else {}
    provider = OAuthClientProvider(f"{base}/mcp", metadata, storage, redirect, receive_callback, **extra)
    async with streamablehttp_client(f"{base}/mcp", auth=provider,
                                     headers={"User-Agent": "claude-code/1.0"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if scenario == "register":
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert "get_conversation" in names and "screen_sanctions" in names
                assert visits == [], "listing tools must not require sign-in"
                free = await session.call_tool("check_quota", {})
                assert not free.isError and visits == [], "a keyless tool must not require sign-in"
            args = {"reference": "1234", "business_number": "+15550001111"}
            result = await session.call_tool("get_conversation", args)
            text = result.content[0].text
            assert "identity_required" not in text and "auth_required" not in text, text[:300]
            if scenario == "refresh":
                first = {"refresh": storage.tokens.refresh_token, "access": storage.tokens.access_token}
                provider.context.token_expiry_time = time.time() - 60
                again = await session.call_tool("get_conversation", args)
                assert "identity_required" not in again.content[0].text
    return {"client": storage.client.model_dump(mode="json"), "tokens": storage.tokens.model_dump(mode="json"),
            "first": first, "visits": visits}


if __name__ == "__main__":
    assert version("mcp") == "1.26.0", "the interoperability client must use the reviewed SDK version"
    if "--check" in sys.argv:
        emit({"sdk": version("mcp")})
    else:
        emit({"event": "result", "result": asyncio.run(run(json.loads(sys.stdin.readline())))})
