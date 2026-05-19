// x402 — Coinbase micropayment protocol (HTTP 402 + on-chain proof).
//
// Parallel to Paddle: agents with crypto wallets (Coinbase Agent Kit,
// x402-compatible MCP clients) pay per tool call in USDC on Base. The worker
// returns 402 Payment Required with payment requirements; the agent pays,
// then retries with X-PAYMENT-PROOF (tx hash) and X-PAYMENT-NONCE headers.
// The worker verifies the tx on Base RPC and proxies on success.
//
// Backward-compat: when env.X402_RECEIVER_ADDRESS is unset, this module is
// never invoked — the worker behaves as the Paddle-only path.

// USDC contract on Base mainnet (6 decimals).
const USDC_BASE_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";

// Free, public Base RPC. Free-tier rate limit is ~100 req/s; each verification
// is 2-3 calls, so well within budget.
const BASE_RPC_URL = "https://mainnet.base.org";

// ERC-20 Transfer(address,address,uint256) event topic.
const ERC20_TRANSFER_TOPIC =
  "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";

// Window in which a tx must have been mined to be accepted as proof.
const PROOF_MAX_AGE_SEC = 600;

// Pricing table — USD cents converted to USDC atomic units (USDC has 6 decimals,
// so $0.01 = 10000 atomic). Zero = free, short-circuits the gate.
//
// Design decision (2026-05-19): only write/cost-incurring operations require
// payment. Read tools (find_business, verify_business, get_status,
// get_outcome) and free probes (preview_cost, self_test) stay free even when
// X402_RECEIVER_ADDRESS is set. Rationale:
//   1) Catalog scorers (MCPScoringEngine, mcp.so indexer) probe tools/call on
//      read tools — gating them would drop us from listings.
//   2) Reads are near-zero cost for us to serve (CPU + cached snapshot).
//   3) Writes (send_message $0.05 SMS via Twilio, schedule_appointment $0.25
//      Cal.com booking, etc.) are where real cost-to-serve lives — gate those.
//   4) An agent can fully evaluate the service for free, then pay for action.
//   5) Paddle subscription path (when wired) covers the same writes with
//      different economics — both rails coexist.
const PRICING_ATOMIC: Readonly<Record<string, number>> = {
  // Free reads — evaluation tier, never gated.
  find_business: 0,
  verify_business: 0,
  get_status: 0,
  get_outcome: 0,
  preview_cost: 0,
  self_test: 0,
  // Paid writes — real cost-to-serve, x402-gated when receiver is configured.
  send_message: 50000,                  // $0.05
  capture_lead: 100000,                 // $0.10
  schedule_appointment: 250000,         // $0.25 attempt (success bonus stays subscription-only)
  send_transactional_confirmation: 30000, // $0.03
  handle_inbound: 80000,                // $0.08
  escalate_to_human: 500000,            // $0.50
  import_booking_url: 5000,             // $0.005
};

export function getRequiredAmount(toolName: string): number {
  return PRICING_ATOMIC[toolName] ?? 0;
}

export function isPricedTool(toolName: string): boolean {
  return (PRICING_ATOMIC[toolName] ?? 0) > 0;
}

export function generateNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let hex = "";
  for (let i = 0; i < bytes.length; i++) {
    hex += bytes[i].toString(16).padStart(2, "0");
  }
  return hex;
}

export type PaymentRequirements = {
  scheme: "exact-evm";
  chain: "base";
  network: "base-mainnet";
  recipient: string;
  amount_atomic: string;
  currency: "USDC";
  currency_address: string;
  nonce: string;
  expires_at_unix: number;
};

export function buildPaymentRequirements(
  toolName: string,
  recipient: string,
): { requirements: PaymentRequirements; nonce: string } {
  const amount = getRequiredAmount(toolName);
  const nonce = generateNonce();
  const requirements: PaymentRequirements = {
    scheme: "exact-evm",
    chain: "base",
    network: "base-mainnet",
    recipient,
    amount_atomic: String(amount),
    currency: "USDC",
    currency_address: USDC_BASE_ADDRESS,
    nonce,
    expires_at_unix: Math.floor(Date.now() / 1000) + PROOF_MAX_AGE_SEC,
  };
  return { requirements, nonce };
}

// ---- On-chain verification ---------------------------------------------------

type RpcResponse<T> = { result?: T; error?: { code: number; message: string } };

