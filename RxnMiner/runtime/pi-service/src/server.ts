import { timingSafeEqual } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  accessSync,
  constants as fsConstants,
  mkdirSync,
  statSync,
} from "node:fs";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import { ZodError } from "zod";
import { RuntimePolicyError } from "./errors.js";
import { UnconfiguredSandboxManager } from "./sandbox.js";
import { PiRuntimeService } from "./service.js";
import { interruptOrphanedLocalSessions, LocalPiAgentService } from "./local-agent.js";
import { SealedReplayJobService, SEALED_REPLAY_CATALOG } from "./replay-jobs.js";
import {
  createSessionRequestSchema,
  localAgentCredentialSchema,
  localAgentCreateRequestSchema,
  localAgentMessageSchema,
  localAgentRetrySchema,
  piModelResolutionSchema,
  reviewOutcomeRequestSchema,
  sealedReplayRequestSchema,
} from "./schemas.js";
import { encodeSseEvent, parseEventCursor, terminalSession } from "./sse.js";
import { InMemoryRuntimeStore } from "./store.js";
import { SqliteRuntimeStore } from "./sqlite-store.js";
import type {
  CreateSessionRequest,
  LocalInputKind,
  ReviewOutcomeRequest,
  RuntimeEvent,
} from "./contracts.js";
import { LocalInputStore } from "./local-inputs.js";
import { configureModelNetwork } from "./model-network.js";
import { inspectPiAuth, inspectPiModel } from "./pi-driver.js";
import { toAgUi } from "./ag-ui.js";
import { recordReviewOutcome } from "./review-outcomes.js";
import {
  DurableExecutionQueue,
  queueOwnsCancellation,
  type EnqueuedExecution,
} from "./execution-queue.js";
import { acquireRuntimeDataLock } from "./runtime-lock.js";
import {
  CANONICAL_CHEM_MCP_RUNNER,
  PI_SDK_VERSION,
  verifyPinnedProtocolFiles,
} from "./protocol-fingerprint.js";

const port = Number.parseInt(process.env.CHEMRXN_PI_PORT ?? "4310", 10);
const internalToken = process.env.CHEMRXN_PI_INTERNAL_TOKEN;
const localDataDir = process.env.CHEMRXN_PI_DATA_DIR;
const modelNetwork = configureModelNetwork();
const configuredPiAuthPath = process.env.CHEMRXN_PI_AUTH_PATH;
const defaultPiProfileRoot = configuredPiAuthPath
  ? path.dirname(configuredPiAuthPath)
  : localDataDir
    ? path.resolve(localDataDir, "..", "pi-auth")
    : path.join(tmpdir(), `chemrxn-pi-profile-${process.pid}`);
const piAuthPath = configuredPiAuthPath
  ?? path.join(defaultPiProfileRoot, "auth.json");
const piModelsPath = process.env.CHEMRXN_PI_MODELS_PATH
  ?? path.join(defaultPiProfileRoot, "models.json");
const runtimeDataLock = localDataDir ? acquireRuntimeDataLock(localDataDir) : undefined;
const store = (() => {
  try {
    return localDataDir
      ? new SqliteRuntimeStore(localDataDir)
      : new InMemoryRuntimeStore();
  } catch (error) {
    runtimeDataLock?.release();
    throw error;
  }
})();
process.once("exit", () => runtimeDataLock?.release());
const service = new PiRuntimeService(store, new UnconfiguredSandboxManager());
const replayJobs = new SealedReplayJobService(store);
const inputStore = localDataDir
  ? new LocalInputStore(path.join(localDataDir, "inputs"))
  : undefined;
const localAgent = localAgentService(inputStore);
const extractionReadiness = localExtractionReadiness(localAgent, inputStore);
const executionQueue = localAgent && store instanceof SqliteRuntimeStore
  ? new DurableExecutionQueue(store, store, {
      start: async (userId, sessionId, apiKey, userDirection, signal) => {
        await localAgent.activatePrepared(userId, sessionId, apiKey, signal);
        signal.throwIfAborted();
        await localAgent.startExtraction(userId, sessionId, userDirection, signal);
      },
      cancel: (userId, sessionId) => localAgent.abort(userId, sessionId),
      interrupt: (userId, sessionId) => localAgent.interruptForShutdown(userId, sessionId),
      settle: (_userId, sessionId) => localAgent.waitForTerminalSettlement(sessionId),
    }, {
      concurrency: localConcurrency(),
    })
  : undefined;
