import { randomUUID } from "node:crypto";
import {
  chmodSync,
  closeSync,
  existsSync,
  fsyncSync,
  linkSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  renameSync,
  unlinkSync,
  watch,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import type { RuntimeEvent, RuntimeJobResult, SessionRecord } from "./contracts.js";
import { RuntimePolicyError } from "./errors.js";
import { runtimeEventSchema, runtimeJobResultSchema, sessionRecordSchema } from "./schemas.js";
import { assertNoGroundTruth, redactSecrets } from "./security.js";

export interface RuntimeStore {
  putSession(record: SessionRecord): void;
  getSession(userId: string, sessionId: string): SessionRecord;
  listSessions(userId: string): SessionRecord[];
  listAllSessions(): SessionRecord[];
  appendEvent(
    userId: string,
    sessionId: string,
    type: RuntimeEvent["type"],
    data: unknown,
    secrets?: readonly string[],
  ): RuntimeEvent;
  listEvents(userId: string, sessionId: string, after?: number): RuntimeEvent[];
  subscribeEvents(sessionId: string, listener: (event: RuntimeEvent) => void): () => void;
  putResult(userId: string, sessionId: string, result: RuntimeJobResult): void;
  getResult(userId: string, sessionId: string): RuntimeJobResult;
}

export class InMemoryRuntimeStore implements RuntimeStore {
  private readonly sessions = new Map<string, SessionRecord>();
  private readonly events = new Map<string, RuntimeEvent[]>();
  private readonly results = new Map<string, RuntimeJobResult>();
  private readonly eventListeners = new Map<string, Set<(event: RuntimeEvent) => void>>();

  putSession(record: SessionRecord): void {
    assertNoGroundTruth(record);
    this.sessions.set(record.id, sessionRecordSchema.parse(structuredClone(record)) as SessionRecord);
  }

  getSession(userId: string, sessionId: string): SessionRecord {
    const record = this.sessions.get(sessionId);
    if (!record) throw new RuntimePolicyError("SESSION_NOT_FOUND", "Session not found", 404);
    if (record.userId !== userId) {
      throw new RuntimePolicyError("CROSS_TENANT_ACCESS", "Session belongs to another tenant", 403);
    }
    return structuredClone(sessionRecordSchema.parse(record) as SessionRecord);
  }

  listSessions(userId: string): SessionRecord[] {
    return [...this.sessions.values()]
      .filter((record) => record.userId === userId)
      .map((record) => structuredClone(record));
  }

  listAllSessions(): SessionRecord[] {
    return [...this.sessions.values()].map((record) => structuredClone(record));
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
    const list = this.events.get(sessionId) ?? [];
    const event = runtimeEventSchema.parse({
      seq: list.length + 1,
      sessionId,
      at: new Date().toISOString(),
      type,
      data: redactSecrets(data, secrets),
    });
    list.push(event);
    this.events.set(sessionId, list);
    this.notifyEvent(sessionId, event);
    return structuredClone(event);
  }

  listEvents(userId: string, sessionId: string, after = 0): RuntimeEvent[] {
    this.getSession(userId, sessionId);
    return structuredClone((this.events.get(sessionId) ?? []).filter((event) => event.seq > after));
  }

  subscribeEvents(sessionId: string, listener: (event: RuntimeEvent) => void): () => void {
    const listeners = this.eventListeners.get(sessionId) ?? new Set();
    listeners.add(listener);
    this.eventListeners.set(sessionId, listeners);
    return () => {
      listeners.delete(listener);
      if (listeners.size === 0) this.eventListeners.delete(sessionId);
    };
  }

  putResult(userId: string, sessionId: string, result: RuntimeJobResult): void {
    this.getSession(userId, sessionId);
    assertNoGroundTruth(result);
    if (result.sessionId !== sessionId) throw new Error("Runtime result session mismatch");
    const parsed = runtimeJobResultSchema.parse(structuredClone(result)) as RuntimeJobResult;
    const existing = this.results.get(sessionId);
    if (existing) {
      if (JSON.stringify(existing) === JSON.stringify(parsed)) return;
      throw new RuntimePolicyError(
        "RESULT_ALREADY_SEALED",
        "Runtime result is immutable after publication",
        409,
      );
    }
    this.results.set(sessionId, parsed);
  }

  getResult(userId: string, sessionId: string): RuntimeJobResult {
    this.getSession(userId, sessionId);
    const result = this.results.get(sessionId);
    if (!result) throw new RuntimePolicyError("RESULT_NOT_READY", "Result is not ready", 409);
    return structuredClone(runtimeJobResultSchema.parse(result) as RuntimeJobResult);
  }

  private notifyEvent(sessionId: string, event: RuntimeEvent): void {
    for (const listener of this.eventListeners.get(sessionId) ?? []) {
      try {
        listener(structuredClone(event));
      } catch {
        // A disconnected UI subscriber cannot make durable event append fail.
      }
    }
  }
}

/**
 * Single-user/local-first durable store.
 *
 * This is deliberately not the production multi-tenant database promised by
 * issue #48. It gives the local product crash/restart persistence and cursor
 * replay without claiming that a filesystem directory is a hosted security
 * boundary.
 */
export class DurableFileRuntimeStore implements RuntimeStore {
  private readonly sessionsDir: string;
  private readonly lastSeq = new Map<string, number>();
  private readonly eventListeners = new Map<string, Set<(event: RuntimeEvent) => void>>();

  constructor(readonly root: string) {
    this.root = path.resolve(root);
    this.sessionsDir = path.join(this.root, "sessions");
    ensurePrivateDirectory(this.root);
    ensurePrivateDirectory(this.sessionsDir);
  }

  putSession(record: SessionRecord): void {
    assertNoGroundTruth(record);
    const directory = this.sessionDirectory(record.id);
    ensurePrivateDirectory(directory);
    atomicPrivateJson(
      path.join(directory, "session.json"),
      sessionRecordSchema.parse(record) as SessionRecord,
    );
  }

  getSession(userId: string, sessionId: string): SessionRecord {
    const record = this.readSession(sessionId);
    if (record.userId !== userId) {
      throw new RuntimePolicyError("CROSS_TENANT_ACCESS", "Session belongs to another tenant", 403);
    }
    return structuredClone(record);
  }

  listSessions(userId: string): SessionRecord[] {
    return readdirSync(this.sessionsDir, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .flatMap((entry) => {
        try {
          const record = this.readSession(entry.name);
          return record.userId === userId ? [structuredClone(record)] : [];
        } catch {
          return [];
        }
      })
      .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  }

  listAllSessions(): SessionRecord[] {
    return readdirSync(this.sessionsDir, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .flatMap((entry) => {
        try {
          return [structuredClone(this.readSession(entry.name))];
        } catch {
          return [];
        }
      })
      .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
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
    const event: RuntimeEvent = {
      seq: this.nextEventSequence(sessionId),
      sessionId,
      at: new Date().toISOString(),
      type,
      data: redactSecrets(data, secrets),
    };
    assertNoGroundTruth(event);
    const written = this.writeEventExclusive(sessionId, event);
    this.lastSeq.set(sessionId, written.seq);
    this.notifyEvent(sessionId, written);
    return structuredClone(written);
  }

  listEvents(userId: string, sessionId: string, after = 0): RuntimeEvent[] {
    this.getSession(userId, sessionId);
    return structuredClone(this.readEvents(sessionId, after));
  }

  subscribeEvents(sessionId: string, listener: (event: RuntimeEvent) => void): () => void {
    safeSessionId(sessionId);
    const directory = this.eventsDirectory(sessionId);
    ensurePrivateDirectory(directory);
    let lastSeen = this.readLastSequence(sessionId);
    const deliver = (event: RuntimeEvent) => {
      if (event.seq <= lastSeen) return;
      lastSeen = event.seq;
      listener(event);
    };
    const listeners = this.eventListeners.get(sessionId) ?? new Set();
    listeners.add(deliver);
    this.eventListeners.set(sessionId, listeners);
    const watcher = watch(directory, (_eventType, filename) => {
      const name = filename ?? undefined;
      const match = name?.match(/^(\d{12})\.json$/);
      if (!match || Number(match[1]) <= lastSeen) return;
      try {
        const event = runtimeEventSchema.parse(
          parseJson(readFileSync(path.join(directory, name!), "utf8"), "runtime event"),
        ) as RuntimeEvent;
        deliver(structuredClone(event));
      } catch {
        // Atomic writers may trigger a directory notification before the link
        // is observable. The same-process notification or a later write event
        // will deliver the committed event; durability never depends on watch.
      }
    });
    return () => {
      watcher.close();
      listeners.delete(deliver);
      if (listeners.size === 0) this.eventListeners.delete(sessionId);
    };
  }

  putResult(userId: string, sessionId: string, result: RuntimeJobResult): void {
    this.getSession(userId, sessionId);
    assertNoGroundTruth(result);
    if (result.sessionId !== sessionId) throw new Error("Runtime result session mismatch");
    const parsed = runtimeJobResultSchema.parse(result) as RuntimeJobResult;
    const target = path.join(this.sessionDirectory(sessionId), "result.json");
    try {
      atomicExclusiveJson(target, parsed);
    } catch (error) {
      if (!isAlreadyExists(error)) throw error;
      const existing = runtimeJobResultSchema.parse(
        parseJson(readFileSync(target, "utf8"), "runtime result"),
      ) as RuntimeJobResult;
      if (JSON.stringify(existing) === JSON.stringify(parsed)) return;
      throw new RuntimePolicyError(
        "RESULT_ALREADY_SEALED",
        "Runtime result is immutable after publication",
        409,
      );
    }
  }

  getResult(userId: string, sessionId: string): RuntimeJobResult {
    this.getSession(userId, sessionId);
    const resultPath = path.join(this.sessionDirectory(sessionId), "result.json");
    if (!existsSync(resultPath)) {
      throw new RuntimePolicyError("RESULT_NOT_READY", "Result is not ready", 409);
    }
    return structuredClone(runtimeJobResultSchema.parse(
      parseJson(readFileSync(resultPath, "utf8"), "runtime result"),
    ) as RuntimeJobResult);
  }

  private readSession(sessionId: string): SessionRecord {
    const sessionPath = path.join(this.sessionDirectory(sessionId), "session.json");
    if (!existsSync(sessionPath)) {
      throw new RuntimePolicyError("SESSION_NOT_FOUND", "Session not found", 404);
    }
    return sessionRecordSchema.parse(
      parseJson(readFileSync(sessionPath, "utf8"), "session record"),
    ) as SessionRecord;
  }

  private readEvents(sessionId: string, after = 0): RuntimeEvent[] {
    const directory = this.eventsDirectory(sessionId);
    if (!existsSync(directory)) return [];
    return readdirSync(directory)
      .filter((name) => {
        const match = name.match(/^(\d{12})\.json$/);
        return match ? Number(match[1]) > after : false;
      })
      .sort()
      .map((name) => runtimeEventSchema.parse(
        parseJson(readFileSync(path.join(directory, name), "utf8"), "runtime event"),
      ));
  }

  private sessionDirectory(sessionId: string): string {
    return path.join(this.sessionsDir, safeSessionId(sessionId));
  }

  private eventsDirectory(sessionId: string): string {
    return path.join(this.sessionDirectory(sessionId), "events");
  }

  private writeEventExclusive(sessionId: string, initial: RuntimeEvent): RuntimeEvent {
    const directory = this.eventsDirectory(sessionId);
    ensurePrivateDirectory(directory);
    let event = initial;
    for (let attempt = 0; attempt < 32; attempt += 1) {
      const target = path.join(directory, `${String(event.seq).padStart(12, "0")}.json`);
      try {
        atomicExclusiveJson(target, event);
        return event;
      } catch (error) {
        if (!isAlreadyExists(error)) throw error;
        const latest = this.readLastSequence(sessionId);
        this.lastSeq.set(sessionId, latest);
        event = { ...event, seq: Math.max(latest, event.seq) + 1 };
      }
    }
    throw new RuntimePolicyError("STORE_BUSY", "Could not reserve the next event cursor", 503);
  }

  private nextEventSequence(sessionId: string): number {
    const cached = this.lastSeq.get(sessionId);
    if (cached !== undefined) return cached + 1;
    const latest = this.readLastSequence(sessionId);
    this.lastSeq.set(sessionId, latest);
    return latest + 1;
  }

  private readLastSequence(sessionId: string): number {
    const directory = this.eventsDirectory(sessionId);
    if (!existsSync(directory)) return 0;
    let latest = 0;
    for (const name of readdirSync(directory)) {
      const match = name.match(/^(\d{12})\.json$/);
      if (match) latest = Math.max(latest, Number(match[1]));
    }
    return latest;
  }

  private notifyEvent(sessionId: string, event: RuntimeEvent): void {
    for (const listener of this.eventListeners.get(sessionId) ?? []) {
      try {
        listener(structuredClone(event));
      } catch {
        // A disconnected UI subscriber cannot make durable event append fail.
      }
    }
  }
}

function safeSessionId(sessionId: string): string {
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(sessionId)) {
    throw new RuntimePolicyError("SESSION_NOT_FOUND", "Session not found", 404);
  }
  return sessionId;
}

function ensurePrivateDirectory(directory: string): void {
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  chmodSync(directory, 0o700);
}

function atomicPrivateJson(target: string, value: unknown): void {
  const temporary = `${target}.${randomUUID()}.tmp`;
  writeAndSync(temporary, value);
  renameSync(temporary, target);
  chmodSync(target, 0o600);
  syncDirectory(path.dirname(target));
}

function atomicExclusiveJson(target: string, value: unknown): void {
  const temporary = `${target}.${randomUUID()}.tmp`;
  writeAndSync(temporary, value);
  try {
    linkSync(temporary, target);
    chmodSync(target, 0o600);
    syncDirectory(path.dirname(target));
  } finally {
    if (existsSync(temporary)) unlinkSync(temporary);
  }
}

function writeAndSync(target: string, value: unknown): void {
  const descriptor = openSync(target, "wx", 0o600);
  try {
    writeFileSync(descriptor, `${JSON.stringify(value)}\n`, { encoding: "utf8" });
    fsyncSync(descriptor);
  } finally {
    closeSync(descriptor);
  }
}

function syncDirectory(directory: string): void {
  const descriptor = openSync(directory, "r");
  try {
    fsyncSync(descriptor);
  } finally {
    closeSync(descriptor);
  }
}

function isAlreadyExists(error: unknown): boolean {
  return error instanceof Error && "code" in error && error.code === "EEXIST";
}

function parseJson(value: string, label: string): unknown {
  try {
    return JSON.parse(value) as unknown;
  } catch {
    throw new Error(`Corrupt durable ${label}`);
  }
}
