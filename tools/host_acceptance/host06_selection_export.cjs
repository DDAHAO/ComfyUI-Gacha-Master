// H06-B: select OUTs on the first and third result pages, verify persistence
// across navigation/reload/compare, then freeze a FINAL export.
const fs = require("node:fs/promises");
const {
  arg, launch, openGacha, goToJobList, openJob, openFirstResults,
  resultState, clickResultButton, saveJson, screenshot,
} = require("./host_ui.cjs");

async function waitStar(page, id, expected, timeout = 30000) {
  await page.waitForFunction(({ outputId, value }) => {
    const star = document.querySelector(`[data-output-id="${outputId}"] .gm-star`);
    return star && star.textContent.trim() === value;
  }, { outputId: id, value: expected }, { timeout });
}

async function starText(page, id) {
  const star = page.locator(`[data-output-id="${id}"] .gm-star`);
  if (!(await star.count())) return null;
  return (await star.textContent()).trim();
}

(async () => {
  const baseUrl = arg("base-url", "http://127.0.0.1:8296");
  const job = arg("job");
  const jobId = arg("job-id");
  const outPath = arg("out", "H06-B.json");
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

    const firstPage = await resultState(page);
    const firstId = firstPage.cards[0].id;
    if ((await starText(page, firstId)) !== "★") {
      await page.locator(`[data-output-id="${firstId}"] .gm-star`).click();
    }
    await waitStar(page, firstId, "★");
    if (shots) await screenshot(page, `${shots}/H06-B-page1-selected.png`);

    await clickResultButton(page, "下一页");
    await clickResultButton(page, "下一页");
    const thirdPage = await resultState(page);
    const thirdId = thirdPage.cards[0].id;
    if ((await starText(page, thirdId)) !== "★") {
      await page.locator(`[data-output-id="${thirdId}"] .gm-star`).click();
    }
    await waitStar(page, thirdId, "★");
    if (shots) await screenshot(page, `${shots}/H06-B-page3-selected.png`);

    // Leave the detail and return.
    await goToJobList(page);
    await openJob(page, job);
    await openFirstResults(page);
    const afterReturnFirst = await starText(page, firstId);
    await clickResultButton(page, "下一页");
    await clickResultButton(page, "下一页");
    const afterReturnThird = await starText(page, thirdId);

    // Full browser reload.
    await page.reload({ waitUntil: "domcontentloaded" });
    await openGacha(page, baseUrl);
    await openJob(page, job);
    await openFirstResults(page);
    const afterReloadFirst = await starText(page, firstId);
    await clickResultButton(page, "下一页");
    await clickResultButton(page, "下一页");
    const afterReloadThird = await starText(page, thirdId);

    // Compare toggle and back.
    await clickResultButton(page, "首页");
    await page.locator(`[data-output-id="${firstId}"] .gm-thumb-open`).click();
    await page.waitForSelector(".gm-compare-grid", { timeout: 30000 });
    const slider = page.locator(".gm-compare-mode-btn").nth(1);
    if (await slider.count()) {
      await slider.click();
      await page.waitForTimeout(800);
    }
    const modeState = await page.evaluate(() => ({
      active: document.querySelector(".gm-compare-mode-btn.active")?.textContent || null,
      sliderHidden: document.querySelector(".gm-compare-slider")?.hidden ?? null,
      sliderDisplay: document.querySelector(".gm-compare-slider")
        ? getComputedStyle(document.querySelector(".gm-compare-slider")).display : null,
      notice: document.querySelector(".gm-compare-slider-notice")?.innerText || null,
    }));
    const sliderVisible = Boolean(
      modeState.active?.includes("滑动") && modeState.sliderHidden === false
    );
    const starDuringCompare = await page.evaluate((id) =>
      document.querySelector(`[data-output-id="${id}"] .gm-star`)?.textContent.trim(), firstId);
    await page.locator('.gm-modal-close[aria-label="关闭对话框"]').first().click();
    await page.waitForTimeout(400);
    const afterCompareFirst = await starText(page, firstId);

    // FINAL export with per-source review.
    await page.locator(".gm-control-bar button").filter({ hasText: "导出" }).first().click();
    await page.waitForSelector(".gm-modal", { timeout: 30000 });
    const selects = page.locator(".gm-modal select");
    await selects.nth(0).selectOption("FINAL");
    await selects.nth(1).selectOption("GROUPED");
    await page.getByRole("button", { name: "预览清单" }).click();
    await page.waitForSelector(".gm-export-review-row", { timeout: 60000 });
    if (shots) await screenshot(page, `${shots}/H06-B-export-review.png`);
    const reviewButton = page.getByRole("button", { name: "确认入选" }).first();
    if (await reviewButton.count()) {
      await reviewButton.click();
      await page.waitForTimeout(800);
    }
    const freeze = page.getByRole("button", { name: "冻结并导出" });
    if (!(await freeze.count()) || await freeze.isDisabled()) {
      const refresh = page.getByRole("button", { name: "刷新预览" });
      if (await refresh.count()) {
        await refresh.click();
        await page.waitForTimeout(1000);
      }
    }
    await page.getByRole("button", { name: "冻结并导出" }).click();

    // Poll the authoritative export state; the modal wording contains
    // unrelated "失败 0" counters and is not a status signal.
    let exports = [];
    let latest = null;
    const deadline = Date.now() + 180000;
    while (Date.now() < deadline) {
      const response = await fetch(`${baseUrl}/gacha_master/v1/jobs/${jobId}/exports`);
      const payload = await response.json();
      exports = payload.data?.items || payload.data || payload;
      latest = Array.isArray(exports) ? exports[0] : null;
      if (latest && ["COMPLETED", "PARTIAL_FAILED", "RECOVERY_REQUIRED", "FROZEN"].includes(latest.status)) break;
      await page.waitForTimeout(1000);
    }
    if (shots) await screenshot(page, `${shots}/H06-B-export-completed.png`);
    const modalText = await page.locator(".gm-modal").innerText().catch(() => "");
    await page.locator('.gm-modal-close[aria-label="关闭对话框"]').first().click();
    await page.waitForTimeout(500);
    const checks = {
      selected_two_distinct: firstId !== thirdId,
      first_selected: awaitStar(afterReturnFirst),
      third_selected: awaitStar(afterReturnThird),
      survive_return: afterReturnFirst === "★" && afterReturnThird === "★",
      survive_reload: afterReloadFirst === "★" && afterReloadThird === "★",
      compare_keeps_selection: starDuringCompare === "★" && afterCompareFirst === "★",
      compare_toggle_ok: sliderVisible || Boolean(modeState.notice && modeState.notice.includes("比例不同")),
      export_completed: Boolean(latest && latest.status === "COMPLETED"),
      export_item_count: Boolean(latest && latest.item_count === 2),
      export_hash_matches: Boolean(latest && latest.manifest_hash),
    };
    const result = {
      schema_version: 1,
      case: "H06-B",
      provenance: { data_source: "real_gpu", environment: "isolated", host: "real" },
      job,
      job_id: jobId,
      selected: { first_page: firstId, third_page: thirdId },
      stars: {
        after_return: { first: afterReturnFirst, third: afterReturnThird },
        after_reload: { first: afterReloadFirst, third: afterReloadThird },
        during_compare: starDuringCompare,
        after_compare: afterCompareFirst,
      },
      slider_visible: sliderVisible,
      slider_mode_state: modeState,
      export_modal_text: modalText.slice(0, 3000),
      exports,
      latest_export: latest,
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
      schema_version: 1, case: "H06-B", job, job_id: jobId,
      provenance: { data_source: "real_gpu", environment: "isolated", host: "real" },
      result: "FAIL", errors: [...errors, `${error.stack || error}`],
    };
    await saveJson(outPath, result);
    console.error(JSON.stringify(result, null, 2));
    await browser.close();
    process.exit(1);
  }
})();

function awaitStar(value) {
  return value === "★";
}
