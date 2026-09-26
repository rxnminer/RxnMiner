import { createHash } from "node:crypto";
import { readFileSync, statSync, type Stats } from "node:fs";
import path from "node:path";
import type { ContextMode, PiThinkingLevel, TaskKind } from "./contracts.js";
import type { SkillManifestEntry } from "./skills.js";

export const PROTOCOL_FINGERPRINT_VERSION = "rxnminer.protocol-fingerprint.v1" as const;
export const PRODUCT_PI_ADAPTER_VERSION = "rxnminer.pi-sdk-adapter.v1" as const;
export const PI_SDK_VERSION = "0.82.1" as const;
export const CANONICAL_CHEM_MCP_RUNNER = "figure_skills/scripts/run_chem_mcp.py" as const;

export interface ProtocolArtifactIdentity {
  kind: string;
  sha256: string;
}

export interface ProtocolFileIdentity {
  role:
    | "reference"
    | "contract"
    | "validator"
    | "perception"
    | "prompt_builder"
    | "tool_runner"
    | "tool_implementation"
    | "tool_schema"
    | "adapter"
    | "orchestrator"
    | "skill"
    | "dependency_manifest"
    | "dependency_source";
  path: string;
  sha256: string;
}

export interface ProtocolToolDescriptor {
  name: string;
  description: string;
  inputSchema: unknown;
}

export interface ProtocolToolSource {
  name: string;
  description?: string;
  inputSchema?: unknown;
  parameters?: unknown;
}

export interface ProtocolFingerprint {
  schemaVersion: typeof PROTOCOL_FINGERPRINT_VERSION;
  sha256: string;
  stage: "dispatch" | "sealed";
  task: TaskKind;
  contextMode: ContextMode;
  input: {
    artifacts: ProtocolArtifactIdentity[];
    perception: {
      status: "deferred_to_canonical_tool" | "resolved_before_model" | "resolved_after_model";
      artifacts: ProtocolArtifactIdentity[];
    };
  };
  skills: Array<{ name: string; sha256: string }>;
  protocolFiles: ProtocolFileIdentity[];
  prompt: { sha256: string; byteLength: number };
  tools: {
    allowlist: string[];
    contractSha256: string;
    runner: typeof CANONICAL_CHEM_MCP_RUNNER;
  };
  model: {
    provider: string;
    id: string;
    thinkingLevel: PiThinkingLevel;
  };
  runtime: {
    piSdkVersion: typeof PI_SDK_VERSION;
    adapterVersion: typeof PRODUCT_PI_ADAPTER_VERSION;
  };
}

/**
 * These hashes are protocol pins, not a second implementation. The parity gate
 * verifies them against the canonical repository files. Updating chemistry
 * policy therefore requires an explicit fingerprint/version update.
 */
