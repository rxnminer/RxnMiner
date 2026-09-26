export const EXTRACTION_STAGES = Object.freeze([
  "draft",
  "uploaded",
  "queued",
  "reading_document",
  "freezing_perception",
  "extracting",
  "validating",
  "review_ready",
  "failed",
  "cancelled",
]);

export const STAGE_COPY = Object.freeze({
  draft: "Choose a reaction image.",
  uploaded: "Files are validated locally and ready to submit.",
  queued: "Waiting for an extraction worker.",
  reading_document: "Reading the reaction image.",
  freezing_perception: "Freezing auditable perception artifacts.",
  extracting: "Building structured reaction records.",
  validating: "Validating the prediction package and trace.",
  review_ready: "The extracted reactions are ready for review.",
  failed: "Extraction stopped with an error.",
  cancelled: "Extraction was cancelled by the reviewer.",
});

export class ExtractionBackend {
  async capabilities() { throw new Error("ExtractionBackend.capabilities is not implemented"); }
  async resolveModel(_input) { throw new Error("ExtractionBackend.resolveModel is not implemented"); }
  async create(_input) { throw new Error("ExtractionBackend.create is not implemented"); }
  async status(_jobId) { throw new Error("ExtractionBackend.status is not implemented"); }
  async cancel(_jobId) { throw new Error("ExtractionBackend.cancel is not implemented"); }
  async retry(_jobId) { throw new Error("ExtractionBackend.retry is not implemented"); }
  async result(_jobId) { throw new Error("ExtractionBackend.result is not implemented"); }
}

export async function createPreflightedExtraction(backend, modelInput, buildInput) {
  if (!backend || typeof backend.resolveModel !== "function") {
    const error = new Error("The extraction backend cannot validate the selected model");
    error.code = "MODEL_PREFLIGHT_UNAVAILABLE";
    throw error;
  }
  if (typeof backend.create !== "function" || typeof buildInput !== "function") {
    throw new TypeError("A connected extraction backend and input builder are required");
  }
  await backend.resolveModel(modelInput);
  return backend.create(buildInput());
}

/**
 * Same-origin adapter for the real local Pi runtime. The browser never receives
 * the runtime bearer token; the FastAPI front door enforces the current tenant.
 */
export class RuntimeExtractionBackend extends ExtractionBackend {
  async authStatus() {
    const response = await fetch("/api/runtime/auth/status", {
      headers: { Accept: "application/json" },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      return {
        configured: false,
        provider: "openai-codex",
        profile: "chemrxn_isolated",
        loginCommand: "./chemrxn login",
      };
    }
    return body;
  }

  async capabilities() {
    const response = await fetch("/api/runtime/capabilities", {
      headers: { Accept: "application/json" },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      return {
        connected: false,
        inputs: { figure: false, paper: false, "paper-si": false },
        message: body?.detail || "Interactive Pi backend not connected",
      };
    }
    return body;
  }

  async resolveModel({ provider, modelId, requireImage = false }) {
    const response = await fetch("/api/runtime/models/resolve", {
      method: "POST",
      headers: {
        Accept: "application/json",
        "content-type": "application/json",
      },
      body: JSON.stringify({ provider, modelId, requireImage }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const runtimeError = body?.error && typeof body.error === "object" ? body.error : body;
      const error = new Error(
        runtimeError?.message || body?.detail || `Model preflight failed (HTTP ${response.status})`,
      );
      error.code = runtimeError?.code || body?.code || "MODEL_PREFLIGHT_FAILED";
      throw error;
    }
    return body;
  }

  async create(input) {
    if (!(input instanceof FormData)) throw new Error("Extraction input must be FormData");
    const response = await fetch("/api/runtime/extractions", {
      method: "POST",
      body: input,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(body?.detail || `Extraction could not start (HTTP ${response.status})`);
      error.code = body?.code || "EXTRACTION_CREATE_FAILED";
      throw error;
    }
    return body;
  }

  async status(jobId) {
    const response = await fetch(`/api/runtime/sessions/${encodeURIComponent(jobId)}`);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body?.detail || `Session status failed (HTTP ${response.status})`);
    return body.session || body;
  }

  async cancel(jobId) {
    const response = await fetch(
      `/api/runtime/sessions/${encodeURIComponent(jobId)}/abort`,
      { method: "POST" },
    );
    if (!response.ok) throw new Error(`Cancellation failed (HTTP ${response.status})`);
    return response.json();
  }

  async retry(jobId) {
    const response = await fetch(
      `/api/runtime/sessions/${encodeURIComponent(jobId)}/retry`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: "{}",
      },
    );
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const runtimeError = body?.error && typeof body.error === "object" ? body.error : body;
      const error = new Error(
        runtimeError?.message || body?.detail || `Retry failed (HTTP ${response.status})`,
      );
      error.code = runtimeError?.code || body?.code || "RETRY_FAILED";
      throw error;
    }
    return body;
  }

  async result(jobId) {
    const response = await fetch(`/api/runtime/sessions/${encodeURIComponent(jobId)}/result`);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body?.detail || `Result failed (HTTP ${response.status})`);
    return body;
  }
}
