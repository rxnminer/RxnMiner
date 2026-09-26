import assert from "node:assert/strict";
import { chmod, cp, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { buildFigureManifest } from "../src/adapters/figure/adapter.js";
import type { FigureFixtureMetadata } from "../src/adapters/figure/contracts.js";
import { FigureEventEmitter } from "../src/adapters/figure/events.js";
import { loadFigureFixture } from "../src/adapters/figure/fixture.js";
import { contentHash } from "../src/security.js";

const fixtureDir = path.join(process.cwd(), "test/fixtures/figure/demo-figure");

test("real sealed Figure fixture builds stable chemistry links and counts", async () => {
  const loaded = await loadFigureFixture(fixtureDir);
  const { manifest } = loaded;
  assert.equal(manifest.schemaVersion, "chemrxn.figure-job.v1");
  assert.equal(manifest.gtIncluded, false);
  assert.deepEqual(manifest.counts, {
    examples: 24,
    templates: 1,
    successful: 24,
    failed: 0,
    unresolved: 0,
    partial: 0,
    metrics: 97,
  });
  assert.equal(manifest.cache.molparser.status, "cache_hit");
  assert.equal(manifest.cache.molparser.detectionCount, 27);
  assert.equal(manifest.validation.connectivity, "valid");
  assert.equal(manifest.validation.strictStereo, "assigned");

  const reaction = manifest.reactions.find((item) => item.printedIdentifier === "12" && item.kind === "example");
  assert.ok(reaction);
  assert.match(reaction.id, /^rxn:fig:/);
  assert.ok(reaction.bboxIds.length > 0);
  const product = manifest.molecules.find((item) => item.reactionId === reaction.id && item.role === "product");
  assert.ok(product);
  assert.equal(product.derivationMethod, "ocsr");
  assert.equal(product.strictStereo, "assigned");
  assert.ok(product.bboxIds.every((id) => reaction.bboxIds.includes(id)));
  assert.ok(product.evidenceIds.every((id) => manifest.evidence.some((evidence) => evidence.id === id)));

  const rebuilt = buildFigureManifest(loaded.metadata, loaded.prediction, loaded.molparser);
  assert.deepEqual(rebuilt.reactions.map((item) => item.id), manifest.reactions.map((item) => item.id));
  assert.deepEqual(rebuilt.molecules.map((item) => item.id), manifest.molecules.map((item) => item.id));
});

test("replay events are ordered, idempotent-shaped, cache-aware, and terminal", async () => {
  const { events, manifest } = await loadFigureFixture(fixtureDir);
  assert.equal(events[0]?.type, "job.created");
  assert.equal(events.at(-1)?.type, "job.review_ready");
  assert.deepEqual(events.map((event) => event.seq), events.map((_, index) => index + 1));
  assert.equal(new Set(events.map((event) => event.eventId)).size, events.length);
  assert.equal(new Set(events.map((event) => event.idempotencyKey)).size, events.length);
  assert.equal(events.some((event) => event.type === "molparser.cache_hit"), true);
  assert.equal(events.some((event) => event.type === "molparser.started"), false);
  assert.equal(events.filter((event) => event.type === "reaction.candidate").length, manifest.reactions.length);
  assert.equal(events.filter((event) => event.type === "artifact.created").length, manifest.artifacts.length);
  assert.equal(JSON.stringify(events).includes("GT_SENTINEL"), false);
  assert.equal(JSON.stringify(events).includes("sk-"), false);
});

test("fixture prediction stays byte-identical and hash-verified", async () => {
  const bytesBefore = await readFile(path.join(fixtureDir, "pred.v2.json"));
  const loaded = await loadFigureFixture(fixtureDir);
  const bytesAfter = await readFile(path.join(fixtureDir, "pred.v2.json"));
  assert.deepEqual(bytesAfter, bytesBefore);
  assert.equal(contentHash(bytesAfter), loaded.metadata.prediction.sha256);
});

test("event emitter deduplicates retries and rejects events after terminal state", () => {
  const emitter = new FigureEventEmitter(
    "job-a",
    "attempt-a",
    () => "2000-01-01T00:00:00.000Z",
  );
  const first = emitter.emit({ type: "job.created", idempotencyKey: "create" });
  const duplicate = emitter.emit({ type: "job.created", idempotencyKey: "create" });
  assert.deepEqual(duplicate, first);
  assert.equal(emitter.events().length, 1);
  emitter.requestCancel("user request");
  const cancelled = emitter.cancel(["artifact:cache"]);
  assert.equal(emitter.events().at(-1)?.type, "job.cancelled");
  assert.deepEqual(emitter.cancel(["artifact:cache"]), cancelled);
  assert.throws(() => emitter.emit({ type: "artifact.created" }), /after terminal event/);
});

test("outcome states remain separate and derived molecules do not gain fake bboxes", async () => {
  const loaded = await loadFigureFixture(fixtureDir);
  const metadata: FigureFixtureMetadata = {
    ...loaded.metadata,
    jobId: "state-mapping",
    attemptId: "attempt-001",
    terminalStatus: "partial",
  };
  const prediction = {
    reaction_templates: [{ template_id: "T1", reactants: [{ "E-SMILES": "*C" }], products: [{ "E-SMILES": "*CO" }] }],
    selected_examples: [
      {
        instance_id: "ok",
        reactants: [{ "E-SMILES": "CC", _source: { derivation: { method: "deterministic_reverse" } } }],
        products: [{ "E-SMILES": "CO", _source: { bbox_index: 3, bbox: [119, 347, 384, 630], origin: "molparser" } }],
      },
      {
        instance_id: "failed",
        reactants: [{ "E-SMILES": "CN", _source: { bbox_index: 4, bbox: [1518, 349, 1786, 675] } }],
        products: [],
        _outcome: { is_failed: true },
      },
      { instance_id: "unknown", reactants: [{ "E-SMILES": "CO" }], products: [] },
      { instance_id: "partial", reactants: [{ "E-SMILES": "CS" }], products: [{ identifier: "missing" }] },
    ],
  };
  const manifest = buildFigureManifest(metadata, prediction, loaded.molparser);
  const byIdentifier = new Map(manifest.reactions.map((item) => [item.printedIdentifier, item]));
  assert.equal(byIdentifier.get("ok")?.outcome, "successful");
  assert.equal(byIdentifier.get("failed")?.outcome, "failed");
  assert.equal(byIdentifier.get("unknown")?.outcome, "unresolved");
  assert.equal(byIdentifier.get("partial")?.outcome, "unresolved");
  const derived = manifest.molecules.find((item) => item.reactionId === byIdentifier.get("ok")?.id && item.role === "reactant");
  assert.equal(derived?.derivationMethod, "deterministic_reverse");
  assert.deepEqual(derived?.bboxIds, []);
});

test("fixture loader rejects path escape, hash drift, and reference sentinel", async () => {
  const rawMetadata = JSON.parse(await readFile(path.join(fixtureDir, "fixture.json"), "utf8")) as FigureFixtureMetadata;
  const prediction = JSON.parse(await readFile(path.join(fixtureDir, "pred.v2.json"), "utf8"));
  const molparser = JSON.parse(await readFile(path.join(fixtureDir, "molparser.json"), "utf8"));
  assert.throws(
    () => buildFigureManifest({ ...rawMetadata, notes: ["GT_SENTINEL"] }, prediction, molparser),
    /Forbidden reference token/,
  );

  const temporary = await mkdtemp(path.join(tmpdir(), "chemrxn-figure-fixture-"));
  try {
    await cp(fixtureDir, temporary, { recursive: true });
    await chmod(path.join(temporary, "pred.v2.json"), 0o600);
    await chmod(path.join(temporary, "fixture.json"), 0o600);
    await writeFile(path.join(temporary, "pred.v2.json"), `${JSON.stringify(prediction)}\nDRIFT`);
    await assert.rejects(loadFigureFixture(temporary), /fixture hash mismatch/);

    await writeFile(path.join(temporary, "pred.v2.json"), JSON.stringify(prediction));
    const escaped = {
      ...rawMetadata,
      prediction: { ...rawMetadata.prediction, locator: "fixture:../outside.json" },
    };
    await writeFile(path.join(temporary, "fixture.json"), JSON.stringify(escaped));
    await assert.rejects(loadFigureFixture(temporary), /escapes fixture directory/);
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});
