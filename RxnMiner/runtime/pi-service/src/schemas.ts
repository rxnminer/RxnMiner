import { z } from "zod";
import {
  RUNTIME_EVENT_TYPES,
  SEALED_REPLAY_FIXTURES,
  SESSION_STATUSES,
  PI_THINKING_LEVELS,
  DEFAULT_EXTRACTION_THINKING_LEVEL,
} from "./contracts.js";

export const createSessionRequestSchema = z.strictObject({
  userId: z.string().min(1).max(256),
  jobId: z.string().min(1).max(256),
  task: z.enum(["extract_all", "inspect_selection", "propose_patch"]),
  contextMode: z.enum(["figure"]),
  artifacts: z.array(z.strictObject({
    id: z.string().min(1).max(512),
    kind: z.enum(["figure", "paper", "si", "selection", "prediction", "reviewed_snapshot"]),
    sha256: z.string().regex(/^[a-fA-F0-9]{64}$/),
    mediaType: z.string().min(1).max(256),
  })).max(10_000),
  auth: z.strictObject({
    mode: z.enum(["api_key", "enterprise_access_token", "personal_codex_subscription"]),
    provider: z.string().min(1).max(128),
    credentialRef: z.string().min(1).max(512),
  }),
  model: z.strictObject({
    provider: z.string().min(1).max(128),
    id: z.string().min(1).max(256),
    thinkingLevel: z.enum(PI_THINKING_LEVELS).default(DEFAULT_EXTRACTION_THINKING_LEVEL),
  }),
});

export const sessionRecordSchema = z.strictObject({
  id: z.string().min(1).max(128),
  userId: z.string().min(1).max(256),
  jobId: z.string().min(1).max(256),
  task: z.enum(["extract_all", "inspect_selection", "propose_patch"]),
  contextMode: z.enum(["figure"]).optional(),
  inputNames: z.array(z.string().min(1).max(512)).max(2).optional(),
  parentSessionId: z.string().min(1).max(128).optional(),
  parentResultSha256: z.string().regex(/^[a-fA-F0-9]{64}$/).optional(),
  status: z.enum(SESSION_STATUSES),
  authMode: z.enum([
    "api_key",
    "enterprise_access_token",
    "personal_codex_subscription",
    "sealed_replay",
  ]),
  thinkingLevel: z.enum(PI_THINKING_LEVELS).optional(),
  sandboxId: z.string().min(1).max(256).optional(),
  createdAt: z.string().min(1).max(64),
  updatedAt: z.string().min(1).max(64),
  blockReason: z.string().min(1).max(256).optional(),
});

export const runtimeEventSchema = z.strictObject({
  seq: z.number().int().positive(),
  sessionId: z.string().min(1).max(128),
  at: z.string().min(1).max(64),
  type: z.enum(RUNTIME_EVENT_TYPES),
  data: z.unknown(),
});

export const sealedReplayRequestSchema = z.strictObject({
  userId: z.string().min(1).max(256),
  fixture: z.enum(SEALED_REPLAY_FIXTURES),
});

export const runtimeJobResultSchema = z.strictObject({
  schemaVersion: z.literal("chemrxn.runtime-result.v1"),
  sessionId: z.string().min(1).max(128),
  jobId: z.string().min(1).max(256),
  contextMode: z.enum(["figure"]),
  source: z.union([
    z.strictObject({
      mode: z.literal("sealed_replay"),
      fixture: z.enum(SEALED_REPLAY_FIXTURES),
      reconstructed: z.literal(true),
      liveEventCapture: z.boolean(),
    }),
    z.strictObject({
      mode: z.literal("live_pi_sdk"),
      reconstructed: z.literal(false),
      liveEventCapture: z.literal(true),
      inputArtifactIds: z.array(z.string().min(1).max(128)).min(1).max(2),
      workspaceId: z.string().regex(/^[A-Za-z0-9._-]{1,256}$/),
    }),
  ]),
  domainContract: z.enum(["chemrxn.figure-job.v1", "chemrxn.pdf-replay.v1"]),
  terminalStatus: z.enum(["review_ready", "partial", "cancelled", "interrupted", "failed"]),
  manifest: z.unknown(),
  createdAt: z.string().min(1).max(64),
  gtIncluded: z.literal(false),
  viewer: z.strictObject({
    kind: z.enum(["figure", "document"]),
    inputArtifactId: z.string().min(1).max(128),
    predictionArtifactId: z.string().min(1).max(512).optional(),
    overviewArtifactId: z.string().min(1).max(512).optional(),
    reactionsArtifactId: z.string().min(1).max(512).optional(),
  }).optional(),
});

