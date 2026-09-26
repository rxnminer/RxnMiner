import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import type { RuntimeJobResult, SessionRecord } from "../src/contracts.js";
import { RuntimePolicyError } from "../src/errors.js";
import {
  DurableExecutionQueue,
  queueOwnsCancellation,
  type QueuedExtractionExecutor,
} from "../src/execution-queue.js";
import {
  SQLITE_TASK_INDEX_FILE,
  SqliteRuntimeStore,
} from "../src/sqlite-store.js";
import { DurableFileRuntimeStore } from "../src/store.js";

function putPrepared(
  store: SqliteRuntimeStore,
  sessionId: string,
  authMode: "api_key" | "pi_auth",
  initialMessage = `Extract ${sessionId}`,
): void {
  const record: SessionRecord = {
    id: sessionId,
    userId: "local-user",
    jobId: `job-${sessionId}`,
    task: "extract_all",
    contextMode: "figure",
    status: "creating",
    authMode: authMode === "pi_auth" ? "personal_codex_subscription" : "api_key",
    sandboxId: sessionId,
    createdAt: "2026-07-31T00:00:00.000Z",
    updatedAt: "2026-07-31T00:00:00.000Z",
  };
  store.putSession(record);
  store.appendEvent("local-user", sessionId, "session.created", {
    jobId: record.jobId,
    contextMode: "figure",
    source: { mode: "live_pi_sdk", localSingleUser: true },
    provider: authMode === "pi_auth" ? "openai-codex" : "openai",
    modelId: "gpt-test",
    authMode,
    requestedWorkspaceId: sessionId,
    inputArtifactIds: [],
    inputSelections: [],
    localExtractionRequest: {
      schemaVersion: "chemrxn.local-extraction-request.v1",
      dispatchRequested: true,
      contextMode: "figure",
      task: "extract_all",
      provider: authMode === "pi_auth" ? "openai-codex" : "openai",
      modelId: "gpt-test",
      authMode,
      requestedWorkspaceId: sessionId,
      inputs: [],
      initialMessage,
    },
  });
}

function enqueuePersisted(store: SqliteRuntimeStore, sessionId: string, order: number): void {
  store.enqueueWorkItem({
    userId: "local-user",
    sessionId,
    priority: 0,
    enqueuedAt: `2026-07-31T00:00:${String(order).padStart(2, "0")}.000Z`,
    requestEventSequence: 1,
  });
}

function sealReviewReady(store: SqliteRuntimeStore, sessionId: string): void {
  let current = store.getSession("local-user", sessionId);
  if (current.status === "creating" || current.status === "awaiting_user_auth") {
    current.status = "ready";
    current.updatedAt = "2026-07-31T00:01:00.000Z";
    delete current.blockReason;
    store.putSession(current);
  }
  current = store.getSession("local-user", sessionId);
  current.status = "running";
  current.updatedAt = "2026-07-31T00:01:01.000Z";
  store.putSession(current);
  current = store.getSession("local-user", sessionId);
  current.status = "review_ready";
  current.updatedAt = "2026-07-31T00:01:02.000Z";
  store.putSession(current);
  store.appendEvent("local-user", sessionId, "job.review_ready", {
    phase: "job.review_ready",
  });
}

function deferred(): { promise: Promise<void>; resolve(): void } {
  let resolvePromise!: () => void;
  const promise = new Promise<void>((resolve) => { resolvePromise = resolve; });
  return { promise, resolve: resolvePromise };
}

test("terminal dispatch history does not intercept a later interactive-turn abort", () => {
  for (const state of ["pending", "active", "blocked"] as const) {
    assert.equal(queueOwnsCancellation(state), true);
  }
  for (const state of ["completed", "failed", "cancelled", "interrupted"] as const) {
    assert.equal(queueOwnsCancellation(state), false);
  }
});

