import { createHash } from "node:crypto";

function token(value: string): string {
  const cleaned = value.trim().replace(/[^A-Za-z0-9._*-]+/g, "-").replace(/^-+|-+$/g, "");
  return (cleaned || "unknown").slice(0, 96);
}

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex").slice(0, 12);
}

export function figureId(sha256: string): string {
  return `fig:${sha256.slice(0, 16)}`;
}

export function detectionId(figId: string, bboxIndex: number): string {
  return `det:${figId}:molparser:${bboxIndex}`;
}

export function bboxId(figId: string, bboxIndex: number | null, bbox: readonly number[] | null): string {
  if (bboxIndex !== null) return `bbox:${figId}:figure-pixel:${bboxIndex}`;
  return `bbox:${figId}:figure-pixel:coord-${digest(JSON.stringify(bbox ?? []))}`;
}

export function reactionId(
  figId: string,
  kind: "example" | "template",
  identifier: string | null,
  duplicateOrdinal: number,
  legacyIndex: number,
): string {
  const key = identifier ? token(identifier) : `legacy-${legacyIndex}`;
  return `rxn:${figId}:${kind}:${key}:${duplicateOrdinal}`;
}

export function moleculeId(reaction: string, role: "reactant" | "product" | "condition", ordinal: number): string {
  return `mol:${reaction}:${role}:${ordinal}`;
}

export function evidenceId(figId: string, kind: string, key: string | number): string {
  return `evidence:${figId}:${token(kind)}:${token(String(key))}`;
}

export function artifactId(jobId: string, kind: string, sha256: string): string {
  return `artifact:${token(jobId)}:${token(kind)}:${sha256.slice(0, 12)}`;
}

export function eventId(jobId: string, attemptId: string, seq: number): string {
  return `event:${token(jobId)}:${token(attemptId)}:${String(seq).padStart(6, "0")}`;
}
