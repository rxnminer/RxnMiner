import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import {
  applyExecutionDispatch,
  applyRuntimeEvent,
  createLifecycleState,
  isTerminalReviewProposalStatus,
  latestReplayableWorkspaceCommand,
  reconnectAfterAcceptedMessage,
  runtimeMessageBehavior,
  runtimeWorkspaceCommand,
  RuntimeEventStream,
  runtimeSourceLabel,
  summarizeRuntimeResult,
  summarizeToolActivity,
  validRuntimeEvent,
} from "./runtime-events.js";

test("sealed live tasks force prompt even when the active composer selected steer", () => {
  const state = createLifecycleState();
  state.status = "review_ready";
  state.source.mode = "live_pi_sdk";
  assert.equal(runtimeMessageBehavior(state, "steer"), "prompt");
  assert.equal(runtimeMessageBehavior(state, "follow_up"), "prompt");
  state.status = "running";
  assert.equal(runtimeMessageBehavior(state, "steer"), "steer");
  state.status = "review_ready";
  state.source.mode = "sealed_replay";
  assert.equal(runtimeMessageBehavior(state, "follow_up"), "follow_up");
});

function event(seq, type, data = {}) {
  return {
    seq,
    sessionId: "session-a",
    at: `2000-01-01T00:00:0${seq}.000Z`,
    type,
    data,
  };
}

test("lifecycle reducer derives only persisted runtime states", () => {
  const state = createLifecycleState();
  assert.equal(applyRuntimeEvent(state, event(1, "session.created", {
    authMode: "pi_auth",
    provider: "openai-codex",
    source: { mode: "live_pi_sdk", reconstructed: false },
  })), true);
  assert.equal(state.status, "creating");
  assert.deepEqual(state.auth, {
    mode: "pi_auth",
    provider: "openai-codex",
    status: "configuring",
  });
  assert.equal(runtimeSourceLabel(state.source), "Live Pi runtime");
  assert.equal(applyRuntimeEvent(state, event(2, "auth.required")), true);
  assert.equal(state.status, "awaiting_user_auth");
  assert.equal(state.authRequired, true);
  assert.equal(applyRuntimeEvent(state, event(3, "session.blocked", {
    code: "REMOTE_SANDBOX_NOT_CONFIGURED",
    message: "Backend not connected",
  })), true);
  assert.equal(state.status, "blocked");
  assert.equal(state.error.code, "REMOTE_SANDBOX_NOT_CONFIGURED");
  assert.equal(state.cursor, 3);
  assert.equal(applyRuntimeEvent(state, event(3, "session.ready")), false);
  assert.equal(state.status, "blocked");
});

test("durable dispatch events project separately without pretending Pi is running", () => {
  const state = createLifecycleState();
  applyRuntimeEvent(state, event(1, "session.created", {
    authMode: "api_key",
    provider: "openai",
    source: { mode: "live_pi_sdk", reconstructed: false },
  }));
  applyRuntimeEvent(state, event(2, "agent.event", {
    phase: "queue.enqueued",
    schemaVersion: "chemrxn.task-dispatch-event.v1",
    workItemId: "wrk-1",
    position: 3,
    ahead: 2,
    concurrency: 1,
    message: "2 extractions ahead.",
  }));
  assert.equal(state.status, "creating");
  assert.deepEqual(state.executionQueue, {
    state: "pending",
    workItemId: "wrk-1",
    position: 3,
    ahead: 2,
    concurrency: 1,
    message: "2 extractions ahead.",
    credentialRequired: false,
  });
  assert.deepEqual(state.queue, { steering: 0, followUp: 0 });

  applyRuntimeEvent(state, event(3, "agent.event", {
    phase: "queue.position",
    workItemId: "wrk-1",
    position: 1,
    ahead: 0,
    concurrency: 1,
  }));
  assert.equal(state.status, "creating");
  assert.equal(state.executionQueue.position, 1);
  applyRuntimeEvent(state, event(4, "agent.event", {
    phase: "queue.started",
    workItemId: "wrk-1",
    concurrency: 1,
  }));
  assert.equal(state.status, "creating");
  assert.equal(state.executionQueue.state, "active");
  assert.equal(state.executionQueue.position, null);

  applyRuntimeEvent(state, event(5, "session.ready"));
  assert.equal(state.status, "ready");
  applyRuntimeEvent(state, event(6, "agent.event", { phase: "extraction.started" }));
  assert.equal(state.status, "running");
  applyRuntimeEvent(state, event(7, "job.review_ready"));
  assert.equal(state.executionQueue.state, "completed");
  assert.equal(state.executionQueue.position, null);
});

test("restart-blocked BYO dispatch asks for a one-run credential and resumes truthfully", () => {
  const state = createLifecycleState();
  applyRuntimeEvent(state, event(1, "session.created", {
    authMode: "api_key",
    provider: "openai",
  }));
  applyRuntimeEvent(state, event(2, "agent.event", {
    phase: "queue.enqueued",
    workItemId: "wrk-byo",
    position: 1,
  }));
  applyRuntimeEvent(state, event(3, "auth.required", {
    code: "QUEUE_CREDENTIAL_REQUIRED",
    workItemId: "wrk-byo",
    message: "Re-enter the API key to continue.",
  }));
  assert.equal(state.status, "awaiting_user_auth");
  assert.equal(state.executionQueue.state, "blocked");
  assert.equal(state.executionQueue.credentialRequired, true);
  assert.equal(state.authRequired, true);

  applyRuntimeEvent(state, event(4, "agent.event", {
    phase: "queue.credential_received",
    workItemId: "wrk-byo",
    message: "Credential received for this run only.",
  }));
  assert.equal(state.status, "awaiting_user_auth");
  assert.equal(state.executionQueue.state, "pending");
  assert.equal(state.executionQueue.credentialRequired, false);
});

test("dispatch snapshot updates queue truth without advancing the event cursor", () => {
  const state = createLifecycleState();
  state.cursor = 8;
  assert.equal(applyExecutionDispatch(state, {
    workItem: { id: "wrk-snapshot", state: "pending" },
    position: 4,
  }), true);
  assert.equal(state.cursor, 8);
  assert.equal(state.executionQueue.position, 4);
  assert.equal(state.executionQueue.ahead, 3);
  assert.equal(applyExecutionDispatch(state, {
    workItem: { id: "wrk-snapshot", state: "not-real" },
    position: 1,
  }), false);
  assert.equal(state.executionQueue.position, 4);
});

test("cursor replay reconstructs auth, tool, blocked, and error truth", () => {
  const persisted = [
    event(1, "session.created", { authMode: "pi_auth", provider: "openai-codex" }),
    event(2, "session.ready"),
    event(3, "agent.event", {
      phase: "pi.tool_execution_start",
      event: { type: "tool_execution_start", toolCallId: "detect-1", toolName: "detect" },
    }),
    event(4, "agent.event", {
      phase: "pi.tool_execution_end",
      event: {
        type: "tool_execution_end",
        toolCallId: "detect-1",
        toolName: "detect",
        isError: true,
      },
    }),
    event(5, "session.blocked", {
      code: "PDF_PERCEPTION_PACKAGE_REQUIRED",
      message: "Frozen PDF perception package is required.",
    }),
  ];
  const state = createLifecycleState();
  for (const item of persisted) applyRuntimeEvent(state, item);
  assert.equal(state.cursor, 5);
  assert.equal(state.auth.status, "ready");
  assert.deepEqual(state.tools.map(({ name, status }) => ({ name, status })), [
    { name: "detect", status: "error" },
  ]);
  assert.equal(state.status, "blocked");
  assert.equal(state.error.code, "PDF_PERCEPTION_PACKAGE_REQUIRED");
});

