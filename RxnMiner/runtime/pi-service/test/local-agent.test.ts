import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { once } from "node:events";
import { createServer } from "node:http";
import type { AgentSession, ToolDefinition } from "@earendil-works/pi-coding-agent";
import {
  interruptOrphanedLocalSessions,
  LocalPiAgentService,
  projectPiEvent,
  recoveredPinnedToolNames,
  runtimeReviewerJobId,
} from "../src/local-agent.js";
import { RuntimePolicyError } from "../src/errors.js";
import { InMemoryRuntimeStore } from "../src/store.js";
import { SqliteRuntimeStore } from "../src/sqlite-store.js";
import {
  createPersistentSessionManager,
  createPiSession,
  inspectPiModel,
  type PiSessionHandle,
} from "../src/pi-driver.js";
import type { ChemMcpClient } from "../src/chem-mcp-client.js";
import { LocalInputStore } from "../src/local-inputs.js";
import { PINNED_TOOL_ALLOWLIST } from "../src/skills.js";

const REPO = path.resolve(process.cwd(), "../..");

test("legacy Pi conversations keep their recorded tool set across new UI tools", () => {
  const legacyReady = [{
    type: "session.ready",
    data: { tools: ["verify_smiles", "render_reactions"] },
  }] as never;
  assert.deepEqual(
    recoveredPinnedToolNames(
      legacyReady,
      ["verify_smiles", "render_reactions", "open_chemistry_workspace", "propose_chemistry_correction"],
    ),
    ["verify_smiles", "render_reactions"],
  );
  assert.throws(
    () => recoveredPinnedToolNames(legacyReady, ["verify_smiles"]),
    /Pinned Pi tool is no longer available: render_reactions/,
  );
  assert.deepEqual(
    recoveredPinnedToolNames(
      [{ type: "session.ready", data: { tools: [] } }] as never,
      ["verify_smiles", "open_chemistry_workspace", "propose_chemistry_correction"],
    ),
    ["verify_smiles", "open_chemistry_workspace", "propose_chemistry_correction"],
  );
});

test("Pi SessionManager persists two isolated product conversations and reopens each", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-pi-session-manager-"));
  const cwdA = path.join(root, "workspaces", "product-session-a");
  const cwdB = path.join(root, "workspaces", "product-session-b");
  const sessionDirA = path.join(root, "private-sessions", "product-session-a");
  const sessionDirB = path.join(root, "private-sessions", "product-session-b");
  for (const directory of [cwdA, cwdB]) mkdirSync(directory, { recursive: true });
  for (const directory of [sessionDirA, sessionDirB]) {
    mkdirSync(directory, { recursive: true, mode: 0o700 });
  }
  try {
    const createdA = await createPersistentSessionManager({
      cwd: cwdA,
      sessionDir: sessionDirA,
      sessionId: "product-session-a",
    });
    createdA.appendMessage({
      role: "assistant",
      content: [{ type: "text", text: "Persist only in conversation A" }],
      stopReason: "stop",
      timestamp: Date.now(),
    } as never);
    createdA.appendCustomEntry("chemrxn.test", { conversation: "a" });
    const createdB = await createPersistentSessionManager({
      cwd: cwdB,
      sessionDir: sessionDirB,
      sessionId: "product-session-b",
    });
    createdB.appendMessage({
      role: "assistant",
      content: [{ type: "text", text: "Persist only in conversation B" }],
      stopReason: "stop",
      timestamp: Date.now(),
    } as never);
    createdB.appendCustomEntry("chemrxn.test", { conversation: "b" });

    assert.equal(createdA.getSessionId(), "product-session-a");
    assert.equal(createdB.getSessionId(), "product-session-b");
    assert.ok(createdA.getSessionFile()?.startsWith(sessionDirA));
    assert.ok(createdB.getSessionFile()?.startsWith(sessionDirB));
    assert.notEqual(createdA.getSessionFile(), createdB.getSessionFile());
    const filesA = readdirSync(sessionDirA).filter((name) => name.endsWith(".jsonl"));
    const filesB = readdirSync(sessionDirB).filter((name) => name.endsWith(".jsonl"));
    assert.deepEqual(filesA.length, 1);
    assert.deepEqual(filesB.length, 1);
    const headerA = JSON.parse(readFileSync(path.join(sessionDirA, filesA[0]!), "utf8").split("\n")[0]!);
    const headerB = JSON.parse(readFileSync(path.join(sessionDirB, filesB[0]!), "utf8").split("\n")[0]!);
    assert.equal(headerA.cwd, cwdA);
    assert.equal(headerB.cwd, cwdB);

    const reopenedA = await createPersistentSessionManager({
      cwd: cwdA,
      sessionDir: sessionDirA,
      sessionId: "product-session-a",
      resume: true,
    });
    const reopenedB = await createPersistentSessionManager({
      cwd: cwdB,
      sessionDir: sessionDirB,
      sessionId: "product-session-b",
      resume: true,
    });
    assert.equal(reopenedA.getSessionId(), "product-session-a");
    assert.equal(reopenedB.getSessionId(), "product-session-b");
    assert.deepEqual(
      reopenedA.getEntries().map((entry) => entry.type),
      ["message", "custom"],
    );
    assert.deepEqual(
      reopenedB.getEntries().map((entry) => entry.type),
      ["message", "custom"],
    );
    assert.deepEqual(
      (reopenedA.getEntries()[1] as { data?: unknown }).data,
      { conversation: "a" },
    );
    assert.deepEqual(
      (reopenedB.getEntries()[1] as { data?: unknown }).data,
      { conversation: "b" },
    );
    const wrongDirectory = await createPersistentSessionManager({
      cwd: cwdA,
      sessionDir: sessionDirB,
      sessionId: "product-session-a",
      resume: true,
    });
    assert.deepEqual(wrongDirectory.getEntries(), []);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("real Pi SDK uses the product-owned model catalog and resumes a loopback conversation", {
  timeout: 30_000,
}, async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-pi-loopback-"));
  const cwd = path.join(root, "workspace");
  const agentDir = path.join(root, "agent");
  const sessionDir = path.join(agentDir, "sessions", "loopback-session");
  const modelsPath = path.join(agentDir, "models.json");
  const apiKey = "loopback-test-key";
  const requests: Array<Record<string, unknown>> = [];
  const originalPiStartupEnv = {
    offline: process.env.PI_OFFLINE,
    versionCheck: process.env.PI_SKIP_VERSION_CHECK,
    telemetry: process.env.PI_TELEMETRY,
  };
  // The embedded product must override CLI startup-network preferences while
  // still allowing the explicitly selected model endpoint below.
  process.env.PI_OFFLINE = "0";
  process.env.PI_SKIP_VERSION_CHECK = "0";
  process.env.PI_TELEMETRY = "1";
  const server = createServer(async (request, response) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    requests.push(JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, unknown>);
    const turn = requests.length;
    response.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      connection: "keep-alive",
    });
    const chunkBase = {
      id: `chatcmpl-loopback-${turn}`,
      object: "chat.completion.chunk",
      created: Math.floor(Date.now() / 1000),
      model: "chemrxn-loopback",
    };
    response.write(`data: ${JSON.stringify({
      ...chunkBase,
      choices: [{
        index: 0,
        delta: { role: "assistant", content: `Loopback answer ${turn}` },
        finish_reason: null,
      }],
    })}\n\n`);
    response.write(`data: ${JSON.stringify({
      ...chunkBase,
      choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
      usage: {
        prompt_tokens: 10,
        completion_tokens: 3,
        total_tokens: 13,
      },
    })}\n\n`);
    response.end("data: [DONE]\n\n");
  });
  try {
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const address = server.address();
    assert.ok(address && typeof address === "object");
    for (const directory of [cwd, agentDir, sessionDir]) {
      mkdirSync(directory, { recursive: true, mode: 0o700 });
    }
    writeFileSync(modelsPath, JSON.stringify({
      providers: {
        "chemrxn-loopback": {
          baseUrl: `http://127.0.0.1:${address.port}/v1`,
          api: "openai-completions",
          models: [{
            id: "chemrxn-loopback",
            name: "ChemRxn loopback acceptance model",
            reasoning: false,
            input: ["text"],
            contextWindow: 32_000,
            maxTokens: 1_024,
            cost: {
              input: 0,
              output: 0,
              cacheRead: 0,
              cacheWrite: 0,
            },
            compat: {
              supportsDeveloperRole: false,
              supportsReasoningEffort: false,
            },
          }],
        },
      },
    }), { mode: 0o600 });

    assert.deepEqual(
      await inspectPiModel("chemrxn-loopback", "chemrxn-loopback", modelsPath),
      {
        available: true,
        provider: "chemrxn-loopback",
        modelId: "chemrxn-loopback",
        supportsImages: false,
      },
    );
    assert.equal(requests.length, 0, "model preflight must not contact the configured endpoint");
    assert.equal(process.env.PI_OFFLINE, "1");
    assert.equal(process.env.PI_SKIP_VERSION_CHECK, "1");
    assert.equal(process.env.PI_TELEMETRY, "0");

    const firstEvents: unknown[] = [];
    const first = await createPiSession({
      cwd,
      agentDir,
      provider: "chemrxn-loopback",
      modelId: "chemrxn-loopback",
      thinkingLevel: "off",
      authMode: "api_key",
      apiKey,
      modelsPath,
      sessionDir,
      sessionId: "loopback-session",
      skills: [],
      tools: [],
      customTools: [],
      onEvent: (event) => firstEvents.push(event),
    });
    await first.session.prompt("First durable turn");
    await first.dispose();

    const secondEvents: unknown[] = [];
    const reopened = await createPiSession({
      cwd,
      agentDir,
      provider: "chemrxn-loopback",
      modelId: "chemrxn-loopback",
      thinkingLevel: "off",
      authMode: "api_key",
      apiKey,
      modelsPath,
      sessionDir,
      sessionId: "loopback-session",
      resume: true,
      skills: [],
      tools: [],
      customTools: [],
      onEvent: (event) => secondEvents.push(event),
    });
    await reopened.session.prompt("Second durable turn");
    await reopened.dispose();

    assert.equal(requests.length, 2);
    const secondMessages = (requests[1]?.messages ?? []) as Array<{
      role?: string;
      content?: string | Array<{ type?: string; text?: string }>;
    }>;
    const requestText = (
      content: string | Array<{ type?: string; text?: string }> | undefined,
    ) => typeof content === "string"
      ? content
      : (content ?? [])
        .filter((part) => part.type === "text" && typeof part.text === "string")
        .map((part) => part.text)
        .join("");
    assert.equal(
      secondMessages.some((message) =>
        message.role === "user" && requestText(message.content) === "First durable turn"),
      true,
    );
    assert.equal(
      secondMessages.some((message) =>
        message.role === "assistant" && requestText(message.content) === "Loopback answer 1"),
      true,
    );
    assert.equal(
      secondMessages.some((message) =>
        message.role === "user" && requestText(message.content) === "Second durable turn"),
      true,
    );
    assert.equal(JSON.stringify(firstEvents).includes("Loopback answer 1"), true);
    assert.equal(JSON.stringify(secondEvents).includes("Loopback answer 2"), true);
    const jsonlFiles = readdirSync(sessionDir).filter((name) => name.endsWith(".jsonl"));
    assert.equal(jsonlFiles.length, 1);
    const durableConversation = readFileSync(
      path.join(sessionDir, jsonlFiles[0]!),
      "utf8",
    );
    assert.match(durableConversation, /First durable turn/);
    assert.match(durableConversation, /Loopback answer 1/);
    assert.match(durableConversation, /Second durable turn/);
    assert.match(durableConversation, /Loopback answer 2/);
    assert.equal(durableConversation.includes(apiKey), false);
  } finally {
    server.close();
    await once(server, "close").catch(() => undefined);
    if (originalPiStartupEnv.offline === undefined) delete process.env.PI_OFFLINE;
    else process.env.PI_OFFLINE = originalPiStartupEnv.offline;
    if (originalPiStartupEnv.versionCheck === undefined) delete process.env.PI_SKIP_VERSION_CHECK;
    else process.env.PI_SKIP_VERSION_CHECK = originalPiStartupEnv.versionCheck;
    if (originalPiStartupEnv.telemetry === undefined) delete process.env.PI_TELEMETRY;
    else process.env.PI_TELEMETRY = originalPiStartupEnv.telemetry;
    rmSync(root, { recursive: true, force: true });
  }
});

