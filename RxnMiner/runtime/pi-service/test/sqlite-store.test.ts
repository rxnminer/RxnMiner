import assert from "node:assert/strict";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";
import type { RuntimeEvent, RuntimeJobResult, SessionRecord } from "../src/contracts.js";
import { RuntimePolicyError } from "../src/errors.js";
import { interruptOrphanedLocalSessions } from "../src/local-agent.js";
import {
  SQLITE_TASK_INDEX_FILE,
  SQLITE_TASK_INDEX_SCHEMA_VERSION,
  SqliteRuntimeStore,
} from "../src/sqlite-store.js";
import { DurableFileRuntimeStore } from "../src/store.js";

function session(overrides: Partial<SessionRecord> = {}): SessionRecord {
  return {
    id: "session-sqlite",
    userId: "user-a",
    jobId: "job-a",
    task: "extract_all",
    status: "creating",
    authMode: "api_key",
    createdAt: "2026-07-31T00:00:00.000Z",
    updatedAt: "2026-07-31T00:00:00.000Z",
    ...overrides,
  };
}

function sealedResult(sessionId: string, manifest: unknown = { reactions: 1 }): RuntimeJobResult {
  return {
    schemaVersion: "chemrxn.runtime-result.v1",
    sessionId,
    jobId: "job-a",
    contextMode: "figure",
    source: {
      mode: "live_pi_sdk",
      reconstructed: false,
      liveEventCapture: true,
      inputArtifactIds: ["input-a"],
      workspaceId: sessionId,
    },
    domainContract: "chemrxn.figure-job.v1",
    terminalStatus: "review_ready",
    manifest,
    createdAt: "2026-07-31T00:00:03.000Z",
    gtIncluded: false,
  };
}

function putPreparedQueueSession(
  store: SqliteRuntimeStore,
  id: string,
  authMode: "api_key" | "pi_auth" = "api_key",
): void {
  const record = session({
    id,
    jobId: `job-${id}`,
    authMode: authMode === "pi_auth" ? "personal_codex_subscription" : "api_key",
  });
  store.putSession(record);
  store.appendEvent("user-a", id, "session.created", {
    source: { mode: "live_pi_sdk", localSingleUser: true },
    contextMode: "figure",
    provider: authMode === "pi_auth" ? "openai-codex" : "openai",
    modelId: "gpt-test",
    authMode,
    localExtractionRequest: {
      schemaVersion: "chemrxn.local-extraction-request.v1",
      dispatchRequested: true,
      contextMode: "figure",
      task: "extract_all",
      provider: authMode === "pi_auth" ? "openai-codex" : "openai",
      modelId: "gpt-test",
      authMode,
      requestedWorkspaceId: id,
      inputs: [],
    },
  });
}

