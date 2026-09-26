import { randomUUID } from "node:crypto";
import type { SessionRecord, SessionStatus } from "./contracts.js";
import { RuntimePolicyError } from "./errors.js";
import type { RuntimeStore } from "./store.js";

export const EXECUTION_WORK_ITEM_VERSION = "chemrxn.execution-work-item.v1" as const;
export const EXECUTION_CONTROL_EVENT_VERSION = "chemrxn.task-dispatch-event.v1" as const;

export const EXECUTION_WORK_ITEM_STATES = [
  "pending",
  "active",
  "blocked",
  "completed",
  "failed",
  "cancelled",
  "interrupted",
] as const;
export type ExecutionWorkItemState = typeof EXECUTION_WORK_ITEM_STATES[number];

export function queueOwnsCancellation(state: ExecutionWorkItemState): boolean {
  return state === "pending" || state === "active" || state === "blocked";
}

export interface ExecutionWorkItem {
  schemaVersion: typeof EXECUTION_WORK_ITEM_VERSION;
  id: string;
  userId: string;
  taskId: string;
  attemptId: string;
  sessionId: string;
  phase: "initial_extraction";
  state: ExecutionWorkItemState;
  priority: number;
  revision: number;
  activationCount: number;
  enqueuedAt: string;
  updatedAt: string;
  startedAt?: string;
  finishedAt?: string;
  lastErrorCode?: string;
}

export interface ExecutionQueueStore {
  enqueueWorkItem(input: {
    userId: string;
    sessionId: string;
    priority: number;
    enqueuedAt: string;
    requestEventSequence: number;
  }): ExecutionWorkItem;
  getWorkItem(userId: string, sessionId: string): ExecutionWorkItem;
  listPendingWorkItems(): ExecutionWorkItem[];
  listBlockedWorkItems(): ExecutionWorkItem[];
  listTerminalWorkItems(): ExecutionWorkItem[];
  queuePosition(userId: string, sessionId: string): number | null;
  claimNextWorkItem(input: {
    leaseOwner: string;
    claimedAt: string;
    leaseExpiresAt: string;
    maxConcurrency: number;
  }): ExecutionWorkItem | undefined;
  blockWorkItem(input: {
    userId: string;
    sessionId: string;
    expectedRevision: number;
    updatedAt: string;
    errorCode: string;
  }): ExecutionWorkItem;
  resumeWorkItem(input: {
    userId: string;
    sessionId: string;
    expectedRevision: number;
    updatedAt: string;
  }): ExecutionWorkItem;
  cancelWorkItem(input: {
    userId: string;
    sessionId: string;
    expectedRevision: number;
    updatedAt: string;
  }): ExecutionWorkItem;
  finishWorkItem(input: {
    userId: string;
    sessionId: string;
    expectedRevision: number;
    state: Extract<ExecutionWorkItemState,
      "completed" | "failed" | "cancelled" | "interrupted">;
    updatedAt: string;
    errorCode?: string;
  }): ExecutionWorkItem;
  recoverActiveWorkItems(recoveredAt: string): ExecutionWorkItem[];
}

export interface QueuedExtractionExecutor {
  start(
    userId: string,
    sessionId: string,
    apiKey: string | undefined,
    userDirection: string | undefined,
    signal: AbortSignal,
  ): Promise<void>;
  cancel(userId: string, sessionId: string): Promise<void>;
  interrupt?(userId: string, sessionId: string): Promise<void>;
  settle?(userId: string, sessionId: string): Promise<void>;
}

export interface EnqueuedExecution {
  workItem: ExecutionWorkItem;
  position: number | null;
  credentialRequired: boolean;
}

const TERMINAL_SESSION_STATUSES = new Set<SessionStatus>([
  "blocked",
  "failed",
  "aborted",
  "review_ready",
  "partial",
  "cancelled",
  "interrupted",
]);

/**
 * Single-process dispatcher backed by SQLite queue truth.
 *
 * The database owns ordering, claiming and recovery. API keys intentionally do
 * not: they live in this process only and a restarted BYO-key item blocks until
 * the user supplies a new one. A claimed item is never replayed automatically
 * after process loss because model/tool work may already have consumed quota.
 */
