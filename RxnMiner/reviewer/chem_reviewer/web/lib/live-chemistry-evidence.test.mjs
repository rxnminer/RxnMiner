import assert from "node:assert/strict";
import { test } from "node:test";
import {
  applyLiveEvidenceEvent,
  applyPdfCandidate,
  createVersionedArtifactLoader,
  createLiveEvidenceState,
  summarizeFhrCoverage,
} from "./live-chemistry-evidence.js";

const sid = "session-a";
const event = (seq, phase, data = {}) => ({
  seq,
  sessionId: sid,
  type: "agent.event",
  data: { phase, ...data },
});

test("versioned artifact loader deduplicates a revision and fetches each newer revision once", async () => {
  const calls = [];
  const loader = createVersionedArtifactLoader(async (url) => {
    calls.push(url);
    return { url };
  });
  const [first, duplicate] = await Promise.all([
    loader.load("pdf:main:molparser:1", 4, "/molparser?v=4"),
    loader.load("pdf:main:molparser:1", 4, "/molparser?v=4"),
  ]);
  assert.equal(first.current, true);
  assert.equal(duplicate.current, true);
  assert.equal(calls.length, 1);
  await loader.load("pdf:main:molparser:1", 4, "/molparser?v=4");
  await loader.load("pdf:main:molparser:1", 9, "/molparser?v=9");
  await loader.load("pdf:main:molparser:1", 9, "/molparser?v=9");
  assert.deepEqual(calls, ["/molparser?v=4", "/molparser?v=9"]);
});

test("versioned artifact loader marks a slow prior response stale", async () => {
  const resolvers = new Map();
  const loader = createVersionedArtifactLoader((url) => new Promise((resolve) => {
    resolvers.set(url, resolve);
  }));
  const oldRequest = loader.load("pdf:main:candidate", 10, "/candidate?v=10");
  const newRequest = loader.load("pdf:main:candidate", 12, "/candidate?v=12");
  await Promise.resolve();
  resolvers.get("/candidate?v=12")({ version: 12 });
  const newest = await newRequest;
  assert.equal(newest.current, true);
  resolvers.get("/candidate?v=10")({ version: 10 });
  const stale = await oldRequest;
  assert.equal(stale.current, false);
  assert.deepEqual(newest.value, { version: 12 });
});

test("versioned artifact loader aborts superseded work, retries failures, and disposes pending reads", async () => {
  const attempts = new Map();
  const signals = new Map();
  const loader = createVersionedArtifactLoader(async (url, { signal }) => {
    attempts.set(url, (attempts.get(url) || 0) + 1);
    signals.set(url, signal);
    if (url === "/retry" && attempts.get(url) === 1) throw new Error("transient");
    if (url === "/retry") return "recovered";
    return new Promise((resolve, reject) => {
      signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
    });
  });
  await assert.rejects(loader.load("retry", 1, "/retry"), /transient/);
  assert.equal((await loader.load("retry", 1, "/retry")).value, "recovered");
  assert.equal(attempts.get("/retry"), 2);

  const old = loader.load("candidate", 1, "/old");
  await Promise.resolve();
  const current = loader.load("candidate", 2, "/current");
  await assert.rejects(old, /aborted/);
  assert.equal(signals.get("/old").aborted, true);
  loader.dispose();
  await assert.rejects(current, /aborted/);
  await assert.rejects(loader.load("candidate", 3, "/future"), /disposed/);
});

