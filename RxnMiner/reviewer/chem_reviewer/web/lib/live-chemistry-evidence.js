export const LIVE_CHEMISTRY_EVIDENCE_EVENT = "chemrxn-live-chemistry-evidence";

/**
 * Deduplicate immutable/progressive artifact reads by the artifact's own
 * revision rather than by the runtime's global event cursor. A newer revision
 * becomes authoritative as soon as it is requested, so a slow older response
 * can never overwrite it in the workspace.
 */
export function createVersionedArtifactLoader(fetchArtifact) {
  if (typeof fetchArtifact !== "function") throw new TypeError("fetchArtifact must be a function");
  const cache = new Map();
  const inFlight = new Map();
  const latest = new Map();
  let disposed = false;

  return {
    async load(resource, revision, url) {
      if (disposed) throw new Error("artifact loader is disposed");
      if (typeof resource !== "string" || !resource) throw new TypeError("resource must be a non-empty string");
      if (!Number.isInteger(revision) || revision < 0) throw new TypeError("revision must be a non-negative integer");
      const previous = latest.get(resource);
      if (previous === undefined || revision > previous) {
        latest.set(resource, revision);
        for (const entry of inFlight.values()) {
          if (entry.resource === resource && entry.revision < revision) entry.controller.abort();
        }
      }
      const key = JSON.stringify([resource, revision]);
      let request = cache.has(key) ? Promise.resolve(cache.get(key)) : inFlight.get(key)?.request;
      if (!request) {
        const controller = new AbortController();
        const entry = { resource, revision, controller, request: null };
        request = Promise.resolve()
          .then(() => fetchArtifact(url, { signal: controller.signal }))
          .then((value) => {
            if (!disposed && !controller.signal.aborted) cache.set(key, value);
            return value;
          })
          .finally(() => {
            if (inFlight.get(key) === entry) inFlight.delete(key);
          });
        entry.request = request;
        inFlight.set(key, entry);
      }
      const value = await request;
      return { value, revision, current: latest.get(resource) === revision };
    },
    dispose() {
      disposed = true;
      for (const entry of inFlight.values()) entry.controller.abort();
      inFlight.clear();
      cache.clear();
    },
  };
}

const VISIBLE_TOOLS = new Set([
  "detect",
  "ocsr_detect",
  "verify_smiles",
  "reverse_rgroup",
  "fill_rgroup",
  "reverse_assemble",
  "assemble",
  "render_reactions",
  "write_pdf_prediction",
  "seed_pdf_candidates",
  "update_pdf_queue",
  "write_pdf_census",
  "read_job_file",
  "inspect_pdf_page",
]);

export function createLiveEvidenceState(sessionId) {
  return {
    sessionId,
    cursor: 0,
    source: null,
    sources: [],
    detections: [],
    tools: [],
    activity: [],
    artifacts: { overview: false, ocsrGrid: false, molparser: false, candidate: false },
    artifactRevisions: { overview: 0, ocsrGrid: 0, molparser: 0, candidate: 0 },
    closure: {
      assemblyRevision: 0,
      lastAssembleSeq: null,
      lastRenderSeq: null,
      postAssemblyVerifications: [],
      revisions: [],
      candidateDiffs: [],
      state: "waiting",
      issues: [],
    },
    pdf: {
      activeDocument: "main",
      documents: {},
      anchors: [],
      candidate: null,
      validation: { status: "pending", valid: null },
      checkpoints: [],
      stage: "waiting",
    },
    status: "starting",
  };
}

function pushActivity(state, activity) {
  state.activity.push({ seq: state.cursor, ...activity });
  if (state.activity.length > 400) state.activity.splice(0, state.activity.length - 400);
}

