import assert from "node:assert/strict";
import { readFileSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { LocalInputStore } from "../src/local-inputs.js";
import { LocalPiAgentService } from "../src/local-agent.js";
import {
  buildProtocolFingerprint,
  CANONICAL_CHEM_MCP_RUNNER,
  PINNED_PROTOCOL_FILES,
  ProtocolIntegrityError,
  ProtocolIntegrityVerifier,
  verifyPinnedProtocolFiles,
} from "../src/protocol-fingerprint.js";
import { localAgentCreateRequestSchema } from "../src/schemas.js";
import { InMemoryRuntimeStore } from "../src/store.js";
import { RuntimePolicyError } from "../src/errors.js";

const REPO = fileURLToPath(new URL("../../../..", import.meta.url));
const A = "a".repeat(64);
const B = "b".repeat(64);

test("protocol fingerprint is canonical and changes on every inference-critical axis", () => {
  const base = {
    stage: "dispatch" as const,
    task: "extract_all" as const,
    contextMode: "figure" as const,
    inputs: [{ kind: "figure", sha256: A }],
    perceptionStatus: "resolved_before_model" as const,
    perception: [{ kind: "molparser_raw", sha256: B }],
    skills: [
      { name: "reaction-extract", path: "/ignored/a", sha256: A },
      { name: "pdf-reaction-extract", path: "/ignored/b", sha256: B },
    ],
    protocolFiles: PINNED_PROTOCOL_FILES,
    prompt: "Use the exact loaded skill.",
    toolContract: [
      {
        name: "verify_smiles",
        description: "Verify one molecular structure.",
        inputSchema: { type: "object", required: ["smiles"], properties: { smiles: { type: "string" } } },
      },
      {
        name: "detect",
        description: "Read structures from one figure.",
        inputSchema: { type: "object", required: ["image_path"], properties: { image_path: { type: "string" } } },
      },
    ],
    provider: "openai-codex",
    modelId: "gpt-5.6-sol",
    thinkingLevel: "medium" as const,
  };
  const canonical = buildProtocolFingerprint(base);
  const reordered = buildProtocolFingerprint({
    ...base,
    inputs: [...base.inputs].reverse(),
    perception: [...base.perception].reverse(),
    skills: [...base.skills].reverse(),
    toolContract: [...base.toolContract].reverse(),
  });
  assert.equal(reordered.sha256, canonical.sha256);
  assert.equal(canonical.tools.runner, CANONICAL_CHEM_MCP_RUNNER);
  assert.equal(canonical.model.thinkingLevel, "medium");
  assert.match(canonical.sha256, /^[a-f0-9]{64}$/);
  const pinnedPaths = new Set(PINNED_PROTOCOL_FILES.map((item) => item.path));
  for (const required of [
    "figure_skills/scripts/run_pi_extract.sh",
    "figure_skills/pi-extension/index.ts",
    "runtime/pi-service/src/workspace-ui-tools.ts",
    "runtime/pi-service/src/chem-mcp-client.ts",
    "runtime/pi-service/src/pi-driver.ts",
    "runtime/pi-service/src/local-agent.ts",
    "figure_skills/requirements.txt",
    "runtime/pi-service/package-lock.json",
    "figure_skills/tools/ocsr_client.py",
    "figure_skills/engine/markush.py",
    "figure_skills/third_party/molecular_assembly/src/decompose.py",
    "figure_skills/third_party/molecular_assembly/src/reverse_assembly.py",
    "figure_skills/third_party/molecular_assembly_v2/src/chemistry/reverse_assembly.py",
    "figure_skills/third_party/molecular_assembly_v2/src/chemistry/ring_transfer.py",
    "figure_skills/third_party/molecular_assembly_v2/src/chemistry/rendering.py",
    "figure_skills/third_party/molecular_assembly_v2/src/esmiles/loader.py",
  ]) assert.equal(pinnedPaths.has(required), true, `missing protocol pin: ${required}`);

  for (const changed of [
    { ...base, inputs: [{ kind: "figure", sha256: B }] },
    { ...base, perception: [{ kind: "molparser_raw", sha256: A }] },
    { ...base, prompt: `${base.prompt} changed` },
    { ...base, protocolFiles: base.protocolFiles.map((file, index) =>
      index === 0 ? { ...file, sha256: B } : file) },
    {
      ...base,
      toolContract: base.toolContract.map((tool) => tool.name === "detect"
        ? { ...tool, description: `${tool.description} changed` } : tool),
    },
    {
      ...base,
      toolContract: base.toolContract.map((tool) => tool.name === "detect"
        ? { ...tool, inputSchema: { ...tool.inputSchema, additionalProperties: false } } : tool),
    },
    { ...base, skills: [{ ...base.skills[0]!, sha256: B }, base.skills[1]!] },
    { ...base, provider: "anthropic" },
    { ...base, modelId: "gpt-other" },
    { ...base, thinkingLevel: "high" as const },
  ]) {
    assert.notEqual(buildProtocolFingerprint(changed).sha256, canonical.sha256);
  }
  verifyPinnedProtocolFiles(REPO);
});

test("protocol verifier returns measured digests and detects post-start file drift", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-protocol-gate-"));
  const file = path.join(root, "protocol.txt");
  writeFileSync(file, "alpha");
  const expected = createHash("sha256").update("alpha").digest("hex");
  const verifier = new ProtocolIntegrityVerifier(root, [{
    role: "contract",
    path: "protocol.txt",
    sha256: expected,
  }]);
  try {
    assert.deepEqual(verifier.verify(), [{
      role: "contract",
      path: "protocol.txt",
      sha256: expected,
    }]);
    writeFileSync(file, "omega");
    assert.throws(
      () => verifier.verify(),
      (error: unknown) => error instanceof ProtocolIntegrityError
        && error.reason === "hash_mismatch",
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("local Pi creation fails closed with a typed error before persisting a session", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-protocol-block-"));
  const store = new InMemoryRuntimeStore();
  const service = new LocalPiAgentService(store, {
    repoRoot: root,
    workspaceRoot: path.join(root, "workspaces"),
    agentRoot: path.join(root, "agents"),
    python: "python3",
  });
  try {
    await assert.rejects(
      service.prepare({
        userId: "local-user",
        jobId: "blocked-job",
        contextMode: "figure",
        workspaceId: "blocked-workspace",
        provider: "openai-codex",
        modelId: "gpt-5.6-sol",
        authMode: "pi_auth",
      }),
      (error: unknown) => error instanceof RuntimePolicyError
        && error.code === "PROTOCOL_INTEGRITY_FAILED"
        && error.status === 503,
    );
    assert.deepEqual(store.listAllSessions(), []);
  } finally {
    await service.disposeAll();
    rmSync(root, { recursive: true, force: true });
  }
});

test("thinking level defaults explicitly and survives persisted retry without a model call", async () => {
  const parsed = localAgentCreateRequestSchema.parse({
    userId: "local-user",
    jobId: "job-default",
    contextMode: "figure",
    workspaceId: "workspace-default",
    provider: "openai-codex",
    modelId: "gpt-5.6-sol",
    authMode: "pi_auth",
  });
  assert.equal(parsed.thinkingLevel, "medium");

  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-thinking-parity-"));
  const inputStore = new LocalInputStore(path.join(root, "inputs"));
  const store = new InMemoryRuntimeStore();
  let id = 0;
  const service = new LocalPiAgentService(store, {
    repoRoot: REPO,
    workspaceRoot: path.join(root, "workspaces"),
    agentRoot: path.join(root, "agents"),
    python: "python3",
    inputStore,
  }, { randomId: () => `session-${++id}` });
  try {
    const image = readFileSync(path.join(REPO, "runtime/pi-service/test/fixtures/figure/demo-figure/image.png"));
    const artifact = await inputStore.stage("local-user", chunks(image), {
      kind: "figure",
      originalName: "reaction.png",
      mediaType: "image/png",
    });
    const first = await service.prepare({
      userId: "local-user",
      jobId: "job-high",
      contextMode: "figure",
      workspaceId: "workspace-high",
      provider: "openai-codex",
      modelId: "gpt-5.6-sol",
      thinkingLevel: "high",
      authMode: "api_key",
      apiKey: "not-persisted-test-key",
      inputs: [{ artifactId: artifact.id, kind: "figure" }],
    });
    assert.equal(first.thinkingLevel, "high");
    first.status = "failed";
    store.putSession(first);

    const retried = await service.prepareRetry(
      "local-user",
      first.id,
      "replacement-test-key",
    );
    assert.equal(retried.session.thinkingLevel, "high");
    const created = store.listEvents("local-user", retried.session.id)
      .find((event) => event.type === "session.created");
    const data = created?.data as Record<string, unknown>;
    assert.equal(data.thinkingLevel, "high");
    assert.equal((data.localExtractionRequest as Record<string, unknown>).thinkingLevel, "high");
    assert.equal(JSON.stringify(data).includes("replacement-test-key"), false);
  } finally {
    await service.disposeAll();
    rmSync(root, { recursive: true, force: true });
  }
});

async function* chunks(value: Buffer): AsyncIterable<Uint8Array> {
  yield value;
}