async function waitFor(
  assertion: () => boolean,
  message: string,
  timeoutMs = 2_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (assertion()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.fail(message);
}

test("durable queue enforces bounded concurrency and never starts cancelled queued work", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-execution-queue-limit-"));
  const store = new SqliteRuntimeStore(root);
  const gates = new Map([
    ["limit-a", deferred()],
    ["limit-b", deferred()],
    ["limit-c", deferred()],
  ]);
  const started: string[] = [];
  let active = 0;
  let maximumActive = 0;
  let cancelCalls = 0;
  let clockTick = 0;
  const executor: QueuedExtractionExecutor = {
    async start(_userId, sessionId, _apiKey, _direction, signal) {
      started.push(sessionId);
      active += 1;
      maximumActive = Math.max(maximumActive, active);
      await gates.get(sessionId)!.promise;
      active -= 1;
      if (!signal.aborted) sealReviewReady(store, sessionId);
    },
    async cancel() { cancelCalls += 1; },
  };
  const queue = new DurableExecutionQueue(store, store, executor, {
    concurrency: 2,
    ownerId: "queue-limit-test",
    now: () => new Date(Date.UTC(2026, 6, 31, 3, 0, 0, clockTick++)),
  });
  try {
    queue.start();
    for (const id of ["limit-a", "limit-b", "limit-c"]) putPrepared(store, id, "api_key");
    queue.enqueue({ userId: "local-user", sessionId: "limit-a", apiKey: "TEST_ONLY_limit-a-secret" });
    queue.enqueue({ userId: "local-user", sessionId: "limit-b", apiKey: "TEST_ONLY_limit-b-secret" });
    queue.enqueue({ userId: "local-user", sessionId: "limit-c", apiKey: "TEST_ONLY_limit-c-secret" });
    await waitFor(() => started.length === 2, "the first two queue items did not acquire slots");
    assert.equal(maximumActive, 2);
    assert.equal(queue.status("local-user", "limit-c").workItem.state, "pending");
    assert.equal(queue.status("local-user", "limit-c").position, 1);

    const cancelled = await queue.cancel("local-user", "limit-c");
    assert.equal(cancelled.state, "cancelled");
    assert.equal(store.getSession("local-user", "limit-c").status, "cancelled");
    assert.equal(store.listEvents("local-user", "limit-c").at(-1)?.type, "job.cancelled");
    assert.equal(started.includes("limit-c"), false);
    assert.equal(cancelCalls, 0, "queued cancellation must not touch the live executor");

    gates.get("limit-a")!.resolve();
    gates.get("limit-b")!.resolve();
    await waitFor(
      () => ["limit-a", "limit-b"].every((id) => queue.status("local-user", id).workItem.state === "completed"),
      "active queue items did not finish",
    );
    assert.equal(maximumActive, 2);
    assert.equal(started.includes("limit-c"), false);
  } finally {
    for (const gate of gates.values()) gate.resolve();
    await queue.stop();
    store.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("a terminal event does not release the worker slot before resource settlement", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-execution-queue-settlement-"));
  const store = new SqliteRuntimeStore(root);
  const cleanup = deferred();
  const started: string[] = [];
  const queue = new DurableExecutionQueue(store, store, {
    async start(_userId, sessionId) {
      started.push(sessionId);
      sealReviewReady(store, sessionId);
    },
    async cancel() {},
    async settle(_userId, sessionId) {
      if (sessionId === "settle-a") await cleanup.promise;
    },
  }, { concurrency: 1, ownerId: "queue-settlement-test" });
  try {
    queue.start();
    for (const id of ["settle-a", "settle-b"]) {
      putPrepared(store, id, "pi_auth");
      queue.enqueue({ userId: "local-user", sessionId: id });
    }
    await waitFor(() => started.length === 1, "first extraction did not start");
    await new Promise((resolve) => setTimeout(resolve, 50));
    assert.deepEqual(started, ["settle-a"]);
    assert.equal(queue.status("local-user", "settle-b").workItem.state, "pending");
    cleanup.resolve();
    await waitFor(() => started.length === 2, "second extraction did not wait for cleanup");
    await waitFor(
      () => queue.status("local-user", "settle-b").workItem.state === "completed",
      "second extraction did not complete",
    );
  } finally {
    cleanup.resolve();
    await queue.stop();
    store.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("a restarted BYO-key queue blocks until one-run credentials are supplied", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-execution-queue-byo-"));
  const first = new SqliteRuntimeStore(root);
  putPrepared(first, "byo-restart", "api_key", "Extract the selected reaction figure");
  enqueuePersisted(first, "byo-restart", 1);
  first.close();

  const restarted = new SqliteRuntimeStore(root);
  const receivedKeys: Array<string | undefined> = [];
  const directions: Array<string | undefined> = [];
  const queue = new DurableExecutionQueue(restarted, restarted, {
    async start(_userId, sessionId, apiKey, direction) {
      receivedKeys.push(apiKey);
      directions.push(direction);
      sealReviewReady(restarted, sessionId);
    },
    async cancel() {},
  }, { ownerId: "queue-byo-restart" });
  const apiKey = "TEST_ONLY_queue-memory-only-credential";
  try {
    const recovery = queue.start();
    assert.deepEqual([...recovery.pendingSessionIds], ["byo-restart"]);
    await waitFor(
      () => queue.status("local-user", "byo-restart").workItem.state === "blocked",
      "restarted BYO-key work did not block for credential re-entry",
    );
    assert.equal(restarted.getSession("local-user", "byo-restart").status, "awaiting_user_auth");
    const authEvent = restarted.listEvents("local-user", "byo-restart").at(-1);
    assert.equal(authEvent?.type, "auth.required");
    assert.equal((authEvent?.data as { code?: string }).code, "QUEUE_CREDENTIAL_REQUIRED");
    assert.deepEqual(receivedKeys, []);

    const resumed = queue.supplyApiKey("local-user", "byo-restart", apiKey);
    assert.equal(resumed.workItem.state, "pending");
    assert.equal(restarted.getSession("local-user", "byo-restart").status, "creating");
    assert.equal(restarted.getSession("local-user", "byo-restart").blockReason, undefined);
    await waitFor(
      () => queue.status("local-user", "byo-restart").workItem.state === "completed",
      "credentialed BYO-key work did not resume",
    );
    assert.deepEqual(receivedKeys, [apiKey]);
    assert.equal(queue.status("local-user", "byo-restart").workItem.lastErrorCode, undefined);
    assert.equal(restarted.getSession("local-user", "byo-restart").blockReason, undefined);
    assert.deepEqual(directions, ["Extract the selected reaction figure"]);
    const events = JSON.stringify(restarted.listEvents("local-user", "byo-restart"));
    assert.equal(events.includes(apiKey), false);
    assert.equal(events.includes("GT_SENTINEL"), false);
    await queue.stop();
    restarted.close();
    assert.equal(
      readFileSync(path.join(root, SQLITE_TASK_INDEX_FILE)).includes(Buffer.from(apiKey)),
      false,
      "the short-lived API key must never enter SQLite pages",
    );
  } finally {
    await queue.stop();
    restarted.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("a stopped queue rejects credential and enqueue mutations before state changes", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-execution-queue-stopped-"));
  const store = new SqliteRuntimeStore(root);
  const queue = new DurableExecutionQueue(store, store, {
    async start() { assert.fail("a keyless request must not activate"); },
    async cancel() {},
  }, { ownerId: "queue-stopped" });
  try {
    queue.start();
    putPrepared(store, "stopped-blocked", "api_key");
    queue.enqueue({ userId: "local-user", sessionId: "stopped-blocked" });
    await waitFor(
      () => queue.status("local-user", "stopped-blocked").workItem.state === "blocked",
      "keyless request did not block",
    );
    await queue.stop();
    assert.throws(
      () => queue.supplyApiKey("local-user", "stopped-blocked", "TEST_ONLY_never-retained"),
      (error: unknown) => error instanceof RuntimePolicyError
        && error.code === "RUNTIME_SHUTTING_DOWN",
    );
    assert.equal(queue.status("local-user", "stopped-blocked").workItem.state, "blocked");
    putPrepared(store, "stopped-new", "pi_auth");
    assert.throws(
      () => queue.enqueue({ userId: "local-user", sessionId: "stopped-new" }),
      (error: unknown) => error instanceof RuntimePolicyError
        && error.code === "RUNTIME_SHUTTING_DOWN",
    );
    assert.equal(store.getSession("local-user", "stopped-new").status, "creating");
  } finally {
    await queue.stop();
    store.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("credential recovery event retries in-process after a transient event write failure", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-execution-queue-auth-event-"));
  const store = new SqliteRuntimeStore(root);
  const originalAppend = store.appendEvent.bind(store);
  let injected = false;
  store.appendEvent = ((userId, sessionId, type, data, secrets = []) => {
    if (!injected && type === "auth.required") {
      injected = true;
      throw new Error("synthetic transient event-plane failure");
    }
    return originalAppend(userId, sessionId, type, data, secrets);
  }) as typeof store.appendEvent;
  const queue = new DurableExecutionQueue(store, store, {
    async start(_userId, sessionId) { sealReviewReady(store, sessionId); },
    async cancel() {},
  }, { ownerId: "queue-auth-event-retry" });
  try {
    queue.start();
    putPrepared(store, "auth-event-retry", "api_key");
    queue.enqueue({ userId: "local-user", sessionId: "auth-event-retry" });
    await waitFor(
      () => queue.status("local-user", "auth-event-retry").workItem.state === "blocked",
      "missing credential did not block",
    );
    await waitFor(
      () => store.listEvents("local-user", "auth-event-retry")
        .some((event) => event.type === "auth.required"),
      "auth.required was not retried without a service restart",
    );
    assert.equal(
      store.listEvents("local-user", "auth-event-retry")
        .filter((event) => event.type === "auth.required").length,
      1,
    );
    queue.supplyApiKey("local-user", "auth-event-retry", "TEST_ONLY_auth-event-one-run-key");
    await waitFor(
      () => queue.status("local-user", "auth-event-retry").workItem.state === "completed",
      "credentialed work did not complete",
    );
    await new Promise((resolve) => setTimeout(resolve, 300));
    assert.equal(
      store.listEvents("local-user", "auth-event-retry")
        .filter((event) => event.type === "auth.required").length,
      1,
      "a stale credential event must not reopen the completed run",
    );
  } finally {
    await queue.stop();
    store.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("a restarted Pi-auth queue resumes without asking for an API key", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-execution-queue-pi-auth-"));
  const first = new SqliteRuntimeStore(root);
  putPrepared(first, "pi-auth-restart", "pi_auth");
  enqueuePersisted(first, "pi-auth-restart", 1);
  first.close();

  const restarted = new SqliteRuntimeStore(root);
  const starts: Array<{ apiKey: string | undefined; direction: string | undefined }> = [];
  const queue = new DurableExecutionQueue(restarted, restarted, {
    async start(_userId, sessionId, apiKey, direction) {
      starts.push({ apiKey, direction });
      sealReviewReady(restarted, sessionId);
    },
    async cancel() {},
  }, { ownerId: "queue-pi-auth-restart" });
  try {
    queue.start();
    await waitFor(
      () => queue.status("local-user", "pi-auth-restart").workItem.state === "completed",
      "Pi-auth queued work did not resume after restart",
    );
    assert.deepEqual(starts, [{ apiKey: undefined, direction: "Extract pi-auth-restart" }]);
    assert.equal(
      restarted.listEvents("local-user", "pi-auth-restart")
        .some((event) => event.type === "auth.required"),
      false,
    );
  } finally {
    await queue.stop();
    restarted.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("active work is interrupted exactly once on queue restart and is never replayed", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-execution-queue-interrupt-"));
  const first = new SqliteRuntimeStore(root);
  putPrepared(first, "active-before-restart", "pi_auth");
  enqueuePersisted(first, "active-before-restart", 1);
  assert.equal(first.claimNextWorkItem({
    leaseOwner: "dead-worker",
    claimedAt: "2026-07-31T00:02:00.000Z",
    leaseExpiresAt: "2026-07-31T01:02:00.000Z",
    maxConcurrency: 1,
  })?.state, "active");
  first.close();

  let starts = 0;
  const restarted = new SqliteRuntimeStore(root);
  const executor: QueuedExtractionExecutor = {
    async start() { starts += 1; },
    async cancel() {},
  };
  const queue = new DurableExecutionQueue(restarted, restarted, executor, {
    ownerId: "replacement-worker",
  });
  try {
    assert.equal(queue.start().interrupted, 1);
    assert.equal(restarted.getSession("local-user", "active-before-restart").status, "interrupted");
    assert.equal(queue.status("local-user", "active-before-restart").workItem.state, "interrupted");
    assert.equal(starts, 0);
    assert.equal(queue.start().interrupted, 0);
    assert.equal(
      restarted.listEvents("local-user", "active-before-restart")
        .filter((event) => event.type === "job.interrupted").length,
      1,
    );
    await queue.stop();
    restarted.close();

    const secondRestart = new SqliteRuntimeStore(root);
    const secondQueue = new DurableExecutionQueue(secondRestart, secondRestart, executor, {
      ownerId: "second-replacement-worker",
    });
    assert.equal(secondQueue.start().interrupted, 0);
    assert.equal(starts, 0);
    assert.equal(
      secondRestart.listEvents("local-user", "active-before-restart")
        .filter((event) => event.type === "job.interrupted").length,
      1,
    );
    await secondQueue.stop();
    secondRestart.close();
  } finally {
    await queue.stop();
    restarted.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("restart completes an active no-artifact turn already returned to ready", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-execution-queue-ready-restart-"));
  const first = new SqliteRuntimeStore(root);
  putPrepared(first, "ready-before-restart", "pi_auth");
  enqueuePersisted(first, "ready-before-restart", 1);
  assert.ok(first.claimNextWorkItem({
    leaseOwner: "crashed-after-ready",
    claimedAt: "2026-07-31T00:02:00.000Z",
    leaseExpiresAt: "2026-07-31T01:02:00.000Z",
    maxConcurrency: 1,
  }));
  const ready = first.getSession("local-user", "ready-before-restart");
  ready.status = "ready";
  ready.updatedAt = "2026-07-31T00:02:01.000Z";
  first.putSession(ready);
  first.close();

  const restarted = new SqliteRuntimeStore(root);
  const queue = new DurableExecutionQueue(restarted, restarted, {
    async start() { assert.fail("a completed conversational turn must not replay"); },
    async cancel() {},
  }, { ownerId: "queue-ready-restart" });
  try {
    assert.equal(queue.start().interrupted, 0);
    assert.equal(queue.status("local-user", "ready-before-restart").workItem.state, "completed");
    assert.equal(restarted.getSession("local-user", "ready-before-restart").status, "ready");
    assert.equal(
      restarted.listEvents("local-user", "ready-before-restart")
        .some((event) => event.type === "job.interrupted"),
      false,
    );
  } finally {
    await queue.stop();
    restarted.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("a no-artifact Pi turn releases its slot when the conversation returns ready", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-execution-queue-ready-"));
  const store = new SqliteRuntimeStore(root);
  const started: string[] = [];
  const queue = new DurableExecutionQueue(store, store, {
    async start(_userId, sessionId) {
      started.push(sessionId);
      const session = store.getSession("local-user", sessionId);
      session.status = "ready";
      session.updatedAt = new Date().toISOString();
      store.putSession(session);
      // Deliberately no event: this covers the state-after-event crash/order
      // window and the legitimate "turn ended without an artifact" path.
    },
    async cancel() {},
  }, { concurrency: 1, ownerId: "queue-ready-release" });
  try {
    queue.start();
    for (const id of ["ready-a", "ready-b"]) {
      putPrepared(store, id, "pi_auth");
      queue.enqueue({ userId: "local-user", sessionId: id });
    }
    await waitFor(
      () => ["ready-a", "ready-b"].every((id) =>
        queue.status("local-user", id).workItem.state === "completed"),
      "ready conversations did not release the bounded extraction slot",
    );
    assert.deepEqual(started, ["ready-a", "ready-b"]);
  } finally {
    await queue.stop();
    store.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("a transient terminal queue write repairs itself and drains the next item", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-execution-queue-finish-repair-"));
  const store = new SqliteRuntimeStore(root);
  const originalFinish = store.finishWorkItem.bind(store);
  let failFirstFinish = true;
  store.finishWorkItem = ((input) => {
    if (failFirstFinish && input.sessionId === "finish-repair-a") {
      failFirstFinish = false;
      throw new Error("synthetic transient finish failure");
    }
    return originalFinish(input);
  }) as typeof store.finishWorkItem;
  const started: string[] = [];
  const queue = new DurableExecutionQueue(store, store, {
    async start(_userId, sessionId) {
      started.push(sessionId);
      sealReviewReady(store, sessionId);
    },
    async cancel() {},
  }, { concurrency: 1, ownerId: "queue-finish-repair" });
  try {
    queue.start();
    for (const id of ["finish-repair-a", "finish-repair-b"]) {
      putPrepared(store, id, "pi_auth");
      queue.enqueue({ userId: "local-user", sessionId: id });
    }
    await waitFor(
      () => started.length === 2,
      "repairing the first terminal write did not wake the queue for the next item",
      3_000,
    );
    await waitFor(
      () => ["finish-repair-a", "finish-repair-b"].every((id) =>
        queue.status("local-user", id).workItem.state === "completed"),
      "both repaired queue items did not converge to completed",
      3_000,
    );
    assert.deepEqual(started, ["finish-repair-a", "finish-repair-b"]);
  } finally {
    await queue.stop();
    store.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("credential block split failure converges blocked work and session to failed", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-execution-queue-block-repair-"));
  const store = new SqliteRuntimeStore(root);
  putPrepared(store, "block-split", "api_key");
  const originalPut = store.putSession.bind(store);
  let failBlockedSessionWrite = true;
  store.putSession = ((session) => {
    if (failBlockedSessionWrite && session.status === "awaiting_user_auth") {
      failBlockedSessionWrite = false;
      throw new Error("synthetic blocked-session write failure");
    }
    return originalPut(session);
  }) as typeof store.putSession;
  const queue = new DurableExecutionQueue(store, store, {
    async start() { assert.fail("a keyless BYO request must not reach the executor"); },
    async cancel() {},
  }, { ownerId: "queue-block-split" });
  try {
    queue.start();
    queue.enqueue({ userId: "local-user", sessionId: "block-split" });
    await waitFor(
      () => queue.status("local-user", "block-split").workItem.state === "failed",
      "blocked queue split failure did not converge to a terminal work item",
      3_000,
    );
    assert.equal(store.getSession("local-user", "block-split").status, "failed");
    assert.equal(store.getSession("local-user", "block-split").blockReason, "QUEUE_EXECUTION_FAILED");
    assert.equal(
      store.listEvents("local-user", "block-split")
        .some((event) => event.type === "agent.error"),
      true,
    );
  } finally {
    await queue.stop();
    store.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("startup repairs a cancelled queue row whose session/event write was interrupted", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-execution-queue-cancel-repair-"));
  const first = new SqliteRuntimeStore(root);
  putPrepared(first, "cancel-crash", "pi_auth");
  const pending = first.enqueueWorkItem({
    userId: "local-user",
    sessionId: "cancel-crash",
    priority: 0,
    enqueuedAt: "2026-07-31T00:00:01.000Z",
    requestEventSequence: 1,
  });
  first.cancelWorkItem({
    userId: "local-user",
    sessionId: "cancel-crash",
    expectedRevision: pending.revision,
    updatedAt: "2026-07-31T00:00:02.000Z",
  });
  first.close();

  const restarted = new SqliteRuntimeStore(root);
  const queue = new DurableExecutionQueue(restarted, restarted, {
    async start() { assert.fail("cancelled work must never start"); },
    async cancel() {},
  });
  try {
    queue.start();
    assert.equal(restarted.getSession("local-user", "cancel-crash").status, "cancelled");
    assert.equal(queue.status("local-user", "cancel-crash").workItem.state, "cancelled");
    assert.equal(
      restarted.listEvents("local-user", "cancel-crash")
        .filter((event) => event.type === "job.cancelled").length,
      1,
    );
  } finally {
    await queue.stop();
    restarted.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("startup terminalizes a prepared session whose durable request envelope is missing", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-execution-queue-incomplete-request-"));
  const first = new SqliteRuntimeStore(root);
  first.putSession({
    id: "incomplete-prepared",
    userId: "local-user",
    jobId: "job-incomplete-prepared",
    task: "extract_all",
    contextMode: "figure",
    status: "creating",
    authMode: "api_key",
    sandboxId: "incomplete-prepared",
    createdAt: "2026-07-31T00:00:00.000Z",
    updatedAt: "2026-07-31T00:00:00.000Z",
  });
  first.close();

  const restarted = new SqliteRuntimeStore(root);
  const queue = new DurableExecutionQueue(restarted, restarted, {
    async start() { assert.fail("an incomplete prepared request must never execute"); },
    async cancel() {},
  }, { ownerId: "queue-incomplete-request" });
  try {
    assert.equal(queue.start().recoveredPrepared, 0);
    const repaired = restarted.getSession("local-user", "incomplete-prepared");
    assert.equal(repaired.status, "interrupted");
    assert.equal(repaired.blockReason, "QUEUE_REQUEST_INCOMPLETE");
    assert.equal(
      restarted.listEvents("local-user", "incomplete-prepared")
        .filter((event) => event.type === "job.interrupted").length,
      1,
    );
    await queue.stop();
    restarted.close();

    const second = new SqliteRuntimeStore(root);
    const secondQueue = new DurableExecutionQueue(second, second, {
      async start() { assert.fail("a repaired incomplete request must remain terminal"); },
      async cancel() {},
    });
    secondQueue.start();
    assert.equal(
      second.listEvents("local-user", "incomplete-prepared")
        .filter((event) => event.type === "job.interrupted").length,
      1,
    );
    await secondQueue.stop();
    second.close();
  } finally {
    await queue.stop();
    restarted.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("startup repairs a missing failed terminal event exactly once", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-execution-queue-failed-event-"));
  const first = new SqliteRuntimeStore(root);
  putPrepared(first, "failed-event-crash", "pi_auth");
  enqueuePersisted(first, "failed-event-crash", 1);
  const active = first.claimNextWorkItem({
    leaseOwner: "failed-worker",
    claimedAt: "2026-07-31T00:03:00.000Z",
    leaseExpiresAt: "2026-07-31T01:03:00.000Z",
    maxConcurrency: 1,
  })!;
  const failed = first.getSession("local-user", "failed-event-crash");
  failed.status = "failed";
  failed.blockReason = "SYNTHETIC_FAILURE";
  failed.updatedAt = "2026-07-31T00:03:01.000Z";
  first.putSession(failed);
  first.finishWorkItem({
    userId: "local-user",
    sessionId: "failed-event-crash",
    expectedRevision: active.revision,
    state: "failed",
    updatedAt: "2026-07-31T00:03:02.000Z",
    errorCode: "SYNTHETIC_FAILURE",
  });
  first.close();

  const countRecoveredFailures = (store: SqliteRuntimeStore): number =>
    store.listEvents("local-user", "failed-event-crash").filter((event) =>
      event.type === "agent.error"
      && (event.data as { source?: string })?.source === "queue_recovery").length;
  const restarted = new SqliteRuntimeStore(root);
  const queue = new DurableExecutionQueue(restarted, restarted, {
    async start() { assert.fail("terminal failed work must not replay"); },
    async cancel() {},
  });
  try {
    queue.start();
    assert.equal(countRecoveredFailures(restarted), 1);
    await queue.stop();
    restarted.close();

    const second = new SqliteRuntimeStore(root);
    const secondQueue = new DurableExecutionQueue(second, second, {
      async start() { assert.fail("terminal failed work must not replay"); },
      async cancel() {},
    });
    secondQueue.start();
    assert.equal(countRecoveredFailures(second), 1);
    await secondQueue.stop();
    second.close();
  } finally {
    await queue.stop();
    restarted.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("verified immutable result wins recovery after session commit loss", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-execution-queue-result-repair-"));
  const first = new SqliteRuntimeStore(root);
  putPrepared(first, "sealed-crash", "pi_auth");
  enqueuePersisted(first, "sealed-crash", 1);
  assert.ok(first.claimNextWorkItem({
    leaseOwner: "crashed-finalizer",
    claimedAt: "2026-07-31T00:01:00.000Z",
    leaseExpiresAt: "2026-07-31T01:01:00.000Z",
    maxConcurrency: 1,
  }));
  const mutableFailure = first.getSession("local-user", "sealed-crash");
  mutableFailure.status = "failed";
  mutableFailure.blockReason = "LIVE_RESULT_MATERIALIZATION_FAILED";
  mutableFailure.updatedAt = "2026-07-31T00:01:01.000Z";
  first.putSession(mutableFailure);
  first.close();

  const result: RuntimeJobResult = {
    schemaVersion: "chemrxn.runtime-result.v1",
    sessionId: "sealed-crash",
    jobId: "job-sealed-crash",
    contextMode: "figure",
    source: {
      mode: "live_pi_sdk",
      reconstructed: false,
      liveEventCapture: true,
      inputArtifactIds: ["input-a"],
      workspaceId: "sealed-crash",
    },
    domainContract: "chemrxn.figure-job.v1",
    terminalStatus: "review_ready",
    manifest: { counts: { reactions: 1 } },
    createdAt: "2026-07-31T00:01:02.000Z",
    gtIncluded: false,
  };
  new DurableFileRuntimeStore(root).putResult("local-user", "sealed-crash", result);
  const resultPath = path.join(root, "sessions", "sealed-crash", "result.json");
  const before = readFileSync(resultPath);

  const restarted = new SqliteRuntimeStore(root);
  assert.equal(
    restarted.listEvents("local-user", "sealed-crash")
      .filter((event) => event.type === "job.review_ready").length,
    1,
    "store startup repairs terminal cursors even when no executor is configured",
  );
  const queue = new DurableExecutionQueue(restarted, restarted, {
    async start() { assert.fail("sealed work must not replay"); },
    async cancel() {},
  });
  try {
    assert.equal(restarted.getSession("local-user", "sealed-crash").status, "review_ready");
    assert.equal(queue.start().interrupted, 0);
    assert.equal(queue.status("local-user", "sealed-crash").workItem.state, "completed");
    const task = restarted.listTasks("local-user")[0]!;
    assert.equal(task.state, "review_ready");
    const attempt = restarted.listAttempts("local-user", task.id)[0]!;
    assert.equal(attempt.runtimeStatus, "review_ready");
    assert.equal(attempt.state, "review_ready");
    assert.deepEqual(readFileSync(resultPath), before);
    assert.equal(
      restarted.listEvents("local-user", "sealed-crash")
        .some((event) => event.type === "job.interrupted"),
      false,
    );
    assert.equal(
      restarted.listEvents("local-user", "sealed-crash")
        .filter((event) => event.type === "job.review_ready").length,
      1,
      "cursor-only clients receive one recovered immutable-result terminal event",
    );
    queue.start();
    assert.equal(
      restarted.listEvents("local-user", "sealed-crash")
        .filter((event) => event.type === "job.review_ready").length,
      1,
    );
  } finally {
    await queue.stop();
    restarted.close();
    rmSync(root, { recursive: true, force: true });
  }
});