test("direct Pi auth fails before SDK initialization without a ChemRxn-owned auth path", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-pi-auth-fail-closed-"));
  try {
    await assert.rejects(
      createPiSession({
        cwd: root,
        agentDir: path.join(root, "agent"),
        provider: "openai-codex",
        modelId: "gpt-5.5",
        authMode: "pi_auth",
        sessionDir: path.join(root, "sessions"),
        sessionId: "must-not-initialize",
        skills: [],
        tools: [],
        customTools: [],
        onEvent: () => {},
      }),
      /ChemRxn-owned auth path is required/,
    );
    assert.equal(existsSync(path.join(root, "agent")), false);
    assert.equal(existsSync(path.join(root, "sessions")), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("local Pi service persists conversation controls but never the BYO key", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-local-agent-"));
  const store = new InMemoryRuntimeStore();
  const inputStore = new LocalInputStore(path.join(root, "inputs"));
  const figure = await inputStore.stage(
    "local-user",
    (async function* () {
      yield readFileSync(
        path.resolve(process.cwd(), "..", "..", "runtime", "pi-service", "test", "fixtures", "figure", "demo-figure", "image.png"),
      );
    })(),
    { kind: "figure", originalName: "figure.png", mediaType: "image/png" },
  );
  let onEvent: ((event: unknown) => void) | undefined;
  let streaming = false;
  const calls: string[] = [];
  let delayAbort = false;
  let releaseAbort = () => {};
  const abortGate = new Promise<void>((resolve) => { releaseAbort = resolve; });
  const session = {
    get isStreaming() { return streaming; },
    prompt: async (text: string) => { calls.push(`prompt:${text}`); },
    steer: async (text: string) => { calls.push(`steer:${text}`); },
    followUp: async (text: string) => { calls.push(`follow:${text}`); },
    abort: async () => {
      calls.push("abort");
      if (delayAbort) await abortGate;
    },
  } as unknown as AgentSession;
  let piDisposed = false;
  const piKeys: Array<string | undefined> = [];
  let piToolNames: string[] = [];
  let piCustomToolNames: string[] = [];
  const pi: PiSessionHandle = {
    session,
    supportsImages: true,
    async dispose() { piDisposed = true; },
  };
  let mcpTerminated = false;
  const mcp = {
    dispose() {},
    async terminate() { mcpTerminated = true; },
  } as unknown as ChemMcpClient;
  const service = new LocalPiAgentService(store, {
    repoRoot: REPO,
    workspaceRoot: path.join(root, "workspaces"),
    agentRoot: path.join(root, "agents"),
    python: "python",
    inputStore,
  }, {
    randomId: (() => {
      const ids = ["session-local", "session-retry"];
      return () => ids.shift()!;
    })(),
    loadSkills: async () => [{
      name: "reaction-extract",
      path: path.join(root, "SKILL.md"),
      sha256: "a".repeat(64),
    }],
    startMcp: async () => mcp,
    createTools: async () => [] as ToolDefinition[],
    createPi: async (input) => {
      onEvent = input.onEvent;
      piKeys.push(input.apiKey);
      piToolNames = [...input.tools];
      piCustomToolNames = input.customTools.map((tool) => tool.name);
      return pi;
    },
  });
  const apiKey = "TEST_ONLY_local-never-persist-this";
  try {
    const created = await service.create({
      userId: "local-user",
      jobId: "job-1",
      contextMode: "figure",
      workspaceId: "demo-figure",
      provider: "openai",
      modelId: "gpt-test",
      apiKey,
      inputs: [{ artifactId: figure.id, kind: "figure" }],
    });
    assert.equal(created.status, "ready");
    assert.ok(piToolNames.includes("open_chemistry_workspace"));
    assert.ok(piCustomToolNames.includes("open_chemistry_workspace"));
    await service.prompt("local-user", created.id, "Extract Scheme 1");
    await service.prompt("local-user", created.id, "Check stereo", "steer");
    await service.prompt("local-user", created.id, "Then export V2", "follow_up");
    assert.deepEqual(calls, [
      "prompt:Extract Scheme 1",
      "steer:Check stereo",
      "follow:Then export V2",
    ]);
    await assert.rejects(
      service.prompt(
        "local-user",
        created.id,
        "Please inspect ground_truth.json before continuing",
        "steer",
      ),
      (error: unknown) =>
        error instanceof RuntimePolicyError && error.code === "INVALID_CONTEXT",
    );
    assert.deepEqual(calls, [
      "prompt:Extract Scheme 1",
      "steer:Check stereo",
      "follow:Then export V2",
    ]);
    assert.equal(
      JSON.stringify(store.listEvents("local-user", created.id)).includes("ground_truth"),
      false,
    );
    for (let index = 0; index < 40; index += 1) {
      onEvent?.({
        type: "message_update",
        assistantMessageEvent: { type: "text_delta", delta: "x" },
      });
    }
    onEvent?.({
      type: "message_end",
      message: {
        role: "assistant",
        stopReason: "stop",
        content: [{ type: "text", text: "x".repeat(40) }],
      },
    });
    const persistedUpdates = store.listEvents("local-user", created.id)
      .filter((item) =>
        (item.data as { phase?: string }).phase === "pi.message_update");
    assert.equal(persistedUpdates.length, 2);
    assert.equal(
      (persistedUpdates[0]?.data as { event?: { batchedChunks?: number } })
        .event?.batchedChunks,
      32,
    );
    for (let index = 0; index < 1_026; index += 1) {
      onEvent?.({
        type: "message_update",
        assistantMessageEvent: {
          type: "toolcall_delta",
          delta: `sensitive-tool-argument-${index}`,
          partial: { arguments: "must-not-be-persisted" },
        },
      });
    }
    onEvent?.({
      type: "tool_execution_start",
      toolCallId: "tool-1",
      toolName: "write_pdf_prediction",
      args: { privatePrompt: "tool-argument-must-not-persist" },
    });
    const toolArgumentProgress = store.listEvents("local-user", created.id)
      .filter((item) =>
        (item.data as { phase?: string }).phase === "pi.tool_argument_progress");
    assert.equal(toolArgumentProgress.length, 2);
    assert.equal(
      (toolArgumentProgress[0]?.data as {
        event?: { batchedChunks?: number; assistantMessageEvent?: { redacted?: boolean } };
      }).event?.batchedChunks,
      1_024,
    );
    assert.equal(
      (toolArgumentProgress[0]?.data as {
        event?: { assistantMessageEvent?: { redacted?: boolean } };
      }).event?.assistantMessageEvent?.redacted,
      true,
    );
    assert.equal(
      JSON.stringify(store.listEvents("local-user", created.id))
        .includes("sensitive-tool-argument"),
      false,
    );
    assert.equal(
      JSON.stringify(store.listEvents("local-user", created.id))
        .includes("must-not-be-persisted"),
      false,
    );
    assert.equal(
      JSON.stringify(store.listEvents("local-user", created.id))
        .includes("tool-argument-must-not-persist"),
      false,
    );

    streaming = true;
    await assert.rejects(
      service.prompt("local-user", created.id, "This must not persist"),
      (error: unknown) => error instanceof RuntimePolicyError && error.code === "AGENT_BUSY",
    );
    const beforeRejectedEvent = store.listEvents("local-user", created.id);
    assert.equal(JSON.stringify(beforeRejectedEvent).includes("This must not persist"), false);

    assert.doesNotThrow(() => onEvent?.({
      type: "message_end",
      message: { role: "assistant", content: [{ type: "text", text: "GT_SENTINEL" }] },
    }));
    const persisted = JSON.stringify(store.listEvents("local-user", created.id));
    assert.equal(persisted.includes(apiKey), false);
    assert.equal(persisted.includes("GT_SENTINEL"), false);
    assert.match(persisted, /PI_EVENT_REJECTED/);
    onEvent?.({ type: "agent_end" });
    await new Promise((resolve) => setTimeout(resolve, 5));
    assert.equal(store.getSession("local-user", created.id).status, "ready");
    assert.equal(store.listEvents("local-user", created.id).at(-1)?.type, "session.ready");
    assert.equal(
      (store.listEvents("local-user", created.id).at(-1)?.data as { reason?: string }).reason,
      "awaiting_artifact",
    );
    streaming = false;
    await service.prompt("local-user", created.id, "Prepare cancellation");
    assert.equal(store.getSession("local-user", created.id).status, "running");
    delayAbort = true;
    const cancelling = service.abort("local-user", created.id);
    await new Promise((resolve) => setTimeout(resolve, 5));
    assert.equal(store.getSession("local-user", created.id).status, "running");
    assert.notEqual(store.listEvents("local-user", created.id).at(-1)?.type, "job.cancelled");
    await assert.rejects(
      service.prompt("local-user", created.id, "Message during cancellation"),
      (error: unknown) =>
        error instanceof RuntimePolicyError && error.code === "AGENT_BUSY",
    );
    const beforeLateCallback = store.listEvents("local-user", created.id).length;
    onEvent?.({ type: "agent_end" });
    assert.equal(store.listEvents("local-user", created.id).length, beforeLateCallback);
    releaseAbort();
    await cancelling;
    assert.equal(mcpTerminated, true);
    assert.equal(piDisposed, true);
    assert.equal(store.getSession("local-user", created.id).status, "cancelled");
    assert.equal(store.listEvents("local-user", created.id).at(-1)?.type, "job.cancelled");
    onEvent?.({ type: "agent_end" });
    await new Promise((resolve) => setTimeout(resolve, 5));
    assert.equal(store.listEvents("local-user", created.id).at(-1)?.type, "job.cancelled");
    await assert.rejects(
      service.prompt("local-user", created.id, "Resurrect session"),
      (error: unknown) =>
        error instanceof RuntimePolicyError && error.code === "SESSION_NOT_ACTIVE",
    );
    assert.equal(JSON.stringify(store.listEvents("local-user", created.id))
      .includes("Resurrect session"), false);
    const replacementKey = "TEST_ONLY_replacement-memory-only";
    const retried = await service.retry("local-user", created.id, replacementKey);
    assert.equal(retried.id, "session-retry");
    assert.equal(retried.status, "running");
    assert.equal(retried.parentSessionId, created.id);
    assert.equal(
      JSON.stringify(store.listEvents("local-user", retried.id)).includes(apiKey),
      false,
    );
    assert.equal(
      JSON.stringify(store.listEvents("local-user", retried.id)).includes(replacementKey),
      false,
    );
    assert.deepEqual(piKeys, [apiKey, replacementKey]);
    await assert.rejects(
      service.retry("local-user", created.id),
      (error: unknown) =>
        error instanceof RuntimePolicyError
        && error.code === "SESSION_NOT_ACTIVE"
        && /Supply it again/.test(error.message),
    );
  } finally {
    await service.dispose("session-local");
    await service.dispose("session-retry");
    rmSync(root, { recursive: true, force: true });
  }
});

test("runtime restart interrupts durable local Pi sessions instead of replaying forever", () => {
  const store = new InMemoryRuntimeStore();
  store.putSession({
    id: "orphaned",
    userId: "local-user",
    jobId: "job-1",
    task: "extract_all",
    status: "running",
    authMode: "api_key",
    createdAt: "2000-01-01T00:00:00.000Z",
    updatedAt: "2000-01-01T00:00:00.000Z",
  });
  store.appendEvent("local-user", "orphaned", "session.created", {
    source: { mode: "live_pi_sdk", localSingleUser: true },
  });
  assert.equal(interruptOrphanedLocalSessions(store), 1);
  assert.equal(store.getSession("local-user", "orphaned").status, "interrupted");
  const terminal = store.listEvents("local-user", "orphaned").at(-1);
  assert.equal(terminal?.type, "job.interrupted");
  assert.equal((terminal?.data as { code: string }).code, "LOCAL_RUNTIME_RESTART");
  assert.equal((terminal?.data as { resumable: boolean }).resumable, true);
  assert.equal(
    (terminal?.data as { credentialRequired: string }).credentialRequired,
    "api_key",
  );
  assert.equal(interruptOrphanedLocalSessions(store), 0);
});

test("runtime restart parks a ready Pi conversation as dormant for lazy recovery", () => {
  const store = new InMemoryRuntimeStore();
  store.putSession({
    id: "ready-conversation",
    userId: "local-user",
    jobId: "job-ready",
    task: "inspect_selection",
    status: "ready",
    authMode: "personal_codex_subscription",
    createdAt: "2000-01-01T00:00:00.000Z",
    updatedAt: "2000-01-01T00:00:00.000Z",
  });
  store.appendEvent("local-user", "ready-conversation", "session.created", {
    source: { mode: "live_pi_sdk", localSingleUser: true },
  });

  assert.equal(interruptOrphanedLocalSessions(store), 1);
  assert.equal(store.getSession("local-user", "ready-conversation").status, "dormant");
  const event = store.listEvents("local-user", "ready-conversation").at(-1);
  assert.equal(event?.type, "session.dormant");
  assert.equal((event?.data as { credentialRequired: string }).credentialRequired, "pi_auth");
  assert.equal(interruptOrphanedLocalSessions(store), 0);
});

test("a dormant Pi-auth conversation lazily rehydrates once and resumes prompting", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-dormant-rehydrate-"));
  const store = new InMemoryRuntimeStore();
  store.putSession({
    id: "dormant-session",
    userId: "local-user",
    jobId: "job-dormant",
    task: "inspect_selection",
    contextMode: "figure",
    status: "dormant",
    authMode: "personal_codex_subscription",
    sandboxId: "dormant-session",
    createdAt: "2000-01-01T00:00:00.000Z",
    updatedAt: "2000-01-01T00:01:00.000Z",
  });
  store.appendEvent("local-user", "dormant-session", "session.created", {
    source: { mode: "live_pi_sdk", localSingleUser: true },
    contextMode: "figure",
    provider: "openai-codex",
    modelId: "gpt-test",
    authMode: "pi_auth",
    inputArtifactIds: [],
  });
  store.appendEvent("local-user", "dormant-session", "session.ready", {
    source: "pi_sdk",
    skillHashes: [{ name: "reaction-extract", sha256: "a".repeat(64) }],
    tools: [...PINNED_TOOL_ALLOWLIST],
  });
  store.appendEvent("local-user", "dormant-session", "session.dormant", {
    credentialRequired: "pi_auth",
  });
  const prompts: string[] = [];
  let createPiCalls = 0;
  let piInput: Parameters<typeof createPiSession>[0] | undefined;
  const pi: PiSessionHandle = {
    session: {
      isStreaming: false,
      prompt: async (text: string) => { prompts.push(text); },
    } as unknown as AgentSession,
    async dispose() {},
  };
  const mcp = {
    dispose() {},
    async terminate() {},
  } as unknown as ChemMcpClient;
  const service = new LocalPiAgentService(store, {
    repoRoot: REPO,
    workspaceRoot: path.join(root, "workspaces"),
    agentRoot: path.join(root, "agents"),
    authPath: path.join(root, "auth.json"),
    python: "python",
  }, {
    loadSkills: async () => [{
      name: "reaction-extract",
      path: path.join(root, "SKILL.md"),
      sha256: "a".repeat(64),
    }],
    startMcp: async () => mcp,
    createTools: async () => [],
    createPi: async (input) => {
      createPiCalls += 1;
      piInput = input;
      await new Promise((resolve) => setTimeout(resolve, 5));
      return pi;
    },
  });
  try {
    await Promise.all([
      service.prompt("local-user", "dormant-session", "First recovered message"),
      service.prompt("local-user", "dormant-session", "Second recovered message"),
    ]);
    assert.equal(createPiCalls, 1);
    assert.equal(piInput?.resume, true);
    assert.equal(piInput?.sessionId, "dormant-session");
    assert.equal(piInput?.authMode, "pi_auth");
    assert.ok(piInput?.modelsPath?.startsWith(path.join(root, "agents")));
    assert.equal(piInput?.modelsPath?.endsWith("models.json"), true);
    assert.deepEqual(prompts.sort(), [
      "First recovered message",
      "Second recovered message",
    ]);
    assert.equal(store.getSession("local-user", "dormant-session").status, "running");
    assert.equal(JSON.stringify(store.listEvents("local-user", "dormant-session")).includes(root), false);
  } finally {
    await service.dispose("dormant-session");
    rmSync(root, { recursive: true, force: true });
  }
});

test("disposeAll aborts and settles an in-flight dormant rehydration", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-dormant-shutdown-"));
  const store = new InMemoryRuntimeStore();
  store.putSession({
    id: "dormant-shutdown",
    userId: "local-user",
    jobId: "job-dormant-shutdown",
    task: "inspect_selection",
    contextMode: "figure",
    status: "dormant",
    authMode: "personal_codex_subscription",
    sandboxId: "dormant-shutdown",
    createdAt: "2000-01-01T00:00:00.000Z",
    updatedAt: "2000-01-01T00:01:00.000Z",
  });
  store.appendEvent("local-user", "dormant-shutdown", "session.created", {
    source: { mode: "live_pi_sdk", localSingleUser: true },
    contextMode: "figure",
    provider: "openai-codex",
    modelId: "gpt-test",
    authMode: "pi_auth",
    inputArtifactIds: [],
  });
  store.appendEvent("local-user", "dormant-shutdown", "session.ready", {
    source: "pi_sdk",
    skillHashes: [{ name: "reaction-extract", sha256: "a".repeat(64) }],
    tools: [...PINNED_TOOL_ALLOWLIST],
  });
  store.appendEvent("local-user", "dormant-shutdown", "session.dormant", {
    credentialRequired: "pi_auth",
  });
  let mcpStarted!: () => void;
  let releaseMcp!: () => void;
  const started = new Promise<void>((resolve) => { mcpStarted = resolve; });
  const gate = new Promise<void>((resolve) => { releaseMcp = resolve; });
  let terminated = 0;
  let createPiCalls = 0;
  const service = new LocalPiAgentService(store, {
    repoRoot: REPO,
    workspaceRoot: path.join(root, "workspaces"),
    agentRoot: path.join(root, "agents"),
    python: "python",
  }, {
    loadSkills: async () => [{
      name: "reaction-extract",
      path: path.join(root, "SKILL.md"),
      sha256: "a".repeat(64),
    }],
    startMcp: async () => {
      mcpStarted();
      await gate;
      return ({
        dispose() {},
        async terminate() { terminated += 1; },
      }) as unknown as ChemMcpClient;
    },
    createTools: async () => [],
    createPi: async () => {
      createPiCalls += 1;
      throw new Error("Pi must not start after runtime shutdown");
    },
  });
  try {
    const prompting = service.prompt(
      "local-user",
      "dormant-shutdown",
      "Resume while the runtime stops",
    );
    await started;
    let disposed = false;
    const disposing = service.disposeAll().finally(() => { disposed = true; });
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(disposed, false, "shutdown must wait for the late MCP handle cleanup");
    releaseMcp();
    await disposing;
    await assert.rejects(
      prompting,
      (error: unknown) => error instanceof RuntimePolicyError
        && error.code === "RUNTIME_SHUTTING_DOWN",
    );
    assert.equal(terminated, 1);
    assert.equal(createPiCalls, 0);
    assert.equal(store.getSession("local-user", "dormant-shutdown").status, "interrupted");
    assert.equal(
      (service as unknown as { active: Map<string, unknown> }).active.size,
      0,
    );
  } finally {
    releaseMcp?.();
    await service.disposeAll();
    rmSync(root, { recursive: true, force: true });
  }
});

