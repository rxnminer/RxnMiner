import { h, computed } from "vue";
import { store, setAtPath, markDirty, showToast } from "/static/lib/store.js";
import { SaveBar } from "/static/components/SaveBar.js?v=20260727a";
import { AnchorPanel } from "/static/components/AnchorPanel.js?v=20260801e";
import { ReactionRow } from "/static/components/ReactionRow.js";
import { TextsPanel } from "/static/components/TextsPanel.js";
import { KetcherModal } from "/static/components/KetcherModal.js";
import { Splitter } from "/static/components/Splitter.js";
import { AuditPanel } from "/static/components/AuditPanel.js";

export const TaskView = {
  setup() {
    const isDropped = computed(() => Boolean(store.taskData?._dropped));

    const addTemplate = () => {
      const arr = store.taskData.reaction_templates || [];
      const blank = {
        template_id: `T${arr.length + 1}_new`,
        _meta: {
          source_caption:
            store.taskMeta?.source_caption || "",
          source_subpanel: "",
          source_page: store.taskMeta?.page_index
            ? store.taskMeta.page_index - 1
            : 0,
        },
        reactants: [],
        conditions: [[]],
        products: [],
        metrics: [],
      };
      setAtPath(["reaction_templates"], [...arr, blank]);
    };

    const addExample = () => {
      const arr = store.taskData.selected_examples || [];
      const tids = (store.taskData.reaction_templates || []).map(
        (t) => t.template_id
      );
      const blank = {
        instance_id: `new-${arr.length + 1}`,
        inherit_template: tids[0] || "",
        markush_enumeration: {},
        reactants: [],
        conditions: [[]],
        products: [],
        metrics: [{ role: "yield", text: "", note: "" }],
      };
      setAtPath(["selected_examples"], [...arr, blank]);
    };

    const removeTemplate = (ti) => {
      const tid = store.taskData.reaction_templates[ti].template_id;
      const ok = confirm(
        `Delete template ${tid}?\n  · All template fields will be removed\n  · Linked selected_examples (inherit_template=${tid}) will not be deleted automatically`
      );
      if (!ok) return;
      const arr = [...store.taskData.reaction_templates];
      arr.splice(ti, 1);
      setAtPath(["reaction_templates"], arr);
    };

    const removeExample = (ei) => {
      const e = store.taskData.selected_examples[ei];
      const ok = confirm(`Delete example ${e.instance_id || `#${ei + 1}`}?`);
      if (!ok) return;
      const arr = [...store.taskData.selected_examples];
      arr.splice(ei, 1);
      setAtPath(["selected_examples"], arr);
    };

    const _deepClone = (v) => JSON.parse(JSON.stringify(v));

    const _uniqId = (base, takenSet) => {
      const stem = base ? `${base}-copy` : "copy";
      if (!takenSet.has(stem)) return stem;
      let n = 2;
      while (takenSet.has(`${stem}${n}`)) n += 1;
      return `${stem}${n}`;
    };

    const duplicateTemplate = (ti) => {
      const arr = store.taskData.reaction_templates || [];
      const t = arr[ti];
      if (!t) return;
      const taken = new Set(arr.map((x) => x.template_id).filter(Boolean));
      const cloned = _deepClone(t);
      cloned.template_id = _uniqId(t.template_id || "T_new", taken);
      const next = [...arr];
      next.splice(ti + 1, 0, cloned);
      setAtPath(["reaction_templates"], next);
      showToast(
        "success",
        `Copied → ${cloned.template_id} (review and apply after editing)`
      );
    };

    const duplicateExample = (ei) => {
      const arr = store.taskData.selected_examples || [];
      const e = arr[ei];
      if (!e) return;
      const taken = new Set(arr.map((x) => x.instance_id).filter(Boolean));
      const cloned = _deepClone(e);
      cloned.instance_id = _uniqId(e.instance_id || "ex", taken);
      const next = [...arr];
      next.splice(ei + 1, 0, cloned);
      setAtPath(["selected_examples"], next);
      showToast(
        "success",
        `Copied → ${cloned.instance_id} (review and apply after editing)`
      );
    };

    const templateToExample = (ti) => {
      const t = store.taskData.reaction_templates[ti];
      if (!t) return;
      const tid = t.template_id || `T${ti + 1}`;
      const candidates = (store.taskData.reaction_templates || [])
        .map((x) => x.template_id)
        .filter((x) => x && x !== tid);
      let inheritTo = candidates[0] || "";
      if (candidates.length > 1) {
        const pick = prompt(
          `Choose a parent template for ${tid}:\n${candidates.join(", ")}\nDefault: ${candidates[0]}. Leave blank for no parent.`,
          candidates[0]
        );
        if (pick === null) return;
        inheritTo = pick.trim();
      } else if (candidates.length === 0) {
        const ok = confirm(
          `No other template is available. The converted example will have no parent. Continue?`
        );
        if (!ok) return;
        inheritTo = "";
      }
      const newExample = {
        instance_id: tid,
        inherit_template: inheritTo,
        markush_enumeration: t.markush_enumeration || {},
        reactants: t.reactants || [],
        conditions: t.conditions || [[]],
        products: t.products || [],
        metrics: t.metrics || [],
      };
      const tplArr = [...store.taskData.reaction_templates];
      tplArr.splice(ti, 1);
      const exArr = [...(store.taskData.selected_examples || []), newExample];
      setAtPath(["reaction_templates"], tplArr);
      setAtPath(["selected_examples"], exArr);
    };

    const exampleToTemplate = (ei) => {
      const e = store.taskData.selected_examples[ei];
      if (!e) return;
      const newTid =
        e.instance_id || `T${(store.taskData.reaction_templates || []).length + 1}_new`;
      const meta = {
        source_caption: store.taskMeta?.source_caption || "",
        source_subpanel: store.taskMeta?.source_subpanel || "",
        source_page: store.taskMeta?.page_index
          ? store.taskMeta.page_index - 1
          : 0,
      };
      const newTemplate = {
        template_id: newTid,
        _meta: meta,
        reactants: e.reactants || [],
        conditions: e.conditions || [[]],
        products: e.products || [],
        metrics: e.metrics || [],
      };
      const tplArr = [...(store.taskData.reaction_templates || []), newTemplate];
      const exArr = [...store.taskData.selected_examples];
      exArr.splice(ei, 1);
      setAtPath(["reaction_templates"], tplArr);
      setAtPath(["selected_examples"], exArr);
    };

    const convertTemplateToExample = (ti) => {
      const t = store.taskData.reaction_templates[ti];
      const tid = t?.template_id || `T${ti + 1}`;
      const ok = confirm(
        `Convert template ${tid} to an example?\n\n` +
          `Retain all reactants, products, conditions, and metrics\n` +
          `Rename: template_id "${tid}" → instance_id "${tid}"\n` +
          `Add: parent template (next dialog) and empty Markush enumeration\n` +
          `Remove: template-only metadata`
      );
      if (!ok) return;
      templateToExample(ti);
    };

    const convertExampleToTemplate = (ei) => {
      const e = store.taskData.selected_examples[ei];
      const iid = e?.instance_id || `#${ei + 1}`;
      const ok = confirm(
        `Convert example ${iid} to a template?\n\n` +
          `Retain all reactants, products, conditions, and metrics\n` +
          `Rename: instance_id "${iid}" → template_id "${iid}"\n` +
          `Add: metadata from the current figure caption\n` +
          `Remove: parent-template link and Markush enumeration`
      );
      if (!ok) return;
      exampleToTemplate(ei);
    };

    return () => {
      if (!store.currentSlug) {
        return h("main", { class: "col task-view layout-banner" }, [
          h("div", { class: "empty-hint" }, "Select a paper from the sidebar"),
        ]);
      }
      if (!store.currentTid) {
        return h("main", { class: "col task-view layout-banner" }, [
          h("div", { class: "empty-hint" }, "Select a task"),
        ]);
      }
      if (store.loading || !store.taskData) {
        return h("main", { class: "col task-view layout-banner" }, [
          h("div", { class: "loading" }, "Loading task…"),
        ]);
      }

      const data = store.taskData;
      const rawTemplates = data.reaction_templates || [];
      const examples = data.selected_examples || [];

      // ── v2 multistep ordering ────────────────────────────────
      // Templates: sort by _multistep.step_order ascending; missing
      // step_order falls back to original array index. We keep the
      // ORIGINAL array index (`origIdx`) as the path[1] value so save
      // round-trips don't shuffle data — only display order changes.
      const templates = rawTemplates
        .map((t, origIdx) => ({
          tpl: t,
          origIdx,
          stepOrder:
            (t._multistep && typeof t._multistep.step_order === "number")
              ? t._multistep.step_order
              : Number.MAX_SAFE_INTEGER,
        }))
        .sort((a, b) => {
          if (a.stepOrder !== b.stepOrder) return a.stepOrder - b.stepOrder;
          return a.origIdx - b.origIdx;
        });

      // Group examples by inherit_template, sub-sort by _multistep.step_order
      const exByT = {};
      for (let i = 0; i < examples.length; i++) {
        const e = examples[i];
        const k = e.inherit_template || "_orphan";
        const so =
          (e._multistep && typeof e._multistep.step_order === "number")
            ? e._multistep.step_order
            : Number.MAX_SAFE_INTEGER;
        (exByT[k] = exByT[k] || []).push({ ex: e, idx: i, stepOrder: so });
      }
      for (const k in exByT) {
        exByT[k].sort((a, b) => {
          if (a.stepOrder !== b.stepOrder) return a.stepOrder - b.stepOrder;
          return a.idx - b.idx;
        });
      }

      // Layout is always side-by-side (anchor on left, reactions on right).
      // The old layout-banner mode (anchor as top banner) was removed —
      // chemists prefer to see the anchor PNG full-height while reading
      // the reaction rows side-by-side. Width of the anchor column is
      // user-controlled via the splitter (see Splitter component).
      const cls =
        "col task-view layout-side" +
        (isDropped.value ? " dropped" : "");
      // Inline CSS variable lets the grid template column width come from
      // the reactive store (live update during drag).
      const styleVars = `--anchor-w: ${store.anchorWidth}px;`;

      return h("main", { class: cls, style: styleVars }, [
        h(SaveBar),
        h(AnchorPanel),
        h(Splitter),
        h("div", { class: "task-body" }, [
          h(AuditPanel),
          // Pre-compute: which examples belong to no existing template
          // (inherit_template empty or pointing to a now-deleted template).
          // We render those in a separate "orphan" section so they're not lost.
          ...(() => {
            const knownTids = new Set(
              templates.map((t) => t.tpl.template_id).filter(Boolean)
            );
            const orphans = [];
            for (let i = 0; i < examples.length; i++) {
              const e = examples[i];
              if (!e.inherit_template || !knownTids.has(e.inherit_template)) {
                orphans.push({ ex: e, idx: i });
              }
            }
            if (orphans.length === 0) return [];
            const concreteOnly = templates.length === 0;
            return [
              h(
                "div",
                { class: "examples-section" },
                [
                  h("h3", null, [
                    concreteOnly
                      ? "Reaction"
                      : "⚠ Example without a parent template",
                    h("span", { class: "count" }, ` ${orphans.length}`),
                    concreteOnly
                      ? null
                      : h(
                          "span",
                          {
                            style:
                              "font-size:11px;color:var(--fg-muted);font-style:italic;margin-left:8px;font-weight:400;",
                          },
                          "Convert to a template or select a parent template"
                        ),
                  ]),
                  ...orphans.map(({ ex, idx }) =>
                    h(ReactionRow, {
                      kind: "example",
                      concreteOnly,
                      data: ex,
                      path: ["selected_examples", idx],
                      rowKey: `example:${idx}`,
                      onRemove: () => removeExample(idx),
                      onConvertType: () => convertExampleToTemplate(idx),
                      onDuplicate: () => duplicateExample(idx),
                      key: ex.instance_id || idx,
                    })
                  ),
                ]
              ),
            ];
          })(),

          // Templates + their step / substrate examples (v2: ordered by step_order)
          ...templates.map(({ tpl: t, origIdx: ti, stepOrder }) => {
            const tid = t.template_id || `T${ti + 1}`;
            const myExamples = exByT[tid] || [];
            const isMsStep = stepOrder !== Number.MAX_SAFE_INTEGER;
            return h("section", { key: tid }, [
              h(ReactionRow, {
                kind: "template",
                data: t,
                path: ["reaction_templates", ti],
                rowKey: `template:${ti}`,
                onRemove: () => removeTemplate(ti),
                onConvertType: () => convertTemplateToExample(ti),
                onDuplicate: () => duplicateTemplate(ti),
              }),
              myExamples.length > 0
                ? h("div", { class: "examples-section" }, [
                    h("h3", null, [
                      `${tid} · ${isMsStep ? "Step example" : "Example"}`,
                      h("span", { class: "count" }, ` ${myExamples.length}`),
                    ]),
                    ...myExamples.map(({ ex, idx }) =>
                      h(ReactionRow, {
                        kind: "example",
                        data: ex,
                        path: ["selected_examples", idx],
                        rowKey: `example:${idx}`,
                        onRemove: () => removeExample(idx),
                        onConvertType: () => convertExampleToTemplate(idx),
                        onDuplicate: () => duplicateExample(idx),
                        key: ex.instance_id || idx,
                      })
                    ),
                  ])
                : null,
            ]);
          }),
          h("div", { class: "add-row-buttons" }, [
            h(
              "button",
              {
                class: "add-row-btn primary-add",
                onClick: addTemplate,
                title: "Add a reaction template missing from the extraction",
              },
              [
                h("div", { class: "add-row-icon" }, "＋"),
                h("div", { class: "add-row-title" }, "Add a reaction"),
                h(
                  "div",
                  { class: "add-row-sub" },
                  "Template — draw reactants, products, and conditions in Ketcher"
                ),
              ]
            ),
            h(
              "button",
              {
                class: "add-row-btn",
                onClick: addExample,
                title: "Add a concrete reaction example",
              },
              [
                h("div", { class: "add-row-icon" }, "＋"),
                h("div", { class: "add-row-title" }, "Add an example"),
                h(
                  "div",
                  { class: "add-row-sub" },
                  "Example — a substrate scope entry or table row"
                ),
              ]
            ),
          ]),

          // Texts (paper-level captions / footnotes)
          h(TextsPanel),
        ]),

        // Ketcher iframe modal
        store.ketcherTarget ? h(KetcherModal) : null,
      ]);
    };
  },
};
