// Discovery endpoints — served 100% from the worker bundle (with optional
// KV-overlay for fresher copies refreshed by cron). No origin contact, ever.
// Sub-50ms response time anywhere on Earth, even if Render is dead.

import { getSnapshots, manifestOps, manifestVersion, type Snapshots } from "./snapshots/index";

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "public, max-age=300",
  "access-control-allow-origin": "*",
};

const TEXT_HEADERS = {
  "content-type": "text/plain; charset=utf-8",
  "cache-control": "public, max-age=300",
  "access-control-allow-origin": "*",
};

const YAML_HEADERS = {
  "content-type": "application/x-yaml; charset=utf-8",
  "cache-control": "public, max-age=300",
  "access-control-allow-origin": "*",
};

type DiscoveryHandler = (snapshots: Snapshots, kvLive: unknown | null) => Response;

/** Build a JSON Response from a value; prefers KV-live override when present. */
function jsonRes(staticValue: unknown, live: unknown | null): Response {
  const body = JSON.stringify(live ?? staticValue);
  return new Response(body, {
    status: 200,
    headers: {
      ...JSON_HEADERS,
      "x-edge-source": live !== null ? "kv-live" : "embedded",
    },
  });
}

function textRes(staticValue: string, live: string | null, contentType: typeof TEXT_HEADERS | typeof YAML_HEADERS): Response {
  return new Response(live ?? staticValue, {
    status: 200,
    headers: {
      ...contentType,
      "x-edge-source": live !== null ? "kv-live" : "embedded",
    },
  });
}

// Edge-owned discovery routes — every entry here is fully edge-served.
// Path → handler.
export const DISCOVERY_HANDLERS: Record<string, DiscoveryHandler> = {
  "/.well-known/agent-service": (s, kv) => jsonRes(s.agentService, kv),
  "/.well-known/agents.json": (s, kv) => jsonRes(s.agentsJson, kv),
  "/.well-known/ai-plugin.json": (s, kv) => jsonRes(s.aiPluginJson, kv),
  "/.well-known/openai-tools.json": (s, kv) => jsonRes(s.openaiToolsJson, kv),
  "/.well-known/anthropic-tools.json": (s, kv) => jsonRes(s.anthropicToolsJson, kv),
  "/.well-known/mcp.json": (s, kv) => jsonRes(s.mcpJson, kv),
  "/manifest": (s, kv) => jsonRes(s.manifest, kv),
  "/manifest/version": (s, kv) => jsonRes(manifestVersion(s), kv),
  "/manifest/ops": (s, kv) => jsonRes(manifestOps(s), kv),
  "/supply/platforms": (s, kv) => jsonRes(s.supplyPlatforms, kv),
  "/compliance/jurisdictions": (s, kv) => jsonRes(s.jurisdictions, kv),
  "/llms.txt": (s, kv) => textRes(s.llmsTxt, kv as string | null, TEXT_HEADERS),
  "/llms-full.txt": (s, kv) => textRes(s.llmsFullTxt, kv as string | null, TEXT_HEADERS),
  "/openapi.yaml": (s, kv) => textRes(s.openapiYaml, kv as string | null, YAML_HEADERS),
};

// KV key for a discovery path (per public-base-url).
function kvKey(publicBaseUrl: string, path: string): string {
  return `live:v1:${publicBaseUrl}:${path}`;
}

/** Try to serve a discovery request from edge. Returns null if path is not edge-owned. */
export async function tryServeDiscovery(
  request: Request,
  publicBaseUrl: string,
  kv: KVNamespace,
): Promise<Response | null> {
  if (request.method !== "GET" && request.method !== "HEAD") return null;
  const url = new URL(request.url);
  const handler = DISCOVERY_HANDLERS[url.pathname];
  if (!handler) return null;

  const snapshots = getSnapshots(publicBaseUrl);

  // Check KV for a fresher version (set by cron).
  const isJson =
    url.pathname.endsWith(".json") ||
    url.pathname === "/manifest" ||
    url.pathname === "/manifest/version" ||
    url.pathname === "/manifest/ops" ||
    url.pathname === "/supply/platforms" ||
    url.pathname === "/compliance/jurisdictions";

  let live: unknown | null = null;
  try {
    live = isJson
      ? await kv.get(kvKey(publicBaseUrl, url.pathname), "json")
      : await kv.get(kvKey(publicBaseUrl, url.pathname), "text");
  } catch {
    live = null;
  }

  const resp = handler(snapshots, live);
  if (request.method === "HEAD") {
    // Same headers, no body.
    return new Response(null, { status: resp.status, headers: resp.headers });
  }
  return resp;
}

/** Update KV with the latest origin payload. Called by cron. */
export async function refreshKvFromOrigin(
  kv: KVNamespace,
  originUrl: string,
  publicBaseUrl: string,
  pathname: string,
  isJson: boolean,
): Promise<void> {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 10_000);
    const r = await fetch(originUrl + pathname, {
      headers: { "x-edge-probe": "cron-refresh" },
      signal: ctrl.signal,
    });
    clearTimeout(t);
    if (!r.ok) return;
    const body = await r.text();
    // Apply the same URL rewrite the snapshots get.
    let rewritten = body;
    for (const orig of [
      "https://agent-broker-edge.basil-agent.workers.dev",
      "https://smb-broker.onrender.com",
      "https://api.smb-broker.example/v1",
      "https://api.smb-broker.example",
    ]) {
      rewritten = rewritten.split(orig).join(publicBaseUrl);
    }
    // Only store if it parses as the expected type.
    if (isJson) {
      try {
        JSON.parse(rewritten);
      } catch {
        return;
      }
    }
    await kv.put(kvKey(publicBaseUrl, pathname), rewritten, {
      expirationTtl: 3600, // 1 hour — cron refreshes every 2 min anyway
    });
  } catch {
    // Silent — embedded snapshot is the safety net.
  }
}