export class DurableExecutionQueue {
  private readonly ownerId: string;
  private readonly apiKeys = new Map<string, string>();
  private readonly running = new Map<string, Promise<void>>();
  private readonly runningOwners = new Map<string, string>();
  private readonly abortControllers = new Map<string, AbortController>();
  private readonly repairTimers = new Set<ReturnType<typeof setTimeout>>();
  private started = false;
  private drainScheduled = false;

  constructor(
    private readonly queueStore: ExecutionQueueStore,
    private readonly runtimeStore: RuntimeStore,
    private readonly executor: QueuedExtractionExecutor,
    private readonly options: {
      concurrency?: number;
      leaseMs?: number;
      ownerId?: string;
      now?: () => Date;
    } = {},
  ) {
    const concurrency = options.concurrency ?? 1;
    if (!Number.isSafeInteger(concurrency) || concurrency < 1 || concurrency > 16) {
      throw new Error("Execution queue concurrency must be an integer from 1 to 16");
    }
    this.ownerId = options.ownerId ?? `local-${process.pid}-${randomUUID()}`;
  }

  get concurrency(): number {
    return this.options.concurrency ?? 1;
  }

  /** Recover queue truth before generic session-orphan reconciliation runs. */
  start(): {
    interrupted: number;
    recoveredPrepared: number;
    pendingSessionIds: Set<string>;
  } {
    if (this.started) {
      return {
        interrupted: 0,
        recoveredPrepared: 0,
        pendingSessionIds: new Set(
          this.queueStore.listPendingWorkItems().map((item) => item.sessionId),
        ),
      };
    }
    this.started = true;
    const now = this.isoNow();
    const recoveredPrepared = this.recoverPreparedWithoutWorkItem(now);
    const recovered = this.queueStore.recoverActiveWorkItems(now);
    for (const workItem of recovered) {
      if (workItem.state === "interrupted") this.interruptRecoveredSession(workItem, now);
    }
    for (const workItem of this.queueStore.listBlockedWorkItems()) {
      this.reconcileBlockedSession(workItem, now);
    }
    for (const workItem of this.queueStore.listTerminalWorkItems()) {
      this.reconcileTerminalWorkItem(workItem, now);
    }
    const pending = this.queueStore.listPendingWorkItems();
    const pendingSessionIds = new Set(pending.map((item) => item.sessionId));
    for (const workItem of pending) pendingSessionIds.add(workItem.sessionId);
    this.safePublishPositions();
    this.scheduleDrain();
    return {
      interrupted: recovered.filter((item) => item.state === "interrupted").length,
      recoveredPrepared,
      pendingSessionIds,
    };
  }

  enqueue(input: {
    userId: string;
    sessionId: string;
    apiKey?: string;
    userDirection?: string;
    priority?: number;
  }): EnqueuedExecution {
    this.assertAccepting();
    const session = this.runtimeStore.getSession(input.userId, input.sessionId);
    if (session.status !== "creating") {
      throw new RuntimePolicyError(
        "SESSION_NOT_ACTIVE",
        `Only a prepared session can be queued (${session.status})`,
        409,
      );
    }
    const now = this.isoNow();
    const workItem = this.queueStore.enqueueWorkItem({
      userId: input.userId,
      sessionId: input.sessionId,
      priority: input.priority ?? 0,
      enqueuedAt: now,
      requestEventSequence: 1,
    });
    if (input.apiKey) this.apiKeys.set(input.sessionId, input.apiKey);
    const position = this.queueStore.queuePosition(input.userId, input.sessionId);
    this.scheduleDrain();
    this.safeAppendControlEvent(input.userId, input.sessionId, {
      phase: "queue.enqueued",
      schemaVersion: EXECUTION_CONTROL_EVENT_VERSION,
      controlEventId: controlEventId(workItem, "enqueued"),
      workItemId: workItem.id,
      position,
      ahead: position == null ? null : Math.max(0, position - 1),
      concurrency: this.concurrency,
      message: position && position > 1
        ? `${position - 1} extraction${position === 2 ? "" : "s"} ahead.`
        : "Waiting for the local extraction worker.",
    }, input.apiKey ? [input.apiKey] : []);
    this.safePublishPositions();
    return { workItem, position, credentialRequired: false };
  }

  status(userId: string, sessionId: string): EnqueuedExecution {
    const workItem = this.queueStore.getWorkItem(userId, sessionId);
    return {
      workItem,
      position: this.queueStore.queuePosition(userId, sessionId),
      credentialRequired: workItem.state === "blocked",
    };
  }

