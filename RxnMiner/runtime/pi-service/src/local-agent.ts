import { createHash } from "node:crypto";
import { chmod, copyFile, mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { resizeImage } from "@earendil-works/pi-coding-agent";
import {
  ChemMcpClient,
  chemistryToolDefinitions,
} from "./chem-mcp-client.js";
import type {
  ContextMode,
  PiThinkingLevel,
  LocalInputSelection,
  RuntimeEvent,
  RuntimeJobResult,
  SessionRecord,
  TaskKind,
} from "./contracts.js";
import { DEFAULT_EXTRACTION_THINKING_LEVEL } from "./contracts.js";
import { RuntimePolicyError } from "./errors.js";
import { createPiSession, type PiSessionHandle } from "./pi-driver.js";
import {
  loadPinnedSkillManifest,
  PINNED_TOOL_ALLOWLIST,
  type SkillManifestEntry,
} from "./skills.js";
import type { RuntimeStore } from "./store.js";
import type { ToolDefinition } from "@earendil-works/pi-coding-agent";
import { assertNoGroundTruth } from "./security.js";
import {
  LocalInputStore,
  type MaterializedLocalInput,
} from "./local-inputs.js";
import {
  FIGURE_WORKSPACE_UI_MODULES,
  WORKSPACE_UI_TOOL_NAMES,
  WORKSPACE_UI_MODULES,
  workspaceUiToolDefinitions,
} from "./workspace-ui-tools.js";
import {
  artifactFromLiveResult,
  buildLiveFigureResult,
  FigureSealBlockedError,
  readWorkspaceArtifact,
  type LiveToolCapture,
} from "./live-results.js";
import {
  buildProtocolFingerprint,
  CANONICAL_CHEM_MCP_RUNNER,
  canonicalToolContract,
  ProtocolIntegrityVerifier,
  type ProtocolArtifactIdentity,
  type ProtocolFileIdentity,
  type ProtocolToolDescriptor,
} from "./protocol-fingerprint.js";

export interface LocalAgentCreateRequest {
  userId: string;
  jobId: string;
  contextMode: ContextMode;
  task?: TaskKind | undefined;
  workspaceId: string;
  provider: string;
  modelId: string;
  thinkingLevel?: PiThinkingLevel | undefined;
  authMode?: "api_key" | "pi_auth" | undefined;
  apiKey?: string | undefined;
  inputs?: LocalInputSelection[] | undefined;
  parentSessionId?: string | undefined;
  parentResultSha256?: string | undefined;
  relation?: "continuation" | "retry" | undefined;
  dispatchRequested?: boolean | undefined;
  initialMessage?: string | undefined;
}

export const LOCAL_EXTRACTION_REQUEST_VERSION = "chemrxn.local-extraction-request.v1" as const;

interface ActiveAgent {
  pi: PiSessionHandle;
  mcp: ChemMcpClient;
  apiKey: string;
  workspace: string;
  workspaceId: string;
  contextMode: ContextMode;
  inputs: MaterializedLocalInput[];
  provider: string;
  modelId: string;
  thinkingLevel: PiThinkingLevel;
  skills: SkillManifestEntry[];
  protocolToolContract: ProtocolToolDescriptor[];
  protocolPrompt?: string;
  protocolPerception: ProtocolArtifactIdentity[];
  toolCaptures: LiveToolCapture[];
  streamDeltas: string[];
  streamDeltaChars: number;
  toolArgumentDeltaChunks: number;
  toolArgumentDeltaChars: number;
  finalizing: boolean;
  cancelling: boolean;
  lastPredictionSha?: string;
  lastPiError?: string;
}

interface LocalAgentDependencies {
  randomId(): string;
  loadSkills(repoRoot: string): Promise<SkillManifestEntry[]>;
  startMcp(options: {
    python: string;
    script: string;
    cwd: string;
    onFatal?: (error: Error) => void;
    signal?: AbortSignal;
  }): Promise<ChemMcpClient>;
  createTools(
    client: ChemMcpClient,
    allowlist: readonly string[],
    workspace?: string,
  ): Promise<ToolDefinition[]>;
  createPi(input: Parameters<typeof createPiSession>[0]): Promise<PiSessionHandle>;
}

export class LocalPiAgentService {
  private readonly active = new Map<string, ActiveAgent>();
  private readonly settlements = new Map<string, Promise<void>>();
  private readonly rehydrating = new Map<string, {
    controller: AbortController;
    promise: Promise<ActiveAgent>;
  }>();
  private readonly sealedApiKeyLeases = new Map<
    string,
    {
      value: string;
      expiresAt: number;
      timer?: ReturnType<typeof setTimeout>;
    }
  >();
  private closing = false;
  private readonly dependencies: LocalAgentDependencies;
  private readonly protocolIntegrity: ProtocolIntegrityVerifier;

  constructor(
    private readonly store: RuntimeStore,
    private readonly options: {
      repoRoot: string;
      workspaceRoot: string;
      agentRoot: string;
      python: string;
      authPath?: string | undefined;
      modelsPath?: string | undefined;
      inputStore?: LocalInputStore | undefined;
      perceptionCacheRoot?: string | undefined;
      credentialLeaseTtlMs?: number | undefined;
      abortTimeoutMs?: number | undefined;
      cleanupTimeoutMs?: number | undefined;
    },
    dependencies: Partial<LocalAgentDependencies> = {},
  ) {
    this.dependencies = {
      randomId: () => randomUUID(),
      loadSkills: loadPinnedSkillManifest,
      startMcp: ChemMcpClient.start,
      createTools: chemistryToolDefinitions,
      createPi: createPiSession,
      ...dependencies,
    };
    this.protocolIntegrity = new ProtocolIntegrityVerifier(this.options.repoRoot);
  }

  /**
   * Persist the cheap, restart-safe half of a local extraction request.
   *
   * Preparing a session does not create a workspace, start MCP/Pi, materialize
   * an input, or consume model quota.  The durable execution queue may hold
   * this record until a bounded worker slot becomes available.
   */
  async prepare(request: LocalAgentCreateRequest): Promise<SessionRecord> {
    if (request.contextMode !== "figure") throw new RuntimePolicyError("INVALID_CONTEXT", "Only reaction images are supported", 400);
    this.assertAccepting();
    this.requireProtocolIntegrity();
    if (request.initialMessage) assertNoGroundTruth({ text: request.initialMessage });
    const authMode = request.authMode ?? "api_key";
    const thinkingLevel = request.thinkingLevel ?? DEFAULT_EXTRACTION_THINKING_LEVEL;
    const sessionId = this.dependencies.randomId();
    const now = new Date().toISOString();
    const session: SessionRecord = {
      id: sessionId,
      userId: request.userId,
      jobId: request.jobId,
      task: request.task ?? "extract_all",
      ...(request.parentSessionId
        ? { parentSessionId: request.parentSessionId } : {}),
      ...(request.parentResultSha256
        ? { parentResultSha256: request.parentResultSha256 } : {}),
      status: "creating",
      contextMode: request.contextMode,
      authMode: authMode === "pi_auth" && request.provider === "openai-codex"
        ? "personal_codex_subscription" : "api_key",
      thinkingLevel,
      sandboxId: sessionId,
      createdAt: now,
      updatedAt: now,
    };
    this.store.putSession(session);
    this.store.appendEvent(request.userId, sessionId, "session.created", {
      jobId: request.jobId,
      contextMode: request.contextMode,
      source: { mode: "live_pi_sdk", localSingleUser: true },
      provider: request.provider,
      modelId: request.modelId,
      thinkingLevel,
      authMode,
      requestedWorkspaceId: request.workspaceId,
      inputArtifactIds: (request.inputs ?? []).map((input) => input.artifactId),
      inputSelections: (request.inputs ?? []).map((input) => ({
        artifactId: input.artifactId,
        kind: input.kind,
      })),
      localExtractionRequest: {
        schemaVersion: LOCAL_EXTRACTION_REQUEST_VERSION,
        dispatchRequested: request.dispatchRequested === true,
        contextMode: request.contextMode,
        task: request.task ?? "extract_all",
        provider: request.provider,
        modelId: request.modelId,
        thinkingLevel,
        authMode: request.authMode ?? "api_key",
        requestedWorkspaceId: request.workspaceId,
        inputs: (request.inputs ?? []).map((input) => ({
          artifactId: input.artifactId,
          kind: input.kind,
        })),
        ...(request.initialMessage ? { initialMessage: request.initialMessage } : {}),
        ...(request.relation ? { relation: request.relation } : {}),
        ...(request.parentSessionId ? { parentSessionId: request.parentSessionId } : {}),
      },
      task: request.task ?? "extract_all",
      workspaceTargetSessionId: request.relation === "continuation" && request.parentSessionId
        ? this.continuationRootSessionId(request.userId, request.parentSessionId)
        : sessionId,
      ...(request.parentSessionId
        ? {
            [request.relation === "retry" ? "retry" : "continuation"]: {
              parentSessionId: request.parentSessionId,
              parentResultSha256: request.parentResultSha256,
              sealedParent: true,
            },
          }
        : {}),
    }, request.apiKey ? [request.apiKey] : []);

    return structuredClone(session);
  }

  /** Activate a previously prepared session using only persisted request data. */
  async activatePrepared(
    userId: string,
    sessionId: string,
    suppliedApiKey?: string,
    signal?: AbortSignal,
  ): Promise<SessionRecord> {
    this.assertAccepting();
    this.requireProtocolIntegrity();
    const session = this.store.getSession(userId, sessionId);
    if (this.active.has(sessionId)) return structuredClone(session);
    if (!["creating", "awaiting_user_auth"].includes(session.status)) {
      throw new RuntimePolicyError(
        "SESSION_NOT_ACTIVE",
        `Prepared Pi session cannot be activated (${session.status})`,
        409,
      );
    }
    const created = this.store.listEvents(userId, sessionId)
      .find((event) => event.type === "session.created");
    const data = created?.data && typeof created.data === "object"
      ? created.data as Record<string, unknown> : {};
    const authMode = data.authMode === "pi_auth" ? "pi_auth" : "api_key";
    if (authMode === "api_key" && !suppliedApiKey) {
      throw new RuntimePolicyError(
        "MODEL_AUTH_REQUIRED",
        "The queued API key lease is unavailable. Re-enter the key to continue.",
        409,
      );
    }
    if (authMode === "pi_auth" && suppliedApiKey) {
      throw new RuntimePolicyError(
        "INVALID_CONTEXT",
        "An API key must not be supplied for a Pi-auth session",
        400,
      );
    }
    const requestEnvelope = data.localExtractionRequest
      && typeof data.localExtractionRequest === "object"
      ? data.localExtractionRequest as Record<string, unknown>
      : {};
    const dispatchRequested = requestEnvelope.dispatchRequested === true;
    const inputSelections = persistedInputSelections(data, !dispatchRequested);
    const request: LocalAgentCreateRequest = {
      userId,
      jobId: session.jobId,
      task: session.task,
      contextMode: continuationContext(data.contextMode),
      workspaceId: typeof data.requestedWorkspaceId === "string"
        ? data.requestedWorkspaceId : session.id,
      provider: requiredContinuationString(data.provider, "provider"),
      modelId: requiredContinuationString(data.modelId, "model"),
      thinkingLevel: persistedThinkingLevel(data.thinkingLevel),
      authMode,
      ...(suppliedApiKey ? { apiKey: suppliedApiKey } : {}),
      inputs: inputSelections,
      ...(session.parentSessionId ? { parentSessionId: session.parentSessionId } : {}),
      ...(session.parentResultSha256
        ? { parentResultSha256: session.parentResultSha256 } : {}),
      ...(data.retry && typeof data.retry === "object"
        ? { relation: "retry" as const }
        : data.continuation && typeof data.continuation === "object"
          ? { relation: "continuation" as const }
          : {}),
    };
    return this.activatePreparedRequest(request, session, signal);
  }

  /** Compatibility path for interactive callers that do not use the queue. */
  async create(request: LocalAgentCreateRequest): Promise<SessionRecord> {
    const session = await this.prepare(request);
    return this.activatePrepared(request.userId, session.id, request.apiKey);
  }

  /** Terminalize a prepared request that lost admission during shutdown. */
  interruptPreparedForShutdown(userId: string, sessionId: string): void {
    const session = this.store.getSession(userId, sessionId);
    if (session.status !== "creating") return;
    session.status = "interrupted";
    session.blockReason = "LOCAL_RUNTIME_SHUTDOWN";
    session.updatedAt = new Date().toISOString();
    this.store.putSession(session);
    this.store.appendEvent(userId, sessionId, "job.interrupted", {
      code: "LOCAL_RUNTIME_SHUTDOWN",
      source: "runtime_shutdown",
      beforeActivation: true,
      message: "The local runtime stopped before this prepared extraction entered the queue.",
      resumable: false,
      retryCreatesNewAttempt: true,
    });
  }

  private async activatePreparedRequest(
    request: LocalAgentCreateRequest,
    session: SessionRecord,
    signal?: AbortSignal,
  ): Promise<SessionRecord> {
    this.requireProtocolIntegrity();
    const sessionId = session.id;
    const workspace = safeChild(this.options.workspaceRoot, sessionId);
    const agentDir = safeChild(this.options.agentRoot, safeIdentity(request.userId));
    const piSessionDir = safeChild(path.join(agentDir, "sessions"), sessionId);

    let mcp: ChemMcpClient | undefined;
    let pi: PiSessionHandle | undefined;
    let inputs: MaterializedLocalInput[] = [];
    let preActivationFatal: Error | undefined;
    try {
      signal?.throwIfAborted();
      await mkdir(workspace, { recursive: true, mode: 0o700 });
      await mkdir(agentDir, { recursive: true, mode: 0o700 });
      await mkdir(piSessionDir, { recursive: true, mode: 0o700 });
      await chmod(piSessionDir, 0o700);
      signal?.throwIfAborted();
      if (request.inputs?.length) {
        if (!this.options.inputStore) {
          throw new RuntimePolicyError(
            "INPUT_BACKEND_NOT_CONNECTED",
            "Local input storage is not configured",
            503,
          );
        }
        inputs = await this.options.inputStore.materialize(
          request.userId,
          request.contextMode,
          request.inputs,
          workspace,
        );
        signal?.throwIfAborted();
        this.store.appendEvent(request.userId, sessionId, "agent.event", {
          phase: "input.validated",
          contextMode: request.contextMode,
          inputs: inputs.map((input) => ({
            id: input.id,
            kind: input.kind,
            originalName: input.originalName,
            mediaType: input.mediaType,
            sha256: input.sha256,
            byteSize: input.byteSize,
            workspacePath: input.workspacePath,
          })),
        });
      }
      const skills = await this.dependencies.loadSkills(this.options.repoRoot);
      signal?.throwIfAborted();
      mcp = await abortableResource(this.dependencies.startMcp({
        python: this.options.python,
        script: path.join(this.options.repoRoot, CANONICAL_CHEM_MCP_RUNNER),
        cwd: workspace,
        ...(signal ? { signal } : {}),
        onFatal: (error) => {
          if (this.active.has(sessionId)) {
            void this.failActiveSession(request.userId, sessionId, error);
          } else {
            preActivationFatal = error;
          }
        },
      }), signal, (resource) => resource.terminate());
      signal?.throwIfAborted();
      const chemistryTools = await abortableValue(
        this.dependencies.createTools(mcp, PINNED_TOOL_ALLOWLIST, workspace),
        signal,
      );
      signal?.throwIfAborted();
      const workspaceUiTools = workspaceUiToolDefinitions({
        jobId: runtimeReviewerJobId(
          request.contextMode,
          request.relation === "continuation" && request.parentSessionId
            ? this.continuationRootSessionId(request.userId, request.parentSessionId)
            : sessionId,
        ),
        reviewTarget: "figure",
        modules: FIGURE_WORKSPACE_UI_MODULES,
        onCommand: (workspaceCommand) => {
          this.store.appendEvent(request.userId, sessionId, "agent.event", {
            phase: "workspace.command",
            workspaceCommand,
          }, request.apiKey ? [request.apiKey] : []);
        },
      });
      const activeToolNames = [
        ...PINNED_TOOL_ALLOWLIST,
        ...WORKSPACE_UI_TOOL_NAMES,
      ];
      const customTools = [...chemistryTools, ...workspaceUiTools];
      const protocolToolContract = canonicalToolContract(customTools);
      pi = await abortableResource(this.dependencies.createPi({
        cwd: workspace,
        agentDir,
        provider: request.provider,
        modelId: request.modelId,
        thinkingLevel: request.thinkingLevel ?? DEFAULT_EXTRACTION_THINKING_LEVEL,
        authMode: request.authMode ?? "api_key",
        ...(request.apiKey ? { apiKey: request.apiKey } : {}),
        ...(this.options.authPath ? { authPath: this.options.authPath } : {}),
        modelsPath: this.options.modelsPath ?? path.join(agentDir, "models.json"),
        sessionDir: piSessionDir,
        sessionId,
        skills,
        tools: [...activeToolNames],
        customTools,
        onEvent: (event) =>
          this.persistPiEvent(request.userId, sessionId, request.apiKey ?? "", event),
      }), signal, (resource) => resource.dispose());
      signal?.throwIfAborted();
      if (preActivationFatal) throw preActivationFatal;
      this.active.set(sessionId, {
        pi,
        mcp,
        apiKey: request.apiKey ?? "",
        workspace,
        workspaceId: sessionId,
        contextMode: request.contextMode,
        inputs,
        provider: request.provider,
        modelId: request.modelId,
        thinkingLevel: request.thinkingLevel ?? DEFAULT_EXTRACTION_THINKING_LEVEL,
        skills,
        protocolToolContract,
        protocolPerception: [],
        toolCaptures: [],
        streamDeltas: [],
        streamDeltaChars: 0,
        toolArgumentDeltaChunks: 0,
        toolArgumentDeltaChars: 0,
        finalizing: false,
        cancelling: false,
      });
      session.status = "ready";
      delete session.blockReason;
      session.updatedAt = new Date().toISOString();
      this.store.putSession(session);
      this.store.appendEvent(request.userId, sessionId, "session.ready", {
        source: "pi_sdk",
        skillHashes: skills.map((skill) => ({
          name: skill.name,
          sha256: skill.sha256,
        })),
        tools: activeToolNames,
        thinkingLevel: request.thinkingLevel ?? DEFAULT_EXTRACTION_THINKING_LEVEL,
        workspaceId: sessionId,
        requestedWorkspaceId: request.workspaceId,
        inputs: inputs.map((input) => ({
          id: input.id,
          kind: input.kind,
          workspacePath: input.workspacePath,
          sha256: input.sha256,
        })),
      });
      return structuredClone(session);
    } catch (error) {
      await Promise.allSettled([
        mcp?.terminate(),
        pi?.dispose(),
      ]);
      const cancelled = signal?.aborted === true;
      const interrupted = cancelled && signal?.reason === "runtime_shutdown";
      session.status = interrupted ? "interrupted" : cancelled ? "cancelled" : "failed";
      if (interrupted) session.blockReason = "LOCAL_RUNTIME_SHUTDOWN";
      else if (cancelled) delete session.blockReason;
      else session.blockReason = "LOCAL_PI_SESSION_FAILED";
      session.updatedAt = new Date().toISOString();
      this.store.putSession(session);
      this.store.appendEvent(
        request.userId,
        sessionId,
        interrupted ? "job.interrupted" : cancelled ? "job.cancelled" : "agent.error",
        interrupted
          ? {
              code: "LOCAL_RUNTIME_SHUTDOWN",
              source: "runtime_shutdown",
              message: "The local runtime stopped during activation. The model turn was not replayed.",
              resumable: false,
              retryCreatesNewAttempt: true,
            }
          : cancelled
          ? {
              phase: "job.cancelled",
              source: "queue",
              duringActivation: true,
              message: "Extraction cancelled before the model turn started.",
            }
          : {
              code: "LOCAL_PI_SESSION_FAILED",
              message: error instanceof Error ? error.message : "Local Pi session failed",
            },
        request.apiKey ? [request.apiKey] : [],
      );
      throw error;
    }
  }

  async startExtraction(
    userId: string,
    sessionId: string,
    userDirection?: string,
    signal?: AbortSignal,
  ): Promise<void> {
    this.assertAccepting();
    this.requireProtocolIntegrity();
    const session = this.store.getSession(userId, sessionId);
    const active = this.requireActive(sessionId);
    const assertStillActive = (): void => {
      signal?.throwIfAborted();
      if (this.active.get(sessionId) !== active) {
        throw new RuntimePolicyError(
          "SESSION_NOT_ACTIVE",
          "Extraction was cancelled while preparing the input",
          409,
        );
      }
    };
    try {
      assertStillActive();
      if (active.inputs.length === 0) {
        throw new RuntimePolicyError(
          "INPUT_REQUIRED",
          "A live extraction session requires a staged input",
          400,
        );
      }
      if (active.pi.supportsImages !== true) {
        throw new RuntimePolicyError(
          "MODEL_HAS_NO_VISION",
          `${active.provider}/${active.modelId} cannot accept the reaction figure`,
          400,
        );
      }
      const figure = active.inputs.find((input) => input.kind === "figure");
      if (!figure) throw new Error("Figure input was not materialized");
      const figureBytes = await readFile(figure.absolutePath);
      assertStillActive();
      const resized = await resizeImage(figureBytes, figure.mediaType, {
        maxWidth: 2600,
        maxHeight: 2600,
        maxBytes: 5 * 1024 * 1024,
      });
      assertStillActive();
      if (!resized) {
        throw new RuntimePolicyError(
          "IMAGE_PREPARATION_FAILED",
          "The reaction figure could not be prepared for the selected vision model",
          400,
        );
      }
      await mkdir(path.join(active.workspace, "output", "perception"), {
        recursive: true,
        mode: 0o700,
      });
      assertStillActive();
      const prompt = figureExtractionPrompt(figure.workspacePath, userDirection, session.task);
      this.persistProtocolFingerprint(
        userId,
        session,
        active,
        prompt,
        "dispatch",
        "deferred_to_canonical_tool",
      );
      this.persistUserMessage(userId, sessionId, active.apiKey, prompt, "prompt");
      session.status = "running";
      session.updatedAt = new Date().toISOString();
      this.store.putSession(session);
      this.store.appendEvent(userId, sessionId, "agent.event", {
        phase: "extraction.started",
        contextMode: "figure",
        inputArtifactId: figure.id,
        imageAttachment: {
          mediaType: resized.mimeType,
          resized: resized.wasResized,
          width: resized.width,
          height: resized.height,
        },
      });
      void active.pi.session.prompt(prompt, {
        images: [{ type: "image", data: resized.data, mimeType: resized.mimeType }],
      }).catch((error) => this.failPrompt(userId, sessionId, active, error));
    } catch (error) {
      await this.failExtractionStart(userId, sessionId, active, error, signal);
      throw error;
    }
  }

  async prompt(
    userId: string,
    sessionId: string,
    text: string,
    behavior: "prompt" | "steer" | "follow_up" = "prompt",
    suppliedApiKey?: string,
    signal?: AbortSignal,
  ): Promise<void> {
    this.assertAccepting();
    // This is a sentinel scan, not proof that arbitrary payloads are GT-free.
    // Run it before rehydration or Pi dispatch so a rejected user turn cannot
    // affect the live model while disappearing from the durable conversation.
    assertNoGroundTruth({ text });
    this.requireProtocolIntegrity();
    signal?.throwIfAborted();
    const session = this.store.getSession(userId, sessionId);
    if (["blocked", "failed", "aborted", "review_ready", "partial", "cancelled", "interrupted"]
      .includes(session.status)) {
      throw new RuntimePolicyError(
        "SESSION_NOT_ACTIVE",
        `Pi session is terminal (${session.status})`,
        409,
      );
    }
    if (suppliedApiKey && session.status !== "dormant") {
      throw new RuntimePolicyError(
        "INVALID_CONTEXT",
        "An API key is only accepted while reopening a dormant session",
        400,
      );
    }
    const active = session.status === "dormant"
      ? await this.rehydrateDormant(userId, session, suppliedApiKey)
      : this.requireActive(sessionId);
    signal?.throwIfAborted();
    if (active.cancelling) {
      throw new RuntimePolicyError(
        "AGENT_BUSY",
        "The current extraction attempt is being cancelled",
        409,
      );
    }
    if (active.finalizing) {
      throw new RuntimePolicyError(
        "AGENT_BUSY",
        "The current turn is sealing immutable result artifacts",
        409,
      );
    }
    if (behavior === "prompt" && active.pi.session.isStreaming) {
      throw new RuntimePolicyError(
        "AGENT_BUSY",
        "Agent is streaming; use steer or follow_up",
        409,
      );
    }
    if (behavior === "steer") {
      await active.pi.session.steer(text);
      signal?.throwIfAborted();
      this.persistUserMessage(userId, sessionId, active.apiKey, text, behavior);
      return;
    }
    if (behavior === "follow_up") {
      await active.pi.session.followUp(text);
      signal?.throwIfAborted();
      this.persistUserMessage(userId, sessionId, active.apiKey, text, behavior);
      return;
    }
    this.persistUserMessage(userId, sessionId, active.apiKey, text, behavior);
    session.status = "running";
    session.updatedAt = new Date().toISOString();
    this.store.putSession(session);
    void active.pi.session.prompt(text).catch((error) =>
      this.failPrompt(userId, sessionId, active, error));
  }

  async continueFrom(
    userId: string,
    parentSessionId: string,
    text: string,
    signal?: AbortSignal,
  ): Promise<SessionRecord> {
    this.assertAccepting();
    assertNoGroundTruth({ text });
    signal?.throwIfAborted();
    const parent = this.store.getSession(userId, parentSessionId);
    if (!["review_ready", "partial"].includes(parent.status)) {
      throw new RuntimePolicyError(
        "SESSION_NOT_ACTIVE",
        `Pi session is not sealed (${parent.status})`,
        409,
      );
    }
    const events = this.store.listEvents(userId, parentSessionId);
    const created = events.find((event) => event.type === "session.created");
    const data = created?.data && typeof created.data === "object"
      ? created.data as Record<string, unknown> : {};
    const active = this.active.get(parentSessionId);
    const contextMode = active?.contextMode ?? continuationContext(data.contextMode);
    const provider = active?.provider ?? requiredContinuationString(data.provider, "provider");
    const modelId = active?.modelId ?? requiredContinuationString(data.modelId, "model");
    const thinkingLevel = active?.thinkingLevel ?? persistedThinkingLevel(data.thinkingLevel);
    const inputArtifactIds = active
      ? active.inputs.map((input) => ({ artifactId: input.id, kind: input.kind }))
      : await this.continuationInputs(userId, data.inputArtifactIds);
    const authMode = parent.authMode === "personal_codex_subscription" ? "pi_auth" : "api_key";
    const apiKey = active?.apiKey || this.takeSealedApiKeyLease(parentSessionId);
    if (authMode === "api_key" && !apiKey) {
      throw new RuntimePolicyError(
        "SESSION_NOT_ACTIVE",
        "The BYO API key was intentionally not persisted. Start a new task and provide it again.",
        409,
      );
    }
    const parentResult = this.store.getResult(userId, parentSessionId);
    const parentResultSha256 = createHash("sha256")
      .update(JSON.stringify(parentResult))
      .digest("hex");
    const task: TaskKind = "inspect_selection";
    const request: LocalAgentCreateRequest = {
      userId,
      jobId: `linked:${parent.id}`,
      task,
      contextMode,
      workspaceId: "linked-task",
      provider,
      modelId,
      thinkingLevel,
      authMode,
      ...(apiKey ? { apiKey } : {}),
      inputs: inputArtifactIds,
      parentSessionId,
      parentResultSha256,
      relation: "continuation",
    };
    let session = await this.prepare(request);
    session = await this.activatePrepared(userId, session.id, apiKey, signal);
    await this.startExtraction(userId, session.id, text, signal);
    session = this.store.getSession(userId, session.id);
    const unchangedParentSha256 = createHash("sha256")
      .update(JSON.stringify(this.store.getResult(userId, parentSessionId)))
      .digest("hex");
    if (unchangedParentSha256 !== parentResultSha256) {
      throw new Error("Sealed parent result changed while creating a continuation");
    }
    return session;
  }

  async prepareRetry(
    userId: string,
    previousSessionId: string,
    suppliedApiKey?: string,
  ): Promise<{ session: SessionRecord; apiKey?: string }> {
    this.assertAccepting();
    const resolved = await this.resolveRetryRequest(
      userId,
      previousSessionId,
      suppliedApiKey,
    );
    const session = await this.prepare({
      ...resolved.request,
      dispatchRequested: true,
    });
    return {
      session,
      ...(resolved.apiKey ? { apiKey: resolved.apiKey } : {}),
    };
  }

  async retry(
    userId: string,
    previousSessionId: string,
    suppliedApiKey?: string,
  ): Promise<SessionRecord> {
    this.assertAccepting();
    const resolved = await this.resolveRetryRequest(
      userId,
      previousSessionId,
      suppliedApiKey,
    );
    if (resolved.request.authMode === "api_key" && !resolved.apiKey) {
      throw new RuntimePolicyError(
        "SESSION_NOT_ACTIVE",
        "The BYO API key is no longer in memory. Supply it again to retry.",
        409,
      );
    }
    let session = await this.create(resolved.request);
    await this.startExtraction(userId, session.id);
    session = this.store.getSession(userId, session.id);
    return session;
  }

  private async resolveRetryRequest(
    userId: string,
    previousSessionId: string,
    suppliedApiKey?: string,
  ): Promise<{ request: LocalAgentCreateRequest; apiKey?: string }> {
    const previous = this.store.getSession(userId, previousSessionId);
    if (!["cancelled", "interrupted", "failed", "aborted"].includes(previous.status)) {
      throw new RuntimePolicyError(
        "RETRY_NOT_ALLOWED",
        `Cannot retry a ${previous.status} live session`,
        409,
      );
    }
    const events = this.store.listEvents(userId, previousSessionId);
    const created = events.find((event) => event.type === "session.created");
    const data = created?.data && typeof created.data === "object"
      ? created.data as Record<string, unknown> : {};
    const contextMode = continuationContext(data.contextMode);
    const provider = requiredContinuationString(data.provider, "provider");
    const modelId = requiredContinuationString(data.modelId, "model");
    const thinkingLevel = persistedThinkingLevel(data.thinkingLevel);
    const inputs = await this.continuationInputs(userId, data.inputArtifactIds);
    const authMode = previous.authMode === "personal_codex_subscription"
      ? "pi_auth" : "api_key";
    if (authMode === "pi_auth" && suppliedApiKey) {
      throw new RuntimePolicyError(
        "INVALID_CONTEXT",
        "An API key must not be supplied for a Pi-auth retry",
        400,
      );
    }
    let apiKey: string | undefined;
    if (suppliedApiKey) {
      this.clearSealedApiKeyLease(previousSessionId);
      apiKey = suppliedApiKey;
    } else {
      apiKey = this.takeSealedApiKeyLease(previousSessionId);
    }
    const request: LocalAgentCreateRequest = {
      userId,
      jobId: previous.jobId,
      task: previous.task,
      contextMode,
      workspaceId: "retry-attempt",
      provider,
      modelId,
      thinkingLevel,
      authMode,
      ...(apiKey ? { apiKey } : {}),
      inputs,
      parentSessionId: previousSessionId,
      relation: "retry",
    };
    return { request, ...(apiKey ? { apiKey } : {}) };
  }

  private async continuationInputs(
    userId: string,
    value: unknown,
  ): Promise<LocalInputSelection[]> {
    if (!this.options.inputStore || !Array.isArray(value) || value.length === 0) {
      throw new RuntimePolicyError(
        "INPUT_REQUIRED",
        "The original immutable input is unavailable for a linked task.",
        409,
      );
    }
    return Promise.all(value.map(async (artifactId) => {
      if (typeof artifactId !== "string") {
        throw new RuntimePolicyError("INVALID_CONTEXT", "Invalid continuation input", 409);
      }
      const artifact = await this.options.inputStore!.get(userId, artifactId);
      return { artifactId: artifact.id, kind: artifact.kind };
    }));
  }

  async liveArtifact(
    userId: string,
    sessionId: string,
    kind: string,
  ): Promise<{ bytes: Buffer; mediaType: string; sha256: string; fileName: string }> {
    const allowed: Record<string, { locator: string; mediaType: string }> = {
      overview: { locator: "output/perception/figure_overview.png", mediaType: "image/png" },
      "ocsr-grid": { locator: "output/perception/figure_ocsr_grid.png", mediaType: "image/png" },
      molparser: { locator: "output/perception/figure_molparser.json", mediaType: "application/json" },
      candidate: { locator: "output/result.v2.json", mediaType: "application/json" },
    };
    let requested = allowed[kind];
    if (!requested) {
      throw new RuntimePolicyError("ARTIFACT_NOT_FOUND", "Live artifact not found", 404);
    }
    const session = this.store.getSession(userId, sessionId);
    if (!session.sandboxId) {
      throw new RuntimePolicyError("ARTIFACT_NOT_FOUND", "Live artifact not found", 404);
    }
    const workspace = safeChild(this.options.workspaceRoot, session.sandboxId);
    const artifactRead = await readWorkspaceArtifact(workspace, requested.locator);
    if (!artifactRead) {
      throw new RuntimePolicyError("ARTIFACT_NOT_FOUND", "Live artifact not found", 404);
    }
    const { bytes, path: target } = artifactRead;
    if (bytes.length > 50 * 1024 * 1024) {
      throw new RuntimePolicyError("ARTIFACT_CORRUPT", "Live artifact exceeds the preview limit", 409);
    }
    return {
      bytes,
      mediaType: requested.mediaType,
      sha256: createHash("sha256").update(bytes).digest("hex"),
      fileName: path.basename(target),
    };
  }

  async artifact(
    userId: string,
    sessionId: string,
    artifactId: string,
  ): Promise<{ bytes: Buffer; mediaType: string; sha256: string; fileName: string }> {
    const session = this.store.getSession(userId, sessionId);
    const result = this.store.getResult(userId, sessionId);
    const artifact = artifactFromLiveResult(result, artifactId);
    if (!artifact || !session.sandboxId) {
      throw new RuntimePolicyError("ARTIFACT_NOT_FOUND", "Artifact not found", 404);
    }
    const workspace = safeChild(this.options.workspaceRoot, session.sandboxId);
    const artifactRead = await readWorkspaceArtifact(workspace, artifact.locator);
    if (!artifactRead) {
      throw new RuntimePolicyError("ARTIFACT_NOT_FOUND", "Artifact not found", 404);
    }
    const { bytes, path: target } = artifactRead;
    if (createHash("sha256").update(bytes).digest("hex") !== artifact.sha256) {
      throw new RuntimePolicyError("ARTIFACT_CORRUPT", "Artifact hash changed", 409);
    }
    return {
      bytes,
      mediaType: artifact.mediaType,
      sha256: artifact.sha256,
      fileName: path.basename(target),
    };
  }

  async abort(userId: string, sessionId: string): Promise<void> {
    const session = this.store.getSession(userId, sessionId);
    if (!["creating", "ready", "running"].includes(session.status)) {
      throw new RuntimePolicyError(
        "SESSION_NOT_ACTIVE",
        `Pi session cannot be cancelled (${session.status})`,
        409,
      );
    }
    const active = this.requireActive(sessionId);
    if (active.finalizing) {
      throw new RuntimePolicyError(
        "AGENT_BUSY",
        "The current turn is sealing immutable result artifacts",
        409,
      );
    }
    if (active.cancelling) {
      throw new RuntimePolicyError(
        "AGENT_BUSY",
        "The current extraction attempt is already being cancelled",
        409,
      );
    }
    active.cancelling = true;
    const piAbort = await boundedOperation(
      Promise.resolve().then(() => active.pi.session.abort()),
      this.options.abortTimeoutMs ?? 1_000,
    );
    session.status = "cancelled";
    session.updatedAt = new Date().toISOString();
    this.store.putSession(session);
    this.store.appendEvent(userId, sessionId, "job.cancelled", {
      phase: "job.cancelled",
      source: "user",
      forced: !piAbort.settled,
      retryable: true,
      inputArtifactIds: active.inputs.map((input) => input.id),
      message: "Extraction cancelled. Retry starts a new isolated attempt.",
    });
    await this.sealActiveSession(sessionId, active, true);
  }

  async interruptForShutdown(userId: string, sessionId: string): Promise<void> {
    let session = this.store.getSession(userId, sessionId);
    if (["review_ready", "partial", "failed", "aborted", "cancelled", "interrupted"]
      .includes(session.status)) return;
    const active = this.active.get(sessionId);
    // An activation in progress is stopped by the dispatcher's AbortSignal;
    // its catch path persists the same runtime-shutdown provenance.
    if (!active) return;
    if (active.cancelling) {
      await this.waitForActiveSettlement(sessionId, active);
      return;
    }
    if (active.finalizing) {
      const deadline = Date.now() + 1_500;
      while (active.finalizing && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 25));
      }
      session = this.store.getSession(userId, sessionId);
      if (["review_ready", "partial", "failed", "cancelled", "interrupted"]
        .includes(session.status)) return;
      // Do not race an immutable result publication. If the bounded wait was
      // insufficient, process restart reconciles the verified result first.
      if (active.finalizing) return;
    }
    active.cancelling = true;
    const piAbort = await boundedOperation(
      Promise.resolve().then(() => active.pi.session.abort()),
      this.options.abortTimeoutMs ?? 1_000,
    );
    session.status = "interrupted";
    session.blockReason = "LOCAL_RUNTIME_SHUTDOWN";
    session.updatedAt = new Date().toISOString();
    this.store.putSession(session);
    this.store.appendEvent(userId, sessionId, "job.interrupted", {
      code: "LOCAL_RUNTIME_SHUTDOWN",
      source: "runtime_shutdown",
      forced: !piAbort.settled,
      message: "The local runtime stopped before extraction completed. Retry creates a new isolated attempt.",
      resumable: false,
      retryCreatesNewAttempt: true,
    }, active.apiKey ? [active.apiKey] : []);
    await this.sealActiveSession(sessionId, active, false);
  }

  async dispose(sessionId: string): Promise<void> {
    this.clearSealedApiKeyLease(sessionId);
    const settling = this.settlements.get(sessionId);
    if (settling) {
      await settling;
      return;
    }
    const active = this.active.get(sessionId);
    if (!active) return;
    this.active.delete(sessionId);
    await Promise.allSettled([
      boundedOperation(active.pi.dispose(), this.options.cleanupTimeoutMs ?? 2_500),
      boundedOperation(active.mcp.terminate(), this.options.cleanupTimeoutMs ?? 2_500),
    ]);
  }

  async disposeAll(): Promise<void> {
    this.closing = true;
    for (const sessionId of this.sealedApiKeyLeases.keys()) {
      this.clearSealedApiKeyLease(sessionId);
    }
    const rehydrations = [...this.rehydrating.values()];
    for (const rehydration of rehydrations) {
      rehydration.controller.abort("runtime_shutdown");
    }
    await Promise.allSettled(rehydrations.map((rehydration) => rehydration.promise));
    await Promise.allSettled(this.settlements.values());
    const active = [...this.active.entries()];
    this.active.clear();
    await Promise.all(active.map(async ([, handle]) => {
      await Promise.allSettled([
        boundedOperation(handle.pi.dispose(), this.options.cleanupTimeoutMs ?? 2_500),
        boundedOperation(handle.mcp.terminate(), this.options.cleanupTimeoutMs ?? 2_500),
      ]);
    }));
  }

  async waitForTerminalSettlement(sessionId: string): Promise<void> {
    const settling = this.settlements.get(sessionId);
    if (settling) await settling;
    while (this.active.has(sessionId)) await new Promise((resolve) => setTimeout(resolve, 25));
  }

  private requireActive(sessionId: string): ActiveAgent {
    const active = this.active.get(sessionId);
    if (!active) {
      throw new RuntimePolicyError(
        "SESSION_NOT_ACTIVE",
        "Pi session is not active in this runtime process",
        409,
      );
    }
    return active;
  }

  private assertAccepting(): void {
    if (!this.closing) return;
    throw new RuntimePolicyError(
      "RUNTIME_SHUTTING_DOWN",
      "The local Pi runtime is shutting down. Retry after it restarts.",
      503,
    );
  }

  private async rehydrateDormant(
    userId: string,
    session: SessionRecord,
    suppliedApiKey?: string,
  ): Promise<ActiveAgent> {
    this.assertAccepting();
    const existing = this.active.get(session.id);
    if (existing) return existing;
    const authMode = session.authMode === "personal_codex_subscription"
      ? "pi_auth" : "api_key";
    if (authMode === "pi_auth" && suppliedApiKey) {
      throw new RuntimePolicyError(
        "INVALID_CONTEXT",
        "An API key must not be supplied for a Pi-auth session",
        400,
      );
    }
    if (authMode === "api_key" && !suppliedApiKey) {
      throw new RuntimePolicyError(
        "CREDENTIAL_REQUIRED",
        "The BYO API key was intentionally not persisted. Enter it again to reopen this conversation.",
        409,
      );
    }
    const pending = this.rehydrating.get(session.id);
    if (pending) return pending.promise;
    const controller = new AbortController();
    const rehydration = this.performDormantRehydration(
      userId,
      session,
      authMode,
      suppliedApiKey,
      controller.signal,
    ).finally(() => {
      if (this.rehydrating.get(session.id)?.promise === rehydration) {
        this.rehydrating.delete(session.id);
      }
    });
    this.rehydrating.set(session.id, { controller, promise: rehydration });
    return rehydration;
  }

  private async performDormantRehydration(
    userId: string,
    session: SessionRecord,
    authMode: "api_key" | "pi_auth",
    apiKey?: string,
    signal?: AbortSignal,
  ): Promise<ActiveAgent> {
    signal?.throwIfAborted();
    const events = this.store.listEvents(userId, session.id);
    const created = events.find((event) => event.type === "session.created");
    const data = created?.data && typeof created.data === "object"
      ? created.data as Record<string, unknown> : {};
    const contextMode = continuationContext(data.contextMode);
    const provider = requiredContinuationString(data.provider, "provider");
    const modelId = requiredContinuationString(data.modelId, "model");
    const thinkingLevel = persistedThinkingLevel(data.thinkingLevel);
    const inputSelections = Array.isArray(data.inputArtifactIds) && data.inputArtifactIds.length
      ? await this.continuationInputs(userId, data.inputArtifactIds)
      : [];
    const workspace = safeChild(this.options.workspaceRoot, session.id);
    const agentDir = safeChild(this.options.agentRoot, safeIdentity(userId));
    const piSessionDir = safeChild(path.join(agentDir, "sessions"), session.id);
    let mcp: ChemMcpClient | undefined;
    let pi: PiSessionHandle | undefined;
    try {
      await mkdir(workspace, { recursive: true, mode: 0o700 });
      signal?.throwIfAborted();
      await mkdir(agentDir, { recursive: true, mode: 0o700 });
      await mkdir(piSessionDir, { recursive: true, mode: 0o700 });
      await chmod(piSessionDir, 0o700);
      signal?.throwIfAborted();
      const inputs = inputSelections.length
        ? await this.options.inputStore!.materialize(
            userId,
            contextMode,
            inputSelections,
            workspace,
          )
        : [];
      signal?.throwIfAborted();
      const skills = await this.dependencies.loadSkills(this.options.repoRoot);
      signal?.throwIfAborted();
      const availableToolNames = [
        ...PINNED_TOOL_ALLOWLIST,
        ...WORKSPACE_UI_TOOL_NAMES,
      ];
      const activeToolNames = recoveredPinnedToolNames(events, availableToolNames);
      assertPinnedRecovery(events, skills, activeToolNames);
      let preActivationFatal: Error | undefined;
      mcp = await abortableResource(this.dependencies.startMcp({
        python: this.options.python,
        script: path.join(this.options.repoRoot, CANONICAL_CHEM_MCP_RUNNER),
        cwd: workspace,
        ...(signal ? { signal } : {}),
        onFatal: (error) => {
          if (this.active.has(session.id)) {
            void this.failActiveSession(userId, session.id, error);
          } else {
            preActivationFatal = error;
          }
        },
      }), signal, (resource) => resource.terminate());
      signal?.throwIfAborted();
      const chemistryTools = await abortableValue(
        this.dependencies.createTools(mcp, PINNED_TOOL_ALLOWLIST, workspace),
        signal,
      );
      signal?.throwIfAborted();
      const workspaceUiTools = activeToolNames.includes(WORKSPACE_UI_TOOL_NAMES[0])
        ? workspaceUiToolDefinitions({
            jobId: runtimeReviewerJobId(
              contextMode,
              typeof data.workspaceTargetSessionId === "string"
                ? data.workspaceTargetSessionId : session.id,
            ),
            reviewTarget: "figure",
            modules: FIGURE_WORKSPACE_UI_MODULES,
            onCommand: (workspaceCommand) => {
              this.store.appendEvent(userId, session.id, "agent.event", {
                phase: "workspace.command",
                workspaceCommand,
              }, apiKey ? [apiKey] : []);
            },
          })
        : [];
      const customTools = [...chemistryTools, ...workspaceUiTools];
      const protocolToolContract = canonicalToolContract(customTools);
      pi = await abortableResource(this.dependencies.createPi({
        cwd: workspace,
        agentDir,
        provider,
        modelId,
        thinkingLevel,
        authMode,
        ...(apiKey ? { apiKey } : {}),
        ...(this.options.authPath ? { authPath: this.options.authPath } : {}),
        modelsPath: this.options.modelsPath ?? path.join(agentDir, "models.json"),
        sessionDir: piSessionDir,
        sessionId: session.id,
        resume: true,
        skills,
        tools: [...activeToolNames],
        customTools,
        onEvent: (event) =>
          this.persistPiEvent(userId, session.id, apiKey ?? "", event),
      }), signal, (resource) => resource.dispose());
      signal?.throwIfAborted();
      if (preActivationFatal) throw preActivationFatal;
      const active: ActiveAgent = {
        pi,
        mcp,
        apiKey: apiKey ?? "",
        workspace,
        workspaceId: session.id,
        contextMode,
        inputs,
        provider,
        modelId,
        thinkingLevel,
        skills,
        protocolToolContract,
        protocolPerception: [],
        toolCaptures: [],
        streamDeltas: [],
        streamDeltaChars: 0,
        toolArgumentDeltaChunks: 0,
        toolArgumentDeltaChars: 0,
        finalizing: false,
        cancelling: false,
      };
      signal?.throwIfAborted();
      this.active.set(session.id, active);
      session.status = "ready";
      delete session.blockReason;
      session.updatedAt = new Date().toISOString();
      this.store.putSession(session);
      this.store.appendEvent(userId, session.id, "session.ready", {
        source: "pi_sdk_rehydrated",
        workspaceId: session.id,
        skillHashes: skills.map((skill) => ({
          name: skill.name,
          sha256: skill.sha256,
        })),
        tools: activeToolNames,
        thinkingLevel,
      });
      return active;
    } catch (error) {
      await Promise.allSettled([mcp?.terminate(), pi?.dispose()]);
      session.status = "interrupted";
      const shutdown = signal?.aborted && signal.reason === "runtime_shutdown";
      session.blockReason = shutdown
        ? "LOCAL_RUNTIME_SHUTDOWN" : "LOCAL_REHYDRATION_FAILED";
      session.updatedAt = new Date().toISOString();
      this.store.putSession(session);
      this.store.appendEvent(userId, session.id, "job.interrupted", {
        code: shutdown ? "LOCAL_RUNTIME_SHUTDOWN" : "LOCAL_REHYDRATION_FAILED",
        source: shutdown ? "runtime_shutdown" : "rehydration",
        message: shutdown
          ? "The local runtime stopped while reopening this persisted Pi conversation."
          : "The persisted Pi conversation could not be reopened safely.",
        resumable: false,
        retryCreatesNewAttempt: true,
      }, apiKey ? [apiKey] : []);
      throw new RuntimePolicyError(
        shutdown ? "RUNTIME_SHUTTING_DOWN" : "LOCAL_REHYDRATION_FAILED",
        error instanceof Error ? error.message : "Pi conversation recovery failed",
        shutdown ? 503 : 409,
      );
    }
  }

  private async failActiveSession(userId: string, sessionId: string, error: Error): Promise<void> {
    const active = this.active.get(sessionId);
    if (!active || active.cancelling) return;
    const session = this.store.getSession(userId, sessionId);
    if (["blocked", "failed", "aborted", "review_ready", "partial", "cancelled", "interrupted"]
      .includes(session.status)) return;
    session.status = "failed";
    session.blockReason = "CHEM_MCP_FAILED";
    session.updatedAt = new Date().toISOString();
    this.store.putSession(session);
    this.store.appendEvent(userId, sessionId, "agent.error", {
      code: "CHEM_MCP_FAILED",
      message: "The local chemistry tool process stopped unexpectedly.",
    }, [active.apiKey, error.message]);
    await this.sealActiveSession(sessionId, active, true);
  }

  private persistUserMessage(
    userId: string,
    sessionId: string,
    apiKey: string,
    text: string,
    behavior: "prompt" | "steer" | "follow_up",
  ): void {
    this.store.appendEvent(userId, sessionId, "agent.event", {
      phase: "conversation.user_message",
      behavior,
      text,
    }, [apiKey]);
  }

  private persistPiEvent(userId: string, sessionId: string, apiKey: string, event: unknown): void {
    try {
      const active = this.active.get(sessionId);
      const persistedSession = this.store.getSession(userId, sessionId);
      if (active?.cancelling) return;
      if (["review_ready", "partial", "failed", "aborted", "cancelled", "interrupted"]
        .includes(persistedSession.status)) return;
      const textDelta = piAssistantTextDelta(event);
      if (active && textDelta) {
        this.flushPiToolArgumentProgress(userId, sessionId, active);
        active.streamDeltas.push(textDelta);
        active.streamDeltaChars += textDelta.length;
        if (active.streamDeltas.length >= 32 || active.streamDeltaChars >= 4_096) {
          this.flushPiTextDeltas(userId, sessionId, active);
        }
        return;
      }
      const toolArgumentDeltaChars = piToolArgumentDeltaChars(event);
      if (active && toolArgumentDeltaChars !== undefined) {
        this.flushPiTextDeltas(userId, sessionId, active);
        active.toolArgumentDeltaChunks += 1;
        active.toolArgumentDeltaChars += toolArgumentDeltaChars;
        if (active.toolArgumentDeltaChunks >= 1_024 || active.toolArgumentDeltaChars >= 65_536) {
          this.flushPiToolArgumentProgress(userId, sessionId, active);
        }
        return;
      }
      if (active) {
        this.flushPiTextDeltas(userId, sessionId, active);
        this.flushPiToolArgumentProgress(userId, sessionId, active);
      }
      const projected = projectPiEvent(event);
      // Scan the bounded bytes that can actually enter the durable lane. Pi's
      // aggregate start/end events repeat the whole internal transcript,
      // including skill documentation and image/tool payloads already covered
      // by individual persisted events. Mount allowlists remain the security
      // boundary; this check remains a sentinel guard on persisted payloads.
      assertNoGroundTruth(projected);
      this.store.appendEvent(userId, sessionId, "agent.event", projected, [apiKey]);
      const messageError = piMessageError(event);
      if (active) {
        captureToolEvent(active, event);
        if (messageError) active.lastPiError = messageError;
        else if (isSuccessfulAssistantMessage(event)) delete active.lastPiError;
      }
      if (projected.phase === "pi.agent_end") {
        const session = this.store.getSession(userId, sessionId);
        if (active?.lastPiError) return;
        if (active?.inputs.length) {
          void this.finalizeLiveResult(userId, sessionId, active);
        } else if (session.status === "running") {
          this.markReadyForMore(
            userId,
            sessionId,
            "turn_complete",
            "Pi turn completed; the conversation is ready for another prompt.",
          );
        }
      }
      if (projected.phase === "pi.agent_settled" && active?.lastPiError) {
        void this.failSettledPiSession(userId, sessionId, active);
      }
    } catch (error) {
      if (!(error instanceof RuntimePolicyError)) throw error;
      this.store.appendEvent(userId, sessionId, "agent.error", {
        code: "PI_EVENT_REJECTED",
        message: "A Pi event was rejected by the durable event policy",
      });
    }
  }

  private flushPiTextDeltas(
    userId: string,
    sessionId: string,
    active: ActiveAgent,
  ): void {
    if (!active.streamDeltas.length) return;
    const delta = active.streamDeltas.join("");
    const batchedChunks = active.streamDeltas.length;
    active.streamDeltas = [];
    active.streamDeltaChars = 0;
    const event = {
      type: "message_update",
      assistantMessageEvent: { type: "text_delta", delta },
      batchedChunks,
    };
    assertNoGroundTruth(event);
    this.store.appendEvent(userId, sessionId, "agent.event", {
      phase: "pi.message_update",
      event,
    }, active.apiKey ? [active.apiKey] : []);
  }

  private async failSettledPiSession(
    userId: string,
    sessionId: string,
    active: ActiveAgent,
  ): Promise<void> {
    if (this.active.get(sessionId) !== active) return;
    const session = this.store.getSession(userId, sessionId);
    session.status = "failed";
    session.blockReason = "MODEL_REQUEST_FAILED";
    session.updatedAt = new Date().toISOString();
    this.store.putSession(session);
    this.store.appendEvent(userId, sessionId, "agent.error", {
      code: "MODEL_REQUEST_FAILED",
      message: active.lastPiError === "fetch failed"
        ? "The model request could not reach the provider after Pi retries."
        : "The model request failed after Pi retries.",
      retryable: true,
    }, active.apiKey ? [active.apiKey] : []);
    await this.sealActiveSession(sessionId, active, true);
  }

  /**
   * Close an activated Pi/MCP pair when input preparation cannot start a turn.
   *
   * Activation and extraction preparation are deliberately separate so a
   * durable queue can claim work before starting heavyweight processes. Once
   * activation succeeds, however, every synchronous preparation failure must
   * terminate those processes and discard the one-run credential lease. The
   * dispatcher may then mark its work item from this terminal session without
   * leaving a hidden active agent behind.
   */
  private async failExtractionStart(
    userId: string,
    sessionId: string,
    active: ActiveAgent,
    error: unknown,
    signal?: AbortSignal,
  ): Promise<void> {
    if (this.active.get(sessionId) !== active) return;

    // The HTTP cancel or shutdown path may already own settlement. Waiting for
    // that single writer avoids stale SQLite revisions and duplicate terminal
    // events while the abort signal simultaneously unwinds input preparation.
    if (active.cancelling) {
      await this.waitForActiveSettlement(sessionId, active);
      return;
    }

    if (signal?.aborted && signal.reason === "runtime_shutdown") {
      await this.interruptForShutdown(userId, sessionId);
      return;
    }
    active.cancelling = true;

    const session = this.store.getSession(userId, sessionId);
    if (signal?.aborted) {
      await boundedOperation(
        Promise.resolve().then(() => active.pi.session.abort()),
        this.options.abortTimeoutMs ?? 1_000,
      );
      session.status = "cancelled";
      delete session.blockReason;
      session.updatedAt = new Date().toISOString();
      this.store.putSession(session);
      this.store.appendEvent(userId, sessionId, "job.cancelled", {
        phase: "job.cancelled",
        source: "queue",
        duringPreparation: true,
        retryable: true,
        message: "Extraction cancelled while preparing the selected input.",
      }, active.apiKey ? [active.apiKey] : []);
      await this.sealActiveSession(sessionId, active, false);
      return;
    }

    const code = error instanceof RuntimePolicyError
      ? error.code : "EXTRACTION_START_FAILED";
    session.status = "failed";
    session.blockReason = code;
    session.updatedAt = new Date().toISOString();
    this.store.putSession(session);
    this.store.appendEvent(userId, sessionId, "agent.error", {
      code,
      phase: "extraction.preparation_failed",
      message: error instanceof RuntimePolicyError
        ? error.message
        : "Extraction setup failed before the model turn could start.",
      retryable: true,
    }, active.apiKey ? [active.apiKey] : []);
    await this.sealActiveSession(sessionId, active, false);
  }

  private async waitForActiveSettlement(
    sessionId: string,
    active: ActiveAgent,
  ): Promise<void> {
    const settling = this.settlements.get(sessionId);
    if (settling) {
      await settling;
      return;
    }
    const deadline = Date.now() + 5_000;
    while (this.active.get(sessionId) === active && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
  }

  private async finalizeLiveResult(
    userId: string,
    sessionId: string,
    active: ActiveAgent,
  ): Promise<void> {
    if (active.finalizing) return;
    active.finalizing = true;
    try {
      const figure = active.inputs.find((input) => input.kind === "figure");
      if (!figure) return;
      const session = this.store.getSession(userId, sessionId);
      const result = await buildLiveFigureResult({
        sessionId,
        jobId: session.jobId,
        workspaceId: active.workspaceId,
        workspace: active.workspace,
        figure,
        provider: active.provider,
        modelId: active.modelId,
        skills: active.skills,
        tools: active.toolCaptures,
      });
      if (!result) {
        this.store.appendEvent(userId, sessionId, "agent.event", {
          phase: "extraction.awaiting_artifact",
          message: "Pi turn ended without a workspace-local assembled V2 artifact. Continue the conversation or retry extraction.",
          requiredPath: "output/result.v2.json",
        });
        this.markReadyForMore(userId, sessionId);
        return;
      }
      const manifest = result.manifest as { artifacts?: Array<{ kind?: string; sha256?: string }> };
      const predictionSha = manifest.artifacts?.find((item) => item.kind === "prediction_v2")?.sha256;
      if (predictionSha && predictionSha === active.lastPredictionSha) return;
      if (predictionSha) active.lastPredictionSha = predictionSha;
      if (active.protocolPrompt) {
        this.persistProtocolFingerprint(
          userId,
          session,
          active,
          active.protocolPrompt,
          "sealed",
          "resolved_after_model",
          perceptionArtifactsFromResult(result),
        );
      }
      await this.publishLiveResult(
        userId,
        sessionId,
        active,
        result,
        result.terminalStatus === "review_ready" ? "job.review_ready" : "job.partial",
        {
          phase: result.terminalStatus === "review_ready" ? "job.review_ready" : "job.partial",
          message: result.terminalStatus === "review_ready"
            ? "Live Figure extraction artifacts are ready for review."
            : "A live partial Figure extraction is ready for review.",
          domainContract: result.domainContract,
          resultSource: "live_pi_sdk",
          counts: (result.manifest as { counts?: unknown }).counts,
          viewer: result.viewer,
        },
      );
    } catch (error) {
      if (error instanceof FigureSealBlockedError) {
        this.store.appendEvent(userId, sessionId, "agent.event", {
          phase: "extraction.closure_required",
          message: "The Figure candidate is not sealable yet. Reconcile the listed verification/provenance issues, then assemble and render again.",
          issues: error.issues,
          closure: error.audit,
        });
        this.markReadyForMore(
          userId,
          sessionId,
          "closure_required",
          "Sealing was blocked until verification, drawn-source lineage, assembly, and rendering are closed.",
        );
        return;
      }
      const session = this.store.getSession(userId, sessionId);
      session.status = "failed";
      session.blockReason = "LIVE_RESULT_MATERIALIZATION_FAILED";
      session.updatedAt = new Date().toISOString();
      this.store.putSession(session);
      this.store.appendEvent(userId, sessionId, "agent.error", {
        code: "LIVE_RESULT_MATERIALIZATION_FAILED",
        message: error instanceof Error ? error.message : "Live result materialization failed",
      }, [active.apiKey]);
      await this.sealActiveSession(sessionId, active, true);
    } finally {
      active.finalizing = false;
    }
  }

  private async publishLiveResult(
    userId: string,
    sessionId: string,
    active: ActiveAgent,
    result: RuntimeJobResult,
    eventType: Extract<RuntimeEvent["type"], "job.review_ready" | "job.partial">,
    eventData: Record<string, unknown>,
  ): Promise<void> {
    try {
      this.store.putResult(userId, sessionId, result);
    } catch (error) {
      // A file-backed store can publish result.json successfully and then
      // crash before indexing it. Accept only the exact immutable payload;
      // a different existing result remains RESULT_ALREADY_SEALED.
      let existing: RuntimeJobResult;
      try {
        existing = this.store.getResult(userId, sessionId);
      } catch {
        throw error;
      }
      if (JSON.stringify(existing) !== JSON.stringify(result)) throw error;
    }

    const projectTerminal = (): void => {
      const session = this.store.getSession(userId, sessionId);
      if (session.status !== result.terminalStatus || session.blockReason) {
        session.status = result.terminalStatus;
        delete session.blockReason;
        session.updatedAt = new Date().toISOString();
        this.store.putSession(session);
      }
      const alreadyProjected = this.store.listEvents(userId, sessionId)
        .some((event) => event.type === eventType);
      if (!alreadyProjected) {
        this.store.appendEvent(userId, sessionId, eventType, eventData);
      }
    };

    try {
      projectTerminal();
    } catch {
      // getResult() repairs the SQLite mirror when result publication won the
      // crash race. Retry presentation once, then leave immutable result truth
      // for startup reconciliation rather than fabricating a failed outcome.
      try {
        this.store.getResult(userId, sessionId);
        projectTerminal();
      } catch {
        // Result remains sealed and recoverable; do not overwrite its outcome.
      }
    }
    await this.sealActiveSession(sessionId, active, true);
  }

  private markReadyForMore(
    userId: string,
    sessionId: string,
    reason = "awaiting_artifact",
    message = "Pi turn completed; the conversation is ready for another prompt.",
  ): void {
    const session = this.store.getSession(userId, sessionId);
    if (session.status !== "running") return;
    session.status = "ready";
    session.updatedAt = new Date().toISOString();
    this.store.putSession(session);
    // Session metadata alone is not enough for a browser that reconstructs
    // state from the append-only cursor ledger. Publish the transition after
    // an assistant turn that did not yet produce a sealable artifact so the
    // composer becomes usable again without polling private runtime state.
    this.store.appendEvent(userId, sessionId, "session.ready", {
      source: "pi_sdk",
      reason,
      message,
    });
  }

  private continuationRootSessionId(userId: string, sessionId: string): string {
    const seen = new Set<string>();
    let current = this.store.getSession(userId, sessionId);
    while (current.parentSessionId && !seen.has(current.id)) {
      seen.add(current.id);
      const parent = this.store.getSession(userId, current.parentSessionId);
      const events = this.store.listEvents(userId, current.id);
      const created = events.find((event) => event.type === "session.created");
      const data = created?.data && typeof created.data === "object"
        ? created.data as Record<string, unknown> : {};
      // A retry owns a future result/workspace of its own. Only linked
      // continuation tasks should keep controlling their sealed ancestor's
      // visual workspace.
      if (data.retry) break;
      current = parent;
    }
    return current.id;
  }

  private async sealActiveSession(
    sessionId: string,
    active: ActiveAgent,
    preserveApiKey = false,
  ): Promise<void> {
    const inFlight = this.settlements.get(sessionId);
    if (inFlight) {
      await inFlight;
      return;
    }
    if (this.active.get(sessionId) !== active) return;
    if (preserveApiKey && active.apiKey) {
      this.clearSealedApiKeyLease(sessionId);
      const lease: {
        value: string;
        expiresAt: number;
        timer?: ReturnType<typeof setTimeout>;
      } = {
        value: active.apiKey,
        expiresAt: Date.now() + (this.options.credentialLeaseTtlMs ?? 10 * 60_000),
      };
      lease.timer = setTimeout(() => {
        if (this.sealedApiKeyLeases.get(sessionId) === lease) {
          this.sealedApiKeyLeases.delete(sessionId);
        }
      }, this.options.credentialLeaseTtlMs ?? 10 * 60_000);
      lease.timer.unref();
      this.sealedApiKeyLeases.set(sessionId, lease);
    }
    // Detach the terminal session before cleanup so a continuation can only
    // consume the single-use sealed credential lease. Queue capacity remains
    // held by the settlement Promise until MCP/Pi disposal completes.
    this.active.delete(sessionId);
    let settlement!: Promise<void>;
    settlement = Promise.allSettled([
      boundedOperation(active.mcp.terminate(), this.options.cleanupTimeoutMs ?? 2_500),
      boundedOperation(active.pi.dispose(), this.options.cleanupTimeoutMs ?? 2_500),
    ])
      .then(() => {
        // Resources are now fully settled; the active handle was detached at
        // seal start to prevent credential reuse during this cleanup window.
      })
      .finally(() => {
        if (this.settlements.get(sessionId) === settlement) {
          this.settlements.delete(sessionId);
        }
      });
    this.settlements.set(sessionId, settlement);
    await settlement;
  }

  private takeSealedApiKeyLease(sessionId: string): string | undefined {
    const lease = this.sealedApiKeyLeases.get(sessionId);
    this.clearSealedApiKeyLease(sessionId);
    if (!lease || lease.expiresAt <= Date.now()) return undefined;
    return lease.value;
  }

  private clearSealedApiKeyLease(sessionId: string): void {
    const lease = this.sealedApiKeyLeases.get(sessionId);
    if (!lease) return;
    if (lease.timer) clearTimeout(lease.timer);
    this.sealedApiKeyLeases.delete(sessionId);
  }

  private persistProtocolFingerprint(
    userId: string,
    session: SessionRecord,
    active: ActiveAgent,
    prompt: string,
    stage: "dispatch" | "sealed",
    perceptionStatus:
      | "deferred_to_canonical_tool"
      | "resolved_before_model"
      | "resolved_after_model",
    perception = active.protocolPerception,
  ): void {
    const protocolFiles = this.requireProtocolIntegrity();
    const fingerprint = buildProtocolFingerprint({
      stage,
      task: session.task,
      contextMode: active.contextMode,
      inputs: active.inputs.map((input) => ({ kind: input.kind, sha256: input.sha256 })),
      perceptionStatus,
      perception,
      skills: active.skills,
      protocolFiles,
      prompt,
      toolContract: active.protocolToolContract,
      provider: active.provider,
      modelId: active.modelId,
      thinkingLevel: active.thinkingLevel,
    });
    active.protocolPrompt = prompt;
    this.store.appendEvent(userId, session.id, "agent.event", {
      phase: "protocol.fingerprint",
      fingerprint,
    }, active.apiKey ? [active.apiKey] : []);
  }

  private requireProtocolIntegrity(): ProtocolFileIdentity[] {
    try {
      return this.protocolIntegrity.verify();
    } catch {
      throw new RuntimePolicyError(
        "PROTOCOL_INTEGRITY_FAILED",
        "ChemRxn's pinned extraction protocol changed or is unavailable. Restart with a verified repository revision before running Pi.",
        503,
      );
    }
  }

  private flushPiToolArgumentProgress(
    userId: string,
    sessionId: string,
    active: ActiveAgent,
  ): void {
    if (!active.toolArgumentDeltaChunks) return;
    const batchedChunks = active.toolArgumentDeltaChunks;
    const byteLength = active.toolArgumentDeltaChars;
    active.toolArgumentDeltaChunks = 0;
    active.toolArgumentDeltaChars = 0;
    this.store.appendEvent(userId, sessionId, "agent.event", {
      phase: "pi.tool_argument_progress",
      event: {
        type: "message_update",
        assistantMessageEvent: {
          type: "toolcall_delta",
          redacted: true,
        },
        batchedChunks,
        byteLength,
      },
    }, active.apiKey ? [active.apiKey] : []);
  }

  private failPrompt(
    userId: string,
    sessionId: string,
    active: ActiveAgent,
    error: unknown,
  ): void {
    if (active.cancelling) return;
    const latest = this.store.getSession(userId, sessionId);
    if (["blocked", "failed", "aborted", "review_ready", "partial", "cancelled", "interrupted"]
      .includes(latest.status)) return;
    latest.status = "failed";
    latest.blockReason = "PI_PROMPT_FAILED";
    latest.updatedAt = new Date().toISOString();
    this.store.putSession(latest);
    this.store.appendEvent(userId, sessionId, "agent.error", {
      code: "PI_PROMPT_FAILED",
      message: error instanceof Error ? error.message : "Pi prompt failed",
    }, [active.apiKey]);
    void this.sealActiveSession(sessionId, active, true);
  }
}

