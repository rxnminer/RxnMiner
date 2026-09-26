import { createHash } from "node:crypto";
import { constants, realpathSync } from "node:fs";
import { open, stat } from "node:fs/promises";
import path from "node:path";
import { buildFigureManifest } from "./adapters/figure/adapter.js";
import {
  artifactId,
  figureId,
} from "./adapters/figure/ids.js";
import type {
  FigureArtifactRef,
  FigureFixtureMetadata,
  FigureJobManifest,
} from "./adapters/figure/contracts.js";
import type {
  LocalInputArtifact,
  RuntimeJobResult,
} from "./contracts.js";
import type { SkillManifestEntry } from "./skills.js";
import { assertNoGroundTruth } from "./security.js";

export interface LiveToolCapture {
  name: string;
  isError: boolean;
  payload?: Record<string, unknown>;
}

export interface FigureClosureAudit {
  valid: boolean;
  assemblyRevision: number;
  lastAssembleIndex: number;
  lastRenderIndex: number;
  postAssemblyVerificationIndexes: number[];
  issues: string[];
}

export class FigureSealBlockedError extends Error {
  constructor(public readonly issues: string[], public readonly audit: FigureClosureAudit) {
    super(`Figure seal blocked: ${issues.join("; ")}`);
    this.name = "FigureSealBlockedError";
  }
}

export function auditFigureToolClosure(tools: LiveToolCapture[]): FigureClosureAudit {
  const complete = tools.map((tool, index) => ({ tool, index }))
    .filter(({ tool }) => !tool.isError);
  const assemblies = complete.filter(({ tool }) => tool.name === "assemble");
  const lastAssembleIndex = assemblies.at(-1)?.index ?? -1;
  const lastRenderIndex = complete.filter(({ tool }) => tool.name === "render_reactions").at(-1)?.index ?? -1;
  const postAssemblyVerificationIndexes = complete
    .filter(({ tool, index }) => tool.name === "verify_smiles" && index > lastAssembleIndex)
    .map(({ index }) => index);
  const issues: string[] = [];
  if (lastAssembleIndex < 0) issues.push("no successful assemble call");
  if (postAssemblyVerificationIndexes.length) {
    issues.push(`${postAssemblyVerificationIndexes.length} successful verify_smiles call(s) occurred after the latest assemble`);
  }
  if (lastRenderIndex <= lastAssembleIndex) issues.push("render_reactions did not complete after the latest assemble");
  return {
    valid: issues.length === 0,
    assemblyRevision: assemblies.length,
    lastAssembleIndex,
    lastRenderIndex,
    postAssemblyVerificationIndexes,
    issues,
  };
}

