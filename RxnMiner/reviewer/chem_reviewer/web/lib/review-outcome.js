const EVENT_NAME = "chemrxn-review-proposal-outcome";
const STATUSES = new Set(["staged", "applied", "discarded", "expired", "rejected"]);
const TARGETS = new Set(["figure", "pdf"]);
const REASONS = new Set([
  "human_edits_present",
  "superseded",
  "matches_review_head",
  "preview_failed",
  "human_edit_replaced",
  "review_head_changed",
  "human_closed",
  "workspace_target_mismatch",
  "reaction_not_found",
  "benchmark_read_only",
  "proposal_not_stageable",
  "human_navigation",
  "human_review_replaced",
  "human_discarded",
]);

function requiredString(value, maximum, label) {
  if (typeof value !== "string") throw new TypeError(`${label} must be a string`);
  const result = value.trim();
  if (!result || result.length > maximum) throw new TypeError(`${label} is invalid`);
  return result;
}

export function normalizeReviewOutcome(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new TypeError("Review outcome must be an object");
  }
  const status = String(input.status || "");
  if (!STATUSES.has(status)) throw new TypeError("Unsupported review outcome");
  const target = String(input.target || "");
  if (!TARGETS.has(target)) throw new TypeError("Unsupported review target");
  const proposalId = requiredString(input.proposalId, 200, "proposalId");
  if (!/^[A-Za-z0-9._:-]+$/.test(proposalId)) throw new TypeError("proposalId is invalid");
  const jobId = requiredString(input.jobId, 256, "jobId");
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(jobId)) throw new TypeError("jobId is invalid");
  const reviewedVersion = input.reviewedVersion == null
    ? null : Number(input.reviewedVersion);
  if (
    (status === "applied" && (!Number.isInteger(reviewedVersion) || reviewedVersion < 1))
    || (status !== "applied" && reviewedVersion != null)
  ) {
    throw new TypeError("reviewedVersion does not match the outcome");
  }
  const patchId = input.patchId == null
    ? null : requiredString(input.patchId, 256, "patchId");
  if (
    (status === "applied" && !patchId)
    || (status !== "applied" && patchId)
  ) {
    throw new TypeError("patchId does not match the outcome");
  }
  const reason = input.reason == null ? null : String(input.reason);
  if (reason && !REASONS.has(reason)) throw new TypeError("Unsupported review outcome reason");
  return Object.freeze({
    proposalId,
    jobId,
    target,
    status,
    ...(reviewedVersion != null ? { reviewedVersion } : {}),
    ...(patchId ? { patchId } : {}),
    ...(reason ? { reason } : {}),
  });
}

export function dispatchReviewOutcome(input, target = window) {
  const outcome = normalizeReviewOutcome(input);
  target.dispatchEvent(new CustomEvent(EVENT_NAME, { detail: outcome }));
  return outcome;
}

export function subscribeReviewOutcomes(listener, target = window) {
  if (typeof listener !== "function") throw new TypeError("Listener must be a function");
  const handler = (event) => {
    try {
      listener(normalizeReviewOutcome(event.detail));
    } catch (error) {
      console.warn("Rejected review outcome", error);
    }
  };
  target.addEventListener(EVENT_NAME, handler);
  return () => target.removeEventListener(EVENT_NAME, handler);
}

export function pendingAfterReviewOutcomeSuccess(
  pending, succeeded, orderOf,
) {
  if (!Array.isArray(pending) || typeof orderOf !== "function") {
    throw new TypeError("Pending review outcome queue is invalid");
  }
  const succeededOrder = orderOf(succeeded);
  return pending.filter((candidate) => (
    candidate.proposalId !== succeeded.proposalId
    || orderOf(candidate) > succeededOrder
  ));
}
