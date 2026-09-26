import { randomUUID } from "node:crypto";
import {
  DEFAULT_EXTRACTION_THINKING_LEVEL,
  type CreateSessionRequest,
  type CredentialBroker,
  type SessionRecord,
} from "./contracts.js";
import { RuntimePolicyError } from "./errors.js";
import { buildMountManifest } from "./security.js";
import type { SandboxManager } from "./sandbox.js";
import type { RuntimeStore } from "./store.js";

export class PiRuntimeService {
  constructor(
    readonly store: RuntimeStore,
    private readonly sandbox: SandboxManager,
    private readonly credentials?: CredentialBroker,
  ) {}

  async create(request: CreateSessionRequest): Promise<SessionRecord> {
    const now = new Date().toISOString();
    const id = randomUUID();
    const record: SessionRecord = {
      id,
      userId: request.userId,
      jobId: request.jobId,
      task: request.task,
      thinkingLevel: request.model.thinkingLevel ?? DEFAULT_EXTRACTION_THINKING_LEVEL,
      status: "creating",
      authMode: request.auth.mode,
      createdAt: now,
      updatedAt: now,
    };
    this.store.putSession(record);
    this.store.appendEvent(request.userId, id, "session.created", {
      task: request.task,
      contextMode: request.contextMode,
      provider: request.model.provider,
      modelId: request.model.id,
      thinkingLevel: request.model.thinkingLevel ?? DEFAULT_EXTRACTION_THINKING_LEVEL,
    });

    let runtimeSecret = "";
    try {
      const manifest = buildMountManifest(request, id);
      if (request.auth.mode === "personal_codex_subscription") {
        const lease = await this.sandbox.create(manifest);
        record.status = "awaiting_user_auth";
        record.sandboxId = lease.id;
        record.updatedAt = new Date().toISOString();
        this.store.putSession(record);
        this.store.appendEvent(request.userId, id, "auth.required", {
          provider: "openai-codex",
          method: "pi_interactive_login",
          command: "/login openai-codex",
          credentialScope: "this_user_only",
          credentialDirectory: "PI_CODING_AGENT_DIR",
        });
        return record;
      }
      if (!this.credentials) {
        throw new RuntimePolicyError(
          "CREDENTIAL_BROKER_NOT_CONFIGURED",
          "A tenant-aware credential broker is required",
          503,
        );
      }
      const secret = await this.credentials.redeem({
        userId: request.userId,
        credentialRef: request.auth.credentialRef,
        mode: request.auth.mode,
        provider: request.auth.provider,
      });
      runtimeSecret = secret.value;
      const lease = await this.sandbox.create(manifest, {
        provider: request.auth.provider,
        credential: secret.value,
        ...(secret.expiresAt ? { expiresAt: secret.expiresAt } : {}),
      });
      record.status = "ready";
      record.sandboxId = lease.id;
      record.updatedAt = new Date().toISOString();
      this.store.putSession(record);
      this.store.appendEvent(request.userId, id, "session.ready", {
        sandboxId: lease.id,
        mountCount: manifest.mounts.length,
        gtIncluded: false,
      });
      return record;
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown runtime failure";
      record.status = error instanceof RuntimePolicyError ? "blocked" : "failed";
      record.blockReason = error instanceof RuntimePolicyError ? error.code : "RUNTIME_FAILURE";
      record.updatedAt = new Date().toISOString();
      this.store.putSession(record);
      this.store.appendEvent(request.userId, id, "session.blocked", {
        code: record.blockReason,
        message,
      }, runtimeSecret ? [runtimeSecret] : []);
      throw error;
    }
  }

  get(userId: string, sessionId: string): SessionRecord {
    return this.store.getSession(userId, sessionId);
  }

  list(userId: string): SessionRecord[] {
    return this.store.listSessions(userId).map((record) =>
      sessionPresentation(record, this.store.listEvents(userId, record.id)));
  }

  events(userId: string, sessionId: string, after = 0) {
    return this.store.listEvents(userId, sessionId, after);
  }

  subscribeEvents(sessionId: string, listener: Parameters<RuntimeStore["subscribeEvents"]>[1]) {
    return this.store.subscribeEvents(sessionId, listener);
  }
}

function sessionPresentation(
  record: SessionRecord,
  events: ReturnType<RuntimeStore["listEvents"]>,
): SessionRecord {
  const created = events.find((event) => event.type === "session.created");
  const createdData = objectData(created?.data);
  const validated = events.find((event) => {
    if (event.type !== "agent.event") return false;
    return objectData(event.data).phase === "input.validated";
  });
  const validatedInputs = objectData(objectData(validated?.data).inputs);
  const validatedInputNames = Array.isArray(validatedInputs)
    ? validatedInputs
      .map((input) => objectData(input).originalName)
      .filter((name): name is string => typeof name === "string" && name.length > 0)
      .slice(0, 2)
    : [];
  const createdInputNames = Array.isArray(createdData.inputNames)
    ? createdData.inputNames
      .filter((name): name is string =>
        typeof name === "string" && name.length > 0 && name.length <= 512)
      .slice(0, 2)
    : [];
  const fixture = objectData(createdData.source).fixture;
  const sealedInputNames = typeof fixture === "string"
    ? [...(SEALED_FIXTURE_INPUT_NAMES[fixture] ?? [])] : [];
  const inputNames = validatedInputNames.length
    ? validatedInputNames : createdInputNames.length ? createdInputNames : sealedInputNames;
  const contextMode = createdData.contextMode;
  return {
    ...record,
    ...(contextMode === "figure" || contextMode === "paper" || contextMode === "paper_si"
      ? { contextMode } : {}),
    ...(inputNames.length ? { inputNames } : {}),
  };
}

const SEALED_FIXTURE_INPUT_NAMES: Readonly<Record<string, readonly string[]>> = Object.freeze({
  "demo-figure": Object.freeze(["demo figure.png"]),
  "pdf-example-job": Object.freeze(["example document.pdf"]),
  "example-document": Object.freeze([
    "example document.pdf",
    "example supporting information.pdf",
  ]),
  "pdf-example-job-cancelled": Object.freeze(["example document.pdf"]),
});

function objectData(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" ? value as Record<string, unknown> : {};
}
