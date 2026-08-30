// Agent Broker — Cloudflare Worker edge front-door.
//
// Mission: AI agents that discover this MCP server should NEVER hit a 30s
// Render cold-start. Discovery payloads are served 100% from the worker bundle
// (snapshotted from origin, URL-rewritten to point at the edge). Tool execution
// is proxied to the origin with retry-on-cold-start. Cron keeps origin warm
// and overlays fresher copies in KV.
//
// Architecture decision rationale:
//   • Read-only discovery endpoints → embedded snapshots → sub-50ms always
//   • State-changing endpoints (mcp tools/call, /ops/*) → proxy to origin
//   • If origin dies, discovery still works indefinitely from the embedded
//     bundle — the worker can outlive Render.

import { Hono } from "hono";
import { proxyToOrigin } from "./proxy";
import { tryServeDiscovery, refreshKvFromOrigin } from "./discovery";
import { handleMcpRequest } from "./mcp-edge";
import { runAlertChecks } from "./alerts";

type Env = {
  CACHE: KVNamespace;
  ORIGIN_URL: string;
  EDGE_VERSION: string;
  SERVICE_NAME: string;
  PUBLIC_BASE_URL?: string;
  // Optional secrets (set via `wrangler secret put`). When unset, the alert
  // subsystem silently no-ops — see src/alerts.ts.
  TELEGRAM_BOT_TOKEN?: string;
  TELEGRAM_CHAT_ID?: string;
  // x402 micropayments (Coinbase Agent Kit / x402-compatible MCP clients).
  // When unset, tools/call is proxied directly to origin and only the Paddle
  // subscription path applies — see src/x402.ts and src/mcp-edge.ts.
  X402_RECEIVER_ADDRESS?: string;
};

const app = new Hono<{ Bindings: Env }>();

// Public-facing URL. Derived per-request from the host header so the same
// worker can serve both workers.dev and any custom domain we map later.
const BRANDED_BASE_URL = "https://hatchloop.dev";

/**
 * The base URL stamped into every descriptor we publish.
 *
 * THE FALLBACK USED TO BE THE REQUEST'S OWN HOST, and that is how a generic
 * hostname got into our public identity. Requests reach this Worker at
 * agent-broker-edge.basil-agent.workers.dev - hatchloop.dev proxies here via a
 * Vercel rewrite - so with PUBLIC_BASE_URL unset (it was), every discovery
 * document advertised the workers.dev address as us.
 *
 * Falling back to the BRANDED host instead means a missing binding can never
 * leak an internal hostname again. The worker's own address is plumbing; it is
 * not the company's name, and it should never be able to become it by default.
 */
function publicBaseUrlOf(c: { req: { url: string }; env: Env }): string {
  return c.env.PUBLIC_BASE_URL || BRANDED_BASE_URL;
}

// ---------------------------------------------------------------------------
// Edge-internal routes (worker-only)
// ---------------------------------------------------------------------------

app.get("/edge/health", (c) => {
  return c.json({
    status: "healthy",
    edge: "cloudflare-workers",
    version: c.env.EDGE_VERSION,
    public_url: publicBaseUrlOf(c),
    timestamp: new Date().toISOString(),
  });
});

app.get("/edge/info", async (c) => {
  let originStatus: string;
  let originLatencyMs: number | null = null;
  const start = Date.now();
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 5000);
    const r = await fetch(c.env.ORIGIN_URL + "/health", {
      headers: { "x-edge-probe": "info" },
      signal: ctrl.signal,
    });
    clearTimeout(t);
    originLatencyMs = Date.now() - start;
    originStatus = r.ok ? "healthy" : `unhealthy_${r.status}`;
  } catch {
    originStatus = "unreachable";
  }

  return c.json({
    edge: {
      version: c.env.EDGE_VERSION,
      service: c.env.SERVICE_NAME,
      runtime: "cloudflare-workers",
      mode: "edge-first",
    },
    origin: {
      url: c.env.ORIGIN_URL,
      status: originStatus,
      latency_ms: originLatencyMs,
      role: "state-changing operations only",
    },
    discovery_served_from: "embedded snapshots + KV live overlay",
    timestamp: new Date().toISOString(),
  });
});

// ---------------------------------------------------------------------------
// /mcp JSON-RPC — edge-served for read methods, proxied for tools/call
// ---------------------------------------------------------------------------

app.all("/mcp", async (c) => {
  return handleMcpRequest(
    c.req.raw,
    c.env.ORIGIN_URL,
    publicBaseUrlOf(c),
    c.env.X402_RECEIVER_ADDRESS,
    c.env.CACHE,
  );
});

// ---------------------------------------------------------------------------
// Health / metrics — short-cached proxy (live counters from origin)
// ---------------------------------------------------------------------------

