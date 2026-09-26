import { h, computed } from "vue";
import { store } from "/static/lib/store.js";

// Human label for a task: prefer v2 anchor_figure ("Scheme 4"), fallback to ID.
function taskLabel(t, tid) {
  return t.anchor_figure || t.source_caption || `Figure ${tid}`;
}

// Pull a Chinese label for synthesis_overview.type (used as small chip).
const SYN_TYPE_ZH = {
  linear: "Linear",
  fan_out_derivatization: "Derivatization",
  mechanistic_branched: "Mechanistic",
  linear_with_failures: "Includes failures",
  convergent: "Convergent",
};

export const TaskNav = {
  emits: ["select"],
  setup(_, { emit }) {
    const toggleCollapse = () => {
      store.taskNavCollapsed = !store.taskNavCollapsed;
    };
    return () => {
      const m = store.manifest;
      const collapsed = store.taskNavCollapsed;

      // Collapsed: thin vertical rail with expand button.
      if (collapsed) {
        return h(
          "aside",
          { class: "col col-collapsed task-nav-collapsed" },
          [
            h(
              "button",
              {
                class: "col-collapse-btn expand",
                onClick: toggleCollapse,
                title: "Expand task list",
              },
              "🧪"
            ),
            h(
              "div",
              { class: "col-collapsed-label" },
              `${Object.keys(m?.tasks || {}).length} Tasks`
            ),
          ]
        );
      }

      if (!store.currentSlug) {
        return h("aside", { class: "col" }, [
          h("div", { class: "col-header" }, [
            h("h2", null, "🧪 Tasks"),
            h(
              "button",
              {
                class: "col-collapse-btn",
                onClick: toggleCollapse,
                title: "Collapse task list",
              },
              "◂"
            ),
          ]),
          h("div", { class: "empty-hint" }, "Select a paper"),
        ]);
      }
      if (!m) {
        return h("aside", { class: "col" }, [
          h("div", { class: "col-header" }, [
            h("h2", null, "🧪 Tasks"),
            h(
              "button",
              {
                class: "col-collapse-btn",
                onClick: toggleCollapse,
                title: "Collapse task list",
              },
              "◂"
            ),
          ]),
          h("div", { class: "loading" }, "Loading manifest…"),
        ]);
      }
      const tasks = m.tasks || {};
      // Sort by anchor_page first, then by numeric task_id within page.
      const tids = Object.keys(tasks).sort((a, b) => {
        const ta = tasks[a];
        const tb = tasks[b];
        const pa = ta.anchor_page ?? 999;
        const pb = tb.anchor_page ?? 999;
        if (pa !== pb) return pa - pb;
        const na = parseInt(a.replace(/^T/, ""), 10);
        const nb = parseInt(b.replace(/^T/, ""), 10);
        if (!isNaN(na) && !isNaN(nb)) return na - nb;
        return a.localeCompare(b);
      });
      return h("aside", { class: "col" }, [
        h("div", { class: "col-header" }, [
          h("h2", null, [
            "🧪 Tasks ",
            h(
              "span",
              { style: "color:var(--fg-dim);font-weight:400;" },
              ` ${tids.length}`
            ),
          ]),
          h(
            "button",
            {
              class: "col-collapse-btn",
              onClick: toggleCollapse,
              title: "Collapse task list",
            },
            "◂"
          ),
        ]),
        ...tids.map((tid) => {
          const t = tasks[tid];
          const dropped = Boolean(t.is_dropped);
          const isMs = Boolean(t.is_multistep_synthesis);
          const synType = t.synthesis_type;
          const nFailed = t.n_failed || 0;
          const synZh = synType ? (SYN_TYPE_ZH[synType] || synType) : "";
          return h(
            "div",
            {
              class:
                "task-item" +
                (store.currentTid === tid ? " active" : "") +
                (dropped ? " dropped" : "") +
                (isMs ? " is-multistep" : "") +
                (nFailed > 0 ? " has-failed" : ""),
              onClick: () => emit("select", tid),
              key: tid,
            },
            [
              h("div", { class: "top" }, [
                h("span", { class: "tid" }, `#${tid}`),
                isMs
                  ? h(
                      "span",
                      {
                        class: "task-chip multistep",
                        title: `Multistep synthesis (${synType})`,
                      },
                      `🔗 ${synZh}`
                    )
                  : null,
                nFailed > 0
                  ? h(
                      "span",
                      {
                        class: "task-chip failed",
                        title: `${nFailed} failed or control entries`,
                      },
                      `✗ ${nFailed}`
                    )
                  : null,
                dropped
                  ? h("span", { class: "dropped-mark" }, "Non-reaction")
                  : t.is_reviewed
                  ? h("span", { class: "reviewed-mark" }, "✓ Reviewed")
                  : null,
              ]),
              h("div", { class: "caption" }, taskLabel(t, tid)),
              h(
                "div",
                { class: "stats" },
                [
                  `p${t.anchor_page ?? "?"} · ${t.n_templates ?? 0} Template · ${t.n_examples ?? 0} examples`,
                  t.n_dag_edges
                    ? h(
                        "span",
                        {
                          style: "color:var(--accent);margin-left:4px;",
                        },
                        ` · ${t.n_dag_edges} DAG edges`
                      )
                    : null,
                ]
              ),
            ]
          );
        }),
      ]);
    };
  },
};
