// Pure helpers for linking figure bboxes to V2 reaction rows.
// A drawn molecule normally carries _source.bbox_index and _source.bbox.
// Derived reactants may have neither; a reaction is still linked through its drawn product.

function sameBox(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length < 4 || b.length < 4) {
    return false;
  }
  return a.slice(0, 4).every((value, i) => Number(value) === Number(b[i]));
}

function visitMolecules(reaction, callback) {
  // Product first: for substrate-scope rows the concrete product bbox is the
  // most useful anchor; reactants often inherit the shared top-template boxes.
  for (const side of ["products", "reactants"]) {
    for (const molecule of reaction?.[side] || []) {
      if (molecule && typeof molecule === "object") callback(molecule);
    }
  }
  for (const step of reaction?.conditions || []) {
    for (const molecule of step || []) {
      if (molecule && typeof molecule === "object") callback(molecule);
    }
  }
}

export function bboxKey(bbox) {
  if (bbox?.bbox_index !== undefined && bbox?.bbox_index !== null) {
    return `idx:${bbox.bbox_index}`;
  }
  if (Array.isArray(bbox?.bbox) && bbox.bbox.length >= 4) {
    return `box:${bbox.bbox.slice(0, 4).map(Number).join(",")}`;
  }
  return "";
}

export function reactionBboxes(reaction) {
  const direct = [];
  const inherited = [];
  const seen = new Set();
  visitMolecules(reaction, (molecule) => {
    const source = molecule._source || {};
    const target = {
      bbox_index:
        source.bbox_index ?? molecule.bbox_index ?? null,
      bbox: source.bbox ?? molecule.bbox ?? null,
    };
    const key = bboxKey(target);
    if (!key || seen.has(key)) return;
    seen.add(key);
    // Concrete scope reactants may inherit the shared top-template bbox.
    // Prefer boxes actually drawn for this row (normally its product); use
    // inherited boxes only when a row has no direct visual anchor at all.
    (source._bbox_inherited ? inherited : direct).push(target);
  });
  return direct.length ? direct : inherited;
}

export function reactionTouchesBbox(reaction, target) {
  const targetKey = bboxKey(target);
  return reactionBboxes(reaction).some((candidate) => {
    const candidateKey = bboxKey(candidate);
    if (targetKey && candidateKey === targetKey) return true;
    return sameBox(candidate.bbox, target?.bbox);
  });
}

export function reactionsForBbox(taskData, target) {
  const out = [];
  for (let index = 0; index < (taskData?.reaction_templates || []).length; index++) {
    const reaction = taskData.reaction_templates[index];
    if (reactionTouchesBbox(reaction, target)) {
      out.push({
        kind: "template",
        index,
        key: `template:${index}`,
        label: reaction.template_id || `T${index + 1}`,
      });
    }
  }
  for (let index = 0; index < (taskData?.selected_examples || []).length; index++) {
    const reaction = taskData.selected_examples[index];
    if (reactionTouchesBbox(reaction, target)) {
      out.push({
        kind: "example",
        index,
        key: `example:${index}`,
        label: reaction.instance_id || `example ${index + 1}`,
      });
    }
  }
  return out;
}

export function reactionElementId(key) {
  return `reaction-${String(key || "").replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}
