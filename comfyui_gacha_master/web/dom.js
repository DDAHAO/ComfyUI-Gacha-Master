// Small DOM helpers. User-provided text is always inserted via textContent;
// no filename, path or error string is ever interpreted as HTML.

import { watchContainerWidth } from "./layout.js";
//
// The modal helper is the single close lifecycle for every dialog: header
// button, Escape, backdrop, navigation and programmatic close all take the
// same path, so views can clean up resources from one onClose callback.

export function h(tag, props = null, ...children) {
  const element = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class" || key === "className") element.className = value;
      else if (key === "style" && typeof value === "object") Object.assign(element.style, value);
      else if (key === "dataset") Object.assign(element.dataset, value);
      else if (key === "attrs") {
        for (const [name, attr] of Object.entries(value)) {
          if (attr === false || attr === null || attr === undefined) continue;
          element.setAttribute(name, attr === true ? "" : String(attr));
        }
      } else if (key.startsWith("on") && typeof value === "function") {
        element.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === "value") {
        element.value = value;
      } else if (key in element && key !== "list") {
        element[key] = value;
      } else {
        element.setAttribute(key, String(value));
      }
    }
  }
  append(element, children);
  return element;
}

function append(element, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(element, child);
    else if (child instanceof Node) element.appendChild(child);
    else element.appendChild(document.createTextNode(String(child)));
  }
}

export function text(value) { return document.createTextNode(value == null ? "" : String(value)); }
export function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
export function frag(...children) { const f = document.createDocumentFragment(); append(f, children); return f; }

export function fmtTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

export function fmtClock(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleTimeString();
}

