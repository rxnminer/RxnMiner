import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { request as httpRequest } from "node:http";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import type { CreateSessionRequest, RuntimeJobResult } from "../src/contracts.js";
import { RuntimePolicyError } from "../src/errors.js";
import { buildMountManifest, redactSecrets } from "../src/security.js";
import {
  createSessionRequestSchema,
  localAgentCreateRequestSchema,
  runtimeEventSchema,
} from "../src/schemas.js";
import { UnconfiguredSandboxManager } from "../src/sandbox.js";
import type { SandboxLease, SandboxLaunchSecrets, SandboxManager } from "../src/sandbox.js";
import { PiRuntimeService } from "../src/service.js";
import { DurableFileRuntimeStore, InMemoryRuntimeStore } from "../src/store.js";
import { SqliteRuntimeStore } from "../src/sqlite-store.js";
import { PredictionVersionStore } from "../src/versioning.js";
import { encodeSseEvent, parseEventCursor, terminalSession } from "../src/sse.js";
import { inspectPiAuth, inspectPiModel } from "../src/pi-driver.js";

function request(overrides: Partial<CreateSessionRequest> = {}): CreateSessionRequest {
  return {
    userId: "user-a",
    jobId: "job-a",
    task: "inspect_selection",
    contextMode: "figure",
    artifacts: [{ id: "figure.png", kind: "figure", sha256: "a".repeat(64), mediaType: "image/png" }],
    auth: { mode: "api_key", provider: "openai", credentialRef: "cred-1" },
    model: { provider: "openai", id: "gpt-5" },
    ...overrides,
  };
}

function sealedResult(sessionId: string, marker = "first"): RuntimeJobResult {
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
    manifest: { marker },
    createdAt: "2000-01-01T00:00:00.000Z",
    gtIncluded: false,
  };
}

test("Pi auth status is secret-free and defaults to the isolated ChemRxn profile", async () => {
  assert.deepEqual(await inspectPiAuth(undefined), {
    configured: false,
    provider: "openai-codex",
    profile: "chemrxn_isolated",
  });
});

test("Pi auth inspection never loads the general Pi custom-model catalog", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-auth-catalog-isolation-"));
  const globalAgentDir = path.join(root, "general-pi-profile");
  const authPath = path.join(root, "chemrxn-auth.json");
  const previousAgentDir = process.env.PI_CODING_AGENT_DIR;
  mkdirSync(globalAgentDir, { recursive: true, mode: 0o700 });
  writeFileSync(path.join(globalAgentDir, "models.json"), JSON.stringify({
    providers: {
      "global-catalog-trap": {
        baseUrl: "http://127.0.0.1:9/v1",
        api: "openai-completions",
        models: [{ id: "trap-model", input: ["text"] }],
      },
    },
  }), { mode: 0o600 });
  writeFileSync(authPath, JSON.stringify({
    "global-catalog-trap": {
      type: "api_key",
      key: "must-not-be-observed",
    },
  }), { mode: 0o600 });
  process.env.PI_CODING_AGENT_DIR = globalAgentDir;
  try {
    assert.deepEqual(await inspectPiAuth(authPath, "global-catalog-trap"), {
      configured: false,
      provider: "global-catalog-trap",
      profile: "chemrxn_isolated",
    });
    assert.deepEqual(readdirSync(globalAgentDir).sort(), ["models.json"]);
  } finally {
    if (previousAgentDir === undefined) {
      delete process.env.PI_CODING_AGENT_DIR;
    } else {
      process.env.PI_CODING_AGENT_DIR = previousAgentDir;
    }
    rmSync(root, { recursive: true, force: true });
  }
});

test("product BYO defaults resolve without network in the locked embedded Pi SDK", async () => {
  const productContractUrl = new URL(
    "../../../../reviewer/chem_reviewer/web/lib/product-model-defaults.js",
    import.meta.url,
  );
  const contract = await import(productContractUrl.href) as {
    PRODUCT_API_PROVIDER_OPTIONS: Array<{
      registryId: string;
      piProvider: string;
      defaultModel: string;
    }>;
  };
  assert.deepEqual(
    contract.PRODUCT_API_PROVIDER_OPTIONS.map((option) => option.registryId),
    ["byo-openai", "byo-anthropic", "byo-gemini"],
  );
  for (const option of contract.PRODUCT_API_PROVIDER_OPTIONS) {
    const resolution = await inspectPiModel(option.piProvider, option.defaultModel);
    assert.equal(
      resolution.available,
      true,
      `${option.registryId} default ${option.piProvider}/${option.defaultModel} `
        + "must exist in the locked Pi SDK",
    );
  }
  assert.equal((await inspectPiModel("google", "gemini-model-that-does-not-exist")).available, false);
  const subscriptionDefault = await inspectPiModel("openai-codex", "gpt-5.6-sol");
  assert.equal(subscriptionDefault.available, true);
  assert.equal(subscriptionDefault.supportsImages, true);
});

