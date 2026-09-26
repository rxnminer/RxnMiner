import type { RuntimeEvent } from "./contracts.js";

export type AgUiCompatibleEvent =
  | {
      type: "RUN_STARTED" | "RUN_FINISHED" | "RUN_ERROR";
      threadId: string;
      runId: string;
      timestamp: number;
      runtimeSeq: number;
      message?: string;
    }
  | {
      type: "TEXT_MESSAGE_START" | "TEXT_MESSAGE_CONTENT" | "TEXT_MESSAGE_END";
      messageId: string;
      role?: "user" | "assistant";
      delta?: string;
      timestamp: number;
      runtimeSeq: number;
    }
  | {
      type: "TOOL_CALL_START" | "TOOL_CALL_RESULT" | "TOOL_CALL_END";
      toolCallId: string;
      toolCallName?: string;
      parentMessageId?: string;
      content?: string;
      timestamp: number;
      runtimeSeq: number;
    }
  | {
      type: "STATE_SNAPSHOT";
      snapshot: Record<string, unknown>;
      timestamp: number;
      runtimeSeq: number;
    }
  | {
      type: "ACTIVITY_SNAPSHOT";
      activityType: string;
      content: Record<string, unknown>;
      timestamp: number;
      runtimeSeq: number;
    }
  | {
      type: "CUSTOM";
      name: string;
      value: unknown;
      timestamp: number;
      runtimeSeq: number;
    };

/**
 * Project the durable ChemRxn event spine into the small AG-UI vocabulary that generic
 * conversation clients understand. This is a compatibility view, not a second source of truth:
 * runtimeSeq always points back to the original persisted cursor and chemistry stays CUSTOM.
 */
