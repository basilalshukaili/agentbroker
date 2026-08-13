// Freemium rate-limiter for AgentBroker edge worker.
//
// Strategy: KV-backed per-IP daily counter (key = "rl:{ip}:{YYYY-MM-DD}",
// TTL = 25 h so the key auto-expires after midnight UTC). Falls back to a
// module-level in-memory map when KV is unavailable — sufficient for MVP
// because Smithery probes are highly concentrated and a single worker
// instance handles a burst before the KV write budget is exhausted.
//
// Free tier: 100 tool calls / IP / day.
// Upgrade path: Polar $49/mo subscription.

export const FREE_TIER_LIMIT = 100;
export const POLAR_CHECKOUT_URL =
  "https://buy.polar.sh/polar_cl_zRn6I67zMjFuenkjDme5RCnDYmA3vefHqX1zG3A5Phh";

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

/** Build the HTTP 429 response body and headers. */
export function rateLimitExceededResponse(): Response {
  const body = JSON.stringify({
    error: "rate_limit_exceeded",
    message:
      `Free tier limit reached (${FREE_TIER_LIMIT} ops/day). ` +
      `Upgrade to unlimited at ${POLAR_CHECKOUT_URL}`,
    upgrade_url: POLAR_CHECKOUT_URL,
    tier: "free",
  });
  return new Response(body, {
    status: 429,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "access-control-allow-origin": "*",
      "x-ratelimit-limit": String(FREE_TIER_LIMIT),
      "x-ratelimit-remaining": "0",
      "retry-after": "86400",
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