test("only durable session.ready opens a completed Pi turn", () => {
  const state = createLifecycleState();
  const persisted = [
    event(1, "session.created", {
      authMode: "api_key",
      provider: "openai",
      source: { mode: "live_pi_sdk", reconstructed: false },
    }),
    event(2, "session.ready"),
    event(3, "agent.event", { phase: "extraction.started" }),
    event(4, "agent.event", { phase: "pi.agent_end" }),
    event(5, "agent.event", { phase: "pi.agent_settled" }),
  ];
  for (const item of persisted) applyRuntimeEvent(state, item);
  assert.equal(state.cursor, 5);
  assert.equal(state.status, "running");

  applyRuntimeEvent(state, event(6, "agent.event", {
      phase: "extraction.awaiting_artifact",
      message: "Continue the conversation or retry extraction.",
  }));
  applyRuntimeEvent(state, event(7, "session.ready", {
    reason: "awaiting_artifact",
    message: "Pi turn completed; the conversation is ready for another prompt.",
  }));
  assert.equal(state.status, "ready");
  applyRuntimeEvent(state, event(8, "agent.event", { phase: "pi.agent_settled" }));
  assert.equal(state.status, "ready");
  assert.equal(
    state.message,
    "Pi turn completed; the conversation is ready for another prompt.",
  );
});

test("persisted Pi workspace commands restore a clickable typed chat citation", () => {
  const state = createLifecycleState();
  const persisted = event(1, "agent.event", {
    phase: "workspace.command",
    workspaceCommand: {
      type: "workspace.open",
      module: "evidence",
      source: "agent",
      jobId: "paper-50",
      reactionId: "pred:1:12",
      page: 25,
      documentSource: "si",
      bboxFrame: "page_render_px",
      bbox: [745, 1044, 856, 1189],
      focus: "selected-only",
      html: "<script>not carried</script>",
    },
  });
  assert.equal(applyRuntimeEvent(state, persisted), true);
  assert.deepEqual(runtimeWorkspaceCommand(persisted), {
    type: "workspace.open",
    module: "evidence",
    source: "agent",
    jobId: "paper-50",
    reactionId: "pred:1:12",
    anchor: null,
    page: 25,
    focus: "selected-only",
    documentSource: "si",
    bboxFrame: "page_render_px",
    bbox: [745, 1044, 856, 1189],
  });
  assert.equal(state.workspaceCommands.length, 1);
  assert.equal(state.message, "Open evidence for pred:1:12 · SI p25");
  assert.equal(state.conversation[0].behavior, "workspace_action");
  assert.match(state.conversation[0].text, /Open evidence/);
  assert.equal("html" in state.conversation[0].workspaceCommand, false);
});

test("persisted correction proposals restore as awaiting human Apply, never as a write", () => {
  const state = createLifecycleState();
  const persisted = event(1, "agent.event", {
    phase: "workspace.command",
    workspaceCommand: {
      type: "review.propose",
      proposalId: "proposal-pdf-1",
      target: "pdf",
      source: "agent",
      jobId: "paper-50",
      reactionId: "pred:1:12",
      rationale: "The scale and yield are separate values.",
      evidence: ["Scheme 1: 77 g (45%)"],
      verdict: "correct fields",
      correctedCondition: "scale = 77 g; yield = 45%",
      apply: true,
    },
  });
  assert.equal(applyRuntimeEvent(state, persisted), true);
  const command = runtimeWorkspaceCommand(persisted);
  assert.equal(command.type, "review.propose");
  assert.equal("apply" in command, false);
  assert.match(state.conversation[0].text, /awaiting human Apply/);
  assert.equal(state.status, "running");
});

test("persisted review outcomes update the original proposal without changing job status", () => {
  const state = createLifecycleState();
  applyRuntimeEvent(state, event(1, "agent.event", {
    phase: "workspace.command",
    workspaceCommand: {
      type: "review.propose",
      proposalId: "proposal-figure-1",
      target: "figure",
      source: "agent",
      jobId: "figure-1",
      reactionId: "3b",
      rationale: "The product stereochemistry differs from the source.",
      evidence: ["bbox 4"],
      changes: [{
        kind: "molecule",
        side: "products",
        index: 0,
        field: "E-SMILES",
        value: "C[C@@H](O)Cl",
      }],
    },
  }));
  applyRuntimeEvent(state, event(2, "job.review_ready"));
  assert.equal(applyRuntimeEvent(state, event(3, "review.outcome", {
    proposalId: "proposal-figure-1",
    jobId: "figure-1",
    target: "figure",
    reactionId: "3b",
    status: "staged",
  })), true);
  assert.equal(state.status, "review_ready");
  assert.equal(state.conversation[0].proposalStatus, "staged");
  assert.equal(applyRuntimeEvent(state, event(4, "review.outcome", {
    proposalId: "proposal-figure-1",
    jobId: "figure-1",
    target: "figure",
    reactionId: "3b",
    status: "applied",
    reviewedVersion: 2,
  })), true);
  assert.equal(state.status, "review_ready");
  assert.equal(state.conversation.length, 1);
  assert.equal(state.conversation[0].proposalStatus, "applied");
  assert.equal(state.conversation[0].reviewedVersion, 2);
});

test("history hydration replays only the latest unresolved workspace intent", () => {
  const proposal = {
    type: "review.propose",
    proposalId: "proposal-figure-fast",
    target: "figure",
    source: "agent",
    jobId: "figure-fast",
    reactionId: "example:0",
    rationale: "The product label needs human verification.",
    evidence: ["bbox 3"],
    changes: [{
      kind: "molecule",
      side: "products",
      index: 0,
      field: "description",
      value: "Human verification requested",
    }],
  };
  const state = createLifecycleState();
  applyRuntimeEvent(state, event(1, "agent.event", {
    phase: "workspace.command",
    workspaceCommand: {
      type: "workspace.open",
      module: "edit",
      source: "agent",
      jobId: "figure-fast",
      reactionId: "example:0",
      focus: "selected-only",
    },
  }));
  applyRuntimeEvent(state, event(2, "agent.event", {
    phase: "workspace.command",
    workspaceCommand: proposal,
  }));
  assert.deepEqual(latestReplayableWorkspaceCommand(state), state.conversation[1].workspaceCommand);

  applyRuntimeEvent(state, event(3, "review.outcome", {
    proposalId: "proposal-figure-fast",
    status: "staged",
  }));
  assert.deepEqual(latestReplayableWorkspaceCommand(state), state.conversation[1].workspaceCommand);
});

test("proposal citations and history restoration share one terminal-status contract", () => {
  assert.equal(isTerminalReviewProposalStatus("proposed"), false);
  assert.equal(isTerminalReviewProposalStatus("staged"), false);
  for (const status of ["applied", "discarded", "expired", "rejected"]) {
    assert.equal(isTerminalReviewProposalStatus(status), true);
  }
});