test("personal subscription starts an isolated sandbox and waits for the user's Pi login", async () => {
  let launchSecrets: SandboxLaunchSecrets | undefined;
  const service = new PiRuntimeService(new InMemoryRuntimeStore(), {
    async create(_manifest, secrets) {
      launchSecrets = secrets;
      return {
        id: "sandbox-auth",
        userId: "user-a",
        jobId: "job-a",
        endpoint: "https://sandbox.invalid/auth",
        expiresAt: new Date(Date.now() + 60_000).toISOString(),
      };
    },
    async destroy() {},
  });
  const session = await service.create(
    request({
      auth: {
        mode: "personal_codex_subscription",
        provider: "openai-codex",
        credentialRef: "interactive-login-has-no-stored-credential",
      },
    }),
  );
  assert.equal(session.status, "awaiting_user_auth");
  assert.equal(launchSecrets, undefined);
  assert.equal(service.events("user-a", session.id).at(-1)?.type, "auth.required");
});

test("session history projects persisted context and input names without mutating the session", () => {
  const store = new InMemoryRuntimeStore();
  const now = new Date().toISOString();
  store.putSession({
    id: "session-history",
    userId: "user-a",
    jobId: "job-a",
    task: "extract_all",
    status: "review_ready",
    authMode: "api_key",
    createdAt: now,
    updatedAt: now,
  });
  store.appendEvent("user-a", "session-history", "session.created", {
    contextMode: "paper_si",
  });
  store.appendEvent("user-a", "session-history", "agent.event", {
    phase: "input.validated",
    inputs: [
      { originalName: "paper.pdf" },
      { originalName: "supporting-information.pdf" },
    ],
  });
  const service = new PiRuntimeService(store, new UnconfiguredSandboxManager());
  assert.deepEqual(service.list("user-a")[0]?.inputNames, [
    "paper.pdf",
    "supporting-information.pdf",
  ]);
  assert.equal(service.list("user-a")[0]?.contextMode, "paper_si");
  assert.equal(store.getSession("user-a", "session-history").contextMode, undefined);
});

test("sealed replay history derives bounded product labels without exposing fixture paths", () => {
  const store = new InMemoryRuntimeStore();
  const now = new Date().toISOString();
  store.putSession({
    id: "session-sealed-history",
    userId: "user-a",
    jobId: "sealed-demo:example-document:session",
    task: "extract_all",
    status: "review_ready",
    authMode: "sealed_replay",
    createdAt: now,
    updatedAt: now,
  });
  store.appendEvent("user-a", "session-sealed-history", "session.created", {
    contextMode: "paper_si",
    source: {
      mode: "sealed_replay",
      fixture: "example-document",
      reconstructed: true,
    },
  });
  const service = new PiRuntimeService(store, new UnconfiguredSandboxManager());
  const presented = service.list("user-a")[0];
  assert.deepEqual(presented?.inputNames, [
    "example document.pdf",
    "example supporting information.pdf",
  ]);
  assert.equal(JSON.stringify(presented).includes("test/fixtures"), false);
  assert.equal(store.getSession("user-a", "session-sealed-history").inputNames, undefined);
});

test("GT and SI cannot leak into narrower context", () => {
  assert.throws(
    () =>
      buildMountManifest(
        request({
          contextMode: "paper",
          artifacts: [{ id: "si.pdf", kind: "si", sha256: "b".repeat(64), mediaType: "application/pdf" }],
        }),
      ),
    (error: unknown) => error instanceof RuntimePolicyError && error.code === "INVALID_CONTEXT",
  );
  const manifest = buildMountManifest(request());
  assert.equal(manifest.gtIncluded, false);
  assert.equal(JSON.stringify(manifest).includes("GT_SENTINEL"), false);
});

test("credentials are redacted before persistence", () => {
  const secret = "TEST_ONLY_super-secret-value-12345";
  const redacted = redactSecrets({ apiKey: secret, nested: { token: "opaque-token-secret" } }, [secret]);
  const serialized = JSON.stringify(redacted);
  assert.equal(serialized.includes(secret), false);
  assert.match(serialized, /REDACTED/);
});

test("tenant access is isolated", () => {
  const store = new InMemoryRuntimeStore();
  store.putSession({
    id: "session-a",
    userId: "user-a",
    jobId: "job-a",
    task: "extract_all",
    status: "ready",
    authMode: "api_key",
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  });
  assert.throws(
    () => store.getSession("user-b", "session-a"),
    (error: unknown) => error instanceof RuntimePolicyError && error.code === "CROSS_TENANT_ACCESS",
  );
});

