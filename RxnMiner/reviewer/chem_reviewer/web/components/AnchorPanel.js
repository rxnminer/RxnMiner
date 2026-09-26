import { h, ref, watch, onMounted, onBeforeUnmount } from "vue";
import { api } from "/static/lib/api.js";
import { store, showToast } from "/static/lib/store.js";
import {
  bboxKey,
  reactionsForBbox,
  reactionElementId,
} from "/static/lib/reaction-link.js";
import { BBoxImagePanel } from "/static/components/BBoxImagePanel.js?v=20260801e";

const ANCHOR_ROLE_COLORS = {
  reactant: { stroke: "#4a6db5", fill: "rgba(74,109,181,.10)" },
  product: { stroke: "#C96342", fill: "rgba(201,99,66,.12)" },
  condition: { stroke: "#7b4fa7", fill: "rgba(123,79,167,.10)" },
  default: { stroke: "#666", fill: "rgba(102,102,102,.08)" },
};
//

export const AnchorPanel = {
  setup() {
    const fullscreen = ref(false);
    const naturalSize = ref(null);
    const bboxes = ref([]); // [{bbox: [x1,y1,x2,y2], identifier, smi, role_hint, ...}]
    const showBboxes = ref(true);
    const hoverBbox = ref(null);

    const onLoad = (ev) => {
      const img = ev.target;
      naturalSize.value = { w: img.naturalWidth, h: img.naturalHeight };
      store.anchorAspect = img.naturalWidth / Math.max(img.naturalHeight, 1);
    };

    const fetchBboxes = async () => {
      bboxes.value = [];
      store.molparserDetections = [];
      store.molparserDetectionsLoaded = false;
      if (!store.currentSlug || !store.currentTid) return;
      try {
        const data = await api.molparser(store.currentSlug, store.currentTid);
        bboxes.value = Array.isArray(data) ? data : [];
        store.molparserDetections = bboxes.value;
      } catch (e) {
        // Non-fatal — bbox overlay just shows nothing
        bboxes.value = [];
        store.molparserDetections = [];
      } finally {
        store.molparserDetectionsLoaded = true;
      }
    };

    // Fetch on task change
    watch(
      () => [store.currentSlug, store.currentTid],
      () => {
        hoverBbox.value = null;
        fetchBboxes();
      },
      { immediate: true }
    );

    const selectBbox = (bbox, ev) => {
      ev?.stopPropagation();
      const key = bboxKey(bbox);
      const links = reactionsForBbox(store.taskData, bbox);
      if (!links.length) {
        store.activeBboxKey = key || null;
        store.activeReactionKey = null;
        store.activeBboxCycle = 0;
        showToast("error", "No reaction is linked to this box");
        return;
      }

      // A grouped scope drawing can back several concrete rows. Repeated clicks
      // on the same bbox cycle through those rows instead of always choosing the first.
      const nextCycle = store.activeBboxKey === key
        ? (store.activeBboxCycle + 1) % links.length
        : 0;
      const selected = links[nextCycle];
      store.activeBboxKey = key;
      store.activeBboxCycle = nextCycle;
      store.activeReactionKey = selected.key;

      requestAnimationFrame(() => {
        document
          .getElementById(reactionElementId(selected.key))
          ?.scrollIntoView({ behavior: "smooth", block: "center" });
      });
      const grouped = links.length > 1
        ? `; linked reactions:  ${links.length}. Click again to cycle through them`
        : "";
      showToast("success", `Located  ${selected.label}${grouped}`, 3200);
    };

    const onEsc = (ev) => {
      if (ev.key === "Escape" && fullscreen.value) {
        fullscreen.value = false;
      }
    };
    onMounted(() => window.addEventListener("keydown", onEsc));
    onBeforeUnmount(() => window.removeEventListener("keydown", onEsc));

    const renderImagePanel = (url, alt) => h(BBoxImagePanel, {
      imageUrl: url,
      alt,
      coordinateSize: naturalSize.value,
      boxes: bboxes.value.map((bbox, index) => {
        const key = bboxKey(bbox) || `bbox:${index}`;
        return {
          ...bbox,
          ...(ANCHOR_ROLE_COLORS[bbox.role_hint] || ANCHOR_ROLE_COLORS.default),
          key,
          label: bbox.identifier || "",
          role: bbox.role_hint || "default",
          active: store.activeBboxKey === key || hoverBbox.value === index,
          dimmed: store.activeEvidenceFocus === "selected-only"
            && Boolean(store.activeBboxKey)
            && store.activeBboxKey !== key,
          data: { "bbox-index": bbox.bbox_index ?? "" },
        };
      }),
      showBoxes: showBboxes.value,
      onImageLoad: onLoad,
      onBoxHover: (bbox, index) => {
        hoverBbox.value = bbox ? index : null;
      },
      onBoxSelect: (bbox, event) => selectBbox(bbox, event),
    });

    // Tooltip: small floating panel showing details of the hovered bbox
    const renderTooltip = () => {
      if (hoverBbox.value === null) return null;
      const b = bboxes.value[hoverBbox.value];
      if (!b) return null;
      const links = reactionsForBbox(store.taskData, b);
      return h(
        "div",
        { class: "bbox-tooltip" },
        [
          b.identifier
            ? h("div", { class: "tt-id" }, [
                h("span", null, "ID: "),
                h("b", null, b.identifier),
              ])
            : null,
          b.role_hint
            ? h("div", { class: "tt-role" }, [
                h("span", null, "Type: "),
                b.role_hint,
              ])
            : null,
          b.smi
            ? h("div", { class: "tt-smi" }, [
                h("span", null, "SMILES: "),
                h("code", null, b.smi.slice(0, 80) + (b.smi.length > 80 ? "…" : "")),
              ])
            : null,
          b.markush
            ? h("div", { class: "tt-markush" }, "(includes Markush)")
            : null,
          h(
            "div",
            { class: "tt-meta" },
            `bbox=[${(b.bbox || []).map((v) => Math.round(v)).join(",")}] · idx=${b.bbox_index}`
          ),
          h(
            "div",
            { class: "tt-link" },
            links.length
              ? `Go to reaction：${links.map((x) => x.label).join(" / ")}`
              : "No linked reaction"
          ),
        ]
      );
    };

    return () => {
      if (!store.currentSlug || !store.currentTid) return null;
      const meta = store.taskMeta || {};
      const cap = meta.anchor_figure || meta.source_caption || "";
      const sub = meta.source_subpanel || "";
      const tid = store.currentTid;
      const page = meta.anchor_page ?? meta.page_index;
      const url = api.anchorUrl(store.currentSlug, tid);
      const sz = naturalSize.value
        ? `${naturalSize.value.w}×${naturalSize.value.h}`
        : meta.anchor_size_px && meta.anchor_size_px.length === 2
        ? `${meta.anchor_size_px[0]}×${meta.anchor_size_px[1]}`
        : "?";
      const isMs = Boolean(meta.is_multistep_synthesis);
      const nBox = bboxes.value.length;

      const titleNodes = [
        h("span", { class: "anchor-icon" }, "📍"),
        h("b", null, cap || `Figure ${tid}`),
        sub ? h("span", { class: "anchor-sub" }, ` (${sub})`) : null,
        page
          ? h("span", { class: "anchor-page" }, `Page ${page}`)
          : null,
        h("span", { class: "anchor-size" }, `${sz} px`),
        isMs
          ? h(
              "span",
              {
                class: "anchor-chip multistep",
                title: `Multistep synthesis: ${meta.synthesis_name || ""}`,
              },
              `🔗 Multistep`
            )
          : null,
        meta.n_failed > 0
          ? h(
              "span",
              {
                class: "anchor-chip failed",
                title: `${meta.n_failed} failed or control entries`,
              },
              `✗ Failed×${meta.n_failed}`
            )
          : null,
        h("span", { class: "anchor-spacer" }),
        // BBox toggle
        h(
          "button",
          {
            class: "anchor-btn" + (showBboxes.value ? " active" : ""),
            title: showBboxes.value
              ? `Hide detection boxes (${nBox})`
              : `Show detection boxes (${nBox})`,
            onClick: (e) => {
              e.stopPropagation();
              showBboxes.value = !showBboxes.value;
            },
          },
          `🔍 BBox×${nBox}`
        ),
        h(
          "button",
          {
            class: "anchor-btn",
            title: "Open fullscreen (Esc to exit)",
            onClick: (e) => {
              e.stopPropagation();
              fullscreen.value = true;
            },
          },
          "⛶ Fullscreen"
        ),
      ];

      return h("section", { class: "anchor-panel" }, [
        h("div", { class: "anchor-title" }, titleNodes),
        h(
          "div",
          {
            class: "anchor-img-wrap",
            onClick: () => (fullscreen.value = true),
            title: "View source in fullscreen",
          },
          [
            renderImagePanel(url, tid),
          ]
        ),
        renderTooltip(),
        // Fullscreen overlay
        fullscreen.value
          ? h(
              "div",
              {
                class: "anchor-fullscreen",
                onClick: () => (fullscreen.value = false),
              },
              [
                h("div", { class: "anchor-fullscreen-bar" }, [
                  h("span", null, [
                    h("b", null, cap || `Figure ${tid}`),
                    page ? ` · Page ${page}` : "",
                    ` · ${sz} px`,
                    nBox > 0
                      ? h(
                          "span",
                          {
                            style:
                              "margin-left:12px;font-size:11px;opacity:0.85;",
                          },
                          `· ${nBox} boxes`
                        )
                      : null,
                  ]),
                  h("div", { style: "display:flex;gap:8px;" }, [
                    h(
                      "button",
                      {
                        class:
                          "anchor-btn" + (showBboxes.value ? " active" : ""),
                        onClick: (e) => {
                          e.stopPropagation();
                          showBboxes.value = !showBboxes.value;
                        },
                        title: "Toggle detection boxes",
                      },
                      `🔍 BBox`
                    ),
                    h(
                      "button",
                      {
                        class: "anchor-btn",
                        onClick: (e) => {
                          e.stopPropagation();
                          fullscreen.value = false;
                        },
                      },
                      "✕ Close (Esc)"
                    ),
                  ]),
                ]),
                h(
                  "div",
                  {
                    class: "anchor-fullscreen-img-wrap",
                    onClick: (e) => e.stopPropagation(),
                  },
                  [
                    renderImagePanel(url, tid),
                  ]
                ),
              ]
            )
          : null,
      ]);
    };
  },
};
