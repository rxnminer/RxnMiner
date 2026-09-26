import { accessSync, constants as fsConstants, mkdirSync } from "node:fs";
import { createReadStream } from "node:fs";
import { access, mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { spawn, spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import { homedir } from "node:os";
import path from "node:path";
import {
  formatInstallIntegrityFailures,
  verifyInstallIntegrity,
} from "./install-integrity.mjs";

const SUPPORTED_INPUT_EXTENSIONS = new Set([
  ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff",
]);
const SEALED_REPLAY_FIXTURES = new Set([
  "demo-figure",
]);
const DEFAULT_VIEWER_PORT = 8787;
const DEFAULT_PI_MODEL = "gpt-5.6-sol";
const DEFAULT_PI_THINKING = "medium";

export async function main(argv, io = defaultIo()) {
  try {
    const parsed = parseArgs(argv, io.env);
    if (parsed.command === "help") {
      io.stdout(helpText());
      return 0;
    }

    const repo = await resolveRepo(io.env);
    if (parsed.command === "doctor") {
      const report = await doctor(repo, io, parsed);
      io.stdout(parsed.json ? `${JSON.stringify(report, null, 2)}\n` : formatDoctor(report));
      return report.ok ? 0 : 1;
    }
    if (parsed.command === "setup") {
      return await setupLocalPython(repo, parsed, io);
    }
    if (parsed.command === "login") {
      return await loginWithPi(parsed, io);
    }
    if (parsed.command === "auth-status") {
      return await showPiAuthStatus(parsed, io);
    }
    if (parsed.command === "logout") {
      return await logoutPiAuth(parsed, io);
    }

    if (parsed.command === "start" || parsed.command === "status" || parsed.command === "stop") {
      return runViewerLauncher(repo, parsed, io);
    }

    if (parsed.command === "open") {
      return await openWorkspace(repo, parsed, io);
    }
    if (parsed.command === "review") {
      return await reviewExternalResult(repo, parsed, io);
    }

    if (parsed.command === "sessions") {
      return await listRuntimeSessions(parsed, io);
    }

    if (parsed.command === "events") {
      return await listRuntimeEvents(parsed, io);
    }
    if (parsed.command === "fixtures") {
      return await listReplayFixtures(parsed, io);
    }
    if (parsed.command === "demo") {
      return await createReplayDemo(repo, parsed, io);
    }
    if (parsed.command === "result") {
      return await getRuntimeResult(parsed, io);
    }
    if (parsed.command === "export") {
      return await exportExtraction(repo, parsed, io);
    }
    if (parsed.command === "retry") {
      return await retryRuntimeSession(repo, parsed, io);
    }
    if (parsed.command === "agent") {
      return await createLocalAgent(repo, parsed, io);
    }
    if (parsed.command === "extract") {
      return await createLiveExtraction(repo, parsed, io);
    }
    if (["chat", "steer", "follow-up"].includes(parsed.command)) {
      return await sendAgentMessage(parsed, io);
    }
    if (parsed.command === "abort") {
      return await abortAgentSession(parsed, io);
    }
    throw new CliError("UNKNOWN_COMMAND", `Unknown command: ${parsed.command}`, 2);
  } catch (error) {
    const code = error instanceof CliError ? error.exitCode : 1;
    const label = error instanceof CliError ? error.code : "UNEXPECTED_ERROR";
    io.stderr(`chemrxn: ${label}: ${error instanceof Error ? error.message : String(error)}\n`);
    return code;
  }
}

export function parseArgs(argv, env = process.env) {
  const args = [...argv];
  let command = args.shift() ?? "help";
  if (command === "auth") {
    const authCommand = args.shift();
    if (authCommand === "status") command = "auth-status";
    else if (authCommand === "logout") command = "logout";
    else {
      throw new CliError(
        "INVALID_ARGUMENT",
        "auth requires `status` or `logout`",
        2,
      );
    }
  }
  command = ({
    jobs: "sessions",
    ask: "chat",
    cancel: "abort",
  })[command] || command;
  if (command === "--help" || command === "-h" || command === "help") {
    return { command: "help" };
  }
  if (![
    "doctor", "setup", "login", "logout", "auth-status", "start", "status", "stop", "open", "review", "sessions", "events",
    "fixtures", "demo", "result", "export", "retry",
    "agent", "extract", "chat", "steer", "follow-up", "abort",
  ].includes(command)) {
    throw new CliError("UNKNOWN_COMMAND", `Unknown command: ${command}`, 2);
  }

  const parsed = {
    command,
    target: undefined,
    json: false,
    noBrowser: false,
    dataRoot: undefined,
    port: DEFAULT_VIEWER_PORT,
    reviewerPort: 8767,
    runtimePort: validPort(env.CHEMRXN_PI_PORT ?? "4310"),
    runtimeSession: undefined,
    userId: env.CHEMRXN_USER_ID,
    after: 0,
    noReviewer: false,
    provider: env.CHEMRXN_MODEL_PROVIDER,
    providerExplicit: false,
    modelId: env.CHEMRXN_MODEL_ID,
    modelExplicit: false,
    thinkingLevel: validThinkingLevel(env.CHEMRXN_PI_THINKING ?? DEFAULT_PI_THINKING),
    thinkingExplicit: false,
    contextMode: "figure",
    contextExplicit: false,
    message: undefined,
    piAuth: env.CHEMRXN_AUTH_MODE === "pi_auth",
    piAuthExplicit: false,
    format: "v2",
    formatExplicit: false,
    output: undefined,
    image: undefined,
    perception: undefined,
    collection: "external-agent",
    title: undefined,
  };
  while (args.length) {
    const arg = args.shift();
    if (arg === "--json") parsed.json = true;
    else if (arg === "--no-browser") parsed.noBrowser = true;
    else if (arg === "--no-reviewer") parsed.noReviewer = true;
    else if (arg === "--data-root") parsed.dataRoot = requiredValue(arg, args);
    else if (arg === "--port") parsed.port = validPort(requiredValue(arg, args));
    else if (arg === "--reviewer-port") parsed.reviewerPort = validPort(requiredValue(arg, args));
    else if (arg === "--runtime-port") parsed.runtimePort = validPort(requiredValue(arg, args));
    else if (arg === "--runtime-session") parsed.runtimeSession = requiredValue(arg, args);
    else if (arg === "--user-id") parsed.userId = requiredValue(arg, args);
    else if (arg === "--after") parsed.after = nonNegativeInteger(requiredValue(arg, args), arg);
    else if (arg === "--provider") {
      parsed.provider = requiredValue(arg, args);
      parsed.providerExplicit = true;
    }
    else if (arg === "--model") {
      parsed.modelId = requiredValue(arg, args);
      parsed.modelExplicit = true;
    }
    else if (arg === "--thinking") {
      parsed.thinkingLevel = validThinkingLevel(requiredValue(arg, args));
      parsed.thinkingExplicit = true;
    }
    else if (arg === "--context") {
      parsed.contextMode = validContext(requiredValue(arg, args));
      parsed.contextExplicit = true;
    }
    else if (arg === "--message") parsed.message = requiredValue(arg, args);
    else if (arg === "--format") {
      parsed.format = validExportFormat(requiredValue(arg, args));
      parsed.formatExplicit = true;
    }
    else if (arg === "--output") parsed.output = requiredOutput(args);
    else if (arg === "--image") parsed.image = requiredValue(arg, args);
    else if (arg === "--perception") parsed.perception = requiredValue(arg, args);
    else if (arg === "--collection") parsed.collection = safeCollection(requiredValue(arg, args));
    else if (arg === "--title") parsed.title = requiredValue(arg, args);
    else if (arg === "--pi-auth") {
      parsed.piAuth = true;
      parsed.piAuthExplicit = true;
    }
    else if (arg?.startsWith("-")) throw new CliError("INVALID_ARGUMENT", `Unknown option: ${arg}`, 2);
    else if (!parsed.target) parsed.target = arg;
    else throw new CliError("INVALID_ARGUMENT", `Unexpected argument: ${arg}`, 2);
  }
  if (parsed.target && ![
    "open", "review", "events", "demo", "result", "export", "retry",
    "agent", "extract", "chat", "steer", "follow-up", "abort",
  ].includes(command)) {
    throw new CliError("INVALID_ARGUMENT", `${command} does not accept a positional argument`, 2);
  }
  if (parsed.runtimeSession && command !== "open") {
    throw new CliError("INVALID_ARGUMENT", "--runtime-session is only valid with open", 2);
  }
  if (command === "events" && !parsed.target) {
    throw new CliError("INVALID_ARGUMENT", "events requires a session ID", 2);
  }
  if (["review", "demo", "result", "export", "retry"].includes(command) && !parsed.target) {
    throw new CliError("INVALID_ARGUMENT", `${command} requires a target`, 2);
  }
  if (["agent", "extract", "chat", "steer", "follow-up", "abort"].includes(command) && !parsed.target) {
    throw new CliError("INVALID_ARGUMENT", `${command} requires a target`, 2);
  }
  if (["chat", "steer", "follow-up"].includes(command) && !parsed.message) {
    throw new CliError("INVALID_ARGUMENT", `${command} requires --message`, 2);
  }
  if (command === "demo" && !SEALED_REPLAY_FIXTURES.has(parsed.target)) {
    throw new CliError(
      "INVALID_ARGUMENT",
      `Unknown sealed demo fixture: ${parsed.target}`,
      2,
    );
  }
  if (parsed.piAuth && !["agent", "extract", "open"].includes(command)) {
    throw new CliError("INVALID_ARGUMENT", "--pi-auth is only valid with agent, extract, or an input passed to open", 2);
  }
  if (parsed.thinkingExplicit && !["agent", "extract", "open"].includes(command)) {
    throw new CliError("INVALID_ARGUMENT", "--thinking is only valid with agent, extract, or an input passed to open", 2);
  }
  if ((parsed.output || parsed.formatExplicit) && command !== "export") {
    throw new CliError("INVALID_ARGUMENT", "--format and --output are only valid with export", 2);
  }
  if ((parsed.image || parsed.perception || parsed.title || parsed.collection !== "external-agent") && command !== "review") {
    throw new CliError("INVALID_ARGUMENT", "--image, --perception, --collection, and --title are only valid with review", 2);
  }
  if (command === "export" && parsed.format === "rxn" && parsed.output === "-") {
    throw new CliError("INVALID_ARGUMENT", "Binary RXN ZIP export requires --output FILE", 2);
  }
  return parsed;
}

export async function resolveRepo(env = process.env) {
  const explicit = env.CHEMRXN_REPO ? path.resolve(env.CHEMRXN_REPO) : null;
  const repositoryFromPackage = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const candidates = [explicit, explicit && path.join(explicit, "RxnMiner"), repositoryFromPackage].filter(Boolean);
  for (const candidate of candidates) {
    if (await exists(path.join(candidate, "harness/scripts/local_workbench.sh"))) return candidate;
  }
  throw new CliError(
    "REPOSITORY_NOT_FOUND",
    "Cannot locate the RxnMiner workbench. Set CHEMRXN_REPO to a repository checkout.",
    2,
  );
}

export async function doctor(repo, io = defaultIo(), options = {}) {
  const checks = [];
  const [nodeMajor = 0, nodeMinor = 0] = process.versions.node
    .split(".")
    .map((part) => Number.parseInt(part, 10));
  const supportedNode = nodeMajor > 22 || (nodeMajor === 22 && nodeMinor >= 19);
  checks.push(check(
    "node",
    supportedNode,
    process.version,
    "Install Node.js >= 22.19 (required by the embedded Pi runtime).",
  ));

  const git = commandVersion(io, "git", ["--version"]);
  checks.push(check(
    "git",
    git.ok,
    git.detail,
    "Install Git before using the private git+ssh package spec.",
  ));
  const gitLfs = commandVersion(io, "git", ["lfs", "version"]);
  checks.push(check(
    "git-lfs",
    gitLfs.ok,
    gitLfs.detail,
    "Install Git LFS, run `git lfs install`, then reinstall the pinned private Git package.",
  ));

  const installIdentity = typeof io.installedCommitIdentity === "function"
    ? await io.installedCommitIdentity(repo)
    : await resolveInstalledCommitIdentity(repo, io);
  checks.push({
    ...check(
      "installed-commit",
      Boolean(installIdentity.commit),
      installIdentity.commit
        ? `${installIdentity.commit} · ${installIdentity.source} · signature/authentication not assessed`
        : "full installed commit SHA unavailable",
      installIdentity.recommendation,
    ),
    required: false,
  });

  const java = findJava(io);
  checks.push(check(
    "java-opsin",
    Boolean(java),
    java?.detail ?? "not found",
    "Install a Java 8+ runtime for offline OPSIN, or set OPSIN_JAVA_BIN to its java executable.",
  ));

  const python = findPython(io, options.dataRoot);
  checks.push(check(
    "python",
    Boolean(python),
    python?.detail ?? "not found",
    "Install Python 3.11 or 3.12, or set CHEMRXN_VIEWER_PY.",
  ));

  if (python?.path) {
    const modules = run(io, python.path, [
      "-c",
      "import fitz, fastapi, uvicorn, PIL; print('fitz fastapi uvicorn PIL')",
    ]);
    checks.push(check(
      "viewer-python-dependencies",
      modules.status === 0,
      modules.status === 0 ? modules.stdout.trim() : modules.stderr.trim() || "missing",
      `${python.path} -m pip install pymupdf==1.26.5 fastapi uvicorn pillow`,
    ));
    const rdkit = run(io, python.path, ["-c", "import rdkit; print(rdkit.__version__)"]);
    checks.push({
      ...check(
        "rdkit",
        rdkit.status === 0,
        rdkit.status === 0 ? rdkit.stdout.trim() : "not importable",
        "Install RDKit for molecule rendering; placeholders are used otherwise.",
      ),
      required: false,
    });
    const chemistryMcp = checkChemistryMcp(repo, python.path, io);
    checks.push(check(
      "chemistry-mcp",
      chemistryMcp.ok,
      chemistryMcp.detail,
      "Run `chemrxn setup` again, then inspect the chem-extract MCP error above.",
    ));
  }

  checks.push(fileCheck(repo, "viewer-launcher", "harness/scripts/local_workbench.sh"));
  checks.push(fileCheck(repo, "web-workspace", "harness/service/harness_index.html"));
  checks.push(fileCheck(repo, "shared-bbox-renderer", "reviewer/chem_reviewer/web/components/BBoxImagePanel.js"));
  checks.push(fileCheck(repo, "ketcher", "reviewer/vendor/ketcher/index.html"));
  checks.push(fileCheck(repo, "rdkit-wasm", "reviewer/chem_reviewer/web/lib/RDKit_minimal.wasm"));
  checks.push(fileCheck(repo, "pi-runtime-foundation", "runtime/pi-service/package.json"));
  const compiledSkills = path.join(repo, "runtime/pi-service/dist/src/skills.js");
  if (readable(compiledSkills)) {
    const compiledSkillCheck = run(io, process.execPath, [
      "--input-type=module",
      "-e",
      "import {pathToFileURL} from 'node:url'; const m=await import(pathToFileURL(process.argv[1]).href); const rows=await m.loadPinnedSkillManifest(process.argv[2]); console.log(rows.map(x=>x.name).join(' '));",
      compiledSkills,
      repo,
    ]);
    checks.push(check(
      "compiled-skill-pins",
      compiledSkillCheck.status === 0,
      compiledSkillCheck.status === 0
        ? compiledSkillCheck.stdout.trim()
        : compiledSkillCheck.stderr.trim() || "compiled runtime does not match repository Skills",
      "Restart with `chemrxn stop && chemrxn start`; the launcher rebuilds the Pi runtime before serving.",
    ));
  } else {
    checks.push(check(
      "compiled-skill-pins",
      true,
      "runtime not built yet; `chemrxn start` will build and validate it",
      "Run `chemrxn start`.",
    ));
  }
  checks.push(fileCheck(repo, "figure-skill", "figure_skills/skills/reaction-extract/SKILL.md"));
  const installIntegrity = await inspectInstallIntegrity(repo, io);
  checks.push(...installIntegrity.checks);
  const authPath = localPiAuthPath(options, io);
  checks.push({
    ...check(
      "pi-auth-profile",
      readable(authPath),
      readable(authPath)
        ? "profile file present (run `chemrxn auth-status` to validate openai-codex OAuth)"
        : "not configured",
      "Run `chemrxn login`, or use CHEMRXN_MODEL_API_KEY.",
    ),
    required: false,
  });

  const blockers = checks
    .filter((item) => item.required !== false && !item.ok)
    .map((item) => `${item.name}: ${item.detail}`);
  return {
    ok: checks.every((item) => item.ok || item.required === false),
    mode: "installable-local-preview",
    extractionBackend: "figure_live_ready",
    interactivePiBackend: "local_api_key_or_pi_auth_ready",
    sealedReplayBackend: "ready",
    distribution: "private-git-spec",
    repo,
    installIdentity,
    checks,
    blockers,
    installIntegrity: installIntegritySummary(installIntegrity),
    operationalPrerequisites: [
      "System Python/Java discovery is best-effort; pinned and hash-locked Python/JVM acquisition is not implemented",
    ],
    skillPolicy: "repository-bundled and SHA-256 pinned; community skills are not loaded at runtime",
  };
}

export async function resolveInstalledCommitIdentity(repo, io = defaultIo()) {
  const recommendation = "Prefer a full commit SHA or an owner-managed protected, signed release tag; doctor reports the resolved commit but does not verify tag signatures or transport authentication.";
  const packageRoot = path.resolve(repo);
  let current = packageRoot;
  for (let depth = 0; depth < 5; depth += 1) {
    if (path.basename(current) === "node_modules") {
      const relativePackage = path.relative(current, packageRoot).split(path.sep).join("/");
      const lockPath = path.join(current, ".package-lock.json");
      try {
        const lock = JSON.parse(await readFile(lockPath, "utf8"));
        const resolved = lock.packages?.[`node_modules/${relativePackage}`]?.resolved;
        const commit = fullCommitFromGitResolution(resolved);
        if (commit) {
          return {
            commit,
            source: "npm hidden lockfile",
            immutable: true,
            cryptographicAuthentication: "not-assessed",
            recommendation,
          };
        }
      } catch {
        // A checkout or older npm installation may not have a hidden lockfile.
      }
      break;
    }
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }

  const checkout = run(io, "git", ["-C", packageRoot, "rev-parse", "--verify", "HEAD^{commit}"]);
  const commit = checkout.status === 0 && fullGitCommit(checkout.stdout.trim())
    ? checkout.stdout.trim().toLowerCase()
    : null;
  return {
    commit,
    source: commit ? "Git checkout HEAD" : "unavailable",
    immutable: Boolean(commit),
    cryptographicAuthentication: "not-assessed",
    recommendation,
  };
}

function fullCommitFromGitResolution(value) {
  if (typeof value !== "string") return null;
  const separator = value.lastIndexOf("#");
  if (separator < 0) return null;
  let fragment;
  try {
    fragment = decodeURIComponent(value.slice(separator + 1));
  } catch {
    return null;
  }
  return fullGitCommit(fragment) ? fragment.toLowerCase() : null;
}

function fullGitCommit(value) {
  return /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/i.test(value);
}

export async function setupLocalPython(repo, options, io = defaultIo()) {
  const installIntegrity = await inspectInstallIntegrity(repo, io);
  if (!installIntegrity.ok) {
    throw new CliError(
      "PACKAGE_INTEGRITY_FAILED",
      formatInstallIntegrityFailures(installIntegrity),
      4,
    );
  }
  const dataRoot = localDataRoot(options, io);
  const environment = path.join(dataRoot, "python");
  const python = findPython(io, dataRoot);
  if (!python?.path) {
    throw new CliError(
      "PYTHON_NOT_FOUND",
      "Python 3.11 or 3.12 is required to create the local chemistry environment.",
      3,
    );
  }
  const java = findJava(io);
  if (!java?.path) {
    throw new CliError(
      "JAVA_NOT_FOUND",
      "Java 8+ is required by the offline OPSIN name-to-structure sidecar. Install OpenJDK or set OPSIN_JAVA_BIN.",
      3,
    );
  }
  const requirements = path.join(repo, "harness", "requirements-local.txt");
  if (!await exists(requirements)) {
    throw new CliError("PACKAGE_INCOMPLETE", "Local Python requirements are missing.", 4);
  }
  await mkdir(dataRoot, { recursive: true });
  const created = run(io, python.path, ["-m", "venv", environment], { stdio: "inherit" });
  if (created.status !== 0) {
    throw new CliError(
      "PYTHON_SETUP_FAILED",
      `Could not create the local environment at ${environment}.`,
      4,
    );
  }
  const environmentPython = process.platform === "win32"
    ? path.join(environment, "Scripts", "python.exe")
    : path.join(environment, "bin", "python");
  const installed = run(io, environmentPython, [
    "-m", "pip", "install", "--disable-pip-version-check", "-r", requirements,
  ], { stdio: "inherit" });
  if (installed.status !== 0) {
    throw new CliError(
      "PYTHON_SETUP_FAILED",
      `Dependency installation failed. The partial environment remains at ${environment} for inspection.`,
      4,
    );
  }
  // Import native chemistry/PDF modules once during the explicit setup step.
  // A fresh macOS venv can spend over a minute on its first native import;
  // doing that inside the runtime's bounded readiness probe makes an otherwise
  // valid first launch look disconnected until the user restarts.
  const warmed = run(io, environmentPython, [
    "-c",
    "import sys, fitz, requests; from PIL import Image; from rdkit import Chem; "
      + "sys.path.insert(0, sys.argv[1]); "
      + "from chem_validate import opsin_name_to_smiles; "
      + "raise SystemExit(0 if sys.version_info >= (3, 9) "
      + "and opsin_name_to_smiles('ethanol') else 1)",
    path.join(repo, "harness", "tools_product"),
  ], {
    stdio: "inherit",
    env: {
      ...io.env,
      ...(path.isAbsolute(java.path) ? { OPSIN_JAVA_BIN: java.path } : {}),
    },
  });
  if (warmed.status !== 0) {
    throw new CliError(
      "PYTHON_SETUP_FAILED",
      `Chemistry dependency preflight failed in ${environment}.`,
      4,
    );
  }
  io.stdout([
    "Local chemistry environment ready.",
    `python: ${environmentPython}`,
    `java: ${java.detail}`,
    `data root: ${dataRoot}`,
    "Next: chemrxn doctor",
  ].join("\n") + "\n");
  return 0;
}

export async function loginWithPi(options, io = defaultIo()) {
  const authPath = localPiAuthPath(options, io);
  const authDir = path.dirname(authPath);
  mkdirSyncSafe(authDir);
  const pi = bundledPiCli(io);
  io.stdout([
    "Opening the Pi CLI bundled with ChemRxn in this local auth profile.",
    "Inside Pi, run: /login",
    "Choose: OpenAI (ChatGPT Plus/Pro)",
    "Exit Pi after login; ChemRxn will reuse only Pi's stored credential interface.",
    `profile: ${authDir}`,
    "",
  ].join("\n"));
  const result = io.spawnSync(pi.command, pi.args, {
    stdio: "inherit",
    env: {
      ...io.env,
      PI_CODING_AGENT_DIR: authDir,
    },
  });
  if (result.error) {
    throw new CliError(
      "PI_CLI_NOT_FOUND",
      "The bundled Pi CLI was not found. Reinstall @chemrxn/cli with npm.",
      4,
    );
  }
  if ((result.status ?? 1) !== 0) return result.status ?? 1;
  const credentials = await piCredentialSummary(authPath, io);
  const subscription = codexSubscriptionCredential(credentials);
  if (!subscription) {
    throw new CliError(
      "PI_LOGIN_INCOMPLETE",
      "Pi exited without an openai-codex OAuth credential in this ChemRxn profile. Run `chemrxn login` again, complete /login with OpenAI (ChatGPT Plus/Pro), then exit.",
      4,
    );
  }
  io.stdout(`Pi subscription profile configured: ${subscription.providerId} (${subscription.type})\n`);
  return 0;
}

function bundledPiCli(io) {
  if (io.env.CHEMRXN_PI_CLI) {
    return { command: path.resolve(io.env.CHEMRXN_PI_CLI), args: [] };
  }
  try {
    const packageEntry = fileURLToPath(import.meta.resolve("@earendil-works/pi-coding-agent"));
    const cliPath = path.join(path.dirname(packageEntry), "cli.js");
    accessSync(cliPath, fsConstants.R_OK);
    return {
      command: process.execPath,
      args: [cliPath],
    };
  } catch {
    throw new CliError(
      "PI_CLI_NOT_FOUND",
      "The Pi CLI bundled with ChemRxn is missing or unreadable. Reinstall @chemrxn/cli; ChemRxn will not execute a PATH fallback.",
      4,
    );
  }
}

export async function showPiAuthStatus(options, io = defaultIo()) {
  const authPath = localPiAuthPath(options, io);
  const credentials = await piCredentialSummary(authPath, io);
  const subscription = codexSubscriptionCredential(credentials);
  const result = {
    configured: Boolean(subscription),
    requiredProvider: "openai-codex",
    requiredType: "oauth",
    networkVerified: false,
    credentials,
    profile: path.dirname(authPath),
    secretsExposed: false,
  };
  io.stdout(options.json
    ? `${JSON.stringify(result, null, 2)}\n`
    : subscription
      ? `Pi subscription profile configured: ${subscription.providerId} (${subscription.type}); usability is verified when the local Pi session starts.\n`
      : credentials.length
        ? "Pi subscription auth is not configured for openai-codex OAuth. Run `chemrxn login` and choose OpenAI (ChatGPT Plus/Pro).\n"
        : "Pi auth not configured. Run `chemrxn login`.\n");
  return subscription ? 0 : 1;
}

export async function logoutPiAuth(options, io = defaultIo()) {
  const authPath = localPiAuthPath(options, io);
  if (typeof io.piLogout === "function") {
    await io.piLogout(authPath, "openai-codex");
  } else {
    const { ModelRuntime } = await import("@earendil-works/pi-coding-agent");
    const runtime = await ModelRuntime.create({ authPath, allowModelNetwork: false });
    await runtime.logout("openai-codex");
  }
  const subscription = codexSubscriptionCredential(await piCredentialSummary(authPath, io));
  if (subscription) {
    throw new CliError(
      "PI_LOGOUT_FAILED",
      "The openai-codex OAuth credential is still present in the ChemRxn profile.",
      4,
    );
  }
  io.stdout("Pi subscription credential removed from the ChemRxn profile.\n");
  return 0;
}

async function piCredentialSummary(authPath, io) {
  if (typeof io.piCredentials === "function") {
    return structuredClone(await io.piCredentials(authPath));
  }
  const { ModelRuntime } = await import("@earendil-works/pi-coding-agent");
  const runtime = await ModelRuntime.create({ authPath, allowModelNetwork: false });
  return (await runtime.listCredentials()).map((credential) => ({
    providerId: credential.providerId,
    type: credential.type,
  }));
}

function codexSubscriptionCredential(credentials) {
  return credentials.find((credential) =>
    credential?.providerId === "openai-codex" && credential?.type === "oauth") || null;
}

export async function openWorkspace(repo, options, io = defaultIo()) {
  if (options.target && await looksLikeInput(options.target)) {
    if (options.runtimeSession) {
      throw new CliError(
        "INVALID_ARGUMENT",
        "--runtime-session cannot be combined with an input path; omit it to create a new extraction, or pass an existing job ID to attach the session.",
        2,
      );
    }
    return createLiveExtraction(repo, options, io);
  }
  if (
    options.si
    || options.piAuthExplicit
    || options.providerExplicit
    || options.modelExplicit
    || options.thinkingExplicit
    || options.message
  ) {
    throw new CliError(
      "INVALID_ARGUMENT",
      "Extraction options on `open` require an image input path.",
      2,
    );
  }

  if (!await viewerHealthy(options.port, io)) {
    const status = runViewerLauncher(repo, { ...options, command: "start", noBrowser: true }, io);
    if (status !== 0) return status;
  }

  const url = workspaceUrl(options.target, options.port, options.runtimeSession);
  io.stdout(`${url}\n`);
  if (!options.noBrowser) launchBrowser(url, io);
  return 0;
}

export async function reviewExternalResult(repo, options, io = defaultIo()) {
  const target = path.resolve(options.target);
  if (!await exists(target)) {
    throw new CliError("RESULT_NOT_FOUND", `External result does not exist: ${target}`, 2);
  }
  if (!options.image) throw new CliError("SOURCE_REQUIRED", "A prediction requires --image FIGURE", 2);
  const dataRoot = localDataRoot(options, io);
  const python = findPython(io, dataRoot);
  if (!python?.path) {
    throw new CliError("PYTHON_NOT_FOUND", "Run `chemrxn setup` before importing an external result.", 3);
  }
  const figureImport = true;
  const args = [path.join(repo, "harness", "scripts", "import_figure_result.py")];
    const image = path.resolve(options.image);
    if (!await exists(image)) throw new CliError("INPUT_NOT_FOUND", `Figure image does not exist: ${image}`, 2);
    args.push("--image", image, "--pred", target);
    if (options.perception) {
      const perception = path.resolve(options.perception);
      if (!await exists(perception)) {
        throw new CliError("INPUT_NOT_FOUND", `MolParser evidence does not exist: ${perception}`, 2);
      }
      args.push("--molparser", perception);
    }
  args.push("--data-root", dataRoot, "--collection", options.collection);
  if (options.title) args.push("--title", options.title);
  const imported = run(io, python.path, args, { stdio: figureImport ? "pipe" : "inherit" });
  if (imported.status !== 0) {
    throw new CliError("RESULT_IMPORT_FAILED", "The GT-free result importer rejected the external artifact.", 4);
  }
  if (!await viewerHealthy(options.port, io)) {
    const status = runViewerLauncher(repo, { ...options, command: "start", noBrowser: true }, io);
    if (status !== 0) return status;
  }
  const importedJobId = await latestCollectionJobId(dataRoot, options.collection);
  let reviewerSlug;
  if (figureImport && typeof imported.stdout === "string") {
    try {
      reviewerSlug = JSON.parse(imported.stdout.trim().split(/\r?\n/).filter(Boolean).at(-1))?.reviewer_slug;
    } catch {
      // The collection index remains the secondary persisted source of imported identity.
    }
  }
  const url = figureImport
    ? reviewerSlug
      ? `http://127.0.0.1:${validPort(options.reviewerPort)}/static/#/${encodeURIComponent(reviewerSlug)}/0`
      : `http://127.0.0.1:${validPort(options.reviewerPort)}/static/`
    : workspaceUrl(importedJobId, options.port);
  io.stdout(`Imported into ${options.collection}.\n${url}\n`);
  if (!options.noBrowser) launchBrowser(url, io);
  return 0;
}

async function latestCollectionJobId(dataRoot, collection) {
  const index = path.join(dataRoot, "jobs", "_collections", `${collection}.jsonl`);
  try {
    const lines = (await readFile(index, "utf8")).split(/\r?\n/).filter(Boolean);
    for (let offset = lines.length - 1; offset >= 0; offset -= 1) {
      const row = JSON.parse(lines[offset]);
      if (typeof row?.id === "string" && row.id && !row.id.includes("..")) return row.id;
    }
  } catch {
    // The importer succeeded, so the collection root remains an honest fallback if its optional
    // navigation index cannot be decoded.
  }
  return undefined;
}

export function workspaceUrl(jobId, port = DEFAULT_VIEWER_PORT, runtimeSession) {
  const base = `http://127.0.0.1:${validPort(port)}/`;
  const query = [];
  if (jobId) query.push(`job=${encodeURIComponent(jobId)}`);
  if (runtimeSession) query.push(`runtime_session=${encodeURIComponent(runtimeSession)}`);
  return query.length ? `${base}?${query.join("&")}` : base;
}

export function runViewerLauncher(repo, options, io = defaultIo()) {
  const script = path.join(repo, "harness/scripts/local_workbench.sh");
  const args = [script, options.command];
  if (options.dataRoot) args.push("--data-root", path.resolve(options.dataRoot));
  if (options.port) args.push("--port", String(options.port));
  if (options.reviewerPort) args.push("--reviewer-port", String(options.reviewerPort));
  if (options.runtimePort) args.push("--runtime-port", String(options.runtimePort));
  if (options.noReviewer) args.push("--no-reviewer");
  if (options.command === "start" && !options.noBrowser) args.push("--open");
  const result = run(io, "bash", args, { stdio: "inherit" });
  return result.status ?? 1;
}

export async function listRuntimeSessions(options, io = defaultIo()) {
  const response = await runtimeRequest("/v1/sessions", options, io);
  const sessions = Array.isArray(response?.sessions) ? response.sessions : [];
  if (options.json) {
    io.stdout(`${JSON.stringify({ sessions }, null, 2)}\n`);
  } else if (!sessions.length) {
    io.stdout("No runtime sessions.\n");
  } else {
    for (const session of sessions) {
      io.stdout([
        session.id,
        session.status ?? "unknown",
        session.task ?? "unknown",
        session.jobId ?? "unknown-job",
        session.updatedAt ?? "",
      ].join("\t") + "\n");
    }
  }
  return 0;
}

export async function listRuntimeEvents(options, io = defaultIo()) {
  const sessionId = encodeURIComponent(options.target);
  const response = await runtimeRequest(
    `/v1/sessions/${sessionId}/events?after=${options.after}`,
    options,
    io,
  );
  const events = Array.isArray(response?.events) ? response.events : [];
  if (options.json) {
    io.stdout(`${JSON.stringify({ events }, null, 2)}\n`);
  } else if (!events.length) {
    io.stdout(`No events after cursor ${options.after}.\n`);
  } else {
    for (const event of events) {
      io.stdout([
        event.seq,
        event.at ?? "",
        event.type ?? "unknown",
        JSON.stringify(event.data ?? null),
      ].join("\t") + "\n");
    }
  }
  return 0;
}

export async function listReplayFixtures(options, io = defaultIo()) {
  const response = await runtimeRequest("/v1/replay-jobs/fixtures", options, io);
  const fixtures = Array.isArray(response?.fixtures) ? response.fixtures : [];
  if (options.json) {
    io.stdout(`${JSON.stringify({ fixtures }, null, 2)}\n`);
  } else {
    for (const fixture of fixtures) {
      io.stdout([
        fixture.fixture,
        fixture.contextMode,
        fixture.label,
        "sealed replay",
      ].join("\t") + "\n");
    }
  }
  return 0;
}

export async function createReplayDemo(repo, options, io = defaultIo()) {
  if (!await viewerHealthy(options.port, io)) {
    const status = runViewerLauncher(repo, { ...options, command: "start", noBrowser: true }, io);
    if (status !== 0) return status;
  }
  const userId = options.userId || io.env.CHEMRXN_USER_ID || "local-user";
  const response = await runtimeRequest("/v1/replay-jobs", options, io, {
    method: "POST",
    timeoutMs: 30_000,
    body: { userId, fixture: options.target },
  });
  return openRuntimeResponse(response, options, io);
}

export async function getRuntimeResult(options, io = defaultIo()) {
  const response = await runtimeRequest(
    `/v1/sessions/${encodeURIComponent(options.target)}/result`,
    options,
    io,
  );
  if (options.json) {
    io.stdout(`${JSON.stringify(response, null, 2)}\n`);
  } else {
    io.stdout([
      response.sessionId,
      response.terminalStatus,
      response.contextMode,
      response.domainContract,
      response.source?.fixture,
      response.source?.reconstructed ? "sealed replay" : "live",
    ].join("\t") + "\n");
  }
  return 0;
}

export async function exportExtraction(repo, options, io = defaultIo()) {
  if (!await viewerHealthy(options.port, io)) {
    const status = runViewerLauncher(repo, { ...options, command: "start", noBrowser: true }, io);
    if (status !== 0) return status;
  }
  const encodedTarget = encodeURIComponent(options.target);
  const registration = await viewerFetch(
    `/api/runtime/sessions/${encodedTarget}/reviewer`,
    options,
    io,
    { method: "POST", allowFailure: true },
  );
  let jobId = options.target;
  if (registration.response.ok) {
    const body = await registration.response.json();
    if (!body?.jobId) {
      throw new CliError(
        "INVALID_VIEWER_RESPONSE",
        "Visual workspace registration returned no job ID",
        4,
      );
    }
    jobId = body.jobId;
  } else if (registration.response.status === 503) {
    const existingJob = await viewerFetch(
      `/api/jobs/${encodedTarget}`,
      options,
      io,
      { allowFailure: true },
    );
    if (!existingJob.response.ok) {
      let message = "Interactive Pi backend not connected";
      try {
        const body = await registration.response.json();
        message = body?.detail || body?.error?.message || message;
      } catch {}
      throw new CliError("EXPORT_FAILED", message, 4);
    }
  } else if (registration.response.status !== 404) {
    let message = `Visual workspace registration returned HTTP ${registration.response.status}`;
    try {
      const body = await registration.response.json();
      message = body?.detail || body?.error?.message || message;
    } catch {}
    throw new CliError("EXPORT_FAILED", message, 4);
  }

  const exportPath = options.format === "review"
    ? `/api/jobs/${encodeURIComponent(jobId)}/review/export`
    : `/api/jobs/${encodeURIComponent(jobId)}/export?fmt=${encodeURIComponent(options.format)}`;
  const exported = await viewerFetch(
    exportPath,
    options,
    io,
  );
  const bytes = Buffer.from(await exported.response.arrayBuffer());
  const disposition = exported.response.headers?.get?.("content-disposition") || "";
  const fallbackExtension = {
    v2: "v2.json",
    csv: "reactions.csv",
    report: "report.json",
    rights: "rights.json",
    rxn: "rxn.zip",
    review: "reviewed_changes.json",
  }[options.format];
  const suggested = disposition.match(/filename="([^"]+)"/i)?.[1]
    || `${safeFilename(jobId)}_${fallbackExtension}`;
  if (options.output === "-" && options.format === "rxn") {
    throw new CliError(
      "INVALID_ARGUMENT",
      "Binary RXN ZIP export requires --output FILE instead of stdout",
      2,
    );
  }
  if (options.output === "-") {
    io.stdout(bytes.toString("utf8"));
    return 0;
  }
  const outputPath = path.resolve(options.output || suggested);
  await (io.writeFile || writeFile)(outputPath, bytes, { mode: 0o600 });
  const result = {
    sessionOrJobId: options.target,
    jobId,
    format: options.format,
    output: outputPath,
    byteSize: bytes.byteLength,
  };
  io.stdout(options.json
    ? `${JSON.stringify(result, null, 2)}\n`
    : `${options.format}\t${bytes.byteLength} bytes\t${outputPath}\n`);
  return 0;
}

export async function retryRuntimeSession(repo, options, io = defaultIo()) {
  if (!await viewerHealthy(options.port, io)) {
    const status = runViewerLauncher(repo, { ...options, command: "start", noBrowser: true }, io);
    if (status !== 0) return status;
  }
  const previous = await runtimeRequest(
    `/v1/sessions/${encodeURIComponent(options.target)}`,
    options,
    io,
    { timeoutMs: 5_000 },
  );
  const response = await runtimeRequest(
    `/v1/sessions/${encodeURIComponent(options.target)}/retry`,
    options,
    io,
    {
      method: "POST",
      body: {
        ...(previous?.authMode === "api_key" && io.env.CHEMRXN_MODEL_API_KEY
          ? { apiKey: io.env.CHEMRXN_MODEL_API_KEY } : {}),
      },
      timeoutMs: 30_000,
    },
  );
  return openRuntimeResponse(response, options, io);
}

export async function createLocalAgent(repo, options, io = defaultIo()) {
  if (!await viewerHealthy(options.port, io)) {
    const status = runViewerLauncher(repo, { ...options, command: "start", noBrowser: true }, io);
    if (status !== 0) return status;
  }
  const auth = localModelAuth(options, io);
  if (auth.authMode === "api_key" && !auth.apiKey) {
    throw new CliError(
      "MODEL_AUTH_REQUIRED",
      "Set CHEMRXN_MODEL_API_KEY, or pass --pi-auth after `chemrxn login`.",
      4,
    );
  }
  const provider = options.provider || (auth.authMode === "pi_auth" ? "openai-codex" : undefined);
  const modelId = options.modelId || (auth.authMode === "pi_auth" ? DEFAULT_PI_MODEL : undefined);
  if (!provider || !modelId) {
    throw new CliError(
      "MODEL_REQUIRED",
      "Pass --provider and --model, or set CHEMRXN_MODEL_PROVIDER and CHEMRXN_MODEL_ID.",
      2,
    );
  }
  const userId = options.userId || io.env.CHEMRXN_USER_ID || "local-user";
  const response = await runtimeRequest("/v1/local-agent-sessions", options, io, {
    method: "POST",
    timeoutMs: 30_000,
    body: {
      userId,
      jobId: `local-agent:${options.target}`,
      contextMode: options.contextMode,
      workspaceId: options.target,
      provider,
      modelId,
      thinkingLevel: options.thinkingLevel,
      ...auth,
    },
  });
  const sessionId = response?.session?.id;
  if (!sessionId) throw new CliError("INVALID_RUNTIME_RESPONSE", "Runtime did not return a session ID", 4);
  if (options.message) {
    await runtimeRequest(
      `/v1/sessions/${encodeURIComponent(sessionId)}/messages`,
      options,
      io,
      {
        method: "POST",
        body: { text: options.message, behavior: "prompt" },
      },
    );
  }
  const url = workspaceUrl(undefined, options.port, sessionId);
  io.stdout(options.json
    ? `${JSON.stringify({ ...response, workspaceUrl: url }, null, 2)}\n`
    : `Pi session ${response.session.status}: ${sessionId}\n${url}\n`);
  if (!options.noBrowser) launchBrowser(url, io);
  return 0;
}

export async function createLiveExtraction(repo, options, io = defaultIo()) {
  const primaryPath = path.resolve(options.target);
  const primary = await validatedInput(primaryPath);
  const inferredContext = "figure";
  if (options.contextExplicit && options.contextMode !== inferredContext) {
    throw new CliError(
      "INVALID_ARGUMENT",
      `--context ${options.contextMode} does not match the selected ${inferredContext} inputs`,
      2,
    );
  }
  if (!await viewerHealthy(options.port, io)) {
    const status = runViewerLauncher(repo, { ...options, command: "start", noBrowser: true }, io);
    if (status !== 0) return status;
  }
  const auth = localModelAuth(options, io);
  if (auth.authMode === "api_key" && !auth.apiKey) {
    throw new CliError(
      "MODEL_AUTH_REQUIRED",
      "Set CHEMRXN_MODEL_API_KEY, or pass --pi-auth after `chemrxn login`.",
      4,
    );
  }
  const provider = options.provider || (auth.authMode === "pi_auth" ? "openai-codex" : undefined);
  const modelId = options.modelId || (auth.authMode === "pi_auth" ? DEFAULT_PI_MODEL : undefined);
  if (!provider || !modelId) {
    throw new CliError(
      "MODEL_REQUIRED",
      "Pass --provider and --model, or set CHEMRXN_MODEL_PROVIDER and CHEMRXN_MODEL_ID.",
      2,
    );
  }
  const uploaded = [
    await uploadRuntimeInput(primaryPath, primary.kind, options, io),
  ];
  const userId = options.userId || io.env.CHEMRXN_USER_ID || "local-user";
  const extractionId = randomUUID();
  const response = await runtimeRequest("/v1/local-agent-sessions", options, io, {
    method: "POST",
    timeoutMs: 30_000,
    body: {
      userId,
      jobId: `local-extract:${extractionId}`,
      contextMode: inferredContext,
      workspaceId: `extract-${extractionId}`,
      provider,
      modelId,
      thinkingLevel: options.thinkingLevel,
      ...auth,
      inputs: uploaded.map((artifact) => ({
        artifactId: artifact.id,
        kind: primary.kind,
      })),
      startExtraction: true,
      ...(options.message ? { initialMessage: options.message } : {}),
    },
  });
  const sessionId = response?.session?.id;
  if (!sessionId) {
    throw new CliError("INVALID_RUNTIME_RESPONSE", "Runtime did not return a session ID", 4);
  }
  const url = workspaceUrl(undefined, options.port, sessionId);
  const output = {
    ...response,
    contextMode: inferredContext,
    inputs: uploaded.map(({ id, kind, originalName, sha256, byteSize }) => ({
      id, kind, originalName, sha256, byteSize,
    })),
    workspaceUrl: url,
  };
  if (options.json) {
    io.stdout(`${JSON.stringify(output, null, 2)}\n`);
  } else {
    const dispatch = response?.dispatch;
    const queueLabel = dispatch?.workItem?.state === "pending"
      ? `accepted · queued${Number.isSafeInteger(dispatch.position) ? ` at position ${dispatch.position}` : ""}`
      : dispatch?.workItem?.state === "active"
        ? "accepted · starting"
        : response.session.status;
    io.stdout([
      `Live Figure extraction: ${queueLabel}`,
      `session: ${sessionId}`,
      ...uploaded.map((artifact) =>
        `${artifact.kind}: ${artifact.originalName} · ${artifact.sha256.slice(0, 12)} · ${artifact.byteSize} bytes`),
      url,
    ].join("\n") + "\n");
  }
  if (!options.noBrowser) launchBrowser(url, io);
  return response.session.status === "failed" ? 4 : 0;
}

async function uploadRuntimeInput(filePath, kind, options, io) {
  const credentials = await runtimeCredentials(options, io);
  const token = credentials.token;
  const userId = options.userId || io.env.CHEMRXN_USER_ID || credentials.userId;
  if (!token || !userId) {
    throw new CliError(
      "RUNTIME_AUTH_REQUIRED",
      "Set CHEMRXN_PI_INTERNAL_TOKEN and CHEMRXN_USER_ID for the local runtime.",
      4,
    );
  }
  let response;
  try {
    response = await io.fetch(
      `http://127.0.0.1:${validPort(options.runtimePort)}/v1/input-artifacts`,
      {
        method: "POST",
        headers: {
          authorization: `Bearer ${token}`,
          "x-chemrxn-user-id": userId,
          "x-chemrxn-input-kind": kind,
          "x-chemrxn-file-name": encodeURIComponent(path.basename(filePath)),
          "content-type": inputMediaType(filePath),
        },
        body: io.createReadStream(filePath),
        duplex: "half",
        signal: AbortSignal.timeout(5 * 60_000),
      },
    );
  } catch (error) {
    throw new CliError(
      "INPUT_UPLOAD_FAILED",
      error instanceof Error ? error.message : "Input upload failed",
      4,
    );
  }
  const body = await response.json();
  if (!response.ok) {
    throw new CliError(
      body?.error?.code ?? "INPUT_UPLOAD_FAILED",
      body?.error?.message ?? `Runtime returned HTTP ${response.status}`,
      4,
    );
  }
  if (!body?.artifact?.id) {
    throw new CliError("INVALID_RUNTIME_RESPONSE", "Runtime did not return an input artifact", 4);
  }
  return body.artifact;
}

async function validatedInput(filePath) {
  let info;
  try {
    info = await stat(filePath);
  } catch {
    throw new CliError("INPUT_NOT_FOUND", `Input file does not exist: ${filePath}`, 2);
  }
  if (!info.isFile()) throw new CliError("INVALID_INPUT", `Input is not a file: ${filePath}`, 2);
  const extension = path.extname(filePath).toLowerCase();
  if (!SUPPORTED_INPUT_EXTENSIONS.has(extension)) {
    throw new CliError(
      "INVALID_INPUT",
      `Supported inputs are ${[...SUPPORTED_INPUT_EXTENSIONS].join(", ")}`,
      2,
    );
  }
  return { kind: "figure", byteSize: info.size };
}

function inputMediaType(filePath) {
  return ({
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
  })[path.extname(filePath).toLowerCase()] ?? "application/octet-stream";
}

export async function sendAgentMessage(options, io = defaultIo()) {
  const behavior = options.command === "steer"
    ? "steer" : options.command === "follow-up" ? "follow_up" : "prompt";
  const response = await runtimeRequest(
    `/v1/sessions/${encodeURIComponent(options.target)}/messages`,
    options,
    io,
    {
      method: "POST",
      body: { text: options.message, behavior },
    },
  );
  const continuedSessionId = response?.continued ? response?.session?.id : undefined;
  const continuedUrl = continuedSessionId
    ? workspaceUrl(undefined, options.port, continuedSessionId)
    : undefined;
  io.stdout(options.json
    ? `${JSON.stringify(
        continuedUrl ? { ...response, workspaceUrl: continuedUrl } : response,
        null,
        2,
      )}\n`
    : continuedSessionId
      ? `new linked task\t${continuedSessionId}\nsealed parent\t${options.target}\n${continuedUrl}\n`
      : `${response.accepted ? "accepted" : "rejected"}\t${behavior}\t${options.target}\n`);
  return 0;
}

export async function abortAgentSession(options, io = defaultIo()) {
  const response = await runtimeRequest(
    `/v1/sessions/${encodeURIComponent(options.target)}/abort`,
    options,
    io,
    { method: "POST" },
  );
  io.stdout(options.json
    ? `${JSON.stringify(response, null, 2)}\n`
    : `${response.status}\t${options.target}\n`);
  return 0;
}

function openRuntimeResponse(response, options, io) {
  const sessionId = response?.session?.id;
  if (!sessionId) {
    throw new CliError("INVALID_RUNTIME_RESPONSE", "Runtime did not return a session ID", 4);
  }
  const url = workspaceUrl(undefined, options.port, sessionId);
  if (options.json) {
    io.stdout(`${JSON.stringify({ ...response, workspaceUrl: url }, null, 2)}\n`);
  } else {
    const label = response?.retried && response?.backend?.mode === "local_pi_sdk"
      ? "live retry" : "sealed replay";
    io.stdout(`${label}: ${response.session.status}\n${url}\n`);
  }
  if (!options.noBrowser) launchBrowser(url, io);
  return 0;
}

async function runtimeRequest(route, options, io, request = {}) {
  const credentials = await runtimeCredentials(options, io);
  const token = credentials.token;
  const userId = options.userId || io.env.CHEMRXN_USER_ID || credentials.userId;
  if (!token || !userId) {
    throw new CliError(
      "RUNTIME_AUTH_REQUIRED",
      "Set CHEMRXN_PI_INTERNAL_TOKEN and CHEMRXN_USER_ID for the local runtime.",
      4,
    );
  }
  let response;
  try {
    response = await io.fetch(`http://127.0.0.1:${validPort(options.runtimePort)}${route}`, {
      method: request.method ?? "GET",
      headers: {
        authorization: `Bearer ${token}`,
        "x-chemrxn-user-id": userId,
        ...(request.body ? { "content-type": "application/json" } : {}),
      },
      ...(request.body ? { body: JSON.stringify(request.body) } : {}),
      signal: AbortSignal.timeout(request.timeoutMs ?? 2500),
    });
  } catch (error) {
    throw new CliError(
      "RUNTIME_NOT_CONNECTED",
      error instanceof Error ? error.message : "Local Pi runtime is unavailable",
      4,
    );
  }
  const body = await response.json();
  if (!response.ok) {
    throw new CliError(
      body?.error?.code ?? "RUNTIME_REQUEST_FAILED",
      body?.error?.message ?? `Runtime returned HTTP ${response.status}`,
      4,
    );
  }
  return body;
}

async function viewerFetch(route, options, io, request = {}) {
  let response;
  try {
    response = await io.fetch(`http://127.0.0.1:${validPort(options.port)}${route}`, {
      method: request.method ?? "GET",
      signal: AbortSignal.timeout(request.timeoutMs ?? 60_000),
    });
  } catch (error) {
    throw new CliError(
      "VIEWER_NOT_CONNECTED",
      error instanceof Error ? error.message : "Local visual workspace is unavailable",
      4,
    );
  }
  if (!response.ok && !request.allowFailure) {
    let message = `Visual workspace returned HTTP ${response.status}`;
    try {
      const body = await response.json();
      message = body?.detail || body?.error?.message || message;
    } catch {}
    throw new CliError("EXPORT_FAILED", message, 4);
  }
  return { response };
}

async function runtimeCredentials(options, io) {
  const dataRoot = localDataRoot(options, io);
  const readLocal = async () => {
    try {
      return (await readFile(path.join(dataRoot, "run", "runtime.token"), "utf8")).trim();
    } catch {
      return "";
    }
  };
  const localToken = await readLocal();
  if (localToken) {
    return {
      token: localToken,
      userId: io.env.CHEMRXN_USER_ID || "local-user",
    };
  }
  if (io.env.CHEMRXN_PI_INTERNAL_TOKEN) {
    return {
      token: io.env.CHEMRXN_PI_INTERNAL_TOKEN,
      userId: io.env.CHEMRXN_USER_ID || "local-user",
    };
  }
  return { token: "", userId: io.env.CHEMRXN_USER_ID || "local-user" };
}

function localModelAuth(options, io) {
  if (options.piAuth) return { authMode: "pi_auth" };
  return {
    authMode: "api_key",
    ...(io.env.CHEMRXN_MODEL_API_KEY ? { apiKey: io.env.CHEMRXN_MODEL_API_KEY } : {}),
  };
}

function localDataRoot(options, io) {
  return path.resolve(
    options.dataRoot
      || io.env.CHEMRXN_VIEWER_DATA
      || path.join(homedir(), "ChemRxn-viewer-data"),
  );
}

function localPiAuthPath(options, io) {
  return path.resolve(
    io.env.CHEMRXN_PI_AUTH_PATH
      || path.join(localDataRoot(options, io), "pi-auth", "auth.json"),
  );
}

function mkdirSyncSafe(target) {
  try {
    mkdirSync(target, { recursive: true, mode: 0o700 });
  } catch {
    throw new CliError("AUTH_PROFILE_CREATE_FAILED", `Could not create ${target}.`, 4);
  }
}

function readable(target) {
  try {
    accessSync(target, fsConstants.R_OK);
    return true;
  } catch {
    return false;
  }
}

async function viewerHealthy(port, io) {
  try {
    const response = await io.fetch(`http://127.0.0.1:${validPort(port)}/healthz`, {
      signal: AbortSignal.timeout(1500),
    });
    if (!response.ok) return false;
    const body = await response.json();
    return body?.ok === true && body?.ui?.document_mode === true;
  } catch {
    return false;
  }
}

async function looksLikeInput(value) {
  const resolved = path.resolve(value);
  try {
    return (await stat(resolved)).isFile();
  } catch {
    return SUPPORTED_INPUT_EXTENSIONS.has(path.extname(value).toLowerCase());
  }
}

function launchBrowser(url, io) {
  const command = process.platform === "darwin" ? "open" : process.platform === "win32" ? "cmd" : "xdg-open";
  const args = process.platform === "win32" ? ["/c", "start", "", url] : [url];
  const child = io.spawn(command, args, { detached: true, stdio: "ignore" });
  child.once("error", (error) => {
    io.stderr(`chemrxn: BROWSER_OPEN_FAILED: ${error.message}; open ${url} manually\n`);
  });
  child.unref();
}

function findPython(io, dataRoot) {
  const localEnvironment = path.resolve(
    dataRoot
      || io.env.CHEMRXN_VIEWER_DATA
      || path.join(homedir(), "ChemRxn-viewer-data"),
    "python",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
  const candidates = [
    io.env.CHEMRXN_VIEWER_PY,
    localEnvironment,
    "python3",
    "python",
  ].filter(Boolean);
  for (const candidate of candidates) {
    const result = run(io, candidate, [
      "-c",
      "import sys; print(sys.executable); raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 13) else 1)",
    ]);
    if (result.status === 0) {
      return { path: candidate, detail: result.stdout.trim() };
    }
  }
  return null;
}

function findJava(io) {
  const candidates = [
    io.env.OPSIN_JAVA_BIN,
    "/opt/homebrew/opt/openjdk/bin/java",
    "/usr/local/opt/openjdk/bin/java",
    "java",
  ].filter((candidate, index, values) => candidate && values.indexOf(candidate) === index);
  for (const candidate of candidates) {
    const version = commandVersion(io, candidate, ["-version"]);
    if (version.ok) return { path: candidate, detail: `${candidate} · ${version.detail}` };
  }
  return null;
}

function commandVersion(io, command, args) {
  const result = run(io, command, args);
  const detail = (result.stdout || result.stderr || result.error?.message || "").trim();
  return {
    ok: result.status === 0,
    detail: detail.split("\n")[0] || (result.status === 0 ? "available" : "not found"),
  };
}

async function inspectInstallIntegrity(repo, io) {
  try {
    const inspect = typeof io.verifyInstallIntegrity === "function"
      ? io.verifyInstallIntegrity
      : verifyInstallIntegrity;
    return await inspect(repo);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return {
      ok: false,
      schemaVersion: null,
      manifest: "packaging/install-integrity.json",
      assetCount: 0,
      verifiedAssetCount: 0,
      lfsAssetCount: 0,
      sampleAssetCount: 0,
      requiredFileCount: 0,
      checks: [check(
        "install-integrity",
        false,
        detail,
        "Restore the package from the pinned private Git release.",
      )],
      failures: [{ code: "INTEGRITY_CHECK_FAILED", path: "package", detail }],
      assets: [],
    };
  }
}

function installIntegritySummary(report) {
  return {
    ok: report.ok,
    schemaVersion: report.schemaVersion,
    manifest: report.manifest,
    assetCount: report.assetCount,
    verifiedAssetCount: report.verifiedAssetCount,
    lfsAssetCount: report.lfsAssetCount,
    sampleAssetCount: report.sampleAssetCount,
    requiredFileCount: report.requiredFileCount,
    failures: report.failures,
  };
}

function checkChemistryMcp(repo, python, io) {
  const script = path.join(repo, "figure_skills", "scripts", "run_chem_mcp.py");
  const requiredTools = [
    "detect",
    "ocsr_detect",
    "assemble",
    "verify_smiles",
    "render_reactions",
    "reverse_rgroup",
    "fill_rgroup",
    "reverse_assemble",
  ];
  const requests = [
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: { name: "chemrxn-doctor", version: "1" },
      },
    },
    { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} },
    {
      jsonrpc: "2.0",
      id: 3,
      method: "tools/call",
      params: { name: "verify_smiles", arguments: { smiles: "C[C@H](O)Cl" } },
    },
  ];
  const result = run(io, python, [script], {
    input: `${requests.map((request) => JSON.stringify(request)).join("\n")}\n`,
    // A first import of RDKit and the chemistry tool graph can exceed 30 s on
    // a clean reader install. Doctor is a bounded cold-start probe, not an
    // interactive request timeout.
    timeout: 90_000,
    maxBuffer: 8 * 1024 * 1024,
  });
  if (result.status !== 0) {
    return {
      ok: false,
      detail: (result.stderr || result.stdout || "chem-extract MCP failed").trim().slice(-1200),
    };
  }
  try {
    const responses = result.stdout.trim().split("\n").filter(Boolean).map(JSON.parse);
    const initialized = responses.find((response) => response.id === 1);
    const listed = responses.find((response) => response.id === 2);
    const verified = responses.find((response) => response.id === 3);
    const names = new Set((listed?.result?.tools || []).map((tool) => tool.name));
    const missing = requiredTools.filter((name) => !names.has(name));
    const verificationText = verified?.result?.content?.[0]?.text;
    const verification = JSON.parse(verificationText || "{}");
    if (
      initialized?.result?.serverInfo?.name !== "chem-extract"
      || missing.length
      || verification.valid !== true
      || verification.canonical_iso !== "C[C@H](O)Cl"
    ) {
      throw new Error(missing.length
        ? `missing tools: ${missing.join(", ")}`
        : "offline stereochemistry verification failed");
    }
    return {
      ok: true,
      detail: `${requiredTools.length} Pi tools · RDKit strict-stereo offline check passed`,
    };
  } catch (error) {
    return {
      ok: false,
      detail: error instanceof Error ? error.message : "Invalid chem-extract MCP response",
    };
  }
}