  supplyApiKey(userId: string, sessionId: string, apiKey: string): EnqueuedExecution {
    this.assertAccepting();
    const current = this.queueStore.getWorkItem(userId, sessionId);
    if (current.state !== "blocked") {
      throw new RuntimePolicyError(
        "INVALID_CONTEXT",
        `Queued credentials are not expected (${current.state})`,
        409,
      );
    }
    const session = this.runtimeStore.getSession(userId, sessionId);
    if (session.authMode !== "api_key" || session.status !== "awaiting_user_auth") {
      throw new RuntimePolicyError(
        "INVALID_CONTEXT",
        "This queued extraction is not waiting for a BYO API key",
        409,
      );
    }
    this.apiKeys.set(sessionId, apiKey);
    const resumed = this.queueStore.resumeWorkItem({
      userId,
      sessionId,
      expectedRevision: current.revision,
      updatedAt: this.isoNow(),
    });
    session.status = "creating";
    delete session.blockReason;
    session.updatedAt = resumed.updatedAt;
    this.runtimeStore.putSession(session);
    this.scheduleDrain();
    this.safeAppendControlEvent(userId, sessionId, {
      phase: "queue.credential_received",
      schemaVersion: EXECUTION_CONTROL_EVENT_VERSION,
      controlEventId: controlEventId(resumed, "credential_received"),
      workItemId: resumed.id,
      message: "Credential received for this run only. Returning to the queue.",
    }, [apiKey]);
    this.safePublishPositions();
    return {
      workItem: resumed,
      position: this.queueStore.queuePosition(userId, sessionId),
      credentialRequired: false,
    };
  }

  async cancel(userId: string, sessionId: string): Promise<ExecutionWorkItem> {
    const current = this.queueStore.getWorkItem(userId, sessionId);
    if (current.state === "active") {
      this.safeAppendControlEvent(userId, sessionId, {
        phase: "queue.cancel_requested",
        schemaVersion: EXECUTION_CONTROL_EVENT_VERSION,
        controlEventId: controlEventId(current, "cancel_requested"),
        workItemId: current.id,
        message: "Stopping the active extraction safely.",
      });
      this.abortControllers.get(sessionId)?.abort("user_requested");
      try {
        await this.executor.cancel(userId, sessionId);
      } catch (error) {
        // During activation the local Pi handle may not exist yet. The abort
        // signal still prevents the activation path from starting a model turn.
        if (!(error instanceof RuntimePolicyError)
          || !["SESSION_NOT_ACTIVE", "AGENT_BUSY"].includes(error.code)) throw error;
      }
      return this.queueStore.getWorkItem(userId, sessionId);
    }
    if (!["pending", "blocked"].includes(current.state)) {
      throw new RuntimePolicyError(
        "SESSION_NOT_ACTIVE",
        `Queued extraction cannot be cancelled (${current.state})`,
        409,
      );
    }
    const now = this.isoNow();
    const cancelled = this.queueStore.cancelWorkItem({
      userId,
      sessionId,
      expectedRevision: current.revision,
      updatedAt: now,
    });
    this.apiKeys.delete(sessionId);
    const session = this.runtimeStore.getSession(userId, sessionId);
    session.status = "cancelled";
    delete session.blockReason;
    session.updatedAt = now;
    this.runtimeStore.putSession(session);
    this.runtimeStore.appendEvent(userId, sessionId, "job.cancelled", {
      phase: "job.cancelled",
      source: "queue",
      beforeActivation: true,
      retryable: true,
      message: "Queued extraction cancelled before any model or chemistry tool started.",
    });
    this.safePublishPositions();
    this.scheduleDrain();
    return cancelled;
  }

  async stop(): Promise<void> {
    this.started = false;
    for (const controller of this.abortControllers.values()) {
      controller.abort("runtime_shutdown");
    }
    await Promise.allSettled([...this.runningOwners].map(([sessionId, userId]) =>
      (this.executor.interrupt ?? this.executor.cancel)(userId, sessionId).catch((error) => {
        if (error instanceof RuntimePolicyError && error.code === "SESSION_NOT_ACTIVE") return;
        throw error;
      })));
    await Promise.allSettled(this.running.values());
    for (const timer of this.repairTimers) clearTimeout(timer);
    this.repairTimers.clear();
    this.apiKeys.clear();
  }

