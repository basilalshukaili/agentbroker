// /mcp JSON-RPC handler — edge-serves read-only methods (initialize, tools/list,
// prompts/list, resources/list, ping); proxies state-changing methods (tools/call)
// to the origin.

import { getSnapshots } from "./snapshots/index";
import { proxyToOrigin } from "./proxy";

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "access-control-allow-origin": "*",
};

function jsonrpcResult(id: unknown, result: unknown): Response {
  return new Response(JSON.stringify({ jsonrpc: "2.0", id, result }), {
    status: 200,
    headers: { ...JSON_HEADERS, "x-edge-source": "embedded-mcp" },
  });
}

function jsonrpcError(id: unknown, code: number, message: string): Response {
  return new Response(JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } }), {
    status: 200,
    headers: JSON_HEADERS,
  });
}

const EDGE_MCP_METHODS: ReadonlySet<string> = new Set([
  "initialize",
  "ping",
  "tools/list",
  "prompts/list",
  "prompts/get",
  "resources/list",
  "resources/read",
]);

export async function handleMcpRequest(
  request: Request,
  originUrl: string,
  publicBaseUrl: string,
): Promise<Response> {
  if (request.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        "access-control-allow-origin": "*",
        "access-control-allow-methods": "POST, OPTIONS",
        "access-control-allow-headers": "content-type, authorization, x-agent-identity",
      },
    });
  }

  if (request.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  // Read body once. We may need to forward it if we end up proxying.
  const bodyText = await request.text();
  let body: { jsonrpc?: string; method?: string; id?: unknown; params?: unknown } = {};
  try {
    body = JSON.parse(bodyText);
  } catch {
    return jsonrpcError(null, -32700, "Parse error");
  }

  const method = String(body.method ?? "");
  const id = body.id;

  if (!EDGE_MCP_METHODS.has(method)) {
    // tools/call and anything else: proxy to origin with retry.
    const proxyReq = new Request(request.url, {
      method: "POST",
      headers: request.headers,
      body: bodyText,
    });
    const { response } = await proxyToOrigin(proxyReq, originUrl);
    return response;
  }

  const snapshots = getSnapshots(publicBaseUrl);

  switch (method) {
    case "initialize": {
      // Use the snapshot but inject the requested protocol version if compatible.
      const init = snapshots.mcpInitialize as { result?: unknown };
      return jsonrpcResult(id, init.result);
    }
    case "ping":
      return jsonrpcResult(id, {});
    case "tools/list": {
      const tl = snapshots.mcpToolsList as { result?: unknown };
      return jsonrpcResult(id, tl.result);
    }
    case "prompts/list":
      return jsonrpcResult(id, { prompts: [] });
    case "prompts/get":
      return jsonrpcError(id, -32602, "No prompts defined");
    case "resources/list":
      return jsonrpcResult(id, { resources: [] });
    case "resources/read":
      return jsonrpcError(id, -32602, "No resources defined");
    default:
      return jsonrpcError(id, -32601, `Method not found: ${method}`);
  }
}
