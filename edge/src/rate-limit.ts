// Freemium rate-limiter for AgentBroker edge worker.
//
// Strategy: KV-backed per-IP daily counter (key = "rl:{ip}:{YYYY-MM-DD}",
// TTL = 25 h so the key auto-expires after midnight UTC). Falls back to a
// module-level in-memory map when KV is unavailable — sufficient for MVP
// because Smithery probes are highly concentrated and a single worker
// instance handles a burst before the KV write budget is exhausted.
//
// Free tier: 100 tool calls / IP / day.
// Upgrade path: CREDIT PACKAGES, not a subscription. The previous comment here
// said "Polar $49/mo subscription" and the code linked a hardcoded Polar
// checkout - docs/PRICING.md states that no monthly tier at any price was ever
// in effect. Link the pricing page, which is generated from the live packages,
// so this can never go stale again.

export const FREE_TIER_LIMIT = 100;
export const PRICING_URL = "https://hatchloop.dev/pricing";
export const FREE_KEY_URL = "https://hatchloop.dev/agent-broker";

// In-memory fallback (per worker instance, resets on cold-start).
const inMemoryCounters = new Map<string, number>();

/** Returns today's date as YYYY-MM-DD in UTC. */
function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

/** Derive best-effort client IP from CF request headers. */
export function clientIp(request: Request): string {
  return (
    request.headers.get("cf-connecting-ip") ??
    request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ??
    "unknown"
  );
}

export interface RateLimitResult {
  allowed: boolean;
  remaining: number;
  limit: number;
}

/**
 * Check and increment the per-IP daily counter.
 * Returns { allowed, remaining, limit }.
 * Never throws — on any error it allows the request (fail-open).
 */
export async function checkRateLimit(
  ip: string,
  kv: KVNamespace | undefined,
): Promise<RateLimitResult> {
  const key = `rl:${ip}:${todayUtc()}`;

  // --- KV path ---
  if (kv) {
    try {
      // Atomic increment via read-modify-write (CF KV lacks native INCR, but
      // the 1-second consistency window is fine for a 100/day soft cap —
      // a brief race that allows 101 calls is acceptable at this scale).
      const raw = await kv.get(key, "text");
      const current = raw ? parseInt(raw, 10) : 0;
      const next = current + 1;
      const allowed = current < FREE_TIER_LIMIT;
      if (allowed) {
        // Only increment when we're allowing; once over the cap stop writing.
        await kv.put(key, String(next), { expirationTtl: 90_000 }); // ~25 h
      }
      return {
        allowed,
        remaining: Math.max(0, FREE_TIER_LIMIT - next),
        limit: FREE_TIER_LIMIT,
      };
    } catch (e) {
      console.warn("rate-limit KV error, falling back to in-memory:", (e as Error).message);
    }
  }

  // --- In-memory fallback ---
  const current = inMemoryCounters.get(key) ?? 0;
  const next = current + 1;
  const allowed = current < FREE_TIER_LIMIT;
  if (allowed) {
    inMemoryCounters.set(key, next);
  }
  return {
    allowed,
    remaining: Math.max(0, FREE_TIER_LIMIT - next),
    limit: FREE_TIER_LIMIT,
  };
}

/** Seconds until the daily quota actually resets (midnight UTC). */
function secondsUntilReset(now: Date = new Date()): number {
  const midnight = Date.UTC(
    now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1, 0, 0, 0, 0);
  return Math.max(1, Math.ceil((midnight - now.getTime()) / 1000));
}

/**
 * The free-tier quota response, in the shape an MCP client can actually read.
 *
 * THREE THINGS WERE WRONG HERE AT ONCE, and every one of them landed on a
 * free-tier agent at the exact moment it was ready to become a paying one.
 *
 * 1. IT WAS NOT JSON-RPC. The body was a bare `{error, message, upgrade_url}`
 *    with HTTP 429 - no `jsonrpc`, no `id`, no `error.code`. A strict MCP
 *    client cannot parse that as a response to its call, so what it sees is not
 *    "you are rate limited", it is a protocol violation. The request id was
 *    available at the call site the whole time and simply was not passed in.
 *
 * 2. "Upgrade to unlimited" IS A PRODUCT WE DO NOT SELL. This file's own
 *    comment called it a "$49/mo subscription"; docs/PRICING.md states plainly
 *    that no subscription tier at any price was ever in effect. Billing is
 *    credit packages. The hardcoded Polar link is dropped entirely rather than
 *    corrected - a URL frozen in a worker bundle is the same "claim written as
 *    a constant" defect that check_pricing.py exists to catch, and it would go
 *    stale again the next time packages change. hatchloop.dev/pricing is
 *    generated from the live packages and cannot drift.
 *
 * 3. `retry-after: 86400` TOLD AN AGENT TO WAIT A FULL DAY. The quota resets at
 *    midnight UTC, so an agent hitting the cap at 23:50 was told to come back
 *    twenty-four hours later instead of in ten minutes.
 *
 * The shape now matches what the ORIGIN returns for the same condition
 * (error_code/retriable/retry_after_ms/how_to_resolve) - an agent must not get
 * two different error contracts depending on which host answered it.
 */
export function rateLimitExceededResponse(id: unknown = null): Response {
  const resetSec = secondsUntilReset();
  const body = JSON.stringify({
    jsonrpc: "2.0",
    id: id ?? null,
    error: {
      code: -32000,
      message:
        `Free tier limit reached (${FREE_TIER_LIMIT} ops/day). ` +
        `The quota resets at midnight UTC. Credit packages: ${PRICING_URL}`,
      data: {
        error_code: "rate_limited",
        retriable: true,
        retry_after_ms: resetSec * 1000,
        tier: "free",
        how_to_resolve: {
          wait_seconds: resetSec,
          upgrade: PRICING_URL,
          free_key: FREE_KEY_URL,
          hint:
            "A free email-verified key raises the write quota; credits are " +
            "pay-as-you-go, not a subscription.",
        },
      },
    },
  });
  return new Response(body, {
    // 200 with a JSON-RPC error, matching jsonrpcError() in mcp-edge.ts: the
    // failure is at the protocol layer, and a client that cannot parse the
    // body learns nothing from the status code. The rate-limit headers stay so
    // HTTP-aware callers still get the signal.
    status: 200,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "access-control-allow-origin": "*",
      "x-ratelimit-limit": String(FREE_TIER_LIMIT),
      "x-ratelimit-remaining": "0",
      "retry-after": String(resetSec),
    },
  });
}

/** Inject rate-limit headers onto an existing Response (non-destructive clone). */
export function withRateLimitHeaders(response: Response, result: RateLimitResult): Response {
  const headers = new Headers(response.headers);
  headers.set("x-ratelimit-limit", String(result.limit));
  headers.set("x-ratelimit-remaining", String(result.remaining));
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}
