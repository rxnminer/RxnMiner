import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import {
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";
import { acquireRuntimeDataLock } from "../src/runtime-lock.js";

test("one local runtime owns a durable data root and stale ownership is reclaimable", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-runtime-lock-"));
  try {
    const first = acquireRuntimeDataLock(root);
    assert.throws(
      () => acquireRuntimeDataLock(root),
      /already owns this local data directory/,
    );
    first.release();
    const second = acquireRuntimeDataLock(root);
    second.release();

    installStaleOwner(root);
    const reclaimed = acquireRuntimeDataLock(root);
    reclaimed.release();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("concurrent stale reclaimers never hold the runtime data lock together", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-runtime-lock-contenders-"));
  try {
    installStaleOwner(root);
    const barrier = path.join(root, "start");
    const moduleUrl = new URL("../src/runtime-lock.js", import.meta.url).href;
    const children = Array.from({ length: 6 }, (_, index) => {
      const result = path.join(root, `result-${index}.json`);
      const script = `
        import { existsSync, writeFileSync } from "node:fs";
        import { acquireRuntimeDataLock } from ${JSON.stringify(moduleUrl)};
        const [root, barrier, result] = process.argv.slice(1);
        while (!existsSync(barrier)) await new Promise(r => setTimeout(r, 2));
        try {
          const lock = acquireRuntimeDataLock(root);
          const start = Date.now();
          await new Promise(r => setTimeout(r, 250));
          const end = Date.now();
          writeFileSync(result, JSON.stringify({ acquired: true, start, end }));
          lock.release();
        } catch (error) {
          writeFileSync(result, JSON.stringify({ acquired: false, message: String(error) }));
        }
      `;
      const child = spawn(process.execPath, ["--input-type=module", "-e", script, root, barrier, result]);
      return { child, result };
    });
    writeFileSync(barrier, "go");
    await Promise.all(children.map(({ child }) => new Promise<void>((resolve, reject) => {
      child.once("error", reject);
      child.once("exit", (code) => code === 0
        ? resolve() : reject(new Error(`lock contender exited ${code}`)));
    })));
    const intervals = children.map(({ result }) =>
      JSON.parse(readFileSync(result, "utf8")) as {
        acquired: boolean;
        start?: number;
        end?: number;
      }).filter((entry) => entry.acquired);
    assert.ok(intervals.length >= 1);
    for (let left = 0; left < intervals.length; left += 1) {
      for (let right = left + 1; right < intervals.length; right += 1) {
        assert.ok(
          intervals[left]!.end! <= intervals[right]!.start!
            || intervals[right]!.end! <= intervals[left]!.start!,
          "two runtime processes held the same durable root concurrently",
        );
      }
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

function installStaleOwner(root: string): void {
  const database = new DatabaseSync(path.join(root, ".chemrxn-runtime-owner.sqlite"));
  database.exec(`
    CREATE TABLE IF NOT EXISTS runtime_owner (
      singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
      pid INTEGER NOT NULL,
      token TEXT NOT NULL,
      acquired_at TEXT NOT NULL
    );
    DELETE FROM runtime_owner;
  `);
  database.prepare(`
    INSERT INTO runtime_owner(singleton, pid, token, acquired_at)
    VALUES(1, ?, ?, ?)
  `).run(2_147_483_647, "stale-owner", "2026-07-31T00:00:00.000Z");
  database.close();
}