export function projectPiEvent(event: unknown): Record<string, unknown> {
  if (!event || typeof event !== "object") return { phase: "pi.unknown" };
  const record = event as Record<string, unknown>;
  const type = typeof record.type === "string" ? record.type : "unknown";
  const safeRecord = redactPiToolArguments(record, type);
  const encoded = JSON.stringify(safeRecord);
  const aggregateControlEvent = type === "agent_start" || type === "agent_end";
  return {
    phase: `pi.${type}`,
    event: !aggregateControlEvent && encoded.length <= 250_000
      ? structuredClone(safeRecord)
      : compactPiEvent(safeRecord, type, Buffer.byteLength(encoded)),
  };
}

function redactPiToolArguments(
  record: Record<string, unknown>,
  type: string,
): Record<string, unknown> {
  if (type !== "tool_execution_start") return record;
  const { args: discarded, ...safe } = record;
  const toolName = typeof record.toolName === "string" ? record.toolName : "";
  const observableInputs = safeObservableToolInputs(toolName, discarded);
  return {
    ...safe,
    argumentsRedacted: Object.prototype.hasOwnProperty.call(record, "args"),
    ...(observableInputs ? { observableInputs } : {}),
  };
}

function safeObservableToolInputs(toolName: string, value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const args = value as Record<string, unknown>;
  const text = (key: string, limit = 4000): string | undefined =>
    typeof args[key] === "string" ? (args[key] as string).slice(0, limit) : undefined;
  const integer = (key: string): number | undefined =>
    Number.isInteger(args[key]) ? args[key] as number : undefined;
  const keep = (input: Record<string, unknown>): Record<string, unknown> | null => {
    const filtered = Object.fromEntries(Object.entries(input).filter(([, item]) => item !== undefined));
    return Object.keys(filtered).length ? filtered : null;
  };
  if (toolName === "read_job_file") {
    return keep({ path: text("path", 500), offset: integer("offset"), limit: integer("limit") });
  }
  if (toolName === "verify_smiles") {
    return keep({ smiles: text("smiles"), require_concrete: args.require_concrete === true || undefined });
  }
  if (toolName === "reverse_rgroup") {
    return keep({ template: text("template"), concrete: text("concrete") });
  }
  if (toolName === "fill_rgroup") {
    return keep({ template: text("template"), r_map: boundedChemicalValue(args.r_map) });
  }
  if (toolName === "reverse_assemble") {
    return keep({
      target_product: text("target_product"),
      product_template: text("product_template"),
      reactant_templates: boundedChemicalValue(args.reactant_templates),
    });
  }
  if (toolName === "assemble") {
    const plan = args.plan && typeof args.plan === "object" ? args.plan as Record<string, unknown> : {};
    return keep({
      plan_path: text("plan_path", 500),
      out_path: text("out_path", 500),
      example_count: Array.isArray(plan.selected_examples) ? plan.selected_examples.length : undefined,
      template_count: Array.isArray(plan.reaction_templates) ? plan.reaction_templates.length : undefined,
    });
  }
  if (toolName === "render_reactions") {
    return keep({
      v2_path: text("v2_path", 500), image_path: text("image_path", 500), out_html: text("out_html", 500),
    });
  }
  if (toolName === "detect" || toolName === "ocsr_detect") {
    return keep({ image_path: text("image_path", 500), out_dir: text("out_dir", 500) });
  }
  return null;
}