test("live evidence exposes immutable input, OCSR, chemistry tools, and unsealed candidate", () => {
  const state = createLiveEvidenceState(sid);
  assert.equal(applyLiveEvidenceEvent(state, event(1, "input.validated", {
    inputs: [{
      id: "input-1",
      kind: "figure",
      originalName: "source.png",
      mediaType: "image/png",
      sha256: "a".repeat(64),
    }],
  })), true);
  assert.equal(state.source.name, "source.png");

  applyLiveEvidenceEvent(state, event(2, "pi.tool_execution_start", {
    event: { toolName: "detect", toolCallId: "detect-1" },
  }));
  applyLiveEvidenceEvent(state, event(3, "pi.tool_execution_end", {
    event: {
      toolName: "detect",
      toolCallId: "detect-1",
      isError: false,
      result: { content: [{ type: "text", text: JSON.stringify({
        n: 1,
        n_concrete: 1,
        n_markush: 0,
        detections: [{ bbox_index: 0, bbox: [1, 2, 3, 4], smi: "CCO", score: 0.9, md5: "abc123" }],
        overview_png: "output/perception/figure_overview.png",
        compare_grid_png: "output/perception/figure_ocsr_grid.png",
        raw_molparser_json: "output/perception/figure_molparser.json",
      }) }] },
    },
  }));
  assert.equal(state.detections[0].smi, "CCO");
  assert.equal(state.detections[0].md5, "abc123");
  assert.deepEqual(state.artifacts, {
    overview: true,
    ocsrGrid: true,
    molparser: true,
    candidate: false,
  });

  applyLiveEvidenceEvent(state, event(4, "pi.tool_execution_end", {
    event: {
      toolName: "reverse_rgroup",
      toolCallId: "reverse-1",
      result: { content: [{ type: "text", text: JSON.stringify({ status: "closed", r_map: { R1: "C" } }) }] },
    },
  }));
  assert.equal(state.tools.at(-1).name, "reverse_rgroup");
  assert.equal(state.tools.at(-1).data.status, "closed");

  applyLiveEvidenceEvent(state, event(5, "pi.tool_execution_end", {
    event: {
      toolName: "assemble",
      toolCallId: "assemble-1",
      result: { content: [{ type: "text", text: JSON.stringify({
        v2_path: "output/result.v2.json", n_examples: 2, n_templates: 1,
      }) }] },
    },
  }));
  assert.equal(state.artifacts.candidate, true);
  assert.match(state.tools.at(-1).summary, /2 examples/);
  assert.equal(applyLiveEvidenceEvent(state, {
    seq: 6, sessionId: sid, type: "agent.error", data: { code: "FAILED" },
  }), true);
  assert.equal(state.status, "failed");
});

test("Figure closure visualizes stale verification and closes only after reassembly plus render", () => {
  const state = createLiveEvidenceState(sid);
  applyLiveEvidenceEvent(state, event(1, "input.validated", {
    inputs: [{ id: "fig", kind: "figure", originalName: "figure.png", sha256: "a".repeat(64) }],
  }));
  const finish = (seq, toolName, toolCallId, payload) => applyLiveEvidenceEvent(state, event(seq, "pi.tool_execution_end", {
    event: { toolName, toolCallId, result: { content: [{ type: "text", text: JSON.stringify(payload) }] } },
  }));
  finish(2, "assemble", "a1", {
    v2_path: "output/result.v2.json", candidate_sha256: "1".repeat(64),
    molecule_inventory: [{ key: "selected_examples:1:products:0", role: "products", "E-SMILES": "CC", bbox_index: 11, molparser_md5: "old" }],
    inventory_total: 1,
  });
  finish(3, "render_reactions", "r1", { html_path: "output/reactions.html" });
  applyLiveEvidenceEvent(state, event(4, "pi.tool_execution_start", {
    event: { toolName: "verify_smiles", toolCallId: "v1", observableInputs: { smiles: "C/C=C/C" } },
  }));
  finish(5, "verify_smiles", "v1", { valid: true, input: "C/C=C/C", canonical_iso: "C/C=C/C" });
  assert.equal(state.closure.state, "stale");
  assert.equal(state.closure.assemblyRevision, 1);
  assert.equal(state.closure.postAssemblyVerifications[0].smiles, "C/C=C/C");
  assert.match(state.closure.issues.join(" "), /newer than candidate v1/);
  finish(6, "assemble", "a2", {
    v2_path: "output/result.v2.json", candidate_sha256: "2".repeat(64),
    molecule_inventory: [{ key: "selected_examples:1:products:0", role: "products", "E-SMILES": "CO", bbox_index: 11, molparser_md5: "new" }],
    inventory_total: 1,
  });
  assert.equal(state.closure.state, "stale");
  finish(7, "render_reactions", "r2", { html_path: "output/reactions.html" });
  assert.equal(state.closure.state, "closed");
  assert.equal(state.closure.assemblyRevision, 2);
  assert.deepEqual(state.closure.postAssemblyVerifications, []);
  assert.equal(state.closure.revisions[1].sha256, "2".repeat(64));
  assert.deepEqual(state.closure.candidateDiffs[0], {
    key: "selected_examples:1:products:0",
    before: { key: "selected_examples:1:products:0", identifier: "", role: "products", esmiles: "CC", bboxIndex: 11, molparserMd5: "old", origin: "" },
    after: { key: "selected_examples:1:products:0", identifier: "", role: "products", esmiles: "CO", bboxIndex: 11, molparserMd5: "new", origin: "" },
  });

  applyLiveEvidenceEvent(state, event(8, "extraction.closure_required", {
    issues: ["product claims bbox 11 without molparser_md5"],
    closure: { assemblyRevision: 2 },
  }));
  assert.equal(state.closure.state, "blocked");
  assert.equal(state.status, "needs_attention");
});

