import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../components/PdfReviewIsland.js", import.meta.url),
  "utf8",
);

test("product workspace surfaces Markush, failed, and unsupported ledger outcomes", () => {
  assert.match(source, /"Markush template": \{ severity: "info"/);
  assert.match(source, /template\?\.kind === "markush"/);
  assert.match(source, /source-grounded generic Markush reaction/);
  assert.match(source, /function conditionAliasStructureStrip/);
  assert.match(source, /Structurally drawn condition aliases expanded to concrete E-SMILES/);
  assert.match(source, /"failed outcome": \{ severity: "review"/);
  assert.match(source, /reportability === "failed_outcome"/);
  assert.match(source, /exception\?\.kind === "unsupported_structure"/);
  assert.match(source, /No desired product/);
  assert.match(source, /exception\?\.reason/);
});
