// Edge cache rules: path → TTL seconds.
// Discovery + static endpoints get long TTLs.
// Live counters get short TTLs.
// Anything not listed is bypass (proxied straight through, no edge cache).
export const CACHE_TTL: Record<string, number> = {
  // Static discovery (5 min — manifest changes rarely)
  "/.well-known/agent-service": 300,
  "/.well-known/ai-plugin.json": 300,
  "/.well-known/openai-tools.json": 300,
  "/.well-known/anthropic-tools.json": 300,
  "/.well-known/agents.json": 300,
  "/.well-known/mcp.json": 300,
  "/llms.txt": 300,
  "/llms-full.txt": 300,
  "/manifest": 300,
  "/manifest/version": 300,
  "/manifest/ops": 300,
  "/openapi.yaml": 300,
  "/supply/platforms": 600,
  "/compliance/jurisdictions": 600,

  // Live state (short TTL so we don't lie too long)
  "/health": 30,
  "/healthz/external": 15,
  "/api/metrics": 10,

  // Web UI (legal pages basically immutable)
  "/": 300,
  "/pricing": 600,
  "/terms": 86400,
  "/privacy": 86400,
  "/refund": 86400,
};

// Endpoints that MUST be proxied even on GET (no caching makes sense).
export const NEVER_CACHE: ReadonlySet<string> = new Set([
  "/ops/get_status",
  "/ops/get_outcome",
]);

// Paths the worker handles directly (no proxy at all).
// Currently only edge-internal endpoints.
export const EDGE_OWNED: ReadonlySet<string> = new Set([
  "/edge/info",
  "/edge/health",
]);

export function getCacheTTL(pathname: string): number {
  // Exact match first
  if (CACHE_TTL[pathname] !== undefined) return CACHE_TTL[pathname];
  // Prefix matches for /ops/get_status/* etc.
  for (const prefix of NEVER_CACHE) {
    if (pathname.startsWith(prefix)) return 0;
  }
  return 0;
}
