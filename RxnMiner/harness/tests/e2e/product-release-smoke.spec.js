const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;
const { createHash } = require("node:crypto");
const { readFileSync, writeFileSync } = require("node:fs");

const RELEASE_URLS = Object.freeze({
  home: process.env.CHEMRXN_RELEASE_HOME_URL || "",
  figure: process.env.CHEMRXN_RELEASE_FIGURE_URL || "",
});
const LIVE_FIGURE = process.env.CHEMRXN_RELEASE_LIVE_FIGURE || "";
const LIVE_MODEL = process.env.CHEMRXN_RELEASE_LIVE_MODEL || "";
const LIVE_TOOL_MODEL = process.env.CHEMRXN_RELEASE_LIVE_TOOL_MODEL || "";
const LOOPBACK_LOG = process.env.CHEMRXN_RELEASE_LOOPBACK_LOG || "";

function releaseUrl(kind) {
  const value = RELEASE_URLS[kind];
  if (!value) {
    throw new Error(`CHEMRXN_RELEASE_${kind.toUpperCase()}_URL is required`);
  }
  return value;
}

async function expectInViewport(locator) {
  await expect.poll(async () => locator.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    return bounds.width > 0
      && bounds.height > 0
      && bounds.right > 0
      && bounds.bottom > 0
      && bounds.left < window.innerWidth
      && bounds.top < window.innerHeight;
  })).toBe(true);
}

async function openProductModule(root, name) {
  const labels = {
    Reactions: "All reactions",
    Evidence: "Reaction details",
    Edit: "Edit",
  };
  const button = root.getByRole("button", { name: labels[name] || name, exact: true });
  if (!(await button.isVisible())) {
    await root.locator(".workspace-more summary").click();
  }
  await button.click();
}

async function openDocumentReview(page) {
  const reviewer = page.locator(".pdf-reviewer-v2");
  const reviewTab = page.getByRole("tab", { name: "Review", exact: true });
  await expect.poll(async () => (
    (await reviewer.isVisible()) || (await reviewTab.isVisible())
  )).toBe(true);
  if (!(await reviewer.isVisible())) await reviewTab.click();
  await expect(reviewer).toBeVisible();
  return reviewer;
}

async function expectRuntimeThinking(page, sessionId, expected) {
  await expect.poll(async () => {
    const response = await page.request.get(
      new URL(`/api/runtime/sessions/${sessionId}/event-log?after=0`, page.url()).href,
    );
    if (!response.ok()) return null;
    const body = await response.json();
    return (body.events || []).find((event) => event.type === "session.created")
      ?.data?.thinkingLevel || null;
  }).toBe(expected);
}

test("cached BBox images settle once across reactive parent renders", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  await page.goto(releaseUrl("home"), { waitUntil: "domcontentloaded" });
  const result = await page.evaluate(async () => {
    const { createApp, h, nextTick, reactive } = await import(
      "/static/lib/vue.esm-browser.prod.js"
    );
    const { BBoxImagePanel } = await import("/static/components/BBoxImagePanel.js");
    const host = document.createElement("div");
    host.id = "bbox-cached-settlement-host";
    document.body.append(host);
    const state = reactive({ coordinateSize: null, unrelatedRenders: 0 });
    let loadCalls = 0;
    const imageUrl = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80"></svg>',
    );
    const app = createApp({
      render: () => h("div", { "data-render": state.unrelatedRenders }, [
        h(BBoxImagePanel, {
          imageUrl,
          coordinateSize: state.coordinateSize,
          alt: "Cached settlement regression fixture",
          // Intentionally inline: its identity changes after every parent render.
          onImageLoad: (event) => {
            loadCalls += 1;
            state.coordinateSize = {
              w: event.currentTarget.naturalWidth,
              h: event.currentTarget.naturalHeight,
            };
          },
        }),
      ]),
    });
    app.mount(host);
    await new Promise((resolve) => setTimeout(resolve, 250));
    state.unrelatedRenders += 1;
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 100));
    const output = {
      loadCalls,
      width: state.coordinateSize?.w || 0,
      height: state.coordinateSize?.h || 0,
    };
    app.unmount();
    host.remove();
    return output;
  });
  expect(result).toEqual({ loadCalls: 1, width: 120, height: 80 });
  expect(pageErrors).toEqual([]);
});

function isDisposedPreviewAbort(request) {
  // Live-evidence previews are already-fetched, in-memory object URLs. When a
  // sealed workspace replaces that transient pane (or the test reloads),
  // Chromium may report disposal as requestfailed. Keep every HTTP failure and
  // every non-abort blob failure visible to the acceptance suite.
  return request.url().startsWith("blob:")
    && request.failure()?.errorText === "net::ERR_ABORTED";
}

