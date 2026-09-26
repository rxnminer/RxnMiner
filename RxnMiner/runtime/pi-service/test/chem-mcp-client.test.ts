import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import {
  ChemMcpClient,
  chemistryToolDefinitions,
} from "../src/chem-mcp-client.js";
import { PINNED_TOOL_ALLOWLIST } from "../src/skills.js";
import { chemistryTestPython } from "./test-python.js";

test("Pi custom tools are discovered from the real chem-extract MCP allowlist", async () => {
  const workspace = mkdtempSync(path.join(tmpdir(), "chemrxn-mcp-"));
  const script = fileURLToPath(
    new URL("../../../../figure_skills/scripts/run_chem_mcp.py", import.meta.url),
  );
  const client = await ChemMcpClient.start({
    python: chemistryTestPython(),
    script,
    cwd: workspace,
    // Cold importing the chemistry server can approach five seconds on a
    // busy laptop even though the protocol is healthy.
    requestTimeoutMs: 15_000,
  });
  try {
    const tools = await client.listTools();
    assert.equal(tools.length, PINNED_TOOL_ALLOWLIST.length);
    const definitions = await chemistryToolDefinitions(client, PINNED_TOOL_ALLOWLIST);
    assert.deepEqual(
      definitions.map((tool) => tool.name).sort(),
      [...PINNED_TOOL_ALLOWLIST].sort(),
    );
    assert.equal(definitions.every((tool) => tool.executionMode === "sequential"), true);
    const unknown = await client.callTool("not_allowed", {});
    assert.equal(unknown.isError, true);
  } finally {
    await client.terminate();
    rmSync(workspace, { recursive: true, force: true });
  }
});

test("failed MCP initialization terminates the spawned Python process", async () => {
  const workspace = mkdtempSync(path.join(tmpdir(), "chemrxn-mcp-init-fail-"));
  const script = path.join(workspace, "hang.py");
  const pidFile = path.join(workspace, "pid");
  writeFileSync(script, [
    "import os, pathlib, time",
    "pathlib.Path(os.environ['PID_FILE']).write_text(str(os.getpid()))",
    "time.sleep(60)",
  ].join("\n"));
  try {
    await assert.rejects(ChemMcpClient.start({
      python: chemistryTestPython(),
      script,
      cwd: workspace,
      env: { PID_FILE: pidFile },
      // Give the child enough time to publish its PID before testing the
      // initialization timeout/termination path.
      requestTimeoutMs: 5_000,
    }), /timed out/);
    const pid = Number(readFileSync(pidFile, "utf8"));
    assert.equal(processAlive(pid), false);
  } finally {
    rmSync(workspace, { recursive: true, force: true });
  }
});

test("aborting MCP initialization terminates the spawned process group promptly", async () => {
  const workspace = mkdtempSync(path.join(tmpdir(), "chemrxn-mcp-init-abort-"));
  const script = path.join(workspace, "hang.py");
  const pidFile = path.join(workspace, "pid");
  writeFileSync(script, [
    "import os, pathlib, time",
    "pathlib.Path(os.environ['PID_FILE']).write_text(str(os.getpid()))",
    "time.sleep(60)",
  ].join("\n"));
  const controller = new AbortController();
  try {
    const started = ChemMcpClient.start({
      python: chemistryTestPython(),
      script,
      cwd: workspace,
      env: { PID_FILE: pidFile },
      requestTimeoutMs: 120_000,
      signal: controller.signal,
    });
    for (let attempt = 0; attempt < 100 && !readPid(pidFile); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    const pid = readPid(pidFile);
    assert.ok(pid, "MCP child did not publish its PID");
    const abortStarted = Date.now();
    controller.abort("runtime_shutdown");
    await assert.rejects(started, /aborted|runtime_shutdown/i);
    assert.ok(Date.now() - abortStarted < 3_000);
    assert.equal(await waitForProcessExit(pid!), true);
  } finally {
    rmSync(workspace, { recursive: true, force: true });
  }
});

test("terminating MCP kills synchronous chemistry work before returning", async () => {
  const workspace = mkdtempSync(path.join(tmpdir(), "chemrxn-mcp-cancel-"));
  const script = path.join(workspace, "sync_server.py");
  const pidFile = path.join(workspace, "pid");
  writeFileSync(script, [
    "import json, os, pathlib, sys, time",
    "pathlib.Path(os.environ['PID_FILE']).write_text(str(os.getpid()))",
    "for line in sys.stdin:",
    "  msg=json.loads(line)",
    "  if msg.get('method') == 'initialize':",
    "    out={'jsonrpc':'2.0','id':msg['id'],'result':{'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'test','version':'1'}}}",
    "  elif msg.get('method') == 'tools/call':",
    "    time.sleep(60)",
    "    out={'jsonrpc':'2.0','id':msg['id'],'result':{'content':[{'type':'text','text':'late'}]}}",
    "  else:",
    "    continue",
    "  print(json.dumps(out), flush=True)",
  ].join("\n"));
  const client = await ChemMcpClient.start({
    python: chemistryTestPython(),
    script,
    cwd: workspace,
    env: { PID_FILE: pidFile },
    requestTimeoutMs: 5_000,
  });
  try {
    const rejected = assert.rejects(client.callTool("slow", {}), /disposed|exited/);
    await new Promise((resolve) => setTimeout(resolve, 50));
    await client.terminate();
    await rejected;
    const pid = Number(readFileSync(pidFile, "utf8"));
    assert.equal(processAlive(pid), false);
  } finally {
    await client.terminate();
    rmSync(workspace, { recursive: true, force: true });
  }
});

test("MCP request timeout terminates synchronous chemistry work", async () => {
  const workspace = mkdtempSync(path.join(tmpdir(), "chemrxn-mcp-timeout-"));
  const script = path.join(workspace, "timeout_server.py");
  const pidFile = path.join(workspace, "pid");
  writeFileSync(script, [
    "import json, os, pathlib, sys, time",
    "pathlib.Path(os.environ['PID_FILE']).write_text(str(os.getpid()))",
    "for line in sys.stdin:",
    "  msg=json.loads(line)",
    "  if msg.get('method') == 'initialize':",
    "    out={'jsonrpc':'2.0','id':msg['id'],'result':{'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'test','version':'1'}}}",
    "    print(json.dumps(out), flush=True)",
    "  elif msg.get('method') == 'tools/call':",
    "    time.sleep(60)",
  ].join("\n"));
  const client = await ChemMcpClient.start({
    python: chemistryTestPython(),
    script,
    cwd: workspace,
    env: { PID_FILE: pidFile },
    requestTimeoutMs: 50,
  });
  try {
    await assert.rejects(client.callTool("slow", {}), /timed out/);
    const pid = Number(readFileSync(pidFile, "utf8"));
    assert.equal(await waitForProcessExit(pid), true);
  } finally {
    await client.terminate();
    rmSync(workspace, { recursive: true, force: true });
  }
});

function processAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function readPid(target: string): number | undefined {
  try {
    const pid = Number(readFileSync(target, "utf8"));
    return Number.isSafeInteger(pid) && pid > 1 ? pid : undefined;
  } catch {
    return undefined;
  }
}

async function waitForProcessExit(pid: number): Promise<boolean> {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (!processAlive(pid)) return true;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  return !processAlive(pid);
}