function boundedChemicalValue(value: unknown, depth = 0): unknown {
  if (typeof value === "string") return value.slice(0, 4000);
  if (typeof value === "number" || typeof value === "boolean" || value === null) return value;
  if (depth >= 2) return "[nested input omitted]";
  if (Array.isArray(value)) return value.slice(0, 50).map((item) => boundedChemicalValue(item, depth + 1));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).slice(0, 50)
      .filter(([key]) => !/key|token|secret|password|credential/i.test(key))
      .map(([key, item]) => [key.slice(0, 160), boundedChemicalValue(item, depth + 1)]));
  }
  return undefined;
}

function compactPiEvent(
  record: Record<string, unknown>,
  type: string,
  byteLength: number,
): Record<string, unknown> {
  const compact: Record<string, unknown> = { type, truncated: true, byteLength };
  if (type === "message_update") {
    const update = record.assistantMessageEvent;
    if (update && typeof update === "object") {
      const event = update as Record<string, unknown>;
      compact.assistantMessageEvent = {
        type: event.type,
        ...(event.type === "text_delta" && typeof event.delta === "string"
          ? { delta: `${event.delta.slice(0, 32_000)}\n[stream chunk truncated]` } : {}),
      };
    }
  } else if (type === "message_end") {
    const message = record.message;
    if (message && typeof message === "object") {
      const typed = message as Record<string, unknown>;
      const text = extractMessageText(typed);
      compact.message = {
        role: typed.role,
        content: text
          ? [{ type: "text", text: `${text.slice(0, 64_000)}\n[message truncated]` }]
          : [],
      };
    }
  } else if (type === "tool_execution_start" || type === "tool_execution_end") {
    compact.toolCallId = record.toolCallId;
    compact.toolName = record.toolName;
    if (type === "tool_execution_end") {
      const result = record.result;
      compact.isError = record.isError === true
        || Boolean(result && typeof result === "object"
          && (result as Record<string, unknown>).isError === true);
    }
  } else if (type === "queue_update") {
    compact.steeringCount = Array.isArray(record.steering) ? record.steering.length : 0;
    compact.followUpCount = Array.isArray(record.followUp) ? record.followUp.length : 0;
  }
  return compact;
}

