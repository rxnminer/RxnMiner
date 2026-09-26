import assert from "node:assert/strict";
import test from "node:test";

import {
  hasValidEvidenceBbox,
  imageClaimMissingBbox,
  isImageClaimSource,
  orderedVerificationRows,
  provenanceNeedsAttention,
  verificationDimensions,
  workspaceChecks,
} from "./review-workspace-diagnostics.js";

function row(id, overrides = {}) {
  return {
    id,
    label: id,
    source: { pred: { metrics: [] }, ...overrides.source },
    evidence: {
      reactants: [],
      products: [],
      metrics: [],
      ...overrides.evidence,
    },
    dimensions: {
      chemistry: "unknown",
      conditions: "unknown",
      grounding: "pass",
      provenance: "pass",
      identifier: "unknown",
      metrics: "unknown",
      ...overrides.dimensions,
    },
    diagnostics: overrides.diagnostics || {},
    claims: overrides.claims || [],
  };
}

test("verification never turns an absent reference diagnostic into a pass", () => {
  const facts = verificationDimensions(row("pred:0:0"));
  assert.equal(facts.find((item) => item.key === "chemistry")?.status, "unchecked");
  assert.equal(facts.find((item) => item.key === "stereo")?.status, "unchecked");
  assert.equal(facts.find((item) => item.key === "grounding")?.status, "pass");
  assert.equal(facts.find((item) => item.key === "human")?.status, "pending");
});

test("strict stereo remains independent from connectivity and human review", () => {
  const source = row("pred:0:1", {
    dimensions: { chemistry: "pass" },
    diagnostics: { strict_stereo: { exact: false } },
  });
  const facts = verificationDimensions(source, { verdict: "correct fields" });
  assert.equal(facts.find((item) => item.key === "chemistry")?.status, "pass");
  assert.equal(facts.find((item) => item.key === "stereo")?.status, "diff");
  assert.equal(facts.find((item) => item.key === "human")?.status, "reviewed");
});

test("verification queue orders substantive risk before unchecked rows", () => {
  const unchecked = row("unchecked");
  const mismatch = row("mismatch", { dimensions: { conditions: "diff" } });
  assert.deepEqual(
    orderedVerificationRows([unchecked, mismatch]).map((item) => item.id),
    ["mismatch", "unchecked"],
  );
});

test("checks use recorded artifacts and label missing diagnostics unavailable", () => {
  const rows = [
    row("first", {
      evidence: {
        reactants: [{
          identifier: "4",
          smi: "CC",
          source: { class: "molparser_raw" },
          raw_molparser: { md5: "a" },
          bbox: [1, 2, 3, 4],
        }],
        metrics: [{ role: "yield", text: "45%" }],
      },
    }),
    row("second", {
      evidence: {
        products: [{
          identifier: "4",
          smi: "CCC",
          source: { class: "image_untraced", derivation_method: "untracked" },
        }],
      },
    }),
  ];
  const checks = workspaceChecks(rows);
  assert.equal(checks.find((item) => item.id === "identifier-consistency")?.status, "flag");
  assert.equal(checks.find((item) => item.id === "lineage")?.status, "flag");
  assert.equal(checks.find((item) => item.id === "metric-range")?.status, "pass");
  assert.equal(checks.find((item) => item.id === "conditions")?.status, "unavailable");
  assert.equal(checks.find((item) => item.id === "strict-stereo")?.status, "unavailable");
});

test("percentage range check rejects impossible recorded values", () => {
  const checks = workspaceChecks([
    row("bad-yield", {
      evidence: { metrics: [{ role: "yield", text: "145%" }] },
    }),
  ]);
  const metric = checks.find((item) => item.id === "metric-range");
  assert.equal(metric?.status, "flag");
  assert.deepEqual(metric?.affectedIds, ["bad-yield"]);
});

