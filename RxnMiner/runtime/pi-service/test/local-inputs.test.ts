import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { LocalInputStore } from "../src/local-inputs.js";
import { RuntimePolicyError } from "../src/errors.js";

test("local input store streams, hashes, isolates, and materializes immutable Figure bytes", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-inputs-"));
  const store = new LocalInputStore(path.join(root, "inputs"));
  const bytes = readFileSync(path.resolve(
    import.meta.dirname,
    "../../../../runtime/pi-service/test/fixtures/figure/demo-figure/image.png",
  ));
  try {
    const artifact = await store.stage("user-a", chunks(bytes), {
      kind: "figure",
      originalName: "../../reaction.png",
      mediaType: "application/octet-stream",
    });
    assert.equal(artifact.originalName, "reaction.png");
    assert.equal(artifact.mediaType, "image/png");
    assert.equal(artifact.byteSize, bytes.length);
    assert.match(artifact.sha256, /^[a-f0-9]{64}$/);
    const preview = await store.read("user-a", artifact.id);
    assert.deepEqual(preview.bytes, bytes);
    assert.equal(preview.artifact.sha256, artifact.sha256);
    await assert.rejects(
      store.get("user-b", artifact.id),
      (error: unknown) =>
        error instanceof RuntimePolicyError && error.code === "INPUT_NOT_FOUND",
    );
    await assert.rejects(
      store.read("user-b", artifact.id),
      (error: unknown) =>
        error instanceof RuntimePolicyError && error.code === "INPUT_NOT_FOUND",
    );

    const workspace = path.join(root, "workspace");
    const [mounted] = await store.materialize(
      "user-a",
      "figure",
      [{ artifactId: artifact.id, kind: "figure" }],
      workspace,
    );
    assert.equal(mounted?.workspacePath, "input/figure.png");
    assert.deepEqual(readFileSync(mounted!.absolutePath), bytes);
    assert.equal(statSync(mounted!.absolutePath).mode & 0o777, 0o400);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("shutdown aborts a stalled upload without publishing partial local input", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-inputs-shutdown-"));
  const store = new LocalInputStore(path.join(root, "inputs"));
  let waiting!: () => void;
  let release!: () => void;
  const stalled = new Promise<void>((resolve) => { waiting = resolve; });
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const controller = new AbortController();
  async function* slowImage(): AsyncIterable<Uint8Array> {
    yield Buffer.from("partial image");
    waiting();
    await gate;
    yield Buffer.from("late bytes");
  }
  try {
    const staging = store.stage("user-a", slowImage(), {
      kind: "figure",
      originalName: "figure.png",
      mediaType: "image/png",
      signal: controller.signal,
    });
    await stalled;
    controller.abort("runtime_shutdown");
    await assert.rejects(
      staging,
      (error: unknown) => error instanceof RuntimePolicyError
        && error.code === "RUNTIME_SHUTTING_DOWN",
    );
    release();
    const persisted = readdirSync(path.join(root, "inputs"), { recursive: true })
      .map(String)
      .filter((name) => /\.(?:bin|json|uploading)$/.test(name));
    assert.deepEqual(persisted, []);
  } finally {
    release?.();
    rmSync(root, { recursive: true, force: true });
  }
});

async function* chunks(value: Buffer): AsyncIterable<Uint8Array> {
  const midpoint = Math.max(1, Math.floor(value.length / 2));
  yield value.subarray(0, midpoint);
  yield value.subarray(midpoint);
}

test("document uploads and contexts are rejected", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "rxnminer-image-only-"));
  try {
    const store = new LocalInputStore(root);
    for (const kind of ["paper", "si"] as const) {
      await assert.rejects(store.stage("user-a", chunks(Buffer.from("%PDF-1.7")),
        { kind, originalName: "input.pdf", mediaType: "application/pdf" }),
        (error: unknown) => error instanceof RuntimePolicyError && error.code === "INVALID_INPUT");
    }
    await assert.rejects(store.materialize("user-a", "paper", [], path.join(root, "work")),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "INVALID_CONTEXT");
  } finally { rmSync(root, { recursive: true, force: true }); }
});
