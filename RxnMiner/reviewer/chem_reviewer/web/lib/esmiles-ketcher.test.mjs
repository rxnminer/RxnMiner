import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  assertEsmilesRoundtrip,
  exportEsmiles,
  hasEsmilesExtension,
  loadEsmiles,
  splitEsmiles,
  waitForEsmilesKetcher,
} from "./esmiles-ketcher.js";

test("splits ordinary and tagged E-SMILES without rewriting either lane", () => {
  assert.deepEqual(splitEsmiles("CCO"), {
    body: "CCO", extension: "", hasSeparator: false,
  });
  assert.deepEqual(splitEsmiles("*C<sep><a>0:R[1]</a>"), {
    body: "*C", extension: "<a>0:R[1]</a>", hasSeparator: true,
  });
  assert.equal(hasEsmilesExtension("CCO<sep>"), false);
  assert.equal(hasEsmilesExtension("*C<sep><a>0:R[1]</a>"), true);
});

test("roundtrip accepts body and coordinate reserialization with retained categories", () => {
  assert.equal(
    assertEsmilesRoundtrip(
      "*C(O)=O<sep><d>0:<dum></d>",
      "C(O)(=O)*<sep><d>3:<dum></d>",
    ),
    "C(O)(=O)*<sep><d>3:<dum></d>",
  );
});

test("roundtrip fails closed on blank, separator loss, extension loss, or tag loss", () => {
  const source = "*C<sep><a>0:R[1]</a><v>0:A:[0:1]</v>";
  assert.throws(() => assertEsmilesRoundtrip(source, ""), /empty E-SMILES/);
  assert.throws(() => assertEsmilesRoundtrip(source, "*C"), /missing.*<sep>/);
  assert.throws(() => assertEsmilesRoundtrip(source, "*C<sep>"), /dropped/);
  assert.throws(
    () => assertEsmilesRoundtrip(source, "*C<sep><a>0:R[1]</a>"),
    /<v>/,
  );
  assert.throws(
    () => assertEsmilesRoundtrip(
      "*C<sep><a>0:R[1]</a>",
      "C*<sep><a>1:R[2]</a>",
    ),
    /labels/,
  );
  assert.throws(
    () => assertEsmilesRoundtrip(
      "N.O<sep><v>0:A:[0:1]</v>",
      "N.O<sep><v>0:A:[8:9]</v>",
    ),
    /labels|repeat semantics/,
  );
  assert.throws(
    () => assertEsmilesRoundtrip(
      "C(*)(C*)<sep><g>[1:2]:|Sg:n|</g>",
      "C(*)(C*)<sep><g>[8:9]:|Sg:n|</g>",
    ),
    /labels|repeat semantics/,
  );
  assert.throws(
    () => assertEsmilesRoundtrip(
      "*C<sep><x>keep</x><a>0:R[1]</a>",
      "*C<sep><x>lost</x><a>0:R[1]</a>",
    ),
    /unsupported/,
  );
  assert.throws(
    () => assertEsmilesRoundtrip(
      "*C<sep><x value='keep'/><a>0:R[1]</a>",
      "C*<sep><x value='keep'/><a>1:R[1]</a>",
    ),
    /preserve unsupported.*exactly/,
  );
});

test("load and export require the pinned full E-SMILES API", async () => {
  let loaded = "";
  const ketcher = {
    async setMolecule(value) { loaded = value; },
    async getEsmiles() { return loaded; },
  };
  const source = "*c1ccccc1<sep><a>0:R[1]</a>";
  await loadEsmiles(ketcher, source);
  assert.equal(loaded, source);
  await loadEsmiles(ketcher, "");
  assert.equal(loaded, "");
  await loadEsmiles(ketcher, source);
  assert.equal(await exportEsmiles(ketcher, source), source);
  await assert.rejects(() => loadEsmiles({ setMolecule() {} }, source), /API/);
});

test("export reparses through pinned Ketcher and rejects invalid coordinate loss", async () => {
  let current = "*C<sep><a>999:R[1]</a>";
  const ketcher = {
    async setMolecule(value) {
      current = value.includes("<a>999:") ? "*C<sep>" : value;
    },
    async getEsmiles() { return current; },
  };
  await assert.rejects(
    () => exportEsmiles(ketcher, "*C<sep><a>0:R[1]</a>"),
    /dropped/,
  );
});

test("PDF island mounts the shared modal and legacy Canvas has no body-only save", () => {
  const island = readFileSync(new URL("../components/PdfReviewIsland.js", import.meta.url), "utf8");
  assert.match(island, /import \{ KetcherModal \}/);
  assert.match(island, /store\.ketcherTarget \? h\(KetcherModal\)/);

  const harness = readFileSync(
    new URL("../../../../harness/service/harness_index.html", import.meta.url),
    "utf8",
  );
  const start = harness.indexOf("// Generalized Ketcher modal");
  const end = harness.indexOf("// One delegated set of handlers", start);
  const editor = harness.slice(start, end);
  assert.match(editor, /loadEsmiles/);
  assert.match(editor, /exportEsmiles/);
  assert.doesNotMatch(editor, /getSmiles\s*\(/);
  assert.doesNotMatch(editor, /split\s*\(\s*["']<sep>/);

  const moleculeCard = readFileSync(
    new URL("../components/MoleculeCard.js", import.meta.url),
    "utf8",
  );
  assert.match(moleculeCard, /props\.readOnly && !props\.allowKetcher/);
  assert.match(island, /store\.ketcherTarget = null/);
});

test("waiter rejects body-only Ketcher and accepts the pinned API", async () => {
  assert.equal(await waitForEsmilesKetcher({
    ketcher: { setMolecule() {}, getSmiles() {} },
  }, 5), null);
  const expected = { setMolecule() {}, getEsmiles() {} };
  assert.equal(await waitForEsmilesKetcher({ ketcher: expected }, 5), expected);
});