async function expectNoWcagViolations(page, label) {
  const result = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const violations = result.violations.map((violation) => ({
    id: violation.id,
    impact: violation.impact,
    help: violation.help,
    targets: violation.nodes.map((node) => node.target),
  }));
  expect(violations, `${label} must pass automated WCAG A/AA checks`).toEqual([]);
}

async function holdLinkedSessionUntilProposalIsDurable(
  page,
  productApi,
  parentSessionId,
) {
  let proposalPersisted = false;
  let persistedCommands = [];
  let resolvePersisted;
  const persistedPromise = new Promise((resolve) => {
    resolvePersisted = resolve;
  });
  const routePattern = `**/api/runtime/sessions/${parentSessionId}/messages`;
  const handler = async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const linkedId = body?.session?.id;
    // Let the product switch to the linked session immediately. Keeping the
    // browser POST intercepted while the test polls the durable store can
    // starve the new panel's event-log request behind image connections and
    // does not match the real API lifecycle.
    await route.fulfill({ response, json: body });
    if (body?.continued === true && linkedId) {
      for (let attempt = 0; attempt < 100; attempt += 1) {
        const historyResponse = await page.request.get(
          productApi(`/api/runtime/sessions/${linkedId}/event-log?after=0`),
        );
        if (historyResponse.ok()) {
          const history = await historyResponse.json();
          persistedCommands = (history.events || [])
            .filter((runtimeEvent) =>
              runtimeEvent.type === "agent.event"
              && runtimeEvent.data?.phase === "workspace.command")
            .map((runtimeEvent) => runtimeEvent.data.workspaceCommand);
          proposalPersisted = persistedCommands.some((command) =>
            command?.type === "review.propose");
          if (proposalPersisted) break;
        }
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
    }
    resolvePersisted(proposalPersisted);
  };
  await page.route(routePattern, handler);
  return {
    waitForPersisted: () => persistedPromise,
    commands: () => persistedCommands,
    remove: () => page.unroute(routePattern, handler),
  };
}

async function openReleaseWorkspace(page, kind) {
  const errors = [];
  const failed = [];
  let mainDocumentRequests = 0;
  let reviewerRequests = 0;
  let ketcherRequests = 0;
  const initialUrl = new URL(releaseUrl(kind));
  expect(initialUrl.searchParams.has("job")).toBe(false);
  const expectedSession = initialUrl.searchParams.get("runtime_session");
  expect(expectedSession).toBeTruthy();
  const reviewerResponsePromise = page.waitForResponse((response) => {
    const request = response.request();
    return request.method() === "POST"
      && new URL(response.url()).pathname
        === `/api/runtime/sessions/${encodeURIComponent(expectedSession)}/reviewer`;
  });
  page.on("pageerror", (error) => errors.push(String(error)));
  page.on("requestfailed", (request) => {
    if (!isDisposedPreviewAbort(request)) {
      failed.push(`${request.method()} ${request.url()} ${request.failure()?.errorText || ""}`);
    }
  });
  page.on("request", (request) => {
    if (request.isNavigationRequest() && request.frame() === page.mainFrame()) {
      mainDocumentRequests += 1;
    }
    if (
      request.method() === "POST"
      && new URL(request.url()).pathname
        === `/api/runtime/sessions/${encodeURIComponent(expectedSession)}/reviewer`
    ) {
      reviewerRequests += 1;
    }
    if (new URL(request.url()).pathname.startsWith("/ketcher/")) {
      ketcherRequests += 1;
    }
  });
  const startedAt = Date.now();
  await page.goto(releaseUrl(kind), { waitUntil: "domcontentloaded" });
  // A sealed PDF handoff may collapse the mounted Pi column before this
  // assertion runs. Assert the durable projection exists; visibility is
  // exercised separately through the PDF pane toggle.
  await expect(page.locator("#runtimepanel .runtime-lifecycle")).toHaveCount(1);
  await expect(page.locator("#runtimepanel .runtime-connection")).toHaveText("sealed");
  await expect(page.locator("#runtimepanel .runtime-result")).toHaveCount(1);
  await expect(page.locator("#runtimepanel .runtime-result-artifacts a")).not.toHaveCount(0);
  await expect(page.locator(".runtime-task-group")).not.toHaveCount(0);
  const shellReadyMs = Date.now() - startedAt;
  expect(shellReadyMs).toBeLessThan(5000);
  await expect.poll(() => {
    const url = new URL(page.url());
    return Boolean(url.searchParams.get("job"));
  }).toBe(true);
  const reviewerResponse = await reviewerResponsePromise;
  expect(reviewerResponse.ok()).toBe(true);
  const reviewerHandoff = await reviewerResponse.json();
  const finalUrl = new URL(page.url());
  expect(reviewerRequests).toBe(1);
  expect(finalUrl.searchParams.get("job")).toBe(reviewerHandoff.jobId);
  expect(finalUrl.searchParams.get("runtime_session")).toBe(expectedSession);
  const workspaceRouteReadyMs = Date.now() - startedAt;
  expect(workspaceRouteReadyMs).toBeLessThan(15000);
  expect(mainDocumentRequests).toBe(1);
  expect(ketcherRequests).toBe(0);
  expect(await page.locator("body").textContent()).not.toContain("GT_SENTINEL");
  const pageSizing = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(pageSizing.scroll).toBe(pageSizing.viewport);
  return {
    errors,
    failed,
    shellReadyMs,
    workspaceRouteReadyMs,
    get mainDocumentRequests() {
      return mainDocumentRequests;
    },
    get reviewerRequests() {
      return reviewerRequests;
    },
    get ketcherRequests() {
      return ketcherRequests;
    },
    startedAt,
  };
}

