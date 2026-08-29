// /mcp JSON-RPC handler — edge-serves the stable read-only methods (initialize,
// tools/list, ping) from snapshots; proxies everything else (tools/call,
// prompts/list, prompts/get, resources/list, resources/read) to origin.
//
// tools/call is gated through x402 (Coinbase micropayments) when
// X402_RECEIVER_ADDRESS is configured. Without it the worker proxies as before
// (Paddle-only / backward-compat path).

import { getSnapshots } from "./snapshots/index";
import { proxyToOrigin } from "./proxy";
import {
  buildPaymentRequirements,
  getRequiredAmount,
  isPricedTool,
  verifyPaymentOnchain,
  type PaymentRequirements,
} from "./x402";
import {
  checkRateLimit,
  clientIp,
  rateLimitExceededResponse,
  withRateLimitHeaders,
} from "./rate-limit";

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

/**
 * Headers to send to the origin, with X-Forwarded-For set AUTHORITATIVELY.
 *
 * The worker used to forward `request.headers` verbatim, which broke the
 * canonical endpoint in two different ways at once. The origin's freemium
 * quota keys on the leftmost X-Forwarded-For entry:
 *
 * 1. WHEN THE CLIENT SENDS NONE - the normal case - the origin saw only the
 *    address the request arrived from, which for everything through
 *    hatchloop.dev is Cloudflare. So EVERY anonymous caller on Earth shared a
 *    single 20/day bucket, and that bucket is permanently drained. A
 *    prospect's first sanctions screen against the endpoint printed on our own
 *    marketing page failed, every time, with a message telling them to go buy
 *    credits. It works on api.hatchloop.dev, which is why testing against the
 *    origin never revealed it.
 *
 * 2. WHEN THE CLIENT SENDS ONE - it was trusted. Anyone could send a random
 *    X-Forwarded-For per request and mint themselves an unlimited free tier.
 *
 * `cf-connecting-ip` is set by Cloudflare from the real TCP peer and cannot be
 * forged by the client, so overwriting with it fixes both: real per-caller
 * buckets, and a value the caller does not control.
 */
function originHeaders(request: Request): Headers {
  const h = new Headers(request.headers);
  const realIp = request.headers.get("cf-connecting-ip");
  if (realIp) {
    h.set("x-forwarded-for", realIp);
    h.set("x-real-ip", realIp);
  }
  return h;
}

// Methods served from embedded snapshots. prompts/* and resources/* used to
// be listed here returning empty arrays — that hid the four actual prompts and
// the cookbook resource that the Python /mcp server exposes. They now proxy to
// origin so the real payload reaches the agent.
const EDGE_MCP_METHODS: ReadonlySet<string> = new Set([
  "initialize",
  "ping",
  "tools/list",
]);

const NONCE_PENDING_TTL_SEC = 600; // 10 min — must match expires_at in 402 body.
const NONCE_SPENT_TTL_SEC = 86_400; // 24 h — replay protection window.

function payment402(
  requirements: PaymentRequirements,
  failureReason?: string,
): Response {
  const amountUsd = (Number(requirements.amount_atomic) / 1e6).toFixed(6);
  const body: Record<string, unknown> = {
    error: "payment_required",
    payment_requirements: requirements,
    instructions:
      `Pay ${amountUsd} USDC on Base to ${requirements.recipient}, then retry POST /mcp ` +
      `with header X-PAYMENT-PROOF: <tx-hash> and X-PAYMENT-NONCE: ${requirements.nonce}`,
  };
  if (failureReason) body.previous_failure_reason = failureReason;
  return new Response(JSON.stringify(body), {
    status: 402,
    headers: { ...JSON_HEADERS, "x-edge-source": "x402-gate" },
  });
}

