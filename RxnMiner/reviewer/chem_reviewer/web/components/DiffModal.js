// Reads /api/paper/<slug>/task/<tid>/diff and shows two tabs:
import { h, ref, onMounted } from "vue";
import { api } from "/static/lib/api.js";
import { store, showToast } from "/static/lib/store.js";

async function fetchDiff(slug, tid) {
  const r = await fetch(
    `/api/paper/${encodeURIComponent(slug)}/task/${encodeURIComponent(tid)}/diff`
  );
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

// Very tiny markdown → HTML (only what we emit ourselves: # / ## / - / **bold** / `code`).
function renderMd(md) {
  const lines = md.split("\n");
  const parts = [];
  let inUl = false;
  const closeUl = () => {
    if (inUl) {
      parts.push("</ul>");
      inUl = false;
    }
  };
  const inline = (s) =>
    s
      .replace(/`([^`]+?)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>")
      .replace(/_(\([^)]+\))_/g, "<em>$1</em>");
  for (const ln of lines) {
    if (ln.startsWith("<!--")) continue;
    if (ln.startsWith("# ")) {
      closeUl();
      parts.push(`<h2>${inline(ln.slice(2))}</h2>`);
    } else if (ln.startsWith("## ")) {
      closeUl();
      parts.push(`<h3>${inline(ln.slice(3))}</h3>`);
    } else if (ln.startsWith("- ")) {
      if (!inUl) {
        parts.push("<ul>");
        inUl = true;
      }
      parts.push(`<li>${inline(ln.slice(2))}</li>`);
    } else if (ln.trim() === "") {
      closeUl();
      parts.push("<br>");
    } else {
      closeUl();
      parts.push(`<p>${inline(ln)}</p>`);
    }
  }
  closeUl();
  return parts.join("\n");
}

