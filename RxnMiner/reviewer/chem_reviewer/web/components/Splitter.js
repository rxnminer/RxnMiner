// Splitter — vertical drag handle between the anchor PNG column and the
// reaction-rows column. Live-updates store.anchorWidth on drag, with min
// (220px) and max (70% viewport) clamps. State persists to localStorage
// via store.js's watch.
import { h, ref, onBeforeUnmount } from "vue";
import {
  store,
  ANCHOR_MIN_W,
  ANCHOR_MAX_W_RATIO,
} from "/static/lib/store.js";

export const Splitter = {
  setup() {
    const dragging = ref(false);

    const onMove = (e) => {
      // Pointer X relative to the .task-view's left edge.
      // The .task-view is itself the second-to-rightmost column of
      // .app-shell; we just need to know how much of it is anchor.
      // Simpler approach: anchor column is `var(--anchor-w)` from the
      // LEFT edge of task-view. We grab the task-view bounding rect.
      const tv = document.querySelector(".task-view.layout-side");
      if (!tv) return;
      const rect = tv.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const max = Math.floor(window.innerWidth * ANCHOR_MAX_W_RATIO);
      const w = Math.max(ANCHOR_MIN_W, Math.min(max, x));
      store.anchorWidth = w;
    };

    const onUp = () => {
      if (!dragging.value) return;
      dragging.value = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };

    const onDown = (e) => {
      e.preventDefault();
      dragging.value = true;
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    };

    const onDoubleClick = () => {
      // Reset to default width
      store.anchorWidth = 420;
    };

    onBeforeUnmount(() => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    });

    return () =>
      h("div", {
        class: "splitter" + (dragging.value ? " dragging" : ""),
        onMousedown: onDown,
        onDblclick: onDoubleClick,
        title: "Drag to resize the source panel (double-click to reset)",
      }, [h("div", { class: "splitter-grip" })]);
  },
};
