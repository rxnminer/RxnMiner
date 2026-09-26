import { createHash } from "node:crypto";
import {
  FIGURE_CONTRACT_VERSION,
  type ConnectivityState,
  type FigureArtifactRef,
  type FigureBBoxRef,
  type FigureDerivationMethod,
  type FigureEvidenceRef,
  type FigureFixtureMetadata,
  type FigureJobManifest,
  type FigureMoleculeOccurrence,
  type FigureOutcomeClass,
  type FigureReactionRef,
  type StrictStereoState,
} from "./contracts.js";
import {
  artifactId,
  bboxId,
  detectionId,
  evidenceId,
  figureId,
  moleculeId,
  reactionId,
} from "./ids.js";

type JsonRecord = Record<string, unknown>;

export function buildFigureManifest(
  metadata: FigureFixtureMetadata,
  predictionValue: unknown,
  molparserValue: unknown,
): FigureJobManifest {
  assertNoReferenceLeak(metadata);
  const prediction = record(predictionValue);
  const detections = array(molparserValue).map(record);
  const figId = figureId(metadata.input.sha256);
  if (metadata.input.figureId !== figId) {
    throw new Error(`Figure ID ${metadata.input.figureId} does not match input hash (${figId})`);
  }

  const bboxes = new Map<string, FigureBBoxRef>();
  const evidence = new Map<string, FigureEvidenceRef>();
  const detectionEvidenceByIndex = new Map<number, string>();
  for (const detection of detections) {
    const index = integer(detection.bbox_index);
    if (index === null) continue;
    const box = bboxTuple(detection.bbox);
    const boxId = bboxId(figId, index, box);
    const evId = evidenceId(figId, "molparser", index);
    bboxes.set(boxId, {
      id: boxId,
      figureId: figId,
      coordinateSpace: "figure_pixel",
      bboxIndex: index,
      bbox: box,
      evidenceId: evId,
    });
    evidence.set(evId, {
      id: evId,
      kind: "molparser_detection",
      figureId: figId,
      bboxId: boxId,
      postHoc: false,
    });
    detectionEvidenceByIndex.set(index, evId);
  }

  const molecules: FigureMoleculeOccurrence[] = [];
  const reactions: FigureReactionRef[] = [];
  const duplicateCounts = new Map<string, number>();
  let metricCount = 0;

  const addReaction = (value: unknown, kind: "example" | "template", legacyIndex: number): void => {
    const reaction = record(value);
    const printedIdentifier = text(kind === "template" ? reaction.template_id : reaction.instance_id)
      ?? text(reaction.identifier);
    const duplicateKey = `${kind}:${printedIdentifier ?? ""}`;
    const duplicateOrdinal = (duplicateCounts.get(duplicateKey) ?? 0) + 1;
    duplicateCounts.set(duplicateKey, duplicateOrdinal);
    const rxnId = reactionId(figId, kind, printedIdentifier, duplicateOrdinal, legacyIndex);
    const unresolvedReasons: string[] = [];
    const outcome = outcomeClass(reaction, kind, unresolvedReasons);
    const moleculeIds: string[] = [];
    const reactionBboxIds = new Set<string>();
    const reactionEvidenceIds = new Set<string>();

    const addMolecules = (role: "reactant" | "product" | "condition", values: unknown[]): void => {
      for (let ordinal = 0; ordinal < values.length; ordinal++) {
        const molecule = record(values[ordinal]);
        const source = record(molecule._source);
        const esmiles = text(molecule["E-SMILES"]) ?? text(molecule.smiles);
        const occurrenceId = moleculeId(rxnId, role, ordinal);
        const boxIndex = integer(source.bbox_index ?? molecule.bbox_index);
        const box = bboxTuple(source.bbox ?? molecule.bbox);
        const occurrenceBboxIds: string[] = [];
        const occurrenceEvidenceIds: string[] = [];
        if (boxIndex !== null || box) {
          const id = bboxId(figId, boxIndex, box);
          const existingEvidence = boxIndex === null ? undefined : detectionEvidenceByIndex.get(boxIndex);
          if (!bboxes.has(id)) {
            bboxes.set(id, {
              id,
              figureId: figId,
              coordinateSpace: "figure_pixel",
              bboxIndex: boxIndex,
              bbox: box,
              evidenceId: existingEvidence ?? null,
            });
          }
          occurrenceBboxIds.push(id);
          reactionBboxIds.add(id);
          if (existingEvidence) {
            occurrenceEvidenceIds.push(existingEvidence);
            reactionEvidenceIds.add(existingEvidence);
          }
        }
        if (!esmiles && role !== "condition") unresolvedReasons.push(`${role}[${ordinal}] has no E-SMILES`);
        const occurrence: FigureMoleculeOccurrence = {
          id: occurrenceId,
          reactionId: rxnId,
          role,
          ordinal,
          identifier: text(molecule.identifier),
          esmiles,
          derivationMethod: derivationMethod(source),
          bboxIds: occurrenceBboxIds,
          evidenceIds: occurrenceEvidenceIds,
          connectivity: metadata.validation.connectivity,
          strictStereo: stereoState(esmiles, metadata.validation.strictStereo),
        };
        molecules.push(occurrence);
        moleculeIds.push(occurrenceId);
      }
    };

    addMolecules("reactant", array(reaction.reactants));
    addMolecules("product", array(reaction.products));
    const conditionEntries = array(reaction.conditions).flatMap((step) => array(step));
    addMolecules("condition", conditionEntries.filter((entry) => hasMolecule(entry)));

    const metrics = array(reaction.metrics);
    metricCount += metrics.length;
    reactions.push({
      id: rxnId,
      kind,
      legacyIndex,
      printedIdentifier,
      outcome: unresolvedReasons.length > 0 && outcome === "successful" ? "unresolved" : outcome,
      moleculeIds,
      bboxIds: [...reactionBboxIds],
      evidenceIds: [...reactionEvidenceIds],
      metricCount: metrics.length,
      conditionCount: conditionEntries.length,
      unresolvedReasons: unique(unresolvedReasons),
    });
  };

  array(prediction.reaction_templates).forEach((item, index) => addReaction(item, "template", index));
  array(prediction.selected_examples).forEach((item, index) => addReaction(item, "example", index));

  const artifacts = buildArtifacts(metadata);
  const validationEvidenceId = evidenceId(figId, "validation", metadata.prediction.sha256.slice(0, 12));
  evidence.set(validationEvidenceId, {
    id: validationEvidenceId,
    kind: "validation",
    figureId: figId,
    postHoc: false,
  });

  const examples = reactions.filter((item) => item.kind === "example");
  return {
    schemaVersion: FIGURE_CONTRACT_VERSION,
    jobId: metadata.jobId,
    attemptId: metadata.attemptId,
    ...(metadata.fixtureId ? { fixtureId: metadata.fixtureId } : {}),
    request: structuredClone(metadata.request),
    input: structuredClone(metadata.input),
    skill: structuredClone(metadata.skill),
    model: structuredClone(metadata.model),
    cache: {
      molparser: {
        status: metadata.molparser.status,
        cacheKey: metadata.molparser.cacheKey,
        sha256: metadata.molparser.sha256,
        detectionCount: detections.length,
      },
    },
    reactions,
    molecules,
    bboxes: [...bboxes.values()],
    evidence: [...evidence.values()],
    artifacts,
    validation: {
      ...structuredClone(metadata.validation),
      evidenceIds: unique([...metadata.validation.evidenceIds, validationEvidenceId]),
    },
    counts: {
      examples: examples.length,
      templates: reactions.length - examples.length,
      successful: examples.filter((item) => item.outcome === "successful").length,
      failed: examples.filter((item) => item.outcome === "failed").length,
      unresolved: examples.filter((item) => item.outcome === "unresolved").length,
      partial: examples.filter((item) => item.outcome === "partial").length,
      metrics: metricCount,
    },
    terminalStatus: metadata.terminalStatus,
    gtIncluded: false,
  };
}

