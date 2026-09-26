import {
  FIGURE_CONTRACT_VERSION,
  type FigureEvent,
  type FigureEventType,
  type FigureProgress,
  type FigureTerminalEventType,
} from "./contracts.js";
import { eventId } from "./ids.js";

const TERMINAL = new Set<FigureEventType>([
  "job.partial",
  "job.review_ready",
  "job.cancelled",
  "job.failed",
]);

export interface EmitFigureEventInput {
  type: FigureEventType;
  data?: Record<string, unknown>;
  artifactIds?: string[];
  progress?: FigureProgress;
  source?: FigureEvent["source"];
  idempotencyKey?: string;
}

export class FigureEventEmitter {
  private readonly emitted: FigureEvent[] = [];
  private readonly byIdempotencyKey = new Map<string, FigureEvent>();
  private terminalType: FigureTerminalEventType | undefined;

  constructor(
    readonly jobId: string,
    readonly attemptId: string,
    private readonly clock: () => string = () => new Date().toISOString(),
    private readonly defaultSource: FigureEvent["source"] = "figure-adapter",
  ) {}

  emit(input: EmitFigureEventInput): FigureEvent {
    validateProgress(input.progress);
    const key = input.idempotencyKey ?? `${input.type}:${this.emitted.length + 1}`;
    const existing = this.byIdempotencyKey.get(key);
    if (existing) return structuredClone(existing);
    if (this.terminalType) {
      throw new Error(`Cannot emit ${input.type} after terminal event ${this.terminalType}`);
    }

    const seq = this.emitted.length + 1;
    const event: FigureEvent = {
      schemaVersion: FIGURE_CONTRACT_VERSION,
      eventId: eventId(this.jobId, this.attemptId, seq),
      idempotencyKey: key,
      seq,
      jobId: this.jobId,
      attemptId: this.attemptId,
      at: this.clock(),
      source: input.source ?? this.defaultSource,
      type: input.type,
      replaySafe: true,
      artifactIds: [...(input.artifactIds ?? [])],
      data: structuredClone(input.data ?? {}),
      ...(input.progress ? { progress: structuredClone(input.progress) } : {}),
    };
    this.emitted.push(event);
    this.byIdempotencyKey.set(key, event);
    if (TERMINAL.has(input.type)) this.terminalType = input.type as FigureTerminalEventType;
    return structuredClone(event);
  }

  requestCancel(reason: string): FigureEvent {
    return this.emit({
      type: "job.cancel_requested",
      data: { reason },
      idempotencyKey: "job.cancel_requested",
    });
  }

  cancel(validArtifactIds: string[]): FigureEvent {
    return this.emit({
      type: "job.cancelled",
      data: { reusableArtifactIds: [...validArtifactIds] },
      artifactIds: validArtifactIds,
      idempotencyKey: "job.cancelled",
    });
  }

  events(): FigureEvent[] {
    return structuredClone(this.emitted);
  }
}

function validateProgress(progress: FigureProgress | undefined): void {
  if (!progress) return;
  if (!Number.isInteger(progress.completed) || !Number.isInteger(progress.total)) {
    throw new Error("Progress completed/total must be integers");
  }
  if (progress.completed < 0 || progress.total <= 0 || progress.completed > progress.total) {
    throw new Error("Progress must satisfy 0 <= completed <= total and total > 0");
  }
}