test("live PDF evidence tracks Main/SI perception, pages, anchors, and prediction validation", () => {
  const state = createLiveEvidenceState(sid);
  applyLiveEvidenceEvent(state, event(1, "input.validated", {
    inputs: [
      { id: "main-1", kind: "paper", originalName: "paper.pdf", mediaType: "application/pdf", sha256: "a".repeat(64) },
      { id: "si-1", kind: "si", originalName: "si.pdf", mediaType: "application/pdf", sha256: "b".repeat(64) },
    ],
  }));
  assert.deepEqual(state.sources.map((item) => item.sourceDocument), ["main", "si"]);
  const pdf = (seq, type, sourceDocument, data = {}, scope = {}) => ({
    seq,
    sessionId: sid,
    type: "agent.event",
    data: {
      phase: `pdf.${type}`,
      pdfEvent: { type, scope, data: { sourceDocument, ...data } },
    },
  });
  applyLiveEvidenceEvent(state, pdf(2, "document.reading_started", "main"));
  applyLiveEvidenceEvent(state, pdf(3, "page.started", "main", { pageNumber: 1 }));
  applyLiveEvidenceEvent(state, pdf(4, "molparser.completed", "main", {
    pageNumber: 1, detectionCount: 7, rawPerception: "perception/main/pages/page_001_molparser.json",
  }));
  applyLiveEvidenceEvent(state, pdf(5, "anchor.discovered", "main", {
    pageNumber: 1, label: "Scheme 1", bboxPdfNorm: [0.1, 0.2, 0.8, 0.7],
  }, { anchorId: "main:anchor:1" }));
  applyLiveEvidenceEvent(state, pdf(6, "perception.completed", "main", {
    pageCount: 7, anchorCount: 3, segmenter: "uniparser",
  }));
  assert.equal(state.pdf.documents.main.pageCount, 7);
  assert.equal(state.pdf.documents.main.pages[1].detectionCount, 7);
  assert.equal(state.pdf.documents.main.pages[1].molparserRevision, 4);
  assert.equal(state.pdf.documents.main.segmenter, "uniparser");
  assert.equal(state.pdf.documents.main.layoutRevision, 6);
  assert.equal(state.pdf.anchors[0].label, "Scheme 1");
  applyLiveEvidenceEvent(state, pdf(7, "molparser.cache_hit", "si", {
    pageNumber: 1, detectionCount: 2,
  }));
  assert.equal(state.pdf.documents.main.pages[1].molparserRevision, 4);
  assert.equal(state.pdf.documents.si.pages[1].molparserRevision, 7);

  applyLiveEvidenceEvent(state, event(8, "pi.tool_execution_end", {
    event: {
      toolName: "write_pdf_prediction",
      toolCallId: "write-1",
      result: { content: [{ type: "text", text: "VALID" }], details: {
        valid: true,
        byteSize: 1234,
        predictionSha256: "c".repeat(64),
        checkpointPath: `output/checkpoints/pred-${"c".repeat(64)}.json`,
      } },
    },
  }));
  assert.equal(state.artifacts.candidate, true);
  assert.match(state.tools.at(-1).summary, /validated PDF prediction/);
  assert.deepEqual(state.pdf.checkpoints, [{
    sha256: "c".repeat(64),
    path: `output/checkpoints/pred-${"c".repeat(64)}.json`,
    byteSize: 1234,
    seq: 8,
  }]);
  applyLiveEvidenceEvent(state, pdf(9, "validation.started", "main"));
  assert.equal(state.pdf.validation.status, "running");
  applyLiveEvidenceEvent(state, pdf(10, "validation.completed", "main", { valid: true }));
  assert.equal(state.pdf.validation.valid, true);
  applyLiveEvidenceEvent(state, pdf(11, "job.review_ready", "main", { reactionCount: 1 }));
  assert.equal(state.status, "review_ready");
  applyLiveEvidenceEvent(state, event(12, "pi.tool_execution_start", {
    event: {
      toolName: "read_job_file", toolCallId: "read-1",
      observableInputs: { path: "perception/main/pages/page_001_molparser.json" },
    },
  }));
  applyLiveEvidenceEvent(state, event(13, "pi.tool_execution_end", {
    event: {
      toolName: "read_job_file", toolCallId: "read-1",
      result: { content: [{ type: "text", text: "large raw record omitted from UI" }], details: {
        path: "perception/main/pages/page_001_molparser.json", returnedBytes: 2048, totalBytes: 4096,
      } },
    },
  }));
  assert.deepEqual(state.tools.at(-1).inputs, {
    path: "perception/main/pages/page_001_molparser.json",
  });
  assert.match(state.tools.at(-1).summary, /2048 bytes/);
  assert.equal("summary" in state.tools.at(-1).data, false);
  assert.equal(state.activity.some((item) => item.kind === "molparser" && item.pageNumber === 1), true);
  assert.equal(state.activity.at(-1).label.includes("read_job_file"), true);
});