const queueRecovery = executionQueue?.start();
const interruptedOnStartup = interruptOrphanedLocalSessions(store, {
  canRehydrate: Boolean(localAgent),
  ...(queueRecovery?.pendingSessionIds
    ? { excludeSessionIds: queueRecovery.pendingSessionIds }
    : {}),
});
let shuttingDown = false;
const shutdownController = new AbortController();

function requireMutationAdmission(): void {
  if (!shuttingDown) return;
  throw new RuntimePolicyError(
    "RUNTIME_SHUTTING_DOWN",
    "The local runtime is shutting down. Retry after it restarts.",
    503,
  );
}

const runtimeServer = createServer(async (request, response) => {
  try {
    const requestUrl = new URL(request.url ?? "/", "http://127.0.0.1");
    if (shuttingDown) {
      return json(response, 503, {
        error: {
          code: "RUNTIME_SHUTTING_DOWN",
          message: "The local runtime is shutting down. Retry after it restarts.",
        },
      });
    }
    if (requestUrl.pathname === "/healthz" && request.method === "GET") {
      return json(response, 200, {
        status: extractionReadiness.ready ? "ok" : "degraded",
        piSdk: PI_SDK_VERSION,
        extractionBackend: extractionReadiness.ready
          ? "figure_live_ready" : "not_connected",
        interactivePiBackend: localAgent ? "local_api_key_or_pi_auth_ready" : "not_connected",
      });
    }
    requireInternalAuth(request);
    if (requestUrl.pathname === "/v1/doctor" && request.method === "GET") {
      return json(response, 200, {
        status: extractionReadiness.ready ? "ok" : "degraded",
        piSdk: PI_SDK_VERSION,
        modelNetwork,
        localStateStore: localDataDir ? "sqlite" : "memory",
        taskIndexSchema: store instanceof SqliteRuntimeStore ? store.schemaVersion : null,
        executionQueue: executionQueue ? {
          enabled: true,
          concurrency: executionQueue.concurrency,
          interruptedOnStartup: queueRecovery?.interrupted ?? 0,
          recoveredPreparedOnStartup: queueRecovery?.recoveredPrepared ?? 0,
        } : { enabled: false },
        extractionBackend: extractionReadiness.ready
          ? "figure_live_ready" : "not_connected",
        interactivePiBackend: localAgent ? "local_api_key_or_pi_auth_ready" : "not_connected",
        blockers: [
          "credential_broker",
          "remote_sandbox",
          ...extractionReadiness.blockers,
        ],
        interruptedOnStartup,
        sessionCounts: Object.fromEntries(
          store.listAllSessions().reduce((counts, session) => {
            counts.set(session.status, (counts.get(session.status) ?? 0) + 1);
            return counts;
          }, new Map<string, number>()),
        ),
      });
    }
    const userId = requiredHeader(request, "x-chemrxn-user-id");
    if (requestUrl.pathname === "/v1/auth/status" && request.method === "GET") {
      return json(response, 200, await inspectPiAuth(piAuthPath));
    }
    if (requestUrl.pathname === "/v1/models/resolve" && request.method === "POST") {
      const requestModel = piModelResolutionSchema.parse(
        await readJson(request, shutdownController.signal),
      );
      const resolution = await inspectPiModel(
        requestModel.provider,
        requestModel.modelId,
        piModelsPath,
      );
      if (!resolution.available) {
        throw new RuntimePolicyError(
          "MODEL_NOT_AVAILABLE",
          `Model ${requestModel.provider}/${requestModel.modelId} is not available in the locked Pi SDK`,
          409,
        );
      }
      if (requestModel.requireImage && !resolution.supportsImages) {
        throw new RuntimePolicyError(
          "MODEL_HAS_NO_VISION",
          `Model ${requestModel.provider}/${requestModel.modelId} cannot inspect reaction figures`,
          409,
        );
      }
      return json(response, 200, resolution);
    }
    if (requestUrl.pathname === "/v1/input-artifacts" && request.method === "POST") {
      if (!inputStore) {
        throw new RuntimePolicyError(
          "INPUT_BACKEND_NOT_CONNECTED",
          "Local input storage requires CHEMRXN_PI_DATA_DIR",
          503,
        );
      }
      const kind = requiredInputKind(request);
      const originalName = decodedHeader(request, "x-chemrxn-file-name", "upload");
      const mediaType = typeof request.headers["content-type"] === "string"
        ? request.headers["content-type"].split(";")[0]!.trim()
        : "application/octet-stream";
      const artifact = await inputStore.stage(userId, request, {
        kind,
        originalName,
        mediaType,
        signal: shutdownController.signal,
      });
      return json(response, 201, { artifact });
    }
    const inputArtifactMatch = requestUrl.pathname.match(/^\/v1\/input-artifacts\/([^/]+)$/);
    if (inputArtifactMatch && request.method === "GET") {
      if (!inputStore) {
        throw new RuntimePolicyError(
          "INPUT_BACKEND_NOT_CONNECTED",
          "Local input storage requires CHEMRXN_PI_DATA_DIR",
          503,
        );
      }
      const artifactId = decodeURIComponent(inputArtifactMatch[1] ?? "");
      const loaded = await inputStore.read(userId, artifactId);
      return binary(response, {
        bytes: loaded.bytes,
        mediaType: loaded.artifact.mediaType,
        sha256: loaded.artifact.sha256,
        fileName: loaded.artifact.originalName,
      });
    }
    if (requestUrl.pathname === "/v1/sessions" && request.method === "POST") {
      const body = createSessionRequestSchema.parse(
        await readJson(request, shutdownController.signal),
      ) as CreateSessionRequest;
      if (body.userId !== userId) {
        throw new RuntimePolicyError("CROSS_TENANT_ACCESS", "Authenticated tenant mismatch", 403);
      }
      requireMutationAdmission();
      return json(response, 201, await service.create(body));
    }
    if (requestUrl.pathname === "/v1/sessions" && request.method === "GET") {
      const sessions = service.list(userId);
      return json(response, 200, {
        sessions,
        ...(executionQueue
          ? {
              dispatches: sessions.flatMap((session) => {
                const dispatch = optionalDispatch(userId, session.id);
                return dispatch ? [dispatch] : [];
              }),
            }
          : {}),
      });
    }
    if (requestUrl.pathname === "/v1/replay-jobs/fixtures" && request.method === "GET") {
      return json(response, 200, { fixtures: SEALED_REPLAY_CATALOG });
    }
    if (requestUrl.pathname === "/v1/replay-jobs" && request.method === "POST") {
      const body = sealedReplayRequestSchema.parse(
        await readJson(request, shutdownController.signal),
      );
      if (body.userId !== userId) {
        throw new RuntimePolicyError("CROSS_TENANT_ACCESS", "Authenticated tenant mismatch", 403);
      }
      requireMutationAdmission();
      const created = await replayJobs.create(userId, body.fixture);
      return json(response, 201, replayResponse(created.session));
    }
    if (requestUrl.pathname === "/v1/local-agent-sessions" && request.method === "POST") {
      if (!localAgent) {
        throw new RuntimePolicyError(
          "REMOTE_SANDBOX_NOT_CONFIGURED",
          "Local Pi Agent is not enabled in this runtime",
          503,
        );
      }
      const body = localAgentCreateRequestSchema.parse(
        await readJson(request, shutdownController.signal),
      );
      if (body.userId !== userId) {
        throw new RuntimePolicyError("CROSS_TENANT_ACCESS", "Authenticated tenant mismatch", 403);
      }
      requireMutationAdmission();
      if (body.startExtraction && executionQueue) {
        const session = await localAgent.prepare({
          ...body,
          dispatchRequested: true,
          ...(body.initialMessage ? { initialMessage: body.initialMessage } : {}),
        });
        let dispatch: EnqueuedExecution;
        try {
          requireMutationAdmission();
          dispatch = executionQueue.enqueue({
            userId,
            sessionId: session.id,
            ...(body.apiKey ? { apiKey: body.apiKey } : {}),
            ...(body.initialMessage ? { userDirection: body.initialMessage } : {}),
          });
        } catch (error) {
          if (shuttingDown) {
            localAgent.interruptPreparedForShutdown(userId, session.id);
          }
          throw error;
        }
        return json(response, 201, agentResponse(session, undefined, dispatch));
      }
      const prepared = await localAgent.prepare(body);
      try {
        requireMutationAdmission();
      } catch (error) {
        if (shuttingDown) {
          localAgent.interruptPreparedForShutdown(userId, prepared.id);
        }
        throw error;
      }
      let session = await localAgent.activatePrepared(
        userId,
        prepared.id,
        body.apiKey,
        shutdownController.signal,
      );
      if (body.startExtraction) {
        requireMutationAdmission();
        await localAgent.startExtraction(
          userId,
          session.id,
          body.initialMessage,
          shutdownController.signal,
        );
        session = store.getSession(userId, session.id);
      }
      return json(response, 201, agentResponse(session));
    }
    const messageMatch = requestUrl.pathname.match(/^\/v1\/sessions\/([^/]+)\/messages$/);
    if (messageMatch && request.method === "POST") {
      if (!localAgent) {
        throw new RuntimePolicyError("SESSION_NOT_ACTIVE", "Local Pi Agent is unavailable", 409);
      }
      const sessionId = decodeURIComponent(messageMatch[1] ?? "");
      const body = localAgentMessageSchema.parse(
        await readJson(request, shutdownController.signal),
      );
      requireMutationAdmission();
      const current = store.getSession(userId, sessionId);
      if (["review_ready", "partial"].includes(current.status)) {
        if (body.behavior !== "prompt") {
          throw new RuntimePolicyError(
            "SESSION_NOT_ACTIVE",
            "A sealed turn can only start a new prompt; steer and follow-up require an active turn.",
            409,
          );
        }
        const continued = await localAgent.continueFrom(
          userId,
          sessionId,
          body.text,
          shutdownController.signal,
        );
        return json(response, 201, agentResponse(continued, {
          parentSessionId: sessionId,
          ...(continued.parentResultSha256
            ? { parentResultSha256: continued.parentResultSha256 } : {}),
        }));
      }
      await localAgent.prompt(
        userId,
        sessionId,
        body.text,
        body.behavior,
        body.apiKey,
        shutdownController.signal,
      );
      return json(response, 202, { accepted: true, sessionId, behavior: body.behavior });
    }
    const dispatchMatch = requestUrl.pathname.match(/^\/v1\/sessions\/([^/]+)\/dispatch$/);
    if (dispatchMatch && request.method === "GET") {
      if (!executionQueue) {
        throw new RuntimePolicyError(
          "WORK_ITEM_NOT_FOUND",
          "Durable local extraction dispatch is not enabled",
          404,
        );
      }
      const sessionId = decodeURIComponent(dispatchMatch[1] ?? "");
      return json(response, 200, executionQueue.status(userId, sessionId));
    }
    const credentialsMatch = requestUrl.pathname.match(
      /^\/v1\/sessions\/([^/]+)\/credentials$/,
    );
    if (credentialsMatch && request.method === "POST") {
      if (!executionQueue) {
        throw new RuntimePolicyError(
          "WORK_ITEM_NOT_FOUND",
          "Durable local extraction dispatch is not enabled",
          404,
        );
      }
      const sessionId = decodeURIComponent(credentialsMatch[1] ?? "");
      const body = localAgentCredentialSchema.parse(
        await readJson(request, shutdownController.signal),
      );
      requireMutationAdmission();
      return json(
        response,
        202,
        executionQueue.supplyApiKey(userId, sessionId, body.apiKey),
      );
    }
    const abortMatch = requestUrl.pathname.match(/^\/v1\/sessions\/([^/]+)\/abort$/);
    if (abortMatch && request.method === "POST") {
      requireMutationAdmission();
      if (!localAgent) {
        throw new RuntimePolicyError("SESSION_NOT_ACTIVE", "Local Pi Agent is unavailable", 409);
      }
      const sessionId = decodeURIComponent(abortMatch[1] ?? "");
      if (executionQueue) {
        try {
          const dispatch = executionQueue.status(userId, sessionId);
          if (queueOwnsCancellation(dispatch.workItem.state)) {
            const workItem = await executionQueue.cancel(userId, sessionId);
            return json(response, 202, {
              accepted: true,
              sessionId,
              status: workItem.state === "cancelled" ? "cancelled" : "cancelling",
              dispatch: {
                workItem,
                position: executionQueue.status(userId, sessionId).position,
                credentialRequired: false,
              },
            });
          }
          // A completed no-artifact dispatch can own a later interactive Pi
          // turn. Its historical work item is terminal, so abort the live
          // agent below instead of trying to rewrite queue history.
        } catch (error) {
          if (!(error instanceof RuntimePolicyError) || error.code !== "WORK_ITEM_NOT_FOUND") {
            throw error;
          }
        }
      }
      await localAgent.abort(userId, sessionId);
      return json(response, 202, { accepted: true, sessionId, status: "cancelled" });
    }
    const reviewOutcomeMatch = requestUrl.pathname.match(
      /^\/v1\/sessions\/([^/]+)\/review-outcomes$/,
    );
    if (reviewOutcomeMatch && request.method === "POST") {
      const sessionId = decodeURIComponent(reviewOutcomeMatch[1] ?? "");
      const body = reviewOutcomeRequestSchema.parse(
        await readJson(request, shutdownController.signal),
      ) as ReviewOutcomeRequest;
      requireMutationAdmission();
      const recorded = recordReviewOutcome(store, userId, sessionId, body);
      return json(response, recorded.idempotent ? 200 : 201, recorded);
    }
    const resultMatch = requestUrl.pathname.match(/^\/v1\/sessions\/([^/]+)\/result$/);
    if (resultMatch && request.method === "GET") {
      const sessionId = decodeURIComponent(resultMatch[1] ?? "");
      return json(response, 200, replayJobs.getResult(userId, sessionId));
    }
    const liveArtifactMatch = requestUrl.pathname.match(
      /^\/v1\/sessions\/([^/]+)\/live-artifacts\/([^/]+)$/,
    );
    if (liveArtifactMatch && request.method === "GET") {
      if (!localAgent) {
        throw new RuntimePolicyError("ARTIFACT_NOT_FOUND", "Live artifact not found", 404);
      }
      const sessionId = decodeURIComponent(liveArtifactMatch[1] ?? "");
      const kind = decodeURIComponent(liveArtifactMatch[2] ?? "");
      return binary(response, await localAgent.liveArtifact(userId, sessionId, kind));
    }
    const artifactMatch = requestUrl.pathname.match(
      /^\/v1\/sessions\/([^/]+)\/artifacts\/([^/]+)$/,
    );
    if (artifactMatch && request.method === "GET") {
      const sessionId = decodeURIComponent(artifactMatch[1] ?? "");
      const artifactId = decodeURIComponent(artifactMatch[2] ?? "");
      const result = replayJobs.getResult(userId, sessionId);
      const artifact = result.source.mode === "sealed_replay"
        ? await replayJobs.artifact(userId, sessionId, artifactId)
        : localAgent
          ? await localAgent.artifact(userId, sessionId, artifactId)
          : (() => {
              throw new RuntimePolicyError("ARTIFACT_NOT_FOUND", "Artifact not found", 404);
            })();
      return binary(response, artifact);
    }
    const retryMatch = requestUrl.pathname.match(/^\/v1\/sessions\/([^/]+)\/retry$/);
    if (retryMatch && request.method === "POST") {
      requireMutationAdmission();
      const sessionId = decodeURIComponent(retryMatch[1] ?? "");
      const previous = store.getSession(userId, sessionId);
      if (previous.authMode !== "sealed_replay") {
        if (!localAgent) {
          throw new RuntimePolicyError(
            "SESSION_NOT_ACTIVE",
            "Local Pi Agent is unavailable",
            409,
          );
        }
        const body = localAgentRetrySchema.parse(
          await readJson(request, shutdownController.signal),
        );
        requireMutationAdmission();
        if (executionQueue) {
          const prepared = await localAgent.prepareRetry(userId, sessionId, body.apiKey);
          let dispatch: EnqueuedExecution;
          try {
            requireMutationAdmission();
            dispatch = executionQueue.enqueue({
              userId,
              sessionId: prepared.session.id,
              ...(prepared.apiKey ? { apiKey: prepared.apiKey } : {}),
            });
          } catch (error) {
            if (shuttingDown) {
              localAgent.interruptPreparedForShutdown(userId, prepared.session.id);
            }
            throw error;
          }
          return json(response, 201, agentResponse(prepared.session, {
            parentSessionId: sessionId,
            relation: "retry",
          }, dispatch));
        }
        const retried = await localAgent.retry(userId, sessionId, body.apiKey);
        return json(response, 201, agentResponse(retried, {
          parentSessionId: sessionId,
          relation: "retry",
        }));
      }
      const created = await replayJobs.retry(userId, sessionId);
      return json(response, 201, replayResponse(created.session));
    }
    const match = requestUrl.pathname.match(/^\/v1\/sessions\/([^/]+)(?:\/(events|stream))?$/);
    if (match && request.method === "GET") {
      const sessionId = decodeURIComponent(match[1] ?? "");
      if (match[2] === "events") {
        const after = parseEventCursor(requestUrl.searchParams.get("after"), undefined);
        const events = service.events(userId, sessionId, after);
        if (requestUrl.searchParams.get("projection") === "ag-ui") {
          return json(response, 200, {
            events: events.flatMap(toAgUi),
            runtimeCursor: events.at(-1)?.seq ?? after,
            source: "chemrxn.persisted-runtime-events",
          });
        }
        return json(response, 200, {
          events,
        });
      }
      if (match[2] === "stream") {
        const after = parseEventCursor(
          requestUrl.searchParams.get("after"),
          typeof request.headers["last-event-id"] === "string"
            ? request.headers["last-event-id"] : undefined,
        );
        return streamSessionEvents(request, response, userId, sessionId, after);
      }
      return json(response, 200, service.get(userId, sessionId));
    }
    return json(response, 404, { error: { code: "NOT_FOUND", message: "Route not found" } });
  } catch (error) {
    const status = error instanceof RuntimePolicyError ? error.status : error instanceof ZodError ? 400 : 500;
    return json(response, status, {
      error: {
        code: error instanceof RuntimePolicyError ? error.code
          : error instanceof ZodError ? "INVALID_REQUEST" : "INTERNAL_ERROR",
        message: error instanceof Error ? error.message : "Unknown error",
      },
    });
  }
});

