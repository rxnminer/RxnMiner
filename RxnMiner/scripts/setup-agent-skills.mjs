#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { existsSync, lstatSync, mkdirSync, readlinkSync, rmSync, symlinkSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(SCRIPT_DIR, "..");
const SKILLS = [
  ["reaction-extract", path.join(REPO, "figure_skills", "skills", "reaction-extract")],
  ["reaction-verify", path.join(REPO, "figure_skills", "skills", "reaction-verify")],
];
const MCP_NAME = "chemrxn-extract";
const MCP_RUNNER = path.join(REPO, "figure_skills", "scripts", "run_chem_mcp.py");

export function parseSetupArgs(argv, env = process.env) {
  const options = {
    targets: new Set(), action: "install", dryRun: false, runtime: true,
    force: false, home: path.resolve(env.HOME || homedir()),
  };
  for (const arg of argv) {
    if (arg === "--all") ["claude", "codex", "pi"].forEach((x) => options.targets.add(x));
    else if (arg === "--claude") options.targets.add("claude");
    else if (arg === "--codex") options.targets.add("codex");
    else if (arg === "--pi") options.targets.add("pi");
    else if (arg === "--check") options.action = "check";
    else if (arg === "--uninstall") options.action = "uninstall";
    else if (arg === "--dry-run") options.dryRun = true;
    else if (arg === "--skills-only" || arg === "--no-runtime") options.runtime = false;
    else if (arg === "--force") options.force = true;
    else if (arg === "--help" || arg === "-h") options.action = "help";
    else throw new Error(`Unknown setup option: ${arg}`);
  }
  return options;
}

function commandPath(name) {
  const probe = spawnSync("sh", ["-c", `command -v ${name}`], { encoding: "utf8" });
  return probe.status === 0 ? probe.stdout.trim() : null;
}

function detectedTargets() {
  const targets = new Set();
  if (commandPath("claude")) targets.add("claude");
  if (commandPath("codex")) targets.add("codex");
  if (commandPath("pi") || existsSync(path.join(REPO, "node_modules", ".bin", "pi"))) targets.add("pi");
  return targets;
}

function destinationRoot(target, home) {
  if (target === "claude") return path.join(home, ".claude", "skills");
  if (target === "codex") return path.join(home, ".codex", "skills");
  return path.join(home, ".pi", "agent", "skills");
}

function sameLink(destination, source) {
  try {
    return lstatSync(destination).isSymbolicLink()
      && path.resolve(path.dirname(destination), readlinkSync(destination)) === path.resolve(source);
  } catch {
    return false;
  }
}

function preflightManagedPath(destination, source, options) {
  if (sameLink(destination, source) || !existsSync(destination) || options.force) return;
  throw new Error(`${destination} already exists and is not managed by this checkout; use --force only after reviewing it`);
}

function preflightLinks(target, options) {
  const root = destinationRoot(target, options.home);
  for (const [name, source] of SKILLS) preflightManagedPath(path.join(root, name), source, options);
}

function installLinks(target, options, log) {
  const root = destinationRoot(target, options.home);
  if (!options.dryRun) mkdirSync(root, { recursive: true, mode: 0o700 });
  for (const [name, source] of SKILLS) {
    const destination = path.join(root, name);
    if (sameLink(destination, source)) {
      log(`ok ${target}: ${name}`);
      continue;
    }
    if (existsSync(destination)) {
      if (!options.force) throw new Error(`${destination} already exists and is not managed by this checkout; use --force only after reviewing it`);
      log(`replace ${destination}`);
      if (!options.dryRun) rmSync(destination, { recursive: true, force: true });
    }
    log(`link ${destination} -> ${source}`);
    if (!options.dryRun) symlinkSync(source, destination, "dir");
  }
}

function uninstallLinks(target, options, log) {
  const root = destinationRoot(target, options.home);
  for (const [name, source] of SKILLS) {
    const destination = path.join(root, name);
    if (sameLink(destination, source)) {
      log(`remove ${destination}`);
      if (!options.dryRun) rmSync(destination);
    } else if (existsSync(destination)) {
      log(`keep unmanaged ${destination}`);
    }
  }
}

function checkLinks(target, options, log) {
  let ok = true;
  const root = destinationRoot(target, options.home);
  for (const [name, source] of SKILLS) {
    const installed = sameLink(path.join(root, name), source);
    log(`${installed ? "ok" : "missing"} ${target}: ${name}`);
    ok &&= installed;
  }
  return ok;
}