function fileCheck(repo, name, relativePath) {
  const absolutePath = path.join(repo, relativePath);
  let ok = false;
  try {
    accessSync(absolutePath, fsConstants.R_OK);
    ok = true;
  } catch {
    ok = false;
  }
  return check(name, ok, ok ? relativePath : `missing: ${relativePath}`, `Restore ${relativePath} from the repository.`);
}

function check(name, ok, detail, fix) {
  return { name, ok, required: true, detail, ...(ok ? {} : { fix }) };
}

function formatDoctor(report) {
  const lines = [
    `ChemRxn doctor — ${report.ok ? "ready for local preview" : "action required"}`,
    `repository: ${report.repo}`,
    `mode: ${report.mode}`,
    `distribution: ${report.distribution}`,
    "",
  ];
  for (const item of report.checks) {
    lines.push(`${item.ok ? "✓" : item.required === false ? "!" : "✗"} ${item.name}: ${item.detail}`);
    if (!item.ok && item.fix) lines.push(`  fix: ${item.fix}`);
  }
  lines.push("", "Backend truth:");
  lines.push(`- extraction: ${report.extractionBackend}`);
  lines.push(`- interactive Pi: ${report.interactivePiBackend}`);
  lines.push(`- sealed replay: ${report.sealedReplayBackend}`);
  lines.push(`- skills: ${report.skillPolicy}`);
  for (const blocker of report.blockers) lines.push(`- ${blocker}`);
  if (!report.blockers.length) lines.push("- no local installation blockers detected");
  if (report.operationalPrerequisites?.length) {
    lines.push("", "Operational prerequisites:");
    for (const prerequisite of report.operationalPrerequisites) lines.push(`- ${prerequisite}`);
  }
  return `${lines.join("\n")}\n`;
}