function buildArtifacts(metadata: FigureFixtureMetadata): FigureArtifactRef[] {
  const toolTraceHash = hashJson(metadata.toolSequence);
  const validationHash = hashJson(metadata.validation);
  return [
    {
      id: artifactId(metadata.jobId, "input-figure", metadata.input.sha256),
      kind: "input_figure",
      sha256: metadata.input.sha256,
      mediaType: metadata.input.mediaType,
      locator: metadata.input.locator,
      immutable: true,
      reusableAfterCancel: true,
      postHoc: false,
    },
    {
      id: artifactId(metadata.jobId, "molparser-raw", metadata.molparser.sha256),
      kind: "molparser_raw",
      sha256: metadata.molparser.sha256,
      mediaType: "application/json",
      locator: metadata.molparser.locator,
      immutable: true,
      reusableAfterCancel: true,
      postHoc: false,
    },
    {
      id: artifactId(metadata.jobId, "prediction-v2", metadata.prediction.sha256),
      kind: "prediction_v2",
      sha256: metadata.prediction.sha256,
      mediaType: "application/json",
      locator: metadata.prediction.locator,
      immutable: true,
      reusableAfterCancel: metadata.terminalStatus === "review_ready",
      postHoc: false,
    },
    {
      id: artifactId(metadata.jobId, "validation", validationHash),
      kind: "validation",
      sha256: validationHash,
      mediaType: "application/json",
      locator: "inline:manifest.validation",
      immutable: true,
      reusableAfterCancel: true,
      postHoc: false,
    },
    {
      id: artifactId(metadata.jobId, "tool-trace", toolTraceHash),
      kind: "tool_trace",
      sha256: toolTraceHash,
      mediaType: "application/json",
      locator: "inline:fixture.toolSequence",
      immutable: true,
      reusableAfterCancel: true,
      postHoc: false,
    },
  ];
}

