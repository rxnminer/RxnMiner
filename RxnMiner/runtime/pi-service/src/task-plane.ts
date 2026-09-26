import type {
  RuntimeEvent,
  RuntimeJobResult,
  SessionRecord,
  SessionStatus,
  TaskKind,
} from "./contracts.js";

export const TASK_ENVELOPE_VERSION = "chemrxn.task.v1" as const;
export const TASK_ATTEMPT_ENVELOPE_VERSION = "chemrxn.task-attempt.v1" as const;
export const TASK_EVENT_REFERENCE_VERSION = "chemrxn.task-event-ref.v1" as const;
export const TASK_RESULT_REFERENCE_VERSION = "chemrxn.task-result-ref.v1" as const;

export const TASK_STATES = [
  "created",
  "validating_inputs",
  "parsing_layout",
  "recognizing_structures",
  "extracting_reactions",
  "validating_chemistry",
  "preparing_review",
  "review_ready",
  "blocked",
  "failed",
  "cancelled",
] as const;
export type TaskState = typeof TASK_STATES[number];

export const TASK_ATTEMPT_RELATIONS = [
  "initial",
  "linked",
  "continuation",
  "retry",
] as const;
export type TaskAttemptRelation = typeof TASK_ATTEMPT_RELATIONS[number];

export interface TaskProgress {
  completed: number;
  total: number;
  unit: string;
  denominator?: string;
}

/**
 * Small, versioned task/index envelopes. Large event/result payloads remain in
 * the existing per-session JSON artifacts and are addressed by the references
 * below.
 */
export interface TaskEnvelope {
  schemaVersion: typeof TASK_ENVELOPE_VERSION;
  id: string;
  userId: string;
  rootSessionId: string;
  jobId: string;
  kind: TaskKind;
  state: TaskState;
  activeAttemptId: string;
  attemptCount: number;
  revision: number;
  progress?: TaskProgress;
  createdAt: string;
  updatedAt: string;
}

export interface TaskAttemptEnvelope {
  schemaVersion: typeof TASK_ATTEMPT_ENVELOPE_VERSION;
  id: string;
  taskId: string;
  sessionId: string;
  jobId: string;
  number: number;
  relation: TaskAttemptRelation;
  parentAttemptId?: string;
  runtimeStatus: SessionStatus;
  state: TaskState;
  revision: number;
  lastEventSequence: number;
  resultId?: string;
  progress?: TaskProgress;
  createdAt: string;
  updatedAt: string;
}

export interface TaskEventReference {
  schemaVersion: typeof TASK_EVENT_REFERENCE_VERSION;
  id: string;
  taskId: string;
  attemptId: string;
  sequence: number;
  type: RuntimeEvent["type"];
  occurredAt: string;
  artifact: {
    locator: string;
    sha256: string;
    mediaType: "application/json";
  };
}

export interface TaskResultReference {
  schemaVersion: typeof TASK_RESULT_REFERENCE_VERSION;
  id: string;
  taskId: string;
  attemptId: string;
  runtimeSchemaVersion: RuntimeJobResult["schemaVersion"];
  artifact: {
    locator: string;
    sha256: string;
    mediaType: "application/json";
    immutable: true;
  };
  sealedAt: string;
}

export interface AttemptTransition {
  status: SessionStatus;
  updatedAt: string;
  blockReason?: string | null;
}

const TASK_STATE_ORDER = new Map<TaskState, number>([
  ["created", 0],
  ["validating_inputs", 1],
  ["parsing_layout", 2],
  ["recognizing_structures", 3],
  ["extracting_reactions", 4],
  ["validating_chemistry", 5],
  ["preparing_review", 6],
  ["review_ready", 7],
  ["blocked", 7],
  ["failed", 7],
  ["cancelled", 7],
]);

const TERMINAL_TASK_STATES = new Set<TaskState>(["review_ready", "blocked", "failed", "cancelled"]);

export function taskStateFromSession(
  status: SessionStatus,
  current: TaskState = "created",
): TaskState {
  if (status === "review_ready" || status === "partial") return "review_ready";
  if (status === "cancelled" || status === "aborted") return "cancelled";
  if (status === "blocked") return "blocked";
  if (status === "failed" || status === "interrupted") return "failed";
  if (status === "running" && current === "created") return "validating_inputs";
  return current;
}

export function taskStateFromEvent(event: RuntimeEvent, current: TaskState): TaskState {
  if (TERMINAL_TASK_STATES.has(current)) return current;
  if (event.type === "job.review_ready" || event.type === "job.partial") return "review_ready";
  if (event.type === "job.cancelled" || event.type === "agent.aborted") return "cancelled";
  if (
    event.type === "job.failed"
    || event.type === "job.interrupted"
  ) return "failed";
  if (event.type === "session.blocked") return "blocked";

  const phase = eventPhase(event.data);
  const candidate = phaseState(phase);
  return candidate ? advanceTaskState(current, candidate) : current;
}