function helpText() {
  return `ChemRxn local-first product launcher

Usage:
  chemrxn doctor [--json]
  chemrxn setup [--data-root DIR]
  chemrxn login [--data-root DIR]
  chemrxn logout [--data-root DIR]
  chemrxn auth status|logout [--data-root DIR]
  chemrxn auth-status [--json] [--data-root DIR]
  chemrxn start [--no-browser] [--data-root DIR] [--port N] [--reviewer-port N] [--runtime-port N]
  chemrxn status [--data-root DIR] [--port N] [--reviewer-port N] [--runtime-port N]
  chemrxn stop [--data-root DIR] [--port N] [--reviewer-port N] [--runtime-port N]
  chemrxn open <IMAGE> [--pi-auth] [--provider PROVIDER] [--model MODEL] [--thinking LEVEL] [--message TEXT]
  chemrxn open [JOB_ID] [--runtime-session SESSION_ID] [--no-browser] [--data-root DIR] [--port N]
  chemrxn review PRED.v2.json --image FIGURE [--perception MOLPARSER.json] [--collection NAME] [--title TEXT]
  chemrxn jobs [--json] [--runtime-port N] [--user-id USER]
  chemrxn sessions [--json] [--runtime-port N] [--user-id USER]
  chemrxn events SESSION_ID [--after CURSOR] [--json] [--runtime-port N] [--user-id USER]
  chemrxn fixtures [--json]
  chemrxn demo <demo-figure> [--no-browser]
  chemrxn result SESSION_ID [--json]
  chemrxn export SESSION_ID|JOB_ID --format v2|rxn|csv|report|rights|review [--output FILE|-] [--json]
  chemrxn retry SESSION_ID [--no-browser]
  chemrxn extract <IMAGE> [--pi-auth] [--provider PROVIDER] [--model MODEL] [--thinking LEVEL] [--message TEXT]
  chemrxn agent WORKSPACE_ID --context figure [--pi-auth] [--provider PROVIDER] [--model MODEL] [--thinking LEVEL] [--message TEXT]
  chemrxn chat SESSION_ID --message TEXT
  chemrxn ask SESSION_ID --message TEXT
  chemrxn steer SESSION_ID --message TEXT
  chemrxn follow-up SESSION_ID --message TEXT
  chemrxn abort SESSION_ID
  chemrxn cancel SESSION_ID

Current honest boundary:
  Sealed replay for Figure uses the real persisted event and result contracts,
  and is always labeled reconstructed replay rather than live execution.
  Figure file handoff, Pi skill execution, persisted events, validation, and live artifacts are connected.
  Local auth supports either an in-memory API key or Pi's user-owned auth profile.
  Pi-auth uses openai-codex and defaults to gpt-5.6-sol with medium thinking; --model/--thinking or CHEMRXN_MODEL_ID/CHEMRXN_PI_THINKING override them.
`;
}

