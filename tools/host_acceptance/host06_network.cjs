// H06-D: failed page request, retry, late response after leaving/returning,
// and compare toggling while a page request is in flight.
const fs = require("node:fs/promises");
const {
  arg, launch, openGacha, goToJobList, openJob, openFirstResults,
  resultState, clickResultButton, waitResultIdle, saveJson, screenshot,
} = require("./host_ui.cjs");

function outMatch(cards) {
  return cards.map((card) => card.id).join("|");
}

(async () => {
  const baseUrl = arg("base-url", "http://127.0.0.1:8296");
  const job = arg("job");
  const outPath = arg("out", "H06-D.json");
  const shots = arg("screenshots", null);
  if (!job) throw new Error("--job is required");
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

  async function pageOne() {
    const current = await resultState(page);
    if (current.label && current.label.includes("第 1 页")) return current;
    await clickResultButton(page, "首页");
    return resultState(page);
  }

  try {
    await openGacha(page, baseUrl);
    await openJob(page, job);
    await openFirstResults(page);
    const initial = await resultState(page);
    const initialIds = outMatch(initial.cards);

    // 1. Fail the next page request once; the current page must remain.
    let failOnce = true;
    await page.route("**/sources/*/outputs*", async (route) => {
      if (failOnce) {
        failOnce = false;
        await route.abort("failed");
      } else {
        await route.continue();
      }
    });
    await page.locator(".gm-result-toolbar button").filter({ hasText: "下一页" }).first().click();
    await page.waitForFunction(() => {
      const bar = document.querySelector(".gm-result-toolbar");
      return bar && [...bar.querySelectorAll("button")].some(
        (button) => button.textContent.includes("重试本页") && !button.hidden
      );
    }, { timeout: 30000 });
    const afterFailure = await resultState(page);
    if (shots) await screenshot(page, `${shots}/H06-D-failed-page.png`);
    await page.locator(".gm-result-toolbar button").filter({ hasText: "重试本页" }).click();
    await waitResultIdle(page);
    const afterRetry = await resultState(page);
    await page.unroute("**/sources/*/outputs*");

    // 2. Delay one page response, leave and return; the late page must not
    //    overwrite the new context.
    await pageOne();
    let delayedOnce = true;
    await page.route("**/sources/*/outputs*", async (route) => {
      if (delayedOnce) {
        delayedOnce = false;
        const response = await route.fetch();
        await new Promise((resolve) => setTimeout(resolve, 2500));
        await route.fulfill({ response });
      } else {
        await route.continue();
      }
    });
    await page.locator(".gm-result-toolbar button").filter({ hasText: "下一页" }).first().click();
    await page.waitForTimeout(150);
    await page.getByRole("button", { name: "← 任务列表" }).click();
    await page.waitForSelector(".gm-job-card", { timeout: 30000 });
    await page.waitForTimeout(500);
    await openJob(page, job);
    await openFirstResults(page);
    await page.waitForTimeout(3500); // let the delayed old response arrive
    const afterLate = await resultState(page);
    await page.unroute("**/sources/*/outputs*");

    // The page must still be usable and advance to page 2.
    await clickResultButton(page, "下一页");
    const afterLateNext = await resultState(page);

    // 3. Compare while a page request is in flight.
    await clickResultButton(page, "首页");
    await page.route("**/sources/*/outputs*", async (route) => {
      const response = await route.fetch();
      await new Promise((resolve) => setTimeout(resolve, 1800));
      await route.fulfill({ response });
    });
    await page.locator(".gm-result-toolbar button").filter({ hasText: "下一页" }).first().click();
    await page.waitForTimeout(150);
    const card = page.locator(".gm-thumb-card").first();
    await card.locator(".gm-thumb-open").click();
    await page.waitForSelector(".gm-compare-grid", { timeout: 30000 });
    const compareOpen = await page.locator(".gm-compare-grid").isVisible();
    await page.locator('.gm-modal-close[aria-label="关闭对话框"]').first().click();
    await waitResultIdle(page, 30000);
    const afterCompareInFlight = await resultState(page);
    await page.unroute("**/sources/*/outputs*");

    const checks = {
      initial_page_kept: initial.cards.length === 50 && initial.label.includes("第 1 页"),
      failure_keeps_cards: afterFailure.cards.length === 50 && outMatch(afterFailure.cards) === initialIds,
      failure_keeps_cursor: afterFailure.label.includes("第 1 页"),
      retry_advances: afterRetry.label.includes("第 2 页") && afterRetry.cards.length === 50,
      late_response_ignored: afterLate.label.includes("第 1 页")
        && outMatch(afterLate.cards) === initialIds,
      late_response_not_stuck: afterLate.buttons.every((button) => !button.disabled || button.text === "首页" || button.text.includes("上一页")),
      page_still_usable: afterLateNext.label.includes("第 2 页") && afterLateNext.cards.length === 50,
      compare_while_loading: compareOpen === true,
      compare_does_not_duplicate: afterCompareInFlight.cards.length === 50
        && new Set(afterCompareInFlight.cards.map((c) => c.id)).size === 50,
      no_permanent_loading: afterCompareInFlight.buttons.every((button) => button.text !== "读取中…"),
    };
    const result = {
      schema_version: 1,
      case: "H06-D",
      provenance: { data_source: "real_gpu", environment: "isolated", host: "real" },
      job,
      initial: { label: initial.label, count: initial.cards.length },
      after_failure: { label: afterFailure.label, count: afterFailure.cards.length, buttons: afterFailure.buttons },
      after_retry: { label: afterRetry.label, count: afterRetry.cards.length },
      after_late: { label: afterLate.label, count: afterLate.cards.length },
      after_late_next: { label: afterLateNext.label, count: afterLateNext.cards.length },
      after_compare_in_flight: { label: afterCompareInFlight.label, count: afterCompareInFlight.cards.length },
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
      schema_version: 1, case: "H06-D", job,
      provenance: { data_source: "real_gpu", environment: "isolated", host: "real" },
      result: "FAIL", errors: [...errors, `${error.stack || error}`],
    };
    await saveJson(outPath, result);
    console.error(JSON.stringify(result, null, 2));
    await browser.close();
    process.exit(1);
  }
})();
