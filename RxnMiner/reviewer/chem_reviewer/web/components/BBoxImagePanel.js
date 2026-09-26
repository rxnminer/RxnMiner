import { h, nextTick, onMounted, ref, watch } from "vue";

const ROLE_STYLE = {
  reactant: { stroke: "#7b4fa7", fill: "rgba(123,79,167,.10)" },
  reactants: { stroke: "#7b4fa7", fill: "rgba(123,79,167,.10)" },
  product: { stroke: "#16874f", fill: "rgba(22,135,79,.10)" },
  products: { stroke: "#16874f", fill: "rgba(22,135,79,.10)" },
  condition: { stroke: "#9a6700", fill: "rgba(154,103,0,.08)" },
  anchor: { stroke: "#2563eb", fill: "rgba(37,99,235,.04)" },
  layout: { stroke: "#667085", fill: "transparent" },
  gt: { stroke: "#e8590c", fill: "transparent" },
  default: { stroke: "#475467", fill: "rgba(71,84,103,.06)" },
};

function finiteSize(size) {
  const w = Number(size?.w);
  const height = Number(size?.h);
  return Number.isFinite(w) && w > 0 && Number.isFinite(height) && height > 0
    ? { w, h: height }
    : null;
}

function finiteBox(box) {
  const values = (box?.bbox || []).slice(0, 4).map(Number);
  return values.length === 4 && values.every(Number.isFinite) ? values : null;
}

function styleFor(box) {
  const base = ROLE_STYLE[box.role] || ROLE_STYLE[box.kind] || ROLE_STYLE.default;
  return {
    stroke: box.stroke || base.stroke,
    fill: box.fill ?? base.fill,
  };
}

