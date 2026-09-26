// Ketcher iframe modal. Opens when store.ketcherTarget is set.
//
// Two modes:
//
// 1. EDIT mode (modify an existing molecule's SMILES)
//    store.ketcherTarget = {
//      mode: "edit",
//      path: ["reaction_templates", 0, "reactants", 1, "E-SMILES"],
//      smiles: "<initial smiles>",
//      identifier: "1a",
//      molecularContext: "PhB(OH)2 starting material"
//    }
//
// 2. APPEND mode (create a new molecule, append to an array)
//    store.ketcherTarget = {
//      mode: "append",
//      parentPath: ["reaction_templates", 0, "reactants"],
//      smiles: "",                   // optional starter
//      identifier: "",
//    }
//    On save, a fresh MoleculeBlock is appended:
//      { identifier: "", "E-SMILES": <ketcher-output>, description: "" }
//
// Communication with the embedded DP Ketcher E-SMILES standalone:
//   const ketcher = iframe.contentWindow.ketcher;
//   await ketcher.setMolecule(fullEsmiles);
//   const newEsmiles = await ketcher.getEsmiles();
// Never strip `<sep>` or use getSmiles(): both lose MolParser extension semantics.
import { h, ref } from "vue";
import { store, setAtPath, getAtPath, showToast } from "/static/lib/store.js";
import {
  exportEsmiles,
  loadEsmiles,
  waitForEsmilesKetcher,
} from "/static/lib/esmiles-ketcher.js";

const KETCHER_SRC = "/ketcher/index.html";

export const KetcherModal = {
  setup() {
    const iframeEl = ref(null);
    const status = ref("loading");      // loading | ready | error
    const errMsg = ref("");

    async function onIframeLoad() {
      const iframe = iframeEl.value;
      if (!iframe) return;
      try {
        const ketcher = await waitForEsmilesKetcher(iframe.contentWindow);
        if (!ketcher) {
          status.value = "error";
          errMsg.value =
            "Ketcher did not initialize within six seconds.";
          return;
        }
        const initial = (store.ketcherTarget?.smiles || "").trim();
        if (initial) await loadEsmiles(ketcher, initial);
        status.value = "ready";
      } catch (e) {
        status.value = "error";
        errMsg.value = e?.message || String(e);
      }
    }

    const close = (commitNew) => {
      if (commitNew) {
        // Caller already wrote-back via "save" path; just clear target.
      }
      store.ketcherTarget = null;
    };

    const saveAndClose = async () => {
      const iframe = iframeEl.value;
      if (!iframe) return;
      const ketcher = iframe.contentWindow?.ketcher;
      if (!ketcher) {
        showToast("error", "Ketcher is not ready");
        return;
      }
      const target = store.ketcherTarget;
      let esmiles;
      try {
        esmiles = await exportEsmiles(ketcher, target?.smiles || "");
      } catch (e) {
        status.value = "error";
        errMsg.value = `Cannot read Ketcher E-SMILES: ${e.message}`;
        showToast("error", errMsg.value);
        return;
      }

      const mode = target.mode || (target.path ? "edit" : "append");

      if (mode === "append") {
        const parentPath = target.parentPath;
        if (!parentPath) {
          showToast("error", "Append mode requires parentPath");
          return;
        }
        const cur = getAtPath(parentPath) || [];
        const newBlock = {
          identifier: "",
          "E-SMILES": esmiles,
          description: "",
        };
        setAtPath(parentPath, [...cur, newBlock]);
        close(true);
        showToast(
          "success",
          "Molecule added. Review and apply changes to save."
        );
        return;
      }

      // A PDF occurrence supplies an in-memory callback. It stages one working
      // reviewed value; durable Apply remains owned by the PDF review lifecycle.
      if (typeof target.onCommit === "function") {
        try {
          await target.onCommit(esmiles, target.occurrence || null);
        } catch (error) {
          status.value = "error";
          errMsg.value = `Cannot stage E-SMILES: ${error?.message || error}`;
          showToast("error", errMsg.value);
          return;
        }
        close(true);
        showToast("success", "Full E-SMILES staged. Review changes and select Apply.");
        return;
      }

      // Figure edit mode writes the complete reviewed E-SMILES to working state.
      setAtPath(target.path, esmiles);
      close(true);
      showToast("success", "Structure updated. Review and apply changes to save.");
    };

    return () => {
      const t = store.ketcherTarget;
      if (!t) return null;
      const isAppend = (t.mode || (t.path ? "edit" : "append")) === "append";
      const titleText = isAppend ? "✎ Ketcher: new molecule" : "✎ Ketcher: edit structure";
      const headerLabel = [
        t.identifier ? `[${t.identifier}]` : null,
        t.molecularContext ? `${t.molecularContext}` : null,
      ]
        .filter(Boolean)
        .join(" · ");
      const submitText = isAppend ? "Add to reaction" : "Use this structure";
      return h(
        "div",
        {
          class: "modal-backdrop",
          onClick: (e) => {
            if (e.target.classList.contains("modal-backdrop")) close(false);
          },
        },
        [
          h("div", { class: "modal-card" }, [
            h("div", { class: "modal-header" }, [
              h("div", null, [
                h("b", { style: "font-size:14px;" }, titleText),
                headerLabel
                  ? h(
                      "span",
                      {
                        style:
                          "color:var(--fg-muted);font-size:12px;margin-left:12px;",
                      },
                      headerLabel
                    )
                  : null,
              ]),
              h(
                "button",
                { class: "icon", onClick: () => close(false), title: "Close" },
                "✕"
              ),
            ]),
            h("div", { class: "modal-body" }, [
              h("iframe", {
                ref: iframeEl,
                src: KETCHER_SRC,
                onLoad: onIframeLoad,
              }),
            ]),
            h("div", { class: "modal-footer" }, [
              status.value === "loading"
                ? h(
                    "span",
                    { style: "color:var(--fg-muted);font-size:12px;" },
                    "Loading Ketcher…"
                  )
                : status.value === "error"
                ? h(
                    "span",
                    { style: "color:var(--red);font-size:12px;" },
                    `Load failed: ${errMsg.value}`
                  )
                : h(
                    "span",
                    { style: "color:var(--green);font-size:12px;" },
                    "Ketcher ready ✓"
                  ),
              h("button", { onClick: () => close(false) }, "Cancel"),
              h(
                "button",
                {
                  class: "primary",
                  onClick: saveAndClose,
                  disabled: status.value !== "ready",
                },
                submitText
              ),
            ]),
          ]),
        ]
      );
    };
  },
};