for (const terminalStatus of ["applied", "discarded", "expired", "rejected"]) {
  test(`history hydration does not reopen a ${terminalStatus} correction proposal`, () => {
    const state = createLifecycleState();
    applyRuntimeEvent(state, event(1, "agent.event", {
      phase: "workspace.command",
      workspaceCommand: {
        type: "workspace.open",
        module: "edit",
        source: "agent",
        jobId: "figure-fast",
        reactionId: "example:0",
      },
    }));
    applyRuntimeEvent(state, event(2, "agent.event", {
      phase: "workspace.command",
      workspaceCommand: {
        type: "review.propose",
        proposalId: "proposal-figure-terminal",
        target: "figure",
        source: "agent",
        jobId: "figure-fast",
        reactionId: "example:0",
        rationale: "Review this correction.",
        evidence: ["bbox 3"],
        changes: [{
          kind: "molecule",
          side: "products",
          index: 0,
          field: "description",
          value: "Corrected",
        }],
      },
    }));
    applyRuntimeEvent(state, event(3, "review.outcome", {
      proposalId: "proposal-figure-terminal",
      status: terminalStatus,
    }));
    assert.equal(latestReplayableWorkspaceCommand(state), null);
  });
}

test("domain terminal events remain distinct product outcomes", () => {
  const state = createLifecycleState();
  assert.equal(applyRuntimeEvent(state, event(1, "job.review_ready")), true);
  assert.equal(state.status, "review_ready");
  assert.equal(applyRuntimeEvent(state, event(2, "job.partial")), true);
  assert.equal(state.status, "partial");
  assert.equal(applyRuntimeEvent(state, event(3, "job.cancelled")), true);
  assert.equal(state.status, "cancelled");
});

test("a persisted dormant event closes reconnect loops but keeps auth recovery explicit", () => {
  const state = createLifecycleState();
  applyRuntimeEvent(state, event(1, "session.created", {
    authMode: "api_key",
    provider: "openai",
    source: { mode: "live_pi_sdk", reconstructed: false },
  }));
  applyRuntimeEvent(state, event(2, "session.dormant", {
    credentialRequired: "api_key",
    message: "Re-enter the API key to reopen this Pi conversation.",
  }));
  assert.equal(state.status, "dormant");
  assert.equal(state.auth.status, "action_required");
  assert.match(state.message, /Re-enter/);
});

test("an accepted dormant prompt reconnects from the existing durable cursor", () => {
  let connects = 0;
  const stream = { connect() { connects += 1; return this; } };
  assert.equal(reconnectAfterAcceptedMessage("dormant", { accepted: true }, stream), true);
  assert.equal(connects, 1);
  assert.equal(reconnectAfterAcceptedMessage("ready", { accepted: true }, stream), false);
  assert.equal(reconnectAfterAcceptedMessage(
    "dormant",
    { continued: true, session: { id: "linked" } },
    stream,
  ), false);
  assert.equal(connects, 1);
});

test("runtime event validation rejects invented progress types", () => {
  assert.equal(validRuntimeEvent(event(1, "agent.event", { phase: "perception.started" })), true);
  assert.equal(validRuntimeEvent(event(2, "fake.progress", { percent: 99 })), false);
});

test("persisted Pi projections rebuild conversation, tool activity, and queue state", () => {
  const state = createLifecycleState();
  applyRuntimeEvent(state, event(1, "agent.event", {
    phase: "conversation.user_message",
    behavior: "prompt",
    text: "Extract the reactions in Scheme 1",
  }));
  applyRuntimeEvent(state, event(2, "agent.event", {
    phase: "pi.message_update",
    event: {
      type: "message_update",
      assistantMessageEvent: { type: "text_delta", delta: "I found " },
    },
  }));
  applyRuntimeEvent(state, event(3, "agent.event", {
    phase: "pi.tool_argument_progress",
    event: {
      type: "message_update",
      assistantMessageEvent: { type: "toolcall_delta", redacted: true },
      batchedChunks: 128,
      byteLength: 65536,
    },
  }));
  assert.deepEqual(state.toolArgumentProgress, {
    active: true,
    chunks: 128,
    bytes: 65536,
  });
  applyRuntimeEvent(state, event(4, "agent.event", {
    phase: "pi.tool_execution_start",
    event: { type: "tool_execution_start", toolCallId: "tool-1", toolName: "detect" },
  }));
  applyRuntimeEvent(state, event(5, "agent.event", {
    phase: "pi.tool_execution_end",
    event: {
      type: "tool_execution_end",
      toolCallId: "tool-1",
      toolName: "detect",
      isError: false,
      result: { isError: true },
    },
  }));
  applyRuntimeEvent(state, event(6, "agent.event", {
    phase: "pi.message_end",
    event: {
      type: "message_end",
      message: { role: "assistant", content: [{ type: "text", text: "I found 8 reactions." }] },
    },
  }));
  applyRuntimeEvent(state, event(7, "agent.event", {
    phase: "pi.queue_update",
    event: { type: "queue_update", steering: ["Focus on stereo"], followUp: ["Export V2"] },
  }));
  applyRuntimeEvent(state, event(8, "agent.event", {
    phase: "pi.agent_end",
    event: { type: "agent_end", messages: [] },
  }));
  assert.equal(state.status, "running");
  applyRuntimeEvent(state, event(9, "session.ready", {
    reason: "turn_complete",
    message: "Pi turn completed; the conversation is ready for another prompt.",
  }));
  assert.deepEqual(state.conversation.map(({ role, text }) => ({ role, text })), [
    { role: "user", text: "Extract the reactions in Scheme 1" },
    { role: "assistant", text: "I found 8 reactions." },
  ]);
  assert.equal(state.streamingAssistant, "");
  assert.deepEqual(state.tools.map(({ name, status }) => ({ name, status })), [
    { name: "detect", status: "error" },
  ]);
  assert.deepEqual(state.toolArgumentProgress, {
    active: false,
    chunks: 0,
    bytes: 0,
  });
  assert.deepEqual(state.queue, { steering: 1, followUp: 1 });
  assert.equal(state.status, "ready");
});

test("persisted PDF events rebuild truthful page, anchor, detection, and cache progress", () => {
  const state = createLifecycleState();
  const documentId = "doc:main:abc";
  const pdfEvent = (type, data = {}, progress) => ({
    phase: `pdf.${type}`,
    pdfEvent: {
      eventId: `pdf:${type}:${progress?.completed || 0}`,
      type,
      scope: {
        documentId,
        pageId: progress?.completed ? `${documentId}:page:${progress.completed}` : undefined,
        anchorId: type === "anchor.discovered" ? `${documentId}:anchor:1` : undefined,
      },
      data: { sourceDocument: "main", ...data },
      ...(progress ? { progress } : {}),
    },
  });
  applyRuntimeEvent(state, event(1, "agent.event", pdfEvent("document.validated")));
  applyRuntimeEvent(state, event(2, "agent.event", pdfEvent(
    "molparser.completed",
    { detectionCount: 7 },
    { completed: 1, total: 2, unit: "pages", denominator: "validated_page_count" },
  )));
  applyRuntimeEvent(state, event(3, "agent.event", pdfEvent("anchor.discovered")));
  applyRuntimeEvent(state, event(4, "agent.event", pdfEvent(
    "molparser.cache_hit",
    { detectionCount: 5 },
    { completed: 2, total: 2, unit: "pages", denominator: "validated_page_count" },
  )));
  assert.equal(state.pdf.active, true);
  assert.equal(state.pdf.completedPages, 2);
  assert.equal(state.pdf.totalPages, 2);
  assert.equal(state.pdf.anchors, 1);
  assert.equal(state.pdf.detections, 12);
  assert.equal(state.pdf.cacheHits, 1);
  assert.equal(state.pdf.phase, "molparser.cache_hit");
});

