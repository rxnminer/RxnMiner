import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const panel = readFileSync(
  new URL("../components/BBoxImagePanel.js", import.meta.url),
  "utf8",
);

test("non-selectable raw evidence boxes cannot intercept prediction molecule clicks", () => {
  assert.match(panel, /"pointer-events": box\.selectable === false \? "none" : undefined/);
});