function run(command, args, options, log, allowFailure = false) {
  log(`$ ${[command, ...args].map(shellDisplay).join(" ")}`);
  if (options.dryRun) return { status: 0, stdout: "", stderr: "" };
  const result = spawnSync(command, args, { encoding: "utf8", stdio: allowFailure ? "pipe" : "inherit", env: process.env });
  if (result.error) throw result.error;
  if (!allowFailure && result.status !== 0) throw new Error(`${command} exited ${result.status}`);
  return result;
}

function shellDisplay(value) {
  return /^[A-Za-z0-9_./:@=-]+$/.test(value) ? value : JSON.stringify(value);
}

function mcpCommand(target) {
  const executable = commandPath(target === "claude" ? "claude" : "codex");
  if (!executable) throw new Error(`${target} CLI is not installed or not on PATH`);
  return executable;
}

function mcpGet(target, options, log) {
  const command = mcpCommand(target);
  return run(command, ["mcp", "get", MCP_NAME], options, log, true);
}

function installMcp(target, options, log) {
  const command = mcpCommand(target);
  const current = mcpGet(target, options, () => {});
  const configured = current.status === 0;
  const output = `${current.stdout || ""}\n${current.stderr || ""}`;
  if (configured && !output.includes(MCP_RUNNER) && !options.dryRun && !options.force) {
    throw new Error(`${MCP_NAME} already exists in ${target} but does not point to ${MCP_RUNNER}`);
  }
  if (configured) {
    const removeArgs = target === "claude"
      ? ["mcp", "remove", "--scope", "user", MCP_NAME]
      : ["mcp", "remove", MCP_NAME];
    run(command, removeArgs, options, log);
  }
  const addArgs = target === "claude"
    ? ["mcp", "add", "--scope", "user", MCP_NAME, "--", "python3", MCP_RUNNER]
    : ["mcp", "add", MCP_NAME, "--", "python3", MCP_RUNNER];
  run(command, addArgs, options, log);
}

function preflightMcp(target, options) {
  mcpCommand(target);
  if (options.dryRun) return;
  const current = mcpGet(target, options, () => {});
  const output = `${current.stdout || ""}\n${current.stderr || ""}`;
  if (current.status === 0 && !output.includes(MCP_RUNNER) && !options.force) {
    throw new Error(`${MCP_NAME} already exists in ${target} but does not point to ${MCP_RUNNER}`);
  }
}

function uninstallMcp(target, options, log) {
  const command = mcpCommand(target);
  const current = mcpGet(target, options, () => {});
  const output = `${current.stdout || ""}\n${current.stderr || ""}`;
  if (current.status !== 0) return log(`not installed ${target}: ${MCP_NAME}`);
  if (!output.includes(MCP_RUNNER) && !options.dryRun) return log(`keep unmanaged ${target} MCP: ${MCP_NAME}`);
  const args = target === "claude"
    ? ["mcp", "remove", "--scope", "user", MCP_NAME]
    : ["mcp", "remove", MCP_NAME];
  run(command, args, options, log);
}

function checkMcp(target, options, log) {
  const executable = commandPath(target === "claude" ? "claude" : "codex");
  if (!executable) {
    log(`missing ${target}: CLI unavailable`);
    return false;
  }
  const current = mcpGet(target, options, () => {});
  const output = `${current.stdout || ""}\n${current.stderr || ""}`;
  const ok = options.dryRun || (current.status === 0 && output.includes(MCP_RUNNER));
  log(`${ok ? "ok" : "missing"} ${target}: ${MCP_NAME} MCP`);
  return ok;
}

function piCommand() {
  return commandPath("pi") || (existsSync(path.join(REPO, "node_modules", ".bin", "pi"))
    ? path.join(REPO, "node_modules", ".bin", "pi") : null);
}

function installPiPackage(options, log) {
  if (!existsSync(path.join(REPO, "node_modules", "typebox"))) {
    run("npm", ["install"], options, log);
  }
  const pi = piCommand();
  if (!pi) throw new Error("Pi CLI is not installed; install Pi or run npm install in this checkout");
  run(pi, ["install", REPO], options, log);
}

function checkPiPackage(options, log) {
  const pi = piCommand();
  if (!pi) {
    log("missing pi: CLI unavailable");
    return false;
  }
  const listed = run(pi, ["list"], options, () => {}, true);
  const output = `${listed.stdout || ""}\n${listed.stderr || ""}`;
  const ok = options.dryRun || (listed.status === 0 && output.includes(REPO));
  log(`${ok ? "ok" : "missing"} pi package: ${REPO}`);
  return ok;
}

function uninstallPiPackage(options, log) {
  const pi = piCommand();
  if (!pi) return log("Pi CLI unavailable; managed skill links are still removed");
  run(pi, ["remove", REPO], options, log, true);
}

