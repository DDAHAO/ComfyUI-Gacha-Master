import {
  badge, button, clear, confirmDialog, errorBox, fmtBytes, fmtTime, h, labeled, loadingState, frag,
} from "../dom.js";

export function mountSettings(container, ctx) {
  let destroyed = false;
  let settings = null;
  let storage = null;
  let backups = [];
  let capabilities = null;
  const root = h("div");
  container.appendChild(root);

  async function load() {
    clear(root);
    root.appendChild(loadingState("正在读取设置与存储信息…"));
    try {
      [settings, storage, backups, capabilities] = await Promise.all([
        ctx.api.settings(),
        ctx.api.storage(),
        ctx.api.backups(),
        ctx.api.capabilities(),
      ]);
      if (destroyed) return;
      ctx.store.set({ settings, capabilities });
      render();
    } catch (error) {
      if (destroyed) return;
      clear(root);
      root.appendChild(errorBox(error));
    }
  }

  function render() {
    clear(root);
    const inputRoots = h("textarea", { class: "gm-input", rows: 4 }, (settings.allowed_input_roots || []).join("\n"));
    const exportRoots = h("textarea", { class: "gm-input", rows: 3 }, (settings.allowed_export_roots || []).join("\n"));
    const numeric = {
      upload_max_mb: (settings.upload_max_file || 0) / 1024 / 1024,
      decode_max_pixels: settings.decode_max_pixels,
      thumbnail_edge: settings.thumbnail_edge,
      page_size: settings.page_size,
      submit_timeout: settings.submit_timeout,
      backup_draws: settings.backup_draws,
      backup_interval: settings.backup_interval,
      backup_retention: settings.backup_retention,
      disk_reserve_gib: (settings.disk_reserve || 0) / 1024 / 1024 / 1024,
      export_workers: settings.export_workers,
    };
    const inputs = {};
    const numericFields = h("div", { class: "gm-settings-grid" });
    for (const [key, label] of Object.entries({
      upload_max_mb: "上传大小上限 (MiB)",
      decode_max_pixels: "最大像素数",
      thumbnail_edge: "缩略图最大边 (px)",
      page_size: "默认分页大小",
      submit_timeout: "提交超时 (秒)",
      backup_draws: "备份间隔 DRAW 数",
      backup_interval: "备份间隔 (秒)",
      backup_retention: "滚动备份保留数",
      disk_reserve_gib: "每卷保留空间 (GiB)",
      export_workers: "导出并发工作数",
    })) {
      const input = h("input", { class: "gm-input", type: "number", value: numeric[key], step: key === "submit_timeout" ? "0.5" : "1" });
      inputs[key] = input;
      numericFields.appendChild(labeled(label, input));
    }
    const targetedCancel = h("input", { type: "checkbox", checked: Boolean(settings.targeted_cancel_verified) });
    const saveStatus = h("div");
    const saveButton = button("保存设置", async () => {
      try {
        const body = {
          allowed_input_roots: inputRoots.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
          allowed_export_roots: exportRoots.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
          upload_max_file: Math.round(Number(inputs.upload_max_mb.value) * 1024 * 1024),
          decode_max_pixels: Math.round(Number(inputs.decode_max_pixels.value)),
          thumbnail_edge: Math.round(Number(inputs.thumbnail_edge.value)),
          page_size: Math.round(Number(inputs.page_size.value)),
          submit_timeout: Number(inputs.submit_timeout.value),
          backup_draws: Math.round(Number(inputs.backup_draws.value)),
          backup_interval: Math.round(Number(inputs.backup_interval.value)),
          backup_retention: Math.round(Number(inputs.backup_retention.value)),
          disk_reserve: Math.round(Number(inputs.disk_reserve_gib.value) * 1024 * 1024 * 1024),
          export_workers: Math.round(Number(inputs.export_workers.value)),
          targeted_cancel_verified: targetedCancel.checked,
        };
        settings = await ctx.api.updateSettings(body);
        clear(saveStatus);
        saveStatus.appendChild(h("span", { class: "gm-notice ok" }, "设置已保存。"));
        ctx.notify("设置已保存。", "ok");
        await load();
      } catch (error) {
        clear(saveStatus);
        saveStatus.appendChild(errorBox(error));
      }
    }, { kind: "primary" });

    root.append(frag(
      h("div", { class: "gm-section-head" },
        h("div", null, h("h2", null, "设置与存储"),
          h("p", { class: "gm-help" }, "路径在运行中不可直接迁移；结果根变更需停机、备份、核验后处理。")),
        button("刷新", () => load(), { kind: "muted" }),
      ),
      h("section", { class: "gm-card" },
        h("h3", null, "数据位置"),
        h("dl", { class: "gm-kv" },
          h("dt", null, "数据根"), h("dd", { class: "gm-mono" }, settings.data_root),
          h("dt", null, "SQLite"), h("dd", { class: "gm-mono" }, settings.db_path),
          h("dt", null, "结果根"), h("dd", { class: "gm-mono" }, settings.results_root),
          h("dt", null, "备份"), h("dd", { class: "gm-mono" }, settings.backups_root),
          h("dt", null, "日志"), h("dd", { class: "gm-mono" }, settings.logs_root),
          h("dt", null, "收件箱"), h("dd", { class: "gm-mono" }, settings.inbox_root),
        ),
        h("p", { class: "gm-help" }, "卸载插件默认保留数据、结果和备份；不会自动删除原图或已发布结果。"),
      ),
      h("section", { class: "gm-card" },
        h("h3", null, "允许的目录"),
        h("p", { class: "gm-help" }, "只有登记过的输入根可被文件夹模式读取；导出只能写入登记过的导出根。每行一个绝对路径。"),
        h("div", { class: "gm-form-row two" },
          labeled("输入根目录", inputRoots),
          labeled("导出根目录", exportRoots),
        ),
        numericFields,
        h("label", { class: "gm-check-inline" }, targetedCancel,
          "已用真实 ComfyUI 验证定向取消（仅人工验证后勾选；默认降级）"),
        h("div", { class: "gm-inline-actions" }, saveButton),
        saveStatus,
      ),
      h("section", { class: "gm-card" },
        h("h3", null, "存储占用"),
        storage ? h("div", { class: "gm-summary-grid" },
          h("div", { class: "gm-stat-card" }, h("span", null, "数据（含 DB/快照/日志）"), h("strong", null, fmtBytes(storage.data_bytes))),
          h("div", { class: "gm-stat-card" }, h("span", null, "结果图片（不含缓存）"), h("strong", null, fmtBytes(storage.result_image_bytes))),
          h("div", { class: "gm-stat-card" }, h("span", null, "缩略图缓存"), h("strong", null, fmtBytes(storage.thumbnails_bytes))),
          h("div", { class: "gm-stat-card" }, h("span", null, "结果目录其他内容"), h("strong", null, fmtBytes(storage.results_other_bytes))),
          h("div", { class: "gm-stat-card" }, h("span", null, "导出成品"), h("strong", null, storage.exports_stats === "known" ? fmtBytes(storage.exports_bytes) : "未完全统计")),
          h("div", { class: "gm-stat-card" }, h("span", null, "数据库备份"), h("strong", null, fmtBytes(storage.backups_bytes))),
          h("div", { class: "gm-stat-card" }, h("span", null, "数据卷可用"), h("strong", null, fmtBytes(storage.disk?.data_free))),
          h("div", { class: "gm-stat-card" }, h("span", null, "结果卷可用"), h("strong", null, fmtBytes(storage.disk?.results_free))),
        ) : null,
        storage ? h("p", { class: "gm-help" },
          `结果目录合计 ${fmtBytes(storage.results_bytes)}，其中已包含缩略图缓存 ${fmtBytes(storage.thumbnails_bytes)}；`
          + "分项互斥，不能把“结果目录”和“缩略图缓存”再相加。"
          + `已永久清理结果 ${storage.retention?.purged_outputs ?? 0} 张，逻辑字节 ${fmtBytes(storage.retention?.purged_bytes || 0)}。`) : null,
        h("div", { class: "gm-inline-actions" },
          button("清理可重建缩略图缓存", async () => {
            if (!(await confirmDialog(
              "只删除缩略图 JPG 缓存，不改动原图、完整结果、数据库或备份。再次浏览会重新生成缩略图。继续？",
              { confirmLabel: "清理缓存" },
            ))) return;
            try {
              const result = await ctx.api.clearCache();
              const parts = [`已删除 ${result.removed} 个缓存文件，释放 ${fmtBytes(result.bytes || 0)}`];
              if (result.skipped) parts.push(`跳过 ${result.skipped} 个非缓存/并发变化文件`);
              if (result.failed) parts.push(`${result.failed} 个失败`);
              ctx.notify(
                parts.join("；") + "。再次浏览会重新生成，当前缓存占用可能增加。",
                result.failed ? "warn" : "ok",
              );
              if (result.errors?.length) {
                ctx.notify(`缓存清理错误示例：${result.errors[0].error}`, "error");
              }
              await load();
            } catch (error) { ctx.notify(error.message, "error"); }
          }, { kind: "muted" }),
        ),
        h("p", { class: "gm-help" },
          "任务级完整结果清理在“任务详情 → 存储清理”中操作，需要先预览范围并明确确认；"
          + "缓存清理与结果删除分开执行，完整结果不会在这里被删除。"),
      ),
      h("section", { class: "gm-card" },
        h("h3", null, "数据库备份"),
        h("p", { class: "gm-help" }, "使用 SQLite 一致性备份；只有校验通过的备份计入轮转。恢复会先保留当前数据库，并要求没有活动执行。"),
        h("div", { class: "gm-inline-actions" },
          button("立即备份", async () => {
            try {
              await ctx.api.createBackup();
              ctx.notify("一致性备份已完成并校验。", "ok");
              await load();
            } catch (error) { ctx.notify(`备份失败：${error.message}`, "error"); }
          }, { kind: "primary" }),
        ),
        h("div", { class: "gm-backup-list" },
          ...(backups.length ? backups.map((backup) => h("div", { class: "gm-backup-row" },
            h("span", { class: "gm-mono" }, backup.id.slice(0, 8)),
            badge(backup.status, backup.status === "VERIFIED" ? "ok" : "bad"),
            h("span", { class: "gm-ellipsis", title: backup.path }, backup.path),
            h("span", { class: "gm-help" }, `${fmtBytes(backup.size)} · ${fmtTime(backup.created_at)}`),
            button("恢复", async () => {
              if (!(await confirmDialog("恢复备份会替换当前数据库，但会先保留当前库和 WAL 证据。恢复后必须重新核验文件与任务。继续？", { danger: true, confirmLabel: "恢复备份" }))) return;
              try {
                const result = await ctx.api.restoreBackup(backup.id);
                ctx.notify(`已恢复备份；原库保留在 ${result.pre_restore_path}`, "ok");
                await load();
              } catch (error) { ctx.notify(`恢复失败：${error.message}`, "error"); }
            }, { kind: "danger" }),
          )) : [h("p", { class: "gm-help" }, "还没有备份。")]),
        ),
      ),
      h("section", { class: "gm-card" },
        h("h3", null, "能力与限制"),
        capabilities ? h("dl", { class: "gm-kv" },
          h("dt", null, "ComfyUI"), h("dd", null, capabilities.comfy_version || "未知"),
          h("dt", null, "前端"), h("dd", null, capabilities.frontend_version || "未知"),
          h("dt", null, "客户端 prompt_id"), h("dd", null, capabilities.client_supplied_prompt_id ? "已验证" : "未验证"),
          h("dt", null, "定向取消"), h("dd", null, capabilities.targeted_cancel || "unverified"),
          h("dt", null, "队列/历史查询"), h("dd", null, `${capabilities.queue_lookup ? "可用" : "不可用"} / ${capabilities.history_lookup ? "可用" : "不可用"}`),
          h("dt", null, "无覆盖发布"), h("dd", null, capabilities.filesystem_no_replace_publish ? "自检通过" : "自检失败"),
        ) : null,
        (capabilities?.warnings || []).length ? h("ul", { class: "gm-warning-list" },
          ...capabilities.warnings.map((warning) => h("li", null, warning))) : null,
        h("p", { class: "gm-help" }, "立即暂停在未验证定向取消的版本上会安全降级为等待当前执行结束；不会使用全局 /interrupt 误伤外部任务。"),
      ),
    ));
  }

  load();
  return {
    destroy() { destroyed = true; },
    refresh() { load(); },
    onEvents() {},
    onGap() { load(); },
  };
}