const PINNED_PROTOCOL_FILE_SPECS = [
  // Chemistry policy and normalization references.
  ["reference", "figure_skills/skills/reaction-extract/LESSONS.md", "ff29c2f0b5e23d74e9b8e83be5b0420745441dcb3f28c3cdb1af6db56d485447"],
  ["reference", "figure_skills/skills/reaction-extract/references/extended-smiles-2.0.md", "f149eb402de4bfe5dd2b0f4263d29f417f921f17c525a79b8f000ec984356daf"],
  ["skill", "figure_skills/skills/reaction-extract/SKILL.md", "fb6741c353ad837169821f8751abdf2cbc56f90747ee007dafebb11e985b25c4"],

  // Context-specific document contracts and frozen validators.
  ["validator", "figure_skills/engine/v2.py", "2ea7954c75d7a571a5ec4f7ac8ca4024c9329bd229de8b6b7622149be9e411e4"],

  // Perception and the direct-Pi prompt/adapter used as the comparison lane.
  ["prompt_builder", "figure_skills/scripts/run_pi_extract.sh", "20601cdf786819c55e7f5c71e6624ee0c2110826df033ccfcd867c157240334f"],
  ["adapter", "figure_skills/pi-extension/index.ts", "e87017be665aa3c827396f905cef4a3c597f33120a98cd02b76c6f0dcb6d1357"],

  // One canonical chemistry MCP runner and implementation.
  ["tool_runner", CANONICAL_CHEM_MCP_RUNNER, "4208db1ab1fd0a3ce74d6ea0e48bfd554d2dc62b5b6fec76a63b15145cdc3b95"],
  ["tool_implementation", "figure_skills/tools/chem_mcp.py", "c6f816fda39d04745388a3b3d446d727d00a8e83c93813854ea8e85c4dc7d12e"],

  // Declared dependency manifests. The Python file is a declared manifest,
  // not a wheel-byte lock; installed-library equivalence remains outside this
  // no-model gate. The Node lock is exact for the embedded Pi adapter.
  ["dependency_manifest", "figure_skills/requirements.txt", "743eb9cb275211163cd05222a8464dcde38a25d78258572f492066dcf0aff08d"],
  ["dependency_manifest", "runtime/pi-service/package.json", "75b893c7b681aab8d222bab82c3c4de08716dfde7e7102d7614a5e649ed2e9ec"],
  ["dependency_manifest", "runtime/pi-service/package-lock.json", "6801d0c7026b00afac982977dce33db9cbbc3cdb77e03d7c1263eacd81ee5379"],

  // Behavior-critical local imports reachable from chem_mcp.py. This closes
  // the source boundary across OCSR, V2 assembly, R-group handling, and both
  // E-SMILES implementations without pretending to pin external services.
  ["dependency_source", "figure_skills/tools/uniparser_client.py", "6c4b116816d792e550c7bdaf3bd3861da2386c86efa8e3c907430d9a886c7698"],
  ["dependency_source", "figure_skills/tools/ocsr_client.py", "d484aa27799dd0f0e391fe5ede987806fbfa3f33405baa942316d7ae775606db"],
  ["dependency_source", "figure_skills/tools/ocsr_visualize.py", "1838b27ae2879de290f3f96a1d9984d2ed693f131fefb7c52f0668c56e0bec3c"],
  ["dependency_source", "figure_skills/tools/esmiles_audit.py", "d397cb056b588c5c2d61813b8be80d2fb3733c7533bd899085ce5024ee5d9b40"],
  ["dependency_source", "figure_skills/tools/render_v2.py", "f78fdf95e6171fbb60ab6cee30f78bd70cbc10e124ca73af95259c6646c3e2ca"],
  ["dependency_source", "figure_skills/engine/markush.py", "06e63ede23585d4ffbe21cf9ee005218d300f0e27c8419223bcbb99f54913b25"],
  ["dependency_source", "figure_skills/tools_product/uniparser_to_v2.py", "749209abc7f892048f8782609f52fcaff77a6d5e14f974bd1cdf69cc055fa90f"],
  ["dependency_source", "figure_skills/agent/markush_substitute.py", "2dbdb7ff179d72483921d125f0a0bebc7a33317255bc9819464f2cb7da890430"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/ma_paths.py", "f99697214bba22b278cb8eeceb2ae7ad7895be58576b716c2112e121fbb75b25"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/abbrev_expand.py", "eec217ca3af77963bbbfd5e51d77410b1b1d6e00a8612ea0d63846f078a57c04"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/align.py", "920ee36e7eefd7b214564e39e00aab551e61be09366f8213ba58c53b0d46a1a6"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/anchors.py", "963a682fc2e0ba08db37f21f5fc1d818aad2fcf28febb8c4f01901ae2b5c1ffc"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/assembly.py", "0b9fff26a543f905f97ecf5c40c45f611144637545b2fd0156a89cfa677ca891"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/assembly_export.py", "1fc03e0fd425bd3b171a021f0588aa8a4a3c897c38eb9427ce9a003948b72432"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/candidates.py", "fb3ef9ea2ae008702aae43e00c6b3f83b1b0ba3ec08c039707f877327d23c340"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/chem_util.py", "a8628af18cf81de06223582cefa73abd0f428fa5cc3b1c95a5e26527ebab6a9a"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/decompose.py", "0684d2ff4d4248f98f77d4ac7035c95e43eac2d69b08f49aad201103d1389484"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/esmiles_contract.py", "14d0c80b419b723af3fc0f314b3275b3a80f9471157831a9848e5a2c7824b86f"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/esmiles_encode.py", "e5edf432805ad0af2c11ea281af104c7eb8240fdc79a613ead81e4b950c20668"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/esmiles_ingress.py", "9d370f79b571f8204235f8b5518840677c983aa7eb545a8c34133127f79ae131"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/esmiles_loader.py", "cf7d64a68f30ae2af02bdd58c7ffd7e132a689d7682c12a5c3efa4e87f920973"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/models.py", "43ff5ce345eef0d5937a820d526ad5d6b54dbab924b1a9ffc42b336ff17e793a"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/peel.py", "f4a7fb4dc16fffad0a8e22300cb5fa4ff43dcd688a0361fd18b85961b3ad6f5e"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/peel_topology.py", "6c853f902ae5e81a3cd06c19fd0433423bf04451b4c46f84e0f8ebdbd2210549"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/r_groups.py", "c1e2708a5ef1a3c4931a3b378a6cebcfa25ce16d639d1aa17c8df582f3a871ad"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/roundtrip.py", "64dd9df5bf20e5d4a8acd0e4c7f5351b895c4cbede7f1e57f375feca1030313f"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/scaffold_geom.py", "379faf406ea538ec9954a4d677c6eff990b0a2296a6fff5bf2932de089f7283e"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/stereo.py", "981a0b2873536177227fcaff376a5aff6a59cf8cb40caaf0afbd499816488261"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/reverse_assembly.py", "d707074b73b2adbae54fa37b71b4efebb2263b1412dbdcff6e62f0275372c55f"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/translator/__init__.py", "c9cdb6a660f9bee8966d0e4463f3fc3757736467829549c435223ad22d393579"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/translator/_utils.py", "3958a9af026df0a96e773f813d4167fbae70002e4c53baf058b898fe6bd57653"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/translator/abbrevs.csv", "5aaea3d25b6535941e8c7aa2694a31ac5f433b8b41a3691b16fd3b2fbb034c6e"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly/src/translator/translator.py", "1b18fd0cb637cebcb5b63b85ada99ac4ca9935e99f321ebf71ac1a85ccaeb2f6"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/__init__.py", "67c0726a7128cc78e87dd50ef179daa18fc857ee7c80c4413d0a87788eb60d37"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/align.py", "b3238a545eb7f0f15f9b6b561604b158ea427b11b82513fd4338ff58f31decce"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/anchors.py", "f47469ae202a1938bb9d3a322be5124b945076c37042fc2acdd98400051924f1"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/assembly.py", "996ca81d1091ea17c82a8c33a0f7e79199c1a4d44039b00f1e29e917673dd850"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/assembly_export.py", "29849fa2f4293b9bfe88b3732f2a78f8f38e4f9dd85121f6b86ec6bb892a5076"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/candidates.py", "7fad0ca05477ae2c06dca1ae18fbd11b4303e01e1071e6b5e4040970a2081e29"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/chem_util.py", "266162f9c539b384532c79175637d549b45f7c96a9163267fe3f6d14fc613dd0"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/decompose.py", "246915ee580de3f3ae37411b80e8d9ae23e80a5d181182a88bd54b480a29a837"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/graft.py", "ec78ced6a4c0babe5bb9c7b6bd9de68ec8027ffdae0a53b3b15e31eab7aa226a"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/models.py", "3bc140484e1e8cd0de267b1184fbd16ecf0721ccc18731136c2efe6620b95a4a"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/peel.py", "a007141c0219795722d0d3a439b644038457b73261933ce4876f2cd1bcb224ec"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/peel_topology.py", "de536c289b8975b123adf862144ae1ebe31a590529545a8508f2b9865dfff411"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/r_groups.py", "f60a37749f70210c6a43ae0e79f8caeaa57fc739af053efbf6de941f1fc92095"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/rendering.py", "5f2d7151b158e4360eea180ce548cebded28deed7842217b44cb257fa7b5b8b8"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/reverse_assembly.py", "07c8ff77db926cd84810edb813d0a3c7258bc702d2e39cbb329de645c6a0be3c"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/ring_transfer.py", "c47f8aa320e3abdf760dc76c4b77def624bc3c0ca1fe90e37b72f92776be56cd"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/roundtrip.py", "a69246499c3519edcac383eb15547f516c2844813d09829a6878caddc64c05dc"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/scaffold_geom.py", "6e007278d37a7f284ef808949c1e8701cb114c4bb9344ffd19ea5e9c7959ee1f"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/chemistry/stereo.py", "a683896aa72fa0726018cdc18bcc8213fdb387a1bf9aa8f9363d48fde797481a"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/esmiles/__init__.py", "0bdd739f66484ff0385959d689bf5d2ff2f9cc26ab3758308e409ae34039e5fa"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/esmiles/abbrev.py", "1f90512052580c7d84ffa0e906f84cabca789cd56ab3a66814f019613639e9bf"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/esmiles/contract.py", "f338e47c93053f5cb71b67eb1cd860ac5a6e6291d53478dc6ccca9b3ba3f4f8f"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/esmiles/encode.py", "6e7524568f9c9c14b51ea58f5edd3c33e5f90dcc52f33991ba2a12f75fb3cec3"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/esmiles/ingress.py", "4618430b8725e8dad718c16eba45c0b58d2e713b1f33694e5afeae4c247a3c43"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/esmiles/loader.py", "4776d31eeb1ebf531a973efc745c74531e0c96e3ceb4ec4631b7d7c50b56363f"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/esmiles/methylene.py", "efed4be61e0b86c8257c64da691e888747bb42cb59bc9093e7bac9836a689643"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/esmiles/ring_display.py", "8dd8ed2c5944583a59221bf4d28a58325ba5f8a3175c1d044a1ad90f42118637"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/esmiles/text_markush.py", "acaea8e993f5e4039406f9e68e991bb752a907a37086d135b78b8c34d5db1fe3"],
  ["dependency_source", "figure_skills/third_party/molecular_assembly_v2/src/esmiles/tilde_bonds.py", "67124ecfad37508de61d5e0fb6b8af841854fa8279327bbf68b7f658625fe892"],

  // Product-side schemas, process adapters, prompt orchestration, and pins.
  ["tool_schema", "runtime/pi-service/src/workspace-ui-tools.ts", "5520c485398913761e9ff0b37403f0f2bd5004d253fcb5ae3bf076bf49270684"],
  ["adapter", "runtime/pi-service/src/chem-mcp-client.ts", "16f9153458115afcdbf79364cf2509942aa584ed7488c11b5526a0f1ae16ffd2"],
  ["adapter", "runtime/pi-service/src/pi-driver.ts", "aa0109b51ae4bf4451547ea29c84aa189562ca9b56fa03511892848e83c15c9d"],
  ["adapter", "runtime/pi-service/src/skills.ts", "6cfb2192c85a26c8ed01c6a1e25bfb832c6977fb60c0046761d356bbc378166f"],
  ["orchestrator", "runtime/pi-service/src/local-agent.ts", "fb4309548574d9243f848445105014c44c1a6ee36ee977c34508480aa7dea3fa"],
] as const satisfies ReadonlyArray<readonly [ProtocolFileIdentity["role"], string, string]>;

export const PINNED_PROTOCOL_FILES: readonly ProtocolFileIdentity[] = Object.freeze(
  PINNED_PROTOCOL_FILE_SPECS.map(([role, filePath, sha256Value]) => Object.freeze({
    role,
    path: filePath,
    sha256: sha256Value,
  })),
);

export const PINNED_CHEMISTRY_TOOL_CONTRACT_SHA256 =
  "5e4e19366b7419196c5056c11e85783c70fe8f43bebec736cf9918d1cf3ec932";

export function assertPinnedChemistryToolContract(
  tools: readonly ProtocolToolSource[],
): ProtocolToolDescriptor[] {
  const contract = canonicalToolContract(tools);
  if (toolContractSha256(contract) !== PINNED_CHEMISTRY_TOOL_CONTRACT_SHA256) {
    throw new ProtocolIntegrityError("chemistry_mcp_tool_contract", "hash_mismatch");
  }
  return contract;
}

export class ProtocolIntegrityError extends Error {
  constructor(
    public readonly component: string,
    public readonly reason: "missing" | "not_file" | "changed" | "hash_mismatch",
  ) {
    super(`Protocol integrity check failed (${reason}): ${component}`);
    this.name = "ProtocolIntegrityError";
  }
}

/**
 * Verify content on every execution boundary. Filesystem timestamps can be
 * unchanged for rapid same-size writes, so metadata alone cannot cache a digest.
 */
export class ProtocolIntegrityVerifier {
  constructor(
    private readonly repoRoot: string,
    private readonly pins: readonly ProtocolFileIdentity[] = PINNED_PROTOCOL_FILES,
  ) {}

  verify(): ProtocolFileIdentity[] {
    return this.pins.map((component) => {
      const target = path.resolve(this.repoRoot, component.path);
      const relative = path.relative(path.resolve(this.repoRoot), target);
      if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
        throw new ProtocolIntegrityError(component.path, "missing");
      }
      let before: Stats;
      try {
        before = statSync(target);
      } catch {
        throw new ProtocolIntegrityError(component.path, "missing");
      }
      if (!before.isFile()) throw new ProtocolIntegrityError(component.path, "not_file");
      const beforeMetadata = protocolMetadata(before);
      const digest = sha256(readFileSync(target));
      const after = statSync(target);
      if (!after.isFile() || protocolMetadata(after) !== beforeMetadata) {
        throw new ProtocolIntegrityError(component.path, "changed");
      }
      if (digest !== component.sha256) {
        throw new ProtocolIntegrityError(component.path, "hash_mismatch");
      }
      return { role: component.role, path: component.path, sha256: digest };
    });
  }
}