export const DiffModal = {
  props: {
    proposal: { type: Object, default: null },
    applying: { type: Boolean, default: false },
    agentContext: { type: Object, default: null },
  },
  emits: ["close", "apply"],
  setup(props, { emit }) {
    const tab = ref("md"); // 'md' | 'json'
    const diff = ref(props.proposal);
    const err = ref(null);
    const loading = ref(!props.proposal);
    const selectedVersion = ref(store.reviewedVersion);

    const reload = async () => {
      loading.value = true;
      err.value = null;
      try {
        diff.value = await fetchDiff(store.currentSlug, store.currentTid);
      } catch (e) {
        err.value = e.message || String(e);
      } finally {
        loading.value = false;
      }
    };

    onMounted(() => {
      if (!props.proposal) void reload();
    });

    const loadVersion = async (version) => {
      if (props.proposal || loading.value) return;
      loading.value = true;
      err.value = null;
      try {
        diff.value = await api.taskVersionDiff(
          store.currentSlug,
          store.currentTid,
          version
        );
        selectedVersion.value = version;
      } catch (e) {
        err.value = e.message || String(e);
      } finally {
        loading.value = false;
      }
    };

    const copyJson = async () => {
      if (!diff.value) return;
      try {
        await navigator.clipboard.writeText(
          JSON.stringify(diff.value.ops || [], null, 2)
        );
        showToast("success", "Copied JSON Patch");
      } catch (e) {
        showToast("error", e.message || "Copy failed");
      }
    };

    const downloadMd = () => {
      const text = diff.value?.markdown || "";
      const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${store.currentSlug}__${store.currentTid}.diff.md`;
      a.click();
      URL.revokeObjectURL(url);
    };

    return () =>
      h(
        "div",
        {
          class: "modal-backdrop",
          onClick: (e) => {
            if (e.target.classList.contains("modal-backdrop")) emit("close");
          },
        },
        [
          h(
            "div",
            { class: "modal-card", style: "max-width: 1100px; height: 80vh;" },
            [
              h("div", { class: "modal-header" }, [
                h("div", null, [
                  h("b", null, "📊 Review changes"),
                  h(
                    "span",
                    {
                      style:
                        "color:var(--fg-muted);font-size:12px;margin-left:12px;",
                    },
                    diff.value
                      ? `${diff.value.proposed ? "Proposed patch" : "Reviewed snapshot"} · ${
                          diff.value.n_ops || 0
                        } changes${
                          diff.value.from_cache ? " · cache" : " · live"
                        }`
                      : ""
                  ),
                ]),
                h(
                  "button",
                  { class: "icon", onClick: () => emit("close") },
                  "✕"
                ),
              ]),
              // Tabs
              h(
                "div",
                {
                  style:
                    "padding:8px 22px;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center;",
                },
                [
                  h(
                    "button",
                    {
                      class: tab.value === "md" ? "primary" : "icon",
                      onClick: () => (tab.value = "md"),
                    },
                    "Readable summary"
                  ),
                  h(
                    "button",
                    {
                      class: tab.value === "json" ? "primary" : "icon",
                      onClick: () => (tab.value = "json"),
                    },
                    "JSON Patch"
                  ),
                  h("div", { style: "flex:1;" }),
                  diff.value && diff.value.is_reviewed
                    ? h(
                        "span",
                        { style: "color:var(--fg-muted);font-size:12px;" },
                        "Saved to disk:"
                      )
                    : null,
                  diff.value && diff.value.is_reviewed
                    ? h(
                        "code",
                        {
                          style: "font-size:11px;background:var(--bg-sunken);",
                        },
                        "expected_output.diff.{json,md}"
                      )
                    : null,
                  diff.value?.proposed
                    ? h("span", {
                        style: "color:var(--orange);font-size:12px;",
                      }, `No files written · Apply creates reviewed-v${diff.value.next_version}`)
                    : null,
                ]
              ),
              !props.proposal && store.reviewedVersions?.length
                ? h("nav", {
                    class: "review-version-history",
                    "aria-label": "Immutable review versions",
                    style:
                      "padding:8px 22px;border-bottom:1px solid var(--border);display:flex;gap:6px;align-items:center;overflow:auto;",
                  }, [
                    h("span", {
                      style: "font-size:11px;color:var(--fg-muted);white-space:nowrap;",
                    }, "Version history"),
                    ...store.reviewedVersions.map((item) => h("button", {
                      class: selectedVersion.value === item.version ? "primary" : "icon",
                      disabled: loading.value,
                      title: `${item.n_ops ?? "?"} ops · ${item.reviewed_at || ""}`,
                      onClick: () => loadVersion(item.version),
                    }, `reviewed-v${item.version}`)),
                  ])
                : null,
              // Body
              h(
                "div",
                {
                  class: "modal-body",
                  style: "padding:20px 22px;background:var(--bg);overflow:auto;",
                },
                [
                  props.agentContext
                    ? h("aside", {
                        class: "agent-proposal-context",
                        "aria-label": "Agent correction proposal",
                      }, [
                        h("b", null,
                          `Pi Agent proposal · ${props.agentContext.reactionId} · not applied`),
                        h("p", null, props.agentContext.rationale),
                        props.agentContext.evidence?.length
                          ? h("ul", null,
                              props.agentContext.evidence.map((item) => h("li", null, item)))
                          : null,
                      ])
                    : null,
                  loading.value
                    ? h("div", { class: "loading" }, "Loading changes …")
                    : err.value
                    ? h(
                        "div",
                        { style: "color:var(--red);" },
                        `Load failed: ${err.value}`
                      )
                    : !diff.value || (!diff.value.is_reviewed && !diff.value.proposed)
                    ? h(
                        "div",
                        { style: "color:var(--fg-muted);" },
                        "No reviewed version has been saved for this task."
                      )
                    : tab.value === "md"
                    ? h("div", {
                        class: "diff-md-body",
                        innerHTML: renderMd(diff.value.markdown || ""),
                      })
                    : h(
                        "pre",
                        {
                          class: "mono",
                          style:
                            "white-space:pre-wrap;font-size:12px;background:var(--bg-card);padding:14px;border-radius:6px;border:1px solid var(--border);max-height:60vh;overflow:auto;",
                        },
                        JSON.stringify(diff.value.ops || [], null, 2)
                      ),
                ]
              ),
              // Footer
              h("div", { class: "modal-footer" }, [
                tab.value === "json"
                  ? h(
                      "button",
                      { class: "icon", onClick: copyJson, disabled: loading.value },
                      "📋 Copy JSON Patch"
                    )
                  : h(
                      "button",
                      { class: "icon", onClick: downloadMd, disabled: loading.value },
                      "⬇ Download Markdown"
                    ),
                diff.value?.proposed
                  ? h("button", {
                      class: "primary",
                      disabled: props.applying || loading.value || !(diff.value.n_ops > 0),
                      onClick: () => emit("apply"),
                    }, props.applying ? "Applying…" : "Apply → immutable reviewed snapshot")
                  : null,
                h("button", { onClick: () => emit("close") }, "Close"),
              ]),
            ]
          ),
        ]
      );
  },
};