  private scheduleDrain(): void {
    if (!this.started || this.drainScheduled) return;
    this.drainScheduled = true;
    queueMicrotask(() => {
      this.drainScheduled = false;
      try {
        this.drain();
      } catch {
        // A transient SQLite contention or artifact-plane failure must not
        // become an unhandled microtask rejection that kills the local UI.
        const retry = setTimeout(() => this.scheduleDrain(), 100);
        retry.unref();
      }
    });
  }

  private assertAccepting(): void {
    if (this.started) return;
    throw new RuntimePolicyError(
      "RUNTIME_SHUTTING_DOWN",
      "The local extraction queue is stopping. Retry after the runtime restarts.",
      503,
    );
  }

  private drain(): void {
    while (this.started && this.running.size < this.concurrency) {
      const claimedAt = this.isoNow();
      const leaseExpiresAt = new Date(
        new Date(claimedAt).getTime() + (this.options.leaseMs ?? 24 * 60 * 60_000),
      ).toISOString();
      const workItem = this.queueStore.claimNextWorkItem({
        leaseOwner: this.ownerId,
        claimedAt,
        leaseExpiresAt,
        maxConcurrency: this.concurrency,
      });
      if (!workItem) break;
      try {
        const session = this.runtimeStore.getSession(workItem.userId, workItem.sessionId);
        const apiKey = this.apiKeys.get(workItem.sessionId);
        if (session.authMode === "api_key" && !apiKey) {
          this.blockForCredential(workItem, session);
          continue;
        }
        this.apiKeys.delete(workItem.sessionId);
        const controller = new AbortController();
        this.abortControllers.set(workItem.sessionId, controller);
        this.runningOwners.set(workItem.sessionId, workItem.userId);
        const running = this.execute(workItem, apiKey, controller.signal)
          .catch(() => {
            // execute() persists a bounded terminal failure before rejecting.
          })
          .finally(() => {
            this.running.delete(workItem.sessionId);
            this.runningOwners.delete(workItem.sessionId);
            this.abortControllers.delete(workItem.sessionId);
            this.safePublishPositions();
            this.scheduleDrain();
          });
        this.running.set(workItem.sessionId, running);
      } catch (error) {
        this.repairClaimFailure(workItem);
      }
    }
    this.safePublishPositions();
  }

  private async execute(
    workItem: ExecutionWorkItem,
    apiKey: string | undefined,
    signal: AbortSignal,
  ): Promise<void> {
    try {
      const direction = queuedDirection(
        this.runtimeStore.listEvents(workItem.userId, workItem.sessionId),
      );
      this.safeAppendControlEvent(workItem.userId, workItem.sessionId, {
        phase: "queue.started",
        schemaVersion: EXECUTION_CONTROL_EVENT_VERSION,
        controlEventId: controlEventId(workItem, "started"),
        workItemId: workItem.id,
        concurrency: this.concurrency,
        message: "Local extraction slot acquired. Preparing chemistry tools and Pi.",
      }, apiKey ? [apiKey] : []);
      await this.executor.start(
        workItem.userId,
        workItem.sessionId,
        apiKey,
        direction,
        signal,
      );
      const terminal = await this.waitForTerminal(workItem.userId, workItem.sessionId);
      if (terminal.status !== "ready") {
        await this.executor.settle?.(workItem.userId, workItem.sessionId);
      }
      this.finishFromSession(workItem.userId, workItem.sessionId, terminal);
    } catch {
      this.repairClaimFailure(workItem);
    }
  }

