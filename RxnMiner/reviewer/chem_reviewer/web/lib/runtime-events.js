import { normalizeWorkspaceCommand } from "./workspace-command.js";

export const RUNTIME_EVENT_TYPES = new Set([
  "session.created", "session.ready", "session.dormant", "session.blocked",
  "auth.required", "auth.succeeded", "auth.failed", "auth.logged_out",
  "agent.event", "agent.error", "agent.aborted", "patch.proposed",
  "review.outcome",
  "job.review_ready", "job.partial", "job.cancelled", "job.interrupted", "job.failed",
]);

export function createLifecycleState() {
  return {
    connection: "idle",
    status: "unknown",
    cursor: 0,
    source: {
      mode: "unknown",
      reconstructed: null,
    },
    authRequired: false,
    auth: {
      mode: "unknown",
      provider: "unknown",
      status: "unknown",
    },
    message: "",
    error: null,
    events: [],
    conversation: [],
    workspaceCommands: [],
    reviewOutcomes: {},
    streamingAssistant: "",
    toolArgumentProgress: {
      active: false,
      chunks: 0,
      bytes: 0,
    },
    tools: [],
    toolHistoryTruncated: false,
    executionQueue: {
      state: "none",
      workItemId: "",
      position: null,
      ahead: null,
      concurrency: null,
      message: "",
      credentialRequired: false,
    },
    queue: { steering: 0, followUp: 0 },
    domain: {
      seen: {},
      figureReactionIds: {},
      figureArtifactIds: {},
      pdfAnchorIds: {},
      pdfDetectionCounts: {},
      pdfCacheHits: {},
    },
    figure: {
      active: false,
      phase: "",
      completed: 0,
      total: 0,
      unit: "",
      candidates: 0,
      artifacts: 0,
      detections: 0,
      cacheStatus: "unknown",
    },
    pdf: {
      active: false,
      phase: "",
      documents: {},
      completedPages: 0,
      totalPages: 0,
      anchors: 0,
      detections: 0,
      cacheHits: 0,
    },
  };
}

export function runtimeMessageBehavior(state, selected) {
  return ["review_ready", "partial"].includes(state?.status)
    && state?.source?.mode === "live_pi_sdk"
    ? "prompt"
    : selected;
}