test("a dormant BYO-key conversation requires re-entry without persisting the key", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-dormant-key-"));
  const store = new InMemoryRuntimeStore();
  store.putSession({
    id: "dormant-key-session",
    userId: "local-user",
    jobId: "job-key",
    task: "extract_all",
    status: "dormant",
    authMode: "api_key",
    createdAt: "2000-01-01T00:00:00.000Z",
    updatedAt: "2000-01-01T00:01:00.000Z",
  });
  const service = new LocalPiAgentService(store, {
    repoRoot: REPO,
    workspaceRoot: path.join(root, "workspaces"),
    agentRoot: path.join(root, "agents"),
    python: "python",
  });
  try {
    await assert.rejects(
      service.prompt("local-user", "dormant-key-session", "Resume"),
      (error: unknown) =>
        error instanceof RuntimePolicyError
        && error.code === "CREDENTIAL_REQUIRED",
    );
    assert.equal(store.getSession("local-user", "dormant-key-session").status, "dormant");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

function addRawMolParserLineage(predictionPath: string, molparserPath: string): void {
  const prediction = JSON.parse(readFileSync(predictionPath, "utf8")) as Record<string, unknown>;
  const raw = JSON.parse(readFileSync(molparserPath, "utf8")) as Array<Record<string, unknown>>;
  const md5ByBbox = new Map(raw.filter((item) => Number.isInteger(item.bbox_index) && typeof item.md5 === "string")
    .map((item) => [item.bbox_index as number, item.md5 as string]));
  for (const key of ["reaction_templates", "selected_examples"] as const) {
    for (const reaction of (prediction[key] as Array<Record<string, unknown>> || [])) {
      for (const role of ["reactants", "products"] as const) {
        for (const molecule of (reaction[role] as Array<Record<string, unknown>> || [])) {
          const source = molecule._source && typeof molecule._source === "object"
            ? molecule._source as Record<string, unknown> : molecule;
          const bboxIndex = Number.isInteger(source.bbox_index)
            ? source.bbox_index as number : Number.isInteger(molecule.bbox_index) ? molecule.bbox_index as number : undefined;
          if (bboxIndex !== undefined && md5ByBbox.has(bboxIndex)) source.molparser_md5 = md5ByBbox.get(bboxIndex);
        }
      }
    }
  }
  writeFileSync(predictionPath, JSON.stringify(prediction));
}

test("a disposed local agent service cannot start a new rehydration", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-dormant-closed-service-"));
  const store = new InMemoryRuntimeStore();
  store.putSession({
    id: "dormant-after-dispose",
    userId: "local-user",
    jobId: "job-after-dispose",
    task: "inspect_selection",
    contextMode: "figure",
    status: "dormant",
    authMode: "personal_codex_subscription",
    createdAt: "2000-01-01T00:00:00.000Z",
    updatedAt: "2000-01-01T00:00:00.000Z",
  });
  let mcpCalls = 0;
  const service = new LocalPiAgentService(store, {
    repoRoot: REPO,
    workspaceRoot: path.join(root, "workspaces"),
    agentRoot: path.join(root, "agents"),
    python: "python",
  }, {
    startMcp: async () => {
      mcpCalls += 1;
      throw new Error("closed service must not start MCP");
    },
  });
  try {
    await service.disposeAll();
    await assert.rejects(
      service.prompt("local-user", "dormant-after-dispose", "Do not reopen"),
      (error: unknown) => error instanceof RuntimePolicyError
        && error.code === "RUNTIME_SHUTTING_DOWN",
    );
    assert.equal(mcpCalls, 0);
    assert.equal(store.getSession("local-user", "dormant-after-dispose").status, "dormant");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("a successful seal transfers a BYO key once in memory to its linked child", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-byo-continuation-"));
  const store = new InMemoryRuntimeStore();
  const inputStore = new LocalInputStore(path.join(root, "inputs"));
  const figureBytes = readFileSync(
    path.resolve(process.cwd(), "..", "..", "runtime", "pi-service", "test", "fixtures", "figure", "demo-figure", "image.png"),
  );
  const figure = await inputStore.stage(
    "local-user",
    (async function* () { yield figureBytes; })(),
    { kind: "figure", originalName: "figure.png", mediaType: "image/png" },
  );
  const ids = ["byo-parent", "byo-child"];
  const eventSinks: Array<(event: unknown) => void> = [];
  let disposed = 0;
  let terminated = 0;
  let releaseParentCleanup!: () => void;
  const parentCleanup = new Promise<void>((resolve) => {
    releaseParentCleanup = resolve;
  });
  let mcpIndex = 0;
  const service = new LocalPiAgentService(store, {
    repoRoot: REPO,
    workspaceRoot: path.join(root, "workspaces"),
    agentRoot: path.join(root, "agents"),
    inputStore,
    credentialLeaseTtlMs: 200,
    python: "python",
  }, {
    randomId: () => ids.shift()!,
    loadSkills: async () => [{
      name: "reaction-extract",
      path: path.join(root, "SKILL.md"),
      sha256: "a".repeat(64),
    }],
    startMcp: async () => {
      const index = mcpIndex++;
      return ({
        dispose() {},
        async terminate() {
          if (index === 0) await parentCleanup;
          terminated += 1;
        },
      }) as unknown as ChemMcpClient;
    },
    createTools: async () => [],
    createPi: async (input) => {
      const index = eventSinks.length;
      eventSinks.push(input.onEvent);
      return {
        session: {
          isStreaming: false,
          prompt: async () => {},
          abort: async () => {},
        } as unknown as AgentSession,
        supportsImages: true,
        async dispose() {
          if (index === 0) await parentCleanup;
          disposed += 1;
        },
      };
    },
  });
  const apiKey = "TEST_ONLY_byo-memory-only-test-value";
  try {
    const parent = await service.create({
      userId: "local-user",
      jobId: "byo-job",
      contextMode: "figure",
      workspaceId: "byo-workspace",
      provider: "openai",
      modelId: "gpt-test",
      authMode: "api_key",
      apiKey,
      inputs: [{ artifactId: figure.id, kind: "figure" }],
    });
    await service.startExtraction("local-user", parent.id);
    const output = path.join(root, "workspaces", parent.id, "output");
    mkdirSync(path.join(output, "perception"), { recursive: true });
    copyFileSync(
      path.resolve(
        process.cwd(),
        "test",
        "fixtures",
        "figure",
        "demo-figure",
        "pred.v2.json",
      ),
      path.join(output, "result.v2.json"),
    );
    copyFileSync(
      path.resolve(
        process.cwd(),
        "test",
        "fixtures",
        "figure",
        "demo-figure",
        "molparser.json",
      ),
      path.join(output, "perception", "molparser.json"),
    );
    eventSinks[0]?.({
      type: "tool_execution_end",
      toolName: "detect",
      isError: false,
      result: {
        content: [{
          type: "text",
          text: JSON.stringify({
            raw_molparser_json: "output/perception/molparser.json",
            molparser_provenance: { source: "cache_hit", cache_key: figure.sha256 },
          }),
        }],
      },
    });
    addRawMolParserLineage(
      path.join(output, "result.v2.json"),
      path.join(output, "perception", "molparser.json"),
    );
    eventSinks[0]?.({
      type: "tool_execution_end",
      toolName: "assemble",
      isError: false,
      result: {
        content: [{
          type: "text",
          text: JSON.stringify({ v2_path: "output/result.v2.json" }),
        }],
      },
    });
    writeFileSync(path.join(output, "reactions.html"), "<!doctype html><title>reaction</title>");
    eventSinks[0]?.({
      type: "tool_execution_end", toolName: "render_reactions", isError: false,
      result: { content: [{ type: "text", text: JSON.stringify({ html_path: "output/reactions.html" }) }] },
    });
    eventSinks[0]?.({ type: "agent_end" });
    for (let attempt = 0; attempt < 100
      && !["review_ready", "partial"].includes(
        store.getSession("local-user", parent.id).status,
      ); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    assert.match(store.getSession("local-user", parent.id).status, /review_ready|partial/);
    const fingerprints = store.listEvents("local-user", parent.id)
      .filter((event) => (event.data as { phase?: string }).phase === "protocol.fingerprint")
      .map((event) => (event.data as {
        fingerprint: { stage: string; model: { thinkingLevel: string } };
      }).fingerprint);
    assert.deepEqual(fingerprints.map((item) => item.stage), ["dispatch", "sealed"]);
    assert.deepEqual(fingerprints.map((item) => item.model.thinkingLevel), ["medium", "medium"]);
    assert.equal(disposed, 0, "parent cleanup remains deliberately in flight");
    assert.equal(terminated, 0, "parent MCP cleanup remains deliberately in flight");
    const concurrent = await Promise.allSettled([
      service.continueFrom(
        "local-user",
        parent.id,
        "Inspect the sealed result without mutating it.",
      ),
      service.continueFrom(
        "local-user",
        parent.id,
        "Attempt a concurrent continuation during cleanup.",
      ),
    ]);
    const fulfilled = concurrent.filter(
      (result): result is PromiseFulfilledResult<Awaited<ReturnType<typeof service.continueFrom>>> =>
        result.status === "fulfilled",
    );
    const rejected = concurrent.filter(
      (result): result is PromiseRejectedResult => result.status === "rejected",
    );
    assert.equal(fulfilled.length, 1, "the sealed BYO credential is single-consumer");
    assert.equal(rejected.length, 1, "a concurrent continuation must re-enter credentials");
    assert.ok(
      rejected[0]?.reason instanceof RuntimePolicyError
        && rejected[0].reason.code === "SESSION_NOT_ACTIVE",
    );
    const child = fulfilled[0]!.value;
    assert.equal(child.id, "byo-child");
    assert.equal(child.status, "running");
    assert.equal(JSON.stringify(store.listEvents("local-user", parent.id)).includes(apiKey), false);
    assert.equal(JSON.stringify(store.listEvents("local-user", child.id)).includes(apiKey), false);
    releaseParentCleanup();
    await service.waitForTerminalSettlement(parent.id);
    assert.equal(disposed, 1);
    assert.equal(terminated, 1);
    const childOutput = path.join(root, "workspaces", child.id, "output");
    mkdirSync(path.join(childOutput, "perception"), { recursive: true });
    copyFileSync(
      path.resolve(
        process.cwd(),
        "test",
        "fixtures",
        "figure",
        "demo-figure",
        "pred.v2.json",
      ),
      path.join(childOutput, "result.v2.json"),
    );
    copyFileSync(
      path.resolve(
        process.cwd(),
        "test",
        "fixtures",
        "figure",
        "demo-figure",
        "molparser.json",
      ),
      path.join(childOutput, "perception", "molparser.json"),
    );
    eventSinks[1]?.({
      type: "tool_execution_end",
      toolName: "detect",
      isError: false,
      result: {
        content: [{
          type: "text",
          text: JSON.stringify({
            raw_molparser_json: "output/perception/molparser.json",
            molparser_provenance: { source: "cache_hit", cache_key: figure.sha256 },
          }),
        }],
      },
    });
    addRawMolParserLineage(
      path.join(childOutput, "result.v2.json"),
      path.join(childOutput, "perception", "molparser.json"),
    );
    eventSinks[1]?.({
      type: "tool_execution_end",
      toolName: "assemble",
      isError: false,
      result: {
        content: [{
          type: "text",
          text: JSON.stringify({ v2_path: "output/result.v2.json" }),
        }],
      },
    });
    writeFileSync(path.join(childOutput, "reactions.html"), "<!doctype html><title>reaction</title>");
    eventSinks[1]?.({
      type: "tool_execution_end", toolName: "render_reactions", isError: false,
      result: { content: [{ type: "text", text: JSON.stringify({ html_path: "output/reactions.html" }) }] },
    });
    eventSinks[1]?.({ type: "agent_end" });
    for (let attempt = 0; attempt < 100
      && !["review_ready", "partial"].includes(
        store.getSession("local-user", child.id).status,
      ); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    const leases = (service as unknown as {
      sealedApiKeyLeases: Map<string, unknown>;
    }).sealedApiKeyLeases;
    assert.equal(leases.size, 1);
    await new Promise((resolve) => setTimeout(resolve, 250));
    assert.equal(leases.size, 0);
  } finally {
    releaseParentCleanup();
    await service.dispose("byo-parent");
    await service.dispose("byo-child");
    rmSync(root, { recursive: true, force: true });
  }
});

test("sealed Figure result continues in a linked task without mutating its parent", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-local-continuation-"));
  const store = new InMemoryRuntimeStore();
  const inputStore = new LocalInputStore(path.join(root, "inputs"));
  const png = readFileSync(
    path.resolve(process.cwd(), "..", "..", "runtime", "pi-service", "test", "fixtures", "figure", "demo-figure", "image.png"),
  );
  const artifact = await inputStore.stage(
    "local-user",
    (async function* () { yield png; })(),
    { kind: "figure", originalName: "figure.png", mediaType: "image/png" },
  );
  const ids = ["parent-session", "linked-session", "retry-session"];
  const prompts: string[] = [];
  const eventSinks: Array<(event: unknown) => void> = [];
  const toolSets: ToolDefinition[][] = [];
  const service = new LocalPiAgentService(store, {
    repoRoot: REPO,
    workspaceRoot: path.join(root, "workspaces"),
    agentRoot: path.join(root, "agents"),
    inputStore,
    python: "python",
  }, {
    randomId: () => ids.shift()!,
    loadSkills: async () => [],
    startMcp: async () => ({
      dispose() {},
      async terminate() {},
    }) as unknown as ChemMcpClient,
    createTools: async () => [],
    createPi: async (input) => {
      eventSinks.push(input.onEvent);
      toolSets.push(input.customTools);
      return ({
      session: {
        isStreaming: false,
        prompt: async (text: string) => { prompts.push(text); },
        abort: async () => {},
      } as unknown as AgentSession,
      supportsImages: true,
      async dispose() {},
      });
    },
  });
  try {
    const parent = await service.create({
      userId: "local-user",
      jobId: "figure-job",
      task: "extract_all",
      contextMode: "figure",
      workspaceId: "figure-workspace",
      provider: "openai-codex",
      modelId: "gpt-test",
      authMode: "pi_auth",
      inputs: [{ artifactId: artifact.id, kind: "figure" }],
    });
    parent.status = "review_ready";
    store.putSession(parent);
    const result = {
      schemaVersion: "chemrxn.runtime-result.v1" as const,
      sessionId: parent.id,
      jobId: parent.jobId,
      contextMode: "figure" as const,
      source: {
        mode: "live_pi_sdk" as const,
        reconstructed: false as const,
        liveEventCapture: true as const,
        inputArtifactIds: [artifact.id],
        workspaceId: parent.id,
      },
      domainContract: "chemrxn.figure-job.v1" as const,
      terminalStatus: "review_ready" as const,
      manifest: { sealed: true },
      createdAt: "2000-01-01T00:00:00.000Z",
      gtIncluded: false as const,
    };
    store.putResult("local-user", parent.id, result);
    const parentBefore = JSON.stringify(store.getResult("local-user", parent.id));
    const linked = await service.continueFrom(
      "local-user",
      parent.id,
      "Inspect the product stereochemistry only.",
    );
    assert.equal(linked.id, "linked-session");
    assert.equal(linked.task, "inspect_selection");
    assert.equal(linked.parentSessionId, parent.id);
    assert.equal(
      linked.parentResultSha256,
      createHash("sha256").update(parentBefore).digest("hex"),
    );
    assert.equal(linked.status, "running");
    assert.equal(JSON.stringify(store.getResult("local-user", parent.id)), parentBefore);
    assert.match(prompts.at(-1) ?? "", /Inspect the product stereochemistry only/);
    const proposalTool = toolSets[1]?.find(
      (tool) => tool.name === "propose_chemistry_correction",
    );
    assert.ok(proposalTool);
    await proposalTool.execute("linked-proposal", {
      reactionId: "example:0",
      rationale: "The linked inspection found a stereochemical mismatch.",
      evidence: ["source bbox 3"],
      changes: [{
        kind: "molecule",
        side: "products",
        index: 0,
        field: "E-SMILES",
        value: "C[C@@H](O)Cl",
      }],
    }, undefined, () => {}, {} as never);
    const proposalEvent = store.listEvents("local-user", linked.id).find((event) =>
      (event.data as { phase?: string })?.phase === "workspace.command");
    assert.equal(
      ((proposalEvent?.data as {
        workspaceCommand?: { jobId?: string };
      })?.workspaceCommand?.jobId),
      runtimeReviewerJobId("figure", parent.id),
    );
    const linkedCreated = store.listEvents("local-user", linked.id)[0];
    assert.equal(
      (linkedCreated?.data as {
        continuation?: { parentSessionId?: string; sealedParent?: boolean };
      }).continuation?.parentSessionId,
      parent.id,
    );
    assert.equal(
      (linkedCreated?.data as {
        continuation?: { sealedParent?: boolean };
      }).continuation?.sealedParent,
      true,
    );
    assert.equal(
      (linkedCreated?.data as { workspaceTargetSessionId?: string }).workspaceTargetSessionId,
      parent.id,
    );
    eventSinks[1]?.({ type: "agent_end" });
    await assert.rejects(
      service.prompt("local-user", linked.id, "Race the result finalizer"),
      (error: unknown) =>
        error instanceof RuntimePolicyError
        && error.code === "AGENT_BUSY"
        && /sealing immutable result/.test(error.message),
    );
    for (let attempt = 0; attempt < 20
      && store.getSession("local-user", linked.id).status !== "ready"; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    assert.equal(store.getSession("local-user", linked.id).status, "ready");
    await service.abort("local-user", linked.id);
    assert.equal(store.getSession("local-user", linked.id).status, "cancelled");
    assert.equal(store.listEvents("local-user", linked.id).at(-1)?.type, "job.cancelled");

    const retried = await service.retry("local-user", linked.id);
    assert.equal(retried.id, "retry-session");
    assert.equal(retried.status, "running");
    assert.equal(retried.jobId, linked.jobId);
    assert.equal(retried.task, linked.task);
    assert.equal(retried.parentSessionId, linked.id);
    const retryCreated = store.listEvents("local-user", retried.id)[0];
    assert.equal(
      (retryCreated?.data as {
        retry?: { parentSessionId?: string; sealedParent?: boolean };
      }).retry?.parentSessionId,
      linked.id,
    );
    assert.equal(
      (retryCreated?.data as { continuation?: unknown }).continuation,
      undefined,
    );
    assert.deepEqual(
      (retryCreated?.data as { inputArtifactIds?: string[] }).inputArtifactIds,
      [artifact.id],
    );
    assert.equal(
      existsSync(path.join(root, "workspaces", retried.id, "output", "result.v2.json")),
      false,
    );
    assert.equal(
      existsSync(path.join(root, "workspaces", retried.id, "output", "pdf_view.json")),
      false,
    );
  } finally {
    await service.dispose("parent-session");
    await service.dispose("linked-session");
    await service.dispose("retry-session");
    rmSync(root, { recursive: true, force: true });
  }
});

test("local Pi service records subscription auth without persisting auth paths or secrets", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-local-pi-auth-"));
  const store = new InMemoryRuntimeStore();
  const authPath = path.join(root, "private", "auth.json");
  let piInput: Parameters<typeof createPiSession>[0] | undefined;
  let onEvent: ((event: unknown) => void) | undefined;
  let piDisposed = false;
  let mcpTerminated = false;
  const mcp = {
    dispose() {},
    async terminate() { mcpTerminated = true; },
  } as unknown as ChemMcpClient;
  const pi: PiSessionHandle = {
    session: {
      isStreaming: false,
      prompt: async () => {},
    } as unknown as AgentSession,
    async dispose() { piDisposed = true; },
  };
  const service = new LocalPiAgentService(store, {
    repoRoot: REPO,
    workspaceRoot: path.join(root, "workspaces"),
    agentRoot: path.join(root, "agents"),
    authPath,
    python: "python",
  }, {
    randomId: () => "pi-auth-session",
    loadSkills: async () => [],
    startMcp: async () => mcp,
    createTools: async () => [],
    createPi: async (input) => {
      piInput = input;
      onEvent = input.onEvent;
      return pi;
    },
  });
  try {
    const created = await service.create({
      userId: "local-user",
      jobId: "job-pi-auth",
      contextMode: "figure",
      workspaceId: "demo-figure",
      provider: "openai-codex",
      modelId: "gpt-5.5",
      authMode: "pi_auth",
    });
    assert.equal(created.authMode, "personal_codex_subscription");
    assert.equal(piInput?.authMode, "pi_auth");
    assert.equal(piInput?.authPath, authPath);
    assert.ok(piInput?.modelsPath?.startsWith(path.join(root, "agents")));
    assert.equal(piInput?.modelsPath?.endsWith("models.json"), true);
    assert.equal(piInput?.apiKey, undefined);
    const persisted = JSON.stringify(store.listEvents("local-user", created.id));
    assert.equal(persisted.includes(authPath), false);
    assert.match(persisted, /pi_auth/);
    await service.prompt("local-user", created.id, "A successful conversation-only turn");
    onEvent?.({
      type: "message_end",
      message: { role: "assistant", stopReason: "stop", content: [{ type: "text", text: "Done" }] },
    });
    onEvent?.({ type: "agent_end" });
    assert.equal(store.getSession("local-user", created.id).status, "ready");
    assert.equal(store.listEvents("local-user", created.id).at(-1)?.type, "session.ready");
    assert.equal(
      (store.listEvents("local-user", created.id).at(-1)?.data as { reason?: string }).reason,
      "turn_complete",
    );
    await service.prompt("local-user", created.id, "A provider-error turn");
    onEvent?.({
      type: "message_end",
      message: { role: "assistant", stopReason: "error", errorMessage: "fetch failed", content: [] },
    });
    onEvent?.({ type: "agent_end" });
    assert.equal(store.getSession("local-user", created.id).status, "running");
    onEvent?.({ type: "agent_settled" });
    for (let attempt = 0; attempt < 50 && !piDisposed; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    assert.equal(store.getSession("local-user", created.id).status, "failed");
    assert.equal(store.getSession("local-user", created.id).blockReason, "MODEL_REQUEST_FAILED");
    assert.equal(store.listEvents("local-user", created.id).at(-1)?.type, "agent.error");
    assert.equal(piDisposed, true);
    assert.equal(mcpTerminated, true);
  } finally {
    await service.dispose("pi-auth-session");
    rmSync(root, { recursive: true, force: true });
  }
});

test("fatal MCP failure terminalizes the session and unloads its Pi credential", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-local-agent-mcp-fail-"));
  const store = new InMemoryRuntimeStore();
  let fatal: ((error: Error) => void) | undefined;
  let piDisposed = false;
  let mcpTerminated = false;
  const mcp = {
    dispose() {},
    async terminate() { mcpTerminated = true; },
  } as unknown as ChemMcpClient;
  const pi: PiSessionHandle = {
    session: {
      isStreaming: false,
    } as unknown as AgentSession,
    async dispose() { piDisposed = true; },
  };
  const service = new LocalPiAgentService(store, {
    repoRoot: REPO,
    workspaceRoot: path.join(root, "workspaces"),
    agentRoot: path.join(root, "agents"),
    python: "python",
  }, {
    randomId: () => "mcp-failed-session",
    loadSkills: async () => [],
    startMcp: async (options) => {
      fatal = options.onFatal;
      return mcp;
    },
    createTools: async () => [],
    createPi: async () => pi,
  });
  try {
    await service.create({
      userId: "local-user",
      jobId: "job-mcp-fail",
      contextMode: "figure",
      workspaceId: "demo-figure",
      provider: "openai",
      modelId: "gpt-test",
      apiKey: "TEST_ONLY_long-enough-test-key",
    });
    fatal?.(new Error("chem-extract MCP request timed out: tools/call"));
    for (let attempt = 0; attempt < 50 && !piDisposed; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    assert.equal(store.getSession("local-user", "mcp-failed-session").status, "failed");
    assert.equal(store.listEvents("local-user", "mcp-failed-session").at(-1)?.type, "agent.error");
    assert.equal(piDisposed, true);
    assert.equal(mcpTerminated, true);
    await assert.rejects(
      service.prompt("local-user", "mcp-failed-session", "Try again"),
      (error: unknown) =>
        error instanceof RuntimePolicyError && error.code === "SESSION_NOT_ACTIVE",
    );
  } finally {
    await service.dispose("mcp-failed-session");
    rmSync(root, { recursive: true, force: true });
  }
});

test("MCP failure during Pi construction cannot publish a ready session", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-local-agent-init-race-"));
  const store = new InMemoryRuntimeStore();
  let fatal: ((error: Error) => void) | undefined;
  let releasePi: (() => void) | undefined;
  let createPiStarted: (() => void) | undefined;
  const piStarted = new Promise<void>((resolve) => { createPiStarted = resolve; });
  const piRelease = new Promise<void>((resolve) => { releasePi = resolve; });
  let piDisposed = false;
  let mcpTerminated = false;
  const mcp = {
    dispose() {},
    async terminate() { mcpTerminated = true; },
  } as unknown as ChemMcpClient;
  const pi: PiSessionHandle = {
    session: { isStreaming: false } as unknown as AgentSession,
    async dispose() { piDisposed = true; },
  };
  const service = new LocalPiAgentService(store, {
    repoRoot: REPO,
    workspaceRoot: path.join(root, "workspaces"),
    agentRoot: path.join(root, "agents"),
    python: "python",
  }, {
    randomId: () => "init-race-session",
    loadSkills: async () => [],
    startMcp: async (options) => {
      fatal = options.onFatal;
      return mcp;
    },
    createTools: async () => [],
    createPi: async () => {
      createPiStarted?.();
      await piRelease;
      return pi;
    },
  });
  try {
    const creating = service.create({
      userId: "local-user",
      jobId: "job-init-race",
      contextMode: "figure",
      workspaceId: "demo-figure",
      provider: "openai",
      modelId: "gpt-test",
      apiKey: "TEST_ONLY_long-enough-test-key",
    });
    await piStarted;
    fatal?.(new Error("chem-extract MCP exited during Pi construction"));
    releasePi?.();
    await assert.rejects(creating, /exited during Pi construction/);
    assert.equal(store.getSession("local-user", "init-race-session").status, "failed");
    assert.equal(
      store.listEvents("local-user", "init-race-session")
        .some((event) => event.type === "session.ready"),
      false,
    );
    assert.equal(piDisposed, true);
    assert.equal(mcpTerminated, true);
  } finally {
    await service.dispose("init-race-session");
    rmSync(root, { recursive: true, force: true });
  }
});

test("workspace setup failures transition a persisted creating session to failed", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-local-agent-fail-"));
  const blockedRoot = path.join(root, "not-a-directory");
  writeFileSync(blockedRoot, "file");
  const store = new InMemoryRuntimeStore();
  const service = new LocalPiAgentService(store, {
    repoRoot: REPO,
    workspaceRoot: blockedRoot,
    agentRoot: path.join(root, "agents"),
    python: "python",
  }, { randomId: () => "failed-create" });
  try {
    await assert.rejects(service.create({
      userId: "local-user",
      jobId: "job-failed",
      contextMode: "figure",
      workspaceId: "demo-figure",
      provider: "openai",
      modelId: "gpt-test",
      apiKey: "TEST_ONLY_long-enough-test-key",
    }));
    assert.equal(store.getSession("local-user", "failed-create").status, "failed");
    assert.equal(store.listEvents("local-user", "failed-create").at(-1)?.type, "agent.error");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("Pi tool projection exposes only strict observable chemistry inputs", () => {
  const page = projectPiEvent({
    type: "tool_execution_start",
    toolCallId: "page-1",
    toolName: "detect",
    args: { image_path: "input/figure.png", api_key: "must-not-persist" },
  });
  assert.deepEqual(page, {
    phase: "pi.tool_execution_start",
    event: {
      type: "tool_execution_start",
      toolCallId: "page-1",
      toolName: "detect",
      argumentsRedacted: true,
      observableInputs: { image_path: "input/figure.png" },
    },
  });
  const fhr = projectPiEvent({
    type: "tool_execution_start",
    toolCallId: "fhr-1",
    toolName: "reverse_rgroup",
    args: { template: "C[*:1]", concrete: "CC", credential: "must-not-persist" },
  });
  assert.deepEqual((fhr.event as Record<string, unknown>).observableInputs, {
    template: "C[*:1]",
    concrete: "CC",
  });
  assert.doesNotMatch(JSON.stringify([page, fhr]), /must-not-persist|api_key|credential/);
  const unknown = projectPiEvent({
    type: "tool_execution_start", toolCallId: "x", toolName: "unknown", args: { secret: "no" },
  });
  assert.equal("observableInputs" in (unknown.event as Record<string, unknown>), false);
});

test("extraction preparation failure disposes activated resources and drops the key lease", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-local-agent-start-fail-"));
  const store = new InMemoryRuntimeStore();
  const inputStore = new LocalInputStore(path.join(root, "inputs"));
  const figure = await inputStore.stage(
    "local-user",
    (async function* () {
      yield readFileSync(
        path.resolve(process.cwd(), "..", "..", "runtime", "pi-service", "test", "fixtures", "figure", "demo-figure", "image.png"),
      );
    })(),
    { kind: "figure", originalName: "figure.png", mediaType: "image/png" },
  );
  let mcpTerminated = 0;
  let piDisposed = 0;
  let promptCalls = 0;
  const service = new LocalPiAgentService(store, {
    repoRoot: REPO,
    workspaceRoot: path.join(root, "workspaces"),
    agentRoot: path.join(root, "agents"),
    inputStore,
    python: "python",
  }, {
    randomId: () => "start-failure-session",
    loadSkills: async () => [],
    startMcp: async () => ({
      dispose() {},
      async terminate() { mcpTerminated += 1; },
    }) as unknown as ChemMcpClient,
    createTools: async () => [],
    createPi: async () => ({
      session: {
        isStreaming: false,
        prompt: async () => { promptCalls += 1; },
        abort: async () => {},
      } as unknown as AgentSession,
      supportsImages: false,
      async dispose() { piDisposed += 1; },
    }),
  });
  const apiKey = "TEST_ONLY_start-failure-memory-only";
  try {
    const prepared = await service.prepare({
      userId: "local-user",
      jobId: "job-start-failure",
      contextMode: "figure",
      workspaceId: "start-failure",
      provider: "openai",
      modelId: "text-only-test",
      authMode: "api_key",
      apiKey,
      dispatchRequested: true,
      inputs: [{ artifactId: figure.id, kind: "figure" }],
    });
    await service.activatePrepared("local-user", prepared.id, apiKey);
    await assert.rejects(
      service.startExtraction("local-user", prepared.id),
      (error: unknown) => error instanceof RuntimePolicyError
        && error.code === "MODEL_HAS_NO_VISION",
    );
    assert.equal(store.getSession("local-user", prepared.id).status, "failed");
    assert.equal(store.getSession("local-user", prepared.id).blockReason, "MODEL_HAS_NO_VISION");
    assert.equal(mcpTerminated, 1);
    assert.equal(piDisposed, 1);
    assert.equal(promptCalls, 0);
    assert.equal(JSON.stringify(store.listEvents("local-user", prepared.id)).includes(apiKey), false);
    await assert.rejects(
      service.retry("local-user", prepared.id),
      (error: unknown) => error instanceof RuntimePolicyError
        && error.code === "SESSION_NOT_ACTIVE"
        && /Supply it again/.test(error.message),
    );
  } finally {
    await service.dispose("start-failure-session");
    rmSync(root, { recursive: true, force: true });
  }
});

test("activation cancellation interrupts deferred tool discovery even when cleanup rejects", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-local-agent-activation-cancel-"));
  const store = new InMemoryRuntimeStore();
  let toolsStarted!: () => void;
  let releaseTools!: () => void;
  const started = new Promise<void>((resolve) => { toolsStarted = resolve; });
  const toolGate = new Promise<void>((resolve) => { releaseTools = resolve; });
  let terminateCalls = 0;
  let piCalls = 0;
  const service = new LocalPiAgentService(store, {
    repoRoot: REPO,
    workspaceRoot: path.join(root, "workspaces"),
    agentRoot: path.join(root, "agents"),
    authPath: path.join(root, "pi-auth.json"),
    python: "python",
  }, {
    randomId: () => "activation-cancel-session",
    loadSkills: async () => [],
    startMcp: async () => ({
      dispose() {},
      async terminate() {
        terminateCalls += 1;
        throw new Error("synthetic cleanup rejection");
      },
    }) as unknown as ChemMcpClient,
    createTools: async () => {
      toolsStarted();
      await toolGate;
      return [];
    },
    createPi: async () => {
      piCalls += 1;
      throw new Error("Pi must not start after cancellation");
    },
  });
  try {
    const prepared = await service.prepare({
      userId: "local-user",
      jobId: "job-activation-cancel",
      contextMode: "figure",
      workspaceId: "activation-cancel",
      provider: "openai-codex",
      modelId: "gpt-test",
      authMode: "pi_auth",
    });
    const controller = new AbortController();
    const activating = service.activatePrepared(
      "local-user",
      prepared.id,
      undefined,
      controller.signal,
    );
    await started;
    controller.abort("user_requested");
    await assert.rejects(activating, /user_requested/);
    assert.equal(store.getSession("local-user", prepared.id).status, "cancelled");
    assert.equal(store.listEvents("local-user", prepared.id).at(-1)?.type, "job.cancelled");
    assert.equal(terminateCalls, 1);
    assert.equal(piCalls, 0);
    releaseTools();
  } finally {
    releaseTools?.();
    await service.dispose("activation-cancel-session");
    rmSync(root, { recursive: true, force: true });
  }
});

test("activation cancellation waits for late MCP and Pi handles to be disposed", async () => {
  for (const phase of ["mcp", "pi"] as const) {
    const root = mkdtempSync(path.join(tmpdir(), `chemrxn-local-agent-late-${phase}-`));
    const store = new InMemoryRuntimeStore();
    let started!: () => void;
    let release!: () => void;
    const startSignal = new Promise<void>((resolve) => { started = resolve; });
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let terminated = 0;
    let disposed = 0;
    const mcp = {
      dispose() {},
      async terminate() { terminated += 1; },
    } as unknown as ChemMcpClient;
    const pi: PiSessionHandle = {
      session: { isStreaming: false } as unknown as AgentSession,
      supportsImages: true,
      async dispose() { disposed += 1; },
    };
    const service = new LocalPiAgentService(store, {
      repoRoot: REPO,
      workspaceRoot: path.join(root, "workspaces"),
      agentRoot: path.join(root, "agents"),
      python: "python",
    }, {
      randomId: () => `late-${phase}-session`,
      loadSkills: async () => [],
      startMcp: async () => {
        if (phase === "mcp") {
          started();
          await gate;
        }
        return mcp;
      },
      createTools: async () => [],
      createPi: async () => {
        if (phase === "pi") {
          started();
          await gate;
        }
        return pi;
      },
    });
    try {
      const prepared = await service.prepare({
        userId: "local-user",
        jobId: `job-late-${phase}`,
        contextMode: "figure",
        workspaceId: `late-${phase}`,
        provider: "openai-codex",
        modelId: "gpt-test",
        authMode: "pi_auth",
      });
      const controller = new AbortController();
      let settled = false;
      const activating = service.activatePrepared(
        "local-user",
        prepared.id,
        undefined,
        controller.signal,
      ).finally(() => { settled = true; });
      await startSignal;
      controller.abort("user_requested");
      await new Promise((resolve) => setTimeout(resolve, 30));
      assert.equal(settled, false, `${phase} activation released before late cleanup`);
      assert.equal(phase === "mcp" ? terminated : disposed, 0);
      release();
      await assert.rejects(activating, /user_requested/);
      assert.equal(store.getSession("local-user", prepared.id).status, "cancelled");
      assert.equal(terminated, 1);
      assert.equal(disposed, phase === "pi" ? 1 : 0);
    } finally {
      release?.();
      await service.disposeAll();
      rmSync(root, { recursive: true, force: true });
    }
  }
});

test("hung Pi abort is bounded for user cancellation and runtime shutdown", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-local-agent-bounded-abort-"));
  const store = new InMemoryRuntimeStore();
  const inputStore = new LocalInputStore(path.join(root, "inputs"));
  const figure = await inputStore.stage(
    "local-user",
    (async function* () {
      yield readFileSync(
        path.resolve(process.cwd(), "..", "..", "runtime", "pi-service", "test", "fixtures", "figure", "demo-figure", "image.png"),
      );
    })(),
    { kind: "figure", originalName: "figure.png", mediaType: "image/png" },
  );
  const ids = ["hung-user-abort", "hung-shutdown-abort"];
  let terminated = 0;
  let disposed = 0;
  const never = new Promise<void>(() => {});
  const service = new LocalPiAgentService(store, {
    repoRoot: REPO,
    workspaceRoot: path.join(root, "workspaces"),
    agentRoot: path.join(root, "agents"),
    inputStore,
    authPath: path.join(root, "auth.json"),
    python: "python",
    abortTimeoutMs: 20,
    cleanupTimeoutMs: 20,
  }, {
    randomId: () => ids.shift()!,
    loadSkills: async () => [],
    startMcp: async () => ({
      dispose() {},
      async terminate() { terminated += 1; },
    }) as unknown as ChemMcpClient,
    createTools: async () => [],
    createPi: async () => ({
      session: {
        isStreaming: false,
        prompt: async () => {},
        abort: async () => never,
      } as unknown as AgentSession,
      supportsImages: true,
      async dispose() { disposed += 1; },
    }),
  });
  const createRunning = async (): Promise<string> => {
    const created = await service.create({
      userId: "local-user",
      jobId: `job-${ids.length}`,
      contextMode: "figure",
      workspaceId: "bounded-abort",
      provider: "openai-codex",
      modelId: "gpt-test",
      authMode: "pi_auth",
      inputs: [{ artifactId: figure.id, kind: "figure" }],
    });
    await service.startExtraction("local-user", created.id);
    return created.id;
  };
  try {
    const userSession = await createRunning();
    const userStarted = Date.now();
    await service.abort("local-user", userSession);
    assert.ok(Date.now() - userStarted < 500);
    assert.equal(store.getSession("local-user", userSession).status, "cancelled");
    const cancelled = store.listEvents("local-user", userSession).at(-1)!;
    assert.equal(cancelled.type, "job.cancelled");
    assert.equal((cancelled.data as { forced?: boolean }).forced, true);

    const shutdownSession = await createRunning();
    const shutdownStarted = Date.now();
    await service.interruptForShutdown("local-user", shutdownSession);
    assert.ok(Date.now() - shutdownStarted < 500);
    assert.equal(store.getSession("local-user", shutdownSession).status, "interrupted");
    const interrupted = store.listEvents("local-user", shutdownSession).at(-1)!;
    assert.equal(interrupted.type, "job.interrupted");
    assert.equal((interrupted.data as { forced?: boolean }).forced, true);
    assert.equal(terminated, 2);
    assert.equal(disposed, 2);
  } finally {
    await service.disposeAll();
    rmSync(root, { recursive: true, force: true });
  }
});

test("prepared local extraction stays cheap and secret-free until explicit activation", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-local-agent-prepare-"));
  const store = new InMemoryRuntimeStore();
  let loadSkillCalls = 0;
  let startMcpCalls = 0;
  let createToolCalls = 0;
  let createPiCalls = 0;
  let activatedApiKey: string | undefined;
  const apiKey = "TEST_ONLY_prepared-memory-only-key";
  const workspaceRoot = path.join(root, "workspaces");
  const inputStore = new LocalInputStore(path.join(root, "inputs"));
  const figure = await inputStore.stage(
    "local-user",
    (async function* () {
      yield readFileSync(
        path.resolve(process.cwd(), "..", "..", "runtime", "pi-service", "test", "fixtures", "figure", "demo-figure", "image.png"),
      );
    })(),
    { kind: "figure", originalName: "figure.png", mediaType: "image/png" },
  );
  const mcp = {
    dispose() {},
    async terminate() {},
  } as unknown as ChemMcpClient;
  const pi: PiSessionHandle = {
    session: {
      isStreaming: false,
      prompt: async () => {},
      abort: async () => {},
    } as unknown as AgentSession,
    async dispose() {},
  };
  const service = new LocalPiAgentService(store, {
    repoRoot: REPO,
    workspaceRoot,
    agentRoot: path.join(root, "agents"),
    inputStore,
    python: "python",
  }, {
    randomId: () => "prepared-session",
    loadSkills: async () => {
      loadSkillCalls += 1;
      return [];
    },
    startMcp: async () => {
      startMcpCalls += 1;
      return mcp;
    },
    createTools: async () => {
      createToolCalls += 1;
      return [];
    },
    createPi: async (input) => {
      createPiCalls += 1;
      activatedApiKey = input.apiKey;
      return pi;
    },
  });
  try {
    const prepared = await service.prepare({
      userId: "local-user",
      jobId: "job-prepared",
      contextMode: "figure",
      workspaceId: "requested-workspace",
      provider: "openai",
      modelId: "gpt-test",
      authMode: "api_key",
      apiKey,
      dispatchRequested: true,
      initialMessage: "Extract only the selected chemistry figure.",
      inputs: [{ artifactId: figure.id, kind: "figure" }],
    });
    assert.equal(prepared.id, "prepared-session");
    assert.equal(prepared.status, "creating");
    assert.equal(existsSync(path.join(workspaceRoot, prepared.id)), false);
    assert.deepEqual(
      { loadSkillCalls, startMcpCalls, createToolCalls, createPiCalls },
      { loadSkillCalls: 0, startMcpCalls: 0, createToolCalls: 0, createPiCalls: 0 },
    );
    const preparedEvents = store.listEvents("local-user", prepared.id);
    assert.equal(preparedEvents.length, 1);
    assert.equal(preparedEvents[0]?.type, "session.created");
    assert.equal(JSON.stringify(preparedEvents).includes(apiKey), false);
    assert.deepEqual(
      (preparedEvents[0]?.data as {
        localExtractionRequest: { dispatchRequested: boolean; initialMessage: string };
      }).localExtractionRequest,
      {
        schemaVersion: "chemrxn.local-extraction-request.v1",
        dispatchRequested: true,
        contextMode: "figure",
        task: "extract_all",
        provider: "openai",
        modelId: "gpt-test",
        thinkingLevel: "medium",
        authMode: "api_key",
        requestedWorkspaceId: "requested-workspace",
        inputs: [{ artifactId: figure.id, kind: "figure" }],
        initialMessage: "Extract only the selected chemistry figure.",
      },
    );

    const activated = await service.activatePrepared("local-user", prepared.id, apiKey);
    assert.equal(activated.id, prepared.id);
    assert.equal(activated.status, "ready");
    assert.equal(existsSync(path.join(workspaceRoot, prepared.id)), true);
    assert.deepEqual(
      { loadSkillCalls, startMcpCalls, createToolCalls, createPiCalls },
      { loadSkillCalls: 1, startMcpCalls: 1, createToolCalls: 1, createPiCalls: 1 },
    );
    assert.equal(activatedApiKey, apiKey);
    assert.equal(
      store.listEvents("local-user", prepared.id)
        .filter((event) => event.type === "session.created").length,
      1,
    );
    assert.equal(
      JSON.stringify(store.listEvents("local-user", prepared.id)).includes(apiKey),
      false,
    );

    await assert.rejects(
      service.prepare({
        userId: "local-user",
        jobId: "job-ground-truth-direction",
        contextMode: "figure",
        workspaceId: "forbidden",
        provider: "openai",
        modelId: "gpt-test",
        authMode: "api_key",
        apiKey,
        initialMessage: "Read GT_SENTINEL before extracting.",
      }),
      (error: unknown) =>
        error instanceof RuntimePolicyError && error.code === "INVALID_CONTEXT",
    );
    assert.equal(store.listSessions("local-user").length, 1);
  } finally {
    await service.dispose("prepared-session");
    rmSync(root, { recursive: true, force: true });
  }
});

test("direct local-agent create keeps SQLite CAS identity across prepare and activation", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-local-agent-sqlite-create-"));
  const store = new SqliteRuntimeStore(path.join(root, "state"));
  const mcp = {
    dispose() {},
    async terminate() {},
  } as unknown as ChemMcpClient;
  const pi: PiSessionHandle = {
    session: { isStreaming: false } as unknown as AgentSession,
    async dispose() {},
  };
  const service = new LocalPiAgentService(store, {
    repoRoot: REPO,
    workspaceRoot: path.join(root, "workspaces"),
    agentRoot: path.join(root, "agents"),
    python: "python",
  }, {
    randomId: () => "sqlite-direct-session",
    loadSkills: async () => [],
    startMcp: async () => mcp,
    createTools: async () => [],
    createPi: async () => pi,
  });
  try {
    const created = await service.create({
      userId: "local-user",
      jobId: "sqlite-direct-job",
      contextMode: "figure",
      workspaceId: "new-conversation",
      provider: "openai",
      modelId: "gpt-test",
      apiKey: "TEST_ONLY_sqlite-direct-memory-only",
    });
    assert.equal(created.status, "ready");
    assert.equal(store.getSession("local-user", created.id).status, "ready");
    assert.equal(
      store.listEvents("local-user", created.id)
        .filter((event) => event.type === "session.created").length,
      1,
    );
  } finally {
    await service.dispose("sqlite-direct-session");
    store.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("oversized Pi message events preserve replay boundaries with explicit truncation", () => {
  const projection = projectPiEvent({
    type: "message_end",
    message: {
      role: "assistant",
      content: [{ type: "text", text: "x".repeat(300_000) }],
    },
  });
  const compact = projection.event as {
    truncated: boolean;
    message: { role: string; content: Array<{ text: string }> };
  };
  assert.equal(compact.truncated, true);
  assert.equal(compact.message.role, "assistant");
  assert.match(compact.message.content[0]?.text ?? "", /message truncated/);
  assert.equal((compact.message.content[0]?.text.length ?? 0) < 70_000, true);
});

test("aggregate Pi control events never persist the repeated internal transcript", () => {
  const raw = {
    type: "agent_end",
    messages: [{
      role: "system",
      content: "Skill documentation mentions ground_truth but is not a mounted artifact.",
    }],
    toolResults: [{ content: "large aggregate copy" }],
  };
  const projection = projectPiEvent(raw);
  assert.deepEqual(projection, {
    phase: "pi.agent_end",
    event: {
      type: "agent_end",
      truncated: true,
      byteLength: Buffer.byteLength(JSON.stringify(raw)),
    },
  });
  assert.doesNotMatch(JSON.stringify(projection), /ground_truth|large aggregate copy/);
});
