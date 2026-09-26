import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import {
  ChemMcpClient,
  normalizeChemistryToolArguments,
} from "../src/chem-mcp-client.js";
import {
  buildProtocolFingerprint,
  assertPinnedChemistryToolContract,
  CANONICAL_CHEM_MCP_RUNNER,
  canonicalToolContract,
  PINNED_CHEMISTRY_TOOL_CONTRACT_SHA256,
  PINNED_PROTOCOL_FILES,
  ProtocolIntegrityError,
  toolContractSha256,
} from "../src/protocol-fingerprint.js";
import { PINNED_TOOL_ALLOWLIST } from "../src/skills.js";
import { chemistryTestPython } from "./test-python.js";

const REPO = fileURLToPath(new URL("../../../..", import.meta.url));

test("Product bridge discovers the same canonical chemistry MCP contract as native Pi", async () => {
  const workspace = mkdtempSync(path.join(tmpdir(), "chemrxn-tool-parity-"));
  const client = await ChemMcpClient.start({
    python: chemistryTestPython(),
    script: path.join(REPO, CANONICAL_CHEM_MCP_RUNNER),
    cwd: workspace,
    requestTimeoutMs: 120_000,
  });
  try {
    const actual = canonicalToolContract(await client.listTools());
    const actualHash = toolContractSha256(actual);
    assert.deepEqual(actual.map((tool) => tool.name), [...PINNED_TOOL_ALLOWLIST].sort());
    assert.equal(actualHash, PINNED_CHEMISTRY_TOOL_CONTRACT_SHA256);
    assert.throws(
      () => assertPinnedChemistryToolContract(actual.map((tool, index) =>
        index === 0 ? { ...tool, description: `${tool.description} drifted` } : tool)),
      (error: unknown) => error instanceof ProtocolIntegrityError
        && error.component === "chemistry_mcp_tool_contract",
    );
    const fingerprint = buildProtocolFingerprint({
      stage: "dispatch",
      task: "extract_all",
      contextMode: "figure",
      inputs: [{ kind: "figure", sha256: "a".repeat(64) }],
      perceptionStatus: "deferred_to_canonical_tool",
      skills: [],
      protocolFiles: PINNED_PROTOCOL_FILES,
      prompt: "tool contract parity probe",
      toolContract: actual,
      provider: "openai-codex",
      modelId: "gpt-5.6-sol",
      thinkingLevel: "medium",
    });
    assert.equal(fingerprint.tools.contractSha256, actualHash);
    assert.deepEqual(fingerprint.tools.allowlist, actual.map((tool) => tool.name));
  } finally {
    await client.terminate();
    rmSync(workspace, { recursive: true, force: true });
  }
});

test("Product bridge resolves canonical relative tool paths inside the session workspace", () => {
  const workspace = path.resolve("/tmp/chemrxn-session-parity");
  assert.deepEqual(
    normalizeChemistryToolArguments("detect", {
      image_path: "input/figure.png",
      out_dir: "output/perception",
    }, workspace),
    {
      image_path: path.join(workspace, "input/figure.png"),
      out_dir: path.join(workspace, "output/perception"),
    },
  );
  assert.throws(
    () => normalizeChemistryToolArguments("assemble", {
      plan_path: "../../outside.json",
    }, workspace),
    /escapes the session workspace/,
  );
});
