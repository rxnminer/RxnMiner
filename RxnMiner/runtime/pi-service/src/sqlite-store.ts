import { createHash } from "node:crypto";
import {
  chmodSync,
  existsSync,
  readFileSync,
} from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import type {
  RuntimeEvent,
  RuntimeJobResult,
  SessionRecord,
} from "./contracts.js";
import { RuntimePolicyError } from "./errors.js";
import { runtimeJobResultSchema, sessionRecordSchema } from "./schemas.js";
import {
  assertNoGroundTruth,
  assertNoPersistedSecrets,
  redactSecrets,
} from "./security.js";
import { DurableFileRuntimeStore, type RuntimeStore } from "./store.js";
import {
  EXECUTION_WORK_ITEM_VERSION,
  type ExecutionQueueStore,
  type ExecutionWorkItem,
  type ExecutionWorkItemState,
} from "./execution-queue.js";
import {
  TASK_ATTEMPT_ENVELOPE_VERSION,
  TASK_ENVELOPE_VERSION,
  TASK_EVENT_REFERENCE_VERSION,
  TASK_RESULT_REFERENCE_VERSION,
  advanceTaskState,
  assertRuntimeStatusTransition,
  taskProgressFromEvent,
  taskStateForImportedSession,
  taskStateFromEvent,
  taskStateFromSession,
  type AttemptTransition,
  type TaskAttemptEnvelope,
  type TaskAttemptRelation,
  type TaskEnvelope,
  type TaskEventReference,
  type TaskProgress,
  type TaskResultReference,
  type TaskState,
} from "./task-plane.js";

export const SQLITE_TASK_INDEX_SCHEMA_VERSION = 3;
export const SQLITE_TASK_INDEX_FILE = "task-index.sqlite3";

const SESSION_INDEX = Symbol("chemrxn.sqlite-session-index");

type IndexedSessionRecord = SessionRecord & {
  [SESSION_INDEX]?: {
    attemptId: string;
    sessionRevision: number;
  };
};

interface Migration {
  version: number;
  name: string;
  sql: string;
}

const MIGRATIONS: readonly Migration[] = [
  {
    version: 1,
    name: "task-index-core",
    sql: `
      CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        root_session_id TEXT NOT NULL,
        job_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        state TEXT NOT NULL,
        active_attempt_id TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        progress_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (tenant_id, root_session_id)
      ) STRICT;

      CREATE TABLE attempts (
        id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        tenant_id TEXT NOT NULL,
        session_id TEXT NOT NULL UNIQUE,
        ordinal INTEGER NOT NULL CHECK (ordinal > 0),
        relation TEXT NOT NULL,
        parent_attempt_id TEXT REFERENCES attempts(id),
        job_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        context_mode TEXT,
        input_names_json TEXT,
        parent_session_id TEXT,
        parent_result_sha256 TEXT,
        runtime_status TEXT NOT NULL,
        state TEXT NOT NULL,
        auth_mode TEXT NOT NULL,
        sandbox_id TEXT,
        block_reason TEXT,
        session_created_at TEXT NOT NULL,
        session_updated_at TEXT NOT NULL,
        index_updated_at TEXT NOT NULL,
        session_locator TEXT NOT NULL,
        session_sha256 TEXT NOT NULL,
        last_event_seq INTEGER NOT NULL DEFAULT 0 CHECK (last_event_seq >= 0),
        result_id TEXT,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        session_revision INTEGER NOT NULL DEFAULT 1 CHECK (session_revision > 0),
        progress_json TEXT,
        UNIQUE (task_id, ordinal)
      ) STRICT;

      CREATE TABLE event_references (
        id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
        tenant_id TEXT NOT NULL,
        seq INTEGER NOT NULL CHECK (seq > 0),
        type TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        artifact_locator TEXT NOT NULL,
        artifact_sha256 TEXT NOT NULL,
        UNIQUE (attempt_id, seq)
      ) STRICT;

      CREATE TABLE result_references (
        id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id) ON DELETE CASCADE,
        tenant_id TEXT NOT NULL,
        runtime_schema_version TEXT NOT NULL,
        artifact_locator TEXT NOT NULL,
        artifact_sha256 TEXT NOT NULL,
        sealed_at TEXT NOT NULL
      ) STRICT;
    `,
  },
  {
    version: 2,
    name: "task-index-lookups",
    sql: `
      CREATE INDEX tasks_tenant_updated_idx
        ON tasks (tenant_id, updated_at DESC, id);
      CREATE INDEX attempts_tenant_session_idx
        ON attempts (tenant_id, session_id);
      CREATE INDEX attempts_task_ordinal_idx
        ON attempts (task_id, ordinal);
      CREATE INDEX event_references_attempt_seq_idx
        ON event_references (attempt_id, seq);
      CREATE INDEX result_references_tenant_attempt_idx
        ON result_references (tenant_id, attempt_id);
    `,
  },
  {
    version: 3,
    name: "durable-execution-queue",
    sql: `
      CREATE TABLE execution_work_items (
        id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
        session_id TEXT NOT NULL REFERENCES attempts(session_id) ON DELETE CASCADE,
        phase TEXT NOT NULL CHECK (phase = 'initial_extraction'),
        request_event_seq INTEGER NOT NULL CHECK (request_event_seq > 0),
        state TEXT NOT NULL CHECK (
          state IN ('pending', 'active', 'blocked', 'completed', 'failed', 'cancelled', 'interrupted')
        ),
        priority INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN -100 AND 100),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        activation_count INTEGER NOT NULL DEFAULT 0 CHECK (activation_count >= 0),
        enqueued_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        lease_owner TEXT,
        lease_expires_at TEXT,
        started_at TEXT,
        finished_at TEXT,
        last_error_code TEXT,
        UNIQUE (attempt_id, phase),
        FOREIGN KEY (attempt_id, request_event_seq)
          REFERENCES event_references(attempt_id, seq),
        CHECK ((state = 'active') = (lease_owner IS NOT NULL)),
        CHECK ((state = 'active') = (lease_expires_at IS NOT NULL)),
        CHECK (state NOT IN ('completed', 'failed', 'cancelled', 'interrupted') OR finished_at IS NOT NULL)
      ) STRICT;

      CREATE INDEX execution_work_items_dispatch_idx
        ON execution_work_items (state, priority DESC, enqueued_at, id);
      CREATE INDEX execution_work_items_tenant_session_idx
        ON execution_work_items (tenant_id, session_id);
    `,
  },
] as const;

interface TaskRow {
  id: string;
  schema_version: string;
  tenant_id: string;
  root_session_id: string;
  job_id: string;
  kind: SessionRecord["task"];
  state: TaskState;
  active_attempt_id: string | null;
  attempt_count: number;
  revision: number;
  progress_json: string | null;
  created_at: string;
  updated_at: string;
}

interface AttemptRow {
  id: string;
  schema_version: string;
  task_id: string;
  tenant_id: string;
  session_id: string;
  ordinal: number;
  relation: TaskAttemptRelation;
  parent_attempt_id: string | null;
  job_id: string;
  kind: SessionRecord["task"];
  context_mode: SessionRecord["contextMode"] | null;
  input_names_json: string | null;
  parent_session_id: string | null;
  parent_result_sha256: string | null;
  runtime_status: SessionRecord["status"];
  state: TaskState;
  auth_mode: SessionRecord["authMode"];
  sandbox_id: string | null;
  block_reason: string | null;
  session_created_at: string;
  session_updated_at: string;
  index_updated_at: string;
  session_locator: string;
  session_sha256: string;
  last_event_seq: number;
  result_id: string | null;
  revision: number;
  session_revision: number;
  progress_json: string | null;
}

interface EventReferenceRow {
  id: string;
  schema_version: string;
  task_id: string;
  attempt_id: string;
  tenant_id: string;
  seq: number;
  type: RuntimeEvent["type"];
  occurred_at: string;
  artifact_locator: string;
  artifact_sha256: string;
}

interface ResultReferenceRow {
  id: string;
  schema_version: string;
  task_id: string;
  attempt_id: string;
  tenant_id: string;
  runtime_schema_version: RuntimeJobResult["schemaVersion"];
  artifact_locator: string;
  artifact_sha256: string;
  sealed_at: string;
}

interface ExecutionWorkItemRow {
  id: string;
  schema_version: typeof EXECUTION_WORK_ITEM_VERSION;
  tenant_id: string;
  task_id: string;
  attempt_id: string;
  session_id: string;
  phase: "initial_extraction";
  request_event_seq: number;
  state: ExecutionWorkItemState;
  priority: number;
  revision: number;
  activation_count: number;
  enqueued_at: string;
  updated_at: string;
  lease_owner: string | null;
  lease_expires_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  last_error_code: string | null;
}

