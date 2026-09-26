import assert from "node:assert/strict";
import { test } from "node:test";
import { recordReviewOutcome } from "../src/review-outcomes.js";
import { reviewOutcomeRequestSchema } from "../src/schemas.js";
import { InMemoryRuntimeStore } from "../src/store.js";

function runtimeWithProposal() {
  const store = new InMemoryRuntimeStore();
  store.putSession({
    id: "session-1",
    userId: "user-a",
    jobId: "job-1",
    task: "propose_patch",
    status: "review_ready",
    authMode: "api_key",
    createdAt: "2026-07-27T00:00:00.000Z",
    updatedAt: "2026-07-27T00:00:00.000Z",
  });
  store.appendEvent("user-a", "session-1", "agent.event", {
    phase: "workspace.command",
    workspaceCommand: {
      type: "review.propose",
      proposalId: "proposal-1",
      target: "figure",
      source: "agent",
      jobId: "job-1",
      reactionId: "3b",
      rationale: "stereo mismatch",
      evidence: ["bbox 4"],
      changes: [],
    },
  });
  return store;
}

test("persists an idempotent staged → applied review lifecycle", () => {
  const store = runtimeWithProposal();
  const staged = recordReviewOutcome(store, "user-a", "session-1", {
    proposalId: "proposal-1",
    jobId: "job-1",
    target: "figure",
    status: "staged",
  });
  assert.equal(staged.event.type, "review.outcome");
  assert.equal(staged.idempotent, false);
  const duplicate = recordReviewOutcome(store, "user-a", "session-1", {
    proposalId: "proposal-1",
    jobId: "job-1",
    target: "figure",
    status: "staged",
  });
  assert.equal(duplicate.event.seq, staged.event.seq);
  assert.equal(duplicate.idempotent, true);
  const applied = recordReviewOutcome(store, "user-a", "session-1", {
    proposalId: "proposal-1",
    jobId: "job-1",
    target: "figure",
    status: "applied",
    reviewedVersion: 2,
    patchId: "reviewed-v2:abcdef",
  });
  assert.equal((applied.event.data as { status: string }).status, "applied");
  assert.equal(store.getSession("user-a", "session-1").status, "review_ready");
});

test("linked tasks may disposition the visual workspace named by their persisted proposal", () => {
  const store = runtimeWithProposal();
  store.appendEvent("user-a", "session-1", "agent.event", {
    phase: "workspace.command",
    workspaceCommand: {
      type: "review.propose",
      proposalId: "proposal-parent-workspace",
      target: "pdf",
      source: "agent",
      jobId: "runtime_pdf_parent123",
      reactionId: "pred:0:0",
      rationale: "The linked follow-up controls its sealed parent workspace.",
      evidence: ["anchor 0"],
      changes: [],
    },
  });
  const staged = recordReviewOutcome(store, "user-a", "session-1", {
    proposalId: "proposal-parent-workspace",
    jobId: "runtime_pdf_parent123",
    target: "pdf",
    status: "staged",
  });
  assert.equal((staged.event.data as { jobId: string }).jobId, "runtime_pdf_parent123");
  assert.equal(store.getSession("user-a", "session-1").jobId, "job-1");
});

test("rejects invented proposals, cross-job outcomes, and terminal rewrites", () => {
  const store = runtimeWithProposal();
  assert.throws(() => recordReviewOutcome(store, "user-a", "session-1", {
    proposalId: "missing",
    jobId: "job-1",
    target: "figure",
    status: "discarded",
  }), /not present/);
  assert.throws(() => recordReviewOutcome(store, "user-a", "session-1", {
    proposalId: "proposal-1",
    jobId: "job-2",
    target: "figure",
    status: "discarded",
  }), /does not belong/);
  recordReviewOutcome(store, "user-a", "session-1", {
    proposalId: "proposal-1",
    jobId: "job-1",
    target: "figure",
    status: "discarded",
  });
  assert.throws(() => recordReviewOutcome(store, "user-a", "session-1", {
    proposalId: "proposal-1",
    jobId: "job-1",
    target: "figure",
    status: "applied",
    reviewedVersion: 1,
    patchId: "reviewed-v1:terminal-rewrite",
  }), /already discarded/);
});

test("review outcomes cannot become a ground-truth or credential side channel", () => {
  for (const reason of [
    "GT_SENTINEL",
    "Authorization: Bearer secret-token",
    "free-form user note",
  ]) {
    assert.equal(reviewOutcomeRequestSchema.safeParse({
      proposalId: "proposal-1",
      jobId: "job-1",
      target: "figure",
      status: "rejected",
      reason,
    }).success, false);
  }
});

test("browser-shaped outcomes omit absent optional fields and recover after transient rejection", () => {
  const browserPayload = {
    proposalId: "proposal-1",
    jobId: "job-1",
    target: "figure",
    status: "staged",
  } as const;
  const parsed = reviewOutcomeRequestSchema.parse(
    JSON.parse(JSON.stringify(browserPayload)),
  );
  assert.deepEqual(parsed, browserPayload);

  const store = runtimeWithProposal();
  recordReviewOutcome(store, "user-a", "session-1", {
    ...browserPayload,
    status: "rejected",
    reason: "preview_failed",
  });
  const restaged = recordReviewOutcome(store, "user-a", "session-1", browserPayload);
  assert.equal((restaged.event.data as { status: string }).status, "staged");
});
