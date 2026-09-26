import { createApp, h, reactive } from "vue";
import {
  createPreflightedExtraction,
  RuntimeExtractionBackend,
} from "/static/extraction-backend.js";
import {
  defaultProductApiModel,
  PRODUCT_API_PROVIDER_OPTIONS,
} from "/static/lib/product-model-defaults.js";
import {
  classifyProductSources,
  submissionFromAssignments,
} from "/static/lib/product-source-classifier.js";
import {
  buildProductExtractionInput,
  productSubmissionModelInput,
  snapshotProductSubmission,
} from "/static/lib/product-submission.js?v=20260731a";

const ACCEPTED_FILES = [
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/tiff",
  ".png",
  ".jpg",
  ".jpeg",
  ".webp",
  ".tif",
  ".tiff",
].join(",");

const THINKING_LEVEL_OPTIONS = Object.freeze([
  ["off", "Off"],
  ["minimal", "Minimal"],
  ["low", "Low"],
  ["medium", "Medium · recommended"],
  ["high", "High"],
  ["xhigh", "Extra high"],
  ["max", "Maximum"],
]);

function fileLabel(file) {
  if (!file) return "";
  const size = file.size < 1024 * 1024
    ? `${Math.max(1, Math.round(file.size / 1024))} KB`
    : `${(file.size / (1024 * 1024)).toFixed(1)} MB`;
  return `${file.name} · ${size}`;
}

function validateImage(file, label) {
  if (!file) return "";
  const hasImageType = /^image\/(png|jpeg|webp|tiff?)$/i.test(file.type || "");
  const hasImageName = /\.(png|jpe?g|webp|tiff?)$/i.test(file.name || "");
  if (!hasImageType && !hasImageName) {
    return `${label} must be a PNG, JPEG, WebP, or TIFF image.`;
  }
  if (file.size > 50 * 1024 * 1024) return `${label} must be smaller than 50 MB.`;
  return "";
}

