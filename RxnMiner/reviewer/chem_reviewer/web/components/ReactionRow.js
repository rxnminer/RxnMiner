// Renders one reaction in the canonical organic-chemistry layout:
//   [reactant1] + [reactant2] →conditions_top→ [product1]
//                 conditions_bottom
//
// `kind` : "template" | "example"
// `data` : a reaction template OR selected_examples[i]
// `path` : path inside store.taskData (for save-back)
import { h, computed } from "vue";
import { store, setAtPath, markDirty, getAtPath } from "/static/lib/store.js";
import { MoleculeCard } from "/static/components/MoleculeCard.js";
import { Editable } from "/static/components/Editable.js";
import {
  bboxKey,
  reactionBboxes,
  reactionElementId,
} from "/static/lib/reaction-link.js";

const ROLES_TOP = new Set([
  "catalyst",
  "ligand",
  "reagent",
  "oxidant",
  "additive",
]);
const ROLES_BOTTOM = new Set([
  "temperature",
  "time",
  "light",
  "atmosphere",
  "solvent",
  "others",
]);
const ALL_ROLES = [
  "catalyst",
  "ligand",
  "reagent",
  "oxidant",
  "additive",
  "solvent",
  "temperature",
  "time",
  "light",
  "atmosphere",
  "others",
];

const METRIC_ROLES = [
  "yield",
  "ee",
  "dr",
  "er",
  "method",
  "characterization",
  "conversion",
  "outcome", // v2: failed reactions report outcome="no reaction" / "0% (decomposed)"
];

function condText(c) {
  return c?.description || c?.text || "";
}
function condRole(c) {
  return c?.role || "";
}

const ROLE_ZH = {
  start: "Start",
  intermediate: "Intermediate",
  final: "Final",
};

