import assert from "node:assert/strict";
import {
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import {
  artifactFromLiveResult,
  auditFigureToolClosure,
  buildLiveFigureResult,
  FigureSealBlockedError,
  readWorkspaceArtifact,
  safeWorkspacePath,
} from "../src/live-results.js";
import { contentHash } from "../src/security.js";

test("live Figure tool artifacts become the shared manifest and viewer contract", async () => {
  const workspace = mkdtempSync(path.join(tmpdir(), "chemrxn-live-result-"));
  mkdirSync(path.join(workspace, "input"), { recursive: true });
  mkdirSync(path.join(workspace, "output", "perception"), { recursive: true });
  const figurePath = path.join(workspace, "input", "figure.png");
  const sourceFigure = path.resolve(
    import.meta.dirname,
    "../../../../runtime/pi-service/test/fixtures/figure/demo-figure/image.png",
  );
  copyFileSync(sourceFigure, figurePath);
  const rawPath = path.join(workspace, "output", "perception", "figure_molparser.json");
  const overviewPath = path.join(workspace, "output", "perception", "figure_overview.png");
  const predictionPath = path.join(workspace, "output", "result.v2.json");
  const reactionsPath = path.join(workspace, "output", "reactions.html");
  writeFileSync(rawPath, JSON.stringify([{
    bbox_index: 0,
    bbox: [1, 2, 20, 30],
    smi: "CCO",
    md5: "abc",
    markush: false,
  }]));
  writeFileSync(overviewPath, readFileSync(sourceFigure));
  writeFileSync(predictionPath, JSON.stringify({
    selected_examples: [{
      instance_id: "1",
      reactants: [{ identifier: "1", "E-SMILES": "CC", _source: { origin: "molparser", bbox_index: 0, bbox: [1, 2, 20, 30], molparser_md5: "abc" } }],
      products: [{ identifier: "2", "E-SMILES": "CCO", _source: { origin: "molparser", bbox_index: 0, bbox: [1, 2, 20, 30], molparser_md5: "abc" } }],
      conditions: [],
      metrics: [{ role: "yield", text: "90%" }],
    }],
    reaction_templates: [],
  }));
  writeFileSync(reactionsPath, "<!doctype html><title>reaction</title>");
  try {
    const bytes = readFileSync(figurePath);
    const result = await buildLiveFigureResult({
      sessionId: "session-live",
      jobId: "job-live",
      workspaceId: "session-live",
      workspace,
      figure: {
        id: "input-live",
        userId: "local-user",
        kind: "figure",
        originalName: "reaction.png",
        mediaType: "image/png",
        sha256: contentHash(bytes),
        byteSize: bytes.length,
        createdAt: "2000-01-01T00:00:00.000Z",
        workspacePath: "input/figure.png",
      },
      provider: "openai",
      modelId: "gpt-test",
      skills: [{
        name: "reaction-extract",
        path: "/skills/reaction-extract/SKILL.md",
        sha256: "a".repeat(64),
      }],
      tools: [
        {
          name: "detect",
          isError: false,
          payload: {
            raw_molparser_json: rawPath,
            overview_png: overviewPath,
            detections: [{ bbox_index: 0, bbox: [1, 2, 20, 30], smi: "CCO" }],
            molparser_provenance: { source: "live_molparser" },
          },
        },
        {
          name: "assemble",
          isError: false,
          payload: { v2_path: predictionPath, n_examples: 1 },
        },
        {
          name: "render_reactions",
          isError: false,
          payload: { html_path: reactionsPath },
        },
      ],
    });
    assert.ok(result);
    assert.equal(result.source.mode, "live_pi_sdk");
    assert.equal(result.source.reconstructed, false);
    assert.equal(result.gtIncluded, false);
    assert.equal(result.terminalStatus, "review_ready");
    assert.equal((result.manifest as { counts: { examples: number } }).counts.examples, 1);
    assert.ok(result.viewer?.overviewArtifactId);
    assert.ok(result.viewer?.reactionsArtifactId);
    assert.equal(
      artifactFromLiveResult(result, result.viewer!.predictionArtifactId!)?.locator,
      "output/result.v2.json",
    );
    assert.equal(safeWorkspacePath(workspace, "/etc/passwd"), undefined);

    const withoutLineage = JSON.parse(readFileSync(predictionPath, "utf8"));
    delete withoutLineage.selected_examples[0].products[0]._source.molparser_md5;
    withoutLineage.selected_examples[0].products[0].identifier = "product from bbox 0";
    writeFileSync(predictionPath, JSON.stringify(withoutLineage));
    await assert.rejects(
      buildLiveFigureResult({
        sessionId: "session-blocked", jobId: "job-blocked", workspaceId: "session-blocked",
        workspace,
        figure: {
          id: "input-live", userId: "local-user", kind: "figure", originalName: "reaction.png",
          mediaType: "image/png", sha256: contentHash(bytes), byteSize: bytes.length,
          createdAt: "2000-01-01T00:00:00.000Z", workspacePath: "input/figure.png",
        },
        provider: "openai", modelId: "gpt-test",
        skills: [{ name: "reaction-extract", path: "/skills/reaction-extract/SKILL.md", sha256: "a".repeat(64) }],
        tools: [
          { name: "detect", isError: false, payload: { raw_molparser_json: rawPath, molparser_provenance: { source: "cache_hit" } } },
          { name: "assemble", isError: false, payload: { v2_path: predictionPath } },
          { name: "render_reactions", isError: false, payload: { html_path: reactionsPath } },
        ],
      }),
      (error: unknown) => error instanceof FigureSealBlockedError
        && /molparser_md5/.test(error.message),
    );
  } finally {
    rmSync(workspace, { recursive: true, force: true });
  }
});

test("Figure closure rejects verification after assembly until a new assemble and render cycle", () => {
  const stale = auditFigureToolClosure([
    { name: "assemble", isError: false, payload: { v2_path: "output/result.v2.json" } },
    { name: "render_reactions", isError: false, payload: { html_path: "output/reactions.html" } },
    { name: "verify_smiles", isError: false, payload: { valid: true, canonical_iso: "CCO" } },
  ]);
  assert.equal(stale.valid, false);
  assert.equal(stale.assemblyRevision, 1);
  assert.deepEqual(stale.postAssemblyVerificationIndexes, [2]);
  assert.match(stale.issues.join(" "), /after the latest assemble/);

  const closed = auditFigureToolClosure([
    { name: "assemble", isError: false },
    { name: "verify_smiles", isError: false },
    { name: "assemble", isError: false },
    { name: "render_reactions", isError: false },
  ]);
  assert.equal(closed.valid, true);
  assert.equal(closed.assemblyRevision, 2);
  assert.deepEqual(closed.postAssemblyVerificationIndexes, []);
  assert.equal(new FigureSealBlockedError(stale.issues, stale).name, "FigureSealBlockedError");
});

test("workspace artifacts accept canonical path aliases without allowing symlink escape", {
  skip: process.platform === "win32" ? "POSIX symlink semantics are covered on Linux and macOS" : false,
}, async () => {
  const canonicalWorkspace = mkdtempSync(path.join(tmpdir(), "chemrxn-live-canonical-"));
  const aliasParent = mkdtempSync(path.join(tmpdir(), "chemrxn-live-alias-"));
  const externalRoot = mkdtempSync(path.join(tmpdir(), "chemrxn-live-external-"));
  const workspaceAlias = path.join(aliasParent, "workspace");
  try {
    mkdirSync(path.join(canonicalWorkspace, "output"), { recursive: true });
    const artifact = path.join(canonicalWorkspace, "output", "result.v2.json");
    writeFileSync(artifact, "{}\n");
    symlinkSync(canonicalWorkspace, workspaceAlias, "dir");

    assert.equal(
      safeWorkspacePath(workspaceAlias, artifact),
      path.join(workspaceAlias, "output", "result.v2.json"),
    );
    assert.equal(
      (await readWorkspaceArtifact(workspaceAlias, artifact))?.bytes.toString("utf8"),
      "{}\n",
    );

    const externalArtifact = path.join(externalRoot, "outside.json");
    writeFileSync(externalArtifact, "{}\n");
    const escaped = path.join(canonicalWorkspace, "output", "escaped.json");
    symlinkSync(externalArtifact, escaped, "file");
    assert.equal(safeWorkspacePath(workspaceAlias, escaped), undefined);
    assert.equal(await readWorkspaceArtifact(workspaceAlias, escaped), undefined);

    const redirectedDirectory = path.join(canonicalWorkspace, "redirected");
    symlinkSync(externalRoot, redirectedDirectory, "dir");
    assert.equal(
      safeWorkspacePath(workspaceAlias, path.join(redirectedDirectory, "not-created.json")),
      undefined,
    );
  } finally {
    rmSync(aliasParent, { recursive: true, force: true });
    rmSync(canonicalWorkspace, { recursive: true, force: true });
    rmSync(externalRoot, { recursive: true, force: true });
  }
});
