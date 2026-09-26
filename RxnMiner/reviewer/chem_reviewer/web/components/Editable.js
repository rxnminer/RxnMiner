// Inline-editable text. Renders a clickable span; on click becomes contenteditable.
// Saves on blur or Enter (Shift+Enter for newline if multiline).
//
// Props:
//   value:      string                — current value
//   placeholder: string                — shown when empty (italic gray)
//   multiline:  boolean                — allow Enter to insert newline
//   classes:    string                — extra css classes
//   onSave(val): callback             — called on commit (blur / Enter)
import { h, ref, watch, nextTick } from "vue";

export const Editable = {
  props: {
    value: { type: String, default: "" },
    placeholder: { type: String, default: "(click to edit)" },
    multiline: { type: Boolean, default: false },
    classes: { type: String, default: "" },
    showPlaceholder: { type: Boolean, default: true },
  },
  emits: ["save"],
  setup(props, { emit }) {
    const editing = ref(false);
    const draft = ref(props.value || "");
    const elRef = ref(null);

    watch(
      () => props.value,
      (v) => {
        if (!editing.value) draft.value = v || "";
      }
    );

    const enter = async () => {
      if (editing.value) return;
      editing.value = true;
      draft.value = props.value || "";
      await nextTick();
      const el = elRef.value;
      if (el) {
        el.focus();
        // place caret at end
        const range = document.createRange();
        range.selectNodeContents(el);
        range.collapse(false);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      }
    };

    const commit = () => {
      if (!editing.value) return;
      editing.value = false;
      const newVal = (elRef.value?.innerText || "").replace(/\u00a0/g, " ");
      // Trim only trailing newline injected by contenteditable
      const cleaned = newVal.replace(/\n+$/, "");
      if (cleaned !== (props.value || "")) {
        emit("save", cleaned);
      }
    };

    const cancel = () => {
      editing.value = false;
      if (elRef.value) elRef.value.innerText = props.value || "";
    };

    const onKeydown = (e) => {
      if (e.key === "Enter" && !(props.multiline && e.shiftKey)) {
        e.preventDefault();
        commit();
      } else if (e.key === "Escape") {
        e.preventDefault();
        cancel();
      }
    };

    return () => {
      const isEmpty = !(props.value || "").trim();
      const showPh = isEmpty && props.showPlaceholder && !editing.value;
      const cls = (props.classes || "") + (showPh ? " placeholder" : "");
      return h(
        "span",
        {
          ref: elRef,
          class: "editable " + cls,
          contenteditable: editing.value ? "true" : "false",
          spellcheck: false,
          style: showPh
            ? "color:var(--fg-dim);font-style:italic;cursor:text;"
            : "cursor:text;",
          onClick: enter,
          onBlur: commit,
          onKeydown,
          // Show the value (not placeholder) so contenteditable text matches what's saved
          innerText: editing.value
            ? props.value || ""
            : showPh
            ? props.placeholder
            : props.value || "",
        }
      );
    };
  },
};