export function applyRuntimeEvent(state, event) {
  if (!validRuntimeEvent(event) || event.seq <= state.cursor) return false;
  if (event.seq !== state.cursor + 1) return false;
  const previousStatus = state.status;
  const previousMessage = state.message;
  state.cursor = event.seq;
  state.error = null;
  const data = event.data && typeof event.data === "object" ? event.data : {};
  const executionQueueEvent = event.type === "agent.event"
    && typeof data.phase === "string"
    && data.phase.startsWith("queue.");
  const transition = {
    "session.created": ["creating", "Session created"],
    "session.ready": ["ready", data.message || "Runtime ready"],
    "session.dormant": ["dormant", data.message || "Conversation ready to reopen"],
    "session.blocked": ["blocked", data.message || data.code || "Session blocked"],
    "auth.required": ["awaiting_user_auth", "Waiting for user authentication"],
    "auth.succeeded": ["ready", "Authentication complete"],
    "auth.failed": ["blocked", data.message || "Authentication failed"],
    "auth.logged_out": ["blocked", "Authentication disconnected"],
    // Queue control records are durable scheduling facts, not evidence that Pi
    // or a chemistry tool has started. Preserve the lifecycle state until the
    // runtime emits session.ready/extraction.started.
    "agent.event": [
      executionQueueEvent ? previousStatus : "running",
      data.message || data.phase || "Agent update",
    ],
    "agent.error": ["failed", data.message || "Agent failed"],
    "agent.aborted": ["aborted", "Agent cancelled"],
    "patch.proposed": ["review_ready", "Proposed changes ready for review"],
    "review.outcome": [state.status, reviewOutcomeLabel(data)],
    "job.review_ready": ["review_ready", data.message || "Extraction artifacts ready for review"],
    "job.partial": ["partial", data.message || "Partial extraction is ready for review"],
    "job.cancelled": ["cancelled", data.message || "Extraction cancelled"],
    "job.interrupted": ["interrupted", data.message || "Extraction interrupted"],
    "job.failed": ["failed", data.message || "Extraction failed"],
  }[event.type];
  state.status = transition[0];
  state.message = transition[1];
  if (event.type === "agent.event" && data.phase === "workspace.command") {
    const command = runtimeWorkspaceCommand(event);
    state.message = command ? workspaceCommandLabel(command) : "Workspace command rejected";
  }
  if (event.type === "session.created") {
    const source = data.source && typeof data.source === "object" ? data.source : {};
    state.source.mode = typeof source.mode === "string" ? source.mode : "unknown";
    state.source.reconstructed = typeof source.reconstructed === "boolean"
      ? source.reconstructed : null;
    const sealedReplay = source.mode === "sealed_replay" && source.reconstructed === true;
    state.auth.mode = sealedReplay
      ? "sealed_replay"
      : typeof data.authMode === "string" ? data.authMode : "unknown";
    state.auth.provider = typeof data.provider === "string" ? data.provider : "unknown";
    state.auth.status = sealedReplay ? "not_required" : "configuring";
  } else if (event.type === "session.ready" || event.type === "auth.succeeded") {
    state.auth.status = "ready";
  } else if (event.type === "session.dormant") {
    state.auth.status = "action_required";
  } else if (event.type === "auth.required") {
    state.auth.status = "action_required";
  } else if (event.type === "auth.failed") {
    state.auth.status = "failed";
  } else if (event.type === "auth.logged_out") {
    state.auth.status = "logged_out";
  }
  if (executionQueueEvent) applyExecutionQueueEvent(state, data);
  if (event.type === "auth.required" && data.code === "QUEUE_CREDENTIAL_REQUIRED") {
    state.executionQueue.state = "blocked";
    state.executionQueue.credentialRequired = true;
    state.executionQueue.message = typeof data.message === "string"
      ? data.message : "Re-enter the API key to continue this queued extraction.";
    if (typeof data.workItemId === "string") {
      state.executionQueue.workItemId = data.workItemId;
    }
  }
  applyExecutionQueueTerminal(state, event.type, data);
  if (event.type === "agent.event"
      && ["pi.agent_end", "pi.agent_settled"].includes(data.phase)) {
    // These are Pi transport notifications. Artifact validation/finalization
    // may still be running after agent_end, so neither event may open the
    // composer. Only a durable session.ready or terminal job event owns that
    // transition. Preserve ready when settled is emitted after session.ready.
    state.status = previousStatus;
    state.message = previousMessage;
  }
  state.authRequired = event.type === "auth.required"
    ? true
    : ["auth.succeeded", "auth.failed", "auth.logged_out"].includes(event.type)
      ? false : state.authRequired;
  if (executionQueueEvent && data.phase === "queue.credential_received") {
    state.authRequired = false;
    state.auth.status = "configuring";
  }
  if (["session.blocked", "auth.failed", "agent.error", "job.failed"].includes(event.type)) {
    state.error = { code: data.code || event.type, message: state.message };
  }
  state.events.push(event);
  if (state.events.length > 200) state.events.splice(0, state.events.length - 200);
  if (event.type === "agent.event") {
    const domainOutcome = applyPiProjection(state, event, data);
    if (domainOutcome) applyDomainOutcome(state, domainOutcome, data);
  }
  if (event.type === "review.outcome") applyReviewOutcomeProjection(state, event, data);
  return true;
}

/** Merge the current durable dispatch row without replacing the event cursor. */
export function applyExecutionDispatch(state, dispatch) {
  const workItem = dispatch?.workItem;
  if (!workItem || typeof workItem !== "object") return false;
  const allowed = new Set([
    "pending", "active", "blocked", "completed", "failed", "cancelled", "interrupted",
  ]);
  if (!allowed.has(workItem.state)) return false;
  state.executionQueue.state = workItem.state;
  state.executionQueue.workItemId = typeof workItem.id === "string" ? workItem.id : "";
  state.executionQueue.position = Number.isSafeInteger(dispatch.position) && dispatch.position > 0
    ? dispatch.position : null;
  state.executionQueue.ahead = state.executionQueue.position == null
    ? null : Math.max(0, state.executionQueue.position - 1);
  state.executionQueue.credentialRequired = workItem.state === "blocked"
    ? dispatch.credentialRequired === true || state.executionQueue.credentialRequired
    : false;
  return true;
}

function applyExecutionQueueEvent(state, data) {
  const phase = data.phase;
  const queueState = {
    "queue.enqueued": "pending",
    "queue.recovered": "pending",
    "queue.position": "pending",
    "queue.credential_received": "pending",
    "queue.started": "active",
    "queue.cancel_requested": "cancelling",
    "queue.finished": typeof data.outcome === "string" ? data.outcome : "completed",
  }[phase];
  if (queueState) state.executionQueue.state = queueState;
  if (typeof data.workItemId === "string") state.executionQueue.workItemId = data.workItemId;
  if (Number.isSafeInteger(data.position) && data.position > 0) {
    state.executionQueue.position = data.position;
  } else if (["queue.started", "queue.finished"].includes(phase)) {
    state.executionQueue.position = null;
  }
  if (Number.isSafeInteger(data.ahead) && data.ahead >= 0) {
    state.executionQueue.ahead = data.ahead;
  } else if (["queue.started", "queue.finished"].includes(phase)) {
    state.executionQueue.ahead = null;
  }
  if (Number.isSafeInteger(data.concurrency) && data.concurrency > 0) {
    state.executionQueue.concurrency = data.concurrency;
  }
  state.executionQueue.message = typeof data.message === "string" ? data.message : "";
  state.executionQueue.credentialRequired = false;
}

