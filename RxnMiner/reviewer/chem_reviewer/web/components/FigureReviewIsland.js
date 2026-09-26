import { computed, createApp, h, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { api } from "/static/lib/api.js";
import { store } from "/static/lib/store.js";
import { TaskView } from "/static/components/TaskView.js?v=20260801e";
import { dispatchReviewOutcome } from "/static/lib/review-outcome.js?v=20260727a";
import {
  bboxKey,
  reactionBboxes,
} from "/static/lib/reaction-link.js";
import {
  clearLatestWorkspaceCommand,
  resolveFigureReactionKey,
  subscribeWorkspaceCommands,
} from "/static/lib/workspace-command.js";

export const FigureReviewIsland = {
  name: "FigureReviewIsland",
  props: {
    jobId: { type: String, required: true },
    slug: { type: String, required: true },
    tid: { type: String, required: true },
    restoreSelection: { type: Object, default: null },
    onSelectionChange: { type: Function, default: null },
  },
  setup(props) {
    const error = ref("");
    const exportError = ref("");
    const workspaceNotice = ref("");
    const toast = computed(() => store.toast);
    let unsubscribeWorkspaceCommands = () => {};
    let pendingWorkspaceCommand = null;

    const downloadExport = async (path, fallback) => {
      exportError.value = "";
      try {
        const response = await fetch(path);
        if (!response.ok) {
          let message = "";
          try {
            const body = await response.json();
            message = body?.detail || body?.error || "";
          } catch {}
          throw new Error(message || `HTTP ${response.status}`);
        }
        const disposition = response.headers.get("content-disposition") || "";
        const filename = disposition.match(/filename="([^"]+)"/i)?.[1] || fallback;
        const url = URL.createObjectURL(await response.blob());
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        link.click();
        URL.revokeObjectURL(url);
      } catch (downloadError) {
        exportError.value = downloadError?.message || "Export failed.";
      }
    };

    const rawExport = (format, extension) => downloadExport(
      `/api/jobs/${encodeURIComponent(props.jobId)}/export?fmt=${encodeURIComponent(format)}`,
      `${props.jobId}_${extension}`,
    );

    const reviewedExport = () => downloadExport(
      `/api/jobs/${encodeURIComponent(props.jobId)}/review/export`,
      `${props.jobId}_reviewed_figure.json`,
    );

    const reactionForKey = (key) => {
      const match = String(key || "").match(/^(template|example):(\d+)$/);
      if (!match) return null;
      const rows = match[1] === "template"
        ? store.taskData?.reaction_templates : store.taskData?.selected_examples;
      return Array.isArray(rows) ? rows[Number(match[2])] || null : null;
    };

    const scrollToBbox = async (key) => {
      await nextTick();
      const box = [...document.querySelectorAll(".figure-review-island [data-box-key]")]
        .find((element) => element.dataset.boxKey === key);
      box?.scrollIntoView({ block: "center", inline: "center", behavior: "smooth" });
    };

    const scrollToReaction = async (key, module, selectDefaultBbox = true) => {
      await nextTick();
      const row = [...document.querySelectorAll(".figure-review-island [data-reaction-key]")]
        .find((element) => element.dataset.reactionKey === key);
      row?.scrollIntoView({ block: "center", behavior: "smooth" });
      if (module === "evidence" && selectDefaultBbox) {
        row?.querySelector(".reaction-bbox-link")?.click();
      }
      if (module === "edit") {
        row?.querySelector("input,textarea,select,[contenteditable=true],button")?.focus();
      }
    };

    const applyWorkspaceCommand = async (command) => {
      if (command.jobId && command.jobId !== props.jobId) return;
      if (!store.taskData) {
        pendingWorkspaceCommand = command;
        return;
      }
      if (command.type === "review.propose") {
        if (command.target !== "figure") {
          dispatchReviewOutcome({
            proposalId: command.proposalId,
            jobId: command.jobId,
            target: command.target,
            status: "rejected",
            reason: "workspace_target_mismatch",
          });
          clearLatestWorkspaceCommand(props.jobId, "review.propose");
          workspaceNotice.value = "This correction proposal targets a different workspace.";
          return;
        }
        const key = resolveFigureReactionKey(store.taskData, command.reactionId);
        if (!key) {
          dispatchReviewOutcome({
            proposalId: command.proposalId,
            jobId: command.jobId,
            target: command.target,
            status: "rejected",
            reason: "reaction_not_found",
          });
          clearLatestWorkspaceCommand(props.jobId, "review.propose");
          workspaceNotice.value = `Reaction ${command.reactionId} is not present in this Figure result.`;
          return;
        }
        store.activeReactionKey = key;
        workspaceNotice.value = `Pi Agent proposed a correction for ${command.reactionId}; not applied.`;
        await scrollToReaction(key, "edit");
        store.pendingAgentReviewProposal = command;
        return;
      }
      if (command.module === "overview") {
        store.activeReactionKey = null;
        store.activeBboxKey = null;
        store.activeEvidenceFocus = null;
        workspaceNotice.value = "Figure overview opened.";
        await nextTick();
        document.querySelector(".figure-review-island")?.scrollTo({ top: 0, behavior: "smooth" });
        return;
      }
      if (!["reactions", "evidence", "edit"].includes(command.module)) {
        workspaceNotice.value = `${command.module} is not available for this Figure workspace.`;
        return;
      }
      const key = resolveFigureReactionKey(store.taskData, command.reactionId);
      if (command.reactionId && !key) {
        workspaceNotice.value = `Reaction ${command.reactionId} is not present in this Figure result.`;
        return;
      }
      if (command.bboxKey && !store.molparserDetectionsLoaded) {
        pendingWorkspaceCommand = command;
        workspaceNotice.value = "Loading Figure evidence boxes…";
        return;
      }
      if (command.bboxKey) {
        const availableKeys = new Set(
          (store.molparserDetections || []).map((detection) => bboxKey(detection)),
        );
        if (!availableKeys.has(command.bboxKey)) {
          store.activeReactionKey = null;
          store.activeBboxKey = null;
          workspaceNotice.value = `BBox ${command.bboxKey} is not present in this Figure evidence.`;
          return;
        }
        if (key) {
          const linkedKeys = new Set(
            reactionBboxes(reactionForKey(key)).map((detection) => bboxKey(detection)),
          );
          if (!linkedKeys.has(command.bboxKey)) {
            store.activeReactionKey = null;
            store.activeBboxKey = null;
            workspaceNotice.value =
              `BBox ${command.bboxKey} is not linked to reaction ${command.reactionId}.`;
            return;
          }
        }
      }
      store.activeEvidenceFocus = command.focus || null;
      if (key) {
        store.activeReactionKey = key;
        store.activeBboxKey = command.bboxKey || null;
        store.activeBboxCycle = 0;
        workspaceNotice.value = `${command.module} opened for ${command.reactionId}.`;
        await scrollToReaction(key, command.module, !command.bboxKey);
      } else {
        if (command.bboxKey) {
          store.activeReactionKey = null;
          store.activeBboxKey = command.bboxKey;
          store.activeBboxCycle = 0;
          workspaceNotice.value = `BBox evidence ${command.bboxKey} opened.`;
          await scrollToBbox(command.bboxKey);
          return;
        } else {
          workspaceNotice.value = "Reaction workspace opened.";
        }
        await nextTick();
        document.querySelector(".figure-review-island .task-body")
          ?.scrollIntoView({ block: "start", behavior: "smooth" });
      }
    };

    onMounted(async () => {
      unsubscribeWorkspaceCommands = subscribeWorkspaceCommands(
        (command) => { void applyWorkspaceCommand(command); },
        window,
        { jobId: props.jobId },
      );
      store.loading = true;
      store.currentSlug = props.slug;
      store.currentTid = props.tid;
      store.taskData = null;
      store.taskOriginal = null;
      store.reviewedVersion = null;
      store.reviewedVersions = [];
      store.pendingAgentReviewProposal = null;
      store.activeReactionKey = null;
      store.activeBboxKey = null;
      store.activeBboxCycle = 0;
      store.molparserDetections = [];
      store.molparserDetectionsLoaded = false;
      store.activeEvidenceFocus = null;
      store.dirty = false;
      try {
        const [task, manifest] = await Promise.all([
          api.task(props.slug, props.tid),
          api.manifest(props.slug).catch(() => null),
        ]);
        store.manifest = manifest;
        store.taskData = task.data;
        store.taskOriginal = task.original_data;
        store.isReviewed = task.is_reviewed;
        store.reviewedAt = task.reviewed_at;
        store.reviewedVersion = task.reviewed_version || null;
        store.reviewedVersions = task.reviewed_versions || [];
        store.taskMeta = task.task_meta || {};
        store.activeReactionKey = props.restoreSelection?.reactionKey || null;
        store.activeBboxKey = props.restoreSelection?.bboxKey || null;
        store.activeBboxCycle = Number.isSafeInteger(props.restoreSelection?.bboxCycle)
          ? props.restoreSelection.bboxCycle : 0;
        if (pendingWorkspaceCommand) {
          const command = pendingWorkspaceCommand;
          pendingWorkspaceCommand = null;
          await applyWorkspaceCommand(command);
        }
      } catch (loadError) {
        error.value = loadError?.message || "Figure review task could not be loaded.";
      } finally {
        store.loading = false;
      }
    });
    watch(
      () => [store.activeReactionKey, store.activeBboxKey, store.activeBboxCycle],
      ([reactionKey, bboxKey, bboxCycle]) => props.onSelectionChange?.({
        reactionKey,
        bboxKey,
        bboxCycle,
      }),
    );
    watch(
      () => store.molparserDetectionsLoaded,
      (loaded) => {
        if (!loaded || !pendingWorkspaceCommand || !store.taskData) return;
        const command = pendingWorkspaceCommand;
        pendingWorkspaceCommand = null;
        void applyWorkspaceCommand(command);
      },
    );
    onUnmounted(() => {
      unsubscribeWorkspaceCommands();
      store.taskData = null;
      store.taskOriginal = null;
      store.reviewedVersion = null;
      store.reviewedVersions = [];
      store.pendingAgentReviewProposal = null;
      store.currentSlug = null;
      store.currentTid = null;
      store.molparserDetectionsLoaded = false;
      store.activeEvidenceFocus = null;
    });
    return () => error.value
      ? h("div", { class: "review-load-error", role: "alert" }, error.value)
      : store.taskData
        ? h("div", { class: "figure-review-island" }, [
          workspaceNotice.value ? h("div", {
            class: "figure-workspace-command-status",
            role: "status",
            "aria-live": "polite",
          }, workspaceNotice.value) : null,
          h("div", { class: "figure-workspace-toolbar" }, [
            h("span", null, store.isReviewed
              ? `Human-reviewed${store.reviewedVersion ? ` · v${store.reviewedVersion}` : ""}`
              : "Raw extraction · no reviewed changes yet"),
            h("details", { class: "figure-export-menu" }, [
              h("summary", null, "Export"),
              h("div", { role: "menu" }, [
                h("button", {
                  role: "menuitem",
                  disabled: !store.isReviewed,
                  onClick: reviewedExport,
                }, "Reviewed Figure · JSON"),
                h("button", {
                  role: "menuitem",
                  onClick: () => rawExport("v2", "raw_v2.json"),
                }, "Raw prediction · JSON"),
                h("button", {
                  role: "menuitem",
                  onClick: () => rawExport("csv", "raw_reactions.csv"),
                }, "Raw reactions · CSV"),
                h("button", {
                  role: "menuitem",
                  onClick: () => rawExport("rxn", "raw_rxn.zip"),
                }, "Raw reactions · RXN"),
                h("button", {
                  role: "menuitem",
                  onClick: () => rawExport("report", "raw_report.json"),
                }, "Raw extraction report"),
              ]),
            ]),
          ]),
          exportError.value ? h("div", {
            class: "figure-export-error",
            role: "alert",
          }, exportError.value) : null,
          h(TaskView),
          toast.value ? h("div", {
            class: `toast ${toast.value.kind}`,
            role: toast.value.kind === "error" ? "alert" : "status",
            "aria-live": toast.value.kind === "error" ? "assertive" : "polite",
          }, toast.value.text) : null,
        ])
        : h("div", { class: "figure-review-loading", role: "status" }, "Loading chemistry workspace…");
  },
};

export function mountFigureReviewIsland(element, props) {
  const previousApiBase = globalThis.CHEMRXN_REVIEWER_API_BASE;
  globalThis.CHEMRXN_REVIEWER_API_BASE = "/api/reviewer";
  const app = createApp(FigureReviewIsland, props);
  app.mount(element);
  return () => {
    app.unmount();
    if (previousApiBase === undefined) delete globalThis.CHEMRXN_REVIEWER_API_BASE;
    else globalThis.CHEMRXN_REVIEWER_API_BASE = previousApiBase;
  };
}