test("sealed Figure domain events rebuild the same progress projection as live events", () => {
  const state = createLifecycleState();
  const domain = (seq, phase, payload = {}, progress, artifactIds = []) =>
    event(seq, "agent.event", {
      phase,
      domainContract: "chemrxn.figure-job.v1",
      domainEventId: `figure:${seq}`,
      payload,
      progress: progress || null,
      artifactIds,
    });
  applyRuntimeEvent(state, domain(1, "molparser.cache_hit", { detectionCount: 75 },
    { completed: 75, total: 75, unit: "detections" }));
  applyRuntimeEvent(state, domain(2, "reaction.candidate", { reactionId: "rxn:1" },
    { completed: 1, total: 24, unit: "reactions" }));
  applyRuntimeEvent(state, domain(3, "artifact.created", {}, undefined, ["artifact:v2"]));
  assert.deepEqual(state.figure, {
    active: true,
    phase: "artifact.created",
    completed: 1,
    total: 24,
    unit: "reactions",
    candidates: 1,
    artifacts: 1,
    detections: 75,
    cacheStatus: "cache hit",
  });
});

test("sealed Figure tool events rebuild a compact disclosed tool ledger", () => {
  const state = createLifecycleState();
  state.source = { mode: "sealed_replay", reconstructed: true };
  const domain = (seq, phase, tool, callId) => event(seq, "agent.event", {
    phase,
    domainContract: "chemrxn.figure-job.v1",
    domainEventId: `figure:${seq}`,
    payload: { tool, callId, status: phase === "tool.completed" ? "completed" : undefined },
    artifactIds: [],
  });
  applyRuntimeEvent(state, domain(1, "tool.started", "verify_smiles", "verify:1"));
  applyRuntimeEvent(state, domain(2, "tool.completed", "verify_smiles", "verify:1"));
  applyRuntimeEvent(state, domain(3, "tool.started", "verify_smiles", "verify:2"));
  applyRuntimeEvent(state, domain(4, "tool.completed", "verify_smiles", "verify:2"));
  applyRuntimeEvent(state, domain(5, "tool.started", "assemble", "assemble:1"));
  applyRuntimeEvent(state, domain(6, "tool.completed", "assemble", "assemble:1"));

  assert.deepEqual(summarizeToolActivity(state.tools), [
    {
      name: "verify_smiles",
      count: 2,
      complete: 2,
      running: 0,
      error: 0,
      reconstructed: true,
      status: "complete",
    },
    {
      name: "assemble",
      count: 1,
      complete: 1,
      running: 0,
      error: 0,
      reconstructed: true,
      status: "complete",
    },
  ]);
});

test("live Figure domain tools never masquerade as a reconstructed ledger", () => {
  const state = createLifecycleState();
  state.source = { mode: "live_pi_sdk", reconstructed: false };
  const domain = (seq, phase) => event(seq, "agent.event", {
    phase,
    domainContract: "chemrxn.figure-job.v1",
    domainEventId: `live-figure-tool:${seq}`,
    payload: { tool: "verify_smiles", callId: "verify:live:1" },
    artifactIds: [],
  });
  applyRuntimeEvent(state, domain(1, "tool.started"));
  applyRuntimeEvent(state, domain(2, "tool.completed"));

  assert.deepEqual(summarizeToolActivity(state.tools), [{
    name: "verify_smiles",
    count: 1,
    complete: 1,
    running: 0,
    error: 0,
    reconstructed: false,
    status: "complete",
  }]);
});

test("sealed PDF domain events and live nested PDF events share one projection", () => {
  const documentId = "doc:main:sealed";
  const state = createLifecycleState();
  applyRuntimeEvent(state, event(1, "agent.event", {
    phase: "molparser.cache_hit",
    domainContract: "chemrxn.pdf-replay.v1",
    domainEventId: "pdf:cache:page:2",
    scope: { documentId, pageId: `${documentId}:page:2` },
    progress: {
      completed: 2,
      total: 8,
      unit: "pages",
      denominator: "validated_page_count",
    },
    payload: { sourceDocument: "main", detectionCount: 13 },
    artifactIds: [],
  }));
  applyRuntimeEvent(state, event(2, "agent.event", {
    phase: "pdf.anchor.discovered",
    pdfEvent: {
      eventId: "pdf:anchor:1",
      type: "anchor.discovered",
      scope: { documentId, anchorId: `${documentId}:anchor:1` },
      data: { sourceDocument: "main" },
      artifactIds: [],
    },
  }));
  assert.equal(state.pdf.active, true);
  assert.equal(state.pdf.completedPages, 2);
  assert.equal(state.pdf.totalPages, 8);
  assert.equal(state.pdf.detections, 13);
  assert.equal(state.pdf.cacheHits, 1);
  assert.equal(state.pdf.anchors, 1);
});

test("future live Figure emitters can attach their native event without a renderer change", () => {
  const state = createLifecycleState();
  applyRuntimeEvent(state, event(1, "agent.event", {
    phase: "figure.reaction.candidate",
    figureEvent: {
      eventId: "figure:reaction:4",
      type: "reaction.candidate",
      progress: { completed: 4, total: 6, unit: "reactions" },
      data: { reactionId: "rxn:4" },
      artifactIds: [],
    },
  }));
  assert.equal(state.figure.active, true);
  assert.equal(state.figure.phase, "reaction.candidate");
  assert.equal(state.figure.completed, 4);
  assert.equal(state.figure.total, 6);
  assert.equal(state.figure.candidates, 1);
});

test("nested live PDF terminal events become product outcomes", () => {
  const state = createLifecycleState();
  applyRuntimeEvent(state, event(1, "session.created", {
    source: { mode: "live_pi_sdk", reconstructed: false },
  }));
  applyRuntimeEvent(state, event(2, "agent.event", {
    phase: "pdf.job.review_ready",
    pdfEvent: {
      eventId: "pdf:terminal",
      type: "job.review_ready",
      scope: {},
      data: { reactionCount: 53 },
      artifactIds: ["prediction", "pdf-view", "validation"],
    },
  }));
  assert.equal(state.status, "review_ready");
  assert.equal(state.message, "Extraction artifacts ready for review");
});

