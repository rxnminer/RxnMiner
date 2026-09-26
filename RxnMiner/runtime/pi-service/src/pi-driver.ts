import { InMemoryCredentialStore } from "@earendil-works/pi-ai";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  createAgentSession,
  DefaultResourceLoader,
  ModelRuntime,
  SessionManager,
  type AgentSession,
  type ToolDefinition,
} from "@earendil-works/pi-coding-agent";
import type { SkillManifestEntry } from "./skills.js";
import type { PiThinkingLevel } from "./contracts.js";
import { DEFAULT_EXTRACTION_THINKING_LEVEL } from "./contracts.js";

const BUILTIN_ONLY_MODELS_PATH = fileURLToPath(
  new URL("./chemrxn-no-custom-models.json", import.meta.url),
);

function enforceEmbeddedPiOfflineStartup(): void {
  // ChemRxn owns model calls explicitly through ModelRuntime. Pi's CLI-oriented
  // update, package-catalog, and telemetry checks are unrelated startup traffic
  // and can otherwise block a local session behind a proxy or offline network.
  // PI_OFFLINE only disables those startup operations; provider requests made
  // by the selected model remain available.
  process.env.PI_OFFLINE = "1";
  process.env.PI_SKIP_VERSION_CHECK = "1";
  process.env.PI_TELEMETRY = "0";
}

export interface PiSessionHandle {
  session: AgentSession;
  supportsImages?: boolean;
  persistentSessionId?: string;
  thinkingLevel?: PiThinkingLevel;
  dispose(): Promise<void>;
}

export async function createPersistentSessionManager(input: {
  cwd: string;
  sessionDir: string;
  sessionId: string;
  resume?: boolean | undefined;
}): Promise<SessionManager> {
  if (!input.resume) {
    return SessionManager.create(input.cwd, input.sessionDir, { id: input.sessionId });
  }
  const matches = (await SessionManager.listAll(input.sessionDir))
    .filter((session) => session.id === input.sessionId);
  if (matches.length === 0) {
    // Pi deliberately does not retain a session that has never received an assistant message.
    // Recreating that empty manager is lossless; a completed turn always has a JSONL to reopen.
    return SessionManager.create(input.cwd, input.sessionDir, { id: input.sessionId });
  }
  if (matches.length !== 1) {
    throw new Error(
      `Persistent Pi session ${input.sessionId} is ambiguous`,
    );
  }
  return SessionManager.open(matches[0]!.path, input.sessionDir, input.cwd);
}

export async function inspectPiAuth(
  authPath: string | undefined,
  provider = "openai-codex",
): Promise<{ configured: boolean; provider: string; profile: "chemrxn_isolated" }> {
  enforceEmbeddedPiOfflineStartup();
  if (!authPath) {
    return { configured: false, provider, profile: "chemrxn_isolated" };
  }
  try {
    const runtime = await ModelRuntime.create({
      authPath,
      // Auth status only checks Pi's built-in openai-codex provider. Keep this
      // path independent from both ~/.pi/agent/models.json and product custom
      // catalogs so a model configuration cannot change login detection.
      modelsPath: BUILTIN_ONLY_MODELS_PATH,
      allowModelNetwork: false,
    });
    return {
      configured: runtime.hasConfiguredAuth(provider),
      provider,
      profile: "chemrxn_isolated",
    };
  } catch {
    return { configured: false, provider, profile: "chemrxn_isolated" };
  }
}

export async function inspectPiModel(
  provider: string,
  modelId: string,
  modelsPath?: string,
): Promise<{
  available: boolean;
  provider: string;
  modelId: string;
  supportsImages: boolean;
}> {
  enforceEmbeddedPiOfflineStartup();
  const runtime = await ModelRuntime.create({
    credentials: new InMemoryCredentialStore(),
    modelsPath: modelsPath ?? BUILTIN_ONLY_MODELS_PATH,
    allowModelNetwork: false,
  });
  const model = runtime.getModel(provider, modelId);
  return {
    available: Boolean(model),
    provider,
    modelId,
    supportsImages: model?.input.includes("image") === true,
  };
}