async function showPdfAgentPane(page) {
  const toggle = page.getByRole("button", { name: "Show Pi", exact: true });
  if (await toggle.isVisible()) await toggle.click();
  await expect(page.locator("#agentpane")).toBeVisible();
  await expect(page.getByRole("button", { name: "Hide Pi", exact: true }))
    .toHaveAttribute("aria-expanded", "true");
}

async function attachEvidence(page, testInfo, kind, state) {
  await page.waitForTimeout(250);
  expect(state.errors).toEqual([]);
  expect(state.failed).toEqual([]);
  const visualReadyMs = state.visualReadyMs ?? (Date.now() - state.startedAt);
  expect(visualReadyMs).toBeLessThan(15000);
  const screenshotPath = testInfo.outputPath(`${kind}-workspace.png`);
  const metricsPath = testInfo.outputPath(`${kind}-metrics.json`);
  await page.screenshot({ path: screenshotPath, fullPage: false });
  writeFileSync(metricsPath, `${JSON.stringify({
    shellReadyMs: state.shellReadyMs,
    workspaceRouteReadyMs: state.workspaceRouteReadyMs,
    visualReadyMs,
    mainDocumentRequests: state.mainDocumentRequests,
    reviewerRequests: state.reviewerRequests,
    ketcherRequests: state.ketcherRequests,
    jsErrors: state.errors.length,
    failedRequests: state.failed.length,
  }, null, 2)}\n`);
  await testInfo.attach(`${kind}-workspace`, {
    path: screenshotPath,
    contentType: "image/png",
  });
  await testInfo.attach(`${kind}-metrics`, {
    path: metricsPath,
    contentType: "application/json",
  });
}

function textContent(content) {
  if (typeof content === "string") return content;
  return Array.isArray(content)
    ? content
      .filter((part) => part?.type === "text" && typeof part.text === "string")
      .map((part) => part.text)
      .join("")
    : "";
}