test("sealed replay provenance never renders as a live runtime", () => {
  const state = createLifecycleState();
  applyRuntimeEvent(state, event(1, "session.created", {
    source: { mode: "sealed_replay", reconstructed: true },
  }));
  assert.deepEqual(state.source, { mode: "sealed_replay", reconstructed: true });
  assert.deepEqual(state.auth, {
    mode: "sealed_replay",
    provider: "unknown",
    status: "not_required",
  });
  assert.equal(runtimeSourceLabel(state.source), "Sealed replay");
  assert.equal(runtimeSourceLabel({ mode: "live_pi_sdk", reconstructed: null }), "Live Pi runtime");
  assert.equal(runtimeSourceLabel({ mode: "unknown", reconstructed: null }), "Runtime");
});

test("a packaged sealed viewer is reviewable without being mislabeled live", () => {
  const summary = summarizeRuntimeResult({
    schemaVersion: "chemrxn.runtime-result.v1",
    sessionId: "sealed-pdf",
    jobId: "fixture:pdf:p50-main",
    contextMode: "paper",
    source: {
      mode: "sealed_replay",
      fixture: "pdf-example-job",
      reconstructed: true,
      liveEventCapture: false,
    },
    domainContract: "chemrxn.pdf-replay.v1",
    terminalStatus: "review_ready",
    manifest: {
      documents: [{ pageCount: 10 }],
      expected: { pageCount: 10, anchorCount: 7, reactionCount: 55 },
      artifacts: [
        { id: "main", mediaType: "application/pdf" },
        { id: "prediction", mediaType: "application/json" },
        { id: "view", mediaType: "application/json" },
      ],
    },
    viewer: {
      kind: "document",
      inputArtifactId: "main",
      predictionArtifactId: "prediction",
      overviewArtifactId: "view",
    },
    gtIncluded: false,
  });
  assert.equal(summary?.live, false);
  assert.equal(summary?.reconstructed, true);
  assert.equal(summary?.reviewable, true);
  assert.equal(summary?.artifacts.length, 3);
});

test("PDF plus SI result exposes its immutable SI document beside the shared main workspace", () => {
  const summary = summarizeRuntimeResult({
    schemaVersion: "chemrxn.runtime-result.v1",
    sessionId: "sealed-pdf-si",
    jobId: "fixture:pdf:p50-main-si",
    contextMode: "paper_si",
    source: {
      mode: "sealed_replay",
      fixture: "example-document",
      reconstructed: true,
      liveEventCapture: false,
    },
    domainContract: "chemrxn.pdf-replay.v1",
    terminalStatus: "review_ready",
    manifest: {
      documents: [
        { sourceDocument: "main", artifactId: "main", pageCount: 10 },
        { sourceDocument: "si", artifactId: "si", pageCount: 33 },
      ],
      expected: { pageCount: 10, anchorCount: 7, reactionCount: 53 },
      artifacts: [
        { id: "main", mediaType: "application/pdf" },
        { id: "si", mediaType: "application/pdf" },
        { id: "prediction", mediaType: "application/json" },
        { id: "view", mediaType: "application/json" },
      ],
    },
    viewer: {
      kind: "document",
      inputArtifactId: "main",
      predictionArtifactId: "prediction",
      overviewArtifactId: "view",
    },
    gtIncluded: false,
  });
  assert.equal(summary?.title, "PDF + SI extraction result");
  assert.equal(summary?.reviewable, true);
  assert.deepEqual(
    summary?.artifacts.map((artifact) => artifact.label),
    ["Original", "Prediction V2", "BBox overview", "Supporting information"],
  );
});

test("runtime reducer accepts only the next contiguous persisted cursor", () => {
  const state = createLifecycleState();
  assert.equal(applyRuntimeEvent(state, event(1, "session.ready")), true);
  assert.equal(applyRuntimeEvent(state, event(3, "agent.event")), false);
  assert.equal(state.cursor, 1);
  assert.equal(applyRuntimeEvent(state, event(2, "agent.event")), true);
  assert.equal(state.cursor, 2);
  assert.equal(applyRuntimeEvent(state, event(2, "agent.error")), false);
});

test("domain identities prevent repeated runtime envelopes from inflating counts", () => {
  const state = createLifecycleState();
  const repeated = (seq) => event(seq, "agent.event", {
    phase: "reaction.candidate",
    domainContract: "chemrxn.figure-job.v1",
    domainEventId: "figure:candidate:12a",
    payload: { reactionId: "rxn:12a" },
    artifactIds: [],
  });
  applyRuntimeEvent(state, repeated(1));
  applyRuntimeEvent(state, repeated(2));
  assert.equal(state.figure.candidates, 1);
});

test("result summaries expose chemistry counts without claiming live execution", () => {
  const summary = summarizeRuntimeResult({
    schemaVersion: "chemrxn.runtime-result.v1",
    gtIncluded: false,
    contextMode: "figure",
    domainContract: "chemrxn.figure-job.v1",
    source: {
      mode: "sealed_replay",
      fixture: "demo-figure",
      reconstructed: true,
      liveEventCapture: false,
    },
    manifest: {
      skill: { name: "reaction-extract", commit: "22cd03fdda4901193" },
      model: { provider: "openai-codex", id: "gpt-5.6-sol:medium" },
      counts: { examples: 24, templates: 1, successful: 24, metrics: 97 },
      molecules: new Array(75),
      bboxes: new Array(27),
      reactions: [{ id: "rxn:1", printedIdentifier: "1a", outcome: "successful" }],
    },
  });
  assert.equal(summary.title, "Figure extraction result");
  assert.equal(summary.reconstructed, true);
  assert.deepEqual(summary.metrics.find(([label]) => label === "Examples"), ["Examples", 24]);
  assert.deepEqual(summary.reactions[0], {
    id: "rxn:1",
    label: "1a",
    outcome: "successful",
  });
  assert.deepEqual(summary.execution, {
    skill: "reaction-extract",
    skillVersion: "22cd03fdda4901193",
    provider: "openai-codex",
    model: "gpt-5.6-sol:medium",
    evidenceMode: "sealed_tool_ledger",
  });
  assert.equal(summarizeRuntimeResult({ ...summary, gtIncluded: true }), null);
});

test("live Pi Figure results expose real artifacts without a sealed-fixture claim", () => {
  const summary = summarizeRuntimeResult({
    schemaVersion: "chemrxn.runtime-result.v1",
    gtIncluded: false,
    contextMode: "figure",
    terminalStatus: "review_ready",
    domainContract: "chemrxn.figure-job.v1",
    source: {
      mode: "live_pi_sdk",
      reconstructed: false,
      liveEventCapture: true,
      inputArtifactIds: ["input-a"],
      workspaceId: "session-a",
    },
    viewer: {
      kind: "figure",
      inputArtifactId: "artifact:input",
      predictionArtifactId: "artifact:prediction",
      overviewArtifactId: "artifact:overview",
    },
    manifest: {
      model: { provider: "openai", id: "gpt-5" },
      counts: { examples: 3, templates: 1, successful: 2, metrics: 4 },
      reactions: [{ id: "rxn:1", printedIdentifier: "12a", outcome: "successful" }],
      molecules: [],
      bboxes: [],
      artifacts: [
        { id: "artifact:input", mediaType: "image/png" },
        { id: "artifact:prediction", mediaType: "application/json" },
        { id: "artifact:overview", mediaType: "image/png" },
      ],
    },
  });
  assert.equal(summary.live, true);
  assert.equal(summary.reconstructed, false);
  assert.equal(summary.fixture, null);
  assert.equal(summary.sourceLabel, "openai · gpt-5");
  assert.deepEqual(summary.artifacts.map((item) => item.label), [
    "Original", "Prediction V2", "BBox overview",
  ]);
});