function cliDestination(options) {
  return path.join(options.home, ".local", "bin", "chemrxn");
}

function preflightCliLink(options) {
  preflightManagedPath(cliDestination(options), path.resolve(REPO, "..", "chemrxn"), options);
}

function installCliLink(options, log) {
  const source = path.resolve(REPO, "..", "chemrxn");
  const destination = cliDestination(options);
  if (sameLink(destination, source)) return log(`ok launcher: ${destination}`);
  if (existsSync(destination)) {
    if (!options.force) throw new Error(`${destination} already exists and is not managed by this checkout`);
    if (!options.dryRun) rmSync(destination, { recursive: true, force: true });
  }
  if (!options.dryRun) mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
  log(`link ${destination} -> ${source}`);
  if (!options.dryRun) symlinkSync(source, destination);
  const pathEntries = (process.env.PATH || "").split(path.delimiter).map((entry) => path.resolve(entry));
  if (!pathEntries.includes(path.resolve(path.dirname(destination)))) {
    log(`note: add ${path.dirname(destination)} to PATH to invoke chemrxn outside this checkout`);
  }
}

function uninstallCliLink(options, log) {
  const source = path.resolve(REPO, "..", "chemrxn");
  const destination = cliDestination(options);
  if (sameLink(destination, source)) {
    log(`remove ${destination}`);
    if (!options.dryRun) rmSync(destination);
  } else if (existsSync(destination)) log(`keep unmanaged ${destination}`);
}

function checkCliLink(options, log) {
  const ok = sameLink(cliDestination(options), path.resolve(REPO, "..", "chemrxn"));
  log(`${ok ? "ok" : "missing"} launcher: ${cliDestination(options)}`);
  return ok;
}

function installRuntime(options, log) {
  if (!options.runtime) return;
  if (!existsSync(path.join(REPO, "node_modules", ".bin", "pi"))) {
    run("npm", ["install"], options, log);
  }
  run(path.resolve(REPO, "..", "chemrxn"), ["setup"], options, log);
}

export function setupHelp() {
  return `ChemRxn universal Agent Skill setup\n\nUsage:\n  ./setup [--all|--claude|--codex|--pi] [--skills-only] [--dry-run]\n  ./setup --check [targets]\n  ./setup --uninstall [targets]\n\nWith no target, installed agent CLIs are detected. Setup installs the same canonical\nFigure Skills into each agent, registers one chemistry MCP for Claude/Codex,\nand installs the native typed extension as a local Pi package. By default it also\nprepares the explicit ChemRxn Workspace runtime; use --skills-only to skip that.\nUse --dry-run to preview an install without writing managed files.\nNo model credentials are read, copied, or created.\n`;
}

export function runSetup(argv, { log = console.log } = {}) {
  const options = parseSetupArgs(argv);
  if (options.action === "help") {
    log(setupHelp().trimEnd());
    return 0;
  }
  if (!options.targets.size) options.targets = detectedTargets();
  if (!options.targets.size) throw new Error("No supported agent CLI detected; pass --claude, --codex, --pi, or --all explicitly");
  if (options.action === "install") {
    preflightCliLink(options);
    for (const target of options.targets) {
      if (target !== "pi") {
        mcpCommand(target);
        preflightLinks(target, options);
        preflightMcp(target, options);
      }
    }
  }
  let ok = true;
  if (options.action === "install") installCliLink(options, log);
  else if (options.action === "uninstall") uninstallCliLink(options, log);
  else ok = checkCliLink(options, log) && ok;
  for (const target of options.targets) {
    if (options.action === "install") {
      if (target === "pi") installPiPackage(options, log);
      else {
        installLinks(target, options, log);
        installMcp(target, options, log);
      }
    } else if (options.action === "uninstall") {
      if (target === "pi") uninstallPiPackage(options, log);
      else {
        uninstallLinks(target, options, log);
        uninstallMcp(target, options, log);
      }
    } else if (target === "pi") ok = checkPiPackage(options, log) && ok;
    else {
      ok = checkLinks(target, options, log) && ok;
      ok = checkMcp(target, options, log) && ok;
    }
  }
  if (options.action === "install") installRuntime(options, log);
  if (options.action === "install") {
    log("\nReady. Invoke reaction-extract (Claude /reaction-extract, Codex $reaction-extract, Pi /skill:reaction-extract).");
    log("Open a persisted external-agent result with: chemrxn review PRED.v2.json --image figure.png [--perception MOLPARSER.json]");
  }
  return ok ? 0 : 1;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    process.exitCode = runSetup(process.argv.slice(2));
  } catch (error) {
    console.error(`setup: ${error instanceof Error ? error.message : String(error)}`);
    process.exitCode = 1;
  }
}