  private blockForCredential(workItem: ExecutionWorkItem, session: SessionRecord): void {
    const now = this.isoNow();
    const blocked = this.queueStore.blockWorkItem({
      userId: workItem.userId,
      sessionId: workItem.sessionId,
      expectedRevision: workItem.revision,
      updatedAt: now,
      errorCode: "QUEUE_CREDENTIAL_REQUIRED",
    });
    session.status = "awaiting_user_auth";
    session.blockReason = "QUEUE_CREDENTIAL_REQUIRED";
    session.updatedAt = now;
    this.runtimeStore.putSession(session);
    const requiredEventId = controlEventId(blocked, "credential_required");
    this.appendRetryableEvent(workItem.userId, workItem.sessionId, "auth.required", {
      code: "QUEUE_CREDENTIAL_REQUIRED",
      schemaVersion: EXECUTION_CONTROL_EVENT_VERSION,
      controlEventId: requiredEventId,
      method: "byo_api_key_reentry",
      credentialScope: "this_run_only",
      persisted: false,
      workItemId: blocked.id,
      message: "The local service restarted before this queued run began. Re-enter the API key to continue; the key was never saved.",
    }, [], () => {
      try {
        const current = this.queueStore.getWorkItem(workItem.userId, workItem.sessionId);
        return current.state === "blocked" && current.revision === blocked.revision;
      } catch {
        return false;
      }
    });
  }

  private waitForTerminal(userId: string, sessionId: string): Promise<SessionRecord> {
    const immediate = this.runtimeStore.getSession(userId, sessionId);
    if (releaseWorkerStatus(immediate.status)) return Promise.resolve(immediate);
    return new Promise((resolve) => {
      let closed = false;
      let unsubscribe = (): void => undefined;
      let timer: ReturnType<typeof setInterval> | undefined;
      const finish = (): void => {
        if (closed) return;
        const current = this.runtimeStore.getSession(userId, sessionId);
        if (!releaseWorkerStatus(current.status)) return;
        closed = true;
        unsubscribe();
        if (timer) clearInterval(timer);
        resolve(current);
      };
      unsubscribe = this.runtimeStore.subscribeEvents(sessionId, finish);
      timer = setInterval(finish, 100);
      timer.unref();
      finish();
    });
  }

  private failNonTerminalSession(
    userId: string,
    sessionId: string,
  ): SessionRecord {
    const session = this.runtimeStore.getSession(userId, sessionId);
    if (TERMINAL_SESSION_STATUSES.has(session.status)) return session;
    session.status = "failed";
    session.blockReason = "QUEUE_EXECUTION_FAILED";
    session.updatedAt = this.isoNow();
    this.runtimeStore.putSession(session);
    this.safeAppendEvent(userId, sessionId, "agent.error", {
      code: "QUEUE_EXECUTION_FAILED",
      message: "Queued extraction failed before completion. Retry creates a new isolated attempt.",
      retryable: true,
    });
    return session;
  }

  private finishFromSession(userId: string, sessionId: string, session: SessionRecord): void {
    try {
      const current = this.queueStore.getWorkItem(userId, sessionId);
      if (!["active", "blocked"].includes(current.state)) return;
      const state = queueTerminalState(session.status);
      const finished = this.queueStore.finishWorkItem({
        userId,
        sessionId,
        expectedRevision: current.revision,
        state,
        updatedAt: this.isoNow(),
        ...(session.blockReason ? { errorCode: session.blockReason } : {}),
      });
      this.safeAppendControlEvent(userId, sessionId, {
        phase: "queue.finished",
        schemaVersion: EXECUTION_CONTROL_EVENT_VERSION,
        controlEventId: controlEventId(finished, "finished"),
        workItemId: finished.id,
        outcome: state,
        message: `Extraction worker released (${state}).`,
      });
      this.safePublishPositions();
      this.scheduleDrain();
    } catch {
      this.scheduleRepair(() => {
        const current = this.runtimeStore.getSession(userId, sessionId);
        this.finishFromSession(userId, sessionId, current);
      });
    }
  }

  private interruptRecoveredSession(workItem: ExecutionWorkItem, now: string): void {
    const session = this.runtimeStore.getSession(workItem.userId, workItem.sessionId);
    if (TERMINAL_SESSION_STATUSES.has(session.status) && session.status !== "interrupted") return;
    if (!TERMINAL_SESSION_STATUSES.has(session.status)) {
      session.status = "interrupted";
      session.blockReason = "QUEUE_WORKER_RESTART";
      session.updatedAt = now;
      this.runtimeStore.putSession(session);
    }
    const alreadyRecorded = this.runtimeStore.listEvents(workItem.userId, workItem.sessionId)
      .some((event) => event.type === "job.interrupted"
        && event.data && typeof event.data === "object"
        && (event.data as Record<string, unknown>).code === "QUEUE_WORKER_RESTART");
    if (alreadyRecorded) return;
    this.safeAppendEvent(workItem.userId, workItem.sessionId, "job.interrupted", {
      code: "QUEUE_WORKER_RESTART",
      message: "The local runtime restarted after this extraction acquired a worker slot. It was not replayed automatically because model or tool work may already have run.",
      resumable: true,
      retryCreatesNewAttempt: true,
      credentialRequired: session.authMode === "api_key" ? "api_key" : "pi_auth",
    });
  }

