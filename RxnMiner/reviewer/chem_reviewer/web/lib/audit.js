import {
  canonicalSmiles,
  sameConnectivity,
} from "/static/lib/rdkit-loader.js";

export const AUDIT_CAUSES = [
  ["unresolved", "Unresolved"],
  ["suspected_gt_error", "Suspected reference error"],
  ["gt_error", "Confirmed reference error"],
  ["gt_incomplete", "Incomplete reference"],
  ["ocsr_error", "Recognition error"],
  ["tool_error", "Tool error"],
  ["pi_error", "Model error"],
  ["mixed_error", "Mixed error"],
  ["format_error", "Format or link error"],
  ["figure_ambiguous", "Ambiguous source"],
];

export const CAUSE_LABELS = Object.fromEntries(AUDIT_CAUSES);

function text(value) {
  return String(value ?? "").trim();
}

function normText(value) {
  return text(value).toLowerCase().replace(/\s+/g, " ");
}

function moleculeSmiles(molecule) {
  return text(molecule?.["E-SMILES"] || molecule?.smiles);
}

const METRIC_ROLES = new Set(["yield", "dr", "ee", "er", "conversion", "outcome"]);

function conditionItems(row) {
  const steps = Array.isArray(row?.conditions) ? row.conditions : [];
  const out = [];
  for (const step of steps) {
    const entries = Array.isArray(step) ? step : [step];
    for (const entry of entries) {
      if (!entry) continue;
      if (typeof entry === "object" && METRIC_ROLES.has(normText(entry.role))) continue;
      if (typeof entry === "string") {
        out.push(normText(entry));
        continue;
      }
      out.push([
        normText(entry.role),
        normText(entry.text || entry.description),
        normText(entry.smiles || entry["E-SMILES"]),
        normText(entry.amount),
      ].join("|"));
    }
  }
  return out.filter(Boolean).sort();
}

function metricMap(row) {
  const out = new Map();
  for (const metric of row?.metrics || []) {
    if (!metric) continue;
    const role = normText(metric.role) || "metric";
    const value = [
      normText(metric.text || metric.description),
      normText(metric.value),
      normText(metric.note),
    ].join("|");
    out.set(role, value);
  }
  return out;
}