function parseToolJson(result, toolName) {
  if (toolName === "read_job_file" || toolName === "inspect_pdf_page") {
    return result?.details && typeof result.details === "object" ? { ...result.details } : {};
  }
  const content = Array.isArray(result?.content) ? result.content : [];
  for (const item of content) {
    if (item?.type !== "text" || typeof item.text !== "string") continue;
    try {
      const parsed = JSON.parse(item.text);
      if (parsed && typeof parsed === "object") {
        return { ...parsed, ...(result?.details && typeof result.details === "object" ? result.details : {}) };
      }
    } catch {
      // A bounded plain-text summary is still useful and safe to display.
      return {
        summary: item.text.slice(0, 4000),
        ...(result?.details && typeof result.details === "object" ? result.details : {}),
      };
    }
  }
  return {};
}

function safeSvg(value) {
  return typeof value === "string" && value.length <= 750_000 && value.includes("<svg")
    ? value : "";
}

function summarizeTool(name, data, isError, inputs = {}) {
  if (isError) return data?.reason || data?.summary || "Tool failed";
  if (name === "read_job_file") {
    return `Read ${inputs.path || data.path || "workspace evidence"}${Number.isFinite(data.returnedBytes) ? ` · ${data.returnedBytes} bytes` : ""}`;
  }
  if (name === "inspect_pdf_page") {
    return `Inspected ${(inputs.source || data.source || "PDF").toUpperCase()} page ${inputs.page || data.page || "?"}`;
  }
  if (name === "detect" || name === "ocsr_detect") {
    return `${Number(data?.n || data?.detections?.length || 0)} structures · ${Number(data?.n_concrete || 0)} concrete · ${Number(data?.n_markush || 0)} Markush`;
  }
  if (name === "verify_smiles") {
    return data?.valid === true
      ? `${data.formula || "valid structure"}${data.canonical_iso ? ` · ${data.canonical_iso}` : ""}`
      : `invalid · ${data?.reason || "could not parse"}`;
  }
  if (name === "assemble") {
    return `${Number(data?.n_examples || 0)} examples · ${Number(data?.n_templates || 0)} templates`;
  }
  if (name === "render_reactions") {
    return `${Number(data?.n_examples || 0)} examples rendered`;
  }
  if (name === "seed_pdf_candidates") {
    return `seeded ${Number(data?.pageCount || 0)} pages · ${Number(data?.candidateCount || 0)} candidates`;
  }
  if (name === "update_pdf_queue") {
    return `${Number(data?.reconciled || 0)}/${Number(data?.total || 0)} candidates reconciled`;
  }
  if (name === "write_pdf_prediction") {
    return data?.valid === true
      ? `validated PDF prediction · ${Number(data?.byteSize || 0)} bytes`
      : data?.summary || "PDF prediction validation failed";
  }
  if (name === "write_pdf_census") {
    return data?.valid === true
      ? `saved completeness census · ${String(data?.censusSha256 || "").slice(0, 12)}`
      : data?.summary || "PDF census validation failed";
  }
  return data?.status || data?.reason || data?.summary || "Deterministic result recorded";
}

function boundedString(value, limit = 2000) {
  return typeof value === "string" ? value.slice(0, limit) : "";
}

function candidateSource(anchor) {
  const source = anchor?._source?.source_document || anchor?._source?.document || anchor?.source_document;
  return String(source || "main").toLowerCase() === "si" ? "si" : "main";
}

function candidateMolecule(value, role, ordinal) {
  const source = value?._source && typeof value._source === "object" ? value._source : {};
  return {
    role,
    ordinal,
    identifier: boundedString(value?.identifier, 500),
    esmiles: boundedString(value?.["E-SMILES"], 4000),
    origin: boundedString(source.origin, 120),
    sourceType: boundedString(source.type, 160),
    bboxIndex: Number.isInteger(source.bbox_index) ? source.bbox_index : null,
    textQuote: boundedString(source.text_quote, 1200),
  };
}

function candidateConditions(value) {
  if (!Array.isArray(value)) return [];
  return value.flatMap((step, stepIndex) => (Array.isArray(step) ? step : [step]).map((item) => ({
    stepIndex,
    role: boundedString(item?.role, 160),
    text: boundedString(item?.text || item?.content, 1200),
    origin: boundedString(item?._source?.origin, 120),
    textQuote: boundedString(item?._source?.text_quote, 1200),
  }))).slice(0, 1000);
}

