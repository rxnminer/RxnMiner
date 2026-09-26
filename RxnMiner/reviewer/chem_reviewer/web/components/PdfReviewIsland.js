import { createApp, Fragment, h, nextTick, reactive, Teleport, watch } from "vue";
import { MoleculeCard } from "/static/components/MoleculeCard.js";
import { KetcherModal } from "/static/components/KetcherModal.js";
import { BBoxImagePanel } from "/static/components/BBoxImagePanel.js?v=20260801e";
import { store } from "/static/lib/store.js";
import { MockPdfInteractionBackend } from "/static/pdf-interaction-backend.js";
import {
  clearLatestWorkspaceCommand,
  dispatchWorkspaceCommand,
  subscribeWorkspaceCommands,
} from "/static/lib/workspace-command.js";
import { dispatchReviewOutcome } from "/static/lib/review-outcome.js?v=20260727a";
import {
  hasValidEvidenceBbox,
  imageClaimMissingBbox,
  orderedVerificationRows,
  provenanceNeedsAttention,
  verificationDimensions,
  workspaceChecks,
} from "/static/lib/review-workspace-diagnostics.js?v=20260806d";
import {
  documentBoxToLocal,
  formatGeometryLocation,
  geometryRequest,
  geometryTargetsForRow,
  localBoxToDocument,
  locationToPdfPoints,
} from "/static/lib/pdf-geometry-review.js?v=20260731a";

const VERDICTS = [
  "extraction error",
  "OCSR error",
  "R-group substitution error",
  "GT issue",
  "granularity-association issue",
  "uncertain",
  "prediction accepted",
];

const PRODUCT_VERDICTS = [
  "accept as extracted",
  "exclude from review set",
  "mark uncertain",
  "correct fields",
  "split reaction record",
  "merge with adjacent record",
];

const FILTERS = [
  ["all", "All"],
  ["unreviewed", "Unreviewed"],
  ["structural", "Structural"],
  ["conditions", "Conditions"],
  ["grounding", "Grounding"],
  ["provenance", "Provenance"],
  ["source", "Source"],
  ["identifier", "Identifier"],
  ["metrics", "Metrics/Yield"],
];

const PRODUCT_SOURCE_SCOPES = new Set(["all", "main", "si"]);
const PRODUCT_EVIDENCE_FILTERS = new Set([
  "all", "displayed", "procedure", "visual", "text-derived", "review",
]);
const PRODUCT_REPORTABILITY_FILTERS = new Set([
  "all", "prior_art", "performed_forward", "reported_forward",
  "failed_outcome", "proposed_mechanism", "unavailable",
]);
const PRODUCT_MAX_COLLAPSED_GROUPS = 120;
const PRODUCT_MAX_GROUP_KEY_LENGTH = 96;
const REPORTABILITY_META = {
  prior_art: {
    short: "Prior art",
    label: "Prior-art reported route",
    className: "prior-art",
  },
  performed_forward: {
    short: "Performed",
    label: "Performed in this paper",
    className: "performed",
  },
  reported_forward: {
    short: "Reported",
    label: "Reported forward route",
    className: "reported",
  },
  failed_outcome: {
    short: "Failed",
    label: "Failed outcome",
    className: "failed",
  },
  proposed_mechanism: {
    short: "Proposed",
    label: "Proposed mechanism",
    className: "proposed",
  },
  unavailable: {
    short: "Context unavailable",
    label: "Reportability context not attached",
    className: "unavailable",
  },
};

function reportabilityMeta(context) {
  const code = typeof context?.reportability === "string"
    && REPORTABILITY_META[context.reportability]
    ? context.reportability : "unavailable";
  const base = REPORTABILITY_META[code];
  return {
    ...base,
    code,
    attached: code !== "unavailable",
    label: typeof context?.label === "string" && context.label.trim()
      ? context.label.trim() : base.label,
  };
}

function emptyDirectoryState() {
  return {
    query: "",
    evidence: "all",
    reportability: "all",
    collapsedGroups: [],
    initializedLayers: [],
  };
}

function normalizeDirectoryState(value) {
  const normalized = emptyDirectoryState();
  if (!value || typeof value !== "object" || Array.isArray(value)) return normalized;
  if (typeof value.query === "string") normalized.query = value.query.slice(0, 320);
  if (PRODUCT_EVIDENCE_FILTERS.has(value.evidence)) normalized.evidence = value.evidence;
  if (PRODUCT_REPORTABILITY_FILTERS.has(value.reportability)) {
    normalized.reportability = value.reportability;
  }
  if (Array.isArray(value.collapsedGroups)) {
    normalized.collapsedGroups = [...new Set(value.collapsedGroups
      .filter((item) => typeof item === "string"
        && item.length <= PRODUCT_MAX_GROUP_KEY_LENGTH))]
      .slice(0, PRODUCT_MAX_COLLAPSED_GROUPS);
  }
  if (Array.isArray(value.initializedLayers)) {
    normalized.initializedLayers = [...new Set(value.initializedLayers
      .filter((item) => ["all", "attention"].includes(item)))];
  }
  return normalized;
}

function normalizeProductReviewUiState(value) {
  const sourceScope = PRODUCT_SOURCE_SCOPES.has(value?.sourceScope)
    ? value.sourceScope : "all";
  const filtersBySource = {};
  for (const source of PRODUCT_SOURCE_SCOPES) {
    filtersBySource[source] = normalizeDirectoryState(value?.filtersBySource?.[source]);
  }
  return { sourceScope, filtersBySource };
}

function sourceRightsMetadata(model) {
  const job = model?.meta?.job || {};
  if (job.source_rights_status !== "verified") return null;
  const rights = job.source_rights;
  return rights && typeof rights === "object" ? { ...rights } : null;
}

if (!window.__pdfReviewGlobalEventsInstalled) {
  window.__pdfReviewGlobalEventsInstalled = true;
  window.addEventListener("pdf-overlay-select", (event) => {
    window.__pdfReviewEventHandlers?.overlay?.(event);
  });
  window.addEventListener("keydown", (event) => {
    window.__pdfReviewEventHandlers?.keydown?.(event);
  });
  window.addEventListener("pdf-bbox-render", (event) => {
    window.__pdfReviewEventHandlers?.viewer?.(event);
  });
}

function normalizedConditions(values) {
  return (values || []).map((value) => String(value || "").toLowerCase().replace(/\s+/g, " ").trim())
    .filter(Boolean).sort();
}

function conditionTexts(steps) {
  const values = [];
  for (const step of steps || []) {
    for (const item of Array.isArray(step) ? step : [step]) {
      if (typeof item === "string") values.push(item);
      else if (item && (item.text || item.description || item["E-SMILES"])) {
        values.push(`${item.role ? `${item.role}: ` : ""}${item.text || item.description || item["E-SMILES"]}`);
      }
    }
  }
  return values;
}

function sameStrings(left, right) {
  return JSON.stringify(normalizedConditions(left)) === JSON.stringify(normalizedConditions(right));
}

function claimGroundingWarnings(evidence) {
  const warnings = Array.isArray(evidence?.claim_grounding_warnings)
    ? [...evidence.claim_grounding_warnings] : [];
  const warningKeys = new Set(
    warnings.map((warning) => `${warning.field || ""}|${warning.path || ""}`),
  );
  const addWarning = (warning) => {
    const key = `${warning.field || ""}|${warning.path || ""}`;
    if (warningKeys.has(key)) return;
    warningKeys.add(key);
    warnings.push(warning);
  };
  for (const [stepIndex, step] of (evidence?.conditions || []).entries()) {
    for (const [itemIndex, raw] of (Array.isArray(step) ? step : [step]).entries()) {
      const item = raw && typeof raw === "object" ? raw : {};
      const source = item._source && typeof item._source === "object" ? item._source : {};
      if (imageClaimMissingBbox(source)) {
        addWarning({
          field: "condition",
          path: `conditions[${stepIndex}][${itemIndex}]`,
          label: item.text || item.description || "condition claim",
          reason: "image claim has no PDF bbox",
        });
      }
    }
  }
  for (const [metricIndex, raw] of (evidence?.metrics || []).entries()) {
    const item = raw && typeof raw === "object" ? raw : {};
    const source = item._source && typeof item._source === "object" ? item._source : {};
    if (imageClaimMissingBbox(source)) {
      addWarning({
        field: "metric",
        path: `metrics[${metricIndex}]`,
        label: item.text || item.content || "metric claim",
        reason: "image claim has no PDF bbox",
      });
    }
  }
  return warnings;
}

function moleculeData(item) {
  if (!item) return null;
  return {
    identifier: item.identifier || "",
    "E-SMILES": item.smi || item["E-SMILES"] || item.smiles || "",
    description: item.description || "",
    _source: {
      origin: item.source?.origin || "",
      derivation: {
        method: item.source?.derivation_method === "untracked"
          ? "" : item.source?.derivation_method || "",
        note: item.source?.derivation_note || "",
      },
    },
  };
}

function rawDetection(item) {
  const raw = item?.raw_molparser;
  if (!raw) return null;
  return {
    ...raw,
    raw_available: true,
    markush: Boolean(raw.markush),
  };
}

function makeDimensions(row, evidence) {
  const pred = row.pred;
  const gt = row.gt;
  const predMolecules = evidence ? [...evidence.reactants, ...evidence.products] : [];
  const identifierMismatch = Boolean(row.diff && ["reactants", "products"].some((side) =>
    (row.diff[side]?.matched || []).some((pair) => pair.identifier_match === false)));
    const groundingBad = predMolecules.some((molecule) => !moleculeHasLocatedEvidence(molecule))
    || claimGroundingWarnings(evidence).length > 0;
  const provenanceBad = predMolecules.some(provenanceNeedsAttention);
  const conditionDiagnostic = row.diagnostics?.conditions;
  const hasYieldDiagnostic = Boolean(
    row.diagnostics?.yield_matches?.length || row.diagnostics?.yield_missing?.length,
  );
  return {
    // `row.status === matched` is the frozen connectivity/no-stereo result.
    // Strict stereochemistry is intentionally rendered as its own dimension;
    // never let a stereo mismatch redefine (or hide behind) connectivity.
    chemistry: !gt ? "unknown" : row.status === "matched" ? "pass" : "diff",
    conditions: !gt || !pred ? "unknown"
      : conditionDiagnostic ? conditionDiagnostic.status
        : sameStrings(gt.conditions, pred.conditions) ? "pass" : "diff",
    grounding: !pred ? "unknown" : groundingBad ? "attention" : "pass",
    provenance: !pred ? "unknown" : provenanceBad ? "attention" : "pass",
    identifier: !gt || !pred ? "unknown" : identifierMismatch ? "diff" : "pass",
    metrics: !gt || !pred || !hasYieldDiagnostic ? "unknown"
      : row.diagnostics?.yield_missing?.length ? "diff" : "pass",
    human: "pending",
  };
}

function buildRows(model) {
  const evidenceByPred = new Map(
    (model.evidence?.rows || []).map((row) => [`${row.anchor}:${row.reaction}`, row]),
  );
  const rows = [];
  if (model.compare) {
    (model.compare.reactions || []).forEach((source, index) => {
      const evidence = source.pred
        ? evidenceByPred.get(`${source.pred.anchor}:${source.pred.example}`) : null;
      const id = source.pred
        ? `pred:${source.pred.anchor}:${source.pred.example}`
        : `gt:${source.gt?.anchor}:${source.gt?.example}`;
      rows.push({
        id,
        source,
        evidence,
        label: source.pred?.label || source.gt?.label || `difference ${index + 1}`,
        dimensions: makeDimensions(source, evidence),
        diagnostics: source.diagnostics || {},
        claims: source.diagnoses || [],
      });
    });
  } else {
    for (const evidence of model.evidence?.rows || []) {
      const source = {
        status: "prediction_only_no_gt",
        pred: {
          anchor: evidence.anchor,
          example: evidence.reaction,
          anchor_label: evidence.anchor_label,
          label: evidence.label,
          reactants: evidence.reactants,
          products: evidence.products,
          conditions: conditionTexts(evidence.conditions),
          metrics: evidence.metrics,
        },
        diagnoses: [],
      };
      rows.push({
        id: evidence.id,
        source,
        evidence,
        label: evidence.label,
        dimensions: makeDimensions(source, evidence),
        diagnostics: {},
        claims: [],
        productReasons: productAttentionReasons(evidence),
      });
    }
  }
  for (const region of model.view?.layout || []) {
    if (region.matched_anchor != null) continue;
    const sourceDocument = documentSource(region);
    rows.unshift({
      id: `region:${sourceDocument}:${region.region_id}`,
      source: { status: "unextracted_region", region },
      evidence: null,
      diagnostics: {},
      label: `Additional region near ${region.label || region.region_id}`,
      dimensions: {
        chemistry: "unknown", conditions: "unknown", grounding: "diff",
        provenance: "unknown", identifier: "unknown", metrics: "unknown", human: "pending",
      },
      claims: [{
        code: "unextracted_region",
        label: "detected layout region is not linked to an extracted anchor",
        confidence: "candidate",
        evidence: [`page ${region.page || "unknown"}`],
      }],
      productReasons: ["unlinked detected region"],
    });
  }
  return rows;
}

const PRODUCT_REASON_META = {
  "Markush template": { severity: "info", label: "source Markush template" },
  "failed outcome": { severity: "review", label: "failed / no product" },
  "unresolved structure": { severity: "review", label: "structure unresolved" },
  "unlinked detected region": { severity: "review", label: "unlinked layout region" },
  "not visually located": { severity: "info", label: "not visually located" },
  "derived structure": { severity: "info", label: "derived structure" },
  "source trace unavailable": { severity: "review", label: "source trace unavailable" },
  "figure-read (no OCSR box)": { severity: "info", label: "read from figure" },
  "reportability check: overview figure": { severity: "review", label: "overview-only evidence" },
  "claim grounding missing": { severity: "review", label: "claim bbox missing" },
  "prior-art route": { severity: "info", label: "prior-art reported route" },
  "reported route": { severity: "info", label: "reported route" },
};

function moleculeHasLocatedEvidence(molecule) {
  return hasValidEvidenceBbox(molecule?.bbox)
    || hasValidEvidenceBbox(molecule?.bbox_page);
}

function productAttentionReasons(evidence) {
  const molecules = [...(evidence?.reactants || []), ...(evidence?.products || [])];
  const reasons = [];
  if (evidence?.template?.kind === "markush") reasons.push("Markush template");
  if (molecules.some((item) => !moleculeHasLocatedEvidence(item))) {
    reasons.push("not visually located");
  }
  if (molecules.some((item) => item.source?.class === "derived")) reasons.push("derived structure");
  if (molecules.some((item) => item.source?.class === "untracked")) {
    reasons.push("source trace unavailable");
  }
  if (molecules.some((item) => item.source?.class === "image_untraced")) {
    // Located in the figure (has a bbox) but not linked to a MolParser OCSR detection.
    // This is the normal state for a figure-read molecule, not a missing source --
    // surface it as an informational "read from figure", not a review-level alarm.
    reasons.push("figure-read (no OCSR box)");
  }
  if (/figure\s*1/i.test(evidence?.anchor_label || "")) {
    reasons.push("reportability check: overview figure");
  }
  if (claimGroundingWarnings(evidence).length) reasons.push("claim grounding missing");
  if (evidence?.review_context?.reportability === "failed_outcome") reasons.push("failed outcome");
  if (evidence?.exception?.kind === "unsupported_structure") reasons.push("unresolved structure");
  if (evidence?.review_context?.reportability === "prior_art") reasons.push("prior-art route");
  if (evidence?.review_context?.reportability === "reported_forward") reasons.push("reported route");
  return reasons;
}

function productReasonMeta(reason) {
  return PRODUCT_REASON_META[reason] || { severity: "review", label: reason };
}

function productReviewReasons(row) {
  return (row.productReasons || []).filter(
    (reason) => productReasonMeta(reason).severity !== "info",
  );
}

function productInfoReasons(row) {
  return (row.productReasons || []).filter(
    (reason) => productReasonMeta(reason).severity === "info",
  );
}

function productSearchText(row) {
  const molecules = row.evidence
    ? [...row.evidence.reactants, ...row.evidence.products] : [];
  return [
    row.label,
    row.source.pred?.anchor_label,
    row.evidence?.anchor_page,
    row.evidence?.review_context?.reportability,
    row.evidence?.review_context?.label,
    ...molecules.flatMap((item) => [item.identifier, item.smi, item.description]),
    ...conditionTexts(row.evidence?.conditions),
    row.evidence?.template?.kind,
    row.evidence?.template?.template_id,
    row.evidence?.exception?.reason,
    row.evidence?.exception?.outcome,
    ...(row.evidence?.metrics || []).flatMap((item) => [item.text, item.value, item.of]),
  ].filter(Boolean).join(" ").toLowerCase();
}

function dimensionChip(name, status) {
  return h("span", {
    class: `pdf-dimension dim-${status}`,
    title: `${name}: ${status}`,
  }, `${name} ${status}`);
}

function sourceClasses(row) {
  const molecules = row.evidence
    ? [...row.evidence.reactants, ...row.evidence.products] : [];
  return new Set(molecules.map((molecule) => molecule.source?.class).filter(Boolean));
}

function needsJudgment(row) {
  return Object.entries(row.dimensions)
    .some(([name, status]) => name !== "human" && ["diff", "attention"].includes(status))
    || row.claims.length > 0;
}

function rowMatchesFilter(row, filter, adjudication) {
  if (filter === "all") return true;
  if (filter === "unreviewed") return !adjudication;
  if (filter === "structural") return row.dimensions.chemistry === "diff";
  if (filter === "conditions") return row.dimensions.conditions === "diff";
  if (filter === "grounding") return ["diff", "attention"].includes(row.dimensions.grounding);
  if (filter === "provenance") return ["diff", "attention"].includes(row.dimensions.provenance);
  if (filter === "source") return sourceClasses(row).has("untracked")
    || sourceClasses(row).has("image_untraced");
  if (filter === "identifier") return row.dimensions.identifier === "diff";
  if (filter === "metrics") return row.dimensions.metrics === "diff";
  return true;
}

function viewAnchorByIndex(model, anchorIndex) {
  if (!Number.isInteger(anchorIndex)) return null;
  return (model.view?.anchors || []).find((anchor, position) =>
    (Number.isInteger(anchor?.index) ? anchor.index : position) === anchorIndex) || null;
}

function documentSource(value) {
  return value?.source_document === "si" || value?.sourceDocument === "si" ? "si" : "main";
}

function explicitDocumentSource(value) {
  const raw = value?.source_document ?? value?.sourceDocument
    ?? value?.document_source ?? value?.src;
  if (typeof raw !== "string") return null;
  const normalized = raw.trim().toLowerCase();
  if (["si", "supporting_information", "supporting information"].includes(normalized)) {
    return "si";
  }
  if (["main", "paper", "main_paper", "main paper"].includes(normalized)) {
    return "main";
  }
  return null;
}

function rowDocumentSource(model, row) {
  return documentSource(
    row?.evidence || row?.source?.region
      || viewAnchorByIndex(model, row?.source?.pred?.anchor),
  );
}

function reviewerLink(model, row) {
  const anchor = row.source.pred?.anchor;
  const viewAnchor = viewAnchorByIndex(model, anchor);
  if (!viewAnchor?.reviewer_slug || !viewAnchor?.has_reviewer || !model.reviewerBase) return null;
  try {
    const url = new URL(model.reviewerBase);
    url.hash = `/${viewAnchor.reviewer_slug}/0`;
    if (!url.pathname.endsWith("/static/")) url.pathname = `${url.pathname.replace(/\/$/, "")}/static/`;
    return url.toString();
  } catch (error) {
    return null;
  }
}

