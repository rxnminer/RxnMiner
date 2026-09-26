import assert from "node:assert/strict";
import test from "node:test";

const originalWindow = globalThis.window;
globalThis.window = new EventTarget();
const { stageFigureReviewProposal } = await import("./review-proposal.js");
globalThis.window = originalWindow;

const task = {
  reaction_templates: [],
  selected_examples: [{
    instance_id: "3b",
    reactants: [{ identifier: "1", "E-SMILES": "CC" }],
    products: [{ identifier: "3b", "E-SMILES": "C[C@H](O)Cl" }],
    conditions: [[{ role: "reagent", text: "LiTMP" }]],
    metrics: [{ role: "yield", text: "58%" }],
  }],
};

test("stages a bounded Figure proposal without mutating the current reviewed data", () => {
  const staged = stageFigureReviewProposal(task, {
    type: "review.propose",
    target: "figure",
    reactionId: "3b",
    changes: [
      { kind: "molecule", side: "products", index: 0, field: "E-SMILES", value: "C[C@@H](O)Cl" },
      { kind: "metric", index: 0, field: "text", value: "59%" },
    ],
  });
  assert.equal(task.selected_examples[0].products[0]["E-SMILES"], "C[C@H](O)Cl");
  assert.equal(task.selected_examples[0].metrics[0].text, "58%");
  assert.equal(staged.data.selected_examples[0].products[0]["E-SMILES"], "C[C@@H](O)Cl");
  assert.equal(staged.data.selected_examples[0].metrics[0].text, "59%");
  assert.equal(staged.reactionKey, "example:0");
});

test("refuses to invent missing molecules, conditions, or metrics", () => {
  assert.throws(() => stageFigureReviewProposal(task, {
    type: "review.propose",
    target: "figure",
    reactionId: "3b",
    changes: [{ kind: "molecule", side: "products", index: 9, field: "E-SMILES", value: "C" }],
  }), /does not exist/);
});