test("installed browser runs and restores a real two-turn Pi conversation", async ({
  page,
}, testInfo) => {
  test.skip(!LIVE_FIGURE || !LIVE_MODEL || !LOOPBACK_LOG,
    "requires the clean-install Pi loopback acceptance environment");
  test.setTimeout(60000);
  const errors = [];
  const failed = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  page.on("requestfailed", (request) => {
    const path = new URL(request.url()).pathname;
    if (!path.endsWith("/events") && !isDisposedPreviewAbort(request)) {
      failed.push(`${request.method()} ${request.url()} ${request.failure()?.errorText || ""}`);
    }
  });

  await page.goto(releaseUrl("home"), { waitUntil: "domcontentloaded" });
  const home = page.locator(".pdf-product-home");
  await expect(home).toBeVisible();
  await page.locator("#pdf-product-primary").setInputFiles(LIVE_FIGURE);
  await home.locator(".advanced-auth-toggle").click();
  await home.getByRole("radio", { name: /Use my API key/ }).check();
  await home.getByRole("combobox", { name: "Provider", exact: true })
    .selectOption("byo-openai");
  await home.getByRole("textbox", { name: "Model", exact: true }).fill(LIVE_MODEL);
  await home.getByRole("combobox", { name: "Thinking", exact: true }).selectOption("off");
  await home.getByLabel("API key", { exact: true }).fill("browser-loopback-lease");
  await home.getByRole("button", { name: "Extract reactions" }).click();

  await page.waitForURL(/runtime_session=/);
  const attachedUrl = new URL(page.url());
  const sessionId = attachedUrl.searchParams.get("runtime_session");
  expect(sessionId).toBeTruthy();
  await expectRuntimeThinking(page, sessionId, "off");
  expect(attachedUrl.searchParams.has("job")).toBe(false);
  await expect(page.locator("body")).toHaveClass(/runtime-session/);
  await expect(page.locator("#runtimepanel .runtime-lifecycle")).toBeVisible();
  await expect(page.locator("#runtimepanel .runtime-conversation"))
    .toContainText("Loopback extraction response is ready for a persisted follow-up.");
  await expect(page.locator("#runtimepanel .runtime-current > b")).toHaveText("ready");

  const composer = page.locator("#runtimepanel .runtime-composer");
  await composer.getByLabel("Message Pi Agent").fill("Second browser turn");
  await composer.getByRole("button", { name: "Send" }).click();
  await expect(page.locator("#runtimepanel .runtime-conversation"))
    .toContainText("Loopback follow-up response restored the first durable turn.");
  await expect(page.locator("#runtimepanel .runtime-current > b")).toHaveText("ready");
  const cursorBeforeReload = Number(
    (await page.locator("#runtimepanel .runtime-current small").textContent())
      ?.match(/cursor (\d+)/)?.[1] || 0,
  );
  expect(cursorBeforeReload).toBeGreaterThan(0);

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator("#runtimepanel .runtime-conversation"))
    .toContainText("Loopback extraction response is ready for a persisted follow-up.");
  await expect(page.locator("#runtimepanel .runtime-conversation"))
    .toContainText("Second browser turn");
  await expect(page.locator("#runtimepanel .runtime-conversation"))
    .toContainText("Loopback follow-up response restored the first durable turn.");
  await expect(page.locator("#runtimepanel .runtime-current > b")).toHaveText("ready");
  await expect(page.locator(`.runtime-attempt[data-runtime-id="${sessionId}"]`))
    .toHaveClass(/on/);
  expect(new URL(page.url()).searchParams.get("runtime_session")).toBe(sessionId);
  expect(new URL(page.url()).searchParams.has("job")).toBe(false);

  const cursorAfterReload = Number(
    (await page.locator("#runtimepanel .runtime-current small").textContent())
      ?.match(/cursor (\d+)/)?.[1] || 0,
  );
  expect(cursorAfterReload).toBe(cursorBeforeReload);
  const browserStorage = await page.evaluate(() => JSON.stringify({
    local: { ...localStorage },
    session: { ...sessionStorage },
  }));
  expect(browserStorage).not.toContain("browser-loopback-lease");
  expect(browserStorage).not.toContain("GT_SENTINEL");
  expect(await page.locator("body").textContent()).not.toContain("browser-loopback-lease");
  expect(await page.locator("body").textContent()).not.toContain("GT_SENTINEL");

  const requests = readFileSync(LOOPBACK_LOG, "utf8")
    .trim()
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line))
    .filter((request) => request.model === LIVE_MODEL);
  expect(requests).toHaveLength(2);
  expect(requests.every((request) => request.authorizationPresent === true)).toBe(true);
  expect(JSON.stringify(requests)).not.toContain("browser-loopback-lease");
  expect(JSON.stringify(requests)).not.toContain("GT_SENTINEL");
  const secondMessages = requests[1].messages || [];
  expect(secondMessages.some((message) =>
    message.role === "assistant"
      && textContent(message.content)
        .includes("Loopback extraction response is ready for a persisted follow-up.")))
    .toBe(true);
  expect(secondMessages.some((message) =>
    message.role === "user" && textContent(message.content) === "Second browser turn"))
    .toBe(true);
  expect(errors).toEqual([]);
  expect(failed).toEqual([]);
  await expectNoWcagViolations(page, "Restored live Pi conversation");

  const screenshotPath = testInfo.outputPath("live-pi-restored-conversation.png");
  await page.screenshot({ path: screenshotPath, fullPage: false });
  await testInfo.attach("live-pi-restored-conversation", {
    path: screenshotPath,
    contentType: "image/png",
  });
});