  private recoverPreparedWithoutWorkItem(now: string): number {
    let recovered = 0;
    for (const session of this.runtimeStore.listAllSessions()) {
      if (session.status !== "creating") continue;
      const created = this.runtimeStore.listEvents(session.userId, session.id)
        .find((event) => event.type === "session.created");
      if (!created) {
        // prepare() persists the session before its request envelope. A crash
        // in that narrow window leaves no safe input/model/auth request to
        // replay, so fail closed instead of keeping an immortal ghost task.
        session.status = "interrupted";
        session.blockReason = "QUEUE_REQUEST_INCOMPLETE";
        session.updatedAt = now;
        this.runtimeStore.putSession(session);
        this.safeAppendEvent(session.userId, session.id, "job.interrupted", {
          code: "QUEUE_REQUEST_INCOMPLETE",
          source: "queue_recovery",
          beforeEnqueue: true,
          message: "The runtime stopped before the extraction request envelope was committed. Start a new task.",
          resumable: false,
          retryCreatesNewAttempt: false,
        });
        continue;
      }
      const data = created?.data && typeof created.data === "object"
        ? created.data as Record<string, unknown> : {};
      const request = data.localExtractionRequest;
      if (!request || typeof request !== "object"
        || (request as Record<string, unknown>).dispatchRequested !== true) continue;
      try {
        this.queueStore.getWorkItem(session.userId, session.id);
        continue;
      } catch (error) {
        if (!(error instanceof RuntimePolicyError) || error.code !== "WORK_ITEM_NOT_FOUND") {
          throw error;
        }
      }
      const workItem = this.queueStore.enqueueWorkItem({
        userId: session.userId,
        sessionId: session.id,
        priority: 0,
        enqueuedAt: now,
        requestEventSequence: created.seq,
      });
      this.safeAppendControlEvent(session.userId, session.id, {
        phase: "queue.recovered",
        schemaVersion: EXECUTION_CONTROL_EVENT_VERSION,
        controlEventId: controlEventId(workItem, "recovered"),
        workItemId: workItem.id,
        message: "Recovered a prepared extraction request after local runtime restart.",
      });
      recovered += 1;
    }
    return recovered;
  }

  private reconcileBlockedSession(workItem: ExecutionWorkItem, now: string): void {
    const session = this.runtimeStore.getSession(workItem.userId, workItem.sessionId);
    if (TERMINAL_SESSION_STATUSES.has(session.status)) {
      this.finishFromSession(workItem.userId, workItem.sessionId, session);
      return;
    }
    if (session.status !== "awaiting_user_auth") {
      session.status = "awaiting_user_auth";
      session.blockReason = "QUEUE_CREDENTIAL_REQUIRED";
      session.updatedAt = now;
      this.runtimeStore.putSession(session);
    }
    const requiredEventId = controlEventId(workItem, "credential_required");
    const alreadyRecorded = this.runtimeStore.listEvents(workItem.userId, workItem.sessionId)
      .some((event) => event.type === "auth.required"
        && event.data && typeof event.data === "object"
        && (event.data as Record<string, unknown>).controlEventId === requiredEventId);
    if (alreadyRecorded) return;
    this.appendRetryableEvent(workItem.userId, workItem.sessionId, "auth.required", {
      code: "QUEUE_CREDENTIAL_REQUIRED",
      schemaVersion: EXECUTION_CONTROL_EVENT_VERSION,
      controlEventId: requiredEventId,
      method: "byo_api_key_reentry",
      credentialScope: "this_run_only",
      persisted: false,
      recovered: true,
      workItemId: workItem.id,
      message: "Re-enter the API key to continue this queued extraction; the key was never saved.",
    }, [], () => {
      try {
        const current = this.queueStore.getWorkItem(workItem.userId, workItem.sessionId);
        return current.state === "blocked" && current.revision === workItem.revision;
      } catch {
        return false;
      }
    });
  }

