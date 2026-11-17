// UI06: freeze an export, logically remove an unexecuted source, append draws
// with stable numbering, and return to the original source position.
const fs = require("node:fs/promises");
const {
  arg, launch, openGacha, openJob, openFirstResults, resultState,
  waitResultIdle, saveJson, screenshot,
} = require("./host_ui.cjs");

async function fetchJson(page, path) {
  return page.evaluate(async (url) => {
    const response = await fetch(url);
    const payload = await response.json();
    return payload.data ?? payload;
  }, path);
}

(async () => {
  const baseUrl = arg("base-url", "http://127.0.0.1:8296");
  const job = arg("job");
  const jobId = arg("job-id");
  const outPath = arg("out", "UI06.json");
  const shots = arg("screenshots", null);
  if (!job || !jobId) throw new Error("--job and --job-id are required");
  if (shots) await fs.mkdir(shots, { recursive: true });

  const browser = await launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error" && message.text().includes("GachaMaster")) {
      errors.push(`console: ${message.text()}`);
    }
  });

  try {
    await openGacha(page, baseUrl);
    await openJob(page, job);
    await openFirstResults(page);
    const pageOne = await resultState(page);
    const selected = [];
    for (const card of pageOne.cards.slice(0, 2)) {
      if (card.star !== "★") {
        await page.locator(`[data-output-id="${card.id}"] .gm-star`).click();
      }
      await page.waitForFunction((id) => document.querySelector(`[data-output-id="${id}"] .gm-star`)?.textContent.trim() === "★", card.id, { timeout: 20000 });
      selected.push(card.id);
    }
    if (shots) await screenshot(page, `${shots}/UI06-selected.png`);

    // QUICK freeze.
    await page.locator(".gm-control-bar button").filter({ hasText: "导出" }).first().click();
    await page.waitForSelector(".gm-modal", { timeout: 30000 });
    const selects = page.locator(".gm-modal select");
    await selects.nth(0).selectOption("QUICK");
    await page.getByRole("button", { name: "预览清单" }).click();
    await page.waitForTimeout(1200);
    const cutoffCheck = page.locator('.gm-modal input[type="checkbox"]').first();
    if (await cutoffCheck.count()) await cutoffCheck.check({ force: true });
    await page.getByRole("button", { name: "冻结并导出" }).click();
    let exportInfo = null;
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      const exports = await fetchJson(page, `/gacha_master/v1/jobs/${jobId}/exports`);
      const list = Array.isArray(exports) ? exports : (exports.items || []);
      exportInfo = list[0] || null;
      if (exportInfo && ["COMPLETED", "PARTIAL_FAILED", "RECOVERY_REQUIRED"].includes(exportInfo.status)) break;
      await page.waitForTimeout(800);
    }
    if (shots) await screenshot(page, `${shots}/UI06-export-frozen.png`);
    await page.locator('.gm-modal-close[aria-label="关闭对话框"]').first().click().catch(() => {});
    await page.waitForTimeout(500);
    if (!exportInfo) throw new Error("export did not complete");
    const exportBefore = await fetchJson(page, `/gacha_master/v1/exports/${exportInfo.id}`);

    // Remove the last source, which is still unexecuted.
    const lastRow = page.locator(".gm-source-row").filter({ hasText: "SRC-0004" }).first();
    await lastRow.waitFor({ state: "visible", timeout: 30000 });
    await lastRow.getByRole("button", { name: "移除" }).click();
    await page.waitForSelector(".gm-modal", { timeout: 30000 });
    await page.locator(".gm-modal").getByRole("button", { name: "移除", exact: true }).click();
    await page.waitForTimeout(1500);
    const afterRemove = await page.evaluate(() => ({
      counts: document.querySelector(".gm-sources-counts")?.innerText || null,
      rows: [...document.querySelectorAll(".gm-source-row")].map((row) => ({
        order: Number(row.dataset.sourceOrder), text: row.innerText.slice(0, 120),
      })),
    }));
    if (shots) await screenshot(page, `${shots}/UI06-source-removed.png`);

    // Append two draws to SRC-0001; numbering must continue after the old max.
    const firstRow = page.locator(".gm-source-row").filter({ hasText: "SRC-0001" }).first();
    await firstRow.getByRole("button", { name: "追加抽卡" }).click();
    await page.waitForSelector(".gm-modal", { timeout: 30000 });
    const countInput = page.locator('.gm-modal input[type="number"]').first();
    await countInput.fill("2");
    await page.waitForTimeout(300);
    const appendPreview = await page.locator(".gm-append-preview").innerText();
    await page.getByRole("button", { name: "确认追加" }).click();
    await page.waitForTimeout(2000);
    const jobAfter = await fetchJson(page, `/gacha_master/v1/jobs/${jobId}`);
    const sourcesAfter = await fetchJson(page, `/gacha_master/v1/jobs/${jobId}/sources?after_order=0&limit=40`);
    const sourceOne = (sourcesAfter.items || []).find((item) => item.source_order === 1);
    const exportAfter = await fetchJson(page, `/gacha_master/v1/exports/${exportInfo.id}`);

    // Return to the first source position.
    const home = page.locator(".gm-source-pager button").filter({ hasText: "首页" }).first();
    if (await home.count() && !(await home.isDisabled())) await home.click();
    await page.waitForTimeout(800);
    const finalPosition = await page.evaluate(() =>
      document.querySelector(".gm-source-pager .gm-pager-position")?.textContent || null);

    const checks = {
      export_completed: exportInfo.status === "COMPLETED",
      export_members_selected: exportBefore.items?.length === selected.length || exportBefore.item_count === selected.length,
      removal_active_count: /有效原图\s*3\s*\/\s*4/.test(afterRemove.counts || ""),
      removal_keeps_numbering: afterRemove.rows.length === 3
        && afterRemove.rows[0].order === 1 && afterRemove.rows[2].order === 3,
      append_preview_continues: /3\s*…\s*4/.test(appendPreview),
      append_plan_increased: Number(jobAfter.summary?.total_planned) === 10 && Number(jobAfter.summary?.appended) === 2,
      source_plan_increased: Number(sourceOne?.summary?.planned) === 4,
      export_identity_unchanged: Boolean(exportAfter.manifest_hash)
        && exportAfter.manifest_hash === exportBefore.manifest_hash
        && exportAfter.item_count === exportBefore.item_count,
      returned_to_first_page: Boolean(finalPosition && finalPosition.includes("第 1 页")),
    };
    const result = {
      schema_version: 1,
      case: "UI06",
      provenance: { data_source: "real_gpu", environment: "isolated", host: "real" },
      job, job_id: jobId,
      selected,
      export: { id: exportInfo.id, status: exportInfo.status, before: exportBefore, after: exportAfter },
      removal: afterRemove,
      append_preview: appendPreview,
      job_after: jobAfter.summary,
      source_one_after: sourceOne?.summary || null,
      final_position: finalPosition,
      errors,
      checks,
      result: Object.values(checks).every(Boolean) ? "PASS" : "FAIL",
    };
    await saveJson(outPath, result);
    console.log(JSON.stringify(result, null, 2));
    await browser.close();
    process.exit(result.result === "PASS" ? 0 : 1);
  } catch (error) {
    const result = {
      schema_version: 1, case: "UI06", job, job_id: jobId,
      provenance: { data_source: "real_gpu", environment: "isolated", host: "real" },
      result: "FAIL", errors: [...errors, `${error.stack || error}`],
    };
    await saveJson(outPath, result);
    console.error(JSON.stringify(result, null, 2));
    await browser.close();
    process.exit(1);
  }
})();