test("live Pi tools seal and restore the existing Figure chemistry workspace", async ({
  page,
}, testInfo) => {
  test.skip(!LIVE_FIGURE || !LIVE_TOOL_MODEL || !LOOPBACK_LOG,
    "requires the clean-install Pi tool acceptance environment");
  // A clean macOS venv can spend over a minute on the first RDKit/Markush
  // import. This is real cold-start chemistry work, not a mocked timer.
  test.setTimeout(180000);
  const errors = [];
  const failed = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  page.on("requestfailed", (request) => {
    const path = new URL(request.url()).pathname;
    if (!path.endsWith("/events") && !isDisposedPreviewAbort(request)) {
      failed.push(`${request.method()} ${request.url()} ${request.failure()?.errorText || ""}`);
    }
  });

  await page.goto(releaseUrl("home"), { waitUntil: "domcontentloaded" });
  const home = page.locator(".pdf-product-home");
  await page.locator("#pdf-product-primary").setInputFiles(LIVE_FIGURE);
  await home.locator(".advanced-auth-toggle").click();
  await home.getByRole("radio", { name: /Use my API key/ }).check();
  await home.getByRole("combobox", { name: "Provider", exact: true })
    .selectOption("byo-openai");
  await home.getByRole("textbox", { name: "Model", exact: true }).fill(LIVE_TOOL_MODEL);
  await home.getByRole("combobox", { name: "Thinking", exact: true }).selectOption("off");
  await home.getByLabel("API key", { exact: true }).fill("browser-loopback-lease");
  await home.getByRole("button", { name: "Extract reactions" }).click();

  await page.waitForURL((url) => Boolean(url.searchParams.get("runtime_session")));
  const sessionId = new URL(page.url()).searchParams.get("runtime_session");
  expect(sessionId).toBeTruthy();
  await expectRuntimeThinking(page, sessionId, "off");
  await expect(page.locator("#runtimepanel .runtime-current > b"))
    .toHaveText("review ready", { timeout: 30_000 });
  await page.waitForURL((url) => Boolean(url.searchParams.get("job")));
  const jobId = new URL(page.url()).searchParams.get("job");
  expect(jobId).toMatch(/^runtime_/);
  await expect(page.locator("#runtimepanel .runtime-connection")).toHaveText("sealed");
  await expect(page.locator("#runtimepanel .runtime-conversation"))
    .toContainText("Live tool artifacts are sealed and ready in the chemistry workspace.");
  for (const tool of ["detect", "assemble", "render_reactions"]) {
    await expect(page.locator("#runtimepanel .runtime-tools li", { hasText: tool }))
      .toContainText("complete");
  }

  const workspace = page.locator("#figureisland .figure-review-island");
  await expect(workspace).toBeVisible();
  await expect(workspace.locator(".anchor-panel")).toBeVisible();
  await expect(workspace.locator(".bbox-overlay")).toHaveAttribute(
    "aria-label",
    "27 extraction boxes",
  );
  await expect(workspace.locator("[data-box-key]")).toHaveCount(27);
  await expect(workspace.locator("[data-reaction-key]")).toHaveCount(1);
  await workspace.locator('[data-bbox-index="3"]').click();
  await expect(workspace.locator("[data-reaction-key]")).toHaveClass(/bbox-linked-active/);

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator("#runtimepanel .runtime-current > b")).toHaveText("review ready");
  await expect(page.locator("#runtimepanel .runtime-connection")).toHaveText("sealed");
  const restoredWorkspace = page.locator("#figureisland .figure-review-island");
  await expect(restoredWorkspace).toBeVisible();
  await expect(restoredWorkspace.locator(".bbox-overlay")).toHaveAttribute(
    "aria-label",
    "27 extraction boxes",
  );
  await restoredWorkspace.locator(".reaction-bbox-link").click();
  await expect(restoredWorkspace.locator('[data-bbox-index="3"]')).toHaveClass(/bbox-active/);
  await restoredWorkspace.locator('[data-bbox-index="3"]').click();
  await expect(restoredWorkspace.locator("[data-reaction-key]"))
    .toHaveClass(/bbox-linked-active/);
  expect(new URL(page.url()).searchParams.get("runtime_session")).toBe(sessionId);
  expect(new URL(page.url()).searchParams.get("job")).toBe(jobId);

  const productApi = (path) => new URL(path, page.url()).href;
  const rawBefore = await page.request.get(productApi(`/api/jobs/${jobId}/export?fmt=v2`));
  expect(rawBefore.ok()).toBe(true);
  const rawBeforeBytes = await rawBefore.body();
  const sourceReportBefore = await (
    await page.request.get(productApi(`/api/jobs/${jobId}/export?fmt=report`))
  ).json();
  const sourcePredictionSha256 = sourceReportBefore.source.prediction_sha256;
  expect(sourcePredictionSha256).toMatch(/^[0-9a-f]{64}$/);
  const jobResponse = await page.request.get(productApi(`/api/jobs/${jobId}`));
  expect(jobResponse.ok()).toBe(true);
  const job = await jobResponse.json();
  const reviewerFragment = new URL(job.reviewer_url).hash.match(/^#\/([^/]+)\/([^/?]+)/);
  expect(reviewerFragment).toBeTruthy();
  const [, reviewerSlug, reviewerTaskId] = reviewerFragment;
  const reviewerTaskPath =
    `/api/reviewer/api/paper/${encodeURIComponent(reviewerSlug)}/task/${encodeURIComponent(reviewerTaskId)}`;
  const taskBeforeResponse = await page.request.get(productApi(reviewerTaskPath));
  expect(taskBeforeResponse.ok()).toBe(true);
  const taskBefore = await taskBeforeResponse.json();
  await restoredWorkspace.evaluate((element) => {
    element.dataset.linkedTaskMount = "keep-alive";
  });
  expect(await page.evaluate(
    (targetJob) => window.chemrxnWorkspaceMounted?.(targetJob),
    jobId,
  )).toBe(true);

  const linkedProposal = await holdLinkedSessionUntilProposalIsDurable(
    page,
    productApi,
    sessionId,
  );
  await page.getByLabel("Message Pi Agent").fill(
    "Figure correction acceptance: inspect example:0 and stage a product note for my review.",
  );
  await page.getByRole("button", { name: "Start linked task" }).click();
  await page.waitForURL((url) =>
    url.searchParams.get("runtime_session") !== sessionId
    && url.searchParams.get("job") === jobId,
  );
  const linkedSessionId = new URL(page.url()).searchParams.get("runtime_session");
  expect(linkedSessionId).toBeTruthy();
  expect(await linkedProposal.waitForPersisted()).toBe(true);
  await linkedProposal.remove();
  await expect(page.locator("#figureisland .figure-review-island"))
    .toHaveAttribute("data-linked-task-mount", "keep-alive");
  await expect(page.locator(".agent-proposal-context"))
    .toContainText("Pi Agent proposal · example:0 · not applied");
  await expect(page.locator(".diff-md-body"))
    .toContainText("Human verification requested for compound 5");
  const taskAfterPreview = await (
    await page.request.get(productApi(reviewerTaskPath))
  ).json();
  expect(taskAfterPreview.reviewed_version).toBe(taskBefore.reviewed_version);
  const sourceReportAfterPreview = await (
    await page.request.get(productApi(`/api/jobs/${jobId}/export?fmt=report`))
  ).json();
  expect(sourceReportAfterPreview.source.prediction_sha256)
    .toBe(sourcePredictionSha256);

  await page.getByRole("button", {
    name: "Apply → immutable reviewed snapshot",
  }).click();
  await expect(page.locator(".save-bar .status.reviewed"))
    .toContainText("reviewed-v");
  const taskAfterApply = await (
    await page.request.get(productApi(reviewerTaskPath))
  ).json();
  expect(taskAfterApply.reviewed_version)
    .toBe((taskBefore.reviewed_version || 0) + 1);
  expect(taskAfterApply.reviewed_versions)
    .toHaveLength(taskBefore.reviewed_versions.length + 1);
  await expect(page.locator(".runtime-workspace-citation .proposal-status"))
    .toContainText(`Applied · reviewed-v${taskAfterApply.reviewed_version}`);
  const rawAfterApply = await page.request.get(
    productApi(`/api/jobs/${jobId}/export?fmt=v2`),
  );
  expect(rawAfterApply.ok()).toBe(true);
  expect((await rawAfterApply.body()).equals(rawBeforeBytes)).toBe(true);
  const sourceReportAfterApply = await (
    await page.request.get(productApi(`/api/jobs/${jobId}/export?fmt=report`))
  ).json();
  expect(sourceReportAfterApply.source.prediction_sha256)
    .toBe(sourcePredictionSha256);

  await page.reload({ waitUntil: "domcontentloaded" });
  expect(new URL(page.url()).searchParams.get("runtime_session")).toBe(linkedSessionId);
  expect(new URL(page.url()).searchParams.get("job")).toBe(jobId);
  await expect(page.locator(".save-bar .status.reviewed"))
    .toContainText(`reviewed-v${taskAfterApply.reviewed_version}`);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", {
    name: `↺ Undo v${taskAfterApply.reviewed_version}`,
  }).click();
  await expect(page.locator(".save-bar .status.fresh")).toContainText("原始");
  const taskAfterUndo = await (
    await page.request.get(productApi(reviewerTaskPath))
  ).json();
  expect(taskAfterUndo.reviewed_version).toBeNull();
  expect(taskAfterUndo.reviewed_versions)
    .toHaveLength(taskAfterApply.reviewed_versions.length);
  const rawAfterUndo = await page.request.get(
    productApi(`/api/jobs/${jobId}/export?fmt=v2`),
  );
  expect((await rawAfterUndo.body()).equals(rawBeforeBytes)).toBe(true);
  const sourceReportAfterUndo = await (
    await page.request.get(productApi(`/api/jobs/${jobId}/export?fmt=report`))
  ).json();
  expect(sourceReportAfterUndo.source.prediction_sha256)
    .toBe(sourcePredictionSha256);

  await page.getByRole("button", { name: "Inspect extraction evidence" }).click();
  await expect(page.locator(".live-chemistry-evidence")).toBeVisible();
  await expect(page.locator(".live-evidence-status")).toContainText(/review ready|needs attention/);
  await expect(page.locator(".live-chemistry-evidence")).not.toContainText("GT_SENTINEL");
  await page.getByRole("button", { name: "Return to sealed review" }).click();
  await expect(page.locator("#figureisland")).toBeVisible();

  const browserStorage = await page.evaluate(() => JSON.stringify({
    local: { ...localStorage },
    session: { ...sessionStorage },
  }));
  expect(browserStorage).not.toContain("browser-loopback-lease");
  expect(browserStorage).not.toContain("GT_SENTINEL");
  expect(await page.locator("body").textContent()).not.toContain("browser-loopback-lease");
  expect(await page.locator("body").textContent()).not.toContain("GT_SENTINEL");
  const requests = readFileSync(LOOPBACK_LOG, "utf8")
    .trim()
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line))
    .filter((request) => request.model === LIVE_TOOL_MODEL);
  expect(requests).toHaveLength(7);
  expect(requests.every((request) => request.authorizationPresent === true)).toBe(true);
  expect(requests.every((request) => request.groundTruthPresent === false)).toBe(true);
  expect(JSON.stringify(requests)).not.toContain("browser-loopback-lease");
  expect(JSON.stringify(requests)).not.toContain("GT_SENTINEL");
  expect(errors).toEqual([]);
  expect(failed).toEqual([]);
  await expectNoWcagViolations(page, "Live Pi Figure workspace");

  const screenshotPath = testInfo.outputPath("live-pi-figure-workspace.png");
  await page.screenshot({ path: screenshotPath, fullPage: false });
  await testInfo.attach("live-pi-figure-workspace", {
    path: screenshotPath,
    contentType: "image/png",
  });
});