test("validated PDF candidate exposes bounded reaction, condition, provenance, and MolParser links", () => {
  const state = createLiveEvidenceState(sid);
  applyPdfCandidate(state, {
    schema_version: "rxnharbor-pred-v2",
    paper_id: "local-pdf",
    anchors: [{
      anchor_figure: "Scheme S1",
      anchor_page: 12,
      anchor_bbox_pdf: [20, 30, 500, 300],
      _source: { source_document: "si" },
      selected_examples: [{
        instance_id: "S1-a",
        reactants: [{
          identifier: "1a",
          "E-SMILES": "CCO",
          _source: { origin: "image", type: "image_structure", bbox_index: 4 },
        }],
        products: [{
          identifier: "2a",
          "E-SMILES": "CC=O",
          _source: { origin: "image", type: "image_structure", bbox_index: 7 },
        }],
        conditions: [[{
          role: "reagent", text: "PCC, CH2Cl2, 0 °C",
          _source: { origin: "document_text", text_quote: "PCC was added at 0 °C." },
        }]],
      }],
    }],
  }, { valid: true, byteSize: 2048 });
  assert.equal(state.pdf.candidate.valid, true);
  assert.equal(state.pdf.candidate.anchors[0].sourceDocument, "si");
  assert.deepEqual(state.pdf.candidate.anchors[0].bboxPdf, [20, 30, 500, 300]);
  assert.equal(state.pdf.candidate.reactions[0].conditions[0].text, "PCC, CH2Cl2, 0 °C");
  assert.deepEqual(state.pdf.candidate.reactions[0].molecules.map((item) => item.bboxIndex), [4, 7]);
  assert.deepEqual(state.pdf.candidate.reactions[0].provenanceQuotes, ["PCC was added at 0 °C."]);
});

test("FHR coverage distinguishes observed deterministic operations from an unchecked omission", () => {
  assert.deepEqual(summarizeFhrCoverage([]), {
    observed: false, count: 0, complete: 0, failed: 0, names: [],
  });
  assert.deepEqual(summarizeFhrCoverage([
    { name: "reverse_rgroup", status: "complete" },
    { name: "fill_rgroup", status: "failed" },
    { name: "read_job_file", status: "complete" },
  ]), {
    observed: true,
    count: 2,
    complete: 1,
    failed: 1,
    names: ["reverse_rgroup", "fill_rgroup"],
  });
});

test("live evidence ignores thinking events, other sessions, duplicates, and unknown tools", () => {
  const state = createLiveEvidenceState(sid);
  assert.equal(applyLiveEvidenceEvent(state, {
    seq: 1, sessionId: sid, type: "agent.event",
    data: { phase: "pi.message_update", event: { thinking: "private" } },
  }), false);
  assert.equal(applyLiveEvidenceEvent(state, {
    seq: 2, sessionId: "other", type: "agent.event",
    data: { phase: "input.validated", inputs: [] },
  }), false);
  assert.equal(applyLiveEvidenceEvent(state, event(3, "pi.tool_execution_start", {
    event: { toolName: "read", toolCallId: "read-1" },
  })), false);
  assert.equal(state.tools.length, 0);
});