function extractMessageText(message: Record<string, unknown>): string {
  if (typeof message.content === "string") return message.content;
  if (!Array.isArray(message.content)) return "";
  return message.content
    .filter((part): part is { type: string; text: string } =>
      Boolean(part && typeof part === "object"
        && (part as Record<string, unknown>).type === "text"
        && typeof (part as Record<string, unknown>).text === "string"))
    .map((part) => part.text)
    .join("");
}

function piMessageError(event: unknown): string | undefined {
  if (!event || typeof event !== "object") return undefined;
  const record = event as Record<string, unknown>;
  if (record.type !== "message_end") return undefined;
  const message = record.message;
  if (!message || typeof message !== "object") return undefined;
  const typed = message as Record<string, unknown>;
  if (typed.role !== "assistant" || typed.stopReason !== "error") return undefined;
  return typeof typed.errorMessage === "string" && typed.errorMessage
    ? typed.errorMessage : "model request failed";
}

function piAssistantTextDelta(event: unknown): string | undefined {
  if (!event || typeof event !== "object") return undefined;
  const record = event as Record<string, unknown>;
  if (record.type !== "message_update") return undefined;
  const update = record.assistantMessageEvent;
  if (!update || typeof update !== "object") return undefined;
  const typed = update as Record<string, unknown>;
  return typed.type === "text_delta" && typeof typed.delta === "string"
    ? typed.delta : undefined;
}

