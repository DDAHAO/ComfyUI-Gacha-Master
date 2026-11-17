// Container-driven layout helpers. CSS container queries do the visual work;
// this module supplies the same breakpoints as a data attribute for older
// hosts, JavaScript-only labels and unit tests.

export const LAYOUT_BREAKPOINTS = { narrow: 480, medium: 720, wide: 800 };

export function containerSize(width) {
  const value = Number(width) || 0;
  if (value < LAYOUT_BREAKPOINTS.narrow) return "narrow";
  if (value < LAYOUT_BREAKPOINTS.medium) return "medium";
  return "wide";
}

export function modalSize(width) {
  const value = Number(width) || 0;
  if (value < 480) return "narrow";
  if (value < LAYOUT_BREAKPOINTS.wide) return "stacked";
  return "wide";
}

export function watchContainerWidth(element, onChange, { initial = true } = {}) {
  if (!element) return () => {};
  let last = "";
  const apply = () => {
    const width = Math.round(element.getBoundingClientRect().width || element.clientWidth || 0);
    const size = containerSize(width);
    element.dataset.gmSize = size;
    if (initial || size !== last) {
      last = size;
      onChange?.(size, width);
    }
  };
  let observer = null;
  if (typeof ResizeObserver === "function") {
    observer = new ResizeObserver(apply);
    observer.observe(element);
  } else {
    window.addEventListener("resize", apply, { passive: true });
  }
  requestAnimationFrame(apply);
  return () => {
    if (observer) observer.disconnect();
    else window.removeEventListener("resize", apply);
    delete element.dataset.gmSize;
  };
}
