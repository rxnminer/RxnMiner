import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { EventEmitter } from "node:events";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { test } from "node:test";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  main,
  parseArgs,
  resolveInstalledCommitIdentity,
  workspaceUrl,
} from "../src/main.mjs";

function fakeIo(overrides = {}) {
  const output = { stdout: "", stderr: "", calls: [] };
  const env = {
    CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
    CHEMRXN_VIEWER_DATA: path.join(tmpdir(), `chemrxn-cli-test-${process.pid}`),
    ...(overrides.env || {}),
  };
  return {
    output,
    io: {
      env,
      fetch: async () => ({ ok: true, json: async () => ({ ok: true, ui: { document_mode: true } }) }),
      spawn() {
        const child = new EventEmitter();
        child.unref = () => {};
        return child;
      },
      spawnSync(command, args) {
        output.calls.push([command, args]);
        if (args?.[0]?.endsWith(path.join("figure_skills", "scripts", "run_chem_mcp.py"))) {
          return {
            status: 0,
            stdout: [
              JSON.stringify({
                jsonrpc: "2.0",
                id: 1,
                result: { serverInfo: { name: "chem-extract", version: "0.1.0" } },
              }),
              JSON.stringify({
                jsonrpc: "2.0",
                id: 2,
                result: {
                  tools: [
                    "detect", "ocsr_detect", "assemble", "verify_smiles",
                    "render_reactions", "reverse_rgroup", "fill_rgroup", "reverse_assemble",
                  ].map((name) => ({ name })),
                },
              }),
              JSON.stringify({
                jsonrpc: "2.0",
                id: 3,
                result: {
                  content: [{
                    type: "text",
                    text: JSON.stringify({
                      valid: true,
                      canonical_iso: "C[C@H](O)Cl",
                    }),
                  }],
                },
              }),
            ].join("\n"),
            stderr: "",
          };
        }
        return { status: 0, stdout: "", stderr: "" };
      },
      async verifyInstallIntegrity() {
        return {
          ok: true,
          schemaVersion: "chemrxn.install-integrity.v1",
          manifest: "packaging/install-integrity.json",
          assetCount: 41,
          verifiedAssetCount: 41,
          lfsAssetCount: 36,
          sampleAssetCount: 5,
          requiredFileCount: 13,
          checks: [
            "private-package-contract",
            "packaged-runtime",
            "packaged-asset-sha256",
            "git-lfs-materialization",
            "bundled-source-samples",
          ].map((name) => ({ name, ok: true, required: true, detail: "verified" })),
          failures: [],
          assets: [],
        };
      },
      async installedCommitIdentity() {
        return {
          commit: "a".repeat(40),
          source: "npm hidden lockfile",
          immutable: true,
          cryptographicAuthentication: "not-assessed",
          recommendation: "Prefer a full commit SHA or protected signed tag.",
        };
      },
      stdout(value) {
        output.stdout += value;
      },
      stderr(value) {
        output.stderr += value;
      },
      ...overrides,
      env,
    },
  };
}

test("clone-first root launcher works without a global npm install", () => {
  const launcher = path.resolve(process.cwd(), "../..", "chemrxn");
  const result = spawnSync(process.execPath, [launcher, "--help"], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /ChemRxn local-first product launcher/);
  assert.match(result.stdout, /chemrxn open <IMAGE>/);
});

test("parseArgs rejects unknown options", () => {
  assert.throws(() => parseArgs(["open", "--pretend"]), /Unknown option/);
});

test("parseArgs reports unknown commands before their trailing arguments", () => {
  assert.throws(() => parseArgs(["foobar", "figure.png"]), /Unknown command: foobar/);
});

test("product command aliases resolve to one canonical lifecycle", () => {
  assert.equal(parseArgs(["jobs"]).command, "sessions");
  assert.equal(parseArgs(["ask", "session-a", "--message", "Inspect stereo"]).command, "chat");
  assert.equal(parseArgs(["cancel", "session-a"]).command, "abort");
  assert.equal(parseArgs(["auth", "status"]).command, "auth-status");
  assert.equal(parseArgs(["auth", "logout"]).command, "logout");
  assert.throws(() => parseArgs(["auth"]), /requires `status` or `logout`/);
});

test("parseArgs rejects invalid ports and missing option values", () => {
  assert.throws(() => parseArgs(["start", "--port", "0"]), /Invalid port/);
  assert.throws(() => parseArgs(["start", "--port"]), /requires a value/);
  assert.throws(() => parseArgs(["events"]), /requires a session ID/);
  assert.throws(() => parseArgs(["events", "session-a", "--after", "-1"]), /requires a value/);
  assert.throws(() => parseArgs(["events", "session-a", "--after", "1.5"]), /non-negative integer/);
  assert.throws(() => parseArgs(["status", "--runtime-session", "session-a"]), /only valid with open/);
  assert.throws(() => parseArgs(["demo"]), /requires a target/);
  assert.throws(() => parseArgs(["demo", "made-up"]), /Unknown sealed demo fixture/);
  assert.throws(() => parseArgs(["result"]), /requires a target/);
  assert.throws(() => parseArgs(["export"]), /requires a target/);
  assert.throws(() => parseArgs(["retry"]), /requires a target/);
  assert.throws(() => parseArgs(["agent"]), /requires a target/);
  assert.throws(() => parseArgs(["chat", "session-a"]), /requires --message/);
  assert.throws(() => parseArgs(["steer", "session-a"]), /requires --message/);
  assert.throws(() => parseArgs(["agent", "job", "--context", "unknown"]), /Invalid context/);
  assert.throws(() => parseArgs(["export", "job", "--format", "sdf"]), /Invalid export format/);
  assert.equal(parseArgs(["export", "job", "--format", "review"]).format, "review");
  assert.equal(parseArgs(["export", "job", "--format", "rights"]).format, "rights");
  assert.throws(() => parseArgs(["export", "job", "--format", "rxn", "--output", "-"]),
    /Binary RXN ZIP export/);
  assert.throws(() => parseArgs(["doctor", "--format", "v2"]), /only valid with export/);
  assert.throws(() => parseArgs(["status", "--thinking", "high"]), /only valid with agent, extract, or an input passed to open/);
  assert.throws(() => parseArgs(["agent", "job", "--thinking", "unbounded"]), /Invalid thinking level/);
});

test("Pi thinking is explicit, defaults to native CLI medium, and accepts an override", () => {
  assert.equal(parseArgs(["agent", "job"]).thinkingLevel, "medium");
  assert.equal(parseArgs(["extract", "figure.png", "--thinking", "high"]).thinkingLevel, "high");
  assert.equal(parseArgs(["open", "figure.png", "--thinking", "xhigh"]).thinkingLevel, "xhigh");
});

test("workspaceUrl deep-links to the exact encoded job", () => {
  assert.equal(
    workspaceUrl("paper 50/latest", 8787),
    "http://127.0.0.1:8787/?job=paper%2050%2Flatest",
  );
});