  private reconcileTerminalWorkItem(workItem: ExecutionWorkItem, now: string): void {
    const session = this.runtimeStore.getSession(workItem.userId, workItem.sessionId);
    if (workItem.state === "completed"
      && (session.status === "review_ready" || session.status === "partial")) {
      const eventType = session.status === "review_ready" ? "job.review_ready" : "job.partial";
      const eventId = controlEventId(workItem, `recovered_${eventType}`);
      const recorded = this.runtimeStore.listEvents(workItem.userId, workItem.sessionId)
        .some((event) => event.type === eventType);
      if (!recorded) {
        this.appendRetryableEvent(workItem.userId, workItem.sessionId, eventType, {
          phase: eventType,
          schemaVersion: EXECUTION_CONTROL_EVENT_VERSION,
          controlEventId: eventId,
          workItemId: workItem.id,
          source: "immutable_result_recovery",
          recovered: true,
          message: session.status === "review_ready"
            ? "Recovered sealed extraction artifacts for review."
            : "Recovered sealed partial extraction artifacts for review.",
        });
      }
      return;
    }
    if (workItem.state === "interrupted") {
      this.interruptRecoveredSession(workItem, now);
      return;
    }
    if (workItem.state === "cancelled") {
      if (session.status !== "cancelled" && session.status !== "aborted") {
        session.status = "cancelled";
        delete session.blockReason;
        session.updatedAt = now;
        this.runtimeStore.putSession(session);
      }
      const recorded = this.runtimeStore.listEvents(workItem.userId, workItem.sessionId)
        .some((event) => event.type === "job.cancelled");
      if (!recorded) {
        this.safeAppendEvent(workItem.userId, workItem.sessionId, "job.cancelled", {
          phase: "job.cancelled",
          source: "queue_recovery",
          beforeActivation: true,
          retryable: true,
          message: "Recovered a queued cancellation after local runtime restart.",
        });
      }
      return;
    }
    if (workItem.state === "failed") {
      if (!TERMINAL_SESSION_STATUSES.has(session.status)) {
        session.status = "failed";
        session.blockReason = workItem.lastErrorCode ?? "QUEUE_EXECUTION_FAILED";
        session.updatedAt = now;
        this.runtimeStore.putSession(session);
      }
      const failureEventId = controlEventId(workItem, "recovered_failed");
      const recorded = this.runtimeStore.listEvents(workItem.userId, workItem.sessionId)
        .some((event) => event.type === "agent.error"
          && event.data && typeof event.data === "object"
          && (event.data as Record<string, unknown>).controlEventId === failureEventId);
      if (!recorded) {
        this.appendRetryableEvent(workItem.userId, workItem.sessionId, "agent.error", {
          code: session.blockReason ?? workItem.lastErrorCode ?? "QUEUE_EXECUTION_FAILED",
          schemaVersion: EXECUTION_CONTROL_EVENT_VERSION,
          controlEventId: failureEventId,
          workItemId: workItem.id,
          source: "queue_recovery",
          message: "Recovered a failed extraction after local runtime restart.",
          retryable: true,
        });
      }
    }
  }

  private safePublishPositions(): void {
    try {
      this.publishPositions();
    } catch {
      // Position is a derived presentation. Queue truth remains in SQLite and
      // the next transition/list request recomputes it authoritatively.
    }
  }

  private safeAppendControlEvent(
    userId: string,
    sessionId: string,
    data: Record<string, unknown>,
    secrets: string[] = [],
  ): void {
    this.appendRetryableEvent(userId, sessionId, "agent.event", data, secrets);
  }

  private appendRetryableEvent(
    userId: string,
    sessionId: string,
    type: Parameters<RuntimeStore["appendEvent"]>[2],
    data: Record<string, unknown>,
    secrets: string[] = [],
    isStillRelevant: () => boolean = () => true,
  ): void {
    const controlId = typeof data.controlEventId === "string"
      ? data.controlEventId : undefined;
    try {
      if (controlId) {
        const recorded = this.runtimeStore.listEvents(userId, sessionId)
          .some((event) => event.data && typeof event.data === "object"
            && (event.data as Record<string, unknown>).controlEventId === controlId);
        if (recorded) return;
      }
      if (!isStillRelevant()) return;
      this.runtimeStore.appendEvent(userId, sessionId, type, data, secrets);
    } catch {
      // Durable queue transitions are not merely cosmetic: cursor-only clients
      // need them after a transient event-plane failure. Retry by deterministic
      // control ID while the transition remains relevant. Raw credentials are
      // never captured in the retry closure; queue events contain no secrets.
      this.scheduleRepair(() => this.appendRetryableEvent(
        userId,
        sessionId,
        type,
        data,
        [],
        isStillRelevant,
      ));
    }
  }