export function applyPdfCandidate(state, value, validation = {}) {
  const anchors = Array.isArray(value?.anchors) ? value.anchors : [];
  const normalizedAnchors = [];
  const reactions = [];
  for (const [anchorIndex, anchor] of anchors.slice(0, 1000).entries()) {
    if (!anchor || typeof anchor !== "object") continue;
    const sourceDocument = candidateSource(anchor);
    const pageNumber = Number(anchor.anchor_page || 0);
    const anchorId = `candidate:${sourceDocument}:${anchorIndex}`;
    const anchorRecord = {
      id: anchorId,
      sourceDocument,
      pageNumber,
      label: boundedString(anchor.anchor_figure || anchor.anchor_kind || `Anchor ${anchorIndex + 1}`, 500),
      bboxPdf: Array.isArray(anchor.anchor_bbox_pdf) ? anchor.anchor_bbox_pdf.slice(0, 4).map(Number) : [],
      reactionIds: [],
    };
    const examples = Array.isArray(anchor.selected_examples) ? anchor.selected_examples : [];
    for (const [reactionIndex, reaction] of examples.slice(0, 5000).entries()) {
      if (!reaction || typeof reaction !== "object") continue;
      const id = `${anchorId}:reaction:${boundedString(reaction.instance_id, 500) || reactionIndex}`;
      const molecules = [];
      for (const role of ["reactants", "products"]) {
        const values = Array.isArray(reaction[role]) ? reaction[role] : [];
        values.slice(0, 500).forEach((molecule, ordinal) => {
          molecules.push(candidateMolecule(molecule, role === "reactants" ? "reactant" : "product", ordinal));
        });
      }
      const conditions = candidateConditions(reaction.conditions);
      const quotes = [...molecules.map((item) => item.textQuote), ...conditions.map((item) => item.textQuote)]
        .filter(Boolean).slice(0, 20);
      reactions.push({
        id,
        instanceId: boundedString(reaction.instance_id, 500) || String(reactionIndex + 1),
        anchorId,
        sourceDocument,
        pageNumber,
        anchorLabel: anchorRecord.label,
        molecules,
        conditions,
        provenanceQuotes: quotes,
      });
      anchorRecord.reactionIds.push(id);
    }
    normalizedAnchors.push(anchorRecord);
  }
  state.pdf.candidate = {
    paperId: boundedString(value?.paper_id, 500),
    schemaVersion: boundedString(value?.schema_version, 160),
    valid: validation.valid === true,
    byteSize: Number(validation.byteSize || 0),
    anchors: normalizedAnchors,
    reactions,
  };
  return true;
}