export function buildProtocolFingerprint(input: {
  stage: ProtocolFingerprint["stage"];
  task: TaskKind;
  contextMode: ContextMode;
  inputs: readonly ProtocolArtifactIdentity[];
  perceptionStatus: ProtocolFingerprint["input"]["perception"]["status"];
  perception?: readonly ProtocolArtifactIdentity[];
  skills: readonly SkillManifestEntry[];
  protocolFiles: readonly ProtocolFileIdentity[];
  prompt: string;
  toolContract: readonly ProtocolToolSource[];
  provider: string;
  modelId: string;
  thinkingLevel: PiThinkingLevel;
}): ProtocolFingerprint {
  const toolContract = canonicalToolContract(input.toolContract);
  const allowlist = toolContract.map((tool) => tool.name);
  const body = {
    schemaVersion: PROTOCOL_FINGERPRINT_VERSION,
    stage: input.stage,
    task: input.task,
    contextMode: input.contextMode,
    input: {
      artifacts: sortedArtifacts(input.inputs),
      perception: {
        status: input.perceptionStatus,
        artifacts: sortedArtifacts(input.perception ?? []),
      },
    },
    skills: [...input.skills]
      .map(({ name, sha256 }) => ({ name, sha256: lowerSha(sha256) }))
      .sort((a, b) => a.name.localeCompare(b.name)),
    protocolFiles: canonicalProtocolFiles(input.protocolFiles),
    prompt: {
      sha256: sha256(input.prompt),
      byteLength: Buffer.byteLength(input.prompt, "utf8"),
    },
    tools: {
      allowlist,
      contractSha256: toolContractSha256(toolContract),
      runner: CANONICAL_CHEM_MCP_RUNNER,
    },
    model: {
      provider: input.provider,
      id: input.modelId,
      thinkingLevel: input.thinkingLevel,
    },
    runtime: {
      piSdkVersion: PI_SDK_VERSION,
      adapterVersion: PRODUCT_PI_ADAPTER_VERSION,
    },
  } satisfies Omit<ProtocolFingerprint, "sha256">;
  return {
    ...body,
    sha256: sha256(canonicalJson(body)),
  };
}

