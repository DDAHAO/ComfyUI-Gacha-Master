// File dropzone. Visual requirements only: it does not upload, decode or
// invent progress. Files are handed to the caller, which owns ordering,
// concurrency and the real upload API.

import { h, clear } from "../dom.js";

export function createDropzone({
  accept = "image/png,image/jpeg,image/webp",
  multiple = true,
  onFiles,
  onFolderDropped,
  onOpenReference,
  onActivate,
} = {}) {
  const input = h("input", {
    type: "file",
    accept,
    multiple,
    class: "gm-dropzone-input",
    tabindex: "-1",
    "aria-hidden": "true",
  });
  const icon = h("span", { class: "gm-dropzone-icon", "aria-hidden": "true" }, "⇩");
  const title = h("span", { class: "gm-dropzone-title" }, "点击选择，或把图片拖到这里");
  const hint = h("span", { class: "gm-dropzone-hint" }, "支持 PNG、JPEG、WebP 静态图片");
  const status = h("span", { class: "gm-dropzone-status", role: "status", "aria-live": "polite" });
  const button = h("button", { class: "gm-dropzone", type: "button" },
    icon, title, hint, status, input,
  );
  let dragDepth = 0;
  let mode = "upload";
  let disabled = false;

  function update({ mode: nextMode = mode, disabled: nextDisabled = disabled, title: nextTitle, hint: nextHint, status: nextStatus, tone = "", counts } = {}) {
    mode = nextMode;
    disabled = Boolean(nextDisabled);
    button.disabled = disabled;
    button.setAttribute("aria-disabled", disabled ? "true" : "false");
    button.classList.toggle("reference", mode === "reference");
    button.classList.toggle("disabled", disabled);
    button.classList.toggle("has-error", tone === "error");
    if (nextTitle) title.textContent = nextTitle;
    if (nextHint) hint.textContent = nextHint;
    else if (mode === "reference") hint.textContent = "引用模式只使用后端可访问的受控目录；浏览器上传不可用。";
    else hint.textContent = "支持 PNG、JPEG、WebP 静态图片";
    if (nextStatus !== undefined) status.textContent = nextStatus;
    if (counts) {
      const parts = [];
      if (counts.queued) parts.push(`${counts.queued} 个排队`);
      if (counts.uploading) parts.push(`${counts.uploading} 个上传与校验中`);
      if (counts.ready) parts.push(`${counts.ready} 个已就绪`);
      if (counts.error) parts.push(`${counts.error} 个失败`);
      status.textContent = parts.join(" · ");
      status.classList.toggle("bad", Boolean(counts.error));
    }
  }

  function openPicker() {
    if (disabled) return;
    if (mode === "reference") {
      onOpenReference?.();
      return;
    }
    input.click();
  }

  function setDragging(active) {
    button.classList.toggle("dragover", active);
  }

  button.addEventListener("click", () => {
    if (mode === "reference") onOpenReference?.();
    else openPicker();
    onActivate?.();
  });
  input.addEventListener("change", () => {
    const files = Array.from(input.files || []);
    input.value = "";
    if (files.length) onFiles?.(files);
  });

  button.addEventListener("dragenter", (event) => {
    if (disabled) return;
    event.preventDefault();
    event.stopPropagation();
    dragDepth += 1;
    setDragging(true);
  });
  button.addEventListener("dragover", (event) => {
    if (disabled) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    setDragging(true);
  });
  button.addEventListener("dragleave", (event) => {
    if (disabled) return;
    // Child transitions fire dragleave as well; only an actual exit clears the
    // highlight. relatedTarget containment covers hosts that omit dragenter.
    if (event.relatedTarget && button.contains(event.relatedTarget)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) setDragging(false);
  });
  button.addEventListener("drop", (event) => {
    event.preventDefault();
    event.stopPropagation();
    dragDepth = 0;
    setDragging(false);
    if (disabled) return;
    const transfer = event.dataTransfer;
    if (!transfer) return;
    const entries = Array.from(transfer.items || []);
    const hasFolder = entries.some((item) => {
      const entry = item.webkitGetAsEntry?.();
      return Boolean(entry?.isDirectory);
    });
    if (hasFolder) onFolderDropped?.();
    const files = Array.from(transfer.files || []);
    if (files.length) onFiles?.(files, { hasFolder });
  });

  return {
    element: button,
    input,
    update,
    openPicker,
    destroy() {
      button.remove();
    },
  };
}