async function rpc<T>(method: string, params: unknown[]): Promise<T | null> {
  try {
    const res = await fetch(BASE_RPC_URL, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
    });
    if (!res.ok) return null;
    const json = (await res.json()) as RpcResponse<T>;
    if (json.error) return null;
    return json.result ?? null;
  } catch {
    return null;
  }
}

type TxReceipt = {
  blockNumber: string;
  status: string;
  to: string;
  logs: Array<{
    address: string;
    topics: string[];
    data: string;
  }>;
};

type TxByHash = {
  blockNumber: string | null;
  hash: string;
};

type BlockHeader = {
  number: string;
  timestamp: string;
};

function hexToBigInt(hex: string): bigint {
  return BigInt(hex);
}

function normalizeAddr(addr: string): string {
  return addr.toLowerCase();
}

// Decode a 32-byte topic into a 20-byte address (last 20 bytes).
function topicToAddress(topic: string): string {
  // topic is 0x + 64 hex chars; address is the last 40.
  if (!topic.startsWith("0x") || topic.length !== 66) return "";
  return "0x" + topic.slice(26).toLowerCase();
}

export type VerifyResult = { valid: boolean; reason?: string };

export async function verifyPaymentOnchain(
  txHash: string,
  expectedRecipient: string,
  expectedAmountAtomic: number,
  kvNonceKey: string,
  kv: KVNamespace,
): Promise<VerifyResult> {
  // Sanity: tx hash shape.
  if (!/^0x[0-9a-fA-F]{64}$/.test(txHash)) {
    return { valid: false, reason: "tx_hash_malformed" };
  }

  // Nonce must exist in KV and not be marked spent.
  const nonceState = await kv.get(kvNonceKey);
  if (nonceState === null) {
    return { valid: false, reason: "nonce_unknown_or_expired" };
  }
  if (nonceState === "spent") {
    return { valid: false, reason: "nonce_already_spent" };
  }

  // 1. eth_getTransactionByHash — tx exists and is mined.
  const tx = await rpc<TxByHash | null>("eth_getTransactionByHash", [txHash]);
  if (!tx) return { valid: false, reason: "tx_not_found" };
  if (!tx.blockNumber) return { valid: false, reason: "tx_not_mined" };

  // 2. eth_getTransactionReceipt — must be successful and have >= 1 confirmation.
  const receipt = await rpc<TxReceipt | null>("eth_getTransactionReceipt", [txHash]);
  if (!receipt) return { valid: false, reason: "receipt_not_found" };
  if (receipt.status !== "0x1") return { valid: false, reason: "tx_reverted" };

  // Tx must target the USDC contract on Base.
  if (normalizeAddr(receipt.to) !== normalizeAddr(USDC_BASE_ADDRESS)) {
    return { valid: false, reason: "tx_target_not_usdc" };
  }

  // 3 + 4. Scan logs for an ERC-20 Transfer to our recipient with sufficient amount.
  const expectedRecipientLc = normalizeAddr(expectedRecipient);
  let matched = false;
  for (const log of receipt.logs ?? []) {
    if (normalizeAddr(log.address) !== normalizeAddr(USDC_BASE_ADDRESS)) continue;
    if (!log.topics || log.topics.length < 3) continue;
    if (log.topics[0].toLowerCase() !== ERC20_TRANSFER_TOPIC) continue;
    const toAddr = topicToAddress(log.topics[2]);
    if (toAddr !== expectedRecipientLc) continue;

    let amount: bigint;
    try {
      amount = hexToBigInt(log.data);
    } catch {
      continue;
    }
    if (amount >= BigInt(expectedAmountAtomic)) {
      matched = true;
      break;
    }
  }
  if (!matched) {
    return { valid: false, reason: "no_matching_transfer_or_amount_too_low" };
  }

  // 5. Block timestamp within the freshness window.
  const block = await rpc<BlockHeader | null>("eth_getBlockByNumber", [
    receipt.blockNumber,
    false,
  ]);
  if (!block) return { valid: false, reason: "block_not_found" };
  const blockTs = Number(hexToBigInt(block.timestamp));
  const nowSec = Math.floor(Date.now() / 1000);
  if (nowSec - blockTs > PROOF_MAX_AGE_SEC) {
    return { valid: false, reason: "proof_too_old" };
  }

  // 6. Nonce already validated above (must exist & not be spent). Caller is
  // responsible for marking it spent after a successful return.
  return { valid: true };
}
