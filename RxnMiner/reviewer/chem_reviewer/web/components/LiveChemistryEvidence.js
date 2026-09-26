import { createApp, h, onMounted, onUnmounted, reactive, ref } from "vue";
import { BBoxImagePanel } from "/static/components/BBoxImagePanel.js?v=20260801e";
import {
  applyLiveEvidenceEvent,
  applyPdfCandidate,
  createVersionedArtifactLoader,
  createLiveEvidenceState,
  LIVE_CHEMISTRY_EVIDENCE_EVENT,
  summarizeFhrCoverage,
} from "/static/lib/live-chemistry-evidence.js?v=20260731c";

function artifactUrl(sessionId, kind, revision) {
  return `/api/runtime/sessions/${encodeURIComponent(sessionId)}/live-artifacts/${encodeURIComponent(kind)}?v=${revision}`;
}

function inputUrl(id) {
  return `/api/runtime/input-artifacts/${encodeURIComponent(id)}`;
}

function pdfArtifactUrl(sessionId, source, kind, revision) {
  return artifactUrl(sessionId, `pdf-${source}-${kind}`, revision);
}

function svgDataUrl(svg) {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

function toolDetails(tool) {
  const data = tool.data || {};
  const inputs = tool.inputs && typeof tool.inputs === "object" ? tool.inputs : {};
  const inputNodes = Object.keys(inputs).length ? [
    h("details", { class: "live-tool-inputs" }, [
      h("summary", null, "Observable inputs"),
      h("pre", { class: "live-evidence-json" }, JSON.stringify(inputs, null, 2).slice(0, 12_000)),
    ]),
  ] : [];
  if (tool.name === "verify_smiles") {
    return [
      ...inputNodes,
      data.input ? h("code", { class: "live-evidence-smiles" }, data.input) : null,
      data.canonical_iso && data.canonical_iso !== data.input
        ? h("small", null, ["canonical: ", h("code", null, data.canonical_iso)]) : null,
      data.stereo_summary ? h("div", { class: "live-stereo-summary" }, [
        h("span", null, `tetrahedral ${data.stereo_summary.tetrahedral_assigned ?? 0}/${data.stereo_summary.tetrahedral_total ?? 0}`),
        h("span", null, `double-bond E/Z ${data.stereo_summary.double_bond_assigned ?? 0}/${data.stereo_summary.double_bond_total ?? 0}`),
      ]) : null,
      Array.isArray(data.stereo_warnings) && data.stereo_warnings.length
        ? h("ul", { class: "live-stereo-warnings" }, data.stereo_warnings.map((warning) => h("li", null, warning))) : null,
    ];
  }
  if (["reverse_rgroup", "fill_rgroup", "reverse_assemble"].includes(tool.name)) {
    const visible = Object.fromEntries(Object.entries(data)
      .filter(([key]) => !["svg", "indexed_svg"].includes(key))
      .slice(0, 24));
    return [...inputNodes, h("pre", { class: "live-evidence-json" }, JSON.stringify(visible, null, 2).slice(0, 12_000))];
  }
  if (tool.name === "assemble" && data.report) {
    return [...inputNodes, h("pre", { class: "live-evidence-report" }, String(data.report).slice(0, 8000))];
  }
  return inputNodes;
}

const LiveChemistryEvidence = {
  props: { sessionId: { type: String, required: true } },
  setup(props) {
  const state = reactive(createLiveEvidenceState(props.sessionId));
  const tab = ref("source");
  const images = reactive({ source: "", overview: "", ocsrGrid: "" });
  const pdfView = reactive({
    source: "main",
    page: 1,
    layouts: {},
    detections: {},
    naturalSizes: {},
    showLayout: true,
    showMolecules: true,
    showAnchors: true,
    candidateLoading: false,
    candidateLoadedVersion: 0,
    candidateRequestedVersion: 0,
    selectedReactionId: "",
  });
  const pdfImageLoadHandlers = new Map();
  function pdfImageLoadHandler(source, page) {
    const key = pdfKey(source, page);
    if (!pdfImageLoadHandlers.has(key)) {
      pdfImageLoadHandlers.set(key, (event) => {
        const image = event?.currentTarget || event?.target;
        const width = Number(image?.naturalWidth || 0);
        const height = Number(image?.naturalHeight || 0);
        if (width <= 0 || height <= 0) return;
        const current = pdfView.naturalSizes[key];
        if (current?.w === width && current?.h === height) return;
        pdfView.naturalSizes[key] = { w: width, h: height };
      });
    }
    return pdfImageLoadHandlers.get(key);
  }
  let disposed = false;
  const loadJsonArtifact = createVersionedArtifactLoader(async (url, { signal }) => {
    const response = await fetch(url, { headers: { Accept: "application/json" }, signal });
    if (!response.ok) throw new Error(`Artifact request failed: ${response.status}`);
    return response.json();
  });
  const loadImageArtifact = createVersionedArtifactLoader(async (url, { signal }) => {
    const response = await fetch(url, { cache: "no-store", signal });
    if (!response.ok) throw new Error(`Artifact request failed: ${response.status}`);
    const blob = await response.blob();
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(reader.error);
      reader.onload = () => resolve(String(reader.result || ""));
      reader.readAsDataURL(blob);
    });
  });
  const loadImage = async (slot, revision, url) => {
    try {
      const result = await loadImageArtifact.load(slot, revision, url);
      if (!disposed && result.current) images[slot] = result.value;
    } catch {
      // The event ledger remains useful if a transient preview fetch is unavailable.
    }
  };
  const isPdf = () => state.sources.some((source) => source.kind === "paper" || source.kind === "si");
  const pdfKey = (source, page) => `${source}:${page}`;
  const candidateArtifactHref = () => {
    const checkpoint = state.pdf.checkpoints.at(-1);
    if (checkpoint) {
      return artifactUrl(
        props.sessionId,
        `pdf-checkpoint-${checkpoint.sha256}`,
        checkpoint.seq,
      );
    }
    const revision = state.artifactRevisions.candidate || 0;
    return isPdf()
      ? pdfArtifactUrl(props.sessionId, "main", "candidate", revision)
      : artifactUrl(props.sessionId, "candidate", revision);
  };
  async function ensurePdfLayout(source, document) {
    if (document.status !== "ready" || pdfView.layouts[source]) return;
    const revision = document.layoutRevision || 0;
    try {
      const loaded = await loadJsonArtifact.load(
        `pdf:${source}:layout`, revision,
        pdfArtifactUrl(props.sessionId, source, "layout", revision),
      );
      if (!disposed && loaded.current && loaded.value) pdfView.layouts[source] = loaded.value;
    } catch {
      // A later evidence event or explicit page selection retries the artifact.
    }
  }
  async function ensurePdfCandidate() {
    const latestCheckpoint = state.pdf.checkpoints.at(-1);
    const candidateVersion = latestCheckpoint?.seq || state.artifactRevisions.candidate || 0;
    if (!state.artifacts.candidate || candidateVersion <= pdfView.candidateLoadedVersion
      || candidateVersion <= 0) return;
    pdfView.candidateLoading = true;
    pdfView.candidateRequestedVersion = Math.max(pdfView.candidateRequestedVersion, candidateVersion);
    const url = latestCheckpoint
      ? artifactUrl(
        props.sessionId,
        `pdf-checkpoint-${latestCheckpoint.sha256}`,
        latestCheckpoint.seq,
      )
      : pdfArtifactUrl(props.sessionId, "main", "candidate", candidateVersion);
    try {
      const loaded = await loadJsonArtifact.load("pdf:main:candidate", candidateVersion, url);
      if (!disposed && loaded.current && loaded.value
        && loaded.revision > pdfView.candidateLoadedVersion) {
        const write = [...state.tools].reverse().find((tool) => tool.name === "write_pdf_prediction");
        applyPdfCandidate(state, loaded.value, write?.data || {});
        pdfView.candidateLoadedVersion = loaded.revision;
      }
    } catch {
      // Keep the previous immutable checkpoint visible and retry explicitly.
    } finally {
      if (pdfView.candidateRequestedVersion === candidateVersion) pdfView.candidateLoading = false;
    }
  }
  async function ensurePdfMolparser(source, pageNumber, page) {
    const key = pdfKey(source, pageNumber);
    if (page?.detectionCount === null || page?.detectionCount === undefined
      || pdfView.detections[key]) return;
    const revision = page.molparserRevision || 0;
    try {
      const loaded = await loadJsonArtifact.load(
        `pdf:${source}:molparser:${pageNumber}`, revision,
        pdfArtifactUrl(props.sessionId, source, `molparser-${pageNumber}`, revision),
      );
      if (!disposed && loaded.current && Array.isArray(loaded.value)) {
        pdfView.detections[key] = loaded.value;
      }
    } catch {
      // A later evidence event or explicit page selection retries the artifact.
    }
  }
  function refreshPdfEvidence() {
    if (!isPdf()) return;
    const source = state.pdf.documents[pdfView.source] ? pdfView.source
      : state.pdf.documents.main ? "main" : "si";
    pdfView.source = source;
    const document = state.pdf.documents[source];
    if (!document) return;
    const pageNumbers = Object.keys(document.pages).map(Number).filter((page) => page > 0);
    if (!pageNumbers.includes(pdfView.page)) pdfView.page = pageNumbers[0] || 1;
    const pageNumber = pdfView.page;
    const page = document.pages[pageNumber];
    void ensurePdfLayout(source, document);
    void ensurePdfCandidate();
    void ensurePdfMolparser(source, pageNumber, page);
  }
  const onEvidence = (browserEvent) => {
    if (applyLiveEvidenceEvent(state, browserEvent.detail)) {
      if (state.source?.kind === "figure") void loadImage("source", 1, inputUrl(state.source.id));
      if (state.artifacts.overview) {
        const revision = state.artifactRevisions.overview || state.cursor;
        void loadImage("overview", revision, artifactUrl(props.sessionId, "overview", revision));
      }
      if (state.artifacts.ocsrGrid) {
        const revision = state.artifactRevisions.ocsrGrid || state.cursor;
        void loadImage("ocsrGrid", revision, artifactUrl(props.sessionId, "ocsr-grid", revision));
      }
      if (state.artifacts.overview && tab.value === "source" && !isPdf()) tab.value = "perception";
      if (state.source || state.tools.length) document.body.classList.add("runtime-has-live-evidence");
      void refreshPdfEvidence();
    }
  };
  onMounted(() => window.addEventListener(LIVE_CHEMISTRY_EVIDENCE_EVENT, onEvidence));
  onUnmounted(() => {
    disposed = true;
    pdfImageLoadHandlers.clear();
    loadJsonArtifact.dispose();
    loadImageArtifact.dispose();
    window.removeEventListener(LIVE_CHEMISTRY_EVIDENCE_EVENT, onEvidence);
  });

  const tabs = () => [
    ["source", isPdf() ? "PDF / SI" : "Source"],
    ["perception", isPdf() ? "Live evidence" : `MolParser${state.detections.length ? ` (${state.detections.length})` : ""}`],
    ["timeline", `Process timeline${state.activity.length ? ` (${state.activity.length})` : ""}`],
    ["reactions", `Provisional reactions${state.pdf.candidate?.reactions.length ? ` (${state.pdf.candidate.reactions.length})` : ""}`],
    ["tools", `Tools & FHR${state.tools.length ? ` (${state.tools.length})` : ""}`],
    ["candidate", "Candidate"],
  ].map(([id, label]) => h("button", {
    type: "button",
    class: tab.value === id ? "active" : "",
    disabled: id === "source" ? !state.source
      : id === "perception" ? (isPdf() ? Object.keys(state.pdf.documents).length === 0 : !state.artifacts.overview)
        : id === "timeline" ? state.activity.length === 0
          : id === "reactions" ? !state.pdf.candidate?.reactions.length
          : id === "tools" ? state.tools.length === 0
            : !state.artifacts.candidate,
    onClick: () => { tab.value = id; },
  }, label));

  const sourcePane = () => state.source
    ? h("div", { class: "live-evidence-source" }, [
        isPdf() ? h("div", { class: "live-pdf-source" }, [
          h("nav", { class: "live-document-tabs", "aria-label": "Source document" }, state.sources
            .filter((source) => source.kind === "paper" || source.kind === "si")
            .map((source) => h("button", {
              type: "button",
              class: pdfView.source === source.sourceDocument ? "active" : "",
              onClick: () => {
                pdfView.source = source.sourceDocument;
                pdfView.page = 1;
                void refreshPdfEvidence();
              },
            }, source.sourceDocument === "si" ? "Supporting information" : "Main paper"))),
          (() => {
            const document = state.pdf.documents[pdfView.source];
            const knownPages = Object.keys(document?.pages || {}).map(Number).sort((a, b) => a - b);
            const total = document?.pageCount || knownPages.at(-1) || 1;
            return h("div", { class: "live-page-controls" }, [
              h("button", { type: "button", disabled: pdfView.page <= 1, onClick: () => { pdfView.page -= 1; void refreshPdfEvidence(); } }, "Previous"),
              h("span", null, `Page ${pdfView.page} / ${total}`),
              h("button", { type: "button", disabled: pdfView.page >= total, onClick: () => { pdfView.page += 1; void refreshPdfEvidence(); } }, "Next"),
            ]);
          })(),
          (() => {
            const source = state.sources.find((item) => item.sourceDocument === pdfView.source)
              || state.sources.find((item) => item.kind === "paper" || item.kind === "si");
            const document = state.pdf.documents[pdfView.source];
            const page = document?.pages?.[pdfView.page];
            if (!source) return null;
            return h("div", { class: "live-pdf-source-page" }, [
              page?.completed ? h("img", {
                src: pdfArtifactUrl(props.sessionId, pdfView.source, `page-${pdfView.page}`, "rendered"),
                alt: `${source.sourceDocument === "si" ? "Supporting information" : "Main paper"} page ${pdfView.page}`,
              }) : h("div", { class: "live-evidence-waiting" }, `Rendering page ${pdfView.page} from immutable ${source.name}…`),
              h("a", { href: inputUrl(source.id), target: "_blank", rel: "noopener" }, "Open immutable PDF"),
            ]);
          })(),
        ]) : state.source.kind === "figure" && images.source
          ? h("img", { src: images.source, alt: state.source.name })
          : h("div", { class: "live-evidence-document" }, `Immutable ${state.source.kind}: ${state.source.name}`),
        (() => {
          const visibleSource = isPdf()
            ? state.sources.find((item) => item.sourceDocument === pdfView.source) || state.source
            : state.source;
          return h("footer", null, [
            h("b", null, visibleSource.name),
            h("code", null, visibleSource.sha256),
          ]);
        })(),
      ])
    : h("div", { class: "live-evidence-waiting" }, "Waiting for immutable input validation…");

  function pdfBoxes(source, page, size) {
    if (!size) return [];
    const boxes = [];
    const layout = pdfView.layouts[source];
    if (pdfView.showLayout && Array.isArray(layout?.regions)) {
      for (const region of layout.regions.filter((item) => Number(item.page) === page)) {
        const bbox = Array.isArray(region.bbox_pdf_norm) ? region.bbox_pdf_norm : [];
        if (bbox.length === 4) boxes.push({
          key: `layout:${region.region_id}`,
          bbox: [bbox[0] * size.w, bbox[1] * size.h, bbox[2] * size.w, bbox[3] * size.h],
          kind: "layout",
          label: region.label || region.region_id || "layout",
          fullLabel: `UniParser · ${region.category || region.kind || "region"}`,
          dash: "6 4",
        });
      }
    }
    if (pdfView.showAnchors) {
      for (const anchor of state.pdf.anchors.filter((item) => item.sourceDocument === source && item.pageNumber === page)) {
        const bbox = anchor.bboxPdfNorm || [];
        if (bbox.length === 4) boxes.push({
          key: anchor.id,
          bbox: [bbox[0] * size.w, bbox[1] * size.h, bbox[2] * size.w, bbox[3] * size.h],
          kind: "anchor",
          label: anchor.label,
          fullLabel: `Agent anchor · ${anchor.label}`,
        });
      }
      const pageGeometry = (pdfView.layouts[source]?.pages || []).find((item) => Number(item.page_no) === page);
      const widthPt = Number(pageGeometry?.width_pt || 0);
      const heightPt = Number(pageGeometry?.height_pt || 0);
      for (const anchor of (state.pdf.candidate?.anchors || [])
        .filter((item) => item.sourceDocument === source && item.pageNumber === page)) {
        const bbox = anchor.bboxPdf || [];
        if (bbox.length === 4 && widthPt > 0 && heightPt > 0) boxes.push({
          key: anchor.id,
          bbox: [bbox[0] * size.w / widthPt, bbox[1] * size.h / heightPt,
            bbox[2] * size.w / widthPt, bbox[3] * size.h / heightPt],
          kind: "anchor",
          stroke: "#cc3d8f",
          label: anchor.label,
          fullLabel: `Provisional reaction anchor · ${anchor.label}`,
        });
      }
    }
    if (pdfView.showMolecules) {
      const detections = pdfView.detections[pdfKey(source, page)] || [];
      const selected = state.pdf.candidate?.reactions.find((item) => item.id === pdfView.selectedReactionId);
      const selectedIndexes = new Map((selected?.molecules || []).filter((item) => item.bboxIndex !== null)
        .map((item) => [item.bboxIndex, item]));
      for (const item of detections) {
        if (!Array.isArray(item?.bbox) || item.bbox.length !== 4) continue;
        const linked = selectedIndexes.get(item.bbox_index);
        boxes.push({
          key: `mol:${source}:${page}:${item.bbox_index}`,
          bbox: item.bbox,
          kind: "default",
          stroke: linked ? "#cc3d8f" : item.markush ? "#e6a23c" : "#37c871",
          label: linked?.identifier || `M${item.bbox_index ?? "?"}`,
          fullLabel: linked
            ? `${linked.role} · ${linked.identifier || linked.esmiles || `MolParser ${item.bbox_index}`}`
            : `MolParser ${item.markush ? "Markush" : "structure"} · ${item.smi || "no SMILES"}`,
        });
      }
    }
    return boxes;
  }

  function focusReaction(reaction) {
    pdfView.source = reaction.sourceDocument;
    pdfView.page = reaction.pageNumber || 1;
    pdfView.selectedReactionId = reaction.id;
    tab.value = "perception";
    void refreshPdfEvidence();
  }

  const provisionalReactionsPane = () => {
    const candidate = state.pdf.candidate;
    if (!candidate) return h("div", { class: "live-evidence-waiting" }, "Waiting for a validated provisional prediction…");
    return h("div", { class: "live-provisional-reactions" }, [
      h("header", { class: "live-provisional-summary" }, [
        h("div", null, [h("b", null, "UNSEALED · provisional"), h("span", null, `${candidate.reactions.length} reaction occurrences`)]),
        h("span", { class: candidate.valid ? "status-complete" : "status-running" }, candidate.valid ? "schema VALID" : "validation pending"),
      ]),
      state.pdf.checkpoints.length ? h("nav", { class: "live-checkpoint-history", "aria-label": "Immutable extraction checkpoints" }, [
        h("b", null, `${state.pdf.checkpoints.length} immutable checkpoint${state.pdf.checkpoints.length === 1 ? "" : "s"}`),
        ...state.pdf.checkpoints.map((checkpoint, index) => h("a", {
          href: artifactUrl(props.sessionId, `pdf-checkpoint-${checkpoint.sha256}`, checkpoint.seq),
          target: "_blank",
          rel: "noopener",
          title: checkpoint.sha256,
        }, `v${index + 1} · ${checkpoint.sha256.slice(0, 8)}`)),
      ]) : null,
      ...candidate.reactions.map((reaction) => h("article", {
        class: `live-reaction-card${pdfView.selectedReactionId === reaction.id ? " is-selected" : ""}`,
      }, [
        h("header", null, [
          h("div", null, [h("b", null, reaction.instanceId), h("span", null, reaction.anchorLabel)]),
          h("button", { type: "button", onClick: () => focusReaction(reaction) }, `${reaction.sourceDocument.toUpperCase()} · page ${reaction.pageNumber || "?"}`),
        ]),
        h("div", { class: "live-reaction-equation" }, [
          h("div", null, reaction.molecules.filter((item) => item.role === "reactant").map((item) =>
            h("code", { title: item.esmiles }, item.identifier || item.esmiles || "unresolved reactant"))),
          h("span", { "aria-hidden": "true" }, "→"),
          h("div", null, reaction.molecules.filter((item) => item.role === "product").map((item) =>
            h("code", { title: item.esmiles }, item.identifier || item.esmiles || "unresolved product"))),
        ]),
        reaction.conditions.length ? h("dl", { class: "live-condition-list" }, reaction.conditions.flatMap((item) => [
          h("dt", null, item.role || `step ${item.stepIndex + 1}`),
          h("dd", null, item.text || "recorded without text"),
        ])) : h("p", { class: "live-unresolved" }, "Conditions unresolved or not yet recorded"),
        reaction.provenanceQuotes.length ? h("details", null, [
          h("summary", null, `${reaction.provenanceQuotes.length} source quote${reaction.provenanceQuotes.length === 1 ? "" : "s"}`),
          ...reaction.provenanceQuotes.map((quote) => h("blockquote", null, quote)),
        ]) : h("p", { class: "live-unresolved" }, "Source quote not yet linked"),
      ])),
    ]);
  };

  const pdfEvidencePane = () => {
    const source = pdfView.source;
    const document = state.pdf.documents[source];
    if (!document) return h("div", { class: "live-evidence-waiting" }, "Waiting for document perception…");
    const pages = Object.keys(document.pages).map(Number).sort((a, b) => a - b);
    const page = pages.includes(pdfView.page) ? pdfView.page : pages[0] || 1;
    const layoutPage = (pdfView.layouts[source]?.pages || []).find((item) => Number(item.page_no) === page);
    const natural = pdfView.naturalSizes[pdfKey(source, page)];
    const size = layoutPage ? { w: Number(layoutPage.w_px), h: Number(layoutPage.h_px) } : natural;
    const recognized = Object.values(document.pages).filter((item) => item.detectionCount !== null).length;
    const detectionTotal = Object.values(document.pages).reduce((sum, item) => sum + Number(item.detectionCount || 0), 0);
    const currentDetections = pdfView.detections[pdfKey(source, page)] || [];
    return h("div", { class: "live-pdf-evidence" }, [
      h("div", { class: "live-pdf-summary" }, [
        h("b", null, source === "si" ? "Supporting information" : "Main paper"),
        h("span", null, `${recognized}/${document.pageCount || pages.length || "?"} pages recognized`),
        h("span", null, `${detectionTotal} structures`),
        h("span", null, `${document.anchorCount || state.pdf.anchors.filter((item) => item.sourceDocument === source).length} regions`),
        h("span", null, document.segmenter === "uniparser" ? "UniParser layout" : document.segmenter),
        h("span", null, `stage: ${state.pdf.stage.replaceAll("_", " ")}`),
        state.pdf.validation.status !== "pending"
          ? h("span", { class: state.pdf.validation.valid === true ? "status-complete" : "status-running" },
            state.pdf.validation.status === "running" ? "validating prediction"
              : state.pdf.validation.valid === true ? "validation passed" : "validation unresolved")
          : null,
      ]),
      h("div", { class: "live-layer-controls" }, [
        ["showLayout", "UniParser"], ["showMolecules", "MolParser"], ["showAnchors", "Agent anchors"],
      ].map(([key, label]) => h("button", {
        type: "button", class: pdfView[key] ? "active" : "", onClick: () => { pdfView[key] = !pdfView[key]; },
      }, label))),
      h("nav", { class: "live-raw-artifact-links", "aria-label": "Raw perception artifacts" }, [
        h("a", { href: pdfArtifactUrl(props.sessionId, source, "layout", document.layoutRevision || 0), target: "_blank", rel: "noopener" }, "Layout JSON"),
        document.segmenter === "uniparser"
          ? h("a", { href: pdfArtifactUrl(props.sessionId, source, "uniparser", document.layoutRevision || 0), target: "_blank", rel: "noopener" }, "Raw UniParser") : null,
        document.pages[page]?.detectionCount !== null && document.pages[page]?.detectionCount !== undefined
          ? h("a", { href: pdfArtifactUrl(props.sessionId, source, `molparser-${page}`, document.pages[page]?.molparserRevision || 0), target: "_blank", rel: "noopener" }, `Raw MolParser · page ${page}`) : null,
        state.artifacts.candidate
          ? h("a", { href: candidateArtifactHref(), target: "_blank", rel: "noopener" }, "Current provisional JSON") : null,
      ]),
      h("div", { class: "live-page-controls" }, [
        h("button", { type: "button", disabled: page <= 1, onClick: () => { pdfView.page -= 1; void refreshPdfEvidence(); } }, "Previous"),
        h("span", null, `Page ${page} / ${document.pageCount || pages.at(-1) || "?"}`),
        h("button", {
          type: "button",
          disabled: document.pageCount ? page >= document.pageCount : !pages.some((item) => item > page),
          onClick: () => { pdfView.page += 1; void refreshPdfEvidence(); },
        }, "Next"),
      ]),
      document.pages[page]?.completed ? h(BBoxImagePanel, {
        imageUrl: pdfArtifactUrl(props.sessionId, source, `page-${page}`, "rendered"),
        coordinateSize: size,
        boxes: pdfBoxes(source, page, size),
        alt: `${source} PDF page ${page}`,
        onImageLoad: pdfImageLoadHandler(source, page),
      }) : h("div", { class: "live-evidence-waiting" }, `Rendering ${source} page ${page}…`),
      currentDetections.length ? h("details", { class: "live-raw-detections" }, [
        h("summary", null, `${currentDetections.length} raw MolParser records on page ${page}`),
        h("div", { class: "live-detection-table" }, currentDetections.slice(0, 250).map((item) =>
          h("article", { class: item.markush ? "is-markush" : "is-concrete" }, [
            h("b", null, `bbox ${item.bbox_index ?? "?"}`),
            h("span", null, item.markush ? "Markush" : "concrete"),
            Number.isFinite(item.score) ? h("small", null, `score ${Number(item.score).toFixed(3)}`) : null,
            h("code", null, item.esmi || item.smi || item.caption || "No structure string"),
            item.md5 ? h("small", { title: String(item.md5) }, `md5 ${String(item.md5).slice(0, 12)}…`) : null,
            Array.isArray(item.bbox) ? h("small", null, `bbox [${item.bbox.join(", ")}]`) : null,
          ]))),
      ]) : null,
    ]);
  };

  const perceptionPane = () => isPdf() ? pdfEvidencePane() : h("div", { class: "live-evidence-perception" }, [
    h("div", { class: "live-evidence-figures" }, [
      state.artifacts.overview && images.overview ? h("figure", null, [
        h("figcaption", null, "MolParser overview"),
        h("img", { src: images.overview, alt: "Detected molecule bounding boxes" }),
      ]) : null,
      state.artifacts.ocsrGrid && images.ocsrGrid ? h("figure", null, [
        h("figcaption", null, "OCSR structures"),
        h("img", { src: images.ocsrGrid, alt: "OCSR comparison grid" }),
      ]) : null,
    ]),
    state.detections.length ? h("div", { class: "live-detection-table" }, state.detections.map((item) =>
      h("article", { class: item.markush ? "is-markush" : "is-concrete" }, [
        h("b", null, `bbox ${item.bbox_index ?? "?"}`),
        h("span", null, item.markush ? "Markush" : "concrete"),
        item.score !== null ? h("small", null, `score ${item.score.toFixed(3)}`) : null,
        h("code", null, item.smi || item.caption || "No structure string"),
        item.md5 ? h("small", { title: item.md5 }, `raw md5 ${item.md5.slice(0, 12)}…`) : null,
      ]))) : null,
  ]);

  function focusActivity(item) {
    if (!item.sourceDocument || !item.pageNumber) return;
    pdfView.source = item.sourceDocument;
    pdfView.page = item.pageNumber;
    tab.value = "perception";
    void refreshPdfEvidence();
  }

  const closurePanel = () => isPdf() ? null : h("section", {
    class: `live-closure-panel state-${state.closure.state}`,
  }, [
    h("header", null, [
      h("div", null, [h("b", null, "Verification → assembly closure"), h("small", null, " deterministic seal gate")]),
      h("span", null, state.closure.state.replaceAll("_", " ")),
    ]),
    h("div", { class: "live-closure-stats" }, [
      h("span", null, `candidate revision v${state.closure.assemblyRevision}`),
      h("span", null, `assemble seq ${state.closure.lastAssembleSeq ?? "—"}`),
      h("span", null, `render seq ${state.closure.lastRenderSeq ?? "—"}`),
    ]),
    state.closure.issues.length ? h("ul", null, state.closure.issues.map((issue) => h("li", null, issue))) :
      h("p", null, "No newer verification is pending and the latest candidate was rendered after assembly."),
    state.closure.revisions.length ? h("div", { class: "live-revision-strip" },
      state.closure.revisions.map((item) => h("span", { title: item.sha256 || "candidate hash unavailable" },
        `v${item.revision} · seq ${item.seq}${item.sha256 ? ` · ${item.sha256.slice(0, 10)}…` : ""}`))) : null,
    state.closure.candidateDiffs.length ? h("div", { class: "live-candidate-diffs" }, [
      h("b", null, `${state.closure.candidateDiffs.length} molecule/lineage change(s) in latest reassembly`),
      ...state.closure.candidateDiffs.map((item) => h("article", null, [
        h("header", null, item.key),
        h("div", null, [
          h("small", null, "BEFORE"),
          h("code", null, item.before?.esmiles || "not present"),
          h("span", null, `bbox ${item.before?.bboxIndex ?? "—"} · md5 ${item.before?.molparserMd5?.slice(0, 10) || "—"}`),
        ]),
        h("div", null, [
          h("small", null, "AFTER"),
          h("code", null, item.after?.esmiles || "removed"),
          h("span", null, `bbox ${item.after?.bboxIndex ?? "—"} · md5 ${item.after?.molparserMd5?.slice(0, 10) || "—"}`),
        ]),
      ])),
    ]) : null,
    state.closure.postAssemblyVerifications.length ? h("div", { class: "live-closure-diffs" },
      state.closure.postAssemblyVerifications.map((item) => h("article", null, [
        h("b", null, `post-assembly verify · seq ${item.seq}`),
        h("code", null, item.smiles || "SMILES unavailable"),
        item.canonical && item.canonical !== item.smiles
          ? h("small", null, ["normalized → ", h("code", null, item.canonical)]) : null,
      ]))) : null,
  ]);

  const timelinePane = () => h("div", null, [
    closurePanel(),
    h("ol", { class: "live-process-timeline" }, state.activity.map((item) =>
      h("li", { class: `kind-${item.kind} status-${item.status}` }, [
        h("span", { class: "live-timeline-seq" }, String(item.seq).padStart(3, "0")),
        h("div", null, [
          h("b", null, item.label),
          item.sourceDocument ? h("small", null, `${item.sourceDocument.toUpperCase()}${item.pageNumber ? ` · page ${item.pageNumber}` : ""}`) : null,
        ]),
        item.sourceDocument && item.pageNumber
          ? h("button", { type: "button", onClick: () => focusActivity(item) }, "Open evidence") : null,
      ]))),
  ]);

  function checkpointForTool(tool) {
    const index = state.pdf.checkpoints.findIndex((item) => item.seq >= tool.seq);
    return index >= 0 ? { index: index + 1, checkpoint: state.pdf.checkpoints[index] } : null;
  }

  const toolsPane = () => {
    const fhr = summarizeFhrCoverage(state.tools);
    return h("div", { class: "live-tools-workspace" }, [
      closurePanel(),
      h("aside", { class: `live-fhr-coverage${fhr.observed ? " is-used" : " is-missing"}` }, [
        h("div", null, [
          h("b", null, "FHR / R-group assembly"),
          h("span", null, fhr.observed
            ? `${fhr.complete}/${fhr.count} operations complete${fhr.failed ? ` · ${fhr.failed} failed` : ""}`
            : "not observed in this extraction"),
        ]),
        h("p", null, fhr.observed
          ? `Observed: ${fhr.names.join(", ")}. Open each card to inspect exact template, concrete structure, R-map, and deterministic output.`
          : "This is not proof that R-group handling was unnecessary. If the source contains templates, Markush structures, or enumerated analogues, request an FHR verification before accepting the result."),
      ]),
      h("div", { class: "live-tool-ledger" }, state.tools.map((tool) => {
        const checkpoint = checkpointForTool(tool);
        return h("article", { class: `live-tool-card status-${tool.status}` }, [
          h("header", null, [
            h("b", null, tool.name),
            h("span", null, tool.status),
          ]),
          h("p", null, tool.summary),
          checkpoint ? h("a", {
            class: "live-tool-checkpoint",
            href: artifactUrl(props.sessionId, `pdf-checkpoint-${checkpoint.checkpoint.sha256}`, checkpoint.checkpoint.seq),
            target: "_blank", rel: "noopener",
          }, `feeds checkpoint v${checkpoint.index}`) : null,
          tool.svg ? h("img", { src: svgDataUrl(tool.svg), alt: `${tool.name} structure render` }) : null,
          ...toolDetails(tool),
        ]);
      })),
    ]);
  };

  const candidatePane = () => h("div", { class: "live-candidate-pane" }, [
    closurePanel(),
    h("div", { class: "live-unsealed-warning" }, [
      h("b", null, "UNSEALED CANDIDATE"),
      " · read-only · may change after source verification",
    ]),
    h("p", null, "A deterministic tool has written a candidate. It is not a reviewed or final result."),
    h("a", {
      href: candidateArtifactHref(),
      target: "_blank",
      rel: "noopener",
    }, "Inspect candidate V2 JSON"),
    h("div", { class: "live-tool-ledger" }, state.tools
      .filter((tool) => tool.name === "assemble" || tool.name === "render_reactions")
      .map((tool) => h("article", { class: `live-tool-card status-${tool.status}` }, [
        h("header", null, [h("b", null, tool.name), h("span", null, tool.status)]),
        h("p", null, tool.summary),
        ...toolDetails(tool),
      ]))),
  ]);

  return () => h("section", { class: "live-chemistry-evidence", "aria-live": "polite" }, [
    h("header", { class: "live-evidence-header" }, [
      h("div", null, [h("h2", null, "Live chemistry evidence"), h("p", null, "Persisted tool evidence · read-only · no GT")]),
      h("div", { class: "live-evidence-header-actions" }, [
        ["review_ready", "partial", "needs_attention"].includes(state.status) && typeof window.showExtractionProcess === "function"
          ? h("button", { type: "button", onClick: () => window.showExtractionProcess(false) }, "Return to sealed review")
          : null,
        h("span", { class: `live-evidence-status status-${state.status}` }, state.status.replaceAll("_", " ")),
      ]),
    ]),
    h("nav", { class: "live-evidence-tabs", "aria-label": "Live chemistry evidence" }, tabs()),
    h("div", { class: "live-evidence-body" }, [
      h("div", { style: { display: tab.value === "source" ? "" : "none" }, "aria-hidden": tab.value !== "source" }, [sourcePane()]),
      h("div", { style: { display: tab.value === "perception" ? "" : "none" }, "aria-hidden": tab.value !== "perception" }, [perceptionPane()]),
      h("div", { style: { display: tab.value === "timeline" ? "" : "none" }, "aria-hidden": tab.value !== "timeline" }, [timelinePane()]),
      h("div", { style: { display: tab.value === "reactions" ? "" : "none" }, "aria-hidden": tab.value !== "reactions" }, [provisionalReactionsPane()]),
      h("div", { style: { display: tab.value === "tools" ? "" : "none" }, "aria-hidden": tab.value !== "tools" }, [toolsPane()]),
      h("div", { style: { display: tab.value === "candidate" ? "" : "none" }, "aria-hidden": tab.value !== "candidate" }, [candidatePane()]),
    ]),
  ]);
  },
};

export function mountLiveChemistryEvidence(element, props) {
  if (element.__chemrxnLiveEvidenceApp && element.__chemrxnLiveEvidenceSessionId === props.sessionId) {
    return element.__chemrxnLiveEvidenceApp;
  }
  if (element.__chemrxnLiveEvidenceApp) element.__chemrxnLiveEvidenceApp.unmount();
  const app = createApp({ render: () => h(LiveChemistryEvidence, props) });
  app.mount(element);
  element.__chemrxnLiveEvidenceApp = app;
  element.__chemrxnLiveEvidenceSessionId = props.sessionId;
  return app;
}
