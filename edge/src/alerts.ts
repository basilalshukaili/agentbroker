// Telegram alert system for the Agent Broker edge worker.
//
// Sends Basil a Telegram message on rare, high-signal events:
//   1. First-revenue signal: total_businesses_found or total_messages_sent
//      transitions from 0 → >0 (fire once, 24h suppression).
//   2. Twilio low balance: external.services.twilio.balance < $2.00 (12h).
//   3. Vapi low balance:   external.services.vapi.balance   < $2.00 (12h).
//   4. Traffic milestones: total_agents_requested crossing 1k/5k/10k/50k/100k
//      (one alert per milestone, lifetime suppression).
//
// Design rules:
//   • If TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is empty, silently no-op.
//     The cron must never throw because alerts are misconfigured.
//   • KV writes are gated on "alert actually fires", so the daily 1k/day
//     free-tier KV budget is unaffected in steady state (zero writes).
//   • Cooldowns are stored as `fired_at` epoch-ms in KV; the check reads it,
//     compares to now, and skips if within the suppression window.
//   • Milestones use a per-threshold KV sentinel ("alert:milestone:<n>"). Once
//     set it never clears, so each milestone alerts at most once forever.
//   • Reads from `live:metrics` and (optionally) probe /healthz/external —
//     the metrics refresh in scheduledHandler is expected to run BEFORE
//     runAlertChecks so KV holds fresh data.
//
// Called by scheduledHandler in src/index.ts inside the
// `if (shouldRefreshKv)` branch (every 30 min).

type AlertsEnv = {
  CACHE: KVNamespace;
  ORIGIN_URL: string;
  TELEGRAM_BOT_TOKEN?: string;
  TELEGRAM_CHAT_ID?: string;
  PUBLIC_BASE_URL?: string;
};

// Cooldown windows (milliseconds).
const REVENUE_COOLDOWN_MS = 24 * 60 * 60 * 1000; // 24h
const BALANCE_COOLDOWN_MS = 12 * 60 * 60 * 1000; // 12h

// Low-balance threshold (USD).
const BALANCE_THRESHOLD_USD = 2.0;

// Lifetime traffic milestones (must be sorted ascending).
const MILESTONES: ReadonlyArray<number> = [1000, 5000, 10000, 50000, 100000];

// KV key helpers — namespaced under `alert:` so they're easy to scan.
const KV_FIRST_REVENUE_FIRED = "alert:first_revenue:fired_at";
const KV_TWILIO_LOW_FIRED = "alert:twilio_low:fired_at";
const KV_VAPI_LOW_FIRED = "alert:vapi_low:fired_at";
const kvMilestoneKey = (n: number) => `alert:milestone:${n}`;

/** Best-effort numeric coercion. Returns null if value can't be a finite number. */
function toFiniteNumber(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

/** Read live:metrics from KV and parse it. Returns {} on any failure. */
async function readLiveMetrics(kv: KVNamespace): Promise<Record<string, unknown>> {
  try {
    const raw = await kv.get("live:metrics", "text");
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    if (parsed && typeof parsed === "object") return parsed as Record<string, unknown>;
    return {};
  } catch {
    return {};
  }
}

/** Probe /healthz/external on the origin. Returns parsed body or null on failure. */
async function fetchExternalHealth(originUrl: string): Promise<Record<string, unknown> | null> {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch(originUrl + "/healthz/external", {
      headers: { "x-edge-probe": "cron-alerts" },
      signal: ctrl.signal,
    });
    clearTimeout(t);
    if (!r.ok) return null;
    const body = (await r.json()) as unknown;
    if (body && typeof body === "object") return body as Record<string, unknown>;
    return null;
  } catch {
    return null;
  }
}

/** Pull `external.services.<name>.balance` (number) out of a /healthz/external body. */
function extractBalance(health: Record<string, unknown> | null, name: string): number | null {
  if (!health) return null;
  const services = (health.services ?? (health.external as Record<string, unknown> | undefined)?.services) as
    | Record<string, unknown>
    | undefined;
  if (!services || typeof services !== "object") return null;
  const svc = services[name];
  if (!svc || typeof svc !== "object") return null;
  return toFiniteNumber((svc as Record<string, unknown>).balance);
}