test("installed Figure product opens Pi, BBoxes, reactions, and immutable artifacts", async ({
  page,
}, testInfo) => {
  const state = await openReleaseWorkspace(page, "figure");
  await expect(page.locator("#runtimepanel .runtime-execution-provenance"))
    .toContainText("reaction-extract");
  await expect(page.locator("#runtimepanel .runtime-execution-provenance"))
    .toContainText("Sealed tool ledger");
  await expect(page.locator("#runtimepanel .runtime-tools-disclosure"))
    .toContainText("Arguments and outputs were not captured");
  for (const [name, count] of [
    ["detect", 1],
    ["verify_smiles", 17],
    ["assemble", 2],
    ["render_reactions", 2],
  ]) {
    const item = page.locator("#runtimepanel .runtime-tools li", { hasText: name });
    await expect(item).toContainText(`${count}× · complete`);
  }
  await expect(page.locator("#figureisland .figure-review-island")).toBeVisible();
  await expectInViewport(page.locator("#figureisland .figure-review-island"));
  await expect(page.locator("#workspaceempty")).toBeHidden();
  await expect(page.locator("#figureisland .bbox-image-panel img")).toBeVisible();
  await expect(page.locator("#figureisland [data-box-key]")).not.toHaveCount(0);
  await expect(page.locator("#figureisland [data-reaction-key]")).not.toHaveCount(0);
  await expect(page.locator("#figureisland .render-error").filter({ hasText: "渲染中" }))
    .toHaveCount(0, { timeout: 12000 });
  const taskBodySizing = await page.locator("#figureisland .task-body").evaluate((element) => ({
    client: element.clientWidth,
    scroll: element.scrollWidth,
  }));
  expect(taskBodySizing.scroll).toBe(taskBodySizing.client);
  const deferredCards = page.locator("#figureisland .render-deferred");
  const deferredBeforeScroll = await deferredCards.count();
  expect(deferredBeforeScroll).toBeGreaterThan(0);
  const renderedBeforeScroll = await page.locator("#figureisland .mol-svg svg").count();
  await expect(page.locator("#docmode")).toBeHidden();
  state.visualReadyMs = Date.now() - state.startedAt;
  const historyResizer = page.locator("#historyresizer");
  const historyWidth = Number(await historyResizer.getAttribute("aria-valuenow"));
  await historyResizer.focus();
  await page.keyboard.press("ArrowRight");
  await expect(historyResizer).toHaveAttribute("aria-valuenow", String(historyWidth + 16));
  await page.keyboard.press("ArrowLeft");
  await expect(historyResizer).toHaveAttribute("aria-valuenow", String(historyWidth));
  await expectNoWcagViolations(page, "Installed Figure workspace");
  await attachEvidence(page, testInfo, "figure", state);
  await page.locator("#figureisland .task-body").evaluate((element) => {
    element.scrollBy({ top: 900, behavior: "instant" });
  });
  await expect.poll(() => deferredCards.count()).toBeLessThan(deferredBeforeScroll);
  await expect.poll(() => page.locator("#figureisland .mol-svg svg").count())
    .toBeGreaterThan(renderedBeforeScroll);
  const deferredAfterScroll = await deferredCards.count();
  const renderedAfterScroll = await page.locator("#figureisland .mol-svg svg").count();
  expect(state.errors).toEqual([]);
  expect(state.failed).toEqual([]);
  const scrollScreenshotPath = testInfo.outputPath("figure-scroll-workspace.png");
  const scrollMetricsPath = testInfo.outputPath("figure-scroll-metrics.json");
  await page.screenshot({ path: scrollScreenshotPath, fullPage: false });
  writeFileSync(scrollMetricsPath, `${JSON.stringify({
    deferredBeforeScroll,
    deferredAfterScroll,
    renderedBeforeScroll,
    renderedAfterScroll,
  }, null, 2)}\n`);
  await testInfo.attach("figure-scroll-workspace", {
    path: scrollScreenshotPath,
    contentType: "image/png",
  });
  await testInfo.attach("figure-scroll-metrics", {
    path: scrollMetricsPath,
    contentType: "application/json",
  });
});

