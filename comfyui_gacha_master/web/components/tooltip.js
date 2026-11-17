// Small supplemental tooltip. The deciding information also stays in the
// page body; this layer only adds the "why" on hover/focus.

import { h } from "../dom.js";

let active = null;
let seq = 0;

function hideActive() {
  active?.hide();
}

export function attachTooltip(element, { content, delay = 350 } = {}) {
  if (!element || !content) return () => {};
  const id = `gm-tooltip-${++seq}`;
  const originalTitle = element.getAttribute("title");
  if (originalTitle) element.removeAttribute("title");
  const originalDescribedBy = element.getAttribute("aria-describedby") || "";
  const describedBy = originalDescribedBy ? `${originalDescribedBy} ${id}` : id;
  element.setAttribute("aria-describedby", describedBy);

  let timer = null;
  let node = null;
  let visible = false;

  function position() {
    if (!node) return;
    const rect = element.getBoundingClientRect();
    const tipRect = node.getBoundingClientRect();
    const margin = 8;
    let left = rect.left + rect.width / 2 - tipRect.width / 2;
    left = Math.max(margin, Math.min(left, window.innerWidth - tipRect.width - margin));
    let top = rect.top - tipRect.height - margin;
    if (top < margin) top = rect.bottom + margin;
    node.style.left = `${Math.round(left)}px`;
    node.style.top = `${Math.round(top)}px`;
  }

  function show() {
    if (visible || !element.isConnected) return;
    hideActive();
    node = h("div", { class: "gm-tooltip", id, role: "tooltip" }, content);
    document.body.appendChild(node);
    visible = true;
    active = { hide };
    requestAnimationFrame(() => {
      if (!visible) return;
      position();
      node?.classList.add("visible");
    });
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
  }

  function hide() {
    clearTimeout(timer);
    timer = null;
    if (!visible) return;
    visible = false;
    node?.remove();
    node = null;
    if (active?.hide === hide) active = null;
    window.removeEventListener("scroll", hide, true);
    window.removeEventListener("resize", hide);
  }

  function scheduleShow() {
    clearTimeout(timer);
    timer = setTimeout(show, delay);
  }

  function onKeyDown(event) {
    if (event.key === "Escape" && visible) {
      event.stopPropagation();
      hide();
    }
  }

  element.addEventListener("pointerenter", scheduleShow);
  element.addEventListener("pointerleave", hide);
  element.addEventListener("focus", scheduleShow);
  element.addEventListener("blur", hide);
  document.addEventListener("keydown", onKeyDown, true);

  return function destroy() {
    hide();
    element.removeEventListener("pointerenter", scheduleShow);
    element.removeEventListener("pointerleave", hide);
    element.removeEventListener("focus", scheduleShow);
    element.removeEventListener("blur", hide);
    document.removeEventListener("keydown", onKeyDown, true);
    element.setAttribute("aria-describedby", originalDescribedBy);
    if (originalTitle) element.setAttribute("title", originalTitle);
  };
}

export function dismissTooltip() {
  hideActive();
}