const httpServer = runtimeServer.listen(port, "127.0.0.1", () => {
  console.log(`ChemRxn Pi runtime listening on http://127.0.0.1:${port}`);
});

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.once(signal, async () => {
    if (shuttingDown) return;
    shuttingDown = true;
    shutdownController.abort("runtime_shutdown");
    const closed = new Promise<void>((resolve) => httpServer.close(() => resolve()));
    const watchdog = setTimeout(() => process.exit(1), 5_000);
    watchdog.unref();
    await executionQueue?.stop();
    await localAgent?.disposeAll();
    await closed;
    clearTimeout(watchdog);
    if (store instanceof SqliteRuntimeStore) store.close();
    runtimeDataLock?.release();
    process.exit(0);
  });
}

function streamSessionEvents(
  request: IncomingMessage,
  response: ServerResponse,
  userId: string,
  sessionId: string,
  initialCursor: number,
): void {
  let cursor = initialCursor;
  const session = service.get(userId, sessionId);
  const pending: RuntimeEvent[] = [];
  let streaming = false;
  let closed = false;
  let timer: ReturnType<typeof setInterval> | undefined;
  const unsubscribe = service.subscribeEvents(sessionId, (event) => {
    if (closed || event.seq <= cursor) return;
    if (!streaming) {
      pending.push(event);
      return;
    }
    writeEvent(event);
    endIfTerminal();
  });
  let initial: RuntimeEvent[];
  try {
    initial = service.events(userId, sessionId, cursor);
  } catch (error) {
    unsubscribe();
    throw error;
  }
  if (terminalSession(session.status) && initial.length === 0) {
    unsubscribe();
    response.writeHead(204, { "cache-control": "no-store" });
    response.end();
    return;
  }
  response.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-cache, no-store",
    connection: "keep-alive",
    "x-accel-buffering": "no",
  });
  response.write("retry: 1000\n\n");
  for (const event of initial) {
    writeEvent(event);
  }
  streaming = true;
  for (const event of pending.sort((left, right) => left.seq - right.seq)) {
    if (event.seq > cursor) writeEvent(event);
  }
  if (terminalSession(session.status)) {
    cleanup();
    response.end();
    return;
  }

  let heartbeat = 0;
  timer = setInterval(() => {
    try {
      heartbeat += 1;
      if (heartbeat >= 30) {
        response.write(": keep-alive\n\n");
        heartbeat = 0;
      }
      if (terminalSession(service.get(userId, sessionId).status)) {
        cleanup();
        response.end();
      }
    } catch {
      cleanup();
      response.end();
    }
  }, 500);
  timer.unref();
  response.once("close", cleanup);

  function writeEvent(event: RuntimeEvent): void {
    if (closed || event.seq <= cursor) return;
    response.write(encodeSseEvent(event));
    cursor = event.seq;
  }

  function endIfTerminal(): void {
    if (terminalSession(service.get(userId, sessionId).status)) {
      cleanup();
      response.end();
    }
  }

  function cleanup(): void {
    if (closed) return;
    closed = true;
    unsubscribe();
    if (timer) clearInterval(timer);
  }
}

