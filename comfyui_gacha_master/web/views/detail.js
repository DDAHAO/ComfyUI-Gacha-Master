import { idempotencyKey } from "../api.js";
import {
  badge, button, clear, confirmDialog, count, errorBox, fmtBytes, fmtTime, h,
  labeled, modal, progressBar, statusBadge, RUN_INTENT, REVIEW_STATUS, frag,
} from "../dom.js";
import { openCompare } from "./compare.js";
import { openReviewWorkspace } from "./review.js";
import { openRecovery } from "./recovery.js";
import { openExportWizard } from "./export.js";
import { SelectionController } from "../selection_controller.js";
import {
  createDelayedSkeleton, skeletonList, skeletonResultStrip, skeletonSourceRow,
} from "../components/skeleton.js";
import { attachTooltip } from "../components/tooltip.js";
import { thumbnailLoader } from "../components/lazy_image.js";
import { ResultPager, RESULT_PAGE_SIZE } from "../collections/result_pager.js";
import { SourceWindow, SOURCE_PAGE_SIZE } from "../collections/source_window.js";
import { registerResourceProvider, RESOURCE_BUDGETS } from "../resources.js";

const CREATION_STAGE_LABEL = {
  WAITING: "等待开始",
  COPYING: "准备原图",
  PLANNING: "生成计划",
  FINALIZING: "完成检查",
  PAUSING: "正在停止",
  PAUSED: "已停止（待继续）",
  DONE: "计划已就绪",
  FAILED: "创建失败",
  ABANDONED: "已放弃",
};

const MAX_RESULT_PAGERS = RESOURCE_BUDGETS.retainedResultSources;

