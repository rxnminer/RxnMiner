const DIMENSION_LABELS = Object.freeze({
  chemistry: "Chemistry connectivity",
  stereo: "Strict stereochemistry",
  conditions: "Conditions",
  metrics: "Yield / metrics",
  grounding: "Grounding",
  provenance: "Provenance",
  identifier: "Identifier",
  human: "Human review",
});

const IMAGE_CLAIM_SOURCE_TYPES = new Set([
  "image_ocr",
  "image_ocr_with_footnote_resolved",
]);

const VERIFIED_DERIVATION_AUDIT_STATUSES = new Set([
  "handoff_inventory_match",
]);

export function isImageClaimSource(source) {
  if (!source || typeof source !== "object") return false;
  const origin = String(source.origin || "").trim().toLowerCase();
  const type = String(source.type || "").trim().toLowerCase();
  return origin ? origin === "image" : IMAGE_CLAIM_SOURCE_TYPES.has(type);
}

export function hasValidEvidenceBbox(value) {
  if (!Array.isArray(value) || value.length !== 4) return false;
  if (!value.every((item) => typeof item === "number" && Number.isFinite(item))) {
    return false;
  }
  const [x1, y1, x2, y2] = value;
  return [x1, y1, x2, y2].every(Number.isFinite) && x2 > x1 && y2 > y1;
}

export function imageClaimMissingBbox(source) {
  return isImageClaimSource(source) && !hasValidEvidenceBbox(source?.bbox);
}

export function provenanceNeedsAttention(molecule) {
  const source = molecule?.source || {};
  const status = source.derivation_status || {};
  const auditStatus = String(status.audit_status || "").trim();
  return source.lineage_contract === "legacy_unsealed"
    || ["untracked", "image_untraced"].includes(source.class)
    || (source.class === "identifier_resolution" && !molecule?.raw_molparser)
    || status.roundtrip_ok === false
    || (auditStatus && !VERIFIED_DERIVATION_AUDIT_STATUSES.has(auditStatus))
    || (status.roundtrip_ok === true
      && !VERIFIED_DERIVATION_AUDIT_STATUSES.has(auditStatus))
    || (!molecule?.raw_molparser && source.derivation_method === "untracked");
}

function displayedStatus(value) {
  if (value === "unknown") return "unchecked";
  return value || "unchecked";
}

function predictedMolecules(row) {
  return row?.evidence
    ? [...(row.evidence.reactants || []), ...(row.evidence.products || [])]
    : [];
}

function metricText(row) {
  return (row?.evidence?.metrics || row?.source?.pred?.metrics || [])
    .flatMap((metric) => [
      metric?.text, metric?.content, metric?.value, metric?.role, metric?.type,
    ])
    .filter(Boolean)
    .join(" ");
}

function explanationFor(row, key, status) {
  if (key === "human") {
    return status === "reviewed"
      ? "A human decision is stored in the versioned review sidecar."
      : "No human decision has been applied for this occurrence.";
  }
  if (key === "stereo") {
    return status === "unchecked"
      ? "No strict-stereo comparison is attached to this occurrence."
      : status === "pass"
        ? "The attached strict-stereo diagnostic is exact."
        : "The attached diagnostic reports a strict-stereo mismatch.";
  }
  if (status === "unchecked") {
    return row?.source?.gt
      ? "This dimension has no attached deterministic diagnostic."
      : "No reference is attached; correctness is not inferred from prediction alone.";
  }
  if (status === "pass") return "The attached deterministic evidence reports no mismatch.";
  if (status === "reviewed") return "A human decision is stored for this occurrence.";
  if (status === "pending") return "Human review is still pending.";
  return status === "attention"
    ? "Available evidence is incomplete and requires inspection."
    : "The attached comparison reports a substantive mismatch.";
}

