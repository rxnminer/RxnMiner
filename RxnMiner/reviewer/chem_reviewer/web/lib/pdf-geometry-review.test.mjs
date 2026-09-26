import assert from "node:assert/strict";
import test from "node:test";

import {
  documentBoxToLocal,
  formatGeometryLocation,
  geometryRequest,
  geometryTargetsForRow,
  localBoxToDocument,
  locationToPdfPoints,
} from "./pdf-geometry-review.js";

const row = {
  id: "pred:12:0",
  evidence: {
    source_document: "si",
    anchor_page: 7,
    reactants: [{
      identifier: "4",
      occurrence: {
        occurrence_id: "si:a12:r0:reactant:0",
        source_document: "si",
        page: 7,
      },
      bbox: [10, 20, 30, 40],
      bbox_page: 7,
      bbox_frame: "pdf_points",
    }],
    products: [],
    conditions: [[{
      role: "temperature",
      text: "-20 °C",
      _source: { origin: "image", source_document: "si" },
    }]],
    metrics: [{
      role: "yield",
      text: "45%",
      _source: {
        origin: "image",
        source_document: "main",
        bbox_page: 3,
        bbox_frame: "pdf_points",
        bbox: [100, 120, 140, 135],
      },
    }],
  },
};

test("targets retain exact namespaced occurrences and deterministic claim paths", () => {
  const targets = geometryTargetsForRow(row);
  assert.deepEqual(targets.map((item) => item.target_id), [
    "si:a12:r0:reactant:0",
    "pred:12:0:conditions:0:0",
    "pred:12:0:metrics:0",
  ]);
  assert.deepEqual(targets[1].target_path, ["conditions", 0, 0]);
  assert.deepEqual(targets[1].location, {
    source_document: "si",
    page: 7,
    bbox_frame: null,
    bbox: null,
  });
  assert.equal(formatGeometryLocation(targets[2].location), "Main · p3 · 100.0, 120.0, 140.0, 135.0");
});

test("duplicate claim labels cannot collapse distinct condition occurrences", () => {
  const duplicateClaims = structuredClone(row);
  duplicateClaims.evidence.conditions = [[
    {
      claim_id: "legacy-claim",
      role: "reagent",
      text: "NaCl",
      _source: { source_document: "main" },
    },
    {
      claim_id: "legacy-claim",
      role: "reagent",
      text: "NaCl",
      _source: { source_document: "main" },
    },
  ]];
  const conditionTargets = geometryTargetsForRow(duplicateClaims)
    .filter((item) => item.target_type === "condition");
  assert.deepEqual(conditionTargets.map((item) => item.target_id), [
    "pred:12:0:conditions:0:0",
    "pred:12:0:conditions:0:1",
  ]);
  assert.equal(new Set(conditionTargets.map((item) => item.target_id)).size, 2);
});

test("anchor crops translate through one explicit viewport origin", () => {
  const viewer = {
    sourceDocument: "si",
    page: 7,
    coordinateFrame: "pdf_points",
    viewportOrigin: { x: 80, y: 120 },
  };
  assert.deepEqual(
    localBoxToDocument([5, 10, 25, 30], viewer),
    [85, 130, 105, 150],
  );
  assert.deepEqual(
    documentBoxToLocal({
      source_document: "si",
      page: 7,
      bbox: [85, 130, 105, 150],
    }, viewer),
    [5, 10, 25, 30],
  );
  assert.equal(documentBoxToLocal({
    source_document: "main",
    page: 7,
    bbox: [85, 130, 105, 150],
  }, viewer), null);
});

test("144 dpi runtime SI maps through canonical PDF points without drift", () => {
  const viewer = {
    sourceDocument: "si",
    page: 9,
    coordinateFrame: "page_render_px",
    coordinateDpi: 144,
    viewportOrigin: { x: 80, y: 120 },
  };
  assert.deepEqual(
    localBoxToDocument([20, 40, 100, 120], viewer),
    [50, 80, 90, 120],
  );
  assert.deepEqual(
    documentBoxToLocal({
      source_document: "si",
      page: 9,
      bbox_frame: "pdf_points",
      bbox: [50, 80, 90, 120],
    }, viewer),
    [20, 40, 100, 120],
  );
  assert.deepEqual(locationToPdfPoints({
    source_document: "si",
    page: 9,
    bbox_frame: "page_render_px",
    bbox: [100, 160, 180, 240],
  }, {
    coordinateFrame: "page_render_px",
    coordinateDpi: 144,
  }), {
    source_document: "si",
    page: 9,
    bbox_frame: "pdf_points",
    bbox: [50, 80, 90, 120],
  });
});

test("pixel-frame raw evidence fails closed without its own trusted DPI", () => {
  const rawLocation = {
    source_document: "si",
    page: 9,
    bbox_frame: "page_render_px",
    bbox: [100, 160, 180, 240],
  };
  const normalizedDocument = {
    source: "si",
    coordinateFrame: "pdf_points",
    coordinateDpi: 72,
  };
  assert.equal(locationToPdfPoints(rawLocation, normalizedDocument), null);
  assert.equal(documentBoxToLocal(rawLocation, {
    sourceDocument: "si",
    page: 9,
    coordinateFrame: "pdf_points",
    coordinateDpi: 72,
    viewportOrigin: { x: 0, y: 0 },
  }), null);

  assert.deepEqual(locationToPdfPoints({
    ...rawLocation,
    coordinateDpi: 144,
  }, normalizedDocument), {
    source_document: "si",
    page: 9,
    bbox_frame: "pdf_points",
    bbox: [50, 80, 90, 120],
  });
});

test("unsupported crop coordinates never masquerade as PDF points", () => {
  assert.equal(locationToPdfPoints({
    source_document: "main",
    page: 2,
    bbox_frame: "crop_px_unnormalized",
    bbox: [10, 20, 30, 40],
  }, {
    coordinateFrame: "pdf_points",
    coordinateDpi: 72,
  }), null);
});

test("geometry request is bounded to one row target and canonical PDF points", () => {
  const target = geometryTargetsForRow(row)[1];
  const request = geometryRequest(row, target, {
    source_document: "main",
    page: 2,
    bbox: [1, 2, 30, 40],
  }, 4, "located in the main method figure");
  assert.deepEqual(request, {
    operation: "locate_pdf_geometry",
    base_version: 4,
    target: {
      difference_id: "pred:12:0",
      target_type: "condition",
      target_id: "pred:12:0:conditions:0:0",
      target_path: ["conditions", 0, 0],
    },
    after: {
      source_document: "main",
      page: 2,
      bbox_frame: "pdf_points",
      bbox: [1, 2, 30, 40],
    },
    note: "located in the main method figure",
    proposed_at: request.proposed_at,
  });
  assert.match(request.proposed_at, /^\d{4}-\d\d-\d\dT/);
  assert.equal(geometryRequest(row, target, {
    source_document: "main",
    page: 2,
    bbox: [1, 2, 1, 40],
  }, 4), null);
});
