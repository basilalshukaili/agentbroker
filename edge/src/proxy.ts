// Smart proxy to the origin (Render) with retry-on-cold-start.
//
// Render free dynos sleep after 15 min idle and take ~30s to wake. The first
// request to a sleeping dyno typically returns 502/503 from Render's router or
// times out. We retry once after a short delay so AI agents never see that.

const MAX_ATTEMPTS = 2;
const RETRY_DELAY_MS = 1500;
const REQUEST_TIMEOUT_MS = 45_000; // 45s — long enough to ride out one cold start

export type ProxyResult = {
  response: Response;
  attempts: number;
  totalMs: number;
  retried: boolean;
};

export async function proxyToOrigin(
  request: Request,
  originUrl: string,
): Promise<ProxyResult> {
  const start = Date.now();
  const incomingUrl = new URL(request.url);
  const targetUrl = originUrl.replace(/\/$/, "") + incomingUrl.pathname + incomingUrl.search;

  // Strip hop-by-hop headers; preserve the rest.
  const fwdHeaders = new Headers(request.headers);
  fwdHeaders.delete("host");
  fwdHeaders.delete("cf-connecting-ip");
  fwdHeaders.delete("cf-ipcountry");
  fwdHeaders.delete("cf-ray");
  fwdHeaders.delete("cf-visitor");
  fwdHeaders.set("x-forwarded-for", request.headers.get("cf-connecting-ip") ?? "");
  fwdHeaders.set("x-forwarded-host", incomingUrl.host);
  fwdHeaders.set("x-forwarded-proto", "https");
  fwdHeaders.set("x-edge-source", "cloudflare-workers");

  // Body has to be cached so we can retry it.
  let bodyBytes: ArrayBuffer | null = null;
  if (request.method !== "GET" && request.method !== "HEAD") {
    bodyBytes = await request.arrayBuffer();
  }

  let lastError: unknown = null;
  let attempts = 0;
  let retried = false;

  for (let i = 0; i < MAX_ATTEMPTS; i++) {
    attempts++;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

    try {
      const response = await fetch(targetUrl, {
        method: request.method,
        headers: fwdHeaders,
        body: bodyBytes ?? undefined,
        signal: controller.signal,
        // Workers does NOT need redirect:'manual' — default is fine
      });

      clearTimeout(timer);

      // Retry only if origin gave a 5xx that smells like cold-start.
      // 502 = bad gateway (Render router can't reach dyno yet)
      // 503 = service unavailable (dyno waking)
      // 504 = gateway timeout (dyno still booting)
      const isColdStart = response.status === 502 || response.status === 503 || response.status === 504;
      if (isColdStart && i < MAX_ATTEMPTS - 1) {
        retried = true;
        // Drain body so the connection releases.
        await response.body?.cancel();
        await new Promise((r) => setTimeout(r, RETRY_DELAY_MS));
        continue;
      }

      return { response, attempts, totalMs: Date.now() - start, retried };
    } catch (err) {
      clearTimeout(timer);
      lastError = err;
      if (i < MAX_ATTEMPTS - 1) {
        retried = true;
        await new Promise((r) => setTimeout(r, RETRY_DELAY_MS));
        continue;
      }
    }
  }

  // All retries exhausted: return a structured 503 so agents can backoff cleanly.
  const errBody = JSON.stringify({
    error: "origin_unreachable",
    message: "Upstream service did not respond. Edge retried " + attempts + " time(s).",
    detail: String(lastError ?? "see status code"),
    edge: "cloudflare-workers",
  });

  return {
    response: new Response(errBody, {
      status: 503,
      headers: {
        "content-type": "application/json",
        "retry-after": "30",
      },
    }),
    attempts,
    totalMs: Date.now() - start,
    retried: true,
  };
}
