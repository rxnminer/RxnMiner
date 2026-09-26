import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import {
  createPreflightedExtraction,
  RuntimeExtractionBackend,
} from "./extraction-backend.js";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("runtime capability adapter exposes connected input modes", async () => {
  globalThis.fetch = async (url, options = {}) => {
    assert.equal(url, "/api/runtime/capabilities");
    assert.equal(options.headers.Accept, "application/json");
    return new Response(JSON.stringify({
      connected: true,
      inputs: { figure: true, paper: true, "paper-si": true },
      message: "Live extraction connected.",
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  const capability = await new RuntimeExtractionBackend().capabilities();
  assert.equal(capability.connected, true);
  assert.deepEqual(capability.inputs, {
    figure: true,
    paper: true,
    "paper-si": true,
  });
});

test("runtime capability adapter converts unavailable service to honest disconnected state", async () => {
  globalThis.fetch = async () => new Response(
    JSON.stringify({ detail: "Interactive Pi backend not connected" }),
    { status: 503, headers: { "Content-Type": "application/json" } },
  );
  const capability = await new RuntimeExtractionBackend().capabilities();
  assert.equal(capability.connected, false);
  assert.deepEqual(capability.inputs, {
    figure: false,
    paper: false,
    "paper-si": false,
  });
  assert.match(capability.message, /not connected/i);
});

test("runtime model preflight sends only provider capability data", async () => {
  globalThis.fetch = async (url, options = {}) => {
    assert.equal(url, "/api/runtime/models/resolve");
    assert.equal(options.method, "POST");
    assert.equal(options.headers["content-type"], "application/json");
    assert.deepEqual(JSON.parse(options.body), {
      provider: "byo-gemini",
      modelId: "gemini-3.1-pro-preview",
      requireImage: true,
    });
    assert.equal(options.body.includes("apiKey"), false);
    return new Response(JSON.stringify({
      available: true,
      provider: "google",
      modelId: "gemini-3.1-pro-preview",
      supportsImages: true,
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  const resolved = await new RuntimeExtractionBackend().resolveModel({
    provider: "byo-gemini",
    modelId: "gemini-3.1-pro-preview",
    requireImage: true,
  });
  assert.equal(resolved.available, true);
  assert.equal(resolved.supportsImages, true);
});

test("runtime model preflight surfaces a typed Pi SDK rejection", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({
    error: {
      code: "MODEL_NOT_AVAILABLE",
      message: "Model openai/missing is not available in the locked Pi SDK",
    },
  }), { status: 409, headers: { "Content-Type": "application/json" } });
  await assert.rejects(
    new RuntimeExtractionBackend().resolveModel({
      provider: "byo-openai",
      modelId: "missing",
      requireImage: true,
    }),
    (error) => error.code === "MODEL_NOT_AVAILABLE" && /locked Pi SDK/.test(error.message),
  );
});

test("preflighted create fails closed before building or sending source bytes", async () => {
  let built = 0;
  let created = 0;
  const buildInput = () => {
    built += 1;
    return new FormData();
  };
  await assert.rejects(
    createPreflightedExtraction({
      async create() { created += 1; },
    }, {
      provider: "byo-openai",
      modelId: "gpt-5.6-terra",
      requireImage: true,
    }, buildInput),
    (error) => error.code === "MODEL_PREFLIGHT_UNAVAILABLE",
  );
  assert.equal(built, 0);
  assert.equal(created, 0);

  await assert.rejects(
    createPreflightedExtraction({
      async resolveModel() {
        const error = new Error("Model rejected");
        error.code = "MODEL_NOT_AVAILABLE";
        throw error;
      },
      async create() { created += 1; },
    }, {
      provider: "byo-openai",
      modelId: "missing",
      requireImage: true,
    }, buildInput),
    (error) => error.code === "MODEL_NOT_AVAILABLE",
  );
  assert.equal(built, 0);
  assert.equal(created, 0);
});

test("runtime retry creates a new live attempt through the same-origin adapter", async () => {
  globalThis.fetch = async (url, options = {}) => {
    assert.equal(url, "/api/runtime/sessions/cancelled%2Fsession/retry");
    assert.equal(options.method, "POST");
    assert.equal(options.headers["content-type"], "application/json");
    assert.equal(options.body, "{}");
    return new Response(JSON.stringify({
      retried: true,
      relation: "retry",
      previousSessionId: "cancelled/session",
      session: { id: "new-attempt", status: "running" },
    }), { status: 201, headers: { "Content-Type": "application/json" } });
  };
  const retried = await new RuntimeExtractionBackend().retry("cancelled/session");
  assert.equal(retried.retried, true);
  assert.equal(retried.session.id, "new-attempt");
});

test("runtime retry reports missing BYO credentials without fake progress", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({
    error: {
      code: "SESSION_NOT_ACTIVE",
      message: "The BYO API key is no longer in memory. Supply it again to retry.",
    },
  }), { status: 409, headers: { "Content-Type": "application/json" } });
  await assert.rejects(
    new RuntimeExtractionBackend().retry("interrupted-session"),
    (error) => error.code === "SESSION_NOT_ACTIVE" && /Supply it again/.test(error.message),
  );
});