test("live Pi PDF result reports model identity and document artifacts", () => {
  const summary = summarizeRuntimeResult({
    schemaVersion: "chemrxn.runtime-result.v1",
    gtIncluded: false,
    contextMode: "paper_si",
    terminalStatus: "review_ready",
    domainContract: "chemrxn.pdf-replay.v1",
    source: {
      mode: "live_pi_sdk",
      reconstructed: false,
      liveEventCapture: true,
      inputArtifactIds: ["input-paper"],
      workspaceId: "session-pdf",
    },
    viewer: {
      kind: "document",
      inputArtifactId: "input-paper",
      predictionArtifactId: "artifact:prediction",
      overviewArtifactId: "artifact:view",
    },
    manifest: {
      versions: {
        skillSha256: "328b0eb37006a5300f76daf9a42d7c03",
        modelProvider: "openai-codex",
        modelId: "gpt-5.5",
      },
      expected: { pageCount: 8, anchorCount: 6, reactionCount: 42 },
      lineage: new Array(18),
      documents: [
        { sourceDocument: "main", pageCount: 8 },
        { sourceDocument: "si", pageCount: 12 },
      ],
      artifacts: [
        { id: "input-paper", mediaType: "application/pdf" },
        { id: "artifact:prediction", mediaType: "application/json" },
        { id: "artifact:view", mediaType: "application/json" },
      ],
    },
  });
  assert.equal(summary.live, true);
  assert.equal(summary.title, "PDF + SI extraction result");
  assert.equal(summary.sourceLabel, "openai-codex · gpt-5.5");
  assert.deepEqual(summary.metrics.slice(0, 2), [
    ["Main PDF pages", 8],
    ["Total document pages", 20],
  ]);
  assert.deepEqual(summary.metrics.find(([label]) => label === "Reactions"), ["Reactions", 42]);
  assert.deepEqual(summary.artifacts.map((item) => item.label), [
    "Original", "Prediction V2", "BBox overview",
  ]);
  assert.deepEqual(summary.execution, {
    skill: "pdf-reaction-extract",
    skillVersion: "328b0eb37006a5300f76daf9a42d7c03",
    provider: "openai-codex",
    model: "gpt-5.5",
    evidenceMode: "captured_live_events",
  });
});

test("paper-only result labels stay valid when legacy document totals are unavailable", () => {
  const summary = summarizeRuntimeResult({
    schemaVersion: "chemrxn.runtime-result.v1",
    gtIncluded: false,
    contextMode: "paper",
    terminalStatus: "review_ready",
    domainContract: "chemrxn.pdf-replay.v1",
    source: {
      mode: "live_pi_sdk",
      reconstructed: false,
      liveEventCapture: true,
    },
    manifest: {
      versions: { modelProvider: "Pi", modelId: "model" },
      expected: { pageCount: 8, anchorCount: 2, reactionCount: 4 },
      documents: [],
      artifacts: [],
    },
  });
  assert.equal(summary.title, "PDF extraction result");
  assert.deepEqual(summary.metrics.find(([label]) => label === "Main PDF pages"),
    ["Main PDF pages", 8]);
  assert.equal(summary.metrics.some(([label]) => label === "Total document pages"), false);
});

test("EventSource adapter reconnects from cursor and validates session identity", () => {
  class FakeEventSource {
    static instance;
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.listeners = {};
      FakeEventSource.instance = this;
    }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    close() { this.readyState = 2; }
  }
  const connections = [];
  const received = [];
  const errors = [];
  const stream = new RuntimeEventStream({
    sessionId: "session-a",
    after: 4,
    EventSourceClass: FakeEventSource,
    onConnection: (state) => connections.push(state),
    onEvent: (value) => received.push(value),
    onProtocolError: (error) => errors.push(error),
  }).connect();
  assert.equal(
    FakeEventSource.instance.url,
    "/api/runtime/sessions/session-a/events?after=4",
  );
  FakeEventSource.instance.onopen();
  FakeEventSource.instance.onmessage({ data: JSON.stringify(event(5, "agent.event")) });
  FakeEventSource.instance.onmessage({
    data: JSON.stringify({ ...event(6, "agent.event"), sessionId: "session-b" }),
  });
  assert.deepEqual(connections, ["connecting", "open"]);
  assert.deepEqual(received.map((value) => value.seq), [5]);
  assert.equal(errors.length, 1);
  assert.equal(stream.after, 5);
  stream.close();
});

test("EventSource adapter reconnects from the last contiguous cursor on a gap", () => {
  class FakeEventSource {
    static instances = [];
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.listeners = {};
      FakeEventSource.instances.push(this);
    }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    close() { this.readyState = 2; }
  }
  const received = [];
  const errors = [];
  const timers = [];
  const stream = new RuntimeEventStream({
    sessionId: "session-a",
    after: 1,
    EventSourceClass: FakeEventSource,
    onEvent: (value) => received.push(value.seq),
    onProtocolError: (error) => errors.push(error.code),
    setTimeoutFn: (callback) => {
      timers.push(callback);
      return timers.length;
    },
    clearTimeoutFn: () => {},
  }).connect();
  FakeEventSource.instances[0].onmessage({ data: JSON.stringify(event(3, "agent.event")) });
  assert.equal(FakeEventSource.instances.length, 1);
  assert.equal(timers.length, 1);
  timers.shift()();
  assert.equal(FakeEventSource.instances.length, 2);
  assert.equal(FakeEventSource.instances[1].url,
    "/api/runtime/sessions/session-a/events?after=1");
  FakeEventSource.instances[1].onmessage({ data: JSON.stringify(event(2, "agent.event")) });
  assert.deepEqual(received, [2]);
  assert.deepEqual(errors, ["RUNTIME_CURSOR_GAP"]);
  assert.equal(stream.after, 2);
  stream.close();
});

test("EventSource adapter bounds recovery when a cursor gap is permanent", () => {
  class FakeEventSource {
    static instances = [];
    constructor() {
      this.readyState = 0;
      this.listeners = {};
      FakeEventSource.instances.push(this);
    }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    close() { this.readyState = 2; }
  }
  const timers = [];
  const errors = [];
  const connections = [];
  const stream = new RuntimeEventStream({
    sessionId: "session-a",
    after: 1,
    EventSourceClass: FakeEventSource,
    maxGapRetries: 2,
    gapRetryBaseMs: 10,
    setTimeoutFn: (callback, delay) => {
      timers.push({ callback, delay });
      return timers.length;
    },
    clearTimeoutFn: () => {},
    onConnection: (connection) => connections.push(connection),
    onProtocolError: (error) => errors.push({
      code: error.code,
      retryable: error.retryable,
      retryInMs: error.retryInMs,
    }),
  }).connect();
  for (let attempt = 0; attempt < 3; attempt += 1) {
    FakeEventSource.instances[attempt].onmessage({
      data: JSON.stringify(event(3, "agent.event")),
    });
    if (attempt < 2) timers.shift().callback();
  }
  assert.equal(FakeEventSource.instances.length, 3);
  assert.deepEqual(errors, [
    { code: "RUNTIME_CURSOR_GAP", retryable: true, retryInMs: 10 },
    { code: "RUNTIME_CURSOR_GAP", retryable: true, retryInMs: 20 },
    { code: "RUNTIME_CURSOR_GAP", retryable: false, retryInMs: undefined },
  ]);
  assert.equal(connections.at(-1), "closed");
  assert.equal(stream.source, null);
  assert.equal(timers.length, 0);
  stream.close();
});