async function issue402(
  toolName: string,
  recipient: string,
  kv: KVNamespace,
  failureReason?: string,
): Promise<Response> {
  const { requirements, nonce } = buildPaymentRequirements(toolName, recipient);
  try {
    await kv.put(`x402:nonce:${nonce}`, "pending", {
      expirationTtl: NONCE_PENDING_TTL_SEC,
    });
  } catch (e) {
    // KV writes can fail at the free-tier daily limit. Log and continue —
    // the agent will simply get a 402 with a nonce that the verify step
    // later treats as unknown, so we don't accidentally accept replay.
    console.warn("x402 nonce KV put failed:", (e as Error).message);
  }
  return payment402(requirements, failureReason);
}

export async function handleMcpRequest(
  request: Request,
  originUrl: string,
  publicBaseUrl: string,
  x402Receiver: string | undefined,
  kv: KVNamespace,
): Promise<Response> {
  if (request.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        "access-control-allow-origin": "*",
        "access-control-allow-methods": "POST, OPTIONS",
        "access-control-allow-headers":
          "content-type, authorization, x-agent-identity, x-payment-proof, x-payment-nonce",
      },
    });
  }

  if (request.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  // Read body once. We may need to forward it if we end up proxying.
  const bodyText = await request.text();
  let body: {
    jsonrpc?: string;
    method?: string;
    id?: unknown;
    params?: { name?: string; arguments?: unknown };
  } = {};
  try {
    body = JSON.parse(bodyText);
  } catch {
    return jsonrpcError(null, -32700, "Parse error");
  }

  const method = String(body.method ?? "");
  const id = body.id;

  if (!EDGE_MCP_METHODS.has(method)) {
    // tools/call: apply freemium rate-limit then optional x402 gate.
    if (method === "tools/call") {
      const ip = clientIp(request);
      const rlResult = await checkRateLimit(ip, kv);
      if (!rlResult.allowed) {
        // `id` is the caller's JSON-RPC request id, read at the top of this
        // function. Passing it is what makes the quota response a RESPONSE
        // rather than an unparseable body: a client correlates by id, and
        // without one it cannot match this to the call it made.
        return rateLimitExceededResponse(id);
      }

      // x402 per-call payment gate (only when receiver address configured).
      if (x402Receiver) {
        const toolName = String(body.params?.name ?? "");
        if (isPricedTool(toolName)) {
          const proof = request.headers.get("x-payment-proof");
          const nonce = request.headers.get("x-payment-nonce");

          if (!proof || !nonce) {
            return issue402(toolName, x402Receiver, kv);
          }

          // Validate the nonce shape before touching KV — keeps malformed
          // inputs from chewing through KV reads.
          if (!/^[0-9a-fA-F]{32}$/.test(nonce)) {
            return issue402(toolName, x402Receiver, kv, "nonce_malformed");
          }

          const kvKey = `x402:nonce:${nonce.toLowerCase()}`;
          const required = getRequiredAmount(toolName);
          const result = await verifyPaymentOnchain(
            proof,
            x402Receiver,
            required,
            kvKey,
            kv,
          );

          if (!result.valid) {
            return issue402(toolName, x402Receiver, kv, result.reason);
          }

          // Mark nonce spent so this proof can't be reused.
          try {
            await kv.put(kvKey, "spent", { expirationTtl: NONCE_SPENT_TTL_SEC });
          } catch (e) {
            console.warn("x402 mark-spent KV put failed:", (e as Error).message);
          }
        }
        // Free tools (preview_cost, self_test, anything not in the price table)
        // fall through unguarded.
      }

      // Proxy and attach rate-limit headers to the response.
      const proxyReq = new Request(request.url, {
        method: "POST",
        headers: originHeaders(request),
        body: bodyText,
      });
      const { response } = await proxyToOrigin(proxyReq, originUrl);
      return withRateLimitHeaders(response, rlResult);
    }

    // All other non-edge methods (prompts/*, resources/*) — proxy straight through.
    const proxyReq = new Request(request.url, {
      method: "POST",
      headers: originHeaders(request),
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
    default:
      return jsonrpcError(id, -32601, `Method not found: ${method}`);
  }
}
