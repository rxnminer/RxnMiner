import { h, computed } from "vue";
import { store } from "/static/lib/store.js";

export const PaperList = {
  emits: ["select"],
  setup(_, { emit }) {
    const toggleCollapse = () => {
      store.paperListCollapsed = !store.paperListCollapsed;
    };
    return () => {
      const items = store.papers || [];
      const collapsed = store.paperListCollapsed;

      // Collapsed view: thin vertical rail with just the expand button.
      if (collapsed) {
        return h(
          "aside",
          { class: "col col-collapsed paper-list-collapsed" },
          [
            h(
              "button",
              {
                class: "col-collapse-btn expand",
                onClick: toggleCollapse,
                title: "Expand paper list",
              },
              "📚"
            ),
            h(
              "div",
              { class: "col-collapsed-label" },
              `${items.length} papers`
            ),
          ]
        );
      }

      return h("aside", { class: "col" }, [
        h("div", { class: "col-header" }, [
          h("h2", null, [
            "📚 Papers ",
            h(
              "span",
              { style: "color:var(--fg-dim);font-weight:400;" },
              ` ${items.length}`
            ),
          ]),
          h(
            "button",
            {
              class: "col-collapse-btn",
              onClick: toggleCollapse,
              title: "Collapse paper list",
            },
            "◂"
          ),
        ]),
        items.length === 0
          ? h("div", { class: "loading" }, "Loading…")
          : items.map((p) =>
              h(
                "div",
                {
                  class:
                    "paper-item" +
                    (store.currentSlug === p.slug ? " active" : ""),
                  onClick: () => emit("select", p.slug),
                  key: p.slug,
                },
                [
                  h("div", { class: "title" }, p.title || p.slug),
                  p.doi
                    ? h(
                        "div",
                        {
                          style:
                            "font-size:10px;color:var(--fg-dim);font-family:var(--mono);margin-top:2px;",
                        },
                        p.doi
                      )
                    : null,
                  h("div", { class: "meta" }, [
                    h(
                      "span",
                      { class: "badge" },
                      `${p.page_count || 0} pages · ${p.n_tasks} tasks`
                    ),
                    p.n_multistep > 0
                      ? h(
                          "span",
                          {
                            class: "badge multistep",
                            title: `${p.n_multistep} multistep figures`,
                          },
                          `🔗 Multistep×${p.n_multistep}`
                        )
                      : null,
                    p.n_failed > 0
                      ? h(
                          "span",
                          {
                            class: "badge failed",
                            title: `${p.n_failed} failed or control entries`,
                          },
                          `✗ Failed×${p.n_failed}`
                        )
                      : null,
                    p.n_reviewed > 0
                      ? h(
                          "span",
                          { class: "badge reviewed" },
                          `✓ ${p.n_reviewed}/${p.n_tasks}`
                        )
                      : null,
                  ]),
                ]
              )
            ),
      ]);
    };
  },
};
