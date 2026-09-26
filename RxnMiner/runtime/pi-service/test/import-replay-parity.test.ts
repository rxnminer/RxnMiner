import assert from "node:assert/strict";
import {
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import {
  artifactFromLiveResult,
  buildLiveFigureResult,
} from "../src/live-results.js";
import { SealedReplayJobService } from "../src/replay-jobs.js";
import { contentHash } from "../src/security.js";
import { InMemoryRuntimeStore } from "../src/store.js";

const REPO = fileURLToPath(new URL("../../../..", import.meta.url));

const EXPECTED_PAPER02_SENTINELS = {
  templateCount: 1,
  exampleCount: 24,
  exampleIds: Array.from({ length: 24 }, (_, index) => String(index + 5)),
  conditionSentinels: [
    ["catalyst", "5 mol% [Ir-(R,R,R)]"],
    ["base", "LiOt-Bu (1.3 equiv)"],
    ["time", "16 h"],
    ["oxidant", "NaBO3·4H2O"],
  ],
  examples: [
    {
      id: "5",
      reactants: ["CCOC(=O)OC/C=C/c1ccccc1", "CCOC(=O)/C=C/c1ccccc1"],
      product: "C=C[C@H](c1ccccc1)[C@@H](C(=O)OCC)[C@H](O)c1ccccc1",
      bboxIndex: 3,
      molparserMd5: "99ff17d0a852fca707b2bc9e51b2ca65",
      metrics: [["NMR yield", "93%"], ["yield", "84%"], ["dr", "98:2"], ["er", ">99.5:0.5"]],
    },
    {
      id: "16",
      reactants: ["CCOC(=O)OC/C=C/c1ccccc1", "C/C=C/C(=O)OCC"],
      product: "C=C[C@H](c1ccccc1)[C@@H](C(=O)OCC)[C@@H](C)O",
      bboxIndex: 8,
      molparserMd5: "754f5fd138ac719489294e884c5ba5be",
      metrics: [["NMR yield", "49%"], ["yield", "44%"], ["dr", "88:12"], ["er", ">99.5:0.5"]],
    },
    {
      id: "28",
      reactants: ["CCOC(=O)OC/C=C/c1ccccc1", "CCN(CC)C(=O)/C=C/c1ccccc1"],
      product: "C=C[C@H](c1ccccc1)[C@@H](C(=O)N(CC)CC)[C@H](O)c1ccccc1",
      bboxIndex: 20,
      molparserMd5: "a3176ac2c82fa9cf768e75c6be0eabc7",
      metrics: [["NMR yield", "69%"], ["yield", "51%"], ["dr", "95:5"], ["er", "99:1"]],
    },
  ],
};

test("live and replay materializers independently preserve authored Paper02 sentinels", async () => {
  const fixture = path.join(
    REPO,
    "runtime/pi-service/test/fixtures/figure/demo-figure",
  );
  const workspace = mkdtempSync(path.join(tmpdir(), "chemrxn-live-replay-parity-"));
  const inputDirectory = path.join(workspace, "input");
  const outputDirectory = path.join(workspace, "output");
  const perceptionDirectory = path.join(outputDirectory, "perception");
  mkdirSync(inputDirectory, { recursive: true });
  mkdirSync(perceptionDirectory, { recursive: true });
  const figurePath = path.join(inputDirectory, "figure.png");
  const predictionPath = path.join(outputDirectory, "result.v2.json");
  const molparserPath = path.join(perceptionDirectory, "figure_molparser.json");
  const reactionsPath = path.join(outputDirectory, "reactions.html");
  copyFileSync(path.join(fixture, "image.png"), figurePath);
  copyFileSync(path.join(fixture, "pred.v2.json"), predictionPath);
  copyFileSync(path.join(fixture, "molparser.json"), molparserPath);
  joinMolParserLineage(predictionPath, molparserPath);
  writeFileSync(reactionsPath, "<!doctype html><title>independent live materialization</title>");

  try {
    const figureBytes = readFileSync(figurePath);
    const live = await buildLiveFigureResult({
      sessionId: "live-parity-session",
      jobId: "live-parity-job",
      workspaceId: "live-parity-session",
      workspace,
      figure: {
        id: "live-input",
        userId: "local-user",
        kind: "figure",
        originalName: "figure.png",
        mediaType: "image/png",
        sha256: contentHash(figureBytes),
        byteSize: figureBytes.length,
        createdAt: "2026-07-31T00:00:00.000Z",
        workspacePath: "input/figure.png",
      },
      provider: "parity-no-model",
      modelId: "fixture-materializer",
      skills: [{
        name: "reaction-extract",
        path: "/pinned/reaction-extract/SKILL.md",
        sha256: "a".repeat(64),
      }],
      tools: [
        {
          name: "detect",
          isError: false,
          payload: {
            raw_molparser_json: molparserPath,
            molparser_provenance: { source: "cache_hit" },
          },
        },
        { name: "assemble", isError: false, payload: { v2_path: predictionPath } },
        { name: "render_reactions", isError: false, payload: { html_path: reactionsPath } },
      ],
    });
    assert.ok(live);
    const livePredictionId = live.viewer?.predictionArtifactId;
    assert.ok(livePredictionId);
    const liveArtifact = artifactFromLiveResult(live, livePredictionId);
    assert.ok(liveArtifact);
    const livePrediction = JSON.parse(
      readFileSync(path.join(workspace, liveArtifact.locator), "utf8"),
    );

    const replayJobs = new SealedReplayJobService(new InMemoryRuntimeStore());
    const replay = await replayJobs.create("local-user", "demo-figure");
    const replayManifest = replay.result.manifest as {
      artifacts: Array<{ id: string; kind: string }>;
    };
    const replayPredictionRef = replayManifest.artifacts
      .find((item) => item.kind === "prediction_v2");
    assert.ok(replayPredictionRef);
    const replayArtifact = await replayJobs.artifact(
      "local-user",
      replay.session.id,
      replayPredictionRef.id,
    );
    const replayPrediction = JSON.parse(replayArtifact.bytes.toString("utf8"));
    const molparser = JSON.parse(readFileSync(molparserPath, "utf8"));

    assert.equal(live.source.mode, "live_pi_sdk");
    assert.equal(replay.result.source.mode, "sealed_replay");
    assert.notEqual(live.sessionId, replay.result.sessionId);
    assert.notEqual(liveArtifact.sha256, replayArtifact.sha256);
    assert.deepEqual(chemistrySentinels(livePrediction, molparser), EXPECTED_PAPER02_SENTINELS);
    assert.deepEqual(chemistrySentinels(replayPrediction, molparser), EXPECTED_PAPER02_SENTINELS);
    assert.equal(live.gtIncluded, false);
    assert.equal(replay.result.gtIncluded, false);
  } finally {
    rmSync(workspace, { recursive: true, force: true });
  }
});

function chemistrySentinels(value: unknown, molparserValue: unknown): unknown {
  const root = record(value);
  const md5ByBbox = new Map(array(molparserValue).flatMap((value): Array<[number, string]> => {
    const item = record(value);
    return Number.isInteger(item.bbox_index) && typeof item.md5 === "string"
      ? [[item.bbox_index as number, item.md5]] : [];
  }));
  const templates = array(root.reaction_templates);
  const examples = array(root.selected_examples).map(record);
  const commonConditions = array(record(templates[0]).conditions).flatMap(array);
  const wantedConditions = new Set(EXPECTED_PAPER02_SENTINELS.conditionSentinels
    .map(([role, text]) => `${role}\u0000${text}`));
  return {
    templateCount: templates.length,
    exampleCount: examples.length,
    exampleIds: examples.map((reaction) => String(reaction.instance_id ?? "")),
    conditionSentinels: commonConditions.flatMap((value): string[][] => {
      const condition = record(value);
      const pair = [String(condition.role ?? ""), String(condition.text ?? "")];
      return wantedConditions.has(`${pair[0]}\u0000${pair[1]}`) ? [pair] : [];
    }),
    examples: ["5", "16", "28"].map((id) => {
      const reaction = examples.find((item) => item.instance_id === id) ?? {};
      const product = record(array(reaction.products)[0]);
      const source = record(product._source);
      const bboxIndex = Number(source.bbox_index);
      return {
        id,
        reactants: array(reaction.reactants).map((value) =>
          String(record(value)["E-SMILES"] ?? "")),
        product: String(product["E-SMILES"] ?? ""),
        bboxIndex,
        molparserMd5: typeof source.molparser_md5 === "string"
          ? source.molparser_md5 : md5ByBbox.get(bboxIndex),
        metrics: array(reaction.metrics).map((value) => {
          const metric = record(value);
          return [String(metric.role ?? ""), String(metric.text ?? "")];
        }),
      };
    }),
  };
}

function joinMolParserLineage(predictionPath: string, molparserPath: string): void {
  const prediction = record(JSON.parse(readFileSync(predictionPath, "utf8")));
  const raw = JSON.parse(readFileSync(molparserPath, "utf8"));
  const md5ByBbox = new Map(array(raw).flatMap((value): Array<[number, string]> => {
    const item = record(value);
    return Number.isInteger(item.bbox_index) && typeof item.md5 === "string"
      ? [[item.bbox_index as number, item.md5]] : [];
  }));
  for (const section of ["reaction_templates", "selected_examples"]) {
    for (const reactionValue of array(prediction[section])) {
      const reaction = record(reactionValue);
      for (const role of ["reactants", "products"]) {
        for (const moleculeValue of array(reaction[role])) {
          const molecule = record(moleculeValue);
          const source = record(molecule._source);
          const bboxIndex = Number.isInteger(source.bbox_index)
            ? source.bbox_index as number : undefined;
          if (bboxIndex !== undefined && md5ByBbox.has(bboxIndex)) {
            source.molparser_md5 = md5ByBbox.get(bboxIndex);
          }
        }
      }
    }
  }
  writeFileSync(predictionPath, JSON.stringify(prediction));
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}
