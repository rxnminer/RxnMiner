import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const island = readFileSync(
  new URL("../components/PdfReviewIsland.js", import.meta.url),
  "utf8",
);
const css = readFileSync(new URL("../pdf-reviewer-v2.css", import.meta.url), "utf8");

test("Evidence renders only concrete E-SMILES condition claims as molecule cards", () => {
  assert.match(island, /const conditionEsmiles = typeof item\["E-SMILES"\] === "string"/);
  assert.match(island, /structure: conditionEsmiles \?/);
  assert.match(island, /data-condition-structure/);
  assert.match(island, /h\(MoleculeCard, \{/);
  assert.match(island, /renderSrc: `\/api\/render\?smi=\$\{encodeURIComponent\(claim\.structure\.esmiles\)\}&w=168&h=126`/);
  assert.match(island, /condition-structure-esmiles/);
});

test("condition structure cards have dedicated responsive Evidence styling", () => {
  assert.match(css, /\.condition-evidence-structure \{/);
  assert.match(css, /\.condition-evidence-structure \.mol-card \{/);
  assert.match(css, /\.condition-structure-esmiles \{/);
  assert.match(css, /overflow-wrap: anywhere/);
});
