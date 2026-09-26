function finiteBox(value) {
  if (!Array.isArray(value) || value.length !== 4) return null;
  const box = value.map(Number);
  return box.every(Number.isFinite) && box[0] < box[2] && box[1] < box[3]
    ? box : null;
}

function sourceDocument(value, fallback = "main") {
  const source = value?.source_document ?? value?.sourceDocument ?? value?.src;
  return source === "si" ? "si" : source === "main" ? "main" : fallback;
}

function coordinateScale(value) {
  const frame = value?.bbox_frame || value?.coordinateFrame || "pdf_points";
  if (frame === "pdf_points") return 1;
  if (frame !== "page_render_px") return null;
  const dpi = Number(value?.coordinateDpi || value?.coordinate_dpi);
  return Number.isFinite(dpi) && dpi > 0 ? dpi / 72 : null;
}

function evidenceLocation(value, fallback = {}) {
  const raw = value?._source && typeof value._source === "object"
    ? value._source : {};
  const normalized = value?.source && typeof value.source === "object"
    ? value.source : {};
  const occurrence = value?.occurrence && typeof value.occurrence === "object"
    ? value.occurrence : {};
  const source = { ...normalized, ...raw };
  const bbox = finiteBox(value?.bbox) || finiteBox(source.bbox);
  const page = Number(
    value?.bbox_page ?? source.bbox_page ?? source.page ?? occurrence.page,
  );
  const location = {
    source_document: sourceDocument(
      source,
      sourceDocument(occurrence, fallback.source_document || "main"),
    ),
    page: Number.isInteger(page) && page > 0 ? page : fallback.page || null,
    bbox_frame: value?.bbox_frame || source.bbox_frame || (bbox ? "pdf_points" : null),
    bbox,
  };
  const coordinateDpi = Number(
    value?.coordinateDpi
      ?? value?.coordinate_dpi
      ?? source.coordinateDpi
      ?? source.coordinate_dpi,
  );
  if (Number.isFinite(coordinateDpi) && coordinateDpi > 0) {
    location.coordinateDpi = coordinateDpi;
  }
  return location;
}

function targetLabel(prefix, value, fallback) {
  const suffix = String(value || "").trim();
  return suffix ? `${prefix} · ${suffix}` : fallback;
}

export function geometryTargetsForRow(row) {
  const evidence = row?.evidence;
  if (!evidence || typeof evidence !== "object") return [];
  const fallback = {
    source_document: sourceDocument(evidence, "main"),
    page: Number(evidence.anchor_page) || null,
  };
  const targets = [];
  for (const [side, type] of [["reactants", "Reactant"], ["products", "Product"]]) {
    for (const [index, molecule] of (evidence[side] || []).entries()) {
      const targetId = molecule?.occurrence?.occurrence_id;
      if (typeof targetId !== "string" || !targetId) continue;
      targets.push({
        target_type: "molecule",
        target_id: targetId,
        target_path: [side, index],
        label: targetLabel(
          `${type} ${index + 1}`,
          molecule.identifier,
          `${type} ${index + 1}`,
        ),
        location: evidenceLocation(molecule, fallback),
      });
    }
  }
  for (const [stepIndex, rawStep] of (evidence.conditions || []).entries()) {
    const step = Array.isArray(rawStep) ? rawStep : [rawStep];
    for (const [itemIndex, item] of step.entries()) {
      if (!item || typeof item !== "object") continue;
      const targetId = String(
        `${row.id}:conditions:${stepIndex}:${itemIndex}`,
      );
      targets.push({
        target_type: "condition",
        target_id: targetId,
        target_path: ["conditions", stepIndex, itemIndex],
        label: targetLabel(
          item.role || "Condition",
          item.text || item.description,
          `Condition ${stepIndex + 1}.${itemIndex + 1}`,
        ),
        location: evidenceLocation(item, fallback),
      });
    }
  }
  for (const [index, metric] of (evidence.metrics || []).entries()) {
    if (!metric || typeof metric !== "object") continue;
      const targetId = `${row.id}:metrics:${index}`;
    targets.push({
      target_type: "metric",
      target_id: targetId,
      target_path: ["metrics", index],
      label: targetLabel(
        metric.role || metric.type || "Metric",
        metric.text || metric.content || metric.value,
        `Metric ${index + 1}`,
      ),
      location: evidenceLocation(metric, fallback),
    });
  }
  return targets;
}

export function localBoxToDocument(box, viewer) {
  const normalized = finiteBox(box);
  if (!normalized) return null;
  const scale = coordinateScale(viewer);
  if (!scale) return null;
  const origin = viewer?.viewportOrigin || { x: 0, y: 0 };
  const x = Number(origin.x) || 0;
  const y = Number(origin.y) || 0;
  return normalized.map(
    (value, index) => (value + (index % 2 ? y : x)) / scale,
  );
}

export function documentBoxToLocal(location, viewer) {
  if (!location || sourceDocument(location) !== sourceDocument(viewer)) return null;
  if (Number(location.page) !== Number(viewer?.page)) return null;
  const box = finiteBox(location.bbox);
  if (!box) return null;
  const sourceScale = coordinateScale(location);
  const targetScale = coordinateScale(viewer);
  if (!sourceScale || !targetScale) return null;
  const origin = viewer?.viewportOrigin || { x: 0, y: 0 };
  const x = Number(origin.x) || 0;
  const y = Number(origin.y) || 0;
  return box.map(
    (value, index) =>
      (value / sourceScale) * targetScale - (index % 2 ? y : x),
  );
}

export function locationToPdfPoints(location, document = {}) {
  if (!location) return null;
  const box = finiteBox(location.bbox);
  if (!box) return null;
  const frame = location.bbox_frame || document.coordinateFrame || "pdf_points";
  // A normalized document catalog cannot supply the render DPI for legacy raw
  // evidence. Pixel-frame evidence is convertible only when its own lineage
  // carries an explicit DPI, or when the document itself is still in the exact
  // same render-pixel frame. A canonical pdf_points catalog must never lend its
  // 72 DPI to raw render pixels.
  const coordinateDpi = (
    location.coordinateDpi
    ?? location.coordinate_dpi
    ?? (
      frame === "page_render_px"
      && document.coordinateFrame === "page_render_px"
        ? document.coordinateDpi ?? document.coordinate_dpi
        : null
    )
  );
  const scale = coordinateScale({
    bbox_frame: frame,
    coordinateDpi,
  });
  if (!scale) return null;
  return {
    source_document: sourceDocument(location),
    page: Number(location.page),
    bbox_frame: "pdf_points",
    bbox: box.map((value) => value / scale),
  };
}

export function formatGeometryLocation(location) {
  if (!location) return "unlocated";
  const source = sourceDocument(location) === "si" ? "SI" : "Main";
  const box = finiteBox(location.bbox);
  return `${source} · p${location.page || "?"} · ${
    box ? box.map((value) => Number(value).toFixed(1)).join(", ") : "no bbox"
  }`;
}

export function geometryRequest(row, target, location, baseVersion, note = "") {
  const bbox = finiteBox(location?.bbox);
  if (!row?.id || !target?.target_id || !bbox) return null;
  return {
    operation: "locate_pdf_geometry",
    base_version: baseVersion,
    target: {
      difference_id: row.id,
      target_type: target.target_type,
      target_id: target.target_id,
      target_path: target.target_path,
    },
    after: {
      source_document: sourceDocument(location),
      page: Number(location.page),
      bbox_frame: "pdf_points",
      bbox,
    },
    note: String(note || ""),
    proposed_at: new Date().toISOString(),
  };
}
