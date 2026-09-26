// chem_reviewer top-level Vue app.

import { createApp, h, computed, onMounted } from "vue";
import { api } from "/static/lib/api.js";
import {
  store,
  showToast,
  PAPER_LIST_COLLAPSED_W,
} from "/static/lib/store.js";

import { PaperList } from "/static/components/PaperList.js";
import { TaskNav } from "/static/components/TaskNav.js";
import { TaskView } from "/static/components/TaskView.js";

const EMBED_WORKSPACE =
  new URLSearchParams(location.search).get("embed") === "workspace";

// ── Hash router ────────────────────────────────────────────────────────
function parseHash() {
  const h = (location.hash || "").replace(/^#\/?/, "");
  if (!h) return { slug: null, tid: null };
  const parts = h.split("/").map(decodeURIComponent);
  return { slug: parts[0] || null, tid: parts[1] || null };
}

function navigate(slug, tid) {
  const enc = (s) => (s ? encodeURIComponent(s) : "");
  const next = `#/${enc(slug || "")}${tid ? "/" + enc(tid) : ""}`;
  if (location.hash !== next) location.hash = next;
}

async function syncFromHash() {
  const { slug, tid } = parseHash();

  // ── paper switch ─────────────────────────────────────────────
  if (slug !== store.currentSlug) {
    store.currentSlug = slug;
    store.manifest = null;
    store.currentTid = null;
    store.taskData = null;
    store.taskOriginal = null;
    store.reviewedVersion = null;
    store.reviewedVersions = [];
    store.reviewPointerCorrupt = false;
    store.activeReactionKey = null;
    store.activeBboxKey = null;
    store.activeBboxCycle = 0;
    store.molparserDetections = [];
    store.molparserDetectionsLoaded = false;
    store.activeEvidenceFocus = null;
    store.comparisonTaskData = null;
    store.comparisonLabel = "GT";
    store.dirty = false;
    if (slug) {
      try {
        store.manifest = await api.manifest(slug);
      } catch (e) {
        showToast("error", `manifest Load failed: ${e.message}`);
        return;
      }
    }
  }

  // ── task switch ──────────────────────────────────────────────
  if (tid !== store.currentTid) {
    if (store.dirty) {
      const ok = confirm("This task has unsaved changes. Discard them and switch?");
      if (!ok) {
        navigate(store.currentSlug, store.currentTid);
        return;
      }
    }
    store.currentTid = tid;
    store.taskData = null;
    store.taskOriginal = null;
    store.reviewedVersion = null;
    store.reviewedVersions = [];
    store.reviewPointerCorrupt = false;
    store.activeReactionKey = null;
    store.activeBboxKey = null;
    store.activeBboxCycle = 0;
    store.molparserDetections = [];
    store.molparserDetectionsLoaded = false;
    store.activeEvidenceFocus = null;
    store.comparisonTaskData = null;
    store.comparisonLabel = "GT";
    store.dirty = false;
    if (slug && tid) {
      store.loading = true;
      try {
        const r = await api.task(slug, tid);
        store.taskData = r.data;
        store.taskOriginal = r.original_data;
        store.isReviewed = r.is_reviewed;
        store.reviewedAt = r.reviewed_at;
        store.reviewedVersion = r.reviewed_version || null;
        store.reviewedVersions = r.reviewed_versions || [];
        store.reviewPointerCorrupt = Boolean(r.review_pointer_corrupt);
        store.taskMeta = r.task_meta || {};

        // Comparison data is loaded only through an explicit reviewer-only
        // pointer. Never guess a GT task from names: extraction/eval views must
        // remain sealed unless their imported artifact opts into human review.
        const reviewCfg = r.data?._reviewer || {};
        const comparisonTid = reviewCfg.comparison_task_id;
        if (comparisonTid && String(comparisonTid) !== String(tid)) {
          try {
            const comparison = await api.task(slug, comparisonTid);
            store.comparisonTaskData = comparison.data || null;
            store.comparisonLabel = reviewCfg.comparison_label || "GT";
          } catch (comparisonError) {
            showToast("error", `comparisonTasksLoad failed: ${comparisonError.message}`);
          }
        }
      } catch (e) {
        showToast("error", `task Load failed: ${e.message}`);
      } finally {
        store.loading = false;
      }
    }
  }
}

window.addEventListener("hashchange", syncFromHash);

// ── Root component ─────────────────────────────────────────────────────
const RootApp = {
  setup() {
    onMounted(async () => {
      store.loading = true;
      try {
        const r = await api.papers();
        store.papers = r.papers || [];
      } catch (e) {
        showToast("error", `Could not load the paper list: ${e.message}`);
      } finally {
        store.loading = false;
      }
      await syncFromHash();
    });

    const toast = computed(() => store.toast);

    return () => {
      // Column widths via CSS variables — collapsed cols become a thin rail.
      const paperW = store.paperListCollapsed
        ? `${PAPER_LIST_COLLAPSED_W}px`
        : "var(--col-paper-w)";
      const taskW = store.taskNavCollapsed
        ? `${PAPER_LIST_COLLAPSED_W}px`
        : "var(--col-task-w)";
      const styleVars = `--paper-w:${paperW}; --task-w:${taskW};`;
      return h("div", {
        class: `app-shell${EMBED_WORKSPACE ? " embed-workspace" : ""}`,
        style: styleVars,
      }, [
        EMBED_WORKSPACE ? null
          : h(PaperList, { onSelect: (slug) => navigate(slug, null) }),
        EMBED_WORKSPACE ? null
          : h(TaskNav, { onSelect: (tid) => navigate(store.currentSlug, tid) }),
        h(TaskView),
        toast.value &&
          h(
            "div",
            { class: `toast ${toast.value.kind}` },
            toast.value.text
          ),
      ]);
    };
  },
};

createApp(RootApp).mount("#app");