/**
 * SQLite is the authoritative small task/index plane. Existing file artifacts
 * remain the payload plane so old sessions, numbered event JSON, and sealed
 * result.json files continue to work without shape or byte migrations.
 */
export class SqliteRuntimeStore implements RuntimeStore, ExecutionQueueStore {
  readonly root: string;
  readonly databasePath: string;
  private readonly artifacts: DurableFileRuntimeStore;
  private readonly database: DatabaseSync;

  constructor(root: string) {
    this.root = path.resolve(root);
    this.artifacts = new DurableFileRuntimeStore(this.root);
    this.databasePath = path.join(this.root, SQLITE_TASK_INDEX_FILE);
    this.database = new DatabaseSync(this.databasePath, {
      enableForeignKeyConstraints: true,
      allowExtension: false,
    });
    try {
      this.database.exec("PRAGMA busy_timeout = 5000");
      this.database.exec("PRAGMA journal_mode = WAL");
      this.database.exec("PRAGMA synchronous = FULL");
      this.database.exec("PRAGMA foreign_keys = ON");
      this.database.exec("PRAGMA trusted_schema = OFF");
      this.runMigrations();
      this.hardenDatabaseFiles();
      this.reconcileFileArtifacts();
      this.hardenDatabaseFiles();
    } catch (error) {
      this.database.close();
      throw error;
    }
  }

  close(): void {
    if (this.database.isOpen) this.database.close();
  }

  get schemaVersion(): number {
    const row = this.database.prepare("PRAGMA user_version").get() as
      | { user_version: number }
      | undefined;
    return row?.user_version ?? 0;
  }

  appliedMigrations(): Array<{ version: number; name: string; appliedAt: string }> {
    return this.database.prepare(`
      SELECT version, name, applied_at
      FROM schema_migrations
      ORDER BY version
    `).all().map((row) => ({
      version: numberColumn(row, "version"),
      name: stringColumn(row, "name"),
      appliedAt: stringColumn(row, "applied_at"),
    }));
  }

  putSession(record: SessionRecord): void {
    assertNoGroundTruth(record);
    assertNoPersistedSecrets(record);
    const marker = (record as IndexedSessionRecord)[SESSION_INDEX];
    const parsed = sessionRecordSchema.parse(record) as SessionRecord;
    const persisted = this.transaction(() => {
      const existing = this.attemptBySession(parsed.id);
      if (!existing) {
        if (marker) throw storeConflict("Session index disappeared during update");
        return this.insertSession(parsed);
      }
      this.assertAttemptTenant(existing, parsed.userId);
      if (!marker) {
        throw storeConflict("Existing sessions require a revision-bearing read before update");
      }
      if (
        marker.attemptId !== existing.id
        || marker.sessionRevision !== existing.session_revision
      ) {
        throw storeConflict("Session state changed before this update");
      }
      this.updateSession(existing, parsed, {
        column: "session_revision",
        expected: marker.sessionRevision,
      });
      return this.requiredAttemptById(existing.id);
    });
    attachSessionIndex(record, persisted);
  }

  getSession(userId: string, sessionId: string): SessionRecord {
    const row = this.attemptBySession(sessionId);
    if (!row) throw new RuntimePolicyError("SESSION_NOT_FOUND", "Session not found", 404);
    this.assertAttemptTenant(row, userId);
    const record = sessionFromAttempt(row);
    attachSessionIndex(record, row);
    return record;
  }

  listSessions(userId: string): SessionRecord[] {
    return this.attemptRows(`
      SELECT * FROM attempts
      WHERE tenant_id = ?
      ORDER BY session_updated_at DESC, session_id
    `, userId).map((row) => {
      const record = sessionFromAttempt(row);
      attachSessionIndex(record, row);
      return record;
    });
  }

  listAllSessions(): SessionRecord[] {
    return this.attemptRows(`
      SELECT * FROM attempts
      ORDER BY session_updated_at DESC, session_id
    `).map((row) => {
      const record = sessionFromAttempt(row);
      attachSessionIndex(record, row);
      return record;
    });
  }

  appendEvent(
    userId: string,
    sessionId: string,
    type: RuntimeEvent["type"],
    data: unknown,
    secrets: readonly string[] = [],
  ): RuntimeEvent {
    this.getSession(userId, sessionId);
    assertNoGroundTruth(data);
    const safeData = redactSecrets(data, secrets);
    assertNoGroundTruth(safeData);
    assertNoPersistedSecrets(safeData);
    this.transaction(() => {
      const attempt = this.requiredAttemptBySession(sessionId);
      this.assertAttemptTenant(attempt, userId);
      this.reconcileEvents(attempt);
    });
    // The file store notifies SSE listeners synchronously. Publish the durable
    // event outside a SQLite transaction so the subscription wrapper can index
    // it without recursively issuing BEGIN on this connection. A crash between
    // file publication and indexing is recovered by the next reconciliation.
    const event = this.artifacts.appendEvent(userId, sessionId, type, safeData);
    this.transaction(() => {
      const attempt = this.requiredAttemptBySession(sessionId);
      this.indexEvent(attempt, event);
    });
    return event;
  }

  listEvents(userId: string, sessionId: string, after = 0): RuntimeEvent[] {
    this.getSession(userId, sessionId);
    const rows = this.transaction(() => {
      const attempt = this.requiredAttemptBySession(sessionId);
      this.reconcileEvents(attempt);
      return this.eventRows(`
        SELECT * FROM event_references
        WHERE attempt_id = ? AND seq > ?
        ORDER BY seq
      `, attempt.id, after);
    });
    const bySequence = new Map(
      this.artifacts.listEvents(userId, sessionId, after).map((event) => [event.seq, event]),
    );
    return rows.map((row) => {
      const event = bySequence.get(row.seq);
      if (!event) throw storeCorrupt(`Indexed event ${row.seq} is missing`);
      this.verifyArtifact(row.artifact_locator, row.artifact_sha256, "runtime event");
      return structuredClone(event);
    });
  }

  subscribeEvents(sessionId: string, listener: (event: RuntimeEvent) => void): () => void {
    const indexed = this.attemptBySession(sessionId);
    if (!indexed) throw new RuntimePolicyError("SESSION_NOT_FOUND", "Session not found", 404);
    return this.artifacts.subscribeEvents(sessionId, (event) => {
      this.transaction(() => {
        const attempt = this.requiredAttemptBySession(sessionId);
        this.indexEvent(attempt, event);
      });
      listener(event);
    });
  }

  putResult(userId: string, sessionId: string, result: RuntimeJobResult): void {
    this.getSession(userId, sessionId);
    assertNoGroundTruth(result);
    assertNoPersistedSecrets(result);
    const parsed = runtimeJobResultSchema.parse(result) as RuntimeJobResult;
    this.transaction(() => {
      const attempt = this.requiredAttemptBySession(sessionId);
      this.assertAttemptTenant(attempt, userId);
      this.assertResultMatchesAttempt(attempt, parsed);
      this.artifacts.putResult(userId, sessionId, parsed);
      this.indexResult(attempt, parsed);
    });
  }

  getResult(userId: string, sessionId: string): RuntimeJobResult {
    this.getSession(userId, sessionId);
    const reference = this.transaction(() => {
      const attempt = this.requiredAttemptBySession(sessionId);
      this.reconcileResult(attempt);
      return this.resultRowByAttempt(attempt.id);
    });
    if (!reference) throw new RuntimePolicyError("RESULT_NOT_READY", "Result is not ready", 409);
    this.verifyArtifact(reference.artifact_locator, reference.artifact_sha256, "runtime result");
    return this.artifacts.getResult(userId, sessionId);
  }

  listTasks(userId: string): TaskEnvelope[] {
    return this.taskRows(`
      SELECT * FROM tasks
      WHERE tenant_id = ?
      ORDER BY updated_at DESC, id
    `, userId).map(taskEnvelope);
  }

  getTask(userId: string, taskId: string): TaskEnvelope {
    const row = this.taskById(taskId);
    if (!row) throw new RuntimePolicyError("TASK_NOT_FOUND", "Task not found", 404);
    this.assertTaskTenant(row, userId);
    return taskEnvelope(row);
  }

  listAttempts(userId: string, taskId: string): TaskAttemptEnvelope[] {
    const task = this.taskById(taskId);
    if (!task) throw new RuntimePolicyError("TASK_NOT_FOUND", "Task not found", 404);
    this.assertTaskTenant(task, userId);
    return this.attemptRows(`
      SELECT * FROM attempts
      WHERE task_id = ?
      ORDER BY ordinal
    `, taskId).map(taskAttemptEnvelope);
  }