// Shared renderer for 8774 anchor images and PDF Reviewer crops/pages.
// Every caller supplies image-space coordinates; the SVG viewBox is the sole
// coordinate transform, so image scaling cannot drift from its overlays.
export const BBoxImagePanel = {
  name: "BBoxImagePanel",
  props: {
    imageUrl: { type: String, required: true },
    coordinateSize: { type: Object, default: null },
    boxes: { type: Array, default: () => [] },
    showBoxes: { type: Boolean, default: true },
    alt: { type: String, default: "" },
    compact: { type: Boolean, default: false },
    dimOutside: { type: Boolean, default: false },
    dimBox: { type: Array, default: null },
    imageId: { type: String, default: "" },
    overlayId: { type: String, default: "" },
    onImageLoad: { type: Function, default: null },
    onImageError: { type: Function, default: null },
    onBoxSelect: { type: Function, default: null },
    onBoxHover: { type: Function, default: null },
    drawMode: { type: Boolean, default: false },
    draftBox: { type: Array, default: null },
    draftLabel: { type: String, default: "Draft" },
    drawLabel: { type: String, default: "Draw replacement evidence box" },
    onDraftChange: { type: Function, default: null },
    onDraftCommit: { type: Function, default: null },
    onDraftCancel: { type: Function, default: null },
  },
  setup(props) {
    let dragStart = null;
    let dragPreviousBox = null;
    let dragPointerId = null;
    let dragCaptureElement = null;
    const imageElement = ref(null);
    let settledImageUrl = "";

    function notifyImageLoaded(event = null) {
      const image = event?.currentTarget || imageElement.value;
      if (!image || !image.complete || image.naturalWidth <= 0) return;
      if (image.getAttribute("src") !== props.imageUrl) return;
      // A render function commonly creates a new callback identity on every
      // render. Image settlement belongs to the immutable image URL, not that
      // callback identity; otherwise a cached image can form a
      // render -> callback -> reactive write -> render feedback loop.
      if (settledImageUrl === props.imageUrl) return;
      settledImageUrl = props.imageUrl;
      props.onImageLoad?.(event || { currentTarget: image, target: image });
    }

    function settleCachedImage() {
      nextTick(() => notifyImageLoaded());
    }

    onMounted(settleCachedImage);
    watch(
      () => props.imageUrl,
      settleCachedImage,
      { flush: "post" },
    );

    function pointerInImage(event, size) {
      const svg = event.currentTarget?.ownerSVGElement;
      const matrix = svg?.getScreenCTM?.();
      if (!svg || !matrix) return null;
      const point = svg.createSVGPoint();
      point.x = event.clientX;
      point.y = event.clientY;
      const local = point.matrixTransform(matrix.inverse());
      return {
        x: Math.max(0, Math.min(size.w, local.x)),
        y: Math.max(0, Math.min(size.h, local.y)),
      };
    }

    function normalizedBox(start, end) {
      if (!start || !end) return null;
      return [
        Math.min(start.x, end.x),
        Math.min(start.y, end.y),
        Math.max(start.x, end.x),
        Math.max(start.y, end.y),
      ];
    }

    function cancelDraft() {
      if (
        dragCaptureElement
        && dragPointerId != null
        && dragCaptureElement.hasPointerCapture?.(dragPointerId)
      ) {
        dragCaptureElement.releasePointerCapture(dragPointerId);
      }
      dragStart = null;
      dragPointerId = null;
      dragCaptureElement = null;
      props.onDraftCancel?.(dragPreviousBox);
      dragPreviousBox = null;
    }

    return () => {
      const size = finiteSize(props.coordinateSize);
      const boxes = props.showBoxes && size
        ? props.boxes.filter((box) => finiteBox(box))
        : [];
      const dim = finiteBox({ bbox: props.dimBox });
      const draft = finiteBox({ bbox: props.draftBox });
      return h("div", {
        class: [
          "bbox-image-panel",
          props.compact ? "is-compact" : "",
          props.dimOutside ? "dim-outside" : "",
        ],
      }, [
        h("img", {
          ref: imageElement,
          id: props.imageId || undefined,
          src: props.imageUrl,
          alt: props.alt,
          draggable: "false",
          loading: props.compact ? "lazy" : "eager",
          decoding: "async",
          onLoad: notifyImageLoaded,
          onError: (event) => {
            settledImageUrl = "";
            props.onImageError?.(event);
          },
        }),
        size && (boxes.length || (props.dimOutside && dim) || props.drawMode || draft)
          ? h("svg", {
              id: props.overlayId || undefined,
              class: [
                "bbox-overlay",
                props.drawMode ? "is-drawing" : "",
              ],
              style: props.drawMode ? { pointerEvents: "auto" } : undefined,
              viewBox: `0 0 ${size.w} ${size.h}`,
              preserveAspectRatio: "xMidYMid meet",
              role: "group",
              "aria-label": `${boxes.length} extraction boxes`,
            }, [
              props.dimOutside && dim
                ? h("path", {
                    class: "bbox-dim-mask",
                    d: `M0 0H${size.w}V${size.h}H0Z M${dim[0]} ${dim[1]}V${dim[3]}H${dim[2]}V${dim[1]}Z`,
                    "fill-rule": "evenodd",
                    fill: "rgba(8,11,17,.68)",
                    "pointer-events": "none",
                  })
                : null,
              ...boxes.map((box, index) => {
                const [x1, y1, x2, y2] = finiteBox(box);
                const visual = styleFor(box);
                const selected = Boolean(box.active);
                const label = box.label || "";
                const fullLabel = box.fullLabel || label;
                const accessibleLabel = fullLabel
                  || `${String(box.kind || box.role || "extraction").replaceAll("_", " ")} box ${index + 1}`;
                const attrs = {};
                for (const [key, value] of Object.entries(box.data || {})) {
                  if (value !== null && value !== undefined) {
                    attrs[`data-${key}`] = String(value);
                  }
                }
                const selectable = box.selectable !== false
                  && typeof props.onBoxSelect === "function";
                return h("g", {
                  key: box.key || index,
                  class: [
                    "bbox-group",
                    "dbox",
                    box.kind || "",
                    box.role || "",
                    selected ? "selected bbox-active" : "",
                    box.dimmed ? "dimmed" : "",
                    box.labelMode === "focus" ? "label-on-focus" : "",
                  ],
                  "data-box-key": box.key || index,
                  "aria-label": selectable ? accessibleLabel : undefined,
                  role: selectable ? "button" : undefined,
                  tabindex: selectable ? 0 : undefined,
                  "pointer-events": box.selectable === false ? "none" : undefined,
                  ...attrs,
                  onMouseenter: () => props.onBoxHover?.(box, index),
                  onMouseleave: () => props.onBoxHover?.(null, index),
                  onClick: (event) => {
                    event.stopPropagation();
                    props.onBoxSelect?.(box, event);
                  },
                  onKeydown: (event) => {
                    if (!selectable || !["Enter", " "].includes(event.key)) return;
                    event.preventDefault();
                    event.stopPropagation();
                    props.onBoxSelect?.(box, event);
                  },
                }, [
                  fullLabel ? h("title", null, fullLabel) : null,
                  h("rect", {
                    x: x1,
                    y: y1,
                    width: Math.max(0.01, x2 - x1),
                    height: Math.max(0.01, y2 - y1),
                    stroke: visual.stroke,
                    fill: visual.fill,
                    "stroke-width": selected ? 4 : box.strokeWidth || 2,
                    "stroke-dasharray": box.dash || undefined,
                    "vector-effect": "non-scaling-stroke",
                    rx: box.radius || 1,
                  }),
                  label ? (() => {
                    // Opaque chip behind the label so it never collides with the page's
                    // own printed caption underneath (the box wraps a scheme whose title
                    // sits exactly where a bare label would land). Sized in image-pixel
                    // space (= the SVG viewBox), monospace char width ~= 0.62 * font-size.
                    const fs = props.compact ? 8 : 10;
                    const chipH = fs + 4;
                    const chipW = label.length * fs * 0.62 + 8;
                    const chipY = Math.max(y1 + 1.5, 1);
                    return h("g", { class: "bbox-label", "pointer-events": "none" }, [
                      h("rect", {
                        x: x1 + 1.5,
                        y: chipY,
                        width: chipW,
                        height: chipH,
                        rx: 2,
                        fill: "rgba(15,17,22,.9)",
                        stroke: box.labelColor || visual.stroke,
                        "stroke-width": 0.75,
                        "vector-effect": "non-scaling-stroke",
                      }),
                      h("text", {
                        x: x1 + 5,
                        y: chipY + chipH - (props.compact ? 3 : 3.5),
                        fill: "#fff",
                        "font-family": "ui-monospace, SFMono-Regular, Menlo, monospace",
                        "font-size": fs,
                        "font-weight": "700",
                        "vector-effect": "non-scaling-stroke",
                      }, label),
                    ]);
                  })() : null,
                ]);
              }),
              draft ? h("g", {
                class: "bbox-draft-group",
                "pointer-events": "none",
              }, [
                h("rect", {
                  x: draft[0],
                  y: draft[1],
                  width: Math.max(0.01, draft[2] - draft[0]),
                  height: Math.max(0.01, draft[3] - draft[1]),
                  stroke: "#c96342",
                  fill: "rgba(201,99,66,.12)",
                  "stroke-width": 4,
                  "vector-effect": "non-scaling-stroke",
                  rx: 1,
                }),
                h("text", {
                  x: draft[0] + 5,
                  y: Math.max(12, draft[1] + 14),
                  fill: "#fff",
                  stroke: "rgba(15,17,22,.95)",
                  "stroke-width": 3,
                  "paint-order": "stroke",
                  "font-family": "ui-monospace, SFMono-Regular, Menlo, monospace",
                  "font-size": 10,
                  "font-weight": "700",
                  "vector-effect": "non-scaling-stroke",
                }, props.draftLabel),
              ]) : null,
              props.drawMode ? h("rect", {
                class: "bbox-draw-capture",
                x: 0,
                y: 0,
                width: size.w,
                height: size.h,
                fill: "transparent",
                tabindex: 0,
                role: "group",
                "aria-label": props.drawLabel,
                onPointerdown: (event) => {
                  if (event.button !== 0) return;
                  const point = pointerInImage(event, size);
                  if (!point) return;
                  event.preventDefault();
                  event.stopPropagation();
                  dragStart = point;
                  dragPreviousBox = finiteBox({ bbox: props.draftBox });
                  dragPointerId = event.pointerId;
                  dragCaptureElement = event.currentTarget;
                  event.currentTarget.setPointerCapture?.(event.pointerId);
                  props.onDraftChange?.([point.x, point.y, point.x, point.y]);
                },
                onPointermove: (event) => {
                  if (!dragStart) return;
                  const point = pointerInImage(event, size);
                  if (!point) return;
                  event.preventDefault();
                  props.onDraftChange?.(normalizedBox(dragStart, point));
                },
                onPointerup: (event) => {
                  if (!dragStart) return;
                  const point = pointerInImage(event, size);
                  const box = normalizedBox(dragStart, point);
                  dragStart = null;
                  event.currentTarget.releasePointerCapture?.(event.pointerId);
                  dragPointerId = null;
                  dragCaptureElement = null;
                  if (!box || box[2] - box[0] < 0.5 || box[3] - box[1] < 0.5) {
                    props.onDraftCancel?.(dragPreviousBox);
                    dragPreviousBox = null;
                    return;
                  }
                  props.onDraftChange?.(box);
                  props.onDraftCommit?.(box);
                  dragPreviousBox = null;
                },
                onPointercancel: cancelDraft,
                onKeydown: (event) => {
                  if (event.key !== "Escape") return;
                  event.preventDefault();
                  cancelDraft();
                },
              }) : null,
            ])
          : null,
      ]);
    };
  },
};
