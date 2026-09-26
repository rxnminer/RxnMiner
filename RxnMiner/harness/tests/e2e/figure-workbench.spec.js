const { test, expect } = require("@playwright/test");
const path = require("node:path");

// Start `node ./rxnminer.mjs start` first. Pass the replay URL printed by `node ./rxnminer.mjs demo`
// as RXNMINER_WORKSPACE_URL to include the result-rendering check.
test("upload accepts an image and rejects PDF inputs", async ({ page }) => {
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  const upload = page.locator('input[type="file"]').first();
  await upload.setInputFiles({ name: "paper.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.7") });
  await expect(page.getByText("Choose a PNG, JPEG, WebP, or TIFF reaction image.", { exact: true })).toBeVisible();
  await upload.setInputFiles(path.resolve(__dirname, "../../product_samples/example-scheme-b.png"));
  await expect(page.getByText("Reaction figure ready.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Use this source", exact: true }).first()).toBeVisible();
  expect(errors).toEqual([]);
});

test("sealed image demo renders source boxes and molecular structures", async ({ page }, testInfo) => {
  test.skip(!process.env.RXNMINER_WORKSPACE_URL, "Set RXNMINER_WORKSPACE_URL to a running figure replay");
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(process.env.RXNMINER_WORKSPACE_URL);
  await expect(page.locator("#figureisland .figure-review-island")).toBeVisible();
  await expect(page.locator("#figureisland .mol-svg svg").first()).toBeVisible();
  await expect(page.locator("#figureisland [data-box-key]").first()).toBeVisible();
  expect(await page.locator("#figureisland [data-reaction-key]").count()).toBeGreaterThan(0);
  await page.screenshot({ path: testInfo.outputPath("figure-demo.png") });
  expect(errors).toEqual([]);
});