function piToolArgumentDeltaChars(event: unknown): number | undefined {
  if (!event || typeof event !== "object") return undefined;
  const record = event as Record<string, unknown>;
  if (record.type !== "message_update") return undefined;
  const update = record.assistantMessageEvent;
  if (!update || typeof update !== "object") return undefined;
  const typed = update as Record<string, unknown>;
  if (typed.type !== "toolcall_delta") return undefined;
  return typeof typed.delta === "string" ? typed.delta.length : 0;
}

function isSuccessfulAssistantMessage(event: unknown): boolean {
  if (!event || typeof event !== "object") return false;
  const record = event as Record<string, unknown>;
  if (record.type !== "message_end") return false;
  const message = record.message;
  if (!message || typeof message !== "object") return false;
  const typed = message as Record<string, unknown>;
  return typed.role === "assistant" && typed.stopReason !== "error";
}

export function recoveredPinnedToolNames(
  events: RuntimeEvent[],
  availableTools: readonly string[],
): string[] {
  const ready = [...events].reverse().find((event) => {
    if (event.type !== "session.ready" || !event.data || typeof event.data !== "object") {
      return false;
    }
    return Array.isArray((event.data as Record<string, unknown>).tools);
  });
  const data = ready?.data && typeof ready.data === "object"
    ? ready.data as Record<string, unknown> : {};
  const recorded = Array.isArray(data.tools)
    ? data.tools.filter((value): value is string => typeof value === "string") : [];
  if (!recorded.length) return [...availableTools];
  const unavailable = recorded.filter((name) => !availableTools.includes(name));
  if (unavailable.length) {
    throw new Error(`Pinned Pi tool is no longer available: ${unavailable.join(", ")}`);
  }
  return recorded;
}