test("local cwd is never accepted as a production sandbox", async () => {
  await assert.rejects(
    new UnconfiguredSandboxManager().create(buildMountManifest(request())),
    (error: unknown) =>
      error instanceof RuntimePolicyError && error.code === "REMOTE_SANDBOX_NOT_CONFIGURED",
  );
});

test("raw prediction stays byte-identical while reviewed snapshots advance", () => {
  const versions = new PredictionVersionStore();
  const raw = Buffer.from('{"reactions":[{"yield":"45%"}]}');
  const baseSha256 = versions.registerRaw("user-a", "job-a", raw);
  const patch = versions.propose({
    userId: "user-a",
    jobId: "job-a",
    sessionId: "session-a",
    baseSha256,
    operations: [{
      op: "replace",
      path: "/reactions/0/yield",
      value: "≤50%",
      evidenceIds: ["main:p2:scheme1"],
      rationale: "The scheme reports an upper-bound yield.",
    }],
  });
  const reviewed = versions.apply("user-a", patch.id);
  assert.equal(reviewed.version, 1);
  assert.equal((reviewed.document as { reactions: Array<{ yield: string }> }).reactions[0]?.yield, "≤50%");
  assert.deepEqual(versions.rawBytes("user-a", "job-a"), raw);
});

test("brokered secret reaches only sandbox launch and never persisted events", async () => {
  const secret = "TEST_ONLY_ephemeral-per-user-secret";
  let received: SandboxLaunchSecrets | undefined;
  const sandbox: SandboxManager = {
    async create(_manifest, launchSecrets): Promise<SandboxLease> {
      received = launchSecrets;
      return {
        id: "sandbox-a",
        userId: "user-a",
        jobId: "job-a",
        endpoint: "https://sandbox.invalid/a",
        expiresAt: new Date(Date.now() + 60_000).toISOString(),
      };
    },
    async destroy() {},
  };
  const store = new InMemoryRuntimeStore();
  const service = new PiRuntimeService(store, sandbox, {
    async redeem() {
      return { value: secret };
    },
  });
  const session = await service.create(request());
  assert.equal(received?.credential, secret);
  assert.equal(JSON.stringify(service.events("user-a", session.id)).includes(secret), false);
});

test("sandbox failures cannot persist a credential echoed by a provider", async () => {
  const secret = "TEST_ONLY_echoed-by-bad-provider";
  const store = new InMemoryRuntimeStore();
  const service = new PiRuntimeService(store, {
    async create() {
      throw new Error(`provider rejected ${secret}`);
    },
    async destroy() {},
  }, {
    async redeem() {
      return { value: secret };
    },
  });
  await assert.rejects(service.create(request()));
  const session = store.listSessions("user-a")[0];
  assert.ok(session);
  assert.equal(JSON.stringify(service.events("user-a", session.id)).includes(secret), false);
});

test("review patches reject prototype-polluting paths", () => {
  const versions = new PredictionVersionStore();
  const baseSha256 = versions.registerRaw("user-a", "job-a", Buffer.from("{}"));
  const patch = versions.propose({
    userId: "user-a",
    jobId: "job-a",
    sessionId: "session-a",
    baseSha256,
    operations: [{
      op: "add",
      path: "/__proto__/polluted",
      value: true,
      evidenceIds: ["evidence-1"],
      rationale: "malicious",
    }],
  });
  assert.throws(() => versions.apply("user-a", patch.id), /Unsafe patch path segment/);
  assert.equal(({} as { polluted?: boolean }).polluted, undefined);
});

