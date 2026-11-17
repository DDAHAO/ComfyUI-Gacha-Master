// Layered image comparison for one SRC and one output. Both images are drawn
// with the same contain geometry; only a clip mask on the top layer changes.
// The component never uploads, selects or fetches: URLs come from the caller.

import { h, clear } from "../dom.js";

export const RATIO_TOLERANCE = 0.005;

export function ratiosMatch(sourceWidth, sourceHeight, resultWidth, resultHeight, tolerance = RATIO_TOLERANCE) {
  const sw = Number(sourceWidth) || 0;
  const sh = Number(sourceHeight) || 0;
  const rw = Number(resultWidth) || 0;
  const rh = Number(resultHeight) || 0;
  if (sw <= 0 || sh <= 0 || rw <= 0 || rh <= 0) return false;
  const left = sw * rh;
  const right = rw * sh;
  const scale = Math.max(left, right);
  if (scale <= 0) return false;
  return Math.abs(left - right) / scale <= tolerance;
}

export function clampPosition(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0.5;
  return Math.max(0, Math.min(1, n));
}

function withRetry(url, retry) {
  if (!retry) return url;
  try {
    const base = new URL(url, window.location.href);
    base.searchParams.set("gm_retry", String(retry));
    return base.toString();
  } catch {
    return `${url}${url.includes("?") ? "&" : "?"}gm_retry=${retry}`;
  }
}

