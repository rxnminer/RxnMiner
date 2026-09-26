const { evidenceOutputDir, rejectOutputOverride } = require("./playwright-output-guard");

rejectOutputOverride();

const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: ".",
  testMatch: "figure-workbench.spec.js",
  fullyParallel: false,
  workers: 1,
  timeout: 30000,
  expect: { timeout: 8000 },
  outputDir: evidenceOutputDir(),
  use: {
    baseURL: process.env.RXNMINER_BASE_URL || "http://127.0.0.1:8787",
    viewport: { width: 1600, height: 1000 },
    trace: "retain-on-failure",
  },
});
