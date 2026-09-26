import type { FigureEvent, FigureFixtureMetadata, FigureJobManifest } from "./contracts.js";
import { FigureEventEmitter } from "./events.js";

export function buildFigureReplayEvents(
  metadata: FigureFixtureMetadata,
  manifest: FigureJobManifest,
): FigureEvent[] {
  if (manifest.gtIncluded !== false) throw new Error("Figure replay must not include GT");
  let tick = 0;
  const base = Date.parse(metadata.startedAt);
  if (!Number.isFinite(base)) throw new Error(`Invalid fixture startedAt: ${metadata.startedAt}`);
  const clock = () => new Date(base + tick++ * 10).toISOString();
  const emitter = new FigureEventEmitter(metadata.jobId, metadata.attemptId, clock, "sealed-replay");

  emitter.emit({
    type: "job.created",
    data: {
      fixtureId: metadata.fixtureId,
      task: metadata.request.task,
      contextMode: "figure",
      inferencePolicy: metadata.request.inferencePolicy,
    },
    idempotencyKey: "job.created",
  });
  emitter.emit({
    type: "input.validated",
    data: {
      figureId: manifest.input.figureId,
      sha256: manifest.input.sha256,
      mediaType: manifest.input.mediaType,
      gtIncluded: false,
    },
    artifactIds: manifest.artifacts.filter((item) => item.kind === "input_figure").map((item) => item.id),
    idempotencyKey: "input.validated",
  });
  emitter.emit({ type: "perception.started", idempotencyKey: "perception.started" });
  if (manifest.cache.molparser.status === "cache_hit") {
    emitter.emit({
      type: "molparser.cache_hit",
      data: {
        cacheKey: manifest.cache.molparser.cacheKey,
        sha256: manifest.cache.molparser.sha256,
        detectionCount: manifest.cache.molparser.detectionCount,
      },
      artifactIds: manifest.artifacts.filter((item) => item.kind === "molparser_raw").map((item) => item.id),
      progress: {
        completed: manifest.cache.molparser.detectionCount,
        total: manifest.cache.molparser.detectionCount,
        unit: "detections",
      },
      idempotencyKey: "molparser.cache_hit",
    });
  } else if (manifest.cache.molparser.status === "live_call") {
    emitter.emit({ type: "molparser.started", idempotencyKey: "molparser.started" });
    emitter.emit({
      type: "molparser.completed",
      data: { detectionCount: manifest.cache.molparser.detectionCount },
      artifactIds: manifest.artifacts.filter((item) => item.kind === "molparser_raw").map((item) => item.id),
      progress: {
        completed: manifest.cache.molparser.detectionCount,
        total: manifest.cache.molparser.detectionCount,
        unit: "detections",
      },
      idempotencyKey: "molparser.completed",
    });
  }
  emitter.emit({
    type: "perception.completed",
    data: {
      detectionCount: manifest.cache.molparser.detectionCount,
      bboxCount: manifest.bboxes.length,
    },
    idempotencyKey: "perception.completed",
  });
  emitter.emit({
    type: "agent.started",
    data: {
      provider: metadata.model.provider,
      model: metadata.model.id,
      skillCommit: metadata.skill.commit,
    },
    idempotencyKey: "agent.started",
  });

  metadata.toolSequence.forEach((tool, index) => {
    const callId = `tool:${String(index + 1).padStart(4, "0")}:${tool}`;
    emitter.emit({
      type: "tool.started",
      source: "chemistry-tool",
      data: { callId, tool },
      progress: { completed: index, total: metadata.toolSequence.length, unit: "tools" },
      idempotencyKey: `${callId}:started`,
    });
    emitter.emit({
      type: "tool.completed",
      source: "chemistry-tool",
      data: { callId, tool, status: "completed" },
      progress: { completed: index + 1, total: metadata.toolSequence.length, unit: "tools" },
      idempotencyKey: `${callId}:completed`,
    });
  });

  manifest.reactions.forEach((reaction, index) => {
    emitter.emit({
      type: "reaction.candidate",
      data: {
        reactionId: reaction.id,
        kind: reaction.kind,
        outcome: reaction.outcome,
        printedIdentifier: reaction.printedIdentifier,
        moleculeIds: reaction.moleculeIds,
        bboxIds: reaction.bboxIds,
      },
      progress: { completed: index + 1, total: manifest.reactions.length, unit: "reactions" },
      idempotencyKey: `reaction.candidate:${reaction.id}`,
    });
  });

  manifest.artifacts.forEach((artifact, index) => {
    emitter.emit({
      type: "artifact.created",
      data: {
        artifactId: artifact.id,
        kind: artifact.kind,
        sha256: artifact.sha256,
        mediaType: artifact.mediaType,
        reusableAfterCancel: artifact.reusableAfterCancel,
        postHoc: artifact.postHoc,
      },
      artifactIds: [artifact.id],
      progress: { completed: index + 1, total: manifest.artifacts.length, unit: "artifacts" },
      idempotencyKey: `artifact.created:${artifact.id}`,
    });
  });

  emitter.emit({ type: "validation.started", idempotencyKey: "validation.started" });
  emitter.emit({
    type: "validation.completed",
    data: {
      connectivity: manifest.validation.connectivity,
      strictStereo: manifest.validation.strictStereo,
      basis: manifest.validation.basis,
      issueCount: manifest.validation.issueCount,
      evidenceIds: manifest.validation.evidenceIds,
    },
    idempotencyKey: "validation.completed",
  });

  const terminalArtifacts = manifest.artifacts
    .filter((item) => item.kind === "prediction_v2" || item.kind === "validation")
    .map((item) => item.id);
  if (metadata.terminalStatus === "review_ready") {
    emitter.emit({
      type: "job.review_ready",
      data: { counts: manifest.counts, validation: manifest.validation },
      artifactIds: terminalArtifacts,
      idempotencyKey: "job.review_ready",
    });
  } else if (metadata.terminalStatus === "partial") {
    emitter.emit({
      type: "job.partial",
      data: { counts: manifest.counts, unresolved: manifest.counts.unresolved },
      artifactIds: manifest.artifacts.filter((item) => item.reusableAfterCancel).map((item) => item.id),
      idempotencyKey: "job.partial",
    });
  } else if (metadata.terminalStatus === "cancelled") {
    emitter.requestCancel("fixture cancellation");
    emitter.cancel(manifest.artifacts.filter((item) => item.reusableAfterCancel).map((item) => item.id));
  } else {
    emitter.emit({
      type: "job.failed",
      data: { code: "FIGURE_PIPELINE_FAILED" },
      artifactIds: manifest.artifacts.filter((item) => item.reusableAfterCancel).map((item) => item.id),
      idempotencyKey: "job.failed",
    });
  }
  return emitter.events();
}
