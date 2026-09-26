const EVENT_NAME = "chemrxn-workspace-command";
const latestCommands = new Map();

export const WORKSPACE_MODULES = Object.freeze([
  "overview",
  "reactions",
  "attention",
  "ask",
  "saved-views",
  "evidence",
  "edit",
  "verify",
  "checks",
  "route",
  "table",
  "molecules",
]);

const MODULE_SET = new Set(WORKSPACE_MODULES);
const FOCUS_MODES = new Set(["selected-only", "current-anchor", "all-occurrences"]);
const SOURCES = new Set(["ui", "agent", "citation", "restore", "system"]);
const DOCUMENT_SOURCES = new Set(["main", "si"]);
const BBOX_FRAMES = new Set(["pdf_points", "page_render_px"]);
const REVIEW_TARGETS = new Set(["figure", "pdf"]);
const PDF_VERDICTS = new Set([
  "accept as extracted",
  "exclude from review set",
  "mark uncertain",
  "correct fields",
  "split reaction record",
  "merge with adjacent record",
]);
const FIGURE_FIELDS = Object.freeze({
  molecule: new Set(["E-SMILES", "identifier", "description"]),
  condition: new Set(["role", "text", "description", "E-SMILES"]),
  metric: new Set(["role", "text", "of"]),
});

function optionalString(value, maximum = 256) {
  if (value == null || value === "") return null;
  if (typeof value !== "string") throw new TypeError("Expected a string");
  const result = value.trim();
  if (!result || result.length > maximum) throw new TypeError("Invalid string length");
  return result;
}

function optionalInteger(value, minimum = 0) {
  if (value == null || value === "") return null;
  const result = Number(value);
  if (!Number.isInteger(result) || result < minimum) throw new TypeError("Expected an integer");
  return result;
}

function requiredString(value, maximum) {
  const result = optionalString(value, maximum);
  if (result == null) throw new TypeError("Required string is missing");
  return result;
}

function requiredProposalId(value) {
  const result = requiredString(value, 200);
  if (!/^[A-Za-z0-9._:-]+$/.test(result)) throw new TypeError("Invalid proposal ID");
  return result;
}

function requiredInteger(value, maximum) {
  const result = optionalInteger(value);
  if (result == null || (maximum != null && result > maximum)) {
    throw new TypeError("Required integer is invalid");
  }
  return result;
}

function optionalBoundedString(value, maximum) {
  if (value == null) return null;
  if (typeof value !== "string" || value.length > maximum) {
    throw new TypeError("Correction field is invalid");
  }
  return value;
}

function optionalEnum(value, allowed, label) {
  if (value == null || value === "") return null;
  const result = String(value);
  if (!allowed.has(result)) throw new TypeError(`Unsupported ${label}`);
  return result;
}

function optionalBboxKey(value) {
  const result = optionalString(value, 160);
  if (result == null) return null;
  const indexed = result.match(/^idx:(\d{1,9})$/);
  const coordinates = result.startsWith("box:")
    ? result.slice(4).split(",").map(Number) : [];
  if (
    !indexed
    && (
      coordinates.length !== 4
      || coordinates.some((coordinate) =>
        !Number.isFinite(coordinate) || Math.abs(coordinate) > 10_000_000)
      || coordinates[2] <= coordinates[0]
      || coordinates[3] <= coordinates[1]
    )
  ) {
    throw new TypeError("Invalid Figure bbox key");
  }
  return result;
}

function optionalBbox(value) {
  if (value == null) return null;
  if (!Array.isArray(value) || value.length !== 4) {
    throw new TypeError("Evidence bbox must contain four coordinates");
  }
  const result = value.map(Number);
  if (
    result.some((coordinate) =>
      !Number.isFinite(coordinate) || coordinate < 0 || coordinate > 10_000_000)
    || result[2] <= result[0]
    || result[3] <= result[1]
  ) {
    throw new TypeError("Evidence bbox is invalid");
  }
  return Object.freeze(result);
}

function normalizeEvidence(value) {
  if (!Array.isArray(value) || value.length > 24) {
    throw new TypeError("Evidence must be a bounded array");
  }
  return Object.freeze(value.map((item) => requiredString(item, 1000)));
}

function normalizeFigureChange(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new TypeError("Figure change must be an object");
  }
  const fields = FIGURE_FIELDS[input.kind];
  if (!fields || !fields.has(input.field)) throw new TypeError("Unsupported Figure correction field");
  const change = {
    kind: input.kind,
    field: input.field,
    value: typeof input.value === "string" && input.value.length <= 8192
      ? input.value : (() => { throw new TypeError("Invalid correction value"); })(),
  };
  if (input.kind === "molecule") {
    if (!["reactants", "products"].includes(input.side)) {
      throw new TypeError("Unsupported molecule side");
    }
    return Object.freeze({
      ...change,
      side: input.side,
      index: requiredInteger(input.index, 511),
    });
  }
  if (input.kind === "condition") {
    return Object.freeze({
      ...change,
      stepIndex: requiredInteger(input.stepIndex, 127),
      itemIndex: requiredInteger(input.itemIndex, 127),
    });
  }
  return Object.freeze({
    ...change,
    index: requiredInteger(input.index, 127),
  });
}