function sameArray(a, b) {
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

function rowPair(predRows, gtRows, index, keyField) {
  const pred = predRows[index];
  if (!pred) return { pred: null, gt: gtRows[index] || null, gtIndex: index };
  const id = pred[keyField];
  const exactIndex = id
    ? gtRows.findIndex((row) => row?.[keyField] === id)
    : -1;
  const gtIndex = exactIndex >= 0 ? exactIndex : index;
  return { pred, gt: gtRows[gtIndex] || null, gtIndex };
}

function metricCounter() {
  return { correct: 0, total: 0, issueKeys: [] };
}

function addMetric(metrics, id, correct, issueKey = null) {
  if (!metrics[id]) metrics[id] = metricCounter();
  metrics[id].total += 1;
  if (correct) metrics[id].correct += 1;
  if (!correct && issueKey) metrics[id].issueKeys.push(issueKey);
}

function sourceMethod(molecule) {
  return molecule?._source?.derivation?.method
    || molecule?._source?.origin
    || molecule?._source?.type
    || "";
}

function suggestStructureCause(molecule, status) {
  const method = sourceMethod(molecule);
  if (status === "stereo") {
    return {
      cause: "unresolved",
      reason: "Connectivity matches; inspect source stereochemistry to distinguish model, recognition, and reference errors.",
    };
  }
  if (["rgroup_fill", "reverse_assembled", "deterministic_repair"].includes(method)) {
    return {
      cause: "unresolved",
      reason: `Derived by ${method}; inspect substituent sites, bridges, dummy atoms, and forward reconstruction before attributing an error.`,
    };
  }
  if (["molparser_direct", "molparser", "image_structure"].includes(method)) {
    return {
      cause: "unresolved",
      reason: "Original recognition retained. Inspect the source to distinguish recognition from reference errors.",
    };
  }
  if (["image_reasoning", "model_reasoning", "pi_reconstruction"].includes(method)) {
    return {
      cause: "unresolved",
      reason: "Model-derived structure. Compare the figure and raw recognition before attributing an error.",
    };
  }
  return { cause: "unresolved", reason: "Review the source, raw recognition, tool trace, and reference to determine the cause." };
}

function issueBase(collection, index, row, field, metricId) {
  const kind = collection === "selected_examples" ? "example" : "template";
  const rowId = row?.instance_id || row?.template_id || `${kind}-${index + 1}`;
  return {
    collection,
    rowIndex: index,
    rowKey: `${kind}:${index}`,
    rowId,
    field,
    metricId,
  };
}

function addIssue(issues, metrics, issue) {
  issues.push(issue);
  addMetric(metrics, issue.metricId, false, issue.key);
}

function rawDetectionFor(molecule, detections) {
  const source = molecule?._source || {};
  const idx = source.bbox_index ?? molecule?.bbox_index;
  const box = source.bbox ?? molecule?.bbox;
  return (detections || []).find((det) => {
    if (!det?.raw_available || !det?.smi || det?.markush) return false;
    if (idx !== undefined && idx !== null && det.bbox_index === idx) return true;
    if (!Array.isArray(box) || !Array.isArray(det.bbox)) return false;
    return box.slice(0, 4).every((value, i) => Number(value) === Number(det.bbox[i]));
  }) || null;
}

async function compareMolecule(predMol, gtMol, detections) {
  const predSmiles = moleculeSmiles(predMol);
  const gtSmiles = moleculeSmiles(gtMol);
  if (!predSmiles || !gtSmiles) {
    return { status: predSmiles === gtSmiles ? "same" : "different", predSmiles, gtSmiles };
  }
  const [predCanonical, gtCanonical, connectivity] = await Promise.all([
    canonicalSmiles(predSmiles),
    canonicalSmiles(gtSmiles),
    sameConnectivity(predSmiles, gtSmiles),
  ]);
  const status = predCanonical && gtCanonical && predCanonical === gtCanonical
    ? "same"
    : connectivity
      ? "stereo"
      : "different";
  const rawDetection = rawDetectionFor(predMol, detections);
  const rawSmiles = text(rawDetection?.smi);
  const rawCanonical = rawSmiles ? await canonicalSmiles(rawSmiles) : null;
  return {
    status,
    predSmiles,
    gtSmiles,
    predCanonical,
    gtCanonical,
    rawSmiles,
    rawEqualsPred: Boolean(rawCanonical && predCanonical && rawCanonical === predCanonical),
    rawEqualsGt: Boolean(rawCanonical && gtCanonical && rawCanonical === gtCanonical),
  };
}

async function compareCollection(collection, predRows, gtRows, issues, metrics, detections) {
  const keyField = collection === "selected_examples" ? "instance_id" : "template_id";
  const n = Math.max(predRows.length, gtRows.length);
  for (let index = 0; index < n; index += 1) {
    const { pred, gt } = rowPair(predRows, gtRows, index, keyField);
    const base = issueBase(collection, index, pred || gt, "row", "rows");
    if (!pred || !gt) {
      addIssue(issues, metrics, {
        ...base,
        key: `${base.rowKey}:row_presence`,
        field: "row_presence",
        label: pred ? "Reference row missing" : "Prediction row missing",
        pred: pred ? base.rowId : "Missing",
        gt: gt ? (gt[keyField] || `#${index + 1}`) : "Missing",
        suggestedCause: pred ? "gt_incomplete" : "pi_error",
        suggestedReason: pred ? "Prediction has no corresponding reference row." : "Reference row missing from the prediction.",
      });
      continue;
    }
    addMetric(metrics, "rows", true);

    const predRowId = text(pred[keyField]);
    const gtRowId = text(gt[keyField]);
    const rowIdKey = `${base.rowKey}:${keyField}`;
    if (predRowId === gtRowId) addMetric(metrics, "row_identifier", true);
    else addIssue(issues, metrics, {
      ...base,
      key: rowIdKey,
      field: keyField,
      metricId: "row_identifier",
      label: keyField === "instance_id" ? "Example identifier differs" : "Template ID differs",
      pred: predRowId || "Missing",
      gt: gtRowId || "Missing",
      suggestedCause: "unresolved",
      suggestedReason: "Check printed identifiers, reference conventions, and row alignment.",
    });

    if (collection === "selected_examples") {
      const predInherit = text(pred.inherit_template);
      const gtInherit = text(gt.inherit_template);
      if (predInherit === gtInherit) addMetric(metrics, "template_link", true);
      else addIssue(issues, metrics, {
        ...base,
        key: `${base.rowKey}:inherit_template`,
        field: "inherit_template",
        metricId: "template_link",
        label: "Inherits template differs",
        pred: predInherit || "Missing",
        gt: gtInherit || "Missing",
        suggestedCause: "format_error",
        suggestedReason: "Check reaction topology and parent-template links.",
      });
    }

    for (const side of ["reactants", "products"]) {
      const predMols = pred[side] || [];
      const gtMols = gt[side] || [];
      const molN = Math.max(predMols.length, gtMols.length);
      for (let mi = 0; mi < molN; mi += 1) {
        const predMol = predMols[mi] || null;
        const gtMol = gtMols[mi] || null;
        const molBase = `${base.rowKey}:${side}:${mi}`;
        const metricPrefix = side === "products" ? "product" : "reactant";
        const comparison = await compareMolecule(predMol, gtMol, detections);
        if (comparison.status === "same") {
          addMetric(metrics, `${metricPrefix}_strict`, true);
          addMetric(metrics, `${metricPrefix}_connectivity`, true);
        } else {
          const suggestion = suggestStructureCause(predMol, comparison.status);
          addIssue(issues, metrics, {
            ...base,
            key: `${molBase}:smiles`,
            field: `${side}.smiles`,
            metricId: `${metricPrefix}_strict`,
            secondaryMetricId: comparison.status === "different" ? `${metricPrefix}_connectivity` : null,
            label: `${side === "products" ? "Product" : "Reactant"} ${mi + 1} ${comparison.status === "stereo" ? "Stereochemistry differs" : "Connectivity differs"}`,
            pred: comparison.predSmiles || "Missing",
            gt: comparison.gtSmiles || "Missing",
            status: comparison.status,
            raw: comparison.rawSmiles || "",
            rawEqualsPred: comparison.rawEqualsPred,
            rawEqualsGt: comparison.rawEqualsGt,
            suggestedCause: comparison.rawEqualsGt && !comparison.rawEqualsPred
              ? (["rgroup_fill", "reverse_assembled", "deterministic_repair"].includes(sourceMethod(predMol)) ? "tool_error" : "pi_error")
              : suggestion.cause,
            suggestedReason: comparison.rawEqualsGt && !comparison.rawEqualsPred
              ? "Raw recognition matches the reference; inspect subsequent tool or model changes."
              : comparison.rawEqualsPred && !comparison.rawEqualsGt
                ? "Original recognition retained but differs from the reference. Inspect the source."
                : suggestion.reason,
          });
          if (comparison.status === "stereo") addMetric(metrics, `${metricPrefix}_connectivity`, true);
          else addMetric(metrics, `${metricPrefix}_connectivity`, false, `${molBase}:smiles`);
        }

        const predIdentifier = text(predMol?.identifier);
        const gtIdentifier = text(gtMol?.identifier);
        if (predIdentifier === gtIdentifier) addMetric(metrics, "molecule_identifier", true);
        else addIssue(issues, metrics, {
          ...base,
          key: `${molBase}:identifier`,
          field: `${side}.identifier`,
          metricId: "molecule_identifier",
          label: `${side === "products" ? "Product" : "Reactant"} ${mi + 1} identifier differs`,
          pred: predIdentifier || "Missing",
          gt: gtIdentifier || "Missing",
          suggestedCause: "unresolved",
          suggestedReason: "Check printed compound numbers, shared substrate identifiers, and reference conventions.",
        });

        for (const field of ["amount", "description"]) {
          const predValue = normText(predMol?.[field]);
          const gtValue = normText(gtMol?.[field]);
          if (predValue === gtValue) addMetric(metrics, field, true);
          else addIssue(issues, metrics, {
            ...base,
            key: `${molBase}:${field}`,
            field: `${side}.${field}`,
            metricId: field,
            label: `${side === "products" ? "Product" : "Reactant"} ${mi + 1} ${field} differs`,
            pred: text(predMol?.[field]) || "Missing",
            gt: text(gtMol?.[field]) || "Missing",
            suggestedCause: gtValue ? "unresolved" : "gt_incomplete",
            suggestedReason: gtValue ? "Check source text, footnotes, and field assignments." : `Reference omits ${field}; check whether it is incomplete.`,
          });
        }
      }
    }

    const predConditions = conditionItems(pred);
    const gtConditions = conditionItems(gt);
    if (sameArray(predConditions, gtConditions)) addMetric(metrics, "conditions", true);
    else addIssue(issues, metrics, {
      ...base,
      key: `${base.rowKey}:conditions`,
      field: "conditions",
      metricId: "conditions",
      label: "Reaction conditions differ",
      pred: predConditions.join(" ; ") || "Missing",
      gt: gtConditions.join(" ; ") || "Missing",
      suggestedCause: predConditions.length > gtConditions.length ? "gt_incomplete" : "unresolved",
      suggestedReason: "Check each catalyst, reagent, solvent, time, temperature, amount, and footnote.",
    });

    const predMetrics = metricMap(pred);
    const gtMetrics = metricMap(gt);
    const roles = new Set([...predMetrics.keys(), ...gtMetrics.keys()]);
    for (const role of roles) {
      const predValue = predMetrics.get(role) || "";
      const gtValue = gtMetrics.get(role) || "";
      const metricId = role === "yield" ? "yield" : role === "dr" ? "dr" : "other_metrics";
      if (predValue === gtValue) addMetric(metrics, metricId, true);
      else addIssue(issues, metrics, {
        ...base,
        key: `${base.rowKey}:metric:${role}`,
        field: `metric.${role}`,
        metricId,
        label: `${role} differs`,
        pred: predValue || "Missing",
        gt: gtValue || "Missing",
        suggestedCause: predValue && !gtValue ? "gt_incomplete" : "unresolved",
        suggestedReason: `Check ${role}, footnote markers, values, and product links in the source.`,
      });
    }
  }
}

export async function buildAudit(prediction, groundTruth, detections = []) {
  const issues = [];
  const metrics = {};
  if (!prediction || !groundTruth) return { issues, metrics };
  await compareCollection(
    "reaction_templates",
    prediction.reaction_templates || [],
    groundTruth.reaction_templates || [],
    issues,
    metrics,
    detections,
  );
  await compareCollection(
    "selected_examples",
    prediction.selected_examples || [],
    groundTruth.selected_examples || [],
    issues,
    metrics,
    detections,
  );
  return { issues, metrics };
}

export function adjudicatedMetric(metric, issues, annotations = {}) {
  const gtOwned = new Set(["suspected_gt_error", "gt_error", "gt_incomplete"]);
  const adjusted = (metric?.issueKeys || []).filter((key) =>
    gtOwned.has(annotations[key]?.cause)
  ).length;
  const correct = metric?.correct || 0;
  const total = metric?.total || 0;
  return {
    correct,
    total,
    adjustedCorrect: Math.min(total, correct + adjusted),
  };
}