export const ReactionRow = {
  props: {
    kind: { type: String, default: "template" },
    concreteOnly: { type: Boolean, default: false },
    data: { type: Object, required: true },
    path: { type: Array, required: true },
    rowKey: { type: String, required: true },
  },
  emits: ["remove", "convertType", "duplicate"],
  setup(props, { emit }) {
    return () => {
      const data = props.data;
      const reactants = data.reactants || [];
      const products = data.products || [];
      const stepsArr = data.conditions || [];
      const nSteps = stepsArr.length;

      const metrics = data.metrics || [];
      const linkedBboxes = reactionBboxes(data);
      const yieldEntry = metrics.find((m) => m.role === "yield");
      const yieldText = yieldEntry?.text || "";
      const meta = data._meta || {};
      // v2 multistep / outcome metadata
      const ms = data._multistep || null;
      const outcome = data._outcome || null;
      const isFailed = Boolean(outcome?.is_failed);
      const expType = outcome?.experiment_type || null;
      const isControlExp = !isFailed && expType;

      // ── Header ─────────────────────────────────────
      // Available template_ids for the inherit dropdown(only relevant for
      // examples). Read live from store so newly added/converted templates
      // show up immediately.
      const availableTids = (store.taskData?.reaction_templates || [])
        .map((t) => t.template_id)
        .filter(Boolean);
      const stepBadge = ms
        ? h(
            "span",
            {
              class: "step-badge",
              title: `Multistep synthesis: ${ms.sequence_id || ""} · Step ${ms.step_order}`,
            },
            [
              `Step ${ms.step_order ?? "?"}`,
              ms.intermediate_role
                ? h(
                    "span",
                    {
                      style:
                        "margin-left:4px;opacity:0.75;font-weight:400;",
                    },
                    `· ${ROLE_ZH[ms.intermediate_role] || ms.intermediate_role}`
                  )
                : null,
            ]
          )
        : null;

      // v2 failed badge — bright red, mandatory if _outcome.is_failed
      const failedBadge = isFailed
        ? h(
            "span",
            {
              class: "failed-badge",
              title: outcome?.note || outcome?.outcome_text || "Failed reaction",
            },
            [
              "✗ Failed",
              outcome?.outcome_text
                ? h(
                    "span",
                    {
                      style:
                        "margin-left:4px;font-weight:400;opacity:0.85;",
                    },
                    `· ${(outcome.outcome_text || "").slice(0, 30)}`
                  )
                : null,
            ]
          )
        : null;

      // v2 control-experiment badge (TEMPO scavenger / KIE / clock)
      const ctrlBadge = isControlExp
        ? h(
            "span",
            {
              class: "control-badge",
              title: outcome?.purpose || `Control experiment: ${expType}`,
            },
            `🧪 ${expType}`
          )
        : null;

      const labelInner =
        props.kind === "template"
          ? [
              h("span", { class: "label-tag template" }, "Template"),
              h("b", null, data.template_id || "?"),
              stepBadge,
              failedBadge,
              ctrlBadge,
              meta.source_caption
                ? h(
                    "span",
                    { style: "color:var(--fg-muted);margin-left:8px;" },
                    `${meta.source_caption}${
                      meta.source_subpanel ? " (" + meta.source_subpanel + ")" : ""
                    }`
                  )
                : null,
            ]
          : props.concreteOnly
            ? [
                h("span", { class: "label-tag example" }, "Reaction"),
                h("b", null, data.instance_id || "?"),
                stepBadge,
                failedBadge,
                ctrlBadge,
              ]
            : [
              h("span", { class: "label-tag example" }, "Example"),
              h("b", null, data.instance_id || "?"),
              stepBadge,
              failedBadge,
              ctrlBadge,
              h(
                "span",
                {
                  style:
                    "color:var(--fg-muted);margin-left:8px;font-size:11px;",
                },
                "↪ Inherits"
              ),
              h(
                "select",
                {
                  style:
                    "background:var(--bg-sunken);border:1px solid var(--border);font-size:11px;color:var(--fg);padding:2px 6px;border-radius:3px;font-family:var(--mono);cursor:pointer;",
                  value: data.inherit_template || "",
                  onChange: (e) =>
                    setAtPath(
                      [...props.path, "inherit_template"],
                      e.target.value
                    ),
                  title: "Parent template",
                },
                [
                  h("option", { value: "" }, "(no parent)"),
                  ...availableTids.map((tid) =>
                    h(
                      "option",
                      {
                        value: tid,
                        selected: tid === (data.inherit_template || ""),
                      },
                      tid
                    )
                  ),
                  data.inherit_template &&
                  !availableTids.includes(data.inherit_template)
                    ? h(
                        "option",
                        {
                          value: data.inherit_template,
                          selected: true,
                          disabled: true,
                        },
                        `${data.inherit_template} (not found)`
                      )
                    : null,
                ]
              ),
            ];

      const yieldEl = yieldText
        ? h("span", { class: "yield-tag" }, `Yield ${yieldText}`)
        : null;

      const bboxEl = linkedBboxes.length
        ? h(
            "button",
            {
              class: "reaction-bbox-link",
              title: "Highlight the source boxes for this reaction",
              onClick: () => {
                const target = linkedBboxes[0];
                store.activeReactionKey = props.rowKey;
                store.activeBboxKey = bboxKey(target);
                store.activeBboxCycle = 0;
              },
            },
            `BBox ${linkedBboxes
              .map((x) => x.bbox_index ?? "box")
              .join(", ")}`
          )
        : null;

      // ── Reactant side ──────────────────────────────
      const addReactant = () => {
        store.ketcherTarget = {
          mode: "append",
          parentPath: [...props.path, "reactants"],
          smiles: "",
          identifier: "",
          molecularContext: "New reactant — draw, then select Add to reaction",
        };
      };
      const removeReactantAt = (idx) => {
        const arr = [...reactants];
        arr.splice(idx, 1);
        setAtPath([...props.path, "reactants"], arr);
      };
      const reactantNodes = [];
      reactants.forEach((r, i) => {
        if (i > 0) reactantNodes.push(h("span", { class: "rxn-plus" }, "+"));
        reactantNodes.push(
          h(MoleculeCard, {
            data: r,
            path: [...props.path, "reactants", i],
            onRemove: () => removeReactantAt(i),
            key: `r-${i}`,
          })
        );
      });
      reactantNodes.push(
        h(
          "button",
          {
            class: "add-mol-btn",
            onClick: addReactant,
            title: "Draw a reactant in Ketcher",
          },
          "+"
        )
      );

      // ── Product side ───────────────────────────────
      const addProduct = () => {
        store.ketcherTarget = {
          mode: "append",
          parentPath: [...props.path, "products"],
          smiles: "",
          identifier: "",
          molecularContext: "New product — draw, then select Add to reaction",
        };
      };
      const removeProductAt = (idx) => {
        const arr = [...products];
        arr.splice(idx, 1);
        setAtPath([...props.path, "products"], arr);
      };
      const productNodes = [];
      products.forEach((p, i) => {
        if (i > 0) productNodes.push(h("span", { class: "rxn-plus" }, "+"));
        productNodes.push(
          h(MoleculeCard, {
            data: p,
            path: [...props.path, "products", i],
            onRemove: () => removeProductAt(i),
            key: `p-${i}`,
          })
        );
      });
      productNodes.push(
        h(
          "button",
          {
            class: "add-mol-btn",
            onClick: addProduct,
            title: "Draw a product in Ketcher",
          },
          "+"
        )
      );

      // ── Conditions arrow ───────────────────────────
      //
      // Structure:
      //   • Single step (nSteps <= 1): one classic arrow with top/bottom pills.
      //   • Multi step (nSteps >= 2): each step rendered as its own arrow
      //     block, separated by a "▼ then" connector, so chemists can see
      //     "step 1 conditions ▼ step 2 conditions ▼ step 3 ..." instead of
      //     all pills mashed onto one arrow.
      //
      // Each step block has its own:
      // After the last step there's a "+ step" button to append a new sub-step.

      const stepsPath = [...props.path, "conditions"];

      const addConditionToStep = (sIdx) => {
        const newCond = {
          identifier: "",
          "E-SMILES": "",
          description: "",
          role: "reagent",
          note: "",
        };
        const cur = stepsArr[sIdx] || [];
        setAtPath([...stepsPath, sIdx], [...cur, newCond]);
      };

      const addStep = () => {
        const cur = stepsArr || [];
        // New step starts with a blank reagent so chemists immediately see
        // a pill they can edit (rather than an empty arrow with a counter).
        setAtPath(stepsPath, [
          ...cur,
          [
            {
              identifier: "",
              "E-SMILES": "",
              description: "",
              role: "reagent",
              note: "",
            },
          ],
        ]);
      };

      const removeStep = (sIdx) => {
        const cur = stepsArr || [];
        const step = cur[sIdx] || [];
        const ok = confirm(
          `Delete step ${sIdx + 1} and its ${step.length} conditions?\n` +
            `(Remaining steps will be renumbered)`
        );
        if (!ok) return;
        const next = [...cur];
        next.splice(sIdx, 1);
        // Never let conditions become an empty list — keep at least [[]]
        setAtPath(stepsPath, next.length ? next : [[]]);
      };

      const renderStepBlock = (step, sIdx, isLast) => {
        const stepTopPills = [];
        const stepBottomPills = [];
        (step || []).forEach((c, cIdx) => {
          const role = condRole(c).toLowerCase();
          const path = [...stepsPath, sIdx, cIdx];
          const pill = h(CondPill, {
            data: c,
            path,
            stepIndex: sIdx,
            condIndex: cIdx,
            multiStep: nSteps > 1,
            key: `${sIdx}-${cIdx}`,
          });
          if (ROLES_BOTTOM.has(role)) stepBottomPills.push(pill);
          else stepTopPills.push(pill);
        });

        const arrowChildren = [
          nSteps > 1
            ? h("div", { class: "step-header" }, [
                h(
                  "span",
                  {
                    class: "step-label",
                    title: `Step ${sIdx + 1}, of ${nSteps} steps`,
                  },
                  `Step ${sIdx + 1}`
                ),
                h(
                  "button",
                  {
                    class: "step-remove-btn",
                    title: "Delete this step and its conditions",
                    onClick: () => removeStep(sIdx),
                  },
                  "× Delete step"
                ),
              ])
            : null,
          h("div", { class: "conditions-top" }, stepTopPills),
          h("div", { class: "arrow-line" }),
          h("div", { class: "conditions-bottom" }, [
            ...stepBottomPills,
            h(
              "button",
              {
                class: "add-cond-btn",
                onClick: () => addConditionToStep(sIdx),
                title:
                  nSteps > 1
                    ? `Add a condition to step ${sIdx + 1}`
                    : "Add a condition",
              },
              "+ Condition"
            ),
            // The "+ step" button only shows on the last step block.
            isLast
              ? h(
                  "button",
                  {
                    class: "add-cond-btn add-step-btn",
                    onClick: addStep,
                    title:
                      nSteps >= 1
                        ? `Add a step after step ${nSteps}`
                        : "Add a reaction step",
                    style: "margin-left:4px;",
                  },
                  "+ step"
                )
              : null,
          ]),
        ];
        return h("div", { class: "rxn-arrow", key: `step-${sIdx}` }, arrowChildren);
      };

      // Render: at least 1 step block (force at least [[]] so we always
      // have a place to add conditions even if data.conditions is empty).
      const renderSteps =
        stepsArr.length === 0 ? [[]] : stepsArr;
      const stepBlocks = [];
      renderSteps.forEach((step, sIdx) => {
        stepBlocks.push(renderStepBlock(step, sIdx, sIdx === renderSteps.length - 1));
        if (sIdx < renderSteps.length - 1) {
          stepBlocks.push(
            h("div", { class: "step-connector", key: `conn-${sIdx}` }, "▼ then")
          );
        }
      });

      const arrowEl =
        nSteps > 1
          ? h("div", { class: "rxn-multistep-stack" }, stepBlocks)
          : stepBlocks[0];

      // ── Card ───────────────────────────────────────
      const rowCls =
        "reaction-row" +
        (props.kind === "example" ? " example" : "") +
        (isFailed ? " row-failed" : "") +
        (isControlExp ? " row-control" : "") +
        (store.activeReactionKey === props.rowKey
          ? " bbox-linked-active"
          : "");
      return h(
        "div",
        {
          id: reactionElementId(props.rowKey),
          class: rowCls,
          "data-reaction-key": props.rowKey,
        },
        [
          // _outcome.note / outcome_text / reason details (failed / control)
          (isFailed || isControlExp) && (outcome?.note || outcome?.reason || outcome?.outcome_text || outcome?.purpose)
            ? h(
                "div",
                {
                  class: "outcome-note",
                  title: "V2 outcome details",
                },
                [
                  isFailed
                    ? h("span", { class: "outcome-tag failed" }, "Failure reason:")
                    : h("span", { class: "outcome-tag control" }, `Control experiment (${expType}):`),
                  " ",
                  outcome?.outcome_text || outcome?.note || outcome?.reason || outcome?.purpose,
                  outcome?.intended_product_identifier
                    ? h(
                        "div",
                        {
                          style:
                            "margin-top:3px;color:var(--fg-muted);font-size:11px;",
                        },
                        `Intended product → ${outcome.intended_product_identifier}`
                      )
                    : null,
                ]
              )
            : null,
          h("div", { class: "row-header" }, [
            h("div", { class: "label" }, labelInner),
            bboxEl,
            yieldEl,
            h("div", { class: "row-actions" }, [
              h(
                "button",
                {
                  class: "icon",
                  title:
                    props.kind === "template"
                      ? "Duplicate this reaction template below"
                      : "Duplicate this example to edit substituents or yield",
                  onClick: () => emit("duplicate"),
                  style:
                    "background:var(--bg-card);border-color:var(--border);font-size:11px;padding:4px 10px;",
                },
                "📋 Duplicate"
              ),
              h(
                "button",
                {
                  class: "icon",
                  title:
                    props.kind === "template"
                      ? "Convert this template to an example"
                      : "Convert this example to a template",
                  onClick: () => emit("convertType"),
                  style:
                    "background:var(--bg-card);border-color:var(--border);font-size:11px;padding:4px 10px;",
                },
                [
                  "🔄 ",
                  props.kind === "template" ? "Make example" : "Make template",
                ]
              ),
              h(
                "button",
                {
                  class: "icon danger",
                  title:
                    props.kind === "template"
                      ? "Delete this reaction template"
                      : "Delete this reaction example",
                  onClick: () => emit("remove"),
                },
                props.kind === "template" ? "Delete reaction" : "Delete example"
              ),
            ]),
          ]),
          h("div", { class: "rxn-line" }, [
            h("div", { class: "rxn-side left" }, reactantNodes),
            arrowEl,
            h("div", { class: "rxn-side right" }, productNodes),
          ]),
          h(MetricsEditor, {
            metrics,
            path: [...props.path, "metrics"],
            products,
          }),
          // Markush enumeration (examples only)
          props.kind === "example" && data.markush_enumeration
            ? h(MarkushEnum, {
                data: data.markush_enumeration,
                path: [...props.path, "markush_enumeration"],
              })
            : null,
        ]
      );
    };
  },
};