function assertPinnedRecovery(
  events: RuntimeEvent[],
  skills: SkillManifestEntry[],
  tools: readonly string[],
): void {
  const ready = [...events].reverse().find((event) => {
    if (event.type !== "session.ready" || !event.data || typeof event.data !== "object") {
      return false;
    }
    return (event.data as Record<string, unknown>).source === "pi_sdk";
  });
  const data = ready?.data && typeof ready.data === "object"
    ? ready.data as Record<string, unknown> : {};
  const expected = Array.isArray(data.skillHashes)
    ? data.skillHashes.flatMap((value) => {
        if (!value || typeof value !== "object") return [];
        const record = value as Record<string, unknown>;
        return typeof record.name === "string" && typeof record.sha256 === "string"
          ? [`${record.name}:${record.sha256}`] : [];
      }).sort()
    : [];
  const actual = skills.map((skill) => `${skill.name}:${skill.sha256}`).sort();
  if (!expected.length || JSON.stringify(expected) !== JSON.stringify(actual)) {
    throw new Error("Pinned Pi skill hashes changed since this conversation was created");
  }
  const expectedTools = Array.isArray(data.tools)
    ? data.tools.filter((value): value is string => typeof value === "string").sort()
    : [];
  const actualTools = [...tools].sort();
  if (!expectedTools.length || JSON.stringify(expectedTools) !== JSON.stringify(actualTools)) {
    throw new Error("Pinned Pi tool allowlist changed since this conversation was created");
  }
}

