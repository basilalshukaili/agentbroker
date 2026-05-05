// KV-backed edge cache.
//
// Stores response body + metadata under a single composite key. Expiration is
// handled by KV's expirationTtl, so we don't need to store timestamps.

export type CachedResponse = {
  status: number;
  headers: Record<string, string>;
  bodyB64: string; // base64-encoded body
  storedAt: number;
};

function bytesToB64(bytes: ArrayBuffer): string {
  const arr = new Uint8Array(bytes);
  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < arr.length; i += chunkSize) {
    binary += String.fromCharCode.apply(
      null,
      Array.from(arr.subarray(i, i + chunkSize)),
    );
  }
  return btoa(binary);
}

function b64ToBytes(b64: string): Uint8Array {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function keyFor(method: string, pathname: string, search: string): string {
  return `v1:${method}:${pathname}${search}`;
}

export async function readCache(
  kv: KVNamespace,
  method: string,
  pathname: string,
  search: string,
): Promise<CachedResponse | null> {
  const key = keyFor(method, pathname, search);
  const json = await kv.get(key, "json");
  return json as CachedResponse | null;
}

export async function writeCache(
  kv: KVNamespace,
  method: string,
  pathname: string,
  search: string,
  response: Response,
  ttlSeconds: number,
): Promise<ArrayBuffer> {
  const bodyBytes = await response.arrayBuffer();
  const headers: Record<string, string> = {};
  response.headers.forEach((v, k) => {
    // Don't persist hop-by-hop or volatile headers.
    if (
      k === "transfer-encoding" ||
      k === "connection" ||
      k === "set-cookie" ||
      k === "cf-ray" ||
      k.startsWith("x-edge-")
    ) {
      return;
    }
    headers[k] = v;
  });

  const cached: CachedResponse = {
    status: response.status,
    headers,
    bodyB64: bytesToB64(bodyBytes),
    storedAt: Date.now(),
  };

  const key = keyFor(method, pathname, search);
  await kv.put(key, JSON.stringify(cached), { expirationTtl: Math.max(60, ttlSeconds) });
  return bodyBytes;
}

export function buildResponseFromCache(cached: CachedResponse): Response {
  const bytes = b64ToBytes(cached.bodyB64);
  return new Response(bytes, {
    status: cached.status,
    headers: cached.headers,
  });
}
