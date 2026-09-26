import { createHash, randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import type {
  ContextMode,
  RuntimeEventType,
  RuntimeJobResult,
  SealedReplayFixture,
  SessionRecord,
} from "./contracts.js";
import type { FigureEvent, FigureJobManifest } from "./adapters/figure/contracts.js";
import { loadFigureFixture } from "./adapters/figure/fixture.js";
import { RuntimePolicyError } from "./errors.js";
import type { RuntimeStore } from "./store.js";

export interface SealedReplayJob {
  session: SessionRecord;
  result: RuntimeJobResult;
}

export const SEALED_REPLAY_CATALOG: ReadonlyArray<{
  fixture: SealedReplayFixture;
  contextMode: ContextMode;
  label: string;
  reconstructed: true;
}> = [
  {
    fixture: "demo-figure",
    contextMode: "figure",
    label: "Demo figure · 24 reactions",
    reconstructed: true,
  },
];

type DomainEvent = FigureEvent;

interface LoadedReplay {
  contextMode: ContextMode;
  contract: RuntimeJobResult["domainContract"];
  manifest: FigureJobManifest;
  events: DomainEvent[];
  terminalStatus: RuntimeJobResult["terminalStatus"];
  liveEventCapture: boolean;
  viewer?: RuntimeJobResult["viewer"];
}

export class SealedReplayJobService {
  constructor(
    private readonly store: RuntimeStore,
    private readonly fixturesRoot = defaultFixturesRoot(),
    private readonly repoRoot = defaultRepoRoot(),
  ) {}

  async create(
    userId: string,
    fixture: SealedReplayFixture,
    retryOfSessionId?: string,
  ): Promise<SealedReplayJob> {
    const loaded = await this.load(fixture);
    const now = new Date().toISOString();
    const sessionId = randomUUID();
    const jobId = `sealed-demo:${fixture}:${sessionId.slice(0, 8)}`;
    const session: SessionRecord = {
      id: sessionId,
      userId,
      jobId,
      task: "extract_all",
      status: "creating",
      authMode: "sealed_replay",
      createdAt: now,
      updatedAt: now,
    };
    this.store.putSession(session);
    this.store.appendEvent(userId, sessionId, "session.created", {
      jobId,
      contextMode: loaded.contextMode,
      source: {
        mode: "sealed_replay",
        fixture,
        reconstructed: true,
        liveEventCapture: loaded.liveEventCapture,
      },
      ...(retryOfSessionId ? { retryOfSessionId } : {}),
    });
    session.status = "running";
    session.updatedAt = new Date().toISOString();
    this.store.putSession(session);

    const terminalEvent = loaded.events.at(-1);
    if (!terminalEvent) throw new Error(`Sealed replay ${fixture} has no events`);
    for (const event of loaded.events.slice(0, -1)) {
      this.appendDomainEvent(userId, sessionId, loaded.contract, event);
    }

    const result: RuntimeJobResult = {
      schemaVersion: "chemrxn.runtime-result.v1",
      sessionId,
      jobId,
      contextMode: loaded.contextMode,
      source: {
        mode: "sealed_replay",
        fixture,
        reconstructed: true,
        liveEventCapture: loaded.liveEventCapture,
      },
      domainContract: loaded.contract,
      terminalStatus: loaded.terminalStatus,
      manifest: structuredClone(loaded.manifest),
      createdAt: new Date().toISOString(),
      gtIncluded: false,
      ...(loaded.viewer ? { viewer: loaded.viewer } : {}),
    };
    this.store.putResult(userId, sessionId, result);
    this.appendDomainEvent(userId, sessionId, loaded.contract, terminalEvent);
    session.status = loaded.terminalStatus;
    session.updatedAt = new Date().toISOString();
    this.store.putSession(session);
    return { session: structuredClone(session), result: structuredClone(result) };
  }

  getResult(userId: string, sessionId: string): RuntimeJobResult {
    return this.store.getResult(userId, sessionId);
  }

  async artifact(
    userId: string,
    sessionId: string,
    artifactId: string,
  ): Promise<{ bytes: Buffer; mediaType: string; sha256: string; fileName: string }> {
    const result = this.store.getResult(userId, sessionId);
    if (result.source.mode !== "sealed_replay") {
      throw new RuntimePolicyError("ARTIFACT_NOT_FOUND", "Artifact not found", 404);
    }
    if (result.domainContract === "chemrxn.figure-job.v1") {
      const manifest = result.manifest as {
        artifacts?: Array<{
          id?: string;
          locator?: string;
          mediaType?: string;
          sha256?: string;
        }>;
      };
      const artifact = manifest.artifacts?.find((item) => item.id === artifactId);
      if (
        !artifact
        || !artifact.locator?.startsWith("fixture:")
        || !artifact.mediaType
        || !artifact.sha256
        || result.source.fixture !== "demo-figure"
      ) {
        throw new RuntimePolicyError("ARTIFACT_NOT_FOUND", "Artifact not found", 404);
      }
      const fixtureDirectory = path.join(this.fixturesRoot, "figure", "demo-figure");
      const relativePath = artifact.locator.slice("fixture:".length);
      const target = safeRepositoryPath(fixtureDirectory, relativePath);
      const bytes = await readFile(target).catch(() => {
        throw new RuntimePolicyError("ARTIFACT_NOT_FOUND", "Artifact not found", 404);
      });
      const digest = createHash("sha256").update(bytes).digest("hex");
      if (digest !== artifact.sha256) {
        throw new RuntimePolicyError("ARTIFACT_CORRUPT", "Packaged artifact hash changed", 409);
      }
      return {
        bytes,
        mediaType: artifact.mediaType,
        sha256: artifact.sha256,
        fileName: path.basename(relativePath),
      };
    }
    throw new RuntimePolicyError("ARTIFACT_NOT_FOUND", "Artifact not found", 404);
  }

  async retry(userId: string, sessionId: string): Promise<SealedReplayJob> {
    const previous = this.store.getResult(userId, sessionId);
    if (previous.source.mode !== "sealed_replay") {
      throw new RuntimePolicyError(
        "RETRY_NOT_ALLOWED",
        "Live Pi sessions are retried through conversation, not sealed replay.",
        409,
      );
    }
    if (!["cancelled", "interrupted", "partial", "failed"].includes(previous.terminalStatus)) {
      throw new RuntimePolicyError(
        "RETRY_NOT_ALLOWED",
        `Cannot retry a ${previous.terminalStatus} replay`,
        409,
      );
    }
    const fixture = previous.source.fixture;
    return this.create(userId, fixture, sessionId);
  }

  private appendDomainEvent(
    userId: string,
    sessionId: string,
    contract: RuntimeJobResult["domainContract"],
    event: DomainEvent,
  ): void {
    const commonType = commonEventType(event.type);
    this.store.appendEvent(userId, sessionId, commonType, {
      phase: event.type,
      domainContract: contract,
      domainEventId: event.eventId,
      domainSequence: event.seq,
      domainTimestamp: event.at,
      replay: {
        mode: "sealed_replay",
        reconstructed: true,
      },
      progress: event.progress ? structuredClone(event.progress) : null,
      artifactIds: [...event.artifactIds],
      ...("scope" in event ? { scope: structuredClone(event.scope) } : {}),
      payload: structuredClone(event.data),
    });
  }

  private async load(fixture: SealedReplayFixture): Promise<LoadedReplay> {
    if (fixture === "demo-figure") {
      const loaded = await loadFigureFixture(
        path.join(this.fixturesRoot, "figure", "demo-figure"),
      );
      const input = loaded.manifest.artifacts.find((item) => item.kind === "input_figure");
      const prediction = loaded.manifest.artifacts.find((item) => item.kind === "prediction_v2");
      return {
        contextMode: "figure",
        contract: "chemrxn.figure-job.v1",
        manifest: loaded.manifest,
        events: loaded.events,
        terminalStatus: normalizeTerminal(loaded.manifest.terminalStatus),
        liveEventCapture: false,
        ...(input?.locator.startsWith("fixture:") && prediction?.locator.startsWith("fixture:")
          ? {
              viewer: {
                kind: "figure",
                inputArtifactId: input.id,
                predictionArtifactId: prediction.id,
              },
            }
          : {}),
      };
    }
    throw new RuntimePolicyError("INVALID_CONTEXT", "Unknown figure replay", 400);
  }
}

function commonEventType(type: string): RuntimeEventType {
  return ({
    "job.review_ready": "job.review_ready",
    "job.partial": "job.partial",
    "job.cancelled": "job.cancelled",
    "job.interrupted": "job.interrupted",
    "job.failed": "job.failed",
    "job.blocked": "job.failed",
  } as Record<string, RuntimeEventType>)[type] ?? "agent.event";
}

function normalizeTerminal(status: string): RuntimeJobResult["terminalStatus"] {
  if (["review_ready", "partial", "cancelled", "interrupted", "failed"].includes(status)) {
    return status as RuntimeJobResult["terminalStatus"];
  }
  if (status === "blocked") return "failed";
  throw new Error(`Unsupported terminal status: ${status}`);
}

function defaultFixturesRoot(): string {
  return fileURLToPath(new URL("../../test/fixtures/", import.meta.url));
}

function defaultRepoRoot(): string {
  const candidates = [
    process.env.CHEMRXN_REPO_ROOT,
    fileURLToPath(new URL("../../../", import.meta.url)),
    fileURLToPath(new URL("../../../../", import.meta.url)),
    path.resolve(process.cwd(), "../.."),
    process.cwd(),
  ].filter((item): item is string => Boolean(item)).map((item) => path.resolve(item));
  return candidates.find((candidate) =>
    existsSync(path.join(candidate, "runtime", "pi-service", "test", "fixtures")))
    ?? candidates[0]!;
}

function safeRepositoryPath(rootValue: string, relativePath: string): string {
  const root = path.resolve(rootValue);
  const target = path.resolve(root, relativePath);
  const relative = path.relative(root, target);
  if (!relative || relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new RuntimePolicyError("ARTIFACT_NOT_FOUND", "Artifact not found", 404);
  }
  return target;
}
