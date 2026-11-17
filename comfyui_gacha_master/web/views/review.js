// Continuous source-group review workspace.
//
// One modal handles the whole queue: preview several results of the current
// source, keep any number of them, then "保留并下一张" / "跳过并下一张" advances
// in place.  Progress is server-authoritative (ReviewSessionClient); this view
// only renders state and forwards user intent.

import {
  badge, button, clear, count, errorBox, fmtTime, h, isTopModal, modal, REVIEW_STATUS,
} from "../dom.js";
import { createImageCompare } from "../components/image_compare.js";
import { thumbnailLoader } from "../components/lazy_image.js";
import { ReviewSessionClient } from "../review_session.js";

function outputLabel(output) {
  return `DRAW-${String(output.draw_no ?? 0).padStart(6, "0")} / OUT-${String(output.out_no ?? 0).padStart(4, "0")}`;
}

function reviewBadge(source) {
  const review = source?.review || {};
  const label = REVIEW_STATUS[review.status] || review.status || "未审核";
  const kind = review.status === "reviewed" ? "ok" : review.status === "selected_pending_review" ? "warn" : "muted";
  return badge(label, kind);
}

export function openReviewWorkspace(ctx, job, { startSourceId = null, startOutputId = null, selection = null, onClose, onExport } = {}) {
  let closed = false;
  let compare = null;
  let mode = "side";
  let thumbRefs = [];
  let lastOutputId = null;
  let lastResultSrc = "";
  let lastThumbSignature = "";
  let completionSignature = "";
  let sourceUrl = "";
  let resultRetry = 0;
  const unsubscribers = [];

  const client = new ReviewSessionClient({
    api: ctx.api,
    jobId: job.id,
    selection,
    notify: ctx.notify,
  });

  const dialog = modal({
    title: `连续筛选 · ${job.display_name || job.display_id}`,
    wide: true,
    initialFocus: () => resultImg,
    onClose: () => cleanup(),
  });

  // ------------------------------------------------------------- header
  const srcBadge = h("span", { class: "gm-badge muted gm-mono" }, "SRC-—");
  const reviewTag = h("span");
  const sourceTitle = h("strong", { class: "gm-ellipsis" });
  const progressLine = h("span", { class: "gm-help" });
  const groupLine = h("span", { class: "gm-help" });
  const saveLine = h("span", { class: "gm-save-state", role: "status", "aria-live": "polite" });
  const liveLine = h("div", { class: "gm-sr-only", role: "status", "aria-live": "polite" });
  const head = h("div", { class: "gm-review-head" },
    h("div", { class: "gm-review-title" },
      srcBadge,
      sourceTitle,
      reviewTag,
    ),
    h("div", { class: "gm-review-counts" }, progressLine, groupLine, saveLine),
    liveLine,
  );

  // -------------------------------------------------------------- stage
  const sourceImg = h("img", { class: "gm-compare-image", alt: "原图", draggable: false });
  const sourceError = h("div", { class: "gm-image-error", hidden: true });
  const resultImg = h("img", {
    class: "gm-compare-image", alt: "生成结果", draggable: false, tabindex: "0",
    "aria-label": "生成结果；方向键切换，空格切换入选",
  });
  const resultError = h("div", { class: "gm-image-error", hidden: true });
  const selectButton = button("入选（Space）", () => toggleCurrent(), { kind: "primary" });
  const selectLine = h("div", { class: "gm-compare-caption gm-review-select" }, selectButton);
  const sideGrid = h("div", { class: "gm-compare-grid" },
    h("div", { class: "gm-compare-pane" },
      h("div", { class: "gm-compare-title" }, "原图"),
      h("div", { class: "gm-compare-frame" }, sourceImg, sourceError),
    ),
    h("div", { class: "gm-compare-pane result" },
      h("div", { class: "gm-compare-title" }, "当前结果"),
      h("div", { class: "gm-compare-frame" }, resultImg, resultError),
      selectLine,
    ),
  );
  const sliderHost = h("div", { class: "gm-compare-slider", hidden: true });
  const sliderNotice = h("div", { class: "gm-compare-slider-notice", hidden: true });
  const sideButton = h("button", {
    class: "gm-compare-mode-btn active", type: "button", "aria-pressed": "true",
    onclick: () => setMode("side"),
  }, "并排对照");
  const sliderButton = h("button", {
    class: "gm-compare-mode-btn", type: "button", "aria-pressed": "false",
    onclick: () => setMode("slider"),
  }, "滑动对比");
  const modeBar = h("div", { class: "gm-compare-modes", role: "group", "aria-label": "比较模式" }, sideButton, sliderButton);
  const stage = h("div", { class: "gm-review-stage" },
    h("div", { class: "gm-review-modebar" }, modeBar,
      h("span", { class: "gm-help" }, "方向键切换同组结果（自动跨页），空格切换入选；缩略图只改变预览。")),
    sideGrid, sliderHost, sliderNotice,
  );

  // -------------------------------------------------------------- pager
  const firstBtn = button("首页", () => client.firstPage(), { kind: "muted" });
  const prevBtn = button("← 上一页", () => client.prevPage(), { kind: "muted" });
  const pageLabel = h("span", { class: "gm-pager-position" });
  const nextBtn = button("下一页 →", () => client.nextPage(), { kind: "muted" });
  const retryBtn = button("重试本页", () => client.retryPage(), { kind: "warn", hidden: true });
  const pageLoading = h("span", { class: "gm-help", role: "status", hidden: true }, "读取中…");
  const pagerBar = h("div", { class: "gm-result-toolbar gm-review-pager" },
    firstBtn, prevBtn, pageLabel, nextBtn, retryBtn, pageLoading);

  const thumbsLabel = h("div", { class: "gm-review-thumbs-label gm-help" });
  const thumbsGrid = h("div", { class: "gm-thumb-grid gm-review-thumbs" });
  const resultMeta = h("div", { class: "gm-compare-meta gm-review-meta" });
  const errorArea = h("div", { class: "gm-review-error" });

  const contentHost = h("div", { class: "gm-review-content" },
    head, stage, pagerBar, thumbsLabel, thumbsGrid, resultMeta, errorArea,
  );
  const failureHost = h("div", { class: "gm-review-failure", hidden: true });
  const completionHost = h("div", { class: "gm-review-completion", hidden: true });
  dialog.body.append(contentHost, failureHost, completionHost);

  // ------------------------------------------------------------- footer
  const backBtn = button("← 上一张原图", () => client.back(), { kind: "muted" });
  const exitBtn = button("退出筛选", () => dialog.close("exit"), { kind: "muted" });
  const skipBtn = button("跳过并下一张", () => decide("SKIPPED"), { kind: "muted" });
  const retainBtn = button("保留并下一张", () => decide("SELECTED"), { kind: "primary" });
  const retryNextBtn = button("重试加载下一组", () => client.retryNext(), { kind: "primary" });
  const reloadBtn = button("读取最新进度", () => client.reload(), { kind: "warn" });
  const retryPositionBtn = button("重试保存进度", () => {
    client.flushPosition().then(() => render());
  }, { kind: "warn" });
  const footerLeft = h("div", { class: "gm-inline-actions" }, backBtn, exitBtn);
  const footerRight = h("div", { class: "gm-inline-actions" }, reloadBtn, retryPositionBtn, retryNextBtn, skipBtn, retainBtn);
  dialog.footer.append(footerLeft, footerRight);

  // ------------------------------------------------------------- events
  unsubscribers.push(client.subscribe((event) => {
    if (closed) return;
    if (event.type === "error" && event.error && event.error.status !== 409) {
      ctx.notify(`筛选操作失败：${event.error.message || event.error}`, "error", { id: "review-error" });
    }
    render();
  }));

  function decide(kind) {
    if (closed) return;
    client.decide(kind).then(() => render());
  }

  function toggleCurrent() {
    const output = client.currentOutput;
    if (!output) return;
    client.toggleCurrent();
    render();
  }

  // ------------------------------------------------------------- images
  function setSource(src, alt) {
    if (!src) return;
    if (sourceImg.getAttribute("src") !== src) sourceImg.src = src;
    sourceImg.alt = alt || "原图";
    sourceImg.hidden = false;
    sourceError.hidden = true;
  }

  function setResult(src, alt) {
    if (!src) return;
    resultRetry = 0;
    lastResultSrc = src;
    resultImg.src = src;
    resultImg.alt = alt || "生成结果";
    resultImg.hidden = false;
    resultError.hidden = true;
  }

  function retryResult() {
    const output = client.currentOutput;
    if (!output) return;
    resultRetry += 1;
    const base = ctx.api.imageUrl(output.image_url || `/outputs/${output.id}/image`);
    const sep = base.includes("?") ? "&" : "?";
    setResult(`${base}${sep}gm_retry=${resultRetry}`, outputLabel(output));
  }

  sourceImg.addEventListener("error", () => {
    sourceImg.hidden = true;
    sourceError.hidden = false;
    clear(sourceError);
    sourceError.append(h("p", null, "原图加载失败；文件身份仍保留。"));
  });
  sourceImg.addEventListener("load", () => { sourceImg.hidden = false; sourceError.hidden = true; });
  sourceImg.addEventListener("contextmenu", (event) => event.preventDefault());

  resultImg.addEventListener("error", () => {
    resultImg.hidden = true;
    resultError.hidden = false;
    clear(resultError);
    resultError.append(
      h("p", null, "结果图加载失败；不会用空块冒充结果。"),
      button("重读结果", () => retryResult(), { kind: "muted" }),
    );
  });
  resultImg.addEventListener("load", () => { resultImg.hidden = false; resultError.hidden = true; });
  resultImg.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (compare?.isDragging()) return;
    toggleCurrent();
  });

  // ------------------------------------------------------------- render
  function render() {
    if (closed) return;
    renderHeader();
    const finished = client.viewState === "finished";
    contentHost.hidden = finished;
    completionHost.hidden = !finished;
    dialog.footer.hidden = finished;
    if (finished) {
      renderCompletion();
      return;
    }
    const nextFailed = client.viewState === "next_failed";
    const loadFailed = client.viewState === "error" && !client.source;
    failureHost.hidden = !(nextFailed || loadFailed);
    contentHost.hidden = nextFailed || loadFailed;
    dialog.footer.hidden = nextFailed || loadFailed;
    if (nextFailed) {
      renderNextFailure();
      return;
    }
    if (loadFailed) {
      renderLoadFailure();
      return;
    }
    failureHost.hidden = true;
    renderStage();
    renderPager();
    renderThumbs();
    renderFooter();
    renderErrors();
    if (!dialog.dialog.contains(document.activeElement) && client.viewState === "ready") {
      resultImg.focus({ preventScroll: true });
    }
  }

  function renderHeader() {
    const source = client.source;
    const summary = client.summary || {};
    srcBadge.textContent = source ? `SRC-${String(source.source_order).padStart(4, "0")}` : "SRC-—";
    sourceTitle.textContent = source?.original_name || "正在读取筛选进度…";
    sourceTitle.title = source?.original_name || "";
    clear(reviewTag);
    if (source) reviewTag.appendChild(reviewBadge(source));
    progressLine.textContent =
      `已确认 ${count(summary.confirmed_sources)} 组 · 待处理 ${count(summary.pending_sources)} 组`
      + ` · 无可用结果 ${count(summary.no_result_sources)} 组`;
    const group = client.group;
    const bits = [];
    if (group) bits.push(`本组可用 ${count(group.available)} 张`, `已选 ${count(group.selected)} 张`);
    if (group && Number(group.pending_draws || 0) > 0) bits.push(`本组仍有 ${count(group.pending_draws)} 次待生成`);
    if (source) bits.push(`${source.width}×${source.height}`);
    groupLine.textContent = bits.filter(Boolean).join(" · ");
    renderSaveState();
  }

  function renderSaveState() {
    clear(saveLine);
    const save = client.save;
    if (client.conflict) {
      saveLine.className = "gm-save-state warn";
      saveLine.appendChild(h("span", null, "筛选进度已在其他页面更新；不会自动覆盖。"));
      return;
    }
    if (save.position === "saving" || save.decision === "saving") {
      saveLine.className = "gm-save-state saving";
      saveLine.appendChild(h("span", null, save.decision === "saving" ? "正在确认本组…" : "进度保存中…"));
      return;
    }
    if (save.position === "failed") {
      saveLine.className = "gm-save-state bad";
      saveLine.appendChild(h("span", null, `进度保存失败：${save.message || "服务器未确认"}（保留上次断点）`));
      return;
    }
    if (save.position === "conflict") {
      saveLine.className = "gm-save-state warn";
      saveLine.appendChild(h("span", null, "进度保存冲突；请读取最新进度。"));
      return;
    }
    if (save.at) {
      saveLine.className = "gm-save-state ok";
      saveLine.appendChild(h("span", null, `进度已保存 ${fmtTime(new Date(save.at).toISOString())}`));
    } else {
      saveLine.className = "gm-save-state";
      saveLine.appendChild(h("span", null, "进度由服务器保存"));
    }
  }

  function renderStage() {
    const source = client.source;
    const output = client.currentOutput;
    if (source) {
      sourceUrl = ctx.api.imageUrl(source.image_url || `/sources/${source.id}/image`);
      setSource(sourceUrl, source.original_name);
    }
    if (!output) {
      resultImg.removeAttribute("src");
      lastOutputId = null;
      lastResultSrc = "";
      clear(resultMeta);
      selectButton.disabled = true;
      selectButton.textContent = "入选（Space）";
      return;
    }
    if (output.id !== lastOutputId) {
      lastOutputId = output.id;
      setResult(ctx.api.imageUrl(output.image_url || `/outputs/${output.id}/image`), outputLabel(output));
      liveLine.textContent = `已载入 ${outputLabel(output)}，${output.selected ? "已入选" : "未入选"}`;
    } else if (!lastResultSrc) {
      setResult(ctx.api.imageUrl(output.image_url || `/outputs/${output.id}/image`), outputLabel(output));
    }
    const pending = selection?.isPending(output.id);
    selectButton.disabled = Boolean(pending) || output.health !== "VERIFIED";
    selectButton.textContent = pending ? "保存中…" : output.selected ? "取消入选（Space）" : "入选（Space）";
    selectButton.classList.toggle("selected", Boolean(output.selected));
    selectButton.title = output.health !== "VERIFIED" ? "异常输出不能入选" : "";
    clear(resultMeta);
    resultMeta.append(
      h("div", { class: "gm-compare-info" },
        h("span", { class: "gm-mono" }, outputLabel(output)),
        badge(output.health === "VERIFIED" ? "可用" : output.health, output.health === "VERIFIED" ? "ok" : "bad"),
        output.selected ? badge("已入选", "ok") : badge("未入选", "muted"),
        pending ? badge("保存中", "warn") : null,
        client.group && client.group.review?.decision
          ? badge(`本组已确认：${client.group.review.decision === "SELECTED" ? "保留" : "跳过"}`, "muted")
          : null,
      ),
      client.group && Number(client.group.pending_draws || 0) > 0
        ? h("p", { class: "gm-help" }, "本组仍有待生成 DRAW；当前确认只针对已有结果，新增结果会按版本规则进入待复筛。")
        : null,
    );
    renderMode();
  }

  function renderPager() {
    const pager = client.pager;
    if (!pager) {
      pageLabel.textContent = "—";
      firstBtn.disabled = true;
      prevBtn.disabled = true;
      nextBtn.disabled = true;
      retryBtn.hidden = true;
      pageLoading.hidden = true;
      return;
    }
    const total = pager.total;
    pageLabel.textContent = `第 ${pager.currentIndex + 1} 页 · 本页 ${count(pager.items.length)} 张`
      + `${total === null ? "" : ` · 合计 ${count(total)} 张`} · 缓存 ${count(pager.cachedPages)} 页`;
    firstBtn.disabled = !pager.hasPrev || pager.loading || client.busy;
    prevBtn.disabled = !pager.hasPrev || pager.loading || client.busy;
    nextBtn.disabled = !pager.hasNext || pager.loading || client.busy;
    retryBtn.hidden = !pager.error;
    pageLoading.hidden = !pager.loading;
  }

  function releaseThumbs() {
    for (const ref of thumbRefs) {
      try { thumbnailLoader.release(ref.img); } catch { /* ignore */ }
    }
    thumbRefs = [];
    clear(thumbsGrid);
  }

  function renderThumbs() {
    const pager = client.pager;
    const items = pager?.items || [];
    const pageSignature = `${pager?.currentIndex}|${items.map((item) => item.id).join(",")}`;
    thumbsLabel.textContent = `本组结果 · 当前页 ${count(items.length)} 张 · 点击缩略图换预览，点星标切换入选`;
    if (pageSignature === lastThumbSignature && thumbsGrid.childElementCount === items.length) {
      for (const ref of thumbRefs) {
        const pending = Boolean(selection?.isPending(ref.item.id));
        ref.card.classList.toggle("current", ref.item.id === client.currentOutputId);
        ref.card.classList.toggle("selected", Boolean(ref.item.selected));
        ref.card.classList.toggle("pending", pending);
        ref.card.classList.toggle("unhealthy", ref.item.health !== "VERIFIED");
        ref.star.textContent = ref.item.selected ? "★" : "☆";
        ref.star.classList.toggle("on", Boolean(ref.item.selected));
        ref.star.disabled = pending || ref.item.health !== "VERIFIED";
        ref.state.textContent = pending ? "保存中…" : "";
      }
      return;
    }
    lastThumbSignature = pageSignature;
    releaseThumbs();
    for (const item of items) {
      const card = h("div", { class: "gm-thumb-card", dataset: { outputId: item.id } });
      const openButton = h("button", {
        class: "gm-thumb-open", type: "button",
        onclick: () => { client.selectOutput(item.id); render(); },
      });
      const img = thumbnailLoader.create(
        ctx.api.imageUrl(item.thumbnail_url || `/outputs/${item.id}/thumbnail`),
        outputLabel(item),
        "gm-thumb",
      );
      openButton.appendChild(img);
      openButton.setAttribute("aria-label", `预览 ${outputLabel(item)}`);
      const mono = h("span", { class: "gm-mono" }, `D${item.draw_no}/O${item.out_no}`);
      const state = h("span", { class: "gm-thumb-state" });
      const star = h("button", {
        class: "gm-star", type: "button",
        onclick: (event) => { event.stopPropagation(); client.toggleOutput(item.id); render(); },
      });
      const health = h("span", { class: "gm-badge bad gm-thumb-health", hidden: item.health === "VERIFIED" });
      if (item.health !== "VERIFIED") health.textContent = item.health;
      card.append(openButton, h("div", { class: "gm-thumb-foot" }, mono, state, star), health);
      card.classList.toggle("current", item.id === client.currentOutputId);
      card.classList.toggle("selected", Boolean(item.selected));
      card.classList.toggle("pending", Boolean(selection?.isPending(item.id)));
      card.classList.toggle("unhealthy", item.health !== "VERIFIED");
      star.textContent = item.selected ? "★" : "☆";
      star.classList.toggle("on", Boolean(item.selected));
      star.disabled = Boolean(selection?.isPending(item.id)) || item.health !== "VERIFIED";
      star.title = item.health !== "VERIFIED" ? "异常输出不能入选" : item.selected ? "取消入选" : "入选";
      star.setAttribute("aria-label", item.selected ? "取消入选" : "入选");
      state.textContent = selection?.isPending(item.id) ? "保存中…" : "";
      thumbsGrid.appendChild(card);
      thumbRefs.push({ card, img, star, state, item });
    }
  }

  function renderFooter() {
    const selected = client.selectedCount;
    const pending = client.pendingSelectionCount || client._groupRefreshPending;
    const busy = client.busy;
    backBtn.disabled = !client.canBack;
    backBtn.title = client.session?.history?.length ? "返回本轮上一组查看或修改" : "还没有可返回的上一组";
    skipBtn.disabled = !client.canSkip;
    retainBtn.disabled = !client.canRetain;
    skipBtn.textContent = pending ? "保存中…" : "跳过并下一张";
    retainBtn.textContent = client.save.decision === "saving"
      ? "正在确认…"
      : pending
        ? "保存中…"
        : `保留并下一张（已选 ${selected}）`;
    skipBtn.title = selected > 0
      ? `本组已选 ${selected} 张；取消入选后可跳过，或选择保留并继续。`
      : "本组不保留任何结果，保存明确跳过并进入下一组";
    retainBtn.title = selected > 0
      ? "确认本组全部已保存的入选结果，然后进入下一张原图"
      : "请先入选至少一张结果";
    reloadBtn.hidden = !client.conflict;
    retryPositionBtn.hidden = client.save.position !== "failed" || Boolean(client.conflict);
    retryNextBtn.hidden = true;
    footerLeft.hidden = false;
    footerRight.hidden = false;
  }

  function renderLoadFailure() {
    clear(failureHost);
    failureHost.append(
      h("div", { class: "gm-notice warn" },
        h("strong", null, "筛选进度读取失败"),
        h("p", null, "服务器断点没有被删除；恢复网络或修复错误后可重试。"),
        errorBox(client.error || { code: "REVIEW_SESSION", message: "无法读取筛选会话。" }),
        h("div", { class: "gm-inline-actions" },
          button("重试读取", () => client.reload(), { kind: "primary" }),
          button("退出筛选", () => dialog.close("exit"), { kind: "muted" }),
        ),
      ),
    );
  }

  function renderErrors() {
    clear(errorArea);
    if (client.viewState === "error" && client.source) {
      errorArea.append(h("div", { class: "gm-inline-error", role: "alert" },
        h("p", null, `本组读取失败：${client.error?.message || client.error || "请重试"}`),
        h("div", { class: "gm-inline-actions" },
          button("重试读取", () => client.reload(), { kind: "muted" }),
          button("读取最新进度", () => client.reload(), { kind: "muted" }),
        ),
      ));
      return;
    }
    if (client.save.decision === "blocked" && client.error) {
      errorArea.append(h("div", { class: "gm-inline-error", role: "alert" }, client.error.message || String(client.error)));
    } else if (client.save.decision === "failed" && client.error && !client.conflict) {
      errorArea.append(errorBox(client.error));
    }
    if (client.conflict) {
      errorArea.append(h("div", { class: "gm-notice warn" },
        h("strong", null, "筛选进度已在其他页面变化"),
        h("p", null, "本页不会自动重发旧决定；请读取最新进度后重新确认。"),
        h("div", { class: "gm-inline-actions" },
          button("读取最新进度", () => client.reload(), { kind: "primary" }),
        ),
      ));
    }
  }

  function renderNextFailure() {
    clear(failureHost);
    failureHost.append(
      h("div", { class: "gm-notice warn" },
        h("strong", null, "本组已保存，下一组加载失败"),
        h("p", null, `上一组的确认与继续边界已提交；重试只会重新加载下一组，不会重复确认，也不会再次改选。`),
        h("p", { class: "gm-help" }, client.error?.message || String(client.error || "")),
        h("div", { class: "gm-inline-actions" },
          button("重试加载下一组", () => client.retryNext(), { kind: "primary" }),
          button("读取最新进度", () => client.reload(), { kind: "muted" }),
          button("退出筛选", () => dialog.close("exit"), { kind: "muted" }),
        ),
      ),
    );
    dialog.footer.hidden = false;
    footerLeft.hidden = false;
    footerRight.hidden = false;
    backBtn.disabled = true;
    skipBtn.disabled = true;
    retainBtn.disabled = true;
    reloadBtn.hidden = true;
    retryPositionBtn.hidden = true;
    retryNextBtn.hidden = false;
  }

  function renderCompletion() {
    const summary = client.summary || {};
    const resume = client.resume || {};
    const signature = JSON.stringify({
      selected: summary.selected_groups,
      skipped: summary.skipped_groups,
      pending: summary.pending_sources,
      noResult: summary.no_result_sources,
      requeue: Boolean(resume.requeue_available),
    });
    if (signature === completionSignature && completionHost.childElementCount) return;
    completionSignature = signature;
    clear(completionHost);
    const counts = [
      ["保留组数", count(summary.selected_groups)],
      ["跳过组数", count(summary.skipped_groups)],
      ["待复筛组", count(summary.pending_sources)],
      ["无可用结果组", count(summary.no_result_sources)],
      ["已选结果", count(summary.selected_outputs)],
    ];
    completionHost.append(
      h("div", { class: "gm-review-complete" },
        h("h3", null, "本轮筛选完成"),
        h("p", { class: "gm-help" },
          "完成范围只限本次筛选队列；未生成、异常输出或后来新增的结果仍会显示为待处理。"),
        h("dl", { class: "gm-kv" },
          ...counts.flatMap(([label, value]) => [h("dt", null, label), h("dd", null, value)])),
        h("div", { class: "gm-inline-actions" },
          button("返回任务详情", () => dialog.close("done"), { kind: "primary" }),
          button("查看已选结果", () => dialog.close("done"), { kind: "muted" }),
          button("进入导出", () => { dialog.close("export"); onExport?.(); }, { kind: "muted" }),
          (resume.requeue_available || Number(summary.pending_sources || 0) > 0)
            ? button("重新检查待复筛", () => client.requeue().then(() => render()), { kind: "warn" })
            : null,
        ),
      ),
    );
    setTimeout(() => completionHost.querySelector("button")?.focus(), 0);
  }

  // -------------------------------------------------------------- mode
  function setMode(nextMode) {
    if (nextMode === mode) return;
    mode = nextMode;
    sideButton.classList.toggle("active", mode === "side");
    sliderButton.classList.toggle("active", mode === "slider");
    sideButton.setAttribute("aria-pressed", mode === "side" ? "true" : "false");
    sliderButton.setAttribute("aria-pressed", mode === "slider" ? "true" : "false");
    renderMode();
  }

  function renderMode() {
    sideGrid.hidden = mode !== "side";
    sliderHost.hidden = mode !== "slider";
    sliderNotice.hidden = true;
    if (mode !== "slider") {
      compare?.destroy();
      compare = null;
      return;
    }
    const output = client.currentOutput;
    if (!output || !sourceUrl) return;
    const resultUrl = ctx.api.imageUrl(output.image_url || `/outputs/${output.id}/image`);
    if (!compare) {
      compare = createImageCompare({
        sourceUrl,
        resultUrl,
        onUnavailable: (reason, detail) => {
          if (reason === "mismatch") {
            clear(sliderNotice);
            sliderNotice.hidden = false;
            sliderNotice.append(
              h("p", null, "图片比例不同，滑动对比已禁用；两张图保持完整比例。"),
              button("返回并排对照", () => setMode("side"), { kind: "primary" }),
            );
          } else if (reason === "error") {
            clear(sliderNotice);
            sliderNotice.hidden = false;
            sliderNotice.append(
              h("p", null, `图片加载失败（${detail?.side === "source" ? "原图" : "生成结果"}）。`),
              h("div", { class: "gm-inline-actions" },
                button("重试加载", () => compare?.retry(), { kind: "muted" }),
                button("返回并排对照", () => setMode("side"), { kind: "muted" }),
              ),
            );
          }
        },
      });
      sliderHost.appendChild(compare.element);
    } else {
      compare.update({ sourceUrl, resultUrl });
    }
  }

  // ------------------------------------------------------------ keyboard
  function onKey(event) {
    if (closed || event.defaultPrevented || !isTopModal(dialog.dialog)) return;
    if (event.key === "Escape") return;
    const target = event.target;
    const element = target instanceof Element ? target : document.activeElement;
    if (element?.closest?.("input, select, textarea, [contenteditable='true'], [role='slider'], [role='textbox']")) return;
    const onButton = element?.closest?.("button, a, [role='button'], [role='link']");
    const isSpace = event.key === " " || event.code === "Space";
    if (isSpace && onButton) return;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      event.stopPropagation();
      client.moveResult(-1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      event.stopPropagation();
      client.moveResult(1);
    } else if (isSpace) {
      event.preventDefault();
      event.stopPropagation();
      toggleCurrent();
    }
  }
  document.addEventListener("keydown", onKey, true);
  unsubscribers.push(() => document.removeEventListener("keydown", onKey, true));

  // ComfyUI's global queue/cancel shortcuts must not fire while the workspace
  // owns the keyboard.  Escape stays with the modal close lifecycle.
  const swallow = (event) => {
    if (event.key !== "Escape") event.stopPropagation();
  };
  dialog.dialog.addEventListener("keydown", swallow);
  dialog.dialog.addEventListener("keyup", swallow);
  unsubscribers.push(() => {
    dialog.dialog.removeEventListener("keydown", swallow);
    dialog.dialog.removeEventListener("keyup", swallow);
  });

  // ------------------------------------------------------------- cleanup
  function cleanup() {
    if (closed) return;
    closed = true;
    for (const unsubscribe of unsubscribers.splice(0)) {
      try { unsubscribe(); } catch { /* ignore cleanup errors */ }
    }
    compare?.destroy();
    compare = null;
    releaseThumbs();
    sourceImg.removeAttribute("src");
    resultImg.removeAttribute("src");
    client.close();
    onClose?.();
  }

  render();
  client.open({ startSourceId, startOutputId, scope: "pending" });
  return {
    close: () => dialog.close("program"),
    getClient: () => client,
    element: dialog.root,
  };
}