function applyExecutionQueueTerminal(state, eventType, data) {
  const terminal = {
    "job.review_ready": "completed",
    "job.partial": "completed",
    "job.cancelled": "cancelled",
    "agent.aborted": "cancelled",
    "job.interrupted": "interrupted",
    "job.failed": "failed",
    "agent.error": "failed",
  }[eventType];
  if (!terminal || state.executionQueue.state === "none") return;
  state.executionQueue.state = terminal;
  state.executionQueue.position = null;
  state.executionQueue.ahead = null;
  state.executionQueue.credentialRequired = false;
  state.executionQueue.message = typeof data.message === "string"
    ? data.message : state.executionQueue.message;
}

function applyPiProjection(state, runtimeEvent, data) {
  const domain = runtimeDomainEvent(data);
  if (domain && !domainAlreadySeen(state, domain)) {
    if (domain.contract === "chemrxn.figure-job.v1") {
      applyFigureProjection(state, domain.event);
    } else if (domain.contract === "chemrxn.pdf-replay.v1") {
      applyPdfProjection(state, domain.event, domain.key);
    }
  }
  if (data.phase === "conversation.user_message" && typeof data.text === "string") {
    state.conversation.push({
      id: `user:${runtimeEvent.seq}`,
      role: "user",
      behavior: typeof data.behavior === "string" ? data.behavior : "prompt",
      text: data.text,
      at: runtimeEvent.at,
    });
    trimConversation(state);
    return null;
  }
  if (data.phase === "workspace.command") {
    const command = runtimeWorkspaceCommand(runtimeEvent);
    if (command) {
      state.workspaceCommands.push({ seq: runtimeEvent.seq, command });
      if (state.workspaceCommands.length > 100) {
        state.workspaceCommands.splice(0, state.workspaceCommands.length - 100);
      }
      state.conversation.push({
        id: `workspace:${runtimeEvent.seq}`,
        role: "assistant",
        behavior: "workspace_action",
        text: workspaceCommandLabel(command),
        at: runtimeEvent.at,
        workspaceCommand: command,
        ...(command.type === "review.propose"
          ? { proposalId: command.proposalId, proposalStatus: "proposed" } : {}),
      });
      trimConversation(state);
    }
    return null;
  }
  if (typeof data.phase !== "string" || !data.phase.startsWith("pi.")) {
    return domainTerminalOutcome(domain?.event);
  }
  const event = data.event && typeof data.event === "object" ? data.event : {};
  if (event.type === "message_update") {
    const update = event.assistantMessageEvent;
    if (update?.type === "text_delta" && typeof update.delta === "string") {
      state.streamingAssistant += update.delta;
    }
    if (
      update?.type === "toolcall_delta"
      && update.redacted === true
      && Number.isSafeInteger(event.batchedChunks)
      && Number.isSafeInteger(event.byteLength)
    ) {
      state.toolArgumentProgress.active = true;
      state.toolArgumentProgress.chunks += event.batchedChunks;
      state.toolArgumentProgress.bytes += event.byteLength;
    }
    return null;
  }
  if (event.type === "message_end" && event.message?.role === "assistant") {
    const text = messageText(event.message) || state.streamingAssistant;
    if (text) {
      state.conversation.push({
        id: `assistant:${runtimeEvent.seq}`,
        role: "assistant",
        behavior: "response",
        text,
        at: runtimeEvent.at,
      });
    }
    state.streamingAssistant = "";
    trimConversation(state);
    return null;
  }
  if (event.type === "tool_execution_start" && typeof event.toolCallId === "string") {
    state.toolArgumentProgress.active = false;
    state.toolArgumentProgress.chunks = 0;
    state.toolArgumentProgress.bytes = 0;
    state.tools.push({
      id: event.toolCallId,
      name: typeof event.toolName === "string" ? event.toolName : "tool",
      status: "running",
      startedAt: runtimeEvent.at,
    });
    if (state.tools.length > 100) {
      state.tools.splice(0, state.tools.length - 100);
      state.toolHistoryTruncated = true;
    }
    return null;
  }
  if (event.type === "tool_execution_end" && typeof event.toolCallId === "string") {
    const tool = [...state.tools].reverse().find((item) => item.id === event.toolCallId);
    if (tool) {
      tool.status = event.isError || event.result?.isError ? "error" : "complete";
      tool.completedAt = runtimeEvent.at;
    }
    return null;
  }
  if (event.type === "queue_update") {
    state.queue = {
      steering: Number.isSafeInteger(event.steeringCount)
        ? event.steeringCount : Array.isArray(event.steering) ? event.steering.length : 0,
      followUp: Number.isSafeInteger(event.followUpCount)
        ? event.followUpCount : Array.isArray(event.followUp) ? event.followUp.length : 0,
    };
  }
  return null;
}

