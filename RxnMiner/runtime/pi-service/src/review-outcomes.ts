import type {
  ReviewOutcomeRequest,
  RuntimeEvent,
} from "./contracts.js";
import { RuntimePolicyError } from "./errors.js";
import type { RuntimeStore } from "./store.js";

const TERMINAL = new Set(["applied", "discarded"]);

function recordData(event: RuntimeEvent): Record<string, unknown> {
  return event.data && typeof event.data === "object" && !Array.isArray(event.data)
    ? event.data as Record<string, unknown> : {};
}

function workspaceCommand(event: RuntimeEvent): Record<string, unknown> | null {
  if (event.type !== "agent.event") return null;
  const data = recordData(event);
  if (data.phase !== "workspace.command") return null;
  const command = data.workspaceCommand;
  return command && typeof command === "object" && !Array.isArray(command)
    ? command as Record<string, unknown> : null;
}

export function recordReviewOutcome(
  store: RuntimeStore,
  userId: string,
  sessionId: string,
  request: ReviewOutcomeRequest,
): { event: RuntimeEvent; idempotent: boolean } {
  const session = store.getSession(userId, sessionId);
  const events = store.listEvents(userId, sessionId, 0);
  const proposal = events
    .map(workspaceCommand)
    .find((command) =>
      command?.type === "review.propose"
      && command.proposalId === request.proposalId);
  if (!proposal) {
    throw new RuntimePolicyError(
      "REVIEW_PROPOSAL_NOT_FOUND",
      "Review proposal is not present in this persisted session",
      404,
    );
  }
  if (proposal.jobId !== request.jobId || proposal.target !== request.target) {
    throw new RuntimePolicyError(
      "REVIEW_PROPOSAL_MISMATCH",
      "Review outcome does not belong to the persisted proposal",
      409,
    );
  }
  if (request.status === "applied" && !request.patchId) {
    throw new RuntimePolicyError(
      "REVIEW_PROPOSAL_MISMATCH",
      "Applied review outcome requires an immutable reviewer patch ID",
      400,
    );
  }
  const outcomes = events.filter((event) =>
    event.type === "review.outcome"
    && recordData(event).proposalId === request.proposalId);
  const latest = outcomes.at(-1);
  const latestData = latest ? recordData(latest) : null;
  if (
    latest
    && latestData?.status === request.status
    && latestData.reviewedVersion === request.reviewedVersion
    && latestData.patchId === request.patchId
    && latestData.reason === request.reason
  ) {
    return { event: latest, idempotent: true };
  }
  if (latestData && TERMINAL.has(String(latestData.status))) {
    throw new RuntimePolicyError(
      "REVIEW_PROPOSAL_TERMINAL",
      `Review proposal is already ${String(latestData.status)}`,
      409,
    );
  }
  const event = store.appendEvent(userId, sessionId, "review.outcome", {
    proposalId: request.proposalId,
    jobId: request.jobId,
    target: request.target,
    reactionId: proposal.reactionId,
    status: request.status,
    ...(request.reviewedVersion != null ? { reviewedVersion: request.reviewedVersion } : {}),
    ...(request.patchId ? { patchId: request.patchId } : {}),
    ...(request.reason ? { reason: request.reason } : {}),
  });
  session.updatedAt = event.at;
  store.putSession(session);
  return { event, idempotent: false };
}