function updateFigureClosure(state) {
  const complete = state.tools.filter((tool) => tool.status === "complete");
  const assemblies = complete.filter((tool) => tool.name === "assemble");
  const lastAssembly = assemblies.at(-1) || null;
  const lastRender = complete.filter((tool) => tool.name === "render_reactions").at(-1) || null;
  const postAssemblyVerifications = lastAssembly
    ? complete.filter((tool) => tool.name === "verify_smiles" && tool.seq > lastAssembly.seq)
    : [];
  const revisions = assemblies.map((tool, index) => ({
    revision: index + 1,
    seq: tool.seq,
    sha256: boundedString(tool.data?.candidate_sha256, 128),
    inventory: Array.isArray(tool.data?.molecule_inventory)
      ? tool.data.molecule_inventory.slice(0, 500).map((item) => ({
        key: boundedString(item?.key, 500),
        identifier: boundedString(item?.identifier, 500),
        role: boundedString(item?.role, 80),
        esmiles: boundedString(item?.["E-SMILES"], 4000),
        bboxIndex: Number.isInteger(item?.bbox_index) ? item.bbox_index : null,
        molparserMd5: boundedString(item?.molparser_md5, 128),
        origin: boundedString(item?.origin, 120),
      })) : [],
    inventoryTotal: Number(tool.data?.inventory_total || 0),
    inventoryTruncated: tool.data?.inventory_truncated === true,
  }));
  const candidateDiffs = [];
  if (revisions.length >= 2) {
    const before = new Map(revisions.at(-2).inventory.map((item) => [item.key, item]));
    const after = new Map(revisions.at(-1).inventory.map((item) => [item.key, item]));
    for (const key of new Set([...before.keys(), ...after.keys()])) {
      const oldItem = before.get(key);
      const newItem = after.get(key);
      if (!oldItem || !newItem || oldItem.esmiles !== newItem.esmiles
          || oldItem.bboxIndex !== newItem.bboxIndex || oldItem.molparserMd5 !== newItem.molparserMd5) {
        candidateDiffs.push({ key, before: oldItem || null, after: newItem || null });
      }
    }
  }
  const issues = [];
  if (!lastAssembly) issues.push("No assembled candidate yet");
  if (postAssemblyVerifications.length) {
    issues.push(`${postAssemblyVerifications.length} verification decision(s) are newer than candidate v${assemblies.length}`);
  }
  if (lastAssembly && (!lastRender || lastRender.seq <= lastAssembly.seq)) {
    issues.push(`Candidate v${assemblies.length} has not been rendered after assembly`);
  }
  state.closure = {
    assemblyRevision: assemblies.length,
    lastAssembleSeq: lastAssembly?.seq || null,
    lastRenderSeq: lastRender?.seq || null,
    postAssemblyVerifications: postAssemblyVerifications.map((tool) => ({      id: tool.id,
      seq: tool.seq,
      smiles: boundedString(tool.inputs?.smiles || tool.data?.input, 4000),
      canonical: boundedString(tool.data?.canonical_iso, 4000),
      valid: tool.data?.valid === true,
    })),
    revisions,
    candidateDiffs: candidateDiffs.slice(0, 500),
    state: !lastAssembly ? "waiting" : issues.length ? "stale" : "closed",
    issues,
  };
}

function boundedDetections(value) {
  if (!Array.isArray(value)) return [];
  return value.slice(0, 250).map((item) => ({
    bbox_index: Number.isInteger(item?.bbox_index) ? item.bbox_index : null,
    bbox: Array.isArray(item?.bbox) ? item.bbox.slice(0, 4) : [],
    smi: typeof item?.smi === "string" ? item.smi.slice(0, 2000) : "",
    caption: typeof item?.caption === "string" ? item.caption.slice(0, 3000) : "",
    score: Number.isFinite(item?.score) ? item.score : null,
    markush: item?.markush === true,
    md5: typeof item?.md5 === "string" ? item.md5.slice(0, 128)
      : typeof item?.molparser_md5 === "string" ? item.molparser_md5.slice(0, 128) : "",
  }));
}