// ── Condition pill ─────────────────────────────────────────
const CondPill = {
  props: {
    data: { type: Object, required: true },
    path: { type: Array, required: true },
    stepIndex: { type: Number, required: true },
    condIndex: { type: Number, required: true },
    multiStep: { type: Boolean, default: false },
  },
  setup(props) {
    return () => {
      const c = props.data;
      const role = condRole(c) || "?";
      const text = condText(c);

      const onSaveText = (newText) => {
        const fieldKey = c.description !== undefined ? "description" : "text";
        setAtPath([...props.path, fieldKey], newText);
      };
      const onChangeRole = (newRole) => {
        setAtPath([...props.path, "role"], newRole);
      };
      const onDelete = () => {
        const stepPath = props.path.slice(0, -1);
        const step = [...(getAtPath(stepPath) || [])];
        step.splice(props.condIndex, 1);
        setAtPath(stepPath, step);
      };

      return h("span", { class: "cond-pill" }, [
        props.multiStep
          ? h(
              "span",
              { class: "role-tag", title: "step number" },
              `s${props.stepIndex + 1}`
            )
          : null,
        h(
          "select",
          {
            class: "role-tag",
            style:
              "background:transparent;border:none;color:var(--fg-muted);font-size:9px;text-transform:uppercase;cursor:pointer;letter-spacing:0.4px;",
            value: role,
            onChange: (e) => onChangeRole(e.target.value),
            title: "Change role",
          },
          ALL_ROLES.map((r) =>
            h("option", { value: r, selected: r === role }, r)
          )
        ),
        h(Editable, {
          value: text,
          placeholder: "(condition)",
          onSave: onSaveText,
        }),
        h(
          "button",
          { class: "delete-btn", onClick: onDelete, title: "Delete condition" },
          "×"
        ),
      ]);
    };
  },
};