function reviewOutcomeLabel(data) {
  const version = Number.isInteger(data.reviewedVersion)
    ? ` as reviewed-v${data.reviewedVersion}` : "";
  return {
    staged: "Correction proposal ready for human review",
    applied: `Correction proposal applied${version}`,
    discarded: "Correction proposal discarded",
    expired: "Correction proposal expired",
    rejected: "Correction proposal rejected",
  }[data.status] || "Review outcome recorded";
}

function applyReviewOutcomeProjection(state, runtimeEvent, data) {
  if (
    typeof data.proposalId !== "string"
    || !["staged", "applied", "discarded", "expired", "rejected"].includes(data.status)
  ) return;
  const outcome = {
    status: data.status,
    reviewedVersion: Number.isInteger(data.reviewedVersion) ? data.reviewedVersion : null,
    patchId: typeof data.patchId === "string" ? data.patchId : null,
    reason: typeof data.reason === "string" ? data.reason : "",
    at: runtimeEvent.at,
  };
  state.reviewOutcomes[data.proposalId] = outcome;
  const message = state.conversation.find((item) => item.proposalId === data.proposalId);
  if (message) {
    message.proposalStatus = outcome.status;
    message.reviewedVersion = outcome.reviewedVersion;
    message.reviewReason = outcome.reason;
    return;
  }
  state.conversation.push({
    id: `review-outcome:${runtimeEvent.seq}`,
    role: "assistant",
    behavior: "review_outcome",
    text: reviewOutcomeLabel(data),
    at: runtimeEvent.at,
    proposalId: data.proposalId,
    proposalStatus: outcome.status,
    reviewedVersion: outcome.reviewedVersion,
    reviewReason: outcome.reason,
  });
  trimConversation(state);
}

export function runtimeWorkspaceCommand(runtimeEvent) {
  if (runtimeEvent?.type !== "agent.event") return null;
  const data = runtimeEvent.data && typeof runtimeEvent.data === "object"
    ? runtimeEvent.data : {};
  if (data.phase !== "workspace.command") return null;
  try {
    return normalizeWorkspaceCommand(data.workspaceCommand);
  } catch {
    return null;
  }
}

export function isTerminalReviewProposalStatus(status) {
  return ["applied", "discarded", "expired", "rejected"].includes(status);
}

export function latestReplayableWorkspaceCommand(state) {
  const conversation = Array.isArray(state?.conversation) ? state.conversation : [];
  for (let index = conversation.length - 1; index >= 0; index -= 1) {
    const message = conversation[index];
    if (!message?.workspaceCommand) continue;
    if (
      message.workspaceCommand.type === "review.propose"
      && isTerminalReviewProposalStatus(message.proposalStatus)
    ) {
      // The latest durable workspace intent has already reached a terminal
      // human outcome. Do not fall back to an older navigation command and
      // make a restored session look as though the proposal were still live.
      return null;
    }
    return message.workspaceCommand;
  }
  return null;
}

function workspaceCommandLabel(command) {
  if (command.type === "review.propose") {
    return `Propose correction for ${command.reactionId} · awaiting human Apply`;
  }
  const target = command.reactionId
    ? ` for ${command.reactionId}`
    : Number.isInteger(command.anchor) ? ` at anchor ${command.anchor + 1}` : "";
  const location = command.page
    ? ` · ${command.documentSource === "si" ? "SI" : "main"} p${command.page}`
    : command.bboxKey ? ` · box ${command.bboxKey}` : "";
  return `Open ${command.module.replaceAll("-", " ")}${target}${location}`;
}

