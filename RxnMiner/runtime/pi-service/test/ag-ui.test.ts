import assert from "node:assert/strict";
import { test } from "node:test";
import { toAgUi } from "../src/ag-ui.js";
import type { RuntimeEvent } from "../src/contracts.js";

function event(
  seq: number,
  type: RuntimeEvent["type"],
  data: unknown = {},
): RuntimeEvent {
  return {
    seq,
    sessionId: "session-a",
    at: `2000-01-01T00:00:0${seq}.000Z`,
    type,
    data,
  };
}

test("AG-UI projection preserves the durable cursor for generic chat and tools", () => {
  const projected = [
    ...toAgUi(event(1, "session.created")),
    ...toAgUi(event(2, "agent.event", {
      phase: "conversation.user_message",
      text: "Inspect compound 12",
    })),
    ...toAgUi(event(3, "agent.event", {
      phase: "pi.message_update",
      event: {
        type: "message_update",
        assistantMessageEvent: { type: "text_delta", delta: "Checking stereo" },
      },
    })),
    ...toAgUi(event(4, "agent.event", {
      phase: "pi.tool_execution_start",
      event: {
        type: "tool_execution_start",
        toolCallId: "tool-1",
        toolName: "render_molecule",
        args: { apiKey: "must-not-project" },
      },
    })),
  ];
  assert.deepEqual(projected.map((item) => item.type), [
    "RUN_STARTED",
    "STATE_SNAPSHOT",
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_END",
    "TEXT_MESSAGE_CONTENT",
    "TOOL_CALL_START",
  ]);
  assert.deepEqual(projected.map((item) => item.runtimeSeq), [1, 1, 2, 2, 2, 3, 4]);
  assert.equal(
    (projected.at(-1) as { toolCallName?: string }).toolCallName,
    "render_molecule",
  );
  assert.doesNotMatch(JSON.stringify(projected), /must-not-project/);
});

test("AG-UI state and activity snapshots preserve recovery state without exposing tool arguments", () => {
  const state = toAgUi(event(5, "session.dormant", { authMode: "api_key" }));
  assert.deepEqual(state, [{
    type: "STATE_SNAPSHOT",
    snapshot: {
      sessionId: "session-a",
      status: "dormant",
      canonicalCursor: 5,
    },
    timestamp: Date.parse("2000-01-01T00:00:05.000Z"),
    runtimeSeq: 5,
  }]);
  const activity = toAgUi(event(6, "agent.event", {
    phase: "pi.tool_argument_progress",
    event: {
      type: "message_update",
      batchedChunks: 9,
      byteLength: 384,
      secretArgument: "must-not-project",
    },
  }));
  assert.equal(activity[0]?.type, "ACTIVITY_SNAPSHOT");
  assert.doesNotMatch(JSON.stringify(activity), /must-not-project/);
  assert.match(JSON.stringify(activity), /"redacted":true/);
});

test("tool results are projected once, bounded, then followed by tool end", () => {
  const projected = toAgUi(event(8, "agent.event", {
    phase: "pi.tool_execution_end",
    event: {
      type: "tool_execution_end",
      toolCallId: "tool-8",
      result: { content: [{ type: "text", text: "done" }] },
    },
  }));
  assert.deepEqual(projected.map((item) => item.type), [
    "TOOL_CALL_RESULT",
    "TOOL_CALL_END",
  ]);
  assert.match((projected[0] as { content: string }).content, /done/);
});

test("chemistry events remain typed AG-UI custom events instead of losing domain detail", () => {
  const [projected] = toAgUi(event(7, "agent.event", {
    phase: "pdf.anchor.discovered",
    pdfEvent: {
      type: "anchor.discovered",
      scope: { anchorId: "anchor:scheme-2" },
    },
  }));
  assert.equal(projected?.type, "CUSTOM");
  assert.equal((projected as { name?: string }).name, "chemrxn.pdf-replay.v1");
  assert.match(JSON.stringify(projected), /anchor:scheme-2/);
});

test("runtime failures project to AG-UI run errors without exposing a second state machine", () => {
  const [projected] = toAgUi(event(9, "job.interrupted", {
    code: "LOCAL_RUNTIME_RESTART",
    message: "Runtime restarted",
  }));
  assert.deepEqual(projected, {
    type: "RUN_ERROR",
    threadId: "session-a",
    runId: "session-a",
    message: "Runtime restarted",
    timestamp: Date.parse("2000-01-01T00:00:09.000Z"),
    runtimeSeq: 9,
  });
});