export async function buildLiveFigureResult(input: {
  sessionId: string;
  jobId: string;
  workspaceId: string;
  workspace: string;
  figure: LocalInputArtifact & { workspacePath: string };
  provider: string;
  modelId: string;
  skills: SkillManifestEntry[];
  tools: LiveToolCapture[];
}): Promise<RuntimeJobResult | undefined> {
  const closure = auditFigureToolClosure(input.tools);
  if (closure.lastAssembleIndex < 0) return undefined;
  if (!closure.valid) throw new FigureSealBlockedError(closure.issues, closure);
  const assembled = [...input.tools].reverse().find((tool) =>
    tool.name === "assemble" && !tool.isError && typeof tool.payload?.v2_path === "string");
  if (!assembled?.payload) return undefined;
  const predictionRead = await readWorkspaceArtifact(
    input.workspace,
    String(assembled.payload.v2_path),
  );
  if (!predictionRead) return undefined;
  const predictionPath = predictionRead.path;
  const prediction = parseJson(predictionRead.bytes.toString("utf8"), "assembled V2");
  assertNoGroundTruth(prediction);

  const detected = [...input.tools].reverse().find((tool) =>
    tool.name === "detect" && !tool.isError && tool.payload);
  if (!detected?.payload) return undefined;
  const molparserRead = typeof detected?.payload?.raw_molparser_json === "string"
    ? await readWorkspaceArtifact(input.workspace, detected.payload.raw_molparser_json)
    : undefined;
  if (!molparserRead) return undefined;
  const molparser = parseJson(molparserRead.bytes.toString("utf8"), "MolParser artifact");
  assertNoGroundTruth(molparser);

  const predictionSha = sha256(predictionRead.bytes);
  const molparserBytes = molparserRead.bytes;
  const molparserSha = sha256(molparserBytes);
  const validation = validateFigurePrediction(prediction);
  const lineageIssues = validateDrawnLineage(prediction, molparser);
  if (lineageIssues.length) throw new FigureSealBlockedError(lineageIssues, closure);
  const skill = input.skills.find((entry) => entry.name === "reaction-extract");
  if (!skill) throw new Error("Pinned reaction-extract skill is missing");
  const provenance = record(detected?.payload?.molparser_provenance);
  const metadata: FigureFixtureMetadata = {
    jobId: input.jobId,
    attemptId: `attempt:${input.sessionId}`,
    input: {
      figureId: figureId(input.figure.sha256),
      sha256: input.figure.sha256,
      mediaType: input.figure.mediaType,
      locator: input.figure.workspacePath,
      locatorScope: "job_mount",
    },
    request: {
      task: "extract_all",
      contextMode: "figure",
      inferencePolicy: "deterministic_reverse_allowed",
    },
    skill: {
      commit: skill.sha256,
      name: "reaction-extract",
    },
    model: {
      provider: input.provider,
      id: input.modelId,
    },
    molparser: {
      locator: workspaceRelative(input.workspace, molparserRead.path),
      sha256: molparserSha,
      cacheKey: typeof provenance.cache_key === "string"
        ? provenance.cache_key : input.figure.sha256,
      status: provenance.source === "live_molparser"
        ? "live_call" : "cache_hit",
    },
    prediction: {
      locator: workspaceRelative(input.workspace, predictionPath),
      sha256: predictionSha,
    },
    toolSequence: input.tools.map((tool) => `${tool.name}:${tool.isError ? "error" : "complete"}`),
    startedAt: new Date().toISOString(),
    terminalStatus: validation.issueCount === 0 ? "review_ready" : "partial",
    validation,
    notes: [
      "Live Pi SDK extraction; no GT or adjudication mounted.",
      "Connectivity and strict stereo remain unknown unless explicitly proven by tool evidence.",
    ],
  };
  const manifest = buildFigureManifest(metadata, prediction, molparser);
  await addRenderedArtifacts(manifest, input);
  const inputArtifact = manifest.artifacts.find((artifact) => artifact.kind === "input_figure");
  const predictionArtifact = manifest.artifacts.find((artifact) => artifact.kind === "prediction_v2");
  const overviewArtifact = manifest.artifacts.find((artifact) => artifact.kind === "overview_render");
  const reactionsArtifact = manifest.artifacts.find((artifact) => artifact.kind === "reaction_render");
  if (!inputArtifact) throw new Error("Live Figure manifest has no input artifact");

  const result: RuntimeJobResult = {
    schemaVersion: "chemrxn.runtime-result.v1",
    sessionId: input.sessionId,
    jobId: input.jobId,
    contextMode: "figure",
    source: {
      mode: "live_pi_sdk",
      reconstructed: false,
      liveEventCapture: true,
      inputArtifactIds: [input.figure.id],
      workspaceId: input.workspaceId,
    },
    domainContract: "chemrxn.figure-job.v1",
    terminalStatus: manifest.terminalStatus,
    manifest,
    createdAt: new Date().toISOString(),
    gtIncluded: false,
    viewer: {
      kind: "figure",
      inputArtifactId: inputArtifact.id,
      ...(predictionArtifact ? { predictionArtifactId: predictionArtifact.id } : {}),
      ...(overviewArtifact ? { overviewArtifactId: overviewArtifact.id } : {}),
      ...(reactionsArtifact ? { reactionsArtifactId: reactionsArtifact.id } : {}),
    },
  };
  assertNoGroundTruth(result);
  return result;
}