// GET AND HEAD. Uptime monitors default to HEAD, and a GET-only health
// route answers them 405 - the endpoint whose job is to say we are up
// telling a monitor the service is broken. The origin had the same gap.
app.on(["GET", "HEAD"], "/health", async (c) => {
  // THIS USED TO ASSERT { manifest: "ok", directory: "ok", compliance: "ok" }.
  //
  // Those are ORIGIN subsystems. This handler runs in a Cloudflare Worker,
  // in a different runtime, with no access to any of them - so it was
  // reporting three things it structurally cannot observe, and doing it in
  // the document an orchestrator or circuit breaker reads to decide whether
  // we are up. The origin could have been returning 502 to everything and
  // this endpoint would still have said all three were fine.
  //
  // Now it reports what it can actually see: that the edge is serving, and
  // what the origin said when asked. The origin's own /health derives its
  // three checks; this forwards that verdict rather than inventing one.
  let origin: unknown = "unknown";
  let status = "healthy";
  try {
    const r = await fetch(`${c.env.ORIGIN_URL}/health`, {
      method: "GET",
      signal: AbortSignal.timeout(3000),
    });
    if (r.ok) {
      origin = await r.json();
      const s = (origin as { status?: string })?.status;
      if (s && s !== "healthy") status = "degraded";
    } else {
      origin = { status: `unhealthy_${r.status}` };
      status = "degraded";
    }
  } catch (e) {
    origin = { status: "unreachable", error: (e as Error).message };
    status = "degraded";
  }
  return c.json({
    // The EDGE is serving - that is the only thing this runtime can assert
    // about itself, and it is true by virtue of answering at all.
    status,
    timestamp: new Date().toISOString(),
    edge: "cloudflare-workers",
    origin,
  });
});

app.get("/api/metrics", async (c) => {
  // Live counters live on origin. Proxy with short TTL.
  const cached = await c.env.CACHE.get("live:metrics", "text");
  if (cached) {
    const meta = await c.env.CACHE.get("live:metrics:ts", "text");
    return new Response(cached, {
      status: 200,
      headers: {
        "content-type": "application/json",
        "x-edge-source": "kv-cached",
        "x-edge-age": meta ? String(Math.floor((Date.now() - Number(meta)) / 1000)) : "0",
      },
    });
  }

  const { response } = await proxyToOrigin(c.req.raw, c.env.ORIGIN_URL);
  if (response.ok) {
    const body = await response.clone().text();
    // KV free-tier caps daily writes at 1k. Swallow quota failures so the
    // response still reaches the caller even when the cache write throws.
    try {
      await c.env.CACHE.put("live:metrics", body, { expirationTtl: 60 });
      await c.env.CACHE.put("live:metrics:ts", String(Date.now()), { expirationTtl: 60 });
    } catch (e) {
      console.warn("KV put live:metrics failed:", (e as Error).message);
    }
    return new Response(body, {
      status: 200,
      headers: {
        "content-type": "application/json",
        "x-edge-source": "origin-fresh",
      },
    });
  }
  return response;
});

// ---------------------------------------------------------------------------
// MCP OAuth discovery — clients probe these before calling tools/call.
// We don't require OAuth; respond with an RFC 8414-shaped doc that advertises
// no authorization endpoints so probers stop polling and proceed to /mcp.
// ---------------------------------------------------------------------------

function oauthMetadata(baseUrl: string): Record<string, unknown> {
  return {
    issuer: baseUrl,
    authorization_endpoint: null,
    token_endpoint: null,
    registration_endpoint: null,
    response_types_supported: [],
    grant_types_supported: [],
    scopes_supported: [],
    token_endpoint_auth_methods_supported: ["none"],
    authorization_required: false,
    service_documentation: `${baseUrl}/manifest`,
  };
}

app.get("/.well-known/oauth-authorization-server", (c) => {
  return new Response(JSON.stringify(oauthMetadata(publicBaseUrlOf(c))), {
    status: 200,
    headers: {
      "content-type": "application/json",
      "cache-control": "public, max-age=86400",
      "x-edge-source": "edge-stub",
    },
  });
});

app.get("/.well-known/oauth-protected-resource", (c) => {
  const baseUrl = publicBaseUrlOf(c);
  return new Response(
    JSON.stringify({
      resource: baseUrl,
      authorization_servers: [],
      bearer_methods_supported: [],
      resource_documentation: `${baseUrl}/manifest`,
      authentication_required: false,
    }),
    {
      status: 200,
      headers: {
        "content-type": "application/json",
        "cache-control": "public, max-age=86400",
        "x-edge-source": "edge-stub",
      },
    },
  );
});

