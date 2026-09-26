export class PdfInteractionBackend {
  async capabilities(_jobId) {
    throw new Error("PdfInteractionBackend.capabilities is not implemented");
  }

  async run(_input) {
    throw new Error("PdfInteractionBackend.run is not implemented");
  }

  async cancel(_interactionId) {
    throw new Error("PdfInteractionBackend.cancel is not implemented");
  }

  async saveView(_interactionId, _title) {
    throw new Error("PdfInteractionBackend.saveView is not implemented");
  }
}

const PAPER_50_EXAMPLES = Object.freeze({
  limitations: {
    id: "demo-limitations",
    title: "Limitations and unsuccessful examples",
    summary: "Six limitation records are present in the sealed example review fixture.",
    output: "cards",
    reactionIds: ["pred:1:0", "pred:0:0"],
    claimIds: [],
    evidenceIds: [],
    warnings: [
      "No live document re-read was performed.",
      "SI citations open only when an SI artifact is attached to the workspace.",
    ],
    items: [
      {
        title: "Scheme 1 · resolution ≤50%",
        reactionIds: ["pred:1:0"],
        status: "failed example",
        chemistry: "Partial reaction record; numeric yield requires review.",
        conditions: "Resolution conditions are reported in the main paper.",
        metrics: "Yield: ≤50% · scale not asserted",
        evidence: [
          { source: "main", page: 2, anchor: 1, bbox: [54.4, 113.2, 558.8, 415.6],
            quote: "Resolution afforded the desired material in ≤50% yield." },
        ],
        uncertainty: "The sealed prediction omitted this numeric yield.",
      },
      {
        title: "Figure 1 · contextual route",
        reactionIds: ["pred:0:0"],
        status: "template / context",
        chemistry: "Contextual overview, not counted as a newly extracted experimental example.",
        conditions: "No complete experimental conditions in the figure.",
        metrics: "No reportable yield",
        evidence: [
          { source: "main", page: 1, bbox: [72, 94, 540, 390],
            quote: "Route overview shown for context." },
        ],
        uncertainty: "Do not promote contextual artwork into a confirmed reaction record.",
      },
      {
        title: "Four additional limitation rows",
        reactionIds: [],
        status: "unresolved",
        chemistry: "Grouped in this compact sealed-demo result.",
        conditions: "Open the evidence view for row-level text.",
        metrics: "Mixed reported and unavailable yields",
        evidence: [
          { source: "si", page: 18, quote: "Representative unsuccessful and low-conversion examples." },
        ],
        uncertainty: "Open this source-specific citation in an attached SI document.",
      },
    ],
  },
  table3: {
    id: "demo-table3",
    title: "Table 3 entry 7 · 14 → 8 + Pummerer BP",
    summary: "The sealed entry-7 worked example links the canonical prediction row to main and SI evidence.",
    output: "table",
    reactionIds: ["pred:6:6"],
    claimIds: [],
    evidenceIds: [],
    warnings: [
      "No live document re-read was performed.",
      "SI citations open only when an SI artifact is attached to the workspace.",
    ],
    columns: ["Entry", "Substrate", "Conditions", "Outcome", "Evidence"],
    rows: [
      ["7", "14", "Pummerer oxidation conditions", "8 + Pummerer BP",
        "main p4 · Table 3"],
      ["S3-7", "14", "expanded screening conditions", "8 / BP outcome detail",
        "SI p31 · screening table"],
    ],
    rowScopes: [
      { source: "main", page: 4, anchor: 6, reactionId: "pred:6:6" },
      { source: "si", page: 31, anchor: null, reactionId: null },
    ],
    evidence: [
      { source: "main", page: 4, anchor: 6, bbox: [353.45, 162.48, 515.42, 194.98],
        quote: "Entry 7 records conversion of 14 to 8 with the Pummerer byproduct." },
      { source: "si", page: 31, quote: "Expanded condition screen for the Pummerer step." },
    ],
  },
  stereo9: {
    id: "demo-stereo9",
    title: "Compound 9 · strict-stereo inspection",
    summary: "Connectivity is consistent, while the drawn stereobond requires an OCSR correction.",
    output: "report",
    reactionIds: ["pred:1:6", "pred:1:7"],
    claimIds: [],
    evidenceIds: [],
    warnings: ["This is a sealed reviewed example, not a new OCSR execution."],
    badges: [
      ["Connectivity", "consistent"],
      ["Strict stereo", "mismatch"],
      ["Diagnosis", "OCSR error"],
    ],
    comparison: {
      rawMolParser: "C[C@H](O)/C=C\\C",
      extracted: "C[C@H](O)/C=C/C",
      reviewedCorrection: "C[C@H](O)/C=C\\C",
      note: "The / versus \\ relation changes alkene stereochemistry; no-stereo equivalence is not correctness.",
    },
    evidence: [
      { source: "main", page: 2, anchor: 1, bbox: [350, 150, 505, 260],
        quote: "Compound 9 is shown with the stereochemistry drawn in Scheme 1." },
    ],
  },
});

function backendError(message, code = "BACKEND_NOT_CONNECTED") {
  const error = new Error(message);
  error.code = code;
  return error;
}