async function addRenderedArtifacts(
  manifest: FigureJobManifest,
  input: Pick<Parameters<typeof buildLiveFigureResult>[0], "workspace" | "jobId" | "tools">,
): Promise<void> {
  const candidates: Array<{
    tool: string;
    field: string;
    kind: FigureArtifactRef["kind"];
    mediaType: string;
  }> = [
    { tool: "detect", field: "overview_png", kind: "overview_render", mediaType: "image/png" },
    { tool: "render_reactions", field: "html_path", kind: "reaction_render", mediaType: "text/html" },
  ];
  for (const candidate of candidates) {
    const capture = [...input.tools].reverse().find((tool) =>
      tool.name === candidate.tool
      && !tool.isError
      && typeof tool.payload?.[candidate.field] === "string");
    if (!capture?.payload) continue;
    const artifact = await readWorkspaceArtifact(
      input.workspace,
      String(capture.payload[candidate.field]),
    );
    if (!artifact) continue;
    const digest = sha256(artifact.bytes);
    manifest.artifacts.push({
      id: artifactId(input.jobId, candidate.kind, digest),
      kind: candidate.kind,
      sha256: digest,
      mediaType: candidate.mediaType,
      locator: workspaceRelative(input.workspace, artifact.path),
      immutable: true,
      reusableAfterCancel: true,
      postHoc: false,
    });
  }
}