function runtimeDomainEvent(data) {
  if (data.figureEvent && typeof data.figureEvent === "object") {
    return {
      contract: "chemrxn.figure-job.v1",
      event: data.figureEvent,
      key: domainEventKey("chemrxn.figure-job.v1", data.figureEvent),
    };
  }
  if (data.pdfEvent && typeof data.pdfEvent === "object") {
    return {
      contract: "chemrxn.pdf-replay.v1",
      event: data.pdfEvent,
      key: domainEventKey("chemrxn.pdf-replay.v1", data.pdfEvent),
    };
  }
  if (
    ["chemrxn.figure-job.v1", "chemrxn.pdf-replay.v1"].includes(data.domainContract)
    && typeof data.phase === "string"
  ) {
    const event = {
      eventId: typeof data.domainEventId === "string" ? data.domainEventId : undefined,
      type: data.phase,
      data: data.payload && typeof data.payload === "object" ? data.payload : {},
      scope: data.scope && typeof data.scope === "object" ? data.scope : {},
      progress: data.progress && typeof data.progress === "object" ? data.progress : undefined,
      artifactIds: Array.isArray(data.artifactIds) ? data.artifactIds : [],
    };
    return {
      contract: data.domainContract,
      event,
      key: domainEventKey(data.domainContract, event),
    };
  }
  if (typeof data.phase === "string" && data.phase.startsWith("pdf.")) {
    return {
      contract: "chemrxn.pdf-replay.v1",
      event: {
        type: data.phase.slice("pdf.".length),
        data,
        scope: {},
        artifactIds: [],
      },
      key: null,
    };
  }
  if (typeof data.phase === "string" && data.phase.startsWith("figure.")) {
    return {
      contract: "chemrxn.figure-job.v1",
      event: {
        type: data.phase.slice("figure.".length),
        data,
        artifactIds: [],
      },
      key: null,
    };
  }
  return null;
}

function domainEventKey(contract, event) {
  const identity = typeof event.eventId === "string" ? event.eventId
    : typeof event.idempotencyKey === "string" ? event.idempotencyKey : null;
  return identity ? `${contract}:${identity}` : null;
}

function domainAlreadySeen(state, domain) {
  if (!domain.key) return false;
  if (state.domain.seen[domain.key]) return true;
  state.domain.seen[domain.key] = true;
  return false;
}

function domainTerminalOutcome(event) {
  return {
    "job.review_ready": "review_ready",
    "job.partial": "partial",
    "job.cancelled": "cancelled",
    "job.interrupted": "interrupted",
    "job.blocked": "blocked",
    "job.failed": "failed",
  }[event?.type] || null;
}

function applyDomainOutcome(state, outcome, data) {
  const domain = runtimeDomainEvent(data);
  const payload = domain?.event?.data && typeof domain.event.data === "object"
    ? domain.event.data : {};
  state.status = outcome;
  state.message = typeof payload.message === "string"
    ? payload.message
    : {
      review_ready: "Extraction artifacts ready for review",
      partial: "Partial extraction is ready for review",
      cancelled: "Extraction cancelled",
      interrupted: "Extraction interrupted",
      blocked: "Extraction blocked",
      failed: "Extraction failed",
    }[outcome];
  if (["blocked", "failed"].includes(outcome)) {
    state.error = {
      code: typeof payload.code === "string" ? payload.code : `job.${outcome}`,
      message: state.message,
    };
  }
}

function applyFigureProjection(state, event) {
  const payload = event.data && typeof event.data === "object" ? event.data : {};
  state.figure.active = true;
  state.figure.phase = typeof event.type === "string" ? event.type : "";
  if (event.progress && typeof event.progress === "object") {
    state.figure.completed = Number.isSafeInteger(event.progress.completed)
      ? event.progress.completed : state.figure.completed;
    state.figure.total = Number.isSafeInteger(event.progress.total)
      ? event.progress.total : state.figure.total;
    state.figure.unit = typeof event.progress.unit === "string"
      ? event.progress.unit : state.figure.unit;
  }
  if (event.type === "reaction.candidate") {
    const reactionId = typeof payload.reactionId === "string" ? payload.reactionId : null;
    if (reactionId) state.domain.figureReactionIds[reactionId] = true;
    state.figure.candidates = Object.keys(state.domain.figureReactionIds).length;
  }
  if (event.type === "artifact.created") {
    for (const artifactId of Array.isArray(event.artifactIds) ? event.artifactIds : []) {
      if (typeof artifactId === "string") state.domain.figureArtifactIds[artifactId] = true;
    }
    state.figure.artifacts = Object.keys(state.domain.figureArtifactIds).length;
  }
  if (event.type === "molparser.cache_hit" || event.type === "molparser.completed") {
    state.figure.cacheStatus = event.type === "molparser.cache_hit" ? "cache hit" : "live call";
    state.figure.detections = Number.isSafeInteger(payload.detectionCount)
      ? payload.detectionCount : state.figure.detections;
  }
  if (["tool.started", "tool.completed", "tool.failed"].includes(event.type)) {
    const name = typeof payload.tool === "string" ? payload.tool : "tool";
    const id = typeof payload.callId === "string"
      ? payload.callId : `domain:${name}:${event.eventId || state.cursor}`;
    const reconstructed = state.source.mode === "sealed_replay"
      && state.source.reconstructed === true;
    let record = [...state.tools].reverse().find((item) => item.id === id);
    if (!record) {
      record = {
        id,
        name,
        status: event.type === "tool.started" ? "running" : "complete",
        startedAt: null,
        reconstructed,
      };
      state.tools.push(record);
      if (state.tools.length > 200) {
        state.tools.splice(0, state.tools.length - 200);
        state.toolHistoryTruncated = true;
      }
    }
    record.status = event.type === "tool.failed" ? "error"
      : event.type === "tool.completed" ? "complete" : "running";
    record.reconstructed = reconstructed;
  }
}

