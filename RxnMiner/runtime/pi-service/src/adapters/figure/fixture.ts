import { readFile } from "node:fs/promises";
import path from "node:path";
import { contentHash } from "../../security.js";
import { buildFigureManifest } from "./adapter.js";
import type { FigureEvent, FigureFixtureMetadata, FigureJobManifest } from "./contracts.js";
import { buildFigureReplayEvents } from "./replay.js";

export interface LoadedFigureFixture {
  metadata: FigureFixtureMetadata;
  manifest: FigureJobManifest;
  events: FigureEvent[];
  prediction: unknown;
  molparser: unknown;
}

export async function loadFigureFixture(directory: string): Promise<LoadedFigureFixture> {
  const metadata = await readJson<FigureFixtureMetadata>(path.join(directory, "fixture.json"));
  const predictionPath = resolveFixturePath(directory, metadata.prediction.locator);
  const molparserPath = resolveFixturePath(directory, metadata.molparser.locator);
  const predictionBytes = await readFile(predictionPath);
  const molparserBytes = await readFile(molparserPath);
  verifyHash("prediction", predictionBytes, metadata.prediction.sha256);
  verifyHash("molparser", molparserBytes, metadata.molparser.sha256);
  const prediction = parseJson(predictionBytes, predictionPath);
  const molparser = parseJson(molparserBytes, molparserPath);
  rejectForbiddenReference(prediction, "prediction");
  rejectForbiddenReference(molparser, "molparser");
  const manifest = buildFigureManifest(metadata, prediction, molparser);
  const events = buildFigureReplayEvents(metadata, manifest);
  return { metadata, manifest, events, prediction, molparser };
}

function resolveFixturePath(directory: string, locator: string): string {
  if (!locator.startsWith("fixture:")) {
    throw new Error(`Replay fixture locator must use fixture: scheme, got ${locator}`);
  }
  const relative = locator.slice("fixture:".length);
  const root = path.resolve(directory);
  const resolved = path.resolve(root, relative);
  if (resolved !== root && !resolved.startsWith(`${root}${path.sep}`)) {
    throw new Error(`Fixture locator escapes fixture directory: ${locator}`);
  }
  return resolved;
}

async function readJson<T>(filePath: string): Promise<T> {
  return parseJson(await readFile(filePath), filePath) as T;
}

function parseJson(bytes: Buffer, filePath: string): unknown {
  try {
    return JSON.parse(bytes.toString("utf8"));
  } catch (error) {
    throw new Error(`Invalid JSON fixture file ${filePath}: ${error instanceof Error ? error.message : String(error)}`);
  }
}

function verifyHash(label: string, bytes: Buffer, expected: string): void {
  const actual = contentHash(bytes);
  if (actual !== expected) throw new Error(`${label} fixture hash mismatch: expected ${expected}, got ${actual}`);
}

function rejectForbiddenReference(value: unknown, label: string): void {
  const serialized = JSON.stringify(value).toLowerCase();
  for (const token of ["gt_sentinel", "ground_truth", "official_gt"]) {
    if (serialized.includes(token)) throw new Error(`${label} contains forbidden reference token ${token}`);
  }
}