export function applyLiveEvidenceEvent(state, runtimeEvent) {
  if (!runtimeEvent || runtimeEvent.sessionId !== state.sessionId) return false;
  if (!Number.isInteger(runtimeEvent.seq) || runtimeEvent.seq <= state.cursor) return false;
  state.cursor = runtimeEvent.seq;
  const data = runtimeEvent.data || {};
  const phase = data.phase;

  if (runtimeEvent.type === "agent.event" && phase === "input.validated") {
    const inputs = Array.isArray(data.inputs) ? data.inputs : [];
    state.sources = inputs.filter((input) => input?.id && input?.kind).map((input) => ({
      id: String(input.id),
      kind: String(input.kind),
      sourceDocument: input.kind === "si" ? "si" : "main",
      name: String(input.originalName || input.kind),
      mediaType: String(input.mediaType || "application/octet-stream"),
      sha256: String(input.sha256 || ""),
    }));
    state.source = state.sources[0] || null;
    for (const source of state.sources.filter((item) => ["paper", "si"].includes(item.kind))) {
      state.pdf.documents[source.sourceDocument] = {
        source,
        pageCount: 0,
        anchorCount: 0,
        segmenter: "pending",
        status: "validated",
        layoutRevision: 0,
        pages: {},
      };
    }
    if (state.sources.length) state.status = "running";
    pushActivity(state, {
      kind: "source", status: "complete",
      label: `Validated ${state.sources.map((item) => item.sourceDocument === "si" ? "SI" : item.kind === "paper" ? "Main PDF" : "Figure").join(" + ")}`,
    });
    return true;
  }

  if (runtimeEvent.type === "agent.event" && typeof phase === "string" && phase.startsWith("pdf.")) {
    if (phase === "pdf.perception_ready") {
      state.pdf.stage = "agent_extraction";
      pushActivity(state, { kind: "perception", status: "complete", label: "Perception bundle ready for Pi extraction" });
      return true;
    }
    const pdfEvent = data.pdfEvent || {};
    const eventData = pdfEvent.data || {};
    const sourceDocument = eventData.sourceDocument === "si" ? "si" : "main";
    const document = state.pdf.documents[sourceDocument] || {
      source: state.sources.find((item) => item.sourceDocument === sourceDocument) || null,
      pageCount: 0,
      anchorCount: 0,
      segmenter: "pending",
      status: "reading",
      layoutRevision: 0,
      pages: {},
    };
    state.pdf.documents[sourceDocument] = document;
    if (pdfEvent.type === "document.reading_started") {
      document.status = "reading";
      state.pdf.stage = "layout";
      pushActivity(state, { kind: "document", status: "running", sourceDocument, label: `Reading ${sourceDocument === "si" ? "SI" : "Main PDF"}` });
    } else if (["page.started", "page.completed", "molparser.started", "molparser.completed", "molparser.cache_hit"].includes(pdfEvent.type)) {
      const pageNumber = Number(eventData.pageNumber || 0);
      if (pageNumber > 0) {
        const page = document.pages[pageNumber] || {
          pageNumber,
          status: "rendered",
          detectionCount: null,
          renderRevision: 0,
          molparserRevision: 0,
        };
        if (pdfEvent.type === "molparser.started") page.status = "recognizing_structures";
        if (pdfEvent.type === "molparser.completed" || pdfEvent.type === "molparser.cache_hit") {
          page.status = pdfEvent.type === "molparser.cache_hit" ? "cache_hit" : "recognized";
          page.detectionCount = Number(eventData.detectionCount || 0);
          page.rawPerception = String(eventData.rawPerception || "");
          page.molparserRevision = runtimeEvent.seq;
        }
        if (pdfEvent.type === "page.completed") {
          page.completed = true;
          page.renderRevision = runtimeEvent.seq;
        }
        document.pages[pageNumber] = page;
        state.pdf.stage = "perception";
        if (pdfEvent.type === "page.completed") pushActivity(state, {
          kind: "page", status: "complete", sourceDocument, pageNumber,
          label: `Rendered ${sourceDocument.toUpperCase()} page ${pageNumber}`,
        });
        if (pdfEvent.type === "molparser.completed" || pdfEvent.type === "molparser.cache_hit") pushActivity(state, {
          kind: "molparser", status: pdfEvent.type === "molparser.cache_hit" ? "cache_hit" : "complete",
          sourceDocument, pageNumber,
          label: `MolParser ${pdfEvent.type === "molparser.cache_hit" ? "cache hit" : "recognized"} ${Number(eventData.detectionCount || 0)} structures`,
        });
      }
    } else if (pdfEvent.type === "anchor.discovered") {
      state.pdf.anchors.push({
        id: String(pdfEvent.scope?.anchorId || `anchor:${runtimeEvent.seq}`),
        sourceDocument,
        pageNumber: Number(eventData.pageNumber || 0),
        label: String(eventData.label || eventData.sourceKey || "Reaction region"),
        bboxPdfNorm: Array.isArray(eventData.bboxPdfNorm) ? eventData.bboxPdfNorm.slice(0, 4) : [],
      });
      pushActivity(state, {
        kind: "anchor", status: "provisional", sourceDocument,
        pageNumber: Number(eventData.pageNumber || 0),
        label: `Discovered ${String(eventData.label || eventData.sourceKey || "reaction region")}`,
      });
    } else if (pdfEvent.type === "perception.completed") {
      document.status = "ready";
      document.pageCount = Number(eventData.pageCount || Object.keys(document.pages).length);
      document.anchorCount = Number(eventData.anchorCount || 0);
      document.segmenter = String(eventData.segmenter || "unknown");
      document.layoutRevision = runtimeEvent.seq;
      state.pdf.stage = "agent_extraction";
      pushActivity(state, {
        kind: "uniparser", status: "complete", sourceDocument,
        label: `${String(eventData.segmenter || "layout")} completed · ${document.pageCount} pages · ${document.anchorCount} candidate regions`,
      });
    } else if (pdfEvent.type === "validation.started") {
      state.pdf.validation = { status: "running", valid: null };
      state.pdf.stage = "validation";
      pushActivity(state, { kind: "validation", status: "running", label: "Running frozen prediction validator" });
    } else if (pdfEvent.type === "validation.completed") {
      state.pdf.validation = { status: "complete", valid: eventData.valid === true };
      state.pdf.stage = "sealing";
      pushActivity(state, { kind: "validation", status: eventData.valid === true ? "complete" : "failed", label: eventData.valid === true ? "Prediction validation passed" : "Prediction validation unresolved" });
    } else if (pdfEvent.type === "job.review_ready" || pdfEvent.type === "job.partial") {
      state.status = pdfEvent.type === "job.review_ready" ? "review_ready" : "partial";
      state.pdf.stage = state.status;
      pushActivity(state, { kind: "seal", status: state.status, label: state.status === "review_ready" ? "Sealed review workspace ready" : "Partial result preserved for review" });
    }
    return true;
  }

  if (runtimeEvent.type === "agent.event" && phase === "extraction.awaiting_artifact") {
    state.status = "needs_attention";
    state.pdf.stage = "awaiting_valid_prediction";
    pushActivity(state, { kind: "candidate", status: "needs_attention", label: "Pi turn ended without a validator-clean prediction" });
    return true;
  }

  if (runtimeEvent.type === "agent.event" && phase === "pi.tool_execution_start") {
    const tool = data.event || {};
    if (!VISIBLE_TOOLS.has(tool.toolName)) return false;
    state.tools.push({
      id: String(tool.toolCallId || `${tool.toolName}:${runtimeEvent.seq}`),
      name: tool.toolName,
      status: "running",
      summary: "Running…",
      seq: runtimeEvent.seq,
      data: {},
      inputs: tool.observableInputs && typeof tool.observableInputs === "object" ? tool.observableInputs : {},
      svg: "",
    });
    pushActivity(state, { kind: "tool", status: "running", toolId: String(tool.toolCallId || ""), label: `Started ${tool.toolName}` });
    if (state.tools.length > 120) state.tools.splice(0, state.tools.length - 120);
    return true;
  }

  if (runtimeEvent.type === "agent.event" && phase === "pi.tool_execution_end") {
    const tool = data.event || {};
    if (!VISIBLE_TOOLS.has(tool.toolName)) return false;
    const parsed = parseToolJson(tool.result, tool.toolName);
    const found = [...state.tools].reverse().find((item) =>
      item.id === tool.toolCallId || (item.name === tool.toolName && item.status === "running"));
    const record = found || {
      id: String(tool.toolCallId || `${tool.toolName}:${runtimeEvent.seq}`),
      name: tool.toolName,
      seq: runtimeEvent.seq,
    };
    record.status = tool.isError === true || tool.result?.isError === true ? "failed" : "complete";
    record.summary = summarizeTool(tool.toolName, parsed, record.status === "failed", record.inputs || {});
    record.data = parsed;
    record.svg = safeSvg(parsed.svg);
    record.seq = runtimeEvent.seq;
    if (!found) state.tools.push(record);
    pushActivity(state, {
      kind: "tool", status: record.status, toolId: record.id,
      label: `${record.status === "failed" ? "Failed" : "Completed"} ${tool.toolName}: ${record.summary}`,
    });

    if (tool.toolName === "detect" || tool.toolName === "ocsr_detect") {
      state.detections = boundedDetections(parsed.detections);
      state.artifacts.overview = Boolean(parsed.overview_png);
      state.artifacts.ocsrGrid = Boolean(parsed.compare_grid_png);
      state.artifacts.molparser = Boolean(parsed.raw_molparser_json);
      if (state.artifacts.overview) state.artifactRevisions.overview = runtimeEvent.seq;
      if (state.artifacts.ocsrGrid) state.artifactRevisions.ocsrGrid = runtimeEvent.seq;
      if (state.artifacts.molparser) state.artifactRevisions.molparser = runtimeEvent.seq;
    }
    if (tool.toolName === "assemble") {
      state.artifacts.candidate = Boolean(parsed.v2_path);
      if (state.artifacts.candidate) state.artifactRevisions.candidate = runtimeEvent.seq;
    }
    if (state.source?.kind === "figure") updateFigureClosure(state);
    if (tool.toolName === "write_pdf_prediction") {
      state.artifacts.candidate = parsed.valid === true;
      if (state.artifacts.candidate) state.artifactRevisions.candidate = runtimeEvent.seq;
      if (parsed.valid === true && /^[a-f0-9]{64}$/.test(String(parsed.predictionSha256 || ""))) {
        const checkpoint = {
          sha256: String(parsed.predictionSha256),
          path: boundedString(parsed.checkpointPath, 500),
          byteSize: Number(parsed.byteSize || 0),
          seq: runtimeEvent.seq,
        };
        if (!state.pdf.checkpoints.some((item) => item.sha256 === checkpoint.sha256)) {
          state.pdf.checkpoints.push(checkpoint);
          if (state.pdf.checkpoints.length > 200) state.pdf.checkpoints.shift();
        }
      }
    }
    return true;
  }

  if (runtimeEvent.type === "agent.event" && phase === "extraction.closure_required") {
    const closure = data.closure && typeof data.closure === "object" ? data.closure : {};
    state.closure = {
      ...state.closure,
      state: "blocked",
      issues: Array.isArray(data.issues) ? data.issues.slice(0, 100).map(String) : state.closure.issues,
      assemblyRevision: Number.isInteger(closure.assemblyRevision)
        ? closure.assemblyRevision : state.closure.assemblyRevision,
    };
    state.status = "needs_attention";
    pushActivity(state, { kind: "closure", status: "blocked", label: "Seal blocked: verification/provenance closure required" });
    return true;
  }

  if (["job.review_ready", "job.partial", "session.failed", "session.cancelled", "agent.error"].includes(runtimeEvent.type)) {
    state.status = runtimeEvent.type === "agent.error"
      ? "failed" : runtimeEvent.type.replace("job.", "").replace("session.", "");
    pushActivity(state, { kind: "terminal", status: state.status, label: `Extraction ${state.status.replaceAll("_", " ")}` });
    return true;
  }
  return false;
}

export function summarizeFhrCoverage(tools) {
  const names = ["reverse_rgroup", "fill_rgroup", "reverse_assemble", "assemble"];
  const observed = (Array.isArray(tools) ? tools : []).filter((tool) => names.includes(tool?.name));
  return {
    observed: observed.length > 0,
    count: observed.length,
    complete: observed.filter((tool) => tool.status === "complete").length,
    failed: observed.filter((tool) => tool.status === "failed").length,
    names: [...new Set(observed.map((tool) => tool.name))],
  };
}

export function dispatchLiveEvidenceEvent(runtimeEvent, target = window) {
  const data = runtimeEvent?.data || {};
  const phase = data.phase;
  const visible = phase === "input.validated"
    || phase === "extraction.awaiting_artifact"
    || (typeof phase === "string" && phase.startsWith("pdf."))
    || phase === "pi.tool_execution_start"
    || phase === "pi.tool_execution_end"
    || ["job.review_ready", "job.partial", "session.failed", "session.cancelled", "agent.error"].includes(runtimeEvent?.type);
  if (!visible) return false;
  target.dispatchEvent(new CustomEvent(LIVE_CHEMISTRY_EVIDENCE_EVENT, {
    detail: runtimeEvent,
  }));
  return true;
}