function applyPdfProjection(state, event, domainKey) {
  const payload = event.data && typeof event.data === "object" ? event.data : {};
  const scope = event.scope && typeof event.scope === "object" ? event.scope : {};
  state.pdf.active = true;
  state.pdf.phase = typeof event.type === "string" ? event.type : "";
  const documentId = typeof scope.documentId === "string" ? scope.documentId : null;
  if (documentId) {
    const existing = state.pdf.documents[documentId] || {
      source: payload.sourceDocument || "document",
      completedPages: 0,
      totalPages: 0,
      anchors: 0,
      detections: 0,
    };
    if (event.progress?.unit === "pages") {
      existing.completedPages = Math.max(existing.completedPages, event.progress.completed || 0);
      existing.totalPages = Math.max(existing.totalPages, event.progress.total || 0);
    }
    if (event.type === "anchor.discovered" && typeof scope.anchorId === "string") {
      state.domain.pdfAnchorIds[scope.anchorId] = true;
    }
    if (event.type === "molparser.completed" || event.type === "molparser.cache_hit") {
      const detectionKey = typeof scope.pageId === "string" ? scope.pageId : domainKey;
      if (detectionKey && Number.isSafeInteger(payload.detectionCount)) {
        state.domain.pdfDetectionCounts[detectionKey] = payload.detectionCount;
      }
    }
    state.pdf.documents[documentId] = existing;
  }
  if (event.type === "molparser.cache_hit") {
    const cacheKey = typeof scope.pageId === "string" ? scope.pageId : domainKey;
    if (cacheKey) state.domain.pdfCacheHits[cacheKey] = true;
  }
  const documents = Object.values(state.pdf.documents);
  state.pdf.completedPages = documents.reduce((sum, item) => sum + item.completedPages, 0);
  state.pdf.totalPages = documents.reduce((sum, item) => sum + item.totalPages, 0);
  state.pdf.anchors = Object.keys(state.domain.pdfAnchorIds).length;
  state.pdf.detections = Object.values(state.domain.pdfDetectionCounts)
    .reduce((sum, count) => sum + count, 0);
  state.pdf.cacheHits = Object.keys(state.domain.pdfCacheHits).length;
}

export function runtimeSourceLabel(source) {
  if (source?.mode === "sealed_replay" && source.reconstructed === true) return "Sealed replay";
  if (source?.mode === "live_pi_sdk") return "Live Pi runtime";
  return "Runtime";
}