export function mountDetail(container, ctx, jobId) {
  let destroyed = false;
  let job = null;
  let includeUnhealthy = false;
  let pollTimer = null;
  let refreshTimer = null;
  let tickCount = 0;
  let busy = false;
  let tickRunning = false;
  let jobError = null;
  let sourcesError = null;
  let exportError = null;
  let sourcesSkeleton = null;
  let expandedSourceId = null;
  let activePager = null;
  let reviewEntry = null;
  let reviewEntryError = null;
  let planReadyNotified = false;
  let lastJobStatus = null;
  const pagers = new Map();          // sourceId -> ResultPager (bounded by MAX_RESULT_PAGERS)
  const pagerRevisions = new Map();  // sourceId -> result_revision when cached
  const pagerAccess = new Map();     // sourceId -> last access timestamp
  const activeCards = new Map();     // outputId -> card refs (only current page is mounted)
  const cardPool = [];               // stable card nodes/images reused across pages and sources
  const observers = new Map();
  const rowRefs = new Map();
  const rowImages = new Map();
  const unsubscribers = [];
  let resultsSignature = "";
  let jobsAbort = null;
  let sourcesAbort = null;
  let cleanupRuns = [];
  let cleanupError = null;
  let cleanupBusy = false;
  let cleanupPoll = null;
  let cleanupActiveId = null;
  let lastStorageRevision = null;

  const headerArea = h("div", { class: "gm-detail-header" });
  const errorArea = h("div", { class: "gm-detail-errors" });
  const summaryArea = h("div", { class: "gm-summary-grid" });
  const detailStats = h("details", { class: "gm-detail-stats" });
  const controlArea = h("div", { class: "gm-control-bar" });
  const statusArea = h("div", { class: "gm-action-status", role: "status", "aria-live": "polite" });
  const connectionArea = h("div", { class: "gm-connection-area" });
  const sourcesBanner = h("div", { class: "gm-sources-banner" });
  const sourcesPager = h("div", { class: "gm-source-pager" });
  const sourcesArea = h("div", { class: "gm-source-rows", "aria-busy": "true" });
  const exportsArea = h("div", { class: "gm-export-history" });
  const reviewEntryArea = h("div", { class: "gm-review-entry" });
  const cleanupArea = h("div", { class: "gm-cleanup-body" });
  const cleanupButton = button("清理未入选结果", () => openCleanupPreview(), { kind: "muted" });
  const cleanupRefresh = button("刷新清理记录", () => loadCleanups(), { kind: "muted" });
  const exportToggle = h("button", { class: "gm-btn muted", type: "button", onclick: () => loadExports() }, "刷新导出记录");

  container.append(
    h("div", { class: "gm-section-head" },
      h("div", null,
        h("h2", null, "任务详情"),
        h("p", { class: "gm-help" }, "原图按页显示；展开一行查看该原图结果。每页结果与缓存都有上限，关闭页面不会停止后台任务。"),
      ),
      h("div", { class: "gm-inline-actions" },
        button("← 任务列表", () => ctx.navigate("jobs"), { kind: "muted" }),
        button("刷新", () => refreshAll(), { kind: "muted" }),
      ),
    ),
    errorArea,
    headerArea,
    connectionArea,
    summaryArea,
    detailStats,
    controlArea,
    statusArea,
    h("section", { class: "gm-card gm-cleanup-card" },
      h("div", { class: "gm-card-head" },
        h("h3", null, "存储清理"),
        h("div", { class: "gm-inline-actions" }, cleanupRefresh, cleanupButton),
      ),
      h("p", { class: "gm-help" },
        "“清理未入选结果”只删除已确认筛选组内、未入选且未被导出的完整结果图片。"
        + "原图、入选图片、导出成品、数据库备份和任务记录不会删除；删除后无法通过数据库备份找回图片。"),
      cleanupArea,
    ),
    h("section", { class: "gm-card" },
      h("div", { class: "gm-card-head" },
        h("h3", null, "原图与结果"),
        h("div", { class: "gm-inline-actions" },
          reviewEntryArea,
          h("label", { class: "gm-check-inline" },
            h("input", {
              type: "checkbox",
              onchange: (event) => { includeUnhealthy = event.target.checked; resetResultPagers(); },
            }),
            "包含异常输出",
          ),
        ),
      ),
      sourcesBanner,
      sourcesPager,
      sourcesArea,
    ),
    h("section", { class: "gm-card" },
      h("div", { class: "gm-card-head" }, h("h3", null, "导出记录"), exportToggle),
      exportsArea,
    ),
  );

  const sourceWindow = new SourceWindow({
    api: ctx.api,
    jobId,
    pageSize: SOURCE_PAGE_SIZE,
    onEvent: (event) => {
      if (destroyed) return;
      if (event.type === "state") {
        sourcesError = null;
        // Paging through the source window is not the initial page load; the
        // busy flag must clear here too or assistive technology and tests see
        // a permanently busy source list after the first navigation.
        sourcesArea.setAttribute("aria-busy", sourceWindow.loading ? "true" : "false");
        renderSourcesBanner();
        renderSources();
      } else if (event.type === "error") {
        sourcesError = sourceWindow.error;
        sourcesArea.setAttribute("aria-busy", "false");
        renderSourcesBanner();
      } else if (event.type === "loading") {
        sourcesArea.setAttribute("aria-busy", "true");
        renderSourcesBanner();
      }
    },
  });

  const unregisterResources = registerResourceProvider(() => ({
    view: "detail",
    mountedSourceRows: rowRefs.size,
    mountedOutputCards: activeCards.size,
    extra: {
      jobId,
      expandedSourceId,
      pagerCount: pagers.size,
      pagerItems: [...pagers.values()].reduce((sum, pager) => sum + pager.cachedItems, 0),
      pageSize: RESULT_PAGE_SIZE,
      sourcePageSize: SOURCE_PAGE_SIZE,
    },
  }));

  const selection = new SelectionController({
    api: ctx.api,
    notify: ctx.notify,
    onChange: (event) => {
      const card = activeCards.get(event.outputId);
      if (card) updateOutputCard(card, event.output);
      if (event.state === "saved") {
        loadJob();
        scheduleSourcesRefresh();
      } else if (event.state === "failed" && !event.conflict) {
        scheduleSourcesRefresh();
      }
    },
    onConflict: (event) => handleConflict(event.output),
  });
  unsubscribers.push(() => selection.destroy());
  unsubscribers.push(unregisterResources);
  unsubscribers.push(attachStore());

  function attachStore() {
    let last = "";
    return ctx.store.subscribe((state) => {
      const signature = `${state.pollError || ""}|${state.lastSyncAt || ""}`;
      if (signature === last) return;
      last = signature;
      renderConnection(state);
    });
  }

  function renderConnection(state = ctx.store.get()) {
    clear(connectionArea);
    if (!state.pollError) {
      connectionArea.hidden = true;
      return;
    }
    connectionArea.hidden = false;
    connectionArea.appendChild(h("div", { class: "gm-notice warn" },
      h("strong", null, "同步中断"),
      h("p", null, `${state.pollError}。当前显示的是最后已知任务状态，可能已经更新；后台任务不受浏览器连接影响。`),
      h("p", { class: "gm-help" }, `最后成功同步：${state.lastSyncAt ? new Date(state.lastSyncAt).toLocaleString() : "尚未成功"}`),
    ));
  }

  // ------------------------------------------------------------- loading
  async function loadJob({ silent = false } = {}) {
    jobsAbort?.abort();
    jobsAbort = new AbortController();
    try {
      const next = await ctx.api.job(jobId, { signal: jobsAbort.signal });
      if (destroyed) return;
      const previousStatus = job?.status;
      const previousCreationStage = job?.creation?.stage;
      const previousStorageRevision = lastStorageRevision;
      job = next;
      const currentStorageRevision = Number(job.summary?.storage_revision || 0);
      if (previousStorageRevision !== null && currentStorageRevision !== previousStorageRevision) {
        // Cached result pages reference images that may now be PURGED.
        resetResultPagers();
        loadCleanups();
      }
      lastStorageRevision = currentStorageRevision;
      if (previousStatus && previousStatus !== job.status && statusArea.dataset.transient === "1") {
        clear(statusArea);
        delete statusArea.dataset.transient;
      }
      if (
        previousCreationStage
        && previousCreationStage !== job.creation?.stage
        && job.creation?.stage === "DONE"
        && !planReadyNotified
      ) {
        planReadyNotified = true;
        ctx.notify(`计划已就绪（${count(job.creation.draws_committed)} DRAW）；请显式点击“开始 / 继续”。`, "ok");
      }
      if (job.status === "CREATING") planReadyNotified = false;
      lastJobStatus = job.status;
      jobError = null;
      renderErrors();
      renderHeader();
      renderSummary();
      renderControls();
      const active = ["RUNNING", "PAUSING"].includes(job.status);
      ctx.setJobActive(active);
    } catch (error) {
      if (destroyed || error?.name === "AbortError") return;
      jobError = error;
      renderErrors();
      if (!job && !silent) renderHeader();
    }
  }

  async function loadSources(force = false) {
    if (sourceWindow.loaded) {
      await sourceWindow.refresh();
    } else {
      await sourceWindow.loadFirst({ force });
    }
    if (destroyed) return;
    sourcesError = sourceWindow.error;
    if (!sourceWindow.error) {
      sourcesArea.setAttribute("aria-busy", "false");
      sourcesSkeleton?.destroy();
      sourcesSkeleton = null;
    }
    renderSourcesBanner();
    applySourceFacts();
  }

  /** React to new result revisions / selections for the current source page. */
  function applySourceFacts() {
    for (const source of sourceWindow.items) {
      const pager = pagers.get(source.id);
      const cachedRevision = pagerRevisions.get(source.id);
      if (pager && cachedRevision !== undefined && cachedRevision !== source.result_revision) {
        pagerRevisions.set(source.id, source.result_revision);
        pager.invalidate({ reload: source.id === expandedSourceId });
      }
      const row = rowRefs.get(source.id);
      if (row) updateSourceRow(row, source);
    }
    if (expandedSourceId) {
      const source = sourceWindow.items.find((item) => item.id === expandedSourceId);
      if (!source) {
        collapseResults();
      }
    }
  }

  async function loadExports() {
    try {
      const items = await ctx.api.exportsList(jobId);
      if (destroyed) return;
      exportError = null;
      clear(exportsArea);
      if (!items?.length) {
        exportsArea.appendChild(h("p", { class: "gm-help" }, "尚无导出批次。"));
        return;
      }
      for (const item of items) {
        const progress = item.progress || {};
        exportsArea.appendChild(h("div", { class: "gm-export-history-row" },
          h("span", { class: "gm-mono" }, item.batch_name || item.id.slice(0, 8)),
          badge(item.kind === "FINAL" ? "正式" : "临时", item.kind === "FINAL" ? "ok" : "muted"),
          badge(item.status, item.status === "COMPLETED" ? "ok" : item.status === "PARTIAL_FAILED" ? "bad" : "warn"),
          h("span", { class: "gm-help" }, `${item.verified_count || 0} / ${item.item_count || 0} 已复制`),
          progress.failed ? h("span", { class: "gm-inline-error" }, `${progress.failed} 项失败`) : null,
          h("span", { class: "gm-help" }, fmtTime(item.created_at)),
        ));
      }
    } catch (error) {
      if (destroyed) return;
      exportError = error;
      clear(exportsArea);
      exportsArea.appendChild(errorBox(error));
    }
  }

  function renderErrors() {
    clear(errorArea);
    if (!jobError) return;
    errorArea.appendChild(h("div", { class: "gm-inline-actions" },
      errorBox(jobError),
      button("重试读取任务", () => { loadJob(); loadSources(true); }, { kind: "muted" }),
    ));
  }

  let sourcesBannerSignature = "";
  function renderSourcesBanner() {
    const signature = [
      sourcesError ? sourcesError.message || String(sourcesError) : "",
      sourceWindow.loaded,
      sourceWindow.loading,
      sourceWindow.items.length,
      sourceWindow.activeSources,
      sourceWindow.totalSources,
      sourceWindow.removedSources,
      sourceWindow.pageIndex,
      sourceWindow.hasNext,
      sourceWindow.hasPrev,
    ].join("|");
    if (signature === sourcesBannerSignature && sourcesBanner.childElementCount) {
      return;
    }
    sourcesBannerSignature = signature;
    clear(sourcesBanner);
    if (sourcesError) {
      sourcesBanner.appendChild(h("div", { class: "gm-notice warn" },
        h("strong", null, "原图清单同步失败"),
        h("p", null, sourcesError.message || String(sourcesError)),
        h("div", { class: "gm-inline-actions" }, button("重试", () => loadSources(true), { kind: "muted" })),
      ));
      return;
    }
    if (!sourceWindow.loaded) {
      sourcesBanner.appendChild(h("p", { class: "gm-help", role: "status" }, "正在读取原图清单…"));
      return;
    }
    sourcesBanner.appendChild(h("div", { class: "gm-sources-counts" },
      h("span", null, `本页 ${count(sourceWindow.items.length)} 行`),
      h("span", null, `有效原图 ${count(sourceWindow.activeSources)} / ${count(sourceWindow.totalSources)}`),
      sourceWindow.removedSources ? h("span", { class: "gm-help" }, `逻辑移除 ${count(sourceWindow.removedSources)}（编号保留）`) : null,
    ));
    renderSourcePager();
  }

  function renderSourcePager() {
    clear(sourcesPager);
    const first = button("首页", async () => { await sourceWindow.first(); renderSourcesPage(); }, {
      kind: "muted", disabled: sourceWindow.loading || !sourceWindow.hasPrev,
    });
    const prev = button("← 上一页", async () => { await sourceWindow.prev(); renderSourcesPage(); }, {
      kind: "muted", disabled: sourceWindow.loading || !sourceWindow.hasPrev,
    });
    const next = button("下一页 →", async () => { await sourceWindow.next(); renderSourcesPage(); }, {
      kind: "muted", disabled: sourceWindow.loading || !sourceWindow.hasNext,
    });
    const jumpInput = h("input", {
      class: "gm-input gm-jump-input", type: "number", min: 1,
      max: sourceWindow.totalSources || 1, placeholder: "SRC 编号",
      title: "按稳定 source_order 定位（不会把全部原图载入内存）",
    });
    const jump = button("定位", async () => {
      const value = Number(jumpInput.value);
      if (!Number.isInteger(value) || value < 1) {
        ctx.notify("请输入有效的原图编号。", "warn");
        return;
      }
      await sourceWindow.jumpToOrder(value);
      renderSourcesPage();
    }, { kind: "muted", disabled: sourceWindow.loading });
    sourcesPager.append(
      first, prev,
      h("span", { class: "gm-pager-position" }, `${sourceWindow.pageLabel} · 每页 ${SOURCE_PAGE_SIZE}`),
      next,
      h("span", { class: "gm-pager-jump" }, jumpInput, jump),
    );
  }

  function renderSourcesPage() {
    renderSourcesBanner();
    renderSources();
  }

  // ------------------------------------------------------------- renders
  function renderHeader() {
    if (!job) return;
    clear(headerArea);
    headerArea.append(frag(
      h("div", { class: "gm-detail-title" },
        h("div", { class: "gm-detail-title-main" },
          h("h1", null, job.display_name || job.display_id),
          h("div", { class: "gm-inline-actions" },
            h("span", { class: "gm-mono" }, job.display_id),
            statusBadge(job.status),
            badge(RUN_INTENT[job.run_intent] || job.run_intent, job.run_intent === "RUN" ? "run" : "muted"),
            badge(job.protection_mode === "snapshot" ? "快照模式" : "引用模式", "muted"),
            job.status === "COMPLETED" ? badge("已完成", "ok") : null,
            job.creation ? badge(CREATION_STAGE_LABEL[job.creation.stage] || job.creation.stage,
              job.creation.stage === "DONE" ? "ok" : ["FAILED", "ABANDONED"].includes(job.creation.stage) ? "bad" : "warn") : null,
          ),
        ),
        h("div", { class: "gm-help" },
          `创建 ${fmtTime(job.created_at)} · 更新 ${fmtTime(job.updated_at)}`,
          job.started_at ? ` · 开始 ${fmtTime(job.started_at)}` : "",
          job.completed_at ? ` · 完成 ${fmtTime(job.completed_at)}` : "",
        ),
      ),
      job.error_message ? h("div", { class: "gm-error-box" },
        h("strong", null, job.error_code || "错误"), h("p", null, job.error_message),
      ) : null,
    ));
  }

  const summaryRefs = { values: {}, progress: null, progressLabel: null, current: null, stateBadge: null, creation: null };
  let summaryBuilt = false;

  function renderSummary() {
    if (!job) return;
    const summary = job.summary || {};
    const creation = job.creation || null;
    const succeeded = creation && job.status === "CREATING" ? creation.draws_committed : summary.succeeded;
    const effective = creation && job.status === "CREATING" ? creation.draws_total : summary.effective;
    const attention = Number(summary.unknown || 0) + Number(summary.failed || 0) + Number(summary.out_abnormal || 0);
    const primary = [
      ["成功抽卡", `${count(succeeded)} / ${count(effective)}`, ""],
      ["可用结果", count(summary.out_available), ""],
      ["已入选", count(summary.selected), ""],
      ["需要处理", attention ? String(attention) : "0", attention ? "bad" : ""],
    ];
    if (!summaryBuilt) {
      summaryBuilt = true;
      for (const [label] of primary) {
        const value = h("strong", null, "—");
        const card = h("div", { class: "gm-stat-card", dataset: { stat: label } }, h("span", null, label), value);
        summaryRefs.values[label] = { card, value };
        summaryArea.appendChild(card);
      }
      const progressHost = h("div", { class: "gm-progress-slot" });
      const current = h("span", { class: "gm-help" });
      const stateBadge = h("span");
      const creationLine = h("p", { class: "gm-help gm-creation-line" });
      summaryRefs.progress = progressHost;
      summaryRefs.current = current;
      summaryRefs.stateBadge = stateBadge;
      summaryRefs.creation = creationLine;
      summaryArea.appendChild(h("div", { class: "gm-stat-card wide" },
        h("div", { class: "gm-progress-head" }, h("span", null, "进度"), stateBadge),
        progressHost,
        current,
        creationLine,
      ));
    }
    for (const [label, value, kind] of primary) {
      const ref = summaryRefs.values[label];
      if (!ref) continue;
      ref.value.textContent = value;
      ref.card.className = `gm-stat-card ${kind}`;
    }
    clear(summaryRefs.stateBadge);
    summaryRefs.stateBadge.appendChild(job.status === "COMPLETED" ? badge("任务已完成", "ok") : badge("按后端状态显示", "muted"));
    clear(summaryRefs.progress);
    const progressLabel = job.status === "CREATING" && creation
      ? `${count(creation.draws_committed)} / ${count(creation.draws_total)} DRAW 已生成`
      : `${count(succeeded)} / ${count(effective)} 有效 DRAW`;
    summaryRefs.progress.appendChild(progressBar(succeeded || 0, effective || 0, progressLabel));
    summaryRefs.current.textContent = summary.current_source
      ? `当前原图：SRC-${String(summary.current_source.source_order).padStart(4, "0")} ${summary.current_source.original_name}`
      : "当前没有正在执行的 DRAW。";
    summaryRefs.creation.textContent = creation
      ? `创建阶段：${CREATION_STAGE_LABEL[creation.stage] || creation.stage} · 原图 ${creation.sources_committed}/${creation.sources_total} · 计划 ${creation.draws_committed}/${creation.draws_total}`
        + (creation.error_message ? ` · ${creation.error_message}` : "")
      : "";

    const wasOpen = detailStats.open;
    clear(detailStats);
    const rows = [
      ["原始计划", count(summary.total_planned)],
      ["追加计划", count(summary.appended)],
      ["在途", count(summary.inflight)],
      ["待执行", count(summary.pending)],
      ["失败/中断", count(summary.failed)],
      ["UNKNOWN", count(summary.unknown)],
      ["可用 OUT", count(summary.out_available)],
      ["异常 OUT", count(summary.out_abnormal)],
      ["已入选", count(summary.selected)],
      ["有效原图", `${count(summary.sources_active)} / ${count(summary.sources_total)}`],
      ["计划版本", count(job.plan_version)],
      ["任务版本", count(job.row_version)],
    ];
    if (creation) {
      rows.push(["创建代次", count(creation.generation)]);
      rows.push(["创建进度版本", count(creation.progress_version)]);
    }
    if (job.status === "RUNNING" && effective > 0 && succeeded === effective) {
      rows.push(["说明", "有效计划已全部成功；等待后端状态确认后才算完成。"]);
    }
    detailStats.append(
      h("summary", null, "执行明细与统计口径"),
      h("dl", { class: "gm-kv" }, ...rows.flatMap(([label, value]) => [h("dt", null, label), h("dd", null, value)])),
    );
    detailStats.open = wasOpen;
  }

  let controlSignature = "";
  const controlTooltips = [];
  function renderControls() {
    if (!job) return;
    const status = job.status;
    const summary = job.summary || {};
    const creation = job.creation || null;
    const signature = [status, job.run_intent, busy, summary.unknown, job.error_message,
      job.environment, job.row_version, creation?.stage, creation?.progress_version].join("|");
    if (signature === controlSignature && controlArea.childElementCount) return;
    controlSignature = signature;
    while (controlTooltips.length) controlTooltips.pop()();
    clear(controlArea);
    if (status === "CREATING") {
      const creationStage = creation?.stage;
      if (creationStage === "PAUSED" || creationStage === "PAUSING") {
        controlArea.append(
          button("继续创建 / 恢复计划", () => resumeCreation(), { kind: "primary", disabled: busy }),
          button("放弃创建", () => abandonCreation(), { kind: "danger", disabled: busy }),
        );
      } else if (creationStage === "FAILED" || creationStage === "ABANDONED") {
        controlArea.append(
          button("放弃创建", () => abandonCreation(), { kind: "danger", disabled: busy }),
        );
      } else if (creationStage === "DONE") {
        controlArea.appendChild(h("span", { class: "gm-help" }, "计划已完成，正在等待任务状态切换；不会自动开始推理。"));
      } else {
        controlArea.append(
          button("暂停创建", () => pauseCreation(), { kind: "warn", disabled: busy }),
          button("放弃创建", () => abandonCreation(), { kind: "danger", disabled: busy }),
        );
      }
      controlArea.appendChild(h("span", { class: "gm-help" }, "创建完成后仍需显式“开始 / 继续”才会推理。"));
    }
    if (["READY", "PAUSED", "BLOCKED"].includes(status) && job.run_intent !== "RUN") {
      controlArea.appendChild(button("开始 / 继续", () => startJob(), { kind: "primary", disabled: busy }));
    }
    if (status === "RECOVERY_REQUIRED" && job.run_intent !== "RUN") {
      controlArea.appendChild(button("已核验，确认继续", () => startJob({ acknowledge: true }), { kind: "primary", disabled: busy }));
    }
    if (["RUNNING", "PAUSING"].includes(status)) {
      controlArea.append(
        button("安全暂停", () => pause("safe"), { kind: "warn", disabled: busy }),
        button("立即暂停", () => pause("now"), { kind: "danger", disabled: busy }),
      );
    }
    if (status === "COMPLETED") {
      controlArea.appendChild(h("span", { class: "gm-help" }, "任务已完成；可以在原图行追加抽卡，或进入导出。"));
    }
    if (status === "RECOVERY_REQUIRED" || Number(summary.unknown || 0) > 0) {
      controlArea.appendChild(button("打开恢复面板", () => openRecoveryPanel(), { kind: "danger" }));
    }
    controlArea.appendChild(button("导出", () => openExport(), { kind: "primary" }));
    if (status === "RUNNING") {
      controlArea.appendChild(badge("后台运行中；关闭浏览器不会停止任务", "run"));
    }
    if (status === "PAUSING") {
      controlArea.appendChild(h("span", { class: "gm-help" }, "后端仍在等待当前项完成并核验；界面不会提前宣告已暂停。"));
    }
    if (status === "BLOCKED") {
      controlArea.appendChild(h("span", { class: "gm-inline-error" }, job.error_message || "任务已阻断，请检查错误与恢复入口。"));
    }
    for (const [label, content] of [
      ["安全暂停", "本次已投递的 DRAW 会完成并核验后再暂停。"],
      ["立即暂停", "只有目标实例验证过定向取消时才立即中断；否则等待当前执行结束。"],
    ]) {
      const target = [...controlArea.querySelectorAll("button")].find((btn) => btn.textContent === label);
      if (target) controlTooltips.push(attachTooltip(target, { content }));
    }
  }

  // ------------------------------------------------------------- sources
  function renderSources() {
    if (!sourceWindow.loaded && !sourceWindow.items.length) {
      if (!sourcesSkeleton) {
        sourcesSkeleton = createDelayedSkeleton(() => skeletonList(skeletonSourceRow, 2));
        clear(sourcesArea);
        sourcesArea.appendChild(sourcesSkeleton.element);
      }
      return;
    }
    sourcesSkeleton?.destroy();
    sourcesSkeleton = null;
    if (!sourceWindow.items.length) {
      clear(sourcesArea);
      for (const id of [...rowRefs.keys()]) removeSourceRow(id);
      sourcesArea.appendChild(h("p", { class: "gm-help" }, "没有可显示的原图（可能已全部逻辑移除）。"));
      return;
    }
    const live = new Set(sourceWindow.items.map((source) => source.id));
    for (const [id, row] of rowRefs) {
      if (live.has(id)) continue;
      removeSourceRow(id);
      row.remove();
    }
    sourceWindow.items.forEach((source, index) => {
      let row = rowRefs.get(source.id);
      if (!row) {
        row = buildSourceRow(source);
        rowRefs.set(source.id, row);
        setupObserver(row, source);
      } else {
        updateSourceRow(row, source);
      }
      // Reconcile position without re-appending an already-correct node:
      // moving DOM nodes on every poll loses focus and makes clicks unstable.
      if (sourcesArea.children[index] !== row) {
        sourcesArea.insertBefore(row, sourcesArea.children[index] || null);
      }
    });
    renderSourcePager();
  }

  function removeSourceRow(sourceId) {
    const row = rowRefs.get(sourceId);
    if (!row) return;
    if (expandedSourceId === sourceId) collapseResults();
    const img = rowImages.get(sourceId);
    if (img) thumbnailLoader.release(img);
    rowImages.delete(sourceId);
    observers.get(sourceId)?.disconnect();
    observers.delete(sourceId);
    rowRefs.delete(sourceId);
  }

  function setupObserver(row, source) {
    if (!("IntersectionObserver" in window)) return;
    if (observers.has(source.id)) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        observer.disconnect();
        observers.delete(source.id);
      }
    }, { rootMargin: "200px" });
    observer.observe(row);
    observers.set(source.id, observer);
  }

  function reviewBadge(source) {
    const review = source.review || {};
    const label = REVIEW_STATUS[review.status] || review.status || "未审核";
    const kind = review.status === "reviewed" ? "ok" : review.status === "selected_pending_review" ? "warn" : "muted";
    return badge(label, kind);
  }

  function sourceActionSignature(source) {
    const summary = source.summary || {};
    return `${summary.succeeded}|${summary.pending}|${summary.planned}|${expandedSourceId === source.id}`;
  }

  function resultsHostId(sourceId) {
    return `gm-results-${sourceId}`;
  }

  function buildSourceActions(source) {
    const summary = source.summary || {};
    const actions = h("div", { class: "gm-source-actions" });
    actions.dataset.signature = sourceActionSignature(source);
    const expanded = expandedSourceId === source.id;
    actions.appendChild(frag(
      button(expanded ? "收起结果" : "查看结果", () => toggleSourceResults(source), {
        kind: expanded ? "muted" : "primary",
      }),
      button("筛选本组", () => openReviewAt(source), {
        kind: "muted",
        disabled: Number(summary.available || 0) === 0,
        title: Number(summary.available || 0) === 0 ? "本组暂无可用结果" : "从本组开始连续筛选",
      }),
      button("追加抽卡", () => openAppend(source), { kind: "muted" }),
      summary.succeeded === 0 && summary.pending === summary.planned
        ? button("移除", () => removeSource(source), { kind: "muted" })
        : null,
    ));
    const toggle = actions.querySelector("button");
    if (toggle) {
      toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
      toggle.setAttribute("aria-controls", resultsHostId(source.id));
    }
    return actions;
  }

  function buildSourceRow(source) {
    const summary = source.summary || {};
    const row = h("article", { class: "gm-source-row", dataset: { sourceId: source.id, sourceOrder: source.source_order } });
    const order = h("div", { class: "gm-source-index" }, `SRC-${String(source.source_order).padStart(4, "0")}`);
    const thumbHost = h("div", { class: "gm-source-thumb-host" });
    const thumb = thumbnailLoader.create(
      ctx.api.imageUrl(source.thumbnail_url || `/sources/${source.id}/thumbnail`),
      source.original_name,
      "gm-source-thumb",
    );
    rowImages.set(source.id, thumb);
    thumbHost.appendChild(thumb);
    const name = h("div", { class: "gm-source-name gm-ellipsis", title: source.original_name }, source.original_name);
    const info = h("div", { class: "gm-help" }, `${source.width}×${source.height} · ${fmtBytes(source.size)} · ${source.status}`);
    const succeeded = h("span", null, `成功 ${count(summary.succeeded)}/${count(summary.planned)}`);
    const available = h("span", null, `可用 ${count(summary.available)}`);
    const selected = h("span", null, `已选 ${count(summary.selected)}`);
    const review = h("span", null, reviewBadge(source));
    const counts = h("div", { class: "gm-source-counts" }, succeeded, available, selected, review);
    const actions = buildSourceActions(source);
    // A row is collapsed by default.  The container itself is hidden here,
    // not merely emptied: late page/poll callbacks must not leave a toolbar,
    // grid, error line or skeleton visible under a collapsed row.
    const outputs = h("div", {
      class: "gm-output-strip",
      id: resultsHostId(source.id),
      hidden: true,
      "aria-hidden": "true",
    });
    row._refs = { order, name, info, succeeded, available, selected, review, actions, outputs, thumbHost };
    row.append(
      h("div", { class: "gm-source-meta" }, order, thumbHost, h("div", { class: "gm-source-info" }, name, info, counts), actions),
      outputs,
    );
    return row;
  }

  function updateSourceRow(row, source) {
    const refs = row._refs;
    const summary = source.summary || {};
    refs.name.textContent = source.original_name;
    refs.name.title = source.original_name;
    refs.info.textContent = `${source.width}×${source.height} · ${fmtBytes(source.size)} · ${source.status}`;
    refs.succeeded.textContent = `成功 ${count(summary.succeeded)}/${count(summary.planned)}`;
    refs.available.textContent = `可用 ${count(summary.available)}`;
    refs.selected.textContent = `已选 ${count(summary.selected)}`;
    clear(refs.review);
    refs.review.appendChild(reviewBadge(source));
    if (refs.actions.dataset.signature !== sourceActionSignature(source)) {
      const next = buildSourceActions(source);
      refs.actions.replaceWith(next);
      row._refs.actions = next;
    }
    row.classList.toggle("expanded", expandedSourceId === source.id);
  }

  // -------------------------------------------------------- review entry
  function renderReviewEntry() {
    clear(reviewEntryArea);
    const session = reviewEntry?.session || null;
    const resume = reviewEntry?.resume || null;
    const summary = reviewEntry?.summary || {};
    // The server resume decision is authoritative.  A session row can carry an
    // older finished_at value from a previous build; a NEXT_PENDING/IN_GROUP
    // resume still means "继续筛选".
    const resumable = Boolean(
      session && resume && (resume.mode === "IN_GROUP" || resume.mode === "NEXT_PENDING"),
    );
    const label = resumable
      ? `继续筛选${resume?.source ? ` · SRC-${String(resume.source.source_order).padStart(4, "0")}` : ""}`
      : "连续筛选";
    const entry = button(label, () => openContinuousReview(null), {
      kind: resumable ? "primary" : "muted",
      title: resumable
        ? `服务器断点：${fmtTime(session.updated_at)}`
        : "按原图组连续筛选；进度保存在服务器，重启后可继续",
    });
    entry.dataset.gmReviewEntry = "1";
    reviewEntryArea.appendChild(entry);
    if (reviewEntryError) {
      reviewEntryArea.appendChild(h("span", { class: "gm-inline-error" }, "筛选进度读取失败"));
      return;
    }
    if (resumable) {
      reviewEntryArea.appendChild(h("span", { class: "gm-help" }, `待处理 ${count(summary.pending_sources)} 组 · 已保存 ${fmtTime(session.updated_at)}`));
    } else if (session) {
      reviewEntryArea.appendChild(h("span", { class: "gm-help" }, "本轮已完成；可重新检查待复筛组"));
    }
  }

  async function loadReviewEntry() {
    try {
      const payload = await ctx.api.reviewSession(jobId);
      if (destroyed) return;
      reviewEntry = payload;
      reviewEntryError = null;
    } catch (error) {
      if (destroyed) return;
      reviewEntryError = error;
    }
    renderReviewEntry();
  }

  function openContinuousReview(startSourceId = null, startOutputId = null) {
    if (!job) return;
    openReviewWorkspace(ctx, job, {
      startSourceId,
      startOutputId,
      selection,
      onExport: () => openExport(),
      onClose: async () => {
        await loadJob();
        await loadSources(true);
        await loadReviewEntry();
        await syncSelectionStates();
      },
    });
  }

  function openReviewAt(source) {
    openContinuousReview(source.id);
  }

  function openReviewAtOutput(output) {
    const sourceId = pagerSourceForOutput(output.id);
    if (!sourceId) {
      ctx.notify("当前结果不在已加载页内，请重新展开本行结果。", "warn");
      return;
    }
    openContinuousReview(sourceId, output.id);
  }

  // ------------------------------------------------------------- results
  function hideResultsHost(row) {
    if (!row) return;
    row.classList.remove("expanded");
    const host = row._refs?.outputs;
    if (!host) return;
    host.hidden = true;
    host.setAttribute("aria-hidden", "true");
    // Collapse at the container layer.  Removing the toolbar/grid/error/
    // skeleton nodes (not only clearing JS maps) guarantees no residual
    // thumbnail, pager bar or placeholder stays visible or focusable, and a
    // later late response has nothing to reveal.
    while (host.firstChild) host.removeChild(host.firstChild);
    host._gmToolbar = null;
    host._gmGrid = null;
    resultsSignature = "";
  }

  function showResultsHost(row) {
    if (!row) return;
    const host = row._refs?.outputs;
    if (!host) return;
    row.classList.add("expanded");
    host.hidden = false;
    host.removeAttribute("aria-hidden");
  }

  function toggleSourceResults(source) {
    if (expandedSourceId === source.id) {
      collapseResults();
      return;
    }
    if (expandedSourceId) {
      const previous = rowRefs.get(expandedSourceId);
      hideResultsHost(previous);
      clearActiveResults();
    }
    expandedSourceId = source.id;
    activePager = null;
    const row = rowRefs.get(source.id);
    showResultsHost(row);
    ensurePager(source);
    updateAllSourceActionButtons();
    renderResults();
  }

  function collapseResults() {
    expandedSourceId = null;
    activePager = null;
    clearActiveResults();
    updateAllSourceActionButtons();
    for (const row of rowRefs.values()) hideResultsHost(row);
  }

  function updateAllSourceActionButtons() {
    for (const source of sourceWindow.items) {
      const row = rowRefs.get(source.id);
      if (!row) continue;
      const refs = row._refs;
      if (refs.actions.dataset.signature !== sourceActionSignature(source)) {
        const next = buildSourceActions(source);
        refs.actions.replaceWith(next);
        row._refs.actions = next;
      }
    }
  }

  function evictPagerIfNeeded(sourceId) {
    if (pagers.size < MAX_RESULT_PAGERS || pagers.has(sourceId)) return;
    let victim = null;
    let oldest = Infinity;
    for (const [id, pager] of pagers) {
      if (id === sourceId || id === expandedSourceId) continue;
      const access = pagerAccess.get(id) || 0;
      if (access < oldest) {
        oldest = access;
        victim = id;
      }
    }
    if (!victim) return;
    const pager = pagers.get(victim);
    pager?.destroy();
    pagers.delete(victim);
    pagerRevisions.delete(victim);
    pagerAccess.delete(victim);
  }

  function ensurePager(source) {
    let pager = pagers.get(source.id);
    const cachedRevision = pagerRevisions.get(source.id);
    if (pager && cachedRevision !== undefined && cachedRevision !== source.result_revision) {
      pager.invalidate({ reload: false });
    }
    if (!pager) {
      evictPagerIfNeeded(source.id);
      pager = new ResultPager({
        api: ctx.api,
        sourceId: source.id,
        pageSize: RESULT_PAGE_SIZE,
        includeUnhealthy,
        onEvent: (event) => {
          if (destroyed) return;
          if (event.type === "state") {
            pagerRevisions.set(source.id, source.result_revision);
          }
          // A late response may only render the row that is still expanded and
          // still owns this pager; it must never reopen a collapsed row.
          if (pager === activePager && expandedSourceId === source.id) renderResults();
          else if (event.type === "state") updateSourceRowIfPresent(source.id);
        },
      });
      pagers.set(source.id, pager);
      pager.loadFirst().catch(() => {});
    } else if (!pager.loaded && !pager.loading && !pager.error) {
      // The page was invalidated while collapsed; load it again on reopen.
      pager.loadFirst().catch(() => {});
    }
    pagerAccess.set(source.id, Date.now());
    activePager = pager;
    return pager;
  }

  function updateSourceRowIfPresent(sourceId) {
    const source = sourceWindow.items.find((item) => item.id === sourceId);
    const row = rowRefs.get(sourceId);
    if (source && row) updateSourceRow(row, source);
  }

  function clearActiveResults() {
    for (const card of activeCards.values()) {
      if (card.img) thumbnailLoader.release(card.img);
      card.output = null;
    }
    activeCards.clear();
    resultsSignature = "";
  }

  function ensureToolbar(host) {
    if (host._gmToolbar) return host._gmToolbar;
    const first = button("首页", () => host._gmToolbar.pager?.goTo(0), { kind: "muted" });
    const prev = button("← 上一页", () => host._gmToolbar.pager?.prev(), { kind: "muted" });
    const label = h("span", { class: "gm-pager-position" });
    const next = button("下一页 →", () => host._gmToolbar.pager?.next(), { kind: "muted" });
    const loading = h("span", { class: "gm-help", role: "status", hidden: true }, "读取中…");
    const retry = button("重试本页", () => host._gmToolbar.pager?.retry(), { kind: "warn", hidden: true });
    const bar = h("div", { class: "gm-result-toolbar" }, first, prev, label, next, loading, retry);
    host._gmToolbar = { bar, first, prev, label, next, loading, retry, pager: null };
    host.appendChild(bar);
    return host._gmToolbar;
  }

  function updateToolbar(toolbar, pager) {
    if (!toolbar) return;
    toolbar.pager = pager;
    const ready = pager.items.length;
    toolbar.label.textContent = `第 ${pager.currentIndex + 1} 页 · 本页 ${count(ready)} 张 · 缓存 ${pager.cachedPages} 页`;
    toolbar.first.disabled = pager.loading || !pager.hasPrev;
    toolbar.prev.disabled = pager.loading || !pager.hasPrev;
    toolbar.next.disabled = pager.loading || !pager.hasNext;
    toolbar.loading.hidden = !pager.loading;
    toolbar.retry.hidden = !pager.error;
  }

  function ensureGrid(host) {
    if (host._gmGrid) return host._gmGrid;
    const grid = h("div", { class: "gm-thumb-grid" });
    host._gmGrid = grid;
    host.appendChild(grid);
    return grid;
  }

  function createOutputCard() {
    const card = {
      node: h("div", { class: "gm-thumb-card", dataset: {} }),
      img: null,
      openButton: h("button", { class: "gm-thumb-open", type: "button" }),
      compareButton: h("button", {
        class: "gm-thumb-compare", type: "button", title: "在对照窗口中打开（Alt+点击缩略图同效）",
      }, "对照"),
      star: h("button", { class: "gm-star", type: "button" }),
      state: h("span", { class: "gm-thumb-state" }),
      mono: h("span", { class: "gm-mono" }),
      healthBadge: h("span", { class: "gm-badge bad gm-thumb-health", hidden: true }),
      purgedLabel: h("span", { class: "gm-purged-label", hidden: true }, "已清理"),
      output: null,
    };
    // ComfyUI's global Space/Enter shortcuts must not steal focus from the
    // result controls; otherwise a keyboard user cannot toggle twice in a row.
    let keyboardActivation = false;
    const keepActivation = (event) => {
      if (event.key === " " || event.key === "Enter") {
        keyboardActivation = true;
        event.stopPropagation();
      }
    };
    for (const control of [card.openButton, card.compareButton, card.star]) {
      control.addEventListener("keydown", keepActivation);
      control.addEventListener("keyup", keepActivation);
    }
    card.openButton.addEventListener("click", (event) => {
      if (!card.output) return;
      // Thumbnail click is the continuous-review workspace (the updated UI).
      // Alt/Ctrl/Meta+click keeps the original single-result compare window.
      if (event.altKey || event.ctrlKey || event.metaKey) openCompareAt(card.output);
      else openReviewAtOutput(card.output);
    });
    card.compareButton.addEventListener("click", (event) => {
      event.stopPropagation();
      if (card.output) openCompareAt(card.output);
    });
    card.star.addEventListener("click", (event) => {
      event.stopPropagation();
      if (card.output) selection.toggle(card.output);
      if (keyboardActivation) {
        keyboardActivation = false;
        // The ComfyUI canvas shortcut can claim focus after the button's
        // keyup; restore it once the current task and the shortcut settle.
        setTimeout(() => {
          if (card.star.isConnected && document.activeElement !== card.star) {
            card.star.focus({ preventScroll: true });
          }
        }, 50);
      }
    });
    card.node.append(
      card.openButton,
      h("div", { class: "gm-thumb-foot" }, card.mono, card.state, card.compareButton, card.star),
      card.healthBadge,
      card.purgedLabel,
    );
    return card;
  }

  function bindOutputCard(card, output) {
    card.output = output;
    card.node.dataset.outputId = output.id;
    card.node.title = `${output.health} · DRAW ${output.draw_no} · OUT ${output.out_no}`;
    const unavailable = (output.retention_state || "RETAINED") !== "RETAINED";
    const url = unavailable
      ? "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
      : ctx.api.imageUrl(output.thumbnail_url || `/outputs/${output.id}/thumbnail`);
    const alt = `DRAW ${output.draw_no} OUT ${output.out_no}`;
    if (!card.img) {
      card.img = thumbnailLoader.create(url, alt, "gm-thumb");
      card.openButton.appendChild(card.img);
      card.img.addEventListener("contextmenu", (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (!card.output || selection.isPending(card.output.id) || card.output.health !== "VERIFIED") return;
        selection.toggle(card.output);
      });
    } else {
      thumbnailLoader.assign(card.img, url, {});
      card.img.alt = alt;
    }
    card.openButton.setAttribute("aria-label", `在连续筛选中预览 ${alt}`);
    card.compareButton.setAttribute("aria-label", `在对照窗口中打开 ${alt}`);
    card.mono.textContent = `D${output.draw_no}/O${output.out_no}`;
    updateOutputCard(card, output);
  }

  function renderResults() {
    if (!expandedSourceId) return;
    const row = rowRefs.get(expandedSourceId);
    if (!row) return;
    const host = row._refs.outputs;
    if (host.hidden) return;
    const pager = activePager;
    if (!pager) return;
    const items = pager.items;
    const signature = `${pager.currentIndex}:${items.map((item) => item.id).join(",")}`;
    const changedPage = signature !== resultsSignature;
    const toolbar = ensureToolbar(host);
    updateToolbar(toolbar, pager);
    let errorLine = host.querySelector(".gm-result-error");
    if (pager.error) {
      if (!errorLine) {
        errorLine = h("div", { class: "gm-inline-error gm-result-error", role: "alert" });
        host.appendChild(errorLine);
      }
      errorLine.textContent = `读取结果失败：${pager.error.message || pager.error}。可重试同一页，游标不会前进。`;
    } else if (errorLine) {
      errorLine.remove();
    }
    const grid = ensureGrid(host);
    if (!pager.loaded && pager.loading && !items.length) {
      grid.hidden = true;
      if (!host.querySelector(".gm-result-skeleton")) {
        const skeleton = skeletonResultStrip();
        skeleton.classList.add("gm-result-skeleton");
        host.appendChild(skeleton);
      }
      return;
    }
    host.querySelector(".gm-result-skeleton")?.remove();
    if (!items.length) {
      grid.hidden = true;
      clearActiveResults();
      let empty = host.querySelector(".gm-result-empty");
      if (!empty) {
        empty = h("p", { class: "gm-help gm-result-empty" });
        host.appendChild(empty);
      }
      empty.textContent = includeUnhealthy
        ? "暂无结果。"
        : "暂无可用结果（可勾选“包含异常输出”查看异常证据）。";
      return;
    }
    host.querySelector(".gm-result-empty")?.remove();
    grid.hidden = false;
    if (changedPage) {
      clearActiveResults();
      resultsSignature = signature;
      while (cardPool.length < items.length) cardPool.push(createOutputCard());
      items.forEach((output, index) => {
        const card = cardPool[index];
        bindOutputCard(card, output);
        activeCards.set(output.id, card);
        if (grid.children[index] !== card.node) {
          grid.insertBefore(card.node, grid.children[index] || null);
        }
      });
      for (let index = items.length; index < cardPool.length; index += 1) {
        const card = cardPool[index];
        card.node.remove();
        card.output = null;
        if (card.img) thumbnailLoader.release(card.img);
      }
    }
  }

  function updateOutputCard(card, output) {
    if (!card || !output) return;
    card.output = output;
    const pending = selection.isPending(output.id);
    const retention = output.retention_state || "RETAINED";
    const unavailable = retention !== "RETAINED";
    card.node.classList.toggle("selected", Boolean(output.selected));
    card.node.classList.toggle("pending", pending);
    card.node.classList.toggle("unhealthy", output.health !== "VERIFIED");
    card.node.classList.toggle("purged", unavailable);
    card.star.textContent = output.selected ? "★" : "☆";
    card.star.classList.toggle("on", Boolean(output.selected));
    card.star.disabled = pending || output.health !== "VERIFIED" || (unavailable && !output.selected);
    card.state.textContent = pending ? "保存中…" : "";
    card.star.title = unavailable
      ? "结果图片已清理，不能入选"
      : output.health !== "VERIFIED" ? "异常输出不能入选" : output.selected ? "取消入选" : "入选";
    card.star.setAttribute("aria-label", `${output.selected ? "取消入选" : "入选"} DRAW ${output.draw_no} OUT ${output.out_no}`);
    card.healthBadge.hidden = output.health === "VERIFIED";
    if (output.health !== "VERIFIED") card.healthBadge.textContent = output.health;
    card.purgedLabel.hidden = !unavailable;
    card.purgedLabel.textContent = retention === "CLEANING" ? "清理中" : "已清理";
    card.openButton.disabled = unavailable;
    card.compareButton.disabled = unavailable;
  }

  async function handleConflict(output) {
    const sourceId = pagerSourceForOutput(output.id);
    if (sourceId) {
      const pager = pagers.get(sourceId);
      const card = activeCards.get(output.id);
      try {
        const states = await ctx.api.outputStates(sourceId, [output.id]);
        const changed = pager?.applyStates(states, { isPending: (id) => selection.isPending(id) }) || [];
        for (const item of changed) {
          const ref = activeCards.get(item.id);
          if (ref) updateOutputCard(ref, item);
        }
        const fresh = states.items?.[0];
        if (!fresh) {
          // The OUT is no longer part of the authoritative result set.
          if (pager) await pager.invalidate({ reload: true });
          clearActiveResults();
          renderResults();
        } else if (card) {
          updateOutputCard(card, card.output);
        }
      } catch (error) {
        ctx.notify("冲突后读取权威入选状态失败；请刷新原图结果。", "warn");
      }
    } else {
      ctx.notify("当前结果不在已加载页内，请刷新原图结果后再确认入选状态。", "warn");
    }
    loadJob();
    scheduleSourcesRefresh();
  }

  function pagerSourceForOutput(outputId) {
    for (const [sourceId, pager] of pagers) {
      if (pager.allCachedItems().some(({ item }) => item.id === outputId)) return sourceId;
    }
    return null;
  }

  function syncSelectionStates() {
    if (!sourceWindow.loaded) return Promise.resolve();
    const jobsToSync = [];
    for (const [sourceId, pager] of pagers) {
      const ids = pager.allCachedItems().map(({ item }) => item.id);
      if (ids.length) jobsToSync.push({ sourceId, pager, ids });
    }
    return Promise.all(jobsToSync.map(async ({ sourceId, pager, ids }) => {
      try {
        for (let offset = 0; offset < ids.length; offset += 200) {
          const chunk = ids.slice(offset, offset + 200);
          const states = await ctx.api.outputStates(sourceId, chunk);
          if (destroyed) return;
          const changed = pager.applyStates(states, { isPending: (id) => selection.isPending(id) });
          for (const item of changed) {
            const card = activeCards.get(item.id);
            if (card) updateOutputCard(card, item);
          }
          if (!states.items?.length && states.missing_ids?.length) {
            // Cached evidence vanished from the server view.  Do not silently
            // keep it selectable; reload this pager's current page.
            await pager.invalidate({ reload: pager === activePager });
          }
        }
      } catch {
        // A failed lightweight read never changes a visible selection state.
      }
    }));
  }

  function resetResultPagers() {
    for (const pager of pagers.values()) pager.destroy();
    pagers.clear();
    pagerRevisions.clear();
    pagerAccess.clear();
    clearActiveResults();
    resultsSignature = "";
    if (expandedSourceId) {
      const source = sourceWindow.items.find((item) => item.id === expandedSourceId);
      if (source) ensurePager(source);
    }
    renderResults();
  }

  function openCompareAt(output) {
    const sourceId = pagerSourceForOutput(output.id);
    const source = sourceWindow.items.find((item) => item.id === sourceId);
    const pager = sourceId ? pagers.get(sourceId) : null;
    if (!source || !pager) return;
    const index = pager.items.findIndex((item) => item.id === output.id);
    pagerAccess.set(sourceId, Date.now());
    openCompare(ctx, {
      source,
      pager,
      index: Math.max(0, index),
      includeUnhealthy,
      selection,
    });
  }

  // ------------------------------------------------------------- actions
  async function startJob({ acknowledge = false } = {}) {
    if (busy || !job) return;
    busy = true;
    clear(statusArea);
    statusArea.appendChild(h("span", { class: "gm-help" }, "正在重新校验输入与工作流…"));
    renderControls();
    try {
      const updated = await ctx.api.startJob(jobId, {
        expected_version: job.row_version,
        recovery_version: acknowledge ? job.recovery_version : undefined,
        acknowledge_recovery: acknowledge,
      }, idempotencyKey());
      job = updated;
      statusArea.dataset.transient = "1";
      statusArea.replaceChildren(h("span", { class: "gm-help" }, "任务已启动，后台将持续执行。"), button("查看恢复报告", () => openRecoveryPanel(), { kind: "muted" }));
      await loadJob();
      await loadSources(true);
    } catch (error) {
      delete statusArea.dataset.transient;
      statusArea.replaceChildren(errorBox(error));
      if (error.code === "RECOVERY_REQUIRED") openRecoveryPanel();
    } finally {
      busy = false;
      renderControls();
    }
  }

  async function resumeCreation() {
    if (busy || !job) return;
    busy = true;
    clear(statusArea);
    try {
      const updated = await ctx.api.startJob(job.id, { expected_version: job.row_version }, idempotencyKey());
      job = updated;
      statusArea.dataset.transient = "1";
      statusArea.replaceChildren(h("span", { class: "gm-help" },
        "已请求继续生成计划；计划完成前不会开始推理，完成后仍需再次点击“开始 / 继续”。"));
      await loadJob();
    } catch (error) {
      delete statusArea.dataset.transient;
      statusArea.replaceChildren(errorBox(error));
    } finally {
      busy = false;
      renderControls();
    }
  }

  async function pauseCreation() {
    if (busy || !job) return;
    busy = true;
    clear(statusArea);
    try {
      const result = await ctx.api.pauseJob(job.id, "safe");
      job = result.job;
      statusArea.dataset.transient = "1";
      statusArea.replaceChildren(h("div", { class: "gm-notice" },
        h("strong", null, `后端阶段：${result.stage}`),
        h("p", null, result.message || ""),
      ));
      await loadJob();
    } catch (error) {
      statusArea.replaceChildren(errorBox(error));
    } finally {
      busy = false;
      renderControls();
    }
  }

  async function abandonCreation() {
    if (!job || busy) return;
    if (!(await confirmDialog("放弃创建会保留已有检查点和源图副本，但该任务不会进入 READY，也不会自动执行。继续？", { danger: true, confirmLabel: "放弃创建" }))) return;
    busy = true;
    try {
      job = await ctx.api.abandonCreation(job.id, "用户在任务详情放弃创建");
      ctx.notify("已放弃创建；历史检查点保留。", "ok");
      await loadJob();
      await loadSources(true);
    } catch (error) {
      ctx.notify(`放弃创建失败：${error.message}`, "error");
    } finally {
      busy = false;
      renderControls();
    }
  }

  async function pause(mode) {
    if (busy || !job) return;
    busy = true;
    renderControls();
    clear(statusArea);
    statusArea.appendChild(h("span", { class: "gm-help" }, "已发送暂停请求；等待后端确认实际阶段…"));
    try {
      const result = await ctx.api.pauseJob(jobId, mode);
      job = result.job;
      renderHeader(); renderSummary(); renderControls();
      statusArea.dataset.transient = "1";
      statusArea.replaceChildren(h("div", { class: `gm-notice ${String(result.stage || "").startsWith("degraded") ? "warn" : ""}` },
        h("strong", null, `后端阶段：${result.stage}`),
        h("p", null, result.message || ""),
      ));
      await loadJob();
      await loadSources(true);
    } catch (error) {
      delete statusArea.dataset.transient;
      statusArea.replaceChildren(errorBox(error));
    } finally {
      busy = false;
      renderControls();
    }
  }

  function openAppend(source) {
    if (!job) return;
    const maxNo = Number(source.summary?.max_draw_no || 0);
    return openAppendModal(source, maxNo);
  }

  function openAppendModal(source, maxNo) {
    const dialog = modal({ title: `追加抽卡 · SRC-${String(source.source_order).padStart(4, "0")}` });
    const countInput = h("input", { class: "gm-input", type: "number", min: 1, max: 100000, value: 5, "data-autofocus": "1" });
    let submitting = false;
    const prioritySelect = h("select", { class: "gm-input" },
      h("option", { value: "priority", selected: true }, "优先执行（当前项之后）"),
      h("option", { value: "tail" }, "排到末尾"),
    );
    const preview = h("div", { class: "gm-append-preview" });
    const status = h("div");
    function updatePreview() {
      const value = Number(countInput.value);
      const valid = Number.isInteger(value) && value > 0;
      clear(preview);
      preview.append(
        h("div", { class: "gm-stat" }, h("span", null, "当前最高 DRAW"), h("strong", null, String(maxNo))),
        h("div", { class: "gm-stat" }, h("span", null, "新编号"), h("strong", null, valid ? `${maxNo + 1} … ${maxNo + value}` : "—")),
        h("div", { class: "gm-stat" }, h("span", null, "计划增加"), h("strong", null, valid ? String(value) : "—")),
      );
    }
    countInput.addEventListener("input", updatePreview);
    updatePreview();
    const confirmButton = button("确认追加", submit, { kind: "primary" });
    async function submit() {
      if (submitting) return;
      const value = Number(countInput.value);
      if (!Number.isInteger(value) || value < 1) {
        status.replaceChildren(errorBox({ code: "BAD_REQUEST", message: "追加次数必须是正整数。" }));
        return;
      }
      submitting = true;
      confirmButton.disabled = true;
      try {
        const result = await ctx.api.append(source.id, {
          count: value, priority: prioritySelect.value, expected_version: job.row_version,
        }, idempotencyKey());
        ctx.notify(`已追加 ${result.count} 次（DRAW ${result.draw_start}—${result.draw_end}）。`, "ok");
        dialog.close("saved");
        await loadJob();
        await loadSources(true);
        if (result.still_paused) {
          ctx.notify("追加只增加计划；任务仍保持暂停，需要显式开始。", "info");
        }
      } catch (error) {
        status.replaceChildren(errorBox(error));
        submitting = false;
        confirmButton.disabled = false;
      }
    }
    dialog.body.append(
      h("p", { class: "gm-help" }, "保留旧结果、选择、失败尝试和种子；追加会分配新的 DRAW 编号与新种子。暂停/阻断状态只追加计划，不会自动恢复运行。"),
      h("div", { class: "gm-form-row two" }, labeled("追加次数", countInput), labeled("执行顺序", prioritySelect)),
      preview,
      status,
    );
    dialog.footer.append(button("取消", () => dialog.close("cancel"), { kind: "muted" }), confirmButton);
  }

  async function removeSource(source) {
    if (!job) return;
    if (!(await confirmDialog(`确认移除 SRC-${String(source.source_order).padStart(4, "0")}？只有完全未执行的原图可以逻辑移除，编号不会回收。`, { danger: true, confirmLabel: "移除" }))) return;
    try {
      await ctx.api.removeSource(source.id, job.row_version);
      ctx.notify("原图已逻辑移除。", "ok");
      await loadJob();
      await loadSources(true);
    } catch (error) {
      ctx.notify(`移除失败：${error.message}`, "error");
    }
  }

  function openRecoveryPanel() {
    if (!job) return;
    openRecovery(ctx, job, {
      onResolved: async () => { await loadJob(); await loadSources(true); },
      onConfirmResume: () => startJob({ acknowledge: true }),
    });
  }

  function openExport() {
    if (!job) return;
    openExportWizard(ctx, job, [], {
      onDone: async () => { await loadJob(); await loadExports(); },
    });
  }

  // ------------------------------------------------------- storage cleanup
  function cleanupStatusLabel(status) {
    return {
      PREVIEW: "预览（未删除）",
      RUNNING: "正在清理",
      COMPLETED: "已完成",
      PARTIAL_FAILED: "部分失败",
      RECOVERY_REQUIRED: "需要核清",
      EXPIRED: "预览已过期",
      CANCELLED: "已停止/取消",
    }[status] || status || "—";
  }

  function cleanupStatusKind(status) {
    if (status === "COMPLETED") return "ok";
    if (status === "PARTIAL_FAILED" || status === "RECOVERY_REQUIRED") return "bad";
    if (status === "RUNNING") return "warn";
    return "";
  }

  async function loadCleanups() {
    cleanupRefresh.disabled = true;
    try {
      const payload = await ctx.api.jobCleanups(jobId);
      if (destroyed) return;
      cleanupRuns = payload.items || [];
      cleanupError = null;
      // The list DTO is bounded; fetch the latest run once so a reload shows
      // its failure reasons and live counters, not just the summary row.
      if (cleanupRuns.length) {
        try {
          const detail = await ctx.api.cleanupDetail(cleanupRuns[0].id);
          if (!destroyed) cleanupRuns[0] = detail;
        } catch {
          // Keep the list row; the explicit refresh still works.
        }
      }
      if (destroyed) return;
      renderCleanup();
      const active = cleanupRuns.find((run) => run.status === "RUNNING");
      if (active) {
        cleanupActiveId = active.id;
        scheduleCleanupPoll();
      }
    } catch (error) {
      if (destroyed) return;
      cleanupError = error;
      renderCleanup();
    } finally {
      cleanupRefresh.disabled = false;
    }
  }

  function renderCleanup() {
    clear(cleanupArea);
    const summary = job?.summary || {};
    const stats = h("div", { class: "gm-summary-grid gm-cleanup-stats" },
      h("div", { class: "gm-stat-card" }, h("span", null, "当前留存结果"), h("strong", null, count(summary.out_retained))),
      h("div", { class: "gm-stat-card" }, h("span", null, "已入选"), h("strong", null, count(summary.selected))),
      h("div", { class: "gm-stat-card" }, h("span", null, "已清理结果"), h("strong", null, count(summary.out_purged))),
      h("div", { class: "gm-stat-card" }, h("span", null, "已清理文件字节"), h("strong", null, fmtBytes(summary.out_purged_bytes || 0))),
    );
    cleanupArea.appendChild(stats);
    if (cleanupError) {
      cleanupArea.appendChild(errorBox(cleanupError));
      return;
    }
    const latest = cleanupRuns[0];
    if (!latest) {
      cleanupArea.appendChild(h("p", { class: "gm-help" }, "本任务还没有清理记录。点击“清理未入选结果”会先显示范围预览，不会立即删除。"));
      return;
    }
    const progress = latest.progress || {};
    const statusBox = h("div", { class: "gm-cleanup-run" },
      h("div", { class: "gm-inline-actions" },
        badge(cleanupStatusLabel(latest.status), cleanupStatusKind(latest.status)),
        h("span", { class: "gm-help" }, fmtTime(latest.created_at)),
      ),
      latest.confirmation_text ? h("p", { class: "gm-help" }, latest.confirmation_text) : null,
      progressBar(
        Number(progress.deleted || 0) + Number(progress.failed || 0) + Number(progress.skipped || 0),
        Number(progress.total || 0),
        `成功 ${count(progress.deleted)} · 失败 ${count(progress.failed)} · 跳过 ${count(progress.skipped)}`,
      ),
      latest.deleted_files
        ? h("p", { class: "gm-help" }, `本次逻辑释放 ${fmtBytes(latest.deleted_bytes || 0)}（其中孤立缩略图 ${fmtBytes(latest.deleted_thumbnail_bytes || 0)}）。`)
        : null,
      latest.error_message ? h("p", { class: "gm-notice bad" }, latest.error_message) : null,
      (latest.errors || []).length
        ? h("details", { class: "gm-detail-stats" },
          h("summary", null, `失败详情（${latest.errors.length}${latest.errors.length >= 10 ? "+" : ""}）`),
          h("ul", { class: "gm-warning-list" },
            ...latest.errors.map((item) => h("li", null,
              `${item.error_code || "FAILED"} · ${item.output_id.slice(0, 8)} · ${item.error_message || ""}`))),
        )
        : null,
    );
    cleanupArea.appendChild(statusBox);
    const history = cleanupRuns.slice(1);
    if (history.length) {
      cleanupArea.appendChild(h("details", { class: "gm-detail-stats" },
        h("summary", null, `历史清理记录（${history.length}）`),
        h("div", { class: "gm-cleanup-history" },
          ...history.map((run) => h("div", { class: "gm-cleanup-history-row" },
            badge(cleanupStatusLabel(run.status), cleanupStatusKind(run.status)),
            h("span", { class: "gm-help" }, `${run.candidate_count} 张 · 删除 ${run.deleted_files} · 失败 ${run.failed_count} · ${fmtTime(run.created_at)}`),
          ))),
      ));
    }
  }

  function scheduleCleanupPoll() {
    clearTimeout(cleanupPoll);
    cleanupPoll = setTimeout(refreshCleanupRun, 900);
  }

  async function refreshCleanupRun() {
    const runId = cleanupActiveId;
    if (destroyed || !runId) return;
    try {
      const run = await ctx.api.cleanupDetail(runId);
      if (destroyed) return;
      const index = cleanupRuns.findIndex((item) => item.id === run.id);
      if (index >= 0) cleanupRuns[index] = run;
      else cleanupRuns.unshift(run);
      renderCleanup();
      if (run.status === "RUNNING") {
        scheduleCleanupPoll();
      } else {
        cleanupActiveId = null;
        ctx.notify(
          run.status === "COMPLETED" ? `清理完成：成功删除 ${run.deleted_files} 张结果。` : `清理结束：${cleanupStatusLabel(run.status)}。`,
          run.status === "COMPLETED" ? "ok" : "warn",
        );
        resetResultPagers();
        await loadJob();
        await loadSources(true);
        await loadExports();
      }
    } catch (error) {
      if (!destroyed) cleanupError = error;
      scheduleCleanupPoll();
    }
  }

  function candidateRow(item) {
    const img = h("img", {
      class: "gm-cleanup-thumb",
      src: ctx.api.imageUrl(item.thumbnail_url),
      alt: `候选结果 ${item.output_id.slice(0, 8)}`,
      loading: "lazy",
    });
    return h("div", { class: "gm-cleanup-item" },
      img,
      h("div", null,
        h("div", { class: "gm-mono" }, `原图 #${item.source_order} · 抽次 ${item.draw_no} · OUT ${item.out_no}`),
        h("div", { class: "gm-help" }, `${fmtBytes(item.size)} · ${item.status}${item.error_message ? " · " + item.error_message : ""}`),
      ),
    );
  }

  async function openCleanupPreview() {
    if (cleanupBusy) return;
    cleanupBusy = true;
    cleanupButton.disabled = true;
    let preview;
    try {
      preview = await ctx.api.cleanupPreview(jobId, idempotencyKey());
    } catch (error) {
      cleanupBusy = false;
      cleanupButton.disabled = false;
      await openCleanupPreviewError(error);
      return;
    }
    cleanupBusy = false;
    cleanupButton.disabled = false;
    const dialog = modal({
      title: "预览：清理未入选结果",
      wide: true,
      description: "清理范围预览；此时不会删除任何文件。",
    });
    const body = dialog.body;
    const footer = dialog.footer;
    clear(body);
    clear(footer);
    if (preview.candidate_count === 0) {
      body.append(frag(
        h("p", { class: "gm-notice warn" }, "当前没有可清理的未入选结果。"),
        h("ul", { class: "gm-warning-list" },
          ...Object.entries(preview.excluded || {})
            .filter(([, value]) => Number(value) > 0)
            .map(([key, value]) => h("li", null, `${key}: ${value}`))),
        h("p", { class: "gm-help" }, preview.overlap_note || ""),
      ));
      footer.appendChild(button("关闭", () => dialog.close("cancel"), { kind: "muted" }));
      return;
    }
    body.append(frag(
      h("p", { class: "gm-cleanup-warning" },
        `将永久删除本任务中已完成筛选且未入选的 ${preview.candidate_count} 张结果，`
        + `预计减少 ${fmtBytes(preview.estimated_bytes)} 的文件占用（另含孤立缩略图 ${fmtBytes(preview.estimated_thumbnail_bytes)}）。`
        + "入选图片、原图、导出成品和任务记录会保留。删除后无法通过数据库备份找回图片。"),
      preview.all_skipped
        ? h("p", { class: "gm-notice bad" }, "全部原图组都被明确跳过：本任务将不再保留这些生成图片。")
        : null,
      h("div", { class: "gm-summary-grid" },
        h("div", { class: "gm-stat-card" }, h("span", null, "候选"), h("strong", null, preview.candidate_count)),
        h("div", { class: "gm-stat-card" }, h("span", null, "入选保护"), h("strong", null, preview.protected_selected_count)),
        h("div", { class: "gm-stat-card" }, h("span", null, "导出引用保护"), h("strong", null, preview.protected_export_count)),
        h("div", { class: "gm-stat-card" }, h("span", null, "过期时间"), h("strong", null, fmtTime(preview.expires_at))),
      ),
      h("p", { class: "gm-help" }, preview.overlap_note || ""),
      Object.entries(preview.excluded || {}).some(([, value]) => Number(value) > 0)
        ? h("details", { class: "gm-detail-stats" },
          h("summary", null, "其他排除原因"),
          h("ul", { class: "gm-warning-list" },
            ...Object.entries(preview.excluded)
              .filter(([, value]) => Number(value) > 0)
              .map(([key, value]) => h("li", null, `${key}: ${value}`))))
        : null,
    ));
    const list = h("div", { class: "gm-cleanup-items" });
    const more = h("div", { class: "gm-inline-actions" });
    let cursor = null;
    async function loadPage() {
      const page = await ctx.api.cleanupItems(preview.id, { limit: 50, cursor });
      if (destroyed) return;
      for (const item of page.items) list.appendChild(candidateRow(item));
      cursor = page.next_cursor;
      clear(more);
      if (cursor) more.appendChild(button("加载更多候选", loadPage, { kind: "muted" }));
    }
    body.append(h("h4", null, "候选结果"), list, more);
    loadPage().catch((error) => body.appendChild(errorBox(error)));
    footer.append(
      button("取消", () => dialog.close("cancel"), { kind: "muted" }),
      button(`永久清理 ${preview.candidate_count} 张结果`, async () => {
        const ok = await confirmDialog(
          `将永久删除 ${preview.candidate_count} 张未入选结果，预计释放 ${fmtBytes(preview.estimated_bytes)}。`
          + "删除后无法撤销，也无法通过数据库备份找回图片。继续？",
          { title: "确认永久清理", danger: true, confirmLabel: "永久删除" },
        );
        if (!ok) return;
        dialog.close("confirmed");
        await startCleanup(preview);
      }, { kind: "danger" }),
    );
  }

  async function openCleanupPreviewError(error) {
    const dialog = modal({ title: "无法清理未入选结果", wide: false });
    clear(dialog.body);
    clear(dialog.footer);
    dialog.body.appendChild(errorBox(error));
    const details = error?.details || {};
    if (details.pending_sources?.length) {
      dialog.body.appendChild(h("p", { class: "gm-help" }, `仍有 ${details.pending_count || details.pending_sources.length} 个原图组未确认，请先继续筛选。`));
    }
    if (details.reason) dialog.body.appendChild(h("p", { class: "gm-help" }, String(details.reason)));
    dialog.footer.append(
      button("关闭", () => dialog.close("cancel"), { kind: "muted" }),
      details.next_action === "continue_review"
        ? button("继续筛选", () => { dialog.close("continue"); openContinuousReview(); }, { kind: "primary" })
        : null,
    );
  }

  async function startCleanup(preview) {
    cleanupBusy = true;
    try {
      const run = await ctx.api.cleanupConfirm(
        preview.id,
        {
          plan_token: preview.plan_token,
          expected_candidate_count: preview.candidate_count,
          confirm_permanent_delete: true,
        },
        idempotencyKey(),
      );
      cleanupActiveId = run.id;
      cleanupRuns = [run, ...cleanupRuns.filter((item) => item.id !== run.id)];
      renderCleanup();
      ctx.notify("后台清理已开始；关闭页面不会取消。", "info");
      scheduleCleanupPoll();
      await loadCleanups();
    } catch (error) {
      ctx.notify(error.message, "error");
      await openCleanupPreviewError(error);
    } finally {
      cleanupBusy = false;
    }
  }

  function refreshAll() {
    loadJob();
    loadSources(true);
    loadExports();
    loadReviewEntry();
    loadCleanups();
    syncSelectionStates();
  }

  // -------------------------------------------------------------- polling
  async function tick() {
    if (destroyed || tickRunning) return;
    tickRunning = true;
    try {
      await loadJob();
      tickCount += 1;
      if (tickCount % 3 === 0) await sourceWindow.refresh();
      if (tickCount % 4 === 0) await syncSelectionStates();
      if (tickCount % 10 === 0) await loadReviewEntry();
    } finally {
      tickRunning = false;
      if (!destroyed) pollTimer = setTimeout(tick, ctx.isActive() ? 2000 : 8000);
    }
  }

  function scheduleSourcesRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(async () => {
      await loadJob();
      await sourceWindow.refresh();
      await syncSelectionStates();
    }, 250);
  }

  if (!sourceWindow.loaded) {
    sourcesSkeleton = createDelayedSkeleton(() => skeletonList(skeletonSourceRow, 2));
    sourcesArea.appendChild(sourcesSkeleton.element);
  }
  renderConnection();
  loadJob().then(async () => {
    await loadSources(true);
    await loadExports();
    await loadReviewEntry();
    await loadCleanups();
    tick();
  });

  return {
    destroy() {
      destroyed = true;
      clearTimeout(pollTimer);
      clearTimeout(refreshTimer);
      clearTimeout(cleanupPoll);
      jobsAbort?.abort();
      sourcesAbort?.abort();
      for (const observer of observers.values()) observer.disconnect();
      observers.clear();
      while (controlTooltips.length) controlTooltips.pop()();
      for (const unsubscribe of unsubscribers.splice(0)) unsubscribe();
      for (const pager of pagers.values()) pager.destroy();
      pagers.clear();
      for (const img of rowImages.values()) thumbnailLoader.release(img);
      rowImages.clear();
      clearActiveResults();
      sourceWindow.destroy();
      sourcesSkeleton?.destroy();
      ctx.setJobActive(false);
    },
    refresh() { refreshAll(); },
    onEvents(events) {
      if (!events?.length) return;
      if (events.some((event) => event.job_id === jobId || ["JOB", "ATTEMPT", "DRAW", "BLOCK"].includes(event.entity_type))) loadJob();
      if (events.some((event) => ["SELECTION", "OUT", "SRC", "JOB", "BLOCK"].includes(event.entity_type))) scheduleSourcesRefresh();
      if (events.some((event) => event.entity_type === "EXPORT")) loadExports();
      if (events.some((event) => event.entity_type === "CLEANUP")) {
        loadCleanups();
        loadJob();
      }
    },
    onGap() { refreshAll(); },
    onReconnect() { refreshAll(); },
  };
}