function validateDrawnLineage(value: unknown, molparserValue: unknown): string[] {
  const prediction = record(value);
  const issues: string[] = [];
  const rawMd5ByBbox = new Map<number, string>();
  array(molparserValue).forEach((itemValue) => {
    const item = record(itemValue);
    if (Number.isInteger(item.bbox_index) && typeof item.md5 === "string" && item.md5) {
      rawMd5ByBbox.set(item.bbox_index as number, item.md5);
    }
  });
  const visit = (moleculeValue: unknown, label: string) => {
    const molecule = record(moleculeValue);
    const source = record(molecule._source);
    const identifier = typeof molecule.identifier === "string" ? molecule.identifier : "";
    const claimed = identifier.match(/\bbbox\s*#?\s*(\d+)\b/i);
    const bboxIndex = Number.isInteger(source.bbox_index)
      ? source.bbox_index as number
      : Number.isInteger(molecule.bbox_index) ? molecule.bbox_index as number : undefined;
    const md5 = typeof source.molparser_md5 === "string"
      ? source.molparser_md5
      : typeof molecule.molparser_md5 === "string" ? molecule.molparser_md5 : "";
    if (claimed && bboxIndex !== Number(claimed[1])) {
      issues.push(`${label} claims bbox ${claimed[1]} without matching machine-readable bbox_index`);
    }
    const claimsMolParser = source.origin === "molparser" || source.type === "image_structure_molparser";
    if (claimed || claimsMolParser) {
      if (!md5) issues.push(`${label} has drawn/MolParser provenance without molparser_md5`);
      else if (bboxIndex === undefined || rawMd5ByBbox.get(bboxIndex) !== md5) {
        issues.push(`${label} bbox_index/molparser_md5 does not join the immutable raw MolParser record`);
      }
    }
  };
  array(prediction.reaction_templates).forEach((reactionValue, reactionIndex) => {
    const reaction = record(reactionValue);
    for (const role of ["reactants", "products"] as const) {
      array(reaction[role]).forEach((molecule, moleculeIndex) =>
        visit(molecule, `template ${reactionIndex + 1} ${role}[${moleculeIndex}]`));
    }
  });
  array(prediction.selected_examples).forEach((reactionValue, reactionIndex) => {
    const reaction = record(reactionValue);
    for (const role of ["reactants", "products"] as const) {
      array(reaction[role]).forEach((molecule, moleculeIndex) =>
        visit(molecule, `example ${reactionIndex + 1} ${role}[${moleculeIndex}]`));
    }
  });
  return issues;
}

function validateFigurePrediction(value: unknown): FigureFixtureMetadata["validation"] {
  const prediction = record(value);
  const examples = array(prediction.selected_examples);
  const issues: string[] = [];
  examples.forEach((item, index) => {
    const reaction = record(item);
    if (array(reaction.reactants).length === 0) issues.push(`example ${index + 1} has no reactants`);
    if (array(reaction.products).length === 0 && record(reaction._outcome).is_failed !== true) {
      issues.push(`example ${index + 1} has no products and is not marked failed`);
    }
  });
  if (examples.length === 0) issues.push("prediction contains no concrete examples");
  return {
    connectivity: "unknown",
    strictStereo: "unknown",
    basis: issues.length
      ? `Live structural completeness audit: ${issues.slice(0, 5).join("; ")}`
      : "Live structural completeness audit passed; connectivity and strict stereo were not independently adjudicated.",
    issueCount: issues.length,
    evidenceIds: [],
  };
}

export function artifactFromLiveResult(
  result: RuntimeJobResult,
  artifactIdValue: string,
): { locator: string; mediaType: string; sha256: string } | undefined {
  if (result.source.mode !== "live_pi_sdk") return undefined;
  const manifest = record(result.manifest);
  const artifacts = array(manifest.artifacts).map(record);
  const artifact = artifacts.find((item) => item.id === artifactIdValue);
  if (
    !artifact
    || typeof artifact.locator !== "string"
    || typeof artifact.mediaType !== "string"
    || typeof artifact.sha256 !== "string"
  ) return undefined;
  return {
    locator: artifact.locator,
    mediaType: artifact.mediaType,
    sha256: artifact.sha256,
  };
}

export function safeWorkspacePath(workspace: string, value: string): string | undefined {
  const root = path.resolve(workspace);
  const candidate = path.isAbsolute(value) ? path.resolve(value) : path.resolve(root, value);
  try {
    // Tool subprocesses can report the canonical macOS spelling (`/private/tmp`)
    // while Node retains the equivalent launcher spelling (`/tmp`). Compare the
    // resolved filesystem identities, then return a path rooted in the launcher's
    // namespace so later workspace-relative artifact locators remain stable.
    const canonicalRoot = realpathSync(root);
    const canonicalCandidate = realpathSync(candidate);
    const canonicalRelative = path.relative(canonicalRoot, canonicalCandidate);
    if (!safeWorkspaceRelative(canonicalRelative)) return undefined;
    return path.join(root, canonicalRelative);
  } catch {
    // Every caller in this module resolves an artifact for an immediate read.
    // Fail closed when either path cannot be canonicalized: lexical fallback
    // would accept a missing child below a symlinked directory that escapes the
    // workspace, leaving a TOCTOU window before the asynchronous read.
    return undefined;
  }
}

export async function readWorkspaceArtifact(
  workspace: string,
  value: string,
): Promise<{ path: string; bytes: Buffer } | undefined> {
  const target = safeWorkspacePath(workspace, value);
  if (!target) return undefined;
  let handle;
  try {
    // Pin the exact inode before reading. O_NOFOLLOW rejects a last-component
    // symlink; the post-open canonical path + inode comparison catches an
    // ancestor swapped between resolution and open. The subsequent read uses
    // this same descriptor, so a later pathname swap cannot redirect it.
    handle = await open(target, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    const opened = await handle.stat();
    if (!opened.isFile()) return undefined;
    const canonicalRoot = realpathSync(path.resolve(workspace));
    const canonicalTarget = realpathSync(target);
    const relative = path.relative(canonicalRoot, canonicalTarget);
    if (!safeWorkspaceRelative(relative)) return undefined;
    const current = await stat(canonicalTarget);
    if (opened.dev !== current.dev || opened.ino !== current.ino) return undefined;
    return { path: target, bytes: await handle.readFile() };
  } catch {
    return undefined;
  } finally {
    await handle?.close().catch(() => undefined);
  }
}

function safeWorkspaceRelative(relative: string): boolean {
  return relative !== ""
    && relative !== ".."
    && !relative.startsWith(`..${path.sep}`)
    && !path.isAbsolute(relative);
}

function workspaceRelative(workspace: string, target: string): string {
  const relative = path.relative(path.resolve(workspace), path.resolve(target));
  if (!relative || relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new Error("Artifact is outside the live session workspace");
  }
  return relative.split(path.sep).join("/");
}

function parseJson(value: string, label: string): unknown {
  try {
    return JSON.parse(value) as unknown;
  } catch {
    throw new Error(`Invalid ${label} JSON`);
  }
}

function sha256(value: Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}