function requireInternalAuth(request: IncomingMessage): void {
  if (!internalToken) throw new Error("CHEMRXN_PI_INTERNAL_TOKEN is required");
  const expected = Buffer.from(`Bearer ${internalToken}`);
  const actual = Buffer.from(request.headers.authorization ?? "");
  if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) {
    throw new RuntimePolicyError("CROSS_TENANT_ACCESS", "Invalid internal service credential", 401);
  }
}

function requiredHeader(request: IncomingMessage, name: string): string {
  const value = request.headers[name];
  if (typeof value !== "string" || value.length === 0) {
    throw new RuntimePolicyError("CROSS_TENANT_ACCESS", `Missing ${name}`, 401);
  }
  return value;
}

async function readJson(
  request: IncomingMessage,
  signal?: AbortSignal,
): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  const iterator = request[Symbol.asyncIterator]() as AsyncIterator<Uint8Array>;
  while (true) {
    const next = await nextRequestChunk(iterator, signal);
    if (next.done) break;
    const chunk = next.value;
    const buffer = Buffer.from(chunk);
    size += buffer.length;
    if (size > 1_000_000) throw new Error("Request body exceeds 1 MB");
    chunks.push(buffer);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function nextRequestChunk(
  iterator: AsyncIterator<Uint8Array>,
  signal?: AbortSignal,
): Promise<IteratorResult<Uint8Array>> {
  if (!signal) return iterator.next();
  requireMutationAdmission();
  return new Promise((resolve, reject) => {
    let settled = false;
    const onAbort = (): void => {
      if (settled) return;
      settled = true;
      reject(new RuntimePolicyError(
        "RUNTIME_SHUTTING_DOWN",
        "The local runtime is shutting down. Retry after it restarts.",
        503,
      ));
    };
    signal.addEventListener("abort", onAbort, { once: true });
    void iterator.next().then((value) => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", onAbort);
      resolve(value);
    }, (error) => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", onAbort);
      reject(error);
    });
  });
}

