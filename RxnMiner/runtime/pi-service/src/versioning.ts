import { randomUUID } from "node:crypto";
import type { ProposedPatch, ReviewedSnapshot } from "./contracts.js";
import { contentHash } from "./security.js";

interface RawRecord {
  userId: string;
  jobId: string;
  bytes: Buffer;
  sha256: string;
}

export class PredictionVersionStore {
  private readonly raw = new Map<string, RawRecord>();
  private readonly patches = new Map<string, ProposedPatch>();
  private readonly snapshots = new Map<string, ReviewedSnapshot[]>();

  registerRaw(userId: string, jobId: string, bytes: Buffer): string {
    const key = this.key(userId, jobId);
    const sha256 = contentHash(bytes);
    const existing = this.raw.get(key);
    if (existing && !existing.bytes.equals(bytes)) {
      throw new Error("Raw prediction is immutable once registered");
    }
    this.raw.set(key, { userId, jobId, bytes: Buffer.from(bytes), sha256 });
    return sha256;
  }

  rawBytes(userId: string, jobId: string): Buffer {
    const record = this.requiredRaw(userId, jobId);
    return Buffer.from(record.bytes);
  }

  propose(input: Omit<ProposedPatch, "id" | "status" | "createdAt">): ProposedPatch {
    const raw = this.requiredRaw(input.userId, input.jobId);
    const snapshots = this.snapshots.get(this.key(input.userId, input.jobId)) ?? [];
    const currentSha = snapshots.at(-1)?.sha256 ?? raw.sha256;
    if (input.baseSha256 !== currentSha) throw new Error("Patch base is stale");
    if (input.operations.some((operation) => !operation.rationale || operation.evidenceIds.length === 0)) {
      throw new Error("Every patch operation requires rationale and evidence");
    }
    const patch: ProposedPatch = {
      ...structuredClone(input),
      id: randomUUID(),
      status: "proposed",
      createdAt: new Date().toISOString(),
    };
    this.patches.set(patch.id, patch);
    return structuredClone(patch);
  }

  apply(userId: string, patchId: string): ReviewedSnapshot {
    const patch = this.patches.get(patchId);
    if (!patch || patch.userId !== userId) throw new Error("Patch not found");
    if (patch.status !== "proposed") throw new Error("Patch was already adjudicated");
    const raw = this.requiredRaw(userId, patch.jobId);
    const key = this.key(userId, patch.jobId);
    const snapshots = this.snapshots.get(key) ?? [];
    const base = structuredClone(snapshots.at(-1)?.document ?? JSON.parse(raw.bytes.toString("utf8")));
    const currentSha = snapshots.at(-1)?.sha256 ?? raw.sha256;
    if (patch.baseSha256 !== currentSha) throw new Error("Patch base is stale");
    for (const operation of patch.operations) applyOperation(base, operation);
    const canonical = JSON.stringify(base);
    const snapshot: ReviewedSnapshot = {
      id: randomUUID(),
      userId,
      jobId: patch.jobId,
      version: snapshots.length + 1,
      parentSha256: currentSha,
      sha256: contentHash(canonical),
      patchId,
      createdAt: new Date().toISOString(),
      document: base,
    };
    patch.status = "applied";
    snapshots.push(snapshot);
    this.snapshots.set(key, snapshots);
    return structuredClone(snapshot);
  }

  private requiredRaw(userId: string, jobId: string): RawRecord {
    const record = this.raw.get(this.key(userId, jobId));
    if (!record) throw new Error("Raw prediction not found");
    return record;
  }

  private key(userId: string, jobId: string): string {
    return `${userId}\0${jobId}`;
  }
}

function applyOperation(
  document: unknown,
  operation: ProposedPatch["operations"][number],
): void {
  if (!operation.path.startsWith("/") || operation.path === "/") {
    throw new Error("Patch path must address a field below the document root");
  }
  const segments = operation.path
    .slice(1)
    .split("/")
    .map((segment) => segment.replaceAll("~1", "/").replaceAll("~0", "~"));
  if (segments.some((segment) => ["__proto__", "prototype", "constructor"].includes(segment))) {
    throw new Error("Unsafe patch path segment");
  }
  let parent = document as Record<string, unknown> | unknown[];
  for (const segment of segments.slice(0, -1)) {
    parent = Array.isArray(parent)
      ? (parent[Number(segment)] as Record<string, unknown> | unknown[])
      : (parent[segment] as Record<string, unknown> | unknown[]);
    if (!parent || typeof parent !== "object") throw new Error("Patch path does not exist");
  }
  const leaf = segments.at(-1) ?? "";
  if (Array.isArray(parent)) {
    const index = Number(leaf);
    if (!Number.isInteger(index)) throw new Error("Array patch path requires an integer index");
    if (operation.op === "remove") parent.splice(index, 1);
    else if (operation.op === "add") parent.splice(index, 0, structuredClone(operation.value));
    else parent[index] = structuredClone(operation.value);
  } else {
    if (operation.op === "remove") delete parent[leaf];
    else parent[leaf] = structuredClone(operation.value);
  }
}
