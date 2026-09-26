import { h, ref, onMounted, watch } from "vue";
import { store, setAtPath } from "/static/lib/store.js";
import {
  AUDIT_CAUSES,
  CAUSE_LABELS,
  adjudicatedMetric,
  buildAudit,
} from "/static/lib/audit.js";

const METRIC_CARDS = [
  ["rows", "Reaction completeness"],
  ["product_connectivity", "Product connectivity"],
  ["product_strict", "Product stereochemistry"],
  ["reactant_connectivity", "Reactant connectivity"],
  ["reactant_strict", "Reactant stereochemistry"],
  ["row_identifier", "Reaction identifier"],
  ["molecule_identifier", "Molecule identifier"],
  ["conditions", "Conditions"],
  ["yield", "Yield"],
  ["dr", "dr"],
  ["amount", "Amount"],
  ["description", "Description"],
  ["other_metrics", "Other metrics"],
  ["template_link", "Template link"],
];

function percent(correct, total) {
  if (!total) return "—";
  return `${(100 * correct / total).toFixed(1)}%`;
}

function effectiveAnnotation(issue) {
  const stored = store.taskData?._reviewer?.audit_annotations?.[issue.key] || {};
  return {
    cause: stored.cause || "unresolved",
    reason: stored.reason || issue.suggestedReason || "",
  };
}

function setAnnotation(issue, patch) {
  const current = store.taskData?._reviewer?.audit_annotations?.[issue.key] || {};
  setAtPath(
    ["_reviewer", "audit_annotations", issue.key],
    { ...current, ...patch },
  );
}

