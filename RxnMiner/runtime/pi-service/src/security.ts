import { createHash, randomUUID } from "node:crypto";
import type { ArtifactRef, CreateSessionRequest, MountManifest } from "./contracts.js";
import { RuntimePolicyError } from "./errors.js";

const ALLOWED_BY_CONTEXT = {
  figure: new Set<ArtifactRef["kind"]>(["figure", "selection"]),
} as const;

const CREDENTIAL_FIELD_NAMES = new Set([
  "token",
  "accesstoken",
  "refreshtoken",
  "idtoken",
  "sessiontoken",
  "securitytoken",
  "apitoken",
  "bearertoken",
  "password",
  "passwd",
  "secret",
  "clientsecret",
  "secretkey",
  "privatekey",
  "signingkey",
  "credential",
  "credentials",
  "apikey",
]);

export function buildMountManifest(request: CreateSessionRequest, sessionId = randomUUID()): MountManifest {
  if (request.contextMode !== "figure") throw new RuntimePolicyError("INVALID_CONTEXT", "Only reaction images are supported");
  const allowed = ALLOWED_BY_CONTEXT.figure;
  const mounts = request.artifacts.map((artifact, index) => {
    if (!allowed.has(artifact.kind)) {
      throw new RuntimePolicyError(
        "INVALID_CONTEXT",
        `${artifact.kind} is not allowed in ${request.contextMode} context`,
      );
    }
    return {
      ...artifact,
      mountPath: `/workspace/input/${String(index).padStart(3, "0")}-${safeName(artifact.id)}`,
      readOnly: true as const,
    };
  });
  return {
    sessionId,
    userId: request.userId,
    jobId: request.jobId,
    contextMode: request.contextMode,
    mounts,
    gtIncluded: false,
  };
}

export function assertNoGroundTruth(value: unknown): void {
  const serialized = JSON.stringify(value).toLowerCase();
  if (
    serialized.includes("gt_sentinel") ||
    serialized.includes("ground_truth") ||
    serialized.includes("/gt/") ||
    serialized.includes("official_gt")
  ) {
    throw new RuntimePolicyError("INVALID_CONTEXT", "Ground truth is forbidden in agent context");
  }
}

export function redactSecrets(value: unknown, secrets: readonly string[] = []): unknown {
  let serialized = JSON.stringify(value);
  if (typeof serialized !== "string") return value;
  for (const secret of secrets.filter((item) => item.length >= 8)) {
    serialized = serialized.replaceAll(secret, "[REDACTED]");
  }
  serialized = serialized
    .replace(/(sk-[A-Za-z0-9_-]{12,})/g, "[REDACTED]")
    .replace(/("(?:token|apiKey|credential)"\s*:\s*")[^"]+"/gi, "$1[REDACTED]\"");
  return redactCredentialFields(JSON.parse(serialized));
}

/**
 * Reject values that would need credential redaction at a durable boundary.
 * Event callers may redact first; task/session/result writers must never rely
 * on silently changing authoritative data.
 */
export function assertNoPersistedSecrets(
  value: unknown,
  secrets: readonly string[] = [],
): void {
  const serialized = JSON.stringify(value);
  if (typeof serialized !== "string") return;
  const redacted = JSON.stringify(redactSecrets(value, secrets));
  const hasExplicitSecret = secrets
    .filter((secret) => secret.length >= 8)
    .some((secret) => serialized.includes(secret));
  const hasAuthorization = /"authorization"\s*:\s*"(?!\[REDACTED\])[^\"]+"/i.test(serialized);
  const hasBearer = /\bBearer\s+[A-Za-z0-9._~+/=-]{8,}/i.test(serialized);
  if (hasExplicitSecret || hasAuthorization || hasBearer || serialized !== redacted) {
    throw new RuntimePolicyError(
      "CREDENTIAL_PERSISTENCE_FORBIDDEN",
      "Credentials are forbidden in durable task state",
      400,
    );
  }
}

function redactCredentialFields(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redactCredentialFields);
  if (!value || typeof value !== "object") return value;
  const redacted: Record<string, unknown> = {};
  for (const [key, nested] of Object.entries(value)) {
    redacted[key] = isCredentialField(key) && isUnredactedCredentialValue(nested)
      ? "[REDACTED]"
      : redactCredentialFields(nested);
  }
  return redacted;
}

function isCredentialField(key: string): boolean {
  const normalized = key.toLowerCase().replace(/[^a-z0-9]/g, "");
  return CREDENTIAL_FIELD_NAMES.has(normalized)
    || normalized.endsWith("password")
    || normalized.endsWith("secret")
    || normalized.endsWith("privatekey");
}

function isUnredactedCredentialValue(value: unknown): boolean {
  if (value == null || value === false || value === 0 || value === "") return false;
  return value !== "[REDACTED]";
}

export function contentHash(content: string | Buffer): string {
  return createHash("sha256").update(content).digest("hex");
}

function safeName(value: string): string {
  return value.replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 96);
}