function json(response: ServerResponse, status: number, value: unknown): void {
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  });
  response.end(JSON.stringify(value));
}

function binary(
  response: ServerResponse,
  artifact: { bytes: Buffer; mediaType: string; sha256: string; fileName: string },
): void {
  response.writeHead(200, {
    "content-type": artifact.mediaType,
    "content-length": String(artifact.bytes.length),
    "cache-control": "private, no-store",
    "content-disposition": `inline; filename="${artifact.fileName.replace(/["\\]/g, "_")}"`,
    "x-content-type-options": "nosniff",
    "x-chemrxn-sha256": artifact.sha256,
  });
  response.end(artifact.bytes);
}

function replayResponse(session: { id: string; jobId: string; status: string }): unknown {
  return {
    session,
    replay: {
      reconstructed: true,
      liveExtraction: false,
    },
    links: {
      status: `/v1/sessions/${encodeURIComponent(session.id)}`,
      events: `/v1/sessions/${encodeURIComponent(session.id)}/events`,
      stream: `/v1/sessions/${encodeURIComponent(session.id)}/stream`,
      result: `/v1/sessions/${encodeURIComponent(session.id)}/result`,
      retry: `/v1/sessions/${encodeURIComponent(session.id)}/retry`,
    },
  };
}

function agentResponse(
  session: { id: string; jobId: string; status: string; authMode?: string },
  continuation?: {
    parentSessionId: string;
    parentResultSha256?: string;
    relation?: "continuation" | "retry";
  },
  dispatch?: EnqueuedExecution,
): unknown {
  return {
    accepted: true,
    session,
    ...(continuation
      ? continuation.relation === "retry"
        ? {
            retried: true,
            relation: "retry",
            previousSessionId: continuation.parentSessionId,
          }
        : {
            continued: true,
            relation: "continuation",
            parentSessionId: continuation.parentSessionId,
            parentResultSha256: continuation.parentResultSha256,
          }
      : {}),
    ...(dispatch ? { dispatch } : {}),
    backend: {
      mode: "local_pi_sdk",
      auth: session.authMode === "personal_codex_subscription"
        ? "personal_codex_subscription_via_pi_auth" : "byo_api_key_in_memory",
      hostedIsolation: false,
    },
    links: {
      status: `/v1/sessions/${encodeURIComponent(session.id)}`,
      events: `/v1/sessions/${encodeURIComponent(session.id)}/events`,
      stream: `/v1/sessions/${encodeURIComponent(session.id)}/stream`,
      messages: `/v1/sessions/${encodeURIComponent(session.id)}/messages`,
      abort: `/v1/sessions/${encodeURIComponent(session.id)}/abort`,
      retry: `/v1/sessions/${encodeURIComponent(session.id)}/retry`,
      ...(dispatch
        ? {
            dispatch: `/v1/sessions/${encodeURIComponent(session.id)}/dispatch`,
            credentials: `/v1/sessions/${encodeURIComponent(session.id)}/credentials`,
          }
        : {}),
    },
  };
}