function PdfReviewApp(model) {
  const productMode = model.productMode === true;
  const jobMeta = model.meta?.job || {};
  const completenessPartial = productMode
    && (jobMeta.status === "incomplete" || jobMeta.reason === "runtime-partial");
  const initialReviewUiState = normalizeProductReviewUiState(model.initialReviewUiState);
  const rows = buildRows(model);
  const savedViewsStorageKey = `chemrxn-saved-views:${model.jobId}`;
  let storedVersions = [];
  let storedViews = [];
  if (productMode) {
    try {
      storedViews = JSON.parse(localStorage.getItem(savedViewsStorageKey) || "[]");
      if (!Array.isArray(storedViews)) storedViews = [];
    } catch (_error) {
      storedViews = [];
    }
  }
  const interactionBackend = model.interactionBackend || new MockPdfInteractionBackend({
    demoJobId: "pdf_issue28_wholepage_p50",
  });
  const verdictOptions = productMode ? PRODUCT_VERDICTS : VERDICTS;
  const storedAdjudications = Object.fromEntries(storedVersions.map((version) => [
    version.reaction_id,
    {
      difference_id: version.reaction_id,
      verdict: version.verdict,
      note: version.note,
      correction: version.correction,
      updated_at: Date.parse(version.created_at) / 1000,
    },
  ]));
  const allowedInitialLayers = new Set(productMode
    ? [
      "region", "overview", "all", "attention", "route", "table", "molecules",
      "ask", "views", "inspector", "edit", "verify", "checks",
    ]
    : ["overview", "queue", "inspector", "verify", "checks"]);
  // Product review starts from the risk queue, including the honest empty
  // state. The source canvas remains one click away; opening on a visually
  // attractive anchor must not hide the fact that review is the next task.
  const defaultProductLayer = "attention";
  const restoredInitialLayer = model.initialLayerExplicit === true
    && allowedInitialLayers.has(model.initialLayer);
  const state = reactive({
    layer: restoredInitialLayer
      ? model.initialLayer : productMode ? defaultProductLayer
        : allowedInitialLayers.has(model.initialLayer) ? model.initialLayer : "overview",
    filter: "unreviewed",
    directoryScope: initialReviewUiState.sourceScope,
    directoryStates: initialReviewUiState.filtersBySource,
    query: "",
    selectedId: null,
    selectedEntityKey: typeof model.initialEntityKey === "string"
      ? model.initialEntityKey : null,
    selectedEntityCandidates: [],
    selectedEntityOccurrenceIds: Array.isArray(model.initialEntityOccurrenceIds)
      ? [...model.initialEntityOccurrenceIds] : [],
    workspaceLocation: null,
    focus: model.initialFocus || "selected-only",
    adjudications: productMode ? storedAdjudications : Object.fromEntries(
      (model.adjudications?.items || []).map((item) => [item.difference_id, item]),
    ),
    draftVerdict: "",
    draftNote: "",
    draftSmiles: "",
    draftMoleculeCorrection: null,
    draftCondition: "",
    geometryCorrections: {},
    geometryTarget: null,
    draftGeometryLocation: null,
    draftGeometryNote: "",
    geometryEditing: false,
    proposedPatch: null,
    agentProposalMeta: null,
    saving: false,
    message: "",
    versions: storedVersions,
    headVersion: 0,
    reviewStateLoading: productMode,
    savedViews: storedViews,
    exportOpen: false,
    interactionPrompt: "",
    interactionScope: "paper",
    interactionMode: "query_index",
    interactionContext: "paper+si",
    interactionInference: "drawn-only",
    interactionOutput: "cards",
    interactionBusy: false,
    interactionError: "",
    interactionResult: null,
    interactionMessage: "",
    capabilities: {
      connected: false,
      sealedDemo: model.jobId === "pdf_issue28_wholepage_p50",
      message: "Checking interaction capabilities…",
    },
  });
  const viewerPanel = reactive({ ...(model.viewerPanel || {}) });
  let reviewUiStateTimer = null;
  function activeDirectoryState() {
    return state.directoryStates[state.directoryScope] || state.directoryStates.all;
  }

  function reviewUiStateSnapshot() {
    return {
      sourceScope: state.directoryScope,
      filtersBySource: Object.fromEntries(
        [...PRODUCT_SOURCE_SCOPES].map((source) => {
          const value = state.directoryStates[source] || emptyDirectoryState();
          return [source, {
            query: String(value.query || "").slice(0, 320),
            evidence: PRODUCT_EVIDENCE_FILTERS.has(value.evidence) ? value.evidence : "all",
            reportability: PRODUCT_REPORTABILITY_FILTERS.has(value.reportability)
              ? value.reportability : "all",
            collapsedGroups: [...new Set(value.collapsedGroups || [])]
              .filter((item) => typeof item === "string"
                && item.length <= PRODUCT_MAX_GROUP_KEY_LENGTH)
              .slice(0, PRODUCT_MAX_COLLAPSED_GROUPS),
            initializedLayers: [...new Set(value.initializedLayers || [])]
              .filter((item) => ["all", "attention"].includes(item)),
          }];
        }),
      ),
    };
  }

  function persistReviewUiState() {
    if (!productMode) return;
    if (reviewUiStateTimer != null) {
      window.clearTimeout(reviewUiStateTimer);
      reviewUiStateTimer = null;
    }
    window.dispatchEvent(new CustomEvent("pdf-review-ui-state", {
      detail: { jobId: model.jobId, state: reviewUiStateSnapshot() },
    }));
  }

  function scheduleReviewUiStatePersist() {
    if (!productMode) return;
    if (reviewUiStateTimer != null) window.clearTimeout(reviewUiStateTimer);
    reviewUiStateTimer = window.setTimeout(persistReviewUiState, 80);
  }

  function setDirectoryScope(source) {
    if (!PRODUCT_SOURCE_SCOPES.has(source)) return;
    state.directoryScope = source;
    // Source switches can immediately remount the document island (Main ↔ SI).
    // Persist synchronously so that remount never restores the previous scope;
    // query typing remains debounced through updateActiveDirectoryState().
    persistReviewUiState();
  }

  function updateActiveDirectoryState(key, value) {
    const current = activeDirectoryState();
    if (!current) return;
    current[key] = value;
    scheduleReviewUiStatePersist();
  }

  function clearProductViewFilters() {
    const all = state.directoryStates.all || emptyDirectoryState();
    all.query = "";
    all.evidence = "all";
    all.reportability = "all";
    state.directoryStates.all = all;
    state.directoryScope = "all";
    scheduleReviewUiStatePersist();
  }

  let proposalRun = 0;
  function reportPdfAgentOutcome(meta, status, extra = {}) {
    if (!meta?.proposalId || !meta?.jobId) return false;
    try {
      dispatchReviewOutcome({
        proposalId: meta.proposalId,
        jobId: meta.jobId,
        target: meta.target || "pdf",
        status,
        ...extra,
      });
      return true;
    } catch (error) {
      console.warn("PDF review outcome was not dispatched", error);
      return false;
    }
  }

  function discardCurrentAgentProposal(reason, { clearCommand = true } = {}) {
    if (!state.agentProposalMeta) return;
    reportPdfAgentOutcome(state.agentProposalMeta, "discarded", { reason });
    if (clearCommand) clearLatestWorkspaceCommand(model.jobId, "review.propose");
    state.agentProposalMeta = null;
  }
  const rowsByAnchor = new Map();
  for (const row of rows) {
    const anchor = row.source.pred?.anchor;
    if (!Number.isInteger(anchor)) continue;
    if (!rowsByAnchor.has(anchor)) rowsByAnchor.set(anchor, []);
    rowsByAnchor.get(anchor).push(row);
  }
  const moleculesByAnchor = new Map();
  for (const molecule of model.view?.molecules || []) {
    if (!Number.isInteger(molecule.anchor)) continue;
    if (!moleculesByAnchor.has(molecule.anchor)) moleculesByAnchor.set(molecule.anchor, []);
    moleculesByAnchor.get(molecule.anchor).push(molecule);
  }
  const anchorThumbnailCache = new Map();
  const judgmentRows = rows.filter(needsJudgment);
  const initialRow = Number.isInteger(model.initialAnchor)
    ? rows.find((row) => row.source.pred?.anchor === model.initialAnchor) : null;
  const restoredRow = typeof model.initialSelectedId === "string"
    ? rows.find((row) => row.id === model.initialSelectedId) : null;
  state.selectedId = restoredRow?.id
    || (model.initialSelectionCleared === true
      ? null : initialRow?.id || judgmentRows[0]?.id || rows[0]?.id || null);
  interactionBackend.capabilities(model.jobId)
    .then((value) => { state.capabilities = value; })
    .catch(() => {
      state.capabilities = { connected: false, sealedDemo: false, message: "Interaction backend not connected." };
    });

  function replaceReviewState(payload) {
    state.versions = Array.isArray(payload?.versions) ? payload.versions : [];
    state.headVersion = Number.isInteger(payload?.head_version) ? payload.head_version : 0;
    state.adjudications = Object.fromEntries(
      (Array.isArray(payload?.adjudications) ? payload.adjudications : [])
        .filter((item) => item?.difference_id)
        .map((item) => [item.difference_id, item]),
    );
    state.geometryCorrections = Object.fromEntries(
      (Array.isArray(payload?.geometry_corrections) ? payload.geometry_corrections : [])
        .filter((item) => item?.occurrence_id)
        .map((item) => [item.occurrence_id, item]),
    );
  }

  async function loadReviewState() {
    if (!productMode) return;
    state.reviewStateLoading = true;
    try {
      const response = await fetch(
        `/api/jobs/${encodeURIComponent(model.jobId)}/pdf/review/versions`,
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload?.detail || `HTTP ${response.status}`);
      replaceReviewState(payload);
    } catch (error) {
      state.message = `Review history unavailable: ${error.message || error}`;
    } finally {
      state.reviewStateLoading = false;
    }
  }

  function persistSavedViews() {
    try {
      localStorage.setItem(savedViewsStorageKey, JSON.stringify(state.savedViews));
      return true;
    } catch (error) {
      state.interactionMessage = `Saved View storage failed: ${error.message || error}`;
      return false;
    }
  }

  void loadReviewState();

  function selected() {
    return rows.find((row) => row.id === state.selectedId) || null;
  }

  function activePatchHead() {
    return state.versions.find((item) => item.version === state.headVersion)?.patch_id
      || "sealed-prediction";
  }

  function dispatchSelection(row, options = {}) {
    const pred = row?.source?.pred;
    const gt = row?.source?.gt;
    const region = row?.source?.region;
    const sourceDocument = options.sourceDocument === "si" ? "si"
      : options.sourceDocument === "main" ? "main"
        : row ? rowDocumentSource(model, row) : "main";
    window.dispatchEvent(new CustomEvent("pdf-review-select", {
      detail: {
        jobId: model.jobId,
        differenceId: row?.id,
        predAnchor: pred?.anchor,
        predReaction: pred?.example,
        gtAnchor: gt?.anchor,
        gtReaction: gt?.example,
        sourceDocument,
        preserveView: options.preserveView === true,
        preserveMoleculeEntity: options.preserveMoleculeEntity === true,
        moleculeEntityKey: options.moleculeEntityKey || undefined,
        moleculeEntityOccurrenceIds: Array.isArray(options.moleculeEntityOccurrenceIds)
          ? options.moleculeEntityOccurrenceIds : undefined,
        revealSource: options.revealSource === true,
        page: pred
          ? viewAnchorByIndex(model, pred.anchor)?.page
          : gt?.page || region?.page,
      },
    }));
  }

  function clearContextSelection(sourceDocument = "main") {
    state.selectedId = null;
    state.selectedEntityKey = null;
    state.selectedEntityCandidates = [];
    state.selectedEntityOccurrenceIds = [];
    state.workspaceLocation = null;
    dispatchSelection(null, {
      sourceDocument,
      preserveView: true,
    });
  }

  function citationReactionRow(citation) {
    if (typeof citation?.reactionId !== "string") return null;
    const row = rows.find((item) => item.id === citation.reactionId) || null;
    if (!row) return null;
    // Unmatched layout regions are useful review rows, but they are not
    // prediction reactions and have no stable anchor/example identity.
    // Treating `region:*` as an associated reaction would let the Vue island
    // retain an inspector that the host PDF selection correctly rejects.
    const pred = row.source?.pred;
    if (!pred || !Number.isInteger(pred.anchor) || !Number.isInteger(pred.example)) {
      return null;
    }
    const source = citation.source === "si" ? "si" : "main";
    if (rowDocumentSource(model, row) !== source) return null;
    const expectedAnchor = pred.anchor;
    const assertedAnchor = Number(citation.anchor);
    if (citation.anchor != null
        && (!Number.isInteger(assertedAnchor) || assertedAnchor !== expectedAnchor)) {
      return null;
    }
    const expectedPage = rowPage(row);
    const assertedPage = Number(citation.page);
    if (citation.page != null
        && (!Number.isInteger(assertedPage) || assertedPage !== expectedPage)) {
      return null;
    }
    return row;
  }

  function selectRegionRow(row) {
    state.selectedId = row.id;
    state.workspaceLocation = null;
    setFocus("selected-only");
    dispatchSelection(row);
  }

  function selectRow(row, options = {}) {
    if (!options.preserveIncomingProposal) {
      proposalRun += 1;
      state.saving = false;
    }
    if (state.agentProposalMeta && !options.preserveIncomingProposal) {
      discardCurrentAgentProposal("human_navigation");
    }
    state.selectedId = row.id;
    state.workspaceLocation = null;
    const preserveMoleculeEntity = options.preserveMoleculeEntity === true
      && Boolean(state.selectedEntityKey);
    state.layer = preserveMoleculeEntity ? "molecules" : "inspector";
    // A deliberate reaction click is a review action, so tighten the PDF to
    // that reaction's evidence. Anchor selection still uses current-anchor and
    // the inspector control can explicitly widen back to the anchor/all views.
    if (row.source.pred) setFocus("selected-only");
    const saved = state.adjudications[row.id] || {};
    state.draftVerdict = saved.verdict || "";
    state.draftNote = saved.note || "";
    state.draftSmiles = saved.correction?.smiles || "";
    state.draftMoleculeCorrection = saved.correction?.molecule || null;
    state.draftCondition = saved.correction?.condition_text || "";
    state.geometryTarget = null;
    state.draftGeometryLocation = null;
    state.draftGeometryNote = "";
    state.geometryEditing = false;
    state.proposedPatch = null;
    state.agentProposalMeta = null;
    dispatchSelection(row, {
      ...(preserveMoleculeEntity ? {
        preserveMoleculeEntity: true,
        moleculeEntityKey: state.selectedEntityKey,
        moleculeEntityOccurrenceIds: state.selectedEntityOccurrenceIds,
      } : {}),
      revealSource: options.revealSource === true,
    });
  }

  const moduleLayers = productMode ? {
    region: "region",
    overview: "overview",
    reactions: "all",
    attention: "attention",
    ask: "ask",
    "saved-views": "views",
    evidence: "inspector",
    edit: "edit",
    verify: "verify",
    checks: "checks",
    route: "route",
    table: "table",
    molecules: "molecules",
  } : {
    overview: "overview",
    reactions: "queue",
    attention: "queue",
    evidence: "inspector",
    edit: "inspector",
    verify: "verify",
    checks: "checks",
  };

  async function stagePdfAgentProposal(command) {
    if (!productMode) {
      reportPdfAgentOutcome(command, "rejected", {
        reason: "benchmark_read_only",
      });
      clearLatestWorkspaceCommand(model.jobId, "review.propose");
      state.message = "Agent correction proposals are disabled in reference-scored benchmark review.";
      return;
    }
    if (command.target !== "pdf") {
      reportPdfAgentOutcome(command, "rejected", {
        reason: "workspace_target_mismatch",
      });
      clearLatestWorkspaceCommand(model.jobId, "review.propose");
      state.message = "This correction proposal targets a different workspace.";
      return;
    }
    const row = rows.find((item) => item.id === command.reactionId);
    if (!row) {
      reportPdfAgentOutcome(command, "rejected", {
        reason: "reaction_not_found",
      });
      clearLatestWorkspaceCommand(model.jobId, "review.propose");
      state.message = `Reaction ${command.reactionId} is not present in this document.`;
      return;
    }
    discardCurrentAgentProposal("superseded", { clearCommand: false });
    selectRow(row, { preserveIncomingProposal: true });
    state.layer = "edit";
    if (command.verdict != null) state.draftVerdict = command.verdict;
    if (command.note != null) state.draftNote = command.note;
    if (command.correctedSmiles != null) {
      state.draftSmiles = command.correctedSmiles;
      state.draftMoleculeCorrection = null;
    }
    if (command.correctedCondition != null) state.draftCondition = command.correctedCondition;
    state.agentProposalMeta = {
      proposalId: command.proposalId,
      jobId: command.jobId,
      target: command.target,
      reactionId: command.reactionId,
      rationale: command.rationale,
      evidence: command.evidence,
    };
    const run = ++proposalRun;
    const ready = await previewProductCorrection(row, run);
    if (run !== proposalRun) return;
    if (ready) {
      reportPdfAgentOutcome(state.agentProposalMeta, "staged");
      return;
    }
    const saved = state.adjudications[row.id] || {};
    state.draftVerdict = saved.verdict || "";
    state.draftNote = saved.note || "";
    state.draftSmiles = saved.correction?.smiles || "";
    state.draftMoleculeCorrection = saved.correction?.molecule || null;
    state.draftCondition = saved.correction?.condition_text || "";
    reportPdfAgentOutcome(state.agentProposalMeta, "rejected", {
      reason: "proposal_not_stageable",
    });
    clearLatestWorkspaceCommand(model.jobId, "review.propose");
    state.agentProposalMeta = null;
  }

  function validateWorkspaceLocation(command) {
    if (!command.documentSource) return { location: null, error: "" };
    const source = command.documentSource;
    const document = (Array.isArray(model.documents) ? model.documents : [])
      .find((item) => item?.source === source && item.available !== false);
    if (!document) {
      return {
        location: null,
        error: source === "si"
          ? "Supporting information is not attached to this workspace."
          : "The main paper is not available in this workspace.",
      };
    }
    const pageCount = Number(document.pageCount)
      || (Array.isArray(document.pages) ? document.pages.length : 0);
    if (!Number.isInteger(command.page) || command.page < 1
        || (pageCount > 0 && command.page > pageCount)) {
      return {
        location: null,
        error: `${source === "si" ? "Supporting information" : "Main paper"}`
          + ` page ${command.page} does not exist in this workspace.`,
      };
    }
    const targetFrame = document.coordinateFrame || "pdf_points";
    if (command.bbox
        && !["pdf_points", "page_render_px"].includes(targetFrame)) {
      return {
        location: null,
        error: `Evidence bounds cannot be mapped into document frame ${targetFrame}.`,
      };
    }
    if (command.bbox && command.bboxFrame !== targetFrame) {
      return {
        location: null,
        error: `Evidence frame ${command.bboxFrame} does not match`
          + ` ${source === "si" ? "supporting information" : "main paper"} frame ${targetFrame}.`,
      };
    }
    return {
      error: "",
      location: {
        documentSource: source,
        page: command.page,
        bbox: command.bbox || null,
        bboxFrame: command.bboxFrame || null,
        source: command.source,
        reactionId: command.reactionId || null,
      },
    };
  }

  function applyWorkspaceCommand(command) {
    if (command.jobId && command.jobId !== model.jobId) return;
    if (command.type === "review.propose") {
      void stagePdfAgentProposal(command);
      return;
    }
    const locationResult = validateWorkspaceLocation(command);
    if (locationResult.error) {
      state.message = locationResult.error;
      return;
    }
    if (command.focus) setFocus(command.focus);
    const hasDocumentLocation = Boolean(locationResult.location);
    const requestedRow = command.reactionId
      ? rows.find((item) => item.id === command.reactionId) || null
      : null;
    const locationCitation = hasDocumentLocation ? {
      source: locationResult.location.documentSource,
      page: command.page,
      anchor: command.anchor,
      reactionId: command.reactionId,
    } : null;
    const row = hasDocumentLocation && requestedRow
      ? citationReactionRow(locationCitation)
      : requestedRow;
    const missingReaction = Boolean(command.reactionId && !requestedRow);
    const mismatchedReaction = Boolean(
      hasDocumentLocation && requestedRow && !row,
    );
    if ((hasDocumentLocation && !row) || missingReaction) {
      clearContextSelection(locationResult.location?.documentSource || "main");
    }
    if (Number.isInteger(command.anchor)) {
      const anchor = viewAnchorByIndex(model, command.anchor);
      if (!anchor) {
        state.message = `Anchor ${command.anchor} is not present in this document.`;
        return;
      }
      selectAnchor(anchor, command.anchor);
    }
    if (row) selectRow(row);
    const layer = moduleLayers[command.module];
    const layerAvailable = Boolean(layer);
    if (layer) {
      state.layer = layer;
      if (["inspector", "edit"].includes(layer) && selected()) dispatchSelection(selected());
      void nextTick(() => {
        const nav = document.querySelector("#docright .pdf-layer-nav");
        const active = nav?.querySelector("button[aria-current=page]");
        if (!nav || !active) return;
        const left = Math.max(
          0,
          active.offsetLeft - ((nav.clientWidth - active.offsetWidth) / 2),
        );
        nav.scrollTo({
          left,
          behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
            ? "auto" : "smooth",
        });
      });
    } else {
      state.message = `${command.module} workspace is not available for this document yet.`;
    }
    if (hasDocumentLocation) {
      state.workspaceLocation = locationResult.location;
      window.dispatchEvent(new CustomEvent("pdf-review-citation-select", {
        detail: {
          jobId: model.jobId,
          source: locationResult.location.documentSource,
          page: command.page,
          anchor: command.anchor,
          bbox: command.bbox,
          bboxFrame: command.bboxFrame,
          reactionAssociated: Boolean(row),
          reactionId: row?.id,
        },
      }));
      const location = `${command.documentSource === "si" ? "SI" : "main paper"}`
        + `${Number.isInteger(command.page) ? ` page ${command.page}` : ""}`;
      const association = missingReaction
        ? ` Reaction ${command.reactionId} is not present, so no reaction was associated.`
        : mismatchedReaction
          ? ` Reaction ${command.reactionId} does not match that source location, so no reaction was associated.`
          : "";
      state.message = layerAvailable
        ? `Pi Agent opened ${location}${command.bbox ? " and requested the supplied evidence bounds" : ""}.${association}`
        : `Pi Agent opened ${location}, but ${command.module} is unavailable in this workspace.${association}`;
    } else if (missingReaction) {
      state.message = `Reaction ${command.reactionId} is not present in this document workspace.`;
    }
  }

  const unsubscribeWorkspaceCommands = subscribeWorkspaceCommands(
    applyWorkspaceCommand,
    window,
    { jobId: model.jobId },
  );
  const rejectedWorkspaceCommand = (event) => {
    state.message = `Workspace command rejected: ${event.detail?.message || "invalid command"}`;
  };
  window.addEventListener("chemrxn-workspace-command-rejected", rejectedWorkspaceCommand);
  let initialLayoutNotification = true;
  const stopLayoutWatch = watch(() => state.layer, (layer) => {
    const layout = layer === "region" ? "pdf"
      : layer === "overview" ? "overview"
        : ["all", "attention", "queue", "route", "table", "molecules", "checks"].includes(layer) ? "review"
        : ["inspector", "edit", "verify"].includes(layer) ? "evidence" : "adaptive";
    window.dispatchEvent(new CustomEvent("chemrxn-workspace-layout", {
      detail: {
        jobId: model.jobId,
        layer,
        layout,
        initial: initialLayoutNotification,
      },
    }));
    initialLayoutNotification = false;
  }, { immediate: true });

  function openWorkspaceModule(module, source = "ui") {
    dispatchWorkspaceCommand({
      type: "workspace.open",
      module,
      source,
      jobId: model.jobId,
    });
  }

  function setFocus(value) {
    state.focus = value;
    window.dispatchEvent(new CustomEvent("pdf-review-focus", {
      detail: { jobId: model.jobId, mode: value },
    }));
  }

  function currentDraftCorrection() {
    const correction = {};
    if (state.draftMoleculeCorrection) {
      correction.molecule = state.draftMoleculeCorrection;
    } else if (state.draftSmiles) {
      correction.smiles = state.draftSmiles;
    }
    if (state.draftCondition) correction.condition_text = state.draftCondition;
    return Object.keys(correction).length ? correction : null;
  }

  function startGeometryEdit(target) {
    const row = selected();
    if (!row || !target) return;
    proposalRun += 1;
    discardCurrentAgentProposal("human_location_edit");
    const active = state.geometryCorrections[target.target_id] || null;
    const source = target.location?.source_document === "si" ? "si" : "main";
    const sourceDocumentMeta = (model.documents || []).find(
      (document) => document?.source === source,
    ) || {};
    const rawLocation = locationToPdfPoints(target.location, sourceDocumentMeta);
    state.geometryTarget = target;
    state.draftGeometryLocation = active?.geometry
      ? {
          source_document: active.geometry.source_document,
          page: active.geometry.page,
          bbox_frame: active.geometry.bbox_frame,
          bbox: [...active.geometry.bbox],
        }
      : rawLocation
        ? rawLocation
        : null;
    state.draftGeometryNote = active?.note || "";
    state.geometryEditing = true;
    state.proposedPatch = null;
    const destination = active?.geometry || target.location || {};
    const sourceDocument = ["main", "si"].includes(destination.source_document)
      ? destination.source_document
      : rowDocumentSource(model, row);
    const page = Number(destination.page) || rowPage(row);
    window.dispatchEvent(new CustomEvent("pdf-review-geometry-navigate", {
      detail: { jobId: model.jobId, sourceDocument, page },
    }));
    state.message = "Drag on the PDF to draw a replacement box, or enter exact coordinates.";
  }

  function inspectGeometryVersion(version) {
    const row = rows.find((item) => item.id === version.reaction_id);
    const target = geometryTargetsForRow(row).find(
      (item) => item.target_id === version.occurrence_id,
    );
    if (!row || !target || !version.geometry) return;
    selectRow(row);
    state.layer = "edit";
    state.geometryTarget = target;
    state.draftGeometryLocation = {
      ...version.geometry,
      bbox: [...version.geometry.bbox],
    };
    state.draftGeometryNote = version.note || "";
    state.geometryEditing = false;
    window.dispatchEvent(new CustomEvent("pdf-review-geometry-navigate", {
      detail: {
        jobId: model.jobId,
        sourceDocument: version.geometry.source_document,
        page: version.geometry.page,
      },
    }));
    state.message = `Showing immutable reviewed-v${version.version} location.`;
  }

  function updateGeometryFromLocal(box) {
    if (!state.geometryTarget) return;
    const documentBox = localBoxToDocument(box, viewerPanel);
    if (!documentBox) return;
    state.draftGeometryLocation = {
      source_document: viewerPanel.sourceDocument === "si" ? "si" : "main",
      page: Number(viewerPanel.page),
      bbox_frame: "pdf_points",
      bbox: documentBox,
    };
    state.proposedPatch = null;
  }

  function updateGeometryCoordinate(index, rawValue) {
    if (!state.geometryTarget) return;
    const current = state.draftGeometryLocation?.bbox
      ? [...state.draftGeometryLocation.bbox] : [0, 0, 0, 0];
    const value = Number(rawValue);
    if (!Number.isFinite(value)) return;
    current[index] = value;
    state.draftGeometryLocation = {
      source_document: state.draftGeometryLocation?.source_document
        || (viewerPanel.sourceDocument === "si" ? "si" : "main"),
      page: Number(state.draftGeometryLocation?.page || viewerPanel.page),
      bbox_frame: "pdf_points",
      bbox: current,
    };
    state.proposedPatch = null;
  }

  async function previewGeometryCorrection(run = ++proposalRun) {
    const row = selected();
    const request = geometryRequest(
      row,
      state.geometryTarget,
      state.draftGeometryLocation,
      state.headVersion,
      state.draftGeometryNote,
    );
    if (!request) {
      state.message = "Draw a positive-area PDF box before reviewing this location.";
      return false;
    }
    state.proposedPatch = null;
    state.saving = true;
    try {
      const response = await fetch(
        `/api/jobs/${encodeURIComponent(model.jobId)}/pdf/review/preview`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(request),
        },
      );
      const proposal = await response.json().catch(() => ({}));
      if (response.status === 409) {
        await loadReviewState();
        state.message = `Location must be reviewed again: ${
          proposal?.detail || "review history or source files changed"
        }. Your staged coordinates are still shown.`;
        return false;
      }
      if (!response.ok) throw new Error(proposal?.detail || `HTTP ${response.status}`);
      if (run !== proposalRun) return false;
      state.proposedPatch = proposal;
      state.geometryEditing = false;
      state.message = "Location diff is ready. Check Before/After on the PDF, then Apply.";
      return true;
    } catch (error) {
      if (run !== proposalRun) return false;
      state.proposedPatch = null;
      state.message = `Location proposal failed: ${error.message || error}`;
      return false;
    } finally {
      if (run === proposalRun) state.saving = false;
    }
  }

  function geometryPanelProps() {
    const drawing = productMode && Boolean(state.geometryTarget);
    const active = drawing
      ? state.geometryCorrections[state.geometryTarget.target_id] || null
      : null;
    const boxes = (viewerPanel.boxes || []).map((box) => ({
      ...box,
      dimmed: state.geometryEditing ? true : box.dimmed,
    }));
    for (const correction of Object.values(state.geometryCorrections)) {
      if (
        !correction?.geometry
        || (drawing && correction.occurrence_id === state.geometryTarget.target_id)
      ) continue;
      const reviewedBox = documentBoxToLocal(correction.geometry, viewerPanel);
      if (!reviewedBox) continue;
      boxes.push({
        key: `reviewed-geometry:${correction.occurrence_id}`,
        kind: "reviewed-geometry",
        role: "condition",
        bbox: reviewedBox,
        label: "Reviewed",
        fullLabel: `Reviewed location · ${
          correction.target_label || correction.occurrence_id
        }`,
        stroke: "#16874f",
        fill: "rgba(22,135,79,.10)",
        strokeWidth: 3,
        labelColor: "#16874f",
        selectable: false,
      });
    }
    if (!drawing) {
      return boxes.length === (viewerPanel.boxes || []).length
        ? viewerPanel
        : { ...viewerPanel, boxes };
    }
    const before = active?.geometry || state.geometryTarget.location;
    const beforeBox = documentBoxToLocal(before, viewerPanel);
    const draftBox = documentBoxToLocal(state.draftGeometryLocation, viewerPanel);
    if (beforeBox) {
      boxes.push({
        key: `review-before:${state.geometryTarget.target_id}`,
        kind: "review-before",
        role: "condition",
        bbox: beforeBox,
        label: "Before",
        fullLabel: `Before · ${state.geometryTarget.label}`,
        stroke: "#b56a16",
        fill: "rgba(181,106,22,.07)",
        dash: "7 4",
        strokeWidth: 3,
        active: true,
        selectable: false,
      });
    }
    return {
      ...viewerPanel,
      boxes,
      drawMode: state.geometryEditing,
      draftBox,
      draftLabel: state.proposedPatch ? "Proposed" : "Draft",
      drawLabel: `Draw replacement location for ${state.geometryTarget.label}`,
      onDraftChange: updateGeometryFromLocal,
      onDraftCommit: () => {
        state.message = "Replacement box staged. Review its coordinates before Apply.";
      },
      onDraftCancel: (previousBox) => {
        if (previousBox) updateGeometryFromLocal(previousBox);
        else state.draftGeometryLocation = null;
        state.message = "Drawing cancelled; the previous staged location is unchanged.";
      },
    };
  }

  async function previewProductCorrection(row, run = ++proposalRun) {
    if (!row || !verdictOptions.includes(state.draftVerdict)) {
      state.message = "Choose a verdict before reviewing this correction.";
      return false;
    }
    const nextAdjudication = {
      difference_id: row.id,
      verdict: state.draftVerdict,
      note: state.draftNote,
      correction: currentDraftCorrection(),
      updated_at: Date.now() / 1000,
    };
    const before = state.adjudications[row.id] || null;
    const comparable = (item) => JSON.stringify({
      verdict: item?.verdict || "",
      note: item?.note || "",
      smiles: item?.correction?.smiles || "",
      molecule: item?.correction?.molecule || null,
      condition: item?.correction?.condition_text || "",
    });
    if (before && comparable(before) === comparable(nextAdjudication)) {
      state.proposedPatch = null;
      state.message = "No correction fields changed.";
      return false;
    }
    const proposalRequest = {
      base_version: state.headVersion,
      after: nextAdjudication,
      proposed_at: new Date().toISOString(),
    };
    // Do not render an actionable diff from the unvalidated browser draft.
    // Only the server-canonical proposal carries the proposal_id accepted by
    // Apply; exposing the request here creates a preview/apply race.
    state.proposedPatch = null;
    state.saving = true;
    try {
      const response = await fetch(
        `/api/jobs/${encodeURIComponent(model.jobId)}/pdf/review/preview`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(proposalRequest),
        },
      );
      const proposal = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(proposal?.detail || `HTTP ${response.status}`);
      if (run !== proposalRun) return;
      state.proposedPatch = proposal;
      state.message = "Proposed changes are ready. Review the diff before Apply.";
      return true;
    } catch (error) {
      if (run !== proposalRun) return;
      state.proposedPatch = null;
      state.message = `Proposal failed: ${error.message || error}`;
      return false;
    } finally {
      if (run === proposalRun) state.saving = false;
    }
  }

  async function saveAdjudication() {
    const row = selected();
    if (!row || !verdictOptions.includes(state.draftVerdict)) {
      state.message = "Choose a verdict before saving.";
      return;
    }
    const nextAdjudication = {
      difference_id: row.id,
      verdict: state.draftVerdict,
      note: state.draftNote,
      correction: currentDraftCorrection(),
      updated_at: Date.now() / 1000,
    };
    if (productMode) {
      discardCurrentAgentProposal("human_review_replaced");
      proposalRun += 1;
      await previewProductCorrection(row);
      return;
    }
    state.adjudications[row.id] = nextAdjudication;
    state.saving = true;
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(model.jobId)}/pdf/adjudications`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          items: Object.values(state.adjudications)
            .filter((item) => VERDICTS.includes(item.verdict)),
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      state.message = "Verdict saved.";
    } catch (error) {
      state.message = `Save failed: ${error.message || error}`;
    } finally {
      state.saving = false;
    }
  }

  async function applyProposedPatch() {
    const proposal = state.proposedPatch;
    if (!proposal) return;
    if (proposal.base_version !== state.headVersion) {
      state.message = "Version history changed. Review the correction again before Apply.";
      proposalRun += 1;
      reportPdfAgentOutcome(state.agentProposalMeta, "expired", {
        reason: "review_head_changed",
      });
      clearLatestWorkspaceCommand(model.jobId, "review.propose");
      state.proposedPatch = null;
      state.agentProposalMeta = null;
      return;
    }
    state.saving = true;
    try {
      const response = await fetch(
        `/api/jobs/${encodeURIComponent(model.jobId)}/pdf/review/apply`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            proposal,
            ...(state.agentProposalMeta?.proposalId
              ? { agent_proposal_id: state.agentProposalMeta.proposalId }
              : {}),
          }),
        },
      );
      const payload = await response.json().catch(() => ({}));
      if (response.status === 409) {
        proposalRun += 1;
        reportPdfAgentOutcome(state.agentProposalMeta, "expired", {
          reason: "review_head_changed",
        });
        clearLatestWorkspaceCommand(model.jobId, "review.propose");
        state.proposedPatch = null;
        state.agentProposalMeta = null;
        await loadReviewState();
        state.message = `Proposal expired: ${
          payload?.detail || "review history or immutable source files changed"
        }. Review it again from the latest state.`;
        return;
      }
      if (!response.ok) throw new Error(payload?.detail || `HTTP ${response.status}`);
      replaceReviewState(payload);
      if (proposal.operation === "locate_pdf_geometry") {
        const active = state.geometryCorrections[proposal.occurrence_id] || null;
        state.draftGeometryLocation = active?.geometry
          ? { ...active.geometry, bbox: [...active.geometry.bbox] } : null;
        state.geometryEditing = false;
      }
      proposalRun += 1;
      const outcomeDispatched = state.agentProposalMeta
        ? reportPdfAgentOutcome(state.agentProposalMeta, "applied", {
            reviewedVersion: payload.head_version,
            ...(payload.head_patch_id ? { patchId: payload.head_patch_id } : {}),
          })
        : true;
      clearLatestWorkspaceCommand(model.jobId, "review.propose");
      state.proposedPatch = null;
      state.agentProposalMeta = null;
      state.message = outcomeDispatched
        ? `Applied reviewed-v${payload.head_version}${
            proposal.operation === "locate_pdf_geometry" ? " location" : ""
          }. Raw prediction unchanged.`
        : `Applied reviewed-v${payload.head_version}, but its Agent status could not be linked. Reload review history before continuing.`;
    } catch (error) {
      state.message = `Apply failed: ${error.message || error}`;
    } finally {
      state.saving = false;
    }
  }

  function discardProposedPatch() {
    const operation = state.proposedPatch?.operation;
    proposalRun += 1;
    state.saving = false;
    discardCurrentAgentProposal("human_discarded");
    state.proposedPatch = null;
    if (operation === "locate_pdf_geometry") state.geometryEditing = true;
    state.message = "Proposed changes discarded.";
  }

  async function undoLatestPatch() {
    if (!state.headVersion || state.saving) return;
    const removed = state.headVersion;
    state.saving = true;
    try {
      const response = await fetch(
        `/api/jobs/${encodeURIComponent(model.jobId)}/pdf/review/undo`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ head_version: state.headVersion }),
        },
      );
      const payload = await response.json().catch(() => ({}));
      if (response.status === 409) {
        await loadReviewState();
        state.message = `Undo was not applied: ${
          payload?.detail || "review history or immutable source files changed"
        }.`;
        return;
      }
      if (!response.ok) throw new Error(payload?.detail || `HTTP ${response.status}`);
      replaceReviewState(payload);
      const restored = state.adjudications[state.selectedId] || null;
      state.draftVerdict = restored?.verdict || "";
      state.draftNote = restored?.note || "";
      state.draftSmiles = restored?.correction?.smiles || "";
      state.draftMoleculeCorrection = restored?.correction?.molecule || null;
      state.draftCondition = restored?.correction?.condition_text || "";
      const restoredGeometry = state.geometryTarget
        ? state.geometryCorrections[state.geometryTarget.target_id] : null;
      state.draftGeometryLocation = restoredGeometry?.geometry
        ? { ...restoredGeometry.geometry, bbox: [...restoredGeometry.geometry.bbox] }
        : null;
      state.draftGeometryNote = restoredGeometry?.note || "";
      state.geometryEditing = false;
      state.proposedPatch = null;
      state.message = `Undid reviewed-v${removed}. Immutable history retained.`;
    } catch (error) {
      state.message = `Undo failed: ${error.message || error}`;
    } finally {
      state.saving = false;
    }
  }

  function downloadText(name, text, type = "application/json") {
    const blob = new Blob([text], { type });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.click();
    URL.revokeObjectURL(url);
  }

  function exportProduct(format) {
    const sourceRights = sourceRightsMetadata(model);
    const payload = {
      schema: "chemrxn.review-patches.v1",
      job_id: model.jobId,
      source: "sealed prediction; not modified",
      ...(sourceRights ? { source_rights: sourceRights } : {}),
      patches: state.versions,
    };
    if (format === "patches") {
      downloadText(`${model.jobId}_review_patches.json`, `${JSON.stringify(payload, null, 2)}\n`);
    } else if (format === "report") {
      const occurrenceRows = rows.filter((row) => row.source.pred);
      const contextCounts = Object.fromEntries(
        contextSummaryForRows(occurrenceRows).map((context) => [context.code, {
          count: context.count,
          label: context.label,
          attached: context.attached,
        }]),
      );
      downloadText(`${model.jobId}_review_report.json`, `${JSON.stringify({
        ...payload,
        schema: "chemrxn.review-report.v1",
        reportability_counts: contextCounts,
        occurrences: occurrenceRows.map((row) => {
          const context = reportabilityMeta(row.evidence?.review_context);
          const evidence = rowEvidenceSummary(row);
          return {
            reaction_id: row.id,
            source_document: rowDocumentSource(model, row),
            page: rowPage(row),
            anchor: row.source.pred?.anchor ?? null,
            anchor_label: row.source.pred?.anchor_label || null,
            reaction: row.source.pred?.example ?? null,
            reportability: {
              code: context.code,
              label: context.label,
              attached: context.attached,
            },
            evidence: {
              located_molecules: evidence.located,
              raw_ocsr_molecules: evidence.raw,
              unresolved_claims: evidence.claimWarnings,
              status: evidence.status,
            },
            review_reasons: productReviewReasons(row),
          };
        }),
        attention_items: rows.flatMap((row) => {
          const reasons = productReviewReasons(row);
          return reasons.length ? [{
            reaction_id: row.id,
            source_document: rowDocumentSource(model, row),
            page: rowPage(row),
            anchor: row.source.pred?.anchor ?? null,
            reasons,
          }] : [];
        }),
        info_items: rows.flatMap((row) => {
          const reasons = productInfoReasons(row);
          return reasons.length ? [{
            reaction_id: row.id,
            source_document: rowDocumentSource(model, row),
            page: rowPage(row),
            anchor: row.source.pred?.anchor ?? null,
            reasons,
          }] : [];
        }),
      }, null, 2)}\n`);
    }
    state.exportOpen = false;
  }

  async function exportExtraction(format) {
    try {
      const response = await fetch(
        `/api/jobs/${encodeURIComponent(model.jobId)}/export?fmt=${encodeURIComponent(format)}`,
      );
      if (!response.ok) {
        const message = await response.text().catch(() => "");
        throw new Error(message || `HTTP ${response.status}`);
      }
      const disposition = response.headers.get("content-disposition") || "";
      const fallback = {
        v2: `${model.jobId}_prediction.json`,
        csv: `${model.jobId}_reactions.csv`,
        report: `${model.jobId}_report.json`,
        rxn: `${model.jobId}_rxn.zip`,
        rights: `${model.jobId}_source_rights.json`,
      }[format];
      const filename = disposition.match(/filename="([^"]+)"/i)?.[1] || fallback;
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      window.alert(`Export failed: ${error?.message || error}`);
    } finally {
      state.exportOpen = false;
    }
  }

  async function exportSummary() {
    try {
      const exportPath = productMode
        ? `/api/jobs/${encodeURIComponent(model.jobId)}/review/export`
        : `/api/jobs/${encodeURIComponent(model.jobId)}/pdf/review/export`;
      const response = await fetch(exportPath);
      if (!response.ok) {
        let message = "";
        try {
          const body = await response.json();
          message = body?.detail || body?.error || "";
        } catch {
          message = await response.text().catch(() => "");
        }
        throw new Error(message || `HTTP ${response.status}`);
      }
      const disposition = response.headers.get("content-disposition") || "";
      const filename = disposition.match(/filename="([^"]+)"/i)?.[1]
        || `${model.jobId}_${productMode ? "reviewed_changes" : "review_summary"}.json`;
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      window.alert(`Review export failed: ${error?.message || error}`);
    } finally {
      state.exportOpen = false;
    }
  }

  function interactionSelection() {
    const row = selected();
    if (!row) return null;
    const molecules = row.evidence
      ? [...(row.evidence.reactants || []), ...(row.evidence.products || [])] : [];
    const located = molecules.find((molecule) => Array.isArray(molecule.bbox));
    return {
      id: row.id,
      label: `${row.source.pred?.anchor_label || "Reaction"} · ${row.label}`,
      anchor: row.source.pred?.anchor,
      anchorId: row.source.pred?.anchor == null ? null : String(row.source.pred.anchor),
      reaction: row.source.pred?.example,
      reactionIds: [row.id],
      moleculeOccurrenceIds: [],
      moleculeIdentifiers: molecules.map((molecule) => molecule.identifier).filter(Boolean),
      claimIds: [],
      sourceDocument: rowDocumentSource(model, row),
      page: row.evidence?.anchor_page
        || viewAnchorByIndex(model, row.source.pred?.anchor)?.page
        || null,
      bbox: located?.bbox || null,
    };
  }

  async function runInteraction(
    prompt = state.interactionPrompt,
    output = state.interactionOutput,
    mode = state.interactionMode,
    scope = state.interactionScope,
  ) {
    state.interactionPrompt = prompt;
    state.interactionOutput = output;
    state.interactionMode = mode;
    state.interactionScope = scope;
    state.interactionBusy = true;
    state.interactionError = "";
    state.interactionMessage = "";
    try {
      state.interactionResult = await interactionBackend.run({
        demo: state.capabilities.sealedDemo === true,
        jobId: model.jobId,
        prompt,
        scope,
        mode,
        context: state.interactionContext,
        inference: state.interactionInference,
        output,
        selection: interactionSelection(),
      });
      state.interactionMessage = "Loaded a deterministic sealed-demo response. No extraction or model ran.";
    } catch (error) {
      state.interactionResult = null;
      state.interactionError = error.code === "CAPABILITY_UNAVAILABLE"
        ? `${error.message} Try one of the sealed example prompts.`
        : "Interaction backend not connected. Only explicit sealed-demo examples are available.";
    } finally {
      state.interactionBusy = false;
    }
  }

  function openCitation(citation) {
    const availableDocuments = Array.isArray(model.documents) ? model.documents : [];
    if (citation.source === "si"
        && !availableDocuments.some((document) => document?.source === "si" && document.available !== false)) {
      state.interactionMessage = "Supporting information is not attached to this workspace.";
      return;
    }
    const row = citationReactionRow(citation);
    const source = citation.source === "si" ? "si" : "main";
    if (row) {
      state.selectedId = row.id;
      state.selectedEntityKey = null;
      state.selectedEntityCandidates = [];
      state.selectedEntityOccurrenceIds = [];
      state.workspaceLocation = null;
      dispatchSelection(row, { preserveView: true });
    } else {
      clearContextSelection(source);
    }
    window.dispatchEvent(new CustomEvent("pdf-review-citation-select", {
      detail: {
        ...citation,
        jobId: model.jobId,
        reactionAssociated: Boolean(row),
        reactionId: row?.id,
      },
    }));
    state.interactionMessage = `Opened ${citation.source === "si" ? "supporting information" : "main paper"} page ${citation.page}.`;
  }

  async function saveCurrentView() {
    const result = state.interactionResult;
    if (!result) {
      state.interactionMessage = "Run a sealed example before saving a view.";
      return;
    }
    let saved;
    try {
      saved = await interactionBackend.saveView(result.interactionId, result.title);
    } catch (error) {
      state.interactionMessage = `This reopened snapshot is already saved; run the query again to create another View. (${error.code || "unavailable"})`;
      return;
    }
    const patchHead = activePatchHead();
    const view = {
      id: `${result.interactionId}:${Date.now()}`,
      title: saved.title,
      createdAt: new Date().toISOString(),
      baseVersion: state.headVersion,
      basePatchHead: patchHead,
      backendVersion: result.backendVersion,
      reactionIds: result.reactionIds || [],
      claimIds: result.claimIds || [],
      evidenceIds: result.evidenceIds || [],
      request: result.request,
      result: saved.result,
    };
    state.savedViews.unshift(view);
    if (persistSavedViews()) state.interactionMessage = `Saved “${view.title}” locally.`;
  }

  function deleteSavedView(viewId) {
    state.savedViews = state.savedViews.filter((view) => view.id !== viewId);
    persistSavedViews();
  }

  function openSavedView(view) {
    state.interactionResult = view.result;
    state.interactionPrompt = view.request?.prompt || "";
    state.interactionScope = view.request?.scope || "paper";
    state.interactionMode = view.request?.mode || "query_index";
    state.interactionContext = view.request?.context || "paper+si";
    state.interactionInference = view.request?.inference || "drawn-only";
    state.interactionOutput = view.request?.output || view.result?.output || "cards";
    state.layer = "ask";
    const currentPatchHead = activePatchHead();
    state.interactionMessage = view.basePatchHead === currentPatchHead
      ? `Opened saved snapshot from ${new Date(view.createdAt).toLocaleString()}.`
      : "Opened a stale snapshot. Local correction versions changed after this view was saved.";
  }

  function exportCurrentView(format) {
    const result = state.interactionResult;
    if (!result) return;
    const sourceRights = sourceRightsMetadata(model);
    if (format === "json") {
      const payload = sourceRights ? { ...result, source_rights: sourceRights } : result;
      downloadText(`${model.jobId}_${result.id}.json`, `${JSON.stringify(payload, null, 2)}\n`);
      return;
    }
    if (format === "report") {
      const evidence = (result.items || []).flatMap((item) => item.evidence || [])
        .concat(result.evidence || []);
      const detailLines = result.items?.length
        ? result.items.flatMap((item) => [
            `### ${item.title}`,
            `- Status: ${item.status}`,
            `- Chemistry: ${item.chemistry}`,
            `- Conditions: ${item.conditions}`,
            `- Metrics: ${item.metrics}`,
          ])
        : result.rows?.length
          ? [
              `| ${(result.columns || []).join(" | ")} |`,
              `| ${(result.columns || []).map(() => "---").join(" | ")} |`,
              ...result.rows.map((row) => `| ${row.join(" | ")} |`),
            ]
          : result.comparison
            ? Object.entries(result.comparison).map(([key, value]) => `- ${key}: ${value}`)
            : [];
      const report = [
        `# ${result.title}`,
        "",
        result.summary,
        "",
        `Scope: ${result.request?.scope || "paper"}`,
        `Execution: ${result.request?.mode || "query_index"}`,
        `Backend: ${result.backendVersion}`,
        `Reaction IDs: ${(result.reactionIds || []).join(", ") || "none"}`,
        ...(sourceRights ? [
          "",
          "## Source rights",
          `- License: ${sourceRights.license || "unspecified"}`,
          `- Attribution: ${sourceRights.attribution || "unspecified"}`,
          `- Source: ${sourceRights.source_url || sourceRights.source_doi || "unspecified"}`,
          `- License URL: ${sourceRights.license_url || "unspecified"}`,
          `- Changes: ${sourceRights.change_notice || "none declared"}`,
        ] : []),
        "",
        "## Results",
        ...detailLines,
        "",
        "## Evidence",
        ...evidence.map((item) =>
          `- ${item.source} p${item.page || "?"}: ${item.quote || "Evidence text unavailable"}`),
        "",
        "## Warnings",
        ...(result.warnings || []).map((warning) => `- ${warning}`),
      ].join("\n");
      downloadText(`${model.jobId}_${result.id}.md`, `${report}\n`, "text/markdown");
      return;
    }
    const evidence = (result.items || []).flatMap((item) => item.evidence || [])
      .concat(result.evidence || []);
    const csv = [
      ["title", "source", "page", "quote"],
      ...evidence.map((item) => [result.title, item.source, item.page || "", item.quote || ""]),
    ].map((row) => row.map((cell) => `"${String(cell).replaceAll('"', '""')}"`).join(",")).join("\n");
    downloadText(`${model.jobId}_${result.id}.csv`, `${csv}\n`, "text/csv");
  }

  function renderCitation(citation) {
    const coords = Array.isArray(citation.bbox)
      ? citation.bbox.map((value) => Number(value).toFixed(0)).join(" · ") : "text location";
    return h("button", {
      class: `interaction-citation source-${citation.source}`,
      onClick: () => openCitation(citation),
    }, [
      h("b", null, `${citation.source === "si" ? "SI" : "Main"} · p${citation.page || "?"}`),
      citation.anchor != null ? h("span", null, `anchor ${citation.anchor}`) : null,
      h("code", null, coords),
      h("q", null, citation.quote || "Evidence text unavailable"),
    ]);
  }

  function renderInteractionResult(result) {
    if (!result) return h("div", { class: "interaction-empty" }, [
      h("b", null, "Start with a sealed worked example"),
      h("p", null, "Results will appear here as cards, a table, or a focused stereo report with clickable evidence."),
    ]);
    const header = h("header", { class: "interaction-result-head" }, [
      h("div", null, [
        h("span", { class: "sealed-result-badge" }, "SEALED DEMO · READ ONLY"),
        h("h3", null, result.title),
        h("p", null, result.summary),
        h("dl", { class: "interaction-result-meta" }, [
          h("dt", null, "Scope"), h("dd", null, result.request?.scope || "paper"),
          h("dt", null, "Execution"), h("dd", null,
            result.request?.mode === "reinspect_selection" ? "Reinspect selection fixture"
              : result.request?.mode === "synthesize_evidence" ? "Synthesize evidence fixture"
                : "Query existing sealed extraction"),
          h("dt", null, "Data"), h("dd", null, `Example sealed fixture · ${result.backendVersion}`),
          h("dt", null, "Reaction IDs"), h("dd", null,
            (result.reactionIds || []).join(", ") || "no canonical reaction IDs"),
          h("dt", null, "Not checked"), h("dd", null, (result.warnings || []).join(" · ")),
        ]),
      ]),
      h("div", { class: "interaction-result-actions" }, [
        h("button", { onClick: saveCurrentView }, "Save View"),
        h("button", { onClick: () => exportCurrentView("json") }, "JSON"),
        h("button", { onClick: () => exportCurrentView("csv") }, "CSV"),
        h("button", { onClick: () => exportCurrentView("report") }, "Report"),
      ]),
    ]);
    if (result.output === "table") {
      return h("section", { class: "interaction-result" }, [
        header,
        h("div", { class: "interaction-table-wrap" }, [
          h("table", null, [
            h("thead", null, h("tr", null, result.columns.map((cell) => h("th", null, cell)))),
            h("tbody", null, result.rows.map((row) =>
              h("tr", null, row.map((cell) => h("td", null, cell))))),
          ]),
        ]),
        h("div", { class: "interaction-evidence-list" }, result.evidence.map(renderCitation)),
      ]);
    }
    if (result.output === "report") {
      return h("section", { class: "interaction-result" }, [
        header,
        h("div", { class: "stereo-badges" }, result.badges.map(([label, value]) =>
          h("span", { class: value === "mismatch" ? "diff" : "" }, [h("b", null, label), value]))),
        h("dl", { class: "stereo-comparison" }, [
          h("dt", null, "Raw MolParser"), h("dd", null, h("code", null, result.comparison.rawMolParser)),
          h("dt", null, "Extracted"), h("dd", null, h("code", null, result.comparison.extracted)),
          h("dt", null, "Reviewed correction"), h("dd", null, h("code", null, result.comparison.reviewedCorrection)),
          h("dt", null, "Interpretation"), h("dd", null, result.comparison.note),
        ]),
        h("div", { class: "interaction-evidence-list" }, result.evidence.map(renderCitation)),
        h("div", { class: "apply-separation" }, [
          h("b", null, "Query is read-only"),
          h("span", null, "To apply this reviewed correction, create a versioned local patch. The sealed prediction remains immutable."),
          h("button", { onClick: () => { state.layer = "edit"; } }, "Open Edit"),
        ]),
      ]);
    }
    return h("section", { class: "interaction-result" }, [
      header,
      h("div", { class: "interaction-card-grid" }, (result.items || []).map((item) =>
        h("article", { class: "interaction-answer-card" }, [
          h("span", { class: "answer-status" }, item.status),
          h("h4", null, item.title),
          h("dl", null, [
            h("dt", null, "Chemistry"), h("dd", null, item.chemistry),
            h("dt", null, "Conditions"), h("dd", null, item.conditions),
            h("dt", null, "Metrics"), h("dd", null, item.metrics),
          ]),
          h("div", { class: "interaction-evidence-list" }, item.evidence.map(renderCitation)),
          h("p", { class: "answer-uncertainty" }, item.uncertainty),
        ]))),
    ]);
  }

  function renderInteraction() {
    const selectedRow = interactionSelection();
    const prompts = [
      ["Show six limitations and failed examples", "6 limitations", "cards"],
      ["Inspect Table 3 Pummerer BP selection", "Table 3", "table"],
      ["Check compound 9 strict stereo", "Compound 9 stereo", "report"],
    ];
    const inspectSelection = () => {
      const identifiers = selectedRow?.moleculeIdentifiers || [];
      if (selectedRow?.id === "pred:6:6") {
        runInteraction("Inspect selected Table 3 entry", "table", "reinspect_selection", "selection");
      } else if (["pred:1:6", "pred:1:7"].includes(selectedRow?.id) && identifiers.includes("9")) {
        runInteraction("Check selected compound 9 strict stereo", "report", "reinspect_selection", "selection");
      } else {
        runInteraction("Inspect this unsupported selection", "cards", "reinspect_selection", "selection");
      }
    };
    return h("section", { class: "pdf-layer interaction-workspace", "data-layer": "ask" }, [
      h("header", { class: "interaction-heading" }, [
        h("div", null, [
          h("span", { class: "eyebrow" }, "Ask this document"),
          h("h2", null, "Query evidence without changing extraction"),
          h("p", null, "Use the whole source or inspect the current reaction, molecule, anchor, or table selection."),
        ]),
        h("div", { class: "interaction-capability", role: "status" }, [
          h("b", null, "Interactive Pi backend not connected"),
          h("span", null, state.capabilities.sealedDemo
            ? "Sealed example adapter available; no model or OCSR call."
            : state.capabilities.message),
        ]),
      ]),
      h("div", { class: "selection-context" }, [
        h("b", null, "Current selection"),
        selectedRow
          ? h("button", { onClick: () => { state.layer = "inspector"; } },
              `${selectedRow.label} · p${selectedRow.page || "?"}`)
          : h("span", null, "Whole document"),
        selectedRow ? h("button", { onClick: inspectSelection }, "Inspect selection") : null,
        h("span", null, "BBox/reaction selection stays linked to the evidence canvas."),
      ]),
      h("div", { class: "prompt-chips" }, prompts.map(([prompt, label, output]) =>
        h("button", { onClick: () => runInteraction(prompt, output) }, label))),
      h("div", { class: "interaction-composer" }, [
        h("textarea", {
          "aria-label": "Ask this document",
          value: state.interactionPrompt,
          placeholder: "Ask about limitations, a reaction, molecule, table row, yield, or stereochemistry…",
          onInput: (event) => { state.interactionPrompt = event.target.value; },
        }),
        h("div", { class: "interaction-controls" }, [
          h("label", null, ["Mode", h("select", {
            value: state.interactionMode,
            onChange: (event) => { state.interactionMode = event.target.value; },
          }, [
            h("option", { value: "query_index" }, "Query extraction"),
            h("option", { value: "synthesize_evidence" }, "Synthesize evidence"),
            h("option", { value: "reinspect_selection" }, "Reinspect selection"),
          ])]),
          h("label", null, ["Scope", h("select", {
            value: state.interactionScope,
            onChange: (event) => { state.interactionScope = event.target.value; },
          }, [
            h("option", { value: "paper" }, "Whole source"),
            h("option", { value: "main" }, "Main paper"),
            h("option", { value: "si" }, "SI only"),
            h("option", { value: "page" }, "Current page"),
            h("option", { value: "anchor" }, "Current anchor"),
            h("option", { value: "selection" }, "Current selection"),
          ])]),
          h("label", null, ["Context", h("select", {
            value: state.interactionContext,
            onChange: (event) => { state.interactionContext = event.target.value; },
          }, [
            h("option", { value: "figure" }, "Figure only"),
            h("option", { value: "paper" }, "Paper"),
            h("option", { value: "paper+si" }, "Paper + SI"),
          ])]),
          h("label", null, ["Inference", h("select", {
            value: state.interactionInference,
            onChange: (event) => { state.interactionInference = event.target.value; },
          }, [
            h("option", { value: "drawn-only" }, "Drawn only"),
            h("option", { value: "deterministic-reverse" }, "Deterministic reverse"),
          ])]),
          h("label", null, ["Output", h("select", {
            value: state.interactionOutput,
            onChange: (event) => { state.interactionOutput = event.target.value; },
          }, [
            h("option", { value: "cards" }, "Reaction cards"),
            h("option", { value: "table" }, "Table"),
            h("option", { value: "route" }, "Route"),
            h("option", { value: "report" }, "Evidence report"),
          ])]),
        ]),
        h("button", {
          class: "interaction-run",
          disabled: state.interactionBusy || !state.interactionPrompt.trim(),
          onClick: () => runInteraction(),
        }, state.interactionBusy ? "Opening sealed fixture…" : "Run query"),
        state.interactionError ? h("p", { class: "interaction-error", role: "alert" }, state.interactionError) : null,
        state.interactionMessage ? h("p", { class: "interaction-message", role: "status" }, state.interactionMessage) : null,
      ]),
      renderInteractionResult(state.interactionResult),
    ]);
  }

  function renderSavedViews() {
    return h("section", { class: "pdf-layer saved-views", "data-layer": "views" }, [
      h("header", null, [
        h("span", { class: "eyebrow" }, "Local snapshots"),
        h("h2", null, "Saved Views"),
        h("p", null, "A Saved View freezes the request, result, evidence links, and correction version. It never changes the sealed prediction."),
      ]),
      state.savedViews.length
        ? h("div", { class: "saved-view-list" }, state.savedViews.map((view) => {
            const currentPatchHead = activePatchHead();
            const stale = view.basePatchHead !== currentPatchHead;
            return h("article", null, [
              h("div", null, [
                h("b", null, view.title),
                h("span", { class: stale ? "view-stale" : "view-current" }, stale ? "stale" : "current"),
                h("small", null, `${new Date(view.createdAt).toLocaleString()} · correction v${view.baseVersion}`),
              ]),
              h("p", null, view.request?.prompt || "Saved interaction"),
              h("div", null, [
                h("button", { onClick: () => openSavedView(view) }, "Open snapshot"),
                h("button", { onClick: () => deleteSavedView(view.id) }, "Delete"),
              ]),
            ]);
          }))
        : h("div", { class: "interaction-empty" }, [
            h("b", null, "No Saved Views yet"),
            h("p", null, "Run a sealed example in Ask, then save the result for repeatable review."),
            h("button", { onClick: () => { state.layer = "ask"; } }, "Open Ask"),
          ]),
    ]);
  }

  function filteredRows() {
    const query = state.query.trim().toLowerCase();
    return rows.filter((row) =>
      rowMatchesFilter(row, state.filter, state.adjudications[row.id])
      && (!query || productSearchText(row).includes(query)));
  }

  function productRowMolecules(row) {
    return row.evidence
      ? [...(row.evidence.reactants || []), ...(row.evidence.products || [])] : [];
  }

  function reactionStructureSignature(row) {
    if (!row.source.pred) return null;
    const side = (items) => (items || []).map((item) => item.smi || item["E-SMILES"] || "")
      .filter(Boolean).sort().join(".");
    const reactants = side(row.evidence?.reactants || row.source.pred.reactants);
    const products = side(row.evidence?.products || row.source.pred.products);
    return reactants && products ? `${reactants}>>${products}` : `occurrence:${row.id}`;
  }

  function rowAnchorKind(row) {
    const anchor = viewAnchorByIndex(model, row.source.pred?.anchor);
    return String(anchor?.kind || anchor?.anchor_kind || "").toLowerCase();
  }

  function rowMatchesProductEvidence(row, filter) {
    const classes = sourceClasses(row);
    const molecules = productRowMolecules(row);
    if (filter === "all") return true;
    if (filter === "displayed") return rowAnchorKind(row) !== "prose";
    if (filter === "procedure") return rowAnchorKind(row) === "prose";
    if (filter === "visual") return molecules.some((item) =>
      moleculeHasLocatedEvidence(item) || item.source?.class === "molparser_raw");
    if (filter === "text-derived") return classes.has("text") || classes.has("derived");
    if (filter === "review") return productReviewReasons(row).length > 0;
    return true;
  }

  function rowMatchesProductReportability(row, filter) {
    if (filter === "all") return true;
    return reportabilityMeta(row.evidence?.review_context).code === filter;
  }

  function productDirectoryRows(attentionOnly = false, { includeQuery = true } = {}) {
    const settings = activeDirectoryState();
    const query = includeQuery ? settings.query.trim().toLowerCase() : "";
    return rows.filter((row) => {
      if (attentionOnly ? !productReviewReasons(row).length : !row.source.pred) return false;
      if (state.directoryScope !== "all"
          && rowDocumentSource(model, row) !== state.directoryScope) return false;
      return rowMatchesProductEvidence(row, settings.evidence)
        && rowMatchesProductReportability(row, settings.reportability)
        && (!query || productSearchText(row).includes(query));
    });
  }

  function rowEvidenceSummary(row) {
    const molecules = productRowMolecules(row);
    const located = molecules.filter(moleculeHasLocatedEvidence).length;
    const raw = molecules.filter((item) => item.source?.class === "molparser_raw").length;
    const claimWarnings = claimGroundingWarnings(row.evidence).length;
    const status = claimWarnings || (molecules.length > 0 && located < molecules.length)
      ? "attention"
      : molecules.length > 0 && located === molecules.length ? "complete" : "unavailable";
    return {
      located,
      raw,
      claimWarnings,
      status,
      label: [
        `${located}/${molecules.length} located`,
        raw ? `${raw} raw OCSR` : null,
        claimWarnings ? `${claimWarnings} claim gap${claimWarnings === 1 ? "" : "s"}`
          : "no recorded claim gaps",
      ].filter(Boolean).join(" · "),
    };
  }

  function contextSummaryForRows(sourceRows) {
    const counts = new Map();
    for (const row of sourceRows) {
      const meta = reportabilityMeta(row.evidence?.review_context);
      counts.set(meta.code, {
        ...meta,
        count: (counts.get(meta.code)?.count || 0) + 1,
      });
    }
    return [...counts.values()].sort((left, right) => {
      if (left.code === "unavailable") return 1;
      if (right.code === "unavailable") return -1;
      return left.short.localeCompare(right.short);
    });
  }

  function productAttentionUnits(sourceRows) {
    const reviewRows = sourceRows || [];
    const reactionRows = reviewRows.filter((row) => row.source.pred || row.source.gt);
    const unlinkedRows = reviewRows.filter(
      (row) => row.source.status === "unextracted_region",
    );
    return {
      reactions: reactionRows.length,
      claims: reactionRows.reduce(
        (total, row) => total + claimGroundingWarnings(row.evidence).length,
        0,
      ),
      unlinkedRegions: unlinkedRows.length,
      reviewItems: reviewRows.length,
    };
  }

  function productStats() {
    const anchors = model.view?.anchors || [];
    const pages = (model.documents || []).flatMap((document) =>
      (document.pages || []).map((page) => ({
        ...page,
        source_document: document.source === "si" ? "si" : "main",
      })));
    if (!pages.length) {
      pages.push(...(model.view?.pages || []).map((page) => ({
        ...page,
        source_document: "main",
      })));
    }
    const molecules = rows.flatMap((row) => row.evidence
      ? [...row.evidence.reactants, ...row.evidence.products] : []);
    const substances = new Map();
    for (const molecule of molecules) {
      const key = molecule.identifier || molecule.smi || "unnamed";
      substances.set(key, (substances.get(key) || 0) + 1);
    }
    const predictionRows = rows.filter((row) => row.source.pred);
    return {
      anchors,
      pages,
      molecules,
      drawableAnchors: anchors.filter((anchor) =>
        Array.isArray(anchor.bbox) && anchor.bbox.length === 4).length,
      proseAnchors: anchors.filter((anchor) =>
        String(anchor.kind || anchor.anchor_kind || "").toLowerCase() === "prose").length,
      uniqueTransformations: new Set(
        predictionRows.map(reactionStructureSignature).filter(Boolean),
      ).size,
      raw: molecules.filter((item) => item.source?.class === "molparser_raw").length,
      derived: molecules.filter((item) => item.source?.class === "derived").length,
      text: molecules.filter((item) => item.source?.class === "text").length,
      imageUntraced: molecules.filter((item) => item.source?.class === "image_untraced").length,
      unlocated: molecules.filter((item) => !moleculeHasLocatedEvidence(item)).length,
      substances: [...substances.entries()].sort((a, b) => b[1] - a[1]),
    };
  }

  function renderProductCensus() {
    const stats = productStats();
    const reactionCount = rows.filter((row) => row.source.pred).length;
    const located = stats.molecules.filter(moleculeHasLocatedEvidence).length;
    const unlocatedNeedingSourceReview = stats.molecules.filter((molecule) =>
      !moleculeHasLocatedEvidence(molecule) && molecule.source?.class === "untracked").length;
    const derivedUnlocated = stats.molecules.filter((molecule) =>
      !moleculeHasLocatedEvidence(molecule) && molecule.source?.class === "derived").length;
    const informationalUnlocated = Math.max(
      0,
      stats.unlocated - unlocatedNeedingSourceReview,
    );
    const unlocatedLabel = unlocatedNeedingSourceReview
      ? informationalUnlocated
        ? `${unlocatedNeedingSourceReview} need source review · ${informationalUnlocated} off-page info`
        : `${unlocatedNeedingSourceReview} need source review`
      : `${stats.unlocated} off-page · info`;
    return h("aside", {
      class: "document-census",
      "data-testid": "document-census",
      "aria-label": "Document extraction census",
      tabindex: 0,
    }, [
      h("span", null, `${stats.pages.length} pages`),
      h("span", null, `${stats.anchors.length} anchors`),
      h("span", null, `${reactionCount} reaction occurrences`),
      h("span", null, `${located} located molecules`),
      stats.unlocated ? h("span", {
        class: unlocatedNeedingSourceReview ? "census-attention" : "census-info",
        title: unlocatedNeedingSourceReview
          ? "These molecule occurrences lack both visual location and a traceable source."
          : derivedUnlocated === stats.unlocated
            ? "All of these are declared derived/unlocated occurrences; they do not inflate Attention."
            : "Informational off-page occurrences are counted here but do not inflate Attention.",
      }, unlocatedLabel) : null,
    ]);
  }

  function distinctMoleculeBoxCount(predicate) {
    return new Set((model.view?.molecules || [])
      .filter((item) => predicate(item) && Array.isArray(item.bbox))
      .map((item) => `${documentSource(item)}:${item.page}:`
        + item.bbox.map((value) => Number(value).toFixed(2)).join(",")))
      .size;
  }

  function anchorBoxCount(anchorIndex) {
    return new Set((moleculesByAnchor.get(anchorIndex) || [])
      .filter((item) => Array.isArray(item.bbox))
      .map((item) => `${documentSource(item)}:${item.page}:`
        + item.bbox.map((value) => Number(value).toFixed(2)).join(",")))
      .size;
  }

  function rowPage(row) {
    const anchorIndex = row.source.pred?.anchor;
    if (Number.isInteger(anchorIndex)) return Number(viewAnchorByIndex(model, anchorIndex)?.page) || null;
    return Number(row.source.gt?.page || row.source.region?.page) || null;
  }

  function coveragePages(stats) {
    return stats.pages.map((page, index) => {
      const pageNumber = Number(page.page) || index + 1;
      const sourceDocument = documentSource(page);
      const anchors = stats.anchors.map((anchor, anchorIndex) => ({
        ...anchor,
        index: Number.isInteger(anchor.index) ? anchor.index : anchorIndex,
      })).filter((anchor) =>
        documentSource(anchor) === sourceDocument && Number(anchor.page) === pageNumber);
      const pageRows = rows.filter((row) =>
        rowDocumentSource(model, row) === sourceDocument && rowPage(row) === pageNumber);
      const reactions = pageRows.filter((row) => row.source.pred || row.source.gt);
      const attention = pageRows.filter((row) => productReviewReasons(row).length);
      const unextracted = pageRows.filter((row) => row.source.status === "unextracted_region");
      const boxes = distinctMoleculeBoxCount((item) =>
        documentSource(item) === sourceDocument && Number(item.page) === pageNumber);
      return {
        page: pageNumber,
        sourceDocument,
        anchors,
        reactions: reactions.length,
        attention: attention.length,
        boxes,
        status: unextracted.length ? "attention" : reactions.length ? "covered" : "text",
      };
    });
  }

  function selectCoveragePage(page) {
    if (page.anchors[0]) {
      selectAnchor(page.anchors[0], page.anchors[0].index);
    } else {
      window.dispatchEvent(new CustomEvent("pdf-review-select", {
        detail: {
          jobId: model.jobId,
          differenceId: null,
          page: page.page,
          sourceDocument: page.sourceDocument,
        },
      }));
    }
  }

  function anchorThumbnailModel(anchor, anchorIndex) {
    if (anchorThumbnailCache.has(anchorIndex)) return anchorThumbnailCache.get(anchorIndex);
    const bbox = (anchor.bbox || []).slice(0, 4).map(Number);
    if (bbox.length < 4 || bbox.some((value) => !Number.isFinite(value))) {
      return { size: null, boxes: [] };
    }
    const pad = 8;
    const source = documentSource(anchor);
    const page = (model.documents || []).find((item) => item.source === source)
      ?.pages?.find((item) => item.page === anchor.page)
      || (model.view?.pages || []).find((item) =>
        documentSource(item) === source && item.page === anchor.page);
    const originX = Math.max(0, bbox[0] - pad);
    const originY = Math.max(0, bbox[1] - pad);
    const cropX2 = Math.min(Number(page?.width) || Infinity, bbox[2] + pad);
    const cropY2 = Math.min(Number(page?.height) || Infinity, bbox[3] + pad);
    const width = Math.max(1, cropX2 - originX);
    const height = Math.max(1, cropY2 - originY);
    const groups = new Map();
    for (const molecule of moleculesByAnchor.get(anchorIndex) || []) {
      if (!Array.isArray(molecule.bbox)) continue;
      const key = molecule.bbox.map((value) => Number(value).toFixed(2)).join(",");
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(molecule);
    }
    const thumbnail = {
      size: { w: width, h: height },
      boxes: [...groups.entries()].map(([key, occurrences]) => {
        const molecule = occurrences[0];
        return {
          key,
          bbox: molecule.bbox.map((value, index) =>
            Number(value) - (index % 2 ? originY : originX)),
          role: molecule.side,
          label: occurrences.length > 1 ? `×${occurrences.length}` : "",
        };
      }),
    };
    anchorThumbnailCache.set(anchorIndex, thumbnail);
    return thumbnail;
  }

  function selectAnchor(anchor, anchorIndex) {
    state.workspaceLocation = null;
    const row = rowsByAnchor.get(anchorIndex)?.[0] || null;
    if (row) state.selectedId = row.id;
    if (productMode) state.layer = "region";
    window.dispatchEvent(new CustomEvent("pdf-review-anchor-select", {
      detail: {
        jobId: model.jobId,
        anchor: anchorIndex,
        page: anchor.page,
        sourceDocument: documentSource(anchor),
        revealSource: true,
      },
    }));
  }

  function renderProductFilterContext(visibleCount, totalCount) {
    const settings = activeDirectoryState();
    const chips = [
      state.directoryScope !== "all"
        ? (state.directoryScope === "si" ? "Supporting information" : "Main paper")
        : null,
      settings.evidence !== "all"
        ? settings.evidence.replaceAll("-", " ") : null,
      settings.reportability !== "all"
        ? REPORTABILITY_META[settings.reportability].short : null,
      settings.query ? `“${settings.query}”` : null,
    ].filter(Boolean);
    return h("div", {
      class: "workspace-filter-context",
      role: "status",
      "aria-label": `${visibleCount} of ${totalCount} reaction occurrences shown`,
    }, [
      h("span", { class: "workspace-filter-count" },
        `${visibleCount} of ${totalCount} reaction occurrences`),
      h("span", { class: "workspace-filter-chips" },
        chips.length
          ? chips.map((label) => h("span", null, label))
          : h("span", { class: "is-default" }, "All sources · all evidence · all context")),
      chips.length ? h("button", {
        class: "workspace-filter-clear",
        onClick: clearProductViewFilters,
      }, "Clear view") : null,
    ]);
  }

  function renderProductOverview() {
    const stats = productStats();
    const attention = rows.filter((row) => productReviewReasons(row).length);
    const attentionUnits = productAttentionUnits(attention);
    const reviewedAttentionReactions = attention.filter(
      (row) => (row.source.pred || row.source.gt) && state.adjudications[row.id],
    ).length;
    const attentionGroups = new Map();
    for (const row of attention) {
      for (const reason of productReviewReasons(row)) {
        if (!attentionGroups.has(reason)) attentionGroups.set(reason, []);
        attentionGroups.get(reason).push(row);
      }
    }
    const groupedAttention = [...attentionGroups.entries()]
      .sort((left, right) => right[1].length - left[1].length);
    const reactionCount = rows.filter((row) => row.source.pred || row.source.gt).length;
    const unextractedCount = rows.filter(
      (row) => row.source.status === "unextracted_region",
    ).length;
    const claimWarningCount = rows.reduce(
      (total, row) => total + claimGroundingWarnings(row.evidence).length,
      0,
    );
    const reportabilityCounts = rows.filter((row) => row.source.pred).reduce((counts, row) => {
      const value = row.evidence?.review_context?.reportability || "unspecified";
      counts[value] = (counts[value] || 0) + 1;
      return counts;
    }, {});
    const reportabilityParts = [
      ["prior_art", "prior-art"],
      ["performed_forward", "performed"],
      ["reported_forward", "reported"],
      ["failed_outcome", "failed outcomes"],
      ["proposed_mechanism", "proposed mechanisms"],
    ].filter(([key]) => reportabilityCounts[key])
      .map(([key, label]) => `${reportabilityCounts[key]} ${label}`);
    if (reportabilityCounts.unspecified && reportabilityParts.length) {
      reportabilityParts.push(`${reportabilityCounts.unspecified} context not declared`);
    }
    const reportabilitySummary = reportabilityParts.join(" · ")
      || "reportability context not attached";
    const byAnchor = stats.anchors.map((anchor, index) => {
      const anchorIndex = Number.isInteger(anchor.index) ? anchor.index : index;
      return {
        ...anchor,
        index: anchorIndex,
        label: anchor.label || `Anchor ${index + 1}`,
        count: rows.filter((row) => row.source.pred?.anchor === anchorIndex).length,
        boxes: anchorBoxCount(anchorIndex),
      };
    }).filter((anchor) =>
      anchor.page && Array.isArray(anchor.bbox) && anchor.bbox.length === 4 && anchor.count > 0);
    const coverage = coveragePages(stats);
    return h("section", { class: "pdf-layer product-overview", "data-layer": "overview" }, [
      h("div", { class: "product-truth-banner" }, [
        h("b", null, "Prediction workspace · no reference attached"),
        h("span", null, "Score unavailable — no reference attached. Counts are model-declared; document completeness is not independently proven."),
      ]),
      h("div", { class: "coverage-headline", "aria-label": "Document extraction summary" }, [
        h("div", null, [
          h("strong", null, String(reactionCount)),
          h("span", null, "reaction occurrences"),
        ]),
        h("div", null, [
          h("strong", null, String(stats.uniqueTransformations)),
          h("span", null, "unique structure signatures"),
        ]),
        h("div", null, [
          h("strong", null, `${reviewedAttentionReactions}/${attentionUnits.reactions}`),
          h("span", null, "affected reactions reviewed"),
        ]),
        h("div", null, [
          h("strong", null, String(stats.anchors.length)),
          h("span", null, `${stats.drawableAnchors} drawable + ${stats.proseAnchors} prose anchors`),
        ]),
        h("p", null, `${stats.raw} MolParser raw · ${stats.text} text · ${stats.derived} derived · `
          + `${stats.imageUntraced} image-untraced molecule occurrences · ${unextractedCount} unlinked regions · `
          + `${claimWarningCount} ungrounded condition/metric claims · `
          + reportabilitySummary),
      ]),
      h("div", { class: "coverage-overview-grid" }, [
        h("section", { class: "product-summary-card coverage-panel" }, [
          h("div", { class: "summary-title" }, [
            h("div", null, [
              h("span", { class: "eyebrow" }, "Document coverage"),
              h("b", null, "Where reactions were found"),
            ]),
            h("span", { class: "coverage-legend" }, "● extracted  ◆ attention  ○ text only"),
          ]),
          h("div", { class: "coverage-map", "aria-label": "PDF page coverage map" },
            coverage.map((page) => h("button", {
              class: `coverage-page status-${page.status}`,
              "aria-label": `${page.sourceDocument === "si" ? "Supporting information" : "Main paper"} page ${page.page}: ${page.reactions} reaction occurrences, ${page.anchors.length} anchors, ${page.boxes} molecule boxes, ${page.attention} review risks`,
              onClick: () => selectCoveragePage(page),
            }, [
              h("span", { class: "coverage-page-number" },
                `${page.sourceDocument === "si" ? "SI " : ""}p${page.page}`),
              h("i", { "aria-hidden": "true" }),
              h("strong", null, page.reactions ? `${page.reactions} occurrences` : "text only"),
              h("small", null, page.anchors.length
                ? `${page.anchors.length} anchor${page.anchors.length === 1 ? "" : "s"} · ${page.boxes} boxes`
                : "no extracted anchor"),
              page.attention
                ? h("em", null, `${page.attention} review risk${page.attention === 1 ? "" : "s"}`) : null,
            ]))),
          h("p", { class: "coverage-note" }, "Coverage shows model-declared extraction, not independent proof that every document reaction was found."),
        ]),
        h("section", { class: "product-summary-card attention-rail" }, [
          h("div", { class: "summary-title" }, [
            h("div", null, [
              h("span", { class: "eyebrow" }, "Top attention"),
              h("b", null, "Review evidence first"),
            ]),
            h("button", { onClick: () => openWorkspaceModule("attention") }, "Open queue"),
          ]),
          h("div", {
            class: "attention-unit-summary",
            "data-testid": "attention-unit-summary",
            "aria-label": "Attention queue units",
          }, [
            h("span", null, [
              h("strong", null, String(attentionUnits.reactions)),
              " affected reactions",
            ]),
            h("span", null, [
              h("strong", null, String(attentionUnits.claims)),
              " unresolved claims",
            ]),
            h("span", null, [
              h("strong", null, String(attentionUnits.unlinkedRegions)),
              " unlinked regions",
            ]),
          ]),
          ...(groupedAttention.length ? groupedAttention.slice(0, 6).map(([reason, affected]) => h("button", {
            class: "attention-preview attention-aggregate",
            onClick: () => selectRow(affected[0]),
          }, [
            h("small", null, String(affected.length)),
            h("b", null, productReasonMeta(reason).label),
            h("span", null, affected.length === 1
              ? `${affected[0].source.pred?.anchor_label || "Document"} · ${affected[0].label}`
              : `${affected.length} review items; open to inspect the first`),
          ])) : [h("p", null, "No review-required evidence risks detected.")]),
        ]),
      ]),
      h("div", { class: "product-summary-grid" }, [
        h("section", { class: "product-summary-card" }, [
          h("div", { class: "summary-title" }, [
            h("div", { class: "anchor-summary-heading" }, [
              h("b", null, "Extracted anchors"),
              h("span", { class: "coverage-legend" },
                `${stats.drawableAnchors} drawable in gallery · ${stats.proseAnchors} prose in reaction list`),
            ]),
            h("button", { onClick: () => openWorkspaceModule("reactions") }, "Open reactions"),
          ]),
          h("div", { class: "route-strip", role: "group", "aria-label": "Extracted anchor gallery" },
            byAnchor.map((anchor, index) => h("button", {
              class: `route-node ${model.initialAnchor === anchor.index ? "initial" : ""}`,
              "aria-label": `${documentSource(anchor) === "si" ? "Supporting information" : "Main paper"}, ${anchor.label}, page ${anchor.page}, ${anchor.count} reaction occurrences, ${anchor.boxes} boxes`,
              onClick: () => selectAnchor(anchor, anchor.index),
            }, [
              h("span", { class: "anchor-thumb" }, [
                h(BBoxImagePanel, {
                  imageUrl: `/api/jobs/${encodeURIComponent(model.jobId)}/pdf/anchor/${anchor.index}/crop?dpi=72`,
                  coordinateSize: anchorThumbnailModel(anchor, anchor.index).size,
                  boxes: anchorThumbnailModel(anchor, anchor.index).boxes,
                  alt: `${anchor.label} extraction crop`,
                  compact: true,
                }),
                h("i", null, `${anchor.boxes} boxes`),
              ]),
              h("span", { class: "route-index" }, String(index + 1).padStart(2, "0")),
              h("b", null, anchor.label),
              h("small", null, `p${anchor.page || "?"} · ${anchor.count} occurrences · ${anchor.boxes} boxes`),
            ]))),
        ]),
        h("details", { class: "product-summary-card substance-library" }, [
          h("summary", null, `Substance library · ${stats.substances.length}`),
          h("div", null, stats.substances.slice(0, 24).map(([name, count]) => h("span", null, [
            h("b", null, name),
            h("small", null, `${count} occurrence${count === 1 ? "" : "s"}`),
          ]))),
        ]),
      ]),
      h("div", { class: "product-export" }, [
        h("button", {
          "aria-expanded": String(state.exportOpen),
          onClick: () => { state.exportOpen = !state.exportOpen; },
        }, "Export ▾"),
        state.exportOpen ? h("div", { class: "export-menu", role: "menu" }, [
          h("button", { role: "menuitem", onClick: exportSummary }, "Reviewed changes · JSON"),
          h("button", { role: "menuitem", onClick: () => exportProduct("patches") }, "Patch history · JSON"),
          h("button", { role: "menuitem", onClick: () => exportProduct("report") }, "Review report + context · JSON"),
          h("button", { role: "menuitem", onClick: () => exportExtraction("v2") }, "Raw prediction · JSON"),
          h("button", { role: "menuitem", onClick: () => exportExtraction("csv") }, "Reaction table · CSV"),
          h("button", { role: "menuitem", onClick: () => exportExtraction("rxn") }, "MDL RXN bundle · ZIP"),
          h("button", { role: "menuitem", onClick: () => exportExtraction("report") }, "Extraction report · JSON"),
          sourceRightsMetadata(model)
            ? h("button", { role: "menuitem", onClick: () => exportExtraction("rights") }, "Source rights · JSON")
            : null,
        ]) : null,
      ]),
    ]);
  }

  function renderOverview() {
    const summary = model.compare?.summary || {};
    const loop = summary.loop_score || model.meta?.job?.pdf_score || {};
    const adjudicated = Object.keys(state.adjudications).length;
    const claims = judgmentRows.reduce((total, row) => total + row.claims.length, 0);
    const chemistryDiffs = rows.filter((row) => row.dimensions.chemistry === "diff").length;
    const diagnostics = summary.review_diagnostics || {};
    const stereo = diagnostics.strict_stereo || {};
    const yieldAudit = diagnostics.yield || {};
    return h("section", { class: "pdf-layer pdf-overview", "data-layer": "overview" }, [
      h("div", { class: "pdf-overview-grid" }, [
        h("div", { class: "pdf-overview-card primary" }, [
          h("span", { class: "eyebrow" }, "Review progress"),
          h("strong", null, `${adjudicated} / ${judgmentRows.length}`),
          h("span", null, "adjudicated / unique review items to judge"),
        ]),
        h("div", { class: "pdf-overview-card" }, [
          h("span", { class: "eyebrow" }, "Authoritative score"),
          h("strong", null, `A ${Number(loop.reward ?? summary.axes?.A ?? 0).toFixed(4)}`),
          h("span", null, `loop overall ${loop.overall == null ? "unavailable" : Number(loop.overall).toFixed(4)}`),
        ]),
        h("div", { class: "pdf-overview-card" }, [
          h("span", { class: "eyebrow" }, "Judgment load"),
          h("strong", null, `${rows.length} unique review items`),
          h("span", null, `${model.compare?.summary?.n_pred ?? rows.length} prediction rows`
            + `${summary.gt_only ? ` + ${summary.gt_only} GT-only` : ""}; `
            + `${claims} candidate claims; ${chemistryDiffs} chemistry diffs`),
        ]),
        h("div", { class: "pdf-overview-card diagnostic" }, [
          h("span", { class: "eyebrow" }, "Strict-stereo diagnostics"),
          h("strong", null, stereo.reaction_exact
            ? `${(100 * stereo.reaction_exact.f1).toFixed(2)}% reaction exact`
            : "unavailable"),
          h("span", null, stereo.reaction_soft
            ? `soft ${(100 * stereo.reaction_soft.score).toFixed(2)}% · `
              + `R ${(100 * stereo.reactants.f1).toFixed(2)}% · `
              + `P ${(100 * stereo.products.f1).toFixed(2)}%`
            : "requires explicit post-hoc GT"),
        ]),
        h("div", { class: "pdf-overview-card diagnostic" }, [
          h("span", { class: "eyebrow" }, "Yield numeric diagnostic"),
          h("strong", null, yieldAudit.coverage == null
            ? "unavailable"
            : `${yieldAudit.matched} / ${yieldAudit.n_gt}`),
          h("span", null, yieldAudit.coverage == null
            ? "requires explicit post-hoc GT"
            : `${(100 * yieldAudit.coverage).toFixed(2)}%; not a frozen axis`),
        ]),
      ]),
      h("div", { class: "pdf-honesty" }, [
        summary.B_anchor_only != null
          ? h("span", { class: "axis-warning" },
              `Local grounding: anchor-only ${Number(summary.B_anchor_only).toFixed(4)}; not a complete B/overall`)
          : summary.axes?.B != null
            ? h("span", null, `Local full B ${Number(summary.axes.B).toFixed(4)}`)
            : h("span", null, "No post-hoc GT; compare axes unavailable"),
        h("button", { onClick: () => { state.layer = "queue"; } }, "Open difference queue"),
      ]),
    ]);
  }

  function renderQueue() {
    const visible = filteredRows();
    return h("section", { class: "pdf-layer pdf-queue", "data-layer": "queue" }, [
      h("div", { class: "queue-controls" }, [
        h("input", {
          value: state.query,
          placeholder: "Filter label, identifier, anchor",
          onInput: (event) => { state.query = event.target.value; },
        }),
        h("span", { class: "queue-count", "data-testid": "queue-visible-count" },
          `${visible.length} visible / ${rows.length} unique differences`),
      ]),
      h("div", { class: "queue-filters" }, FILTERS.map(([value, label]) =>
        h("button", {
          class: state.filter === value ? "on" : "",
          onClick: () => { state.filter = value; },
        }, `${label} ${rows.filter((row) =>
          rowMatchesFilter(row, value, state.adjudications[row.id])).length}`))),
      h("div", { class: "queue-rows" }, visible.map((row) => {
        const saved = state.adjudications[row.id];
        return h("button", {
          class: `queue-row ${state.selectedId === row.id ? "selected" : ""}`,
          "data-difference-id": row.id,
          onClick: () => selectRow(row),
        }, [
          h("span", { class: "queue-title" }, row.label),
          h("span", { class: "queue-dimensions" }, [
            dimensionChip("Chemistry", row.dimensions.chemistry),
            dimensionChip("Conditions", row.dimensions.conditions),
            dimensionChip("Grounding", row.dimensions.grounding),
            dimensionChip("Provenance", row.dimensions.provenance),
            dimensionChip("Identifier", row.dimensions.identifier),
            dimensionChip("Metrics/Yield", row.dimensions.metrics),
            dimensionChip("Human", saved ? "reviewed" : "pending"),
          ]),
          h("span", { class: "queue-claims" },
            `${row.claims.length} candidate claim${row.claims.length === 1 ? "" : "s"}`),
        ]);
      })),
    ]);
  }

  function moleculeSmiles(molecule) {
    return molecule?.smi || molecule?.["E-SMILES"] || "";
  }

  // One canonical render size for every list thumbnail so the server LRU + browser cache are shared
  // across identical molecules (cache key is exact (smi,w,h)). Display size is set smaller in CSS.
  const RXN_THUMB_W = 96;
  const RXN_THUMB_H = 60;

  function moleculeThumb(row, molecule, role) {
    // Follow a human's Ketcher correction, not the stale extracted structure.
    const smi = reviewedMoleculeFor(row, molecule) || moleculeSmiles(molecule);
    if (!smi) return null;
    const label = molecule.identifier || molecule.description || "structure";
    const src = `/api/render?smi=${encodeURIComponent(smi)}&w=${RXN_THUMB_W}&h=${RXN_THUMB_H}`;
    // The endpoint always returns an SVG (a labelled placeholder for unparseable SMILES), so onerror
    // never fires -- the placeholder IS the "unrenderable" state. E-SMILES renders its base only, so
    // flag "partial" ONLY when there is real (non-whitespace) extension content after the first <sep>.
    const partial = /\S/.test(smi.split("<sep>").slice(1).join("<sep>"));
    return h("img", {
      // Key by render URL so filtering/reordering never reuses a DOM node with a stale image.
      key: src,
      class: `rxn-thumb rxn-thumb-${role}`,
      src,
      width: RXN_THUMB_W,
      height: RXN_THUMB_H,
      loading: "lazy",
      decoding: "async",
      alt: `${role}: ${label}`,
      title: partial ? `${label}\n${smi}\n(base structure preview; E-SMILES extensions not shown)`
        : `${label}\n${smi}`,
    });
  }

  // A compact reactant -> product structure strip so the list is scannable as chemistry, not just
  // identifiers. Returns null when the occurrence has no drawable structure (keeps the text-only row).
  function reactionStructureStrip(row) {
    const drawable = (items) => (items || [])
      .filter((item) => reviewedMoleculeFor(row, item) || moleculeSmiles(item));
    const reactants = drawable(row.evidence?.reactants);
    const products = drawable(row.evidence?.products);
    if (!reactants.length && !products.length) return null;
    const side = (items, role) => (items.length
      ? [moleculeThumb(row, items[0], role),
         items.length > 1 ? h("span", { class: "rxn-more" }, `+${items.length - 1}`) : null]
      : [h("span", { class: "rxn-thumb rxn-thumb-empty", title: "no structure read" }, "?")]);
    return h("span", { class: "reaction-structures", "aria-hidden": "true" }, [
      ...side(reactants, "reactant"),
      h("span", { class: "rxn-arrow" }, "→"),
      ...side(products, "product"),
    ]);
  }

  function conditionAliasStructureStrip(row) {
    const aliases = (row.evidence?.conditions || []).flatMap((step) =>
      (Array.isArray(step) ? step : [step]))
      .filter((item) => item && typeof item === "object"
        && item["E-SMILES"] && item._source?.alias);
    if (!aliases.length) return null;
    const unique = [...new Map(aliases.map((item) => [item._source.alias, item])).values()];
    return h("span", {
      class: "reaction-structures condition-alias-structures",
      title: "Structurally drawn condition aliases expanded to concrete E-SMILES",
    }, [
      h("span", { class: "rxn-arrow" }, "cat."),
      ...unique.slice(0, 2).map((item) => moleculeThumb(row, {
        identifier: item._source.alias,
        "E-SMILES": item["E-SMILES"],
      }, "condition")),
      unique.length > 2 ? h("span", { class: "rxn-more" }, `+${unique.length - 2}`) : null,
    ]);
  }

  function renderProductReactionButton(row, stayInRegion = false, onSelect = null) {
    const reactantIds = (row.evidence?.reactants || [])
      .map((item) => item.identifier).filter(Boolean);
    const productIds = (row.evidence?.products || [])
      .map((item) => item.identifier).filter(Boolean);
    const reactionLabel = reactantIds.length || productIds.length
      ? `${reactantIds.join(" + ") || "?"} → ${productIds.join(" + ") || "?"}`
      : "Unlabelled reaction occurrence";
    const failedOutcome = row.evidence?.review_context?.reportability === "failed_outcome";
    const reactionDisplay = productIds.length
      ? `${productIds.join(" + ")} · from ${reactantIds.join(" + ") || "unlabelled inputs"}`
      : failedOutcome
        ? `No desired product · from ${reactantIds.join(" + ") || "reported inputs"}`
        : reactionLabel;
    const conditions = conditionTexts(row.evidence?.conditions);
    const exceptionReason = row.evidence?.exception?.reason;
    const reviewReasons = productReviewReasons(row);
    const context = reportabilityMeta(row.evidence?.review_context);
    return h("button", {
      key: row.id,
      class: `product-reaction-row ${state.selectedId === row.id ? "selected" : ""}`,
      "data-difference-id": row.id,
      onClick: () => onSelect
        ? onSelect(row)
        : stayInRegion ? selectRegionRow(row) : selectRow(row),
    }, [
      h("span", { class: "reaction-index" }, [
        row.label || `row ${row.source.pred?.example + 1}`,
        rowDocumentSource(model, row) === "si"
          ? h("span", { class: "rxn-src-tag", title: "from the Supporting Information" }, "SI")
          : null,
        row.evidence?.template?.kind === "markush"
          ? h("span", { class: "rxn-src-tag", title: "source-grounded generic Markush reaction" }, "Markush")
          : null,
      ]),
      h("span", { class: "reaction-summary" }, [
        reactionStructureStrip(row),
        conditionAliasStructureStrip(row),
        h("b", { title: reactionLabel }, reactionDisplay),
        h("small", null, [conditions.join(" · "), exceptionReason]
          .filter(Boolean).join(" · ") || "No condition text"),
      ]),
      h("span", { class: "reaction-flags" }, [
        h("span", {
          class: `context-flag context-${context.className}`,
          title: context.label,
        }, context.short),
        ...reviewReasons.slice(0, 2).map((reason) => h("span", {
          class: "risk-flag",
          title: reason,
        }, productReasonMeta(reason).label)),
        reviewReasons.length > 2
          ? h("span", { class: "risk-flag" }, `+${reviewReasons.length - 2}`) : null,
        state.adjudications[row.id] ? h("span", { class: "reviewed-flag" }, "reviewed") : null,
        h("span", { class: "reaction-review-cta", "aria-hidden": "true" },
          state.selectedId === row.id ? "Selected" : "Review →"),
      ]),
    ]);
  }

  function renderRegionReview() {
    const row = selected();
    const anchorIndex = row?.source.pred?.anchor;
    const anchor = Number.isInteger(anchorIndex) ? viewAnchorByIndex(model, anchorIndex) : null;
    const regionRows = Number.isInteger(anchorIndex) ? rowsByAnchor.get(anchorIndex) || [] : [];
    return h("section", {
      class: "pdf-layer region-review",
      "data-layer": "region",
    }, [
      h("header", { class: "region-review-heading" }, [
        h("div", null, [
          h("span", { class: "eyebrow" }, "Selected PDF region"),
          h("h2", null, [
            anchor?.label || "Choose a region on the PDF",
            row?.evidence?.review_context?.label
              ? h("span", {
                  class: `anchor-context-tag context-${row.evidence.review_context.reportability}`,
                }, row.evidence.review_context.label) : null,
          ]),
          h("p", null, anchor
            ? `${documentSource(anchor) === "si" ? "Supporting information" : "Main paper"} · page ${anchor.page}`
            : "Click a blue region on the page to inspect its extracted reactions."),
        ]),
        anchor ? h("span", { class: "region-count" },
          `${regionRows.length} occurrence${regionRows.length === 1 ? "" : "s"}`) : null,
      ]),
      anchor ? h("div", { class: "region-reaction-list" },
        regionRows.map((item) => renderProductReactionButton(item, true)))
        : h("div", { class: "region-empty" }, [
          h("b", null, "Select a reaction region"),
          h("span", null, "The page stays primary; extracted reactions will appear here."),
        ]),
      row && anchor ? h("footer", { class: "region-review-footer" }, [
        h("span", null, "Click a reaction to highlight its molecules."),
        h("button", { onClick: () => { state.layer = "inspector"; } }, "View details"),
      ]) : null,
    ]);
  }

  function renderProductDirectory(attentionOnly = false) {
    const settings = activeDirectoryState();
    const query = settings.query.trim().toLowerCase();
    const candidates = attentionOnly
      ? rows.filter((row) => productReviewReasons(row).length)
      : rows.filter((row) => row.source.pred);
    const hasSI = rows.some((row) => rowDocumentSource(model, row) === "si");
    const sourceFilters = [
      ["all", "All"],
      ...(hasSI ? [["main", "Main"], ["si", "SI"]] : []),
    ];
    const evidenceFilters = [
      ["all", "All evidence"],
      ["displayed", "Scheme/Table"],
      ["procedure", "Procedure"],
      ["visual", "Image-grounded"],
      ["text-derived", "Text/derived"],
      ["review", "Needs review"],
    ];
    const availableContexts = new Set(candidates
      .map((row) => reportabilityMeta(row.evidence?.review_context).code));
    const contextFilters = [
      ["all", "All context"],
      ...[
        "performed_forward", "prior_art", "reported_forward",
        "failed_outcome", "proposed_mechanism", "unavailable",
      ].filter((value) => availableContexts.has(value))
        .map((value) => [value, REPORTABILITY_META[value].short]),
    ];
    const sourceScoped = candidates.filter((row) => state.directoryScope === "all"
      || rowDocumentSource(model, row) === state.directoryScope);
    const evidenceScoped = sourceScoped.filter((row) =>
      rowMatchesProductEvidence(row, settings.evidence));
    const visible = productDirectoryRows(attentionOnly);
    const visibleAttentionUnits = productAttentionUnits(visible);
    const groups = new Map();
    for (const row of visible) {
      const sourceDocument = rowDocumentSource(model, row);
      const anchorIndex = row.source.pred?.anchor;
      const page = rowPage(row);
      const key = Number.isInteger(anchorIndex)
        ? `${sourceDocument}|anchor:${anchorIndex}`
        : `${sourceDocument}|unlinked-regions`;
      if (!groups.has(key)) {
        groups.set(key, {
          sourceDocument,
          anchorIndex: Number.isInteger(anchorIndex) ? anchorIndex : null,
          label: row.source.pred?.anchor_label
            || (Number.isInteger(anchorIndex)
              ? row.source.region?.label || "Extracted anchor"
              : "Unlinked detected regions"),
          page,
          pages: new Set(),
          rows: [],
        });
      }
      const group = groups.get(key);
      if (page) group.pages.add(page);
      group.rows.push(row);
    }
    const layerKey = attentionOnly ? "attention" : "all";
    const groupEntries = [...groups.entries()].sort((left, right) => {
      const a = left[1];
      const b = right[1];
      // Actionable, anchored chemistry comes first. Layout regions that were
      // never linked to an extraction stay visible as one honest, folded group
      // instead of pushing every reaction below a wall of page-level warnings.
      if ((a.anchorIndex == null) !== (b.anchorIndex == null)) {
        return a.anchorIndex == null ? 1 : -1;
      }
      if (a.sourceDocument !== b.sourceDocument) return a.sourceDocument === "main" ? -1 : 1;
      if ((a.page || Infinity) !== (b.page || Infinity)) return (a.page || Infinity) - (b.page || Infinity);
      return String(a.label).localeCompare(String(b.label));
    });
    const groupStateKey = (groupKey) => `${layerKey}|${groupKey}`;
    const collapsed = new Set(settings.collapsedGroups);
    if (!query && !settings.initializedLayers.includes(layerKey)) {
      const firstActionable = groupEntries.findIndex(([, group]) => group.anchorIndex != null);
      groupEntries.forEach(([groupKey, group], index) => {
        const collapseByDefault = group.anchorIndex == null
          || (groupEntries.length > 6 && index !== firstActionable);
        if (collapseByDefault) collapsed.add(groupStateKey(groupKey));
      });
      settings.collapsedGroups = [...collapsed].slice(0, PRODUCT_MAX_COLLAPSED_GROUPS);
      settings.initializedLayers = [...settings.initializedLayers, layerKey];
      scheduleReviewUiStatePersist();
    }
    const toggleGroup = (groupKey) => {
      const key = groupStateKey(groupKey);
      if (collapsed.has(key)) collapsed.delete(key);
      else collapsed.add(key);
      updateActiveDirectoryState("collapsedGroups", [...collapsed]);
    };
    const setVisibleGroupsCollapsed = (value) => {
      for (const [groupKey] of groupEntries) {
        const key = groupStateKey(groupKey);
        if (value) collapsed.add(key);
        else collapsed.delete(key);
      }
      updateActiveDirectoryState("collapsedGroups", [...collapsed]);
    };
    return h("section", {
      class: "pdf-layer product-directory",
      "data-layer": attentionOnly ? "attention" : "all",
    }, [
      h("div", { class: "queue-controls" }, [
        h("label", { class: "product-search" }, [
          h("span", null, "Search review items"),
          h("input", {
            value: settings.query,
            placeholder: "Reaction, region, identifier, condition, page, or anchor",
            onInput: (event) => updateActiveDirectoryState(
              "query", event.target.value.slice(0, 320),
            ),
          }),
        ]),
        h("span", { class: "queue-count", "data-testid": "queue-visible-count" },
          attentionOnly
            ? `${visibleAttentionUnits.reactions} affected reactions · `
              + `${visibleAttentionUnits.claims} unresolved claims · `
              + `${visibleAttentionUnits.unlinkedRegions} unlinked regions`
            : `${visible.length} reaction occurrences / ${candidates.length}`),
      ]),
      h("div", { class: "product-directory-filter-stack" }, [
        h("div", {
          class: "product-directory-filters",
          role: "group",
          "aria-label": "Document source filter",
        }, [
          h("span", null, "Source"),
          ...sourceFilters.map(([value, label]) => h("button", {
            class: state.directoryScope === value ? "on" : "",
            "aria-pressed": String(state.directoryScope === value),
            onClick: () => setDirectoryScope(value),
          }, `${label} ${candidates.filter((row) => value === "all"
            || rowDocumentSource(model, row) === value).length}`)),
        ]),
        h("div", {
          class: "product-directory-filters",
          role: "group",
          "aria-label": "Evidence type filter",
        }, [
          h("span", null, "Evidence"),
          ...evidenceFilters.map(([value, label]) => h("button", {
            class: settings.evidence === value ? "on" : "",
            "aria-pressed": String(settings.evidence === value),
            onClick: () => updateActiveDirectoryState("evidence", value),
          }, `${label} ${sourceScoped.filter((row) =>
            rowMatchesProductEvidence(row, value)
            && rowMatchesProductReportability(row, settings.reportability)).length}`)),
        ]),
        h("div", {
          class: "product-directory-filters",
          role: "group",
          "aria-label": "Reportability context filter",
        }, [
          h("span", null, "Context"),
          ...contextFilters.map(([value, label]) => h("button", {
            class: settings.reportability === value ? "on" : "",
            "aria-pressed": String(settings.reportability === value),
            onClick: () => updateActiveDirectoryState("reportability", value),
          }, `${label} ${evidenceScoped.filter((row) =>
            rowMatchesProductReportability(row, value)).length}`)),
        ]),
      ]),
      attentionOnly ? h("p", { class: "directory-note" },
        "Counts distinguish affected reaction rows, individual condition/metric claims, and unlinked layout regions. Informational derived/text evidence does not inflate this queue.") : null,
      groupEntries.length ? h("div", { class: "anchor-collapse-actions" }, [
        h("button", {
          disabled: Boolean(query),
          onClick: () => setVisibleGroupsCollapsed(false),
        }, "Expand visible"),
        h("button", {
          disabled: Boolean(query),
          onClick: () => setVisibleGroupsCollapsed(true),
        }, "Collapse visible"),
        query
          ? h("span", null, "Search temporarily expands matching groups")
          : collapsed.size
          ? h("span", null, `${groupEntries.filter(([key]) =>
              collapsed.has(groupStateKey(key))).length} collapsed`) : null,
      ]) : null,
      ...groupEntries.map(([groupKey, groupRecord], groupIndex) => {
        const {
          sourceDocument, anchorIndex, label: anchor, page, pages, rows: group,
        } = groupRecord;
        const groupKeyValue = groupStateKey(groupKey);
        const isCollapsed = !query && collapsed.has(groupKeyValue);
        const contentId = `anchor-reactions-${attentionOnly ? "attention" : "all"}-${groupIndex}`;
        const contexts = contextSummaryForRows(group);
        return h("section", {
          class: `anchor-group${isCollapsed ? " is-collapsed" : ""}`,
          "data-collapsed": String(isCollapsed),
        }, [
          h("header", null, h("button", {
            class: "anchor-group-toggle",
            "aria-expanded": String(!isCollapsed),
            "aria-controls": contentId,
            disabled: Boolean(query),
            title: query ? "Search temporarily expands matching groups" : null,
            onClick: () => toggleGroup(groupKey),
          }, [
            h("span", { class: "anchor-group-title" }, [
              h("b", null, anchor),
              h("span", null, anchorIndex == null
                ? `${sourceDocument === "si" ? "Supporting information" : "Main paper"} · ${
                  pages.size
                } page${pages.size === 1 ? "" : "s"}`
                : `${sourceDocument === "si" ? "Supporting information" : "Main paper"} · page ${page || "?"}`),
              h("span", { class: "anchor-context-summary" },
                contexts.map((context) => h("span", {
                  class: `context-flag context-${context.className}`,
                  title: context.label,
                }, `${context.short} ${context.count}`))),
            ]),
            h("span", { class: "anchor-group-count" }, [
              anchorIndex == null
                ? `${group.length} unlinked region${group.length === 1 ? "" : "s"}`
                : `${group.length} occurrence${group.length === 1 ? "" : "s"}`,
              h("i", { "aria-hidden": "true" }, isCollapsed ? "▸" : "▾"),
            ]),
          ])),
          !isCollapsed ? h("div", { class: "anchor-reactions", id: contentId },
            group.map((row) => renderProductReactionButton(row))) : null,
        ]);
      }),
      !visible.length ? (attentionOnly && candidates.length === 0
        ? h("section", { class: "attention-clear-state" }, [
            h("header", null, [
              h("span", { class: "attention-clear-mark", "aria-hidden": "true" }, "✓"),
              h("div", null, [
                h("h2", null, "No substantive attention items recorded"),
                h("p", null,
                  "Persisted evidence checks did not raise a review-level warning. Informational derived or unlocated occurrences remain visible in the census; this is not independent proof that every reaction is correct or complete."),
              ]),
            ]),
            h("div", { class: "attention-clear-actions" }, [
              h("button", { onClick: () => {
                state.layer = "region";
                setFocus("current-anchor");
              } }, "Open source regions"),
              h("button", { onClick: () => openWorkspaceModule("reactions") },
                `Browse ${rows.filter((row) => row.source.pred).length} reactions`),
              h("button", { onClick: () => openWorkspaceModule("route") }, "Open document map"),
            ]),
            h("div", { class: "attention-clear-preview" }, [
              h("b", null, "Extracted reaction preview"),
              ...rows.filter((row) => row.source.pred).slice(0, 4)
                .map((row) => renderProductReactionButton(row)),
            ]),
          ])
        : h("div", { class: "empty-directory" }, "No review items match this search.")) : null,
    ]);
  }

  function productAnchorSummaries() {
    const visibleRows = productDirectoryRows(false);
    const visibleByAnchor = new Map();
    for (const row of visibleRows) {
      const anchor = row.source.pred?.anchor;
      if (!Number.isInteger(anchor)) continue;
      if (!visibleByAnchor.has(anchor)) visibleByAnchor.set(anchor, []);
      visibleByAnchor.get(anchor).push(row);
    }
    return (model.view?.anchors || []).map((anchor, index) => {
      const anchorIndex = Number.isInteger(anchor.index) ? anchor.index : index;
      const anchorRows = rowsByAnchor.get(anchorIndex) || [];
      return {
        ...anchor,
        index: anchorIndex,
        label: anchor.label || `Anchor ${index + 1}`,
        count: anchorRows.length,
        visibleCount: visibleByAnchor.get(anchorIndex)?.length || 0,
        boxes: anchorBoxCount(anchorIndex),
        contexts: contextSummaryForRows(visibleByAnchor.get(anchorIndex) || []),
      };
    }).filter((anchor) =>
      anchor.page && Array.isArray(anchor.bbox) && anchor.bbox.length === 4
      && anchor.count > 0 && anchor.visibleCount > 0);
  }

  function renderRouteWorkspace() {
    const anchors = productAnchorSummaries();
    return h("section", {
      class: "pdf-layer product-data-workspace route-workspace",
      "data-layer": "route",
    }, [
      h("header", { class: "data-workspace-heading" }, [
        h("div", null, [
          h("span", { class: "eyebrow" }, "Document map"),
          h("h2", null, "Extracted anchors"),
          h("p", null, "Move through Main and SI by extracted scheme, figure, or table. The route follows the active source, evidence, context, and search filters."),
        ]),
        h("button", { onClick: () => openWorkspaceModule("reactions") }, "Open reaction list"),
      ]),
      renderProductFilterContext(
        anchors.reduce((total, anchor) => total + anchor.visibleCount, 0),
        rows.filter((row) => row.source.pred).length,
      ),
      h("div", { class: "route-workspace-grid" }, anchors.map((anchor, index) => {
        const thumbnail = anchorThumbnailModel(anchor, anchor.index);
        const source = documentSource(anchor);
        return h("button", {
          class: `route-node ${model.initialAnchor === anchor.index ? "initial" : ""}`,
          "aria-label": `${source === "si" ? "Supporting information" : "Main paper"}, ${anchor.label}, page ${anchor.page}, ${anchor.visibleCount} matching of ${anchor.count} reaction occurrences, ${anchor.boxes} boxes`,
          onClick: () => selectAnchor(anchor, anchor.index),
        }, [
          h("span", { class: "anchor-thumb" }, [
            h(BBoxImagePanel, {
              imageUrl: `/api/jobs/${encodeURIComponent(model.jobId)}/pdf/anchor/${anchor.index}/crop?dpi=96`,
              coordinateSize: thumbnail.size,
              boxes: thumbnail.boxes,
              alt: `${source === "si" ? "Supporting information" : "Main paper"} ${anchor.label} extraction crop`,
              compact: true,
            }),
            h("i", null, `${anchor.boxes} boxes`),
          ]),
          h("span", { class: "route-index" }, String(index + 1).padStart(2, "0")),
          h("span", { class: `route-source route-source-${source}` },
            source === "si" ? "Supporting information" : "Main paper"),
          h("b", null, anchor.label),
          h("small", null, `p${anchor.page} · ${anchor.visibleCount}/${anchor.count} matching occurrences · ${anchor.boxes} boxes`),
          h("span", { class: "route-contexts" }, anchor.contexts.map((context) =>
            h("span", {
              class: `context-flag context-${context.className}`,
              title: context.label,
            }, `${context.short} ${context.count}`))),
        ]);
      })),
      !anchors.length ? h("p", { class: "empty-directory" }, "No extracted anchors are available.") : null,
    ]);
  }

  function renderTableWorkspace() {
    const settings = activeDirectoryState();
    const visible = productDirectoryRows(false);
    return h("section", {
      class: "pdf-layer product-data-workspace table-workspace",
      "data-layer": "table",
    }, [
      h("header", { class: "data-workspace-heading" }, [
        h("div", null, [
          h("span", { class: "eyebrow" }, "Structured result"),
          h("h2", null, "Reaction table"),
          h("p", null, "Every row stays linked to the same reaction and source evidence used by the visual reviewer."),
        ]),
        h("label", { class: "product-search" }, [
          h("span", null, "Search table"),
          h("input", {
            value: settings.query,
            placeholder: "Identifier, condition, anchor, or page",
            onInput: (event) => updateActiveDirectoryState(
              "query", event.target.value.slice(0, 320),
            ),
          }),
        ]),
      ]),
      renderProductFilterContext(
        visible.length,
        rows.filter((row) => row.source.pred).length,
      ),
      h("div", { class: "workspace-table-wrap" }, [
        h("table", null, [
          h("thead", null, h("tr", null, [
            h("th", { scope: "col" }, "Source"),
            h("th", { scope: "col" }, "Context"),
            h("th", { scope: "col" }, "Reaction"),
            h("th", { scope: "col" }, "Conditions"),
            h("th", { scope: "col" }, "Evidence"),
            h("th", { scope: "col" }, "Review"),
          ])),
          h("tbody", null, visible.map((row) => {
            const molecules = [...(row.evidence?.reactants || []), ...(row.evidence?.products || [])];
            const identifiers = molecules.map((item) => item.identifier).filter(Boolean);
            const conditions = conditionTexts(row.evidence?.conditions);
            const source = rowDocumentSource(model, row);
            const context = reportabilityMeta(row.evidence?.review_context);
            const evidence = rowEvidenceSummary(row);
            const select = () => selectRow(row);
            return h("tr", {
              class: state.selectedId === row.id ? "selected" : "",
              onClick: select,
            }, [
              h("td", { "data-label": "Source" }, h("button", {
                class: "table-row-target",
                "aria-label": `Locate ${source === "si" ? "Supporting information" : "Main paper"}, ${row.source.pred?.anchor_label || "Unanchored"}, page ${rowPage(row) || "?"}, ${row.label} on PDF`,
                onClick: (event) => {
                  event.stopPropagation();
                  selectRow(row, { revealSource: true });
                },
              }, [
                h("b", null, row.source.pred?.anchor_label || "Unanchored"),
                h("small", null,
                  `${source === "si" ? "Supporting information" : "Main paper"} · p${rowPage(row) || "?"} · ${row.label}`),
              ])),
              h("td", { "data-label": "Context" }, h("span", {
                class: `context-flag context-${context.className}`,
                title: context.label,
              }, context.short)),
              h("td", { "data-label": "Reaction" },
                identifiers.join(" → ") || "Unlabelled reaction"),
              h("td", { "data-label": "Conditions" },
                conditions.join(" · ") || "No condition text"),
              h("td", { "data-label": "Evidence" }, [
                h("span", { class: `evidence-${evidence.status}` },
                  evidence.label),
              ]),
              h("td", { "data-label": "Review" }, state.adjudications[row.id]
                ? "reviewed" : row.productReasons?.join(" · ") || "no flagged risk"),
            ]);
          })),
        ]),
      ]),
      !visible.length ? h("p", { class: "empty-directory" }, "No reaction rows match this search.") : null,
    ]);
  }

  function substanceRecords() {
    const records = new Map();
    for (const row of rows) {
      for (const [role, molecules] of [
        ["reactant", row.evidence?.reactants || []],
        ["product", row.evidence?.products || []],
      ]) {
        for (const [moleculeIndex, molecule] of molecules.entries()) {
          const occurrenceId = molecule.occurrence?.occurrence_id
            || `${rowDocumentSource(model, row)}:${row.id}:${role}:${moleculeIndex}`;
          const rawSmiles = moleculeSmiles(molecule);
          const reviewedSmiles = reviewedMoleculeFor(row, molecule);
          const identityChanged = Boolean(reviewedSmiles && reviewedSmiles !== rawSmiles);
          // Human edits are deliberately isolated until the backend computes a
          // fresh chemistry identity for the reviewed projection. Keeping the
          // stale raw key would silently leave the occurrence in the wrong group.
          const key = identityChanged
            ? `reviewed-pending:${occurrenceId}`
            : molecule.entity_key || `occurrence:${occurrenceId}`;
          const current = records.get(key) || {
            key,
            entityKey: key,
            identity: molecule.entity_identity || null,
            identifiers: new Set(),
            descriptions: new Set(),
            aliases: new Set(),
            smiles: reviewedSmiles || rawSmiles || "",
            identityChanged,
            count: 0,
            sources: new Set(),
            sourceDocuments: new Set(),
            reactions: new Map(),
            occurrences: [],
          };
          if (molecule.identifier) current.identifiers.add(molecule.identifier);
          if (molecule.description) current.descriptions.add(molecule.description);
          for (const alias of molecule.aliases || []) {
            if (alias) current.aliases.add(alias);
          }
          if (molecule.identifier) current.aliases.add(molecule.identifier);
          if (molecule.description) current.aliases.add(molecule.description);
          current.count += 1;
          if (molecule.source?.class) current.sources.add(molecule.source.class);
          current.sourceDocuments.add(rowDocumentSource(model, row));
          current.reactions.set(row.id, row);
          current.occurrences.push({
            occurrenceId, row, molecule, role, moleculeIndex,
            sourceDocument: rowDocumentSource(model, row),
          });
          records.set(key, current);
        }
      }
    }
    return [...records.values()].sort((left, right) =>
      right.count - left.count
      || ([...left.identifiers][0] || left.smiles || left.key)
        .localeCompare([...right.identifiers][0] || right.smiles || right.key));
  }

  function moleculeEntityLabel(record) {
    return [...record.identifiers][0]
      || [...record.descriptions][0]
      || (record.identityChanged ? "Edited structure" : "Unnamed structure");
  }

  function selectMoleculeEntity(record, options = {}) {
    if (!record) return;
    state.selectedEntityKey = record.entityKey;
    state.selectedEntityCandidates = [];
    state.selectedEntityOccurrenceIds = record.occurrences.map((item) => item.occurrenceId);
    state.layer = "molecules";
    const current = [...record.reactions.values()].find((row) => row.id === state.selectedId);
    const occurrenceMatch = options.occurrenceIds?.length
      ? record.occurrences.find((item) => options.occurrenceIds.includes(item.occurrenceId))?.row
      : null;
    const reaction = occurrenceMatch || current || record.reactions.values().next().value || null;
    if (reaction) {
      selectRow(reaction, { preserveMoleculeEntity: true });
    } else {
      window.dispatchEvent(new CustomEvent("pdf-review-select", {
        detail: {
          jobId: model.jobId,
          preserveMoleculeEntity: true,
          moleculeEntityKey: record.entityKey,
          moleculeEntityOccurrenceIds: state.selectedEntityOccurrenceIds,
        },
      }));
    }
  }

  function renderMoleculeEntity(record) {
    const reactions = [...record.reactions.values()];
    const representative = record.occurrences.find((item) => moleculeSmiles(item.molecule))
      || record.occurrences[0];
    const identity = record.identity || {};
    return h("div", { class: "molecule-entity-review" }, [
      h("header", { class: "molecule-entity-heading" }, [
        h("div", null, [
          h("span", { class: "eyebrow" }, "Chemistry entity"),
          h("h2", null, moleculeEntityLabel(record)),
          h("p", null, [
            `${record.occurrences.length} occurrence${record.occurrences.length === 1 ? "" : "s"} · `,
            `${reactions.length} reaction${reactions.length === 1 ? "" : "s"} · `,
            [...record.sourceDocuments].map((source) =>
              source === "si" ? "Supporting information" : "Main paper").join(" + "),
          ]),
        ]),
        h("button", {
          class: "entity-back",
          onClick: () => {
            state.selectedEntityKey = null;
            state.selectedEntityOccurrenceIds = [];
            state.selectedEntityCandidates = [];
            window.dispatchEvent(new CustomEvent("pdf-review-select", {
              detail: { jobId: model.jobId, preserveView: true },
            }));
          },
        }, "All substances"),
      ]),
      h("div", { class: "molecule-entity-summary" }, [
        h("div", { class: "molecule-entity-structure" }, [
          representative
            ? renderMolecule(
                representative.molecule,
                null,
                `entity-${record.entityKey}`,
                false,
                representative.row,
                representative.role,
                representative.moleculeIndex,
              )
            : null,
        ]),
        h("dl", { class: "molecule-entity-facts" }, [
          h("dt", null, "Identity basis"),
          h("dd", null, identity.basis || "isolated occurrence (identity unavailable)"),
          h("dt", null, "Identity comparison"),
          h("dd", null, identity.stereo_sensitive === false
            ? "connectivity only" : "stereo-sensitive when encoded"),
          h("dt", null, "Body stereo encoding"),
          h("dd", null, identity.stereo_encoded
            ? "present in serialized structure" : "not specified in serialized structure"),
          h("dt", null, "Canonical structure"),
          h("dd", null, identity.canonical_isomeric_smiles || record.smiles || "unavailable"),
          h("dt", null, "Identifiers / aliases"),
          h("dd", null, [...record.aliases].join(" · ") || "none recorded"),
          h("dt", null, "Source classes"),
          h("dd", null, [...record.sources].join(" · ") || "provenance unavailable"),
          record.identityChanged ? h("dt", null, "Regrouping") : null,
          record.identityChanged
            ? h("dd", { class: "entity-identity-pending" },
                "Edited occurrence is isolated until reviewed identity is recomputed.")
            : null,
        ]),
      ]),
      h("section", { class: "entity-reaction-list" }, [
        h("header", null, [
          h("b", null, "Participating reactions"),
          h("span", null, `${reactions.length} occurrence${reactions.length === 1 ? "" : "s"}`),
        ]),
        ...reactions.map((row) => renderProductReactionButton(row, false, (selectedRow) =>
          selectRow(selectedRow, { preserveMoleculeEntity: true }))),
      ]),
      selected() && record.reactions.has(state.selectedId)
        ? h("div", { class: "molecule-entity-detail" }, [
            h("div", { class: "entity-detail-label" }, [
              h("span", { class: "eyebrow" }, "Selected reaction occurrence"),
              h("b", null, selected().label),
            ]),
            renderInspector(),
          ])
        : null,
    ]);
  }

  function renderMoleculeWorkspace() {
    const query = state.query.trim().toLowerCase();
    const allRecords = substanceRecords();
    const selectedRecord = allRecords.find((record) => record.entityKey === state.selectedEntityKey);
    const records = allRecords.filter((record) =>
      !query || `${[...record.identifiers].join(" ")} ${[...record.aliases].join(" ")} ${record.smiles}`
        .toLowerCase().includes(query));
    return h("section", {
      class: "pdf-layer product-data-workspace molecule-workspace",
      "data-layer": "molecules",
    }, [
      selectedRecord ? renderMoleculeEntity(selectedRecord) : null,
      !selectedRecord && state.selectedEntityCandidates.length
        ? h("section", { class: "entity-ambiguity", role: "alert" }, [
            h("span", { class: "eyebrow" }, "Shared source box"),
            h("h2", null, "Choose the chemistry entity"),
            h("p", null,
              "This physical detection is referenced by more than one structure identity. They were not merged automatically."),
            h("div", { class: "molecule-library-grid" },
              state.selectedEntityCandidates.map((key) => {
                const record = allRecords.find((item) => item.entityKey === key);
                return record ? h("button", { onClick: () => selectMoleculeEntity(record) }, [
                  h("span", null, [
                    h("b", null, moleculeEntityLabel(record)),
                    h("small", null, record.smiles || "No serialized structure"),
                  ]),
                  h("span", null, [
                    h("strong", null, String(record.count)),
                    h("small", null, `${record.reactions.size} reactions`),
                  ]),
                ]) : null;
              })),
          ])
        : null,
      selectedRecord ? null : h(Fragment, null, [
      h("header", { class: "data-workspace-heading" }, [
        h("div", null, [
          h("span", { class: "eyebrow" }, "Substance library"),
          h("h2", null, `${records.length} chemistry identities`),
          h("p", null,
            "Identity groups use strict-stereo canonical SMILES or exact full E-SMILES wire. Every Main/SI occurrence and review state remains separate."),
        ]),
        h("label", { class: "product-search" }, [
          h("span", null, "Search molecules"),
          h("input", {
            value: state.query,
            placeholder: "Identifier or SMILES",
            onInput: (event) => { state.query = event.target.value; },
          }),
        ]),
      ]),
      h("div", { class: "molecule-library-grid" }, records.map((record) => h("button", {
        class: state.selectedEntityKey === record.entityKey ? "selected" : "",
        onClick: () => selectMoleculeEntity(record),
      }, [
        h("span", null, [
          h("b", null, moleculeEntityLabel(record)),
          h("small", null, record.smiles || "No serialized structure"),
        ]),
        h("span", null, [
          h("strong", null, String(record.count)),
          h("small", null, `${record.reactions.size} reaction${record.reactions.size === 1 ? "" : "s"}`),
        ]),
        h("em", null, [
          [...record.sourceDocuments].map((source) => source === "si" ? "SI" : "Main").join(" + "),
          " · ",
          [...record.sources].join(" · ") || "provenance unavailable",
        ]),
      ]))),
      !records.length ? h("p", { class: "empty-directory" }, "No molecules match this search.") : null,
      ]),
    ]);
  }

  function reviewedMoleculeFor(row, item) {
    const saved = state.adjudications[row?.id]?.correction?.molecule;
    const draft = state.selectedId === row?.id ? state.draftMoleculeCorrection : null;
    const correction = draft?.occurrence_id === item?.occurrence?.occurrence_id
      ? draft
      : saved?.occurrence_id === item?.occurrence?.occurrence_id ? saved : null;
    return correction?.esmiles || item?.smi || item?.["E-SMILES"] || item?.smiles || "";
  }

  function stageReviewedMolecule(row, payload) {
    const occurrence = payload?.occurrence;
    if (!row || !occurrence || occurrence.reaction_id !== row.id) {
      throw new Error("Ketcher edit target no longer matches the selected reaction");
    }
    proposalRun += 1;
    state.saving = false;
    discardCurrentAgentProposal("human_edit_replaced");
    state.draftMoleculeCorrection = {
      ...occurrence,
      esmiles: payload.esmiles,
    };
    state.draftSmiles = "";
    state.draftVerdict = "correct fields";
    state.proposedPatch = null;
    state.layer = "edit";
    state.message = "Full E-SMILES staged for one occurrence. Review changes before Apply.";
  }

  function renderMolecule(
    item,
    comparison,
    key,
    reference = false,
    row = null,
    role = "",
    moleculeIndex = 0,
  ) {
    if (!item) return null;
    const smiles = reference ? (item.smi || item["E-SMILES"] || item.smiles || "")
      : reviewedMoleculeFor(row, item);
    const editable = productMode && !reference && Boolean(item.occurrence);
    return h(MoleculeCard, {
      key,
      data: moleculeData({ ...item, smi: smiles }),
      comparisonData: moleculeData(comparison),
      rawDetection: rawDetection(item),
      provenanceLabel: reference ? "GT ref" : item.source?.compact || "untracked",
      provenanceTitle: reference
        ? "GT reference provenance unavailable"
        : item.source?.label || "untracked source",
      readOnly: true,
      compact: true,
      strictDerivation: true,
      // Larger structures in the detail lane -- the right rail is the primary info surface.
      renderSrc: `/api/render?smi=${encodeURIComponent(smiles)}&w=168&h=126`,
      deferChemCompare: true,
      allowKetcher: editable,
      editTarget: editable ? item.occurrence : null,
      onReviewEsmiles: (payload) => stageReviewedMolecule(row, payload),
      "data-role": role,
      "data-molecule-index": moleculeIndex,
    });
  }

  function renderReactionLane(label, molecules, comparisons, row) {
    const reactants = molecules?.reactants || [];
    const products = molecules?.products || [];
    const compareReactants = comparisons?.reactants || [];
    const compareProducts = comparisons?.products || [];
    return h("div", { class: "evidence-lane" }, [
      h("div", { class: "lane-label" }, label),
      h("div", { class: "lane-molecules" }, [
        ...reactants.map((molecule, index) =>
          renderMolecule(
            molecule, compareReactants[index], `${label}-r-${index}`,
            label === "GT", row, "reactant", index,
          )),
        h("span", { class: "lane-arrow" }, "to"),
        ...products.map((molecule, index) =>
          renderMolecule(
            molecule, compareProducts[index], `${label}-p-${index}`,
            label === "GT", row, "product", index,
          )),
      ]),
    ]);
  }

  function renderLineage(row) {
    const molecules = row.evidence
      ? [...row.evidence.reactants, ...row.evidence.products] : [];
    if (!molecules.length) return h("div", { class: "empty-lineage" }, "No prediction lineage for this row.");
    return h("div", { class: "lineage-list" }, molecules.map((molecule) => {
      const raw = molecule.raw_molparser;
      const derivationStatus = molecule.source?.derivation_status || {};
      let closureAuditText = null;
      let auditScopeText = null;
      if (derivationStatus.audit_status === "unverified_no_row_level_fhr_proof") {
        closureAuditText = "source reports roundtrip · unverified candidate";
        auditScopeText = "no row-level FHR proof";
      } else if (derivationStatus.audit_status === "counterparty_stereo_mismatch_unverified") {
        closureAuditText = "audit row candidate matches; reaction product stereo does not";
        auditScopeText = "handoff inventory · unverified for this reaction";
      } else if (derivationStatus.audit_status === "handoff_inventory_match") {
        closureAuditText = "roundtrip matched · handoff inventory audit";
        auditScopeText = "handoff inventory · not upstream extraction seal";
      } else if (derivationStatus.audit_status === "handoff_inventory_mismatch") {
        closureAuditText = "handoff audit mismatch · candidate, not confirmed";
        auditScopeText = "handoff inventory · not upstream extraction seal";
      } else if (Object.hasOwn(derivationStatus, "roundtrip_ok")) {
        closureAuditText = derivationStatus.roundtrip_ok
          ? "roundtrip recorded · audit scope unavailable"
          : "diagnostic mismatch · candidate, not confirmed";
      }
      const gtMolecules = [...(row.source.gt?.reactants || []), ...(row.source.gt?.products || [])];
      const gt = gtMolecules.find((item) => item.identifier === molecule.identifier)
        || gtMolecules.find((item) => item.canon && item.canon === molecule.smi);
      const rawRelation = !raw ? "raw unavailable"
        : ["equals_pred", "equivalent_to_pred"].includes(molecule.raw_relation) ? "raw equals Pi"
          : gt && raw.canon && raw.canon === gt.canon ? "raw equals GT"
            : "raw differs from Pi and GT";
      const textEvidence = molecule.text_quote
        || row.source.pred?.metrics?.map((metric) => metric.text).filter(Boolean).join("; ")
        || row.source.pred?.conditions?.join("; ")
        || "no metric/text evidence";
      const unavailable = !raw && ["untracked", "image_untraced"].includes(molecule.source?.class);
      const sourceStep = molecule.source?.class === "derived"
        ? raw ? `derived · raw support via ${raw.joined_by}` : "derived structure evidence"
        : raw ? `raw MP via ${raw.joined_by}`
          : "no raw OCSR";
      const derivationMethod = String(molecule.source?.derivation_method || "").trim();
      const derivationLabel = derivationMethod && derivationMethod !== "untracked"
        ? derivationMethod
        : raw
          ? molecule.source?.class === "identifier_resolution"
            ? "identifier resolution with raw support"
            : "direct MolParser observation"
          : "untracked";
      return h("article", { class: `lineage-chain ${unavailable ? "lineage-muted" : ""}` }, [
        h("div", { class: "lineage-flow" }, [
          h("span", null, `Pred ${molecule.identifier || "unnamed"}`),
          h("span", null, molecule.source?.compact || "untracked"),
          h("span", null, sourceStep),
          h("span", null, gt ? `GT ${gt.identifier || "molecule"}` : "no GT molecule"),
          h("span", null, textEvidence),
          h("span", null, row.claims[0]?.label || "no candidate cause"),
        ]),
        unavailable
          ? h("p", { class: "lineage-unavailable" },
              molecule.source?.origin === "image"
                ? "No raw OCSR join; inspect the source warning."
                : "No auditable source trace is available for this prediction molecule.")
          : h("dl", null, [
          h("dt", null, "Derivation"), h("dd", null, derivationLabel),
          closureAuditText ? h("dt", null, "Closure audit") : null,
          closureAuditText ? h("dd", null, closureAuditText) : null,
          auditScopeText ? h("dt", null, "Audit scope") : null,
          auditScopeText ? h("dd", null, auditScopeText) : null,
          derivationStatus.audit_file ? h("dt", null, "Audit artifact") : null,
          derivationStatus.audit_file ? h("dd", null, derivationStatus.audit_file) : null,
          derivationStatus.audit_target_sha256 ? h("dt", null, "Audit target") : null,
          derivationStatus.audit_target_sha256 ? h("dd", null,
            derivationStatus.audit_target_sha256.slice(0, 16)) : null,
          derivationStatus.current_reaction_target_sha256
            ? h("dt", null, "Current reaction target") : null,
          derivationStatus.current_reaction_target_sha256 ? h("dd", null,
            derivationStatus.current_reaction_target_sha256.slice(0, 16)) : null,
          derivationStatus.supporting_document ? h("dt", null, "Supporting source") : null,
          derivationStatus.supporting_document ? h("dd", null,
            `${derivationStatus.supporting_document === "si" ? "Supporting information" : "Main paper"}${
              derivationStatus.supporting_page ? ` · p${derivationStatus.supporting_page}` : ""
            }`) : null,
          h("dt", null, "Raw relation"), h("dd", null, rawRelation),
          h("dt", null, "Raw smi"), h("dd", null, raw?.smi || "not recorded"),
          h("dt", null, "Raw caption"), h("dd", null, raw?.caption || "not recorded"),
          h("dt", null, "Raw esmi"), h("dd", null, raw?.esmi || "not recorded"),
          h("dt", null, "Trace key"), h("dd", null,
            raw ? `${raw.joined_by}: ${raw.md5 || raw.bbox_index}` : "untracked"),
          raw?.namespace ? h("dt", null, "Structure source") : null,
          raw?.namespace ? h("dd", null, `${raw.namespace} ${raw.owner}`) : null,
          molecule.geometry_source ? h("dt", null, "Geometry source") : null,
          molecule.geometry_source ? h("dd", null, molecule.geometry_source) : null,
          molecule.source?.join_warning ? h("dt", null, "Join warning") : null,
          molecule.source?.join_warning ? h("dd", null, molecule.source.join_warning) : null,
          molecule.source?.structure_evidence ? h("dt", null, "Structure evidence") : null,
          molecule.source?.structure_evidence
            ? h("dd", null, JSON.stringify(molecule.source.structure_evidence)) : null,
        ]),
      ]);
    }));
  }

  function renderConditionEvidence(row) {
    const claims = [];
    for (const [stepIndex, step] of (row.evidence?.conditions || []).entries()) {
      for (const [conditionIndex, raw] of (Array.isArray(step) ? step : [step]).entries()) {
        const item = typeof raw === "string" ? { text: raw } : raw || {};
        const source = {
          ...(item.source && typeof item.source === "object" ? item.source : {}),
          ...(item._source && typeof item._source === "object" ? item._source : {}),
        };
        const text = item.text || item.description || item["E-SMILES"] || "";
        const conditionEsmiles = typeof item["E-SMILES"] === "string"
          ? item["E-SMILES"].trim() : "";
        const textPage = Number(source.text_page);
        const sourceDocument = explicitDocumentSource(source);
        const textLocation = Number.isSafeInteger(textPage) && textPage > 0
          ? validateWorkspaceLocation({ documentSource: sourceDocument, page: textPage })
          : { location: null, error: "" };
        claims.push({
          key: item.claim_id || `${stepIndex}:${conditionIndex}`,
          targetId: `${row.id}:conditions:${stepIndex}:${conditionIndex}`,
          role: item.role || "condition",
          text,
          sourceClass: source.type || source.class || source.origin || "source unavailable",
          sourceDocument,
          page: source.bbox_page || source.page || null,
          textPage: Number.isSafeInteger(textPage) && textPage > 0 ? textPage : null,
          textLocationAvailable: Boolean(textLocation.location && !textLocation.error),
          bbox: hasValidEvidenceBbox(source.bbox) ? source.bbox : null,
          quote: item.text_quote || source.text_quote || "",
          groundingMissing: imageClaimMissingBbox(source),
          structure: conditionEsmiles ? {
            identifier: source.alias || item.identifier || item.text || item.role || "condition structure",
            esmiles: conditionEsmiles,
            sourceType: source.type || source.class || source.origin || "source unavailable",
          } : null,
        });
      }
    }
    return h("div", { class: "condition-evidence" }, [
      h("b", null, "Prediction conditions"),
      ...(claims.length ? claims.map((claim) => h("article", {
        key: claim.key,
        class: `condition-evidence-claim${claim.groundingMissing ? " claim-grounding-missing" : ""}`,
      }, [
        h("div", null, [
          h("span", { class: "metric-role" }, claim.role),
          h("strong", null, claim.text || "empty condition"),
        ]),
        h("small", null, [
          claim.sourceDocument === "si" ? "SI"
            : claim.sourceDocument === "main" ? "Main" : "Document unavailable",
          claim.page ? ` · image p${claim.page}` : "",
          claim.textPage ? ` · text p${claim.textPage}` : "",
          ` · ${claim.sourceClass}`,
          claim.bbox ? ` · bbox ${claim.bbox.map((value) => Number(value).toFixed(1)).join(" · ")}` : "",
          claim.groundingMissing
            ? h("span", { class: "claim-warning-badge" }, " · PDF bbox missing") : null,
          state.geometryCorrections[claim.targetId]
            ? h("span", { class: "reviewed-geometry-badge" },
                ` · reviewed location attached (${formatGeometryLocation(
                  state.geometryCorrections[claim.targetId].geometry,
                )})`)
            : null,
        ]),
        claim.quote ? h("q", null, claim.quote) : null,
        claim.structure ? h("div", {
          class: "condition-evidence-structure",
          "data-condition-structure": claim.structure.identifier,
        }, [
          h(MoleculeCard, {
            data: {
              identifier: claim.structure.identifier,
              "E-SMILES": claim.structure.esmiles,
              description: `${claim.role} condition structure`,
              _source: { origin: claim.structure.sourceType },
            },
            readOnly: true,
            compact: true,
            strictDerivation: true,
            provenanceLabel: "condition",
            provenanceTitle: claim.structure.sourceType,
            renderSrc: `/api/render?smi=${encodeURIComponent(claim.structure.esmiles)}&w=168&h=126`,
            deferChemCompare: true,
          }),
          h("code", { class: "condition-structure-esmiles" }, claim.structure.esmiles),
        ]) : null,
        claim.textPage && claim.textLocationAvailable
          ? h("button", {
            class: "condition-evidence-locate",
            onClick: () => {
              window.dispatchEvent(new CustomEvent("pdf-review-geometry-navigate", {
                detail: {
                  jobId: model.jobId,
                  sourceDocument: claim.sourceDocument,
                  page: Number(claim.textPage),
                },
              }));
              state.message = `Showing ${claim.sourceDocument === "si" ? "SI" : "Main"} page ${
                claim.textPage
              } for ${claim.role} text evidence.`;
            },
          }, `Locate text · p${claim.textPage}`)
          : null,
      ])) : [h("p", null, "none")]),
    ]);
  }

  function renderMetrics(label, metrics, diagnostic, row = null) {
    const claims = (metrics || []).map((metric, metricIndex) => ({
      targetId: row ? `${row.id}:metrics:${metricIndex}` : null,
      role: metric.role || metric.type || "metric",
      value: metric.text || metric.content || metric.value || "",
      of: metric.of || metric.for_product || "",
      source: {
        ...(metric.source && typeof metric.source === "object" ? metric.source : {}),
        ...(metric._source && typeof metric._source === "object" ? metric._source : {}),
      },
    })).map((claim) => ({
      ...claim,
      groundingMissing: imageClaimMissingBbox(claim.source),
    }));
    return h("div", { class: "metric-claims" }, [
      h("b", null, label),
      ...(claims.length ? claims.map((claim) => h("div", {
        class: `metric-claim${claim.groundingMissing ? " claim-grounding-missing" : ""}`,
      }, [
        h("span", { class: "metric-role" }, claim.role),
        h("span", { class: "metric-value" }, claim.value),
        claim.of ? h("span", { class: "metric-of" }, `of ${claim.of}`) : null,
        Object.keys(claim.source).length ? h("small", { class: "metric-source" }, [
          explicitDocumentSource(claim.source) === "si" ? "SI"
            : explicitDocumentSource(claim.source) === "main" ? "Main" : "Document unavailable",
          claim.source.bbox_page || claim.source.page
            ? ` · p${claim.source.bbox_page || claim.source.page}` : "",
          ` · ${claim.source.type || claim.source.class || claim.source.origin || "source"}`,
          claim.groundingMissing
            ? h("span", { class: "claim-warning-badge" }, " · PDF bbox missing") : null,
          claim.targetId && state.geometryCorrections[claim.targetId]
            ? h("span", { class: "reviewed-geometry-badge" },
                ` · reviewed location attached (${formatGeometryLocation(
                  state.geometryCorrections[claim.targetId].geometry,
                )})`)
            : null,
        ]) : null,
      ])) : [h("p", null, "none")]),
      diagnostic ? h("small", { class: diagnostic.status === "diff" ? "diag-diff" : "diag-info" },
        diagnostic.status === "diff"
          ? `${diagnostic.substantive.length} substantive condition mismatch(es)`
          : `${diagnostic.info.length} role/format/detail note(s); no substantive mismatch`) : null,
    ]);
  }

  function selectVerificationRow(row) {
    if (!row) return;
    proposalRun += 1;
    state.saving = false;
    discardCurrentAgentProposal("human_navigation");
    state.selectedId = row.id;
    state.workspaceLocation = null;
    state.layer = "verify";
    if (row.source.pred) setFocus("selected-only");
    const saved = state.adjudications[row.id] || {};
    state.draftVerdict = saved.verdict || "";
    state.draftNote = saved.note || "";
    state.draftSmiles = saved.correction?.smiles || "";
    state.draftMoleculeCorrection = saved.correction?.molecule || null;
    state.draftCondition = saved.correction?.condition_text || "";
    state.proposedPatch = null;
    dispatchSelection(row);
  }

  function verificationQueue() {
    return orderedVerificationRows(rows, state.adjudications);
  }

  function moveVerificationSelection(offset) {
    const queue = verificationQueue();
    if (!queue.length) return;
    const current = Math.max(0, queue.findIndex((row) => row.id === state.selectedId));
    selectVerificationRow(queue[Math.max(0, Math.min(queue.length - 1, current + offset))]);
  }

  function stageVerificationDecision(kind) {
    const row = selected();
    if (!row) return;
    discardCurrentAgentProposal("human_review_replaced");
    if (kind === "accept") {
      // An acceptance is not allowed to carry a correction left over from an
      // earlier draft for the same occurrence.
      state.draftSmiles = "";
      state.draftMoleculeCorrection = null;
      state.draftCondition = "";
    }
    state.draftVerdict = productMode
      ? kind === "accept" ? "accept as extracted" : "correct fields"
      : kind === "accept" ? "prediction accepted" : "extraction error";
    state.proposedPatch = null;
    state.layer = "edit";
    state.message = kind === "accept"
      ? "Acceptance staged. Review the bounded change before Apply."
      : "Correction review opened. Edit fields or open full Ketcher, then review the diff.";
  }

  function renderVerificationFact(fact) {
    return h("article", {
      class: `verify-fact status-${fact.status}`,
      "data-dimension": fact.key,
    }, [
      h("div", null, [
        h("b", null, fact.label),
        h("span", { class: `verify-status status-${fact.status}` }, fact.status),
      ]),
      h("p", null, fact.explanation),
    ]);
  }

  function renderVerifyWorkspace() {
    const queue = verificationQueue();
    let row = selected();
    if (!row || !queue.some((item) => item.id === row.id)) row = queue[0] || null;
    if (!row) {
      return h("section", {
        class: "pdf-layer verify-workspace",
        "data-layer": "verify",
      }, [
        h("h2", null, "Verify extraction"),
        h("p", null, "No reaction occurrence is available for review."),
      ]);
    }
    const index = queue.findIndex((item) => item.id === row.id);
    const facts = verificationDimensions(row, state.adjudications[row.id]);
    const predEvidence = row.evidence || {
      reactants: row.source.pred?.reactants || [],
      products: row.source.pred?.products || [],
    };
    const gt = row.source.gt || null;
    const flagged = facts.filter((fact) => ["diff", "attention"].includes(fact.status));
    const unchecked = facts.filter((fact) => fact.status === "unchecked");
    return h("section", {
      class: "pdf-layer verify-workspace",
      "data-layer": "verify",
      "data-difference-id": row.id,
    }, [
      h("header", { class: "verify-heading" }, [
        h("div", null, [
          h("span", { class: "eyebrow" }, "Human verification · source stays visible"),
          h("h2", null, row.label),
          h("p", null, `${row.source.pred?.anchor_label || "Document occurrence"} · ${
            rowDocumentSource(model, row) === "si" ? "Supporting information" : "Main paper"
          } p${rowPage(row) || "?"}`),
        ]),
        h("div", { class: "verify-progress" }, [
          h("strong", null, `${index + 1}/${queue.length}`),
          h("span", null, `${flagged.length} flagged · ${unchecked.length} unchecked`),
        ]),
      ]),
      h("div", { class: "verify-truth-banner" }, [
        h("b", null, gt
          ? "Reference-backed comparison"
          : "Prediction-only verification"),
        h("span", null, gt
          ? "Attached diagnostics are shown by dimension; human review remains separate."
          : "Unchecked means no reference-backed diagnostic is attached. It is not a pass."),
      ]),
      h("div", { class: "verify-layout" }, [
        h("div", { class: "verify-main" }, [
          h("section", { class: `evidence-side-by-side verify-reaction ${gt ? "" : "prediction-only"}` }, [
            renderReactionLane("PRED", predEvidence, gt, row),
            gt ? renderReactionLane("GT", gt, predEvidence, row)
              : h("aside", { class: "verify-source-note" }, [
                  h("b", null, "Check against the selected PDF region"),
                  h("p", null, "The source document remains pinned beside this workspace. Molecule cards are model-rendered and stay linked to the active PDF boxes."),
                ]),
          ]),
          h("div", {
            class: "verify-dimensions",
            "aria-label": "Verification dimensions",
          }, facts.map(renderVerificationFact)),
          row.claims?.length ? h("details", { class: "verify-candidates" }, [
            h("summary", null, `${row.claims.length} candidate diagnosis claim(s)`),
            h("p", null, "Agent candidates are hypotheses until a human verdict is applied."),
            ...row.claims.map((claim) => h("article", null, [
              h("b", null, claim.label),
              h("span", null, (claim.evidence || []).join("; ") || "No evidence text attached"),
            ])),
          ]) : null,
          h("div", { class: "verify-actions", role: "group", "aria-label": "Verification actions" }, [
            h("button", {
              class: "verify-accept",
              onClick: () => stageVerificationDecision("accept"),
            }, "A · Stage accept"),
            h("button", {
              class: "verify-correct",
              onClick: () => stageVerificationDecision("correct"),
            }, "X · Correct"),
            h("button", { onClick: () => { state.layer = "inspector"; } }, "Evidence"),
            h("button", { onClick: () => { state.layer = "edit"; } }, "E · Edit"),
          ]),
          h("p", { class: "verify-shortcuts" },
            "J/K move through the queue. Decisions are staged only; Apply remains explicit."),
        ]),
        h("aside", { class: "verify-queue", "aria-label": "Verification queue" }, [
          h("header", null, [
            h("b", null, "Review queue"),
            h("span", null, "risk-first"),
          ]),
          ...queue.map((item, queueIndex) => {
            const itemFacts = verificationDimensions(item, state.adjudications[item.id]);
            const itemFlags = itemFacts.filter((fact) =>
              ["diff", "attention"].includes(fact.status)).length;
            const itemUnchecked = itemFacts.filter((fact) => fact.status === "unchecked").length;
            return h("button", {
              class: item.id === row.id ? "selected" : "",
              "aria-current": item.id === row.id ? "true" : null,
              onClick: () => selectVerificationRow(item),
            }, [
              h("span", null, String(queueIndex + 1).padStart(2, "0")),
              h("span", null, [
                h("b", null, item.label),
                h("small", null, `${itemFlags} flagged · ${itemUnchecked} unchecked`),
              ]),
              state.adjudications[item.id]
                ? h("em", { class: "reviewed-flag" }, "reviewed") : null,
            ]);
          }),
        ]),
      ]),
    ]);
  }

  function renderChecksWorkspace() {
    const checks = workspaceChecks(rows);
    const flags = checks.filter((item) => item.status === "flag");
    return h("section", {
      class: "pdf-layer checks-workspace",
      "data-layer": "checks",
    }, [
      h("header", { class: "checks-heading" }, [
        h("div", null, [
          h("span", { class: "eyebrow" }, "Deterministic artifact QA"),
          h("h2", null, "Checks"),
          h("p", null, "These checks expose recorded evidence gaps and invariant violations. They do not claim extraction completeness or model correctness."),
        ]),
        h("div", null, [
          h("strong", null, String(flags.length)),
          h("span", null, `flag${flags.length === 1 ? "" : "s"}`),
        ]),
      ]),
      h("div", { class: "checks-grid" }, checks.map((item) => h("article", {
        class: `check-card status-${item.status}`,
        "data-check-id": item.id,
      }, [
        h("header", null, [
          h("b", null, item.title),
          h("span", { class: `check-status status-${item.status}` }, item.status),
        ]),
        h("p", null, item.summary),
        item.detail ? h("small", null, item.detail) : null,
        item.affectedIds.length ? h("div", { class: "check-affected" }, [
          h("span", null, `${item.affectedIds.length} affected occurrence(s)`),
          h("button", {
            onClick: () => {
              const affected = rows.find((row) => row.id === item.affectedIds[0]);
              if (affected?.source?.pred || affected?.source?.gt) selectVerificationRow(affected);
              else if (affected) selectRow(affected);
            },
          }, "Inspect first"),
        ]) : null,
      ]))),
    ]);
  }

  function renderBboxes(label, molecules) {
    const boxes = (molecules || []).map((molecule) => ({
      identifier: molecule.identifier || "unnamed",
      page: molecule.bbox_page || molecule.page,
      bbox: molecule.bbox,
      reviewed: state.geometryCorrections[molecule.occurrence?.occurrence_id] || null,
    })).filter((item) => item.bbox || item.reviewed);
    return h("div", { class: "bbox-claims" }, [
      h("b", null, label),
      ...(boxes.length ? boxes.map((item) => h("div", { class: "bbox-claim" }, [
        h("span", null, item.identifier),
        h("span", null, `p${item.page || "?"}`),
        h("code", null, item.bbox
          ? item.bbox.map((value) => Number(value).toFixed(1)).join(" · ")
          : "raw bbox unavailable"),
        item.reviewed ? h("span", { class: "reviewed-geometry-badge" },
          `reviewed · ${formatGeometryLocation(item.reviewed.geometry)}`) : null,
      ])) : [h("p", null, "No drawable bbox")]),
    ]);
  }

  function renderGeometryCorrection(row) {
    const targets = geometryTargetsForRow(row);
    const target = state.geometryTarget;
    const active = target ? state.geometryCorrections[target.target_id] : null;
    const location = state.draftGeometryLocation;
    const coordinates = location?.bbox || ["", "", "", ""];
    return h("section", {
      class: `pdf-geometry-editor${state.geometryEditing ? " is-drawing" : ""}`,
      "data-testid": "pdf-geometry-editor",
    }, [
      h("header", null, [
        h("div", null, [
          h("span", { class: "eyebrow" }, "Evidence geometry sidecar"),
          h("b", null, "Locate on PDF"),
        ]),
        h("span", null, `${Object.keys(state.geometryCorrections).length} reviewed location${
          Object.keys(state.geometryCorrections).length === 1 ? "" : "s"
        }`),
      ]),
      h("p", null,
        "Choose one exact occurrence or claim. Draw on Main/SI with the shared overlay; raw extraction geometry remains unchanged."),
      h("div", { class: "geometry-target-row" }, [
        h("label", null, [
          h("span", null, "Target"),
          h("select", {
            "aria-label": "Location target",
            value: target?.target_id || "",
            onChange: (event) => {
              const next = targets.find((item) => item.target_id === event.target.value);
              if (next) startGeometryEdit(next);
            },
          }, [
            h("option", { value: "" }, "Choose molecule / condition / metric"),
            ...targets.map((item) => h("option", {
              value: item.target_id,
            }, `${item.location?.bbox ? "Located" : "Missing"} · ${item.label} · ${
              item.target_type === "molecule"
                ? `${item.target_path[0]} ${Number(item.target_path[1]) + 1}`
                : item.target_type === "condition"
                  ? `condition ${Number(item.target_path[1]) + 1}.${Number(item.target_path[2]) + 1}`
                  : `metric ${Number(item.target_path[1]) + 1}`
            } · ${item.location?.source_document === "si" ? "SI" : "Main"} p${
              item.location?.page || "?"
            }`)),
          ]),
        ]),
        target ? h("button", {
          onClick: () => startGeometryEdit(target),
        }, active ? "Adjust reviewed location" : "Locate on PDF") : null,
      ]),
      target ? h(Fragment, null, [
        h("div", { class: "geometry-target-summary" }, [
          h("b", null, target.label),
          h("span", null, [
            "Raw: ",
            formatGeometryLocation(target.location),
          ]),
          active ? h("span", { class: "reviewed-location" }, [
            `Reviewed-v${active.reviewed_version || state.headVersion}: `,
            formatGeometryLocation(active.geometry),
          ]) : null,
        ]),
        h("aside", {
          class: "geometry-draw-instruction",
          role: "status",
        }, [
          h("b", null, state.geometryEditing
            ? "Draw mode active" : "Location staged"),
          h("span", null, state.geometryEditing
            ? `Current destination: ${
                viewerPanel.sourceDocument === "si" ? "SI" : "Main"
              } page ${viewerPanel.page}. Drag a tight rectangle on the PDF.`
            : "Use Draw replacement to revise it, or review the server-validated diff."),
          h("div", null, [
            h("button", {
              class: state.geometryEditing ? "on" : "",
              onClick: () => {
                state.geometryEditing = true;
                state.proposedPatch = null;
                state.message = "Draw mode active on the current PDF page.";
              },
            }, "Draw replacement on this page"),
            h("button", {
              onClick: () => {
                const destination = active?.geometry || target.location;
                if (!destination?.page) return;
                window.dispatchEvent(new CustomEvent("pdf-review-geometry-navigate", {
                  detail: {
                    jobId: model.jobId,
                    sourceDocument: destination.source_document,
                    page: destination.page,
                  },
                }));
              },
            }, "Show current location"),
            state.geometryEditing ? h("button", {
              onClick: () => {
                state.geometryEditing = false;
                state.message = "Draw mode paused; no location was applied.";
              },
            }, "Stop drawing") : null,
          ]),
        ]),
        h("fieldset", { class: "geometry-coordinate-fields" }, [
          h("legend", null, "Exact PDF-point coordinates"),
          ...["x1", "y1", "x2", "y2"].map((label, index) => h("label", null, [
            h("span", null, label),
            h("input", {
              type: "number",
              step: "0.1",
              min: "0",
              "aria-label": `Location ${label}`,
              value: coordinates[index],
              onInput: (event) => updateGeometryCoordinate(index, event.target.value),
            }),
          ])),
        ]),
        h("label", { class: "geometry-note" }, [
          h("span", null, "Location note"),
          h("input", {
            value: state.draftGeometryNote,
            placeholder: "Why this page/box is the right evidence",
            onInput: (event) => {
              state.draftGeometryNote = event.target.value;
              state.proposedPatch = null;
            },
          }),
        ]),
        h("div", { class: "geometry-review-actions" }, [
          h("button", {
            class: "review-location",
            disabled: state.saving || !location?.bbox,
            onClick: () => previewGeometryCorrection(),
          }, state.saving ? "Checking location…" : "Review location"),
          h("button", {
            disabled: !location,
            onClick: () => {
              proposalRun += 1;
              state.draftGeometryLocation = null;
              state.proposedPatch = null;
              state.geometryEditing = true;
              state.message = "Staged location cleared. Draw a new box to continue.";
            },
          }, "Clear staged box"),
        ]),
      ]) : h("p", { class: "geometry-empty" },
        "Select a target to attach or adjust one reviewed location."),
    ]);
  }

  function renderProposedPatch() {
    const proposal = state.proposedPatch;
    if (!proposal) return null;
    const geometryProposal = proposal.operation === "locate_pdf_geometry";
    const before = proposal.before || {};
    const after = proposal.after || {};
    const fields = geometryProposal ? [
      ["Target", before.target_label || after.target_label || proposal.occurrence_id,
        after.target_label || proposal.occurrence_id],
      ["PDF location",
        formatGeometryLocation(before.geometry || after.original),
        formatGeometryLocation(after.geometry)],
      ["Location note", before.note || "none", after.note || "none"],
    ] : [
      ["Verdict", before.verdict || "unreviewed", after.verdict || "unreviewed"],
      ["SMILES", before.correction?.smiles || "unchanged", after.correction?.smiles || "unchanged"],
      ["Molecule occurrence",
        before.correction?.molecule?.occurrence_id || "unchanged",
        after.correction?.molecule?.occurrence_id || "unchanged"],
      ["Reviewed E-SMILES",
        before.correction?.molecule?.esmiles || "unchanged",
        after.correction?.molecule?.esmiles || "unchanged"],
      ["Condition", before.correction?.condition_text || "unchanged",
        after.correction?.condition_text || "unchanged"],
      ["Note", before.note || "none", after.note || "none"],
    ].filter(([, left, right]) => left !== right);
    return h("section", {
      class: "proposed-patch-diff",
      "aria-label": "Proposed correction diff",
    }, [
      h("header", null, [
        h("div", null, [
          h("span", { class: "eyebrow" }, "Proposed patch · not applied"),
          h("b", null, `${geometryProposal ? "Locate on PDF · " : ""}${
            proposal.base_patch_id
          } → new reviewed version`),
        ]),
        h("span", null, "Raw prediction remains immutable"),
      ]),
      state.agentProposalMeta ? h("aside", {
        class: "pdf-agent-proposal-context",
        "aria-label": "Pi Agent proposal context",
      }, [
        h("b", null, "Pi Agent proposal · not applied"),
        h("p", null, state.agentProposalMeta.rationale),
        state.agentProposalMeta.evidence?.length
          ? h("ul", null,
              state.agentProposalMeta.evidence.map((item) => h("li", null, item)))
          : null,
      ]) : null,
      h("div", { class: "patch-diff-fields" }, fields.map(([label, beforeValue, afterValue]) =>
        h("div", null, [
          h("b", null, label),
          h("del", null, beforeValue),
          h("ins", null, afterValue),
        ]))),
      h("div", { class: "patch-diff-actions" }, [
        h("button", { class: "apply-patch", onClick: applyProposedPatch }, "Apply patch"),
        h("button", { onClick: discardProposedPatch }, "Discard"),
      ]),
    ]);
  }

  function renderInspector() {
    const row = selected();
    if (!row && state.workspaceLocation) {
      const location = state.workspaceLocation;
      const sourceLabel = location.documentSource === "si"
        ? "Supporting information" : "Main paper";
      const bboxLabel = location.bbox
        ? `${location.bbox[0]}, ${location.bbox[1]} → ${location.bbox[2]}, ${location.bbox[3]}`
        : "Whole page";
      return h("section", {
        class: "pdf-layer pdf-inspector pdf-location-inspector",
        "data-layer": "inspector",
      }, [
        h("div", { class: "inspector-head" }, [
          h("div", null, [
            h("span", { class: "eyebrow" }, "Agent evidence location"),
            h("h3", null, `${sourceLabel} · page ${location.page}`),
          ]),
        ]),
        h("div", { class: "product-risk-summary" }, [
          h("b", null, "Agent-supplied evidence target"),
          h("span", null,
            location.reactionId
              ? `Reaction ${location.reactionId} is not present in this workspace; no association was retained.`
              : "No reaction association was asserted by this command; the previous reaction selection was cleared."),
        ]),
        h("div", { class: "condition-grid" }, [
          h("div", null, [
            h("b", null, "Document source"),
            h("p", null, sourceLabel),
          ]),
          h("div", null, [
            h("b", null, "Coordinate frame"),
            h("p", null, location.bboxFrame || "Whole page (no coordinate frame asserted)"),
          ]),
          h("div", null, [
            h("b", null, "Evidence bounds"),
            h("p", null, bboxLabel),
          ]),
          h("div", null, [
            h("b", null, "Next review step"),
            h("p", null, "Choose a reaction or ask Pi to open this evidence with a reactionId."),
          ]),
        ]),
        h("button", {
          onClick: () => {
            state.layer = "all";
          },
        }, "Choose a reaction"),
      ]);
    }
    if (!row) return h("section", { class: "pdf-layer" }, "No reaction selected.");
    const predEvidence = row.evidence || {
      reactants: row.source.pred?.reactants || [],
      products: row.source.pred?.products || [],
    };
    const claimWarnings = claimGroundingWarnings(row.evidence);
    const unresolvedRows = productMode
      ? rows.filter((item) => productReviewReasons(item).length) : [];
    const unresolvedIndex = unresolvedRows.findIndex((item) => item.id === row.id);
    const rowAnchor = viewAnchorByIndex(model, row.source.pred?.anchor);
    const rowSource = rowDocumentSource(model, row);
    const rowPage = row.evidence?.anchor_page || rowAnchor?.page || row.source.region?.page || "?";
    const gt = row.source.gt || null;
    const link = reviewerLink(model, row);
    const latestVersion = state.versions.find(
      (version) => version.version === state.headVersion,
    );
    return h("section", {
      class: "pdf-layer pdf-inspector",
      "data-layer": state.layer === "edit" ? "edit" : "inspector",
      "data-difference-id": row.id,
    }, [
      h("div", { class: "inspector-head" }, [
        h("div", null, [
          h("span", { class: "eyebrow" }, productMode ? "Reaction evidence" : "Evidence inspector"),
          h("h3", null, row.label),
        ]),
        h("div", { class: "inspector-actions" }, [
          productMode ? h("button", {
            class: "back-to-attention",
            onClick: () => openWorkspaceModule("attention"),
          }, "Back to Attention") : null,
          productMode && row.source.pred ? h("button", {
            class: "edit-occurrence",
            onClick: () => openWorkspaceModule("edit"),
          }, "Edit this occurrence") : null,
          productMode && unresolvedRows.length ? h("div", {
            class: "unresolved-navigation",
            "aria-label": "Unresolved review item navigation",
          }, [
            h("button", {
              disabled: unresolvedIndex <= 0,
              onClick: () => selectRow(unresolvedRows[unresolvedIndex - 1]),
            }, "Previous unresolved"),
            h("span", null, unresolvedIndex >= 0
              ? `${unresolvedIndex + 1} / ${unresolvedRows.length}` : "not in queue"),
            h("button", {
              disabled: unresolvedIndex >= unresolvedRows.length - 1,
              onClick: () => selectRow(
                unresolvedRows[unresolvedIndex < 0 ? 0 : unresolvedIndex + 1],
              ),
            }, "Next unresolved"),
          ]) : null,
          h("label", null, [
            "Focus overlay ",
            h("select", {
              value: state.focus,
              onChange: (event) => setFocus(event.target.value),
            }, [
              h("option", { value: "selected-only" }, "selected-only"),
              h("option", { value: "current-anchor" }, "current-anchor"),
              h("option", { value: "all-occurrences" }, "all-occurrences"),
            ]),
          ]),
        ]),
      ]),
      productMode ? h("nav", {
        class: "review-breadcrumb",
        "aria-label": "Current document evidence location",
      }, [
        h("span", null, rowSource === "si" ? "Supporting information" : "Main paper"),
        h("i", { "aria-hidden": "true" }, "›"),
        h("span", null, `page ${rowPage}`),
        h("i", { "aria-hidden": "true" }, "›"),
        h("span", null, rowAnchor?.label || row.source.pred?.anchor_label || "Unanchored"),
        h("i", { "aria-hidden": "true" }, "›"),
        h("strong", null, row.label),
      ]) : null,
      productMode ? h("div", { class: "product-risk-summary" }, [
        h("b", null, productReviewReasons(row).length
          ? "Review required"
          : productInfoReasons(row).length ? "Evidence notes" : "Evidence trace available"),
        h("span", null, [...productReviewReasons(row), ...productInfoReasons(row)]
          .map((reason) => productReasonMeta(reason).label).join(" · ")
          || "No prediction-only evidence warning is recorded for this reaction."),
      ]) : h("div", { class: "inspector-dimensions" }, [
        dimensionChip("Chemistry", row.dimensions.chemistry),
        dimensionChip("Conditions", row.dimensions.conditions),
        dimensionChip("Grounding", row.dimensions.grounding),
        dimensionChip("Provenance", row.dimensions.provenance),
        dimensionChip("Identifier", row.dimensions.identifier),
        dimensionChip("Metrics/Yield", row.dimensions.metrics),
        dimensionChip("Human", state.adjudications[row.id] ? "reviewed" : "pending"),
      ]),
      productMode && row.evidence?.review_context ? h("aside", {
        class: `review-context-banner context-${row.evidence.review_context.reportability}`,
      }, [
        h("b", null, row.evidence.review_context.label),
        h("span", null, `Reportability: ${row.evidence.review_context.reportability.replaceAll("_", " ")}`),
      ]) : productMode ? h("aside", { class: "review-context-unavailable" },
        "Reportability context not attached; no class is inferred from page title or chemistry.") : null,
      productMode && claimWarnings.length ? h("aside", {
        class: "claim-grounding-alert",
        "data-testid": "claim-grounding-alert",
      }, [
        h("b", null, `${claimWarnings.length} condition/metric claim${claimWarnings.length === 1 ? " lacks" : "s lack"} PDF geometry`),
        h("span", null, "The chemistry remains reviewable, but these image-read claims are not independently clickable or grounded."),
        h("ul", null, claimWarnings.slice(0, 6).map((warning) =>
          h("li", null, `${warning.field}: ${warning.label} — ${warning.reason}`))),
      ]) : null,
      h("div", { class: `evidence-side-by-side ${gt ? "" : "prediction-only"}` }, [
        renderReactionLane("PRED", predEvidence, gt, row),
        gt ? renderReactionLane("GT", gt, predEvidence, row)
          : h("p", { class: "no-gt-inline" }, [
              h("b", null, "Prediction-only review. "),
              "No reference is attached; correctness is not inferred.",
            ]),
      ]),
      h("div", { class: `condition-grid ${gt ? "" : "prediction-only"}` }, [
        renderConditionEvidence(row),
        gt ? h("div", null, [h("b", null, "GT conditions"),
          h("p", null, (gt.conditions || []).join(" | ") || "unavailable"),
          row.diagnostics?.conditions
            ? h("small", { class: row.dimensions.conditions === "diff" ? "diag-diff" : "diag-info" },
                row.dimensions.conditions === "diff"
                  ? `${row.diagnostics.conditions.substantive.length} substantive mismatch(es)`
                  : `${row.diagnostics.conditions.info.length} role/format/detail note(s)`)
            : null]) : null,
        renderMetrics(
          "Prediction metrics",
          row.source.pred?.metrics || row.evidence?.metrics || [],
          null,
          row,
        ),
        gt ? renderMetrics("GT metrics", gt.metrics || []) : null,
        renderBboxes("Prediction locations",
          [...(predEvidence.reactants || []), ...(predEvidence.products || [])]),
        gt ? renderBboxes("GT locations", [...(gt.reactants || []), ...(gt.products || [])]) : null,
        row.diagnostics?.yield_missing?.length
          ? h("div", { class: "yield-audit-miss" }, [
              h("b", null, "Yield numeric mismatch"),
              ...row.diagnostics.yield_missing.map((miss) =>
                h("p", null, `Missing ${miss.gt.text} for ${miss.gt.of || "row"}`)),
            ]) : null,
      ]),
      h("details", { class: "lineage-panel", open: !productMode }, [
        h("summary", null, `Auditable lineage · ${
          (predEvidence.reactants || []).length + (predEvidence.products || []).length
        } molecule records`),
        renderLineage(row),
      ]),
      productMode ? null : h("div", { class: "candidate-panel" }, [
        h("b", null, `Candidate diagnoses (${row.claims.length} claims; not confirmed)`),
        ...(row.claims.length
          ? row.claims.map((claim) => h("div", null, [
              h("span", { class: "candidate-label" }, claim.label),
              h("small", null, (claim.evidence || []).join("; ")),
            ]))
          : [h("p", null, "No automatic diagnosis claim.")]),
      ]),
      (!productMode || state.layer === "edit") ? h("div", { class: "adjudication-panel" }, [
        h("div", { class: "adjudication-title" }, [
          h("b", null, productMode ? "Versioned local correction" : "Human adjudication"),
          h("span", null, productMode
            ? "Saved as a patch sidecar; raw prediction remains unchanged"
            : "Shortcuts 1-7 select verdicts"),
        ]),
        productMode ? renderGeometryCorrection(row) : null,
        h("select", {
          "aria-label": productMode ? "Review action" : "Human verdict",
          value: state.draftVerdict,
          onChange: (event) => {
            proposalRun += 1;
            state.saving = false;
            discardCurrentAgentProposal("human_edit_replaced");
            state.draftVerdict = event.target.value;
            state.proposedPatch = null;
          },
        }, [
          h("option", { value: "" }, "Choose verdict"),
          ...verdictOptions.map((verdict) => h("option", { value: verdict }, verdict)),
        ]),
        h("textarea", {
          "aria-label": "Review note",
          value: state.draftNote,
          placeholder: "Review note",
          onInput: (event) => {
            proposalRun += 1;
            state.saving = false;
            discardCurrentAgentProposal("human_edit_replaced");
            state.draftNote = event.target.value;
            state.proposedPatch = null;
          },
        }),
        h("div", { class: "correction-fields" }, [
          productMode ? h("aside", { class: "pdf-molecule-correction" }, [
            state.draftMoleculeCorrection ? h(Fragment, null, [
              h("b", null, "Staged molecule occurrence"),
              h("span", null, state.draftMoleculeCorrection.occurrence_id),
              h("code", null, state.draftMoleculeCorrection.esmiles),
              h("button", {
                onClick: () => {
                  state.draftMoleculeCorrection = null;
                  state.proposedPatch = null;
                },
              }, "Clear staged structure"),
            ]) : h(Fragment, null, [
              h("b", null, "Molecule structure"),
              h("span", null,
                "Use Open full Ketcher on one PRED molecule card. Free-text reaction-level SMILES is disabled."),
            ]),
          ]) : h("input", {
            "aria-label": "Corrected SMILES",
            value: state.draftSmiles,
            placeholder: "Optional corrected SMILES",
            onInput: (event) => {
              proposalRun += 1;
              state.saving = false;
              discardCurrentAgentProposal("human_edit_replaced");
              state.draftSmiles = event.target.value;
              state.draftMoleculeCorrection = null;
              state.proposedPatch = null;
            },
          }),
          h("input", {
            "aria-label": "Corrected condition text",
            value: state.draftCondition,
            placeholder: "Optional corrected condition text",
            onInput: (event) => {
              proposalRun += 1;
              state.saving = false;
              discardCurrentAgentProposal("human_edit_replaced");
              state.draftCondition = event.target.value;
              state.proposedPatch = null;
            },
          }),
        ]),
        productMode ? renderProposedPatch() : null,
        h("div", { class: "adjudication-actions" }, [
          h("button", { disabled: state.saving, onClick: saveAdjudication },
            state.saving ? "Saving" : productMode ? "Review changes" : "Save verdict"),
          productMode
            ? h("button", {
                disabled: !state.headVersion || state.saving,
                onClick: undoLatestPatch,
              }, latestVersion?.operation === "locate_pdf_geometry"
                ? `Undo reviewed-v${state.headVersion} · Locate`
                : "Undo latest patch")
            : h("button", { onClick: exportSummary }, "Export review summary"),
          link ? h("a", { href: link, target: "_blank" }, "Open Ketcher reviewer") : null,
          h("span", null, state.message),
        ]),
        productMode ? h("div", { class: "version-history" }, [
          h("b", null, `Version history (${state.versions.length})`),
          ...(state.versions.length
            ? [...state.versions].reverse().map((version) => h("article", null, [
                h("span", null, `v${version.version}${version.version === state.headVersion ? " · active" : ""}`),
                h("div", null, [
                  h("b", null, version.operation === "locate_pdf_geometry"
                    ? `Locate · ${version.target_label || version.occurrence_id}`
                    : version.verdict),
                  h("small", null,
                    `${version.actor} · ${new Date(version.created_at).toLocaleString()} · parent v${version.parent_version}`),
                ]),
                version.operation === "locate_pdf_geometry"
                  ? h("button", {
                      onClick: () => inspectGeometryVersion(version),
                    }, `Show ${version.geometry?.source_document === "si" ? "SI" : "Main"} p${version.geometry?.page}`)
                  : null,
              ]))
            : [h("p", null, "No local corrections yet.")]),
        ]) : null,
      ]) : h("div", { class: "edit-callout" }, [
        h("span", null, "Need to correct this record?"),
        h("button", { onClick: () => { state.layer = "edit"; } }, "Open Edit"),
      ]),
    ]);
  }

  const overlayHandler = (event) => {
    const detail = event.detail || {};
    const match = rows.find((row) =>
      (detail.predAnchor != null
        && Number(row.source.pred?.anchor) === Number(detail.predAnchor)
        && Number(row.source.pred?.example) === Number(detail.predReaction))
      || (detail.gtAnchor != null
        && Number(row.source.gt?.anchor) === Number(detail.gtAnchor)
        && Number(row.source.gt?.example) === Number(detail.gtReaction)));
    if (match) {
      selectRow(match);
      requestAnimationFrame(() => {
        document.getElementById("docright")?.scrollTo({ top: 0, behavior: "smooth" });
      });
    }
  };
  const keydownHandler = (event) => {
    if (!["inspector", "edit", "verify"].includes(state.layer)
        || event.target?.closest?.("input,textarea,select,[contenteditable]")) return;
    if (state.layer === "verify") {
      const key = event.key.toLowerCase();
      if (key === "j" || event.key === "ArrowDown") moveVerificationSelection(1);
      else if (key === "k" || event.key === "ArrowUp") moveVerificationSelection(-1);
      else if (key === "a") stageVerificationDecision("accept");
      else if (key === "x") stageVerificationDecision("correct");
      else if (key === "e") state.layer = "edit";
      else return;
      event.preventDefault();
      return;
    }
    if (productMode && ["ArrowUp", "ArrowDown"].includes(event.key)) {
      const current = rows.findIndex((row) => row.id === state.selectedId);
      const offset = event.key === "ArrowDown" ? 1 : -1;
      const next = rows[Math.max(0, Math.min(rows.length - 1, current + offset))];
      if (next) {
        const layer = state.layer;
        selectRow(next);
        state.layer = layer;
      }
      event.preventDefault();
      return;
    }
    const index = Number(event.key) - 1;
    if (!productMode && index >= 0 && index < VERDICTS.length) {
      state.draftVerdict = VERDICTS[index];
      event.preventDefault();
    }
  };
  const viewerHandler = (event) => {
    Object.assign(viewerPanel, event.detail || {});
  };
  const moleculeHandler = (event) => {
    const detail = event.detail || {};
    if (!productMode || (detail.jobId && detail.jobId !== model.jobId)) return;
    const entityKeys = new Set(
      [detail.entityKey, ...(detail.entityKeys || [])].filter(Boolean),
    );
    const occurrenceIds = Array.isArray(detail.occurrenceIds)
      ? detail.occurrenceIds.filter(Boolean) : [];
    const matching = substanceRecords().filter((record) =>
      entityKeys.has(record.entityKey)
      || record.occurrences.some((item) => occurrenceIds.includes(item.occurrenceId)));
    state.layer = "molecules";
    state.selectedEntityOccurrenceIds = occurrenceIds;
    if (matching.length === 1 && !detail.ambiguous) {
      selectMoleculeEntity(matching[0], { occurrenceIds });
    } else {
      state.selectedEntityKey = null;
      state.selectedEntityCandidates = matching.map((record) => record.entityKey);
    }
    requestAnimationFrame(() => {
      document.getElementById("docright")?.scrollTo({ top: 0, behavior: "smooth" });
    });
  };
  const regionHandler = (event) => {
    const detail = event.detail || {};
    if (detail.jobId && detail.jobId !== model.jobId) return;
    if (state.layer === "edit" && state.geometryTarget) {
      state.message = "Location target retained while you navigate Main/SI and PDF pages.";
      return;
    }
    state.selectedEntityKey = null;
    state.selectedEntityCandidates = [];
    state.selectedEntityOccurrenceIds = [];
    const anchorIndex = detail.anchor == null ? null : Number(detail.anchor);
    if (!Number.isInteger(anchorIndex)) {
      state.selectedId = null;
      state.layer = "region";
      setFocus("current-anchor");
      return;
    }
    const reactionIndex = detail.reaction == null ? null : Number(detail.reaction);
    const row = rows.find((item) => item.source.pred?.anchor === anchorIndex
      && (!Number.isInteger(reactionIndex) || item.source.pred?.example === reactionIndex));
    if (!row) return;
    state.selectedId = row.id;
    state.layer = "region";
    setFocus(Number.isInteger(reactionIndex) ? "selected-only" : "current-anchor");
  };
  window.addEventListener("pdf-review-molecule-select", moleculeHandler);
  window.addEventListener("pdf-review-region-select", regionHandler);
  window.__pdfReviewEventHandlers = {
    overlay: overlayHandler,
    keydown: keydownHandler,
    viewer: viewerHandler,
    region: regionHandler,
  };

  return {
    unmounted() {
      unsubscribeWorkspaceCommands();
      stopLayoutWatch();
      if (reviewUiStateTimer != null) persistReviewUiState();
      // The modal target is shared with the Figure reviewer. Never carry an
      // occurrence callback or a read-only reference into another PDF job.
      store.ketcherTarget = null;
      window.removeEventListener("chemrxn-workspace-command-rejected", rejectedWorkspaceCommand);
      window.removeEventListener("pdf-review-molecule-select", moleculeHandler);
      window.removeEventListener("pdf-review-region-select", regionHandler);
      if (window.__pdfReviewEventHandlers?.overlay === overlayHandler) {
        window.__pdfReviewEventHandlers = null;
      }
    },
    render() {
      const attentionUnits = productMode
        ? productAttentionUnits(rows.filter((row) => productReviewReasons(row).length)) : null;
      const navigation = productMode ? [
        ["attention", "attention", "Attention", "Attention",
          attentionUnits.reactions + attentionUnits.unlinkedRegions],
        ["region", "region", "Reactions", "Region reactions"],
        ["inspector", "evidence", "Evidence", "Reaction details"],
        ["edit", "edit", "Edit", "Edit"],
      ] : [
        ["overview", "overview", "Overview"],
        ["queue", "attention", "Difference Queue"],
        ["inspector", "evidence", "Evidence Inspector"],
      ];
      return h(Fragment, null, [
        viewerPanel.imageUrl ? h(Teleport, { to: "#docbboxpanel" },
          h(BBoxImagePanel, geometryPanelProps())) : null,
        h("div", {
          class: `pdf-reviewer-v2${productMode ? " product-workspace" : ""}`,
        }, [
          h("nav", { class: "pdf-layer-nav", "aria-label": "PDF reviewer layers" }, [
            ...navigation.map(([value, module, label, ariaLabel, count]) => h("button", {
              class: state.layer === value ? "on" : "",
              "aria-label": ariaLabel || label,
              "aria-current": state.layer === value ? "page" : null,
              onClick: () => {
                if (value === "region") {
                  state.layer = "region";
                  setFocus("current-anchor");
                } else {
                  openWorkspaceModule(module);
                }
              },
            }, [
              h("span", null, label),
              Number.isFinite(count) && count > 0
                ? h("span", { class: "nav-count", "aria-hidden": "true" }, String(count)) : null,
            ])),
            productMode ? h("details", { class: "workspace-more" }, [
              h("summary", null, "More"),
              h("div", null, [
                ["overview", "overview", "Overview"],
                ["all", "reactions", "All reactions"],
                ["verify", "verify", "Verify"],
                ["checks", "checks", "Checks"],
                ["route", "route", "Document map"],
                ["table", "table", "Table"],
                ["molecules", "molecules", "Molecules"],
                ["process", "process", "Extraction process"],
                ["ask", "ask", "Ask"],
                ["views", "saved-views", "Saved views"],
              ].map(([value, module, label]) => h("button", {
                class: state.layer === value ? "on" : "",
                onClick: (event) => {
                  event.currentTarget.closest("details")?.removeAttribute("open");
                  if (value === "process" && typeof window.showExtractionProcess === "function") {
                    window.showExtractionProcess(true);
                  } else {
                    openWorkspaceModule(module);
                  }
                },
              }, label)),
            )]) : null,
          ]),
          completenessPartial ? h("aside", {
            class: "pdf-completeness-warning",
            role: "alert",
          }, [
            h("b", null, "PARTIAL · NOT REVIEW READY"),
            h("span", null,
              " The prediction is schema-valid, but its page/anchor/member completeness audit remains open. Review Attention and Extraction process before using it."),
          ]) : null,
          productMode ? renderProductCensus() : null,
          state.message ? h("p", {
            class: "interaction-message workspace-command-message",
            role: "status",
            "aria-live": "polite",
          }, state.message) : null,
          productMode
            ? state.layer === "region" ? renderRegionReview()
              : state.layer === "overview" ? renderProductOverview()
              : state.layer === "all" ? renderProductDirectory(false)
                : state.layer === "attention" ? renderProductDirectory(true)
                  : state.layer === "route" ? renderRouteWorkspace()
                  : state.layer === "table" ? renderTableWorkspace()
                      : state.layer === "molecules" ? renderMoleculeWorkspace()
                        : state.layer === "verify" ? renderVerifyWorkspace()
                          : state.layer === "checks" ? renderChecksWorkspace()
                        : state.layer === "ask" ? renderInteraction()
                          : state.layer === "views" ? renderSavedViews()
                            : renderInspector()
            : state.layer === "overview" ? renderOverview()
              : state.layer === "queue" ? renderQueue()
                : state.layer === "verify" ? renderVerifyWorkspace()
                  : state.layer === "checks" ? renderChecksWorkspace()
                    : renderInspector(),
        ]),
        store.ketcherTarget ? h(KetcherModal) : null,
      ]);
    },
  };
}