export function verificationDimensions(row, adjudication = null) {
  const stereo = row?.diagnostics?.strict_stereo;
  const statuses = {
    chemistry: displayedStatus(row?.dimensions?.chemistry),
    stereo: stereo ? (stereo.exact ? "pass" : "diff") : "unchecked",
    conditions: displayedStatus(row?.dimensions?.conditions),
    metrics: displayedStatus(row?.dimensions?.metrics),
    grounding: displayedStatus(row?.dimensions?.grounding),
    provenance: displayedStatus(row?.dimensions?.provenance),
    identifier: displayedStatus(row?.dimensions?.identifier),
    human: adjudication ? "reviewed" : "pending",
  };
  return Object.entries(statuses).map(([key, status]) => ({
    key,
    label: DIMENSION_LABELS[key],
    status,
    explanation: explanationFor(row, key, status),
  }));
}

export function verificationRiskScore(row, adjudication = null) {
  return verificationDimensions(row, adjudication).reduce((score, item) => {
    if (item.status === "diff") return score + 4;
    if (item.status === "attention") return score + 3;
    if (item.status === "pending") return score + 1;
    return score;
  }, 0) + (row?.claims?.length || 0) * 2;
}

export function orderedVerificationRows(rows, adjudications = {}) {
  return [...(rows || [])]
    .filter((row) => row?.source?.pred || row?.source?.gt)
    .sort((left, right) =>
      verificationRiskScore(right, adjudications[right.id])
      - verificationRiskScore(left, adjudications[left.id])
      || String(left.label || left.id).localeCompare(String(right.label || right.id)));
}

function check(id, title, status, summary, affectedIds = [], detail = "") {
  return {
    id,
    title,
    status,
    summary,
    affectedIds: [...new Set(affectedIds)],
    detail,
  };
}