// ── Metrics editor (yield/ee/dr/method/...) ───────────────
//
// HANDOFF.md §3.2: every metric MUST point at the product it measures via
// the `of` field. 604 / 606 metrics in the dataset do. We provide a select
// of the row's own product identifiers so chemists can fix a wrong `of`
// or add it when missing.
const MetricsEditor = {
  props: {
    metrics: { type: Array, default: () => [] },
    path: { type: Array, required: true },
    // Products from the parent row — used to populate the `of` <select>.
    // Items are MoleculeBlock dicts; we read .identifier from each.
    products: { type: Array, default: () => [] },
  },
  setup(props) {
    return () => {
      const items = props.metrics;
      // Distinct product identifiers that this row produces.
      const productIds = (props.products || [])
        .map((p) => p?.identifier)
        .filter(Boolean);
      // De-dupe while preserving order.
      const productOpts = [];
      const seen = new Set();
      for (const id of productIds) {
        if (!seen.has(id)) {
          productOpts.push(id);
          seen.add(id);
        }
      }
      return h("div", { class: "metrics-row" }, [
        ...items.map((m, i) => {
          const roleColor =
            m.role === "outcome" ? "var(--red)" : "var(--green)";
          const ofVal = m.of || "";
          // Surface "of" pointing to a non-existent product (e.g. row's
          // products got deleted) so reviewer can fix it instead of
          // silently dropping the value.
          const ofMissing =
            ofVal && !productOpts.includes(ofVal);
          return h(
            "span",
            {
              key: i,
              class:
                "cond-pill metric-pill" +
                (m.role === "outcome" ? " metric-outcome" : "") +
                (ofMissing ? " metric-of-orphan" : ""),
              title: m.of
                ? `Metric for product ${m.of}` +
                  (ofMissing ? " (ID not found in this reaction)" : "")
                : "Missing product identifier: every metric must specify of",
            },
            [
              h(
                "select",
                {
                  class: "role-tag",
                  "aria-label": `Metric role for ${m.of || `metric ${i + 1}`}`,
                  style:
                    `background:transparent;border:none;color:${roleColor};font-size:9px;text-transform:uppercase;cursor:pointer;font-weight:600;letter-spacing:0.4px;`,
                  value: m.role,
                  onChange: (e) =>
                    setAtPath([...props.path, i, "role"], e.target.value),
                },
                METRIC_ROLES.map((r) =>
                  h("option", { value: r, selected: r === m.role }, r)
                )
              ),
              h(Editable, {
                value: m.text || "",
                placeholder: "(value)",
                onSave: (v) => setAtPath([...props.path, i, "text"], v),
              }),
              // ── of <select> (editable) ─────────────────────
              h(
                "select",
                {
                  class: "metric-of-select",
                  title: "Product identifier (required)",
                  value: ofVal,
                  onChange: (e) => {
                    const v = e.target.value;
                    if (v === "") {
                      // Clearing: drop the key entirely (rather than store empty string)
                      const next = [...items];
                      const cur = { ...next[i] };
                      delete cur.of;
                      next[i] = cur;
                      setAtPath(props.path, next);
                    } else {
                      setAtPath([...props.path, i, "of"], v);
                    }
                  },
                },
                [
                  h("option", { value: "" }, "(no product)"),
                  ...productOpts.map((pid) =>
                    h(
                      "option",
                      { value: pid, selected: pid === ofVal },
                      pid
                    )
                  ),
                  // If `of` points to a non-existent product, keep the
                  // value selectable so we don't silently lose the data.
                  ofMissing
                    ? h(
                        "option",
                        {
                          value: ofVal,
                          selected: true,
                          disabled: true,
                        },
                        `${ofVal} (unlinked)`
                      )
                    : null,
                ]
              ),
              h(
                "button",
                {
                  class: "delete-btn",
                  style: "display:inline;",
                  onClick: () => {
                    const next = [...items];
                    next.splice(i, 1);
                    setAtPath(props.path, next);
                  },
                },
                "×"
              ),
            ]
          );
        }),
        h(
          "button",
          {
            class: "add-cond-btn",
            onClick: () => {
              // New metric defaults to first product if any (so the
              // mandatory `of` field starts populated).
              const newMetric = {
                role: "yield",
                text: "",
                note: "",
              };
              if (productOpts.length > 0) {
                newMetric.of = productOpts[0];
              }
              setAtPath(props.path, [...items, newMetric]);
            },
          },
          "+ metric"
        ),
      ]);
    };
  },
};