export function mountPdfReviewIsland(element, model) {
  store.ketcherTarget = null;
  if (element.__pdfReviewApp) element.__pdfReviewApp.unmount();
  element.innerHTML = "";
  const app = createApp(PdfReviewApp(model));
  app.mount(element);
  element.__pdfReviewApp = app;
  const mountedRows = buildRows(model);
  const restoredFirst = typeof model.initialSelectedId === "string"
    ? mountedRows.find((row) => row.id === model.initialSelectedId)
    : null;
  const first = restoredFirst || (Number.isInteger(model.initialAnchor)
    ? mountedRows.find((row) => row.source.pred?.anchor === model.initialAnchor)
    : null) || mountedRows.find(needsJudgment) || mountedRows[0];
  app.__initialSelectionEmitted = false;
  if (first && model.emitInitialSelection !== false) {
    window.dispatchEvent(new CustomEvent("pdf-review-select", {
      detail: {
        jobId: model.jobId,
        differenceId: first.id,
        preserveView: Boolean(model.preserveInitialView),
        predAnchor: first.source.pred?.anchor,
        predReaction: first.source.pred?.example,
        gtAnchor: first.source.gt?.anchor,
        gtReaction: first.source.gt?.example,
        sourceDocument: rowDocumentSource(model, first),
        page: first.source.pred
          ? viewAnchorByIndex(model, first.source.pred.anchor)?.page
          : first.source.gt?.page || first.source.region?.page,
      },
    }));
    app.__initialSelectionEmitted = true;
  }
  return app;
}