export function taskProgressFromEvent(event: RuntimeEvent): TaskProgress | undefined {
  const data = objectValue(event.data);
  const pdfEvent = objectValue(data.pdfEvent);
  return normalizeProgress(data.progress) ?? normalizeProgress(pdfEvent.progress);
}

export function assertRuntimeStatusTransition(from: SessionStatus, to: SessionStatus): void {
  if (from === to) return;
  const allowed = RUNTIME_STATUS_TRANSITIONS[from];
  if (!allowed.has(to)) {
    throw new Error(`Invalid runtime status transition: ${from} -> ${to}`);
  }
}

export function taskStateForImportedSession(
  session: SessionRecord,
  events: readonly RuntimeEvent[],
): TaskState {
  let state: TaskState = "created";
  for (const event of events) state = taskStateFromEvent(event, state);
  return taskStateFromSession(session.status, state);
}

export function advanceTaskState(current: TaskState, candidate: TaskState): TaskState {
  if (TERMINAL_TASK_STATES.has(current)) return current;
  return (TASK_STATE_ORDER.get(candidate) ?? 0) >= (TASK_STATE_ORDER.get(current) ?? 0)
    ? candidate
    : current;
}

function eventPhase(value: unknown): string {
  const data = objectValue(value);
  if (typeof data.phase === "string") return data.phase;
  return "";
}

function phaseState(phase: string): TaskState | undefined {
  const normalized = phase.startsWith("pdf.") ? phase.slice(4) : phase;
  if (normalized === "input.validated" || normalized === "job.created") {
    return "validating_inputs";
  }
  if (
    normalized === "document.validated"
    || normalized === "document.reading_started"
    || normalized === "page.started"
    || normalized === "page.completed"
    || normalized === "anchor.discovered"
  ) return "parsing_layout";
  if (
    normalized.startsWith("perception.")
    || normalized.startsWith("molparser.")
  ) return "recognizing_structures";
  if (
    normalized === "extraction.started"
    || normalized.startsWith("agent.")
    || normalized.startsWith("reaction.")
    || normalized.startsWith("identifier.")
    || normalized.startsWith("conditions.")
    || normalized === "si.evidence_linked"
    || normalized.startsWith("tool.")
  ) return "extracting_reactions";
  if (normalized.startsWith("validation.")) return "validating_chemistry";
  if (normalized === "artifact.created" || normalized === "extraction.awaiting_artifact") {
    return "preparing_review";
  }
  if (normalized === "job.review_ready" || normalized === "job.partial") return "review_ready";
  if (normalized === "job.cancelled") return "cancelled";
  if (normalized === "job.blocked") return "blocked";
  if (normalized === "job.failed" || normalized === "job.interrupted") {
    return "failed";
  }
  return undefined;
}

function normalizeProgress(value: unknown): TaskProgress | undefined {
  const progress = objectValue(value);
  if (
    typeof progress.completed !== "number"
    || !Number.isFinite(progress.completed)
    || progress.completed < 0
    || typeof progress.total !== "number"
    || !Number.isFinite(progress.total)
    || progress.total < 0
    || progress.completed > progress.total
    || typeof progress.unit !== "string"
    || progress.unit.length < 1
    || progress.unit.length > 64
  ) return undefined;
  return {
    completed: progress.completed,
    total: progress.total,
    unit: progress.unit,
    ...(typeof progress.denominator === "string" && progress.denominator.length <= 128
      ? { denominator: progress.denominator }
      : {}),
  };
}

function objectValue(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

const RUNTIME_STATUS_TRANSITIONS: Readonly<Record<SessionStatus, ReadonlySet<SessionStatus>>> = {
  creating: new Set([
    "awaiting_user_auth", "ready", "running", "blocked", "failed", "aborted", "cancelled",
    "interrupted",
  ]),
  awaiting_user_auth: new Set([
    "creating", "ready", "dormant", "blocked", "failed", "aborted", "cancelled",
    "interrupted",
  ]),
  ready: new Set([
    "running", "dormant", "blocked", "failed", "aborted", "review_ready", "partial",
    "cancelled", "interrupted",
  ]),
  dormant: new Set(["ready", "running", "blocked", "failed", "aborted", "cancelled", "interrupted"]),
  running: new Set([
    "ready", "blocked", "failed", "aborted", "review_ready", "partial", "cancelled",
    "interrupted",
  ]),
  blocked: new Set(),
  failed: new Set(),
  aborted: new Set(),
  review_ready: new Set(),
  partial: new Set(),
  cancelled: new Set(),
  interrupted: new Set(),
};