test("SQLite task plane migrates and emits canonical versioned index envelopes", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-sqlite-index-"));
  try {
    const store = new SqliteRuntimeStore(root);
    assert.equal(store.schemaVersion, SQLITE_TASK_INDEX_SCHEMA_VERSION);
    assert.deepEqual(store.appliedMigrations().map((migration) => migration.version), [1, 2, 3]);

    const record = session();
    store.putSession(record);
    const created = store.appendEvent("user-a", record.id, "session.created", {
      contextMode: "paper",
      source: { mode: "live_pi_sdk" },
    });
    record.status = "running";
    record.updatedAt = "2026-07-31T00:00:01.000Z";
    store.putSession(record);
    const progress = store.appendEvent("user-a", record.id, "agent.event", {
      phase: "pdf.page.completed",
      progress: { completed: 1, total: 4, unit: "pages", denominator: "validated_page_count" },
    });

    const task = store.listTasks("user-a")[0];
    assert.ok(task);
    assert.equal(task.schemaVersion, "chemrxn.task.v1");
    assert.match(task.id, /^tsk_[a-f0-9]{32}$/);
    assert.equal(task.rootSessionId, record.id);
    assert.equal(task.state, "parsing_layout");
    assert.deepEqual(task.progress, {
      completed: 1,
      total: 4,
      unit: "pages",
      denominator: "validated_page_count",
    });

    const attempt = store.getAttempt("user-a", task.activeAttemptId);
    assert.equal(attempt.schemaVersion, "chemrxn.task-attempt.v1");
    assert.match(attempt.id, /^att_[a-f0-9]{32}$/);
    assert.equal(attempt.lastEventSequence, 2);
    assert.equal(attempt.runtimeStatus, "running");
    const references = store.listEventReferences("user-a", attempt.id);
    assert.deepEqual(references.map((reference) => reference.schemaVersion), [
      "chemrxn.task-event-ref.v1",
      "chemrxn.task-event-ref.v1",
    ]);
    assert.deepEqual(references.map((reference) => reference.sequence), [1, 2]);
    assert.match(references[0]!.id, /^evt_[a-f0-9]{32}$/);

    const eventArtifact = JSON.parse(readFileSync(
      path.join(root, references[1]!.artifact.locator),
      "utf8",
    )) as Record<string, unknown>;
    assert.deepEqual(eventArtifact, progress);
    assert.deepEqual(Object.keys(eventArtifact).sort(), ["at", "data", "seq", "sessionId", "type"]);
    assert.deepEqual(created, store.listEvents("user-a", record.id)[0]);
    assert.equal(statSync(path.join(root, SQLITE_TASK_INDEX_FILE)).mode & 0o777, 0o600);
    store.close();

    const restarted = new SqliteRuntimeStore(root);
    assert.equal(restarted.listTasks("user-a")[0]?.id, task.id);
    const delivered: number[] = [];
    const unsubscribe = restarted.subscribeEvents(record.id, (event) => delivered.push(event.seq));
    assert.equal(restarted.appendEvent(
      "user-a",
      record.id,
      "agent.event",
      { phase: "agent.message" },
    ).seq, 3);
    assert.deepEqual(delivered, [3]);
    const blocked = restarted.getSession("user-a", record.id);
    blocked.status = "blocked";
    blocked.blockReason = "USER_ACTION_REQUIRED";
    blocked.updatedAt = "2026-07-31T00:00:02.000Z";
    restarted.putSession(blocked);
    restarted.appendEvent("user-a", record.id, "session.blocked", {
      code: "USER_ACTION_REQUIRED",
    });
    assert.equal(restarted.getTask("user-a", task.id).state, "blocked");
    unsubscribe();
    restarted.close();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("SQLite upgrades a durable v2 task index to the v3 execution queue in place", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-sqlite-v2-v3-"));
  try {
    const initial = new SqliteRuntimeStore(root);
    const record = session({ id: "v2-upgrade-session" });
    initial.putSession(record);
    initial.appendEvent("user-a", record.id, "session.created", {
      source: { mode: "live_pi_sdk" },
      authMode: "api_key",
    });
    const taskId = initial.listTasks("user-a")[0]!.id;
    initial.close();

    // Recreate the exact pre-queue boundary without rewriting the v1/v2 data.
    const database = new DatabaseSync(path.join(root, SQLITE_TASK_INDEX_FILE));
    database.exec(`
      DROP TABLE execution_work_items;
      DELETE FROM schema_migrations WHERE version = 3;
      PRAGMA user_version = 2;
    `);
    database.close();
    const sessionBefore = readFileSync(
      path.join(root, "sessions", record.id, "session.json"),
    );
    const eventBefore = readFileSync(
      path.join(root, "sessions", record.id, "events", "000000000001.json"),
    );

    const upgraded = new SqliteRuntimeStore(root);
    assert.equal(upgraded.schemaVersion, 3);
    assert.deepEqual(
      upgraded.appliedMigrations().map(({ version, name }) => ({ version, name })),
      [
        { version: 1, name: "task-index-core" },
        { version: 2, name: "task-index-lookups" },
        { version: 3, name: "durable-execution-queue" },
      ],
    );
    assert.equal(upgraded.listTasks("user-a")[0]?.id, taskId);
    const queued = upgraded.enqueueWorkItem({
      userId: "user-a",
      sessionId: record.id,
      priority: 0,
      enqueuedAt: "2026-07-31T00:00:01.000Z",
      requestEventSequence: 1,
    });
    assert.equal(queued.state, "pending");
    assert.equal(queued.phase, "initial_extraction");
    upgraded.close();

    assert.deepEqual(
      readFileSync(path.join(root, "sessions", record.id, "session.json")),
      sessionBefore,
    );
    assert.deepEqual(
      readFileSync(path.join(root, "sessions", record.id, "events", "000000000001.json")),
      eventBefore,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("SQLite queue preserves FIFO positions and prevents two stores from over-claiming", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-sqlite-queue-claim-"));
  try {
    const first = new SqliteRuntimeStore(root);
    putPreparedQueueSession(first, "queue-first");
    putPreparedQueueSession(first, "queue-second");
    const firstPending = first.enqueueWorkItem({
      userId: "user-a",
      sessionId: "queue-first",
      priority: 0,
      enqueuedAt: "2026-07-31T01:00:00.000Z",
      requestEventSequence: 1,
    });
    first.enqueueWorkItem({
      userId: "user-a",
      sessionId: "queue-second",
      priority: 0,
      enqueuedAt: "2026-07-31T01:00:01.000Z",
      requestEventSequence: 1,
    });
    assert.deepEqual(
      first.listPendingWorkItems().map((work) => work.sessionId),
      ["queue-first", "queue-second"],
    );
    assert.equal(first.queuePosition("user-a", "queue-first"), 1);
    assert.equal(first.queuePosition("user-a", "queue-second"), 2);

    const second = new SqliteRuntimeStore(root);
    const claimed = first.claimNextWorkItem({
      leaseOwner: "runtime-a",
      claimedAt: "2026-07-31T01:00:02.000Z",
      leaseExpiresAt: "2026-07-31T02:00:02.000Z",
      maxConcurrency: 1,
    });
    assert.equal(claimed?.sessionId, "queue-first");
    assert.equal(claimed?.activationCount, 1);
    assert.equal(first.queuePosition("user-a", "queue-first"), null);
    assert.equal(second.queuePosition("user-a", "queue-second"), 1);
    assert.equal(second.claimNextWorkItem({
      leaseOwner: "runtime-b",
      claimedAt: "2026-07-31T01:00:02.001Z",
      leaseExpiresAt: "2026-07-31T02:00:02.001Z",
      maxConcurrency: 1,
    }), undefined, "the global active count must cap claims across SQLite connections");
    assert.throws(
      () => second.cancelWorkItem({
        userId: "user-a",
        sessionId: "queue-first",
        expectedRevision: firstPending.revision,
        updatedAt: "2026-07-31T01:00:03.000Z",
      }),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "STORE_CONFLICT",
    );
    const completed = first.finishWorkItem({
      userId: "user-a",
      sessionId: "queue-first",
      expectedRevision: claimed!.revision,
      state: "completed",
      updatedAt: "2026-07-31T01:00:04.000Z",
    });
    assert.equal(completed.state, "completed");
    assert.equal(second.claimNextWorkItem({
      leaseOwner: "runtime-b",
      claimedAt: "2026-07-31T01:00:05.000Z",
      leaseExpiresAt: "2026-07-31T02:00:05.000Z",
      maxConcurrency: 1,
    })?.sessionId, "queue-second");
    assert.throws(
      () => first.getWorkItem("user-b", "queue-first"),
      (error: unknown) =>
        error instanceof RuntimePolicyError && error.code === "CROSS_TENANT_ACCESS",
    );
    first.close();
    second.close();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("SQLite queue recovery interrupts active work once and never reclaims it", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-sqlite-queue-recovery-"));
  try {
    const first = new SqliteRuntimeStore(root);
    putPreparedQueueSession(first, "queue-orphan");
    first.enqueueWorkItem({
      userId: "user-a",
      sessionId: "queue-orphan",
      priority: 0,
      enqueuedAt: "2026-07-31T02:00:00.000Z",
      requestEventSequence: 1,
    });
    assert.equal(first.claimNextWorkItem({
      leaseOwner: "dead-runtime",
      claimedAt: "2026-07-31T02:00:01.000Z",
      leaseExpiresAt: "2026-07-31T03:00:01.000Z",
      maxConcurrency: 1,
    })?.state, "active");
    first.close();

    const restarted = new SqliteRuntimeStore(root);
    const recovered = restarted.recoverActiveWorkItems("2026-07-31T02:10:00.000Z");
    assert.equal(recovered.length, 1);
    assert.equal(recovered[0]?.state, "interrupted");
    assert.equal(recovered[0]?.activationCount, 1);
    assert.equal(restarted.listPendingWorkItems().length, 0);
    assert.equal(restarted.claimNextWorkItem({
      leaseOwner: "new-runtime",
      claimedAt: "2026-07-31T02:10:01.000Z",
      leaseExpiresAt: "2026-07-31T03:10:01.000Z",
      maxConcurrency: 1,
    }), undefined);
    assert.deepEqual(
      restarted.recoverActiveWorkItems("2026-07-31T02:20:00.000Z"),
      [],
    );
    restarted.close();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("SQLite imports legacy file sessions without rewriting event or result bytes", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-sqlite-import-"));
  try {
    const legacy = new DurableFileRuntimeStore(root);
    const record = session({ id: "legacy-session", status: "review_ready" });
    legacy.putSession(record);
    legacy.appendEvent("user-a", record.id, "session.created", {
      contextMode: "figure",
      source: { mode: "sealed_replay" },
    });
    legacy.appendEvent("user-a", record.id, "job.review_ready", {
      phase: "job.review_ready",
    });
    legacy.putResult("user-a", record.id, sealedResult(record.id));

    const paths = [
      path.join(root, "sessions", record.id, "session.json"),
      path.join(root, "sessions", record.id, "events", "000000000001.json"),
      path.join(root, "sessions", record.id, "events", "000000000002.json"),
      path.join(root, "sessions", record.id, "result.json"),
    ];
    const before = paths.map((target) => readFileSync(target));

    const indexed = new SqliteRuntimeStore(root);
    assert.deepEqual(indexed.getSession("user-a", record.id), record);
    assert.deepEqual(indexed.listEvents("user-a", record.id).map((event) => event.seq), [1, 2]);
    assert.deepEqual(indexed.getResult("user-a", record.id), sealedResult(record.id));
    const task = indexed.listTasks("user-a")[0];
    assert.ok(task);
    const attempt = indexed.getAttempt("user-a", task.activeAttemptId);
    const resultReference = indexed.getResultReference("user-a", attempt.id);
    assert.equal(resultReference.schemaVersion, "chemrxn.task-result-ref.v1");
    assert.match(resultReference.id, /^res_[a-f0-9]{32}$/);
    assert.equal(resultReference.artifact.locator, `sessions/${record.id}/result.json`);
    assert.equal(resultReference.artifact.immutable, true);
    assert.equal(resultReference.artifact.sha256.length, 64);
    indexed.close();

    assert.deepEqual(paths.map((target) => readFileSync(target)), before);
    const restarted = new SqliteRuntimeStore(root);
    assert.equal(
      restarted.appendEvent("user-a", record.id, "review.outcome", {
        proposalId: "legacy-proposal",
        status: "discarded",
      }).seq,
      3,
    );
    restarted.close();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("SQLite result recovery restores a cancelled terminal cursor exactly once", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-sqlite-cancel-result-recovery-"));
  try {
    const legacy = new DurableFileRuntimeStore(root);
    const record = session({ id: "cancel-result-crash", status: "running" });
    legacy.putSession(record);
    legacy.appendEvent("user-a", record.id, "session.created", {
      contextMode: "figure",
      source: { mode: "sealed_replay" },
    });
    legacy.putResult("user-a", record.id, {
      ...sealedResult(record.id),
      terminalStatus: "cancelled",
    });
    const resultPath = path.join(root, "sessions", record.id, "result.json");
    const before = readFileSync(resultPath);

    const restarted = new SqliteRuntimeStore(root);
    assert.equal(restarted.getSession("user-a", record.id).status, "cancelled");
    assert.equal(
      restarted.listEvents("user-a", record.id)
        .filter((event) => event.type === "job.cancelled").length,
      1,
    );
    restarted.close();

    const second = new SqliteRuntimeStore(root);
    assert.equal(
      second.listEvents("user-a", record.id)
        .filter((event) => event.type === "job.cancelled").length,
      1,
    );
    second.close();
    assert.deepEqual(readFileSync(resultPath), before);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("SQLite rejects result identity mismatches before sealing the artifact", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-sqlite-result-identity-"));
  const store = new SqliteRuntimeStore(root);
  try {
    const record = session({ contextMode: "figure" });
    store.putSession(record);
    const resultPath = path.join(root, "sessions", record.id, "result.json");
    assert.throws(
      () => store.putResult("user-a", record.id, {
        ...sealedResult(record.id),
        jobId: "wrong-job",
      }),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "STORE_CORRUPT",
    );
    assert.equal(existsSync(resultPath), false);
    assert.throws(
      () => store.putResult("user-a", record.id, {
        ...sealedResult(record.id),
        contextMode: "paper",
        domainContract: "chemrxn.pdf-replay.v1",
      }),
      /Invalid input: expected/,
    );
    assert.equal(existsSync(resultPath), false);
    const task = store.listTasks("user-a")[0]!;
    const attempt = store.listAttempts("user-a", task.id)[0]!;
    assert.throws(
      () => store.getResultReference("user-a", attempt.id),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "RESULT_NOT_READY",
    );
    store.putResult("user-a", record.id, sealedResult(record.id));
    assert.equal(existsSync(resultPath), true);
    assert.equal(store.getResult("user-a", record.id).jobId, record.jobId);
  } finally {
    store.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("SQLite session transitions use CAS and reject terminal rewinds", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-sqlite-cas-"));
  try {
    const first = new SqliteRuntimeStore(root);
    const record = session({ id: "cas-session", status: "ready" });
    first.putSession(record);
    const second = new SqliteRuntimeStore(root);
    const winner = first.getSession("user-a", record.id);
    const stale = second.getSession("user-a", record.id);
    const staleMarkerless = { ...stale };
    winner.status = "running";
    winner.updatedAt = "2026-07-31T00:00:01.000Z";
    first.putSession(winner);
    const spread = { ...first.getSession("user-a", record.id) };
    spread.updatedAt = "2026-07-31T00:00:01.500Z";
    assert.throws(
      () => first.putSession(spread),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "STORE_CONFLICT",
    );
    staleMarkerless.status = "failed";
    staleMarkerless.updatedAt = "2026-07-31T00:00:01.750Z";
    assert.throws(
      () => second.putSession(staleMarkerless),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "STORE_CONFLICT",
    );
    stale.status = "failed";
    stale.updatedAt = "2026-07-31T00:00:02.000Z";
    assert.throws(
      () => second.putSession(stale),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "STORE_CONFLICT",
    );
    assert.equal(second.getSession("user-a", record.id).status, "running");

    const task = first.listTasks("user-a")[0]!;
    const beforeEvent = first.getAttempt("user-a", task.activeAttemptId);
    first.appendEvent("user-a", record.id, "agent.event", { phase: "perception.started" });
    assert.throws(
      () => first.transitionAttempt("user-a", beforeEvent.id, beforeEvent.revision, {
        status: "failed",
        updatedAt: "2026-07-31T00:00:03.000Z",
      }),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "STORE_CONFLICT",
    );
    const current = first.getAttempt("user-a", beforeEvent.id);
    const failed = first.transitionAttempt("user-a", current.id, current.revision, {
      status: "failed",
      updatedAt: "2026-07-31T00:00:04.000Z",
      blockReason: "TEST_FAILURE",
    });
    assert.equal(failed.runtimeStatus, "failed");
    assert.equal(first.getSession("user-a", record.id).blockReason, "TEST_FAILURE");
    assert.throws(
      () => first.transitionAttempt("user-a", failed.id, failed.revision, {
        status: "running",
        updatedAt: "2026-07-31T00:00:05.000Z",
        blockReason: null,
      }),
      (error: unknown) =>
        error instanceof RuntimePolicyError && error.code === "INVALID_TASK_TRANSITION",
    );
    first.close();
    second.close();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("SQLite session CAS token reflects the exact committed write", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-sqlite-cas-token-"));
  try {
    const first = new SqliteRuntimeStore(root);
    const record = session({ id: "cas-token-session", status: "ready" });
    first.putSession(record);
    const second = new SqliteRuntimeStore(root);
    const update = first.getSession("user-a", record.id);
    update.status = "running";
    update.updatedAt = "2026-07-31T00:00:01.000Z";

    type Transaction = <T>(operation: () => T) => T;
    const firstInternals = first as unknown as { transaction: Transaction };
    const originalTransaction = firstInternals.transaction.bind(first) as Transaction;
    let injected = false;
    firstInternals.transaction = <T>(operation: () => T): T => {
      const result = originalTransaction(operation);
      if (!injected) {
        injected = true;
        const concurrent = second.getSession("user-a", record.id);
        concurrent.blockReason = "CONCURRENT_UPDATE";
        concurrent.updatedAt = "2026-07-31T00:00:02.000Z";
        second.putSession(concurrent);
      }
      return result;
    };
    try {
      first.putSession(update);
    } finally {
      firstInternals.transaction = originalTransaction;
    }
    assert.equal(injected, true);

    update.blockReason = "STALE_OVERWRITE";
    update.updatedAt = "2026-07-31T00:00:03.000Z";
    assert.throws(
      () => first.putSession(update),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "STORE_CONFLICT",
    );
    assert.equal(
      first.getSession("user-a", record.id).blockReason,
      "CONCURRENT_UPDATE",
    );
    first.close();
    second.close();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("SQLite event projection remains monotonic when two stores index out of order", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-sqlite-event-order-"));
  try {
    const first = new SqliteRuntimeStore(root);
    const record = session({ id: "event-order-session", status: "running" });
    first.putSession(record);
    const second = new SqliteRuntimeStore(root);
    const task = first.listTasks("user-a")[0]!;

    type StoreInternals = {
      indexEvent: (attempt: unknown, event: RuntimeEvent) => void;
      reconcileEvents: (attempt: unknown) => void;
      transaction: <T>(operation: () => T) => T;
    };
    const firstInternals = first as unknown as StoreInternals;
    const secondInternals = second as unknown as StoreInternals;
    const originalFirstIndex = firstInternals.indexEvent.bind(first);
    const originalSecondReconcile = secondInternals.reconcileEvents.bind(second);
    let delayed: { attempt: unknown; event: RuntimeEvent } | undefined;
    firstInternals.indexEvent = (attempt, event) => {
      delayed = { attempt, event };
    };
    secondInternals.reconcileEvents = () => undefined;

    const older = first.appendEvent("user-a", record.id, "agent.event", {
      phase: "perception.started",
      progress: { completed: 1, total: 4, unit: "pages" },
    });
    const newer = second.appendEvent("user-a", record.id, "agent.event", {
      phase: "validation.started",
      progress: { completed: 2, total: 4, unit: "pages" },
    });
    assert.equal(older.seq, 1);
    assert.equal(newer.seq, 2);
    assert.equal(delayed?.event.seq, 1);

    firstInternals.indexEvent = originalFirstIndex;
    secondInternals.reconcileEvents = originalSecondReconcile;
    const afterNewerAttempt = second.getAttempt("user-a", task.activeAttemptId);
    const afterNewerTask = second.getTask("user-a", task.id);
    firstInternals.transaction(() => originalFirstIndex(delayed!.attempt, delayed!.event));

    const projectedAttempt = first.getAttempt("user-a", task.activeAttemptId);
    const projectedTask = first.getTask("user-a", task.id);
    assert.equal(projectedAttempt.lastEventSequence, 2);
    assert.equal(projectedAttempt.state, "validating_chemistry");
    assert.deepEqual(projectedAttempt.progress, {
      completed: 2,
      total: 4,
      unit: "pages",
    });
    assert.equal(projectedAttempt.updatedAt, afterNewerAttempt.updatedAt);
    assert.equal(projectedTask.state, afterNewerTask.state);
    assert.deepEqual(projectedTask.progress, afterNewerTask.progress);
    assert.equal(projectedTask.updatedAt, afterNewerTask.updatedAt);
    assert.deepEqual(
      first.listEventReferences("user-a", task.activeAttemptId)
        .map((reference) => reference.sequence),
      [1, 2],
    );
    first.close();
    second.close();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("SQLite restart recovery terminalizes orphan work exactly once", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-sqlite-recovery-"));
  try {
    const initial = new SqliteRuntimeStore(root);
    const record = session({ id: "recovery-session", status: "running" });
    initial.putSession(record);
    initial.appendEvent("user-a", record.id, "session.created", {
      source: { mode: "live_pi_sdk" },
      contextMode: "paper",
    });
    initial.close();

    const restarted = new SqliteRuntimeStore(root);
    assert.equal(interruptOrphanedLocalSessions(restarted, { canRehydrate: true }), 1);
    assert.equal(restarted.getSession("user-a", record.id).status, "interrupted");
    assert.equal(restarted.listEvents("user-a", record.id).at(-1)?.type, "job.interrupted");
    restarted.close();

    const secondRestart = new SqliteRuntimeStore(root);
    assert.equal(interruptOrphanedLocalSessions(secondRestart, { canRehydrate: true }), 0);
    assert.equal(secondRestart.listEvents("user-a", record.id).length, 2);
    secondRestart.close();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("SQLite groups retry attempts and enforces tenant, GT, and credential boundaries", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-sqlite-policy-"));
  try {
    const store = new SqliteRuntimeStore(root);
    const parent = session({ id: "retry-parent", status: "cancelled" });
    store.putSession(parent);
    store.appendEvent("user-a", parent.id, "session.created", {
      source: { mode: "sealed_replay" },
    });
    const child = session({ id: "retry-child", jobId: "job-retry" });
    store.putSession(child);
    store.appendEvent("user-a", child.id, "session.created", {
      source: { mode: "sealed_replay" },
      retryOfSessionId: parent.id,
    });

    const tasks = store.listTasks("user-a");
    assert.equal(tasks.length, 1);
    assert.equal(tasks[0]?.rootSessionId, parent.id);
    const attempts = store.listAttempts("user-a", tasks[0]!.id);
    assert.equal(attempts.length, 2);
    assert.equal(attempts[1]?.relation, "retry");
    assert.equal(attempts[1]?.parentAttemptId, attempts[0]?.id);
    assert.equal(tasks[0]?.activeAttemptId, attempts[1]?.id);

    assert.throws(
      () => store.getTask("user-b", tasks[0]!.id),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "CROSS_TENANT_ACCESS",
    );
    assert.throws(
      () => store.getAttempt("user-b", attempts[0]!.id),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "CROSS_TENANT_ACCESS",
    );

    const secret = "TEST_ONLY_task-plane-secret-123456";
    store.appendEvent("user-a", child.id, "agent.event", {
      message: `provider echoed ${secret}`,
    }, [secret]);
    const persisted = JSON.stringify(store.listEvents("user-a", child.id).at(-1));
    assert.equal(persisted.includes(secret), false);
    assert.match(persisted, /REDACTED/);
    store.appendEvent("user-a", child.id, "agent.event", {
      authMode: "api_key",
      access_token: "opaque-access-token",
      refreshToken: "opaque-refresh-token",
      password: "opaque-password",
      client_secret: "opaque-client-secret",
      private_key: "opaque-private-key",
    });
    const credentialEvent = store.listEvents("user-a", child.id).at(-1)!;
    assert.deepEqual(credentialEvent.data, {
      authMode: "api_key",
      access_token: "[REDACTED]",
      refreshToken: "[REDACTED]",
      password: "[REDACTED]",
      client_secret: "[REDACTED]",
      private_key: "[REDACTED]",
    });
    const lastSequence = store.getAttempt("user-a", attempts[1]!.id).lastEventSequence;
    assert.throws(
      () => store.appendEvent("user-a", child.id, "agent.event", { value: "official_gt" }),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "INVALID_CONTEXT",
    );
    assert.equal(
      store.getAttempt("user-a", attempts[1]!.id).lastEventSequence,
      lastSequence,
    );
    assert.throws(
      () => store.appendEvent("user-a", child.id, "agent.event", {
        authorization: "Bearer opaque-runtime-credential",
      }),
      (error: unknown) =>
        error instanceof RuntimePolicyError && error.code === "CREDENTIAL_PERSISTENCE_FORBIDDEN",
    );
    assert.throws(
      () => store.putSession(session({
        id: "secret-session",
        blockReason: secret,
      })),
      (error: unknown) =>
        error instanceof RuntimePolicyError && error.code === "CREDENTIAL_PERSISTENCE_FORBIDDEN",
    );
    assert.throws(
      () => store.putResult("user-a", child.id, sealedResult(child.id, {
        password: "opaque-result-password",
      })),
      (error: unknown) =>
        error instanceof RuntimePolicyError && error.code === "CREDENTIAL_PERSISTENCE_FORBIDDEN",
    );
    assert.throws(
      () => store.putResult("user-a", child.id, sealedResult(child.id, { official_gt: true })),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "INVALID_CONTEXT",
    );
    assert.throws(
      () => store.getResultReference("user-a", attempts[1]!.id),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "RESULT_NOT_READY",
    );
    store.close();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