export function workspaceChecks(rows) {
  const sourceRows = rows || [];
  const reactionRows = sourceRows.filter((row) => row?.source?.pred);
  const allMolecules = reactionRows.flatMap((row) =>
    predictedMolecules(row).map((molecule) => ({ row, molecule })));

  const unlinked = sourceRows.filter((row) => row?.source?.status === "unextracted_region");

  const imageUngrounded = [];
  const textUngrounded = [];
  for (const entry of allMolecules) {
    if (entry.molecule.bbox || entry.molecule.bbox_page) continue;
    if (["text", "derived"].includes(entry.molecule.source?.class)) textUngrounded.push(entry.row.id);
    else imageUngrounded.push(entry.row.id);
  }

  const traceGaps = allMolecules.filter(({ molecule }) => {
    const sourceClass = String(molecule.source?.class || "").trim();
    const derivationMethod = String(molecule.source?.derivation_method || "").trim();
    if (molecule.raw_molparser) return false;
    return !sourceClass
      || ["untracked", "image_untraced"].includes(sourceClass)
      || derivationMethod === "untracked";
  });

  const identifierGroups = new Map();
  for (const { row, molecule } of allMolecules) {
    const identifier = String(molecule.identifier || "").trim().toLowerCase();
    const structure = String(molecule.canon || molecule.smi || molecule["E-SMILES"] || "").trim();
    if (!identifier || !structure) continue;
    const group = identifierGroups.get(identifier) || { structures: new Set(), rows: new Set() };
    group.structures.add(structure);
    group.rows.add(row.id);
    identifierGroups.set(identifier, group);
  }
  const inconsistentGroups = [...identifierGroups.entries()]
    .filter(([, group]) => group.rows.size > 1 && group.structures.size > 1);
  const repeatedGroups = [...identifierGroups.values()].filter((group) => group.rows.size > 1);

  const metricRows = reactionRows.filter((row) => /%/.test(metricText(row)));
  const invalidMetricRows = metricRows.filter((row) => {
    const values = [...metricText(row).matchAll(/(-?\d+(?:\.\d+)?)\s*%/g)]
      .map((match) => Number(match[1]));
    return values.some((value) => value < 0 || value > 100);
  });

  const conditionDiagnostics = reactionRows.filter((row) => row?.diagnostics?.conditions);
  const conditionDiffs = conditionDiagnostics.filter((row) =>
    row.diagnostics.conditions.status === "diff"
    || row.diagnostics.conditions.substantive?.length);

  const stereoDiagnostics = reactionRows.filter((row) => row?.diagnostics?.strict_stereo);
  const stereoDiffs = stereoDiagnostics.filter((row) =>
    row.diagnostics.strict_stereo.exact === false);

  return [
    check(
      "unlinked-regions",
      "Unlinked document regions",
      unlinked.length ? "flag" : "pass",
      unlinked.length
        ? `${unlinked.length} detected region${unlinked.length === 1 ? "" : "s"} are not linked to an extracted anchor.`
        : "Every detected layout region in this artifact is linked or intentionally excluded.",
      unlinked.map((row) => row.id),
      "This is a coverage warning, not proof that a missing region contains a reaction.",
    ),
    check(
      "grounding",
      "Visual grounding coverage",
      imageUngrounded.length ? "flag" : textUngrounded.length ? "info"
        : allMolecules.length ? "pass" : "unavailable",
      imageUngrounded.length
        ? `${new Set(imageUngrounded).size} reaction occurrence(s) contain image-derived molecules without drawable geometry.`
        : textUngrounded.length
          ? "Only text-derived or deterministic structures lack molecule boxes."
          : allMolecules.length
            ? "All image-derived molecule occurrences have drawable geometry."
            : "No prediction molecules are available for a grounding check.",
      imageUngrounded,
      `${textUngrounded.length} text/derived unboxed occurrence(s) are informational, not visual-grounding failures.`,
    ),
    check(
      "lineage",
      "Auditable source lineage",
      traceGaps.length ? "flag" : allMolecules.length ? "pass" : "unavailable",
      traceGaps.length
        ? `${traceGaps.length} molecule occurrence(s) have no auditable raw OCSR/source join.`
        : allMolecules.length
          ? "Every molecule occurrence has a recorded source class or raw lineage."
          : "No molecule lineage is available.",
      traceGaps.map(({ row }) => row.id),
      "A V2 prediction is not treated as raw MolParser evidence.",
    ),
    check(
      "identifier-consistency",
      "Cross-occurrence identifier consistency",
      inconsistentGroups.length ? "flag" : repeatedGroups.length ? "pass" : "unavailable",
      inconsistentGroups.length
        ? `${inconsistentGroups.length} repeated identifier(s) map to more than one serialized structure.`
        : repeatedGroups.length
          ? `${repeatedGroups.length} repeated identifier group(s) are structurally consistent.`
          : "No repeated identifiers are available for a cross-occurrence check.",
      inconsistentGroups.flatMap(([, group]) => [...group.rows]),
      "Connectivity and strict stereochemistry remain separate review dimensions.",
    ),
    check(
      "metric-range",
      "Yield and percentage range",
      invalidMetricRows.length ? "flag" : metricRows.length ? "pass" : "unavailable",
      invalidMetricRows.length
        ? `${invalidMetricRows.length} reaction occurrence(s) contain percentages outside 0–100%.`
        : metricRows.length
          ? `${metricRows.length} occurrence(s) contain percentage metrics within 0–100%.`
          : "No percentage metric is attached; numeric correctness is unchecked.",
      invalidMetricRows.map((row) => row.id),
      "This range check does not claim that a value was extracted from the correct table row.",
    ),
    check(
      "conditions",
      "Substantive condition comparison",
      conditionDiffs.length ? "flag" : conditionDiagnostics.length ? "pass" : "unavailable",
      conditionDiffs.length
        ? `${conditionDiffs.length} occurrence(s) have substantive condition mismatches.`
        : conditionDiagnostics.length
          ? `${conditionDiagnostics.length} compared occurrence(s) have no substantive mismatch.`
          : "No reference-backed condition diagnostic is attached.",
      conditionDiffs.map((row) => row.id),
      "Role-only, formatting-only, and extra-detail differences remain informational.",
    ),
    check(
      "strict-stereo",
      "Strict stereochemistry comparison",
      stereoDiffs.length ? "flag" : stereoDiagnostics.length ? "pass" : "unavailable",
      stereoDiffs.length
        ? `${stereoDiffs.length} occurrence(s) fail the attached strict-stereo comparison.`
        : stereoDiagnostics.length
          ? `${stereoDiagnostics.length} occurrence(s) pass strict-stereo comparison.`
          : "No strict-stereo reference comparison is attached.",
      stereoDiffs.map((row) => row.id),
      "A no-stereo connectivity match is never promoted to a strict-stereo pass.",
    ),
  ];
}