test("lineage check fails closed when no source class or raw record exists", () => {
  const checks = workspaceChecks([
    row("missing-source", {
      evidence: {
        reactants: [{ identifier: "7", smi: "CC", bbox: [1, 2, 3, 4] }],
      },
    }),
  ]);
  const lineage = checks.find((item) => item.id === "lineage");
  assert.equal(lineage?.status, "flag");
  assert.deepEqual(lineage?.affectedIds, ["missing-source"]);
});

test("unverified FHR claims and counterparty stereo mismatches require provenance review", () => {
  assert.equal(provenanceNeedsAttention({
    source: { derivation_status: { audit_status: "handoff_inventory_match" } },
  }), false);
  assert.equal(provenanceNeedsAttention({
    source: { derivation_status: { roundtrip_ok: true } },
  }), true);
  assert.equal(provenanceNeedsAttention({
    source: {
      derivation_status: {
        reported_roundtrip_ok: true,
        audit_status: "unverified_no_row_level_fhr_proof",
      },
    },
  }), true);
  assert.equal(provenanceNeedsAttention({
    source: {
      derivation_status: {
        reported_roundtrip_ok: true,
        audit_status: "counterparty_stereo_mismatch_unverified",
      },
    },
  }), true);
  assert.equal(provenanceNeedsAttention({
    source: {
      derivation_status: {
        roundtrip_ok: true,
        audit_status: "bogus_future_status",
      },
    },
  }), true);
  assert.equal(provenanceNeedsAttention({
    source: { derivation_status: { audit_status: "bogus_future_status" } },
  }), true);
  assert.equal(provenanceNeedsAttention({
    source: {
      derivation_status: {
        roundtrip_ok: true,
        audit_status: "handoff_inventory_match",
      },
    },
  }), false);
  assert.equal(provenanceNeedsAttention({
    source: { class: "identifier_resolution" },
  }), true);
  assert.equal(provenanceNeedsAttention({
    source: { class: "identifier_resolution" },
    raw_molparser: { md5: "a" },
  }), false);
  assert.equal(provenanceNeedsAttention({
    source: {
      class: "molparser_raw",
      lineage_contract: "legacy_unsealed",
    },
    raw_molparser: { md5: "a" },
  }), true);
});

test("type-only image OCR claims remain unresolved until PDF geometry exists", () => {
  assert.equal(isImageClaimSource({ type: "image_ocr" }), true);
  assert.equal(
    isImageClaimSource({ type: "image_ocr_with_footnote_resolved" }),
    true,
  );
  assert.equal(imageClaimMissingBbox({ type: "image_ocr" }), true);
  assert.equal(
    imageClaimMissingBbox({
      type: "image_ocr_with_footnote_resolved",
      bbox: [1, 2, 3, 4],
    }),
    false,
  );
  assert.equal(imageClaimMissingBbox({ origin: "image" }), true);
  assert.equal(imageClaimMissingBbox({ origin: "text" }), false);
  assert.equal(
    imageClaimMissingBbox({ origin: "text", type: "image_ocr" }),
    false,
  );
});

test("claim grounding accepts only finite, ordered four-coordinate bboxes", () => {
  assert.equal(hasValidEvidenceBbox([1, 2, 3, 4]), true);
  assert.equal(hasValidEvidenceBbox(["1", "2", "3", "4"]), false);
  assert.equal(hasValidEvidenceBbox([null, 2, 3, 4]), false);
  assert.equal(hasValidEvidenceBbox([false, 2, 3, 4]), false);
  assert.equal(hasValidEvidenceBbox(["", 2, 3, 4]), false);
  assert.equal(hasValidEvidenceBbox("1,2,3,4"), false);
  assert.equal(hasValidEvidenceBbox([1, 2, 3]), false);
  assert.equal(hasValidEvidenceBbox([1, 2, Number.NaN, 4]), false);
  assert.equal(hasValidEvidenceBbox([3, 2, 1, 4]), false);
  assert.equal(hasValidEvidenceBbox([1, 4, 3, 2]), false);
  assert.equal(imageClaimMissingBbox({ type: "image_ocr", bbox: "1,2,3,4" }), true);
  assert.equal(imageClaimMissingBbox({ type: "image_ocr", bbox: [3, 2, 1, 4] }), true);
});