export function interruptOrphanedLocalSessions(
  store: RuntimeStore,
  options: { canRehydrate?: boolean; excludeSessionIds?: ReadonlySet<string> } = {},
): number {
  let reconciled = 0;
  for (const session of store.listAllSessions()) {
    if (options.excludeSessionIds?.has(session.id)) continue;
    if (!["creating", "ready", "running"].includes(session.status)) continue;
    const events = store.listEvents(session.userId, session.id);
    const created = events.find((event) => event.type === "session.created");
    const data = created?.data && typeof created.data === "object"
      ? created.data as Record<string, unknown> : {};
    const source = data.source && typeof data.source === "object"
      ? data.source as Record<string, unknown> : {};
    if (source.mode !== "live_pi_sdk") continue;
    if (session.status === "ready" && options.canRehydrate !== false) {
      session.status = "dormant";
      session.blockReason = "LOCAL_RUNTIME_REHYDRATION_REQUIRED";
      session.updatedAt = new Date().toISOString();
      store.putSession(session);
      store.appendEvent(session.userId, session.id, "session.dormant", {
        code: "LOCAL_RUNTIME_REHYDRATION_REQUIRED",
        message: session.authMode === "personal_codex_subscription"
          ? "The local runtime restarted. The next message will reopen this Pi conversation."
          : "The local runtime restarted. Re-enter the API key to reopen this Pi conversation.",
        resumable: true,
        credentialRequired: session.authMode === "personal_codex_subscription"
          ? "pi_auth" : "api_key",
      });
      reconciled += 1;
      continue;
    }
    session.status = "interrupted";
    session.blockReason = "LOCAL_RUNTIME_RESTART";
    session.updatedAt = new Date().toISOString();
    store.putSession(session);
    store.appendEvent(session.userId, session.id, "job.interrupted", {
      code: "LOCAL_RUNTIME_RESTART",
      message: session.authMode === "personal_codex_subscription"
        ? "The local Pi runtime restarted. Retry creates a new isolated attempt using Pi auth."
        : "The local Pi runtime restarted. Retry creates a new isolated attempt after you supply the API key again.",
      resumable: true,
      retryCreatesNewAttempt: true,
      credentialRequired: session.authMode === "personal_codex_subscription"
        ? "pi_auth" : "api_key",
    });
    reconciled += 1;
  }
  return reconciled;
}