function FigureProductApp({ backend, onAttachSession, runtimeSessionId }) {
  const state = reactive({
    sourceType: null,
    primary: null,
    si: null,
    assignments: [],
    sourceMessage: "",
    fileError: "",
    needsRoleConfirmation: false,
    dragging: false,
    submitting: false,
    submitError: "",
    loadedSample: null,
    samples: [],
    samplesLoading: true,
    loadingSample: null,
    showAdvanced: false,
    authMode: "pi_auth",
    apiProvider: PRODUCT_API_PROVIDER_OPTIONS[0].registryId,
    apiModel: PRODUCT_API_PROVIDER_OPTIONS[0].defaultModel,
    thinkingLevel: "medium",
    apiKey: "",
    auth: {
      checking: true,
      configured: false,
      loginCommand: "chemrxn login",
    },
    capability: {
      checking: true,
      connected: false,
      inputs: { figure: false },
      message: "Checking the local extraction service…",
      diagnosticMessage: "",
    },
  });
  let sourceInput = null;
  let sampleLoadGeneration = 0;
  let sampleLoadController = null;

  function cancelPendingSample() {
    sampleLoadGeneration += 1;
    if (sampleLoadController) sampleLoadController.abort();
    sampleLoadController = null;
    state.loadingSample = null;
  }

  function syncSubmission() {
    const submission = submissionFromAssignments(state.assignments);
    state.sourceType = submission.sourceType;
    state.primary = submission.primary;
    state.si = submission.si;
    window.dispatchEvent(new CustomEvent("chemrxn-product-source-change", {
      detail: {
        count: state.assignments.length,
        sourceType: submission.sourceType,
        complete: Boolean(submission.primary)
          && !state.needsRoleConfirmation
          && assignmentErrors().length === 0,
      },
    }));
  }

  function assignmentErrors() {
    return state.assignments.map((row) => (
      validateImage(row.file, "Reaction figure")
    )).filter(Boolean);
  }

  function applyClassification(result, { preserveLoadedSample = false } = {}) {
    if (!preserveLoadedSample) state.loadedSample = null;
    if (!result.ok) {
      state.fileError = result.message;
      return false;
    }
    state.assignments = result.assignments;
    state.needsRoleConfirmation = result.needsConfirmation;
    state.sourceMessage = result.message;
    state.fileError = "";
    syncSubmission();
    return true;
  }

  function addFiles(fileList, { preserveLoadedSample = false } = {}) {
    const incoming = Array.from(fileList || []).filter(Boolean);
    if (!incoming.length) return;
    cancelPendingSample();
    let candidates = incoming;
    applyClassification(classifyProductSources(candidates), { preserveLoadedSample });
    if (sourceInput) sourceInput.value = "";
  }

  function chooseSources(event) {
    addFiles(event.target.files);
  }

  function dropSources(event) {
    state.dragging = false;
    addFiles(event.dataTransfer?.files);
  }

  function removeSource(index) {
    cancelPendingSample();
    const remaining = state.assignments.filter((_, rowIndex) => rowIndex !== index)
      .map((row) => row.file);
    state.loadedSample = null;
    applyClassification(classifyProductSources(remaining));
  }

  async function startExtraction() {
    if (state.submitting) return;
    state.submitError = "";
    state.submitting = true;
    const submission = snapshotProductSubmission(state);
    if (submission.authMode === "api_key") state.apiKey = "";
    try {
      const buildInput = () => buildProductExtractionInput(submission);
      const created = submission.authMode === "api_key"
        ? await createPreflightedExtraction(
          backend,
          productSubmissionModelInput(submission),
          buildInput,
        )
        : await backend.create(buildInput());
      const sessionId = created.sessionId || created.jobId;
      if (!sessionId) throw new Error("The extraction service did not return a task ID.");
      onAttachSession(sessionId);
    } catch (error) {
      state.submitError = error?.message || "Extraction could not start.";
    } finally {
      state.submitting = false;
    }
  }

  async function refreshCapabilities() {
    state.capability.checking = true;
    try {
      const capability = await backend.capabilities();
      state.capability = {
        checking: false,
        connected: capability?.connected === true,
        inputs: {
          figure: capability?.inputs?.figure === true,
        },
        message: capability?.connected
          ? "Reaction image extraction is available."
          : "Extraction service not connected. You can still load the demo figure.",
        diagnosticMessage: typeof capability?.message === "string" ? capability.message : "",
      };
    } catch (error) {
      state.capability = {
        checking: false,
        connected: false,
        inputs: { figure: false },
        message: "Extraction service not connected. You can still load the demo figure.",
        diagnosticMessage: error?.message || "",
      };
    }
  }

  async function refreshAuth() {
    state.auth.checking = true;
    try {
      const status = typeof backend.authStatus === "function" ? await backend.authStatus() : {};
      state.auth = {
        checking: false,
        configured: status?.configured === true,
        loginCommand: status?.loginCommand || "chemrxn login",
      };
    } catch {
      state.auth = { checking: false, configured: false, loginCommand: "chemrxn login" };
    }
  }

  async function loadSamples() {
    state.samplesLoading = true;
    try {
      const response = await fetch("/api/samples", { headers: { Accept: "application/json" } });
      const samples = await response.json();
      state.samples = response.ok && Array.isArray(samples) ? samples : [];
    } catch {
      state.samples = [];
    } finally {
      state.samplesLoading = false;
    }
  }

  async function loadSample(sample) {
    cancelPendingSample();
    const generation = sampleLoadGeneration;
    const controller = new AbortController();
    sampleLoadController = controller;
    state.loadingSample = sample.id;
    state.submitError = "";
    try {
      const sources = Array.isArray(sample.files) && sample.files.length
        ? sample.files
        : [{ role: "figure", filename: sample.filename,
          url: `/api/samples/${encodeURIComponent(sample.id)}` }];
      const loaded = await Promise.all(sources.map(async (source) => {
        const response = await fetch(source.url, { signal: controller.signal });
        if (!response.ok) throw new Error(`Demo source is unavailable (HTTP ${response.status}).`);
        const blob = await response.blob();
        return {
          role: source.role,
          file: new File([blob], source.filename, {
            type: blob.type || "image/png",
          }),
        };
      }));
      if (generation !== sampleLoadGeneration) return false;
      const result = classifyProductSources(loaded.map((row) => row.file));
      if (!result.ok) throw new Error(result.message);
      result.assignments = loaded.map((row) => ({ ...row, reason: "bundled-sample" }));
      result.needsConfirmation = false;
      result.message = "Demo source ready. Loading it did not run a model.";
      applyClassification(result, { preserveLoadedSample: true });
      state.loadedSample = sample.id;
      return true;
    } catch (error) {
      if (error?.name === "AbortError" || generation !== sampleLoadGeneration) return false;
      state.submitError = error?.message || "Demo source could not be loaded.";
      return false;
    } finally {
      if (generation === sampleLoadGeneration) {
        sampleLoadController = null;
        state.loadingSample = null;
      }
    }
  }

  function sampleCard(sample, featured = false) {
    const fileCount = sample.files?.length || 1;
    const rights = sample.rights?.status === "verified" ? sample.rights : null;
    const rightsLabel = rights?.label || "Local sample";
    const rightsTitle = rights?.attribution || "Redistribution rights are not declared by this local sample.";
    return h("article", {
      class: `sample-source sample-card ${featured ? "featured" : ""} ${state.loadedSample === sample.id ? "selected" : ""}`,
    }, [
      sample.previewUrl
        ? h("img", { src: sample.previewUrl, alt: "", loading: "lazy" })
        : h("div", { class: "sample-pdf-preview", "aria-hidden": "true" }, [
            h("span", null, "FIGURE"),
            h("small", null, "source image"),
          ]),
      h("div", { class: "sample-card-copy" }, [
        h("div", { class: "sample-card-meta" }, [
          h("span", { class: "sample-kind" }, "FIGURE"),
          h("span", { class: `sample-license ${rights ? "verified" : "undeclared"}`, title: rightsTitle }, rightsLabel),
        ]),
        h("b", null, sample.title),
        h("small", null, sample.sub),
        h("button", {
          type: "button",
          class: "sample-load",
          "aria-pressed": String(state.loadedSample === sample.id),
          "aria-busy": String(state.loadingSample === sample.id),
          onClick: () => { void loadSample(sample); },
        }, state.loadingSample === sample.id
          ? "Loading source…"
          : state.loadedSample === sample.id
            ? "Demo loaded"
            : "Use this source"),
      ]),
    ]);
  }

  return {
    mounted() {
      void refreshCapabilities();
      void refreshAuth();
      void loadSamples();
    },
    render() {
      const fileErrors = assignmentErrors();
      const submission = submissionFromAssignments(state.assignments);
      const hasCompleteSource = Boolean(submission.sourceType && submission.primary)
        && !state.needsRoleConfirmation
        && !fileErrors.length;
      const capabilityReady = Boolean(
        submission.sourceType && state.capability.inputs[submission.sourceType],
      );
      const authReady = state.authMode === "pi_auth"
        ? state.auth.configured
        : Boolean(state.apiProvider && state.apiModel.trim() && state.apiKey);
      const canExtract = hasCompleteSource
        && state.capability.connected
        && capabilityReady
        && authReady
        && !state.submitting;
      const serviceTitle = runtimeSessionId
        ? "Task attached"
        : state.capability.checking || state.auth.checking
          ? "Checking local service"
          : !state.capability.connected
            ? "Extraction service not connected"
            : !state.auth.configured && state.authMode === "pi_auth"
              ? "Sign in to extract"
              : "Ready to extract";
      const serviceReady = state.capability.connected && authReady;
      const featuredSample = state.samples.find((sample) => sample.sourceType === "figure");
      const otherSamples = state.samples.filter((sample) => sample.sourceType === "figure" && sample !== featuredSample);

      return h("section", { class: "pdf-product-home", "aria-labelledby": "pdf-product-title" }, [
        h("header", { class: "pdf-product-hero" }, [
          h("div", null, [
            h("span", { class: "product-kicker" }, "LOCAL CHEMISTRY WORKSPACE"),
            h("h2", { id: "pdf-product-title" }, "Drop chemistry in. Keep every source connected."),
            h("p", null, "Upload a reaction image, run RxnMiner, and inspect the extracted structures."),
          ]),
          h("div", {
            class: `backend-status ${serviceReady ? "connected" : "disconnected"}`,
            role: "status",
            "aria-live": "polite",
            "aria-atomic": "true",
          }, [
            h("span", { class: "status-light", "aria-hidden": "true" }),
            h("div", null, [
              h("b", null, serviceTitle),
              h("small", null, state.capability.message),
            ]),
          ]),
        ]),

        h("div", { class: "pdf-product-grid" }, [
          h("section", { class: "product-panel source-panel", "aria-labelledby": "source-panel-title" }, [
            h("div", { class: "panel-heading" }, [
              h("span", null, "01"),
              h("div", null, [
                h("h3", { id: "source-panel-title" }, "Add your source"),
                h("p", null, "Drop one reaction image."),
              ]),
            ]),
            h("div", {
              class: `unified-dropzone ${state.dragging ? "dragging" : ""} ${state.assignments.length ? "has-files" : ""}`,
              onDragenter: (event) => { event.preventDefault(); state.dragging = true; },
              onDragover: (event) => { event.preventDefault(); state.dragging = true; },
              onDragleave: (event) => {
                if (!event.currentTarget.contains(event.relatedTarget)) state.dragging = false;
              },
              onDrop: (event) => { event.preventDefault(); dropSources(event); },
            }, [
              h("div", { class: "dropzone-mark", "aria-hidden": "true" }, "↥"),
              h("div", null, [
                h("b", null, state.assignments.length ? "Add or replace a source" : "Drop files here"),
                h("p", null, state.assignments.length
                  ? "Drop an image to replace this source."
                  : "PNG, JPEG, WebP, or TIFF · one image"),
              ]),
              h("label", { class: "file-action" }, [
                state.assignments.length ? "Browse files" : "Choose files",
                h("input", {
                  id: "pdf-product-primary",
                  ref: (element) => { sourceInput = element; },
                  type: "file",
                  accept: ACCEPTED_FILES,
                  multiple: false,
                  onChange: chooseSources,
                }),
              ]),
            ]),

            state.assignments.length ? h("div", { class: "source-assignment-list", "aria-label": "Selected sources" }, [
              ...state.assignments.map((row, index) => h("div", { class: "source-assignment-row" }, [
                h("span", { class: `source-file-icon ${row.role}`, "aria-hidden": "true" }, "IMG"),
                h("div", { class: "source-file-copy" }, [
                  h("b", null, row.file.name),
                  h("small", null, fileLabel(row.file).replace(`${row.file.name} · `, "")),
                ]),
                h("span", { class: "source-role-static" }, "Reaction figure"),
                h("button", {
                  type: "button",
                  class: "source-remove",
                  "aria-label": `Remove ${row.file.name}`,
                  onClick: () => removeSource(index),
                }, "Remove"),
              ])),
            ]) : null,

            state.sourceMessage ? h("p", {
              class: `source-message ${state.needsRoleConfirmation ? "confirm" : ""}`,
              role: state.needsRoleConfirmation ? "alert" : "status",
            }, state.sourceMessage) : null,
            state.fileError || fileErrors.length
              ? h("div", { class: "file-errors", role: "alert" }, [
                  state.fileError ? h("p", null, state.fileError) : null,
                  ...fileErrors.map((error) => h("p", null, error)),
                ]) : null,

            h("div", { class: "extract-action-row" }, [
              h("button", {
                class: "primary-action",
                disabled: !canExtract,
                onClick: () => { void startExtraction(); },
              }, state.submitting ? "Starting extraction…" : "Extract reactions"),
              h("span", null, hasCompleteSource
                ? !state.capability.connected
                  ? "The demo can be reviewed now; extraction waits for the local service."
                  : !authReady ? "Sign in before starting a new extraction." : "Your files remain on this computer."
                : state.needsRoleConfirmation ? "Choose a reaction image." : "Add a complete source to continue."),
            ]),
            state.submitError
              ? h("p", { class: "file-errors", role: "alert" }, state.submitError) : null,

            h("button", {
              class: "advanced-auth-toggle",
              type: "button",
              "aria-expanded": String(state.showAdvanced),
              onClick: () => { state.showAdvanced = !state.showAdvanced; },
            }, `${state.showAdvanced ? "Hide" : "Show"} advanced settings`),
            state.showAdvanced ? h("fieldset", { class: "advanced-auth" }, [
              h("legend", null, "Model access"),
              h("label", { class: "auth-choice" }, [
                h("input", {
                  type: "radio",
                  name: "product-auth-mode",
                  checked: state.authMode === "pi_auth",
                  onChange: () => { state.authMode = "pi_auth"; state.apiKey = ""; },
                }),
                h("span", null, [
                  h("b", null, "ChatGPT / Codex subscription"),
                  h("small", null, "Uses the isolated ChemRxn Pi profile on this computer."),
                ]),
              ]),
              h("label", { class: "auth-choice" }, [
                h("input", {
                  type: "radio",
                  name: "product-auth-mode",
                  checked: state.authMode === "api_key",
                  onChange: () => { state.authMode = "api_key"; },
                }),
                h("span", null, [
                  h("b", null, "Use my API key"),
                  h("small", null, "Held only for this request and never remembered by the browser."),
                ]),
              ]),
              state.authMode === "pi_auth" ? h("div", { class: "login-instruction" }, [
                h("span", null, state.auth.configured ? "Login detected" : "Login required"),
                h("code", null, state.auth.loginCommand),
                h("button", { type: "button", onClick: () => { void refreshAuth(); } }, "Check again"),
              ]) : h("div", { class: "api-auth-fields" }, [
                h("label", null, [
                  "Provider",
                  h("select", {
                    value: state.apiProvider,
                    onChange: (event) => {
                      state.apiProvider = event.target.value;
                      state.apiModel = defaultProductApiModel(state.apiProvider);
                    },
                  }, PRODUCT_API_PROVIDER_OPTIONS.map((option) => h("option", { value: option.registryId }, option.label))),
                ]),
                h("label", null, [
                  "Model",
                  h("input", {
                    type: "text",
                    value: state.apiModel,
                    maxlength: 80,
                    autocomplete: "off",
                    onInput: (event) => { state.apiModel = event.target.value; },
                  }),
                ]),
                h("label", { class: "api-key-field" }, [
                  "API key",
                  h("input", {
                    type: "password",
                    value: state.apiKey,
                    autocomplete: "new-password",
                    spellcheck: "false",
                    onInput: (event) => { state.apiKey = event.target.value; },
                  }),
                ]),
                h("small", { class: "truth-note" }, "Not stored in browser storage, URLs, events, artifacts, or logs."),
              ]),
              h("div", { class: "model-execution-fields" }, [
                h("label", null, [
                  "Thinking",
                  h("select", {
                    value: state.thinkingLevel,
                    "aria-label": "Thinking",
                    onChange: (event) => { state.thinkingLevel = event.target.value; },
                  }, THINKING_LEVEL_OPTIONS.map(([value, label]) => h("option", { value }, label))),
                ]),
                h("small", { class: "truth-note" }, "Exact request: unsupported levels fail clearly instead of silently reducing reasoning."),
              ]),
            ]) : null,
          ]),

          h("aside", { class: "product-panel demo-panel", "aria-labelledby": "demo-panel-title" }, [
            h("div", { class: "panel-heading" }, [
              h("span", null, "02"),
              h("div", null, [
                h("h3", { id: "demo-panel-title" }, "Try a reaction figure"),
                h("p", null, "Source-only examples; rights are shown per item. Loading never starts a model."),
              ]),
            ]),
            h("div", { class: "sample-source-list", "aria-busy": String(state.samplesLoading) }, [
              state.samplesLoading
                ? h("p", { class: "truth-note" }, "Loading examples…")
                : featuredSample
                  ? sampleCard(featuredSample, true)
                  : h("p", { class: "truth-note" }, "The example image is unavailable."),
              otherSamples.length ? h("details", { class: "more-samples" }, [
                h("summary", null, `More source examples · ${otherSamples.length}`),
                h("div", { class: "sample-gallery" }, otherSamples.map((sample) => sampleCard(sample))),
              ]) : null,
            ]),
            h("div", { class: `product-account ${state.auth.configured ? "configured" : ""}` }, [
              h("div", null, [
                h("b", null, state.auth.configured ? "ChatGPT / Codex connected" : "Connect ChatGPT / Codex"),
                h("small", null, state.auth.configured
                  ? "The credential is checked only when extraction starts."
                  : "Your login stays in a ChemRxn-only directory."),
              ]),
              h("button", {
                type: "button",
                onClick: () => { state.showAdvanced = true; },
              }, state.auth.configured ? "View settings" : "How to sign in"),
            ]),
            h("div", { class: "product-trust-note" }, [
              h("b", null, "Review before you accept."),
              h("p", null, "Every extracted reaction keeps its source location. Raw evidence remains unchanged when you make a reviewed correction."),
            ]),
          ]),
        ]),
      ]);
    },
  };
}

export function mountFigureUpload(element, options = {}) {
  if (element.__pdfProductApp) element.__pdfProductApp.unmount();
  const backend = options.backend || new RuntimeExtractionBackend();
  const runtimeSessionId = options.runtimeSessionId
    || new URLSearchParams(window.location.search).get("runtime_session");
  const app = createApp(FigureProductApp({
    backend,
    onAttachSession: options.onAttachSession || (() => {}),
    runtimeSessionId,
  }));
  app.mount(element);
  element.__pdfProductApp = app;
  return app;
}