  getAttempt(userId: string, attemptId: string): TaskAttemptEnvelope {
    const row = this.attemptById(attemptId);
    if (!row) throw new RuntimePolicyError("ATTEMPT_NOT_FOUND", "Task attempt not found", 404);
    this.assertAttemptTenant(row, userId);
    return taskAttemptEnvelope(row);
  }

  listEventReferences(
    userId: string,
    attemptId: string,
    after = 0,
  ): TaskEventReference[] {
    const attempt = this.attemptById(attemptId);
    if (!attempt) throw new RuntimePolicyError("ATTEMPT_NOT_FOUND", "Task attempt not found", 404);
    this.assertAttemptTenant(attempt, userId);
    const rows = this.transaction(() => {
      this.reconcileEvents(this.requiredAttemptById(attemptId));
      return this.eventRows(`
        SELECT * FROM event_references
        WHERE attempt_id = ? AND seq > ?
        ORDER BY seq
      `, attemptId, after);
    });
    return rows.map(eventReferenceEnvelope);
  }

  getResultReference(userId: string, attemptId: string): TaskResultReference {
    const attempt = this.attemptById(attemptId);
    if (!attempt) throw new RuntimePolicyError("ATTEMPT_NOT_FOUND", "Task attempt not found", 404);
    this.assertAttemptTenant(attempt, userId);
    const row = this.transaction(() => {
      this.reconcileResult(this.requiredAttemptById(attemptId));
      return this.resultRowByAttempt(attemptId);
    });
    if (!row) throw new RuntimePolicyError("RESULT_NOT_READY", "Result is not ready", 409);
    return resultReferenceEnvelope(row);
  }

  transitionAttempt(
    userId: string,
    attemptId: string,
    expectedRevision: number,
    transition: AttemptTransition,
  ): TaskAttemptEnvelope {
    if (!Number.isSafeInteger(expectedRevision) || expectedRevision < 1) {
      throw storeConflict("Invalid attempt revision");
    }
    this.transaction(() => {
      const current = this.requiredAttemptById(attemptId);
      this.assertAttemptTenant(current, userId);
      if (current.revision !== expectedRevision) {
        throw storeConflict("Task attempt changed before this transition");
      }
      const next = sessionFromAttempt(current);
      next.status = transition.status;
      next.updatedAt = transition.updatedAt;
      if (typeof transition.blockReason === "string") {
        next.blockReason = transition.blockReason;
      } else if (transition.blockReason === null) {
        delete next.blockReason;
      }
      const parsed = sessionRecordSchema.parse(next) as SessionRecord;
      assertNoGroundTruth(parsed);
      assertNoPersistedSecrets(parsed);
      this.updateSession(current, parsed, { column: "revision", expected: expectedRevision });
    });
    return taskAttemptEnvelope(this.requiredAttemptById(attemptId));
  }

  enqueueWorkItem(input: {
    userId: string;
    sessionId: string;
    priority: number;
    enqueuedAt: string;
    requestEventSequence: number;
  }): ExecutionWorkItem {
    if (!Number.isSafeInteger(input.priority) || input.priority < -100 || input.priority > 100) {
      throw new RuntimePolicyError("INVALID_CONTEXT", "Invalid queue priority", 400);
    }
    if (!Number.isSafeInteger(input.requestEventSequence) || input.requestEventSequence < 1) {
      throw new RuntimePolicyError("INVALID_CONTEXT", "Invalid dispatch request event", 400);
    }
    return this.transaction(() => {
      const attempt = this.requiredAttemptBySession(input.sessionId);
      this.assertAttemptTenant(attempt, input.userId);
      this.reconcileEvents(attempt);
      const existing = this.workItemBySession(input.sessionId);
      if (existing) return executionWorkItemEnvelope(existing);
      const requestEvent = this.eventRowByAttemptSequence(
        attempt.id,
        input.requestEventSequence,
      );
      if (!requestEvent || requestEvent.type !== "session.created") {
        throw new RuntimePolicyError(
          "DISPATCH_REQUEST_CORRUPT",
          "The durable extraction request event is missing or invalid",
          409,
        );
      }
      const session = sessionFromAttempt(attempt);
      if (session.status !== "creating") {
        throw new RuntimePolicyError(
          "SESSION_NOT_ACTIVE",
          `Only a prepared session can be queued (${session.status})`,
          409,
        );
      }
      const id = canonicalId("wrk", input.userId, input.sessionId, "initial_extraction");
      this.database.prepare(`
        INSERT INTO execution_work_items (
          id, schema_version, tenant_id, task_id, attempt_id, session_id,
          phase, request_event_seq, state, priority, revision, activation_count,
          enqueued_at, updated_at, lease_owner, lease_expires_at,
          started_at, finished_at, last_error_code
        ) VALUES (?, ?, ?, ?, ?, ?, 'initial_extraction', ?, 'pending', ?, 1, 0,
          ?, ?, NULL, NULL, NULL, NULL, NULL)
      `).run(
        id,
        EXECUTION_WORK_ITEM_VERSION,
        input.userId,
        attempt.task_id,
        attempt.id,
        input.sessionId,
        input.requestEventSequence,
        input.priority,
        input.enqueuedAt,
        input.enqueuedAt,
      );
      return executionWorkItemEnvelope(this.requiredWorkItemBySession(input.sessionId));
    });
  }

  getWorkItem(userId: string, sessionId: string): ExecutionWorkItem {
    const row = this.workItemBySession(sessionId);
    if (!row) throw new RuntimePolicyError("WORK_ITEM_NOT_FOUND", "Queued work not found", 404);
    this.assertWorkItemTenant(row, userId);
    return executionWorkItemEnvelope(row);
  }

  listPendingWorkItems(): ExecutionWorkItem[] {
    return this.workItemRows(`
      SELECT * FROM execution_work_items
      WHERE state = 'pending'
      ORDER BY priority DESC, enqueued_at, id
    `).map(executionWorkItemEnvelope);
  }

  listBlockedWorkItems(): ExecutionWorkItem[] {
    return this.workItemRows(`
      SELECT * FROM execution_work_items
      WHERE state = 'blocked'
      ORDER BY updated_at, id
    `).map(executionWorkItemEnvelope);
  }

  listTerminalWorkItems(): ExecutionWorkItem[] {
    return this.workItemRows(`
      SELECT * FROM execution_work_items
      WHERE state IN ('completed', 'failed', 'cancelled', 'interrupted')
      ORDER BY updated_at, id
    `).map(executionWorkItemEnvelope);
  }

  queuePosition(userId: string, sessionId: string): number | null {
    const row = this.workItemBySession(sessionId);
    if (!row) throw new RuntimePolicyError("WORK_ITEM_NOT_FOUND", "Queued work not found", 404);
    this.assertWorkItemTenant(row, userId);
    if (row.state !== "pending") return null;
    const ahead = this.database.prepare(`
      SELECT COUNT(*) AS count
      FROM execution_work_items
      WHERE state = 'pending' AND (
        priority > ?
        OR (priority = ? AND enqueued_at < ?)
        OR (priority = ? AND enqueued_at = ? AND id < ?)
      )
    `).get(
      row.priority,
      row.priority,
      row.enqueued_at,
      row.priority,
      row.enqueued_at,
      row.id,
    );
    return numberColumn(ahead, "count") + 1;
  }

  claimNextWorkItem(input: {
    leaseOwner: string;
    claimedAt: string;
    leaseExpiresAt: string;
    maxConcurrency: number;
  }): ExecutionWorkItem | undefined {
    if (
      !/^[A-Za-z0-9._:-]{1,200}$/.test(input.leaseOwner)
      || !Number.isSafeInteger(input.maxConcurrency)
      || input.maxConcurrency < 1
      || input.maxConcurrency > 16
    ) {
      throw new RuntimePolicyError("INVALID_CONTEXT", "Invalid queue lease request", 400);
    }
    return this.transaction(() => {
      const active = numberColumn(this.database.prepare(`
        SELECT COUNT(*) AS count FROM execution_work_items WHERE state = 'active'
      `).get(), "count");
      if (active >= input.maxConcurrency) return undefined;
      const next = castRow<ExecutionWorkItemRow>(this.database.prepare(`
        SELECT * FROM execution_work_items
        WHERE state = 'pending'
        ORDER BY priority DESC, enqueued_at, id
        LIMIT 1
      `).get());
      if (!next) return undefined;
      const changed = this.database.prepare(`
        UPDATE execution_work_items
        SET state = 'active', lease_owner = ?, lease_expires_at = ?,
            started_at = COALESCE(started_at, ?), updated_at = ?,
            activation_count = activation_count + 1, revision = revision + 1,
            last_error_code = NULL, finished_at = NULL
        WHERE id = ? AND state = 'pending' AND revision = ?
      `).run(
        input.leaseOwner,
        input.leaseExpiresAt,
        input.claimedAt,
        input.claimedAt,
        next.id,
        next.revision,
      );
      if (Number(changed.changes) !== 1) return undefined;
      return executionWorkItemEnvelope(this.requiredWorkItemBySession(next.session_id));
    });
  }