function scopeFixture(fixture, input) {
  const scoped = structuredClone(fixture);
  const selection = input.selection || {};
  const allows = (evidence) => {
    if (input.context === "paper" && evidence.source === "si") return false;
    if (input.context === "figure"
        && (evidence.source !== "main" || !Array.isArray(evidence.bbox))) return false;
    if (input.scope === "main" && evidence.source !== "main") return false;
    if (input.scope === "si" && evidence.source !== "si") return false;
    if (input.scope === "page" && Number(evidence.page) !== Number(selection.page)) return false;
    if (["anchor", "selection"].includes(input.scope)
        && selection.anchor != null
        && evidence.source === "main"
        && Number(evidence.anchor) !== Number(selection.anchor)) return false;
    return true;
  };
  if (Array.isArray(scoped.items)) {
    scoped.items = scoped.items.map((item) => ({
      ...item,
      evidence: (item.evidence || []).filter(allows),
    })).filter((item) => item.evidence.length);
    scoped.reactionIds = [...new Set(scoped.items.flatMap((item) => item.reactionIds || []))];
  }
  if (Array.isArray(scoped.evidence)) scoped.evidence = scoped.evidence.filter(allows);
  if (Array.isArray(scoped.rows) && Array.isArray(scoped.rowScopes)) {
    const kept = scoped.rows.map((row, index) => ({ row, scope: scoped.rowScopes[index] }))
      .filter((entry) => entry.scope && allows(entry.scope));
    scoped.rows = kept.map((entry) => entry.row);
    scoped.rowScopes = kept.map((entry) => entry.scope);
    scoped.reactionIds = [...new Set(kept.map((entry) => entry.scope.reactionId).filter(Boolean))];
  }
  const evidenceCount = (scoped.items || []).reduce((total, item) =>
    total + (item.evidence || []).length, 0) + (scoped.evidence || []).length;
  if (!evidenceCount) {
    throw backendError(
      "The sealed fixture has no evidence inside the selected scope.",
      "CAPABILITY_UNAVAILABLE",
    );
  }
  const omitted = [];
  if (input.context !== "paper+si") omitted.push("SI evidence outside the selected context was not checked.");
  if (input.scope !== "paper") omitted.push(`Evidence outside scope “${input.scope}” was not checked.`);
  scoped.warnings = [...(scoped.warnings || []), ...omitted];
  return scoped;
}

/**
 * Deterministic, sealed UI fixture. It never starts Pi, a model, OCSR, or a timer.
 * Unknown queries fail honestly instead of fabricating an answer.
 */
export class MockPdfInteractionBackend extends PdfInteractionBackend {
  constructor({ demoJobId = "pdf_issue28_wholepage_p50" } = {}) {
    super();
    this.demoJobId = demoJobId;
    this.results = new Map();
  }

  async capabilities(jobId) {
    const sealedDemo = jobId === this.demoJobId;
    return {
      connected: false,
      sealedDemo,
      queryIndex: false,
      synthesizeEvidence: false,
      reinspectSelection: false,
      liveQuery: false,
      inspectSelection: sealedDemo,
      mainEvidenceNavigation: sealedDemo,
      siEvidenceNavigation: false,
      message: sealedDemo
        ? "Interactive Pi backend not connected. Sealed demo responses only."
        : "Interaction backend not connected.",
    };
  }

  async run(input = {}) {
    if (input.jobId !== this.demoJobId || input.demo !== true) {
      throw backendError("Interaction backend not connected");
    }
    const text = `${input.prompt || ""} ${input.selection?.label || ""}`.toLowerCase();
    let fixture = null;
    if (/limitation|failed|低收率|限制|失败/.test(text)) fixture = PAPER_50_EXAMPLES.limitations;
    else if (/table\s*3|pummerer|bp/.test(text)) fixture = PAPER_50_EXAMPLES.table3;
    else if (/(compound|product)?\s*9|stereo|立体/.test(text)) fixture = PAPER_50_EXAMPLES.stereo9;
    if (!fixture) {
      throw backendError(
        "This UI-only build has no sealed response for that request.",
        "CAPABILITY_UNAVAILABLE",
      );
    }
    if (input.mode === "reinspect_selection" && !input.selection?.id) {
      throw backendError("Reinspect selection requires an active selection.", "CAPABILITY_UNAVAILABLE");
    }
    if (input.mode === "reinspect_selection") {
      const supported = fixture.id === "demo-table3"
        ? input.selection.id === "pred:6:6"
        : fixture.id === "demo-stereo9"
          ? ["pred:1:6", "pred:1:7"].includes(input.selection.id)
          : false;
      if (!supported) {
        throw backendError(
          "This selection has no sealed reinspect example.",
          "CAPABILITY_UNAVAILABLE",
        );
      }
    }
    if (input.output && input.output !== fixture.output) {
      throw backendError(
        `The sealed ${fixture.title} example is available only as ${fixture.output}.`,
        "CAPABILITY_UNAVAILABLE",
      );
    }
    const scopedFixture = scopeFixture(fixture, input);
    const interactionId = `${fixture.id}:${input.scope || "paper"}`;
    const result = {
      ...scopedFixture,
      interactionId,
      sealedDemo: true,
      backendVersion: "mock-example-job-v1",
      request: {
        prompt: input.prompt || "",
        scope: input.scope || "paper",
        mode: input.mode || "query_index",
        context: input.context || "paper",
        inference: input.inference || "drawn-only",
        output: fixture.output,
        selection: input.selection || null,
      },
    };
    this.results.set(interactionId, result);
    return result;
  }

  async cancel(interactionId) {
    if (!this.results.has(interactionId)) {
      throw backendError("No running interaction exists.", "CAPABILITY_UNAVAILABLE");
    }
    return { interactionId, cancelled: true };
  }

  async saveView(interactionId, title) {
    const result = this.results.get(interactionId);
    if (!result) throw backendError("Run a sealed demo query before saving a view.", "CAPABILITY_UNAVAILABLE");
    return { interactionId, title: title || result.title, result };
  }
}
