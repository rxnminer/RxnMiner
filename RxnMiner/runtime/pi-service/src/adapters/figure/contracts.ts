export const FIGURE_CONTRACT_VERSION = "chemrxn.figure-job.v1" as const;

export type FigureEventType =
  | "job.created"
  | "input.validated"
  | "perception.started"
  | "perception.completed"
  | "molparser.cache_hit"
  | "molparser.started"
  | "molparser.completed"
  | "agent.started"
  | "agent.message"
  | "tool.started"
  | "tool.completed"
  | "tool.failed"
  | "reaction.candidate"
  | "artifact.created"
  | "validation.started"
  | "validation.completed"
  | "job.partial"
  | "job.review_ready"
  | "job.cancel_requested"
  | "job.cancelled"
  | "job.failed";

export type FigureTerminalEventType = "job.partial" | "job.review_ready" | "job.cancelled" | "job.failed";
export type FigureOutcomeClass = "successful" | "failed" | "template" | "unresolved" | "partial";
export type FigureDerivationMethod =
  | "drawn"
  | "ocsr"
  | "deterministic_reverse"
  | "model_inferred"
  | "human_reviewed"
  | "unknown";
export type ConnectivityState = "valid" | "invalid" | "unknown";
export type StrictStereoState = "assigned" | "unassigned" | "conflict" | "not_applicable" | "unknown";

export interface FigureProgress {
  completed: number;
  total: number;
  unit: "detections" | "reactions" | "tools" | "artifacts";
}

export interface FigureEvent {
  schemaVersion: typeof FIGURE_CONTRACT_VERSION;
  eventId: string;
  idempotencyKey: string;
  seq: number;
  jobId: string;
  attemptId: string;
  at: string;
  source: "figure-adapter" | "sealed-replay" | "pi" | "chemistry-tool";
  type: FigureEventType;
  replaySafe: true;
  progress?: FigureProgress;
  artifactIds: string[];
  data: Record<string, unknown>;
}

export interface FigureInputRef {
  figureId: string;
  sha256: string;
  mediaType: string;
  locator: string;
  locatorScope: "external_manifest" | "job_mount" | "fixture";
}

export interface FigureBBoxRef {
  id: string;
  figureId: string;
  coordinateSpace: "figure_pixel";
  bboxIndex: number | null;
  bbox: [number, number, number, number] | null;
  evidenceId: string | null;
}

export interface FigureEvidenceRef {
  id: string;
  kind: "molparser_detection" | "figure_pixels" | "tool_trace" | "validation" | "human_adjudication";
  figureId: string;
  sha256?: string;
  bboxId?: string;
  locator?: string;
  postHoc: boolean;
}

export interface FigureMoleculeOccurrence {
  id: string;
  reactionId: string;
  role: "reactant" | "product" | "condition";
  ordinal: number;
  identifier: string | null;
  esmiles: string | null;
  derivationMethod: FigureDerivationMethod;
  bboxIds: string[];
  evidenceIds: string[];
  connectivity: ConnectivityState;
  strictStereo: StrictStereoState;
}

export interface FigureReactionRef {
  id: string;
  kind: "example" | "template";
  legacyIndex: number;
  printedIdentifier: string | null;
  outcome: FigureOutcomeClass;
  moleculeIds: string[];
  bboxIds: string[];
  evidenceIds: string[];
  metricCount: number;
  conditionCount: number;
  unresolvedReasons: string[];
}

export interface FigureArtifactRef {
  id: string;
  kind:
    | "input_figure"
    | "molparser_raw"
    | "prediction_v2"
    | "validation"
    | "overview_render"
    | "reaction_render"
    | "tool_trace"
    | "posthoc_review";
  sha256: string;
  mediaType: string;
  locator: string;
  immutable: true;
  reusableAfterCancel: boolean;
  postHoc: boolean;
}

export interface FigureValidationSummary {
  connectivity: ConnectivityState;
  strictStereo: StrictStereoState;
  basis: string;
  issueCount: number;
  evidenceIds: string[];
}

export interface FigureCacheSummary {
  molparser: {
    status: "cache_hit" | "live_call" | "unavailable";
    cacheKey: string | null;
    sha256: string | null;
    detectionCount: number;
  };
}

export interface FigureJobManifest {
  schemaVersion: typeof FIGURE_CONTRACT_VERSION;
  jobId: string;
  attemptId: string;
  fixtureId?: string;
  request: {
    task: "extract_all" | "inspect_selection" | "propose_patch";
    contextMode: "figure";
    inferencePolicy: "drawn_only" | "deterministic_reverse_allowed";
  };
  input: FigureInputRef;
  skill: {
    commit: string;
    name: "reaction-extract";
  };
  model: {
    provider: string;
    id: string;
  };
  cache: FigureCacheSummary;
  reactions: FigureReactionRef[];
  molecules: FigureMoleculeOccurrence[];
  bboxes: FigureBBoxRef[];
  evidence: FigureEvidenceRef[];
  artifacts: FigureArtifactRef[];
  validation: FigureValidationSummary;
  counts: {
    examples: number;
    templates: number;
    successful: number;
    failed: number;
    unresolved: number;
    partial: number;
    metrics: number;
  };
  terminalStatus: "review_ready" | "partial" | "cancelled" | "failed";
  gtIncluded: false;
}

export interface FigureFixtureMetadata {
  fixtureId?: string;
  jobId: string;
  attemptId: string;
  input: FigureInputRef;
  request: FigureJobManifest["request"];
  skill: FigureJobManifest["skill"];
  model: FigureJobManifest["model"];
  molparser: {
    locator: string;
    sha256: string;
    cacheKey: string;
    status: "cache_hit" | "live_call";
  };
  prediction: {
    locator: string;
    sha256: string;
  };
  toolSequence: string[];
  startedAt: string;
  terminalStatus: FigureJobManifest["terminalStatus"];
  validation: FigureValidationSummary;
  notes: string[];
}
