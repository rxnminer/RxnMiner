import { h, onBeforeUnmount, onMounted, reactive } from "vue";
import {
  DISTRIBUTED_TERMINAL,
  shardProgress,
  shardStatusLabel,
  validateShardArchive,
  visibleShardCases,
} from "/static/lib/distributed-shard.js";

async function jsonRequest(url, options = {}) {
  const response = await fetch(url, { headers: { Accept: "application/json", ...(options.headers || {}) }, ...options });
  let body = {};
  try { body = await response.json(); } catch {}
  if (!response.ok) {
    const detail = body?.detail;
    const message = typeof detail === "string" ? detail : detail?.message;
    throw new Error(message || `Request failed (HTTP ${response.status})`);
  }
  return body;
}

export const DistributedShardPanel = {
  name: "DistributedShardPanel",
  setup() {
    const state = reactive({
      file: null,
      fileError: "",
      batch: null,
      batches: [],
      busy: false,
      error: "",
      concurrency: 1,
      filter: "all",
    });
    let timer = null;

    async function refreshList() {
      try {
        const response = await jsonRequest("/api/distributed/shards");
        state.batches = response.batches || [];
        if (!state.batch && state.batches.length) await selectBatch(state.batches[0].id);
      } catch (error) {
        state.error = error?.message || "Could not load distributed shards.";
      }
    }

    async function selectBatch(id) {
      state.error = "";
      try {
        state.batch = await jsonRequest(`/api/distributed/shards/${encodeURIComponent(id)}`);
        schedule();
      } catch (error) {
        state.error = error?.message || "Could not load this shard.";
      }
    }

    function schedule() {
      if (timer) clearTimeout(timer);
      timer = null;
      if (state.batch && !DISTRIBUTED_TERMINAL.has(state.batch.status)) {
        timer = setTimeout(async () => {
          await selectBatch(state.batch.id);
        }, state.batch.status === "running" ? 3000 : 8000);
      }
    }

    function choose(event) {
      state.file = event.target.files?.[0] || null;
      state.fileError = validateShardArchive(state.file);
      state.error = "";
    }

    async function upload() {
      if (!state.file || state.fileError || state.busy) return;
      state.busy = true;
      state.error = "";
      try {
        const form = new FormData();
        form.append("archive", state.file);
        state.batch = await jsonRequest("/api/distributed/shards", { method: "POST", body: form });
        state.file = null;
        await refreshList();
        schedule();
      } catch (error) {
        state.error = error?.message || "Shard preflight failed.";
      } finally {
        state.busy = false;
      }
    }

    async function start() {
      if (!state.batch || state.batch.status !== "staged" || state.busy) return;
      state.busy = true;
      state.error = "";
      try {
        state.batch = await jsonRequest(`/api/distributed/shards/${encodeURIComponent(state.batch.id)}/start`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ concurrency: state.concurrency }),
        });
        schedule();
      } catch (error) {
        state.error = error?.message || "Shard could not start.";
      } finally {
        state.busy = false;
      }
    }

    async function openCase(row) {
      state.error = "";
      try {
        const result = await jsonRequest(
          `/api/distributed/shards/${encodeURIComponent(state.batch.id)}/cases/${encodeURIComponent(row.id)}/reviewer`,
          { method: "POST" },
        );
        location.assign(result.workspaceUrl);
      } catch (error) {
        state.error = error?.message || "Sealed case could not open in the workspace.";
      }
    }

    onMounted(() => { void refreshList(); });
    onBeforeUnmount(() => { if (timer) clearTimeout(timer); });

    return () => {
      const batch = state.batch;
      const progress = shardProgress(batch);
      const cases = visibleShardCases(batch?.cases, state.filter);
      return h("section", { class: "distributed-shard-panel", "aria-labelledby": "distributed-shard-title" }, [
        h("div", { class: "panel-heading" }, [
          h("span", null, "03"),
          h("div", null, [
            h("h3", { id: "distributed-shard-title" }, "Run an assigned Figure shard"),
            h("p", null, "Import a frozen, GT-free package. Pi runs each figure in an isolated sealed session."),
          ]),
        ]),
        h("div", { class: "distributed-shard-layout" }, [
          h("div", { class: "distributed-shard-import" }, [
            h("label", { class: "distributed-file" }, [
              h("b", null, state.file?.name || "Choose shard-XXX.input.tar.gz"),
              h("small", null, state.file ? `${Math.max(1, Math.round(state.file.size / 1024 / 1024))} MB` : "Contains 47 figures and immutable MolParser caches; no GT."),
              h("input", { type: "file", accept: ".gz,application/gzip", onChange: choose }),
            ]),
            state.fileError ? h("p", { class: "file-errors", role: "alert" }, state.fileError) : null,
            h("button", { class: "primary-action", disabled: !state.file || !!state.fileError || state.busy, onClick: upload },
              state.busy && !batch ? "Checking hashes and reference isolation…" : "Import and preflight shard"),
            h("p", { class: "truth-note" }, "The browser never receives GT or subscription credentials. Cache misses and repository drift fail closed."),
            state.batches.length ? h("label", { class: "distributed-history" }, [
              "Previous shard",
              h("select", { value: batch?.id || "", onChange: (event) => { void selectBatch(event.target.value); } },
                state.batches.map((item) => h("option", { value: item.id }, `${item.shardId} · ${shardStatusLabel(item.status)} · ${item.sealed || 0}/${item.total || 0}`))),
            ]) : null,
          ]),
          batch ? h("div", { class: `distributed-shard-status status-${batch.status}` }, [
            h("div", { class: "distributed-status-head" }, [
              h("div", null, [h("b", null, `${batch.shardId} · ${shardStatusLabel(batch.status)}`), h("small", null, `Pinned ${String(batch.repoPin || "").slice(0, 12)} · archive ${String(batch.archiveSha256 || "").slice(0, 12)}`)]),
              h("strong", null, `${progress.sealed}/${progress.total}`),
            ]),
            h("div", { class: "distributed-progress", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(progress.total), "aria-valuenow": String(progress.sealed) },
              h("i", { style: `width:${progress.percent}%` })),
            batch.status === "staged" ? h("div", { class: "distributed-start" }, [
              h("label", null, ["Concurrency", h("select", { value: state.concurrency, onChange: (event) => { state.concurrency = Number(event.target.value); } }, [h("option", { value: 1 }, "1 · safest"), h("option", { value: 2 }, "2 · coordinator-approved")])]),
              h("button", { class: "primary-action", disabled: state.busy, onClick: start }, state.busy ? "Starting…" : "Start sealed extraction"),
            ]) : null,
            batch.status === "complete" ? h("div", { class: "distributed-success" }, [
              h("p", null, "All cases passed the frozen validator. Open any sealed result below or return the checksummed submission."),
              h("div", { class: "distributed-downloads" }, [
                h("a", { href: `/api/distributed/shards/${encodeURIComponent(batch.id)}/submission`, download: "" }, "Download submission"),
                h("a", { href: `/api/distributed/shards/${encodeURIComponent(batch.id)}/submission?checksum=true`, download: "" }, "Download SHA-256"),
              ]),
            ]) : null,
            batch.status === "failed" || batch.status === "interrupted" ? h("p", { class: "file-errors" }, "The run stopped without a valid complete submission. Preserve logs; do not hand-edit predictions.") : null,
          ]) : h("div", { class: "distributed-empty" }, "No shard imported in this local workspace."),
        ]),
        state.error ? h("p", { class: "file-errors distributed-error", role: "alert" }, state.error) : null,
        batch?.cases?.length ? h("div", { class: "distributed-cases" }, [
          h("div", { class: "distributed-case-tools" }, [
            h("b", null, "Figure results"),
            h("select", { value: state.filter, onChange: (event) => { state.filter = event.target.value; } }, ["all", "sealed", "running", "pending"].map(value => h("option", { value }, value))),
          ]),
          h("div", { class: "distributed-case-grid" }, cases.map(row => h("article", { class: `distributed-case case-${row.status}` }, [
            h("span", { class: "case-state" }, row.status),
            h("b", { title: row.id }, row.id),
            h("small", null, [row.source || "unknown", row.difficulty ? ` · ${row.difficulty}` : ""]),
            row.reviewable ? h("button", { type: "button", onClick: () => { void openCase(row); } }, "Open result") : null,
          ]))),
        ]) : null,
      ]);
    };
  },
};