app.get("/.well-known/openid-configuration", (c) => {
  // OpenID Connect Discovery 1.0 — same payload shape as oauth-authorization-server
  // is acceptable for probers that fall back here.
  return new Response(JSON.stringify(oauthMetadata(publicBaseUrlOf(c))), {
    status: 200,
    headers: {
      "content-type": "application/json",
      "cache-control": "public, max-age=86400",
      "x-edge-source": "edge-stub",
    },
  });
});

app.get("/healthz/external", async (c) => {
  // Always proxy — this is supposed to be live external-API checks.
  const { response } = await proxyToOrigin(c.req.raw, c.env.ORIGIN_URL);
  return response;
});

// ---------------------------------------------------------------------------
// Discovery routes — try edge first, proxy as fallback for anything unknown
// ---------------------------------------------------------------------------

app.all("*", async (c) => {
  const publicBaseUrl = publicBaseUrlOf(c);

  // Try edge-served discovery first.
  const discovered = await tryServeDiscovery(c.req.raw, publicBaseUrl, c.env.CACHE);
  if (discovered !== null) return discovered;

  // Fallback: proxy to origin with retry. Used for /ops/*, /webhooks/*,
  // /supply/import_booking_url, /compliance/check, web pages.
  const { response, attempts, totalMs, retried } = await proxyToOrigin(c.req.raw, c.env.ORIGIN_URL);
  const headers = new Headers(response.headers);
  headers.set("x-edge-source", "origin-proxy");
  headers.set("x-edge-attempts", String(attempts));
  headers.set("x-edge-retried", String(retried));
  headers.set("server-timing", `origin;dur=${totalMs}`);
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
});

// ---------------------------------------------------------------------------
// Cron handler: refresh KV live overlay + keep origin warm
// ---------------------------------------------------------------------------

const REFRESH_TARGETS: ReadonlyArray<{ path: string; isJson: boolean }> = [
  { path: "/manifest", isJson: true },
  { path: "/.well-known/agents.json", isJson: true },
  { path: "/.well-known/anthropic-tools.json", isJson: true },
  { path: "/.well-known/openai-tools.json", isJson: true },
  { path: "/.well-known/agent-service", isJson: true },
  { path: "/.well-known/ai-plugin.json", isJson: true },
  { path: "/.well-known/mcp.json", isJson: true },
  { path: "/llms.txt", isJson: false },
  { path: "/supply/platforms", isJson: true },
  { path: "/compliance/jurisdictions", isJson: true },
];

async function scheduledHandler(event: ScheduledController, env: Env, ctx: ExecutionContext) {
  // Resolve public base url for cron — we don't have a request here, so use
  // the configured value or default to the workers.dev URL the worker is on.
  const publicBaseUrl = env.PUBLIC_BASE_URL ?? "https://agent-broker-edge.basil-agent.workers.dev";

  const tasks: Promise<unknown>[] = [];

  // Always keep the Render dyno warm (no KV writes, free).
  tasks.push(
    fetch(env.ORIGIN_URL + "/health", { headers: { "x-edge-probe": "cron-warmup" } }).catch(() => null),
  );

  // KV writes count against the free-tier 1k/day budget. Refresh discovery
  // and live metrics only every 30 min — between refreshes, edge requests
  // either hit the existing KV-cached entry or fall through to the embedded
  // snapshot / origin.
  const minute = new Date(event.scheduledTime).getUTCMinutes();
  const shouldRefreshKv = minute % 30 === 0;

  if (shouldRefreshKv) {
    for (const { path, isJson } of REFRESH_TARGETS) {
      tasks.push(refreshKvFromOrigin(env.CACHE, env.ORIGIN_URL, publicBaseUrl, path, isJson));
    }

    tasks.push(
      (async () => {
        try {
          const r = await fetch(env.ORIGIN_URL + "/api/metrics", {
            headers: { "x-edge-probe": "cron-metrics" },
          });
          if (r.ok) {
            const body = await r.text();
            await env.CACHE.put("live:metrics", body, { expirationTtl: 60 });
            await env.CACHE.put("live:metrics:ts", String(Date.now()), { expirationTtl: 60 });
          }
        } catch (e) {
          console.warn("cron metrics refresh failed:", (e as Error).message);
        }

        // Alert checks read fresh metrics from KV — keep this AFTER the
        // refresh above (still safe to run even if the refresh failed; the
        // checks just see stale data, and never throw).
        try {
          await runAlertChecks(env, env.CACHE);
        } catch (e) {
          console.warn("cron alerts failed:", (e as Error).message);
        }
      })(),
    );
  }

  // waitUntil so cron returns fast but tasks complete in the background.
  ctx.waitUntil(Promise.all(tasks));
}

export default {
  fetch: app.fetch,
  scheduled: scheduledHandler,
} satisfies ExportedHandler<Env>;