test("shared lifecycle mount keeps the compact three-pane PDF desk resumable", () => {
  const html = readFileSync(
    new URL("../../../../harness/service/harness_index.html", import.meta.url),
    "utf8",
  );
  assert.equal((html.match(/id="runtimepanel"/g) || []).length, 1);
  assert.match(html, /<div id="runtimepanel">[\s\S]*product-agent-welcome/);
  assert.match(html, /function mountRuntimeLifecycle\(\)/);
  assert.match(
    html,
    /if\(!ACTIVE_RUNTIME_SESSION\|\|!el\|\|typeof window\.mountRuntimeLifecyclePanel!=="function"\) return/,
  );
  assert.doesNotMatch(
    html,
    /reset\(\)[\s\S]{0,500}runtimepanel[^]*style\.display\s*=\s*"none"/,
  );
  assert.match(html, /body\.classList\.add\("runtime-session"\)/);
  assert.match(html, /loadRuntimeHistory/);
  assert.match(html, /fetch\("\/api\/runtime\/sessions"/);
  assert.match(html, /let autoFollow=false/);
  assert.match(
    html,
    /const newProductHome=!ACTIVE_RUNTIME_SESSION&&!ACTIVE_WORKSPACE_JOB&&!jobId&&activeId===undefined/,
  );
  assert.match(html, /if\(ACTIVE_RUNTIME_SESSION\|\|newProductHome\)\{ await loadRuntimeHistory\(\); return; \}/);
  assert.doesNotMatch(html, /aria-label="Pi Agent conversation and progress"/);
  assert.match(html, /Task history/);
  assert.match(html, /data-session-pane="history"/);
  assert.match(html, /data-session-pane="agent"/);
  assert.match(html, /data-session-pane="workspace"/);
  assert.match(html, /id="pdfagenttoggle"/);
  assert.match(html, /aria-controls="agentpane"/);
  assert.match(html, /function setPdfAgentPaneExpanded/);
  assert.match(html, /pdfAgentPaneExpanded/);
  assert.match(html, /data-pdf-agent-pane="collapsed"/);
  assert.match(
    html,
    /grid-template-columns:var\(--history-pane-width,220px\) 5px var\(--agent-pane-width,340px\) 5px minmax\(620px,1fr\)/,
  );
  assert.match(html, /role="separator"/);
  assert.match(html, /aria-valuenow="220"/);
  assert.match(html, /setAttribute\("aria-valuenow",String\(width\)\)/);
  assert.match(html, /WORKSPACE_STATE_KEY="chemrxn\.workspace-state\.v1"/);
  assert.match(html, /class="chemrxn-mark"/);
  assert.match(html, /--acc:#426da1/);
  assert.match(html, /mainPages=Number\(DOC\.documents\.main\?\.pageCount\)/);
  assert.match(html, /\+' pages total<\/span>'/);
  assert.match(html, /mountFigureReviewIsland/);
  assert.match(html, /id="figureisland"/);
  assert.match(html, /runtime-figure-workspace/);
  const figureIsland = readFileSync(
    new URL("../components/FigureReviewIsland.js", import.meta.url),
    "utf8",
  );
  assert.match(figureIsland, /CHEMRXN_REVIEWER_API_BASE/);
  assert.match(figureIsland, /h\(TaskView\)/);
  const saveBar = readFileSync(
    new URL("../components/SaveBar.js", import.meta.url),
    "utf8",
  );
  assert.match(saveBar, /pendingAgentCommand/);
  assert.match(saveBar, /human_edit_replaced/);
  assert.match(saveBar, /reviewedVersion: r\.version/);
  const reviewerApp = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  const reviewerStyles = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
  assert.match(reviewerApp, /EMBED_WORKSPACE/);
  assert.match(reviewerApp, /embed-workspace/);
  assert.match(reviewerStyles, /\.app-shell\.embed-workspace/);
  assert.match(reviewerStyles, /@media \(max-width: 640px\)/);
  assert.match(reviewerStyles, /\.task-view\.layout-side \.splitter/);
});

test("Playwright evidence uses a guarded product subdirectory outside broad roots", () => {
  const configPath = fileURLToPath(
    new URL("../../../../harness/tests/e2e/playwright.config.js", import.meta.url),
  );
  const guardPath = fileURLToPath(
    new URL(
      "../../../../harness/tests/e2e/playwright-output-guard.js",
      import.meta.url,
    ),
  );
  const repoRoot = realpathSync(fileURLToPath(new URL("../../../../", import.meta.url)));
  const loadGuard = (evidenceParent) => spawnSync(
    process.execPath,
    [
      "-e",
      "const guard=require(process.argv[1]);process.stdout.write(guard.evidenceOutputDir())",
      guardPath,
    ],
    {
      encoding: "utf8",
      env: { ...process.env, PDF_REVIEWER_EVIDENCE: evidenceParent },
    },
  );

  for (const unsafe of [homedir(), tmpdir(), repoRoot]) {
    const result = loadGuard(unsafe);
    assert.notEqual(result.status, 0, `unsafe output parent was accepted: ${unsafe}`);
  }
  if (process.platform === "darwin") {
    const aliases = [
      repoRoot.replace(/^\/Users\//, "/users/"),
      join("/System/Volumes/Data", repoRoot),
    ].filter((value) => existsSync(value));
    for (const alias of aliases) {
      const result = loadGuard(alias);
      assert.notEqual(result.status, 0, `repo alias was accepted: ${alias}`);
    }
  }

  const missingContainer = mkdtempSync(
    join(tmpdir(), "chemrxn-evidence-missing-container-"),
  );
  const missingParent = join(missingContainer, "absent");
  assert.notEqual(
    loadGuard(missingParent).status,
    0,
    "missing evidence parent was accepted",
  );
  rmSync(missingContainer, { recursive: true, force: true });
  const symlinkTarget = mkdtempSync(join(tmpdir(), "chemrxn-evidence-real-"));
  const symlinkParent = `${symlinkTarget}-link`;
  symlinkSync(symlinkTarget, symlinkParent, process.platform === "win32" ? "junction" : "dir");
  assert.notEqual(
    loadGuard(symlinkParent).status,
    0,
    "symlink evidence parent was accepted",
  );
  rmSync(symlinkParent, { force: true });
  rmSync(symlinkTarget, { recursive: true, force: true });

  const safeParent = mkdtempSync(
    join(tmpdir(), "chemrxn-evidence-parent-contract-"),
  );
  const result = loadGuard(safeParent);
  assert.equal(result.status, 0, result.stderr);
  assert.ok(
    result.stdout.startsWith(
      `${realpathSync(safeParent)}/chemrxn-pdf-reviewer-test-results-`,
    ),
    result.stdout,
  );
  assert.equal(existsSync(result.stdout), true);
  const reviewerE2e = readFileSync(
    new URL("../../../../harness/tests/e2e/figure-workbench.spec.js", import.meta.url),
    "utf8",
  );
  assert.match(reviewerE2e, /testInfo\.outputPath\("figure-demo\.png"\)/);
  assert.doesNotMatch(reviewerE2e, /process\.env\.PDF_REVIEWER_EVIDENCE/);

  const configSource = readFileSync(configPath, "utf8");
  assert.match(configSource, /require\("\.\/playwright-output-guard"\)/);
  assert.match(configSource, /rejectOutputOverride\(\)/);
  assert.match(configSource, /outputDir: evidenceOutputDir\(\)/);
  const victim = mkdtempSync(join(tmpdir(), "chemrxn-playwright-output-victim-"));
  const marker = join(victim, "must-survive.txt");
  writeFileSync(marker, "do not delete", "utf8");
  const override = spawnSync(
    process.execPath,
    [
      "-e",
      "require(process.argv[1]).rejectOutputOverride(['node','test','--output',process.argv[2]])",
      guardPath,
      victim,
    ],
    {
      encoding: "utf8",
    },
  );
  assert.notEqual(override.status, 0, "Playwright --output override was accepted");
  assert.equal(readFileSync(marker, "utf8"), "do not delete");
  rmSync(victim, { recursive: true, force: true });
  rmSync(safeParent, { recursive: true, force: true });
});

test("PDF molecule entity selection survives the existing session document remount", () => {
  const html = readFileSync(
    new URL("../../../../harness/service/harness_index.html", import.meta.url),
    "utf8",
  );
  const island = readFileSync(
    new URL("../components/PdfReviewIsland.js", import.meta.url),
    "utf8",
  );
  assert.match(html, /restored\.selectedEntityKey/);
  assert.match(html, /selectedEntityOccurrenceIds:\s*\(DOC\.selectedEntityOccurrenceIds\|\|\[\]\)/);
  assert.match(html, /initialEntityKey:\s*DOC\.selectedEntityKey\|\|null/);
  assert.match(html, /initialEntityOccurrenceIds:\s*DOC\.selectedEntityOccurrenceIds/);
  assert.match(island, /selectedEntityKey: typeof model\.initialEntityKey === "string"/);
  assert.match(island, /Array\.isArray\(model\.initialEntityOccurrenceIds\)/);
});

test("sealed extraction turns present an explicit linked-task composer", () => {
  const component = readFileSync(
    new URL("../components/RuntimeLifecyclePanel.js", import.meta.url),
    "utf8",
  );
  assert.match(component, /const canMessage = \(\) => \["running", "ready", "dormant"\]\.includes\(state\.status\)/);
  assert.match(component, /sealedTurn\(\) && state\.source\.mode === "live_pi_sdk"/);
  assert.match(component, /state\.status === "dormant" && state\.auth\.mode === "api_key"/);
  assert.match(component, /compose\.apiKey = ""/);
  assert.match(component, /This extraction turn is sealed/);
  assert.match(component, /read-only sealed preview/);
  assert.match(component, /window\.location\.assign\(target\.toString\(\)\)/);
  assert.match(component, /\/retry/);
  assert.match(component, /Retry as new attempt/);
  assert.match(component, /state\.connection = sealedTurn\(\) \? "sealed" : "complete"/);
  assert.match(component, /if \(state\.source\.mode !== "sealed_replay"\)/);
  assert.match(component, /stream\?\.close\(\)/);
  assert.match(component, /element\.__runtimeLifecycleSessionId === options\.sessionId/);
  assert.match(component, /element\.__runtimeLifecycleSessionId = options\.sessionId/);
  assert.match(component, /runtime-agent-header/);
  assert.match(component, /runtime-diagnostics/);
  assert.match(component, /runtimeConversationDisplay/);
  assert.match(component, /PDF extraction/);
  assert.match(component, /runtime-task-contract/);
  assert.match(component, /event\.key === "Enter" && !event\.shiftKey/);
  assert.match(component, /openVisualWorkspace\(\{ automatic: true \}\)/);
  assert.match(component, /searchParams\.set\("runtime_session", body\.session\.id\)/);
  assert.match(component, /subscribeReviewOutcomes/);
  assert.match(component, /reviewOutcomeQueue = reviewOutcomeQueue/);
  assert.match(component, /\/review-outcomes/);
  assert.match(component, /attempt < 3/);
  assert.match(component, /Retry status sync/);
  assert.match(component, /proposalTerminal/);
  assert.match(component, /disabled: proposalTerminal/);
  assert.match(component, /isTerminalReviewProposalStatus\(message\.proposalStatus\)/);
});

test("lifecycle UI exposes isolated IDs, busy state, and complete live announcements", () => {
  const component = readFileSync(
    new URL("../components/RuntimeLifecyclePanel.js", import.meta.url),
    "utf8",
  );
  const upload = readFileSync(
    new URL("../components/FigureUpload.js", import.meta.url),
    "utf8",
  );
  const submission = readFileSync(
    new URL("./product-submission.js", import.meta.url),
    "utf8",
  );
  const styles = readFileSync(
    new URL("../pdf-product.css", import.meta.url),
    "utf8",
  );
  assert.match(component, /runtime-lifecycle-title-\$\{suffix\}/);
  assert.match(component, /runtime-message-\$\{suffix\}/);
  assert.match(component, /"aria-busy": String\(/);
  assert.match(component, /"aria-atomic": "true"/);
  assert.match(upload, /class: `backend-status[\s\S]*"aria-live": "polite"/);
  assert.match(submission, /form\.append\("auth_mode", submission\.authMode\)/);
  assert.match(upload, /await createPreflightedExtraction/);
  assert.match(upload, /snapshotProductSubmission\(state\)/);
  assert.match(upload, /buildProductExtractionInput\(submission\)/);
  assert.match(upload, /Show.*advanced settings/);
  assert.match(upload, /Use my API key/);
  assert.match(upload, /state\.apiKey = ""/);
  assert.match(upload, /defaultProductApiModel\(state\.apiProvider\)/);
  assert.match(upload, /PRODUCT_API_PROVIDER_OPTIONS\.map/);
  assert.match(upload, /never remembered by the browser/);
  assert.match(upload, /fetch\("\/api\/samples"/);
  assert.match(upload, /Loading never starts a model/);
  assert.match(upload, /void loadSample\(sample\)/);
  assert.match(upload, /ChemRxn-only directory/);
  assert.match(upload, /classifyProductSources/);
  assert.doesNotMatch(upload, /Swap Main \/ SI/);
  assert.match(styles, /\.runtime-lifecycle :is\(a,button,input,select,textarea\):focus-visible/);
  assert.match(component, /\/credentials/);
  assert.match(component, /compose\.apiKey = "";[\s\S]*JSON\.stringify\(\{ apiKey \}\)/);
  assert.doesNotMatch(component, /localStorage|sessionStorage/);
  assert.match(component, /Continue queued extraction/);
  assert.match(styles, /\.runtime-message\.is-task-contract/);
  assert.match(styles, /\.runtime-current small\{color:#aab3c3/);
  assert.match(styles, /\.pdf-reviewer-v2 \.runtime-current small\{color:#667085\}/);
});
