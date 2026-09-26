const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: ".",
  testMatch: "product-release-smoke.spec.js",
  fullyParallel: false,
  workers: 1,
  timeout: 30000,
  expect: { timeout: 10000 },
  outputDir: process.env.CHEMRXN_RELEASE_EVIDENCE || "release-results",
  use: {
    viewport: { width: 1600, height: 1000 },
    trace: "retain-on-failure",
  },
});
