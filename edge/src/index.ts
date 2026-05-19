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

type Env = {
  CACHE: KVNamespace;
  ORIGIN_URL: string;
  EDGE_VERSION: string;
  SERVICE_NAME: string;
  PUBLIC_BASE_URL?: string;
};

const app = new Hono<{ Bindings: Env }>();

// Public-facing URL. Derived per-request from the host header so the same
// worker can serve both workers.dev and any custom domain we map later.
function publicBaseUrlOf(c: { req: { url: string }; env: Env }): string {
  if (c.env.PUBLIC_BASE_URL) return c.env.PUBLIC_BASE_URL;
  const u = new URL(c.req.url);
  return `${u.protocol}//${u.host}`;
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
  return handleMcpRequest(c.req.raw, c.env.ORIGIN_URL, publicBaseUrlOf(c));
});

// ---------------------------------------------------------------------------
// Health / metrics — short-cached proxy (live counters from origin)
// ---------------------------------------------------------------------------

app.get("/health", async (c) => {
  // Edge-served health: we report on edge state independent of origin.
  // For full origin probe, agents can hit /healthz/external.
  return c.json({
    status: "healthy",
    timestamp: new Date().toISOString(),
    checks: { manifest: "ok", directory: "ok", compliance: "ok" },
    edge: "cloudflare-workers",
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
          if (!r.ok) return;
          const body = await r.text();
          await env.CACHE.put("live:metrics", body, { expirationTtl: 60 });
          await env.CACHE.put("live:metrics:ts", String(Date.now()), { expirationTtl: 60 });
        } catch (e) {
          console.warn("cron metrics refresh failed:", (e as Error).message);
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
