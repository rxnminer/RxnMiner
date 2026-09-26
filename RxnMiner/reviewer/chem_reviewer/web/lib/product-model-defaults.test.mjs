import assert from "node:assert/strict";
import test from "node:test";
import {
  defaultProductApiModel,
  PRODUCT_API_PROVIDER_OPTIONS,
  productApiProvider,
} from "./product-model-defaults.js";

test("product BYO provider contract is explicit and immutable", () => {
  assert.deepEqual(
    PRODUCT_API_PROVIDER_OPTIONS.map((option) => option.registryId),
    ["byo-openai", "byo-anthropic", "byo-gemini"],
  );
  assert.equal(Object.isFrozen(PRODUCT_API_PROVIDER_OPTIONS), true);
  assert.equal(PRODUCT_API_PROVIDER_OPTIONS.every(Object.isFrozen), true);
});

test("product provider lookup has no implicit fallback", () => {
  assert.equal(defaultProductApiModel("byo-openai"), "gpt-5.6-terra");
  assert.equal(productApiProvider("not-connected"), undefined);
  assert.equal(defaultProductApiModel("not-connected"), "");
});