  blockWorkItem(input: {
    userId: string;
    sessionId: string;
    expectedRevision: number;
    updatedAt: string;
    errorCode: string;
  }): ExecutionWorkItem {
    return this.transitionWorkItem(input, ["active"], "blocked", {
      lastErrorCode: input.errorCode,
    });
  }

  resumeWorkItem(input: {
    userId: string;
    sessionId: string;
    expectedRevision: number;
    updatedAt: string;
  }): ExecutionWorkItem {
    return this.transitionWorkItem(input, ["blocked"], "pending", {
      lastErrorCode: null,
    });
  }

  cancelWorkItem(input: {
    userId: string;
    sessionId: string;
    expectedRevision: number;
    updatedAt: string;
  }): ExecutionWorkItem {
    return this.transitionWorkItem(input, ["pending", "blocked"], "cancelled", {
      finishedAt: input.updatedAt,
      lastErrorCode: null,
    });
  }

  finishWorkItem(input: {
    userId: string;
    sessionId: string;
    expectedRevision: number;
    state: Extract<ExecutionWorkItemState,
      "completed" | "failed" | "cancelled" | "interrupted">;
    updatedAt: string;
    errorCode?: string;
  }): ExecutionWorkItem {
    return this.transitionWorkItem(input, ["active", "blocked"], input.state, {
      finishedAt: input.updatedAt,
      lastErrorCode: input.errorCode ?? null,
    });
  }

  recoverActiveWorkItems(recoveredAt: string): ExecutionWorkItem[] {
    return this.transaction(() => {
      const active = this.workItemRows(`
        SELECT * FROM execution_work_items WHERE state = 'active' ORDER BY id
      `);
      for (const row of active) {
        const attempt = this.requiredAttemptById(row.attempt_id);
        const target = recoveredWorkItemState(attempt.runtime_status);
        const errorCode = target === "interrupted"
          ? "QUEUE_WORKER_RESTART"
          : attempt.block_reason;
        const changed = this.database.prepare(`
          UPDATE execution_work_items
          SET state = ?, lease_owner = NULL, lease_expires_at = NULL,
              finished_at = ?, updated_at = ?, last_error_code = ?, revision = revision + 1
          WHERE id = ? AND state = 'active' AND revision = ?
        `).run(
          target,
          recoveredAt,
          recoveredAt,
          errorCode,
          row.id,
          row.revision,
        );
        if (Number(changed.changes) !== 1) throw storeConflict("Queue recovery raced");
      }
      return active.map((row) =>
        executionWorkItemEnvelope(this.requiredWorkItemBySession(row.session_id)));
    });
  }

  private transitionWorkItem(
    input: {
      userId: string;
      sessionId: string;
      expectedRevision: number;
      updatedAt: string;
    },
    from: ExecutionWorkItemState[],
    to: ExecutionWorkItemState,
    changes: { finishedAt?: string; lastErrorCode?: string | null } = {},
  ): ExecutionWorkItem {
    return this.transaction(() => {
      const current = this.requiredWorkItemBySession(input.sessionId);
      this.assertWorkItemTenant(current, input.userId);
      if (current.revision !== input.expectedRevision || !from.includes(current.state)) {
        throw storeConflict("Queued work changed before this transition");
      }
      const placeholders = from.map(() => "?").join(", ");
      const terminal = ["completed", "failed", "cancelled", "interrupted"].includes(to);
      const changed = this.database.prepare(`
        UPDATE execution_work_items
        SET state = ?, updated_at = ?, lease_owner = NULL, lease_expires_at = NULL,
            finished_at = ?, last_error_code = ?, revision = revision + 1
        WHERE id = ? AND revision = ? AND state IN (${placeholders})
      `).run(
        to,
        input.updatedAt,
        terminal ? (changes.finishedAt ?? input.updatedAt) : null,
        changes.lastErrorCode ?? null,
        current.id,
        input.expectedRevision,
        ...from,
      );
      if (Number(changed.changes) !== 1) {
        throw storeConflict("Queued work changed before this transition");
      }
      return executionWorkItemEnvelope(this.requiredWorkItemBySession(input.sessionId));
    });
  }

  private runMigrations(): void {
    this.database.exec(`
      CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        checksum TEXT NOT NULL,
        applied_at TEXT NOT NULL
      ) STRICT
    `);
    const current = this.schemaVersion;
    if (current > SQLITE_TASK_INDEX_SCHEMA_VERSION) {
      throw new Error(
        `Task index schema ${current} is newer than supported ${SQLITE_TASK_INDEX_SCHEMA_VERSION}`,
      );
    }
    for (const migration of MIGRATIONS) {
      const checksum = sha256(Buffer.from(migration.sql));
      this.transaction(() => {
        // Re-read under BEGIN IMMEDIATE. Two accidentally concurrent local
        // launchers may both observe the old user_version before either owns
        // the migration lock; only the winner executes DDL.
        const row = this.database.prepare(`
          SELECT name, checksum FROM schema_migrations WHERE version = ?
        `).get(migration.version);
        if (row) {
          if (
            stringColumn(row, "name") !== migration.name
            || stringColumn(row, "checksum") !== checksum
          ) {
            throw storeCorrupt(`Task index migration ${migration.version} was modified`);
          }
          return;
        }
        this.database.exec(migration.sql);
        this.database.prepare(`
          INSERT INTO schema_migrations (version, name, checksum, applied_at)
          VALUES (?, ?, ?, ?)
        `).run(migration.version, migration.name, checksum, new Date().toISOString());
        this.database.exec(`PRAGMA user_version = ${migration.version}`);
      });
    }
    if (this.schemaVersion !== SQLITE_TASK_INDEX_SCHEMA_VERSION) {
      throw storeCorrupt("Task index migrations are incomplete");
    }
  }

  private reconcileFileArtifacts(): void {
    const sessions = this.artifacts.listAllSessions();
    const byIdentity = new Map(sessions.map((session) => [
      sessionIdentity(session.userId, session.id),
      session,
    ]));
    const completed = new Set<string>();
    const visiting = new Set<string>();
    const ensure = (session: SessionRecord): void => {
      const identity = sessionIdentity(session.userId, session.id);
      if (completed.has(identity)) return;
      if (visiting.has(identity)) throw storeCorrupt("Session retry lineage contains a cycle");
      visiting.add(identity);
      assertNoGroundTruth(session);
      assertNoPersistedSecrets(session);
      const events = this.artifacts.listEvents(session.userId, session.id);
      for (const event of events) {
        assertNoGroundTruth(event);
        assertNoPersistedSecrets(event);
      }
      const lineage = eventLineage(session, events);
      const parent = lineage.parentSessionId
        ? byIdentity.get(sessionIdentity(session.userId, lineage.parentSessionId))
        : undefined;
      if (parent) ensure(parent);
      this.transaction(() => {
        let attempt = this.attemptBySession(session.id);
        if (attempt) {
          this.assertAttemptTenant(attempt, session.userId);
          this.repairSessionMirror(attempt);
        } else {
          const parentAttempt = lineage.parentSessionId
            ? this.attemptBySession(lineage.parentSessionId)
            : undefined;
          attempt = this.insertSession(
            session,
            parentAttempt && parentAttempt.tenant_id === session.userId ? parentAttempt : undefined,
            lineage.relation,
            events,
            true,
          );
        }
        for (const event of events) this.indexEvent(attempt, event);
        this.reconcileResult(this.requiredAttemptBySession(session.id));
      });
      this.reconcileTerminalEventFromResult(session.userId, session.id);
      visiting.delete(identity);
      completed.add(identity);
    };
    for (const session of sessions) ensure(session);
    for (const attempt of this.attemptRows("SELECT * FROM attempts ORDER BY id")) {
      this.transaction(() => this.repairSessionMirror(this.requiredAttemptById(attempt.id)));
    }
    this.verifyIndexedArtifacts();
  }