test("workspaceUrl carries the runtime session into the same Web workspace", () => {
  assert.equal(
    workspaceUrl("example-job", 8787, "session/a"),
    "http://127.0.0.1:8787/?job=example-job&runtime_session=session%2Fa",
  );
});
test("open accepts a hierarchical imported job ID instead of treating it as a file", async () => {
  const { io, output } = fakeIo();
  const code = await main(["open", "paper 50/latest", "--no-browser"], io);
  assert.equal(code, 0);
  assert.equal(output.stdout, "http://127.0.0.1:8787/?job=paper%2050%2Flatest\n");
});

test("open existing job prints exact local URL without a second renderer", async () => {
  const { io, output } = fakeIo();
  const code = await main(["open", "example-job-latest", "--no-browser"], io);
  assert.equal(code, 0);
  assert.equal(output.stdout, "http://127.0.0.1:8787/?job=example-job-latest\n");
  assert.equal(output.calls.length, 0);
});

test("open existing job ignores inherited extraction auth defaults", async () => {
  const { io, output } = fakeIo({
    env: {
      CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
      CHEMRXN_MODEL_PROVIDER: "anthropic",
      CHEMRXN_MODEL_ID: "claude-sonnet-4-5",
      CHEMRXN_AUTH_MODE: "pi_auth",
    },
  });
  const code = await main([
    "open", "example-job-latest", "--runtime-session", "session-a", "--no-browser",
  ], io);
  assert.equal(code, 0);
  assert.equal(
    output.stdout,
    "http://127.0.0.1:8787/?job=example-job-latest&runtime_session=session-a\n",
  );
});

test("open existing job rejects an explicit thinking level instead of silently ignoring it", async () => {
  const { io, output } = fakeIo();
  assert.equal(await main([
    "open", "example-job-latest", "--thinking", "high", "--no-browser",
  ], io), 2);
  assert.match(output.stderr, /Extraction options on `open` require an image input path/);
});

test("browser opener failure is reported without crashing the successful deep-link", async () => {
  const { io, output } = fakeIo({
    spawn() {
      const child = new EventEmitter();
      child.unref = () => {};
      queueMicrotask(() => child.emit("error", new Error("xdg-open missing")));
      return child;
    },
  });
  const code = await main(["open", "job-a"], io);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(code, 0);
  assert.match(output.stderr, /BROWSER_OPEN_FAILED/);
  assert.match(output.stderr, /open http:\/\/127\.0\.0\.1:8787\/\?job=job-a manually/);
});

test("open routes an input into extraction instead of presenting a fake URL", async () => {
  const { io, output } = fakeIo();
  const code = await main(["open", "figure.png", "--no-browser"], io);
  assert.equal(code, 2);
  assert.match(output.stderr, /INPUT_NOT_FOUND/);
  assert.doesNotMatch(output.stdout, /http:/);
});

test("open input rejects runtime-session before any model-backed side effect", async () => {
  const figure = path.resolve(process.cwd(), "../runtime/pi-service/test/fixtures/figure/demo-figure/image.png");
  let fetches = 0;
  const { io, output } = fakeIo({
    async fetch() {
      fetches += 1;
      throw new Error("must not be called");
    },
  });
  const code = await main([
    "open", figure, "--runtime-session", "existing-session", "--no-browser",
  ], io);
  assert.equal(code, 2);
  assert.match(output.stderr, /cannot be combined with an input path/);
  assert.equal(fetches, 0);
  assert.equal(output.calls.length, 0);
});