export function toAgUi(runtimeEvent: RuntimeEvent): AgUiCompatibleEvent[] {
  const timestamp = Date.parse(runtimeEvent.at);
  const common = {
    timestamp: Number.isFinite(timestamp) ? timestamp : 0,
    runtimeSeq: runtimeEvent.seq,
  };
  const data = objectValue(runtimeEvent.data);
  if (runtimeEvent.type === "session.created") {
    return [
      {
        type: "RUN_STARTED",
        threadId: runtimeEvent.sessionId,
        runId: runtimeEvent.sessionId,
        ...common,
      },
      stateSnapshot(runtimeEvent, common, "creating"),
    ];
  }
  if (runtimeEvent.type === "session.ready" || runtimeEvent.type === "session.dormant"
      || runtimeEvent.type === "session.blocked") {
    const status = runtimeEvent.type === "session.ready" ? "ready"
      : runtimeEvent.type === "session.dormant" ? "dormant" : "blocked";
    return [stateSnapshot(runtimeEvent, common, status)];
  }
  if (runtimeEvent.type === "job.review_ready" || runtimeEvent.type === "job.partial"
      || runtimeEvent.type === "job.cancelled") {
    return [{
      type: "RUN_FINISHED",
      threadId: runtimeEvent.sessionId,
      runId: runtimeEvent.sessionId,
      ...common,
    }];
  }
  if (runtimeEvent.type === "agent.error" || runtimeEvent.type === "job.failed"
      || runtimeEvent.type === "job.interrupted") {
    return [{
      type: "RUN_ERROR",
      threadId: runtimeEvent.sessionId,
      runId: runtimeEvent.sessionId,
      message: stringValue(data.message) ?? runtimeEvent.type,
      ...common,
    }];
  }
  if (runtimeEvent.type === "agent.event" && data.phase === "conversation.user_message") {
    const messageId = `runtime:${runtimeEvent.sessionId}:user:${runtimeEvent.seq}`;
    const text = stringValue(data.text) ?? "";
    return [
      { type: "TEXT_MESSAGE_START", messageId, role: "user", ...common },
      { type: "TEXT_MESSAGE_CONTENT", messageId, delta: text, ...common },
      { type: "TEXT_MESSAGE_END", messageId, ...common },
    ];
  }
  if (runtimeEvent.type === "agent.event") {
    const piEvent = objectValue(data.event);
    if (piEvent.type === "message_update") {
      const update = objectValue(piEvent.assistantMessageEvent);
      if (update.type === "text_delta" && typeof update.delta === "string") {
        return [{
          type: "TEXT_MESSAGE_CONTENT",
          messageId: `runtime:${runtimeEvent.sessionId}:assistant:active`,
          delta: update.delta,
          ...common,
        }];
      }
    }
    if (piEvent.type === "message_start" && objectValue(piEvent.message).role === "assistant") {
      return [{
        type: "TEXT_MESSAGE_START",
        messageId: `runtime:${runtimeEvent.sessionId}:assistant:active`,
        role: "assistant",
        ...common,
      }];
    }
    if (piEvent.type === "message_end" && objectValue(piEvent.message).role === "assistant") {
      return [{
        type: "TEXT_MESSAGE_END",
        messageId: `runtime:${runtimeEvent.sessionId}:assistant:active`,
        ...common,
      }];
    }
    if (piEvent.type === "tool_execution_start" && typeof piEvent.toolCallId === "string") {
      return [{
        type: "TOOL_CALL_START",
        toolCallId: piEvent.toolCallId,
        toolCallName: stringValue(piEvent.toolName) ?? "tool",
        parentMessageId: `runtime:${runtimeEvent.sessionId}:assistant:active`,
        ...common,
      }];
    }
    if (piEvent.type === "tool_execution_end" && typeof piEvent.toolCallId === "string") {
      const result = piEvent.result === undefined
        ? []
        : [{
            type: "TOOL_CALL_RESULT" as const,
            toolCallId: piEvent.toolCallId,
            content: compactJson(piEvent.result),
            ...common,
          }];
      return [
        ...result,
        {
          type: "TOOL_CALL_END",
          toolCallId: piEvent.toolCallId,
          ...common,
        },
      ];
    }
    if (data.phase === "pi.tool_argument_progress") {
      return [{
        type: "ACTIVITY_SNAPSHOT",
        activityType: "tool-arguments",
        content: {
          redacted: true,
          batchedChunks: numberValue(piEvent.batchedChunks),
          byteLength: numberValue(piEvent.byteLength),
        },
        ...common,
      }];
    }
    const domain = data.figureEvent ? "chemrxn.figure-job.v1"
      : data.pdfEvent ? "chemrxn.pdf-replay.v1"
        : typeof data.domainContract === "string" ? data.domainContract : undefined;
    if (domain) {
      return [{
        type: "CUSTOM",
        name: domain,
        value: {
          runtimeEventType: runtimeEvent.type,
          phase: data.phase,
          event: data.figureEvent ?? data.pdfEvent ?? data.payload ?? data,
        },
        ...common,
      }];
    }
    if (typeof data.phase === "string" && data.phase.startsWith("pi.")) {
      return [{
        type: "CUSTOM",
        name: "pi.runtime",
        value: {
          runtimeEventType: runtimeEvent.type,
          piEventType: piEvent.type,
          phase: data.phase,
          event: piEvent,
        },
        ...common,
      }];
    }
  }
  return [{
    type: "CUSTOM",
    name: "chemrxn.runtime",
    value: boundedValue({
      runtimeEventType: runtimeEvent.type,
      data: runtimeEvent.data,
    }),
    ...common,
  }];
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function stateSnapshot(
  runtimeEvent: RuntimeEvent,
  common: { timestamp: number; runtimeSeq: number },
  status: string,
): AgUiCompatibleEvent {
  return {
    type: "STATE_SNAPSHOT",
    snapshot: {
      sessionId: runtimeEvent.sessionId,
      status,
      canonicalCursor: runtimeEvent.seq,
    },
    ...common,
  };
}

function compactJson(value: unknown): string {
  const encoded = JSON.stringify(value);
  if (encoded.length <= 64_000) return encoded;
  return `${encoded.slice(0, 64_000)}\n[tool result truncated]`;
}

function boundedValue(value: unknown): unknown {
  const encoded = JSON.stringify(value);
  if (encoded.length <= 64_000) return value;
  return {
    truncated: true,
    json: `${encoded.slice(0, 64_000)}\n[custom event truncated]`,
  };
}
