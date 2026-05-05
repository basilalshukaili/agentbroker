// Embedded snapshots of every discovery payload, captured 2026-05-05 from
// the origin (https://smb-broker.onrender.com). These ship inside the worker
// bundle so discovery requests are served entirely from edge code with zero
// origin contact — the worker can outlive Render and still serve agents.
//
// Cron refreshes a "live" copy in KV every 2 min; the edge handler uses the
// live copy if present, else falls back to these snapshots.

import manifestRaw from "./manifest.json";
import agentsJsonRaw from "./agents.json";
import aiPluginJsonRaw from "./ai-plugin.json";
import openaiToolsJsonRaw from "./openai-tools.json";
import anthropicToolsJsonRaw from "./anthropic-tools.json";
import mcpJsonRaw from "./mcp.json";
import agentServiceJsonRaw from "./agent-service.json";
import supplyPlatformsRaw from "./supply-platforms.json";
import jurisdictionsRaw from "./jurisdictions.json";
import mcpToolsListRaw from "./mcp-tools-list.json";
import mcpInitializeRaw from "./mcp-initialize.json";

import llmsTxtRaw from "./llms.txt";
import llmsFullTxtRaw from "./llms-full.txt";
import openapiYamlRaw from "./openapi.yaml";

// Origin URLs to rewrite to the public-facing edge URL.
// Order matters — longer URLs must come first to avoid partial replacements.
const URL_PATTERNS: readonly string[] = [
  "https://agentbroker.qzz.io",
  "https://smb-broker.onrender.com",
  "https://api.smb-broker.example/v1",
  "https://api.smb-broker.example",
] as const;

function rewriteUrlsInString(s: string, replacement: string): string {
  let out = s;
  for (const orig of URL_PATTERNS) {
    out = out.split(orig).join(replacement);
  }
  return out;
}

function rewriteUrls<T>(input: T, replacement: string): T {
  if (typeof input === "string") {
    return rewriteUrlsInString(input, replacement) as unknown as T;
  }
  if (Array.isArray(input)) {
    return input.map((x) => rewriteUrls(x, replacement)) as unknown as T;
  }
  if (input !== null && typeof input === "object") {
    const result: Record<string, unknown> = {};
    for (const k of Object.keys(input as object)) {
      result[k] = rewriteUrls((input as Record<string, unknown>)[k], replacement);
    }
    return result as unknown as T;
  }
  return input;
}

export type Snapshots = {
  manifest: unknown;
  agentService: unknown;
  agentsJson: unknown;
  aiPluginJson: unknown;
  openaiToolsJson: unknown;
  anthropicToolsJson: unknown;
  mcpJson: unknown;
  supplyPlatforms: unknown;
  jurisdictions: unknown;
  mcpToolsList: unknown;
  mcpInitialize: unknown;
  llmsTxt: string;
  llmsFullTxt: string;
  openapiYaml: string;
};

let cached: { url: string; data: Snapshots } | null = null;

export function getSnapshots(publicBaseUrl: string): Snapshots {
  if (cached && cached.url === publicBaseUrl) return cached.data;
  const data: Snapshots = {
    manifest: rewriteUrls(manifestRaw, publicBaseUrl),
    agentService: rewriteUrls(agentServiceJsonRaw, publicBaseUrl),
    agentsJson: rewriteUrls(agentsJsonRaw, publicBaseUrl),
    aiPluginJson: rewriteUrls(aiPluginJsonRaw, publicBaseUrl),
    openaiToolsJson: rewriteUrls(openaiToolsJsonRaw, publicBaseUrl),
    anthropicToolsJson: rewriteUrls(anthropicToolsJsonRaw, publicBaseUrl),
    mcpJson: rewriteUrls(mcpJsonRaw, publicBaseUrl),
    supplyPlatforms: supplyPlatformsRaw,
    jurisdictions: jurisdictionsRaw,
    mcpToolsList: mcpToolsListRaw,
    mcpInitialize: mcpInitializeRaw,
    llmsTxt: rewriteUrlsInString(llmsTxtRaw, publicBaseUrl),
    llmsFullTxt: rewriteUrlsInString(llmsFullTxtRaw, publicBaseUrl),
    openapiYaml: rewriteUrlsInString(openapiYamlRaw, publicBaseUrl),
  };
  cached = { url: publicBaseUrl, data };
  return data;
}

// Manifest version helper — derives /manifest/version response from full manifest.
export function manifestVersion(snapshots: Snapshots): unknown {
  const m = snapshots.manifest as { service?: { version?: string }; operations?: unknown[] };
  return {
    version: m.service?.version ?? "0.1.0",
    last_updated: "2026-05-05T05:00:00Z",
    service_name: "smb-broker",
    operation_count: Array.isArray(m.operations) ? m.operations.length : 13,
  };
}

// Manifest ops summary — lightweight version for /manifest/ops.
export function manifestOps(snapshots: Snapshots): unknown {
  const m = snapshots.manifest as { operations?: Array<Record<string, unknown>> };
  const ops = m.operations ?? [];
  return {
    operations: ops.map((op) => ({
      name: op.name,
      description: op.description,
      when_to_use: op.when_to_use,
      execution_profile: op.execution_profile,
      cost_model: op.cost_model,
      slo: op.slo,
    })),
  };
}
