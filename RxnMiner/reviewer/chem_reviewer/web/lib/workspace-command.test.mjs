import assert from "node:assert/strict";
import test from "node:test";

import {
  WORKSPACE_MODULES,
  normalizeWorkspaceCommand,
  resolveFigureReactionKey,
} from "./workspace-command.js";

test("normalizes an allowlisted workspace command without carrying markup", () => {
  const command = normalizeWorkspaceCommand({
    type: "workspace.open",
    module: "evidence",
    source: "agent",
    jobId: "paper-50",
    reactionId: "pred:1:12",
    page: 25,
    focus: "selected-only",
    documentSource: "si",
    bboxFrame: "page_render_px",
    bbox: [745, 1044, 856, 1189],
    html: "<script>alert(1)</script>",
  });
  assert.deepEqual(command, {
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
  assert.equal("html" in command, false);
  assert.equal(Object.isFrozen(command), true);
});

test("rejects unknown modules, command types, and focus modes", () => {
  assert.throws(() => normalizeWorkspaceCommand({
    type: "workspace.render-html",
    module: "overview",
  }), /Unsupported workspace command/);
  assert.throws(() => normalizeWorkspaceCommand({
    type: "workspace.open",
    module: "arbitrary-agent-ui",
  }), /Unsupported workspace module/);
  assert.throws(() => normalizeWorkspaceCommand({
    type: "workspace.open",
    module: "evidence",
    focus: "everything-and-gt",
  }), /Unsupported evidence focus/);
  assert.throws(() => normalizeWorkspaceCommand({
    type: "workspace.open",
    module: "evidence",
    documentSource: "si",
  }), /documentSource requires a page/);
  assert.throws(() => normalizeWorkspaceCommand({
    type: "workspace.open",
    module: "evidence",
    documentSource: "si",
    page: 25,
    bbox: [745, 1044, 856, 1189],
  }), /requires documentSource, page, and bboxFrame/);
  assert.throws(() => normalizeWorkspaceCommand({
    type: "workspace.open",
    module: "evidence",
    documentSource: "si",
    page: 25,
    bboxFrame: "page_render_px",
    bbox: [856, 1044, 745, 1189],
  }), /bbox is invalid/);
  assert.throws(() => normalizeWorkspaceCommand({
    type: "workspace.open",
    module: "evidence",
    bboxKey: "javascript:alert(1)",
  }), /Invalid Figure bbox key/);
  assert.throws(() => normalizeWorkspaceCommand({
    type: "workspace.open",
    module: "evidence",
    documentSource: "si",
    page: 25,
    anchor: 1,
  }), /mutually exclusive/);
  assert.throws(() => normalizeWorkspaceCommand({
    type: "workspace.open",
    module: "evidence",
    documentSource: "main",
    page: 2,
    bboxFrame: "anchor_crop_px",
    bbox: [10, 10, 20, 20],
  }), /Unsupported bbox frame/);
});

test("declares the initial adaptive workspace modules", () => {
  assert.deepEqual(WORKSPACE_MODULES, [
    "overview", "reactions", "attention", "ask", "saved-views",
    "evidence", "edit", "verify", "checks", "route", "table", "molecules",
  ]);
});

test("normalizes an exact Figure bbox key without inventing page geometry", () => {
  const command = normalizeWorkspaceCommand({
    type: "workspace.open",
    module: "evidence",
    source: "agent",
    jobId: "figure-1",
    reactionId: "5",
    bboxKey: "idx:0",
  });
  assert.equal(command.bboxKey, "idx:0");
  assert.equal("bbox" in command, false);
  assert.equal("documentSource" in command, false);
});

test("normalizes legacy flat pages into an explicit main-paper namespace", () => {
  const command = normalizeWorkspaceCommand({
    type: "workspace.open",
    module: "evidence",
    page: 3,
  });
  assert.equal(command.page, 3);
  assert.equal(command.documentSource, "main");
});

test("accepts finite signed coordinate keys emitted by the Figure renderer", () => {
  const command = normalizeWorkspaceCommand({
    type: "workspace.open",
    module: "evidence",
    bboxKey: "box:-3.5,2,18.25,40",
  });
  assert.equal(command.bboxKey, "box:-3.5,2,18.25,40");
  assert.throws(() => normalizeWorkspaceCommand({
    type: "workspace.open",
    module: "evidence",
    bboxKey: "box:100,100,50,50",
  }), /Invalid Figure bbox key/);
});

test("normalizes bounded Agent review proposals and removes undeclared capabilities", () => {
  const command = normalizeWorkspaceCommand({
    type: "review.propose",
    proposalId: "proposal-pdf-1",
    target: "pdf",
    source: "agent",
    jobId: "paper-50",
    reactionId: "pred:1:12",
    rationale: "The reported yield belongs to the selected row.",
    evidence: ["Scheme 1 footnote"],
    verdict: "correct fields",
    correctedCondition: "77 g scale; 45% yield",
    apply: true,
    html: "<button>Apply</button>",
  });
  assert.equal(command.type, "review.propose");
  assert.equal(command.correctedCondition, "77 g scale; 45% yield");
  assert.equal("apply" in command, false);
  assert.equal("html" in command, false);
  assert.equal(Object.isFrozen(command.evidence), true);
});

test("rejects unbounded or arbitrary Agent correction shapes", () => {
  assert.throws(() => normalizeWorkspaceCommand({
    type: "review.propose",
    proposalId: "proposal-pdf-invalid",
    target: "pdf",
    jobId: "paper-50",
    reactionId: "pred:1:12",
    rationale: "Do it",
    evidence: [],
    verdict: "agent says correct",
  }), /Unsupported PDF verdict/);
  assert.throws(() => normalizeWorkspaceCommand({
    type: "review.propose",
    proposalId: "proposal-figure-invalid",
    target: "figure",
    jobId: "figure-1",
    reactionId: "3b",
    rationale: "Replace arbitrary data",
    evidence: [],
    changes: [{ kind: "json-patch", field: "/anything", value: "{}" }],
  }), /Unsupported Figure correction field/);
});

test("resolves Figure reaction citations without inventing a second identity", () => {
  const task = {
    reaction_templates: [{ template_id: "T1" }],
    selected_examples: [{ instance_id: "3a" }, { instance_id: "3b" }],
  };
  assert.equal(resolveFigureReactionKey(task, "T1"), "template:0");
  assert.equal(resolveFigureReactionKey(task, "3b"), "example:1");
  assert.equal(resolveFigureReactionKey(task, "example:0"), "example:0");
  assert.equal(resolveFigureReactionKey(task, "missing"), null);
  assert.equal(resolveFigureReactionKey(task, "example:99"), null);
});

test("subscriptions replay the latest validated command to late workspace mounts", async () => {
  const originalWindow = globalThis.window;
  const originalCustomEvent = globalThis.CustomEvent;
  class TestCustomEvent extends Event {
    constructor(type, options = {}) {
      super(type);
      this.detail = options.detail;
    }
  }
  const target = new EventTarget();
  globalThis.window = target;
  globalThis.CustomEvent = TestCustomEvent;
  try {
    const module = await import(`./workspace-command.js?late=${Date.now()}`);
    module.dispatchWorkspaceCommand({
      type: "workspace.open",
      module: "attention",
      source: "agent",
      jobId: "paper-50",
    }, target);
    let replayed = null;
    const unsubscribe = module.subscribeWorkspaceCommands((command) => {
      replayed = command;
    }, target, { jobId: "paper-50" });
    assert.equal(replayed?.module, "attention");
    assert.equal(module.latestWorkspaceCommand("paper-50")?.jobId, "paper-50");
    assert.equal(module.clearLatestWorkspaceCommand("paper-50", "workspace.open"), true);
    assert.equal(module.latestWorkspaceCommand("paper-50"), null);
    let wrongJobReplay = null;
    const unsubscribeWrongJob = module.subscribeWorkspaceCommands((command) => {
      wrongJobReplay = command;
    }, target, { jobId: "paper-49" });
    assert.equal(wrongJobReplay, null);
    unsubscribeWrongJob();
    unsubscribe();
  } finally {
    globalThis.window = originalWindow;
    globalThis.CustomEvent = originalCustomEvent;
  }
});