test("review imports a standalone Figure prediction into the Chem Reviewer", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-external-figure-review-"));
  const pred = path.join(root, "pred.v2.json");
  const image = path.join(root, "figure.png");
  const molparser = path.join(root, "molparser.json");
  writeFileSync(pred, "{}\n");
  writeFileSync(image, "png");
  writeFileSync(molparser, "[]\n");
  const { io, output } = fakeIo({
    env: {
      CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
      CHEMRXN_VIEWER_PY: process.execPath,
    },
  });
  try {
    const code = await main([
      "review", pred, "--image", image, "--perception", molparser,
      "--collection", "figure-run", "--no-browser",
    ], io);
    assert.equal(code, 0, output.stderr);
    const importerCall = output.calls.find(([, args]) => args?.[0]?.endsWith("import_figure_result.py"));
    assert.ok(importerCall);
    assert.deepEqual(importerCall[1].slice(1, 7), [
      "--image", image, "--pred", pred, "--molparser", molparser,
    ]);
    assert.match(output.stdout, /Imported into figure-run/);
    assert.match(output.stdout, /http:\/\/127\.0\.0\.1:8767\/static\//);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("review fails closed when a standalone prediction has no source", async () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-external-review-no-pdf-"));
  const pred = path.join(root, "pred.json");
  writeFileSync(pred, "{}\n");
  const { io, output } = fakeIo();
  try {
    assert.equal(await main(["review", pred, "--no-browser"], io), 2);
    assert.match(output.stderr, /requires --image FIGURE/i);
    assert.equal(output.calls.length, 0);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("open starts the existing local Web workspace when health is unavailable", async () => {
  const { io, output } = fakeIo({
    fetch: async () => {
      throw new Error("not running");
    },
  });
  const code = await main(["open", "job-a", "--no-browser"], io);
  assert.equal(code, 0);
  assert.equal(output.calls.length, 1);
  assert.equal(output.calls[0][0], "bash");
  assert.match(output.calls[0][1][0], /local_workbench\.sh$/);
  assert.equal(output.calls[0][1][1], "start");
});

test("non-open lifecycle commands reject positional arguments", () => {
  for (const command of ["doctor", "start", "status", "stop"]) {
    assert.throws(() => parseArgs([command, "figure.png"]), /does not accept a positional argument/);
  }
});

test("start delegates lifecycle and options to the supported local viewer", async () => {
  const { io, output } = fakeIo();
  const code = await main([
    "start",
    "--no-browser",
    "--data-root",
    "./test-data",
    "--port",
    "8877",
    "--reviewer-port",
    "8878",
    "--no-reviewer",
  ], io);
  assert.equal(code, 0);
  assert.equal(output.calls.length, 1);
  const [command, args] = output.calls[0];
  assert.equal(command, "bash");
  assert.match(args[0], /local_workbench\.sh$/);
  assert.deepEqual(args.slice(1), [
    "start",
    "--data-root",
    path.resolve("./test-data"),
    "--port",
    "8877",
    "--reviewer-port",
    "8878",
    "--runtime-port",
    "4310",
    "--no-reviewer",
  ]);
});

test("status and stop delegate to the same supported launcher", async () => {
  for (const command of ["status", "stop"]) {
    const { io, output } = fakeIo();
    assert.equal(await main([command], io), 0);
    assert.equal(output.calls[0][0], "bash");
    assert.equal(output.calls[0][1][1], command);
  }
});

test("sessions reads the authenticated structured runtime API", async () => {
  let request;
  const { io, output } = fakeIo({
    env: {
      CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
      CHEMRXN_PI_INTERNAL_TOKEN: "local-token",
      CHEMRXN_USER_ID: "user-a",
    },
    async fetch(url, options) {
      request = { url, options };
      return {
        ok: true,
        async json() {
          return {
            sessions: [{
              id: "session-a",
              status: "ready",
              task: "extract_all",
              jobId: "job-a",
              updatedAt: "2000-01-01T00:00:00.000Z",
            }],
          };
        },
      };
    },
  });
  assert.equal(await main(["sessions"], io), 0);
  assert.match(output.stdout, /session-a\tready\textract_all\tjob-a/);
  assert.equal(request.url, "http://127.0.0.1:4310/v1/sessions");
  assert.equal(request.options.headers.authorization, "Bearer local-token");
  assert.equal(request.options.headers["x-chemrxn-user-id"], "user-a");
});

test("resolved local data-root runtime credential wins over an unrelated inherited token", async () => {
  const dataRoot = mkdtempSync(path.join(tmpdir(), "chemrxn-runtime-token-"));
  mkdirSync(path.join(dataRoot, "run"), { recursive: true });
  writeFileSync(path.join(dataRoot, "run", "runtime.token"), "current-local-token\n", {
    mode: 0o600,
  });
  let authorization = "";
  const { io } = fakeIo({
    env: {
      CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
      CHEMRXN_VIEWER_DATA: dataRoot,
      CHEMRXN_PI_INTERNAL_TOKEN: "unrelated-runtime-token",
    },
    async fetch(_url, options) {
      authorization = options.headers.authorization;
      return { ok: true, json: async () => ({ sessions: [] }) };
    },
  });
  try {
    assert.equal(await main(["sessions"], io), 0);
    assert.equal(authorization, "Bearer current-local-token");
  } finally {
    rmSync(dataRoot, { recursive: true, force: true });
  }
});

test("events replays after a persisted cursor and supports JSON", async () => {
  let requestedUrl = "";
  const { io, output } = fakeIo({
    env: {
      CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
      CHEMRXN_PI_INTERNAL_TOKEN: "local-token",
      CHEMRXN_USER_ID: "user-a",
    },
    async fetch(url) {
      requestedUrl = url;
      return {
        ok: true,
        async json() {
          return {
            events: [{
              seq: 3,
              at: "2000-01-01T00:00:03.000Z",
              type: "agent.event",
              data: { phase: "perception.started" },
            }],
          };
        },
      };
    },
  });
  assert.equal(await main(["events", "session/a", "--after", "2", "--json"], io), 0);
  assert.equal(requestedUrl, "http://127.0.0.1:4310/v1/sessions/session%2Fa/events?after=2");
  assert.deepEqual(JSON.parse(output.stdout).events.map((event) => event.seq), [3]);
});

test("demo creates an explicitly reconstructed job and opens the shared workspace", async () => {
  const requests = [];
  const { io, output } = fakeIo({
    env: {
      CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
      CHEMRXN_PI_INTERNAL_TOKEN: "local-token",
      CHEMRXN_USER_ID: "local-user",
    },
    async fetch(url, options = {}) {
      requests.push({ url, options });
      if (url.endsWith("/healthz")) {
        return { ok: true, json: async () => ({ ok: true, ui: { document_mode: true } }) };
      }
      return {
        ok: true,
        async json() {
          return {
            session: { id: "session-demo", status: "review_ready" },
            replay: { reconstructed: true, liveExtraction: false },
          };
        },
      };
    },
  });
  assert.equal(
    await main(["demo", "demo-figure", "--no-browser"], io),
    0,
  );
  assert.match(output.stdout, /sealed replay: review_ready/);
  assert.match(output.stdout, /runtime_session=session-demo/);
  const create = requests.find((request) => request.url.endsWith("/v1/replay-jobs"));
  assert.equal(create.options.method, "POST");
  assert.deepEqual(JSON.parse(create.options.body), {
    userId: "local-user",
    fixture: "demo-figure",
  });
});

test("result and retry use persisted runtime endpoints", async () => {
  const requests = [];
  const { io, output } = fakeIo({
    env: {
      CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
      CHEMRXN_PI_INTERNAL_TOKEN: "local-token",
      CHEMRXN_USER_ID: "local-user",
    },
    async fetch(url, options = {}) {
      requests.push({ url, options });
      if (url.endsWith("/healthz")) {
        return { ok: true, json: async () => ({ ok: true, ui: { document_mode: true } }) };
      }
      if (url.endsWith("/result")) {
        return {
          ok: true,
          json: async () => ({
            sessionId: "old",
            terminalStatus: "cancelled",
            contextMode: "paper",
            domainContract: "chemrxn.pdf-replay.v1",
            source: { fixture: "pdf-example-job-cancelled", reconstructed: true },
          }),
        };
      }
      return {
        ok: true,
        json: async () => ({ session: { id: "new", status: "review_ready" } }),
      };
    },
  });
  assert.equal(await main(["result", "old"], io), 0);
  assert.match(output.stdout, /old\tcancelled\tpaper/);
  assert.equal(await main(["retry", "old", "--no-browser"], io), 0);
  assert.match(output.stdout, /runtime_session=new/);
  assert.equal(requests.some((request) => request.url.endsWith("/sessions/old/retry")
    && request.options.method === "POST"), true);
});

test("retry resumes a live attempt and supplies a BYO key only in the request body", async () => {
  const requests = [];
  const { io, output } = fakeIo({
    env: {
      CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
      CHEMRXN_PI_INTERNAL_TOKEN: "local-token",
      CHEMRXN_USER_ID: "local-user",
      CHEMRXN_MODEL_API_KEY: "sk-retry-memory-only",
    },
    async fetch(url, options = {}) {
      requests.push({ url, options });
      if (url.endsWith("/healthz")) {
        return { ok: true, json: async () => ({ ok: true, ui: { document_mode: true } }) };
      }
      if (url.endsWith("/sessions/cancelled-live") && (options.method || "GET") === "GET") {
        return {
          ok: true,
          json: async () => ({
            id: "cancelled-live",
            authMode: "api_key",
            status: "cancelled",
          }),
        };
      }
      return {
        ok: true,
        json: async () => ({
          retried: true,
          relation: "retry",
          previousSessionId: "cancelled-live",
          backend: { mode: "local_pi_sdk" },
          session: { id: "retry-live", status: "running" },
        }),
      };
    },
  });
  assert.equal(await main(["retry", "cancelled-live", "--no-browser"], io), 0);
  assert.match(output.stdout, /live retry: running/);
  assert.match(output.stdout, /runtime_session=retry-live/);
  assert.doesNotMatch(output.stdout, /sk-retry-memory-only/);
  const retry = requests.find((request) =>
    request.url.endsWith("/sessions/cancelled-live/retry"));
  assert.deepEqual(JSON.parse(retry.options.body), { apiKey: "sk-retry-memory-only" });
});

test("retry ignores an inherited BYO key for a Pi-auth parent", async () => {
  const requests = [];
  const { io } = fakeIo({
    env: {
      CHEMRXN_PI_INTERNAL_TOKEN: "local-token",
      CHEMRXN_USER_ID: "local-user",
      CHEMRXN_MODEL_API_KEY: "sk-stale-unrelated-key",
    },
    async fetch(url, options = {}) {
      requests.push({ url, options });
      if (url.endsWith("/healthz")) {
        return { ok: true, json: async () => ({ ok: true, ui: { document_mode: true } }) };
      }
      if (url.endsWith("/sessions/pi-auth-parent") && (options.method || "GET") === "GET") {
        return {
          ok: true,
          json: async () => ({
            id: "pi-auth-parent",
            authMode: "personal_codex_subscription",
            status: "interrupted",
          }),
        };
      }
      return {
        ok: true,
        json: async () => ({
          retried: true,
          backend: { mode: "local_pi_sdk" },
          session: { id: "pi-auth-retry", status: "running" },
        }),
      };
    },
  });
  assert.equal(await main(["retry", "pi-auth-parent", "--no-browser"], io), 0);
  const retry = requests.find((request) =>
    request.url.endsWith("/sessions/pi-auth-parent/retry"));
  assert.deepEqual(JSON.parse(retry.options.body), {});
  assert.equal(JSON.stringify(requests).includes("sk-stale-unrelated-key"), false);
});

test("export registers a live runtime result and downloads from the shared viewer", async () => {
  const writes = [];
  const calls = [];
  const { io, output } = fakeIo({
    async fetch(url, options = {}) {
      calls.push([url, options.method || "GET"]);
      if (url.endsWith("/healthz")) {
        return { ok: true, json: async () => ({ ok: true, ui: { document_mode: true } }) };
      }
      if (url.endsWith("/api/runtime/sessions/session-a/reviewer")) {
        return { ok: true, json: async () => ({ jobId: "runtime_pdf_abc" }) };
      }
      if (url.includes("/api/jobs/runtime_pdf_abc/export?fmt=csv")) {
        const bytes = new TextEncoder().encode("reaction,reactants,products\n1,CCO,CCN\n");
        return {
          ok: true,
          headers: { get: () => 'attachment; filename="runtime_pdf_abc_reactions.csv"' },
          arrayBuffer: async () => bytes.buffer,
        };
      }
      throw new Error(`unexpected URL: ${url}`);
    },
    async writeFile(target, bytes, options) {
      writes.push({ target, text: Buffer.from(bytes).toString("utf8"), options });
    },
  });
  assert.equal(await main([
    "export", "session-a", "--format", "csv", "--output", "chemistry.csv", "--json",
  ], io), 0);
  assert.deepEqual(calls.map(([url, method]) => [new URL(url).pathname, method]), [
    ["/healthz", "GET"],
    ["/api/runtime/sessions/session-a/reviewer", "POST"],
    ["/api/jobs/runtime_pdf_abc/export", "GET"],
  ]);
  assert.equal(writes.length, 1);
  assert.equal(path.basename(writes[0].target), "chemistry.csv");
  assert.match(writes[0].text, /CCO,CCN/);
  assert.equal(writes[0].options.mode, 0o600);
  const result = JSON.parse(output.stdout);
  assert.equal(result.jobId, "runtime_pdf_abc");
  assert.equal(result.format, "csv");
});

test("export also accepts an existing visual job without inventing a second renderer", async () => {
  const writes = [];
  const { io } = fakeIo({
    async fetch(url) {
      if (url.endsWith("/healthz")) {
        return { ok: true, json: async () => ({ ok: true, ui: { document_mode: true } }) };
      }
      if (url.endsWith("/api/runtime/sessions/example-job-latest/reviewer")) {
        return { ok: false, status: 404 };
      }
      if (url.includes("/api/jobs/example-job-latest/export?fmt=report")) {
        const bytes = new TextEncoder().encode('{"schema":"chemrxn.extraction-report.v1"}\n');
        return {
          ok: true,
          headers: { get: () => 'attachment; filename="example-job_report.json"' },
          arrayBuffer: async () => bytes.buffer,
        };
      }
      throw new Error(`unexpected URL: ${url}`);
    },
    async writeFile(target, bytes) {
      writes.push({ target, text: Buffer.from(bytes).toString("utf8") });
    },
  });
  assert.equal(await main([
    "export", "example-job-latest", "--format", "report", "--output", "report.json",
  ], io), 0);
  assert.equal(writes.length, 1);
  assert.match(writes[0].text, /extraction-report/);
});

test("review export downloads the shared human-reviewed artifact instead of raw extraction", async () => {
  const writes = [];
  const calls = [];
  const { io, output } = fakeIo({
    async fetch(url) {
      calls.push(url);
      if (url.endsWith("/healthz")) {
        return { ok: true, json: async () => ({ ok: true, ui: { document_mode: true } }) };
      }
      if (url.endsWith("/api/runtime/sessions/reviewed-job/reviewer")) {
        return { ok: false, status: 404 };
      }
      if (url.endsWith("/api/jobs/reviewed-job/review/export")) {
        const bytes = new TextEncoder().encode(
          '{"schema":"chemrxn.review-export.v1","target":"figure"}\n',
        );
        return {
          ok: true,
          headers: { get: () => 'attachment; filename="reviewed-job_reviewed_figure.json"' },
          arrayBuffer: async () => bytes.buffer,
        };
      }
      throw new Error(`unexpected URL: ${url}`);
    },
    async writeFile(target, bytes, options) {
      writes.push({ target, text: Buffer.from(bytes).toString("utf8"), options });
    },
  });
  assert.equal(await main([
    "export", "reviewed-job", "--format", "review", "--output", "review.json", "--json",
  ], io), 0);
  assert.equal(calls.some((url) => url.includes("/export?fmt=review")), false);
  assert.equal(calls.some((url) => url.endsWith("/review/export")), true);
  assert.equal(writes.length, 1);
  assert.match(writes[0].text, /chemrxn\.review-export\.v1/);
  assert.equal(writes[0].options.mode, 0o600);
  assert.equal(JSON.parse(output.stdout).format, "review");
});

test("rights export downloads the shared source-rights sidecar with a safe default filename", async () => {
  const writes = [];
  const calls = [];
  const { io, output } = fakeIo({
    async fetch(url) {
      calls.push(url);
      if (url.endsWith("/healthz")) {
        return { ok: true, json: async () => ({ ok: true, ui: { document_mode: true } }) };
      }
      if (url.endsWith("/api/runtime/sessions/ccby-job/reviewer")) {
        return { ok: false, status: 404 };
      }
      if (url.endsWith("/api/jobs/ccby-job/export?fmt=rights")) {
        const bytes = new TextEncoder().encode(
          '{"schema":"chemrxn.source-rights.v1","raw_prediction_immutable":true}\n',
        );
        return {
          ok: true,
          headers: { get: () => "" },
          arrayBuffer: async () => bytes.buffer,
        };
      }
      throw new Error(`unexpected URL: ${url}`);
    },
    async writeFile(target, bytes, options) {
      writes.push({ target, text: Buffer.from(bytes).toString("utf8"), options });
    },
  });
  assert.equal(await main([
    "export", "ccby-job", "--format", "rights", "--json",
  ], io), 0);
  assert.equal(calls.some((url) => url.endsWith("/export?fmt=rights")), true);
  assert.equal(writes.length, 1);
  assert.equal(path.basename(writes[0].target), "ccby-job_rights.json");
  assert.match(writes[0].text, /chemrxn\.source-rights\.v1/);
  assert.equal(writes[0].options.mode, 0o600);
  assert.equal(JSON.parse(output.stdout).format, "rights");
});

test("export accepts an existing visual job when the Pi runtime is not connected", async () => {
  const writes = [];
  const { io } = fakeIo({
    async fetch(url) {
      if (url.endsWith("/healthz")) {
        return { ok: true, json: async () => ({ ok: true, ui: { document_mode: true } }) };
      }
      if (url.endsWith("/api/runtime/sessions/imported-job/reviewer")) {
        return {
          ok: false,
          status: 503,
          json: async () => ({ detail: "Interactive Pi backend not connected" }),
        };
      }
      if (url.endsWith("/api/jobs/imported-job")) {
        return { ok: true, json: async () => ({ id: "imported-job", status: "done" }) };
      }
      if (url.includes("/api/jobs/imported-job/export?fmt=csv")) {
        const bytes = new TextEncoder().encode("kind,label\nexample,1\n");
        return {
          ok: true,
          headers: { get: () => 'attachment; filename="imported-job_reactions.csv"' },
          arrayBuffer: async () => bytes.buffer,
        };
      }
      throw new Error(`unexpected URL: ${url}`);
    },
    async writeFile(target, bytes) {
      writes.push({ target, text: Buffer.from(bytes).toString("utf8") });
    },
  });
  assert.equal(await main([
    "export", "imported-job", "--format", "csv", "--output", "imported.csv",
  ], io), 0);
  assert.equal(writes.length, 1);
  assert.match(writes[0].text, /example,1/);
});

test("export surfaces a runtime result that is not ready instead of treating it as a viewer job", async () => {
  const calls = [];
  const { io, output } = fakeIo({
    async fetch(url) {
      calls.push(url);
      if (url.endsWith("/healthz")) {
        return { ok: true, json: async () => ({ ok: true, ui: { document_mode: true } }) };
      }
      if (url.endsWith("/api/runtime/sessions/running-session/reviewer")) {
        return {
          ok: false,
          status: 409,
          json: async () => ({ detail: "Runtime result is not review-ready" }),
        };
      }
      throw new Error(`unexpected URL: ${url}`);
    },
  });
  assert.equal(await main(["export", "running-session", "--format", "v2"], io), 4);
  assert.match(output.stderr, /Runtime result is not review-ready/);
  assert.equal(calls.some((url) => url.includes("/api/jobs/running-session/export")), false);
});

test("local Pi session keeps BYO API key out of output and supports conversation actions", async () => {
  const requests = [];
  const { io, output } = fakeIo({
    env: {
      CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
      CHEMRXN_PI_INTERNAL_TOKEN: "local-token",
      CHEMRXN_USER_ID: "local-user",
      CHEMRXN_MODEL_API_KEY: "sk-local-secret-value",
      CHEMRXN_MODEL_PROVIDER: "test-provider",
      CHEMRXN_MODEL_ID: "test-model",
    },
    async fetch(url, options = {}) {
      requests.push({ url, options });
      if (url.endsWith("/healthz")) {
        return { ok: true, json: async () => ({ ok: true, ui: { document_mode: true } }) };
      }
      if (url.endsWith("/local-agent-sessions")) {
        return {
          ok: true,
          json: async () => ({ session: { id: "pi-session", status: "ready" } }),
        };
      }
      if (url.endsWith("/abort")) {
        return { ok: true, json: async () => ({ accepted: true, status: "cancelled" }) };
      }
      return {
        ok: true,
        json: async () => ({ accepted: true, behavior: JSON.parse(options.body).behavior }),
      };
    },
  });
  assert.equal(await main([
    "agent", "workspace-a", "--message", "extract Scheme 1", "--no-browser",
  ], io), 0);
  assert.match(output.stdout, /Pi session ready: pi-session/);
  assert.doesNotMatch(output.stdout, /sk-local-secret/);
  const create = requests.find((request) => request.url.endsWith("/local-agent-sessions"));
  assert.equal(JSON.parse(create.options.body).apiKey, "sk-local-secret-value");
  assert.equal(JSON.parse(create.options.body).thinkingLevel, "medium");
  assert.equal(requests.some((request) => request.url.endsWith("/messages")
    && JSON.parse(request.options.body).text === "extract Scheme 1"), true);

  assert.equal(await main(["ask", "pi-session", "--message", "extract this figure"], io), 0);
  assert.equal(await main(["steer", "pi-session", "--message", "inspect stereo"], io), 0);
  assert.equal(await main(["follow-up", "pi-session", "--message", "render results"], io), 0);
  assert.equal(await main(["cancel", "pi-session"], io), 0);
  assert.equal(requests.some((request) => request.url.endsWith("/messages")
    && JSON.parse(request.options.body).behavior === "follow_up"), true);
});

test("ask reports the new workspace when a sealed session forks a linked task", async () => {
  const { io, output } = fakeIo({
    env: {
      CHEMRXN_PI_INTERNAL_TOKEN: "local-token",
      CHEMRXN_USER_ID: "local-user",
    },
    async fetch(url) {
      assert.match(url, /\/v1\/sessions\/sealed-session\/messages$/);
      return {
        ok: true,
        json: async () => ({
          accepted: true,
          continued: true,
          parentSessionId: "sealed-session",
          session: { id: "linked-session", status: "running" },
        }),
      };
    },
  });
  assert.equal(await main([
    "ask", "sealed-session", "--message", "Which product has uncertain stereo?",
  ], io), 0);
  assert.match(output.stdout, /new linked task\tlinked-session/);
  assert.match(output.stdout, /sealed parent\tsealed-session/);
  assert.match(output.stdout, /runtime_session=linked-session/);
});

test("Pi auth mode creates a subscription-backed session without sending an API key", async () => {
  const requests = [];
  const { io } = fakeIo({
    env: {
      CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
      CHEMRXN_PI_INTERNAL_TOKEN: "local-token",
      CHEMRXN_USER_ID: "local-user",
    },
    async fetch(url, options = {}) {
      requests.push({ url, options });
      if (url.endsWith("/healthz")) {
        return { ok: true, json: async () => ({ ok: true, ui: { document_mode: true } }) };
      }
      return {
        ok: true,
        json: async () => ({ session: { id: "pi-auth-session", status: "ready" } }),
      };
    },
  });
  assert.equal(await main(["agent", "workspace-a", "--pi-auth", "--no-browser"], io), 0);
  const create = requests.find((request) => request.url.endsWith("/local-agent-sessions"));
  const body = JSON.parse(create.options.body);
  assert.equal(body.authMode, "pi_auth");
  assert.equal(body.provider, "openai-codex");
  assert.equal(body.modelId, "gpt-5.6-sol");
  assert.equal(body.thinkingLevel, "medium");
  assert.equal(Object.hasOwn(body, "apiKey"), false);

  requests.length = 0;
  assert.equal(await main([
    "agent", "workspace-b", "--pi-auth", "--provider", "openai-codex",
    "--model", "gpt-5.5", "--thinking", "high", "--no-browser",
  ], io), 0);
  const overridden = requests.find((request) => request.url.endsWith("/local-agent-sessions"));
  const overriddenBody = JSON.parse(overridden.options.body);
  assert.equal(overriddenBody.provider, "openai-codex");
  assert.equal(overriddenBody.modelId, "gpt-5.5");
  assert.equal(overriddenBody.thinkingLevel, "high");
  assert.equal(Object.hasOwn(overriddenBody, "apiKey"), false);
});

test("Pi auth mode accepts an explicit model override without sending an API key", async () => {
  const requests = [];
  const { io } = fakeIo({
    env: {
      CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
      CHEMRXN_PI_INTERNAL_TOKEN: "local-token",
      CHEMRXN_USER_ID: "local-user",
    },
    async fetch(url, options = {}) {
      requests.push({ url, options });
      if (url.endsWith("/healthz")) {
        return { ok: true, json: async () => ({ ok: true, ui: { document_mode: true } }) };
      }
      return {
        ok: true,
        json: async () => ({ session: { id: "pi-auth-override", status: "ready" } }),
      };
    },
  });
  assert.equal(await main([
    "agent", "workspace-a", "--pi-auth", "--model", "gpt-5.6-terra", "--no-browser",
  ], io), 0);
  const create = requests.find((request) => request.url.endsWith("/local-agent-sessions"));
  const body = JSON.parse(create.options.body);
  assert.equal(body.authMode, "pi_auth");
  assert.equal(body.provider, "openai-codex");
  assert.equal(body.modelId, "gpt-5.6-terra");
  assert.equal(body.thinkingLevel, "medium");
  assert.equal(Object.hasOwn(body, "apiKey"), false);
});

test("login opens the configured Pi CLI in a product-owned auth profile", async () => {
  const dataRoot = mkdtempSync(path.join(tmpdir(), "chemrxn-pi-login-"));
  const piCli = path.join(dataRoot, "pi-cli");
  let invocation;
  const { io, output } = fakeIo({
    env: {
      CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
      CHEMRXN_PI_CLI: piCli,
    },
    async piCredentials(authPath) {
      assert.equal(authPath, path.join(dataRoot, "pi-auth", "auth.json"));
      return [{ providerId: "openai-codex", type: "oauth" }];
    },
    spawnSync(command, args, options) {
      invocation = { command, args, options };
      return { status: 0, stdout: "", stderr: "" };
    },
  });
  try {
    assert.equal(await main(["login", "--data-root", dataRoot], io), 0);
    assert.equal(invocation.command, piCli);
    assert.deepEqual(invocation.args, []);
    assert.equal(
      invocation.options.env.PI_CODING_AGENT_DIR,
      path.join(dataRoot, "pi-auth"),
    );
    assert.match(output.stdout, /OpenAI \(ChatGPT Plus\/Pro\)/);
    assert.match(output.stdout, /Pi CLI bundled with ChemRxn/);
    assert.match(output.stdout, /Pi subscription profile configured: openai-codex \(oauth\)/);
  } finally {
    rmSync(dataRoot, { recursive: true, force: true });
  }
});

test("login fails honestly when Pi exits before a credential is saved", async () => {
  const dataRoot = mkdtempSync(path.join(tmpdir(), "chemrxn-pi-incomplete-"));
  const piCli = path.join(dataRoot, "pi-cli");
  const { io, output } = fakeIo({
    env: {
      CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
      CHEMRXN_PI_CLI: piCli,
    },
    async piCredentials() { return []; },
    spawnSync() { return { status: 0, stdout: "", stderr: "" }; },
  });
  try {
    assert.equal(await main(["login", "--data-root", dataRoot], io), 4);
    assert.match(output.stderr, /PI_LOGIN_INCOMPLETE/);
    assert.match(output.stderr, /openai-codex OAuth credential/);
  } finally {
    rmSync(dataRoot, { recursive: true, force: true });
  }
});

test("login and auth status reject unrelated Pi credentials", async () => {
  const dataRoot = mkdtempSync(path.join(tmpdir(), "chemrxn-pi-provider-"));
  const { io, output } = fakeIo({
    env: {
      CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
      CHEMRXN_PI_CLI: path.join(dataRoot, "pi-cli"),
    },
    async piCredentials() {
      return [{ providerId: "anthropic", type: "api_key" }];
    },
    spawnSync() { return { status: 0, stdout: "", stderr: "" }; },
  });
  try {
    assert.equal(await main(["login", "--data-root", dataRoot], io), 4);
    assert.match(output.stderr, /PI_LOGIN_INCOMPLETE/);
    output.stdout = "";
    output.stderr = "";
    assert.equal(await main(["auth-status", "--json", "--data-root", dataRoot], io), 1);
    const status = JSON.parse(output.stdout);
    assert.equal(status.configured, false);
    assert.equal(status.requiredProvider, "openai-codex");
    assert.equal(status.requiredType, "oauth");
    assert.equal(status.networkVerified, false);
  } finally {
    rmSync(dataRoot, { recursive: true, force: true });
  }
});

test("auth logout removes only the ChemRxn openai-codex subscription credential", async () => {
  const dataRoot = mkdtempSync(path.join(tmpdir(), "chemrxn-pi-logout-"));
  let credentials = [
    { providerId: "openai-codex", type: "oauth" },
    { providerId: "anthropic", type: "api_key" },
  ];
  let removedProvider = "";
  const { io, output } = fakeIo({
    async piCredentials() { return credentials; },
    async piLogout(authPath, providerId) {
      assert.equal(authPath, path.join(dataRoot, "pi-auth", "auth.json"));
      removedProvider = providerId;
      credentials = credentials.filter((item) => item.providerId !== providerId);
    },
  });
  try {
    assert.equal(await main(["auth", "logout", "--data-root", dataRoot], io), 0);
    assert.equal(removedProvider, "openai-codex");
    assert.match(output.stdout, /credential removed/);
    assert.deepEqual(credentials, [{ providerId: "anthropic", type: "api_key" }]);
  } finally {
    rmSync(dataRoot, { recursive: true, force: true });
  }
});

test("login never falls back to a PATH Pi executable", async () => {
  const dataRoot = mkdtempSync(path.join(tmpdir(), "chemrxn-pi-missing-"));
  const explicitPi = path.join(dataRoot, "missing-explicit-pi");
  let invoked = "";
  const { io, output } = fakeIo({
    env: {
      CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
      CHEMRXN_PI_CLI: explicitPi,
    },
    spawnSync(command) {
      invoked = command;
      return { status: null, error: new Error("missing") };
    },
  });
  try {
    assert.equal(await main(["login", "--data-root", dataRoot], io), 4);
    assert.equal(invoked, explicitPi);
    assert.match(output.stderr, /PI_CLI_NOT_FOUND/);
    assert.notEqual(invoked, "pi");
  } finally {
    rmSync(dataRoot, { recursive: true, force: true });
  }
});

test("extract uploads an immutable Figure and starts the real Pi skill session", async () => {
  const requests = [];
  const figure = path.resolve(process.cwd(), "../runtime/pi-service/test/fixtures/figure/demo-figure/image.png");
  const { io, output } = fakeIo({
    env: {
      CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
      CHEMRXN_PI_INTERNAL_TOKEN: "local-token",
      CHEMRXN_USER_ID: "local-user",
      CHEMRXN_MODEL_API_KEY: "sk-local-secret-value",
      CHEMRXN_MODEL_PROVIDER: "openai",
      CHEMRXN_MODEL_ID: "gpt-4.1",
    },
    createReadStream(filePath) {
      return { filePath };
    },
    async fetch(url, options = {}) {
      requests.push({ url, options });
      if (url.endsWith("/healthz")) {
        return { ok: true, json: async () => ({ ok: true, ui: { document_mode: true } }) };
      }
      if (url.endsWith("/input-artifacts")) {
        return {
          ok: true,
          json: async () => ({
            artifact: {
              id: "input-figure",
              kind: "figure",
              originalName: "1.png",
              sha256: "a".repeat(64),
              byteSize: 123,
            },
          }),
        };
      }
      return {
        ok: true,
        json: async () => ({
          session: { id: "live-session", status: "creating" },
          dispatch: {
            workItem: { id: "wrk-live", sessionId: "live-session", state: "pending" },
            position: 2,
            credentialRequired: false,
          },
        }),
      };
    },
  });
  assert.equal(await main([
    "extract", figure, "--message", "Focus on stereo", "--no-browser",
  ], io), 0);
  assert.match(output.stdout, /Live Figure extraction: accepted · queued at position 2/);
  assert.match(output.stdout, /runtime_session=live-session/);
  assert.doesNotMatch(output.stdout, /sk-local-secret/);
  const upload = requests.find((request) => request.url.endsWith("/input-artifacts"));
  assert.equal(upload.options.headers["x-chemrxn-input-kind"], "figure");
  assert.equal(upload.options.duplex, "half");
  const create = requests.find((request) => request.url.endsWith("/local-agent-sessions"));
  const body = JSON.parse(create.options.body);
  assert.equal(body.contextMode, "figure");
  assert.equal(body.thinkingLevel, "medium");
  assert.equal(body.startExtraction, true);
  assert.equal(body.initialMessage, "Focus on stereo");
  assert.deepEqual(body.inputs, [{ artifactId: "input-figure", kind: "figure" }]);

  requests.length = 0;
  output.stdout = "";
  assert.equal(await main([
    "open", figure, "--message", "Inspect conditions", "--no-browser",
  ], io), 0);
  assert.match(output.stdout, /Live Figure extraction: accepted · queued at position 2/);
  assert.match(output.stdout, /runtime_session=live-session/);
  const openCreate = requests.find((request) => request.url.endsWith("/local-agent-sessions"));
  assert.equal(JSON.parse(openCreate.options.body).initialMessage, "Inspect conditions");
});

test("runtime commands fail honestly when auth or backend is unavailable", async () => {
  {
    const isolated = mkdtempSync(path.join(tmpdir(), "chemrxn-no-runtime-auth-"));
    try {
      const { io, output } = fakeIo({
        env: {
          CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
          CHEMRXN_VIEWER_DATA: isolated,
        },
      });
      assert.equal(await main(["sessions"], io), 4);
      assert.match(output.stderr, /RUNTIME_AUTH_REQUIRED/);
    } finally {
      rmSync(isolated, { recursive: true, force: true });
    }
  }
  {
    const { io, output } = fakeIo({
      env: {
        CHEMRXN_REPO: path.resolve(process.cwd(), ".."),
        CHEMRXN_PI_INTERNAL_TOKEN: "local-token",
        CHEMRXN_USER_ID: "user-a",
      },
      async fetch() {
        throw new Error("connection refused");
      },
    });
    assert.equal(await main(["events", "session-a"], io), 4);
    assert.match(output.stderr, /RUNTIME_NOT_CONNECTED/);
  }
});

test("doctor JSON exposes backend truth and repository checks", async () => {
  const { io, output } = fakeIo();
  const code = await main(["doctor", "--json"], io);
  const report = JSON.parse(output.stdout);
  assert.equal(code, 0);
  assert.equal(report.extractionBackend, "figure_live_ready");
  assert.equal(report.interactivePiBackend, "local_api_key_or_pi_auth_ready");
  assert.equal(report.sealedReplayBackend, "ready");
  assert.equal(report.distribution, "private-git-spec");
  assert.equal(report.installIdentity.commit, "a".repeat(40));
  assert.equal(report.installIdentity.cryptographicAuthentication, "not-assessed");
  assert.equal(report.checks.some((item) => item.name === "installed-commit" && item.ok), true);
  assert.equal(report.checks.some((item) => item.name === "git" && item.ok), true);
  assert.equal(report.checks.some((item) => item.name === "git-lfs" && item.ok), true);
  assert.equal(report.checks.some((item) => item.name === "java-opsin" && item.ok), true);
  assert.equal(report.checks.some((item) => item.name === "shared-bbox-renderer" && item.ok), true);
  assert.equal(report.checks.some((item) => item.name === "chemistry-mcp" && item.ok), true);
  assert.equal(report.checks.some((item) => item.name === "compiled-skill-pins" && item.ok), true);
  assert.equal(report.checks.some((item) => item.name === "figure-skill" && item.ok), true);
  assert.equal(report.checks.some((item) => item.name === "packaged-runtime" && item.ok), true);
  assert.equal(report.checks.some((item) => item.name === "git-lfs-materialization" && item.ok), true);
  assert.equal(report.installIntegrity.verifiedAssetCount, 41);
  assert.match(report.skillPolicy, /SHA-256 pinned/);
  assert.deepEqual(report.blockers, []);
  assert.match(report.operationalPrerequisites[0], /best-effort/);
});

test("doctor resolves an immutable npm Git install commit without claiming authentication", async () => {
  const prefix = mkdtempSync(path.join(tmpdir(), "chemrxn-installed-identity-"));
  const packageRoot = path.join(prefix, "node_modules", "@chemrxn", "cli");
  const commit = "b".repeat(40);
  try {
    mkdirSync(packageRoot, { recursive: true });
    writeFileSync(path.join(prefix, "node_modules", ".package-lock.json"), JSON.stringify({
      packages: {
        "node_modules/@chemrxn/cli": {
          resolved: `git+ssh://git@github.com/rxnminer/RxnMiner.git#${commit}`,
        },
      },
    }));
    const identity = await resolveInstalledCommitIdentity(packageRoot, {
      spawnSync() {
        throw new Error("Git fallback should not run when npm lock identity is available");
      },
    });
    assert.equal(identity.commit, commit);
    assert.equal(identity.source, "npm hidden lockfile");
    assert.equal(identity.cryptographicAuthentication, "not-assessed");
    assert.match(identity.recommendation, /does not verify tag signatures or transport authentication/);
  } finally {
    rmSync(prefix, { recursive: true, force: true });
  }
});

test("doctor fails with an actionable Python check when no interpreter works", async () => {
  const { io, output } = fakeIo({
    spawnSync() {
      return { status: 1, stdout: "", stderr: "not found" };
    },
  });
  const code = await main(["doctor"], io);
  assert.equal(code, 1);
  assert.match(output.stdout, /action required/);
  assert.match(output.stdout, /Install Python 3.11 or 3.12/);
  assert.match(output.stdout, /extraction: figure_live_ready/);
  assert.match(output.stdout, /python: not found/);
});

test("doctor reports Git LFS and Java install prerequisites separately", async () => {
  const { io, output } = fakeIo({
    spawnSync(command, args) {
      output.calls.push([command, args]);
      if (command === "git" && args[0] === "lfs") {
        return { status: 1, stdout: "", stderr: "git: 'lfs' is not a git command" };
      }
      if (args[0] === "-version") {
        return { status: 1, stdout: "", stderr: "java not found" };
      }
      if (args?.[0]?.endsWith(path.join("figure_skills", "scripts", "run_chem_mcp.py"))) {
        return {
          status: 1,
          stdout: "",
          stderr: "chemistry probe omitted in prerequisite fixture",
        };
      }
      return { status: 0, stdout: command === "git" ? "git version 2.50.0\n" : "/usr/bin/python3\n", stderr: "" };
    },
  });
  assert.equal(await main(["doctor"], io), 1);
  assert.match(output.stdout, /✗ git-lfs/);
  assert.match(output.stdout, /git lfs install/);
  assert.match(output.stdout, /✗ java-opsin/);
  assert.match(output.stdout, /Java 8\+ runtime/);
});

test("setup creates a product-owned Python environment without a hidden postinstall", async () => {
  const dataRoot = mkdtempSync(path.join(tmpdir(), "chemrxn-python-setup-"));
  let environmentCreated = false;
  const { io, output } = fakeIo({
    spawnSync(command, args) {
      output.calls.push([command, args]);
      if (command.startsWith(dataRoot) && args[0] === "-c" && !environmentCreated) {
        return { status: 1, stdout: "", stderr: "not created yet" };
      }
      if (args[0] === "-m" && args[1] === "venv") environmentCreated = true;
      return {
        status: 0,
        stdout: args[0] === "-c" ? "/usr/bin/python3\n" : "",
        stderr: "",
      };
    },
  });
  try {
    assert.equal(await main(["setup", "--data-root", dataRoot], io), 0);
    assert.match(output.stdout, /Local chemistry environment ready/);
    assert.equal(output.calls.some(([command, args]) =>
      command === "python3"
      && args[0] === "-m"
      && args[1] === "venv"
      && args[2] === path.join(dataRoot, "python")), true);
    assert.equal(output.calls.some(([command, args]) =>
      command === path.join(dataRoot, "python", "bin", "python")
      && args.slice(0, 3).join(" ") === "-m pip install"
      && args.at(-1).endsWith(path.join("harness", "requirements-local.txt"))), true);
    assert.equal(output.calls.some(([command, args]) =>
      command === path.join(dataRoot, "python", "bin", "python")
      && args[0] === "-c"
      && args[1].includes("from rdkit import Chem")
      && args[1].includes("opsin_name_to_smiles")), true);
  } finally {
    rmSync(dataRoot, { recursive: true, force: true });
  }
});

test("setup fails closed on package integrity before creating a Python environment", async () => {
  const { io, output } = fakeIo({
    async verifyInstallIntegrity() {
      return {
        ok: false,
        schemaVersion: "chemrxn.install-integrity.v1",
        manifest: "packaging/install-integrity.json",
        assetCount: 1,
        verifiedAssetCount: 0,
        lfsAssetCount: 1,
        sampleAssetCount: 0,
        requiredFileCount: 1,
        checks: [],
        failures: [{
          code: "GIT_LFS_POINTER",
          path: "reviewer/vendor/ketcher/index.html",
          detail: "unmaterialized Git LFS pointer",
        }],
        assets: [],
      };
    },
  });
  assert.equal(await main(["setup"], io), 4);
  assert.match(output.stderr, /PACKAGE_INTEGRITY_FAILED/);
  assert.match(output.stderr, /GIT_LFS_POINTER/);
  assert.match(output.stderr, /git lfs install/);
  assert.equal(output.calls.length, 0);
});

test("setup refuses to create a venv until Java-backed OPSIN is available", async () => {
  const { io, output } = fakeIo({
    spawnSync(command, args) {
      output.calls.push([command, args]);
      if (args[0] === "-version") return { status: 1, stdout: "", stderr: "java not found" };
      return { status: 0, stdout: "/usr/bin/python3\n", stderr: "" };
    },
  });
  assert.equal(await main(["setup"], io), 3);
  assert.match(output.stderr, /JAVA_NOT_FOUND/);
  assert.match(output.stderr, /offline OPSIN/);
  assert.equal(output.calls.some(([, args]) => args[0] === "-m" && args[1] === "venv"), false);
});

test("CLI help states the honest backend boundary", async () => {
  const { io, output } = fakeIo();
  const code = await main(["help"], io);
  assert.equal(code, 0);
  assert.match(output.stdout, /Figure file handoff/);
  assert.match(output.stdout, /sealed replay/i);
  assert.match(output.stdout, /live artifacts are connected/);
  assert.match(output.stdout, /Pi-auth uses openai-codex and defaults to gpt-5\.6-sol/);
  assert.match(output.stdout, /medium thinking; --model\/--thinking or CHEMRXN_MODEL_ID\/CHEMRXN_PI_THINKING override them/);
  assert.match(output.stdout, /--format v2\|rxn\|csv\|report\|rights\|review/);
});