export function verifyPinnedProtocolFiles(repoRoot: string): ProtocolFileIdentity[] {
  return new ProtocolIntegrityVerifier(repoRoot).verify();
}

export function semanticJsonSha256(value: unknown): string {
  return sha256(canonicalJson(value));
}

export function canonicalToolContract(
  tools: readonly ProtocolToolSource[],
): ProtocolToolDescriptor[] {
  const contract = tools.map((tool) => {
    const description = typeof tool.description === "string" ? tool.description : "";
    const inputSchema = tool.inputSchema ?? tool.parameters;
    if (!tool.name || inputSchema === undefined) {
      throw new Error(`Incomplete protocol tool descriptor: ${tool.name || "<unnamed>"}`);
    }
    return {
      name: tool.name,
      description,
      inputSchema: sortValue(inputSchema),
    };
  }).sort((a, b) => a.name.localeCompare(b.name));
  for (let index = 1; index < contract.length; index += 1) {
    if (contract[index - 1]!.name === contract[index]!.name) {
      throw new Error(`Duplicate protocol tool descriptor: ${contract[index]!.name}`);
    }
  }
  return contract;
}

export function toolContractSha256(tools: readonly ProtocolToolSource[]): string {
  return sha256(canonicalJson(canonicalToolContract(tools)));
}

