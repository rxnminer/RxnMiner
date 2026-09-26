export type TaskKind = "extract_all" | "ask_pdf" | "inspect_selection" | "propose_patch";
export type ContextMode = "figure" | "paper" | "paper_si";
export type AuthMode = "api_key" | "enterprise_access_token" | "personal_codex_subscription";
export type LocalInputKind = "figure" | "paper" | "si";
export const PI_THINKING_LEVELS = [
  "off", "minimal", "low", "medium", "high", "xhigh", "max",
] as const;
export type PiThinkingLevel = typeof PI_THINKING_LEVELS[number];
export const DEFAULT_EXTRACTION_THINKING_LEVEL: PiThinkingLevel = "medium";
export const RUNTIME_EVENT_TYPES = [
  "session.created", "session.ready", "session.dormant", "session.blocked",
  "auth.required", "auth.succeeded", "auth.failed", "auth.logged_out",
  "agent.event", "agent.error", "agent.aborted", "patch.proposed",
  "review.outcome",
  "job.review_ready", "job.partial", "job.cancelled", "job.interrupted", "job.failed",
] as const;
export type RuntimeEventType = typeof RUNTIME_EVENT_TYPES[number];
export const SESSION_STATUSES = [
  "creating", "awaiting_user_auth", "ready", "dormant", "running", "blocked", "failed", "aborted",
  "review_ready", "partial", "cancelled", "interrupted",
] as const;
export type SessionStatus = typeof SESSION_STATUSES[number];

export const SEALED_REPLAY_FIXTURES = [
  "demo-figure",
] as const;
export type SealedReplayFixture = typeof SEALED_REPLAY_FIXTURES[number];

export interface ArtifactRef {
  id: string;
  kind: "figure" | "paper" | "si" | "selection" | "prediction" | "reviewed_snapshot";
  sha256: string;
  mediaType: string;
}

export interface LocalInputArtifact {
  id: string;
  userId: string;
  kind: LocalInputKind;
  originalName: string;
  mediaType: string;
  sha256: string;
  byteSize: number;
  createdAt: string;
}

export interface LocalInputSelection {
  artifactId: string;
  kind: LocalInputKind;
}

export interface CreateSessionRequest {
  userId: string;
  jobId: string;
  task: TaskKind;
  contextMode: ContextMode;
  artifacts: ArtifactRef[];
  auth: {
    mode: AuthMode;
    provider: string;
    credentialRef: string;
  };
  model: {
    provider: string;
    id: string;
    thinkingLevel?: PiThinkingLevel;
  };
}

export interface MountManifest {
  sessionId: string;
  userId: string;
  jobId: string;
  contextMode: ContextMode;
  mounts: Array<ArtifactRef & { mountPath: string; readOnly: true }>;
  gtIncluded: false;
}

export interface RuntimeEvent {
  seq: number;
  sessionId: string;
  at: string;
  type: RuntimeEventType;
  data: unknown;
}

export type ReviewOutcomeStatus =
  | "staged"
  | "applied"
  | "discarded"
  | "expired"
  | "rejected";

export type ReviewOutcomeReason =
  | "human_edits_present"
  | "superseded"
  | "matches_review_head"
  | "preview_failed"
  | "human_edit_replaced"
  | "review_head_changed"
  | "human_closed"
  | "workspace_target_mismatch"
  | "reaction_not_found"
  | "benchmark_read_only"
  | "proposal_not_stageable"
  | "human_navigation"
  | "human_review_replaced"
  | "human_discarded";

export interface ReviewOutcomeRequest {
  proposalId: string;
  jobId: string;
  target: "figure" | "pdf";
  status: ReviewOutcomeStatus;
  reviewedVersion?: number;
  patchId?: string;
  reason?: ReviewOutcomeReason;
}

export interface SessionRecord {
  id: string;
  userId: string;
  jobId: string;
  task: TaskKind;
  contextMode?: ContextMode;
  inputNames?: string[];
  parentSessionId?: string;
  parentResultSha256?: string;
  status: SessionStatus;
  authMode: AuthMode | "sealed_replay";
  thinkingLevel?: PiThinkingLevel;
  sandboxId?: string;
  createdAt: string;
  updatedAt: string;
  blockReason?: string;
}

export interface RuntimeJobResult {
  schemaVersion: "chemrxn.runtime-result.v1";
  sessionId: string;
  jobId: string;
  contextMode: ContextMode;
  source: {
    mode: "sealed_replay";
    fixture: SealedReplayFixture;
    reconstructed: true;
    liveEventCapture: boolean;
  } | {
    mode: "live_pi_sdk";
    fixture?: never;
    reconstructed: false;
    liveEventCapture: true;
    inputArtifactIds: string[];
    workspaceId: string;
  };
  domainContract: "chemrxn.figure-job.v1" | "chemrxn.pdf-replay.v1";
  terminalStatus: Extract<
    SessionStatus,
    "review_ready" | "partial" | "cancelled" | "interrupted" | "failed"
  >;
  manifest: unknown;
  createdAt: string;
  gtIncluded: false;
  viewer?: {
    kind: "figure" | "document";
    inputArtifactId: string;
    predictionArtifactId?: string;
    overviewArtifactId?: string;
    reactionsArtifactId?: string;
  };
}

export interface CredentialBroker {
  redeem(input: {
    userId: string;
    credentialRef: string;
    mode: Exclude<AuthMode, "personal_codex_subscription">;
    provider: string;
  }): Promise<{ value: string; expiresAt?: string }>;
}

export interface ProposedPatch {
  id: string;
  userId: string;
  jobId: string;
  sessionId: string;
  baseSha256: string;
  status: "proposed" | "applied" | "rejected";
  operations: Array<{
    op: "replace" | "add" | "remove";
    path: string;
    value?: unknown;
    evidenceIds: string[];
    rationale: string;
  }>;
  createdAt: string;
}

export interface ReviewedSnapshot {
  id: string;
  userId: string;
  jobId: string;
  version: number;
  parentSha256: string;
  sha256: string;
  patchId: string;
  createdAt: string;
  document: unknown;
}

export interface ArtifactResolver {
  materialize(input: {
    userId: string;
    jobId: string;
    sandboxId: string;
    mounts: MountManifest["mounts"];
  }): Promise<void>;
}
