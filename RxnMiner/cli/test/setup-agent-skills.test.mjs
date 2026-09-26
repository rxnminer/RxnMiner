import assert from "node:assert/strict";
import { chmodSync, existsSync, lstatSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test } from "node:test";
import { parseSetupArgs } from "../../scripts/setup-agent-skills.mjs";

const REPO = path.resolve(import.meta.dirname, "../..");
const SCRIPT = path.join(REPO, "scripts", "setup-agent-skills.mjs");

test("universal setup parses explicit harness targets and safe lifecycle flags", () => {
  const parsed = parseSetupArgs(["--claude", "--codex", "--skills-only", "--dry-run"], { HOME: "/tmp/home" });
  assert.deepEqual([...parsed.targets], ["claude", "codex"]);
  assert.equal(parsed.runtime, false);
  assert.equal(parsed.dryRun, true);
  assert.equal(parsed.action, "install");
  assert.throws(() => parseSetupArgs(["--unknown"]), /Unknown setup option/);
  const help = spawnSync(process.execPath, [SCRIPT, "--help"], { encoding: "utf8" });
  assert.equal(help.status, 0, help.stderr);
  assert.match(help.stdout, /--dry-run/);
});

test("setup installs canonical links for all agents and removes only managed links", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-agent-setup-"));
  const home = path.join(root, "home");
  const bin = path.join(root, "bin");
  const calls = path.join(root, "calls.txt");
  mkdirSync(home, { recursive: true });
  mkdirSync(bin, { recursive: true });
  const fake = `#!/bin/sh\necho "$(basename "$0") $*" >> "$CHEMRXN_TEST_CALLS"\nif [ "$1 $2" = "mcp get" ]; then exit 1; fi\nexit 0\n`;
  for (const name of ["claude", "codex", "pi", "npm"]) {
    const target = path.join(bin, name);
    writeFileSync(target, fake);
    chmodSync(target, 0o755);
  }
  const env = {
    ...process.env,
    HOME: home,
    PATH: `${bin}:${process.env.PATH}`,
    CHEMRXN_TEST_CALLS: calls,
  };
  try {
    const installed = spawnSync(process.execPath, [SCRIPT, "--all", "--skills-only"], { env, encoding: "utf8" });
    assert.equal(installed.status, 0, installed.stderr);
    assert.equal(lstatSync(path.join(home, ".local", "bin", "chemrxn")).isSymbolicLink(), true);
    for (const [agent, prefix] of [["claude", ".claude"], ["codex", ".codex"]]) {
      for (const skill of ["reaction-extract", "reaction-verify"]) {
        const link = path.join(home, prefix, "skills", skill);
        assert.equal(existsSync(link), true, `${agent} ${skill}`);
        assert.equal(lstatSync(link).isSymbolicLink(), true);
      }
    }
    const commandLog = readFileSync(calls, "utf8");
    assert.match(commandLog, /claude mcp add --scope user chemrxn-extract/);
    assert.match(commandLog, /codex mcp add chemrxn-extract/);
    assert.match(commandLog, /pi install/);
    assert.equal(existsSync(path.join(home, ".claude", "auth.json")), false);
    assert.equal(existsSync(path.join(home, ".codex", "auth.json")), false);
    assert.equal(existsSync(path.join(home, ".pi", "agent", "auth.json")), false);
    assert.equal(existsSync(path.join(home, ".pi", "agent", "skills", "reaction-extract")), false,
      "Pi consumes the root package manifest rather than a duplicate global skill link");

    const removed = spawnSync(process.execPath, [SCRIPT, "--all", "--skills-only", "--uninstall"], { env, encoding: "utf8" });
    assert.equal(removed.status, 0, removed.stderr);
    assert.equal(existsSync(path.join(home, ".local", "bin", "chemrxn")), false);
    assert.equal(existsSync(path.join(home, ".claude", "skills", "pdf-reaction-extract")), false);
    assert.equal(existsSync(path.join(home, ".codex", "skills", "reaction-extract")), false);
    assert.equal(existsSync(path.join(home, ".pi", "agent", "skills", "reaction-verify")), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("setup refuses to overwrite an unmanaged skill without explicit force", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-agent-collision-"));
  const home = path.join(root, "home");
  const bin = path.join(root, "bin");
  const collision = path.join(home, ".codex", "skills", "reaction-extract");
  mkdirSync(bin, { recursive: true });
  const codex = path.join(bin, "codex");
  writeFileSync(codex, "#!/bin/sh\nexit 1\n");
  chmodSync(codex, 0o755);
  mkdirSync(collision, { recursive: true });
  writeFileSync(path.join(collision, "SKILL.md"), "foreign\n");
  try {
    const result = spawnSync(process.execPath, [SCRIPT, "--codex", "--skills-only"], {
      env: { ...process.env, HOME: home, PATH: `${bin}:/usr/bin:/bin` }, encoding: "utf8",
    });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /already exists and is not managed/);
    assert.equal(readFileSync(path.join(collision, "SKILL.md"), "utf8"), "foreign\n");
    assert.equal(existsSync(path.join(home, ".local", "bin", "chemrxn")), false,
      "all unmanaged collisions are detected before the shared launcher is written");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("setup preflights an unmanaged MCP before writing links", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-agent-mcp-collision-"));
  const home = path.join(root, "home");
  const bin = path.join(root, "bin");
  mkdirSync(home, { recursive: true });
  mkdirSync(bin, { recursive: true });
  const codex = path.join(bin, "codex");
  writeFileSync(codex, "#!/bin/sh\nif [ \"$1 $2\" = \"mcp get\" ]; then echo /foreign/run_mcp.py; exit 0; fi\nexit 0\n");
  chmodSync(codex, 0o755);
  try {
    const result = spawnSync(process.execPath, [SCRIPT, "--codex", "--skills-only"], {
      env: { ...process.env, HOME: home, PATH: `${bin}:/usr/bin:/bin` }, encoding: "utf8",
    });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /already exists in codex but does not point/);
    assert.equal(existsSync(path.join(home, ".local", "bin", "chemrxn")), false);
    assert.equal(existsSync(path.join(home, ".codex", "skills")), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("setup preflights requested agent CLIs before writing managed files", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-agent-preflight-"));
  const home = path.join(root, "home");
  mkdirSync(home, { recursive: true });
  try {
    const result = spawnSync(process.execPath, [SCRIPT, "--claude", "--skills-only"], {
      env: { ...process.env, HOME: home, PATH: "/usr/bin:/bin" }, encoding: "utf8",
    });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /claude CLI is not installed/);
    assert.equal(existsSync(path.join(home, ".local", "bin", "chemrxn")), false);
    assert.equal(existsSync(path.join(home, ".claude", "skills")), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("setup check reports every requested agent even when the launcher is missing", () => {
  const root = mkdtempSync(path.join(tmpdir(), "chemrxn-agent-check-"));
  const home = path.join(root, "home");
  const bin = path.join(root, "bin");
  mkdirSync(home, { recursive: true });
  mkdirSync(bin, { recursive: true });
  const fake = "#!/bin/sh\nexit 1\n";
  for (const name of ["claude", "codex", "pi"]) {
    const target = path.join(bin, name);
    writeFileSync(target, fake);
    chmodSync(target, 0o755);
  }
  try {
    const result = spawnSync(process.execPath, [SCRIPT, "--all", "--skills-only", "--check"], {
      env: { ...process.env, HOME: home, PATH: `${bin}:/usr/bin:/bin` }, encoding: "utf8",
    });
    assert.equal(result.status, 1);
    assert.match(result.stdout, /missing launcher:/);
    assert.match(result.stdout, /missing claude: reaction-extract/);
    assert.match(result.stdout, /missing claude: chemrxn-extract MCP/);
    assert.match(result.stdout, /missing codex: reaction-verify/);
    assert.match(result.stdout, /missing codex: chemrxn-extract MCP/);
    assert.match(result.stdout, /missing pi package:/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
