import {
  button, clear, count, errorBox, fmtTime, h, labeled, modal, progressBar, statusBadge, RUN_INTENT,
} from "../dom.js";
import { createDelayedSkeleton, skeletonJobCard, skeletonList } from "../components/skeleton.js";

export function mountJobs(container, ctx) {
  let destroyed = false;
  let cursor = null;
  let pageIndex = 0;
  let timer = null;
  let refreshDebounce = null;
  let loading = false;
  let loaded = false;
  let items = [];
  let error = null;
  let lastSuccessAt = null;
  let skeleton = null;
  const cards = new Map();
  const deletedJobIds = new Set();

  const header = h("div", { class: "gm-section-head" },
    h("div", null,
      h("h2", null, "抽卡任务"),
      h("p", { class: "gm-help" }, "后台执行独立于浏览器；关闭页面不会停止已授权的任务。"),
    ),
    button("+ 新建任务", () => ctx.navigate("create"), { kind: "primary" }),
  );
  const connectionArea = h("div", { class: "gm-connection-area" });
  const list = h("div", { class: "gm-job-list", "aria-busy": "true" });
  const banner = h("div", { class: "gm-list-banner" });
  const more = h("div", { class: "gm-more" });
  container.append(header, connectionArea, banner, list, more);

  const unsubscribe = ctx.store.subscribe((state) => renderConnection(state));

  function renderConnection(state = ctx.store.get()) {
    clear(connectionArea);
    if (state.pollError) {
      connectionArea.appendChild(h("div", { class: "gm-notice warn", role: "status" },
        h("strong", null, "同步中断"),
        h("p", null, `${state.pollError}。下方保留最后一次成功读取的任务状态，可能已经更新；后台任务不受影响。`),
        h("p", { class: "gm-help" }, `最后成功同步：${state.lastSyncAt ? new Date(state.lastSyncAt).toLocaleString() : "尚未成功"}`),
      ));
    }
  }

  function renderBanner() {
    clear(banner);
    if (!error) return;
    if (items.length) {
      banner.appendChild(h("div", { class: "gm-notice warn" },
        h("strong", null, "任务列表刷新失败，保留上次内容"),
        h("p", null, error.message || String(error)),
        h("p", { class: "gm-help" }, `最后成功刷新：${lastSuccessAt ? new Date(lastSuccessAt).toLocaleString() : "尚未成功"}`),
        h("div", { class: "gm-inline-actions" }, button("重试", () => refreshFirst(), { kind: "muted" })),
      ));
    }
  }

  function createJobCard(job) {
    const title = h("h3");
    const idText = h("span", { class: "gm-mono" });
    const badgeHost = h("span");
    const stats = {};
    const statHost = h("div", { class: "gm-job-meta" });
    for (const [key, label] of [
      ["succeeded", "成功抽卡"], ["available", "可用结果"], ["selected", "已入选"], ["sources", "原图"], ["intent", "运行意图"],
    ]) {
      const value = h("strong");
      stats[key] = value;
      statHost.appendChild(h("span", { class: "gm-job-stat" }, h("span", null, label), value));
    }
    const progressHost = h("div");
    const current = h("div", { class: "gm-current" });
    const errorHost = h("div");
    const created = h("span");
    const node = h("article", {
      class: "gm-job-card",
      role: "link",
      tabindex: "0",
      onclick: (event) => {
        if (event.target.closest("button, a")) return;
        ctx.navigate("detail", { jobId: job.id });
      },
      onkeydown: (event) => {
        if (event.target === event.currentTarget && event.key === "Enter") {
          event.preventDefault();
          ctx.navigate("detail", { jobId: job.id });
        }
      },
    },
      h("div", { class: "gm-job-top" },
        h("div", { class: "gm-job-title" }, title, idText),
        badgeHost,
      ),
      statHost,
      progressHost,
      current,
      errorHost,
      h("div", { class: "gm-job-foot" },
        created,
        h("div", { class: "gm-inline-actions" },
          button("删除项目", () => openDelete(job), { kind: "danger" }),
          h("button", {
            class: "gm-link-btn",
            type: "button",
            onclick: () => ctx.navigate("detail", { jobId: job.id }),
          }, "查看详情 →"),
        ),
      ),
    );
    return { node, title, idText, badgeHost, stats, progressHost, current, errorHost, created };
  }

  function openDelete(job) {
    const dialog = modal({ title: "删除项目" });
    const projectName = job.display_name || job.display_id;
    const formId = `gm-delete-${job.id}`;
    const input = h("input", {
      class: "gm-input", type: "text", autocomplete: "off", "data-autofocus": "1",
    });
    input.spellcheck = false;
    const status = h("div", { role: "status" });
    const cancel = button("取消", () => dialog.close("cancel"), { kind: "muted" });
    const confirm = button("永久删除", null, { kind: "danger", disabled: true, type: "submit" });
    confirm.setAttribute("form", formId);
    let submitting = false;
    dialog.body.append(
      h("p", null, "将永久删除项目、项目内的原图快照及全部生成图片（含已入选图片）。导出文件和项目外的原图会保留。"),
      h("p", { style: { overflowWrap: "anywhere" } }, "项目名称：", h("strong", null, projectName)),
      h("form", { id: formId, onsubmit: submit }, labeled("输入项目名称以确认", input), status),
    );
    dialog.footer.append(cancel, confirm);
    input.addEventListener("input", () => { confirm.disabled = input.value !== projectName; });

    function removeCard() {
      deletedJobIds.add(job.id);
      if (destroyed) return;
      items = items.filter((item) => item.id !== job.id);
      renderList();
    }

    async function submit(event) {
      event.preventDefault();
      if (submitting || input.value !== projectName) return;
      submitting = true;
      input.disabled = cancel.disabled = confirm.disabled = true;
      confirm.textContent = "正在删除…";
      clear(status);
      try {
        await ctx.api.deleteJob(job.id, input.value);
        removeCard();
        dialog.close("saved");
        ctx.notify("项目已删除。", "ok");
        refreshFirst();
      } catch (error) {
        if (error.code === "JOB_DELETE_INCOMPLETE") removeCard();
        if (dialog.isOpen()) status.replaceChildren(errorBox(error));
        else ctx.notify(error.message || "删除项目失败。", "error");
      } finally {
        submitting = false;
        input.disabled = cancel.disabled = false;
        confirm.disabled = input.value !== projectName;
        confirm.textContent = "永久删除";
      }
    }
  }

  function updateJobCard(refs, job) {
    const summary = job.summary || {};
    refs.title.textContent = job.display_name || job.display_id;
    refs.idText.textContent = job.display_id;
    refs.node.setAttribute("aria-label", `打开任务 ${job.display_name || job.display_id}`);
    refs.node.dataset.jobId = job.id;
    clear(refs.badgeHost);
    refs.badgeHost.appendChild(statusBadge(job.status));
    refs.stats.succeeded.textContent = `${count(summary.succeeded)} / ${count(summary.effective)}`;
    refs.stats.available.textContent = count(summary.out_available);
    refs.stats.selected.textContent = count(summary.selected);
    refs.stats.sources.textContent = count(summary.sources_active);
    refs.stats.intent.textContent = RUN_INTENT[job.run_intent] || job.run_intent || "—";
    clear(refs.progressHost);
    refs.progressHost.appendChild(progressBar(summary.succeeded || 0, summary.effective || 0, `${count(summary.succeeded)} / ${count(summary.effective)} 有效 DRAW`));
    refs.current.textContent = summary.current_source
      ? `当前原图：SRC-${String(summary.current_source.source_order).padStart(4, "0")} ${summary.current_source.original_name}`
      : "";
    refs.current.hidden = !summary.current_source;
    clear(refs.errorHost);
    if (job.error_message) {
      refs.errorHost.appendChild(h("div", { class: "gm-inline-error" }, `${job.error_code || "错误"}：${job.error_message}`));
    }
    refs.created.textContent = `创建于 ${fmtTime(job.created_at)}`;
  }

  function renderList() {
    if (!loaded) {
      skeleton?.destroy();
      skeleton = createDelayedSkeleton(() => skeletonList(skeletonJobCard, 3));
      clear(list);
      list.appendChild(skeleton.element);
      return;
    }
    list.setAttribute("aria-busy", "false");
    clear(more);
    if (pageIndex > 0) {
      more.appendChild(h("span", { class: "gm-help" }, `正在查看较早任务（第 ${pageIndex + 1} 页）。本页不会自动刷新。`));
      more.appendChild(button("回到最新任务", () => loadFirst({ reset: true }), { kind: "primary" }));
    }
    if (cursor) more.appendChild(button("加载更早任务", () => loadMore(), { kind: "muted" }));
    if (!items.length) {
      for (const card of cards.values()) card.node.remove();
      cards.clear();
      clear(list);
      list.appendChild(h("div", { class: "gm-empty" },
        h("p", null, "还没有抽卡任务。新建任务导入原图并绑定工作流即可开始。"),
        h("p", { class: "gm-help" }, "创建任务只进入任务详情，需要显式点击“开始 / 继续”。"),
        button("新建任务", () => ctx.navigate("create"), { kind: "primary" }),
      ));
      return;
    }
    // Remove the empty/error placeholder if it is still the only child.
    if (list.firstElementChild && !list.firstElementChild.classList.contains("gm-job-card")) clear(list);
    const live = new Set(items.map((job) => job.id));
    for (const [id, refs] of cards) {
      if (!live.has(id)) {
        refs.node.remove();
        cards.delete(id);
      }
    }
    items.forEach((job, index) => {
      let refs = cards.get(job.id);
      if (!refs) {
        refs = createJobCard(job);
        cards.set(job.id, refs);
      }
      updateJobCard(refs, job);
      // Reconcile order without moving an already-correct node; re-appending
      // on every poll breaks focus and Playwright actionability.
      if (list.children[index] !== refs.node) {
        list.insertBefore(refs.node, list.children[index] || null);
      }
    });
  }

  async function loadFirst({ reset = false } = {}) {
    if (loading || destroyed) return;
    if (!reset && pageIndex > 0) {
      // Older pages are a stable view; background refresh must not silently
      // replace them with the newest page.
      return;
    }
    loading = true;
    try {
      const payload = await ctx.api.listJobs(null);
      if (destroyed) return;
      items = (payload.items || []).filter((job) => !deletedJobIds.has(job.id));
      cursor = payload.next_cursor;
      pageIndex = 0;
      loaded = true;
      error = null;
      lastSuccessAt = Date.now();
      skeleton?.destroy();
      skeleton = null;
      renderList();
      renderBanner();
    } catch (failure) {
      if (destroyed) return;
      error = failure;
      if (!loaded) {
        loaded = true;
        skeleton?.destroy();
        skeleton = null;
        clear(list);
        list.setAttribute("aria-busy", "false");
        list.appendChild(h("div", { class: "gm-list-error" },
          errorBox(failure),
          button("重试读取任务", () => { loaded = false; error = null; renderList(); loadFirst(); }, { kind: "muted" }),
        ));
      } else {
        renderBanner();
      }
    } finally {
      loading = false;
    }
  }

  async function loadMore() {
    if (!cursor || loading || destroyed) return;
    loading = true;
    try {
      const payload = await ctx.api.listJobs(cursor);
      if (destroyed) return;
      // A finite page window: the previous page's cards are unmounted, so the
      // task list cannot grow without bound.  "回到最新任务" returns to the
      // first page; forward pagination makes older jobs reachable.
      items = (payload.items || []).filter((job) => !deletedJobIds.has(job.id));
      cursor = payload.next_cursor;
      pageIndex += 1;
      error = null;
      renderList();
      renderBanner();
    } catch (failure) {
      if (destroyed) return;
      error = failure;
      renderBanner();
    } finally {
      loading = false;
    }
  }

  function refreshFirst() { return loadFirst({ reset: false }); }

  function schedule() {
    clearTimeout(timer);
    if (destroyed || !ctx.isActive()) return;
    timer = setTimeout(async () => {
      await refreshFirst();
      schedule();
    }, 4000);
  }

  ctx.setJobActive(false);
  renderList();
  loadFirst({ reset: true }).then(schedule);
  renderConnection();

  return {
    destroy() {
      destroyed = true;
      clearTimeout(timer);
      clearTimeout(refreshDebounce);
      unsubscribe();
      skeleton?.destroy();
    },
    refresh() { return loadFirst({ reset: true }); },
    onEvents(events) {
      if (!events?.length) return;
      const relevant = events.some((event) => ["JOB", "DRAW", "ATTEMPT", "EXPORT", "BLOCK"].includes(event.entity_type));
      if (!relevant) return;
      clearTimeout(refreshDebounce);
      refreshDebounce = setTimeout(() => refreshFirst(), 200);
    },
    onGap() { loadFirst({ reset: false }); },
    onReconnect() { loadFirst({ reset: false }); },
  };
}
