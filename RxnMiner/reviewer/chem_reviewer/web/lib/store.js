// Tiny shared reactive store (Vue reactive). Holds:
//   - paper list, current selection
//   - currently loaded task (data + original_data)
//   - dirty flag (any edit since last save)
//   - toast messages
//   - layout state (paper-list collapsed, anchor column width, ...)
import { reactive, watch } from "vue";

// ── Layout persistence (localStorage) ──────────────────────
const LS_KEY = "chem_reviewer.layout.v1";

function _loadLayout() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (raw) return JSON.parse(raw);
  } catch (e) { /* ignore */ }
  return {};
}

function _saveLayout(state) {
  try {
    localStorage.setItem(
      LS_KEY,
      JSON.stringify({
        paperListCollapsed: state.paperListCollapsed,
        taskNavCollapsed: state.taskNavCollapsed,
        anchorWidth: state.anchorWidth,
      })
    );
  } catch (e) { /* ignore */ }
}

const _saved = _loadLayout();

export const store = reactive({
  papers: [],
  currentSlug: null,
  manifest: null,            // current paper's manifest (with is_reviewed per task)
  currentTid: null,
  taskData: null,            // mutable copy edited by UI
  taskOriginal: null,        // the on-disk N_predicted_output.json (untouched)
  isReviewed: false,
  reviewedAt: null,
  reviewedVersion: null,
  reviewedVersions: [],
  reviewPointerCorrupt: false,
  pendingAgentReviewProposal: null, // typed proposal; never an applied artifact
  taskMeta: null,            // {anchor_figure, anchor_page, is_multistep_synthesis, ...}
  dirty: false,
  saving: false,
  loading: false,
  toast: null,               // {kind: 'success'|'error', text: '...'}
  ketcherTarget: null,       // {pathArr, smiles} when modal is open

  // Figure bbox <-> reaction-row selection. These are transient UI fields;
  // they intentionally do not persist across tasks or browser sessions.
  activeReactionKey: null,   // "template:0" | "example:12"
  activeBboxKey: null,       // "idx:5" or coordinate fallback
  activeBboxCycle: 0,        // repeated bbox clicks cycle grouped scope rows
  molparserDetections: [],   // genuine raw records from <tid>_molparser.json
  molparserDetectionsLoaded: false,
  activeEvidenceFocus: null, // selected-only | current-anchor | all-occurrences
  comparisonTaskData: null,  // explicit human-review comparison task (usually GT)
  comparisonLabel: "GT",

  // ── Layout (persisted to localStorage) ──────────────────
  paperListCollapsed: Boolean(_saved.paperListCollapsed),
  taskNavCollapsed: Boolean(_saved.taskNavCollapsed),
  anchorWidth: Number(_saved.anchorWidth) || 420, // px, default 420

  // Anchor natural aspect ratio (informational; layout is now always side-by-side)
  anchorAspect: null,
});

// Persist layout state on change
watch(
  () => [store.paperListCollapsed, store.taskNavCollapsed, store.anchorWidth],
  () => _saveLayout(store)
);

// Layout constants
export const ASPECT_THRESHOLD = 0.85;     // legacy export, no longer drives layout
export const PAPER_LIST_COLLAPSED_W = 32; // px
export const ANCHOR_MIN_W = 220;          // px
export const ANCHOR_MAX_W_RATIO = 0.7;    // 70% of viewport at most

let _toastTimer = null;
export function showToast(kind, text, ms = 2500) {
  store.toast = { kind, text, ts: Date.now() };
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    store.toast = null;
  }, ms);
}

// Mark the loaded task as dirty (called by editing helpers below).
export function markDirty() {
  if (!store.dirty) store.dirty = true;
}

// Helper: mutate a path inside store.taskData and mark dirty.
//   setAtPath(['reaction_templates', 0, 'reactants', 1, 'E-SMILES'], 'CCO')
export function setAtPath(pathArr, value) {
  if (!store.taskData) return;
  let obj = store.taskData;
  for (let i = 0; i < pathArr.length - 1; i++) {
    const key = pathArr[i];
    if (obj[key] === undefined || obj[key] === null) {
      // Create container of the right type based on next key
      const next = pathArr[i + 1];
      obj[key] = typeof next === "number" ? [] : {};
    }
    obj = obj[key];
  }
  obj[pathArr[pathArr.length - 1]] = value;
  markDirty();
}

export function getAtPath(pathArr) {
  if (!store.taskData) return undefined;
  let obj = store.taskData;
  for (const k of pathArr) {
    if (obj == null) return undefined;
    obj = obj[k];
  }
  return obj;
}

// Beforeunload guard so chemists don't lose unsaved work
window.addEventListener("beforeunload", (e) => {
  if (store.dirty) {
    e.preventDefault();
    e.returnValue = "Unsaved changes will be lost. Leave?";
    return e.returnValue;
  }
});