export const localAgentCreateRequestSchema = z.strictObject({
  userId: z.string().min(1).max(256),
  jobId: z.string().min(1).max(256),
  contextMode: z.enum(["figure"]),
  workspaceId: z.string().regex(/^[A-Za-z0-9._-]{1,128}$/),
  provider: z.string().min(1).max(128),
  modelId: z.string().min(1).max(256),
  thinkingLevel: z.enum(PI_THINKING_LEVELS).default(DEFAULT_EXTRACTION_THINKING_LEVEL),
  authMode: z.enum(["api_key", "pi_auth"]).default("api_key"),
  apiKey: z.string().min(8).max(16_384).optional(),
  inputs: z.array(z.strictObject({
    artifactId: z.string().min(1).max(128),
    kind: z.enum(["figure"]),
  })).max(2).optional(),
  startExtraction: z.boolean().optional(),
  initialMessage: z.string().min(1).max(20_000).optional(),
}).superRefine((request, context) => {
  if (request.authMode === "api_key" && !request.apiKey) {
    context.addIssue({
      code: "custom",
      path: ["apiKey"],
      message: "apiKey is required when authMode is api_key",
    });
  }
  if (request.authMode === "pi_auth" && request.apiKey) {
    context.addIssue({
      code: "custom",
      path: ["apiKey"],
      message: "apiKey must not be sent when authMode is pi_auth",
    });
  }
  if (request.authMode === "pi_auth" && request.provider !== "openai-codex") {
    context.addIssue({
      code: "custom",
      path: ["provider"],
      message: "pi_auth requires the openai-codex provider",
    });
  }
});

export const piModelResolutionSchema = z.strictObject({
  provider: z.string().min(1).max(128),
  modelId: z.string().min(1).max(256),
  requireImage: z.boolean().default(false),
});

export const localAgentMessageSchema = z.strictObject({
  text: z.string().min(1).max(20_000),
  behavior: z.enum(["prompt", "steer", "follow_up"]).default("prompt"),
  apiKey: z.string().min(8).max(16_384).optional(),
});

export const localAgentRetrySchema = z.strictObject({
  apiKey: z.string().min(8).max(16_384).optional(),
});

export const localAgentCredentialSchema = z.strictObject({
  apiKey: z.string().min(8).max(16_384),
});

export const reviewOutcomeRequestSchema = z.strictObject({
  proposalId: z.string().regex(/^[A-Za-z0-9._:-]{1,200}$/),
  jobId: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/),
  target: z.enum(["figure", "pdf"]),
  status: z.enum(["staged", "applied", "discarded", "expired", "rejected"]),
  reviewedVersion: z.number().int().positive().optional(),
  patchId: z.string().min(1).max(256).optional(),
  reason: z.enum([
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
  ]).optional(),
}).superRefine((value, context) => {
  if (value.status === "applied" && value.reviewedVersion == null) {
    context.addIssue({
      code: "custom",
      path: ["reviewedVersion"],
      message: "reviewedVersion is required for an applied proposal",
    });
  }
  if (value.status === "applied" && value.patchId == null) {
    context.addIssue({
      code: "custom",
      path: ["patchId"],
      message: "patchId is required for an applied proposal",
    });
  }
  if (value.status !== "applied" && (value.reviewedVersion != null || value.patchId != null)) {
    context.addIssue({
      code: "custom",
      path: ["status"],
      message: "Only an applied proposal may carry a reviewed version or patch ID",
    });
  }
});