function requiredValue(option, args) {
  const value = args.shift();
  if (!value || value.startsWith("-")) {
    throw new CliError("INVALID_ARGUMENT", `${option} requires a value`, 2);
  }
  return value;
}

function requiredOutput(args) {
  const value = args.shift();
  if (!value || (value.startsWith("-") && value !== "-")) {
    throw new CliError("INVALID_ARGUMENT", "--output requires a file path or -", 2);
  }
  return value;
}

function validPort(value) {
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new CliError("INVALID_ARGUMENT", `Invalid port: ${value}`, 2);
  }
  return port;
}

function nonNegativeInteger(value, option) {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new CliError("INVALID_ARGUMENT", `${option} requires a non-negative integer`, 2);
  }
  return parsed;
}

function validContext(value) {
  if (!["figure"].includes(value)) {
    throw new CliError("INVALID_ARGUMENT", `Invalid context: ${value}`, 2);
  }
  return value;
}

function validThinkingLevel(value) {
  if (!["off", "minimal", "low", "medium", "high", "xhigh", "max"].includes(value)) {
    throw new CliError(
      "INVALID_ARGUMENT",
      `Invalid thinking level: ${value}; expected off, minimal, low, medium, high, xhigh, or max`,
      2,
    );
  }
  return value;
}

