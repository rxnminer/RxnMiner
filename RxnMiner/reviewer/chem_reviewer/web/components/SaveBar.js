import { h, computed, ref, watch } from "vue";
import { api } from "/static/lib/api.js";
import { store, showToast } from "/static/lib/store.js";
import { DiffModal } from "/static/components/DiffModal.js";
import { stageFigureReviewProposal } from "/static/lib/review-proposal.js";
import { clearLatestWorkspaceCommand } from "/static/lib/workspace-command.js";
import { dispatchReviewOutcome } from "/static/lib/review-outcome.js?v=20260727a";

export const SaveBar = {
  setup() {
    const saving = ref(false);
    const proposing = ref(false);
    const justSaved = ref(false);
    const showDiff = ref(false);
    const proposal = ref(null);
    const proposedTaskData = ref(null);
    const agentProposalMeta = ref(null);
    let proposalRun = 0;
    let pendingAgentCommand = null;
    const reportAgentOutcome = (meta, status, extra = {}) => {
      if (!meta?.proposalId || !meta?.jobId) return false;
      try {
        dispatchReviewOutcome({
          proposalId: meta.proposalId,
          jobId: meta.jobId,
          target: "figure",
          status,
          ...extra,
        });
        return true;
      } catch (error) {
        console.warn("Figure review outcome was not dispatched", error);
        return false;
      }
    };

    const hydrateReviewState = (response) => {
      store.taskData = response.data;
      store.taskOriginal = response.original_data;
      store.isReviewed = response.is_reviewed;
      store.reviewedAt = response.reviewed_at;
      store.reviewedVersion = response.reviewed_version || null;
      store.reviewedVersions = response.reviewed_versions || [];
      store.reviewPointerCorrupt = Boolean(response.review_pointer_corrupt);
    };

    const breadcrumb = computed(() => {
      const meta = store.taskMeta || {};
      const cap = meta.anchor_figure || meta.source_caption || "";
      const sub = meta.source_subpanel || "";
      return [
        store.currentSlug,
        `#${store.currentTid}`,
        cap + (sub ? ` (${sub})` : ""),
      ].filter(Boolean);
    });

    const reviewChanges = async () => {
      if (!store.currentSlug || !store.currentTid || !store.dirty) return;
      proposing.value = true;
      const run = ++proposalRun;
      proposedTaskData.value = null;
      agentProposalMeta.value = null;
      try {
        const response = await api.previewTask(
          store.currentSlug,
          store.currentTid,
          stripMeta(store.taskData)
        );
        if (run !== proposalRun) return;
        proposal.value = response;
        if (!proposal.value?.n_ops) {
          showToast("success", "No structured changes to apply");
          return;
        }
        showDiff.value = true;
      } catch (e) {
        if (run !== proposalRun) return;
        showToast("error", `Cannot preview changes:${e.message}`);
      } finally {
        if (run === proposalRun) proposing.value = false;
      }
    };

    const reviewAgentProposal = async (command) => {
      if (!store.currentSlug || !store.currentTid || !store.taskData) return;
      if (store.dirty) {
        reportAgentOutcome(command, "rejected", {
          reason: "human_edits_present",
        });
        clearLatestWorkspaceCommand(command.jobId, "review.propose");
        if (store.pendingAgentReviewProposal === command) {
          store.pendingAgentReviewProposal = null;
        }
        showToast(
          "error",
          "Review and apply your unsaved edits, or reload before requesting an agent proposal."
        );
        return;
      }
      const previousAgentProposal = agentProposalMeta.value || pendingAgentCommand;
      if (
        previousAgentProposal
        && previousAgentProposal.proposalId !== command.proposalId
      ) {
        reportAgentOutcome(previousAgentProposal, "discarded", {
          reason: "superseded",
        });
      }
      pendingAgentCommand = command;
      proposing.value = true;
      const run = ++proposalRun;
      showDiff.value = false;
      proposal.value = null;
      proposedTaskData.value = null;
      agentProposalMeta.value = null;
      try {
        const staged = stageFigureReviewProposal(store.taskData, command);
        const response = await api.previewTask(
          store.currentSlug,
          store.currentTid,
          stripMeta(staged.data)
        );
        if (run !== proposalRun) return;
        if (!response?.n_ops) {
          reportAgentOutcome(command, "rejected", {
            reason: "matches_review_head",
          });
          clearLatestWorkspaceCommand(command.jobId, "review.propose");
          if (pendingAgentCommand === command) pendingAgentCommand = null;
          showToast("success", "The agent proposal matches the current version; nothing to apply.");
          return;
        }
        proposedTaskData.value = staged.data;
        agentProposalMeta.value = {
          proposalId: command.proposalId,
          jobId: command.jobId,
          target: command.target,
          reactionId: command.reactionId,
          rationale: command.rationale,
          evidence: command.evidence,
        };
        proposal.value = response;
        showDiff.value = true;
        if (pendingAgentCommand === command) pendingAgentCommand = null;
        reportAgentOutcome(agentProposalMeta.value, "staged");
      } catch (e) {
        if (run !== proposalRun) return;
        reportAgentOutcome(command, "rejected", {
          reason: "preview_failed",
        });
        if (pendingAgentCommand === command) pendingAgentCommand = null;
        clearLatestWorkspaceCommand(command.jobId, "review.propose");
        showToast("error", `Cannot preview the agent proposal:${e.message}`);
      } finally {
        if (run === proposalRun) proposing.value = false;
        if (store.pendingAgentReviewProposal === command) {
          store.pendingAgentReviewProposal = null;
        }
      }
    };

    watch(
      () => store.pendingAgentReviewProposal,
      (command) => {
        if (command) void reviewAgentProposal(command);
      },
      { immediate: true }
    );

    watch(
      () => store.dirty,
      (dirty) => {
        if (
          !dirty
          || (!agentProposalMeta.value && !proposedTaskData.value && !pendingAgentCommand)
        ) return;
        proposalRun += 1;
        proposing.value = false;
        const displacedProposal = agentProposalMeta.value || pendingAgentCommand;
        if (displacedProposal?.jobId) {
          reportAgentOutcome(displacedProposal, "discarded", {
            reason: "human_edit_replaced",
          });
          clearLatestWorkspaceCommand(displacedProposal.jobId, "review.propose");
        }
        pendingAgentCommand = null;
        showDiff.value = false;
        proposal.value = null;
        proposedTaskData.value = null;
        agentProposalMeta.value = null;
        showToast("success", "Agent proposal dismissed; your edits are retained.");
      }
    );

    const apply = async () => {
      if (!store.currentSlug || !store.currentTid) return;
      saving.value = true;
      try {
        const reviewerProposalId = proposal.value?.proposal_id;
        const appliedAgentProposal = agentProposalMeta.value;
        if (!reviewerProposalId) {
          throw new Error("Review proposal identity is unavailable; preview the correction again");
        }
        const r = await api.saveTask(
          store.currentSlug,
          store.currentTid,
          // Strip our own _reviewed_meta if user editing across saves
          stripMeta(proposedTaskData.value || store.taskData),
          reviewerProposalId,
          appliedAgentProposal?.proposalId
        );
        const refreshed = await api.task(store.currentSlug, store.currentTid);
        hydrateReviewState(refreshed);
        store.dirty = false;
        // Update manifest's per-task is_reviewed flag so TaskNav lights up
        if (
          store.manifest?.tasks?.[store.currentTid]
        ) {
          store.manifest.tasks[store.currentTid].is_reviewed = true;
          store.manifest.tasks[store.currentTid].reviewed_at = r.reviewed_at;
        }
        // Refresh paper list reviewed counter
        const p = store.papers.find((x) => x.slug === store.currentSlug);
        if (p) {
          const m = store.manifest;
          let n = 0;
          for (const tid in m.tasks)
            if (m.tasks[tid].is_reviewed) n++;
          p.n_reviewed = n;
          p.last_reviewed = r.reviewed_at;
        }
        justSaved.value = true;
        const outcomeDispatched = appliedAgentProposal
          ? reportAgentOutcome(appliedAgentProposal, "applied", {
              reviewedVersion: r.version,
              patchId: r.proposal_id,
            })
          : true;
        if (agentProposalMeta.value?.jobId) {
          clearLatestWorkspaceCommand(agentProposalMeta.value.jobId, "review.propose");
        }
        proposal.value = null;
        proposedTaskData.value = null;
        agentProposalMeta.value = null;
        showDiff.value = false;
        setTimeout(() => (justSaved.value = false), 1200);
        showToast(
          outcomeDispatched ? "success" : "warning",
          outcomeDispatched
            ? `Applied · immutable reviewed-v${r.version}`
            : `Applied reviewed-v${r.version}; agent status could not be linked. Reload version history.`
        );
      } catch (e) {
        if (e.status === 409) {
          if (agentProposalMeta.value) {
            reportAgentOutcome(agentProposalMeta.value, "expired", {
              reason: "review_head_changed",
            });
            const expiredJobId = agentProposalMeta.value.jobId;
            proposalRun += 1;
            showDiff.value = false;
            proposal.value = null;
            proposedTaskData.value = null;
            agentProposalMeta.value = null;
            clearLatestWorkspaceCommand(expiredJobId, "review.propose");
            try {
              hydrateReviewState(await api.task(store.currentSlug, store.currentTid));
              store.dirty = false;
            } catch {
              // The server already rejected the stale write; leave the current
              // visible task intact if refresh itself is unavailable.
            }
            showToast(
              "error",
              "The review version changed. Request a new proposal based on the latest version. No files were written."
            );
            return;
          }
          try {
            proposal.value = await api.previewTask(
              store.currentSlug,
              store.currentTid,
              stripMeta(proposedTaskData.value || store.taskData)
            );
            if (proposal.value?.n_ops > 0) {
              showToast("error", "Version changed; review the refreshed comparison before applying.");
            } else {
              hydrateReviewState(
                await api.task(store.currentSlug, store.currentTid)
              );
              store.dirty = false;
              showDiff.value = false;
              proposal.value = null;
              proposedTaskData.value = null;
              agentProposalMeta.value = null;
              showToast("success", "Version changed; no pending changes remain.");
            }
          } catch (refreshError) {
            showDiff.value = false;
            proposal.value = null;
            proposedTaskData.value = null;
            agentProposalMeta.value = null;
            showToast("error", `Changes expired and could not be refreshed:${refreshError.message}`);
          }
        } else {
          showToast("error", `Save failed:${e.message}`);
        }
      } finally {
        saving.value = false;
      }
    };

    const undo = async () => {
      const currentVersion = store.reviewedVersion;
      const ok = confirm(
        `Revert reviewed-v${currentVersion || "?"}, restoring its immutable parent? Version history will be retained.`
      );
      if (!ok) return;
      try {
        const undone = await api.undoTask(store.currentSlug, store.currentTid);
        const r = await api.task(store.currentSlug, store.currentTid);
        hydrateReviewState(r);
        store.dirty = false;
        if (store.manifest?.tasks?.[store.currentTid]) {
          store.manifest.tasks[store.currentTid].is_reviewed = r.is_reviewed;
          store.manifest.tasks[store.currentTid].reviewed_at = r.reviewed_at;
        }
        const p = store.papers.find((x) => x.slug === store.currentSlug);
        if (p && !r.is_reviewed && p.n_reviewed > 0) p.n_reviewed -= 1;
        showToast(
          "success",
          undone.head_kind === "legacy"
            ? `Reverted reviewed-v${undone.undone_version} → legacy reviewed snapshot`
            : undone.head_version
            ? `Reverted reviewed-v${undone.undone_version} → reviewed-v${undone.head_version}`
            : `Reverted reviewed-v${undone.undone_version} → raw prediction`
        );
      } catch (e) {
        showToast("error", `Undo failed:${e.message}`);
      }
    };

    const recoverPointer = async () => {
      const latest = store.reviewedVersions?.[store.reviewedVersions.length - 1];
      if (!latest) {
        showToast("error", "No immutable reviewed version is available for recovery");
        return;
      }
      const ok = confirm(
        `The review pointer is invalid. Restore immutable reviewed-v${latest.version}？`
      );
      if (!ok) return;
      try {
        await api.restoreTaskVersion(
          store.currentSlug,
          store.currentTid,
          latest.version
        );
        hydrateReviewState(
          await api.task(store.currentSlug, store.currentTid)
        );
        store.dirty = false;
        showToast("success", `Restored review pointer → reviewed-v${latest.version}`);
      } catch (e) {
        showToast("error", `Restore failed:${e.message}`);
      }
    };

    const toggleDropped = () => {
      const cur = Boolean(store.taskData?._dropped);
      if (cur) {
        const next = { ...store.taskData };
        delete next._dropped;
        delete next._dropped_reason;
        store.taskData = next;
        store.dirty = true;
      } else {
        const reason = prompt(
          "Reason for marking as a non-reaction (optional)? The task will be crossed out in the review list."
        );
        if (reason === null) return;
        store.taskData = {
          ...store.taskData,
          _dropped: true,
          _dropped_reason: reason || "(no reason provided)",
        };
        store.dirty = true;
      }
    };

    return () => {
      const isDropped = Boolean(store.taskData?._dropped);
      const cls =
        "save-bar" +
        (store.dirty ? " dirty" : "") +
        (justSaved.value ? " saved-flash" : "");
      let statusEl;
      if (store.reviewPointerCorrupt) {
        statusEl = h(
          "span",
          { class: "status dropped" },
          "review pointer damaged · immutable history retained"
        );
      } else if (isDropped) {
        statusEl = h(
          "span",
          { class: "status dropped" },
          "Marked as non-reaction"
        );
      } else if (store.dirty) {
        statusEl = h("span", { class: "status dirty" }, "Unsaved changes");
      } else if (store.isReviewed) {
        statusEl = h(
          "span",
          { class: "status reviewed" },
          store.reviewedVersion
            ? `reviewed-v${store.reviewedVersion} · ${formatDate(store.reviewedAt)}`
            : `legacy reviewed snapshot · ${formatDate(store.reviewedAt)}`
        );
      } else {
        statusEl = h("span", { class: "status fresh" }, "Original (unreviewed)");
      }
      return h("div", { class: cls }, [
        h("div", { class: "info" }, [
          h("div", { class: "breadcrumb" }, [
            ...breadcrumb.value.map((s, i) =>
              h("span", null, (i > 0 ? " · " : "") + s)
            ),
          ]),
          statusEl,
        ]),
        h("div", { class: "actions" }, [
          store.reviewPointerCorrupt
            ? h(
                "button",
                {
                  class: "warn",
                  onClick: recoverPointer,
                  disabled: !store.reviewedVersions?.length,
                },
                "Recover latest reviewed version"
              )
            : null,
          store.isReviewed
            ? h(
                "button",
                {
                  class: "icon",
                  onClick: () => (showDiff.value = true),
                  title: "Compare reviewed output with the original prediction",
                },
                `📊 Diff${store.reviewedVersions?.length ? ` · ${store.reviewedVersions.length} versions` : ""}`
              )
            : null,
          h(
            "button",
            {
              onClick: toggleDropped,
              class: "icon" + (isDropped ? " warn" : " danger"),
              title: isDropped
                ? "Clear the non-reaction flag"
                : "Mark this task as a non-reaction or invalid",
            },
            isDropped ? "↻ Restore reaction" : "🚫 Not a reaction"
          ),
          store.isReviewed
            ? h(
                "button",
                {
                  onClick: undo,
                  class: "icon",
                  title: "Restore the immutable parent version",
                },
                store.reviewedVersion
                  ? `↺ Undo v${store.reviewedVersion}`
                  : "↺ Undo legacy"
              )
            : null,
          h(
            "button",
            {
              onClick: reviewChanges,
              class: store.dirty ? "warn" : "primary",
              disabled: saving.value || proposing.value || !store.dirty,
            },
            proposing.value ? "Preparing diff…" : store.dirty ? "Review changes" : "Up to date"
          ),
        ]),
        showDiff.value
          ? h(DiffModal, {
                proposal: proposal.value,
                applying: saving.value,
                agentContext: agentProposalMeta.value,
                onApply: apply,
                onClose: () => {
                  proposalRun += 1;
                  if (agentProposalMeta.value?.jobId) {
                    reportAgentOutcome(agentProposalMeta.value, "discarded", {
                      reason: "human_closed",
                    });
                    clearLatestWorkspaceCommand(
                      agentProposalMeta.value.jobId,
                      "review.propose"
                    );
                  }
                  showDiff.value = false;
                  proposal.value = null;
                  proposedTaskData.value = null;
                  agentProposalMeta.value = null;
                },
              })
          : null,
      ]);
    };
  },
};

function stripMeta(d) {
  // Remove _reviewed_meta we may have read from a previous save round-trip;
  // server re-injects fresh meta on save.
  if (d && typeof d === "object" && "_reviewed_meta" in d) {
    const out = { ...d };
    delete out._reviewed_meta;
    return out;
  }
  return d;
}

function formatDate(iso) {
  if (!iso) return "?";
  try {
    const d = new Date(iso);
    return (
      d.getFullYear() +
      "-" +
      String(d.getMonth() + 1).padStart(2, "0") +
      "-" +
      String(d.getDate()).padStart(2, "0") +
      " " +
      String(d.getHours()).padStart(2, "0") +
      ":" +
      String(d.getMinutes()).padStart(2, "0")
    );
  } catch {
    return iso;
  }
}