test("installed reviewer handoff keeps the prior route when the workspace cannot mount", async ({
  page,
}) => {
  const initialUrl = new URL(releaseUrl("figure"));
  const sessionId = initialUrl.searchParams.get("runtime_session");
  expect(initialUrl.searchParams.has("job")).toBe(false);
  expect(sessionId).toBeTruthy();
  let reviewerRequests = 0;
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (
      request.method() === "POST"
      && path === `/api/runtime/sessions/${encodeURIComponent(sessionId)}/reviewer`
    ) reviewerRequests += 1;
  });
  await page.route("**/api/jobs/runtime_*", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "forced reviewer metadata failure" }),
      });
      return;
    }
    await route.continue();
  });
  await page.goto(initialUrl.toString(), { waitUntil: "domcontentloaded" });
  await expect(page.locator("#runtimepanel .runtime-result-error"))
    .toContainText("could not be mounted");
  expect(new URL(page.url()).searchParams.has("job")).toBe(false);
  expect(reviewerRequests).toBe(1);
  await expect(page.locator("#workspaceempty")).toBeVisible();
  await expect(page.locator("body")).not.toHaveClass(/runtime-has-workspace/);
});

test("installed narrow shell keeps the Figure workspace mounted across product tabs", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = await openReleaseWorkspace(page, "figure");
  const agentTab = page.locator('#sessiontabs [data-session-pane="agent"]');
  const historyTab = page.locator('#sessiontabs [data-session-pane="history"]');
  const workspaceTab = page.locator('#sessiontabs [data-session-pane="workspace"]');

  await expect(agentTab).toHaveClass(/on/);
  await expect(page.locator("#agentpane")).toBeVisible();
  await expect(page.locator("#figureisland")).toBeHidden();

  await workspaceTab.click();
  await expect(workspaceTab).toHaveClass(/on/);
  await expectInViewport(page.locator("#figureisland .figure-review-island"));
  await expect(page.locator("#figureisland [data-box-key]")).not.toHaveCount(0);
  await expect(page.locator("#figureisland [data-reaction-key]")).not.toHaveCount(0);
  const visibleReviewPair = await page.evaluate(() => {
    const visibleBounds = (selector) => Array.from(document.querySelectorAll(selector))
      .map((element) => {
        const bounds = element.getBoundingClientRect();
        const width = Math.max(0, Math.min(bounds.right, window.innerWidth)
          - Math.max(bounds.left, 0));
        const height = Math.max(0, Math.min(bounds.bottom, window.innerHeight)
          - Math.max(bounds.top, 0));
        return { width, height, area: width * height };
      })
      .sort((a, b) => b.area - a.area)[0] || { width: 0, height: 0, area: 0 };
    return {
      bbox: visibleBounds("#figureisland [data-box-key]"),
      reaction: visibleBounds("#figureisland [data-reaction-key]"),
    };
  });
  expect(visibleReviewPair.bbox.width).toBeGreaterThanOrEqual(8);
  expect(visibleReviewPair.bbox.height).toBeGreaterThanOrEqual(8);
  expect(visibleReviewPair.bbox.area).toBeGreaterThanOrEqual(64);
  expect(visibleReviewPair.reaction.width).toBeGreaterThanOrEqual(8);
  expect(visibleReviewPair.reaction.height).toBeGreaterThanOrEqual(8);
  expect(visibleReviewPair.reaction.area).toBeGreaterThanOrEqual(64);
  await expectNoWcagViolations(page, "Installed narrow Figure workspace");
  const taskBody = page.locator("#figureisland .task-body");
  await taskBody.evaluate((element) => {
    element.scrollTop = Math.min(720, element.scrollHeight - element.clientHeight);
  });
  const scrollBeforeTabs = await taskBody.evaluate((element) => element.scrollTop);
  expect(scrollBeforeTabs).toBeGreaterThan(0);

  await historyTab.click();
  await expect(historyTab).toHaveClass(/on/);
  await expect(page.locator("#history")).toBeVisible();
  await expect(page.locator("#figureisland")).toBeHidden();
  await agentTab.click();
  await expect(page.locator("#runtimepanel .runtime-lifecycle")).toBeVisible();
  await workspaceTab.click();
  await expectInViewport(page.locator("#figureisland .figure-review-island"));
  expect(await taskBody.evaluate((element) => element.scrollTop)).toBe(scrollBeforeTabs);

  const sizing = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(sizing.scroll).toBe(sizing.viewport);
  expect(state.mainDocumentRequests).toBe(1);
  expect(state.reviewerRequests).toBe(1);
  expect(state.errors).toEqual([]);
  expect(state.failed).toEqual([]);

  const screenshotPath = testInfo.outputPath("narrow-figure-workspace.png");
  await page.screenshot({ path: screenshotPath, fullPage: false });
  await testInfo.attach("narrow-figure-workspace", {
    path: screenshotPath,
    contentType: "image/png",
  });
});
