import { randomUUID } from "node:crypto";
import { chmodSync, mkdirSync } from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

export interface RuntimeDataLock {
  path: string;
  release(): void;
}

interface OwnerRow {
  pid: number;
  token: string;
}

/**
 * Hold one local runtime owner per durable data root.
 *
 * The owner is acquired with a SQLite BEGIN IMMEDIATE transaction. That gives
 * stale-owner replacement a real compare/serialize boundary: two contenders
 * cannot both observe and replace the same stale owner, and a crash anywhere
 * in acquisition rolls the transaction back without leaving a second
 * unrecoverable lock. SQLite is already the runtime's local truth plane.
 */
export function acquireRuntimeDataLock(root: string): RuntimeDataLock {
  const absoluteRoot = path.resolve(root);
  mkdirSync(absoluteRoot, { recursive: true, mode: 0o700 });
  const lockPath = path.join(absoluteRoot, ".chemrxn-runtime-owner.sqlite");
  const database = new DatabaseSync(lockPath);
  database.exec("PRAGMA busy_timeout = 5000");
  const owner = {
    pid: process.pid,
    token: randomUUID(),
    acquiredAt: new Date().toISOString(),
  };
  try {
    database.exec("BEGIN IMMEDIATE");
    database.exec(`
      CREATE TABLE IF NOT EXISTS runtime_owner (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        pid INTEGER NOT NULL,
        token TEXT NOT NULL,
        acquired_at TEXT NOT NULL
      )
    `);
    const existing = database.prepare(
      "SELECT pid, token FROM runtime_owner WHERE singleton = 1",
    ).get() as unknown as OwnerRow | undefined;
    if (existing && processExists(Number(existing.pid))) {
      database.exec("ROLLBACK");
      database.close();
      throw new Error(
        `Another ChemRxn runtime (pid ${existing.pid}) already owns this local data directory`,
      );
    }
    database.prepare(`
      INSERT INTO runtime_owner(singleton, pid, token, acquired_at)
      VALUES(1, ?, ?, ?)
      ON CONFLICT(singleton) DO UPDATE SET
        pid = excluded.pid,
        token = excluded.token,
        acquired_at = excluded.acquired_at
    `).run(owner.pid, owner.token, owner.acquiredAt);
    database.exec("COMMIT");
    chmodSync(lockPath, 0o600);
  } catch (error) {
    try { database.exec("ROLLBACK"); } catch {}
    try { database.close(); } catch {}
    throw error;
  }

  let released = false;
  return {
    path: lockPath,
    release() {
      if (released) return;
      released = true;
      try {
        database.exec("BEGIN IMMEDIATE");
        database.prepare(
          "DELETE FROM runtime_owner WHERE singleton = 1 AND token = ?",
        ).run(owner.token);
        database.exec("COMMIT");
      } catch {
        try { database.exec("ROLLBACK"); } catch {}
      } finally {
        try { database.close(); } catch {}
      }
    },
  };
}

function processExists(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return Boolean(error && typeof error === "object" && "code" in error
      && (error as { code?: string }).code === "EPERM");
  }
}