/** UTC timestamp like "2026-05-19 10:42 UTC" for human-readable alert messages. */
function fmtUtc(now: number): string {
  const d = new Date(now);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(
    d.getUTCMinutes(),
  )} UTC`;
}

/** POST the message to Telegram. Returns true if the API accepted it. */
async function sendTelegram(token: string, chatId: string, text: string): Promise<boolean> {
  try {
    const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        parse_mode: "Markdown",
        disable_web_page_preview: true,
      }),
    });
    if (!r.ok) {
      console.warn("telegram sendMessage failed:", r.status, await r.text().catch(() => ""));
      return false;
    }
    return true;
  } catch (e) {
    console.warn("telegram sendMessage threw:", (e as Error).message);
    return false;
  }
}

/** Read a `fired_at` epoch-ms from KV. Returns 0 if missing/unparseable. */
async function readFiredAt(kv: KVNamespace, key: string): Promise<number> {
  try {
    const v = await kv.get(key, "text");
    if (!v) return 0;
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  } catch {
    return 0;
  }
}

/** Persist a `fired_at` timestamp. Swallow KV-quota failures. */
async function recordFiredAt(kv: KVNamespace, key: string, now: number): Promise<void> {
  try {
    await kv.put(key, String(now));
  } catch (e) {
    console.warn(`KV put ${key} failed:`, (e as Error).message);
  }
}

/** Persist a sentinel value (existence-only marker, e.g. milestone hit). */
async function recordSentinel(kv: KVNamespace, key: string, now: number): Promise<void> {
  try {
    await kv.put(key, String(now));
  } catch (e) {
    console.warn(`KV put ${key} failed:`, (e as Error).message);
  }
}

/** Has this milestone already fired? (Sentinel-style: key existence = fired.) */
async function milestoneHasFired(kv: KVNamespace, n: number): Promise<boolean> {
  try {
    const v = await kv.get(kvMilestoneKey(n), "text");
    return v !== null && v !== "";
  } catch {
    return false;
  }
}

/**
 * Run all alert checks. Safe to call from cron — never throws.
 * Caller is expected to have refreshed `live:metrics` in KV first.
 */
export async function runAlertChecks(env: AlertsEnv, kv: KVNamespace): Promise<void> {
  const token = env.TELEGRAM_BOT_TOKEN;
  const chatId = env.TELEGRAM_CHAT_ID;
  // Silent no-op if either secret is missing — alerts disabled.
  if (!token || !chatId) return;

  const metricsUrl =
    (env.PUBLIC_BASE_URL ?? "https://agent-broker-edge.basil-agent.workers.dev") + "/api/metrics";
  const now = Date.now();
  const ts = fmtUtc(now);

  const metrics = await readLiveMetrics(kv);

  // ---- 1. First-revenue signal -------------------------------------------
  // Fires when total_businesses_found OR total_messages_sent crosses 0 → >0.
  // 24h suppression once fired.
  const businessesFound = toFiniteNumber(metrics.total_businesses_found) ?? 0;
  const messagesSent = toFiniteNumber(metrics.total_messages_sent) ?? 0;
  if (businessesFound > 0 || messagesSent > 0) {
    const firedAt = await readFiredAt(kv, KV_FIRST_REVENUE_FIRED);
    if (now - firedAt > REVENUE_COOLDOWN_MS) {
      const which = messagesSent > 0 ? "messages_sent" : "businesses_found";
      const count = messagesSent > 0 ? messagesSent : businessesFound;
      const msg =
        `*Agent Broker* — first \`${which}\`!\n` +
        `Count: *${count}*\n` +
        `Time: ${ts}\n` +
        metricsUrl;
      const ok = await sendTelegram(token, chatId, msg);
      if (ok) await recordFiredAt(kv, KV_FIRST_REVENUE_FIRED, now);
    }
  }

  // ---- 2 & 3. Twilio / Vapi low balance ---------------------------------
  // Only probe /healthz/external if we actually need to (i.e. at least one of
  // the two alerts is OFF cooldown). Saves one origin round-trip when both
  // are still suppressed.
  const twilioFiredAt = await readFiredAt(kv, KV_TWILIO_LOW_FIRED);
  const vapiFiredAt = await readFiredAt(kv, KV_VAPI_LOW_FIRED);
  const twilioOnCooldown = now - twilioFiredAt <= BALANCE_COOLDOWN_MS;
  const vapiOnCooldown = now - vapiFiredAt <= BALANCE_COOLDOWN_MS;

  if (!twilioOnCooldown || !vapiOnCooldown) {
    const health = await fetchExternalHealth(env.ORIGIN_URL);

    if (!twilioOnCooldown) {
      const twilioBal = extractBalance(health, "twilio");
      if (twilioBal !== null && twilioBal < BALANCE_THRESHOLD_USD) {
        const msg =
          `*Agent Broker* — Twilio low balance!\n` +
          `Balance: *$${twilioBal.toFixed(2)}* (threshold $${BALANCE_THRESHOLD_USD.toFixed(2)})\n` +
          `Time: ${ts}\n` +
          `Top up: https://console.twilio.com/`;
        const ok = await sendTelegram(token, chatId, msg);
        if (ok) await recordFiredAt(kv, KV_TWILIO_LOW_FIRED, now);
      }
    }

    if (!vapiOnCooldown) {
      // Vapi field is only present when VAPI_API_KEY is set on origin.
      // extractBalance returns null when absent → treat as no-op.
      const vapiBal = extractBalance(health, "vapi");
      if (vapiBal !== null && vapiBal < BALANCE_THRESHOLD_USD) {
        const msg =
          `*Agent Broker* — Vapi low balance!\n` +
          `Balance: *$${vapiBal.toFixed(2)}* (threshold $${BALANCE_THRESHOLD_USD.toFixed(2)})\n` +
          `Time: ${ts}\n` +
          `Top up: https://dashboard.vapi.ai/`;
        const ok = await sendTelegram(token, chatId, msg);
        if (ok) await recordFiredAt(kv, KV_VAPI_LOW_FIRED, now);
      }
    }
  }

  // ---- 4. Traffic milestones --------------------------------------------
  // Fire at most once per threshold, ever. Sentinel KV key per milestone.
  const agentsRequested = toFiniteNumber(metrics.total_agents_requested) ?? 0;
  if (agentsRequested > 0) {
    for (const threshold of MILESTONES) {
      if (agentsRequested < threshold) break; // list is sorted; nothing further matches
      if (await milestoneHasFired(kv, threshold)) continue;
      const msg =
        `*Agent Broker* — traffic milestone reached!\n` +
        `\`total_agents_requested\` >= *${threshold.toLocaleString("en-US")}*\n` +
        `Current: *${agentsRequested.toLocaleString("en-US")}*\n` +
        `Time: ${ts}\n` +
        metricsUrl;
      const ok = await sendTelegram(token, chatId, msg);
      if (ok) await recordSentinel(kv, kvMilestoneKey(threshold), now);
    }
  }
}