function normalizeReviewProposal(input) {
  if (!REVIEW_TARGETS.has(input.target)) throw new TypeError("Unsupported review target");
  const common = {
    type: "review.propose",
    target: input.target,
    source: "agent",
    jobId: requiredString(input.jobId, 160),
    proposalId: requiredProposalId(input.proposalId),
    reactionId: requiredString(input.reactionId, 320),
    rationale: requiredString(input.rationale, 4000),
    evidence: normalizeEvidence(input.evidence || []),
  };
  if (input.target === "figure") {
    if (!Array.isArray(input.changes) || !input.changes.length || input.changes.length > 32) {
      throw new TypeError("Figure proposal must contain bounded changes");
    }
    return Object.freeze({
      ...common,
      changes: Object.freeze(input.changes.map(normalizeFigureChange)),
    });
  }
  const verdict = input.verdict == null ? null : String(input.verdict);
  if (!verdict || !PDF_VERDICTS.has(verdict)) throw new TypeError("Unsupported PDF verdict");
  const output = {
    ...common,
    verdict,
    note: optionalBoundedString(input.note, 8000),
    correctedSmiles: optionalBoundedString(input.correctedSmiles, 8192),
    correctedCondition: optionalBoundedString(input.correctedCondition, 8192),
  };
  return Object.freeze(output);
}

/**
 * Validate the only workspace mutation shape accepted from UI actions or Pi.
 * It deliberately carries identifiers and view intent, never markup or code.
 */
export function normalizeWorkspaceCommand(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new TypeError("Workspace command must be an object");
  }
  if (input.type === "review.propose") return normalizeReviewProposal(input);
  if (input.type !== "workspace.open") {
    throw new TypeError("Unsupported workspace command");
  }
  if (!MODULE_SET.has(input.module)) {
    throw new TypeError(`Unsupported workspace module: ${String(input.module || "")}`);
  }
  const focus = input.focus == null ? null : String(input.focus);
  if (focus && !FOCUS_MODES.has(focus)) throw new TypeError("Unsupported evidence focus");
  const source = input.source == null ? "ui" : String(input.source);
  if (!SOURCES.has(source)) throw new TypeError("Unsupported command source");
  const assertedDocumentSource = optionalEnum(
    input.documentSource,
    DOCUMENT_SOURCES,
    "document source",
  );
  const bboxFrame = optionalEnum(input.bboxFrame, BBOX_FRAMES, "bbox frame");
  const bbox = optionalBbox(input.bbox);
  const bboxKey = optionalBboxKey(input.bboxKey);
  const page = optionalInteger(input.page, 1);
  // Older persisted browser commands carried a flat page without a source.
  // Normalize those explicitly to main rather than leaving the consumer to
  // invent a document namespace.
  const documentSource = assertedDocumentSource || (page != null ? "main" : null);
  const anchor = optionalInteger(input.anchor);
  if (documentSource && page == null) {
    throw new TypeError("documentSource requires a page");
  }
  if (documentSource && anchor != null) {
    throw new TypeError("Anchor and document location are mutually exclusive");
  }
  if (bbox && (!documentSource || page == null || !bboxFrame)) {
    throw new TypeError("Evidence bbox requires documentSource, page, and bboxFrame");
  }
  if (!bbox && bboxFrame) throw new TypeError("bboxFrame requires an evidence bbox");
  return Object.freeze({
    type: "workspace.open",
    module: input.module,
    source,
    jobId: optionalString(input.jobId, 160),
    reactionId: optionalString(input.reactionId, 320),
    anchor,
    page,
    focus,
    ...(documentSource ? { documentSource } : {}),
    ...(bboxKey ? { bboxKey } : {}),
    ...(bboxFrame ? { bboxFrame } : {}),
    ...(bbox ? { bbox } : {}),
  });
}

export function resolveFigureReactionKey(taskData, reactionId) {
  if (!reactionId || !taskData || typeof taskData !== "object") return null;
  const identifier = String(reactionId);
  const direct = identifier.match(/^(template|example):(\d+)$/);
  if (direct) {
    const index = Number(direct[2]);
    const rows = direct[1] === "template"
      ? taskData.reaction_templates : taskData.selected_examples;
    return Array.isArray(rows) && rows[index] ? `${direct[1]}:${index}` : null;
  }
  const templates = Array.isArray(taskData.reaction_templates)
    ? taskData.reaction_templates : [];
  const templateIndex = templates.findIndex((item) => item?.template_id === identifier);
  if (templateIndex >= 0) return `template:${templateIndex}`;
  const examples = Array.isArray(taskData.selected_examples)
    ? taskData.selected_examples : [];
  const exampleIndex = examples.findIndex((item) => item?.instance_id === identifier);
  return exampleIndex >= 0 ? `example:${exampleIndex}` : null;
}

export function dispatchWorkspaceCommand(input, target = window) {
  const command = normalizeWorkspaceCommand(input);
  latestCommands.set(command.jobId || "", command);
  target.dispatchEvent(new CustomEvent(EVENT_NAME, { detail: command }));
  return command;
}

export function subscribeWorkspaceCommands(listener, target = window, options = {}) {
  if (typeof listener !== "function") throw new TypeError("Listener must be a function");
  const handler = (event) => {
    try {
      listener(normalizeWorkspaceCommand(event.detail));
    } catch (error) {
      console.warn("Rejected chemistry workspace command", error);
      target.dispatchEvent(new CustomEvent("chemrxn-workspace-command-rejected", {
        detail: { message: error.message || String(error) },
      }));
    }
  };
  target.addEventListener(EVENT_NAME, handler);
  const replayKey = typeof options.jobId === "string" ? options.jobId : "";
  const replay = latestCommands.get(replayKey);
  if (options.replayLatest !== false && replay) listener(replay);
  return () => target.removeEventListener(EVENT_NAME, handler);
}

export function latestWorkspaceCommand(jobId = "") {
  return latestCommands.get(jobId) || null;
}

export function clearLatestWorkspaceCommand(jobId = "", expectedType = null) {
  const current = latestCommands.get(jobId);
  if (!current || (expectedType && current.type !== expectedType)) return false;
  return latestCommands.delete(jobId);
}
