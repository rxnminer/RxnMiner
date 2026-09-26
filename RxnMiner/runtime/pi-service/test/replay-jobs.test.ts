import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { SealedReplayJobService } from "../src/replay-jobs.js";
import { DurableFileRuntimeStore, InMemoryRuntimeStore } from "../src/store.js";
import { RuntimePolicyError } from "../src/errors.js";

test("Figure sealed replay persists chemistry events and a reviewable result", async () => {
  const store = new InMemoryRuntimeStore();
  const jobs = new SealedReplayJobService(store);
  const created = await jobs.create("user-a", "demo-figure");
  assert.equal(created.session.status, "review_ready");
  assert.equal(created.result.source.reconstructed, true);
  assert.equal(created.result.source.liveEventCapture, false);
  assert.equal(created.result.domainContract, "chemrxn.figure-job.v1");
  assert.equal(created.result.viewer?.kind, "figure");
  const manifest = created.result.manifest as {
    artifacts: Array<{ id: string; kind: string; sha256: string }>;
    counts: { examples: number; templates: number };
    gtIncluded: boolean;
  };
  assert.equal(manifest.counts.examples, 24);
  assert.equal(manifest.counts.templates, 1);
  assert.equal(manifest.gtIncluded, false);
  const events = store.listEvents("user-a", created.session.id);
  assert.equal(events.length, 114);
  assert.equal(events[0]?.type, "session.created");
  assert.equal(events.at(-1)?.type, "job.review_ready");
  assert.equal(
    events.some((event) => (event.data as { phase?: string }).phase === "molparser.cache_hit"),
    true,
  );
  const inputId = manifest.artifacts.find((item) => item.kind === "input_figure")!.id;
  const predictionId = manifest.artifacts.find((item) => item.kind === "prediction_v2")!.id;
  const input = await jobs.artifact("user-a", created.session.id, inputId);
  const prediction = await jobs.artifact("user-a", created.session.id, predictionId);
  assert.equal(input.sha256, "5f957606ef2fd220d75881a681958d58c74833d0a7ffc70525a2aee16f14e29f");
  assert.equal(prediction.sha256, "cf494189ef50574408d19128be0d97d4dd1379e1c76e5b14100d65c767ea8bf8");
  assert.equal(input.mediaType, "image/png");
  assert.equal(JSON.parse(prediction.bytes.toString("utf8")).selected_examples.length, 24);
});
