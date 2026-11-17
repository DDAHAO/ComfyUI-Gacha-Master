import { idempotencyKey } from "../api.js";
import {
  badge, button, clear, errorBox, fmtTime, h, labeled, modal, progressBar, spinner, frag,
} from "../dom.js";

const REVIEW_LABEL = { SELECTED: "已确认入选", SKIPPED: "已确认跳过" };
const EXPORT_STAGE_LABELS = ["选择类型与布局", "审核预览", "冻结确认", "复制结果"];

export function openExportWizard(ctx, job, sources, { onDone } = {}) {
  const settings = ctx.store.get().settings || {};
  const roots = settings.allowed_export_roots || [];
  const dialog = modal({ title: `导出 · ${job.display_id}`, wide: true });
  const stageBar = h("ol", { class: "gm-export-stages", "aria-label": "导出步骤" });
  const body = h("div", { class: "gm-export-body" });
  const errorArea = h("div", { class: "gm-export-errors" });
  const footer = h("div", { class: "gm-inline-actions gm-export-footer" });
  dialog.body.append(stageBar, errorArea, body);
  dialog.footer.appendChild(footer);

  let kind = "QUICK";
  let layout = settings.export_layout || "FLAT";
  let targetRoot = roots[0] || "";
  let draft = null;
  let confirmed = null;
  let busy = false;
  let polling = false;
  let pollTimer = null;
  let closed = false;
  let lastError = null;
  let needsRefresh = false;
  let completionReported = false;

  function stage() {
    if (confirmed) return 4; // freeze is committed; copying is the current stage
    return draft ? 2 : 1;
  }

  function renderStage() {
    clear(stageBar);
    const currentIndex = Math.max(0, Math.min(3, stage() - 1));
    EXPORT_STAGE_LABELS.forEach((label, index) => {
      const state = index < currentIndex ? "done" : index === currentIndex ? "current" : "locked";
      const item = h("li", {
        class: `gm-export-stage ${state}`,
        "aria-current": state === "current" ? "step" : undefined,
      },
      h("span", { class: "gm-export-stage-index", "aria-hidden": "true" }, state === "done" ? "✓" : String(index + 1)),
      h("span", null, label));
      stageBar.appendChild(item);
    });
  }

  function showErrors(error) {
    lastError = error;
    if (!error) return;
    if (error.status === 409) needsRefresh = true;
  }

  function render() {
    if (closed) return;
    renderStage();
    clear(body);
    clear(footer);
    clear(errorArea);
    if (lastError) {
      errorArea.appendChild(errorBox(lastError));
      if (needsRefresh) {
        errorArea.appendChild(h("div", { class: "gm-notice warn" },
          h("strong", null, "预览已过期"),
          h("p", null, "结果版本、选择或计划在预览后发生变化。冻结前必须刷新预览；刷新后所有原图审核都需要重做。"),
          h("div", { class: "gm-inline-actions" }, button("刷新预览", () => refreshPreview(), { kind: "primary", disabled: busy })),
        ));
      }
    }
    if (confirmed) {
      renderProgress();
      return;
    }
    if (!roots.length) {
      body.appendChild(h("div", { class: "gm-notice warn" },
        h("strong", null, "没有已登记的导出根目录"),
        h("p", null, "请先在“设置与存储”中登记一个导出根目录。导出不会写入插件数据目录或结果目录。"),
      ));
      footer.appendChild(button("关闭", () => close(), { kind: "muted" }));
      return;
    }
    renderSelectors();
    if (draft) renderDraft();
    else {
      footer.append(
        button("关闭", () => close(), { kind: "muted" }),
        button("预览清单", () => previewDraft(), { kind: "primary", disabled: busy }),
      );
    }
  }

  function renderSelectors() {
    const disabled = busy || Boolean(draft);
    const kindSelect = h("select", {
      class: "gm-input", disabled,
      "data-autofocus": draft ? undefined : "1",
      onchange: (event) => { kind = event.target.value; invalidateDraft(); render(); },
    },
      h("option", { value: "QUICK", selected: kind === "QUICK" }, "临时导出（当前入选健康结果）"),
      h("option", { value: "FINAL", selected: kind === "FINAL" }, "正式导出（逐原图审核）"),
    );
    const layoutSelect = h("select", {
      class: "gm-input", disabled,
      onchange: (event) => { layout = event.target.value; invalidateDraft(); render(); },
    },
      h("option", { value: "FLAT", selected: layout === "FLAT" }, "平铺（默认）"),
      h("option", { value: "GROUPED", selected: layout === "GROUPED" }, "按原图分组"),
    );
    const rootSelect = h("select", {
      class: "gm-input", disabled,
      onchange: (event) => { targetRoot = event.target.value; invalidateDraft(); render(); },
    }, ...roots.map((root) => h("option", { value: root, selected: root === targetRoot }, root)));
    body.append(
      h("div", { class: "gm-form-row three" },
        labeled("导出类型", kindSelect, "临时导出保持 QUICK；正式导出保持 FINAL。"),
        labeled("目录结构", layoutSelect, "每批次可自由选择平铺或按原图分组。"),
        labeled("目标根目录", rootSelect, "每批次在目标根下独占创建新目录。"),
      ),
      h("p", { class: "gm-help" }, "只复制冻结成员，不移动/重编码/硬链接源结果，不覆盖既有文件。导出失败不回滚已经成功的抽卡。"),
    );
  }

  function invalidateDraft() {
    draft = null;
    confirmed = null;
    needsRefresh = false;
    lastError = null;
    completionReported = false;
  }

  function sourceThumb(sourceId) {
    return h("img", {
      class: "gm-export-source-thumb",
      src: ctx.api.imageUrl(`/sources/${sourceId}/thumbnail`),
      alt: "",
      loading: "lazy",
      decoding: "async",
    });
  }

  function renderDraft() {
    const preview = draft.preview || {};
    const cutoff = draft.cutoff_summary || {};
    const previewSources = preview.sources || [];
    const reviews = new Map((draft.reviews || []).map((review) => [review.source_id, review]));
    const missingReviews = previewSources.filter((source) => !reviews.has(source.source_id));
    const needsCutoff = Number(cutoff.pending_draws || 0) + Number(cutoff.unknown_draws || 0) + Number(cutoff.inflight_draws || 0) > 0;
    const allSkippedZero = draft.kind === "FINAL" && previewSources.length > 0
      && previewSources.every((source) => reviews.get(source.source_id)?.decision === "SKIPPED");
    const cutoffCheck = h("input", { type: "checkbox", disabled: busy });
    const zeroCheck = h("input", { type: "checkbox", disabled: busy });

    body.append(
      h("div", { class: "gm-export-summary" },
        h("div", { class: "gm-stat" }, h("span", null, "类型"), h("strong", null, draft.kind === "FINAL" ? "正式" : "临时")),
        h("div", { class: "gm-stat" }, h("span", null, "布局"), h("strong", null, draft.layout)),
        h("div", { class: "gm-stat" }, h("span", null, "当前入选"), h("strong", null, String(preview.total_selected || 0))),
        h("div", { class: "gm-stat" }, h("span", null, "待执行 DRAW"), h("strong", null, String(cutoff.pending_draws || 0))),
        h("div", { class: "gm-stat" }, h("span", null, "UNKNOWN"), h("strong", null, String(cutoff.unknown_draws || 0))),
      ),
      h("p", { class: "gm-help" }, `草稿版本 ${draft.draft_version} · 截止事件 ${cutoff.last_event_seq ?? "—"} · 计划版本 ${cutoff.plan_version ?? "—"} · 目标 ${targetRoot}`),
    );

    if (draft.kind === "FINAL") {
      body.appendChild(h("h3", null, "逐原图审核"));
      if (!previewSources.length) {
        body.appendChild(h("p", { class: "gm-help" }, "该任务没有可审核的原图。"));
      } else {
        const table = h("div", { class: "gm-review-table" });
        for (const source of previewSources) {
          const review = reviews.get(source.source_id);
          const row = h("div", { class: "gm-export-review-row" },
            sourceThumb(source.source_id),
            h("div", { class: "gm-export-review-name" },
              h("strong", null, `SRC-${String(source.source_order).padStart(4, "0")}`),
              h("span", { class: "gm-ellipsis", title: source.original_name }, source.original_name),
            ),
            h("div", { class: "gm-export-review-counts" },
              h("span", null, `入选 ${source.selected_count}`),
              h("span", null, `可用 ${source.available}`),
              h("span", null, `待执行 ${source.pending}`),
              h("span", null, `失败 ${source.failed}`),
              source.unknown ? h("span", { class: "gm-inline-error" }, `UNKNOWN ${source.unknown}`) : null,
            ),
            h("div", { class: "gm-export-review-status" },
              review ? badge(REVIEW_LABEL[review.decision] || review.decision, review.decision === "SELECTED" ? "ok" : "muted") : badge("未审核", "warn"),
            ),
            review ? null : h("div", { class: "gm-export-review-actions" },
              button("确认入选", () => reviewSource(source, "SELECTED"), {
                kind: "primary", disabled: busy || needsRefresh || source.selected_count === 0,
              }),
              button("明确跳过", () => reviewSource(source, "SKIPPED"), { kind: "muted", disabled: busy || needsRefresh }),
            ),
          );
          table.appendChild(row);
        }
        body.appendChild(table);
      }
      body.appendChild(h("p", { class: "gm-help" },
        `已审核 ${draft.reviews?.length || 0} / ${previewSources.length} 张原图。仅“查看过”或“有一张入选”不能替代正式审核；没有健康入选的原图需要明确跳过。`));
    } else {
      body.appendChild(h("p", { class: "gm-help" }, "临时导出不需要逐原图审核；确认后会冻结当前入选集合。"));
    }

    body.append(frag(
      needsCutoff ? h("label", { class: "gm-check-inline gm-warning" },
        cutoffCheck, "我确认本次只导出当前截止版本；后续新结果需要再次审核导出。") : null,
      allSkippedZero ? h("label", { class: "gm-check-inline gm-warning" },
        zeroCheck, "本次正式导出包含 0 张图片，仅生成审核清单；我确认这不是图片交付。") : null,
    ));

    footer.append(
      button("刷新预览", () => refreshPreview(), { kind: "muted", disabled: busy }),
      button("关闭", () => close(), { kind: "muted" }),
      button(needsRefresh ? "需先刷新预览" : "冻结并导出", async () => {
        if (busy || needsRefresh) return;
        if (draft.kind === "FINAL" && missingReviews.length) {
          showErrors({ code: "EXPORT_REVIEW_REQUIRED", message: `还有 ${missingReviews.length} 张原图未审核或未跳过。` });
          render();
          return;
        }
        if (needsCutoff && !cutoffCheck.checked) {
          showErrors({ code: "EXPORT_REVIEW_REQUIRED", message: "请确认只导出当前截止版本。" });
          render();
          return;
        }
        if (allSkippedZero && !zeroCheck.checked) {
          showErrors({ code: "EXPORT_REVIEW_REQUIRED", message: "请二次确认零图片正式导出。" });
          render();
          return;
        }
        await freeze(cutoffCheck.checked, zeroCheck.checked, cutoff.last_event_seq);
      }, { kind: "primary", disabled: busy || needsRefresh }),
    );
  }

  async function previewDraft() {
    if (busy || closed) return;
    if (!targetRoot) {
      showErrors({ code: "BAD_REQUEST", message: "请选择导出目标根目录。" });
      render();
      return;
    }
    busy = true;
    lastError = null;
    clear(footer);
    footer.append(button("关闭", () => close(), { kind: "muted" }), spinner("正在生成预览清单…"));
    try {
      draft = await ctx.api.exportPreview(job.id, { kind, layout, target_root: targetRoot }, idempotencyKey());
      needsRefresh = false;
    } catch (error) {
      showErrors(error);
    } finally {
      busy = false;
      render();
    }
  }

  async function refreshPreview() {
    if (busy) return;
    draft = null;
    needsRefresh = false;
    lastError = null;
    await previewDraft();
  }

  async function reviewSource(source, decision) {
    if (busy || !draft || needsRefresh) return;
    busy = true;
    try {
      draft = await ctx.api.exportReview(draft.id, source.source_id, {
        decision,
        expected_result_revision: source.result_revision,
        expected_selection_version: source.selection_version,
      });
      lastError = null;
    } catch (error) {
      showErrors(error);
    } finally {
      busy = false;
      render();
    }
  }

  async function freeze(confirmCutoff, confirmZero, expectedCutoff) {
    if (busy) return;
    busy = true;
    lastError = null;
    try {
      confirmed = await ctx.api.exportConfirm(draft.id, {
        expected_draft_version: draft.draft_version,
        expected_cutoff_event_id: expectedCutoff,
        layout,
        target_root: targetRoot,
        confirm_cutoff: confirmCutoff,
        confirm_zero: confirmZero,
      }, idempotencyKey());
      startPolling();
    } catch (error) {
      showErrors(error);
    } finally {
      busy = false;
      render();
    }
  }

  function renderProgress() {
    if (!confirmed) return;
    const progress = confirmed.progress || {};
    const terminal = ["COMPLETED", "PARTIAL_FAILED"].includes(confirmed.status);
    body.append(frag(
      h("div", { class: "gm-export-summary" },
        h("div", { class: "gm-stat" }, h("span", null, "批次"), h("strong", { class: "gm-mono" }, confirmed.batch_name || confirmed.id.slice(0, 8))),
        h("div", { class: "gm-stat" }, h("span", null, "状态"), h("strong", null, confirmed.status)),
        h("div", { class: "gm-stat" }, h("span", null, "冻结"), h("strong", null, confirmed.frozen_at ? "是" : "否")),
      ),
      progressBar(progress.verified || 0, progress.total || 0, `${progress.verified || 0} / ${progress.total || 0} 已验证`),
      h("dl", { class: "gm-kv" },
        h("dt", null, "目标目录"), h("dd", { class: "gm-mono" }, confirmed.batch_dir || "—"),
        h("dt", null, "清单哈希"), h("dd", { class: "gm-mono" }, confirmed.manifest_hash || "—"),
        h("dt", null, "冻结时间"), h("dd", null, fmtTime(confirmed.frozen_at)),
        h("dt", null, "完成时间"), h("dd", null, fmtTime(confirmed.completed_at)),
        h("dt", null, "复制进度"), h("dd", null, `已验证 ${progress.verified || 0} · 待处理 ${progress.pending || 0} · 失败 ${progress.failed || 0}`),
      ),
      confirmed.error_message ? h("div", { class: "gm-inline-error" }, confirmed.error_message) : null,
      progress.failed ? h("div", { class: "gm-warning" },
        `${progress.failed} 个文件复制失败；这是可恢复的部分失败，不是“全部成功”。继续复制只处理未验证项，冻结成员不会改变。`) : null,
      terminal
        ? h("div", { class: `gm-notice ${confirmed.status === "COMPLETED" ? "ok" : "warn"}` },
          h("strong", null, (progress.total || 0) === 0
            ? "审核清单已生成"
            : confirmed.status === "COMPLETED" ? "复制完成" : "部分失败，可恢复"),
          h("p", null, (progress.total || 0) === 0
            ? "本次正式导出包含 0 张图片，仅生成审核清单；这不是图片交付。"
            : confirmed.status === "COMPLETED"
              ? "所有冻结文件已按哈希校验后无覆盖发布。"
              : "已完成的文件保留；可按原冻结清单继续处理未验证项。"),
        )
        : h("p", { class: "gm-help" }, "后台复制进行中。关闭面板只隐藏显示，不会取消后台复制。"),
    ));
    const failedItems = (confirmed.items || []).filter((item) => item.status === "FAILED");
    if (failedItems.length) {
      body.appendChild(h("details", { class: "gm-export-failures" },
        h("summary", null, `失败的复制项（${failedItems.length}）`),
        ...failedItems.map((item) => h("div", { class: "gm-export-failure-row" },
          h("span", { class: "gm-mono" }, item.target_rel_path || item.id),
          h("span", { class: "gm-inline-error" }, item.error_message || item.error_code || "复制失败"),
        )),
      ));
    }
    footer.append(
      button("关闭", () => close(), { kind: "muted" }),
      button("继续复制", () => resumeCopy(), {
        kind: "primary",
        disabled: busy || confirmed.status === "COMPLETED",
        title: confirmed.status === "COMPLETED" ? "复制已完成" : "按原冻结清单处理未验证项",
      }),
    );
    if (!terminal) {
      if (polling) {
        body.appendChild(h("p", { class: "gm-help", role: "status" }, "正在轮询真实 EXPORT 状态…"));
      } else {
        body.appendChild(h("div", { class: "gm-inline-actions" },
          button("开始轮询进度", () => startPolling(), { kind: "muted" }),
        ));
      }
    }
  }

  async function resumeCopy() {
    if (busy || !confirmed || closed) return;
    busy = true;
    try {
      confirmed = await ctx.api.exportResume(confirmed.id, idempotencyKey());
      lastError = null;
      startPolling();
    } catch (error) {
      showErrors(error);
    } finally {
      busy = false;
      render();
    }
  }

  function startPolling() {
    clearTimeout(pollTimer);
    if (closed || polling || !confirmed) return;
    if (["COMPLETED", "PARTIAL_FAILED"].includes(confirmed.status)) {
      reportCompletion();
      return;
    }
    polling = true;
    const tick = async () => {
      if (closed) { polling = false; return; }
      try {
        const detail = await ctx.api.exportDetail(confirmed.id);
        if (closed) { polling = false; return; }
        confirmed = detail;
        lastError = null;
        if (["COMPLETED", "PARTIAL_FAILED"].includes(detail.status)) {
          polling = false;
          render();
          reportCompletion();
          return;
        }
      } catch (error) {
        if (closed) { polling = false; return; }
        showErrors(error);
        render();
      }
      pollTimer = setTimeout(tick, 1200);
    };
    pollTimer = setTimeout(tick, 600);
  }

  function reportCompletion() {
    if (completionReported || !confirmed) return;
    completionReported = true;
    const total = confirmed.progress?.total || confirmed.item_count || 0;
    if (confirmed.status === "COMPLETED" && total === 0) {
      ctx.notify("正式导出审核清单已生成（0 张图片）；本次没有图片交付。", "info");
    } else if (confirmed.status === "COMPLETED") {
      ctx.notify(`导出批次 ${confirmed.batch_name || confirmed.id.slice(0, 8)} 已复制完成。`, "ok");
    } else if (confirmed.status === "PARTIAL_FAILED") {
      ctx.notify("导出部分失败；未验证项可按原冻结清单继续复制。", "warn", { sticky: true });
    }
    onDone?.();
  }

  function close() {
    if (closed) return;
    closed = true;
    polling = false;
    clearTimeout(pollTimer);
    dialog.close("close");
  }

  render();
  return { close, reload: render };
}
