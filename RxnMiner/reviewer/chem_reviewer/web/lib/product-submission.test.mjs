import assert from "node:assert/strict";
import test from "node:test";
import { createPreflightedExtraction } from "../extraction-backend.js";
import {
  buildProductExtractionInput,
  productSubmissionModelInput,
  snapshotProductSubmission,
} from "./product-submission.js";

function namedBlob(name, bytes, type) {
  return Object.assign(new Blob([bytes], { type }), { name });
}

test("pending model preflight cannot reroute a captured key or source", async () => {
  const originalFigure = namedBlob("original-figure.png", "figure-a", "image/png");
  const replacementPaper = namedBlob("replacement.pdf", "%PDF-replacement", "application/pdf");
  const state = {
    authMode: "api_key",
    apiProvider: "byo-openai",
    apiModel: "  gpt-5.6-terra  ",
    thinkingLevel: "high",
    apiKey: "openai-request-key",
    sourceType: "figure",
    primary: originalFigure,
    si: null,
  };
  const submission = snapshotProductSubmission(state);
  let releasePreflight;
  const preflightGate = new Promise((resolve) => { releasePreflight = resolve; });
  let resolvedModel;
  let createdInput;
  const backend = {
    async resolveModel(input) {
      resolvedModel = input;
      await preflightGate;
      return { available: true };
    },
    async create(input) {
      createdInput = input;
      return { sessionId: "stable-submission" };
    },
  };

  const pending = createPreflightedExtraction(
    backend,
    productSubmissionModelInput(submission),
    () => buildProductExtractionInput(submission),
  );
  state.apiProvider = "byo-gemini";
  state.apiModel = "gemini-3.1-pro-preview";
  state.apiKey = "gemini-replacement-key";
  state.thinkingLevel = "off";
  state.sourceType = "paper";
  state.primary = replacementPaper;
  releasePreflight();
  await pending;

  assert.deepEqual(resolvedModel, {
    provider: "byo-openai",
    modelId: "gpt-5.6-terra",
    requireImage: true,
  });
  assert.equal(createdInput.get("source_type"), "figure");
  assert.equal(createdInput.get("llm_provider"), "byo-openai");
  assert.equal(createdInput.get("llm_model"), "gpt-5.6-terra");
  assert.equal(createdInput.get("thinking_level"), "high");
  assert.equal(createdInput.get("llm_key"), "openai-request-key");
  assert.equal(createdInput.get("primary").name, "original-figure.png");
  assert.equal(createdInput.has("si"), false);
});

test("Pi-auth submission snapshots never carry a BYO key", () => {
  const submission = snapshotProductSubmission({
    authMode: "pi_auth",
    apiProvider: "byo-openai",
    apiModel: "gpt-5.6-terra",
    thinkingLevel: "medium",
    apiKey: "must-not-cross",
    sourceType: "figure",
    primary: namedBlob("figure.png", "image", "image/png"),
    si: null,
  });
  const form = buildProductExtractionInput(submission);
  assert.equal(submission.apiKey, "");
  assert.equal(form.get("auth_mode"), "pi_auth");
  assert.equal(form.get("thinking_level"), "medium");
  assert.equal(form.has("llm_key"), false);
});