export function createImageCompare({
  sourceUrl,
  resultUrl,
  sourceLabel = "原图",
  resultLabel = "生成结果",
  position = 0.5,
  onUnavailable,
  onPositionChange,
  onStatus,
} = {}) {
  let source = sourceUrl;
  let result = resultUrl;
  let generation = 0;
  let ratioOk = true;
  let loadState = "loading"; // loading | ready | error | mismatch
  let dragging = false;
  let pointerId = null;
  let retryCount = 0;

  const sourceImg = h("img", { class: "gm-imagecompare-img", alt: sourceLabel, draggable: false, decoding: "async" });
  const resultImg = h("img", { class: "gm-imagecompare-img", alt: resultLabel, draggable: false, decoding: "async" });
  const sourceLayer = h("div", { class: "gm-imagecompare-layer top" }, sourceImg);
  const resultLayer = h("div", { class: "gm-imagecompare-layer base" }, resultImg);
  const divider = h("div", { class: "gm-imagecompare-divider", "aria-hidden": "true" });
  const handle = h("div", {
    class: "gm-imagecompare-handle",
    role: "slider",
    tabindex: "0",
    "aria-label": "对比分割线位置",
    "aria-valuemin": "0",
    "aria-valuemax": "100",
    "aria-valuenow": "50",
    "aria-valuetext": "50% 原图",
    "aria-orientation": "horizontal",
  }, h("span", { class: "gm-imagecompare-grip", "aria-hidden": "true" }, "⇔"));
  const labels = h("div", { class: "gm-imagecompare-labels", "aria-hidden": "true" },
    h("span", { class: "gm-imagecompare-label left" }, sourceLabel),
    h("span", { class: "gm-imagecompare-label right" }, resultLabel),
  );
  const notice = h("div", { class: "gm-imagecompare-notice", role: "status" });
  const stage = h("div", { class: "gm-imagecompare-stage" },
    resultLayer, sourceLayer, divider, handle, labels, notice,
  );
  const element = h("div", { class: "gm-imagecompare", dataset: { state: "loading" } }, stage);

  function setPosition(value, { emit = false } = {}) {
    const next = clampPosition(value);
    const pct = next * 100;
    divider.style.left = `${pct}%`;
    handle.style.left = `${pct}%`;
    handle.setAttribute("aria-valuenow", String(Math.round(pct)));
    handle.setAttribute("aria-valuetext", `${Math.round(pct)}% 原图 / ${100 - Math.round(pct)}% 结果`);
    sourceLayer.style.clipPath = `inset(0 ${100 - pct}% 0 0)`;
    if (emit) onPositionChange?.(next);
    return next;
  }

  function setStatus(state, message = "") {
    loadState = state;
    element.dataset.state = state;
    clear(notice);
    if (message) notice.textContent = message;
    notice.hidden = !message;
    handle.classList.toggle("disabled", state !== "ready");
    handle.setAttribute("aria-disabled", state === "ready" ? "false" : "true");
    if (state !== "ready") handle.tabIndex = -1;
    else handle.tabIndex = 0;
    onStatus?.(state, message);
  }

  function fail(reason, message, side = "") {
    setStatus(reason, message);
    onUnavailable?.(reason, { side, sourceUrl: source, resultUrl: result });
  }

  function retry() {
    retryCount += 1;
    startLoading();
  }

  function startLoading() {
    generation += 1;
    const token = generation;
    setStatus("loading", "正在读取两张图片…");
    sourceImg.removeAttribute("src");
    resultImg.removeAttribute("src");
    sourceImg.src = withRetry(source, retryCount);
    resultImg.src = withRetry(result, retryCount);
    const pending = new Set(["source", "result"]);
    const failures = [];
    function settle(side, ok) {
      if (token !== generation) return;
      if (!ok) failures.push(side);
      pending.delete(side);
      if (pending.size) return;
      if (failures.length) {
        fail("error", failures.includes("source") ? "原图加载失败；文件身份仍保留，请重读。" : "生成结果加载失败；不会用空块冒充结果。", failures[0]);
        return;
      }
      const sw = sourceImg.naturalWidth;
      const sh = sourceImg.naturalHeight;
      const rw = resultImg.naturalWidth;
      const rh = resultImg.naturalHeight;
      ratioOk = ratiosMatch(sw, sh, rw, rh);
      if (!ratioOk) {
        fail("mismatch", "图片比例不同，请使用并排对照。");
        return;
      }
      setStatus("ready", "");
      element.dataset.sourceSize = `${sw}x${sh}`;
      element.dataset.resultSize = `${rw}x${rh}`;
    }
    sourceImg.addEventListener("load", () => settle("source", true), { once: true });
    resultImg.addEventListener("load", () => settle("result", true), { once: true });
    sourceImg.addEventListener("error", () => settle("source", false), { once: true });
    resultImg.addEventListener("error", () => settle("result", false), { once: true });
  }

  // ------------------------------------------------------------- pointer
  function onPointerDown(event) {
    if (loadState !== "ready" || event.button !== 0) return;
    dragging = true;
    pointerId = event.pointerId;
    handle.setPointerCapture?.(event.pointerId);
    handle.classList.add("dragging");
    handle.focus({ preventScroll: true });
    event.preventDefault();
    updateFromClientX(event.clientX);
  }

  function onPointerMove(event) {
    if (!dragging || event.pointerId !== pointerId) return;
    updateFromClientX(event.clientX);
  }

  function onPointerUp(event) {
    if (!dragging || (pointerId !== null && event.pointerId !== pointerId)) return;
    dragging = false;
    pointerId = null;
    handle.classList.remove("dragging");
    try { handle.releasePointerCapture?.(event.pointerId); } catch { /* already released */ }
  }

  function updateFromClientX(clientX) {
    const rect = stage.getBoundingClientRect();
    if (!rect.width) return;
    setPosition((clientX - rect.left) / rect.width, { emit: true });
  }

  handle.addEventListener("pointerdown", onPointerDown);
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerUp);
  window.addEventListener("pointercancel", onPointerUp);

  // ------------------------------------------------------------- keyboard
  function onKeyDown(event) {
    if (loadState !== "ready") return;
    let next = null;
    const step = event.shiftKey ? 0.1 : 0.01;
    if (event.key === "ArrowLeft") next = parseFloat(handle.getAttribute("aria-valuenow")) / 100 - step;
    else if (event.key === "ArrowRight") next = parseFloat(handle.getAttribute("aria-valuenow")) / 100 + step;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = 1;
    else if (event.key === "PageUp") next = parseFloat(handle.getAttribute("aria-valuenow")) / 100 - 0.1;
    else if (event.key === "PageDown") next = parseFloat(handle.getAttribute("aria-valuenow")) / 100 + 0.1;
    else if (event.key === " " || event.code === "Space" || event.key === "Enter") {
      // Space on the slider must not reach document-level selection.
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    if (next === null) return;
    event.preventDefault();
    event.stopPropagation();
    setPosition(next, { emit: true });
  }
  handle.addEventListener("keydown", onKeyDown);

  function update({ sourceUrl: nextSource, resultUrl: nextResult, position: nextPosition } = {}) {
    let reload = false;
    if (nextSource !== undefined && nextSource !== source) { source = nextSource; reload = true; }
    if (nextResult !== undefined && nextResult !== result) { result = nextResult; reload = true; }
    if (reload) {
      retryCount = 0;
      startLoading();
    }
    if (nextPosition !== undefined) setPosition(nextPosition);
    return { reload };
  }

  function destroy() {
    generation += 1;
    handle.removeEventListener("pointerdown", onPointerDown);
    handle.removeEventListener("keydown", onKeyDown);
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", onPointerUp);
    window.removeEventListener("pointercancel", onPointerUp);
    sourceImg.removeAttribute("src");
    resultImg.removeAttribute("src");
    element.remove();
  }

  setPosition(position);
  startLoading();

  return {
    element,
    update,
    setPosition,
    retry,
    destroy,
    isDragging: () => dragging,
    isReady: () => loadState === "ready",
    ratioOk: () => ratioOk,
  };
}
