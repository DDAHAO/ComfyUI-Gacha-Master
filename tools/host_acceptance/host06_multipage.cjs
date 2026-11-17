// H06-A: real-host single-source multi-page navigation at the 50/51 and
// 100/101 boundaries. Requires a real job with at least 101 OUT on SRC-0001.
const path = require("node:path");
const fs = require("node:fs/promises");
const {
  arg, launch, openGacha, openJob, openFirstResults, resultState,
  drawNo, outNo, clickResultButton, openCompare, saveJson, screenshot, waitResultIdle,
} = require("./host_ui.cjs");

(async () => {
  const baseUrl = arg("base-url", "http://127.0.0.1:8296");
  const job = arg("job");
  const expected = Number(arg("expected-out", "101"));
  const outPath = arg("out", "H06-A.json");
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
  const requests = [];
  page.on("request", (request) => {
    const url = request.url();
    if (url.includes("/gacha_master/v1/") && url.includes("/sources") && url.includes("/outputs")) {
      requests.push(url);
    }
  });

  try {
    await openGacha(page, baseUrl);
    await openJob(page, job);
    await openFirstResults(page);
    const pages = [];
    const first = await resultState(page);
    pages.push(first);
    if (shots) await screenshot(page, path.join(shots, "H06-A-page-1.png"));

    for (let index = 0; index < 10; index += 1) {
      const next = page.locator(".gm-result-toolbar button").filter({ hasText: "下一页" }).first();
      if (await next.isDisabled()) break;
      await clickResultButton(page, "下一页");
      pages.push(await resultState(page));
      if (shots) await screenshot(page, path.join(shots, `H06-A-page-${pages.length}.png`));
    }

    // First/previous/last navigation.
    await clickResultButton(page, "首页");
    const home = await resultState(page);
    const beforeFirst = home;
    await clickResultButton(page, "下一页");
    await clickResultButton(page, "下一页");
    const boundary = await resultState(page);

    // Revisit page 1 after the three-page cache has been evicted.
    await clickResultButton(page, "首页");
    const revisit = await resultState(page);

    // Compare on page 1 and on the final page.
    const compareFirst = await openCompare(page, 0);
    await clickResultButton(page, "下一页");
    await clickResultButton(page, "下一页");
    const compareLast = await openCompare(page, 0);

    const allCards = pages.flatMap((item) => item.cards);
    const ids = allCards.map((card) => card.id);
    const numbers = allCards.map(drawNo).filter((value) => value !== null);
    const sizes = pages.map((item) => item.cards.length);
    const expectedNumbers = Array.from({ length: expected }, (_, index) => index + 1);
    const checks = {
      all_reachable: ids.length === expected && new Set(ids).size === expected,
      draw_order_complete: JSON.stringify(numbers) === JSON.stringify(expectedNumbers),
      sizes_50_50_1: JSON.stringify(sizes) === JSON.stringify([50, 50, 1]),
      page2_starts_51: sizes[0] === 50 && drawNo(pages[1].cards[0]) === 51,
      page3_starts_101: sizes[2] >= 1 && drawNo(pages[2].cards[0]) === 101,
      one_out_per_draw: allCards.every((card) => outNo(card) === 1),
      first_page_prev_disabled: beforeFirst.buttons.find((b) => b.text.includes("上一页"))?.disabled === true,
      zero_horizontal_overflow: await page.evaluate(() => {
        const host = document.querySelector(".gacha-master-host");
        return !host || host.scrollWidth <= host.clientWidth + 2;
      }),
      revisit_same_ids: JSON.stringify(home.cards.map((c) => c.id)) === JSON.stringify(revisit.cards.map((c) => c.id)),
      compare_first_images: compareFirst.imageCount >= 2 && compareFirst.widths.every((width) => width > 0),
      compare_last_images: compareLast.imageCount >= 2 && compareLast.widths.every((width) => width > 0),
      no_wrong_source_image: allCards.every((card) => !card.imageSrc || card.imageSrc.includes(`/outputs/${card.id}/thumbnail`)),
    };
    const result = {
      schema_version: 1,
      case: "H06-A",
      provenance: { data_source: "real_gpu", environment: "isolated", host: "real" },
      job,
      expected_out: expected,
      page_sizes: sizes,
      page_labels: pages.map((item) => item.label),
      first_page: pages[0].cards.slice(0, 2),
      page2_first: pages[1]?.cards[0] || null,
      page3_first: pages[2]?.cards[0] || null,
      total_cards_seen: ids.length,
      unique_ids: new Set(ids).size,
      numbers_complete: numbers.length === expected && Math.min(...numbers) === 1 && Math.max(...numbers) === expected,
      boundary,
      home_buttons: home.buttons,
      before_first_buttons: beforeFirst.buttons,
      revisit_same_ids: checks.revisit_same_ids,
      compare_first: compareFirst,
      compare_last: compareLast,
      requests: requests.slice(0, 40),
      request_count: requests.length,
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
      schema_version: 1, case: "H06-A", job,
      provenance: { data_source: "real_gpu", environment: "isolated", host: "real" },
      result: "FAIL", errors: [...errors, `${error.stack || error}`], requests,
    };
    await saveJson(outPath, result);
    console.error(JSON.stringify(result, null, 2));
    await browser.close();
    process.exit(1);
  }
})();
