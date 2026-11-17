// Bounded notification stack. Real messages only; no demo rotation and no
// replacement messages when one is dismissed. The visible list is capped so a
// burst of selection saves cannot push the whole screen away.

import { h, clear } from "../dom.js";

export const TOAST_LIMIT = 3;
export const TOAST_QUEUE_LIMIT = 12;

const DURATION = { ok: 5000, info: 6000, warn: 9000, error: 0 };

// Pure queue logic, kept free of DOM so it can be unit tested.
export class ToastQueue {
  constructor({ limit = TOAST_LIMIT, queueLimit = TOAST_QUEUE_LIMIT } = {}) {
    this.limit = limit;
    this.queueLimit = queueLimit;
    this.visible = [];
    this.queued = [];
  }

  push(item) {
    const existing = this.visible.find((entry) => entry.id === item.id);
    if (existing) {
      existing.message = item.message;
      existing.kind = item.kind;
      existing.repeat = (existing.repeat || 0) + 1;
      return { type: "update", item: existing };
    }
    const alreadyQueued = this.queued.find((entry) => entry.id === item.id);
    if (alreadyQueued) {
      alreadyQueued.message = item.message;
      alreadyQueued.kind = item.kind;
      alreadyQueued.repeat = (alreadyQueued.repeat || 0) + 1;
      return { type: "update", item: alreadyQueued };
    }
    if (this.visible.length < this.limit) {
      this.visible.push(item);
      return { type: "show", item };
    }
    this.queued.push(item);
    while (this.queued.length > this.queueLimit) this.queued.shift();
    return { type: "queue", item };
  }

  dismiss(id) {
    const index = this.visible.findIndex((entry) => entry.id === id);
    if (index < 0) return { type: "missing" };
    this.visible.splice(index, 1);
    const next = this.queued.shift();
    if (next) {
      this.visible.push(next);
      return { type: "promote", item: next };
    }
    return { type: "dismissed" };
  }
}

export function createToastStack({ portalRoot = null } = {}) {
  const element = h("div", {
    class: "gm-toast-stack",
    role: "region",
    "aria-label": "通知",
  });
  const root = portalRoot || h("div", { class: "gm-toast-portal" });
  root.appendChild(element);
  if (!portalRoot) document.body.appendChild(root);

  const queue = new ToastQueue();
  const nodes = new Map();
  const timers = new Map();
  let destroyed = false;
  let seq = 0;

  function durationFor(kind) {
    return DURATION[kind] ?? DURATION.info;
  }

  function clearTimer(id) {
    const timer = timers.get(id);
    if (timer) clearTimeout(timer);
    timers.delete(id);
  }

  function armTimer(item) {
    clearTimer(item.id);
    const duration = durationFor(item.kind);
    if (item.sticky || !duration || destroyed) return;
    timers.set(item.id, setTimeout(() => dismiss(item.id), duration));
  }

  function buildNode(item) {
    const liveRole = item.kind === "error" ? "alert" : "status";
    const node = h("div", {
      class: `gm-toast ${item.kind || "info"}`,
      dataset: { toastId: item.id },
    });
    const message = h("div", { class: "gm-toast-message", role: liveRole, "aria-live": item.kind === "error" ? "assertive" : "polite" }, item.message);
    if (item.detail) message.appendChild(h("p", { class: "gm-toast-detail" }, item.detail));
    const close = h("button", {
      class: "gm-toast-close",
      type: "button",
      title: "关闭通知",
      "aria-label": "关闭通知",
      onclick: () => dismiss(item.id),
    }, "×");
    node.append(message, close);
    node.addEventListener("pointerenter", () => clearTimer(item.id));
    node.addEventListener("pointerleave", () => armTimer(item));
    node.addEventListener("focusin", () => clearTimer(item.id));
    node.addEventListener("focusout", (event) => {
      if (!node.contains(event.relatedTarget)) armTimer(item);
    });
    if (item.actionLabel && typeof item.onAction === "function") {
      node.querySelector(".gm-toast-message")?.appendChild(
        h("button", {
          class: "gm-toast-action",
          type: "button",
          onclick: () => {
            try { item.onAction(); } finally { dismiss(item.id); }
          },
        }, item.actionLabel),
      );
    }
    return node;
  }

  function show(message, kind = "info", options = {}) {
    if (destroyed) return null;
    const id = options.id || `toast-${++seq}`;
    const item = {
      id,
      message: String(message ?? ""),
      detail: options.detail ? String(options.detail) : "",
      kind: DURATION[kind] !== undefined ? kind : "info",
      sticky: options.sticky ?? kind === "error",
      actionLabel: options.actionLabel || "",
      onAction: options.onAction || null,
    };
    const result = queue.push(item);
    if (result.type === "show" || result.type === "promote") {
      const node = buildNode(result.item);
      nodes.set(result.item.id, node);
      element.appendChild(node);
      armTimer(result.item);
    } else if (result.type === "update") {
      const node = nodes.get(result.item.id);
      if (node) {
        const label = node.querySelector(".gm-toast-message");
        if (label) {
          const countText = result.item.repeat > 1 ? `（×${result.item.repeat}）` : "";
          label.textContent = result.item.message + countText;
          if (result.item.detail) label.appendChild(h("p", { class: "gm-toast-detail" }, result.item.detail));
        }
        node.className = `gm-toast ${result.item.kind || "info"}`;
        armTimer(result.item);
      }
    }
    return id;
  }

  function dismiss(id) {
    clearTimer(id);
    const result = queue.dismiss(id);
    const node = nodes.get(id);
    if (node) node.remove();
    nodes.delete(id);
    if (result.type === "promote") {
      const promoted = buildNode(result.item);
      nodes.set(result.item.id, promoted);
      element.appendChild(promoted);
      armTimer(result.item);
    }
    return result.type;
  }

  function clearAll() {
    for (const id of [...timers.keys()]) clearTimer(id);
    nodes.clear();
    queue.visible = [];
    queue.queued = [];
    clear(element);
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    clearAll();
    element.remove();
    if (!portalRoot) root.remove();
  }

  return { element, show, dismiss, clearAll, destroy, queue };
}