// ── Markush enumeration (key→value, edit/add/delete) ───────
const MarkushEnum = {
  props: {
    data: { type: Object, default: () => ({}) },
    path: { type: Array, required: true },
  },
  setup(props) {
    return () => {
      const entries = Object.entries(props.data || {});
      if (!entries.length && !props.path) return null;
      return h(
        "div",
        {
          style:
            "padding:8px 0 4px;margin-top:8px;border-top:1px dashed var(--border-soft);display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--fg-muted);",
        },
        [
          h(
            "span",
            {
              style:
                "font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:0.5px;",
            },
            "Markush:"
          ),
          ...entries.map(([k, v]) =>
            h("span", { key: k, class: "cond-pill", style: "background:var(--bg-sunken);" }, [
              h(
                "span",
                { style: "font-family:var(--mono);color:var(--accent);font-weight:600;" },
                `${k}=`
              ),
              h(Editable, {
                value: String(v ?? ""),
                placeholder: "?",
                onSave: (val) => setAtPath([...props.path, k], val),
              }),
              h(
                "button",
                {
                  class: "delete-btn",
                  onClick: () => {
                    const next = { ...props.data };
                    delete next[k];
                    setAtPath(props.path, next);
                  },
                },
                "×"
              ),
            ])
          ),
          h(
            "button",
            {
              class: "add-cond-btn",
              onClick: () => {
                const k = prompt("New variable name (e.g. R, Ar, R1):");
                if (!k || !k.trim()) return;
                const next = { ...(props.data || {}), [k.trim()]: "" };
                setAtPath(props.path, next);
              },
            },
            "+ Variable"
          ),
        ]
      );
    };
  },
};
