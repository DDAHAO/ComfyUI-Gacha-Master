// Local skeletons. Shapes approximate the real cards so the first paint does
// not jump when data arrives. Only the visible area gets placeholders.

import { h } from "../dom.js";

export function skeletonBlock({ height = 12, width = "100%", radius = 6, className = "" } = {}) {
  return h("span", {
    class: `gm-skeleton ${className}`,
    style: {
      display: "block",
      height: typeof height === "number" ? `${height}px` : height,
      width,
      borderRadius: `${radius}px`,
    },
    "aria-hidden": "true",
  });
}

export function skeletonJobCard() {
  return h("div", { class: "gm-skeleton-card gm-skeleton-job", "aria-hidden": "true" },
    h("div", { class: "gm-skeleton-row" }, skeletonBlock({ width: "48%", height: 16 }), skeletonBlock({ width: 62, height: 18 })),
    h("div", { class: "gm-skeleton-row" }, skeletonBlock({ width: "30%" }), skeletonBlock({ width: "22%" }), skeletonBlock({ width: "18%" })),
    skeletonBlock({ height: 7, radius: 99 }),
    skeletonBlock({ width: "35%", height: 10 }),
  );
}

export function skeletonSourceRow() {
  return h("div", { class: "gm-skeleton-source", "aria-hidden": "true" },
    h("div", { class: "gm-skeleton-source-head" },
      skeletonBlock({ width: 64, height: 14 }),
      skeletonBlock({ width: 72, height: 72, radius: 8 }),
      h("div", { class: "gm-skeleton-source-info" },
        skeletonBlock({ width: "46%", height: 14 }),
        skeletonBlock({ width: "62%", height: 10 }),
        skeletonBlock({ width: "38%", height: 10 }),
      ),
    ),
    h("div", { class: "gm-skeleton-thumbs" }, ...Array.from({ length: 6 }, () => skeletonBlock({ height: 92, radius: 8 }))),
  );
}

export function skeletonResultStrip() {
  return h("div", { class: "gm-skeleton-thumbs", "aria-hidden": "true" },
    ...Array.from({ length: 5 }, () => skeletonBlock({ height: 92, radius: 8 })));
}

// Delay first display so very fast responses never flash placeholders.
export function createDelayedSkeleton(factory, { delay = 150 } = {}) {
  const host = h("div", { class: "gm-skeleton-host", "aria-busy": "true", "aria-live": "polite" });
  let timer = setTimeout(() => {
    if (!destroyed) host.appendChild(factory());
  }, delay);
  let destroyed = false;
  function destroy() {
    destroyed = true;
    clearTimeout(timer);
    timer = null;
    host.remove();
  }
  function cancel() {
    clearTimeout(timer);
    timer = null;
  }
  return { element: host, cancel, destroy };
}

export function skeletonList(factory, count = 3, options = {}) {
  const wrap = h("div", { class: "gm-skeleton-list", "aria-hidden": "true" });
  for (let index = 0; index < count; index += 1) wrap.appendChild(factory());
  return wrap;
}