function optionalDispatch(userId: string, sessionId: string): EnqueuedExecution | undefined {
  if (!executionQueue) return undefined;
  try {
    return executionQueue.status(userId, sessionId);
  } catch (error) {
    if (error instanceof RuntimePolicyError && error.code === "WORK_ITEM_NOT_FOUND") {
      return undefined;
    }
    throw error;
  }
}

function localConcurrency(): number {
  const raw = process.env.CHEMRXN_LOCAL_MAX_CONCURRENCY;
  if (raw == null || raw.trim() === "") return 1;
  if (!/^\d+$/.test(raw)) {
    throw new Error("CHEMRXN_LOCAL_MAX_CONCURRENCY must be an integer from 1 to 16");
  }
  const value = Number.parseInt(raw, 10);
  if (!Number.isSafeInteger(value) || value < 1 || value > 16) {
    throw new Error("CHEMRXN_LOCAL_MAX_CONCURRENCY must be an integer from 1 to 16");
  }
  return value;
}

function localAgentService(inputStoreValue?: LocalInputStore): LocalPiAgentService | undefined {
  if (process.env.CHEMRXN_LOCAL_AGENT_ENABLED !== "1") return undefined;
  const repoRoot = process.env.CHEMRXN_REPO_ROOT;
  const workspaceRoot = process.env.CHEMRXN_LOCAL_WORKSPACE_ROOT;
  const agentRoot = process.env.CHEMRXN_PI_AGENT_ROOT;
  const python = process.env.CHEMRXN_CHEM_PYTHON;
  if (!repoRoot || !workspaceRoot || !agentRoot || !python) return undefined;
  return new LocalPiAgentService(store, {
    repoRoot,
    workspaceRoot,
    agentRoot,
    authPath: piAuthPath,
    modelsPath: piModelsPath,
    python,
    inputStore: inputStoreValue,
    perceptionCacheRoot: process.env.CHEMRXN_LOCAL_PERCEPTION_CACHE_ROOT
      ?? path.resolve(workspaceRoot, "..", "perception-cache"),
  });
}

