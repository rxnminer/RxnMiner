import assert from "node:assert/strict";
import test from "node:test";

const originalWindow = globalThis.window;
const originalCustomEvent = globalThis.CustomEvent;
class TestCustomEvent extends Event {
  constructor(type, options = {}) {
    super(type);
    this.detail = options.detail;
  }
}
globalThis.window = new EventTarget();
globalThis.CustomEvent = TestCustomEvent;
const {
  dispatchReviewOutcome,
  normalizeReviewOutcome,
  pendingAfterReviewOutcomeSuccess,
  subscribeReviewOutcomes,
} = await import("./review-outcome.js");
globalThis.window = originalWindow;
globalThis.CustomEvent = originalCustomEvent;

test("normalizes a bounded applied outcome without undeclared payload", () => {
  const outcome = normalizeReviewOutcome({
    proposalId: "proposal:123",
    jobId: "job-1",
    target: "figure",
    status: "applied",
    reviewedVersion: 3,
    patchId: "reviewed-v3:abcdef",
    html: "<script>no</script>",
  });
  assert.equal(outcome.reviewedVersion, 3);
  assert.equal("html" in outcome, false);
  assert.equal(Object.isFrozen(outcome), true);
});

test("a newer successful disposition drops stale failed states for the same proposal", () => {
  const staged = { proposalId: "proposal-1", status: "staged" };
  const rejected = { proposalId: "proposal-1", status: "rejected" };
  const other = { proposalId: "proposal-2", status: "staged" };
  const orders = new Map([[staged, 1], [rejected, 2], [other, 1]]);
  assert.deepEqual(
    pendingAfterReviewOutcomeSuccess(
      [staged, rejected, other],
      rejected,
      (item) => orders.get(item),
    ),
    [other],
  );
});

test("rejects status/version mismatches and unsafe proposal IDs", () => {
  assert.throws(() => normalizeReviewOutcome({
    proposalId: "bad/id",
    jobId: "job-1",
    target: "pdf",
    status: "discarded",
  }), /proposalId is invalid/);
  assert.throws(() => normalizeReviewOutcome({
    proposalId: "proposal-1",
    jobId: "../outside",
    target: "pdf",
    status: "discarded",
  }), /jobId is invalid/);
  assert.throws(() => normalizeReviewOutcome({
    proposalId: "proposal-1",
    jobId: "job-1",
    target: "pdf",
    status: "applied",
  }), /reviewedVersion/);
  assert.throws(() => normalizeReviewOutcome({
    proposalId: "proposal-1",
    jobId: "job-1",
    target: "pdf",
    status: "applied",
    reviewedVersion: 2,
  }), /patchId/);
  assert.throws(() => normalizeReviewOutcome({
    proposalId: "proposal-1",
    jobId: "job-1",
    target: "pdf",
    status: "rejected",
    reason: "Authorization: Bearer secret-token",
  }), /Unsupported review outcome reason/);
});

test("dispatch and subscribe share one validated event contract", () => {
  const target = new EventTarget();
  let received = null;
  const unsubscribe = subscribeReviewOutcomes((outcome) => {
    received = outcome;
  }, target);
  dispatchReviewOutcome({
    proposalId: "proposal-1",
    jobId: "job-1",
    target: "pdf",
    status: "staged",
  }, target);
  assert.equal(received?.status, "staged");
  assert.deepEqual(Object.keys(received), [
    "proposalId", "jobId", "target", "status",
  ]);
  assert.equal(JSON.stringify(received).includes("null"), false);
  unsubscribe();
});