test("local durable store survives restart and replays events after a cursor", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-runtime-"));
  try {
    const first = new DurableFileRuntimeStore(root);
    first.putSession({
      id: "session-durable",
      userId: "user-a",
      jobId: "job-a",
      task: "extract_all",
      status: "ready",
      authMode: "api_key",
      createdAt: "2000-01-01T00:00:00.000Z",
      updatedAt: "2000-01-01T00:00:00.000Z",
    });
    first.appendEvent("user-a", "session-durable", "session.created", { contextMode: "figure" });
    first.appendEvent("user-a", "session-durable", "session.ready", { sandboxId: "sandbox-a" });

    const restarted = new DurableFileRuntimeStore(root);
    assert.equal(restarted.getSession("user-a", "session-durable").jobId, "job-a");
    assert.deepEqual(restarted.listEvents("user-a", "session-durable", 1).map((event) => event.seq), [2]);
    writeFileSync(
      path.join(root, "sessions/session-durable/events/000000000003.json.crash.tmp"),
      "{\"truncated\":",
    );
    const third = restarted.appendEvent(
      "user-a",
      "session-durable",
      "agent.event",
      { phase: "perception.started" },
    );
    assert.equal(third.seq, 3);
    assert.deepEqual(
      new DurableFileRuntimeStore(root)
        .listEvents("user-a", "session-durable", 2)
        .map((event) => [event.seq, event.type]),
      [[3, "agent.event"]],
    );
    assert.deepEqual(
      readdirSync(path.join(root, "sessions/session-durable/events"))
        .filter((name) => name.endsWith(".json"))
        .sort(),
      ["000000000001.json", "000000000002.json", "000000000003.json"],
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("runtime results are write-once in memory and on disk", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-sealed-result-"));
  try {
    for (const store of [
      new InMemoryRuntimeStore(),
      new DurableFileRuntimeStore(root),
    ]) {
      store.putSession({
        id: "sealed-session",
        userId: "user-a",
        jobId: "job-a",
        task: "extract_all",
        status: "running",
        authMode: "api_key",
        createdAt: "2000-01-01T00:00:00.000Z",
        updatedAt: "2000-01-01T00:00:00.000Z",
      });
      store.putResult("user-a", "sealed-session", sealedResult("sealed-session"));
      assert.doesNotThrow(() =>
        store.putResult("user-a", "sealed-session", sealedResult("sealed-session")));
      assert.throws(
        () => store.putResult(
          "user-a",
          "sealed-session",
          sealedResult("sealed-session", "replacement"),
        ),
        (error: unknown) =>
          error instanceof RuntimePolicyError && error.code === "RESULT_ALREADY_SEALED",
      );
      assert.deepEqual(
        store.getResult("user-a", "sealed-session").manifest,
        { marker: "first" },
      );
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("local durable store keeps tenant boundaries and user-only permissions", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-runtime-"));
  try {
    const store = new DurableFileRuntimeStore(root);
    store.putSession({
      id: "session-private",
      userId: "user-a",
      jobId: "job-private",
      task: "inspect_selection",
      status: "ready",
      authMode: "personal_codex_subscription",
      createdAt: "2000-01-01T00:00:00.000Z",
      updatedAt: "2000-01-01T00:00:00.000Z",
    });
    assert.throws(
      () => store.getSession("user-b", "session-private"),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "CROSS_TENANT_ACCESS",
    );
    assert.throws(
      () => store.getSession("user-a", "../session-private"),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "SESSION_NOT_FOUND",
    );
    assert.equal(statSync(root).mode & 0o777, 0o700);
    assert.equal(
      statSync(path.join(root, "sessions/session-private/session.json")).mode & 0o777,
      0o600,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("local durable events redact credentials and reject GT before disk", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-runtime-"));
  try {
    const secret = "TEST_ONLY_local-secret-value-12345";
    const store = new DurableFileRuntimeStore(root);
    store.putSession({
      id: "session-redacted",
      userId: "user-a",
      jobId: "job-a",
      task: "inspect_selection",
      status: "running",
      authMode: "api_key",
      createdAt: "2000-01-01T00:00:00.000Z",
      updatedAt: "2000-01-01T00:00:00.000Z",
    });
    store.appendEvent(
      "user-a",
      "session-redacted",
      "agent.event",
      { message: `provider used ${secret}` },
      [secret],
    );
    assert.throws(
      () => store.appendEvent("user-a", "session-redacted", "agent.event", { value: "GT_SENTINEL" }),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "INVALID_CONTEXT",
    );
    const eventsDirectory = path.join(root, "sessions/session-redacted/events");
    const eventFile = readdirSync(eventsDirectory).find((name) => name.endsWith(".json"));
    assert.ok(eventFile);
    const disk = readFileSync(path.join(eventsDirectory, eventFile), "utf8");
    assert.equal(disk.includes(secret), false);
    assert.equal(disk.includes("GT_SENTINEL"), false);
    assert.match(disk, /REDACTED/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("two local store instances allocate monotonic event cursors", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-runtime-"));
  try {
    const first = new DurableFileRuntimeStore(root);
    first.putSession({
      id: "session-shared",
      userId: "user-a",
      jobId: "job-a",
      task: "extract_all",
      status: "running",
      authMode: "api_key",
      createdAt: "2000-01-01T00:00:00.000Z",
      updatedAt: "2000-01-01T00:00:00.000Z",
    });
    const second = new DurableFileRuntimeStore(root);
    assert.equal(first.appendEvent("user-a", "session-shared", "agent.event", { worker: 1 }).seq, 1);
    assert.equal(second.appendEvent("user-a", "session-shared", "agent.event", { worker: 2 }).seq, 2);
    assert.deepEqual(
      first.listEvents("user-a", "session-shared").map((event) => event.seq),
      [1, 2],
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("Zod validates untrusted session requests and persisted events", () => {
  assert.equal(createSessionRequestSchema.safeParse(request()).success, true);
  assert.equal(createSessionRequestSchema.safeParse({
    ...request(),
    artifacts: [{ id: "paper", kind: "paper", sha256: "not-a-hash", mediaType: "application/pdf" }],
  }).success, false);
  assert.equal(createSessionRequestSchema.safeParse({ ...request(), unexpected: true }).success, false);
  assert.equal(localAgentCreateRequestSchema.safeParse({
    userId: "local-user",
    jobId: "job-a",
    contextMode: "figure",
    task: "inspect_selection",
    workspaceId: "workspace-a",
    provider: "openai-codex",
    modelId: "gpt-5.5",
    authMode: "pi_auth",
  }).success, false);
  const piAuthRequest = {
    userId: "local-user",
    jobId: "job-a",
    contextMode: "figure",
    workspaceId: "workspace-a",
    provider: "openai-codex",
    modelId: "gpt-5.5",
    authMode: "pi_auth",
  } as const;
  assert.equal(localAgentCreateRequestSchema.safeParse(piAuthRequest).success, true);
  assert.equal(localAgentCreateRequestSchema.safeParse({
    ...piAuthRequest,
    provider: "anthropic",
  }).success, false);
  assert.equal(runtimeEventSchema.safeParse({
    seq: 1,
    sessionId: "session-a",
    at: "2000-01-01T00:00:00.000Z",
    type: "invented.progress",
    data: {},
  }).success, false);
});

test("SSE helpers preserve cursor IDs and stop retrying terminal sessions", () => {
  const event = {
    seq: 7,
    sessionId: "session-a",
    at: "2000-01-01T00:00:00.000Z",
    type: "agent.event" as const,
    data: { phase: "perception.started" },
  };
  assert.equal(parseEventCursor("4", "7"), 7);
  assert.throws(
    () => parseEventCursor("not-a-cursor", undefined),
    (error: unknown) => error instanceof RuntimePolicyError && error.code === "INVALID_CURSOR",
  );
  assert.equal(encodeSseEvent(event), `id: 7\ndata: ${JSON.stringify(event)}\n\n`);
  assert.equal(terminalSession("blocked"), true);
  assert.equal(terminalSession("running"), false);
});

test("runtime SSE replays persisted events and honors Last-Event-ID", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-runtime-sse-"));
  const modelsPath = path.join(root, "product-models.json");
  writeFileSync(modelsPath, JSON.stringify({
    providers: {
      "chemrxn-private-catalog": {
        baseUrl: "http://127.0.0.1:9/v1",
        api: "openai-completions",
        models: [{
          id: "catalog-only-model",
          input: ["text"],
        }],
      },
    },
  }), { mode: 0o600 });
  const port = 45_000 + Math.floor(Math.random() * 5_000);
  const token = "local-runtime-test-token";
  const serverPath = fileURLToPath(new URL("../src/server.js", import.meta.url));
  const child = spawn(process.execPath, [serverPath], {
    env: {
      ...process.env,
      CHEMRXN_PI_PORT: String(port),
      CHEMRXN_PI_INTERNAL_TOKEN: token,
      CHEMRXN_PI_DATA_DIR: root,
      CHEMRXN_PI_MODELS_PATH: modelsPath,
    },
    stdio: "ignore",
  });
  const base = `http://127.0.0.1:${port}`;
  const headers = {
    authorization: `Bearer ${token}`,
    "x-chemrxn-user-id": "user-a",
    "content-type": "application/json",
  };
  try {
    let ready = false;
    for (let attempt = 0; attempt < 100; attempt += 1) {
      try {
        ready = (await fetch(`${base}/healthz`)).ok;
        if (ready) break;
      } catch {}
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    assert.equal(ready, true);
    const authStatus = await fetch(`${base}/v1/auth/status`, { headers }).then(
      (response) => response.json(),
    ) as Record<string, unknown>;
    assert.deepEqual(authStatus, {
      configured: false,
      provider: "openai-codex",
      profile: "chemrxn_isolated",
    });
    assert.equal(JSON.stringify(authStatus).includes("auth.json"), false);
    const customModelResponse = await fetch(`${base}/v1/models/resolve`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        provider: "chemrxn-private-catalog",
        modelId: "catalog-only-model",
        requireImage: false,
      }),
    });
    assert.equal(customModelResponse.status, 200);
    const customModel = await customModelResponse.json() as Record<string, unknown>;
    assert.deepEqual(customModel, {
      available: true,
      provider: "chemrxn-private-catalog",
      modelId: "catalog-only-model",
      supportsImages: false,
    });
    assert.equal(JSON.stringify(customModel).includes(root), false);
    const invalidRequest = await fetch(`${base}/v1/sessions`, {
      method: "POST",
      headers,
      body: JSON.stringify({ ...request(), unexpected: true }),
    });
    assert.equal(invalidRequest.status, 400);
    const created = await fetch(`${base}/v1/sessions`, {
      method: "POST",
      headers,
      body: JSON.stringify(request({
        artifacts: [],
        auth: {
          mode: "personal_codex_subscription",
          provider: "openai-codex",
          credentialRef: "interactive",
        },
      })),
    });
    assert.equal(created.status, 503);
    const externalStore = new SqliteRuntimeStore(root);
    const sessionId = "runtime-sse-integration";
    const active = {
      id: sessionId,
      userId: "user-a",
      jobId: "job-runtime-sse",
      task: "extract_all" as const,
      status: "running" as const,
      authMode: "api_key" as const,
      createdAt: "2000-01-01T00:00:00.000Z",
      updatedAt: "2000-01-01T00:00:00.000Z",
    };
    externalStore.putSession(active);
    externalStore.appendEvent("user-a", sessionId, "session.created", {
      task: "extract_all",
      contextMode: "figure",
      source: { mode: "live_pi_sdk" },
    });
    externalStore.appendEvent("user-a", sessionId, "session.ready", {
      source: "pi_sdk",
    });
    const stream = await fetch(`${base}/v1/sessions/${sessionId}/stream?after=0`, {
      headers,
      signal: AbortSignal.timeout(3_000),
    });
    assert.equal(stream.status, 200);
    externalStore.appendEvent("user-a", sessionId, "agent.event", {
      phase: "perception.started",
    });
    externalStore.appendEvent("user-a", sessionId, "session.blocked", {
      code: "TEST_TERMINAL",
      message: "Test stream reached a terminal state.",
    });
    await new Promise((resolve) => setTimeout(resolve, 100));
    const blocked = externalStore.getSession("user-a", sessionId);
    blocked.status = "blocked";
    blocked.updatedAt = "2000-01-01T00:00:01.000Z";
    externalStore.putSession(blocked);
    const body = await stream.text();
    assert.match(body, /id: 1/);
    assert.match(body, /id: 2/);
    assert.match(body, /id: 3/);
    assert.match(body, /id: 4/);
    assert.match(body, /perception\.started/);
    assert.match(body, /session\.blocked/);
    const complete = await fetch(`${base}/v1/sessions/${sessionId}/stream?after=0`, {
      headers: { ...headers, "last-event-id": "4" },
    });
    assert.equal(complete.status, 204);
    const invalid = await fetch(`${base}/v1/sessions/${sessionId}/stream?after=bad`, { headers });
    assert.equal(invalid.status, 400);
    externalStore.appendEvent("user-a", sessionId, "agent.event", {
      phase: "workspace.command",
      workspaceCommand: {
        type: "review.propose",
        proposalId: "proposal-runtime-integration",
        target: "figure",
        source: "agent",
        jobId: active.jobId,
        reactionId: "3b",
        rationale: "The source wedge is inverted.",
        evidence: ["bbox 4"],
        changes: [],
      },
    });
    const reviewOutcomeRequest = {
      proposalId: "proposal-runtime-integration",
      jobId: active.jobId,
      target: "figure",
      status: "applied",
      reviewedVersion: 2,
      patchId: "reviewed-v2:runtime-integration",
    };
    const reviewOutcome = await fetch(
      `${base}/v1/sessions/${sessionId}/review-outcomes`,
      {
        method: "POST",
        headers,
        body: JSON.stringify(reviewOutcomeRequest),
      },
    );
    assert.equal(reviewOutcome.status, 201);
    const recordedOutcome = await reviewOutcome.json() as {
      event: { type: string; data: Record<string, unknown> };
      idempotent: boolean;
    };
    assert.equal(recordedOutcome.event.type, "review.outcome");
    assert.equal(recordedOutcome.event.data.reviewedVersion, 2);
    assert.equal(recordedOutcome.idempotent, false);
    const duplicateOutcome = await fetch(
      `${base}/v1/sessions/${sessionId}/review-outcomes`,
      {
        method: "POST",
        headers,
        body: JSON.stringify(reviewOutcomeRequest),
      },
    );
    assert.equal(duplicateOutcome.status, 200);
    assert.equal(
      ((await duplicateOutcome.json()) as { idempotent: boolean }).idempotent,
      true,
    );
    externalStore.close();

    const catalog = await fetch(`${base}/v1/replay-jobs/fixtures`, { headers }).then(
      (response) => response.json(),
    ) as { fixtures: Array<{ fixture: string; reconstructed: boolean }> };
    assert.equal(catalog.fixtures.some(
      (fixture) => fixture.fixture === "demo-figure" && fixture.reconstructed === true,
    ), true);
    const replayResponse = await fetch(`${base}/v1/replay-jobs`, {
      method: "POST",
      headers,
      body: JSON.stringify({ userId: "user-a", fixture: "demo-figure" }),
    });
    assert.equal(replayResponse.status, 201);
    const replay = await replayResponse.json() as {
      session: { id: string; status: string };
      replay: { reconstructed: boolean; liveExtraction: boolean };
    };
    assert.equal(replay.session.status, "review_ready");
    assert.deepEqual(replay.replay, { reconstructed: true, liveExtraction: false });
    const replayEvents = await fetch(
      `${base}/v1/sessions/${replay.session.id}/events?after=0`,
      { headers },
    ).then((response) => response.json()) as { events: Array<{ seq: number; type: string }> };
    assert.equal(replayEvents.events.length, 114);
    assert.equal(replayEvents.events.at(-1)?.type, "job.review_ready");
    const replayResult = await fetch(
      `${base}/v1/sessions/${replay.session.id}/result`,
      { headers },
    ).then((response) => response.json()) as {
      source: { fixture: string; reconstructed: boolean };
      gtIncluded: boolean;
    };
    assert.deepEqual(replayResult.source, {
      mode: "sealed_replay",
      fixture: "demo-figure",
      reconstructed: true,
      liveEventCapture: false,
    });
    assert.equal(replayResult.gtIncluded, false);
    const replayComplete = await fetch(
      `${base}/v1/sessions/${replay.session.id}/stream?after=114`,
      { headers },
    );
    assert.equal(replayComplete.status, 204);
    const crossTenant = await fetch(
      `${base}/v1/sessions/${replay.session.id}/result`,
      { headers: { ...headers, "x-chemrxn-user-id": "user-b" } },
    );
    assert.equal(crossTenant.status, 403);
  } finally {
    child.kill("SIGTERM");
    await new Promise((resolve) => child.once("exit", resolve));
    rmSync(root, { recursive: true, force: true });
  }
});

test("runtime shutdown rejects an admitted slow mutation without creating a ghost task", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-runtime-shutdown-admission-"));
  const readinessProbe = path.join(root, "python-readiness-probe");
  writeFileSync(readinessProbe, "#!/bin/sh\nexit 0\n", { mode: 0o700 });
  const port = 50_000 + Math.floor(Math.random() * 5_000);
  const token = "shutdown-admission-token";
  const serverPath = fileURLToPath(new URL("../src/server.js", import.meta.url));
  const repoRoot = path.resolve(process.cwd(), "..", "..");
  const child = spawn(process.execPath, [serverPath], {
    env: {
      ...process.env,
      CHEMRXN_PI_PORT: String(port),
      CHEMRXN_PI_INTERNAL_TOKEN: token,
      CHEMRXN_PI_DATA_DIR: root,
      CHEMRXN_LOCAL_AGENT_ENABLED: "1",
      CHEMRXN_REPO_ROOT: repoRoot,
      CHEMRXN_LOCAL_WORKSPACE_ROOT: path.join(root, "workspaces"),
      CHEMRXN_PI_AGENT_ROOT: path.join(root, "agents"),
      // This test exercises HTTP admission, shutdown, and queue durability. A
      // real RDKit/PyMuPDF cold import is covered by readiness tests and would
      // make this independent lifecycle assertion machine-speed dependent.
      CHEMRXN_CHEM_PYTHON: readinessProbe,
    },
    stdio: "ignore",
  });
  try {
    let ready = false;
    const readinessDeadline = Date.now() + 10_000;
    while (Date.now() < readinessDeadline) {
      try {
        ready = (await fetch(`http://127.0.0.1:${port}/healthz`)).ok;
        if (ready) break;
      } catch {}
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    assert.equal(ready, true);

    let requestHandle: ReturnType<typeof httpRequest> | undefined;
    const response = new Promise<{ status: number; body: string }>((resolve, reject) => {
      requestHandle = httpRequest({
        hostname: "127.0.0.1",
        port,
        path: "/v1/local-agent-sessions",
        method: "POST",
        headers: {
          authorization: `Bearer ${token}`,
          "x-chemrxn-user-id": "user-a",
          "content-type": "application/json",
        },
      }, (incoming) => {
        const chunks: Buffer[] = [];
        incoming.on("data", (chunk) => chunks.push(Buffer.from(chunk)));
        incoming.once("end", () => resolve({
          status: incoming.statusCode ?? 0,
          body: Buffer.concat(chunks).toString("utf8"),
        }));
      });
      requestHandle.once("error", reject);
      // Deliberately never finish the JSON body before shutdown. The request
      // has crossed route admission but must not reach prepare()/enqueue().
      requestHandle.write('{"userId":"user-a","jobId":"ghost-job",');
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    child.kill("SIGTERM");
    const rejected = await Promise.race([
      response,
      new Promise<never>((_, reject) => setTimeout(
        () => reject(new Error("slow mutation did not receive shutdown rejection")),
        3_000,
      )),
    ]);
    requestHandle?.destroy();
    assert.equal(rejected.status, 503);
    assert.equal(
      (JSON.parse(rejected.body) as { error?: { code?: string } }).error?.code,
      "RUNTIME_SHUTTING_DOWN",
    );
    await new Promise<void>((resolve) => child.once("exit", () => resolve()));
    const store = new SqliteRuntimeStore(root);
    assert.deepEqual(store.listAllSessions(), []);
    assert.deepEqual(store.listPendingWorkItems(), []);
    store.close();
  } finally {
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGKILL");
      await new Promise<void>((resolve) => child.once("exit", () => resolve()));
    }
    rmSync(root, { recursive: true, force: true });
  }
});

test("degraded restart interrupts orphaned live Pi sessions even when local agent is disabled", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-runtime-reconcile-"));
  const store = new DurableFileRuntimeStore(root);
  store.putSession({
    id: "live-orphan",
    userId: "local-user",
    jobId: "job-live",
    task: "extract_all",
    status: "ready",
    authMode: "api_key",
    createdAt: "2000-01-01T00:00:00.000Z",
    updatedAt: "2000-01-01T00:00:00.000Z",
  });
  store.appendEvent("local-user", "live-orphan", "session.created", {
    source: { mode: "live_pi_sdk", localSingleUser: true },
  });
  const port = 50_000 + Math.floor(Math.random() * 5_000);
  const serverPath = fileURLToPath(new URL("../src/server.js", import.meta.url));
  const child = spawn(process.execPath, [serverPath], {
    env: {
      ...process.env,
      CHEMRXN_PI_PORT: String(port),
      CHEMRXN_PI_INTERNAL_TOKEN: "reconcile-token",
      CHEMRXN_PI_DATA_DIR: root,
      CHEMRXN_LOCAL_AGENT_ENABLED: "0",
    },
    stdio: "ignore",
  });
  try {
    let ready = false;
    for (let attempt = 0; attempt < 100; attempt += 1) {
      try {
        ready = (await fetch(`http://127.0.0.1:${port}/healthz`)).ok;
        if (ready) break;
      } catch {}
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    assert.equal(ready, true);
    const reconciled = new DurableFileRuntimeStore(root);
    assert.equal(reconciled.getSession("local-user", "live-orphan").status, "interrupted");
    assert.equal(
      reconciled.listEvents("local-user", "live-orphan").at(-1)?.type,
      "job.interrupted",
    );
  } finally {
    child.kill("SIGTERM");
    await new Promise<void>((resolve) => child.once("exit", () => resolve()));
    rmSync(root, { recursive: true, force: true });
  }
});

test("health never claims live extraction when configured dependencies fail preflight", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-runtime-preflight-"));
  const port = 50_000 + Math.floor(Math.random() * 5_000);
  const serverPath = fileURLToPath(new URL("../src/server.js", import.meta.url));
  const child = spawn(process.execPath, [serverPath], {
    env: {
      ...process.env,
      CHEMRXN_PI_PORT: String(port),
      CHEMRXN_PI_INTERNAL_TOKEN: "preflight-token",
      CHEMRXN_PI_DATA_DIR: root,
      CHEMRXN_LOCAL_AGENT_ENABLED: "1",
      CHEMRXN_REPO_ROOT: root,
      CHEMRXN_LOCAL_WORKSPACE_ROOT: root,
      CHEMRXN_PI_AGENT_ROOT: root,
      CHEMRXN_CHEM_PYTHON: path.join(root, "missing-python"),
    },
    stdio: "ignore",
  });
  try {
    let health: Record<string, unknown> | undefined;
    for (let attempt = 0; attempt < 100; attempt += 1) {
      try {
        const response = await fetch(`http://127.0.0.1:${port}/healthz`);
        if (response.ok) {
          health = await response.json() as Record<string, unknown>;
          break;
        }
      } catch {}
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    assert.equal(health?.extractionBackend, "not_connected");
    assert.equal(health?.interactivePiBackend, "local_api_key_or_pi_auth_ready");
    assert.equal("blockers" in (health ?? {}), false);
    const doctorResponse = await fetch(`http://127.0.0.1:${port}/v1/doctor`, {
      headers: { authorization: "Bearer preflight-token" },
    });
    assert.equal(doctorResponse.status, 200);
    const doctor = await doctorResponse.json() as Record<string, unknown>;
    assert.ok((doctor.blockers as string[]).includes("chemistry_python_unavailable"));
    assert.ok((doctor.blockers as string[]).includes("figure_skill_unavailable"));
    assert.ok((doctor.blockers as string[]).includes("chemistry_mcp_unavailable"));
  } finally {
    child.kill("SIGTERM");
    await new Promise<void>((resolve) => child.once("exit", () => resolve()));
    rmSync(root, { recursive: true, force: true });
  }
});
