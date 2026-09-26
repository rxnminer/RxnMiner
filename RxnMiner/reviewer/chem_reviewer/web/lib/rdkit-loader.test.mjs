import assert from "node:assert/strict";
import { test } from "node:test";

test("embedded chemistry islands lazily bootstrap the shared local RDKit renderer", async () => {
  const originalWindow = globalThis.window;
  const originalDocument = globalThis.document;
  let appended = null;
  try {
    globalThis.window = {};
    globalThis.document = {
      querySelector() {
        return null;
      },
      createElement(tag) {
        assert.equal(tag, "script");
        return {
          dataset: {},
          addEventListener() {},
        };
      },
      head: {
        appendChild(script) {
          appended = script;
          globalThis.window.initRDKitModule = async ({ locateFile }) => {
            assert.equal(locateFile(), "/static/lib/RDKit_minimal.wasm");
            return {
              get_mol(smiles) {
                assert.equal(smiles, "CCO");
                return {
                  is_valid: () => true,
                  get_svg: (width, height) => `<svg data-size="${width}x${height}"/>`,
                  delete() {},
                };
              },
            };
          };
        },
      },
    };
    const loader = await import(`./rdkit-loader.js?embedded-test=${Date.now()}`);
    const svg = await loader.renderSmilesToSvg("CCO", { width: 180, height: 120 });
    assert.equal(appended?.src, "/static/lib/RDKit_minimal.js");
    assert.equal(appended?.async, true);
    assert.equal(appended?.dataset?.chemrxnRdkit, "true");
    assert.equal(svg, '<svg data-size="180x120"/>');
  } finally {
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
    if (originalDocument === undefined) delete globalThis.document;
    else globalThis.document = originalDocument;
  }
});
