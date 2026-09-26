import { h } from "vue";
import { store, setAtPath } from "/static/lib/store.js";
import { Editable } from "/static/components/Editable.js";

// Per CLAUDE.md schema: text role is one of {title, caption, footnote, others}.
// We let chemists pick from a select but also allow free typing via a custom
// option (we add the current value if it's not in the enum).
const TEXT_ROLES = ["title", "caption", "footnote", "others"];

const TEXT_ROLE_ZH = {
  title: "Title",
  caption: "Caption",
  footnote: "Footnote",
  others: "Other",
};

export const TextsPanel = {
  setup() {
    const addText = () => {
      const cur = store.taskData?.texts || [];
      setAtPath(
        ["texts"],
        [...cur, { role: "footnote", text: "", note: "" }]
      );
    };

    const removeText = (i) => {
      const cur = store.taskData?.texts || [];
      const item = cur[i];
      const ok = confirm(
        `Delete this text?\n  role: ${item?.role || "?"}\n  text: ${
          (item?.text || "").slice(0, 60) || "(empty)"
        }`
      );
      if (!ok) return;
      const next = [...cur];
      next.splice(i, 1);
      setAtPath(["texts"], next);
    };

    return () => {
      const texts = store.taskData?.texts || [];
      return h("section", { class: "texts-panel" }, [
        h("h3", null, [
          "📝 Text / titles / notes ",
          h(
            "span",
            {
              style:
                "font-family:var(--mono);font-size:11px;background:var(--bg-sunken);padding:2px 7px;border-radius:10px;color:var(--fg-muted);font-weight:400;margin-left:4px;",
            },
            String(texts.length)
          ),
        ]),
        ...texts.map((t, i) =>
          h("div", { class: "text-item", key: i }, [
            h(RoleSelect, {
              value: t.role || "others",
              onChange: (v) => setAtPath(["texts", i, "role"], v),
            }),
            // Body container
            h("div", { class: "text-body" }, [
              h("div", null, [
                h(Editable, {
                  value: t.text || "",
                  onSave: (val) => setAtPath(["texts", i, "text"], val),
                  placeholder: "(text, caption, or footnote)",
                  multiline: true,
                }),
              ]),
              h(
                "div",
                {
                  style:
                    "color:var(--fg-muted);font-size:11px;margin-top:4px;display:flex;gap:6px;align-items:center;",
                },
                [
                  h(
                    "span",
                    {
                      style:
                        "font-family:var(--mono);text-transform:uppercase;letter-spacing:0.4px;font-size:9px;color:var(--fg-dim);",
                    },
                    "note"
                  ),
                  h(Editable, {
                    value: t.note || "",
                    onSave: (val) => setAtPath(["texts", i, "note"], val),
                    placeholder: "(optional note or footnote reference)",
                  }),
                ]
              ),
            ]),
            // Delete this text item
            h(
              "button",
              {
                class: "icon danger",
                style: "margin-left:auto;flex-shrink:0;font-size:11px;",
                onClick: () => removeText(i),
                title: "Delete this text",
              },
              "× Delete"
            ),
          ])
        ),
        // Add a new text
        h(
          "div",
          { style: "margin-top:14px;text-align:center;" },
          [
            h(
              "button",
              {
                class: "add-cond-btn",
                style: "padding:8px 18px;font-size:12px;",
                onClick: addText,
              },
              "＋ Add text, title, caption, or footnote"
            ),
          ]
        ),
      ]);
    };
  },
};

// ── Role select (with custom-value support) ─────────────────────
const RoleSelect = {
  props: {
    value: { type: String, default: "others" },
  },
  emits: ["change"],
  setup(props, { emit }) {
    return () => {
      // If current value isn't in our enum, surface it so user doesn't lose it
      const opts = TEXT_ROLES.includes(props.value)
        ? TEXT_ROLES
        : [...TEXT_ROLES, props.value];
      return h(
        "select",
        {
          class: "role-tag",
          style:
            "background:var(--bg-sunken);border:1px solid var(--border);color:var(--fg-muted);font-size:10px;text-transform:uppercase;cursor:pointer;letter-spacing:0.4px;padding:3px 6px;border-radius:3px;flex-shrink:0;",
          value: props.value,
          onChange: (e) => emit("change", e.target.value),
          title: "Change role",
        },
        opts.map((r) =>
          h(
            "option",
            { value: r, selected: r === props.value },
            TEXT_ROLE_ZH[r] || r
          )
        )
      );
    };
  },
};
