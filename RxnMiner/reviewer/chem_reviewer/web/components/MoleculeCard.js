// One molecule card (a structure picture from RDKit + identifier + actions).
// `data`  : MoleculeBlock { identifier, "E-SMILES", description, _source?, bbox?, bbox_index? }
// `path`  : path to this block inside store.taskData (e.g. ["reaction_templates",0,"reactants",1])
import { h, ref, watch, onMounted, onBeforeUnmount, computed } from "vue";
import {
  canonicalSmiles,
  cleanSmilesForRender,
  renderSmilesToSvg,
  sameConnectivity,
} from "/static/lib/rdkit-loader.js";
import { store, setAtPath, showToast } from "/static/lib/store.js";
import { hasEsmilesExtension } from "/static/lib/esmiles-ketcher.js";
import { Editable } from "/static/components/Editable.js";
const SOURCE_TYPE_CFG = {
  image_structure: null, // default — no badge clutter for the common case
  image_ocr: { label: "OCR", color: "info", title: "Text recognized in the figure" },
  identifier_pointer: {
    label: "↪ Internal reference",
    color: "info",
    title: "Compound with the same identifier in another reaction in this task",
  },
  cross_template_pointer: {
    label: "↪ Step reference",
    color: "purple",
    title: "Product from a preceding step used as a reactant",
  },
  cross_figure_identifier_pointer: {
    label: "↪ Figure reference",
    color: "yellow",
    title: "Compound defined in another figure",
  },
  identifier_pointer_cross_fig: {
    label: "↪ Figure reference",
    color: "yellow",
    title: "Compound defined in another figure",
  },
  intended_but_not_formed: {
    label: "⚠ Not formed",
    color: "red",
    title: "Intended product shown in the figure but not formed",
  },
  named_reagent: {
    label: "Reagent lookup",
    color: "muted",
    title: "Standard reagent structure obtained by name lookup",
  },
  implicit_from_markush: {
    label: "Markush expansion",
    color: "green",
    title: "Derived from a template by Markush substitution",
  },
  inferred_chemistry: {
    label: "Chemical inference",
    color: "muted",
    title: "Inferred from chemistry, such as a byproduct",
  },
  text_inference: {
    label: "Text inference",
    color: "muted",
    title: "Inferred from article text rather than a figure label",
  },
  human_corrected: {
    label: "Human correction",
    color: "green",
    title: "Manually corrected during review",
  },
  cross_ref: {
    label: "Cross-reference",
    color: "info",
    title: "Resolved through a cross-reference",
  },
  not_reported: {
    label: "Not reported",
    color: "muted",
    title: "Not explicitly reported in the source",
  },
  paper_text_quote: {
    label: "Source quotation",
    color: "info",
    title: "Direct quotation from the article",
  },
};