  private insertSession(
    record: SessionRecord,
    explicitParent?: AttemptRow,
    explicitRelation?: TaskAttemptRelation,
    importedEvents: readonly RuntimeEvent[] = [],
    artifactAlreadyExists = false,
  ): AttemptRow {
    const parent = explicitParent ?? (record.parentSessionId
      ? this.attemptBySession(record.parentSessionId)
      : undefined);
    if (parent) this.assertAttemptTenant(parent, record.userId);
    if (record.parentSessionId && !parent) {
      throw new RuntimePolicyError("SESSION_NOT_FOUND", "Parent session not found", 404);
    }
    const attemptId = canonicalId("att", record.userId, record.id);
    const taskId = parent?.task_id ?? canonicalId("tsk", record.userId, record.id);
    const task = parent ? this.requiredTaskById(taskId) : undefined;
    const ordinal = parent ? this.nextAttemptOrdinal(taskId) : 1;
    const relation = explicitRelation ?? (parent ? "linked" : "initial");
    const state = importedEvents.length
      ? taskStateForImportedSession(record, importedEvents)
      : taskStateFromSession(record.status);
    const locator = sessionLocator(record.id);
    if (!artifactAlreadyExists) this.artifacts.putSession(record);
    const digest = this.artifactDigest(locator, "session record");
    if (!task) {
      this.database.prepare(`
        INSERT INTO tasks (
          id, schema_version, tenant_id, root_session_id, job_id, kind, state,
          active_attempt_id, attempt_count, revision, progress_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, 1, NULL, ?, ?)
      `).run(
        taskId,
        TASK_ENVELOPE_VERSION,
        record.userId,
        record.id,
        record.jobId,
        record.task,
        state,
        record.createdAt,
        record.updatedAt,
      );
    }
    this.database.prepare(`
      INSERT INTO attempts (
        id, schema_version, task_id, tenant_id, session_id, ordinal, relation,
        parent_attempt_id, job_id, kind, context_mode, input_names_json,
        parent_session_id, parent_result_sha256, runtime_status, state, auth_mode,
        sandbox_id, block_reason, session_created_at, session_updated_at,
        index_updated_at, session_locator, session_sha256, last_event_seq,
        result_id, revision, session_revision, progress_json
      ) VALUES (
        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0,
        NULL, 1, 1, NULL
      )
    `).run(
      attemptId,
      TASK_ATTEMPT_ENVELOPE_VERSION,
      taskId,
      record.userId,
      record.id,
      ordinal,
      relation,
      parent?.id ?? null,
      record.jobId,
      record.task,
      record.contextMode ?? null,
      record.inputNames ? JSON.stringify(record.inputNames) : null,
      record.parentSessionId ?? null,
      record.parentResultSha256 ?? null,
      record.status,
      state,
      record.authMode,
      record.sandboxId ?? null,
      record.blockReason ?? null,
      record.createdAt,
      record.updatedAt,
      record.updatedAt,
      locator,
      digest,
    );
    this.database.prepare(`
      UPDATE tasks
      SET active_attempt_id = ?, attempt_count = (
        SELECT COUNT(*) FROM attempts WHERE task_id = ?
      ), state = ?, updated_at = ?, revision = revision + 1
      WHERE id = ?
    `).run(attemptId, taskId, state, record.updatedAt, taskId);
    return this.requiredAttemptById(attemptId);
  }

  private updateSession(
    current: AttemptRow,
    next: SessionRecord,
    compare: { column: "revision" | "session_revision"; expected: number },
  ): void {
    if (
      current.tenant_id !== next.userId
      || current.session_id !== next.id
      || current.job_id !== next.jobId
      || current.kind !== next.task
      || current.session_created_at !== next.createdAt
      || current.parent_session_id !== (next.parentSessionId ?? null)
      || current.parent_result_sha256 !== (next.parentResultSha256 ?? null)
    ) {
      throw new RuntimePolicyError(
        "INVALID_TASK_TRANSITION",
        "Task attempt identity is immutable after creation",
        409,
      );
    }
    try {
      assertRuntimeStatusTransition(current.runtime_status, next.status);
    } catch (error) {
      throw new RuntimePolicyError(
        "INVALID_TASK_TRANSITION",
        error instanceof Error ? error.message : "Invalid task transition",
        409,
      );
    }
    const state = taskStateFromSession(next.status, current.state);
    const comparison = compare.column === "revision" ? "revision" : "session_revision";
    const changed = this.database.prepare(`
      UPDATE attempts
      SET job_id = ?, kind = ?, context_mode = ?, input_names_json = ?,
          parent_session_id = ?, parent_result_sha256 = ?, runtime_status = ?, state = ?,
          auth_mode = ?, sandbox_id = ?, block_reason = ?, session_created_at = ?,
          session_updated_at = ?, index_updated_at = ?, revision = revision + 1,
          session_revision = session_revision + 1
      WHERE id = ? AND ${comparison} = ?
    `).run(
      next.jobId,
      next.task,
      next.contextMode ?? null,
      next.inputNames ? JSON.stringify(next.inputNames) : null,
      next.parentSessionId ?? null,
      next.parentResultSha256 ?? null,
      next.status,
      state,
      next.authMode,
      next.sandboxId ?? null,
      next.blockReason ?? null,
      next.createdAt,
      next.updatedAt,
      next.updatedAt,
      current.id,
      compare.expected,
    );
    if (Number(changed.changes) !== 1) {
      throw storeConflict("Task attempt changed before this transition");
    }
    this.artifacts.putSession(next);
    const digest = this.artifactDigest(current.session_locator, "session record");
    this.database.prepare(`
      UPDATE attempts SET session_sha256 = ? WHERE id = ?
    `).run(digest, current.id);
    this.updateActiveTask(current.task_id, current.id, state, undefined, next.updatedAt);
  }

  private reconcileEvents(attempt: AttemptRow): void {
    const events = this.artifacts.listEvents(attempt.tenant_id, attempt.session_id);
    for (const event of events) {
      assertNoGroundTruth(event);
      assertNoPersistedSecrets(event);
      this.indexEvent(this.requiredAttemptById(attempt.id), event);
    }
  }