export function fmtBytes(value) {
  const n = Number(value || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MiB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GiB`;
}

// Backend counters are authoritative. A missing field is "not read", never 0.
export function count(value, placeholder = "—") {
  return value === undefined || value === null || value === "" ? placeholder : String(value);
}

export const JOB_STATUS = {
  CREATING: "创建中",
  READY: "就绪",
  RUNNING: "运行中",
  PAUSING: "正在暂停",
  PAUSED: "已暂停",
  BLOCKED: "已阻断",
  RECOVERY_REQUIRED: "需要核验",
  COMPLETED: "已完成",
};

export const RUN_INTENT = {
  STOPPED: "已停止",
  RUN: "允许运行",
  PAUSE_SAFE: "安全暂停中",
  PAUSE_NOW: "立即暂停中",
};

export const REVIEW_STATUS = {
  unreviewed: "未审核",
  available_unreviewed: "未审核",
  viewed_no_selection: "已查看无入选",
  selected_pending_review: "有入选待确认",
  reviewed: "本版本审核完成",
};

export function badge(label, kind = "") {
  return h("span", { class: `gm-badge ${kind}` }, label);
}

export function statusBadge(status) {
  const kind = {
    COMPLETED: "ok", RUNNING: "run", READY: "ready", PAUSING: "warn", PAUSED: "muted",
    BLOCKED: "bad", RECOVERY_REQUIRED: "bad", CREATING: "muted",
  }[status] || "";
  return badge(JOB_STATUS[status] || status, kind);
}

export function button(label, onClick, { kind = "", disabled = false, title = "", type = "button" } = {}) {
  return h("button", {
    class: `gm-btn ${kind}`,
    type,
    disabled,
    title: title || undefined,
    onclick: onClick,
  }, label);
}

export function labeled(label, control, help, helpId) {
  return h("label", { class: "gm-field" },
    h("span", { class: "gm-field-label" }, label),
    control,
    help ? h("span", { class: "gm-help", id: helpId || undefined }, help) : null,
  );
}

export function select(options, value, onChange, { disabled = false } = {}) {
  const el = h("select", { disabled, onchange: (event) => onChange?.(event.target.value) });
  for (const option of options) {
    el.appendChild(h("option", { value: option.value, selected: String(option.value) === String(value) }, option.label));
  }
  return el;
}

// --------------------------------------------------------------- modals
// Only the top-most modal reacts to Escape/Tab. Callers get exactly one
// onClose notification regardless of how the dialog was closed.
const modalStack = [];

export function isTopModal(dialog) {
  return modalStack.length > 0 && modalStack[modalStack.length - 1].dialog === dialog;
}

export function hasOpenModal() {
  return modalStack.length > 0;
}

export function closeAllModals(reason = "navigation") {
  for (const entry of [...modalStack].reverse()) entry.close(reason);
}

function focusableElements(dialog) {
  const selector = [
    "a[href]",
    "button:not([disabled])",
    "input:not([disabled]):not([type='hidden'])",
    "select:not([disabled])",
    "textarea:not([disabled])",
    "[tabindex]:not([tabindex='-1'])",
  ].join(",");
  return [...dialog.querySelectorAll(selector)].filter((element) => {
    if (element.hidden) return false;
    if (element.getClientRects().length === 0) return false;
    const style = window.getComputedStyle(element);
    return style.visibility !== "hidden" && style.display !== "none";
  });
}

export function modal({ title, wide = false, onClose, initialFocus, description } = {}) {
  const previousActive = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const root = h("div", { class: "gm-modal-overlay", role: "presentation" },
    h("div", {
      class: `gm-modal ${wide ? "wide" : ""}`,
      role: "dialog",
      "aria-modal": "true",
      "aria-label": title || "对话框",
      tabindex: "-1",
    }),
  );
  const dialog = root.firstChild;
  if (description) dialog.setAttribute("aria-description", description);
  const header = h("div", { class: "gm-modal-header" },
    h("h3", null, title || ""),
    h("button", { class: "gm-icon-btn gm-modal-close", type: "button", title: "关闭", "aria-label": "关闭对话框", onclick: () => close("button") }, "×"),
  );
  const body = h("div", { class: "gm-modal-body" });
  const footer = h("div", { class: "gm-modal-footer" });
  dialog.append(header, body, footer);

  let closed = false;
  let pointerDownOnBackdrop = false;
  let initialFocusDone = false;
  const entry = { dialog, close };
  const stopModalWatch = watchContainerWidth(body);

  function focusInitial() {
    const preferred = initialFocus?.();
    if (preferred && dialog.contains(preferred)) {
      preferred.focus();
      return;
    }
    const auto = dialog.querySelector("[data-autofocus]");
    if (auto instanceof HTMLElement) { auto.focus(); return; }
    const first = focusableElements(dialog)[0];
    if (first) first.focus();
    else dialog.focus();
  }

  function restoreFocus() {
    if (previousActive && previousActive.isConnected && typeof previousActive.focus === "function") {
      previousActive.focus();
      return;
    }
    const fallback = document.querySelector(".gacha-master-host .gm-body");
    if (fallback instanceof HTMLElement) {
      fallback.setAttribute("tabindex", "-1");
      fallback.focus();
    }
  }

  function trapTab(event) {
    const items = focusableElements(dialog);
    if (!items.length) {
      event.preventDefault();
      dialog.focus();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (event.shiftKey) {
      if (active === first || !dialog.contains(active)) {
        event.preventDefault();
        last.focus();
      }
    } else if (active === last || !dialog.contains(active)) {
      event.preventDefault();
      first.focus();
    }
  }

  function onKey(event) {
    if (!isTopModal(dialog) || event.defaultPrevented) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close("escape");
    } else if (event.key === "Tab") {
      trapTab(event);
    }
  }

  function close(reason = "program") {
    if (closed) return;
    closed = true;
    const index = modalStack.indexOf(entry);
    if (index >= 0) modalStack.splice(index, 1);
    document.removeEventListener("keydown", onKey, true);
    root.removeEventListener("pointerdown", onBackdropDown);
    root.removeEventListener("pointerup", onBackdropUp);
    stopModalWatch();
    root.remove();
    try {
      onClose?.(reason);
    } finally {
      restoreFocus();
    }
  }

  function onBackdropDown(event) {
    pointerDownOnBackdrop = event.target === root;
  }

  function onBackdropUp(event) {
    if (pointerDownOnBackdrop && event.target === root) close("backdrop");
    pointerDownOnBackdrop = false;
  }

  root.addEventListener("pointerdown", onBackdropDown);
  root.addEventListener("pointerup", onBackdropUp);
  document.addEventListener("keydown", onKey, true);
  modalStack.push(entry);
  document.body.appendChild(root);
  // RequestAnimationFrame is paused while the host pane is hidden/backgrounded.
  // A timer guarantees that modal focus still lands inside the dialog.
  const focusOnce = () => {
    if (closed || initialFocusDone) return;
    initialFocusDone = true;
    focusInitial();
  };
  const focusTimer = setTimeout(focusOnce, 0);
  const frame = requestAnimationFrame(focusOnce);
  // A concurrent render (for example a selection save) can move focus back
  // to a card after the dialog's first focus pass.  The modal is still open,
  // so re-assert focus inside it shortly after the first attempt.
  const forceFocus = () => {
    if (closed || dialog.contains(document.activeElement)) return;
    initialFocusDone = false;
    focusInitial();
  };
  setTimeout(forceFocus, 150);
  setTimeout(forceFocus, 500);
  const apiClose = (reason) => {
    clearTimeout(focusTimer);
    cancelAnimationFrame(frame);
    close(reason);
  };
  entry.close = apiClose;
  return {
    root,
    dialog,
    header,
    body,
    footer,
    close: apiClose,
    isOpen: () => !closed,
  };
}

export function confirmDialog(message, { title = "请确认", danger = false, confirmLabel = "确认", cancelLabel = "取消" } = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const dialog = modal({
      title,
      wide: false,
      onClose: () => finish(false),
      // Deferred until after the buttons are appended.
      initialFocus: () => dialog.footer.querySelector("[data-gm-dialog-confirm='1']"),
      // Stable hooks for tests and for browsers without :has().
      description: message,
    });
    dialog.body.appendChild(h("p", { class: "gm-confirm-text" }, message));
    const cancel = button(cancelLabel, () => closeWith(false), { kind: "muted" });
    const confirm = button(confirmLabel, () => closeWith(true), { kind: danger ? "danger" : "primary" });
    cancel.dataset.gmDialogCancel = "1";
    confirm.dataset.gmDialogConfirm = "1";
    dialog.footer.append(cancel, confirm);
    function closeWith(value) {
      // Settle before closing so the modal's onClose cannot overwrite us.
      finish(value);
      dialog.close("confirm");
    }
  });
}

// Legacy helper retained for call sites outside the app shell. New code uses
// the toast stack; this one is bounded by the caller and has no timers left
// behind after the host is removed.
export function toast(host, message, kind = "info", timeout = 5000) {
  if (!host) return;
  const item = h("div", { class: `gm-toast ${kind}` }, message);
  host.appendChild(item);
  setTimeout(() => item.remove(), timeout);
}

export function progressBar(value, total, label) {
  const n = Number(value) || 0;
  const t = Number(total) || 0;
  const pct = t > 0 ? Math.min(100, Math.max(0, (n / t) * 100)) : 0;
  return h("div", { class: "gm-progress-wrap" },
    h("div", {
      class: "gm-progress",
      role: "progressbar",
      "aria-valuemin": "0",
      "aria-valuemax": String(Math.max(0, t)),
      "aria-valuenow": String(t > 0 ? Math.max(0, Math.min(n, t)) : 0),
      "aria-label": label || `${n} / ${t}`,
    }, h("span", { style: { width: `${pct}%` } })),
    h("span", { class: "gm-progress-label" }, label || `${n} / ${t}`),
  );
}

export function emptyState(message, action) {
  return h("div", { class: "gm-empty" }, h("p", null, message), action || null);
}

export function loadingState(message = "加载中…") {
  return h("div", { class: "gm-loading", role: "status" }, message);
}

export function image(src, alt, className = "", onClick) {
  return h("img", {
    class: className,
    src,
    alt: alt || "",
    loading: "lazy",
    decoding: "async",
    onclick: onClick,
  });
}

export function errorBox(error) {
  return h("div", { class: "gm-error-box", role: "alert" },
    h("strong", null, error?.code || "错误"),
    h("p", null, error?.message || String(error)),
    error?.traceId ? h("code", null, error.traceId) : null,
    error?.retryable ? h("p", { class: "gm-help" }, "该错误可直接重试。") : null,
  );
}

export function spinner(label = "处理中…") {
  return h("span", { class: "gm-spinner", role: "status", "aria-label": label },
    h("span", { class: "gm-spinner-dot", "aria-hidden": "true" }), label);
}