export const MoleculeCard = {
  props: {
    data: { type: Object, required: true },
    path: { type: Array, default: () => [] },
    readOnly: { type: Boolean, default: false },
    compact: { type: Boolean, default: false },
    strictDerivation: { type: Boolean, default: false },
    rawDetection: { type: Object, default: null },
    comparisonData: { type: Object, default: null },
    provenanceLabel: { type: String, default: "" },
    provenanceTitle: { type: String, default: "" },
    renderSrc: { type: String, default: "" },
    deferChemCompare: { type: Boolean, default: false },
    lazyRender: { type: Boolean, default: true },
    allowKetcher: { type: Boolean, default: false },
    editTarget: { type: Object, default: null },
  },
  emits: ["remove", "review-esmiles"],
  setup(props, { emit }) {
    const cardElement = ref(null);
    const svg = ref(null);
    const renderState = ref("loading"); // "deferred" | "loading" | "ok" | "empty" | "error"
    const showSmiles = ref(false);
    const showMolparser = ref(false);
    const molparserSvg = ref(null);
    const molparserRenderState = ref("idle");
    const molparserCompare = ref("none");
    const showGt = ref(true);
    const gtSvg = ref(null);
    const gtRenderState = ref("idle");
    const gtCompare = ref("none");
    let renderActivated = false;
    let visibilityObserver = null;

    const molparserDetection = computed(() => {
      if (props.rawDetection?.raw_available !== false) {
        if (props.rawDetection) return props.rawDetection;
      }
      const src = props.data?._source || {};
      const idx = src.bbox_index ?? props.data?.bbox_index;
      const box = src.bbox ?? props.data?.bbox;
      return (store.molparserDetections || []).find((det) => {
        if (!det?.raw_available) return false;
        if (idx !== undefined && idx !== null && det.bbox_index === idx) return true;
        if (!Array.isArray(box) || !Array.isArray(det.bbox)) return false;
        return box.slice(0, 4).every((value, i) => Number(value) === Number(det.bbox[i]));
      }) || null;
    });

    const comparisonMolecule = computed(() => {
      if (props.comparisonData) return props.comparisonData;
      const comparison = store.comparisonTaskData;
      const [collection, rowIndex, side, moleculeIndex] = props.path || [];
      if (!comparison || !["selected_examples", "reaction_templates"].includes(collection)) {
        return null;
      }
      const reviewCfg = store.taskData?._reviewer || {};
      const allowedSides = reviewCfg.comparison_sides;
      if (Array.isArray(allowedSides) && !allowedSides.includes(side)) return null;

      const sourceRow = store.taskData?.[collection]?.[rowIndex];
      const candidates = comparison?.[collection] || [];
      const rowKey = collection === "selected_examples" ? "instance_id" : "template_id";
      const comparisonRow = candidates.find(
        (row) => sourceRow?.[rowKey] && row?.[rowKey] === sourceRow[rowKey]
      ) || candidates[rowIndex];
      const molecules = comparisonRow?.[side] || [];
      const ours = props.data?.identifier;
      return molecules.find((molecule) => ours && molecule?.identifier === ours)
        || molecules[moleculeIndex]
        || null;
    });

    async function refreshSvg() {
      if (props.renderSrc) {
        svg.value = null;
        renderState.value = "ok";
        return;
      }
      const esmi = props.data?.["E-SMILES"] || "";
      if (!esmi.trim()) {
        svg.value = null;
        renderState.value = "empty";
        return;
      }
      renderState.value = "loading";
      const result = await renderSmilesToSvg(esmi, {
        width: 220,
        height: 160,
      });
      if (result) {
        svg.value = result;
        renderState.value = "ok";
      } else {
        svg.value = null;
        renderState.value = "error";
      }
    }

    async function refreshMolparserComparison() {
      const det = molparserDetection.value;
      const ours = props.data?.["E-SMILES"] || "";
      if (!det?.raw_available || !det.smi) {
        molparserCompare.value = "none";
        return;
      }
      const cleanOurs = cleanSmilesForRender(ours);
      const cleanRaw = cleanSmilesForRender(det.smi);
      if (cleanOurs === cleanRaw) {
        molparserCompare.value = "exact";
        return;
      }
      const [canonOurs, canonRaw] = await Promise.all([
        canonicalSmiles(cleanOurs),
        canonicalSmiles(cleanRaw),
      ]);
      molparserCompare.value = canonOurs && canonRaw && canonOurs === canonRaw
        ? "equivalent"
        : "changed";
    }

    async function refreshGtComparison() {
      const gt = comparisonMolecule.value;
      const ours = props.data?.["E-SMILES"] || "";
      const gtSmiles = gt?.["E-SMILES"] || gt?.smiles || "";
      if (!gtSmiles) {
        gtCompare.value = "none";
        gtSvg.value = null;
        gtRenderState.value = "idle";
        return;
      }
      gtRenderState.value = "loading";
      const [rendered, canonOurs, canonGt, connectivitySame] = await Promise.all([
        renderSmilesToSvg(gtSmiles, { width: 220, height: 145 }),
        canonicalSmiles(ours),
        canonicalSmiles(gtSmiles),
        sameConnectivity(ours, gtSmiles),
      ]);
      gtSvg.value = rendered;
      gtRenderState.value = rendered ? "ok" : "error";
      gtCompare.value = canonOurs && canonGt && canonOurs === canonGt
        ? "same"
        : connectivitySame
          ? "stereo"
          : "different";
    }

    async function toggleMolparser() {
      showMolparser.value = !showMolparser.value;
      if (!showMolparser.value || molparserRenderState.value === "ok") return;
      const raw = molparserDetection.value?.smi || "";
      molparserRenderState.value = "loading";
      molparserSvg.value = await renderSmilesToSvg(raw, { width: 220, height: 145 });
      molparserRenderState.value = molparserSvg.value ? "ok" : "error";
    }

    function activateRendering() {
      if (renderActivated) return;
      renderActivated = true;
      visibilityObserver?.disconnect();
      visibilityObserver = null;
      refreshSvg();
      if (!props.deferChemCompare) {
        refreshMolparserComparison();
        refreshGtComparison();
      }
    }

    onMounted(() => {
      const canDefer = props.lazyRender
        && !props.renderSrc
        && typeof IntersectionObserver === "function"
        && cardElement.value;
      if (!canDefer) {
        activateRendering();
        return;
      }
      renderState.value = "deferred";
      const scrollRoot = cardElement.value.closest(".task-body");
      visibilityObserver = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) activateRendering();
      }, {
        // Prepare the next reaction row without rendering the full document at once.
        root: scrollRoot,
        rootMargin: "320px 0px",
      });
      visibilityObserver.observe(cardElement.value);
    });
    onBeforeUnmount(() => visibilityObserver?.disconnect());
    watch(
      () => props.data?.["E-SMILES"],
      () => {
        if (!renderActivated) return;
        refreshSvg();
        if (!props.deferChemCompare) {
          refreshMolparserComparison();
          refreshGtComparison();
        }
      },
    );
    watch(
      () => comparisonMolecule.value?.["E-SMILES"] || comparisonMolecule.value?.smiles,
      () => {
        if (renderActivated) refreshGtComparison();
      },
    );
    watch(
      () => molparserDetection.value?.smi,
      () => {
        molparserSvg.value = null;
        molparserRenderState.value = "idle";
        if (renderActivated) refreshMolparserComparison();
      },
    );

    const openKetcher = () => {
      if (props.readOnly && !props.allowKetcher) {
        showToast("error", "This reference structure is read-only");
        return;
      }
      const occurrence = props.editTarget;
      store.ketcherTarget = {
        mode: "edit",
        ...(occurrence ? {} : { path: [...props.path, "E-SMILES"] }),
        smiles: props.data?.["E-SMILES"] || "",
        identifier: props.data?.identifier || "",
        molecularContext: props.data?.description || "",
        occurrence,
        ...(occurrence ? {
          onCommit: (esmiles, target) => emit("review-esmiles", {
            esmiles,
            occurrence: target,
          }),
        } : {}),
      };
    };

    const copySmiles = async () => {
      const s = props.data?.["E-SMILES"] || "";
      if (!s) return;
      try {
        await navigator.clipboard.writeText(s);
      } catch (e) {
        // ignore
      }
    };

    const onSaveIdent = (v) => {
      setAtPath([...props.path, "identifier"], v);
    };
    const onSaveDesc = (v) => {
      setAtPath([...props.path, "description"], v);
    };

    return () => {
      const fullEsmiles = props.data?.["E-SMILES"] || "";
      const isEmpty = !fullEsmiles.trim();
      const partialPreview = hasEsmilesExtension(fullEsmiles);
      const src = props.data?._source || {};
      const srcType = src.type;
      const srcCfg = srcType ? SOURCE_TYPE_CFG[srcType] : null;
      const isIntendedButNotFormed = srcType === "intended_but_not_formed";
      const isPointer =
        srcType &&
        (srcType.endsWith("_pointer") ||
          srcType === "identifier_pointer_cross_fig");

      // Source badge (small chip below identifier)
      const sourceBadge = srcCfg
        ? h(
            "span",
            {
              class: `mol-source-badge mol-source-${srcCfg.color}`,
              title: `${srcCfg.title}\nSource type: ${srcType}` +
                (src.points_to ? `\n→ ${src.points_to}` : "") +
                (src.from_template ? `\nfrom_template: ${src.from_template}` : "") +
                (src.source_figure ? `\nsource_figure: ${src.source_figure}` : ""),
            },
            srcCfg.label
          )
        : null;

      // _source.model_review badge — MolParser/extractor flagged this caption
      // as suspicious. HANDOFF.md §3.2 lists this as part of _source. Real
      // data has 9 molecules with this field across paper05/07/09. The
      // chemist MUST see this warning.
      const mr = src.model_review || null;
      const mrConfColor = {
        high: "red",
        medium: "yellow",
        low: "muted",
      }[(mr && mr.confidence) || "medium"] || "yellow";
      const modelReviewBadge = mr
        ? h(
            "span",
            {
              class: `mol-source-badge mol-model-review mol-source-${mrConfColor}`,
              title:
                `⚠ The recognizer or extractor flagged a possible recognition error.\n\n` +
                `reviewer: ${mr.reviewer || "?"}\n` +
                `confidence: ${mr.confidence || "?"}\n` +
                `concern: ${mr.concern || "(none)"}` +
                (mr.suggest ? `\n\nSuggested replacement: ${mr.suggest}` : ""),
            },
            "⚠ Needs review"
          )
        : null;

      const mp = molparserDetection.value;
      const mpCfg = {
        exact: {
          label: "MP original",
          className: "mp-same",
          title: "E-SMILES is identical to the original recognition wire string",
        },
        equivalent: {
          label: "MP equivalent",
          className: "mp-equivalent",
          title: "Different wire strings with identical canonical isomeric SMILES",
        },
        changed: {
          label: "MP modified",
          className: "mp-changed",
          title: "Structure differs from the original recognition; it may have been changed by the model or a deterministic tool",
        },
        none: {
          label: "MP checking",
          className: "mp-pending",
          title: "Comparing original recognition results",
        },
      }[molparserCompare.value];
      const molparserBadge = mp && !props.deferChemCompare
        ? h(
            "button",
            {
              class: `molparser-badge ${mpCfg.className}`,
              title: `${mpCfg.title}\nInspect the original wire string, caption, and rendered structure`,
              onClick: toggleMolparser,
            },
            mpCfg.label
          )
        : null;

      const derivation = src.derivation || {};
      let derivationMethod = derivation.method || "";
      if (!props.strictDerivation && !derivationMethod && props.path?.[2] === "products" && mp) {
        if (["exact", "equivalent"].includes(molparserCompare.value) && !mp.markush) {
          derivationMethod = "molparser_direct";
        } else if (mp.markush && molparserCompare.value === "changed") {
          derivationMethod = "markush_materialized_untracked";
        }
      }
      if (!props.strictDerivation && !derivationMethod && props.path?.[2] === "reactants" && src._bbox_inherited) {
        derivationMethod = "reactant_derived_untracked";
      }
      const derivationCfg = {
        molparser_direct: ["MP direct", "prov-direct", "Directly recognized concrete structure"],
        rgroup_fill: ["R-group fill", "prov-tool", "Product materialized by R-group substitution"],
        image_reasoning: ["Pi visual reasoning", "prov-model", "Inferred from figure text, positions, or variables"],
        reverse_assemble: ["Reverse assembly", "prov-tool", "Reactants derived from the product and template by reverse assembly"],
        reverse_assemble_repaired: ["Reverse + repair", "prov-tool", "Reverse assembly followed by deterministic bridge or ring repair"],
        model_inference: ["Model reasoning", "prov-model", "Derived through model reasoning"],
        markush_materialized_untracked: ["Materialized template", "prov-unknown", "Expanded from a Markush template; the historical record does not identify the method"],
        reactant_derived_untracked: ["Untracked derivation", "prov-unknown", "Derived reactant; the historical record does not distinguish tool from model reasoning"],
      }[derivationMethod];
      const derivationBadge = derivationCfg
        ? h(
            "span",
            {
              class: `derivation-badge ${derivationCfg[1]}`,
              title: derivation.note
                ? `${derivationCfg[2]}\n${derivation.note}`
                : derivationCfg[2],
            },
            derivationCfg[0]
          )
        : null;

      const gt = comparisonMolecule.value;
      const gtSmiles = gt?.["E-SMILES"] || gt?.smiles || "";
      const gtPanel = gt && !props.deferChemCompare
        ? h("div", { class: "gt-compare-panel" }, [
            h("div", { class: "gt-compare-head" }, [
              h("b", null, `${store.comparisonLabel || "GT"} comparison`),
              h(
                "span",
                {
                  class: `gt-compare-status ${
                    gtCompare.value === "same"
                      ? "gt-same"
                      : gtCompare.value === "stereo"
                        ? "gt-stereo"
                        : "gt-different"
                  }`,
                },
                gtCompare.value === "same"
                  ? "Exact match"
                  : gtCompare.value === "stereo"
                    ? "Same connectivity / different stereochemistry"
                    : "Different connectivity"
              ),
              h(
                "button",
                {
                  class: "gt-toggle",
                  onClick: () => (showGt.value = !showGt.value),
                },
                showGt.value ? "Collapse" : "Expand"
              ),
            ]),
            showGt.value
              ? h("div", { class: "gt-compare-body" }, [
                  h("div", { class: "gt-compare-render" }, [
                    gtRenderState.value === "loading"
                      ? h("div", { class: "render-error" }, "GT Rendering structure…")
                      : gtRenderState.value === "ok" && gtSvg.value
                        ? h("div", { innerHTML: gtSvg.value })
                        : h("div", { class: "render-error" }, "RDKit cannot render GT"),
                  ]),
                  h("code", { class: "gt-smiles" }, gtSmiles || "(empty)"),
                ])
              : null,
          ])
        : null;

      if (isEmpty && !props.data?.description) {
        return h("div", { class: "mol-card placeholder" }, [
          props.readOnly ? null : h("div", { class: "mol-actions" }, [
            h(
              "button",
              {
                class: "icon danger",
                title: "Delete this empty slot",
                onClick: () => emit("remove"),
              },
              "×"
            ),
          ]),
          h("div", null, "(empty slot)"),
          (!props.readOnly || props.allowKetcher) ? h(
            "button",
            {
              class: "icon",
              style: "margin-top:8px;",
              onClick: openKetcher,
            },
            "✎ Draw in Ketcher"
          ) : null,
        ]);
      }

      let svgEl;
      if (renderState.value === "deferred") {
        svgEl = h("div", { class: "render-deferred" }, "Structure preview loads on view");
      } else if (renderState.value === "loading") {
        svgEl = h("div", { class: "render-error" }, "Rendering…");
      } else if (renderState.value === "ok" && props.renderSrc) {
        svgEl = h("img", { src: props.renderSrc, alt: props.data.identifier || "molecule" });
      } else if (renderState.value === "ok" && svg.value) {
        svgEl = h("div", { innerHTML: svg.value });
      } else if (renderState.value === "empty") {
        svgEl = h(
          "div",
          {
            class: "render-error",
            style: "color:#999;font-style:italic;",
          },
          "(no SMILES)"
        );
      } else {
        // error
        svgEl = h("div", { class: "render-error" }, [
          "RDKit cannot render",
          h(
            "div",
            { style: "font-size:9px;margin-top:4px;" },
            (props.data?.["E-SMILES"] || "").slice(0, 60)
          ),
        ]);
      }

      const cardCls =
        "mol-card" +
        (props.compact ? " compact-review" : "") +
        (isIntendedButNotFormed ? " mol-not-formed" : "") +
        (isPointer ? " mol-pointer" : "") +
        (mr ? " mol-needs-review" : "") +
        (showMolparser.value ? " molparser-open" : "");
      return h("div", { class: cardCls, ref: cardElement }, [
        props.readOnly && !props.allowKetcher ? null : h("div", { class: "mol-actions" }, [
          (!props.readOnly || props.allowKetcher) ? h(
            "button",
            {
              class: "icon",
              title: partialPreview
                ? "Inspect or edit the full E-SMILES in Ketcher"
                : "Edit structure in Ketcher",
              onClick: openKetcher,
            },
            "✎"
          ) : null,
          props.readOnly ? null : h(
            "button",
            { class: "icon", title: "Copy SMILES", onClick: copySmiles },
            "📋"
          ),
          props.readOnly ? null : h(
            "button",
            {
              class: "icon",
              title: showSmiles.value ? "Collapse SMILES" : "Expand SMILES",
              onClick: () => (showSmiles.value = !showSmiles.value),
            },
            showSmiles.value ? "▴" : "▾"
          ),
          props.readOnly ? null : h(
            "button",
            {
              class: "icon danger",
              title: "Delete this molecule slot",
              onClick: () => {
                if (confirm(`Delete molecule [${props.data?.identifier || "?"}] ${fullEsmiles.slice(0, 30)}?`)) {
                  emit("remove");
                }
              },
            },
            "×"
          ),
        ]),
        h("div", { class: "mol-svg" }, [svgEl]),
        partialPreview ? h("aside", {
          class: "esmiles-partial-preview",
          role: "note",
          title: "RDKit renders only the molecular body before <sep>.",
        }, [
          h("b", null, "Partial preview · extension not visualized"),
          h("code", null, fullEsmiles),
          props.allowKetcher ? h("button", { onClick: openKetcher }, "Open full Ketcher") : null,
        ]) : null,
        gtPanel,
        h("div", { class: "mol-meta" }, [
          props.readOnly
            ? h("span", { class: "ident" }, props.data.identifier || "?")
            : h(Editable, {
                value: props.data.identifier || "",
                placeholder: "?",
                classes: "ident",
                showPlaceholder: false,
                onSave: onSaveIdent,
              }),
          props.provenanceLabel
            ? h("span", {
                class: "pdf-provenance-compact",
                title: props.provenanceTitle || `source: ${props.provenanceLabel}`,
              }, props.provenanceLabel)
            : null,
          sourceBadge,
          modelReviewBadge,
          molparserBadge,
          derivationBadge,
          h(
            "span",
            { style: "color:var(--fg-dim);font-size:10px;margin-left:auto;" },
            renderState.value === "ok" ? "✓" : ""
          ),
        ]),
        showSmiles.value
          ? h(
              "div",
              { class: "smiles-row" },
              props.data["E-SMILES"] || "(empty)"
            )
          : null,
        // v2 _source detail panel (bbox / md5 / pointer destination / model_review)
        showSmiles.value && (src.bbox || src.bbox_index !== undefined || src.molparser_md5 || src.points_to || mr)
          ? h("div", { class: "mol-source-detail" }, [
              mr
                ? h(
                    "div",
                    { class: "mol-source-mr" },
                    [
                      h("div", { class: "src-key" }, "⚠ model_review:"),
                      h("div", null, [
                        h("span", { class: "src-key" }, "  reviewer: "),
                        h("span", { class: "src-val" }, mr.reviewer || "?"),
                      ]),
                      h("div", null, [
                        h("span", { class: "src-key" }, "  confidence: "),
                        h(
                          "span",
                          {
                            class: "src-val",
                            style: `color: var(--${mrConfColor === "red" ? "red" : mrConfColor === "yellow" ? "orange" : "fg-muted"});font-weight:600;`,
                          },
                          mr.confidence || "?"
                        ),
                      ]),
                      h("div", null, [
                        h("span", { class: "src-key" }, "  concern: "),
                        h(
                          "span",
                          { class: "src-val", style: "white-space:normal;" },
                          mr.concern || "(none)"
                        ),
                      ]),
                      mr.suggest
                        ? h("div", { style: "margin-top:4px;" }, [
                            h("span", { class: "src-key" }, "  Suggested SMILES: "),
                            h(
                              "span",
                              {
                                class: "src-val",
                                style: "color:var(--green);font-weight:600;",
                              },
                              mr.suggest
                            ),
                          ])
                        : null,
                    ]
                  )
                : null,
              src.bbox
                ? h("div", null, [
                    h("span", { class: "src-key" }, "bbox: "),
                    h("span", { class: "src-val" }, JSON.stringify(src.bbox)),
                  ])
                : null,
              src.bbox_index !== undefined
                ? h("div", null, [
                    h("span", { class: "src-key" }, "bbox_index: "),
                    h("span", { class: "src-val" }, String(src.bbox_index)),
                  ])
                : null,
              src.molparser_md5
                ? h("div", null, [
                    h("span", { class: "src-key" }, "md5: "),
                    h("span", { class: "src-val" }, src.molparser_md5),
                  ])
                : null,
              src.points_to
                ? h("div", null, [
                    h("span", { class: "src-key" }, "→ "),
                    h("span", { class: "src-val" }, src.points_to),
                    src.from_template
                      ? ` (from ${src.from_template})`
                      : "",
                  ])
                : null,
              src.source_figure
                ? h("div", null, [
                    h("span", { class: "src-key" }, "source_figure: "),
                    h("span", { class: "src-val" }, src.source_figure),
                  ])
                : null,
              src.note
                ? h("div", { style: "color:var(--fg-muted);margin-top:2px;" }, [
                    h("span", { class: "src-key" }, "note: "),
                    src.note,
                  ])
                : null,
            ])
          : null,
        showMolparser.value && mp
          ? h("div", { class: "molparser-evidence" }, [
              h("div", { class: "molparser-evidence-head" }, [
                h("b", null, "Original recognition evidence"),
                h(
                  "span",
                  { class: `molparser-status ${mpCfg.className}` },
                  mpCfg.label
                ),
                h(
                  "span",
                  { class: "molparser-evidence-meta" },
                  `bbox ${mp.bbox_index ?? "?"}` +
                    (mp.score !== undefined ? ` · score ${mp.score}` : "") +
                    (mp.markush ? " · Markush" : "")
                ),
              ]),
              h("div", { class: "molparser-render" }, [
                molparserRenderState.value === "loading"
                  ? h("div", { class: "render-error" }, "MolParser Rendering structure…")
                  : molparserRenderState.value === "ok" && molparserSvg.value
                    ? h("div", { innerHTML: molparserSvg.value })
                    : h("div", { class: "render-error" }, "RDKit cannot render MolParser smi"),
              ]),
              h("div", { class: "molparser-fields" }, [
                h("div", { class: "molparser-field" }, [
                  h("span", { class: "field-key" }, "Extracted E-SMILES"),
                  h("code", null, props.data?.["E-SMILES"] || "(empty)"),
                ]),
                h("div", { class: "molparser-field" }, [
                  h("span", { class: "field-key" }, "MolParser raw smi"),
                  h("code", null, mp.smi || "(empty)"),
                ]),
                h("div", { class: "molparser-field" }, [
                  h("span", { class: "field-key" }, "MolParser raw caption"),
                  h("code", null, mp.caption || "(empty)"),
                ]),
                mp.esmi
                  ? h("div", { class: "molparser-field" }, [
                      h("span", { class: "field-key" }, "MolParser raw esmi"),
                      h("code", null, mp.esmi),
                    ])
                  : null,
                mp.md5
                  ? h("div", { class: "molparser-field compact" }, [
                      h("span", { class: "field-key" }, "md5"),
                      h("code", null, mp.md5),
                    ])
                  : null,
              ]),
            ])
          : null,
        props.data.description !== undefined
          ? (props.readOnly
              ? h("div", { class: "desc-row" }, props.data.description || "")
              : h(Editable, {
                  value: props.data.description || "",
                  placeholder: "(description, e.g. 'PhB(OH)₂ (1.5 equiv)')",
                  classes: "desc-row",
                  onSave: onSaveDesc,
                }))
          : null,
      ]);
    };
  },
};
