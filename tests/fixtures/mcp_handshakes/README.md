# MCP handshake fixtures

Replayed by `tests/unit/test_mcp_handshake_replay.py`.

| File | What it is | Honest limits |
|---|---|---|
| `python_sdk_legacy_before_2026-07-28.json` | **A real capture.** The official MCP Python SDK client (mcp 1.26.0) doing `initialize`, `notifications/initialized`, `tools/list`, `tools/call`, `ping`, recorded at the HTTP layer against the commit *before* the 2026-07-28 work (1f85885). | Legacy era only: that SDK speaks up to 2025-11-25. Response *shapes* are stored (statuses, result keys, tool names), not full text, because `instructions` and `tools/list` depend on the deployment's configuration. |
| `modern_2026-07-28_sequence_from_spec.json` | **Constructed**, not captured, from the specification's wire examples and the sequence the TypeScript SDK v2 documents for its `auto` negotiation mode. | No 2026-07-28 client was available on the build machine and installing one from a registry was not authorised. Replace it with a capture the first time one is available. |
| `scanner_shapes_observed_2026-10-03.json` | **Observed header shapes, reconstructed bodies.** What the callers asking for this revision today actually send, from a read-only aggregate of the production access log. | Caddy logs request headers, not bodies, so every body is the minimum the named method requires. |

## Re-recording the legacy capture

```
pip install mcp          # a development tool only; not a runtime dependency
python scripts/record_mcp_handshake.py --out tests/fixtures/mcp_handshakes/python_sdk_legacy_before_2026-07-28.json
```

To record against another checkout (this is how the committed file was made, from a `git archive` of
1f85885): `--root <dir> --server-commit "<label>"`. The recorder stubs the usage log and the spine client
before importing the app, so a recording can never write a row anywhere.

## Capturing a modern client

Drive any 2026-07-28 client at the app through an ASGI transport and hook the request/response events the way
`record_mcp_handshake.py` does (`httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), event_hooks=...)`).
Do not point a recording at production.
