import assert from "node:assert/strict";
import test from "node:test";
import { classifyProductSources, submissionFromAssignments } from "./product-source-classifier.js";
test("one image is accepted without changing its file object", () => {
  const file = { name: "reaction.png", type: "image/png" };
  const result = classifyProductSources([file]);
  assert.equal(result.ok, true);
  assert.equal(submissionFromAssignments(result.assignments).primary, file);
});
test("PDF, SI, mixed inputs and multiple images are rejected", () => {
  const image = { name: "reaction.png", type: "image/png" };
  const pdf = { name: "paper.pdf", type: "application/pdf" };
  for (const files of [[pdf], [image, pdf], [image, image]]) assert.equal(classifyProductSources(files).ok, false);
  assert.equal(submissionFromAssignments([{ role: "main", file: pdf }]).primary, null);
});