function localExtractionReadiness(
  agent: LocalPiAgentService | undefined,
  inputStoreValue: LocalInputStore | undefined,
): { ready: boolean; blockers: string[] } {
  const blockers: string[] = [];
  if (!agent) blockers.push("local_agent_configuration");
  if (!inputStoreValue) blockers.push("local_input_store");
  if (!agent || !inputStoreValue) return { ready: false, blockers };

  const repoRoot = process.env.CHEMRXN_REPO_ROOT!;
  const workspaceRoot = process.env.CHEMRXN_LOCAL_WORKSPACE_ROOT!;
  const agentRoot = process.env.CHEMRXN_PI_AGENT_ROOT!;
  const python = process.env.CHEMRXN_CHEM_PYTHON!;
  for (const [name, target] of [
    ["workspace_root", workspaceRoot],
    ["pi_agent_root", agentRoot],
  ] as const) {
    try {
      mkdirSync(target, { recursive: true, mode: 0o700 });
      if (!statSync(target).isDirectory()) throw new Error("not a directory");
      accessSync(target, fsConstants.R_OK | fsConstants.W_OK | fsConstants.X_OK);
    } catch {
      blockers.push(`${name}_unavailable`);
    }
  }
  try {
    if (!statSync(repoRoot).isDirectory()) throw new Error("not a directory");
    accessSync(repoRoot, fsConstants.R_OK | fsConstants.X_OK);
  } catch {
    blockers.push("repo_root_unavailable");
  }
  const requiredFiles = [
    ["figure_skill", path.join(repoRoot, "figure_skills", "skills", "reaction-extract", "SKILL.md")],
    ["chemistry_mcp", path.join(repoRoot, CANONICAL_CHEM_MCP_RUNNER)],
  ] as const;
  for (const [name, target] of requiredFiles) {
    try {
      if (!statSync(target).isFile()) throw new Error("not a file");
      accessSync(target, fsConstants.R_OK);
    } catch {
      blockers.push(`${name}_unavailable`);
    }
  }
  try {
    verifyPinnedProtocolFiles(repoRoot);
  } catch {
    blockers.push("protocol_fingerprint_pin_mismatch");
  }
  const pythonCheck = spawnSync(
    python,
    [
      "-c",
      "import sys, fitz, requests; from PIL import Image; from rdkit import Chem; "
        + "raise SystemExit(0 if sys.version_info >= (3, 9) else 1)",
    ],
    // A first import from a fresh macOS product venv can take several seconds
    // while native RDKit/PyMuPDF modules are loaded. This is a startup gate,
    // not a request-path probe, so allow a bounded cold start.
    { stdio: "ignore", timeout: 180_000 },
  );
  if (pythonCheck.error || pythonCheck.status !== 0) {
    blockers.push("chemistry_python_unavailable");
  }
  return { ready: blockers.length === 0, blockers };
}

function requiredInputKind(request: IncomingMessage): LocalInputKind {
  const value = request.headers["x-chemrxn-input-kind"];
  if (value === "figure") return value;
  throw new RuntimePolicyError(
    "INVALID_INPUT",
    "x-chemrxn-input-kind must be figure",
    400,
  );
}

function decodedHeader(request: IncomingMessage, name: string, fallback: string): string {
  const raw = request.headers[name];
  if (typeof raw !== "string" || !raw) return fallback;
  try {
    return decodeURIComponent(raw);
  } catch {
    throw new RuntimePolicyError("INVALID_INPUT", `${name} is not valid URI encoding`, 400);
  }
}