async function abortableResource<T>(
  pending: Promise<T>,
  signal: AbortSignal | undefined,
  dispose: (resource: T) => Promise<unknown>,
): Promise<T> {
  if (!signal) return pending;
  if (signal.aborted) {
    try {
      const resource = await pending;
      await dispose(resource);
    } catch {
      // The requested operation may have failed before producing a resource.
    }
    signal.throwIfAborted();
  }
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const onAbort = (): void => {
      if (settled) return;
      settled = true;
      // A subprocess/session constructor can resolve after cancellation. Do
      // not report activation settled (and therefore release the queue slot)
      // until that late handle has been explicitly terminated/disposed.
      void pending.then(
        async (resource) => { await dispose(resource); },
        () => undefined,
      ).catch(() => undefined).finally(() => {
        reject(abortReason(signal));
      });
    };
    signal.addEventListener("abort", onAbort, { once: true });
    void pending.then((resource) => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", onAbort);
      resolve(resource);
    }, (error) => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", onAbort);
      reject(error);
    });
  });
}

async function abortableValue<T>(
  pending: Promise<T>,
  signal: AbortSignal | undefined,
): Promise<T> {
  if (!signal) return pending;
  if (signal.aborted) signal.throwIfAborted();
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const onAbort = (): void => {
      if (settled) return;
      settled = true;
      // Values such as tool-definition arrays own no external process. Their
      // late resolution can be ignored after the already-created MCP handle is
      // terminated by the activation catch path.
      void pending.catch(() => undefined);
      reject(abortReason(signal));
    };
    signal.addEventListener("abort", onAbort, { once: true });
    void pending.then((value) => {
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

function abortReason(signal: AbortSignal): Error {
  return signal.reason instanceof Error
    ? signal.reason
    : new Error(typeof signal.reason === "string" ? signal.reason : "Operation aborted");
}

async function boundedOperation(
  operation: Promise<unknown>,
  timeoutMs: number,
): Promise<{ settled: boolean }> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      operation.then(
        () => ({ settled: true }),
        () => ({ settled: false }),
      ),
      new Promise<{ settled: false }>((resolve) => {
        timer = setTimeout(() => resolve({ settled: false }), timeoutMs);
        // This timeout is the durability boundary for cancellation and
        // shutdown. Keep it referenced so terminal state is persisted even
        // when the hung provider operation is the last remaining handle.
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function safeChild(root: string, value: string): string {
  if (!/^[A-Za-z0-9._-]{1,128}$/.test(value)) {
    throw new RuntimePolicyError("INVALID_CONTEXT", "Invalid local workspace identity");
  }
  return path.join(path.resolve(root), value);
}

function safeIdentity(value: string): string {
  return createHash("sha256").update(value).digest("hex").slice(0, 32);
}

function continuationContext(value: unknown): ContextMode {
  if (value === "figure") return value;
  throw new RuntimePolicyError(
    "INVALID_CONTEXT",
    "The parent session has no reusable context mode.",
    409,
  );
}

function persistedInputSelections(
  data: Record<string, unknown>,
  allowEmpty = false,
): LocalInputSelection[] {
  if (Array.isArray(data.inputSelections)) {
    const selections = data.inputSelections.map((value) => {
      const item = value && typeof value === "object"
        ? value as Record<string, unknown> : {};
      if (
        typeof item.artifactId !== "string"
        || !["figure"].includes(String(item.kind))
      ) {
        throw new RuntimePolicyError(
          "INVALID_CONTEXT",
          "The prepared session contains an invalid input selection.",
          409,
        );
      }
      return {
        artifactId: item.artifactId,
        kind: item.kind as LocalInputSelection["kind"],
      };
    });
    if (selections.length > 0 || allowEmpty) return selections;
  }
  throw new RuntimePolicyError(
    "INPUT_REQUIRED",
    "The prepared session has no restart-safe input selection.",
    409,
  );
}

function requiredContinuationString(value: unknown, label: string): string {
  if (typeof value === "string" && value.trim()) return value;
  throw new RuntimePolicyError(
    "INVALID_CONTEXT",
    `The parent session has no reusable ${label}.`,
    409,
  );
}

function persistedThinkingLevel(value: unknown): PiThinkingLevel {
  if (
    value === "off"
    || value === "minimal"
    || value === "low"
    || value === "medium"
    || value === "high"
    || value === "xhigh"
    || value === "max"
  ) return value;
  // Sessions created before explicit thinking provenance used Pi SDK's medium
  // default. Record that compatibility value instead of silently resolving to
  // off or inheriting a mutable user setting.
  return DEFAULT_EXTRACTION_THINKING_LEVEL;
}

function addProtocolArtifact(
  target: ProtocolArtifactIdentity[],
  artifact: ProtocolArtifactIdentity,
): void {
  if (!target.some((item) => item.kind === artifact.kind && item.sha256 === artifact.sha256)) {
    target.push(artifact);
  }
}

function perceptionArtifactsFromResult(
  result: RuntimeJobResult,
  fallback: readonly ProtocolArtifactIdentity[] = [],
): ProtocolArtifactIdentity[] {
  const manifest = result.manifest && typeof result.manifest === "object"
    ? result.manifest as Record<string, unknown> : {};
  const artifacts = Array.isArray(manifest.artifacts) ? manifest.artifacts : [];
  const resolved = artifacts.flatMap((value): ProtocolArtifactIdentity[] => {
    if (!value || typeof value !== "object") return [];
    const item = value as Record<string, unknown>;
    const kind = typeof item.kind === "string" ? item.kind : "";
    const sha256 = typeof item.sha256 === "string" ? item.sha256 : "";
    if (!/molparser|perception|layout|page_render/i.test(kind) || !/^[a-f0-9]{64}$/.test(sha256)) {
      return [];
    }
    return [{ kind, sha256 }];
  });
  for (const artifact of fallback) addProtocolArtifact(resolved, artifact);
  return resolved;
}

function figureExtractionPrompt(
  workspacePath: string,
  userDirection?: string,
  task: TaskKind = "extract_all",
): string {
  const taskContract = task === "inspect_selection"
    ? "\nThis is a linked inspection task. Re-read the immutable figure for the user's requested scope and produce a new scoped result; do not modify or claim to modify the sealed parent result."
    : "";
  const direction = userDirection?.trim()
    ? `\n\nAdditional user direction:\n${userDirection.trim()}`
    : "";
  return `/skill:reaction-extract

Extract every reaction from the attached reaction figure. The immutable original is also available at
\`${workspacePath}\` in the current workspace.

Required durable outputs:
1. Call \`detect\` first with \`image_path="${workspacePath}"\` and
   \`out_dir="output/perception"\`. Inspect both returned images.
2. Follow the loaded reaction-extract skill completely, including strict-stereo checks and complete
   reactant/product pairs.
3. Finish every source-grounded \`verify_smiles\` decision before the final assembly. If any
   \`verify_smiles\` call occurs after an assembly, that candidate is stale: update the plan and call
   \`assemble\` again with \`out_path="output/result.v2.json"\`.
4. Preserve \`bbox_index\`, the exact raw \`molparser_md5\`, and MolParser E-SMILES on every directly
   drawn molecule. A human-readable identifier such as “from bbox 11” is not provenance.
5. After the final verification decision, call \`assemble\` with the full reconciled plan and then
   call \`render_reactions\` with \`v2_path="output/result.v2.json"\`,
   \`image_path="${workspacePath}"\`, and \`out_html="output/reactions.html"\`. The final render must
   occur after the final assembly.
6. Do not claim completion until both workspace-local output files exist and the closure gate passes.
   Never use GT, a prior prediction, or an adjudication file.${taskContract}${direction}`;
}

export function runtimeReviewerJobId(
  contextMode: ContextMode,
  workspaceTargetSessionId: string,
): string {
  const digest = createHash("sha256")
    .update(workspaceTargetSessionId)
    .digest("hex")
    .slice(0, 12);
  return `runtime_${digest}`;
}

function captureToolEvent(active: ActiveAgent, value: unknown): void {
  if (!value || typeof value !== "object") return;
  const event = value as Record<string, unknown>;
  if (event.type !== "tool_execution_end" || typeof event.toolName !== "string") return;
  const result = event.result && typeof event.result === "object"
    ? event.result as Record<string, unknown> : {};
  const content = Array.isArray(result.content) ? result.content : [];
  const text = content
    .filter((part): part is { type: string; text: string } =>
      Boolean(part && typeof part === "object"
        && (part as Record<string, unknown>).type === "text"
        && typeof (part as Record<string, unknown>).text === "string"))
    .map((part) => part.text)
    .find((item) => item.trim().startsWith("{"));
  let payload: Record<string, unknown> | undefined;
  if (text) {
    try {
      const parsed = JSON.parse(text) as unknown;
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        payload = parsed as Record<string, unknown>;
      }
    } catch {
      payload = undefined;
    }
  }
  const details = result.details && typeof result.details === "object"
    && !Array.isArray(result.details) ? result.details as Record<string, unknown> : undefined;
  active.toolCaptures.push({
    name: event.toolName,
    isError: event.isError === true || result.isError === true,
    ...(payload || details ? { payload: { ...(payload ?? {}), ...(details ?? {}) } } : {}),
  });
  // Bound retained tool history for long extraction conversations.
  if (active.toolCaptures.length > 4_096) {
    active.toolCaptures.splice(0, active.toolCaptures.length - 4_096);
  }
}