function outcomeClass(reaction: JsonRecord, kind: "example" | "template", reasons: string[]): FigureOutcomeClass {
  if (kind === "template") return "template";
  const outcome = record(reaction._outcome);
  if (outcome.is_failed === true || text(outcome.status) === "failed") return "failed";
  if (text(outcome.status) === "partial") return "partial";
  if (text(outcome.status) === "unresolved") return "unresolved";
  if (array(reaction.products).length === 0) {
    reasons.push("reaction has no product and is not explicitly marked failed");
    return "unresolved";
  }
  return "successful";
}

function derivationMethod(source: JsonRecord): FigureDerivationMethod {
  const explicit = text(record(source.derivation).method) ?? text(source.derivation_method);
  if (explicit && ["drawn", "ocsr", "deterministic_reverse", "model_inferred", "human_reviewed"].includes(explicit)) {
    return explicit as FigureDerivationMethod;
  }
  const origin = (text(source.origin) ?? "").toLowerCase();
  if (origin === "molparser" || origin === "ocsr") return "ocsr";
  if (["fhr", "reverse", "markush", "enumerated"].some((value) => origin.includes(value))) {
    return "deterministic_reverse";
  }
  if (origin === "reviewed" || origin === "human") return "human_reviewed";
  if (source._bbox_inherited || origin === "agent" || origin === "model") return "model_inferred";
  if (source.type === "image_structure") return "drawn";
  return "unknown";
}

function stereoState(esmiles: string | null, overall: StrictStereoState): StrictStereoState {
  if (!esmiles) return "unknown";
  if (!/[@\\/]/.test(esmiles)) return "not_applicable";
  if (overall === "conflict" || overall === "unassigned") return overall;
  return "assigned";
}

function hasMolecule(value: unknown): boolean {
  const item = record(value);
  return typeof item["E-SMILES"] === "string" || typeof item.smiles === "string";
}

function assertNoReferenceLeak(value: unknown): void {
  const serialized = JSON.stringify(value).toLowerCase();
  for (const forbidden of ["gt_sentinel", "ground_truth", "official_gt"]) {
    if (serialized.includes(forbidden)) throw new Error(`Forbidden reference token in Figure fixture metadata: ${forbidden}`);
  }
}

function hashJson(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function record(value: unknown): JsonRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as JsonRecord : {};
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function integer(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function bboxTuple(value: unknown): [number, number, number, number] | null {
  if (!Array.isArray(value) || value.length < 4) return null;
  const coordinates = value.slice(0, 4).map(Number);
  if (coordinates.some((item) => !Number.isFinite(item))) return null;
  return [coordinates[0]!, coordinates[1]!, coordinates[2]!, coordinates[3]!];
}

function unique(values: string[]): string[] {
  return [...new Set(values)];
}