export async function createPiSession(input: {
  cwd: string;
  agentDir: string;
  provider: string;
  modelId: string;
  thinkingLevel?: PiThinkingLevel;
  authMode: "api_key" | "pi_auth";
  apiKey?: string | undefined;
  authPath?: string | undefined;
  modelsPath?: string | undefined;
  sessionDir: string;
  sessionId: string;
  resume?: boolean | undefined;
  skills: SkillManifestEntry[];
  tools: string[];
  customTools: ToolDefinition[];
  onEvent(event: unknown): void;
}): Promise<PiSessionHandle> {
  const requestedThinkingLevel = input.thinkingLevel ?? DEFAULT_EXTRACTION_THINKING_LEVEL;
  enforceEmbeddedPiOfflineStartup();
  if (input.authMode === "pi_auth" && !input.authPath) {
    throw new Error(
      "A ChemRxn-owned auth path is required for pi_auth; the general Pi profile is never used.",
    );
  }
  const modelsPath = input.modelsPath ?? path.join(input.agentDir, "models.json");
  const runtime = input.authMode === "api_key"
    ? await ModelRuntime.create({
      credentials: new InMemoryCredentialStore(),
      modelsPath,
      allowModelNetwork: false,
    })
    : await ModelRuntime.create({
      ...(input.authPath ? { authPath: input.authPath } : {}),
      modelsPath,
      allowModelNetwork: false,
    });
  const usesRuntimeKey = input.authMode === "api_key";
  if (usesRuntimeKey) {
    if (!input.apiKey) throw new Error("An API key is required for api_key auth");
    await runtime.setRuntimeApiKey(input.provider, input.apiKey);
  } else if (!runtime.hasConfiguredAuth(input.provider)) {
    throw new Error(
      `Pi auth is not configured for ${input.provider}. Run \`chemrxn login\` first.`,
    );
  }
  const model = runtime.getModel(input.provider, input.modelId);
  if (!model) {
    if (usesRuntimeKey) await runtime.removeRuntimeApiKey(input.provider);
    throw new Error(`Model ${input.provider}/${input.modelId} is not available`);
  }

  let session: AgentSession | undefined;
  try {
    const loader = new DefaultResourceLoader({
      cwd: input.cwd,
      agentDir: input.agentDir,
      additionalSkillPaths: input.skills.map((skill) => skill.path),
      noExtensions: true,
      noPromptTemplates: true,
      noThemes: true,
      noContextFiles: true,
      skillsOverride: (current) => ({
        skills: current.skills.filter((skill) =>
          input.skills.some((pinned) => pinned.path === skill.filePath),
        ),
        diagnostics: current.diagnostics,
      }),
    });
    await loader.reload();
    const loadedSkillPaths = new Set(
      loader.getSkills().skills.map((skill) => skill.filePath),
    );
    const missingSkills = input.skills.filter((skill) => !loadedSkillPaths.has(skill.path));
    if (missingSkills.length) {
      throw new Error(`Pinned Pi skills did not load: ${missingSkills.map((skill) => skill.name).join(", ")}`);
    }
    ({ session } = await createAgentSession({
      cwd: input.cwd,
      agentDir: input.agentDir,
      modelRuntime: runtime,
      model,
      thinkingLevel: requestedThinkingLevel,
      resourceLoader: loader,
      sessionManager: await createPersistentSessionManager({
        cwd: input.cwd,
        sessionDir: input.sessionDir,
        sessionId: input.sessionId,
        resume: input.resume,
      }),
      tools: input.tools,
      customTools: input.customTools,
    }));
    if (session.thinkingLevel !== requestedThinkingLevel) {
      throw new Error(
        `Model ${input.provider}/${input.modelId} cannot honor thinking=${requestedThinkingLevel} `
          + `(Pi resolved ${session.thinkingLevel}); choose an explicitly supported level.`,
      );
    }
    const activeToolNames = new Set(session.agent.state.tools.map((tool) => tool.name));
    const missingTools = input.tools.filter((tool) => !activeToolNames.has(tool));
    if (missingTools.length) {
      throw new Error(`Pinned chemistry tools are not active: ${missingTools.join(", ")}`);
    }
  } catch (error) {
    session?.dispose();
    if (usesRuntimeKey) await runtime.removeRuntimeApiKey(input.provider);
    throw error;
  }
  const unsubscribe = session.subscribe(input.onEvent);
  return {
    session,
    supportsImages: model.input.includes("image"),
    persistentSessionId: input.sessionId,
    thinkingLevel: session.thinkingLevel,
    async dispose() {
      unsubscribe();
      session.dispose();
      if (usesRuntimeKey) await runtime.removeRuntimeApiKey(input.provider);
    },
  };
}
