import { idempotencyKey } from "../api.js";
import { badge, button, clear, confirmDialog, errorBox, h, loadingState, modal } from "../dom.js";

export function openRecovery(ctx, job, { onResolved, onConfirmResume } = {}) {
  let closed = false;
  let busy = false;
  const dialog = modal({
    title: "重启核验与 UNKNOWN 处理",
    wide: true,
    onClose: () => { closed = true; },
  });
  const body = h("div");
  const footer = h("div", { class: "gm-inline-actions" });
  dialog.body.append(
    h("p", { class: "gm-help" },
      "后端重启会清除自动运行授权。核验只读取本地收据、结果文件和远端 queue/history，不会自动重新提交。每个 UNKNOWN 必须单独决定；关闭面板不会恢复运行。"),
    body,
  );
  dialog.footer.appendChild(footer);

  async function load() {
    if (closed) return;
    clear(body);
    clear(footer);
    body.appendChild(loadingState("正在重新读取核验报告…"));
    try {
      const report = await ctx.api.recovery(job.id);
      if (closed || !dialog.isOpen()) return;
      clear(body);
      const attempts = report.unknown_attempts || [];
      if (!attempts.length) {
        body.appendChild(h("div", { class: "gm-notice" },
          h("strong", null, "没有 UNKNOWN 尝试"),
          h("p", null, "重启授权已清除。确认输入、工作流和能力后，可以显式继续任务；不会自动开始。"),
        ));
        footer.append(
          button("关闭", () => dialog.close("close"), { kind: "muted" }),
          button("已核验，确认继续", async () => {
            if (busy) return;
            busy = true;
            dialog.close("resume");
            onConfirmResume?.();
          }, { kind: "primary" }),
        );
        return;
      }
      body.appendChild(h("div", { class: "gm-inline-actions" },
        badge(`recovery_version ${report.recovery_version}`, "warn"),
        badge(`待处理 ${attempts.length}`, "bad"),
        button("重新核验全部", load, { kind: "muted" }),
      ));
      for (const attempt of attempts) body.appendChild(attemptCard(attempt));
      footer.append(button("关闭", () => dialog.close("close"), { kind: "muted" }));
    } catch (error) {
      if (closed) return;
      clear(body);
      body.appendChild(errorBox(error));
      footer.append(button("关闭", () => dialog.close("close"), { kind: "muted" }));
    }
  }

  function attemptCard(attempt) {
    const card = h("article", { class: "gm-recovery-card" });
    const status = h("div");
    const reason = h("input", { class: "gm-input", placeholder: "处理理由（会写入事件记录）" });
    const confirm = h("input", { type: "checkbox" });
    const files = attempt.files || [];
    const localBusy = h("span", { class: "gm-help" }, "");
    const buttons = [];

    async function reconcile() {
      if (busy) return;
      busy = true; buttons.forEach((btn) => { btn.disabled = true; });
      localBusy.textContent = "正在重新核验（不会提交）…";
      try {
        await ctx.api.reconcile(attempt.attempt_id);
        if (!closed) await load();
        ctx.notify("已重新核验；未自动提交或恢复运行。", "info");
      } catch (error) {
        if (!closed) status.replaceChildren(errorBox(error));
      } finally {
        busy = false;
      }
    }

    async function resolve(decision, confirmInactive = false) {
      if (busy) return;
      if (!(await confirmDialog(`确认对 DRAW-${String(attempt.draw_no || 0).padStart(6, "0")} 执行“${decision}”？不会自动重新提交。`, {
        danger: decision !== "keep_unknown",
      }))) return;
      busy = true; buttons.forEach((btn) => { btn.disabled = true; });
      localBusy.textContent = "正在保存人工决定…";
      try {
        await ctx.api.resolve(attempt.attempt_id, {
          decision,
          reason: reason.value || undefined,
          confirm_inactive: confirmInactive,
        }, idempotencyKey());
        ctx.notify("人工核验决定已保存。", "ok");
        if (!closed) await load();
        onResolved?.();
      } catch (error) {
        if (!closed) {
          ctx.notify(`处理失败：${error.message}`, "error");
          await load();
        }
      } finally {
        busy = false;
      }
    }

    const reconcileBtn = button("重新核验", reconcile, { kind: "muted" });
    const keepBtn = button("维持 UNKNOWN", () => resolve("keep_unknown"), { kind: "muted" });
    const retryBtn = button("放弃并重试", async () => {
      if (!confirm.checked) {
        status.replaceChildren(errorBox({ code: "STATE_CONFLICT", message: "请先确认旧执行不再活动。" }));
        return;
      }
      await resolve("retry", true);
    }, { kind: "primary" });
    const failBtn = button("标记失败", () => resolve("mark_failed"), { kind: "warn" });
    buttons.push(reconcileBtn, keepBtn, retryBtn, failBtn);

    card.append(
      h("div", { class: "gm-recovery-head" },
        h("div", null,
          h("h4", null, `DRAW-${String(attempt.draw_no || 0).padStart(6, "0")}`),
          h("span", { class: "gm-mono" }, attempt.attempt_id),
        ),
        badge(attempt.remote_state || "unknown", "bad"),
      ),
      h("dl", { class: "gm-kv" },
        h("dt", null, "预期 prompt"), h("dd", { class: "gm-mono" }, attempt.expected_prompt_id || "—"),
        h("dt", null, "实际 prompt"), h("dd", { class: "gm-mono" }, attempt.actual_prompt_id || "—"),
        h("dt", null, "尝试目录"), h("dd", { class: "gm-mono", title: attempt.attempt_dir }, attempt.attempt_dir || "—"),
        h("dt", null, "本地文件"), h("dd", null, files.length ? `${files.length} 个文件（仅作为证据，不自动认定成功）` : "无完整文件"),
        h("dt", null, "种子"), h("dd", null, (attempt.seeds || []).map((seed) => `#${seed.node_id}.${seed.input_name}=${seed.seed}`).join("；") || "—"),
      ),
      h("p", { class: "gm-help" }, attempt.detail || ""),
      h("div", { class: "gm-form-row" }, reason),
      h("label", { class: "gm-check-inline" }, confirm, "我已确认旧执行不再活动（仅用于放弃旧尝试并重试）"),
      h("div", { class: "gm-inline-actions" }, reconcileBtn, keepBtn, retryBtn, failBtn, localBusy),
      status,
    );
    return card;
  }

  load();
  return { close: () => dialog.close("program"), reload: load };
}