  private indexEvent(initialAttempt: AttemptRow, event: RuntimeEvent): void {
    assertNoGroundTruth(event);
    assertNoPersistedSecrets(event);
    if (event.sessionId !== initialAttempt.session_id) {
      throw storeCorrupt("Runtime event session identity changed");
    }
    let attempt = initialAttempt;
    if (event.type === "session.created") {
      attempt = this.adoptEventLineage(attempt, event);
    }
    const locator = eventLocator(attempt.session_id, event.seq);
    const digest = this.artifactDigest(locator, "runtime event");
    const id = canonicalId("evt", attempt.tenant_id, attempt.session_id, String(event.seq));
    const existing = this.eventRowByAttemptSequence(attempt.id, event.seq);
    if (existing) {
      if (
        existing.id !== id
        || existing.type !== event.type
        || existing.occurred_at !== event.at
        || existing.artifact_locator !== locator
        || existing.artifact_sha256 !== digest
      ) throw storeCorrupt(`Runtime event cursor ${event.seq} changed`);
      return;
    }
    this.database.prepare(`
      INSERT INTO event_references (
        id, schema_version, task_id, attempt_id, tenant_id, seq, type,
        occurred_at, artifact_locator, artifact_sha256
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      id,
      TASK_EVENT_REFERENCE_VERSION,
      attempt.task_id,
      attempt.id,
      attempt.tenant_id,
      event.seq,
      event.type,
      event.at,
      locator,
      digest,
    );
    // Another store may have indexed a higher cursor while this event artifact
    // was still being published. Always project from the transaction's current
    // row rather than the snapshot captured before file I/O.
    attempt = this.requiredAttemptById(attempt.id);
    if (event.seq <= attempt.last_event_seq) {
      this.recomputeOrderedEventProjection(attempt, event.seq);
      return;
    }
    const state = taskStateFromEvent(event, attempt.state);
    const progress = taskProgressFromEvent(event);
    const changed = this.database.prepare(`
      UPDATE attempts
      SET last_event_seq = ?, state = ?,
          progress_json = COALESCE(?, progress_json),
          index_updated_at = MAX(index_updated_at, ?),
          revision = revision + 1
      WHERE id = ? AND ? > last_event_seq
    `).run(
      event.seq,
      state,
      progress ? JSON.stringify(progress) : null,
      event.at,
      attempt.id,
      event.seq,
    );
    if (Number(changed.changes) === 1) {
      this.updateActiveTask(attempt.task_id, attempt.id, state, progress, event.at);
    }
  }

  private recomputeOrderedEventProjection(attempt: AttemptRow, insertedSequence: number): void {
    const references = this.eventRows(`
      SELECT * FROM event_references
      WHERE attempt_id = ?
      ORDER BY seq
    `, attempt.id);
    const artifacts = new Map(
      this.artifacts.listEvents(attempt.tenant_id, attempt.session_id)
        .map((event) => [event.seq, event]),
    );
    const events: RuntimeEvent[] = [];
    let progress: TaskProgress | undefined;
    let latestAt = attempt.index_updated_at;
    for (const reference of references) {
      const indexed = artifacts.get(reference.seq);
      if (!indexed) throw storeCorrupt(`Indexed event ${reference.seq} is missing`);
      this.verifyArtifact(reference.artifact_locator, reference.artifact_sha256, "runtime event");
      events.push(indexed);
      progress = taskProgressFromEvent(indexed) ?? progress;
      latestAt = indexed.at;
    }
    const orderedState = taskStateForImportedSession(sessionFromAttempt(attempt), events);
    const state = advanceTaskState(attempt.state, orderedState);
    const changed = this.database.prepare(`
      UPDATE attempts
      SET state = ?, progress_json = COALESCE(?, progress_json),
          index_updated_at = MAX(index_updated_at, ?), revision = revision + 1
      WHERE id = ? AND ? <= last_event_seq
    `).run(
      state,
      progress ? JSON.stringify(progress) : null,
      latestAt,
      attempt.id,
      insertedSequence,
    );
    if (Number(changed.changes) === 1) {
      this.updateActiveTask(attempt.task_id, attempt.id, state, progress, latestAt);
    }
  }

  private adoptEventLineage(attempt: AttemptRow, event: RuntimeEvent): AttemptRow {
    const lineage = eventLineage(sessionFromAttempt(attempt), [event]);
    if (!lineage.parentSessionId) return attempt;
    const parent = this.attemptBySession(lineage.parentSessionId);
    if (!parent) throw storeCorrupt("Persisted attempt lineage points to a missing parent");
    this.assertAttemptTenant(parent, attempt.tenant_id);
    if (parent.id === attempt.id) throw storeCorrupt("Task attempt cannot parent itself");
    if (attempt.task_id === parent.task_id) {
      this.database.prepare(`
        UPDATE attempts SET parent_attempt_id = ?, relation = ? WHERE id = ?
      `).run(parent.id, lineage.relation, attempt.id);
      return this.requiredAttemptById(attempt.id);
    }
    const orphanTaskId = attempt.task_id;
    const ordinal = this.nextAttemptOrdinal(parent.task_id);
    this.database.prepare(`
      UPDATE attempts
      SET task_id = ?, ordinal = ?, parent_attempt_id = ?, relation = ?
      WHERE id = ?
    `).run(parent.task_id, ordinal, parent.id, lineage.relation, attempt.id);
    this.database.prepare(`UPDATE event_references SET task_id = ? WHERE attempt_id = ?`)
      .run(parent.task_id, attempt.id);
    this.database.prepare(`UPDATE result_references SET task_id = ? WHERE attempt_id = ?`)
      .run(parent.task_id, attempt.id);
    this.database.prepare(`
      UPDATE tasks
      SET active_attempt_id = ?, attempt_count = (
        SELECT COUNT(*) FROM attempts WHERE task_id = ?
      ), state = ?, updated_at = ?, revision = revision + 1
      WHERE id = ?
    `).run(attempt.id, parent.task_id, attempt.state, event.at, parent.task_id);
    this.database.prepare(`DELETE FROM tasks WHERE id = ? AND NOT EXISTS (
      SELECT 1 FROM attempts WHERE task_id = ?
    )`).run(orphanTaskId, orphanTaskId);
    return this.requiredAttemptById(attempt.id);
  }

  private reconcileResult(attempt: AttemptRow): void {
    const locator = resultLocator(attempt.session_id);
    const absolute = this.absoluteArtifactPath(locator);
    const existing = this.resultRowByAttempt(attempt.id);
    if (!existsSync(absolute)) {
      if (existing) throw storeCorrupt("Indexed runtime result is missing");
      return;
    }
    const result = this.artifacts.getResult(attempt.tenant_id, attempt.session_id);
    assertNoGroundTruth(result);
    assertNoPersistedSecrets(result);
    this.indexResult(attempt, result);
    this.reconcileTerminalSessionFromResult(
      this.requiredAttemptById(attempt.id),
      result,
    );
  }

  private reconcileTerminalEventFromResult(userId: string, sessionId: string): void {
    const attempt = this.requiredAttemptBySession(sessionId);
    if (!this.resultRowByAttempt(attempt.id)) return;
    const result = this.artifacts.getResult(userId, sessionId);
    const type = ({
      review_ready: "job.review_ready",
      partial: "job.partial",
      cancelled: "job.cancelled",
      interrupted: "job.interrupted",
      failed: "job.failed",
    } as const)[result.terminalStatus];
    const recorded = this.artifacts.listEvents(userId, sessionId)
      .some((event) => event.type === type);
    if (recorded) return;
    this.appendEvent(userId, sessionId, type, {
      phase: type,
      source: "immutable_result_recovery",
      recovered: true,
      message: ({
        review_ready: "Recovered sealed extraction artifacts for review.",
        partial: "Recovered sealed partial extraction artifacts for review.",
        cancelled: "Recovered a sealed cancelled extraction result.",
        interrupted: "Recovered a sealed interrupted extraction result.",
        failed: "Recovered a sealed failed extraction result.",
      } as const)[result.terminalStatus],
    });
  }

  private reconcileTerminalSessionFromResult(
    attempt: AttemptRow,
    result: RuntimeJobResult,
  ): void {
    // A verified immutable result is stronger evidence than the mutable
    // session mirror. This repairs a crash after result.json was published but
    // before the session/SQLite terminal transition committed (including a
    // concurrent runtime shutdown that temporarily marked it interrupted).
    const state = taskStateFromSession(result.terminalStatus, attempt.state);
    const updatedAt = result.createdAt > attempt.session_updated_at
      ? result.createdAt : attempt.session_updated_at;
    if (attempt.runtime_status !== result.terminalStatus
      || attempt.state !== state
      || attempt.block_reason) {
      this.database.prepare(`
        UPDATE attempts
        SET runtime_status = ?, state = ?, block_reason = NULL,
            session_updated_at = ?, index_updated_at = ?, revision = revision + 1,
            session_revision = session_revision + 1
        WHERE id = ?
      `).run(result.terminalStatus, state, updatedAt, updatedAt, attempt.id);
    }
    const repaired = this.requiredAttemptById(attempt.id);
    const session = sessionFromAttempt(repaired);
    this.artifacts.putSession(session);
    const digest = this.artifactDigest(repaired.session_locator, "session record");
    this.database.prepare(`UPDATE attempts SET session_sha256 = ? WHERE id = ?`)
      .run(digest, repaired.id);
    // This is the sole non-monotone task projection: a verified immutable
    // result outranks a mutable failure/cancellation mirror written during the
    // crash window after result.json publication. Limit the override to the
    // same active attempt so a newer retry can never be rewound.
    this.database.prepare(`
      UPDATE tasks
      SET state = ?, updated_at = MAX(updated_at, ?), revision = revision + 1
      WHERE id = ? AND active_attempt_id = ?
    `).run(state, updatedAt, repaired.task_id, repaired.id);
    const workState = recoveredWorkItemState(result.terminalStatus);
    this.database.prepare(`
      UPDATE execution_work_items
      SET state = ?, lease_owner = NULL, lease_expires_at = NULL,
          finished_at = ?, updated_at = ?, last_error_code = NULL,
          revision = revision + 1
      WHERE attempt_id = ? AND state <> ?
    `).run(workState, updatedAt, updatedAt, repaired.id, workState);
  }

  private indexResult(attempt: AttemptRow, result: RuntimeJobResult): void {
    this.assertResultMatchesAttempt(attempt, result);
    const locator = resultLocator(attempt.session_id);
    const digest = this.artifactDigest(locator, "runtime result");
    const id = canonicalId("res", attempt.tenant_id, attempt.session_id);
    const existing = this.resultRowByAttempt(attempt.id);
    if (existing) {
      if (
        existing.id !== id
        || existing.artifact_locator !== locator
        || existing.artifact_sha256 !== digest
        || existing.runtime_schema_version !== result.schemaVersion
      ) {
        throw new RuntimePolicyError(
          "RESULT_ALREADY_SEALED",
          "Runtime result is immutable after publication",
          409,
        );
      }
      return;
    }
    this.database.prepare(`
      INSERT INTO result_references (
        id, schema_version, task_id, attempt_id, tenant_id, runtime_schema_version,
        artifact_locator, artifact_sha256, sealed_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      id,
      TASK_RESULT_REFERENCE_VERSION,
      attempt.task_id,
      attempt.id,
      attempt.tenant_id,
      result.schemaVersion,
      locator,
      digest,
      result.createdAt,
    );
    this.database.prepare(`
      UPDATE attempts
      SET result_id = ?, revision = revision + 1, index_updated_at = ?
      WHERE id = ?
    `).run(id, result.createdAt, attempt.id);
    const current = this.requiredAttemptById(attempt.id);
    this.updateActiveTask(current.task_id, current.id, current.state, undefined, result.createdAt);
  }

  private assertResultMatchesAttempt(attempt: AttemptRow, result: RuntimeJobResult): void {
    if (result.sessionId !== attempt.session_id) {
      throw storeCorrupt("Runtime result session identity changed");
    }
    if (result.jobId !== attempt.job_id) {
      throw storeCorrupt("Runtime result job identity changed");
    }
    if (attempt.context_mode && result.contextMode !== attempt.context_mode) {
      throw storeCorrupt("Runtime result context mode changed");
    }
    const expectedContract = result.contextMode === "figure"
      ? "chemrxn.figure-job.v1" : "chemrxn.pdf-replay.v1";
    if (result.domainContract !== expectedContract) {
      throw storeCorrupt("Runtime result domain contract does not match its context mode");
    }
  }

  private repairSessionMirror(attempt: AttemptRow): void {
    const absolute = this.absoluteArtifactPath(attempt.session_locator);
    if (existsSync(absolute) && sha256(readFileSync(absolute)) === attempt.session_sha256) return;
    this.artifacts.putSession(sessionFromAttempt(attempt));
    const digest = this.artifactDigest(attempt.session_locator, "session record");
    this.database.prepare(`UPDATE attempts SET session_sha256 = ? WHERE id = ?`)
      .run(digest, attempt.id);
  }

  private verifyIndexedArtifacts(): void {
    for (const row of this.eventRows("SELECT * FROM event_references ORDER BY attempt_id, seq")) {
      this.verifyArtifact(row.artifact_locator, row.artifact_sha256, "runtime event");
    }
    for (const row of this.resultRows("SELECT * FROM result_references ORDER BY attempt_id")) {
      this.verifyArtifact(row.artifact_locator, row.artifact_sha256, "runtime result");
    }
  }

  private updateActiveTask(
    taskId: string,
    attemptId: string,
    state: TaskState,
    progress: TaskProgress | undefined,
    updatedAt: string,
  ): void {
    const task = this.requiredTaskById(taskId);
    const stateWithoutRegression = task.active_attempt_id === attemptId
      ? advanceTaskState(task.state, state)
      : state;
    this.database.prepare(`
      UPDATE tasks
      SET state = ?, progress_json = COALESCE(?, progress_json),
          updated_at = MAX(updated_at, ?),
          revision = revision + 1
      WHERE id = ? AND active_attempt_id = ?
    `).run(
      stateWithoutRegression,
      progress ? JSON.stringify(progress) : null,
      updatedAt,
      taskId,
      attemptId,
    );
  }

  private nextAttemptOrdinal(taskId: string): number {
    const row = this.database.prepare(`
      SELECT COALESCE(MAX(ordinal), 0) AS latest FROM attempts WHERE task_id = ?
    `).get(taskId);
    return numberColumn(row, "latest") + 1;
  }

  private attemptBySession(sessionId: string): AttemptRow | undefined {
    return castRow<AttemptRow>(this.database.prepare(`
      SELECT * FROM attempts WHERE session_id = ?
    `).get(sessionId));
  }

  private requiredAttemptBySession(sessionId: string): AttemptRow {
    const row = this.attemptBySession(sessionId);
    if (!row) throw new RuntimePolicyError("SESSION_NOT_FOUND", "Session not found", 404);
    return row;
  }

  private attemptById(attemptId: string): AttemptRow | undefined {
    return castRow<AttemptRow>(this.database.prepare(`
      SELECT * FROM attempts WHERE id = ?
    `).get(attemptId));
  }

  private requiredAttemptById(attemptId: string): AttemptRow {
    const row = this.attemptById(attemptId);
    if (!row) throw new RuntimePolicyError("ATTEMPT_NOT_FOUND", "Task attempt not found", 404);
    return row;
  }

  private taskById(taskId: string): TaskRow | undefined {
    return castRow<TaskRow>(this.database.prepare(`SELECT * FROM tasks WHERE id = ?`).get(taskId));
  }

  private requiredTaskById(taskId: string): TaskRow {
    const row = this.taskById(taskId);
    if (!row) throw new RuntimePolicyError("TASK_NOT_FOUND", "Task not found", 404);
    return row;
  }

  private eventRowByAttemptSequence(attemptId: string, seq: number): EventReferenceRow | undefined {
    return castRow<EventReferenceRow>(this.database.prepare(`
      SELECT * FROM event_references WHERE attempt_id = ? AND seq = ?
    `).get(attemptId, seq));
  }

  private resultRowByAttempt(attemptId: string): ResultReferenceRow | undefined {
    return castRow<ResultReferenceRow>(this.database.prepare(`
      SELECT * FROM result_references WHERE attempt_id = ?
    `).get(attemptId));
  }

  private workItemBySession(sessionId: string): ExecutionWorkItemRow | undefined {
    return castRow<ExecutionWorkItemRow>(this.database.prepare(`
      SELECT * FROM execution_work_items WHERE session_id = ?
    `).get(sessionId));
  }

  private requiredWorkItemBySession(sessionId: string): ExecutionWorkItemRow {
    const row = this.workItemBySession(sessionId);
    if (!row) throw new RuntimePolicyError("WORK_ITEM_NOT_FOUND", "Queued work not found", 404);
    return row;
  }

  private taskRows(sql: string, ...parameters: Array<string | number>): TaskRow[] {
    return this.database.prepare(sql).all(...parameters).map((row) => castRequiredRow<TaskRow>(row));
  }

  private attemptRows(sql: string, ...parameters: Array<string | number>): AttemptRow[] {
    return this.database.prepare(sql).all(...parameters).map((row) => castRequiredRow<AttemptRow>(row));
  }

  private eventRows(sql: string, ...parameters: Array<string | number>): EventReferenceRow[] {
    return this.database.prepare(sql).all(...parameters)
      .map((row) => castRequiredRow<EventReferenceRow>(row));
  }

  private resultRows(sql: string, ...parameters: Array<string | number>): ResultReferenceRow[] {
    return this.database.prepare(sql).all(...parameters)
      .map((row) => castRequiredRow<ResultReferenceRow>(row));
  }

  private workItemRows(sql: string, ...parameters: Array<string | number>): ExecutionWorkItemRow[] {
    return this.database.prepare(sql).all(...parameters)
      .map((row) => castRequiredRow<ExecutionWorkItemRow>(row));
  }

  private assertAttemptTenant(row: AttemptRow, userId: string): void {
    if (row.tenant_id !== userId) {
      throw new RuntimePolicyError("CROSS_TENANT_ACCESS", "Session belongs to another tenant", 403);
    }
  }

  private assertTaskTenant(row: TaskRow, userId: string): void {
    if (row.tenant_id !== userId) {
      throw new RuntimePolicyError("CROSS_TENANT_ACCESS", "Task belongs to another tenant", 403);
    }
  }

  private assertWorkItemTenant(row: ExecutionWorkItemRow, userId: string): void {
    if (row.tenant_id !== userId) {
      throw new RuntimePolicyError("CROSS_TENANT_ACCESS", "Queued work belongs to another tenant", 403);
    }
  }

  private transaction<T>(operation: () => T): T {
    try {
      this.database.exec("BEGIN IMMEDIATE");
      const result = operation();
      this.database.exec("COMMIT");
      this.hardenDatabaseFiles();
      return result;
    } catch (error) {
      if (this.database.isOpen && this.database.isTransaction) {
        try {
          this.database.exec("ROLLBACK");
        } catch {
          // Preserve the original policy or SQLite failure.
        }
      }
      if (isDatabaseBusy(error)) {
        throw new RuntimePolicyError("STORE_BUSY", "SQLite task index is busy", 503);
      }
      throw error;
    }
  }

  private artifactDigest(locator: string, label: string): string {
    const absolute = this.absoluteArtifactPath(locator);
    if (!existsSync(absolute)) throw storeCorrupt(`Missing durable ${label}`);
    return sha256(readFileSync(absolute));
  }

  private verifyArtifact(locator: string, expectedSha256: string, label: string): void {
    if (this.artifactDigest(locator, label) !== expectedSha256) {
      throw storeCorrupt(`Durable ${label} changed after indexing`);
    }
  }

  private absoluteArtifactPath(locator: string): string {
    if (!/^sessions\/[A-Za-z0-9_-]{1,128}\/(?:session\.json|result\.json|events\/\d{12}\.json)$/.test(locator)) {
      throw storeCorrupt("Task index contains an unsafe artifact locator");
    }
    const absolute = path.resolve(this.root, ...locator.split("/"));
    const prefix = `${this.root}${path.sep}`;
    if (!absolute.startsWith(prefix)) throw storeCorrupt("Task artifact escapes the data root");
    return absolute;
  }

  private hardenDatabaseFiles(): void {
    for (const suffix of ["", "-wal", "-shm"] as const) {
      const target = `${this.databasePath}${suffix}`;
      if (existsSync(target)) chmodSync(target, 0o600);
    }
  }
}

function recoveredWorkItemState(
  status: SessionRecord["status"],
): Extract<ExecutionWorkItemState, "completed" | "failed" | "cancelled" | "interrupted"> {
  if (status === "ready" || status === "review_ready" || status === "partial") return "completed";
  if (status === "cancelled" || status === "aborted") return "cancelled";
  if (status === "failed" || status === "blocked") return "failed";
  return "interrupted";
}

function taskEnvelope(row: TaskRow): TaskEnvelope {
  if (!row.active_attempt_id) throw storeCorrupt("Task has no active attempt");
  return {
    schemaVersion: TASK_ENVELOPE_VERSION,
    id: row.id,
    userId: row.tenant_id,
    rootSessionId: row.root_session_id,
    jobId: row.job_id,
    kind: row.kind,
    state: row.state,
    activeAttemptId: row.active_attempt_id,
    attemptCount: row.attempt_count,
    revision: row.revision,
    ...(parseProgress(row.progress_json) ? { progress: parseProgress(row.progress_json)! } : {}),
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

function taskAttemptEnvelope(row: AttemptRow): TaskAttemptEnvelope {
  const progress = parseProgress(row.progress_json);
  return {
    schemaVersion: TASK_ATTEMPT_ENVELOPE_VERSION,
    id: row.id,
    taskId: row.task_id,
    sessionId: row.session_id,
    jobId: row.job_id,
    number: row.ordinal,
    relation: row.relation,
    ...(row.parent_attempt_id ? { parentAttemptId: row.parent_attempt_id } : {}),
    runtimeStatus: row.runtime_status,
    state: row.state,
    revision: row.revision,
    lastEventSequence: row.last_event_seq,
    ...(row.result_id ? { resultId: row.result_id } : {}),
    ...(progress ? { progress } : {}),
    createdAt: row.session_created_at,
    updatedAt: row.index_updated_at,
  };
}

function eventReferenceEnvelope(row: EventReferenceRow): TaskEventReference {
  return {
    schemaVersion: TASK_EVENT_REFERENCE_VERSION,
    id: row.id,
    taskId: row.task_id,
    attemptId: row.attempt_id,
    sequence: row.seq,
    type: row.type,
    occurredAt: row.occurred_at,
    artifact: {
      locator: row.artifact_locator,
      sha256: row.artifact_sha256,
      mediaType: "application/json",
    },
  };
}

function resultReferenceEnvelope(row: ResultReferenceRow): TaskResultReference {
  return {
    schemaVersion: TASK_RESULT_REFERENCE_VERSION,
    id: row.id,
    taskId: row.task_id,
    attemptId: row.attempt_id,
    runtimeSchemaVersion: row.runtime_schema_version,
    artifact: {
      locator: row.artifact_locator,
      sha256: row.artifact_sha256,
      mediaType: "application/json",
      immutable: true,
    },
    sealedAt: row.sealed_at,
  };
}

function executionWorkItemEnvelope(row: ExecutionWorkItemRow): ExecutionWorkItem {
  if (row.schema_version !== EXECUTION_WORK_ITEM_VERSION) {
    throw storeCorrupt("Queued work item schema is unsupported");
  }
  return {
    schemaVersion: EXECUTION_WORK_ITEM_VERSION,
    id: row.id,
    userId: row.tenant_id,
    taskId: row.task_id,
    attemptId: row.attempt_id,
    sessionId: row.session_id,
    phase: row.phase,
    state: row.state,
    priority: row.priority,
    revision: row.revision,
    activationCount: row.activation_count,
    enqueuedAt: row.enqueued_at,
    updatedAt: row.updated_at,
    ...(row.started_at ? { startedAt: row.started_at } : {}),
    ...(row.finished_at ? { finishedAt: row.finished_at } : {}),
    ...(row.last_error_code ? { lastErrorCode: row.last_error_code } : {}),
  };
}

function sessionFromAttempt(row: AttemptRow): SessionRecord {
  const inputNames = row.input_names_json
    ? JSON.parse(row.input_names_json) as string[]
    : undefined;
  return {
    id: row.session_id,
    userId: row.tenant_id,
    jobId: row.job_id,
    task: row.kind,
    ...(row.context_mode ? { contextMode: row.context_mode } : {}),
    ...(inputNames ? { inputNames } : {}),
    ...(row.parent_session_id ? { parentSessionId: row.parent_session_id } : {}),
    ...(row.parent_result_sha256 ? { parentResultSha256: row.parent_result_sha256 } : {}),
    status: row.runtime_status,
    authMode: row.auth_mode,
    ...(row.sandbox_id ? { sandboxId: row.sandbox_id } : {}),
    createdAt: row.session_created_at,
    updatedAt: row.session_updated_at,
    ...(row.block_reason ? { blockReason: row.block_reason } : {}),
  };
}

function attachSessionIndex(record: SessionRecord, row: AttemptRow): void {
  Object.defineProperty(record, SESSION_INDEX, {
    value: { attemptId: row.id, sessionRevision: row.session_revision },
    enumerable: false,
    configurable: true,
    writable: true,
  });
}

function eventLineage(
  session: SessionRecord,
  events: readonly RuntimeEvent[],
): { parentSessionId?: string; relation: TaskAttemptRelation } {
  const created = events.find((event) => event.type === "session.created");
  const data = objectValue(created?.data);
  const retry = objectValue(data.retry);
  const continuation = objectValue(data.continuation);
  if (typeof data.retryOfSessionId === "string") {
    return { parentSessionId: data.retryOfSessionId, relation: "retry" };
  }
  if (typeof retry.parentSessionId === "string") {
    return { parentSessionId: retry.parentSessionId, relation: "retry" };
  }
  if (typeof continuation.parentSessionId === "string") {
    return { parentSessionId: continuation.parentSessionId, relation: "continuation" };
  }
  return session.parentSessionId
    ? { parentSessionId: session.parentSessionId, relation: "linked" }
    : { relation: "initial" };
}

function parseProgress(value: string | null): TaskProgress | undefined {
  if (!value) return undefined;
  try {
    return JSON.parse(value) as TaskProgress;
  } catch {
    throw storeCorrupt("Task progress index is corrupt");
  }
}

function sessionLocator(sessionId: string): string {
  return `sessions/${safeSessionId(sessionId)}/session.json`;
}

function eventLocator(sessionId: string, seq: number): string {
  return `sessions/${safeSessionId(sessionId)}/events/${String(seq).padStart(12, "0")}.json`;
}

function resultLocator(sessionId: string): string {
  return `sessions/${safeSessionId(sessionId)}/result.json`;
}

function safeSessionId(value: string): string {
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(value)) {
    throw new RuntimePolicyError("SESSION_NOT_FOUND", "Session not found", 404);
  }
  return value;
}

function canonicalId(prefix: "tsk" | "att" | "evt" | "res" | "wrk", ...parts: string[]): string {
  const hash = createHash("sha256");
  for (const part of parts) hash.update(`${Buffer.byteLength(part)}:${part};`);
  return `${prefix}_${hash.digest("hex").slice(0, 32)}`;
}

function sessionIdentity(userId: string, sessionId: string): string {
  return `${userId.length}:${userId}:${sessionId}`;
}

function sha256(bytes: Buffer): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function castRow<T>(row: Record<string, unknown> | undefined): T | undefined {
  return row as T | undefined;
}

function castRequiredRow<T>(row: Record<string, unknown>): T {
  return row as T;
}

function stringColumn(row: Record<string, unknown> | undefined, key: string): string {
  const value = row?.[key];
  if (typeof value !== "string") throw storeCorrupt(`Task index column ${key} is invalid`);
  return value;
}

function numberColumn(row: Record<string, unknown> | undefined, key: string): number {
  const value = row?.[key];
  if (typeof value !== "number" || !Number.isSafeInteger(value)) {
    throw storeCorrupt(`Task index column ${key} is invalid`);
  }
  return value;
}

function objectValue(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function storeConflict(message: string): RuntimePolicyError {
  return new RuntimePolicyError("STORE_CONFLICT", message, 409);
}

function storeCorrupt(message: string): RuntimePolicyError {
  return new RuntimePolicyError("STORE_CORRUPT", message, 500);
}

function isDatabaseBusy(error: unknown): boolean {
  return error instanceof Error && /database (?:is )?(?:locked|busy)/i.test(error.message);
}