function validExportFormat(value) {
  if (!["v2", "rxn", "csv", "report", "rights", "review"].includes(value)) {
    throw new CliError(
      "INVALID_ARGUMENT",
      `Invalid export format: ${value}; expected v2, rxn, csv, report, rights, or review`,
      2,
    );
  }
  return value;
}

function safeCollection(value) {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(value)) {
    throw new CliError(
      "INVALID_ARGUMENT",
      "--collection must be 1-64 characters using letters, numbers, dot, underscore, or hyphen",
      2,
    );
  }
  return value;
}

function safeFilename(value) {
  return String(value).replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^[._]+|[._]+$/g, "")
    || "chemrxn-export";
}

function run(io, command, args, options = {}) {
  return io.spawnSync(command, args, {
    encoding: "utf8",
    ...options,
  });
}

async function exists(target) {
  try {
    await access(target, fsConstants.R_OK);
    return true;
  } catch {
    return false;
  }
}

function defaultIo() {
  return {
    env: process.env,
    fetch: globalThis.fetch,
    spawn,
    spawnSync,
    createReadStream,
    writeFile,
    stdout: (value) => process.stdout.write(value),
    stderr: (value) => process.stderr.write(value),
  };
}

export class CliError extends Error {
  constructor(code, message, exitCode = 1) {
    super(message);
    this.code = code;
    this.exitCode = exitCode;
  }
}
