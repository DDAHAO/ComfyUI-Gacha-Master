import { badge, button, clear, h, isTopModal, modal } from "../dom.js";
import { createImageCompare } from "../components/image_compare.js";
import { attachTooltip } from "../components/tooltip.js";

function outputLabel(output) {
  return `DRAW-${String(output.draw_no ?? 0).padStart(6, "0")} / ATTEMPT-${String(output.attempt_no ?? 0).padStart(3, "0")} / OUT-${String(output.out_no ?? 0).padStart(4, "0")}`;
}

function retryUrl(url, attempt) {
  if (!attempt) return url;
  try {
    const parsed = new URL(url, window.location.href);
    parsed.searchParams.set("gm_retry", String(attempt));
    return parsed.toString();
  } catch {
    return `${url}${url.includes("?") ? "&" : "?"}gm_retry=${attempt}`;
  }
}

/**
 * Compare modal bound to the same ResultPager the detail view uses.
 *
 * It never keeps a private copy of the result list: page changes, cache
 * eviction and selection state all flow through the pager.  Only two high-res
 * images are referenced (plus the slider component's two), and closing the
 * dialog releases all of them.
 */
export function openCompare(ctx, {
  source, pager, index = 0, includeUnhealthy = false, selection,
} = {}) {
  if (!pager || !pager.items?.length) {
    ctx.notify("该原图当前页还没有可用结果。", "warn");
    return null;
  }
  let current = Math.max(0, Math.min(index, pager.items.length - 1));
  let mode = "side";
  let position = 0.5;
  let closed = false;
  let sliderUnavailable = "";
  let sliderError = null;
  let compare = null;
  let lastSelectionEvent = null;
  let needsSliderReload = false;
  let moving = false;
  const viewedIds = new Set();
  const viewedController = new AbortController();
  const sourceUrl = ctx.api.imageUrl(source.image_url || `/sources/${source.id}/image`);
  const cleanups = [() => viewedController.abort()];

  const dialog = modal({
    title: `对照筛选 · SRC-${String(source.source_order).padStart(4, "0")}`,
    wide: true,
    initialFocus: () => resultImg,
    onClose: () => cleanup(),
  });

  // -------------------------------------------------------------- header
  const sideButton = h("button", {
    class: "gm-compare-mode-btn active", type: "button", "aria-pressed": "true", onclick: () => setMode("side"),
  }, "并排对照");
  const sliderButton = h("button", {
    class: "gm-compare-mode-btn", type: "button", "aria-pressed": "false", onclick: () => setMode("slider"),
  }, "滑动对比");
  const modeBar = h("div", { class: "gm-compare-modes", role: "group", "aria-label": "比较模式" }, sideButton, sliderButton);

  const sourceCaption = h("div", { class: "gm-compare-caption gm-ellipsis", title: source.original_name }, source.original_name);
  const sourceImg = h("img", { class: "gm-compare-image", src: sourceUrl, alt: source.original_name, draggable: false });
  const sourceError = h("div", { class: "gm-image-error", hidden: true });
  const resultImg = h("img", {
    class: "gm-compare-image", alt: "生成结果", draggable: false, tabindex: "0",
    "aria-label": "生成结果；方向键切换，空格切换入选",
  });
  const resultError = h("div", { class: "gm-image-error", hidden: true });
  const sideGrid = h("div", { class: "gm-compare-grid" },
    h("div", { class: "gm-compare-pane" },
      h("div", { class: "gm-compare-title" }, "原图"),
      h("div", { class: "gm-compare-frame" }, sourceImg, sourceError),
      sourceCaption,
    ),
    h("div", { class: "gm-compare-pane result" },
      h("div", { class: "gm-compare-title" }, "生成结果"),
      h("div", { class: "gm-compare-frame" }, resultImg, resultError),
      h("div", { class: "gm-compare-caption" }, "右键结果图可切换入选"),
    ),
  );
  const sliderHost = h("div", { class: "gm-compare-slider", hidden: true });
  const sliderNotice = h("div", { class: "gm-compare-slider-notice", hidden: true });
  const meta = h("div", { class: "gm-compare-meta" });
  const positionLabel = h("span", { class: "gm-compare-position" });
  const pageButton = h("button", { class: "gm-btn muted", type: "button", onclick: () => loadNextPage() }, "下一页结果");
  const selectButton = button("入选", () => toggle(), { kind: "primary" });
  const prevBtn = button("← 上一张", () => move(-1), { kind: "muted" });
  const nextBtn = button("下一张 →", () => move(1), { kind: "muted" });
  const helpText = h("p", { class: "gm-help gm-compare-help" },
    "方向键切换同页结果，跨页会自动加载相邻页；空格切换入选；滑动模式中分割线获得焦点后方向键只移动分割线。Esc 关闭。");

  dialog.body.append(
    h("div", { class: "gm-compare-toolbar" }, modeBar, helpText),
    sideGrid,
    sliderHost,
    sliderNotice,
    meta,
  );
  dialog.footer.append(
    h("div", { class: "gm-compare-nav" }, prevBtn, positionLabel, nextBtn),
    h("div", { class: "gm-compare-actions" }, pageButton, selectButton),
  );

  const tooltipCleanups = [
    attachTooltip(selectButton, { content: "Space 或右键结果图也可以切换入选；保存中时该图片暂时不可重复操作。" }),
    attachTooltip(sliderButton, { content: "分割线只比较当前原图与当前结果；比例不同会自动提示返回并排模式。" }),
  ];
  cleanups.push(() => tooltipCleanups.forEach((fn) => fn()));

  sourceImg.addEventListener("error", () => {
    sourceImg.hidden = true;
    sourceError.hidden = false;
    clear(sourceError);
    sourceError.append(
      h("p", null, "原图加载失败；文件身份仍保留。"),
      button("重读原图", () => { sourceImg.hidden = false; sourceError.hidden = true; sourceImg.src = retryUrl(sourceUrl, Date.now()); }, { kind: "muted" }),
    );
  });
  sourceImg.addEventListener("load", () => { sourceImg.hidden = false; sourceError.hidden = true; });
  sourceImg.addEventListener("contextmenu", (event) => event.preventDefault());

  resultImg.addEventListener("error", () => {
    resultImg.hidden = true;
    resultError.hidden = false;
    clear(resultError);
    resultError.append(
      h("p", null, "生成结果加载失败；不会用空块冒充结果。"),
      button("重读结果", () => { resultImg.hidden = false; resultError.hidden = true; reloadResult(); }, { kind: "muted" }),
    );
  });
  resultImg.addEventListener("load", () => { resultImg.hidden = false; resultError.hidden = true; });
  resultImg.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (compare?.isDragging()) return;
    toggle();
  });

  const unsubscribeSelection = selection?.subscribe((event) => {
    if (event.outputId !== currentOutput()?.id) return;
    lastSelectionEvent = event;
    renderMeta();
    renderSelectButton();
  });
  cleanups.push(() => unsubscribeSelection?.());

  const unsubscribePager = pager.subscribe((event) => {
    if (closed) return;
    current = Math.max(0, Math.min(current, pager.items.length - 1));
    if (event.type === "state" || event.type === "error") {
      // If the current OUT disappeared (revision change), fall back to the
      // first item on the new page instead of showing a different image.
      if (!pager.items.length) {
        clear(meta);
        resultImg.removeAttribute("src");
        positionLabel.textContent = "当前页没有结果";
        return;
      }
      render();
    }
  });
  cleanups.push(() => unsubscribePager());

  function currentOutput() { return pager.items[current]; }

  function resultUrlOf(output) {
    return ctx.api.imageUrl(output.image_url || `/outputs/${output.id}/image`);
  }

  function markViewed(output) {
    if (!output || viewedIds.has(output.id)) return;
    viewedIds.add(output.id);
    if (viewedIds.size > 2000) viewedIds.clear();
    ctx.api.viewed(output.id, { signal: viewedController.signal }).catch(() => {});
  }

  function reloadResult() {
    const output = currentOutput();
    if (!output) return;
    resultImg.src = retryUrl(resultUrlOf(output), Date.now());
  }

  function render() {
    const output = currentOutput();
    if (!output) return;
    resultImg.src = resultUrlOf(output);
    resultImg.alt = outputLabel(output);
    resultImg.classList.toggle("unhealthy", output.health !== "VERIFIED");
    positionLabel.textContent = `第 ${pager.currentIndex + 1} 页 · 第 ${current + 1}/${pager.items.length} 张 · 缓存 ${pager.cachedPages} 页`;
    prevBtn.disabled = moving || (current === 0 && !pager.hasPrev);
    nextBtn.disabled = moving || (current === pager.items.length - 1 && !pager.hasNext);
    pageButton.hidden = !pager.hasNext;
    pageButton.disabled = moving || pager.loading;
    markViewed(output);
    renderMeta();
    renderSelectButton();
    renderMode();
  }

  function renderMeta() {
    const output = currentOutput();
    if (!output) return;
    clear(meta);
    const identity = h("div", { class: "gm-compare-info" },
      h("span", { class: "gm-mono" }, outputLabel(output)),
      badge(source.original_name, "muted"),
      badge(output.health === "VERIFIED" ? "可用" : output.health, output.health === "VERIFIED" ? "ok" : "bad"),
      output.selected ? badge("已入选", "ok") : badge("未入选", "muted"),
      output.health !== "VERIFIED" ? badge("异常输出不可入选", "bad") : null,
    );
    meta.appendChild(identity);
    if (includeUnhealthy && output.health !== "VERIFIED") {
      meta.appendChild(h("p", { class: "gm-help" }, "当前处于“包含异常输出”视图；只用于查看证据，不能入选或导出。"));
    }
    if (pager.error) {
      meta.appendChild(h("p", { class: "gm-inline-error", role: "alert" },
        `结果页读取失败：${pager.error.message || pager.error}`));
      meta.appendChild(button("重试本页", () => pager.retry(), { kind: "muted" }));
    }
    if (lastSelectionEvent?.state === "failed" && lastSelectionEvent.outputId === output.id) {
      const errorBlock = h("div", { class: "gm-inline-error", role: "alert" },
        lastSelectionEvent.conflict
          ? "入选状态已在其他页面变化，已恢复并重新读取权威状态。"
          : `入选状态保存失败：${lastSelectionEvent.error?.message || lastSelectionEvent.error}`);
      meta.appendChild(errorBlock);
      if (lastSelectionEvent.conflict) {
        meta.appendChild(button("重新读取当前页", () => reloadCurrentPage(), { kind: "muted" }));
      }
    } else if (lastSelectionEvent?.state === "saved" && lastSelectionEvent.outputId === output.id) {
      meta.appendChild(h("p", { class: "gm-help" }, "入选状态已由服务器确认。"));
    }
    if (sliderUnavailable === "mismatch" && mode === "slider") {
      meta.appendChild(h("p", { class: "gm-inline-error" }, "图片比例不同，滑动对比已禁用。"));
    }
  }

  function renderSelectButton() {
    const output = currentOutput();
    if (!output) return;
    const pending = selection?.isPending(output.id);
    selectButton.disabled = pending || output.health !== "VERIFIED";
    selectButton.textContent = pending ? "保存中…" : output.selected ? "取消入选（Space）" : "入选（Space）";
    selectButton.classList.toggle("selected", Boolean(output.selected));
    selectButton.title = output.health !== "VERIFIED" ? "异常输出不能入选" : "";
  }

  function renderMode() {
    sideButton.classList.toggle("active", mode === "side");
    sliderButton.classList.toggle("active", mode === "slider");
    sideButton.setAttribute("aria-pressed", mode === "side" ? "true" : "false");
    sliderButton.setAttribute("aria-pressed", mode === "slider" ? "true" : "false");
    sideGrid.hidden = mode !== "side";
    sliderHost.hidden = mode !== "slider";
    sliderNotice.hidden = mode !== "slider" || (!sliderUnavailable && !sliderError);
    if (mode === "slider") renderSlider();
  }

  function renderSlider() {
    const output = currentOutput();
    if (!output) return;
    clear(sliderNotice);
    if (sliderUnavailable === "mismatch") {
      sliderHost.hidden = true;
      sliderNotice.hidden = false;
      sliderNotice.append(
        h("p", null, "图片比例不同，请使用并排对照。两张图不会被裁切或拉伸。"),
        button("返回并排对照", () => setMode("side"), { kind: "primary" }),
      );
      return;
    }
    if (sliderUnavailable === "error") {
      sliderHost.hidden = true;
      sliderNotice.hidden = false;
      sliderNotice.append(
        h("p", null, `图片加载失败（${sliderError === "source" ? "原图" : "生成结果"}）；文件身份仍保留。`),
        h("div", { class: "gm-inline-actions" },
          button("重试加载", () => { sliderUnavailable = ""; compare?.retry(); renderMode(); }, { kind: "primary" }),
          button("返回并排对照", () => setMode("side"), { kind: "muted" }),
        ),
      );
      return;
    }
    sliderHost.hidden = false;
    sliderNotice.hidden = true;
    if (!compare) {
      needsSliderReload = false;
      compare = createImageCompare({
        sourceUrl,
        resultUrl: resultUrlOf(output),
        position,
        onPositionChange: (value) => { position = value; },
        onUnavailable: (reason, detail) => {
          sliderUnavailable = reason;
          sliderError = detail?.side || null;
          renderMode();
          renderMeta();
        },
        onStatus: (state) => {
          if (state === "ready") {
            sliderUnavailable = "";
            sliderError = null;
            sliderNotice.hidden = true;
          }
        },
      });
      sliderHost.appendChild(compare.element);
    } else {
      compare.update({ sourceUrl, resultUrl: resultUrlOf(output), position });
      if (needsSliderReload && !compare.isReady()) {
        needsSliderReload = false;
        compare.retry();
      }
    }
  }

  function setMode(nextMode) {
    if (nextMode === mode) return;
    mode = nextMode;
    needsSliderReload = nextMode === "slider";
    if (nextMode === "side") {
      sliderUnavailable = "";
      sliderError = null;
    }
    renderMode();
    renderMeta();
    // Switching modes must not record an extra "viewed" write.
  }

  async function move(delta) {
    if (closed || moving) return;
    const next = current + delta;
    if (next >= 0 && next < pager.items.length) {
      current = next;
      position = 0.5;
      lastSelectionEvent = null;
      sliderUnavailable = "";
      sliderError = null;
      render();
      return;
    }
    if (next < 0 && pager.hasPrev) {
      moving = true;
      render();
      try {
        await pager.prev();
        if (closed) return;
        current = Math.max(0, pager.items.length - 1);
        position = 0.5;
        lastSelectionEvent = null;
        sliderUnavailable = "";
        sliderError = null;
        render();
      } finally {
        moving = false;
        if (!closed) render();
      }
      return;
    }
    if (next >= pager.items.length && pager.hasNext) {
      moving = true;
      render();
      try {
        await pager.next();
        if (closed) return;
        current = 0;
        position = 0.5;
        lastSelectionEvent = null;
        sliderUnavailable = "";
        sliderError = null;
        render();
      } finally {
        moving = false;
        if (!closed) render();
      }
    }
  }

  async function loadNextPage() {
    if (!pager.hasNext || moving) return;
    moving = true;
    render();
    try {
      await pager.next();
      if (closed) return;
      current = 0;
      position = 0.5;
      lastSelectionEvent = null;
      sliderUnavailable = "";
      sliderError = null;
      render();
    } finally {
      moving = false;
      if (!closed) render();
    }
  }

  function toggle() {
    const output = currentOutput();
    if (!output || output.health !== "VERIFIED" || !selection) return;
    if (selection.isPending(output.id)) return;
    selection.toggle(output);
  }

  async function reloadCurrentPage() {
    if (moving) return;
    const outputId = currentOutput()?.id;
    moving = true;
    try {
      await pager.invalidate({ reload: true });
      if (closed) return;
      if (!pager.items.length) {
        ctx.notify("当前页已没有结果；请关闭对照并在详情页重新加载。", "warn");
        return;
      }
      const index = pager.items.findIndex((item) => item.id === outputId);
      current = index >= 0 ? index : 0;
      lastSelectionEvent = null;
      render();
    } catch (error) {
      ctx.notify(`重新读取结果失败：${error?.message || error}`, "error");
    } finally {
      moving = false;
      if (!closed) render();
    }
  }

  function onKey(event) {
    if (closed || event.defaultPrevented || !isTopModal(dialog.dialog)) return;
    if (event.key === "Escape") return; // the modal base owns Escape
    const target = event.target;
    const element = target instanceof Element ? target : document.activeElement;
    if (element?.closest?.("input, select, textarea, [contenteditable='true'], [role='slider'], [role='textbox']")) return;
    const onButton = element?.closest?.("button, a, [role='button'], [role='link']");
    const isSpace = event.key === " " || event.code === "Space";
    if (isSpace && onButton) return; // native click must not also toggle selection
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      move(-1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      move(1);
    } else if (isSpace) {
      event.preventDefault();
      toggle();
    }
  }

  // Slider mode right-click: only the result half of the stage toggles.
  function onStageContext(event) {
    if (!compare || mode !== "slider" || compare.isDragging()) {
      event.preventDefault();
      return;
    }
    const ratio = position;
    const rect = event.currentTarget.getBoundingClientRect();
    const x = (event.clientX - rect.left) / Math.max(1, rect.width);
    event.preventDefault();
    if (x >= ratio) toggle();
  }
  sliderHost.addEventListener("contextmenu", onStageContext);

  document.addEventListener("keydown", onKey, true);
  cleanups.push(() => document.removeEventListener("keydown", onKey, true));

  function cleanup() {
    if (closed) return;
    closed = true;
    compare?.destroy();
    compare = null;
    for (const fn of cleanups.splice(0)) {
      try { fn(); } catch { /* ignore cleanup errors */ }
    }
    // Drop high-res references immediately so the browser can release them.
    sourceImg.removeAttribute("src");
    resultImg.removeAttribute("src");
  }

  render();
  return {
    close: () => dialog.close("program"),
    move,
    toggle,
    setMode,
    getMode: () => mode,
    element: dialog.root,
  };
}