function sortedArtifacts(input: readonly ProtocolArtifactIdentity[]): ProtocolArtifactIdentity[] {
  return [...input]
    .map(({ kind, sha256 }) => ({ kind, sha256: lowerSha(sha256) }))
    .sort((a, b) => a.kind.localeCompare(b.kind) || a.sha256.localeCompare(b.sha256));
}

function canonicalProtocolFiles(
  input: readonly ProtocolFileIdentity[],
): ProtocolFileIdentity[] {
  const files = input.map(({ role, path: filePath, sha256 }) => ({
    role,
    path: filePath,
    sha256: lowerSha(sha256),
  })).sort((a, b) => a.path.localeCompare(b.path) || a.role.localeCompare(b.role));
  for (let index = 1; index < files.length; index += 1) {
    if (files[index - 1]!.path === files[index]!.path) {
      throw new Error(`Duplicate protocol file identity: ${files[index]!.path}`);
    }
  }
  return files;
}

function protocolMetadata(value: Stats): string {
  return [value.dev, value.ino, value.size, value.mtimeMs, value.ctimeMs].join(":");
}

function lowerSha(value: string): string {
  const normalized = value.toLowerCase();
  if (!/^[a-f0-9]{64}$/.test(normalized)) throw new Error(`Invalid protocol SHA-256: ${value}`);
  return normalized;
}

function sha256(value: string | Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(sortValue(value));
}

function sortValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortValue);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, item]) => [key, sortValue(item)]));
  }
  return value;
}