  private safeAppendEvent(
    userId: string,
    sessionId: string,
    type: Parameters<RuntimeStore["appendEvent"]>[2],
    data: unknown,
    secrets: string[] = [],
  ): void {
    try {
      this.runtimeStore.appendEvent(userId, sessionId, type, data, secrets);
    } catch {
      // SQLite remains authoritative. Status fetch/startup reconciliation can
      // repair presentation without repeating model or chemistry work.
    }
  }

  private repairClaimFailure(workItem: ExecutionWorkItem): void {
    try {
      const terminal = this.failNonTerminalSession(workItem.userId, workItem.sessionId);
      this.finishFromSession(workItem.userId, workItem.sessionId, terminal);
    } catch {
      this.scheduleRepair(() => this.repairClaimFailure(workItem));
    }
  }

  private scheduleRepair(repair: () => void): void {
    if (!this.started) return;
    const timer = setTimeout(() => {
      this.repairTimers.delete(timer);
      if (!this.started) return;
      repair();
    }, 250);
    timer.unref();
    this.repairTimers.add(timer);
  }

  private publishPositions(): void {
    for (const workItem of this.queueStore.listPendingWorkItems()) {
      const position = this.queueStore.queuePosition(workItem.userId, workItem.sessionId);
      if (position == null) continue;
      const events = this.runtimeStore.listEvents(workItem.userId, workItem.sessionId);
      const last = [...events].reverse().find((event) => {
        if (event.type !== "agent.event" || !event.data || typeof event.data !== "object") return false;
        const data = event.data as Record<string, unknown>;
        return data.phase === "queue.position" || data.phase === "queue.enqueued";
      });
      const previous = last?.data && typeof last.data === "object"
        ? Number((last.data as Record<string, unknown>).position) : Number.NaN;
      if (previous === position) continue;
      this.runtimeStore.appendEvent(workItem.userId, workItem.sessionId, "agent.event", {
        phase: "queue.position",
        schemaVersion: EXECUTION_CONTROL_EVENT_VERSION,
        controlEventId: controlEventId(workItem, `position:${position}`),
        workItemId: workItem.id,
        position,
        ahead: Math.max(0, position - 1),
        concurrency: this.concurrency,
        message: position > 1
          ? `${position - 1} extraction${position === 2 ? "" : "s"} ahead.`
          : "Next in line for the local extraction worker.",
      });
    }
  }

  private isoNow(): string {
    return (this.options.now?.() ?? new Date()).toISOString();
  }
}

function queuedDirection(events: ReturnType<RuntimeStore["listEvents"]>): string | undefined {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (!event?.data || typeof event.data !== "object") continue;
    const data = event.data as Record<string, unknown>;
    if (event.type === "agent.event" && data.phase === "queue.enqueued") {
      return typeof data.userDirection === "string" ? data.userDirection : undefined;
    }
    if (event.type === "session.created") {
      const request = data.localExtractionRequest;
      if (request && typeof request === "object") {
        const initialMessage = (request as Record<string, unknown>).initialMessage;
        return typeof initialMessage === "string" ? initialMessage : undefined;
      }
    }
  }
  return undefined;
}

function controlEventId(workItem: ExecutionWorkItem, transition: string): string {
  return `${workItem.id}:${workItem.revision}:${transition}`;
}

function queueTerminalState(
  status: SessionStatus,
): Extract<ExecutionWorkItemState, "completed" | "failed" | "cancelled" | "interrupted"> {
  if (status === "review_ready" || status === "partial") return "completed";
  if (status === "ready") return "completed";
  if (status === "cancelled" || status === "aborted") return "cancelled";
  if (status === "interrupted") return "interrupted";
  return "failed";
}

function releaseWorkerStatus(status: SessionStatus): boolean {
  return status === "ready" || TERMINAL_SESSION_STATUSES.has(status);
}
