import { resolveFigureReactionKey } from "./workspace-command.js";

function clone(value) {
  // Vue exposes taskData as a reactive Proxy, which structuredClone rejects.
  // The persisted V2 contract is JSON, so this also strips transient reactivity.
  return JSON.parse(JSON.stringify(value));
}

function existingArray(value, label) {
  if (!Array.isArray(value)) throw new TypeError(`${label} does not exist in this reaction`);
  return value;
}

function existingObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${label} does not exist in this reaction`);
  }
  return value;
}

export function stageFigureReviewProposal(taskData, command) {
  if (command?.type !== "review.propose" || command.target !== "figure") {
    throw new TypeError("Expected a Figure review proposal");
  }
  const key = resolveFigureReactionKey(taskData, command.reactionId);
  if (!key) throw new TypeError(`Reaction ${command.reactionId} is not present`);
  const [kind, indexText] = key.split(":");
  const collection = kind === "template" ? "reaction_templates" : "selected_examples";
  const output = clone(taskData);
  const row = existingObject(output?.[collection]?.[Number(indexText)], "Reaction");
  for (const change of command.changes) {
    if (change.kind === "molecule") {
      const molecule = existingObject(
        existingArray(row[change.side], change.side)[change.index],
        `${change.side}[${change.index}]`,
      );
      molecule[change.field] = change.value;
    } else if (change.kind === "condition") {
      const step = existingArray(row.conditions, "conditions")[change.stepIndex];
      const items = Array.isArray(step) ? step : [step];
      const condition = existingObject(items[change.itemIndex], "Condition");
      condition[change.field] = change.value;
    } else if (change.kind === "metric") {
      const metric = existingObject(
        existingArray(row.metrics, "metrics")[change.index],
        `metrics[${change.index}]`,
      );
      metric[change.field] = change.value;
    } else {
      throw new TypeError("Unsupported Figure correction");
    }
  }
  return Object.freeze({ data: output, reactionKey: key });
}