export const AuditPanel = {
  setup() {
    const loading = ref(false);
    const result = ref({ issues: [], metrics: {} });
    const expanded = ref(true);
    const showAll = ref(false);
    const causeFilter = ref("all");
    const fieldFilter = ref("all");
    const query = ref("");

    async function refresh() {
      if (!store.taskData || !store.comparisonTaskData) {
        result.value = { issues: [], metrics: {} };
        return;
      }
      loading.value = true;
      try {
        result.value = await buildAudit(
          store.taskData,
          store.comparisonTaskData,
          store.molparserDetections || [],
        );
      } finally {
        loading.value = false;
      }
    }

    function jumpTo(issue) {
      store.activeReactionKey = issue.rowKey;
      requestAnimationFrame(() => {
        document.getElementById(`reaction-${issue.rowKey.replace(":", "-")}`)
          ?.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    }

    onMounted(refresh);
    watch(
      () => [
        store.currentSlug,
        store.currentTid,
        store.comparisonTaskData,
        store.molparserDetections?.length || 0,
      ],
      () => refresh(),
    );

    return () => {
      if (!store.comparisonTaskData) return null;
      const issues = result.value.issues || [];
      const metrics = result.value.metrics || {};
      const annotations = store.taskData?._reviewer?.audit_annotations || {};
      const causeCounts = {};
      for (const issue of issues) {
        const cause = effectiveAnnotation(issue).cause;
        causeCounts[cause] = (causeCounts[cause] || 0) + 1;
      }
      const fields = [...new Set(issues.map((issue) => issue.field))].sort();
      const q = query.value.trim().toLowerCase();
      const filtered = issues.filter((issue) => {
        const annotation = effectiveAnnotation(issue);
        if (causeFilter.value !== "all" && annotation.cause !== causeFilter.value) return false;
        if (fieldFilter.value !== "all" && issue.field !== fieldFilter.value) return false;
        if (!q) return true;
        return [issue.rowId, issue.label, issue.pred, issue.gt, annotation.reason]
          .some((value) => String(value || "").toLowerCase().includes(q));
      });
      const visible = showAll.value ? filtered : filtered.slice(0, 40);

      return h("section", { class: "audit-panel" }, [
        h("div", { class: "audit-header" }, [
          h("div", null, [
            h("h2", null, "Reference field audit"),
            h("div", { class: "audit-subtitle" }, [
              `comparison ${store.comparisonLabel || "GT"} · ${issues.length} differences`,
              " · Raw agreement and human-adjusted agreement are reported separately",
            ]),
          ]),
          h("div", { class: "audit-header-actions" }, [
            h("button", { class: "secondary", disabled: loading.value, onClick: refresh }, loading.value ? "Calculating…" : "Recalculate"),
            h("button", { class: "secondary", onClick: () => { expanded.value = !expanded.value; } }, expanded.value ? "Collapse audit" : "Expand audit"),
          ]),
        ]),
        !expanded.value ? null : h("div", { class: "audit-content" }, [
          h("div", { class: "audit-metrics" }, METRIC_CARDS.map(([id, label]) => {
            const scored = adjudicatedMetric(metrics[id], issues, annotations);
            const changed = scored.adjustedCorrect !== scored.correct;
            return h("div", { class: `audit-metric-card${changed ? " adjusted" : ""}` }, [
              h("div", { class: "audit-metric-label" }, label),
              h("div", { class: "audit-metric-value" }, percent(scored.correct, scored.total)),
              h("div", { class: "audit-metric-count" }, `${scored.correct} correct correct · incorrect ${Math.max(0, scored.total - scored.correct)} · total ${scored.total}`),
              changed
                ? h("div", { class: "audit-adjusted" }, `Adjusted ${percent(scored.adjustedCorrect, scored.total)} · ${scored.adjustedCorrect}/${scored.total}`)
                : null,
            ]);
          })),
          h("div", { class: "audit-cause-summary" }, [
            h("b", null, "Difference attribution："),
            ...AUDIT_CAUSES.map(([value, label]) => causeCounts[value]
              ? h("button", {
                  class: `audit-cause-chip cause-${value}`,
                  onClick: () => { causeFilter.value = value; },
                }, `${label} ${causeCounts[value]}`)
              : null),
          ]),
          h("div", { class: "audit-note" }, "Suggested causes require human confirmation against source evidence, recognition, and tool traces. Adjusted accuracy is shown only after a reference error is confirmed."),
          h("div", { class: "audit-filters" }, [
            h("select", { value: causeFilter.value, onChange: (event) => { causeFilter.value = event.target.value; } }, [
              h("option", { value: "all" }, "All causes"),
              ...AUDIT_CAUSES.map(([value, label]) => h("option", { value }, `${label}${causeCounts[value] ? ` (${causeCounts[value]})` : ""}`)),
            ]),
            h("select", { value: fieldFilter.value, onChange: (event) => { fieldFilter.value = event.target.value; } }, [
              h("option", { value: "all" }, "All fields"),
              ...fields.map((field) => h("option", { value: field }, field)),
            ]),
            h("input", { value: query.value, placeholder: "Search identifier, yield, or condition…", onInput: (event) => { query.value = event.target.value; } }),
            h("span", { class: "audit-filter-count" }, `${filtered.length}/${issues.length}`),
          ]),
          h("div", { class: "audit-issue-list" }, visible.map((issue) => {
            const annotation = effectiveAnnotation(issue);
            return h("article", { class: `audit-issue cause-${annotation.cause}` }, [
              h("div", { class: "audit-issue-top" }, [
                h("button", { class: "audit-row-link", onClick: () => jumpTo(issue) }, issue.rowId),
                h("b", null, issue.label),
                issue.status === "stereo" ? h("span", { class: "audit-status stereo" }, "Connectivity matches") : null,
                issue.suggestedCause && issue.suggestedCause !== "unresolved"
                  ? h("span", { class: "audit-status suggestion" }, `Suggested cause：${CAUSE_LABELS[issue.suggestedCause] || issue.suggestedCause}`)
                  : null,
                h("select", {
                  class: "audit-cause-select",
                  value: annotation.cause,
                  onChange: (event) => setAnnotation(issue, { cause: event.target.value }),
                }, AUDIT_CAUSES.map(([value, label]) => h("option", { value }, label))),
              ]),
              h("div", { class: "audit-values" }, [
                h("div", null, [h("span", null, "Pi"), h("code", null, issue.pred)]),
                h("div", null, [h("span", null, "GT"), h("code", null, issue.gt)]),
                issue.raw
                  ? h("div", { class: "audit-raw-value" }, [
                      h("span", null, `Raw MP${issue.rawEqualsPred ? " = Pi" : issue.rawEqualsGt ? " = GT" : ""}`),
                      h("code", null, issue.raw),
                    ])
                  : null,
              ]),
              h("input", {
                class: "audit-reason-input",
                value: annotation.reason,
                placeholder: "Describe the evidence and cause…",
                onChange: (event) => setAnnotation(issue, { reason: event.target.value }),
              }),
            ]);
          })),
          filtered.length > 40
            ? h("button", { class: "audit-show-all", onClick: () => { showAll.value = !showAll.value; } }, showAll.value ? "Show first 40" : `Show all ${filtered.length} items`)
            : null,
        ]),
      ]);
    };
  },
};
