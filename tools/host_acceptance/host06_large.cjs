// H06-C: real ComfyUI host loading a 5000-source / 10000-OUT fixture.
// The data is a fixture; the host and browser are real.
const fs = require("node:fs/promises");
const {
  arg, launch, openGacha, openJob, saveJson, screenshot,
} = require("./host_ui.cjs");

async function sourceState(page) {
  return page.evaluate(() => {
    const rows = [...document.querySelectorAll(".gm-source-row")].map((row) => ({
      order: Number(row.dataset.sourceOrder),
      id: row.dataset.sourceId,
      text: row.innerText.slice(0, 200),
    }));
    const pager = document.querySelector(".gm-source-pager");
    return {
      rows,
      label: pager?.querySelector(".gm-pager-position")?.textContent || null,
      counts: document.querySelector(".gm-sources-counts")?.innerText || null,
      buttons: pager ? [...pager.querySelectorAll("button")].map((button) => ({
        text: button.textContent.trim(), disabled: button.disabled,
      })) : [],
    };
  });
}

(async () => {
  const baseUrl = arg("base-url", "http://127.0.0.1:8296");
  const job = arg("job");
  const outPath = arg("out", "H06-C.json");
  const shots = arg("screenshots", null);
  if (!job) throw new Error("--job is required");
  if (shots) await fs.mkdir(shots, { recursive: true });

  const browser = await launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  const requests = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("request", (request) => {
    const url = request.url();
    if (url.includes("/gacha_master/v1/")) requests.push(url);
  });

  try {
    await openGacha(page, baseUrl);
    await openJob(page, job);
    const first = await sourceState(page);
    if (shots) await screenshot(page, `${shots}/H06-C-sources-page1.png`);

    let pages = 1;
    let previousLabel = first.label;
    for (let index = 0; index < 200; index += 1) {
      await page.waitForFunction(() => {
        return document.querySelector(".gm-source-rows")?.getAttribute("aria-busy") !== "true";
      }, { timeout: 30000 });
      const next = page.locator(".gm-source-pager button").filter({ hasText: "下一页" }).first();
      if (await next.isDisabled()) {
        await page.waitForTimeout(500);
        await page.waitForFunction(() => {
          return document.querySelector(".gm-source-rows")?.getAttribute("aria-busy") !== "true";
        }, { timeout: 30000 });
        if (await next.isDisabled()) break;
      }
      await next.click();
      await page.waitForFunction((label) => {
        const current = document.querySelector(".gm-source-pager .gm-pager-position")?.textContent || null;
        const busy = document.querySelector(".gm-source-rows")?.getAttribute("aria-busy");
        return current !== label && busy !== "true";
      }, previousLabel, { timeout: 30000 });
      await page.waitForTimeout(60);
      previousLabel = (await sourceState(page)).label;
      pages += 1;
    }
    const last = await sourceState(page);
    if (shots) await screenshot(page, `${shots}/H06-C-sources-last.png`);

    const jumpInput = page.getByPlaceholder("SRC 编号");
    await jumpInput.fill("4321");
    await page.getByRole("button", { name: "定位" }).click();
    await page.waitForTimeout(800);
    const jump = await sourceState(page);
    if (shots) await screenshot(page, `${shots}/H06-C-jump-4321.png`);

    // Open the result strip of the first source on the last page.
    const view = page.getByRole("button", { name: "查看结果" }).first();
    await view.click();
    await page.waitForSelector(".gm-thumb-card", { timeout: 60000 });
    await page.waitForTimeout(600);
    const result = await page.evaluate(() => ({
      cards: document.querySelectorAll(".gm-thumb-card").length,
      label: document.querySelector(".gm-result-toolbar .gm-pager-position")?.textContent || null,
      sourceRows: document.querySelectorAll(".gm-source-row").length,
    }));
    if (shots) await screenshot(page, `${shots}/H06-C-late-source-results.png`);

    const sourceRequests = requests.filter((url) => url.includes("/sources?"));
    const outputRequests = requests.filter((url) => url.includes("/outputs?"));
    const maxSourceLimit = Math.max(0, ...sourceRequests.map((url) => Number(new URL(url).searchParams.get("limit") || 0)));
    const maxOutputLimit = Math.max(0, ...outputRequests.map((url) => Number(new URL(url).searchParams.get("limit") || 0)));
    const checks = {
      first_page_40: first.rows.length === 40 && first.rows[0].order === 1,
      pages_125: pages === 125,
      last_page_40: last.rows.length === 40,
      last_page_range: last.rows.length === 40
        && last.rows[0].order === 4961 && last.rows[last.rows.length - 1].order === 5000,
      jump_4321: jump.rows.length > 0 && jump.rows[0].order === 4321,
      source_limit_bounded: maxSourceLimit > 0 && maxSourceLimit <= 200,
      result_limit_bounded: outputRequests.length > 0 && maxOutputLimit <= 200,
      late_source_results: result.cards === 2 && result.label.includes("第 1 页"),
      no_full_output_read_on_open: sourceRequests.length > 0 && outputRequests.every((url) => Number(new URL(url).searchParams.get("limit") || 0) <= 200),
    };
    const report = {
      schema_version: 1,
      case: "H06-C",
      provenance: { data_source: "fixture", environment: "isolated", host: "real" },
      job,
      first_page: { rows: first.rows.length, first: first.rows[0], label: first.label, counts: first.counts },
      pages,
      last_page: { rows: last.rows.length, first: last.rows[0], last: last.rows[last.rows.length - 1], label: last.label },
      jump: { rows: jump.rows.length, first: jump.rows[0], label: jump.label },
      late_source_result: result,
      request_count: requests.length,
      source_request_count: sourceRequests.length,
      output_request_count: outputRequests.length,
      max_source_limit: maxSourceLimit,
      max_output_limit: maxOutputLimit,
      errors,
      checks,
      result: Object.values(checks).every(Boolean) ? "PASS" : "FAIL",
    };
    await saveJson(outPath, report);
    console.log(JSON.stringify(report, null, 2));
    await browser.close();
    process.exit(report.result === "PASS" ? 0 : 1);
  } catch (error) {
    const report = {
      schema_version: 1, case: "H06-C", job,
      provenance: { data_source: "fixture", environment: "isolated", host: "real" },
      result: "FAIL", errors: [...errors, `${error.stack || error}`], requests,
    };
    await saveJson(outPath, report);
    console.error(JSON.stringify(report, null, 2));
    await browser.close();
    process.exit(1);
  }
})();