function messageText(message) {
  if (typeof message.content === "string") return message.content;
  if (!Array.isArray(message.content)) return "";
  return message.content
    .filter((part) => part && part.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("");
}

function trimConversation(state) {
  if (state.conversation.length > 100) {
    state.conversation.splice(0, state.conversation.length - 100);
  }
}

export function validRuntimeEvent(event) {
  return Boolean(
    event && Number.isSafeInteger(event.seq) && event.seq > 0
    && typeof event.sessionId === "string"
    && typeof event.at === "string"
    && RUNTIME_EVENT_TYPES.has(event.type),
  );
}

export function summarizeRuntimeResult(result) {
  if (
    !result
    || result.schemaVersion !== "chemrxn.runtime-result.v1"
    || result.gtIncluded !== false
    || !result.source
    || !["sealed_replay", "live_pi_sdk"].includes(result.source.mode)
  ) return null;
  const reconstructed = result.source.mode === "sealed_replay"
    && result.source.reconstructed === true;
  const live = result.source.mode === "live_pi_sdk"
    && result.source.reconstructed === false
    && result.source.liveEventCapture === true;
  if (!reconstructed && !live) return null;
  const manifest = result.manifest && typeof result.manifest === "object"
    ? result.manifest : {};
  const artifacts = Array.isArray(manifest.artifacts) ? manifest.artifacts : [];
  const viewerArtifactLinks = result.viewer && typeof result.viewer === "object"
    ? [
      ["Original", result.viewer.inputArtifactId],
      ["Prediction V2", result.viewer.predictionArtifactId],
      ["BBox overview", result.viewer.overviewArtifactId],
      ["Reaction render", result.viewer.reactionsArtifactId],
    ].filter(([, artifactId]) => typeof artifactId === "string")
      .map(([label, artifactId]) => {
        const artifact = artifacts.find((item) => item?.id === artifactId);
        return {
          label,
          artifactId,
          mediaType: artifact?.mediaType || "application/octet-stream",
        };
      })
    : [];
  const documentArtifactLinks = Array.isArray(manifest.documents)
    ? manifest.documents
      .filter((document) =>
        document?.sourceDocument === "si"
        && typeof document.artifactId === "string"
        && document.artifactId !== result.viewer?.inputArtifactId)
      .map((document) => {
        const artifact = artifacts.find((item) => item?.id === document.artifactId);
        return {
          label: "Supporting information",
          artifactId: document.artifactId,
          mediaType: artifact?.mediaType || "application/pdf",
        };
      })
    : [];
  const artifactLinks = [...viewerArtifactLinks, ...documentArtifactLinks];
  const reviewable = Boolean(
    result.viewer
    && typeof result.viewer === "object"
    && ["figure", "document"].includes(result.viewer.kind)
    && typeof result.viewer.inputArtifactId === "string"
    && typeof result.viewer.predictionArtifactId === "string",
  );
  if (result.domainContract === "chemrxn.figure-job.v1") {
    const counts = manifest.counts && typeof manifest.counts === "object"
      ? manifest.counts : {};
    return {
      title: "Figure extraction result",
      fixture: reconstructed ? result.source.fixture : null,
      contextMode: result.contextMode,
      reconstructed,
      live,
      reviewable,
      sourceLabel: live
        ? `${manifest.model?.provider || "Pi"} · ${manifest.model?.id || "model"}`
        : result.source.fixture,
      terminalStatus: result.terminalStatus,
      validation: manifest.validation,
      execution: {
        skill: typeof manifest.skill?.name === "string"
          ? manifest.skill.name : "reaction-extract",
        skillVersion: typeof manifest.skill?.commit === "string"
          ? manifest.skill.commit : "",
        provider: typeof manifest.model?.provider === "string"
          ? manifest.model.provider : "unknown",
        model: typeof manifest.model?.id === "string" ? manifest.model.id : "unknown",
        evidenceMode: reconstructed ? "sealed_tool_ledger" : "captured_live_events",
      },
      artifacts: artifactLinks,
      metrics: [
        ["Examples", counts.examples],
        ["Templates", counts.templates],
        ["Successful", counts.successful],
        ["Metrics", counts.metrics],
        ["Molecules", Array.isArray(manifest.molecules) ? manifest.molecules.length : undefined],
        ["BBoxes", Array.isArray(manifest.bboxes) ? manifest.bboxes.length : undefined],
      ].filter((item) => Number.isFinite(item[1])),
      reactions: Array.isArray(manifest.reactions)
        ? manifest.reactions.slice(0, 12).map((reaction) => ({
          id: reaction.id,
          label: reaction.printedIdentifier || reaction.id,
          outcome: reaction.outcome || "unknown",
        }))
        : [],
    };
  }
  if (result.domainContract === "chemrxn.pdf-replay.v1") {
    const expected = manifest.expected && typeof manifest.expected === "object"
      ? manifest.expected : {};
    const documents = Array.isArray(manifest.documents) ? manifest.documents : [];
    const totalDocumentPages = documents.length > 0 && documents.every((document) =>
      Number.isSafeInteger(document?.pageCount))
      ? documents.reduce((sum, document) => sum + document.pageCount, 0)
      : undefined;
    return {
      title: result.contextMode === "paper_si"
        ? "PDF + SI extraction result" : "PDF extraction result",
      fixture: result.source.fixture,
      contextMode: result.contextMode,
      reconstructed,
      live,
      reviewable,
      sourceLabel: live
        ? `${manifest.versions?.modelProvider || "Pi"} · ${manifest.versions?.modelId || "model"}`
        : result.source.fixture,
      terminalStatus: result.terminalStatus,
      validation: manifest.validation,
      execution: {
        skill: "pdf-reaction-extract",
        skillVersion: typeof manifest.versions?.skillSha256 === "string"
          ? manifest.versions.skillSha256 : "",
        provider: typeof manifest.versions?.modelProvider === "string"
          ? manifest.versions.modelProvider : "unknown",
        model: typeof manifest.versions?.modelId === "string"
          ? manifest.versions.modelId : "unknown",
        evidenceMode: reconstructed ? "sealed_stage_ledger" : "captured_live_events",
      },
      artifacts: artifactLinks,
      metrics: [
        ["Main PDF pages", expected.pageCount],
        ["Total document pages", totalDocumentPages],
        ["Anchors", expected.anchorCount],
        ["Reactions", expected.reactionCount],
        ["Lineage", Array.isArray(manifest.lineage) ? manifest.lineage.length : undefined],
        ["Documents", documents.length],
      ].filter((item) => Number.isFinite(item[1])),
      reactions: [],
    };
  }
  return null;
}

/**
 * Collapse an ordered tool ledger into a compact, truthful product summary.
 * Sealed Figure replay records only tool names and completion states; it does
 * not invent arguments or outputs that were not captured in the fixture.
 */
export function summarizeToolActivity(tools) {
  const records = Array.isArray(tools) ? tools : [];
  const groups = new Map();
  for (const tool of records) {
    if (!tool || typeof tool.name !== "string" || !tool.name) continue;
    const current = groups.get(tool.name) || {
      name: tool.name,
      count: 0,
      complete: 0,
      running: 0,
      error: 0,
      reconstructed: false,
    };
    current.count += 1;
    if (tool.status === "complete") current.complete += 1;
    else if (tool.status === "running") current.running += 1;
    else if (tool.status === "error") current.error += 1;
    current.reconstructed ||= tool.reconstructed === true;
    groups.set(tool.name, current);
  }
  return [...groups.values()].map((group) => ({
    ...group,
    status: group.error ? "error" : group.running ? "running" : "complete",
  }));
}

export class RuntimeEventStream {
  constructor({
    sessionId,
    after = 0,
    EventSourceClass = globalThis.EventSource,
    onConnection = () => {},
    onEvent = () => {},
    onProtocolError = () => {},
    maxGapRetries = 3,
    gapRetryBaseMs = 250,
    setTimeoutFn = globalThis.setTimeout,
    clearTimeoutFn = globalThis.clearTimeout,
  }) {
    this.sessionId = sessionId;
    this.after = after;
    this.EventSourceClass = EventSourceClass;
    this.onConnection = onConnection;
    this.onEvent = onEvent;
    this.onProtocolError = onProtocolError;
    this.maxGapRetries = maxGapRetries;
    this.gapRetryBaseMs = gapRetryBaseMs;
    this.setTimeoutFn = setTimeoutFn;
    this.clearTimeoutFn = clearTimeoutFn;
    this.source = null;
    this.gapRetries = 0;
    this.reconnectTimer = null;
  }

  connect({ preserveGapRetries = false } = {}) {
    if (!this.EventSourceClass) throw new Error("EventSource is unavailable");
    this.close({ preserveGapRetries });
    const url = `/api/runtime/sessions/${encodeURIComponent(this.sessionId)}/events?after=${this.after}`;
    const source = new this.EventSourceClass(url);
    this.source = source;
    this.onConnection("connecting");
    source.onopen = () => this.onConnection("open");
    source.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data);
        if (!validRuntimeEvent(event) || event.sessionId !== this.sessionId) {
          throw new Error("Invalid runtime event");
        }
        if (event.seq <= this.after) return;
        if (event.seq !== this.after + 1) {
          this.recoverCursorGap(event.seq);
          return;
        }
        this.gapRetries = 0;
        this.after = event.seq;
        this.onEvent(event);
      } catch (error) {
        this.onProtocolError(error);
      }
    };
    source.addEventListener("runtime.error", (message) => {
      let error = { code: "RUNTIME_STREAM_FAILED", message: "Runtime stream failed." };
      try { error = JSON.parse(message.data); } catch {}
      this.onProtocolError(error);
      this.close();
    });
    source.onerror = () => {
      this.onConnection(source.readyState === 2 ? "closed" : "reconnecting");
    };
    return this;
  }

  recoverCursorGap(received) {
    this.source?.close();
    this.source = null;
    this.gapRetries += 1;
    const error = new Error(
      `Runtime cursor gap: expected ${this.after + 1}, received ${received}`,
    );
    error.code = "RUNTIME_CURSOR_GAP";
    if (this.gapRetries > this.maxGapRetries) {
      error.retryable = false;
      this.onConnection("closed");
      this.onProtocolError(error);
      return;
    }
    const retryInMs = this.gapRetryBaseMs * (2 ** (this.gapRetries - 1));
    error.retryable = true;
    error.retryInMs = retryInMs;
    this.onConnection("reconnecting");
    this.onProtocolError(error);
    this.reconnectTimer = this.setTimeoutFn(() => {
      this.reconnectTimer = null;
      this.connect({ preserveGapRetries: true });
    }, retryInMs);
  }

  close({ preserveGapRetries = false } = {}) {
    this.source?.close();
    this.source = null;
    if (this.reconnectTimer !== null) {
      this.clearTimeoutFn(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (!preserveGapRetries) this.gapRetries = 0;
  }
}

export function reconnectAfterAcceptedMessage(previousStatus, responseBody, stream) {
  if (previousStatus !== "dormant" || responseBody?.continued || !stream) return false;
  stream.connect();
  return true;
}
